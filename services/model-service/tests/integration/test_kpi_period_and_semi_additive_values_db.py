"""KNOWN-VALUE guards for the KPI wrong-numbers lane (L7), on real Postgres.

Every assertion here is a hand-computed figure, not a shape check. The bugs in
scope all return a plausible number, so "the SQL contains DATE_TRUNC" is not
evidence: only running the emitted SQL against real rows and comparing the
result to an arithmetic the reader can check by eye proves the fix.

Covered:

  * **Bug-9233 / Bug-9385 (CRITICAL, wrong numbers)** — a ``period_to_date``
    (YTD) KPI must return the CURRENT-YEAR total, never the all-time SUM. All
    four boundary cases are seeded: the first day of the period, a mid-period
    day, the anchor day itself (the upper boundary), the day AFTER the anchor,
    and two prior-period rows that must be EXCLUDED. An all-time SUM passes any
    test that does not check exclusion, so exclusion is asserted explicitly.

  * **Bug-8570 (wrong numbers)** — a semi-additive KPI combined with time
    intelligence must reduce EACH period to its closing/average value BEFORE
    the time-intelligence arithmetic. Un-reduced, the trailing sum of three
    months of daily balances is the sum of every row (370); reduced, it is the
    sum of the three monthly CLOSING balances (140).

  * **Bug-8573 (wrong numbers)** — ``first``/``last`` must reduce per period.

The KPI's model slug becomes the FROM table, so the test creates a table with
that exact name in an isolated schema and executes the compiled SQL verbatim.
``CURRENT_DATE`` is pinned to a fixed anchor so the expected figures are exact;
an unpinned pass then proves the real ``CURRENT_DATE`` form behaves the same.

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_kpi_period_and_semi_additive_values_db.py -v
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)

# The KPI's model slug — also the physical table name in the isolated schema,
# so the compiled SQL runs verbatim.
_SLUG = "modelx"
_TIME_COL = "as_of_date"

# Pinned evaluation day. Fixed so every expected figure below is exact.
_ANCHOR = date(2026, 5, 15)

# (as_of_date, base_amount, balance). ``base_amount`` drives the YTD figures;
# ``balance`` drives the semi-additive figures.
_ROWS: list[tuple[date, float, float]] = [
    # --- prior period: MUST be excluded by a YTD window ---
    (date(2025, 1, 2), 700.0, 0.0),    # same day-of-year as an included row
    (date(2025, 12, 31), 1000.0, 0.0),  # the day before the period starts
    # --- current period ---
    (date(2026, 1, 1), 10.0, 10.0),    # first day of the period (boundary)
    (date(2026, 1, 31), 0.0, 20.0),    # January closing balance
    (date(2026, 2, 28), 0.0, 30.0),    # February closing balance
    (date(2026, 3, 1), 0.0, 100.0),
    (date(2026, 3, 2), 0.0, 120.0),
    (date(2026, 3, 3), 0.0, 90.0),     # March closing balance
    # Mid period. Deliberately in APRIL, not March: the semi-additive cases
    # below reduce over the March window, and a fourth March row carrying a
    # zero balance would change the closing/average figures under test.
    (date(2026, 4, 10), 20.0, 0.0),
    (date(2026, 5, 15), 30.0, 0.0),    # the anchor day itself (boundary)
    # --- after the anchor: MUST be excluded ---
    (date(2026, 5, 16), 500.0, 0.0),
]

# Hand-computed. YTD window is [2026-01-01, 2026-05-16):
#   included  10 (Jan 1) + 20 (Apr 10) + 30 (May 15) = 60
#   excluded  700 (2025-01-02), 1000 (2025-12-31), 500 (2026-05-16)
_EXPECTED_YTD_BASE_AMOUNT = 60.0
# Every base_amount row, for contrast. An all-time SUM returns this.
_EXPECTED_ALL_TIME_BASE_AMOUNT = 2260.0

# Daily balances 100 / 120 / 90 in March 2026.
_EXPECTED_MARCH_CLOSING_BALANCE = 90.0
_EXPECTED_MARCH_SUM_OF_ROWS = 310.0  # the wrong number a missing reduction gives


def _pin_today(sql: str) -> str:
    """Substitute the pinned anchor for CURRENT_DATE.

    Only the evaluation DAY changes; every window boundary the compiler emitted
    is left exactly as written, which is the behaviour under test.
    """
    return sql.replace("CURRENT_DATE", f"DATE '{_ANCHOR.isoformat()}'")


@asynccontextmanager
async def _seeded_schema(rows=None) -> AsyncIterator[object]:
    """Seed an isolated schema with *rows* (default ``_ROWS``).

    ``rows`` is explicit rather than global so a test needing a different shape
    (a NULL period to carry forward across; a prior period whose row SUM differs
    from its closing balance) does not perturb the figures every other test in
    this module hand-computed from ``_ROWS``.

    ``balance`` is NULLABLE: a semi-additive period that aggregates to NULL is
    exactly what carry-forward exists to fill, so the schema must be able to
    express one.
    """
    rows = _ROWS if rows is None else rows
    schema = f"kpi_known_values_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.execute(
            text(
                f'CREATE TABLE "{_SLUG}" ('
                f'"{_TIME_COL}" date NOT NULL, '
                '"base_amount" numeric NOT NULL, '
                '"balance" numeric)'
            )
        )
        for d, amount, bal in rows:
            await conn.execute(
                text(
                    f'INSERT INTO "{_SLUG}" ("{_TIME_COL}", "base_amount", '
                    '"balance") VALUES (:d, :a, :b)'
                ),
                {"d": d, "a": amount, "b": bal},
            )
    await boot.dispose()

    # ``server_settings`` pins search_path at connection startup for EVERY
    # pooled connection, so a checkout that skipped a connect-event hook cannot
    # silently resolve the model table in the wrong schema.
    engine = create_async_engine(
        _DB_URL,
        future=True,
        connect_args={"server_settings": {"search_path": schema}},
    )

    try:
        yield engine
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


async def _scalar(engine, sql: str) -> float | None:
    async with engine.connect() as conn:
        row = (await conn.execute(text(sql))).first()
    if row is None or row[0] is None:
        return None
    return float(row[0])


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _DB_URL,
        reason=(
            "Set TESSALLITE_VERSIONING_DB_URL (or "
            "TESSALLITE_IMPORTER_REHYDRATION_DB_URL) to a reachable Postgres"
        ),
    ),
]


class TestPeriodToDateReturnsTheCurrentPeriodNotAllTime:
    """Bug-9233 / Bug-9385: a YTD KPI must never serve the all-time SUM."""

    @pytest.mark.asyncio
    async def test_ytd_known_value_and_prior_period_exclusion(self):
        from src.kpi_compiler import CompilerContext, compile_scalar_kpi_sql

        ctx = CompilerContext(model_slug=_SLUG, time_column=_TIME_COL)
        sql = compile_scalar_kpi_sql(
            'SUM("base_amount")', ctx,
            ti_type="period_to_date", ti_grain="year",
        )
        assert sql is not None

        async with _seeded_schema() as engine:
            ytd = await _scalar(engine, _pin_today(sql))
            all_time = await _scalar(
                engine, f'SELECT SUM("base_amount") FROM "{_SLUG}"'
            )

        # First day of the period (10) + mid period (20) + the anchor day
        # itself (30). The prior-year rows (700, 1000) and the day after the
        # anchor (500) are excluded.
        assert ytd == _EXPECTED_YTD_BASE_AMOUNT
        assert all_time == _EXPECTED_ALL_TIME_BASE_AMOUNT
        # The regression this exists to catch: YTD silently equal to all-time.
        assert ytd != all_time

    @pytest.mark.asyncio
    async def test_ytd_excludes_the_day_after_the_anchor(self):
        """The upper bound is ``< CURRENT_DATE + 1 day``: the anchor day is IN,
        the next day is OUT. Proven by moving the anchor back one day, which
        must drop exactly the 30 booked on 2026-05-15."""
        from src.kpi_compiler import CompilerContext, compile_scalar_kpi_sql

        ctx = CompilerContext(model_slug=_SLUG, time_column=_TIME_COL)
        sql = compile_scalar_kpi_sql(
            'SUM("base_amount")', ctx,
            ti_type="period_to_date", ti_grain="year",
        )
        day_before = sql.replace("CURRENT_DATE", "DATE '2026-05-14'")

        async with _seeded_schema() as engine:
            ytd_before = await _scalar(engine, day_before)

        assert ytd_before == _EXPECTED_YTD_BASE_AMOUNT - 30.0  # 30.0

    @pytest.mark.asyncio
    async def test_ytd_with_the_real_current_date_still_excludes_prior_years(self):
        """The UNPINNED ``CURRENT_DATE`` form must behave the same.

        The fixture is re-based onto the wall-clock year rather than read from
        it. Written against the literal 2026 rows, this test goes VACUOUS the
        moment the clock leaves 2026: every row falls in a prior year, the
        expected total collapses to 0, and ``ytd == 0`` passes for a query that
        returns nothing at all — including one that is broken. Re-basing keeps a
        real included row, a real excluded prior-year row, and a real excluded
        future row in every calendar year the suite ever runs in.
        """
        from src.kpi_compiler import CompilerContext, compile_scalar_kpi_sql

        today = date.today()
        rows = [
            # Prior year — must be EXCLUDED by a year-to-date window.
            (date(today.year - 1, 1, 2), 700.0, 0.0),
            (date(today.year - 1, 12, 31), 1000.0, 0.0),
            # Current year, on or before today — must be INCLUDED.
            (date(today.year, 1, 1), 10.0, 0.0),
            (today, 30.0, 0.0),
            # Tomorrow — must be EXCLUDED.
            (today + timedelta(days=1), 500.0, 0.0),
        ]
        included = 10.0 + 30.0
        all_time = 700.0 + 1000.0 + 10.0 + 30.0 + 500.0

        ctx = CompilerContext(model_slug=_SLUG, time_column=_TIME_COL)
        sql = compile_scalar_kpi_sql(
            'SUM("base_amount")', ctx,
            ti_type="period_to_date", ti_grain="year",
        )
        async with _seeded_schema(rows) as engine:
            ytd = await _scalar(engine, sql)
            actual_all_time = await _scalar(
                engine, f'SELECT SUM("base_amount") FROM "{_SLUG}"',
            )

        assert ytd == included, (
            "the unpinned CURRENT_DATE form does not bound to the current year"
        )
        assert actual_all_time == all_time
        assert ytd != actual_all_time

    @pytest.mark.asyncio
    async def test_month_to_date_is_bounded_to_the_month(self):
        from src.kpi_compiler import CompilerContext, compile_scalar_kpi_sql

        ctx = CompilerContext(model_slug=_SLUG, time_column=_TIME_COL)
        sql = compile_scalar_kpi_sql(
            'SUM("base_amount")', ctx,
            ti_type="period_to_date", ti_grain="month",
        )
        async with _seeded_schema() as engine:
            mtd = await _scalar(engine, _pin_today(sql))

        # May 2026 window is [2026-05-01, 2026-05-16): only the 30 on May 15.
        assert mtd == 30.0


class TestSemiAdditiveReducesPerPeriod:
    """Bug-8573: ``last`` must return the closing balance, not the sum."""

    @pytest.mark.asyncio
    async def test_last_returns_the_closing_balance_not_the_row_sum(self):
        from src.kpi_compiler import CompilerContext, compile_expression

        ctx = CompilerContext(
            model_slug=_SLUG,
            time_column=_TIME_COL,
            at_grain="day",
            non_additive_agg="last",
            where_clause=(
                f'"{_TIME_COL}" >= DATE \'2026-03-01\' '
                f'AND "{_TIME_COL}" < DATE \'2026-04-01\''
            ),
        )
        sql = compile_expression('measure("balance")', ctx).sql

        async with _seeded_schema() as engine:
            value = await _scalar(engine, sql)

        # Daily balances 100 / 120 / 90 -> the closing balance is 90.
        assert value == _EXPECTED_MARCH_CLOSING_BALANCE
        assert value != _EXPECTED_MARCH_SUM_OF_ROWS

    @pytest.mark.asyncio
    async def test_avg_reduces_over_the_daily_buckets(self):
        from src.kpi_compiler import CompilerContext, compile_expression

        ctx = CompilerContext(
            model_slug=_SLUG,
            time_column=_TIME_COL,
            at_grain="day",
            non_additive_agg="avg",
            where_clause=(
                f'"{_TIME_COL}" >= DATE \'2026-03-01\' '
                f'AND "{_TIME_COL}" < DATE \'2026-04-01\''
            ),
        )
        sql = compile_expression('measure("balance")', ctx).sql

        async with _seeded_schema() as engine:
            value = await _scalar(engine, sql)

        # (100 + 120 + 90) / 3
        assert value == pytest.approx(310.0 / 3.0)


class TestTimeIntelligenceAppliesTheReductionPerPeriod:
    """Bug-8570: reduce each period BEFORE the time-intelligence arithmetic."""

    @pytest.mark.asyncio
    async def test_trailing_sum_of_monthly_closing_balances(self):
        """Three months of daily balances.

        January closes at 20, February at 30, March at 90 (100/120/90).
        The trailing 3-month sum of CLOSING balances is 20 + 30 + 90 = 140.
        Un-reduced it would be every row in those months:
        10 + 20 + 30 + 100 + 120 + 90 = 370 — the Bug-8570 wrong number.
        """
        from src.api import kpis as kpis_mod

        async with _seeded_schema() as engine:
            issued: list[str] = []

            async def _router(model_id, sql, bearer, **kw):
                issued.append(sql)
                async with engine.connect() as conn:
                    rows = (
                        await conn.execute(text(_pin_today(sql)))
                    ).mappings().all()
                return {"rows": [dict(r) for r in rows]}

            with patch.object(kpis_mod, "_execute_via_router", _router):
                value = await kpis_mod._evaluate_expression_via_sql(
                    'trailing_sum(measure("balance"), 3, "month")',
                    uuid.uuid4(), _SLUG, "tok",
                    {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                    SimpleNamespace(calc_agg_mode="semi_additive"),
                    time_column=_TIME_COL,
                    time_window_end_sql="DATE '2026-04-01'",
                    at_grain="day",
                    non_additive_agg="last",
                )

        assert not kpis_mod._is_evaluation_failure(value), (
            "Bug-8570: the reduction is threaded through the decomposition now, "
            "so this combination must SERVE rather than fail closed"
        )
        assert value == 140.0
        assert value != 370.0
        assert issued, "the decomposed path must reach the router"

    @pytest.mark.asyncio
    async def test_the_same_kpi_without_the_reduction_is_the_row_sum(self):
        """Control: drop the reduction and the number becomes the row sum, which
        is what makes 140 vs 370 a real difference rather than a coincidence."""
        from src.api import kpis as kpis_mod

        async with _seeded_schema() as engine:

            async def _router(model_id, sql, bearer, **kw):
                async with engine.connect() as conn:
                    rows = (
                        await conn.execute(text(_pin_today(sql)))
                    ).mappings().all()
                return {"rows": [dict(r) for r in rows]}

            with patch.object(kpis_mod, "_execute_via_router", _router):
                value = await kpis_mod._evaluate_expression_via_sql(
                    'trailing_sum(measure("balance"), 3, "month")',
                    uuid.uuid4(), _SLUG, "tok",
                    {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                    SimpleNamespace(calc_agg_mode="automatic"),
                    time_column=_TIME_COL,
                    time_window_end_sql="DATE '2026-04-01'",
                )

        assert value == 370.0


# ---------------------------------------------------------------------------
# Bug-9482 — carry-forward
# ---------------------------------------------------------------------------

# A month with NO value to carry into, between two months that have one.
# Daily balances: January closes at 20, February aggregates to NULL, March
# closes at 90 (100 / 120 / 90).
_CARRY_ROWS: list[tuple[date, float, float | None]] = [
    (date(2026, 1, 31), 0.0, 20.0),
    (date(2026, 2, 28), 0.0, None),   # the gap carry-forward must fill
    (date(2026, 3, 1), 0.0, 100.0),
    (date(2026, 3, 2), 0.0, 120.0),
    (date(2026, 3, 3), 0.0, 90.0),
]

# Hand-computed monthly buckets: Jan 20, Feb NULL, Mar 310.
#   filled:   (20 + 20 + 310) / 3 = 116.666...   <- February carries January's 20
#   unfilled: (20 + 310) / 2      = 165.0        <- February is simply skipped
_EXPECTED_MONTHLY_AVG_CARRIED = (20.0 + 20.0 + 310.0) / 3.0
_EXPECTED_MONTHLY_AVG_UNFILLED = (20.0 + 310.0) / 2.0

# Daily buckets: 20, NULL, 100, 120, 90.
#   filled:   (20 + 20 + 100 + 120 + 90) / 5 = 70.0
#   unfilled: (20 + 100 + 120 + 90) / 4      = 82.5
_EXPECTED_DAILY_AVG_CARRIED = 70.0
_EXPECTED_DAILY_AVG_UNFILLED = 82.5


class TestCarryForwardIsExecutableAndFillsTheGap:
    """Bug-9482 — the emission must RUN, and the fill must change the number.

    Every carry-forward KPI used to emit SQL PostgreSQL rejects on EVERY
    version: the fill was wrapped around the AGGREGATE, and the PostgreSQL
    pre-pass turned that into an aggregate inside ``FILTER`` and an aggregate
    ``ORDER BY`` on a window function. Reproduced on 15.19 and 16.15.

    The tests that were supposed to guard this asserted ``"COALESCE" in sql``
    and ``"ARRAY_AGG" in sql`` and never executed anything, so they passed
    against SQL that could not run. Nothing short of executing it is evidence,
    and every assertion below is a figure computed by hand from
    ``_CARRY_ROWS`` — with the un-filled control beside it, so the number is
    evidence about the FILL and not about the arithmetic.

    Test escape: existing coverage ENSHRINED the broken shape.
    Guard: this class + test_kpi_compiler's no-aggregate-inside-a-window
    invariant. Tier: T3 (wrong numbers, db-integration scope).
    """

    @pytest.mark.asyncio
    async def test_monthly_average_carries_the_empty_month_forward(self):
        from src.kpi_compiler import CompilerContext, compile_expression

        def _ctx(carry: bool) -> CompilerContext:
            return CompilerContext(
                model_slug=_SLUG, time_column=_TIME_COL,
                at_grain="month", non_additive_agg="avg", carry_forward=carry,
            )

        carried_sql = compile_expression('measure("balance")', _ctx(True)).sql
        plain_sql = compile_expression('measure("balance")', _ctx(False)).sql

        async with _seeded_schema(_CARRY_ROWS) as engine:
            carried = await _scalar(engine, carried_sql)
            plain = await _scalar(engine, plain_sql)

        assert carried == pytest.approx(_EXPECTED_MONTHLY_AVG_CARRIED)
        assert plain == pytest.approx(_EXPECTED_MONTHLY_AVG_UNFILLED)
        assert carried != plain, (
            "carry_forward changed nothing: the empty February was skipped "
            "rather than carrying January's closing balance forward"
        )

    @pytest.mark.asyncio
    async def test_daily_average_carries_the_empty_day_forward(self):
        """The ``at_grain=day`` shape, whose inner query has a different column
        set (no raw time column) and therefore a different fill scope."""
        from src.kpi_compiler import CompilerContext, compile_expression

        def _ctx(carry: bool) -> CompilerContext:
            return CompilerContext(
                model_slug=_SLUG, time_column=_TIME_COL,
                at_grain="day", non_additive_agg="avg", carry_forward=carry,
            )

        async with _seeded_schema(_CARRY_ROWS) as engine:
            carried = await _scalar(
                engine, compile_expression('measure("balance")', _ctx(True)).sql,
            )
            plain = await _scalar(
                engine, compile_expression('measure("balance")', _ctx(False)).sql,
            )

        assert carried == pytest.approx(_EXPECTED_DAILY_AVG_CARRIED)
        assert plain == pytest.approx(_EXPECTED_DAILY_AVG_UNFILLED)

    @pytest.mark.asyncio
    async def test_closing_balance_with_carry_forward_still_closes_at_90(self):
        """``last`` + carry-forward: the fill must not move the closing balance.

        The reduction already skips NULL rows, so filling them cannot change
        which value is last. A different answer here would mean the fill leaked
        into the reduction.
        """
        from src.kpi_compiler import CompilerContext, compile_expression

        ctx = CompilerContext(
            model_slug=_SLUG, time_column=_TIME_COL,
            at_grain="day", non_additive_agg="last", carry_forward=True,
        )
        async with _seeded_schema(_CARRY_ROWS) as engine:
            value = await _scalar(
                engine, compile_expression('measure("balance")', ctx).sql,
            )

        assert value == _EXPECTED_MARCH_CLOSING_BALANCE  # 90.0

    @pytest.mark.asyncio
    async def test_carry_forward_alone_serves_the_closing_balance(self):
        """``carry_forward`` with NEITHER semi-additive half set.

        A gap-fill needs a period series, and the plain single-aggregate path
        has one row. The compiler routes this through the same bucketed builder
        using its documented half-configuration defaults (bucket by the KPI's
        own time column, reduce with ``last``) — the treatment ``at_grain``
        alone and ``non_additive_agg`` alone already get. The alternative is the
        model-wide SUM of a balance column (330 here), which is the exact wrong
        number the semi-additive machinery exists to prevent.
        """
        from src.kpi_compiler import CompilerContext, compile_expression

        ctx = CompilerContext(
            model_slug=_SLUG, time_column=_TIME_COL, carry_forward=True,
        )
        async with _seeded_schema(_CARRY_ROWS) as engine:
            value = await _scalar(
                engine, compile_expression('measure("balance")', ctx).sql,
            )
            row_sum = await _scalar(
                engine, f'SELECT SUM("balance") FROM "{_SLUG}"',
            )

        assert value == _EXPECTED_MARCH_CLOSING_BALANCE  # 90.0
        assert row_sum == 330.0
        assert value != row_sum

    @pytest.mark.asyncio
    async def test_aggregate_of_aggregate_with_carry_forward_runs_and_fills(self):
        """The third interacting wrap. ``row_first`` is used because every other
        calc_agg_mode emits nested aggregates for aggregate-of-aggregate — a
        SEPARATE, pre-existing defect tracked as Bug-9480, reproduced here at
        base as well as at tip.
        """
        from src.kpi_compiler import CompilerContext, compile_expression

        def _ctx(carry: bool) -> CompilerContext:
            return CompilerContext(
                model_slug=_SLUG, calc_agg_mode="row_first",
                time_column=_TIME_COL, inner_agg="sum", inner_grain="month",
                outer_agg="avg", carry_forward=carry,
            )

        async with _seeded_schema(_CARRY_ROWS) as engine:
            carried = await _scalar(
                engine, compile_expression('measure("balance")', _ctx(True)).sql,
            )
            plain = await _scalar(
                engine, compile_expression('measure("balance")', _ctx(False)).sql,
            )

        assert carried == pytest.approx(_EXPECTED_MONTHLY_AVG_CARRIED)
        assert plain == pytest.approx(_EXPECTED_MONTHLY_AVG_UNFILLED)

    @pytest.mark.asyncio
    async def test_the_business_definition_filter_still_reaches_the_base_scope(self):
        """The fill adds two scopes between the outer reduction and the base
        table. ``_inject_where`` finds the FIRST ``GROUP BY``, so the business
        definition's WHERE must still land on the base table — not on a wrapper
        that does not have the column.
        """
        from src.kpi_compiler import CompilerContext, compile_expression

        ctx = CompilerContext(
            model_slug=_SLUG, time_column=_TIME_COL,
            at_grain="month", non_additive_agg="avg", carry_forward=True,
            where_clause=(
                f'"{_TIME_COL}" >= DATE \'2026-03-01\' '
                f'AND "{_TIME_COL}" < DATE \'2026-04-01\''
            ),
        )
        async with _seeded_schema(_CARRY_ROWS) as engine:
            value = await _scalar(
                engine, compile_expression('measure("balance")', ctx).sql,
            )

        # March only: one bucket, 100 + 120 + 90 = 310.
        assert value == pytest.approx(310.0)


# ---------------------------------------------------------------------------
# Bug-8569 — the target leg must reduce the same way the value leg does
# ---------------------------------------------------------------------------

# February's row SUM (1029) is deliberately far from its CLOSING balance (30),
# so an un-reduced target leg cannot coincidentally produce the right figure.
#
# The anchor is mid-month on purpose. A ``period_to_date`` value compares against
# the prior period TO THE SAME POINT, so the target window is
# [1 February, anchor - 1 month) — every row below sits inside its own window at
# this anchor, and none of the figures depend on a month-end boundary.
_TARGET_LEG_ANCHOR = date(2026, 3, 15)
_TARGET_LEG_ROWS: list[tuple[date, float, float | None]] = [
    (date(2026, 2, 10), 0.0, 500.0),
    (date(2026, 2, 11), 0.0, 499.0),
    (date(2026, 2, 12), 0.0, 30.0),    # February CLOSING balance
    (date(2026, 3, 1), 0.0, 100.0),
    (date(2026, 3, 2), 0.0, 120.0),
    (date(2026, 3, 3), 0.0, 90.0),     # March CLOSING balance
]
_EXPECTED_VALUE_MARCH_CLOSING = 90.0
_EXPECTED_TARGET_FEBRUARY_CLOSING = 30.0
# What an un-reduced target leg serves instead: 500 + 499 + 30.
_UNREDUCED_FEBRUARY_ROW_SUM = 1029.0


async def _evaluate_production_kpi(engine, *, anchor: date, **kpi_overrides):
    """Drive the PRODUCTION single-KPI path with a real-Postgres router.

    Returns ``(response, issued_sql)``. *anchor* pins ``CURRENT_DATE`` so every
    expected figure is exact; only the evaluation DAY is substituted, and every
    window boundary the compiler emitted is left exactly as written.

    The default KPI is an ordinary closing-balance scorecard (value = this
    month's closing balance, target = last month's). *kpi_overrides* replaces
    any field on it, so a caller can drive a different authored configuration
    through the same production entry point rather than a hand-built context.
    """
    from src.api import kpis as kpis_mod
    from src.kpi_evaluator import MeasureValueProvider

    model_id = uuid.uuid4()
    time_dimension_id = uuid.uuid4()
    time_dim = SimpleNamespace(
        id=time_dimension_id, model_id=model_id, is_time_dim=True,
        name=_TIME_COL,
    )

    class _ScalarResult:
        def all(self):
            return []

        def first(self):
            return None

    class _Result:
        def scalars(self):
            return _ScalarResult()

        def all(self):
            return []

        def first(self):
            return None

    class _Db:
        async def get(self, _model, pk):
            return time_dim if pk == time_dimension_id else None

        async def execute(self, *_a, **_k):
            return _Result()

    kpi = SimpleNamespace(
        id=uuid.uuid4(),
        name="Closing balance",
        display_name=None,
        expression='period_to_date(measure("balance"), "month")',
        kpi_type="single_measure",
        calc_agg_mode="semi_additive",
        at_grain="day",
        non_additive_agg="last",
        carry_forward=False,
        inner_agg=None, inner_grain=None, outer_agg=None,
        target_type="expression",
        target_value=None,
        target_measure_id=None,
        target_expression='prior_period(measure("balance"), "month")',
        target_period=None,
        direction="higher_is_better",
        presentation_type=None, presentation_meta=None,
        trend_period="month", trend_threshold=0.01,
        trend_sparkline_periods=12,
        format_token=None, format_custom=None, unit_label=None,
        null_display_value="N/A",
        business_definition=None,
        time_dimension_id=time_dimension_id,
    )
    for key, val in kpi_overrides.items():
        setattr(kpi, key, val)

    issued: list[str] = []

    async def _router(_model_id, sql, _bearer, **_kw):
        issued.append(sql)
        async with engine.connect() as conn:
            pinned = sql.replace("CURRENT_DATE", f"DATE '{anchor.isoformat()}'")
            rows = (await conn.execute(text(pinned))).mappings().all()
        return {"rows": [dict(r) for r in rows]}

    with patch.object(kpis_mod, "_execute_via_router", _router):
        response = await kpis_mod._evaluate_single_kpi(
            kpi, _Db(), model_id, _SLUG, "tok",
            MeasureValueProvider(),
            measure_map={
                "balance": SimpleNamespace(name="balance", default_agg="sum"),
            },
        )
    return response, issued


class TestTargetLegKnownValues:
    """Bug-8569 — value and target must be computed under the SAME rules.

    The registry entry for Bug-8569 is a WRONG-NUMBERS item, and its guards
    asserted SQL SHAPE ("GROUP BY and LIMIT 1 appear in the target SQL"). A
    shape assertion cannot tell 30 from 1029: it passes for any query that
    happens to group and limit. This runs both legs against real rows.

    The KPI is an ordinary closing-balance scorecard: value = this month's
    closing balance, target = last month's. Without the shared reduction the
    target leg emits a plain per-period SUM, so the modeller sees 90 against a
    target of 1029 — attainment reads 9% instead of 300% and the RAG band
    inverts. The headline value is RIGHT and the verdict beside it is wrong,
    which is the harder failure to notice.

    Test escape: every Bug-8569 guard asserted argument lists or SQL shape;
    none compared two numbers. Guard: this class. Tier: T3 (wrong numbers,
    db-integration scope).
    """

    async def _evaluate(self, engine, **kpi_overrides):
        return await _evaluate_production_kpi(
            engine, anchor=_TARGET_LEG_ANCHOR, **kpi_overrides,
        )

    @pytest.mark.asyncio
    async def test_target_is_the_prior_periods_closing_balance_not_its_row_sum(self):
        async with _seeded_schema(_TARGET_LEG_ROWS) as engine:
            response, issued = await self._evaluate(engine)

        assert issued, "the evaluation must reach the router"
        assert response.value == _EXPECTED_VALUE_MARCH_CLOSING, (
            "the VALUE leg no longer serves March's closing balance: "
            f"{response.value}"
        )
        assert response.target == _EXPECTED_TARGET_FEBRUARY_CLOSING, (
            "the TARGET leg was computed WITHOUT the value leg's semi-additive "
            f"reduction: it returned {response.target} where February's "
            f"closing balance is {_EXPECTED_TARGET_FEBRUARY_CLOSING}. "
            f"{_UNREDUCED_FEBRUARY_ROW_SUM} is the un-reduced row sum — "
            "attainment reads 9% instead of 300% and the RAG band inverts "
            "(Bug-8569)"
        )
        assert response.target != _UNREDUCED_FEBRUARY_ROW_SUM

    @pytest.mark.asyncio
    async def test_the_row_sum_really_is_the_wrong_answer(self):
        """Control: the two figures genuinely differ in this data, so the
        assertion above is evidence rather than a coincidence."""
        async with _seeded_schema(_TARGET_LEG_ROWS) as engine:
            february_sum = await _scalar(
                engine,
                f'SELECT SUM("balance") FROM "{_SLUG}" '
                f'WHERE "{_TIME_COL}" >= DATE \'2026-02-01\' '
                f'AND "{_TIME_COL}" < DATE \'2026-02-15\'',
            )
        assert february_sum == _UNREDUCED_FEBRUARY_ROW_SUM
        assert february_sum != _EXPECTED_TARGET_FEBRUARY_CLOSING


# ---------------------------------------------------------------------------
# L7B-01 — carry_forward must reach the DECOMPOSED time-intelligence path
# ---------------------------------------------------------------------------

# A NULL day inside each period, between two days that have a value. Both the
# value period (March) and the target period (February) therefore have a gap,
# so the fill moves the two legs independently and neither figure can come out
# right by coincidence.
_L7B01_ROWS: list[tuple[date, float, float | None]] = [
    (date(2026, 2, 10), 0.0, 500.0),
    (date(2026, 2, 11), 0.0, None),   # the gap carry-forward must fill
    (date(2026, 2, 12), 0.0, 30.0),
    (date(2026, 3, 1), 0.0, 100.0),
    (date(2026, 3, 2), 0.0, None),    # the gap carry-forward must fill
    (date(2026, 3, 3), 0.0, 90.0),
]

# at_grain=day, non_additive_agg=avg. One bucket per day; AVG skips a NULL
# bucket, so the fill is what puts the carried value into the denominator.
#   value  (March)    unfilled (100 + 90) / 2       =  95.0
#                     filled   (100 + 100 + 90) / 3 =  96.666...
#   target (February) unfilled (500 + 30) / 2       = 265.0
#                     filled   (500 + 500 + 30) / 3 = 343.333...
_L7B01_VALUE_UNFILLED = 95.0
_L7B01_VALUE_FILLED = (100.0 + 100.0 + 90.0) / 3.0
_L7B01_TARGET_UNFILLED = 265.0
_L7B01_TARGET_FILLED = (500.0 + 500.0 + 30.0) / 3.0


def _island_col() -> str:
    """The fill scope's island column, read from the PRODUCER."""
    from src.kpi_compiler import _CARRY_FORWARD_ISLAND_COL

    return _CARRY_FORWARD_ISLAND_COL


