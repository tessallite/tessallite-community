"""KPI Latest materialisation — $KPIs virtual table upsert logic.

Extracted from kpis.py (Bug-7219) to reduce the monolith's responsibility
count. All public symbols are re-exported from kpis.py so existing imports
continue to work.
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select

from shared.db.kpi_latest_ordering import (
    kpi_latest_write_guard,
    kpi_latest_write_is_noop,
    strip_ungenerated_token,
)
from shared.db.kpi_latest_write import (
    KpiPublishOutcome,
    bound_kpi_latest_strings,
    truncate_for_column,
)
from shared.db.models import KPILatest

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Column-width truncation
# ---------------------------------------------------------------------------
# R7 finding 5 (root cause): only ``status_label`` used to be bounded, so a long
# ``formatted_value`` (String(128)) raised StringDataRightTruncationError inside
# the per-row upsert — the concrete, reachable trigger for the swallowed-failure
# chain below. Every length-constrained column is now bounded by ONE shared
# helper whose widths are derived from the ORM columns (shared/db/kpi_latest_write.py),
# so the model-service and the scheduler sweep cannot bound different sets.
#
# ``_STATUS_LABEL_MAX_LEN`` / ``_truncate_status_label`` are kept as thin
# delegating aliases: several existing guards import them by name.
_STATUS_LABEL_MAX_LEN = KPILatest.__table__.c.status_label.type.length


def _truncate_status_label(label: str | None) -> str | None:
    """Bound a status label to the persistence column width."""
    return truncate_for_column(label, KPILatest, "status_label")


# ---------------------------------------------------------------------------
# KPI Latest value tuple (change detection)
# ---------------------------------------------------------------------------

def _kpi_latest_value_tuple(
    *,
    kpi_name: str,
    value,
    target,
    status,
    status_label,
    trend_pct,
    formatted_value,
) -> tuple:
    """Normalise the value-bearing kpi_latest columns into a comparable tuple.

    Numeric columns come back from the DB as ``Decimal`` but are written as
    ``float``; normalising both sides to ``float`` lets an unchanged render be
    recognised as a no-op. ``evaluated_at`` is deliberately excluded -- it is a
    freshness stamp, not a published value, so refreshing it on every render is
    exactly the write amplification F-017-29 removes. The materialised value
    stays correct: an unchanged write would have stored the same numbers.
    """
    def _num(v):
        return None if v is None else float(v)

    return (
        kpi_name,
        _num(value),
        _num(target),
        status,
        status_label,
        _num(trend_pct),
        formatted_value,
    )


# ---------------------------------------------------------------------------
# Batch upsert
# ---------------------------------------------------------------------------

async def _upsert_kpi_latest_batch(
    db,
    model_id: UUID,
    kpi_objs: dict,
    result_map: dict,
    *,
    eval_version_id=None,
    eval_epoch: int | None = None,
    eval_started_at=None,
    eval_generation: int | None = None,
) -> KpiPublishOutcome:
    """Upsert evaluated KPI results into kpi_latest for the $KPIs virtual table.

    F-017-29: ``evaluate-batch`` is on the scorecard *read* path and was issuing
    one UPSERT plus a commit on every render even when the materialised value
    had not changed -- write amplification on a hot read path. We now load the
    existing rows once and write only the KPIs whose value-bearing columns
    actually differ (or that have no row yet). When nothing changed we skip the
    writes and the commit entirely. The published $KPIs value is unaffected: a
    skipped write would have stored identical numbers, so the governed value
    fed to the JDBC ``$KPIs`` surface stays correct. ``evaluated_at`` is not
    part of the change set, so an unchanged render no longer bumps it -- that is
    intentional (it is a freshness stamp, not a value).

    Each changed row is still upserted independently inside a SAVEPOINT so a
    single failing row (e.g. a constraint or persistence error on one KPI)
    cannot abort the whole batch -- the others are still materialised, and the
    failure is logged per-KPI. Status labels are truncated to the column width
    before the write; the explicit fail-loud label is still returned to the
    caller in full via the response, only the materialised $KPIs copy is
    bounded.

    Bug-7982 completion round (wrong-number stamp-timing): ``eval_version_id`` /
    ``eval_epoch`` MUST be the deploy pointer/epoch that was current when the
    evaluation whose results are in ``result_map`` STARTED (captured by the
    caller before the — possibly slow — evaluation ran), never re-read fresh
    here at write time. A revert can commit and bump ``deploy_epoch`` WHILE an
    evaluation is in flight; the values in ``result_map`` were computed under
    the OLD definition, so stamping them with a freshly-read NEW epoch here
    would make a stale/wrong-definition value look current on ``$KPIs``. The
    caller (``kpis.py`` ``evaluate_batch``) reads the model's epoch once at the
    top of the request, before resolving served KPI definitions or evaluating
    anything, and threads it through unchanged. When the caller passes no
    explicit binding (``eval_epoch is None``), the model has none either way
    (or the caller intentionally withholds it), so no row is stamped as
    current-for-a-real-epoch — never silently re-derived from a possibly-newer
    DB read.

    Bug-7982 completion round (opus5 finding 2.5 — epoch monotonicity): capturing
    the epoch at evaluation START (above) closes the wrong-number race, but it
    opens a NEW availability race with the post-deploy re-eval trigger
    (``src/kpi_reeval_trigger.py``): a SLOW evaluation started under epoch 5 can still
    be in flight when a deploy bumps the epoch to 6 and the trigger's FAST
    re-evaluation (also epoch 6) already published a fresh, servable row; if the
    slow epoch-5 write is then applied unconditionally, it CLOBBERS the fresh
    epoch-6 row with a now-stale epoch-5 value, and ``$KPIs`` goes from serving a
    correct number to serving NOTHING until the next hourly sweep. Each row's
    write is therefore gated on ``where=(existing.evaluated_for_epoch IS NULL OR
    existing.evaluated_for_epoch <= eval_epoch)`` — a write for an epoch OLDER
    than what is already published is suppressed (logged at WARNING; the request
    still returns success, since dropping the caller's stale write is exactly the
    safety property this closes), never regresses a newer row. Skipped when
    ``eval_epoch is None`` (no reference epoch to compare against; matches the
    unconditional write behaviour test callers that omit the kwarg expect).

    Bug-7982 R7 finding 1 (dedup stranded the ordering marker): the pre-upsert
    skip now consults the SHARED ``kpi_latest_write_is_noop`` predicate, which
    treats the ordering token as a stored, load-bearing column. See its docstring
    for the reproduced three-writer sequence the old value-only key admitted.

    Bug-7982 R7 finding 5 (swallowed publish failure): this helper now RETURNS a
    :class:`KpiPublishOutcome` instead of ``None``. A per-row failure is still
    isolated (one bad KPI must not starve its siblings) but it is no longer
    invisible: the caller propagates ``succeeded`` to the response, and the
    durable ``pending_kpi_reeval`` outbox row is cleared ONLY on a genuine
    success. Previously the failure was logged and dropped, evaluate-batch
    returned HTTP 200 regardless, and both outbox-clearing paths deleted the row
    on that 200 — a publish failure destroyed its own safety net.
    """
    from datetime import datetime, timezone
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not result_map:
        return KpiPublishOutcome()

    # Load existing rows once so we can dedupe unchanged writes.
    existing_rows = (
        await db.execute(
            select(KPILatest).where(
                KPILatest.model_id == model_id,
                KPILatest.kpi_id.in_(list(result_map.keys())),
            )
        )
    ).scalars().all()
    existing_by_kpi = {
        row.kpi_id: (
            _kpi_latest_value_tuple(
                kpi_name=row.kpi_name,
                value=row.value,
                target=row.target,
                status=row.status,
                status_label=row.status_label,
                trend_pct=row.trend_pct,
                formatted_value=row.formatted_value,
            ),
            # Bug-7982 residual 2: include the deploy binding so a value that is
            # numerically unchanged but was evaluated for a STALE epoch (e.g. after
            # a revert bumped deploy_epoch) is still rewritten with the current
            # epoch — otherwise the dedup would leave it permanently non-servable.
            row.evaluated_for_epoch,
            row.evaluated_for_version_id,
            # R7 finding 1: the ORDERING TOKEN is part of the stored state a skip
            # would leave behind. Without it, a same-value-but-later write is
            # skipped, the published token never advances, and an interleaved
            # STALER writer then beats the stranded token and serves a wrong value.
            row.eval_generation,
            row.eval_started_at,
        )
        for row in existing_rows
    }

    now = datetime.now(timezone.utc)
    considered = 0
    skipped = 0
    suppressed = 0
    failed = 0
    persisted = 0
    # R6 finding 2: an ``INSERT ... ON CONFLICT DO UPDATE ... WHERE <false>``
    # still ACQUIRES the conflicting row's lock during the conflict check even
    # when the WHERE suppresses the update. If we only end the transaction when
    # ``persisted > 0``, a suppressed-only (or all-failed) batch leaves those row
    # locks held for the rest of the caller's request. Track whether ANY upsert
    # statement ran so we can release the locks with an explicit rollback below.
    attempted_upsert = False
    for kpi_id, response in result_map.items():
        kpi = kpi_objs.get(kpi_id)
        if kpi is None:
            continue
        considered += 1
        # R7 finding 5: bound EVERY length-constrained column, not just the
        # status label. An unbounded formatted_value (String(128)) is the
        # reachable StringDataRightTruncationError that made the swallowed
        # per-row failure a real, not theoretical, outbox-loss trigger.
        kpi_name, status_label, formatted_value = bound_kpi_latest_strings(
            kpi_name=kpi.name,
            status_label=response.status_label,
            formatted_value=response.formatted_value,
        )
        new_tuple = _kpi_latest_value_tuple(
            kpi_name=kpi_name,
            value=response.value,
            target=response.target,
            status=response.status,
            status_label=status_label,
            trend_pct=response.trend_pct,
            formatted_value=formatted_value,
        )
        # Skip the write only when it would change NOTHING that is stored — value,
        # deploy binding AND ordering token (R7 finding 1).
        _existing = existing_by_kpi.get(kpi_id)
        if kpi_latest_write_is_noop(
            stored_value_tuple=_existing[0] if _existing else None,
            stored_epoch=_existing[1] if _existing else None,
            stored_version_id=_existing[2] if _existing else None,
            stored_generation=_existing[3] if _existing else None,
            stored_started_at=_existing[4] if _existing else None,
            new_value_tuple=new_tuple,
            eval_epoch=eval_epoch,
            eval_version_id=eval_version_id,
            eval_generation=eval_generation,
            eval_started_at=eval_started_at,
        ):
            skipped += 1
            continue
        _upsert_values = {
            "kpi_name": kpi_name,
            "value": response.value,
            "target": response.target,
            "status": response.status,
            "status_label": status_label,
            "trend_pct": response.trend_pct,
            "formatted_value": formatted_value,
            "evaluated_at": now,
            "evaluated_for_version_id": eval_version_id,
            "evaluated_for_epoch": eval_epoch,
            "eval_started_at": eval_started_at,
            "eval_generation": eval_generation,
        }
        conflict_kwargs: dict = {
            "constraint": "uq_kpi_latest_model_kpi",
            # R7 review round 1, finding 5: a writer with no generation of its
            # own must not overwrite a stored one with NULL — that would make the
            # row sort oldest and let a genuinely staler evaluation publish over
            # it. The INSERT still stores NULL; only the UPDATE preserves.
            "set_": strip_ungenerated_token(_upsert_values, eval_generation),
        }
        # R6 finding 1: order writes by the tuple (evaluated_for_epoch,
        # eval_started_at), not epoch alone — otherwise two SAME-epoch writers
        # race and the last to COMMIT wins even if it read staler data. The guard
        # is the single shared helper both upsert helpers use so they cannot drift.
        _guard = kpi_latest_write_guard(eval_epoch, eval_started_at, eval_generation)
        if _guard is not None:
            conflict_kwargs["where"] = _guard
        stmt = (
            pg_insert(KPILatest)
            .values(model_id=model_id, kpi_id=kpi_id, **_upsert_values)
            .on_conflict_do_update(**conflict_kwargs)
            # RETURNING lets us detect a conflict whose `where` evaluated false
            # (Postgres treats that as "no action" — no row comes back) so we
            # can tell a genuine write from a suppressed stale one.
            .returning(KPILatest.kpi_id)
        )
        try:
            # SAVEPOINT: isolate each row so one failure cannot poison the
            # surrounding transaction and abort the rest of the batch.
            attempted_upsert = True
            async with db.begin_nested():
                result = await db.execute(stmt)
            if result.first() is None:
                suppressed += 1
                log.warning(
                    "kpi_latest upsert SUPPRESSED for kpi %s (%s) on model %s: a "
                    "FRESHER evaluation is already published (a newer epoch, or the "
                    "SAME epoch %s with a later ordering token) — this slower/staler "
                    "evaluation would otherwise have clobbered it.",
                    kpi_id, getattr(kpi, "name", "?"), model_id, eval_epoch,
                )
            else:
                persisted += 1
        except Exception as exc:  # noqa: BLE001 -- isolate the row, but REPORT it
            # R7 finding 5: still isolated (a single bad KPI must not starve its
            # siblings) but no longer invisible — the failure is counted and
            # surfaced to the caller so the durable outbox row is NOT cleared.
            failed += 1
            log.error(
                "kpi_latest upsert FAILED for kpi %s (%s) on model %s: %s — "
                "$KPIs will not serve a current-epoch value for this KPI; the "
                "post-deploy re-eval outbox row is retained for retry.",
                kpi_id, getattr(kpi, "name", "?"), model_id, exc,
            )
    # R6 finding 2: end the transaction after ANY upsert attempt, not only when a
    # row persisted. A suppressed-only or all-failed batch still took row locks in
    # the conflict check; an explicit rollback releases them immediately instead
    # of holding them through the rest of the caller's request.
    if persisted:
        try:
            await db.commit()
        except Exception as exc:
            await db.rollback()
            # The commit failure means every row that "persisted" rolled back.
            log.error(
                "kpi_latest commit FAILED for model %s: %s — %d row(s) rolled "
                "back; the post-deploy re-eval outbox row is retained for retry.",
                model_id, exc, persisted,
            )
            failed += persisted
            persisted = 0
    elif attempted_upsert:
        await db.rollback()
    return KpiPublishOutcome(
        considered=considered,
        persisted=persisted,
        suppressed=suppressed,
        skipped=skipped,
        failed=failed,
    )
