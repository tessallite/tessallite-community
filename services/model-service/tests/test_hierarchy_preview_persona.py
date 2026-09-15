"""Bug-5424: the hierarchy preview is scoped to the caller's persona.

1. ``persona_id`` is accepted by the preview endpoint.
2. A persona that excludes the hierarchy gets a 404.
3. Levels whose key attribute the persona excludes are filtered out.
4. Calling without ``persona_id`` still works.

Bug-9895 changed WHERE the persona's row- and column-level decisions are made,
not whether they are made. The endpoint no longer compiles a row-security
predicate and splices it into a physical-table scan; every statement is routed
through the query-router ``/execute`` path with the caller's bearer and the
resolved persona, so the router applies the allow-list, the persona default
filters, CLS and RLS to the model query itself. The two tests that asserted the
splice (``..._injects_rls_predicate_into_sql``,
``..._skips_rls_when_persona_bypasses``) are superseded by that decision and are
replaced here by the contract that survives it: the persona reaches the router,
and nothing row-security-shaped is built locally. ``bypass_row_security`` is the
router's decision now and is covered by the query-router suite.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.api import hierarchies as hier_mod

from ._hierarchy_preview_harness import (
    PREFIX,
    Routed,
    entered,
    get_preview,
    make_levels,
    make_persona,
    preview_patches,
)
from .conftest import TEST_MODEL_ID, async_gen_from, client, make_mock_db, make_model

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_preview_accepts_persona_id_parameter(client):
    """GET .../preview?persona_id=... is accepted and returns 200."""
    hierarchy_id = uuid.uuid4()
    persona = make_persona()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    patches = preview_patches(
        routed=routed, persona=persona, levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(patches):
        resp = await get_preview(
            client, hierarchy_id, f"sample_size=10&persona_id={persona.id}",
        )

    assert resp.status_code == 200
    assert resp.json()["hierarchy_id"] == str(hierarchy_id)


@pytest.mark.asyncio
async def test_preview_forwards_the_persona_to_the_router(client):
    """Bug-9895: the persona the endpoint resolved for its METADATA decisions is
    the persona the router applies to the DATA. If these two diverged, a level
    the preview shows could be filled with another persona's rows."""
    hierarchy_id = uuid.uuid4()
    persona = make_persona()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    patches = preview_patches(
        routed=routed, persona=persona, levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(patches):
        resp = await get_preview(
            client, hierarchy_id, f"sample_size=10&persona_id={persona.id}",
        )

    assert resp.status_code == 200
    assert routed.calls
    assert {c["persona_id"] for c in routed.calls} == {str(persona.id)}


@pytest.mark.asyncio
async def test_preview_builds_no_row_security_predicate_of_its_own(client):
    """Bug-9895 / audit row A37: the endpoint must not compile or splice a
    row-security predicate. Row security is injected into the model query by the
    router, which is the only place that can prove it covers every scan."""
    assert not hasattr(hier_mod, "compile_row_security")
    assert not hasattr(hier_mod, "RowSecurityCompileError")

    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    patches = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(patches):
        resp = await get_preview(client, hierarchy_id, "sample_size=10")

    assert resp.status_code == 200
    for sql in routed.sqls:
        assert " AND (" not in sql, sql


@pytest.mark.asyncio
async def test_f007_16_preview_row_security_error_is_422_not_500(client):
    """F-007-16 / Bug-9021: a misconfigured row-security rule fails closed with
    the typed 422. The router raises it now (Bug-9895); the endpoint must pass
    it through rather than degrade it to an empty 200."""
    from fastapi import HTTPException

    hierarchy_id = uuid.uuid4()

    async def _raise(model_id, sql, bearer, *, persona_id=None, timeout_s=60.0):
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "A row-level security rule on this model is misconfigured "
                    "and could not be compiled."
                ),
                "error_type": "row_security_misconfigured",
            },
        )

    patches = preview_patches(
        routed=_raise, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(patches):
        resp = await get_preview(client, hierarchy_id, "sample_size=10")

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_type"] == "row_security_misconfigured"
    assert "dimension_in" not in str(detail)


@pytest.mark.asyncio
async def test_preview_returns_404_when_persona_excludes_hierarchy(client):
    """If the persona's included_hierarchy_ids does not include the requested
    hierarchy, the preview endpoint must return 404."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())
    hierarchy_id = uuid.uuid4()
    other_hierarchy_id = uuid.uuid4()
    persona = make_persona(included_hierarchy_ids=[str(other_hierarchy_id)])

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(
                return_value=types.SimpleNamespace(
                    id=hierarchy_id, name="Geo", model_id=TEST_MODEL_ID,
                ),
            ),
        ),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview"
            f"?sample_size=10&persona_id={persona.id}",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_filters_levels_by_persona_dimension_exclusion(client):
    """A level whose key attribute the persona excludes (column-level security)
    is removed from the hierarchy before anything is routed. Two levels minus
    one leaves a hierarchy too shallow to preview, so the H2 validation fires."""
    hierarchy_id = uuid.uuid4()
    levels = make_levels(2, names=["Region", "Excluded"])
    routed = Routed()
    patches = preview_patches(
        routed=routed, persona=make_persona(), levels=levels,
        hierarchy_id=hierarchy_id,
        level_dims=["region_code", "excluded_code"],
        excluded_attrs={levels[1].key_attribute_id},
    )
    with entered(patches):
        resp = await get_preview(client, hierarchy_id, "sample_size=10")

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "H2"
    assert routed.sqls == [], "an excluded level must never reach the source"


@pytest.mark.asyncio
async def test_preview_without_persona_id_works_as_before(client):
    """Calling preview without persona_id still works; the router resolves the
    caller's own persona."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    patches = preview_patches(
        routed=routed, persona=None, levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(patches):
        resp = await get_preview(client, hierarchy_id, "sample_size=10")

    assert resp.status_code == 200
    assert resp.json()["hierarchy_id"] == str(hierarchy_id)
    assert {c["persona_id"] for c in routed.calls} == {None}
