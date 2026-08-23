"""Soundness rules for the pocket incremental (windowed DELETE/INSERT) leg.

Bug-8699 and Bug-8700. This module owns *when* a pocket may be patched instead
of rebuilt, *which window* the patch must cover, and *which SQL* expresses it.
``shared/pocket/refresh.py`` only executes what this module decides.

The structural problem
----------------------
A windowed DELETE/INSERT is only a correct way to bring a cached table up to
date when the DELETE can find and remove the row copy the INSERT is about to
supersede. The original implementation matched the two by the WATERMARK COLUMN
VALUE::

    DELETE FROM dst USING delta AS src WHERE dst.<watermark> = src.<watermark>

A watermark column exists precisely because it MOVES when a row changes, so the
cached copy of an edited row sits at its OLD watermark and the DELETE — looking
for the NEW one — never finds it. The INSERT then adds a second copy of the same
business row (Bug-8699).

Deleting by the window RANGE instead (``dst.<watermark> >= <window start>``,
what the aggregate sibling does) does NOT fix that case: the stale copy is
outside the window by construction, which is exactly why the row entered the
delta. Range deletion only works for an append-only source whose watermark never
moves, which is not a property a pocket declares anywhere.

The only sound matcher is ROW IDENTITY. A pocket caches ``SELECT * FROM
<model>`` (see ``shared/pocket/structure.py``), so its rows are the model's rows
and their identity is the model FACT table's declared primary key — provided
that key is actually materialised as an output column of the cached table. When
it is not, no sound incremental patch exists and the caller must rebuild in
FULL. That is a fail-closed degradation to the documented pocket behaviour,
never a wrong number. Joined models take the same full-refresh path unless the
refresh driver can prove complete per-table watermark coverage (Bug-8745).

The window
----------
The delta window used to be ``NOW() - incremental_lookback_hours`` — a wall-clock
delta with no reference to when this pocket was last actually refreshed. Any gap
between two successful refreshes longer than the lookback (a paused scheduler, a
lock-busy skip, a weekly cron with a 24h lookback) left every source row that
changed inside the gap permanently invisible to every future run (Bug-8700).
The window start is therefore anchored to the pocket's own last COMPLETED
refresh run, with the lookback kept only as a late-arriving-data cushion. No
prior completed run means there is nothing to anchor to, and the only sound
answer is a FULL rebuild.

The anchor is the previous run's ``started_at``, not its ``completed_at``: the
previous run read the source at some point between the two, so anchoring to the
later timestamp assumes the lookback cushion exceeds that run's duration. It
usually does; ``started_at`` removes the assumption at the cost of re-processing
rows that changed while the previous run was executing, which is idempotent
under an identity-keyed DELETE/INSERT.

``started_at`` is stamped by the TENANT METADATA database's clock, while the
watermark values are written by the SOURCE database's clock, so the anchor is
inherently a cross-server quantity. That is why the window is handed to the
source as a DURATION rather than as an absolute instant. Offset-aware values use
the source's ``NOW()``; naive timestamps use the source's UTC wall clock because
Tessallite's naive-timestamp contract is UTC. This keeps a session timezone
ahead of UTC from moving the whole delta window into the future (Bug-8746).

Be precise about which offset cancels, because the first version of this
paragraph over-claimed. The elapsed seconds are ``datetime.now()`` on the
SCHEDULER PROCESS minus a ``window_start`` derived from ``started_at`` on the
METADATA DATABASE, so a process/metadata skew does NOT cancel — it shifts the
window by that skew. Two things absorb it: the lookback cushion, and (when the
shift is larger than the cushion) the reconciliation invariant, which sees the
rows that fell outside as cached-at-a-different-watermark and forces a full
rebuild. So the residual is a wasted rebuild, not a wrong number.

:func:`build_window_expression` documents the second, larger reason for the
duration form (a UTC literal is silently wrong against a naive watermark
column).

What the delta cannot see, and the invariant that covers it
-----------------------------------------------------------
A watermark delta only ever asks "which rows have a recent watermark", so plenty
of real changes never appear in it. Earlier revisions of this module guarded a
growing LIST of those shapes, and three consecutive external review rounds each
found one more member of the list by live reproduction — a static enumeration
being asked to prove a dynamic property, which CLAUDE.md names as an escalation
shape rather than something to keep patching. The leg now rests on ONE positive
invariant instead, checked before it mutates anything
(:func:`build_unreconciled_key_probe_sql`):

    For every key the source currently produces, EITHER the cache already holds
    that key at that exact watermark, OR the delta carries it. Otherwise refuse
    and rebuild in FULL.

That subsumes the whole enumeration: a key missing from the cache (a late
dimension row completing an inner join; a late-arriving load), and a key cached
at a DIFFERENT watermark than the source now reports (a backdated correction —
the shape an EXISTS-only probe could not see, because the key WAS cached), both
fail it. A key cached at the same watermark means the source has not touched
that row, so leaving it alone is correct.

Two things sit alongside the invariant because it does not cover them:

* **Rows the source no longer produces** — deleted at source, or dropped by the
  pocket's WHERE. They are not in the key set at all, so the invariant is silent
  on them; the orphan anti-join against the key set removes them.
* **Multiplicity.** The invariant asks EXISTS, so a key the source produces
  TWICE satisfies it. ``build_key_integrity_probe_sql`` over the key set covers
  that, and over the cache and the delta as well.

The one true residual for a permitted single-table incremental pocket, which
nothing downstream can detect: a source that edits a row WITHOUT moving its own
watermark. That is the user's contract with their source, and the help page
says so plainly. A joined model is refused before this leg unless complete
per-table coverage is proven (Bug-8745).

The key set, delta, integrity probes, and DELETE/INSERT patch now share one
REPEATABLE READ source transaction (Bug-8741 / source Bug-8739). Concurrent
inserts and deletes therefore cannot appear in only one scan. Key-set first is
retained for clarity and stable statement shape, but correctness no longer
depends on a READ COMMITTED ordering trade-off. The empty-key-set guard remains
useful for a source snapshot captured mid ``TRUNCATE``-and-reload: one stable
snapshot can still represent an incomplete non-atomic load even though its own
statements agree with each other.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Iterable, Sequence

__all__ = [
    "DEFAULT_LOOKBACK_HOURS",
    "FACT_TABLE_TYPE",
    "POCKET_RUN_STATUS_COMPLETED",
    "build_delta_sql",
    "build_identity_delete_sql",
    "build_insert_from_delta_sql",
    "build_key_count_sql",
    "build_key_integrity_probe_sql",
    "build_key_scan_sql",
    "build_orphan_delete_sql",
    "build_unreconciled_key_probe_sql",
    "build_window_expression",
    "resolve_incremental_watermark_coverage",
    "resolve_incremental_window_start",
    "resolve_row_identity_candidates",
    "usable_identity_columns",
    "window_elapsed_seconds",
]

# The single spelling of a successful pocket refresh run. ``refresh.py`` writes
# it and this module reads it; keeping one constant is what stops the window
# anchor from silently matching nothing after a rename.
POCKET_RUN_STATUS_COMPLETED = "completed"

# ``PocketDefinition.incremental_lookback_hours`` is nullable; this is the
# cushion applied when the pocket declares none.
DEFAULT_LOOKBACK_HOURS = 24

# ``ModelTable.table_type`` value identifying the star's centre. A model has at
# most one (DB-enforced: ``uq_model_tables_one_fact_per_model``).
FACT_TABLE_TYPE = "fact"


async def resolve_incremental_watermark_coverage(
    db: Any,
    *,
    model_id: Any,
    version_id: Any | None,
    incremental_column: str | None,
) -> bool:
    """Prove whether the configured watermark covers the pocket's model.

    Bug-8745. A pocket materialises the model's joined row population, while
    the current product stores exactly one ``incremental_column``. That one
    watermark cannot prove freshness for a joined dimension: changing the
    dimension can change the materialised number without changing the fact
    row's watermark. Until per-table watermark metadata exists, the only
    authoritative proof available is the captured deployed snapshot with
    exactly one valid fact table. The snapshot is keyed by the build-start
    ``version_id`` rather than by the live model pointer: draft ``ModelTable``
    rows must never influence a build that is pinned to an immutable deployed
    definition.

    The read deliberately fails closed: no column, no captured version, a
    missing/unavailable/malformed/mismatched snapshot, or more than one
    snapshot table returns ``False``. The caller then uses the full CTAS path.
    This is a positive snapshot-structure check, not a static enumeration of
    join shapes, so a newly introduced draft table cannot accidentally bypass
    it. A metadata read failure is also treated as unproven coverage.
    """
    if not incremental_column or version_id is None:
        return False

    from sqlalchemy import select

    from shared.db.models import ModelVersion

    try:
        row = (
            await db.execute(
                select(
                    ModelVersion.model_id,
                    ModelVersion.snapshot_json,
                    ModelVersion.snapshot_unavailable,
                ).where(ModelVersion.id == version_id)
            )
        ).first()
    except Exception:
        # Coverage is a precondition for the delta leg, not a reason to make
        # the refresh itself fail before it can take the safe full path.
        return False

    if row is None:
        return False
    try:
        snapshot_model_id, snapshot_json, snapshot_unavailable = row[0:3]
    except (IndexError, KeyError, TypeError):
        return False
    if snapshot_unavailable:
        return False
    if str(snapshot_model_id) != str(model_id):
        return False
    if not isinstance(snapshot_json, dict):
        return False

    tables = snapshot_json.get("tables")
    if not isinstance(tables, list) or len(tables) != 1:
        return False
    table = tables[0]
    if not isinstance(table, dict):
        return False

    # These are the identity/shape fields emitted for every ModelTable by the
    # model snapshot serialiser. Requiring them prevents a partial or foreign
    # snapshot from being treated as the one-table proof. A model with one
    # valid table must have the fact anchor; a lone dimension is not a valid
    # model topology for this decision.
    required = ("id", "model_id", "table_type", "physical_name")
    if any(not table.get(field) for field in required):
        return False
    if str(table["model_id"]) != str(model_id):
        return False
    if table["table_type"] != FACT_TABLE_TYPE:
        return False
    if not isinstance(table["physical_name"], str):
        return False
    return True


async def resolve_incremental_window_start(
    db: Any,
    *,
    pocket_id: Any,
    lookback_hours: int | None,
) -> datetime | None:
    """Window start for the next incremental delta, or None to force FULL.

    Bug-8700. Anchored to the pocket's last COMPLETED refresh run so the window
    always covers the whole interval since the cache was last known correct,
    however long the scheduler was away. Returns ``None`` when no completed run
    exists to anchor to — the caller must then rebuild in full rather than
    invent a wall-clock window that can skip rows.
    """
    from sqlalchemy import select

    from shared.db.models import PocketRefreshRun

    row = (
        await db.execute(
            select(PocketRefreshRun.started_at, PocketRefreshRun.completed_at)
            .where(
                PocketRefreshRun.pocket_definition_id == pocket_id,
                PocketRefreshRun.status == POCKET_RUN_STATUS_COMPLETED,
            )
            .order_by(PocketRefreshRun.completed_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    anchor = row[0] or row[1]
    if not isinstance(anchor, datetime):
        # A completed run with neither timestamp cannot anchor anything.
        return None
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    hours = int(lookback_hours or DEFAULT_LOOKBACK_HOURS)
    if hours < 1:
        hours = 1
    return anchor - timedelta(hours=hours)


async def resolve_row_identity_candidates(db: Any, *, model_id: Any) -> tuple[str, ...]:
    """Declared row-identity columns for a pocket over ``model_id``.

    Bug-8699. A pocket caches ``SELECT * FROM <model>``, so one cached row is one
    model row and its identity is the FACT table's declared primary key.

    Returns an empty tuple — meaning "no sound incremental patch exists, rebuild
    in full" — when the model declares no fact primary key, or when any of its
    names is also carried by another table of the same model. The second rule is
    load-bearing: the cached table exposes bare column names, so a fact key named
    ``id`` that a dimension also carries cannot be matched to the fact column with
    confidence, and keying the DELETE on a dimension's id would remove every
    cached row sharing it — turning a duplication bug into a data-loss bug.

    Known limitation (reviewer round 2, recorded rather than engineered around):
    this establishes that the NAME is unambiguous in the model's declared
    columns, not that the cached column bearing that name PROVABLY originated
    from the fact table. The one gap left is stale drift metadata — a same-named
    dimension column marked ``drift_removed`` while the deployed projection still
    emits it. The authoritative answer would come from the pocket's own
    ``row_manifest`` / the deployed snapshot's column provenance rather than from
    ``ModelColumn``.

    What actually stops a wrong-column key from being USED is the three on-disk
    uniqueness probes in ``refresh.py`` — over the CACHE, the SOURCE's current
    key set, and the DELTA. Round 2 credited only the first two here, which
    round 3 showed was a real gap and not merely imprecise wording: a key that
    is unique in the cache and the delta but duplicated in the source population
    is invisible to a semi-join and to an EXISTS probe alike, and the pocket
    under-reports permanently. All three sets are probed now.
    """
    from sqlalchemy import select

    from shared.db.models import ModelColumn, ModelTable

    rows = (
        await db.execute(
            select(
                ModelTable.table_type,
                ModelColumn.column_name,
                ModelColumn.is_primary_key,
            )
            .join(ModelColumn, ModelColumn.model_table_id == ModelTable.id)
            .where(
                ModelTable.model_id == model_id,
                ModelColumn.drift_removed.is_(False),
            )
        )
    ).all()
    if not rows:
        return ()

    occurrences: Counter[str] = Counter(str(r[1]) for r in rows)
    fact_keys = sorted(
        {
            str(r[1])
            for r in rows
            if bool(r[2]) and str(r[0]) == FACT_TABLE_TYPE
        }
    )
    if not fact_keys:
        return ()
    if any(occurrences[name] > 1 for name in fact_keys):
        return ()
    return tuple(fact_keys)


def usable_identity_columns(
    *,
    candidates: Sequence[str],
    destination_columns: Iterable[str],
    incremental_column: str,
    incremental_column_data_type: str,
) -> tuple[str, ...]:
    """Narrow declared identity candidates to ones the CACHED TABLE really has.

    The declared key is metadata; what the incremental DELETE can actually match
    on is whatever the cached table exposes. The model's published projection
    hides columns and may rename them (a persona star exposes semantic aliases),
    so a declared key that is not present VERBATIM in the target catalogue cannot
    be used and the caller must rebuild in full.

    The watermark column is checked the same way. It is not used to match rows
    any more, but the delta SELECT still filters on it, so a pocket whose
    ``incremental_column`` is not an output column of its own cached table would
    fail the whole refresh; degrading to a full rebuild is both correct and
    self-healing.

    Comparison is EXACT (case-sensitive), matching the row-manifest contract the
    query-router's RLS gate uses. Anything else is refused rather than guessed.
    """
    if not candidates:
        return ()
    destination = set(destination_columns or ())
    if not destination:
        return ()
    if incremental_column not in destination:
        return ()
    # The delta compares the watermark against a time expression, so a
    # non-temporal watermark cannot be filtered at all. Before this check the
    # run blew up with ``operator does not exist: text >= timestamp with time
    # zone``, marking the pocket ``failed``; the next sweep then saw ``failed``,
    # rebuilt in full to ``fresh``, and the run after that failed again — a
    # permanent flap of one failed run per two sweeps. Refusing here degrades it
    # to a plain, quiet full rebuild instead.
    if not _is_temporal_type(incremental_column_data_type):
        return ()
    if not set(candidates).issubset(destination):
        return ()
    return tuple(candidates)


def _is_temporal_type(data_type: str) -> bool:
    """True for a column type the delta's time comparison can actually filter."""
    normalised = (data_type or "").strip().lower()
    return normalised.startswith(("date", "timestamp"))


