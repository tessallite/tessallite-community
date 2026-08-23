"""
Explorer RBAC compliance matrix — backend enforcement (accept + reject).

Verifies the per-role privilege table documented in
docs/architecture/architecture_explorer-rbac-matrix.md is actually enforced by
the model-service for the three product personas:

  * Viewer       — member with a project 'viewer' binding (read-only).
  * Modeller     — member with a project 'modeler' binding (model-level setup).
  * Tenant Admin — tenant_admin / system_admin (everything; inherits the rest).

Covered policy decisions (2026-06-22):
  * delete_model is now modeler-gated (a modeller owns the full model lifecycle).
  * create_project is tenant_admin-only (was implicitly any authenticated user).
  * update_project: rename is modeler+, but enabling/disabling a project (is_active)
    requires admin.
  * Tenant admin can do everything a modeller and viewer can do (bypass).

Run from tessallite/services/model-service/:
    pytest tests/test_explorer_privilege_matrix.py
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.auth.middleware import CurrentServiceUser, require_tenant_admin
from src.auth.rbac import (
    caller_has_role,
    filter_projects_by_user_access,
    require_role,
)

_PROJECT_ID = uuid.uuid4()
_MODEL_ID = uuid.uuid4()
_USER_ID = "user-123"


def _make_binding(
    role: str,
    model_id=None,
    user_identity: str = _USER_ID,
    project_id=_PROJECT_ID,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        user_identity=user_identity,
        project_id=project_id,
        model_id=model_id,
        role=role,
    )


def _make_db_with_bindings(bindings: list) -> MagicMock:
    """Mock DB that answers binding lookups for the caller (mirrors the
    pattern in test_rbac_model_scope.py)."""
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        text = str(stmt)
        is_existence_probe = "user_identity" not in text
        if "user_access_bindings.project_id, user_access_bindings.user_identity" in text:
            result.all.return_value = [
                (binding.project_id, binding.user_identity) for binding in bindings
            ]
            return result
        params = stmt.compile().params
        query_user_identity = next(
            (value for key, value in params.items() if key.startswith("user_identity")),
            next((value for key, value in params.items() if key.startswith("lower")), _USER_ID),
        )
        matching = [
            b for b in bindings
            if b.user_identity == query_user_identity
            or (
                "@" in str(query_user_identity)
                and str(b.user_identity).lower() == str(query_user_identity).lower()
            )
        ]
        result.scalar_one_or_none.return_value = matching[0] if matching else None
        if is_existence_probe:
            result.first.return_value = (bindings[0],) if bindings else None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


def _member(role: str = "member") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        role=role,
        tenant_id="__system__" if role == "system_admin" else "t",
        user_id=_USER_ID,
        email="u@x",
    )


def _service(role: str = "tenant_admin") -> CurrentServiceUser:
    return CurrentServiceUser(
        principal="model-service-deploy",
        tenant_id="t",
        role=role,
        scopes=["query-router.cache-evict"],
    )


async def _run_require_role(
    min_role: str,
    binding_role: str | None,
    user_role="member",
    current_user=None,
):
    """Drive require_role(min_role) for a member holding a single project
    binding of binding_role (or no binding when None). Raises HTTPException on
    deny; returns None on grant."""
    bindings = [_make_binding(binding_role)] if binding_role else []
    db = _make_db_with_bindings(bindings)
    user = current_user or _member(user_role)
    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen
        dep_fn = require_role(min_role).dependency
        await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=user)


# ---------------------------------------------------------------------------
# require_role enforcement engine: binding tier vs required tier
# ---------------------------------------------------------------------------

# (min_role, binding_role, should_grant)
_MATRIX = [
    # viewer-gated reads (e.g. get_project, export_model)
    ("viewer", "viewer", True),
    ("viewer", "modeler", True),
    ("viewer", "admin", True),
    # modeler-gated writes (rename project, create/rename/delete/deploy model)
    ("modeler", "viewer", False),
    ("modeler", "modeler", True),
    ("modeler", "admin", True),
    # admin-gated (delete project, enable/disable project, access bindings)
    ("admin", "viewer", False),
    ("admin", "modeler", False),
    ("admin", "admin", True),
]


@pytest.mark.parametrize("min_role,binding_role,should_grant", _MATRIX)
@pytest.mark.asyncio
async def test_require_role_matrix(min_role, binding_role, should_grant):
    if should_grant:
        await _run_require_role(min_role, binding_role)  # no raise
    else:
        with pytest.raises(HTTPException) as exc:
            await _run_require_role(min_role, binding_role)
        assert exc.value.status_code == 403


@pytest.mark.parametrize("min_role", ["viewer", "modeler", "admin"])
@pytest.mark.asyncio
async def test_tenant_admin_inherits_every_tier(min_role):
    """Tenant admin can do everything a modeller/viewer can — bypasses bindings
    on every gate, even with no binding present."""
    await _run_require_role(min_role, binding_role=None, user_role="tenant_admin")


@pytest.mark.parametrize("min_role", ["viewer", "modeler", "admin"])
@pytest.mark.asyncio
async def test_system_admin_inherits_every_tier(min_role):
    await _run_require_role(min_role, binding_role=None, user_role="system_admin")


@pytest.mark.parametrize("service_role", ["tenant_admin", "system_admin"])
@pytest.mark.asyncio
async def test_require_role_service_principal_admin_role_does_not_bypass_bindings(service_role):
    with pytest.raises(HTTPException) as exc:
        await _run_require_role(
            "admin",
            binding_role="viewer",
            current_user=_service(service_role),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_matches_legacy_mixed_case_email_binding():
    user = types.SimpleNamespace(
        role="member",
        tenant_id="t",
        user_id="alice@example.com",
        email="alice@example.com",
    )
    db = _make_db_with_bindings([
        _make_binding("modeler", user_identity="Alice@Example.COM"),
    ])
    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen
        dep_fn = require_role("modeler").dependency
        await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=user)


# ---------------------------------------------------------------------------
# Model delete is now modeler-gated (policy change)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_modeller_can_delete_model():
    """A project modeller owns model setup, including deletion."""
    await _run_require_role("modeler", binding_role="modeler")  # no raise


@pytest.mark.asyncio
async def test_viewer_cannot_delete_model():
    with pytest.raises(HTTPException) as exc:
        await _run_require_role("modeler", binding_role="viewer")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# create_project is tenant_admin-only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_project_requires_tenant_admin():
    for user in (_member("tenant_admin"), _member("system_admin")):
        assert await require_tenant_admin(current_user=user) is user
    for role in ("member", "modeler", "viewer"):
        with pytest.raises(HTTPException) as exc:
            await require_tenant_admin(current_user=_member(role))
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# update_project: rename is modeler+, but enable/disable (is_active) needs admin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caller_has_role_admin_split():
    """The is_active guard relies on caller_has_role(..., 'admin'): a modeller
    is not admin, a tenant admin is."""
    db = _make_db_with_bindings([_make_binding("modeler")])
    assert await caller_has_role(db, _member("member"), _PROJECT_ID, "admin") is False
    # modeler binding still satisfies modeler-tier rename
    assert await caller_has_role(db, _member("member"), _PROJECT_ID, "modeler") is True
    # tenant admin bypasses
    assert await caller_has_role(db, _member("tenant_admin"), _PROJECT_ID, "admin") is True
    assert await caller_has_role(db, _member("system_admin"), _PROJECT_ID, "admin") is True


@pytest.mark.parametrize("service_role", ["tenant_admin", "system_admin"])
@pytest.mark.asyncio
async def test_caller_has_role_service_principal_categorically_denied(service_role):
    """Service principals never pass human RBAC checks (AUTH-RR-01).
    They must use scope-based dependencies exclusively."""
    user = _service(service_role)
    db = _make_db_with_bindings([
        _make_binding("viewer", user_identity=user.user_id),
    ])
    # Even with a viewer binding, service principals return False
    assert (
        await caller_has_role(db, user, _PROJECT_ID, "admin")
        is False
    )
    assert (
        await caller_has_role(db, user, _PROJECT_ID, "viewer")
        is False
    )


@pytest.mark.asyncio
async def test_filter_projects_human_admins_see_all_projects():
    project_ids = [_PROJECT_ID, uuid.uuid4()]
    db = _make_db_with_bindings([_make_binding("viewer")])

    assert await filter_projects_by_user_access(
        db,
        project_ids,
        _USER_ID,
        current_user=_member("tenant_admin"),
    ) == set(project_ids)
    assert await filter_projects_by_user_access(
        db,
        project_ids,
        _USER_ID,
        current_user=_member("system_admin"),
    ) == set(project_ids)


@pytest.mark.parametrize("service_role", ["tenant_admin", "system_admin"])
@pytest.mark.asyncio
async def test_filter_projects_service_principal_admin_role_uses_bindings(service_role):
    other_project_id = uuid.uuid4()
    user = _service(service_role)
    db = _make_db_with_bindings([
        _make_binding("viewer", user_identity=user.user_id),
        _make_binding(
            "viewer",
            user_identity="other@example.com",
            project_id=other_project_id,
        ),
    ])

    visible = await filter_projects_by_user_access(
        db,
        [_PROJECT_ID, other_project_id],
        user.user_id,
        current_user=user,
    )

    assert visible == {_PROJECT_ID}


@pytest.mark.asyncio
async def test_update_project_disable_denied_for_modeller():
    """Handler-level: a modeller toggling is_active gets 403, not a silent
    success."""
    from shared.schemas.pydantic_models import ProjectUpdate
    from src.api.projects import update_project

    project = types.SimpleNamespace(id=_PROJECT_ID, slug="p", display_name="P", is_active=True)
    db = _make_db_with_bindings([_make_binding("modeler")])
    db.get = AsyncMock(return_value=project)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch("src.api.projects.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen
        with pytest.raises(HTTPException) as exc:
            await update_project(
                project_id=_PROJECT_ID,
                body=ProjectUpdate(is_active=False),
                current_user=_member("member"),
            )
    assert exc.value.status_code == 403
    db.commit.assert_not_awaited()
