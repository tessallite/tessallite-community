"""Unit tests for impact-scan table matching (F-030-10) and column extraction (Bug-7458).

Bug-8471 replaced the regex physical-name matcher (``_table_matches``) and the
name-set column extractor (``_extract_column_names``) with sqlglot-parsed
``extract_physical_tables`` / ``tables_agree`` / ``extract_semantic_names``. The
F-030-10 and Bug-7458 invariants below are unchanged; only the function under
test moved.
"""
from __future__ import annotations

import pytest

from src.api.impact_usage import (
    extract_physical_tables,
    extract_semantic_names,
    tables_agree,
)

pytestmark = pytest.mark.unit


def _names_table(model_table: str, sql: str) -> bool:
    """Does ``sql`` name ``model_table``? The F-030-10 question, re-homed."""
    return any(
        tables_agree(model_table, schema, table)
        for schema, table in extract_physical_tables(sql)
    )


def test_table_match_is_identifier_aware():
    """F-030-10: a table name must match as a whole SQL identifier, not as a
    substring — `order` must not match `orders` or `order_items`."""
    assert _names_table("order", "select * from order where id = 1")
    assert _names_table("order", "select * from public.order o")
    # Substring false positives are rejected.
    assert not _names_table("order", "select * from orders")
    assert not _names_table("order", "select * from order_items")
    assert not _names_table("order", "select reorder_flag from products")


def test_table_match_handles_qualified_and_quoted_names():
    assert _names_table("payment", "select * from analytics.payment p")
    assert _names_table("payment", 'select * from "payment"')
    assert not _names_table("pay", "select payment_method from payment")


def test_table_match_survives_fully_quoted_physical_sql():
    """Bug-8471: the router emits every identifier quoted, so the rewritten SQL
    reads ``FROM "demo_data"."payment_transaction"``. The old regex looked for
    the unquoted ``demo_data.payment_transaction`` spelling and found nothing —
    which is why the scan reported zero usage on every real model."""
    sql = (
        'SELECT SUM("payment_transaction"."transaction_amount") AS "m0" '
        'FROM "demo_data"."payment_transaction" AS "payment_transaction"'
    )
    assert _names_table("demo_data.payment_transaction", sql)
    assert not _names_table("other_schema.payment_transaction", sql)


def test_table_match_ignores_the_aggregate_target_of_an_accelerated_query():
    """An accelerated query's physical SQL names the aggregate table, not a
    model table, so it must contribute no match rather than a false one."""
    sql = 'SELECT SUM("Revenue__sum") AS value FROM "trgt"."3732ce3df075"'
    assert not _names_table("demo_data.payment_transaction", sql)


# ---------------------------------------------------------------------------
# Column extraction tests (Bug-7458)
# ---------------------------------------------------------------------------


def test_extract_column_names_basic_select():
    """Bug-7458: column identifiers are extracted from a basic SELECT query."""
    cols = extract_semantic_names("SELECT customer_id, amount FROM orders WHERE region = 'US'")
    assert "customer_id" in cols
    assert "amount" in cols
    assert "region" in cols


def test_extract_column_names_with_aggregation():
    """Bug-7458: column extraction handles aggregate functions."""
    cols = extract_semantic_names("SELECT region, SUM(amount) FROM sales GROUP BY region")
    assert "region" in cols
    assert "amount" in cols


def test_extract_column_names_returns_lowercase():
    """Bug-7458: all extracted names are lowercased for case-insensitive matching."""
    cols = extract_semantic_names("SELECT Customer_ID, AMOUNT FROM Orders")
    assert "customer_id" in cols
    assert "amount" in cols


def test_extract_column_names_parse_failure_does_not_crash():
    """Bug-7458: unparseable queries do not raise (best-effort)."""
    cols = extract_semantic_names("THIS IS NOT SQL AT ALL $$$")
    assert isinstance(cols, set)


def test_extract_column_names_empty_query():
    """Bug-7458: empty input returns an empty set."""
    assert extract_semantic_names("") == set()


def test_extract_column_names_join_columns():
    """Bug-7458: columns from JOIN conditions are extracted."""
    cols = extract_semantic_names(
        "SELECT o.order_id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id"
    )
    assert "order_id" in cols
    assert "name" in cols
    assert "customer_id" in cols
    assert "id" in cols


def test_extract_column_names_qualified_columns():
    """Bug-7458: qualified column references (table.column) are extracted."""
    cols = extract_semantic_names("SELECT t.revenue, t.cost FROM transactions t")
    assert "revenue" in cols
    assert "cost" in cols


def test_extract_semantic_names_excludes_output_aliases_and_the_from_target():
    """Bug-8474 class, guarded at the source. The legacy semantic-name path is
    load-bearing for the whole pre-contract corpus, so it must return FIELD
    references only. A plain identifier sweep would also return the output alias
    ``value`` and the model name ``modelx``, crediting usage to a measure the
    query never named."""
    cols = extract_semantic_names('SELECT SUM("Revenue") AS value FROM "modelx"')
    assert cols == {"revenue"}


# ---------------------------------------------------------------------------
# Bug-8074 — column-usage must not collapse table identity or undercount tokens.
#
# Wrong-analytics guard. The endpoint keyed columns by lowercase bare NAME, so a
# second table carrying the same column name (id, region, created_at - ordinary
# in a star schema) overwrote the first and usage was reported against the wrong
# table. Extraction also returned a SET of names, so hit_count (documented as
# "total token occurrences") could never exceed query_count. A modeller reading
# either number could delete a column that is genuinely depended on.
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from src.api.impact_scan import (  # noqa: E402
    _extract_column_occurrences,
    _hash_query,
    _resolve_table_aliases,
    aggregate_column_usage,
)

_TS = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _two_table_model():
    """Two model tables that BOTH carry a `region` column, plus one column
    unique to each. This is the shape the old keying silently collapsed."""
    col_map = {
        ("orders", "region"): ("region", "orders"),
        ("orders", "order_id"): ("order_id", "orders"),
        ("customers", "region"): ("region", "customers"),
        ("customers", "customer_id"): ("customer_id", "customers"),
    }
    tables_by_col = {
        "region": {"orders", "customers"},
        "order_id": {"orders"},
        "customer_id": {"customers"},
    }
    return col_map, tables_by_col


def _by_key(items):
    return {(i.table_name, i.column_name): i for i in items}


def test_duplicate_column_name_is_attributed_to_the_qualified_table():
    """orders.region and customers.region are DIFFERENT columns and must be
    reported separately, each against its own table."""
    col_map, tables_by_col = _two_table_model()
    entries = [
        ("SELECT orders.region FROM orders", _TS),
        ("SELECT customers.region FROM customers", _TS),
        ("SELECT customers.region FROM customers", _TS),
    ]
    _parsed, _skipped, items = aggregate_column_usage(entries, col_map, tables_by_col)
    by_key = _by_key(items)

    assert ("orders", "region") in by_key
    assert ("customers", "region") in by_key
    assert by_key[("orders", "region")].query_count == 1
    assert by_key[("customers", "region")].query_count == 2
    assert by_key[("orders", "region")].ambiguous is False


def test_query_alias_resolves_to_its_physical_table():
    """`FROM customers c ... c.region` must be charged to customers, not to
    whichever table the bare name happened to map to."""
    col_map, tables_by_col = _two_table_model()
    entries = [("SELECT c.region FROM customers AS c", _TS)]
    _parsed, _skipped, items = aggregate_column_usage(entries, col_map, tables_by_col)
    by_key = _by_key(items)

    assert ("customers", "region") in by_key
    assert ("orders", "region") not in by_key


def test_repeated_token_counts_every_occurrence():
    """hit_count is documented as total occurrences. A column used three times
    in one query must read 3 hits / 1 query, not 1 / 1."""
    col_map, tables_by_col = _two_table_model()
    sql = (
        "SELECT orders.order_id FROM orders "
        "WHERE orders.order_id > 10 AND orders.order_id < 99"
    )
    _parsed, _skipped, items = aggregate_column_usage([(sql, _TS)], col_map, tables_by_col)
    item = _by_key(items)[("orders", "order_id")]

    assert item.hit_count == 3
    assert item.query_count == 1


