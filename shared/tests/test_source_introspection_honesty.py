"""F-014-05 / F-014-09 / G-014-02: introspection honesty.

Truncation is a flag, never a selectable ``__truncated__`` row. BigQuery
profile SQL uses APPROX_COUNT_DISTINCT and ``__TABLES__``, not COUNT(*).
"""
from __future__ import annotations

import pytest

from shared.source_introspection import (
    TRUNCATION_SCHEMA,
    TRUNCATION_TABLE,
    _build_cardinality_sql,
    _bq_tables_row_count_sql,
    is_truncation_marker,
    normalize_discover_payload,
)


def test_normalize_discover_payload_tuple_sets_truncated_without_sentinel():
    tables = [{"schema": "public", "table": "orders", "type": "BASE TABLE"}]
    payload = normalize_discover_payload((tables, True))
    assert payload["truncated"] is True
    assert payload["tables"] == tables
    assert all(not is_truncation_marker(t) for t in payload["tables"])


def test_normalize_discover_payload_strips_legacy_sentinel_row():
    raw = [
        {"schema": "public", "table": "orders", "type": "BASE TABLE"},
        {"schema": TRUNCATION_SCHEMA, "table": TRUNCATION_TABLE, "type": "NOTICE"},
    ]
    payload = normalize_discover_payload(raw)
    assert payload["truncated"] is True
    assert payload["tables"] == [
        {"schema": "public", "table": "orders", "type": "BASE TABLE"},
    ]


def test_normalize_discover_payload_keeps_real_public_truncated_table_bug_9290():
    """Bug-9290: a PostgreSQL table named ``__truncated__`` in ``public`` is real."""
    row = {"schema": "public", "table": "__truncated__", "type": "BASE TABLE"}
    payload = normalize_discover_payload([row])
    assert payload["truncated"] is False
    assert payload["tables"] == [row]
    assert is_truncation_marker(row) is False


@pytest.mark.asyncio
async def test_profile_table_rejects_truncation_marker():
    from shared.source_introspection import profile_table

    conn = type("C", (), {"connection_type": "postgresql", "encrypted_credentials": b"x", "config": {}})()
    with pytest.raises(ValueError, match="Truncation markers"):
        await profile_table(conn, schema=TRUNCATION_SCHEMA, table=TRUNCATION_TABLE)


@pytest.mark.asyncio
async def test_profile_table_does_not_reject_public_truncated_bug_9290():
    """Bug-9290: profile_table must not raise for a real public.__truncated__ table."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, patch

    from shared.source_introspection import profile_table

    conn = type(
        "C",
        (),
        {
            "connection_type": "postgresql",
            "encrypted_credentials": b"x",
            "config": {},
            "project_id": None,
        },
    )()

    async def _fake_profile(*_a, **_k):
        return ([{"column_name": "id", "data_type": "int", "is_nullable": False}], 1)

    @asynccontextmanager
    async def _noop_audit(*_a, **_k):
        yield

    with (
        patch("shared.source_introspection._decrypt", return_value={"host": "h"}),
        patch(
            "shared.source_introspection.assert_introspection_endpoint_allowed",
            new_callable=AsyncMock,
        ),
        patch(
            "shared.source_introspection._PROFILE_DISPATCH",
            {"postgresql": _fake_profile},
        ),
        patch(
            "shared.source_introspection._stamp_primary_keys",
            new_callable=AsyncMock,
            return_value=[{"column_name": "id", "data_type": "int", "is_nullable": False}],
        ),
        patch("shared.source_introspection._audited_introspection", _noop_audit),
    ):
        columns, row_count = await profile_table(
            conn, schema="public", table="__truncated__",
        )
    assert row_count == 1
    assert columns[0]["column_name"] == "id"


def test_normalize_discover_payload_dict_passthrough():
    payload = normalize_discover_payload(
        {"tables": [{"schema": "s", "table": "t", "type": "BASE TABLE"}], "truncated": False}
    )
    assert payload["truncated"] is False
    assert len(payload["tables"]) == 1


def test_bq_profile_sql_is_approx_and_tables_metadata():
    sql, _, leading = _build_cardinality_sql(
        "bigquery", "analytics", "events",
        [{"column_name": "user_id", "data_type": "string", "approx_distinct": None}],
    )
    assert leading is False
    assert "APPROX_COUNT_DISTINCT" in sql
    assert "COUNT(DISTINCT" not in sql
    assert "COUNT(*)" not in sql
    meta = _bq_tables_row_count_sql("proj", "analytics")
    assert "__TABLES__" in meta
    assert "@table_id" in meta
