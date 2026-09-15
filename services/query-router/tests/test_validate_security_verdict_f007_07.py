"""F-007-07 / F-008-19 / Bug-9088 / Bug-9843: expect=error cannot PASS on a
connect failure — and the mid-query variants that Bug-9838 owns.

The tests below named Bug-9088 only, so Bug-9838 — the issue that actually owns
the mid-query classification and the direct-runner rollback crash — read as
unguarded in the coverage report. Fixed here rather than left as an example of
the very pattern Bug-9843 tracks: the tests were written in the same session
that filed Bug-9838, which is how easily it happens.
"""
from __future__ import annotations

import pytest

from validate_security import QueryResult, SecurityQuery, _validate_query
import validate_security as vs

# Bug-9901: `validate_security.psycopg2` is an optional import — `None` when
# the driver is not installed (see validate_security.py's own try/except).
# Neither the real CI query-router job (.github/workflows/ci.yml) nor its
# local mirror (scripts/run-like-ci.sh) installs psycopg2 for this service:
# only the `python-tests-shared` CI job pins `psycopg2-binary`, for a
# different suite (`tests/unit/test_suite_gate.py`'s JDBC liveness check).
# The three tests below patch `vs.psycopg2.connect`, which raised a bare
# `AttributeError: None has no attribute 'connect'` in that environment —
# the silent-failure shape this convention exists to avoid. Skip explicitly,
# matching the `skipif(not _HAS_X, ...)` convention already used in this
# test suite (see test_semi_additive.py) rather than let the driver-absent
# case masquerade as a product failure or vanish into an unguarded error.
_HAS_PSYCOPG2 = vs.psycopg2 is not None
_NO_PSYCOPG2_REASON = (
    "psycopg2 is not installed in this test environment (neither the "
    "query-router CI job nor its local mirror installs it) -- Bug-9901"
)


def _q() -> SecurityQuery:
    return SecurityQuery(
        label="SB01",
        description="persona blocks hidden measure",
        sql="SELECT fee_amount FROM x",
        table_variant="_restricted",
        expect="error",
    )


def test_f007_07_transport_error_is_environment_not_ready_not_pass():
    result = QueryResult(
        label="SB01",
        columns=[],
        rows=[],
        row_count=0,
        error="Connection error: port 5433",
        transport_error=True,
    )
    verdict, reason = _validate_query(_q(), result)
    assert verdict == "FAIL"
    assert "ENVIRONMENT_NOT_READY" in reason


def test_f007_07_typed_persona_denial_is_pass():
    result = QueryResult(
        label="SB01",
        columns=[],
        rows=[],
        row_count=0,
        error="OBJECT_NOT_AVAILABLE: one or more requested objects",
        transport_error=False,
    )
    verdict, reason = _validate_query(_q(), result)
    assert verdict == "PASS"
    assert "Correctly blocked" in reason


def test_f007_07_untyped_500_is_fail_not_pass():
    result = QueryResult(
        label="SB01",
        columns=[],
        rows=[],
        row_count=0,
        error="Internal Server Error",
        transport_error=False,
    )
    verdict, reason = _validate_query(_q(), result)
    assert verdict == "FAIL"
    assert "Expected a product security denial" in reason


# ---------------------------------------------------------------------------
# Bug-9088 — the transport classes the connect-time guard above does not cover.
#
# Connect failure was only the class that was OBSERVED. A session can also die
# MID-QUERY: the gateway restarts, its own watchdog recycles the accept loop, a
# proxy times out. psycopg2 reports that in the same place a server-side refusal
# arrives, so classifying only connect-time failures left the same "a broken
# environment is indistinguishable from an enforced refusal" hole open one step
# further along.
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, raises):
        self._raises = raises
        self.description = None

    def execute(self, _sql):
        raise self._raises


class _FakeConn:
    """A psycopg2-shaped connection whose ``closed`` says whether it survived."""

    def __init__(self, raises, closed_after):
        self._raises = raises
        self._closed_after = closed_after
        self.closed = 0
        self.autocommit = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self._raises)

    def rollback(self):
        self.rolled_back = True
        if self.closed:
            raise RuntimeError("connection already closed")

    def close(self):
        self.closed = self.closed or 1

    def _die(self):
        self.closed = self._closed_after


def _run_gateway_with(monkeypatch, conn):
    import validate_security as vs

    def _connect(**_kw):
        conn._die()
        return conn

    monkeypatch.setattr(vs.psycopg2, "connect", _connect)
    return vs._run_query_jdbc("SELECT 1", "SB01")


@pytest.mark.skipif(not _HAS_PSYCOPG2, reason=_NO_PSYCOPG2_REASON)
def test_bug9088_a_session_lost_mid_query_is_a_transport_error(monkeypatch):
    """The gateway died while answering: no product answer was observed."""
    conn = _FakeConn(RuntimeError("server closed the connection unexpectedly"),
                     closed_after=2)
    result = _run_gateway_with(monkeypatch, conn)

    assert result.transport_error is True, (
        "a failure that took the session with it is a transport failure, not a "
        "security refusal"
    )
    verdict, reason = _validate_query(_q(), result)
    assert verdict == "FAIL"
    assert "ENVIRONMENT_NOT_READY" in reason


@pytest.mark.skipif(not _HAS_PSYCOPG2, reason=_NO_PSYCOPG2_REASON)
def test_bug9088_a_server_refusal_on_a_live_session_is_not_transport(monkeypatch):
    """The session survived, so the product answered — this must stay judgeable.

    The guard must not over-classify: turning a real denial into
    ENVIRONMENT_NOT_READY would lose the only positive evidence the suite has
    that the persona gate rejects anything.
    """
    conn = _FakeConn(RuntimeError("OBJECT_NOT_AVAILABLE: fee_amount"),
                     closed_after=0)
    result = _run_gateway_with(monkeypatch, conn)

    assert result.transport_error is False
    verdict, _ = _validate_query(_q(), result)
    assert verdict == "PASS"


@pytest.mark.skipif(not _HAS_PSYCOPG2, reason=_NO_PSYCOPG2_REASON)
def test_bug9088_direct_runner_survives_a_rollback_on_a_dead_session(monkeypatch):
    """A broken session made the direct runner raise out of its own handler.

    ``conn.rollback()`` on a connection the server has dropped raises, and it sat
    inside the ``except`` block — so a mid-query disconnect crashed the scenario
    with an unhandled exception instead of returning a result the suite could
    judge. The failure it was hiding is exactly the one this issue is about.
    """
    import validate_security as vs

    conn = _FakeConn(RuntimeError("SSL connection has been closed unexpectedly"),
                     closed_after=2)

    def _connect(**_kw):
        conn._die()
        return conn

    monkeypatch.setattr(vs.psycopg2, "connect", _connect)
    result = vs._run_query_direct("SELECT 1", "DIRECT")

    assert result.transport_error is True
    assert "closed" in result.error.lower()
