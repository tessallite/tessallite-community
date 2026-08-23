"""Bug-8143: XMLA password login must receive the SPA/JDBC failed-login throttle.

The gateway XMLA Basic-auth relay adds the signed internal bypass header, so
model-service's dedicated login bucket is skipped for the internal login call.
JDBC compensates with ``JdbcConnectionGovernor``; before this fix the XMLA
(Excel / Power BI) password path consulted no per-IP failed-login governor at
all, so an attacker could make materially more password guesses over XMLA than
through the SPA.

These tests prove the XMLA path now routes through the SAME governor, keyed per
(client-ip, identity) so a proxied surface cannot be locked out or cleared for
everyone by one actor:
  * repeated wrong-password XMLA logins for one identity are throttled once the
    configured failure count is reached, and the throttled request is refused
    (429) WITHOUT reaching the upstream login relay;
  * only a genuine 401 rejection counts — an operational fault must not throttle;
  * a valid login clears the identity's window (a mistyped-then-correct user is
    not locked out); the test fails if that success-clear is reverted;
  * the 0-threshold opt-out disables the control.
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

_SOAP_NO_CATALOG = "<Envelope><Body><Discover/></Body></Envelope>"
# Starlette's TestClient reports this as the client host.
_TEST_IP = "testclient"


def _basic(user: str, pw: str) -> dict:
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _reject_401(*_args, **_kwargs):
    """A genuine wrong-password rejection as surfaced by the login relay:
    model-service returns 401 -> raise_for_status -> HTTPStatusError."""
    raise httpx.HTTPStatusError(
        "401 Unauthorized",
        request=httpx.Request("POST", "http://model-service/login"),
        response=httpx.Response(401),
    )


def _build_client() -> TestClient:
    async def echo(request):
        body = await request.body()
        return JSONResponse(
            {"jwt": getattr(request.state, "jwt_token", ""), "body_len": len(body)}
        )

    app = Starlette(routes=[Route("/api/v1/xmla", echo, methods=["POST"])])
    app.add_middleware(BasicAuthMiddleware)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    credential_cache._reset_for_tests()
    # Fresh governor with a low failure threshold so the throttle is reachable
    # inside a short test. Concurrency cap disabled (0) — only the failed-auth
    # window is under test here.
    gov = JdbcConnectionGovernor(
        max_conn_per_ip=0, max_auth_failures=3, auth_failure_window_seconds=60
    )
    monkeypatch.setattr(auth_basic, "get_governor", lambda: gov)
    yield gov
    credential_cache._reset_for_tests()


def test_repeated_failed_xmla_logins_are_throttled(monkeypatch, _reset_state):
    """After N failed password logins the XMLA path is throttled identically to
    JDBC, and the throttled attempt does NOT reach the upstream login relay."""
    gov = _reset_state
    key = _throttle_key(_TEST_IP, "attacker")
    upstream_calls = {"n": 0}

    async def _reject(username, password):
        upstream_calls["n"] += 1
        _reject_401()

    monkeypatch.setattr(auth_basic, "login_discover", _reject)
    client = _build_client()

    # Three genuine wrong-password attempts (each varies the password so the
    # credential cache never short-circuits the login exchange).
    for i in range(3):
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("attacker", f"guess{i}")
        )
        assert resp.status_code == 401
    assert upstream_calls["n"] == 3  # every failure reached the relay so far
    assert gov.is_throttled(key) is True

    # The fourth attempt is refused by the throttle BEFORE the relay is hit.
    resp = client.post(
        "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("attacker", "guess3")
    )
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After")
    # Revert guard: without the is_throttled check this would be 4. The upstream
    # relay was NOT consulted for the throttled attempt.
    assert upstream_calls["n"] == 3


def test_success_clears_identity_window(monkeypatch, _reset_state):
    """A successful login clears THIS identity's failure window so a legitimate
    user who mistyped is not locked out. Failing to clear (a revert of
    record_auth_success) would leave 4 in-window failures >= threshold 3 and
    throttle the user — this test catches that."""
    gov = _reset_state
    key = _throttle_key(_TEST_IP, "user")
    outcome = {"reject": True}

    async def _login(username, password):
        if outcome["reject"]:
            _reject_401()
        return "jwt-ok"

    monkeypatch.setattr(auth_basic, "login_discover", _login)
    client = _build_client()

    # Two failures — below the threshold of 3.
    for i in range(2):
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("user", f"typo{i}")
        )
        assert resp.status_code == 401
    assert gov.is_throttled(key) is False

    # Correct password now — succeeds AND clears the window.
    outcome["reject"] = False
    resp = client.post(
        "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("user", "correct")
    )
    assert resp.status_code == 200
    assert resp.json()["jwt"] == "jwt-ok"
    assert gov.is_throttled(key) is False

    # Two MORE failures after the clear: only 2 in-window (< 3), so still not
    # throttled. Without the success-clear there would be 4 in-window (>= 3) and
    # this assertion would fail.
    outcome["reject"] = True
    for i in range(2):
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("user", f"typo{i+2}")
        )
        assert resp.status_code == 401
    assert gov.is_throttled(key) is False


def test_one_identity_does_not_throttle_or_clear_another(monkeypatch, _reset_state):
    """Behind the reverse proxy every client shares one IP. The identity-keyed
    throttle must isolate accounts: guessing account A must not lock out or reset
    account B."""
    gov = _reset_state

    async def _reject(username, password):
        _reject_401()

    monkeypatch.setattr(auth_basic, "login_discover", _reject)
    client = _build_client()

    # Fully throttle account "alice".
    for i in range(3):
        client.post(
            "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("alice", f"g{i}")
        )
    assert gov.is_throttled(_throttle_key(_TEST_IP, "alice")) is True
    # "bob" (same proxy IP) is untouched.
    assert gov.is_throttled(_throttle_key(_TEST_IP, "bob")) is False

    # A successful login for "bob" must NOT clear alice's window.
    async def _ok(username, password):
        return "jwt-bob"

    monkeypatch.setattr(auth_basic, "login_discover", _ok)
    resp = client.post(
        "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("bob", "right")
    )
    assert resp.status_code == 200
    assert gov.is_throttled(_throttle_key(_TEST_IP, "alice")) is True


def test_operational_fault_does_not_throttle(monkeypatch, _reset_state):
    """An operational upstream fault (200 without an access_token cookie ->
    ValueError) returns 401 to the client but must NOT poison the throttle,
    otherwise a misconfigured upstream would lock out every XMLA client."""
    gov = _reset_state
    key = _throttle_key(_TEST_IP, "user")

    async def _missing_cookie(username, password):
        raise ValueError("model-service login response missing access_token cookie")

    monkeypatch.setattr(auth_basic, "login_discover", _missing_cookie)
    client = _build_client()
    for i in range(5):
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("user", f"p{i}")
        )
        assert resp.status_code == 401
    # Five operational faults, but zero throttle failures recorded.
    assert gov.is_throttled(key) is False


def test_throttle_key_bounds_identity_length():
    """A client-supplied username cannot inflate per-key memory: the identity
    component is length-bounded."""
    huge = "a" * 100_000
    key = _throttle_key("1.2.3.4", huge)
    ident = key.split(":", 2)[2]
    assert len(ident) <= 128


def test_governor_caps_tracked_failure_keys():
    """Bug-8143 memory guard: because the XMLA throttle key embeds a
    client-supplied identity, the governor must bound the number of tracked
    failure windows so a high-cardinality flood cannot exhaust memory."""
    ticks = {"t": 0.0}

    def clock():
        ticks["t"] += 1.0
        return ticks["t"]

    gov = JdbcConnectionGovernor(
        max_conn_per_ip=0,
        max_auth_failures=2,
        auth_failure_window_seconds=10_000,  # nothing expires during the test
        max_tracked_keys=5,
        time_fn=clock,
    )
    # Flood 500 distinct attacker-fabricated identities behind one IP.
    for i in range(500):
        gov.record_auth_failure(f"xmla:1.2.3.4:user{i}")
    # Memory stays bounded regardless of attacker cardinality.
    assert len(gov._failures) <= 5


def test_active_block_survives_sustained_flood():
    """An already-throttling identity (at threshold) must NOT be flushed and
    un-blocked by a later high-cardinality flood of single-failure fabricated
    keys: capacity eviction drops sub-threshold buckets first. Without that
    rule the older victim bucket (least recent) would be evicted and the
    attacker's real target would silently un-throttle — this test catches that
    revert."""
    ticks = {"t": 0.0}

    def clock():
        ticks["t"] += 1.0
        return ticks["t"]

    gov = JdbcConnectionGovernor(
        max_conn_per_ip=0,
        max_auth_failures=3,
        auth_failure_window_seconds=10_000,
        max_tracked_keys=10,
        time_fn=clock,
    )
    # Bring the real victim to the throttle threshold FIRST (oldest timestamps).
    vkey = "xmla:1.2.3.4:victim"
    for _ in range(3):
        gov.record_auth_failure(vkey)
    assert gov.is_throttled(vkey) is True

    # Now a sustained flood of single-failure (sub-threshold) fabricated keys,
    # all newer than the victim's failures.
    for i in range(200):
        gov.record_auth_failure(f"xmla:1.2.3.4:flood{i}")

    # Memory bounded AND the victim's block survived the flood.
    assert len(gov._failures) <= 10
    assert gov.is_throttled(vkey) is True


def test_throttle_disabled_when_threshold_is_zero(monkeypatch):
    """A 0 threshold opts the control out entirely (revert-safe by config):
    failures never throttle the XMLA path."""
    credential_cache._reset_for_tests()
    gov = JdbcConnectionGovernor(
        max_conn_per_ip=0, max_auth_failures=0, auth_failure_window_seconds=60
    )
    monkeypatch.setattr(auth_basic, "get_governor", lambda: gov)

    async def _reject(username, password):
        _reject_401()

    monkeypatch.setattr(auth_basic, "login_discover", _reject)
    client = _build_client()
    for i in range(6):
        resp = client.post(
            "/api/v1/xmla", content=_SOAP_NO_CATALOG, headers=_basic("u", f"g{i}")
        )
        assert resp.status_code == 401  # never 429 — control is opted out
    credential_cache._reset_for_tests()
