"""
Integration tests: save and evaluate each of the 6 wizard KPI types against
the seed model with hand-computed numeric expectations (F-017-01).

Reference values are computed independently through the JDBC gateway (the
sanctioned query path) with explicit period-bounded SQL, then the KPI
endpoint's evaluated number must match the business math:

  growth_rate    = (current_period - prior_period) / |prior_period|
  moving_window  = mean of the trailing N period sums (nulls skipped)
  ratio          = SUM(net_sales) / SUM(Revenue)
  variance       = SUM(Revenue) - SUM(net_sales)
  composite      = weighted pct-of-target scores of its children

All entity IDs are resolved dynamically by name — no hardcoded UUIDs.

Requires live Docker services (model-service :8001, gateway :5433, postgres)
and the seeded acme-demo tenant.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None

from .conftest import (
    API_BASE, TENANT_ID, EMAIL, PASSWORD, MODEL_SLUG,
    _dimension_id,
)

JDBC_HOST = os.environ.get("GATEWAY_JDBC_HOST", "localhost")
JDBC_PORT = int(os.environ.get("GATEWAY_JDBC_PORT", "5433"))

pytestmark = [pytest.mark.integration]

REL = 1e-6  # relative tolerance for float comparison


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


# ---------------------------------------------------------------------------
# Reference values via the JDBC gateway (independent of the KPI machinery)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def jdbc():
    if psycopg2 is None:
        pytest.skip("psycopg2-binary not installed")
    try:
        conn = psycopg2.connect(
            host=JDBC_HOST, port=JDBC_PORT, database=TENANT_ID,
            user=EMAIL, password=PASSWORD, connect_timeout=10,
        )
        conn.autocommit = True
    except Exception as exc:
        pytest.skip(f"JDBC gateway not reachable: {exc}")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def kpis_url(project_id, model_id):
    return f"{API_BASE}/projects/{project_id}/models/{model_id}/kpis"


@pytest.fixture(scope="module")
def dim_business_date_id(_dimensions):
    return _dimension_id(_dimensions, "business_date")


def _scalar(jdbc, sql: str) -> float | None:
    cur = jdbc.cursor()
    try:
        try:
            cur.execute(sql)
        except Exception as exc:  # noqa: BLE001
            # Profile-portable: these baseline queries reference the dev
            # acme-demo `modelx` columns (Revenue/net_sales/...). On the demo
            # bundle's `modely` they don't exist -> skip cleanly instead of
            # failing (Bug-5453/5498 tracks full demo-bundle portability).
            if "Unknown column" in str(exc) or "does not exist" in str(exc):
                pytest.skip(
                    f"baseline SQL column not on the active model ({exc}) — "
                    f"needs the dev acme-demo modelx profile (Bug-5453/5498)"
                )
            raise
        row = cur.fetchone()
    finally:
        cur.close()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _period_sum(jdbc, start: str, end: str) -> float | None:
    return _scalar(
        jdbc,
        f'SELECT SUM("Revenue") FROM {MODEL_SLUG} '
        f'WHERE "business_date" >= {start} AND "business_date" < {end}',
    )


# ---------------------------------------------------------------------------
# KPI lifecycle helpers
# ---------------------------------------------------------------------------


def _create_kpi(kpis_url, headers, payload: dict) -> dict:
    body = {
        "status_graphic": "Traffic Light",
        "trend_graphic": "Standard Arrow",
        **payload,
    }
    resp = httpx.post(kpis_url, json=body, headers=headers, timeout=30.0)
    assert resp.status_code == 201, f"Create failed ({resp.status_code}): {resp.text}"
    return resp.json()


def _evaluate(kpis_url, headers, kpi_id: str) -> dict:
    resp = httpx.post(
        f"{kpis_url}/{kpi_id}/evaluate", headers=headers, timeout=60.0,
    )
    assert resp.status_code == 200, f"Evaluate failed ({resp.status_code}): {resp.text}"
    return resp.json()


def _delete_kpi(kpis_url, headers, kpi_id: str) -> None:
    resp = httpx.delete(f"{kpis_url}/{kpi_id}", headers=headers, timeout=30.0)
    assert resp.status_code == 204, f"Cleanup failed ({resp.status_code}): {resp.text}"


@pytest.fixture
def kpi_factory(headers, kpis_url):
    created: list[str] = []

    def make(payload: dict) -> dict:
        kpi = _create_kpi(kpis_url, headers, payload)
        created.append(kpi["id"])
        return kpi

    yield make
    for kpi_id in created:
        _delete_kpi(kpis_url, headers, kpi_id)


def _uname(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# The six wizard KPI types
# ---------------------------------------------------------------------------


def test_simple_measure_kpi(headers, kpis_url, kpi_factory, jdbc):
    expected = _scalar(jdbc, f'SELECT SUM("Revenue") FROM {MODEL_SLUG}')
    assert expected is not None

    kpi = kpi_factory({
        "name": _uname("it_simple"),
        "kpi_type": "simple_measure",
        "expression": 'measure("Revenue")',
    })
    result = _evaluate(kpis_url, headers, kpi["id"])
    assert result["value"] == pytest.approx(expected, rel=REL)
    assert result["formatted_value"] is not None


def test_ratio_kpi(headers, kpis_url, kpi_factory, jdbc):
    net_sales = _scalar(jdbc, f'SELECT SUM("net_sales") FROM {MODEL_SLUG}')
    revenue = _scalar(jdbc, f'SELECT SUM("Revenue") FROM {MODEL_SLUG}')
    assert net_sales is not None and revenue not in (None, 0)
    expected = net_sales / revenue

    kpi = kpi_factory({
        "name": _uname("it_ratio"),
        "kpi_type": "ratio",
        "expression": 'safe_div(measure("net_sales"), measure("Revenue"))',
    })
    result = _evaluate(kpis_url, headers, kpi["id"])
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_variance_kpi(headers, kpis_url, kpi_factory, jdbc):
    net_sales = _scalar(jdbc, f'SELECT SUM("net_sales") FROM {MODEL_SLUG}')
    revenue = _scalar(jdbc, f'SELECT SUM("Revenue") FROM {MODEL_SLUG}')
    expected = revenue - net_sales

    kpi = kpi_factory({
        "name": _uname("it_variance"),
        "kpi_type": "variance",
        "expression": 'measure("Revenue") - measure("net_sales")',
    })
    result = _evaluate(kpis_url, headers, kpi["id"])
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_growth_rate_kpi(headers, kpis_url, kpi_factory, jdbc, dim_business_date_id):
    """Wizard Growth Rate (pct_change) — year grain so the seed data has
    rows in both the current and prior comparison windows."""
    cur = _period_sum(
        jdbc, "DATE_TRUNC('year', CURRENT_DATE)", "CURRENT_DATE",
    )
    prior = _period_sum(
        jdbc,
        "DATE_TRUNC('year', CURRENT_DATE) - INTERVAL '1 year'",
        "CURRENT_DATE - INTERVAL '1 year'",
    )
    assert cur is not None and prior not in (None, 0), (
        "Seed data must cover the current and prior YTD windows"
    )
    expected = (cur - prior) / abs(prior)

    kpi = kpi_factory({
        "name": _uname("it_growth"),
        "kpi_type": "growth_rate",
        "expression": 'pct_change(measure("Revenue"), "year")',
        "time_dimension_id": dim_business_date_id,
    })
    result = _evaluate(kpis_url, headers, kpi["id"])
    assert result["value"] is not None, "Growth Rate KPI must evaluate (F-017-01)"
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_moving_window_kpi(headers, kpis_url, kpi_factory, jdbc, dim_business_date_id):
    """Wizard Moving Window (moving_avg over 3 trailing month windows)."""
    sums = [
        _period_sum(
            jdbc,
            f"CURRENT_DATE - INTERVAL '{k + 1} months'",
            f"CURRENT_DATE - INTERVAL '{k} months'",
        )
        for k in range(3)
    ]
    present = [s for s in sums if s is not None]
    assert present, "Seed data must cover at least one trailing month window"
    expected = sum(present) / len(present)

    kpi = kpi_factory({
        "name": _uname("it_moving"),
        "kpi_type": "moving_window",
        "expression": 'moving_avg(measure("Revenue"), "month", literal(3))',
        "time_dimension_id": dim_business_date_id,
        "trend_period": "month",
    })
    result = _evaluate(kpis_url, headers, kpi["id"])
    assert result["value"] is not None, "Moving Window KPI must evaluate (F-017-01)"
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_moving_window_quarter_grain(headers, kpis_url, kpi_factory, jdbc, dim_business_date_id):
    """Quarter grain must not emit INTERVAL 'n quarter' (F-017-07)."""
    sums = [
        _period_sum(
            jdbc,
            f"CURRENT_DATE - INTERVAL '{(k + 1) * 3} months'",
            f"CURRENT_DATE - INTERVAL '{k * 3} months'",
        )
        for k in range(2)
    ]
    present = [s for s in sums if s is not None]
    assert present, "Seed data must cover at least one trailing quarter window"
    expected = sum(present) / len(present)

    kpi = kpi_factory({
        "name": _uname("it_moving_q"),
        "kpi_type": "moving_window",
        "expression": 'moving_avg(measure("Revenue"), "quarter", literal(2))',
        "time_dimension_id": dim_business_date_id,
    })
    result = _evaluate(kpis_url, headers, kpi["id"])
    assert result["value"] is not None
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_composite_kpi(headers, kpis_url, kpi_factory, jdbc):
    """Composite of two weighted children, pct-of-target normalisation.

    Child A: revenue with static target = 2x actual  -> score 50
    Child B: net sales with static target = 4x actual -> score 25
    Weights 0.6 / 0.4 -> composite = 0.6*50 + 0.4*25 = 40
    Evaluated through evaluate-batch (the scorecard path for composites).
    """
    revenue = _scalar(jdbc, f'SELECT SUM("Revenue") FROM {MODEL_SLUG}')
    net_sales = _scalar(jdbc, f'SELECT SUM("net_sales") FROM {MODEL_SLUG}')
    assert revenue and net_sales

    parent = kpi_factory({
        "name": _uname("it_composite"),
        "kpi_type": "composite",
        "expression": 'literal(0)',
    })
    kpi_factory({
        "name": _uname("it_comp_child_a"),
        "kpi_type": "simple_measure",
        "expression": 'measure("Revenue")',
        "target_type": "static",
        "target_value": revenue * 2,
        "parent_kpi_id": parent["id"],
        "weight": 0.6,
    })
    kpi_factory({
        "name": _uname("it_comp_child_b"),
        "kpi_type": "simple_measure",
        "expression": 'measure("net_sales")',
        "target_type": "static",
        "target_value": net_sales * 4,
        "parent_kpi_id": parent["id"],
        "weight": 0.4,
    })

    resp = httpx.post(
        f"{kpis_url}/evaluate-batch",
        json={"kpi_ids": [parent["id"]]},
        headers=headers,
        timeout=60.0,
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    composite_value = results[0]["value"]
    assert composite_value == pytest.approx(40.0, rel=1e-3), (
        f"Composite score should be 0.6*50 + 0.4*25 = 40, got {composite_value}"
    )

    single = _evaluate(kpis_url, headers, parent["id"])
    assert single["value"] == pytest.approx(40.0, rel=1e-3), (
        f"Single evaluate of a composite should be 40, got {single['value']}"
    )


# ---------------------------------------------------------------------------
# Ad-hoc preview parity (the wizard live-preview path)
# ---------------------------------------------------------------------------


def test_adhoc_growth_rate_preview_matches_saved(
    headers, kpis_url, kpi_factory, jdbc, dim_business_date_id,
):
    """The wizard preview (evaluate-adhoc with time_dimension) must produce
    the same number as the saved KPI evaluation."""
    kpi = kpi_factory({
        "name": _uname("it_growth_parity"),
        "kpi_type": "growth_rate",
        "expression": 'pct_change(measure("Revenue"), "year")',
        "time_dimension_id": dim_business_date_id,
    })
    saved = _evaluate(kpis_url, headers, kpi["id"])

    resp = httpx.post(
        f"{kpis_url}/evaluate-adhoc",
        json={
            "expression": 'pct_change(measure("Revenue"), "year")',
            "time_dimension": dim_business_date_id,
        },
        headers=headers,
        timeout=60.0,
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["value"] == pytest.approx(saved["value"], rel=REL)
    assert preview["compiled_sql"], "Show SQL preview must surface the gateway SQL"
    assert "OVER (" not in preview["compiled_sql"].upper(), (
        "TI must evaluate via decomposed simple queries, not window functions"
    )


# ---------------------------------------------------------------------------
# B9 round 3 — fail-loud status labels must not crash /evaluate-batch
# ---------------------------------------------------------------------------


def _evaluate_batch(kpis_url, headers, kpi_ids: list[str]) -> dict:
    resp = httpx.post(
        f"{kpis_url}/evaluate-batch",
        json={"kpi_ids": kpi_ids},
        headers=headers,
        timeout=90.0,
    )
    assert resp.status_code == 200, (
        f"evaluate-batch must not 500 on fail-loud labels "
        f"({resp.status_code}): {resp.text}"
    )
    return resp.json()


def _result_for(results: list[dict], kpi_id: str) -> dict:
    for r in results:
        if r.get("kpi_id") == kpi_id:
            return r
    raise AssertionError(f"kpi {kpi_id} missing from batch results")


def test_batch_deep_composite_chain_labels_not_500(headers, kpis_url, kpi_factory, jdbc):
    """Repro (a): a 6-deep composite chain exceeds _MAX_COMPOSITE_DEPTH (5).

    The depth label is 68 chars (> old String(64)). Batch must return 200 with
    the explicit depth label, not crash on the upsert.
    """
    leaf = kpi_factory({
        "name": _uname("it_deep_leaf"),
        "kpi_type": "simple_measure",
        "expression": 'measure("Revenue")',
        "target_type": "static",
        "target_value": 100.0,
    })
    child_id = leaf["id"]
    top_id = None
    for level in range(6):
        comp = kpi_factory({
            "name": _uname(f"it_deep_c{level}"),
            "kpi_type": "composite",
            "expression": "literal(0)",
        })
        patch_resp = httpx.patch(
            f"{kpis_url}/{child_id}",
            json={"parent_kpi_id": comp["id"], "weight": 1.0},
            headers=headers,
            timeout=30.0,
        )
        assert patch_resp.status_code == 200, patch_resp.text
        child_id = comp["id"]
        top_id = comp["id"]

    data = _evaluate_batch(kpis_url, headers, [top_id])
    top = _result_for(data["results"], top_id)
    assert top["value"] is None, "Over-depth composite must fail loud (null value)"
    assert "deeper than" in (top["status_label"] or ""), (
        f"Expected explicit depth label, got {top['status_label']!r}"
    )


def test_batch_ti_without_time_dimension_labels_not_500(headers, kpis_url, kpi_factory, jdbc):
    """Repro (b): a derived time-intelligence KPI saved via the raw API with no
    time dimension (the wizard save-gate is UI-only). The fail-loud label is 69
    chars. Batch must return 200 with the explicit label, not 500."""
    kpi = kpi_factory({
        "name": _uname("it_ti_no_dim"),
        "kpi_type": "growth_rate",
        "expression": 'pct_change(measure("Revenue"), "year")',
    })
    data = _evaluate_batch(kpis_url, headers, [kpi["id"]])
    res = _result_for(data["results"], kpi["id"])
    assert res["value"] is None
    assert "time dimension" in (res["status_label"] or "").lower(), (
        f"Expected the TI-no-time-dimension label, got {res['status_label']!r}"
    )


def test_batch_broken_child_does_not_take_down_siblings(
    headers, kpis_url, kpi_factory, jdbc,
):
    """Repro (c): a healthy composite requested alone whose auto-loaded child is
    a broken TI KPI (no time dimension). The batch transitively loads the child,
    whose 69-char label previously crashed the upsert. Batch must return 200; the
    healthy KPIs requested alongside must still evaluate."""
    revenue = _scalar(jdbc, f'SELECT SUM("Revenue") FROM {MODEL_SLUG}')
    assert revenue

    parent = kpi_factory({
        "name": _uname("it_brk_parent"),
        "kpi_type": "composite",
        "expression": "literal(0)",
    })
    kpi_factory({
        "name": _uname("it_brk_good"),
        "kpi_type": "simple_measure",
        "expression": 'measure("Revenue")',
        "target_type": "static",
        "target_value": revenue * 2,
        "parent_kpi_id": parent["id"],
        "weight": 1.0,
    })
    kpi_factory({
        "name": _uname("it_brk_ti"),
        "kpi_type": "growth_rate",
        "expression": 'pct_change(measure("Revenue"), "year")',
        "parent_kpi_id": parent["id"],
        "weight": 1.0,
    })
    standalone = kpi_factory({
        "name": _uname("it_brk_standalone"),
        "kpi_type": "simple_measure",
        "expression": 'measure("Revenue")',
    })

    data = _evaluate_batch(kpis_url, headers, [parent["id"], standalone["id"]])
    results = data["results"]

    sa_res = _result_for(results, standalone["id"])
    assert sa_res["value"] == pytest.approx(revenue, rel=REL), (
        "An unrelated healthy KPI must still evaluate when a sibling's child is broken"
    )

    par_res = _result_for(results, parent["id"])
    assert par_res["value"] == pytest.approx(50.0, rel=1e-3), (
        f"Composite should score 50 from its one healthy child, got {par_res['value']}"
    )