def window_elapsed_seconds(
    window_start: datetime, now: datetime | None = None
) -> int | None:
    """How far BACK from the source's own clock the delta window reaches.

    The window is anchored (Bug-8700) but expressed as a DURATION, not as an
    absolute instant, and that is deliberate — see
    :func:`build_window_expression`. Rounded UP so the window can only ever be
    wider than computed, never narrower.

    Returns ``None`` — meaning "rebuild in FULL" — when the anchor is not in the
    past. A window start later than now means the clock that stamped
    ``PocketRefreshRun.started_at`` and the clock reading it disagree by more
    than the entire lookback. Clamping that to a one-second window (the previous
    behaviour) is the worst possible answer: it produces an almost-empty delta
    that leaves every changed row stale and SILENT, because those rows' keys are
    already cached and no probe fires on them. Every other impossible state in
    this module fails closed and so does this one.
    """
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    elapsed = ceil((reference - window_start).total_seconds())
    if elapsed < 1:
        return None
    return elapsed


def build_window_expression(elapsed_seconds: int, watermark_data_type: str) -> str:
    """The SQL expression the delta filters on: the anchored window start, in
    the SOURCE database's own clock and the watermark column's own type.

    Why a duration back from ``NOW()`` and not an absolute UTC literal
    ------------------------------------------------------------------
    The first version of this fix rendered an absolute ``'YYYY-MM-DD HH:MM:SS+00'``
    literal, on the reasoning that an explicit offset makes the comparison
    time-zone independent. That is true for ``timestamptz`` and FALSE — inverted
    — for ``timestamp without time zone``, the most common warehouse shape and
    Redshift's default: PostgreSQL DISCARDS the offset when casting the literal
    to a naive timestamp rather than converting it. On a source whose session
    zone is behind UTC the effective window then became SHORTER than the lookback
    by that offset, silently dropping edits that were squarely inside it —
    Bug-8700's own harm class, re-introduced by its fix. Reviewer round 3
    reproduced it live (a 30-minute-old edit excluded from a 1.5h window).

    A duration back from the source's ``NOW()`` fixes both halves at once:

    * ``timestamptz`` columns use the source instant from ``NOW()``;
    * naive timestamp columns use ``NOW() AT TIME ZONE 'UTC'``. Tessallite
      interprets naive timestamps as UTC, so using the session wall clock would
      make an ahead-of-UTC session's delta permanently empty (Bug-8746).

    This removes the source-session TIMEZONE offset. It does not remove physical
    clock skew between the metadata database, scheduler process, and source;
    the lookback cushion and fail-closed reconciliation absorb that residual.

    The anchor is NOT lost: ``elapsed_seconds`` is measured from the last
    completed run, so after a week-long outage the window reaches back a week.

    DATE-typed watermarks get one extra step. Comparing a ``date`` against an
    instant casts the date to MIDNIGHT, so a mid-day window start would exclude
    rows on the boundary day that the window is supposed to cover. Truncating the
    window start to its own date widens it to the whole boundary day — the safe
    direction, and the same correction the aggregate sibling applies.
    """
    clock = "NOW()"
    if _is_naive_timestamp_type(watermark_data_type):
        clock = "(NOW() AT TIME ZONE 'UTC')"
    interval = f"({clock} - INTERVAL '{int(elapsed_seconds)} seconds')"
    if _is_date_only_type(watermark_data_type):
        return f"({interval}::date)"
    return interval