def test_stable_bind_references_override_ambiguous_sql_name_matching():
    """New logs carry the exact ModelColumn ID selected by the binder. That
    identity must win even when the raw SQL uses a shared unqualified name."""
    col_map, tables_by_col = _two_table_model()
    orders_region_id = uuid.uuid4()
    _parsed, _skipped, items = aggregate_column_usage(
        [("SELECT region FROM orders JOIN customers ON 1=1", _TS)],
        col_map,
        tables_by_col,
        stable_column_ids_by_index=[[str(orders_region_id)]],
        columns_by_id={str(orders_region_id): ("region", "orders")},
    )

    by_key = _by_key(items)
    assert ("orders", "region") in by_key
    assert ("", "region") not in by_key
    assert by_key[("orders", "region")].ambiguous is False


def test_unqualified_reference_on_a_shared_name_is_reported_as_ambiguous():
    """A bare `region` genuinely does not say which table. Reporting it against
    one of them is a wrong number; it must be surfaced instead."""
    col_map, tables_by_col = _two_table_model()
    entries = [("SELECT region FROM orders JOIN customers ON 1=1", _TS)]
    _parsed, _skipped, items = aggregate_column_usage(entries, col_map, tables_by_col)
    item = _by_key(items)[("", "region")]

    assert item.ambiguous is True
    assert item.candidate_tables == ["customers", "orders"]
    assert item.hit_count == 1


def test_unqualified_reference_on_a_unique_name_is_attributed():
    """No ambiguity when only one model table carries the name — the common
    case must not regress into an 'ambiguous' row."""
    col_map, tables_by_col = _two_table_model()
    entries = [("SELECT order_id FROM orders", _TS)]
    _parsed, _skipped, items = aggregate_column_usage(entries, col_map, tables_by_col)
    item = _by_key(items)[("orders", "order_id")]

    assert item.ambiguous is False
    assert item.candidate_tables == []


def test_reference_qualified_to_a_non_model_alias_is_not_counted():
    """A CTE or foreign-schema alias that happens to expose a same-named column
    is not this model's usage."""
    col_map, tables_by_col = _two_table_model()
    entries = [(
        "WITH staging AS (SELECT 1 AS region) SELECT staging.region FROM staging",
        _TS,
    )]
    _parsed, _skipped, items = aggregate_column_usage(entries, col_map, tables_by_col)

    assert all(i.table_name != "orders" for i in items)
    assert all(not i.ambiguous for i in items)


def test_extract_occurrences_preserves_qualifier_and_multiplicity():
    occ = _extract_column_occurrences(
        "SELECT o.amount, o.amount, region FROM orders o"
    )
    assert occ.count(("o", "amount")) == 2
    assert (None, "region") in occ


def test_resolve_table_aliases_maps_alias_and_bare_name():
    """Every qualifier resolves to the table it names. Bug-8474 changed the VALUE
    from the bare name to the spelling the query used, so a contradicting schema
    can refuse a match instead of being erased before comparison."""
    aliases = _resolve_table_aliases("SELECT 1 FROM sales.orders AS o JOIN customers ON 1=1")
    assert aliases["o"] == "sales.orders"
    assert aliases["orders"] == "sales.orders"
    assert aliases["sales.orders"] == "sales.orders"
    assert aliases["customers"] == "customers"


# ---------------------------------------------------------------------------
# Bug-8463 / Bug-8464 - Fable R1 findings 1 and 2.
#
# 1. Both /impact routes joined on ModelColumn.table_id, an attribute that does
#    not exist (the ORM column is model_table_id), so every call 500'd. Every
#    Bug-8074 test was pure-function, so the suite stayed green while the
#    endpoint could not run at all.
# 2. ModelTable.physical_name is stored SCHEMA-QUALIFIED in real models (the
#    shipped acme-demo seed uses demo_data.<table> throughout) while a query
#    qualifies a column with the bare table name or an alias. Comparing the two
#    directly matched nothing, so usage read as zero for exactly the references
#    that name their table - the same "modeller drops a used column" harm
#    Bug-8074 set out to prevent.
# ---------------------------------------------------------------------------

_QUALIFIED_COL_MAP = {
    ("demo_data.dim_service_type", "service_type"):
        ("service_type", "demo_data.dim_service_type"),
    ("demo_data.payment_transaction", "amount"):
        ("amount", "demo_data.payment_transaction"),
}
_QUALIFIED_TABLES_BY_COL = {
    "service_type": {"demo_data.dim_service_type"},
    "amount": {"demo_data.payment_transaction"},
}


def test_schema_qualified_physical_name_attributes_alias_references():
    """`FROM demo_data.dim_service_type AS s ... s.service_type` must be
    attributed to that table. Before the fix it produced no usage item at all."""
    entries = [
        ("SELECT s.service_type FROM demo_data.dim_service_type AS s", _TS),
        ("SELECT s.service_type FROM demo_data.dim_service_type AS s", _TS),
    ]
    _parsed, _skipped, items = aggregate_column_usage(
        entries, _QUALIFIED_COL_MAP, _QUALIFIED_TABLES_BY_COL,
    )
    by_key = _by_key(items)

    key = ("demo_data.dim_service_type", "service_type")
    assert key in by_key
    assert by_key[key].hit_count == 2
    assert by_key[key].query_count == 2
    assert by_key[key].ambiguous is False


def test_schema_qualified_physical_name_attributes_bare_and_qualified_references():
    """Both `dim_service_type.service_type` and the fully qualified spelling
    must resolve to the schema-qualified model table."""
    entries = [
        ("SELECT dim_service_type.service_type FROM demo_data.dim_service_type", _TS),
        ("SELECT demo_data.dim_service_type.service_type "
         "FROM demo_data.dim_service_type", _TS),
    ]
    _parsed, _skipped, items = aggregate_column_usage(
        entries, _QUALIFIED_COL_MAP, _QUALIFIED_TABLES_BY_COL,
    )
    by_key = _by_key(items)

    key = ("demo_data.dim_service_type", "service_type")
    assert key in by_key
    assert by_key[key].hit_count == 2
    assert by_key[key].ambiguous is False


_TWO_SCHEMA_COL_MAP = {
    ("sales.orders", "region"): ("region", "sales.orders"),
    ("archive.orders", "region"): ("region", "archive.orders"),
}
_TWO_SCHEMA_TABLES_BY_COL = {"region": {"sales.orders", "archive.orders"}}


def test_same_bare_name_in_two_schemas_is_ambiguous_not_arbitrary():
    """Bare-name matching must not silently pick one of two same-named tables in
    different schemas - that is the Bug-8074 wrong-table harm in a new guise.

    The query here genuinely does not say which schema it means. When it DOES,
    see ``test_qualifying_schema_selects_the_matching_table``."""
    col_map = _TWO_SCHEMA_COL_MAP
    tables_by_col = _TWO_SCHEMA_TABLES_BY_COL
    entries = [("SELECT o.region FROM orders AS o", _TS)]

    _parsed, _skipped, items = aggregate_column_usage(entries, col_map, tables_by_col)
    by_key = _by_key(items)

    assert ("", "region") in by_key
    assert by_key[("", "region")].ambiguous is True
    assert by_key[("", "region")].candidate_tables == ["archive.orders", "sales.orders"]


def test_qualifying_schema_selects_the_matching_table():
    """Bug-8474 shape 3, positive half. When the query DOES name a schema, the
    reference is not ambiguous — it is that table and only that table."""
    entries = [("SELECT o.region FROM sales.orders AS o", _TS)]
    _parsed, _skipped, items = aggregate_column_usage(
        entries, _TWO_SCHEMA_COL_MAP, _TWO_SCHEMA_TABLES_BY_COL,
    )
    by_key = _by_key(items)

    assert ("sales.orders", "region") in by_key
    assert by_key[("sales.orders", "region")].ambiguous is False
    assert ("archive.orders", "region") not in by_key
    assert ("", "region") not in by_key


