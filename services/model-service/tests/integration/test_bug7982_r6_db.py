"""Real-async DB guards for the Bug-7982 R6 Codex-reproduced findings.

These prove behaviour a mocked session cannot — the exact class that let the R3
and R5 "zero findings" rounds ship the bugs the external gate then reproduced
live:

  * finding 1 — within-epoch write ordering: a later-STARTING same-epoch
    evaluation is authoritative regardless of commit order;
  * finding 2 — a suppressed-only upsert batch ENDS its transaction (releases the
    row lock) instead of holding it for the rest of the caller's request;
  * finding 5 — ``lock_timeout`` is restored after the advisory-lock acquisition,
    so later statements in the same transaction are not bound by the short
    acquisition timeout;
  * finding 6 (producer) — the durable ``pending_kpi_reeval`` outbox row is
    written for a deployed model and upserts (one row per model).

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres. Reuses the isolated-schema fixture.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_bug7982_r6_db.py -v
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from shared.db.models import KPI, KPILatest, PendingKpiReeval
from src.api._model_lock import acquire_model_definition_lock
from src.api.kpi_latest import _upsert_kpi_latest_batch

# Reuse the isolated-schema + seed helpers (creates every TenantBase table,
# including kpi_latest.eval_started_at and pending_kpi_reeval).
from tests.integration.test_versioning_consistency_db import (  # noqa: E402
    _DB_URL,
    _isolated_schema,
    _seed_model,
)

pytestmark = [pytest.mark.integration]


def _resp(value):
    return SimpleNamespace(
        value=value, target=None, status=1, status_label="ok",
        trend_pct=None, formatted_value=str(value),
    )


async def _seed_kpi(session, model_id) -> uuid.UUID:
    kpi_id = uuid.uuid4()
    session.add(KPI(id=kpi_id, model_id=model_id, name=f"kpi-{kpi_id.hex[:6]}"))
    await session.flush()
    await session.commit()
    return kpi_id


async def _stored_value(session, model_id, kpi_id):
    row = (
        await session.execute(
            select(KPILatest).where(
                KPILatest.model_id == model_id, KPILatest.kpi_id == kpi_id
            )
        )
    ).scalar_one_or_none()
    return None if row is None else float(row.value)


# ---------------------------------------------------------------------------
# Finding 1 — within-epoch ordering (later START wins regardless of commit order)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_later_started_same_epoch_write_wins_regardless_of_commit_order():
    epoch = 6
    t_early = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)
    t_late = t_early + timedelta(seconds=30)

    # Case A: the LATER-started write (value 250) commits FIRST, then the
    # earlier-started write (value 100) commits — it must be SUPPRESSED, not clobber.
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)

            await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(250)},
                eval_epoch=epoch, eval_started_at=t_late,
            )
            await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(100)},
                eval_epoch=epoch, eval_started_at=t_early,
            )
            assert await _stored_value(s, model_id, kpi_id) == 250, (
                "an earlier-started same-epoch write clobbered a later-started one"
            )

    # Case B: the earlier-started write (100) commits first, then the
    # later-started (250) — the later-started value must WIN (apply).
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)

            await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(100)},
                eval_epoch=epoch, eval_started_at=t_early,
            )
            await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(250)},
                eval_epoch=epoch, eval_started_at=t_late,
            )
            assert await _stored_value(s, model_id, kpi_id) == 250, (
                "the later-started same-epoch write did not win"
            )


# ---------------------------------------------------------------------------
# Finding 2 — a suppressed-only batch releases the row lock
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_suppressed_only_batch_releases_row_lock(tmp_path):
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s1:
            model_id = await _seed_model(s1)
            kpi_id = await _seed_kpi(s1, model_id)
            kpi = await s1.get(KPI, kpi_id)
            # Publish a NEWER epoch-7 row so an epoch-6 write is fully suppressed.
            await _upsert_kpi_latest_batch(
                s1, model_id, {kpi_id: kpi}, {kpi_id: _resp(777)},
                eval_epoch=7, eval_started_at=datetime.now(timezone.utc),
            )
            # A suppressed-only batch (epoch 6 < published 7): nothing persists.
            await _upsert_kpi_latest_batch(
                s1, model_id, {kpi_id: kpi}, {kpi_id: _resp(100)},
                eval_epoch=6, eval_started_at=datetime.now(timezone.utc),
            )
            # With finding 2 fixed, s1's transaction is ended (rolled back), so a
            # SECOND concurrent session can lock/update the SAME row immediately.
            async with factory() as s2:
                await s2.execute(text("SET LOCAL lock_timeout = '3s'"))
                # FOR UPDATE NOWAIT would raise 55P03 if s1 still held the lock.
                locked = (
                    await s2.execute(
                        text(
                            "SELECT value FROM kpi_latest WHERE model_id = :m "
                            "AND kpi_id = :k FOR UPDATE NOWAIT"
                        ),
                        {"m": model_id, "k": kpi_id},
                    )
                ).scalar_one()
                assert float(locked) == 777
                await s2.rollback()


# ---------------------------------------------------------------------------
# Finding 5 — lock_timeout is restored after acquisition (within the same txn)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_lock_timeout_is_restored_after_acquisition():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            # Value in effect at the start of this transaction (session default).
            before = (
                await s.execute(text("SELECT current_setting('lock_timeout')"))
            ).scalar_one()

            await acquire_model_definition_lock(s, model_id)

            after = (
                await s.execute(text("SELECT current_setting('lock_timeout')"))
            ).scalar_one()
            # Restored to the prior value — NOT stuck at the short acquisition
            # timeout that governs only the pg_advisory_xact_lock call itself.
            assert after == before, (
                f"lock_timeout leaked: before={before!r} after={after!r} "
                "(a later statement in this txn would be bound by the short "
                "acquisition timeout — finding 5)"
            )
            assert after != "30s", "lock_timeout is still the short acquisition value"


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_lock_acquisition_is_actually_bounded_by_the_short_timeout(monkeypatch):
    """Finding 5 (reviewer #9): the short timeout must ACTUALLY bound the
    acquisition — proven by contention, not just by reading lock_timeout back.

    Mutation check: deleting the ``setshort`` CTE (never bounding the wait) makes
    this hang until the session default rather than raising 503 within ~1s.

    R7 finding 6 (test quality): the assertion used to accept anything under the
    8s ``wait_for`` cap, which could not distinguish "bounded at 1s" from "not
    bounded at all but the harness gave up". It now asserts BOTH sides of the
    documented ~1s bound: the call really contended (it did not return
    instantly), and it was released close to 1s rather than at some later,
    unrelated timeout. The session default in this environment is well above the
    upper bound, so a dropped ``setshort`` CTE fails the upper assertion."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from fastapi import HTTPException

    from src.api._model_lock import model_advisory_lock_key

    async with _isolated_schema() as (factory, _schema):
        async with factory() as holder:
            model_id = await _seed_model(holder)
            key = model_advisory_lock_key(model_id)
            # Holder takes the SAME advisory lock and keeps its transaction open.
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": key}
            )

            fake_settings = SimpleNamespace(MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS=1)
            async with factory() as waiter:
                import time as _t

                import asyncio

                # R7 finding 6: WARM the waiter's connection before timing. An
                # AsyncSession connects lazily on its first statement, so leaving
                # that inside the timed region measured ~2s of asyncpg connect +
                # search_path setup on top of the 1s lock wait — which is why the
                # old assertion had to be loosened to "< 8s" and could therefore
                # not prove the documented bound at all. Measured directly
                # against this Postgres, the CTE bounds the wait at 1.00s.
                await waiter.execute(text("SELECT 1"))
                await waiter.rollback()

                t0 = _t.monotonic()
                with patch(
                    "shared.db.model_lock.get_settings", return_value=fake_settings
                ):
                    # wait_for so a REGRESSION that drops the short-timeout CTE
                    # (unbounded wait) fails CLEANLY as a TimeoutError rather than
                    # hanging the CI job (reviewer round 2, #6).
                    with pytest.raises(HTTPException) as exc:
                        await asyncio.wait_for(
                            acquire_model_definition_lock(waiter, model_id),
                            timeout=8,
                        )
                elapsed = _t.monotonic() - t0
                assert exc.value.status_code == 503
                # R7 finding 6: assert BOTH sides of the documented ~1s bound.
                # Lower bound — it really contended (an instant 503 would mean
                # the lock was never actually contested and the timing proves
                # nothing). Upper bound — it was released at ~1s, not at some
                # later unrelated timeout or the harness's own 8s cap, which the
                # old `elapsed < 8` assertion could not distinguish.
                assert 0.5 <= elapsed < 2.0, (
                    f"acquisition was not bounded at the configured 1s "
                    f"(took {elapsed:.2f}s; expected roughly 1s of real waiting)"
                )
                await waiter.rollback()
            await holder.rollback()


