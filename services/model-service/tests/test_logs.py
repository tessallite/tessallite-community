"""Tests for query log API: filters, CSV export, and retention purge."""
from __future__ import annotations

import csv
import io
import types
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from contextlib import contextmanager

from tests.conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    TEST_USER_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


@contextmanager
def _rbac_role(role: str | None):
    """Patch require_role's DB so the caller resolves to ``role`` (F-030-02).

    ``role=None`` simulates a tenant user with NO binding to the project while
    the project HAS other bindings → 403 (not the bootstrap-admin path).
    """
    mock_db = AsyncMock()
    result = MagicMock()
    if role is None:
        result.scalar_one_or_none.return_value = None
        result.first.return_value = (uuid.uuid4(),)  # bindings exist probe
    else:
        result.scalar_one_or_none.return_value = types.SimpleNamespace(
            id=uuid.uuid4(), user_identity=TEST_USER_ID,
            project_id=TEST_PROJECT_ID, model_id=None, role=role,
        )
        result.first.return_value = (uuid.uuid4(),)
    mock_db.execute = AsyncMock(return_value=result)
    with patch("src.auth.rbac.get_tenant_db", async_gen_from(mock_db)):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_query_log(
    *,
    user_identity: str = "alice@test.com",
    status: str = "success",
    route_type: str = "source",
    error_type: str | None = None,
    client_kind: str | None = None,
    created_at: datetime | None = None,
    execution_ms: int = 42,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        user_identity=user_identity,
        protocol="jdbc",
        raw_query="SELECT 1",
        query_fingerprint="abc123",
        route_type=route_type,
        aggregate_id=None,
        pocket_id=None,
        persona_id=None,
        client_kind=client_kind,
        rewritten_query=None,
        execution_ms=execution_ms,
        rows_returned=10,
        bytes_processed=None,
        status=status,
        error_type=error_type,
        error_detail=None,
        created_at=created_at or NOW,
    )


def _mock_db_with_logs(logs: list, total: int | None = None):
    db = make_mock_db()
    call_count = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalars.return_value.all.return_value = logs
        else:
            result.scalar_one.return_value = total if total is not None else len(logs)
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


# ---------------------------------------------------------------------------
# Tests: new filter parameters
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_query_logs_date_range_filter(client):
    logs = [_make_query_log(created_at=NOW)]
    db = _mock_db_with_logs(logs, 1)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            params={
                "date_from": "2026-01-01T00:00:00",
                "date_to": "2026-01-02T00:00:00",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1


@pytest.mark.anyio
async def test_query_logs_user_identity_filter(client):
    logs = [_make_query_log(user_identity="admin@acme.com")]
    db = _mock_db_with_logs(logs, 1)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            params={"user_identity": "admin"},
        )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.anyio
async def test_query_logs_route_type_filter(client):
    logs = [_make_query_log(route_type="aggregate")]
    db = _mock_db_with_logs(logs, 1)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            params={"route_type": "aggregate"},
        )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.anyio
async def test_query_logs_client_kind_filter(client):
    logs = [_make_query_log(client_kind="looker_cloud")]
    db = _mock_db_with_logs(logs, 1)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            params={"client_kind": "looker_cloud"},
        )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["client_kind"] == "looker_cloud"


@pytest.mark.anyio
async def test_query_logs_drill_client_kind_filter(client):
    # Bug-6430: drill-through REST executions are tagged client_kind="drill" by
    # the query-router, so the logs API must accept and filter that value.
    logs = [_make_query_log(client_kind="drill")]
    db = _mock_db_with_logs(logs, 1)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            params={"client_kind": "drill"},
        )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["client_kind"] == "drill"


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", ["/queries", "/queries/export"])
async def test_query_logs_reject_invalid_client_kind(client, suffix):
    resp = await client.get(
        f"/api/v1/projects/{TEST_PROJECT_ID}/logs{suffix}",
        params={"client_kind": "unknown_client"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["headless", "agent", "mcp"])
async def test_query_logs_accept_expanded_client_kinds(client, kind):
    """Bug-7451 / CF-030-DS-F03002: headless, agent, and mcp must be accepted
    by the client_kind filter without 422."""
    logs = [_make_query_log(client_kind=kind)]
    db = _mock_db_with_logs(logs, 1)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            params={"client_kind": kind},
        )
    assert resp.status_code == 200
    assert resp.json()["items"][0]["client_kind"] == kind


