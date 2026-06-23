"""Shared helpers for governance integration API routers and sync orchestrators.

Extracted from duplicated copies in solidatus.py, collibra.py,
solidatus_sync.py, and collibra_sync.py (SCI-003, SCI-008).
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.db.models import Model

_settings = get_settings()


# ---------------------------------------------------------------------------
# Fernet helpers
# ---------------------------------------------------------------------------


def encrypt_credentials(data: dict) -> bytes:
    f = Fernet(_settings.CREDENTIAL_ENCRYPTION_KEY.encode())
    return f.encrypt(json.dumps(data).encode())


def decrypt_credentials(data: bytes) -> dict:
    f = Fernet(_settings.CREDENTIAL_ENCRYPTION_KEY.encode())
    return json.loads(f.decrypt(data).decode())


def decrypt_token(encrypted_credentials: bytes) -> str:
    """Convenience: decrypt and return the 'token' field."""
    return decrypt_credentials(encrypted_credentials).get("token", "")


# ---------------------------------------------------------------------------
# FastAPI helpers
# ---------------------------------------------------------------------------


def not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def get_model(db: AsyncSession, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise not_found("Model not found")
    return model


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------


def payload_hash(obj) -> str:
    """Stable SHA-256 hash of a mapped payload object for diffing."""
    d: dict = {
        "external_id": obj.external_id,
        "type": getattr(obj, "type", getattr(obj, "asset_type", "")),
        "name": getattr(obj, "name", getattr(obj, "display_name", "")),
        "attributes": getattr(obj, "attributes", getattr(obj, "properties", {})),
    }
    if hasattr(obj, "source_external_id"):
        d["source"] = obj.source_external_id
        d["target"] = obj.target_external_id
        d["edge_type"] = getattr(obj, "type", getattr(obj, "relation_type", ""))
    raw = json.dumps(d, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


async def load_mappings(db: AsyncSession, connection_id: UUID, mapping_cls):
    """Load existing mappings keyed by (object_type, tessallite_object_id)."""
    result = await db.execute(
        select(mapping_cls).where(mapping_cls.connection_id == connection_id)
    )
    mappings: dict[tuple[str, str]] = {}
    for m in result.scalars().all():
        key = (m.tessallite_object_type, m.tessallite_object_id)
        mappings[key] = m
    return mappings
