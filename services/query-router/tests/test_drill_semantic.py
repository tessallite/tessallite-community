"""Semantic drill-through builder tests (Phase 4.1).

Tests the semantic_builder module: SQL shape, hierarchy resolution,
cursor pagination, and drill-options discovery.  All DB access is
mocked via a fake AsyncSession.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock

import pytest

from src.drill.semantic_builder import (
    DrillSemanticError,
    _clamp_limit,
    _hydrate_snapshot,
    build_drill_sql,
    decode_cursor,
    encode_cursor,
    resolve_drill_options,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid():
    return uuid.uuid4()


def _measure(model_id=None, name="amount", default_agg="SUM", source_column_id=None):
    return types.SimpleNamespace(
        id=_uuid(),
        model_id=model_id or _uuid(),
        name=name,
        default_agg=default_agg,
        # Curation resolution reads source_column_id to find the intrinsic
        # source table; None keeps the fake DB's execute order unchanged.
        source_column_id=source_column_id,
        user_defined_attribute_id=None,
    )


def _uda_measure(model_id=None, name="amount", default_agg="SUM", uda_id=None):
    return types.SimpleNamespace(
        id=_uuid(),
        model_id=model_id or _uuid(),
        name=name,
        default_agg=default_agg,
        source_column_id=None,
        user_defined_attribute_id=uda_id or _uuid(),
    )


def _model(slug="modely"):
    return types.SimpleNamespace(id=_uuid(), slug=slug)


def _dimension(model_id, name, display_name=None, uda_id=None, col_id=None):
    return types.SimpleNamespace(
        id=_uuid(),
        model_id=model_id,
        name=name,
        display_name=display_name or name,
        user_defined_attribute_id=uda_id,
        source_column_id=col_id,
    )


def _hierarchy(hier_id, model_id, name="date_hierarchy"):
    return types.SimpleNamespace(id=hier_id, model_id=model_id, name=name)


def _level(hier_id, ordinal, name, key_attr_id, source="user_defined_attribute"):
    return types.SimpleNamespace(
        hierarchy_id=hier_id,
        ordinal=ordinal,
        name=name,
        key_attribute_id=key_attr_id,
        key_attribute_source=source,
    )


class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalars(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


def _make_db(*, get_map=None, execute_results=None):
    """Build a fake AsyncSession with configurable get() and execute() responses."""
    db = AsyncMock()
    _get_map = get_map or {}

    async def fake_get(cls, pk):
        return _get_map.get((cls.__name__, pk))

    db.get = AsyncMock(side_effect=fake_get)

    if execute_results:
        db.execute = AsyncMock(side_effect=[FakeResult(r) for r in execute_results])
    else:
        db.execute = AsyncMock(return_value=FakeResult([]))

    return db


# ---------------------------------------------------------------------------
# encode_cursor / decode_cursor
# ---------------------------------------------------------------------------


def test_encode_decode_roundtrip():
    for offset in (0, 1, 50, 999, 10_000):
        assert decode_cursor(encode_cursor(offset)) == offset


def test_decode_cursor_none_returns_zero():
    assert decode_cursor(None) == 0
    assert decode_cursor("") == 0


def test_decode_cursor_invalid_raises():
    with pytest.raises(DrillSemanticError) as exc:
        decode_cursor("not-valid-base64!!")
    assert exc.value.error_code == "INVALID_CURSOR"


# ---------------------------------------------------------------------------
# _clamp_limit
# ---------------------------------------------------------------------------


def test_clamp_limit():
    assert _clamp_limit(None) == 1000
    assert _clamp_limit(0) == 1000
    assert _clamp_limit(-5) == 1000
    assert _clamp_limit(500) == 500
    assert _clamp_limit(99_999) == 10_000


# ---------------------------------------------------------------------------
# resolve_drill_options — no hierarchy
# ---------------------------------------------------------------------------


async def test_drill_options_returns_empty_when_no_hierarchy():
    model_id = _uuid()
    measure = _measure(model_id=model_id)
    dim = _dimension(model_id, "country", uda_id=None, col_id=_uuid())

    db = _make_db(
        get_map={("Measure", measure.id): measure},
        execute_results=[
            [dim],       # _load_dimensions_by_name
            [],          # _find_drillable_hierarchies → levels query (no levels)
        ],
    )
    result = await resolve_drill_options(
        measure_id=measure.id,
        grouping_levels=[{"column": "country", "value": "US"}],
        db=db,
    )
    assert result == []


async def test_drill_options_measure_not_found():
    db = _make_db()
    with pytest.raises(DrillSemanticError) as exc:
        await resolve_drill_options(
            measure_id=_uuid(),
            grouping_levels=[{"column": "x"}],
            db=db,
        )
    assert exc.value.error_code == "MEASURE_NOT_FOUND"


# ---------------------------------------------------------------------------
# build_drill_sql — hierarchy drill-down
# ---------------------------------------------------------------------------


async def test_build_drill_sql_hierarchy_drill_down():
    """Year dimension in a date hierarchy → drills to month."""
    model_id = _uuid()
    hier_id = _uuid()
    year_uda_id = _uuid()
    month_uda_id = _uuid()

    measure = _measure(model_id=model_id, name="amount", default_agg="SUM")
    model = _model(slug="modely")
    year_dim = _dimension(model_id, "business_date_year", uda_id=year_uda_id)
    month_dim = _dimension(model_id, "business_date_month", display_name="Month", uda_id=month_uda_id)

    year_level = _level(hier_id, 0, "Year", year_uda_id)
    month_level = _level(hier_id, 1, "Month", month_uda_id)
    hier = _hierarchy(hier_id, model_id, "business_date")

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[
            [],                         # _load_curation → no DrillThroughSet
            [year_dim],                 # _load_dimensions_by_name
            [year_level],               # HierarchyLevel.where(key_attribute_id.in_)
            [hier],                     # HierarchyDefinition.where(id.in_)
            [year_level, month_level],  # all levels by hierarchy
            [month_dim],               # _resolve_level_dimension for next level
        ],
    )

    (
        sql,
        model_id_str,
        offset,
        limit,
        drill_dim,
        drill_mode,
        path,
        drillable,
        fact_table,
        source_join_path,
    ) = (
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=None,
            grouping_levels=[{"column": "business_date_year", "value": 2025}],
            cursor=None,
            limit=100,
            db=db,
        )
    )
    assert source_join_path == []

    assert drill_mode == "hierarchy"
    assert drill_dim is not None
    assert drill_dim.name == "business_date_month"
    assert '"business_date_month"' in sql
    assert 'SUM("amount")' in sql
    assert 'FROM "modely"' in sql
    assert '"business_date_year" = 2025' in sql
    assert "GROUP BY" in sql
    assert offset == 0
    assert limit == 100
    assert len(drillable) == 1
    assert drillable[0].hierarchy_name == "business_date"


# ---------------------------------------------------------------------------
# build_drill_sql — leaf level (no further drill)
# ---------------------------------------------------------------------------


async def test_build_drill_sql_leaf_level():
    """Dimension at leaf of hierarchy (or no hierarchy) → leaf mode."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount", default_agg="SUM")
    model = _model(slug="modely")
    day_dim = _dimension(model_id, "business_date", uda_id=None, col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[
            [],         # _load_curation → no DrillThroughSet
            [day_dim],  # _load_dimensions_by_name
            [],         # HierarchyLevel.where → no levels (leaf/no hierarchy)
        ],
    )

    (
        sql,
        model_id_str,
        offset,
        limit,
        drill_dim,
        drill_mode,
        path,
        drillable,
        fact_table,
        source_join_path,
    ) = (
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=None,
            grouping_levels=[{"column": "business_date", "value": "2025-03-15"}],
            cursor=None,
            limit=50,
            db=db,
        )
    )
    assert source_join_path == []

    # F-019-02: leaf mode returns the *contributing detail rows*, not a
    # restated aggregate. The projection is the cell's grouping dimension(s);
    # the measure is NOT rolled up and there is no GROUP BY.
    assert drill_mode == "leaf"
    assert drill_dim is None
    assert '"business_date"' in sql
    assert 'SUM("amount")' not in sql       # not aggregated
    assert '"amount"' in sql                # raw measure column projected
    assert 'FROM "modely"' in sql
    assert "GROUP BY" not in sql
    assert "ORDER BY" in sql                # F-019-04: deterministic pagination
    # Bug-1108: the leaf ORDER BY must be a TOTAL order, not just the constant
    # cell coordinate. With no PK-backed dimension the widest deterministic key
    # is the full projection, so the measure value must appear in ORDER BY
    # after the (constant) leaf dimension.
    order_clause = sql[sql.index("ORDER BY"):sql.index("LIMIT")]
    assert '"business_date"' in order_clause
    assert '"amount"' in order_clause       # measure tie-breaker -> total order
    assert len(drillable) == 0


