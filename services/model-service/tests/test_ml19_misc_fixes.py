"""ML19 (unit 029) — scratchpad / pivot-view / preferences / branding fixes.

Business-outcome tests for the medium/low findings closed in ML19:

  F-029-06  embed tokens cannot write scratchpad / pivot / preference rows
  F-029-08  scratchpad multi-statement expr → 400 (not 500); duplicate → 409
  F-029-11  branding / scratchpad optional fields are clearable (exclude_unset)
  F-029-12  preferences validate entity_type; scratchpad PATCH checks scope
  F-029-13  branding rejects a mismatched tenant_id and a bad hex colour
  F-029-14  scratchpad rejects an unknown data_type at the API
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.main import app
from src.auth.middleware import (
    CurrentUser,
    CurrentEmbedUser,
    get_current_user,
)
from shared.db.models import Model, ScratchpadMeasure, SavedPivotView
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)

USER_EMAIL = TEST_USER_ID
BASE = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


@pytest.fixture
def auth():
    user = CurrentUser(user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=USER_EMAIL)
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def embed_auth():
    user = CurrentEmbedUser(
        user_id="embed-token",
        tenant_id=TEST_TENANT,
        email="embed@token",
        model_ids=[str(TEST_MODEL_ID)],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client(auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.fixture
async def embed_client(embed_auth):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


def _scratch(name="m1", created_by=USER_EMAIL, data_type="numeric"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name="Display",
        expression="1 + 1",
        data_type=data_type,
        format="0.00",
        created_by=created_by,
        created_at=NOW,
        updated_at=NOW,
    )


def _db_with(scratch=None, pivot=None) -> AsyncMock:
    db = make_mock_db()
    model = make_model()

    async def _get(cls, ident):
        if cls is Model:
            return model
        if cls is ScratchpadMeasure:
            return scratch
        if cls is SavedPivotView:
            return pivot
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock(return_value=None)
    return db


# --------------------------------------------------------------------------
# F-029-14 / F-029-08: scratchpad input validation
# --------------------------------------------------------------------------

class TestScratchpadValidation:

    @pytest.mark.asyncio
    async def test_unknown_data_type_rejected_422(self, client):
        db = _db_with()
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{BASE}/scratchpad-measures",
                json={"name": "x", "expression": "1+1", "data_type": "decimal"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_multi_statement_expression_is_400_not_500(self, client):
        db = _db_with()
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{BASE}/scratchpad-measures",
                json={"name": "x", "expression": "1; SELECT 2"},
            )
        assert resp.status_code == 400
        assert "single SQL expression" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_duplicate_name_is_409_not_500(self, client):
        db = _db_with()
        existing = _scratch()
        dup_result = AsyncMock()
        dup_result.scalar_one_or_none = lambda: existing
        db.execute = AsyncMock(return_value=dup_result)
        # Bug-8162: this test used to reach the duplicate check with NO router
        # stub at all — it passed only because the (unreachable) router failed
        # OPEN in the test environment. Under the fail-closed contract that is
        # now a 503, so the router leg has to be stubbed explicitly. The
        # assertion below is unchanged: what is under test is still "duplicate
        # name → 409, not 500".
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)), \
             patch(
                 "src.api.scratchpad_measures._validate_expression_against_model",
                 AsyncMock(return_value=None),
             ):
            resp = await client.post(
                f"{BASE}/scratchpad-measures",
                json={"name": "m1", "expression": "1+1"},
            )
        assert resp.status_code == 409
        db.add.assert_not_called()


# --------------------------------------------------------------------------
# F-029-11: clearable optional fields on scratchpad PATCH
# --------------------------------------------------------------------------

class TestScratchpadClearable:

    @pytest.mark.asyncio
    async def test_explicit_null_format_clears_it(self, client):
        row = _scratch()
        db = _db_with(scratch=row)
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)):
            resp = await client.patch(
                f"{BASE}/scratchpad-measures/{row.id}",
                json={"format": None},
            )
        assert resp.status_code == 200
        assert row.format is None

    @pytest.mark.asyncio
    async def test_absent_format_left_untouched(self, client):
        row = _scratch()
        db = _db_with(scratch=row)
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)):
            resp = await client.patch(
                f"{BASE}/scratchpad-measures/{row.id}",
                json={"display_name": "New"},
            )
        assert resp.status_code == 200
        assert row.format == "0.00"  # unchanged
        assert row.display_name == "New"


# --------------------------------------------------------------------------
# F-029-06: embed tokens cannot mutate
# --------------------------------------------------------------------------

class TestEmbedCannotMutate:

    @pytest.mark.asyncio
    async def test_embed_cannot_create_scratchpad(self, embed_client):
        db = _db_with()
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)):
            resp = await embed_client.post(
                f"{BASE}/scratchpad-measures",
                json={"name": "x", "expression": "1+1"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_embed_cannot_create_pivot_view(self, embed_client):
        db = _db_with()
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)):
            resp = await embed_client.post(
                f"{BASE}/pivot-views",
                json={"name": "v", "measure_id": "m", "row_dim_ids": [], "col_dim_ids": []},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_embed_cannot_list_pivot_views(self, embed_client):
        # Bug-6423: pivot-view reads must fail closed for embed tokens, matching
        # the stricter saved-queries read endpoints.
        db = _db_with()
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)):
            resp = await embed_client.get(f"{BASE}/pivot-views")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_embed_cannot_toggle_favourite(self, embed_client):
        db = _db_with()
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await embed_client.put(
                f"{BASE}/preferences/favourite",
                json={"entity_type": "kpi", "entity_id": str(uuid.uuid4())},
            )
        assert resp.status_code == 403


# --------------------------------------------------------------------------
# F-029-12: preference entity_type validation
# --------------------------------------------------------------------------

class TestPreferenceValidation:

    @pytest.mark.asyncio
    async def test_unknown_entity_type_rejected_422(self, client):
        db = _db_with()
        with patch("src.api.preferences.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{BASE}/preferences/favourite",
                json={"entity_type": "rogue", "entity_id": str(uuid.uuid4())},
            )
        assert resp.status_code == 422


# --------------------------------------------------------------------------
# F-029-11 / F-029-13: branding clearing, tenant mismatch, hex validation
# --------------------------------------------------------------------------

class TestBranding:

    @pytest.mark.asyncio
    async def test_mismatched_tenant_id_rejected_403(self, auth):
        # require_tenant_admin override
        from src.auth.middleware import require_tenant_admin

        app.dependency_overrides[require_tenant_admin] = lambda: CurrentUser(
            user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=USER_EMAIL,
            role="tenant_admin",
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as ac:
                resp = await ac.put(
                    "/api/v1/tenants/other-tenant/branding",
                    json={"primary_color": "#0B5FFF"},
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(require_tenant_admin, None)

    @pytest.mark.asyncio
    async def test_bad_hex_colour_rejected_422(self, auth):
        from src.auth.middleware import require_tenant_admin

        app.dependency_overrides[require_tenant_admin] = lambda: CurrentUser(
            user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=USER_EMAIL,
            role="tenant_admin",
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as ac:
                resp = await ac.put(
                    f"/api/v1/tenants/{TEST_TENANT}/branding",
                    json={"primary_color": "not-a-colour"},
                )
            assert resp.status_code == 422
        finally:
            app.dependency_overrides.pop(require_tenant_admin, None)


# --------------------------------------------------------------------------
# F-029-22: saved-pivot-view sharing + ownership
# --------------------------------------------------------------------------

def _pivot(name="v1", created_by=USER_EMAIL, is_shared=False):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        measure_id="m",
        row_dim_ids="[]",
        col_dim_ids="[]",
        config_json=None,
        created_by=created_by,
        is_shared=is_shared,
        created_at=NOW,
        updated_at=NOW,
    )


class TestPivotViewSharing:

    @pytest.mark.asyncio
    async def test_create_shared_view_persists_flag(self, client):
        from shared.db.models import Measure as MeasureModel
        db = _db_with()
        measure_id = uuid.uuid4()
        # Bug-5316 validation queries for the measure and dimensions.
        # The test needs the mock to return a valid measure for measure_id.
        _orig_get = db.get

        async def _get_with_measure(cls, ident):
            if cls is MeasureModel and str(ident) == str(measure_id):
                return types.SimpleNamespace(id=measure_id, model_id=TEST_MODEL_ID)
            return await _orig_get(cls, ident)

        db.get = AsyncMock(side_effect=_get_with_measure)

        async def _refresh(view):
            # The real DB populates server-default timestamps on refresh; the
            # mock must too, or _to_response().isoformat() fails on None.
            view.id = uuid.uuid4()
            view.created_at = NOW
            view.updated_at = NOW

        db.refresh = AsyncMock(side_effect=_refresh)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{BASE}/pivot-views",
                json={
                    "name": "v", "measure_id": str(measure_id),
                    "row_dim_ids": [], "col_dim_ids": [], "is_shared": True,
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["is_shared"] is True
        assert body["is_owner"] is True
        added = db.add.call_args[0][0]
        assert added.is_shared is True

    @pytest.mark.asyncio
    async def test_create_view_with_empty_measure_id_succeeds(self, client):
        # Bug-6412: a first measure of Record Count / scratchpad / none makes the
        # frontend send an empty primary measure_id (the real selection lives in
        # config). The save must succeed rather than 400 on measure validation.
        db = _db_with()

        async def _refresh(view):
            view.id = uuid.uuid4()
            view.created_at = NOW
            view.updated_at = NOW

        db.refresh = AsyncMock(side_effect=_refresh)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{BASE}/pivot-views",
                json={
                    "name": "rc-only", "measure_id": "",
                    "row_dim_ids": [], "col_dim_ids": [],
                },
            )
        assert resp.status_code == 201
        assert resp.json()["measure_id"] == ""

    @pytest.mark.asyncio
    async def test_list_marks_others_shared_view_not_owned_or_editable(self, client):
        # A view shared by a colleague is visible, marked not-owned, and — for a
        # non-modeler caller — not editable.
        other = _pivot(name="shared-by-bob", created_by="bob@acme", is_shared=True)
        rows = AsyncMock()
        rows.scalars = lambda: types.SimpleNamespace(all=lambda: [other])
        db = _db_with()
        db.execute = AsyncMock(return_value=rows)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)), \
             patch("src.api.pivot_views.caller_has_role", AsyncMock(return_value=False)):
            resp = await client.get(f"{BASE}/pivot-views")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["is_shared"] is True
        assert items[0]["is_owner"] is False
        assert items[0]["can_edit"] is False
        assert items[0]["created_by"] == "bob@acme"

    @pytest.mark.asyncio
    async def test_list_marks_others_shared_view_editable_for_modeler(self, client):
        # Bug-5839: a modeler may edit/delete shared views they do not own, so
        # can_edit is True even though they are not the owner.
        other = _pivot(name="shared-by-bob", created_by="bob@acme", is_shared=True)
        rows = AsyncMock()
        rows.scalars = lambda: types.SimpleNamespace(all=lambda: [other])
        db = _db_with()
        db.execute = AsyncMock(return_value=rows)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)), \
             patch("src.api.pivot_views.caller_has_role", AsyncMock(return_value=True)):
            resp = await client.get(f"{BASE}/pivot-views")
        items = resp.json()
        assert items[0]["is_owner"] is False
        assert items[0]["can_edit"] is True

    @pytest.mark.asyncio
    async def test_non_owner_viewer_cannot_delete_shared_view(self, client):
        # A viewer cannot delete a colleague's shared view — 403, not a silent
        # 404 that hides a view they can actually see (Bug-5839 boundary).
        other = _pivot(created_by="bob@acme", is_shared=True)
        db = _db_with(pivot=other)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)), \
             patch("src.api.pivot_views.caller_has_role", AsyncMock(return_value=False)):
            resp = await client.delete(f"{BASE}/pivot-views/{other.id}")
        assert resp.status_code == 403
        db.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_cannot_delete_personal_view(self, client):
        # A personal (unshared) view of another user stays private: 404 hides its
        # existence, regardless of the caller's role.
        other = _pivot(created_by="bob@acme", is_shared=False)
        db = _db_with(pivot=other)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{BASE}/pivot-views/{other.id}")
        assert resp.status_code == 404
        db.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_modeler_can_delete_shared_view(self, client):
        # Bug-5839: a modeler may delete a shared view they do not own.
        other = _pivot(created_by="bob@acme", is_shared=True)
        db = _db_with(pivot=other)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)), \
             patch("src.api.pivot_views.caller_has_role", AsyncMock(return_value=True)):
            resp = await client.delete(f"{BASE}/pivot-views/{other.id}")
        assert resp.status_code == 204
        db.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_modeler_can_update_shared_view(self, client):
        # Bug-5839: a modeler may rename/retire a shared view they do not own.
        other = _pivot(name="old", created_by="bob@acme", is_shared=True)
        db = _db_with(pivot=other)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)), \
             patch("src.api.pivot_views.caller_has_role", AsyncMock(return_value=True)):
            resp = await client.patch(
                f"{BASE}/pivot-views/{other.id}", json={"name": "renamed"},
            )
        assert resp.status_code == 200
        assert other.name == "renamed"
        assert resp.json()["can_edit"] is True
        assert resp.json()["is_owner"] is False

    @pytest.mark.asyncio
    async def test_modeler_cannot_unshare_others_view(self, client):
        # Bug-5839 share boundary: a modeler may edit a shared view they do not
        # own, but publishing/unpublishing stays owner-only. Flipping another
        # user's shared view to personal would hide it from everyone — reject.
        other = _pivot(created_by="bob@acme", is_shared=True)
        db = _db_with(pivot=other)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)), \
             patch("src.api.pivot_views.caller_has_role", AsyncMock(return_value=True)):
            resp = await client.patch(
                f"{BASE}/pivot-views/{other.id}", json={"is_shared": False},
            )
        assert resp.status_code == 403
        # The stored flag is untouched.
        assert other.is_shared is True

    @pytest.mark.asyncio
    async def test_patch_empty_measure_id_still_validates_dims(self, client):
        # Bug-6412 / Bug-5316: clearing measure_id skips only the measure
        # existence check; the dimension refs are still validated.
        # L18 (Bug-8161/8182/7442): a config-VALIDATION failure like a malformed
        # dimension id is now a TYPED 422 carrying ``error_code`` (was a bare
        # 400) so the client can map it. The intent — dims are still validated
        # when the measure pointer is cleared — is unchanged.
        own = _pivot(created_by=USER_EMAIL, is_shared=False)
        db = _db_with(pivot=own)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)):
            resp = await client.patch(
                f"{BASE}/pivot-views/{own.id}",
                json={"measure_id": "", "row_dim_ids": ["not-a-uuid"]},
            )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "INVALID_DIMENSION_ID"

    @pytest.mark.asyncio
    async def test_owner_can_toggle_share_via_patch(self, client):
        own = _pivot(created_by=USER_EMAIL, is_shared=False)
        db = _db_with(pivot=own)
        with patch("src.api.pivot_views.get_tenant_db", async_gen_from(db)):
            resp = await client.patch(
                f"{BASE}/pivot-views/{own.id}", json={"is_shared": True},
            )
        assert resp.status_code == 200
        assert own.is_shared is True
        assert resp.json()["is_shared"] is True
