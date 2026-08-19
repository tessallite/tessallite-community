"""Query-router security lane (QR-2) round-2 regression guards.

Reproduce-first, real-path guards for the three Fable deep-review defects that
the isolated producer/renderer seam tests missed:

  * Bug-6383 — the portable-ESCAPE fix was INERT because the binder rebuilt every
    resolved filter POSITIONALLY (``LogicalFilter(name, op, value)``), dropping
    the new ``like_escape`` field before any WHERE renderer saw it. These tests
    drive the REAL binder (``bind_query_to_model``) and prove ``like_escape``
    survives, then drive the REAL transpile boundary and prove the emitted SQL is
    dialect-correct: ``ESCAPE`` on Postgres/SQL Server/Spark, and NO ``ESCAPE`` on
    BigQuery (invalid GoogleSQL — backslash is BigQuery's native LIKE escape).

  * Bug-6139 — the ``$KPIs`` CLS gate was bypassable via legacy
    ``value_measure_id``/``goal_measure_id`` bindings, nested ``kpi()`` refs, and
    unparseable expressions. These tests exercise ``_kpi_allowed_by_persona`` and
    the lineage resolver on every channel and prove fail-closed.

  * Bug-6140 — HAVING was an open threshold oracle (extracted but never gated).
    These tests drive ``_restricted_having_columns`` and prove a HAVING over a
    restricted-column measure / bare restricted column is blocked, and that an
    unparseable HAVING fails closed.
"""
from __future__ import annotations

import types
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
import sqlglot

# Importing dialects registers the BigQuery generator patches (incl. the
# Bug-6383 ESCAPE-drop). Production always imports it via source_sql; the tests
# transpile through the same helpers so the patch is in force.
from src.rewrite import dialects as _dialects  # noqa: F401
from src.rewrite.dialects import _transpile_to_dialect
from src.rewrite.conditions import _render_where
from src.ir.logical_query import LogicalFilter, LogicalQuery
from src.semantic.binder import bind_query_to_model

_P = "src.semantic.binder"


# ---------------------------------------------------------------------------
# Bug-6383 — like_escape survives the binder positional rebuild (REAL path)
# ---------------------------------------------------------------------------

def _query(filters: list[LogicalFilter]) -> LogicalQuery:
    return LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT 1",
        requested_measures=["revenue"],
        requested_dimensions=["name"],
        filters=filters,
        grain=["name"],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp",
    )


def _dim(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"d-{name}", name=name,
        source_column_id=f"col-{name}", user_defined_attribute_id=None,
    )


def _meas(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=f"m-{name}", name=name, default_agg="sum", is_additive=True,
        source_column_id=f"col-{name}", user_defined_attribute_id=None,
        measure_type="standard", expression=None, calc_agg_mode=None,
        semi_additive_behavior=None, variant_kind=None, variant_of_measure_id=None,
    )


def _patches(dimensions, measures):
    model = types.SimpleNamespace(id="model-1", slug="testmodel", deployed_version_id="v1")
    # A2: the binder resolves a deployed model's semantic shape from the deployed
    # snapshot (A1). Patch resolve_deployed_shape in the binder's namespace to return
    # a shape containing the test's dims/measures so the deployed path serves them.
    _shape = types.SimpleNamespace(
        measures=measures,
        dimensions=dimensions,
        hidden_column_ids=set(),
        physical_columns_all=set(),
        physical_columns_visible=set(),
        physical_column_ids={},
        columns_by_id={},
        hierarchy_rows=[],
        attribute_relationships=[],
        qualified_column_ids={},
        table_name_ids={},
    )
    stack = ExitStack()
    stack.enter_context(patch(f"{_P}._load_model", new=AsyncMock(return_value=model)))
    stack.enter_context(patch(f"{_P}.resolve_deployed_shape", new=AsyncMock(return_value=_shape)))
    stack.enter_context(patch(f"{_P}._load_measures", new=AsyncMock(return_value=measures)))
    stack.enter_context(patch(f"{_P}._load_dimensions", new=AsyncMock(return_value=dimensions)))
    stack.enter_context(patch(f"{_P}._load_hidden_column_ids", new=AsyncMock(return_value=set())))
    stack.enter_context(patch(f"{_P}._load_hierarchy_level_dimensions", new=AsyncMock(return_value=[])))
    return stack


async def _bind_one(filt: LogicalFilter) -> LogicalFilter:
    with _patches([_dim("name")], [_meas("revenue")]):
        bound = await bind_query_to_model(_query([filt]), AsyncMock())
    assert len(bound.resolved_filters) == 1
    return bound.resolved_filters[0]


