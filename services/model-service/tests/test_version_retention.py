"""F-013-17: bounded version retention prune.

Covers:
- keep-all default (0) is a no-op (no DELETE, and no lock acquired);
- a positive count prunes only versions older than the newest N;
- the currently-deployed version (re-read UNDER the lock — opus5 R4 finding 3) is
  never pruned even when older than N;
- a prune failure is swallowed (a Save must not fail because retention did).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.versions import _prune_old_versions

pytestmark = pytest.mark.asyncio


def _ids(n):
    return [uuid.uuid4() for _ in range(n)]


def _result(all_rows=None, scalar=None):
    """A result mock that answers both .all() (version-id select) and
    .scalar_one_or_none() (the deployed_version_id read)."""
    r = MagicMock()
    r.all.return_value = [(vid,) for vid in (all_rows or [])]
    r.scalar_one_or_none.return_value = scalar
    return r


async def test_keep_all_is_noop():
    model_id = uuid.uuid4()
    db = AsyncMock()
    with patch("shared.config.resolver.get_setting", new=AsyncMock(return_value=0)):
        await _prune_old_versions(db, model_id)
    # keep=0 returns before any DB work — no lock, no select, no delete.
    db.execute.assert_not_called()


async def test_prunes_versions_beyond_count():
    model_id = uuid.uuid4()
    # newest-first: 5 versions, keep 2 → delete the oldest 3.
    versions = _ids(5)
    db = AsyncMock()
    # execute order: advisory lock, deployed-pointer read (None), version-id
    # select, delete.
    db.execute = AsyncMock(side_effect=[
        _result(),                      # advisory lock
        _result(scalar=None),           # deployed_version_id read
        _result(all_rows=versions),     # version-id select
        _result(),                      # delete
    ])
    with patch("shared.config.resolver.get_setting", new=AsyncMock(return_value=2)):
        await _prune_old_versions(db, model_id)
    assert db.execute.await_count == 4
    db.commit.assert_awaited_once()


async def test_deployed_version_never_pruned():
    model_id = uuid.uuid4()
    versions = _ids(5)
    deployed = versions[4]  # the OLDEST — would normally be pruned at keep=2

    captured = {}

    async def _exec(stmt, *a, **k):
        n = captured["n"] = captured.get("n", 0) + 1
        if n == 1:
            return _result()                       # advisory lock
        if n == 2:
            return _result(scalar=deployed)        # deployed pointer (under lock)
        if n == 3:
            return _result(all_rows=versions)      # version-id select
        captured["delete_stmt"] = stmt             # the delete
        return _result()

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_exec)

    with patch("shared.config.resolver.get_setting", new=AsyncMock(return_value=2)):
        await _prune_old_versions(db, model_id)

    # Delete was issued (4 execute calls) and the deployed id (re-read under the
    # lock) is excluded from the retained/deleted computation: retained =
    # newest 2 + deployed = {v0, v1, v4}; deletes {v2, v3}.
    assert db.execute.await_count == 4
    compiled = captured["delete_stmt"].compile()
    bound: set = set()
    for v in compiled.params.values():
        if isinstance(v, (list, tuple, set)):
            bound.update(v)
        else:
            bound.add(v)
    assert deployed not in bound
    assert versions[2] in bound and versions[3] in bound


async def test_prune_failure_is_swallowed():
    model_id = uuid.uuid4()
    db = AsyncMock()
    with patch(
        "shared.config.resolver.get_setting",
        new=AsyncMock(side_effect=RuntimeError("registry down")),
    ):
        # Must not raise.
        await _prune_old_versions(db, model_id)
