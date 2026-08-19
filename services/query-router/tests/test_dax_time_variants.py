"""
Tests for DAX time-variant hint bridge (Phase 6 Feature 1).

Coverage:
  - parse_dax_to_ir: time_variant_hints populated from parsed_dax
  - parse_dax_to_ir: hints absent when parsed_dax not provided
  - parse_dax_to_ir: non-hint DAX is unaffected
  - rewrite_for_source: TOTALYTD hint (ytd) dispatches to variant emitter
  - rewrite_for_source: SAMEPERIODLASTYEAR hint (prior_year) dispatches to variant emitter
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import attach_fixture_deployed_shape

from src.parsing.dax_normalizer import parse_dax_to_ir
from src.ir.logical_query import LogicalQuery, BoundQuery
from shared.semantic.time_variants_sql import VariantSql

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUMMARIZE_DAX = (
    "EVALUATE SUMMARIZECOLUMNS("
    "    Calendar[Year],"
    '    "Revenue", [Revenue]'
    ")"
)


def _make_dax_bound_query(
    measure_name: str = "Revenue",
    dim_name: str = "Year",
    variant_hints: dict[str, str] | None = None,
) -> BoundQuery:
    """Build a minimal BoundQuery for a DAX query with optional time_variant_hints."""
    model = types.SimpleNamespace(
        id="m-1",
        slug="testmodel",
        display_name="Test Model",
        deployed_version_id="v1",
    )
    m = types.SimpleNamespace(
        id="meas-1",
        name=measure_name,
        default_agg="sum",
        is_additive=True,
        measure_type="standard",
        expression=None,
        calc_agg_mode=None,
        semi_additive_behavior=None,
        variant_kind=None,
        variant_n=None,
        source_column_id="col-1",
        user_defined_attribute_id=None,
        calendar_model_table_id=None,
    )
    d = types.SimpleNamespace(
        id="dim-1",
        name=dim_name,
        source_column_id="col-2",
        user_defined_attribute_id=None,
        calc_expression=None,
        dimension_kind="time",
        is_time_dim=True,
        time_grain="year",
    )
    lq = LogicalQuery(
        model_id="m-1",
        protocol="dax",
        raw_query=_SUMMARIZE_DAX,
        requested_measures=[measure_name],
        requested_dimensions=[dim_name],
        filters=[],
        grain=[dim_name],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="abc123",
        time_variant_hints=variant_hints,
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[m],
        resolved_dimensions=[d],
        resolved_filters=[],
    )


def _make_derived_time_bound_query(
    *,
    derived_name: str,
    derived_expr: str,
    time_grain: str,
    variant_kind: str,
) -> BoundQuery:
    model = types.SimpleNamespace(
        id="m-1",
        slug="testmodel",
        display_name="Test Model",
        deployed_version_id="v1",
    )
    measure = types.SimpleNamespace(
        id="meas-1",
        name=f"Revenue {variant_kind}",
        default_agg="sum",
        is_additive=True,
        measure_type="standard",
        expression=None,
        calc_agg_mode=None,
        semi_additive_behavior=None,
        variant_kind=variant_kind,
        variant_n=None,
        source_column_id="col-revenue",
        user_defined_attribute_id=None,
        calendar_model_table_id=None,
    )
    derived_dim = types.SimpleNamespace(
        id=f"dim-{derived_name}",
        name=derived_name,
        source_column_id=None,
        user_defined_attribute_id=None,
        calc_expression=derived_expr,
        dimension_kind="time",
        is_time_dim=True,
        time_grain=time_grain,
        hierarchy_id="hier-calendar",
    )
    base_date_dim = types.SimpleNamespace(
        id="dim-business-date",
        name="business_date",
        source_column_id="col-business-date",
        user_defined_attribute_id=None,
        calc_expression=None,
        dimension_kind="time",
        is_time_dim=True,
        time_grain="day",
        hierarchy_id="hier-calendar",
    )
    lq = LogicalQuery(
        model_id="m-1",
        protocol="jdbc",
        raw_query=(
            f'SELECT {derived_name}, "{measure.name}" '
            f"FROM testmodel GROUP BY {derived_name}"
        ),
        requested_measures=[measure.name],
        requested_dimensions=[derived_name],
        filters=[],
        grain=[derived_name],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="derived-time",
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[measure],
        resolved_dimensions=[derived_dim],
        resolved_filters=[],
        resolved_dimensions_by_name={
            derived_dim.name: derived_dim,
            base_date_dim.name: base_date_dim,
        },
    )


def _make_year_quarter_prior_year_query(
    grain_order: tuple[str, str],
) -> BoundQuery:
    model = types.SimpleNamespace(
        id="m-1",
        slug="testmodel",
        display_name="Test Model",
        deployed_version_id="v1",
    )
    measure = types.SimpleNamespace(
        id="meas-1",
        name="Revenue prior_year",
        default_agg="sum",
        is_additive=True,
        measure_type="standard",
        expression=None,
        calc_agg_mode=None,
        semi_additive_behavior=None,
        variant_kind="prior_year",
        variant_n=None,
        source_column_id="col-revenue",
        user_defined_attribute_id=None,
        calendar_model_table_id=None,
    )
    dims = {
        "business_date_year": types.SimpleNamespace(
            id="dim-year",
            name="business_date_year",
            source_column_id=None,
            user_defined_attribute_id=None,
            calc_expression="EXTRACT(YEAR FROM sale_date)",
            dimension_kind="time",
            is_time_dim=True,
            time_grain="year",
            hierarchy_id="hier-calendar",
        ),
        "business_date_quarter": types.SimpleNamespace(
            id="dim-quarter",
            name="business_date_quarter",
            source_column_id=None,
            user_defined_attribute_id=None,
            calc_expression="EXTRACT(QUARTER FROM sale_date)",
            dimension_kind="time",
            is_time_dim=True,
            time_grain="quarter",
            hierarchy_id="hier-calendar",
        ),
        "business_date": types.SimpleNamespace(
            id="dim-business-date",
            name="business_date",
            source_column_id="col-business-date",
            user_defined_attribute_id=None,
            calc_expression=None,
            dimension_kind="time",
            is_time_dim=True,
            time_grain="day",
            hierarchy_id="hier-calendar",
        ),
    }
    lq = LogicalQuery(
        model_id="m-1",
        protocol="jdbc",
        raw_query=(
            f'SELECT {", ".join(grain_order)}, "{measure.name}" '
            f'FROM testmodel GROUP BY {", ".join(grain_order)}'
        ),
        requested_measures=[measure.name],
        requested_dimensions=list(grain_order),
        filters=[],
        grain=list(grain_order),
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="year-quarter-prior-year",
    )
    return BoundQuery(
        logical_query=lq,
        model=model,
        resolved_measures=[measure],
        resolved_dimensions=[dims[name] for name in grain_order],
        resolved_filters=[],
        resolved_dimensions_by_name=dims,
    )


def _derived_time_db() -> AsyncMock:
    revenue_col = types.SimpleNamespace(
        id="col-revenue",
        column_name="revenue_amount",
        model_table_id="tbl-sales",
    )
    date_col = types.SimpleNamespace(
        id="col-business-date",
        column_name="sale_date",
        model_table_id="tbl-sales",
        data_type="date",
    )
    table = types.SimpleNamespace(
        id="tbl-sales",
        physical_name="demo.sales",
        alias="base",
        table_type="fact",
        source_id="src-1",
    )
    join_res = MagicMock()
    join_res.scalars.return_value.all.return_value = []

    async def _db_execute(stmt):
        text = str(stmt)
        if "hierarchy_definitions" in text.lower():
            r = MagicMock()
            r.first.return_value = ("standard", None)
            r.all.return_value = [("standard", None)]
            return r
        if "ModelColumn" in text or "model_column" in text.lower():
            r = MagicMock()
            r.scalars.return_value.all.return_value = [revenue_col, date_col]
            return r
        if "ModelTable" in text or "model_table" in text.lower():
            r = MagicMock()
            r.scalars.return_value.all.return_value = [table]
            return r
        if "Join" in text or "join" in text.lower():
            return join_res
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        r.scalar_one_or_none.return_value = None
        r.all.return_value = []
        r.first.return_value = None
        return r

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_db_execute)
    db.get = AsyncMock(return_value=None)
    return db


# ---------------------------------------------------------------------------
# parse_dax_to_ir — hint propagation
# ---------------------------------------------------------------------------

class TestDaxNormalizerHints:
    def test_time_variant_hints_populated_from_parsed_dax(self):
        """Hints in parsed_dax are applied to the returned LogicalQuery."""
        parsed_dax = {"time_variant_hints": {"Revenue": "ytd"}}
        lq = parse_dax_to_ir(_SUMMARIZE_DAX, "m-1", parsed_dax=parsed_dax)
        assert lq.time_variant_hints == {"Revenue": "ytd"}

    def test_time_variant_hints_absent_without_parsed_dax(self):
        """Without parsed_dax, time_variant_hints is None."""
        lq = parse_dax_to_ir(_SUMMARIZE_DAX, "m-1")
        assert lq.time_variant_hints is None

    def test_empty_hints_dict_not_applied(self):
        """Empty hints dict in parsed_dax leaves time_variant_hints None."""
        parsed_dax = {"time_variant_hints": {}}
        lq = parse_dax_to_ir(_SUMMARIZE_DAX, "m-1", parsed_dax=parsed_dax)
        assert lq.time_variant_hints is None

    def test_non_hint_dax_unaffected(self):
        """A plain SUMMARIZECOLUMNS without hints produces no variant hints."""
        plain_dax = (
            "EVALUATE SUMMARIZECOLUMNS("
            "    Sales[Region],"
            '    "Revenue", [Revenue]'
            ")"
        )
        lq = parse_dax_to_ir(plain_dax, "m-1")
        assert lq.time_variant_hints is None
        assert "Region" in lq.requested_dimensions
        assert "Revenue" in lq.requested_measures

    def test_multiple_hints_propagated(self):
        """All entries from parsed_dax.time_variant_hints are preserved."""
        parsed_dax = {
            "time_variant_hints": {
                "Revenue": "ytd",
                "Sales": "prior_year",
            }
        }
        lq = parse_dax_to_ir(_SUMMARIZE_DAX, "m-1", parsed_dax=parsed_dax)
        assert lq.time_variant_hints == {"Revenue": "ytd", "Sales": "prior_year"}

    def test_parsed_dax_without_hints_key_ignored(self):
        """parsed_dax without time_variant_hints key leaves hints None."""
        parsed_dax = {"dimensions": ["Year"], "measures": ["Revenue"]}
        lq = parse_dax_to_ir(_SUMMARIZE_DAX, "m-1", parsed_dax=parsed_dax)
        assert lq.time_variant_hints is None


# ---------------------------------------------------------------------------
# rewrite_for_source — variant dispatch via time_variant_hints
# ---------------------------------------------------------------------------

class TestRewriterVariantDispatch:
    """
    Verify that the rewriter uses time_variant_hints when the ORM measure
    has no variant_kind set. Both the YTD (TOTALYTD) and prior-year
    (SAMEPERIODLASTYEAR) paths must reach emit_variant_expression.
    """

    @pytest.mark.asyncio
    async def test_totalytd_hint_dispatches_to_ytd_variant(self):
        """TOTALYTD hint → variant_kind='ytd' → emit_variant_expression called."""
        bound = _make_dax_bound_query(
            measure_name="Revenue",
            dim_name="Year",
            variant_hints={"Revenue": "ytd"},
        )

        _col = types.SimpleNamespace(
            id="col-1",
            column_name="revenue_amount",
            model_table_id="tbl-1",
        )
        _time_col = types.SimpleNamespace(
            id="col-2",
            column_name="sale_date",
            model_table_id="tbl-1",
            # Bug-3607: the variant date anchor must resolve to a DATE/TIMESTAMP
            # column. A realistic time-dim source column is DATE-typed.
            data_type="date",
        )
        _table = types.SimpleNamespace(
            id="tbl-1",
            physical_name="demo.sales",
            alias="base",
            table_type="fact",
            source_id="src-1",
        )
        _join_res = MagicMock()
        _join_res.scalars.return_value.all.return_value = []

        async def _db_execute(stmt):
            text = str(stmt)
            if "ModelColumn" in text or "model_column" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_col, _time_col]
                return r
            if "ModelTable" in text or "model_table" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_table]
                return r
            if "Join" in text or "join" in text.lower():
                return _join_res
            if "DataSource" in text or "data_source" in text.lower():
                r = MagicMock()
                r.scalar_one_or_none.return_value = None
                return r
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            r.scalar_one_or_none.return_value = None
            return r

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_db_execute)
        db.get = AsyncMock(return_value=None)

        with patch(
            "src.rewrite.source_sql.emit_variant_expression",
        ) as mock_emit:
            mock_emit.return_value = VariantSql(sql="FAKE_YTD_SQL", referenced_calendar_keys=frozenset())
            from src.rewrite.query_rewriter import rewrite_for_source
            await attach_fixture_deployed_shape(bound, db)
            sql = await rewrite_for_source(bound, db)

        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][0] == "ytd", f"Expected variant_kind='ytd', got {call_args[0][0]!r}"

    @pytest.mark.asyncio
    async def test_sameperiodlastyear_hint_dispatches_to_prior_year_variant(self):
        """SAMEPERIODLASTYEAR hint → variant_kind='prior_year' → emit called."""
        bound = _make_dax_bound_query(
            measure_name="Sales",
            dim_name="Date",
            variant_hints={"Sales": "prior_year"},
        )

        _col = types.SimpleNamespace(
            id="col-1",
            column_name="sales_amount",
            model_table_id="tbl-1",
        )
        _time_col = types.SimpleNamespace(
            id="col-2",
            column_name="order_date",
            model_table_id="tbl-1",
            # Bug-3607: variant date anchor must resolve to a DATE column.
            data_type="date",
        )
        _table = types.SimpleNamespace(
            id="tbl-1",
            physical_name="demo.orders",
            alias="base",
            table_type="fact",
            source_id="src-1",
        )
        _join_res = MagicMock()
        _join_res.scalars.return_value.all.return_value = []

        async def _db_execute(stmt):
            text = str(stmt)
            if "ModelColumn" in text or "model_column" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_col, _time_col]
                return r
            if "ModelTable" in text or "model_table" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_table]
                return r
            if "Join" in text or "join" in text.lower():
                return _join_res
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            r.scalar_one_or_none.return_value = None
            return r

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_db_execute)
        db.get = AsyncMock(return_value=None)

        with patch(
            "src.rewrite.source_sql.emit_variant_expression",
        ) as mock_emit:
            mock_emit.return_value = VariantSql(sql="FAKE_PRIOR_YEAR_SQL", referenced_calendar_keys=frozenset())
            from src.rewrite.query_rewriter import rewrite_for_source
            await attach_fixture_deployed_shape(bound, db)
            sql = await rewrite_for_source(bound, db)

        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][0] == "prior_year", (
            f"Expected variant_kind='prior_year', got {call_args[0][0]!r}"
        )

    @pytest.mark.asyncio
    async def test_no_hints_no_variant_dispatch(self):
        """Without time_variant_hints, variant dispatch is not triggered."""
        bound = _make_dax_bound_query(
            measure_name="Revenue",
            dim_name="Year",
            variant_hints=None,
        )

        _col = types.SimpleNamespace(
            id="col-1",
            column_name="revenue_amount",
            model_table_id="tbl-1",
        )
        _time_col = types.SimpleNamespace(
            id="col-2",
            column_name="sale_date",
            model_table_id="tbl-1",
            # Bug-3607: the variant date anchor must resolve to a DATE/TIMESTAMP
            # column. A realistic time-dim source column is DATE-typed.
            data_type="date",
        )
        _table = types.SimpleNamespace(
            id="tbl-1",
            physical_name="demo.sales",
            alias="base",
            table_type="fact",
            source_id="src-1",
        )
        _join_res = MagicMock()
        _join_res.scalars.return_value.all.return_value = []

        async def _db_execute(stmt):
            text = str(stmt)
            if "ModelColumn" in text or "model_column" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_col, _time_col]
                return r
            if "ModelTable" in text or "model_table" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_table]
                return r
            if "Join" in text or "join" in text.lower():
                return _join_res
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            r.scalar_one_or_none.return_value = None
            return r

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_db_execute)
        db.get = AsyncMock(return_value=None)

        with patch(
            "src.rewrite.source_sql.emit_variant_expression",
        ) as mock_emit:
            mock_emit.return_value = "SHOULD_NOT_CALL"
            from src.rewrite.query_rewriter import rewrite_for_source
            await attach_fixture_deployed_shape(bound, db)
            await rewrite_for_source(bound, db)

        mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# Unmocked DAX YTD — proves calendar preflight + real emission
# ---------------------------------------------------------------------------

class TestDaxYtdUnmockedEmission:
    """Phase 9 acceptance: DAX time_variant_hints exercises the full
    calendar preflight and real emit_variant_expression without patches."""

    @pytest.mark.asyncio
    async def test_ytd_hint_emits_real_variant_sql(self):
        """A DAX hint of ytd with a hierarchy calendar_type='standard'
        must emit real YTD SQL through the unpatched emitter."""
        bound = _make_dax_bound_query(
            measure_name="Revenue",
            dim_name="Year",
            variant_hints={"Revenue": "ytd"},
        )

        _col = types.SimpleNamespace(
            id="col-1",
            column_name="revenue_amount",
            model_table_id="tbl-1",
        )
        _time_col = types.SimpleNamespace(
            id="col-2",
            column_name="sale_date",
            model_table_id="tbl-1",
            # Bug-3607: the variant date anchor must resolve to a DATE/TIMESTAMP
            # column. A realistic time-dim source column is DATE-typed.
            data_type="date",
        )
        _table = types.SimpleNamespace(
            id="tbl-1",
            physical_name="demo.sales",
            alias="base",
            table_type="fact",
            source_id="src-1",
        )
        _join_res = MagicMock()
        _join_res.scalars.return_value.all.return_value = []

        _hierarchy_id = "hier-1"

        _src_col = types.SimpleNamespace(
            id="col-2", model_table_id="tbl-1",
        )

        async def _db_execute(stmt):
            text = str(stmt)
            if "hierarchy_definitions" in text.lower():
                r = MagicMock()
                r.first.return_value = ("standard", None)
                r.all.return_value = [("standard", None)]
                return r
            if "ModelColumn" in text or "model_column" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_col, _time_col]
                return r
            if "ModelTable" in text or "model_table" in text.lower():
                r = MagicMock()
                r.scalars.return_value.all.return_value = [_table]
                return r
            if "Join" in text or "join" in text.lower():
                return _join_res
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            r.scalar_one_or_none.return_value = None
            r.all.return_value = []
            r.first.return_value = None
            return r

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_db_execute)
        db.get = AsyncMock(return_value=_src_col)

        from src.rewrite.query_rewriter import rewrite_for_source
        await attach_fixture_deployed_shape(bound, db)
        sql = await rewrite_for_source(bound, db)

        assert "SUM" in sql.upper(), f"Expected SUM in emitted SQL: {sql}"
        assert "YEAR" in sql.upper() or "EXTRACT" in sql.upper(), (
            f"Expected YTD period logic in SQL: {sql}"
        )


def _variant_render_db() -> AsyncMock:
    """Minimal DB mock that lets ``rewrite_for_source`` reach the variant
    dispatch block in source_sql (fact table + revenue/date columns, a
    standard calendar). Shared by the count_distinct variant guard tests."""
    _col = types.SimpleNamespace(
        id="col-1", column_name="revenue_amount", model_table_id="tbl-1",
    )
    _time_col = types.SimpleNamespace(
        id="col-2", column_name="sale_date", model_table_id="tbl-1",
        data_type="date",
    )
    _table = types.SimpleNamespace(
        id="tbl-1", physical_name="demo.sales", alias="base",
        table_type="fact", source_id="src-1",
    )
    _src_col = types.SimpleNamespace(id="col-2", model_table_id="tbl-1")

    async def _db_execute(stmt):
        text = str(stmt)
        if "hierarchy_definitions" in text.lower():
            r = MagicMock()
            r.first.return_value = ("standard", None)
            r.all.return_value = [("standard", None)]
            return r
        if "ModelColumn" in text or "model_column" in text.lower():
            r = MagicMock()
            r.scalars.return_value.all.return_value = [_col, _time_col]
            return r
        if "ModelTable" in text or "model_table" in text.lower():
            r = MagicMock()
            r.scalars.return_value.all.return_value = [_table]
            return r
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        r.scalar_one_or_none.return_value = None
        r.all.return_value = []
        r.first.return_value = None
        return r

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_db_execute)
    db.get = AsyncMock(return_value=_src_col)
    return db


class TestCountDistinctCumulationGuard:
    """Bug-6229: count_distinct is non-additive, so cumulation/moving-window
    variants (which SUM the per-period distinct counts) must fail loud rather
    than emit an inflated running total. Lag/parallel-period variants stay
    allowed. Exercises the real guard in source_sql (not a constant check)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", ["ytd", "qtd", "mtd", "trailing_n", "moving_avg_n"])
    async def test_count_distinct_cumulation_variant_rejected(self, variant):
        from src.ir.logical_query import SemanticBindingError
        from src.rewrite.query_rewriter import rewrite_for_source

        bound = _make_dax_bound_query(
            measure_name="Distinct Customers",
            dim_name="Year",
            variant_hints={"Distinct Customers": variant},
        )
        bound.resolved_measures[0].default_agg = "count_distinct"

        with pytest.raises(SemanticBindingError) as exc:
            await attach_fixture_deployed_shape(bound, _variant_render_db())
            await rewrite_for_source(bound, _variant_render_db())
        assert "count_distinct" in str(exc.value).lower() or "distinct count" in str(exc.value).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", ["prior_year", "prior_quarter", "lag"])
    async def test_count_distinct_parallel_period_variant_allowed(self, variant):
        """A prior-period / lag variant references ONE period's distinct count,
        which is correct — it must NOT be rejected by the cumulation guard."""
        from src.rewrite.query_rewriter import rewrite_for_source

        bound = _make_dax_bound_query(
            measure_name="Distinct Customers",
            dim_name="Year",
            variant_hints={"Distinct Customers": variant},
        )
        bound.resolved_measures[0].default_agg = "count_distinct"

        # Must not raise the count_distinct cumulation guard; emits SQL.
        await attach_fixture_deployed_shape(bound, _variant_render_db())
        sql = await rewrite_for_source(bound, _variant_render_db())
        assert "COUNT(DISTINCT" in sql.upper()