# ---------------------------------------------------------------------------
# Round-2 #3 — eval_started_at validator (naive -> UTC) + clamp-prevents-wedge
# ---------------------------------------------------------------------------


def test_kpi_batch_request_coerces_naive_eval_started_at_to_utc():
    """Round-2 #3: a NAIVE marker is coerced to tz-aware UTC so it cannot be
    silently timezone-shifted on persist and invert the ordering. (Pure unit — no
    DB.)"""
    from shared.schemas.domains.governance_advanced import KPIBatchRequest

    req = KPIBatchRequest(
        kpi_ids=[uuid.uuid4()], eval_started_at=datetime(2026, 7, 28, 10, 0, 0)
    )
    assert req.eval_started_at.tzinfo is not None
    assert req.eval_started_at.utcoffset().total_seconds() == 0
    # An already-aware value passes through unchanged.
    aware = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)
    assert KPIBatchRequest(kpi_ids=[uuid.uuid4()], eval_started_at=aware).eval_started_at == aware


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_future_marker_wedges_but_clamped_marker_does_not():
    """Round-2 #3: prove WHY the handler clamps body.eval_started_at to the server
    clock. A stored FUTURE same-epoch marker suppresses every later legitimate
    same-epoch write (a wedge); a clamped (server-clock) marker does not."""
    epoch = 6
    server_now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)

            # UNCLAMPED: a bogus future marker is published (value 200).
            await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(200)},
                eval_epoch=epoch, eval_started_at=future,
            )
            # A later legitimate same-epoch write (server-clock time) is SUPPRESSED
            # — the wedge the clamp exists to prevent.
            await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(300)},
                eval_epoch=epoch, eval_started_at=server_now,
            )
            assert await _stored_value(s, model_id, kpi_id) == 200, (
                "expected the future marker to wedge the row (demonstrating the risk)"
            )

        # CLAMPED: the same publish clamped to the server clock does NOT wedge.
        async with factory() as s:
            model_id = await _seed_model(s)
            kpi_id = await _seed_kpi(s, model_id)
            kpi = await s.get(KPI, kpi_id)

            clamped = min(future, server_now)  # what kpis.py does: min(body, clock)
            await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(200)},
                eval_epoch=epoch, eval_started_at=clamped,
            )
            await _upsert_kpi_latest_batch(
                s, model_id, {kpi_id: kpi}, {kpi_id: _resp(300)},
                eval_epoch=epoch, eval_started_at=server_now + timedelta(seconds=1),
            )
            assert await _stored_value(s, model_id, kpi_id) == 300, (
                "clamping to the server clock must let a later legit write apply"
            )


