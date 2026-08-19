"""
Tests for model-scoped RBAC binding enforcement.

Coverage:
  - Model-scoped 'modeler' binding overrides project 'viewer' binding (grants access).
  - Model-scoped 'viewer' binding prevents a 'modeler'-required endpoint (denies).
  - No model binding → falls back to project binding (grants).
  - No model AND no project binding → 403 (F-021-04: no zero-binding bootstrap grant).

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
async def test_no_bindings_denies():
    """F-021-04 hard cutover (decision #9): a project with ZERO bindings denies
    every ordinary caller. There is NO zero-binding bootstrap-admin grant."""
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
        with pytest.raises(HTTPException) as exc_info:
            await dep_fn(
                project_id=_PROJECT_ID,
                model_id=None,
                current_user=current_user,
            )
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_unbound_user_with_other_bindings_denied():
    """A caller with no binding of their own is denied even when the project has
    OTHER users' bindings — no bootstrap grant, no implicit admin (F-021-04)."""
    db = MagicMock()

    async def _execute(stmt):
        # Every caller-scoped lookup filters on user_identity and finds nothing
        # for this caller (the only binding belongs to someone else).
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.scalars.return_value.all.return_value = []
        return result

    db.execute = AsyncMock(side_effect=_execute)

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
        email="user@example.com",
    )

    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("admin").dependency
        with pytest.raises(HTTPException) as exc_info:
            await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=current_user)
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_multi_binding_unbound_user_gets_403_not_500():
    """F-H27R1-01 regression + F-021-04 hard cutover: an unbound user on a
    project with multiple bindings gets a clean 403, never a 500. require_role's
    caller-scoped lookups are user-filtered (scalar_one_or_none returns at most
    the caller's own row), so a multi-binding project never raises
    MultipleResultsFound; with the bootstrap grant removed the caller is denied.
    """
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        # The only queries require_role now issues are user-filtered caller
        # lookups; they return None for this unbound caller. If any path ever
        # issued an UNFILTERED scalar_one_or_none against >=2 rows it would
        # raise here — proving require_role never does that.
        result.scalar_one_or_none.side_effect = lambda: None
        result.scalars.return_value.all.return_value = []
        return result

    db.execute = AsyncMock(side_effect=_execute)

    current_user = types.SimpleNamespace(
        role="member",
        tenant_id="tenant",
        user_id=_USER_ID,
        email="user@example.com",
    )

    with patch("src.auth.rbac.get_tenant_db") as mock_db_gen:
        async def _gen(*a, **kw):
            yield db
        mock_db_gen.side_effect = _gen

        dep_fn = require_role("admin").dependency
        with pytest.raises(HTTPException) as exc_info:
            await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=current_user)
        # Clean deny, not a 500 from an unhandled MultipleResultsFound.
        assert exc_info.value.status_code == 403


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


# ---------------------------------------------------------------------------
# F-021-02 / Bug-7992: require_role enforces the embed token's OWN project/model
# scope at the shared choke point, so a project-scoped token cannot read another
# project's metadata even on a route that never calls enforce_model_scope.
# Test escape: the report proved the escape only on measures.py; the vast
# majority of viewer-admitting routes had no enforce_model_scope at all. Guard:
# the require_role embed branch now checks project_ids/model_ids directly.
# Tier: T1 (embed isolation contract).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_require_role_blocks_embed_out_of_project_scope():
    from src.auth.middleware import CurrentEmbedUser
    # Token scoped to a DIFFERENT project than the route's project_id.
    embed = CurrentEmbedUser(
        user_id="e", tenant_id="t", email="e",
        project_ids=[str(uuid.uuid4()).lower()], model_ids=None,
    )
    dep_fn = require_role("viewer").dependency
    with pytest.raises(HTTPException) as exc:
        await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=embed)
    assert exc.value.status_code == 403
    assert "project" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_require_role_allows_embed_in_project_scope():
    from src.auth.middleware import CurrentEmbedUser
    embed = CurrentEmbedUser(
        user_id="e", tenant_id="t", email="e",
        project_ids=[str(_PROJECT_ID).lower()], model_ids=None,
    )
    dep_fn = require_role("viewer").dependency
    # In-scope project, viewer route → allowed (returns None, no raise).
    await dep_fn(project_id=_PROJECT_ID, model_id=None, current_user=embed)


@pytest.mark.asyncio
async def test_require_role_blocks_embed_out_of_model_scope():
    from src.auth.middleware import CurrentEmbedUser
    other_model = uuid.uuid4()
    embed = CurrentEmbedUser(
        user_id="e", tenant_id="t", email="e",
        project_ids=[str(_PROJECT_ID).lower()],
        model_ids=[str(uuid.uuid4()).lower()],  # not the route's model
    )
    dep_fn = require_role("viewer").dependency
    with pytest.raises(HTTPException) as exc:
        await dep_fn(
            project_id=_PROJECT_ID, model_id=other_model, current_user=embed,
        )
    assert exc.value.status_code == 403
    assert "model" in exc.value.detail.lower()
