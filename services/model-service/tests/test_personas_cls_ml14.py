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
                "audience_roles": ["sales_analyst"],
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


def test_yaml_import_narrowing_persona_validated():
    """Bug-9266 / F-008-03: YAML import of an allow-listed audience-less
    persona is refused. After explicit-audience assignment the allow-list
    would otherwise never apply (unrestricted model).
    """
    from shared.model_snapshot.yaml_deserialiser import (
        YamlImportError,
        parse_model_yaml,
    )

    yaml_doc = """
model:
  name: sales
  display_name: Sales
tables:
  - name: orders
    source_table: public.orders
    columns:
      - name: amount
        type: number
measures:
  - name: revenue
    table: orders
    column: amount
    aggregation: sum
personas:
  - name: regional
    allowed_measures: [revenue]
"""
    with pytest.raises(YamlImportError) as exc:
        parse_model_yaml(yaml_doc)
    joined = " ".join(exc.value.errors).lower()
    assert "regional" in joined
    assert "audience" in joined


def test_yaml_import_narrowing_persona_with_audience_is_accepted():
    """Bug-9266: snapshot/YAML round-trips that already name audience_roles
    still import; filter-only empty everything stays allowed without roles.
    """
    from shared.model_snapshot.yaml_deserialiser import parse_model_yaml

    yaml_doc = """
model:
  name: sales
  display_name: Sales
tables:
  - name: orders
    source_table: public.orders
    columns:
      - name: amount
        type: number
measures:
  - name: revenue
    table: orders
    column: amount
    aggregation: sum
personas:
  - name: regional
    allowed_measures: [revenue]
    audience_roles: [analyst]
  - name: everyone
"""
    snap = parse_model_yaml(yaml_doc)
    by_slug = {p["slug"]: p for p in snap["personas"]}
    assert by_slug["regional"]["audience_roles"] == ["analyst"]
    assert by_slug["regional"]["included_measure_ids"]
    assert by_slug["everyone"]["audience_roles"] == []
    assert not by_slug["everyone"]["included_measure_ids"]


@pytest.mark.asyncio
async def test_cls_base_reuse_gives_identical_block_sets():
    """personas.py N-scan -> single-load refactor must not change any CLS
    authorization decision: the per-call load path (base=None) and the
    request-level base-reuse path must return identical blocked measure/dimension
    sets for the same underlying data (Bucket D / CodeRabbit CLS closure)."""
    from types import SimpleNamespace

    from src.api.personas import (
        ClsClosureBase,
        _cls_blocked_object_ids,
    )

    col_id = uuid.uuid4()
    other_id = uuid.uuid4()
    m = SimpleNamespace(
        id=uuid.uuid4(), name="revenue",
        source_column_id=col_id, display_column_id=None,
    )
    d = SimpleNamespace(
        id=uuid.uuid4(), name="region",
        source_column_id=other_id, display_column_id=None,
    )
    col_rows = [(col_id, "salary", "employees"), (other_id, "region", "dim_region")]

    def _measures_res():
        r = MagicMock(); r.scalars.return_value.all.return_value = [m]; return r

    def _dims_res():
        r = MagicMock(); r.scalars.return_value.all.return_value = [d]; return r

    def _cols_res():
        r = MagicMock(); r.all.return_value = col_rows; return r

    def _uda_res():
        r = MagicMock(); r.all.return_value = []; return r

    restricted = [col_id]

    # Path A: base=None loads measures, dims, cols, uda in order.
    db_a = make_mock_db()
    db_a.execute = _execute_queue(_measures_res(), _dims_res(), _cols_res(), _uda_res())
    a_m, a_d = await _cls_blocked_object_ids(db_a, TEST_MODEL_ID, restricted, base=None)

    # Path B: a pre-loaded base runs only the scoped uda query.
    base = ClsClosureBase([m], [d], list(col_rows))
    db_b = make_mock_db()
    db_b.execute = _execute_queue(_uda_res())
    b_m, b_d = await _cls_blocked_object_ids(db_b, TEST_MODEL_ID, restricted, base=base)

    assert (a_m, a_d) == (b_m, b_d)
    assert a_m == [m.id] and a_d == []
