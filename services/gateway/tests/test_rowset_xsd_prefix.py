"""Bug-5518: Discover-rowset inline XSD must xs:-prefix built-in types.

A bare ``type="string"`` resolves (via the rowset default namespace) to an
undefined type, making the inline schema invalid. Excel/MSOLAP validates the
rowset schema strictly and rejects it ("cannot retrieve list of databases"),
while lenient clients (curl, JDBC) tolerate it. Custom rowset types
(uuid/row/xmlDocument) must stay unprefixed.
"""
import re
import pytest
from src.dax.mdschema import build_discover_response, _qualify_xsd_type

_BUILTINS = ["string", "int", "dateTime", "unsignedLong", "boolean", "double"]


@pytest.mark.parametrize("rtype", ["DBSCHEMA_CATALOGS", "MDSCHEMA_CUBES", "MDSCHEMA_DIMENSIONS"])
def test_discover_schema_prefixes_builtin_types(rtype):
    xml = build_discover_response(
        rtype, "tpcds_retail", "m1",
        measures=[{"name": "net_sales"}], dimensions=[{"name": "year"}],
        tenant_models=[{"slug": "tpcds_retail", "display_name": "TPC-DS"}],
    )
    # Isolate the inline <xs:schema> ... </xs:schema> block.
    m = re.search(r"<xs:schema\b.*?</xs:schema>", xml, re.S)
    assert m, f"{rtype}: no inline schema emitted"
    schema = m.group(0)
    # No bare built-in type/base references remain.
    for t in _BUILTINS:
        assert f'type="{t}"' not in schema, f'{rtype}: bare type="{t}" leaks'
        assert f'base="{t}"' not in schema, f'{rtype}: bare base="{t}" leaks'
    # The custom uuid/row types stay unprefixed (resolve to the rowset namespace).
    assert 'type="row"' in schema
    assert 'name="uuid"' in schema


def test_qualify_helper():
    assert _qualify_xsd_type("string") == "xs:string"
    assert _qualify_xsd_type("dateTime") == "xs:dateTime"
    assert _qualify_xsd_type("uuid") == "uuid"  # custom — unchanged
    assert _qualify_xsd_type("row") == "row"
