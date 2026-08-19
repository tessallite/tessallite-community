"""Bug-7153 / Bug-5918 / F-014-05: source schema listing via connection-introspect.

After Bug-7153, ``list_schemas`` routes through the query-router's
connection-introspect ``discover-tables`` endpoint and extracts unique schema
names. The payload may be a list (legacy) or ``{tables, truncated}``.
"""
from __future__ import annotations

import pytest

from src.api.connections import tables_from_discover_payload


def test_schemas_extracted_from_discover_tables_response():
    tables = [
        {"schema": "public", "table": "orders", "type": "BASE TABLE"},
        {"schema": "public", "table": "customers", "type": "BASE TABLE"},
        {"schema": "sales", "table": "invoices", "type": "BASE TABLE"},
        {"schema": "analytics", "table": "reports", "type": "VIEW"},
    ]
    schemas = sorted({
        t["schema"] for t in tables_from_discover_payload(tables) if "schema" in t
    })
    assert schemas == ["analytics", "public", "sales"]


def test_schemas_extracted_from_structured_payload():
    payload = {
        "tables": [
            {"schema": "public", "table": "orders", "type": "BASE TABLE"},
            {"schema": "sales", "table": "invoices", "type": "BASE TABLE"},
        ],
        "truncated": True,
    }
    schemas = sorted({
        t["schema"] for t in tables_from_discover_payload(payload) if "schema" in t
    })
    assert schemas == ["public", "sales"]


def test_schemas_empty_for_empty_response():
    tables = []
    schemas = sorted({
        t["schema"] for t in tables_from_discover_payload(tables) if "schema" in t
    })
    assert schemas == []


def test_schemas_single_schema():
    tables = [
        {"schema": "default", "table": "t1", "type": "BASE TABLE"},
        {"schema": "default", "table": "t2", "type": "BASE TABLE"},
    ]
    schemas = sorted({
        t["schema"] for t in tables_from_discover_payload(tables) if "schema" in t
    })
    assert schemas == ["default"]


def test_build_schema_listing_sql_removed():
    """Bug-7153: the hand-built SQL function must not exist in sources.py."""
    with pytest.raises(ImportError):
        from src.api.sources import _build_schema_listing_sql  # noqa: F401
