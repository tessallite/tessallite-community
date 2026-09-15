"""Bug-9891 -- a hierarchy LEVEL set on one axis dropped every subtotal on the
other axis.

Reported from Excel 365 against the demo: Geography Channel on columns,
expanded to City, and three flat fields on rows (channel > device > account
type). Every middle-level group header rendered blank and the row subtotals
came and went with the field order.

Excel sends the expanded hierarchy as ``[H].[H].[City].Members`` and the flat
fields as ``CrossJoin(DrilldownLevel({[a].[a].[All]}), ...)``. No rollup
detector claimed the level set, so the Bug-9785 coverage guard saw an
uncovered ``city_name`` and failed safe by dropping ALL rollups -- the three
flat-attribute rollups on the other axis included. Only leaf tuples were
served: 210 instead of 336 for the reported shape.

The guard: a level set is registered leaf-only. The coverage invariant holds,
the grain builders serve exactly the named level for it, and the other axis
keeps its full lattice. A level set alone (no rollups anywhere) is never
registered -- the plain path renders it unchanged.
"""

from __future__ import annotations

from src.dax.subtotal_engine import (
    SubtotalHierarchy,
    SubtotalLevel,
    build_multi_subtotal_queries,
    build_subtotal_queries,
    detect_level_set_hierarchies,
    uncovered_axis_dimensions,
)

_GEO_META = [{
    "name": "Geography Channel",
    "levels": [
        {"name": "(All)", "ordinal": 0},
        {"name": "Country", "ordinal": 1},
        {"name": "City", "ordinal": 2},
        {"name": "Channel", "ordinal": 3},
    ],
}]
_GEO_LEVEL_MAP = {"geography channel": {
    "country": "country_code", "city": "city_name", "channel": "channel_name",
}}
_COL = ("Hierarchize(AddCalculatedMembers("
        "{[Geography Channel].[Geography Channel].[City].Members}))")
_ROW = ("CrossJoin(CrossJoin("
        "Hierarchize(AddCalculatedMembers({DrilldownLevel({[channel_name].[channel_name].[All]})})), "
        "Hierarchize(AddCalculatedMembers({DrilldownLevel({[device_type].[device_type].[All]})}))), "
        "Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type].[account_type].[All]})})))")


def _flat(name: str) -> SubtotalHierarchy:
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=1, dim_name=name)],
        axis=1, is_flat_attribute_rollup=True,
    )


def test_level_set_is_registered_leaf_only_down_to_the_named_level() -> None:
    found = detect_level_set_hierarchies(_COL, _ROW, _GEO_META, _GEO_LEVEL_MAP, registered=set())
    assert len(found) == 1
    geo = found[0]
    assert geo.hierarchy_name == "Geography Channel"
    assert geo.axis == 0
    assert geo.leaf_only and not geo.include_all
    # Country is carried for parent assignment; Channel (below City) is not served.
    assert [lvl.dim_name for lvl in geo.levels] == ["country_code", "city_name"]


def test_level_set_covers_the_axis_so_the_flat_rollups_survive() -> None:
    """The reported failure: with only the flat rollups, city_name was
    uncovered and the guard dropped everything."""
    flats = [_flat("channel_name"), _flat("device_type"), _flat("account_type")]
    axis_dims = {"city_name", "channel_name", "device_type", "account_type"}
    assert uncovered_axis_dimensions(axis_dims, flats) == {"city_name"}
    geo = detect_level_set_hierarchies(_COL, _ROW, _GEO_META, _GEO_LEVEL_MAP, registered=set())
    assert uncovered_axis_dimensions(axis_dims, flats + geo) == set()


def test_leaf_only_hierarchy_adds_no_grain_of_its_own() -> None:
    geo = detect_level_set_hierarchies(_COL, _ROW, _GEO_META, _GEO_LEVEL_MAP, registered=set())[0]
    flats = [_flat("channel_name"), _flat("device_type"), _flat("account_type")]
    common = dict(
        mdx_dims=["country_code", "city_name", "channel_name", "device_type", "account_type"],
        mdx_measures=["base_amount"], where_sql_clauses=[], model_slug="m",
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        measure_canonical={},
    )
    # Alone it serves nothing above City.
    assert build_subtotal_queries(hierarchy=geo, **common) == []
    # Beside the three flat rollups the lattice is the flat fields' 2^3 - 1
    # combinations, each still grouped by BOTH geo columns (City grain only).
    queries = build_multi_subtotal_queries(hierarchies=flats + [geo], **common)
    assert len(queries) == 7
    for q in queries:
        assert "country_code" in q.dim_cols and "city_name" in q.dim_cols
        assert q.grain_per_hierarchy["Geography Channel"] == 2
    assert {q.level_name for q in queries} >= {"Grand Total"} or any(
        q.grain_per_hierarchy["channel_name"] == -1 for q in queries
    )


def test_already_registered_undefined_and_all_level_sets_are_ignored() -> None:
    assert detect_level_set_hierarchies(
        _COL, _ROW, _GEO_META, _GEO_LEVEL_MAP, registered={"Geography Channel"},
    ) == []
    assert detect_level_set_hierarchies(
        "{[Calendar].[Calendar].[Year].Members}", "", _GEO_META, _GEO_LEVEL_MAP, registered=set(),
    ) == []
    assert detect_level_set_hierarchies(
        "{[Geography Channel].[Geography Channel].[(All)].Members}", "",
        _GEO_META, _GEO_LEVEL_MAP, registered=set(),
    ) == []
