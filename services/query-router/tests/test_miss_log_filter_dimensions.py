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
    *,
    passthrough: bool = False,
    bound_filter_names: list[str] | None = None,
) -> BoundQuery:
    lq = MagicMock()
    lq.query_fingerprint = "deadbeef" * 8
    lq.raw_query = "SELECT d, COUNT(1) FROM t WHERE f = 'x' GROUP BY d"
    lq.grain = group_by
    # Real LogicalQuery flags are bools; an ordinary filtered query is not a
    # passthrough. Set them explicitly so the F-009-20 logger gate reads a real
    # bool rather than a truthy MagicMock attribute.
    lq.has_complex_sql = passthrough
    lq.has_unresolvable_where = passthrough

    model = MagicMock()
    model.id = "model-1"

    # F-009-20: the logger keeps a passthrough filter only when its name is a
    # bound model reference. ``bound_filter_names`` (default: every filter dim)
    # is the set the binder resolved; anything outside it is an unbound
    # passthrough name that must be dropped from telemetry.
    known = set(bound_filter_names) if bound_filter_names is not None else set(filter_dims)
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_dimensions=[_dim(d) for d in select_dims],
        resolved_measures=[_measure(m) for m in measures],
        resolved_filters=[
            LogicalFilter(dimension_name=d, operator="eq", value="x")
            for d in filter_dims
        ],
        resolved_dimensions_by_name={d: _dim(d) for d in (set(select_dims) | known)},
    )


class _FakeDB:
    """Minimal async DB stub for the upsert-based log_query_miss.

    Bug-6726 changed log_query_miss from SELECT-then-INSERT to
    INSERT ... ON CONFLICT DO UPDATE.  This stub captures the INSERT
    values from the compiled statement so test assertions can inspect
    them.
    """

    def __init__(self) -> None:
        self.added: list = []
        self._insert_params: dict = {}

    async def execute(self, stmt):
        # Extract compiled params from the upsert statement
        try:
            from sqlalchemy.dialects.postgresql import dialect as pg_dialect
            compiled = stmt.compile(dialect=pg_dialect())
            params = dict(compiled.params)
            if params and not self._insert_params:
                self._insert_params = params
        except Exception:
            pass

        result = MagicMock()
        # Return occurrence_count=1 (insert path, no variant merge needed)
        _row = SimpleNamespace(
            id="fake-id",
            occurrence_count=1,
            predicate_variants_json=None,
            # Bug-8071: RETURNING now also yields the per-reason history.
            miss_reason_counts_json=None,
            miss_reason="no_aggregate",
            first_seen_at="2026-01-01T00:00:00+00:00",
        )
        result.fetchone.return_value = _row
        result.scalar_one_or_none.return_value = None
        return result

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass

    @property
    def captured(self) -> SimpleNamespace:
        """Return a namespace of captured INSERT values for test assertions."""
        return SimpleNamespace(**self._insert_params)


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

    obj = db.captured
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

    obj = db.captured
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

    obj = db.captured
    assert set(obj.requested_dimensions) == {"product", "region", "manager"}
    assert set(obj.requested_grain) == {"product", "region", "manager"}


@pytest.mark.asyncio
async def test_required_grain_overrides_derived_grain():
    """F-009-01 / F-030-03 / F-101-06 / F-102-03 (Bug-8773 / Bug-9099): when the
    execute path forwards the matcher's ``required_grain``, the logged
    ``requested_grain`` is EXACTLY that set — including DISTINCT / DATE_TRUNC
    substitutions the matcher applied — not the logger's re-derived
    ``lq.grain | filter_dims``."""
    bound = _make_bound(
        select_dims=["region"],
        filter_dims=["manager"],
        group_by=["region"],
        measures=["base_amount"],
    )
    db = _FakeDB()
    # Matcher required a DATE_TRUNC substitution grain the raw query does not name.
    await log_query_miss(
        db, bound, "no_aggregate",
        required_grain=["order_month", "region", "manager"],
    )

    obj = db.captured
    assert set(obj.requested_grain) == {"order_month", "region", "manager"}, (
        "requested_grain must equal the matcher's required_grain when forwarded"
    )


@pytest.mark.asyncio
async def test_required_grain_none_falls_back_to_derived():
    """When the matcher early-returned (required_grain None), the logger keeps
    its documented lq.grain | filter_dims fallback."""
    bound = _make_bound(
        select_dims=["region"],
        filter_dims=["manager"],
        group_by=["region"],
        measures=["base_amount"],
    )
    db = _FakeDB()
    await log_query_miss(db, bound, "no_aggregate", required_grain=None)

    obj = db.captured
    assert set(obj.requested_grain) == {"region", "manager"}


@pytest.mark.asyncio
async def test_passthrough_unbound_filter_dropped_from_telemetry():
    """F-009-20 (Bug-8775 sibling): a passthrough / complex-SQL query keeps raw
    unresolved filter names for the SOURCE rewrite, but those names must not
    reach optimizer telemetry — a pocket built from a column the model does not
    have can never match. The bound filter is kept; the unbound one is dropped
    from predicates and grain."""
    bound = _make_bound(
        select_dims=["region"],
        filter_dims=["region", "ghost_col"],
        group_by=["region"],
        measures=["base_amount"],
        passthrough=True,
        bound_filter_names=["region"],  # ghost_col is NOT a model column
    )
    db = _FakeDB()
    await log_query_miss(db, bound, "aggregate_skip:passthrough", required_grain=None)

    obj = db.captured
    predicate_cols = {p["column_name"] for p in obj.predicates_json}
    assert predicate_cols == {"region"}, (
        "unbound passthrough filter 'ghost_col' must be dropped from predicates"
    )
    assert "ghost_col" not in obj.requested_grain
    assert "ghost_col" not in obj.requested_dimensions


