"""Canonical auth identity helpers."""
from __future__ import annotations

from sqlalchemy import func


def canonical_email(value: str) -> str:
    return (value or "").strip().lower()


def canonical_user_identity(value: str) -> str:
    identity = (value or "").strip()
    if "@" in identity and not identity.startswith("service:"):
        return identity.lower()
    return identity


def is_service_identity(value: str) -> bool:
    """True when *value* denotes an internal service principal.

    A service principal's canonical identity is always ``service:<principal>``
    (see CurrentServiceUser / create_service_access_token); it is never a human
    email. Embed identities are NOT distinguishable here — an embed subject is an
    arbitrary caller-supplied ``user_identity`` with no reserved prefix — so embed
    principals are rejected at the route gate (require_tenant_admin /
    forbid_embed_user), not by this string check.
    """
    return canonical_user_identity(value).startswith("service:")


def user_identity_matches(column, identity: str):
    canonical = canonical_user_identity(identity)
    if "@" in canonical and not canonical.startswith("service:"):
        return func.lower(column) == canonical
    return column == canonical
