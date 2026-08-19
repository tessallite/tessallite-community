"""RUNTIME invariant: no write to snapshot-owned state without the model lock.

Why this exists instead of a better static checker (Bug-7982 R7, findings 3+4)
-----------------------------------------------------------------------------
Three consecutive external cross-family gates rejected three successive versions
of a STATIC "prove the lock covers this write" checker
(``shared/db/model_lock_coverage.py``):

  R3   substring match           -> defeated by a comment containing the call
  R4/5 regex, then AST presence  -> defeated by a docstring; then by dead code
  R6   AST + hardcoded receiver  -> defeated by ``stmt = update(...); db.execute(stmt)``
       names + bare-call check      and by ``db.execute(delete(T).where(...))``,
                                    and by an unrecognised session variable name
  R6   route-derived discovery   -> structurally blind to a NON-route writer
       over ``src.api``             (``optimizer/src/stats/collector.py``)

These are not four unrelated bugs. They are one root cause with two faces:

  * the checker PROVES coverage by recognising code SHAPES, so it silently
    passes every shape it does not recognise; and
  * it ENUMERATES writers by guessing where writers live (FastAPI routes whose
    path contains ``{model_id}``, in the ``src.api`` package), so it is blind by
    construction to background jobs, sweeps, scripts, and routes scoped by some
    other identifier.

Both biases point the same way: toward a false PASS. Narrowing the AST checker to
handle the two newly-reported shapes would leave shapes #3, #4 and #5 for a
fourth gate to find. The property being asserted is DYNAMIC ("at the moment this
write executes, is the lock held?"); it cannot be soundly settled by reading
source text.

This module asserts it where it is actually decidable: at the single chokepoint
every write must pass through, whatever code shape produced it. A SQLAlchemy
``before_cursor_execute`` listener registered on the ``Engine`` CLASS (so it
covers every engine in the process — services, background jobs, scripts and test
harnesses alike) sees the final SQL of every statement. If that statement writes
a snapshot-owned table and the connection's current transaction has not acquired
the per-model definition lock, the invariant is violated. No CODE SHAPE evades
it, because it never looks at code:

  * a chained builder, a Name-bound statement, a helper three frames down, a raw
    ``text()`` string, an ``executemany`` — all arrive here as SQL;
  * a non-route writer, another service, a cron job, an ad-hoc script — all use
    an Engine;
  * a session variable named ``foo`` is indistinguishable from one named ``db``.

BOUNDARY — the residual, enumerated (R7 review round 1, finding 4)
------------------------------------------------------------------
"No code shape evades it" is NOT "nothing evades it", and the first draft of this
docstring overclaimed exactly that. What the guard sees is SQL text on a
SQLAlchemy ``Engine``. Therefore:

1. It recognises the DML it enumerates: INSERT / UPDATE / DELETE / TRUNCATE /
   MERGE / ``COPY … FROM``. A write statement outside that list would not be
   seen. ``tests/unit/test_model_write_lock_guard.py`` pins each one so the list
   cannot silently shrink.
2. A write performed INSIDE a server-side function or procedure invoked from a
   ``SELECT`` is invisible — the statement text is a read. Not currently
   reachable: ``test_no_write_capable_server_side_routine_is_defined`` asserts
   the migrations define no such routine, so the residual is closed by
   construction rather than by hope. If that ever changes, so must this guard.
3. A write issued on a connection that is NOT a SQLAlchemy Engine connection
   (a raw asyncpg/psycopg handle) is invisible. Source-database traffic goes
   through ``shared/source_executor.py``'s connector clients, which never touch
   tenant-metadata tables, so it is outside the guarded domain by design.
4. ``AUTOCOMMIT`` makes ``pg_advisory_xact_lock`` meaningless — it is released
   the instant its own statement ends, so a recorded lock would outlive the real
   one. ``acquire_model_definition_lock`` REFUSES to run on an autocommit
   connection rather than let the guard record a lock that is already gone.
5. LOCK IDENTITY (Bug-8440). The first version asked only "is a lock held?", so
   writing model B's rows while holding model A's lock passed — the two models
   were serialised by nothing, which is the whole point of the lock. The guard
   now also compares the held key against the model identity the statement
   supplies (``_identity_mismatch``). That comparison is decidable only where
   the statement NAMES ``model_id`` and supplies its value rather than deriving
   it from a subquery; on any other shape it stays silent. That residual is a
   deliberate fail-OPEN on the identity dimension ONLY, argued at
   ``_identity_mismatch`` — the "no lock at all" check above is unchanged and
   unconditional, so the identity check can only ever ADD a report.

Deliberate non-holders
----------------------
Some paths rebuild snapshot-owned state WHOLESALE with no single owning model to
lock against — project import, and the catalogue/dbt/cube/AtScale importers,
which rehydrate into a model they created in the same transaction (nothing else
can reference it yet, so there is no race to serialise). They declare themselves
with :func:`model_write_lock_exempt` and a reason. That is a first-class part of
the design, not a workaround: without it, the highest-volume benign writer floods
the report and the operator turns the guard off.

Lock registration
-----------------
``acquire_model_definition_lock`` tags its acquisition statement with
``/* tessallite:model-lock:<key> */``. The listener recognises the tag on the
SAME connection object, in the SAME event stream, and records the lock. This
avoids bridging async session objects into a sync event handler and guarantees
the ordering is exactly the DB's ordering. The record is cleared on ``begin`` /
``commit`` / ``rollback`` / ``engine_connect``, which mirrors
``pg_advisory_xact_lock``'s transaction scope exactly. (Subtransaction rollback
does NOT release a transaction-level advisory lock in PostgreSQL, and correspondingly
does not clear the record.)

Modes (``settings.MODEL_WRITE_LOCK_GUARD_MODE``)
-----------------------------------------------
``strict`` raises :class:`ModelWriteWithoutLockError` — used by the guard test
suites, where a violation must fail the build. ``warn`` (the default) logs an
ERROR with the offending table and statement, turning a silent race into an
operator-visible signal without ever breaking a running product. ``off``
disables the listener for contexts that legitimately rewrite this state wholesale
(migrations, seeding, restore tooling).

A derivation FAILURE or INCOMPLETE result is handled the same way, and fails
CLOSED (Bug-8439). The
guarded table set comes from ``snapshot_owned_tables.derive()``. If that raises,
``strict`` refuses the write (:class:`GuardDerivationError`) instead of returning
an empty set — "no table is guarded" is the fail-OPEN answer, and in the mode
whose contract is "a violation fails the build", not knowing IS a failure. The
same exception is raised when a write target cannot be resolved or an exclusion
justification is contradicted; returning the partial table set would leave the
unknown targets unguarded.
``warn`` retries on a re-arm window and re-reports with the count of writes that
went unchecked, instead of caching the failure for the life of the process behind
a single startup ERROR line that nothing ever repeats. The window bounds LOG
VOLUME, so it applies to ``warn`` only — ``strict`` re-attempts the derivation on
every statement, because suppressing there would turn one transient fault into a
five-minute total write outage that outlives the fault (Bug-8705).

A ``warn`` report is RATE-LIMITED per table set, not emitted once and then muted
forever. The one-shot version was worse than useless: a benign unlocked write
(model CREATE on ``models``, an import rebuilding 23 tables) claimed the key
within minutes of process start, and a genuine race hours later was silent. The
window re-arms and the re-emitted line carries the count it suppressed, so a
RECURRING violation — which is what a real race looks like — stays visible.

Residual on identity, stated plainly: this guard proves a model-definition lock
IS HELD on the writing connection. It does not prove it is the lock for the SAME
model the statement writes — the statement's SQL does not carry that. Binding the
lock to the endpoint's own ``model_id`` remains ``model_lock_coverage``'s job.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

#: Tag embedded in the lock-acquisition SQL. Recognised by the listener.
LOCK_STATEMENT_TAG = "tessallite:model-lock"

#: The exact leading text ``model_lock.py`` emits, so the listener can rule the
#: tag out with an O(1) prefix test instead of scanning every statement.
_LOCK_TAG_PREFIX = f"/* {LOCK_STATEMENT_TAG}:"
_LOCK_TAG_RE = re.compile(re.escape(_LOCK_TAG_PREFIX) + r"(-?\d+)\s*\*/")

# An optionally schema-qualified, optionally double-quoted table reference. A
# quoted identifier may itself contain a dot ("weird.name"), so the qualifier
# split must respect quoting rather than split on every dot.
_TABLE_REF = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
_QUALIFIED_REF = rf"({_TABLE_REF}(?:\s*\.\s*{_TABLE_REF})?)"

# PostgreSQL allows an inheritance-scoping ``ONLY`` before the table name
# (``UPDATE ONLY t``, ``DELETE FROM ONLY t``). Skip it so the TABLE is captured
# rather than the keyword.
_ONLY = r"(?:ONLY\s+)?"

# The DML this guard recognises. Enumerated deliberately and pinned by
# ``tests/unit/test_model_write_lock_guard.py`` — see the BOUNDARY section of the
# module docstring. ``findall`` (not ``search``) so a write nested in a CTE
# — ``WITH x AS (DELETE FROM t ...)`` — is caught as well as a leading one.
#
# R7 review round 1, finding 4: MERGE and COPY were missing. MERGE did not merely
# go unseen — the generic UPDATE alternative captured the ``SET`` of
# ``WHEN MATCHED THEN UPDATE SET``, which ``_NON_TABLE_CAPTURES`` then discarded,
# so the statement read as "not a write at all". Both were proved live to write a
# guarded table in strict mode without raising.
_WRITE_TARGET_RES = (
    re.compile(
        rf"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|"
        rf"MERGE\s+INTO)\s+{_ONLY}{_QUALIFIED_REF}",
        re.IGNORECASE,
    ),
    # ``COPY t FROM …`` writes; ``COPY t TO …`` and ``COPY (SELECT …) TO …`` read.
    re.compile(
        rf"\bCOPY\s+{_ONLY}{_QUALIFIED_REF}\s*(?:\([^)]*\)\s*)?FROM\b",
        re.IGNORECASE,
    ),
)

# ``ON CONFLICT DO UPDATE SET`` makes the regex capture "SET"; ``FOR UPDATE
# NOWAIT`` captures "NOWAIT". Neither is a table.
_NON_TABLE_CAPTURES = {"set", "nowait", "of", "skip"}

# Comments and string literals can contain anything, including a full DML
# statement naming a guarded table. Scanning them produced a live-proved FALSE
# POSITIVE (an INSERT into an unguarded audit table whose inlined literal
# mentioned ``DELETE FROM measures`` was reported as a violation, and in strict
# mode would have broken a correct write). They are blanked before classification.
_NOISE_RE = re.compile(
    r"'(?:[^']|'')*'"          # single-quoted literal, '' escape
    r"|\$([A-Za-z_]\w*)?\$.*?\$\1?\$"  # dollar-quoted literal
    r"|--[^\n]*"               # line comment
    r"|/\*.*?\*/",             # block comment
    re.DOTALL,
)

_CONN_LOCK_KEY = "_tessallite_model_definition_locks"
_CONN_EXEMPT_KEY = "_tessallite_model_write_exempt"

# ---------------------------------------------------------------------------
# Bug-8440: the lock must be the RIGHT model's lock
# ---------------------------------------------------------------------------
# ``held_lock_keys(conn)`` being non-empty only proved that SOME per-model lock
# was held on this connection. Writing model B's rows while holding model A's
# lock therefore passed the guard — live-reproduced by the Codex cross-family
# gate. Two models are serialised against each other by nothing at all in that
# case, which is precisely the race the lock exists to remove.
#
# Duplicated from ``model_lock.model_advisory_lock_key`` rather than imported:
# ``model_lock`` imports THIS module for ``LOCK_STATEMENT_TAG``, so importing it
# back would be a cycle. ``test_the_guard_and_the_lock_primitive_agree_on_the_key``
# pins the two against each other so the duplication cannot drift — a divergent
# mask would silently stop every identity check matching.
_LOCK_KEY_MASK = 0x7FFFFFFFFFFFFFFF

#: The identity check is only attempted when the statement NAMES the owning
#: model column. A write scoped purely by a child key
#: (``DELETE FROM aggregate_columns WHERE aggregate_id = $1``) carries no model
#: identity to check, and demanding one there would report every correctly
#: locked cascade step.
_MODEL_ID_COLUMN_RE = re.compile(r"\bmodel_id\b", re.IGNORECASE)

#: A model id reached through a subquery is DERIVED, not supplied, so it is
#: absent from both the statement text and the bound parameters. Checking those
#: statements would report a correct write on missing evidence.
_SUBQUERY_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)

_UUID_TEXT_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

#: Bound on the executemany parameter scan. A bulk INSERT can carry tens of
#: thousands of rows and this runs inside ``before_cursor_execute``; every row of
#: one statement belongs to the same unit of work, so a sample settles it.
_MAX_PARAM_ROWS_SCANNED = 50

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_STRICT = "strict"
_VALID_MODES = (MODE_OFF, MODE_WARN, MODE_STRICT)


class ModelWriteWithoutLockError(RuntimeError):
    """A snapshot-owned table was written without the per-model definition lock."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_mode_override: str | None = None
