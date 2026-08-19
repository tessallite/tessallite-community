"""Bug-5534 follow-up fixes from the Power BI / Excel XMLA diagnosis.

1. Login relay cold-start retry: a Cloud Run cold start on model-service
   surfaced as httpx.ReadTimeout, which the auth middleware turned into a
   spurious 401 challenge that MSOLAP/ADOMD clients never recover from.
   ``_post_login`` now retries transport timeouts (``gateway.login_retry_attempts``,
   default 1) and never retries a credential rejection (HTTPStatusError).
2. Malformed tenant slug rejection: ``/api/v1/xmla/acme-demo,`` (trailing
   comma from a copy-paste) used to half-work through cross-tenant auth,
   hiding the typo from the BI client. The tenant route now 404s any path
   segment that fails the canonical slug pattern ``^[a-z0-9_-]+$``.

Test escape note: the cold-start 401 was only visible on live GCP (scale-to-
zero); these tests pin the retry contract at the unit boundary. Tier: T1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import router_client  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Login relay retry-on-timeout
# ---------------------------------------------------------------------------

def _snapshot(monkeypatch, *, retries: int) -> None:
    values = {
        "gateway.login_retry_attempts": retries,
        "gateway.router_client_timeout_default": 15,
        "gateway.router_client_timeout_medium": 30,
        "gateway.router_client_timeout_long": 60,
        "gateway.router_client_timeout_xlong": 120,
    }
    monkeypatch.setattr(
        "src.router_client.system_snapshot_get", lambda key: values[key]
    )


class _TokenResponse:
    status_code = 200
    cookies: dict = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"access_token": "jwt-ok"}


class _FlakyClient:
    """Raises ReadTimeout for the first ``fail_first`` posts, then succeeds."""

    posts = 0  # class-level so a fresh instance per attempt still counts

    def __init__(self, *, fail_first: int, final_exc: Exception | None = None):
        self.fail_first = fail_first
        self.final_exc = final_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).posts += 1
        if type(self).posts <= self.fail_first:
            raise httpx.ReadTimeout("cold start")
        if self.final_exc is not None:
            raise self.final_exc
        return _TokenResponse()


def _patch_flaky(monkeypatch, *, fail_first: int, final_exc=None):
    _FlakyClient.posts = 0

    def _factory(**_kwargs):
        return _FlakyClient(fail_first=fail_first, final_exc=final_exc)

    monkeypatch.setattr("src.router_client.httpx.AsyncClient", _factory)


@pytest.mark.asyncio
async def test_login_retries_read_timeout_then_succeeds(monkeypatch):
    """First attempt hits a cold-start ReadTimeout; the retry gets the JWT."""
    _snapshot(monkeypatch, retries=1)
    _patch_flaky(monkeypatch, fail_first=1)
    monkeypatch.setattr(
        "src.router_client._extract_token_from_response", lambda _r: "jwt-ok"
    )
    token = await router_client.login_discover("u@x.test", "pw")
    assert token == "jwt-ok"
    assert _FlakyClient.posts == 2


@pytest.mark.asyncio
async def test_login_timeout_exhausts_retries_and_raises(monkeypatch):
    """When every attempt times out the ReadTimeout propagates (operational
    error surfaces; it is not masked as a credential failure)."""
    _snapshot(monkeypatch, retries=1)
    _patch_flaky(monkeypatch, fail_first=99)
    with pytest.raises(httpx.ReadTimeout):
        await router_client.login_for_token("acme", "u@x.test", "pw")
    assert _FlakyClient.posts == 2  # 1 attempt + 1 retry, no infinite loop


@pytest.mark.asyncio
async def test_login_credential_rejection_is_never_retried(monkeypatch):
    """A 401 from the auth authority must propagate on the FIRST attempt —
    retrying a rejected credential would hammer the login endpoint and delay
    the client's challenge handling."""
    _snapshot(monkeypatch, retries=3)
    rejection = httpx.HTTPStatusError(
        "401",
        request=httpx.Request("POST", "http://model-service/login"),
        response=httpx.Response(401),
    )
    _patch_flaky(monkeypatch, fail_first=0, final_exc=rejection)
    with pytest.raises(httpx.HTTPStatusError):
        await router_client.login_for_token("acme", "u@x.test", "wrong-pw")
    assert _FlakyClient.posts == 1


# ---------------------------------------------------------------------------
# 2. Malformed tenant slug rejection
# ---------------------------------------------------------------------------

from src.dax.xmla_server import _TENANT_SLUG_RE  # noqa: E402


@pytest.mark.parametrize(
    "slug,valid",
    [
        ("acme-demo", True),
        ("uat_auto", True),
        ("t1", True),
        ("acme-demo,", False),   # the observed Power BI copy-paste artifact
        ("acme-demo ", False),
        ("Acme-Demo", False),
        ("acme.demo", False),
        ("", False),
    ],
)
def test_tenant_slug_pattern(slug, valid):
    assert bool(_TENANT_SLUG_RE.fullmatch(slug)) is valid


@pytest.mark.asyncio
async def test_malformed_slug_returns_404_before_handler():
    """A malformed path segment is rejected with a clear 404 instead of
    half-working through cross-tenant discovery."""
    from starlette.requests import Request as StarletteRequest

    from src.dax.xmla_server import xmla_tenant_endpoint

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/xmla/acme-demo,",
        "headers": [],
        "query_string": b"",
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = StarletteRequest(scope, _receive)
    response = await xmla_tenant_endpoint("acme-demo,", request)
    assert response.status_code == 404
    assert b"acme-demo" in response.body


# ---------------------------------------------------------------------------
# 3. Bug-6948 (CF-002-GPT-F00201) — tenant GET probe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_get_probe_returns_200():
    """The tenant-specific XMLA endpoint must accept an authenticated GET
    probe with a 200 response, matching the server endpoint's behavior.
    BI clients (MSOLAP, Power BI) issue a GET to discover whether the
    endpoint is live before sending XMLA POST traffic."""
    from starlette.requests import Request as StarletteRequest

    from src.dax.xmla_server import xmla_tenant_endpoint

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/xmla/acme-demo",
        "headers": [],
        "query_string": b"",
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = StarletteRequest(scope, _receive)
    # Simulate middleware having set auth state
    request.state.username = "admin@acme-demo.com"
    request.state.jwt_token = "valid.jwt.token"
    response = await xmla_tenant_endpoint("acme-demo", request)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_tenant_get_probe_malformed_slug_returns_404():
    """A GET probe with a malformed slug must still 404 before the GET
    handler fires."""
    from starlette.requests import Request as StarletteRequest

    from src.dax.xmla_server import xmla_tenant_endpoint

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/xmla/acme-demo,",
        "headers": [],
        "query_string": b"",
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = StarletteRequest(scope, _receive)
    response = await xmla_tenant_endpoint("acme-demo,", request)
    assert response.status_code == 404
