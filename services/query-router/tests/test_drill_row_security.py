"""Drill-through row security — semantic gateway path.

Row security is now applied by the ``_handle_execute`` pipeline
automatically.  These tests verify that:

1. The drill handler delegates to ``_handle_execute`` with the correct
   ``principal`` so row security fires.
2. ``_handle_execute`` propagation: the principal flows through
   unchanged.
3. When ``principal`` is None (legacy callers), no error is raised.

The actual row-security wrap logic is tested in the execute pipeline's
own test suite.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.security import Principal
from src.api.drill_routes import DrillThroughRequest, _handle_drill_through
from src.drill.semantic_builder import encode_cursor

pytestmark = pytest.mark.asyncio


def _uuid():
    return uuid.uuid4()


def _build_sql_return(**overrides):
    defaults = dict(
        sql='SELECT "month", SUM("amount") AS "amount" FROM "modely" GROUP BY "month" LIMIT 101',
        model_id_str=str(_uuid()),
        offset=0,
        effective_limit=100,
        drill_dim=None,
        drill_mode="leaf",
        hierarchy_path=[],
        drillable=[],
        fact_table="orders",
        source_join_path=[],
    )
    defaults.update(overrides)
    return (
        defaults["sql"],
        defaults["model_id_str"],
        defaults["offset"],
        defaults["effective_limit"],
        defaults["drill_dim"],
        defaults["drill_mode"],
        defaults["hierarchy_path"],
        defaults["drillable"],
        defaults["fact_table"],
        defaults["source_join_path"],
    )


def _execute_response(rows=None):
    return types.SimpleNamespace(
        rows=rows or [],
        columns=["month", "amount"],
        route_type="source",
        execution_ms=10,
        bytes_processed=512,
        rows_returned=len(rows or []),
    )


# ---------------------------------------------------------------------------
# 1. Principal propagates to _handle_execute
# ---------------------------------------------------------------------------


async def test_drill_passes_principal_to_execute():
    db = AsyncMock()
    body = DrillThroughRequest(limit=2)
    principal = Principal(
        user_identity="alice@acme.com",
        roles=frozenset({"region_manager_north"}),
    )
    captured = {}

    async def _fake_execute(req, db, **kwargs):
        captured["principal"] = kwargs.get("principal")
        return _execute_response()

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        await _handle_drill_through(
            _uuid(), body, db,
            principal=principal,
            user_identity="alice@acme.com",
        )

    assert captured["principal"] is principal
    assert captured["principal"].user_identity == "alice@acme.com"


# ---------------------------------------------------------------------------
# 2. principal=None (legacy callers) — still works
# ---------------------------------------------------------------------------


async def test_drill_principal_none_is_safe():
    db = AsyncMock()
    body = DrillThroughRequest(limit=2)
    captured = {}

    async def _fake_execute(req, db, **kwargs):
        captured["principal"] = kwargs.get("principal")
        return _execute_response()

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        await _handle_drill_through(_uuid(), body, db, principal=None)

    assert captured["principal"] is None


# ---------------------------------------------------------------------------
# 3. user_identity flows through to _handle_execute for audit logging
# ---------------------------------------------------------------------------


async def test_drill_passes_user_identity_to_execute():
    db = AsyncMock()
    body = DrillThroughRequest(limit=2)
    captured = {}

    async def _fake_execute(req, db, **kwargs):
        captured["user_identity"] = kwargs.get("user_identity")
        return _execute_response()

    with (
        patch(
            "src.api.drill_routes.build_drill_sql",
            new=AsyncMock(return_value=_build_sql_return()),
        ),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        await _handle_drill_through(
            _uuid(), body, db,
            user_identity="bob@acme.com",
        )

    assert captured["user_identity"] == "bob@acme.com"
