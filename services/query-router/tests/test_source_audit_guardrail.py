"""Phase 4.2 — Source audit guard rail tests.

Verifies:
1. PostgresExecutor._tag() injects the /* tessallite:routed */ comment.
2. F-027-11: the dispatcher tags routed SQL centrally for ALL dialects.
3. [SOURCE_AUDIT] logging emits entries when the env flag is set.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from src.execution.dispatcher import _ROUTED_MARKER, _tag_routed
from src.execution.postgres_executor import PostgresExecutor


# ---------------------------------------------------------------------------
# PostgresExecutor._tag()
# ---------------------------------------------------------------------------


def test_tag_injects_routed_comment():
    sql = 'SELECT "month", SUM("amount") FROM "modely" GROUP BY "month"'
    tagged = PostgresExecutor._tag(sql)
    assert tagged.startswith("/* tessallite:routed */")
    assert sql in tagged


def test_tag_idempotent():
    sql = '/* tessallite:routed */ SELECT 1'
    assert PostgresExecutor._tag(sql) == sql


def test_tag_idempotent_with_leading_whitespace():
    sql = '  /* tessallite:routed */ SELECT 1'
    assert PostgresExecutor._tag(sql) == sql


def test_tag_preserves_sql_content():
    sql = "SELECT * FROM foo WHERE x = 'bar'"
    tagged = PostgresExecutor._tag(sql)
    assert tagged == f"/* tessallite:routed */ {sql}"


# ---------------------------------------------------------------------------
# F-027-11 — central dispatcher tagging (all dialects)
# ---------------------------------------------------------------------------


def test_dispatcher_tag_injects_routed_comment():
    sql = "SELECT 1 FROM bigquery_table"
    tagged = _tag_routed(sql)
    assert tagged.startswith(_ROUTED_MARKER)
    assert sql in tagged


def test_dispatcher_tag_idempotent_with_pg_tag():
    # PostgresExecutor._tag sees SQL already tagged by the dispatcher and
    # must not double-tag it.
    once = _tag_routed("SELECT 1")
    assert PostgresExecutor._tag(once) == once


def test_dispatcher_tag_idempotent_with_leading_whitespace():
    sql = f"   {_ROUTED_MARKER} SELECT 1"
    assert _tag_routed(sql) == sql


@pytest.mark.asyncio
async def test_execute_on_connection_tags_all_dialects(monkeypatch):
    """Every connector branch receives SQL carrying the routed marker."""
    import src.execution.dispatcher as disp

    seen: dict[str, str] = {}

    class _StubExecutor:
        def __init__(self, *_a, **_k):
            pass

        async def execute(self, sql):
            seen["sql"] = sql
            return ([], 0, [])

        def execute_sync(self, sql):  # bigquery path (run in thread)
            seen["sql"] = sql
            return ([], 0, [])

        def close(self):
            pass

        @classmethod
        async def create(cls, *_a, **_k):
            return cls()

    # Patch each executor import target the dispatcher resolves lazily.
    import src.execution.snowflake_executor as sf
    import src.execution.spark_executor as sp
    import src.execution.sqlserver_executor as ms
    monkeypatch.setattr(sf, "SnowflakeExecutor", _StubExecutor)
    monkeypatch.setattr(sp, "SparkExecutor", _StubExecutor)
    monkeypatch.setattr(ms, "SqlServerExecutor", _StubExecutor)

    class _Conn:
        def __init__(self, ctype):
            self.connection_type = ctype

    for ctype in ("snowflake", "hadoop_spark", "sqlserver"):
        seen.clear()
        await disp.execute_on_connection("SELECT 1", _Conn(ctype), db=None)
        assert seen["sql"].startswith(_ROUTED_MARKER), ctype


# ---------------------------------------------------------------------------
# [SOURCE_AUDIT] logging
# ---------------------------------------------------------------------------


@pytest.fixture()
def _enable_source_audit():
    """Temporarily enable the SOURCE_AUDIT flag in the source_executor module."""
    import shared.source_executor as mod
    original = mod._SOURCE_AUDIT
    mod._SOURCE_AUDIT = True
    yield
    mod._SOURCE_AUDIT = original


@pytest.fixture()
def _disable_source_audit():
    """Ensure SOURCE_AUDIT is off."""
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
