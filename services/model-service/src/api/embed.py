"""Embed token API: mint and revoke scoped JWTs for ISV embedding."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.audit.logger import audit
from shared.auth.embed_revocation import revoke_embed_token
from shared.db.models import SystemTenant
from shared.db.session import get_system_db, get_tenant_db
from shared.schemas.pydantic_models import EmbedTokenRequest, EmbedTokenResponse, EmbedTokenScope
from src.auth.local_backend import create_embed_token
from src.auth.middleware import CurrentUser, require_tenant_admin

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

    token, expires_at = create_embed_token(
        user_identity=body.user_identity,
        tenant_id=body.tenant_id,
        persona_id=body.persona_id,
        project_ids=body.project_ids,
        model_ids=body.model_ids,
        capabilities=body.capabilities,
        expiry_minutes=body.expiry_minutes,
    )

    scope = EmbedTokenScope(
        tenant_id=body.tenant_id,
        user_identity=body.user_identity,
        persona_id=body.persona_id,
        project_ids=body.project_ids,
        model_ids=body.model_ids,
        capabilities=body.capabilities if body.capabilities is not None else ["query", "chat", "explore"],
        expiry_minutes=body.expiry_minutes,
    )

    try:
        async for db in get_tenant_db(body.tenant_id):
            await audit(
                db,
                action="embed_token.created",
                severity="warn",
                actor_email=admin.email,
                detail=scope.model_dump(),
            )
            await db.commit()
    except Exception:
        logger.warning("Audit logging failed for embed token (tenant=%s)", body.tenant_id, exc_info=True)

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
    admin: CurrentUser = Depends(require_tenant_admin),
) -> None:
    """Revoke an embed token by its jti claim.

    The token becomes unusable immediately (within the 60-second cache
    refresh window). The revocation record is pruned automatically
    after the token's maximum possible expiry (24 hours).
    """
    max_expiry = datetime.now(timezone.utc) + timedelta(hours=24)
    await revoke_embed_token(
        jti=jti,
        tenant_id=admin.tenant_id,
        revoked_by=admin.email,
        expires_at=max_expiry,
    )

    try:
        async for db in get_tenant_db(admin.tenant_id):
            await audit(
                db,
                action="embed_token.revoked",
                severity="warn",
                actor_email=admin.email,
                detail={"jti": str(jti)},
            )
            await db.commit()
    except Exception:
        logger.warning(
            "Audit logging failed for embed token revocation (jti=%s)", jti,
            exc_info=True,
        )
