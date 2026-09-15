"""Bug-9902 -- a bare flat-attribute member set dropped every rollup in the query.

The Bug-9891 class in its second spelling. Excel places a standalone attribute
with subtotals OFF (or simply fully listed) as that attribute's own ``.Members``
set. ``detect_flat_attribute_rollups`` deliberately refuses to claim a lone flat
member set -- it is also how Excel populates an ordinary single-field pivot, and
claiming it would fabricate an All row nobody asked for -- so it claims one only
when the SAME axis carries a ``DrilldownLevel`` or a second attribute. That
leaves two live shapes unclaimed:

* the set on the OTHER axis from the rollup, and
* the grouped wire spelling ``[Dimensions].[a].[a].Members``, which
  ``_normalize_wire_mdx`` turns into the three-part ``[a].[a].[a].Members`` that
  ``_SUBTOTAL_MEMBERS_RE`` (two-part, or ``.[(All)]``-qualified) never matches.

The Bug-9785 coverage guard then saw an uncovered dimension and dropped EVERY
rollup. Since Bug-9862 F6 that is a SOAP fault rather than a silent display gap:
``Axis1 omitted a requested rollup grain: channel_name=(All) ...``.

The guard: the set is registered leaf-only -- it covers the axis, contributes no
grain of its own, and the other field keeps its full lattice. A flat member set
ALONE is never registered; the plain path renders it unchanged.

Verified to fail on pre-fix code. ``detect_flat_member_set_rollups`` does not
exist at the lane's base SHA (main a9970914b), so this file fails at import
there; and the live shape (a) against that SHA raises the Bug-9862 fault above
instead of returning the lattice.
"""

from __future__ import annotations

import pytest

from src.dax.rollup_validator import RollupValidationError, validate_rollup_lattice
from src.dax.subtotal_engine import (
    SUBTOTAL_GRAIN_PREFIX,
    SubtotalHierarchy,
    SubtotalLevel,
    build_multi_subtotal_queries,
    detect_flat_attribute_rollups,
    detect_flat_member_set_rollups,
    detect_subtotal_hierarchies,
    uncovered_axis_dimensions,
)

_ATTRS = {"channel_name", "device_type"}

_DRILL = ("Hierarchize(AddCalculatedMembers("
          "{DrilldownLevel({[channel_name].[channel_name].[All]})}))")
# The ungrouped spelling: Excel's plain two-part member set.
_FLAT_SET = "Hierarchize(AddCalculatedMembers({[device_type].[device_type].Members}))"
# The grouped spelling after _normalize_wire_mdx has swapped [Dimensions].[x]
# for the internal [x].[x] prefix -- the shape reported live against modely.
_FLAT_SET_WIRE = (
    "Hierarchize(AddCalculatedMembers("
    "{[device_type].[device_type].[device_type].Members}))")

_AXIS_DIMS = {"channel_name", "device_type"}


def _flat(name: str, axis: int = 1) -> SubtotalHierarchy:
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=0, dim_name=name)],
        axis=axis, is_flat_attribute_rollup=True,
    )


def _member(dim: str, value: str) -> dict:
    hier = f"[{dim}].[{dim}]"
    return {
        "hierarchy": hier, "uname": f"{hier}.[{value}]",
        "name": value, "key": value, "value": value, "caption": value,
        "lname": f"{hier}.[{dim}]", "lnum": "1", "parent": f"{hier}.[All]",
        "has_children": False, "member_type": 1, "member_ordinal": 0,
        "children_cardinality": 0,
    }


def _all_member(dim: str) -> dict:
    hier = f"[{dim}].[{dim}]"
    return {
        "hierarchy": hier, "uname": f"{hier}.[All]",
        "name": "All", "key": "All", "value": "All", "caption": "All",
        "lname": f"{hier}.[(All)]", "lnum": "0", "parent": None,
        "has_children": True, "member_type": 2, "member_ordinal": 0,
        "children_cardinality": 1,
    }


