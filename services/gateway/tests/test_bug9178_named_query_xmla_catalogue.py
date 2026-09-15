"""Bug-9178: Named Query tables in the XMLA/DAX tenant catalogue (gateway).

Named Queries are served over SQL/JDBC as ``@name`` relations, but the XMLA
gateway advertised them nowhere — Excel / Power BI table enumeration
(DBSCHEMA_TABLES / DBSCHEMA_COLUMNS) saw only the cube and dimension tables,
so a governed queryable object was invisible to the BI clients the semantic
layer exists for.

These tests pin the catalogue seam that makes ``@name`` discoverable:

  * each DEPLOYED Named Query is advertised as an ``@name`` table row in
    DBSCHEMA_TABLES, and its snapshot-derived ``output_columns`` as
    DBSCHEMA_COLUMNS rows (via ``build_named_query_relation_columns`` — the
    SAME builder the JDBC catalogue registration uses, so the two channels
    advertise identical column metadata);
  * definitions come from the deployed snapshot only (invariant 7) — an
    undeployed model advertises no Named Query tables;
  * a 409 DEPLOYED_SNAPSHOT_INVALID faults the discovery instead of rendering
    a deceptive empty table list (Bug-8384 parity: discovery and Execute
    must agree);
  * a persona-variant catalogue advertises a Named Query if and only if its
    deployed definition BINDS over that persona's model query (Bug-9186,
    rule 4) — the verdict comes from the query-router, the same authority
    its executor applies, and it fails CLOSED when it cannot be obtained.
    Persona allow-lists / RLS / CLS on the Named Query's DATA are enforced
    by the query-router at query time, unchanged.

Test escape: no test asserted the XMLA table rowsets for a reference surface
that names no model table, so the JDBC-only registration never surfaced the
channel gap. Guard: this module. Tier: T1 (producer/consumer catalogue
contract, area gateway/xmla-catalogue/named-query).
"""
from __future__ import annotations

import httpx
import pytest
from defusedxml import ElementTree as ET

