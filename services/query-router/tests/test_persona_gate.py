"""Unit tests for the router persona allow-list gate — Phase 8.B.3."""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

from src.ir.logical_query import LogicalFilter
from src.security.persona_gate import (
    apply_persona_gate,
    enforce_persona,
    enforce_persona_gate,
    merge_default_filters,
)

from conftest import make_bound_query, make_dimension, make_measure


def _persona(
    *,
    measure_ids=None,
    dimension_ids=None,
    hierarchy_ids=None,
    default_filters=None,
    name: str = "Sales",
    model_id: str = "model-1",
    pid: uuid.UUID | None = None,
):
    return types.SimpleNamespace(
        id=pid or uuid.uuid4(),
        model_id=model_id,
        name=name,
        included_measure_ids=measure_ids or [],
        included_dimension_ids=dimension_ids or [],
        included_hierarchy_ids=hierarchy_ids or [],
        default_filters=default_filters or {},
    )


def test_empty_lists_allow_anything():
    measure = make_measure("revenue")
    dim = make_dimension("region")
    bound = make_bound_query(dimensions=[dim], measures=[measure])

    enforce_persona(_persona(), bound)


def test_measure_in_list_passes():
    measure = make_measure("revenue")
    dim = make_dimension("region")
    bound = make_bound_query(dimensions=[dim], measures=[measure])

    enforce_persona(
        _persona(measure_ids=[str(measure.id)]),
        bound,
    )


def test_measure_not_in_list_raises_403():
    # F-008-02: a persona-allow-list denial must NOT disclose the object name,
    # kind, or persona identity to the client — a restricted 403 is
    # indistinguishable from an unknown-object 403 (no existence oracle).
    measure = make_measure("revenue")
    other = uuid.uuid4()
    bound = make_bound_query(dimensions=[], measures=[measure])

    with pytest.raises(HTTPException) as exc:
        enforce_persona(_persona(measure_ids=[str(other)]), bound)

    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    # The measure name / kind / persona must NOT leak in the client payload.
    assert "object_kind" not in detail
    assert "object_name" not in detail
    assert "persona_id" not in detail
    assert "revenue" not in detail.get("message", "")


def test_dimension_not_in_list_raises_403():
    # F-008-02: non-disclosing denial (see test_measure_not_in_list_raises_403).
    dim = make_dimension("region")
    other = uuid.uuid4()
    bound = make_bound_query(dimensions=[dim], measures=[])

    with pytest.raises(HTTPException) as exc:
        enforce_persona(_persona(dimension_ids=[str(other)]), bound)

    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    assert "object_kind" not in detail
    assert "region" not in detail.get("message", "")


def test_hierarchy_filter_uses_dimension_hierarchy_id():
    h_id = uuid.uuid4()
    other_h = uuid.uuid4()
    dim = make_dimension("region")
    dim.hierarchy_id = other_h
    bound = make_bound_query(dimensions=[dim], measures=[])

    with pytest.raises(HTTPException) as exc:
        enforce_persona(_persona(hierarchy_ids=[str(h_id)]), bound)

    # F-008-02: denied, but non-disclosing (no object_kind / name in payload).
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    assert "object_kind" not in exc.value.detail


def test_hierarchy_filter_skips_dimensions_without_hierarchy():
    h_id = uuid.uuid4()
    dim = make_dimension("flat_dim")
    # no hierarchy_id attr set → should not raise
    bound = make_bound_query(dimensions=[dim], measures=[])

    enforce_persona(_persona(hierarchy_ids=[str(h_id)]), bound)


# ---------------------------------------------------------------------------
# SELECT * — persona filters instead of rejecting
# ---------------------------------------------------------------------------


def test_select_star_filters_disallowed_measures():
    m_ok = make_measure("revenue")
    m_deny = make_measure("cost")
    bound = make_bound_query(
        dimensions=[], measures=[m_ok, m_deny], select_star=True,
    )

    enforce_persona(_persona(measure_ids=[str(m_ok.id)]), bound)

    assert len(bound.resolved_measures) == 1
    assert bound.resolved_measures[0].name == "revenue"
    assert bound.persona_narrowed_star is True


def test_select_star_filters_disallowed_dimensions():
    d_ok = make_dimension("region")
    d_deny = make_dimension("secret_field")
    bound = make_bound_query(
        dimensions=[d_ok, d_deny], measures=[], select_star=True,
    )

    enforce_persona(_persona(dimension_ids=[str(d_ok.id)]), bound)

    assert len(bound.resolved_dimensions) == 1
    assert bound.resolved_dimensions[0].name == "region"
    assert bound.persona_narrowed_star is True


