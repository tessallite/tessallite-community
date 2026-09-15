"""Bug-9895 -- the hierarchy preview is a derivation over the PERSONA MODEL QUERY.

Persona-layering rule 4, audit row A37. Before the fix ``preview_hierarchy``
built ``SELECT DISTINCT <key> FROM <physical table> AS t WHERE ... AND
(<compiled rls>) LIMIT n`` and ran it through the query-router
``/introspect/batch`` route, which applies NO persona, NO column-level security
and NO row-level security. Every query the preview issues is now
``SELECT DISTINCT <level dimensions> FROM <model>`` posted to ``/execute`` with
the caller's own bearer, so the one authority that gates every other query gates
this one too.

Each test here fails against the pre-fix code:

* ``test_preview_issues_no_physical_table_scan`` -- pre-fix the endpoint called
  ``_introspect_batch_via_router`` with SQL naming the physical table
  ``dim_region``; the helper is asserted never to be called and the routed SQL
  is asserted to name the MODEL.
* ``test_preview_does_not_splice_row_security_locally`` -- pre-fix
  ``compile_row_security`` was called in this endpoint and its predicate spliced
  into the inner WHERE. The symbol no longer exists on the module.
* ``test_viewer_path_reaches_the_router`` -- pre-fix a viewer's preview
  returned a ``preview_query_failed`` warning carrying the 403 from the
  modeller-gated ``/introspect/batch`` (Bug-9900). It now routes and returns
  members.
* ``test_drill_is_bounded_by_the_whole_ancestor_path`` -- the Bug-9871 contract,
  re-expressed on the routed shape.
* ``test_level_count_probe_is_a_projection_not_an_aggregate`` --
  ``COUNT(DISTINCT <dim>)`` binds the dimension as a MEASURE and the persona
  gate denies it (``reason=measure_not_included``), which lost the level count
  for exactly the restricted personas this endpoint now serves.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.api import hierarchies as hier_mod

from ._hierarchy_preview_harness import (
    LEVEL_DIMS,
    PREFIX,  # noqa: F401  (kept so the route prefix has one definition)
    Routed,
    entered,
    get_preview,
    make_levels,
    make_persona,
    preview_patches,
)
from .conftest import TEST_MODEL_ID, client

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_preview_issues_no_physical_table_scan(client):
    """Bug-9895: no statement the preview issues names a physical table, and the
    raw-SQL ``/introspect/batch`` helper is never reached."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    introspect = AsyncMock(return_value={})
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    ctx.append(patch("src.api.hierarchies._introspect_batch_via_router", introspect))
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=0")

    assert resp.status_code == 200
    introspect.assert_not_awaited()
    assert routed.sqls, "the preview issued no routed query"
    for sql in routed.sqls:
        assert '"modely"' in sql, sql
        assert "dim_region" not in sql, sql
        # The spliced-scan shape used a table alias; the model query has none.
        assert " AS t " not in sql, sql
    assert [m["key_value"] for m in resp.json()["members"]] == ["GB"]


@pytest.mark.asyncio
async def test_preview_does_not_splice_row_security_locally(client):
    """Bug-9895: the endpoint no longer compiles or splices a row-security
    predicate of its own -- the router injects it into the model query."""
    assert not hasattr(hier_mod, "compile_row_security")
    assert not hasattr(hier_mod, "_build_sample_sql")
    assert not hasattr(hier_mod, "_build_estimate_sql")
    assert not hasattr(hier_mod, "_build_cross_table_sample_sql")
    assert not hasattr(hier_mod, "_build_ancestor_path_sample_sql")

    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=0")

    assert resp.status_code == 200
    for sql in routed.sqls:
        assert "region_code" not in sql, sql


