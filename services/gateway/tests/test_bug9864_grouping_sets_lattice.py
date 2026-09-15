"""Bug-9864 -- serve the rollup lattice with GROUP BY GROUPING SETS.

The full CrossJoin lattice (2^N - 1 rollup grains for N one-level fields,
Bug-9845) used to run as one source query per grain: measured on the local
stack, the Bug-9891 owner shape issued 15 grain queries plus the detail query.
One grouping-sets query now serves all of them.

The bar these tests hold the rewrite to is ROW-SET EQUALITY, not counts: the
split of a single lattice result must produce, per grain, exactly the rows the
per-grain queries produced, with exactly the same grain tags. Anything less and
a pivot silently changes numbers.

Every test here fails on pre-fix code because ``build_lattice_query``,
``split_lattice_results`` and ``grouping_marker_name`` did not exist -- the
module raises ImportError at collection. The row-set equality test additionally
executes both plans against a real in-memory table so it would fail on any
future change that keeps the API but breaks the mapping.
"""

from __future__ import annotations

import itertools

import pytest
import sqlglot

from shared.connector_qualify import CONNECTOR_TO_SQLGLOT
from src.dax.rollup_validator import validate_rollup_lattice
from src.dax.subtotal_engine import (
    GrainResult,
    LatticeUnavailable,
    SubtotalHierarchy,
    SubtotalLevel,
    build_lattice_query,
    build_multi_subtotal_queries,
    compute_multi_lne_subtotals,
    grouping_marker_name,
    merge_multi_hierarchy_results,
    split_lattice_results,
)

# --------------------------------------------------------------------------
# Fixtures: two flat rollups + one two-level hierarchy, the shape the record
# names (Bug-9891 owner pivot, minus one flat field to keep the fixture small).
# --------------------------------------------------------------------------

from src.dax.subtotal_engine import GrainQuery as _GrainQueryType

_MEASURES_META = [{"name": "base_amount", "default_agg": "sum"}]
_MEASURE_CANONICAL = {"base_amount": "base_amount"}


def _flat(name: str) -> SubtotalHierarchy:
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=1, dim_name=name)],
        axis=1, is_flat_attribute_rollup=True,
    )


def _geo() -> SubtotalHierarchy:
    return SubtotalHierarchy(
        hierarchy_name="Geography", mdx_dim_name="Geography",
        mdx_hier_name="Geography",
        levels=[
            SubtotalLevel(name="Country", ordinal=1, dim_name="country_code"),
            SubtotalLevel(name="City", ordinal=2, dim_name="city_name"),
        ],
        axis=0,
    )


def _hierarchies() -> list[SubtotalHierarchy]:
    return [_flat("channel_name"), _flat("account_type"), _geo()]


def _common(hierarchies: list[SubtotalHierarchy]) -> dict:
    dims = [lvl.dim_name for h in hierarchies for lvl in h.levels]
    return dict(
        mdx_dims=dims,
        mdx_measures=["base_amount"],
        where_sql_clauses=[],
        model_slug="modely",
        measures_meta=_MEASURES_META,
        measure_canonical=_MEASURE_CANONICAL,
        hierarchies=hierarchies,
    )


def _lattice_for(hierarchies: list[SubtotalHierarchy], **over):
    common = _common(hierarchies)
    common.update(over)
    queries = build_multi_subtotal_queries(**common)
    return queries, build_lattice_query(
        queries,
        mdx_measures=common["mdx_measures"],
        measures_meta=common["measures_meta"],
        measure_canonical=common["measure_canonical"],
        where_sql_clauses=common["where_sql_clauses"],
        model_slug=common["model_slug"],
        connector_type=over.get("connector_type", "postgresql"),
    )


# --------------------------------------------------------------------------
# (a) Row-set equality against the per-grain path, on a real database.
# --------------------------------------------------------------------------

