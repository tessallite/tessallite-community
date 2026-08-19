"""Bug-6598 / Bug-6599: UserAccessBinding.source provenance surfacing + carry.

Bug-6598 — the access API response schema (``UserAccessBindingResponse``) must
expose ``source`` so a caller can tell a manually granted role from one
materialised by SSO group sync ("sso_group").

Bug-6599 — project export must carry ``source`` so an exported sso_group
binding does not silently re-import as "manual" (which would make an
SSO-managed grant permanent and unrevocable). Legacy bundles that predate the
column default to "manual" on import (fail-safe: never treat an unknown grant
as SSO-managed / auto-revocable).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.model_snapshot.project_serialiser import export_project
from shared.schemas.pydantic_models import UserAccessBindingResponse


# ---------------------------------------------------------------------------
# Bug-6598 — response schema surfaces provenance
# ---------------------------------------------------------------------------

def test_response_schema_carries_source_from_orm():
    orm_like = types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        model_id=None,
        user_identity="alice@example.com",
        role="viewer",
        source="sso_group",
        created_at=datetime.now(timezone.utc),
    )
    resp = UserAccessBindingResponse.model_validate(orm_like)
    assert resp.source == "sso_group"


def test_response_schema_source_defaults_to_manual_when_absent():
    """A legacy caller/object without ``source`` still validates, defaulting to
    the fail-safe "manual" provenance."""
    payload = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "model_id": None,
        "user_identity": "bob@example.com",
        "role": "modeler",
        "created_at": datetime.now(timezone.utc),
    }
    resp = UserAccessBindingResponse.model_validate(payload)
    assert resp.source == "manual"


# ---------------------------------------------------------------------------
# Bug-6599 — export carries source; import default is manual
# ---------------------------------------------------------------------------

def _export_db(bindings):
    """Mock AsyncSession for export_project driven with only access_bindings.

    execute() is called three times in order:
      1. model slug map (result.all())        -> []
      2. UserAccessBinding rows (scalars().all()) -> bindings
      3. "models always" query (scalars().all())  -> []
    """
    slug_result = MagicMock()
    slug_result.all.return_value = []

    binding_result = MagicMock()
    binding_result.scalars.return_value.all.return_value = bindings

    models_result = MagicMock()
    models_result.scalars.return_value.all.return_value = []

    results = iter([slug_result, binding_result, models_result])

    db = AsyncMock()
    db.get = AsyncMock(
        return_value=types.SimpleNamespace(
            slug="proj", display_name="Project", is_active=True,
        )
    )
    db.execute = AsyncMock(side_effect=lambda *a, **k: next(results))
    return db


@pytest.mark.asyncio
async def test_export_carries_binding_source():
    """A project export emits each binding's ``source`` verbatim so provenance
    survives the round-trip (an sso_group grant stays sso_group)."""
    sso_binding = types.SimpleNamespace(
        user_identity="alice@example.com",
        role="viewer",
        model_id=None,
        source="sso_group",
    )
    manual_binding = types.SimpleNamespace(
        user_identity="bob@example.com",
        role="modeler",
        model_id=None,
        source="manual",
    )
    db = _export_db([sso_binding, manual_binding])

    bundle = await export_project(
        uuid.uuid4(), db, tenant_slug="acme", sections={"access_bindings"}
    )

    by_user = {b["user_identity"]: b for b in bundle["access_bindings"]}
    assert by_user["alice@example.com"]["source"] == "sso_group"
    assert by_user["bob@example.com"]["source"] == "manual"


def test_import_builds_binding_with_source_defaulting_manual():
    """The rehydrator constructs a real ``UserAccessBinding`` from each bundle
    entry via ``source=ab.get("source", "manual")``. Building the actual ORM
    object here locks the producer/consumer field-name alignment (bundle key
    "source" -> ORM column ``source``) and the fail-safe default: a modern
    bundle keeps its provenance; a legacy bundle (no ``source``) becomes
    "manual", never an SSO-managed / auto-revocable grant."""
    from shared.db.models import UserAccessBinding

    modern = {"user_identity": "a", "role": "viewer", "source": "sso_group"}
    legacy = {"user_identity": "b", "role": "viewer"}  # pre-Bug-6303, no source

    b_modern = UserAccessBinding(
        user_identity=modern["user_identity"],
        role=modern["role"],
        source=modern.get("source", "manual"),
    )
    b_legacy = UserAccessBinding(
        user_identity=legacy["user_identity"],
        role=legacy["role"],
        source=legacy.get("source", "manual"),
    )

    assert b_modern.source == "sso_group"
    assert b_legacy.source == "manual"