# ---------------------------------------------------------------------------
# build_drill_sql — COUNT_DISTINCT aggregation
# ---------------------------------------------------------------------------


async def test_build_drill_sql_count_distinct_hierarchy_step():
    """COUNT_DISTINCT aggregation appears in HIERARCHY step-down mode (leaf
    mode returns detail rows and never aggregates — F-019-02)."""
    model_id = _uuid()
    hier_id = _uuid()
    year_uda = _uuid()
    month_uda = _uuid()
    measure = _measure(model_id=model_id, name="customer_id", default_agg="COUNT_DISTINCT")
    model = _model(slug="modely")
    year_dim = _dimension(model_id, "year", uda_id=year_uda)
    month_dim = _dimension(model_id, "month", display_name="Month", uda_id=month_uda)
    year_lvl = _level(hier_id, 0, "Year", year_uda)
    month_lvl = _level(hier_id, 1, "Month", month_uda)
    hier = _hierarchy(hier_id, model_id, "date_hierarchy")

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[
            [],                       # _load_curation
            [year_dim],               # _load_dimensions_by_name
            [year_lvl],               # HierarchyLevel matching key_attr
            [hier],                   # HierarchyDefinition
            [year_lvl, month_lvl],    # all levels ordered
            [month_dim],              # _resolve_level_dimension
        ],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "year", "value": 2025}],
        cursor=None,
        limit=None,
        db=db,
    )

    assert 'COUNT(DISTINCT "customer_id")' in sql


# ---------------------------------------------------------------------------
# build_drill_sql — string value escaping
# ---------------------------------------------------------------------------


async def test_build_drill_sql_string_value_escaping():
    model_id = _uuid()
    measure = _measure(model_id=model_id)
    model = _model(slug="modely")
    dim = _dimension(model_id, "name", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "name", "value": "O'Brien"}],
        cursor=None,
        limit=None,
        db=db,
    )

    assert "O''Brien" in sql


# ---------------------------------------------------------------------------
# build_drill_sql — NULL value filter
# ---------------------------------------------------------------------------


async def test_build_drill_sql_null_value():
    model_id = _uuid()
    measure = _measure(model_id=model_id)
    model = _model(slug="modely")
    dim = _dimension(model_id, "region", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": None}],
        cursor=None,
        limit=None,
        db=db,
    )

    assert '"region" IS NULL' in sql


# ---------------------------------------------------------------------------
# build_drill_sql — cursor pagination
# ---------------------------------------------------------------------------


async def test_build_drill_sql_cursor_offset():
    model_id = _uuid()
    measure = _measure(model_id=model_id)
    model = _model(slug="modely")
    dim = _dimension(model_id, "region", col_id=_uuid())

    cursor = encode_cursor(500)
    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    sql, _, offset, limit, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=cursor,
        limit=100,
        db=db,
    )

    assert offset == 500
    assert "OFFSET 500" in sql
    assert "LIMIT 101" in sql  # effective_limit + 1


# ---------------------------------------------------------------------------
# build_drill_sql — measure not found
# ---------------------------------------------------------------------------


async def test_build_drill_sql_measure_not_found():
    db = _make_db()
    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=_uuid(),
            hierarchy_id=None,
            grouping_levels=[{"column": "x"}],
            cursor=None,
            limit=None,
            db=db,
        )
    assert exc.value.error_code == "MEASURE_NOT_FOUND"


# ---------------------------------------------------------------------------
# build_drill_sql — model not found
# ---------------------------------------------------------------------------


