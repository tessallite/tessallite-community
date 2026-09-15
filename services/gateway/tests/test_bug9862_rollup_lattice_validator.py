"""Bug-9862 F6 -- the rollup validator must catch an OMITTED coordinate.

``_validate_rollup_tuples`` proves the emitted axis is a SUBSET of what the
source rows produced. A subset proof is blind to the failure that actually
reaches users: Bug-9891 put ``[Geography].[Geography].[City].Members`` on one
axis, the Bug-9785 coverage guard failed safe by dropping every rollup, and
Excel received 210 leaf tuples for a 336-tuple request -- a structurally
perfect response, no error, blank subtotal rows in the pivot.

These tests pin the two checks that close the gap:

* LATTICE COVERAGE -- every grain the client asked for that the source rows can
  supply must carry at least one tuple. ``NON EMPTY`` may remove member
  COMBINATIONS inside a grain, and may remove a whole grain that nothing
  coarsens into; it may never remove a grain the data supports.
* MEMBER IDENTITY -- one unique name means one member, across every XMLA
  identity field, and its level and parent must agree with the Discover
  catalogue.

Verified to fail on pre-fix code: every assertion here is against
``src.dax.rollup_validator``, a module that does not exist before this change,
and against ``build_real_execute_response(requested_rollups=...)``, a parameter
it does not accept before this change. Running this file at the lane's base SHA
fails at import/collection for the unit cases and with ``TypeError`` for the
wiring case; the pre-fix response builder returns that response WITHOUT error,
which is precisely the defect.
"""

from __future__ import annotations

import pytest

from src.dax.mdx_execute import (
    _validate_requested_rollup_lattice,
    build_real_execute_response,
)
from src.dax.rollup_validator import (
    RollupValidationError,
    expected_grain_lattice,
    validate_member_identity,
    validate_rollup_lattice,
)
from src.dax.subtotal_engine import (
    SUBTOTAL_GRAIN_PREFIX,
    SubtotalHierarchy,
    SubtotalLevel,
    build_multi_subtotal_queries,
    detect_level_set_hierarchies,
)

# --- fixtures: the Bug-9891 shape ------------------------------------------

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
_COL_EXPR = ("Hierarchize(AddCalculatedMembers("
             "{[Geography Channel].[Geography Channel].[City].Members}))")
_ROW_EXPR = (
    "CrossJoin(CrossJoin("
    "Hierarchize(AddCalculatedMembers({DrilldownLevel({[channel_name].[channel_name].[All]})})), "
    "Hierarchize(AddCalculatedMembers({DrilldownLevel({[device_type].[device_type].[All]})}))), "
    "Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type].[account_type].[All]})})))"
)


def _flat(name: str, axis: int = 1) -> SubtotalHierarchy:
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=1, dim_name=name)],
        axis=axis, is_flat_attribute_rollup=True,
    )


def _member(dim: str, value: str, *, ordinal: int = 0) -> dict:
    hier = f"[{dim}].[{dim}]"
    return {
        "hierarchy": hier,
        "uname": f"{hier}.[{value}]",
        "name": value, "key": value, "value": value, "caption": value,
        "lname": f"{hier}.[{dim}]",
        "lnum": "1",
        "parent": f"{hier}.[All]",
        "has_children": False,
        "member_type": 1,
        "member_ordinal": ordinal,
        "children_cardinality": 0,
    }


def _all_member(dim: str) -> dict:
    hier = f"[{dim}].[{dim}]"
    return {
        "hierarchy": hier,
        "uname": f"{hier}.[All]",
        "name": "All", "key": "All", "value": "All", "caption": "All",
        "lname": f"{hier}.[(All)]",
        "lnum": "0",
        "parent": None,
        "has_children": True,
        "member_type": 2,
        "member_ordinal": 0,
        "children_cardinality": 1,
    }


def _tagged(grains: dict[str, int], values: dict[str, str]) -> dict:
    row = dict(values)
    for hname, ordinal in grains.items():
        row[SUBTOTAL_GRAIN_PREFIX + hname] = ordinal
    return row


