"""Bug-9845 — CrossJoin of flat attributes must return the full Cartesian set.

``CrossJoin`` is the Cartesian product of its inputs. For ``account_type`` (5)
and ``channel_name`` (7) the client asks for ``(5+1) x (7+1) = 48`` tuples;
for three fields ``(5+1)(2+1)(6+1) = 126``. ``build_multi_subtotal_queries``
pruned every grain where an outer field was at All while an inner field was
at detail, so the gateway returned 41 and 76: whole subtotal families deleted
before ``NON EMPTY`` could ever evaluate them. Wrong numbers by omission.

The pruning was introduced for the suppressed-All era (Bug-9244: cells were
positioned by ordinal and the extra tuples displaced values). On the native-All
profile a real engine returns the whole set and Excel matches cells to tuples,
so the only requirement is ``Hierarchize`` order, which the merge sort already
produces. The legacy ``calculated-total`` profile keeps the pruning, frozen
as measured, until Bug-9851 retires it.

Test escape: the multi-hierarchy production-path tests were written against
the pruned count (41) and asserted it as the contract.
Guard: this file. Tier: T3 (wrong numbers on the release path).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

import pytest

from src.dax import xmla_server
from src.dax.subtotal_engine import (
    SubtotalHierarchy,
    SubtotalLevel,
    build_multi_subtotal_queries,
)

_NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"
_EXCEL = "Microsoft Office Excel"
_MEMBERS = {
    "account_type": ["CREDIT", "CURRENT", "LOAN", "SAVINGS", "WALLET"],
    "channel_name": ["API", "ATM", "Batch", "Branch", "Mobile", "POS", "Web"],
    "refund_flag": ["False", "True"],
}


def _execute_method(statement: str) -> Element:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command><Statement>{statement}</Statement></Command>
      <Properties><PropertyList>
        <Catalog>m</Catalog>
        <AxisFormat>TupleFormat</AxisFormat>
        <SspropInitAppName>{_EXCEL}</SspropInitAppName>
      </PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    method = xmla_server._find_method(ET.fromstring(xml))
    assert method is not None
    return method


def _native_statement(dimensions: list[str], measure: str) -> str:
    """The exact shape Excel sends on the native-All profile (ALEX log)."""
    sets = [
        f"Hierarchize(AddCalculatedMembers({{DrilldownLevel({{[{d}].[{d}].[All]}})}}))"
        for d in dimensions
    ]
    axis = sets[0]
    for current in sets[1:]:
        axis = f"CrossJoin({axis}, {current})"
    return (
        f"SELECT {{[Measures].[{measure}]}} DIMENSION PROPERTIES PARENT_UNIQUE_NAME "
        f"ON COLUMNS, NON EMPTY {axis} DIMENSION PROPERTIES PARENT_UNIQUE_NAME,"
        "HIERARCHY_UNIQUE_NAME,MEMBER_TYPE ON ROWS FROM [m]"
    )


def _fake_source(dimensions: list[str]):
    """Answer any grain SQL with one row per member combination of the grouped
    dims; the value encodes the coordinate so every tuple can be checked."""
    def value_for(coord: dict[str, str]) -> float:
        # Distinct, deterministic, and different per grain so a shifted cell
        # would be caught: sum of member positions, All contributes 100.
        total = 0.0
        for d in dimensions:
            if d in coord:
                total += 1 + _MEMBERS[d].index(coord[d])
            else:
                total += 100.0
        return total

    async def execute_query(**kwargs: Any):
        sql = str(kwargs.get("sql", ""))
        grouped = [d for d in dimensions if re.search(rf'"{d}"', sql)]
        rows: list[dict[str, Any]] = []
        from itertools import product
        for combo in product(*[_MEMBERS[d] for d in grouped]) if grouped else [()]:
            coord = dict(zip(grouped, combo))
            rows.append({**coord, "avg_base_amount": value_for(coord)})
        return {"columns": grouped + ["avg_base_amount"], "rows": rows}
    return execute_query, value_for


def _patch(monkeypatch: pytest.MonkeyPatch, dimensions: list[str], execute_query) -> None:
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None, None

    async def fake_list_models(tenant_slug: str, jwt_token: str):
        return [{"id": "model-1", "slug": "m", "project_slug": "p"}]

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id="", **_):
        return [{"name": "avg_base_amount", "default_agg": "avg"}]

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id="", **_):
        return [{"name": d, "display_name": d} for d in dimensions]

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "list_all_models_for_tenant", fake_list_models)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", execute_query)


def _tuples(body: str) -> list[list[dict[str, str]]]:
    root = ET.fromstring(body)
    axis = next(a for a in root.iter(_NS + "Axis") if a.get("name") == "Axis1")
    out = []
    for tup in axis.iter(_NS + "Tuple"):
        members = []
        for m in tup.iter(_NS + "Member"):
            members.append({
                "uname": m.findtext(_NS + "UName", "") or "",
                "lnum": m.findtext(_NS + "LNum", "") or "",
                "caption": m.findtext(_NS + "Caption", "") or "",
                "type": m.findtext(_NS + "MEMBER_TYPE", "") or "",
            })
        out.append(members)
    return out


def _cells(body: str) -> dict[int, float]:
    root = ET.fromstring(body)
    return {int(c.get("CellOrdinal")): float(c.findtext(_NS + "Value")) for c in root.iter(_NS + "Cell")}


@pytest.fixture(autouse=True)
def _native_profile(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TESSALLITE_XMLA_EXCEL_PROFILE", raising=False)
    monkeypatch.delenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dimensions, expected",
    [
        (["account_type", "channel_name"], (5 + 1) * (7 + 1)),
        (["account_type", "refund_flag", "channel_name"], (5 + 1) * (2 + 1) * (7 + 1)),
    ],
    ids=["two-flat-48", "three-flat-144"],
)
async def test_bug9845_native_all_returns_the_full_cartesian_set(
    monkeypatch: pytest.MonkeyPatch, dimensions: list[str], expected: int,
) -> None:
    execute_query, value_for = _fake_source(dimensions)
    _patch(monkeypatch, dimensions, execute_query)
    response = await xmla_server._handle_execute(
        _execute_method(_native_statement(dimensions, "avg_base_amount")),
        tenant_slug="demo", jwt_token="token", session_id="bug-9845",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200 and "<soap11env:Fault>" not in body

    tuples = _tuples(body)
    assert len(tuples) == expected, f"{len(tuples)} tuples returned, {expected} requested"

    # Every combination of All/member per hierarchy appears exactly once.
    sigs = Counter(tuple(m["lnum"] for m in t) for t in tuples)
    from itertools import product
    for combo in product("01", repeat=len(dimensions)):
        want = 1
        for d, lv in zip(dimensions, combo):
            want *= len(_MEMBERS[d]) if lv == "1" else 1
        assert sigs[combo] == want, (combo, sigs[combo], want)

    # Hierarchize order: All x All... first, and for any fixed outer prefix the
    # inner All row precedes that prefix's inner member rows.
    assert all(m["lnum"] == "0" for m in tuples[0])
    seen_prefix_member: set[tuple[str, ...]] = set()
    for t in tuples:
        for depth in range(1, len(t)):
            prefix = tuple(m["uname"] for m in t[:depth])
            if t[depth]["lnum"] == "0":
                assert prefix not in seen_prefix_member, (prefix, t)
            else:
                seen_prefix_member.add(prefix)

    # Every cell carries the SOURCE value of its own coordinate: no shifting.
    cells = _cells(body)
    assert len(cells) == expected
    for ordinal, t in enumerate(tuples):
        coord = {d: m["caption"] for d, m in zip(dimensions, t) if m["lnum"] == "1"}
        assert cells[ordinal] == value_for(coord), (ordinal, t, cells[ordinal])

    # The All member is a real MEMBER_TYPE=2 All, never a calculated Total.
    assert all(m["type"] == "2" for t in tuples for m in t if m["lnum"] == "0")
    assert "__TessalliteTotal__" not in body


def test_bug9845_engine_emits_every_grain() -> None:
    hierarchies = [
        SubtotalHierarchy(hierarchy_name=d, mdx_dim_name=d, mdx_hier_name=d,
                          levels=[SubtotalLevel(name=d, ordinal=1, dim_name=d)],
                          axis=1, is_flat_attribute_rollup=True)
        for d in ("account_type", "channel_name")
    ]
    common = dict(mdx_dims=["account_type", "channel_name"], mdx_measures=["avg_base_amount"],
                  where_sql_clauses=[], model_slug="m",
                  measures_meta=[{"name": "avg_base_amount", "default_agg": "avg"}],
                  hierarchies=hierarchies, measure_canonical={})
    full = build_multi_subtotal_queries(**common)
    # 2^2 combinations minus the all-detail one the caller already ran. The
    # nested-prefix pruning of the retired calculated-total profile is gone
    # (Bug-9874): the All-outer x detail-inner grain is always planned.
    assert len(full) == 3
    labels = {q.level_name for q in full}
    assert "Grand Total" in labels

