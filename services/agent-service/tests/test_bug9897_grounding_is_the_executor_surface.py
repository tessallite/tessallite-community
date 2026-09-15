"""Bug-9897 / persona-layering rule 4, audit row A45.

The agent's grounding catalogue was narrowed by ``ProjectPersonaModelScope``
(an agent-service object) while every query it issues is enforced by the
query-router against the model ``Persona`` it resolves from the caller's JWT.
Two policies over two different objects: the planner could be told a measure
exists, plan around it, and only discover at a 403 that the executor never had
it -- and, worse for a governance surface, be told a CLS-restricted measure
exists at all.

The catalogue is now the EXECUTOR's own answer: the query-router headless
metadata endpoints, which run ``resolve_execution_persona`` (the resolution
``/execute`` performs), the persona allow-list and the transitive
column-level-security closure over the deployed shape. The ProjectPersona scope
may then only narrow further (owner decision 4.6(a)).

Marked ``executor_surface_real`` so the conftest passthrough -- which
neutralises this hop for the prompt-rendering suites -- does not apply.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.exec.model_surface import (  # noqa: E402
    ExecutorModelSurface,
    load_executor_surfaces,
)
from src.prompt.assembler import (  # noqa: E402
    _ModelProfile,
    _PersonaScope,
    _apply_executor_surface,
    _apply_persona_filter,
    _persona_field_scopes,
)

pytestmark = pytest.mark.executor_surface_real

MODEL_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
MODEL_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _profile(model_id=MODEL_A):
    """A profile carrying the model's FULL surface, as the DB loader builds it."""
    return _ModelProfile(
        id=model_id,
        slug="sales",
        display_name="Sales",
        overview=None,
        analytical_capabilities=None,
        abbreviation_conflict_rules=None,
        example_questions=[],
        measure_names=["revenue", "salary_cost"],
        dimension_names=["region", "employee_email"],
        filterable_where_names=["region", "employee_email", "revenue"],
        sortable_names=["revenue", "salary_cost", "region"],
        aggregates_summary=[],
        calendar_aliases=[],
        dimension_aliases=[],
        tagged_fields={},
        dimension_value_hints={},
        measure_metadata={
            "revenue": "meta-revenue",
            "salary_cost": "meta-salary",
            # A cross-model REFERENCE measure: present only in metadata, never
            # in this model's executable measure_names.
            "group_revenue": "meta-group",
        },
        dimensions={
            "region": {"kind": "dimension"},
            "employee_email": {"kind": "dimension"},
        },
    )


def _surface(measures, dimensions, model_id=MODEL_A):
    return {model_id: ExecutorModelSurface(
        model_id=model_id,
        measure_names=frozenset(measures),
        dimension_names=frozenset(dimensions),
    )}


class TestTheCatalogueIsNarrowedToWhatTheExecutorAccepts:
    def test_a_measure_the_executor_refuses_is_not_advertised(self):
        """``salary_cost`` reaches a column-level-security restricted column,
        so the router withholds it. The prompt must not name it either."""
        [narrowed] = _apply_executor_surface(
            [_profile()], _surface(["revenue"], ["region", "employee_email"]),
        )
        assert narrowed.measure_names == ["revenue"]
        assert "salary_cost" not in narrowed.measure_metadata

    def test_a_dimension_the_executor_refuses_is_not_advertised(self):
        [narrowed] = _apply_executor_surface(
            [_profile()], _surface(["revenue", "salary_cost"], ["region"]),
        )
        assert narrowed.dimension_names == ["region"]
        assert set(narrowed.dimensions) == {"region"}

    def test_withheld_names_are_stripped_from_every_derived_list(self):
        """A name the executor refuses must not survive in the filter/sort
        vocabulary the planner is shown either."""
        [narrowed] = _apply_executor_surface(
            [_profile()], _surface(["revenue"], ["region"]),
        )
        assert "employee_email" not in narrowed.filterable_where_names
        assert "salary_cost" not in narrowed.sortable_names

    def test_the_source_profile_is_not_mutated(self):
        """Bug-7935 invariant: profiles are shared across concurrent requests,
        so one caller's narrowing must never reach another's prompt."""
        profile = _profile()
        _apply_executor_surface([profile], _surface(["revenue"], ["region"]))
        assert profile.measure_names == ["revenue", "salary_cost"]
        assert profile.dimension_names == ["region", "employee_email"]

    def test_a_cross_model_reference_measure_survives(self):
        """``group_revenue`` is metadata-only and disclosed as not directly
        queryable, so this model's executor verdict says nothing about it."""
        [narrowed] = _apply_executor_surface(
            [_profile()], _surface(["revenue"], ["region"]),
        )
        assert "group_revenue" in narrowed.measure_metadata


