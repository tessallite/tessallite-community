"""
Tests for model-scoped RBAC binding enforcement.

Coverage:
  - Model-scoped 'modeler' binding overrides project 'viewer' binding (grants access).
  - Model-scoped 'viewer' binding prevents a 'modeler'-required endpoint (denies).
  - No model binding → falls back to project binding (grants).
  - No model AND no project binding → bootstrap-admin rule applies (grants if zero bindings).

Run from tessallite/services/model-service/:
    pytest tests/test_rbac_model_scope.py
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.auth.rbac import require_role, ROLE_HIERARCHY

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_PROJECT_ID = uuid.uuid4()
_MODEL_ID = uuid.uuid4()
_USER_ID = "user-123"


def _make_binding(role: str, model_id=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        user_identity=_USER_ID,
        project_id=_PROJECT_ID,
        model_id=model_id,
        role=role,
    )


def _make_db_with_bindings(bindings: list) -> MagicMock:
    """Build a mock DB that returns bindings matching the query filters."""
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        text = str(stmt)

        is_existence_probe = "user_identity" not in text

        matching = []
        for b in bindings:
            if b.user_identity == _USER_ID:
                if b.model_id is not None and "model_id" in text and str(b.model_id) in text:
                    matching.append(b)
                elif b.model_id is None and "IS NULL" in text.upper() or "is_(None)" in text.lower():
                    matching.append(b)
                elif not matching:
                    # fallback: return first matching binding for count query
                    matching.append(b)

        result.scalar_one_or_none.return_value = matching[0] if matching else None
        result.scalars.return_value.all.return_value = matching
        # Bootstrap existence probe (project_id only, no user_identity) reads
        # via .first() (F-H27R1-01). Return a row only if ANY binding exists
        # on the project, mirroring SELECT id ... LIMIT 1.
        if is_existence_probe:
            result.first.return_value = (bindings[0],) if bindings else None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


def test_role_hierarchy_comes_from_shared_taxonomy():
    from shared.auth.roles import PROJECT_ROLE_HIERARCHY

    assert ROLE_HIERARCHY == list(PROJECT_ROLE_HIERARCHY)


def test_legacy_project_role_aliases_are_viewer_equivalent():
    from src.auth.rbac import _role_level

    # Legacy/audience aliases never out-rank viewer, and an unknown role is
    # strictly below viewer (least privilege / fail-safe).
    assert _role_level("member") == _role_level("viewer")
    assert _role_level("analyst") == _role_level("viewer")
    assert _role_level("model_technical") == _role_level("viewer")
    assert _role_level("unknown") > _role_level("viewer")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_binding_overrides_project_binding_grants():
    """Model-scoped 'modeler' binding overrides project 'viewer' → access granted for modeler endpoint."""
    model_binding = _make_binding("modeler", model_id=_MODEL_ID)
    project_binding = _make_binding("viewer", model_id=None)
    db = _make_db_with_bindings([model_binding, project_binding])

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
    )

    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("modeler").dependency
        # Should not raise
        await dep_fn(
            project_id=_PROJECT_ID,
            model_id=_MODEL_ID,
            current_user=current_user,
        )


@pytest.mark.asyncio
async def test_model_binding_viewer_denies_modeler_endpoint():
    """Model-scoped 'viewer' binding prevents access when 'modeler' is required."""
    model_binding = _make_binding("viewer", model_id=_MODEL_ID)
    db = _make_db_with_bindings([model_binding])

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
    )

    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("modeler").dependency
        with pytest.raises(HTTPException) as exc_info:
            await dep_fn(
                project_id=_PROJECT_ID,
                model_id=_MODEL_ID,
                current_user=current_user,
            )
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_no_model_binding_falls_back_to_project_binding():
    """When no model-scoped binding exists, project binding is used."""
    project_binding = _make_binding("modeler", model_id=None)
    db = _make_db_with_bindings([project_binding])

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
    )

    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("modeler").dependency
        # Should not raise — project binding covers the request
        await dep_fn(
            project_id=_PROJECT_ID,
            model_id=None,
            current_user=current_user,
        )


@pytest.mark.asyncio
async def test_no_bindings_bootstrap_admin_rule():
    """If the project has zero bindings, treat caller as admin (bootstrap rule)."""
    db = _make_db_with_bindings([])  # no bindings at all

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
    )

    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("admin").dependency
        # Bootstrap: no bindings → implicit admin → should not raise
        await dep_fn(
            project_id=_PROJECT_ID,
            model_id=None,
            current_user=current_user,
        )


@pytest.mark.asyncio
async def test_bootstrap_admin_rule_emits_audit_event():
    """F-021-02 (accepted risk D2): when the bootstrap rule grants implicit
    admin, an audit event must be written so admins can see who used it."""
    import src.auth.rbac as rbac

    rbac._bootstrap_audit_seen.clear()
    db = _make_db_with_bindings([])  # no bindings → bootstrap fires
    db.commit = AsyncMock()

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
        email="user@example.com",
    )

    audit_mock = AsyncMock()
    with patch("shared.audit.logger.audit", audit_mock), \
         patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("admin").dependency
        await dep_fn(
            project_id=_PROJECT_ID,
            model_id=None,
            current_user=current_user,
        )

    audit_mock.assert_awaited_once()
    kwargs = audit_mock.await_args.kwargs
    assert kwargs["action"] == "rbac.bootstrap_admin_grant"
    assert kwargs["severity"] == "warn"
    assert kwargs["target_type"] == "project"
    assert kwargs["target_id"] == _PROJECT_ID
    assert kwargs["actor_email"] == "user@example.com"
    assert kwargs["detail"]["rule"] == "bootstrap_admin"
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_bootstrap_admin_audit_is_deduplicated():
    """The same (tenant, user, project) only writes one event per TTL window."""
    import src.auth.rbac as rbac

    rbac._bootstrap_audit_seen.clear()
    db = _make_db_with_bindings([])
    db.commit = AsyncMock()

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
        email="user@example.com",
    )

    audit_mock = AsyncMock()
    with patch("shared.audit.logger.audit", audit_mock), \
         patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("admin").dependency
        await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=current_user)
        await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=current_user)

    audit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_denied_request_does_not_emit_bootstrap_audit():
    """A caller rejected because other bindings exist must not produce the
    bootstrap-admin audit event."""
    import src.auth.rbac as rbac

    rbac._bootstrap_audit_seen.clear()
    other_user_binding = types.SimpleNamespace(
        user_identity="someone-else",
        project_id=_PROJECT_ID,
        model_id=None,
        role="admin",
    )

    # Caller has no binding, but the project has one (another user's): the
    # caller-scoped lookups (filtered by user_identity) return None, while
    # the any-binding existence query returns the other user's row, so the
    # bootstrap rule must NOT fire and access is denied.
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        text = str(stmt)
        # Caller-scoped lookups filter on user_identity in the WHERE clause;
        # the any-binding existence query filters on project_id only and now
        # reads via .first() (F-H27R1-01).
        if "user_identity =" in text:
            result.scalar_one_or_none.return_value = None
        else:
            result.first.return_value = (other_user_binding.project_id,)
        return result

    db.execute = AsyncMock(side_effect=_execute)

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
        email="user@example.com",
    )

    audit_mock = AsyncMock()
    with patch("shared.audit.logger.audit", audit_mock), \
         patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("admin").dependency
        with pytest.raises(HTTPException) as exc_info:
            await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=current_user)
        assert exc_info.value.status_code == 403

    audit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_binding_unbound_user_gets_403_not_500():
    """F-H27R1-01 regression: a project with TWO OR MORE bindings, accessed by
    an unbound user, must return 403 — not raise MultipleResultsFound (HTTP
    500). The existence probe uses .first()/limit(1), which never raises on
    multiple rows, unlike the previous scalar_one_or_none()."""
    import src.auth.rbac as rbac
    from sqlalchemy.exc import MultipleResultsFound

    rbac._bootstrap_audit_seen.clear()

    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        text = str(stmt)
        if "user_identity =" in text:
            # Caller has no binding of their own.
            result.scalar_one_or_none.return_value = None
        else:
            # The existence probe must use .first() — exercising the OLD
            # scalar_one_or_none() against >=2 rows would raise, so we make
            # that path explode to prove the code no longer touches it.
            result.scalar_one_or_none.side_effect = MultipleResultsFound(
                "Multiple rows were found when one or none was required"
            )
            # .first() returns the first row of a multi-row existence result.
            result.first.return_value = (uuid.uuid4(),)
        return result

    db.execute = AsyncMock(side_effect=_execute)

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
        email="user@example.com",
    )

    audit_mock = AsyncMock()
    with patch("shared.audit.logger.audit", audit_mock), \
         patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("admin").dependency
        with pytest.raises(HTTPException) as exc_info:
            await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=current_user)
        # Clean deny, not a 500 from an unhandled MultipleResultsFound.
        assert exc_info.value.status_code == 403

    # Deny path: no bootstrap audit event.
    audit_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# ML26: explicit fail-closed RBAC coverage (wrong role -> 403)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_viewer_denied_modeler_write():
    """A user with a project-level 'viewer' binding must get 403 on a
    modeler-gated (write) endpoint — fail closed."""
    project_binding = _make_binding("viewer", model_id=None)
    db = _make_db_with_bindings([project_binding])
    current_user = types.SimpleNamespace(role="member", tenant_id="t", user_id=_USER_ID)

    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen
        dep_fn = require_role("modeler").dependency
        with pytest.raises(HTTPException) as exc:
            await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=current_user)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_unknown_binding_role_fails_closed():
    """A binding carrying an unrecognised role is treated as below every known
    tier (deny), never as an accidental grant — fail closed."""
    weird = _make_binding("superuser", model_id=None)  # not in ROLE_HIERARCHY
    db = _make_db_with_bindings([weird])
    current_user = types.SimpleNamespace(role="member", tenant_id="t", user_id=_USER_ID)

    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen
        dep_fn = require_role("viewer").dependency
        with pytest.raises(HTTPException) as exc:
            await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=current_user)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_embed_user_denied_write_route():
    """Embed tokens may read (viewer) but never modify (modeler/admin)."""
    from src.auth.middleware import CurrentEmbedUser
    embed = CurrentEmbedUser(user_id="e", tenant_id="t", email="e")

    dep_fn = require_role("modeler").dependency
    with pytest.raises(HTTPException) as exc:
        await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=embed)
    assert exc.value.status_code == 403
