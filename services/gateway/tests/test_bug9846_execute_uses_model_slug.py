"""Bug-9846 — Execute put the CATALOG name in the SQL FROM clause.

A catalog name is a published IDENTITY. Since Bug-9825 it is the qualifying
combination that is actually unique — ``tenant__project__model``, optionally
persona-suffixed — because a model slug is unique only within a project. The
model slug is a different thing: the TABLE a source query selects from.

``_handle_execute`` passed the catalog straight through as ``model_slug`` to
every statement builder, and ``model_slug`` becomes
``from_table = _q(model_slug or "model_table")``. So a client that connected
using the qualified name the gateway itself advertised produced

    FROM "acme-demo__project1__modely"

and every query failed with::

    Unknown table 'acme-demo__project1__modely' in FROM clause.
    Use the model name 'modely' instead.

Observed live on ALEX 2026-09-03 22:57:03 as "the query did not run" the moment
Excel was closed and reopened. A live session reuses its saved connection string
(bare slug — works); a reopen re-picks from the catalog browser, which can only
offer the qualified name. The Excel harness never caught it because it builds
its connection with the bare slug and never browses catalogs.

Test escape: every Execute test connected with a bare-slug catalog, so
``model_slug == catalog`` was accidentally correct in all of them.
Guard: this file — a qualified catalog must still produce the model's own
FROM table. Tier: T2 (cross-service contract, gateway -> query-router).
"""

from __future__ import annotations

from typing import Any

from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

import pytest

from src.dax import xmla_server

TENANT = "acme-demo"
MODEL_ID = "58118363-b947-4c40-8edb-baf9f486c230"
MODEL_SLUG = "modely"
QUALIFIED_CATALOG = "acme-demo__project1__modely"

_STATEMENT = (
    "SELECT NON EMPTY Hierarchize(AddCalculatedMembers("
    "{[account_type].[account_type].[(All)].Members})) "
    "DIMENSION PROPERTIES PARENT_UNIQUE_NAME, HIERARCHY_UNIQUE_NAME "
    "ON COLUMNS FROM [{catalog}] WHERE ([Measures].[avg_base_amount]) "
    "CELL PROPERTIES VALUE"
)


def _execute_method(catalog: str) -> Element:
    statement = _STATEMENT.replace("{catalog}", catalog)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{statement}</Statement></Command>
      <Properties><PropertyList>
        <Catalog>{catalog}</Catalog>
        <AxisFormat>TupleFormat</AxisFormat>
        <SspropInitAppName>Microsoft Office Excel</SspropInitAppName>
      </PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    method = xmla_server._find_method(ET.fromstring(xml))
    assert method is not None
    return method


