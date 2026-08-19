"""Bug-6252 (AKA Fable:F-017-05) — semi-additive KPIs must never silently SUM.

Re-verified against current code 2026-08-03. The finding's literal wording
("`avg` silently becomes SUM") is STALE — ``_semi_additive_expr`` gained
explicit avg/min/max branches after the finding was written. The ROOT CAUSE it
described is still live in a different shape and is what this module pins:

  * ``non_additive_agg`` was an unvalidated free-form ``Optional[str]`` while
    every sibling enum on the KPI model was gated; and
  * ``_semi_additive_expr`` ended in an unconditional ``return SUM(col)``.

So ANY token the compiler did not recognise SUMmed a column of per-period
balances. The reachable shapes were not hypothetical typos: the canonical
MEASURE-level vocabulary (``last_non_empty`` / ``first_non_empty`` /
``avg_of_children`` / ``by_account``) is a different vocabulary from the
compiler's, so an operator reusing the tokens the Measures panel taught them got
a silent SUM for every one — a daily-balance KPI reporting the sum of every
day's balance instead of the closing balance.

Test escape: no test ever passed a non-canonical ``non_additive_agg``; the
suite only exercised ``first``/``last``. Guard: this module.
Tier: T1 producer/consumer contract.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.schemas.domains.governance_advanced import (
    _KPI_NON_ADDITIVE_AGGS,
    KPICreate,
    KPIUpdate,
)
from src.kpi_compiler import (
    CompilerContext,
    _build_semi_additive_sql,
    _canonical_non_additive_agg,
    _semi_additive_expr,
)

pytestmark = pytest.mark.unit


def _ctx(agg: str | None) -> CompilerContext:
    return CompilerContext(
        model_slug="balances",
        time_column="as_of_date",
        at_grain="month",
        non_additive_agg=agg,
    )


class TestSchemaGate:
    @pytest.mark.parametrize("token", sorted(_KPI_NON_ADDITIVE_AGGS))
    def test_canonical_tokens_are_accepted(self, token) -> None:
        assert KPICreate(name="k", non_additive_agg=token).non_additive_agg == token

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("last_non_empty", "last"),
            ("first_non_empty", "first"),
            ("avg_of_children", "avg"),
            ("average", "avg"),
            ("LAST", "last"),
            ("  Avg  ", "avg"),
        ],
    )
    def test_measure_vocabulary_is_bridged_not_silently_summed(
        self, given, expected
    ) -> None:
        assert KPICreate(name="k", non_additive_agg=given).non_additive_agg == expected
        assert KPIUpdate(non_additive_agg=given).non_additive_agg == expected

    @pytest.mark.parametrize("token", ["by_account", "last_value", "closing", "total"])
    def test_unsupported_tokens_are_rejected_on_create_and_update(self, token) -> None:
        """``by_account`` has no KPI-compiler equivalent (it needs a per-row
        account-type column the compiler never reads), so it must fail rather
        than quietly become a sum."""
        with pytest.raises(ValueError, match="non_additive_agg"):
            KPICreate(name="k", non_additive_agg=token)
        with pytest.raises(ValueError, match="non_additive_agg"):
            KPIUpdate(non_additive_agg=token)

    def test_none_is_a_noop(self) -> None:
        assert KPICreate(name="k").non_additive_agg is None
        assert KPIUpdate().non_additive_agg is None


class TestCompilerBackstop:
    """Rows persisted before the gate (or written by any non-Pydantic writer)
    must fail loud in the compiler rather than reach the SUM fallback."""

    def test_legacy_persisted_token_is_bridged(self) -> None:
        assert _canonical_non_additive_agg("last_non_empty") == "last"

    @pytest.mark.parametrize("token", ["by_account", "closing_balance", ""])
    def test_unsupported_persisted_token_raises(self, token) -> None:
        with pytest.raises(ValueError, match="not supported"):
            _canonical_non_additive_agg(token)

    @pytest.mark.parametrize("token", ["by_account", "closing_balance"])
    def test_build_semi_additive_sql_refuses_rather_than_summing(self, token) -> None:
        with pytest.raises(ValueError, match="not supported"):
            _build_semi_additive_sql("SUM(balance)", _ctx(token))

    def test_semi_additive_expr_has_no_silent_sum_fallback(self) -> None:
        """The defect was structural: the function ENDED in ``return SUM(...)``,
        so every unhandled token produced plausible-looking wrong SQL."""
        with pytest.raises(ValueError):
            _semi_additive_expr("by_account", "inner_val", "as_of_date")


class TestCompiledSqlIsCorrectPerReducer:
    """Known-shape assertions: the reducer the modeller chose must be the
    aggregate that actually appears in the SQL."""

    def test_avg_reduces_with_avg_not_sum(self) -> None:
        sql = _build_semi_additive_sql("SUM(balance)", _ctx("avg"))
        assert "AVG(inner_val)" in sql
        assert "SUM(inner_val)" not in sql

    def test_min_and_max_reduce_with_their_own_aggregate(self) -> None:
        assert "MIN(inner_val)" in _build_semi_additive_sql("SUM(b)", _ctx("min"))
        assert "MAX(inner_val)" in _build_semi_additive_sql("SUM(b)", _ctx("max"))

    def test_explicit_sum_still_sums(self) -> None:
        """``sum`` is a legitimate explicit choice — the fix removes the silent
        fallback, not the option."""
        assert "SUM(inner_val)" in _build_semi_additive_sql("SUM(b)", _ctx("sum"))

    @pytest.mark.parametrize("agg,direction", [("last", "DESC"), ("first", "ASC")])
    def test_first_last_use_ordered_limit_one(self, agg, direction) -> None:
        sql = _build_semi_additive_sql("SUM(balance)", _ctx(agg))
        assert f"ORDER BY \"as_of_date\" {direction}" in sql
        assert "LIMIT 1" in sql

    def test_bridged_measure_token_compiles_as_its_canonical_reducer(self) -> None:
        """The exact wrong-numbers path: a KPI carrying the measure-level
        ``avg_of_children`` used to SUM. It must now AVG."""
        sql = _build_semi_additive_sql("SUM(balance)", _ctx("avg_of_children"))
        assert "AVG(inner_val)" in sql
        assert "SUM(inner_val)" not in sql


class TestUnsupportedTokenDoesNotReachThePythonFallback:
    """Bug-6252 INTEGRATION half (deep-review finding 2).

    The compiler's fail-loud is worthless if the caller downgrades it. The only
    production caller of ``compile_expression`` wraps it in
    ``except Exception: return _COMPILER_UNSUPPORTED``, which routes the KPI to
    the PYTHON evaluator — and that evaluator applies NO ``at_grain`` bucketing
    and NO semi-additive reduction, so it serves the plain model-wide SUM. The
    exact silent wrong number the raise exists to prevent, merely produced one
    frame further out: a daily-balance KPI over 100/120/90 reports 310 instead
    of the 90 closing balance, in a cell that looks like a legitimate number.

    Test escape: every other test in this module asserts at compiler-FUNCTION
    level; none crossed ``_evaluate_expression_via_sql``. That is the
    unit-test-passes-but-production-path-fails gap CLAUDE.md names.
    Guard: this class. Tier: T1 producer/consumer contract.
    """

    @pytest.mark.asyncio
    async def test_unsupported_token_fails_the_kpi_instead_of_summing(self) -> None:
        from src.api import kpis as kpis_mod

        called: list[str] = []

        async def _must_not_execute(*a, **kw):
            called.append("router")
            raise AssertionError("no SQL may be executed for an unservable KPI")

        with patch.object(kpis_mod, "_execute_via_router", _must_not_execute):
            result = await kpis_mod._evaluate_expression_via_sql(
                'measure("balance")',
                uuid.uuid4(),
                "modelx",
                "tok",
                {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                SimpleNamespace(calc_agg_mode="semi_additive"),
                time_column="as_of_date",
                at_grain="day",
                non_additive_agg="by_account",
            )

        assert result is kpis_mod._GUARD_REFUSED, (
            "an unsupported non_additive_agg must FAIL the KPI closed; "
            "_COMPILER_UNSUPPORTED routes it to the Python evaluator, which "
            "returns the un-reduced SUM"
        )
        assert result is not kpis_mod._COMPILER_UNSUPPORTED
        assert called == [], "no query may run for a KPI that cannot be compiled"

    @pytest.mark.asyncio
    async def test_a_bridged_token_still_compiles_and_serves(self) -> None:
        """Control: the fail-closed leg must not swallow a legitimate KPI."""
        from src.api import kpis as kpis_mod

        captured: list[str] = []

        async def _fake_router(model_id, sql, bearer, **kw):
            # Real contract: _execute_via_router returns a result mapping the
            # caller reads "rows" out of.
            captured.append(sql)
            return {"rows": [{"value": 90.0}]}

        with patch.object(kpis_mod, "_execute_via_router", _fake_router):
            result = await kpis_mod._evaluate_expression_via_sql(
                'measure("balance")',
                uuid.uuid4(),
                "modelx",
                "tok",
                {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                SimpleNamespace(calc_agg_mode="semi_additive"),
                time_column="as_of_date",
                at_grain="day",
                non_additive_agg="last_non_empty",
            )

        assert not kpis_mod._is_evaluation_failure(result)
        assert result is not kpis_mod._COMPILER_UNSUPPORTED
        assert result == 90.0
        # And the bridged token compiled to the CLOSING-balance reduction, not
        # a sum: last -> ORDER BY <time> DESC LIMIT 1.
        assert captured, "the control case must actually reach the router"
        assert "ORDER BY" in captured[0] and "LIMIT 1" in captured[0], captured[0]

    @pytest.mark.asyncio
    async def test_semi_additive_reduction_survives_the_time_intelligence_path(
        self,
    ) -> None:
        """Bug-6252 residual (deep-review R3 finding 2): the DECOMPOSED-TI
        branch of ``_evaluate_expression_via_sql`` is taken BEFORE the
        semi-additive CompilerContext is built, and ``_evaluate_ti_decomposed``
        never receives ``at_grain`` / ``non_additive_agg``. A semi-additive KPI
        carrying a TI expression therefore served the plain per-period SUM:
        with daily balances 100/120/90 the month contributes 310 instead of the
        90 closing balance -- silently, with no error.

        Either disposition is acceptable: reduce correctly, or refuse. Serving
        an un-reduced per-period SUM is not.

        Test escape: every earlier Bug-6252 test asserted through the NON-TI
        branch. Guard: this test. Tier: T1.
        """
        from src.api import kpis as kpis_mod

        captured: list[str] = []

        async def _fake_router(model_id, sql, bearer, **kw):
            captured.append(sql)
            return {"rows": [{"value": 100.0}]}

        with patch.object(kpis_mod, "_execute_via_router", _fake_router):
            result = await kpis_mod._evaluate_expression_via_sql(
                'trailing_sum(measure("balance"), 3, "month")',
                uuid.uuid4(), "modelx", "tok",
                {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                SimpleNamespace(calc_agg_mode="semi_additive"),
                time_column="as_of_date",
                at_grain="day",
                non_additive_agg="last",
            )

        if kpis_mod._is_evaluation_failure(result):
            assert captured == [], (
                "the KPI was failed closed, so no per-period query should have "
                "run"
            )
            return
        assert captured, "the TI path must reach the router in the served case"
        for sql in captured:
            upper = sql.upper()
            assert "GROUP BY" in upper or "LIMIT 1" in upper, (
                "the decomposed per-period query applied NO semi-additive "
                "reduction -- it is a plain SUM over every row in the period, "
                f"the exact Bug-6252 wrong number: {sql}"
            )

    @pytest.mark.asyncio
    async def test_a_plain_ti_kpi_without_a_semi_additive_grain_still_serves(
        self,
    ) -> None:
        """Control: the fail-closed leg must only fire for the semi-additive
        combination, never for an ordinary time-intelligence KPI."""
        from src.api import kpis as kpis_mod

        captured: list[str] = []

        async def _fake_router(model_id, sql, bearer, **kw):
            captured.append(sql)
            return {"rows": [{"value": 100.0}]}

        with patch.object(kpis_mod, "_execute_via_router", _fake_router):
            result = await kpis_mod._evaluate_expression_via_sql(
                'trailing_sum(measure("balance"), 3, "month")',
                uuid.uuid4(), "modelx", "tok",
                {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                SimpleNamespace(calc_agg_mode="automatic"),
                time_column="as_of_date",
            )

        assert not kpis_mod._is_evaluation_failure(result)
        assert captured, "an ordinary TI KPI must still reach the router"


class TestSemiAdditiveNeverReachesThePythonEvaluator:
    """Bug-6252 (deep-review R4 finding 1 / R5 finding 3 -- the missing guard).

    R4 replaced per-exit guarding with one ``_python_fallback()`` helper and
    shipped NO test for it. The R5 reviewer proved the gap by mutation:
    reverting the helper's refusal left all 34 tests in this module green,
    because they exercise the R3 guards (``KPIUnsupportedAggregationError``,
    the decomposed-TI check) rather than the three routes R4 actually closed.

    These are those three routes. The Python evaluator applies no reduction and
    emits a bare ``SELECT SUM(balance) FROM model``, so a semi-additive KPI
    reaching it reports 310 for daily balances 100/120/90 where the closing
    balance is 90.

    Test escape: R4 added no coverage for its own headline fix.
    Guard: this class. Tier: T1.
    """

    _ROUTES = [
        ("nested time intelligence", 'pct_change(measure("balance"), "month") * 100'),
        ("kpi cross-reference", 'kpi("other_kpi") + measure("balance")'),
        ("uncompilable expression", "measure(("),
    ]

    @staticmethod
    async def _evaluate(expression, **kw):
        from src.api import kpis as kpis_mod

        called: list[str] = []

        async def _router(model_id, sql, bearer, **_kw):
            called.append(sql)
            return {"rows": [{"value": 310.0}]}

        with patch.object(kpis_mod, "_execute_via_router", _router):
            result = await kpis_mod._evaluate_expression_via_sql(
                expression, uuid.uuid4(), "modelx", "tok",
                {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                SimpleNamespace(calc_agg_mode="semi_additive"),
                time_column="as_of_date", **kw,
            )
        return result, called

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label,expression", _ROUTES)
    async def test_semi_additive_never_reaches_the_python_evaluator(
        self, label, expression,
    ) -> None:
        from src.api import kpis as kpis_mod

        result, called = await self._evaluate(
            expression, at_grain="month", non_additive_agg="last",
        )
        assert result is not kpis_mod._COMPILER_UNSUPPORTED, (
            f"{label}: a KPI carrying at_grain/non_additive_agg was handed to "
            "the Python evaluator, which applies NO reduction and emits a bare "
            "SELECT SUM(balance) FROM model -- 310 instead of the 90 closing "
            "balance"
        )
        assert result is kpis_mod._GUARD_REFUSED, f"{label}: expected fail-closed"
        assert called == [], f"{label}: no SQL may run for an unservable KPI"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label,expression", _ROUTES)
    async def test_control_without_a_semi_additive_grain_still_falls_back(
        self, label, expression,
    ) -> None:
        """The fail-closed leg must not cost an ordinary KPI its fallback."""
        from src.api import kpis as kpis_mod

        result, _ = await self._evaluate(expression)
        assert result is kpis_mod._COMPILER_UNSUPPORTED, (
            f"{label}: an ordinary KPI must keep its Python-evaluator fallback"
        )


def test_a_half_configured_semi_additive_kpi_is_still_reduced() -> None:
    """Bug-6252 (deep-review R5 finding 1). The compiler dispatched on
    ``non_additive_agg AND at_grain`` while every guard states the invariant as
    OR, so a KPI with only ONE of the two set -- which nothing validates
    against and which the REST API, project import and the agent all accept --
    fell through to ``SELECT SUM(balance) FROM model``: 310 for daily balances
    100/120/90 where the closing balance is 90. Tier: T1.
    """
    from src.kpi_compiler import CompilerContext, compile_expression

    for kwargs in (
        {"non_additive_agg": "last"},
        {"at_grain": "month"},
    ):
        ctx = CompilerContext(
            model_slug="modelx", time_column="as_of_date", **kwargs
        )
        sql = compile_expression('measure("balance")', ctx).sql
        upper = sql.upper()
        assert "GROUP BY" in upper or "LIMIT 1" in upper, (
            f"a half-configured semi-additive KPI ({kwargs}) was compiled to an "
            f"un-reduced aggregate: {sql}"
        )


class TestShareRankIsNotSwallowedByTheSemiAdditiveBranch:
    """Bug-6252 (deep-review R6 finding 1) -- guard against the lane's OWN
    regression.

    R5 widened the compiler's semi-additive dispatch from
    ``non_additive_agg AND at_grain`` to OR. The share/rank branch is the very
    next ``elif`` in that chain, so a share-of-total KPI that also carries
    ``at_grain`` stopped reaching it: the share KPI was compiled as a plain
    semi-additive scalar instead. A share over region with at_grain='day' went
    from the correct 220/310 = 71% to a bare 90 -- displayed as a percentage,
    that reads as 9000%.

    Two mutually exclusive reductions of the same rows cannot both apply, so
    the combination is refused rather than silently resolved by elif order.

    Test escape: the R5 change was mutation-proven only against the shape it
    targeted; nothing exercised a share/rank KPI carrying a grain.
    Guard: this class. Tier: T1.
    """

    @staticmethod
    async def _evaluate(**kw):
        from src.api import kpis as kpis_mod

        called: list[str] = []

        async def _router(model_id, sql, bearer, **_kw):
            called.append(sql)
            return {"rows": [{"value": 0.71}]}

        with patch.object(kpis_mod, "_execute_via_router", _router):
            result = await kpis_mod._evaluate_expression_via_sql(
                'measure("revenue")', uuid.uuid4(), "modelx", "tok",
                {"revenue": SimpleNamespace(name="revenue", default_agg="sum")},
                SimpleNamespace(calc_agg_mode="automatic"),
                time_column="as_of_date",
                base_expression='measure("revenue")',
                **kw,
            )
        return result, called

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "grain_kwargs",
        [{"at_grain": "day"}, {"non_additive_agg": "last"},
         {"at_grain": "day", "non_additive_agg": "last"}],
    )
    async def test_share_plus_a_semi_additive_grain_is_refused(
        self, grain_kwargs,
    ) -> None:
        from src.api import kpis as kpis_mod

        result, called = await self._evaluate(
            share_type="share_of_total", share_dimension="region",
            **grain_kwargs,
        )
        assert result is kpis_mod._GUARD_REFUSED, (
            "a share/rank KPI carrying a semi-additive grain was served: the "
            "semi-additive branch captures it before share/rank is reached, so "
            "a percentage is replaced by a raw scalar (71% -> 90)"
        )
        assert called == [], "no SQL may run for an ambiguous KPI"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "grain_kwargs",
        [{"at_grain": "day"}, {"non_additive_agg": "last"}],
    )
    async def test_aggregate_of_aggregate_plus_a_grain_is_refused(
        self, grain_kwargs,
    ) -> None:
        """Deep-review R7 finding 2: aggregate-of-aggregate is the FIRST branch
        of the same compiler chain and swallows the semi-additive reduction
        identically -- inner=sum / outer=avg at inner_grain=month drops both
        ``at_grain`` and ``last``. Unlike ``at_grain`` it IS settable in the
        shipped KPI wizard, so guarding only share/rank left the more reachable
        half of the same clash open."""
        from src.api import kpis as kpis_mod

        result, called = await self._evaluate(
            inner_agg="sum", outer_agg="avg", inner_grain="month",
            **grain_kwargs,
        )
        assert result is kpis_mod._GUARD_REFUSED, (
            "an aggregate-of-aggregate KPI carrying a semi-additive grain was "
            "served: the agg-of-agg branch wins and the reduction vanishes"
        )
        assert called == [], "no SQL may run for an ambiguous KPI"

    @pytest.mark.asyncio
    async def test_a_plain_aggregate_of_aggregate_kpi_still_serves(self) -> None:
        """Control: agg-of-agg without a grain is an ordinary, valid KPI."""
        from src.api import kpis as kpis_mod

        result, called = await self._evaluate(
            inner_agg="sum", outer_agg="avg", inner_grain="month",
        )
        assert not kpis_mod._is_evaluation_failure(result)
        assert called, "an ordinary agg-of-agg KPI must still reach the router"

    @pytest.mark.asyncio
    async def test_a_plain_share_kpi_without_a_grain_still_serves(self) -> None:
        """Control: the guard must fire ONLY on the clash."""
        from src.api import kpis as kpis_mod

        result, called = await self._evaluate(
            share_type="share_of_total", share_dimension="region",
        )
        assert not kpis_mod._is_evaluation_failure(result)
        assert called, "an ordinary share KPI must still reach the router"
        assert "region" in called[0], called[0]

    @pytest.mark.asyncio
    async def test_a_plain_semi_additive_kpi_without_share_still_serves(self) -> None:
        """Control: the R5 OR-widening must survive this guard."""
        from src.api import kpis as kpis_mod

        result, called = await self._evaluate(at_grain="day")
        assert not kpis_mod._is_evaluation_failure(result)
        assert called, "an ordinary semi-additive KPI must still reach the router"
        upper = called[0].upper()
        assert "GROUP BY" in upper or "LIMIT 1" in upper, called[0]


class TestGuardRefusalIsDistinctFromExecutionFailure:
    """Bug-8568 (deep-review R6 finding 7).

    ``_EVALUATION_ERROR`` meant BOTH "a correctness guard refused" and "the
    router call raised" (a timeout, a 5xx, the row-security 403). R4 made the
    ad-hoc endpoint return 400 for the whole union, having reasoned only about
    the first member. That (a) made the Bug-8449 deny-all branch unreachable on
    ad-hoc while /evaluate and batch still consulted the sink -- the exact
    three-endpoint drift Bug-8449 removed -- and (b) told a modeller whose
    preview merely timed out to go debug a correct expression.

    A refusal is deterministic and about the DEFINITION; an execution failure
    is transient and about the RUN. They are now separate sentinels.

    Test escape: nothing distinguished the two members of an overloaded
    sentinel. Guard: this class. Tier: T1.
    """

    def test_the_two_sentinels_are_distinct_objects(self) -> None:
        from src.api import kpis as kpis_mod

        assert kpis_mod._GUARD_REFUSED is not kpis_mod._EVALUATION_ERROR

    def test_both_still_count_as_an_evaluation_failure(self) -> None:
        """Every consumer that only needs 'no value was produced' must keep
        treating them alike, or a refusal would fall through unhandled."""
        from src.api import kpis as kpis_mod

        assert kpis_mod._is_evaluation_failure(kpis_mod._GUARD_REFUSED)
        assert kpis_mod._is_evaluation_failure(kpis_mod._EVALUATION_ERROR)
        assert not kpis_mod._is_evaluation_failure(None)
        assert not kpis_mod._is_evaluation_failure(0.0)
        assert not kpis_mod._is_evaluation_failure(kpis_mod._COMPILER_UNSUPPORTED)

    @pytest.mark.asyncio
    async def test_a_router_failure_is_not_reported_as_a_definition_error(
        self,
    ) -> None:
        """The execution-failure leg must keep returning _EVALUATION_ERROR, so
        the ad-hoc endpoint routes it to the fallback (and thus to the Bug-8449
        deny-all check) instead of the 400."""
        from src.api import kpis as kpis_mod

        async def _boom(model_id, sql, bearer, **kw):
            raise TimeoutError("router timed out")

        with patch.object(kpis_mod, "_execute_via_router", _boom):
            result = await kpis_mod._evaluate_expression_via_sql(
                'measure("balance")', uuid.uuid4(), "modelx", "tok",
                {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                SimpleNamespace(calc_agg_mode="automatic"),
                time_column="as_of_date",
            )

        assert result is kpis_mod._EVALUATION_ERROR, (
            "a transient router failure must stay an EXECUTION failure"
        )
        assert result is not kpis_mod._GUARD_REFUSED, (
            "a timeout is not a definition problem and must not produce the "
            "ad-hoc 400 that tells the modeller to check their expression"
        )

    @pytest.mark.asyncio
    async def test_a_guard_refusal_is_not_reported_as_an_execution_failure(
        self,
    ) -> None:
        """Mirror of the above: the refusal legs must NOT look transient, or the
        ad-hoc endpoint would retry them through the un-reduced Python path."""
        from src.api import kpis as kpis_mod

        async def _router(model_id, sql, bearer, **kw):
            raise AssertionError("no SQL may run for a refused KPI")

        with patch.object(kpis_mod, "_execute_via_router", _router):
            result = await kpis_mod._evaluate_expression_via_sql(
                'measure("balance")', uuid.uuid4(), "modelx", "tok",
                {"balance": SimpleNamespace(name="balance", default_agg="sum")},
                SimpleNamespace(calc_agg_mode="semi_additive"),
                time_column="as_of_date",
                at_grain="day", non_additive_agg="by_account",
            )

        assert result is kpis_mod._GUARD_REFUSED
        assert result is not kpis_mod._EVALUATION_ERROR
