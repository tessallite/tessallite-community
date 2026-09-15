"""Bug-9864 -- the lattice returns EXACTLY the per-grain rows, on real PostgreSQL.

The unit suite (``tests/test_bug9864_grouping_sets_lattice.py``) proves the
planning and the marker split in isolation. This one proves the claim that
matters to a user: run BOTH plans against a real database that actually
implements ``GROUP BY GROUPING SETS`` and ``GROUPING()``, and assert row-set
equality per grain -- the same rows, the same measure values, the same grain
tags. Counts would not catch a mapping that shuffles rows between grains.

Requires a PostgreSQL URL::

    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      python -m pytest tests/integration/test_bug9864_grouping_sets_rowset_db.py

``scripts/run-like-ci.sh gateway`` provides it. SQLite cannot stand in here --
it has no GROUPING SETS at all, which is precisely why this test exists
separately from the unit suite.

Fails on pre-fix code: ``build_lattice_query`` / ``split_lattice_results`` do
not exist, so the module fails to import.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

import pytest
import sqlglot
from sqlalchemy.ext.asyncio import create_async_engine

from src.dax.subtotal_engine import (
    GrainResult,
    SubtotalHierarchy,
    SubtotalLevel,
    build_lattice_query,
    build_multi_subtotal_queries,
    grouping_marker_name,
    merge_multi_hierarchy_results,
    split_lattice_results,
)

pytestmark = pytest.mark.asyncio

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)

# One row per (country, city, channel, account) so every rollup grain has a
# distinct, hand-checkable sum. The NULL city_name is deliberate: it is a real
# member whose value is NULL, and it must never be confused with the All row
# that the City column rolls up into.
_FACT_ROWS = [
    ("GB", "London", "WEB", "CREDIT", 10),
    ("GB", "London", "WEB", "DEBIT", 20),
    ("GB", "London", "ATM", "CREDIT", 30),
    ("GB", "Leeds", "ATM", "DEBIT", 40),
    ("GB", None, "WEB", "CREDIT", 45),
    ("US", "Austin", "WEB", "CREDIT", 50),
    ("US", "Austin", "ATM", "DEBIT", 60),
    ("US", "Boston", "WEB", "DEBIT", 70),
]

_MEASURES_META = [{"name": "base_amount", "default_agg": "sum"}]
_MEASURE_CANONICAL = {"base_amount": "base_amount"}


def _flat(name: str) -> SubtotalHierarchy:
    return SubtotalHierarchy(
        hierarchy_name=name, mdx_dim_name=name, mdx_hier_name=name,
        levels=[SubtotalLevel(name=name, ordinal=1, dim_name=name)],
        axis=1, is_flat_attribute_rollup=True,
    )


def _hierarchies() -> list[SubtotalHierarchy]:
    """Two flat rollups plus one two-level hierarchy -- the record's shape."""
    return [
        _flat("channel_name"),
        _flat("account_type"),
        SubtotalHierarchy(
            hierarchy_name="Geography", mdx_dim_name="Geography",
            mdx_hier_name="Geography",
            levels=[
                SubtotalLevel(name="Country", ordinal=1,
                              dim_name="country_code"),
                SubtotalLevel(name="City", ordinal=2, dim_name="city_name"),
            ],
            axis=0,
        ),
    ]


def _plan(hierarchies):
    dims = [lvl.dim_name for h in hierarchies for lvl in h.levels]
    queries = build_multi_subtotal_queries(
        mdx_dims=dims, mdx_measures=["base_amount"], where_sql_clauses=[],
        model_slug="modely", measures_meta=_MEASURES_META,
        measure_canonical=_MEASURE_CANONICAL, hierarchies=hierarchies,
    )
    lattice = build_lattice_query(
        queries, mdx_measures=["base_amount"], measures_meta=_MEASURES_META,
        measure_canonical=_MEASURE_CANONICAL, where_sql_clauses=[],
        model_slug="modely",
    )
    return queries, lattice