async def test_binder_preserves_like_escape_exact_match():
    # contains → operator "like", like_escape "\\". The binder resolves the
    # dimension via the EXACT-match branch and must NOT drop like_escape.
    rf = await _bind_one(LogicalFilter("name", "like", "%10\\%\\_x%", like_escape="\\"))
    assert rf.like_escape == "\\", "binder dropped like_escape on exact-match rebuild"


async def test_binder_preserves_like_escape_case_insensitive_match():
    # A differently-cased filter name resolves via the case-insensitive branch,
    # which rebuilds with the canonical name — like_escape must still survive.
    rf = await _bind_one(LogicalFilter("NAME", "not_like", "%a\\_b%", like_escape="\\"))
    assert rf.dimension_name == "name"
    assert rf.like_escape == "\\", "binder dropped like_escape on case-fold rebuild"


async def test_binder_preserves_none_like_escape_for_raw_like():
    # A raw like (no escape intent) must stay None — not coerced to a value.
    rf = await _bind_one(LogicalFilter("name", "like", "E%", like_escape=None))
    assert rf.like_escape is None


async def test_full_path_escape_is_dialect_correct():
    # bind → render → transpile. Postgres/SQL Server/Spark carry ESCAPE; BigQuery
    # must NOT (invalid GoogleSQL — backslash is BigQuery's native LIKE escape).
    rf = await _bind_one(LogicalFilter("name", "like", "%10\\%\\_x%", like_escape="\\"))
    where = _render_where([rf], {"name": '"name"'}, "postgresql", None)
    assert "ESCAPE" in where  # postgres-canonical render carries it
    pg_stmt = f"SELECT 1 FROM t WHERE {where}"

    # BigQuery: ESCAPE must be dropped by the transpile boundary.
    bq = _transpile_to_dialect(pg_stmt, "bigquery")
    assert "ESCAPE" not in bq.upper(), f"BigQuery must not emit ESCAPE: {bq}"
    # The escaped pattern must survive so literal % / _ still match literally.
    node = sqlglot.parse_one(bq, read="bigquery")
    assert node.find(sqlglot.exp.Like) is not None
    assert node.find(sqlglot.exp.Escape) is None

    # SQL Server + Spark: ESCAPE is REQUIRED (no native backslash escape) and
    # must be preserved.
    for tgt in ("tsql", "spark"):
        out = _transpile_to_dialect(pg_stmt, tgt)
        assert "ESCAPE" in out.upper(), f"{tgt} must keep ESCAPE: {out}"
        esc = sqlglot.parse_one(out, read=tgt).find(sqlglot.exp.Escape)
        assert esc is not None and esc.expression.this


async def test_not_like_escape_dropped_on_bigquery():
    rf = await _bind_one(LogicalFilter("name", "not_like", "%a\\_b%", like_escape="\\"))
    where = _render_where([rf], {"name": '"name"'}, "postgresql", None)
    bq = _transpile_to_dialect(f"SELECT 1 FROM t WHERE {where}", "bigquery")
    assert "ESCAPE" not in bq.upper()


# ---------------------------------------------------------------------------
# Bug-6609 (Fable R2): the AGGREGATE route emits its WHERE without a sqlglot
# pass, so a contains/notContains filter routed to a BigQuery/Spark-target
# aggregate emitted invalid `LIKE ... ESCAPE '\'`. The WHERE is now transpiled.
# ---------------------------------------------------------------------------

from conftest import (  # noqa: E402
    make_agg_col, make_aggregate, make_bound_query, make_dimension, make_measure,
)
from src.rewrite.query_rewriter import rewrite_for_aggregate  # noqa: E402


def _agg_bq_with_like_filter():
    m = make_measure("amount", default_agg="sum")
    d = make_dimension("region")
    agg = make_aggregate(["region"], [make_agg_col(m, "sum")])
    agg.grain_physical_cols = ["region"]
    bq = make_bound_query(
        [d], [m], grain=["region"],
        filters=[LogicalFilter("region", "like", "%10\\%\\_x%", like_escape="\\")],
    )
    return bq, agg


