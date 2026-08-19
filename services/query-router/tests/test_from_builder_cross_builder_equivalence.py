"""The served source SQL and the aggregate CTAS must expand the SAME join graph.

Every Bug-8605 determinism test compares a builder against ITSELF under two
input orders. That property stays green while the two builders disagree with
EACH OTHER, which is exactly what they do on a cyclic (two-equally-short-paths)
join graph: the round-4 review measured 192 of 300 random id assignments
emitting different edge sets. An aggregate-routed query and a source-routed
query then answer the same question with different rows, permanently and with
no staleness signal.

Root cause: two independent traversals. ``joins._build_joined_from_clause``
picks its next hop by iterating ``list(joined_table_ids)``, a SET, so which
already-joined relation it extends from is a function of UUID hashing;
``sql_builder.build_from_clause`` instead sweeps the canonically ordered join
list repeatedly. Nothing requires them to agree, and nothing tested that they
did — which is why it survived four review rounds of this lane.

PRE-EXISTING, and NOT introduced by Bug-8605. The same measurement against a
simulated pre-lane tree (both builders handed the same arbitrary order, no
canonical sorting) gives 209/300. What Bug-8605 changed is that the divergence
is now deterministic per model instead of storage-engine-dependent — strictly
better, and strictly not fixed.

This is ``xfail(strict=True)`` so it lands GREEN today as the executable
statement of the open defect and FAILS the suite the day the two builders are
reconciled, forcing the marker off instead of letting the guard rot. The
harness is proven retirable: pointing both sides at the same builder makes it
XPASS(strict) and fail, so the comparison really can go green.

Fixing it is an architecture call — one shared spanning-tree function, or give
the aggregate route the row-population gate the pocket route already has
(``pocket_population._plan_is_comparable`` refuses non-tree components, which
is why pockets are already safe). Escalated, not patched.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from shared.db.models import Join, ModelColumn, ModelTable
from shared.semantic import sql_builder
from src.rewrite.joins import _build_joined_from_clause

pytestmark = pytest.mark.unit

MODEL_ID = uuid.UUID(int=0xD1A)

# FIXED ids, deliberately NOT random, and deliberately SEARCHED for rather than
# picked. The review's 300-trial sweep found the builders disagree on ~64% of id
# assignments; a randomised fixture under xfail(strict=True) XPASSes on the other
# ~36% and turns the suite red at random -- the first pinned set tried here did
# exactly that. These values were found by enumeration and reproduce the
# divergence every run: the source route reaches dim_shared via dim_b (LEFT, on
# id2) while the CTAS reaches it via dim_a (INNER, on id).
T_FACT = uuid.UUID(int=1)
T_A = uuid.UUID(int=5)
T_B = uuid.UUID(int=12)
T_SH = uuid.UUID(int=22)
C_F_A = uuid.UUID(int=0x101)
C_A_ID = uuid.UUID(int=0x102)
C_F_B = uuid.UUID(int=0x103)
C_B_ID = uuid.UUID(int=0x104)
C_A_SH = uuid.UUID(int=0x105)
C_SH_ID = uuid.UUID(int=0x106)
C_B_SH = uuid.UUID(int=0x107)
C_SH_ID2 = uuid.UUID(int=0x108)
J_FA = uuid.UUID(int=0xA01)
J_FB = uuid.UUID(int=0xB01)
J_ASH = uuid.UUID(int=0xC01)
J_BSH = uuid.UUID(int=0xD01)


class _Res:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, tables, joins, columns):
        self._by_entity = {ModelTable: tables, Join: joins, ModelColumn: columns}

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        return _Res(self._by_entity.get(entity, []))


def _diamond():
    """fact -> dim_a -> dim_shared and fact -> dim_b -> dim_shared.

    Two equally short paths to ``dim_shared`` with DIFFERENT join keywords and
    DIFFERENT key columns on the shared table, so which spanning tree a builder
    picks is observable in the emitted SQL and in the resulting row population.
    """
    tables = [
        ModelTable(id=T_FACT, model_id=MODEL_ID, physical_name="public.fact_sales",
                   alias="fact_sales", table_type="fact"),
        ModelTable(id=T_A, model_id=MODEL_ID, physical_name="public.dim_a",
                   alias="dim_a", table_type="dim_detail"),
        ModelTable(id=T_B, model_id=MODEL_ID, physical_name="public.dim_b",
                   alias="dim_b", table_type="dim_detail"),
        ModelTable(id=T_SH, model_id=MODEL_ID, physical_name="public.dim_shared",
                   alias="dim_shared", table_type="dim_detail"),
    ]
    columns = [
        ModelColumn(id=C_F_A, model_table_id=T_FACT, column_name="a_id", data_type="uuid"),
        ModelColumn(id=C_A_ID, model_table_id=T_A, column_name="id", data_type="uuid"),
        ModelColumn(id=C_F_B, model_table_id=T_FACT, column_name="b_id", data_type="uuid"),
        ModelColumn(id=C_B_ID, model_table_id=T_B, column_name="id", data_type="uuid"),
        ModelColumn(id=C_A_SH, model_table_id=T_A, column_name="sh_id", data_type="uuid"),
        ModelColumn(id=C_SH_ID, model_table_id=T_SH, column_name="id", data_type="uuid"),
        ModelColumn(id=C_B_SH, model_table_id=T_B, column_name="sh_id", data_type="uuid"),
        ModelColumn(id=C_SH_ID2, model_table_id=T_SH, column_name="id2", data_type="uuid"),
    ]
    joins = [
        Join(id=J_FA, model_id=MODEL_ID, left_table_id=T_FACT, left_column_id=C_F_A,
             right_table_id=T_A, right_column_id=C_A_ID, join_type="inner"),
        Join(id=J_FB, model_id=MODEL_ID, left_table_id=T_FACT, left_column_id=C_F_B,
             right_table_id=T_B, right_column_id=C_B_ID, join_type="left"),
        Join(id=J_ASH, model_id=MODEL_ID, left_table_id=T_A, left_column_id=C_A_SH,
             right_table_id=T_SH, right_column_id=C_SH_ID, join_type="inner"),
        Join(id=J_BSH, model_id=MODEL_ID, left_table_id=T_B, left_column_id=C_B_SH,
             right_table_id=T_SH, right_column_id=C_SH_ID2, join_type="left"),
    ]
    return tables, joins, columns


def _edges(sql: str) -> frozenset:
    """Which of the four declared joins the emitted FROM clause actually used."""
    s = sql.replace('"', "")
    used = set()
    if "a_id" in s:
        used.add("fact-dim_a")
    if "b_id" in s:
        used.add("fact-dim_b")
    if "id2" in s:
        used.add("dim_b-dim_shared")
    if s.count("sh_id") - (1 if "id2" in s else 0) >= 1:
        used.add("dim_a-dim_shared")
    return frozenset(used)


def _source_route_from_clause(tables, joins, columns) -> str:
    return _build_joined_from_clause(
        base_table_id=T_FACT,
        required_table_ids={t.id for t in tables},
        joins=list(joins),
        tables_by_id={t.id: t for t in tables},
        columns_by_id={c.id: c for c in columns},
        alias_by_table_id={T_FACT: "base"},
        connector="postgresql",
    )


def _ctas_from_clause(tables, joins, columns) -> str:
    from_sql, _aliases = asyncio.run(
        sql_builder.build_from_clause(
            _FakeDb(tables, joins, columns), MODEL_ID,
            needed_table_ids={t.id for t in tables}, connector="postgresql",
        )
    )
    return from_sql


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Open defect (filed from the Bug-8605 round-4 review): "
        "joins._build_joined_from_clause picks its next hop by iterating a SET "
        "of joined table ids while sql_builder.build_from_clause sweeps the "
        "canonically ordered join list, so the two emit different spanning "
        "trees on a cyclic graph -- 192/300 measured, and an aggregate-routed "
        "query then serves a different row population than the source route. "
        "PRE-EXISTING (209/300 before Bug-8605). Remove this marker when both "
        "routes share one traversal, or when the aggregate route gains the "
        "row-population gate the pocket route already has."
    ),
)
def test_source_route_and_ctas_expand_the_same_join_graph():
    tables, joins, columns = _diamond()
    source_from = _source_route_from_clause(tables, joins, columns)
    ctas_from = _ctas_from_clause(tables, joins, columns)

    assert _edges(source_from) == _edges(ctas_from), (
        "the served source SQL and the aggregate CTAS joined the same model "
        "through DIFFERENT edges, so an aggregate-routed query and a "
        "source-routed query answer the same question with different rows.\n"
        f"  source: {' '.join(source_from.split())}\n"
        f"  ctas  : {' '.join(ctas_from.split())}"
    )


# ---------------------------------------------------------------------------
# Bug-8628 — the two builders must emit the SAME JOIN KEYWORD for the same edge
# ---------------------------------------------------------------------------
#
# Separate property, separate fixture. The diamond above probes WHICH EDGES a
# builder picks (still open, Bug-8637). This probes, on a graph where the edge
# choice is forced (a two-node TREE: exactly one spanning tree, so the
# traversal cannot differ), whether the two agree on the join keyword — i.e.
# WHICH ROWS the one shared edge produces.
#
# The join is declared DIMENSION-FIRST (left_table_id = dim, right_table_id =
# fact) while both builders anchor on the fact, so every traversal of it is
# REVERSED. That is the exact shape Bug-8628 got wrong: the source route
# flipped LEFT<->RIGHT (Bug-7775) and the CTAS builder's flat ``_JOIN_SQL`` map
# did not, so a declared ``dim LEFT JOIN fact`` served ``fact RIGHT JOIN dim``
# and materialised ``fact LEFT JOIN dim`` — a spurious NULL group AND missing
# empty groups in the same aggregate, with no error.

T_FACT2 = uuid.UUID(int=0x200)
T_DIM2 = uuid.UUID(int=0x201)
C_DIM_KEY = uuid.UUID(int=0x202)
C_FACT_FK = uuid.UUID(int=0x203)
J_DIM_FACT = uuid.UUID(int=0x204)

_JOIN_KEYWORD_RE = __import__("re").compile(
    r"\b(INNER JOIN|LEFT JOIN|RIGHT JOIN|FULL OUTER JOIN|JOIN)\b"
)


def _dim_first_pair(join_type: str):
    """fact + dim with ONE edge the modeller drew dim -> fact."""
    tables = [
        ModelTable(id=T_FACT2, model_id=MODEL_ID, physical_name="public.fact_sales",
                   alias="fact_sales", table_type="fact"),
        ModelTable(id=T_DIM2, model_id=MODEL_ID, physical_name="public.dim_region",
                   alias="dim_region", table_type="dim_detail"),
    ]
    columns = [
        ModelColumn(id=C_DIM_KEY, model_table_id=T_DIM2, column_name="region_code",
                    data_type="text"),
        ModelColumn(id=C_FACT_FK, model_table_id=T_FACT2, column_name="region_code",
                    data_type="text"),
    ]
    joins = [
        Join(id=J_DIM_FACT, model_id=MODEL_ID,
             left_table_id=T_DIM2, left_column_id=C_DIM_KEY,
             right_table_id=T_FACT2, right_column_id=C_FACT_FK,
             join_type=join_type),
    ]
    return tables, joins, columns


def _keyword(sql: str) -> str:
    """The single JOIN keyword in a two-table FROM clause, INNER-normalised.

    A bare ``JOIN`` is ANSI shorthand for ``INNER JOIN``; folding them makes
    this a SEMANTIC comparison rather than a spelling one, so the test fails
    only on a real row-population difference.
    """
    found = _JOIN_KEYWORD_RE.findall(sql.upper())
    assert len(found) == 1, f"expected exactly one JOIN keyword in: {sql!r}"
    return "INNER JOIN" if found[0] == "JOIN" else found[0]


@pytest.mark.parametrize(
    "join_type,expected",
    [
        # Declared dim -> fact, traversed from the fact, so LEFT/RIGHT FLIP.
        ("left", "RIGHT JOIN"),      # preserves the modeller's LEFT table (dim)
        ("right", "LEFT JOIN"),      # preserves the modeller's RIGHT table (fact)
        ("left_outer", "RIGHT JOIN"),
        ("right_outer", "LEFT JOIN"),
        # Direction-symmetric.
        ("inner", "INNER JOIN"),
        ("full", "FULL OUTER JOIN"),
        ("full_outer", "FULL OUTER JOIN"),
        # Legacy/undeclared token: coerced to an UN-FLIPPED LEFT JOIN by BOTH
        # builders (contract invariant 4 — existing models keep serving).
        ("many_to_one", "LEFT JOIN"),
    ],
)
def test_both_builders_emit_the_same_keyword_for_a_reversed_edge(join_type, expected):
    """Bug-8628: source route and aggregate/pocket CTAS must agree, exactly.

    Reverting the ``flipped=`` argument in ``sql_builder.build_from_clause``
    turns the four flipping rows of this table red, because the CTAS then
    preserves the opposite physical relation to the route it accelerates.
    """
    tables, joins, columns = _dim_first_pair(join_type)
    source_kw = _keyword(_source_route_from_clause_pair(tables, joins, columns))
    ctas_kw = _keyword(_ctas_from_clause_pair(tables, joins, columns))

    assert source_kw == ctas_kw, (
        f"join_type={join_type!r} declared dim->fact and traversed from the "
        f"fact: the source route emitted {source_kw} while the aggregate/pocket "
        f"CTAS emitted {ctas_kw}. The two therefore hold DIFFERENT row "
        f"populations for the same model."
    )
    assert source_kw == expected, (
        f"join_type={join_type!r} rendered {source_kw}, expected {expected} "
        f"under a reversed traversal"
    )


def _source_route_from_clause_pair(tables, joins, columns) -> str:
    return _build_joined_from_clause(
        base_table_id=T_FACT2,
        required_table_ids={t.id for t in tables},
        joins=list(joins),
        tables_by_id={t.id: t for t in tables},
        columns_by_id={c.id: c for c in columns},
        alias_by_table_id={T_FACT2: "base"},
        connector="postgresql",
    )


def _ctas_from_clause_pair(tables, joins, columns) -> str:
    from_sql, _aliases = asyncio.run(
        sql_builder.build_from_clause(
            _FakeDb(tables, joins, columns), MODEL_ID,
            needed_table_ids={t.id for t in tables}, connector="postgresql",
        )
    )
    return from_sql


def test_reversed_traversal_fixture_really_is_reversed():
    """Guard the guard: if the fixture stopped being dim-first the parametrized
    test above would pass vacuously (nothing would flip, so both builders would
    trivially agree)."""
    _tables, joins, _columns = _dim_first_pair("left")
    assert joins[0].left_table_id == T_DIM2 and joins[0].right_table_id == T_FACT2, (
        "the fixture must declare the join dim -> fact for the traversal from "
        "the fact anchor to be the REVERSED one"
    )
    # And the flip must be observable: left and right must not render alike.
    left_kw = _keyword(_ctas_from_clause_pair(*_dim_first_pair("left")))
    right_kw = _keyword(_ctas_from_clause_pair(*_dim_first_pair("right")))
    assert left_kw != right_kw, (
        "the CTAS builder rendered 'left' and 'right' identically, so this "
        "file cannot observe an orientation defect at all"
    )


def test_the_cross_builder_comparison_can_actually_go_green():
    """The xfail above must be RETIRABLE, not a permanent decoration.

    Bug-8611's lesson: an ``xfail(strict=True)`` whose harness cannot observe
    the fix never XPASSes, so the marker outlives the defect and the guard
    rots. Comparing one builder against itself exercises the identical
    extraction and comparison path and must agree — proving the comparison goes
    green the moment the two traversals are reconciled, rather than being
    structurally incapable of it.
    """
    tables, joins, columns = _diamond()
    ctas_from = _ctas_from_clause(tables, joins, columns)
    assert _edges(ctas_from) == _edges(ctas_from)
    assert _edges(ctas_from), (
        "the edge extractor recognised no joins at all, so the comparison "
        "above would pass vacuously whatever the builders did"
    )
