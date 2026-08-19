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
    return types.SimpleNamespace(
        id=_uuid(),
        project_id=_uuid(),
        slug=slug,
        deployed_version_id=None,
        deploy_epoch=0,
        data_epoch=0,
    )


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

    def all(self):
        return self._items


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
        cursor_spec,
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
    assert cursor_spec.stable is True
    assert limit == 100
    assert len(drillable) == 1
    assert drillable[0].hierarchy_name == "business_date"


def _drill_down_db(model_id, hier_id):
    """Staged fake DB matching test_build_drill_sql_hierarchy_drill_down."""
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
        get_map={("Measure", measure.id): measure, ("Model", model_id): model},
        execute_results=[
            [],                          # _load_curation
            [year_dim],                  # _load_dimensions_by_name
            [year_level],                # HierarchyLevel.where(key_attribute_id.in_)
            [hier],                      # HierarchyDefinition.where(id.in_)
            [year_level, month_level],   # all levels by hierarchy
            [month_dim],                 # _resolve_level_dimension next level
        ],
    )
    return db, measure


# Bug-6277: an explicit hierarchy_id that is not drillable from the current
# cell must fail loudly (HIERARCHY_NOT_DRILLABLE), never silently fall through
# to leaf detail mode returning a different product than requested.
async def test_build_drill_sql_non_drillable_hierarchy_id_raises():
    model_id = _uuid()
    hier_id = _uuid()
    db, measure = _drill_down_db(model_id, hier_id)
    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=_uuid(),  # not the drillable hierarchy
            grouping_levels=[{"column": "business_date_year", "value": 2025}],
            limit=100,
            db=db,
        )
    assert exc.value.error_code == "HIERARCHY_NOT_DRILLABLE"


# Bug-6274 [SECURITY]: the persona allow-list filters the drillable set before
# selection, so an explicitly-requested but non-allowed hierarchy cannot drill.
async def test_build_drill_sql_allow_list_blocks_non_allowed_hierarchy():
    model_id = _uuid()
    hier_id = _uuid()
    db, measure = _drill_down_db(model_id, hier_id)
    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure.id,
            hierarchy_id=hier_id,                       # the real drillable hierarchy
            grouping_levels=[{"column": "business_date_year", "value": 2025}],
            limit=100,
            db=db,
            allowed_hierarchy_ids={str(_uuid())},        # but persona forbids it
        )
    assert exc.value.error_code == "HIERARCHY_NOT_DRILLABLE"


async def test_build_drill_sql_allow_list_permits_allowed_hierarchy():
    # Guard against over-block: an allowed hierarchy still drills.
    model_id = _uuid()
    hier_id = _uuid()
    db, measure = _drill_down_db(model_id, hier_id)
    _sql, _mid, _off, _lim, drill_dim, drill_mode, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "business_date_year", "value": 2025}],
        limit=100,
        db=db,
        allowed_hierarchy_ids={str(hier_id)},
    )
    assert drill_mode == "hierarchy"
    assert drill_dim is not None


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
        cursor_spec,
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


