"""Regression tests for Bug-8349 and Bug-8350 (agent-service webhook lane).

  - Bug-8349 [HIGH]: the outbound webhook dispatcher must never transmit an
    unsigned payload (it used to compute an empty signature header and POST
    anyway, recording a 2xx as "delivered" with no DLQ row -- the exact
    fail-open class already fixed once for the platform-wide dispatcher,
    Bug-8056). A signing secret must also be auto-generated the first time a
    webhook URL is configured, mirroring model-service's ``create_webhook``.
    A manual DLQ retry must resolve the row only AFTER a confirmed signed
    2xx, never before the retry attempt.
  - Bug-8350 [MEDIUM]: the webhook DLQ must never persist or return the raw
    destination URL (it commonly embeds a bearer token / API key); only a
    sanitised host/path hint and a one-way hash.

See test_bug_5951_5952_5956_5957.py::TestWebhookUnsignedRefusedNotSent for
the core "never POST unsigned" regression tests (those replace the two
tests that used to assert the old fail-open contract).
"""
from __future__ import annotations

import asyncio
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch as _patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_bug_5951_5952_5956_5957._mock_tenant_db_factory)
# ---------------------------------------------------------------------------


def _make_webhook_cfg(url="https://receiver.example/hooks/bearer-tok-123?key=shh"):
    return types.SimpleNamespace(
        enabled=True,
        webhook_url=url,
        webhook_signing_secret=b"encrypted-bytes",
    )


def _mock_tenant_db_factory(cfg, dlq_sink: list | None = None, existing_rows: list | None = None):
    """Async-generator factory mimicking get_tenant_db, with a DLQ sink and
    an optional list of pre-existing DLQ rows resolvable via db.get."""
    existing = list(existing_rows or [])

    async def _gen(tenant_id):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = cfg
        db.execute = AsyncMock(return_value=result)
        if dlq_sink is not None:
            db.add = lambda row: dlq_sink.append(row)
            db.commit = AsyncMock()

            async def _get(model, row_id):
                for row in list(existing) + dlq_sink:
                    if getattr(row, "id", None) == row_id:
                        return row
                return None

            db.get = AsyncMock(side_effect=_get)
        yield db

    return _gen


# ---------------------------------------------------------------------------
# Bug-8349(b) -- auto-generate a signing secret the first time webhook_url
# is set (mirrors model-service's create_webhook)
# ---------------------------------------------------------------------------


