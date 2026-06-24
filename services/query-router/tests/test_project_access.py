from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

from shared.auth.middleware import CurrentEmbedUser, CurrentUser
from shared.auth.project_access import ensure_project_model_access, load_authorized_model
from shared.db.models import Model, UserAccessBinding


def _user(role: str = "member") -> CurrentUser:
    return CurrentUser(
        user_id="user@example.com",
        tenant_id="tenant-1",
        email="user@example.com",
        role=role,
    )


def _binding(project_id, model_id=None, role="viewer"):
    return types.SimpleNamespace(
        user_identity="user@example.com",
        project_id=project_id,
        model_id=model_id,
        role=role,
    )


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

    def first(self):
        return self.rows[0] if self.rows else None


class _DB:
    def __init__(self, *, model=None, bindings=None):
        self.model = model
        self.bindings = bindings or []

    async def get(self, cls, key):
        if cls is Model and self.model is not None and self.model.id == key:
            return self.model
        return None

    async def execute(self, stmt):
        text = str(stmt)
        params = stmt.compile().params
        user_identity = next(
            (v for k, v in params.items() if k.startswith("user_identity")),
            None,
        )
        project_id = next(
            (v for k, v in params.items() if k.startswith("project_id")),
            None,
        )
        model_id = next((v for k, v in params.items() if k.startswith("model_id")), None)
        matching = [
            b for b in self.bindings
            if (user_identity is None or b.user_identity == user_identity)
            and (project_id is None or b.project_id == project_id)
        ]
        if "user_access_bindings.role" not in text:
            return _Result([
                b for b in self.bindings
                if project_id is None or b.project_id == project_id
            ][:1])
        if "model_id IS NULL" in text:
            return _Result([b for b in matching if b.model_id is None])
        if "model_id =" in text:
            return _Result([
                b for b in matching
                if b.model_id is not None and (model_id is None or b.model_id == model_id)
            ])
        return _Result([])


@pytest.mark.asyncio
async def test_project_binding_allows_model_viewer_access():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB(bindings=[_binding(project_id, role="viewer")])

    await ensure_project_model_access(
        db, _user(), project_id=project_id, model_id=model_id, min_role="viewer"
    )


@pytest.mark.asyncio
async def test_missing_binding_denies_when_project_has_bindings():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB(bindings=[_binding(project_id, role="viewer")])

    with pytest.raises(HTTPException) as exc:
        await ensure_project_model_access(
            db,
            CurrentUser(
                user_id="other@example.com",
                tenant_id="tenant-1",
                email="other@example.com",
                role="member",
            ),
            project_id=project_id,
            model_id=model_id,
            min_role="viewer",
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_load_authorized_model_rejects_url_project_mismatch():
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=other_project_id)
    db = _DB(model=model, bindings=[_binding(other_project_id, role="viewer")])

    with pytest.raises(HTTPException) as exc:
        await load_authorized_model(
            db,
            _user(),
            model_id=model_id,
            project_id=project_id,
            min_role="viewer",
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_modeler_required_rejects_viewer_binding():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB(bindings=[_binding(project_id, role="viewer")])

    with pytest.raises(HTTPException) as exc:
        await ensure_project_model_access(
            db, _user(), project_id=project_id, model_id=model_id, min_role="modeler"
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_embed_project_scope_rejects_other_project():
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB()
    user = CurrentEmbedUser(
        user_id="embed@example.com",
        tenant_id="tenant-1",
        email="embed@example.com",
        project_ids=[str(other_project_id)],
        model_ids=None,
    )

    with pytest.raises(HTTPException) as exc:
        await ensure_project_model_access(
            db,
            user,
            project_id=project_id,
            model_id=model_id,
            min_role="viewer",
        )

    assert exc.value.status_code == 403
    assert "Project not in embed token scope" in exc.value.detail


@pytest.mark.asyncio
async def test_embed_project_scope_allows_matching_project_without_model_scope():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    db = _DB()
    user = CurrentEmbedUser(
        user_id="embed@example.com",
        tenant_id="tenant-1",
        email="embed@example.com",
        project_ids=[str(project_id)],
        model_ids=None,
    )

    await ensure_project_model_access(
        db,
        user,
        project_id=project_id,
        model_id=model_id,
        min_role="viewer",
    )
