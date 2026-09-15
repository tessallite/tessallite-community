"""Bug-9825 — a catalog name must identify exactly one model.

A model slug is unique only WITHIN a project, and a project slug only within a
tenant. XMLA published and resolved catalogs by bare model slug or display name
and returned the FIRST match, so with two accessible projects that each contain
a ``sales`` model, Excel could bind a workbook to plausible data from the wrong
project and nothing in the response said so. Wrong numbers, silently.

The name is now the combination that actually is unique — tenant, project,
model, plus the persona for a persona view — and an unqualified legacy name is
accepted only when exactly one accessible model matches. Ambiguity is refused,
not resolved by list order.

The pre-existing catalog tests could not have caught the defect: they call
``_rows_catalogs`` with an EMPTY model list, so no catalog name is ever built
from a model at all.
"""
from __future__ import annotations

import uuid

import pytest

from src.dax import mdschema, xmla_server
from src.dax.catalog_naming import (
    CATALOG_NAME_MAX_LENGTH,
    AmbiguousCatalogError,
    build_catalog_name,
)

TENANT = "acme"
MODEL_A = str(uuid.uuid4())
MODEL_B = str(uuid.uuid4())


def _model(model_id, project_slug, slug, display=None):
    return {
        "id": model_id,
        "project_id": str(uuid.uuid4()),
        "slug": slug,
        "project_slug": project_slug,
        "display_name": display or slug.title(),
        "deployed_version_id": "v1",
        "tenant_slug": TENANT,
    }


# Two projects, both with a model called "sales" — the reported collision.
COLLIDING = [
    _model(MODEL_A, "alpha", "sales"),
    _model(MODEL_B, "beta", "sales"),
]


class TestCatalogName:
    def test_name_is_tenant_project_model(self):
        assert build_catalog_name("acme", "alpha", "sales") == "acme__alpha__sales"

    def test_persona_is_part_of_the_name(self):
        """CUBE_NAME is the catalog name, so this is what gives the cube list a
        visible persona viewpoint to pick."""
        assert build_catalog_name("acme", "alpha", "sales", "technical") == (
            "acme__alpha__sales__technical"
        )

    def test_unknown_parts_are_omitted_rather_than_left_blank(self):
        assert build_catalog_name("", "alpha", "sales") == "alpha__sales"
        assert build_catalog_name("", "", "sales") == "sales"

    def test_names_fit_the_advertised_literal(self):
        literal = next(
            row for row in mdschema._rows_literals()
            if row["LiteralName"] == "DBLITERAL_CATALOG_NAME"
        )
        assert int(literal["LiteralMaxLength"]) == CATALOG_NAME_MAX_LENGTH
        assert int(literal["LiteralMaxLength"]) >= len(
            build_catalog_name("acme", "alpha", "sales", "technical")
        )


class TestCatalogPublication:
    def test_colliding_slugs_publish_distinct_catalog_names(self):
        """THE defect: both models are called 'sales'."""
        names = [r["CATALOG_NAME"] for r in mdschema._rows_catalogs("", COLLIDING)]
        assert names == ["acme__alpha__sales", "acme__beta__sales"]

    def test_description_is_the_friendly_label_only(self):
        """The name carries the identification, so the description need not."""
        row = mdschema._rows_catalogs("", [COLLIDING[0]])[0]
        assert row["DESCRIPTION"] == "Sales"

    def test_persona_views_are_published_as_their_own_catalogs(self):
        models = [dict(COLLIDING[0], personas=[
            {"id": str(uuid.uuid4()), "slug": "technical", "name": "Technical"},
        ])]
        rows = mdschema._rows_catalogs("", models)
        assert [r["CATALOG_NAME"] for r in rows] == [
            "acme__alpha__sales", "acme__alpha__sales__technical",
        ]
        assert rows[1]["DESCRIPTION"] == "Sales (Technical)"

    def test_a_saved_workbook_using_the_bare_slug_still_matches(self):
        rows = mdschema._rows_catalogs("sales", [COLLIDING[0]])
        assert len(rows) == 1
        assert rows[0]["CATALOG_NAME"] == "acme__alpha__sales"

    def test_the_qualified_name_matches_as_a_restriction_too(self):
        rows = mdschema._rows_catalogs("acme__alpha__sales", [COLLIDING[0]])
        assert len(rows) == 1