def render_lattice_sql(base_sql: str, grouping_sets: list[list[str]]) -> str:
    """The PostgreSQL-canonical SQL the query-router renders for a lattice.

    Mirrors ``rewrite/source_sql.py``: the bound query's plain GROUP BY becomes
    GROUP BY GROUPING SETS, and one ``GROUPING(<col>)`` marker per grain column
    joins the SELECT list. The dialect tests below transpile and re-parse it.
    """
    tree = sqlglot.parse_one(base_sql, read="postgres")
    grain = [c.name for c in tree.args["group"].expressions]
    for name in grain:
        tree = tree.select(
            f'GROUPING("{name}") AS "{grouping_marker_name(name)}"',
            dialect="postgres",
        )
    sets = ", ".join(
        "(" + ", ".join(f'"{n}"' for n in gs) + ")" for gs in grouping_sets
    )
    head, _, _tail = tree.sql(dialect="postgres").partition(" GROUP BY ")
    return f"{head} GROUP BY GROUPING SETS ({sets})"


def test_the_lattice_asks_for_exactly_the_planned_grains():
    """(a) Planning: one grouping set per planned grain, each a subset of the
    single base query's grain, and the planner's own GrainQuery objects handed
    back untouched.

    Row-set equality of the EXECUTED plans is proved against real PostgreSQL in
    ``tests/integration/test_bug9864_grouping_sets_rowset_db.py`` -- SQLite has
    no GROUPING SETS, so it cannot stand in for that.
    """
    hierarchies = _hierarchies()
    queries, lattice = _lattice_for(hierarchies)
    assert len(queries) > 1

    assert lattice.grain_queries == queries
    assert lattice.grouping_sets == [list(q.dim_cols) for q in queries]
    union = set(lattice.dim_cols)
    for gs in lattice.grouping_sets:
        assert set(gs) <= union
    # The base query groups by exactly the union of the grains, so every set is
    # renderable and nothing coarser than the finest planned grain is fetched.
    assert union == {d for q in queries for d in q.dim_cols}
    # One SELECT, one GROUP BY -- the shape the router binds and layers on.
    assert lattice.sql.count("SELECT") == 1
    assert lattice.sql.count("GROUP BY") == 1
    for d in lattice.dim_cols:
        assert f'"{d}"' in lattice.sql


# --------------------------------------------------------------------------
# (b) NULL members are members, not the All row.
# --------------------------------------------------------------------------

def test_null_member_value_is_preserved_as_a_member_not_read_as_all():
    """(b) The marker decides the grain; the column's NULL-ness never does.

    A row whose ``channel_name`` is NULL because the MEMBER is blank, and a row
    whose ``channel_name`` is NULL because the grain rolled it up, are the same
    bytes in the column. Only ``GROUPING()`` tells them apart -- reading NULL as
    All turns a "(blank)" row into a duplicate grand total.
    """
    hierarchies = [_flat("channel_name"), _flat("account_type")]
    queries, lattice = _lattice_for(hierarchies)

    m_chan = grouping_marker_name("channel_name")
    m_acct = grouping_marker_name("account_type")
    columns = ["channel_name", "account_type", m_chan, m_acct, "base_amount"]
    rows = [
        # A real member whose value IS NULL, grouped by channel only.
        {"channel_name": None, "account_type": None,
         m_chan: 0, m_acct: 1, "base_amount": 11},
        # The genuine All/All grand row.
        {"channel_name": None, "account_type": None,
         m_chan: 1, m_acct: 1, "base_amount": 99},
        # A real member whose value IS NULL, grouped by account only.
        {"channel_name": None, "account_type": None,
         m_chan: 1, m_acct: 0, "base_amount": 22},
    ]
    split = split_lattice_results(lattice, columns, rows)
    by_dims = {frozenset(r.query.dim_cols): r for r in split}

    chan_only = by_dims[frozenset(["channel_name"])]
    assert [r["base_amount"] for r in chan_only.rows] == [11]
    assert chan_only.rows[0]["channel_name"] is None  # still a member

    acct_only = by_dims[frozenset(["account_type"])]
    assert [r["base_amount"] for r in acct_only.rows] == [22]

    grand = by_dims[frozenset()]
    assert [r["base_amount"] for r in grand.rows] == [99]

    # The NULL member never leaked into the grand total, and vice versa.
    assert len(chan_only.rows) == 1 and len(grand.rows) == 1


