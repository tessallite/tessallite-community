"""Drill-through REST endpoint contract test — semantic gateway path.

Tests the HTTP-adjacent glue in ``src.api.drill_routes._handle_drill_through``
by mocking ``build_drill_sql`` and ``_handle_execute``.  The semantic builder
and execute pipeline have their own unit tests; this file locks the response
envelope, error-status mapping, and pagination logic.

Invariants under test:

* ``build_drill_sql`` raises a normal semantic error → 400; stale scoped
  cursors map to 409 so callers restart deliberately.
* Successful drill → response carries ``columns``, ``rows``, ``page``
  (cursor / next_cursor / has_more), ``drill_mode``, ``drill_dimension``,
  ``hierarchy_path``, ``drillable_hierarchies``.
* ``has_more`` is True when the execute pipeline returns more than
  ``effective_limit`` rows; the envelope truncates and surfaces
  ``next_cursor``.
* Row security, persona gating, and aggregate routing are handled by
  ``_handle_execute`` — no manual wrap in drill_routes.

T-R4-1 tests (drill_options persona validation):

* Nonexistent persona_id → 404.
* Wrong-model persona_id → 404.
* Persona excludes measure → 403.
* Persona filters out excluded hierarchies.
* Locked embed persona applied to drill_options.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.drill_routes import (
    DrillThroughRequest,
    _handle_drill_through,
    drill_options,
    drill_through,
)
from src.drill.cursor import CursorOrderTerm, DrillCursorSpec
from src.drill.semantic_builder import (
    DrillDimension,
    DrillSemanticError,
    DrillableHierarchy,
    HierarchyPathEntry,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid():
    return uuid.uuid4()


def _build_sql_return(
    *,
    sql='SELECT "month", SUM("amount") AS "amount" FROM "modely" WHERE "year" = 2025 GROUP BY "month" ORDER BY "month" ASC NULLS LAST LIMIT 101',
    model_id_str=None,
    cursor_spec=None,
    effective_limit=100,
    drill_dim=None,
    drill_mode="leaf",
    hierarchy_path=None,
    drillable=None,
    fact_table="orders",
    source_join_path=None,
):
    return (
        sql,
        model_id_str or str(_uuid()),
        cursor_spec or DrillCursorSpec.build(
            scope={"fixture": "rest"},
            order_terms=[CursorOrderTerm("month")],
            stable=True,
        ),
        effective_limit,
        drill_dim,
        drill_mode,
        hierarchy_path or [],
        drillable or [],
        fact_table,
        source_join_path or [],
    )


def _execute_response(rows=None, columns=None):
    return types.SimpleNamespace(
        rows=rows or [],
        columns=columns or ["month", "amount"],
        route_type="source",
        execution_ms=42,
        bytes_processed=1024,
        rows_returned=len(rows or []),
    )


# ---------------------------------------------------------------------------
# 400 — semantic builder error code
# ---------------------------------------------------------------------------


async def test_handle_surfaces_semantic_error_as_400():
    db = AsyncMock()
    body = DrillThroughRequest()

    with patch(
        "src.api.drill_routes.build_drill_sql",
        new=AsyncMock(side_effect=DrillSemanticError("MEASURE_NOT_FOUND", "Measure xyz not found")),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "MEASURE_NOT_FOUND"


async def test_handle_surfaces_stale_cursor_as_409():
    db = AsyncMock()
    body = DrillThroughRequest(cursor="old-scope-token")

    with patch(
        "src.api.drill_routes.build_drill_sql",
        new=AsyncMock(side_effect=DrillSemanticError(
            "STALE_CURSOR", "Cursor scope changed; restart the drill."
        )),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 409
    assert exc.value.detail["error_code"] == "STALE_CURSOR"


# ---------------------------------------------------------------------------
# 200 — response envelope shape with hierarchy drill
# ---------------------------------------------------------------------------


async def test_handle_returns_envelope_with_hierarchy_drill():
    db = AsyncMock()
    body = DrillThroughRequest(
        grouping_levels=[{"column": "year", "value": 2025}],
        limit=2,
    )
    hier_id = _uuid()
    drill_dim = DrillDimension(id=_uuid(), name="month", display_name="Month")
    drillable = [DrillableHierarchy(
        hierarchy_id=hier_id,
        hierarchy_name="business_date",
        current_level_name="Year",
        current_level_ordinal=0,
        next_level_name="Month",
        next_level_ordinal=1,
        next_level_dimension_id=drill_dim.id,
        next_level_dimension_name="month",
        next_level_dimension_display_name="Month",
    )]
    path = [HierarchyPathEntry(level_name="Year", dimension_name="year", value=2025)]

    rows = [
        {"month": 1, "amount": 100},
        {"month": 2, "amount": 200},
        {"month": 3, "amount": 300},  # extra row = has_more probe
    ]

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return(
                effective_limit=2,
                drill_dim=drill_dim,
                drill_mode="hierarchy",
                hierarchy_path=path,
                drillable=drillable,
            )),
        ),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(rows=rows, columns=["month", "amount"])),
        ),
    ):
        resp = await _handle_drill_through(_uuid(), body, db)

    assert resp.drill_mode == "hierarchy"
    assert resp.drill_dimension is not None
    assert resp.drill_dimension.name == "month"
    assert resp.columns == ["month", "amount"]
    assert len(resp.rows) == 2  # truncated from 3
    assert resp.page.has_more is True
    assert resp.page.next_cursor is not None
    assert resp.route_type == "source"
    assert resp.execution_ms == 42
    assert len(resp.hierarchy_path) == 1
    assert resp.hierarchy_path[0].level_name == "Year"
    assert len(resp.drillable_hierarchies) == 1
    assert resp.drillable_hierarchies[0].hierarchy_name == "business_date"


# ---------------------------------------------------------------------------
# 200 — leaf mode, no further drill
# ---------------------------------------------------------------------------


async def test_handle_returns_leaf_mode_no_further_drill():
    db = AsyncMock()
    body = DrillThroughRequest(
        grouping_levels=[{"column": "region", "value": "US"}],
        limit=5,
    )
    rows = [{"region": "US", "amount": 500}]

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return(
                effective_limit=5,
                drill_mode="leaf",
            )),
        ),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(rows=rows, columns=["region", "amount"])),
        ),
    ):
        resp = await _handle_drill_through(_uuid(), body, db)

    assert resp.drill_mode == "leaf"
    assert resp.drill_dimension is None
    assert resp.page.has_more is False
    assert resp.page.next_cursor is None
    assert len(resp.rows) == 1


# ---------------------------------------------------------------------------
# 200 — no next_cursor when page not full
# ---------------------------------------------------------------------------


async def test_handle_no_next_cursor_when_page_not_full():
    db = AsyncMock()
    body = DrillThroughRequest(limit=10)
    rows = [{"x": 1}, {"x": 2}]

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return(effective_limit=10)),
        ),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(rows=rows, columns=["x"])),
        ),
    ):
        resp = await _handle_drill_through(_uuid(), body, db)

    assert resp.page.has_more is False
    assert resp.page.next_cursor is None
    assert len(resp.rows) == 2


# ---------------------------------------------------------------------------
# Bug-8048 — stable keyset continuation or coded refusal
# ---------------------------------------------------------------------------


async def test_handle_rejects_multi_page_leaf_without_unique_order_key():
    db = AsyncMock()
    body = DrillThroughRequest(limit=10)
    rows = [{"x": i} for i in range(11)]  # 11 rows for effective_limit=10 → has_more
    unstable = DrillCursorSpec.build(
        scope={"fixture": "unstable"},
        order_terms=[CursorOrderTerm("x")],
        stable=False,
    )

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return(
                cursor_spec=unstable,
                effective_limit=10,
            )),
        ),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(rows=rows, columns=["x"])),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 409
    assert exc.value.detail["error_code"] == "STABLE_CURSOR_UNAVAILABLE"


async def test_handle_mints_keyset_cursor_from_last_visible_row():
    db = AsyncMock()
    body = DrillThroughRequest(limit=10)
    rows = [{"x": i} for i in range(11)]  # 11 rows for effective_limit=10 → has_more
    spec = DrillCursorSpec.build(
        scope={"fixture": "stable"},
        order_terms=[CursorOrderTerm("x")],
        stable=True,
    )

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return(
                cursor_spec=spec,
                effective_limit=10,
            )),
        ),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(rows=rows, columns=["x"])),
        ),
    ):
        resp = await _handle_drill_through(_uuid(), body, db)

    assert resp.page.has_more is True
    assert resp.page.next_cursor is not None
    decoded = spec.decode(resp.page.next_cursor)
    assert decoded is not None
    assert decoded[0].value == 9


async def test_handle_maps_oversize_producer_cursor_to_coded_409():
    db = AsyncMock()
    body = DrillThroughRequest(limit=10)
    order_terms = [CursorOrderTerm(f"key_{index}") for index in range(4)]
    spec = DrillCursorSpec.build(
        scope={"fixture": "oversize"}, order_terms=order_terms, stable=True,
    )
    huge_row = {term.name: "x" * 4096 for term in order_terms}
    rows = [dict(huge_row, row=index) for index in range(11)]

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return(
                cursor_spec=spec,
                effective_limit=10,
            )),
        ),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(
                rows=rows, columns=[term.name for term in order_terms],
            )),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 409
    assert exc.value.detail["error_code"] == "CURSOR_TOO_LARGE"


# ---------------------------------------------------------------------------
# Invalid hierarchy_id → 400
# ---------------------------------------------------------------------------


async def test_handle_invalid_hierarchy_id():
    db = AsyncMock()
    body = DrillThroughRequest(hierarchy_id="not-a-uuid")

    with pytest.raises(HTTPException) as exc:
        await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 400
    assert "hierarchy_id" in str(exc.value.detail).lower()


# ---------------------------------------------------------------------------
# Execute pipeline error → 502
# ---------------------------------------------------------------------------


async def test_handle_execute_pipeline_error_becomes_502():
    """Bug-8809: 502 on an untyped pipeline failure, and the exception text
    must NOT be echoed to the caller. An execute-pipeline exception routinely
    carries the physical table/schema (driver errors, generation-guard
    messages, rewriter assertions quoting the rewritten SQL), and this route
    accepts embed credentials."""
    db = AsyncMock()
    body = DrillThroughRequest()
    leak = "confidential_fact_ledger"

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(side_effect=RuntimeError(
                f"connection lost while scanning acme_aggregates.{leak}"
            )),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 502
    body_text = str(exc.value.detail)
    assert leak not in body_text, body_text
    assert "acme_aggregates" not in body_text, body_text
    assert exc.value.detail["error_code"] == "drill_execution_failed"


# ---------------------------------------------------------------------------
# Persona passthrough: persona_id reaches _handle_execute
# ---------------------------------------------------------------------------


async def test_handle_passes_persona_id_to_execute():
    db = AsyncMock()
    persona_id = str(_uuid())
    body = DrillThroughRequest(persona_id=persona_id, limit=5)
    rows = []

    captured = {}

    async def _fake_execute(req, db, **kwargs):
        captured["persona_id"] = kwargs.get("persona_id")
        captured["model_id"] = req.model_id
        return _execute_response(rows=rows)

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        await _handle_drill_through(_uuid(), body, db, tenant_id="acme")

    assert captured["persona_id"] == persona_id


# ---------------------------------------------------------------------------
# F-019-19: force_route passthrough + validation
# ---------------------------------------------------------------------------


async def test_handle_passes_force_route_to_execute_request():
    db = AsyncMock()
    body = DrillThroughRequest(force_route="source", limit=5)
    captured = {}

    async def _fake_execute(req, db, **kwargs):
        captured["force_route"] = req.force_route
        return _execute_response(rows=[])

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        await _handle_drill_through(_uuid(), body, db, tenant_id="acme")

    assert captured["force_route"] == "source"


async def test_handle_labels_drill_traffic_distinctly():
    # Bug-6430: drill-through REST executions must be attributable in telemetry
    # rather than blending into BI JDBC traffic. protocol stays "jdbc" (so the
    # generated GROUP BY SQL keeps strict-parser treatment) but client_kind is
    # tagged "drill".
    db = AsyncMock()
    body = DrillThroughRequest(limit=5)
    captured = {}

    async def _fake_execute(req, db, **kwargs):
        captured["protocol"] = req.protocol
        captured["client_kind"] = req.client_kind
        return _execute_response(rows=[])

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        await _handle_drill_through(_uuid(), body, db, tenant_id="acme")

    assert captured["protocol"] == "jdbc"
    assert captured["client_kind"] == "drill"


async def test_handle_passes_override_agg_to_builder():
    """Bug-6273: a pivot column's non-default aggregate must reach the
    hierarchy drill builder so drill reconciliation uses the clicked column's
    aggregate instead of the measure default."""
    db = AsyncMock()
    body = DrillThroughRequest(
        grouping_levels=[{"column": "year", "value": 2025}],
        override_agg="avg",
        limit=5,
    )
    captured = {}

    async def _fake_build(**kwargs):
        captured["override_agg"] = kwargs.get("override_agg")
        return _build_sql_return(drill_mode="hierarchy")

    with (
        patch("src.api.drill_routes.build_drill_sql", new=_fake_build),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(rows=[])),
        ),
    ):
        await _handle_drill_through(_uuid(), body, db, tenant_id="acme")

    assert captured["override_agg"] == "avg"


async def test_handle_rejects_invalid_force_route_before_execution():
    db = AsyncMock()
    body = DrillThroughRequest(force_route="aggregate_only")

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ) as build_mock,
        patch("src.api.drill_routes._handle_execute", new=AsyncMock()) as execute_mock,
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 422
    build_mock.assert_not_awaited()
    execute_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Valid hierarchy_id → passed to build_drill_sql
# ---------------------------------------------------------------------------


async def test_handle_passes_valid_hierarchy_id_to_builder():
    db = AsyncMock()
    hier_id = _uuid()
    body = DrillThroughRequest(
        grouping_levels=[{"column": "year", "value": 2025}],
        hierarchy_id=str(hier_id),
        limit=5,
    )

    captured = {}

    async def _fake_build(**kwargs):
        captured["hierarchy_id"] = kwargs.get("hierarchy_id")
        return _build_sql_return(drill_mode="hierarchy")

    with (
        patch("src.api.drill_routes.build_drill_sql", new=_fake_build),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(rows=[])),
        ),
    ):
        resp = await _handle_drill_through(_uuid(), body, db)

    assert captured["hierarchy_id"] == hier_id
    assert resp.drill_mode == "hierarchy"


# ---------------------------------------------------------------------------
# Bug-6274 [SECURITY] — /drill-through honours the persona hierarchy allow-list
# ---------------------------------------------------------------------------


async def test_handle_forwards_allowed_hierarchy_ids_to_builder():
    """The persona allow-list must reach build_drill_sql so the single-hierarchy
    auto-select cannot step down a non-allowed hierarchy."""
    db = AsyncMock()
    allowed = {str(_uuid())}
    body = DrillThroughRequest(
        grouping_levels=[{"column": "year", "value": 2025}], limit=5,
    )
    captured = {}

    async def _fake_build(**kwargs):
        captured["allowed"] = kwargs.get("allowed_hierarchy_ids")
        return _build_sql_return(drill_mode="leaf")

    with (
        patch("src.api.drill_routes.build_drill_sql", new=_fake_build),
        patch(
            "src.api.drill_routes._handle_execute",
            new=AsyncMock(return_value=_execute_response(rows=[])),
        ),
    ):
        await _handle_drill_through(_uuid(), body, db, allowed_hierarchy_ids=allowed)

    assert captured["allowed"] == allowed


async def _call_drill_through(measure_id, body, current_user, db, *, persona=None):
    """Invoke the drill_through route with mocked tenant DB + persona resolver."""
    async def _fake_tenant_db(tenant_id):
        yield db

    with (
        patch("src.api.drill_routes.get_tenant_db", _fake_tenant_db),
        patch("src.api.drill_routes._enforce_measure_model_scope", new=AsyncMock()),
        patch("src.api.drill_routes.load_authorized_model", new=AsyncMock(return_value=None)),
        patch(
            "src.api.drill_routes.resolve_execution_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.drill_routes._handle_drill_through",
            new=AsyncMock(return_value="OK"),
        ) as handle_mock,
    ):
        result = await drill_through(
            body, measure_id,
            current_user=current_user,
            x_simulate_principal=None,
            x_simulate_roles=None,
            x_simulate_groups=None,
            x_simulate_claims=None,
        )
    return result, handle_mock


async def test_drill_through_forbidden_hierarchy_returns_403():
    """A persona with a non-empty included_hierarchy_ids may not drill a
    hierarchy outside that list; an explicit request for a forbidden hierarchy
    is rejected 403 before the pipeline runs."""
    measure_id = _uuid()
    model_id = _uuid()
    allowed_hier = _uuid()
    forbidden_hier = _uuid()
    persona = _mock_persona(
        persona_id=_uuid(), model_id=model_id,
        included_hierarchy_ids=[str(allowed_hier)],
    )
    db = _mock_db_for_drill_options(measure_model_id=model_id, persona=persona)
    body = DrillThroughRequest(
        grouping_levels=[{"column": "year", "value": 2025}],
        hierarchy_id=str(forbidden_hier),
    )
    user = _mock_current_user()

    with pytest.raises(HTTPException) as exc:
        await _call_drill_through(measure_id, body, user, db, persona=persona)
    assert exc.value.status_code == 403
    assert "hierarchy" in exc.value.detail.lower()


async def test_drill_through_forbidden_measure_returns_403():
    """SECURITY: /drill-through must enforce the persona MEASURE allow-list the
    same way /drill-options does — a persona scoped to other measures may not
    drill a forbidden measure's detail rows."""
    measure_id = _uuid()
    model_id = _uuid()
    other_measure = _uuid()
    persona = _mock_persona(
        persona_id=_uuid(), model_id=model_id,
        included_measure_ids=[str(other_measure)],
    )
    db = _mock_db_for_drill_options(measure_model_id=model_id, persona=persona)
    body = DrillThroughRequest(grouping_levels=[{"column": "year", "value": 2025}])
    user = _mock_current_user()

    with pytest.raises(HTTPException) as exc:
        await _call_drill_through(measure_id, body, user, db, persona=persona)
    assert exc.value.status_code == 403
    assert "measure" in exc.value.detail.lower()


