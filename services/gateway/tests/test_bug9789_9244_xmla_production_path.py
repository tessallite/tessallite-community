"""Production-shaped XMLA regressions for the native Excel rollup contract.

The tests enter through ``_handle_execute`` so MDX parsing, rollup detection,
source-grain execution, merge, axis construction, and cell serialization are
all exercised together.  They deliberately use source-shaped rows returned
by the request's generated SQL; they do not test a response builder in
isolation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

import pytest

from src.dax import xmla_server
from tests.lattice_fake import lattice_aware
from src.dax.cube_model import advertise_all_member, build_cube_dimensions
from src.dax.mdschema import (
    _rows_hierarchies,
    _rows_levels,
    _rows_members,
    build_discover_response,
)


_NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"
_ROWSET_NS = "{urn:schemas-microsoft-com:xml-analysis:rowset}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_ROW_PROPERTIES = (
    "PARENT_UNIQUE_NAME, HIERARCHY_UNIQUE_NAME, MEMBER_TYPE, "
    "MEMBER_ORDINAL, CHILDREN_CARDINALITY, MEMBER_KEY, MEMBER_VALUE, "
    "MEMBER_NAME, MEMBER_CAPTION, LEVEL_UNIQUE_NAME, LEVEL_NUMBER, "
    "DISPLAY_INFO"
)


# Bug-9788: the rollup wire contract asserted in this module (a COMPLETE All
# identity on the Execute axis, with source-computed weighted subtotals) is the
# contract for clients that RECEIVE ALL_MEMBER in Discover. Excel does not:
# its Execute axes suppress All-grain tuples to match, because the live
# two-flat-attribute save refusal proved Excel cannot cache All members its
# DISCOVER disclaimed. These tests therefore run as a non-Excel client; the
# Excel-side pairing is owned by test_bug9788_excel_rollup_all_pairing.py.
_ROLLUP_CLIENT_APP = "Tessallite XMLA Conformance Harness"


def _execute_method(statement: str, app_name: str = _ROLLUP_CLIENT_APP) -> Element:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Execute xmlns="urn:schemas-microsoft-com:xml-analysis">
      <Command>
        <Statement>{statement}</Statement>
      </Command>
      <Properties><PropertyList>
        <Catalog>m</Catalog>
        <AxisFormat>TupleFormat</AxisFormat>
        <SspropInitAppName>{app_name}</SspropInitAppName>
      </PropertyList></Properties>
    </Execute>
  </soap:Body>
</soap:Envelope>"""
    root = ET.fromstring(xml)
    method = xmla_server._find_method(root)
    assert method is not None
    return method


def _axis_statement(dimensions: list[str], measure: str) -> str:
    sets = [
        f"Hierarchize(AddCalculatedMembers({{[{d}].[{d}].[(All)].Members}}))"
        for d in dimensions
    ]
    axis = sets[0]
    for current in sets[1:]:
        axis = f"CrossJoin({axis}, {current})"
    return (
        f"SELECT {{[Measures].[{measure}]}} ON COLUMNS, "
        f"NON EMPTY {axis} DIMENSION PROPERTIES {_ROW_PROPERTIES} ON ROWS "
        "FROM [m]"
    )


def _excel_slicer_statement(dimensions: list[str], measure: str) -> str:
    """The native PivotTable shape captured from ALEX on 2026-09-02.

    Excel places every selected flat hierarchy on Axis0/COLUMNS and carries the
    sole value member in the WHERE slicer.  The earlier regression guard used
    dimensions on ROWS and the measure on COLUMNS, so it never exercised the
    response topology that Excel persists in its PivotCache.
    """
    sets = [
        f"Hierarchize(AddCalculatedMembers({{DrilldownLevel({{[{d}].[{d}].[All]}})}}))"
        for d in dimensions
    ]
    axis = sets[0]
    for current in sets[1:]:
        axis = f"CrossJoin({axis}, {current})"
    return (
        f"SELECT NON EMPTY {axis} DIMENSION PROPERTIES {_ROW_PROPERTIES} "
        f"ON COLUMNS FROM [m] WHERE ([Measures].[{measure}]) "
        "CELL PROPERTIES VALUE, FORMAT_STRING"
    )