def _tagged(grains: dict[str, int], values: dict[str, str]) -> dict:
    row = dict(values)
    for hname, ordinal in grains.items():
        row[SUBTOTAL_GRAIN_PREFIX + hname] = ordinal
    return row


def _common_build_args() -> dict:
    return dict(
        mdx_dims=["channel_name", "device_type"], mdx_measures=["base_amount"],
        where_sql_clauses=[], model_slug="m",
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        measure_canonical={},
    )


# --- (a) the flat member set on the OTHER axis -----------------------------


def test_flat_member_set_on_the_other_axis_is_registered_leaf_only():
    """The reported failure: device_type was uncovered and the guard dropped
    the channel_name rollup with it."""
    flats = detect_flat_attribute_rollups(_FLAT_SET, _DRILL, _ATTRS)
    assert [r.mdx_dim_name for r in flats] == ["channel_name"]
    assert uncovered_axis_dimensions(_AXIS_DIMS, flats) == {"device_type"}

    found = detect_flat_member_set_rollups(
        _FLAT_SET, _DRILL, _ATTRS,
        registered={r.mdx_dim_name for r in flats},
    )
    assert len(found) == 1
    dev = found[0]
    assert dev.mdx_dim_name == "device_type"
    assert dev.axis == 0
    assert dev.leaf_only and not dev.include_all
    assert dev.is_flat_attribute_rollup
    assert [lvl.dim_name for lvl in dev.levels] == ["device_type"]
    assert uncovered_axis_dimensions(_AXIS_DIMS, flats + found) == set()


def test_the_rollup_side_keeps_its_full_lattice_and_the_flat_side_adds_none():
    flats = detect_flat_attribute_rollups(_FLAT_SET, _DRILL, _ATTRS)
    found = detect_flat_member_set_rollups(
        _FLAT_SET, _DRILL, _ATTRS, registered={r.mdx_dim_name for r in flats},
    )
    queries = build_multi_subtotal_queries(
        hierarchies=flats + found, **_common_build_args(),
    )
    # channel_name {detail, All} x device_type {detail}, minus the all-detail
    # combination the original query already serves: exactly one grain.
    assert len(queries) == 1
    grain = queries[0]
    assert grain.grain_per_hierarchy == {"channel_name": -1, "device_type": 0}
    assert grain.dim_cols == ["device_type"]


# --- (b) the grouped wire spelling, both fields on ONE axis ----------------


def test_wire_spelled_flat_member_set_beside_a_drilldown_on_the_same_axis():
    """``[a].[a].[a].Members`` -- what the wire name becomes after
    normalisation. ``_SUBTOTAL_MEMBERS_RE`` never matched it, so even the
    same-axis CrossJoin (which the two-part form already survives) failed."""
    row = f"CrossJoin({_DRILL}, {_FLAT_SET_WIRE})"
    flats = detect_flat_attribute_rollups("", row, _ATTRS)
    assert [r.mdx_dim_name for r in flats] == ["channel_name"]
    assert uncovered_axis_dimensions(_AXIS_DIMS, flats) == {"device_type"}

    found = detect_flat_member_set_rollups(
        "", row, _ATTRS, registered={r.mdx_dim_name for r in flats},
    )
    assert len(found) == 1
    assert found[0].mdx_dim_name == "device_type"
    assert found[0].axis == 1
    assert found[0].leaf_only and not found[0].include_all
    assert uncovered_axis_dimensions(_AXIS_DIMS, flats + found) == set()


def test_an_already_claimed_attribute_keeps_its_full_all_grain():
    """The two-part set beside a DrilldownLevel is claimed with its All grain
    by detect_flat_attribute_rollups; it must not be downgraded to leaf-only."""
    row = f"CrossJoin({_DRILL}, {_FLAT_SET})"
    flats = detect_flat_attribute_rollups("", row, _ATTRS)
    assert {r.mdx_dim_name for r in flats} == {"channel_name", "device_type"}
    assert all(not r.leaf_only and r.include_all for r in flats)
    assert detect_flat_member_set_rollups(
        "", row, _ATTRS, registered={r.mdx_dim_name for r in flats},
    ) == []


