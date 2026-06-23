"""Tests for Named Set CRUD, validation, and preview endpoints."""
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
    make_mock_db,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/named-sets"


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


def _named_set(
    *,
    ns_id: uuid.UUID | None = None,
    name: str = "Test Set",
    expression: str = "{ [Dim].[Hier].&[A] }",
    list_type: str = "advanced_mdx",
    builder_definition: dict | None = None,
    certification_status: str = "draft",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=ns_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        scope=1,
        expression=expression,
        dimensions=None,
        builder_definition=builder_definition,
        list_type=list_type,
        certification_status=certification_status,
        owner_user_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _model(project_id: uuid.UUID | None = None) -> types.SimpleNamespace:
    """Model row returned by the F-018-04 project-scope check."""
    return types.SimpleNamespace(
        id=TEST_MODEL_ID,
        project_id=project_id or TEST_PROJECT_ID,
        slug="test-model",
    )


def _dimension(name: str = "Customer") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, name=name,
    )


def _measure(name: str = "Revenue") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, name=name,
    )


# ---- CRUD Tests ----

@pytest.mark.asyncio
async def test_list_named_sets(client):
    ns = _named_set()
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    db.execute = AsyncMock(return_value=_ScalarResult([ns]))
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Test Set"
    assert data[0]["certification_status"] == "draft"


@pytest.mark.asyncio
async def test_create_named_set_with_expression(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    ns = _named_set()

    async def mock_refresh(obj):
        for attr, val in vars(ns).items():
            setattr(obj, attr, val)

    db.refresh = mock_refresh
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Test Set",
            "expression": "{ [Dim].[Hier].&[A] }",
        })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_named_set_with_builder_definition(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    ns = _named_set(
        builder_definition={"type": "fixedMembers", "dimension": "Product", "members": ["A"]},
        expression="{ [Product].[Product].&[A] }",
        list_type="fixed",
    )

    async def mock_refresh(obj):
        for attr, val in vars(ns).items():
            setattr(obj, attr, val)

    db.refresh = mock_refresh
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Test Set",
            "builder_definition": {"type": "fixedMembers", "dimension": "Product", "members": ["A"]},
            "list_type": "fixed",
        })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_named_set_no_expression_no_builder_fails(client):
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad Set",
        })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_named_set_recompiles_builder(client):
    ns = _named_set()
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    async def mock_refresh(obj):
        pass

    db.refresh = mock_refresh
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(f"{PREFIX}/{ns.id}", json={
            "builder_definition": {"type": "topN", "entity": "Customer", "count": 5, "measure": "Revenue", "direction": "top"},
        })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_named_set(client):
    ns = _named_set()
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{ns.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_named_set_not_found(client):
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), None])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---- Validation Tests ----

@pytest.mark.asyncio
async def test_validate_with_expression(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    dims_result = _ScalarResult([_dimension()])
    meas_result = _ScalarResult([_measure()])
    db.execute = AsyncMock(side_effect=[dims_result, meas_result])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/validate", json={
            "expression": "{ [Customer].[Customer].&[ACME] }",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is True
    assert data["estimated_cost_band"] == "low"


@pytest.mark.asyncio
async def test_validate_with_builder_definition(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    dims_result = _ScalarResult([_dimension()])
    meas_result = _ScalarResult([_measure()])
    db.execute = AsyncMock(side_effect=[dims_result, meas_result])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/validate", json={
            "builder_definition": {
                "type": "topN",
                "entity": "Customer",
                "count": 10,
                "measure": "Revenue",
                "direction": "top",
            },
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is True
    assert data["compiled_expression"] is not None
    assert "TopCount" in data["compiled_expression"]
    assert data["explanation"] is not None
    assert data["estimated_cost_band"] == "medium"


@pytest.mark.asyncio
async def test_validate_invalid_builder(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    dims_result = _ScalarResult([])
    meas_result = _ScalarResult([])
    db.execute = AsyncMock(side_effect=[dims_result, meas_result])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/validate", json={
            "builder_definition": {"type": "topN"},
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is False
    assert len(data["errors"]) > 0


@pytest.mark.asyncio
async def test_validate_empty_returns_error(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    dims_result = _ScalarResult([])
    meas_result = _ScalarResult([])
    db.execute = AsyncMock(side_effect=[dims_result, meas_result])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/validate", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is False


@pytest.mark.asyncio
async def test_validate_blocked_function(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    dims_result = _ScalarResult([])
    meas_result = _ScalarResult([])
    db.execute = AsyncMock(side_effect=[dims_result, meas_result])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/validate", json={
            "expression": "DRILLDOWNLEVEL([Geography].[City])",
        })
    data = resp.json()
    assert data["is_valid"] is False
    assert any("DRILLDOWNLEVEL" in e for e in data["errors"])


@pytest.mark.asyncio
async def test_validate_high_cost_expression(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    dims_result = _ScalarResult([])
    meas_result = _ScalarResult([])
    db.execute = AsyncMock(side_effect=[dims_result, meas_result])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/validate", json={
            "expression": "CROSSJOIN([A].Members, [B].Members)",
        })
    data = resp.json()
    assert data["estimated_cost_band"] == "high"


# ---- Preview Tests ----

@pytest.mark.asyncio
async def test_preview_named_set(client):
    ns = _named_set(builder_definition={"type": "fixedMembers", "members": ["Member A", "Member B"]})
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{ns.id}/preview")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["caption"] == "Member A"
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_preview_not_found(client):
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), None])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{uuid.uuid4()}/preview")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_router_failure_returns_empty(client):
    ns = _named_set()
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.named_sets._execute_via_router", AsyncMock(side_effect=ValueError("Router error"))):
        resp = await client.post(f"{PREFIX}/{ns.id}/preview")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 0


