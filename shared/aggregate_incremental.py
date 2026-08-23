"""Soundness rules for the AGGREGATE incremental (windowed DELETE/INSERT) leg.

Bug-8717, Bug-8810, Bug-8812. This module owns *when* an aggregate may be
patched instead of rebuilt and *which window* the patch must cover;
``services/scheduler/src/jobs/incremental_refresh.py`` only executes what this
module decides. It is the aggregate-side counterpart of
``shared/pocket/incremental.py``, deliberately separate because the two families
have different row semantics and the pocket's fix does NOT transfer.

Why the pocket's fix does not transfer
--------------------------------------
Bug-8699 was the same harm on the POCKET leg: an edited row's cached copy was
never removed, so the cache served it twice. That was fixable because a pocket
row IS a source row — give the DELETE the model fact table's primary key and it
finds the superseded copy exactly. An AGGREGATE row is a BUCKET: a
``GROUP BY`` output over an unknown, unrecorded set of source rows. There is no
key that maps a bucket back to the rows that produced it, so there is nothing
for an identity-keyed DELETE to match.

The structural problem
----------------------
Write ``A = GROUP BY G over S`` for the materialised aggregate, ``P`` for the
partition column (the leg already requires ``P`` to be one of ``G``), and ``W``
for the window start. The leg emits::

    DELETE FROM A WHERE P >= W;
    INSERT INTO A SELECT G, agg(M) FROM S WHERE P >= W GROUP BY G;

Every bucket at or above ``W`` is therefore re-derived from current source
truth and is correct by construction. The patch is correct overall **iff every
bucket below ``W`` still has the contents it had when it was last built**.

That predicate is not decidable at runtime, and the reason is not an
implementation gap — it is missing information:

* ``S_prev`` is not retained anywhere. The source keeps no history; the
  aggregate keeps buckets, not row identities.
* A row that moved from ``t_old < W`` to ``t_new >= W`` presents to the source
  exactly like a row that was always at ``t_new``. This holds even if the model
  carried a separate change-marker column: the marker tells you the row changed
  and where it is NOW, never where it WAS. Its old bucket keeps counting it,
  its new bucket counts it again — Bug-8717, live-reproduced at 250 against a
  source truth of 150.
* A row deleted below ``W``, and a row inserted below ``W``, are invisible to
  any watermark predicate for the same reason.
* The only complete detector is a per-bucket comparison of the aggregate's
  stored counts/measures against a fresh ``GROUP BY`` over the whole source —
  which is the full rebuild's own scan, so it destroys the only reason the leg
  exists. Cheaper partial detectors (a below-window row-count total, a distinct
  bucket anti-join) miss net-cancelling changes and intra-below-window moves;
  extending such an enumeration one shape per review round is the exact pattern
  CLAUDE.md names as an escalation shape, and the pocket sibling already spent
  five rounds proving it does not converge.

State the impossibility precisely, because the absolute form is not quite true:
no detector reading only the CURRENT source-table state and the CURRENT
aggregate state, at less than full-rebuild cost, can decide it. Row-level history
does exist in principle — change data capture, logical decoding, an audit table —
it is simply not something Tessallite ingests today. If it ever did, this whole
argument would need revisiting, and the sound design would be the one logged in
``docs/execution/execution_future-features.md``: derive the aggregate from a
POCKET, where row identity exists at the layer the aggregate is built from.

So the answer here is an EXCLUSION, not a detector. The range DELETE/INSERT is
sound only under a declared contract, and must refuse to run without one.

The contract
------------
    No row already written to the source is ever modified in ANY way — its
    measure values, its grain values and its partition value are all immutable
    once written — and no row is ever deleted. New rows always arrive with a
    partition value no earlier than ``incremental_lookback`` days before the run,
    and never with a NULL partition value unless the leg is NULL-inclusive (see
    :func:`build_watermark_predicate`).

The first clause has to be TOTAL immutability, and the reason is worth pinning
because the first version of this contract got it wrong. That version said rows
are "never updated in a way that moves ``P``", which explicitly permits an
in-place restatement that leaves the partition value alone. It is unsound: a row
below the window whose MEASURE is corrected — an order restated from 100 to 300 —
never enters the delta (its ``P`` did not move) and its bucket is never
re-derived, so the aggregate keeps serving the old figure for ever. Reviewer
round 1 traced exactly that: aggregate 100 against a source truth of 300. A
below-window edit to a GRAIN value (a customer re-pointed from one region to
another, date untouched) misattributes the same way. Only "nothing already
written ever changes" is sufficient.

The user declares this contract explicitly through
``AggregateRefreshPolicy.incremental_append_only``. Missing and legacy fields
default to false, so undeclared policies degrade to a FULL rebuild. The same
posture applies to an unsupported connector, cross-database materialisation,
non-re-aggregatable statistics, variant measures, a legacy manifest, or a
changed grain shape: the worst case is a slower run, never a wrong number.

The preconditions that ARE provable
-----------------------------------
The declaration above covers what the SOURCE does. These close the places where
the emitted patch could fail to cover its own window even under a perfect
append-only source, and they are enforced unconditionally — so they already hold
on the day the declaration turns the leg on:

* :func:`resolve_delete_grain` (Bug-8810) — the DELETE and the INSERT must
  address the same model column.
* :func:`resolve_window_anchor` (Bug-8812) — the window must reach back to the
  previous run's START, not its completion.
* :func:`is_temporal_watermark_type` (Bug-8819) — a non-temporal partition
  column makes the comparison lexicographic, not chronological.
* :func:`build_watermark_predicate` (Bug-8820) — the predicate must be
  NULL-inclusive, and identical on both sides.
* :func:`is_source_clock_safe_watermark_type` and
  :func:`build_window_expression` (Bug-8743/Bug-8760/Bug-8828/Bug-8831) — a
  naive timestamp has no durable source-clock convention, so it is refused;
  an accepted window is one typed, timezone-explicit constant reused by both
  statements.

This list is an enumeration, and CLAUDE.md is right that enumerations are the
wrong shape for proving a dynamic property — which is exactly why the leg does
NOT rest on it. The soundness argument rests on the declared contract; these are
implementation preconditions on the SQL the leg emits, each one closing a defect
that was live-reproduced against PostgreSQL. Every one of them fails closed to a
FULL rebuild.

CLOSED IN L19 (Aggregate Incremental Wrong-Numbers)
--------------------------------------------------
Bug-8831 (HIGH) and Bug-8828 (HIGH) are now closed. The window is rendered as a
timezone-explicit SQL CONSTANT (``TIMESTAMPTZ 'YYYY-MM-DD HH:MM:SS+00:00'``)
rather than a
``NOW() - INTERVAL`` expression. Because the expression is a constant literal,
not a function call, it evaluates identically in both DELETE and INSERT
(Bug-8831). Because it is explicitly typed with a ``+00:00`` offset for
``timestamptz``, the session ``TimeZone`` does not affect its meaning
(Bug-8828, Bug-8760). DATE and naive timestamp watermarks fail closed without
source calendar/clock metadata. Removing ``NOW()`` also happens to close
Bug-8829 (the T-SQL transpilation failure) as a side effect.

INSERT column order (source Bug-8832, tracked on main as Bug-8784 /
Bug-8832 ``[FIXED-PENDING-VERIFICATION]``) is NO LONGER open: the fix is
present at ``services/scheduler/src/jobs/incremental_refresh.py:1044-1065``,
which binds the INSERT by the aliases emitted by the shared SELECT builder so
a heap reorder cannot write one measure into another's column. This sentence
previously read "remains OPEN and is tracked separately" — the opposite
direction of the dead-schema comments corrected in models.py, but the same
defect class: a source comment asserting an issue state that the code
contradicts. Verify status in the registry, not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Iterable, Optional

__all__ = [
    "APPEND_ONLY_POLICY_ATTR",
    "DEFAULT_LOOKBACK_DAYS",
    "WindowAnchor",
    "policy_declares_append_only",
    "periodic_full_rebuild_due",
    "build_watermark_predicate",
    "build_window_expression",
    "is_temporal_watermark_type",
    "is_source_clock_safe_watermark_type",
    "resolve_delete_grain",
    "resolve_window_anchor",
    "resolve_window_start",
    "window_elapsed_seconds",
]

# The persisted ``AggregateRefreshPolicy`` attribute declaring that its source
# partition is append-only. ``getattr(..., False)`` keeps pre-migration objects
# and older imported bundles fail-closed.
APPEND_ONLY_POLICY_ATTR = "incremental_append_only"

# ``AggregateRefreshPolicy.incremental_lookback`` is nullable; this is the
# cushion applied when the policy declares none. Matches the job's historical
# default and the UI's initial value.
DEFAULT_LOOKBACK_DAYS = 1


@dataclass(frozen=True)
class WindowAnchor:
    """Where the delta window is anchored, and which field supplied it."""

    anchor: datetime
    field: str


def policy_declares_append_only(policy: Any) -> bool:
    """True when the refresh policy DECLARES its partition column append-only.

    See the module docstring for why this declaration is the only sound basis
    for a range DELETE/INSERT. ``None`` (no policy row at all) is refused for
    the same reason a missing or legacy declaration is.
    """
    if policy is None:
        return False
    # Refresh-policy rows can come from older manifests, direct ORM writes, or
    # untyped import payloads. Only the exact persisted contract is eligible:
    # incremental mode, a real column name, and the canonical boolean True.
    if getattr(policy, "refresh_mode", None) != "incremental":
        return False
    column = getattr(policy, "incremental_column", None)
    if not isinstance(column, str) or not column.strip():
        return False
    value = getattr(policy, APPEND_ONLY_POLICY_ATTR, False)
    return type(value) is bool and value is True


def periodic_full_rebuild_due(
    policy: Any,
    last_full_refresh_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> bool:
    """Return whether an append-only policy must be corrected by a full build.

    ``None`` means the user did not request periodic correction. Once a cadence
    is configured, absence of a prior completed full build fails safe by making
    the first run full. Naive persisted timestamps are interpreted as UTC, like
    the existing incremental-window anchors.
    """
    raw_interval = getattr(policy, "full_rebuild_interval_days", None)
    if raw_interval is None:
        return False
    try:
        interval_days = int(raw_interval)
    except (TypeError, ValueError):
        return True
    if interval_days < 1 or last_full_refresh_at is None:
        return True

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    completed = last_full_refresh_at
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    return completed <= reference - timedelta(days=interval_days)


def is_temporal_watermark_type(data_type: Any) -> bool:
    """True for a partition column type the watermark comparison can order.

    Bug-8819. Both emitted predicates compare the partition column against a
    ``'YYYY-MM-DD HH:MM:SS'`` literal. Against a TEXT column that comparison is
    LEXICOGRAPHIC, not chronological, and the failure is silent rather than
    loud: a column holding ``'YYYY-MM-DD'`` date strings sorts every value for
    the watermark's own calendar day BEFORE the literal, because the shorter
    string is a strict prefix. Those rows are excluded from the INSERT — and
    from the DELETE too, so the two sides stay consistent and nothing errors.
    The rows are simply never materialised, and the next run's watermark has
    moved past them, so the loss is permanent and recurs every time the boundary
    date advances. Reviewer round 1 traced it: aggregate 100.0/1 row against a
    source truth of 150.0/2 rows, from a single contract-compliant append.

    A genuine ``date`` column is orderable, but it still carries a source-local
    calendar convention. ``is_source_clock_safe_watermark_type`` applies the
    separate source-clock gate and refuses it without persisted calendar data.

    This is the aggregate half of a rule the POCKET leg already enforces
    (``shared/pocket/incremental.py``, ``_is_temporal_type`` in
    ``usable_identity_columns``). It was missed here because the two legs were
    hardened in separate lanes; per CLAUDE.md's shared-primitive discipline the
    rule now exists on both callers of the watermark pattern. Consolidating the
    two into one predicate is tracked separately rather than done here, because
    the pocket module is a different lane's freshly landed code.

    An INTEGER date key (``20260201``) is refused as well. It fails LOUDLY today
    rather than silently — PostgreSQL rejects the timestamp literal against an
    integer column — but a failed run marks the aggregate invalid and the next
    sweep full-rebuilds it, which is the permanent one-failure-per-two-sweeps
    flap the pocket lane closed with this same gate. Refusing up front turns it
    into a plain, quiet full rebuild.
    """
    normalised = _normalise_watermark_type(data_type)
    return normalised.startswith(("date", "timestamp"))


def is_source_clock_safe_watermark_type(data_type: Any) -> bool:
    """Return whether a watermark can be compared without a clock convention.

    ``timestamp without time zone`` values are source-local wall-clock values,
    and ``date`` values carry a source-local calendar whose midnight may differ
    from scheduler UTC. Without persisted source calendar/timezone metadata,
    aggregate incremental must fail closed for both. Timezone-aware timestamps
    carry their source instant explicitly.
    """
    return _watermark_type_class(data_type) == "timestamptz"


def _normalise_watermark_type(data_type: Any) -> str:
    """Normalize metadata whitespace/case before temporal classification."""
    return " ".join(str(data_type or "").strip().lower().split())


def _watermark_type_class(data_type: Any) -> str | None:
    """Return the one temporal class the renderer can safely emit.

    Keep this classifier shared by admission and rendering. Connector-native
    spellings not listed here intentionally fail closed to full refresh; in
    particular, Snowflake's ``timestamp_tz`` remains a correctness-safe
    fallback until its metadata normalization is proven.
    """
    normalised = _normalise_watermark_type(data_type)
    if normalised in {
        "timestamptz",
        "timestampz",
        "timestamp with time zone",
    }:
        return "timestamptz"
    if normalised.startswith("timestamp(") and normalised.endswith(") with time zone"):
        precision = normalised[len("timestamp(") : -len(") with time zone")]
        if precision.isdigit():
            try:
                precision_value = int(precision)
            except ValueError:
                return None
            if 0 <= precision_value <= 6:
                return "timestamptz"
    return None


def window_elapsed_seconds(
    window_start: datetime, now: Optional[datetime] = None
) -> Optional[int]:
    """A clock-skew guard: refuse if the window start is not in the past.

    Bug-8812. The window is anchored to the previous run's ``started_at``.
    Rounded UP, so the window can only ever be wider than computed, never
    narrower. Since Bug-8831 the elapsed seconds are NOT threaded into
    ``build_window_expression`` — the window is rendered directly from the
    watermark datetime. This function remains as a pure guard.

    Returns ``None`` — meaning "rebuild in FULL" — when the anchor is not in the
    past. That means the clock which stamped ``AggregateRefreshRun.started_at``
    and the clock reading it disagree by more than the entire lookback. Clamping
    to a one-second window would be the worst answer available: it produces an
    almost-empty patch that leaves every recent row unmaterialised and SILENT.
    Every other impossible state in this module fails closed, and so does this.
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