async def test_build_drill_sql_uses_keyset_not_offset_on_first_page():
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

    sql, _, cursor_spec, limit, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=None,
        limit=100,
        db=db,
    )

    assert cursor_spec.stable is False  # no PK-backed dimension in this fixture
    assert "OFFSET" not in sql
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
    # F-019-01: a source-table override reached by a NON-expanding (many-to-one)
    # join does NOT multiply the measure, so it is honoured and the join path is
    # emitted for runtime rewrite. (The expanding case is rejected — see
    # test_expanding_source_override_multiplying_measure_is_rejected.)
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
    # many_to_one traversed FROM the fact table (fact=many, override=one): a
    # lookup that does not duplicate the fact measure.
    safe_join = types.SimpleNamespace(
        id=join_id,
        left_table_id=fact_table_id,
        right_table_id=override_table_id,
        join_type="many_to_one",
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
            [ds],           # _load_curation -> DrillThroughSet
            [],             # tiebreaker
            [safe_join],    # F-019-01 cardinality: resolve path joins (live)
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


async def test_expanding_source_override_multiplying_measure_is_rejected():
    """F-019-01 (CRITICAL): a finer-grained source override reached by a
    one-to-many join would repeat the parent fact measure on every child row,
    so summing the projected measure multiplies the clicked cell. The builder
    must refuse the configuration with a coded error rather than return a
    non-reconciling detail set."""
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
    # one_to_many traversed FROM the fact table (fact=one, order_lines=many):
    # each line repeats the order's amount -> SUM multiplies.
    expanding_join = types.SimpleNamespace(
        id=join_id,
        left_table_id=fact_table_id,
        right_table_id=override_table_id,
        join_type="one_to_many",
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
            [ds],               # _load_curation -> DrillThroughSet
            [],                 # tiebreaker
            [expanding_join],   # F-019-01 cardinality: resolve path joins (live)
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
    assert exc.value.error_code == "DRILL_EXPANDING_OVERRIDE_MULTIPLIES_MEASURE"


async def test_unresolvable_join_path_is_rejected():
    """Opus-R1-F2: a saved source_join_path with stale/garbage join IDs that
    don't resolve has unknowable cardinality. The guard rejects rather than
    accepting an unknowable path (fail toward rejection, not wrong numbers)."""
    model_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    source_col_id = _uuid()
    stale_join_id = _uuid()  # no join row for this ID exists
    measure = _measure(
        model_id=model_id, name="amount", source_column_id=source_col_id,
    )
    model = _model(slug="modely")
    fact_table = types.SimpleNamespace(id=fact_table_id, physical_name="orders")
    override_table = types.SimpleNamespace(id=override_table_id, physical_name="order_lines")
    ds = _drill_set(
        source_table_id=override_table_id,
        source_join_path=[str(stale_join_id)],
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
            [ds],   # _load_curation -> DrillThroughSet
            [],     # tiebreaker
            [],     # F-019-01 cardinality: resolve path joins (empty: stale ID)
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
    assert exc.value.error_code == "DRILL_JOIN_PATH_UNRESOLVABLE"


async def test_expanding_override_with_measure_on_leaf_table_is_allowed():
    """F-019-01: the expanding-override guard fires only when the measure's
    value column lives on the COARSER (fact) table. When the measure column is
    physically on the override (leaf) table itself, each leaf row carries its
    own detail value and the sum reconciles — so it is allowed."""
    model_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    source_col_id = _uuid()
    join_id = _uuid()
    # Measure's value column lives on the OVERRIDE (leaf) table.
    measure = _measure(
        model_id=model_id, name="line_amount", source_column_id=source_col_id,
    )
    model = _model(slug="modely")
    fact_table = types.SimpleNamespace(id=fact_table_id, physical_name="orders")
    override_table = types.SimpleNamespace(id=override_table_id, physical_name="order_lines")
    ds = _drill_set(
        source_table_id=override_table_id,
        source_join_path=[str(join_id)],
    )
    expanding_join = types.SimpleNamespace(
        id=join_id,
        left_table_id=fact_table_id,
        right_table_id=override_table_id,
        join_type="one_to_many",
    )

    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelTable", override_table_id): override_table,
            ("ModelTable", fact_table_id): fact_table,
            # Measure column is on the override (leaf) table.
            ("ModelColumn", source_col_id): types.SimpleNamespace(
                model_table_id=override_table_id
            ),
        },
        execute_results=[
            [ds],           # _load_curation -> DrillThroughSet
            [],             # tiebreaker
            [_dimension(model_id, "region", col_id=_uuid())],
            [],
        ],
    )

    # No _resolve_path_joins_live call happens because the measure column is on
    # the override table (guard short-circuits before the cardinality query), so
    # the build succeeds and emits the join path.
    sql, *rest = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        cursor=None,
        limit=None,
        db=db,
    )
    assert rest[-1] == [str(join_id)]


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
    # ORDER BY must precede LIMIT so the keyset boundary and result order agree.
    assert sql.index("ORDER BY") < sql.index("LIMIT")


