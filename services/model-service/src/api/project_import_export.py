"""Project-level export + import endpoints.

Routes:
  POST /projects/{project_id}/export   -- export full project as JSON bundle
  POST /projects/import                -- import project from JSON bundle
"""
from __future__ import annotations

import asyncio
import binascii
from typing import Any, Optional
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from shared.audit.logger import audit_required
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
from shared.importers.import_warnings import (
    ImportWarningResponse,
    normalize_import_warnings,
)
from shared.model_snapshot.project_serialiser import export_project
from shared.model_snapshot.consistent_read import consistent_read_session
from shared.physical_cleanup import attempt_scheduled_physical_cleanup
from src.api.personas import seed_technical_persona
from src.cold_start_trigger import trigger_predictive_cold_start
from src.auth.middleware import CurrentUser, forbid_embed_user, require_tenant_admin
from src.auth.rbac import require_role
from src.licensing_guard import enforce_demo_source_locked, enforce_import_model_cap

router = APIRouter(tags=["project-import-export"])

# Bug-8029: strong references to in-flight best-effort cold-start trigger tasks
# so the event loop does not GC them mid-flight (F-013-13 pattern).
_cold_start_tasks: set[asyncio.Task] = set()

ALL_SECTIONS = {
    "connections", "llm_configs", "agent_config",
    "cross_model_recipes", "project_settings", "access_bindings",
}
DEFAULT_SECTIONS = ALL_SECTIONS - {"access_bindings"}


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class ProjectExportRequest(BaseModel):
    include_credentials: bool = False
    passphrase: Optional[str] = Field(default=None, min_length=8)
    sections: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_SECTIONS)
    )

    @field_validator("sections")
    @classmethod
    def _check_sections(cls, v: list[str]) -> list[str]:
        # Bug-7294: the endpoint used to intersect ``sections`` with
        # ALL_SECTIONS, so a caller who asked for a section that does not exist
        # (a typo like "connectons", or a section the caller believes exists but
        # does not) got a silently smaller export with no signal that their
        # request was partly ignored. Reject unknown names up front with a clear
        # 422 so the caller learns exactly which values are invalid instead of
        # silently receiving an incomplete bundle.
        unknown = [s for s in v if s not in ALL_SECTIONS]
        if unknown:
            raise ValueError(
                "Unknown export section(s): "
                + ", ".join(sorted(set(unknown)))
                + ". Valid sections: "
                + ", ".join(sorted(ALL_SECTIONS))
            )
        return v


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
    warnings: list[ImportWarningResponse] = Field(default_factory=list)
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

    # Bug-7294: section names are now validated on the request schema, so every
    # value here is already a known section — no silent intersection drop.
    sections = set(body.sections)

    result: dict[str, Any] | None = None
    # Bug-8380: every project section and model snapshot must observe the same
    # committed point in time; separate per-model transactions still skew.
    async with consistent_read_session(current_user.tenant_id) as tenant_db:
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

        # F-022-01/F-022-02: a full project bundle export (optionally including
        # encrypted credentials) is a high-sensitivity egress event. Emit a
        # reconstructive, fail-closed audit record so the export cannot succeed
        # without a durable trail of who exported what — and whether credentials
        # were included.
        await audit_required(
            tenant_db,
            action="project.export",
            severity="warn" if not body.include_credentials else "critical",
            actor_email=current_user.email,
            target_type="project", target_id=project_id,
            detail={
                "sections": sorted(sections),
                "include_credentials": body.include_credentials,
            },
        )
        await tenant_db.commit()

    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _import_credentials_included(passphrase: Any, bundle: dict[str, Any]) -> bool:
    """F-020-06: did this import actually ingest credentials?

    True only when BOTH a passphrase is supplied AND the bundle declares
    ``credentials_included``. The IMPORT request has no ``include_credentials``
    field (that is an EXPORT-request field), so the previous
    ``getattr(body, "include_credentials", False)`` was always False — the most
    sensitive ingress event the product has was audited severity=warn with
    ``credentials_included=false``. The bundle is the authority on whether it
    carries credentials.
    """
    return bool(passphrase and (bundle or {}).get("credentials_included", False))


