"""Bug-9788 — the field-list grouping toggle must move BOTH sides together.

Saving a workbook containing a PivotTable dimension corrupts the file. The
regression window was established from an artefact in this repo:
``tessallite-website/assets/downloads/tessallite-tpcds-live-dashboard.xlsx``
saved correctly on 2026-06-28 and records ONE DIMENSION PER HIERARCHY
(``dimensionUniqueName="[customer_segment]"``); the grouping that put 72
hierarchies under a shared ``[Dimensions]`` landed 2026-07-07, nine days later.

The toggle exists to test that ONE structural variable. These tests pin the
property that makes the test meaningful, which the first version of the toggle
broke: ``dimension_unique_name_for`` honoured the flag while ``_rows_dimensions``
did not, so the deployed cube advertised ``[Dimensions]`` in MDSCHEMA_DIMENSIONS
while every hierarchy claimed ``[account_type]`` — a DANGLING reference in every
row, and a worse cube than either mode. A half-applied toggle does not test the
hypothesis, it tests a third thing that never ships.
"""

import importlib
import os

import pytest

DIMS = [
    {"name": "account_type", "source_column_id": "c1"},
    {"name": "channel_name", "source_column_id": "c2"},
    {"name": "business_date", "is_time_dim": True, "time_grain": "day"},
]
HIERS = [{
    "id": "h1", "name": "Cal", "dimension_kind": "time",
    "levels": [{"ordinal": 0, "name": "Year", "time_unit": "year"},
               {"ordinal": 1, "name": "Month", "time_unit": "month"}],
}]


@pytest.fixture
def cube_for(monkeypatch):
    def _build(grouping):
        monkeypatch.setenv("TESSALLITE_XMLA_FIELD_LIST_GROUPING", grouping)
        import src.dax.cube_model as cm
        import src.dax.mdschema as ms
        importlib.reload(cm)
        importlib.reload(ms)
        cube = cm.build_cube_dimensions(DIMS, HIERS)
        return (cm, ms, cube,
                ms._rows_dimensions("m", cube, {}),
                ms._rows_hierarchies("m", cube, [], {}, {}))
    return _build


@pytest.fixture(autouse=True)
def _restore_default():
    yield
    os.environ.pop("TESSALLITE_XMLA_FIELD_LIST_GROUPING", None)
    import src.dax.cube_model as cm
    import src.dax.mdschema as ms
    importlib.reload(cm)
    importlib.reload(ms)


@pytest.mark.parametrize("grouping", ["true", "false"])
def test_no_hierarchy_references_a_dimension_that_does_not_exist(cube_for, grouping):
    """THE invariant. A hierarchy naming a DIMENSION_UNIQUE_NAME absent from
    MDSCHEMA_DIMENSIONS is a dangling reference in the cube a client caches."""
    _, _, _, dim_rows, hier_rows = cube_for(grouping)
    declared = {r["DIMENSION_UNIQUE_NAME"] for r in dim_rows}
    dangling = [
        r["HIERARCHY_UNIQUE_NAME"] for r in hier_rows
        if r.get("DIMENSION_UNIQUE_NAME")
        and r["DIMENSION_UNIQUE_NAME"] not in declared
    ]
    assert not dangling, (
        f"grouping={grouping}: {len(dangling)} hierarchies reference a dimension "
        f"absent from MDSCHEMA_DIMENSIONS, e.g. {dangling[:3]}"
    )


def test_grouping_on_emits_the_group_nodes(cube_for):
    _, _, _, dim_rows, _ = cube_for("true")
    declared = {r["DIMENSION_UNIQUE_NAME"] for r in dim_rows}
    assert "[Dimensions]" in declared
    # Bug-9878: time hierarchies sit under the time-typed [Time] node.
    assert "[Time]" in declared
    time_row = next(r for r in dim_rows if r["DIMENSION_UNIQUE_NAME"] == "[Time]")
    assert time_row["DIMENSION_TYPE"] == "1"


def test_grouping_off_emits_one_dimension_per_hierarchy(cube_for):
    """The shape the known-good 28-June workbook recorded."""
    _, _, _, dim_rows, hier_rows = cube_for("false")
    declared = {r["DIMENSION_UNIQUE_NAME"] for r in dim_rows}
    assert "[Dimensions]" not in declared
    assert "[Hierarchies]" not in declared
    assert "[Time]" not in declared
    assert "[account_type]" in declared
    for r in hier_rows:
        if r.get("DIMENSION_UNIQUE_NAME") and r["DIMENSION_UNIQUE_NAME"] != "[Measures]":
            assert r["HIERARCHY_UNIQUE_NAME"].startswith(
                r["DIMENSION_UNIQUE_NAME"] + ".")


@pytest.mark.parametrize("grouping", ["true", "false"])
def test_the_same_hierarchies_exist_in_both_modes(cube_for, grouping):
    """The toggle must change GROUPING only — never which fields exist. A mode
    that drops or adds a field is not a controlled experiment."""
    _, _, _, _, hier_rows = cube_for(grouping)
    names = {r["HIERARCHY_NAME"] for r in hier_rows if r.get("HIERARCHY_NAME")}
    assert {"account_type", "channel_name", "business_date", "Cal"} <= names


def test_the_default_is_the_shipped_grouped_behaviour(monkeypatch):
    """Unset must not silently change the cube shape clients already cached."""
    monkeypatch.delenv("TESSALLITE_XMLA_FIELD_LIST_GROUPING", raising=False)
    import src.dax.cube_model as cm
    importlib.reload(cm)
    assert cm.field_list_grouping_enabled() is True