async def test_leaf_order_by_is_total_order_not_constant_only():
    """Bug-1108: leaf ORDER BY must not be the constant cell coordinate only.

    The cell dimension value is constant across every contributing row, so an
    ORDER BY over it alone is not a total order and continuation pages can
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
            [(src_col_id, "txn_id_col", "txn_id")],
            # _resolve_pk_tiebreaker_dims -> complete PK tuple
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
    assert order_clause.rstrip().endswith('"txn_id" ASC NULLS LAST')
    assert '"account_type"' in order_clause
    assert '"amount"' in order_clause


async def test_live_composite_pk_uses_full_tuple_for_repeated_first_component():
    """R1 HIGH: a composite PK prefix is not unique; continuation must compare
    the second component when adjacent rows repeat the first component."""
    model_id = _uuid()
    src_col_id = _uuid()
    table_id = _uuid()
    pk_a_id = _uuid()
    pk_b_id = _uuid()
    measure = _measure(
        model_id=model_id, name="amount", source_column_id=src_col_id,
    )
    model = _model(slug="modely")
    grouping_dim = _dimension(model_id, "account_type", col_id=_uuid())
    src_col = types.SimpleNamespace(id=src_col_id, model_table_id=table_id)
    src_table = types.SimpleNamespace(id=table_id, physical_name="demo.payment")

    def make_db():
        return _make_db(
            get_map={
                ("Measure", measure.id): measure,
                ("Model", model_id): model,
                ("ModelColumn", src_col_id): src_col,
                ("ModelTable", table_id): src_table,
            },
            execute_results=[
                [],
                [
                    (pk_a_id, "line_id", "line_key"),
                    (pk_b_id, "tenant_id", "tenant_key"),
                ],
                [grouping_dim],
                [],
            ],
        )

    first_sql, _, spec, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "account_type", "value": "WALLET"}],
        cursor=None,
        limit=2,
        db=make_db(),
    )
    assert spec.stable is True
    first_order = first_sql[first_sql.index("ORDER BY"):first_sql.index("LIMIT")]
    assert first_order.index('"line_key"') < first_order.index('"tenant_key"')

    token = spec.encode({
        "account_type": "WALLET",
        "amount": 10,
        "line_key": 7,
        "tenant_key": 41,
    })
    continued_sql, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "account_type", "value": "WALLET"}],
        cursor=token,
        limit=2,
        db=make_db(),
    )
    assert '"line_key" = 7' in continued_sql
    assert '"tenant_key" > 41' in continued_sql


async def test_live_composite_pk_missing_component_is_not_stable():
    model_id = _uuid()
    src_col_id = _uuid()
    table_id = _uuid()
    measure = _measure(
        model_id=model_id, name="amount", source_column_id=src_col_id,
    )
    model = _model(slug="modely")
    grouping_dim = _dimension(model_id, "account_type", col_id=_uuid())
    db = _make_db(
        get_map={
            ("Measure", measure.id): measure,
            ("Model", model_id): model,
            ("ModelColumn", src_col_id): types.SimpleNamespace(
                id=src_col_id, model_table_id=table_id,
            ),
            ("ModelTable", table_id): types.SimpleNamespace(
                id=table_id, physical_name="demo.payment",
            ),
        },
        execute_results=[
            [],
            [
                (_uuid(), "line_id", "line_key"),
                (_uuid(), "tenant_id", None),
            ],
            [grouping_dim],
            [],
        ],
    )

    _sql, _model_id, spec, *_ = await build_drill_sql(
        measure_id=measure.id,
        hierarchy_id=None,
        grouping_levels=[{"column": "account_type", "value": "WALLET"}],
        cursor=None,
        limit=2,
        db=db,
    )
    assert spec.stable is False


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
              columns=None, tables=None, joins=None):
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
        "joins": joins or [],
    }


def _snap_join(join_id, left_table_id, right_table_id, join_type="many_to_one"):
    return {
        "id": str(join_id),
        "left_table_id": str(left_table_id),
        "right_table_id": str(right_table_id),
        "join_type": join_type,
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


async def test_snapshot_composite_pk_continuation_uses_every_component():
    """R1 HIGH snapshot parity: a repeated first PK component advances on the
    second component rather than skipping or repeating the row."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    pk_a_id = _uuid()
    pk_b_id = _uuid()
    table_id = _uuid()
    grouping_col_id = _uuid()

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(
            measure_id, model_id, "amount", source_column_id=src_col_id,
        )],
        dimensions=[
            _snap_dimension(
                _uuid(), model_id, "region", source_column_id=grouping_col_id,
            ),
            _snap_dimension(
                _uuid(), model_id, "line_key", source_column_id=pk_a_id,
            ),
            _snap_dimension(
                _uuid(), model_id, "tenant_key", source_column_id=pk_b_id,
            ),
        ],
        columns=[
            _snap_column(src_col_id, table_id, "amount_col"),
            _snap_column(grouping_col_id, table_id, "region_col"),
            _snap_column(pk_b_id, table_id, "tenant_id", is_primary_key=True),
            _snap_column(pk_a_id, table_id, "line_id", is_primary_key=True),
        ],
        tables=[_snap_table(table_id, "payments")],
    )
    model = _model_with_version(slug="modely", deployed_version_id=version_id)
    model.id = model_id
    version = _model_version(version_id, model_id, snap_json)
    live_measure = _measure(
        model_id=model_id, name="amount", source_column_id=src_col_id,
    )
    live_measure.id = measure_id

    def make_db():
        return _make_snapshot_db(
            get_map={
                ("Measure", measure_id): live_measure,
                ("Model", model_id): model,
                ("ModelVersion", version_id): version,
            },
        )

    sql, _, spec, *_ = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=None,
        limit=2,
        db=make_db(),
    )
    assert spec.stable is True
    order_clause = sql[sql.index("ORDER BY"):sql.index("LIMIT")]
    assert order_clause.index('"line_key"') < order_clause.index('"tenant_key"')

    token = spec.encode({
        "region": "US",
        "amount": 10,
        "line_key": 7,
        "tenant_key": 41,
    })
    continued_sql, *_ = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=token,
        limit=2,
        db=make_db(),
    )
    assert '"line_key" = 7' in continued_sql
    assert '"tenant_key" > 41' in continued_sql


