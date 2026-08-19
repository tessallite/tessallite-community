"""Tests for the webhook / event notification system (Phase 3, Block C)."""
from __future__ import annotations

import asyncio
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from sqlalchemy.dialects import postgresql

from shared.webhooks.dispatcher import reveal_destination_url
from src.main import app
from src.auth.middleware import CurrentUser, get_current_user, require_tenant_admin
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_admin() -> CurrentUser:
    return CurrentUser(user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID)


def _make_endpoint(
    endpoint_id: uuid.UUID | None = None,
    name: str = "my-hook",
    url: str = "https://example.com/hook",
    event_filters: list | None = None,
    is_active: bool = True,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=endpoint_id or uuid.uuid4(),
        name=name,
        url=url,
        signing_secret=b"encrypted-bytes",
        event_filters=event_filters or ["*"],
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def _make_delivery(
    delivery_id: uuid.UUID | None = None,
    endpoint_id: uuid.UUID | None = None,
    status: str = "delivered",
    event_type: str = "test.ping",
    destination_url_snapshot: str | None = "https://example.com/hook",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=delivery_id or uuid.uuid4(),
        endpoint_id=endpoint_id or uuid.uuid4(),
        event_type=event_type,
        payload={"message": "test"},
        status=status,
        attempts=1,
        response_code=200,
        error_message=None,
        created_at=NOW,
        # Bug-8557: every row the enqueue paths write now pins the destination
        # it was queued for, so the default here models a real row. Pass None
        # to model a legacy row written before migration 0202.
        destination_url_snapshot=destination_url_snapshot,
    )


@pytest.fixture
def admin_auth():
    admin = _make_admin()
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_tenant_admin] = lambda: admin
    yield admin
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_tenant_admin, None)


@pytest.fixture
async def client(admin_auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Dispatcher unit tests
# ---------------------------------------------------------------------------

class TestDispatcher:

    def test_compute_signature_format(self):
        from shared.webhooks.dispatcher import compute_signature

        sig = compute_signature("secret123", 1700000000, b'{"test":true}')
        assert sig.startswith("t=1700000000,v1=")
        assert len(sig.split(",v1=")[1]) == 64

    def test_compute_signature_deterministic(self):
        from shared.webhooks.dispatcher import compute_signature

        s1 = compute_signature("abc", 100, b"body")
        s2 = compute_signature("abc", 100, b"body")
        assert s1 == s2

    def test_compute_signature_changes_with_secret(self):
        from shared.webhooks.dispatcher import compute_signature

        s1 = compute_signature("key1", 100, b"body")
        s2 = compute_signature("key2", 100, b"body")
        assert s1 != s2

    def test_event_matches_wildcard(self):
        from shared.webhooks.dispatcher import _event_matches

        assert _event_matches(["*"], "model.published") is True

    def test_event_matches_exact(self):
        from shared.webhooks.dispatcher import _event_matches

        assert _event_matches(["model.published", "user.created"], "model.published") is True
        assert _event_matches(["model.published"], "user.created") is False

    def test_event_matches_empty_filters(self):
        from shared.webhooks.dispatcher import _event_matches

        assert _event_matches([], "model.published") is False

    @pytest.mark.asyncio
    async def test_generate_signing_secret(self):
        from shared.webhooks.dispatcher import generate_signing_secret

        with patch("shared.security.credential_crypto.get_settings") as mock_settings:
            from cryptography.fernet import Fernet
            key = Fernet.generate_key().decode()
            mock_settings.return_value.CREDENTIAL_ENCRYPTION_KEY = key

            plaintext, encrypted = generate_signing_secret()
            assert len(plaintext) > 20
            assert isinstance(encrypted, bytes)

            f = Fernet(key.encode())
            decrypted = f.decrypt(encrypted).decode()
            assert decrypted == plaintext

    # ------------------------------------------------------------------
    # Bug-5951: signing secret validation
    # ------------------------------------------------------------------

    def test_is_valid_signing_secret_rejects_none_empty_and_whitespace(self):
        """Bug-5951: None, empty string, and whitespace-only must never be
        used for HMAC."""
        from shared.webhooks.dispatcher import is_valid_signing_secret

        assert is_valid_signing_secret(None) is False
        assert is_valid_signing_secret("") is False
        assert is_valid_signing_secret("   ") is False
        assert is_valid_signing_secret("\t\n") is False

    @pytest.mark.parametrize("placeholder", [
        "changeme", "secret", "placeholder", "test", "password",
        "CHANGEME", "  secret  ", "Placeholder",
    ])
    def test_is_valid_signing_secret_rejects_placeholders(self, placeholder):
        """Bug-5951: known placeholder values (case-insensitive) are rejected."""
        from shared.webhooks.dispatcher import is_valid_signing_secret

        assert is_valid_signing_secret(placeholder) is False

    def test_is_valid_signing_secret_accepts_real_secret(self):
        """Bug-5951: a strong random secret passes validation."""
        from shared.webhooks.dispatcher import is_valid_signing_secret

        assert is_valid_signing_secret("wJalrXUtnFEMI_K7MDENGbPxRfiCYzExampleKey") is True

    def test_rebuild_signed_body_omits_signature_for_placeholder_secret(self):
        """Bug-5951: rebuild_signed_body returns an empty sig_header when the
        decrypted secret is a known placeholder."""
        from shared.webhooks.dispatcher import rebuild_signed_body

        ep = _make_endpoint()
        delivery = _make_delivery(endpoint_id=ep.id, status="pending")

        with patch("shared.webhooks.dispatcher._decrypt_secret", return_value="changeme"):
            _url, _body, sig_header = rebuild_signed_body(ep, delivery)
        assert sig_header == ""

    def test_rebuild_signed_body_signs_with_real_secret(self):
        """Bug-5951: rebuild_signed_body produces an HMAC when the secret is real."""
        from shared.webhooks.dispatcher import rebuild_signed_body

        ep = _make_endpoint()
        delivery = _make_delivery(endpoint_id=ep.id, status="pending")

        with patch("shared.webhooks.dispatcher._decrypt_secret", return_value="real-production-key-abc123"):
            _url, _body, sig_header = rebuild_signed_body(ep, delivery)
        assert sig_header.startswith("t=")
        assert ",v1=" in sig_header

    # ------------------------------------------------------------------
    # Bug-5952: fresh timestamp on retries
    # ------------------------------------------------------------------

    def test_rebuild_signed_body_fresh_timestamp_per_call(self):
        """Bug-5952: each call to rebuild_signed_body must produce a fresh
        timestamp so retries do not reuse a stale signature window."""
        from shared.webhooks.dispatcher import rebuild_signed_body

        ep = _make_endpoint()
        delivery = _make_delivery(endpoint_id=ep.id, status="pending")

        with (
            patch("shared.webhooks.dispatcher._decrypt_secret", return_value="real-secret-key-abc"),
            patch("shared.webhooks.dispatcher.time") as mock_time,
        ):
            mock_time.time.side_effect = [1700000000, 1700000010]
            _url1, body1, sig1 = rebuild_signed_body(ep, delivery)
            _url2, body2, sig2 = rebuild_signed_body(ep, delivery)

        # The two signatures must have different timestamps.
        assert "t=1700000000," in sig1
        assert "t=1700000010," in sig2
        assert sig1 != sig2

    # ------------------------------------------------------------------
    # Bug-5951: _post_once header omission
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_post_once_omits_signature_header_when_empty(self):
        """Bug-5951: _post_once must not include X-Tessallite-Signature when
        sig_header is empty -- an empty header misleads the receiver.

        Bug-7334: _post_once now uses client.stream() instead of client.post()
        for bounded response reading. The mock must provide an async context
        manager for the stream response.
        """
        from shared.webhooks.dispatcher import _post_once

        captured_headers = {}

        # Build a mock streaming response context manager.
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        class FakeStream:
            async def __aenter__(self):
                return mock_resp
            async def __aexit__(self, *a):
                pass

        def _fake_stream(method, url, *, content, headers):
            captured_headers.update(headers)
            return FakeStream()

        fake_client = AsyncMock()
        fake_client.stream = _fake_stream
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)

        with patch("shared.webhooks.dispatcher.httpx.AsyncClient", return_value=fake_client):
            ok, status, error = await _post_once(
                "https://example.com/hook", b'{"test":true}', "", "model.published",
            )

        assert ok is True
        assert "X-Tessallite-Signature" not in captured_headers
        assert captured_headers["X-Tessallite-Event"] == "model.published"

    @pytest.mark.asyncio
    async def test_post_once_includes_signature_header_when_present(self):
        """Bug-5951: _post_once includes X-Tessallite-Signature when a real
        HMAC signature is provided.

        Bug-7334: uses client.stream() mock (see above).
        """
        from shared.webhooks.dispatcher import _post_once

        captured_headers = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        class FakeStream:
            async def __aenter__(self):
                return mock_resp
            async def __aexit__(self, *a):
                pass

        def _fake_stream(method, url, *, content, headers):
            captured_headers.update(headers)
            return FakeStream()

        fake_client = AsyncMock()
        fake_client.stream = _fake_stream
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)

        with patch("shared.webhooks.dispatcher.httpx.AsyncClient", return_value=fake_client):
            ok, status, error = await _post_once(
                "https://example.com/hook", b'{"test":true}',
                "t=1700000000,v1=abc123", "model.published",
            )

        assert ok is True
        assert captured_headers["X-Tessallite-Signature"] == "t=1700000000,v1=abc123"

    # ------------------------------------------------------------------
    # Bug-5951: deliver_test_event with placeholder secret
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_deliver_test_event_placeholder_secret_is_not_sent_or_delivered(self):
        """Bug-8056 (F-022-07): a placeholder/invalid signing secret means no
        HMAC can be produced. An unsigned webhook must NEVER be transmitted and
        must NEVER be recorded as delivered — it is a terminal failed (dlq)
        outcome the operator can see.

        Test escape: the prior test asserted an unsigned test event was still
        recorded 'delivered', encoding the exact fail-open bug. Guard:
        attempt_delivery refuses to POST when sig_header is empty and records
        dlq. Tier: T1.
        """
        from shared.webhooks.dispatcher import deliver_test_event

        ep = _make_endpoint()
        db = make_mock_db()

        with (
            patch("shared.webhooks.dispatcher._decrypt_secret", return_value="changeme"),
            patch("shared.webhooks.dispatcher._post_once", new_callable=AsyncMock) as mock_post,
            patch("shared.webhooks.dispatcher.validate_webhook_url", return_value=ep.url),
        ):
            mock_post.return_value = (True, 200, None)
            delivery = await deliver_test_event(db, ep, {"msg": "hello"})

            # The payload must NOT be transmitted at all — an unsigned webhook
            # is never sent.
            mock_post.assert_not_awaited()
            # And the outcome is a terminal failure, never 'delivered'.
            assert delivery.status == "dlq"
            assert delivery.status != "delivered"
            assert "signing secret" in (delivery.error_message or "")