def build_window_expression(watermark: datetime, watermark_data_type: str) -> str:
    """The window start, rendered as a timezone-explicit SQL constant.

    Bug-8831 + Bug-8828 + Bug-8760 (the three-way fix). This used to emit a
    ``NOW() - INTERVAL '<n>' seconds`` expression, which was:

    * Zone-safe for ``timestamptz`` (durations are offset-invariant) — Bug-8760.
    * Evaluated TWICE (once per statement) because ``execute_source_ddl`` splits
      ``DELETE; INSERT`` and runs each in its own autocommitted call, and
      PostgreSQL's ``NOW()`` is ``transaction_timestamp()``. The DELETE and INSERT
      windows therefore differ by the execution gap and rows between them are
      permanently lost — Bug-8831, live-traced at 877/2 against source 927/3.
    * Cast through the SESSION ``TimeZone`` for ``::date``. Ahead of UTC the
      derived date can be a day LATER, narrowing the window by a full day —
      Bug-8828.
    * Untranspilable to T-SQL (``GETDATE() - INTERVAL '86400' SECONDS``) —
      Bug-8829 (MEDIUM), solved as a side effect of removing ``NOW()``.

    Now the window is a typed SQL literal computed from the UTC ``watermark``
    (which is the anchor pushed back by ``lookback_days`` — see
    :func:`resolve_window_start`). Because the expression is a CONSTANT, not a
    function call, both DELETE and INSERT evaluate the SAME value and Bug-8831
    is closed. Because the literal is explicitly typed (``TIMESTAMPTZ`` carrying
    ``+00:00``), PostgreSQL does not resolve it against the session ``TimeZone``
    and both Bug-8828 and Bug-8760 are closed. Naive timestamps and DATE values
    are rejected before rendering: there is no durable source-clock/calendar
    convention in the persisted model, so emitting a scheduler-UTC literal
    would be unsafe.
    """
    if not is_source_clock_safe_watermark_type(watermark_data_type):
        raise ValueError(
            "incremental watermark requires a timezone-aware timestamp; "
            "naive source-local timestamps and DATE values have no durable "
            "source-clock/calendar convention"
        )
    if watermark.tzinfo is None:
        watermark = watermark.replace(tzinfo=timezone.utc)
    utc_naive = watermark.astimezone(timezone.utc).replace(tzinfo=None)

    # ``timestamptz`` / ``timestamp with time zone``: explicit UTC offset so the
    # value is correct regardless of the source session's TimeZone.
    if _is_timestamptz_type(watermark_data_type):
        return (
            f"TIMESTAMPTZ '{utc_naive.strftime('%Y-%m-%d %H:%M:%S')}+00:00'"
        )

    raise ValueError(
        "incremental watermark type was admitted without a renderable "
        "timezone-aware timestamp class"
    )


