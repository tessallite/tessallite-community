"""Tests for column-level persona tag restriction (H4 fix).

Verifies that _check_column_restrictions correctly uses
source_column_id (not model_column_id) to match restricted columns,
fails CLOSED for complex SQL (F-008-03), silently narrows SELECT *
(F-008-05), and composes with row security (F-008-02).
"""
from __future__ import annotations

import types
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import src.routing.router as router_mod
from src.routing.router import _check_column_restrictions, route_query
from src.ir.logical_query import (
    BoundQuery,
    LogicalFilter,
    LogicalQuery,
    RouteDecision,
    SelectExpression,
)


def _make_bq(
    measures: list,
    dimensions: list,
    *,
    has_complex_sql: bool = False,
    select_star: bool = False,
    allowed_physical_columns: set[str] | None = None,
    filters: list | None = None,
    dimensions_by_name: dict | None = None,
    order_by: list | None = None,
    has_unresolvable_order: bool = False,
    has_unresolvable_where: bool = False,
    raw_query: str = "SELECT ...",
    bound_derived_expressions: list | None = None,
) -> BoundQuery:
    filters = filters or []
    lq = LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query=raw_query,
        requested_measures=[m.name for m in measures],
        requested_dimensions=[d.name for d in dimensions],
        filters=list(filters),
        grain=[d.name for d in dimensions],
        order_by=list(order_by or []),
        limit=None,
        offset=None,
        query_fingerprint="abc",
        has_complex_sql=has_complex_sql,
        has_unresolvable_order=has_unresolvable_order,
        has_unresolvable_where=has_unresolvable_where,
        select_star=select_star,
    )
    model = types.SimpleNamespace(id="model-1", slug="test")
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=measures,
        resolved_dimensions=dimensions,
        resolved_filters=list(filters),
        resolved_dimensions_by_name=dimensions_by_name or {},
        allowed_physical_columns=allowed_physical_columns or set(),
        bound_derived_expressions=list(bound_derived_expressions or []),
    )


def _filter(dimension_name: str, operator: str = "gt", value=0):
    return LogicalFilter(dimension_name=dimension_name, operator=operator, value=value)


def _scalar_result(items: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


def _rows_result(rows: list) -> MagicMock:
    """A result whose ``.all()`` returns row tuples (used for the model table
    (physical_name, alias) query in the calc-dimension CLS lookups)."""
    result = MagicMock()
    result.all.return_value = rows
    return result


@pytest.mark.asyncio
async def test_dimension_blocked_via_source_column_id():
    restricted_col_id = uuid4()
    dim = types.SimpleNamespace(
        name="secret_region",
        source_column_id=restricted_col_id,
    )
    bq = _make_bq([], [dim])

    tag_id = uuid4()
    persona = types.SimpleNamespace(id=uuid4())

    tag_result = MagicMock()
    tag_result.scalars.return_value.all.return_value = [tag_id]
    col_result = MagicMock()
    col_result.scalars.return_value.all.return_value = [restricted_col_id]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[tag_result, col_result])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "secret_region" in blocked


@pytest.mark.asyncio
async def test_unrestricted_dimension_passes():
    dim = types.SimpleNamespace(
        name="public_region",
        source_column_id=uuid4(),
    )
    bq = _make_bq([], [dim])

    persona = types.SimpleNamespace(id=uuid4())

    tag_result = MagicMock()
    tag_result.scalars.return_value.all.return_value = [uuid4()]
    col_result = MagicMock()
    col_result.scalars.return_value.all.return_value = [uuid4()]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[tag_result, col_result])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "public_region" not in blocked


# ---------------------------------------------------------------------------
# F-008-03 — complex SQL must fail CLOSED when tag restrictions exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complex_sql_with_restrictions_fails_closed():
    """A persona restricted from a tagged column must not be able to read
    it by wrapping the query in a subquery/CTE: with restrictions present,
    complex SQL is rejected outright (fail closed), never executed
    unrestricted."""
    restricted_col_id = uuid4()
    bq = _make_bq([], [], has_complex_sql=True)
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # persona has a restricted tag
        _scalar_result([restricted_col_id]),  # tag has columns
    ])

    with pytest.raises(HTTPException) as exc:
        await _check_column_restrictions(bq, persona, db)

    assert exc.value.status_code == 403
    # Complex-SQL CLS rejection keeps the COLUMN_RESTRICTED code (F-008-02
    # non-disclosure applies to the message: it no longer names the persona).
    assert exc.value.detail["error_code"] == "COLUMN_RESTRICTED"
    assert "rejected" in exc.value.detail["message"].lower()


@pytest.mark.asyncio
async def test_complex_sql_without_restrictions_passes():
    """Personas without tag restrictions keep the existing complex-SQL
    passthrough behaviour."""
    bq = _make_bq([], [], has_complex_sql=True)
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar_result([])])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


# ---------------------------------------------------------------------------
# F-008-05 — SELECT * silently drops restricted columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_star_narrows_restricted_columns():
    """Per the documented CLS contract, SELECT * does not 403 — the
    restricted column is silently absent from the results while the other
    columns survive."""
    restricted_col_id = uuid4()
    restricted_dim = types.SimpleNamespace(
        name="email", source_column_id=restricted_col_id,
    )
    open_dim = types.SimpleNamespace(name="city", source_column_id=uuid4())
    bq = _make_bq(
        [], [restricted_dim, open_dim],
        select_star=True,
        allowed_physical_columns={"email", "city"},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["email"]),            # physical names of restricted ids
    ])

    blocked = await _check_column_restrictions(bq, persona, db)

    assert blocked == []
    assert [d.name for d in bq.resolved_dimensions] == ["city"]
    assert bq.persona_narrowed_star is True
    assert "email" not in bq.allowed_physical_columns
    assert "city" in bq.allowed_physical_columns


@pytest.mark.asyncio
async def test_select_star_without_restricted_columns_untouched():
    """A star query that touches no restricted column is not narrowed."""
    open_dim = types.SimpleNamespace(name="city", source_column_id=uuid4())
    bq = _make_bq([], [open_dim], select_star=True)
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([uuid4()]),  # restricted ids not in the query
    ])

    blocked = await _check_column_restrictions(bq, persona, db)

    assert blocked == []
    assert [d.name for d in bq.resolved_dimensions] == ["city"]
    assert bq.persona_narrowed_star is False