class TestFailClosed:
    def test_a_model_with_no_verdict_is_dropped(self):
        assert _apply_executor_surface([_profile()], {}) == []

    def test_a_model_the_executor_accepts_no_measure_for_is_dropped(self):
        assert _apply_executor_surface([_profile()], _surface([], ["region"])) == []

    def test_only_the_verdicted_models_survive(self):
        profiles = [_profile(MODEL_A), _profile(MODEL_B)]
        kept = _apply_executor_surface(
            profiles, _surface(["revenue"], ["region"], model_id=MODEL_A),
        )
        assert [p.id for p in kept] == [MODEL_A]


class TestTheProjectPersonaMayOnlyNarrowFurther:
    def test_project_persona_narrows_the_executor_surface(self):
        profiles = _apply_executor_surface(
            [_profile()],
            _surface(["revenue", "salary_cost"], ["region", "employee_email"]),
        )
        scopes = {MODEL_A: _PersonaScope(
            model_id=MODEL_A,
            measure_names={"revenue"},
            dimension_names={"region"},
        )}
        [final] = _apply_persona_filter(profiles, scopes)
        assert final.measure_names == ["revenue"]
        assert final.dimension_names == ["region"]

    def test_project_persona_cannot_widen_the_executor_surface(self):
        """A ProjectPersona naming a measure the executor refuses must not put
        it back: the intersection, not the union, is what the agent may plan
        against."""
        profiles = _apply_executor_surface(
            [_profile()], _surface(["revenue"], ["region"]),
        )
        scopes = {MODEL_A: _PersonaScope(
            model_id=MODEL_A,
            measure_names={"revenue", "salary_cost"},
            dimension_names={"region", "employee_email"},
        )}
        [final] = _apply_persona_filter(profiles, scopes)
        assert final.measure_names == ["revenue"]
        assert final.dimension_names == ["region"]

    def test_an_empty_project_persona_list_still_means_no_restriction(self):
        """Pre-existing contract preserved: an empty include list on a scope
        row is full access to whatever survived above it, including the
        metadata-only cross-model names."""
        profiles = _apply_executor_surface(
            [_profile()], _surface(["revenue"], ["region"]),
        )
        scopes = {MODEL_A: _PersonaScope(
            model_id=MODEL_A, measure_names=set(), dimension_names=set(),
        )}
        [final] = _apply_persona_filter(profiles, scopes)
        assert final.measure_names == ["revenue"]
        assert "group_revenue" in final.measure_metadata


class TestTheDefectItself:
    """The pre-fix pipeline, expressed with pre-fix symbols only, so this
    statement is checkable on either tree.

    Before this lane the ONLY narrowing between the loaded model surface and
    the prompt was ``_apply_persona_filter``. A ProjectPersona that includes
    ``salary_cost`` therefore advertised it -- with nothing anywhere in
    agent-service aware that the query-router withholds that measure for this
    caller, because its column closure reaches a column-level-security
    restricted column. The agent planned against a measure it could not read.
    """

    def test_project_persona_alone_advertises_a_measure_the_executor_refuses(self):
        scopes = {MODEL_A: _PersonaScope(
            model_id=MODEL_A,
            measure_names={"revenue", "salary_cost"},
            dimension_names={"region", "employee_email"},
        )}
        [prefix_shape] = _apply_persona_filter([_profile()], scopes)
        assert "salary_cost" in prefix_shape.measure_names, (
            "this documents the pre-fix behaviour; if it ever stops holding, "
            "the ProjectPersona filter has changed meaning"
        )

    def test_the_executor_verdict_removes_it(self):
        profiles = _apply_executor_surface(
            [_profile()], _surface(["revenue"], ["region"]),
        )
        scopes = {MODEL_A: _PersonaScope(
            model_id=MODEL_A,
            measure_names={"revenue", "salary_cost"},
            dimension_names={"region", "employee_email"},
        )}
        [final] = _apply_persona_filter(profiles, scopes)
        assert "salary_cost" not in final.measure_names