class TestSignedDeliveryFailClosed:
    """Bug-8056 (F-022-07): sign every webhook; mark delivered only on a real
    successful signed send."""

    @pytest.mark.asyncio
    async def test_attempt_delivery_marks_delivered_only_after_signed_post(self):
        """A signed body that gets a 2xx is delivered (the happy path is
        unchanged): _post_once is called with the signature and the row is
        delivered."""
        from shared.webhooks.dispatcher import attempt_delivery

        delivery = _make_delivery(status="pending")
        delivery.attempts = 0
        captured = {}

        async def fake_post(url, body, sig, event_type):
            captured["sig"] = sig
            return True, 200, None

        with patch("shared.webhooks.dispatcher._post_once", side_effect=fake_post) as mp:
            await attempt_delivery(
                delivery, "https://x/hook", b"{}", "t=1700000000,v1=abcd",
            )
        mp.assert_awaited_once()
        assert captured["sig"] == "t=1700000000,v1=abcd"
        assert delivery.status == "delivered"

    @pytest.mark.asyncio
    async def test_attempt_delivery_refuses_unsigned_and_records_failed(self):
        """An empty signature (missing/placeholder/undecryptable secret) must
        NOT be transmitted and must NOT be recorded as delivered — a terminal
        failed (dlq) outcome with a clear operator-facing error instead.

        Test escape: attempt_delivery previously POSTed the unsigned body and a
        2xx set status='delivered'. Guard: fail-closed on empty sig_header.
        Tier: T1.
        """
        from shared.webhooks.dispatcher import attempt_delivery

        delivery = _make_delivery(status="pending")
        delivery.attempts = 0

        with patch(
            "shared.webhooks.dispatcher._post_once", new_callable=AsyncMock,
        ) as mp:
            await attempt_delivery(delivery, "https://x/hook", b"{}", "")

        mp.assert_not_awaited()
        assert delivery.status == "dlq"
        assert delivery.status != "delivered"
        assert delivery.response_code is None
        assert "signing secret" in (delivery.error_message or "")

    def test_outgoing_signature_is_verifiable_by_receiver(self):
        """A receiver holding the shared secret can verify the signature the
        dispatcher emits — proving webhooks are authenticated, not merely
        stamped with an opaque header."""
        import hashlib
        import hmac
        from shared.webhooks.dispatcher import compute_signature

        secret = "receiver-shared-secret-abc123"
        body = b'{"event_type":"model.published"}'
        sig = compute_signature(secret, 1700000000, body)

        parts = dict(p.split("=", 1) for p in sig.split(","))
        msg = f"{parts['t']}.".encode() + body
        expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(parts["v1"], expected)
        # A different secret must NOT verify.
        wrong = hmac.new(b"wrong-secret", msg, hashlib.sha256).hexdigest()
        assert not hmac.compare_digest(parts["v1"], wrong)