async def test_drill_through_allowed_hierarchy_proceeds():
    """An explicit request for an ALLOWED hierarchy passes the gate and the
    allow-list is forwarded to the handler."""
    measure_id = _uuid()
    model_id = _uuid()
    allowed_hier = _uuid()
    persona = _mock_persona(
        persona_id=_uuid(), model_id=model_id,
        included_hierarchy_ids=[str(allowed_hier)],
    )
    db = _mock_db_for_drill_options(measure_model_id=model_id, persona=persona)
    body = DrillThroughRequest(
        grouping_levels=[{"column": "year", "value": 2025}],
        hierarchy_id=str(allowed_hier),
    )
    user = _mock_current_user()

    result, handle_mock = await _call_drill_through(
        measure_id, body, user, db, persona=persona,
    )
    assert result == "OK"
    handle_mock.assert_awaited_once()
    assert handle_mock.await_args.kwargs["allowed_hierarchy_ids"] == {str(allowed_hier)}


async def test_handle_passes_trusted_drill_join_path_to_execute_request():
    db = AsyncMock()
    join_id = str(_uuid())
    body = DrillThroughRequest(
        grouping_levels=[{"column": "region", "value": "EMEA"}],
        limit=5,
    )
    captured = {}

    async def _fake_execute(exec_request, *_args, **kwargs):
        captured["raw_query"] = exec_request.raw_query
        captured["drill_join_path_ids"] = kwargs.get("drill_join_path_ids")
        return _execute_response(rows=[])

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(
                return_value=_build_sql_return(
                    sql='SELECT "region", "amount" FROM "modely"',
                    source_join_path=[join_id],
                )
            ),
        ),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        await _handle_drill_through(_uuid(), body, db)

    assert "tessallite_drill_join_path" not in captured["raw_query"]
    assert captured["drill_join_path_ids"] == [join_id]


