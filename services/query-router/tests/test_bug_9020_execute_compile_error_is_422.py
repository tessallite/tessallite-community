"""F-007-02 / Bug-9020: /execute hoist compile maps to 422, not 500."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, status

from src.api import routes as _routes
from src.security import Principal, RowSecurityCompileError


pytestmark = pytest.mark.unit


def _bound(body: _routes.ExecuteRequest) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        model=types.SimpleNamespace(id="model-1", deployed_version_id="v1"),
        logical_query=types.SimpleNamespace(
            protocol="jdbc",
            raw_query=body.raw_query,
            query_fingerprint="fp",
            limit=None,
            grain=[],
            select_star=False,
            has_complex_sql=False,
        ),
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
    )


@pytest.mark.asyncio
async def test_bug_9020_execute_compile_error_is_422():
    """A bad row-security DSL on /execute is ``row_security_misconfigured``
    422, not an untyped 500. Guard: this test. Tier: T3.
    """
    body = _routes.ExecuteRequest(
        model_id="model-1",
        raw_query='SELECT SUM("Revenue") FROM "t"',
        protocol="jdbc",
    )
    bound = _bound(body)
    _routes._cache.clear()
    compile_error = RowSecurityCompileError(
        "unknown row-security function: 'dimension_in'"
    )
    log_mock = AsyncMock()
    route_mock = AsyncMock()

    with (
        patch.object(_routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(_routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(
            _routes,
            "_evaluate_bound_field_compatibility",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            _routes,
            "resolve_target_dialect_for_bound",
            new=AsyncMock(return_value="postgres"),
        ),
        patch.object(
            _routes,
            "compile_row_security",
            new=AsyncMock(side_effect=compile_error),
        ),
        patch.object(_routes, "_log_query_failure", new=log_mock),
        patch.object(_routes, "route_query", new=route_mock),
    ):
        with pytest.raises(HTTPException) as exc:
            await _routes._handle_execute(
                body,
                db=AsyncMock(),
                user_identity="analyst@acme-demo.com",
                principal=Principal(
                    user_identity="analyst@acme-demo.com",
                    roles=frozenset({"region_manager_emea"}),
                ),
                tenant_id="acme-demo",
            )

    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert exc.value.detail["error_type"] == "row_security_misconfigured"
    log_mock.assert_awaited()
    route_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_f008_22_persona_403_not_replaced_by_compatibility_422():
    """F-008-22: a persona OBJECT_NOT_AVAILABLE 403 must not be rewritten
    as a field-compatibility 422 that confirms the denied object exists.
    """
    body = _routes.ExecuteRequest(
        model_id="model-1",
        raw_query='SELECT SUM("base_amount") FROM "t"',
        protocol="jdbc",
    )
    bound = _bound(body)
    _routes._cache.clear()

    async def _deny(*_a, **_k):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "OBJECT_NOT_AVAILABLE",
                "message": "One or more requested objects are not available.",
            },
        )

    async def _incompat(*_a, **_k):
        return types.SimpleNamespace(status="incompatible")

    persona = types.SimpleNamespace(
        id="p-business", name="Business", included_measure_ids=["m-rev"],
    )
    route_mock = AsyncMock()

    with (
        patch.object(_routes, "_bind_query_parameters", new=AsyncMock()),
        patch.object(_routes, "bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch.object(_routes, "enforce_persona_gate", new=_deny),
        patch.object(
            _routes, "_evaluate_bound_field_compatibility", new=_incompat,
        ),
        patch.object(_routes, "route_query", new=route_mock),
    ):
        with pytest.raises(HTTPException) as exc:
            await _routes._handle_execute(
                body,
                db=AsyncMock(),
                user_identity="sales@acme-demo.com",
                persona=persona,
                tenant_id="acme-demo",
            )

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert exc.value.detail["error_code"] == "OBJECT_NOT_AVAILABLE"
    route_mock.assert_not_awaited()
