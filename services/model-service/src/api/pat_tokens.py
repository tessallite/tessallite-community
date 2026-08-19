"""Personal Access Token (PAT) management API (Bug-7314).

Any authenticated non-embed user can mint, list, and revoke their OWN Personal
Access Tokens. A PAT lets SSO users (who have no password) authenticate BI
clients: the plaintext is shown ONCE at creation and used as the password in
Excel (XMLA Basic) / Power BI (PostgreSQL :5433).

Ownership is enforced end to end: every query is scoped to the caller's own
``local_users`` row in the caller's own tenant. A user can never see or revoke
another user's token, and the plaintext is never retrievable after creation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from shared.auth.identity import canonical_email
from shared.db.models import LocalUser, PersonalAccessToken
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    PersonalAccessTokenCreate,
    PersonalAccessTokenCreateResponse,
    PersonalAccessTokenResponse,
)
from src.auth.middleware import CurrentUser, require_human_user
from src.auth.pat import generate_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/tokens", tags=["auth", "personal-access-tokens"])


async def _resolve_local_user(db, current_user: CurrentUser) -> LocalUser:
    """Resolve the caller's ``local_users`` row in their tenant, or 404."""
    result = await db.execute(
        select(LocalUser).where(
            func.lower(LocalUser.email) == canonical_email(current_user.email)
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post(
    "",
    response_model=PersonalAccessTokenCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_token(
    body: PersonalAccessTokenCreate,
    current_user: CurrentUser = Depends(require_human_user),
) -> PersonalAccessTokenCreateResponse:
    """Mint a new PAT for the authenticated user. Returns the plaintext ONCE."""
    plaintext, token_prefix, token_hash = generate_token()
    expires_at: datetime | None = None
    if body.expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    async for db in get_tenant_db(current_user.tenant_id):
        user = await _resolve_local_user(db, current_user)
        pat = PersonalAccessToken(
            user_id=user.id,
            token_hash=token_hash,
            token_prefix=token_prefix,
            label=body.label or "",
            expires_at=expires_at,
        )
        db.add(pat)
        await db.commit()
        await db.refresh(pat)
        # Never log the plaintext or the hash. The prefix is non-secret.
        logger.info(
            "PAT minted (prefix=%s) for user %s in tenant %s",
            token_prefix, user.email, current_user.tenant_id,
        )
        return PersonalAccessTokenCreateResponse(
            token=plaintext,
            pat=PersonalAccessTokenResponse.model_validate(pat),
        )


@router.get("", response_model=list[PersonalAccessTokenResponse])
async def list_tokens(
    current_user: CurrentUser = Depends(require_human_user),
) -> list[PersonalAccessTokenResponse]:
    """List the authenticated user's PATs (metadata only; no plaintext/hash)."""
    async for db in get_tenant_db(current_user.tenant_id):
        user = await _resolve_local_user(db, current_user)
        result = await db.execute(
            select(PersonalAccessToken)
            .where(PersonalAccessToken.user_id == user.id)
            .order_by(PersonalAccessToken.created_at.desc())
        )
        rows = result.scalars().all()
        return [PersonalAccessTokenResponse.model_validate(r) for r in rows]


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: str,
    current_user: CurrentUser = Depends(require_human_user),
) -> None:
    """Revoke one of the authenticated user's PATs.

    Ownership is enforced by filtering on the caller's ``user_id`` — a token
    that exists but belongs to another user is reported as 404, never revoked.
    Revocation is a soft delete (sets ``revoked_at``): the row is retained for
    audit, and validation rejects any revoked token on every subsequent use.
    """
    try:
        token_uuid = uuid.UUID(token_id)
    except (ValueError, AttributeError, TypeError):
        # A malformed id can never match a real row; report as not-found rather
        # than letting the DB driver raise a 500 on an invalid UUID literal.
        raise HTTPException(status_code=404, detail="Token not found")

    async for db in get_tenant_db(current_user.tenant_id):
        user = await _resolve_local_user(db, current_user)
        result = await db.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.id == token_uuid,
                PersonalAccessToken.user_id == user.id,
            )
        )
        pat = result.scalar_one_or_none()
        if pat is None:
            raise HTTPException(status_code=404, detail="Token not found")
        if pat.revoked_at is None:
            pat.revoked_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info(
                "PAT revoked (prefix=%s) by user %s in tenant %s",
                pat.token_prefix, user.email, current_user.tenant_id,
            )