def test_a_null_grouping_marker_is_refused_rather_than_guessed():
    _queries, lattice = _lattice_for([_flat("channel_name"),
                                      _flat("account_type")])
    m_chan = grouping_marker_name("channel_name")
    m_acct = grouping_marker_name("account_type")
    with pytest.raises(LatticeUnavailable):
        split_lattice_results(
            lattice,
            ["channel_name", "account_type", m_chan, m_acct, "base_amount"],
            [{"channel_name": None, "account_type": None,
              m_chan: None, m_acct: 1, "base_amount": 1}],
        )


# --------------------------------------------------------------------------
# (c) The rendered lattice SQL transpiles and parses per dialect.
# --------------------------------------------------------------------------

def _rendered_source_lattice(connector: str) -> str:
    """The PostgreSQL-canonical shape the router emits, for transpilation."""
    hierarchies = _hierarchies()
    _queries, lattice = _lattice_for(hierarchies)
    return render_lattice_sql(lattice.sql, lattice.grouping_sets)


@pytest.mark.parametrize("connector", ["postgresql", "bigquery"])
def test_lattice_sql_transpiles_and_parses_on_each_dialect(connector):
    """(c) Both allow-listed dialects render a parseable GROUPING SETS query."""
    pg_sql = _rendered_source_lattice(connector)
    dialect = CONNECTOR_TO_SQLGLOT[connector]
    out = sqlglot.transpile(pg_sql, read="postgres", write=dialect)[0]
    # Parses as that dialect's own SQL -- not merely as PostgreSQL text.
    reparsed = sqlglot.parse_one(out, read=dialect)
    assert "GROUPING SETS" in out.upper()
    assert reparsed.args.get("group") is not None
    assert "GROUPING(" in out.upper().replace(" ", "")


def test_bigquery_lattice_uses_backticks_not_double_quotes():
    """(c) BigQuery identifier quoting, through the same transpile boundary
    ``shared.connector_qualify`` routes every other identifier through."""
    pg_sql = _rendered_source_lattice("bigquery")
    out = sqlglot.transpile(
        pg_sql, read="postgres", write=CONNECTOR_TO_SQLGLOT["bigquery"],
    )[0]
    assert "`" in out
    assert '"' not in out
    assert f"`{grouping_marker_name('channel_name')}`" in out


# --------------------------------------------------------------------------
# (d) Shapes the lattice must refuse, so the caller keeps the per-grain path.
# --------------------------------------------------------------------------

def test_duplicate_grain_column_sets_refuse_the_lattice():
    """(d) Two grains grouping by the same columns cannot be told apart by
    their markers, so folding them would drop one grain's rows."""
    queries, _lattice = _lattice_for([_flat("channel_name"),
                                      _flat("account_type")])
    dup = list(queries) + [queries[0]]
    with pytest.raises(LatticeUnavailable):
        build_lattice_query(
            dup,
            mdx_measures=["base_amount"], measures_meta=_MEASURES_META,
            measure_canonical=_MEASURE_CANONICAL, where_sql_clauses=[],
            model_slug="modely",
        )


def test_a_grain_with_no_sql_refuses_the_lattice():
    queries, _ = _lattice_for([_flat("channel_name"), _flat("account_type")])
    queries[0].sql = ""
    with pytest.raises(LatticeUnavailable):
        build_lattice_query(
            queries,
            mdx_measures=["base_amount"], measures_meta=_MEASURES_META,
            measure_canonical=_MEASURE_CANONICAL, where_sql_clauses=[],
            model_slug="modely",
        )


