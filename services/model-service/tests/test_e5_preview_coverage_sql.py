"""E5 enhancement SQL-builder coverage.

F-016-24: cross-table hierarchy preview joins the child level table to the
parent level table on the model's defined join, instead of bailing with
``cross_table_preview_not_supported``.

F-016-23: calendar coverage validation builds MIN/MAX range probes for the
fact and calendar tables and compares them.

Both builders emit canonical PostgreSQL transpiled to the source dialect; these
tests assert the SQL shape and dialect quoting without touching a live source.
"""
from __future__ import annotations

import pytest

from src.api.hierarchies import (
    _build_ancestor_path_sample_sql,
    _build_cross_table_sample_sql,
    _build_estimate_sql,
    _build_sample_sql,
)
from src.api.calendar import _build_minmax_sql

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# F-016-24 — cross-table sample SQL
# ---------------------------------------------------------------------------


def test_cross_table_sample_joins_parent_and_filters():
    sql = _build_cross_table_sample_sql(
        "postgresql",
        child_table="products",
        child_key_expr='"t"."product_name"',
        child_join_col="category_id",
        parent_table="categories",
        parent_key_expr='"p"."category_name"',
        parent_join_col="id",
        parent_key="Beverages",
        sample_size=50,
    )
    # Joins child (t) to parent (p) on the model join columns.
    assert " AS t " in sql
    assert " AS p " in sql
    assert "categories" in sql
    assert "products" in sql
    assert 't."category_id" = p."id"' in sql
    # Filters to the requested parent member, literal produced by sqlglot.
    assert "'Beverages'" in sql
    assert "DISTINCT" in sql
    assert "LIMIT 50" in sql


def test_cross_table_sample_escapes_parent_key_literal():
    # An apostrophe in the parent key must be library-escaped, never spliced.
    sql = _build_cross_table_sample_sql(
        "postgresql",
        child_table="products",
        child_key_expr='"t"."product_name"',
        child_join_col="category_id",
        parent_table="categories",
        parent_key_expr='"p"."category_name"',
        parent_join_col="id",
        parent_key="O'Brien",
        sample_size=10,
    )
    assert "'O''Brien'" in sql


def test_cross_table_sample_bigquery_backticks():
    # Key expressions are resolved canonical-PG (double-quoted) at build time —
    # the same convention the preview loop uses — then transpiled to the source
    # dialect by transpile_preview_sql. BigQuery must emit backticks.
    sql = _build_cross_table_sample_sql(
        "bigquery",
        child_table="ds.products",
        child_key_expr='"t"."product_name"',
        child_join_col="category_id",
        parent_table="ds.categories",
        parent_key_expr='"p"."category_name"',
        parent_join_col="id",
        parent_key="Beverages",
        sample_size=20,
    )
    # BigQuery uses backticks, not double quotes, for identifiers.
    assert "`" in sql
    assert '"products"' not in sql


# ---------------------------------------------------------------------------
# Bug-5424 — RLS predicate injection in preview SQL
# ---------------------------------------------------------------------------


def test_estimate_sql_injects_rls_where():
    """_build_estimate_sql must include the RLS predicate in its WHERE."""
    sql = _build_estimate_sql(
        "postgresql",
        table_name="dim_region",
        key_expr='"t"."region_code"',
        sample_size=100,
        rls_where='"customer_region" = \'US\'',
    )
    assert '"customer_region" = \'US\'' in sql
    assert "IS NOT NULL" in sql


def test_estimate_sql_no_rls_when_none():
    """_build_estimate_sql must not add RLS clause when rls_where is None."""
    sql = _build_estimate_sql(
        "postgresql",
        table_name="dim_region",
        key_expr='"t"."region_code"',
        sample_size=100,
        rls_where=None,
    )
    assert "customer_region" not in sql


def test_sample_sql_injects_rls_where():
    """_build_sample_sql must include the RLS predicate in its WHERE."""
    sql = _build_sample_sql(
        "postgresql",
        table_name="dim_region",
        key_expr='"t"."region_code"',
        sample_size=100,
        rls_where='"customer_region" IN (\'US\', \'EU\')',
    )
    assert '"customer_region" IN' in sql
    assert "IS NOT NULL" in sql


def test_sample_sql_no_rls_when_none():
    """_build_sample_sql must not add RLS clause when rls_where is None."""
    sql = _build_sample_sql(
        "postgresql",
        table_name="dim_region",
        key_expr='"t"."region_code"',
        sample_size=100,
        rls_where=None,
    )
    assert "customer_region" not in sql


