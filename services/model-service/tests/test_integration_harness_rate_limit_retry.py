"""
Guard for the integration harness's 429 handling (Bug-8863 / Bug-8865 / Bug-8885).

The behaviour under test is not a product endpoint — it is the property that
makes the model-service integration tier's green mean something. Those tests
drive ONE live service whose ``TenantRateLimitMiddleware`` caps a tenant at
``rate_limit.per_minute`` requests and answers the excess with HTTP 429 +
``Retry-After``. Before this guard existed the harness had no 429 handling at
all, so a throttled request became an ordinary assertion failure inside
whichever helper made it, with a membership that drifted run to run purely
with wall-clock request rate. That drift was mis-attributed to unrelated code
changes more than once.

Correctness of the tier therefore rests on: a throttled request is re-issued
per the ``Retry-After`` contract; persistent throttling is named rather than
disguised as a product failure; and nothing outside the live integration
origin is touched, so the in-process unit tests keep their exact semantics.

Pure tests: no live services, no Docker, no network.
"""
from __future__ import annotations

import httpx
import pytest

from tests.integration.rate_limit_retry import (
    RateLimitExhausted,
    parse_retry_after,
    send_with_retry,
    targets_host,
)

API_BASE = "http://localhost:8001/api/v1"
OTHER_ORIGIN = "http://elsewhere.test/api/v1"


class _Response:
    def __init__(self, status_code: int, retry_after: str | None = None):
        self.status_code = status_code
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Request:
    def __init__(self, url: str, method: str = "POST"):
        self.url = url
        self.method = method


class _Sender:
    """Replays a fixed list of responses and records how often it was called."""

    def __init__(self, responses: list[_Response]):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, request, *args, **kwargs):
        self.calls += 1
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


@pytest.fixture
def slept():
    """Collects the sleep durations the retry asks for instead of waiting."""
    waits: list[float] = []
    return waits, waits.append


# ---------------------------------------------------------------------------
# The core property: throttling must cost time, never correctness
# ---------------------------------------------------------------------------


def test_throttled_request_is_reissued_until_the_service_accepts_it(slept):
    """Bug-8865: a 429 used to surface as "Create failed (429)"."""
    waits, sleep = slept
    sender = _Sender([
        _Response(429, retry_after="1"),
        _Response(429, retry_after="2"),
        _Response(201),
    ])

    response = send_with_retry(
        sender,
        _Request(f"{API_BASE}/projects/p/models/m/kpis"),
        api_base=API_BASE,
        sleep=sleep,
    )

    assert response.status_code == 201, (
        "A throttled request must be re-issued and its eventual real response "
        "returned — not reported as a product failure"
    )
    assert sender.calls == 3
    assert waits == [1.0, 2.0], "Retry-After must be honoured, not guessed"


def test_persistent_throttling_raises_a_named_error_not_a_bare_429(slept):
    """An environment condition must never masquerade as a product assertion."""
    waits, sleep = slept
    sender = _Sender([_Response(429, retry_after="1")])

    with pytest.raises(RateLimitExhausted) as excinfo:
        send_with_retry(
            sender,
            _Request(f"{API_BASE}/projects/p/models/m/kpis"),
            api_base=API_BASE,
            sleep=sleep,
        )

    assert "429" in str(excinfo.value)
    assert sender.calls > 1, "The budget must include at least one retry"


def test_throttled_responses_are_closed_before_the_next_attempt(slept):
    """Each discarded 429 must release its connection."""
    _, sleep = slept
    throttled = _Response(429, retry_after="0")
    sender = _Sender([throttled, _Response(200)])

    send_with_retry(
        sender, _Request(f"{API_BASE}/x"), api_base=API_BASE, sleep=sleep,
    )

    assert throttled.closed is True


# ---------------------------------------------------------------------------
# Scope guards: only 429, only the live integration origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 201, 400, 404, 409, 422, 500, 503])
def test_every_other_status_is_returned_on_the_first_attempt(status, slept):
    """A real defect must still fail the test that found it."""
    waits, sleep = slept
    sender = _Sender([_Response(status)])

    response = send_with_retry(
        sender, _Request(f"{API_BASE}/x"), api_base=API_BASE, sleep=sleep,
    )

    assert response.status_code == status
    assert sender.calls == 1
    assert waits == []


def test_requests_outside_the_integration_origin_are_never_retried(slept):
    """The in-process unit tests must keep their exact semantics."""
    waits, sleep = slept
    sender = _Sender([_Response(429, retry_after="1"), _Response(200)])

    response = send_with_retry(
        sender, _Request(f"{OTHER_ORIGIN}/x"), api_base=API_BASE, sleep=sleep,
    )

    assert response.status_code == 429, (
        "A 429 from anywhere but the live integration origin must pass "
        "through unchanged"
    )
    assert sender.calls == 1
    assert waits == []


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:8001/api/v1/projects", True),
        ("http://localhost:8001/health", True),
        ("http://localhost:8002/api/v1/projects", False),
        ("https://localhost:8001/api/v1/projects", False),
        ("http://otherhost:8001/api/v1/projects", False),
        ("http://testserver/api/v1/projects", False),
    ],
)
def test_origin_matching_covers_scheme_host_and_port(url, expected):
    assert targets_host(url, API_BASE) is expected


# ---------------------------------------------------------------------------
# Retry-After parsing must never stall or crash a run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("30", 30.0),
        ("0", 0.0),
        (None, 5.0),        # absent -> short probe inside the 60s window
        ("", 5.0),          # malformed -> short probe
        ("banana", 5.0),
        ("-5", 5.0),        # negative -> short probe
        ("99999", 65.0),    # clamped to the cap
    ],
)
def test_retry_after_is_parsed_defensively_and_clamped(raw, expected):
    assert parse_retry_after(raw, cap=65.0) == expected


# ---------------------------------------------------------------------------
# The wrapper as the conftest actually installs it, over a real httpx.Client
# ---------------------------------------------------------------------------


def test_patching_httpx_client_send_retries_a_real_client_and_restores_cleanly():
    """Exercises the exact patch shape tests/integration/conftest.py applies."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    original_send = httpx.Client.send

    def _send(self, request, *args, **kwargs):
        return send_with_retry(
            lambda req, *a, **kw: original_send(self, req, *a, **kw),
            request,
            *args,
            api_base=API_BASE,
            sleep=lambda _seconds: None,
            **kwargs,
        )

    httpx.Client.send = _send
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            resp = client.get(f"{API_BASE}/projects")
        assert resp.status_code == 200
        assert attempts["n"] == 2
    finally:
        httpx.Client.send = original_send

    assert httpx.Client.send is original_send, (
        "The integration package must leave httpx unpatched for the rest of "
        "the suite"
    )