# --- the lattice the planner and the validator must agree on ---------------


def test_expected_lattice_is_the_planner_lattice_plus_the_detail_combination():
    """One producer, so the safety net can never certify its own omission."""
    flats = [_flat("A"), _flat("B"), _flat("C")]
    lattice = expected_grain_lattice(flats)
    planned = build_multi_subtotal_queries(
        mdx_dims=["A", "B", "C"], mdx_measures=["amount"],
        where_sql_clauses=[], model_slug="m",
        measures_meta=[{"name": "amount", "default_agg": "sum"}],
        hierarchies=flats, measure_canonical={},
    )
    # 2^3 combinations; the planner skips only the all-detail one because the
    # caller's original query already serves it.
    assert len(lattice) == 8
    assert len(planned) == 7
    assert (1, 1, 1) in lattice


def test_leaf_only_and_no_all_hierarchies_contribute_one_grain_each():
    """Bug-9891/Bug-9857 shapes must not inflate the expected lattice."""
    geo = detect_level_set_hierarchies(
        _COL_EXPR, _ROW_EXPR, _GEO_META, _GEO_LEVEL_MAP, registered=set(),
    )[0]
    assert expected_grain_lattice([geo]) == [(2,)]
    assert expected_grain_lattice([_flat("A"), geo]) == [(1, 2), (-1, 2)]


# --- (a) an omitted planned grain faults, naming it ------------------------


def test_omitted_planned_grain_raises_naming_the_missing_grain():
    flats = [_flat("A"), _flat("B")]
    rows = [
        _tagged({"A": 1, "B": 1}, {"A": "a1", "B": "b1"}),
        _tagged({"A": 1, "B": -1}, {"A": "a1"}),
        _tagged({"A": -1, "B": 1}, {"B": "b1"}),
        _tagged({"A": -1, "B": -1}, {}),
    ]
    complete = [
        [_member("A", "a1"), _member("B", "b1")],
        [_member("A", "a1"), _all_member("B")],
        [_all_member("A"), _member("B", "b1")],
        [_all_member("A"), _all_member("B")],
    ]
    validate_rollup_lattice("Axis1", flats, rows, complete)

    # Drop the [All A] x [b1] tuples -- the grain the source clearly supplies.
    omitted = [t for t in complete if t[0].get("member_type") != 2 or t[1].get("member_type") == 2]
    with pytest.raises(RollupValidationError) as exc:
        validate_rollup_lattice("Axis1", flats, rows, omitted)
    message = str(exc.value)
    assert "Axis1 omitted a requested rollup grain" in message
    assert "A=(All)" in message and "B=B" in message


def test_a_grand_total_dropped_from_a_three_field_cartesian_is_caught():
    """Bug-9845's reported class: whole subtotal families silently deleted."""
    flats = [_flat("A"), _flat("B"), _flat("C")]
    rows = [_tagged({"A": 1, "B": 1, "C": 1}, {"A": "a", "B": "b", "C": "c"})]
    detail_only = [[_member("A", "a"), _member("B", "b"), _member("C", "c")]]
    with pytest.raises(RollupValidationError):
        validate_rollup_lattice("Axis1", flats, rows, detail_only)


# --- (b) NON EMPTY may legitimately drop combinations, and whole grains ----


def test_non_empty_may_drop_member_combinations_within_a_grain():
    """Coverage is asserted per GRAIN. A grain that carries one tuple where
    the Cartesian product allows four is a NON EMPTY result, not an omission."""
    flats = [_flat("A"), _flat("B")]
    rows = [
        _tagged({"A": 1, "B": 1}, {"A": "a1", "B": "b1"}),
        _tagged({"A": 1, "B": 1}, {"A": "a2", "B": "b2"}),
        _tagged({"A": 1, "B": -1}, {"A": "a1"}),
        _tagged({"A": -1, "B": 1}, {"B": "b1"}),
        _tagged({"A": -1, "B": -1}, {}),
    ]
    sparse = [
        [_member("A", "a1"), _member("B", "b1")],
        [_member("A", "a2", ordinal=1), _member("B", "b2", ordinal=1)],
        # a2 x All and All x b2 are absent: those cells had no facts.
        [_member("A", "a1"), _all_member("B")],
        [_all_member("A"), _member("B", "b1")],
        [_all_member("A"), _all_member("B")],
    ]
    validate_rollup_lattice("Axis1", flats, rows, sparse)