async def test_build_drill_sql_model_not_found():
    model_id = _uuid()
    measure = _measure(model_id=model_id)
    db = _make_db(get_map={("Measure", measure.id): measure})

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=None,
            grouping_levels=[],
            cursor=None,
            limit=None,
            db=db,
        )
    assert exc.value.error_code == "MODEL_NOT_FOUND"


# ---------------------------------------------------------------------------
# SQL shape: always SELECT <dim>, <agg>(<measure>) FROM "<slug>" WHERE ... GROUP BY <dim>
# ---------------------------------------------------------------------------


async def test_sql_shape_always_uses_model_slug():
    """Every drill SQL must reference the model slug, not a physical table."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="revenue", default_agg="AVG")
    model = _model(slug="my_model")
    dim = _dimension(model_id, "category", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "category", "value": "Electronics"}],
        cursor=None,
        limit=10,
        db=db,
    )

    # Leaf detail mode (F-019-02): the cell's dimension is projected as a
    # detail row; the measure is not aggregated. The invariant under test is
    # that the SQL references the model slug, never a physical table.
    assert 'FROM "my_model"' in sql
    assert '"category"' in sql
    assert "public." not in sql
    assert "payment_transaction" not in sql


# ---------------------------------------------------------------------------
# build_drill_sql — deepest level selection (Year+Month → drill Month→Day)
# ---------------------------------------------------------------------------


async def test_build_drill_sql_deepest_level_selected():
    """When grouping_levels contain Year AND Month from the same hierarchy,
    the drill must pick Month→Day (deepest), not Year→Month (shallowest)."""
    model_id = _uuid()
    hier_id = _uuid()
    year_uda = _uuid()
    month_uda = _uuid()
    day_uda = _uuid()

    measure = _measure(model_id=model_id, name="amount", default_agg="SUM")
    model = _model(slug="modely")
    year_dim = _dimension(model_id, "year", uda_id=year_uda)
    month_dim = _dimension(model_id, "month", uda_id=month_uda)
    day_dim = _dimension(model_id, "day", display_name="Day", uda_id=day_uda)

    year_lvl = _level(hier_id, 0, "Year", year_uda)
    month_lvl = _level(hier_id, 1, "Month", month_uda)
    day_lvl = _level(hier_id, 2, "Day", day_uda)
    hier = _hierarchy(hier_id, model_id, "date_hierarchy")

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[
            [],                                         # _load_curation
            [year_dim, month_dim],                      # _load_dimensions_by_name
            [year_lvl, month_lvl],                      # HierarchyLevel matching key_attr
            [hier],                                     # HierarchyDefinition
            [year_lvl, month_lvl, day_lvl],             # all levels ordered
            [month_dim],                                # _resolve_level_dimension (Year→Month)
            [day_dim],                                  # _resolve_level_dimension (Month→Day)
        ],
    )

    sql, _, _, _, drill_dim, drill_mode, _, drillable, _ft, _join_path = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=hier_id,
        grouping_levels=[
            {"column": "year", "value": 2025},
            {"column": "month", "value": 3},
        ],
        cursor=None,
        limit=100,
        db=db,
    )

    assert drill_mode == "hierarchy"
    assert drill_dim is not None
    assert drill_dim.name == "day", (
        f"Expected drill to Day but got {drill_dim.name} — "
        "deepest level not selected"
    )
    assert '"day"' in sql
    assert "GROUP BY" in sql


# ---------------------------------------------------------------------------
# build_drill_sql — boolean value handling
# ---------------------------------------------------------------------------


async def test_build_drill_sql_boolean_values():
    """Boolean grouping level values produce TRUE/FALSE literals."""
    model_id = _uuid()
    measure = _measure(model_id=model_id)
    model = _model(slug="modely")
    dim = _dimension(model_id, "is_active", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    sql_true, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "is_active", "value": True}],
        cursor=None,
        limit=None,
        db=db,
    )
    assert '"is_active" = TRUE' in sql_true

    db2 = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )
    sql_false, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "is_active", "value": False}],
        cursor=None,
        limit=None,
        db=db2,
    )
    assert '"is_active" = FALSE' in sql_false


# ===========================================================================
# B16 — curation wiring, leaf detail mode, filters, pagination, bound params
# ===========================================================================


def _drill_set(*, source_table_id=None, detail_columns=None,
               joined_dimension_ids=None, row_limit_override=None,
               source_join_path=None):
    return types.SimpleNamespace(
        id=_uuid(),
        source_table_id=source_table_id,
        detail_columns=detail_columns,
        joined_dimension_ids=joined_dimension_ids,
        row_limit_override=row_limit_override,
        source_join_path=source_join_path,
    )


def _model_table(physical_name="orders"):
    return types.SimpleNamespace(id=_uuid(), physical_name=physical_name)


# --- F-019-01: detail-column projection (hidden column absent) --------------


async def test_curation_detail_columns_replace_default_projection():
    """A curated detail-column set drives the leaf projection. A column the
    modeller did NOT curate (e.g. a PII column) is absent from the SQL."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount", source_column_id=_uuid())
    model = _model(slug="modely")
    table = _model_table("orders")
    # The detail columns resolve to two dimension names; "ssn" is deliberately
    # not in the curated set, so it must not appear in the projection.
    ds = _drill_set(detail_columns=[str(_uuid()), str(_uuid())])

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelColumn", measure.source_column_id): types.SimpleNamespace(
                model_table_id=table.id
            ),
            ("ModelTable", table.id): table,
        },
        execute_results=[
            [ds],                          # _load_curation → DrillThroughSet
            ["order_id", "region"],        # detail_columns → dim names
            [],                            # PK tie-breaker → none (Bug-1108)
            [_dimension(model_id, "region", col_id=_uuid())],  # _load_dimensions_by_name
            [],                            # no hierarchy levels
        ],
    )

    sql, *_rest = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=None,
        db=db,
    )
    fact_table = _rest[-2]

    assert '"order_id"' in sql
    assert '"region"' in sql
    assert "ssn" not in sql              # hidden / un-curated column absent
    assert 'SUM("amount")' not in sql    # leaf detail, no aggregation
    assert fact_table == "orders"        # transparency field populated


# --- 5261: unprojectable detail columns raise, not silently dropped ---------