# ---------------------------------------------------------------------------
# T-R4-1 — drill_options persona validation regression tests
# ---------------------------------------------------------------------------


def _mock_current_user(*, tenant_id="test-tenant", email="test@test.com", persona_id=None, model_ids=None):
    """Build a mock CurrentUser. If persona_id is set, returns a CurrentEmbedUser."""
    from shared.auth.middleware import CurrentEmbedUser, CurrentUser
    if persona_id is not None:
        user = MagicMock(spec=CurrentEmbedUser)
        user.tenant_id = tenant_id
        user.email = email
        user.persona_id = persona_id
        user.model_ids = model_ids or []
    else:
        user = MagicMock(spec=CurrentUser)
        user.tenant_id = tenant_id
        user.email = email
    return user


def _mock_persona(*, persona_id, model_id, included_measure_ids=None, included_hierarchy_ids=None):
    p = MagicMock()
    p.id = persona_id
    p.model_id = model_id
    p.included_measure_ids = included_measure_ids or []
    p.included_hierarchy_ids = included_hierarchy_ids or []
    p.included_dimension_ids = []
    # Model the real Persona shape: an unset MagicMock attribute is truthy,
    # which would trip the resolver's visibility-widening guard. Both
    # widening flags must be concrete booleans — includes_hidden_columns
    # (F-008-04) and bypass_row_security (Bug-6136) — or every drill
    # persona would be misclassified as widening and dropped from its
    # audience.
    p.includes_hidden_columns = False
    p.bypass_row_security = False
    p.audience_roles = []
    return p