_mode_lock = threading.Lock()


def _configured_mode() -> str:
    if _mode_override is not None:
        return _mode_override
    try:
        from shared.config.settings import get_settings

        mode = str(get_settings().MODEL_WRITE_LOCK_GUARD_MODE or MODE_WARN).lower()
    except Exception:  # noqa: BLE001 — never let config break a DB write
        return MODE_WARN
    return mode if mode in _VALID_MODES else MODE_WARN


@contextmanager
def guard_mode(mode: str):
    """Temporarily force the guard mode (tests, migrations, restore tooling)."""
    global _mode_override
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode!r}")
    with _mode_lock:
        previous, _mode_override = _mode_override, mode
    try:
        yield
    finally:
        with _mode_lock:
            _mode_override = previous


# ---------------------------------------------------------------------------
# Guarded table set (lazily derived — see snapshot_owned_tables.py)
# ---------------------------------------------------------------------------

_guarded_tables: frozenset[str] | None = None

#: Monotonic time of the last derivation failure, and how many statements have
#: been let through since. A FAILED derivation used to be cached permanently
#: (``_derivation_failed = True``) behind a single ERROR line, so one transient
#: fault at process start silently disabled the guard for the life of the process
#: — Bug-8439's second half. There is no "the guard is off" heartbeat anywhere,
#: so that line scrolls away and nothing ever says the invariant stopped being
#: checked.
_derivation_failed_at: float | None = None
_derivation_suppressed = 0