def _is_date_only_type(data_type: str) -> bool:
    """True for a watermark column that carries a DATE with no time part."""
    normalised = (data_type or "").strip().lower()
    return normalised == "date"


def _is_naive_timestamp_type(data_type: str) -> bool:
    """True when the watermark stores a UTC wall-clock timestamp."""
    normalised = (data_type or "").strip().lower()
    return (
        normalised.startswith("timestamp")
        and "with time zone" not in normalised
        and "timestamptz" not in normalised
    )


def build_delta_sql(
    select_sql: str, quoted_incremental_column: str, window_expression: str
) -> str:
    """Rows whose watermark landed at or after the window start.

    ``window_expression`` is the output of :func:`build_window_expression` — a
    SQL expression evaluated on the SOURCE, not a literal rendered here.
    """
    return (
        f"SELECT * FROM ({select_sql}) AS pocket_src "
        f"WHERE pocket_src.{quoted_incremental_column} >= {window_expression}"
    )


def build_key_scan_sql(
    select_sql: str, quoted_keys: Sequence[str], quoted_watermark: str
) -> str:
    """The source's CURRENT identity set: key columns plus the watermark.

    Projecting only these is what makes both the deleted-row anti-join and the
    reconciliation invariant affordable next to a full re-materialisation of
    every column. The watermark rides along because
    :func:`build_unreconciled_key_probe_sql` needs to compare the source's
    watermark for a row against the CACHED copy's — the comparison that turns
    the leg's soundness argument from an enumeration into an invariant.
    """
    # The watermark can legitimately BE one of the key columns: an event or
    # measurement fact keyed ``(device_id, reading_ts)`` with
    # ``incremental_column = reading_ts`` is an ordinary model, and nothing
    # upstream forbids it (``incremental_column`` is free text — Bug-8718).
    # Projecting it twice makes the key-set ``CREATE TEMP TABLE ... AS SELECT``
    # raise ``column "..." specified more than once``, which marks the pocket
    # ``failed`` and clears its row manifest; the next sweep full-rebuilds it to
    # ``fresh`` and the run after that fails again — one failed run per two
    # sweeps, for ever. That is the same permanent flap the temporal-type gate
    # in ``usable_identity_columns`` closes for a non-temporal watermark,
    # re-entering by a different door.
    projected = list(quoted_keys)
    if quoted_watermark not in projected:
        projected.append(quoted_watermark)
    cols = ", ".join(f"pocket_src.{c}" for c in projected)
    return f"SELECT {cols} FROM ({select_sql}) AS pocket_src"