@pytest.mark.anyio
async def test_spawn_background_tracks_and_releases_task():
    """Bug-6004: a bare ``asyncio.create_task(...)`` result is not referenced
    anywhere, so the event loop is free to garbage-collect the Task mid-flight
    (see the "Important" note in the asyncio docs). ``_spawn_background`` must
    hold a strong reference until the task finishes, then release it."""
    from shared.webhooks import dispatcher as disp

    release = asyncio.Event()

    async def _work():
        await release.wait()

    task = disp._spawn_background(_work())
    try:
        assert task in disp._background_tasks
    finally:
        release.set()
        await task
    # The done-callback is scheduled via call_soon; yield once so it runs.
    await asyncio.sleep(0)
    assert task not in disp._background_tasks


@pytest.mark.anyio
async def test_dispatch_queued_deliveries_claims_rows_with_skip_locked(monkeypatch):
    """Bug-6005: the background dispatch path spawned by ``emit_webhook``
    must claim delivery rows under the same ``FOR UPDATE SKIP LOCKED`` guard
    the scheduler drain sweep uses. Without a shared lock, the drain sweep
    could grab the same row while this path is mid-POST and deliver the same
    webhook twice."""
    from shared.webhooks import dispatcher as disp

    captured = {}
    delivery = _make_delivery(status="pending")
    ep = _make_endpoint(endpoint_id=delivery.endpoint_id)

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = delivery

    async def _execute(stmt):
        captured["stmt"] = stmt
        return result

    db.execute = AsyncMock(side_effect=_execute)
    db.get = AsyncMock(return_value=ep)
    db.commit = AsyncMock()

    async def _tenant_db(_tenant_id):
        yield db

    async def fake_attempt(d, url_, body_, sig_):
        d.status = "delivered"

    monkeypatch.setattr(disp, "get_tenant_db", lambda tenant_id: _tenant_db(tenant_id))
    monkeypatch.setattr(disp, "rebuild_signed_body", lambda ep_, d: (ep_.url, b"{}", ""))
    monkeypatch.setattr(disp, "attempt_delivery", fake_attempt)

    await disp._dispatch_queued_deliveries(TEST_TENANT, [delivery.id])

    sql = str(captured["stmt"].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql
    assert delivery.status == "delivered"


@pytest.mark.anyio
async def test_dispatch_queued_deliveries_skips_row_locked_by_drain(monkeypatch):
    """Bug-6005: if the drain sweep already holds the row's lock (SKIP LOCKED
    returns nothing), the background dispatch path must back off rather than
    proceeding without a lock -- that's exactly the race the shared lock
    closes."""
    from shared.webhooks import dispatcher as disp

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock()

    async def _tenant_db(_tenant_id):
        yield db

    monkeypatch.setattr(disp, "get_tenant_db", lambda tenant_id: _tenant_db(tenant_id))

    await disp._dispatch_queued_deliveries(TEST_TENANT, [uuid.uuid4()])

    db.get.assert_not_called()


@pytest.mark.anyio
async def test_drain_pending_deliveries_claims_rows_with_skip_locked(monkeypatch):
    """Bug-5276: retry drains claim due rows with row locks before POST."""
    from shared.webhooks import dispatcher as disp

    captured = {}
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []

    async def _execute(stmt):
        captured["stmt"] = stmt
        return result

    db.execute = AsyncMock(side_effect=_execute)

    async def _tenant_db(_tenant_id):
        yield db

    monkeypatch.setattr(disp, "get_tenant_db", lambda tenant_id: _tenant_db(tenant_id))

    attempted = await disp.drain_pending_deliveries(TEST_TENANT)

    assert attempted == 0
    sql = str(captured["stmt"].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql


@pytest.mark.anyio
async def test_drain_pending_deliveries_reclaims_each_row_individually(monkeypatch):
    """Bug-6005 round-2: the batch-wide lock alone is not enough -- each
    per-row ``db.commit()`` releases the lock on every other row still
    queued in that batch (Postgres releases all locks a transaction holds
    at COMMIT). Drain must re-claim each row with its own
    ``FOR UPDATE SKIP LOCKED`` immediately before processing it. This test
    drives a non-empty ``due_ids`` list and asserts the per-row re-SELECT
    actually happens and carries the lock."""
    from shared.webhooks import dispatcher as disp

    delivery = _make_delivery(status="pending")
    ep = _make_endpoint(endpoint_id=delivery.endpoint_id)

    id_result = MagicMock()
    id_result.scalars.return_value.all.return_value = [delivery.id]

    row_result = MagicMock()
    row_result.scalar_one_or_none.return_value = delivery

    captured_stmts = []

    async def _execute(stmt):
        captured_stmts.append(stmt)
        # 1st call: the batch id-collecting SELECT. 2nd call: the per-row
        # re-claim SELECT.
        return id_result if len(captured_stmts) == 1 else row_result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.get = AsyncMock(return_value=ep)
    db.commit = AsyncMock()

    async def _tenant_db(_tenant_id):
        yield db

    async def fake_attempt(d, url_, body_, sig_):
        d.status = "delivered"

    monkeypatch.setattr(disp, "get_tenant_db", lambda tenant_id: _tenant_db(tenant_id))
    monkeypatch.setattr(disp, "rebuild_signed_body", lambda ep_, d: (ep_.url, b"{}", ""))
    monkeypatch.setattr(disp, "attempt_delivery", fake_attempt)

    attempted = await disp.drain_pending_deliveries(TEST_TENANT)

    assert attempted == 1
    assert delivery.status == "delivered"
    assert len(captured_stmts) == 2
    row_sql = str(captured_stmts[1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in row_sql
    assert "SKIP LOCKED" in row_sql
    # Bug-6005 round-2: the per-row re-SELECT must repeat the due-time
    # predicate, not just re-lock on id -- otherwise a row a concurrent
    # process rescheduled to a future backoff time would still look
    # claimable here (an early, backoff-violating retry).
    assert "next_attempt_at" in row_sql
    assert "<=" in row_sql


@pytest.mark.anyio
async def test_drain_pending_deliveries_skips_row_claimed_elsewhere_between_selects(
    monkeypatch,
):
    """Bug-6005 round-2: if a row that made it into the batch id list is no
    longer claimable by the time drain re-selects it individually --
    already locked by a concurrent dispatch, already delivered, or
    rescheduled to a future ``next_attempt_at`` -- drain must skip it, not
    deliver it a second time or force a lock past a concurrent claimant."""
    from shared.webhooks import dispatcher as disp

    delivery_id = uuid.uuid4()

    id_result = MagicMock()
    id_result.scalars.return_value.all.return_value = [delivery_id]

    row_result = MagicMock()
    row_result.scalar_one_or_none.return_value = None  # lost the race

    call_count = {"n": 0}

    async def _execute(stmt):
        call_count["n"] += 1
        return id_result if call_count["n"] == 1 else row_result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.get = AsyncMock()

    async def _tenant_db(_tenant_id):
        yield db

    monkeypatch.setattr(disp, "get_tenant_db", lambda tenant_id: _tenant_db(tenant_id))

    attempted = await disp.drain_pending_deliveries(TEST_TENANT)

    assert attempted == 0
    db.get.assert_not_called()


# ---------------------------------------------------------------------------
# API tests — CRUD
# ---------------------------------------------------------------------------

class TestWebhookCrud:

    @pytest.mark.asyncio
    async def test_list_webhooks_empty(self, client):
        db = make_mock_db()
        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.get("/api/v1/admin/webhooks")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_create_webhook(self, client):
        db = make_mock_db()
        ep_id = uuid.uuid4()

        async def mock_refresh(obj):
            obj.id = ep_id
            obj.is_active = True
            obj.created_at = NOW
            obj.updated_at = NOW

        db.refresh = AsyncMock(side_effect=mock_refresh)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.generate_signing_secret", return_value=("plain", b"enc")),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
        ):
            resp = await client.post(
                "/api/v1/admin/webhooks",
                json={"name": "test-hook", "url": "https://example.com/hook"},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-hook"
        assert data["url"] == "https://example.com/hook"
        assert data["event_filters"] == ["*"]
        # Bug-6312: the signing secret must be revealed exactly once on create.
        assert data["signing_secret"] == "plain"
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_create_webhook_secret_not_on_list(self, client):
        """Bug-6312: the signing secret must NOT leak through GET /webhooks
        (ordinary list).  Only the create 201 response reveals it."""
        db = make_mock_db()
        # Populate with a real endpoint so the loop body actually executes
        # (an empty list would make the assertion vacuous).
        ep = _make_endpoint()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [ep]
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.get("/api/v1/admin/webhooks")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 1, "list must be non-empty for this assertion to be meaningful"
        for item in items:
            assert "signing_secret" not in item

    @pytest.mark.asyncio
    async def test_update_webhook_does_not_leak_secret(self, client):
        """Bug-6312: PUT (update) must not reveal the signing secret."""
        ep = _make_endpoint()
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)

        async def mock_refresh(obj):
            pass

        db.refresh = AsyncMock(side_effect=mock_refresh)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
        ):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{ep.id}",
                json={"name": "updated-hook"},
            )
        assert resp.status_code == 200
        # Bug-8556 narrowed this contract rather than removing it: the update
        # response now carries a ``signing_secret`` FIELD, but it is populated
        # only when this update rotated the secret. An edit that did not
        # rotate — like this name change — must still disclose nothing.
        assert resp.json().get("signing_secret") is None

    @pytest.mark.asyncio
    async def test_changing_a_webhook_url_rotates_the_signing_secret(self, client):
        """Bug-8410 parity (R1 reviewer F6): the agent-service dispatcher
        rotates on receiver change; the platform-wide one must not silently
        hand receiver A's secret to receiver B.

        A's operator has that secret written down. Leaving it in place means
        they can forge events that B accepts as authentic, and B is verifying
        with a credential its own operator never issued."""
        ep = _make_endpoint(url="https://receiver-a.example/hooks/tok-A")
        original_secret = ep.signing_secret
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock) as audit_mock,
            patch(
                "src.api.webhooks.generate_signing_secret",
                return_value=("plaintext-B", b"secret-for-B"),
            ),
        ):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{ep.id}",
                json={"url": "https://receiver-b.example/hooks/tok-B"},
            )

        assert resp.status_code == 200
        assert ep.signing_secret == b"secret-for-B", (
            "the endpoint was repointed at a different receiver but the "
            "signing secret issued for the OLD receiver is still in place"
        )
        assert ep.signing_secret != original_secret
        assert audit_mock.call_args[1]["detail"]["signing_secret_rotated"] is True
        # Bug-8556: the rotation is no longer silent. The one-time plaintext
        # for the NEW receiver comes back in this response so the admin can
        # share it at the point of action; without it the endpoint is dead on
        # its first event and nothing in the product says so.
        assert resp.json()["signing_secret"] == "plaintext-B"

    @pytest.mark.asyncio
    async def test_url_change_logs_an_operator_warning(self, client, caplog):
        """R2 reviewer finding 1 -- the rotation is otherwise invisible: the
        response carries no plaintext, the SPA shows no dialog, and the admin
        help page used to say Edit only changes name/URL/filters. A
        signature-verifying receiver answers 401/403, which _is_non_retryable
        treats as terminal, so the endpoint is dead on the first event with no
        retry. agent-service's twin logs 'call rotate-secret to obtain the new
        secret'; this one must too."""
        ep = _make_endpoint(url="https://receiver-a.example/hooks/tok-A")
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)
        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
            caplog.at_level("WARNING"),
        ):
            await client.put(
                f"/api/v1/admin/webhooks/{ep.id}",
                json={"url": "https://receiver-b.example/hooks/tok-B"},
            )
        assert any(
            "rotate-secret" in r.message.lower()
            or "rotate secret" in r.message.lower()
            for r in caplog.records
        ), caplog.text

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_does_not_rotate_the_signing_secret(self, client):
        """The rule must not overshoot: renaming an endpoint has nothing to do
        with which party holds the credential."""
        ep = _make_endpoint(url="https://receiver-a.example/hooks/tok-A")
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock) as audit_mock,
            patch(
                "src.api.webhooks.generate_signing_secret",
                return_value=("plaintext-B", b"secret-for-B"),
            ) as gen_mock,
        ):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{ep.id}", json={"name": "renamed"},
            )

        assert resp.status_code == 200
        gen_mock.assert_not_called()
        assert ep.signing_secret == b"encrypted-bytes"
        assert audit_mock.call_args[1]["detail"]["signing_secret_rotated"] is False

    @pytest.mark.asyncio
    async def test_resaving_the_same_url_does_not_rotate(self, client):
        """An idempotent PUT of the current configuration is not a receiver
        change, and must not invalidate a working receiver's credential."""
        same = "https://receiver-a.example/hooks/tok-A"
        ep = _make_endpoint(url=same)
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
            patch(
                "src.api.webhooks.generate_signing_secret",
                return_value=("plaintext-B", b"secret-for-B"),
            ) as gen_mock,
        ):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{ep.id}", json={"url": same},
            )

        assert resp.status_code == 200
        gen_mock.assert_not_called()
        assert ep.signing_secret == b"encrypted-bytes"

    @pytest.mark.asyncio
    async def test_update_webhook(self, client):
        ep = _make_endpoint()
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)

        async def mock_refresh(obj):
            pass

        db.refresh = AsyncMock(side_effect=mock_refresh)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
        ):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{ep.id}",
                json={"name": "updated-hook"},
            )
        assert resp.status_code == 200
        assert ep.name == "updated-hook"

    @pytest.mark.asyncio
    async def test_update_webhook_not_found(self, client):
        db = make_mock_db()
        db.get = AsyncMock(return_value=None)

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{uuid.uuid4()}",
                json={"name": "nope"},
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_webhook(self, client):
        ep = _make_endpoint()
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
        ):
            resp = await client.delete(f"/api/v1/admin/webhooks/{ep.id}")
        assert resp.status_code == 204
        db.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_webhook_not_found(self, client):
        db = make_mock_db()
        db.get = AsyncMock(return_value=None)

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"/api/v1/admin/webhooks/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API tests — Event catalogue + filter validation (F-022-01)
# ---------------------------------------------------------------------------

