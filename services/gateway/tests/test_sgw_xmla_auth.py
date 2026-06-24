"""XMLA Basic/Bearer/session middleware tests for the S-GW security tail.

  F-002-06  credential cache (one login per burst); session-resume requests
            pass through the middleware to the handler.
  F-002-07  Bearer-JWT requests reach the handler with the token threaded.
  F-002-15  the SOAP Catalog (a model slug) is not blindly used as a tenant;
            an unknown-tenant (404/422) response falls through to discovery.
"""
from __future__ import annotations

import base64

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.dax import credential_cache
from src.dax.auth_basic import BasicAuthMiddleware


def _build_client() -> TestClient:
    async def echo(request):
        body = await request.body()  # the handler re-reads the body (must be cached)
        return JSONResponse(
            {
                "jwt": getattr(request.state, "jwt_token", ""),
                "user": getattr(request.state, "username", ""),
                "body_len": len(body),
            }
        )

    app = Starlette(routes=[Route("/api/v1/xmla", echo, methods=["POST"])])
    app.add_middleware(BasicAuthMiddleware)
    return TestClient(app)


def _basic(user: str, pw: str) -> dict:
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


_SOAP_WITH_CATALOG = (
    '<Envelope><Body><Discover><Properties><PropertyList>'
    '<Catalog>modelx</Catalog>'
    '</PropertyList></Properties></Discover></Body></Envelope>'
)
_SOAP_WITH_SESSION = (
    '<Envelope xmlns:tns="urn:schemas-microsoft-com:xml-analysis">'
    '<Header><tns:Session SessionId="abc-123"/></Header>'
    '<Body><Discover/></Body></Envelope>'
)
_SOAP_BEGIN_SESSION = (
    '<Envelope xmlns:tns="urn:schemas-microsoft-com:xml-analysis">'
    '<Header><tns:BeginSession/></Header>'
    '<Body><Discover/></Body></Envelope>'
)


@pytest.fixture(autouse=True)
def _reset_cache():
    credential_cache._reset_for_tests()
    yield
    credential_cache._reset_for_tests()


# ---------------------------------------------------------------------------
# F-002-07 — Bearer-JWT path reaches the handler
# ---------------------------------------------------------------------------

class TestBearerPath:
    def test_bearer_token_threaded_to_handler(self):
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla",
            content=_SOAP_WITH_CATALOG,
            headers={"Authorization": "Bearer my.jwt.token"},
        )
        assert resp.status_code == 200
        assert resp.json()["jwt"] == "my.jwt.token"

    def test_empty_bearer_rejected(self):
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_WITH_CATALOG,
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# F-002-06 — session-resume passthrough + credential cache
# ---------------------------------------------------------------------------

class TestSessionPassthrough:
    def test_session_only_request_passes_through(self):
        client = _build_client()
        # No Authorization header, but a live Session header → reach handler.
        resp = client.post("/api/v1/xmla", content=_SOAP_WITH_SESSION)
        assert resp.status_code == 200
        assert resp.json()["jwt"] == ""  # handler will consult session store

    def test_begin_session_without_auth_still_challenged(self):
        client = _build_client()
        # BeginSession is a NEW session (no SessionId) → still needs creds.
        resp = client.post("/api/v1/xmla", content=_SOAP_BEGIN_SESSION)
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")

    def test_no_auth_no_session_challenged(self):
        client = _build_client()
        resp = client.post("/api/v1/xmla", content=_SOAP_WITH_CATALOG)
        assert resp.status_code == 401


class TestCredentialCache:
    def test_login_called_once_per_burst(self, monkeypatch):
        calls = {"n": 0}

        async def _fake_login(catalog, username, password):
            calls["n"] += 1
            return "jwt-for-acme"

        # Catalog is treated as unknown-tenant so discovery is the login path.
        async def _fake_discover(username, password):
            calls["n"] += 1
            return "jwt-for-acme"

        monkeypatch.setattr("src.dax.auth_basic.login_for_token", _fake_login)
        monkeypatch.setattr("src.dax.auth_basic.login_discover", _fake_discover)
        client = _build_client()
        headers = _basic("u@acme.com", "pw")
        # No catalog → discovery is the single login path.
        soap_no_catalog = "<Envelope><Body><Discover/></Body></Envelope>"
        for _ in range(5):
            resp = client.post("/api/v1/xmla", content=soap_no_catalog, headers=headers)
            assert resp.status_code == 200
            assert resp.json()["jwt"] == "jwt-for-acme"
        assert calls["n"] == 1  # cached after the first login

    def test_different_password_not_served_from_cache(self, monkeypatch):
        async def _fake_discover(username, password):
            return f"jwt-{password}"

        monkeypatch.setattr("src.dax.auth_basic.login_discover", _fake_discover)
        client = _build_client()
        soap = "<Envelope><Body><Discover/></Body></Envelope>"
        r1 = client.post("/api/v1/xmla", content=soap, headers=_basic("u", "pw1"))
        r2 = client.post("/api/v1/xmla", content=soap, headers=_basic("u", "pw2"))
        assert r1.json()["jwt"] == "jwt-pw1"
        assert r2.json()["jwt"] == "jwt-pw2"


# ---------------------------------------------------------------------------
# F-002-15 — Catalog is not blindly a tenant in the login path
# ---------------------------------------------------------------------------

class TestCatalogNotTenant:
    def test_unknown_tenant_catalog_falls_through_to_discovery(self, monkeypatch):
        """A 404 (catalog is a model slug, not a tenant) is not a fatal error;
        the middleware falls through to cross-tenant discovery."""
        events = []

        async def _fake_login(catalog, username, password):
            events.append(("login", catalog))
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(404),
            )

        async def _fake_discover(username, password):
            events.append(("discover", username))
            return "jwt-discovered"

        monkeypatch.setattr("src.dax.auth_basic.login_for_token", _fake_login)
        monkeypatch.setattr("src.dax.auth_basic.login_discover", _fake_discover)
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_WITH_CATALOG, headers=_basic("u", "pw")
        )
        assert resp.status_code == 200
        assert resp.json()["jwt"] == "jwt-discovered"
        assert events == [("login", "modelx"), ("discover", "u")]

    def test_operational_error_surfaces_as_401_not_swallowed(self, monkeypatch):
        async def _fake_login(catalog, username, password):
            raise httpx.HTTPStatusError(
                "503", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(503),
            )

        async def _fake_discover(username, password):  # pragma: no cover
            raise AssertionError("discovery must not run on operational error")

        monkeypatch.setattr("src.dax.auth_basic.login_for_token", _fake_login)
        monkeypatch.setattr("src.dax.auth_basic.login_discover", _fake_discover)
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_WITH_CATALOG, headers=_basic("u", "pw")
        )
        assert resp.status_code == 401