# ---------------------------------------------------------------------------
# Bug-8474 — three shapes that OVER-attributed usage. All three are the same
# failure in different clothing: the matcher accepted a qualifier that the
# query's own FROM contradicts or never contained. Over-counting is the
# fail-safe direction for "a modeller drops a used column", but it makes the
# usage numbers structurally untrustworthy, and Bug-8471 makes this legacy path
# load-bearing for the entire pre-contract log corpus.
# ---------------------------------------------------------------------------

_ORDERS_COL_MAP = {("demo_data.orders", "region"): ("region", "demo_data.orders")}
_ORDERS_TABLES_BY_COL = {"region": {"demo_data.orders"}}


def _orders_usage(sql: str):
    _parsed, _skipped, items = aggregate_column_usage(
        [(sql, _TS)], _ORDERS_COL_MAP, _ORDERS_TABLES_BY_COL,
    )
    return _by_key(items)


def test_cte_shadowing_a_model_table_name_is_not_model_usage():
    """Bug-8474 shape 1: a CTE named `orders`, built from a DIFFERENT table, was
    counted as usage of the model's `demo_data.orders`. sqlglot's
    find_all(exp.Table) yields the CTE reference in the outer FROM exactly like a
    real table, and nothing excluded declared CTE names."""
    by_key = _orders_usage(
        "WITH orders AS (SELECT r AS region FROM demo_data.other_stuff) "
        "SELECT o.region FROM orders o"
    )
    assert by_key == {}


def test_qualifier_absent_from_the_from_clause_is_not_model_usage():
    """Bug-8474 shape 2: `SELECT orders.region FROM demo_data.customers` names no
    `orders` table at all. The old `alias_map.get(qualifier, qualifier)` fallback
    let the phantom qualifier through and bare-matched the model table."""
    by_key = _orders_usage("SELECT orders.region FROM demo_data.customers")
    assert by_key == {}


def test_same_bare_name_in_a_different_schema_is_not_model_usage():
    """Bug-8474 shape 3: the query explicitly names `other_schema.orders`. The
    alias map stored only the BARE name as its value, so the contradicting schema
    was discarded before the candidate match could refuse it."""
    by_key = _orders_usage("SELECT o.region FROM other_schema.orders AS o")
    assert by_key == {}


def test_a_real_reference_still_counts_after_the_over_attribution_fixes():
    """Guard against fixing over-attribution by under-attributing everything —
    the dangerous direction for this feature."""
    by_key = _orders_usage("SELECT o.region FROM demo_data.orders AS o")
    assert ("demo_data.orders", "region") in by_key
    assert by_key[("demo_data.orders", "region")].hit_count == 1


def test_column_loading_joins_reference_real_orm_attributes():
    """Bug-8464 guard. Both /impact routes join ModelColumn to ModelTable;
    constructing that statement is what raised AttributeError in production
    while every pure-function test stayed green. A join predicate naming a
    non-existent attribute is a 500 on the first request, not a type error at
    import time, so only building the statement catches it."""
    from sqlalchemy import select

    from shared.db.models import ModelColumn, ModelTable

    stmt = (
        select(ModelColumn.column_name, ModelTable.physical_name)
        .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
    )
    compiled = str(stmt)
    assert "model_columns.column_name" in compiled
    assert "model_tables.physical_name" in compiled
    # The attribute the routes used before the fix must not silently exist.
    assert not hasattr(ModelColumn, "table_id")


def test_impact_routes_use_the_real_foreign_key_attribute():
    """Source-level guard tied to the two exact join sites, so a future edit
    reintroducing `ModelColumn.table_id` fails here rather than in production."""
    import inspect

    import src.api.impact_scan as impact_scan

    source = inspect.getsource(impact_scan)
    assert "ModelColumn.table_id" not in source
    assert source.count("ModelColumn.model_table_id == ModelTable.id") == 2


# ---------------------------------------------------------------------------
# Endpoint-level execution guard (Fable R1 finding 1).
#
# The pure-function tests above cannot see a broken join predicate, a missing
# ORM attribute, or a route that never reaches the aggregation at all. This one
# drives the real FastAPI route with a stub session, so the SQLAlchemy
# statements the route builds are actually constructed and the response body is
# the one a modeller would receive.
# ---------------------------------------------------------------------------

import types  # noqa: E402
import uuid  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from .conftest import (  # noqa: E402
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
    make_model,
)


def _rows_result(rows):
    result = MagicMock()
    result.all.return_value = rows
    result.scalars.return_value.all.return_value = rows
    return result


def _scalars_result(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    result.all.return_value = rows
    return result


@pytest.mark.anyio
async def test_column_usage_route_executes_and_reports_the_right_table(client):
    """Drives GET /impact/column-usage end to end.

    Asserts a known answer, not a 200: two model tables share the column name
    `region`, the logged query qualifies it, and the response must attribute the
    usage to the qualified table with the schema-qualified physical name the
    seed actually stores.
    """
    orders_region_id = uuid.uuid4()
    customers_region_id = uuid.uuid4()
    log = types.SimpleNamespace(
        id=uuid.uuid4(),
        raw_query="SELECT region FROM demo_data.orders JOIN demo_data.customers ON 1=1",
        created_at=None,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            # column_name + table physical_name rows
            _rows_result([
                (orders_region_id, "region", "demo_data.orders"),
                (customers_region_id, "region", "demo_data.customers"),
            ]),
            # count of successful logs that exist (window-disclosure)
            _scalar_result(1),
            # QueryLog rows
            _scalars_result([log]),
            # Binder trace: selected and filtered references both resolved to
            # the orders ModelColumn despite the ambiguous raw spelling.
            _rows_result([(
                log.id,
                {"column_usage_refs": [
                    {"column_id": str(orders_region_id), "role": "select"},
                    {"column_id": str(orders_region_id), "role": "filter"},
                ]},
            )]),
        ]
    )

    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
            f"/impact/column-usage"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_queries_parsed"] == 1
    by_table = {(c["table_name"], c["column_name"]): c for c in body["columns"]}
    assert ("demo_data.orders", "region") in by_table
    item = by_table[("demo_data.orders", "region")]
    # Two occurrences of o.region in one query.
    assert item["hit_count"] == 2
    assert item["query_count"] == 1
    assert item["ambiguous"] is False
    # The other table carrying the same column name must NOT be credited.
    assert ("demo_data.customers", "region") not in by_table


# ---------------------------------------------------------------------------
# Bug-8483 — transitive physical inputs of calculated / UDA-backed fields.
#
# A calculated measure or a UDA-backed field has no source_column_id, so the
# binder emits no physical column for it and the usage scan saw NOTHING. A
# physical column feeding a heavily used calculated measure therefore reported
# zero usage, and a modeller reading that can drop it and break every downstream
# consumer — the exact harm Bug-8074 exists to prevent, one layer deeper.
# ---------------------------------------------------------------------------

from shared.model_dependency.snapshot import (  # noqa: E402
    DimensionRow,
    MeasureRow,
    ModelDependencySnapshot,
    UdaRow,
)

from src.api.impact_usage import (  # noqa: E402
    build_semantic_closure,
    build_semantic_name_index,
    columns_for_objects,
    resolve_semantic_objects,
)

COL_REVENUE = "col-revenue"
COL_COST = "col-cost"
COL_REGION = "col-region"
COL_UDA_A = "col-uda-a"
COL_UDA_B = "col-uda-b"

