"""CRUD coverage for personas — Phase 8.B.1.

Locks the contract for:
  * POST   /projects/{pid}/models/{mid}/personas
  * GET    /projects/{pid}/models/{mid}/personas
  * GET    /projects/{pid}/models/{mid}/personas/{id}
  * PATCH  /projects/{pid}/models/{mid}/personas/{id}
  * DELETE /projects/{pid}/models/{mid}/personas/{id}
  * GET    /projects/{pid}/models/{mid}/personas/{id}/resolution

Empty-list semantics covered separately in
``test_personas_empty_list_semantics.py``.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/personas"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def __iter__(self):
        return iter(self._items)


def _persona(
    *,
    persona_id: uuid.UUID | None = None,
    name: str = "Sales",
    slug: str = "sales",
    description: str | None = None,
    included_measure_ids: list[str] | None = None,
    included_dimension_ids: list[str] | None = None,
    included_hierarchy_ids: list[str] | None = None,
    audience_roles: list[str] | None = None,
    default_filters: dict | None = None,
    includes_hidden_columns: bool = False,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=persona_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        slug=slug,
        description=description,
        included_measure_ids=included_measure_ids or [],
        included_dimension_ids=included_dimension_ids or [],
        included_hierarchy_ids=included_hierarchy_ids or [],
        audience_roles=audience_roles or [],
        default_filters=default_filters or {},
        bypass_row_security=False,
        includes_hidden_columns=includes_hidden_columns,
        created_at=NOW,
        updated_at=NOW,
    )


def _scripted_get(*, model, persona=None, measure=None):
    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Persona":
            return persona
        if name == "Measure":
            return measure
        return None

    return AsyncMock(side_effect=_get)


def _execute_script(*results):
    queue = list(results)

    async def _side(*_a, **_kw):
        if queue:
            return queue.pop(0)
        empty = MagicMock()
        empty.scalar_one_or_none.return_value = None
        empty.scalars.return_value.all.return_value = []
        return empty

    return AsyncMock(side_effect=_side)


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persona_round_trip(client):
    model = make_model()
    measure_id = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    # F-008-21: create now validates include ids against the model — the
    # measure-existence query must report the requested measure as present.
    db.execute = _execute_script(_ScalarResult([measure_id]))

    captured: dict = {}

    def _add(obj):
        captured["obj"] = obj
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.add = _add

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Sales",
                "slug": "sales",
                "description": "Sales analyst scope",
                "included_measure_ids": [str(measure_id)],
                "audience_roles": ["sales_analyst"],
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Sales"
    assert body["slug"] == "sales"
    assert body["included_measure_ids"] == [str(measure_id)]
    assert body["included_dimension_ids"] == []
    assert body["audience_roles"] == ["sales_analyst"]
    assert body["default_filters"] == {}


@pytest.mark.asyncio
async def test_create_persona_name_conflict_returns_409(client):
    from sqlalchemy.exc import IntegrityError

    model = make_model()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.commit = AsyncMock(side_effect=IntegrityError("dup", None, None))
    db.rollback = AsyncMock()

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={"name": "Sales", "slug": "sales"})

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["error_code"] == "PERSONA_NAME_CONFLICT"


# ---------------------------------------------------------------------------
# GET (list + detail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_personas(client):
    model = make_model()
    p1 = _persona(name="A", slug="a")
    p2 = _persona(name="B", slug="b")
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_script(_ScalarResult([p1, p2]))

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert {i["name"] for i in items} == {"A", "B"}


@pytest.mark.asyncio
async def test_list_personas_for_audience_filters(client):
    model = make_model()
    sales = _persona(name="Sales", slug="sales", audience_roles=["sales_analyst"])
    finance = _persona(name="Finance", slug="finance", audience_roles=["finance_analyst"])
    unrestricted = _persona(name="All", slug="all")
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_script(_ScalarResult([sales, finance, unrestricted]))

    from src.auth.middleware import CurrentUser, get_current_user
    from src.main import app

    sales_user = CurrentUser(
        user_id="alice",
        tenant_id="test-tenant",
        email="alice@example.com",
        role="sales_analyst",
    )
    app.dependency_overrides[get_current_user] = lambda: sales_user

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}?for_audience=true")

    app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    names = {i["name"] for i in resp.json()}
    assert names == {"Sales", "All"}


@pytest.mark.asyncio
async def test_get_persona_detail(client):
    model = make_model()
    p = _persona(name="Sales")
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{p.id}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "Sales"


@pytest.mark.asyncio
async def test_get_persona_404_when_other_model(client):
    other_model_id = uuid.uuid4()
    model = make_model()
    p = _persona()
    p.model_id = other_model_id
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{p.id}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_persona_replaces_include_lists(client):
    model = make_model()
    p = _persona(included_measure_ids=[str(uuid.uuid4())])
    new_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)
    # F-008-16 / F-008-21: the patch now validates the EFFECTIVE scope. Script
    # the measure-existence query (both new ids present) and the dimension-name
    # query (``year`` is a real dimension, so the default filter is valid);
    # the third query is the restricted-columns lookup in the response build.
    db.execute = _execute_script(
        _ScalarResult([uuid.UUID(i) for i in new_ids]),  # include-id existence
        _ScalarResult(["year"]),                          # dimension names
        _ScalarResult([]),                                # restricted columns
    )

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{p.id}",
            json={"included_measure_ids": new_ids, "default_filters": {"year": 2026}},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["included_measure_ids"] == new_ids
    assert body["default_filters"] == {"year": 2026}


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_persona(client):
    model = make_model()
    p = _persona()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{p.id}")

    assert resp.status_code == 204
    db.delete.assert_awaited_once_with(p)


# ---------------------------------------------------------------------------
# Resolution helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_allowed_when_measure_in_list(client):
    model = make_model()
    measure_id = uuid.uuid4()
    p = _persona(included_measure_ids=[str(measure_id)])
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"{PREFIX}/{p.id}/resolution?measure_id={measure_id}"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["measure_allowed"] is True


@pytest.mark.asyncio
async def test_resolution_denied_when_measure_not_in_populated_list(client):
    model = make_model()
    in_list = uuid.uuid4()
    out_of_list = uuid.uuid4()
    p = _persona(included_measure_ids=[str(in_list)])
    measure = types.SimpleNamespace(id=out_of_list, name="gl_expense_sum")
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p, measure=measure)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"{PREFIX}/{p.id}/resolution?measure_id={out_of_list}"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["measure_allowed"] is False
    assert "gl_expense_sum" in (body["reason"] or "")


# ---------------------------------------------------------------------------
# Cascade helper — strip_id_from_personas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strip_id_from_personas_removes_target():
    from src.api.personas import strip_id_from_personas

    model_id = TEST_MODEL_ID
    target = uuid.uuid4()
    other = uuid.uuid4()
    p1 = _persona(name="A", included_measure_ids=[str(target), str(other)])
    p2 = _persona(name="B", included_measure_ids=[str(other)])
    db = make_mock_db()
    db.execute = _execute_script(_ScalarResult([p1, p2]))

    touched = await strip_id_from_personas(
        db, model_id=model_id, object_id=target, object_class="measure"
    )

    assert touched == ["A"]
    assert p1.included_measure_ids == [str(other)]
    assert p2.included_measure_ids == [str(other)]


# ---------------------------------------------------------------------------
# F-008-05 residual — persona responses expose restricted_column_ids so the
# gateway can drop restricted column names from persona catalogue metadata
# ---------------------------------------------------------------------------


class _RowsResult:
    """Mock for queries consumed via ``result.all()`` returning row tuples."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


@pytest.mark.asyncio
async def test_list_personas_includes_restricted_column_ids(client):
    model = make_model()
    persona = _persona(name="Partner", slug="partner")
    restricted_col = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_script(
        _ScalarResult([persona]),                    # personas select
        _RowsResult([(persona.id, restricted_col)]),  # restriction join
    )

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["restricted_column_ids"] == [str(restricted_col)]


@pytest.mark.asyncio
async def test_get_persona_includes_restricted_column_ids(client):
    model = make_model()
    persona = _persona(name="Partner", slug="partner")
    restricted_col = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=persona)
    db.execute = _execute_script(
        _RowsResult([(persona.id, restricted_col)]),  # restriction join
    )

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{persona.id}")

    assert resp.status_code == 200
    assert resp.json()["restricted_column_ids"] == [str(restricted_col)]