from src.dax import mdschema
from src.dax import xmla_server as xs
from src.router_client import (
    get_deployed_named_queries,
    persona_visible_named_queries as _REAL_PERSONA_VISIBLE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NQ_DEPLOYED = {
    "id": "nq-1",
    "name": "top_cities",
    "description": "Top 10 cities by revenue",
    "display_folder": "Ops",
    "output_columns": [
        {"name": "city_name", "type": "string"},
        {"name": "revenue", "type": "number"},
        {"name": "is_active", "type": "boolean"},
    ],
    "shape": "aggregated",
}


def _discover_xml(request_type: str, catalog: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>{request_type}</RequestType>
      <Restrictions><RestrictionList/></Restrictions>
      <Properties><PropertyList><Catalog>{catalog}</Catalog></PropertyList></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://model-service/versions/v-1")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.fixture
def _patched(monkeypatch):
    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "proj-1", None, "v-1"

    async def fake_measures(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "m1", "name": "Revenue", "default_agg": "sum"}]

    async def fake_dimensions(model_id, tenant_slug, jwt_token, **kw):
        return [{"id": "d1", "name": "Region", "source": "column"}]

    async def fake_hierarchies(model_id, tenant_slug, jwt_token, **kw):
        return []

    async def fake_version_snapshot(model_id, version_id, tenant_slug,
                                    jwt_token, **kw):
        return {}  # no deployed-description overlay

    async def fake_list_all(tenant_slug, jwt_token):
        return [{"id": "model-1", "project_id": "proj-1", "trust_meta": {}}]

    async def fake_deployed_nqs(model_id, deployed_version_id, tenant_slug,
                                jwt_token, **kw):
        return [_NQ_DEPLOYED]

    monkeypatch.setattr(xs, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xs, "get_model_measures", fake_measures)
    monkeypatch.setattr(xs, "get_model_dimensions", fake_dimensions)
    monkeypatch.setattr(xs, "get_model_hierarchies", fake_hierarchies)
    monkeypatch.setattr(xs, "get_model_version_snapshot", fake_version_snapshot)
    monkeypatch.setattr(xs, "list_all_models_for_tenant", fake_list_all)
    monkeypatch.setattr(xs, "get_deployed_named_queries", fake_deployed_nqs)

    # Bug-9186: every catalogue build asks the query-router which Named
    # Queries bind here, including the base surface. Default the verdict to
    # "the fixture's Named Query binds"; the persona cells override it.
    import src.router_client as rc

    async def fake_visible(model_id, jwt_token, persona_id):
        return {"top_cities"}

    monkeypatch.setattr(rc, "persona_visible_named_queries", fake_visible)


async def _discover(request_type: str, catalog: str = "model_1"):
    root = ET.fromstring(_discover_xml(request_type, catalog))
    method_el = xs._find_method(root)
    return await xs._handle_discover(
        method_el, tenant_slug="acme", jwt_token="tok",
    )


# ---------------------------------------------------------------------------
# Rowset builders
# ---------------------------------------------------------------------------

def test_rows_tables_advertises_deployed_named_queries() -> None:
    rows = mdschema._rows_tables(
        "sales", [], [{"name": "Region"}], None, [_NQ_DEPLOYED],
    )
    names = [r["TABLE_NAME"] for r in rows]
    assert names == ["sales", "Region", "@top_cities"]
    nq_row = rows[-1]
    assert nq_row["TABLE_CATALOG"] == "sales"
    assert nq_row["TABLE_TYPE"] == "TABLE"
    assert "Top 10 cities by revenue" in nq_row["DESCRIPTION"]


def test_rows_tables_skips_junk_entries_and_empty_names() -> None:
    rows = mdschema._rows_tables(
        "sales", [], [], None,
        [None, "junk", {"name": ""}, {"name": "  "}, _NQ_DEPLOYED],
    )
    names = [r["TABLE_NAME"] for r in rows]
    assert names == ["sales", "@top_cities"]


def test_rows_tables_uses_fallback_description_like_jdbc() -> None:
    nq = dict(_NQ_DEPLOYED, description=None)
    rows = mdschema._rows_tables("sales", [], [], None, [nq])
    assert "Named Query @top_cities (deployed definition)" in rows[-1]["DESCRIPTION"]


def test_rows_columns_advertises_named_query_output_columns() -> None:
    rows = mdschema._rows_columns(
        "sales", [], [{"name": "Region"}], None, [_NQ_DEPLOYED],
    )
    nq_rows = [r for r in rows if r["TABLE_NAME"] == "@top_cities"]
    assert [(r["COLUMN_NAME"], r["ORDINAL_POSITION"]) for r in nq_rows] == [
        ("city_name", "1"), ("revenue", "2"), ("is_active", "3"),
    ]
    by_name = {r["COLUMN_NAME"]: r for r in nq_rows}
    assert by_name["city_name"]["DATA_TYPE"] == "130"   # DBTYPE_WSTR
    assert by_name["revenue"]["DATA_TYPE"] == "5"       # DBTYPE_R8
    assert by_name["revenue"]["NUMERIC_PRECISION"] == "19"
    assert by_name["is_active"]["DATA_TYPE"] == "11"    # DBTYPE_BOOL
    assert by_name["city_name"]["IS_NULLABLE"] == "true"


def test_rows_columns_maps_date_and_timestamp_types() -> None:
    nq = {
        "name": "dates",
        "output_columns": [
            {"name": "d", "type": "date"},
            {"name": "ts", "type": "timestamp"},
        ],
    }
    rows = mdschema._rows_columns("sales", [], [], None, [nq])
    by_name = {r["COLUMN_NAME"]: r for r in rows}
    assert by_name["d"]["DATA_TYPE"] == "7"       # DBTYPE_DATE
    assert by_name["ts"]["DATA_TYPE"] == "135"    # DBTYPE_DBTIMESTAMP


def test_rows_columns_star_named_query_advertises_star_column() -> None:
    # Bug-9180 (adjacent): the deployed output_columns of a star definition
    # carry a single "*" entry, so the catalogue advertises exactly what the
    # snapshot holds — same behaviour as the JDBC channel. Column-list parity
    # with the served result is tracked separately (Bug-9180).
    nq = {"name": "slice", "output_columns": [{"name": "*", "type": "string"}]}
    rows = mdschema._rows_columns("sales", [], [], None, [nq])
    nq_rows = [r for r in rows if r["TABLE_NAME"] == "@slice"]
    assert [(r["COLUMN_NAME"], r["DATA_TYPE"]) for r in nq_rows] == [("*", "130")]


def test_no_named_queries_keeps_sibling_rowsets_unchanged() -> None:
    tables = mdschema._rows_tables("sales", [], [{"name": "Region"}])
    assert [r["TABLE_NAME"] for r in tables] == ["sales", "Region"]
    cols = mdschema._rows_columns("sales", [], [{"name": "Region"}])
    assert [r["TABLE_NAME"] for r in cols] == ["Region"]


def test_discover_response_emits_named_query_rows_in_xml() -> None:
    xml = mdschema.build_discover_response(
        request_type="DBSCHEMA_TABLES",
        catalog_name="sales",
        model_id="model-1",
        measures=[],
        dimensions=[{"name": "Region"}],
        named_queries=[_NQ_DEPLOYED],
    )
    assert "<TABLE_NAME>@top_cities</TABLE_NAME>" in xml
    assert "<TABLE_NAME>Region</TABLE_NAME>" in xml

    xml = mdschema.build_discover_response(
        request_type="DBSCHEMA_COLUMNS",
        catalog_name="sales",
        model_id="model-1",
        measures=[],
        dimensions=[],
        named_queries=[_NQ_DEPLOYED],
    )
    assert "<TABLE_NAME>@top_cities</TABLE_NAME>" in xml
    assert "<COLUMN_NAME>city_name</COLUMN_NAME>" in xml
    assert "<COLUMN_NAME>revenue</COLUMN_NAME>" in xml


# ---------------------------------------------------------------------------
# Discover handler (mocked model-service)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_db_schema_tables_includes_named_queries(
    monkeypatch, _patched,
):
    resp = await _discover("DBSCHEMA_TABLES", catalog="sales")
    body = resp.body.decode()
    assert "<TABLE_NAME>@top_cities</TABLE_NAME>" in body
    assert "<TABLE_NAME>sales</TABLE_NAME>" in body