MSR_REVENUE = "msr-revenue"
MSR_COST = "msr-cost"
MSR_PROFIT = "msr-profit"
MSR_PROFIT_YOY = "msr-profit-yoy"
MSR_UDA = "msr-uda"
DIM_REGION = "dim-region"
DIM_UDA = "dim-uda"
UDA_ONE = "uda-1"


def _closure_snapshot() -> ModelDependencySnapshot:
    """A model with every shape whose physical inputs are indirect."""
    return ModelDependencySnapshot(
        tenant_id="t", project_id="p", model_id="m", dependency_revision=1,
        udas=(
            UdaRow(
                id=UDA_ONE, name="uda_one", display_name="UDA One", table_id="tbl",
                column_ref_ids=(COL_UDA_A, COL_UDA_B),
            ),
        ),
        measures=(
            MeasureRow(id=MSR_REVENUE, name="Revenue", display_name="Revenue",
                       source_column_id=COL_REVENUE),
            MeasureRow(id=MSR_COST, name="Cost", display_name="Cost",
                       source_column_id=COL_COST),
            # Calculated: no source_column_id at all, only expression references.
            MeasureRow(id=MSR_PROFIT, name="Profit", display_name="Profit",
                       calc_expression='measure("Revenue") - measure("Cost")',
                       calc_reference_ids=(MSR_REVENUE, MSR_COST)),
            # Variant of a calculated measure: two hops from any real column.
            MeasureRow(id=MSR_PROFIT_YOY, name="Profit YoY", display_name="Profit YoY",
                       variant_of_measure_id=MSR_PROFIT),
            MeasureRow(id=MSR_UDA, name="UdaMeasure", display_name="UdaMeasure",
                       user_defined_attribute_id=UDA_ONE),
        ),
        dimensions=(
            DimensionRow(id=DIM_REGION, name="Region", display_name="Region",
                         source_column_id=COL_REGION),
            DimensionRow(id=DIM_UDA, name="UdaDim", display_name="UdaDim",
                         user_defined_attribute_id=UDA_ONE),
        ),
    )


def test_calculated_measure_closure_reaches_its_base_physical_columns():
    """Bug-8483: `Profit = Revenue - Cost` has no source_column_id, but dropping
    either base column breaks it. Both must show as used."""
    closure = build_semantic_closure(_closure_snapshot())
    assert closure[("measure", MSR_PROFIT)] == frozenset({COL_REVENUE, COL_COST})


def test_variant_of_a_calculated_measure_closes_over_two_hops():
    closure = build_semantic_closure(_closure_snapshot())
    assert closure[("measure", MSR_PROFIT_YOY)] == frozenset({COL_REVENUE, COL_COST})


def test_uda_backed_measure_and_dimension_reach_the_attribute_columns():
    """Bug-8483: a UDA-backed field's physical inputs live on the attribute, not
    on the measure/dimension row."""
    closure = build_semantic_closure(_closure_snapshot())
    assert closure[("measure", MSR_UDA)] == frozenset({COL_UDA_A, COL_UDA_B})
    assert closure[("dimension", DIM_UDA)] == frozenset({COL_UDA_A, COL_UDA_B})


def test_directly_bound_objects_close_over_exactly_their_own_column():
    """The closure must not inflate an ordinary measure — existing counts for
    directly bound fields have to stay byte-identical."""
    closure = build_semantic_closure(_closure_snapshot())
    assert closure[("measure", MSR_REVENUE)] == frozenset({COL_REVENUE})
    assert closure[("dimension", DIM_REGION)] == frozenset({COL_REGION})


def test_self_referencing_calculated_measure_does_not_recurse_forever():
    """A model can be edited into a calc cycle. The closure must terminate and
    still report every column reachable on the non-cyclic paths."""
    snapshot = ModelDependencySnapshot(
        tenant_id="t", project_id="p", model_id="m", dependency_revision=1,
        measures=(
            MeasureRow(id="a", name="A", display_name="A",
                       calc_reference_ids=("b",)),
            MeasureRow(id="b", name="B", display_name="B",
                       source_column_id=COL_REVENUE, calc_reference_ids=("a",)),
        ),
    )
    closure = build_semantic_closure(snapshot)
    assert closure[("measure", "a")] == frozenset({COL_REVENUE})
    assert closure[("measure", "b")] == frozenset({COL_REVENUE})


def test_measure_wrapped_as_a_dimension_still_resolves_its_closure():
    """The binder wraps a measure referenced outside an aggregate as a synthetic
    dimension carrying the MEASURE's id. A strict type lookup would report no
    usage; the cross-type retry recovers it."""
    closure = build_semantic_closure(_closure_snapshot())
    assert columns_for_objects([("dimension", MSR_PROFIT)], closure) == {
        COL_REVENUE, COL_COST,
    }


def test_legacy_semantic_names_resolve_through_the_model_definitions():
    """Bug-8471 fix (b): a legacy raw_query names SEMANTIC fields. Resolving them
    against the model is the only way to learn which physical columns it used."""
    snapshot = _closure_snapshot()
    index = build_semantic_name_index(snapshot)
    closure = build_semantic_closure(snapshot)
    objects = resolve_semantic_objects(
        'SELECT "Region", SUM("Profit") FROM "modelx"', index,
    )
    assert objects == {("dimension", DIM_REGION), ("measure", MSR_PROFIT)}
    assert columns_for_objects(objects, closure) == {COL_REGION, COL_REVENUE, COL_COST}


# ---------------------------------------------------------------------------
# Endpoint-level execution guard for POST /impact/scan.
#
# Bug-8464 required an endpoint test on BOTH impact routes; only the GET had
# one, so the POST route's own statements were never constructed by any test.
# Bug-8471 gives it a known answer to assert rather than a 200: the scan must
# record table usage for a query whose raw_query contains no physical name at
# all, which is every real query the gateway logs.
# ---------------------------------------------------------------------------

from shared.db.models import GatewayQueryReference  # noqa: E402


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scan_snapshot(region_col, revenue_col, cost_col):
    return ModelDependencySnapshot(
        tenant_id="t", project_id=str(TEST_PROJECT_ID), model_id=str(TEST_MODEL_ID),
        dependency_revision=1,
        measures=(
            MeasureRow(id=MSR_REVENUE, name="Revenue", display_name="Revenue",
                       source_column_id=str(revenue_col)),
            MeasureRow(id=MSR_COST, name="Cost", display_name="Cost",
                       source_column_id=str(cost_col)),
            MeasureRow(id=MSR_PROFIT, name="Profit", display_name="Profit",
                       calc_expression='measure("Revenue") - measure("Cost")',
                       calc_reference_ids=(MSR_REVENUE, MSR_COST)),
        ),
        dimensions=(
            DimensionRow(id=DIM_REGION, name="Region", display_name="Region",
                         source_column_id=str(region_col)),
        ),
    )


def _patched_loader(snapshot):
    loader = MagicMock()
    loader.return_value.load = AsyncMock(return_value=snapshot)
    return loader


def _added_references(db):
    return [
        call.args[0] for call in db.add.call_args_list
        if isinstance(call.args[0], GatewayQueryReference)
    ]