def test_cross_table_sample_injects_rls_where():
    """_build_cross_table_sample_sql must include the RLS predicate."""
    sql = _build_cross_table_sample_sql(
        "postgresql",
        child_table="products",
        child_key_expr='"t"."product_name"',
        child_join_col="category_id",
        parent_table="categories",
        parent_key_expr='"p"."category_name"',
        parent_join_col="id",
        parent_key="Beverages",
        sample_size=50,
        rls_where='"region" = \'US\'',
    )
    assert '"region" = \'US\'' in sql
    assert " AS t " in sql
    assert " AS p " in sql


def test_cross_table_sample_no_rls_when_none():
    """_build_cross_table_sample_sql must not add RLS when rls_where is None."""
    sql = _build_cross_table_sample_sql(
        "postgresql",
        child_table="products",
        child_key_expr='"t"."product_name"',
        child_join_col="category_id",
        parent_table="categories",
        parent_key_expr='"p"."category_name"',
        parent_join_col="id",
        parent_key="Beverages",
        sample_size=50,
        rls_where=None,
    )
    assert "region" not in sql.lower()


# ---------------------------------------------------------------------------
# F-016-23 — calendar coverage MIN/MAX SQL
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


# ---------------------------------------------------------------------------
# Bug-3617 (Phase 0.5a) — member key/caption separation in preview SQL
# ---------------------------------------------------------------------------


def test_sample_sql_selects_caption_when_present():
    """With a caption_expr, the sample selects caption_value alongside the key."""
    sql = _build_sample_sql(
        "postgresql",
        table_name="public.dim_account",
        key_expr='"t"."account_type_code"',
        sample_size=100,
        caption_expr='"t"."account_type_name"',
    )
    assert "key_value" in sql
    assert "caption_value" in sql
    assert "account_type_code" in sql
    assert "account_type_name" in sql
    assert "DISTINCT" in sql


def test_sample_sql_no_caption_is_single_column():
    """Without a caption_expr the query is the legacy single-column shape."""
    sql = _build_sample_sql(
        "postgresql",
        table_name="public.dim_account",
        key_expr='"t"."account_type_code"',
        sample_size=100,
    )
    assert "key_value" in sql
    assert "caption_value" not in sql


def test_cross_table_sample_selects_caption_when_present():
    """Cross-table sample also carries the caption when supplied."""
    sql = _build_cross_table_sample_sql(
        "postgresql",
        child_table="products",
        child_key_expr='"t"."product_code"',
        child_join_col="category_id",
        parent_table="categories",
        parent_key_expr='"p"."category_name"',
        parent_join_col="id",
        parent_key="Beverages",
        sample_size=50,
        caption_expr='"t"."product_name"',
    )
    assert "key_value" in sql
    assert "caption_value" in sql
    assert "product_name" in sql


def test_cross_table_sample_no_caption_is_single_key_column():
    sql = _build_cross_table_sample_sql(
        "postgresql",
        child_table="products",
        child_key_expr='"t"."product_code"',
        child_join_col="category_id",
        parent_table="categories",
        parent_key_expr='"p"."category_name"',
        parent_join_col="id",
        parent_key="Beverages",
        sample_size=50,
    )
    assert "key_value" in sql
    assert "caption_value" not in sql


# ---------------------------------------------------------------------------
# Bug-3617 (Phase 0.5b) — ancestor key-path sample SQL
# ---------------------------------------------------------------------------


def test_ancestor_path_sample_selects_all_level_keys():
    """Each level key is aliased key_0..key_n; all are NOT NULL filtered + ordered."""
    sql = _build_ancestor_path_sample_sql(
        "postgresql",
        table_name="public.fact_sales",
        key_exprs=[
            'EXTRACT(YEAR FROM "t"."order_date")',
            'EXTRACT(MONTH FROM "t"."order_date")',
        ],
        sample_size=200,
    )
    assert "AS key_0" in sql
    assert "AS key_1" in sql
    assert "DISTINCT" in sql
    # Both keys NOT NULL filtered.
    assert sql.upper().count("IS NOT NULL") == 2
    # Ordered by both key columns (ancestor-first) so the path is deterministic.
    assert "ORDER BY 1, 2" in sql
    assert "LIMIT 200" in sql


def test_ancestor_path_sample_includes_caption_and_rls():
    sql = _build_ancestor_path_sample_sql(
        "postgresql",
        table_name="public.fact_sales",
        key_exprs=['"t"."year_key"', '"t"."month_key"'],
        sample_size=50,
        rls_where='"t"."region" = \'EMEA\'',
        caption_expr='"t"."month_name"',
    )
    assert "caption_value" in sql
    assert "month_name" in sql
    assert "EMEA" in sql


def test_ancestor_path_sample_bigquery_backticks():
    sql = _build_ancestor_path_sample_sql(
        "bigquery",
        table_name="ds.fact_sales",
        key_exprs=['"t"."year_key"', '"t"."month_key"'],
        sample_size=20,
    )
    assert "`" in sql
    assert '"fact_sales"' not in sql