#: Monotonic time the "derivation succeeded but is INCOMPLETE" report last fired.
#: Bug-8723: that report used to be one-shot per process while the harder-failure
#: report re-armed — the quieter treatment for the quieter (and worse) fault.
_last_incomplete_report_at: float | None = None

#: ``before_cursor_execute`` runs on whatever thread issued the statement, so the
#: failure bookkeeping is a genuinely concurrent read-modify-write. Cheap: only
#: taken on the failure/recovery paths, never on the hot success path (which
#: short-circuits on the cached ``_guarded_tables``).
_derivation_lock = threading.Lock()

#: How long a failed derivation is trusted before it is retried. Same cadence as
#: the violation report's re-arm window: an operator who watches for one sees the
#: other.
_DERIVATION_RETRY_SECONDS = 300.0


class GuardDerivationError(RuntimeError):
    """The snapshot-owned table set could not be derived.

    Raised (``strict`` mode only) INSTEAD of letting a write through unchecked.
    ``strict`` is what the guard test suites and any fail-closed deployment run
    under, so a broken derivation fails the build rather than quietly removing
    the invariant.
    """


def reset_guarded_tables_cache() -> None:
    """Test hook: forget the derived set and any recorded derivation failure."""
    global _guarded_tables, _derivation_failed_at, _derivation_suppressed
    global _last_incomplete_report_at
    _guarded_tables = None
    _derivation_failed_at = None
    _derivation_suppressed = 0
    _last_incomplete_report_at = None