# ---------------------------------------------------------------------------
# Bug-7451: probe traffic exclusion from user-facing query log
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_query_logs_exclude_probes_by_default(client):
    """Bug-7451: the default query-log list must exclude introspect route_type
    and discover_members protocol rows so the user sees only real queries.
    Verify by capturing the SQL statement and checking for the probe-exclusion
    WHERE clauses."""
    logs = [_make_query_log(route_type="source")]
    db = _mock_db_with_logs(logs, 1)
    captured = []

    _orig_execute = db.execute

    async def _capture(stmt, *a, **k):
        captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True}))
                        if hasattr(stmt, "compile") else str(stmt))
        return await _orig_execute(stmt, *a, **k)

    db.execute = AsyncMock(side_effect=_capture)

    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
        )
    assert resp.status_code == 200
    # The SQL must contain the probe-exclusion filter.
    combined_sql = " ".join(captured).lower()
    assert "introspect" in combined_sql, "Default query should exclude introspect route_type"
    assert "discover_members" in combined_sql, "Default query should exclude discover_members protocol"


@pytest.mark.anyio
async def test_query_logs_include_probes_when_requested(client):
    """Bug-7451: when include_probes=true, the probe-exclusion filters must NOT
    be applied, so introspect/discover-members rows are visible."""
    probe = _make_query_log(route_type="introspect")
    probe.protocol = "discover_members"
    db = _mock_db_with_logs([probe], 1)
    captured = []

    _orig_execute = db.execute

    async def _capture(stmt, *a, **k):
        captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True}))
                        if hasattr(stmt, "compile") else str(stmt))
        return await _orig_execute(stmt, *a, **k)

    db.execute = AsyncMock(side_effect=_capture)

    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            params={"include_probes": "true"},
        )
    assert resp.status_code == 200
    # The SQL must NOT contain the exclusion filter tokens.
    combined_sql = " ".join(captured).lower()
    # When include_probes=true, we should NOT see the NOT IN exclusion clause
    # for 'introspect' as part of probe filtering.  (The word 'introspect' may
    # appear in data, but NOT IN ('introspect') should be absent.)
    assert "not in" not in combined_sql or "introspect" not in combined_sql.split("not in")[1].split(")")[0], \
        "include_probes=true should not apply probe exclusion"


@pytest.mark.anyio
async def test_csv_export_excludes_probes_by_default(client):
    """Bug-7451 / Bug-7454: the CSV export must also exclude probe rows by
    default, matching the list endpoint behaviour."""
    logs = [_make_query_log(route_type="source")]
    db = make_mock_db()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = logs
    captured = []

    async def _capture(stmt, *a, **k):
        captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True}))
                        if hasattr(stmt, "compile") else str(stmt))
        return result_mock

    db.execute = AsyncMock(side_effect=_capture)
    with _rbac_role("modeler"):
        with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries/export",
            )
    assert resp.status_code == 200
    combined_sql = " ".join(captured).lower()
    assert "introspect" in combined_sql, "CSV export should exclude introspect by default"


@pytest.mark.anyio
async def test_query_logs_combined_filters(client):
    logs = [_make_query_log(user_identity="bob@test.com", status="error", error_type="timeout")]
    db = _mock_db_with_logs(logs, 1)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            params={
                "status": "error",
                "user_identity": "bob",
                "date_from": "2025-12-01T00:00:00",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# Tests: CSV export
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_csv_export_returns_csv(client):
    logs = [_make_query_log(), _make_query_log(user_identity="bob@test.com")]
    db = make_mock_db()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = logs
    db.execute = AsyncMock(return_value=result_mock)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries/export",
        )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    assert "attachment" in resp.headers.get("content-disposition", "")

    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    assert rows[0] == [
        "created_at", "user_identity", "protocol", "client_kind", "raw_query", "route_type",
        "status", "error_type", "execution_ms", "rows_returned", "query_fingerprint",
    ]
    assert len(rows) == 3  # header + 2 data rows


@pytest.mark.anyio
async def test_csv_export_respects_filters(client):
    logs = [_make_query_log(status="error", error_type="timeout")]
    db = make_mock_db()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = logs
    db.execute = AsyncMock(return_value=result_mock)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries/export",
            params={"status": "error"},
        )
    assert resp.status_code == 200
    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    assert len(rows) == 2  # header + 1 data row


