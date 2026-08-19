"""Dedicated-connection PostgreSQL advisory lock held across ORM commits.

Bug-6570 / Bug-6548 / Bug-6104 root cause (Fable F-1, empirically proven live):
the aggregate/pocket refresh locks (and the sibling sweep single-flight lock,
Bug-6604) took a *session*-scoped ``pg_try_advisory_lock`` on the **ORM
session's** connection and assumed the connection stayed bound to the session
across the mid-materialisation commits the refresh executors perform. It does
not. ``shared/db/session.py`` builds pooled ``async_sessionmaker(engine,
expire_on_commit=False)`` factories; the SQLAlchemy default returns the physical
connection to the pool on every ``commit()``/``rollback()`` and lazily checks
out a (possibly different) connection on the next statement. A PostgreSQL
session-level advisory lock is held by the CONNECTION (backend), not by the ORM
``Session``. So the first mid-body commit hands the lock-bearing connection back
to the pool; the exit ``pg_advisory_unlock`` then runs on a *different*
connection and returns False, leaking the lock (spurious ``RefreshInFlightError``
until ``pool_recycle``) and, worse, letting a second refresher that lands on the
freed connection re-acquire the lock re-entrantly and run a concurrent DROP+CTAS
— the exact corruption the lock was meant to prevent.

Fix: hold the advisory lock on a **dedicated connection** checked out from the
session's own engine and kept open (never returned to the pool) for the entire
``async with`` body, released only on exit. The ORM session keeps committing
freely on its own churning connection; the lock's lifetime is decoupled from
that churn. PostgreSQL advisory locks live in ONE lock space per database keyed
on the id, independent of which connection queries them, so:

* two holders on the same key still mutually exclude (the second one's
  dedicated connection sees the id held by the first and gets False);
* the optimizer's transaction-scoped ``pg_try_advisory_xact_lock`` (creator
  finalisation, ``acquire_refresh_lock``) on its ORM connection still mutually
  excludes / is excluded by this dedicated-connection lock on the same id.

Re-entrancy: ``incremental_refresh_aggregate`` re-enters
``full_refresh_aggregate`` on the SAME ORM session. A naive dedicated-connection
lock would open a *second* dedicated connection for the nested acquire, see the
id already held by the outer dedicated connection, and raise
``RefreshInFlightError`` against itself (Fable scenario 3). A per-session holder
registry makes the nested acquire reuse the outer dedicated connection and only
bump a depth counter; the lock is released once, when depth returns to 0.

Fail-safe (Bug-6605): the lock must NEVER survive this context manager. Whenever
the lock may be held and ``pg_advisory_unlock`` cannot verifiably run — including
on ``asyncio.CancelledError`` at shutdown or a broken connection — the dedicated
connection is INVALIDATED (its backend is terminated), which makes PostgreSQL
free every session lock that backend held. A plain ``close()`` (returning the
connection to the pool) is used ONLY when the lock is provably not held (a busy
acquire that returned False). Release helpers never re-raise, so the locked
body's ORIGINAL exception is the one that propagates.

Two public variants share this machinery:

* ``dedicated_advisory_lock`` — raises ``on_conflict()`` when the key is busy
  (the refresh executors: a concurrent refresh must be reported as in-flight).
* ``dedicated_advisory_lock_optional`` — yields ``True``/``False`` instead
  (the sweep single-flight guard: another instance simply skips).
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

logger = logging.getLogger(__name__)


def lock_id(name: str) -> int:
    """Stable 60-bit advisory-lock key (same scheme as shared.distributed_lock)."""
    return int(hashlib.sha256(name.encode()).hexdigest()[:15], 16)


class _Holder:
    """A dedicated lock connection plus a re-entrancy depth counter."""

    __slots__ = ("conn", "depth")

    def __init__(self, conn: AsyncConnection):
        self.conn = conn
        self.depth = 1


# Per-ORM-session registry of currently-held advisory locks, keyed on the lock
# id. A session is used sequentially (the refresh sweep processes one aggregate/
# pocket at a time on a shared tenant session; re-entrancy is strictly nested),
# so no intra-session concurrency races this map. Different concurrent refreshes
# run on different sessions -> different registry entries. Weak keys let the map
# drop entries when a session is garbage-collected.
_held: "WeakKeyDictionary[AsyncSession, dict[int, _Holder]]" = WeakKeyDictionary()


async def _safe_close(conn: AsyncConnection) -> None:
    """Return a connection that provably holds NO lock to the pool."""
    try:
        await conn.close()
    except Exception:  # pragma: no cover - defensive
        pass


async def _invalidate(conn: AsyncConnection) -> None:
    """Terminate the backend so PostgreSQL frees every session lock it held.

    Used whenever the connection MAY hold the advisory lock but we cannot
    verifiably unlock it (broken/aborted/cancelled) — invalidation is the only
    way to guarantee the lock does not leak back into the pool.
    """
    try:
        await conn.invalidate()
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        await conn.close()
    except Exception:  # pragma: no cover - defensive
        pass


async def _release_and_close(conn: AsyncConnection, key: int) -> bool | None:
    """Release the session lock on ``conn`` and return it to the pool.

    Returns True (freed), False (unlock reported not-held), or None (unlock
    could not run — the connection was invalidated to force the backend to
    close, which frees the lock server-side regardless).

    Catches ``BaseException`` (not just ``Exception``) so a ``CancelledError``
    during the exit unlock still triggers the invalidate fail-safe rather than
    falling through to a plain ``close()`` that would strand the locked backend
    in the pool (Bug-6605). Never re-raises: the locked body's original
    exception must be the one that propagates.
    """
    try:
        result = await conn.execute(
            text("SELECT pg_advisory_unlock(:id)").bindparams(id=key)
        )
        released = bool(result.scalar())
        await conn.commit()
    except BaseException as exc:  # noqa: BLE001 - lock safety dominates
        logger.warning(
            "advisory unlock failed for key %s (%s); invalidating the lock "
            "connection so the backend releases the lock",
            key, exc,
        )
        await _invalidate(conn)
        return None
    await _safe_close(conn)
    return released


async def _enter(db: AsyncSession, key: int) -> _Holder | None:
    """Acquire ``key`` on a dedicated connection (or bump depth if re-entrant).

    Returns the holder when acquired/held, or ``None`` when another session
    already holds the key (busy). Raises only on a misconfigured session or an
    infrastructure failure during acquire (the connection is invalidated first
    so a possibly-taken lock cannot leak — Bug-6605).
    """
    holders = _held.get(db)
    holder = holders.get(key) if holders is not None else None
    if holder is not None:
        # Re-entrant acquire (e.g. incremental -> full on the same session):
        # reuse the connection that already holds the lock; do not open a
        # second one (which would deadlock the refresh against itself).
        holder.depth += 1
        return holder

    bind = db.bind
    if bind is None or not hasattr(bind, "connect"):
        # Production sessions bind an AsyncEngine (which has .connect()). A
        # session bound to an AsyncConnection (external-transaction test pattern)
        # cannot hand out an independent lock connection.
        raise RuntimeError(
            "advisory lock requires a session bound to an engine; got "
            f"{type(bind).__name__ if bind is not None else 'an unbound session'}"
        )

    # Check out an independent connection from the session's engine. It is held
    # open (never returned to the pool) until the context manager exits, so its
    # advisory lock cannot be released by the ORM session's commit churn.
    conn = await bind.connect()
    try:
        result = await conn.execute(
            text("SELECT pg_try_advisory_lock(:id)").bindparams(id=key)
        )
        acquired = bool(result.scalar())
        # End the implicit transaction so the lock connection does not sit
        # idle-in-transaction. A session-level advisory lock survives commit
        # (that is the whole point) because the connection/backend is unchanged.
        await conn.commit()
    except BaseException:
        # The try-lock may already have taken the lock; invalidate the backend
        # so it is freed rather than returned to the pool while held (Bug-6605).
        await _invalidate(conn)
        raise

    if not acquired:
        # Provably not held -> a plain close returns the connection to the pool.
        await _safe_close(conn)
        return None

    if holders is None:
        holders = {}
        _held[db] = holders
    holder = _Holder(conn)
    holders[key] = holder
    return holder


async def _exit(db: AsyncSession, key: int, holder: _Holder) -> None:
    """Balance the depth counter; release + close on the outermost exit.

    Release is gated on the depth counter, not on ``async with`` nesting order:
    the last frame to exit (depth back to 0) unlocks and closes the dedicated
    connection. Under strict incremental->full nesting this is the outermost
    frame; gating on depth keeps it correct even if that assumption breaks.
    """
    holder.depth -= 1
    if holder.depth > 0:
        return
    current = _held.get(db)
    if current is not None:
        current.pop(key, None)
        if not current:
            _held.pop(db, None)
    released = await _release_and_close(holder.conn, key)
    if released is False:
        logger.warning(
            "pg_advisory_unlock returned False for key %s -- the dedicated lock "
            "connection did not hold the lock (unexpected)",
            key,
        )


@asynccontextmanager
async def dedicated_advisory_lock(
    db: AsyncSession,
    key: int,
    *,
    on_conflict: Callable[[], Exception],
) -> AsyncIterator[None]:
    """Hold advisory lock ``key`` on a dedicated connection for the whole
    ``async with`` body, surviving every commit/rollback the ORM session
    performs. Raise ``on_conflict()`` when another session already holds ``key``.
    Re-entrant on the same ``db``.
    """
    holder = await _enter(db, key)
    if holder is None:
        raise on_conflict()
    try:
        yield
    finally:
        await _exit(db, key, holder)


@asynccontextmanager
async def dedicated_advisory_lock_optional(
    db: AsyncSession, key: int
) -> AsyncIterator[bool]:
    """Non-raising variant: yield ``True`` when ``key`` was acquired (held on a
    dedicated connection for the body) or ``False`` when another session holds
    it. Used by the sweep single-flight guard, which skips on ``False``.
    """
    holder = await _enter(db, key)
    if holder is None:
        yield False
        return
    try:
        yield True
    finally:
        await _exit(db, key, holder)