def _mock_db_for_drill_options(*, measure_model_id=None, persona=None):
    """Build a mock async DB session for drill_options persona logic.

    First execute call returns measure_model_id (Measure.model_id lookup).
    Second execute call returns persona (Persona lookup).
    """
    db = AsyncMock()
    call_count = 0

    async def _execute_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one_or_none.return_value = measure_model_id
        else:
            result.scalar_one_or_none.return_value = persona
        return result

    db.execute = AsyncMock(side_effect=_execute_side_effect)
    return db


async def _call_drill_options(measure_id, body, current_user, db, drillable=None):
    """Call drill_options with mocked dependencies."""
    async def _fake_tenant_db(tenant_id):
        yield db

    with (
        patch("src.api.drill_routes.get_tenant_db", _fake_tenant_db),
        patch("src.api.drill_routes._enforce_measure_model_scope", new=AsyncMock()),
        patch("src.api.drill_routes.load_authorized_model", new=AsyncMock(return_value=None)),
        patch(
            "src.api.drill_routes.resolve_drill_options",
            new=AsyncMock(return_value=drillable or []),
        ),
    ):
        return await drill_options(body, measure_id, current_user)


async def test_drill_options_nonexistent_persona_returns_404():
    measure_id = _uuid()
    model_id = _uuid()
    nonexistent_persona_id = _uuid()

    db = _mock_db_for_drill_options(measure_model_id=model_id, persona=None)
    body = DrillThroughRequest(persona_id=str(nonexistent_persona_id))
    user = _mock_current_user()

    with pytest.raises(HTTPException) as exc:
        await _call_drill_options(measure_id, body, user, db)

    assert exc.value.status_code == 404
    assert "persona" in exc.value.detail.lower()