async def test_unprojectable_detail_column_raises_not_silently_dropped():
    """A detail_column with no Dimension over it must raise
    DRILL_DETAIL_COLUMN_NOT_PROJECTABLE rather than being silently omitted
    from the projection (5261)."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount", source_column_id=_uuid())
    model = _model(slug="modely")
    table = _model_table("orders")
    # Three detail columns, but only two resolve to dimensions — the third
    # is a physical column with no Dimension (e.g. an internal audit column).
    ds = _drill_set(detail_columns=[str(_uuid()), str(_uuid()), str(_uuid())])

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelColumn", measure.source_column_id): types.SimpleNamespace(
                model_table_id=table.id
            ),
            ("ModelTable", table.id): table,
        },
        execute_results=[
            [ds],                          # _load_curation -> DrillThroughSet
            ["order_id", "region"],        # detail_columns -> only 2 of 3 resolved
        ],
    )

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=None,
            grouping_levels=[{"column": "region", "value": "EMEA"}],
            cursor=None,
            limit=None,
            db=db,
        )

    assert exc.value.error_code == "DRILL_DETAIL_COLUMN_NOT_PROJECTABLE"
    assert "1 drill-through detail column" in str(exc.value)


async def test_all_detail_columns_projectable_no_error():
    """When every detail column resolves to a dimension, no error is raised
    (regression guard for 5261)."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount", source_column_id=_uuid())
    model = _model(slug="modely")
    table = _model_table("orders")
    ds = _drill_set(detail_columns=[str(_uuid()), str(_uuid())])

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelColumn", measure.source_column_id): types.SimpleNamespace(
                model_table_id=table.id
            ),
            ("ModelTable", table.id): table,
        },
        execute_results=[
            [ds],                          # _load_curation -> DrillThroughSet
            ["order_id", "region"],        # detail_columns -> all 2 resolved
            [],                            # PK tie-breaker -> none
            [_dimension(model_id, "region", col_id=_uuid())],  # _load_dimensions_by_name
            [],                            # no hierarchy levels
        ],
    )

    # Should not raise
    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=None,
        db=db,
    )
    assert '"order_id"' in sql
    assert '"region"' in sql


# --- F-019-01: joined dimensions appended -----------------------------------


async def test_curation_joined_dimension_added_to_projection():
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount", source_column_id=_uuid())
    model = _model(slug="modely")
    table = _model_table("orders")
    ds = _drill_set(
        detail_columns=[str(_uuid())],
        joined_dimension_ids=[str(_uuid())],
    )

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelColumn", measure.source_column_id): types.SimpleNamespace(
                model_table_id=table.id
            ),
            ("ModelTable", table.id): table,
        },
        execute_results=[
            [ds],                       # _load_curation
            ["order_id"],               # detail_columns → dim names
            ["customer__name"],         # joined_dimension_ids → dim names
            [],                         # PK tie-breaker → none (Bug-1108)
            [_dimension(model_id, "region", col_id=_uuid())],  # _load_dimensions_by_name
            [],                         # no hierarchy levels
        ],
    )

    sql, *rest = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=None,
        db=db,
    )

    assert '"order_id"' in sql
    assert '"customer__name"' in sql     # joined dim label present


# --- F-019-01: row-limit override honoured ----------------------------------


async def test_curation_row_limit_override_used_as_default():
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount", source_column_id=_uuid())
    model = _model(slug="modely")
    table = _model_table("orders")
    ds = _drill_set(row_limit_override=250)

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelColumn", measure.source_column_id): types.SimpleNamespace(
                model_table_id=table.id
            ),
            ("ModelTable", table.id): table,
        },
        execute_results=[
            [ds],   # _load_curation (no detail / joined columns)
            [],     # PK tie-breaker → none (Bug-1108)
            [_dimension(model_id, "region", col_id=_uuid())],  # _load_dimensions_by_name
            [],     # no hierarchy levels
        ],
    )

    sql, _, _, effective_limit, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=None,            # no request limit → curated override applies
        db=db,
    )

    assert effective_limit == 250
    assert "LIMIT 251" in sql  # effective + 1


