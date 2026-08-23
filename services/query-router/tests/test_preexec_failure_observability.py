"""Tests for pre-execution failure observability (Bug-7674).

Pre-execution failures (parse / bind / undeployed model / persona-gate denial /
route-stage 422s) previously raised HTTPException before a BoundQuery existed and
were NEVER persisted to QueryLog — the log viewer's status=error filter,
failure-spike alerting and CSV exports missed the most common real-world failure
class. These tests assert that:

  * ``_preexec_error_type`` maps each pre-execution status / typed detail to the
    documented, stable error_type label (parse_error / binding_error /
    not_deployed / persona_denied / routing_rejected, and typed detail
    passthrough), and
  * ``log_query_failure`` with ``bound_query=None`` persists a QueryLog error row
    carrying the request's raw query and protocol (via the overrides), so a
    failed query is observable with a non-empty preview.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status

from src.api.routes import _preexec_error_type


# ---------------------------------------------------------------------------
# _preexec_error_type — status / typed-detail classification
# ---------------------------------------------------------------------------


def test_preexec_error_type_uses_typed_detail_when_present():
    exc = HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"message": "x", "error_type": "no_aggregate_match"},
    )
    assert _preexec_error_type(exc) == "no_aggregate_match"


def test_preexec_error_type_parse_400():
    exc = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Parse failed")
    assert _preexec_error_type(exc) == "parse_error"


def test_preexec_error_type_not_deployed_409():
    exc = HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not deployed")
    assert _preexec_error_type(exc) == "not_deployed"


def test_preexec_error_type_persona_denied_403():
    exc = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="denied")
    assert _preexec_error_type(exc) == "persona_denied"


def test_preexec_error_type_binding_422_without_typed_detail():
    exc = HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown column",
    )
    assert _preexec_error_type(exc) == "binding_error"


def test_preexec_error_type_server_fault_500():
    exc = HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="boom",
    )
    assert _preexec_error_type(exc) == "parse_error"


def test_preexec_error_type_snapshot_unavailable_503():
    """Bug-8520: 503 had no mapping, so a blocked deployment fell through to
    the catch-all ``routing_rejected`` — indistinguishable in QueryLog from a
    genuine "no route for this query" rejection, which defeats the whole point
    of the typed 503. Every /execute, /headless/query and /plugin/execute
    pre-execution failure runs through this mapper, so an operator filtering
    or alerting on error_type could not isolate "a deployment needs repair"."""
    exc = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Model is deployed but its deployed snapshot is unavailable",
    )
    assert _preexec_error_type(exc) == "snapshot_unavailable"


def test_preexec_error_type_still_falls_back_to_routing_rejected():
    """The catch-all must stay reachable for genuinely unmapped statuses — the
    503 branch narrows the fallback, it must not replace it."""
    exc = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="nope")
    assert _preexec_error_type(exc) == "routing_rejected"


# ---------------------------------------------------------------------------
# log_query_failure(bound_query=None, ...) persists a usable error row
# ---------------------------------------------------------------------------


class _CapturingDB:
    def __init__(self):
        self.added: list = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_log_query_failure_bound_none_uses_overrides():
    """Bug-7674: with no BoundQuery, the row must carry the request's raw query
    and protocol (via the overrides) and status='error' — not an empty preview
    and a placeholder protocol."""
    from src.logging.query_logger import log_query_failure

    db = _CapturingDB()
    row = await log_query_failure(
        db=db,
        bound_query=None,
        decision=None,
        execution_ms=3,
        user_identity="user@example.com",
        error_type="binding_error",
        error_detail="Unknown filter column 'ghost'",
        raw_query_override="SELECT ghost FROM sales",
        protocol_override="jdbc",
    )
    assert db.committed
    assert row.status == "error"
    assert row.error_type == "binding_error"
    assert row.model_id is None
    assert row.raw_query == "SELECT ghost FROM sales"
    assert row.protocol == "jdbc"
    assert row.route_type == "unknown"
    assert row.rows_returned == 0


@pytest.mark.asyncio
async def test_log_query_failure_bound_none_defaults_without_overrides():
    """Backwards-compatible: without the overrides, a bound=None failure still
    logs (empty raw_query, protocol='unknown') — the historical shape."""
    from src.logging.query_logger import log_query_failure

    db = _CapturingDB()
    row = await log_query_failure(
        db=db,
        bound_query=None,
        decision=None,
        execution_ms=0,
        error_type="routing_error",
        error_detail="x",
    )
    assert row.raw_query == ""
    assert row.protocol == "unknown"
    assert row.status == "error"


# ---------------------------------------------------------------------------
# _log_preexec_failure — best-effort, classifies, and never raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_preexec_failure_persists_and_classifies(monkeypatch):
    """The wrapper helper persists a classified failure row and runs the
    failure-spike check, using the raw query + protocol from the request."""
    import src.api.routes as routes_mod

    captured = {}

    async def _fake_log_query_failure(**kwargs):
        captured.update(kwargs)

    async def _fake_spike(db, tenant_id, project_id=None):
        captured["spike_checked"] = True

    monkeypatch.setattr(routes_mod, "log_query_failure", _fake_log_query_failure)
    monkeypatch.setattr(routes_mod, "_check_failure_spike", _fake_spike)

    exc = HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not deployed")
    await routes_mod._log_preexec_failure(
        db=AsyncMock(),
        user_identity="u@example.com",
        tenant_id="t1",
        raw_query="SELECT 1 FROM m",
        protocol="jdbc",
        exc=exc,
        start_ms=0.0,
        persona_id=uuid.uuid4(),
        client_kind="agent",
    )
    assert captured["error_type"] == "not_deployed"
    assert captured["raw_query_override"] == "SELECT 1 FROM m"
    assert captured["protocol_override"] == "jdbc"
    assert captured["client_kind"] == "agent"
    assert captured.get("spike_checked") is True


@pytest.mark.asyncio
async def test_log_preexec_failure_swallows_persistence_error(monkeypatch):
    """A failure to persist the observability row must NOT convert a clean 4xx
    into a 500 — the helper swallows its own errors."""
    import src.api.routes as routes_mod

    async def _boom(**kwargs):
        raise RuntimeError("db down")

    async def _fake_spike(db, tenant_id, project_id=None):
        pass

    monkeypatch.setattr(routes_mod, "log_query_failure", _boom)
    monkeypatch.setattr(routes_mod, "_check_failure_spike", _fake_spike)

    exc = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Parse failed")
    # Must not raise.
    await routes_mod._log_preexec_failure(
        db=AsyncMock(),
        user_identity="u@example.com",
        tenant_id="t1",
        raw_query="bad sql",
        protocol="jdbc",
        exc=exc,
        start_ms=0.0,
    )
