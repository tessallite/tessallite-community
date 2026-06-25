"""Named-set resolution on a BigQuery source — regression guard.

Goal (live-observed 2026-06-24 on the TPC-DS BigQuery model): a Top-N and a
filtered named set must resolve to the correct members and correctly filter a
query when the model's SOURCE is BigQuery.

Why this is a *unit* regression (no live BigQuery required): the named-set
consumer path is a pure producer of PostgreSQL-canonical SQL. The contract is:

  1. ``shared.named_list_compiler.compile_definition`` turns a structured
     builder_definition (dynamic_top_n / filtered) into an MDX set expression.
  2. A BI tool inlines that expression on an MDX axis; the gateway's
     ``_mdx_to_sql`` translates the TopCount / Filter MDX into PostgreSQL-
     canonical SQL (identifiers quoted via ``connector_qualify`` with the
     ``postgresql`` dialect — the canonical interchange form).
  3. The query-router is the *single* place that re-quotes that canonical SQL
     for the source dialect. For BigQuery that means backticks, via the same
     ``shared.connector_qualify`` / sqlglot round-trip used by
     ``query-router/src/rewrite/dialects._requote_identifiers_for_dialect``.

This test composes the real production functions across those three layers and
asserts the BigQuery-native result: backtick quoting (never PostgreSQL double
quotes reaching BigQuery), correct LIMIT / ORDER BY for the Top-N, and correct
HAVING for the filtered set. It would fail if any layer regressed to emitting
double-quoted identifiers or a per-connector branch, or if the compiler stopped
producing the TopCount / Filter shape the gateway translator recognises.

Live validation that this unit test deliberately does NOT cover (documented for
the post-deploy gateway step): executing the transpiled SQL against a real
BigQuery dataset through the JDBC/XMLA gateway and confirming the returned
member list matches the deployed named set. That requires a live BigQuery
source and a deployed model and must be run as a post-deploy gateway check.
"""
from __future__ import annotations

import re

import pytest
import sqlglot

from shared.connector_qualify import quote_identifier
from shared.named_list_compiler import compile_definition
from src.dax.xmla_server import _mdx_to_sql


# A model whose SOURCE is BigQuery. The metadata shape mirrors what the gateway
# loads from the model-service for the live TPC-DS model.
_MEASURES = [{"name": "net_sales", "default_agg": "sum"}]
_DIMS = [{"name": "item_category"}]


def _to_bigquery(canonical_sql: str) -> str:
    """Apply the same PG->BigQuery transpilation the query-router applies.

    Mirrors ``_requote_identifiers_for_dialect(sql, "bigquery")``: parse as
    PostgreSQL and re-emit as BigQuery so double-quoted identifiers become
    backtick-quoted and dialect syntax is normalised. This is the only place
    connector-native quoting is introduced, exactly as in production.
    """
    return sqlglot.parse_one(canonical_sql, read="postgres").sql(dialect="bigquery")


# ---------------------------------------------------------------------------
# Top-N named set: top 5 item_category by net_sales
# ---------------------------------------------------------------------------


def test_top_n_named_set_compiles_to_recognised_mdx():
    """The structured builder must compile to the TopCount MDX shape the
    gateway translator recognises (entity.Members, count, [Measures].[m])."""
    builder = {
        "type": "dynamic_top_n",
        "entity": "item_category",
        "count": 5,
        "measure": "net_sales",
        "direction": "top",
    }
    mdx_set = compile_definition(builder)
    assert mdx_set == "TopCount([item_category].Members, 5, [Measures].[net_sales])"


def test_top_n_named_set_resolves_correctly_on_bigquery():
    """End-to-end: compiled Top-N MDX -> gateway canonical SQL -> BigQuery SQL.

    Asserts the BigQuery result ranks by the aggregated measure, keeps the
    LIMIT, and quotes every identifier with backticks (never double quotes).
    """
    builder = {
        "type": "dynamic_top_n",
        "entity": "item_category",
        "count": 5,
        "measure": "net_sales",
        "direction": "top",
    }
    mdx_set = compile_definition(builder)
    mdx = (
        f"SELECT {{[Measures].[net_sales]}} ON COLUMNS, "
        f"{mdx_set} ON ROWS FROM [tpcds]"
    )

    canonical_sql, protocol = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="tpcds")
    assert protocol == "jdbc"

    bq_sql = _to_bigquery(canonical_sql)

    # BigQuery identifier quoting — backticks, never PostgreSQL double quotes.
    assert '"' not in bq_sql, f"double-quoted identifier reached BigQuery: {bq_sql}"
    assert f"`{quote_identifier('bigquery', 'item_category')[1:-1]}`" in bq_sql
    assert "`item_category`" in bq_sql
    assert "`net_sales`" in bq_sql

    # Top-N membership: rank by the aggregated measure DESC and keep 5.
    upper = bq_sql.upper()
    assert "ORDER BY" in upper
    assert "DESC" in upper
    assert "LIMIT 5" in upper
    assert "SUM(`net_sales`)" in bq_sql
    assert "GROUP BY `item_category`" in bq_sql


def test_bottom_n_named_set_resolves_ascending_on_bigquery():
    builder = {
        "type": "dynamic_top_n",
        "entity": "item_category",
        "count": 3,
        "measure": "net_sales",
        "direction": "bottom",
    }
    mdx_set = compile_definition(builder)
    assert mdx_set.startswith("BottomCount(")
    mdx = (
        f"SELECT {{[Measures].[net_sales]}} ON COLUMNS, "
        f"{mdx_set} ON ROWS FROM [tpcds]"
    )
    bq_sql = _to_bigquery(_mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="tpcds")[0])
    upper = bq_sql.upper()
    assert '"' not in bq_sql
    assert "ASC" in upper
    assert "LIMIT 3" in upper


