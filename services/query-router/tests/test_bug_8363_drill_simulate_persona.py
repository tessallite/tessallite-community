"""Bug-8363 — /measures/{id}/drill-through must resolve the PERSONA against the
simulated identity, not the real admin (Bug-8301 class).

Before this fix the endpoint applied the SIMULATED principal for row security
(``resolve_principal`` -> ``_handle_drill_through(principal=...)``) but resolved
the persona with the REAL ``current_user``. An admin previewing "what does this
restricted user see" therefore got the user's ROW filter combined with the
ADMIN's persona entitlement: persona-CLS, the persona measure allow-list, the
persona hierarchy allow-list and persona default filters were all absent, so the
preview showed drill rows the simulated user could never reach.

These are route-wiring tests: they capture the ``current_user`` the persona
resolver is actually called with, which is exactly what regressed.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest


def _fake_tenant_db_gen(db):
    async def _gen(_tenant_id):
        yield db
    return _gen


def _admin_user():
    from shared.auth.middleware import CurrentUser

    return CurrentUser(
        user_id="admin@x.com", tenant_id="t1", email="admin@x.com",
        role="tenant_admin", roles=["tenant_admin"],
    )


def _drill_body():
    import src.api.drill_routes as drill_routes

    return drill_routes.DrillThroughRequest(
        measure_value=None,
        filters=[],
    )


async def _run_drill(*, simulate: bool, capture: dict):
    import src.api.drill_routes as drill_routes

    db = AsyncMock()

    async def _capture_persona(db_, *, current_user, model_id, requested_persona_id):
        capture["email"] = current_user.email
        capture["role"] = current_user.role
        capture["roles"] = list(current_user.roles or [])
        return None

    scalar_result = AsyncMock()
    scalar_result.scalar_one_or_none = lambda: "model-1"
    db.execute = AsyncMock(return_value=scalar_result)

    with (
        patch.object(drill_routes, "get_tenant_db", new=_fake_tenant_db_gen(db)),
        patch.object(
            drill_routes, "_enforce_measure_model_scope",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            drill_routes, "load_authorized_model",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            drill_routes, "resolve_execution_persona", new=_capture_persona,
        ),
        patch.object(
            drill_routes, "_handle_drill_through",
            new=AsyncMock(return_value="ok"),
        ),
    ):
        return await drill_routes.drill_through(
            _drill_body(),
            measure_id=uuid.uuid4(),
            current_user=_admin_user(),
            x_simulate_principal="viewer@x.com" if simulate else None,
            x_simulate_roles="viewer" if simulate else None,
            x_simulate_groups=None,
            x_simulate_claims=None,
        )


@pytest.mark.asyncio
async def test_drill_through_resolves_persona_against_simulated_identity():
    """With simulate headers set, persona resolution must see the SIMULATED
    viewer. A revert to ``current_user=current_user`` resolves persona as the
    privileged admin and this goes red."""
    capture: dict = {}
    out = await _run_drill(simulate=True, capture=capture)

    assert out == "ok"
    assert capture["email"] == "viewer@x.com", (
        "drill-through persona resolved against the real admin, not the "
        "simulated user — the preview would show rows the user cannot see"
    )
    assert capture["role"] == "viewer"
    assert capture["roles"] == ["viewer"]


@pytest.mark.asyncio
async def test_drill_through_simulation_never_escalates_persona_entitlement():
    """Simulating a plain viewer must NOT carry the admin's privileged role
    into persona resolution — otherwise 'simulate' would be a privilege
    escalation rather than a preview."""
    capture: dict = {}
    await _run_drill(simulate=True, capture=capture)

    assert "tenant_admin" not in capture["roles"]
    assert capture["role"] != "tenant_admin"


@pytest.mark.asyncio
async def test_drill_through_without_simulation_resolves_persona_as_real_caller():
    """Unchanged behaviour for the normal (non-simulated) path: the real
    caller's identity drives persona resolution, including its embed lock."""
    capture: dict = {}
    await _run_drill(simulate=False, capture=capture)

    assert capture["email"] == "admin@x.com"
    assert capture["role"] == "tenant_admin"