async def test_drill_options_wrong_model_persona_returns_404():
    measure_id = _uuid()
    model_id = _uuid()
    other_model_id = _uuid()
    persona_id = _uuid()

    persona = _mock_persona(persona_id=persona_id, model_id=other_model_id)
    # Persona exists but model_id doesn't match measure's model,
    # so the constrained query (Persona.model_id == measure_model_id) returns None
    db = _mock_db_for_drill_options(measure_model_id=model_id, persona=None)
    body = DrillThroughRequest(persona_id=str(persona_id))
    user = _mock_current_user()

    with pytest.raises(HTTPException) as exc:
        await _call_drill_options(measure_id, body, user, db)

    assert exc.value.status_code == 404
    assert "persona" in exc.value.detail.lower()


async def test_drill_options_persona_excludes_measure_returns_403():
    measure_id = _uuid()
    model_id = _uuid()
    persona_id = _uuid()
    other_measure_id = _uuid()

    persona = _mock_persona(
        persona_id=persona_id,
        model_id=model_id,
        included_measure_ids=[str(other_measure_id)],
    )
    db = _mock_db_for_drill_options(measure_model_id=model_id, persona=persona)
    body = DrillThroughRequest(persona_id=str(persona_id))
    user = _mock_current_user()

    with pytest.raises(HTTPException) as exc:
        await _call_drill_options(measure_id, body, user, db)

    assert exc.value.status_code == 403
    assert "measure" in exc.value.detail.lower()