class TestAggregateRouteEscape:
    def test_bigquery_aggregate_where_drops_escape_and_is_valid(self):
        bq, agg = _agg_bq_with_like_filter()
        sql = rewrite_for_aggregate(bq, agg, target_dialect="bigquery")
        assert "ESCAPE" not in sql.upper(), f"BigQuery aggregate must not emit ESCAPE: {sql}"
        # Must be valid GoogleSQL: parses, LIKE present, no Escape wrapper.
        tree = sqlglot.parse_one(sql, read="bigquery")
        assert tree.find(sqlglot.exp.Like) is not None
        assert tree.find(sqlglot.exp.Escape) is None

    @pytest.mark.parametrize("dialect", ["tsql", "spark"])
    def test_escape_preserved_on_supporting_aggregate_targets(self, dialect):
        bq, agg = _agg_bq_with_like_filter()
        sql = rewrite_for_aggregate(bq, agg, target_dialect=dialect)
        assert "ESCAPE" in sql.upper(), f"{dialect} aggregate must keep ESCAPE: {sql}"
        esc = sqlglot.parse_one(sql, read=dialect).find(sqlglot.exp.Escape)
        assert esc is not None and esc.expression.this

    def test_postgres_aggregate_where_unchanged(self):
        bq, agg = _agg_bq_with_like_filter()
        sql = rewrite_for_aggregate(bq, agg, target_dialect="postgres")
        assert "ESCAPE" in sql.upper()
        assert sqlglot.parse_one(sql, read="postgres").find(sqlglot.exp.Like) is not None


# ---------------------------------------------------------------------------
# Bug-6139 — $KPIs CLS gate closes EVERY channel (legacy ids, nested kpi(),
# unparseable expressions). These drive the real gate decision function.
# ---------------------------------------------------------------------------

from src.api.routes import _kpi_allowed_by_persona  # noqa: E402

_RESTRICTED = "11111111-1111-1111-1111-111111111111"
_CLEAN = "22222222-2222-2222-2222-222222222222"


