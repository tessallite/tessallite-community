"""Bug-9887 — the XMLA Discover auth/persona overheads, as behaviour.

Two deterministic per-request overheads were measured on the local docker stack
and are guarded here:

* A DOOMED tenant-scoped login. The middleware passed the SOAP ``Catalog``
  to ``login_for_token`` as if it were a tenant slug. Every catalogue this
  product publishes is ``tenant__project__model``, so that login always
  answered 404 and the request then paid a SECOND, O(active tenants)
  cross-tenant discovery login — 2.5-4.2 s per credential-cache miss.
* A credential cache keyed on ``(catalog, username, password)`` with a 30 s
  TTL, so switching between a business catalogue and its technical sibling,
  or pausing longer than 30 s, re-paid that double login in full.

Plus the uncached persona fan-out: ``get_model_personas`` is issued once per
model on DBSCHEMA_CATALOGS, MDSCHEMA_CATALOGS and MDSCHEMA_CUBES — three of the
~11 calls in Excel's startup Discover — for identical data.
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
        await request.body()
        return JSONResponse({"jwt": getattr(request.state, "jwt_token", "")})

    app = Starlette(routes=[Route("/api/v1/xmla", echo, methods=["POST"])])
    app.add_middleware(BasicAuthMiddleware)
    return TestClient(app)


def _basic(user: str, pw: str) -> dict:
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _soap(catalog: str) -> str:
    return (
        "<Envelope><Body><Discover><Properties><PropertyList>"
        f"<Catalog>{catalog}</Catalog>"
        "</PropertyList></Properties></Discover></Body></Envelope>"
    )


def _unknown_tenant() -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "404",
        request=httpx.Request("POST", "http://ms/login"),
        response=httpx.Response(404),
    )


class _LoginSpy:
    """Records every login the middleware issues, in order."""

    def __init__(self, *, tenant_ok: bool = True):
        self.calls: list[tuple[str, str]] = []
        self._tenant_ok = tenant_ok

    async def login_for_token(self, tenant, username, password):
        self.calls.append(("tenant", tenant))
        if not self._tenant_ok:
            raise _unknown_tenant()
        return f"jwt-tenant-{tenant}"

    async def login_discover(self, username, password):
        self.calls.append(("discover", ""))
        return "jwt-discover"

    def install(self, monkeypatch):
        monkeypatch.setattr(
            "src.dax.auth_basic.login_for_token", self.login_for_token,
        )
        monkeypatch.setattr(
            "src.dax.auth_basic.login_discover", self.login_discover,
        )
        return self


@pytest.fixture(autouse=True)
def _reset_cache():
    credential_cache._reset_for_tests()
    yield
    credential_cache._reset_for_tests()


# ---------------------------------------------------------------------------
# Deliverable 1 — no doomed tenant-scoped login
# ---------------------------------------------------------------------------

class TestBug9887SingleLoginPerCacheMiss:
    def test_model_catalogue_performs_exactly_one_login(self, monkeypatch):
        """A ``tenant__project__model`` catalogue costs ONE login, not two.

        Pre-fix this asserted 2 calls: ``("tenant", "acme-demo__project1__modely")``
        (a 404 against a tenant slug that never existed) followed by
        ``("discover", "")``.
        """
        spy = _LoginSpy().install(monkeypatch)
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla",
            content=_soap("acme-demo__project1__modely"),
            headers=_basic("admin@acme-demo.com", "acme-demo"),
        )
        assert resp.status_code == 200
        assert spy.calls == [("tenant", "acme-demo")]
        assert resp.json()["jwt"] == "jwt-tenant-acme-demo"

    def test_persona_catalogue_also_resolves_the_tenant(self, monkeypatch):
        spy = _LoginSpy().install(monkeypatch)
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla",
            content=_soap("acme-demo__project1__modely__technical"),
            headers=_basic("admin@acme-demo.com", "acme-demo"),
        )
        assert resp.status_code == 200
        assert spy.calls == [("tenant", "acme-demo")]

    def test_genuine_tenant_slug_still_uses_the_tenant_scoped_path(self, monkeypatch):
        """A bare catalogue that IS a tenant keeps its tenant-scoped login.

        The per-tenant lockout boundary (Bug-9799) is applied there, so the
        fix must not divert a real tenant slug into cross-tenant discovery.
        """
        spy = _LoginSpy().install(monkeypatch)
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla",
            content=_soap("acme-demo"),
            headers=_basic("admin@acme-demo.com", "acme-demo"),
        )
        assert resp.status_code == 200
        assert spy.calls == [("tenant", "acme-demo")]

    def test_unknown_tenant_still_falls_through_to_discovery(self, monkeypatch):
        """F-002-15 is preserved: a genuinely unknown tenant still discovers."""
        spy = _LoginSpy(tenant_ok=False).install(monkeypatch)
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla",
            content=_soap("legacy-model-slug"),
            headers=_basic("u@x.com", "pw"),
        )
        assert resp.status_code == 200
        assert spy.calls == [("tenant", "legacy-model-slug"), ("discover", "")]


# ---------------------------------------------------------------------------
# Deliverable 2 — credential cache keyed on identity, not catalogue
# ---------------------------------------------------------------------------

class TestBug9887CredentialCacheIsIdentityKeyed:
    def test_second_catalogue_is_served_from_cache(self, monkeypatch):
        """Two different catalogues, same credentials: ONE login in total.

        Pre-fix the second catalogue missed the cache (the catalogue was in the
        key) and paid a full login of its own.
        """
        spy = _LoginSpy().install(monkeypatch)
        client = _build_client()
        creds = _basic("admin@acme-demo.com", "acme-demo")
        r1 = client.post(
            "/api/v1/xmla", content=_soap("acme-demo__project1__modely"),
            headers=creds,
        )
        r2 = client.post(
            "/api/v1/xmla",
            content=_soap("acme-demo__project1__modely__technical"),
            headers=creds,
        )
        assert (r1.status_code, r2.status_code) == (200, 200)
        assert spy.calls == [("tenant", "acme-demo")]
        assert r1.json()["jwt"] == r2.json()["jwt"] == "jwt-tenant-acme-demo"

    def test_a_different_password_is_never_served_the_cached_token(self):
        credential_cache.put("u@acme.com", "right-pw", "jwt")
        assert credential_cache.get("u@acme.com", "wrong-pw") is None

    def test_default_ttl_comes_from_the_setting(self, monkeypatch):
        import shared.config.settings as settings_mod

        s = settings_mod.get_settings()
        monkeypatch.setattr(
            s, "GATEWAY_XMLA_CREDENTIAL_CACHE_TTL_SECONDS", 120, raising=False,
        )
        assert credential_cache.default_ttl() == 120
        # Clamped, so a mistyped value cannot hold a disabled account for hours.
        monkeypatch.setattr(
            s, "GATEWAY_XMLA_CREDENTIAL_CACHE_TTL_SECONDS", 999_999, raising=False,
        )
        assert credential_cache.default_ttl() == credential_cache._MAX_TTL_SECONDS

    def test_zero_ttl_disables_the_cache_entirely(self, monkeypatch):
        import shared.config.settings as settings_mod

        monkeypatch.setattr(
            settings_mod.get_settings(),
            "GATEWAY_XMLA_CREDENTIAL_CACHE_TTL_SECONDS", 0, raising=False,
        )
        credential_cache.put("u@acme.com", "pw", "jwt")
        assert credential_cache.get("u@acme.com", "pw") is None

    def test_expired_entry_forces_a_fresh_login(self, monkeypatch):
        """The TTL is the residual revocation window — it must actually fire."""
        spy = _LoginSpy().install(monkeypatch)
        client = _build_client()
        creds = _basic("admin@acme-demo.com", "acme-demo")
        soap = _soap("acme-demo__project1__modely")
        client.post("/api/v1/xmla", content=soap, headers=creds)
        assert spy.calls == [("tenant", "acme-demo")]
        # Age the entry past its lifetime rather than sleeping.
        with credential_cache._lock:
            for key, (token, _stored_at) in list(credential_cache._cache.items()):
                credential_cache._cache[key] = (token, 0.0)
        client.post("/api/v1/xmla", content=soap, headers=creds)
        assert spy.calls == [("tenant", "acme-demo"), ("tenant", "acme-demo")]

    def test_rejected_login_purges_the_cached_credential(self, monkeypatch):
        """User deactivate / password change reaches the gateway as a 401.

        The cached entry for that identity must be dropped immediately rather
        than surviving the rest of its (now much longer) TTL.
        """
        credential_cache.put("u@acme.com", "old-pw", "jwt-stale")

        async def _reject_tenant(tenant, username, password):
            raise _unknown_tenant()

        async def _reject_discover(username, password):
            raise httpx.HTTPStatusError(
                "401",
                request=httpx.Request("POST", "http://ms/login"),
                response=httpx.Response(401),
            )

        monkeypatch.setattr("src.dax.auth_basic.login_for_token", _reject_tenant)
        monkeypatch.setattr("src.dax.auth_basic.login_discover", _reject_discover)
        client = _build_client()
        resp = client.post(
            "/api/v1/xmla", content=_soap("modely"),
            headers=_basic("u@acme.com", "new-pw"),
        )
        assert resp.status_code == 401
        assert credential_cache.get("u@acme.com", "old-pw") is None


# ---------------------------------------------------------------------------
# Deliverable 3 — persona fan-out is cached
# ---------------------------------------------------------------------------

class TestBug9887PersonaFanOutIsCached:
    @pytest.fixture(autouse=True)
    def _reset_router_client_caches(self):
        from src import router_client

        router_client._reset_metadata_caches_for_tests()
        yield
        router_client._reset_metadata_caches_for_tests()

    def _patch_transport(self, monkeypatch, calls, payload):
        from src import router_client

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                calls.append(url)
                return _Resp()

        monkeypatch.setattr(router_client.httpx, "AsyncClient", _Client)

    @pytest.mark.asyncio
    async def test_repeat_calls_are_served_from_cache(self, monkeypatch):
        """Pre-fix this issued one GET per call; Excel makes three per connect."""
        from src import router_client

        calls: list[str] = []
        payload = [{"id": "p1", "slug": "technical", "name": "Technical"}]
        self._patch_transport(monkeypatch, calls, payload)

        for _ in range(3):
            personas = await router_client.get_model_personas(
                "m1", "acme-demo", "jwt-a", project_id="proj1",
            )
            assert personas == payload
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_different_principal_never_reads_the_cached_list(
        self, monkeypatch,
    ):
        """``for_audience=true`` filters by the CALLER's roles.

        A second JWT is a different security context and must re-fetch, not
        inherit an admin-primed list.
        """
        from src import router_client

        calls: list[str] = []
        self._patch_transport(monkeypatch, calls, [{"id": "p1", "slug": "s"}])

        await router_client.get_model_personas(
            "m1", "acme-demo", "jwt-a", project_id="proj1",
        )
        await router_client.get_model_personas(
            "m1", "acme-demo", "jwt-b", project_id="proj1",
        )
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_failed_fetch_is_never_cached(self, monkeypatch):
        """Fail-closed: a persona-blind catalogue must not be pinned for the TTL."""
        from src import router_client

        calls: list[str] = []

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                calls.append(url)
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(router_client.httpx, "AsyncClient", _Client)

        for _ in range(2):
            with pytest.raises(Exception):
                await router_client.get_model_personas(
                    "m1", "acme-demo", "jwt-a", project_id="proj1",
                )
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_expiry_refreshes_after_a_persona_change(self, monkeypatch):
        """A persona edit propagates within one TTL window.

        This is the SHIPPED metadata-catalogue invalidation (the
        ``XMLA_METADATA_CACHE_TTL`` lever), reused rather than duplicated.
        """
        from src import router_client

        calls: list[str] = []
        self._patch_transport(monkeypatch, calls, [{"id": "p1", "slug": "s"}])

        await router_client.get_model_personas(
            "m1", "acme-demo", "jwt-a", project_id="proj1",
        )
        # Age the entry past the TTL exactly as a real expiry would.
        for key, (_stored_at, value) in list(router_client._personas_cache.items()):
            router_client._personas_cache[key] = (0.0, value)
        await router_client.get_model_personas(
            "m1", "acme-demo", "jwt-a", project_id="proj1",
        )
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_ttl_zero_disables_the_persona_cache(self, monkeypatch):
        from src import router_client

        monkeypatch.setenv("XMLA_METADATA_CACHE_TTL", "0")
        calls: list[str] = []
        self._patch_transport(monkeypatch, calls, [{"id": "p1", "slug": "s"}])

        for _ in range(2):
            await router_client.get_model_personas(
                "m1", "acme-demo", "jwt-a", project_id="proj1",
            )
        assert len(calls) == 2