# ---- Project scope tests (F-018-04) ----

@pytest.mark.asyncio
async def test_list_named_sets_cross_project_model_404(client):
    """A model that belongs to another project must 404, not leak its sets."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model(project_id=uuid.uuid4()))
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_create_named_set_cross_project_model_404(client):
    """Creating a set on another project's model must 404 (RBAC bypass)."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model(project_id=uuid.uuid4()))
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Cross Set",
            "expression": "{ [Dim].[Hier].&[A] }",
        })
    assert resp.status_code == 404
    assert db.add.call_count == 0


@pytest.mark.asyncio
async def test_delete_named_set_cross_project_model_404(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model(project_id=uuid.uuid4()))
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{uuid.uuid4()}")
    assert resp.status_code == 404
    db.delete.assert_not_called()


# ---- F-018-02: preview membership must match compiled MDX ----

class _OneResult:
    """A query result that supports both scalar_one_or_none() and all()."""
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return list(self._rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None


@pytest.mark.asyncio
async def test_filter_preview_uses_aggregate_having_matching_compiled_mdx(client):
    """A measure-threshold filter must preview with GROUP BY ... HAVING over
    the aggregated measure — the same membership the compiled MDX `Filter`
    produces — not a row-level WHERE that drops the condition (F-018-02)."""
    from shared.named_list_compiler import compile_definition

    bd = {
        "type": "filter",
        "entity": "customer",
        "conditions": [{"field": "total_sales", "operator": ">", "value": 1000}],
        "logic": "AND",
    }
    # The deployed MDX aggregates the measure per member.
    compiled = compile_definition(bd)
    assert compiled == "Filter([customer].Members, [Measures].[total_sales] > 1000)"

    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    ns = _named_set(builder_definition=bd, list_type="filtered")

    # _resolve_dim_name -> entity is a dimension; _resolve_measure_aggs ->
    # total_sales is a SUM measure.
    dim_res = _OneResult(scalar="customer")
    meas_res = _OneResult(rows=[("total_sales", "sum")])
    db.execute = AsyncMock(side_effect=[dim_res, meas_res])

    captured = {}

    async def fake_router(model_id, sql, bearer, timeout_s=30.0):
        captured["sql"] = sql
        return {"rows": [{"customer": "Acme"}, {"customer": "Globex"}]}

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.named_sets._execute_via_router", side_effect=fake_router):
        resp = await client.post(f"{PREFIX}/preview-by-definition", json={
            "builder_definition": bd,
        })

    assert resp.status_code == 200
    sql = captured["sql"].upper()
    # Aggregate threshold semantics, not a row-level WHERE.
    assert "GROUP BY" in sql
    assert "HAVING" in sql
    assert "SUM(" in sql
    assert "1000" in sql
    assert " WHERE " not in sql
    data = resp.json()
    assert [i["caption"] for i in data["items"]] == ["Acme", "Globex"]
    assert data["warnings"] == []


@pytest.mark.asyncio
async def test_filter_preview_warns_when_field_is_not_a_measure(client):
    """If a condition field is not a measure it cannot be expressed as the
    compiled MDX measure threshold; the preview must warn instead of silently
    returning a contradictory unfiltered list (F-018-02)."""
    bd = {
        "type": "filter",
        "entity": "customer",
        "conditions": [{"field": "region", "operator": "=", "value": "EMEA"}],
        "logic": "AND",
    }
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())

    dim_res = _OneResult(scalar="customer")
    meas_res = _OneResult(rows=[])  # 'region' is not a measure
    db.execute = AsyncMock(side_effect=[dim_res, meas_res])

    captured = {}

    async def fake_router(model_id, sql, bearer, timeout_s=30.0):
        captured["sql"] = sql
        return {"rows": [{"customer": "Acme"}]}

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.named_sets._execute_via_router", side_effect=fake_router):
        resp = await client.post(f"{PREFIX}/preview-by-definition", json={
            "builder_definition": bd,
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["warnings"]
    assert any("region" in w for w in data["warnings"])
    # No HAVING was built, so it falls back to the unfiltered list (with warning).
    assert "HAVING" not in captured["sql"].upper()


# ---------------------------------------------------------------------------
# ML17 fixes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_fixed_members_no_false_warnings(client):
    """F-018-11: a fixedMembers set's member keys (`&[key]`) must not be
    validated as model references — they previously produced a false
    "Reference [...] not found" warning per member."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    dims_result = _ScalarResult([_dimension(name="region")])
    meas_result = _ScalarResult([])
    db.execute = AsyncMock(side_effect=[dims_result, meas_result])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/validate", json={
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "region",
                "members": [{"key": "EMEA"}, {"key": "APAC"}],
            },
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_valid"] is True
    # Member keys EMEA/APAC must NOT be flagged as unknown references.
    assert not any("EMEA" in w or "APAC" in w for w in (data["warnings"] or []))


@pytest.mark.asyncio
async def test_revert_does_not_restore_certification(client):
    """F-018-05: reverting to an older version restores the definition but
    never the certification status, so a revert cannot re-certify a set an
    admin later deprecated."""
    ns = _named_set(name="Set", expression="{ NEW }", certification_status="deprecated")
    version = types.SimpleNamespace(
        version_number=3,
        snapshot={
            "name": "Set",
            "display_name": "Set",
            "description": None,
            "display_folder": None,
            "expression": "{ OLD }",
            "builder_definition": None,
            "list_type": "advanced_mdx",
            "scope": 1,
            "dimensions": None,
            "certification_status": "certified",
        },
    )
    db = make_mock_db()

    async def _get(model, _id):
        return ns if model.__name__ == "NamedSet" else _model()
    db.get = AsyncMock(side_effect=lambda model, _id: ns if getattr(model, "__name__", "") == "NamedSet" else _model())
    db.execute = AsyncMock(side_effect=[
        _OneResult(scalar=version),                      # version lookup
        _OneResult(scalar=2),                            # _create_version max()
    ])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{ns.id}/versions/3/revert")
    assert resp.status_code == 200
    # Definition restored from snapshot...
    assert ns.expression == "{ OLD }"
    # ...but certification_status NOT restored to "certified".
    assert ns.certification_status != "certified"


@pytest.mark.asyncio
async def test_duplicate_name_returns_409(client):
    """F-018-19: a (model_id, name) collision returns 409, not a raw 500."""
    from sqlalchemy.exc import IntegrityError

    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    db.flush = AsyncMock(side_effect=IntegrityError("dup", {}, Exception("dup")))
    db.rollback = AsyncMock()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Dupe",
            "expression": "{ [A].[A].&[x] }",
        })
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"].lower()


def test_glossary_create_rejects_unknown_target_type():
    """F-018-16: GlossaryEntryCreate now validates target_type/visibility/
    confidence the same way Update does."""
    import pytest as _pytest
    from shared.schemas.pydantic_models import GlossaryEntryCreate

    with _pytest.raises(Exception):
        GlossaryEntryCreate(term="t", definition="d", target_type="banana")
    with _pytest.raises(Exception):
        GlossaryEntryCreate(term="t", definition="d", target_type="dimension", visibility="banana")
    # Valid values pass.
    ok = GlossaryEntryCreate(term="t", definition="d", target_type="dimension", visibility="show", confidence="high")
    assert ok.target_type == "dimension"


def test_named_set_list_type_rejects_unsupported_modes():
    """F-018-24: list_type must be a mode the compiler can build."""
    import pytest as _pytest

    from shared.schemas.domains.governance_advanced import (
        NamedSetCreate,
        NamedSetUpdate,
    )

    # The two modes the schema docstring used to advertise but the compiler
    # never built — now rejected loudly instead of silently stored.
    for bad in ("relative_time", "exception", "banana"):
        with _pytest.raises(Exception):
            NamedSetCreate(name="x", expression="[a]", list_type=bad)
        with _pytest.raises(Exception):
            NamedSetUpdate(list_type=bad)

    # All four supported modes pass.
    for ok in ("fixed", "dynamic_top_n", "filtered", "advanced_mdx"):
        assert NamedSetCreate(name="x", expression="[a]", list_type=ok).list_type == ok