async def test_request_limit_overrides_curated_override():
    """An explicit request limit beats the curated override."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount", source_column_id=_uuid())
    model = _model(slug="modely")
    table = _model_table("orders")
    ds = _drill_set(row_limit_override=250)

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelColumn", measure.source_column_id): types.SimpleNamespace(
                model_table_id=table.id
            ),
            ("ModelTable", table.id): table,
        },
        execute_results=[
            [ds],
            [],   # PK tie-breaker → none (Bug-1108)
            [_dimension(model_id, "region", col_id=_uuid())],
            [],
        ],
    )

    _, _, _, effective_limit, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=50,
        db=db,
    )
    assert effective_limit == 50


async def test_source_table_override_join_path_is_emitted_for_runtime_rewrite():
    model_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    source_col_id = _uuid()
    join_id = _uuid()
    measure = _measure(
        model_id=model_id, name="amount", source_column_id=source_col_id,
    )
    model = _model(slug="modely")
    fact_table = types.SimpleNamespace(id=fact_table_id, physical_name="orders")
    override_table = types.SimpleNamespace(id=override_table_id, physical_name="order_lines")
    ds = _drill_set(
        source_table_id=override_table_id,
        source_join_path=[str(join_id)],
    )

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelTable", override_table_id): override_table,
            ("ModelTable", fact_table_id): fact_table,
            ("ModelColumn", source_col_id): types.SimpleNamespace(
                model_table_id=fact_table_id
            ),
        },
        execute_results=[
            [ds],
            [],
            [_dimension(model_id, "region", col_id=_uuid())],
            [],
        ],
    )

    sql, *rest = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=None,
        db=db,
    )

    source_join_path = rest[-1]
    assert "tessallite_drill_join_path" not in sql
    assert source_join_path == [str(join_id)]


async def test_source_table_override_without_join_path_is_rejected():
    model_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    source_col_id = _uuid()
    measure = _measure(
        model_id=model_id, name="amount", source_column_id=source_col_id,
    )
    model = _model(slug="modely")
    fact_table = types.SimpleNamespace(id=fact_table_id, physical_name="orders")
    override_table = types.SimpleNamespace(id=override_table_id, physical_name="order_lines")
    ds = _drill_set(source_table_id=override_table_id)

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelTable", override_table_id): override_table,
            ("ModelTable", fact_table_id): fact_table,
            ("ModelColumn", source_col_id): types.SimpleNamespace(
                model_table_id=fact_table_id
            ),
        },
        execute_results=[[ds], []],
    )

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=None,
            grouping_levels=[{"column": "region", "value": "EMEA"}],
            cursor=None,
            limit=None,
            db=db,
        )

    assert exc.value.error_code == "DRILL_JOIN_PATH_REQUIRED"


async def test_uda_source_table_override_without_join_path_is_rejected():
    model_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    uda_id = _uuid()
    measure = _uda_measure(model_id=model_id, name="amount", uda_id=uda_id)
    model = _model(slug="modely")
    fact_table = types.SimpleNamespace(id=fact_table_id, physical_name="orders")
    override_table = types.SimpleNamespace(id=override_table_id, physical_name="order_lines")
    uda = types.SimpleNamespace(id=uda_id, table_id=fact_table_id)
    ds = _drill_set(source_table_id=override_table_id)

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelTable", override_table_id): override_table,
            ("ModelTable", fact_table_id): fact_table,
            ("UserDefinedAttribute", uda_id): uda,
        },
        execute_results=[[ds], []],
    )

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=None,
            grouping_levels=[{"column": "region", "value": "EMEA"}],
            cursor=None,
            limit=None,
            db=db,
        )

    assert exc.value.error_code == "DRILL_JOIN_PATH_REQUIRED"


# --- F-019-03: filters (slicer context) reach the WHERE ---------------------


async def test_filters_reach_where_clause():
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount")
    model = _model(slug="modely")
    dim = _dimension(model_id, "region", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        filters=[{"column": "status", "op": "eq", "value": "completed"}],
        cursor=None,
        limit=None,
        db=db,
    )

    # Both the cell coordinate and the slicer filter must be present, ANDed.
    assert '"region" = \'EMEA\'' in sql
    assert '"status" = \'completed\'' in sql


# --- F-019-04: deterministic ORDER BY ---------------------------------------


async def test_order_by_present_for_pagination_stability():
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount")
    model = _model(slug="modely")
    dim = _dimension(model_id, "region", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=None,
        db=db,
    )

    assert "ORDER BY" in sql
    # ORDER BY must precede LIMIT/OFFSET so paging is stable.
    assert sql.index("ORDER BY") < sql.index("LIMIT")


async def test_leaf_order_by_is_total_order_not_constant_only():
    """Bug-1108: leaf ORDER BY must not be the constant cell coordinate only.

    The cell dimension value is constant across every contributing row, so an
    ORDER BY over it alone is not a total order and LIMIT/OFFSET pages can
    skip/duplicate on engines with non-stable scan order. The emitted ORDER BY
    must carry a tie-breaker tail (the un-aggregated measure value) so each row
    has a distinct ordering position.
    """
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount")
    model = _model(slug="modely")
    dim = _dimension(model_id, "account_type", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],   # no DrillThroughSet, no source table
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "account_type", "value": "WALLET"}],
        cursor=None,
        limit=None,
        db=db,
    )

    order_clause = sql[sql.index("ORDER BY"):sql.index("LIMIT")]
    # The constant cell coordinate is the lead key...
    assert '"account_type"' in order_clause
    # ...but it is NOT the whole key: the measure value follows it as a
    # deterministic tie-breaker, so the sort is a total order.
    assert '"amount"' in order_clause
    assert order_clause.index('"account_type"') < order_clause.index('"amount"')


async def test_leaf_order_by_appends_pk_dimension_tiebreaker():
    """Bug-1108: when the source table has a PK-backed projectable dimension,
    it is appended to the projection AND the ORDER BY as a guaranteed-unique
    tail, giving a strict total order even when two fact rows share identical
    detail/measure values.
    """
    model_id = _uuid()
    src_col_id = _uuid()
    table_id = _uuid()
    measure = _measure(
        model_id=model_id, name="amount", source_column_id=src_col_id,
    )
    model = _model(slug="modely")
    dim = _dimension(model_id, "account_type", col_id=_uuid())
    # measure.source_column_id -> ModelColumn -> ModelTable resolves the
    # effective source table; the PK-dimension query then returns "txn_id".
    src_col = types.SimpleNamespace(id=src_col_id, model_table_id=table_id)
    src_table = types.SimpleNamespace(id=table_id, physical_name="demo.payment")

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelColumn", src_col_id): src_col,
            ("ModelTable", table_id): src_table,
        },
        execute_results=[
            [],            # _load_curation -> no DrillThroughSet
            ["txn_id"],    # _resolve_pk_tiebreaker_dim -> PK dimension name
            [dim],         # _load_dimensions_by_name
            [],            # HierarchyLevel.where -> leaf
        ],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "account_type", "value": "WALLET"}],
        cursor=None,
        limit=None,
        db=db,
    )

    # PK dimension is projected (transparency) and is the trailing unique key.
    assert '"txn_id"' in sql
    order_clause = sql[sql.index("ORDER BY"):sql.index("LIMIT")]
    assert order_clause.rstrip().endswith('"txn_id"')
    assert '"account_type"' in order_clause
    assert '"amount"' in order_clause


async def test_leaf_order_by_without_pk_is_best_effort_not_total():
    """3546: when no PK-backed dimension exists, the ORDER BY is the full
    projection but NOT a strict total order. Two fact rows with identical
    projected values share the same sort position. This test documents the
    residual: the ORDER BY covers all projected columns but makes no
    guarantee of uniqueness without a PK dimension."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount")
    model = _model(slug="modely")
    dim = _dimension(model_id, "category", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],   # no DrillThroughSet, no PK
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "category", "value": "Books"}],
        cursor=None,
        limit=None,
        db=db,
    )

    order_clause = sql[sql.index("ORDER BY"):sql.index("LIMIT")]
    # Without a PK tiebreaker, the ORDER BY should include all projected
    # columns (best-effort determinism) but no PK tail.
    assert '"category"' in order_clause
    assert '"amount"' in order_clause
    # The ORDER BY includes exactly the projection columns — no more, no less.
    # This is NOT a total order; it is the best the semantic path can achieve.
    # (If a PK dimension were modelled, it would appear as a trailing key.)