async def test_snapshot_composite_pk_missing_dimension_refuses_stability():
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    pk_a_id = _uuid()
    pk_b_id = _uuid()
    table_id = _uuid()
    region_col_id = _uuid()
    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(
            measure_id, model_id, "amount", source_column_id=src_col_id,
        )],
        dimensions=[
            _snap_dimension(
                _uuid(), model_id, "region", source_column_id=region_col_id,
            ),
            _snap_dimension(
                _uuid(), model_id, "line_key", source_column_id=pk_a_id,
            ),
        ],
        columns=[
            _snap_column(src_col_id, table_id, "amount_col"),
            _snap_column(region_col_id, table_id, "region_col"),
            _snap_column(pk_a_id, table_id, "line_id", is_primary_key=True),
            _snap_column(pk_b_id, table_id, "tenant_id", is_primary_key=True),
        ],
        tables=[_snap_table(table_id, "payments")],
    )
    model = _model_with_version(slug="modely", deployed_version_id=version_id)
    model.id = model_id
    version = _model_version(version_id, model_id, snap_json)
    live_measure = _measure(
        model_id=model_id, name="amount", source_column_id=src_col_id,
    )
    live_measure.id = measure_id
    db = _make_snapshot_db(get_map={
        ("Measure", measure_id): live_measure,
        ("Model", model_id): model,
        ("ModelVersion", version_id): version,
    })

    _sql, _model_id, spec, *_ = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=None,
        limit=2,
        db=db,
    )
    assert spec.stable is False


# ===========================================================================
# F-019-01 — expanding-override multiplication guard (snapshot path)
# ===========================================================================


def test_path_cardinality_reads_the_declared_cardinality_field():
    """Join-orientation contract (invariant 3): the classification reads
    ``Join.cardinality``, not the orientation field the two used to share.

    A snapshot join carrying a real orientation token (everything the write
    API has accepted since Bug-7775) used to classify as "mixed" whatever its
    true fan-out was, so the expanding-override guard refused valid overrides.
    A snapshot written before the split has no ``cardinality`` key at all, and
    must keep falling back to the legacy token in ``join_type``.
    """
    from src.drill.semantic_builder import _path_cardinality_from

    fact = _uuid()
    dim = _uuid()

    declared = types.SimpleNamespace(
        left_table_id=fact, right_table_id=dim,
        join_type="left", cardinality="many_to_one",
    )
    assert _path_cardinality_from(fact, [declared]) == "many-to-one"

    undeclared = types.SimpleNamespace(
        left_table_id=fact, right_table_id=dim,
        join_type="left", cardinality=None,
    )
    assert _path_cardinality_from(fact, [undeclared]) == "mixed", (
        "an undeclared fan-out is UNKNOWN, and an expanding hop repeats the "
        "parent measure across child rows — the guard must stay closed"
    )

    legacy = types.SimpleNamespace(
        left_table_id=fact, right_table_id=dim, join_type="many_to_one",
    )
    assert _path_cardinality_from(fact, [legacy]) == "many-to-one"