@pytest.mark.anyio
async def test_csv_export_neutralises_formula_injection(client):
    """F-030-13: free-text columns beginning with =, +, -, @, tab or CR are
    prefixed with a single quote so a spreadsheet does not execute them."""
    evil = _make_query_log(user_identity="=HYPERLINK(\"http://evil\")")
    evil.raw_query = "=cmd|'/c calc'!A1"
    db = make_mock_db()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [evil]
    db.execute = AsyncMock(return_value=result_mock)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries/export",
        )
    assert resp.status_code == 200
    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    header, data = rows[0], rows[1]
    raw_idx = header.index("raw_query")
    user_idx = header.index("user_identity")
    # The leading-formula character is now neutralised with a quote prefix.
    assert data[raw_idx].startswith("'=")
    assert data[user_idx].startswith("'=")


@pytest.mark.anyio
async def test_query_trace_returns_stages_in_order(client):
    """F-030-16: the stored RouteLog parse/bind/route stages are exposed and
    ordered parse -> bind -> route for one query log scoped to the project."""
    log = _make_query_log()
    stages = [
        types.SimpleNamespace(route_stage="route", detail={"route_type": "aggregate"}),
        types.SimpleNamespace(route_stage="parse", detail={"grain": []}),
        types.SimpleNamespace(route_stage="bind", detail={"resolved_measures": []}),
    ]
    db = make_mock_db()

    async def _get(_model, _id):
        return log

    db.get = _get
    call = {"n": 0}

    async def _execute(stmt):
        call["n"] += 1
        result = MagicMock()
        if call["n"] == 1:
            # model-in-project check -> a matching id
            result.scalar_one_or_none.return_value = log.model_id
        else:
            result.scalars.return_value.all.return_value = stages
        return result

    db.execute = _execute
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries/{log.id}/trace",
        )
    assert resp.status_code == 200
    body = resp.json()
    assert [s["route_stage"] for s in body] == ["parse", "bind", "route"]


# ---------------------------------------------------------------------------
# Tests: retention purge
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_purge_query_logs_deletes_old_entries():
    # Retention resolved via get_setting (registry walk + coerce), not a raw
    # TenantSetting read — value_json stores a scalar int (F-012-05).
    from unittest.mock import patch
    from shared.audit import purge as purge_mod

    db = AsyncMock()

    route_result = MagicMock()
    route_result.rowcount = 5

    query_result = MagicMock()
    query_result.rowcount = 10

    call_count = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        # 1st execute → RouteLog delete, 2nd → QueryLog delete.
        if call_count == 1:
            return route_result
        return query_result

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()

    with patch.object(purge_mod, "get_setting", AsyncMock(return_value=30)):
        deleted = await purge_mod.purge_query_logs(db)
    assert deleted == 10
    assert db.commit.await_count == 1


@pytest.mark.anyio
async def test_purge_query_logs_zero_retention_skips():
    # 0 = indefinite must skip the purge — the bug let an explicit 0 fall
    # through to the 90-day default (F-012-05).
    from unittest.mock import patch
    from shared.audit import purge as purge_mod

    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    with patch.object(purge_mod, "get_setting", AsyncMock(return_value=0)):
        deleted = await purge_mod.purge_query_logs(db)
    assert deleted == 0
    assert db.commit.await_count == 0
    db.execute.assert_not_awaited()