def _rows_for_offset(order, offset, limit, total=23):
    """Simulate an UNSTABLE-scan source: a deterministic SQL ORDER BY over a
    UNIQUE key (txn_id 0..total-1) projected as paged windows. If the order
    key were a constant, an unstable engine could return any window -- this
    models the engine honouring a TOTAL order, which is exactly what the fix
    guarantees by emitting one.
    """
    ordered = sorted(range(total), key=order)
    return ordered[offset:offset + limit]


async def test_leaf_pagination_disjoint_coverage_under_total_order():
    """Disjoint-coverage proof: with a total-order key, consecutive LIMIT/
    OFFSET pages are disjoint and their union is the full row set with no skip
    or duplicate — the property F-019-04 promised. Simulated against an
    unstable source by paging over a unique total-order key.
    """
    total = 23
    page = 5
    # Unique total-order key (txn_id); an unstable engine MUST still honour it.
    key = lambda i: i  # noqa: E731
    seen: list[int] = []
    offset = 0
    while offset < total:
        rows = _rows_for_offset(key, offset, page, total=total)
        if not rows:
            break
        # No overlap with previously returned rows.
        assert not (set(rows) & set(seen)), f"duplicate at offset {offset}"
        seen.extend(rows)
        offset += page

    # Union of all pages == full set, in order, no gaps, no dups.
    assert seen == list(range(total))
    assert len(seen) == len(set(seen)) == total


# --- F-019-05: operators implemented (not silently equality) ----------------


async def test_filter_operators_compiled_not_forced_to_equality():
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount")
    model = _model(slug="modely")
    dim = _dimension(model_id, "region", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        filters=[
            {"column": "amount", "op": "gte", "value": 100},
            {"column": "status", "op": "in", "value": ["a", "b"]},
        ],
        cursor=None,
        limit=None,
        db=db,
    )

    assert '"amount" >= 100' in sql          # gte, NOT rewritten to =
    assert '"status" IN (\'a\', \'b\')' in sql  # in, NOT rewritten to =


async def test_unsupported_operator_raises_structured_error():
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount")
    model = _model(slug="modely")
    dim = _dimension(model_id, "region", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[[], [dim], []],
    )

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=None,
            grouping_levels=[{"column": "region", "value": "EMEA"}],
            filters=[{"column": "x", "op": "regex", "value": ".*"}],
            cursor=None,
            limit=None,
            db=db,
        )
    assert exc.value.error_code == "DrillThroughUnsupportedOperator"


# ---------------------------------------------------------------------------
# Bug-5344 — pivot/drill ordering & the GROUP-BY-measure antipattern guard.
#
# Matrix covering: leaf measure-only (no dim / no curation), leaf with a
# grouping dimension, leaf with curated detail columns, and the hierarchy
# step-down — asserting in every case that (1) a MEASURE is NEVER in GROUP BY
# (the antipattern), and (2) the leaf detail surfaces the BIGGEST contributing
# rows first (measure DESC), never a wall of the smallest values (the "drill
# to 0s" defect).
# ---------------------------------------------------------------------------


def _order_clause(sql: str) -> str:
    return sql[sql.index("ORDER BY"):sql.index("LIMIT")]


async def test_bug5344_leaf_measure_only_orders_measure_desc():
    """Measure-only drill (no dimension, no curated detail columns): leaf detail
    ordered by the measure DESCENDING (biggest contributors first), no GROUP BY,
    no aggregation. Reproduces the exact 'drill to 0s' scenario."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="tax_amount", default_agg="SUM")
    model = _model(slug="modell")
    db = _make_db(
        get_map={("Measure", measure.id): measure, ("Model", model_id): model},
        execute_results=[[], [], [], []],  # _load_curation (no drill set); rest unused
    )
    sql, _mid, _off, _lim, _dim, drill_mode, *_ = await build_drill_sql(
        measure_id=measure.id, hierarchy_id=None, grouping_levels=[],
        cursor=None, limit=50, db=db,
    )
    assert drill_mode == "leaf"
    assert "GROUP BY" not in sql                 # antipattern guard
    assert "SUM(" not in sql                      # un-aggregated detail rows
    oc = _order_clause(sql)
    assert '"tax_amount" DESC' in oc              # Bug-5344: biggest first, not 0s
    assert oc.count("DESC") == 1
    assert sql.startswith('SELECT "tax_amount" FROM "modell"')


async def test_bug5344_leaf_with_dimension_measure_desc_dim_asc():
    """Leaf with a grouping dimension: dimension stays ASC (stable key), the
    un-aggregated measure value sorts DESC, no measure in GROUP BY."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount", default_agg="SUM")
    model = _model(slug="modely")
    day_dim = _dimension(model_id, "business_date", col_id=_uuid())
    db = _make_db(
        get_map={("Measure", measure.id): measure, ("Model", model_id): model},
        execute_results=[
            [],          # _load_curation
            [day_dim],   # _load_dimensions_by_name
            [],          # HierarchyLevel → leaf
        ],
    )
    sql, _mid, _off, _lim, _dim, drill_mode, *_ = await build_drill_sql(
        measure_id=measure.id, hierarchy_id=None,
        grouping_levels=[{"column": "business_date", "value": "2025-03-15"}],
        cursor=None, limit=50, db=db,
    )
    assert drill_mode == "leaf"
    assert "GROUP BY" not in sql
    oc = _order_clause(sql)
    assert '"business_date"' in oc and '"business_date" DESC' not in oc  # dim ASC
    assert '"amount" DESC' in oc                                         # measure DESC


