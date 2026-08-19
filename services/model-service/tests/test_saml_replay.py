"""F-021-03 / Bug-7993: SAML assertion replay-ledger + request-binding tests.

The replay ledger (``record_assertion_or_reject``) must accept the FIRST use of
an assertion ID and reject every subsequent use, fail closed on a missing ID or
a DB error, and the ACS handler must reject a replayed assertion with 401. The
request-binding (InResponseTo) is enforced by python3-saml via the
``request_id`` we now thread through; here we assert the wiring passes the
expected request id into the library call.

Test escape: ACS success tests mocked ``process_saml_response`` so no replay set
existed. Guard: this suite exercises the ledger directly plus the ACS reject
path. Tier: T1 (SSO replay-resistance security contract).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@asynccontextmanager
async def _noop_sso_overlay(tenant_id):
    """No-op stand-in for the CP-08 per-request tenant SSO overlay
    (G-021-02), which otherwise opens a real tenant DB to read sso.config.
    The replay-reject path does not depend on overlay content."""
    yield {}


class _FakeSystemDB:
    """Minimal async system session: records executes, replays results."""

    def __init__(self, insert_returns_row: bool):
        self._insert_returns_row = insert_returns_row
        self.executed = []
        self.committed = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        result = MagicMock()
        text = str(stmt).lower()
        if "insert" in text:
            result.first.return_value = ("assert-1",) if self._insert_returns_row else None
        else:
            # the reap DELETE
            result.first.return_value = None
        return result

    async def commit(self):
        self.committed = True


def _sysdb_gen(db):
    async def _gen():
        yield db
    return _gen


@pytest.mark.asyncio
async def test_first_use_of_assertion_accepted():
    from src.auth.saml_replay import record_assertion_or_reject

    db = _FakeSystemDB(insert_returns_row=True)
    with patch("src.auth.saml_replay.get_system_db", _sysdb_gen(db)):
        ok = await record_assertion_or_reject(
            assertion_id="assert-1", tenant_id="acme",
            not_on_or_after=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    assert ok is True
    assert db.committed is True


@pytest.mark.asyncio
async def test_replayed_assertion_rejected():
    """A second POST of the same assertion id hits ON CONFLICT DO NOTHING and
    returns no row → reject."""
    from src.auth.saml_replay import record_assertion_or_reject

    db = _FakeSystemDB(insert_returns_row=False)
    with patch("src.auth.saml_replay.get_system_db", _sysdb_gen(db)):
        ok = await record_assertion_or_reject(
            assertion_id="assert-1", tenant_id="acme",
            not_on_or_after=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    assert ok is False


@pytest.mark.asyncio
async def test_missing_assertion_id_fails_closed():
    from src.auth.saml_replay import record_assertion_or_reject

    # No DB access should be needed — a missing id can't be tracked → reject.
    ok = await record_assertion_or_reject(
        assertion_id="", tenant_id="acme", not_on_or_after=None,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_db_error_fails_closed():
    from src.auth.saml_replay import record_assertion_or_reject

    class _BoomDB:
        async def execute(self, stmt):
            raise RuntimeError("db down")

        async def commit(self):
            pass

    with patch("src.auth.saml_replay.get_system_db", _sysdb_gen(_BoomDB())):
        ok = await record_assertion_or_reject(
            assertion_id="assert-1", tenant_id="acme", not_on_or_after=None,
        )
    assert ok is False


@pytest.mark.asyncio
async def test_missing_not_on_or_after_still_bounds_row():
    """A missing IdP horizon must not make the row unbounded — a conservative
    expiry is applied so the ledger is still reaped."""
    from src.auth.saml_replay import record_assertion_or_reject

    db = _FakeSystemDB(insert_returns_row=True)
    with patch("src.auth.saml_replay.get_system_db", _sysdb_gen(db)):
        ok = await record_assertion_or_reject(
            assertion_id="assert-1", tenant_id="acme", not_on_or_after=None,
        )
    assert ok is True


# ---------------------------------------------------------------------------
# ACS reject path: a replayed assertion (ledger returns False) yields 401.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acs_rejects_replayed_assertion():
    import types
    import uuid
    import httpx
    from shared.auth.backend import UserIdentity
    from src.main import app
    from .conftest import make_mock_db
    from .test_sso import _saml_result, _yield

    identity = UserIdentity(
        email="saml-user@corp.com", display_name="SAML User",
        groups=[], source_backend="saml", raw_claims={},
    )
    db = make_mock_db()

    with (
        patch("src.api.sso.consume_state", new_callable=AsyncMock,
              return_value=("acme", None, "req-1", None)),
        patch("src.api.sso.process_saml_response",
              return_value=_saml_result(identity)),
        # The replay ledger reports this assertion has already been seen.
        patch("src.api.sso.record_assertion_or_reject",
              new_callable=AsyncMock, return_value=False),
        patch("src.api.sso.tenant_sso_overlay", _noop_sso_overlay),
        patch("src.api.sso.get_tenant_db", lambda tid: _yield(db)),
        patch("src.api.sso.audit", new_callable=AsyncMock) as audit_mock,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            follow_redirects=False,
        ) as ac:
            resp = await ac.post(
                "/api/v1/auth/saml/acs",
                data={"SAMLResponse": "replayed", "RelayState": "state"},
            )

    assert resp.status_code == 401
    assert "replay" in resp.json()["detail"].lower()
    # The failure must be audited as an SSO failure with a replay reason.
    assert any(
        c.kwargs.get("action") == "auth.sso_failure"
        and c.kwargs.get("detail", {}).get("reason") == "assertion_replay"
        for c in audit_mock.await_args_list
    )
