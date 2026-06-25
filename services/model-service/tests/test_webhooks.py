"""Tests for the webhook / event notification system (Phase 3, Block C)."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from sqlalchemy.dialects import postgresql

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

    @pytest.mark.asyncio
    async def test_dispatch_event_pending_on_first_failure(self):
        """F-022-06: a single retryable failure leaves the delivery ``pending``
        with ``next_attempt_at`` set (the scheduler drain job retries it
        asynchronously) and makes exactly one POST — no inline backoff."""
        from shared.webhooks import dispatcher as disp

        ep = _make_endpoint()
        db = make_mock_db()

        ep_result = MagicMock()
        ep_result.scalars.return_value.all.return_value = [ep]
        db.execute = AsyncMock(return_value=ep_result)

        with (
            patch("shared.webhooks.dispatcher.get_tenant_db", async_gen_from(db)),
            patch("shared.webhooks.dispatcher._decrypt_secret", return_value="secret"),
            patch("shared.webhooks.dispatcher._post_once", new_callable=AsyncMock) as mock_post,
        ):
            mock_post.return_value = (False, 500, "Server Error")

            await disp.dispatch_event(TEST_TENANT, "model.published", {"model_id": "abc"})

            assert db.add.call_count >= 1
            delivery = db.add.call_args_list[-1][0][0]
            assert delivery.status == "pending"
            assert delivery.attempts == 1
            assert delivery.next_attempt_at is not None
            assert delivery.event_type == "model.published"
            assert mock_post.await_count == 1  # one POST, no inline retries

    @pytest.mark.asyncio
    async def test_dispatch_event_dlq_on_non_retryable(self):
        """A 4xx (non-408/429) is non-retryable and goes straight to the DLQ."""
        from shared.webhooks import dispatcher as disp

        ep = _make_endpoint()
        db = make_mock_db()
        ep_result = MagicMock()
        ep_result.scalars.return_value.all.return_value = [ep]
        db.execute = AsyncMock(return_value=ep_result)

        with (
            patch("shared.webhooks.dispatcher.get_tenant_db", async_gen_from(db)),
            patch("shared.webhooks.dispatcher._decrypt_secret", return_value="secret"),
            patch("shared.webhooks.dispatcher._post_once", new_callable=AsyncMock) as mock_post,
        ):
            mock_post.return_value = (False, 404, "Not Found")
            await disp.dispatch_event(TEST_TENANT, "model.published", {"model_id": "abc"})
            delivery = db.add.call_args_list[-1][0][0]
            assert delivery.status == "dlq"
            assert delivery.next_attempt_at is None

    @pytest.mark.asyncio
    async def test_dispatch_event_dlq_on_unsafe_url(self):
        """F-022-05: a stored endpoint whose URL is SSRF-unsafe is never POSTed
        to — the delivery is DLQ'd before any network call."""
        from shared.webhooks import dispatcher as disp

        ep = _make_endpoint(url="http://169.254.169.254/latest/meta-data")
        db = make_mock_db()
        ep_result = MagicMock()
        ep_result.scalars.return_value.all.return_value = [ep]
        db.execute = AsyncMock(return_value=ep_result)

        with (
            patch("shared.webhooks.dispatcher.get_tenant_db", async_gen_from(db)),
            patch("shared.webhooks.dispatcher._post_once", new_callable=AsyncMock) as mock_post,
        ):
            await disp.dispatch_event(TEST_TENANT, "model.published", {"model_id": "abc"})
            delivery = db.add.call_args_list[-1][0][0]
            assert delivery.status == "dlq"
            assert "unsafe" in (delivery.error_message or "").lower()
            assert mock_post.await_count == 0  # never attempted

    @pytest.mark.asyncio
    async def test_dispatch_event_delivered_on_success(self):
        from shared.webhooks.dispatcher import dispatch_event

        ep = _make_endpoint()
        db = make_mock_db()

        ep_result = MagicMock()
        ep_result.scalars.return_value.all.return_value = [ep]
        db.execute = AsyncMock(return_value=ep_result)

        with (
            patch("shared.webhooks.dispatcher.get_tenant_db", async_gen_from(db)),
            patch("shared.webhooks.dispatcher._decrypt_secret", return_value="secret"),
            patch("shared.webhooks.dispatcher._post_once", new_callable=AsyncMock) as mock_post,
        ):
            mock_post.return_value = (True, 200, None)

            await dispatch_event(TEST_TENANT, "model.published", {"model_id": "abc"})

            added = db.add.call_args_list[-1][0][0]
            assert added.status == "delivered"
            assert added.attempts == 1

    @pytest.mark.asyncio
    async def test_dispatch_event_skips_non_matching_filter(self):
        from shared.webhooks.dispatcher import dispatch_event

        ep = _make_endpoint(event_filters=["user.created"])
        db = make_mock_db()

        ep_result = MagicMock()
        ep_result.scalars.return_value.all.return_value = [ep]
        db.execute = AsyncMock(return_value=ep_result)

        with (
            patch("shared.webhooks.dispatcher.get_tenant_db", async_gen_from(db)),
            patch("shared.webhooks.dispatcher._post_once", new_callable=AsyncMock) as mock_post,
        ):
            await dispatch_event(TEST_TENANT, "model.published", {"model_id": "abc"})
            mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_event_dlq_on_oversized_payload(self):
        from shared.webhooks.dispatcher import dispatch_event

        ep = _make_endpoint()
        db = make_mock_db()

        ep_result = MagicMock()
        ep_result.scalars.return_value.all.return_value = [ep]
        db.execute = AsyncMock(return_value=ep_result)

        big_payload = {"data": "x" * 100_000}

        with (
            patch("shared.webhooks.dispatcher.get_tenant_db", async_gen_from(db)),
            patch("shared.webhooks.dispatcher._decrypt_secret", return_value="secret"),
            patch("shared.webhooks.dispatcher._post_once", new_callable=AsyncMock) as mock_post,
        ):
            await dispatch_event(TEST_TENANT, "model.published", big_payload)
            mock_post.assert_not_called()
            added = db.add.call_args_list[-1][0][0]
            assert added.status == "dlq"
            assert "byte limit" in added.error_message


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
            patch("src.api.webhooks.audit", new_callable=AsyncMock),
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
        db.add.assert_called()

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
            patch("src.api.webhooks.audit", new_callable=AsyncMock),
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
            patch("src.api.webhooks.audit", new_callable=AsyncMock),
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
            patch("src.api.webhooks.audit", new_callable=AsyncMock),
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
            patch("src.api.webhooks.audit", new_callable=AsyncMock),
        ):
            resp = await client.post(f"/api/v1/admin/webhooks/{ep.id}/rotate-secret")
        assert resp.status_code == 200
        assert resp.json()["signing_secret"] == "new-secret-plain"


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
        db.get = AsyncMock(return_value=delivery)

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
