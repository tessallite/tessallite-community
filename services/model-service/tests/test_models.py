"""
Unit tests for model CRUD routes.

GET    /api/v1/projects/{project_id}/models
GET    /api/v1/projects/{project_id}/models/{model_id}
POST   /api/v1/projects/{project_id}/models
PATCH  /api/v1/projects/{project_id}/models/{model_id}
DELETE /api/v1/projects/{project_id}/models/{model_id}

Key invariant: `seed` must be auto-generated (12 hex chars) — never supplied by caller.
"""
from __future__ import annotations

import re
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    client,
    make_mock_db,
    make_model,
    async_gen_from,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

class _ScalarResult:
    """Stand-in for ``sqlalchemy.engine.Result`` that the route handlers
    consume. Supports the three method shapes the production code uses:
    ``.scalars().all()``, ``.scalar_one_or_none()``, and ``.all()``."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def first(self):
        # Existence probe used by resolve_listable_model_scope / bootstrap path.
        return self._items[0] if self._items else None


_EMPTY_RESULT = _ScalarResult([])


async def _noop_trust_meta(_db, _model):
    """Patched in place of ``_build_trust_meta`` so tests that only care
    about CRUD behaviour don't have to mock the aggregate + source
    queries the trust helper issues."""
    return {"last_refreshed_at": None, "source_system": None, "owner": ""}


# ---------------------------------------------------------------------------
# GET — list models
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_models(client):
    model = make_model()
    model.deployed_version_id = None
    mock_db = make_mock_db()
    # Bug-8101 follow-up: list_models authorizes + filters via
    # resolve_listable_model_scope first. F-021-04 (decision #9) removed the
    # zero-binding bootstrap path, so the caller must hold a binding. A
    # PROJECT-WIDE binding (model_id=None) grants visibility of ALL models, so
    # resolve makes ONE query (user bindings) and returns "see all".
    # F-013-12: batched list decoration follows. Query order:
    #   1. resolve: user bindings load -> rows of (model_id,) [(None,) = project-wide -> all]
    #   2. list models
    #   3. grouped last_saved version per model  -> rows of (model_id, n)
    #   4. grouped latest refresh per model       -> rows of (model_id, ts)
    #   5. first DataSource per model              -> scalars
    # (the deployed-pointer lookup is skipped because no model has a pointer)
    mock_db.execute = AsyncMock(side_effect=[
        _ScalarResult([(None,)]),  # resolve: caller has a project-wide binding -> sees all
        _ScalarResult([model]),
        _ScalarResult([]),   # no saved versions
        _ScalarResult([]),   # no refreshed aggregates
        _ScalarResult([]),   # no data sources
    ])

    with patch("src.api.models.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["slug"] == "test-model"


# ---------------------------------------------------------------------------
# GET — single model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_model_found(client):
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models._build_trust_meta", new=_noop_trust_meta),
    ):
        resp = await client.get(f"{PREFIX}/{TEST_MODEL_ID}")

    assert resp.status_code == 200
    assert resp.json()["id"] == str(TEST_MODEL_ID)


@pytest.mark.asyncio
async def test_get_model_wrong_project(client):
    """Model exists but belongs to a different project → 404."""
    model = make_model(project_id=uuid.uuid4())  # different project
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)

    with patch("src.api.models.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.get(f"{PREFIX}/{TEST_MODEL_ID}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST — create model (seed auto-generated)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_model_seed_is_generated(client):
    """seed must be a 12-char hex string auto-generated by the route — not from body."""
    captured = {}
    mock_db = make_mock_db()

    async def _refresh(obj):
        captured["seed"] = obj.seed
        obj.id = TEST_MODEL_ID
        obj.created_at = obj.updated_at = NOW
        obj.description = None
        obj.target_id = None
        obj.status = "active"
        obj.refresh_strategy = "scheduled"
        obj.miss_threshold_daily = 3
        obj.miss_threshold_weekly = 5
        obj.schema_drift_interval_hours = 24

    mock_db.refresh = _refresh

    with patch("src.api.models.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(
            PREFIX,
            # BI-safe slug: hyphens are rejected (BI clients parse them as
            # operators) — underscores only.
            json={"slug": "my_model", "display_name": "My Model"},
        )

    assert resp.status_code == 201
    seed = captured["seed"]
    # 12-char lowercase hex
    assert re.fullmatch(r"[0-9a-f]{12}", seed), f"seed={seed!r} is not 12 hex chars"


@pytest.mark.asyncio
async def test_create_model_seed_unique_across_calls(client):
    """Two consecutive creates must produce different seeds."""
    seeds = []
    mock_db = make_mock_db()

    async def _refresh(obj):
        seeds.append(obj.seed)
        obj.id = uuid.uuid4()
        obj.created_at = obj.updated_at = NOW
        obj.description = None
        obj.target_id = None
        obj.status = "active"
        obj.refresh_strategy = "scheduled"
        obj.miss_threshold_daily = 3
        obj.miss_threshold_weekly = 5
        obj.schema_drift_interval_hours = 24

    mock_db.refresh = _refresh

    with patch("src.api.models.get_tenant_db", async_gen_from(mock_db)):
        await client.post(PREFIX, json={"slug": "m1", "display_name": "M1"})
        await client.post(PREFIX, json={"slug": "m2", "display_name": "M2"})

    assert len(seeds) == 2
    assert seeds[0] != seeds[1], "seeds must be unique per model"


@pytest.mark.asyncio
async def test_create_model_seeds_technical_persona(client):
    """Bug-6138: creating a model must seed the canonical Technical persona in
    the SAME transaction (before commit), so the hidden-columns technical
    catalog is live for new models — not only for models that predate the
    seeding migration. Guards the endpoint boundary: if the seed call is
    removed from create_model, this fails."""
    mock_db = make_mock_db()

    async def _refresh(obj):
        obj.id = TEST_MODEL_ID
        obj.created_at = obj.updated_at = NOW
        obj.description = None
        obj.target_id = None
        obj.status = "active"
        obj.refresh_strategy = "scheduled"
        obj.miss_threshold_daily = 3
        obj.miss_threshold_weekly = 5
        obj.schema_drift_interval_hours = 24

    mock_db.refresh = _refresh
    seed_mock = AsyncMock()

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models.seed_technical_persona", seed_mock),
    ):
        resp = await client.post(
            PREFIX, json={"slug": "my_model", "display_name": "My Model"}
        )

    assert resp.status_code == 201
    seed_mock.assert_awaited_once()
    # Seeded against the tenant session, before the transaction commits.
    assert seed_mock.await_args.args[0] is mock_db
    assert mock_db.commit.await_count >= 1


# ---------------------------------------------------------------------------
# PATCH — update model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_model_display_name(client):
    model = make_model(display_name="Old")
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.refresh = AsyncMock()

    with patch("src.api.models.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{TEST_MODEL_ID}",
            json={"display_name": "New"},
        )

    assert resp.status_code == 200
    assert resp.json()["display_name"] == "New"


# ---------------------------------------------------------------------------
# Bug-9409 — include_all_measures is a recorded aggregate-shape decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_include_all_measures_change_is_recorded_with_its_transition(client):
    """A flag change alters what every future aggregate materialises, so the
    audit entry must name the transition — not just list the field.

    The lifecycle it triggers is the EXISTING one, on the optimizer's next
    sweep: ``backfill_include_all_measures`` widens active aggregates on
    OFF->ON, and new builds narrow to the requested measures on ON->OFF. The
    PATCH itself is deliberately non-destructive.
    """
    model = make_model()
    model.include_all_measures = False
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.refresh = AsyncMock()

    recorded: dict = {}

    async def _audit(_db, **kwargs):
        recorded.update(kwargs)

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models.audit", new=AsyncMock(side_effect=_audit)),
    ):
        resp = await client.patch(
            f"{PREFIX}/{TEST_MODEL_ID}",
            json={"include_all_measures": True},
        )

    assert resp.status_code == 200
    assert model.include_all_measures is True
    detail = recorded.get("detail") or {}
    assert detail.get("include_all_measures") == {
        "from": False,
        "to": True,
        "aggregate_lifecycle": "backfill_widens_active_aggregates_on_next_sweep",
    }


@pytest.mark.asyncio
async def test_an_unchanged_include_all_measures_value_records_no_transition(client):
    """Re-sending the same value is not a change and must not claim one."""
    model = make_model()
    model.include_all_measures = True
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.refresh = AsyncMock()

    recorded: dict = {}

    async def _audit(_db, **kwargs):
        recorded.update(kwargs)

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models.audit", new=AsyncMock(side_effect=_audit)),
    ):
        resp = await client.patch(
            f"{PREFIX}/{TEST_MODEL_ID}",
            json={"include_all_measures": True},
        )

    assert resp.status_code == 200
    assert "include_all_measures" not in (recorded.get("detail") or {})


@pytest.mark.asyncio
async def test_a_patch_that_omits_the_flag_never_rewrites_it(client):
    """The PRESERVATION half of the decision, at the API boundary: an unrelated
    edit must not carry the new default onto an existing model."""
    model = make_model()
    model.include_all_measures = True
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.refresh = AsyncMock()

    with patch("src.api.models.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.patch(
            f"{PREFIX}/{TEST_MODEL_ID}",
            json={"display_name": "Renamed"},
        )

    assert resp.status_code == 200
    assert model.include_all_measures is True, (
        "an existing model's persisted opt-in must survive an unrelated edit"
    )


# ---------------------------------------------------------------------------
# DELETE — model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_model(client):
    model = make_model()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)

    with (
        patch("src.api.models.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.models.delete_model_cascade", new_callable=AsyncMock, return_value=[]) as mock_cascade,
    ):
        resp = await client.delete(f"{PREFIX}/{TEST_MODEL_ID}")

    assert resp.status_code == 204
    mock_cascade.assert_called_once_with(mock_db, TEST_MODEL_ID)


@pytest.mark.asyncio
async def test_delete_model_not_found(client):
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=None)

    with patch("src.api.models.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.delete(f"{PREFIX}/{uuid.uuid4()}")

    assert resp.status_code == 404
