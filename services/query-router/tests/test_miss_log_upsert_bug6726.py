"""Bug-6726 regression: miss-log upsert and telemetry isolation.

Two defects:
(a) Repeated queries with the same (model, fingerprint, persona) caused an
    IntegrityError on uq_query_miss_logs_model_fingerprint_persona because
    the miss-log insert was not an upsert.  The new implementation uses
    INSERT ... ON CONFLICT DO UPDATE.
(b) A bookkeeping / telemetry failure in the miss-log path propagated as a
    500 into the user query.  record_query_success now wraps the miss-log
    call in try/except.

These tests verify both fixes at the unit level without a live database.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ir.logical_query import BoundQuery, LogicalFilter
from src.logging.query_logger import log_query_miss


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dim(name: str) -> SimpleNamespace:
    obj = SimpleNamespace()
    obj.name = name
    return obj


def _measure(name: str) -> SimpleNamespace:
    obj = SimpleNamespace()
    obj.name = name
    return obj


def _make_bound(
    select_dims: list[str] | None = None,
    filter_dims: list[str] | None = None,
    group_by: list[str] | None = None,
    measures: list[str] | None = None,
    fingerprint: str = "deadbeef" * 8,
) -> BoundQuery:
    select_dims = select_dims or ["region"]
    filter_dims = filter_dims or []
    group_by = group_by or ["region"]
    measures = measures or ["revenue"]

    lq = MagicMock()
    lq.query_fingerprint = fingerprint
    lq.raw_query = "SELECT region, SUM(revenue) FROM t GROUP BY region"
    lq.grain = group_by
    lq.has_unresolvable_where = False
    lq.has_complex_sql = False

    model = MagicMock()
    model.id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_dimensions=[_dim(d) for d in select_dims],
        resolved_measures=[_measure(m) for m in measures],
        resolved_filters=[
            LogicalFilter(dimension_name=d, operator="eq", value="x")
            for d in filter_dims
        ],
    )


class _UpsertFakeDB:
    """Minimal async DB stub that simulates the upsert RETURNING result.

    On first call to execute() with an INSERT statement, returns a row
    with occurrence_count=1 (fresh insert).  On second call, returns
    occurrence_count=2 (conflict / upsert path).
    """

    def __init__(self) -> None:
        self._insert_call_count = 0
        self.committed = 0
        self.executed_stmts: list = []

    async def execute(self, stmt):
        self.executed_stmts.append(stmt)
        stmt_str = str(getattr(stmt, "compile", lambda: stmt)())
        # Detect whether this is the upsert INSERT or the post-upsert UPDATE
        is_insert = "INSERT" in str(type(stmt).__name__).upper() or (
            hasattr(stmt, "is_insert") and stmt.is_insert
        )
        if is_insert or "insert" in stmt_str.lower()[:30]:
            self._insert_call_count += 1
            row = SimpleNamespace(
                id=uuid.uuid4(),
                occurrence_count=self._insert_call_count,
                predicate_variants_json=None if self._insert_call_count == 1 else [
                    {
                        "predicate_set_hash": "abc123",
                        "predicates": [],
                        "occurrence_count": 1,
                        "last_seen_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
                # Bug-8071: the upsert RETURNING clause now also yields the
                # per-reason history so the follow-up UPDATE can merge it.
                miss_reason_counts_json=None if self._insert_call_count == 1 else [
                    {
                        "reason": "no_aggregate",
                        "occurrence_count": 1,
                        "first_seen_at": "2026-01-01T00:00:00+00:00",
                        "last_seen_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
                miss_reason="no_aggregate",
                first_seen_at="2026-01-01T00:00:00+00:00",
            )
            result = MagicMock()
            result.fetchone.return_value = row
            return result
        # UPDATE statement (for predicate_variants_json merge)
        return MagicMock()

    async def commit(self) -> None:
        self.committed += 1


# ---------------------------------------------------------------------------
# (a) Upsert: same query twice -> second write upserts without error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_insert_succeeds():
    """First miss-log write for a new (model, fingerprint, persona) succeeds."""
    bound = _make_bound()
    db = _UpsertFakeDB()
    await log_query_miss(db, bound, "no_aggregate")

    assert db.committed == 1
    # First call: occurrence_count=1 -> no variant merge UPDATE needed
    # Only the upsert INSERT statement should have been executed + commit
    assert db._insert_call_count == 1


@pytest.mark.asyncio
async def test_second_write_upserts_no_error():
    """Second miss-log write for the same key upserts (no IntegrityError).

    Bug-6726: before the fix, the second INSERT would violate the unique
    constraint and the IntegrityError would 500 the user query.
    """
    bound = _make_bound()
    db = _UpsertFakeDB()

    # First write
    await log_query_miss(db, bound, "no_aggregate")

    # Second write -- same key, must NOT raise
    await log_query_miss(db, bound, "no_aggregate")

    assert db.committed == 2
    assert db._insert_call_count == 2


@pytest.mark.asyncio
async def test_upsert_merges_predicate_variants():
    """On the conflict path (occurrence_count > 1), predicate variants are
    merged via the post-upsert UPDATE."""
    bound = _make_bound(filter_dims=["manager"])
    db = _UpsertFakeDB()

    # First write
    await log_query_miss(db, bound, "no_aggregate")
    first_stmt_count = len(db.executed_stmts)

    # Second write triggers the variant merge (occurrence_count=2)
    await log_query_miss(db, bound, "no_aggregate")
    # The second call should execute BOTH the upsert INSERT and the
    # post-merge UPDATE statement (2 extra statements).
    assert len(db.executed_stmts) == first_stmt_count + 2


# ---------------------------------------------------------------------------
# (b) Telemetry isolation: forced failure does not fail the query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telemetry_failure_does_not_propagate():
    """A forced telemetry failure in log_query_miss must NOT propagate
    when called through record_query_success.

    Bug-6726 (b): before the fix, an IntegrityError from the miss-log
    path would 500 the user query even though the query had already
    executed successfully.
    """
    from src.api.routes import record_query_success
    from src.ir.logical_query import LogicalQuery

    bound = _make_bound()
    decision = SimpleNamespace(
        route_type="source",
        reason="no_aggregate",
        aggregate_id=None,
        pocket_id=None,
        rewritten_query="SELECT ...",
        aggregate_skipped_reasons=None,
    )

    # Mock log_query to succeed (it runs before log_query_miss)
    mock_log_query = AsyncMock()
    # Mock log_query_miss to RAISE (simulating a telemetry failure)
    mock_log_miss = AsyncMock(side_effect=RuntimeError("simulated DB failure"))

    mock_db = AsyncMock()
    mock_audit = AsyncMock()

    with patch("src.api.routes.log_query", mock_log_query), \
         patch("src.api.routes.log_query_miss", mock_log_miss), \
         patch("src.api.routes.audit", mock_audit):
        # Must NOT raise despite log_query_miss failure
        await record_query_success(
            mock_db,
            bound=bound,
            decision=decision,
            elapsed_ms=100,
            rows_returned=5,
            bytes_processed=1024,
            user_identity="test@example.com",
            tenant_id="test-tenant",
            persona=None,
            client_kind="jdbc",
        )

    # log_query_miss was called and raised, but the exception was swallowed
    mock_log_miss.assert_called_once()


@pytest.mark.asyncio
async def test_telemetry_success_still_logged():
    """When log_query_miss succeeds, the miss IS logged (no regression
    from the try/except wrapping)."""
    from src.api.routes import record_query_success

    bound = _make_bound()
    decision = SimpleNamespace(
        route_type="source",
        reason="no_aggregate",
        aggregate_id=None,
        pocket_id=None,
        rewritten_query="SELECT ...",
        aggregate_skipped_reasons=None,
    )

    mock_log_query = AsyncMock()
    mock_log_miss = AsyncMock()  # succeeds
    mock_db = AsyncMock()
    mock_audit = AsyncMock()

    with patch("src.api.routes.log_query", mock_log_query), \
         patch("src.api.routes.log_query_miss", mock_log_miss), \
         patch("src.api.routes.audit", mock_audit):
        await record_query_success(
            mock_db,
            bound=bound,
            decision=decision,
            elapsed_ms=100,
            rows_returned=5,
            bytes_processed=1024,
            user_identity="test@example.com",
            tenant_id="test-tenant",
            persona=None,
            client_kind="jdbc",
        )

    mock_log_miss.assert_called_once()