def test_a_grain_nothing_coarsens_into_is_not_required():
    """Every detail row was pruned (NON EMPTY, or a DrilldownMember filter),
    so the detail grain is genuinely absent. Aggregating a finer grain cannot
    produce an empty coarser one, which is why the reverse is still enforced."""
    flats = [_flat("A"), _flat("B")]
    rows = [
        _tagged({"A": 1, "B": -1}, {"A": "a1"}),
        _tagged({"A": -1, "B": -1}, {}),
    ]
    without_detail = [
        [_member("A", "a1"), _all_member("B")],
        [_all_member("A"), _all_member("B")],
    ]
    validate_rollup_lattice("Axis1", flats, rows, without_detail)


def test_an_empty_result_never_faults():
    validate_rollup_lattice("Axis1", [_flat("A"), _flat("B")], [], [])


def test_a_suppress_wire_client_may_omit_every_all_grain():
    """``RollupWireMode.SUPPRESS`` drops All-grain rows by contract."""
    flats = [_flat("A")]
    rows = [_tagged({"A": 1}, {"A": "a1"})]
    detail_only = [[_member("A", "a1")]]
    with pytest.raises(RollupValidationError):
        validate_rollup_lattice("Axis1", flats, rows, detail_only)
    validate_rollup_lattice(
        "Axis1", flats, rows, detail_only, all_grain_suppressed=True,
    )


# --- (c) member identity ----------------------------------------------------


_CAT = {"[A].[A]": ["A"], "[Geography Channel].[Geography Channel]":
        ["Country", "City", "Channel"]}


@pytest.mark.parametrize("field,value,expected_field", [
    ("caption", "Renamed", "caption"),
    ("parent", "[A].[A].[somewhere-else]", "parent"),
    ("lname", "[A].[A].[Other]", "lname"),
    ("member_ordinal", 7, "member_ordinal"),
    ("key", "different", "key"),
    ("value", "different", "value"),
    ("children_cardinality", 3, "children_cardinality"),
    ("has_children", True, "has_children"),
    ("member_type", 2, "member_type"),
])
def test_one_unique_name_may_not_carry_two_identities(field, value, expected_field):
    first = _member("A", "a1")
    second = dict(first)
    second[field] = value
    with pytest.raises(RollupValidationError) as exc:
        validate_member_identity("Axis1", [[first], [second]])
    assert "member identity changed across tuples" in str(exc.value)
    assert expected_field in str(exc.value)


def test_a_stable_repeated_member_passes():
    m = _member("A", "a1")
    validate_member_identity("Axis1", [[m], [dict(m)], [dict(m)]], _CAT)


def test_a_level_the_catalogue_does_not_advertise_raises():
    m = _member("A", "a1")
    m["lname"] = "[A].[A].[Fabricated]"
    with pytest.raises(RollupValidationError) as exc:
        validate_member_identity("Axis1", [[m]], _CAT)
    assert "does not advertise" in str(exc.value)


def test_a_level_number_that_contradicts_the_catalogue_position_raises():
    hier = "[Geography Channel].[Geography Channel]"
    m = {
        **_member("x", "y"),
        "hierarchy": hier,
        "uname": f"{hier}.[City].&[paris]",
        "lname": f"{hier}.[City]",
        "lnum": "1",  # City is catalogue position 2
        "parent": f"{hier}.[All]",
    }
    with pytest.raises(RollupValidationError) as exc:
        validate_member_identity("Axis1", [[m]], _CAT)
    assert "LEVEL_NUMBER" in str(exc.value)


