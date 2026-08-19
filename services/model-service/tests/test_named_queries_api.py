"""API tests for the Named Query authoring routes (model-service).

Key behaviours:
  - create validates through the router + derives output_columns/shape (201)
  - validation failure, DML, @ references and column-cap overages are 400s
  - the @ namespace is shared with parameters and named sets (409)
  - patch re-validates and stales the artifact
  - delete cascades + evicts the router cache
  - refresh queues a background run (202); policy upsert
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    NOW,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/named-queries"

_VALID_OK = {"ok": True, "errors": []}
_VALID_FAIL = {"ok": False, "errors": ["Unknown column: bogus_col"]}


class _Scalars:
    """The ScalarResult half returned by ``Result.scalars()``.

    Kept distinct from the Result so the fake honours the real SQLAlchemy
    contract the Bug-8924 guard pins (a fake must not return itself from
    ``scalars()``, which silently collapses Result and ScalarResult).
    """

    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None

    def one_or_none(self):
        return self._items[0] if self._items else None


class _ScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return _Scalars(self._items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalar_one(self):
        return self._items[0]

    def one_or_none(self):
        return self._items[0] if self._items else None


_EMPTY = _ScalarResult([])


def _make_nq_response_obj(nq_id: uuid.UUID, *, artifact=None, policy=None):
    return types.SimpleNamespace(
        id=nq_id,
        model_id=TEST_MODEL_ID,
        name="TopCities",
        display_name=None,
        description=None,
        display_folder=None,
        definition_sql='SELECT * FROM "test-model" WHERE branch_id = \'1\'',
        output_columns=[{"name": "*", "type": "string"}],
        shape="projection",
        row_cap=None,
        column_cap=None,
        certification_status="draft",
        created_by="test@example.com",
        artifact=artifact,
        refresh_policy_row=policy,
        created_at=NOW,
        updated_at=NOW,
    )


def _make_artifact_obj(*, status="stale"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        target_id=uuid.uuid4(),
        physical_table_name="nq_abc123_abcd",
        target_schema="public",
        row_count=None,
        status=status,
        failure_reason=None,
        last_refresh_at=None,
        retired_at=None,
    )


def _make_execute_script(*results):
    """Statement-agnostic script: yields each result in order, then empty."""
    iterator = iter(list(results))

    async def _side(*_a, **_kw):
        try:
            return next(iterator)
        except StopIteration:
            return _EMPTY

    return AsyncMock(side_effect=_side)


def _make_db(*, nq_obj=None):
    """A mock session with: get side-effect (model / named query), add that
    assigns ids (so response validation sees a real UUID), and an execute
    script."""
    db = make_mock_db()
    added: list = []

    def _add(obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        added.append(obj)

    db.add = MagicMock(side_effect=_add)

    async def _refresh(obj):
        # Server defaults are filled by the DB on flush + refresh; the mock
        # session refreshes nothing, so stamp the missing timestamps here.
        for field in ("started_at", "created_at", "updated_at"):
            if hasattr(obj, field) and getattr(obj, field, None) is None:
                setattr(obj, field, NOW)

    db.refresh = AsyncMock(side_effect=_refresh)

    async def _get(cls, pk):
        if cls.__name__ == "Model":
            return make_model()
        if cls.__name__ == "NamedQuery":
            return nq_obj if nq_obj is not None else _make_nq_response_obj(pk)
        return None

    db.get = AsyncMock(side_effect=_get)
    db.execute = _make_execute_script()
    return db


@pytest.fixture(autouse=True)
def _patch_router_and_maps():
    with (
        patch(
            "src.api.named_queries._validate_via_router",
            new=AsyncMock(return_value=_VALID_OK),
        ),
        patch(
            "src.api.named_queries._model_field_maps",
            new=AsyncMock(return_value=({"branch_id": "string"}, set())),
        ),
        patch(
            "src.api.named_queries.get_setting",
            new=AsyncMock(return_value=200),
        ),
        patch(
            "src.api.named_queries._evict_query_router_cache",
            new=AsyncMock(),
        ),
    ):
        yield


def _auth_headers(role: str = "admin") -> dict:
    return {"Authorization": f"Bearer test-token-{role}"}


def _patch_tenant_db(monkeypatch, db):
    from .conftest import async_gen_from

    monkeypatch.setattr(
        "src.api.named_queries.get_tenant_db", async_gen_from(db)
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def test_create_named_query_success(client, monkeypatch) -> None:
    reload_obj = _make_nq_response_obj(uuid.uuid4())
    db = _make_db()
    # model-lock, 3 namespace checks (empty), then the post-commit reload.
    db.execute = _make_execute_script(
        _EMPTY, _EMPTY, _EMPTY, _EMPTY, _ScalarResult([reload_obj])
    )
    _patch_tenant_db(monkeypatch, db)

    payload = {
        "name": "TopCities",
        "definition_sql": 'SELECT * FROM "test-model" WHERE branch_id = \'1\'',
    }
    resp = await client.post(PREFIX, json=payload, headers=_auth_headers())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "TopCities"
    assert body["shape"] == "projection"
    assert body["artifact"] is None


async def test_create_validation_failure_400(client, monkeypatch) -> None:
    db = _make_db()
    _patch_tenant_db(monkeypatch, db)
    with patch(
        "src.api.named_queries._validate_via_router",
        new=AsyncMock(return_value=_VALID_FAIL),
    ):
        resp = await client.post(
            PREFIX,
            json={"name": "Bad", "definition_sql": 'SELECT bogus_col FROM "test-model"'},
            headers=_auth_headers(),
        )
    assert resp.status_code == 400
    assert "validation" in resp.json()["detail"]["message"].lower()


async def test_create_dml_definition_rejected(client, monkeypatch) -> None:
    db = _make_db()
    _patch_tenant_db(monkeypatch, db)
    resp = await client.post(
        PREFIX,
        json={"name": "Bad", "definition_sql": 'DELETE FROM "test-model"'},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "forbidden keyword" in resp.json()["detail"]["errors"][0]


async def test_create_at_reference_in_definition_rejected(client, monkeypatch) -> None:
    db = _make_db()
    _patch_tenant_db(monkeypatch, db)
    resp = await client.post(
        PREFIX,
        json={"name": "Bad", "definition_sql": "SELECT * FROM @other"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    assert "@ placeholders" in resp.json()["detail"]["errors"][0]


async def test_create_name_collision_with_parameter_409(client, monkeypatch) -> None:
    db = _make_db()
    param_row = _ScalarResult([(uuid.uuid4(), "@TopCities")])
    # model-lock then the parameter namespace check returns the row.
    db.execute = _make_execute_script(_EMPTY, param_row)
    _patch_tenant_db(monkeypatch, db)
    resp = await client.post(
        PREFIX,
        json={"name": "TopCities", "definition_sql": 'SELECT * FROM "test-model"'},
        headers=_auth_headers(),
    )
    assert resp.status_code == 409
    assert "model parameter" in resp.json()["detail"]


async def test_create_name_collision_with_named_set_409(client, monkeypatch) -> None:
    db = _make_db()
    ns_row = _ScalarResult([(uuid.uuid4(), "TopCities")])
    # model-lock, empty parameter check, then the named-set check hits.
    db.execute = _make_execute_script(_EMPTY, _EMPTY, ns_row)
    _patch_tenant_db(monkeypatch, db)
    resp = await client.post(
        PREFIX,
        json={"name": "TopCities", "definition_sql": 'SELECT * FROM "test-model"'},
        headers=_auth_headers(),
    )
    assert resp.status_code == 409
    assert "named set" in resp.json()["detail"]


async def test_create_column_cap_rejected(client, monkeypatch) -> None:
    db = _make_db()
    _patch_tenant_db(monkeypatch, db)
    with patch(
        "src.api.named_queries.get_setting",
        new=AsyncMock(return_value=2),
    ):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Wide",
                "definition_sql": 'SELECT a, b, c FROM "test-model"',
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 400
    assert "max_columns" in resp.json()["detail"]["errors"][0]


# ---------------------------------------------------------------------------
# Patch / delete / policy / runs
# ---------------------------------------------------------------------------

async def test_update_definition_stales_artifact(client, monkeypatch) -> None:
    artifact = _make_artifact_obj(status="fresh")
    nq_obj = _make_nq_response_obj(uuid.uuid4(), artifact=artifact)
    db = _make_db(nq_obj=nq_obj)
    # 1st execute = the NQ lookup; 2nd = the model-definition lock; 3rd = the
    # post-commit reload.
    db.execute = _make_execute_script(
        _ScalarResult([nq_obj]), _EMPTY, _ScalarResult([nq_obj])
    )
    _patch_tenant_db(monkeypatch, db)
    resp = await client.patch(
        f"{PREFIX}/{nq_obj.id}",
        json={"definition_sql": 'SELECT * FROM "test-model" WHERE branch_id = \'2\''},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert artifact.status == "stale"


async def test_delete_named_query_204(client, monkeypatch) -> None:
    db = _make_db()
    # 1st execute = the NQ lookup; 2nd = the model-definition lock.
    db.execute = _make_execute_script(
        _ScalarResult([_make_nq_response_obj(uuid.uuid4())]), _EMPTY
    )
    _patch_tenant_db(monkeypatch, db)
    resp = await client.delete(f"{PREFIX}/{uuid.uuid4()}", headers=_auth_headers())
    assert resp.status_code == 204


async def test_refresh_queues_background_run(client, monkeypatch) -> None:
    db = _make_db()
    _patch_tenant_db(monkeypatch, db)
    with patch(
        "src.api.named_queries._run_named_query_refresh_in_background",
        new=MagicMock(),
    ):
        resp = await client.post(
            f"{PREFIX}/{uuid.uuid4()}/refresh", headers=_auth_headers()
        )
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"


async def test_upsert_refresh_policy(client, monkeypatch) -> None:
    db = _make_db()
    # model-lock, then the policy SELECT (empty -> create).
    db.execute = _make_execute_script(_EMPTY, _EMPTY)
    _patch_tenant_db(monkeypatch, db)
    resp = await client.put(
        f"{PREFIX}/{uuid.uuid4()}/refresh/policy",
        json={"cron_expression": "0 * * * *", "is_enabled": True},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["is_enabled"] is True