@pytest.mark.asyncio
async def test_non_passthrough_keeps_all_bound_filters():
    """F-009-20 guard: the drop applies ONLY on the passthrough path. An ordinary
    filtered query's filters are all bound by the binder and must be kept."""
    bound = _make_bound(
        select_dims=["region"],
        filter_dims=["manager"],
        group_by=["region"],
        measures=["base_amount"],
        passthrough=False,
    )
    db = _FakeDB()
    await log_query_miss(db, bound, "no_aggregate")

    obj = db.captured
    predicate_cols = {p["column_name"] for p in obj.predicates_json}
    assert predicate_cols == {"manager"}
    assert "manager" in obj.requested_grain


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

    obj = db.captured
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

    obj = db.captured
    assert obj.requested_dimensions.count("region") == 1, (
        "Dimension appearing in both SELECT and WHERE must not be duplicated"
    )


# ---------------------------------------------------------------------------
# Re-queue tests — existing miss log must be refreshed and un-linked
# ---------------------------------------------------------------------------

import uuid as _uuid


class _FakeDBConflict:
    """DB stub that simulates the ON CONFLICT (upsert update) path.

    Returns occurrence_count=2 from the upsert RETURNING so
    log_query_miss enters the variant-merge branch and fires a
    follow-up UPDATE.
    """

    def __init__(self) -> None:
        self._insert_params: dict = {}
        self._update_params: dict = {}
        self._stmts: list = []
        self._call_count = 0

    async def execute(self, stmt):
        self._call_count += 1
        self._stmts.append(stmt)
        try:
            from sqlalchemy.dialects.postgresql import dialect as pg_dialect
            compiled = stmt.compile(dialect=pg_dialect())
            params = dict(compiled.params)
            if self._call_count == 1:
                self._insert_params = params
            else:
                self._update_params = params
        except Exception:
            pass

        result = MagicMock()
        # First execute -> upsert RETURNING (conflict path)
        if self._call_count == 1:
            _row = SimpleNamespace(
                id=_uuid.uuid4(),
                occurrence_count=2,  # conflict: existing row incremented
                predicate_variants_json=[{
                    "predicate_set_hash": "old",
                    "predicates": [],
                    "occurrence_count": 1,
                    "last_seen_at": "2026-01-01T00:00:00+00:00",
                }],
                # Bug-8071: RETURNING now also yields the per-reason history.
                miss_reason_counts_json=[{
                    "reason": "no_aggregate",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-01-01T00:00:00+00:00",
                    "last_seen_at": "2026-01-01T00:00:00+00:00",
                }],
                miss_reason="no_aggregate",
                first_seen_at="2026-01-01T00:00:00+00:00",
            )
            result.fetchone.return_value = _row
        return result

    async def commit(self) -> None:
        pass

    @property
    def captured(self) -> SimpleNamespace:
        """Return a namespace of captured INSERT values for test assertions.

        The INSERT and ON CONFLICT SET clauses use the same computed values
        for requested_dimensions, requested_grain, requested_measures, and
        candidate_aggregate_id (None in SET).
        """
        return SimpleNamespace(**self._insert_params)


@pytest.mark.asyncio
async def test_existing_miss_log_candidate_aggregate_id_cleared():
    """When an existing miss log has candidate_aggregate_id set but the query
    still misses, the ON CONFLICT SET clause must reset candidate_aggregate_id
    to NULL so the optimizer re-queues the pattern."""
    bound = _make_bound(
        select_dims=["region"],
        filter_dims=["manager"],
        group_by=["region"],
        measures=["base_amount"],
    )
    db = _FakeDBConflict()
    await log_query_miss(db, bound, "no_aggregate")

    # Verify the upsert's ON CONFLICT SET clause includes
    # candidate_aggregate_id (meaning it resets it on every conflict).
    # The compiled SQL for the first execute() call is the full upsert.
    upsert_stmt = db._stmts[0] if db._stmts else None
    assert upsert_stmt is not None, "No statement captured"
    from sqlalchemy.dialects.postgresql import dialect as pg_dialect
    sql_text = str(upsert_stmt.compile(dialect=pg_dialect()))
    # The ON CONFLICT ... DO UPDATE SET ... portion must contain
    # candidate_aggregate_id to reset it on re-queue.
    set_clause = sql_text.split("DO UPDATE SET")[1] if "DO UPDATE SET" in sql_text else ""
    assert "candidate_aggregate_id" in set_clause, (
        "candidate_aggregate_id must be reset in ON CONFLICT SET — "
        "the linked aggregate is not serving the query"
    )


@pytest.mark.asyncio
async def test_existing_miss_log_grain_refreshed_with_filter_dims():
    """An existing miss log with an incomplete grain (no filter dims) must
    have its grain updated to the current full grain on upsert."""
    bound = _make_bound(
        select_dims=["region"],
        filter_dims=["manager"],
        group_by=["region"],
        measures=["base_amount"],
    )
    db = _FakeDBConflict()
    await log_query_miss(db, bound, "no_aggregate")

    obj = db.captured
    assert "manager" in obj.requested_grain, (
        "existing miss log grain must be refreshed to include filter dimensions"
    )
    assert "manager" in obj.requested_dimensions


@pytest.mark.asyncio
async def test_existing_miss_log_measures_refreshed():
    """Measures on an existing miss log are updated to reflect the current query."""
    bound = _make_bound(
        select_dims=["region"],
        filter_dims=[],
        group_by=["region"],
        measures=["base_amount", "transaction_count"],
    )
    db = _FakeDBConflict()
    await log_query_miss(db, bound, "no_aggregate")

    obj = db.captured
    assert "transaction_count" in obj.requested_measures