def guarded_tables() -> frozenset[str]:
    """The snapshot-owned table set, or ``frozenset()`` if it cannot be derived.

    FAIL-CLOSED (Bug-8439). Three behaviours the previous version lacked:

    * ``strict`` mode RAISES :class:`GuardDerivationError` rather than returning
      an empty or incomplete set. Either result leaves writes unchecked, which
      is the fail-OPEN answer; in the mode whose whole contract is "a violation
      must fail the build", not knowing is a failure.
    * ``warn`` mode RETRIES after ``_DERIVATION_RETRY_SECONDS`` instead of
      caching the failure forever, so a transient fault (a partially written
      source file on a shared tree, a mid-deploy import) self-heals.
    * every re-emission carries the number of writes that went unchecked in the
      meantime, so "the guard is inactive" stays visible the way a recurring
      violation does, instead of being one line at process start.
    """
    global _guarded_tables, _derivation_failed_at, _derivation_suppressed
    global _last_incomplete_report_at
    if _guarded_tables is not None:
        return _guarded_tables
    now = time.monotonic()
    strict = _configured_mode() == MODE_STRICT
    # Bug-8705 (review round 1): the suppression window exists to bound LOG
    # VOLUME in warn mode. Strict mode does not log — it raises — so applying the
    # window there bought nothing and cost everything: one transient fault turned
    # into a 300-second total write outage during which the derivation was never
    # re-attempted even after the fault cleared. Strict always retries.
    if (
        not strict
        and _derivation_failed_at is not None
        and (now - _derivation_failed_at) < _DERIVATION_RETRY_SECONDS
    ):
        with _derivation_lock:
            _derivation_suppressed += 1
        return frozenset()
    try:
        from shared.model_snapshot.snapshot_owned_tables import (
            derive,
            exclusion_justification_failures,
        )

        tables, unresolved = derive()
        justification_failures = exclusion_justification_failures()
    except Exception as exc:  # noqa: BLE001
        with _derivation_lock:
            _derivation_failed_at = now
            suppressed, _derivation_suppressed = _derivation_suppressed, 0
        if strict:
            raise GuardDerivationError(
                "Bug-7982/Bug-8439: the snapshot-owned table set could not be "
                "derived, so no write can be checked against it. Refusing the "
                "write in strict mode rather than letting it through unguarded."
            ) from exc
        logger.error(
            "Bug-7982: could not derive the snapshot-owned table set (%r) — the "
            "runtime model-write lock guard is INACTIVE and will retry in %ds "
            "(%d write statement(s) went unchecked since the last report).",
            exc, int(_DERIVATION_RETRY_SECONDS), suppressed,
        )
        return frozenset()
    # A derivation that SUCCEEDS but is INCOMPLETE is the fail-open direction
    # this module exists to remove: the returned set is missing whatever could
    # not be resolved, and every write to those tables passes unnoticed.
    #
    # Bug-8723: this used to be reported exactly once per process, because the
    # successful-but-incomplete set was cached and the function short-circuited
    # forever after. That is the same latched one-shot ERROR Bug-8705 rejected
    # for the FAILURE case — given to the quieter, worse case. An incomplete set
    # is therefore NOT cached: it re-derives (cheap — the derivation itself is
    # memoised) and re-reports on the same re-arm window a violation uses, so
    # "the guard has a hole" keeps saying so.
    if unresolved or justification_failures:
        if strict:
            details = []
            if unresolved:
                details.append("unresolved targets: " + ", ".join(unresolved))
            if justification_failures:
                details.append(
                    "contradicted exclusions: " + "; ".join(justification_failures)
                )
            raise GuardDerivationError(
                "Bug-8439/Bug-8441: the snapshot-owned table set was derived "
                "incompletely, so writes cannot be checked safely in strict "
                "mode (" + "; ".join(details) + ")."
            )
        with _derivation_lock:
            due = (
                _last_incomplete_report_at is None
                or (now - _last_incomplete_report_at) >= _DERIVATION_RETRY_SECONDS
            )
            if due:
                _last_incomplete_report_at = now
        if due:
            if unresolved:
                logger.error(
                    "Bug-8439: %d rehydrator write target(s) could not be "
                    "resolved to a table (%s) — the runtime model-write lock "
                    "guard does NOT cover them. See "
                    "shared/model_snapshot/snapshot_owned_tables.py.",
                    len(unresolved), ", ".join(unresolved),
                )
            if justification_failures:
                logger.error(
                    "Bug-8441: %d snapshot-owned exclusion(s) are contradicted "
                    "by rehydrator.py and have been DROPPED (those tables are "
                    "now guarded, which may produce new reports): %s",
                    len(justification_failures), "; ".join(justification_failures),
                )
        # Bug-8742: recovering from a FAILED derivation into an INCOMPLETE one
        # is still a recovery, and the count of writes that went unchecked
        # while the guard was down is the operator's only record that the
        # invariant had a hole. It must not be zeroed unreported here just
        # because the successor derivation is itself imperfect.
        with _derivation_lock:
            recovered_from = _derivation_failed_at
            unchecked = _derivation_suppressed
            _derivation_failed_at = None
            _derivation_suppressed = 0
        if recovered_from is not None:
            logger.error(
                "Bug-7982: the snapshot-owned table set derived again after a "
                "failure (incompletely — see the report above). %d write "
                "statement(s) went unchecked while it was down.", unchecked,
            )
        return tables
    with _derivation_lock:
        recovered_from, unchecked = _derivation_failed_at, _derivation_suppressed
        _guarded_tables = tables
        _derivation_failed_at = None
        _derivation_suppressed = 0
    if recovered_from is not None:
        # Bug-8705: without this, a self-heal erases the evidence. "The guard was
        # inactive for N writes and is now back" is the operator's only record
        # that the invariant had a hole, and it must not vanish with the fault.
        logger.error(
            "Bug-7982: the snapshot-owned table set derived successfully again "
            "after a failure — the runtime model-write lock guard is ACTIVE. "
            "%d write statement(s) went unchecked while it was down.", unchecked,
        )
    # Bug-8707: return the LOCAL set, not the module global. A concurrent
    # ``reset_guarded_tables_cache()`` between the assignment and the return
    # would hand the listener ``None``, and ``tables & None`` raises inside
    # ``before_cursor_execute`` — breaking a correct write.
    return tables