def _patch(monkeypatch: pytest.MonkeyPatch, captured: list[str]) -> None:
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return MODEL_ID, "project-1", None, None

    async def fake_list_all_models_for_tenant(tenant_slug: str, jwt_token: str):
        # What the tenant listing really carries: the model's own slug, plus the
        # project slug the qualified catalog name is built from.
        return [{
            "id": MODEL_ID,
            "slug": MODEL_SLUG,
            "display_name": "modely",
            "project_id": "project-1",
            "project_slug": "project1",
            "tenant_slug": TENANT,
        }]

    async def fake_get_model_measures(
        model_id: str, tenant_slug: str, jwt_token: str,
        project_id: str = "", **_: Any,
    ):
        return [{"name": "avg_base_amount", "default_agg": "avg"}]

    async def fake_get_model_dimensions(
        model_id: str, tenant_slug: str, jwt_token: str,
        project_id: str = "", **_: Any,
    ):
        return [{"name": "account_type", "display_name": "account_type"}]

    async def fake_execute_query(**kwargs: Any):
        captured.append(str(kwargs.get("sql", "")))
        return {"columns": ["account_type", "avg_base_amount"], "rows": []}

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(
        xmla_server, "list_all_models_for_tenant", fake_list_all_models_for_tenant,
    )
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", fake_execute_query)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catalog",
    [QUALIFIED_CATALOG, MODEL_SLUG],
    ids=["qualified-catalog", "bare-slug-catalog"],
)
async def test_bug9846_from_clause_names_the_model_not_the_catalog(
    monkeypatch: pytest.MonkeyPatch, catalog: str,
) -> None:
    """Whatever spelling the client connects with, the FROM table is the model.

    Both spellings are advertised to clients — the catalog list publishes the
    qualified name, and a saved workbook still carries the bare slug — so both
    must produce the same source query.
    """
    captured: list[str] = []
    _patch(monkeypatch, captured)

    response = await xmla_server._handle_execute(
        _execute_method(catalog),
        tenant_slug=TENANT, jwt_token="token", session_id="bug-9846",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body, body[:600]

    assert captured, "Execute generated no SQL"
    for sql in captured:
        assert QUALIFIED_CATALOG not in sql, (
            "Bug-9846: the catalog identity reached the SQL FROM clause; "
            f"query-router rejects it as an unknown table.\nSQL: {sql}"
        )
        assert f'"{MODEL_SLUG}"' in sql or f" {MODEL_SLUG}" in sql, (
            f"the model slug {MODEL_SLUG!r} is not the FROM table.\nSQL: {sql}"
        )


@pytest.mark.asyncio
async def test_bug9846_slug_lookup_failure_falls_back_to_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed tenant listing degrades to the previous behaviour, never to ``""``.

    An empty slug would silently emit ``FROM "model_table"`` — a wrong table
    rather than a clear failure — so the fallback is the caller's own catalog.
    """
    async def _boom(tenant_slug: str, jwt_token: str):
        raise RuntimeError("model-service unavailable")

    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", _boom)
    slug = await xmla_server._resolve_model_slug(
        MODEL_SLUG, MODEL_ID, TENANT, "token",
    )
    assert slug == MODEL_SLUG


@pytest.mark.asyncio
async def test_bug9846_resolver_prefers_the_model_slug_over_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper returns the model's slug, not the qualified name it was given."""
    async def fake_list(tenant_slug: str, jwt_token: str):
        return [{"id": MODEL_ID, "slug": MODEL_SLUG, "project_slug": "project1"}]

    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", fake_list)
    slug = await xmla_server._resolve_model_slug(
        QUALIFIED_CATALOG, MODEL_ID, TENANT, "token",
    )
    assert slug == MODEL_SLUG


@pytest.mark.asyncio
async def test_review_f5_qualified_catalog_never_substitutes_for_the_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deep-review F5 (2026-09-04): a qualified catalog is an identity, not a
    table. When its slug cannot be resolved the resolver raises rather than
    handing ``acme-demo__project1__modely`` to the SQL builders."""
    async def _boom(tenant_slug: str, jwt_token: str):
        raise RuntimeError("model-service unavailable")

    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", _boom)
    with pytest.raises(xmla_server.ModelSlugUnresolvedError):
        await xmla_server._resolve_model_slug(
            QUALIFIED_CATALOG, MODEL_ID, TENANT, "token",
        )
    with pytest.raises(xmla_server.ModelSlugUnresolvedError):
        await xmla_server._resolve_model_slug(
            MODEL_ID, MODEL_ID, TENANT, "token",  # raw UUID catalog
        )


@pytest.mark.asyncio
async def test_review_f5_execute_faults_instead_of_querying_a_catalog_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production path: an unresolved qualified catalog is a server fault
    with no identifier in it, and no SQL is executed."""
    captured: list[str] = []
    _patch(monkeypatch, captured)

    async def _boom(tenant_slug: str, jwt_token: str):
        raise RuntimeError("model-service unavailable")

    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", _boom)
    response = await xmla_server._handle_execute(
        _execute_method(QUALIFIED_CATALOG),
        tenant_slug=TENANT, jwt_token="token", session_id="review-f5",
    )
    body = response.body.decode("utf-8")
    assert "Fault" in body
    assert QUALIFIED_CATALOG not in body
    assert MODEL_SLUG not in body
    assert captured == []


@pytest.mark.asyncio
async def test_review_b4_slug_remembered_at_resolution_serves_display_name_and_persona_catalogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deep-review B4: a display-name or legacy persona catalog resolves the
    model; the physical slug is remembered THERE, so a later listing failure
    can never let the catalog spelling stand in for the table name."""
    listing = [{
        "id": MODEL_ID, "slug": MODEL_SLUG, "display_name": "Friendly Model Name",
        "project_id": "project-1", "project_slug": "project1", "tenant_slug": TENANT,
    }]

    async def fake_list(tenant_slug: str, jwt_token: str):
        return listing

    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", fake_list)
    xmla_server._SLUG_BY_MODEL_ID.clear()
    mid, _pid, _persona, _dvid = await xmla_server._resolve_model_id(
        "Friendly Model Name", TENANT, "token",
    )
    assert mid == MODEL_ID

    async def _boom(tenant_slug: str, jwt_token: str):
        raise RuntimeError("model-service unavailable")

    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", _boom)
    slug = await xmla_server._resolve_model_slug("Friendly Model Name", MODEL_ID, TENANT, "token")
    assert slug == MODEL_SLUG  # never "Friendly Model Name"