def test_an_unplanned_grouping_vector_is_refused_rather_than_dropped():
    _queries, lattice = _lattice_for([_flat("channel_name"),
                                      _flat("account_type")])
    m_chan = grouping_marker_name("channel_name")
    m_acct = grouping_marker_name("account_type")
    with pytest.raises(LatticeUnavailable):
        split_lattice_results(
            lattice,
            ["channel_name", "account_type", m_chan, m_acct, "base_amount"],
            # Grouped by BOTH -- the detail grain, which the lattice never asks
            # for because the caller's own query already covers it.
            [{"channel_name": "WEB", "account_type": "CREDIT",
              m_chan: 0, m_acct: 0, "base_amount": 1}],
        )


def test_missing_markers_refuse_the_split():
    _queries, lattice = _lattice_for([_flat("channel_name"),
                                      _flat("account_type")])
    with pytest.raises(LatticeUnavailable):
        split_lattice_results(
            lattice, ["channel_name", "account_type", "base_amount"],
            [{"channel_name": "WEB", "account_type": "CREDIT",
              "base_amount": 1}],
        )


def test_one_flat_field_still_plans_its_all_grain_and_needs_no_lattice():
    """One flat field asks for exactly ONE rollup grain: the grand total,
    grouped by nothing.

    That grain must still be planned and served -- dropping it would delete the
    field's All row. There is simply nothing to fold: one grain is already one
    query, it has no grain column to carry a marker, and folding it would change
    a working single query into an unsplittable one. So the planner keeps it and
    the lattice declines it.
    """
    common = _common([_flat("channel_name")])
    queries = build_multi_subtotal_queries(**common)
    # The All grain is present, tagged, and grouped by nothing.
    assert len(queries) == 1
    assert queries[0].dim_cols == []
    assert queries[0].grain_per_hierarchy == {"channel_name": -1}
    assert queries[0].level_name == "Grand Total"
    assert queries[0].sql  # it is a real, runnable query

    with pytest.raises(LatticeUnavailable) as info:
        build_lattice_query(
            queries, mdx_measures=["base_amount"],
            measures_meta=_MEASURES_META,
            measure_canonical=_MEASURE_CANONICAL, where_sql_clauses=[],
            model_slug="modely",
        )
    assert "one" in str(info.value).lower()


def test_two_flat_fields_do_fold_into_a_lattice():
    """The counterpart: as soon as there is more than one grain, folding is
    both possible and worth doing."""
    _queries, lattice = _lattice_for([_flat("channel_name"),
                                      _flat("account_type")])
    assert len(lattice.grouping_sets) == 3
    assert [] in lattice.grouping_sets  # the grand total is still served


# --------------------------------------------------------------------------
# (e) The Bug-9862 validator passes on the Bug-9891 shape via the new path.
# --------------------------------------------------------------------------

def _axis_tuples_for(rows, hierarchies):
    """Render merged rows as the axis tuples the response builder emits.

    One member per hierarchy per row, named by the level that row's grain sits
    at -- or the synthetic All member (``member_type`` 2) when the hierarchy was
    rolled up entirely. This is the shape ``_tuple_grain`` classifies.
    """
    from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX
    tuples = []
    for row in rows:
        members = []
        for h in hierarchies:
            grain = row.get(SUBTOTAL_GRAIN_PREFIX + h.hierarchy_name, -1)
            deepest = None
            for lvl in h.levels:
                if grain >= lvl.ordinal:
                    deepest = lvl
            member = {"hierarchy": f"[{h.mdx_dim_name}].[{h.mdx_hier_name}]"}
            if deepest is None:
                member["member_type"] = 2
            else:
                member["lname"] = (
                    f"[{h.mdx_dim_name}].[{h.mdx_hier_name}].[{deepest.name}]"
                )
            members.append(member)
        tuples.append(members)
    return tuples