class TestWebhookEventCatalogue:
    @pytest.mark.asyncio
    async def test_event_types_endpoint_returns_catalogue(self, client):
        from shared.webhooks.event_types import WEBHOOK_EVENT_TYPES
        resp = await client.get("/api/v1/admin/webhooks/event-types")
        assert resp.status_code == 200
        data = resp.json()
        values = {e["value"] for e in data}
        assert values == set(WEBHOOK_EVENT_TYPES)
        # aggregate.* events have no backend emitter — must not be offered.
        assert "aggregate.retired" not in values
        assert "aggregate.created" not in values
        # SLA breach must be selectable
        assert "refresh.sla_breach" in values
        # F-012-22: scheduled/manual refreshes now emit completion events, so
        # both are subscribable (refresh.failed is no longer a dead event).
        assert "refresh.completed" in values
        assert "refresh.failed" in values

    @pytest.mark.asyncio
    async def test_create_rejects_unknown_event_filter(self, client):
        db = make_mock_db()
        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                "/api/v1/admin/webhooks",
                json={
                    "name": "bad-hook",
                    "url": "https://example.com/hook",
                    # aggregate.created has no emitter — still an unknown filter.
                    # (refresh.failed is now a real event after F-012-22.)
                    "event_filters": ["aggregate.created"],
                },
            )
        assert resp.status_code == 422
        db.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_accepts_known_event_and_wildcard(self, client):
        db = make_mock_db()
        ep_id = uuid.uuid4()

        async def mock_refresh(obj):
            obj.id = ep_id
            obj.is_active = True
            obj.created_at = NOW
            obj.updated_at = NOW

        db.refresh = AsyncMock(side_effect=mock_refresh)
        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.generate_signing_secret", return_value=("plain", b"enc")),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
        ):
            resp = await client.post(
                "/api/v1/admin/webhooks",
                json={
                    "name": "good-hook",
                    "url": "https://example.com/hook",
                    "event_filters": ["refresh.sla_breach", "*"],
                },
            )
        assert resp.status_code == 201
        db.add.assert_called()

    @pytest.mark.asyncio
    async def test_update_rejects_unknown_event_filter(self, client):
        ep = _make_endpoint()
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)
        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{ep.id}",
                json={"event_filters": ["aggregate.created"]},
            )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# API tests — Test delivery