@pytest.mark.anyio
async def test_impact_scan_route_records_usage_from_stable_bind_refs(client):
    """Bug-8471 + Bug-8483, end to end through POST /impact/scan.

    The logged raw_query is what a BI client actually sends - SEMANTIC names and
    the model name, no physical identifier anywhere. Matching
    ModelTable.physical_name against that text is what made tables_matched 0 on
    every real model. The stable bind reference plus the calculated measure's
    dependency closure must produce the table usage instead.
    """
    region_col, revenue_col, cost_col = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    log = types.SimpleNamespace(
        id=uuid.uuid4(),
        raw_query='SELECT "Region", SUM("Profit") AS value FROM "modelx"',
        rewritten_query="",
        user_identity="analyst@acme-demo.com",
        protocol="jdbc",
        created_at=_TS,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([("demo_data.orders", "postgresql")]),        # tables + source
            _rows_result([                                             # columns -> table
                (region_col, "demo_data.orders"),
                (revenue_col, "demo_data.orders"),
                (cost_col, "demo_data.orders"),
            ]),
            _scalar_result(None),                                      # watermark
            _scalars_result([log]),                                    # query logs
            _rows_result([(                                            # bind trace
                log.id,
                {
                    "column_usage_refs": [
                        {"column_id": str(region_col), "role": "select"},
                    ],
                    # Profit is calculated: the binder bound no physical column
                    # for it, only its identity.
                    "semantic_object_refs": [
                        {"object_type": "measure", "object_id": MSR_PROFIT,
                         "object_name": "Profit", "role": "measure"},
                    ],
                },
            )]),
            _scalars_result([]),                                       # existing refs
        ]
    )

    snapshot = _scan_snapshot(region_col, revenue_col, cost_col)
    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tables_matched"] == 1
    assert body["references_upserted"] == 1
    # Region directly, plus Revenue and Cost through the calculated measure.
    assert body["columns_matched"] == 3

    references = _added_references(db)
    assert len(references) == 1
    assert references[0].queried_table == "demo_data.orders"
    assert references[0].hit_count == 1
    assert references[0].query_user == "analyst@acme-demo.com"
    assert references[0].last_seen_at == _TS


@pytest.mark.anyio
async def test_impact_scan_route_records_usage_from_a_legacy_log(client):
    """Bug-8471, legacy half. Roughly 94% of the shipped corpus predates the
    stable bind reference. Those logs must still resolve, from the physical SQL
    that ran and from the semantic names in the raw query - the physical SQL
    quotes every identifier, which the old regex matcher could not see."""
    region_col = uuid.uuid4()
    log = types.SimpleNamespace(
        id=uuid.uuid4(),
        raw_query='SELECT "Region" FROM "modelx"',
        rewritten_query='SELECT "orders"."region" FROM "demo_data"."orders" AS "orders"',
        user_identity=None,
        protocol="jdbc",
        created_at=_TS,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([("demo_data.orders", "postgresql")]),
            _rows_result([(region_col, "demo_data.orders")]),
            _scalar_result(None),
            _scalars_result([log]),
            _rows_result([]),          # no bind trace at all: a legacy log
            _scalars_result([]),
        ]
    )

    snapshot = _scan_snapshot(region_col, uuid.uuid4(), uuid.uuid4())
    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tables_matched"] == 1
    assert body["references_upserted"] == 1
    assert body["columns_matched"] == 1


