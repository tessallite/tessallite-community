"""Source audit guard rail + single-execution-gateway tests.

F-014-02 / Bug-7984: routed user-query physical I/O flows through the SINGLE
public shared gateway ``shared.source_executor.execute_routed_query``. The
query-router no longer holds its own connector executor classes or opens driver
connections. These tests verify:

1. The dispatcher tags routed SQL centrally for ALL dialects (F-027-11).
2. The dispatcher delegates to the shared gateway and preserves the
   ``(rows, bytes, columns)`` contract.
3. A placeholder/unconfigured connection is rejected before any connect.
4. Every routed execution produces a ``[SOURCE_AUDIT]`` trace.
5. Producer-derived guard: no connector driver is imported directly by the
   query-router execution package (the single-gateway invariant).
6. ``[SOURCE_AUDIT]`` redaction behaviour (Bug-7174).
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from src.execution.dispatcher import _ROUTED_MARKER, _tag_routed


# ---------------------------------------------------------------------------
# F-027-11 — central dispatcher tagging (all dialects)
# ---------------------------------------------------------------------------


def test_dispatcher_tag_injects_routed_comment():
    sql = "SELECT 1 FROM bigquery_table"
    tagged = _tag_routed(sql)
    assert tagged.startswith(_ROUTED_MARKER)
    assert sql in tagged


def test_dispatcher_tag_idempotent():
    once = _tag_routed("SELECT 1")
    assert _tag_routed(once) == once


def test_dispatcher_tag_idempotent_with_leading_whitespace():
    sql = f"   {_ROUTED_MARKER} SELECT 1"
    assert _tag_routed(sql) == sql


# ---------------------------------------------------------------------------
# F-014-02 — the dispatcher delegates to the shared execution gateway,
# preserving the (rows, bytes, columns) contract, and passes tagged SQL.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_on_connection_delegates_to_shared_gateway(monkeypatch):
    import src.execution.dispatcher as disp

    seen: dict[str, object] = {}

    async def _fake_routed(conn, sql, *, tenant_session=None, tenant_slug=None, max_rows=None):
        seen["sql"] = sql
        seen["max_rows"] = max_rows
        return ([{"a": 1}], 42, ["a"])

    monkeypatch.setattr(disp, "execute_routed_query", _fake_routed)

    class _Conn:
        connection_type = "bigquery"
        config = {}

    rows, byts, cols = await disp.execute_on_connection("SELECT 1", _Conn(), db=None)
    assert (rows, byts, cols) == ([{"a": 1}], 42, ["a"])
    # SQL reached the gateway carrying the routed marker.
    assert str(seen["sql"]).startswith(_ROUTED_MARKER)
    # The result.max_rows cap is threaded to the gateway (enforced there).
    assert isinstance(seen["max_rows"], int)


@pytest.mark.asyncio
async def test_execute_on_connection_translates_too_large(monkeypatch):
    """A shared SourceResultTooLargeError becomes the query-router
    ResultTooLargeError so the HTTP layer's handling is unchanged."""
    import src.execution.dispatcher as disp
    from shared.source_executor import SourceResultTooLargeError
    from src.ir.logical_query import ResultTooLargeError

    async def _boom(conn, sql, *, tenant_session=None, tenant_slug=None, max_rows=None):
        raise SourceResultTooLargeError("too big")

    monkeypatch.setattr(disp, "execute_routed_query", _boom)

    class _Conn:
        connection_type = "postgresql"
        config = {}

    with pytest.raises(ResultTooLargeError):
        await disp.execute_on_connection("SELECT 1", _Conn(), db=None)


@pytest.mark.asyncio
async def test_execute_on_connection_rejects_unconfigured_before_connect():
    """A placeholder connection (config.unconfigured=True) must be rejected by
    the shared gateway's guard before any connect attempt."""
    import src.execution.dispatcher as disp
    from shared.source_executor import UnconfiguredConnectionError

    class _Conn:
        connection_type = "postgresql"
        config = {"unconfigured": True}
        display_name = "placeholder"

    with pytest.raises(UnconfiguredConnectionError):
        await disp.execute_on_connection("SELECT 1", _Conn(), db=None)


@pytest.mark.asyncio
async def test_execute_on_connection_emits_source_audit(monkeypatch, caplog):
    """Every routed execution must produce a [SOURCE_AUDIT] trace via the
    shared gateway. Patch the per-connector shared dispatch so no real driver
    is opened, but keep the gateway's guard + audit path live."""
    import shared.source_executor as se_mod

    async def _fake_pg_routed(conn_obj, sql, *, tenant_session=None, max_rows=None, tenant_slug=None):
        return ([], 0, [])

    monkeypatch.setattr(se_mod, "_execute_pg_routed", _fake_pg_routed)
    original = se_mod._SOURCE_AUDIT
    se_mod._SOURCE_AUDIT = True
    try:
        import src.execution.dispatcher as disp

        class _Conn:
            connection_type = "postgresql"
            config = {}
            id = None
            project_id = None

        with caplog.at_level(logging.INFO):
            await disp.execute_on_connection("SELECT 1", _Conn(), db=None)

        assert any("[SOURCE_AUDIT]" in r.message for r in caplog.records)
    finally:
        se_mod._SOURCE_AUDIT = original


