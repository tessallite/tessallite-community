"""ML14 — persona scope validation & resolution explainer (F-008-16/21/22).

These lock the fail-closed save-time validation added in ML14:
  * slug pattern rejected server-side (F-008-21);
  * foreign include ids rejected (F-008-21);
  * malformed / unknown-dimension default_filters rejected (F-008-16);
  * the resolution endpoint explains dimensions/hierarchies/tags, not just
    measures (F-008-22).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)
from .test_personas_crud import _ScalarResult, _persona, _scripted_get

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/personas"


def _execute_queue(*results):
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
# F-008-21 — slug pattern enforced server-side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persona_rejects_bad_slug(client):
    model = make_model()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX, json={"name": "Bad", "slug": "Has Spaces"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# F-008-21 — foreign include ids rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persona_rejects_foreign_measure_id(client):
    model = make_model()
    foreign = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    # The measure-existence query returns an empty set → the id is foreign.
    db.execute = _execute_queue(_ScalarResult([]))
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Sales", "slug": "sales",
                "included_measure_ids": [str(foreign)],
            },
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "PERSONA_INCLUDE_NOT_IN_MODEL"


# ---------------------------------------------------------------------------
# F-008-16 — default_filters validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persona_rejects_unknown_filter_dimension(client):
    model = make_model()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    # No include ids → first query is the dimension-name lookup; ``region``
    # is not among the model's dimensions.
    db.execute = _execute_queue(_ScalarResult(["country"]))
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Sales", "slug": "sales",
                "default_filters": {"region": "EMEA"},
            },
        )
    assert resp.status_code == 422
    assert (
        resp.json()["detail"]["error_code"]
        == "PERSONA_DEFAULT_FILTER_UNKNOWN_DIMENSION"
    )


@pytest.mark.asyncio
async def test_create_persona_rejects_between_arity(client):
    model = make_model()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_queue(_ScalarResult(["amount"]))
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Sales", "slug": "sales",
                "default_filters": {"amount": {"between": [1]}},  # arity 1
            },
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "PERSONA_DEFAULT_FILTER_INVALID"


@pytest.mark.asyncio
async def test_create_persona_rejects_unsupported_operator(client):
    model = make_model()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_queue(_ScalarResult(["amount"]))
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Sales", "slug": "sales",
                "default_filters": {"amount": {"foo": 5}},
            },
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "PERSONA_DEFAULT_FILTER_INVALID"


@pytest.mark.asyncio
async def test_create_persona_accepts_valid_filter(client):
    model = make_model()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    # dimension-name query reports ``amount`` exists; then create commits.
    db.execute = _execute_queue(_ScalarResult(["amount"]))

    from .conftest import NOW

    def _add(obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.add = MagicMock(side_effect=_add)

    async def _refresh(obj):
        return None

    db.refresh = _refresh
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Sales", "slug": "sales",
                "default_filters": {"amount": {"gte": 100}},
            },
        )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# F-008-22 — resolution explainer covers all kinds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_requires_exactly_one_object(client):
    model = make_model()
    p = _persona()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{p.id}/resolution")  # none supplied
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "PERSONA_RESOLUTION_BAD_REQUEST"


@pytest.mark.asyncio
async def test_resolution_dimension_denied(client):
    model = make_model()
    allowed = uuid.uuid4()
    other = uuid.uuid4()
    p = _persona(included_dimension_ids=[str(allowed)])
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"{PREFIX}/{p.id}/resolution?dimension_id={other}",
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object_kind"] == "dimension"
    assert body["allowed"] is False


@pytest.mark.asyncio
async def test_resolution_tag_restricted_is_denied(client):
    model = make_model()
    p = _persona()
    restricted_tag = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)
    # The tag-restriction lookup returns the restricted tag id.
    db.execute = _execute_queue(_ScalarResult([restricted_tag]))
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"{PREFIX}/{p.id}/resolution?tag_id={restricted_tag}",
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object_kind"] == "tag"
    assert body["allowed"] is False


@pytest.mark.asyncio
async def test_resolution_measure_legacy_fields_preserved(client):
    model = make_model()
    allowed = uuid.uuid4()
    p = _persona(included_measure_ids=[str(allowed)])
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)
    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"{PREFIX}/{p.id}/resolution?measure_id={allowed}",
        )
    assert resp.status_code == 200
    body = resp.json()
    # Backward-compatible measure fields still populated for the frontend.
    assert body["measure_allowed"] is True
    assert body["allowed"] is True
    assert body["object_kind"] == "measure"
