"""Bug-9865: an unrestricted MDSCHEMA_MEMBERS browse must not be quadratic.

An unrestricted ``MDSCHEMA_MEMBERS`` Discover on the demo model took ~35 s
(19,343 rows as the viewer, 31,650 as admin) and harness scenario
16-6-discover-diagnostics timed out at 30 s on it alone. ``_rows_members``
rescanned a whole level per member twice -- once to count a member's children,
once per ancestor hop inside ``ancestor_key_path_from_parent_chain`` -- so the
row builder alone cost 4.6 s of CPU at 24k members and grew with the square of
the member count.

The fix indexes both per dimension, once. These tests pin BOTH halves: the rows
are byte-for-byte what they were (the indexes must not change any advertised
member identity, child cardinality or parent), and the builder scales linearly.
"""

import time

import pytest

from src.dax.mdschema import _rows_members
from src.dax.member_uname import (
    ancestor_key_path_from_parent_chain,
    build_parent_index,
)

CATALOG = "acme-demo__project1__modely"
# A hierarchy-sourced dimension is advertised under the [Hierarchies] group.
HIER = "[Hierarchies].[Calendar]"


def _uname_key_path(uname: str) -> list[str]:
    """The ``&[k0]&[k1]...`` key path of a canonical member unique name."""
    return [part.strip("[]") for part in uname.split("&")[1:]]


def _old_key_path(mem, level_idx, members_by_level) -> list[str]:
    """The pre-fix ancestor walk: a linear scan of each ancestor level."""
    path = [mem["name"]]
    cur_parent = str(mem.get("parent") or "")
    for lvl in range(level_idx - 1, -1, -1):
        if not cur_parent:
            break
        path.append(cur_parent)
        nxt = ""
        for candidate in members_by_level.get(lvl, []):
            if str(candidate.get("name", "")) == cur_parent:
                nxt = str(candidate.get("parent") or "")
                break
        cur_parent = nxt
    path.reverse()
    return path

CALENDAR_DIM = {
    "name": "Calendar",
    "source": "hierarchy",
    "levels": [{"name": "Year"}, {"name": "Month"}, {"name": "Day"}],
}
FLAT_DIM = {"name": "country_code"}


def _calendar_members(years=("2025", "2026"), months=("1", "2"), days=("1", "2")):
    """A calendar whose Month and Day keys REPEAT under different ancestors.

    That repetition is the case the canonical key path exists for, so it is the
    case the index must not disturb.
    """
    by_level = {0: [], 1: [], 2: []}
    for y in years:
        by_level[0].append({"name": y, "caption": y, "key": y, "parent": ""})
        for m in months:
            by_level[1].append(
                {"name": f"{y}-{m}", "caption": m, "key": m, "parent": y}
            )
            for d in days:
                by_level[2].append(
                    {"name": f"{y}-{m}-{d}", "caption": d, "key": d,
                     "parent": f"{y}-{m}"}
                )
    return {"members_by_level": by_level}


def _rows_for(dimensions, member_data, restrictions=None):
    return _rows_members(
        CATALOG, [], dimensions, restrictions or {}, member_data, {}
    )


def _by_uname(rows):
    return {r["MEMBER_UNIQUE_NAME"]: r for r in rows}