# ---------------------------------------------------------------------------
# Finding 6 (producer) — the durable outbox row
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
async def test_pending_kpi_reeval_outbox_is_written_and_upserts():
    from src.api.versions import _enqueue_pending_kpi_reeval

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id = await _seed_model(s)
            project_id = (
                await s.execute(text("SELECT project_id FROM models WHERE id = :m"),
                                {"m": model_id})
            ).scalar_one()

            # No deployed KPI yet -> enqueue is a no-op (nothing to re-evaluate).
            await _enqueue_pending_kpi_reeval(
                s, model_id=model_id, project_id=project_id, epoch=1
            )
            await s.commit()
            s.expire_all()
            rows = (await s.execute(select(PendingKpiReeval))).scalars().all()
            assert rows == [], "outbox row written for a model with no deployed KPI"

            # Add a DEPLOYED KPI -> enqueue writes exactly one row.
            kpi_id = uuid.uuid4()
            s.add(KPI(id=kpi_id, model_id=model_id, name="k", is_deployed=True))
            await s.flush()
            await _enqueue_pending_kpi_reeval(
                s, model_id=model_id, project_id=project_id, epoch=1
            )
            await s.commit()
            s.expire_all()
            rows = (await s.execute(select(PendingKpiReeval))).scalars().all()
            assert len(rows) == 1 and rows[0].requested_for_epoch == 1

            # A later deploy UPSERTS the same row with the newer epoch (one row).
            await _enqueue_pending_kpi_reeval(
                s, model_id=model_id, project_id=project_id, epoch=2
            )
            await s.commit()
            s.expire_all()
            rows = (await s.execute(select(PendingKpiReeval))).scalars().all()
            assert len(rows) == 1 and rows[0].requested_for_epoch == 2

            # The epoch-bounded delete (used by the trigger + sweep drain) removes
            # the row only up to the drained epoch.
            await s.execute(
                text(
                    "DELETE FROM pending_kpi_reeval WHERE model_id = :m "
                    "AND requested_for_epoch <= :e"
                ),
                {"m": model_id, "e": 1},
            )
            await s.commit()
            s.expire_all()
            rows = (await s.execute(select(PendingKpiReeval))).scalars().all()
            assert len(rows) == 1, "epoch-2 row wrongly deleted by an epoch-1 drain"

            await s.execute(
                text(
                    "DELETE FROM pending_kpi_reeval WHERE model_id = :m "
                    "AND requested_for_epoch <= :e"
                ),
                {"m": model_id, "e": 2},
            )
            await s.commit()
            s.expire_all()
            rows = (await s.execute(select(PendingKpiReeval))).scalars().all()
            assert rows == []