@pytest.mark.asyncio
async def test_discover_db_schema_columns_includes_named_query_columns(
    monkeypatch, _patched,
):
    resp = await _discover("DBSCHEMA_COLUMNS", catalog="sales")
    body = resp.body.decode()
    assert "<TABLE_NAME>@top_cities</TABLE_NAME>" in body
    assert "<COLUMN_NAME>city_name</COLUMN_NAME>" in body
    assert "<COLUMN_NAME>revenue</COLUMN_NAME>" in body


@pytest.mark.asyncio
async def test_undeployed_model_advertises_no_named_query_tables(
    monkeypatch, _patched,
):
    """Invariant 7: only the deployed snapshot defines Named Queries."""
    async def undeployed_resolve(catalog, tenant_slug, jwt_token):
        return "model-1", "proj-1", None, None

    fetched: list[tuple] = []

    async def nq_fetch(*args, **kwargs):
        fetched.append(args)
        return []

    monkeypatch.setattr(xs, "_resolve_model_id", undeployed_resolve)
    monkeypatch.setattr(xs, "get_deployed_named_queries", nq_fetch)

    resp = await _discover("DBSCHEMA_TABLES", catalog="sales")
    assert resp.status_code == 200
    assert b"@top_cities" not in resp.body
    # The fetch must not even run for an undeployed model.
    assert fetched == []


@pytest.mark.asyncio
async def test_named_queries_fetched_only_for_table_rowsets(
    monkeypatch, _patched,
):
    """The deployed-snapshot fetch is scoped to the table-listing rowsets."""
    fetched: list[str] = []

    async def nq_fetch(model_id, deployed_version_id, tenant_slug,
                       jwt_token, **kw):
        fetched.append(str(deployed_version_id))
        return [_NQ_DEPLOYED]

    monkeypatch.setattr(xs, "get_deployed_named_queries", nq_fetch)

    await _discover("MDSCHEMA_DIMENSIONS", catalog="sales")
    assert fetched == []
    await _discover("DBSCHEMA_TABLES", catalog="sales")
    assert fetched == ["v-1"]