# ---------------------------------------------------------------------------

class TestWebhookTestDelivery:

    @pytest.mark.asyncio
    async def test_test_delivery(self, client):
        # F-022-06/07: the test endpoint returns the exact delivery row it
        # fired (deliver_test_event), not "the latest delivery for the
        # endpoint". One attempt, no inline backoff.
        ep = _make_endpoint()
        delivery = _make_delivery(endpoint_id=ep.id)
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)

        async def fake_deliver(db_, endpoint, payload):
            return delivery

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.deliver_test_event", side_effect=fake_deliver),
        ):
            resp = await client.post(f"/api/v1/admin/webhooks/{ep.id}/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["event_type"] == "test.ping"
        assert data["id"] == str(delivery.id)  # the row we fired, not a guess

    @pytest.mark.asyncio
    async def test_test_delivery_not_found(self, client):
        db = make_mock_db()
        db.get = AsyncMock(return_value=None)

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.post(f"/api/v1/admin/webhooks/{uuid.uuid4()}/test")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API tests — Secret rotation
# ---------------------------------------------------------------------------

class TestSecretRotation:

    @pytest.mark.asyncio
    async def test_rotate_secret(self, client):
        ep = _make_endpoint()
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.generate_signing_secret", return_value=("new-secret-plain", b"enc")),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
        ):
            resp = await client.post(f"/api/v1/admin/webhooks/{ep.id}/rotate-secret")
        assert resp.status_code == 200
        assert resp.json()["signing_secret"] == "new-secret-plain"

    @pytest.mark.asyncio
    async def test_rotate_secret_fails_closed_when_audit_write_fails(self, client):
        """F-022-02: rotation is a protected mutation. If its required audit
        record cannot be persisted, the request must fail (audit_required
        raises AuditWriteError) rather than silently rotating without evidence.

        Test escape: no test simulated an audit persistence failure on a
        protected route. Guard: producers call audit_required before commit.
        Tier: T1.
        """
        from shared.audit.logger import AuditWriteError

        ep = _make_endpoint()
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.generate_signing_secret", return_value=("new-secret-plain", b"enc")),
            patch(
                "src.api.webhooks.audit_required",
                new_callable=AsyncMock,
                side_effect=AuditWriteError("audit down"),
            ),
        ):
            # The audit failure must propagate out of the handler (fail-closed);
            # the ASGI test transport re-raises unhandled exceptions rather than
            # inventing a 500, which is exactly the "mutation did not succeed"
            # signal we assert here.
            with pytest.raises(AuditWriteError):
                await client.post(f"/api/v1/admin/webhooks/{ep.id}/rotate-secret")
        # The endpoint's rotation must not have been committed.
        db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# API tests — Delivery history
# ---------------------------------------------------------------------------

class TestDeliveryHistory:

    @pytest.mark.asyncio
    async def test_list_deliveries(self, client):
        db = make_mock_db()
        d1 = _make_delivery()
        d2 = _make_delivery(status="dlq")

        result = MagicMock()
        result.scalars.return_value.all.return_value = [d1, d2]
        db.execute = AsyncMock(return_value=result)

        ep_id = uuid.uuid4()
        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"/api/v1/admin/webhooks/{ep_id}/deliveries")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_list_deliveries_rejects_unbounded_limit(self, client):
        """Bug-8056 review #8: paging must be bounded so a caller cannot
        materialise the whole per-tenant delivery table (limit) or send a
        negative LIMIT to Postgres (500)."""
        ep_id = uuid.uuid4()
        db = make_mock_db()
        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            too_big = await client.get(
                f"/api/v1/admin/webhooks/{ep_id}/deliveries?limit=100000000"
            )
            negative = await client.get(
                f"/api/v1/admin/webhooks/{ep_id}/deliveries?limit=-1"
            )
        assert too_big.status_code == 422
        assert negative.status_code == 422

    @pytest.mark.asyncio
    async def test_list_dlq_rejects_unbounded_limit(self, client):
        db = make_mock_db()
        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.get("/api/v1/admin/webhooks/dlq?limit=100000000")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# API tests — DLQ lifecycle
# ---------------------------------------------------------------------------

