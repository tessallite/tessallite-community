"""Serve-time overdue staleness gate (Bug-5148 / Bug-8338 / F-102-01).

A pocket or aggregate carries a status label (``fresh`` / ``active``) that is
written ONLY by the refresh sweep and by drift detection. If the scheduler is
scaled-to-zero, crashed, or simply behind, an artifact stays labelled fresh
while the source moves on — and the matchers, which gate on the label alone,
serve the stale artifact as if it were current. That is a silent
wrong-numbers defect on the primary serving path.

This module is the SINGLE source of truth for the serve-time overdue check,
shared by BOTH the aggregate matcher and the pocket matcher so neither drifts
from the other. It mirrors the scheduler's own "is this artifact due for a
refresh?" logic (``scheduler.sweep._is_due`` /
``scheduler.pocket_refresh._is_due``): the previous scheduled cron fire is
computed with ``croniter``, and the artifact is DUE when its last refresh
predates that fire. The serve gate adds a configurable GRACE window on top of
the due point, because the scheduler runs on a cadence (hourly): an artifact
that just crossed its cron fire is legitimately awaiting the next sweep and
must still serve. The grace deadline is anchored at the FIRST scheduled fire
that the last refresh missed (``get_next`` from ``last_refresh``), NOT at the
fire just before ``now``: anchoring at the latter would make the gate inert for
any cron period at or below the grace window and oscillate for longer periods.
Anchoring at the first missed fire is MONOTONIC — once a scheduled refresh is
missed past the grace window the artifact stays overdue no matter how many
further fires elapse. Only once ``now`` is past ``first_missed_fire + grace`` —
i.e. a scheduled refresh has been MISSED, not merely pending — does the
artifact count as overdue and get refused to source.

Fail-safe contract (documented, deliberate):

* No cron / empty cron  -> return False. A manual-policy or event-policy
  artifact has NO scheduled cadence, so "overdue by schedule" is undefined; it
  cannot be proven stale here and must keep its existing (status-gated)
  behaviour. Event pockets are re-materialised by the drift path (F-005-21);
  manual artifacts have no cadence by design.
* No last_refresh_at    -> return False. An unmaterialised artifact never
  reaches the matcher as a servable candidate (status gates it out first); if
  one somehow does, we cannot compute a miss against a schedule with no
  baseline, so we defer to the status gate rather than fabricate an overdue
  verdict.
* Unparseable cron       -> return False (do NOT refuse a servable artifact on
  a config error) but log so it surfaces. This matches the scheduler, which
  skips (does not crash) an invalid cron. A False here never ACCEPTS an
  under-fresh artifact — it only declines to add a NEW refusal — so the outcome
  is the pre-gate status behaviour, never a silent wrong number introduced by
  this module.

A True return means "definitely overdue past the grace window" — the matcher
must treat the artifact as a miss so the router falls back to source, which is
always correct.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from croniter import croniter

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC (matches the scheduler's own handling)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# Config key template for the serve-time grace window. One template, so the
# aggregate gate, the pocket gate and the freshness producer cannot read
# different keys.
OVERDUE_GRACE_CONFIG_KEY = "{kind}.serve_overdue_grace_hours"

# Artifact kinds that own a serve-time overdue gate. A typo'd kind would
# resolve a key that does not exist in the config registry, which silently
# DISABLES the gate — so the kind is validated rather than interpolated blind.
OVERDUE_GRACE_KINDS: frozenset[str] = frozenset(
    {"aggregate", "pocket", "named_query"}
)


def overdue_grace_config_key(kind: str) -> str:
    """Registry key holding the serve-time grace window for *kind*."""
    if kind not in OVERDUE_GRACE_KINDS:
        raise ValueError(
            f"Unknown artifact kind {kind!r} for the serve-time overdue gate; "
            f"expected one of {sorted(OVERDUE_GRACE_KINDS)}"
        )
    return OVERDUE_GRACE_CONFIG_KEY.format(kind=kind)


def resolve_overdue_grace_seconds(kind: str) -> float | None:
    """Resolve the serve-time grace window for *kind*, in SECONDS.

    Bug-8528 [shared-primitive drift]. This module exists so the aggregate and
    pocket serve-time gates cannot drift from each other, but it owned only the
    overdue PREDICATE — the grace-window RESOLUTION was independently inlined at
    three sites (``routing/aggregate_matcher``, ``routing/pocket_matcher``, and
    Bug-8365's ``_result_freshness`` in the query-router's ``api/routes``). All
    three were at parity, but nothing structural held them there: a key or unit
    edited at one site would have labelled a served result with a verdict
    computed from a threshold DIFFERENT to the gate that admitted the artifact,
    so the analyst-facing "as of / stale" chip would contradict the gate — a
    trust defect on the primary serving path. This is the single resolver all
    three now call.

    Contract (unchanged from the three inline copies, preserved exactly):
      * the value is stored in HOURS and returned in SECONDS;
      * ``system_snapshot_get`` is a sync snapshot read that falls back to the
        config registry default, so the gate stays live with a sane default even
        before the snapshot loads or during a config gap — it never silently
        goes inert and lets an overdue artifact serve stale numbers;
      * a NUMERIC value (including ``0``, the strictest setting) ENABLES the
        gate. Only an uncoercible value returns ``None`` (gate disabled), so a
        deliberate 0 can never be mistaken for "off".
    """
    from shared.config.bootstrap import system_snapshot_get

    # Resolve the key OUTSIDE the try: the except clause below catches
    # ValueError, so an unknown kind raised inside it would be swallowed into a
    # None — i.e. a typo'd kind would silently DISABLE the gate, the exact
    # failure mode the kind validation exists to prevent.
    config_key = overdue_grace_config_key(kind)
    try:
        return float(system_snapshot_get(config_key)) * 3600.0
    except (TypeError, ValueError):
        return None


def artifact_overdue(
    cron_expression: str | None,
    last_refresh_at: datetime | None,
    now: datetime,
    grace_seconds: float,
) -> bool:
    """Return True when a scheduled artifact has MISSED a refresh past the grace
    window and must therefore NOT serve (fall back to source).

    ``cron_expression`` — the artifact's refresh cron (the SAME schedule the
    scheduler uses to refresh it). ``None``/empty => not schedule-driven =>
    never overdue (see module docstring fail-safe contract).

    ``last_refresh_at`` — when the artifact was last materialised. ``None`` =>
    cannot compute a miss => not overdue here (status gate owns that case).

    ``now`` — current time (UTC-aware or naive; naive treated as UTC).

    ``grace_seconds`` — how long AFTER the scheduled fire an artifact may still
    serve before it is considered a missed refresh. Sourced from config by the
    caller; never hardcoded. A negative value is clamped to 0.
    """
    if not cron_expression:
        return False
    if last_refresh_at is None:
        return False

    now = _as_utc(now)
    last = _as_utc(last_refresh_at)
    grace = timedelta(seconds=max(0.0, grace_seconds))

    try:
        # DUE anchor: the most recent scheduled fire before now. Mirrors the
        # scheduler's own due predicate (``last < prev_fire`` == "a refresh was
        # scheduled and has not run"), so this gate refuses only artifacts the
        # scheduler itself would consider due.
        prev_fire = _as_utc(croniter(cron_expression, now).get_prev(datetime))
        # OVERDUE anchor: the FIRST scheduled fire strictly AFTER the last
        # refresh — i.e. the first refresh that was expected and (per the stale
        # label) missed. The deadline is anchored HERE, not at prev_fire.
        #
        # Anchoring at prev_fire (the fire just before now) was a defect: for any
        # cron period <= grace, ``now - prev_fire`` is always less than one
        # period <= grace, so ``now > prev_fire + grace`` could NEVER fire and
        # the gate was permanently inert for hourly/2h/4h/6h schedules; for
        # longer periods it oscillated back to "serve" for the first ``grace``
        # window after every subsequent fire. Anchoring at the FIRST missed fire
        # is MONOTONIC: once a scheduled refresh is missed past the grace window
        # the artifact stays overdue no matter how many further fires elapse.
        first_missed = _as_utc(croniter(cron_expression, last).get_next(datetime))
    except Exception as exc:  # invalid cron — mirror scheduler: skip, don't crash
        logger.warning(
            "serve-time overdue gate: invalid cron %r — treating as not overdue: %s",
            cron_expression, exc,
        )
        return False

    # DUE: a scheduled fire has passed since the last refresh. OVERDUE:
    # additionally, now is past the grace window after the FIRST such missed fire,
    # so the refresh was genuinely missed rather than merely pending the next
    # cadence sweep.
    is_due = last < prev_fire
    if not is_due:
        return False
    return now > (first_missed + grace)
