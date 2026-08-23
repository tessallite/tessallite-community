"""
Conftest for integration tests — overrides the parent conftest's autouse
fixtures that mock the DB. Integration tests hit live services via HTTP.

All entity IDs (project, model, measures, dimensions) are resolved
dynamically by slug/name so tests survive reseeds that regenerate UUIDs.
"""
from __future__ import annotations

import logging
import os

import httpx
import pytest

from .rate_limit_retry import send_with_retry
from .live_profile import (
    api_up,
    authentication_not_ready,
    environment_not_ready,
    live_integration_enabled,
    profile_item_or_not_ready,
    validate_live_profile,
)

LIVE_INTEGRATION_ENABLED = live_integration_enabled(os.environ)
LIVE_INTEGRATION_SKIP_REASON = (
    "live KPI integration is disabled; set TESSALLITE_RUN_LIVE_INTEGRATION=1 "
    "and every explicit INTEGRATION_TEST_* profile variable"
)

validate_live_profile(os.environ)

API_BASE = os.environ.get("INTEGRATION_TEST_API_BASE", "")
TENANT_ID = os.environ.get("INTEGRATION_TEST_TENANT", "")
EMAIL = os.environ.get("INTEGRATION_TEST_EMAIL", "")
PASSWORD = os.environ.get("INTEGRATION_TEST_PASSWORD", "")
PROJECT_SLUG = os.environ.get("INTEGRATION_TEST_PROJECT", "")
MODEL_SLUG = os.environ.get("INTEGRATION_TEST_MODEL", "")

_logger = logging.getLogger(__name__)


def _api_up() -> bool:
    return api_up(API_BASE)


# ---------------------------------------------------------------------------
# Rate-limiter isolation
# ---------------------------------------------------------------------------
# Root cause: the live model-service's TenantRateLimitMiddleware uses an
# in-memory store with a default ceiling of 60 requests/minute/tenant.
# Integration tests collectively exceed this ceiling within a single test
# session, causing later tests (especially batch KPI evaluations) to receive
# HTTP 429 responses and fail. The parent conftest's disable_rate_limiting
# only modifies the in-process snapshot — useless for integration tests that
# hit the live service over HTTP.
#
# ``_retry_rate_limited_requests`` below makes the harness correct on its own by
# honouring the 429 + ``Retry-After`` contract. It OWNS that property outright:
# nothing else has to hold for the suite to be right, and in particular the
# suite no longer depends on the deployed service's mutable global config.
#
# The previous approach — switching the limiter off on the live service for the
# duration of the package — was deleted on 2026-08-11 (Bug-8885); see the note
# further down for why. Before the retry existed, its silent failures let 429s
# surface as ordinary assertion failures ("Create failed (429)") whose
# membership drifted run to run, and those were repeatedly mis-attributed to
# whichever lane happened to be running.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="package", autouse=True)
def _retry_rate_limited_requests():
    """Honour the service's 429 + ``Retry-After`` contract for the whole
    integration package.

    Every integration test drives the live service through the synchronous
    top-level ``httpx`` helpers, all of which funnel into
    ``httpx.Client.send``. Wrapping that one entry point covers every existing
    call site without a retry argument at each of them.

    Scope is deliberately tight: the wrapper is installed for the integration
    PACKAGE only (restored the moment it finishes, so a full-suite run leaves
    nothing patched behind), it touches only the synchronous client — the
    in-process unit tests drive the app through ``httpx.AsyncClient``, which is
    a different class — and it retries only responses whose request addressed
    the live integration origin. Everything else is returned untouched.
    """
    original_send = httpx.Client.send

    def _send(self, request, *args, **kwargs):
        return send_with_retry(
            lambda req, *a, **kw: original_send(self, req, *a, **kw),
            request,
            *args,
            api_base=API_BASE,
            **kwargs,
        )

    httpx.Client.send = _send
    try:
        yield
    finally:
        httpx.Client.send = original_send