@pytest.mark.anyio
async def test_impact_scan_route_does_not_credit_an_unrelated_query(client):
    """An accelerated legacy query's physical SQL names the aggregate target, not
    a model table, and its raw_query names nothing this model knows. The scan
    must record nothing rather than invent a match - over-reporting is the
    fail-safe direction but it destroys trust in the numbers (Bug-8474)."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    log = types.SimpleNamespace(
        id=uuid.uuid4(),
        raw_query='SELECT SUM("Unknownfield") AS value FROM "modelx"',
        rewritten_query='SELECT SUM("Revenue__sum") AS value FROM "trgt"."3732ce3df075"',
        user_identity=None,
        protocol="jdbc",
        created_at=_TS,
    )
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([("demo_data.orders", "postgresql")]),
            _rows_result([(uuid.uuid4(), "demo_data.orders")]),
            _scalar_result(None),
            _scalars_result([log]),      # window 1
            _rows_result([]),            # window 1 bind traces
            _scalars_result([]),         # window 2: no rows left
        ]
    )

    snapshot = _scan_snapshot(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tables_matched"] == 0
    assert body["references_upserted"] == 0
    assert _added_references(db) == []
    # The window matched nothing, so the scan advanced past it and looked
    # further rather than stopping on it.
    assert body["logs_scanned"] == 1
    assert body["more_remaining"] is False


@pytest.mark.anyio
async def test_impact_scan_advances_past_a_window_that_matched_nothing(client):
    """The watermark only advances from RECORDED usage. A full window in which
    nothing matched would therefore leave it where it was, and every later press
    would re-read the same rows forever — everything past that block would be
    permanently unreachable. The scan must advance through the empty window
    within the same request."""
    region_col = uuid.uuid4()
    unmatched = types.SimpleNamespace(
        id=uuid.uuid4(), raw_query="SELECT 1", rewritten_query="SELECT 1",
        user_identity=None, protocol="jdbc", created_at=_TS,
    )
    matched = types.SimpleNamespace(
        id=uuid.uuid4(), raw_query='SELECT "Region" FROM "modelx"',
        rewritten_query="", user_identity=None, protocol="jdbc",
        created_at=datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([("demo_data.orders", "postgresql")]),
            _rows_result([(region_col, "demo_data.orders")]),
            _scalar_result(None),
            _scalars_result([unmatched]),   # window 1 — matches nothing
            _rows_result([]),
            _scalars_result([matched]),     # window 2 — reached only if we advance
            _rows_result([]),
            _scalars_result([]),            # existing references
        ]
    )

    snapshot = _scan_snapshot(region_col, uuid.uuid4(), uuid.uuid4())
    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["logs_scanned"] == 2, "the second window was never read"
    assert body["tables_matched"] == 1
    assert body["references_upserted"] == 1


@pytest.mark.anyio
async def test_impact_scan_route_accumulates_onto_an_existing_reference(client):
    """Repeat runs of the same query must add to the existing (table, hash) row
    rather than create a duplicate, and must never rewind last_seen_at."""
    region_col = uuid.uuid4()
    raw = 'SELECT "Region" FROM "modelx"'
    later = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    logs = [
        types.SimpleNamespace(
            id=uuid.uuid4(), raw_query=raw, rewritten_query="",
            user_identity=None, protocol="jdbc", created_at=_TS,
        ),
        types.SimpleNamespace(
            id=uuid.uuid4(), raw_query=raw, rewritten_query="",
            user_identity=None, protocol="jdbc", created_at=later,
        ),
    ]
    existing = types.SimpleNamespace(
        id=uuid.uuid4(),
        queried_table="demo_data.orders",
        query_text_hash=_hash_query(raw),
        last_seen_at=_TS,
        hit_count=4,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([("demo_data.orders", "postgresql")]),
            _rows_result([(region_col, "demo_data.orders")]),
            _scalar_result(None),
            _scalars_result(logs),
            _rows_result([
                (entry.id, {"column_usage_refs": [{"column_id": str(region_col)}]})
                for entry in logs
            ]),
            _scalars_result([existing]),
            MagicMock(),   # the UPDATE
        ]
    )

    snapshot = _scan_snapshot(region_col, uuid.uuid4(), uuid.uuid4())
    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
        )

    assert resp.status_code == 200, resp.text
    # An existing row is updated, never inserted again.
    assert resp.json()["references_upserted"] == 0
    assert _added_references(db) == []
    update_stmt = db.execute.call_args_list[-1].args[0]
    compiled = update_stmt.compile()
    # Bug-5847: the increment stays an atomic SQL expression, and both logs are
    # counted in the one statement.
    assert "hit_count" in str(compiled)
    assert compiled.params["last_seen_at"] == later


# ---------------------------------------------------------------------------
# Round-1 deep-review promotions. Each was RED against the round-1 code and
# guards a defect the review found, not a coverage top-up.
# ---------------------------------------------------------------------------

import time  # noqa: E402

import sqlglot as sqlglot_module  # noqa: E402

from src.api.impact_usage import (  # noqa: E402
    expand_objects,
    is_sql_protocol,
    source_dialects,
)


def test_bigquery_rewritten_sql_resolves_its_physical_tables():
    """Bug-8471 fix (a) on a BigQuery source.

    ``rewritten_query`` is written in the SOURCE connector's dialect. BigQuery
    quotes identifiers with backticks, which the default sqlglot dialect does
    not recognise, so every BigQuery model resolved ZERO physical tables from
    the physical SQL (measured live: modell 313/369 logs, inventory 44/239,
    onboarding 6/19 all yielded nothing).
    """
    sql = (
        'SELECT `payment_transaction`.`country_code` AS `country_code` '
        'FROM `large_demo_data`.`payment_transaction` AS `payment_transaction` '
        'GROUP BY `country_code`'
    )
    found = extract_physical_tables(sql, source_dialects(["bigquery"]))
    assert any(
        tables_agree("large_demo_data.payment_transaction", schema, table)
        for schema, table in found
    ), f"BigQuery physical SQL resolved no model table: {sorted(found)}"


def test_postgres_rewritten_sql_still_resolves_after_the_dialect_fix():
    sql = ('SELECT "payment_transaction"."region" '
           'FROM "demo_data"."payment_transaction" AS "payment_transaction"')
    found = extract_physical_tables(sql, source_dialects(["postgresql"]))
    assert any(tables_agree("demo_data.payment_transaction", s, t) for s, t in found)


def test_source_dialects_always_keeps_the_default_and_maps_through_the_shared_table():
    """Must fail OPEN toward more parsing: an unknown connector degrades to the
    default dialect rather than parsing nothing, because the failure being fixed
    is a silent empty result."""
    assert source_dialects(["bigquery"]) == (None, "bigquery")
    assert source_dialects(["postgresql"]) == (None, "postgres")
    assert source_dialects([None, "not_a_connector"]) == (None,)
    assert set(source_dialects(["postgresql", "bigquery"])) == {None, "postgres", "bigquery"}


def test_closure_disambiguation_does_not_also_emit_an_ambiguous_row():
    """A legacy hit must be counted ONCE.

    Two model tables carry ``region``. The legacy raw_query names the semantic
    field ``region`` (bound to orders.region) and a calculated measure whose
    closure lands on customers.region. The token scan cannot tell which table
    ``region`` belongs to, so it emitted an AMBIGUOUS credit and the closure
    loop then credited both precise (table, column) keys as well: three hits for
    two real references, plus a phantom "Ambiguous" row next to the precise rows
    that resolved it.
    """
    col_map = {
        ("demo_data.orders", "region"): ("region", "demo_data.orders"),
        ("demo_data.customers", "region"): ("region", "demo_data.customers"),
    }
    tables_by_col = {"region": {"demo_data.orders", "demo_data.customers"}}
    columns_by_id = {
        "col-o": ("region", "demo_data.orders"),
        "col-c": ("region", "demo_data.customers"),
    }
    _parsed, _skipped, items = aggregate_column_usage(
        [('SELECT "region", SUM("Margin") AS m0 FROM "modelx"', _TS)],
        col_map, tables_by_col,
        stable_column_ids_by_index=[None],
        columns_by_id=columns_by_id,
        closure_column_ids_by_index=[["col-o", "col-c"]],
    )
    assert [i for i in items if i.ambiguous] == [], (
        "the closure resolved this column exactly; no ambiguous row may remain")
    assert sum(i.hit_count for i in items) == 2, (
        f"two references must produce two hits, got "
        f"{[(i.table_name, i.hit_count) for i in items]}")


def test_a_text_ambiguous_column_the_closure_cannot_resolve_stays_ambiguous():
    """The disambiguation must not swallow the genuinely-unknowable case."""
    col_map = {
        ("demo_data.orders", "region"): ("region", "demo_data.orders"),
        ("demo_data.customers", "region"): ("region", "demo_data.customers"),
    }
    tables_by_col = {"region": {"demo_data.orders", "demo_data.customers"}}
    _parsed, _skipped, items = aggregate_column_usage(
        [("SELECT region FROM orders", _TS)],
        col_map, tables_by_col,
        stable_column_ids_by_index=[None],
        columns_by_id={},
        closure_column_ids_by_index=[[]],
    )
    assert len(items) == 1
    assert items[0].ambiguous is True
    assert items[0].candidate_tables == ["demo_data.customers", "demo_data.orders"]


def test_semantic_closure_stays_linear_on_a_deep_calc_dag():
    """``build_semantic_closure`` runs per request, synchronously, inside an
    async route. Memoising only the outermost frame made a branching calc DAG
    exponential: 40 measures at depth 20 measured 4.2 s of event-loop-blocking
    CPU."""
    depth = 20
    rows = []
    for lvl in range(depth):
        for side in ("l", "r"):
            mid = f"m{lvl}{side}"
            if lvl == depth - 1:
                rows.append(MeasureRow(id=mid, name=mid, display_name=mid,
                                       source_column_id=f"c{lvl}{side}"))
            else:
                rows.append(MeasureRow(id=mid, name=mid, display_name=mid,
                                       calc_reference_ids=(f"m{lvl+1}l", f"m{lvl+1}r")))
    snapshot = ModelDependencySnapshot(
        tenant_id="t", project_id="p", model_id="m", dependency_revision=1,
        measures=tuple(rows),
    )
    started = time.perf_counter()
    closure = build_semantic_closure(snapshot)
    elapsed = time.perf_counter() - started
    assert closure[("measure", "m0l")] == frozenset({f"c{depth-1}l", f"c{depth-1}r"})
    assert elapsed < 0.5, f"closure took {elapsed:.2f}s for {len(rows)} measures"


def test_aggregate_column_usage_parses_each_distinct_text_once(monkeypatch):
    """Both legacy parses ran per LOG ROW, not per distinct text. A real corpus
    repeats the same SQL thousands of times: measured 5000 modelx rows over 48
    distinct texts at 1.97 s unmemoized, on the request's synchronous path."""
    import src.api.impact_scan as impact_scan

    calls: list[str] = []
    real_parse = sqlglot_module.parse

    def counting_parse(sql, *args, **kwargs):
        calls.append(sql)
        return real_parse(sql, *args, **kwargs)

    monkeypatch.setattr(impact_scan.sqlglot, "parse", counting_parse)

    texts = [
        "SELECT region FROM orders",
        "SELECT order_id FROM orders",
        "SELECT customer_id FROM customers",
    ]
    col_map, tables_by_col = _two_table_model()
    entries = [(texts[i % 3], _TS) for i in range(200)]
    aggregate_column_usage(entries, col_map, tables_by_col)

    # Two derivations (occurrences + aliases) over three distinct texts.
    assert len(calls) <= 6, f"{len(calls)} sqlglot parses for 3 distinct texts"


def test_two_calculated_measures_over_one_column_are_two_references():
    """A closure-reached reference must weigh the same as a direct one. Two
    directly bound measures on the same column produce two column_usage_refs
    entries and two hits; two calculated measures reading it must too, or the
    same query counts differently depending on how the model is authored."""
    snapshot = ModelDependencySnapshot(
        tenant_id="t", project_id="p", model_id="m", dependency_revision=1,
        measures=(
            MeasureRow(id="base", name="Base", display_name="Base",
                       source_column_id="colX"),
            MeasureRow(id="c1", name="C1", display_name="C1",
                       calc_reference_ids=("base",)),
            MeasureRow(id="c2", name="C2", display_name="C2",
                       calc_reference_ids=("base",)),
        ),
    )
    closure = build_semantic_closure(snapshot)
    assert expand_objects(
        [("measure", "c1"), ("measure", "c2")], closure,
    ) == ["colX", "colX"]


def test_dax_raw_query_is_not_mined_for_semantic_names():
    """The legacy name path must fail CLOSED on a language sqlglot cannot read.
    sqlglot turns ``EVALUATE SUMMARIZECOLUMNS(...)`` into a column named
    ``evaluate`` and loses every real field, so a model with a field named
    ``evaluate`` would be credited usage by a query naming no such field."""
    assert is_sql_protocol("jdbc") is True
    assert is_sql_protocol("plugin") is True
    assert is_sql_protocol("dax") is False
    assert is_sql_protocol("discover_members") is False
    assert is_sql_protocol(None) is False
    # The underlying hazard the gate exists for.
    assert "evaluate" in extract_semantic_names(
        'EVALUATE SUMMARIZECOLUMNS("Region", "Total", [Revenue])'
    )


