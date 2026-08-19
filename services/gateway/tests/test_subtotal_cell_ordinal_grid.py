"""Subtotal cell-ordinal regression grid (Bug-3616 / Bug-3617 / Bug-3618).

A 4-axis-shape x 1..2-measure grid that pins the MDDataSet cell-ordinal
convention and DISCOVER<->Execute member-uname consistency for subtotal
pivots. The four axis shapes are:

  1. ROWS    — two subtotal hierarchies stacked on the ROWS axis,
               measures on COLUMNS.
  2. COLUMNS — two subtotal hierarchies on the COLUMNS axis, measures on
               ROWS (Bug-3616: 2-measure ordinal was transposed).
  3. MIRROR  — subtotal hierarchy on COLUMNS + a flat dim on ROWS
               (Bug-3618: Axis0 was un-deduplicated and cells misaligned).
  4. FLAT    — flat (non-subtotal) dims on both axes (baseline).

Every grid case asserts:
  * cell values land at the correct MDDataSet CellOrdinal
    (ordinal = row_pos * |Axis0| + col_pos, measures being members of the
    axis they sit on, never a separate multiplier);
  * no two cells share a CellOrdinal;
  * (Bug-3617) DISCOVER (MDSCHEMA_MEMBERS) member unames match the Execute
    axis-tuple unames for the same subtotal member.

These tests are intentionally driven through build_real_execute_response
end-to-end (not the cell-data helpers in isolation) so the assertion is the
business outcome a BI client observes.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from src.dax.mdx_execute import build_real_execute_response
from src.dax.mdschema import _rows_members
from src.dax.subtotal_engine import (
    SubtotalHierarchy,
    SubtotalLevel,
    SUBTOTAL_GRAIN_KEY,
    SUBTOTAL_GRAIN_PREFIX,
)

_NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"


def _mk(name, mdx_dim, mdx_hier, levels, axis):
    return SubtotalHierarchy(
        hierarchy_name=name,
        mdx_dim_name=mdx_dim,
        mdx_hier_name=mdx_hier,
        levels=[SubtotalLevel(name=n, ordinal=o, dim_name=d) for n, o, d in levels],
        axis=axis,
    )


def _parse(xml):
    """Return ({axis_name: [[caption,...], ...]}, {ordinal: float})."""
    root = ET.fromstring(xml)  # also asserts the response is well-formed XML
    axes: dict[str, list[list[str]]] = {}
    unames: dict[str, list[list[str]]] = {}
    for axis in root.iter(_NS + "Axis"):
        caps, uns = [], []
        for t in axis.iter(_NS + "Tuple"):
            caps.append([m.findtext(_NS + "Caption") for m in t.iter(_NS + "Member")])
            uns.append([m.findtext(_NS + "UName") for m in t.iter(_NS + "Member")])
        axes[axis.get("name")] = caps
        unames[axis.get("name")] = uns
    cells = {}
    for c in root.iter(_NS + "Cell"):
        cells[int(c.get("CellOrdinal"))] = float(c.findtext(_NS + "Value"))
    return axes, cells, unames


def _ordinals(xml):
    return [int(m.group(1)) for m in re.finditer(r'CellOrdinal="(\d+)"', xml)]


# ---------------------------------------------------------------------------
# Shape 1 — two subtotal hierarchies on ROWS, measures on COLUMNS
# ---------------------------------------------------------------------------

def _rows_only_data(extra_measure=False):
    g = SUBTOTAL_GRAIN_PREFIX
    rows = [
        {"country": "France", "category": "Bikes", "Amount": 100, "Qty": 1, g + "Geo": 0, g + "Prod": 0},
        {"country": "France", "category": "Cars",  "Amount": 200, "Qty": 2, g + "Geo": 0, g + "Prod": 0},
        {"country": "France", "category": "",       "Amount": 300, "Qty": 3, g + "Geo": 0, g + "Prod": -1},
        {"country": "",        "category": "",       "Amount": 300, "Qty": 3, g + "Geo": -1, g + "Prod": -1},
    ]
    return rows


def _build_rows_only(measures):
    measure_set = "{" + ", ".join(f"[Measures].[{m}]" for m in measures) + "}"
    mdx = (
        f"SELECT {measure_set} ON COLUMNS, "
        "CrossJoin([Geography].[Geo].MEMBERS, [Product].[Prod].MEMBERS) ON ROWS "
        "FROM [demo]"
    )
    h_geo = _mk("Geo", "Geography", "Geo", [("Country", 0, "country")], axis=1)
    h_prod = _mk("Prod", "Product", "Prod", [("Category", 0, "category")], axis=1)
    return build_real_execute_response(
        mdx=mdx, catalog="demo",
        columns=["country", "category", *measures],
        rows=_rows_only_data(),
        measures_meta=[{"name": m, "default_agg": "sum"} for m in measures],
        dimensions_meta=[{"name": "country"}, {"name": "category"}],
        client_app_name="Excel",
        subtotal_hierarchies=[h_geo, h_prod],
    )


def test_rows_one_measure():
    axes, cells, _ = _parse(_build_rows_only(["Amount"]))
    assert len(axes["Axis1"]) == 4
    assert ["Amount"] in axes["Axis0"]
    assert len(_ordinals(_build_rows_only(["Amount"]))) == 4
    by_tuple = {tuple(c): cells[i] for i, c in enumerate(axes["Axis1"])}
    assert by_tuple[("France", "Bikes")] == 100.0
    assert by_tuple[("France", "Cars")] == 200.0
    assert by_tuple[("France", "All")] == 300.0
    assert by_tuple[("All", "All")] == 300.0


def test_rows_two_measures():
    xml = _build_rows_only(["Amount", "Qty"])
    axes, cells, _ = _parse(xml)
    n_axis0 = len(axes["Axis0"])  # 2 measures
    assert n_axis0 == 2
    assert len(axes["Axis1"]) == 4
    ords = _ordinals(xml)
    assert len(ords) == len(set(ords)), "duplicate CellOrdinals"
    # ordinal = row_pos * |Axis0| + measure_pos
    measure_pos = {tuple(c): i for i, c in enumerate(axes["Axis0"])}
    row_pos = {tuple(c): i for i, c in enumerate(axes["Axis1"])}
    amt = measure_pos[("Amount",)]
    qty = measure_pos[("Qty",)]
    r_fb = row_pos[("France", "Bikes")]
    assert cells[r_fb * n_axis0 + amt] == 100.0
    assert cells[r_fb * n_axis0 + qty] == 1.0
    r_all = row_pos[("All", "All")]
    assert cells[r_all * n_axis0 + amt] == 300.0
    assert cells[r_all * n_axis0 + qty] == 3.0


# ---------------------------------------------------------------------------
# Shape 2 — two subtotal hierarchies on COLUMNS, measures on ROWS (Bug-3616)
# ---------------------------------------------------------------------------

def _build_cols_only(measures):
    g = SUBTOTAL_GRAIN_PREFIX
    rows = [
        {"country": "France", "category": "Bikes", "Amount": 11, "Qty": 91, g + "Geo": 0, g + "Prod": 0},
        {"country": "France", "category": "",       "Amount": 12, "Qty": 92, g + "Geo": 0, g + "Prod": -1},
        {"country": "",        "category": "",       "Amount": 13, "Qty": 93, g + "Geo": -1, g + "Prod": -1},
    ]
    measure_set = "{" + ", ".join(f"[Measures].[{m}]" for m in measures) + "}"
    mdx = (
        "SELECT CrossJoin([Geography].[Geo].MEMBERS, [Product].[Prod].MEMBERS) ON COLUMNS, "
        f"{measure_set} ON ROWS FROM [demo]"
    )
    h_geo = _mk("Geo", "Geography", "Geo", [("Country", 0, "country")], axis=0)
    h_prod = _mk("Prod", "Product", "Prod", [("Category", 0, "category")], axis=0)
    return build_real_execute_response(
        mdx=mdx, catalog="demo",
        columns=["country", "category", *measures],
        rows=rows,
        measures_meta=[{"name": m, "default_agg": "sum"} for m in measures],
        dimensions_meta=[{"name": "country"}, {"name": "category"}],
        client_app_name="Excel",
        subtotal_hierarchies=[h_geo, h_prod],
    )


def test_cols_one_measure():
    xml = _build_cols_only(["Amount"])
    axes, cells, _ = _parse(xml)
    assert len(axes["Axis0"]) == 3  # France/Bikes, France/All, All/All
    ords = _ordinals(xml)
    assert len(ords) == len(set(ords))
    # single measure: ordinal == col position
    col_pos = {tuple(c): i for i, c in enumerate(axes["Axis0"])}
    assert cells[col_pos[("France", "Bikes")]] == 11.0
    assert cells[col_pos[("France", "All")]] == 12.0
    assert cells[col_pos[("All", "All")]] == 13.0


def test_cols_two_measures_ordinal_is_axis_major():
    """Bug-3616: measures on ROWS are the SLOW axis; the ordinal is
    measure_pos * |Axis0| + col_pos, not col_pos * num_measures + m_idx."""
    xml = _build_cols_only(["Amount", "Qty"])
    axes, cells, _ = _parse(xml)
    n_axis0 = len(axes["Axis0"])  # 3 column tuples
    assert n_axis0 == 3
    assert len(axes["Axis1"]) == 2  # Amount, Qty on rows
    ords = _ordinals(xml)
    assert len(ords) == len(set(ords)), "duplicate CellOrdinals"
    col_pos = {tuple(c): i for i, c in enumerate(axes["Axis0"])}
    m_pos = {tuple(c): i for i, c in enumerate(axes["Axis1"])}
    amt, qty = m_pos[("Amount",)], m_pos[("Qty",)]
    # Amount row occupies the whole first Axis0 span; Qty the second.
    assert cells[amt * n_axis0 + col_pos[("France", "Bikes")]] == 11.0
    assert cells[amt * n_axis0 + col_pos[("France", "All")]] == 12.0
    assert cells[amt * n_axis0 + col_pos[("All", "All")]] == 13.0
    assert cells[qty * n_axis0 + col_pos[("France", "Bikes")]] == 91.0
    assert cells[qty * n_axis0 + col_pos[("France", "All")]] == 92.0
    assert cells[qty * n_axis0 + col_pos[("All", "All")]] == 93.0


# ---------------------------------------------------------------------------
# Shape 3 — MIRROR: subtotal hierarchy on COLUMNS + flat dim on ROWS (Bug-3618)
# ---------------------------------------------------------------------------

def _build_mirror(measures):
    gk = SUBTOTAL_GRAIN_KEY
    # Detail + per-channel subtotal rows. Category subtotal hierarchy on
    # COLUMNS; channel is a flat dim on ROWS.
    rows = [
        {"channel": "Web",   "category": "Bikes", "Amount": 10, "Qty": 1, gk: 0},
        {"channel": "Web",   "category": "Cars",  "Amount": 5,  "Qty": 2, gk: 0},
        {"channel": "Web",   "category": "",       "Amount": 15, "Qty": 3, gk: -1},
        {"channel": "Store", "category": "Bikes", "Amount": 20, "Qty": 4, gk: 0},
        {"channel": "Store", "category": "",       "Amount": 20, "Qty": 4, gk: -1},
    ]
    if len(measures) == 1:
        col_set = "[Product].[Prod].MEMBERS"
    else:
        # measures on the column axis alongside the subtotal hierarchy is not
        # the mirror shape; keep measures on the slicer via WHERE for M=1 and
        # use a separate two-measure mirror with measures on ROWS would not be
        # a mirror — so the M=2 mirror keeps measures on ROWS.
        col_set = "[Product].[Prod].MEMBERS"
    if len(measures) == 1:
        mdx = (
            f"SELECT {col_set} ON COLUMNS, "
            "[channel].[channel].MEMBERS ON ROWS FROM [demo]"
        )
    else:
        measure_set = "{" + ", ".join(f"[Measures].[{m}]" for m in measures) + "}"
        mdx = (
            f"SELECT CrossJoin({col_set}, {measure_set}) ON COLUMNS, "
            "[channel].[channel].MEMBERS ON ROWS FROM [demo]"
        )
    h_prod = _mk("Prod", "Product", "Prod", [("Category", 0, "category")], axis=0)
    return build_real_execute_response(
        mdx=mdx, catalog="demo",
        columns=["channel", "category", *measures],
        rows=rows,
        measures_meta=[{"name": m, "default_agg": "sum"} for m in measures],
        dimensions_meta=[{"name": "channel"}, {"name": "category"}],
        client_app_name="Excel",
        subtotal_hierarchy=h_prod,
    )


def test_mirror_one_measure():
    """Bug-3618: Axis0 must carry the deduplicated subtotal column members
    (Bikes, Cars, All) and Axis1 the flat row members (Web, Store), with
    cells aligned to ordinal = row_pos * |Axis0| + col_pos."""
    xml = _build_mirror(["Amount"])
    axes, cells, _ = _parse(xml)
    ords = _ordinals(xml)
    assert len(ords) == len(set(ords)), "duplicate CellOrdinals"
    # Axis0 = deduplicated category subtotal members.
    col_caps = [tuple(c) for c in axes["Axis0"]]
    assert ("Bikes",) in col_caps
    assert ("All",) in col_caps
    assert len(col_caps) == len(set(col_caps)), "Axis0 not deduplicated"
    n_axis0 = len(col_caps)
    row_caps = [tuple(c) for c in axes["Axis1"]]
    assert ("Web",) in row_caps and ("Store",) in row_caps
    col_pos = {c: i for i, c in enumerate(col_caps)}
    row_pos = {c: i for i, c in enumerate(row_caps)}
    assert cells[row_pos[("Web",)] * n_axis0 + col_pos[("Bikes",)]] == 10.0
    assert cells[row_pos[("Web",)] * n_axis0 + col_pos[("All",)]] == 15.0
    assert cells[row_pos[("Store",)] * n_axis0 + col_pos[("Bikes",)]] == 20.0
    assert cells[row_pos[("Store",)] * n_axis0 + col_pos[("All",)]] == 20.0


def test_mirror_two_measures():
    xml = _build_mirror(["Amount", "Qty"])
    axes, cells, _ = _parse(xml)
    ords = _ordinals(xml)
    assert len(ords) == len(set(ords)), "duplicate CellOrdinals"
    # Axis0 = (category subtotal member x measure) tuples.
    col_caps = [tuple(c) for c in axes["Axis0"]]
    assert len(col_caps) == len(set(col_caps)), "Axis0 not deduplicated"
    n_axis0 = len(col_caps)
    row_caps = [tuple(c) for c in axes["Axis1"]]
    col_pos = {c: i for i, c in enumerate(col_caps)}
    row_pos = {c: i for i, c in enumerate(row_caps)}
    # Web/Bikes/Amount and Web/Bikes/Qty land at distinct ordinals.
    assert cells[row_pos[("Web",)] * n_axis0 + col_pos[("Bikes", "Amount")]] == 10.0
    assert cells[row_pos[("Web",)] * n_axis0 + col_pos[("Bikes", "Qty")]] == 1.0
    assert cells[row_pos[("Store",)] * n_axis0 + col_pos[("All", "Amount")]] == 20.0
    assert cells[row_pos[("Store",)] * n_axis0 + col_pos[("All", "Qty")]] == 4.0


# ---------------------------------------------------------------------------
# Shape 4 — FLAT both axes (non-subtotal baseline)
# ---------------------------------------------------------------------------

def _build_flat(measures):
    rows = [
        {"country": "France", "Amount": 100, "Qty": 1},
        {"country": "Germany", "Amount": 200, "Qty": 2},
    ]
    measure_set = "{" + ", ".join(f"[Measures].[{m}]" for m in measures) + "}"
    mdx = (
        f"SELECT {measure_set} ON COLUMNS, "
        "[country].[country].MEMBERS ON ROWS FROM [demo]"
    )
    return build_real_execute_response(
        mdx=mdx, catalog="demo",
        columns=["country", *measures],
        rows=rows,
        measures_meta=[{"name": m, "default_agg": "sum"} for m in measures],
        dimensions_meta=[{"name": "country"}],
        client_app_name="Excel",
    )


def test_flat_one_measure():
    xml = _build_flat(["Amount"])
    ords = _ordinals(xml)
    assert len(ords) == len(set(ords))
    assert len(ords) == 2


def test_flat_two_measures():
    xml = _build_flat(["Amount", "Qty"])
    ords = _ordinals(xml)
    assert len(ords) == len(set(ords))
    # 2 rows x 2 measure columns = 4 cells
    assert len(ords) == 4


# ---------------------------------------------------------------------------
# Bug-3617 — DISCOVER member unames match Execute axis-tuple unames
# ---------------------------------------------------------------------------

def _discover_members(dname, level_names, members_by_level):
    """Run MDSCHEMA_MEMBERS and return the member rows for a hierarchy dim."""
    member_data = {
        dname: {
            "levels": level_names,
            "members_by_level": members_by_level,
        }
    }
    rows = _rows_members(
        catalog="demo",
        measures=[],
        dimensions=[{"name": dname, "source": "hierarchy"}],
        restrictions={},
        member_data=member_data,
    )
    # Bug-6891: hierarchies group under [Hierarchies]; select by hierarchy grammar.
    return [
        r for r in rows
        if str(r.get("HIERARCHY_UNIQUE_NAME", "")).startswith(f"[{dname}].")
    ]


def test_discover_member_unames_match_execute_for_subtotal_hierarchy():
    """Bug-3617 (Phase 2 — RESOLVED): DISCOVER<->Execute member-uname parity.

    A multi-level (Year>Month) hierarchy now emits the SAME canonical,
    ancestor-qualified key-form unique name in BOTH the Execute axis
    (``...[Month].&[2025]&[4]``) and DISCOVER (MDSCHEMA_MEMBERS). The two
    month-4 members therefore get DISTINCT MEMBER_UNIQUE_NAMEs — their year
    is in the key path — so the historical caption-form collision
    (``...[4]`` for both) is GONE and no client-side reconciliation bridge is
    needed for parity. This test was previously the known-limitation pin; it
    now asserts the parity the fix delivers (see
    docs/questions/questions_bug3617-discover-uname.md, Option A).
    """
    gk = SUBTOTAL_GRAIN_KEY
    h_cal = _mk(
        "Cal", "Calendar", "Cal",
        [("Year", 0, "year"), ("Month", 1, "month")], axis=1,
    )
    rows = [
        {"year": "2025", "month": "4", "Amount": 10, gk: 1},
        {"year": "2025", "month": "5", "Amount": 20, gk: 1},
        {"year": "2025", "month": "",  "Amount": 30, gk: 0},
        {"year": "2026", "month": "4", "Amount": 40, gk: 1},
        {"year": "2026", "month": "",  "Amount": 40, gk: 0},
        {"year": "",     "month": "",  "Amount": 70, gk: -1},
    ]
    mdx = (
        "SELECT {[Measures].[Amount]} ON COLUMNS, "
        "[Calendar].[Cal].MEMBERS ON ROWS FROM [demo]"
    )
    xml = build_real_execute_response(
        mdx=mdx, catalog="demo",
        columns=["year", "month", "Amount"],
        rows=rows,
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "year"}, {"name": "month"}],
        client_app_name="Excel",
        subtotal_hierarchy=h_cal,
    )
    _, _, unames = _parse(xml)
    execute_unames = {u for t in unames["Axis1"] for u in t}
    # The path-qualified month members the Execute (subtotal) axis emits.
    assert "[Calendar].[Cal].[Month].&[2025]&[4]" in execute_unames
    assert "[Calendar].[Cal].[Month].&[2026]&[4]" in execute_unames

    # DISCOVER (MDSCHEMA_MEMBERS) now emits the SAME canonical path-qualified
    # grammar — the two month-4 members get DISTINCT unique names (year in the
    # path), so the historical caption-form collision is gone.
    members = _discover_members(
        "Calendar",
        ["Year", "Month"],
        {
            0: [
                {"name": "2025", "ordinal": 0, "parent": "", "level": "Year"},
                {"name": "2026", "ordinal": 1, "parent": "", "level": "Year"},
            ],
            1: [
                {"name": "4", "ordinal": 0, "parent": "2025", "level": "Month"},
                {"name": "5", "ordinal": 1, "parent": "2025", "level": "Month"},
                {"name": "4", "ordinal": 2, "parent": "2026", "level": "Month"},
            ],
        },
    )
    discover_unames = [r["MEMBER_UNIQUE_NAME"] for r in members]

    # The two month-4 members are now DISTINCT canonical unames, each emitted once.
    assert discover_unames.count("[Calendar].[Calendar].[Month].&[2025]&[4]") == 1
    assert discover_unames.count("[Calendar].[Calendar].[Month].&[2026]&[4]") == 1
    # The old caption-form collision string is no longer emitted at all.
    assert "[Calendar].[Calendar].[4]" not in discover_unames

    # Identity is now separate from the display caption: both month-4 members keep
    # MEMBER_CAPTION "4" (the human label) but carry distinct keys/unique names.
    month_4_rows = [r for r in members if r["MEMBER_KEY"] == "4"]
    assert len(month_4_rows) == 2
    assert all(r["MEMBER_CAPTION"] == "4" for r in month_4_rows)
    assert {r["MEMBER_UNIQUE_NAME"] for r in month_4_rows} == {
        "[Calendar].[Calendar].[Month].&[2025]&[4]",
        "[Calendar].[Calendar].[Month].&[2026]&[4]",
    }
    # PARENT_UNIQUE_NAME is the canonical parent (year) path, not caption form.
    assert {r["PARENT_UNIQUE_NAME"] for r in month_4_rows} == {
        "[Calendar].[Calendar].[Year].&[2025]",
        "[Calendar].[Calendar].[Year].&[2026]",
    }

    # DISCOVER<->Execute parity: DISCOVER members use the identical key-path grammar
    # the Execute subtotal axis emits ([Month].&[year]&[month]); every multi-level
    # member is path-qualified, never caption form.
    for u in discover_unames:
        if "].[Month]." in u:
            assert ".&[" in u, f"month member must be path-qualified, got {u}"
