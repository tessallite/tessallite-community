"""Bug-9847: a measure's advertised DATA_TYPE must match the cells it returns.

`MDSCHEMA_MEASURES` and `DBSCHEMA_COLUMNS` advertised `DATA_TYPE 5`
(DBTYPE_R8) for every measure, including the three synthetic Info measures
whose Execute cells are `<Value xsi:type="xsd:string">`. Excel allocates its
PivotTable cache column from the advertised type, so it was told to expect a
number and handed text.

The wire type is declared per measure as `xmla_data_type` -- deliberately NOT
the measure's `data_type`, which is its SOURCE COLUMN's type (a count over a
text column is a text column and a numeric cell).
"""

import re

import pytest

from src.dax.mdschema import build_discover_response
from src.dax.xmla_server import _build_trust_info_measures

CATALOG = "acme-demo__project1__modely"

REAL_MEASURE = {
    "name": "base_amount",
    "display_name": "Base Amount",
    "default_agg": "sum",
    # The source column's type. It must NOT reach the advertised wire type.
    "data_type": "numeric",
    "is_hidden": False,
}
TEXT_SOURCED_MEASURE = {
    "name": "transaction_count",
    "display_name": "Transactions",
    "default_agg": "count",
    "data_type": "text",
    "is_hidden": False,
}


def _rowset(request_type, measures):
    return build_discover_response(
        request_type, CATALOG, "m1", measures, [],
        properties={"SspropInitAppName": "Microsoft Office Excel"},
    )


def _typed(xml, name_tag):
    """`{column/measure name: DATA_TYPE}` from a rendered rowset."""
    out = {}
    for row in re.findall(r"<row>(.*?)</row>", xml, re.S):
        name = re.search(rf"<{name_tag}>([^<]*)</{name_tag}>", row)
        dtype = re.search(r"<DATA_TYPE>([^<]*)</DATA_TYPE>", row)
        if name and dtype:
            out[name.group(1)] = dtype.group(1)
    return out


INFO_MEASURE_NAMES = ("_info_last_refreshed", "_info_source_system", "_info_owner")


def test_info_measures_declare_a_string_wire_type():
    """The producer's own declaration -- the consumers below read only this."""
    declared = {m["name"]: m.get("xmla_data_type") for m in _build_trust_info_measures()}
    assert set(declared) == set(INFO_MEASURE_NAMES)
    assert set(declared.values()) == {"string"}


@pytest.mark.parametrize(
    "request_type,name_tag",
    [("MDSCHEMA_MEASURES", "MEASURE_NAME"), ("DBSCHEMA_COLUMNS", "COLUMN_NAME")],
)
def test_info_measures_advertise_dbtype_wstr(request_type, name_tag):
    measures = [REAL_MEASURE] + _build_trust_info_measures()
    types = _typed(_rowset(request_type, measures), name_tag)
    for info_name in INFO_MEASURE_NAMES:
        assert types[info_name] == "130", (
            f"{request_type} advertises {info_name} as DATA_TYPE "
            f"{types[info_name]}; its Execute cells are xsd:string"
        )


@pytest.mark.parametrize(
    "request_type,name_tag",
    [("MDSCHEMA_MEASURES", "MEASURE_NAME"), ("DBSCHEMA_COLUMNS", "COLUMN_NAME")],
)
def test_a_real_measure_still_advertises_dbtype_r8(request_type, name_tag):
    """Including one whose SOURCE column is text -- its cells are still numbers."""
    measures = [REAL_MEASURE, TEXT_SOURCED_MEASURE]
    types = _typed(_rowset(request_type, measures), name_tag)
    assert types["base_amount"] == "5"
    assert types["transaction_count"] == "5"


def test_the_two_rowsets_agree_on_every_measure():
    """One measure, one advertised type: the rowsets must not disagree."""
    measures = [REAL_MEASURE, TEXT_SOURCED_MEASURE] + _build_trust_info_measures()
    from_measures = _typed(_rowset("MDSCHEMA_MEASURES", measures), "MEASURE_NAME")
    from_columns = _typed(_rowset("DBSCHEMA_COLUMNS", measures), "COLUMN_NAME")
    for measure in measures:
        name = measure["name"]
        assert from_measures[name] == from_columns[name]


def test_string_measures_carry_no_numeric_precision():
    """A DBTYPE_WSTR column must not claim a numeric precision and scale."""
    xml = _rowset("MDSCHEMA_MEASURES", _build_trust_info_measures())
    for row in re.findall(r"<row>(.*?)</row>", xml, re.S):
        if "<DATA_TYPE>130</DATA_TYPE>" not in row:
            continue
        precision = re.search(r"<NUMERIC_PRECISION>([^<]*)</NUMERIC_PRECISION>", row)
        assert precision is None or precision.group(1) == ""


def test_string_measure_rows_never_emit_an_empty_numeric_element():
    """Bug-9847 follow-up (ALEX ladder 2026-09-05): a string-typed measure row
    carried ``NUMERIC_PRECISION`` as an EMPTY element and MSOLAP refused the
    whole MDSCHEMA_MEASURES rowset ("error while parsing the 'NUMERIC_PRECISION'
    element"), so every PivotTable creation failed. The optional element must
    be omitted, and the serialiser must never emit an empty cell for a
    non-string XSD type from any producer."""
    from src.dax import mdschema as ms

    measures = [
        {"name": "base_amount", "default_agg": "sum"},
        {"name": "_info_owner", "default_agg": "sum", "xmla_data_type": "string"},
    ]
    rows = ms._rows_measures("m", measures)
    string_row = next(r for r in rows if r["MEASURE_NAME"] == "_info_owner")
    assert string_row["DATA_TYPE"] == "130"
    assert string_row.get("NUMERIC_PRECISION") is None
    assert string_row.get("NUMERIC_SCALE") is None

    col_defs = [
        {"name": "MEASURE_NAME", "type": "string", "required": True},
        {"name": "NUMERIC_PRECISION", "type": "unsignedShort"},
        {"name": "NUMERIC_SCALE", "type": "short"},
    ]
    xml = ms._build_rowset_xml(col_defs, [{"MEASURE_NAME": "x", "NUMERIC_PRECISION": "", "NUMERIC_SCALE": ""}])
    assert "<NUMERIC_PRECISION>" not in xml and "<NUMERIC_SCALE>" not in xml
    xml = ms._build_rowset_xml(col_defs, [{"MEASURE_NAME": "x", "NUMERIC_PRECISION": "16"}])
    assert "<NUMERIC_PRECISION>16</NUMERIC_PRECISION>" in xml