# ---------------------------------------------------------------------------
# F-008-02 — active row security must NOT bypass column-level security
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rls_active_does_not_bypass_column_restrictions(monkeypatch):
    """Both protections apply together: a caller under active row-security
    rules requesting a tag-restricted column gets 403 — the RLS source
    route must never run before the CLS gate."""
    restricted_col_id = uuid4()
    dim = types.SimpleNamespace(name="email", source_column_id=restricted_col_id)
    bq = _make_bq([], [dim])
    persona = types.SimpleNamespace(id=uuid4(), bypass_row_security=False)

    # Row security IS active for this principal. If the CLS gate were
    # (re)moved below the RLS early-return, route_query would return the
    # RLS decision instead of raising 403.
    compiled = types.SimpleNamespace(active_rule_ids=["rule-1"], applied_rules=[])
    monkeypatch.setattr(
        router_mod, "compile_row_security", AsyncMock(return_value=compiled)
    )
    monkeypatch.setattr(router_mod, "has_active_rules", lambda c: True)
    monkeypatch.setattr(
        router_mod, "resolve_target_dialect_for_bound",
        AsyncMock(return_value="postgres"),
    )
    rls_route = AsyncMock(
        return_value=RouteDecision(
            route_type="source", rewritten_query="SELECT 1",
            reason="rls", aggregate_id=None,
        )
    )
    monkeypatch.setattr(router_mod, "_route_with_row_security", rls_route)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
    ])

    with pytest.raises(HTTPException) as exc:
        await route_query(
            bq, db,
            principal=types.SimpleNamespace(user_identity="viewer@test"),
            persona=persona,
        )

    assert exc.value.status_code == 403
    # F-008-02: non-disclosing 403 — the block fires but the restricted
    # column name must not leak to the client.
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    assert "columns" not in exc.value.detail
    assert "email" not in exc.value.detail.get("message", "")
    rls_route.assert_not_called()


# ---------------------------------------------------------------------------
# F-008-06 — closure enforcement: calculated / variant / UDA-backed objects
# referencing restricted columns are blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculated_measure_referencing_restricted_column_blocked():
    """A calculated measure whose expression reads a restricted column must
    be blocked even though the measure itself carries no source_column_id."""
    restricted_col_id = uuid4()
    base_id = uuid4()
    base_measure = types.SimpleNamespace(
        id=base_id, name="salary_total", source_column_id=restricted_col_id,
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None,
    )
    calc_measure = types.SimpleNamespace(
        id=uuid4(), name="avg_salary_index", source_column_id=None,
        measure_type="calculated",
        expression='measure("salary_total") / 100',
        variant_of_measure_id=None, user_defined_attribute_id=None,
    )
    bq = _make_bq([calc_measure], [])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result([]),                   # UDA refs touching restricted
        _scalar_result([base_measure]),       # model measures (closure map)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "avg_salary_index" in blocked


@pytest.mark.asyncio
async def test_calculated_measure_with_clean_closure_passes():
    """A calculated measure whose references touch no restricted column
    keeps working."""
    base_measure = types.SimpleNamespace(
        id=uuid4(), name="shipments", source_column_id=uuid4(),
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None,
    )
    calc_measure = types.SimpleNamespace(
        id=uuid4(), name="shipments_pct", source_column_id=None,
        measure_type="calculated", expression='measure("shipments") * 100',
        variant_of_measure_id=None, user_defined_attribute_id=None,
    )
    bq = _make_bq([calc_measure], [])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([uuid4()]),       # restricted ids: unrelated column
        _scalar_result([]),              # UDA refs
        _scalar_result([base_measure]),  # model measures
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_variant_measure_of_restricted_base_blocked():
    """A time-variant measure (e.g. YTD) of a measure built on a restricted
    column is blocked through the base-measure closure."""
    restricted_col_id = uuid4()
    base_id = uuid4()
    base_measure = types.SimpleNamespace(
        id=base_id, name="salary_total", source_column_id=restricted_col_id,
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None,
    )
    variant = types.SimpleNamespace(
        id=uuid4(), name="salary_total_ytd", source_column_id=None,
        measure_type="standard", variant_of_measure_id=base_id,
        user_defined_attribute_id=None, expression=None,
    )
    bq = _make_bq([variant], [])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result([]),              # UDA refs
        _scalar_result([base_measure]),  # model measures
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary_total_ytd" in blocked