class TestDlqLifecycle:

    @pytest.mark.asyncio
    async def test_list_dlq(self, client):
        db = make_mock_db()
        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.get("/api/v1/admin/webhooks/dlq")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_retry_dlq(self, client):
        # F-022-13: a manual DLQ retry is a single in-place re-attempt, not a
        # full inline backoff. On a failed attempt the delivery is left
        # ``pending`` (the scheduler drain job continues) — no inline sleeps.
        delivery = _make_delivery(status="dlq")
        delivery.next_attempt_at = None
        ep = _make_endpoint(endpoint_id=delivery.endpoint_id)
        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda cls, id: delivery if id == delivery.id else ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        async def fake_attempt(d, url, body, sig):
            d.attempts = (d.attempts or 0) + 1
            d.status = "pending"

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.rebuild_signed_body", return_value=(ep.url, b"{}", "")),
            patch("src.api.webhooks.attempt_delivery", side_effect=fake_attempt),
        ):
            resp = await client.post(f"/api/v1/admin/webhooks/dlq/{delivery.id}/retry")
        assert resp.status_code == 200
        # One re-attempt was made on the same row (attempts reset then bumped).
        assert delivery.attempts == 1
        assert delivery.status == "pending"

    @pytest.mark.asyncio
    async def test_retry_dlq_repins_current_secret(self, client):
        """Bug-8056: a manual DLQ retry must re-pin the delivery's signing
        snapshot to the endpoint's CURRENT secret, so a retry after the operator
        rotated the secret actually signs (the recovery the missing-signature
        failure instructs). Without this a row DLQ'd for an unsignable pinned
        secret would refuse-and-DLQ forever.
        """
        delivery = _make_delivery(status="dlq")
        delivery.next_attempt_at = None
        delivery.signing_secret_snapshot = b"stale-unsignable-snapshot"
        ep = _make_endpoint(endpoint_id=delivery.endpoint_id)
        ep.signing_secret = b"current-rotated-secret"
        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda cls, id: delivery if id == delivery.id else ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        captured = {}

        def fake_rebuild(endpoint, d):
            # Capture the snapshot the rebuild sees — it must already be re-pinned.
            captured["snapshot"] = d.signing_secret_snapshot
            return (endpoint.url, b"{}", "t=1,v1=abc")

        async def fake_attempt(d, url, body, sig):
            d.attempts = (d.attempts or 0) + 1
            d.status = "delivered"

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.rebuild_signed_body", side_effect=fake_rebuild),
            patch("src.api.webhooks.attempt_delivery", side_effect=fake_attempt),
        ):
            resp = await client.post(f"/api/v1/admin/webhooks/dlq/{delivery.id}/retry")
        assert resp.status_code == 200
        assert delivery.signing_secret_snapshot == b"current-rotated-secret"
        assert captured["snapshot"] == b"current-rotated-secret"

    @pytest.mark.asyncio
    async def test_retry_dlq_not_found(self, client):
        db = make_mock_db()
        db.get = AsyncMock(return_value=None)

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.post(f"/api/v1/admin/webhooks/dlq/{uuid.uuid4()}/retry")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_dlq(self, client):
        delivery = _make_delivery(status="dlq")
        db = make_mock_db()
        # Bug-8357 (R1 reviewer F5) -- delete_dlq loads the endpoint too, so
        # db.get must answer by model rather than returning one object for
        # every lookup.
        db.get = AsyncMock(side_effect=_delivery_then_endpoint(delivery))

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"/api/v1/admin/webhooks/dlq/{delivery.id}")
        assert resp.status_code == 204
        db.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_dlq_rejects_non_dlq(self, client):
        delivery = _make_delivery(status="delivered")
        db = make_mock_db()
        db.get = AsyncMock(return_value=delivery)

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"/api/v1/admin/webhooks/dlq/{delivery.id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bug-8350-sibling (R2 MED-4) — GET /admin/webhooks/dlq and
# GET /admin/webhooks/{id}/deliveries read-time redaction backstop, plus the
# webhook.dlq_delete / webhook.dlq_retry audit log entries. Defense in depth
# on top of the write-time scrub in attempt_delivery, for rows written
# before that fix shipped.
# ---------------------------------------------------------------------------


def _delivery_then_endpoint(
    delivery, url="https://receiver.example/hooks/bearer-tok-123?key=shh",
):
    """``db.get`` side effect that answers by MODEL, not by call order.

    Several handlers now load both a ``WebhookDelivery`` and its
    ``WebhookEndpoint``; a single ``return_value`` hands the endpoint lookup a
    delivery object and fails on ``.url``.
    """
    from shared.db.models import WebhookEndpoint

    async def _get(model, row_id):
        if model is WebhookEndpoint:
            return types.SimpleNamespace(
                id=delivery.endpoint_id, url=url, is_active=True,
            )
        return delivery

    return _get


