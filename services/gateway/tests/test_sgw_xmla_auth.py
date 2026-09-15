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

    app = Starlette(routes=[
        Route("/api/v1/xmla", echo, methods=["POST"]),
        Route("/xmla", echo, methods=["POST"]),
        Route("/msmdpump.dll", echo, methods=["GET", "HEAD", "POST"]),
    ])
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


def _collect_route_paths(routes, prefix=""):
    """Collect all route paths, recursing into FastAPI >= 0.139 _IncludedRouter."""
    paths: set[str] = set()
    for route in routes:
        tp = type(route).__name__
        if tp == "_IncludedRouter":
            inc_prefix = getattr(route.include_context, "prefix", "")
            orig = route.original_router
            if hasattr(orig, "routes"):
                paths.update(_collect_route_paths(orig.routes, prefix + inc_prefix))
        elif hasattr(route, "path"):
            paths.add(prefix + route.path)
    return paths


def test_standard_xmla_pump_aliases_are_registered():
    # FastAPI 0.139+ wraps include_router calls in _IncludedRouter; a flat
    # iteration of app.routes no longer sees the prefixed paths.
    from src.main import app

    paths = _collect_route_paths(app.routes)
    assert "/api/v1/xmla/msmdpump.dll" in paths
    assert "/xmla/msmdpump.dll" in paths
    assert "/msmdpump.dll" in paths


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

    @pytest.mark.parametrize("method", ["get", "head", "post"])
    def test_root_msmdpump_alias_is_challenged(self, method):
        client = _build_client()
        request = getattr(client, method)
        kwargs = {"content": _SOAP_WITH_CATALOG} if method == "post" else {}
        resp = request("/msmdpump.dll", **kwargs)
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate", "").startswith("Basic")


class TestPersonalAccessToken:
    """Bug-7314: a Personal Access Token presented as the XMLA Basic password
    must be routed to the model-service login exchange (which validates it),
    NOT mistaken for a pre-issued JWT, and never leak into the response as
    anything other than the exchanged JWT."""

    _PAT = "tesspat_ab12cd34_S0meRandomSecretValueThatIsLongEnough12345"

    def test_pat_password_routed_through_login_exchange(self, monkeypatch):
        seen = {}

        async def _fake_discover(username, password):
            seen["username"] = username
            seen["password"] = password
            return "jwt-from-pat"

        monkeypatch.setattr("src.dax.auth_basic.login_discover", _fake_discover)
        client = _build_client()
        soap = "<Envelope><Body><Discover/></Body></Envelope>"
        resp = client.post(
            "/api/v1/xmla", content=soap,
            headers=_basic("sso.user1@tessallite.local", self._PAT),
        )
        assert resp.status_code == 200
        # The PAT reached the login exchange verbatim; the response carries the
        # exchanged JWT, not the PAT.
        assert seen["password"] == self._PAT
        assert resp.json()["jwt"] == "jwt-from-pat"

    def test_pat_is_not_treated_as_jwt(self):
        # A PAT must never satisfy the gateway's JWT-direct branch: it is not a
        # valid JWT and does not start with the "ey" sentinel.
        from src.auth.base import verify_jwt_token

        assert not self._PAT.startswith("ey")
        with pytest.raises(ValueError):
            verify_jwt_token(self._PAT)


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