def _is_timestamptz_type(data_type: Any) -> bool:
    """True for a watermark column whose type carries a time zone.

    ``timestamptz``, ``timestamp with time zone``, and similar variants that
    PostgreSQL normalises to ``timestamp with time zone``. For these columns an
    explicit UTC offset literal is unambiguous regardless of the source
    session's ``TimeZone`` setting.

    ``timestamp without time zone`` must NOT match — the ``"without"`` variant
    is a naive type that stores no offset and the ``+00:00`` suffix would be
    misleading when compared against it.
    """
    return _watermark_type_class(data_type) == "timestamptz"


def build_watermark_predicate(column_ref: str, window_expression: str) -> str:
    """The window predicate, in the ONE form both sides of the patch must use.

    Bug-8820. ``<col> >= <watermark>`` is never true for a NULL partition value,
    on either side. So the DELETE never removes the aggregate's NULL bucket and
    the INSERT never re-derives it — while a FULL rebuild does build one, because
    ``GROUP BY`` emits a NULL group. An appended row with a NULL partition value
    is therefore invisible to every incremental run, permanently, and no clause
    of the contract is broken: the source is append-only, nothing was edited or
    deleted, and the aggregate still under-reports. Reviewer round 1 traced it at
    100.0/1 row against a source truth of 150.0/2 rows.

    Making the predicate NULL-inclusive fixes it symmetrically and cheaply: the
    DELETE drops the NULL bucket and the INSERT rebuilds it from the current NULL
    population, so that bucket is simply re-derived in full on every run. The
    alternative — refusing whenever ``ModelColumn.is_nullable`` — fails closed but
    that flag defaults True on reflected columns, so it would disable the leg
    almost everywhere for a case this handles exactly.

    The cost is worth stating: the INSERT's NULL arm re-scans every
    NULL-partition source row on EVERY run, because that bucket is rebuilt from
    scratch each time rather than patched. On a source whose partition column is
    largely NULL the "incremental" run degenerates towards a full scan. That is a
    performance characteristic, not a wrong number, and it is bounded by the NULL
    population rather than by table size — but a caller wondering why a patch is
    slow should look here first.

    Both callers build their predicate here so the DELETE range and the INSERT
    range cannot drift apart in a future edit; that symmetry is the same property
    :func:`resolve_delete_grain` protects for the column identity.

    Since Bug-8831 the window expression is a timezone-explicit SQL CONSTANT
    rendered from a UTC datetime, so the DELETE and INSERT evaluate the SAME
    value: identical text AND identical value. The two ranges cover the same
    rows. See :func:`build_window_expression`.

    ``window_expression`` is the output of :func:`build_window_expression` — a
    typed SQL literal (``TIMESTAMPTZ '...'``).
    """
    return f"({column_ref} >= {window_expression} OR {column_ref} IS NULL)"


