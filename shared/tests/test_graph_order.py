"""Bug-8605 — the canonical model-graph ordering rule itself.

``shared/semantic/graph_order.py`` is the single place that decides how a
model's physical graph is enumerated and which table anchors the FROM clause.
The invariant everything rests on is that the LIVE graph, the DEPLOYED
snapshot, and a live graph REHYDRATED from that snapshot all enumerate
identically. That is why the key is ``id`` and not creation order: round-1 deep
review proved on PostgreSQL 15 that ``rehydrate_into_live`` (revert-to-version,
project import) re-stamps ``created_at`` on every row while preserving ``id``,
so a ``created_at`` key silently diverges from the snapshot the router is still
bound to — and ``compare_closures`` cannot see it, because ``created_at`` is
not a field the snapshot carries.
"""
from __future__ import annotations

import io
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from shared.db.models import Join, ModelColumn, ModelTable
from shared.semantic.graph_order import (
    MODEL_COLUMN_ORDER,
    MODEL_JOIN_ORDER,
    MODEL_TABLE_ORDER,
    anchor_is_by_convention,
    canonical_column_order,
    canonical_join_order,
    canonical_table_order,
    order_model_columns,
    order_model_joins,
    order_model_tables,
    pick_anchor_table,
    select_model_columns,
    select_model_joins,
    select_model_tables,
)

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _table(name, table_type="dim_detail", created_at=_T0, tid=None):
    return ModelTable(
        id=tid or uuid.uuid4(),
        model_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        table_type=table_type,
        physical_name=name,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# The database-side layer
# ---------------------------------------------------------------------------

def test_select_model_tables_carries_the_canonical_order_by():
    sql = str(select_model_tables(uuid.uuid4()))
    assert re.search(r"ORDER BY\s+model_tables\.id\b", sql), sql


def test_select_model_joins_carries_the_canonical_order_by():
    sql = str(select_model_joins(uuid.uuid4()))
    assert re.search(r"ORDER BY\s+joins\.id\b", sql), sql


def test_select_model_columns_carries_the_canonical_order_by():
    sql = str(select_model_columns(uuid.uuid4()))
    assert re.search(
        r"ORDER BY\s+model_columns\.model_table_id,\s*model_columns\.id", sql
    ), sql


def test_the_ordering_key_is_one_the_snapshot_actually_carries():
    """The invariant the whole design rests on, asserted at the source.

    Canonical order must use a field that (a) the serialiser writes into the
    snapshot row bodies and (b) ``rehydrate_into_live`` restores verbatim. ``id``
    satisfies both. ``created_at`` satisfies NEITHER — the serialiser excludes
    it and the rehydrator lets ``server_default=func.now()`` re-stamp it — which
    is exactly why the first version of this module was wrong. If either
    property ever changes, the anchor silently diverges between the CTAS and the
    router and no drift is reported, so it is pinned here rather than trusted.
    """
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    serialiser = io.open(
        os.path.join(repo, "tessallite/shared/model_snapshot/serialiser.py"),
        encoding="utf-8",
    ).read()
    assert 'exclude=("created_at", "updated_at")' in serialiser, (
        "the serialiser's exclusion set changed; re-check whether created_at "
        "now survives into the snapshot before relying on this test's premise"
    )
    rehydrator = io.open(
        os.path.join(repo, "tessallite/shared/model_snapshot/rehydrator.py"),
        encoding="utf-8",
    ).read()
    assert "def _strip_pk_and_uuids" in rehydrator, (
        "the rehydrator's row coercion moved; re-verify that it still carries "
        "the primary key through verbatim, which is what makes id a safe "
        "canonical ordering key across a revert"
    )
    graph_order = io.open(
        os.path.join(repo, "tessallite/shared/semantic/graph_order.py"),
        encoding="utf-8",
    ).read()
    assert "created_at" not in graph_order.split('"""', 2)[2], (
        "graph_order's CODE references created_at again. It must not: a revert "
        "re-stamps that column, so ordering on it makes the live graph and the "
        "deployed snapshot anchor on different tables with no drift reported."
    )


def test_the_serialiser_writes_tables_and_joins_in_canonical_order():
    """The snapshot's stored LIST order must BE the canonical order.

    Round-2 review, finding 4: the serialiser still ordered tables and joins by
    ``(created_at, id)`` after canonical order became ``id``, so the system
    carried two different orders for the same rows. Every current consumer of
    the deployed ``tables_by_id`` happens to re-sort, so it was not a live
    wrong number — but the only thing enumerating who must re-sort is a
    seven-file regex whose own docstring admits it is blind to anything not on
    its list. Making the producer emit canonical order removes the second order
    entirely.

    Other row families deliberately keep ``(created_at, id)``: they do not feed
    the FROM anchor or the JOIN expansion, and their list order is
    presentation, not semantics.

    Asserts the serialiser REFERENCES the shared constants rather than
    containing the right literal. Round-3 review caught the first version of
    this guard pinning ``".order_by(ModelTable.id)"`` as a string: the key had
    already moved once in this lane, and a guard pinned to the current spelling
    stays green precisely when the producer and the builders drift apart again.
    """
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    source = io.open(
        os.path.join(repo, "tessallite/shared/model_snapshot/serialiser.py"),
        encoding="utf-8",
    ).read()
    assert ".order_by(*MODEL_TABLE_ORDER)" in source, (
        "the snapshot serialiser no longer orders tables through the shared "
        "MODEL_TABLE_ORDER constant. The key must live in exactly one place: a "
        "copy is what lets the stored list order and "
        "shared/semantic/graph_order.py silently disagree, so a consumer that "
        "trusts list order anchors somewhere the builders do not."
    )
    assert ".order_by(*MODEL_JOIN_ORDER)" in source, (
        "the snapshot serialiser no longer orders joins through the shared "
        "MODEL_JOIN_ORDER constant"
    )
    assert ".order_by(ModelTable.created_at" not in source, (
        "tables are ordered by created_at again -- a revert re-stamps that "
        "column, which is the exact divergence Bug-8605 R2 removed"
    )
    assert ".order_by(Join.created_at" not in source, (
        "joins are ordered by created_at again"
    )


def test_the_canonical_order_constants_are_the_key_the_helpers_apply():
    """The exported constants and the helpers cannot spell the key differently.

    ``MODEL_TABLE_ORDER`` exists so a producer that builds its statement some
    other way (the snapshot serialiser) is not forced to copy the key. That
    only helps if the constant and the helper stay the same thing.
    """
    from sqlalchemy import select as _select

    pairs = [
        (order_model_tables(_select(ModelTable)), MODEL_TABLE_ORDER, ModelTable),
        (order_model_joins(_select(Join)), MODEL_JOIN_ORDER, Join),
        (order_model_columns(_select(ModelColumn)), MODEL_COLUMN_ORDER,
         ModelColumn),
    ]
    for stmt, constant, entity in pairs:
        helper_sql = str(stmt)
        expected = str(_select(entity).order_by(*constant))
        assert helper_sql == expected, (
            f"{entity.__name__}: the ordering helper and its exported constant "
            f"apply different keys.\n  helper:   {helper_sql}\n"
            f"  constant: {expected}"
        )


# ---------------------------------------------------------------------------
# The Python-side layer
# ---------------------------------------------------------------------------

def test_canonical_table_order_sorts_by_id():
    high = _table("dim_high", tid=uuid.UUID(int=2))
    low = _table("dim_low", tid=uuid.UUID(int=1))
    assert [t.physical_name for t in canonical_table_order([high, low])] == [
        "dim_low", "dim_high",
    ]


def test_canonical_order_ignores_created_at_entirely():
    """Creation order must NOT influence the result — that was the old bug.

    The older row here has the HIGHER id, so any residual creation-order
    preference would reverse this.
    """
    older_higher_id = _table("dim_older", created_at=_T0,
                             tid=uuid.UUID(int=99))
    newer_lower_id = _table("dim_newer", created_at=_T0 + timedelta(days=400),
                            tid=uuid.UUID(int=1))
    assert [
        t.physical_name
        for t in canonical_table_order([older_higher_id, newer_lower_id])
    ] == ["dim_newer", "dim_older"]


def test_canonical_order_is_the_same_for_live_and_snapshot_shaped_rows():
    """A live row and its snapshot-hydrated twin must sort identically.

    Hydrated rows carry no ``created_at`` (the serialiser drops it) but do carry
    the same ``id``. Sorting on ``id`` alone is what makes the two sides agree;
    the previous ``(created_at, id)`` key had to special-case this shape, and
    that special case is what a revert broke.
    """
    live = [
        _table("dim_b", tid=uuid.UUID(int=2)),
        _table("dim_a", tid=uuid.UUID(int=1)),
    ]
    hydrated = [
        ModelTable(id=uuid.UUID(int=2), physical_name="dim_b",
                   table_type="dim_detail"),
        ModelTable(id=uuid.UUID(int=1), physical_name="dim_a",
                   table_type="dim_detail"),
    ]
    assert (
        [t.physical_name for t in canonical_table_order(live)]
        == [t.physical_name for t in canonical_table_order(hydrated)]
        == ["dim_a", "dim_b"]
    )


def test_the_anchor_survives_a_rehydrate_that_re_stamps_created_at():
    """Bug-8605 round-1 finding 1: a revert/import must not move the anchor.

    ``rehydrate_into_live`` deletes every ModelTable row and re-inserts it from
    the snapshot, which carries ``id`` but NOT ``created_at``. PostgreSQL's
    ``now()`` is transaction-fixed, so every re-inserted row shares ONE
    timestamp. Measured on PostgreSQL 15 against the previous key:
    ``dim_customer -> dim_region`` came back as ``dim_region -> dim_customer``,
    while the router stayed bound to the old snapshot's order — the CTAS and the
    router anchored on different tables and the closure reported nothing.
    """
    authored = [
        _table("dim_customer", created_at=_T0, tid=uuid.UUID(int=0xF)),
        _table("dim_region", created_at=_T0 + timedelta(days=31),
               tid=uuid.UUID(int=1)),
    ]
    snapshot_order = [t.physical_name for t in canonical_table_order(authored)]

    rehydrated_at = _T0 + timedelta(days=365)
    rehydrated = [
        _table("dim_customer", created_at=rehydrated_at, tid=uuid.UUID(int=0xF)),
        _table("dim_region", created_at=rehydrated_at, tid=uuid.UUID(int=1)),
    ]
    assert pick_anchor_table(rehydrated).physical_name == snapshot_order[0], (
        "a revert re-stamped created_at and moved the live FROM anchor away "
        "from the table the deployed snapshot still anchors on: the CTAS and "
        "the router would build on different base tables and the definition "
        "closure cannot see it (created_at is not a field the snapshot carries)"
    )


def test_canonical_order_tolerates_ids_carried_as_text():
    """Hydrated rows can carry the id as a string; both sides must still agree."""
    as_text = [
        ModelTable(id=str(uuid.UUID(int=2)), physical_name="dim_b",
                   table_type="dim_detail"),
        ModelTable(id=str(uuid.UUID(int=1)), physical_name="dim_a",
                   table_type="dim_detail"),
    ]
    assert [t.physical_name for t in canonical_table_order(as_text)] == [
        "dim_a", "dim_b",
    ]


def test_canonical_join_order_sorts_by_id():
    a = Join(id=uuid.UUID(int=2), join_type="left")
    b = Join(id=uuid.UUID(int=1), join_type="left")
    assert [j.id for j in canonical_join_order([a, b])] == [b.id, a.id]


def test_canonical_column_order_groups_by_table_then_id():
    t1, t2 = uuid.UUID(int=1), uuid.UUID(int=2)
    cols = [
        ModelColumn(id=uuid.UUID(int=20), model_table_id=t2, column_name="b2"),
        ModelColumn(id=uuid.UUID(int=11), model_table_id=t1, column_name="a2"),
        ModelColumn(id=uuid.UUID(int=10), model_table_id=t1, column_name="a1"),
    ]
    assert [c.column_name for c in canonical_column_order(cols)] == [
        "a1", "a2", "b2",
    ]


# ---------------------------------------------------------------------------
# The anchor rule
# ---------------------------------------------------------------------------

def test_anchor_prefers_the_fact_table_regardless_of_id():
    dim = _table("dim_low", tid=uuid.UUID(int=1))
    fact = _table("fact_sales", table_type="fact", tid=uuid.UUID(int=99))
    assert pick_anchor_table([dim, fact]).physical_name == "fact_sales"
    assert pick_anchor_table([fact, dim]).physical_name == "fact_sales"


def test_anchor_matches_fact_case_sensitively_like_the_storage_index():
    """The partial unique index tests the SQL literal ``table_type = 'fact'``.

    A row stored as ``"Fact"`` is therefore NOT capped by the index and is not
    treated as a fact by ``source_sql``/``table_resolution`` either. Matching it
    case-insensitively here would make this module call a row the fact table
    when nothing else in the read path does — a disagreement worse than the
    ordering bug this module exists to fix. (``table_type`` being an
    unvalidated free string is tracked as its own issue.)
    """
    odd = _table("Fact_sales", table_type="Fact", tid=uuid.UUID(int=99))
    dim = _table("dim_low", tid=uuid.UUID(int=1))
    assert pick_anchor_table([odd, dim]).physical_name == "dim_low"


def test_zero_fact_anchor_is_the_lowest_id_table():
    a = _table("dim_a", tid=uuid.UUID(int=5))
    b = _table("dim_b", tid=uuid.UUID(int=2))
    assert pick_anchor_table([a, b]).physical_name == "dim_b"
    assert pick_anchor_table([b, a]).physical_name == "dim_b"


def test_anchor_of_an_empty_graph_is_none():
    """Callers raise their own context-specific error; the rule must not."""
    assert pick_anchor_table([]) is None


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], False),
        ([_table("only")], False),
        ([_table("f", table_type="fact"), _table("d")], False),
        ([_table("d1"), _table("d2")], True),
    ],
)
def test_anchor_by_convention_flags_only_the_undeclared_case(rows, expected):
    """The operational signal: a zero-fact multi-table model has no declared base.

    One table has no choice to make, and a fact table IS the declaration. Only
    the remaining shape has the platform picking for the modeller, which is the
    case worth logging.
    """
    assert anchor_is_by_convention(rows) is expected