# ---------------------------------------------------------------------------
# Statement classification
# ---------------------------------------------------------------------------

_SPLIT_REF_RE = re.compile(rf"({_TABLE_REF})")


def _bare_table_name(captured: str) -> str:
    """Normalise a captured (possibly quoted, possibly qualified) table ref.

    Splits on the QUALIFIER dot only, so a quoted identifier that itself contains
    a dot survives intact (``"weird.name"`` is one table, not a schema
    qualification). Naive ``split(".")`` reported it as ``name``.
    """
    parts = _SPLIT_REF_RE.findall(captured)
    part = (parts[-1] if parts else captured).strip()
    if part.startswith('"') and part.endswith('"') and len(part) >= 2:
        part = part[1:-1].replace('""', '"')
    return part.lower()


def written_tables(statement: str) -> set[str]:
    """Every table this statement writes, lower-cased and unqualified.

    A plain ``SELECT`` short-circuits without scanning: it is the hot read path.
    (A ``SELECT`` that invokes a WRITING server-side routine is the enumerated
    residual #2 in the module docstring, closed by construction in the tests.)
    Everything else — including ``WITH``-prefixed CTEs that hide a write — is
    scanned with comments and string literals blanked first, so a DML statement
    quoted inside a literal is not mistaken for a real one.
    """
    if not statement:
        return set()
    if statement[:32].lstrip()[:6].upper() == "SELECT":
        return set()
    scrubbed = _NOISE_RE.sub(" ", statement)
    out: set[str] = set()
    for pattern in _WRITE_TARGET_RES:
        for captured in pattern.findall(scrubbed):
            ref = captured[0] if isinstance(captured, tuple) else captured
            name = _bare_table_name(ref)
            if name and name not in _NON_TABLE_CAPTURES:
                out.add(name)
    return out