def test_a_parent_at_an_impossible_depth_raises():
    hier = "[Geography Channel].[Geography Channel]"
    country = {
        **_member("x", "y"), "hierarchy": hier,
        "uname": f"{hier}.[Country].&[fr]",
        "lname": f"{hier}.[Country]", "lnum": "1",
        "parent": f"{hier}.[All]",
    }
    channel = {
        **_member("x", "y"), "hierarchy": hier,
        "uname": f"{hier}.[Channel].&[fr]&[paris]&[web]",
        "lname": f"{hier}.[Channel]", "lnum": "3",
        # Skips City: a level-3 member cannot be the child of a level-1 one.
        "parent": country["uname"],
    }
    with pytest.raises(RollupValidationError) as exc:
        validate_member_identity("Axis1", [[country], [channel]], _CAT)
    assert "exactly one level above" in str(exc.value)


def test_a_parent_that_is_not_on_this_axis_is_not_depth_checked():
    hier = "[Geography Channel].[Geography Channel]"
    city = {
        **_member("x", "y"), "hierarchy": hier,
        "uname": f"{hier}.[City].&[fr]&[paris]",
        "lname": f"{hier}.[City]", "lnum": "2",
        "parent": f"{hier}.[Country].&[fr]",
    }
    validate_member_identity("Axis1", [[city]], _CAT)


# --- (d) the Bug-9891 shape, with the coverage guard forced to drop --------


def _bug9891_requested_rollups() -> list[SubtotalHierarchy]:
    geo = detect_level_set_hierarchies(
        _COL_EXPR, _ROW_EXPR, _GEO_META, _GEO_LEVEL_MAP, registered=set(),
    )[0]
    return [
        _flat("channel_name"), _flat("device_type"), _flat("account_type"), geo,
    ]


_GEO_HIER = "[Geography Channel].[Geography Channel]"


def _city_member(city: str, *, ordinal: int = 0) -> dict:
    """A City member exactly as the plain level-set path renders it."""
    return {
        "hierarchy": _GEO_HIER,
        "uname": f"{_GEO_HIER}.[City].&[fr]&[{city}]",
        "name": city, "key": city, "value": city, "caption": city,
        "lname": f"{_GEO_HIER}.[City]",
        "lnum": "2",
        "parent": f"{_GEO_HIER}.[Country].&[fr]",
        "has_children": True,
        "member_type": 1,
        "member_ordinal": ordinal,
        "children_cardinality": 0,
    }


_BUG9891_DIMS = [
    {"name": "channel_name"}, {"name": "device_type"},
    {"name": "account_type"}, {"name": "country_code"}, {"name": "city_name"},
]


def test_bug9891_dropped_rollups_now_fault_instead_of_serving_leaf_tuples():
    """THE regression this validator exists for.

    Before Bug-9891's fix the coverage guard dropped all four rollups and the
    response carried leaf tuples only. Nothing in the pipeline objected. With
    the guard forced to drop them (rollups requested, none served), the
    validator must fault and name a missing grain.
    """
    requested = _bug9891_requested_rollups()
    rows = [
        {"channel_name": "web", "device_type": "mobile",
         "account_type": "retail", "city_name": "paris",
         "country_code": "fr", "amount": 1},
        {"channel_name": "store", "device_type": "desktop",
         "account_type": "retail", "city_name": "lyon",
         "country_code": "fr", "amount": 2},
    ]
    leaf_only_axis = [
        [_member("channel_name", r["channel_name"], ordinal=i),
         _member("device_type", r["device_type"], ordinal=i),
         _member("account_type", r["account_type"], ordinal=i)]
        for i, r in enumerate(rows)
    ]
    city_axis = [[_city_member("paris")], [_city_member("lyon", ordinal=1)]]
    with pytest.raises(RollupValidationError) as exc:
        _validate_requested_rollup_lattice(
            requested, rows,
            [_GEO_HIER], [], city_axis,
            ["[channel_name].[channel_name]", "[device_type].[device_type]",
             "[account_type].[account_type]"],
            [], leaf_only_axis,
            _BUG9891_DIMS, _GEO_META,
        )
    message = str(exc.value)
    # The COLUMN axis is intact (the level set rendered fine); the fault names
    # the ROW axis, whose eight-grain lattice collapsed to leaf tuples.
    assert "Axis1 omitted a requested rollup grain" in message
    assert "(All)" in message