@pytest.mark.asyncio
async def test_routed_source_audit_attributes_tenant(monkeypatch, caplog):
    """Bug-8039/8041: the routed user-query [SOURCE_AUDIT] record must attribute
    the touch to the canonical TENANT derived from the tenant-bound session — a
    project UUID is not enough because it can collide across tenants."""
    import types

    import shared.source_executor as se_mod

    seen = {}

    async def _fake_pg_routed(conn_obj, sql, *, tenant_session=None, max_rows=None, tenant_slug=None):
        seen["tenant_slug"] = tenant_slug
        return ([], 0, [])

    monkeypatch.setattr(se_mod, "_execute_pg_routed", _fake_pg_routed)
    original = se_mod._SOURCE_AUDIT
    se_mod._SOURCE_AUDIT = True
    try:
        import src.execution.dispatcher as disp

        class _Conn:
            connection_type = "postgresql"
            config = {}
            id = "conn-1"
            project_id = "proj-1"

        # A tenant-bound session carries the slug in session.info['tenant_id'].
        fake_db = types.SimpleNamespace(info={"tenant_id": "acme-demo"})
        with caplog.at_level(logging.INFO):
            await disp.execute_on_connection("SELECT 1", _Conn(), db=fake_db)

        assert seen["tenant_slug"] == "acme-demo", "tenant slug must reach the executor"
        assert any(
            "[SOURCE_AUDIT]" in r.message and "tenant=acme-demo" in r.message
            for r in caplog.records
        ), "routed audit record must carry tenant=<slug>"
    finally:
        se_mod._SOURCE_AUDIT = original


# ---------------------------------------------------------------------------
# F-014-02 producer-derived guard: the single-gateway invariant.
# ---------------------------------------------------------------------------


def test_no_connector_driver_imported_in_query_router_execution():
    """The query-router execution package must NOT import any source-database
    driver directly; all physical I/O goes through the shared gateway. This
    fails if a per-connector executor (opening asyncpg/bigquery/pyhive/etc.) is
    reintroduced outside the sanctioned shared dialect layer."""
    import pathlib

    exec_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "execution"
    driver_tokens = (
        "import asyncpg",
        "from google.cloud import bigquery",
        "import snowflake.connector",
        "from pyhive import hive",
        "import aioodbc",
    )
    offenders: list[str] = []
    for py in exec_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for tok in driver_tokens:
            if tok in text:
                offenders.append(f"{py.name}: {tok}")
    assert not offenders, (
        "query-router execution package opened a connector driver directly; "
        "route all physical I/O through shared.source_executor.execute_routed_query "
        f"instead: {offenders}"
    )


# ---------------------------------------------------------------------------
# [SOURCE_AUDIT] logging + redaction (Bug-7174)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _enable_source_audit():
    import shared.source_executor as mod
    original = mod._SOURCE_AUDIT
    mod._SOURCE_AUDIT = True
    yield
    mod._SOURCE_AUDIT = original


@pytest.fixture()
def _disable_source_audit():
    import shared.source_executor as mod
    original = mod._SOURCE_AUDIT
    mod._SOURCE_AUDIT = False
    yield
    mod._SOURCE_AUDIT = original


def test_audit_log_emits_when_enabled(_enable_source_audit, caplog):
    from shared.source_executor import _audit_log

    with caplog.at_level(logging.INFO):
        _audit_log("execute_source_sql", "SELECT 1 FROM test_table")

    assert any("[SOURCE_AUDIT]" in record.message for record in caplog.records)
    assert any("execute_source_sql" in record.message for record in caplog.records)


def test_audit_log_silent_when_disabled(_disable_source_audit, caplog):
    from shared.source_executor import _audit_log

    with caplog.at_level(logging.INFO):
        _audit_log("execute_source_sql", "SELECT 1 FROM test_table")

    assert not any("[SOURCE_AUDIT]" in record.message for record in caplog.records)


def test_audit_log_truncates_long_sql(_enable_source_audit, caplog):
    from shared.source_executor import _audit_log

    long_sql = "SELECT " + "x" * 200 + " FROM test_table"
    with caplog.at_level(logging.INFO):
        _audit_log("execute_source_ddl", long_sql)

    audit_records = [r for r in caplog.records if "[SOURCE_AUDIT]" in r.message]
    assert len(audit_records) == 1
    assert len(audit_records[0].message) < len(long_sql) + 100


def test_audit_log_redacts_string_literals(_enable_source_audit, caplog):
    from shared.source_executor import _audit_log

    sql = "SELECT * FROM customers WHERE email='person@example.com' AND name='John Doe'"
    with caplog.at_level(logging.INFO):
        _audit_log("execute_source_sql", sql)

    audit_records = [r for r in caplog.records if "[SOURCE_AUDIT]" in r.message]
    assert len(audit_records) == 1
    msg = audit_records[0].message
    assert "person@example.com" not in msg
    assert "John Doe" not in msg
    assert "'?'" in msg


def test_audit_log_redacts_numeric_literals(_enable_source_audit, caplog):
    from shared.source_executor import _audit_log

    sql = "SELECT * FROM accounts WHERE balance > 50000 AND account_id = 12345"
    with caplog.at_level(logging.INFO):
        _audit_log("execute_source_sql", sql)

    audit_records = [r for r in caplog.records if "[SOURCE_AUDIT]" in r.message]
    assert len(audit_records) == 1
    msg = audit_records[0].message
    assert "50000" not in msg
    assert "12345" not in msg


def test_redact_sql_literals_unit():
    from shared.source_executor import _redact_sql_literals

    assert "'?'" in _redact_sql_literals("SELECT * FROM t WHERE x = 'hello'")
    assert "hello" not in _redact_sql_literals("SELECT * FROM t WHERE x = 'hello'")
    result = _redact_sql_literals("SELECT * FROM t WHERE x = 'it\\'s fine'")
    assert "it" not in result or "'?'" in result