def build_key_integrity_probe_sql(table_ref: str, quoted_keys: Sequence[str]) -> str:
    """Probe proving the key really identifies at most one row of ``table_ref``.

    The declared key is trusted as an INTENT, never as a fact about the rows on
    disk: a join in the model can fan the fact grain out, a name can collide with
    a differently-meant column, and a cache already corrupted by the Bug-8699
    duplication carries the key twice. Any duplicate or NULL key makes the
    identity DELETE unsound (it would delete too much, or fail to match at all,
    since ``NULL = NULL`` is never true), so the caller falls back to a full
    rebuild — which also repairs an already-duplicated cache.

    The caller runs this against the CACHED TABLE and, separately, against the
    DELTA. The cache probe is the load-bearing one. The delta probe is a
    deliberate CONSERVATISM, and the distinction is recorded here so a later
    reader does not mistake it for the same guarantee: with a unique, non-NULL
    cache, a duplicated or NULL-keyed delta still converges (the orphan delete
    runs first, the identity delete removes at most the one cached copy, and the
    INSERT writes exactly what the source produced) and the NEXT run's cache
    probe then sees the duplicate and forces a full rebuild. Refusing up front
    simply gets there one run earlier, without ever publishing a cache whose key
    the leg cannot reason about. Removing it is not a wrong number; keeping it
    is one fewer state to reason about.
    """
    keys = ", ".join(quoted_keys)
    null_pred = " OR ".join(f"{k} IS NULL" for k in quoted_keys)
    # One grouped pass, no FROM-less SELECT and no scalar subqueries: this leg
    # serves Redshift as well as PostgreSQL, and Redshift's support for both is
    # narrower. NULL keys collapse into a single GROUP BY bucket, so summing
    # that bucket's count is an exact NULL-row count, not an approximation.
    return (
        "SELECT "
        "COALESCE(SUM(CASE WHEN g.dup_n > 1 THEN 1 ELSE 0 END), 0) "
        "AS duplicate_keys, "
        f"COALESCE(SUM(CASE WHEN {null_pred} THEN g.dup_n ELSE 0 END), 0) "
        "AS null_keys "
        f"FROM (SELECT {keys}, COUNT(*) AS dup_n FROM {table_ref} "
        f"GROUP BY {keys}) AS g"
    )


