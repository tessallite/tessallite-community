"""Bug-8605 / Bug-8601 — the FROM anchor must not depend on row order.

``build_from_clause`` used to choose its anchor positionally::

    facts = [t for t in tables.values() if t.table_type == "fact"]
    anchor = facts[0] if facts else next(iter(tables.values()))

over a ``select(ModelTable)`` carrying no ORDER BY. On a model with a fact
table the one-fact-per-model index makes that stable. On a model with NO fact
table — legal; ``_assert_at_most_one_fact`` imposes no minimum — the anchor was
whichever row the database happened to return first.

Round-3 review measured that flipping against real PostgreSQL 15 with no edit at
all: an index scan returned ``dim_customer`` first, a sequential scan returned
``dim_region`` first, and a routine ``VACUUM FULL`` made the second order
permanent. Because a LEFT JOIN preserves the BASE relation's rows, the two FROM
clauses have different row membership, so the same unchanged model materialised
different totals — ``COUNT(*)=2, SUM=350.00`` versus ``COUNT(*)=1, SUM=100.00``.

The Bug-8250 definition closure cannot catch it: the closure compares row SETS,
and this is an ordering property of the read that produced them. Every row is
identical on both sides, so no drift is reported and the artifact is stamped
version-compatible.

The fix (``shared/semantic/graph_order.py``) pins the order in two layers: the
reads carry ``ORDER BY id`` AND the anchor rule re-sorts in Python, so the
choice is a pure function of the rows even if a caller obtains them some other
way. This file asserts BOTH layers; ``test_graph_order.py`` covers the rule
itself, including WHY the key is ``id`` rather than creation order — a revert
re-stamps ``created_at`` on every row but preserves ``id``, so a creation-order
key silently diverges from the snapshot the router is still bound to.

Do NOT weaken ``_OrderedSession`` to ignore ``ORDER BY``: round-4 review proved
that a stub which ignores it makes a guard here unretirable, because the
database-side sort is invisible to such a stub.
"""
from __future__ import annotations

import asyncio
import io
import uuid
from datetime import datetime, timezone

import pytest

from shared.semantic.sql_builder import build_from_clause

# FIXED ids, because canonical order IS id order -- random uuid4 values would
# make which table anchors vary between test runs.
MODEL_ID = uuid.UUID(int=0xDE1)
T_A = uuid.UUID(int=0xA1)   # dim_customer -- canonically first
T_B = uuid.UUID(int=0xB2)   # dim_region
C_A = uuid.UUID(int=0xC1)
C_B = uuid.UUID(int=0xC2)
J_1 = uuid.UUID(int=0x11)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _order_by_names(stmt) -> list[str]:
    """The column names an ``ORDER BY`` on ``stmt`` sorts by, if any."""
    names: list[str] = []
    for clause in getattr(stmt, "_order_by_clauses", ()) or ():
        element = getattr(clause, "element", clause)
        name = getattr(element, "name", None) or getattr(element, "key", None)
        if name:
            names.append(str(name))
    return names


class _OrderedSession:
    """Returns rows in a caller-chosen order, as an unordered ``SELECT``
    legitimately may -- but HONOURS an ``ORDER BY`` when the statement carries
    one.

    Honouring it is what lets this file prove the database-side half of the
    fix. Round-4 review proved the point by mutation: with a stub that ignored
    ORDER BY, applying Bug-8605's own fix direction verbatim to
    ``sql_builder.py`` left the guard unable to see it.

    ``honour_order`` can be turned OFF to model a caller that obtains rows
    without the canonical read (a cache, a hand-built graph, a future call
    site). The anchor must still be stable there, which is the second layer of
    the fix.
    """

    def __init__(self, tables, joins, columns, honour_order: bool = True):
        self._tables = tables
        self._joins = joins
        self._columns = columns
        self._honour_order = honour_order
        self.seen_order_by: dict[str, list[str]] = {}

    def _apply_order(self, rows, stmt, entity):
        names = _order_by_names(stmt)
        self.seen_order_by[entity] = names
        if not names or not self._honour_order:
            return rows
        return sorted(
            rows, key=lambda r: tuple(str(getattr(r, n, "")) for n in names)
        )

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"].__name__
        if entity == "ModelTable":
            return _Result(self._apply_order(self._tables, stmt, entity))
        if entity == "Join":
            return _Result(self._apply_order(self._joins, stmt, entity))
        if entity == "ModelColumn":
            return _Result(self._apply_order(self._columns, stmt, entity))
        return _Result([])