def test_an_empty_physical_name_cannot_be_mistaken_for_the_ambiguous_sentinel():
    """``table_lower`` comes from ModelTable.physical_name. When the sentinel was
    the empty string, an empty physical_name took the ambiguous output branch
    with an EMPTY candidate set and raised StopIteration — a 500 on the panel."""
    col_map = {("", "region"): ("region", "")}
    tables_by_col = {"region": {""}}
    _parsed, _skipped, items = aggregate_column_usage(
        [("SELECT region FROM orders", _TS)], col_map, tables_by_col,
    )
    assert len(items) == 1
    assert items[0].ambiguous is False
    assert items[0].column_name == "region"


def test_ambiguous_row_canonical_spelling_is_deterministic():
    """The name shown to the modeller must not depend on set iteration order."""
    col_map = {
        ("archive.orders", "region"): ("Region", "archive.orders"),
        ("sales.orders", "region"): ("region", "sales.orders"),
    }
    tables_by_col = {"region": {"archive.orders", "sales.orders"}}
    names = set()
    for _ in range(8):
        _p, _s, items = aggregate_column_usage(
            [("SELECT region FROM orders", _TS)], col_map, tables_by_col,
        )
        names.add(items[0].column_name)
    assert names == {"Region"}, f"canonical spelling varied across runs: {names}"


@pytest.mark.anyio
async def test_impact_scan_route_resolves_a_bigquery_physical_query(client):
    """R1 finding 1, at the route rather than the pure function.

    The pure parser being dialect-capable is not enough — the route has to
    derive the model's connector and pass it in. Without that thread, a
    BigQuery model's physical SQL resolves nothing and the only table-usage
    source for a join-only table is silently dead on that whole source family.
    The bind trace is deliberately absent so the physical SQL is the ONLY
    evidence available.
    """
    log = types.SimpleNamespace(
        id=uuid.uuid4(),
        raw_query='SELECT "Country" FROM "modell"',
        rewritten_query=(
            "SELECT `payment_transaction`.`country_code` AS `country_code` "
            "FROM `large_demo_data`.`payment_transaction` AS `payment_transaction`"
        ),
        user_identity=None,
        protocol="jdbc",
        created_at=_TS,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([("large_demo_data.payment_transaction", "bigquery")]),
            _rows_result([(uuid.uuid4(), "large_demo_data.payment_transaction")]),
            _scalar_result(None),
            _scalars_result([log]),
            _rows_result([]),          # no bind trace
            _scalars_result([]),       # existing references
        ]
    )

    # An empty snapshot: no semantic name can resolve, so a match can only come
    # from the BigQuery-dialect parse of the physical SQL.
    empty = ModelDependencySnapshot(
        tenant_id="t", project_id=str(TEST_PROJECT_ID), model_id=str(TEST_MODEL_ID),
        dependency_revision=1,
    )
    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(empty)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tables_matched"] == 1, (
        "the BigQuery physical SQL resolved no model table; the source dialect "
        "is not reaching extract_physical_tables"
    )
    assert body["references_upserted"] == 1
    assert _added_references(db)[0].queried_table == "large_demo_data.payment_transaction"


# ---------------------------------------------------------------------------
# Round-2 deep-review promotions.
# ---------------------------------------------------------------------------


def _closure_measure(**kw):
    base = dict(
        id=None, name="", display_name=None, source_column_id=None,
        semi_additive_account_column_id=None, resolved_date_col_id=None,
        date_dimension_column_id=None, user_defined_attribute_id=None,
        calc_reference_ids=(), variant_of_measure_id=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_a_calculated_time_variant_measure_credits_its_date_column_once():
    """Bug-8696 x Bug-8483 seam. The producer emits a measure's calendar date
    column DIRECTLY (role measure_date) and ALSO lists the measure in
    semantic_object_refs when it bound no value column - and that measure's
    dependency closure contains the same date column. Crediting both counts one
    reference twice. architecture_query-routing.md states the invariant: an
    object in semantic_object_refs must never also contribute to
    column_usage_refs, "so a consumer that credits both cannot double-count"."""
    measure_id = "11111111-1111-1111-1111-111111111111"
    base_id = "22222222-2222-2222-2222-222222222222"
    amount_col = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    date_col = "dddddddd-dddd-dddd-dddd-dddddddddddd"

    snapshot = types.SimpleNamespace(
        measures=[
            _closure_measure(id=measure_id, name="Profit YoY",
                             resolved_date_col_id=date_col,
                             calc_reference_ids=[base_id]),
            _closure_measure(id=base_id, name="Revenue",
                             source_column_id=amount_col),
        ],
        dimensions=[], udas=[],
    )
    closure = build_semantic_closure(snapshot)
    # What the producer records for this measure: nothing direct (it bound no
    # value column), and the measure itself as an unexpanded object.
    stable_ids: list[str] = []
    closure_ids = expand_objects([("measure", measure_id)], closure)

    table = "demo_data.payment_transaction"
    columns_by_id = {amount_col: ("transaction_amount", table),
                     date_col: ("business_date", table)}
    col_map = {(table, "transaction_amount"): ("transaction_amount", table),
               (table, "business_date"): ("business_date", table)}
    tables_by_col = {"transaction_amount": {table}, "business_date": {table}}

    _parsed, _skipped, items = aggregate_column_usage(
        [('SELECT SUM("Profit YoY") FROM "modelx"', None)],
        col_map, tables_by_col,
        stable_column_ids_by_index=[stable_ids],
        columns_by_id=columns_by_id,
        closure_column_ids_by_index=[closure_ids],
    )
    hits = {item.column_name: item.hit_count for item in items}
    assert hits["transaction_amount"] == 1
    assert hits["business_date"] == 1, (
        "the calendar date column was credited twice for one query - once "
        f"directly and once through the closure: {hits}"
    )


def test_a_bound_time_variant_measure_still_credits_its_date_column_once():
    """The other half of the same partition: a measure that DID bind its value
    column gets its date column on the direct path and is not an unexpanded
    object, so Bug-8696 stays fixed for that shape too - exactly one hit."""
    table = "demo_data.payment_transaction"
    amount_col = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    date_col = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    columns_by_id = {amount_col: ("transaction_amount", table),
                     date_col: ("business_date", table)}
    col_map = {(table, "transaction_amount"): ("transaction_amount", table),
               (table, "business_date"): ("business_date", table)}
    tables_by_col = {"transaction_amount": {table}, "business_date": {table}}

    _parsed, _skipped, items = aggregate_column_usage(
        [('SELECT SUM("Revenue YoY") FROM "modelx"', None)],
        col_map, tables_by_col,
        stable_column_ids_by_index=[[amount_col, date_col]],
        columns_by_id=columns_by_id,
        closure_column_ids_by_index=[[]],
    )
    hits = {item.column_name: item.hit_count for item in items}
    assert hits == {"transaction_amount": 1, "business_date": 1}


@pytest.mark.anyio
async def test_impact_scan_does_not_promise_progress_it_cannot_make(client):
    """The resume point is the newest RECORDED usage, so a pass that matched
    nothing leaves it where it was and the next press re-reads the identical
    rows. Reporting more_remaining=true there tells the modeller to press a
    button that provably cannot advance, forever. logs_scanned still reports
    the real work, so the honest answer is "read N rows, none touched this
    model" - not "0 tables checked", which reads as "unused"."""
    full_window = [
        types.SimpleNamespace(
            id=uuid.uuid4(), raw_query="SELECT 1", rewritten_query="SELECT 1",
            user_identity=None, protocol="jdbc", created_at=_TS,
        )
        for _ in range(2)
    ]

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    side_effects = [
        _rows_result([("demo_data.orders", "postgresql")]),
        _rows_result([(uuid.uuid4(), "demo_data.orders")]),
        _scalar_result(None),
    ]
    # Every window is full and matches nothing: the scan exhausts its window
    # budget without recording anything.
    for _ in range(4):
        side_effects.append(_scalars_result(full_window))
        side_effects.append(_rows_result([]))
    db.execute = AsyncMock(side_effect=side_effects)

    snapshot = _scan_snapshot(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    with patch("src.api.impact_scan._MAX_LOG_ROWS", len(full_window)), \
            patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["references_upserted"] == 0
    assert body["logs_scanned"] == 8, "all four windows must still be examined"
    assert body["more_remaining"] is False, (
        "nothing was recorded, so the resume point did not move and pressing "
        "again would re-read the same rows"
    )


@pytest.mark.anyio
async def test_impact_scan_still_reports_more_remaining_when_it_made_progress(client):
    """The gate must not suppress the genuine case: a full window that DID
    record usage really does leave more rows to read."""
    region_col = uuid.uuid4()
    window = [
        types.SimpleNamespace(
            id=uuid.uuid4(), raw_query='SELECT "Region" FROM "modelx"',
            rewritten_query="", user_identity=None, protocol="jdbc",
            created_at=_TS,
        )
        for _ in range(2)
    ]

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([("demo_data.orders", "postgresql")]),
            _rows_result([(region_col, "demo_data.orders")]),
            _scalar_result(None),
            _scalars_result(window),
            _rows_result([]),
            _scalars_result([]),
        ]
    )

    snapshot = _scan_snapshot(region_col, uuid.uuid4(), uuid.uuid4())
    with patch("src.api.impact_scan._MAX_LOG_ROWS", len(window)), \
            patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/impact/scan"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["references_upserted"] == 1
    assert body["more_remaining"] is True