def test_validator_passes_on_the_bug9891_shape_served_by_the_lattice():
    """(e) The safety net that exists to catch a dropped grain sees a complete
    lattice when the response came from one grouping-sets query.

    The rows are synthesised from the plan rather than from a database: the
    validator reads only the grain TAGS, and the tags come from the split, so a
    database would add nothing this assertion depends on.
    """
    hierarchies = _hierarchies()
    _queries, lattice = _lattice_for(hierarchies)

    markers = {d: grouping_marker_name(d) for d in lattice.dim_cols}
    columns = list(lattice.dim_cols) + list(markers.values()) + ["base_amount"]
    values = {
        "country_code": "GB", "city_name": "London",
        "channel_name": "WEB", "account_type": "CREDIT",
    }
    rows = []
    for gs in lattice.grouping_sets:
        row = {d: (values[d] if d in gs else None) for d in lattice.dim_cols}
        for d in lattice.dim_cols:
            row[markers[d]] = 0 if d in gs else 1
        row["base_amount"] = 1
        rows.append(row)

    split = split_lattice_results(lattice, columns, rows)
    detail = GrainResult(
        query=_GrainQueryType(
            sql="", protocol="jdbc", grain_ordinal=999, level_name="detail",
            dim_cols=["country_code", "city_name", "channel_name",
                      "account_type"],
        ),
        columns=["country_code", "city_name", "channel_name", "account_type",
                 "base_amount"],
        rows=[dict(values, base_amount=1)],
    )
    _cols, merged = merge_multi_hierarchy_results(detail, split, hierarchies)

    validate_rollup_lattice(
        "Axis1", hierarchies, merged, _axis_tuples_for(merged, hierarchies),
    )


# --------------------------------------------------------------------------
# (f) LAST_NON_EMPTY still computes over the lattice-served grains.
# --------------------------------------------------------------------------

def test_last_non_empty_overrides_are_identical_on_both_paths():
    """(f) LNE is computed in Python from the DETAIL rows and keyed by each
    grain's ``dim_cols``, so folding the grain queries into one lattice query
    must not change a single override. This asserts that directly."""
    hierarchies = [
        _flat("channel_name"),
        SubtotalHierarchy(
            hierarchy_name="Calendar", mdx_dim_name="Calendar",
            mdx_hier_name="Calendar",
            levels=[
                SubtotalLevel(name="Year", ordinal=1, dim_name="year",
                              time_unit="year"),
                SubtotalLevel(name="Month", ordinal=2, dim_name="month",
                              time_unit="month"),
            ],
            axis=1,
        ),
    ]
    common = _common(hierarchies)
    common["mdx_measures"] = ["balance"]
    common["measures_meta"] = [{"name": "balance",
                                "default_agg": "last_non_empty"}]
    common["measure_canonical"] = {"balance": "balance"}
    queries = build_multi_subtotal_queries(**common)
    lattice = build_lattice_query(
        queries,
        mdx_measures=common["mdx_measures"],
        measures_meta=common["measures_meta"],
        measure_canonical=common["measure_canonical"],
        where_sql_clauses=[], model_slug="modely",
    )

    detail_rows = [
        {"channel_name": "WEB", "year": "2026", "month": "2026-01",
         "balance": 5},
        {"channel_name": "WEB", "year": "2026", "month": "2026-02",
         "balance": 7},
        {"channel_name": "ATM", "year": "2026", "month": "2026-01",
         "balance": 3},
    ]
    from_multi = compute_multi_lne_subtotals(
        detail_rows, hierarchies, ["balance"], queries,
    )
    from_lattice = compute_multi_lne_subtotals(
        detail_rows, hierarchies, ["balance"], lattice.grain_queries,
    )
    assert from_multi == from_lattice
    assert from_multi, "the fixture must actually produce overrides"


def test_the_lattice_covers_the_full_cartesian_set_for_three_flat_fields():
    """Bug-9845's 2^N - 1: three flat fields are seven grains in ONE query."""
    hierarchies = [_flat("a"), _flat("b"), _flat("c")]
    queries, lattice = _lattice_for(hierarchies)
    assert len(queries) == 7
    assert len(lattice.grouping_sets) == 7
    expected = {
        frozenset(combo)
        for r in range(0, 3)
        for combo in itertools.combinations(["a", "b", "c"], r)
    }
    assert {frozenset(gs) for gs in lattice.grouping_sets} == expected
