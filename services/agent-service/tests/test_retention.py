"""F-023-27 — shared agent conversation retention logic.

Covers the soft-delete window, the hard-purge grace window, the
grace<=0 disable, and the per-project sweep that the scheduler runs.
The SQLAlchemy session is mocked: each `db.execute` first returns the
id-select result, then the update/delete result.
"""
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.agent import retention


def _select_result(ids):
    """A result whose .all() yields one-tuples of ids (mirrors select(id))."""
    res = MagicMock()
    res.all.return_value = [(i,) for i in ids]
    return res


def _install_savepoints(db):
    """Make ``db.begin_nested()`` behave like a real async savepoint context.

    ``sweep_tenant_retention`` wraps each project in ``async with
    db.begin_nested()`` (Bug-2861). On an AsyncMock, ``begin_nested`` would
    otherwise return a coroutine, not an async context manager. This installs a
    real ``@asynccontextmanager`` that does nothing on success and lets
    exceptions raised inside the ``with`` body propagate to the caller's
    try/except (mirroring a savepoint auto-rollback leaving the outer
    transaction usable).
    """
    @asynccontextmanager
    async def _savepoint():
        yield

    db.begin_nested = MagicMock(side_effect=lambda: _savepoint())


@pytest.mark.asyncio
async def test_soft_delete_marks_inactive():
    db = AsyncMock()
    ids = [uuid.uuid4(), uuid.uuid4()]
    # 1st execute = select ids; 2nd execute = update
    db.execute.side_effect = [_select_result(ids), MagicMock()]

    n = await retention.soft_delete_inactive_conversations(db, uuid.uuid4(), 30)
    assert n == 2
    # select + update both issued
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_soft_delete_none_when_nothing_inactive():
    db = AsyncMock()
    db.execute.side_effect = [_select_result([])]
    n = await retention.soft_delete_inactive_conversations(db, uuid.uuid4(), 30)
    assert n == 0
    # only the select ran — no update for an empty set
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_soft_delete_skips_nonpositive_retention():
    db = AsyncMock()
    n = await retention.soft_delete_inactive_conversations(db, uuid.uuid4(), 0)
    assert n == 0
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_hard_purge_deletes_turns_then_conversations():
    db = AsyncMock()
    ids = [uuid.uuid4()]
    # select ids, delete turns, delete conversations
    db.execute.side_effect = [_select_result(ids), MagicMock(), MagicMock()]
    n = await retention.hard_purge_soft_deleted(db, uuid.uuid4(), 30)
    assert n == 1
    assert db.execute.await_count == 3


@pytest.mark.asyncio
async def test_hard_purge_disabled_when_grace_nonpositive():
    db = AsyncMock()
    n = await retention.hard_purge_soft_deleted(db, uuid.uuid4(), 0)
    assert n == 0
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_tenant_iterates_configs_and_commits():
    db = AsyncMock()
    _install_savepoints(db)
    p1 = uuid.uuid4()
    soft_ids = [uuid.uuid4()]
    purge_ids = [uuid.uuid4(), uuid.uuid4()]
    # configs select, then per-project: soft-select, soft-update,
    # purge-select, purge-delete-turns, purge-delete-convs
    configs_res = MagicMock()
    configs_res.all.return_value = [(p1, 30)]
    db.execute.side_effect = [
        configs_res,
        _select_result(soft_ids), MagicMock(),
        _select_result(purge_ids), MagicMock(), MagicMock(),
    ]

    soft, purged = await retention.sweep_tenant_retention(db, 30, tenant_slug="acme")
    assert soft == 1
    assert purged == 2
    db.commit.assert_awaited()
    # Each project's work is wrapped in a savepoint (Bug-2861).
    db.begin_nested.assert_called_once()


@pytest.mark.asyncio
async def test_sweep_savepoint_isolates_failing_project(caplog):
    """Bug-2861: one failing project must not abort the others.

    The first project raises mid-sweep (simulating a DB error that, without a
    savepoint, would turn the async session rollback-only and poison every
    later project's query plus the closing commit). With per-project
    ``begin_nested`` savepoints the failure is isolated: the savepoint rolls
    back, the outer transaction stays usable, and the second and third
    projects complete and are committed.
    """
    db = AsyncMock()
    _install_savepoints(db)

    p_bad, p_good1, p_good2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    configs_res = MagicMock()
    configs_res.all.return_value = [(p_bad, 30), (p_good1, 30), (p_good2, 30)]

    bad_soft_ids = [uuid.uuid4()]
    good1_soft_ids = [uuid.uuid4(), uuid.uuid4()]
    good2_purge_ids = [uuid.uuid4()]

    # Sequence of db.execute results, in call order:
    #  - configs select
    #  - p_bad: soft-select (returns ids) -> soft-update RAISES
    #  - p_good1: soft-select (2 ids) -> soft-update OK -> purge-select (none)
    #  - p_good2: soft-select (none) -> purge-select (1 id) -> del turns -> del convs
    db.execute.side_effect = [
        configs_res,
        # p_bad
        _select_result(bad_soft_ids),
        RuntimeError("simulated DB error in UPDATE"),
        # p_good1
        _select_result(good1_soft_ids), MagicMock(),
        _select_result([]),
        # p_good2
        _select_result([]),
        _select_result(good2_purge_ids), MagicMock(), MagicMock(),
    ]

    soft, purged = await retention.sweep_tenant_retention(db, 30, tenant_slug="acme")

    # The bad project contributed nothing; the two good projects did their work.
    assert soft == 2  # only p_good1's soft-deletes
    assert purged == 1  # only p_good2's purge
    # A savepoint was opened for every project, including the failing one.
    assert db.begin_nested.call_count == 3
    # The session stayed usable: the closing commit still ran for the good work.
    db.commit.assert_awaited()
    # The failure was logged, not swallowed silently.
    assert any("Agent retention failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_sweep_commits_even_when_first_project_fails(caplog):
    """Bug-2861 regression guard: a leading failure must not block the commit.

    Before the savepoint fix, the first project's error left the session
    rollback-only, so the final ``db.commit()`` itself failed and the whole
    tenant sweep was silently dropped. Here the only successful work belongs to
    the *second* project; the commit must still fire.
    """
    db = AsyncMock()
    _install_savepoints(db)

    p_bad, p_good = uuid.uuid4(), uuid.uuid4()
    configs_res = MagicMock()
    configs_res.all.return_value = [(p_bad, 30), (p_good, 30)]

    db.execute.side_effect = [
        configs_res,
        # p_bad: soft-select RAISES immediately
        RuntimeError("simulated DB error in SELECT"),
        # p_good: soft-select (1 id) -> soft-update -> purge-select (none)
        _select_result([uuid.uuid4()]), MagicMock(),
        _select_result([]),
    ]

    soft, purged = await retention.sweep_tenant_retention(db, 30, tenant_slug="acme")
    assert soft == 1
    assert purged == 0
    db.commit.assert_awaited()