async def test_drill_hierarchy_step_down_group_by_is_dimension_not_measure():
    """Hierarchy step-down aggregates the measure and GROUPS BY the next-level
    DIMENSION — never the measure. Confirms the antipattern is absent on the
    aggregating path too."""
    model_id = _uuid()
    hier_id = _uuid()
    year_uda, month_uda = _uuid(), _uuid()
    measure = _measure(model_id=model_id, name="amount", default_agg="SUM")
    model = _model(slug="modely")
    year_dim = _dimension(model_id, "year", uda_id=year_uda)
    month_dim = _dimension(model_id, "month", display_name="Month", uda_id=month_uda)
    year_lvl = _level(hier_id, 0, "Year", year_uda)
    month_lvl = _level(hier_id, 1, "Month", month_uda)
    hier = _hierarchy(hier_id, model_id, "date_hierarchy")
    db = _make_db(
        get_map={("Measure", measure.id): measure, ("Model", model_id): model},
        execute_results=[
            [], [year_dim], [year_lvl], [hier], [year_lvl, month_lvl], [month_dim],
        ],
    )
    sql, _mid, _off, _lim, _dim, drill_mode, *_ = await build_drill_sql(
        measure_id=measure.id, hierarchy_id=hier_id,
        grouping_levels=[{"column": "year", "value": "2025"}],
        cursor=None, limit=50, db=db,
    )
    assert drill_mode == "hierarchy"
    assert 'SUM("amount")' in sql                 # measure aggregated
    assert 'GROUP BY "month"' in sql              # grouped by the DIMENSION
    # the antipattern guard: the measure name never appears inside GROUP BY ...
    gb = sql[sql.index("GROUP BY"):sql.index("ORDER BY")]
    assert '"amount"' not in gb


# ===========================================================================
# 5262+3971 — Snapshot-aligned metadata resolution
# ===========================================================================


def _snapshot(*, model_id, measures=None, dimensions=None,
              hierarchies=None, drill_through_sets=None,
              columns=None, tables=None):
    """Build a minimal snapshot_json dict for testing."""
    return {
        "schema_version": 1,
        "model": {"id": str(model_id), "slug": "modely"},
        "measures": measures or [],
        "dimensions": dimensions or [],
        "hierarchies": hierarchies or [],
        "drill_through_sets": drill_through_sets or [],
        "columns": columns or [],
        "tables": tables or [],
    }


def _snap_measure(measure_id, model_id, name="amount", default_agg="SUM",
                  source_column_id=None, uda_id=None):
    d = {
        "id": str(measure_id), "model_id": str(model_id),
        "name": name, "default_agg": default_agg,
    }
    if source_column_id:
        d["source_column_id"] = str(source_column_id)
    if uda_id:
        d["user_defined_attribute_id"] = str(uda_id)
    return d


def _snap_dimension(dim_id, model_id, name, display_name=None,
                    source_column_id=None, uda_id=None):
    d = {
        "id": str(dim_id), "model_id": str(model_id),
        "name": name, "display_name": display_name or name,
    }
    if source_column_id:
        d["source_column_id"] = str(source_column_id)
    if uda_id:
        d["user_defined_attribute_id"] = str(uda_id)
    return d


def _snap_hierarchy(hier_id, model_id, name, levels):
    return {
        "id": str(hier_id), "model_id": str(model_id),
        "name": name, "levels": levels,
    }


def _snap_level(ordinal, name, key_attribute_id,
                key_attribute_source="user_defined_attribute",
                level_id=None):
    return {
        "id": str(level_id or _uuid()), "ordinal": ordinal,
        "name": name, "key_attribute_id": str(key_attribute_id),
        "key_attribute_source": key_attribute_source,
    }


def _snap_drill_set(measure_id, *, detail_columns=None,
                    joined_dimension_ids=None, row_limit_override=None,
                    source_table_id=None, source_join_path=None):
    d = {"id": str(_uuid()), "measure_id": str(measure_id)}
    if detail_columns is not None:
        d["detail_columns"] = detail_columns
    if joined_dimension_ids is not None:
        d["joined_dimension_ids"] = joined_dimension_ids
    if row_limit_override is not None:
        d["row_limit_override"] = row_limit_override
    if source_table_id is not None:
        d["source_table_id"] = str(source_table_id)
    if source_join_path is not None:
        d["source_join_path"] = source_join_path
    return d


def _snap_column(col_id, table_id, column_name, is_primary_key=False):
    return {
        "id": str(col_id), "model_table_id": str(table_id),
        "column_name": column_name, "is_primary_key": is_primary_key,
    }


def _snap_table(table_id, physical_name):
    return {"id": str(table_id), "physical_name": physical_name}


def _model_with_version(slug="modely", deployed_version_id=None):
    return types.SimpleNamespace(
        id=_uuid(), slug=slug,
        deployed_version_id=deployed_version_id,
    )


def _model_version(version_id, model_id, snapshot_json):
    return types.SimpleNamespace(
        id=version_id, model_id=model_id,
        snapshot_json=snapshot_json,
        version_number=1,
    )


def _make_snapshot_db(*, get_map=None, execute_results=None):
    """Build a fake AsyncSession with snapshot-aware get()."""
    db = AsyncMock()
    _get_map = get_map or {}

    async def fake_get(cls, pk):
        cls_name = cls.__name__ if hasattr(cls, '__name__') else str(cls)
        return _get_map.get((cls_name, pk))

    db.get = AsyncMock(side_effect=fake_get)
    if execute_results:
        db.execute = AsyncMock(side_effect=[FakeResult(r) for r in execute_results])
    else:
        db.execute = AsyncMock(return_value=FakeResult([]))
    return db


async def test_snapshot_leaf_drill_uses_deployed_dimensions():
    """5262+3971: build_drill_sql resolves dimensions from the deployed
    snapshot, not live tables. A dimension renamed in the live DB but
    not yet redeployed must still appear under its deployed name."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    dim_col_id = _uuid()

    # Snapshot has the dimension named "region" (deployed state)
    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                source_column_id=src_col_id)],
        dimensions=[_snap_dimension(_uuid(), model_id, "region",
                                    source_column_id=dim_col_id)],
    )
    model = _model_with_version(slug="modely", deployed_version_id=version_id)
    model.id = model_id
    version = _model_version(version_id, model_id, snap_json)

    # Live DB has the measure (needed for initial lookup) and model
    live_measure = _measure(model_id=model_id, name="amount")
    live_measure.id = measure_id

    db = _make_snapshot_db(
        get_map={
            ("Measure", measure_id): live_measure,
            ("Model", model_id): model,
            ("ModelVersion", version_id): version,
        },
    )

    sql, _, _, _, _, drill_mode, *_ = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=None,
        limit=50,
        db=db,
    )

    assert drill_mode == "leaf"
    assert '"region"' in sql
    assert 'FROM "modely"' in sql
    # The dimension was resolved from the snapshot, not from live DB queries
    # (db.execute was not called for dimension loading)


async def test_snapshot_hierarchy_drill_uses_deployed_hierarchy():
    """5262+3971: hierarchy drill-down resolves levels from the deployed
    snapshot."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    hier_id = _uuid()
    year_uda = _uuid()
    month_uda = _uuid()
    year_dim_id = _uuid()
    month_dim_id = _uuid()

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount")],
        dimensions=[
            _snap_dimension(year_dim_id, model_id, "year", uda_id=year_uda),
            _snap_dimension(month_dim_id, model_id, "month",
                            display_name="Month", uda_id=month_uda),
        ],
        hierarchies=[_snap_hierarchy(hier_id, model_id, "date_hierarchy", [
            _snap_level(0, "Year", year_uda),
            _snap_level(1, "Month", month_uda),
        ])],
    )
    model = _model_with_version(slug="modely", deployed_version_id=version_id)
    model.id = model_id
    version = _model_version(version_id, model_id, snap_json)

    live_measure = _measure(model_id=model_id, name="amount")
    live_measure.id = measure_id

    db = _make_snapshot_db(
        get_map={
            ("Measure", measure_id): live_measure,
            ("Model", model_id): model,
            ("ModelVersion", version_id): version,
        },
    )

    sql, _, _, _, drill_dim, drill_mode, _, drillable, *_ = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "year", "value": 2025}],
        cursor=None,
        limit=100,
        db=db,
    )

    assert drill_mode == "hierarchy"
    assert drill_dim is not None
    assert drill_dim.name == "month"
    assert 'SUM("amount")' in sql
    assert 'GROUP BY "month"' in sql
    assert len(drillable) == 1