# ---------------------------------------------------------------------------
# Per-connection lock state
# ---------------------------------------------------------------------------

def _clear_connection_state(conn) -> None:
    info = getattr(conn, "info", None)
    if isinstance(info, dict):
        info.pop(_CONN_LOCK_KEY, None)


def _clear_connection_exemption(conn) -> None:
    """Drop any exemption when a connection is (re)checked out of the pool.

    ``Connection.info`` is the POOLED connection's dict and survives checkin —
    verified live. ``connection_write_exempt``'s ``finally`` already pops the
    key, so no leak exists today, but a future ``commit()`` inside an exempt
    block would strand it and silently exempt that pooled connection for the
    rest of the process. Clearing at checkout bounds that to one transaction.
    Deliberately NOT cleared on begin/commit/rollback: an exemption must span the
    whole wholesale rebuild, which commits inside itself.
    """
    info = getattr(conn, "info", None)
    if isinstance(info, dict):
        info.pop(_CONN_EXEMPT_KEY, None)


def _record_lock(conn, key: str) -> None:
    info = getattr(conn, "info", None)
    if isinstance(info, dict):
        info.setdefault(_CONN_LOCK_KEY, set()).add(key)


def held_lock_keys(conn) -> set[str]:
    info = getattr(conn, "info", None)
    if isinstance(info, dict):
        return set(info.get(_CONN_LOCK_KEY) or ())
    return set()


def _candidate_lock_keys(statement: str, parameters) -> set[str]:
    """Every advisory-lock key a UUID in this statement could correspond to.

    Values arrive as ``UUID``, as text, or as 16 raw bytes depending on driver
    and paramstyle, and the id may be inlined in the SQL or bound — so all four
    channels are read. Anything that is not a UUID is ignored, so this
    over-collects rather than under-collects: a key the write is entitled to use
    must never be missing from this set, or a correct write is reported.
    """
    out: set[str] = set()

    def add(value) -> None:
        try:
            if isinstance(value, UUID):
                parsed = value
            elif isinstance(value, str):
                parsed = UUID(value)
            elif isinstance(value, (bytes, bytearray)) and len(value) == 16:
                parsed = UUID(bytes=bytes(value))
            else:
                return
        except (ValueError, TypeError, AttributeError):
            return
        out.add(str(parsed.int & _LOCK_KEY_MASK))

    for token in _UUID_TEXT_RE.findall(statement or ""):
        add(token)

    rows = parameters if isinstance(parameters, (list, tuple)) else (parameters,)
    for row in rows[:_MAX_PARAM_ROWS_SCANNED]:
        if isinstance(row, dict):
            values = row.values()
        elif isinstance(row, (list, tuple)):
            values = row
        else:
            values = (row,)
        for value in values:
            add(value)
    return out