class TestWebhookSecretLifecycle:
    """Bug-8349 (auto-generate on first configuration) + Bug-8410 (rotate when
    the receiver changes). One function owns both, because they are the same
    rule: a signing secret belongs to the receiver it was issued for."""

    def _record(self, *, webhook_url=None, webhook_signing_secret=None):
        return types.SimpleNamespace(
            project_id=uuid.uuid4(),
            webhook_url=webhook_url,
            webhook_signing_secret=webhook_signing_secret,
        )

    def test_generates_secret_when_url_first_set(self):
        from src.api.agent_config import _apply_webhook_secret_lifecycle

        record = self._record(webhook_url="https://hooks.example.com/agent")
        with _patch(
            "src.api.agent_config.generate_signing_secret",
            return_value=("plaintext-secret", b"encrypted-bytes"),
        ) as gen_mock:
            _apply_webhook_secret_lifecycle(record, previous_url=None)

        gen_mock.assert_called_once()
        assert record.webhook_signing_secret == b"encrypted-bytes"

    def test_does_not_overwrite_existing_secret_on_an_unrelated_edit(self):
        """An existing secret must not be replaced when the RECEIVER has not
        changed -- a modeller editing display_name never has the credential
        they already shared swapped out from under them."""
        from src.api.agent_config import _apply_webhook_secret_lifecycle

        record = self._record(
            webhook_url="https://hooks.example.com/agent",
            webhook_signing_secret=b"already-set",
        )
        with _patch("src.api.agent_config.generate_signing_secret") as gen_mock:
            _apply_webhook_secret_lifecycle(
                record, previous_url="https://hooks.example.com/agent",
            )

        gen_mock.assert_not_called()
        assert record.webhook_signing_secret == b"already-set"

    def test_no_op_when_url_blank_or_none(self):
        from src.api.agent_config import _apply_webhook_secret_lifecycle

        for url in (None, "", "   "):
            record = self._record(webhook_url=url)
            with _patch("src.api.agent_config.generate_signing_secret") as gen_mock:
                _apply_webhook_secret_lifecycle(record, previous_url=None)
            gen_mock.assert_not_called()
            assert record.webhook_signing_secret is None

    @pytest.mark.parametrize(
        "previous_url,new_url,why",
        [
            (
                "https://receiver-a.example/hooks/tok-A",
                "https://receiver-b.example/hooks/tok-B",
                "a different host is plainly a different receiver",
            ),
            (
                "https://receiver.example/hooks/v1/tok-A",
                "https://receiver.example/hooks/v2/tok-B",
                "same host, different path -- the path carries the bearer "
                "material (Bug-8350's repro), so this is a different consumer",
            ),
            (
                "https://receiver.example/hooks?key=old",
                "https://receiver.example/hooks?key=new",
                "same host and path, different query credential",
            ),
            (
                "",
                "https://receiver-b.example/hooks/tok-B",
                "the reported repro: set A, CLEAR, then set B -- clearing "
                "must not launder the old secret onto the new receiver",
            ),
        ],
    )
    def test_rotates_when_the_receiver_changes(self, previous_url, new_url, why):
        """Bug-8410 -- the secret generated for receiver A must not be the one
        still verifying payloads sent to receiver B. A's operator, who has that
        secret written down, could otherwise forge events B accepts."""
        from src.api.agent_config import _apply_webhook_secret_lifecycle

        record = self._record(
            webhook_url=new_url, webhook_signing_secret=b"secret-for-A",
        )
        with _patch(
            "src.api.agent_config.generate_signing_secret",
            return_value=("plaintext-B", b"secret-for-B"),
        ) as gen_mock:
            _apply_webhook_secret_lifecycle(record, previous_url=previous_url)

        gen_mock.assert_called_once()
        assert record.webhook_signing_secret == b"secret-for-B", why

    @pytest.mark.asyncio
    async def test_patch_config_rotates_the_secret_when_the_url_changes(self):
        """End-to-end through the PATCH handler -- the reported repro drove
        the API, not the helper, and the helper is where the previous URL has
        to be captured BEFORE the patch overwrites it."""
        from src.api import agent_config

        project_id = uuid.uuid4()
        record = types.SimpleNamespace(
            id=uuid.uuid4(), project_id=project_id,
            webhook_url="https://receiver-a.example/hooks/tok-A",
            webhook_signing_secret=b"secret-for-A",
            enabled=False,
        )
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        cfg_result = MagicMock()
        cfg_result.scalar_one_or_none.return_value = record
        db.execute = AsyncMock(return_value=cfg_result)

        async def _db_gen(tenant_id):
            yield db

        current_user = types.SimpleNamespace(
            user_id="modeler@example.com", tenant_id="acme", role="modeler",
        )
        body = agent_config.AgentConfigPatch(
            webhook_url="https://receiver-b.example/hooks/tok-B",
        )

        with (
            _patch.object(agent_config, "get_tenant_db", _db_gen),
            _patch.object(agent_config, "_require_project_modeller", AsyncMock()),
            _patch.object(
                agent_config, "generate_signing_secret",
                return_value=("plaintext-B", b"secret-for-B"),
            ),
        ):
            await agent_config.patch_agent_config(project_id, body, current_user)

        assert record.webhook_signing_secret == b"secret-for-B", (
            "the webhook receiver was changed to a different host but the "
            "signing secret issued for the OLD receiver is still in place "
            "(Bug-8410)"
        )

    @pytest.mark.asyncio
    async def test_patch_config_auto_generates_secret_on_first_webhook_url(self):
        """End-to-end through the PATCH handler: setting webhook_url on a
        record with no secret yet must leave webhook_signing_secret non-NULL
        after the call, without requiring a separate rotate-secret call."""
        from src.api import agent_config

        project_id = uuid.uuid4()
        record = types.SimpleNamespace(
            id=uuid.uuid4(), project_id=project_id,
            webhook_url=None, webhook_signing_secret=None,
            enabled=False,
        )
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        cfg_result = MagicMock()
        cfg_result.scalar_one_or_none.return_value = record
        db.execute = AsyncMock(return_value=cfg_result)

        async def _db_gen(tenant_id):
            yield db

        current_user = types.SimpleNamespace(
            user_id="modeler@example.com", tenant_id="acme", role="modeler",
        )
        body = agent_config.AgentConfigPatch(webhook_url="https://hooks.example.com/agent")

        with (
            _patch.object(agent_config, "get_tenant_db", _db_gen),
            _patch.object(
                agent_config, "_require_project_modeller", AsyncMock(),
            ),
            _patch.object(
                agent_config, "generate_signing_secret",
                return_value=("plaintext", b"encrypted-once"),
            ),
        ):
            await agent_config.patch_agent_config(project_id, body, current_user)

        assert record.webhook_signing_secret == b"encrypted-once", (
            "webhook_url was set for the first time but no signing secret "
            "was auto-generated -- every subsequent event would dispatch "
            "unsigned by default"
        )


