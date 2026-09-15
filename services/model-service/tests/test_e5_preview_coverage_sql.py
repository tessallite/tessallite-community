"""E5 enhancement SQL-builder coverage -- calendar coverage probe.

F-016-23: calendar coverage validation builds MIN/MAX range probes for the fact
and calendar tables and compares them. The builder emits canonical PostgreSQL
transpiled to the source dialect; these tests assert the SQL shape and dialect
quoting without touching a live source.

The hierarchy-preview builders this file also covered (F-016-24 cross-table
sample, the Bug-5424 RLS splice, the Bug-3617 caption and ancestor-path samples)
no longer exist: Bug-9895 re-expressed the preview as a projection over the
persona model query, so it builds no physical-table SQL of its own and the
router owns both dialect and row security. Their BEHAVIOUR is guarded through
the route in ``test_bug9895_preview_over_persona_model_query.py`` and
``test_bug9871_preview_ancestor_bound.py``.
"""
from __future__ import annotations

import pytest

from src.api.calendar import _build_minmax_sql

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# F-016-23 -- calendar coverage MIN/MAX SQL
# ---------------------------------------------------------------------------


def test_minmax_sql_postgres_shape():
    sql = _build_minmax_sql("postgresql", table_name="public.orders", date_col="order_date")
    assert "MIN(" in sql.upper()
    assert "MAX(" in sql.upper()
    assert "order_date" in sql
    assert "orders" in sql
    assert "AS lo" in sql or "lo" in sql


def test_minmax_sql_bigquery_backticks():
    sql = _build_minmax_sql("bigquery", table_name="ds.orders", date_col="order_date")
    assert "`" in sql
    assert '"orders"' not in sql
