"""Bound the IdP attribute set embedded in the signed JWT (Bug-1072).

The SAML ACS and OIDC callback previously copied the IdP's full raw
attribute/claim set into the JWT ``claims`` field with no size guard.
A group-heavy or attribute-rich IdP can push the signed token past
cookie and header limits, breaking login for that user. Query-time row
security only ever reads the claim names referenced by enabled
``saml_claim`` / ``oidc_scope`` rules, so everything else is dead
weight in the token.

``bound_token_claims`` keeps exactly the claim names referenced by
enabled rules in the tenant plus the ``AUTH_JWT_CLAIMS_ALLOWLIST``
setting, logs every dropped attribute name, and raises
``ClaimsTooLargeError`` if the kept set still exceeds
``AUTH_JWT_CLAIMS_MAX_BYTES``. Failing the login is deliberate: an
oversized token would fail opaquely at the cookie/header layer anyway,
and silently dropping a rule-referenced claim could *widen* row access
(a restricting rule that no longer matches its principal does not
fire). Rules added after login require a re-login to take effect — the
existing claims-snapshot semantic.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.db.models import RowSecurityRule

logger = logging.getLogger(__name__)


class ClaimsTooLargeError(Exception):
    """The bounded claim set still exceeds ``AUTH_JWT_CLAIMS_MAX_BYTES``."""

    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(
            f"SSO attribute set serializes to {size} bytes after filtering; "
            f"the token claims limit is {limit} bytes "
            f"(AUTH_JWT_CLAIMS_MAX_BYTES). Narrow the IdP attribute release "
            f"or raise the limit."
        )


async def referenced_claim_names(db: AsyncSession) -> set[str]:
    """Claim names that enabled row-security rules actually read.

    Scans the whole tenant (not one model) because the issued token
    serves every model the user can query.
    """
    result = await db.execute(
        select(RowSecurityRule.attribute_claim_name)
        .where(
            RowSecurityRule.is_enabled.is_(True),
            RowSecurityRule.attribute_source.in_(("saml_claim", "oidc_scope")),
            RowSecurityRule.attribute_claim_name.is_not(None),
        )
        .distinct()
    )
    return {name for name in result.scalars().all() if name}


def bound_token_claims(
    raw_claims: dict,
    referenced: set[str],
    *,
    backend: str,
    subject: str,
) -> dict:
    """Filter ``raw_claims`` to RLS-referenced + allow-listed names and
    enforce the serialized-size cap. Raises ``ClaimsTooLargeError`` when
    the kept set is still over the cap."""
    settings = get_settings()
    allowlist = {
        name.strip()
        for name in (settings.AUTH_JWT_CLAIMS_ALLOWLIST or "").split(",")
        if name.strip()
    }
    keep_names = referenced | allowlist
    raw_claims = raw_claims or {}
    kept = {k: v for k, v in raw_claims.items() if k in keep_names}
    dropped = sorted(set(raw_claims) - set(kept))
    if dropped:
        logger.info(
            "%s login for %s: dropped %d IdP attribute(s) not referenced by "
            "row-security rules or AUTH_JWT_CLAIMS_ALLOWLIST from the JWT "
            "claims: %s",
            backend, subject, len(dropped), ", ".join(dropped),
        )

    size = len(json.dumps(kept, default=str, sort_keys=True).encode("utf-8"))
    limit = int(settings.AUTH_JWT_CLAIMS_MAX_BYTES)
    if size > limit:
        logger.error(
            "%s login for %s: JWT claims payload is %d bytes, exceeding "
            "AUTH_JWT_CLAIMS_MAX_BYTES=%d even after filtering to %d "
            "claim(s); refusing to issue an oversized token",
            backend, subject, size, limit, len(kept),
        )
        raise ClaimsTooLargeError(size, limit)
    return kept
