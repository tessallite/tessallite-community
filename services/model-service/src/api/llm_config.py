"""LLM Provider Configuration CRUD + test endpoints — project-scoped.

Post 2026-04 admin-config restructure: ``LLMProviderConfig`` is a
project-scoped entity (FK ``project_id``). The endpoint moved from the
tenant-level ``/settings/llm`` to ``/api/v1/projects/{project_id}/llm-configs``.
The ``is_active`` "tenant default" concept is gone — the project's
default agent / judge LLM is selected via ``ProjectSetting`` keys
``agent.llm_config_id`` / ``agent.judge_llm_config_id``.

Endpoints:

  GET    /api/v1/projects/{project_id}/llm-configs
  POST   /api/v1/projects/{project_id}/llm-configs
  PUT    /api/v1/projects/{project_id}/llm-configs/{config_id}
  DELETE /api/v1/projects/{project_id}/llm-configs/{config_id}
  POST   /api/v1/projects/{project_id}/llm-configs/{config_id}/test

RBAC: ``require_role('admin')`` for write paths, ``require_role('viewer')``
for read paths. The LLM connection-test proxy routes require
``require_tenant_admin`` (Bug-5229) because the optimizer backend they
call gates at tenant-admin level.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings
from shared.db.models import LLMProviderConfig, Project
from shared.db.session import get_tenant_db
# Shared service-account-auth policy (single source of truth across the CRUD
# guard, the import neutraliser, and the Google adapter). Aliased to the
# historical private name used across this module + its tests.
from shared.llm.sa_auth import (
    service_account_auth_allowed,
    uses_service_account_auth as _uses_service_account_auth,
)
from shared.schemas.pydantic_models import (
    LLMConnectionTestRequest,
    LLMConnectionTestResponse,
    LLMProviderConfigCreate,
    LLMProviderConfigResponse,
    LLMProviderConfigUpdate,
)
from src.auth.middleware import CurrentUser, forbid_embed_user, require_tenant_admin
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(
    prefix="/projects/{project_id}/llm-configs",
    tags=["llm-config"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encrypt_api_key(raw: str | None) -> bytes | None:
    if raw is None:
        return None
    # Rotation-aware: encrypts under the current key (the first rotation key).
    from shared.security.credential_crypto import encrypt_str
    return encrypt_str(raw)


def _decrypt_api_key(encrypted: bytes | None) -> str | None:
    if encrypted is None:
        return None
    # Rotation-aware decrypt: current key first, then any previous key.
    from shared.security.credential_crypto import decrypt_str
    return decrypt_str(encrypted)


def _guard_service_account_auth(provider: str | None, config: dict | None) -> None:
    """Reject non-API-key (service-account/OAuth) LLM auth unless an operator has
    explicitly enabled it via ``LLM_ALLOW_SERVICE_ACCOUNT_AUTH``. Keeps spend on a
    bring-your-own API key by default so a project admin cannot point the agent at
    the deployment's cloud credentials (cost-leak hardening)."""
    if _uses_service_account_auth(provider, config) and not service_account_auth_allowed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Service-account / OAuth LLM auth (e.g. google_mode='vertex_ai') is "
                "disabled on this deployment. Use a bring-your-own API key instead, "
                "or set LLM_ALLOW_SERVICE_ACCOUNT_AUTH=true to allow cloud-credential "
                "billing."
            ),
        )


async def _ensure_project(db, project_id: UUID) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} not found",
        )
    return project


