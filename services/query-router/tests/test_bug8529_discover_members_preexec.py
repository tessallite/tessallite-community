"""Bug-8529 regression coverage for discover-members pre-execution logging."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from src.ir.logical_query import DeployedSnapshotUnavailableError


_MODEL_ID = str(uuid.uuid4())


def _user() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        email="analyst@example.test",
        tenant_id="tenant-1",
        role="member",
        roles=["member"],
        groups=[],
        claims={},
    )


def _body() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        model_id=_MODEL_ID,
        dimension_name="Region",
        # Bug-9865: the real DiscoverMembersRequest carries an optional source
        # scan bound; the stand-in must match the model it stands in for.
        limit=None,
        persona_id=None,
    )


def _db_generator(db):
    async def _generate(*_args, **_kwargs):
        yield db

    return _generate


def _assert_logged(log_failure: AsyncMock, db, exc: HTTPException) -> None:
    log_failure.assert_awaited_once()
    args = log_failure.call_args.args
    assert args[0] is db
    assert args[1:3] == ("analyst@example.test", "tenant-1")
    assert args[3] == "DISCOVER_MEMBERS(Region)"
    assert args[4] == "discover_members"
    assert args[5] is exc
    assert log_failure.call_args.kwargs["client_kind"] == "discover_members"


@pytest.mark.asyncio
async def test_bug_8529_authorization_failure_is_logged_once():
    """Route authorization failures retain the shared pre-execution contract."""
    from src.api import routes

    db = AsyncMock()
    failure = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Model access denied",
    )
    log_failure = AsyncMock()

    with (
        patch.object(routes, "enforce_model_scope"),
        patch.object(routes, "get_tenant_db", _db_generator(db)),
        patch.object(routes, "load_authorized_model", AsyncMock(side_effect=failure)),
        patch.object(routes, "_log_preexec_failure", log_failure),
    ):
        with pytest.raises(HTTPException) as raised:
            await routes.discover_members(_body(), _user())

    assert raised.value is failure
    _assert_logged(log_failure, db, failure)


@pytest.mark.asyncio
async def test_bug_8529_model_scope_failure_is_logged_once():
    """The route's model-scope authorization check is inside the log boundary."""
    from src.api import routes

    db = AsyncMock()
    failure = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Model is outside the authenticated scope",
    )
    log_failure = AsyncMock()

    with (
        patch.object(routes, "enforce_model_scope", side_effect=failure),
        patch.object(routes, "get_tenant_db", _db_generator(db)),
        patch.object(routes, "load_authorized_model", AsyncMock()),
        patch.object(routes, "_log_preexec_failure", log_failure),
    ):
        with pytest.raises(HTTPException) as raised:
            await routes.discover_members(_body(), _user())

    assert raised.value is failure
    _assert_logged(log_failure, db, failure)


@pytest.mark.asyncio
async def test_bug_8529_bind_failure_is_logged_as_snapshot_unavailable():
    """A bind-stage unusable deployment keeps the 503 and QueryLog type."""
    from src.api import routes

    db = AsyncMock()
    failure = DeployedSnapshotUnavailableError("snapshot is unavailable")
    log_failure = AsyncMock()

    with (
        patch.object(routes, "enforce_model_scope"),
        patch.object(routes, "get_tenant_db", _db_generator(db)),
        patch.object(routes, "load_authorized_model", AsyncMock(return_value=None)),
        patch.object(routes, "resolve_execution_persona", AsyncMock(return_value=None)),
        patch.object(routes, "bind_query_to_model", AsyncMock(side_effect=failure)),
        patch.object(routes, "_log_preexec_failure", log_failure),
    ):
        with pytest.raises(HTTPException) as raised:
            await routes.discover_members(_body(), _user())

    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert routes._preexec_error_type(raised.value) == "snapshot_unavailable"
    _assert_logged(log_failure, db, raised.value)


@pytest.mark.asyncio
async def test_bug_8529_route_failure_is_logged_without_duplicate_execution_log():
    """A route-stage unusable deployment is logged once at the route boundary."""
    from src.api import routes

    db = AsyncMock()
    bound = types.SimpleNamespace(
        resolved_dimensions=[types.SimpleNamespace(name="Region")],
        logical_query=types.SimpleNamespace(),
    )
    failure = DeployedSnapshotUnavailableError("snapshot is unavailable")
    log_failure = AsyncMock()

    with (
        patch.object(routes, "enforce_model_scope"),
        patch.object(routes, "get_tenant_db", _db_generator(db)),
        patch.object(routes, "load_authorized_model", AsyncMock(return_value=None)),
        patch.object(routes, "resolve_execution_persona", AsyncMock(return_value=None)),
        patch.object(routes, "bind_query_to_model", AsyncMock(return_value=bound)),
        patch.object(routes, "_augment_discover_with_display_column", AsyncMock(return_value=None)),
        patch.object(routes.Principal, "from_current_user", return_value=MagicMock()),
        patch.object(routes, "route_query", AsyncMock(side_effect=failure)),
        patch.object(routes, "_log_preexec_failure", log_failure),
    ):
        with pytest.raises(HTTPException) as raised:
            await routes.discover_members(_body(), _user())

    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert routes._preexec_error_type(raised.value) == "snapshot_unavailable"
    _assert_logged(log_failure, db, raised.value)
