"""Bug-9771 — invariants for the INTERNAL <-> WIRE hierarchy-name bridge.

XMLA requires a hierarchy's unique name to be prefixed by the unique name of the
dimension that owns it. The field-list grouping (Bug-6603/6891) put standalone
attributes under a shared ``[Dimensions]`` node while leaving hierarchy names as
``[account_type].[account_type]`` — declaring an owning dimension that does not
exist in MDSCHEMA_DIMENSIONS. Excel could not resolve the owner and labelled
every grouped field ``(All)`` in the PivotTable Rows drop zone.

The fix advertises the conformant WIRE name and translates at two seams rather
than teaching ten bracket-parsing sites in ``mdx_execute`` the grouped grammar.
These tests pin the properties that make that bridge safe; they are the guard
the external review asked for.
"""

import pytest

from src.dax.cube_model import (
    HIERARCHY_GROUP_UNIQUE_NAME,
    TIME_GROUP_UNIQUE_NAME,
    STANDALONE_GROUP_UNIQUE_NAME,
    build_cube_dimensions,
    dimension_unique_name_for,
    hierarchy_unique_name_for,
    internal_hierarchy_unique_name_for,
)
from src.dax.mdx_execute import _to_wire, wire_hierarchy_map
from src.dax import xmla_server


def _attr(name):
    return {"name": name, "source_column_id": f"col-{name}"}


def _hier(name, levels=("Year", "Month")):
    return {
        "id": f"h-{name}",
        "name": name,
        "dimension_kind": "time",
        "levels": [
            {"ordinal": i, "name": lv, "time_unit": lv.lower()}
            for i, lv in enumerate(levels)
        ],
    }


class TestGrammarConformance:
    """The property whose violation caused the bug."""

    @pytest.mark.parametrize("dims,hiers", [
        ([_attr("account_type"), _attr("channel_name")], []),
        ([], [_hier("Order Calendar")]),
        ([_attr("account_type")], [_hier("Order Calendar")]),
        ([{"name": "business_date", "is_time_dim": True, "time_grain": "day"}], []),
    ])
    def test_hierarchy_name_is_always_prefixed_by_its_dimension(self, dims, hiers):
        for d in build_cube_dimensions(dims, hiers):
            dim_uname = dimension_unique_name_for(d)
            hier_uname = hierarchy_unique_name_for(d)
            assert hier_uname.startswith(dim_uname + "."), (
                f"{hier_uname!r} does not live under {dim_uname!r} — Excel cannot "
                "resolve the owning dimension and falls back to the (All) caption"
            )

    def test_grouped_fields_use_their_group_node(self):
        cube = build_cube_dimensions([_attr("account_type")], [_hier("Order Calendar")])
        by_name = {d["name"]: d for d in cube}
        assert hierarchy_unique_name_for(by_name["account_type"]) == \
            f"{STANDALONE_GROUP_UNIQUE_NAME}.[account_type]"
        # Bug-9878: a calendar (time) hierarchy lives under the [Time] node.
        assert hierarchy_unique_name_for(by_name["Order Calendar"]) == \
            f"{TIME_GROUP_UNIQUE_NAME}.[Order Calendar]"
        non_time = build_cube_dimensions([], [dict(_hier("Geography"), dimension_kind="geo")])
        assert hierarchy_unique_name_for(non_time[0]) == \
            f"{HIERARCHY_GROUP_UNIQUE_NAME}.[Geography]"

    def test_flat_time_dimension_lives_under_the_time_node(self):
        """Bug-9878: a flat time attribute joins the calendar hierarchies under
        the one time-typed [Time] node; its internal name is unchanged and the
        wire name is derived from the group node like every other field."""
        cube = build_cube_dimensions(
            [{"name": "business_date", "is_time_dim": True, "time_grain": "day"}], [],
        )
        d = cube[0]
        assert internal_hierarchy_unique_name_for(d) == "[business_date].[business_date]"
        assert hierarchy_unique_name_for(d) == f"{TIME_GROUP_UNIQUE_NAME}.[business_date]"


