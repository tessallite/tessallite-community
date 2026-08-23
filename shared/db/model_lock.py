"""Per-model definition/governance advisory lock (Bug-7980 / Bug-7982).

A single per-model PostgreSQL transaction advisory lock serialises the
version-control operations (Save, deploy, revert, undeploy, retention prune)
against EVERY model-scoped writer of snapshot-owned definition/governance state.

This helper lives in ``shared`` so BOTH the model-service (definition/governance
writers + the version-control operations) and the scheduler (the refresh-SLA
writers in ``services/scheduler/src/api/sla.py``, which mutate the snapshot-owned
``RefreshSLAConfig``) acquire the SAME lock on the SAME key. A lock helper that
lived in only one service silently left the other service's model-scoped writers
unserialised (Bug-7982 R6 reviewer finding 6).

Coverage is enforced structurally, not by a hand-maintained module list: each
service's ``test_model_lock_coverage.py`` DISCOVERS every ``src.api`` module,
enumerates every ``APIRouter``'s model-scoped (``{model_id}``) mutating route, and
requires each to EFFECTIVELY acquire this lock (reachable, dominating the first
write, bound to the endpoint's own ``model_id``) OR be on an explicit,
reasoned ``_ALLOW_NO_LOCK`` (read/compute/telemetry/operational/integration, or an
entity that is preserved-in-place — not truncate-reinserted — on revert). A new
model-scoped mutating route therefore fails the guard until it locks or is
consciously allow-listed. ``project_rehydrator`` project import is a DELIBERATE
non-holder for the REBUILD half only — the models it rehydrates into are created
in the same transaction, so nothing can reference them yet — and it declares that
with ``model_write_lock_exempt``. Its ``replace``-mode DELETE half is NOT exempt:
it removes pre-existing models through ``delete_model_cascade``, which holds the
lock (Save additionally snapshots under a REPEATABLE READ connection).

Two holders the ROUTE-derived guard cannot see, so they are named here instead
(Bug-8703):

  * ``shared/model_snapshot/cascade_delete.py::delete_model_cascade`` acquires
    the lock as its FIRST statement. It is the single canonical model-delete
    path, so all three of its callers inherit it — the model DELETE endpoint,
    ``delete_project_cascade`` behind the ``{project_id}``-shaped project DELETE
    route, and ``project_rehydrator`` import-replace. Locking at any one call
    site would have left the other two unlocked; that is why the lock lives in
    the primitive. Its two multi-model callers sort model ids by ``str(uuid)``.
  * migration ``0194``'s ``_lock_models``, for the same reason (a migration is
    not a route), sorted the same way.

ORDER, stated once so every holder can be checked against it: take the advisory
lock for a model BEFORE touching any row that model owns, and where several
models are involved, acquire in ``sorted(key=str)`` order. Reaching the rows
first completes an ABBA cycle through the FK ``KEY SHARE`` on ``models`` and
PostgreSQL aborts one side with ``40P01``, mid-write.

Every caller MUST use ``model_advisory_lock_key`` so the key is byte-identical
across call sites — a divergent key would silently stop serialising. The lock is
transaction-scoped (``pg_advisory_xact_lock``) and released automatically on
commit or rollback.

Ordering contract (which clause wins when they conflict):
  * When ownership is validated by a SEPARATE step that reads only the Model
    (``ensure_model_in_project`` / ``_get_model``), acquire the lock AFTER it so an
    unauthorised model_id does not contend for the cluster-wide lock; the
    write-target entity is then read AFTER the lock.
  * When ownership and the write-target read are the SAME step (a
    read-modify-write whose ownership check IS the entity fetch, e.g.
    ``db.get(KPI, kpi_id); if kpi.model_id != model_id`` or ``_get_table_or_404`` /
    ``_get_scoped_rule``), READ-UNDER-LOCK WINS: acquire the lock FIRST, then
    read+check the entity, so a concurrent revert cannot make the handler operate
    on stale entity state (Bug-7980). The coarse ``require_role`` dependency still
    gates entry before the handler body and the 404/403 raises immediately after
    the read (releasing the lock), so the residual contention is negligible.
It must NOT be held across a slow external call (LLM / source DDL); acquire it
late, just before the write, in those endpoints (glossary/translation bootstrap,
calendar auto-create) and re-read any decision input under the lock at that point.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.db.model_write_lock_guard import LOCK_STATEMENT_TAG

logger = logging.getLogger(__name__)

# 63-bit positive bigint — pg_advisory_xact_lock takes a signed bigint.
_LOCK_KEY_MASK = 0x7FFFFFFFFFFFFFFF

# PostgreSQL SQLSTATE for "lock timeout" (raised when ``lock_timeout`` expires
# while waiting to acquire a lock, including an advisory lock).
_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


def model_advisory_lock_key(model_id: UUID) -> int:
    """The single canonical advisory-lock key for a model. All lockers share it."""
    return model_id.int & _LOCK_KEY_MASK


class AutocommitLockError(RuntimeError):
    """``pg_advisory_xact_lock`` was requested on an AUTOCOMMIT connection.

    A transaction-scoped advisory lock taken with no surrounding transaction is
    released immediately and serialises nothing. This is always a programming
    error at the call site, never a runtime condition to retry.
    """


def _is_autocommit(conn) -> bool | None:
    """Whether an established ``AsyncConnection`` is in AUTOCOMMIT.

    ``None`` = could not determine. Read from the DBAPI connection's own state
    (a plain attribute read: no IO, no greenlet requirement). Verified
    empirically against this project's asyncpg driver, because the obvious
    alternatives do NOT work here: ``AsyncConnection`` has no
    ``get_execution_options``; engine/connection ``_execution_options`` are empty
    even for an ``isolation_level="AUTOCOMMIT"`` engine; ``get_isolation_level()``
    raises ``MissingGreenlet``; and ``in_transaction()`` is True in BOTH modes.
    """
    try:
        sync = getattr(conn, "sync_connection", None) or conn
        pooled = getattr(sync, "connection", None)
        raw = getattr(pooled, "dbapi_connection", None) if pooled is not None else None
        if raw is None:
            return None
    except Exception:  # noqa: BLE001 — connection not established
        return None
    autocommit = getattr(raw, "autocommit", None)
    if autocommit is True:
        return True
    level = getattr(raw, "isolation_level", None)
    if isinstance(level, str):
        return level.upper() == "AUTOCOMMIT"
    return False if autocommit is False else None


async def _reject_autocommit(db: AsyncSession) -> None:
    """Raise if ``db``'s connection is running in AUTOCOMMIT.

    Establishes the connection first (``db.connection()``) so the check has a
    real DBAPI handle to inspect rather than silently returning "unknown" on the
    very first statement of a session — a swallow that would defeat the check
    exactly when it matters.

    A session-like object that exposes no ``connection()`` at all (a test double)
    yields "unknown", which is LOGGED, not silently accepted. It must never crash
    the lock acquisition: refusing to lock because the autocommit CHECK could not
    run would be a worse outcome than the condition it screens for.
    """
    connect = getattr(db, "connection", None)
    verdict = _is_autocommit(await connect()) if callable(connect) else None
    if verdict is True:
        raise AutocommitLockError(
            "acquire_model_definition_lock was called on an AUTOCOMMIT "
            "connection. pg_advisory_xact_lock is transaction-scoped, so the "
            "lock would be released immediately and serialise nothing, while the "
            "runtime write guard would go on treating this connection as locked. "
            "Run the model-definition write inside a real transaction."
        )
    if verdict is None:
        # Never silently pass: an undetermined answer is logged so the gap is
        # visible rather than being an invisible hole in the check.
        logger.warning(
            "Bug-7982: could not determine whether this connection is in "
            "AUTOCOMMIT before acquiring the per-model definition lock; "
            "proceeding. If it IS autocommit, the lock serialises nothing."
        )


async def acquire_model_definition_lock(db: AsyncSession, model_id: UUID) -> None:
    """Acquire the per-model definition/governance advisory lock on ``db``.

    Bug-7982 completion round: bounds how long THIS call blocks waiting for the
    lock with a PostgreSQL ``lock_timeout``
    (``settings.MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS``), so a slow holder cannot
    starve every other definition writer indefinitely. A wait that exceeds the
    timeout raises a clear, retryable 503 instead of hanging; it never affects how
    long another transaction may continue to HOLD the lock once acquired.

    Ordering (opus5 finding 4.1): PostgreSQL does not guarantee the evaluation
    order of sibling SELECT-target expressions, so the timeout must be SET before
    the lock is acquired via structure, not sibling ordering. A chain of
    MATERIALIZED CTEs, each referenced in the FROM of the next, forces
    dependency-order evaluation and cannot be inlined/reordered (``set_config``
    is VOLATILE; MATERIALIZED additionally forbids pull-up).

    R6 finding 5: ``set_config('lock_timeout', X, true)`` is ``SET LOCAL`` —
    transaction-scoped, NOT statement-scoped. Left as-is, the short acquisition
    timeout would silently govern EVERY later statement in the same transaction,
    so a later wait that trips the cap surfaces as an unhandled 500. The statement
    captures the prior ``lock_timeout``, sets the short one, acquires the lock
    under it, then RESTORES the prior value — so ONLY the acquisition is bounded.
    Still exactly one ``db.execute()`` (the call-count contract several
    ordered-mock unit tests depend on):

      prev  -> current_setting('lock_timeout')                  (capture original)
      setshort -> set_config('lock_timeout', :timeout, true)    (bound acquisition)
      acq   -> pg_advisory_xact_lock(:key)                      (acquire under bound)
      final -> set_config('lock_timeout', prev, true)           (restore original)
    """
    # Bug-7982 R7 (review round 1, finding 4 case 8): ``pg_advisory_xact_lock``
    # is released at the end of its TRANSACTION. Under AUTOCOMMIT there is no
    # transaction beyond the statement itself, so the lock is gone the instant
    # this call returns — it serialises nothing, and the runtime write guard
    # would go on treating the connection as locked for every later statement.
    # Refuse loudly rather than hand back a lock that does not exist.
    await _reject_autocommit(db)
    settings = get_settings()
    timeout = f"{int(settings.MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS)}s"
    key = model_advisory_lock_key(model_id)
    # Bug-7982 R7 (findings 3+4): tag the acquisition so the RUNTIME write guard
    # (shared/db/model_write_lock_guard.py) can record the lock on this exact
    # connection, in the same event stream, without bridging an async session
    # into a sync SQLAlchemy event handler. The tag is a SQL comment and carries
    # only the integer lock key, which is derived from the model UUID.
    lock_tag = f"/* {LOCK_STATEMENT_TAG}:{key} */"
    try:
        await db.execute(
            text(
                f"{lock_tag} "
                "WITH "
                "prev AS MATERIALIZED ("
                "  SELECT current_setting('lock_timeout') AS orig"
                "), "
                "setshort AS MATERIALIZED ("
                "  SELECT set_config('lock_timeout', :timeout, true) AS s, "
                "         prev.orig AS orig FROM prev"
                "), "
                "acq AS MATERIALIZED ("
                "  SELECT pg_advisory_xact_lock(:key) AS l, "
                "         setshort.orig AS orig FROM setshort"
                ") "
                "SELECT set_config('lock_timeout', acq.orig, true) FROM acq"
            ),
            {"timeout": timeout, "key": key},
        )
    except DBAPIError as exc:
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None)
        if sqlstate == _LOCK_NOT_AVAILABLE_SQLSTATE:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "This model's definition is locked by another concurrent "
                    "change (Save, deploy, revert, or another definition edit) "
                    "that has not finished yet. Retry in a moment."
                ),
            ) from exc
        raise
