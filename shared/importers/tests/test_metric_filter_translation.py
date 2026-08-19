"""Bug-7301 [WRONG NUMBERS]: a dbt metric ``filter`` is scoped to that ONE
metric. The importer must NOT land it in any persona's ``default_filters`` —
persona defaults apply to every measure/dimension the persona exposes, so a
metric-scoped filter written there silently changes the numbers for every other
measure, for everyone using that persona.

The correct behaviour: persist nothing, and surface each filter as a warning so
the modeller recreates it deliberately (fail loud, not silently wrong).

This replaces the F-020-16 suite, which asserted the mis-scoped behaviour
(metric filters written into persona ``default_filters``) — that assertion was
the defect, not a contract.
"""
from __future__ import annotations

from shared.importers.dbt_mapper import _apply_metric_filters
from shared.importers.dbt_parser import DbtMetric
from shared.importers.import_warnings import ImportWarningResponse


def _metric(name: str, filt: str, measure: str = "rev") -> DbtMetric:
    return DbtMetric(
        name=name,
        metric_type="simple",
        type_params={"measure": measure},
        filter=filt,
    )


def _models_with_persona(default_filters=None):
    return [
        {
            "personas": [
                {"slug": "everyone", "default_filters": default_filters or {}}
            ]
        }
    ]


class TestApplyMetricFilters:
    def test_simple_filter_not_written_to_persona_defaults(self):
        # The classic wrong-numbers trap: a completed-orders filter on one
        # metric must NOT become a persona-wide default that scopes every
        # other measure.
        models = _models_with_persona()
        warnings: list[ImportWarningResponse] = []
        _apply_metric_filters(
            [_metric("completed_rev", "{{ Dimension('order__status') }} = 'completed'")],
            models,
            warnings,
        )
        # Nothing landed in the persona filter dict.
        assert models[0]["personas"][0]["default_filters"] == {}
        # And the user is told it was skipped, by metric name.
        assert warnings
        assert any("completed_rev" in w.detail for w in warnings)
        assert any("NOT imported" in w.detail for w in warnings)

    def test_comparison_filter_not_written(self):
        models = _models_with_persona()
        warnings: list[ImportWarningResponse] = []
        _apply_metric_filters(
            [_metric("vip", "{{ Dimension('customer__tier') }} >= 3")],
            models,
            warnings,
        )
        assert models[0]["personas"][0]["default_filters"] == {}
        assert any("vip" in w.detail for w in warnings)

    def test_existing_persona_filter_untouched(self):
        # A real user-authored persona filter must survive the import verbatim —
        # the importer neither adds to nor removes from it.
        models = _models_with_persona({"status": "open"})
        warnings: list[ImportWarningResponse] = []
        _apply_metric_filters(
            [_metric("x", "{{ Dimension('order__status') }} = 'completed'")],
            models,
            warnings,
        )
        assert models[0]["personas"][0]["default_filters"] == {"status": "open"}

    def test_no_filter_metrics_produce_no_warning(self):
        models = _models_with_persona()
        warnings: list[ImportWarningResponse] = []
        _apply_metric_filters(
            [DbtMetric(name="plain", metric_type="simple",
                       type_params={"measure": "rev"}, filter=None)],
            models,
            warnings,
        )
        assert models[0]["personas"][0]["default_filters"] == {}
        assert warnings == []

    def test_multiple_filters_all_reported(self):
        models = _models_with_persona()
        warnings: list[ImportWarningResponse] = []
        _apply_metric_filters(
            [
                _metric("m1", "{{ Dimension('a__x') }} = 1"),
                _metric("m2", "lower(status) like '%done%'"),
            ],
            models,
            warnings,
        )
        assert models[0]["personas"][0]["default_filters"] == {}
        # Both metric names appear in the single aggregated warning.
        joined = " ".join(warning.detail for warning in warnings)
        assert "m1" in joined and "m2" in joined
        assert "2 dbt metric filter(s)" in joined

    def test_more_than_five_filters_are_all_named(self):
        # R1 review: EVERY skipped metric must be named (no truncate-at-5), so
        # the modeller can recreate each — a silently dropped filter is the
        # wrong-numbers trap this fix exists to close.
        models = _models_with_persona()
        warnings: list[ImportWarningResponse] = []
        metrics = [
            _metric(f"metric_{i}", f"{{{{ Dimension('a__x{i}') }}}} = {i}")
            for i in range(7)
        ]
        _apply_metric_filters(metrics, models, warnings)
        assert models[0]["personas"][0]["default_filters"] == {}
        joined = " ".join(warning.detail for warning in warnings)
        for i in range(7):
            assert f"metric_{i}" in joined, f"metric_{i} was dropped from the warning"
        assert "7 dbt metric filter(s)" in joined