# --- (c) a flat member set ALONE stays on the plain path -------------------


def test_a_lone_flat_member_set_registers_no_rollup_at_all():
    """The caller only consults this detector when another one already
    produced a rollup. With a lone flat set nothing else fires, so the plain
    path renders the axis exactly as it did before."""
    for expr in (_FLAT_SET, _FLAT_SET_WIRE):
        assert detect_flat_attribute_rollups("", expr, _ATTRS) == []
        assert detect_subtotal_hierarchies("", expr, [], {}) == []


def test_a_restricted_axis_is_left_to_the_fail_safe_drop():
    """A set-restricting function makes the OTHER axis's grand total a total
    over rows this axis does not show. A missing subtotal is a display gap; a
    wrong one is a wrong number, so the set is not claimed."""
    row = f"CrossJoin({_DRILL}, Head({_FLAT_SET}, 3))"
    assert detect_flat_member_set_rollups("", row, _ATTRS, registered=set()) == []


def test_a_member_reference_is_not_a_level_set():
    """``[a].[a].[CREDIT].Members`` names a MEMBER, not the attribute's single
    self-named level, and must not be mistaken for the field's member set."""
    expr = "{[device_type].[device_type].[CREDIT].Members}"
    assert detect_flat_member_set_rollups("", expr, _ATTRS, registered=set()) == []
    assert detect_flat_member_set_rollups(
        "", "{[device_type].[device_type].&[3].Members}", _ATTRS, registered=set(),
    ) == []


# --- (d) the Bug-9862 validator passes on (a) and (b) ----------------------


def _validator_fixture() -> tuple[list[SubtotalHierarchy], list[dict], list[list[dict]]]:
    flats = detect_flat_attribute_rollups(_FLAT_SET, _DRILL, _ATTRS)
    found = detect_flat_member_set_rollups(
        _FLAT_SET, _DRILL, _ATTRS, registered={r.mdx_dim_name for r in flats},
    )
    hierarchies = flats + found
    rows = [
        _tagged({"channel_name": 0, "device_type": 0},
                {"channel_name": "web", "device_type": "mobile"}),
        _tagged({"channel_name": 0, "device_type": 0},
                {"channel_name": "store", "device_type": "desktop"}),
        _tagged({"channel_name": -1, "device_type": 0}, {"device_type": "mobile"}),
        _tagged({"channel_name": -1, "device_type": 0}, {"device_type": "desktop"}),
    ]
    tuples = [
        [_member("channel_name", "web"), _member("device_type", "mobile")],
        [_member("channel_name", "store"), _member("device_type", "desktop")],
        [_all_member("channel_name"), _member("device_type", "mobile")],
        [_all_member("channel_name"), _member("device_type", "desktop")],
    ]
    return hierarchies, rows, tuples


def test_validator_accepts_the_axis_the_fix_produces():
    hierarchies, rows, tuples = _validator_fixture()
    validate_rollup_lattice("Axis1", hierarchies, rows, tuples)


def test_validator_still_faults_on_the_pre_fix_axis():
    """Dropping every rollup -- the pre-fix behaviour -- leaves the leaf grain
    only, and the validator names the omitted channel_name=(All)."""
    hierarchies, rows, tuples = _validator_fixture()
    leaf_only_axis = [t for t in tuples if t[0].get("member_type") != 2]
    with pytest.raises(RollupValidationError) as exc:
        validate_rollup_lattice("Axis1", hierarchies, rows, leaf_only_axis)
    assert "omitted a requested rollup grain" in str(exc.value)
    assert "channel_name=(All)" in str(exc.value)
