"""Bug-8071 — the miss row must keep a per-reason history, not just the last one.

``QueryMissLog`` is unique on (model, fingerprint, persona) and its
``occurrence_count`` accumulates across repeats, but the ON CONFLICT clause
OVERWROTE ``miss_reason`` every time. One row was doing two jobs — cumulative
candidate state AND event history — and only the newest event survived.

These tests pin the merge helper's accumulation, its bound, and its eviction
order. The end-to-end upsert wiring (insert path and conflict path both writing
``miss_reason_counts_json``) is covered alongside the existing upsert tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.logging.query_logger import _MAX_MISS_REASONS, _merge_miss_reason

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)


def _counts(history):
    return {
        e["reason"]: e["occurrence_count"]
        for e in history
        if "reason" in e
    }


def test_first_miss_seeds_the_history():
    history = _merge_miss_reason(None, "grain_missing", _T0)
    assert _counts(history) == {"grain_missing": 1}
    assert history[0]["first_seen_at"] == _T0.isoformat()
    assert history[0]["last_seen_at"] == _T0.isoformat()


def test_repeat_of_the_same_reason_increments_and_keeps_first_seen():
    history = _merge_miss_reason(None, "grain_missing", _T0)
    later = _T0 + timedelta(hours=5)
    history = _merge_miss_reason(history, "grain_missing", later)

    assert _counts(history) == {"grain_missing": 2}
    assert history[0]["first_seen_at"] == _T0.isoformat()
    assert history[0]["last_seen_at"] == later.isoformat()


def test_a_new_reason_does_not_erase_the_old_one():
    """The reported defect: 400 grain_missing misses followed by one stale miss
    used to read as simply 'stale'."""
    history = None
    for i in range(400):
        history = _merge_miss_reason(history, "grain_missing", _T0 + timedelta(minutes=i))
    history = _merge_miss_reason(history, "stale", _T0 + timedelta(days=1))

    assert _counts(history) == {"grain_missing": 400, "stale": 1}


def test_history_is_bounded_and_evicts_least_recently_seen():
    """The joined ``aggregate_skip:a,b`` form makes the stored string's
    cardinality combinatorial, so the list must not grow without limit."""
    history = None
    # Distinct reasons, oldest first.
    for i in range(_MAX_MISS_REASONS + 5):
        history = _merge_miss_reason(history, f"reason_{i}", _T0 + timedelta(minutes=i))

    assert len(history) == _MAX_MISS_REASONS
    kept = {e["reason"] for e in history if "reason" in e}
    # A summary occupies one bounded slot, so six individual reasons were
    # evicted while their BUILD totals remain exact.
    assert "reason_0" not in kept
    assert f"reason_{_MAX_MISS_REASONS + 4}" in kept
    summary = next(e for e in history if "class_totals" in e)
    assert summary["class_totals"]["build"] == 6
    assert sum(
        e.get("occurrence_count", 0) for e in history
    ) == _MAX_MISS_REASONS + 5


def test_malformed_existing_history_does_not_crash_the_merge():
    """Miss-log telemetry must never fail the user query (Bug-6726). A corrupt
    JSONB value must degrade, not raise."""
    history = _merge_miss_reason(["garbage", 7, None], "stale", _T0)
    assert _counts(history) == {"stale": 1}


# ---------------------------------------------------------------------------
# Producer wiring: the history must actually be written on BOTH upsert paths.
# A merge helper that is never persisted is the classic
# producer-fixed-consumer-unwired gap.
# ---------------------------------------------------------------------------

import uuid  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from src.ir.logical_query import BoundQuery  # noqa: E402
from src.logging.query_logger import log_query_miss  # noqa: E402


def _bound():
    lq = MagicMock()
    lq.query_fingerprint = "f" * 64
    lq.raw_query = "SELECT region, SUM(revenue) FROM t GROUP BY region"
    lq.grain = ["region"]
    lq.has_unresolvable_where = False
    lq.has_complex_sql = False
    model = MagicMock()
    model.id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    return BoundQuery(
        logical_query=lq, model=model,
        resolved_dimensions=[SimpleNamespace(name="region")],
        resolved_measures=[SimpleNamespace(name="revenue")],
        resolved_filters=[],
    )


class _FakeDB:
    """Captures the statements log_query_miss issues.

    ``occurrence_count`` on the RETURNING row selects the path: 1 = fresh
    insert (no follow-up UPDATE), >1 = conflict (follow-up UPDATE merges).
    """

    def __init__(
        self,
        occurrence_count: int,
        existing_reasons=None,
        existing_reason: str = "grain_missing",
    ):
        self._occ = occurrence_count
        self._existing = existing_reasons
        self._existing_reason = existing_reason
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        result = MagicMock()
        result.fetchone.return_value = SimpleNamespace(
            id=uuid.uuid4(),
            occurrence_count=self._occ,
            predicate_variants_json=None,
            miss_reason_counts_json=self._existing,
            miss_reason=self._existing_reason,
            first_seen_at=_T0,
        )
        return result

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_insert_path_seeds_the_reason_history():
    db = _FakeDB(occurrence_count=1)
    await log_query_miss(db, _bound(), "grain_missing")

    insert_stmt = db.statements[0]
    values = insert_stmt.compile().params
    assert values["miss_reason"] == "grain_missing"
    seeded = values["miss_reason_counts_json"]
    assert [e["reason"] for e in seeded] == ["grain_missing"]
    assert seeded[0]["occurrence_count"] == 1


@pytest.mark.asyncio
async def test_conflict_path_merges_into_the_existing_history():
    """The defect in one assertion: the repeat must ADD to the history, and the
    earlier reason must survive rather than be overwritten."""
    existing = [{
        "reason": "grain_missing",
        "occurrence_count": 400,
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
    }]
    db = _FakeDB(occurrence_count=401, existing_reasons=existing)
    await log_query_miss(db, _bound(), "stale")

    # statements[0] = upsert, statements[1] = follow-up merge UPDATE
    assert len(db.statements) == 2
    merged = db.statements[1].compile().params["miss_reason_counts_json"]
    counts = {e["reason"]: e["occurrence_count"] for e in merged}
    assert counts == {"grain_missing": 400, "stale": 1}


@pytest.mark.asyncio
async def test_first_post_upgrade_event_preserves_the_legacy_reason_baseline():
    """A row with 400 legacy misses and no histogram must not attribute all
    401 occurrences to the first new reason after the migration."""
    db = _FakeDB(
        occurrence_count=401,
        existing_reasons=None,
        existing_reason="grain_missing",
    )
    await log_query_miss(db, _bound(), "stale")

    update_values = db.statements[1].compile().params
    merged = update_values["miss_reason_counts_json"]
    assert _counts(merged) == {"stale": 1}
    summary = next(e for e in merged if "class_totals" in e)
    assert summary["class_totals"] == {
        "build": 400, "repair": 0, "ineligible": 0,
    }
    assert update_values["miss_reason"] == "stale"