# DELETED 2026-08-11 (user decision, closes Bug-8885): ``_disable_live_rate_limiting``.
#
# It authenticated as system admin and PUT ``rate_limit.enabled = False`` on the
# RUNNING service for the duration of the integration package, restoring it on
# teardown. That is a test fixture mutating global production-shaped config on a
# shared deployment, and every failure mode it had was silent:
#
#   - a crashed or interrupted session left the live service with rate limiting
#     OFF indefinitely. This was not theoretical — the service was found in
#     exactly that state on 2026-08-11, which is Bug-8885's predicted hazard
#     actually materialising;
#   - a concurrent session's teardown re-enabled the limiter underneath a
#     running session;
#   - its own ``/auth/system/login`` is governed by the stricter 10/minute login
#     bucket, so back-to-back sessions could not authenticate to disable
#     anything, and it warned and continued throttled.
#
# ``_retry_rate_limited_requests`` above OWNS the correctness property outright,
# so this fixture bought only wall-clock — roughly 131s versus 10s per 80
# throttled requests. That is not worth a fixture that can leave a deployed
# service unprotected, and the ambiguity it created cost far more than it saved:
# its drifting 429 failures were repeatedly mis-attributed to whichever lane
# happened to be running.
#
# If the runtime becomes a problem, fix it by raising the limit for the test
# tenant in configuration, or by giving the suite its own tenant — never by
# switching a safety control off on a live service from a test.


@pytest.fixture(autouse=True)
def disable_rate_limiting():
    """Override the parent conftest's in-process rate-limiter disable.

    The parent conftest calls update_snapshot("rate_limit.enabled", False) which
    only modifies the test process's in-memory config snapshot — irrelevant for
    integration tests that hit the live service over HTTP. The package-scoped
    _retry_rate_limited_requests fixture absorbs the real service's throttling
    instead, by honouring its 429 + Retry-After contract.
    """
    yield


@pytest.fixture(autouse=True)
def mock_system_bootstrap():
    yield


@pytest.fixture(autouse=True)
def mock_rbac_get_tenant_db():
    yield


@pytest.fixture(autouse=True)
def mock_emit_webhook():
    yield


@pytest.fixture(scope="module")
def token():
    if not _api_up():
        environment_not_ready("model-service is not reachable")
    resp = httpx.post(
        f"{API_BASE}/auth/login",
        json={"tenant_id": TENANT_ID, "email": EMAIL, "password": PASSWORD},
        timeout=10.0,
    )
    authentication_not_ready(resp.status_code, resp.text)
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Dynamic entity resolution — survives reseeds
# ---------------------------------------------------------------------------


def _find_by_name(items: list[dict], name: str) -> dict:
    for item in items:
        if item.get("name") == name:
            return item
    raise LookupError(
        f"name {name!r} not found in {[i.get('name') for i in items]}"
    )


@pytest.fixture(scope="module")
def project_id(headers):
    resp = httpx.get(f"{API_BASE}/projects", headers=headers, timeout=10.0)
    authentication_not_ready(resp.status_code, resp.text)
    assert resp.status_code == 200, f"List projects failed: {resp.text}"
    return profile_item_or_not_ready(resp.json(), PROJECT_SLUG, "project")["id"]


@pytest.fixture(scope="module")
def model_id(headers, project_id):
    resp = httpx.get(
        f"{API_BASE}/projects/{project_id}/models",
        headers=headers, timeout=10.0,
    )
    authentication_not_ready(resp.status_code, resp.text)
    assert resp.status_code == 200, f"List models failed: {resp.text}"
    return profile_item_or_not_ready(resp.json(), MODEL_SLUG, "model")["id"]


@pytest.fixture(scope="module")
def _measures(headers, project_id, model_id):
    resp = httpx.get(
        f"{API_BASE}/projects/{project_id}/models/{model_id}/measures",
        headers=headers, timeout=10.0,
    )
    assert resp.status_code == 200, f"List measures failed: {resp.text}"
    return resp.json()


@pytest.fixture(scope="module")
def _dimensions(headers, project_id, model_id):
    resp = httpx.get(
        f"{API_BASE}/projects/{project_id}/models/{model_id}/dimensions",
        headers=headers, timeout=10.0,
    )
    assert resp.status_code == 200, f"List dimensions failed: {resp.text}"
    return resp.json()


def _measure_id(measures: list[dict], name: str) -> str:
    # Profile-portable: report environment readiness when the selected live
    # profile lacks this measure. The KPI integration tests assume the profile's model has
    # the Revenue/net_sales/gross_margin_pct/... measure set.
    try:
        return _find_by_name(measures, name)["id"]
    except LookupError:
        environment_not_ready(
            f"measure {name!r} not on the active model "
            f"({[m.get('name') for m in measures]}) — the selected live "
            "profile does not expose the KPI measure set (Bug-5453/5498)"
        )


def _dimension_id(dimensions: list[dict], name: str) -> str:
    try:
        return _find_by_name(dimensions, name)["id"]
    except LookupError:
        environment_not_ready(
            f"dimension {name!r} not on the active model "
            f"({[d.get('name') for d in dimensions]}) — the selected live "
            "profile does not expose the KPI dimension set (Bug-5453/5498)"
        )
