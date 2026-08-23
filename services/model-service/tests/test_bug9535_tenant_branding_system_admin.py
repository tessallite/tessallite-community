"""Bug-9535 — system admins manage branding in the path-target tenant DB."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api import tenant_branding
from src.api.tenant_branding import BrandingConfig
from src.auth.middleware import CurrentUser

pytestmark = pytest.mark.unit


def _system_admin() -> CurrentUser:
    return CurrentUser("root", "__system__", "root@example.test", "system_admin")


def _tenant_admin() -> CurrentUser:
    return CurrentUser("admin", "tenant-a", "admin@example.test", "tenant_admin")


def test_bug9535_resolves_only_real_authorized_branding_targets() -> None:
    assert (
        tenant_branding._resolve_branding_target_tenant("tenant-b", _system_admin())
        == "tenant-b"
    )

    with pytest.raises(HTTPException) as cross_tenant:
        tenant_branding._resolve_branding_target_tenant("tenant-b", _tenant_admin())
    assert cross_tenant.value.status_code == 403

    with pytest.raises(HTTPException) as system_target:
        tenant_branding._resolve_branding_target_tenant("__system__", _system_admin())
    assert system_target.value.status_code == 400


@pytest.mark.asyncio
async def test_bug9535_get_and_put_open_the_system_admin_path_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_tenants: list[str] = []
    db = AsyncMock()

    async def _target_db(tenant_id: str):
        opened_tenants.append(tenant_id)
        yield db

    get_setting = AsyncMock(return_value=None)
    set_setting = AsyncMock()
    monkeypatch.setattr(tenant_branding, "get_tenant_db", _target_db)
    monkeypatch.setattr(tenant_branding, "get_setting", get_setting)
    monkeypatch.setattr(tenant_branding, "set_setting", set_setting)

    read = await tenant_branding.get_branding("tenant-b", _system_admin())
    written = await tenant_branding.update_branding(
        "tenant-b",
        BrandingConfig(primary_color="#0B5FFF"),
        _system_admin(),
    )

    assert opened_tenants == ["tenant-b", "tenant-b"]
    assert read == BrandingConfig()
    assert written == BrandingConfig()
    set_setting.assert_awaited_once_with(
        "branding.primary_color",
        "#0B5FFF",
        actor="root@example.test",
        tenant_session=db,
        tenant_scope=True,
    )
    db.commit.assert_awaited_once()