def _kpi(**kw) -> types.SimpleNamespace:
    base = dict(
        id=None, name=None, expression=None, target_expression=None,
        status_expression=None, trend_expression=None,
        value_measure_id=None, goal_measure_id=None, target_measure_id=None,
        kpi_type=None, parent_kpi_id=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


class TestKpiLegacyMeasureIdBindings:
    """Legacy value_measure_id / goal_measure_id were ignored by the gate, so a
    KPI bound to a restricted-column measure via the legacy binding leaked."""

    def test_value_measure_id_reaching_restricted_column_is_withheld(self):
        kpi = _kpi(id="k1", name="Legacy Value KPI", value_measure_id=_RESTRICTED)
        assert _kpi_allowed_by_persona(
            kpi, None, {}, cls_blocked_measure_ids=frozenset({_RESTRICTED}),
        ) is False

    def test_goal_measure_id_reaching_restricted_column_is_withheld(self):
        kpi = _kpi(id="k2", name="Legacy Goal KPI", goal_measure_id=_RESTRICTED)
        assert _kpi_allowed_by_persona(
            kpi, None, {}, cls_blocked_measure_ids=frozenset({_RESTRICTED}),
        ) is False

    def test_value_measure_id_outside_allow_list_is_withheld(self):
        kpi = _kpi(id="k3", name="Legacy KPI", value_measure_id=_RESTRICTED)
        assert _kpi_allowed_by_persona(kpi, {_CLEAN}, {}) is False

    def test_legacy_binding_clear_of_restriction_is_served(self):
        kpi = _kpi(id="k4", name="Clean Legacy KPI", value_measure_id=_CLEAN)
        assert _kpi_allowed_by_persona(
            kpi, None, {}, cls_blocked_measure_ids=frozenset({_RESTRICTED}),
        ) is True


class TestKpiNestedReferences:
    """A composite KPI referencing another KPI via kpi("Name") must inherit the
    nested KPI's lineage — the old gate only scanned measure() refs, so a
    pre-aggregated composite bypassed both gates entirely."""

    def _maps(self):
        kpi_b = _kpi(id="b", name="Base", expression='measure("Salary")')
        kpi_by_name = {"Base": kpi_b, "base": kpi_b}
        measure_name_to_id = {"Salary": _RESTRICTED}
        return kpi_b, kpi_by_name, measure_name_to_id

    def test_nested_kpi_reaching_restricted_column_is_withheld(self):
        _b, kpi_by_name, m2id = self._maps()
        composite = _kpi(id="a", name="Composite", expression='kpi("Base")')
        assert _kpi_allowed_by_persona(
            composite, None, m2id,
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
            kpi_by_name=kpi_by_name,
        ) is False

    def test_nested_kpi_measure_outside_allow_list_is_withheld(self):
        _b, kpi_by_name, m2id = self._maps()
        composite = _kpi(id="a", name="Composite", expression='kpi("Base")')
        assert _kpi_allowed_by_persona(
            composite, {_CLEAN}, m2id, kpi_by_name=kpi_by_name,
        ) is False

    def test_unresolvable_nested_kpi_name_fails_closed(self):
        composite = _kpi(id="a", name="Composite", expression='kpi("Ghost")')
        assert _kpi_allowed_by_persona(
            composite, None, {}, cls_blocked_measure_ids=frozenset({_RESTRICTED}),
            kpi_by_name={},
        ) is False

    def test_nested_kpi_clear_of_restriction_is_served(self):
        kpi_b = _kpi(id="b", name="Base", expression='measure("Sales")')
        composite = _kpi(id="a", name="Composite", expression='kpi("Base")')
        assert _kpi_allowed_by_persona(
            composite, None, {"Sales": _CLEAN},
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
            kpi_by_name={"Base": kpi_b},
        ) is True

    def test_reference_cycle_is_safe_and_served_when_clear(self):
        # A ↔ B cycle must not recurse forever; with clean lineage it serves.
        kpi_a = _kpi(id="a", name="A", expression='kpi("B")')
        kpi_b = _kpi(id="b", name="B", expression='kpi("A")')
        assert _kpi_allowed_by_persona(
            kpi_a, None, {}, cls_blocked_measure_ids=frozenset({_RESTRICTED}),
            kpi_by_name={"A": kpi_a, "B": kpi_b},
        ) is True


class TestKpiCompositeChildren:
    """R1 (opus-4-8): a composite KPI's served value comes from children bound via
    parent_kpi_id, NOT its own (placeholder) expression. The gate must fold in
    every child's lineage or a composite over a restricted-column child leaks."""

    def test_composite_child_reaching_restricted_column_is_withheld(self):
        # Composite scorecard whose own expression is a harmless placeholder, but
        # whose child aggregates a restricted column.
        child = _kpi(id="child", name="SalaryLeaf", expression='measure("AvgSalary")')
        composite = _kpi(
            id="scorecard", name="Scorecard", kpi_type="composite",
            expression="literal(0)",
        )
        children_by_parent = {"scorecard": [child]}
        assert _kpi_allowed_by_persona(
            composite, None, {"AvgSalary": _RESTRICTED},
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
            kpi_by_name={"SalaryLeaf": child}, children_by_parent=children_by_parent,
        ) is False

    def test_composite_child_via_legacy_value_binding_is_withheld(self):
        # Child binds the restricted measure via the legacy value_measure_id.
        child = _kpi(id="child", name="Leaf", value_measure_id=_RESTRICTED)
        composite = _kpi(id="sc", name="SC", kpi_type="composite", expression="literal(0)")
        assert _kpi_allowed_by_persona(
            composite, None, {},
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
            kpi_by_name={"Leaf": child}, children_by_parent={"sc": [child]},
        ) is False

    def test_nested_composite_grandchild_is_withheld(self):
        # Composite -> composite -> leaf(restricted): transitive child walk.
        leaf = _kpi(id="leaf", name="Leaf", expression='measure("Sal")')
        mid = _kpi(id="mid", name="Mid", kpi_type="composite", expression="literal(0)")
        top = _kpi(id="top", name="Top", kpi_type="composite", expression="literal(0)")
        children_by_parent = {"top": [mid], "mid": [leaf]}
        assert _kpi_allowed_by_persona(
            top, None, {"Sal": _RESTRICTED},
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
            kpi_by_name={}, children_by_parent=children_by_parent,
        ) is False

    def test_composite_with_clean_children_is_served(self):
        child = _kpi(id="child", name="Leaf", expression='measure("Sales")')
        composite = _kpi(id="sc", name="SC", kpi_type="composite", expression="literal(0)")
        assert _kpi_allowed_by_persona(
            composite, None, {"Sales": _CLEAN},
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
            kpi_by_name={"Leaf": child}, children_by_parent={"sc": [child]},
        ) is True

    def test_composite_child_outside_allow_list_is_withheld(self):
        child = _kpi(id="child", name="Leaf", expression='measure("Sales")')
        composite = _kpi(id="sc", name="SC", kpi_type="composite", expression="literal(0)")
        assert _kpi_allowed_by_persona(
            composite, {_CLEAN}, {"Sales": _RESTRICTED},
            kpi_by_name={"Leaf": child}, children_by_parent={"sc": [child]},
        ) is False


class TestKpiInterceptPersonaLoad:
    """Bug-6610 (opus R3 test-escape guard): the $KPIs intercept in
    _handle_execute must RESOLVE the persona from persona_id when only the id is
    supplied (persona=None), so the CLS/allow-list gate is never inert depending
    on the caller's persona-passing convention."""

    def _kpi_request(self):
        from src.api.routes import ExecuteRequest
        return ExecuteRequest(model_id="model-1", raw_query="SELECT * FROM m$KPIs", protocol="jdbc")

    def _kpi_logical_query(self):
        lq = LogicalQuery(
            model_id="model-1", protocol="jdbc", raw_query="SELECT * FROM m$KPIs",
            requested_measures=[], requested_dimensions=[], filters=[], grain=[],
            order_by=[], limit=None, offset=None, query_fingerprint="fp",
        )
        lq.from_tables = ["m$kpis"]
        return lq

    async def test_intercept_loads_persona_from_persona_id(self):
        from unittest.mock import AsyncMock as _AM
        import src.api.routes as routes
        sentinel = types.SimpleNamespace(id="persona-1")
        captured = {}

        async def _fake_handle(db, model_id, lq, *, persona, **kw):
            captured["persona"] = persona
            return "OK"

        with patch.object(routes, "_bind_query_parameters", new=_AM(return_value=None)), \
             patch.object(routes, "_parse", new=lambda body: self._kpi_logical_query()), \
             patch.object(routes, "load_persona", new=_AM(return_value=sentinel)) as lp, \
             patch.object(routes, "_handle_kpi_table_query", new=_fake_handle):
            await routes._handle_execute(
                self._kpi_request(), AsyncMock(), user_identity="u@t.com",
                persona=None, persona_id="persona-1",
            )
        lp.assert_awaited_once()
        assert captured["persona"] is sentinel, "intercept must load persona from persona_id"

    async def test_intercept_uses_supplied_persona_without_reload(self):
        from unittest.mock import AsyncMock as _AM
        import src.api.routes as routes
        supplied = types.SimpleNamespace(id="persona-2")
        captured = {}

        async def _fake_handle(db, model_id, lq, *, persona, **kw):
            captured["persona"] = persona
            return "OK"

        with patch.object(routes, "_bind_query_parameters", new=_AM(return_value=None)), \
             patch.object(routes, "_parse", new=lambda body: self._kpi_logical_query()), \
             patch.object(routes, "load_persona", new=_AM(return_value=None)) as lp, \
             patch.object(routes, "_handle_kpi_table_query", new=_fake_handle):
            await routes._handle_execute(
                self._kpi_request(), AsyncMock(), user_identity="u@t.com",
                persona=supplied, persona_id="persona-2",
            )
        lp.assert_not_awaited()
        assert captured["persona"] is supplied


class TestKpiUnparseableExpression:
    """An unparseable expression could reference anything — fail closed rather
    than serve it as if it had no lineage (the old extract_measure_names
    swallowed parse errors and returned [])."""

    def test_bare_division_expression_fails_closed(self):
        # Direct division raises in the DSL parser (must use safe_div).
        kpi = _kpi(id="k", name="Bad", expression='measure("A") / measure("B")')
        assert _kpi_allowed_by_persona(
            kpi, None, {"A": _CLEAN, "B": _CLEAN},
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
        ) is False

    def test_garbage_expression_fails_closed(self):
        kpi = _kpi(id="k", name="Bad", expression="@@@ not valid @@@")
        assert _kpi_allowed_by_persona(kpi, {_CLEAN}, {}) is False

    def test_unparseable_target_expression_fails_closed(self):
        # target_expression feeds the served ``target`` value — an unparseable
        # one must fail closed.
        kpi = _kpi(id="k", name="Bad", target_expression='measure("A") / 0')
        assert _kpi_allowed_by_persona(
            kpi, None, {"A": _CLEAN},
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
        ) is False

    def test_legacy_status_expression_not_scanned(self):
        # The legacy v1 status_expression is NOT a served-value lineage channel
        # (no application code reads it), so a stale non-v2 status_expression must
        # NOT fail-close an otherwise-clean KPI — availability guard.
        kpi = _kpi(id="k", name="Clean", expression='measure("A")',
                   status_expression="legacy v1 junk (not v2 dsl)")
        assert _kpi_allowed_by_persona(
            kpi, None, {"A": _CLEAN},
            cls_blocked_measure_ids=frozenset({_RESTRICTED}),
        ) is True

    def test_no_gate_active_serves_regardless(self):
        # With no restriction in force the gate is inactive and serves.
        kpi = _kpi(id="k", name="Bad", expression="@@@ garbage @@@")
        assert _kpi_allowed_by_persona(kpi, None, {}) is True