def test_path_cardinality_from_classifies_expansion():
    """Unit: _path_cardinality_from mirrors model-service cardinality — a
    one-to-many hop FROM the fact table is 'one-to-many' (expanding)."""
    from src.drill.semantic_builder import _path_cardinality_from

    fact = _uuid()
    lines = _uuid()
    # Forward traversal fact->lines, one_to_many edge.
    j_fwd = types.SimpleNamespace(
        left_table_id=fact, right_table_id=lines, join_type="one_to_many",
    )
    assert _path_cardinality_from(fact, [j_fwd]) == "one-to-many"

    # many_to_one lookup fact->dim: non-expanding.
    dim = _uuid()
    j_lookup = types.SimpleNamespace(
        left_table_id=fact, right_table_id=dim, join_type="many_to_one",
    )
    assert _path_cardinality_from(fact, [j_lookup]) == "many-to-one"

    # Reverse traversal inverts: a many_to_one edge stored lines->fact, but
    # traversed FROM fact, becomes one-to-many (expanding).
    j_rev = types.SimpleNamespace(
        left_table_id=lines, right_table_id=fact, join_type="many_to_one",
    )
    assert _path_cardinality_from(fact, [j_rev]) == "one-to-many"

    # Empty path: nothing to expand.
    assert _path_cardinality_from(fact, []) == "none"

    # Fable-R1-F1: multi-hop path in fact→override order (the reversed
    # persisted path). Two hops: fact → mid (many_to_one) → lines
    # (one_to_many). Mixed cardinality.
    mid = _uuid()
    j1 = types.SimpleNamespace(
        left_table_id=fact, right_table_id=mid, join_type="many_to_one",
    )
    j2 = types.SimpleNamespace(
        left_table_id=mid, right_table_id=lines, join_type="one_to_many",
    )
    assert _path_cardinality_from(fact, [j1, j2]) == "mixed"

    # Two-hop all many_to_one (collapsing): safe.
    dim2 = _uuid()
    j3 = types.SimpleNamespace(
        left_table_id=fact, right_table_id=mid, join_type="many_to_one",
    )
    j4 = types.SimpleNamespace(
        left_table_id=mid, right_table_id=dim2, join_type="many_to_one",
    )
    assert _path_cardinality_from(fact, [j3, j4]) == "many-to-one"

    # Two-hop all one_to_many (expanding): unsafe.
    j5 = types.SimpleNamespace(
        left_table_id=fact, right_table_id=mid, join_type="one_to_many",
    )
    j6 = types.SimpleNamespace(
        left_table_id=mid, right_table_id=lines, join_type="one_to_many",
    )
    assert _path_cardinality_from(fact, [j5, j6]) == "one-to-many"