def test_select_star_no_narrowing_when_all_allowed():
    m = make_measure("revenue")
    bound = make_bound_query(dimensions=[], measures=[m], select_star=True)

    enforce_persona(_persona(measure_ids=[str(m.id)]), bound)

    assert len(bound.resolved_measures) == 1
    assert bound.persona_narrowed_star is False


def test_explicit_query_still_rejects_disallowed_measure():
    """Non-star queries must still raise 403 on disallowed objects."""
    m = make_measure("revenue")
    other = uuid.uuid4()
    bound = make_bound_query(dimensions=[], measures=[m], select_star=False)

    with pytest.raises(HTTPException) as exc:
        enforce_persona(_persona(measure_ids=[str(other)]), bound)

    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# apply_persona_gate (DB-loading wrapper)
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self, persona, *, execute_error: BaseException | None = None):
        self._persona = persona
        self._execute_error = execute_error

    async def get(self, cls, key):  # noqa: ARG002 — mimic AsyncSession.get
        if self._persona is None:
            return None
        if str(key) != str(self._persona.id):
            return None
        return self._persona

    async def execute(self, stmt):  # noqa: ARG002
        if self._execute_error is not None:
            raise self._execute_error

        class _R:
            def all(self):
                return []

        return _R()


@pytest.mark.asyncio
async def test_apply_returns_none_when_header_absent():
    bound = make_bound_query(dimensions=[], measures=[])
    db = _FakeDB(persona=None)

    result = await apply_persona_gate(
        db, model_id="model-1", persona_id=None, bound=bound
    )
    assert result is None


