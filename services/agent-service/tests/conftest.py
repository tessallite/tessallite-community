"""Shared fixtures for agent-service tests."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from shared.config.fastapi_drift import check_fastapi_version_drift

check_fastapi_version_drift()

from src.main import app  # noqa: E402
from src.auth.middleware import CurrentUser, get_current_user  # noqa: E402


TEST_TENANT = "test-tenant"
TEST_USER_ID = "user@example.com"
TEST_PROJECT_ID = uuid.uuid4()
NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def async_gen_from(value):
    async def _gen(*args, **kwargs):
        yield value
    return _gen


def make_mock_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.delete = AsyncMock()

    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    default_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=default_result)
    return db


def make_agent_config(
    project_id: uuid.UUID = TEST_PROJECT_ID,
    enabled: bool = True,
    session_history_depth: int = 20,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        enabled=enabled,
        session_history_depth=session_history_depth,
        conversation_retention_days=30,
    )


def make_turn(
    conversation_id: uuid.UUID | None = None,
    turn_index: int = 0,
    user_message: str = "test question",
    answer_text: str | None = "test answer",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=conversation_id or uuid.uuid4(),
        turn_index=turn_index,
        user_message=user_message,
        answer_text=answer_text,
    )


@pytest.fixture
def auth():
    user = CurrentUser(
        user_id=TEST_USER_ID,
        tenant_id=TEST_TENANT,
        email=TEST_USER_ID,
        role="tenant_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client(auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.fixture(autouse=True)
def executor_surface_passthrough(request):
    """Neutralise the Bug-9897 query-router grounding hop for unrelated tests.

    ``assemble_prompt`` / ``load_selectable_models`` narrow the grounding
    catalogue to the field surface the query-router says it will accept for the
    caller (``_apply_executor_surface``). That hop is an EXTERNAL dependency of
    every prompt-rendering test in this suite, and those tests are about what
    the prompt renders, not about the router verdict — without this they would
    all ground on nothing, because a test that supplies no bearer correctly
    gets no verdict (fail closed).

    Tests that are ABOUT the verdict mark themselves ``executor_surface_real``
    and exercise the production narrowing.
    """
    if request.node.get_closest_marker("executor_surface_real"):
        yield
        return
    with patch(
        "src.prompt.assembler._apply_executor_surface",
        side_effect=lambda profiles, surfaces: profiles,
    ):
        yield