def _graph(table_order, honour_order: bool = True):
    from shared.db.models import Join, ModelColumn, ModelTable

    # created_at is populated INVERSE to the id order, so a rule that regressed
    # to sorting on creation time would pick dim_region and fail these
    # assertions. Canonical order is id: T_A (0xA1) sorts before T_B (0xB2).
    by_id = {
        T_A: ModelTable(id=T_A, model_id=MODEL_ID, source_id=uuid.uuid4(),
                        table_type="dim_aggregate", physical_name="dim_customer",
                        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc)),
        T_B: ModelTable(id=T_B, model_id=MODEL_ID, source_id=uuid.uuid4(),
                        table_type="dim_detail", physical_name="dim_region",
                        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    }
    columns = [
        ModelColumn(id=C_A, model_table_id=T_A, column_name="k", data_type="text"),
        ModelColumn(id=C_B, model_table_id=T_B, column_name="k", data_type="text"),
    ]
    joins = [
        Join(id=J_1, model_id=MODEL_ID, left_table_id=T_A,
             right_table_id=T_B, join_type="left",
             left_column_id=C_A, right_column_id=C_B,
             created_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
    ]
    return _OrderedSession(
        [by_id[t] for t in table_order], joins, columns,
        honour_order=honour_order,
    )


def test_from_clause_anchor_does_not_depend_on_row_order():
    """The same rows in two legal orders must produce the same FROM clause."""
    first, _ = asyncio.run(
        build_from_clause(_graph([T_A, T_B]), MODEL_ID, connector="postgresql")
    )
    second, _ = asyncio.run(
        build_from_clause(_graph([T_B, T_A]), MODEL_ID, connector="postgresql")
    )
    assert first == second, (
        "the FROM anchor moved with nothing but the row order:\n"
        f"  {first!r}\nvs\n  {second!r}\n"
        "A LEFT JOIN preserves the BASE relation's rows, so these two clauses "
        "have different row membership and the CTAS materialises different "
        "totals for an unchanged model."
    )


def test_anchor_is_the_canonically_first_table_not_merely_stable():
    """Pin the VALUE, not just the agreement between two runs.

    A rule that returned the same wrong table twice would satisfy the
    comparison above. The canonical rule is ``id``, and ``dim_customer``
    (0xA1) sorts before ``dim_region`` (0xB2), so it is the base in both
    orders -- even though it is the OLDER row and creation order would agree
    here only by coincidence.
    """
    for order in ([T_A, T_B], [T_B, T_A]):
        from_sql, aliases = asyncio.run(
            build_from_clause(_graph(order), MODEL_ID, connector="postgresql")
        )
        assert from_sql.startswith('"dim_customer" AS base'), from_sql
        assert aliases[T_A] == "base"
        assert aliases[T_B] == "t1"


def test_the_reads_carry_the_canonical_order_by():
    """The database-side layer: the statements must be ordered at the source.

    Without this the guard would pass on the Python re-sort alone, and a caller
    that streamed rows or counted on the read order would still be exposed.
    """
    session = _graph([T_A, T_B])
    asyncio.run(build_from_clause(session, MODEL_ID, connector="postgresql"))
    assert session.seen_order_by.get("ModelTable") == ["id"], (
        "the ModelTable read lost its canonical ORDER BY: "
        f"{session.seen_order_by!r}"
    )
    assert session.seen_order_by.get("Join") == ["id"], (
        "the Join read lost its canonical ORDER BY — join order breaks ties "
        "between equally short anchor-to-table paths and fixes alias numbering: "
        f"{session.seen_order_by!r}"
    )


def test_anchor_is_stable_even_when_the_read_order_is_not_honoured():
    """The Python-side layer: rows obtained some other way still anchor the same.

    Models the shapes an ORDER BY cannot reach — a process cache, a hand-built
    graph, a future call site that reads without the canonical helper.
    """
    first, _ = asyncio.run(
        build_from_clause(
            _graph([T_A, T_B], honour_order=False), MODEL_ID,
            connector="postgresql",
        )
    )
    second, _ = asyncio.run(
        build_from_clause(
            _graph([T_B, T_A], honour_order=False), MODEL_ID,
            connector="postgresql",
        )
    )
    assert first == second == '"dim_customer" AS base\n  LEFT JOIN "dim_region" AS t1 ON base."k" = t1."k"', (
        f"{first!r} vs {second!r}"
    )


def test_a_model_with_a_fact_table_is_already_deterministic():
    """The reachable case is ZERO-fact, not multi-fact.

    Pinned so the fix stays scoped correctly: the one-fact-per-model partial
    unique index (migration 0136 and ``ModelTable.__table_args__``) caps fact
    tables at one, so where a fact table exists the anchor choice was stable
    whatever order the rows arrived in — and must remain so.
    """
    from shared.db.models import ModelTable
    from shared.semantic.graph_order import pick_anchor_table

    fact = ModelTable(id=T_B, model_id=MODEL_ID, source_id=uuid.uuid4(),
                      table_type="fact", physical_name="fact_sales",
                      created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    dim = ModelTable(id=T_A, model_id=MODEL_ID, source_id=uuid.uuid4(),
                     table_type="dim_detail", physical_name="dim_region",
                     created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    # The dim sorts FIRST by id and is also older, so a rule that dropped the
    # fact preference in favour of the ordering alone would pick dim_region.
    assert pick_anchor_table([fact, dim]).physical_name == "fact_sales"
    assert pick_anchor_table([dim, fact]).physical_name == "fact_sales"


def test_zero_fact_models_are_legal():
    """The precondition the fix addresses: a model may have no fact table.

    ``_assert_at_most_one_fact`` is an AT MOST check. If a minimum were ever
    enforced the zero-fact anchor case would become unreachable, and the
    convention logging in ``build_from_clause`` would be dead code — this test
    fails so that is noticed rather than left in place forever.
    """
    import os

    # Read the rule as SOURCE rather than importing it: this is the shared tier,
    # which must not depend on a service package.
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    path = os.path.join(
        repo, "tessallite/services/model-service/src/api/tables.py"
    )
    source = io.open(path, encoding="utf-8").read()
    assert "_assert_at_most_one_fact" in source, (
        "the one-fact rule moved or was renamed; re-check whether a model can "
        "still have zero fact tables (Bug-8605's precondition)"
    )
    assert "_assert_at_least_one_fact" not in source, (
        "a MINIMUM fact-table rule appeared. If a model can no longer have zero "
        "fact tables, the zero-fact anchor convention is unreachable and the "
        "logging/branch in build_from_clause should be re-scoped."
    )


def test_both_ctas_builders_share_one_anchor_rule():
    """Shared-primitive rule: the two CTAS builders must not drift apart.

    ``shared/semantic/sql_builder.py`` and the optimizer's own
    ``_build_source_from_clause`` produce the CTAS bodies for the same models.
    A positional anchor reintroduced in either one is a wrong number, so both
    must go through ``graph_order``. Source-level because the optimizer package
    is not importable from the shared tier.
    """
    import os
    import re

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    targets = {
        "shared/semantic/sql_builder.py":
            "tessallite/shared/semantic/sql_builder.py",
        "optimizer creator.py":
            "tessallite/services/optimizer/src/lifecycle/creator.py",
    }
    banned = re.compile(
        r"anchor\s*=\s*facts\[0\]|next\(iter\(tables\.values\(\)\)\)"
    )
    for label, rel in targets.items():
        source = io.open(os.path.join(repo, rel), encoding="utf-8").read()
        assert "pick_anchor_table" in source, (
            f"{label} no longer uses the shared anchor rule; a second, "
            "independent anchor rule is how these two builders disagree"
        )
        offending = [
            line for line in source.splitlines()
            if banned.search(line) and not line.lstrip().startswith("#")
        ]
        assert not offending, (
            f"{label} reintroduced a positional anchor: {offending}"
        )


#: Every module that reads a model's ``ModelTable``/``Join`` rows on a path
#: where the ORDER is load-bearing — the FROM anchor, the JOIN expansion, the
#: base table a query projects against, or the watermark column an incremental
#: load reads. Enumerated by grepping for the primitive, per the
#: shared-primitive rule; the registry entry named only the first two.
ORDER_SENSITIVE_GRAPH_READERS = [
    "tessallite/shared/semantic/sql_builder.py",
    "tessallite/services/optimizer/src/lifecycle/creator.py",
    "tessallite/services/query-router/src/rewrite/table_resolution.py",
    "tessallite/services/query-router/src/rewrite/snapshot_graph_resolvers.py",
    "tessallite/services/scheduler/src/jobs/full_refresh.py",
    "tessallite/services/scheduler/src/jobs/incremental_refresh.py",
    # Round-1 review finding 2: the SEVENTH site, and the only one whose
    # verdict is persisted (is_invalid on dimensions/measures).
    "tessallite/shared/semantic/model_validator.py",
]


@pytest.mark.parametrize("rel", ORDER_SENSITIVE_GRAPH_READERS)
def test_no_unordered_model_graph_reads_remain(rel):
    """Every live ModelTable/Join read on an anchor path is canonically ordered.

    Narrow on purpose. This asserts a textual property of six NAMED files the
    fix actually changed — it is a regression guard against re-adding the exact
    pattern that was removed, NOT a coverage tool making a global claim. A
    grep-shaped rule cannot prove ordering for a dynamically built statement,
    and it is blind to any file not on the list above, so extending the list is
    part of touching a new builder. The behavioural guards in this file, in the
    optimizer suite and in the query-router suite are the real evidence; this
    only stops a silent regression.
    """
    import os
    import re

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    source = io.open(os.path.join(repo, rel), encoding="utf-8").read()
    # Whole-file, whitespace-tolerant: the pattern is frequently written across
    # several lines, which a line-by-line scan would miss entirely.
    # Round-2 review finding 2 widened this: the eighth site was a
    # ``select(ModelColumn).where(model_table_id.in_(...))`` in a file ALREADY
    # on the list, which the ModelTable/Join-only pattern could not see.
    bad = re.compile(
        r"(?:sa_)?select\(\s*(?:ModelTable|Join)\s*\)\s*\.\s*where\(\s*"
        r"(?:ModelTable|Join)\.model_id"
        r"|(?:sa_)?select\(\s*ModelColumn\s*\)\s*\.\s*where\(\s*"
        r"ModelColumn\.model_table_id",
        re.MULTILINE,
    )
    # A raw ``select(...)`` is fine when it is wrapped in one of the ordering
    # helpers — ``_order_model_tables(select(ModelTable).where(...))`` — which
    # is how a read that also needs ``.execution_options`` is written.
    # Known remaining blind spots, stated rather than implied: a locally
    # aliased ``select``, ``.filter(...)``, ``and_(...)``, relationship loading
    # (``Model.tables``), and any file not on the list above. Round-1 review
    # found a seventh site the file list missed and round-2 found an eighth the
    # entity list missed; treat the list itself as the thing to extend.
    wrappers = ("order_model_tables(", "order_model_joins(",
                "order_model_columns(")
    hits = [
        m.group(0).replace("\n", " ")
        for m in bad.finditer(source)
        if not any(w in source[max(0, m.start() - 80):m.start()] for w in wrappers)
    ]
    assert not hits, (
        f"{rel} reads the model graph without the canonical ordering helper "
        f"(use select_model_tables / select_model_joins, or wrap the statement "
        f"in order_model_tables / order_model_joins): {hits}"
    )


@pytest.mark.parametrize("rel", ORDER_SENSITIVE_GRAPH_READERS)
def test_order_sensitive_readers_exist(rel):
    """A rename must fail loudly instead of silently shrinking the guard."""
    import os

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    assert os.path.exists(os.path.join(repo, rel)), (
        f"{rel} is listed as an order-sensitive model-graph reader but does not "
        "exist; the guard above is scanning nothing for it"
    )