def build_key_count_sql(key_table_ref: str) -> str:
    """How many rows the source's current key set holds.

    Used for one specific catastrophe: a source mid ``TRUNCATE``-then-reload
    presents an EMPTY population, and an orphan anti-join against it would
    delete every cached row while the INSERT restores only the delta window.
    """
    return f"SELECT COUNT(*) AS source_keys FROM {key_table_ref}"


def build_unreconciled_key_probe_sql(
    table_ref: str,
    delta_table_ref: str,
    key_table_ref: str,
    quoted_keys: Sequence[str],
    quoted_watermark: str,
) -> str:
    """THE positive invariant the whole leg rests on.

    For every key the source currently produces, EITHER the cache already holds
    that key at that exact watermark, OR the delta carries it. Anything else is
    a row the patch cannot account for, so the leg refuses and the caller
    rebuilds in FULL.

    Why this replaced an enumeration
    --------------------------------
    Earlier revisions guarded a growing LIST of unsound shapes — "a key absent
    from the cache and outside the delta", then "a key duplicated in the source
    population", then "a cached row whose source watermark moved outside the
    window". Three consecutive external review rounds each found one more member
    of that list by live reproduction, which is CLAUDE.md's named escalation
    shape: a static enumeration being asked to prove a dynamic property. The
    predicate above is the property itself, so it subsumes the enumeration
    instead of extending it:

    * key not in the cache at all -> the cache cannot hold it at any watermark,
      so it must be in the delta or the patch is refused (the late-dimension /
      late-arriving-load class);
    * key cached at a DIFFERENT watermark and not in the delta -> refused (the
      backdated-correction class, which the old EXISTS-only probe could not see
      because the key WAS cached);
    * key cached at the SAME watermark -> the source has not changed that row
      since the cache was built, so leaving it alone is correct.

    Deliberately NOT covered, and it cannot be: a source that edits a row
    WITHOUT moving its own watermark. Nothing downstream can detect a change the
    change-marker did not record. That is the one true residual, and it is the
    user's contract with their source.

    Multiplicity is still a separate concern — this predicate asks EXISTS, so a
    key the source produces TWICE satisfies it. ``build_key_integrity_probe_sql``
    over the key set is what covers that; the two are complementary, not
    redundant.

    Comparison is NULL-safe by hand (``a = b OR (a IS NULL AND b IS NULL)``)
    rather than via ``IS NOT DISTINCT FROM``, which Redshift does not support.
    """
    on_delta = " AND ".join(f"d.{k} = k.{k}" for k in quoted_keys)
    on_dst = " AND ".join(f"dst.{k} = k.{k}" for k in quoted_keys)
    same_watermark = (
        f"(dst.{quoted_watermark} = k.{quoted_watermark} "
        f"OR (dst.{quoted_watermark} IS NULL AND k.{quoted_watermark} IS NULL))"
    )
    return (
        "SELECT COUNT(*) AS unreconciled_keys FROM (SELECT 1 FROM "
        f"{key_table_ref} AS k "
        f"WHERE NOT EXISTS (SELECT 1 FROM {delta_table_ref} AS d WHERE {on_delta}) "
        f"AND NOT EXISTS (SELECT 1 FROM {table_ref} AS dst "
        f"WHERE {on_dst} AND {same_watermark}) "
        "LIMIT 1) AS m"
    )