def _persona_resolve(slug: str):
    async def resolve(catalog, tenant_slug, jwt_token):
        return "model-1", "proj-1", {
            "id": "p-1", "slug": slug,
            "included_measure_ids": ["m1"], "included_dimension_ids": ["d1"],
        }, "v-1"
    return resolve


def _router_verdict(monkeypatch, visible: set[str] | None):
    """Stand in for the query-router's bind verdict (Bug-9186)."""
    import src.router_client as rc

    calls: list[tuple[str, str | None]] = []

    async def fake_visible(model_id, jwt_token, persona_id):
        calls.append((model_id, persona_id))
        return visible

    monkeypatch.setattr(rc, "persona_visible_named_queries", fake_visible)
    return calls


@pytest.mark.asyncio
async def test_named_query_that_binds_for_the_persona_is_advertised(
    monkeypatch, _patched,
):
    """Bug-9186 / rule 4: visible iff it BINDS over the persona's model query.

    The verdict comes from the query-router — the same authority its executor
    applies — so a Named Query the persona can run is advertised on the
    persona-variant catalogue, and the router is asked for THAT persona.
    """
    monkeypatch.setattr(xs, "_resolve_model_id", _persona_resolve("business"))
    calls = _router_verdict(monkeypatch, {"top_cities"})

    resp = await _discover("DBSCHEMA_TABLES", catalog="sales_business")
    body = resp.body.decode()
    assert "<TABLE_NAME>@top_cities</TABLE_NAME>" in body
    assert "<TABLE_NAME>sales_business</TABLE_NAME>" in body
    assert calls == [("model-1", "p-1")]


@pytest.mark.asyncio
async def test_named_query_that_does_not_bind_is_suppressed(
    monkeypatch, _patched,
):
    """The catalogue must not advertise a Named Query the executor refuses.

    This is the leak Bug-9178 closed bluntly (hide ALL Named Queries on any
    narrowed surface) and Bug-9186 now closes per object: neither the table
    row nor its column rows may appear for a persona the definition does not
    bind for — a restricted MEASURE, a CLS tag or a persona default filter
    all reach this verdict, none of which the old identifier scan could see.
    """
    monkeypatch.setattr(xs, "_resolve_model_id", _persona_resolve("regiononly"))
    _router_verdict(monkeypatch, set())

    for rowset in ("DBSCHEMA_TABLES", "DBSCHEMA_COLUMNS"):
        resp = await _discover(rowset, catalog="sales_regiononly")
        body = resp.body.decode()
        assert "@top_cities" not in body, (
            f"{rowset}: a Named Query that does not bind for this persona must "
            f"NOT be advertised"
        )
        assert "city_name" not in body, (
            f"{rowset}: its column list must not leak either"
        )
        # The rest of the persona surface is still advertised, proving the
        # suppression is per-object and not a blanket failure.
        assert "sales_regiononly" in body


@pytest.mark.asyncio
async def test_unreachable_router_hides_every_named_query(
    monkeypatch, _patched,
):
    """Fail CLOSED: a verdict that cannot be obtained is not granted.

    ``persona_visible_named_queries`` returns an empty set when the router
    cannot be reached, so the catalogue advertises no Named Query relation
    rather than one the executor may refuse.
    """
    import src.router_client as rc

    monkeypatch.setattr(xs, "_resolve_model_id", _persona_resolve("business"))

    class _DeadClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    # Undo the fixture's stub: this cell must drive the REAL helper, whose
    # fail-closed branch is what it exists to prove.
    monkeypatch.setattr(
        rc, "persona_visible_named_queries", _REAL_PERSONA_VISIBLE,
    )
    monkeypatch.setattr(rc.httpx, "AsyncClient", _DeadClient)

    resp = await _discover("DBSCHEMA_TABLES", catalog="sales_business")
    body = resp.body.decode()
    assert "@top_cities" not in body
    assert "<TABLE_NAME>sales_business</TABLE_NAME>" in body