def _to_response(record: LLMProviderConfig) -> LLMProviderConfigResponse:
    resp = LLMProviderConfigResponse.model_validate(record)
    resp.has_api_key = record.encrypted_api_key is not None
    return resp


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=list[LLMProviderConfigResponse],
    dependencies=[require_role("viewer")],
)
async def list_llm_configs(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[LLMProviderConfigResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_project(db, project_id)
        result = await db.execute(
            select(LLMProviderConfig)
            .where(LLMProviderConfig.project_id == project_id)
            .order_by(LLMProviderConfig.created_at)
        )
        return [_to_response(c) for c in result.scalars().all()]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "",
    response_model=LLMProviderConfigResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("admin")],
)
async def create_llm_config(
    project_id: UUID,
    body: LLMProviderConfigCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> LLMProviderConfigResponse:
    _guard_service_account_auth(body.provider, body.config)
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_project(db, project_id)
        record = LLMProviderConfig(
            project_id=project_id,
            provider=body.provider,
            display_name=body.display_name,
            base_url=body.base_url,
            encrypted_api_key=_encrypt_api_key(body.api_key),
            model_name=body.model_name,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            timeout_seconds=body.timeout_seconds,
            config=body.config or {},
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return _to_response(record)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.put(
    "/{config_id}",
    response_model=LLMProviderConfigResponse,
    dependencies=[require_role("admin")],
)
async def update_llm_config(
    project_id: UUID,
    config_id: UUID,
    body: LLMProviderConfigUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> LLMProviderConfigResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_project(db, project_id)
        record = await db.get(LLMProviderConfig, config_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="LLM config not found")
        updates = body.model_dump(exclude_unset=True)
        # Guard on the EFFECTIVE post-update provider/config so an update can't
        # switch an existing row to service-account/OAuth auth either.
        _guard_service_account_auth(
            updates.get("provider", record.provider),
            updates.get("config", record.config),
        )
        if "api_key" in updates:
            record.encrypted_api_key = _encrypt_api_key(updates.pop("api_key"))
        for k, v in updates.items():
            setattr(record, k, v)
        await db.commit()
        await db.refresh(record)
        return _to_response(record)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete(
    "/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("admin")],
)
async def delete_llm_config(
    project_id: UUID,
    config_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_project(db, project_id)
        record = await db.get(LLMProviderConfig, config_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="LLM config not found")
        await db.delete(record)
        await db.commit()


@router.post(
    "/{config_id}/test",
    response_model=LLMConnectionTestResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def test_llm_config(
    project_id: UUID,
    config_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> LLMConnectionTestResponse:
    """Proxy the connection test to the optimizer service, which owns the LLM adapters."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_project(db, project_id)
        record = await db.get(LLMProviderConfig, config_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="LLM config not found")
        break

    import httpx
    try:
        headers = {}
        if current_user.raw_token:
            headers["Authorization"] = f"Bearer {current_user.raw_token}"
        async with httpx.AsyncClient(
            timeout=float(system_snapshot_get("control.llm_config_timeout"))
        ) as client:
            resp = await client.post(
                f"{settings.OPTIMIZER_URL}/api/v1/optimize/llm/{config_id}/test",
                params={"tenant_id": current_user.tenant_id},
                headers=headers,
            )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="LLM config not found")
        if resp.status_code in (401, 403):
            # Review F-6: the optimizer gates LLM tests at tenant_admin; a
            # downstream authorisation rejection is the caller's 403, not a
            # 502 gateway fault. Gate alignment itself is tracked under
            # F-011-11.
            raise HTTPException(
                status_code=403,
                detail="LLM connection tests require tenant administrator access",
            )
        if not resp.is_success:
            raise HTTPException(
                status_code=502,
                detail=f"Optimizer returned {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        return LLMConnectionTestResponse(
            success=data.get("ok", False),
            message=data.get("error") or "Connection successful",
            latency_ms=data.get("latency_ms"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach optimizer service: {exc}",
        )


@router.post(
    "/test-adhoc",
    response_model=LLMConnectionTestResponse,
    dependencies=[Depends(require_tenant_admin)],
)
async def test_llm_config_adhoc(
    project_id: UUID,
    body: LLMConnectionTestRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> LLMConnectionTestResponse:
    """Test an LLM provider configuration without saving it first."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_project(db, project_id)
        break

    import httpx
    try:
        # The optimizer-side endpoint is authenticated (F-011-02); forward the
        # caller's bearer the same way the saved-config test route does.
        headers = {}
        if current_user.raw_token:
            headers["Authorization"] = f"Bearer {current_user.raw_token}"
        async with httpx.AsyncClient(
            timeout=float(system_snapshot_get("control.llm_config_timeout"))
        ) as client:
            resp = await client.post(
                f"{settings.OPTIMIZER_URL}/api/v1/optimize/llm/test-adhoc",
                json=body.model_dump(),
                headers=headers,
            )
        if resp.status_code in (401, 403):
            # Review F-6: see the saved-config test route above.
            raise HTTPException(
                status_code=403,
                detail="LLM connection tests require tenant administrator access",
            )
        if not resp.is_success:
            raise HTTPException(
                status_code=502,
                detail=f"Optimizer returned {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        return LLMConnectionTestResponse(
            success=data.get("ok", False),
            message=data.get("error") or "Connection successful",
            latency_ms=data.get("latency_ms"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach optimizer service: {exc}",
        )