class TestDeliveryErrorMessageReadTimeRedaction:

    @pytest.mark.asyncio
    async def test_list_dlq_scrubs_leaked_url_in_error_message(self, client):
        leaked = _make_delivery(status="dlq")
        leaked.error_message = (
            "404 Not Found: no route for "
            "https://receiver.example/hooks/bearer-tok-123?key=shh"
        )
        db = make_mock_db()
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [leaked]
        # Bug-8357 -- GET /dlq now also resolves each row's endpoint URL so the
        # read-time backstop can strip a scheme-less echo of the
        # credential-bearing path, not just text carrying a scheme delimiter.
        endpoint = types.SimpleNamespace(
            id=leaked.endpoint_id,
            url="https://receiver.example/hooks/bearer-tok-123?key=shh",
        )
        endpoints_result = MagicMock()
        endpoints_result.scalars.return_value.all.return_value = [endpoint]
        db.execute = AsyncMock(side_effect=[rows_result, endpoints_result])

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.get("/api/v1/admin/webhooks/dlq")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert "bearer-tok-123" not in (body[0]["error_message"] or "")
        assert "shh" not in (body[0]["error_message"] or "")

    @pytest.mark.asyncio
    async def test_list_deliveries_scrubs_leaked_url_in_error_message(self, client):
        leaked = _make_delivery(status="dlq")
        leaked.error_message = (
            "ConnectError: could not reach "
            "https://receiver.example/hooks/bearer-tok-123?key=shh"
        )
        db = make_mock_db()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [leaked]
        db.execute = AsyncMock(return_value=result)
        # Bug-8357 -- the handler loads the endpoint to feed its URL to the
        # backstop; make_mock_db's db.get returns None by default, which the
        # handler tolerates (falling back to the generic sweep). Give it the
        # real endpoint so the stronger path is what runs here.
        db.get = AsyncMock(return_value=types.SimpleNamespace(
            id=leaked.endpoint_id,
            url="https://receiver.example/hooks/bearer-tok-123?key=shh",
        ))

        with patch("src.api.webhooks.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/admin/webhooks/{leaked.endpoint_id}/deliveries"
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert "bearer-tok-123" not in (body[0]["error_message"] or "")
        assert "shh" not in (body[0]["error_message"] or "")

    @pytest.mark.asyncio
    async def test_delete_dlq_audit_log_scrubs_error_message(self, client):
        leaked = _make_delivery(status="dlq")
        leaked.error_message = (
            "404 Not Found: https://receiver.example/hooks/bearer-tok-123?key=shh"
        )
        db = make_mock_db()
        # Bug-8357 (R1 reviewer F5) -- delete_dlq now also loads the endpoint so
        # the audit-log scrub gets the URL, not just the scheme-anchored sweep.
        db.get = AsyncMock(side_effect=_delivery_then_endpoint(leaked))

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock) as audit_mock,
        ):
            resp = await client.delete(f"/api/v1/admin/webhooks/dlq/{leaked.id}")

        assert resp.status_code == 204
        audit_mock.assert_awaited_once()
        _, kwargs = audit_mock.call_args
        recorded = kwargs["detail"]["error_message"]
        assert "bearer-tok-123" not in recorded
        assert "shh" not in recorded

    @pytest.mark.asyncio
    async def test_delete_dlq_audit_scrubs_a_path_only_url_echo(self, client):
        """R1 reviewer F5 -- delete_dlq wrote error_message into the audit log
        with NO endpoint URL, so a receiver echo of only the credential-bearing
        path (no scheme for a pattern to anchor on) survived into a record that
        is longer-lived and more widely exported than the row being deleted."""
        leaked = _make_delivery(status="dlq")
        leaked.error_message = "404 Not Found: no route for /hooks/bearer-tok-123"
        db = make_mock_db()
        db.get = AsyncMock(side_effect=_delivery_then_endpoint(leaked))

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock) as audit_mock,
        ):
            resp = await client.delete(f"/api/v1/admin/webhooks/dlq/{leaked.id}")

        assert resp.status_code == 204
        recorded = audit_mock.call_args[1]["detail"]["error_message"]
        assert "bearer-tok-123" not in recorded, (
            "a path-only echo reached the audit log unredacted"
        )

    @pytest.mark.asyncio
    async def test_retry_dlq_audit_log_scrubs_prior_error(self, client):
        """Fresh-reviewer follow-up: the delete_dlq audit scrub is covered
        above, but retry_dlq's own `prior_error` scrub (a separate call
        site) was untested -- mutating only that line would have gone
        undetected."""
        leaked = _make_delivery(status="dlq")
        leaked.error_message = (
            "404 Not Found: https://receiver.example/hooks/bearer-tok-123?key=shh"
        )
        leaked.next_attempt_at = None
        ep = _make_endpoint(endpoint_id=leaked.endpoint_id)
        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda cls, id: leaked if id == leaked.id else ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        async def fake_attempt(d, url, body, sig):
            d.attempts = (d.attempts or 0) + 1
            d.status = "pending"

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.rebuild_signed_body", return_value=(ep.url, b"{}", "")),
            patch("src.api.webhooks.attempt_delivery", side_effect=fake_attempt),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock) as audit_mock,
        ):
            resp = await client.post(f"/api/v1/admin/webhooks/dlq/{leaked.id}/retry")

        assert resp.status_code == 200
        audit_mock.assert_awaited_once()
        _, kwargs = audit_mock.call_args
        recorded = kwargs["detail"]["prior_error"]
        assert "bearer-tok-123" not in recorded
        assert "shh" not in recorded


# ---------------------------------------------------------------------------
# Bug-8557 — the destination is pinned to the delivery row, like the secret
# ---------------------------------------------------------------------------