# ---------------------------------------------------------------------------
# Bug-8349(d) -- manual DLQ retry resolves only after a confirmed signed 2xx
# ---------------------------------------------------------------------------


class TestManualRetryResolvesOnlyAfterSuccess:
    @pytest.mark.asyncio
    async def test_successful_retry_resolves_the_row(self):
        from src.webhooks import dispatcher

        cfg = _make_webhook_cfg()
        existing_row = types.SimpleNamespace(
            id=uuid.uuid4(), resolved_at=None,
        )
        dlq_sink: list = []

        async def fake_post_once(tenant_id, url, body, sig_header, event_type):
            return True, 200, None

        with (
            _patch.object(
                dispatcher, "get_tenant_db",
                _mock_tenant_db_factory(cfg, dlq_sink, existing_rows=[existing_row]),
            ),
            _patch.object(dispatcher, "_decrypt_secret", return_value="a-real-strong-secret"),
            _patch.object(dispatcher, "_post_once", side_effect=fake_post_once),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
                source_dlq_id=existing_row.id,
            )

        assert existing_row.resolved_at is not None, (
            "a retry that succeeded (signed 2xx) must resolve the DLQ row"
        )
        assert dlq_sink == [], "a successful retry must not create a new DLQ row"

    @pytest.mark.asyncio
    async def test_failed_retry_does_not_resolve_and_updates_row_in_place(self):
        from src.webhooks import dispatcher

        cfg = _make_webhook_cfg()
        existing_row = types.SimpleNamespace(
            id=uuid.uuid4(), resolved_at=None, attempt_count=3, last_error="HTTP 500",
        )
        dlq_sink: list = []

        async def fake_post_once(tenant_id, url, body, sig_header, event_type):
            return False, 500, "HTTP 500"

        with (
            _patch.object(
                dispatcher, "get_tenant_db",
                _mock_tenant_db_factory(cfg, dlq_sink, existing_rows=[existing_row]),
            ),
            _patch.object(dispatcher, "_decrypt_secret", return_value="a-real-strong-secret"),
            _patch.object(dispatcher, "_post_once", side_effect=fake_post_once),
            _patch.object(dispatcher.asyncio, "sleep", AsyncMock()),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
                source_dlq_id=existing_row.id,
            )

        assert existing_row.resolved_at is None, (
            "a retry that failed again must NOT be marked resolved"
        )
        assert dlq_sink == [], (
            "a retry of an existing DLQ row must update it in place, not "
            "insert a duplicate row"
        )
        assert existing_row.attempt_count == 4  # one more attempt was recorded

    @pytest.mark.asyncio
    async def test_retry_endpoint_does_not_resolve_before_dispatch(self):
        """Bug-8349 -- the retry_dlq handler itself must not touch
        resolved_at; resolution is dispatch_event's responsibility, and only
        after success. Regression guard for the old
        'row.resolved_at = now(); commit(); THEN dispatch' ordering bug."""
        from src.api import webhooks as agent_webhooks

        project_id = uuid.uuid4()
        dlq_id = uuid.uuid4()
        row = types.SimpleNamespace(
            id=dlq_id, project_id=project_id, resolved_at=None,
            event_type="turn.completed", payload={"payload": {"k": "v"}},
            conversation_id=None, turn_id=None,
        )
        db = AsyncMock()
        db.get = AsyncMock(return_value=row)

        async def _db_gen(tenant_id):
            yield db

        current_user = types.SimpleNamespace(
            user_id="modeler@example.com", tenant_id="acme", role="modeler",
        )

        captured_kwargs = {}

        def fake_spawn_background(coro):
            # Never actually schedule the coroutine (it's a real coroutine
            # object from dispatch_event); close it to avoid an
            # "unawaited coroutine" warning, and capture nothing further --
            # this test only asserts the row was not eagerly resolved and
            # that retry_dlq wired source_dlq_id.
            coro.close()
            return None

        with (
            # Bug-8356 — authorization moved from a per-handler
            # `_require_modeller` call to a router-level dependency, so
            # calling the handler function directly no longer runs (or
            # needs to stub) any gate. HTTP-boundary gating is covered by
            # tests/test_bug_8356_webhook_project_idor.py.
            _patch.object(agent_webhooks, "get_tenant_db", _db_gen),
        ):
            import src.api.conversations as conversations_mod
            with _patch.object(
                conversations_mod, "_spawn_background", side_effect=fake_spawn_background,
            ) as spawn_mock:
                await agent_webhooks.retry_dlq(project_id, dlq_id, current_user)

        assert row.resolved_at is None, (
            "retry_dlq must not mark the row resolved before the retry runs"
        )
        spawn_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_rejects_already_resolved_row(self):
        """Reviewer follow-up (round 1) -- retrying an already-resolved DLQ
        row must not silently spawn a phantom resend; it is rejected with
        409. This does not close the full concurrent-double-click race
        (tracked separately, LOW severity) but does close the simpler,
        decidable-up-front case of a row already known resolved."""
        from fastapi import HTTPException

        from src.api import webhooks as agent_webhooks

        project_id = uuid.uuid4()
        dlq_id = uuid.uuid4()
        row = types.SimpleNamespace(
            id=dlq_id, project_id=project_id,
            resolved_at=types.SimpleNamespace(),  # any non-None sentinel
        )
        db = AsyncMock()
        db.get = AsyncMock(return_value=row)

        async def _db_gen(tenant_id):
            yield db

        current_user = types.SimpleNamespace(
            user_id="modeler@example.com", tenant_id="acme", role="modeler",
        )

        with (
            # Bug-8356 — authorization moved from a per-handler
            # `_require_modeller` call to a router-level dependency, so
            # calling the handler function directly no longer runs (or
            # needs to stub) any gate. HTTP-boundary gating is covered by
            # tests/test_bug_8356_webhook_project_idor.py.
            _patch.object(agent_webhooks, "get_tenant_db", _db_gen),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await agent_webhooks.retry_dlq(project_id, dlq_id, current_user)

        assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Bug-8350 -- DLQ never persists or returns the raw destination URL
# ---------------------------------------------------------------------------


class TestWebhookDlqUrlRedaction:
    def test_redact_url_for_display_strips_credentials_path_and_query(self):
        """Bug-8350's own repro embeds the secret in the PATH
        (``/hooks/<bearer-token>``), not only the query string -- the hint
        must drop the path entirely, not just userinfo/query/fragment."""
        from shared.webhooks.redact import redact_url_for_display

        url = "https://user:pass@receiver.example:8443/hooks/bearer-tok-123?key=shh#frag"
        redacted = redact_url_for_display(url)

        assert "pass" not in redacted
        assert "shh" not in redacted
        assert "bearer-tok-123" not in redacted
        # Host + port are still useful for an operator to identify which
        # receiver failed.
        assert "receiver.example" in redacted
        assert "8443" in redacted

    def test_redact_url_for_display_tolerates_malformed_port(self):
        """Bug-8349 R2 HIGH — ``parsed.port`` raises ``ValueError`` for a
        syntactically invalid port (e.g. ``https://host:notaport/hook``).
        Before this fix that propagated straight out of
        ``redact_url_for_display``, which ``dispatcher._persist_dlq`` calls
        UNCONDITIONALLY on every DLQ write — including the branch that
        exists specifically to record a URL ``validate_webhook_url`` just
        rejected. A legacy row with a malformed-port URL (stored before the
        port check existed) would crash the very code path meant to record
        its own rejection, losing the event with zero DLQ record anyway."""
        from shared.webhooks.redact import redact_url_for_display

        result = redact_url_for_display("https://receiver.example:notaport/hooks/x")
        assert result == "<unparseable-url>"

    def test_no_url_hash_helper_survives(self):
        """Bug-8407 — ``hash_url_secure`` / ``verify_url_hash`` and the
        ``target_url_hash`` column are GONE, and must not come back without a
        reader.

        The three tests that stood here asserted the hash was bcrypt-backed
        rather than a fast SHA-256 digest. That was the right fix for HALF the
        finding: it closed the offline-brute-force path (the gate measured
        42,001 candidates in 0.07s against the original digest). The other
        half never moved — nothing in the codebase ever READ the column, and
        salted bcrypt cannot correlate two rows, so it could not serve the
        "dedup/correlation" purpose its own docstring claimed. A persisted,
        per-row derivative of a secret-bearing URL with no consumer is a
        liability in a stolen backup no matter how slow the hash is, and it
        cost a ~0.3s bcrypt call on every DLQ write. Migration 0188 drops it.
        """
        import shared.webhooks.redact as redact

        assert not hasattr(redact, "hash_url_secure")
        assert not hasattr(redact, "verify_url_hash")

    def test_scrub_url_from_text_removes_known_and_bare_urls(self):
        from shared.webhooks.redact import scrub_url_from_text

        url = "https://receiver.example/hooks/bearer-tok-123?key=shh"
        text = f"Connection to {url} failed: refused"
        scrubbed = scrub_url_from_text(text, url)
        assert "bearer-tok-123" not in scrubbed
        assert "shh" not in scrubbed

        # Even without the exact url passed, a bare http(s) substring is
        # swept -- covers an HTTP client echoing a differently-formatted URL.
        assert "shh" not in scrub_url_from_text(text)

    @pytest.mark.asyncio
    async def test_persist_dlq_never_stores_raw_url_or_leaks_it_in_last_error(self):
        from src.webhooks import dispatcher

        cfg = _make_webhook_cfg(
            url="https://receiver.example/hooks/bearer-tok-123?key=shh",
        )
        dlq_sink: list = []

        async def fake_post_once(tenant_id, url, body, sig_header, event_type):
            # Simulate an HTTP client exception message echoing the request URL.
            return False, None, f"ConnectError: could not reach {url}"

        with (
            _patch.object(
                dispatcher, "get_tenant_db", _mock_tenant_db_factory(cfg, dlq_sink),
            ),
            _patch.object(dispatcher, "_decrypt_secret", return_value="a-real-strong-secret"),
            _patch.object(dispatcher, "_post_once", side_effect=fake_post_once),
            _patch.object(dispatcher.asyncio, "sleep", AsyncMock()),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
            )

        assert len(dlq_sink) == 1
        row = dlq_sink[0]
        assert not hasattr(row, "target_url"), (
            "AgentWebhookDlq must not carry a target_url attribute at all"
        )
        assert row.target_host == "https://receiver.example"
        assert "bearer-tok-123" not in (row.last_error or "")
        assert "shh" not in (row.last_error or "")
        # Bug-8407 — no fingerprint of the URL is persisted at all any more.
        assert not hasattr(row, "target_url_hash"), (
            "the write-only target_url_hash was dropped (migration 0188); "
            "re-adding a hash of a secret-bearing URL needs a real reader"
        )

    def test_dlq_row_response_schema_has_no_target_url_field(self):
        """Producer/consumer alignment: the API response model must not
        expose target_url even if a future edit re-adds it to the ORM."""
        from src.api.webhooks import DlqRow

        assert "target_url" not in DlqRow.model_fields
        assert "target_host" in DlqRow.model_fields

    def test_agent_webhook_dlq_model_has_no_target_url_column(self):
        from shared.db.models import AgentWebhookDlq

        assert "target_url" not in AgentWebhookDlq.__table__.columns
        assert "target_host" in AgentWebhookDlq.__table__.columns
        # Bug-8407 — the write-only fingerprint column is gone.
        assert "target_url_hash" not in AgentWebhookDlq.__table__.columns


# ---------------------------------------------------------------------------
# Bug-8349 R2 [HIGH] -- dispatch_event must never lose an event with zero
# DLQ record on a transport-level failure. These tests drive the REAL
# httpx/httpcore transport stack -- ``_post_once`` is never mocked -- unlike
# every prior test in this module (and the ones that shipped in R1), which
# is exactly why this escaped 2 Claude-family review rounds: they all mocked
# ``_post_once`` directly and never exercised the real transport. Only the
# OS-level DNS syscall is mocked for determinism (the same pattern
# services/scheduler/tests/test_ssrf_async_dns.py already uses for
# offline-safe DNS-failure tests) -- the SSRF-safe transport, the httpcore
# connection pool, and httpx's own URL parsing are all real.
# ---------------------------------------------------------------------------


class TestRealTransportFailureModesNeverLoseEvent:
    @pytest.mark.asyncio
    async def test_post_once_real_transport_dns_failure_returns_failure_tuple(self):
        """``shared/webhooks/ssrf.py``'s ``_SSRFSafeTransport`` calls the raw
        httpcore connection pool directly, with none of httpx's usual
        exception-mapping wrapper -- a bare ``httpcore.ConnectError`` (real
        DNS failure) is NOT an ``httpx.HTTPError`` subclass and used to
        propagate straight out of ``_post_once``, uncaught."""
        import asyncio as _asyncio
        import socket

        from src.webhooks import dispatcher

        with _patch.object(
            _asyncio.get_running_loop(), "getaddrinfo",
            new_callable=AsyncMock,
            side_effect=socket.gaierror("Name resolution failed"),
        ):
            ok, status, error = await dispatcher._post_once(
                "acme",
                "https://nonexistent-domain-for-bug-8349-test.invalid/hook",
                b'{"k":"v"}', "", "turn.completed",
            )

        assert ok is False
        assert status is None
        assert error is not None  # a failure tuple, never a raised exception

    @pytest.mark.asyncio
    async def test_post_once_real_transport_malformed_port_returns_failure_tuple(self):
        """``httpx.InvalidURL`` (malformed port) is raised by httpx's own URL
        parsing before the transport is even reached, and is confirmed NOT a
        subclass of ``httpx.HTTPError`` -- same escape, different cause."""
        import httpx

        from src.webhooks import dispatcher

        # Pin the premise this test protects against regressing: if a
        # future httpx release ever makes InvalidURL an HTTPError subclass,
        # the narrow `except httpx.HTTPError` alone would be sufficient
        # again -- but we must not rely on that.
        assert not issubclass(httpx.InvalidURL, httpx.HTTPError)

        ok, status, error = await dispatcher._post_once(
            "acme",
            "https://receiver.example:notaport/hook",
            b'{"k":"v"}', "", "turn.completed",
        )
        assert ok is False
        assert status is None
        assert error is not None

    @pytest.mark.asyncio
    async def test_dispatch_event_real_dns_failure_writes_dlq_row_not_mocked_transport(self):
        """End-to-end: the DB layer is stubbed (no live Postgres needed for
        this unit test) but ``_post_once``/httpx/the SSRF transport are all
        REAL. A real DNS failure must still resolve to exactly one DLQ row,
        not a lost event and not an unhandled exception escaping the
        fire-and-forget task."""
        import asyncio as _asyncio
        import socket

        from src.webhooks import dispatcher

        cfg = _make_webhook_cfg(
            url="https://nonexistent-domain-for-bug-8349-test.invalid/hook",
        )
        dlq_sink: list = []

        with (
            _patch.object(
                dispatcher, "get_tenant_db", _mock_tenant_db_factory(cfg, dlq_sink),
            ),
            _patch.object(dispatcher, "_decrypt_secret", return_value="a-real-strong-secret"),
            _patch.object(dispatcher.asyncio, "sleep", AsyncMock()),
            _patch.object(
                _asyncio.get_running_loop(), "getaddrinfo",
                new_callable=AsyncMock,
                side_effect=socket.gaierror("Name resolution failed"),
            ),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
            )

        assert len(dlq_sink) == 1, (
            "a real DNS failure through the real transport must still "
            "produce exactly one DLQ row -- zero DLQ rows here means the "
            "event vanished, which is the exact HIGH finding"
        )
        row = dlq_sink[0]
        assert row.attempt_count >= 1
        assert row.last_error

    @pytest.mark.asyncio
    async def test_dispatch_event_malformed_port_rejected_preflight_writes_dlq_row(self):
        """A malformed-port webhook_url (e.g. a legacy row stored before
        ``validate_webhook_url``'s port check existed) must be rejected at
        the SSRF pre-flight step -- never reaching ``_post_once``/the
        transport at all -- and still successfully write a DLQ row rather
        than crashing inside ``_persist_dlq``'s ``redact_url_for_display``
        call (which also used to raise on a malformed port)."""
        from src.webhooks import dispatcher

        cfg = _make_webhook_cfg(url="https://receiver.example:notaport/hook")
        dlq_sink: list = []
        post_once_calls: list = []

        async def _spy_post_once(*args, **kwargs):
            post_once_calls.append(args)
            return True, 200, None

        with (
            _patch.object(
                dispatcher, "get_tenant_db", _mock_tenant_db_factory(cfg, dlq_sink),
            ),
            _patch.object(dispatcher, "_decrypt_secret", return_value="a-real-strong-secret"),
            _patch.object(dispatcher, "_post_once", side_effect=_spy_post_once),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
            )

        assert post_once_calls == [], (
            "a malformed-port URL must be rejected pre-flight by "
            "validate_webhook_url, never reaching the transport"
        )
        assert len(dlq_sink) == 1
        row = dlq_sink[0]
        assert "invalid port" in (row.last_error or "").lower()


# ---------------------------------------------------------------------------
# Bug-8349 R2 [HIGH], defense in depth -- the fire-and-forget spawner's
# done-callback must retrieve and log a background task's exception instead
# of silently discarding it.
# ---------------------------------------------------------------------------


class TestSpawnBackgroundLogsUnhandledExceptions:
    @pytest.mark.asyncio
    async def test_on_done_logs_task_exception(self):
        from src.api import conversations

        async def _boom():
            raise RuntimeError("simulated unexpected failure")

        with _patch.object(conversations, "logger") as mock_logger:
            task = conversations._spawn_background(_boom())
            with pytest.raises(RuntimeError):
                await task

        mock_logger.error.assert_called_once()
        _, kwargs = mock_logger.error.call_args
        assert isinstance(kwargs.get("exc_info"), RuntimeError)

    @pytest.mark.asyncio
    async def test_on_done_does_not_log_for_a_clean_success(self):
        from src.api import conversations

        async def _fine():
            return "ok"

        with _patch.object(conversations, "logger") as mock_logger:
            task = conversations._spawn_background(_fine())
            result = await task

        assert result == "ok"
        mock_logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_done_does_not_raise_for_a_cancelled_task(self):
        """``Task.exception()`` raises CancelledError on a cancelled task;
        the done-callback must guard with ``cancelled()`` first."""
        from src.api import conversations

        async def _slow():
            await asyncio.sleep(10)

        with _patch.object(conversations, "logger") as mock_logger:
            task = conversations._spawn_background(_slow())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        mock_logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# Bug-8350 R2 [MED-2] -- GET /dlq read-time redaction backstop.
# ---------------------------------------------------------------------------


class TestDlqReadTimeRedactionBackstop:
    @pytest.mark.asyncio
    async def test_list_dlq_scrubs_a_url_in_last_error_even_if_write_path_missed_it(self):
        """Defense in depth: simulate a row that predates the write-time
        scrub (or a future write-path regression) still carrying a raw URL
        in last_error -- GET /dlq must never return it verbatim."""
        from src.api import webhooks as agent_webhooks

        project_id = uuid.uuid4()
        leaked_row = types.SimpleNamespace(
            id=uuid.uuid4(),
            event_type="turn.completed",
            target_host="https://receiver.example",
            attempt_count=4,
            last_status_code=None,
            last_error="ConnectError: could not reach https://receiver.example/hooks/leaked-token?key=shh",
            first_attempted_at=None,
            last_attempted_at=None,
            resolved_at=None,
            payload={},
        )

        # Bug-8357 -- list_dlq now issues TWO selects: the project's
        # configured webhook_url (so the backstop can also strip a scheme-less
        # echo of the credential-bearing path) and then the DLQ rows. A single
        # return_value would hand the URL query the row result and vice versa.
        cfg_result = MagicMock()
        cfg_result.scalar_one_or_none.return_value = (
            "https://receiver.example/hooks/leaked-token?key=shh"
        )
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [leaked_row]
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[cfg_result, rows_result])

        async def _db_gen(tenant_id):
            yield db

        current_user = types.SimpleNamespace(
            user_id="modeler@example.com", tenant_id="acme", role="modeler",
        )

        with (
            # Bug-8356 — authorization moved from a per-handler
            # `_require_modeller` call to a router-level dependency, so
            # calling the handler function directly no longer runs (or
            # needs to stub) any gate. HTTP-boundary gating is covered by
            # tests/test_bug_8356_webhook_project_idor.py.
            _patch.object(agent_webhooks, "get_tenant_db", _db_gen),
        ):
            rows = await agent_webhooks.list_dlq(project_id, current_user)

        assert len(rows) == 1
        assert "leaked-token" not in (rows[0].last_error or "")
        assert "shh" not in (rows[0].last_error or "")

    @pytest.mark.asyncio
    async def test_list_dlq_scrubs_a_scheme_less_path_echo(self):
        """Bug-8357 -- a receiver that echoed only the credential-bearing PATH
        (``/hooks/<token>``) leaks the same secret with no scheme for a pattern
        to anchor on. The backstop therefore loads the project's configured
        URL; without it this row would be returned verbatim."""
        from src.api import webhooks as agent_webhooks

        project_id = uuid.uuid4()
        leaked_row = types.SimpleNamespace(
            id=uuid.uuid4(),
            event_type="turn.completed",
            target_host="https://receiver.example",
            attempt_count=4,
            last_status_code=404,
            last_error="HTTP 404: no route for /hooks/leaked-token?key=shh",
            first_attempted_at=None,
            last_attempted_at=None,
            resolved_at=None,
            payload={},
        )
        cfg_result = MagicMock()
        cfg_result.scalar_one_or_none.return_value = (
            "https://receiver.example/hooks/leaked-token?key=shh"
        )
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [leaked_row]
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[cfg_result, rows_result])

        async def _db_gen(tenant_id):
            yield db

        current_user = types.SimpleNamespace(
            user_id="modeler@example.com", tenant_id="acme", role="modeler",
        )
        with _patch.object(agent_webhooks, "get_tenant_db", _db_gen):
            rows = await agent_webhooks.list_dlq(project_id, current_user)

        assert len(rows) == 1
        assert "leaked-token" not in (rows[0].last_error or "")
        assert "shh" not in (rows[0].last_error or "")


