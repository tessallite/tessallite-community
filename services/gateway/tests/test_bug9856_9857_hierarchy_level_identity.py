"""Bug-9856 / Bug-9857 -- a defined multi-level hierarchy on an Excel axis.

Bug-9856: Excel rendered the Year captions but every value cell was blank.
Three coupled producer defects, all entered through ``_handle_execute`` here
so parsing, rollup detection, the hierarchy alias, axis construction and cell
serialisation are exercised together:

1. the bare ``[H].[H].[Year].Members`` shape named members on a level
   MDSCHEMA_LEVELS never advertised (``[H].[H].[business_date Calendar]``);
2. the Bug-9764 single-level DrilldownLevel named Year ``[H].[H].[2025]``
   while Discover and the expanded shape name it ``[H].[H].[Year].&[2025]``;
3. the hierarchy alias column leaked onto the rollup response and the cell
   ordinals stopped indexing the axis (0,4,8 for three tuples).

Bug-9857: Excel's "Expand Entire Field" sends the two-argument
``DrilldownLevel({[H].[H].[All]}, [H].[H].[Month])``; the level name was
translated as a MEMBER (``WHERE day = 'Month'``).

The contract asserted throughout is the Bug-9771 invariant: Discover and
Execute emit byte-identical member identity.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.dax import xmla_server
from src.dax.cube_model import build_cube_dimensions
from src.dax.mdschema import _rows_members
from tests.test_bug9789_9244_xmla_production_path import (
    _cells_by_ordinal,
    _execute_method,
    _member_identity,
    _members_from_axis,
)

HIER = "[Hierarchies].[business_date Calendar]"
EXCEL = "Microsoft Office Excel"
PROPS = "DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME"
WHERE = "WHERE ([Measures].[avg_base_amount]) CELL PROPERTIES VALUE"

CALENDAR_HIER = {
    "name": "business_date Calendar",
    "levels": [
        {"ordinal": 0, "name": "Year", "key_attribute": {"id": "c-year", "source": "physical_column"}},
        {"ordinal": 1, "name": "Month", "key_attribute": {"id": "c-month", "source": "physical_column"}},
        {"ordinal": 2, "name": "Day", "key_attribute": {"id": "c-day", "source": "physical_column"}},
    ],
}
CALENDAR_DIMS = [
    {"name": "business_date_calendar_year", "display_name": "business_date_calendar_year", "source_column_id": "c-year"},
    {"name": "business_date_calendar_month", "display_name": "business_date_calendar_month", "source_column_id": "c-month"},
    {"name": "business_date_calendar_day", "display_name": "business_date_calendar_day", "source_column_id": "c-day"},
]
YEAR_ROWS = [
    {"business_date_calendar_year": "2025", "avg_base_amount": 1805.78},
    {"business_date_calendar_year": "2026", "avg_base_amount": 1822.84},
]
MONTH_ROWS = [
    {"business_date_calendar_year": "2025", "business_date_calendar_month": "1", "avg_base_amount": 1.0},
    {"business_date_calendar_year": "2026", "business_date_calendar_month": "2", "avg_base_amount": 2.0},
]
GRAND = 1816.87


def _statement(axis_set: str) -> str:
    return (
        f"SELECT NON EMPTY Hierarchize(AddCalculatedMembers({{{axis_set}}})) "
        f"{PROPS} ON COLUMNS FROM [m] {WHERE}"
    )


LEVEL_SHAPE = _statement(f"{HIER}.[Year].Members")
DRILL_SHAPE = _statement(f"DrilldownLevel({{{HIER}.[All]}})")
EXPAND_SHAPE = _statement(f"DrilldownLevel({{{HIER}.[All]}}, {HIER}.[Month])")
# The shape Excel actually sends for "Expand Entire Field" on a placed
# hierarchy (ALEX gateway log, 2026-09-04 05:38): the YEAR set drilled one
# level, with no All member in the set.
# Excel doubles the brace pair around the base set; the text below is the
# gateway log line verbatim apart from the hierarchy spelling.
EXCEL_EXPAND_SHAPE = _statement(
    f"DrilldownLevel({{{{{HIER}.[Year].Members}}}},{HIER}.[Year])"
)
NESTED_EXPAND_SHAPE = _statement(
    f"DrilldownLevel(DrilldownLevel({{{HIER}.[Year].Members}},{HIER}.[Year]),{HIER}.[Month])"
)
DAY_ROWS = [
    {"business_date_calendar_year": "2025", "business_date_calendar_month": "1",
     "business_date_calendar_day": "2025-01-05", "avg_base_amount": 5.0},
    {"business_date_calendar_year": "2026", "business_date_calendar_month": "2",
     "business_date_calendar_day": "2026-02-05", "avg_base_amount": 6.0},
]


@pytest.fixture
def calendar_gateway(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the model/query boundaries; returns the list of executed SQL."""
    executed: list[str] = []

    async def resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def resolve_model_slug(*_: Any, **__: Any):
        return "m"

    async def measures(*_: Any, **__: Any):
        return [{"name": "avg_base_amount", "default_agg": "avg"}]

    async def dimensions(*_: Any, **__: Any):
        return [dict(d) for d in CALENDAR_DIMS]

    async def hierarchies(*_: Any, **__: Any):
        return [CALENDAR_HIER]

    async def named_sets(*_: Any, **__: Any):
        return []

    async def trust(*_: Any, **__: Any):
        return {}

    async def execute_query(sql: str, *_: Any, **__: Any):
        executed.append(sql)
        if "business_date_calendar_day" in sql:
            return {
                "columns": ["business_date_calendar_year", "business_date_calendar_month", "business_date_calendar_day", "avg_base_amount"],
                "rows": [dict(r) for r in DAY_ROWS],
            }
        if "business_date_calendar_month" in sql:
            return {
                "columns": ["business_date_calendar_year", "business_date_calendar_month", "avg_base_amount"],
                "rows": [dict(r) for r in MONTH_ROWS],
            }
        if "business_date_calendar_year" in sql:
            return {
                "columns": ["business_date_calendar_year", "avg_base_amount"],
                "rows": [dict(r) for r in YEAR_ROWS],
            }
        return {"columns": ["avg_base_amount"], "rows": [{"avg_base_amount": GRAND}]}

    monkeypatch.setattr(xmla_server, "_resolve_model_id", resolve_model_id)
    monkeypatch.setattr(xmla_server, "_resolve_model_slug", resolve_model_slug)
    monkeypatch.setattr(xmla_server, "get_model_measures", measures)
    monkeypatch.setattr(xmla_server, "get_model_dimensions", dimensions)
    monkeypatch.setattr(xmla_server, "get_model_hierarchies", hierarchies)
    monkeypatch.setattr(xmla_server, "get_model_named_sets", named_sets)
    monkeypatch.setattr(xmla_server, "_fetch_trust_values", trust)
    monkeypatch.setattr(xmla_server, "execute_query", execute_query)
    return executed


