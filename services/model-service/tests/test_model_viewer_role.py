"""Contract tests for the built-in Model-viewer role (Bug-8101 / F-104-01, D2).

Covers the five enforcement points that make the role real (not UI-only):

1. Role definition — ``model_viewer`` is an assignable project-access token at
   VIEWER privilege level (satisfies viewer, never modeler/admin).
2. Backend capability gate — a ``model_viewer`` binding is rejected 403 on
   authoring endpoints (require_role("modeler")) and allowed on read/query
   (require_role("viewer")).
4. Assignment validation — the Modeller-supersedes-Model-viewer invariant is
   enforced authoritatively in POST /access, both directions.

(3 routing/read-only and 5 frontend gating are covered by the frontend suite.)
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from .result_fakes import FakeScalarResult

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from shared.db.models import UserAccessBinding
from shared.schemas.pydantic_models import ModelResponse
from .conftest import make_model

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PROJECT_ID = uuid.uuid4()
PREFIX = f"/api/v1/projects/{PROJECT_ID}/access"


def _user(user_id: str, tenant_id: str = "acme") -> CurrentUser:
    return CurrentUser(user_id=user_id, tenant_id=tenant_id, email=user_id)


def _binding(role: str, model_id=None, uid="user@example.com") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        model_id=model_id,
        user_identity=uid,
        role=role,
        source="manual",
        created_at=NOW,
    )


class _Result:
    """Supports scalars().all(), .all(), scalar_one_or_none(), and .first()."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def first(self):
        return self._items[0] if self._items else None


@pytest.fixture(autouse=True)
def _no_grant_lock():
    """The supersession grant path takes a Postgres advisory lock
    (``_lock_user_project_grants``) that the mocked async session cannot honour;
    patch it to a no-op so these unit tests exercise the decision logic. The
    lock's serialization guarantee is a DB behaviour, not decision logic, so
    stubbing it here does not weaken what these tests assert."""
    with patch("src.api.access._lock_user_project_grants", new=AsyncMock()):
        yield


async def _yield(db):
    yield db


def _mock_db(execute_results):
    """execute_results: a list of _Result, returned in call order."""
    db = AsyncMock()
    db.add = lambda x: None
    db.commit = AsyncMock()
    db.delete = AsyncMock()

    async def _refresh(obj):
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        obj.created_at = NOW

    db.refresh = _refresh
    calls = {"i": 0}

    async def _execute(*_a, **_k):
        i = calls["i"]
        calls["i"] = min(i + 1, len(execute_results) - 1)
        return execute_results[i]

    db.execute = _execute
    return db


# ---------------------------------------------------------------------------
# 1. Role definition — pure logic
# ---------------------------------------------------------------------------

def test_model_viewer_is_assignable_at_viewer_level():
    from shared.auth.roles import (
        PROJECT_MODEL_VIEWER_ROLE,
        PROJECT_ROLE_SET,
        project_role_level,
    )

    assert PROJECT_MODEL_VIEWER_ROLE in PROJECT_ROLE_SET, "must be grantable"
    # Same privilege level as viewer: satisfies viewer, never modeler/admin.
    assert project_role_level("model_viewer") == project_role_level("viewer")
    assert project_role_level("model_viewer") > project_role_level("modeler")
    assert project_role_level("model_viewer") > project_role_level("admin")


def test_model_viewer_schema_role_accepted():
    from shared.schemas.pydantic_models import UserAccessBindingCreate

    b = UserAccessBindingCreate(user_identity="a@b.com", role="model_viewer")
    assert b.role == "model_viewer"


