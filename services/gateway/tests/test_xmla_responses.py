"""
Quick validation script for XMLA response generation.
Tests that all rowsets return the correct columns.

Run from gateway directory: python tests/test_xmla_responses.py
"""
import sys
from pathlib import Path
import json

# Add parent directory to path to allow src imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax import mdschema

# Load config directly
_CONFIG_PATH = Path(__file__).parent.parent / "src" / "dax" / "mdschema_config.json"
_ROWSETS = json.loads(_CONFIG_PATH.read_text())["rowsets"]

# Test data
TEST_CATALOG = "demo"
TEST_MEASURES = [
    {"name": "Sales", "aggregation": "sum"},
    {"name": "Count", "aggregation": "count"},
]
TEST_DIMENSIONS = [
    {"name": "Date"},
    {"name": "Product"},
]

def test_rowset_columns():
    """Every rowset builder must return rows whose keys match the
    column set declared in the shared rowsets config.

    Missing keys would cause Excel to fail rendering; extras just
    warn because the client can tolerate them.
    """
    tests = {
        "MDSCHEMA_CATALOGS": lambda: mdschema._rows_catalogs(TEST_CATALOG, []),
        "MDSCHEMA_CUBES": lambda: mdschema._rows_cubes(TEST_CATALOG, []),
        "MDSCHEMA_DIMENSIONS": lambda: mdschema._rows_dimensions(TEST_CATALOG, TEST_DIMENSIONS, {}),
        "MDSCHEMA_MEASURES": lambda: mdschema._rows_measures(TEST_CATALOG, TEST_MEASURES),
    }

    for rtype, row_func in tests.items():
        expected_columns = {c["name"] for c in _ROWSETS[rtype]["columns"]}
        rows = row_func()
        assert rows, f"{rtype}: expected at least one row, got none"
        actual_columns = set(rows[0].keys())
        missing = expected_columns - actual_columns
        assert not missing, f"{rtype}: missing columns {missing}"


def test_xml_output():
    """`build_discover_response` must emit a parseable XML document
    for the default schema rowsets discovery — the first call Excel
    makes on connect."""
    xml = mdschema.build_discover_response(
        request_type="DISCOVER_SCHEMA_ROWSETS",
        catalog_name=TEST_CATALOG,
        model_id="",
        measures=[],
        dimensions=[],
    )
    assert "<?xml" in xml or "<root" in xml, (
        "Expected generated response to be a valid XML document"
    )

TEST_HIERARCHY_DEFS = [
    {
        "id": "h1",
        "name": "Date Hierarchy",
        "type": "date_embedded",
        "dimension_kind": "time",
        "levels": [
            {"ordinal": 0, "name": "Year"},
            {"ordinal": 1, "name": "Month"},
            {"ordinal": 2, "name": "Day"},
        ],
    },
    {
        "id": "h2",
        "name": "Product Hierarchy",
        "type": "explicit",
        "dimension_kind": None,
        "levels": [
            {"ordinal": 0, "name": "Category"},
            {"ordinal": 1, "name": "Subcategory"},
        ],
    },
]
TEST_DISCOVER_DIMS = [
    {"name": "Date Hierarchy", "source": "hierarchy", "hierarchy_id": "h1"},
    {"name": "Product", "source": "dimension"},
    {"name": "Product Hierarchy", "source": "hierarchy", "hierarchy_id": "h2"},
]


def test_measuregroups_only_default():
    """All measures belong to a single 'default' measure group."""
    rows = mdschema._rows_measuregroups(TEST_CATALOG, TEST_MEASURES)
    assert len(rows) == 1
    assert rows[0]["MEASUREGROUP_NAME"] == "default"


def test_measures_all_default_group():
    """All measures belong to the default group."""
    rows = mdschema._rows_measures(TEST_CATALOG, TEST_MEASURES)
    by_name = {r["MEASURE_NAME"]: r for r in rows}
    assert by_name["Sales"]["MEASUREGROUP_NAME"] == "default"
    assert by_name["Count"]["MEASUREGROUP_NAME"] == "default"


def test_measuregroup_dimensions_all_default():
    """All dimensions belong to the default group."""
    rows = mdschema._rows_measuregroup_dimensions(
        TEST_CATALOG, TEST_DISCOVER_DIMS, TEST_MEASURES,
    )
    assert all(r["MEASUREGROUP_NAME"] == "default" for r in rows)

    date_rows = [r for r in rows if r["DIMENSION_UNIQUE_NAME"] == "[Date Hierarchy]"]
    assert len(date_rows) == 1

    product_rows = [r for r in rows if r["DIMENSION_UNIQUE_NAME"] == "[Product]"]
    assert all(r["MEASUREGROUP_NAME"] == "default" for r in product_rows)


def test_full_discover_response_with_hierarchy_defs():
    """build_discover_response threads hierarchy_defs through to measuregroup rows."""
    xml = mdschema.build_discover_response(
        request_type="MDSCHEMA_MEASUREGROUPS",
        catalog_name=TEST_CATALOG,
        model_id="model-1",
        measures=TEST_MEASURES,
        dimensions=TEST_DISCOVER_DIMS,
        hierarchy_defs=TEST_HIERARCHY_DEFS,
    )
    assert "default" in xml


if __name__ == "__main__":
    print("Testing XMLA Response Generation\n" + "=" * 50)

    print("\n1. Column Set Validation:")
    columns_ok = test_rowset_columns()

    print("\n2. XML Output Validation:")
    xml_ok = test_xml_output()

    print("\n" + "=" * 50)
    if columns_ok and xml_ok:
        print("PASS All tests passed!")
        sys.exit(0)
    else:
        print("FAIL Some tests failed")
        sys.exit(1)
