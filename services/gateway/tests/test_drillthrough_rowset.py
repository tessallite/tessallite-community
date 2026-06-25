"""Unit tests for the DRILLTHROUGH Rowset XML formatter.

Validates ``build_drillthrough_rowset`` produces correct XMLA Rowset
XML for flat DRILLTHROUGH results.
"""
from __future__ import annotations

from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element

import pytest

from src.dax.drillthrough_handler import (
    build_drillthrough_rowset,
    _augment_hierarchy_columns,
    _extract_measure_name,
    _parse_value,
    _xml_safe_name,
)
from src.dax.ts_mdx_parser import parse_mdx


_ROWSET_NS = "urn:schemas-microsoft-com:xml-analysis:rowset"


def _parse_rowset(xml: str) -> Element:
    """Wrap the rowset body in a container and parse."""
    wrapped = f'<wrapper xmlns:tns="urn:schemas-microsoft-com:xml-analysis">{xml}</wrapper>'
    return ET.fromstring(wrapped)


def _find_rows(xml: str) -> list[Element]:
    root_el = ET.fromstring(xml.split("<return>")[1].split("</return>")[0].replace(
        f'xmlns="{_ROWSET_NS}"', ""))
    return root_el.findall("row")


# ---------------------------------------------------------------------------
# build_drillthrough_rowset
# ---------------------------------------------------------------------------

def test_empty_result():
    xml = build_drillthrough_rowset([], [])
    assert "<return>" in xml
    assert "<root" in xml
    assert "<row" not in xml


def test_single_row_single_column():
    xml = build_drillthrough_rowset(["amount"], [{"amount": 100}])
    assert "<return>" in xml
    rows = _find_rows(xml)
    assert len(rows) == 1
    assert rows[0].find("amount").text == "100"


def test_multiple_rows_multiple_columns():
    rows_data = [
        {"month": "Jan", "amount": 100},
        {"month": "Feb", "amount": 200},
        {"month": "Mar", "amount": 300},
    ]
    xml = build_drillthrough_rowset(["month", "amount"], rows_data)
    rows = _find_rows(xml)
    assert len(rows) == 3
    assert rows[0].find("month").text == "Jan"
    assert rows[0].find("amount").text == "100"
    assert rows[2].find("month").text == "Mar"


def test_xml_special_characters_escaped():
    xml = build_drillthrough_rowset(
        ["name"],
        [{"name": 'AT&T <Corp> "Inc"'}],
    )
    assert "&amp;" in xml
    assert "&lt;" in xml
    assert "&gt;" in xml
    assert "&quot;" in xml
    rows = _find_rows(xml)
    assert len(rows) == 1


def test_null_values_omitted():
    xml = build_drillthrough_rowset(
        ["a", "b"],
        [{"a": "x", "b": None}],
    )
    rows = _find_rows(xml)
    assert len(rows) == 1
    assert rows[0].find("a").text == "x"
    assert rows[0].find("b") is None


def test_numeric_values_rendered_as_string():
    xml = build_drillthrough_rowset(
        ["count", "ratio"],
        [{"count": 42, "ratio": 3.14}],
    )
    rows = _find_rows(xml)
    assert rows[0].find("count").text == "42"
    assert rows[0].find("ratio").text == "3.14"


def test_schema_contains_column_definitions():
    xml = build_drillthrough_rowset(["month", "amount"], [])
    assert 'name="month"' in xml
    assert 'name="amount"' in xml
    # Built-in XSD types must be xs:-prefixed so the inline schema is valid and
    # Excel/MSOLAP accepts the rowset (Bug-5518); a bare type="string" resolves to
    # the rowset target namespace where it is undefined.
    assert 'type="xs:string"' in xml
    assert 'type="string"' not in xml


def test_parseable_xml():
    xml = build_drillthrough_rowset(
        ["region", "sales"],
        [{"region": "US", "sales": 500}],
    )
    full = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<tns:ExecuteResponse xmlns:tns="urn:schemas-microsoft-com:xml-analysis">'
        f'{xml}'
        f'</tns:ExecuteResponse>'
    )
    root = ET.fromstring(full)
    assert root is not None


def test_empty_string_value():
    xml = build_drillthrough_rowset(["name"], [{"name": ""}])
    rows = _find_rows(xml)
    assert len(rows) == 1
    assert rows[0].find("name").text is None or rows[0].find("name").text == ""


def test_column_order_preserved():
    rows_data = [{"c": 3, "a": 1, "b": 2}]
    xml = build_drillthrough_rowset(["a", "b", "c"], rows_data)
    rows = _find_rows(xml)
    children = list(rows[0])
    assert [c.tag for c in children] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# _augment_hierarchy_columns
# ---------------------------------------------------------------------------

def test_augment_no_hierarchy_path():
    cols, rows = _augment_hierarchy_columns(
        ["month", "amount"], [{"month": 1, "amount": 100}], [],
    )
    assert cols == ["month", "amount"]


def test_augment_adds_upper_levels():
    cols, rows = _augment_hierarchy_columns(
        ["month", "amount"],
        [{"month": 1, "amount": 100}, {"month": 2, "amount": 200}],
        [{"level_name": "Year", "dimension_name": "year", "value": 2025}],
    )
    assert cols == ["year", "month", "amount"]
    assert rows[0]["year"] == 2025
    assert rows[1]["year"] == 2025
    assert rows[0]["month"] == 1


def test_augment_skips_existing_column():
    cols, rows = _augment_hierarchy_columns(
        ["year", "amount"],
        [{"year": 2025, "amount": 100}],
        [{"level_name": "Year", "dimension_name": "year", "value": 2025}],
    )
    assert cols == ["year", "amount"]
    assert len(rows) == 1


def test_augment_multiple_ancestors():
    cols, rows = _augment_hierarchy_columns(
        ["day", "amount"],
        [{"day": 15, "amount": 50}],
        [
            {"level_name": "Year", "dimension_name": "year", "value": 2025},
            {"level_name": "Month", "dimension_name": "month", "value": 5},
        ],
    )
    assert cols == ["year", "month", "day", "amount"]
    assert rows[0]["year"] == 2025
    assert rows[0]["month"] == 5


# ---------------------------------------------------------------------------
# _extract_measure_name
# ---------------------------------------------------------------------------

def test_extract_measure_from_drillthrough():
    p = parse_mdx(
        "DRILLTHROUGH SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales]"
    )
    assert _extract_measure_name(p) == "Sales"


def test_extract_measure_case_insensitive():
    p = parse_mdx(
        "DRILLTHROUGH SELECT {[measures].[Amount]} ON COLUMNS FROM [demo]"
    )
    assert _extract_measure_name(p) == "Amount"


def test_extract_measure_none_when_no_columns():
    p = parse_mdx("DRILLTHROUGH SELECT FROM [demo]")
    assert _extract_measure_name(p) is None


# ---------------------------------------------------------------------------
# _parse_value
# ---------------------------------------------------------------------------

def test_parse_value_int():
    assert _parse_value("2025") == 2025


def test_parse_value_float():
    assert _parse_value("3.14") == 3.14


def test_parse_value_string():
    assert _parse_value("US") == "US"


# ---------------------------------------------------------------------------
# _xml_safe_name
# ---------------------------------------------------------------------------

def test_xml_safe_name_normal():
    assert _xml_safe_name("amount") == "amount"


def test_xml_safe_name_spaces():
    assert _xml_safe_name("my column") == "my_column"


def test_xml_safe_name_leading_digit():
    assert _xml_safe_name("1st_col") == "_1st_col"


def test_xml_safe_name_special_chars():
    assert _xml_safe_name("col@#$") == "col___"