class TestBug8557DestinationPinning:
    """Bug-8557: a queued delivery must go to the receiver it was queued FOR.

    F-022-06 pinned the signing secret to a delivery at enqueue time but left
    the destination live, so ``rebuild_signed_body`` returned
    ``endpoint.url`` — the CURRENT value. An admin repointing an endpoint at a
    different receiver therefore redirected every already-queued delivery to
    it: receiver B got receiver A's payload, signed with A's pinned secret,
    which B cannot verify and should never have seen at all. Because a URL
    change also rotates the endpoint secret (Bug-8410 parity), the post-change
    state was not merely wrong but incoherent — B was handed a valid HMAC
    computed under a key it does not hold.
    """

    def test_bug_8557_rebuild_uses_the_pinned_url_not_the_edited_endpoint_url(self):
        from shared.webhooks.dispatcher import rebuild_signed_body

        ep = _make_endpoint(url="https://receiver-B.example/hook")
        # The row was enqueued while the endpoint still pointed at receiver A.
        delivery = _make_delivery(
            endpoint_id=ep.id,
            status="pending",
            destination_url_snapshot="https://receiver-A.example/hook",
        )

        with patch(
            "shared.webhooks.dispatcher._decrypt_secret",
            return_value="real-production-key-abc123",
        ):
            url, _body, sig_header = rebuild_signed_body(ep, delivery)

        assert url == "https://receiver-A.example/hook"
        assert "receiver-B" not in (url or "")
        # Still signed: pinning the destination must not disturb the secret.
        assert sig_header.startswith("t=")

    @pytest.mark.anyio
    async def test_bug_8557_emit_webhook_pins_the_destination_at_enqueue(
        self, monkeypatch,
    ):
        """The enqueue path writes the snapshot; without it every row would be
        incoherent and every event would dead-letter."""
        from shared.webhooks import dispatcher as disp

        ep = _make_endpoint(url="https://receiver-A.example/hook")
        added: list = []

        ep_result = MagicMock()
        ep_result.scalars.return_value.all.return_value = [ep]

        db = AsyncMock()
        db.execute = AsyncMock(return_value=ep_result)
        db.add = MagicMock(side_effect=added.append)
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        async def _tenant_db(_tenant_id):
            yield db

        monkeypatch.setattr(disp, "get_tenant_db", lambda t: _tenant_db(t))
        monkeypatch.setattr(disp, "_spawn_background", lambda coro: coro.close())

        await disp.emit_webhook(TEST_TENANT, "test.ping", {"a": 1})

        assert len(added) == 1
        assert reveal_destination_url(added[0].destination_url_snapshot) == "https://receiver-A.example/hook"
        # The secret is still pinned alongside it (F-022-06 unchanged).
        assert added[0].signing_secret_snapshot == ep.signing_secret

    @pytest.mark.anyio
    async def test_bug_8557_row_without_a_snapshot_is_dead_lettered_never_guessed(
        self, monkeypatch,
    ):
        """A legacy row (enqueued before migration 0202) has no authoritative
        target. It must NOT fall back to the endpoint's live url — that
        fallback IS the defect. It is dead-lettered, unsent."""
        from shared.webhooks import dispatcher as disp

        ep = _make_endpoint(url="https://receiver-B.example/hook")
        delivery = _make_delivery(
            endpoint_id=ep.id, status="pending", destination_url_snapshot=None,
        )
        delivery.next_attempt_at = NOW

        id_result = MagicMock()
        id_result.scalars.return_value.all.return_value = [delivery.id]
        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = delivery

        calls = []

        async def _execute(stmt):
            calls.append(stmt)
            return id_result if len(calls) == 1 else row_result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)
        db.get = AsyncMock(return_value=ep)
        db.commit = AsyncMock()

        async def _tenant_db(_tenant_id):
            yield db

        posted: list = []

        async def fake_attempt(d, url_, body_, sig_):
            posted.append(url_)
            d.status = "delivered"

        monkeypatch.setattr(disp, "get_tenant_db", lambda t: _tenant_db(t))
        monkeypatch.setattr(disp, "attempt_delivery", fake_attempt)

        await disp.drain_pending_deliveries(TEST_TENANT)

        assert posted == [], "an incoherent row must never be transmitted"
        assert delivery.status == "dlq"
        assert delivery.next_attempt_at is None
        assert delivery.error_message.startswith("incoherent_delivery_row")

    @pytest.mark.anyio
    async def test_bug_8557_queued_dispatch_also_refuses_a_row_without_a_snapshot(
        self, monkeypatch,
    ):
        """Shared-primitive discipline: the immediate background dispatch is a
        second caller of ``rebuild_signed_body`` and must honour the same
        invariant as the drain sweep."""
        from shared.webhooks import dispatcher as disp

        ep = _make_endpoint(url="https://receiver-B.example/hook")
        delivery = _make_delivery(
            endpoint_id=ep.id, status="pending", destination_url_snapshot=None,
        )

        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = delivery

        db = AsyncMock()
        db.execute = AsyncMock(return_value=row_result)
        db.get = AsyncMock(return_value=ep)
        db.commit = AsyncMock()

        async def _tenant_db(_tenant_id):
            yield db

        posted: list = []

        async def fake_attempt(d, url_, body_, sig_):
            posted.append(url_)

        monkeypatch.setattr(disp, "get_tenant_db", lambda t: _tenant_db(t))
        monkeypatch.setattr(disp, "attempt_delivery", fake_attempt)

        await disp._dispatch_queued_deliveries(TEST_TENANT, [delivery.id])

        assert posted == []
        assert delivery.status == "dlq"
        assert delivery.error_message.startswith("incoherent_delivery_row")

    @pytest.mark.anyio
    async def test_bug_8557_test_delivery_pins_and_sends_to_the_snapshot(self):
        """``deliver_test_event`` is the third enqueue site."""
        from shared.webhooks import dispatcher as disp

        ep = _make_endpoint(url="https://receiver-A.example/hook")
        added: list = []
        db = AsyncMock()
        db.add = MagicMock(side_effect=added.append)
        db.flush = AsyncMock()

        posted: list = []

        async def fake_attempt(d, url_, body_, sig_):
            posted.append(url_)
            d.status = "delivered"

        with (
            patch.object(disp, "attempt_delivery", side_effect=fake_attempt),
            patch.object(disp, "_decrypt_secret", return_value="real-production-key-abc"),
        ):
            delivery = await disp.deliver_test_event(db, ep, {"m": 1})

        assert reveal_destination_url(delivery.destination_url_snapshot) == "https://receiver-A.example/hook"
        assert posted == ["https://receiver-A.example/hook"]

    @pytest.mark.asyncio
    async def test_bug_8557_dlq_retry_repins_the_destination(self, client):
        """A MANUAL retry is the operator explicitly choosing to send this row
        wherever the endpoint points now, so it re-pins — which is also the
        recovery path for a legacy row that dead-lettered as incoherent."""
        delivery = _make_delivery(status="dlq", destination_url_snapshot=None)
        delivery.next_attempt_at = None
        delivery.signing_secret_snapshot = b"snap"
        ep = _make_endpoint(endpoint_id=delivery.endpoint_id,
                            url="https://receiver-B.example/hook")
        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda cls, id: delivery if id == delivery.id else ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        seen = {}

        def fake_rebuild(endpoint, d):
            seen["snapshot"] = d.destination_url_snapshot
            return (reveal_destination_url(d.destination_url_snapshot), b"{}", "t=1,v1=abc")

        async def fake_attempt(d, url, body, sig):
            seen["sent_to"] = url
            d.status = "delivered"

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.rebuild_signed_body", side_effect=fake_rebuild),
            patch("src.api.webhooks.attempt_delivery", side_effect=fake_attempt),
        ):
            resp = await client.post(f"/api/v1/admin/webhooks/dlq/{delivery.id}/retry")

        assert resp.status_code == 200
        assert reveal_destination_url(delivery.destination_url_snapshot) == "https://receiver-B.example/hook"
        assert reveal_destination_url(seen["snapshot"]) == "https://receiver-B.example/hook"
        assert seen["sent_to"] == "https://receiver-B.example/hook"


# ---------------------------------------------------------------------------
# Bug-8556 — the rotation triggered by a URL change is no longer invisible
# ---------------------------------------------------------------------------

class TestBug8556RotationDisclosureOnUpdate:
    """Bug-8556: repointing an endpoint rotates its signing secret (Bug-8410
    parity). Until this fix the route returned ``WebhookResponse``, which
    carries no plaintext, so the admin saved an edit and every subsequent
    delivery was signed with a value no human had ever seen. The new receiver
    answers 401/403, ``_is_non_retryable`` treats that as terminal, and the
    endpoint was dead on its first event with the only signal a server log."""

    @pytest.mark.asyncio
    async def test_bug_8556_url_change_returns_the_new_plaintext_once(self, client):
        ep = _make_endpoint(url="https://receiver-A.example/hook")
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
        ):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{ep.id}",
                json={"url": "https://receiver-B.example/hook"},
            )

        assert resp.status_code == 200
        body = resp.json()
        secret = body.get("signing_secret")
        assert secret, "the rotated plaintext must be returned to the admin"
        # It is the value actually stored, not a decoy: the stored form is the
        # encrypted round-trip of what was returned.
        from shared.security.credential_crypto import decrypt_str
        assert decrypt_str(ep.signing_secret) == secret

    @pytest.mark.asyncio
    async def test_bug_8556_unrelated_edit_returns_no_plaintext(self, client):
        """No rotation, no dialog: the SPA keys the one-time secret dialog off
        this field, so a name-only edit must not open it."""
        ep = _make_endpoint(url="https://receiver-A.example/hook")
        db = make_mock_db()
        db.get = AsyncMock(return_value=ep)
        db.refresh = AsyncMock(side_effect=lambda obj: None)

        with (
            patch("src.api.webhooks.get_tenant_db", async_gen_from(db)),
            patch("src.api.webhooks.audit_required", new_callable=AsyncMock),
        ):
            resp = await client.put(
                f"/api/v1/admin/webhooks/{ep.id}", json={"name": "renamed"},
            )

        assert resp.status_code == 200
        assert resp.json().get("signing_secret") is None