@pytest.mark.asyncio
async def test_routed_queries_carry_the_callers_bearer_and_persona(client):
    """The derivation re-enters the persona path AS THE CALLER: the caller's own
    bearer and the resolved persona, never an internal service scope."""
    hierarchy_id = uuid.uuid4()
    persona = make_persona()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    ctx = preview_patches(
        routed=routed, persona=persona, levels=make_levels(), hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=0")

    assert resp.status_code == 200
    assert routed.calls
    for call in routed.calls:
        assert call["bearer"] == "test-token"
        assert call["persona_id"] == str(persona.id)
        assert call["model_id"] == str(TEST_MODEL_ID)


@pytest.mark.asyncio
async def test_viewer_path_reaches_the_router(client):
    """Bug-9900 narrowed ``/introspect/batch`` to modeller, which returned the
    viewer an empty member list carrying ``preview_query_failed``. The routed
    path serves the viewer members again."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(
        rows_for=lambda sql: [{"country_code": "GB"}, {"country_code": "US"}],
    )
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=0")

    body = resp.json()
    assert resp.status_code == 200
    assert [w["type"] for w in body["warnings"]] == []
    assert [m["key_value"] for m in body["members"]] == ["GB", "US"]


@pytest.mark.asyncio
async def test_drill_is_bounded_by_the_whole_ancestor_path(client):
    """Bug-9871 on the routed shape: children of [GB].[London] filter on BOTH
    the parent level and every ancestor level above it, so a level whose keys
    repeat under different ancestors returns only the requested branch."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"channel_name": "ATM"}])
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(
            client, hierarchy_id,
            "sample_size=100&expand_level=2&parent_key=London&ancestor_keys=GB",
        )

    assert resp.status_code == 200
    sql = routed.sample_sql
    assert 'CAST("country_code" AS VARCHAR) = \'GB\'' in sql, sql
    assert 'CAST("city_name" AS VARCHAR) = \'London\'' in sql, sql
    assert 'SELECT DISTINCT "channel_name"' in sql, sql


@pytest.mark.asyncio
async def test_ancestor_key_literal_is_escaped_by_sqlglot(client):
    """A quote in a member key must be escaped by the literal builder, never
    spliced raw (F-016-19), on the routed shape too."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [])
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(
            client, hierarchy_id,
            "sample_size=100&expand_level=2&parent_key=London&ancestor_keys=G%27B",
        )

    assert resp.status_code == 200
    assert "'G''B'" in routed.sample_sql


@pytest.mark.asyncio
async def test_ancestor_keys_length_mismatch_still_warns(client):
    """The Bug-9871 mismatch warning survives the re-expression."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [])
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(
            client, hierarchy_id,
            "sample_size=100&expand_level=2&parent_key=London"
            "&ancestor_keys=GB&ancestor_keys=extra",
        )

    assert resp.status_code == 200
    assert "ancestor_keys_mismatch" in {w["type"] for w in resp.json()["warnings"]}
    sql = routed.sample_sql
    assert 'CAST("city_name" AS VARCHAR) = \'London\'' in sql
    assert "country_code" not in sql


@pytest.mark.asyncio
async def test_key_path_enumeration_selects_every_ancestor_dimension(client):
    """Bug-3617 Phase 0.5b: a parent-less enumeration below the root returns the
    whole ancestor key tuple. The model query resolves the joins, so -- unlike
    the physical-scan builder -- the levels need not share one table."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(
        rows_for=lambda sql: [{"country_code": "GB", "city_name": "London"}],
    )
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(
            client, hierarchy_id,
            "sample_size=100&expand_level=1&include_key_path=true",
        )

    assert resp.status_code == 200
    assert 'SELECT DISTINCT "country_code", "city_name"' in routed.sample_sql
    member = resp.json()["members"][0]
    assert member["key_path"] == ["GB", "London"]
    assert member["key_value"] == "London"


@pytest.mark.asyncio
async def test_level_count_probe_is_a_projection_not_an_aggregate(client):
    """Bug-9895: level counts must not be asked for as ``COUNT(DISTINCT <dim>)``.

    That binds the level's dimension as a MEASURE, so the persona gate denies it
    (``[PERSONA_DENY] reason=measure_not_included``) for every persona whose
    included measures do not list it -- losing the count for exactly the
    restricted personas this endpoint now serves.
    """
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}] * 4)
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=0")

    assert resp.status_code == 200
    for sql in routed.sqls:
        assert "COUNT(" not in sql.upper(), sql
    # One probe per level, plus the sample.
    assert len(routed.sqls) == len(LEVEL_DIMS) + 1
    assert [ls["estimated_members"] for ls in resp.json()["levels_summary"]] == [4, 4, 4]


@pytest.mark.asyncio
async def test_level_counts_can_be_suppressed(client):
    """``include_level_counts=false`` (what the XMLA member path sends) spends
    no routed query on counts nothing reads."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(
            client, hierarchy_id,
            "sample_size=100&expand_level=0&include_level_counts=false",
        )

    assert resp.status_code == 200
    assert len(routed.sqls) == 1
    # Falls back to the stored table row estimate rather than reporting nothing.
    assert [ls["estimated_members"] for ls in resp.json()["levels_summary"]] == [100, 100, 100]