class TestSemiAdditiveVariantBase:
    """Bug-6571: a semi-additive measure (balance) with an admissible
    lag/parallel_period variant must wrap the SEMI-ADDITIVE base aggregation
    (last_non_empty/first_non_empty/min/max/avg_of_children), NOT SUM.

    Emitting LAG(SUM(balance)) sums every intra-period balance row before the
    window compares periods, so the prior-period value is a nonsense grand
    total instead of the period-end balance -> wrong numbers reach the BI
    client. The fix wraps LAG(last_non_empty(balance)).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("variant", ["prior_year", "lag"])
    async def test_semi_additive_variant_wraps_last_non_empty(self, variant):
        from src.rewrite.query_rewriter import rewrite_for_source

        bound = _make_dax_bound_query(
            measure_name="Account Balance",
            dim_name="Year",
            variant_hints={"Account Balance": variant},
        )
        bound.resolved_measures[0].semi_additive_behavior = "last_non_empty"
        bound.resolved_measures[0].default_agg = "last_non_empty"

        await attach_fixture_deployed_shape(bound, _variant_render_db())
        sql = await rewrite_for_source(bound, _variant_render_db())
        upper = sql.upper()
        # Semi-additive base must be present (ARRAY_AGG ... FILTER picks the
        # last non-null balance within each period bucket).
        assert "ARRAY_AGG" in upper, f"Expected last_non_empty base, got: {sql}"
        assert "FILTER (WHERE" in upper, f"Expected FILTER clause, got: {sql}"
        # The additive SUM(balance) base must NOT be the window's inner expr.
        assert "LAG(SUM(" not in upper, (
            f"Semi-additive variant must not wrap SUM: {sql}"
        )
        assert "SUM(\"BASE\".\"REVENUE_AMOUNT\")" not in upper, (
            f"Semi-additive variant must not sum the balance column: {sql}"
        )

    @pytest.mark.asyncio
    async def test_first_non_empty_variant_wraps_array_agg_asc(self):
        from src.rewrite.query_rewriter import rewrite_for_source

        bound = _make_dax_bound_query(
            measure_name="Account Balance",
            dim_name="Year",
            variant_hints={"Account Balance": "prior_year"},
        )
        bound.resolved_measures[0].semi_additive_behavior = "first_non_empty"
        bound.resolved_measures[0].default_agg = "first_non_empty"

        await attach_fixture_deployed_shape(bound, _variant_render_db())
        sql = await rewrite_for_source(bound, _variant_render_db())
        upper = sql.upper()
        assert "ARRAY_AGG" in upper and "ASC" in upper, (
            f"Expected first_non_empty (ASC) base, got: {sql}"
        )
        assert "LAG(SUM(" not in upper

    @pytest.mark.asyncio
    async def test_min_semi_additive_variant_wraps_min_not_sum(self):
        from src.rewrite.query_rewriter import rewrite_for_source

        bound = _make_dax_bound_query(
            measure_name="Account Balance",
            dim_name="Year",
            variant_hints={"Account Balance": "prior_year"},
        )
        bound.resolved_measures[0].semi_additive_behavior = "min"
        bound.resolved_measures[0].default_agg = "min"

        await attach_fixture_deployed_shape(bound, _variant_render_db())
        sql = await rewrite_for_source(bound, _variant_render_db())
        upper = sql.upper()
        assert "MIN(\"BASE\".\"REVENUE_AMOUNT\")" in upper, (
            f"Expected MIN base for semi-additive min, got: {sql}"
        )
        assert "LAG(SUM(" not in upper


# ---------------------------------------------------------------------------
# Derived time-grain dispatch — period variants anchor to base date column
# ---------------------------------------------------------------------------

class TestDerivedTimeGrainVariantAnchors:
    @pytest.mark.asyncio
    async def test_current_period_ytd_month_grain_anchors_to_base_date(self):
        bound = _make_derived_time_bound_query(
            derived_name="business_date_month",
            derived_expr="EXTRACT(MONTH FROM sale_date)",
            time_grain="month",
            variant_kind="ytd",
        )

        from src.rewrite.query_rewriter import rewrite_for_source
        await attach_fixture_deployed_shape(bound, _derived_time_db())
        sql = await rewrite_for_source(bound, _derived_time_db())

        assert 'MIN("base"."sale_date")' in sql
        assert "EXTRACT(YEAR FROM MIN(" in sql
        assert "EXTRACT(YEAR FROM MIN((EXTRACT(MONTH" not in sql
        assert 'GROUP BY (EXTRACT(MONTH FROM "base"."sale_date"))' in sql

    @pytest.mark.asyncio
    async def test_prior_period_month_grain_keeps_gap_guard_on_base_date(self):
        bound = _make_derived_time_bound_query(
            derived_name="business_date_month",
            derived_expr="EXTRACT(MONTH FROM sale_date)",
            time_grain="month",
            variant_kind="prior_month",
        )

        from src.rewrite.query_rewriter import rewrite_for_source
        await attach_fixture_deployed_shape(bound, _derived_time_db())
        sql = await rewrite_for_source(bound, _derived_time_db())

        assert 'MIN("base"."sale_date")' in sql
        assert "LAG(SUM(" in sql
        assert "THEN LAG(" in sql
        assert "ELSE NULL" in sql
        assert "EXTRACT(YEAR FROM MIN((EXTRACT(MONTH" not in sql

    @pytest.mark.asyncio
    async def test_quarter_grain_qtd_anchors_to_base_date(self):
        bound = _make_derived_time_bound_query(
            derived_name="business_date_quarter",
            derived_expr="EXTRACT(QUARTER FROM sale_date)",
            time_grain="quarter",
            variant_kind="qtd",
        )

        from src.rewrite.query_rewriter import rewrite_for_source
        await attach_fixture_deployed_shape(bound, _derived_time_db())
        sql = await rewrite_for_source(bound, _derived_time_db())

        assert 'MIN("base"."sale_date")' in sql
        assert "EXTRACT(QUARTER FROM MIN(" in sql
        assert "EXTRACT(YEAR FROM MIN(" in sql
        assert "EXTRACT(QUARTER FROM MIN((EXTRACT(QUARTER" not in sql

    @pytest.mark.asyncio
    async def test_year_boundary_prior_year_anchors_derived_year_to_base_date(self):
        bound = _make_derived_time_bound_query(
            derived_name="business_date_year",
            derived_expr="EXTRACT(YEAR FROM sale_date)",
            time_grain="year",
            variant_kind="prior_year",
        )

        from src.rewrite.query_rewriter import rewrite_for_source
        await attach_fixture_deployed_shape(bound, _derived_time_db())
        sql = await rewrite_for_source(bound, _derived_time_db())

        assert 'MIN("base"."sale_date")' in sql
        assert "LAG(SUM(" in sql
        assert "EXTRACT(YEAR FROM MIN(" in sql
        assert "EXTRACT(YEAR FROM MIN((EXTRACT(YEAR" not in sql

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "grain_order",
        [
            ("business_date_year", "business_date_quarter"),
            ("business_date_quarter", "business_date_year"),
        ],
    )
    async def test_prior_year_uses_finest_time_grain_regardless_of_group_order(
        self,
        grain_order,
    ):
        bound = _make_year_quarter_prior_year_query(grain_order)

        from src.rewrite.query_rewriter import rewrite_for_source
        await attach_fixture_deployed_shape(bound, _derived_time_db())
        sql = await rewrite_for_source(bound, _derived_time_db())

        assert "LAG(SUM(" in sql
        assert "EXTRACT(YEAR FROM MIN(" in sql
        assert "EXTRACT(QUARTER FROM MIN(" in sql
        assert "EXTRACT(QUARTER FROM MIN((EXTRACT(QUARTER" not in sql


# ---------------------------------------------------------------------------
# Route-level: DAX hints bypass aggregate matching
# ---------------------------------------------------------------------------

class TestDaxHintsReachAggregateRouting:
    """Bug-8043 (F-015-03, Option A): DAX time-variant hints now FLOW INTO aggregate
    matching, where the period-variant route either proves an equivalent
    base-over-aggregate acceleration or fails closed to source. The old blanket
    source-only force was removed; the wrong-numbers protection (never serve the
    untransformed base measure as the variant) moved into ``find_best_aggregate``."""

    @pytest.mark.asyncio
    async def test_dax_hints_reach_matcher_and_fall_back_when_unproven(self):
        """route_query now CALLS find_best_aggregate for a DAX time-variant hint; when
        the period-variant route is not proven (no aggregate) the query still falls back
        to the correct source rewrite."""
        bound = _make_dax_bound_query(
            measure_name="Revenue",
            dim_name="Year",
            variant_hints={"Revenue": "ytd"},
        )

        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
            scalar_one_or_none=MagicMock(return_value=None),
        ))

        # Matcher proves nothing (fail-closed) -> no aggregate -> source fallback.
        _no_match = types.SimpleNamespace(
            aggregate=None, skip_reasons=["period_variant_unproven"],
            name_to_canonical=None, logical_to_aggregate_grain=None,
            calc_expandable_measures=None, period_variant_plan=None,
        )

        with (
            patch("src.routing.router.rewrite_for_source", new_callable=AsyncMock) as mock_source,
            patch("src.routing.router.find_best_aggregate", new_callable=AsyncMock) as mock_agg,
            patch("src.routing.router.find_best_pocket", new_callable=AsyncMock) as mock_pocket,
            patch("src.routing.router.resolve_target_dialect_for_bound", new_callable=AsyncMock) as mock_dialect,
            patch("src.routing.router.compile_row_security", new_callable=AsyncMock) as mock_rls,
            patch("src.routing.router.has_active_rules", return_value=False) as mock_rules,
        ):
            mock_source.return_value = "SELECT 1"
            mock_dialect.return_value = "postgres"
            mock_rls.return_value = None
            mock_agg.return_value = _no_match
            mock_pocket.return_value = types.SimpleNamespace(pocket=None, skipped_reason=None)

            from src.routing.router import route_query
            decision = await route_query(bound, db)

        assert decision.route_type == "source"
        # The matcher is now consulted for DAX time-variant queries (the source-only
        # force was removed); a revert re-adding the gate makes this assertion fail.
        mock_agg.assert_called_once()