# ---------------------------------------------------------------------------
# Filtered named set: item_category where net_sales > 1000000
# ---------------------------------------------------------------------------


def test_filtered_named_set_compiles_to_recognised_mdx():
    builder = {
        "type": "filtered",
        "entity": "item_category",
        "conditions": [
            {"field": "net_sales", "operator": ">", "value": 1000000},
        ],
        "logic": "AND",
    }
    mdx_set = compile_definition(builder)
    assert mdx_set == (
        "Filter([item_category].Members, [Measures].[net_sales] > 1000000)"
    )


def test_filtered_named_set_resolves_correctly_on_bigquery():
    """End-to-end: compiled Filter MDX -> gateway canonical SQL -> BigQuery SQL.

    The MDX ``Filter`` over a measure threshold is an aggregate test per member,
    so the resolved SQL must apply the test in HAVING over a GROUP BY of the
    entity, quoted for BigQuery.
    """
    builder = {
        "type": "filtered",
        "entity": "item_category",
        "conditions": [
            {"field": "net_sales", "operator": ">", "value": 1000000},
        ],
        "logic": "AND",
    }
    mdx_set = compile_definition(builder)
    mdx = (
        f"SELECT {{[Measures].[net_sales]}} ON COLUMNS, "
        f"{mdx_set} ON ROWS FROM [tpcds]"
    )

    canonical_sql, protocol = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="tpcds")
    assert protocol == "jdbc"

    bq_sql = _to_bigquery(canonical_sql)

    assert '"' not in bq_sql, f"double-quoted identifier reached BigQuery: {bq_sql}"
    assert "`item_category`" in bq_sql
    assert "GROUP BY `item_category`" in bq_sql

    upper = bq_sql.upper()
    assert "HAVING" in upper
    # Aggregate-threshold membership, not a row-level WHERE.
    assert re.search(r"SUM\(`net_sales`\)\s*>\s*1000000", bq_sql), bq_sql
    assert " WHERE " not in upper


def test_multi_condition_filtered_named_set_resolves_all_clauses_on_bigquery():
    """A filtered named set with two conditions (the compiler joins them with a
    single AND) must render BOTH aggregated HAVING clauses on BigQuery — not
    silently drop the second, which would over-include members."""
    builder = {
        "type": "filtered",
        "entity": "item_category",
        "conditions": [
            {"field": "net_sales", "operator": ">", "value": 1000000},
            {"field": "net_sales", "operator": "<", "value": 9000000},
        ],
        "logic": "AND",
    }
    mdx = (
        f"SELECT {{[Measures].[net_sales]}} ON COLUMNS, "
        f"{compile_definition(builder)} ON ROWS FROM [tpcds]"
    )
    canonical_sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="tpcds")
    bq_sql = _to_bigquery(canonical_sql)
    upper = bq_sql.upper()
    assert "HAVING" in upper
    assert upper.count(" AND ") >= 1
    assert re.search(r"SUM\(`net_sales`\)\s*>\s*1000000", bq_sql), bq_sql
    assert re.search(r"SUM\(`net_sales`\)\s*<\s*9000000", bq_sql), bq_sql
    assert '"' not in bq_sql


def test_mixed_numeric_string_filter_fails_loud_not_silent():
    """A filter mixing a numeric measure threshold with a string-valued measure
    comparison cannot be expressed as an aggregate HAVING; `_mdx_to_sql` must
    fail loud rather than silently drop the string clause (over-including
    members), per the module's fail-loud policy."""
    mdx = (
        'SELECT {[Measures].[net_sales]} ON COLUMNS, '
        'Filter([item_category].Members, '
        '[Measures].[net_sales] > 100 AND [Measures].[net_sales] = "A") '
        'ON ROWS FROM [tpcds]'
    )
    with pytest.raises(ValueError, match="Filter"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="tpcds")


def test_filtered_named_set_having_is_aggregated_in_canonical_sql():
    """Root-cause guard (not BigQuery-specific): the canonical PostgreSQL SQL
    the gateway emits for a filtered named set must wrap the measure in its
    aggregate inside HAVING. A bare column (``HAVING "net_sales" > value``) is
    rejected by PostgreSQL because the column is neither grouped nor aggregated,
    so the named set would fail to resolve on *any* source, not just BigQuery."""
    builder = {
        "type": "filtered",
        "entity": "item_category",
        "conditions": [
            {"field": "net_sales", "operator": ">", "value": 1000000},
        ],
        "logic": "AND",
    }
    mdx = (
        f"SELECT {{[Measures].[net_sales]}} ON COLUMNS, "
        f"{compile_definition(builder)} ON ROWS FROM [tpcds]"
    )
    canonical_sql, _ = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="tpcds")
    assert 'HAVING SUM("net_sales") > 1000000' in canonical_sql, canonical_sql
    # The canonical statement must parse and re-emit cleanly as PostgreSQL.
    reparsed = sqlglot.parse_one(canonical_sql, read="postgres").sql(dialect="postgres")
    assert "HAVING" in reparsed.upper()


def test_top_n_named_set_table_ref_is_backtick_quoted_on_bigquery():
    """The fact table reference must also be backtick-quoted for BigQuery so
    the FROM clause is valid GoogleSQL (no double-quoted table name)."""
    builder = {
        "type": "dynamic_top_n",
        "entity": "item_category",
        "count": 5,
        "measure": "net_sales",
        "direction": "top",
    }
    mdx = (
        f"SELECT {{[Measures].[net_sales]}} ON COLUMNS, "
        f"{compile_definition(builder)} ON ROWS FROM [tpcds]"
    )
    bq_sql = _to_bigquery(_mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="tpcds")[0])
    assert "FROM `tpcds`" in bq_sql
    assert '"' not in bq_sql