def resolve_window_anchor(last_run: Any) -> Optional[WindowAnchor]:
    """The instant the previous completed run's source read is guaranteed to
    post-date, or ``None`` when the run carries no usable timestamp.

    Bug-8812. The previous run read the source at some instant between its
    ``started_at`` and its ``completed_at``. Anchoring the next window to
    ``completed_at`` — what this leg did, contradicting its own module docstring
    — assumes the lookback cushion exceeds that run's duration; when it does
    not, a row committed after the previous run's read but before its completion
    was never materialised by that run and starts BELOW the next run's window,
    where no future incremental run will ever look at it again.

    ``started_at`` removes the assumption. The cost is re-processing whatever
    changed while the previous run was executing, which is free here: the patch
    re-derives whole buckets from current source truth, so covering a bucket
    twice produces the same rows.

    There is deliberately NO ``completed_at`` fallback. An earlier revision fell
    back to it and merely logged, which made this the one branch in the module
    that degraded to "patch anyway on the narrower window and hope the cushion
    covers it" — the exact assumption Bug-8812 exists to remove, inside a module
    whose entire thesis is that an unprovable precondition rebuilds in full.
    ``AggregateRefreshRun.started_at`` is a non-nullable ``TIMESTAMPTZ`` with a
    server default, so a completed run without one is not a state the schema can
    produce; that makes the fallback unreachable, which is a reason to delete it
    rather than a reason to keep it. Returning ``None`` sends the caller to a
    FULL rebuild, like every other refusal here.
    """
    if last_run is None:
        return None
    value = getattr(last_run, "started_at", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return WindowAnchor(anchor=value, field="started_at")
    return None


def resolve_window_start(
    anchor: datetime, lookback_days: Optional[int]
) -> datetime:
    """Window start: the anchor pushed back by the late-arrival cushion.

    A non-positive or missing lookback is coerced to
    :data:`DEFAULT_LOOKBACK_DAYS`. Zero is reachable from the UI (the lookback
    field's minimum is 0) and would leave no cushion at all for the
    late-arriving rows the control exists to catch, so it is treated as "not
    configured" rather than honoured literally.
    """
    days = DEFAULT_LOOKBACK_DAYS
    try:
        candidate = int(lookback_days) if lookback_days is not None else 0
    except (TypeError, ValueError):
        candidate = 0
    if candidate >= 1:
        days = candidate
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return anchor - timedelta(days=days)


def resolve_delete_grain(
    *,
    grain_cols: Iterable[Any],
    watermark_table_id: Any,
    watermark_column_name: str,
) -> Any | None:
    """The aggregate grain the DELETE may range over, or ``None`` to rebuild.

    Bug-8810. The leg's whole correctness argument is that the DELETE range and
    the INSERT range cover the same source rows, and that only holds if both
    sides address THE SAME model column. They were resolved by two independent
    rules that could disagree:

    * the INSERT predicate column came from the job's watermark resolver, which
      picks the column of that name on the ANCHOR (fact) table and falls back to
      canonical table order (the Bug-8605 fix);
    * the DELETE column came from the FIRST grain whose ``source_column_name``
      matched — ignoring ``source_table_id`` — and, failing that, the first
      grain whose LOGICAL NAME matched.

    Two divergences follow. When the same column name exists on the fact and on
    a dimension or calendar table and the aggregate grains the DIMENSION's copy,
    the DELETE is evaluated over one table's values and the INSERT over
    another's. Worse, the logical-name fallback can match an expression-backed
    grain — a user-defined attribute such as ``DATE_TRUNC('month', order_date)``
    — whose physical column holds BUCKET STARTS. Comparing bucket starts to a
    mid-window watermark leaves the boundary period's bucket in place while the
    INSERT re-derives every row in it, double-counting that period on EVERY run
    with no moved row required.

    So the grain is accepted only when it is a plain column grain
    (``source_expression`` unset, which ``resolve_aggregate_layout`` makes
    mutually exclusive with a source column) whose ``(source_table_id,
    source_column_name)`` is exactly the resolved watermark column's. The
    logical-name fallback is not reinstated under any condition: a grain whose
    logical name matches and whose source column also matches is already found
    by the identity rule, and one whose source column does NOT match is the
    unsound case above.

    Completing the equivalence argument
    -----------------------------------
    Matching the identity is necessary but the two sides also have to be
    QUALIFIED the same way, and they are rendered by different helpers: the
    INSERT predicate by the job's ``_resolve_incremental_column_ref`` and the
    aggregate column's own value by ``_grain_source_ref``. Given this rule they
    agree whenever the FROM builder produced an alias for the watermark's table,
    which it does for every table the layout needs. The one branch where they
    differ in TEXT is the alias-missing fallback: ``_grain_source_ref`` emits a
    bare ``"col"`` while the predicate emits ``base."col"``. That branch cannot
    produce a wrong number, and the reason is worth recording so it is not
    re-derived every review: a bare reference either resolves unambiguously — in
    which case it is the same single column the identity rule already pinned —
    or the name occurs on two tables of the FROM and PostgreSQL raises "column
    reference is ambiguous". Likewise ``base."col"`` raises if the anchor does
    not carry the column. Both outcomes fail the run loudly, which marks the
    aggregate invalid and sends the next sweep to a full rebuild. So the residual
    is a failed run, never a silent divergence between the two ranges.
    """
    if watermark_table_id is None or not watermark_column_name:
        return None
    wanted_table = str(watermark_table_id)
    for grain in grain_cols or ():
        if getattr(grain, "source_expression", None) is not None:
            continue
        if getattr(grain, "source_column_name", None) != watermark_column_name:
            continue
        grain_table = getattr(grain, "source_table_id", None)
        if grain_table is None or str(grain_table) != wanted_table:
            continue
        if not getattr(grain, "physical_col_name", None):
            continue
        return grain
    return None