async def test_drill_options_persona_filters_excluded_hierarchies():
    measure_id = _uuid()
    model_id = _uuid()
    persona_id = _uuid()
    allowed_hier_id = _uuid()
    hidden_hier_id = _uuid()

    persona = _mock_persona(
        persona_id=persona_id,
        model_id=model_id,
        included_measure_ids=[],
        included_hierarchy_ids=[str(allowed_hier_id)],
    )
    db = _mock_db_for_drill_options(measure_model_id=model_id, persona=persona)
    body = DrillThroughRequest(persona_id=str(persona_id))
    user = _mock_current_user()

    drillable = [
        DrillableHierarchy(
            hierarchy_id=allowed_hier_id,
            hierarchy_name="geo",
            current_level_name="Country",
            current_level_ordinal=0,
            next_level_name="City",
            next_level_ordinal=1,
            next_level_dimension_id=_uuid(),
            next_level_dimension_name="city",
            next_level_dimension_display_name="City",
        ),
        DrillableHierarchy(
            hierarchy_id=hidden_hier_id,
            hierarchy_name="product",
            current_level_name="Category",
            current_level_ordinal=0,
            next_level_name="SKU",
            next_level_ordinal=1,
            next_level_dimension_id=_uuid(),
            next_level_dimension_name="sku",
            next_level_dimension_display_name="SKU",
        ),
    ]

    resp = await _call_drill_options(measure_id, body, user, db, drillable=drillable)

    assert len(resp.hierarchies) == 1
    assert resp.hierarchies[0].hierarchy_id == str(allowed_hier_id)
    assert resp.hierarchies[0].hierarchy_name == "geo"


