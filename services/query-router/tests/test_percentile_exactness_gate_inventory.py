"""Phase 0 guard — the percentile exactness gate's request inventory is
SEMANTIC and COMPLETE, not syntactic or one-level (Bug-7779 HIGH).

Spec: docs/strategy/strategy_percentile-aggregate-routing.md (I1, Gap G,
§12.3). ``_query_uses_percentile`` must fire the dialect-exactness gate for a
quantile arriving via:
  (1) explicit SELECT MEDIAN()/PERCENTILE_* syntax,
  (2) a HAVING percentile,
  (3) a measure ``default_agg`` (bare ``SELECT med_x``),
  (4) a calculated measure at ANY nesting depth whose base stat is a quantile.
It must fail CLOSED on any unparseable expression, missing dependency row, DB
failure, or unparseable HAVING — never fail open.

Otherwise a BigQuery-source aggregate serves its APPROX_QUANTILES column in
exact mode (exact p90 of [1,100] = 90.1, but the APPROX boundary = 100).

Run from tessallite/services/query-router/:
    pytest tests/test_percentile_exactness_gate_inventory.py
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock

from src.routing.router import _query_uses_percentile

from conftest import make_measure, make_dimension, make_bound_query


def _select_expr(agg_function: str | None = None, inner_column: str = "x"):
    return types.SimpleNamespace(agg_function=agg_function, inner_column=inner_column)


def _db_returning(measures_by_name: dict[str, object]) -> AsyncMock:
    """An AsyncMock db whose execute() returns rows for a ``name == <name>``
    filter. The gate loads unresolved calc dependencies one name at a time and
    reads ``.scalars().first()``."""
    db = AsyncMock()

    async def _execute(stmt):
        wanted = None
        try:
            params = stmt.compile().params
            wanted = next(
                (v for v in params.values() if isinstance(v, str) and v in measures_by_name),
                None,
            )
        except Exception:
            text = str(stmt)
            wanted = next((nm for nm in measures_by_name if nm and nm in text), None)
        found = measures_by_name.get(wanted)
        scalars = types.SimpleNamespace(
            first=lambda: found, all=lambda: [found] if found else []
        )
        return types.SimpleNamespace(scalars=lambda: scalars)

    db.execute = _execute
    return db


def _patch_parse(monkeypatch, refs_by_name: dict[str, list[str]]):
    """Patch parse_expression so a calc expression maps to a known reference
    list; matched by the reference names appearing in the expression text."""
    import shared.semantic.calculated_expression as calc_mod

    def _fake(expr: str):
        for _name, refs in refs_by_name.items():
            if refs and all(r in expr for r in refs):
                return types.SimpleNamespace(
                    references=[types.SimpleNamespace(name=r) for r in refs]
                )
        return types.SimpleNamespace(references=[])

    monkeypatch.setattr(calc_mod, "parse_expression", _fake)


def _exact_percentile_cont(values: list[float], p: float) -> float:
    """Reference PERCENTILE_CONT (linear interpolation), independent of the
    production implementation — used only to pin the known values the gate
    protects."""
    s = sorted(values)
    if not s:
        raise ValueError("empty")
    rank = p * (len(s) - 1)
    lo = int(rank)
    frac = rank - lo
    if lo + 1 >= len(s):
        return float(s[lo])
    return float(s[lo]) + frac * (float(s[lo + 1]) - float(s[lo]))


def test_exact_p90_differs_from_approx_boundary_sanity():
    """Known-value guard for WHY the exactness gate must fire on a BigQuery
    source: exact PERCENTILE_CONT(0.9) over [1, 100] is 90.1, but
    APPROX_QUANTILES(...,100)[OFFSET(90)] returns the ascending boundary 100.
    Serving the approximate column in exact mode returns 100 instead of 90.1 —
    the wrong number the Bug-7779 gate prevents."""
    exact_p90 = _exact_percentile_cont([1, 100], 0.9)
    approx_boundary = 100.0  # APPROX_QUANTILES OFFSET(90) over [1,100]
    assert abs(exact_p90 - 90.1) < 1e-9
    assert abs(exact_p90 - approx_boundary) > 9.0  # 90.1 vs 100: ~9.9 apart


def _calc(name, expr):
    return make_measure(
        name,
        measure_type="calculated",
        calc_agg_mode="expression_as_written",
        expression=expr,
    )


# --------------------------------------------------------------------------- #
# (1) explicit SELECT syntax                                                   #
# --------------------------------------------------------------------------- #

async def test_explicit_select_percentile_detected():
    m = make_measure("latency")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = [_select_expr(agg_function="p50")]
    assert await _query_uses_percentile(bq, AsyncMock()) is True


# --------------------------------------------------------------------------- #
# (2) HAVING percentile — Codex R1 finding 2                                   #
# --------------------------------------------------------------------------- #

async def test_having_percentile_detected():
    """A HAVING percentile with no SELECT percentile still fires the gate."""
    m = make_measure("revenue", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = [_select_expr(agg_function="sum")]
    bq.logical_query.having_raw = "HAVING MEDIAN(latency) > 5"
    assert await _query_uses_percentile(bq, AsyncMock()) is True


async def test_unparseable_having_fails_closed():
    m = make_measure("revenue", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = []
    bq.logical_query.having_raw = "HAVING ((("  # unparseable
    assert await _query_uses_percentile(bq, AsyncMock()) is True


async def test_having_non_percentile_not_detected():
    m = make_measure("revenue", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = [_select_expr(agg_function="sum")]
    bq.logical_query.having_raw = "HAVING SUM(revenue) > 100"
    assert await _query_uses_percentile(bq, AsyncMock()) is False


# --------------------------------------------------------------------------- #
# (3) measure default_agg                                                      #
# --------------------------------------------------------------------------- #

async def test_measure_default_agg_quantile_detected():
    m = make_measure("latency_p90", default_agg="p90")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = []
    assert await _query_uses_percentile(bq, AsyncMock()) is True


async def test_plain_additive_measure_not_detected():
    m = make_measure("revenue", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = [_select_expr(agg_function="sum")]
    assert await _query_uses_percentile(bq, AsyncMock()) is False


# --------------------------------------------------------------------------- #
# (4) recursive calc dependency traversal — Codex R1 finding 1                 #
# --------------------------------------------------------------------------- #

async def test_calc_measure_with_resolved_quantile_base_detected(monkeypatch):
    base = make_measure("p90_latency", default_agg="p90")
    calc = _calc("ratio", 'measure("p90_latency") / 2')
    bq = make_bound_query([make_dimension("country")], [calc, base])
    bq.logical_query.select_expressions = []

    _patch_parse(monkeypatch, {"ratio": ["p90_latency"]})
    assert await _query_uses_percentile(bq, AsyncMock()) is True


async def test_nested_calc_quantile_base_via_db_detected(monkeypatch):
    """The finding-1 repro: outer calc -> inner calc (loaded from DB) -> p90
    base (loaded from DB). One-level lookup missed this; the full traversal
    must fire the gate."""
    outer = _calc("outer", 'measure("inner") + 1')
    inner = _calc("inner", 'measure("p90_base") * 2')
    p90_base = make_measure("p90_base", default_agg="p90")
    bq = make_bound_query([make_dimension("country")], [outer])  # only outer resolved
    bq.logical_query.select_expressions = []

    _patch_parse(monkeypatch, {"outer": ["inner"], "inner": ["p90_base"]})
    db = _db_returning({"inner": inner, "p90_base": p90_base})
    assert await _query_uses_percentile(bq, db) is True


async def test_missing_calc_dependency_fails_closed(monkeypatch):
    """A referenced base measure that cannot be loaded (missing row) must fail
    closed — the DAG cannot be proven quantile-free."""
    outer = _calc("outer", 'measure("ghost") + 1')
    bq = make_bound_query([make_dimension("country")], [outer])
    bq.logical_query.select_expressions = []

    _patch_parse(monkeypatch, {"outer": ["ghost"]})
    db = _db_returning({})  # ghost not found
    assert await _query_uses_percentile(bq, db) is True


async def test_calc_dependency_all_additive_not_detected(monkeypatch):
    """A calc whose full DAG resolves to only additive base measures does not
    fire the gate."""
    outer = _calc("outer", 'measure("rev") + measure("cost")')
    rev = make_measure("rev", default_agg="sum")
    cost = make_measure("cost", default_agg="sum")
    bq = make_bound_query([make_dimension("country")], [outer, rev, cost])
    bq.logical_query.select_expressions = []

    _patch_parse(monkeypatch, {"outer": ["rev", "cost"]})
    assert await _query_uses_percentile(bq, AsyncMock()) is False


async def test_empty_calc_expression_fails_closed():
    """Codex R3 finding 3: a calculated measure with an empty/blank expression
    is malformed and cannot be proven quantile-free -> fail closed (fire the
    gate), never report 'no dependencies'."""
    for blank in ("", "   ", None):
        calc = make_measure(
            "ratio", measure_type="calculated",
            calc_agg_mode="expression_as_written", expression=blank,
        )
        bq = make_bound_query([make_dimension("country")], [calc])
        bq.logical_query.select_expressions = []
        assert await _query_uses_percentile(bq, AsyncMock()) is True, blank


async def test_explicit_sum_over_quantile_default_not_fired():
    """Codex R3 finding 2 (gate side): an explicit SUM(latency) overrides a p50
    default_agg, so the exactness gate is not needlessly fired via default_agg
    (the query has no quantile). The explicit SUM is checked in path (1)."""
    m = make_measure("latency", default_agg="p50")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = [
        types.SimpleNamespace(
            classification="analytical", agg_function="sum",
            inner_column="latency", composable=False, inner_aggregates=[],
        ),
    ]
    assert await _query_uses_percentile(bq, AsyncMock()) is False


async def test_unparseable_calc_fails_closed(monkeypatch):
    calc = _calc("ratio", "!!! not parseable !!!")
    bq = make_bound_query([make_dimension("country")], [calc])
    bq.logical_query.select_expressions = []

    import shared.semantic.calculated_expression as calc_mod

    def _boom(expr):
        raise ValueError("unparseable")

    monkeypatch.setattr(calc_mod, "parse_expression", _boom)
    assert await _query_uses_percentile(bq, AsyncMock()) is True


async def test_cycle_in_calc_dag_terminates(monkeypatch):
    """A dependency cycle must not hang; it terminates and (being quantile-free)
    returns False."""
    a = _calc("a", 'measure("b")')
    b = _calc("b", 'measure("a")')
    bq = make_bound_query([make_dimension("country")], [a, b])
    bq.logical_query.select_expressions = []

    _patch_parse(monkeypatch, {"a": ["b"], "b": ["a"]})
    assert await _query_uses_percentile(bq, AsyncMock()) is False


# --------------------------------------------------------------------------- #
# Legacy ``median`` default_agg token — Codex R2 finding 1                     #
# --------------------------------------------------------------------------- #

async def test_legacy_median_default_agg_detected():
    """A measure carrying the LEGACY ``median`` token (un-coerced to p50) is a
    quantile request. ``is_quantile_stat_type('median')`` is False, so the gate
    must use ``is_quantile_agg_token`` — otherwise a legacy row slips the gate
    and an approximate column serves in exact mode."""
    m = make_measure("med_x", default_agg="median")
    bq = make_bound_query([make_dimension("country")], [m])
    bq.logical_query.select_expressions = []
    assert await _query_uses_percentile(bq, AsyncMock()) is True


# --------------------------------------------------------------------------- #
# Deployed-snapshot authority under version skew — Codex R2 finding 2          #
# --------------------------------------------------------------------------- #

async def test_dependency_uses_deployed_snapshot_not_live_draft(monkeypatch):
    """The recursive traversal must resolve unselected calc dependencies from
    the DEPLOYED SNAPSHOT the binder pins, not live draft ORM rows. Version
    skew: the deployed ``inner`` base is p90 but a live draft lowered it to
    sum. The aggregate still stores the deployed p90, so the gate MUST fire
    (return True) using the deployed value — not read the live sum and fail
    open."""
    outer = _calc("outer", 'measure("inner") + 1')
    bq = make_bound_query([make_dimension("country")], [outer])  # only outer resolved
    bq.logical_query.select_expressions = []
    bq.model.deployed_version_id = "v-deployed"

    _patch_parse(monkeypatch, {"outer": ["inner"]})

    # Deployed snapshot: inner.default_agg = p90 (authoritative).
    deployed_inner = make_measure("inner", default_agg="p90")
    shape = types.SimpleNamespace(measures=[deployed_inner])

    async def _fake_shape(model, db):
        return shape

    # The gate lazily imports resolve_deployed_shape from the source module.
    import src.semantic.snapshot_resolver as snap_mod
    monkeypatch.setattr(snap_mod, "resolve_deployed_shape", _fake_shape)

    # Live ORM would return sum (the draft) — but the gate must NOT consult it.
    live_draft_inner = make_measure("inner", default_agg="sum")
    db = _db_returning({"inner": live_draft_inner})

    assert await _query_uses_percentile(bq, db) is True


async def test_dependency_missing_from_deployed_snapshot_fails_closed(monkeypatch):
    """When a deployed shape exists but does not contain a referenced
    dependency, the gate cannot prove the DAG quantile-free from the
    authoritative source -> fail closed (True)."""
    outer = _calc("outer", 'measure("ghost") + 1')
    bq = make_bound_query([make_dimension("country")], [outer])
    bq.logical_query.select_expressions = []
    bq.model.deployed_version_id = "v-deployed"

    _patch_parse(monkeypatch, {"outer": ["ghost"]})
    shape = types.SimpleNamespace(measures=[make_measure("other", default_agg="sum")])

    async def _fake_shape(model, db):
        return shape

    import src.semantic.snapshot_resolver as snap_mod
    monkeypatch.setattr(snap_mod, "resolve_deployed_shape", _fake_shape)

    assert await _query_uses_percentile(bq, AsyncMock()) is True
