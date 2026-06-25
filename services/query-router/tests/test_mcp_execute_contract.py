"""F-027-02 — MCP execute_query <-> ExecuteRequest contract tests.

The MCP server's client (``tessallite/mcp-server/src/client.py``) posts
to ``/api/v1/execute``. Its body must satisfy the ``ExecuteRequest``
Pydantic schema — the 2026-06-11 review found it sending ``sql`` where
the schema requires ``raw_query`` (422 on every call) and a
``row_limit`` field the schema silently dropped.

These tests machine-check both sides of the seam:
  1. the schema's required fields and the new ``row_limit`` property;
  2. the exact body keys the MCP client emits (extracted from its AST,
     so either side drifting fails this test);
  3. the server-side ``row_limit`` clamp semantics.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Same sys.modules pollution guard as test_query_identity_logging.py:
# other tests inject stub shared.db modules; once polluted, src.api.routes
# cannot be imported from disk in this process.
_SHARED_DB_SESSION = sys.modules.get("shared.db.session")
_POLLUTED = _SHARED_DB_SESSION is not None and getattr(
    _SHARED_DB_SESSION, "__file__", None
) is None

pytestmark = pytest.mark.skipif(
    _POLLUTED,
    reason="sys.modules already polluted by another test; run this file alone",
)

_MCP_CLIENT_PATH = (
    Path(__file__).resolve().parents[3] / "mcp-server" / "src" / "client.py"
)


def _mcp_execute_query_body_keys() -> set[str]:
    """Extract the body keys the MCP client's execute_query emits."""
    tree = ast.parse(_MCP_CLIENT_PATH.read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_query":
            for sub in ast.walk(node):
                # body: dict = {...}
                if isinstance(sub, ast.Dict):
                    for k in sub.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
                # body["persona_id"] = ...
                if isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if (
                            isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.slice, ast.Constant)
                            and isinstance(tgt.slice.value, str)
                        ):
                            keys.add(tgt.slice.value)
    assert keys, "execute_query body dict not found in mcp-server client.py"
    return keys


def test_execute_request_required_fields():
    from src.api.routes import ExecuteRequest

    schema = ExecuteRequest.model_json_schema()
    assert set(schema.get("required", [])) == {"model_id", "raw_query"}
    assert "row_limit" in schema["properties"], (
        "row_limit must be an accepted ExecuteRequest field (F-027-02)"
    )
    assert "sql" not in schema["properties"]


def test_mcp_client_body_matches_execute_request_schema():
    from src.api.routes import ExecuteRequest

    schema = ExecuteRequest.model_json_schema()
    properties = set(schema["properties"])
    required = set(schema.get("required", []))
    emitted = _mcp_execute_query_body_keys()

    unknown = emitted - properties
    assert not unknown, (
        f"MCP client sends fields ExecuteRequest does not define: {sorted(unknown)} "
        "— Pydantic would silently drop them (F-027-02)"
    )
    missing = required - emitted
    assert not missing, (
        f"MCP client omits required ExecuteRequest fields: {sorted(missing)}"
    )


def test_mcp_protocol_keeps_label_but_parses_as_jdbc():
    """B10 round-1 finding 5: protocol "mcp" must be attributable in
    telemetry (LogicalQuery.protocol == "mcp") while keeping the exact
    JDBC parse strictness."""
    from src.api.routes import ExecuteRequest, _parse

    body = ExecuteRequest(
        model_id="m-1",
        raw_query="SELECT region, SUM(amount) FROM t GROUP BY region",
        protocol="mcp",
    )
    lq = _parse(body)
    assert lq.protocol == "mcp"


def test_mcp_protocol_enforces_jdbc_group_by_strictness():
    """An aggregate mixed with a bare ungrouped column must raise for
    "mcp" exactly as it does for "jdbc" — the relabel must not weaken
    parser validation."""
    from src.api.routes import ExecuteRequest, _parse
    from src.parsing.sql_parser import GroupByError

    body = ExecuteRequest(
        model_id="m-1",
        raw_query="SELECT region, SUM(amount) FROM t",
        protocol="mcp",
    )
    with pytest.raises(GroupByError):
        _parse(body)


@pytest.mark.asyncio
async def test_row_limit_clamps_parsed_limit(monkeypatch):
    """body.row_limit must clamp the parsed query's LIMIT (smaller wins)."""
    captured = await _run_handle_execute(monkeypatch, parsed_limit=1000, row_limit=10)
    assert captured["limit"] == 10


@pytest.mark.asyncio
async def test_row_limit_applies_when_query_has_no_limit(monkeypatch):
    captured = await _run_handle_execute(monkeypatch, parsed_limit=None, row_limit=25)
    assert captured["limit"] == 25


