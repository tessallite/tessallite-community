"""Bug-9196/F01: signed embed claims stay separate at agent consumers."""
from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from shared.auth.jwt import decode_access_token
from shared.auth.middleware import CurrentEmbedUser, _build_user_from_payload
from src.api import agent_config

pytestmark = pytest.mark.unit

_MODEL_PERSONA_ID = UUID("22222222-2222-2222-2222-222222222222")
_PROJECT_PERSONA_ID = UUID("11111111-1111-1111-1111-111111111111")


@lru_cache(maxsize=1)
def _model_service_embed_producer():
    """Load the production model-service signer without replacing agent ``src``."""
    path = (
        Path(__file__).resolve().parents[2]
        / "model-service"
        / "src"
        / "auth"
        / "local_backend.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_l12_model_service_local_backend", path
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load embed producer from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _AllowListResult:
    def __init__(self, model_id: UUID):
        self.model_id = model_id

    def scalars(self):
        return self

    def all(self):
        return [self.model_id]


async def _run_selectable_models_route(
    user: CurrentEmbedUser,
    *,
    project_id: UUID,
    model_id: UUID,
    loader: AsyncMock,
) -> None:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_AllowListResult(model_id))

    async def _tenant_db(_tenant_id: str):
        yield db

    with (
        patch.object(agent_config, "get_tenant_db", new=_tenant_db),
        patch.object(
            agent_config,
            "require_project_chat_access",
            new=AsyncMock(),
        ),
        patch(
            "src.prompt.assembler.load_selectable_models",
            new=loader,
        ),
    ):
        result = await agent_config.list_selectable_models(
            project_id,
            current_user=user,
        )
    assert result == []


@pytest.mark.asyncio
async def test_signed_token_reaches_query_router_and_agent_config_in_separate_namespaces():
    """Use the production signer and shared decoder at both semantic consumers."""
    producer = _model_service_embed_producer()
    project_id = uuid4()
    model_id = uuid4()
    token, _ = producer.create_embed_token(
        user_identity="signed-agent-viewer@example.com",
        tenant_id="test-tenant",
        persona_id=str(_MODEL_PERSONA_ID),
        project_persona_id=str(_PROJECT_PERSONA_ID),
        project_ids=[str(project_id)],
        model_ids=[str(model_id)],
        capabilities=["chat"],
    )
    user = _build_user_from_payload(decode_access_token(token))
    assert isinstance(user, CurrentEmbedUser)
    assert user.persona_id == str(_MODEL_PERSONA_ID)
    assert user.project_persona_id == str(_PROJECT_PERSONA_ID)

    # Query-router's shared resolver reads only the model Persona claim.
    from shared.security.persona_resolver import resolve_effective_persona

    qr_db = AsyncMock()
    model_persona = object()
    with patch(
        "shared.security.persona_resolver.load_persona_or_fail",
        new=AsyncMock(return_value=model_persona),
    ) as load_persona:
        assert (
            await resolve_effective_persona(
                qr_db,
                current_user=user,
                model_id=model_id,
                requested_persona_id=None,
            )
            is model_persona
        )
    load_persona.assert_awaited_once_with(
        qr_db,
        str(_MODEL_PERSONA_ID),
        model_id,
    )

    # Agent config's direct consumer reads only project_persona_id. The model
    # Persona UUID must never be supplied to load_selectable_models.
    loader = AsyncMock(return_value=[])
    await _run_selectable_models_route(
        user,
        project_id=project_id,
        model_id=model_id,
        loader=loader,
    )
    assert loader.await_args.args[3] == _PROJECT_PERSONA_ID
    assert loader.await_args.args[3] != _MODEL_PERSONA_ID

    # A legacy signed token retains query-router's model lock but cannot create
    # an agent ProjectPersona lock by compatibility reinterpretation.
    legacy_token, _ = producer.create_embed_token(
        user_identity="legacy-agent-viewer@example.com",
        tenant_id="test-tenant",
        persona_id=str(_MODEL_PERSONA_ID),
        project_ids=[str(project_id)],
        model_ids=[str(model_id)],
        capabilities=["chat"],
    )
    legacy_user = _build_user_from_payload(decode_access_token(legacy_token))
    assert legacy_user.persona_id == str(_MODEL_PERSONA_ID)
    assert legacy_user.project_persona_id is None
    legacy_loader = AsyncMock(return_value=[])
    await _run_selectable_models_route(
        legacy_user,
        project_id=project_id,
        model_id=model_id,
        loader=legacy_loader,
    )
    assert legacy_loader.await_args.args[3] is None
