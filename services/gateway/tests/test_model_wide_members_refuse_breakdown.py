"""Owner decision 2026-09-04 (manual review on ALEX): a model-wide member --
KPI goal, KPI status, or an info measure (last refreshed / source system /
owner) -- is one figure for the whole model. With a dimension on an axis the
gateway refuses with a specific message; it never repeats the figure per
member (goal), never silently answers one cell against no members (info),
and only the KPI VALUE breaks down by dimension. Without a breakdown all of
them serve.
"""

from __future__ import annotations

from typing import Any

import pytest
from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

from src.dax import xmla_server


def _method(statement: str) -> Element:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
<Execute xmlns="urn:schemas-microsoft-com:xml-analysis"><Command><Statement>{statement}</Statement></Command>
<Properties><PropertyList><Catalog>m</Catalog><AxisFormat>TupleFormat</AxisFormat>
<SspropInitAppName>Microsoft Office Excel</SspropInitAppName></PropertyList></Properties></Execute>
</soap:Body></soap:Envelope>"""
    method = xmla_server._find_method(ET.fromstring(xml))
    assert method is not None
    return method


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    executed: list[str] = []

    async def resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def resolve_model_slug(*_: Any, **__: Any):
        return "m"

    async def measures(*_: Any, **__: Any):
        return [{"id": "m1", "name": "fee_amount", "default_agg": "sum"}]

    async def dimensions(*_: Any, **__: Any):
        return [{"id": "d1", "name": "region"}]

    async def hierarchies(*_: Any, **__: Any):
        return []

    async def named_sets(*_: Any, **__: Any):
        return []

    async def trust(*_: Any, **__: Any):
        return {"_info_last_refreshed": "2026-09-04T00:00:00Z", "_info_source_system": "pg", "_info_owner": "owner@example.test"}

    async def execute_query(sql: str = "", **kwargs: Any):
        executed.append(sql or kwargs.get("sql", ""))
        if "region" in (sql or ""):
            return {"columns": ["region", "fee_amount"], "rows": [
                {"region": "North", "fee_amount": 10.0}, {"region": "South", "fee_amount": 20.0},
            ]}
        return {"columns": ["fee_amount"], "rows": [{"fee_amount": 30.0}]}

    for name, fn in (
        ("_resolve_model_id", resolve_model_id), ("_resolve_model_slug", resolve_model_slug),
        ("get_model_measures", measures), ("get_model_dimensions", dimensions),
        ("get_model_hierarchies", hierarchies), ("get_model_named_sets", named_sets),
        ("_fetch_trust_values", trust), ("execute_query", execute_query),
    ):
        monkeypatch.setattr(xmla_server, name, fn)
    return executed


async def _run(statement: str) -> str:
    response = await xmla_server._handle_execute(
        _method(statement), tenant_slug="demo", jwt_token="t", session_id="mw",
    )
    return response.body.decode("utf-8")


@pytest.mark.parametrize("member", ["_info_owner", "Last Refreshed"])
async def test_info_measure_with_a_dimension_breakdown_is_refused(gateway, member) -> None:
    body = await _run(
        f"SELECT {{[Measures].[fee_amount],[Measures].[{member}]}} ON COLUMNS, "
        "{[region].[region].Members} ON ROWS FROM [m]"
    )
    assert "Fault" in body, body
    assert "was requested with a dimension breakdown" in body, body
    assert "one value for the whole model" in body, body
    assert gateway == [], "no SQL may run for a refused breakdown"


async def test_info_measure_alone_with_a_dimension_is_refused_not_blank(gateway) -> None:
    """The silent case from the owner's review: info-only plus a dimension
    used to answer one cell against no members, which Excel showed as
    nothing at all."""
    body = await _run(
        "SELECT {[Measures].[_info_owner]} ON COLUMNS, {[region].[region].Members} ON ROWS FROM [m]"
    )
    assert "Fault" in body, body
    assert "Info measure '_info_owner' was requested with a dimension breakdown" in body


async def test_info_measure_without_a_breakdown_serves(gateway) -> None:
    body = await _run("SELECT {[Measures].[_info_owner]} ON COLUMNS FROM [m]")
    assert "Fault" not in body, body
    assert "owner@example.test" in body
