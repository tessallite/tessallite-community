"""Bug-8043 (F-015-03, Option A) — period-variant aggregate route.

Proves the period-variant route is numerically equivalent to the source route (it emits
the variant as ONE GROUP-BY query through the ORDINARY aggregate envelope, reusing the
SAME emitter with a byte-equivalent binding) and that every unproved admission condition
falls back to source (never serves a base value as a variant).

Codex live-Postgres gate (2026-07-27) regression guards baked in here: HAVING is applied
BEFORE the window (window INPUT-ROW parity), ORDER BY / LIMIT / projection positions +
aliases are preserved, and the grain dimension's STORED value is the key (never a
DATE_TRUNC rewrite). A revert of any of these breaks a marked assertion.

Tiering: source-vs-emitter structural parity is a T1 producer/consumer contract; the
shape/fallback guards are T2 fixed-behaviour regression guards.
"""
from __future__ import annotations

import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.semantic.time_variants_sql import VariantBinding, emit_variant_expression
from src.ir.logical_query import BoundQuery, LogicalQuery, SelectExpression
from src.rewrite.aggregate import rewrite_for_aggregate
from src.rewrite.period_variant_aggregate import PeriodVariantItem, PeriodVariantPlan
from src.routing.aggregate_matcher import (
    _build_period_variant_plan_for_agg,
    _resolve_period_variant_context,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _agg(*, grain, grain_physical_cols, columns, table="agg_sales", schema="acme_agg"):
    return types.SimpleNamespace(
        id="agg-1", physical_table_name=table, target_schema=schema,
        grain=list(grain), grain_physical_cols=list(grain_physical_cols),
        columns=list(columns), status="active", is_stale=False,
        last_refreshed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        persona_id=None, built_for_version_id=None, built_for_epoch=None,
        refresh_policy=None,
    )


def _agg_col(measure_name, stat_type, physical_col_name):
    return types.SimpleNamespace(
        measure=types.SimpleNamespace(name=measure_name),
        stat_type=stat_type, physical_col_name=physical_col_name,
    )


def _se_var(name, alias=None, agg="sum"):
    """SELECT expression for a variant measure (analytical: AGG("name"))."""
    return SelectExpression(
        raw_text=f'{agg.upper()}("{name}")', alias=alias,
        classification="analytical", agg_function=agg,
        inner_column=name, inner_literal=None,
    )


def _se_dim(name, alias=None):
    """SELECT expression for a bare dimension reference."""
    return SelectExpression(
        raw_text=f'"{name}"', alias=alias, classification="passthrough",
        agg_function=None, inner_column=name, inner_literal=None,
    )


def _bound(*, resolved_measures, resolved_dimensions, grain, time_period_grains=None,
           dim_types=None, filters=None, select_expressions=None,
           deployed_version_id=None, order_by=None, limit=None, offset=None,
           having_raw=None):
    model = types.SimpleNamespace(
        id="m-1", slug="testmodel",
        deployed_version_id=deployed_version_id, deploy_epoch=0,
    )
    lq = LogicalQuery(
        model_id="m-1", protocol="jdbc", raw_query="SELECT ...",
        requested_measures=[m.name for m in resolved_measures],
        requested_dimensions=[d.name for d in resolved_dimensions],
        filters=[], grain=list(grain), order_by=list(order_by or []),
        limit=limit, offset=offset, query_fingerprint="pv-test",
        select_expressions=select_expressions or [],
        time_period_grains=time_period_grains, having_raw=having_raw,
    )
    return BoundQuery(
        logical_query=lq, model=model,
        resolved_measures=list(resolved_measures),
        resolved_dimensions=list(resolved_dimensions),
        resolved_filters=list(filters or []),
        resolved_dimensions_by_name={d.name: d for d in resolved_dimensions},
        dim_type_by_name=dict(dim_types or {}),
    )


def _pv_plan(**over):
    base = dict(
        items=[PeriodVariantItem(
            measure_name="YTD Sales", variant_kind="ytd",
            variant_n=None, alias="YTD Sales", base_sum_col="sales__sum",
        )],
        anchor_phys_col="month_c", anchor_data_type="date",
        partition_logical_to_phys={"region": "region_c"},
        time_grain_unit="month", time_dim_logical="sale_month",
        calendar_type="standard", fiscal_year_start_month=None,
        base_sum_cols=("sales__sum",),
    )
    base.update(over)
    return PeriodVariantPlan(**base)


def _render(bound, agg, plan, dialect="postgres"):
    return rewrite_for_aggregate(
        bound, agg, dialect,
        logical_to_aggregate_grain=None, period_variant_plan=plan,
    )


def _region_dim():
    return types.SimpleNamespace(
        id="dim-region", name="region", is_time_dim=False,
        dimension_kind="attribute", data_type="text",
    )


def _month_dim(name="sale_month"):
    return types.SimpleNamespace(
        id=f"dim-{name}", name=name, is_time_dim=True, dimension_kind="time",
        time_grain="month", hierarchy_id=None, source_column_id="dcol",
        data_type="date",
    )


# ---------------------------------------------------------------------------
# Envelope integration — the variant is one GROUP-BY query through the ordinary path
# ---------------------------------------------------------------------------

class TestEnvelopeIntegration:
    def _scenario(self, **bound_over):
        agg = _agg(
            grain=["region", "product", "sale_month"],
            grain_physical_cols=["region_c", "product_c", "month_c"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        base = dict(
            resolved_measures=[types.SimpleNamespace(name="YTD Sales", variant_kind="ytd")],
            resolved_dimensions=[_region_dim(), _month_dim()],
            grain=["region", "sale_month"],
            dim_types={"sale_month": "date", "region": "text"},
            select_expressions=[
                _se_dim("region"), _se_dim("sale_month"),
                _se_var("YTD Sales", "ytd"),
            ],
        )
        base.update(bound_over)
        return _bound(**base), agg

    def test_single_group_by_query_rolls_finer_dim_away(self):
        """No parallel two-stage CTE: one GROUP-BY query. The finer grain dimension
        (product) is summed away; the window is the doubled-aggregate SUM(SUM(base))
        OVER. A revert to a windows-over-finer-rows shape breaks this."""
        bound, agg = self._scenario()
        sql = _render(bound, agg, _pv_plan())
        u = sql.upper()
        assert "__PV_ROLLED" not in u and "WITH " not in u
        assert "GROUP BY" in u
        # product is finer than the query grain -> rolled away (never grouped).
        assert "PRODUCT_C" not in u.split("GROUP BY")[1]
        assert '"REGION_C"' in u.split("GROUP BY")[1]
        assert '"MONTH_C"' in u.split("GROUP BY")[1]
        # YTD cumulative window over the re-aggregated stored base (doubled aggregate).
        assert "SUM(SUM(" in u
        assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in u

    def test_stored_time_key_projected_not_date_trunc(self):
        """Codex finding 3: the grain dimension's STORED value is the key — never a
        DATE_TRUNC rewrite (which would attach right values to wrong period keys)."""
        bound, agg = self._scenario()
        sql = _render(bound, agg, _pv_plan())
        assert "DATE_TRUNC" not in sql.upper()
        assert '"month_c" AS "sale_month"' in sql

    def test_window_matches_source_emitter(self):
        """Source-vs-aggregate parity: the projected window equals the SAME emitter the
        source route uses, with base = the envelope's SUM(<stored sum>) re-aggregation and
        fact_date = MIN(<time-dim column>). A revert that stops reusing the emitter or
        changes the base/anchor breaks this."""
        bound, agg = self._scenario()
        sql = _render(bound, agg, _pv_plan())
        expected = emit_variant_expression(
            "ytd",
            VariantBinding(
                base_expression='SUM("sales__sum")', base_unaggregated=None,
                fact_date_column='MIN("month_c")', calendar_alias="cal",
                calendar_columns=None, dialect="postgresql", n=None,
                partition_by=('"region_c"',), calendar_type="standard",
                fiscal_year_start_month=None, time_grain="month",
            ),
        ).sql
        assert expected in sql, sql

    def test_having_applied_before_window(self):
        """Codex finding 1 [WRONG NUMBERS]: HAVING must filter the grouped base rows
        BEFORE the window (SQL applies HAVING before window functions). The ordinary
        envelope maps HAVING SUM(<base>) onto the stored base column. A revert that drops
        HAVING (the old standalone builder never emitted it) breaks this."""
        bound, agg = self._scenario(having_raw='HAVING SUM("Sales") > 100')
        sql = _render(bound, agg, _pv_plan())
        u = sql.upper()
        assert "HAVING" in u
        assert "SALES__SUM" in u.split("HAVING")[1]

    def test_order_by_and_limit_preserved(self):
        """Codex finding 2 [CONSUMER BREAKAGE]: ORDER BY + LIMIT must be preserved
        (ordinal BI clients bind by position). A revert that drops them breaks this."""
        bound, agg = self._scenario(
            select_expressions=[_se_var("YTD Sales", "ytd"), _se_dim("sale_month")],
            resolved_dimensions=[_month_dim()], grain=["sale_month"],
            order_by=[("sale_month", "desc")], limit=1,
            dim_types={"sale_month": "date"},
        )
        sql = _render(bound, agg, _pv_plan(partition_logical_to_phys={}))
        u = sql.upper()
        assert "ORDER BY" in u and "DESC" in u
        assert "LIMIT 1" in u

    def test_projection_positions_and_aliases_preserved(self):
        """Codex finding 2: the SELECT column ORDER + aliases must match the query, not a
        fixed builder order. `SELECT SUM("YTD Sales") AS ytd, sale_month` keeps ytd FIRST."""
        bound, agg = self._scenario(
            select_expressions=[_se_var("YTD Sales", "ytd"), _se_dim("sale_month")],
            resolved_dimensions=[_month_dim()], grain=["sale_month"],
            dim_types={"sale_month": "date"},
        )
        sql = _render(bound, agg, _pv_plan(partition_logical_to_phys={}))
        assert sql.index('AS "ytd"') < sql.index('AS "sale_month"')

    def test_bigquery_dialect(self):
        bound, agg = self._scenario()
        sql = _render(bound, agg, _pv_plan(), dialect="bigquery")
        assert "`sales__sum`" in sql
        assert "DATE_TRUNC" not in sql.upper()  # stored key, not a trunc

    def test_prior_year_carries_adjacency_guard(self):
        bound, agg = self._scenario(
            resolved_measures=[types.SimpleNamespace(name="PY Sales", variant_kind="prior_year")],
            select_expressions=[
                _se_dim("region"), _se_dim("sale_month"), _se_var("PY Sales", "py"),
            ],
        )
        plan = _pv_plan(items=[PeriodVariantItem(
            "PY Sales", "prior_year", None, "PY Sales", "sales__sum")])
        sql = _render(bound, agg, plan)
        # F-015-19 adjacency guard: prior period must be exactly one back, else NULL.
        assert "ELSE NULL END" in sql.upper()
        assert "LAG(" in sql.upper()


# ---------------------------------------------------------------------------
# Per-aggregate plan coverage
# ---------------------------------------------------------------------------

def _pv_ctx(filter_logicals=None):
    from src.routing.aggregate_matcher import _PeriodVariantCtx
    return _PeriodVariantCtx(
        items=[("YoY Sales", "prior_month", None, "YoY Sales", "Sales")],
        anchor_logical="sale_date", anchor_data_type="date",
        time_grain_unit="month", time_dim_logical="sale_date",
        partition_logicals=["region"],
        filter_logicals=list(filter_logicals or []),
        calendar_type="standard", fiscal_year_start_month=None,
    )


class TestPerAggregateCoverage:
    def test_admits_when_base_sum_anchor_and_partition_present(self):
        agg = _agg(
            grain=["region", "sale_date"],
            grain_physical_cols=["region_col", "sale_date_col"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        plan = _build_period_variant_plan_for_agg(
            agg, _pv_ctx(), {"region": "region", "sale_date": "sale_date"})
        assert plan is not None
        assert plan.anchor_phys_col == "sale_date_col"
        assert plan.partition_logical_to_phys == {"region": "region_col"}
        assert plan.items[0].base_sum_col == "sales__sum"

    def test_rejects_missing_base_sum_column(self):
        agg = _agg(
            grain=["region", "sale_date"],
            grain_physical_cols=["region_col", "sale_date_col"],
            columns=[_agg_col("Sales", "max", "sales__max")],
        )
        assert _build_period_variant_plan_for_agg(
            agg, _pv_ctx(), {"region": "region", "sale_date": "sale_date"}) is None

    def test_rejects_missing_anchor_column(self):
        agg = _agg(
            grain=["region"], grain_physical_cols=["region_col"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        assert _build_period_variant_plan_for_agg(
            agg, _pv_ctx(), {"region": "region"}) is None

    def test_rejects_missing_partition_column(self):
        agg = _agg(
            grain=["sale_date"], grain_physical_cols=["sale_date_col"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        assert _build_period_variant_plan_for_agg(
            agg, _pv_ctx(), {"sale_date": "sale_date"}) is None

    def test_maps_ungrouped_filter_column_to_physical(self):
        agg = _agg(
            grain=["region", "product", "sale_date"],
            grain_physical_cols=["region_c", "product_c", "sale_date_c"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        plan = _build_period_variant_plan_for_agg(
            agg, _pv_ctx(filter_logicals=["product"]),
            {"region": "region", "sale_date": "sale_date", "product": "product"},
        )
        assert plan is not None
        assert plan.filter_logical_to_phys == {"product": "product_c"}


# ---------------------------------------------------------------------------
# Query-level admission (context resolver) — fallback matrix
# ---------------------------------------------------------------------------

def _variant_measure(name="YoY Sales", kind="prior_month", base_id="base-1",
                     resolved_date_col_id="dcol"):
    # resolved_date_col_id defaults to the time dim's source_column_id ("dcol") so the
    # window-anchor identity check passes; a divergent value forces fall-back.
    return types.SimpleNamespace(
        id="var-1", name=name, variant_kind=kind, variant_n=None,
        variant_of_measure_id=base_id, default_agg="sum", is_additive=True,
        measure_type="standard", semi_additive_behavior=None,
        resolved_calendar_id=None, calendar_model_table_id=None,
        resolved_date_col_id=resolved_date_col_id,
    )


def _base_measure(default_agg="sum", **over):
    m = types.SimpleNamespace(
        id="base-1", name="Sales", default_agg=default_agg, is_additive=True,
        measure_type="standard", semi_additive_behavior=None, variant_kind=None,
    )
    for k, v in over.items():
        setattr(m, k, v)
    return m


def _date_dim(name="sale_date", time_grain="day"):
    return types.SimpleNamespace(
        id=f"dim-{name}", name=name, is_time_dim=True, dimension_kind="time",
        time_grain=time_grain, hierarchy_id=None, source_column_id="dcol",
        data_type="date",
    )


def _db(base=None):
    """Undeployed-model DB: standard calendar rules + base measure live-load."""
    base = base if base is not None else _base_measure()

    async def _exec(stmt):
        text = str(stmt).lower()
        r = MagicMock()
        if "hierarchydefinition" in text or "hierarchy_definition" in text:
            r.first.return_value = None  # -> ('standard', None)
        if "measures" in text or "measure" in text:
            r.scalar_one_or_none.return_value = base
        r.scalars.return_value.all.return_value = []
        return r

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_exec)
    db.get = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
class TestContextAdmission:
    async def test_admits_valid_prior_month_query(self):
        bound = _bound(
            resolved_measures=[_variant_measure()],
            resolved_dimensions=[_region_dim(), _date_dim()],
            grain=["region", "sale_date"],
            dim_types={"sale_date": "date", "region": "text"},
        )
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert skip is None and ctx is not None
        assert ctx.time_grain_unit == "day"
        assert ctx.anchor_logical == "sale_date"
        assert ctx.partition_logicals == ["region"]
        assert ctx.items[0] == ("YoY Sales", "prior_month", None, "YoY Sales", "Sales")

    async def test_no_period_variant_is_noop(self):
        plain = types.SimpleNamespace(name="Sales", variant_kind=None)
        bound = _bound(resolved_measures=[plain], resolved_dimensions=[_date_dim()],
                       grain=["sale_date"])
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert ctx is None and skip is None

    async def test_mixed_with_plain_measure_falls_back(self):
        plain = types.SimpleNamespace(name="Sales", variant_kind=None)
        bound = _bound(
            resolved_measures=[_variant_measure(), plain],
            resolved_dimensions=[_date_dim()], grain=["sale_date"],
            dim_types={"sale_date": "date"},
        )
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert ctx is None and skip == "period_variant_unproven"

    async def test_non_sum_base_falls_back(self):
        bound = _bound(
            resolved_measures=[_variant_measure()],
            resolved_dimensions=[_date_dim()], grain=["sale_date"],
            dim_types={"sale_date": "date"},
        )
        ctx, skip = await _resolve_period_variant_context(
            bound, _db(base=_base_measure(default_agg="avg")))
        assert ctx is None and skip == "period_variant_unproven"

    async def test_non_date_anchor_falls_back(self):
        d = _date_dim()
        d.data_type = "integer"
        bound = _bound(
            resolved_measures=[_variant_measure()],
            resolved_dimensions=[_region_dim(), d],
            grain=["region", "sale_date"],
            dim_types={"sale_date": "integer"},
        )
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert ctx is None and skip == "period_variant_unproven"

    async def test_divergent_resolved_date_anchor_falls_back(self):
        """WRONG-NUMBERS guard (opus5 round-2): a variant configured to order by a
        DIFFERENT date column than the grain time dimension's own source column must
        fall back to source."""
        v = _variant_measure(resolved_date_col_id="ship_date_col")  # != dim's "dcol"
        bound = _bound(
            resolved_measures=[v],
            resolved_dimensions=[_region_dim(), _date_dim()],
            grain=["region", "sale_date"],
            dim_types={"sale_date": "date", "region": "text"},
        )
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert ctx is None and skip == "period_variant_unproven"

    async def test_matching_resolved_date_anchor_admits(self):
        v = _variant_measure(resolved_date_col_id="dcol")  # == dim's source_column_id
        bound = _bound(
            resolved_measures=[v],
            resolved_dimensions=[_region_dim(), _date_dim()],
            grain=["region", "sale_date"],
            dim_types={"sale_date": "date", "region": "text"},
        )
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert skip is None and ctx is not None

    async def test_date_trunc_only_grain_falls_back(self):
        """WRONG-NUMBERS parity guard (opus5 round-1): a SQL DATE_TRUNC time-period grain
        does not prove the variant time_grain the source route computes at; this shape
        falls back to source."""
        bound = _bound(
            resolved_measures=[_variant_measure()],
            resolved_dimensions=[_region_dim(), _date_dim()],
            grain=["region"],
            time_period_grains=[("month", "sale_date")],
            dim_types={"sale_date": "date", "region": "text"},
        )
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert ctx is None and skip == "period_variant_unproven"

    async def test_month_time_dimension_admits_at_dimension_grain(self):
        bound = _bound(
            resolved_measures=[_variant_measure(kind="prior_year")],
            resolved_dimensions=[_region_dim(), _date_dim("sale_month", "month")],
            grain=["region", "sale_month"],
            dim_types={"sale_month": "date", "region": "text"},
        )
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert skip is None and ctx is not None
        assert ctx.time_grain_unit == "month"
        assert ctx.anchor_logical == "sale_month"
        assert ctx.partition_logicals == ["region"]

    async def test_multiple_time_period_grains_fall_back(self):
        bound = _bound(
            resolved_measures=[_variant_measure()],
            resolved_dimensions=[_date_dim()], grain=[],
            time_period_grains=[("month", "sale_date"), ("year", "ship_date")],
            dim_types={"sale_date": "date"},
        )
        ctx, skip = await _resolve_period_variant_context(bound, _db())
        assert ctx is None and skip == "period_variant_unproven"


# ---------------------------------------------------------------------------
# End-to-end: find_best_aggregate -> validate_aggregate_route -> rewrite_for_aggregate
# ---------------------------------------------------------------------------

async def _aw(v):
    return v


@pytest.mark.asyncio
class TestEndToEndRouting:
    async def test_matcher_admits_validates_and_rewriter_builds(self, monkeypatch):
        """The REAL production path: matcher admits + returns a plan, validator (with the
        plan) accepts the finer grain, rewriter emits the single GROUP-BY variant query.
        A revert of any half breaks this."""
        from src.routing import aggregate_matcher as am
        from src.routing.aggregate_matcher import find_best_aggregate
        from src.routing.exactness_validator import validate_aggregate_route

        bound = _bound(
            resolved_measures=[_variant_measure(name="YTD Sales", kind="ytd")],
            resolved_dimensions=[_region_dim(), _month_dim()],
            grain=["region", "sale_month"],
            dim_types={"sale_month": "date", "region": "text"},
            select_expressions=[
                _se_dim("region"), _se_dim("sale_month"), _se_var("YTD Sales", "ytd"),
            ],
        )
        agg = _agg(
            grain=["region", "product", "sale_month"],
            grain_physical_cols=["region_c", "product_c", "month_c"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        monkeypatch.setattr(am, "load_active_aggregates",
                            lambda model_id, db: _aw([agg]))
        monkeypatch.setattr(am, "_get_canonical_dims_cached",
                            lambda bq, db: _aw([]))

        result = await find_best_aggregate(bound, _db())
        assert result.aggregate is agg
        assert result.period_variant_plan is not None

        ok, reason = validate_aggregate_route(
            bound, agg,
            logical_to_aggregate_grain=result.logical_to_aggregate_grain,
            period_variant_plan=result.period_variant_plan,
        )
        assert ok is True, reason

        sql = rewrite_for_aggregate(
            bound, agg, "postgres",
            logical_to_aggregate_grain=result.logical_to_aggregate_grain,
            period_variant_plan=result.period_variant_plan,
        )
        u = sql.upper()
        assert "GROUP BY" in u and "SUM(SUM(" in u
        assert "PRODUCT_C" not in u.split("GROUP BY")[1]

    async def test_non_calendar_dax_hint_never_serves_untransformed_base(self, monkeypatch):
        """WRONG-NUMBERS guard (opus5 round-3): a NON-calendar DAX time-intel hint
        (DATEADD -> 'period_offset') is not served by the period-variant route, so the
        matcher's DAX-hint guard MUST stop the ordinary loop from serving the untransformed
        base. Drives the REAL find_best_aggregate; a revert of the guard makes the base
        aggregate match and this fail."""
        from src.routing import aggregate_matcher as am
        from src.routing.aggregate_matcher import find_best_aggregate

        plain = types.SimpleNamespace(
            id="m-sales", name="Sales", variant_kind=None, variant_n=None,
            default_agg="sum", is_additive=True, measure_type="standard",
            calc_agg_mode=None, semi_additive_behavior=None,
        )
        bound = _bound(
            resolved_measures=[plain],
            resolved_dimensions=[_region_dim(), _month_dim()],
            grain=["region", "sale_month"],
            dim_types={"sale_month": "date", "region": "text"},
        )
        bound.logical_query.time_variant_hints = {"Sales": "period_offset"}
        agg = _agg(
            grain=["region", "sale_month"],
            grain_physical_cols=["region_c", "month_c"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        monkeypatch.setattr(am, "load_active_aggregates",
                            lambda model_id, db: _aw([agg]))
        monkeypatch.setattr(am, "_get_canonical_dims_cached",
                            lambda bq, db: _aw([]))
        result = await find_best_aggregate(bound, _db())
        assert result.aggregate is None
        assert result.period_variant_plan is None

    async def test_matcher_falls_back_when_no_aggregate_stores_base_sum(self, monkeypatch):
        from src.routing import aggregate_matcher as am
        from src.routing.aggregate_matcher import find_best_aggregate

        bound = _bound(
            resolved_measures=[_variant_measure(kind="prior_month")],
            resolved_dimensions=[_region_dim(), _month_dim()],
            grain=["region", "sale_month"],
            dim_types={"sale_month": "date", "region": "text"},
        )
        agg = _agg(
            grain=["region", "sale_month"],
            grain_physical_cols=["region_c", "month_c"],
            columns=[_agg_col("Sales", "max", "sales__max")],  # no SUM -> source
        )
        monkeypatch.setattr(am, "load_active_aggregates",
                            lambda model_id, db: _aw([agg]))
        monkeypatch.setattr(am, "_get_canonical_dims_cached",
                            lambda bq, db: _aw([]))
        result = await find_best_aggregate(bound, _db())
        assert result.aggregate is None
        assert result.period_variant_plan is None


# ---------------------------------------------------------------------------
# validate_aggregate_route: a proven plan bypasses the exact-grain rejection
# ---------------------------------------------------------------------------

class TestValidatorPeriodVariant:
    def test_finer_grain_variant_rejected_without_plan_admitted_with_plan(self):
        """opus5 round-2 finding 1 (feature-dead): an ORM variant measure makes
        compute_has_non_additive True, so validate_aggregate_route rejects a
        finer-than-query-grain aggregate. A proven period_variant_plan must bypass that
        rejection. A revert of the validator fix makes the WITH-plan assertion fail."""
        from src.routing.exactness_validator import validate_aggregate_route

        variant = _variant_measure(name="YTD Sales", kind="ytd")
        bound = _bound(
            resolved_measures=[variant],
            resolved_dimensions=[_region_dim(), _month_dim()],
            grain=["region", "sale_month"],
            dim_types={"sale_month": "date", "region": "text"},
        )
        agg = _agg(
            grain=["region", "product", "sale_month"],
            grain_physical_cols=["region_c", "product_c", "month_c"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        l2a = {"region": "region", "sale_month": "sale_month"}

        ok_no_plan, _ = validate_aggregate_route(
            bound, agg, logical_to_aggregate_grain=l2a)
        assert ok_no_plan is False

        ok_plan, reason = validate_aggregate_route(
            bound, agg, logical_to_aggregate_grain=l2a,
            period_variant_plan=_pv_plan())
        assert ok_plan is True, reason


_PV_FACT = "t-pv-fact"
_PV_DIM = "t-pv-dim"


def _pv_population_world(monkeypatch, *, dim_join_type, dim_key_is_pk):
    """Install a REAL two-relation join graph for the period-variant candidate loop.

    Bug-8664: the period-variant loop reads the SAME materialised rows as the
    ordinary loop, so it carries the same row-population exposure and must take
    the same gate. Without this the whole PV branch runs against the conftest
    synthetic one-relation world, where the gate can never refuse anything —
    deleting the PV gate leaves the entire suite green.
    """
    from src.routing import aggregate_population as ap
    from src.routing import pocket_matcher as pm
    from src.routing.aggregate_population import AggregateObjectIndex
    from src.routing.pocket_population import JoinEdge, ModelJoinGraph

    graph = ModelJoinGraph(
        table_ids=frozenset({_PV_FACT, _PV_DIM}),
        edges=(JoinEdge(_PV_FACT, _PV_DIM, "c-fact-pk", "c-dim-pk", dim_join_type),),
        pk_column_ids=frozenset({"c-dim-pk"}) if dim_key_is_pk else frozenset(),
        table_id_by_column_id={"c-fact-pk": _PV_FACT, "c-dim-pk": _PV_DIM},
        anchor_table_id=_PV_FACT,
    )
    index = AggregateObjectIndex(
        dimension_names=frozenset({"region", "product", "sale_month"}),
        # ``product`` lives on the extra relation the aggregate joined; the
        # query groups only by relations on the fact.
        table_by_dimension_name={
            "region": _PV_FACT, "sale_month": _PV_FACT, "product": _PV_DIM,
        },
        measure_id_by_name={"Sales": "m-sales"},
        table_by_measure_id={"m-sales": _PV_FACT},
        expression_by_measure_id={},
    )

    async def _graph(_model, _db):
        return graph, {}

    async def _index(_model, _db, *, graph, table_id_by_uda_id):
        return index

    monkeypatch.setattr(pm, "_load_model_join_graph", _graph)
    monkeypatch.setattr(pm, "_query_plan_table_ids", lambda *a, **k: {_PV_FACT})
    monkeypatch.setattr(ap, "load_aggregate_object_index", _index)


@pytest.mark.asyncio
class TestPeriodVariantPopulationGate:
    """Bug-8664 on the SECOND aggregate candidate loop."""

    async def _run(self, monkeypatch, *, dim_join_type, dim_key_is_pk):
        from src.routing import aggregate_matcher as am
        from src.routing.aggregate_matcher import find_best_aggregate

        bound = _bound(
            resolved_measures=[_variant_measure(name="YTD Sales", kind="ytd")],
            resolved_dimensions=[_region_dim(), _month_dim()],
            grain=["region", "sale_month"],
            dim_types={"sale_month": "date", "region": "text"},
            select_expressions=[
                _se_dim("region"), _se_dim("sale_month"), _se_var("YTD Sales", "ytd"),
            ],
        )
        agg = _agg(
            grain=["region", "product", "sale_month"],
            grain_physical_cols=["region_c", "product_c", "month_c"],
            columns=[_agg_col("Sales", "sum", "sales__sum")],
        )
        monkeypatch.setattr(am, "load_active_aggregates",
                            lambda model_id, db: _aw([agg]))
        monkeypatch.setattr(am, "_get_canonical_dims_cached",
                            lambda bq, db: _aw([]))
        _pv_population_world(
            monkeypatch, dim_join_type=dim_join_type, dim_key_is_pk=dim_key_is_pk,
        )
        return agg, await find_best_aggregate(bound, _db())

    async def test_lossy_extra_relation_refuses_the_period_variant_route(
        self, monkeypatch,
    ):
        """The aggregate joined ``fact INNER JOIN dim`` for its ``product`` grain;
        the query's own plan is the bare fact. Every fact row with no ``product``
        partner is missing from the aggregate, so the YTD number it would serve is
        understated. The PV loop must refuse it exactly as the ordinary loop does."""
        from src.routing.aggregate_matcher import AggregateSkipReason

        _agg_def, result = await self._run(
            monkeypatch, dim_join_type="inner", dim_key_is_pk=True,
        )
        assert result.aggregate is None
        assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (
            result.skip_reasons or []
        )

    async def test_row_preserving_extra_relation_still_serves(self, monkeypatch):
        """Control: the same shape with a row-preserving, non-fanning edge still
        accelerates, so the refusal above is the GATE and not the fixture."""
        agg, result = await self._run(
            monkeypatch, dim_join_type="left", dim_key_is_pk=True,
        )
        assert result.aggregate is agg
        assert result.period_variant_plan is not None