class TestRoundTrip:
    """to_internal(to_wire(x)) == x, and both directions idempotent."""

    DIMS = [_attr("account_type"), _attr("account_type_name"), _attr("region")]
    HIERS = [_hier("Order Calendar")]

    def _map(self):
        return wire_hierarchy_map(self.DIMS, self.HIERS)

    def test_round_trip_is_lossless(self):
        hier_map = self._map()
        for internal in hier_map:
            wire = _to_wire(internal, hier_map)
            assert xmla_server._normalize_wire_mdx(
                wire, self.DIMS, self.HIERS,
            ) == internal

    def test_round_trip_preserves_level_and_member_suffixes(self):
        hier_map = self._map()
        for suffix in ("", ".[All]", ".[(All)]", ".[account_type]", ".&[EMEA]&[UK]"):
            internal = "[account_type].[account_type]" + suffix
            wire = _to_wire(internal, hier_map)
            assert wire == "[Dimensions].[account_type]" + suffix
            assert xmla_server._normalize_wire_mdx(
                wire, self.DIMS, self.HIERS,
            ) == internal

    def test_both_directions_are_idempotent(self):
        hier_map = self._map()
        wire = _to_wire("[account_type].[account_type].[All]", hier_map)
        assert _to_wire(wire, hier_map) == wire
        once = xmla_server._normalize_wire_mdx(wire, self.DIMS, self.HIERS)
        assert xmla_server._normalize_wire_mdx(once, self.DIMS, self.HIERS) == once

    def test_every_wire_name_is_unique(self):
        cube = build_cube_dimensions(self.DIMS, self.HIERS)
        names = [hierarchy_unique_name_for(d) for d in cube]
        assert len(names) == len(set(names))

    def test_prefix_collision_between_field_names_does_not_bleed(self):
        """`account_type` is a strict prefix of `account_type_name`. Translating
        one must never partially rewrite the other."""
        hier_map = self._map()
        wire = _to_wire("[account_type_name].[account_type_name].[All]", hier_map)
        assert wire == "[Dimensions].[account_type_name].[All]"
        assert xmla_server._normalize_wire_mdx(wire, self.DIMS, self.HIERS) == \
            "[account_type_name].[account_type_name].[All]"


class TestFailsLoud:
    """A naming failure must fault, never degrade to a silently wrong response."""

    def test_duplicate_wire_identities_cannot_occur_for_a_real_model(self):
        """The duplicate-identity guard in ``wire_hierarchy_map`` is DEFENSIVE
        and currently unreachable, which is worth pinning rather than pretending
        otherwise: ``build_cube_dimensions`` already dedupes by name, and both
        the wire and internal names are pure functions of (group, name) — so two
        entries sharing a wire name necessarily share an internal name too and
        are not a collision. This test pins the reachable property (uniqueness
        for real models); the guard stays as cheap insurance in case the naming
        functions ever stop being name-derived.
        """
        cube = build_cube_dimensions(
            [_attr("dup"), _attr("dup"), _attr("other")], [_hier("Cal")],
        )
        names = [hierarchy_unique_name_for(d) for d in cube]
        assert len(names) == len(set(names))

    def test_map_does_not_swallow_errors(self):
        """The pre-review implementation caught Exception and returned {}, which
        would emit INTERNAL names after DISCOVER had advertised WIRE ones —
        reproducing this bug intermittently, with a 200 and no fault."""
        with pytest.raises(Exception):
            wire_hierarchy_map("not-a-list", None)


class TestInboundNormalization:
    DIMS = [_attr("account_type")]
    HIERS = []

    def test_dax_statements_are_never_rewritten(self):
        """DAX does not use the MDX hierarchy grammar; rewriting matching text
        inside a DAX expression could alter a RESULT VALUE, not an identity."""
        for stmt in (
            "EVALUATE FILTER(T, T[x] = \"[Dimensions].[account_type]\")",
            "DEFINE MEASURE T[m] = 1 EVALUATE T",
        ):
            assert xmla_server._normalize_wire_mdx(
                stmt, self.DIMS, self.HIERS,
            ) == stmt

    def test_unknown_hierarchies_are_left_untouched(self):
        """Only names the MODEL actually maps are translated. A metadata-free
        rewrite would happily mangle a field that no longer exists, or a genuine
        field named `Dimensions`."""
        stmt = "SELECT {[Dimensions].[not_a_real_field].Members} ON ROWS FROM [m]"
        assert xmla_server._normalize_wire_mdx(stmt, self.DIMS, self.HIERS) == stmt

    def test_a_real_field_named_dimensions_is_not_corrupted(self):
        """`[Dimensions].[Dimensions]` is the legitimate wire name of a field
        genuinely called `Dimensions`; it must round-trip, not be mangled."""
        dims = [_attr("Dimensions")]
        cube = build_cube_dimensions(dims, [])
        wire = hierarchy_unique_name_for(cube[0])
        assert wire == "[Dimensions].[Dimensions]"
        assert xmla_server._normalize_wire_mdx(wire, dims, []) == \
            internal_hierarchy_unique_name_for(cube[0])

    def test_empty_and_bracketless_input_is_returned_unchanged(self):
        for stmt in ("", "SELECT 1", None):
            assert xmla_server._normalize_wire_mdx(
                stmt, self.DIMS, self.HIERS,
            ) == stmt