@router.post(
    "/projects/import",
    response_model=ProjectImportResponse,
)
async def import_project_endpoint(
    body: ProjectImportRequest = Body(...),
    # F1 (F-021-04): project import creates/replaces a project and writes the
    # importer's admin binding, so it must mirror create_project's HUMAN-only
    # gate. The former coarse token-role check admitted any principal whose token
    # role string was "tenant_admin"/"system_admin" — including a service
    # principal (aggregate-rebuild-service, model-service-deploy, ...) minted with
    # role="tenant_admin" — which then reached import_project and had Step 10b
    # persist a junk admin binding for its non-human "service:<principal>"
    # identity. require_tenant_admin rejects service AND embed principals (via
    # is_human_tenant_admin_or_system_admin), exactly as project CREATE does.
    # forbid_embed_user is retained as belt-and-suspenders, matching create_project.
    current_user: CurrentUser = Depends(require_tenant_admin),
    _: None = Depends(forbid_embed_user),
) -> ProjectImportResponse:

    if not body.dry_run:
        enforce_demo_source_locked(current_user.tenant_id)

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

    # Bug-7468: enforce the licensed model cap BEFORE creating any models.
    # In create mode, all bundle models are additive. In replace mode, the
    # existing project's models are deleted first, so the net change may be
    # lower (or even negative). Compute the effective delta and enforce.
    bundle_model_count = len(bundle.get("models", []) or [])

    result: dict[str, Any] | None = None
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        if not body.dry_run and bundle_model_count > 0:
            from shared.db.models import Model, Project
            from sqlalchemy import func as sa_func, select

            if body.mode == "replace":
                # In replace mode, find the existing project to subtract its
                # models from the count (they will be deleted).
                slug_override = body.project_slug
                existing_slug = (
                    slug_override
                    or bundle.get("project", {}).get("slug")
                )
                existing_project_model_count = 0
                if existing_slug:
                    existing_proj = (
                        await tenant_db.execute(
                            select(Project).where(
                                Project.slug == existing_slug
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_proj:
                        r = await tenant_db.execute(
                            select(sa_func.count())
                            .select_from(Model)
                            .where(Model.project_id == existing_proj.id)
                        )
                        existing_project_model_count = int(r.scalar() or 0)

                async def _count_models_replace() -> int:
                    r = await tenant_db.execute(
                        select(sa_func.count()).select_from(Model)
                    )
                    total = int(r.scalar() or 0)
                    return total - existing_project_model_count

                # Bug-6567: pass db so imports and direct creates serialise
                # via the same advisory lock, preventing concurrent cap bypass.
                await enforce_import_model_cap(
                    bundle_model_count, _count_models_replace, db=tenant_db,
                )
            else:
                # create mode: all bundle models are additive
                async def _count_models_create() -> int:
                    r = await tenant_db.execute(
                        select(sa_func.count()).select_from(Model)
                    )
                    return int(r.scalar() or 0)

                # Bug-6567: pass db so imports and direct creates serialise
                # via the same advisory lock, preventing concurrent cap bypass.
                await enforce_import_model_cap(
                    bundle_model_count, _count_models_create, db=tenant_db,
                )

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
                    persona_slug_overrides=body.persona_slugs,
                    connection_mapping=body.connection_mapping,
                )
                # Keep the endpoint contract stable even when a legacy planner
                # implementation or an in-process caller returns raw strings.
                plan = dict(plan)
                plan["warnings"] = normalize_import_warnings(
                    plan.get("warnings", []), source="project_import"
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
                # Bug-6138: project-bundle import creates models via shared
                # import_project, which does not seed the canonical Technical
                # persona. Seed it (idempotent) for each imported/created model —
                # in the same transaction, before commit — so bug-window or
                # hand-authored bundles still get the hidden-columns technical
                # catalog. id_map["models"] maps source id -> new live id.
                for new_model_id in (
                    (result.get("id_map") or {}).get("models") or {}
                ).values():
                    await seed_technical_persona(tenant_db, UUID(str(new_model_id)))

                # B3-F1 / F-022-01/F-022-02: a project import is the most
                # sensitive ingress mutation — it creates connections (with
                # decrypted credentials), personas, models, and can replace an
                # entire project. Fail closed so the import cannot commit
                # without a durable audit record of who imported what.
                _creds_included = _import_credentials_included(
                    body.passphrase, bundle
                )
                await audit_required(
                    tenant_db,
                    action="project.import",
                    severity="critical" if _creds_included else "warn",
                    actor_email=current_user.email,
                    target_type="project",
                    detail={
                        "mode": body.mode,
                        "project_slug": body.project_slug,
                        "credentials_included": _creds_included,
                        "models_imported": result.get("models_imported", 0),
                    },
                )
                await tenant_db.commit()
                await attempt_scheduled_physical_cleanup(tenant_db)

                # Bug-8029: kick the optimizer's durable predictive cold-start
                # pipeline for each imported model so a freshly-imported (and
                # deployed) model generates predictive candidates now instead of
                # waiting for the next scheduled predictive sweep tick. Fire-and-
                # forget / best-effort / idempotent per deployed version — the
                # optimizer endpoint no-ops for a model that did not land
                # deployed, so an unconditional call per imported model is safe.
                for _new_model_id in (
                    (result.get("id_map") or {}).get("models") or {}
                ).values():
                    _cs_task = asyncio.create_task(
                        trigger_predictive_cold_start(
                            current_user.tenant_id, str(_new_model_id)
                        )
                    )
                    _cold_start_tasks.add(_cs_task)
                    _cs_task.add_done_callback(_cold_start_tasks.discard)
        except ProjectImportError as exc:
            await tenant_db.rollback()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )
        except ValueError as exc:
            # Bug-6291: BI-safe slug validation in
            # insert_model_with_slug_retry / validate_bi_safe_slug
            # raises ValueError for invalid model or persona slugs.
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