@pytest.mark.anyio
async def test_purge_query_logs_default_retention():
    # When the tenant has not set a value, get_setting returns the registry
    # default (90). The purge runs but deletes nothing here.
    from unittest.mock import patch
    from shared.audit import purge as purge_mod

    db = AsyncMock()

    route_result = MagicMock()
    route_result.rowcount = 0
    query_result = MagicMock()
    query_result.rowcount = 0

    call_count = 0

    async def _execute(stmt, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return route_result
        return query_result

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()

    with patch.object(purge_mod, "get_setting", AsyncMock(return_value=90)):
        deleted = await purge_mod.purge_query_logs(db)
    assert deleted == 0


@pytest.mark.anyio
async def test_purge_query_logs_respects_scalar_indefinite_through_registry():
    """F-012-05 root-cause proof: TenantSetting.value_json stores a bare
    scalar int (0 for indefinite), NOT a {"value": n} dict. The purge resolves
    retention through the real registry resolver, so a stored scalar 0 must
    skip the purge — the old direct read assumed a dict and silently fell back
    to the 90-day default, deleting logs the tenant marked indefinite.

    This drives the actual `shared.config.resolver._read_tenant` so the test
    exercises the real shape rather than a stub of `get_setting`.
    """
    from shared.audit import purge as purge_mod
    from shared.config import resolver as resolver_mod

    resolver_mod.clear_cache()

    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.info = {}

    async def _read_tenant_scalar(session, key, tenant_id=""):
        # Real stored shape: a bare scalar int, exactly what the resolver and
        # registry coerce produce. 0 = indefinite.
        if key == "query_log.retention_days":
            return True, 0
        return False, None

    with patch.object(resolver_mod, "_read_tenant", side_effect=_read_tenant_scalar):
        deleted = await purge_mod.purge_query_logs(db)

    assert deleted == 0
    db.execute.assert_not_awaited()  # purge skipped entirely
    db.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_purge_query_miss_logs_deletes_old_entries():
    """F-030-17: miss logs are purged by last_seen_at against the same
    query-log retention setting."""
    from unittest.mock import patch
    from shared.audit import purge as purge_mod

    db = AsyncMock()
    result = MagicMock()
    result.rowcount = 7
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    with patch.object(purge_mod, "get_setting", AsyncMock(return_value=30)):
        deleted = await purge_mod.purge_query_miss_logs(db)
    assert deleted == 7
    assert db.commit.await_count == 1


@pytest.mark.anyio
async def test_purge_query_miss_logs_zero_retention_skips():
    """F-030-17: a retention of 0 means indefinite — no miss-log purge runs."""
    from unittest.mock import patch
    from shared.audit import purge as purge_mod

    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    with patch.object(purge_mod, "get_setting", AsyncMock(return_value=0)):
        deleted = await purge_mod.purge_query_miss_logs(db)
    assert deleted == 0
    db.execute.assert_not_awaited()
    assert db.commit.await_count == 0


# ---------------------------------------------------------------------------
# F-030-24: misses endpoint persona + min-occurrence filters
# ---------------------------------------------------------------------------

def _make_miss_log(*, occurrence_count: int = 5, persona_id=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        query_fingerprint="fp",
        miss_reason="no_aggregate",
        normalized_query=None,
        requested_dimensions=[],
        requested_measures=[],
        requested_grain=[],
        occurrence_count=occurrence_count,
        first_seen_at=NOW,
        last_seen_at=NOW,
        candidate_aggregate_id=None,
        persona_id=persona_id,
    )


@pytest.mark.anyio
async def test_misses_accepts_persona_and_min_occurrence_filters(client):
    """F-030-24: the misses endpoint accepts persona_id and min_occurrence and
    applies them as WHERE clauses (captured statements) without error."""
    persona = uuid.uuid4()
    rows = [_make_miss_log(occurrence_count=5, persona_id=persona)]
    db = make_mock_db()
    captured = []

    async def _execute(stmt, *a, **k):
        captured.append(str(stmt))
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    db.execute = AsyncMock(side_effect=_execute)
    with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/logs/misses"
            f"?model_id={TEST_MODEL_ID}&persona_id={persona}&min_occurrence=3",
        )
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    sql = " ".join(captured)
    assert "persona_id" in sql
    assert "occurrence_count" in sql


# ---------------------------------------------------------------------------
# Tests: project RBAC (F-030-02)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_logs_403_for_user_with_no_project_binding(client):
    """A tenant user with no binding to the project (project has other bindings,
    so the bootstrap-admin path does not apply) cannot read its query logs."""
    db = _mock_db_with_logs([_make_query_log()], 1)
    with _rbac_role(None):
        with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_list_logs_viewer_allowed(client):
    db = _mock_db_with_logs([_make_query_log()], 1)
    with _rbac_role("viewer"):
        with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries",
            )
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_csv_export_403_for_viewer(client):
    """Bulk CSV export of raw query rows is stricter than a page read — a
    viewer must be denied; only modeler+ may export (F-030-02)."""
    db = make_mock_db()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [_make_query_log()]
    db.execute = AsyncMock(return_value=result_mock)
    with _rbac_role("viewer"):
        with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries/export",
            )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_csv_export_modeler_allowed(client):
    db = make_mock_db()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [_make_query_log()]
    db.execute = AsyncMock(return_value=result_mock)
    with _rbac_role("modeler"):
        with patch("src.api.logs.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/projects/{TEST_PROJECT_ID}/logs/queries/export",
            )
    assert resp.status_code == 200