class TestCredentialCacheInvalidation:
    """Bug-6309: a JWT must not be served after the auth material changes."""

    def test_put_evicts_prior_password_entry(self):
        # A fresh login with a NEW password for the same user must evict the
        # OLD-password entry so it cannot keep being served.
        credential_cache.put("u@acme.com", "old-pw", "jwt-old")
        assert credential_cache.get("u@acme.com", "old-pw") == "jwt-old"
        credential_cache.put("u@acme.com", "new-pw", "jwt-new")
        assert credential_cache.get("u@acme.com", "old-pw") is None
        assert credential_cache.get("u@acme.com", "new-pw") == "jwt-new"

    def test_invalidate_purges_user_entry(self):
        credential_cache.put("u@acme.com", "pw", "jwt")
        credential_cache.invalidate("u@acme.com")
        assert credential_cache.get("u@acme.com", "pw") is None

    def test_ttl_is_not_extended_by_reads(self):
        # A read must never refresh the entry's age (non-extendable lifetime),
        # so a disabled account / changed password cannot be kept alive by a
        # steady request burst. Serving with ttl<=0 proves the age gate fires.
        credential_cache.put("u@acme.com", "pw", "jwt")
        assert credential_cache.get("u@acme.com", "pw", ttl=0) is None
        # And the expired entry was swept.
        assert credential_cache.get("u@acme.com", "pw") is None

    def test_rejected_login_invalidates_stale_cache(self, monkeypatch):
        # Seed a cache entry for the old password, then present a new/changed
        # password that misses the cache and is REJECTED upstream. The rejection
        # must purge the stale old-password entry so it is no longer served.
        credential_cache.put("u", "old-pw", "jwt-stale")

        async def _reject(username, password):
            # The shape production actually produces. This raised a bare
            # ValueError until the Bug-5534 review: login_discover's own
            # docstring says an all-tenants rejection is an HTTP 401, and the
            # only ValueError it raises is a MISSING access_token cookie — a
            # backend protocol fault, not a rejection. Simulating a rejection
            # with ValueError tested a path that never occurs.
            raise httpx.HTTPStatusError(
                "401", request=httpx.Request("POST", "http://ms/login"),
                response=httpx.Response(401),
            )

        monkeypatch.setattr("src.dax.auth_basic.login_discover", _reject)
        client = _build_client()
        soap = "<Envelope><Body><Discover/></Body></Envelope>"
        resp = client.post("/api/v1/xmla", content=soap, headers=_basic("u", "new-pw"))
        assert resp.status_code == 401
        # The stale old-password token is gone.
        assert credential_cache.get("u", "old-pw") is None


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

    def test_locked_catalog_429_does_not_fall_through_to_discovery(self, monkeypatch):
        """Bug-9799: a generic 429 is a real lockout, not an unknown Catalog.

        Bug-9799's property — a lockout must NOT fall through to cross-tenant
        discovery — is unchanged and still asserted by ``_fake_discover``.
        What changed in the Bug-5534 review is the outward code: a lockout is
        relayed as 429, not reported as 401. The credentials may be perfectly
        correct and merely throttled, and a 401 carries WWW-Authenticate, which
        prompts MSOLAP to ask for a new password mid-lockout.
        """
        async def _fake_login(catalog, username, password):
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(429),
            )

        async def _fake_discover(username, password):  # pragma: no cover
            raise AssertionError("locked tenant login must not fall through")

        monkeypatch.setattr("src.dax.auth_basic.login_for_token", _fake_login)
        monkeypatch.setattr("src.dax.auth_basic.login_discover", _fake_discover)
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_WITH_CATALOG, headers=_basic("u", "pw")
        )
        assert resp.status_code == 429
        assert "WWW-Authenticate" not in resp.headers
        assert int(resp.headers["Retry-After"]) > 0

    def test_operational_error_surfaces_as_503_not_swallowed(self, monkeypatch):
        """An upstream 5xx is the authority failing to answer, not a rejection.

        This asserted 401 until Bug-5534 item B. The INTENT — an operational
        error must not be swallowed and must not fall through to discovery — is
        unchanged and still asserted below; only the status changed, and that
        change is the point: a cold Cloud Run model-service raising a timeout
        was reported to Power BI as wrong credentials, so the operator
        re-checked passwords through the very window a retry would have
        survived. A 401 also carries WWW-Authenticate, which re-prompts for a
        password that was never wrong.
        """
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
        assert resp.status_code == 503
        assert "WWW-Authenticate" not in resp.headers


class TestBug9869PaddedUserName:
    """Bug-9869 (ALEX, 2026-09-04): a user name padded with whitespace reached
    the login (which matched the account) while the credential cache and every
    following request keyed on the padded value and answered 401. The name is
    normalised once, before login and cache alike; the password is untouched."""

    def test_padded_user_name_is_normalised_for_login_and_cache(self, monkeypatch):
        seen = []

        async def _fake_discover(username, password):
            seen.append((username, password))
            return "jwt-admin"

        monkeypatch.setattr("src.dax.auth_basic.login_discover", _fake_discover)
        client = _build_client()
        soap = "<Envelope><Body><Discover/></Body></Envelope>"
        padded = _basic(" admin@acme-demo.com ", " pw ")
        clean = _basic("admin@acme-demo.com", " pw ")
        for headers in (padded, clean, padded):
            resp = client.post("/api/v1/xmla", content=soap, headers=headers)
            assert resp.status_code == 200, resp.text
            assert resp.json()["jwt"] == "jwt-admin"
        # One login for the burst: padded and clean spellings share the cache
        # entry, and the password kept its own surrounding spaces.
        assert seen == [("admin@acme-demo.com", " pw ")]
