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


def _join(left, right, jtype="many_to_one") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        left_table_id=left,
        right_table_id=right,
        join_type=jtype,
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