@pytest.mark.asyncio
async def test_apply_400_when_invalid_uuid():
    bound = make_bound_query(dimensions=[], measures=[])
    db = _FakeDB(persona=None)

    with pytest.raises(HTTPException) as exc:
        await apply_persona_gate(
            db, model_id="model-1", persona_id="not-a-uuid", bound=bound
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "PERSONA_INVALID"


@pytest.mark.asyncio
async def test_apply_404_when_persona_missing():
    bound = make_bound_query(dimensions=[], measures=[])
    db = _FakeDB(persona=None)

    with pytest.raises(HTTPException) as exc:
        await apply_persona_gate(
            db,
            model_id="model-1",
            persona_id=str(uuid.uuid4()),
            bound=bound,
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["error_code"] == "PERSONA_NOT_FOUND"


@pytest.mark.asyncio
async def test_apply_404_when_persona_belongs_to_other_model():
    bound = make_bound_query(dimensions=[], measures=[])
    p = _persona(model_id="model-OTHER")
    db = _FakeDB(persona=p)

    with pytest.raises(HTTPException) as exc:
        await apply_persona_gate(
            db, model_id="model-1", persona_id=str(p.id), bound=bound
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_apply_returns_persona_on_pass():
    measure = make_measure("revenue")
    bound = make_bound_query(dimensions=[], measures=[measure])
    p = _persona(measure_ids=[str(measure.id)])
    db = _FakeDB(persona=p)

    result = await apply_persona_gate(
        db, model_id="model-1", persona_id=str(p.id), bound=bound
    )
    assert result is p


@pytest.mark.asyncio
async def test_f008_01_backing_lookup_failure_refuses_query():
    """Bug-9260: when the excluded-measure backing lookup raises, the
    query is refused rather than served with the F-008-01 guard disabled.
    """
    measure = make_measure("revenue")
    dim = make_dimension("fee_amount")
    bound = make_bound_query(dimensions=[dim], measures=[], select_star=False)
    p = _persona(measure_ids=[str(measure.id)])
    db = _FakeDB(persona=p, execute_error=RuntimeError("simulated timeout"))

    with pytest.raises(HTTPException) as exc:
        await enforce_persona_gate(
            db, persona=p, model_id="model-1", bound=bound,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# merge_default_filters (Phase 8.B.4)
# ---------------------------------------------------------------------------


def test_merge_appends_eq_filter_for_scalar():
    bound = make_bound_query(dimensions=[], measures=[])
    p = _persona(default_filters={"year": 2026})

    merged = merge_default_filters(p, bound)

    assert merged == ["year"]
    assert len(bound.resolved_filters) == 1
    f = bound.resolved_filters[0]
    assert f.dimension_name == "year"
    assert f.operator == "eq"
    assert f.value == 2026


def test_merge_uses_in_for_list_value():
    bound = make_bound_query(dimensions=[], measures=[])
    p = _persona(default_filters={"region": ["EMEA", "APAC"]})

    merge_default_filters(p, bound)

    assert bound.resolved_filters[0].operator == "in"
    assert bound.resolved_filters[0].value == ["EMEA", "APAC"]


def test_merge_default_filter_is_mandatory_not_overridable():
    """F-008-01: a persona default filter is a MANDATORY security predicate,
    never overridable by a user filter on the same dimension. Both the user's
    filter and the persona default are present and AND-composed at render time,
    so a user assigned to EMEA who requests APAC gets EMEA AND APAC (their scope
    is inescapable). The prior behaviour ('user filter wins, skip the persona
    default') let an EMEA user read out-of-persona rows."""
    bound = make_bound_query(
        dimensions=[],
        measures=[],
        filters=[LogicalFilter("year", "eq", 2025)],
    )
    p = _persona(default_filters={"year": 2026, "region": "EMEA"})

    merged = merge_default_filters(p, bound)

    # Both persona defaults are appended (mandatory), even the colliding one.
    assert set(merged) == {"year", "region"}
    # The user's year filter is retained AND the persona's year default is added
    # — they AND together, so the user cannot escape the persona's year scope.
    year_filters = [f for f in bound.resolved_filters if f.dimension_name == "year"]
    assert len(year_filters) == 2
    assert {f.value for f in year_filters} == {2025, 2026}


def test_merge_supports_dict_operator_form():
    bound = make_bound_query(dimensions=[], measures=[])
    p = _persona(default_filters={"amount": {"gte": 100}})

    merge_default_filters(p, bound)

    assert bound.resolved_filters[0].operator == "gte"
    assert bound.resolved_filters[0].value == 100


def test_merge_ignores_unknown_operator():
    bound = make_bound_query(dimensions=[], measures=[])
    p = _persona(default_filters={"x": {"bogus_op": 1}})

    merged = merge_default_filters(p, bound)

    assert merged == []
    assert bound.resolved_filters == []


def test_merge_noop_when_default_filters_empty():
    bound = make_bound_query(dimensions=[], measures=[])
    p = _persona(default_filters={})

    assert merge_default_filters(p, bound) == []
    assert bound.resolved_filters == []


def test_merge_skips_at_parameter_keys():
    """Bug-7662: @-prefixed keys in default_filters are parameter overrides
    consumed by _bind_query_parameters, NOT dimension filters. Treating them
    as dimension names appends an unresolvable LogicalFilter that breaks
    every query for this persona."""
    bound = make_bound_query(dimensions=[], measures=[])
    p = _persona(default_filters={"@region": "APAC", "year": 2026})

    merged = merge_default_filters(p, bound)

    # Only the real dimension filter is merged; @region is skipped.
    assert merged == ["year"]
    assert len(bound.resolved_filters) == 1
    assert bound.resolved_filters[0].dimension_name == "year"


def test_merge_skips_all_at_prefixed_keys():
    """Bug-7662: verify ALL @-prefixed keys are skipped, not just the first."""
    bound = make_bound_query(dimensions=[], measures=[])
    p = _persona(default_filters={"@p1": "x", "@p2": ["a", "b"]})

    merged = merge_default_filters(p, bound)

    assert merged == []
    assert bound.resolved_filters == []


# ---------------------------------------------------------------------------
# F-008-03 — complex SQL fails CLOSED against persona allow lists / filters
# ---------------------------------------------------------------------------


def _complex_bound():
    """Bound query in the complex-SQL passthrough shape: the binder skips
    column resolution, so the resolved lists are empty."""
    bound = make_bound_query(
        dimensions=[], measures=[],
        raw_sql="SELECT * FROM (SELECT cost FROM test_model) t",
    )
    bound.logical_query.has_complex_sql = True
    return bound


def test_complex_sql_with_measure_allow_list_fails_closed():
    """A persona allow list must not be bypassable with a one-line
    subquery: complex SQL cannot be validated, so it is rejected."""
    bound = _complex_bound()
    p = _persona(measure_ids=[str(uuid.uuid4())])

    with pytest.raises(HTTPException) as exc:
        enforce_persona(p, bound)

    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "PERSONA_COMPLEX_SQL_NOT_ALLOWED"


def test_complex_sql_with_dimension_allow_list_fails_closed():
    bound = _complex_bound()
    p = _persona(dimension_ids=[str(uuid.uuid4())])

    with pytest.raises(HTTPException) as exc:
        enforce_persona(p, bound)

    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "PERSONA_COMPLEX_SQL_NOT_ALLOWED"


def test_complex_sql_with_default_filters_fails_closed():
    """Persona default filters cannot be injected into raw passthrough SQL,
    so a scoping persona (e.g. region='EMEA') must reject complex SQL
    rather than silently run it unscoped."""
    bound = _complex_bound()
    p = _persona(default_filters={"region": "EMEA"})

    with pytest.raises(HTTPException) as exc:
        enforce_persona(p, bound)

    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "PERSONA_COMPLEX_SQL_NOT_ALLOWED"


def test_complex_sql_unrestricted_persona_passes():
    """A persona with no allow lists and no default filters (e.g. the
    technical persona) keeps the complex-SQL passthrough behaviour."""
    bound = _complex_bound()

    enforce_persona(_persona(), bound)  # must not raise


def test_count_star_allowed_under_measure_allow_list():
    """COUNT(*) binds to the synthetic __row_count measure, which has no
    ``id`` and references no modeled measure. A persona measure allow list
    must neither crash (live 500 found during B1 verification: business
    persona + ``SELECT region_code, count(*)`` raised AttributeError) nor
    block it."""
    allowed = make_measure("revenue")
    row_count = types.SimpleNamespace(
        name="__row_count", default_agg="count", is_additive=True,
        source_column_id=None,
    )
    bound = make_bound_query(dimensions=[], measures=[allowed, row_count])

    enforce_persona(_persona(measure_ids=[str(allowed.id)]), bound)  # must not raise


def test_count_star_survives_star_narrowing():
    """Star narrowing keeps the synthetic row-count measure."""
    allowed = make_measure("revenue")
    blocked = make_measure("cost")
    row_count = types.SimpleNamespace(
        name="__row_count", default_agg="count", is_additive=True,
        source_column_id=None,
    )
    bound = make_bound_query(
        dimensions=[], measures=[allowed, blocked, row_count], select_star=True,
    )

    enforce_persona(_persona(measure_ids=[str(allowed.id)]), bound)

    names = [m.name for m in bound.resolved_measures]
    assert "revenue" in names
    assert "__row_count" in names
    assert "cost" not in names


def test_f008_01_bare_select_of_hidden_measure_as_dimension_is_403():
    """F-008-01: binder wraps ``SELECT fee_amount`` as a virtual dimension
    whose id is the measure id. The measure allow-list must 403 it.
    """
    fee = make_measure("fee_amount")
    fee.is_measure_as_dimension = True
    allowed = make_measure("revenue")
    bound = make_bound_query(dimensions=[fee], measures=[], select_star=False)

    with pytest.raises(HTTPException) as exc:
        enforce_persona(_persona(measure_ids=[str(allowed.id)]), bound)

    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"


def test_f008_01_star_drops_hidden_measure_as_dimension():
    """F-008-01: SELECT * silently drops the virtual measure-dimension."""
    fee = make_measure("fee_amount")
    fee.is_measure_as_dimension = True
    region = make_dimension("region")
    allowed = make_measure("revenue")
    bound = make_bound_query(
        dimensions=[fee, region], measures=[allowed], select_star=True,
    )

    enforce_persona(_persona(measure_ids=[str(allowed.id)]), bound)

    names = [d.name for d in bound.resolved_dimensions]
    assert "fee_amount" not in names
    assert "region" in names
    assert bound.persona_narrowed_star is True


def test_f008_01_sum_of_excluded_measure_is_403():
    """F-008-01: SUM(hidden_measure) is 403 when the measure is not on the allow-list."""
    measure = make_measure("fee_amount")
    bound = make_bound_query(dimensions=[], measures=[measure])
    with pytest.raises(HTTPException) as exc:
        enforce_persona(_persona(measure_ids=[str(uuid.uuid4())]), bound)
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"


def test_f008_01_real_dim_sharing_excluded_measure_backing_column_is_403():
    """F-008-01: a real dimension whose source_column_id is the backing
    column of a hidden measure must 403 on explicit SELECT.
    """
    col_id = uuid.uuid4()
    fee = make_measure("fee_amount")
    dim = make_dimension("fee_amount")
    dim.source_column_id = col_id
    allowed = make_measure("revenue")
    bound = make_bound_query(dimensions=[dim], measures=[], select_star=False)

    with pytest.raises(HTTPException) as exc:
        enforce_persona(
            _persona(measure_ids=[str(allowed.id)]),
            bound,
            excluded_measure_column_ids={str(col_id)},
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
