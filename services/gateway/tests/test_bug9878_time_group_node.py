"""Bug-9878 -- one time-typed [Time] node for every date field (owner option D).

Before: each visible flat time attribute kept a top-level dimension node of its
own (five stray peers of [Dimensions] and [Hierarchies] in Excel's field list)
while the calendar hierarchies built over the same columns sat under
[Hierarchies]. Now both live under ONE DIMENSION_TYPE 1 node, [Time] -- the SSAS
date-dimension shape -- so Excel's Timeline filter still finds a time-typed
dimension and the field list reads Measures / KPIs / Dimensions / Hierarchies /
Time. Non-time user hierarchies stay under [Hierarchies].
"""

from src.dax import mdschema as ms
from src.dax.cube_model import (
    HIERARCHY_GROUP_UNIQUE_NAME,
    STANDALONE_GROUP_UNIQUE_NAME,
    TIME_GROUP_UNIQUE_NAME,
    build_cube_dimensions,
    dimension_unique_name_for,
    hierarchy_unique_name_for,
    is_grouped_hierarchy,
    is_standalone_attribute,
    is_time_group_member,
)

_DIMS = [
    {"name": "account_type"},
    {"name": "business_date", "is_time_dim": True, "time_grain": "day"},
]
_HIERS = [
    {"id": "h-cal", "name": "business_date Calendar", "dimension_kind": "time",
     "levels": [{"ordinal": i, "name": n, "time_unit": n.lower()} for i, n in enumerate(("Year", "Month", "Day"))]},
    {"id": "h-geo", "name": "Geography Channel", "dimension_kind": "geo",
     "levels": [{"ordinal": i, "name": n} for i, n in enumerate(("Country", "City", "Channel"))]},
]


def _cube():
    return {d["name"]: d for d in build_cube_dimensions(_DIMS, _HIERS)}


def test_every_time_field_resolves_to_the_time_node():
    cube = _cube()
    assert is_time_group_member(cube["business_date"])
    assert is_time_group_member(cube["business_date Calendar"])
    assert dimension_unique_name_for(cube["business_date"]) == TIME_GROUP_UNIQUE_NAME
    assert dimension_unique_name_for(cube["business_date Calendar"]) == TIME_GROUP_UNIQUE_NAME
    assert hierarchy_unique_name_for(cube["business_date"]) == "[Time].[business_date]"
    assert hierarchy_unique_name_for(cube["business_date Calendar"]) == "[Time].[business_date Calendar]"


def test_non_time_fields_keep_their_groups():
    cube = _cube()
    assert is_standalone_attribute(cube["account_type"])
    assert dimension_unique_name_for(cube["account_type"]) == STANDALONE_GROUP_UNIQUE_NAME
    assert is_grouped_hierarchy(cube["Geography Channel"])
    assert not is_time_group_member(cube["Geography Channel"])
    assert dimension_unique_name_for(cube["Geography Channel"]) == HIERARCHY_GROUP_UNIQUE_NAME


def test_discover_emits_one_typed_time_node_and_no_stray_date_nodes():
    cube = build_cube_dimensions(_DIMS, _HIERS)
    drows = ms._rows_dimensions("m", cube, {})
    by_uname = {r["DIMENSION_UNIQUE_NAME"]: r for r in drows}
    assert set(by_uname) == {"[Dimensions]", "[Time]", "[Hierarchies]", "[Measures]"}
    assert by_uname["[Time]"]["DIMENSION_TYPE"] == "1"
    assert by_uname["[Time]"]["DIMENSION_CARDINALITY"] == "2"
    assert by_uname["[Hierarchies]"]["DIMENSION_TYPE"] == "3"
    hrows = ms._rows_hierarchies("m", cube, [{"name": "base_amount"}], {}, {})
    owner = {r["HIERARCHY_NAME"]: r["DIMENSION_UNIQUE_NAME"] for r in hrows if r.get("HIERARCHY_NAME")}
    assert owner["business_date"] == "[Time]"
    assert owner["business_date Calendar"] == "[Time]"
    assert owner["Geography Channel"] == "[Hierarchies]"
    assert owner["account_type"] == "[Dimensions]"
    # Every hierarchy's owner is a declared dimension (the Bug-9771 grammar).
    assert set(owner.values()) <= set(by_uname)