def test_supersession_scope_covers_logic():
    """Coverage is asymmetric: a Modeller scope supersedes a Model-viewer scope
    only when it COVERS (is a superset of) it — NOT on mere overlap. This is the
    invariant guard for Finding-1: a model-scoped modeler must NOT supersede a
    project-wide model_viewer (which would strip read on other models)."""
    from shared.auth.roles import scope_covers

    m = uuid.uuid4()
    other = uuid.uuid4()
    # Project-wide (None) covers everything.
    assert scope_covers(None, None) is True
    assert scope_covers(None, m) is True
    # A concrete outer covers only the SAME concrete inner...
    assert scope_covers(m, m) is True
    assert scope_covers(m, other) is False
    # ...and NEVER the project-wide inner (the Finding-1 case).
    assert scope_covers(m, None) is False


# ---------------------------------------------------------------------------
# 2. Backend capability gate — model_viewer is viewer-level
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_model_viewer_rejected_on_authoring_endpoint():
    """A model_viewer binding must be rejected 403 by require_role("modeler")
    (delete model is an authoring/SAVE-class endpoint)."""
    mv = _binding("model_viewer", uid="analyst@example.com")
    rbac_db = _mock_db([_Result([mv]), _Result([mv])])
    model_id = uuid.uuid4()

    app.dependency_overrides[get_current_user] = lambda: _user("analyst@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.auth.rbac.get_tenant_db", lambda tid: _yield(rbac_db)):
                resp = await ac.delete(
                    f"/api/v1/projects/{PROJECT_ID}/models/{model_id}"
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_project_wide_model_viewer_browses_all_models():
    """A PROJECT-WIDE model_viewer binding admits the model-list read (not 403)
    and, being project-wide, sees ALL models in the project. Exercises the
    resolve_listable_model_scope None (see-all) branch for a model_viewer
    principal; the model-SCOPED case is covered separately below."""
    m1, m2 = uuid.uuid4(), uuid.uuid4()
    model_1 = make_model(model_id=m1, project_id=PROJECT_ID)
    model_2 = make_model(model_id=m2, project_id=PROJECT_ID)
    for m in (model_1, model_2):
        m.deployed_version_id = None
        m.last_deployed_at = None
        m.canvas_layout = None
    # resolve bindings load -> one PROJECT-WIDE (model_id=None) model_viewer row
    # -> None (all models visible); then the Model list query returns both.
    list_db = _mock_db([
        _Result([(None,)]),
        _Result([model_1, model_2]),
    ])

    def _stub_decorate(_db, models):
        return [ModelResponse.model_validate(m) for m in models]

    app.dependency_overrides[get_current_user] = lambda: _user("analyst@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.models.get_tenant_db", lambda tid: _yield(list_db)), \
                 patch("src.api.models._decorate_models_batch", new=AsyncMock(side_effect=_stub_decorate)):
                resp = await ac.get(f"/api/v1/projects/{PROJECT_ID}/models")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()}
    assert ids == {str(m1), str(m2)}, "project-wide model_viewer sees all models"