class TestCarryForwardReachesTheDecomposedTimeIntelligencePath:
    """L7B-01 — the decomposed TI path must carry the fill into EVERY period.

    ``_evaluate_ti_decomposed`` builds one query per period and threads the
    KPI's semi-additive reduction into each of them. It threaded ``at_grain``
    and ``non_additive_agg`` and dropped ``carry_forward``, so a wizard-
    authorable KPI evaluated to a number computed under a configuration the
    modeller did not author — silently, on both legs.

    The predecessor of that threading REFUSED this shape outright
    (``if non_additive_agg or at_grain: return _GUARD_REFUSED``). Replacing an
    honest refusal with a silently unconfigured number is strictly worse, which
    is why every assertion below is on a VALUE rather than on the presence of
    the flag.

    Test escape: every carry-forward guard built a ``CompilerContext`` by hand,
    so none of them could see a field the production caller never put into the
    context IT builds. Guard: this class, which authors the KPI and drives
    ``_evaluate_single_kpi``. Tier: T3 (wrong numbers, db-integration scope).
    """

    _KPI = {
        "calc_agg_mode": "semi_additive",
        "at_grain": "day",
        "non_additive_agg": "avg",
        "expression": 'period_to_date(measure("balance"), "month")',
        "target_expression": 'prior_period(measure("balance"), "month")',
    }

    @pytest.mark.asyncio
    async def test_both_legs_apply_the_authored_carry_forward(self):
        async with _seeded_schema(_L7B01_ROWS) as engine:
            carried, carried_sql = await _evaluate_production_kpi(
                engine, anchor=_TARGET_LEG_ANCHOR,
                carry_forward=True, **self._KPI,
            )
            plain, _ = await _evaluate_production_kpi(
                engine, anchor=_TARGET_LEG_ANCHOR,
                carry_forward=False, **self._KPI,
            )

        # Control: the two configurations genuinely differ in this data, so
        # the assertions below are evidence about the FILL.
        assert plain.value == pytest.approx(_L7B01_VALUE_UNFILLED)
        assert plain.target == pytest.approx(_L7B01_TARGET_UNFILLED)

        assert carried.value == pytest.approx(_L7B01_VALUE_FILLED), (
            "the VALUE leg ignored the authored carry_forward: it returned "
            f"{carried.value} where the filled March average is "
            f"{_L7B01_VALUE_FILLED}. {_L7B01_VALUE_UNFILLED} is the figure a "
            "dropped fill produces (L7B-01)"
        )
        assert carried.target == pytest.approx(_L7B01_TARGET_FILLED), (
            "the TARGET leg ignored the authored carry_forward: it returned "
            f"{carried.target} where the filled February average is "
            f"{_L7B01_TARGET_FILLED}. {_L7B01_TARGET_UNFILLED} is the figure a "
            "dropped fill produces (L7B-01)"
        )

        assert any(_island_col() in sql for sql in carried_sql), (
            "no per-period query carried the fill scope; the decomposed path "
            f"emitted: {carried_sql}"
        )

    @pytest.mark.asyncio
    async def test_carry_forward_alone_still_reduces_per_period(self):
        """``carry_forward`` with NEITHER semi-additive half set, on the
        decomposed path.

        ``compile_expression`` routes this shape through the bucketed builder
        using its documented half-configuration (bucket by the KPI's own time
        column, reduce with ``last``). The per-period query must take the same
        branch, or the number a modeller sees for a carry-forward KPI depends
        on whether its expression happens to carry time intelligence.
        """
        async with _seeded_schema(_L7B01_ROWS) as engine:
            response, issued = await _evaluate_production_kpi(
                engine, anchor=_TARGET_LEG_ANCHOR,
                calc_agg_mode="automatic",
                at_grain=None, non_additive_agg=None, carry_forward=True,
                expression='period_to_date(measure("balance"), "month")',
                target_expression='prior_period(measure("balance"), "month")',
            )
            march_row_sum = await _scalar(
                engine,
                f'SELECT SUM("balance") FROM "{_SLUG}" '
                f'WHERE "{_TIME_COL}" >= DATE \'2026-03-01\'',
            )

        # Bucketed by the KPI's own time column and reduced with ``last``:
        # March closes at 90, February at 30.
        assert response.value == pytest.approx(90.0), (
            f"the per-period query served {response.value}; {march_row_sum} is "
            "the un-reduced per-period SUM the bucketed builder exists to "
            "prevent (L7B-01)"
        )
        assert response.target == pytest.approx(30.0)
        assert any(_island_col() in sql for sql in issued), (
            f"no per-period query carried the fill scope: {issued}"
        )

    @pytest.mark.asyncio
    async def test_the_fill_cannot_reach_outside_the_period_window(self):
        """The documented boundary of the decomposed shape.

        Each per-period query receives its own window as ``where_clause``, so
        the fill orders over the rows INSIDE that window only. February's 500
        can never carry into March, and a leading NULL day stays NULL because
        nothing precedes it in its own window. Asserted so the boundary is a
        gate rather than a docstring: a change that hoisted the fill above the
        window would pull March's average toward February's values.
        """
        rows = [
            (date(2026, 2, 12), 0.0, 500.0),   # last February day, a real value
            (date(2026, 3, 1), 0.0, None),     # leading NULL — nothing to carry
            (date(2026, 3, 2), 0.0, 60.0),
        ]
        async with _seeded_schema(rows) as engine:
            response, _ = await _evaluate_production_kpi(
                engine, anchor=_TARGET_LEG_ANCHOR,
                carry_forward=True, **self._KPI,
            )

        # March buckets are NULL then 60. The leading NULL stays NULL, so the
        # average is 60 — not (500 + 60) / 2 = 280, which is what a fill able
        # to see February would produce.
        assert response.value == pytest.approx(60.0), (
            f"the fill reached outside its own period window: {response.value}"
        )
        assert response.target == pytest.approx(500.0)