async def test_snapshot_expanding_source_override_is_rejected():
    """F-019-01: on the DEPLOYED (snapshot) path, an expanding one-to-many
    source override that would multiply the parent fact measure is rejected."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    dim_col_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    join_id = _uuid()

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                source_column_id=src_col_id)],
        dimensions=[_snap_dimension(_uuid(), model_id, "region",
                                    source_column_id=dim_col_id)],
        columns=[
            # Measure's value column lives on the FACT (coarser) table.
            _snap_column(src_col_id, fact_table_id, "amount"),
            _snap_column(dim_col_id, override_table_id, "region"),
        ],
        tables=[
            _snap_table(fact_table_id, "orders"),
            _snap_table(override_table_id, "order_lines"),
        ],
        drill_through_sets=[_snap_drill_set(
            measure_id,
            source_table_id=override_table_id,
            source_join_path=[str(join_id)],
        )],
        joins=[_snap_join(join_id, fact_table_id, override_table_id, "one_to_many")],
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

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure_id,
            hierarchy_id=None,
            grouping_levels=[{"column": "region", "value": "US"}],
            cursor=None,
            limit=50,
            db=db,
        )
    assert exc.value.error_code == "DRILL_EXPANDING_OVERRIDE_MULTIPLIES_MEASURE"


async def test_snapshot_nonexpanding_source_override_is_allowed():
    """F-019-01: a many-to-one (non-expanding) source override on the snapshot
    path does not multiply the measure and is honoured."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    dim_col_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    join_id = _uuid()

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                source_column_id=src_col_id)],
        dimensions=[_snap_dimension(_uuid(), model_id, "region",
                                    source_column_id=dim_col_id)],
        columns=[
            _snap_column(src_col_id, fact_table_id, "amount"),
            _snap_column(dim_col_id, override_table_id, "region"),
        ],
        tables=[
            _snap_table(fact_table_id, "orders"),
            _snap_table(override_table_id, "customers"),
        ],
        drill_through_sets=[_snap_drill_set(
            measure_id,
            source_table_id=override_table_id,
            source_join_path=[str(join_id)],
        )],
        joins=[_snap_join(join_id, fact_table_id, override_table_id, "many_to_one")],
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

    sql, *_ = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=None,
        limit=50,
        db=db,
    )
    # Build succeeds; leaf projection present.
    assert 'FROM "modely"' in sql


async def test_snapshot_unresolvable_join_path_is_rejected():
    """Opus-R1-F2 (snapshot path): a saved source_join_path with stale join
    IDs that don't exist in the snapshot is rejected (unknowable cardinality)."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    dim_col_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    stale_join_id = _uuid()  # no join with this ID in the snapshot

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                source_column_id=src_col_id)],
        dimensions=[_snap_dimension(_uuid(), model_id, "region",
                                    source_column_id=dim_col_id)],
        columns=[
            _snap_column(src_col_id, fact_table_id, "amount"),
            _snap_column(dim_col_id, override_table_id, "region"),
        ],
        tables=[
            _snap_table(fact_table_id, "orders"),
            _snap_table(override_table_id, "order_lines"),
        ],
        drill_through_sets=[_snap_drill_set(
            measure_id,
            source_table_id=override_table_id,
            source_join_path=[str(stale_join_id)],
        )],
        joins=[],  # no join rows — the saved ID is stale
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

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure_id,
            hierarchy_id=None,
            grouping_levels=[{"column": "region", "value": "US"}],
            cursor=None,
            limit=50,
            db=db,
        )
    assert exc.value.error_code == "DRILL_JOIN_PATH_UNRESOLVABLE"


async def test_snapshot_multihop_expanding_override_in_persisted_order_is_rejected():
    """Fable-R1-F1: a 2-hop expanding path in persisted (override->fact) order
    must be correctly classified after reversal. The persisted order is
    lines->mid->fact, which reversed is fact->mid->lines. If fact->mid is
    many_to_one and mid->lines is one_to_many, the path expands and must be
    rejected."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    dim_col_id = _uuid()
    fact_table_id = _uuid()
    mid_table_id = _uuid()
    override_table_id = _uuid()
    join1_id = _uuid()
    join2_id = _uuid()

    # Persisted order: override->mid->fact. Join1: lines->mid (many_to_one).
    # Join2: mid->fact (many_to_one). Reversed from fact: fact->mid
    # (one_to_many) -> lines (one_to_many) -> expanding.
    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                source_column_id=src_col_id)],
        dimensions=[_snap_dimension(_uuid(), model_id, "region",
                                    source_column_id=dim_col_id)],
        columns=[
            _snap_column(src_col_id, fact_table_id, "amount"),
            _snap_column(dim_col_id, override_table_id, "region"),
        ],
        tables=[
            _snap_table(fact_table_id, "orders"),
            _snap_table(mid_table_id, "intermediate"),
            _snap_table(override_table_id, "order_lines"),
        ],
        drill_through_sets=[_snap_drill_set(
            measure_id,
            source_table_id=override_table_id,
            # Persisted order: override(lines) -> mid -> fact
            source_join_path=[str(join1_id), str(join2_id)],
        )],
        joins=[
            # lines -> mid: many_to_one (from override perspective)
            _snap_join(join1_id, override_table_id, mid_table_id, "many_to_one"),
            # mid -> fact: many_to_one (from override perspective)
            _snap_join(join2_id, mid_table_id, fact_table_id, "many_to_one"),
        ],
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

    with pytest.raises(DrillSemanticError) as exc:
        await build_drill_sql(
            measure_id=measure_id,
            hierarchy_id=None,
            grouping_levels=[{"column": "region", "value": "US"}],
            cursor=None,
            limit=50,
            db=db,
        )
    assert exc.value.error_code == "DRILL_EXPANDING_OVERRIDE_MULTIPLIES_MEASURE"