@pytest.mark.asyncio
async def test_router_row_security_422_is_not_swallowed(client):
    """F-007-16 / Bug-9021: a misconfigured row-security rule still fails closed
    with the typed 422 now that the ROUTER compiles the predicate."""
    from fastapi import HTTPException

    hierarchy_id = uuid.uuid4()

    async def _raise(model_id, sql, bearer, *, persona_id=None, timeout_s=60.0):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "A row-level security rule on this model is misconfigured.",
                "error_type": "row_security_misconfigured",
            },
        )

    ctx = preview_patches(
        routed=_raise, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
    )
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=0")

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_type"] == "row_security_misconfigured"


@pytest.mark.asyncio
async def test_level_without_a_dimension_warns_instead_of_scanning(client):
    """A level whose key attribute is not published as a dimension cannot be
    expressed over the model query. It is reported, never served by falling back
    to a physical scan."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [])
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id,
        level_dims=["country_code", None, "channel_name"],
    )
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=1")

    body = resp.json()
    assert resp.status_code == 200
    assert body["members"] == []
    assert "level_not_published_as_dimension" in {w["type"] for w in body["warnings"]}
    assert routed.sqls == []


@pytest.mark.asyncio
async def test_caption_dimension_is_selected_alongside_the_key(client):
    """Bug-3617 Phase 0.5a on the routed shape: a level with a DISPLAY attribute
    carries its caption alongside the key, so MEMBER_CAPTION can differ from
    MEMBER_KEY on the XMLA wire. The caption column must not join the ORDER BY,
    which addresses the key columns by ordinal."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(
        rows_for=lambda sql: [{"country_code": "GB", "country_label": "United Kingdom"}],
    )
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id, caption_dim="country_label",
    )
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=0")

    assert resp.status_code == 200
    sql = routed.sample_sql
    assert 'SELECT DISTINCT "country_code", "country_label"' in sql, sql
    assert sql.rstrip().endswith("ORDER BY 1 LIMIT 100"), sql
    member = resp.json()["members"][0]
    assert member["key_value"] == "GB"
    assert member["caption"] == "United Kingdom"


@pytest.mark.asyncio
async def test_caption_falls_back_to_the_key_when_no_display_attribute(client):
    """No display attribute: the sample stays the single-column shape and the
    caption equals the key (the legacy, back-compatible behaviour)."""
    hierarchy_id = uuid.uuid4()
    routed = Routed(rows_for=lambda sql: [{"country_code": "GB"}])
    ctx = preview_patches(
        routed=routed, persona=make_persona(), levels=make_levels(),
        hierarchy_id=hierarchy_id, caption_dim=None,
    )
    with entered(ctx):
        resp = await get_preview(client, hierarchy_id, "sample_size=100&expand_level=0")

    assert resp.status_code == 200
    assert 'SELECT DISTINCT "country_code" FROM' in routed.sample_sql
    member = resp.json()["members"][0]
    assert member["key_value"] == "GB"
    assert member["caption"] == "GB"