class TestCubesAgreeWithCatalogs:
    """MDSCHEMA_CUBES sets CUBE_NAME to the catalog name, so the two rowsets
    must publish the same names — otherwise a cube is advertised that belongs
    to no catalog."""

    def test_cube_names_match_catalog_names(self):
        catalogs = {r["CATALOG_NAME"] for r in mdschema._rows_catalogs("", COLLIDING)}
        cubes = {r["CATALOG_NAME"] for r in mdschema._rows_cubes("", COLLIDING)}
        assert catalogs == cubes

    def test_cube_name_equals_its_catalog_name(self):
        for row in mdschema._rows_cubes("", COLLIDING):
            assert row["CUBE_NAME"] == row["CATALOG_NAME"]

    def test_cube_list_shows_the_persona_viewpoint(self):
        models = [dict(COLLIDING[0], personas=[
            {"id": str(uuid.uuid4()), "slug": "technical", "name": "Technical"},
        ])]
        names = [r["CUBE_NAME"] for r in mdschema._rows_cubes("", models)]
        assert names == ["acme__alpha__sales", "acme__alpha__sales__technical"]


def _patch_models(monkeypatch, models, personas_by_model=None):
    personas_by_model = personas_by_model or {}

    async def _list(_tenant, _jwt):
        return models

    async def _personas(model_id, _tenant, _jwt, project_id=None):
        return personas_by_model.get(str(model_id), [])

    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", _list)
    monkeypatch.setattr(xmla_server, "get_model_personas", _personas)


class TestCatalogResolution:
    @pytest.mark.asyncio
    async def test_qualified_name_resolves_past_a_slug_collision(self, monkeypatch):
        _patch_models(monkeypatch, COLLIDING)
        mid, _pid, persona, _dvid = await xmla_server._resolve_model_id(
            "acme__beta__sales", TENANT, "jwt",
        )
        assert mid == MODEL_B
        assert persona is None

    @pytest.mark.asyncio
    async def test_an_ambiguous_bare_slug_is_refused_not_guessed(self, monkeypatch):
        """THE defect. This used to return whichever model came first."""
        _patch_models(monkeypatch, COLLIDING)
        with pytest.raises(AmbiguousCatalogError) as exc_info:
            await xmla_server._resolve_model_id("sales", TENANT, "jwt")
        assert len(exc_info.value.matches) == 2

    @pytest.mark.asyncio
    async def test_an_unambiguous_bare_slug_still_resolves(self, monkeypatch):
        _patch_models(monkeypatch, [COLLIDING[0]])
        mid, _pid, _persona, _dvid = await xmla_server._resolve_model_id(
            "sales", TENANT, "jwt",
        )
        assert mid == MODEL_A

    @pytest.mark.asyncio
    async def test_an_ambiguous_persona_suffix_is_refused_too(self, monkeypatch):
        """Both candidates have equal slug length, so the longest-slug ordering
        that the base case relied on could not separate them either."""
        _patch_models(monkeypatch, COLLIDING, {
            MODEL_A: [{"id": str(uuid.uuid4()), "slug": "technical"}],
            MODEL_B: [{"id": str(uuid.uuid4()), "slug": "technical"}],
        })
        with pytest.raises(AmbiguousCatalogError):
            await xmla_server._resolve_model_id("sales_technical", TENANT, "jwt")

    @pytest.mark.asyncio
    async def test_a_qualified_persona_name_resolves_to_that_persona(
        self, monkeypatch,
    ):
        persona = {"id": str(uuid.uuid4()), "slug": "technical", "name": "Technical"}
        _patch_models(monkeypatch, COLLIDING, {MODEL_A: [persona]})
        mid, _pid, resolved, _dvid = await xmla_server._resolve_model_id(
            "acme__alpha__sales__technical", TENANT, "jwt",
        )
        assert mid == MODEL_A
        assert resolved is not None and resolved["slug"] == "technical"

    @pytest.mark.asyncio
    async def test_a_persona_the_model_lacks_does_not_fall_back_to_the_base(
        self, monkeypatch,
    ):
        """Failing open would hand an unrestricted view to a request that
        explicitly asked for a restricted one."""
        _patch_models(monkeypatch, COLLIDING, {MODEL_A: []})
        mid, _pid, persona, _dvid = await xmla_server._resolve_model_id(
            "acme__alpha__sales__nosuch", TENANT, "jwt",
        )
        assert mid is None
        assert persona is None

    @pytest.mark.asyncio
    async def test_a_name_from_another_tenant_does_not_resolve(self, monkeypatch):
        """The tenant is part of the name, so a name built for one tenant must
        not match a same-named model in another."""
        _patch_models(monkeypatch, COLLIDING)
        mid, _pid, _persona, _dvid = await xmla_server._resolve_model_id(
            "other__alpha__sales", TENANT, "jwt",
        )
        assert mid is None

    @pytest.mark.asyncio
    async def test_an_unknown_catalog_still_resolves_to_nothing(self, monkeypatch):
        _patch_models(monkeypatch, COLLIDING)
        mid, _pid, _persona, _dvid = await xmla_server._resolve_model_id(
            "acme__alpha__nosuchmodel", TENANT, "jwt",
        )
        assert mid is None