async def test_snapshot_multihop_nonexpanding_override_is_allowed():
    """Fable-R1-F1: a 2-hop non-expanding path in persisted order must be
    correctly classified after reversal and allowed."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    src_col_id = _uuid()
    dim_col_id = _uuid()
    fact_table_id = _uuid()
    mid_table_id = _uuid()
    override_table_id = _uuid()
    join1_id = _uuid()
    join2_id = _uuid()

    # Persisted order: override->mid->fact. Join1: customers->mid
    # (one_to_many). Join2: mid->fact (one_to_many). Reversed from fact:
    # fact->mid (many_to_one) -> customers (many_to_one) -> collapsing (safe).
    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                source_column_id=src_col_id)],
        dimensions=[_snap_dimension(_uuid(), model_id, "region",
                                    source_column_id=dim_col_id)],
        columns=[
            _snap_column(src_col_id, fact_table_id, "amount"),
            _snap_column(dim_col_id, override_table_id, "region"),
        ],
        tables=[
            _snap_table(fact_table_id, "orders"),
            _snap_table(mid_table_id, "intermediate"),
            _snap_table(override_table_id, "customers"),
        ],
        drill_through_sets=[_snap_drill_set(
            measure_id,
            source_table_id=override_table_id,
            source_join_path=[str(join1_id), str(join2_id)],
        )],
        joins=[
            _snap_join(join1_id, override_table_id, mid_table_id, "one_to_many"),
            _snap_join(join2_id, mid_table_id, fact_table_id, "one_to_many"),
        ],
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

    sql, *_ = await build_drill_sql(
        measure_id=measure_id,
        hierarchy_id=None,
        grouping_levels=[{"column": "region", "value": "US"}],
        cursor=None,
        limit=50,
        db=db,
    )
    assert 'FROM "modely"' in sql


async def test_snapshot_uda_measure_with_override_fails_closed():
    """Fable-R1-F2: a UDA-backed measure on the deployed-snapshot path has
    intrinsic_table = None (snapshot doesn't embed UDA table IDs). When a
    source-table override with a join path is present, the guard cannot verify
    cardinality and must fail closed rather than silently accept an unknowable
    expanding path."""
    model_id = _uuid()
    version_id = _uuid()
    measure_id = _uuid()
    uda_id = _uuid()
    dim_col_id = _uuid()
    fact_table_id = _uuid()
    override_table_id = _uuid()
    join_id = _uuid()

    snap_json = _snapshot(
        model_id=model_id,
        measures=[_snap_measure(measure_id, model_id, "amount",
                                uda_id=uda_id)],  # UDA measure, no source_column_id
        dimensions=[_snap_dimension(_uuid(), model_id, "region",
                                    source_column_id=dim_col_id)],
        columns=[
            _snap_column(dim_col_id, override_table_id, "region"),
        ],
        tables=[
            _snap_table(fact_table_id, "orders"),
            _snap_table(override_table_id, "order_lines"),
        ],
        drill_through_sets=[_snap_drill_set(
            measure_id,
            source_table_id=override_table_id,
            source_join_path=[str(join_id)],
        )],
        joins=[_snap_join(join_id, fact_table_id, override_table_id, "one_to_many")],
    )
    model = _model_with_version(slug="modely", deployed_version_id=version_id)
    model.id = model_id
    version = _model_version(version_id, model_id, snap_json)

    # UDA measure -- no source_column_id -> snapshot intrinsic table is None.
    live_measure = _uda_measure(model_id=model_id, name="amount", uda_id=uda_id)
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
    assert exc.value.error_code == "DRILL_INTRINSIC_TABLE_UNRESOLVABLE"