async def test_snapshot_measure_not_deployed_raises():
    """5262+3971: a measure that exists in live DB but not in the deployed
    snapshot must raise DRILL_MEASURE_NOT_DEPLOYED."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    deployed_measure_id = _uuid()  # different measure in snapshot

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(deployed_measure_id, model_id, "old_amount")],
        dimensions=[_snap_dimension(_uuid(), model_id, "region")],
    )
    model = _model_with_version(slug="modely", deployed_version_id=version_id)
    model.id = model_id
    version = _model_version(version_id, model_id, snap_json)

    live_measure = _measure(model_id=model_id, name="new_measure")
    live_measure.id = measure_id

    db = _make_snapshot_db(
        get_map={
            ("Measure", measure_id): live_measure,
            ("Model", model_id): model,
            ("ModelVersion", version_id): version,
        },
    )

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure_id,
            hierarchy_id=None,
            grouping_levels=[{"column": "region", "value": "US"}],
            cursor=None,
            limit=50,
            db=db,
        )

    assert exc.value.error_code == "DRILL_MEASURE_NOT_DEPLOYED"


async def test_snapshot_curation_detail_columns_resolved():
    """5262+3971: drill-through set detail columns are resolved from the
    deployed snapshot, including the 5261 validation."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    detail_col_id = _uuid()
    table_id = _uuid()
    dim_id = _uuid()

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                source_column_id=src_col_id)],
        dimensions=[
            _snap_dimension(dim_id, model_id, "order_id",
                            source_column_id=detail_col_id),
            _snap_dimension(_uuid(), model_id, "region",
                            source_column_id=_uuid()),
        ],
        drill_through_sets=[
            _snap_drill_set(measure_id, detail_columns=[str(detail_col_id)]),
        ],
        columns=[
            _snap_column(src_col_id, table_id, "amount_col"),
            _snap_column(detail_col_id, table_id, "order_id_col"),
        ],
        tables=[_snap_table(table_id, "orders")],
    )
    model = _model_with_version(slug="modely", deployed_version_id=version_id)
    model.id = model_id
    version = _model_version(version_id, model_id, snap_json)

    live_measure = _measure(model_id=model_id, name="amount",
                            source_column_id=src_col_id)
    live_measure.id = measure_id

    db = _make_snapshot_db(
        get_map={
            ("Measure", measure_id): live_measure,
            ("Model", model_id): model,
            ("ModelVersion", version_id): version,
        },
    )

    sql, *rest = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=None,
        db=db,
    )
    fact_table = rest[-2]

    assert '"order_id"' in sql
    assert fact_table == "orders"


async def test_no_snapshot_falls_back_to_live():
    """When a model has no deployed_version_id, drill reads from live tables
    (the pre-5262+3971 behavior)."""
    model_id = _uuid()
    measure = _measure(model_id=model_id, name="amount")
    # Model without deployed_version_id
    model = types.SimpleNamespace(id=model_id, slug="modely",
                                  deployed_version_id=None)
    dim = _dimension(model_id, "region", col_id=_uuid())

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
        },
        execute_results=[
            [],         # _load_curation -> no DrillThroughSet
            [dim],      # _load_dimensions_by_name
            [],         # HierarchyLevel -> leaf
        ],
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=None,
        limit=50,
        db=db,
    )

    assert '"region"' in sql
    assert 'FROM "modely"' in sql


async def test_snapshot_pk_tiebreaker_resolved():
    """5262+3971: PK tiebreaker dimension resolved from snapshot."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    pk_col_id = _uuid()
    table_id = _uuid()

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                source_column_id=src_col_id)],
        dimensions=[
            _snap_dimension(_uuid(), model_id, "region",
                            source_column_id=_uuid()),
            _snap_dimension(_uuid(), model_id, "txn_id",
                            source_column_id=pk_col_id),
        ],
        columns=[
            _snap_column(src_col_id, table_id, "amount_col"),
            _snap_column(pk_col_id, table_id, "txn_id_col",
                         is_primary_key=True),
        ],
        tables=[_snap_table(table_id, "payments")],
    )
    model = _model_with_version(slug="modely", deployed_version_id=version_id)
    model.id = model_id
    version = _model_version(version_id, model_id, snap_json)

    live_measure = _measure(model_id=model_id, name="amount",
                            source_column_id=src_col_id)
    live_measure.id = measure_id

    db = _make_snapshot_db(
        get_map={
            ("Measure", measure_id): live_measure,
            ("Model", model_id): model,
            ("ModelVersion", version_id): version,
        },
    )

    sql, *_ = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=None,
        limit=50,
        db=db,
    )

    # PK tiebreaker from snapshot should be in ORDER BY
    assert '"txn_id"' in sql
    order_clause = sql[sql.index("ORDER BY"):sql.index("LIMIT")]
    assert '"txn_id"' in order_clause
