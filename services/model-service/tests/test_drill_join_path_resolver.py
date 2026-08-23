"""BFS join-path resolver — Phase 8.A.4.

Verifies the in-memory path enumeration that powers
``GET /measures/{id}/drill-through-set/join-paths``.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.measures import (
    _cardinality_hint_from_path,
    _enumerate_join_paths,
)

pytestmark = pytest.mark.unit


def _join(left, right, jtype="many_to_one", cardinality=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        left_table_id=left,
        right_table_id=right,
        join_type=jtype,
        cardinality=cardinality,
    )


def _db_with_joins(joins) -> AsyncMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(joins)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_same_table_returns_empty_path():
    fact = uuid.uuid4()
    db = _db_with_joins([])
    paths = await _enumerate_join_paths(db, uuid.uuid4(), fact, fact)
    assert paths == [[]]


@pytest.mark.asyncio
async def test_single_hop_path():
    fact = uuid.uuid4()
    dim = uuid.uuid4()
    j = _join(dim, fact)
    db = _db_with_joins([j])
    paths = await _enumerate_join_paths(db, uuid.uuid4(), dim, fact)
    assert len(paths) == 1
    assert paths[0] == [j]


@pytest.mark.asyncio
async def test_two_hop_path_through_bridge():
    fact = uuid.uuid4()
    bridge = uuid.uuid4()
    leaf = uuid.uuid4()
    j1 = _join(leaf, bridge)
    j2 = _join(bridge, fact)
    db = _db_with_joins([j1, j2])
    paths = await _enumerate_join_paths(db, uuid.uuid4(), leaf, fact)
    assert len(paths) == 1
    assert [j.id for j in paths[0]] == [j1.id, j2.id]


@pytest.mark.asyncio
async def test_two_distinct_paths_returned():
    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    mid_a = uuid.uuid4()
    mid_b = uuid.uuid4()
    j1 = _join(leaf, mid_a)
    j2 = _join(mid_a, fact)
    j3 = _join(leaf, mid_b)
    j4 = _join(mid_b, fact)
    db = _db_with_joins([j1, j2, j3, j4])
    paths = await _enumerate_join_paths(db, uuid.uuid4(), leaf, fact)
    assert len(paths) == 2
    path_sets = {tuple(j.id for j in p) for p in paths}
    assert (j1.id, j2.id) in path_sets
    assert (j3.id, j4.id) in path_sets


@pytest.mark.asyncio
async def test_hop_cap_excludes_long_chains():
    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    a, b, c, d, e = (uuid.uuid4() for _ in range(5))
    edges = [
        _join(leaf, a),
        _join(a, b),
        _join(b, c),
        _join(c, d),
        _join(d, e),
        _join(e, fact),
    ]
    db = _db_with_joins(edges)
    paths = await _enumerate_join_paths(db, uuid.uuid4(), leaf, fact, max_hops=4)
    assert paths == []  # 6-hop path exceeds cap


@pytest.mark.asyncio
async def test_no_path_returns_empty():
    db = _db_with_joins([])
    paths = await _enumerate_join_paths(db, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert paths == []


def test_cardinality_hint_pure_many_to_one():
    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    j = _join(leaf, fact, jtype="many_to_one")
    assert _cardinality_hint_from_path([j], leaf) == "many-to-one"


def test_cardinality_hint_mixed_when_one_hop_inverted():
    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    bridge = uuid.uuid4()
    j1 = _join(leaf, bridge, jtype="many_to_one")
    # j2 is fact->bridge (right side), so traversal from bridge to fact inverts to one_to_many.
    j2 = _join(fact, bridge, jtype="many_to_one")
    assert _cardinality_hint_from_path([j1, j2], leaf) == "mixed"


# ---------------------------------------------------------------------------
# Join-orientation contract (invariant 3) — the hint reads CARDINALITY, not the
# orientation field the two properties used to share.
# ---------------------------------------------------------------------------


def test_cardinality_hint_reads_the_declared_cardinality_field():
    """A join with a real orientation and a declared fan-out classifies.

    Every join the write API has accepted since Bug-7775 carries an
    orientation token (inner/left/right/full) in ``join_type``. Classifying
    from that field made all of them "mixed" regardless of their true fan-out,
    which refused valid drill-through source overrides. The hint now reads
    ``Join.cardinality``.
    """
    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    j = _join(leaf, fact, jtype="right", cardinality="many_to_one")
    assert _cardinality_hint_from_path([j], leaf) == "many-to-one"


def test_cardinality_hint_is_mixed_when_the_fan_out_is_undeclared():
    """Fail-closed: an orientation with no declared cardinality is UNKNOWN.

    Not knowing whether a hop expands is not the same as knowing it does not —
    an expanding hop repeats the parent measure across child rows.
    """
    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    j = _join(leaf, fact, jtype="right", cardinality=None)
    assert _cardinality_hint_from_path([j], leaf) == "mixed"


def test_cardinality_hint_still_reads_a_legacy_token_in_join_type():
    """Invariant 4: a row written before the split keeps working unchanged."""
    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    j = _join(leaf, fact, jtype="many_to_one", cardinality=None)
    assert _cardinality_hint_from_path([j], leaf) == "many-to-one"


def test_cardinality_hint_inverts_the_declared_cardinality_on_reverse_traversal():
    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    # Declared leaf(many) -> fact(one); traversed FROM the fact it expands.
    j = _join(leaf, fact, jtype="right", cardinality="many_to_one")
    assert _cardinality_hint_from_path([j], fact) == "one-to-many"


# ---------------------------------------------------------------------------
# Bug-7267 — _validate_join_path rejects reverse-direction hops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_join_path_rejects_reverse_direction_bug7267():
    """A join path that traverses a join in reverse (right_table_id matches
    cursor) should be rejected with DRILL_JOIN_PATH_WRONG_DIRECTION, because
    the drill SQL builder emits joins in the defined left->right direction
    and a reverse hop produces incorrect ON conditions."""
    from fastapi import HTTPException
    from src.api.measures import _validate_join_path

    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    # Join defined as fact -> leaf (left=fact, right=leaf).
    # Walking from leaf means cursor=leaf matches right_table_id -> reverse hop.
    j = _join(fact, leaf)
    db = _db_with_joins([j])
    effective_table = types.SimpleNamespace(id=leaf)

    with pytest.raises(HTTPException) as exc_info:
        await _validate_join_path(db, [j.id], uuid.uuid4(), effective_table, fact)

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["code"] == "DRILL_JOIN_PATH_WRONG_DIRECTION"
    assert str(j.id) in detail["reversed_join_ids"]


@pytest.mark.asyncio
async def test_validate_join_path_accepts_forward_direction_bug7267():
    """A join path where every hop is forward (cursor matches left_table_id)
    should pass validation."""
    from src.api.measures import _validate_join_path

    fact = uuid.uuid4()
    leaf = uuid.uuid4()
    j = _join(leaf, fact)
    db = _db_with_joins([j])
    effective_table = types.SimpleNamespace(id=leaf)

    # Should not raise
    await _validate_join_path(db, [j.id], uuid.uuid4(), effective_table, fact)
