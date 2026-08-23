"""Per-request tenant SSO overlay (G-021-02).

Tenant-admin IdP settings live in ``tenant_settings`` key ``sso.config``.
Login/callback handlers bind that document onto a ContextVar for the request
so OIDC/SAML backends read tenant issuer/metadata instead of only process env.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator

SSO_CONFIG_KEY = "sso.config"

_overlay: ContextVar[dict[str, Any]] = ContextVar("sso_overlay", default={})


def current_overlay() -> dict[str, Any]:
    return _overlay.get() or {}


async def load_tenant_overlay(tenant_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from shared.db.models import TenantSetting
    from shared.db.session import get_tenant_db

    async for db in get_tenant_db(tenant_id):
        row = (
            await db.execute(
                select(TenantSetting).where(TenantSetting.key == SSO_CONFIG_KEY)
            )
        ).scalar_one_or_none()
        return dict(row.value_json) if row is not None else {}
    return {}


@asynccontextmanager
async def tenant_sso_overlay(tenant_id: str) -> AsyncIterator[dict[str, Any]]:
    ov = await load_tenant_overlay(tenant_id)
    token = _overlay.set(ov or {})
    try:
        yield ov
    finally:
        _overlay.reset(token)
