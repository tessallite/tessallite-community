"""Tests for the per-column collation determinism checker (Bug-7892 / I16).

These verify that the collation profiler correctly identifies per-column
collation determinism from the database catalog. The profiler is the
foundation of text BIJECTION relabel certification: a non-deterministic
collation can merge distinct codes and produce wrong numbers.

Known-answer cases:
  (1) Deterministic collation (binary/default/C): column's collation is
      deterministic -> check returns True -> text relabel can be certified.
  (2) Non-deterministic collation (ci/ai): column's collation is non-
      deterministic -> check returns False -> text relabel is REJECTED.
  (3) Execution error/timeout: check returns False (fail-closed -> source-only).
  (4) Unknown connector (BigQuery/Snowflake/etc): fail closed (False).
"""
from __future__ import annotations

import pytest

from shared.semantic.collation_profiler import (
    check_column_collation_deterministic,
    check_columns_collation_deterministic,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake executor for PostgreSQL catalog queries
# ---------------------------------------------------------------------------


class _FakeConn:
    connection_type = "postgresql"


def _fake_executor(row_map):
    """Return an execute_source_sql stub mapping SQL substrings to rows."""
    async def _exec(conn_obj, sql, *, tenant_session=None):
        for needle, rows in row_map.items():
            if needle in sql:
                return rows, []
        return [], []
    return _exec


# ---------------------------------------------------------------------------
# Known-answer proof (1): deterministic collation -> True (certified)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_collation_returns_true(monkeypatch):
    """A PostgreSQL column whose collation is deterministic (binary/C/default):
    the catalog query returns one row with is_deterministic=True -> True
    (group-stable). The text relabel CAN be certified."""
    import shared.source_executor as se

    async def _exec(conn_obj, sql, *, tenant_session=None):
        # Column exists, collation is deterministic.
        return [{"is_deterministic": True}], ["is_deterministic"]

    monkeypatch.setattr(se, "execute_source_sql", _exec)
    result = await check_column_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        schema="public", table="dim_geo", column="country_code",
    )
    assert result is True


# ---------------------------------------------------------------------------
# Known-answer proof (2): non-deterministic collation -> False (REJECTED)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nondeterministic_collation_returns_false(monkeypatch):
    """A PostgreSQL column whose collation is non-deterministic (ci/ai/ICU
    with case folding): the catalog query returns one row with
    is_deterministic=False -> False (NOT group-stable). The text relabel
    MUST be REJECTED because it could fold distinct codes and produce wrong
    numbers."""
    import shared.source_executor as se

    async def _exec(conn_obj, sql, *, tenant_session=None):
        # Column exists, collation is non-deterministic.
        return [{"is_deterministic": False}], ["is_deterministic"]

    monkeypatch.setattr(se, "execute_source_sql", _exec)
    result = await check_column_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        schema="public", table="dim_geo", column="country_code",
    )
    assert result is False


# ---------------------------------------------------------------------------
# Nonexistent column -> False (fail-closed, not false-certified)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonexistent_column_fails_closed(monkeypatch):
    """A column that does not exist in the catalog returns zero rows ->
    fail closed (False). This must NEVER return True (would false-certify
    a misnamed column)."""
    import shared.source_executor as se

    async def _exec(conn_obj, sql, *, tenant_session=None):
        return [], []  # zero rows = column not found

    monkeypatch.setattr(se, "execute_source_sql", _exec)
    result = await check_column_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        schema="public", table="dim_geo", column="nonexistent_col",
    )
    assert result is False


# ---------------------------------------------------------------------------
# Known-answer proof: attcollation=0 on a text column -> False (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attcollation_zero_fails_closed(monkeypatch):
    """attcollation=0 means non-collatable type (e.g. integer). For a text
    column we are trying to certify, this is unexpected and must fail closed
    (return False), never return True. The catalog query returns
    is_deterministic=false for attcollation=0."""
    import shared.source_executor as se

    async def _exec(conn_obj, sql, *, tenant_session=None):
        # The query returns is_deterministic=false for attcollation=0.
        return [{"is_deterministic": False}], ["is_deterministic"]

    monkeypatch.setattr(se, "execute_source_sql", _exec)
    result = await check_column_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        schema="public", table="dim_geo", column="int_column",
    )
    assert result is False


# ---------------------------------------------------------------------------
# Known-answer proof (3): execution error -> False (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_error_fails_closed_to_false(monkeypatch):
    """Any execution error fails closed to False (collation NOT proven
    deterministic -> text certification refused)."""
    import shared.source_executor as se

    async def _boom(conn_obj, sql, *, tenant_session=None):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(se, "execute_source_sql", _boom)
    result = await check_column_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        schema="public", table="dim_geo", column="country_code",
    )
    assert result is False


@pytest.mark.asyncio
async def test_timeout_fails_closed_to_false(monkeypatch):
    """A timeout on the catalog query fails closed to False."""
    import shared.source_executor as se
    from shared.source_executor import QueryTimeoutError

    async def _timeout(conn_obj, sql, *, tenant_session=None):
        raise QueryTimeoutError("slow catalog query")

    monkeypatch.setattr(se, "execute_source_sql", _timeout)
    result = await check_column_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        schema="public", table="dim_geo", column="country_code",
    )
    assert result is False


# ---------------------------------------------------------------------------
# Known-answer proof (4): unknown connector -> False (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bigquery_fails_closed():
    """BigQuery: cannot determine per-column collation from SQL catalog ->
    fail closed. Text relabels stay source-only."""
    result = await check_column_collation_deterministic(
        conn_obj=_FakeConn(), connector="bigquery",
        schema="dataset", table="dim_geo", column="country_code",
    )
    assert result is False


@pytest.mark.asyncio
async def test_snowflake_fails_closed():
    result = await check_column_collation_deterministic(
        conn_obj=_FakeConn(), connector="snowflake",
        schema="schema", table="dim_geo", column="country_code",
    )
    assert result is False


# ---------------------------------------------------------------------------
# Multi-column check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_columns_deterministic_returns_true(monkeypatch):
    """check_columns_collation_deterministic returns True only when ALL
    listed columns have deterministic collation."""
    import shared.source_executor as se

    async def _exec(conn_obj, sql, *, tenant_session=None):
        return [{"is_deterministic": True}], ["is_deterministic"]

    monkeypatch.setattr(se, "execute_source_sql", _exec)
    result = await check_columns_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        table_ref="public.dim_geo",
        columns=["country_code", "country_name"],
    )
    assert result is True


@pytest.mark.asyncio
async def test_any_nondeterministic_column_returns_false(monkeypatch):
    """If ANY column has a non-deterministic collation, the multi-column
    check returns False."""
    import shared.source_executor as se
    call_count = [0]
    async def _exec(conn_obj, sql, *, tenant_session=None):
        call_count[0] += 1
        if call_count[0] == 2:
            # Second column: non-deterministic
            return [{"is_deterministic": False}], ["is_deterministic"]
        return [{"is_deterministic": True}], ["is_deterministic"]
    monkeypatch.setattr(se, "execute_source_sql", _exec)
    result = await check_columns_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        table_ref="public.dim_geo",
        columns=["country_code", "country_name"],
    )
    assert result is False


@pytest.mark.asyncio
async def test_malformed_table_ref_fails_closed():
    """A table_ref without a dot (no schema) fails closed."""
    result = await check_columns_collation_deterministic(
        conn_obj=_FakeConn(), connector="postgresql",
        table_ref="dim_geo",
        columns=["country_code"],
    )
    assert result is False