@pytest.mark.asyncio
async def test_smaller_query_limit_wins_over_row_limit(monkeypatch):
    captured = await _run_handle_execute(monkeypatch, parsed_limit=5, row_limit=100)
    assert captured["limit"] == 5


@pytest.mark.asyncio
async def test_handle_execute_reuses_resolved_persona_without_db_reload(monkeypatch):
    """F-008-23: route handlers already resolve the effective persona.
    _handle_execute must reuse that row for policy enforcement instead of
    loading it again through apply_persona_gate."""
    persona = SimpleNamespace(id="persona-1")

    captured = await _run_handle_execute(
        monkeypatch,
        parsed_limit=None,
        row_limit=None,
        persona=persona,
        persona_id="persona-1",
        forbid_persona_gate=True,
    )

    assert captured["enforced_persona"] is persona
    assert captured["merged_persona"] is persona
    assert captured["routed_persona"] is persona
    assert captured["logged_persona_id"] == "persona-1"


async def _run_handle_execute(
    monkeypatch,
    *,
    parsed_limit,
    row_limit,
    persona=None,
    persona_id=None,
    forbid_persona_gate: bool = False,
) -> dict:
    """Drive _handle_execute with all pipeline seams stubbed; return the
    state of the logical query as the binder saw it."""
    from src.api import routes as routes_mod
    from src.api.routes import ExecuteRequest, _handle_execute

    captured: dict = {}

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
        "limit": parsed_limit,
        "offset": None,
    })()
    fake_bound = type("B", (), {
        "model": fake_model,
        "logical_query": fake_lq,
        "resolved_dimensions": [],
        "resolved_measures": [],
        "resolved_filters": [],
        "has_passthrough_expressions": False,
    })()
    fake_decision = type("D", (), {
        "route_type": "source",
        "reason": "x",
        "rewritten_query": "",
        "aggregate_id": None,
        "pocket_id": None,
    })()

    monkeypatch.setattr(routes_mod, "_parse", lambda body: fake_lq)

    async def fake_bind(lq, db, include_hidden=False):
        captured["limit"] = lq.limit
        return fake_bound

    monkeypatch.setattr(routes_mod, "bind_query_to_model", fake_bind)

    async def fake_persona_gate(*a, **kw):
        captured["persona_gate_called"] = True
        if forbid_persona_gate:
            raise AssertionError(
                "resolved persona must not be reloaded by apply_persona_gate"
            )
        return None

    monkeypatch.setattr(routes_mod, "apply_persona_gate", fake_persona_gate)
    captured["persona_gate_called"] = False

    async def fake_enforce_persona_gate(*args, **kwargs):
        captured["enforced_persona"] = kwargs["persona"]

    monkeypatch.setattr(routes_mod, "enforce_persona_gate", fake_enforce_persona_gate)

    def fake_merge_default_filters(persona_arg, bound_arg):
        captured["merged_persona"] = persona_arg
        return []

    monkeypatch.setattr(routes_mod, "merge_default_filters", fake_merge_default_filters)
    monkeypatch.setattr(routes_mod, "audit_filters_present", lambda *a, **kw: None)
    monkeypatch.setattr(routes_mod, "audit_result_columns", lambda *a, **kw: None)

    async def fake_route(bound, db, **kwargs):
        captured["routed_persona"] = kwargs.get("persona")
        return fake_decision

    monkeypatch.setattr(routes_mod, "route_query", fake_route)

    async def fake_execute(bound, decision, db):
        return ([], 0, [], "source")

    monkeypatch.setattr(routes_mod, "execute_routed_query", fake_execute)

    async def fake_log_query(**kwargs):
        captured["logged_persona_id"] = (
            str(kwargs["persona_id"]) if kwargs.get("persona_id") is not None else None
        )

    async def fake_log_query_miss(*a, **kw):
        pass

    async def fake_audit(*a, **kw):
        pass

    monkeypatch.setattr(routes_mod, "log_query", fake_log_query)
    monkeypatch.setattr(routes_mod, "log_query_miss", fake_log_query_miss)
    monkeypatch.setattr(routes_mod, "audit", fake_audit)

    async def fake_build_trace(*args, **kwargs):
        from src.api.routes import PipelineTrace
        return PipelineTrace()

    monkeypatch.setattr(routes_mod, "_build_trace", fake_build_trace)

    body = ExecuteRequest(
        model_id="m-1",
        # Unique per parameterisation: the module-global result cache keys
        # on raw_query, and a cache hit would skip the bind seam captured
        # by this harness.
        raw_query=f"SELECT 1 /* clamp {parsed_limit}/{row_limit} */",
        protocol="jdbc",
        row_limit=row_limit,
    )
    await _handle_execute(
        body,
        _FakeDB(),
        user_identity="user@tenant.com",
        persona_id=persona_id,
        persona=persona,
    )
    return captured
