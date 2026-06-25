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


# ---------------------------------------------------------------------------
# Read tools (evaluate_kpi / preview_named_set) + create_aggregate
# ---------------------------------------------------------------------------


class TestReadToolAllowList:
    def _bundle(self):
        return types.SimpleNamespace(
            allow_list_model_ids=[ALLOWED_MODEL], persona_scopes=None
        )

    def test_evaluate_kpi_forbidden_model_refused(self):
        outcome = _allow_list_refusal_outcome(
            EvaluateKpiToolCall(model_id=str(FORBIDDEN_MODEL), kpi_id="k"),
            self._bundle(), None, None,
        )
        assert outcome is not None
        assert outcome.status == "refused"
        assert outcome.guardrail_actions[0]["reason"] == "model_not_allow_listed"

    def test_preview_named_set_forbidden_model_refused(self):
        outcome = _allow_list_refusal_outcome(
            PreviewNamedSetToolCall(
                model_id=str(FORBIDDEN_MODEL), named_set_id="n"
            ),
            self._bundle(), None, None,
        )
        assert outcome is not None
        assert outcome.status == "refused"

    def test_allowed_model_passes(self):
        outcome = _allow_list_refusal_outcome(
            EvaluateKpiToolCall(model_id=str(ALLOWED_MODEL), kpi_id="k"),
            self._bundle(), None, None,
        )
        assert outcome is None

    def test_create_aggregate_keeps_plan_shape(self):
        outcome = _allow_list_refusal_outcome(
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

    def test_invalid_model_id_refused(self):
        outcome = _allow_list_refusal_outcome(
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
    db.execute = AsyncMock(return_value=cfg_result)

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


# ── Bug-5279: persona field-scope on KPI/named-set/create-aggregate ──────

class TestPersonaScopeOnNonQueryTools:
    """Bug-5279 — the KPI, named-set, and create-aggregate branches must
    enforce the persona field scope, not just the model allow-list."""

    def _bundle(self, *, persona_scopes=None, model_profiles=None, kpi_ids=None, ns_ids=None):
        model_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        profile = types.SimpleNamespace(
            id=model_id,
            kpis=[types.SimpleNamespace(id=k) for k in (kpi_ids or [])],
            named_sets=[types.SimpleNamespace(id=n) for n in (ns_ids or [])],
        )
        return types.SimpleNamespace(
            allow_list_model_ids=[model_id],
            persona_scopes=persona_scopes,
            model_profiles=model_profiles or [profile],
        )

    def test_create_aggregate_blocked_when_measure_outside_persona(self):
        from src.pipeline import _allow_list_refusal_outcome
        from src.tools.spec import CreateAggregateToolCall
        model_id = "11111111-1111-1111-1111-111111111111"
        call = CreateAggregateToolCall(
            model_id=model_id,
            measures=["hidden_measure"],
            dimensions=["dim1"],
            description="test",
        )
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["dim1"]),
        )
        bundle = self._bundle(
            persona_scopes={uuid.UUID(model_id): scope},
        )
        result = _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is not None
        assert result.status == "refused"
        assert "persona_scope_violation" in str(result.guardrail_actions)

    def test_create_aggregate_passes_when_all_fields_in_scope(self):
        from src.pipeline import _allow_list_refusal_outcome
        from src.tools.spec import CreateAggregateToolCall
        model_id = "11111111-1111-1111-1111-111111111111"
        call = CreateAggregateToolCall(
            model_id=model_id,
            measures=["visible_measure"],
            dimensions=["dim1"],
            description="test",
        )
        scope = PersonaFieldScope(
            measures=frozenset(["visible_measure"]),
            dimensions=frozenset(["dim1"]),
        )
        bundle = self._bundle(
            persona_scopes={uuid.UUID(model_id): scope},
        )
        result = _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is None

    def test_evaluate_kpi_blocked_when_kpi_not_in_profile(self):
        from src.pipeline import _allow_list_refusal_outcome
        from src.tools.spec import EvaluateKpiToolCall
        model_id = "11111111-1111-1111-1111-111111111111"
        call = EvaluateKpiToolCall(model_id=model_id, kpi_id="hidden-kpi")
        scope = PersonaFieldScope(
            measures=frozenset(["m1"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(
            persona_scopes={uuid.UUID(model_id): scope},
            kpi_ids=["visible-kpi"],  # hidden-kpi is not exposed
        )
        result = _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is not None
        assert result.status == "refused"

    def test_no_persona_allows_through(self):
        from src.pipeline import _allow_list_refusal_outcome
        from src.tools.spec import CreateAggregateToolCall
        model_id = "11111111-1111-1111-1111-111111111111"
        call = CreateAggregateToolCall(
            model_id=model_id,
            measures=["any_measure"],
            dimensions=["any_dim"],
            description="test",
        )
        bundle = self._bundle(persona_scopes=None)
        result = _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is None

    def test_model_not_in_persona_refused(self):
        from src.pipeline import _allow_list_refusal_outcome
        from src.tools.spec import CreateAggregateToolCall
        model_id = "11111111-1111-1111-1111-111111111111"
        call = CreateAggregateToolCall(
            model_id=model_id,
            measures=["m1"],
            dimensions=["d1"],
            description="test",
        )
        # Persona scopes dict is set but the model is NOT in it
        bundle = self._bundle(persona_scopes={})
        result = _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is not None
        assert result.status == "refused"
        assert "persona" in result.answer_text.lower()

    def test_preview_named_set_blocked_when_ns_not_in_profile(self):
        """Review R1 5279-F2 — preview_named_set must be persona-scoped."""
        from src.pipeline import _allow_list_refusal_outcome
        from src.tools.spec import PreviewNamedSetToolCall
        model_id = "11111111-1111-1111-1111-111111111111"
        call = PreviewNamedSetToolCall(model_id=model_id, named_set_id="hidden-ns")
        scope = PersonaFieldScope(
            measures=frozenset(["m1"]),
            dimensions=frozenset(["d1"]),
        )
        bundle = self._bundle(
            persona_scopes={uuid.UUID(model_id): scope},
            ns_ids=["visible-ns"],  # hidden-ns is not exposed
        )
        result = _allow_list_refusal_outcome(call, bundle, None, None)
        assert result is not None
        assert result.status == "refused"
        assert "persona_scope_violation" in str(result.guardrail_actions)
