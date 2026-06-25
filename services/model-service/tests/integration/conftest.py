"""
Conftest for integration tests — overrides the parent conftest's autouse
fixtures that mock the DB. Integration tests hit live services via HTTP.

All entity IDs (project, model, measures, dimensions) are resolved
dynamically by slug/name so tests survive reseeds that regenerate UUIDs.
"""
from __future__ import annotations

import os

import httpx
import pytest

API_BASE = os.environ.get("INTEGRATION_TEST_API_BASE", "http://localhost:8001/api/v1")
# Profile-aware: defaults target the dev acme-demo stack; override via env to run
# against the Community demo bundle (tenant=demo / admin@demo.com / demo /
# project-demo / modely) — see agentic-testing/config/profiles/*.env.
TENANT_ID = os.environ.get("INTEGRATION_TEST_TENANT", "acme-demo")
EMAIL = os.environ.get("INTEGRATION_TEST_EMAIL", "admin@acme-demo.com")
PASSWORD = os.environ.get("INTEGRATION_TEST_PASSWORD", "acme-demo")

PROJECT_SLUG = os.environ.get("INTEGRATION_TEST_PROJECT", "project1")
MODEL_SLUG = os.environ.get("INTEGRATION_TEST_MODEL", "modelx")


def _api_up() -> bool:
    try:
        root = API_BASE.rsplit("/api/v1", 1)[0]
        return httpx.get(f"{root}/health", timeout=3.0).status_code == 200
    except Exception:
        return False


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
        pytest.skip("Model-service not reachable")
    resp = httpx.post(
        f"{API_BASE}/auth/login",
        json={"tenant_id": TENANT_ID, "email": EMAIL, "password": PASSWORD},
        timeout=10.0,
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Dynamic entity resolution — survives reseeds
# ---------------------------------------------------------------------------


def _find_by_slug(items: list[dict], slug: str) -> dict:
    for item in items:
        if item.get("slug") == slug:
            return item
    raise LookupError(
        f"slug {slug!r} not found in {[i.get('slug') for i in items]}"
    )


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
    assert resp.status_code == 200, f"List projects failed: {resp.text}"
    return _find_by_slug(resp.json(), PROJECT_SLUG)["id"]


@pytest.fixture(scope="module")
def model_id(headers, project_id):
    resp = httpx.get(
        f"{API_BASE}/projects/{project_id}/models",
        headers=headers, timeout=10.0,
    )
    assert resp.status_code == 200, f"List models failed: {resp.text}"
    return _find_by_slug(resp.json(), MODEL_SLUG)["id"]


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
    # Profile-portable: skip (not error) when the active model lacks this
    # measure — the KPI integration tests assume the dev acme-demo `modelx`
    # measures (Revenue/net_sales/gross_margin_pct/...); on the demo bundle's
    # `modely` they are absent, so the test skips cleanly instead of erroring.
    try:
        return _find_by_name(measures, name)["id"]
    except LookupError:
        pytest.skip(
            f"measure {name!r} not on the active model "
            f"({[m.get('name') for m in measures]}) — needs the dev acme-demo "
            f"modelx profile (see Bug-5453/5498 for demo-bundle portability)"
        )


def _dimension_id(dimensions: list[dict], name: str) -> str:
    try:
        return _find_by_name(dimensions, name)["id"]
    except LookupError:
        pytest.skip(
            f"dimension {name!r} not on the active model "
            f"({[d.get('name') for d in dimensions]}) — needs the dev acme-demo "
            f"modelx profile (Bug-5453/5498)"
        )