def _identity_mismatch(statement: str, parameters, held: set[str]) -> str | None:
    """The held lock belongs to a DIFFERENT model than this write's target.

    Returns the operator-facing detail, or ``None`` when the statement gives no
    grounds to say so.

    FAIL DIRECTION, stated rather than implied. On a statement shape it does not
    recognise this check FAILS OPEN: it stays silent instead of reporting. That
    is deliberate and it is the ONLY place in this module where open is the
    right direction. Demanding a resolvable model identity from every guarded
    write would report every correctly locked cascade step scoped by a child key
    — and a guard whose report stream is mostly false is a guard operators turn
    off, which is the self-muting failure this module has already been rebuilt
    out of twice. Nothing here weakens the pre-existing check: a write with NO
    lock at all is reported exactly as before, unconditionally. This can only
    ADD a report, never remove one.

    The recognised, decidable case is a statement that NAMES ``model_id`` and
    supplies its value directly. That is the shape every model-scoped writer in
    this product actually emits.
    """
    # Cheap raw test first: no ``model_id`` token anywhere means there is
    # nothing to decide, and it skips the DOTALL scrub on the common statement.
    if not _MODEL_ID_COLUMN_RE.search(statement):
        return None
    scrubbed = _NOISE_RE.sub(" ", statement)
    # Re-tested on the scrubbed text so a ``model_id`` mention that lives only
    # inside a comment or a string literal does not open the check.
    if not _MODEL_ID_COLUMN_RE.search(scrubbed):
        return None
    if _SUBQUERY_RE.search(scrubbed):
        return None
    candidates = _candidate_lock_keys(statement, parameters)
    if not candidates or candidates & held:
        return None
    return (
        f"lock key(s) held on this connection: {sorted(held)}; "
        f"the model identifier(s) this statement supplies map to "
        f"{sorted(candidates)}"
    )


def _is_exempt(conn) -> str | None:
    info = getattr(conn, "info", None)
    if isinstance(info, dict):
        return info.get(_CONN_EXEMPT_KEY)
    return None


@contextmanager
def connection_write_exempt(conn, reason: str):
    """Mark a raw SYNC connection as a deliberate non-holder for ``reason``.

    Most callers want :func:`model_write_lock_exempt`, which takes the
    ``AsyncSession`` they already hold. This is the low-level form used by that
    wrapper and by tests that own a sync connection directly.
    """
    info = getattr(conn, "info", None)
    if not isinstance(info, dict):
        yield
        return
    previous = info.get(_CONN_EXEMPT_KEY)
    info[_CONN_EXEMPT_KEY] = reason
    try:
        yield
    finally:
        if previous is None:
            info.pop(_CONN_EXEMPT_KEY, None)
        else:
            info[_CONN_EXEMPT_KEY] = previous


@asynccontextmanager
async def model_write_lock_exempt(db, reason: str):
    """Declare an ``AsyncSession``'s writes a DELIBERATE non-holder for ``reason``.

    For the paths that rebuild snapshot-owned state WHOLESALE with no single
    owning model to lock against: project import, and the catalogue/dbt/cube/
    AtScale importers, which rehydrate into a model they created in the same
    transaction (nothing else can reference it yet, so there is no race).

    R7 review round 1, finding 2: the sync-only form of this shipped with a
    docstring claiming it was wired into those paths and ZERO callers. That is
    what made the ``warn`` report useless — the highest-volume benign writer
    claimed 26 table-set keys within minutes and muted the guard for everything
    that mattered. The exemption must be declared AT THE SOURCE, not compensated
    for by a dedup cache.

    The reason is recorded on the connection and, in ``warn`` mode, is the thing
    an operator sees is ABSENT when a report does fire.
    """
    conn = await db.connection()
    sync_conn = getattr(conn, "sync_connection", None)
    if sync_conn is None:  # pragma: no cover - non-async engine
        sync_conn = conn
    with connection_write_exempt(sync_conn, reason):
        yield


# ---------------------------------------------------------------------------
# Violation reporting
# ---------------------------------------------------------------------------

#: Re-arm window for a repeated report of the SAME table set, in seconds.
#: R7 review round 1, finding 1: this used to be "emit once per table set, ever".
#: That is strictly worse than no dedup. ``warn`` is the production default, and
#: a live run of the model-service integration suite showed 26 distinct guarded
#: tables each reported exactly once within minutes — all of them benign
#: (model CREATE, wholesale import rebuilds). Every one of those keys was then
#: permanently claimed, so a GENUINE unlocked write racing a revert hours later
#: would have been silent for the rest of the process's life. The window re-arms
#: and the re-emitted line carries the suppressed count, so a recurring
#: violation — the shape a real race has — stays visible.
_REPORT_REARM_SECONDS = 300.0
_MAX_REPORTED = 256

#: table-set key -> (monotonic time of last emission, suppressed since then)
_reported: dict[str, list] = {}


