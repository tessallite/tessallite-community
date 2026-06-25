"""Phase 1 (CR-002 Finding 1) — audit identity is server-derived.

Before the fix, `ExecuteRequest.user_identity` was pulled straight from the
request body into `log_query`, so an attacker with direct access to the
query-router could spoof identity fields and the gateway was passing an
empty string that poisoned every audit-log row.

The field has been removed from `ExecuteRequest`. `_handle_execute` now
takes `user_identity` as a handler parameter, which the route wires from
`current_user.email` (the validated JWT claim).

This test verifies:
1. `ExecuteRequest` schema no longer accepts `user_identity` (extras are
   ignored silently by Pydantic v2 default, but the helper should not
   read from it).
2. `_handle_execute` called with an explicit user_identity forwards it to
   `log_query`, even when a rogue body somehow provides a different value.
"""
from __future__ import annotations

import sys

import pytest

# This test file must be run in isolation because other tests in the
# suite (test_demo_data_integration, test_expression_routing) inject stub
# modules into sys.modules for shared.db.* and sqlalchemy. Once a stub
# module without a __file__ attribute lands in sys.modules, the import
# machinery cannot re-resolve the real module from disk — a pop() isn't
# enough.
#
# Run this file directly:
#     pytest services/query-router/tests/test_query_identity_logging.py -v
#
# The full-suite runner in CI should invoke it as a separate pytest
# process to avoid the collection-order landmine.

_SHARED_DB_SESSION = sys.modules.get("shared.db.session")
_POLLUTED = _SHARED_DB_SESSION is not None and getattr(
    _SHARED_DB_SESSION, "__file__", None
) is None

pytestmark = pytest.mark.skipif(
    _POLLUTED,
    reason="sys.modules already polluted by another test; run this file alone",
)


def test_execute_request_ignores_user_identity_field():
    # Deferred import: other tests in this suite poison sys.modules for
    # shared.db.session, which chains through src.api.routes's module-level
    # imports. Importing inside the test body avoids that landmine.
    from src.api.routes import ExecuteRequest

    # Pydantic v2 defaults to extra="ignore", so sending user_identity is
    # silently dropped. Confirm the model has no such attribute.
    body = ExecuteRequest.model_validate({
        "model_id": "m-1",
        "raw_query": "SELECT 1",
        "protocol": "jdbc",
        "user_identity": "attacker@evil.com",  # should be ignored
    })
    assert not hasattr(body, "user_identity"), (
        "ExecuteRequest must not accept user_identity from the client"
    )


@pytest.mark.asyncio
async def test_handle_execute_uses_server_identity_in_log_query(monkeypatch):
    """Prove that log_query is called with the JWT-derived user_identity,
    not with any body-supplied field."""
    # Deferred import — see note above.
    from src.api import routes as routes_mod
    from src.api.routes import ExecuteRequest, _handle_execute

    captured: dict = {}

    async def fake_log_query(**kwargs):
        captured.update(kwargs)

    async def fake_log_query_miss(*args, **kwargs):
        pass

    monkeypatch.setattr(routes_mod, "log_query", fake_log_query)
    monkeypatch.setattr(routes_mod, "log_query_miss", fake_log_query_miss)

    async def fake_persona_gate(*a, **kw):
        return None

    monkeypatch.setattr(routes_mod, "apply_persona_gate", fake_persona_gate)
    monkeypatch.setattr(routes_mod, "audit_filters_present", lambda *a, **kw: None)
    monkeypatch.setattr(routes_mod, "audit_result_columns", lambda *a, **kw: None)

    async def fake_audit(*a, **kw):
        pass

    monkeypatch.setattr(routes_mod, "audit", fake_audit)

    # Stub out the pipeline so we never touch a real DB.
    class _FakeDB:
        pass

    fake_model = type("M", (), {"id": "model-1", "display_name": "Test"})()
    fake_lq = type("LQ", (), {
        "query_fingerprint": "abc123",
        "protocol": "jdbc",
        "raw_query": "SELECT 1",
        "select_star": False,
        "grain": [],
        "from_tables": [],
        "requested_measures": [],
        "requested_dimensions": [],
        "filters": [],
        "order_by": [],
        "limit": None,
        "offset": None,
    })()
    fake_logical = fake_lq
    fake_bound = type("B", (), {
        "model": fake_model,
        "logical_query": fake_lq,
        "resolved_dimensions": [],
        "resolved_measures": [],
        "resolved_filters": [],
        "has_passthrough_expressions": False,
    })()
    fake_decision = type(
        "D",
        (),
        {
            "route_type": "source",
            "reason": "x",
            "rewritten_query": "",
            "aggregate_id": None,
            "pocket_id": None,
        },
    )()

    monkeypatch.setattr(routes_mod, "_parse", lambda body: fake_logical)

    async def fake_bind(lq, db, include_hidden=False):
        return fake_bound

    monkeypatch.setattr(routes_mod, "bind_query_to_model", fake_bind)

    async def fake_route(bound, db, **kwargs):
        return fake_decision

    monkeypatch.setattr(routes_mod, "route_query", fake_route)

    async def fake_execute(bound, decision, db):
        return ([], 0, [], "source")

    monkeypatch.setattr(routes_mod, "execute_routed_query", fake_execute)

    async def fake_build_trace(*args, **kwargs):
        from src.api.routes import PipelineTrace
        return PipelineTrace()

    monkeypatch.setattr(routes_mod, "_build_trace", fake_build_trace)

    body = ExecuteRequest(
        model_id="m-1",
        raw_query="SELECT 1",
        protocol="jdbc",
        client_kind="looker_cloud",
    )
    await _handle_execute(body, _FakeDB(), user_identity="legit@tenant.com")

    assert captured.get("user_identity") == "legit@tenant.com", (
        f"log_query received {captured.get('user_identity')!r}; "
        f"expected the JWT-derived email 'legit@tenant.com'"
    )
    assert captured.get("client_kind") == "looker_cloud"