class TestThePromptAndTheChokepointCarryOneSurface:
    def test_the_execution_scope_equals_what_the_prompt_advertises(self):
        """The biconditional: what the catalogue shows is exactly what the
        chokepoint accepts, for the same identity."""
        [narrowed] = _apply_executor_surface(
            [_profile()], _surface(["revenue"], ["region"]),
        )
        scopes = _persona_field_scopes([narrowed])
        assert scopes[MODEL_A].measures == frozenset(narrowed.measure_names)
        assert scopes[MODEL_A].dimensions == frozenset(narrowed.dimension_names)
        assert "salary_cost" not in scopes[MODEL_A].measures


class TestTheVerdictComesFromTheExecutorItself:
    @pytest.mark.asyncio
    async def test_it_asks_the_query_router_with_the_callers_own_bearer(self):
        seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            leaf = request.url.path.rsplit("/", 1)[-1]
            body = (
                [{"id": "m1", "name": "revenue"}] if leaf == "measures"
                else [{"id": "d1", "name": "region"}]
            )
            return httpx.Response(200, json=body)

        transport = httpx.MockTransport(_handler)
        real_client = httpx.AsyncClient

        def _client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        with patch("src.exec.model_surface.httpx.AsyncClient", _client):
            surfaces = await load_executor_surfaces([MODEL_A], "caller-jwt")

        assert surfaces[MODEL_A].measure_names == frozenset({"revenue"})
        assert surfaces[MODEL_A].dimension_names == frozenset({"region"})
        assert {r.headers["authorization"] for r in seen} == {"Bearer caller-jwt"}
        assert {r.url.path.rsplit("/", 1)[-1] for r in seen} == {
            "measures", "dimensions",
        }
        for request in seen:
            assert f"/api/v1/headless/models/{MODEL_A}/" in request.url.path

    @pytest.mark.asyncio
    async def test_no_bearer_yields_no_verdict(self):
        assert await load_executor_surfaces([MODEL_A], None) == {}
        assert await load_executor_surfaces([MODEL_A], "") == {}

    @pytest.mark.asyncio
    async def test_a_refused_or_unreachable_model_yields_no_verdict(self):
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": "denied"})

        transport = httpx.MockTransport(_handler)
        real_client = httpx.AsyncClient

        def _client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        with patch("src.exec.model_surface.httpx.AsyncClient", _client):
            surfaces = await load_executor_surfaces([MODEL_A], "caller-jwt")

        assert surfaces == {}

    @pytest.mark.asyncio
    async def test_one_models_failure_does_not_lose_the_others(self):
        def _handler(request: httpx.Request) -> httpx.Response:
            if str(MODEL_B) in request.url.path:
                return httpx.Response(500, json={"detail": "boom"})
            leaf = request.url.path.rsplit("/", 1)[-1]
            body = (
                [{"id": "m1", "name": "revenue"}] if leaf == "measures"
                else [{"id": "d1", "name": "region"}]
            )
            return httpx.Response(200, json=body)

        transport = httpx.MockTransport(_handler)
        real_client = httpx.AsyncClient

        def _client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        with patch("src.exec.model_surface.httpx.AsyncClient", _client):
            surfaces = await load_executor_surfaces([MODEL_A, MODEL_B], "jwt")

        assert set(surfaces) == {MODEL_A}