# ---------------------------------------------------------------------------
# 4. Assignment validation — supersession, both directions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grant_modeler_over_model_viewer_requires_confirm():
    """Granting modeler where an overlapping model_viewer exists is rejected 409
    without supersede=true, so the UI can show the confirmation (cancel path —
    nothing changes)."""
    existing_mv = _binding("model_viewer")  # project-wide model_viewer
    # First execute = binding load (returns the model_viewer).
    db = _mock_db([_Result([existing_mv])])

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    PREFIX,
                    json={"user_identity": "user@example.com", "role": "modeler"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "modeller_supersedes_model_viewer"


@pytest.mark.asyncio
async def test_grant_modeler_over_same_scope_model_viewer_flips_in_place():
    """With supersede=true, granting modeler on the SAME scope as an existing
    model_viewer flips that binding's role to modeler in place (the upsert reuses
    the row) so only Modeller remains — no orphan row."""
    existing_mv = _binding("model_viewer")  # project-wide
    deleted = []
    # execute #1: binding load; #2: upsert lookup finds the SAME model_viewer row.
    db = _mock_db([_Result([existing_mv]), _Result([existing_mv])])
    db.delete = AsyncMock(side_effect=lambda obj: deleted.append(obj))

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    f"{PREFIX}?supersede=true",
                    json={"user_identity": "user@example.com", "role": "modeler"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
    assert resp.json()["role"] == "modeler"
    assert existing_mv.role == "modeler", "same-scope row flips to modeler"
    assert deleted == [], "no delete needed — the row was reused in place"


@pytest.mark.asyncio
async def test_grant_project_wide_modeler_removes_model_scoped_model_viewer():
    """A project-wide modeler grant supersedes a DIFFERENT-scope (model-scoped)
    model_viewer binding: that binding is deleted so only Modeller remains."""
    model_id = uuid.uuid4()
    model_scoped_mv = _binding("model_viewer", model_id=model_id)
    deleted = []
    # #1 binding load returns the model-scoped model_viewer; #2 upsert lookup for
    # the project-wide target finds no existing row → creates a new modeler.
    db = _mock_db([_Result([model_scoped_mv]), _Result([])])
    db.delete = AsyncMock(side_effect=lambda obj: deleted.append(obj))

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    f"{PREFIX}?supersede=true",
                    json={"user_identity": "user@example.com", "role": "modeler"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
    assert resp.json()["role"] == "modeler"
    assert model_scoped_mv in deleted, "the disjoint-scope model_viewer is removed"


@pytest.mark.asyncio
async def test_grant_model_viewer_over_modeler_requires_confirm():
    """Other direction: granting model_viewer where an overlapping modeler exists
    is rejected 409 without supersede=true (Modeller strictly supersedes)."""
    existing_modeler = _binding("modeler")
    db = _mock_db([_Result([existing_modeler])])

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    PREFIX,
                    json={"user_identity": "user@example.com", "role": "model_viewer"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_grant_model_viewer_over_modeler_confirmed_keeps_only_modeler():
    """With supersede=true, the redundant model_viewer grant is NOT persisted —
    the existing overlapping Modeller binding remains as the effective role."""
    existing_modeler = _binding("modeler")
    added = []
    db = _mock_db([_Result([existing_modeler])])
    db.add = lambda obj: added.append(obj) if isinstance(obj, UserAccessBinding) else None

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    f"{PREFIX}?supersede=true",
                    json={"user_identity": "user@example.com", "role": "model_viewer"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
    assert resp.json()["role"] == "modeler", "only Modeller remains"
    assert added == [], "no redundant model_viewer binding is created"


@pytest.mark.asyncio
async def test_grant_model_viewer_on_disjoint_scope_no_supersession():
    """A model_viewer grant on a model where the user is NOT a modeler is retained
    even if the user is a modeler on a DIFFERENT model (disjoint scopes)."""
    other_model = uuid.uuid4()
    target_model = uuid.uuid4()
    existing_modeler = _binding("modeler", model_id=other_model)
    added = []
    # #1 binding load; #2 upsert lookup (no existing row on target model).
    db = _mock_db([_Result([existing_modeler]), _Result([])])
    db.add = lambda obj: added.append(obj) if isinstance(obj, UserAccessBinding) else None

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    PREFIX,
                    json={
                        "user_identity": "user@example.com",
                        "role": "model_viewer",
                        "model_id": str(target_model),
                    },
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201
    assert len(added) == 1
    assert added[0].role == "model_viewer"
    assert str(added[0].model_id) == str(target_model)


@pytest.mark.asyncio
async def test_model_scoped_modeler_does_not_supersede_project_wide_model_viewer():
    """Finding-1 regression: granting a MODEL-scoped modeler must NOT supersede a
    PROJECT-WIDE model_viewer. The project-wide viewer gives read on every model;
    a modeler on one model does not cover it, so require_role precedence already
    yields authoring-on-B + read-elsewhere with NO deletion and NO 409."""
    target_model = uuid.uuid4()
    project_wide_mv = _binding("model_viewer", model_id=None)  # read on all models
    added = []
    deleted = []
    # #1 binding load returns the project-wide viewer; #2 upsert lookup (no row
    # on the target model) -> create a new model-scoped modeler.
    db = _mock_db([_Result([project_wide_mv]), _Result([])])
    db.add = lambda obj: added.append(obj) if isinstance(obj, UserAccessBinding) else None
    db.delete = AsyncMock(side_effect=lambda obj: deleted.append(obj))

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    PREFIX,  # NO supersede — must not be required
                    json={
                        "user_identity": "user@example.com",
                        "role": "modeler",
                        "model_id": str(target_model),
                    },
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201, "must NOT 409 — no supersession applies"
    assert resp.json()["role"] == "modeler"
    assert deleted == [], "the project-wide model_viewer must be retained"
    assert len(added) == 1 and str(added[0].model_id) == str(target_model)


@pytest.mark.asyncio
async def test_model_scoped_model_viewer_not_redundant_under_model_scoped_modeler_other_model():
    """Finding-1 regression (mirror): granting a PROJECT-WIDE model_viewer while
    the user is modeler on only ONE model is NOT redundant — the model-scoped
    modeler does not cover the project-wide grant, so it is persisted (and, being
    the broader scope, is not blocked)."""
    one_model = uuid.uuid4()
    model_scoped_modeler = _binding("modeler", model_id=one_model)
    added = []
    # #1 binding load; #2 upsert lookup (no project-wide row) -> create.
    db = _mock_db([_Result([model_scoped_modeler]), _Result([])])
    db.add = lambda obj: added.append(obj) if isinstance(obj, UserAccessBinding) else None

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    PREFIX,  # project-wide model_viewer, NO supersede needed
                    json={"user_identity": "user@example.com", "role": "model_viewer"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 201, "must NOT 409 — modeler does not cover project-wide"
    assert len(added) == 1
    assert added[0].role == "model_viewer"
    assert added[0].model_id is None


@pytest.mark.asyncio
async def test_preflight_reports_supersession():
    """The preflight endpoint reports whether supersession will fire, without
    mutating anything, so the admin UI can show the confirmation."""
    existing_mv = _binding("model_viewer")
    db = _mock_db([_Result([existing_mv])])

    app.dependency_overrides[get_current_user] = lambda: _user("admin@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.access.get_tenant_db", lambda tid: _yield(db)):
                resp = await ac.post(
                    f"{PREFIX}/preflight",
                    json={"user_identity": "user@example.com", "role": "modeler"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["supersedes"] is True
    assert body["removed_model_viewer_count"] == 1
    db.delete.assert_not_called()


# ---------------------------------------------------------------------------
# 2 (expanded). Backend capability gate — 403 boundary across authoring families
# (Codex finding 3: the only prior 403 test was model DELETE).
# ---------------------------------------------------------------------------


async def _authoring_403(method: str, path: str, rbac_results, json=None) -> int:
    """Drive an authoring request as a model_viewer principal and return status.

    ``rbac_results`` is the execute-result sequence require_role consumes."""
    rbac_db = _mock_db(rbac_results)
    app.dependency_overrides[get_current_user] = lambda: _user("analyst@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.auth.rbac.get_tenant_db", lambda tid: _yield(rbac_db)):
                resp = await ac.request(method, path, json=json)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    return resp.status_code


@pytest.mark.asyncio
async def test_model_viewer_403_on_model_patch():
    """PATCH model metadata (require_role modeler) must reject a model_viewer 403."""
    mv = _binding("model_viewer", model_id=None, uid="analyst@example.com")
    model_id = uuid.uuid4()
    # model-scoped lookup (path model_id) -> none; project lookup -> mv.
    status_code = await _authoring_403(
        "PATCH",
        f"/api/v1/projects/{PROJECT_ID}/models/{model_id}",
        [_Result([]), _Result([mv])],
        json={"display_name": "x"},
    )
    assert status_code == 403


@pytest.mark.asyncio
async def test_model_viewer_403_on_model_create():
    """POST create model (require_role modeler, project-level) must reject 403."""
    mv = _binding("model_viewer", model_id=None, uid="analyst@example.com")
    # no model_id in path -> project lookup none, existence probe finds a binding.
    status_code = await _authoring_403(
        "POST",
        f"/api/v1/projects/{PROJECT_ID}/models",
        [_Result([]), _Result([mv])],
        json={"slug": "newmodel", "display_name": "New"},
    )
    assert status_code == 403


@pytest.mark.asyncio
async def test_model_viewer_403_on_deploy():
    """POST deploy (require_role modeler) must reject a model_viewer 403."""
    model_id = uuid.uuid4()
    mv = _binding("model_viewer", model_id=model_id, uid="analyst@example.com")
    # model-scoped binding on this model -> viewer level -> rejects modeler.
    status_code = await _authoring_403(
        "POST",
        f"/api/v1/projects/{PROJECT_ID}/models/{model_id}/deploy",
        [_Result([mv])],
    )
    assert status_code == 403


@pytest.mark.asyncio
async def test_model_viewer_403_on_save_version():
    """POST /versions (SAVE, require_role modeler) must reject a model_viewer 403."""
    model_id = uuid.uuid4()
    mv = _binding("model_viewer", model_id=model_id, uid="analyst@example.com")
    status_code = await _authoring_403(
        "POST",
        f"/api/v1/projects/{PROJECT_ID}/models/{model_id}/versions",
        [_Result([mv])],
        json={},
    )
    assert status_code == 403


# ---------------------------------------------------------------------------
# 3. Read-only routing producer — caller_can_author=false + scope denial + browse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_model_reports_caller_cannot_author_for_model_viewer():
    """The model detail response must carry caller_can_author=false for a
    model_viewer principal (the producer signal that drives the read-only
    Model Builder). Guards the Bug-8017 producer-only class from the read side."""
    model_id = uuid.uuid4()
    mv = _binding("model_viewer", model_id=model_id, uid="analyst@example.com")
    # require_role("viewer") gate: model-scoped lookup -> mv (viewer ok).
    rbac_db = _mock_db([_Result([mv])])
    model = make_model(model_id=model_id, project_id=PROJECT_ID)
    model.deployed_version_id = None
    model.last_deployed_at = None
    model.canvas_layout = None
    models_db = _mock_db([
        _Result([]),        # _resolve_version_numbers: last_saved (none)
        _Result([mv]),      # caller_has_role: model-scoped lookup -> mv (viewer)
    ])
    models_db.get = AsyncMock(return_value=model)

    async def _noop_trust(_db, _m):
        return {"last_refreshed_at": None, "source_system": None, "owner": ""}

    app.dependency_overrides[get_current_user] = lambda: _user("analyst@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.auth.rbac.get_tenant_db", lambda tid: _yield(rbac_db)), \
                 patch("src.api.models.get_tenant_db", lambda tid: _yield(models_db)), \
                 patch("src.api.models._build_trust_meta", new=_noop_trust):
                resp = await ac.get(
                    f"/api/v1/projects/{PROJECT_ID}/models/{model_id}"
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    assert resp.json()["caller_can_author"] is False


@pytest.mark.asyncio
async def test_model_viewer_denied_outside_granted_model():
    """A model_viewer scoped to model M must be denied 403 on GET of a DIFFERENT
    model in the project (outside-scope denial via require_role("viewer"))."""
    granted = uuid.uuid4()
    other = uuid.uuid4()
    mv = _binding("model_viewer", model_id=granted, uid="analyst@example.com")
    # For the OTHER model: model-scoped lookup -> none; project lookup -> none;
    # existence probe -> project has bindings -> 403.
    rbac_db = _mock_db([_Result([]), _Result([]), _Result([mv])])

    app.dependency_overrides[get_current_user] = lambda: _user("analyst@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.auth.rbac.get_tenant_db", lambda tid: _yield(rbac_db)):
                resp = await ac.get(
                    f"/api/v1/projects/{PROJECT_ID}/models/{other}"
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_model_scoped_viewer_can_browse_only_granted_model():
    """Codex-HIGH regression: a MODEL-SCOPED model_viewer must be able to browse
    the model list (no 403) and see ONLY their granted model, not every model in
    the project. Guards the resolve_listable_model_scope fix end-to-end."""
    granted = uuid.uuid4()
    other = uuid.uuid4()
    model_granted = make_model(model_id=granted, project_id=PROJECT_ID)
    model_other = make_model(model_id=other, project_id=PROJECT_ID)
    for m in (model_granted, model_other):
        m.deployed_version_id = None
        m.last_deployed_at = None
        m.canvas_layout = None

    # list handler DB: #1 resolve bindings load -> one model-scoped row (granted);
    # #2 the Model list query -> both models.
    models_db = _mock_db([
        _Result([(granted,)]),                 # bindings: model_id rows
        _Result([model_granted, model_other]),  # all project models
    ])

    def _stub_decorate(_db, models):
        return [ModelResponse.model_validate(m) for m in models]

    app.dependency_overrides[get_current_user] = lambda: _user("analyst@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.models.get_tenant_db", lambda tid: _yield(models_db)), \
                 patch("src.api.models._decorate_models_batch", new=AsyncMock(side_effect=_stub_decorate)):
                resp = await ac.get(f"/api/v1/projects/{PROJECT_ID}/models")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200, "model-scoped viewer must NOT be 403'd on browse"
    ids = {m["id"] for m in resp.json()}
    assert ids == {str(granted)}, "must see ONLY the granted model, not all models"


# ---------------------------------------------------------------------------
# Codex-LOW: no project-existence disclosure oracle on the model-list route.
# An inaccessible EXISTING project and a NONEXISTENT project must return the
# SAME 403 — a caller must not be able to tell valid from invalid project ids.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_models_inaccessible_existing_project_403():
    """A member with NO binding on an EXISTING access-controlled project (the
    project has other bindings) is denied 403 on browse."""
    other_users_binding_id = uuid.uuid4()
    # #1 caller bindings load -> none; #2 binding existence probe -> a binding
    # EXISTS (project is access-controlled) -> 403 before any project lookup.
    models_db = _mock_db([
        _Result([]),
        _Result([other_users_binding_id]),
    ])

    app.dependency_overrides[get_current_user] = lambda: _user("nobody@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.models.get_tenant_db", lambda tid: _yield(models_db)):
                resp = await ac.get(f"/api/v1/projects/{PROJECT_ID}/models")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_models_nonexistent_project_403_not_empty_list():
    """A member browsing a NONEXISTENT project must get the SAME 403, NOT 200 []
    — otherwise the zero-binding bootstrap rule leaks project existence (a
    nonexistent id would 200-[] while an access-controlled id 403s)."""
    # #1 caller bindings -> none; #2 binding existence -> none (zero bindings);
    # #3 PROJECT existence -> none (project does not exist) -> 403.
    models_db = _mock_db([
        _Result([]),
        _Result([]),
        _Result([]),
    ])

    app.dependency_overrides[get_current_user] = lambda: _user("nobody@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.models.get_tenant_db", lambda tid: _yield(models_db)):
                resp = await ac.get(f"/api/v1/projects/{uuid.uuid4()}/models")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 403, "nonexistent project must 403, not leak 200 []"


@pytest.mark.asyncio
async def test_list_models_zero_binding_project_denies_member():
    """F-021-04 hard cutover (decision #9): a member on a REAL existing project
    that has zero bindings is now DENIED (403). The zero-binding "first-user
    implicit admin" bootstrap convenience was removed — access requires a
    persisted binding."""
    # resolve_listable_model_scope issues ONE query (caller bindings) and, when
    # the caller holds none, denies with 403 (no bootstrap, no existence probe).
    models_db = _mock_db([_Result([])])

    app.dependency_overrides[get_current_user] = lambda: _user("firstuser@example.com")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.models.get_tenant_db", lambda tid: _yield(models_db)):
                resp = await ac.get(f"/api/v1/projects/{PROJECT_ID}/models")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 403
