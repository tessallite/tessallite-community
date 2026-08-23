"""F-023-07 / F-023-08 — server-side execution scope enforcement.

Business outcomes under test:

* F-023-07 — a recipe (or compound/direct query) that targets a model
  outside the project agent's allow-list is rejected at execution time
  by the single chokepoint (``execute_query``), and the two read tools
  (``evaluate_kpi`` / ``preview_named_set``) honour the allow-list too.
  Recipe CRUD is gated by project role: writes need a modeller/admin
  binding (or tenant_admin role), and step models must belong to the
  project.
* F-023-08 — the conversation's persona is enforced server-side at the
  same chokepoint: a tool call referencing a model or field outside the
  persona scope is refused regardless of what the LLM emitted. Prompt
  filtering remains defence-in-depth only.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.exec.query import (
    ModelNotAllowListedError,
    PersonaFieldScope,
    PersonaScopeViolationError,
    QueryExecutionError,
    QueryExecution,
    _extract_router_error_detail,
    enforce_execution_scope,
    execute_query,
)
from src.exec.recipe import RecipeExecutionError, execute_recipe
from src.pipeline import _allow_list_refusal_outcome
from src.tools.spec import (
    CreateAggregateToolCall,
    EvaluateKpiToolCall,
    PreviewNamedSetToolCall,
    QueryToolCall,
    RunRecipeToolCall,
)

from .conftest import TEST_TENANT, TEST_PROJECT_ID, make_mock_db

ALLOWED_MODEL = uuid.uuid4()
FORBIDDEN_MODEL = uuid.uuid4()


def _call(
    model_id: uuid.UUID = ALLOWED_MODEL,
    measures: list[str] | None = None,
    dimensions: list[str] | None = None,
    where: list[dict] | None = None,
    having: list[dict] | None = None,
    sort: list[dict] | None = None,
) -> QueryToolCall:
    return QueryToolCall(
        model_id=str(model_id),
        measures=measures if measures is not None else ["revenue"],
        dimensions=dimensions if dimensions is not None else ["country"],
        where=where or [],
        having=having or [],
        sort=sort or [],
        limit=100,
    )


def _scope(
    measures: frozenset[str] = frozenset({"revenue"}),
    dimensions: frozenset[str] = frozenset({"country"}),
) -> dict[uuid.UUID, PersonaFieldScope]:
    return {ALLOWED_MODEL: PersonaFieldScope(measures=measures, dimensions=dimensions)}


# ---------------------------------------------------------------------------
# Chokepoint function — allow-list
# ---------------------------------------------------------------------------


class TestAllowListEnforcement:
    def test_forbidden_model_raises(self):
        with pytest.raises(ModelNotAllowListedError):
            enforce_execution_scope(
                _call(FORBIDDEN_MODEL),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=None,
            )

    def test_allowed_model_no_persona_passes(self):
        out = enforce_execution_scope(
            _call(),
            allowed_model_ids={ALLOWED_MODEL},
            persona_scopes=None,
        )
        assert out == ALLOWED_MODEL

    def test_empty_allow_list_fails_closed(self):
        with pytest.raises(ModelNotAllowListedError):
            enforce_execution_scope(
                _call(), allowed_model_ids=set(), persona_scopes=None
            )

    def test_invalid_model_id_raises_query_error(self):
        with pytest.raises(QueryExecutionError):
            enforce_execution_scope(
                QueryToolCall(
                    model_id="not-a-uuid", measures=[], dimensions=[],
                    where=[], having=[], sort=[],
                ),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=None,
            )


# ---------------------------------------------------------------------------
# Chokepoint function — persona scope
# ---------------------------------------------------------------------------


class TestPersonaScopeEnforcement:
    def test_model_excluded_by_persona_raises(self):
        with pytest.raises(PersonaScopeViolationError):
            enforce_execution_scope(
                _call(),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes={},  # persona exposes no models -> fail closed
            )

    def test_out_of_scope_measure_raises_and_names_field(self):
        with pytest.raises(PersonaScopeViolationError) as exc:
            enforce_execution_scope(
                _call(measures=["salary"]),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=_scope(),
            )
        assert "salary" in str(exc.value)

    def test_out_of_scope_dimension_raises(self):
        with pytest.raises(PersonaScopeViolationError) as exc:
            enforce_execution_scope(
                _call(dimensions=["employee_name"]),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=_scope(),
            )
        assert "employee_name" in str(exc.value)

    def test_out_of_scope_where_field_raises(self):
        with pytest.raises(PersonaScopeViolationError) as exc:
            enforce_execution_scope(
                _call(where=[{"name": "ssn", "op": "eq", "value": "x"}]),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=_scope(),
            )
        assert "ssn" in str(exc.value)

    def test_out_of_scope_having_field_raises(self):
        with pytest.raises(PersonaScopeViolationError):
            enforce_execution_scope(
                _call(having=[{"name": "bonus", "op": "gt", "value": 1}]),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=_scope(),
            )

    def test_out_of_scope_sort_field_raises(self):
        with pytest.raises(PersonaScopeViolationError):
            enforce_execution_scope(
                _call(sort=[{"name": "bonus", "direction": "desc"}]),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=_scope(),
            )

    def test_fully_in_scope_call_passes(self):
        out = enforce_execution_scope(
            _call(
                where=[{"name": "country", "op": "eq", "value": "DE"}],
                sort=[{"name": "revenue", "direction": "desc"}],
            ),
            allowed_model_ids={ALLOWED_MODEL},
            persona_scopes=_scope(),
        )
        assert out == ALLOWED_MODEL

    def test_allow_list_checked_before_persona(self):
        # A forbidden model must surface as allow-list refusal even when
        # the persona mapping would also exclude it.
        with pytest.raises(ModelNotAllowListedError):
            enforce_execution_scope(
                _call(FORBIDDEN_MODEL),
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes={},
            )


# ---------------------------------------------------------------------------
# execute_query — enforcement happens before any DB or network use
# ---------------------------------------------------------------------------


class TestExecuteQueryChokepoint:
    def test_router_field_compatibility_detail_message_extracted(self):
        resp = httpx.Response(
            422,
            json={
                "detail": {
                    "message": (
                        "There is no aggregation path between Average Student Age "
                        "and Teacher Name. Average Student Age can be used with: "
                        "Student Grade, School."
                    ),
                    "error_type": "field_compatibility",
                    "field_compatibility": {"status": "incompatible", "issues": []},
                }
            },
        )

        detail = _extract_router_error_detail(resp)

        assert detail.startswith("There is no aggregation path")
        assert "field_compatibility" not in detail

    @pytest.mark.asyncio
    async def test_forbidden_model_rejected_before_db_access(self):
        db = AsyncMock()
        with pytest.raises(ModelNotAllowListedError):
            await execute_query(
                db, _call(FORBIDDEN_MODEL), "jwt",
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=None,
            )
        db.get.assert_not_awaited()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persona_violation_rejected_before_db_access(self):
        db = AsyncMock()
        with pytest.raises(PersonaScopeViolationError):
            await execute_query(
                db, _call(measures=["salary"]), "jwt",
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=_scope(),
            )
        db.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_in_scope_call_proceeds_past_enforcement(self):
        # db.get -> None makes the model lookup fail AFTER enforcement,
        # proving an in-scope call is not blocked by the gate.
        db = AsyncMock()
        db.get = AsyncMock(return_value=None)
        with pytest.raises(QueryExecutionError) as exc:
            await execute_query(
                db, _call(), "jwt",
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=_scope(),
            )
        assert "not found" in str(exc.value)
        db.get.assert_awaited_once()


# ---------------------------------------------------------------------------
# Recipe steps run through the same chokepoint
# ---------------------------------------------------------------------------


def _recipe(step_model: uuid.UUID, measures: list[str]) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        name="Margin",
        parameters=[],
        steps=[
            {
                "name": "sales",
                "model_id": str(step_model),
                "measures": measures,
                "dimensions": [],
            }
        ],
        combine=None,
    )


class TestRecipeExecutionEnforcement:
    @pytest.mark.asyncio
    async def test_recipe_step_on_forbidden_model_rejected(self):
        recipe = _recipe(FORBIDDEN_MODEL, ["revenue"])
        db = make_mock_db()
        db.get = AsyncMock(return_value=recipe)
        with pytest.raises(RecipeExecutionError) as exc:
            await execute_recipe(
                db, TEST_PROJECT_ID,
                RunRecipeToolCall(recipe_id=str(recipe.id), parameters={}),
                "jwt",
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=None,
            )
        assert isinstance(exc.value.__cause__, ModelNotAllowListedError)

    @pytest.mark.asyncio
    async def test_recipe_step_outside_persona_scope_rejected(self):
        recipe = _recipe(ALLOWED_MODEL, ["salary"])
        db = make_mock_db()
        db.get = AsyncMock(return_value=recipe)
        with pytest.raises(RecipeExecutionError) as exc:
            await execute_recipe(
                db, TEST_PROJECT_ID,
                RunRecipeToolCall(recipe_id=str(recipe.id), parameters={}),
                "jwt",
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=_scope(),
            )
        assert isinstance(exc.value.__cause__, PersonaScopeViolationError)

    @pytest.mark.asyncio
    async def test_saved_recipe_with_renamed_measure_executes_and_combines(self):
        """Bug-8096: rename propagation must keep both step and combine names
        aligned so an already-saved recipe remains executable."""
        recipe = types.SimpleNamespace(
            id=uuid.uuid4(),
            project_id=TEST_PROJECT_ID,
            name="Net revenue ratio",
            parameters=[],
            steps=[
                {
                    "name": "sales",
                    "model_id": str(ALLOWED_MODEL),
                    "measures": ["Net Revenue"],
                    "dimensions": [],
                },
                {
                    "name": "units",
                    "model_id": str(ALLOWED_MODEL),
                    "measures": ["Units"],
                    "dimensions": [],
                },
            ],
            combine={
                "op": "div",
                "args": [
                    {"ref": {"step": "sales", "measure": "Net Revenue"}},
                    {"ref": {"step": "units", "measure": "Units"}},
                ],
            },
        )
        db = make_mock_db()
        db.get = AsyncMock(return_value=recipe)
        executions = [
            QueryExecution(
                sql="select 100",
                columns=["Net Revenue"],
                rows=[{"Net Revenue": 100}],
                rows_returned=1,
                route_type="source",
                routed_sql=None,
                aggregate_id=None,
                pocket_id=None,
                execution_ms=1,
            ),
            QueryExecution(
                sql="select 4",
                columns=["Units"],
                rows=[{"Units": 4}],
                rows_returned=1,
                route_type="source",
                routed_sql=None,
                aggregate_id=None,
                pocket_id=None,
                execution_ms=1,
            ),
        ]

        persona_scopes = _scope()
        with patch(
            "src.exec.recipe.execute_query",
            new=AsyncMock(side_effect=executions),
        ) as query:
            result = await execute_recipe(
                db,
                TEST_PROJECT_ID,
                RunRecipeToolCall(recipe_id=str(recipe.id), parameters={}),
                "jwt",
                allowed_model_ids={ALLOWED_MODEL},
                persona_scopes=persona_scopes,
            )

        assert [call.args[1].measures for call in query.await_args_list] == [
            ["Net Revenue"],
            ["Units"],
        ]
        assert [call.kwargs["persona_scopes"] for call in query.await_args_list] == [
            persona_scopes,
            persona_scopes,
        ]
        assert all("persona_id" not in call.kwargs for call in query.await_args_list)
        assert result.combine_value == 25


# ---------------------------------------------------------------------------
# Read tools (evaluate_kpi / preview_named_set) + create_aggregate
# ---------------------------------------------------------------------------


class TestReadToolAllowList:
    def _bundle(self):
        return types.SimpleNamespace(
            allow_list_model_ids=[ALLOWED_MODEL], persona_scopes=None
        )

    @pytest.mark.asyncio
    async def test_evaluate_kpi_forbidden_model_refused(self):
        outcome = await _allow_list_refusal_outcome(
            EvaluateKpiToolCall(model_id=str(FORBIDDEN_MODEL), kpi_id="k"),
            self._bundle(), None, None,
        )
        assert outcome is not None
        assert outcome.status == "refused"
        assert outcome.guardrail_actions[0]["reason"] == "model_not_allow_listed"

    @pytest.mark.asyncio
    async def test_preview_named_set_forbidden_model_refused(self):
        outcome = await _allow_list_refusal_outcome(
            PreviewNamedSetToolCall(
                model_id=str(FORBIDDEN_MODEL), named_set_id="n"
            ),
            self._bundle(), None, None,
        )
        assert outcome is not None
        assert outcome.status == "refused"

    @pytest.mark.asyncio
    async def test_allowed_model_passes(self):
        outcome = await _allow_list_refusal_outcome(
            EvaluateKpiToolCall(model_id=str(ALLOWED_MODEL), kpi_id="k"),
            self._bundle(), None, None,
        )
        assert outcome is None

    @pytest.mark.asyncio
    async def test_create_aggregate_keeps_plan_shape(self):
        outcome = await _allow_list_refusal_outcome(
            CreateAggregateToolCall(
                model_id=str(FORBIDDEN_MODEL), measures=["m"],
                dimensions=["d"], description="x",
            ),
            self._bundle(), None, None,
        )
        assert outcome is not None
        assert outcome.plan == {
            "tool": "create_aggregate", "model_id": str(FORBIDDEN_MODEL)
        }

    @pytest.mark.asyncio
    async def test_invalid_model_id_refused(self):
        outcome = await _allow_list_refusal_outcome(
            EvaluateKpiToolCall(model_id="garbage", kpi_id="k"),
            self._bundle(), None, None,
        )
        assert outcome is not None
        assert outcome.status == "refused"


# ---------------------------------------------------------------------------
# Assembler — the bundle carries enforceable persona scopes
# ---------------------------------------------------------------------------


class TestPersonaFieldScopesDerivation:
    def _profile(self, model_id, measures, dimensions):
        from src.prompt.assembler import _ModelProfile

        return _ModelProfile(
            id=model_id, slug="m", display_name="M",
            overview=None, analytical_capabilities=None,
            abbreviation_conflict_rules=None, example_questions=[],
            measure_names=list(measures), dimension_names=list(dimensions),
            filterable_where_names=list(measures) + list(dimensions),
            sortable_names=list(measures) + list(dimensions),
            aggregates_summary=[], calendar_aliases=[], dimension_aliases=[],
            tagged_fields={}, dimension_value_hints={},
        )

    def test_scopes_match_filtered_prompt_exactly(self):
        from src.prompt.assembler import (
            _PersonaScope,
            _apply_persona_filter,
            _persona_field_scopes,
        )

        in_model = uuid.uuid4()
        out_model = uuid.uuid4()
        profiles = [
            self._profile(in_model, ["revenue", "salary"], ["country", "ssn"]),
            self._profile(out_model, ["qty"], ["sku"]),
        ]
        scopes = {
            in_model: _PersonaScope(
                model_id=in_model,
                measure_names={"revenue"},
                dimension_names={"country"},
            )
            # out_model has no scope row -> excluded entirely
        }
        filtered = _apply_persona_filter(profiles, scopes)
        field_scopes = _persona_field_scopes(filtered)

        assert set(field_scopes) == {in_model}
        assert field_scopes[in_model].measures == frozenset({"revenue"})
        assert field_scopes[in_model].dimensions == frozenset({"country"})

    def test_empty_include_lists_mean_full_model_access(self):
        from src.prompt.assembler import (
            _PersonaScope,
            _apply_persona_filter,
            _persona_field_scopes,
        )

        model_id = uuid.uuid4()
        profiles = [self._profile(model_id, ["revenue"], ["country"])]
        scopes = {
            model_id: _PersonaScope(
                model_id=model_id, measure_names=set(), dimension_names=set()
            )
        }
        filtered = _apply_persona_filter(profiles, scopes)
        field_scopes = _persona_field_scopes(filtered)
        assert field_scopes[model_id].measures == frozenset({"revenue"})
        assert field_scopes[model_id].dimensions == frozenset({"country"})


# ---------------------------------------------------------------------------
# Recipe CRUD role gate — endpoint level
# ---------------------------------------------------------------------------


def _user(role: str):
    from src.auth.middleware import CurrentUser

    return CurrentUser(
        user_id="user@example.com",
        tenant_id=TEST_TENANT,
        email="user@example.com",
        role=role,
    )


def _gate_db_member_without_modeller_binding() -> AsyncMock:
    """Bindings exist in the tenant (no bootstrap-open), but none grant
    this user modeller/admin: first query (user's binding) -> None,
    second query (any binding) -> a row."""
    db = AsyncMock()
    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None
    some_result = MagicMock()
    some_result.scalar_one_or_none.return_value = types.SimpleNamespace(
        role="viewer"
    )
    db.execute = AsyncMock(side_effect=[none_result, some_result])
    return db


def _recipe_payload() -> dict:
    return {
        "name": "Ratio",
        "description": None,
        "parameters": [],
        "steps": [
            {
                "name": "sales",
                "model_id": str(uuid.uuid4()),
                "measures": ["revenue"],
                "dimensions": [],
                "filters": [],
                "limit": 100,
            }
        ],
        "combine": None,
        "notes": None,
    }


async def _request(method: str, path: str, role: str, gate_db, endpoint_db, json=None):
    from src.auth.middleware import get_current_user
    from src.main import app

    app.dependency_overrides[get_current_user] = lambda: _user(role)
    try:
        with (
            patch("src.api.agent_config.get_tenant_db", lambda *a, **kw: _agen(gate_db)),
            patch("src.api.recipes.get_tenant_db", lambda *a, **kw: _agen(endpoint_db)),
            patch(
                "src.api.recipes.acquire_model_definition_lock",
                new_callable=AsyncMock,
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, json=json)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def _agen(db):
    yield db


def _endpoint_db(model_project_id=None) -> AsyncMock:
    db = make_mock_db()
    cfg_result = MagicMock()
    cfg_result.scalar_one_or_none.return_value = types.SimpleNamespace(
        project_id=TEST_PROJECT_ID
    )
    cfg_result.scalars.return_value.all.return_value = []
    measure_result = MagicMock()
    measure_result.scalars.return_value.all.return_value = ["revenue"]

    async def _execute(stmt):
        return measure_result if "FROM measures" in str(stmt) else cfg_result

    db.execute = AsyncMock(side_effect=_execute)

    async def _get(_cls, pk):
        return types.SimpleNamespace(
            id=pk, project_id=model_project_id or TEST_PROJECT_ID
        )

    db.get = AsyncMock(side_effect=_get)

    async def _refresh(record):
        if getattr(record, "id", None) is None:
            record.id = uuid.uuid4()

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


BASE = f"/api/v1/projects/{TEST_PROJECT_ID}/agent/recipes"


class TestRecipeCrudRoleGate:
    @pytest.mark.asyncio
    async def test_member_without_modeller_binding_cannot_create(self):
        resp = await _request(
            "POST", BASE, "member",
            _gate_db_member_without_modeller_binding(), _endpoint_db(),
            json=_recipe_payload(),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_without_modeller_binding_cannot_update(self):
        resp = await _request(
            "PUT", f"{BASE}/{uuid.uuid4()}", "member",
            _gate_db_member_without_modeller_binding(), _endpoint_db(),
            json=_recipe_payload(),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_member_without_modeller_binding_cannot_delete(self):
        resp = await _request(
            "DELETE", f"{BASE}/{uuid.uuid4()}", "member",
            _gate_db_member_without_modeller_binding(), _endpoint_db(),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_tenant_admin_can_create(self):
        resp = await _request(
            "POST", BASE, "tenant_admin",
            AsyncMock(), _endpoint_db(),
            json=_recipe_payload(),
        )
        assert resp.status_code == 201, resp.text

    @pytest.mark.asyncio
    async def test_member_with_binding_can_list(self):
        gate_db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = types.SimpleNamespace(
            role="viewer"
        )
        gate_db.execute = AsyncMock(return_value=result)
        resp = await _request(
            "GET", BASE, "member", gate_db, _endpoint_db(),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_step_model_outside_project_rejected_400(self):
        resp = await _request(
            "POST", BASE, "tenant_admin",
            AsyncMock(), _endpoint_db(model_project_id=uuid.uuid4()),
            json=_recipe_payload(),
        )
        assert resp.status_code == 400
        assert "not in project" in resp.text


# ── Bug-5279 / Bug-6329: persona field-scope on KPI/named-set/aggregate ──

MODEL_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_UNSET = object()


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    """Stand-in for a SQLAlchemy Result supporting ``.all()`` and
    ``.scalars().all()``."""

    def __init__(self, rows=None, *, scalar_rows=None):
        self._rows = rows or []
        self._scalar_rows = scalar_rows if scalar_rows is not None else (rows or [])

    def all(self):
        return self._rows

    def scalars(self):
        return _Scalars(self._scalar_rows)


def _kpi_lineage_db(*, get_return, measure_rows=None, dim_rows=None, kpi_rows=None):
    """Mock DB for the KPI lineage gate.

    ``db.get`` -> the target KPI; ``db.execute`` is called three times, in
    order: measure-id/name map (``.all()`` -> *measure_rows*), dimension
    id/name map (``.all()`` -> *dim_rows*), then the model KPI set
    (``.scalars().all()`` -> *kpi_rows*, for transitive kpi() refs)."""
    db = AsyncMock()
    db.get = AsyncMock(return_value=get_return)
    db.execute = AsyncMock(side_effect=[
        _Result(measure_rows or []),
        _Result(dim_rows or []),
        _Result(scalar_rows=kpi_rows or []),
    ])
    return db


def _ns_lineage_db(
    *, get_return, dim_rows=None,
    deployed_expression=_UNSET, deployed_dimensions=_UNSET,
):
    """Mock DB for the named-set lineage gate.

    The live NamedSet and its deployed snapshot intentionally share the
    definition in legacy tests; Bug-9029-specific tests vary the two to prove
    the precheck follows the deployed authority.
    """
    db = AsyncMock()
    version_id = uuid.uuid4()
    model = types.SimpleNamespace(
        id=getattr(get_return, "model_id", MODEL_ID),
        deployed_version_id=version_id,
    )
    version = types.SimpleNamespace(
        id=version_id,
        model_id=model.id,
        snapshot_json={
            "measures": [{"id": str(uuid.uuid4()), "name": "m1"}],
            "named_sets": [{
                "id": str(get_return.id),
                "expression": (
                    getattr(get_return, "expression", None)
                    if deployed_expression is _UNSET else deployed_expression
                ),
                "dimensions": (
                    getattr(get_return, "dimensions", None)
                    if deployed_dimensions is _UNSET else deployed_dimensions
                ),
            }],
        },
    )

    async def _get(cls, _id):
        if cls.__name__ == "NamedSet":
            return get_return
        if cls.__name__ == "Model":
            return model
        if cls.__name__ == "ModelVersion":
            return version
        return None

    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock(return_value=_Result(dim_rows or []))
    return db


def _kpi_row(*, expression=None, target_expression=None,
             value_measure_id=None, goal_measure_id=None,
             target_measure_id=None, time_dimension_id=None,
             name="KPI", model_id=MODEL_ID):
    return types.SimpleNamespace(
        id=uuid.uuid4(), model_id=model_id, name=name,
        expression=expression, target_expression=target_expression,
        value_measure_id=value_measure_id, goal_measure_id=goal_measure_id,
        target_measure_id=target_measure_id, time_dimension_id=time_dimension_id,
    )


def _ns_row(*, expression=None, dimensions=None, model_id=MODEL_ID):
    return types.SimpleNamespace(
        id=uuid.uuid4(), model_id=model_id, expression=expression,
        dimensions=dimensions,
    )


class TestPersonaScopeOnNonQueryTools:
    """Bug-5279 — the KPI, named-set, and create-aggregate branches must
    enforce the persona field scope, not just the model allow-list.
    Bug-6329 — the KPI / named-set checks resolve measure / dimension
    lineage from the DB (not an always-complete profile list)."""

    def _bundle(self, *, persona_scopes=None):
        return types.SimpleNamespace(
            allow_list_model_ids=[MODEL_ID],
            persona_scopes=persona_scopes,
            model_profiles=[],
        )

    @pytest.mark.asyncio
    async def test_create_aggregate_blocked_when_measure_outside_persona(self):
        call = CreateAggregateToolCall(
            model_id=str(MODEL_ID),
            measures=["hidden_measure"],
            dimensions=["dim1"],
            description="test",
        )
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["dim1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        result = await _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is not None
        assert result.status == "refused"
        assert "persona_scope_violation" in str(result.guardrail_actions)

    @pytest.mark.asyncio
    async def test_create_aggregate_passes_when_all_fields_in_scope(self):
        call = CreateAggregateToolCall(
            model_id=str(MODEL_ID),
            measures=["visible_measure"],
            dimensions=["dim1"],
            description="test",
        )
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["dim1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        result = await _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_evaluate_kpi_blocked_when_measure_hidden(self):
        """Bug-6329 — a KPI whose expression references a measure the
        persona cannot see must be refused, even though the (unfiltered)
        prompt profile still lists it."""
        kpi = _kpi_row(expression='measure("hidden_measure")')
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(get_return=kpi, kpi_rows=[kpi])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"
        assert "persona_scope_violation" in str(result.guardrail_actions)

    @pytest.mark.asyncio
    async def test_evaluate_kpi_blocked_via_legacy_measure_id(self):
        """Bug-6329 — legacy value/goal/target measure-id bindings are
        resolved and gated too."""
        hidden_mid = uuid.uuid4()
        kpi = _kpi_row(value_measure_id=hidden_mid)
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        # measure table maps the hidden id to a name NOT in the visible set
        db = _kpi_lineage_db(
            get_return=kpi, measure_rows=[(hidden_mid, "hidden_measure")], kpi_rows=[kpi],
        )
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_passes_when_measure_visible(self):
        """Bug-6329 — a KPI on a visible measure is allowed through."""
        kpi = _kpi_row(expression='measure("visible_measure")')
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(get_return=kpi, kpi_rows=[kpi])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is None

    @pytest.mark.asyncio
    async def test_evaluate_kpi_fail_closed_when_kpi_missing(self):
        """Bug-6329 — an unresolvable KPI id is refused (fail-closed)."""
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(uuid.uuid4()))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(get_return=None)
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_fail_closed_on_unparseable_expression(self):
        """Bug-6329 round-2 (Codex) — a KPI whose expression cannot be
        parsed has indeterminate lineage and must be refused, not allowed
        through with an empty inferred measure set (fail-closed)."""
        kpi = _kpi_row(expression="this is not a valid ((kpi expression")
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(get_return=kpi, kpi_rows=[kpi])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_blocked_via_composite_child_hidden_measure(self):
        """Bug-6329 round-2 (Codex) — a composite KPI referencing a child
        kpi() built on a hidden measure must be refused (transitive
        lineage), even though the parent expression names no measure."""
        child = _kpi_row(name="Child KPI", expression='measure("hidden_measure")')
        parent = _kpi_row(name="Parent KPI", expression='kpi("Child KPI")')
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(parent.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(get_return=parent, kpi_rows=[parent, child])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_composite_passes_when_child_measure_visible(self):
        """Bug-6329 round-2 — a composite KPI whose child references only a
        visible measure is allowed (transitive resolution succeeds)."""
        child = _kpi_row(name="Child KPI", expression='measure("visible_measure")')
        parent = _kpi_row(name="Parent KPI", expression='kpi("Child KPI")')
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(parent.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(get_return=parent, kpi_rows=[parent, child])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is None

    @pytest.mark.asyncio
    async def test_evaluate_kpi_fail_closed_on_unresolved_kpi_ref(self):
        """Bug-6329 round-2 — a kpi() dependency that resolves to no KPI in
        the model is indeterminate lineage and must be refused."""
        parent = _kpi_row(name="Parent KPI", expression='kpi("Missing Child")')
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(parent.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(get_return=parent, kpi_rows=[parent])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_blocked_when_dimension_ref_hidden(self):
        """Bug-6329 round-2 (Codex deep-review) — a KPI whose expression
        references a hidden dimension via dimension() must be refused; the
        measure-only gate let this through."""
        kpi = _kpi_row(expression='safe_div(measure("visible_measure"), dimension("hidden_dim"))')
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["visible_dim"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(
            get_return=kpi,
            dim_rows=[(uuid.uuid4(), "hidden_dim"), (uuid.uuid4(), "visible_dim")],
            kpi_rows=[kpi],
        )
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_blocked_when_time_dimension_hidden(self):
        """Bug-6329 round-2 — a KPI bound to a hidden time_dimension_id must
        be refused."""
        hidden_dim_id = uuid.uuid4()
        kpi = _kpi_row(
            expression='measure("visible_measure")', time_dimension_id=hidden_dim_id,
        )
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["visible_dim"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(
            get_return=kpi,
            dim_rows=[(hidden_dim_id, "hidden_dim"), (uuid.uuid4(), "visible_dim")],
            kpi_rows=[kpi],
        )
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_fail_closed_on_unresolved_dimension_ref(self):
        """Bug-6329 round-2 verify — a dimension() ref that resolves to no
        model dimension is indeterminate lineage and must be refused, not
        silently ignored (symmetric with measure/kpi handling)."""
        kpi = _kpi_row(expression='safe_div(measure("visible_measure"), dimension("ghost_dim"))')
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["visible_dim"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(
            get_return=kpi,
            dim_rows=[(uuid.uuid4(), "visible_dim")],  # ghost_dim absent
            kpi_rows=[kpi],
        )
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_fail_closed_on_dangling_time_dimension(self):
        """Bug-6329 round-2 verify — a time_dimension_id that does not
        resolve to a model dimension (deleted/dangling) is indeterminate
        lineage and must be refused."""
        kpi = _kpi_row(
            expression='measure("visible_measure")', time_dimension_id=uuid.uuid4(),
        )
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["visible_dim"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(
            get_return=kpi,
            dim_rows=[(uuid.uuid4(), "visible_dim")],  # the tdid is not present
            kpi_rows=[kpi],
        )
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_evaluate_kpi_passes_when_dimension_visible(self):
        """Bug-6329 round-2 — a KPI referencing only a visible dimension and
        a visible time dimension is allowed."""
        visible_dim_id = uuid.uuid4()
        kpi = _kpi_row(
            expression='safe_div(measure("visible_measure"), dimension("visible_dim"))',
            time_dimension_id=visible_dim_id,
        )
        call = EvaluateKpiToolCall(model_id=str(MODEL_ID), kpi_id=str(kpi.id))
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["visible_dim"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _kpi_lineage_db(
            get_return=kpi,
            dim_rows=[(uuid.uuid4(), "hidden_dim"), (visible_dim_id, "visible_dim")],
            kpi_rows=[kpi],
        )
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_persona_allows_through(self):
        call = CreateAggregateToolCall(
            model_id=str(MODEL_ID),
            measures=["any_measure"],
            dimensions=["any_dim"],
            description="test",
        )
        bundle = self._bundle(persona_scopes=None)
        result = await _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is None

    @pytest.mark.asyncio
    async def test_model_not_in_persona_refused(self):
        call = CreateAggregateToolCall(
            model_id=str(MODEL_ID),
            measures=["m1"],
            dimensions=["d1"],
            description="test",
        )
        # Persona scopes dict is set but the model is NOT in it
        bundle = self._bundle(persona_scopes={})
        result = await _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is not None
        assert result.status == "refused"
        assert "persona" in result.answer_text.lower()

    @pytest.mark.asyncio
    async def test_preview_named_set_blocked_when_dimension_hidden(self):
        """Bug-6329 — a named set referencing a real model dimension the
        persona cannot see must be refused."""
        ns = _ns_row(expression="[hidden_dim].members")
        call = PreviewNamedSetToolCall(model_id=str(MODEL_ID), named_set_id=str(ns.id))
        scope = PersonaFieldScope(
            measures=frozenset(["m1"]),
            dimensions=frozenset(["visible_dim"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _ns_lineage_db(get_return=ns, dim_rows=[("hidden_dim",), ("visible_dim",)])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"
        assert "persona_scope_violation" in str(result.guardrail_actions)

    @pytest.mark.asyncio
    async def test_preview_named_set_blocked_via_authoritative_dimensions_field(self):
        """Bug-6329 round-2 (Codex) — the authoritative persisted
        ``dimensions`` field is also consulted: a named set that names a
        hidden dimension there is refused even when the MDX expression
        yields no confident reference."""
        ns = _ns_row(expression="{ some.opaque.Members }", dimensions="hidden_dim, other")
        call = PreviewNamedSetToolCall(model_id=str(MODEL_ID), named_set_id=str(ns.id))
        scope = PersonaFieldScope(
            measures=frozenset(["m1"]),
            dimensions=frozenset(["visible_dim"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _ns_lineage_db(get_return=ns, dim_rows=[("hidden_dim",), ("visible_dim",)])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is not None
        assert result.status == "refused"

    @pytest.mark.asyncio
    async def test_preview_named_set_passes_when_dimension_visible(self):
        """Bug-6329 — a named set on a visible dimension is allowed."""
        ns = _ns_row(expression="[visible_dim].members")
        call = PreviewNamedSetToolCall(model_id=str(MODEL_ID), named_set_id=str(ns.id))
        scope = PersonaFieldScope(
            measures=frozenset(["m1"]),
            dimensions=frozenset(["visible_dim"]),
        )
        bundle = self._bundle(persona_scopes={MODEL_ID: scope})
        db = _ns_lineage_db(get_return=ns, dim_rows=[("hidden_dim",), ("visible_dim",)])
        result = await _allow_list_refusal_outcome(call, bundle, None, None, db=db)
        assert result is None

    @pytest.mark.asyncio
    async def test_preview_named_set_uses_deployed_expression_not_live_draft(self):
        """Bug-9029: a hidden live draft must not block a published visible set."""
        ns = _ns_row(expression="[hidden_dim].members")
        call = PreviewNamedSetToolCall(model_id=str(MODEL_ID), named_set_id=str(ns.id))
        scope = PersonaFieldScope(
            measures=frozenset(["m1"]),
            dimensions=frozenset(["visible_dim"]),
        )
        db = _ns_lineage_db(
            get_return=ns,
            dim_rows=[("hidden_dim",), ("visible_dim",)],
            deployed_expression="[visible_dim].members",
            deployed_dimensions="visible_dim",
        )
        result = await _allow_list_refusal_outcome(call, bundle=self._bundle(
            persona_scopes={MODEL_ID: scope}
        ), prompt_messages=None, llm_raw_response=None, db=db)
        assert result is None

    @pytest.mark.asyncio
    async def test_preview_named_set_follows_deployed_hidden_dimension(self):
        """Bug-9029: a live-visible draft cannot authorise a hidden published set."""
        ns = _ns_row(expression="[visible_dim].members")
        call = PreviewNamedSetToolCall(model_id=str(MODEL_ID), named_set_id=str(ns.id))
        scope = PersonaFieldScope(
            measures=frozenset(["m1"]),
            dimensions=frozenset(["visible_dim"]),
        )
        db = _ns_lineage_db(
            get_return=ns,
            dim_rows=[("hidden_dim",), ("visible_dim",)],
            deployed_expression="[hidden_dim].members",
            deployed_dimensions="hidden_dim",
        )
        result = await _allow_list_refusal_outcome(
            call, self._bundle(persona_scopes={MODEL_ID: scope}), None, None, db=db
        )
        assert result is not None
        assert result.status == "refused"