# ---------------------------------------------------------------------------
# Bug-7607 — a CALCULATED DIMENSION whose expression references a restricted
# physical column must be blocked at the router runtime for a persona lacking
# access. The Fable review confirmed the runtime path is correct but had NO
# test exercising a calc DIMENSION (only calc measures) — this is that guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculated_dimension_referencing_restricted_column_blocked():
    """A projected calc dimension (source_column_id NULL, carries calc_expression)
    whose expression reads a restricted physical column must be blocked — the
    restricted value must not surface through the derived dimension."""
    restricted_col_id = uuid4()
    # calc_expression references the restricted physical column `salary`.
    calc_dim = types.SimpleNamespace(
        name="salary_band",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="CASE WHEN salary > 100000 THEN 'high' ELSE 'low' END",
    )
    bq = _make_bq([], [calc_dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names (needs_phys_names)
        _scalar_result(["salary", "region", "dept"]),  # ALL model physical names
        _rows_result([("employees", "emp")]),          # model table (physical, alias) rows
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary_band" in blocked


@pytest.mark.asyncio
async def test_calculated_dimension_qualified_restricted_column_blocked():
    """A calc dimension referencing the restricted column with a table qualifier
    (`emp.salary`) must still be blocked — the closure matches on the base column
    name, mirroring what the rewriter emits."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="salary_band",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="LOWER(emp.salary)",
    )
    bq = _make_bq([], [calc_dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result(["salary"]),
        _scalar_result(["salary", "region"]),  # ALL model physical names
        _rows_result([("employees", "emp")]),  # model table rows
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary_band" in blocked


@pytest.mark.asyncio
async def test_calculated_dimension_whole_row_reference_fails_closed():
    """Codex R1 finding 1: ``CAST(emp AS TEXT)`` serialises the WHOLE ``emp`` row
    (every column, restricted included) but a name-only match sees only the
    identifier ``emp``. Since ``emp`` is NOT a known model physical column, the
    gate must fail closed and block — the restricted values must not leak through
    a whole-row expansion."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="row_dump",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="CAST(emp AS TEXT)",
    )
    bq = _make_bq([], [calc_dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result(["salary"]),
        # Known columns do NOT include `emp` (which is a table/row, not a column).
        _scalar_result(["salary", "region", "dept"]),
        _rows_result([("emp", None)]),  # `emp` is a table -> row ref
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "row_dump" in blocked


@pytest.mark.asyncio
async def test_calculated_dimension_star_reference_fails_closed():
    """Codex R1 finding 1: a star (``emp.*`` / ``*``) expands to every column and
    must fail closed."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="star_dump",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="ROW(emp.*)",
    )
    bq = _make_bq([], [calc_dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result(["salary"]),
        _scalar_result(["salary", "region"]),
        _rows_result([("employees", "emp")]),
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "star_dump" in blocked


@pytest.mark.asyncio
async def test_calculated_dimension_clean_expression_not_blocked():
    """A calc dimension whose expression references only KNOWN, non-restricted
    columns passes — no false positive."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="region_bucket",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="UPPER(region)",
    )
    bq = _make_bq([], [calc_dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result(["salary"]),  # restricted col is `salary`, not `region`
        _scalar_result(["salary", "region", "dept"]),  # `region` IS a known col
        _rows_result([("employees", "emp")]),  # `region` is NOT a table -> clean
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_calculated_dimension_unparseable_expression_fails_closed():
    """A calc dimension whose expression cannot be parsed must FAIL CLOSED
    (blocked) rather than execute an unverified expression over a possibly
    restricted column."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="broken_calc",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression=")(*&^ not valid sql %$#",
    )
    bq = _make_bq([], [calc_dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result(["salary"]),
        _scalar_result(["salary", "region"]),
        _rows_result([("employees", "emp")]),
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "broken_calc" in blocked


@pytest.mark.asyncio
async def test_clean_calc_dimension_in_filter_not_over_blocked():
    """R2-1: a CLEAN calc dimension (references only the non-restricted `region`)
    used ONLY in a WHERE filter must NOT be over-blocked. The filter gate must
    populate known_physical_names so the gate can verify `region` is a real
    column instead of failing closed on a missing lookup."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="region_bucket",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="UPPER(region)",
    )
    bq = _make_bq(
        [], [],
        filters=[_filter("region_bucket", "eq", "EMEA")],
        dimensions_by_name={"region_bucket": calc_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        # _ensure_cls_physical_lookups (filter path): restricted, then known, then tables.
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result(["salary", "region", "dept"]),  # ALL model physical names
        _rows_result([("orders", "o"), ("employees", "emp")]),  # table rows
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_clean_calc_dimension_freshly_loaded_in_order_by_not_over_blocked():
    """R2-1 (R3): a CLEAN calc dimension referenced ONLY in ORDER BY and NOT in
    the resolved set is loaded fresh from the model inside the order-by resolver.
    The lookups must be (re)loaded after that fresh load so the calc-expr gate
    can verify `region` is a real column instead of failing closed (over-block)."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="region_bucket",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="UPPER(region)",
    )
    bq = _make_bq(
        [], [],
        order_by=[("region_bucket", "desc")],  # order-only, not projected/resolved
        dimensions_by_name={},                  # NOT pre-resolved -> loaded fresh
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        # order-by resolver pre-load: no calc dim known yet -> only restricted phys.
        _scalar_result(["salary"]),           # restricted physical names
        # fresh Dimension load by name -> returns the calc dim.
        _scalar_result([calc_dim]),
        # calc dim detected -> known + tables loaded now.
        _scalar_result(["salary", "region", "dept"]),  # ALL model physical names
        _rows_result([("orders", "o")]),               # table rows (region not a table)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_calc_dimension_whole_row_ref_table_name_collision_fails_closed():
    """R2-2: a whole-row reference ``to_jsonb(orders)`` uses the TABLE identifier
    ``orders``. Even if a column named ``orders`` also exists (name collision),
    the gate must fail closed because ``orders`` matches a table name — the row
    serialisation would leak every column of the ``orders`` table."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="order_dump",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="to_jsonb(orders)",
    )
    bq = _make_bq([], [calc_dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result(["salary"]),                       # restricted physical names
        _scalar_result(["salary", "region", "orders"]),   # `orders` IS also a column (collision)
        _rows_result([("orders", None), ("employees", "emp")]),  # `orders` is a table -> row ref
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "order_dump" in blocked


@pytest.mark.asyncio
async def test_calculated_dimension_restricted_403_via_route_query(monkeypatch):
    """End-to-end at route_query: a persona lacking access to `salary` requesting
    a calc dimension `salary_band` derived from it gets a 403 COLUMN_RESTRICTED —
    the derived dimension does not leak the restricted value."""
    restricted_col_id = uuid4()
    calc_dim = types.SimpleNamespace(
        name="salary_band",
        source_column_id=None,
        user_defined_attribute_id=None,
        measure_type=None,
        variant_of_measure_id=None,
        calc_expression="CASE WHEN salary > 100000 THEN 'high' ELSE 'low' END",
    )
    bq = _make_bq([], [calc_dim])
    persona = types.SimpleNamespace(
        id=uuid4(), name="External Partner", bypass_row_security=False,
    )

    monkeypatch.setattr(
        router_mod, "compile_row_security", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(router_mod, "has_active_rules", lambda c: False)
    monkeypatch.setattr(
        router_mod, "resolve_target_dialect_for_bound",
        AsyncMock(return_value="postgres"),
    )

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result(["salary", "region"]),  # ALL model physical names
        _rows_result([("employees", "emp")]),  # model table rows
        _scalar_result(["PII"]),              # tag names for the 403 payload
    ])

    with pytest.raises(HTTPException) as exc:
        await route_query(
            bq, db,
            principal=types.SimpleNamespace(user_identity="partner@test"),
            persona=persona,
        )

    assert exc.value.status_code == 403
    # F-008-02: non-disclosing 403 — the block fires but the restricted
    # column name must not leak to the client.
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    assert "columns" not in exc.value.detail
    assert "salary_band" not in exc.value.detail.get("message", "")


@pytest.mark.asyncio
async def test_uda_backed_dimension_with_restricted_column_blocked():
    """A user-defined-attribute dimension whose column refs include a
    restricted column is blocked."""
    restricted_col_id = uuid4()
    uda_id = uuid4()
    dim = types.SimpleNamespace(
        name="masked_email", source_column_id=None,
        user_defined_attribute_id=uda_id,
    )
    bq = _make_bq([], [dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result([uda_id]),  # this UDA's refs touch the restricted col
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "masked_email" in blocked


@pytest.mark.asyncio
async def test_star_narrowing_drops_calculated_measure_with_restricted_closure():
    """SELECT * narrowing applies the same closure: the tainted calculated
    measure disappears while clean objects survive."""
    restricted_col_id = uuid4()
    base_id = uuid4()
    base_measure = types.SimpleNamespace(
        id=base_id, name="salary_total", source_column_id=restricted_col_id,
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None,
    )
    calc_measure = types.SimpleNamespace(
        id=uuid4(), name="avg_salary_index", source_column_id=None,
        measure_type="calculated", expression='measure("salary_total")',
        variant_of_measure_id=None, user_defined_attribute_id=None,
    )
    clean_measure = types.SimpleNamespace(
        id=uuid4(), name="order_count", source_column_id=uuid4(),
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None,
    )
    open_dim = types.SimpleNamespace(name="city", source_column_id=uuid4())
    bq = _make_bq(
        [calc_measure, clean_measure], [open_dim],
        select_star=True,
        allowed_physical_columns={"salary", "city", "order_id"},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result([]),               # UDA refs
        _scalar_result([base_measure]),   # model measures
        _scalar_result(["salary"]),       # physical names of restricted ids
    ])

    blocked = await _check_column_restrictions(bq, persona, db)

    assert blocked == []
    assert [m.name for m in bq.resolved_measures] == ["order_count"]
    assert [d.name for d in bq.resolved_dimensions] == ["city"]
    assert bq.persona_narrowed_star is True
    assert "salary" not in bq.allowed_physical_columns


# ---------------------------------------------------------------------------
# M-2 — star query where EVERY resolved object is restricted must 403
# cleanly instead of falling through to the raw-star rewrite fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_with_all_columns_restricted_rejected_up_front():
    restricted_a = uuid4()
    restricted_b = uuid4()
    dim_a = types.SimpleNamespace(name="email", source_column_id=restricted_a)
    dim_b = types.SimpleNamespace(name="phone", source_column_id=restricted_b)
    bq = _make_bq([], [dim_a, dim_b], select_star=True)
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_a, restricted_b]),
    ])

    with pytest.raises(HTTPException) as exc:
        await _check_column_restrictions(bq, persona, db)

    assert exc.value.status_code == 403
    # F-008-02: non-disclosing — a generic "nothing available", not a
    # confirmation that every column of this table is restricted.
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    # The bound query must NOT have been mutated before the rejection.
    assert [d.name for d in bq.resolved_dimensions] == ["email", "phone"]


# ---------------------------------------------------------------------------
# F-008-11 — tag-restricted columns must not be usable as a value-probing
# oracle through WHERE filters (fail closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_on_restricted_column_blocked():
    """A persona restricted from `salary` must not binary-search its values
    via WHERE salary > X on an allowed measure — the filter reference is
    blocked, fail closed."""
    restricted_col_id = uuid4()
    restricted_dim = types.SimpleNamespace(
        name="salary", source_column_id=restricted_col_id,
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    allowed_measure = types.SimpleNamespace(
        id=uuid4(), name="headcount", source_column_id=uuid4(),
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None,
    )
    bq = _make_bq(
        [allowed_measure], [],
        filters=[_filter("salary", "gt", 100000)],
        dimensions_by_name={"salary": restricted_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # physical names of restricted ids
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary" in blocked


@pytest.mark.asyncio
async def test_order_by_only_restricted_column_blocked():
    """Bug-6140: a persona restricted from `salary` must not use it as a
    RANKING ORACLE via ORDER BY salary DESC LIMIT n on an allowed projection —
    the ORDER BY-only reference is blocked, fail closed."""
    restricted_col_id = uuid4()
    restricted_dim = types.SimpleNamespace(
        name="salary", source_column_id=restricted_col_id,
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    allowed_measure = types.SimpleNamespace(
        id=uuid4(), name="headcount", source_column_id=uuid4(),
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None,
    )
    bq = _make_bq(
        [allowed_measure], [],
        order_by=[("salary", "desc")],
        dimensions_by_name={"salary": restricted_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # physical names of restricted ids
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary" in blocked


@pytest.mark.asyncio
async def test_order_by_expression_over_restricted_column_blocked():
    """Bug-6140: an EXPRESSION sort key (has_unresolvable_order) referencing a
    restricted physical column must also be blocked — the tuple list is partial
    for expression sorts, so the raw ORDER BY is re-parsed and checked."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        order_by=[],  # partial/empty for an expression sort
        has_unresolvable_order=True,
        raw_query="SELECT region FROM t ORDER BY salary * 2 DESC LIMIT 10",
        dimensions_by_name={},  # unresolved -> physical-name fallback
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # physical names of restricted ids
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "salary" as Dimension -> none
        _scalar_result([]),                   # "salary" as Measure -> none (physical col)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked  # salary referenced in the expression ORDER BY -> blocked


@pytest.mark.asyncio
async def test_order_by_unparseable_fails_closed():
    """Bug-6140 (R2b): a restricted persona with an expression ORDER BY whose
    raw SQL cannot be strictly parsed must FAIL CLOSED (block) rather than let
    the rewriter emit an unverified sort over a possibly-restricted column."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        order_by=[],
        has_unresolvable_order=True,
        raw_query="SELECT region FROM t ORDER BY )(*&^ %$# not valid sql",
        dimensions_by_name={},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked  # unparseable ORDER BY -> fail closed


@pytest.mark.asyncio
async def test_order_by_top_level_restricted_column_blocked_with_nested_sort():
    """Bug-6140 (R2 hardening): the gate must inspect the TOP-LEVEL ORDER BY the
    rewriter renders, not a nested sort (array_agg/window ORDER BY) that appears
    earlier in the parse tree. Here the nested aggregate sorts by an allowed
    column while the top-level sort ranks by the restricted `salary`; the
    restricted top-level sort must be caught."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        order_by=[],
        has_unresolvable_order=True,
        raw_query=(
            "SELECT region, ARRAY_AGG(dept ORDER BY region) FROM t "
            "GROUP BY region ORDER BY salary DESC"
        ),
        dimensions_by_name={},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "salary" as Dimension -> none
        _scalar_result([]),                   # "salary" as Measure -> none
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked  # top-level ORDER BY salary is restricted -> blocked


@pytest.mark.asyncio
async def test_order_by_only_restricted_measure_blocked():
    """Bug-6140 (Codex R2): a measure that aggregates a restricted column and is
    used ONLY to sort — never projected — is a ranking oracle. Its semantic name
    (`Revenue`) differs from the restricted physical column (`salary_amount`), so
    neither the projection gate nor the physical-name fallback catches it; the
    order-only name must be resolved to the measure and blocked via its closure."""
    restricted_col_id = uuid4()
    restricted_measure = types.SimpleNamespace(
        id=uuid4(), name="Revenue", source_column_id=restricted_col_id,
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None, calc_expression=None,
    )
    allowed_dim = types.SimpleNamespace(
        name="region", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [allowed_dim],
        order_by=[("Revenue", "desc")],   # order-only measure, not projected
        dimensions_by_name={"region": allowed_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary_amount"]),    # restricted physical names (!= "revenue")
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none) — loaded 4th
        _scalar_result([]),                   # order-only name as Dimension -> none
        _scalar_result([restricted_measure]),  # order-only name as Measure -> Revenue
        _scalar_result([restricted_measure]),  # all model measures (closure)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "Revenue" in blocked


@pytest.mark.asyncio
async def test_order_by_only_allowed_measure_not_blocked():
    """An order-only measure over an UNRESTRICTED column must sort cleanly."""
    restricted_col_id = uuid4()
    allowed_measure = types.SimpleNamespace(
        id=uuid4(), name="Revenue", source_column_id=uuid4(),  # not restricted
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [],
        order_by=[("Revenue", "desc")],
        dimensions_by_name={},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary_amount"]),    # restricted physical names
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none) — loaded 4th
        _scalar_result([]),                   # order-only name as Dimension -> none
        _scalar_result([allowed_measure]),     # order-only name as Measure -> Revenue
        _scalar_result([allowed_measure]),     # all model measures (closure)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_order_by_allowed_column_not_blocked():
    """A persona ordering by an UNRESTRICTED column is not blocked."""
    restricted_col_id = uuid4()
    allowed_dim = types.SimpleNamespace(
        name="region", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [allowed_dim],
        order_by=[("region", "asc")],
        dimensions_by_name={"region": allowed_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids (salary)
        _scalar_result(["salary"]),           # physical names of restricted ids
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


# ---------------------------------------------------------------------------
# Bug-6140-adjacent: HAVING is a THRESHOLD ORACLE — the ORDER-BY oracle fix was
# not applied to the adjacent HAVING clause (columns extracted, never gated).
# ---------------------------------------------------------------------------

def _with_having(bq: BoundQuery, having_raw: str, having_columns: list[str]) -> BoundQuery:
    bq.logical_query.having_raw = having_raw
    bq.logical_query.having_columns = list(having_columns)
    return bq


@pytest.mark.asyncio
async def test_having_only_restricted_column_blocked():
    """Bug-6140-adjacent: a persona restricted from `salary` must not use it as a
    THRESHOLD ORACLE via ``... GROUP BY dept HAVING SUM(salary) > N`` on an
    allowed projection. The HAVING-only reference resolves to the restricted
    physical column and is blocked, fail closed."""
    restricted_col_id = uuid4()
    allowed_dim = types.SimpleNamespace(
        name="dept", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _with_having(
        _make_bq(
            [], [allowed_dim],
            raw_query="SELECT dept FROM t GROUP BY dept HAVING SUM(salary) > 1000000",
            dimensions_by_name={"dept": allowed_dim},
        ),
        "HAVING SUM(salary) > 1000000", ["salary"],
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # physical names of restricted ids
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "salary" as Dimension -> none
        _scalar_result([]),                   # "salary" as Measure -> none (physical col)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary" in blocked


@pytest.mark.asyncio
async def test_having_restricted_measure_blocked():
    """A measure aggregating a restricted column, referenced ONLY in HAVING — its
    semantic name (`Revenue`) differs from the restricted physical column
    (`salary_amount`), so it must be resolved to the measure and blocked via its
    column closure, exactly like the ORDER-BY measure oracle."""
    restricted_col_id = uuid4()
    restricted_measure = types.SimpleNamespace(
        id=uuid4(), name="Revenue", source_column_id=restricted_col_id,
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None, calc_expression=None,
    )
    allowed_dim = types.SimpleNamespace(
        name="region", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _with_having(
        _make_bq(
            [], [allowed_dim],
            raw_query="SELECT region FROM t GROUP BY region HAVING Revenue > 5",
            dimensions_by_name={"region": allowed_dim},
        ),
        "HAVING Revenue > 5", ["Revenue"],
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary_amount"]),    # restricted physical names (!= "revenue")
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none) — loaded 4th
        _scalar_result([]),                   # having-only name as Dimension -> none
        _scalar_result([restricted_measure]),  # having-only name as Measure -> Revenue
        _scalar_result([restricted_measure]),  # all model measures (closure)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "Revenue" in blocked


@pytest.mark.asyncio
async def test_having_unparseable_fails_closed():
    """A restricted persona whose HAVING text cannot be strictly parsed must FAIL
    CLOSED (block) rather than let the rewriter emit an unverified HAVING over a
    possibly-restricted column."""
    restricted_col_id = uuid4()
    bq = _with_having(
        _make_bq(
            [], [],
            raw_query="SELECT region FROM t GROUP BY region HAVING )(*&^ bad",
            dimensions_by_name={},
        ),
        "HAVING )(*&^ %$# not valid", [],
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked  # unparseable HAVING -> fail closed


@pytest.mark.asyncio
async def test_having_allowed_column_not_blocked():
    """A HAVING over an UNRESTRICTED aggregate must not be blocked (no false
    positive)."""
    restricted_col_id = uuid4()
    allowed_dim = types.SimpleNamespace(
        name="dept", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _with_having(
        _make_bq(
            [], [allowed_dim],
            raw_query="SELECT dept FROM t GROUP BY dept HAVING COUNT(orders) > 3",
            dimensions_by_name={"dept": allowed_dim},
        ),
        "HAVING COUNT(orders) > 3", ["orders"],
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids (salary)
        _scalar_result(["salary"]),           # physical names of restricted ids
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "orders" as Dimension -> none
        _scalar_result([]),                   # "orders" as Measure -> none
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_filter_on_restricted_column_blocked_case_variant():
    """ML3 lesson: a case-variant raw column name in WHERE must not slip
    past the physical-name match."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        filters=[_filter("SALARY", "gt", 100000)],  # upper-cased
        dimensions_by_name={},  # unresolved -> physical-name fallback
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result(["salary"]),  # stored lower-case; filter is upper-case
        _scalar_result([]),          # Bug-7812: UDA restrictions (none)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "SALARY" in blocked


@pytest.mark.asyncio
async def test_filter_on_unrestricted_column_allowed():
    """A WHERE on a non-restricted column for the authorised query is not
    blocked."""
    restricted_col_id = uuid4()
    open_dim = types.SimpleNamespace(
        name="region", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [],
        filters=[_filter("region", "eq", "EMEA")],
        dimensions_by_name={"region": open_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([restricted_col_id]),
        _scalar_result(["salary"]),  # restricted col is not `region`
        _scalar_result([]),          # Bug-7812: UDA restrictions (none)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_star_query_with_restricted_filter_still_403():
    """A SELECT * narrows the projection, but a WHERE on a restricted column
    is still a probing oracle and must 403 — narrowing does not excuse it."""
    restricted_col_id = uuid4()
    open_dim = types.SimpleNamespace(name="city", source_column_id=uuid4())
    restricted_dim = types.SimpleNamespace(
        name="salary", source_column_id=restricted_col_id,
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [open_dim],
        select_star=True,
        allowed_physical_columns={"city", "salary"},
        filters=[_filter("salary", "gt", 1)],
        dimensions_by_name={"salary": restricted_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # physical names (star narrowing)
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary" in blocked


# ---------------------------------------------------------------------------
# F-008-02 — CLS 403 payload is NON-disclosing: a restricted-column denial must
# be indistinguishable from an unknown-object denial (no existence oracle). The
# specific column/tag/persona detail goes only to the server log.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cls_403_payload_is_non_disclosing(monkeypatch):
    """The 403 raised by route_query for a restricted column must NOT leak the
    column name, the restricting tag names, or the persona identity (F-008-02).
    An analyst must not be able to prove that ``email`` exists / is classified
    PII from the error alone."""
    restricted_col_id = uuid4()
    dim = types.SimpleNamespace(name="email", source_column_id=restricted_col_id)
    bq = _make_bq([], [dim])
    persona = types.SimpleNamespace(
        id=uuid4(), name="External Partner", bypass_row_security=False,
    )

    monkeypatch.setattr(
        router_mod, "compile_row_security", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(router_mod, "has_active_rules", lambda c: False)
    monkeypatch.setattr(
        router_mod, "resolve_target_dialect_for_bound",
        AsyncMock(return_value="postgres"),
    )

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags (check)
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["PII"]),              # tag names (logged, not returned)
    ])

    with pytest.raises(HTTPException) as exc:
        await route_query(
            bq, db,
            principal=types.SimpleNamespace(user_identity="partner@test"),
            persona=persona,
        )

    detail = exc.value.detail
    assert exc.value.status_code == 403
    assert detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    # None of the sensitive identifiers may appear in the client payload.
    assert "columns" not in detail
    assert "tags" not in detail
    assert "persona_name" not in detail
    assert "persona_id" not in detail
    msg = detail.get("message", "")
    assert "email" not in msg
    assert "PII" not in msg
    assert "External Partner" not in msg


# ---------------------------------------------------------------------------
# Bug-7804 — the runtime CLS closure must walk Dimension.display_column_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dimension_blocked_via_display_column_id():
    """Bug-7804: a flat dimension can carry a SEPARATE display column
    (``display_column_id``, Bug-5434) surfaced as the member CAPTION while the
    KEY column (``source_column_id``) is clean. A persona restricted from the
    DISPLAY column must be blocked — otherwise member discovery / serving reads
    and leaks the restricted display VALUE. Asserts the KNOWN permission
    decision: the dimension is in the blocked list."""
    restricted_display_col_id = uuid4()
    dim = types.SimpleNamespace(
        name="employee_name",
        # KEY column is clean/unrestricted...
        source_column_id=uuid4(),
        # ...but the DISPLAY column is the restricted one.
        display_column_id=restricted_display_col_id,
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq([], [dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),                    # restricted tags
        _scalar_result([restricted_display_col_id]),  # restricted column ids
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "employee_name" in blocked


@pytest.mark.asyncio
async def test_dimension_clean_display_column_passes():
    """A dimension whose display column is NOT restricted keeps working — no
    false positive from the Bug-7804 display_column_id closure."""
    dim = types.SimpleNamespace(
        name="employee_name",
        source_column_id=uuid4(),
        display_column_id=uuid4(),   # unrelated, unrestricted display column
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq([], [dim])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),   # restricted tags
        _scalar_result([uuid4()]),   # restricted column ids: unrelated column
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


# ---------------------------------------------------------------------------
# Bug-7047 — HAVING on a CLS-restricted column reached via a SELECT alias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_having_restricted_column_via_select_alias_blocked():
    """Bug-7047: ``SELECT dept, AVG(salary) AS avg_sal ... HAVING avg_sal > 1000``
    references the restricted column ``salary`` only through the SELECT ALIAS
    ``avg_sal``. The rewriter expands the alias to its underlying aggregate
    ``AVG(salary)`` before executing, so the security gate MUST expand the alias
    too and block on the restricted underlying column — else the HAVING is a
    threshold oracle. Asserts the KNOWN decision: the query is blocked."""
    restricted_col_id = uuid4()
    allowed_dim = types.SimpleNamespace(
        name="dept", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _with_having(
        _make_bq(
            [], [allowed_dim],
            raw_query=(
                "SELECT dept, AVG(salary) AS avg_sal FROM t "
                "GROUP BY dept HAVING avg_sal > 1000"
            ),
            dimensions_by_name={"dept": allowed_dim},
        ),
        # The HAVING references ONLY the alias — a raw-column scan sees
        # "avg_sal", never "salary".
        "HAVING avg_sal > 1000", ["avg_sal"],
    )
    bq.logical_query.select_expressions = [
        SelectExpression(
            raw_text="AVG(salary) AS avg_sal", alias="avg_sal",
            classification="analytical", agg_function="avg",
            inner_column="salary", inner_literal=None,
        ),
    ]
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # physical names of restricted ids
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # unresolved names as Dimension -> none
        _scalar_result([]),                   # unresolved names as Measure -> none
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    # The expanded underlying column "salary" hits the restricted physical name.
    assert "salary" in blocked


@pytest.mark.asyncio
async def test_having_clean_select_alias_not_blocked():
    """A HAVING over an alias whose underlying expression touches NO restricted
    column must not be blocked (no false positive from alias expansion)."""
    restricted_col_id = uuid4()
    allowed_dim = types.SimpleNamespace(
        name="dept", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _with_having(
        _make_bq(
            [], [allowed_dim],
            raw_query=(
                "SELECT dept, COUNT(orders) AS n FROM t "
                "GROUP BY dept HAVING n > 3"
            ),
            dimensions_by_name={"dept": allowed_dim},
        ),
        "HAVING n > 3", ["n"],
    )
    bq.logical_query.select_expressions = [
        SelectExpression(
            raw_text="COUNT(orders) AS n", alias="n",
            classification="analytical", agg_function="count",
            inner_column="orders", inner_literal=None,
        ),
    ]
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids (salary)
        _scalar_result(["salary"]),           # physical names of restricted ids
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "n"/"orders" as Dimension -> none
        _scalar_result([]),                   # "n"/"orders" as Measure -> none
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


# ---------------------------------------------------------------------------
# Bug-7045 — deep (3+ level) transitive calculated-measure CLS closure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transitive_three_level_calculated_measure_chain_blocked():
    """Bug-7045: a 3-level calculated-measure chain A -> B -> C where C reads a
    restricted column must be blocked at query time through the recursive
    closure (the model-service catalogue hides the same chain; this guards the
    query-router serving half against drift). Asserts the KNOWN decision: the
    top measure A is blocked."""
    restricted_col_id = uuid4()
    # Level 3: the base standard measure over the RESTRICTED column.
    c_id = uuid4()
    measure_c = types.SimpleNamespace(
        id=c_id, name="salary_base", source_column_id=restricted_col_id,
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None, calc_expression=None,
    )
    # Level 2: calculated measure referencing C.
    b_id = uuid4()
    measure_b = types.SimpleNamespace(
        id=b_id, name="salary_scaled", source_column_id=None,
        measure_type="calculated", expression='measure("salary_base") * 1.1',
        variant_of_measure_id=None, user_defined_attribute_id=None,
        calc_expression=None,
    )
    # Level 1: calculated measure referencing B (the projected measure).
    measure_a = types.SimpleNamespace(
        id=uuid4(), name="salary_index", source_column_id=None,
        measure_type="calculated", expression='measure("salary_scaled") / 100',
        variant_of_measure_id=None, user_defined_attribute_id=None,
        calc_expression=None,
    )
    bq = _make_bq([measure_a], [])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result([]),                   # UDA refs touching restricted
        _scalar_result([measure_a, measure_b, measure_c]),  # model measures (closure)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary_index" in blocked


@pytest.mark.asyncio
async def test_transitive_three_level_clean_chain_passes():
    """A deep calculated-measure chain whose leaf reads NO restricted column
    keeps working — the recursive closure does not over-block."""
    c_id = uuid4()
    measure_c = types.SimpleNamespace(
        id=c_id, name="orders_base", source_column_id=uuid4(),
        measure_type="standard", variant_of_measure_id=None,
        user_defined_attribute_id=None, expression=None, calc_expression=None,
    )
    b_id = uuid4()
    measure_b = types.SimpleNamespace(
        id=b_id, name="orders_scaled", source_column_id=None,
        measure_type="calculated", expression='measure("orders_base") * 2',
        variant_of_measure_id=None, user_defined_attribute_id=None,
        calc_expression=None,
    )
    measure_a = types.SimpleNamespace(
        id=uuid4(), name="orders_index", source_column_id=None,
        measure_type="calculated", expression='measure("orders_scaled") + 1',
        variant_of_measure_id=None, user_defined_attribute_id=None,
        calc_expression=None,
    )
    bq = _make_bq([measure_a], [])
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),
        _scalar_result([uuid4()]),   # restricted ids: unrelated column
        _scalar_result([]),          # UDA refs
        _scalar_result([measure_a, measure_b, measure_c]),
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


# ---------------------------------------------------------------------------
# CLS derived-expression leaf gap (intake
# 2026-07-14-cls-derived-expression-leaf-not-checked-source-path): a function
# grain such as ``GROUP BY UPPER(restricted_col)`` must be blocked on the
# SOURCE route. The restricted leaf is referenced ONLY inside the bound derived
# expression, so the projection/filter/order/having gates never see it.
# ---------------------------------------------------------------------------


def _bde(*leaves):
    """A minimal BoundDerivedExpression stand-in exposing ``inputs`` leaves."""
    return types.SimpleNamespace(inputs=list(leaves))


def _bleaf(*, column_id="", physical_column=""):
    return types.SimpleNamespace(column_id=column_id, physical_column=physical_column)


@pytest.mark.asyncio
async def test_derived_expression_leaf_restricted_by_id_blocked():
    """``GROUP BY UPPER(country)`` where ``country`` is restricted: the binder
    bound the leaf to the restricted ModelColumn id, so the id-first check blocks
    on the SOURCE route (UPPER = disclosure modulo case). Asserts the KNOWN
    decision: the query is blocked even though no bare grain dimension names
    ``country``."""
    restricted_col_id = uuid4()
    open_dim = types.SimpleNamespace(
        name="region", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [open_dim],
        raw_query="SELECT UPPER(country) FROM t GROUP BY UPPER(country)",
        bound_derived_expressions=[
            _bde(_bleaf(column_id=str(restricted_col_id), physical_column="country")),
        ],
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["country"]),          # restricted physical names (fallback set)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked  # restricted leaf inside the derived expression -> blocked


@pytest.mark.asyncio
async def test_derived_expression_leaf_restricted_by_name_when_id_unbound():
    """When the binder withheld the leaf id (``column_id == ""``, §7.1
    all-or-nothing), the restricted-physical-name fallback must still block a
    function grain over the restricted column — fail closed."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        raw_query="SELECT DATE_TRUNC('month', hire_date) FROM t "
                  "GROUP BY DATE_TRUNC('month', hire_date)",
        bound_derived_expressions=[
            _bde(_bleaf(column_id="", physical_column="hire_date")),
        ],
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["hire_date"]),        # restricted physical names (fallback)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked  # unbound leaf still blocked via restricted physical name


@pytest.mark.asyncio
async def test_derived_expression_leaf_clean_not_blocked():
    """A function grain over a NON-restricted column (``GROUP BY UPPER(region)``)
    must still serve — no false positive from the derived-leaf gate."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        raw_query="SELECT UPPER(region) FROM t GROUP BY UPPER(region)",
        bound_derived_expressions=[
            _bde(_bleaf(column_id=str(uuid4()), physical_column="region")),
        ],
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names (region is clean)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_derived_expression_leaf_restricted_403_via_route_query(monkeypatch):
    """End-to-end at route_query: a persona restricted from ``country`` running
    ``GROUP BY UPPER(country)`` gets a 403 COLUMN_RESTRICTED — the source route
    does not execute and disclose the restricted values."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        raw_query="SELECT UPPER(country) FROM t GROUP BY UPPER(country)",
        bound_derived_expressions=[
            _bde(_bleaf(column_id=str(restricted_col_id), physical_column="country")),
        ],
    )
    persona = types.SimpleNamespace(
        id=uuid4(), name="External Partner", bypass_row_security=False,
    )

    monkeypatch.setattr(
        router_mod, "compile_row_security", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(router_mod, "has_active_rules", lambda c: False)
    monkeypatch.setattr(
        router_mod, "resolve_target_dialect_for_bound",
        AsyncMock(return_value="postgres"),
    )

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["country"]),          # restricted physical names
        _scalar_result(["PII"]),              # tag names for the 403 payload
    ])

    with pytest.raises(HTTPException) as exc:
        await route_query(
            bq, db,
            principal=types.SimpleNamespace(user_identity="partner@test"),
            persona=persona,
        )

    assert exc.value.status_code == 403
    # F-008-02: non-disclosing 403.
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# Bug-7811 — a FUNCTION-WRAPPED WHERE predicate over a restricted column is a
# value-probing oracle. It is NOT representable as a LogicalFilter, so it sets
# has_unresolvable_where (not has_complex_sql) and is absent from
# resolved_filters. The filter gate must re-parse the raw WHERE — as the ORDER
# BY / HAVING gates do — and fail closed on the restricted column.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_where_function_wrapped_restricted_column_blocked():
    """``WHERE UPPER(salary) = 'X'`` on an allowed projection is a value-probing
    oracle over the restricted ``salary``. The wrapped predicate never becomes a
    LogicalFilter (has_unresolvable_where=True, resolved_filters empty), so the
    gate must re-parse the raw WHERE and block. Asserts the KNOWN decision:
    ``salary`` is blocked."""
    restricted_col_id = uuid4()
    allowed_dim = types.SimpleNamespace(
        name="department", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [allowed_dim],
        has_unresolvable_where=True,
        raw_query=(
            "SELECT department, COUNT(*) FROM t "
            "WHERE UPPER(salary) = 'X' GROUP BY department"
        ),
        dimensions_by_name={"department": allowed_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "salary" as Dimension -> none
        _scalar_result([]),                   # "salary" as Measure -> none (physical col)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary" in blocked


@pytest.mark.asyncio
async def test_unresolvable_where_arithmetic_over_restricted_column_blocked():
    """``WHERE salary * 2 > N`` — an arithmetic-wrapped restricted column — is
    the bisection oracle in another disguise and must be blocked."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        has_unresolvable_where=True,
        raw_query="SELECT region FROM t WHERE salary * 2 > 200000",
        dimensions_by_name={},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "salary" as Dimension -> none
        _scalar_result([]),                   # "salary" as Measure -> none
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked  # salary inside the arithmetic WHERE -> blocked


@pytest.mark.asyncio
async def test_unresolvable_where_clean_column_not_blocked():
    """An unresolvable WHERE that wraps only a NON-restricted column
    (``WHERE UPPER(region) = 'EMEA'``) must still serve — no false positive."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        has_unresolvable_where=True,
        raw_query="SELECT region FROM t WHERE UPPER(region) = 'EMEA'",
        dimensions_by_name={},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names (region is clean)
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "region" as Dimension -> none
        _scalar_result([]),                   # "region" as Measure -> none
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []


@pytest.mark.asyncio
async def test_unresolvable_where_unparseable_fails_closed():
    """A restricted persona whose unresolvable WHERE cannot be strictly parsed
    must FAIL CLOSED rather than let the rewriter emit an unverified predicate
    over a possibly-restricted column."""
    restricted_col_id = uuid4()
    bq = _make_bq(
        [], [],
        has_unresolvable_where=True,
        raw_query="SELECT region FROM t WHERE )(*&^ %$# not valid",
        dimensions_by_name={},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked  # unparseable WHERE -> fail closed


@pytest.mark.asyncio
async def test_unresolvable_where_function_wrapped_restricted_403_via_route_query(monkeypatch):
    """End-to-end at route_query: a persona restricted from ``salary`` running
    ``WHERE UPPER(salary) = 'X'`` gets a 403 COLUMN_RESTRICTED — the source route
    does not execute the probing predicate."""
    restricted_col_id = uuid4()
    allowed_dim = types.SimpleNamespace(
        name="department", source_column_id=uuid4(),
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [allowed_dim],
        has_unresolvable_where=True,
        raw_query=(
            "SELECT department, COUNT(*) FROM t "
            "WHERE UPPER(salary) = 'X' GROUP BY department"
        ),
        dimensions_by_name={"department": allowed_dim},
    )
    persona = types.SimpleNamespace(
        id=uuid4(), name="External Partner", bypass_row_security=False,
    )

    monkeypatch.setattr(
        router_mod, "compile_row_security", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(router_mod, "has_active_rules", lambda c: False)
    monkeypatch.setattr(
        router_mod, "resolve_target_dialect_for_bound",
        AsyncMock(return_value="postgres"),
    )

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result([]),                   # Bug-7812: UDA restrictions (none)
        _scalar_result([]),                   # "salary" as Dimension -> none
        _scalar_result([]),                   # "salary" as Measure -> none
        _scalar_result(["PII"]),              # tag names for the 403 payload
    ])

    with pytest.raises(HTTPException) as exc:
        await route_query(
            bq, db,
            principal=types.SimpleNamespace(user_identity="partner@test"),
            persona=persona,
        )

    assert exc.value.status_code == 403
    # F-008-02: non-disclosing 403 — the block fires but the restricted
    # column name must not leak to the client.
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    assert "columns" not in exc.value.detail
    assert "salary" not in exc.value.detail.get("message", "")


# ---------------------------------------------------------------------------
# Bug-7812 — a UDA-backed dimension used ONLY in a resolvable filter (never
# projected) must be blocked. Its UDA restriction set was loaded only for
# PROJECTED objects, so the filter-only UDA dimension was checked against an
# empty set and served — a value-probing oracle over the UDA's restricted
# backing column, and a divergence from model-service (which always loads it).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uda_dimension_in_resolvable_filter_only_blocked():
    """A UDA-backed dimension ``risk_band`` (source_column_id NULL, its UDA
    references restricted ``salary``) used ONLY in ``WHERE risk_band = 'HIGH'``
    (never projected) is a value-probing oracle. The filter gate must load the
    UDA restriction set even though no PROJECTED object is UDA-backed. Asserts
    the KNOWN decision: ``risk_band`` is blocked."""
    restricted_col_id = uuid4()
    uda_id = uuid4()
    # risk_band is UDA-backed and appears ONLY in the filter (not projected).
    risk_band = types.SimpleNamespace(
        name="risk_band", source_column_id=None, display_column_id=None,
        user_defined_attribute_id=uda_id, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    allowed_dim = types.SimpleNamespace(
        name="dept", source_column_id=uuid4(), display_column_id=None,
        user_defined_attribute_id=None, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [allowed_dim],
        filters=[_filter("risk_band", "eq", "HIGH")],
        dimensions_by_name={"risk_band": risk_band, "dept": allowed_dim},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result([uda_id]),             # Bug-7812: UDA refs touching restricted -> uda_id
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "risk_band" in blocked


@pytest.mark.asyncio
async def test_uda_dimension_in_filter_only_clean_not_blocked():
    """A UDA-backed filter-only dimension whose UDA does NOT touch a restricted
    column must still serve — no false positive from the Bug-7812 load."""
    restricted_col_id = uuid4()
    uda_id = uuid4()
    risk_band = types.SimpleNamespace(
        name="risk_band", source_column_id=None, display_column_id=None,
        user_defined_attribute_id=uda_id, measure_type=None,
        variant_of_measure_id=None, calc_expression=None,
    )
    bq = _make_bq(
        [], [],
        filters=[_filter("risk_band", "eq", "HIGH")],
        dimensions_by_name={"risk_band": risk_band},
    )
    persona = types.SimpleNamespace(id=uuid4())

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result([uuid4()]),            # restricted tags
        _scalar_result([restricted_col_id]),  # restricted column ids
        _scalar_result(["salary"]),           # restricted physical names
        _scalar_result([]),                   # UDA refs touching restricted -> none (clean)
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert blocked == []