def _patch_execute_environment(
    monkeypatch: pytest.MonkeyPatch,
    dimensions: list[str],
    execute_query: Callable[..., Any],
) -> None:
    async def fake_resolve_model_id(catalog: str, tenant_slug: str, jwt_token: str):
        return "model-1", "project-1", None, None

    async def fake_get_model_measures(
        model_id: str, tenant_slug: str, jwt_token: str,
        project_id: str = "", **_: Any,
    ):
        return [{"name": "average_base_amount", "default_agg": "avg"}]

    async def fake_get_model_dimensions(
        model_id: str, tenant_slug: str, jwt_token: str,
        project_id: str = "", **_: Any,
    ):
        return [{"name": name, "display_name": name} for name in dimensions]

    monkeypatch.setattr(xmla_server, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(xmla_server, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(xmla_server, "execute_query", execute_query)


def _members_from_axis(body: str, axis_name: str = "Axis1") -> list[list[Element]]:
    root = ET.fromstring(body)
    axis = next(
        axis for axis in root.iter(_NS + "Axis")
        if axis.get("name") == axis_name
    )
    return [list(item.iter(_NS + "Member")) for item in axis.iter(_NS + "Tuple")]


def _axis_info_names(body: str) -> list[str]:
    root = ET.fromstring(body)
    return [
        item.get("name", "")
        for item in root.iter(_NS + "AxisInfo")
    ]


def _axis_names(body: str) -> list[str]:
    root = ET.fromstring(body)
    return [item.get("name", "") for item in root.iter(_NS + "Axis")]


def _cells_by_ordinal(body: str) -> dict[int, float]:
    root = ET.fromstring(body)
    return {
        int(cell.get("CellOrdinal", "-1")): float(cell.findtext(_NS + "Value"))
        for cell in root.iter(_NS + "Cell")
    }


def _member_identity(member: Element) -> dict[str, str]:
    def text(name: str, fallback: str = "") -> str:
        return member.findtext(_NS + name, fallback) or fallback

    return {
        "hierarchy": member.get("Hierarchy", "") or "",
        "uname": text("UName"),
        "caption": text("Caption"),
        "lname": text("LName"),
        "lnum": text("LNum"),
        "parent": text("PARENT_UNIQUE_NAME"),
        "type": text("MEMBER_TYPE"),
        "ordinal": text("MEMBER_ORDINAL"),
        "children": text("CHILDREN_CARDINALITY"),
        "display": text("DisplayInfo"),
        "key": text("MEMBER_KEY"),
        "value": text("MEMBER_VALUE"),
        "name": text("MEMBER_NAME"),
    }


def _assert_root_parent_is_xml_null(member: Element) -> None:
    parent = member.find(_NS + "PARENT_UNIQUE_NAME")
    assert parent is not None
    assert parent.text is None
    assert parent.get(_XSI_NIL) == "true"


def _discover_all_rows(
    dimensions: list[str], cardinalities: list[int],
) -> dict[str, dict[str, Any]]:
    metadata = [{"name": name, "display_name": name} for name in dimensions]
    member_data = {
        name: {
            "members": [{"name": f"{name}-value-{i}"} for i in range(cardinality)]
        }
        for name, cardinality in zip(dimensions, cardinalities, strict=True)
    }
    rows = _rows_members(
        "m", [{"name": "average_base_amount"}], metadata, {}, member_data,
    )
    return {
        row["HIERARCHY_UNIQUE_NAME"]: row
        for row in rows
        if row.get("MEMBER_TYPE") == "2"
    }


def test_bug_9789_excel_discovery_keeps_grand_tuple_identity_and_field_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discover exposes the All identity that live Excel uses for totals."""
    monkeypatch.delenv("TESSALLITE_XMLA_ALL_MEMBER", raising=False)
    dimensions = build_cube_dimensions(
        [{"name": "account_type", "display_name": "Account Type"}],
        [],
    )
    properties = {"SspropInitAppName": "Microsoft Office Excel"}
    hierarchy = next(
        row for row in _rows_hierarchies(
            "m", dimensions, [{"name": "average_base_amount"}], {}, properties,
        )
        if row.get("HIERARCHY_NAME") == "account_type"
    )
    levels = [
        row for row in _rows_levels("m", dimensions, {}, properties=properties)
        if row.get("HIERARCHY_UNIQUE_NAME") == "[Dimensions].[account_type]"
    ]

    assert hierarchy["HIERARCHY_CAPTION"] == "Account Type"
    assert hierarchy["DEFAULT_MEMBER"] == "[Dimensions].[account_type].[All]"
    # Bug-9772 / Bug-9874: Excel receives ALL_MEMBER like every other client.
    # The SaveAs refusal that once made this column Excel-suppressed was the
    # enumerated intrinsic member properties, not ALL_MEMBER (bisected
    # 2026-09-03; contract section 4.1); the native-all profile is the only one.
    assert hierarchy["ALL_MEMBER"] == "[Dimensions].[account_type].[All]"

    non_excel = next(
        row for row in _rows_hierarchies(
            "m", dimensions, [{"name": "average_base_amount"}], {},
            {"SspropInitAppName": "Power BI"},
        )
        if row.get("HIERARCHY_NAME") == "account_type"
    )
    assert non_excel["ALL_MEMBER"] == "[Dimensions].[account_type].[All]"
    assert [row["LEVEL_CAPTION"] for row in levels] == [
        "Account Type",
        "account_type",
    ]
    assert [row["LEVEL_NAME"] for row in levels] == ["(All)", "account_type"]
    discovered = _discover_all_rows(["account_type"], [2])
    assert discovered["[Dimensions].[account_type]"]["PARENT_UNIQUE_NAME"] is None


def test_bug_9789_discover_serializes_root_parent_as_xml_null() -> None:
    body = build_discover_response(
        "MDSCHEMA_MEMBERS",
        "m",
        "model-1",
        [{"name": "average_base_amount"}],
        [{"name": "account_type", "display_name": "Account Type"}],
        member_data={
            "account_type": {"members": [{"name": "CURRENT"}]},
        },
    )
    root = ET.fromstring(body)
    rows = list(root.iter(_ROWSET_NS + "row"))
    all_row = next(
        row
        for row in rows
        if row.findtext(_ROWSET_NS + "MEMBER_TYPE") == "2"
    )
    leaf_row = next(
        row
        for row in rows
        if row.findtext(_ROWSET_NS + "MEMBER_TYPE") == "1"
    )

    assert all_row.find(_ROWSET_NS + "PARENT_UNIQUE_NAME") is None
    assert leaf_row.findtext(_ROWSET_NS + "PARENT_UNIQUE_NAME") == (
        "[Dimensions].[account_type].[All]"
    )


def test_bug_9789_excel_all_member_diagnostic_override_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The emergency override serves other clients only; Excel's pair is fixed."""
    monkeypatch.setenv("TESSALLITE_XMLA_ALL_MEMBER", "false")

    assert advertise_all_member(
        properties={"SspropInitAppName": "Microsoft Office Excel"}
    ) is True
def _assert_all_metadata_matches(
    tuples: list[list[Element]], dimensions: list[str], cardinalities: list[int],
) -> None:
    discovered = _discover_all_rows(dimensions, cardinalities)
    by_hierarchy: dict[str, dict[str, str]] = {}
    for item in tuples:
        for member in item:
            identity = _member_identity(member)
            if identity["type"] == "2":
                hierarchy = identity["lname"].rsplit(".[", 1)[0]
                by_hierarchy.setdefault(hierarchy, identity)

    assert set(by_hierarchy) == set(discovered)
    for hierarchy, discover in discovered.items():
        execute = by_hierarchy[hierarchy]
        expected = {
            "hierarchy": discover["HIERARCHY_UNIQUE_NAME"],
            "uname": discover["MEMBER_UNIQUE_NAME"],
            "caption": discover["MEMBER_CAPTION"],
            "lname": discover["LEVEL_UNIQUE_NAME"],
            "lnum": discover["LEVEL_NUMBER"],
            "parent": discover.get("PARENT_UNIQUE_NAME") or "",
            "type": discover["MEMBER_TYPE"],
            "ordinal": discover["MEMBER_ORDINAL"],
            "children": discover["CHILDREN_CARDINALITY"],
            "key": discover["MEMBER_KEY"],
            "name": discover["MEMBER_NAME"],
        }
        expected["display"] = str(
            0x10000 | int(discover["CHILDREN_CARDINALITY"])
        )
        expected["value"] = expected["key"]
        assert execute == expected


def _assert_flat_member_metadata_matches_discovery(
    tuples: list[list[Element]], members_by_dimension: dict[str, list[str]],
) -> None:
    """Compare every persisted flat-member property across Discover/Execute."""
    dimensions = [
        {"name": name, "display_name": name}
        for name in members_by_dimension
    ]
    member_data = {
        name: {
            "members": [
                {
                    "name": value,
                    "key": value,
                    "caption": value,
                    "ordinal": ordinal,
                }
                for ordinal, value in enumerate(sorted(values, key=str.casefold))
            ],
        }
        for name, values in members_by_dimension.items()
    }
    discovered_rows = _rows_members(
        "m", [{"name": "average_base_amount"}], dimensions, {}, member_data,
    )
    discovered = {
        (row["HIERARCHY_UNIQUE_NAME"], row["MEMBER_UNIQUE_NAME"]): row
        for row in discovered_rows
        if row["MEMBER_TYPE"] in {"1", "2"}
    }

    executed: dict[tuple[str, str], dict[str, str]] = {}
    for item in tuples:
        for member in item:
            identity = _member_identity(member)
            key = (identity["hierarchy"], identity["uname"])
            previous = executed.setdefault(key, identity)
            assert previous == identity, f"Execute changed member metadata for {key!r}"

    assert set(executed) == set(discovered)
    for key, execute in executed.items():
        discover = discovered[key]
        expected = {
            "hierarchy": discover["HIERARCHY_UNIQUE_NAME"],
            "uname": discover["MEMBER_UNIQUE_NAME"],
            "caption": discover["MEMBER_CAPTION"],
            "lname": discover["LEVEL_UNIQUE_NAME"],
            "lnum": discover["LEVEL_NUMBER"],
            "parent": discover.get("PARENT_UNIQUE_NAME") or "",
            "type": discover["MEMBER_TYPE"],
            "ordinal": discover["MEMBER_ORDINAL"],
            "children": discover["CHILDREN_CARDINALITY"],
            "key": discover["MEMBER_KEY"],
            "value": discover["MEMBER_KEY"],
            "name": discover["MEMBER_NAME"],
        }
        expected["display"] = (
            str(0x10000 | int(discover["CHILDREN_CARDINALITY"]))
            if int(discover["CHILDREN_CARDINALITY"])
            else "0"
        )
        assert execute == expected, f"Discover/Execute mismatch for {key!r}"


@pytest.mark.parametrize(
    ("statement_factory", "axis_name", "expected_axis_names"),
    [
        (_axis_statement, "Axis1", ["Axis0", "Axis1", "SlicerAxis"]),
        (
            _excel_slicer_statement,
            "Axis0",
            ["Axis0", "SlicerAxis"],
        ),
    ],
    ids=["measure-columns-dimensions-rows", "axis0-measure-slicer"],
)
@pytest.mark.asyncio
async def test_bug_9789_production_path_preserves_all_metadata_and_weighted_avg(
    monkeypatch: pytest.MonkeyPatch,
    statement_factory: Callable[[list[str], str], str],
    axis_name: str,
    expected_axis_names: list[str],
):
    """Bug-9789: Discover and Execute must expose one complete All identity."""
    dimensions = ["account_type", "channel_name"]
    accounts = ["CREDIT", "DEBIT", "SAVINGS", "CHECKING", "OTHER"]
    channels = ["WEB", "BRANCH", "ATM", "MOBILE", "PHONE", "PARTNER", "MAIL"]
    leaf_rows = [
        {
            "account_type": account,
            "channel_name": channel,
            "average_base_amount": float(account_index * 100 + channel_index),
        }
        for account_index, account in enumerate(accounts, 1)
        for channel_index, channel in enumerate(channels, 1)
    ]
    subtotal_values = {
        account: float(1000 + index * 37)
        for index, account in enumerate(accounts, 1)
    }
    # Bug-9845: the All-account x channel family is part of the requested
    # CrossJoin and must be served and returned, not pruned.
    channel_values = {
        channel: float(2000 + index * 11)
        for index, channel in enumerate(channels, 1)
    }
    source_weighted_grand_total = 1816.8733126510879
    sql_calls: list[str] = []

    async def fake_execute_query(
        model_id: str, sql: str, tenant_slug: str, jwt_token: str,
        protocol: str = "dax", **_: Any,
    ):
        has_account = '"account_type"' in sql
        has_channel = '"channel_name"' in sql
        if has_account and has_channel:
            return {
                "columns": [*dimensions, "average_base_amount"],
                "rows": leaf_rows,
            }
        if has_account:
            return {
                "columns": ["account_type", "average_base_amount"],
                "rows": [
                    {"account_type": account, "average_base_amount": value}
                    for account, value in subtotal_values.items()
                ],
            }
        if has_channel:
            return {
                "columns": ["channel_name", "average_base_amount"],
                "rows": [
                    {"channel_name": channel, "average_base_amount": value}
                    for channel, value in channel_values.items()
                ],
            }
        return {
            "columns": ["average_base_amount"],
            "rows": [{"average_base_amount": source_weighted_grand_total}],
        }

    _patch_execute_environment(
        monkeypatch, dimensions,
        lattice_aware(fake_execute_query, sql_calls),
    )
    response = await xmla_server._handle_execute(
        _execute_method(statement_factory(dimensions, "average_base_amount")),
        tenant_slug="demo", jwt_token="token", session_id="bug-9789-production",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body

    assert _axis_info_names(body) == expected_axis_names
    assert _axis_names(body) == expected_axis_names

    tuples = _members_from_axis(body, axis_name)
    # Bug-9845: the full CrossJoin - 35 leaves + 5 account subtotals
    # + 7 channel subtotals + grand total = (5+1) x (7+1).
    assert len(tuples) == 48
    assert sum(all(_member_identity(m)["type"] == "1" for m in item) for item in tuples) == 35
    assert sum(
        _member_identity(item[0])["type"] == "1"
        and _member_identity(item[1])["type"] == "2"
        for item in tuples
    ) == 5
    assert sum(
        _member_identity(item[0])["type"] == "2"
        and _member_identity(item[1])["type"] == "1"
        for item in tuples
    ) == 7
    assert sum(all(_member_identity(m)["type"] == "2" for m in item) for item in tuples) == 1
    for item in tuples:
        for member in item:
            if _member_identity(member)["type"] == "2":
                _assert_root_parent_is_xml_null(member)
    _assert_all_metadata_matches(tuples, dimensions, [len(accounts), len(channels)])
    _assert_flat_member_metadata_matches_discovery(
        tuples,
        {"account_type": accounts, "channel_name": channels},
    )

    values = _cells_by_ordinal(body)
    assert len(values) == 48
    assert tuple(_member_identity(m)["caption"] for m in tuples[0]) == (
        "All account_type", "All channel_name",
    )
    assert values[0] == pytest.approx(source_weighted_grand_total)
    for ordinal, item in enumerate(tuples):
        captions = tuple(_member_identity(m)["caption"] for m in item)
        if captions[0] in subtotal_values and captions[1] == "All channel_name":
            assert values[ordinal] == pytest.approx(subtotal_values[captions[0]])
        if captions[0] == "All account_type" and captions[1] in channel_values:
            assert values[ordinal] == pytest.approx(channel_values[captions[1]])
    # detail + account grain + channel grain + grand total
    # Bug-9864: the three rollup grains arrive in ONE grouping-sets query,
    # so this is the detail query plus one lattice query. Every metadata and
    # weighted-average assertion above is unchanged.
    assert len(sql_calls) == 2


@pytest.mark.asyncio
async def test_bug_9244_production_path_populates_every_three_dimensional_subtotal(
    monkeypatch: pytest.MonkeyPatch,
):
    """Bug-9244: intermediate nested subtotal cells keep their axis ordinal."""
    dimensions = ["account_type", "refund_flag", "source_system"]
    accounts = [f"A{i}" for i in range(1, 6)]
    refunds = ["False", "True"]
    sources = [f"S{i}" for i in range(1, 7)]
    leaves = [
        {
            "account_type": account,
            "refund_flag": refund,
            "source_system": source,
            "average_base_amount": float(account_index + source_index),
        }
        for account_index, account in enumerate(accounts, 1)
        for refund in refunds
        for source_index, source in enumerate(sources, 1)
    ]
    sql_calls: list[str] = []

    async def fake_execute_query(
        model_id: str, sql: str, tenant_slug: str, jwt_token: str,
        protocol: str = "dax", **_: Any,
    ):
        has_account = '"account_type"' in sql
        has_refund = '"refund_flag"' in sql
        has_source = '"source_system"' in sql
        if has_account and has_refund and has_source:
            return {
                "columns": [*dimensions, "average_base_amount"],
                "rows": leaves,
            }
        if has_account and has_refund and not has_source:
            return {
                "columns": ["account_type", "refund_flag", "average_base_amount"],
                "rows": [
                    {
                        "account_type": account,
                        "refund_flag": refund,
                        "average_base_amount": float(10000 + account_index * 10 + refund_index),
                    }
                    for account_index, account in enumerate(accounts, 1)
                    for refund_index, refund in enumerate(refunds)
                ],
            }
        if has_account and not has_refund and not has_source:
            return {
                "columns": ["account_type", "average_base_amount"],
                "rows": [
                    {"account_type": account, "average_base_amount": float(20000 + index)}
                    for index, account in enumerate(accounts, 1)
                ],
            }
        # Bug-9845: every other Cartesian grain is requested too and must be
        # served; values are distinct per grain so a shifted cell is caught.
        grouped = [
            (d, members) for d, members, present in (
                ("account_type", accounts, has_account),
                ("refund_flag", refunds, has_refund),
                ("source_system", sources, has_source),
            ) if present
        ]
        if not grouped:
            return {
                "columns": ["average_base_amount"],
                "rows": [{"average_base_amount": 30000.0}],
            }
        from itertools import product as _product
        rows = []
        for combo in _product(*[m for _d, m in grouped]):
            row = dict(zip([d for d, _m in grouped], combo))
            row["average_base_amount"] = float(
                40000 + sum(
                    (i + 1) * 100 ** k
                    for k, (d, members) in enumerate(grouped)
                    for i, member in enumerate(members) if row[d] == member
                )
            )
            rows.append(row)
        return {
            "columns": [d for d, _m in grouped] + ["average_base_amount"],
            "rows": rows,
        }

    _patch_execute_environment(
        monkeypatch, dimensions,
        lattice_aware(fake_execute_query, sql_calls),
    )
    response = await xmla_server._handle_execute(
        _execute_method(_axis_statement(dimensions, "average_base_amount")),
        tenant_slug="demo", jwt_token="token", session_id="bug-9244-production",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body

    tuples = _members_from_axis(body)
    values = _cells_by_ordinal(body)
    # Bug-9845: the full CrossJoin, (5+1) x (2+1) x (6+1).
    assert len(tuples) == 126
    assert len(values) == len(tuples)
    assert set(values) == set(range(len(tuples)))
    _assert_all_metadata_matches(
        tuples, dimensions, [len(accounts), len(refunds), len(sources)],
    )

    seen_intermediate: set[tuple[str, str, str]] = set()
    for ordinal, item in enumerate(tuples):
        captions = tuple(_member_identity(m)["caption"] for m in item)
        types = tuple(_member_identity(m)["type"] for m in item)
        if types == ("1", "1", "2"):
            account, refund, _ = captions
            expected = 10000 + int(account[1:]) * 10 + int(refund == "True")
            assert values[ordinal] == pytest.approx(expected)
            seen_intermediate.add(captions)
        elif types == ("1", "2", "2"):
            assert values[ordinal] == pytest.approx(20000 + int(captions[0][1:]))
        elif types == ("2", "2", "2"):
            assert values[ordinal] == pytest.approx(30000.0)
    assert len(seen_intermediate) == 10
    # Bug-9845: every one of the 2^3 grain families is present.
    from collections import Counter as _Counter
    families = _Counter(tuple(_member_identity(m)["type"] for m in item) for item in tuples)
    assert families == {
        ("1", "1", "1"): 60, ("1", "1", "2"): 10, ("1", "2", "1"): 30, ("2", "1", "1"): 12,
        ("1", "2", "2"): 5, ("2", "1", "2"): 2, ("2", "2", "1"): 6, ("2", "2", "2"): 1,
    }
    # Bug-9864: detail query + ONE grouping-sets query carrying all seven
    # rollup grains. The 2^3 grain families asserted just above are all still
    # present -- the lattice changed the round trips, not the result.
    assert len(sql_calls) == 2


def test_bug_9789_discovery_fixture_uses_real_member_keys_not_synthetic_all():
    """Real ``All`` data keys remain distinct from the synthetic All member."""
    dimensions = [{"name": "status", "display_name": "Status"}]
    member_data = {
        "status": {
            "members": [
                {"name": key, "key_value": key, "caption": key}
                for key in ("All", "(All)", "all", " ALL ", "A]B")
            ],
        },
    }
    rows = _rows_members("m", [], dimensions, {}, member_data)
    names = [row["MEMBER_UNIQUE_NAME"] for row in rows]
    assert len(names) == len(set(names))
    assert names[0] == "[Dimensions].[status].[All]"
    assert "[Dimensions].[status].[A]]B]" in names


@pytest.mark.asyncio
async def test_bug_9789_real_all_key_uses_specific_member_discovery_path():
    """A canonical real-key All restriction is not treated as a rollup probe."""
    result = await xmla_server._load_hierarchy_member_data(
        model_id="model-1",
        project_id="project-1",
        dimension={
            "name": "status",
            "hierarchy_id": "hierarchy-1",
            "levels": [{"name": "status", "ordinal": 0}],
        },
        tenant_slug="demo",
        jwt_token="token",
        restrictions={
            "MEMBER_UNIQUE_NAME": ["[status].[status].&[All]"],
            "TREE_OP": ["8"],
        },
    )
    assert result["members_by_level"]["0"][0]["name"] == "All"