async def _execute(statement: str, session: str) -> str:
    response = await xmla_server._handle_execute(
        _execute_method(statement, EXCEL),
        tenant_slug="demo", jwt_token="token", session_id=session,
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "Fault" not in body, body[:600]
    return body


def _identities(body: str) -> list[dict[str, str]]:
    return [_member_identity(t[0]) for t in _members_from_axis(body, "Axis0")]


def _discover_identities() -> dict[str, dict[str, str]]:
    """MDSCHEMA_MEMBERS identity for the same cube, keyed by unique name."""
    cube = build_cube_dimensions(CALENDAR_DIMS, [CALENDAR_HIER])
    member_data = {
        "business_date Calendar": {
            "members_by_level": {
                0: [{"name": "2025"}, {"name": "2026"}],
                1: [{"name": "1", "parent": "2025"}, {"name": "2", "parent": "2026"}],
            },
        },
    }
    rows = _rows_members(
        "m", [{"name": "avg_base_amount"}], cube, {}, member_data,
        properties={"SspropInitAppName": EXCEL},
    )
    return {
        r["MEMBER_UNIQUE_NAME"]: r
        for r in rows if r.get("HIERARCHY_UNIQUE_NAME") == HIER
    }


def _assert_matches_discover(identity: dict[str, str]) -> None:
    discover = _discover_identities()
    assert identity["uname"] in discover, (identity["uname"], sorted(discover))
    row = discover[identity["uname"]]
    assert identity["lname"] == row["LEVEL_UNIQUE_NAME"]
    assert identity["lnum"] == row["LEVEL_NUMBER"]
    assert identity["parent"] == (row.get("PARENT_UNIQUE_NAME") or "")


async def test_bug9856_level_members_shape_names_the_advertised_level(
    calendar_gateway: list[str],
) -> None:
    """``[H].[H].[Year].Members`` -- the shape Excel sends when the
    hierarchy is placed -- must name its members on the Year level."""
    body = await _execute(LEVEL_SHAPE, "bug-9856-level")
    members = _identities(body)
    assert [m["uname"] for m in members] == [
        f"{HIER}.[Year].&[2025]", f"{HIER}.[Year].&[2026]",
    ]
    assert {m["lname"] for m in members} == {f"{HIER}.[Year]"}
    assert {m["lnum"] for m in members} == {"1"}
    assert {m["display"] for m in members} == {"1"}  # expandable, no children here
    for m in members:
        _assert_matches_discover(m)
    assert _cells_by_ordinal(body) == {0: 1805.78, 1: 1822.84}


async def test_bug9856_first_level_drilldown_keeps_one_identity_per_member(
    calendar_gateway: list[str],
) -> None:
    """The Bug-9764 single-level drill must name Year exactly as Discover and
    the expanded shape do, and its cells must index the axis (0,1,2)."""
    body = await _execute(DRILL_SHAPE, "bug-9856-drill")
    members = _identities(body)
    assert [m["uname"] for m in members] == [
        f"{HIER}.[All]", f"{HIER}.[Year].&[2025]", f"{HIER}.[Year].&[2026]",
    ]
    assert [m["lnum"] for m in members] == ["0", "1", "1"]
    for m in members[1:]:
        _assert_matches_discover(m)
    # Bug-9856 (3): the hierarchy alias no longer leaks a stray flat
    # dimension into the rollup response -- ordinals are contiguous.
    assert _cells_by_ordinal(body) == {0: GRAND, 1: 1805.78, 2: 1822.84}


async def test_bug9857_two_argument_drilldown_expands_to_the_named_level(
    calendar_gateway: list[str],
) -> None:
    """``DrilldownLevel({All}, [H].[H].[Month])`` returns All, Year and Month
    grains; the level name is never translated as a member filter."""
    body = await _execute(EXPAND_SHAPE, "bug-9857-expand")
    for sql in calendar_gateway:
        assert "'Month'" not in sql, sql
    grains = {
        frozenset(c for c in ("business_date_calendar_year", "business_date_calendar_month") if c in sql)
        for sql in calendar_gateway
    }
    assert grains == {
        frozenset(),
        frozenset({"business_date_calendar_year"}),
        frozenset({"business_date_calendar_year", "business_date_calendar_month"}),
    }
    members = _identities(body)
    assert [m["uname"] for m in members] == [
        f"{HIER}.[All]",
        f"{HIER}.[Year].&[2025]",
        f"{HIER}.[Month].&[2025]&[1]",
        f"{HIER}.[Year].&[2026]",
        f"{HIER}.[Month].&[2026]&[2]",
    ]
    assert [m["lnum"] for m in members] == ["0", "1", "2", "1", "2"]
    assert members[2]["parent"] == f"{HIER}.[Year].&[2025]"
    for m in members[1:]:
        _assert_matches_discover(m)
    assert _cells_by_ordinal(body) == {
        0: GRAND, 1: 1805.78, 2: 1.0, 3: 1822.84, 4: 2.0,
    }


async def test_bug9857_unknown_level_argument_is_not_guessed(
    calendar_gateway: list[str],
) -> None:
    """A level the hierarchy does not have leaves the statement alone: no
    rollup is registered and no grain is invented."""
    statement = _statement(f"DrilldownLevel({{{HIER}.[All]}}, {HIER}.[Quarter])")
    response = await xmla_server._handle_execute(
        _execute_method(statement, EXCEL),
        tenant_slug="demo", jwt_token="token", session_id="bug-9857-unknown",
    )
    assert response.status_code == 200
    assert not any("business_date_calendar_month" in sql for sql in calendar_gateway)


async def test_bug9857_excel_level_set_expand_serves_year_and_month_without_all(
    calendar_gateway: list[str],
) -> None:
    """Excel's real expand drills the Year set: Year rollups plus Month
    detail, in Hierarchize order, and NO All row -- the set never had one."""
    body = await _execute(EXCEL_EXPAND_SHAPE, "bug-9857-excel-expand")
    for sql in calendar_gateway:
        assert "'Year'" not in sql, sql
    assert not any(
        "business_date_calendar" not in sql for sql in calendar_gateway
    ), "an All-grain query was issued for a set without All"
    members = _identities(body)
    assert [m["uname"] for m in members] == [
        f"{HIER}.[Year].&[2025]",
        f"{HIER}.[Month].&[2025]&[1]",
        f"{HIER}.[Year].&[2026]",
        f"{HIER}.[Month].&[2026]&[2]",
    ]
    for m in members:
        _assert_matches_discover(m)
    assert _cells_by_ordinal(body) == {0: 1805.78, 1: 1.0, 2: 1822.84, 3: 2.0}


async def test_bug9857_nested_expand_resolves_from_the_inside_out(
    calendar_gateway: list[str],
) -> None:
    """Month then Day: the outer DrilldownLevel wraps the inner one."""
    body = await _execute(NESTED_EXPAND_SHAPE, "bug-9857-nested")
    members = _identities(body)
    assert [m["lnum"] for m in members] == ["1", "2", "3", "1", "2", "3"]
    assert members[2]["uname"] == f"{HIER}.[Day].&[2025]&[1]&[2025-01-05]"
    assert members[2]["parent"] == f"{HIER}.[Month].&[2025]&[1]"
    assert members[5]["uname"] == f"{HIER}.[Day].&[2026]&[2]&[2026-02-05]"
    assert _cells_by_ordinal(body) == {0: 1805.78, 1: 1.0, 2: 5.0, 3: 1822.84, 4: 2.0, 5: 6.0}


async def test_bug9870_bare_lower_level_shape_names_the_path_and_places_each_cell(
    calendar_gateway: list[str],
) -> None:
    """Excel's "expand all" on a placed hierarchy sends the bare
    ``[H].[H].[Month].Members``. Month 1 exists under 2025 and 2026: each is
    its own path-qualified member, parented on its year, with its own cell
    (ALEX 2026-09-04: cities were named by a single key and parented on All,
    so Excel could not place them under their countries)."""
    body = await _execute(_statement(f"{HIER}.[Month].Members"), "bug-9870-level2")
    members = _identities(body)
    assert [m["uname"] for m in members] == [
        f"{HIER}.[Month].&[2025]&[1]", f"{HIER}.[Month].&[2026]&[2]",
    ]
    assert [m["parent"] for m in members] == [
        f"{HIER}.[Year].&[2025]", f"{HIER}.[Year].&[2026]",
    ]
    for m in members:
        _assert_matches_discover(m)
    assert _cells_by_ordinal(body) == {0: 1.0, 1: 2.0}


# Bug-9873: Excel's expand (+) on a member of a placed hierarchy -- the
# two-argument same-hierarchy DrilldownMember, cumulative targets, nested one
# call deeper per level, and the {-{X}} collapse form.
def _ddm(base: str, targets: str) -> str:
    return _statement(f"DrilldownMember({base}, {{{targets}}})")


async def test_bug9873_expand_one_member_serves_its_children_only(
    calendar_gateway: list[str],
) -> None:
    body = await _execute(
        _ddm(f"{{{{{{{HIER}.[Year].Members}}}}}}", f"{HIER}.[Year].&amp;[2026]"),
        "bug-9873-expand",
    )
    members = _identities(body)
    assert [m["uname"] for m in members] == [
        f"{HIER}.[Year].&[2025]", f"{HIER}.[Year].&[2026]", f"{HIER}.[Month].&[2026]&[2]",
    ]
    assert members[2]["parent"] == f"{HIER}.[Year].&[2026]"
    assert _cells_by_ordinal(body) == {0: 1805.78, 1: 1822.84, 2: 2.0}
    assert not any(
        "business_date_calendar" not in sql for sql in calendar_gateway
    ), "no All grain for a base set without All"


async def test_bug9873_collapse_form_inverts_the_rule(calendar_gateway: list[str]) -> None:
    body = await _execute(
        _ddm(f"{{{{{{{HIER}.[Year].Members}}}}}}", f"-{{{HIER}.[Year].&amp;[2026]}}"),
        "bug-9873-collapse",
    )
    assert [m["uname"] for m in _identities(body)] == [
        f"{HIER}.[Year].&[2025]", f"{HIER}.[Month].&[2025]&[1]", f"{HIER}.[Year].&[2026]",
    ]


async def test_bug9873_nested_expand_two_levels_deep(calendar_gateway: list[str]) -> None:
    inner = f"DrilldownMember({{{{{{{HIER}.[Year].Members}}}}}}, {{{HIER}.[Year].&amp;[2026]}})"
    body = await _execute(
        _statement(f"DrilldownMember({inner}, {{{HIER}.[Month].&amp;[2026]&amp;[2]}})"),
        "bug-9873-nested",
    )
    unames = [m["uname"] for m in _identities(body)]
    assert unames == [
        f"{HIER}.[Year].&[2025]", f"{HIER}.[Year].&[2026]",
        f"{HIER}.[Month].&[2026]&[2]", f"{HIER}.[Day].&[2026]&[2]&[2026-02-05]",
    ], unames
