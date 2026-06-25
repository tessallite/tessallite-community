"""F-020-16: dbt metric filters translate to the real persona default_filters
dict shape (not a junk list the query router cannot read)."""
from __future__ import annotations

from shared.importers.dbt_mapper import _apply_metric_filters, _parse_simple_filter
from shared.importers.dbt_parser import DbtMetric


def _metric(name: str, filt: str, measure: str = "rev") -> DbtMetric:
    return DbtMetric(
        name=name,
        metric_type="simple",
        type_params={"measure": measure},
        filter=filt,
    )


def _models_with_persona():
    return [{"personas": [{"slug": "everyone", "default_filters": {}}]}]


class TestParseSimpleFilter:
    def test_string_equality(self):
        dim, op, val = _parse_simple_filter(
            "{{ Dimension('order__status') }} = 'completed'"
        )
        assert dim == "status"
        assert op == "="
        assert val == "completed"

    def test_numeric_comparison(self):
        dim, op, val = _parse_simple_filter(
            "{{ Dimension('customer__tier') }} >= 3"
        )
        assert dim == "tier"
        assert op == ">="
        assert val == 3

    def test_compound_filter_not_parsed(self):
        dim, _, _ = _parse_simple_filter(
            "{{ Dimension('a__x') }} = 1 AND {{ Dimension('a__y') }} = 2"
        )
        assert dim is None

    def test_raw_sql_not_parsed(self):
        dim, _, _ = _parse_simple_filter("revenue > 0")
        assert dim is None


class TestApplyMetricFilters:
    def test_simple_filter_becomes_dict_value(self):
        models = _models_with_persona()
        warnings: list[str] = []
        _apply_metric_filters(
            [_metric("completed_rev", "{{ Dimension('order__status') }} = 'completed'")],
            models,
            warnings,
        )
        df = models[0]["personas"][0]["default_filters"]
        # Real shape: dict keyed by dimension name, scalar for equality.
        assert isinstance(df, dict)
        assert df["status"] == "completed"

    def test_comparison_filter_carries_operator(self):
        models = _models_with_persona()
        _apply_metric_filters(
            [_metric("vip", "{{ Dimension('customer__tier') }} >= 3")],
            models,
            [],
        )
        df = models[0]["personas"][0]["default_filters"]
        assert df["tier"] == {"gte": 3}

    def test_not_equal_filter_uses_canonical_neq(self):
        # Bug-2700: a dbt ``!=`` metric filter must translate to the canonical
        # ``neq`` operator the query-router consumer accepts. The old ``ne``
        # token was silently rejected by persona_gate._coerce_filter, dropping
        # the persona scope-exclusion at query time (wrong/widened data scope).
        models = _models_with_persona()
        _apply_metric_filters(
            [_metric("non_void", "{{ Dimension('order__status') }} != 'void'")],
            models,
            [],
        )
        df = models[0]["personas"][0]["default_filters"]
        assert df["status"] == {"neq": "void"}

    def test_all_mapped_operators_are_consumer_accepted(self):
        # Cross-layer guard: every operator the dbt mapper emits must be in the
        # query-router consumer's accepted operator vocabulary, otherwise the
        # filter is silently dropped at query time.
        from shared.importers.dbt_mapper import _OP_MAP

        # _SUPPORTED_OPERATORS is the authoritative consumer set in
        # query-router/src/security/persona_gate.py. Mirrored here to avoid a
        # cross-service import in the shared package's test suite.
        consumer_accepted = frozenset(
            {"eq", "neq", "gt", "gte", "lt", "lte",
             "in", "not_in", "between", "like", "is_null", "is_not_null"}
        )
        for sql_op, canonical in _OP_MAP.items():
            assert canonical in consumer_accepted, (
                f"dbt op '{sql_op}' maps to '{canonical}', which the persona "
                f"default_filters consumer does not accept"
            )

    def test_complex_filter_stored_nothing_and_warns(self):
        models = _models_with_persona()
        warnings: list[str] = []
        _apply_metric_filters(
            [_metric("x", "lower(status) like '%done%'")],
            models,
            warnings,
        )
        # Nothing junk stored; the persona filter dict stays empty.
        assert models[0]["personas"][0]["default_filters"] == {}
        assert any("too complex" in w for w in warnings)

    def test_existing_persona_filter_not_clobbered(self):
        models = [{"personas": [{"slug": "p", "default_filters": {"status": "open"}}]}]
        _apply_metric_filters(
            [_metric("x", "{{ Dimension('order__status') }} = 'completed'")],
            models,
            [],
        )
        # Existing user filter wins.
        assert models[0]["personas"][0]["default_filters"]["status"] == "open"