# ---------------------------------------------------------------------------
# Bug-7334 class (Bug-8349 R2 gate residual, found by the fresh-reviewer
# pass on this lane) -- _post_once used client.post(), which materialises
# the ENTIRE response body before returning even though this function only
# ever reads the status code. A hostile or misbehaving receiver answering
# with a multi-GB error body would exhaust the shared agent-service
# process's memory -- the exact class already fixed once for the
# platform-wide sibling dispatcher (shared/webhooks/dispatcher.py), which
# this fix now mirrors via client.stream() + a 8 KB read cap.
# ---------------------------------------------------------------------------


class TestPostOnceDoesNotDownloadUnboundedResponseBody:
    @pytest.mark.asyncio
    async def test_hostile_receiver_cannot_stream_an_unbounded_error_body(self):
        """Real socket server, real httpx/httpcore transport (SSRF loopback
        check stubbed only so a 127.0.0.1 receiver is reachable in this
        test -- SSRF blocking itself is covered by
        tests/unit/test_ssrf_defence_depth.py). Asserts the dispatcher never
        reads more than a few KB of a receiver that offers to send 64 MB."""
        import socket
        import threading
        import types as _types

        import shared.webhooks.ssrf as ssrf
        from src.webhooks import dispatcher

        class _AlwaysGlobal:
            is_global = True
            is_multicast = False

        sent = {"bytes": 0}
        total_mb = 64
        chunk = b"A" * 65536

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def _serve() -> None:
            conn, _ = srv.accept()
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 500 Internal Server Error\r\n"
                b"Content-Length: %d\r\n\r\n" % (total_mb * 1024 * 1024)
            )
            try:
                for _ in range(total_mb * 16):
                    conn.sendall(chunk)
                    sent["bytes"] += len(chunk)
            except OSError:
                pass  # client capped the read and closed -- the expected path
            finally:
                conn.close()

        threading.Thread(target=_serve, daemon=True).start()

        with _patch.object(
            ssrf, "ipaddress",
            _types.SimpleNamespace(ip_address=lambda _s: _AlwaysGlobal()),
        ):
            ok, status, error = await dispatcher._post_once(
                "acme", f"http://127.0.0.1:{port}/hook", b'{"k":"v"}', "",
                "turn.completed",
            )

        assert ok is False
        assert status == 500
        assert sent["bytes"] < 4 * 1024 * 1024, (
            f"the dispatcher let the server push {sent['bytes']} bytes of a "
            f"body it never even reads -- a multi-GB body would OOM the "
            f"shared agent-service process (Bug-7334 class)"
        )


