"""Bug-8380 project exports keep every model on the project read session."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from shared.model_snapshot.project_serialiser import export_project

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_project_models_use_the_same_session_as_project_sections():
    project_id = uuid4()
    model_id = uuid4()
    project = SimpleNamespace(slug="project", display_name="Project", is_active=True)
    model = SimpleNamespace(id=model_id)
    models_result = MagicMock()
    models_result.scalars.return_value.all.return_value = [model]
    db = AsyncMock()
    db.get = AsyncMock(return_value=project)
    db.execute = AsyncMock(return_value=models_result)
    snapshot_model_mock = AsyncMock(
        return_value={"model": {"id": str(model_id)}}
    )

    with patch(
        "shared.model_snapshot.project_serialiser.snapshot_model",
        snapshot_model_mock,
    ):
        bundle = await export_project(
            project_id,
            db,
            tenant_slug="tenant",
            sections=set(),
        )

    assert bundle["models"] == [{"model": {"id": str(model_id)}}]
    snapshot_model_mock.assert_awaited_once_with(
        model_id, db, include_versions=True
    )
