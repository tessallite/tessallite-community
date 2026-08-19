"""Wave C #5 — CLS reject-whole-query surface contract at the gateway.

A query that references a CLS-blocked column is refused wholesale by the
query-router (403); the gateway must translate that denial into the surface's
uniform access-denied signal — XMLA access-denied SOAP CLIENT fault, JDBC
SQLSTATE 42501 — never a partial/redacted result. (The query-router SELECT *
narrowing that still returns a partial result is tracked as a scope_request to
the query-router owner; this suite pins the gateway side.)
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from defusedxml import ElementTree as ET

from src.dax import xmla_server as xs
from src.router_client import QueryRouterError
from src.jdbc.server import _router_error_sqlstate


# ---------------------------------------------------------------------------
# JDBC — a 403 denial maps to SQLSTATE 42501 (insufficient_privilege)
# ---------------------------------------------------------------------------

def test_jdbc_router_403_maps_to_42501():
    exc = QueryRouterError("Column restricted", 403)
    assert _router_error_sqlstate(exc) == "42501"


def test_jdbc_explicit_sqlstate_wins():
    exc = QueryRouterError("boom", 403, sqlstate="22P02")
    assert _router_error_sqlstate(exc) == "22P02"


def test_jdbc_non_403_defaults_to_42601():
    exc = QueryRouterError("Parse failed", 400)
    assert _router_error_sqlstate(exc) == "42601"


# ---------------------------------------------------------------------------
# XMLA — a 403 denial becomes an access-denied SOAP CLIENT fault (status 403)
# ---------------------------------------------------------------------------

def _patch_execute_for_denial(monkeypatch, exc: Exception) -> None:
    async def _resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None, None

    async def _meta(**kwargs):
        return ([{"id": "meas-1", "name": "amount"}], [], [])

    async def _named_sets(*a, **kw):
        return []

    def _stmt_to_sql(*a, **kw):
        return "SELECT 1", "jdbc"

    async def _raise(*a, **kw):
        raise exc

    monkeypatch.setattr(xs, "_resolve_model_id", _resolve_model_id)
    monkeypatch.setattr(xs, "_load_model_metadata_cached", _meta)
    monkeypatch.setattr(xs, "get_model_named_sets", _named_sets)
    monkeypatch.setattr(xs, "_statement_to_sql", _stmt_to_sql)
    monkeypatch.setattr(xs, "execute_query", _raise)


def _execute_el(statement: str):
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        '<Execute xmlns="urn:schemas-microsoft-com:xml-analysis">'
        f'<Command><Statement>{statement}</Statement></Command>'
        '<Properties><PropertyList><Catalog>m</Catalog></PropertyList></Properties>'
        '</Execute></soap:Body></soap:Envelope>'
    )
    root = ET.fromstring(envelope)
    return xs._find_method(root)


@pytest.mark.asyncio
async def test_xmla_cls_denial_is_client_fault_403(monkeypatch):
    denial = QueryRouterError(
        "This query references a column you are not permitted to see.", 403,
    )
    _patch_execute_for_denial(monkeypatch, denial)

    resp = await xs._handle_execute(
        _execute_el("SELECT {[Measures].[amount]} ON COLUMNS FROM [m]"),
        tenant_slug="demo", jwt_token="tok", session_id="sid-cls",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "<soap11env:Fault>" in body
    assert "soap11env:Client" in body, body  # access-denied is a CLIENT fault
    assert "not permitted" in body
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_xmla_non_403_router_error_is_server_fault(monkeypatch):
    # A non-denial router error keeps the generic Server fault mapping.
    _patch_execute_for_denial(monkeypatch, QueryRouterError("router 500", 500))
    resp = await xs._handle_execute(
        _execute_el("SELECT {[Measures].[amount]} ON COLUMNS FROM [m]"),
        tenant_slug="demo", jwt_token="tok", session_id="sid-500",
    )
    body = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
    assert "soap11env:Server" in body, body
