"""Project-level export + import endpoints.

Routes:
  POST /projects/{project_id}/export   -- export full project as JSON bundle
  POST /projects/import                -- import project from JSON bundle
"""
from __future__ import annotations

import binascii
from typing import Any, Optional
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from shared.db.session import get_tenant_db
from shared.model_snapshot.credential_envelope import (
    EnvelopeError,
    build_envelope,
    fernet_from_envelope,
)
from shared.security.credential_crypto import get_credential_fernet
from shared.model_snapshot.project_rehydrator import (
    ProjectImportError,
    import_project,
    plan_project_import,
)
from shared.model_snapshot.project_serialiser import export_project
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(tags=["project-import-export"])

ALL_SECTIONS = {
    "connections", "llm_configs", "agent_config",
    "cross_model_recipes", "project_settings", "access_bindings",
}
DEFAULT_SECTIONS = ALL_SECTIONS - {"access_bindings"}


def _require_tenant_admin(
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    # Import has no project_id path param so require_role() cannot be used here.
    # Tenant-level admin or system admin is required to create/replace projects.
    if current_user.role not in ("tenant_admin", "system_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project import requires tenant admin privileges",
        )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class ProjectExportRequest(BaseModel):
    include_credentials: bool = False
    passphrase: Optional[str] = Field(default=None, min_length=8)
    sections: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_SECTIONS)
    )


class ProjectImportRequest(BaseModel):
    bundle: dict[str, Any]
    passphrase: Optional[str] = None
    mode: str = Field(default="create", pattern="^(create|replace)$")
    dry_run: bool = False
    project_slug: Optional[str] = Field(default=None, max_length=64)
    project_display_name: Optional[str] = Field(default=None, max_length=255)
    model_slugs: Optional[dict[str, str]] = None
    persona_slugs: Optional[dict[str, str]] = None
    connection_mapping: Optional[dict[str, str]] = None
    override_connections: bool = False


class ProjectImportResponse(BaseModel):
    # F-020-E3: a dry-run produces no project, so project_id is optional and
    # the deletion/creation plan is returned instead of an applied result.
    project_id: Optional[str] = None
    project_slug: str
    id_map: dict[str, dict[str, str]]
    models_imported: int
    models_requiring_deploy: list[str]
    post_import_actions: list[str]
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = False
    plan: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.post(
    "/projects/{project_id}/export",
    dependencies=[require_role("admin")],
)
async def export_project_endpoint(
    project_id: UUID,
    body: ProjectExportRequest = Body(...),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict[str, Any]:
    if body.include_credentials and not body.passphrase:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="passphrase is required when include_credentials is true",
        )

    # Rotation-aware: MultiFernet encrypts under the current key and decrypts
    # under the current or any previous key (F-014-03). Drop-in for a raw
    # Fernet — the serialiser/rehydrator call .encrypt/.decrypt on it.
    system_fernet = get_credential_fernet()
    passphrase_fernet = None
    envelope = None

    if body.include_credentials and body.passphrase:
        passphrase_fernet, envelope = build_envelope(body.passphrase)

    sections = set(body.sections) & ALL_SECTIONS

    result: dict[str, Any] | None = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        try:
            result = await export_project(
                project_id,
                tenant_db,
                tenant_slug=current_user.tenant_id,
                sections=sections,
                include_credentials=body.include_credentials,
                system_fernet=system_fernet,
                passphrase_fernet=passphrase_fernet,
            )
        except ValueError as exc:
            # F-020-15: a missing project must be a 404, not an uncaught 500.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            )
        if envelope:
            result["credentials_envelope"] = envelope

    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@router.post(
    "/projects/import",
    response_model=ProjectImportResponse,
    dependencies=[Depends(_require_tenant_admin)],
)
async def import_project_endpoint(
    body: ProjectImportRequest = Body(...),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ProjectImportResponse:

    bundle = body.bundle
    has_creds = bundle.get("credentials_included", False)

    # F-020-E3: a dry-run never decrypts credentials or mutates the tenant DB,
    # so it does not require a passphrase even when the bundle carries creds.
    if has_creds and not body.passphrase and not body.dry_run:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="passphrase is required when the bundle includes "
            "credentials",
        )

    # Rotation-aware: see export path above (F-014-03).
    system_fernet = None
    passphrase_fernet = None
    if not body.dry_run:
        system_fernet = get_credential_fernet()

    if has_creds and body.passphrase and not body.dry_run:
        envelope = bundle.get("credentials_envelope", {})
        if not envelope:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Bundle has credentials_included=true but no "
                "credentials_envelope",
            )
        try:
            passphrase_fernet = fernet_from_envelope(
                body.passphrase, envelope
            )
        except EnvelopeError as exc:
            # F-020-15: a malformed/unsupported envelope is a distinct,
            # actionable error — not a wrong-passphrase guess.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid credentials envelope: {exc}",
            )
        except (InvalidToken, KeyError, ValueError, binascii.Error) as exc:
            # Wrong passphrase (InvalidToken on first decrypt), missing salt
            # key, or malformed base64 — surface as a credential problem
            # without masking unrelated bugs as a passphrase failure.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid passphrase or corrupted credentials: {exc}",
            )

    result: dict[str, Any] | None = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        try:
            if body.dry_run:
                # Non-mutating: compute the deletion/creation plan and roll
                # back so nothing touches the tenant DB (F-020-E3).
                plan = await plan_project_import(
                    bundle,
                    tenant_db,
                    mode=body.mode,
                    project_slug_override=body.project_slug,
                    project_display_name_override=body.project_display_name,
                    model_slug_overrides=body.model_slugs,
                    connection_mapping=body.connection_mapping,
                )
                await tenant_db.rollback()
                result = {
                    "project_id": plan["target_project_id"],
                    "project_slug": plan["target_project_slug"],
                    "id_map": {
                        "models": {},
                        "connections": {},
                        "personas": {},
                        "llm_configs": {},
                        "judge_rubrics": {},
                    },
                    "models_imported": 0,
                    "models_requiring_deploy": [],
                    "post_import_actions": plan["post_import_actions"],
                    "warnings": plan["warnings"],
                    "dry_run": True,
                    "plan": plan,
                }
            else:
                result = await import_project(
                    bundle,
                    tenant_db,
                    mode=body.mode,
                    project_slug_override=body.project_slug,
                    project_display_name_override=body.project_display_name,
                    model_slug_overrides=body.model_slugs,
                    persona_slug_overrides=body.persona_slugs,
                    connection_mapping=body.connection_mapping,
                    override_connections=body.override_connections,
                    system_fernet=system_fernet,
                    passphrase_fernet=passphrase_fernet,
                    actor=current_user.email or current_user.user_id,
                )
                await tenant_db.commit()
        except ProjectImportError as exc:
            await tenant_db.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )
        except InvalidToken:
            await tenant_db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid passphrase or corrupted credentials",
            )
        except Exception:
            await tenant_db.rollback()
            raise

    assert result is not None
    return ProjectImportResponse(**result)
