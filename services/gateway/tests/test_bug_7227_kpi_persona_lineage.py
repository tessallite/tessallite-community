"""Bug-7227: XMLA/JDBC KPI list must be lineage-filtered, not blanked, for a
measure-restricted persona.

Before this fix the gateway did ``kpis = []`` whenever a persona carried a
populated ``included_measure_ids`` allow-list, so a persona restricted to some
measures saw ZERO KPIs in Excel/Power BI — even the KPIs whose underlying
measures it WAS allowed to read. The fix (``filter_kpis_for_persona``) mirrors
the query-router ``$KPIs`` policy (``_kpi_allowed_by_persona`` /
``_kpi_lineage_measure_ids``): a KPI is advertised iff EVERY measure in its
transitive lineage is inside the allow-list; anything whose lineage cannot be
fully verified is withheld (fail closed).

Core invariant under test: a restricted persona sees EXACTLY the allowed KPI
subset — not empty, not all.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax.kpi_persona_filter import (
    _kpi_allowed_by_persona,
    _kpi_lineage_measure_ids,
    filter_kpis_for_persona,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MEASURES = [
    {"id": "m_rev", "name": "revenue"},
    {"id": "m_cost", "name": "cost"},
    {"id": "m_units", "name": "units"},
    {"id": "m_secret", "name": "secret_amount"},
]

# revenue-only KPI (direct value binding).
_KPI_REVENUE = {
    "id": "k_rev",
    "name": "total_revenue",
    "value_measure_id": "m_rev",
    "goal_measure_id": None,
    "expression": "",
    "target_expression": None,
    "parent_kpi_id": None,
}

# value revenue + goal cost (two direct bindings).
_KPI_MARGIN = {
    "id": "k_margin",
    "name": "margin",
    "value_measure_id": "m_rev",
    "goal_measure_id": "m_cost",
    "expression": "",
    "target_expression": None,
    "parent_kpi_id": None,
}

# v2 expression KPI referencing units via measure("units").
_KPI_UNITS_EXPR = {
    "id": "k_units",
    "name": "unit_velocity",
    "value_measure_id": None,
    "goal_measure_id": None,
    "expression": 'measure("units")',
    "target_expression": None,
    "parent_kpi_id": None,
}

# KPI whose lineage reaches a restricted measure via its expression.
_KPI_SECRET = {
    "id": "k_secret",
    "name": "secret_ratio",
    "value_measure_id": None,
    "goal_measure_id": None,
    "expression": 'safe_div(measure("secret_amount"), measure("revenue"))',
    "target_expression": None,
    "parent_kpi_id": None,
}


class TestLineageResolution:
    def test_direct_value_binding(self):
        ids, ok = _kpi_lineage_measure_ids(_KPI_REVENUE, {}, {}, {})
        assert ok is True
        assert ids == {"m_rev"}

    def test_value_and_goal_bindings(self):
        ids, ok = _kpi_lineage_measure_ids(_KPI_MARGIN, {}, {}, {})
        assert ok is True
        assert ids == {"m_rev", "m_cost"}

    def test_expression_measure_ref_resolves(self):
        name_to_id = {"units": "m_units"}
        ids, ok = _kpi_lineage_measure_ids(_KPI_UNITS_EXPR, {}, name_to_id, {})
        assert ok is True
        assert ids == {"m_units"}

    def test_expression_measure_name_unresolvable_fails_closed(self):
        # No name->id mapping for "units" -> lineage cannot be verified.
        ids, ok = _kpi_lineage_measure_ids(_KPI_UNITS_EXPR, {}, {}, {})
        assert ok is False

    def test_unparseable_expression_fails_closed(self):
        kpi = {**_KPI_UNITS_EXPR, "expression": "this is ((( not valid"}
        _ids, ok = _kpi_lineage_measure_ids(kpi, {}, {"units": "m_units"}, {})
        assert ok is False


class TestPersonaAllowList:
    def test_unrestricted_allows_all(self):
        # None allow-list -> serve.
        assert _kpi_allowed_by_persona(_KPI_MARGIN, None, {}, {}, {}) is True

    def test_full_lineage_in_allow_list_allows(self):
        assert _kpi_allowed_by_persona(
            _KPI_MARGIN, {"m_rev", "m_cost"}, {}, {}, {},
        ) is True

    def test_missing_lineage_measure_withholds(self):
        # goal m_cost not in allow-list.
        assert _kpi_allowed_by_persona(
            _KPI_MARGIN, {"m_rev"}, {}, {}, {},
        ) is False

    def test_unverifiable_lineage_withholds(self):
        # Expression references units but no name->id map -> fail closed even if
        # the allow-list is broad.
        assert _kpi_allowed_by_persona(
            _KPI_UNITS_EXPR, {"m_units"}, {}, {}, {},
        ) is False


class TestFilterSubsetInvariant:
    """A restricted persona sees EXACTLY the allowed subset — not empty, not all."""

    def _kpis(self):
        return [
            dict(_KPI_REVENUE),
            dict(_KPI_MARGIN),
            dict(_KPI_UNITS_EXPR),
        ]

    def test_no_restriction_returns_all(self):
        result = filter_kpis_for_persona(self._kpis(), _MEASURES, None)
        assert [k["name"] for k in result] == [
            "total_revenue", "margin", "unit_velocity",
        ]

    def test_empty_allow_list_returns_all(self):
        result = filter_kpis_for_persona(self._kpis(), _MEASURES, set())
        assert len(result) == 3

    def test_revenue_only_persona_sees_only_revenue_kpi(self):
        # Allow revenue only: total_revenue kept; margin needs cost -> dropped;
        # unit_velocity needs units -> dropped. NOT empty, NOT all.
        result = filter_kpis_for_persona(self._kpis(), _MEASURES, {"m_rev"})
        assert [k["name"] for k in result] == ["total_revenue"]

    def test_revenue_and_cost_persona_sees_revenue_and_margin(self):
        result = filter_kpis_for_persona(
            self._kpis(), _MEASURES, {"m_rev", "m_cost"},
        )
        assert [k["name"] for k in result] == ["total_revenue", "margin"]

    def test_units_persona_sees_only_unit_velocity(self):
        result = filter_kpis_for_persona(self._kpis(), _MEASURES, {"m_units"})
        assert [k["name"] for k in result] == ["unit_velocity"]

    def test_order_preserved(self):
        result = filter_kpis_for_persona(
            self._kpis(), _MEASURES, {"m_rev", "m_cost", "m_units"},
        )
        assert [k["name"] for k in result] == [
            "total_revenue", "margin", "unit_velocity",
        ]

    def test_restricted_measure_kpi_withheld_when_not_allowed(self):
        # secret_ratio reaches m_secret; a persona without it must not see it,
        # even though it also references the allowed revenue measure.
        kpis = [dict(_KPI_REVENUE), dict(_KPI_SECRET)]
        result = filter_kpis_for_persona(kpis, _MEASURES, {"m_rev"})
        assert [k["name"] for k in result] == ["total_revenue"]

    def test_scoped_measure_set_withholds_kpi_over_trimmed_measure(self):
        # Simulates the XMLA/model-service path where measures_meta is already
        # persona-scoped: if a lineage measure name is absent from the scoped
        # measure set, name->id fails and the KPI is fail-closed withheld even
        # though its id happens to be in the allow-list.
        scoped_measures = [{"id": "m_rev", "name": "revenue"}]  # no "units"
        result = filter_kpis_for_persona(
            [dict(_KPI_UNITS_EXPR)], scoped_measures, {"m_units"},
        )
        assert result == []


class TestCompositeAndNestedLineage:
    """Composite children (parent_kpi_id) and nested kpi() refs fold into the
    parent's lineage (union), matching the query-router policy."""

    def test_composite_parent_folds_child_lineage(self):
        parent = {
            "id": "k_parent",
            "name": "scorecard",
            "value_measure_id": None,
            "expression": "",  # composite placeholder
            "parent_kpi_id": None,
        }
        child_a = {
            "id": "k_child_a",
            "name": "child_a",
            "value_measure_id": "m_rev",
            "expression": "",
            "parent_kpi_id": "k_parent",
        }
        child_b = {
            "id": "k_child_b",
            "name": "child_b",
            "value_measure_id": "m_cost",
            "expression": "",
            "parent_kpi_id": "k_parent",
        }
        kpis = [parent, child_a, child_b]

        # Parent needs BOTH children's measures. Allow only m_rev -> parent and
        # child_b withheld; child_a (revenue-only) kept.
        result = filter_kpis_for_persona(kpis, _MEASURES, {"m_rev"})
        assert [k["name"] for k in result] == ["child_a"]

        # Allow both -> all three visible.
        result_full = filter_kpis_for_persona(kpis, _MEASURES, {"m_rev", "m_cost"})
        assert {k["name"] for k in result_full} == {
            "scorecard", "child_a", "child_b",
        }

    def test_nested_kpi_reference_folds_lineage(self):
        base = {
            "id": "k_base",
            "name": "base_kpi",
            "value_measure_id": "m_cost",
            "expression": "",
            "parent_kpi_id": None,
        }
        wrapper = {
            "id": "k_wrap",
            "name": "wrapper_kpi",
            "value_measure_id": "m_rev",
            "expression": 'kpi("base_kpi")',
            "parent_kpi_id": None,
        }
        kpis = [base, wrapper]
        # wrapper needs m_rev (own) + m_cost (nested base). Allow only m_rev ->
        # wrapper withheld; base needs m_cost -> also withheld.
        result = filter_kpis_for_persona(kpis, _MEASURES, {"m_rev"})
        assert result == []
        # Allow both -> both visible.
        result_full = filter_kpis_for_persona(kpis, _MEASURES, {"m_rev", "m_cost"})
        assert {k["name"] for k in result_full} == {"base_kpi", "wrapper_kpi"}

    def test_unresolvable_nested_kpi_fails_closed(self):
        wrapper = {
            "id": "k_wrap",
            "name": "wrapper_kpi",
            "value_measure_id": "m_rev",
            "expression": 'kpi("does_not_exist")',
            "parent_kpi_id": None,
        }
        # Nested kpi name not found -> fail closed even with a broad allow-list.
        result = filter_kpis_for_persona([wrapper], _MEASURES, {"m_rev"})
        assert result == []

    def test_cycle_between_kpis_is_safe(self):
        a = {
            "id": "k_a", "name": "a", "value_measure_id": "m_rev",
            "expression": 'kpi("b")', "parent_kpi_id": None,
        }
        b = {
            "id": "k_b", "name": "b", "value_measure_id": "m_cost",
            "expression": 'kpi("a")', "parent_kpi_id": None,
        }
        # Cycle must not hang; both resolve to {m_rev, m_cost}.
        result = filter_kpis_for_persona([a, b], _MEASURES, {"m_rev", "m_cost"})
        assert {k["name"] for k in result} == {"a", "b"}
        # Restrict to m_rev -> both withheld (each needs the other's measure).
        result_r = filter_kpis_for_persona([a, b], _MEASURES, {"m_rev"})
        assert result_r == []