@pytest.mark.asyncio
async def test_base_catalogue_asks_the_router_with_no_persona_id(
    monkeypatch, _patched,
):
    """The BASE catalogue is asked too, with no ``persona_id``.

    A viewer connects to the base catalogue, not to a persona-variant one, so
    the gateway holds no persona object for it. Skipping the verdict on that
    basis would have left every base-surface catalogue unfiltered. The router
    resolves the CALLER'S OWN effective persona from the bearer instead — the
    same resolution ``/execute`` performs — and a privileged caller genuinely
    has none, so it gets everything back.
    """
    calls = _router_verdict(monkeypatch, {"top_cities"})

    resp = await _discover("DBSCHEMA_TABLES", catalog="sales")
    assert "<TABLE_NAME>@top_cities</TABLE_NAME>" in resp.body.decode()
    assert calls == [("model-1", None)]

    calls.clear()
    _router_verdict(monkeypatch, set())
    resp = await _discover("DBSCHEMA_TABLES", catalog="sales")
    assert "@top_cities" not in resp.body.decode(), (
        "a base-surface caller whose own persona does not bind the Named "
        "Query must not be shown it"
    )


@pytest.mark.asyncio
async def test_409_invalid_deployed_snapshot_faults_table_discovery(
    monkeypatch, _patched,
):
    """Bug-8384 parity: a broken deployed authority must fault, never render
    a deceptive empty Named Query table list."""
    async def boom(*a, **kw):
        raise _status_error(409)

    monkeypatch.setattr(xs, "get_deployed_named_queries", boom)

    resp = await _discover("DBSCHEMA_TABLES", catalog="sales")
    body = resp.body.decode()
    assert "Fault" in body
    assert "DEPLOYED_SNAPSHOT_INVALID" in body
    assert "Redeploy the model" in body


@pytest.mark.asyncio
async def test_transient_named_query_fetch_failure_degrades_gracefully(
    monkeypatch, _patched,
):
    """A network blip must not fault the whole table discovery."""
    async def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(xs, "get_deployed_named_queries", boom)

    resp = await _discover("DBSCHEMA_TABLES", catalog="sales")
    assert resp.status_code == 200
    assert b"<TABLE_NAME>sales</TABLE_NAME>" in resp.body


# ---------------------------------------------------------------------------
# router_client helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_deployed_named_queries_reads_snapshot_definitions(monkeypatch):
    import src.router_client as rc

    async def fake_snapshot(model_id, version_id, tenant_slug, jwt_token,
                            **kwargs):
        return {"named_queries": [_NQ_DEPLOYED, "junk", None, {"name": ""}]}

    monkeypatch.setattr(rc, "get_model_version_snapshot", fake_snapshot)

    nqs = await get_deployed_named_queries("m1", "v-1", "acme", "tok")
    assert [nq["name"] for nq in nqs] == ["top_cities"]


@pytest.mark.asyncio
async def test_get_deployed_named_queries_fails_closed_on_transient_error(
    monkeypatch,
):
    import src.router_client as rc

    async def boom(model_id, version_id, tenant_slug, jwt_token, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(rc, "get_model_version_snapshot", boom)

    assert await get_deployed_named_queries("m1", "v-1", "acme", "tok") == []


@pytest.mark.asyncio
async def test_get_deployed_named_queries_propagates_409(monkeypatch):
    """The 409 DEPLOYED_SNAPSHOT_INVALID must reach the discovery handler so
    it can fault (Bug-8384), not be swallowed into an empty list."""
    import src.router_client as rc

    async def boom(model_id, version_id, tenant_slug, jwt_token, **kwargs):
        raise _status_error(409)

    monkeypatch.setattr(rc, "get_model_version_snapshot", boom)

    with pytest.raises(httpx.HTTPStatusError):
        await get_deployed_named_queries("m1", "v-1", "acme", "tok")
