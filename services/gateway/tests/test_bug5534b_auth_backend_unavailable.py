"""Bug-5534 item B — a cold auth backend is not a wrong password.

Live diagnosis (2026-07-10, gateway logs): the first authenticated Power BI leg
logged ``src.dax.auth_basic: Cross-tenant discovery for admin@acme-demo.com
raised ReadTimeout``. The Cloud Run model-service had scaled to zero and took
~26 seconds to serve ``/auth/login``.

Both login branches caught that timeout and returned 401. So an OUTAGE was
reported to the client as a credential rejection — the exact masking
``_is_credential_failure`` documents must never happen ("Everything else (DB
outage, encoding error, timeout) should NOT be silently masked as an auth
failure"), and which both branch comments claimed to avoid while doing it.

The cost was not cosmetic. A 401 carries ``WWW-Authenticate``, so MSOLAP
re-prompts for credentials that were never wrong, and the operator spends the
cold-start window re-checking passwords instead of retrying. 503 with
``Retry-After`` says the thing that is actually true.

These tests pin both directions: an operational fault is 503 and never counts
as a failed login, while a genuine rejection is still 401.
"""
from __future__ import annotations

import base64

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.dax import auth_basic, credential_cache
from src.dax.auth_basic import BasicAuthMiddleware, _throttle_key
from src.jdbc.throttle import JdbcConnectionGovernor

_SOAP = "<Envelope><Body><Discover/></Body></Envelope>"
_TEST_IP = "testclient"


def _basic(user="u@t.com", pw="pw"):
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _client() -> TestClient:
    async def echo(request):
        return JSONResponse({"jwt": getattr(request.state, "jwt_token", "")})

    app = Starlette(routes=[Route("/api/v1/xmla", echo, methods=["POST"])])
    app.add_middleware(BasicAuthMiddleware)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    credential_cache._reset_for_tests()
    gov = JdbcConnectionGovernor(
        max_conn_per_ip=0, max_auth_failures=3, auth_failure_window_seconds=60
    )
    monkeypatch.setattr(auth_basic, "get_governor", lambda: gov)
    yield gov
    credential_cache._reset_for_tests()


def _timeout(*_a, **_kw):
    """What a cold Cloud Run model-service actually produced."""
    raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", "http://ms/login"))


class TestOperationalFaultIsNotACredentialRejection:
    def test_discovery_timeout_answers_503_not_401(self, monkeypatch):
        """THE defect: the cold-start path returned 401."""
        async def _boom(username, password):
            _timeout()

        monkeypatch.setattr(auth_basic, "login_discover", _boom)
        r = _client().post("/api/v1/xmla", content=_SOAP, headers=_basic())
        assert r.status_code == 503, (
            "a timeout from the auth authority must not be reported as a "
            "credential rejection"
        )

    def test_the_503_does_not_re_prompt_for_credentials(self, monkeypatch):
        """``WWW-Authenticate`` is what makes MSOLAP ask for a password again.
        Nothing is wrong with the password, so it must be absent."""
        async def _boom(username, password):
            _timeout()

        monkeypatch.setattr(auth_basic, "login_discover", _boom)
        r = _client().post("/api/v1/xmla", content=_SOAP, headers=_basic())
        assert "WWW-Authenticate" not in r.headers
        assert r.headers.get("Retry-After")
        assert int(r.headers["Retry-After"]) > 0

    def test_tenant_specific_login_fault_also_answers_503(self, monkeypatch):
        """The same defect existed on BOTH login branches, and both comments
        claimed the opposite of what the code did."""
        async def _boom_tenant(catalog, username, password):
            _timeout()

        async def _unused_discovery(username, password):  # pragma: no cover
            raise AssertionError("discovery must not run after an operational fault")

        monkeypatch.setattr(auth_basic, "login_for_token", _boom_tenant)
        monkeypatch.setattr(auth_basic, "login_discover", _unused_discovery)
        soap = ('<Envelope><Body><Discover><Properties><PropertyList>'
                '<Catalog>acme</Catalog></PropertyList></Properties></Discover>'
                '</Body></Envelope>')
        r = _client().post("/api/v1/xmla", content=soap, headers=_basic())
        assert r.status_code == 503

    def test_an_operational_fault_never_counts_as_a_failed_login(
        self, monkeypatch, _state,
    ):
        """A cold backend must not push a legitimate user toward the throttle:
        an outage would otherwise lock them out for guessing correctly."""
        async def _boom(username, password):
            _timeout()

        monkeypatch.setattr(auth_basic, "login_discover", _boom)
        client = _client()
        for _ in range(5):
            client.post("/api/v1/xmla", content=_SOAP, headers=_basic())
        assert not _state.is_throttled(_throttle_key(_TEST_IP, "u@t.com"))


class TestGenuineRejectionIsUnchanged:
    def test_wrong_password_is_still_401_with_a_challenge(self, monkeypatch):
        """The over-correction direction. If an actually-wrong password stopped
        returning 401, clients would retry forever instead of prompting."""
        async def _reject(username, password):
            raise httpx.HTTPStatusError(
                "401", request=httpx.Request("POST", "http://ms/login"),
                response=httpx.Response(401),
            )

        monkeypatch.setattr(auth_basic, "login_discover", _reject)
        r = _client().post("/api/v1/xmla", content=_SOAP, headers=_basic())
        assert r.status_code == 401
        assert r.headers.get("WWW-Authenticate", "").startswith("Basic")

    def test_every_tenant_rejecting_is_still_401(self, monkeypatch):
        """``login_discover`` raises a plain ValueError when no tenant accepts
        the credentials — a rejection, not a fault."""
        async def _reject(username, password):
            raise ValueError("no tenant accepted these credentials")

        monkeypatch.setattr(auth_basic, "login_discover", _reject)
        r = _client().post("/api/v1/xmla", content=_SOAP, headers=_basic())
        assert r.status_code == 401