def build_orphan_delete_sql(
    table_ref: str, key_table_ref: str, quoted_keys: Sequence[str]
) -> str:
    """Remove cached rows the source no longer produces.

    Covers both a row deleted at source and a row that stopped satisfying the
    pocket's WHERE predicate. Neither ever appears in a watermark delta, so
    without this the cache over-reports them forever.
    """
    # The DELETE target is NOT aliased: Redshift's DELETE grammar has no alias
    # slot, and the fully-qualified reference reads the same on both dialects.
    on = " AND ".join(f"k.{k} = {table_ref}.{k}" for k in quoted_keys)
    return (
        f"DELETE FROM {table_ref} "
        f"WHERE NOT EXISTS (SELECT 1 FROM {key_table_ref} AS k WHERE {on})"
    )


def build_identity_delete_sql(
    table_ref: str, delta_table_ref: str, quoted_keys: Sequence[str]
) -> str:
    """Remove the superseded copy of every row the delta re-derives.

    Matched on row IDENTITY, never on the watermark value — matching on the
    watermark is Bug-8699 itself.
    """
    # Target not aliased — see ``build_orphan_delete_sql`` (Redshift).
    on = " AND ".join(f"{table_ref}.{k} = src.{k}" for k in quoted_keys)
    return f"DELETE FROM {table_ref} USING {delta_table_ref} AS src WHERE {on}"


def build_insert_from_delta_sql(
    table_ref: str, delta_table_ref: str, quoted_columns: Sequence[str]
) -> str:
    """Write the delta back, matched BY NAME against the cached table.

    ``INSERT INTO t SELECT * FROM delta`` is positional. The delta is derived
    from the same SELECT that built the cache, so the orders normally agree —
    but "normally" is not a guarantee worth a silent column-shift, which would
    write every value into the wrong column and be invisible to every gate. An
    explicit column list taken from the target catalogue's ordinal order makes a
    divergence raise at the database instead: the run fails, the pocket is
    marked ``failed``, and the next sweep rebuilds it in FULL.
    """
    cols = ", ".join(quoted_columns)
    return f"INSERT INTO {table_ref} ({cols}) SELECT {cols} FROM {delta_table_ref}"
