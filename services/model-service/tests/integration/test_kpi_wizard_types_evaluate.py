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

Run with the explicit profile variables documented in
test_kpi_business_builder_evaluate.py, including
TESSALLITE_RUN_LIVE_INTEGRATION=1 and INTEGRATION_TEST_EXCLUSIVE=1.
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
    LIVE_INTEGRATION_ENABLED, LIVE_INTEGRATION_SKIP_REASON,
    _dimension_id,
)
from .live_profile import environment_not_ready

JDBC_HOST = os.environ.get("GATEWAY_JDBC_HOST", "localhost")
JDBC_PORT = int(os.environ.get("GATEWAY_JDBC_PORT", "5433"))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not LIVE_INTEGRATION_ENABLED, reason=LIVE_INTEGRATION_SKIP_REASON),
]

REL = 1e-6  # relative tolerance for float comparison


@pytest.fixture(scope="module", autouse=True)
def _require_kpi_model(_measures):
    """Report an unavailable live profile when it lacks the KPI measures.
    These tests assume the dev acme-demo `modelx` (Revenue/net_sales/...); on the
    demo bundle's `modely` they are absent (Bug-5453/5498)."""
    if "net_sales" not in {m.get("name") for m in _measures}:
        environment_not_ready(
            "active model lacks KPI measures (net_sales/...) — needs the dev "
            "acme-demo modelx profile (Bug-5453/5498)"
        )


