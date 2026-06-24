"""Filter dimensions must be included in the miss log grain.

The aggregate matcher computes required_grain = GROUP_BY | WHERE_filter_dims.
An aggregate without filter dimensions cannot serve a filtered query — those
columns are absent from the pre-aggregated table.

Before the fix, log_query_miss stored only SELECT/GROUP BY dimensions, so the
AI optimiser received incomplete grains and would recommend aggregates that the
matcher would immediately reject at query time.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.ir.logical_query import BoundQuery, LogicalFilter, LogicalQuery
from src.logging.query_logger import log_query_miss


def _dim(name: str) -> SimpleNamespace:
    obj = SimpleNamespace()
    obj.name = name
    return obj


def _measure(name: str) -> SimpleNamespace:
    obj = SimpleNamespace()
    obj.name = name
    return obj


def _make_bound(
    select_dims: list[str],
    filter_dims: list[str],
    group_by: list[str],
    measures: list[str],
) -> BoundQuery:
    lq = MagicMock()
    lq.query_fingerprint = "deadbeef" * 8
    lq.raw_query = "SELECT d, COUNT(1) FROM t WHERE f = 'x' GROUP BY d"
    lq.grain = group_by

    model = MagicMock()
    model.id = "model-1"

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


class _FakeDB:
    """Minimal async DB stub — captures the object passed to add()."""

    def __init__(self) -> None:
        self.added: list = []

    async def execute(self, stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass


@pytest.mark.asyncio
async def test_filter_dims_included_in_requested_dimensions():
    """WHERE filter dim 'manager' must appear alongside SELECT dim 'department'."""
    bound = _make_bound(
        select_dims=["department"],
        filter_dims=["manager"],
        group_by=["department"],
        measures=["employee_count"],
    )
    db = _FakeDB()
    await log_query_miss(db, bound, "no_aggregate")

    assert db.added, "QueryMissLog was not added to the session"
    obj = db.added[0]
    assert "department" in obj.requested_dimensions
    assert "manager" in obj.requested_dimensions, (
        "Filter dimension 'manager' missing from requested_dimensions — "
        "aggregates without this column cannot serve the filtered query"
    )


@pytest.mark.asyncio
async def test_filter_dims_included_in_requested_grain():
    """requested_grain must mirror the full grain the aggregate matcher computes."""
    bound = _make_bound(
        select_dims=["department"],
        filter_dims=["manager"],
        group_by=["department"],
        measures=["employee_count"],
    )
    db = _FakeDB()
    await log_query_miss(db, bound, "no_aggregate")

    obj = db.added[0]
    assert "department" in obj.requested_grain
    assert "manager" in obj.requested_grain, (
        "Filter dimension 'manager' missing from requested_grain"
    )


@pytest.mark.asyncio
async def test_multiple_filter_dims_all_captured():
    """All WHERE-filtered dimensions appear in the logged grain."""
    bound = _make_bound(
        select_dims=["product"],
        filter_dims=["region", "manager"],
        group_by=["product"],
        measures=["revenue"],
    )
    db = _FakeDB()
    await log_query_miss(db, bound, "no_aggregate")

    obj = db.added[0]
    assert set(obj.requested_dimensions) == {"product", "region", "manager"}
    assert set(obj.requested_grain) == {"product", "region", "manager"}


@pytest.mark.asyncio
async def test_no_filter_dims_unchanged():
    """Queries without WHERE filters record only SELECT/GROUP BY dimensions."""
    bound = _make_bound(
        select_dims=["region", "payment_method"],
        filter_dims=[],
        group_by=["region", "payment_method"],
        measures=["base_amount"],
    )
    db = _FakeDB()
    await log_query_miss(db, bound, "no_aggregate")

    obj = db.added[0]
    assert set(obj.requested_dimensions) == {"region", "payment_method"}
    assert set(obj.requested_grain) == {"region", "payment_method"}


@pytest.mark.asyncio
async def test_filter_dim_already_in_select_not_duplicated():
    """A dimension that appears in both WHERE and SELECT is not duplicated."""
    bound = _make_bound(
        select_dims=["region", "department"],
        filter_dims=["region"],  # also in SELECT
        group_by=["region", "department"],
        measures=["headcount"],
    )
    db = _FakeDB()
    await log_query_miss(db, bound, "no_aggregate")

    obj = db.added[0]
    assert obj.requested_dimensions.count("region") == 1, (
        "Dimension appearing in both SELECT and WHERE must not be duplicated"
    )


# ---------------------------------------------------------------------------
# Re-queue tests — existing miss log must be refreshed and un-linked
# ---------------------------------------------------------------------------

import uuid as _uuid


class _FakeDBWithExisting:
    """DB stub that returns an existing QueryMissLog for execute()."""

    def __init__(self, existing_obj) -> None:
        self._existing = existing_obj
        self.added: list = []

    async def execute(self, stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._existing
        return result

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass


def _existing_miss(grain, measures, candidate_aggregate_id=None):
    obj = SimpleNamespace()
    obj.occurrence_count = 3
    obj.last_seen_at = None
    obj.miss_reason = "old_reason"
    obj.requested_grain = list(grain)
    obj.requested_dimensions = list(grain)
    obj.requested_measures = list(measures)
    obj.candidate_aggregate_id = candidate_aggregate_id
    # F-005-14: the upsert now also maintains a per-literal-variant breakdown.
    obj.predicate_variants_json = None
    return obj


@pytest.mark.asyncio
async def test_existing_miss_log_candidate_aggregate_id_cleared():
    """When an existing miss log has candidate_aggregate_id set but the query
    still misses, candidate_aggregate_id must be cleared so the optimizer
    re-queues the pattern."""
    existing = _existing_miss(
        grain=["region"],
        measures=["base_amount"],
        candidate_aggregate_id=_uuid.uuid4(),
    )
    assert existing.candidate_aggregate_id is not None

    bound = _make_bound(
        select_dims=["region"],
        filter_dims=["manager"],
        group_by=["region"],
        measures=["base_amount"],
    )
    db = _FakeDBWithExisting(existing)
    await log_query_miss(db, bound, "no_aggregate")

    assert existing.candidate_aggregate_id is None, (
        "candidate_aggregate_id must be cleared when the query still misses — "
        "the linked aggregate is not serving it"
    )


@pytest.mark.asyncio
async def test_existing_miss_log_grain_refreshed_with_filter_dims():
    """An existing miss log with an incomplete grain (no filter dims) must
    have its grain updated to the current full grain on upsert."""
    existing = _existing_miss(
        grain=["region"],        # incomplete — missing filter dim 'manager'
        measures=["base_amount"],
        candidate_aggregate_id=None,
    )

    bound = _make_bound(
        select_dims=["region"],
        filter_dims=["manager"],
        group_by=["region"],
        measures=["base_amount"],
    )
    db = _FakeDBWithExisting(existing)
    await log_query_miss(db, bound, "no_aggregate")

    assert "manager" in existing.requested_grain, (
        "existing miss log grain must be refreshed to include filter dimensions"
    )
    assert "manager" in existing.requested_dimensions


@pytest.mark.asyncio
async def test_existing_miss_log_measures_refreshed():
    """Measures on an existing miss log are updated to reflect the current query."""
    existing = _existing_miss(
        grain=["region"],
        measures=["base_amount"],
    )

    bound = _make_bound(
        select_dims=["region"],
        filter_dims=[],
        group_by=["region"],
        measures=["base_amount", "transaction_count"],
    )
    db = _FakeDBWithExisting(existing)
    await log_query_miss(db, bound, "no_aggregate")

    assert "transaction_count" in existing.requested_measures