def render_lattice_sql(base_sql: str, grouping_sets: list[list[str]]) -> str:
    """The PostgreSQL SQL the query-router renders for a lattice request.

    Mirrors ``rewrite/source_sql.py``: the bound query's plain GROUP BY becomes
    GROUP BY GROUPING SETS, and one ``GROUPING(<col>)`` marker per grain column
    joins the SELECT list.
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


@asynccontextmanager
async def _fact_schema():
    if not _DB_URL:
        pytest.skip("no versioning/importer Postgres URL configured")
    schema = f"bug9864_{uuid.uuid4().hex}"
    engine = create_async_engine(_DB_URL, future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(
            f'CREATE TABLE "{schema}"."modely" ('
            '"country_code" TEXT, "city_name" TEXT, "channel_name" TEXT, '
            '"account_type" TEXT, "base_amount" NUMERIC)'
        )
        for row in _FACT_ROWS:
            vals = ", ".join(
                "NULL" if v is None
                else (f"'{v}'" if isinstance(v, str) else str(v))
                for v in row
            )
            await conn.exec_driver_sql(
                f'INSERT INTO "{schema}"."modely" VALUES ({vals})'
            )
    try:
        yield engine, schema
    finally:
        await engine.dispose()
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await drop.dispose()


async def _run(engine, schema: str, sql: str):
    """Execute gateway-canonical SQL against the fixture schema."""
    scoped = sql.replace('"modely"', f'"{schema}"."modely"')
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql(scoped)
        cols = list(result.keys())
        return cols, [dict(zip(cols, r)) for r in result.fetchall()]


def _row_key(row: dict, dims: list[str]) -> tuple:
    return tuple(row.get(d) for d in dims) + (int(row["base_amount"]),)


def _detail_stub(queries):
    detail_dims = ["country_code", "city_name", "channel_name", "account_type"]
    return GrainResult(
        query=type(queries[0])(
            sql="", protocol="jdbc", grain_ordinal=999, level_name="detail",
            dim_cols=detail_dims,
        ),
        columns=detail_dims + ["base_amount"],
        rows=[],
    )


async def test_lattice_rows_equal_the_per_grain_rows_on_postgresql():
    """Row-set equality per grain, both plans executed on real PostgreSQL."""
    hierarchies = _hierarchies()
    queries, lattice = _plan(hierarchies)
    assert len(queries) > 1, "the fixture must plan a real lattice"

    async with _fact_schema() as (engine, schema):
        per_grain = []
        for q in queries:
            cols, rows = await _run(engine, schema, q.sql)
            per_grain.append(GrainResult(query=q, columns=cols, rows=rows))

        lat_cols, lat_rows = await _run(
            engine, schema,
            render_lattice_sql(lattice.sql, lattice.grouping_sets),
        )

    # The engine really did return every grain in ONE result set.
    assert len(lat_rows) == sum(len(r.rows) for r in per_grain)

    split = split_lattice_results(lattice, lat_cols, lat_rows)
    assert len(split) == len(per_grain)

    by_level = {r.query.level_name: r for r in split}
    for expected in per_grain:
        got = by_level[expected.query.level_name]
        # Grain identity -- what the merge and the Bug-9862 validator key on.
        assert got.query.grain_ordinal == expected.query.grain_ordinal
        assert got.query.grain_per_hierarchy == expected.query.grain_per_hierarchy
        assert got.query.dim_cols == expected.query.dim_cols
        dims = expected.query.dim_cols
        assert {_row_key(r, dims) for r in got.rows} == {
            _row_key(r, dims) for r in expected.rows
        }, f"grain {expected.query.level_name} differs"

    # And the merged response is identical end to end.
    cols_a, rows_a = merge_multi_hierarchy_results(
        _detail_stub(queries), per_grain, hierarchies,
    )
    cols_b, rows_b = merge_multi_hierarchy_results(
        _detail_stub(queries), split, hierarchies,
    )
    assert cols_a == cols_b
    assert rows_a == rows_b


async def test_null_member_is_not_read_as_the_all_row_on_postgresql():
    """The fixture's NULL ``city_name`` is a real member.

    In the lattice result its column is NULL, and so is the City column of every
    row that rolled City up -- identical bytes. Only ``GROUPING()`` separates
    them. This asserts the split keeps the NULL member inside the City grain and
    out of the Country grain, and the sums prove it.
    """
    hierarchies = _hierarchies()
    _queries, lattice = _plan(hierarchies)

    async with _fact_schema() as (engine, schema):
        lat_cols, lat_rows = await _run(
            engine, schema,
            render_lattice_sql(lattice.sql, lattice.grouping_sets),
        )

    # At least one lattice row is a genuine NULL member rather than a rollup.
    marker = grouping_marker_name("city_name")
    assert any(
        r["city_name"] is None and int(r[marker]) == 0 for r in lat_rows
    ), "fixture must produce a NULL city member"

    split = split_lattice_results(lattice, lat_cols, lat_rows)
    by_dims = {frozenset(r.query.dim_cols): r for r in split}

    # The grain that groups by Country only (City rolled up) must carry GB's
    # WHOLE total, the NULL-city row included.
    country_only = by_dims[frozenset(["country_code"])]
    gb = [r for r in country_only.rows if r["country_code"] == "GB"]
    assert len(gb) == 1
    assert int(gb[0]["base_amount"]) == 10 + 20 + 30 + 40 + 45

    # The grain that groups by Country AND City keeps the NULL city as its own
    # member -- it did not collapse into GB's rollup and did not vanish.
    country_city = by_dims[frozenset(["country_code", "city_name"])]
    null_city = [
        r for r in country_city.rows
        if r["country_code"] == "GB" and r["city_name"] is None
    ]
    assert len(null_city) == 1
    assert int(null_city[0]["base_amount"]) == 45
    assert {r["city_name"] for r in country_city.rows} == {
        "London", "Leeds", None, "Austin", "Boston",
    }
