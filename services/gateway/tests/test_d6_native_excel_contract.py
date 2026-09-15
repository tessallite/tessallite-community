"""D6 native Excel XMLA contract regressions.

These tests exercise the XMLA rowset and MDDataSet builders directly. They
verify the structural contract a BI client consumes: grouped hierarchy names,
native All/child tuples, subtotal values, and executable KPI member identity.
They do not pretend to reproduce Excel's private OOXML cache format.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from src.dax.cube_model import build_cube_dimensions
from src.dax.kpi_persona_filter import filter_kpis_for_native_xmla
from src.dax.mdschema import _rows_hierarchies, _rows_kpis
from src.dax.mdx_execute import build_real_execute_response, resolve_kpi_property_expr
from src.dax.subtotal_engine import (
    SUBTOTAL_GRAIN_KEY,
    SUBTOTAL_GRAIN_PREFIX,
    SubtotalHierarchy,
    SubtotalLevel,
)

import pytest


@pytest.fixture(autouse=True)
def _select_calculated_total_profile(monkeypatch):
    """This file specifies the calculated-total Excel profile. It is no longer
    the default (owner decision 2026-09-04, native-all is), so select it
    explicitly; the contract it pins is unchanged."""
    monkeypatch.setenv("TESSALLITE_XMLA_EXCEL_PROFILE", "calculated-total")

_NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"
_DIMS = [
    {"name": "account_type", "display_name": "Account Type"},
    {"name": "channel_name", "display_name": "Channel"},
]
_MEASURES = [{"id": "m_amount", "name": "base_amount", "default_agg": "sum"}]


def _subtotal(name: str) -> SubtotalHierarchy:
    return SubtotalHierarchy(
        hierarchy_name=name,
        mdx_dim_name=name,
        mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=0, dim_name=name)],
        axis=1,
    )


def _rollup_rows() -> list[dict[str, object]]:
    grain = SUBTOTAL_GRAIN_PREFIX
    return [
        {"account_type": "CHECKING", "channel_name": "WEB", "base_amount": 10,
         SUBTOTAL_GRAIN_KEY: 0,
         grain + "account_type": 0, grain + "channel_name": 0},
        {"account_type": "CHECKING", "channel_name": "STORE", "base_amount": 20,
         SUBTOTAL_GRAIN_KEY: 0,
         grain + "account_type": 0, grain + "channel_name": 0},
        {"account_type": "CHECKING", "channel_name": "", "base_amount": 30,
         SUBTOTAL_GRAIN_KEY: -1,
         grain + "account_type": 0, grain + "channel_name": -1},
        {"account_type": "SAVINGS", "channel_name": "WEB", "base_amount": 5,
         SUBTOTAL_GRAIN_KEY: 0,
         grain + "account_type": 0, grain + "channel_name": 0},
        {"account_type": "SAVINGS", "channel_name": "STORE", "base_amount": 10,
         SUBTOTAL_GRAIN_KEY: 0,
         grain + "account_type": 0, grain + "channel_name": 0},
        {"account_type": "SAVINGS", "channel_name": "", "base_amount": 15,
         SUBTOTAL_GRAIN_KEY: -1,
         grain + "account_type": 0, grain + "channel_name": -1},
        {"account_type": "", "channel_name": "", "base_amount": 45,
         SUBTOTAL_GRAIN_KEY: -2,
         grain + "account_type": -1, grain + "channel_name": -1},
    ]


# Bug-9788: the rollup Execute contract asserted below (All tuples with
# source-computed subtotal values) belongs to clients that RECEIVE
# ALL_MEMBER in Discover. Excel does not — its Execute axes suppress
# All-grain tuples to match (the two-flat save refusal, proven live), so
# these builder tests run as a non-Excel client. The Excel-side pairing is
# owned by test_bug9788_excel_rollup_all_pairing.py.
_ROLLUP_CLIENT_APP = "Tessallite XMLA Conformance Harness"


def _xml_for_grouping(
    grouping: str, monkeypatch, app_name: str = _ROLLUP_CLIENT_APP,
) -> str:
    monkeypatch.setenv("TESSALLITE_XMLA_FIELD_LIST_GROUPING", grouping)
    mdx = (
        "SELECT {[Measures].[base_amount]} ON COLUMNS, "
        "CrossJoin(DrilldownLevel({[account_type].[account_type].[All]}), "
        "DrilldownLevel({[channel_name].[channel_name].[All]})) ON ROWS "
        "FROM [demo]"
    )
    return build_real_execute_response(
        mdx=mdx,
        catalog="demo",
        columns=["account_type", "channel_name", "base_amount"],
        rows=_rollup_rows(),
        measures_meta=_MEASURES,
        dimensions_meta=_DIMS,
        hierarchy_defs=[],
        client_app_name=app_name,
        axis_format="tupleformat",
        subtotal_hierarchies=[_subtotal("account_type"), _subtotal("channel_name")],
    )


def _axis_tuples(xml: str, axis_name: str) -> list[list[ET.Element]]:
    root = ET.fromstring(xml)
    axis = next(a for a in root.iter(_NS + "Axis") if a.get("name") == axis_name)
    return [list(t.iter(_NS + "Member")) for t in axis.iter(_NS + "Tuple")]


def test_bug_9789_excel_advertises_all_member_and_keeps_requested_rollups(monkeypatch):
    """Discover advertises ALL_MEMBER for Excel; the rollup wire keeps one identity.

    Bug-9772 / Bug-9874: Excel and every other client share the native-all
    contract, so ALL_MEMBER is advertised and All-grain rollup tuples are
    returned on the native All member. The SaveAs refusal that once made this
    column Excel-suppressed was the enumerated member properties, not this.
    """
    cube = build_cube_dimensions(_DIMS, [])
    hier_rows = _rows_hierarchies(
        "demo", cube, _MEASURES, {},
        {"SspropInitAppName": "Microsoft Office Excel"},
    )
    account = next(r for r in hier_rows if r.get("HIERARCHY_NAME") == "account_type")
    assert account["HIERARCHY_CAPTION"] == "Account Type"
    assert account["DEFAULT_MEMBER"] == "[Dimensions].[account_type].[All]"
    assert account["ALL_MEMBER"] == "[Dimensions].[account_type].[All]"

    non_excel_rows = _rows_hierarchies(
        "demo", cube, _MEASURES, {}, {"SspropInitAppName": "Power BI"},
    )
    non_excel = next(
        r for r in non_excel_rows if r.get("HIERARCHY_NAME") == "account_type"
    )
    assert non_excel["ALL_MEMBER"] == "[Dimensions].[account_type].[All]"

    xml = _xml_for_grouping("true", monkeypatch)
    tuples = _axis_tuples(xml, "Axis1")
    assert tuples
    assert all(len(item) == 2 for item in tuples)
    names = [
        [member.findtext(_NS + "UName") for member in item]
        for item in tuples
    ]
    assert ["[Dimensions].[account_type].[All]", "[Dimensions].[channel_name].[All]"] in names
    assert ["[Dimensions].[account_type].[CHECKING]", "[Dimensions].[channel_name].[WEB]"] in names
    # A synthetic All is a rollup member only in the tuple whose source grain
    # requested it; no extra All tuple is manufactured by axis assembly.
    assert len(names) == 7
    assert len([name for name in names if name[0].endswith(".[All]")]) == 1
    assert len([name for name in names if name[1].endswith(".[All]")]) == 3

    all_members = {
        member.findtext(_NS + "UName"): member
        for item in tuples
        for member in item
        if member.findtext(_NS + "UName", "").endswith(".[All]")
    }
    child_members = {
        member.findtext(_NS + "UName"): member
        for item in tuples
        for member in item
        if not member.findtext(_NS + "UName", "").endswith(".[All]")
    }
    assert all_members
    assert child_members
    assert all(
        member.findtext(_NS + "MEMBER_TYPE") == "2"
        and member.findtext(_NS + "LName").endswith(".[(All)]")
        and member.findtext(_NS + "PARENT_UNIQUE_NAME") == ""
        and member.find(_NS + "PARENT_UNIQUE_NAME").get(
            "{http://www.w3.org/2001/XMLSchema-instance}nil"
        ) == "true"
        for member in all_members.values()
    )
    for member in child_members.values():
        hierarchy = member.findtext(_NS + "HIERARCHY_UNIQUE_NAME", "")
        hierarchy_name = hierarchy.rsplit("].[", 1)[-1].rstrip("]")
        assert member.findtext(_NS + "MEMBER_TYPE") == "1"
        assert member.findtext(_NS + "LName") == f"{hierarchy}.[{hierarchy_name}]"
        assert member.findtext(_NS + "PARENT_UNIQUE_NAME") == f"{hierarchy}.[All]"

    cells = {
        int(cell.get("CellOrdinal")): float(cell.findtext(_NS + "Value"))
        for cell in ET.fromstring(xml).iter(_NS + "Cell")
    }
    assert list(cells.values()) == [10, 20, 30, 5, 10, 15, 45]

    # Bug-9874: the SAME build under the Excel identity is byte-for-byte the
    # native shape -- Excel's DISCOVER advertised the All member and the axis
    # carries it. No interim calculated Total (MEMBER_TYPE=4) exists any more.
    excel_xml = _xml_for_grouping(
        "true", monkeypatch, app_name="Microsoft Office Excel",
    )
    excel_tuples = _axis_tuples(excel_xml, "Axis1")
    assert len(excel_tuples) == 7  # 4 detail + 2 subtotal + 1 grand total
    excel_names = [
        [member.findtext(_NS + "UName") for member in item] for item in excel_tuples
    ]
    assert excel_names == names
    for item in excel_tuples:
        for member in item:
            assert member.findtext(_NS + "MEMBER_TYPE") in ("1", "2")


def test_bug_9244_weighted_average_values_map_to_all_76_nested_row_tuples():
    """The live 5 x 2 x 6 shape keeps every source-computed AVG subtotal.

    Parent values are deliberately unlike either the sum or unweighted mean of
    their visible leaf averages.  This proves the response maps router/source
    values to the requested tuples instead of folding averages in the gateway.
    """
    grain = SUBTOTAL_GRAIN_PREFIX
    accounts = [f"A{i}" for i in range(1, 6)]
    refunds = ["False", "True"]
    sources = [f"S{i}" for i in range(1, 7)]
    rows: list[dict[str, object]] = []

    for account_index, account in enumerate(accounts, start=1):
        for refund_index, refund in enumerate(refunds):
            for source_index, source in enumerate(sources, start=1):
                rows.append({
                    "account_type": account,
                    "refund_flag": refund,
                    "source_system": source,
                    "average_base_amount": account_index * 10 + refund_index * 2 + source_index,
                    grain + "account_type": 0,
                    grain + "refund_flag": 0,
                    grain + "source_system": 0,
                })
            rows.append({
                "account_type": account,
                "refund_flag": refund,
                "source_system": "",
                "average_base_amount": 10_000 + account_index * 100 + refund_index,
                grain + "account_type": 0,
                grain + "refund_flag": 0,
                grain + "source_system": -1,
            })
        rows.append({
            "account_type": account,
            "refund_flag": "",
            "source_system": "",
            "average_base_amount": 20_000 + account_index,
            grain + "account_type": 0,
            grain + "refund_flag": -1,
            grain + "source_system": -1,
        })
    rows.append({
        "account_type": "",
        "refund_flag": "",
        "source_system": "",
        "average_base_amount": 30_000,
        grain + "account_type": -1,
        grain + "refund_flag": -1,
        grain + "source_system": -1,
    })

    xml = build_real_execute_response(
        mdx=(
            "SELECT {[Measures].[average_base_amount]} ON COLUMNS, "
            "CrossJoin(CrossJoin([account_type].[account_type].Members, "
            "[refund_flag].[refund_flag].Members), "
            "[source_system].[source_system].Members) ON ROWS FROM [modely]"
        ),
        catalog="modely",
        columns=[
            "account_type", "refund_flag", "source_system",
            "average_base_amount",
        ],
        rows=rows,
        measures_meta=[{
            "name": "average_base_amount",
            "display_name": "Average Base Amount",
            "default_agg": "avg",
        }],
        dimensions_meta=[
            {"name": "account_type"},
            {"name": "refund_flag"},
            {"name": "source_system"},
        ],
        client_app_name=_ROLLUP_CLIENT_APP,
        axis_format="tupleformat",
        subtotal_hierarchies=[
            _subtotal("account_type"),
            _subtotal("refund_flag"),
            _subtotal("source_system"),
        ],
    )

    tuples = _axis_tuples(xml, "Axis1")
    cells = [
        float(cell.findtext(_NS + "Value"))
        for cell in ET.fromstring(xml).iter(_NS + "Cell")
    ]
    assert len(tuples) == 76
    assert len(cells) == 76
    values = {
        tuple(member.findtext(_NS + "Caption") for member in members): cells[index]
        for index, members in enumerate(tuples)
    }

    for account_index, account in enumerate(accounts, start=1):
        for refund_index, refund in enumerate(refunds):
            expected = 10_000 + account_index * 100 + refund_index
            assert values[(account, refund, "All source_system")] == expected
        assert values[(account, "All refund_flag", "All source_system")] == 20_000 + account_index
    assert values[("All account_type", "All refund_flag", "All source_system")] == 30_000
    assert ("All account_type", "False", "S1") not in values

    leaf_values = [values[("A1", "False", source)] for source in sources]
    assert values[("A1", "False", "All source_system")] != sum(leaf_values)
    assert values[("A1", "False", "All source_system")] != sum(leaf_values) / len(leaf_values)


def test_bug_9788_grouping_keeps_crossjoin_arity_and_totals(monkeypatch):
    """Both grouped and ungrouped wire modes keep captions, axes, and values."""
    for grouping, prefix in (("true", "[Dimensions]"), ("false", "[account_type]")):
        xml = _xml_for_grouping(grouping, monkeypatch)
        tuples = _axis_tuples(xml, "Axis1")
        assert all(len(item) == 2 for item in tuples)
        first = tuples[0]
        assert first[0].findtext(_NS + "HIERARCHY_UNIQUE_NAME") == f"{prefix}.[account_type]"
        assert first[1].findtext(_NS + "HIERARCHY_UNIQUE_NAME") == (
            "[Dimensions].[channel_name]" if grouping == "true"
            else "[channel_name].[channel_name]"
        )
        values = [
            float(cell.findtext(_NS + "Value"))
            for cell in ET.fromstring(xml).iter(_NS + "Cell")
        ]
        assert values[-1] == 45


def test_bug_9830_native_kpi_discover_execute_and_persona_parity():
    measures = [
        {"id": "m_revenue", "name": "Revenue"},
        {"id": "m_orders", "name": "Orders"},
        {"id": "m_hidden", "name": "Hidden", "is_hidden": True},
    ]
    kpis = [
        {"id": "k_revenue", "name": "Revenue KPI", "display_name": "Revenue KPI",
         "value_measure_id": "m_revenue", "expression": ""},
        {"id": "k_composite", "name": "Composite KPI", "display_name": "Composite KPI",
         "value_measure_id": None,
         "expression": 'measure("Revenue") + measure("Orders")'},
        {"id": "k_missing", "name": "Missing KPI", "display_name": "Missing KPI",
         "value_measure_id": "m_missing", "expression": ""},
        {"id": "k_hidden", "name": "Hidden KPI", "display_name": "Hidden KPI",
         "value_measure_id": "m_hidden", "expression": ""},
    ]

    visible_measures = [m for m in measures if not m.get("is_hidden")]
    rows = _rows_kpis("demo", kpis, visible_measures)
    assert [row["KPI_NAME"] for row in rows] == ["Revenue KPI"]
    advertised = rows[0]["KPI_VALUE"]
    executed = resolve_kpi_property_expr(kpis[0], "KPIValue", measures)
    assert advertised == executed == "[Measures].[Revenue]"

    assert [k["name"] for k in filter_kpis_for_native_xmla(
        kpis, visible_measures, {"m_revenue"},
    )] == ["Revenue KPI"]
    assert filter_kpis_for_native_xmla(
        kpis, visible_measures,
    ) == [kpis[0]]
