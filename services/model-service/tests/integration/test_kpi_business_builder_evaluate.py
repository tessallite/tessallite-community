"""
Integration tests for KPI Business Builder evaluate-adhoc endpoint.

Requires live Docker services (model-service, query-router, postgres).
Uses the acme-demo tenant with seeded data.

All entity IDs are resolved dynamically by name — no hardcoded UUIDs.

Run:
    cd tessallite/services/model-service
    INTEGRATION_TEST_API_BASE=http://localhost:8001/api/v1 \
      pytest tests/integration/test_kpi_business_builder_evaluate.py -v
"""
from __future__ import annotations

import os

import httpx
import pytest

from .conftest import API_BASE, TENANT_ID, EMAIL, PASSWORD, _measure_id, _dimension_id

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def _require_kpi_model(_measures):
    """Skip the whole KPI module when the active model lacks the KPI measures.
    These tests assume the dev acme-demo `modelx` (Revenue/net_sales/...); on the
    demo bundle's `modely` they are absent, so skip cleanly (Bug-5453/5498)."""
    if "net_sales" not in {m.get("name") for m in _measures}:
        pytest.skip(
            "active model lacks KPI measures (net_sales/...) — needs the dev "
            "acme-demo modelx profile (Bug-5453/5498)"
        )


@pytest.fixture(scope="module")
def evaluate_url(project_id, model_id):
    return f"{API_BASE}/projects/{project_id}/models/{model_id}/kpis/evaluate-adhoc"


@pytest.fixture(scope="module")
def measure_revenue_id(_measures):
    return _measure_id(_measures, "Revenue")


@pytest.fixture(scope="module")
def measure_gross_margin_pct_id(_measures):
    return _measure_id(_measures, "gross_margin_pct")


@pytest.fixture(scope="module")
def measure_transaction_count_id(_measures):
    return _measure_id(_measures, "transaction_count")


@pytest.fixture(scope="module")
def measure_net_sales_id(_measures):
    return _measure_id(_measures, "net_sales")


@pytest.fixture(scope="module")
def measure_discount_amount_id(_measures):
    return _measure_id(_measures, "discount_amount")


@pytest.fixture(scope="module")
def dim_business_date_id(_dimensions):
    return _dimension_id(_dimensions, "business_date")


@pytest.fixture(scope="module")
def dim_customer_segment_id(_dimensions):
    return _dimension_id(_dimensions, "customer_segment")


@pytest.fixture(scope="module")
def dim_product_name_id(_dimensions):
    return _dimension_id(_dimensions, "product_name")


def _evaluate(evaluate_url, headers, business_definition: dict) -> dict:
    resp = httpx.post(
        evaluate_url,
        json={"business_definition": business_definition},
        headers=headers,
        timeout=30.0,
    )
    assert resp.status_code == 200, f"Evaluate failed ({resp.status_code}): {resp.text}"
    return resp.json()


def _base_single_measure_bd(
    measure_id: str,
    time_window: dict | None = None,
    filters: list | None = None,
):
    bd = {
        "builder": "business_kpi",
        "version": 1,
        "formula": {
            "type": "single_measure",
            "measure_id": measure_id,
        },
        "filters": filters or [],
    }
    if time_window is not None:
        bd["time_window"] = time_window
    return bd


# ---------------------------------------------------------------------------
# Time window preset tests — prove presets change the evaluation result
# ---------------------------------------------------------------------------


class TestTimeWindowPresets:
    """Prove that different time window presets produce different scalar values."""

    def test_no_time_window_returns_value(
        self, headers, evaluate_url, measure_revenue_id,
    ):
        bd = _base_single_measure_bd(measure_revenue_id)
        result = _evaluate(evaluate_url, headers, bd)
        assert result["value"] is not None

    def test_last_12_months_differs_from_unscoped(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        bd_all = _base_single_measure_bd(measure_revenue_id)
        bd_12m = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
        )
        r_all = _evaluate(evaluate_url, headers, bd_all)
        r_12m = _evaluate(evaluate_url, headers, bd_12m)
        assert r_all["value"] is not None
        assert r_12m["value"] is not None
        assert r_all["value"] != r_12m["value"], (
            "unscoped and last_12_months should produce different Revenue totals"
        )

    def test_last_6_months_differs_from_last_12_months(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        bd_6m = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_6_months",
                "dimension_id": dim_business_date_id,
            },
        )
        bd_12m = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
        )
        r_6m = _evaluate(evaluate_url, headers, bd_6m)
        r_12m = _evaluate(evaluate_url, headers, bd_12m)
        assert r_12m["value"] is not None
        if r_6m["value"] is not None:
            assert r_6m["value"] != r_12m["value"], (
                "last_6_months and last_12_months should produce different totals"
            )

    def test_time_window_emits_predicates(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        bd = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
        )
        result = _evaluate(evaluate_url, headers, bd)
        scope = result.get("compiled_scope", {})
        tw_preds = scope.get("time_window_predicates", [])
        assert len(tw_preds) >= 1, "Time window should emit predicates"
        assert any("business_date" in p for p in tw_preds)


