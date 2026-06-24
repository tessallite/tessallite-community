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
from src.ir.logical_query import BoundQuery, LogicalFilter, LogicalQuery, RouteDecision


def _make_bq(
    measures: list,
    dimensions: list,
    *,
    has_complex_sql: bool = False,
    select_star: bool = False,
    allowed_physical_columns: set[str] | None = None,
    filters: list | None = None,
    dimensions_by_name: dict | None = None,
) -> BoundQuery:
    filters = filters or []
    lq = LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT ...",
        requested_measures=[m.name for m in measures],
        requested_dimensions=[d.name for d in dimensions],
        filters=list(filters),
        grain=[d.name for d in dimensions],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="abc",
        has_complex_sql=has_complex_sql,
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
    )


def _filter(dimension_name: str, operator: str = "gt", value=0):
    return LogicalFilter(dimension_name=dimension_name, operator=operator, value=value)


def _scalar_result(items: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
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
    assert exc.value.detail["error_code"] == "COLUMN_RESTRICTED"
    assert "email" in exc.value.detail["columns"]
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
    assert exc.value.detail["error_code"] == "COLUMN_RESTRICTED"
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
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary" in blocked


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
    ])

    blocked = await _check_column_restrictions(bq, persona, db)
    assert "salary" in blocked


# ---------------------------------------------------------------------------
# F-008-19 — CLS 403 payload carries the consistent error code plus tag and
# persona names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cls_403_payload_is_informative(monkeypatch):
    """The 403 raised by route_query carries COLUMN_RESTRICTED, the column
    names, the restricting tag names, and the persona name."""
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
        _scalar_result(["PII"]),              # tag names for the payload
    ])

    with pytest.raises(HTTPException) as exc:
        await route_query(
            bq, db,
            principal=types.SimpleNamespace(user_identity="partner@test"),
            persona=persona,
        )

    detail = exc.value.detail
    assert detail["error_code"] == "COLUMN_RESTRICTED"
    assert "email" in detail["columns"]
    assert detail["persona_name"] == "External Partner"
    assert "PII" in detail["tags"]
    assert "External Partner" in detail["message"]
    assert "PII" in detail["message"]