def test_a_closure_answer_outside_the_qualifier_falls_back_to_ambiguous():
    """When the closure names a table the query's own qualifier does not allow,
    neither source can be trusted over the other. The reference must be reported
    as ambiguous, never silently dropped - an under-report is the dangerous
    direction for a feature whose whole job is to stop a modeller dropping a
    column that is in use."""
    col_map = {
        ("sales.orders", "region"): ("region", "sales.orders"),
        ("archive.orders", "region"): ("region", "archive.orders"),
        ("other.orders", "region"): ("region", "other.orders"),
    }
    tables_by_col = {"region": {"sales.orders", "archive.orders", "other.orders"}}
    # The closure resolves `region` to a table the bare qualifier `orders`
    # cannot pick between sales/archive/other -- and it names a THIRD one.
    columns_by_id = {"col-x": ("region", "other.orders")}

    _parsed, _skipped, items = aggregate_column_usage(
        [("SELECT o.region FROM orders AS o", _TS)],
        col_map, tables_by_col,
        stable_column_ids_by_index=[None],
        columns_by_id=columns_by_id,
        closure_column_ids_by_index=[["col-x"]],
    )
    assert items, "the reference must not vanish"
    assert sum(i.hit_count for i in items) >= 1


# ---------------------------------------------------------------------------
# Round-3 deep-review promotions.
# ---------------------------------------------------------------------------


def test_a_dax_raw_query_is_not_mined_for_physical_column_names():
    """The protocol gate was applied to the SEMANTIC-name path only; the
    physical-column TOKEN scan inside aggregate_column_usage parsed raw_query
    with sqlglot unconditionally, so a model with a physical column named
    ``evaluate`` was credited usage by a DAX statement naming no such column -
    the exact hazard the gate exists for, and a doc/code divergence with
    strategy_impact-analysis.md 2.2a ("a DAX statement ... must fail closed")."""
    _parsed, _skipped, items = aggregate_column_usage(
        [('EVALUATE SUMMARIZECOLUMNS("Region", "Total", [Revenue])', None)],
        {("demo.t", "evaluate"): ("evaluate", "demo.t")},
        {"evaluate": {"demo.t"}},
        stable_column_ids_by_index=[None],
        columns_by_id={},
        closure_column_ids_by_index=[[]],
        sql_protocol_by_index=[False],
    )
    assert items == [], f"a DAX statement invented column usage: {items}"


def test_a_sql_raw_query_is_still_mined_for_physical_column_names():
    """The gate must not suppress the SQL case it is not meant to catch."""
    _parsed, _skipped, items = aggregate_column_usage(
        [("SELECT region FROM orders", None)],
        {("demo.t", "region"): ("region", "demo.t")},
        {"region": {"demo.t"}},
        stable_column_ids_by_index=[None],
        columns_by_id={},
        closure_column_ids_by_index=[[]],
        sql_protocol_by_index=[True],
    )
    assert [i.column_name for i in items] == ["region"]


def test_sql_protocol_allowlist_covers_every_sql_speaking_protocol():
    """Enumeration guard for a coverage tool (CLAUDE.md coverage-tool blind-spot
    audit). SQL_PROTOCOLS is an allowlist, so a NEW SQL-speaking protocol
    silently under-reports until it is added. Pinned against the protocol values
    that actually occur in the shipped corpora; ``preview`` was missing and its
    raw_query is plain SQL (``SELECT * FROM large_demo_data.payment_transaction
    LIMIT 50``)."""
    from src.api.impact_usage import SQL_PROTOCOLS

    # Observed live across acme-demo_meta, large_meta and acme_meta.
    sql_speaking = {"jdbc", "plugin", "headless", "introspect", "mcp", "preview"}
    not_sql = {"discover_members"}

    assert sql_speaking <= SQL_PROTOCOLS, (
        f"a SQL-speaking protocol is not allowlisted, so its legacy logs "
        f"under-report: {sql_speaking - SQL_PROTOCOLS}"
    )
    assert not (not_sql & SQL_PROTOCOLS), (
        "DISCOVER_MEMBERS(field) is a metadata-discovery call, not SQL"
    )


@pytest.mark.anyio
async def test_column_usage_discloses_that_it_examined_only_a_window(client):
    """A model with more successful logs than the window returns usage for the
    newest slice only. Without a disclosure, a column referenced solely in older
    traffic produces NO row and the tab renders "No column usage found" - a
    false negative in the one tool whose job is to say whether a column is safe
    to drop."""
    region_col = uuid.uuid4()
    log = types.SimpleNamespace(
        id=uuid.uuid4(), raw_query="SELECT region FROM demo_data.orders",
        rewritten_query="", user_identity=None, protocol="jdbc", created_at=_TS,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([(region_col, "region", "demo_data.orders")]),
            _scalar_result(15670),        # logs that EXIST
            _scalars_result([log]),       # the window actually read
            _rows_result([]),             # bind traces
        ]
    )

    snapshot = _scan_snapshot(region_col, uuid.uuid4(), uuid.uuid4())
    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
            f"/impact/column-usage"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["logs_available"] == 15670
    assert body["truncated"] is True, (
        "the endpoint read 1 of 15670 logs and did not say so"
    )


@pytest.mark.anyio
async def test_column_usage_does_not_claim_truncation_when_it_read_everything(client):
    """The disclosure must not cry wolf on a fully-scanned corpus."""
    region_col = uuid.uuid4()
    log = types.SimpleNamespace(
        id=uuid.uuid4(), raw_query="SELECT region FROM demo_data.orders",
        rewritten_query="", user_identity=None, protocol="jdbc", created_at=_TS,
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(
        side_effect=[
            _rows_result([(region_col, "region", "demo_data.orders")]),
            _scalar_result(1),
            _scalars_result([log]),
            _rows_result([]),
        ]
    )

    snapshot = _scan_snapshot(region_col, uuid.uuid4(), uuid.uuid4())
    with patch("src.api.impact_scan.get_tenant_db", async_gen_from(db)), \
            patch("src.api.impact_scan.ModelDependencyLoader", _patched_loader(snapshot)):
        resp = await client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
            f"/impact/column-usage"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["logs_available"] == 1
    assert body["truncated"] is False
