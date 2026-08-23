"""Embed token API: mint and revoke scoped JWTs for ISV embedding."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from shared.audit.logger import audit_required
from shared.auth.embed_revocation import revoke_embed_token
from shared.db.models import (
    EmbedTokenMint,
    Model,
    Persona,
    Project,
    ProjectPersona,
    SystemTenant,
)
from shared.db.session import get_system_db, get_tenant_db
from shared.schemas.pydantic_models import EmbedTokenRequest, EmbedTokenResponse, EmbedTokenScope
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from src.auth.local_backend import create_embed_token
from src.auth.middleware import CurrentUser, require_tenant_admin

# The platform pseudo-tenant a canonical system admin authenticates under
# (shared/auth/middleware.py). No embed token ever carries it as a claim.
SYSTEM_TENANT = "__system__"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/embed-token", response_model=EmbedTokenResponse)
async def mint_embed_token(
    body: EmbedTokenRequest,
    admin: CurrentUser = Depends(require_tenant_admin),
) -> EmbedTokenResponse:
    """Mint a scoped embed JWT for ISV embedding.

    Requires tenant_admin or system_admin. The embed token grants
    restricted access to the specified tenant, optionally locked to
    a persona, model subset, and capability set.
    """
    if admin.role != "system_admin" and body.tenant_id != admin.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create embed tokens for another tenant",
        )

    async for sys_db in get_system_db():
        tenant = (
            await sys_db.execute(
                select(SystemTenant).where(SystemTenant.slug == body.tenant_id)
            )
        ).scalar_one_or_none()
        if tenant is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Tenant '{body.tenant_id}' does not exist",
            )

    # Bug-5943: validate that scope IDs actually exist in the target tenant
    # so admin-time typos surface immediately instead of producing runtime
    # 403/400 errors for embedded users.
    async for tenant_db in get_tenant_db(body.tenant_id):
        if body.project_ids:
            for pid in body.project_ids:
                try:
                    pid_uuid = UUID(pid)
                except (ValueError, TypeError):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Invalid project ID format: '{pid}'",
                    )
                proj = await tenant_db.get(Project, pid_uuid)
                if proj is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Project '{pid}' does not exist in tenant '{body.tenant_id}'",
                    )

        if body.model_ids:
            for mid in body.model_ids:
                try:
                    mid_uuid = UUID(mid)
                except (ValueError, TypeError):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Invalid model ID format: '{mid}'",
                    )
                mdl = await tenant_db.get(Model, mid_uuid)
                if mdl is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Model '{mid}' does not exist in tenant '{body.tenant_id}'",
                    )
                # Also validate that the model belongs to an allowed project
                if body.project_ids:
                    allowed_projects = {UUID(p) for p in body.project_ids}
                    if mdl.project_id not in allowed_projects:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=(
                                f"Model '{mid}' belongs to project '{mdl.project_id}' "
                                f"which is not in the allowed project_ids list"
                            ),
                        )

        if body.persona_id:
            try:
                persona_uuid = UUID(body.persona_id)
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid persona ID format: '{body.persona_id}'",
                )
            persona = await tenant_db.get(Persona, persona_uuid)
            if persona is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Persona '{body.persona_id}' does not exist in tenant '{body.tenant_id}'",
                )
            # Bug-8253: the locked persona must fall WITHIN the token's scope.
            # A persona belongs to exactly one model (Persona.model_id), and that
            # model to exactly one project. Minting a token scoped to project P1 /
            # model M1 but locking a persona that belongs to M2/P2 would hand the
            # embed session a persona outside its declared project/model scope —
            # the agent-service later rejects the mismatch at query time, but the
            # scope contract must be validated at mint time so the typo surfaces
            # to the admin instead of producing a dead token. Validate the
            # persona's model against ``model_ids`` (when set) and the persona
            # model's project against ``project_ids`` (when set).
            persona_model = await tenant_db.get(Model, persona.model_id)
            if persona_model is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Persona '{body.persona_id}' references model "
                        f"'{persona.model_id}' which does not exist"
                    ),
                )
            if body.model_ids:
                allowed_models = {str(m).lower() for m in body.model_ids}
                if str(persona.model_id).lower() not in allowed_models:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Persona '{body.persona_id}' belongs to model "
                            f"'{persona.model_id}' which is not in the allowed "
                            f"model_ids list"
                        ),
                    )
            if body.project_ids:
                allowed_projects = {str(p).lower() for p in body.project_ids}
                if str(persona_model.project_id).lower() not in allowed_projects:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Persona '{body.persona_id}' belongs to project "
                            f"'{persona_model.project_id}' which is not in the "
                            f"allowed project_ids list"
                        ),
                    )

        if body.project_persona_id:
            try:
                project_persona_uuid = UUID(body.project_persona_id)
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "Invalid project persona ID format: "
                        f"'{body.project_persona_id}'"
                    ),
                )
            project_persona = await tenant_db.get(
                ProjectPersona, project_persona_uuid
            )
            if project_persona is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Project persona '{body.project_persona_id}' does not "
                        f"exist in tenant '{body.tenant_id}'"
                    ),
                )
            if body.project_ids:
                allowed_projects = {str(p).lower() for p in body.project_ids}
                if str(project_persona.project_id).lower() not in allowed_projects:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"Project persona '{body.project_persona_id}' belongs "
                            f"to project '{project_persona.project_id}' which is "
                            "not in the allowed project_ids list"
                        ),
                    )
    # Bug-7995 / F-024-01: carry the admin-authored row-security subject
    # (role/groups/claims) into the signed token so attribute/role RLS rules
    # fire for the embedded session. The subject only narrows returned rows;
    # a token minted without an rls subject fails closed (deny-all) at query
    # time on any role-governed model.
    rls = body.rls
    token, expires_at = create_embed_token(
        user_identity=body.user_identity,
        tenant_id=body.tenant_id,
        persona_id=body.persona_id,
        project_persona_id=body.project_persona_id,
        project_ids=body.project_ids,
        model_ids=body.model_ids,
        capabilities=body.capabilities,
        rls_role=rls.role if rls else None,
        rls_groups=rls.groups if rls else None,
        rls_claims=rls.claims if rls else None,
        expiry_minutes=body.expiry_minutes,
    )
    from src.auth.local_backend import decode_access_token as _decode_embed
    jti_raw = _decode_embed(token).get("jti")

    caps = body.capabilities if body.capabilities is not None else []
    scope = EmbedTokenScope(
        tenant_id=body.tenant_id,
        user_identity=body.user_identity,
        persona_id=body.persona_id,
        project_persona_id=body.project_persona_id,
        project_ids=body.project_ids,
        model_ids=body.model_ids,
        capabilities=caps,
        rls=rls,
        expiry_minutes=body.expiry_minutes,
    )

    async for db in get_tenant_db(body.tenant_id):
        if jti_raw:
            import uuid as _uuid
            db.add(
                EmbedTokenMint(
                    jti=_uuid.UUID(str(jti_raw)),
                    actor_email=admin.email,
                    user_identity=body.user_identity,
                    persona_id=body.persona_id,
                    project_persona_id=body.project_persona_id,
                    project_ids=body.project_ids,
                    model_ids=body.model_ids,
                    capabilities=caps,
                    expires_at=expires_at,
                )
            )
        await audit_required(
            db,
            action="embed_token.created",
            severity="warn",
            actor_email=admin.email,
            detail=scope.model_dump(),
        )
        await db.commit()

    await emit_webhook(body.tenant_id, "embed_token.created", {
        "user_identity": body.user_identity,
        "actor": admin.email,
    })

    logger.info(
        "Embed token created by %s for tenant=%s user=%s",
        admin.email, body.tenant_id, body.user_identity,
    )

    return EmbedTokenResponse(
        token=token,
        expires_at=expires_at.isoformat(),
        scope=scope,
    )


@router.delete(
    "/embed-token/{jti}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_embed_token_endpoint(
    jti: UUID,
    tenant_id: str | None = Query(
        default=None,
        max_length=64,
        description=(
            "Tenant the token belongs to. Optional for a tenant admin (defaults "
            "to their own tenant, and may not name another). REQUIRED for a "
            "system admin, whose own tenant is the platform pseudo-tenant."
        ),
    ),
    admin: CurrentUser = Depends(require_tenant_admin),
) -> None:
    """Revoke an embed token by its jti claim.

    The token becomes unusable immediately (within the 60-second cache
    refresh window).

    Bug-6306 / Bug-6352: revocation is tenant-scoped — a record is only ever
    honoured for a token bearing the SAME tenant claim
    (``shared.auth.embed_revocation.is_embed_token_revoked``). The platform
    keeps no register of issued embed tokens, so ownership of an arbitrary jti
    cannot be proven here; scoping the *effect* is what makes naming another
    tenant's jti a no-op instead of a cross-tenant denial of service.

    R3: that scoping originally used the CALLER's tenant, which quietly broke
    the one revocation that matters most. ``mint_embed_token`` deliberately
    lets a system admin mint for ANY tenant, but a canonical system admin's own
    tenant is the ``__system__`` pseudo-tenant, so their revocation was filed
    under a tenant no token ever claims: the API returned 204, the audit log
    recorded the revocation, and the token kept working for its full lifetime.
    The record must name the tenant the TOKEN belongs to, so a system admin
    supplies it explicitly and a tenant admin may only ever name their own.
    """
    if admin.tenant_id == SYSTEM_TENANT:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "tenant_id is required when revoking as a system admin — "
                    "a revocation only silences a token bearing that tenant's "
                    "claim, and the platform pseudo-tenant matches none."
                ),
            )
        target_tenant = tenant_id.strip()
    else:
        if tenant_id and tenant_id.strip() != admin.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot revoke embed tokens for another tenant",
            )
        target_tenant = admin.tenant_id

    # R4: a revocation only silences a token bearing this EXACT tenant claim, so
    # a slug that names no tenant is the same silent 204 the required-parameter
    # check was added to prevent -- and the likelier operator error, since a
    # mistyped or wrong-case slug looks identical to a correct one. Resolve the
    # target before writing anything, the way ``mint_embed_token`` already does.
    async for sys_db in get_system_db():
        known = (
            await sys_db.execute(
                select(SystemTenant.slug).where(SystemTenant.slug == target_tenant)
            )
        ).scalar_one_or_none()
        if known is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Tenant '{target_tenant}' does not exist, so this "
                    "revocation would silence nothing."
                ),
            )

    max_expiry = datetime.now(timezone.utc) + timedelta(hours=24)
    await revoke_embed_token(
        jti=jti,
        tenant_id=target_tenant,
        revoked_by=admin.email,
        expires_at=max_expiry,
    )

    async for db in get_tenant_db(target_tenant):
        mint = await db.get(EmbedTokenMint, jti)
        if mint is not None and mint.revoked_at is None:
            mint.revoked_at = datetime.now(timezone.utc)
        await audit_required(
            db,
            action="embed_token.revoked",
            severity="warn",
            actor_email=admin.email,
            detail={"jti": str(jti), "tenant_id": target_tenant},
        )
        await db.commit()

    await emit_webhook(target_tenant, "embed_token.revoked", {
        "jti": str(jti),
        "actor": admin.email,
    })


@router.get("/embed-tokens")
async def list_embed_tokens(
    admin: CurrentUser = Depends(require_tenant_admin),
) -> list[dict]:
    """Tenant inventory of minted embed tokens (F-021-08)."""
    if admin.tenant_id == SYSTEM_TENANT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="List embed tokens from a tenant-admin session",
        )
    async for db in get_tenant_db(admin.tenant_id):
        rows = (
            await db.execute(
                select(EmbedTokenMint).order_by(EmbedTokenMint.created_at.desc())
            )
        ).scalars().all()
        return [
            {
                "jti": str(r.jti),
                "actor_email": r.actor_email,
                "user_identity": r.user_identity,
                "persona_id": r.persona_id,
                "project_persona_id": r.project_persona_id,
                "capabilities": r.capabilities or [],
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    return []