# ---------------------------------------------------------------------------
# Dimension filter tests — prove filter predicates are compiled and applied
# ---------------------------------------------------------------------------


class TestDimensionFilters:
    """Prove that adding dimension filters compiles correct predicates."""

    def test_fixed_filter_emits_predicate(
        self, headers, evaluate_url, measure_revenue_id, dim_customer_segment_id,
    ):
        bd = _base_single_measure_bd(
            measure_revenue_id,
            filters=[
                {
                    "dimension_id": dim_customer_segment_id,
                    "operator": "in",
                    "mode": "fixed",
                    "values": ["Retail", "Corporate"],
                }
            ],
        )
        result = _evaluate(evaluate_url, headers, bd)
        scope = result.get("compiled_scope", {})
        filter_preds = scope.get("filter_predicates", [])
        assert len(filter_preds) == 1
        assert "customer_segment" in filter_preds[0]
        assert "Retail" in filter_preds[0]
        assert "Corporate" in filter_preds[0]

    def test_relative_date_filter_passes_validation(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        """Relative date filter passes validation and compiles predicates."""
        bd = _base_single_measure_bd(
            measure_revenue_id,
            filters=[
                {
                    "dimension_id": dim_business_date_id,
                    "operator": "gte",
                    "mode": "relative",
                    "values": ["last_30_days"],
                }
            ],
        )
        resp = httpx.post(
            evaluate_url,
            json={"business_definition": bd},
            headers=headers,
            timeout=30.0,
        )
        assert resp.status_code in (200, 400)
        if resp.status_code == 400:
            detail = resp.json().get("detail", "")
            assert "Invalid business definition" not in str(detail), (
                f"Should pass validation but got: {detail}"
            )
        if resp.status_code == 200:
            result = resp.json()
            scope = result.get("compiled_scope", {})
            filter_preds = scope.get("filter_predicates", [])
            assert len(filter_preds) >= 1
            combined = " ".join(filter_preds)
            assert "business_date" in combined

    def test_filter_changes_compiled_scope_vs_unfiltered(
        self, headers, evaluate_url, measure_revenue_id, dim_customer_segment_id,
    ):
        bd_plain = _base_single_measure_bd(measure_revenue_id)
        bd_filtered = _base_single_measure_bd(
            measure_revenue_id,
            filters=[
                {
                    "dimension_id": dim_customer_segment_id,
                    "operator": "in",
                    "mode": "fixed",
                    "values": ["Retail"],
                }
            ],
        )
        r_plain = _evaluate(evaluate_url, headers, bd_plain)
        r_filtered = _evaluate(evaluate_url, headers, bd_filtered)
        plain_preds = r_plain.get("compiled_scope", {}).get("filter_predicates", [])
        filtered_preds = r_filtered.get("compiled_scope", {}).get("filter_predicates", [])
        assert len(plain_preds) == 0
        assert len(filtered_preds) == 1


# ---------------------------------------------------------------------------
# Time-variant (time intelligence) tests
# ---------------------------------------------------------------------------


class TestTimeVariant:
    """Prove time-variant KPIs compile and evaluate correctly."""

    def test_yoy_growth_compiles_decomposed_sql(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        """YoY growth compiles into two decomposed SQL queries (current +
        prior period). The value may be null if the seed data doesn't
        cover the prior-year window, but the SQL must always be emitted.
        """
        bd = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_revenue_id,
                "time_calculation": {
                    "type": "yoy_growth_pct",
                    "grain": "year",
                },
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        result = _evaluate(evaluate_url, headers, bd)
        sql = result.get("compiled_sql", "")
        assert sql, "YoY growth should compile SQL even if the result is null"
        assert sql.upper().count("SELECT") >= 2, (
            "YoY growth must decompose into at least 2 SQL queries"
        )

    def test_moving_average_evaluates_successfully(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        """Moving average evaluates via decomposed TI queries."""
        bd = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_revenue_id,
                "time_calculation": {
                    "type": "moving_average",
                    "periods": 3,
                    "grain": "month",
                },
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        result = _evaluate(evaluate_url, headers, bd)
        assert result["value"] is not None
        assert isinstance(result["value"], (int, float))

    def test_trailing_sum_differs_from_raw(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        """trailing_sum should produce a different value than raw measure."""
        bd_raw = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
        )
        bd_trail = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_revenue_id,
                "time_calculation": {
                    "type": "trailing_sum",
                    "periods": 3,
                    "grain": "month",
                },
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        r_raw = _evaluate(evaluate_url, headers, bd_raw)
        resp = httpx.post(
            evaluate_url,
            json={"business_definition": bd_trail},
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code == 200:
            r_trail = resp.json()
            if r_raw["value"] is not None and r_trail["value"] is not None:
                assert r_raw["value"] != r_trail["value"], (
                    "Trailing sum should differ from raw measure value"
                )


# ---------------------------------------------------------------------------
# Compiled SQL preview — "Show SQL" surfaces the real gateway SQL
# ---------------------------------------------------------------------------


class TestCompiledSqlPreview:
    """Prove the adhoc evaluate returns the actual SQL sent to the gateway."""

    def test_single_measure_returns_select_sql(
        self, headers, evaluate_url, measure_revenue_id,
    ):
        bd = _base_single_measure_bd(measure_revenue_id)
        result = _evaluate(evaluate_url, headers, bd)
        sql = result.get("compiled_sql")
        assert sql, "compiled_sql should be populated for a SQL-evaluated KPI"
        upper = sql.upper()
        assert "SELECT" in upper and "FROM" in upper, (
            f"compiled_sql should be a real SQL statement, got: {sql}"
        )
        assert "measure(" not in sql, (
            f"compiled_sql should be SQL, not the KPI DSL, got: {sql}"
        )

    def test_filtered_kpi_sql_contains_where(
        self, headers, evaluate_url, measure_revenue_id, dim_customer_segment_id,
    ):
        bd = _base_single_measure_bd(
            measure_revenue_id,
            filters=[
                {
                    "dimension_id": dim_customer_segment_id,
                    "operator": "in",
                    "mode": "fixed",
                    "values": ["Retail"],
                }
            ],
        )
        result = _evaluate(evaluate_url, headers, bd)
        sql = result.get("compiled_sql")
        assert sql, "compiled_sql should be populated"
        assert "WHERE" in sql.upper(), f"Filtered KPI SQL should have WHERE, got: {sql}"


# ---------------------------------------------------------------------------
# Cache isolation tests
# ---------------------------------------------------------------------------


class TestCacheIsolation:
    """Prove scoped KPI results do not leak between different filter configs."""

    def test_different_time_windows_no_cache_bleed(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        bd_12m = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
        )
        bd_6m = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_6_months",
                "dimension_id": dim_business_date_id,
            },
        )
        r_12m = _evaluate(evaluate_url, headers, bd_12m)
        r_6m = _evaluate(evaluate_url, headers, bd_6m)
        if r_12m["value"] is not None and r_6m["value"] is not None:
            assert r_12m["value"] != r_6m["value"], (
                "Different time windows must not return cached results from each other"
            )

    def test_same_config_returns_consistent_result(
        self, headers, evaluate_url, measure_revenue_id,
    ):
        bd = _base_single_measure_bd(measure_revenue_id)
        r1 = _evaluate(evaluate_url, headers, bd)
        r2 = _evaluate(evaluate_url, headers, bd)
        assert r1["value"] == r2["value"], (
            "Same business definition should return consistent values"
        )

    def test_different_sessions_same_filter_consistent(
        self, headers, evaluate_url, measure_revenue_id,
    ):
        bd = _base_single_measure_bd(measure_revenue_id)
        r1 = _evaluate(evaluate_url, headers, bd)
        resp = httpx.post(
            f"{API_BASE}/auth/login",
            json={"tenant_id": TENANT_ID, "email": EMAIL, "password": PASSWORD},
            timeout=10.0,
        )
        token2 = resp.json()["access_token"]
        headers2 = {"Authorization": f"Bearer {token2}", "Content-Type": "application/json"}
        r2 = _evaluate(evaluate_url, headers2, bd)
        assert r1["value"] == r2["value"], (
            "Same user with fresh token should see the same results (no session bleed)"
        )

    def test_persona_scope_does_not_bleed_into_no_persona(
        self, headers, evaluate_url, project_id, model_id, measure_revenue_id,
    ):
        """Evaluate with and without persona_id — results consistent for admin."""
        bd = _base_single_measure_bd(measure_revenue_id)
        r_no_persona = _evaluate(evaluate_url, headers, bd)
        persona_url = (
            f"{API_BASE}/projects/{project_id}/models/{model_id}/personas"
        )
        resp = httpx.get(persona_url, headers=headers, timeout=10.0)
        if resp.status_code != 200 or not resp.json():
            pytest.skip("No personas configured in demo model")
        persona_id = resp.json()[0]["id"]
        url_with_persona = f"{evaluate_url}?persona_id={persona_id}"
        resp2 = httpx.post(
            url_with_persona,
            json={"business_definition": bd},
            headers=headers,
            timeout=30.0,
        )
        assert resp2.status_code == 200, f"Persona evaluate failed: {resp2.text}"
        r_with_persona = resp2.json()
        assert r_no_persona["value"] == r_with_persona["value"], (
            "Admin user with unrestricted persona should see the same results"
        )


# ---------------------------------------------------------------------------
# UAT business scenarios — prove end-to-end KPI definitions from plan
# ---------------------------------------------------------------------------


class TestUATBusinessScenarios:
    """Prove the 7 UAT scenarios from the action plan evaluate successfully."""

    def test_ratio_formula_net_sales_over_revenue(
        self, headers, evaluate_url,
        measure_net_sales_id, measure_revenue_id, dim_business_date_id,
    ):
        """Ratio formula: net_sales / Revenue evaluates via Python fallback."""
        bd = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "ratio",
                "numerator_measure_id": measure_net_sales_id,
                "denominator_measure_id": measure_revenue_id,
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        result = _evaluate(evaluate_url, headers, bd)
        assert result["value"] is not None
        assert 0 < result["value"] <= 1.5, (
            f"Net sales / Revenue ratio should be reasonable, got {result['value']}"
        )

    def test_transaction_count_evaluates(
        self, headers, evaluate_url, measure_transaction_count_id, dim_business_date_id,
    ):
        """Transaction count measure evaluates over last 12 months."""
        bd = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_transaction_count_id,
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        result = _evaluate(evaluate_url, headers, bd)
        assert result["value"] is not None
        assert result["value"] > 0

    def test_prior_period_evaluates(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        """Prior-period revenue evaluates successfully."""
        bd = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_revenue_id,
                "time_calculation": {
                    "type": "prior_period",
                    "grain": "month",
                },
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        result = _evaluate(evaluate_url, headers, bd)
        assert result["value"] is not None
        assert isinstance(result["value"], (int, float))

    def test_yoy_growth_differs_from_raw(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        """YoY growth value differs from the raw measure value.

        The seed data covers ~1 year; the prior-year window for the
        business builder's 12-month preset may not have data. When the
        YoY value is null (prior period empty), skip the comparison —
        the decomposed-SQL test above already validates the compile path.
        """
        bd_raw = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
        )
        bd_yoy = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_revenue_id,
                "time_calculation": {
                    "type": "yoy_growth_pct",
                    "grain": "year",
                },
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        r_raw = _evaluate(evaluate_url, headers, bd_raw)
        r_yoy = _evaluate(evaluate_url, headers, bd_yoy)
        assert r_raw["value"] is not None
        if r_yoy["value"] is None:
            pytest.skip("Prior-year window has no data in the seed dataset")
        assert r_raw["value"] != r_yoy["value"]

    def test_not_in_filter_compiles_and_evaluates(
        self, headers, evaluate_url,
        measure_revenue_id, dim_business_date_id, dim_customer_segment_id,
    ):
        """KPI with not_in filter compiles predicate and evaluates."""
        bd = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_revenue_id,
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [
                {
                    "dimension_id": dim_customer_segment_id,
                    "operator": "not_in",
                    "mode": "fixed",
                    "values": ["NonExistentSegment"],
                }
            ],
        }
        result = _evaluate(evaluate_url, headers, bd)
        assert result["value"] is not None
        scope = result.get("compiled_scope", {})
        filter_preds = scope.get("filter_predicates", [])
        assert len(filter_preds) == 1
        assert "NOT IN" in filter_preds[0]
        assert "NonExistentSegment" in filter_preds[0]

    def test_gross_margin_pct_evaluates(
        self, headers, evaluate_url, measure_gross_margin_pct_id, dim_business_date_id,
    ):
        """Gross margin percentage measure evaluates over last 12 months."""
        bd = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_gross_margin_pct_id,
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        result = _evaluate(evaluate_url, headers, bd)
        assert result["value"] is not None

    def test_moving_average_differs_from_raw(
        self, headers, evaluate_url, measure_revenue_id, dim_business_date_id,
    ):
        """3-month moving average should differ from full 12-month sum."""
        bd_raw = _base_single_measure_bd(
            measure_revenue_id,
            time_window={
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
        )
        bd_ma = {
            "builder": "business_kpi",
            "version": 1,
            "formula": {
                "type": "single_measure",
                "measure_id": measure_revenue_id,
                "time_calculation": {
                    "type": "moving_average",
                    "periods": 3,
                    "grain": "month",
                },
            },
            "time_window": {
                "preset": "last_12_months",
                "dimension_id": dim_business_date_id,
            },
            "filters": [],
        }
        r_raw = _evaluate(evaluate_url, headers, bd_raw)
        r_ma = _evaluate(evaluate_url, headers, bd_ma)
        assert r_raw["value"] is not None
        assert r_ma["value"] is not None
        assert r_ma["value"] < r_raw["value"], (
            "3-month moving average should be less than 12-month total"
        )
