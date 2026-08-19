"""Regression guards for Bug-8553.

The existing webhook lifecycle tests proved that the encrypted secret changes,
but did not assert the response contract that tells the modeller the change
happened. These tests keep the producer honest without ever expecting or
returning the plaintext signing secret.

Test escape: the old tests stopped at the database record and did not inspect
the config response. Guard: these response assertions fail against the old
producer and distinguish URL rotation from an unrelated save. Tier: T2.
"""
from __future__ import annotations

import types
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _record(*, webhook_url: str, webhook_signing_secret: bytes):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        webhook_url=webhook_url,
        webhook_signing_secret=webhook_signing_secret,
        enabled=False,
    )


def _user():
    return types.SimpleNamespace(
        user_id="modeler@example.com",
        tenant_id="acme",
        role="modeler",
    )


async def _patch_config(record, body, *, generated_secret=None):
    from src.api import agent_config

    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = record
    db.execute = AsyncMock(return_value=result)

    async def _db_gen(_tenant_id):
        yield db

    patches = [
        patch.object(agent_config, "get_tenant_db", _db_gen),
        patch.object(agent_config, "_require_project_modeller", AsyncMock()),
    ]
    if generated_secret is not None:
        patches.append(
            patch.object(
                agent_config,
                "generate_signing_secret",
                return_value=generated_secret,
            )
        )

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        return await agent_config.patch_agent_config(
            record.project_id, body, _user()
        )


async def _put_config(record, body, *, generated_secret=None):
    from shared.db.models import Project
    from src.api import agent_config

    db = AsyncMock()
    db.get = AsyncMock(return_value=Project(id=record.project_id))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = record
    models_result = MagicMock()
    models_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[config_result, models_result])

    async def _db_gen(_tenant_id):
        yield db

    patches = [
        patch.object(agent_config, "get_tenant_db", _db_gen),
        patch.object(agent_config, "_require_project_modeller", AsyncMock()),
    ]
    if generated_secret is not None:
        patches.append(
            patch.object(
                agent_config,
                "generate_signing_secret",
                return_value=generated_secret,
            )
        )

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        return await agent_config.upsert_agent_config(
            record.project_id, body, _user()
        )


@pytest.mark.asyncio
async def test_bug_8553_response_flags_url_rotation_without_returning_plaintext():
    """A changed receiver is signalled, but the response remains non-secret."""
    from src.api.agent_config import AgentConfigPatch

    record = _record(
        webhook_url="https://receiver-a.example/hooks/old",
        webhook_signing_secret=b"secret-for-a",
    )
    response = await _patch_config(
        record,
        AgentConfigPatch(webhook_url="https://receiver-b.example/hooks/new"),
        generated_secret=("plaintext-b", b"secret-for-b"),
    )

    assert response.webhook_secret_rotated is True
    assert "webhook_signing_secret" not in response.model_dump()
    assert "plaintext-b" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_bug_8553_response_does_not_flag_an_unrelated_save():
    """Editing another setting does not manufacture a rotation notice."""
    from src.api.agent_config import AgentConfigPatch

    record = _record(
        webhook_url="https://receiver-a.example/hooks/old",
        webhook_signing_secret=b"secret-for-a",
    )
    response = await _patch_config(
        record,
        AgentConfigPatch(display_name="Updated assistant"),
    )

    assert response.webhook_secret_rotated is False


@pytest.mark.asyncio
async def test_bug_8553_put_response_flags_the_spa_save_path():
    """The PUT route used by the SPA carries the same non-secret signal."""
    from src.api.agent_config import AgentConfigUpsert

    record = _record(
        webhook_url="https://receiver-a.example/hooks/old",
        webhook_signing_secret=b"secret-for-a",
    )
    response = await _put_config(
        record,
        AgentConfigUpsert(webhook_url="https://receiver-b.example/hooks/new"),
        generated_secret=("plaintext-b", b"secret-for-b"),
    )

    assert response.webhook_secret_rotated is True
    assert "plaintext-b" not in response.model_dump_json()
