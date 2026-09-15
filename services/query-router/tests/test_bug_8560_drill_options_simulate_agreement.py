"""Bug-8560 (audit row A34) — the drill OPTION CATALOGUE and the drill EXECUTOR
must answer to the same identity, and offer exactly what will execute.

``/measures/{id}/drill-through`` was fixed under Bug-8363 to resolve the persona
against the SIMULATED identity. ``/measures/{id}/drill-options`` accepted no
simulate headers at all and resolved the persona against the REAL caller. With
simulation active an admin's drill-options list was therefore computed from the
ADMIN's persona — offering hierarchies the simulated user cannot reach — while
the very next ``/drill-through`` call on the same simulated identity enforced
the SIMULATED user's allow-list and answered 403. The preview offered a choice
it then refused.

The wave's invariant: what the catalogue advertises equals what the executor
accepts, for the same identity, BY CONSTRUCTION. Both endpoints now resolve the
identity through ``_persona_identity_for_drill``, gate through
``_resolve_drill_scope``, and narrow the drillable set through
``semantic_builder.filter_drillable_by_persona`` — one code path, not two kept
in step.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_8560_drill_options_simulate_agreement.py -v
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import src.api.drill_routes as drill_routes
from src.drill.semantic_builder import (
    DrillableHierarchy,
    filter_drillable_by_persona,
)


def _uuid():
    return uuid.uuid4()


MODEL_ID = _uuid()
MEASURE_ID = _uuid()
# A hierarchy the admin's persona allows and the simulated viewer's does not.
ADMIN_ONLY_HIER = _uuid()
# A hierarchy both personas allow.
SHARED_HIER = _uuid()


def _admin_user():
    from shared.auth.middleware import CurrentUser

    return CurrentUser(
        user_id="admin@x.com", tenant_id="t1", email="admin@x.com",
        role="tenant_admin", roles=["tenant_admin"],
    )


def _persona(name, *, hierarchy_ids, measure_ids=()):
    return types.SimpleNamespace(
        id=_uuid(),
        name=name,
        model_id=MODEL_ID,
        included_measure_ids=[str(m) for m in measure_ids],
        included_hierarchy_ids=[str(h) for h in hierarchy_ids],
        default_filters={},
    )


ADMIN_PERSONA = _persona(
    "Admin", hierarchy_ids=[ADMIN_ONLY_HIER, SHARED_HIER],
)
VIEWER_PERSONA = _persona("Viewer", hierarchy_ids=[SHARED_HIER])


def _hier(hierarchy_id, name):
    return DrillableHierarchy(
        hierarchy_id=hierarchy_id,
        hierarchy_name=name,
        current_level_name="year",
        current_level_ordinal=0,
        next_level_name="quarter",
        next_level_ordinal=1,
        next_level_dimension_id=_uuid(),
        next_level_dimension_name=f"{name}_quarter",
        next_level_dimension_display_name=f"{name} quarter",
    )


DRILLABLE = [
    _hier(ADMIN_ONLY_HIER, "admin_only"),
    _hier(SHARED_HIER, "shared"),
]


def _db():
    """Tenant DB double that answers the measure -> model_id lookup."""
    db = AsyncMock()
    result = AsyncMock()
    result.scalar_one_or_none = lambda: MODEL_ID
    db.execute = AsyncMock(return_value=result)
    return db


async def _persona_by_identity(_db, *, current_user, model_id, requested_persona_id):
    """Return the persona of whichever identity the endpoint resolved against.

    This is what regressed: a catalogue computed from the admin's persona while
    the executor enforced the viewer's.
    """
    if current_user.email == "viewer@x.com":
        return VIEWER_PERSONA
    return ADMIN_PERSONA


def _patches(*, handle_drill_through=None):
    db = _db()

    def _fake_tenant_db(_tenant_id):
        async def _gen():
            yield db
        return _gen()

    ctx = [
        patch.object(drill_routes, "get_tenant_db", _fake_tenant_db),
        patch.object(
            drill_routes, "_enforce_measure_model_scope", new=AsyncMock(),
        ),
        patch.object(
            drill_routes, "load_authorized_model", new=AsyncMock(return_value=None),
        ),
        patch.object(
            drill_routes, "resolve_execution_persona", new=_persona_by_identity,
        ),
        patch.object(
            drill_routes, "resolve_drill_options",
            new=AsyncMock(return_value=list(DRILLABLE)),
        ),
        patch.object(
            drill_routes, "_handle_drill_through",
            new=handle_drill_through or AsyncMock(return_value="OK"),
        ),
    ]
    return ctx


async def _options(*, simulate: bool) -> set[str]:
    """Hierarchy ids ``/drill-options`` advertises for this identity."""
    ctx = _patches()
    for c in ctx:
        c.start()
    try:
        resp = await drill_routes.drill_options(
            drill_routes.DrillThroughRequest(
                grouping_levels=[{"column": "year", "value": 2025}],
            ),
            measure_id=MEASURE_ID,
            current_user=_admin_user(),
            x_simulate_principal="viewer@x.com" if simulate else None,
            x_simulate_roles="viewer" if simulate else None,
            x_simulate_groups=None,
            x_simulate_claims=None,
        )
    finally:
        for c in reversed(ctx):
            c.stop()
    return {h.hierarchy_id.lower() for h in resp.hierarchies}


async def _executes(hierarchy_id, *, simulate: bool) -> bool:
    """True when ``/drill-through`` ACCEPTS this hierarchy for this identity."""
    handle = AsyncMock(return_value="OK")
    ctx = _patches(handle_drill_through=handle)
    for c in ctx:
        c.start()
    try:
        await drill_routes.drill_through(
            drill_routes.DrillThroughRequest(
                grouping_levels=[{"column": "year", "value": 2025}],
                hierarchy_id=str(hierarchy_id),
            ),
            measure_id=MEASURE_ID,
            current_user=_admin_user(),
            x_simulate_principal="viewer@x.com" if simulate else None,
            x_simulate_roles="viewer" if simulate else None,
            x_simulate_groups=None,
            x_simulate_claims=None,
        )
        return True
    except HTTPException as exc:
        if exc.status_code == 403:
            return False
        raise
    finally:
        for c in reversed(ctx):
            c.stop()


# ---------------------------------------------------------------------------
# Identity — /drill-options must resolve the persona the executor resolves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drill_options_resolves_persona_against_simulated_identity():
    """A revert to ``current_user=current_user`` resolves the admin persona and
    this goes red: the catalogue offers the admin-only hierarchy."""
    captured: dict = {}

    async def _capture(_db, *, current_user, model_id, requested_persona_id):
        captured["email"] = current_user.email
        captured["role"] = current_user.role
        captured["roles"] = list(current_user.roles or [])
        return VIEWER_PERSONA

    ctx = _patches()
    for c in ctx:
        c.start()
    try:
        with patch.object(drill_routes, "resolve_execution_persona", new=_capture):
            await drill_routes.drill_options(
                drill_routes.DrillThroughRequest(
                    grouping_levels=[{"column": "year", "value": 2025}],
                ),
                measure_id=MEASURE_ID,
                current_user=_admin_user(),
                x_simulate_principal="viewer@x.com",
                x_simulate_roles="viewer",
                x_simulate_groups=None,
                x_simulate_claims=None,
            )
    finally:
        for c in reversed(ctx):
            c.stop()

    assert captured["email"] == "viewer@x.com", (
        "drill-options resolved the persona against the real admin, so the "
        "option catalogue disagrees with the executor (Bug-8560)"
    )
    assert captured["role"] == "viewer"
    assert captured["roles"] == ["viewer"]


@pytest.mark.asyncio
async def test_drill_options_simulation_never_escalates_persona_entitlement():
    """Simulating a plain viewer must not carry the admin's privileged role
    into persona resolution — simulate-as is a preview, not an escalation."""
    captured: dict = {}

    async def _capture(_db, *, current_user, model_id, requested_persona_id):
        captured["role"] = current_user.role
        captured["roles"] = list(current_user.roles or [])
        return VIEWER_PERSONA

    ctx = _patches()
    for c in ctx:
        c.start()
    try:
        with patch.object(drill_routes, "resolve_execution_persona", new=_capture):
            await drill_routes.drill_options(
                drill_routes.DrillThroughRequest(
                    grouping_levels=[{"column": "year", "value": 2025}],
                ),
                measure_id=MEASURE_ID,
                current_user=_admin_user(),
                x_simulate_principal="viewer@x.com",
                x_simulate_roles="viewer",
                x_simulate_groups=None,
                x_simulate_claims=None,
            )
    finally:
        for c in reversed(ctx):
            c.stop()

    assert "tenant_admin" not in captured["roles"]
    assert captured["role"] != "tenant_admin"


@pytest.mark.asyncio
async def test_drill_options_without_simulation_resolves_the_real_caller():
    """Unchanged behaviour for the normal path, including an embed lock."""
    captured: dict = {}

    async def _capture(_db, *, current_user, model_id, requested_persona_id):
        captured["email"] = current_user.email
        captured["role"] = current_user.role
        return ADMIN_PERSONA

    ctx = _patches()
    for c in ctx:
        c.start()
    try:
        with patch.object(drill_routes, "resolve_execution_persona", new=_capture):
            await drill_routes.drill_options(
                drill_routes.DrillThroughRequest(
                    grouping_levels=[{"column": "year", "value": 2025}],
                ),
                measure_id=MEASURE_ID,
                current_user=_admin_user(),
                x_simulate_principal=None,
                x_simulate_roles=None,
                x_simulate_groups=None,
                x_simulate_claims=None,
            )
    finally:
        for c in reversed(ctx):
            c.stop()

    assert captured["email"] == "admin@x.com"
    assert captured["role"] == "tenant_admin"


# ---------------------------------------------------------------------------
# Agreement — offered == executable, for the SAME identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("simulate", [False, True])
async def test_offered_drills_are_exactly_the_executable_drills(simulate):
    """The catalogue and the executor must agree for the same identity, on both
    the real-caller and the simulate-as path."""
    offered = await _options(simulate=simulate)
    for hierarchy_id in (ADMIN_ONLY_HIER, SHARED_HIER):
        accepted = await _executes(hierarchy_id, simulate=simulate)
        assert accepted == (str(hierarchy_id).lower() in offered), (
            f"catalogue/executor disagreement for hierarchy {hierarchy_id} "
            f"(simulate={simulate}): offered="
            f"{str(hierarchy_id).lower() in offered} accepted={accepted}"
        )


@pytest.mark.asyncio
async def test_simulated_viewer_is_not_offered_the_admin_only_drill():
    """The concrete Bug-8560 symptom: with simulation active the admin-only
    hierarchy was still advertised, then refused on execute."""
    offered = await _options(simulate=True)
    assert str(SHARED_HIER).lower() in offered
    assert str(ADMIN_ONLY_HIER).lower() not in offered, (
        "the simulated viewer was offered a drill only the admin persona "
        "allows; /drill-through then answers 403 (Bug-8560)"
    )


@pytest.mark.asyncio
async def test_real_admin_is_still_offered_its_own_drills():
    """The fix must narrow only the simulated identity, never the real one."""
    offered = await _options(simulate=False)
    assert str(SHARED_HIER).lower() in offered
    assert str(ADMIN_ONLY_HIER).lower() in offered


# ---------------------------------------------------------------------------
# The single narrowing function both surfaces use
# ---------------------------------------------------------------------------


def test_filter_drillable_by_persona_is_id_representation_insensitive():
    """Bug-6832 class: the allow-list reaches this code as a UUID, lower or
    upper hex, or brace-wrapped. One normalisation, used by both surfaces, is
    what keeps a case variant from making them disagree."""
    kept = filter_drillable_by_persona(
        DRILLABLE, {str(SHARED_HIER).upper(), "{" + str(ADMIN_ONLY_HIER) + "}"},
    )
    assert {h.hierarchy_id for h in kept} == {SHARED_HIER, ADMIN_ONLY_HIER}


def test_filter_drillable_by_persona_empty_allow_list_is_unrestricted():
    assert filter_drillable_by_persona(DRILLABLE, None) == DRILLABLE
    assert filter_drillable_by_persona(DRILLABLE, set()) == DRILLABLE


def test_build_drill_sql_narrows_through_the_shared_filter():
    """The executor's step-down candidates and the catalogue must be narrowed by
    the SAME function, so no second inline comparison can drift from it."""
    import inspect

    from src.drill import semantic_builder

    source = inspect.getsource(semantic_builder.build_drill_sql)
    assert "filter_drillable_by_persona" in source, (
        "build_drill_sql narrows the drillable set with its own inline "
        "comparison again — that is how A34 was reintroducible"
    )
