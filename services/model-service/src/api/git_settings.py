"""Git integration settings.

  GET    /tenants/me/git-settings   — read remote config (token masked)
  PUT    /tenants/me/git-settings   — save remote URL + PAT (encrypted)
  POST   /tenants/me/git-settings/test — test connection via git ls-remote
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from shared.db.models import TenantSetting
from shared.db.session import get_tenant_db
from shared.security.credential_crypto import get_credential_fernet
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/tenants/me/git-settings",
    tags=["git-settings"],
)

_SETTING_KEY = "git_remote"


class GitSettingsRead(BaseModel):
    remote_url: Optional[str] = None
    has_token: bool = False


class GitSettingsWrite(BaseModel):
    remote_url: str
    token: Optional[str] = None


class GitTestResult(BaseModel):
    success: bool
    message: str


@router.get(
    "",
    response_model=GitSettingsRead,
    dependencies=[require_role("admin")],
)
async def get_git_settings(
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GitSettingsRead:
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        row = await tenant_db.get(TenantSetting, _SETTING_KEY)
        if row is None:
            return GitSettingsRead()
        val = row.value_json or {}
        return GitSettingsRead(
            remote_url=val.get("remote_url"),
            has_token=bool(val.get("encrypted_token")),
        )
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "unavailable")


@router.put(
    "",
    response_model=GitSettingsRead,
    dependencies=[require_role("admin")],
)
async def save_git_settings(
    body: GitSettingsWrite,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GitSettingsRead:
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        row = await tenant_db.get(TenantSetting, _SETTING_KEY)
        stored: dict = {}
        if row is not None:
            stored = dict(row.value_json or {})

        stored["remote_url"] = body.remote_url

        if body.token:
            fernet = get_credential_fernet()
            stored["encrypted_token"] = fernet.encrypt(
                body.token.encode()
            ).decode()
        elif body.token == "":
            stored.pop("encrypted_token", None)

        if row is None:
            tenant_db.add(TenantSetting(
                key=_SETTING_KEY,
                value_json=stored,
                updated_by=current_user.email or current_user.user_id,
            ))
        else:
            row.value_json = stored
            row.updated_by = current_user.email or current_user.user_id

        await tenant_db.commit()
        return GitSettingsRead(
            remote_url=stored.get("remote_url"),
            has_token=bool(stored.get("encrypted_token")),
        )
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "unavailable")


@router.post(
    "/test",
    response_model=GitTestResult,
    dependencies=[require_role("admin")],
)
async def test_git_connection(
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GitTestResult:
    """Run git ls-remote against the configured remote to verify access."""
    async for tenant_db in get_tenant_db(current_user.tenant_id):
        row = await tenant_db.get(TenantSetting, _SETTING_KEY)
        if row is None or not (row.value_json or {}).get("remote_url"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "No remote URL configured.",
            )
        val = row.value_json
        remote_url = val["remote_url"]
        token: str | None = None
        enc = val.get("encrypted_token")
        if enc:
            fernet = get_credential_fernet()
            token = fernet.decrypt(enc.encode()).decode()

        try:
            import os
            import re
            import subprocess

            auth_url = remote_url
            if token and "://" in remote_url:
                scheme, rest = remote_url.split("://", 1)
                auth_url = f"{scheme}://{token}@{rest}"

            clean_env: dict[str, str] = {}
            for key in ("PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP"):
                val_env = os.environ.get(key)
                if val_env is not None:
                    clean_env[key] = val_env

            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "ls-remote", "--exit-code", auth_url],
                capture_output=True,
                timeout=15,
                text=True,
                env=clean_env,
            )
            if result.returncode == 0:
                return GitTestResult(success=True, message="OK")
            safe_err = re.sub(
                r"(https?://)([^@]+)@", r"\1***@", result.stderr.strip()
            )
            return GitTestResult(success=False, message=safe_err[:300])
        except Exception as exc:
            return GitTestResult(success=False, message=str(exc)[:300])
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "unavailable")