def test_bug9891_full_lattice_passes_the_same_validator():
    """The shape Bug-9891 was fixed to produce: all eight row-axis grains."""
    requested = _bug9891_requested_rollups()
    grains = [
        (1, 1, 1), (1, 1, -1), (1, -1, 1), (-1, 1, 1),
        (1, -1, -1), (-1, 1, -1), (-1, -1, 1), (-1, -1, -1),
    ]
    names = ["channel_name", "device_type", "account_type"]
    values = {"channel_name": "web", "device_type": "mobile",
              "account_type": "retail"}
    rows, tuples = [], []
    for grain in grains:
        row = {"city_name": "paris", "country_code": "fr", "amount": 1,
               SUBTOTAL_GRAIN_PREFIX + "Geography Channel": 2}
        members = []
        for name, ordinal in zip(names, grain):
            row[SUBTOTAL_GRAIN_PREFIX + name] = ordinal
            if ordinal == -1:
                members.append(_all_member(name))
            else:
                row[name] = values[name]
                members.append(_member(name, values[name]))
        rows.append(row)
        tuples.append(members)

    _validate_requested_rollup_lattice(
        requested, rows,
        [_GEO_HIER], [], [[_city_member("paris")]],
        [f"[{n}].[{n}]" for n in names], [], tuples,
        _BUG9891_DIMS, _GEO_META,
    )


def test_the_validator_is_wired_into_the_execute_response_builder():
    """Deliverable 3: behind the existing validation call site, and reported as
    a SOAP-faultable ``ValueError`` -- not a silent 200 with missing rows."""
    requested = [_flat("channel_name"), _flat("device_type")]
    rows = [
        {"channel_name": "web", "device_type": "mobile", "amount": 1},
        {"channel_name": "store", "device_type": "desktop", "amount": 2},
    ]
    kwargs = dict(
        mdx="SELECT",
        catalog="modely",
        columns=["channel_name", "device_type", "amount"],
        rows=rows,
        measures_meta=[{"name": "amount", "display_name": "Amount",
                        "default_agg": "sum"}],
        dimensions_meta=[{"name": "channel_name"}, {"name": "device_type"}],
        client_app_name="Microsoft Excel",
    )
    # Without the requested rollups the builder cannot know anything is missing
    # -- exactly the pre-fix blind spot.
    assert build_real_execute_response(**kwargs)
    with pytest.raises(ValueError) as exc:
        build_real_execute_response(**kwargs, requested_rollups=requested)
    assert "omitted a requested rollup grain" in str(exc.value)


def test_off_and_warn_modes_do_not_fault(monkeypatch, caplog):
    requested = [_flat("A"), _flat("B")]
    rows = [_tagged({"A": 1, "B": 1}, {"A": "a1", "B": "b1"})]
    detail_only = [[_member("A", "a1"), _member("B", "b1")]]
    args = (
        requested, rows, ["[Measures].[m]"], [], None,
        ["[A].[A]", "[B].[B]"], [], detail_only,
        [{"name": "A"}, {"name": "B"}], [],
    )
    with pytest.raises(RollupValidationError):
        _validate_requested_rollup_lattice(*args)
    monkeypatch.setenv("TESSALLITE_XMLA_ROLLUP_VALIDATION", "warn")
    _validate_requested_rollup_lattice(*args)
    monkeypatch.setenv("TESSALLITE_XMLA_ROLLUP_VALIDATION", "off")
    _validate_requested_rollup_lattice(*args)


def test_no_requested_rollups_costs_nothing():
    """The plain path must be untouched: no rollups in, immediate return."""
    _validate_requested_rollup_lattice(
        None, [{"a": 1}], ["[A].[A]"], [_member("A", "a1")], None,
        [], [], None, [{"name": "A"}], [],
    )