# ---------------------------------------------------------------------------
# Reference values via the JDBC gateway (independent of the KPI machinery)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def jdbc():
    if psycopg2 is None:
        environment_not_ready("psycopg2-binary is not installed")
    try:
        conn = psycopg2.connect(
            host=JDBC_HOST, port=JDBC_PORT, database=TENANT_ID,
            user=EMAIL, password=PASSWORD, connect_timeout=10,
        )
        conn.autocommit = True
    except Exception as exc:
        environment_not_ready(f"JDBC gateway is not reachable: {exc}")
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
            # bundle's `modely` they don't exist -> report an unavailable
            # profile (Bug-5453/5498 tracks full demo-bundle portability).
            if "Unknown column" in str(exc) or "does not exist" in str(exc):
                environment_not_ready(
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


def _save_model(headers, project_id: str, model_id: str) -> None:
    """Save (create a new version snapshot) so that any entities created since
    the last save are captured in the snapshot the deploy will point to.

    Bug-8688: without this step, deploy re-deploys the PREVIOUS saved snapshot
    which predates the newly-created KPI, so the KPI is absent from the
    deployed snapshot and evaluate returns 404 (Withheld by F-017-01).
    """
    save_url = f"{API_BASE}/projects/{project_id}/models/{model_id}/versions"
    resp = httpx.post(save_url, json={}, headers=headers, timeout=60.0)
    assert resp.status_code == 200, (
        f"Save (create version) failed ({resp.status_code}): {resp.text}"
    )


def _deploy_model(headers, project_id: str, model_id: str) -> None:
    """Save then deploy the model so newly created KPIs appear in the deployed
    snapshot and can be evaluated through the query gateway.

    Bug-8546: deploy without save meant KPIs were never in the snapshot.
    Bug-8688: deploy alone re-deploys the previous snapshot which predates any
    KPIs created since the last save; the save step captures them first.
    """
    # Save: create a fresh version snapshot that includes any entities
    # (KPIs, measures, etc.) created since the last save.
    _save_model(headers, project_id, model_id)
    # Deploy: point the model's deployed_version_id to the latest version.
    deploy_url = f"{API_BASE}/projects/{project_id}/models/{model_id}/deploy"
    resp = httpx.post(deploy_url, headers=headers, timeout=60.0)
    # 200 = deployed, 409 = already deployed with same definition — both OK.
    assert resp.status_code in (200, 409), (
        f"Deploy failed ({resp.status_code}): {resp.text}"
    )


def _evaluate(kpis_url, headers, kpi_id: str) -> dict:
    resp = httpx.post(
        f"{kpis_url}/{kpi_id}/evaluate", headers=headers, timeout=60.0,
    )
    assert resp.status_code == 200, f"Evaluate failed ({resp.status_code}): {resp.text}"
    return resp.json()


def _delete_kpi(kpis_url, headers, kpi_id: str) -> None:
    resp = httpx.delete(f"{kpis_url}/{kpi_id}", headers=headers, timeout=30.0)
    assert resp.status_code == 204, f"Cleanup failed ({resp.status_code}): {resp.text}"


class _KpiLifecycle:
    """Create KPIs, publish them into the deployed snapshot, evaluate them.

    Bug-8546: without a deploy step, newly created KPIs on a deployed model
    return 404 from the evaluate endpoint because they are not in the
    deployed snapshot (F-017-01 serving authority).
    Bug-8688: any mutation made after the last save (a PATCH re-parenting a
    child, for instance) also has to be captured before evaluation, or the
    deployed snapshot is stale.

    Bug-8863: the save+deploy used to run after EVERY create, so a test that
    builds a 7-KPI chain serialised and deployed seven model snapshots and
    added seven version rows to the shared live model, when only the last one
    is ever observable. Publication is now deferred to the first evaluation:
    one save+deploy per test, after every mutation that test makes. The
    invariant above is preserved — and enforced rather than assumed, because
    evaluation only happens through this object, so a pending mutation cannot
    be evaluated against a stale snapshot.
    """

    def __init__(self, headers, kpis_url, project_id, model_id):
        self._headers = headers
        self._kpis_url = kpis_url
        self._project_id = project_id
        self._model_id = model_id
        self._created: list[str] = []
        self._pending = False

    def __call__(self, payload: dict) -> dict:
        kpi = _create_kpi(self._kpis_url, self._headers, payload)
        self._created.append(kpi["id"])
        self._pending = True
        return kpi

    def mark_dirty(self) -> None:
        """Record a mutation made outside this factory (e.g. a direct PATCH)
        so the next evaluation publishes it."""
        self._pending = True

    def publish(self) -> None:
        """Save + deploy if anything has changed since the last publication."""
        if self._pending:
            _deploy_model(self._headers, self._project_id, self._model_id)
            self._pending = False

    def evaluate(self, kpi_id: str) -> dict:
        self.publish()
        return _evaluate(self._kpis_url, self._headers, kpi_id)

    def evaluate_batch(self, kpi_ids: list[str]) -> dict:
        self.publish()
        return _evaluate_batch(self._kpis_url, self._headers, kpi_ids)

    def cleanup(self) -> None:
        """Delete every KPI this test created, then re-deploy.

        Deletion is attempted for every KPI even if one fails. These tests
        share a single live model, so a KPI stranded by an early failure
        changes the state every later run starts from — the shared-state half
        of Bug-8863. Failures are still surfaced, just after the whole
        cleanup has been attempted rather than instead of it.
        """
        failures: list[str] = []
        for kpi_id in self._created:
            try:
                _delete_kpi(self._kpis_url, self._headers, kpi_id)
            except Exception as exc:  # noqa: BLE001 - reported below
                failures.append(f"delete {kpi_id}: {exc}")
        if self._created:
            # Re-deploy to remove the deleted KPIs from the snapshot.
            try:
                _deploy_model(self._headers, self._project_id, self._model_id)
            except Exception as exc:  # noqa: BLE001 - reported below
                failures.append(f"re-deploy after cleanup: {exc}")
        if failures:
            raise AssertionError("KPI cleanup incomplete — " + "; ".join(failures))


@pytest.fixture
def kpi_factory(headers, kpis_url, project_id, model_id):
    factory = _KpiLifecycle(headers, kpis_url, project_id, model_id)
    yield factory
    factory.cleanup()


def _uname(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# The six wizard KPI types
# ---------------------------------------------------------------------------


def test_simple_measure_kpi(kpi_factory, jdbc):
    expected = _scalar(jdbc, f'SELECT SUM("Revenue") FROM {MODEL_SLUG}')
    assert expected is not None

    kpi = kpi_factory({
        "name": _uname("it_simple"),
        "kpi_type": "simple_measure",
        "expression": 'measure("Revenue")',
    })
    result = kpi_factory.evaluate(kpi["id"])
    assert result["value"] == pytest.approx(expected, rel=REL)
    assert result["formatted_value"] is not None


def test_ratio_kpi(kpi_factory, jdbc):
    net_sales = _scalar(jdbc, f'SELECT SUM("net_sales") FROM {MODEL_SLUG}')
    revenue = _scalar(jdbc, f'SELECT SUM("Revenue") FROM {MODEL_SLUG}')
    assert net_sales is not None and revenue not in (None, 0)
    expected = net_sales / revenue

    kpi = kpi_factory({
        "name": _uname("it_ratio"),
        "kpi_type": "ratio",
        "expression": 'safe_div(measure("net_sales"), measure("Revenue"))',
    })
    result = kpi_factory.evaluate(kpi["id"])
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_variance_kpi(kpi_factory, jdbc):
    net_sales = _scalar(jdbc, f'SELECT SUM("net_sales") FROM {MODEL_SLUG}')
    revenue = _scalar(jdbc, f'SELECT SUM("Revenue") FROM {MODEL_SLUG}')
    expected = revenue - net_sales

    kpi = kpi_factory({
        "name": _uname("it_variance"),
        "kpi_type": "variance",
        "expression": 'measure("Revenue") - measure("net_sales")',
    })
    result = kpi_factory.evaluate(kpi["id"])
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_growth_rate_kpi(kpi_factory, jdbc, dim_business_date_id):
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
    result = kpi_factory.evaluate(kpi["id"])
    assert result["value"] is not None, "Growth Rate KPI must evaluate (F-017-01)"
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_moving_window_kpi(kpi_factory, jdbc, dim_business_date_id):
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
    result = kpi_factory.evaluate(kpi["id"])
    assert result["value"] is not None, "Moving Window KPI must evaluate (F-017-01)"
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_moving_window_quarter_grain(kpi_factory, jdbc, dim_business_date_id):
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
    result = kpi_factory.evaluate(kpi["id"])
    assert result["value"] is not None
    assert result["value"] == pytest.approx(expected, rel=REL)


def test_composite_kpi(kpi_factory, jdbc):
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

    results = kpi_factory.evaluate_batch([parent["id"]])["results"]
    assert len(results) == 1
    composite_value = results[0]["value"]
    assert composite_value == pytest.approx(40.0, rel=1e-3), (
        f"Composite score should be 0.6*50 + 0.4*25 = 40, got {composite_value}"
    )

    single = kpi_factory.evaluate(parent["id"])
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
    # `jdbc` is requested for its gateway-reachability readiness gate, not for
    # data: without a reachable gateway these assertions are meaningless, and
    # the helper emits an explicit ENVIRONMENT_NOT_READY result. Do not remove
    # it as an unused parameter.
    kpi = kpi_factory({
        "name": _uname("it_growth_parity"),
        "kpi_type": "growth_rate",
        "expression": 'pct_change(measure("Revenue"), "year")',
        "time_dimension_id": dim_business_date_id,
    })
    saved = kpi_factory.evaluate(kpi["id"])

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
    # `jdbc` is requested for its gateway-reachability readiness gate, not for
    # data: without a reachable gateway these assertions are meaningless, and
    # the helper emits an explicit ENVIRONMENT_NOT_READY result. Do not remove
    # it as an unused parameter.
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

    # Bug-8688: the PATCHes above change parent-child linkages outside the
    # factory, so the publication has to cover them too. evaluate_batch below
    # publishes once, after every mutation this test makes, and the final
    # parent-child topology goes into that single snapshot.
    #
    # This call is belt-and-braces, NOT load-bearing: the loop's own creates
    # already left the publication pending, so removing it would change nothing
    # here. It is kept so that the "an external mutation must be declared"
    # contract is visible at the one site in this file that mutates outside the
    # factory — a future test that PATCHes without creating would need it for
    # real. Nothing currently fails if it is deleted; that missing guard is
    # filed as Bug-8890 rather than left implied.
    kpi_factory.mark_dirty()

    data = kpi_factory.evaluate_batch([top_id])
    top = _result_for(data["results"], top_id)
    assert top["value"] is None, "Over-depth composite must fail loud (null value)"
    assert "deeper than" in (top["status_label"] or ""), (
        f"Expected explicit depth label, got {top['status_label']!r}"
    )


def test_batch_ti_without_time_dimension_labels_not_500(kpi_factory, jdbc):
    """Repro (b): a derived time-intelligence KPI saved via the raw API with no
    time dimension (the wizard save-gate is UI-only). The fail-loud label is 69
    chars. Batch must return 200 with the explicit label, not 500."""
    # `jdbc` is requested for its gateway-reachability readiness gate, not for
    # data: without a reachable gateway these assertions are meaningless, and
    # the helper emits an explicit ENVIRONMENT_NOT_READY result. Do not remove
    # it as an unused parameter.
    kpi = kpi_factory({
        "name": _uname("it_ti_no_dim"),
        "kpi_type": "growth_rate",
        "expression": 'pct_change(measure("Revenue"), "year")',
    })
    data = kpi_factory.evaluate_batch([kpi["id"]])
    res = _result_for(data["results"], kpi["id"])
    assert res["value"] is None
    assert "time dimension" in (res["status_label"] or "").lower(), (
        f"Expected the TI-no-time-dimension label, got {res['status_label']!r}"
    )


def test_batch_broken_child_does_not_take_down_siblings(
    kpi_factory, jdbc,
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

    data = kpi_factory.evaluate_batch([parent["id"], standalone["id"]])
    results = data["results"]

    sa_res = _result_for(results, standalone["id"])
    assert sa_res["value"] == pytest.approx(revenue, rel=REL), (
        "An unrelated healthy KPI must still evaluate when a sibling's child is broken"
    )

    par_res = _result_for(results, parent["id"])
    assert par_res["value"] == pytest.approx(50.0, rel=1e-3), (
        f"Composite should score 50 from its one healthy child, got {par_res['value']}"
    )