def _report(tables: set[str], statement: str, *, identity: str | None = None) -> None:
    mode = _configured_mode()
    listed = ", ".join(sorted(tables))
    if identity is None:
        detail = (
            f"Bug-7982: snapshot-owned table(s) [{listed}] were written WITHOUT the "
            f"per-model definition lock (shared/db/model_lock.py::"
            f"acquire_model_definition_lock). A concurrent model revert can discard "
            f"this write, or this write can land stale rows on top of a just-restored "
            f"snapshot. Statement: {statement[:400]}"
        )
        rearm_key = listed
    else:
        detail = (
            f"Bug-8440: snapshot-owned table(s) [{listed}] were written while "
            f"holding the per-model definition lock of a DIFFERENT model, which "
            f"serialises nothing between the two — {identity}. "
            f"Statement: {statement[:400]}"
        )
        # A separate rate-limit key: an identity mismatch must not be swallowed
        # by an unrelated unlocked-write report that already claimed this table
        # set inside the re-arm window. They are different defects.
        rearm_key = f"{listed} [identity]"
    if mode == MODE_STRICT:
        raise ModelWriteWithoutLockError(detail)
    now = time.monotonic()
    entry = _reported.get(rearm_key)
    if entry is not None and (now - entry[0]) < _REPORT_REARM_SECONDS:
        entry[1] += 1
        return
    suppressed = entry[1] if entry is not None else 0
    if len(_reported) >= _MAX_REPORTED:
        # Evict the least-recently-emitted key rather than clearing everything —
        # a full clear would re-log every known key in a burst.
        oldest = min(_reported, key=lambda k: _reported[k][0])
        _reported.pop(oldest, None)
    _reported[rearm_key] = [now, 0]
    if suppressed:
        logger.error(
            "%s (%d further occurrence(s) suppressed in the last %ds)",
            detail, suppressed, int(_REPORT_REARM_SECONDS),
        )
    else:
        logger.error("%s", detail)


def reset_reported_cache() -> None:
    """Test hook: allow the same table set to be reported again immediately."""
    _reported.clear()


# ---------------------------------------------------------------------------
# Engine-class listeners
# ---------------------------------------------------------------------------

def _on_transaction_boundary(conn, *_args, **_kwargs) -> None:
    # ``pg_advisory_xact_lock`` is released at transaction end, so the record
    # must not outlive the transaction that took it.
    _clear_connection_state(conn)


def _on_before_cursor_execute(
    conn, _cursor, statement, parameters, _context, _executemany
) -> None:
    if not statement:
        return
    # O(1) prefix test on the hot path: the producer always emits the tag as the
    # statement's leading comment (see model_lock.py), so a full scan is never
    # needed to rule it out.
    if statement.startswith(_LOCK_TAG_PREFIX):
        tag = _LOCK_TAG_RE.match(statement)
        if tag is not None:
            _record_lock(conn, tag.group(1))
            return
    if _configured_mode() == MODE_OFF:
        return
    tables = written_tables(statement)
    if not tables:
        return
    guarded = tables & guarded_tables()
    if not guarded:
        return
    # ORDER MATTERS since Bug-8440 (it did not before, when both of these
    # returned silently). A declared exemption must be checked FIRST. An
    # import-replace is the live case: ``project_rehydrator`` deletes the
    # pre-existing models through ``delete_model_cascade``, which RECORDS those
    # models' locks on this connection, and then rebuilds the new models' rows
    # under ``model_write_lock_exempt``. Asking the identity question first
    # would compare the new models' ids against the deleted models' lock keys
    # and report every import-replace — raising, in strict mode, on a write that
    # is correct by design.
    if _is_exempt(conn) is not None:
        return
    held = held_lock_keys(conn)
    if held:
        # Bug-8440: holding SOME model's lock is not holding THIS model's lock.
        mismatch = _identity_mismatch(statement, parameters, held)
        if mismatch is not None:
            _report(guarded, statement, identity=mismatch)
        return
    _report(guarded, statement)


_installed = False


def install() -> None:
    """Register the listeners on the Engine CLASS (idempotent).

    Class-level registration is deliberate: a per-engine hook would cover only
    the engines someone remembered to wire, which is the same "enumerate the
    places writers live" mistake the static guard made.
    """
    global _installed
    if _installed:
        return
    _installed = True
    event.listen(Engine, "before_cursor_execute", _on_before_cursor_execute)
    for boundary in ("begin", "commit", "rollback", "engine_connect"):
        event.listen(Engine, boundary, _on_transaction_boundary)
    event.listen(Engine, "engine_connect", _clear_connection_exemption)