async def test_drill_options_locked_embed_persona_applied():
    measure_id = _uuid()
    model_id = _uuid()
    locked_persona_id = _uuid()

    persona = _mock_persona(
        persona_id=locked_persona_id,
        model_id=model_id,
        included_measure_ids=[str(measure_id)],
    )
    db = _mock_db_for_drill_options(measure_model_id=model_id, persona=persona)
    # Body has no persona_id — embed token provides it
    body = DrillThroughRequest()
    user = _mock_current_user(persona_id=str(locked_persona_id), model_ids=[str(model_id)])

    drillable = [
        DrillableHierarchy(
            hierarchy_id=_uuid(),
            hierarchy_name="time",
            current_level_name="Year",
            current_level_ordinal=0,
            next_level_name="Month",
            next_level_ordinal=1,
            next_level_dimension_id=_uuid(),
            next_level_dimension_name="month",
            next_level_dimension_display_name="Month",
        ),
    ]

    resp = await _call_drill_options(measure_id, body, user, db, drillable=drillable)

    assert len(resp.hierarchies) == 1
    assert resp.hierarchies[0].hierarchy_name == "time"


# ---------------------------------------------------------------------------
# B1 (decision #11) — XMLA <Parameters> session_vars reach the drill query.
#
# The XMLA gateway forwards an Execute's declared <Parameters> as
# app.<name> session_vars on the /drill-through request body. This CONSUMER
# must accept that field on DrillThroughRequest and thread it into the
# ExecuteRequest it builds, so _handle_execute scopes the drill detail query
# by the same parameters as the main /execute path. Without the field, Pydantic
# would silently drop the gateway's session_vars and the drill would run
# UNSCOPED (producer/consumer field-drop). This locks the consumer end of B1.
# ---------------------------------------------------------------------------


async def test_b1_drillthrough_session_vars_thread_into_execute_request():
    db = AsyncMock()
    body = DrillThroughRequest(session_vars={"app.region": "EMEA"})
    # The field must be accepted, not silently dropped.
    assert body.session_vars == {"app.region": "EMEA"}

    captured = {}

    async def _capture_execute(exec_request, *args, **kwargs):
        captured["exec_request"] = exec_request
        return _execute_response(rows=[{"month": 1, "amount": 100}])

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ),
        patch("src.api.drill_routes._handle_execute", new=_capture_execute),
    ):
        await _handle_drill_through(_uuid(), body, db)

    # The drill detail query is scoped by the same declared parameters as the
    # main /execute path — pre-fix the ExecuteRequest carried no session_vars.
    assert captured["exec_request"].session_vars == {"app.region": "EMEA"}