class TestTransportExceptionMessageIsScrubbedInBothChannels:
    """Bug-8430 residual, R2 reviewer finding 5 — the generic
    ``except Exception`` branch in ``_post_once``.

    R3 test escape: the R2 fix that scrubbed this log shipped with NO guard.
    The shared sibling's equivalent branch IS pinned
    (``tests/unit/test_webhook_failopen_signing.py::
    TestPlatformDispatcherAttemptDeadline::
    test_a_transport_exception_message_is_scrubbed``), and the Bug-8430
    closeout cites that test as the guard for a fix it says landed in BOTH
    dispatchers — so the agent-service half was covered only by the closeout
    sentence. Deleting ``scrub_url_from_text`` from the log call turns this
    red. Tier: T2 (secret-leak regression).
    """

    @pytest.mark.asyncio
    async def test_the_transport_error_log_does_not_quote_the_url(self, caplog):
        from src.webhooks import dispatcher

        url = "https://receiver.example/hooks/bearer-tok-123?key=shh-shh-shh"

        async def _boom(*_a, **_kw):
            # An httpcore/httpx transport exception routinely quotes the
            # request URL verbatim in its own message; that message is what
            # the branch used to hand straight to the application log, which
            # is shipped, retained and searched far outside this process.
            raise RuntimeError(f"ConnectError: could not reach {url}")

        with (
            _patch.object(dispatcher, "_send_and_read_status", _boom),
            _patch.object(dispatcher, "_shared_client", object()),
            _patch.object(dispatcher, "_tenant_budgets", {}),
            caplog.at_level("WARNING", logger="src.webhooks.dispatcher"),
        ):
            ok, status, error = await dispatcher._post_once(
                "tenant-scrub", url, b"{}", "t=1,v1=abc", "test.ping",
            )

        assert ok is False and status is None
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "bearer-tok-123" not in logged, logged
        assert "shh-shh-shh" not in logged, logged
        assert "<redacted-url>" in logged, logged
        # The RETURN channel is asserted too, not just the log. Every consumer
        # of this tuple today scrubs before persisting (`_persist_dlq`), which
        # is why an unscrubbed return was never a live leak — but the shared
        # sibling scrubs it (`shared/webhooks/dispatcher.py`, the same generic
        # branch), and a future consumer must not inherit a raw credential
        # because the two drifted. R3 FIND-3.
        #
        # This assertion is load-bearing: the first draft of this test only
        # checked `error is not None`, so reverting the return-value scrub left
        # it GREEN — a guard written for a fix it could not actually observe.
        assert error is not None
        assert "bearer-tok-123" not in error, error
        assert "shh-shh-shh" not in error, error