class TestBug9865MemberIdentityUnchanged:
    """The indexes replace two scans; nothing they feed may change."""

    def test_children_cardinality_matches_the_real_child_count(self):
        rows = _by_uname(_rows_for([CALENDAR_DIM], {"Calendar": _calendar_members()}))
        # 2 years, each with 2 months, each with 2 days.
        all_row = next(r for r in rows.values() if r["MEMBER_TYPE"] == "2")
        assert all_row["CHILDREN_CARDINALITY"] == "2"
        assert rows[f"{HIER}.[Year].&[2025]"]["CHILDREN_CARDINALITY"] == "2"
        assert rows[f"{HIER}.[Month].&[2025]&[2025-1]"]["CHILDREN_CARDINALITY"] == "2"
        assert (
            rows[f"{HIER}.[Day].&[2025]&[2025-1]&[2025-1-1]"]["CHILDREN_CARDINALITY"]
            == "0"
        )

    def test_repeated_captions_under_different_ancestors_stay_distinct(self):
        rows = _by_uname(_rows_for([CALENDAR_DIM], {"Calendar": _calendar_members()}))
        # Month caption "1" exists under BOTH years; the ancestor walk is what
        # keeps them apart, and it now reads the index instead of scanning.
        for year in ("2025", "2026"):
            month = rows[f"{HIER}.[Month].&[{year}]&[{year}-1]"]
            assert month["MEMBER_CAPTION"] == "1"
            assert month["PARENT_UNIQUE_NAME"] == f"{HIER}.[Year].&[{year}]"
            day = rows[f"{HIER}.[Day].&[{year}]&[{year}-2]&[{year}-2-1]"]
            assert day["MEMBER_CAPTION"] == "1"
            assert day["PARENT_UNIQUE_NAME"] == f"{HIER}.[Month].&[{year}]&[{year}-2]"

    def test_rows_equal_the_pre_fix_scan_algorithm(self):
        """Equivalence oracle: the two replaced scans, recomputed here.

        The fix is an indexing change, so the strongest guard is that every
        advertised child count and ancestor path still equals what the
        per-member level scans produced.
        """
        members_by_level = _calendar_members()["members_by_level"]
        rows = [
            r
            for r in _rows_for([CALENDAR_DIM], {"Calendar": _calendar_members()})
            if r["MEMBER_TYPE"] == "1"
        ]
        level_of = {"Year": 0, "Month": 1, "Day": 2}
        for row in rows:
            level_name = row["LEVEL_UNIQUE_NAME"].rsplit(".", 1)[-1].strip("[]")
            level_idx = level_of[level_name]
            mem = next(
                m for m in members_by_level[level_idx]
                if m["name"] == row["MEMBER_KEY"]
            )
            # The advertised identity is exactly the pre-fix walk's key path.
            assert _uname_key_path(row["MEMBER_UNIQUE_NAME"]) == _old_key_path(
                mem, level_idx, members_by_level
            )
            expected_children = sum(
                1
                for child in members_by_level.get(level_idx + 1, [])
                if str(child.get("parent") or "") == mem["name"]
            )
            assert row["CHILDREN_CARDINALITY"] == str(expected_children)

    def test_flat_dimension_rows_are_unaffected(self):
        data = {"country_code": {"members": [
            {"name": "GB", "caption": "United Kingdom", "key": "GB"},
            {"name": "FR", "caption": "France", "key": "FR"},
        ]}}
        rows = _rows_for([FLAT_DIM], data)
        data_rows = [r for r in rows if r["MEMBER_TYPE"] == "1"]
        assert [r["MEMBER_CAPTION"] for r in data_rows] == [
            "United Kingdom", "France",
        ]
        assert {r["CHILDREN_CARDINALITY"] for r in data_rows} == {"0"}

    def test_parent_index_first_occurrence_wins_like_the_old_scan(self):
        """The replaced scan stopped at the FIRST match; the index must too."""
        members_by_level = {
            0: [{"name": "dup", "parent": "first"},
                {"name": "dup", "parent": "second"}],
        }
        index = build_parent_index(members_by_level)
        assert index[0]["dup"] == "first"

    def test_walker_without_an_index_is_unchanged(self):
        """The index is an optimisation, not a behaviour switch."""
        members_by_level = _calendar_members()["members_by_level"]
        for level_idx, members in members_by_level.items():
            for mem in members:
                with_index = ancestor_key_path_from_parent_chain(
                    mem["name"], level_idx, mem["parent"], members_by_level,
                    parent_index=build_parent_index(members_by_level),
                )
                without = ancestor_key_path_from_parent_chain(
                    mem["name"], level_idx, mem["parent"], members_by_level,
                )
                assert with_index == without


def _wide_calendar(n_years):
    by_level = {0: [], 1: [], 2: []}
    for y in range(n_years):
        yk = f"Y{y}"
        by_level[0].append({"name": yk, "caption": yk, "key": yk, "parent": ""})
        for m in range(12):
            mk = f"{yk}-M{m}"
            by_level[1].append(
                {"name": mk, "caption": str(m), "key": str(m), "parent": yk}
            )
            for d in range(30):
                by_level[2].append({
                    "name": f"{mk}-D{d}", "caption": str(d), "key": str(d),
                    "parent": mk,
                })
    return {"members_by_level": by_level}


def _build_seconds(n_years: int) -> tuple[int, float]:
    data = {"Calendar": _wide_calendar(n_years)}
    started = time.perf_counter()
    rows = _rows_members(CATALOG, [], [CALENDAR_DIM], {}, data, {})
    return len(rows), time.perf_counter() - started


def test_bug9865_unrestricted_browse_is_not_quadratic():
    """A 14.9k-member browse must scale linearly, not quadratically.

    Measured in this worktree: pre-fix 3.04 s for these 14,921 rows, post-fix
    0.22 s -- and the growth was quadratic. The guard is the GROWTH, not a
    wall-clock budget: under CI's coverage instrumentation the same build
    takes ~11 s (base 0cad081db and tip alike, 2026-09-05), so an absolute
    1.0 s limit failed on every CI-mirror run while the builder was fine.
    Four times the input costs a linear builder ~4x and the quadratic one
    ~16x; the bound sits between them.
    """
    small_rows, small = _build_seconds(10)
    large_rows, large = _build_seconds(40)
    assert small_rows == 10 * (1 + 12 + 12 * 30) + 1  # + the All member
    assert large_rows == 40 * (1 + 12 + 12 * 30) + 1
    ratio = large / max(small, 1e-6)
    assert ratio < 8.0, (
        f"MDSCHEMA_MEMBERS built {large_rows} rows in {large:.2f}s vs "
        f"{small_rows} rows in {small:.2f}s (x{ratio:.1f}); the per-member "
        "level scans of Bug-9865 have returned"
    )
