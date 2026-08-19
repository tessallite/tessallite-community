"""Tests for Named Set CRUD, validation, and preview endpoints."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.security.execute_contract import ROW_SECURITY_DENY_ALL_RULE_ID

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    FakeResult,
    async_gen_from,
    make_mock_db,
    routed_execute,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/named-sets"


@pytest.fixture(autouse=True)
def _stub_model_lock():
    """Bug-7982: named-set definition/governance writers acquire the per-model
    advisory lock via ``acquire_model_definition_lock`` so they serialise with
    Save/revert. The DB is mocked in these unit tests, so stub the lock to a
    no-op spy (its real serialisation is covered by the DB integration suite).
    Yields the spy so a test can assert the writer invoked it."""
    with patch(
        "src.api.named_sets.acquire_model_definition_lock", new=AsyncMock()
    ) as _lock:
        yield _lock


@pytest.mark.asyncio
async def test_writer_acquires_model_definition_lock(client, _stub_model_lock):
    """Bug-7982 [CRITICAL]: a named-set definition/governance write must take the
    per-model advisory lock, so a concurrent model revert cannot lose it or
    republish a superseded definition. Asserts the PATCH (definition/governance)
    writer acquires the lock keyed on THIS model."""
    ns = _named_set(name="Set", expression="{ A }", certification_status="draft")
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    async def _refresh(obj):
        pass
    db.refresh = _refresh

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{ns.id}", json={"description": "changed"}
        )
    assert resp.status_code == 200, resp.text
    _stub_model_lock.assert_awaited()
    # Locked on the model under mutation.
    assert _stub_model_lock.await_args.args[1] == TEST_MODEL_ID


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
    # Bug-5963: listing also resolves the effective persona, and Bug-8767
    # added an rbac.caller_has_role binding lookup. routed_execute answers
    # per statement, so neither the count nor the ORDER of those extra
    # reads can shift the named-set rows onto the wrong query. The default
    # CurrentUser resolves to no persona, so the named set is unaffected.
    db.execute = AsyncMock(side_effect=routed_execute(named_sets=[ns]))
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
    dims_result = FakeResult([_dimension()])
    meas_result = FakeResult([_measure()])
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
    dims_result = FakeResult([_dimension()])
    meas_result = FakeResult([_measure()])
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
    dims_result = FakeResult([])
    meas_result = FakeResult([])
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
    dims_result = FakeResult([])
    meas_result = FakeResult([])
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
    dims_result = FakeResult([])
    meas_result = FakeResult([])
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
    dims_result = FakeResult([])
    meas_result = FakeResult([])
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
async def test_preview_router_failure_surfaces_visible_warning(client):
    """Bug-5927/Bug-5702 (F-018-GPT-02): a router execution failure during
    a dynamic (topN) preview must surface as a visible warning, not a
    silent "0 members" that is indistinguishable from a legitimately
    empty set. A modeller must not be able to save/certify/publish a
    broken dynamic set believing it has zero members.

    Note: the previous version of this test used the default
    builder_definition=None (advanced_mdx), which never reaches
    _execute_via_router at all — it asserted an empty result for the
    wrong reason. This uses a real topN builder_definition so the router
    mock is actually exercised.
    """
    bd = {
        "type": "topN", "entity": "Customer", "count": 5,
        "measure": "Revenue", "direction": "top",
    }
    ns = _named_set(builder_definition=bd)
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])
    # preview_named_set (the /{id}/preview route) resolves the caller's
    # effective persona before _preview_from_builder, which issues its own
    # db.execute -> scalars().all() call; give it an empty-personas result
    # so persona resolution short-circuits, then the dim/measure lookups.
    persona_res = types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: []),
    )
    dim_res = _OneResult(scalar="Customer")
    meas_res = _OneResult(rows=[("Revenue", "sum")])
    db.execute = AsyncMock(side_effect=[persona_res, dim_res, meas_res])

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.named_sets._execute_via_router", AsyncMock(side_effect=ValueError("Router error"))):
        resp = await client.post(f"{PREFIX}/{ns.id}/preview")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 0
    assert data["warnings"], "router failure must produce a visible warning"
    assert any("Router error" in w for w in data["warnings"])


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

    async def fake_router(model_id, sql, bearer, timeout_s=30.0, persona_id=None):
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


def test_bug7256_having_quotes_numeric_looking_string_like_the_compiler():
    """Bug-7256: the HAVING preview formatter must type a value exactly as the
    MDX compiler does. ``named_list_compiler._compile_filter`` quotes EVERY
    ``str`` value as a string literal — a numeric-looking string like ``"1000"``
    is quoted, and only a genuine numeric renders bare.

    Pre-fix ``_format_having_value("1000")`` ran ``float()`` on the string and,
    when it parsed, emitted ``1000`` BARE — so the preview treated ``"1000"`` as
    a number while the deployed set treated it as a string. This asserts the two
    agree, and fails on the pre-fix bare rendering.
    """
    from src.api.named_sets import _format_having_value
    from shared.named_list_compiler import compile_definition

    # Genuine numeric -> bare (unchanged, and matches the compiler).
    assert _format_having_value(1000) == "1000"
    assert _format_having_value(3.5) == "3.5"
    # Booleans stay keyword literals.
    assert _format_having_value(True) == "TRUE"

    # Numeric-looking STRING -> quoted string literal (the fix).
    assert _format_having_value("1000") == "'1000'"
    # Ordinary string -> quoted, with embedded-quote escaping (unchanged).
    assert _format_having_value("O'Brien") == "'O''Brien'"

    # Cross-check the producer: the compiler quotes the same string value as an
    # MDX string literal (not a bare number), so preview HAVING and the deployed
    # set now agree on typing.
    compiled_str = compile_definition({
        "type": "filter",
        "entity": "customer",
        "conditions": [{"field": "total_sales", "operator": ">", "value": "1000"}],
        "logic": "AND",
    })
    assert '"1000"' in compiled_str  # string literal, not a bare 1000
    compiled_num = compile_definition({
        "type": "filter",
        "entity": "customer",
        "conditions": [{"field": "total_sales", "operator": ">", "value": 1000}],
        "logic": "AND",
    })
    assert "[Measures].[total_sales] > 1000)" in compiled_num  # bare numeric


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

    async def fake_router(model_id, sql, bearer, timeout_s=30.0, persona_id=None):
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
    dims_result = FakeResult([_dimension(name="region")])
    meas_result = FakeResult([])
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
    empty_scalars = types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: []))
    db.execute = AsyncMock(side_effect=[
        _OneResult(scalar=version),                      # version lookup
        empty_scalars,                                   # Bug-5931: _get_model_metadata dims
        empty_scalars,                                   # Bug-5931: _get_model_metadata measures
        _OneResult(scalar=2),                            # _create_version max()
    ])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{ns.id}/versions/3/revert")
    assert resp.status_code == 200, resp.text
    # Definition restored from snapshot...
    assert ns.expression == "{ OLD }"
    # ...but certification_status NOT restored to "certified".
    assert ns.certification_status != "certified"


@pytest.mark.asyncio
async def test_revert_rejects_snapshot_with_hard_validation_error(client):
    """Bug-5931/Bug-5709 (F-018-GPT-07/F-018-R14): revert must run the same
    current-model validation as validate_named_set and block a restore that
    is hard-invalid (e.g. a blocked MDX function), instead of silently
    making it the set's live definition."""
    ns = _named_set(name="Set", expression="{ CURRENT }", certification_status="draft")
    version = types.SimpleNamespace(
        version_number=2,
        snapshot={
            "name": "Set",
            "display_name": "Set",
            "description": None,
            "display_folder": None,
            "expression": "DrillDownLevel([Customer].Members)",
            "builder_definition": None,
            "list_type": "advanced_mdx",
            "scope": 1,
            "dimensions": None,
            "certification_status": "draft",
        },
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda model, _id: ns if getattr(model, "__name__", "") == "NamedSet" else _model())
    empty_scalars = types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: []))
    db.execute = AsyncMock(side_effect=[
        _OneResult(scalar=version),  # version lookup
        empty_scalars,               # _get_model_metadata dims
        empty_scalars,               # _get_model_metadata measures
    ])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{ns.id}/versions/2/revert")
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "DRILLDOWNLEVEL" in detail
    # The set's live definition must NOT have been mutated by the blocked revert.
    assert ns.expression == "{ CURRENT }"


@pytest.mark.asyncio
async def test_revert_rejects_snapshot_with_non_compiling_builder_definition(client):
    """A snapshot whose builder_definition no longer compiles (e.g. it is
    missing a field the compiler now requires) must block the revert with
    a clear error rather than saving an uncompilable definition."""
    ns = _named_set(
        name="Set", expression="{ CURRENT }", builder_definition=None,
        certification_status="draft",
    )
    version = types.SimpleNamespace(
        version_number=2,
        snapshot={
            "name": "Set",
            "display_name": "Set",
            "description": None,
            "display_folder": None,
            "expression": None,
            # topN requires 'measure' — compile_definition raises
            # CompilationError without it.
            "builder_definition": {"type": "topN", "entity": "Customer", "count": 5},
            "list_type": "builder",
            "scope": 1,
            "dimensions": None,
            "certification_status": "draft",
        },
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda model, _id: ns if getattr(model, "__name__", "") == "NamedSet" else _model())
    empty_scalars = types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: []))
    db.execute = AsyncMock(side_effect=[
        _OneResult(scalar=version),  # version lookup
        empty_scalars,               # _get_model_metadata dims
        empty_scalars,               # _get_model_metadata measures
    ])
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{PREFIX}/{ns.id}/versions/2/revert")
    assert resp.status_code == 400, resp.text
    assert "no longer compiles" in resp.json()["detail"]
    assert ns.expression == "{ CURRENT }"


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

    # All five supported modes pass.
    for ok in ("fixed", "dynamic_top_n", "filtered", "advanced_mdx", "sql_fixed"):
        assert NamedSetCreate(name="x", expression="[a]", list_type=ok).list_type == ok


def test_compile_fixed_members_accepts_numeric_keys_without_crashing():
    """Bug-6267: numeric member keys are valid source values and compile as
    member keys, not Python type errors."""
    from shared.named_list_compiler import compile_definition

    expr = compile_definition(
        {
            "type": "fixedMembers",
            "dimension": "Customer",
            "members": [{"key": 1001, "caption": "Acme"}, 1002],
        }
    )

    assert "[Customer].[Customer].&[1001]" in expr
    assert "[Customer].[Customer].&[1002]" in expr


@pytest.mark.parametrize(
    "builder_definition, expected",
    [
        (
            {
                "type": "fixedMembers",
                "dimension": "Customer",
                "members": [{"caption": "Missing key"}],
            },
            "requires a non-empty 'key'",
        ),
        (
            {
                "type": "fixedMembers",
                "dimension": "Customer",
                "members": [{"key": {"bad": "shape"}}],
            },
            "key must be a string or number",
        ),
        (
            {
                "type": "topN",
                "entity": "Customer",
                "count": "many",
                "measure": "Revenue",
            },
            "positive 'count'",
        ),
    ],
)
@pytest.mark.asyncio
async def test_preview_by_definition_rejects_invalid_builder_shapes(
    client, builder_definition, expected
):
    """Bug-6267: malformed builder definitions return 422, not 500."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            f"{PREFIX}/preview-by-definition",
            json={"builder_definition": builder_definition},
        )

    assert resp.status_code == 422, resp.text
    assert expected in resp.json()["detail"]


# ---- Bug-6262 (F-018-02): PATCH definition-delta and certification guards ----


@pytest.mark.asyncio
async def test_noop_patch_on_certified_set_keeps_certification(client):
    """Bug-6262: a no-op save (admin echoes back all fields unchanged) must
    NOT decertify a certified set.  The old code detected definition changes
    by key presence, so echoing unchanged values triggered demotion."""
    ns = _named_set(
        name="Top 5",
        expression="{ [Dim].[Hier].&[A] }",
        list_type="advanced_mdx",
        certification_status="certified",
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    async def _refresh(obj):
        pass
    db.refresh = _refresh

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(f"{PREFIX}/{ns.id}", json={
            "name": "Top 5",
            "expression": "{ [Dim].[Hier].&[A] }",
            "list_type": "advanced_mdx",
            "certification_status": "certified",
        })
    assert resp.status_code == 200
    # The critical assertion: certification must NOT have been demoted.
    assert ns.certification_status == "certified"
    # No version row should have been created (db.add not called with a version).
    # Since _create_version calls db.add, and the mock tracks calls, we verify
    # that db.add was NOT called (no version row for a no-op).
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_modeler_patch_description_on_certified_set_succeeds(client):
    """Bug-6262: a modeler editing only the description of a certified set
    must NOT get a 403.  The old code triggered the admin-only guard when
    the frontend echoed back certification_status='certified'."""
    from src.auth.middleware import CurrentUser
    from src.main import app
    from src.auth.middleware import get_current_user

    modeler = CurrentUser(
        user_id="modeler@example.com",
        tenant_id="test-tenant",
        email="modeler@example.com",
        role="modeler",
    )
    app.dependency_overrides[get_current_user] = lambda: modeler

    ns = _named_set(
        name="Top 5",
        expression="{ [Dim].[Hier].&[A] }",
        list_type="advanced_mdx",
        certification_status="certified",
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    async def _refresh(obj):
        pass
    db.refresh = _refresh

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(f"{PREFIX}/{ns.id}", json={
            "description": "Updated description only",
            "certification_status": "certified",
        })
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    # The key assertion: the modeler is NOT blocked with a 403 despite the
    # payload echoing certification_status="certified".  Because description
    # is a definition field and the value changed, the set is correctly
    # auto-demoted to draft (the backend's designed edit-demote behaviour).
    assert ns.certification_status == "draft"
    assert ns.description == "Updated description only"


@pytest.mark.asyncio
async def test_real_definition_change_on_certified_set_demotes_to_draft(client):
    """Bug-6262 positive case: a genuine definition change MUST still
    demote a certified set to 'draft' and write a version row."""
    ns = _named_set(
        name="Top 5",
        expression="{ [Dim].[Hier].&[A] }",
        list_type="advanced_mdx",
        certification_status="certified",
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    async def _refresh(obj):
        pass
    db.refresh = _refresh

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(f"{PREFIX}/{ns.id}", json={
            "expression": "{ [Dim].[Hier].&[B] }",
            "certification_status": "certified",
        })
    assert resp.status_code == 200
    # Real definition change: expression changed, so certification is demoted.
    assert ns.certification_status == "draft"
    # The new expression must be applied.
    assert ns.expression == "{ [Dim].[Hier].&[B] }"
    # A version row must have been created.
    db.add.assert_called()


@pytest.mark.asyncio
async def test_modeler_cannot_set_shared_status(client):
    """Bug-6264 [SECURITY]: 'shared' is certified-equivalent (the XMLA
    catalogue renders it as [Certified]) and must be admin-only, exactly like
    'certified'/'deprecated'. A modeler promoting a draft set to 'shared' must
    be blocked with 403."""
    from src.auth.middleware import CurrentUser, get_current_user
    from src.main import app

    modeler = CurrentUser(
        user_id="modeler@example.com",
        tenant_id="test-tenant",
        email="modeler@example.com",
        role="modeler",
    )
    app.dependency_overrides[get_current_user] = lambda: modeler

    ns = _named_set(name="Set", expression="{ A }", certification_status="draft")
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{ns.id}", json={"certification_status": "shared"}
        )
    assert resp.status_code == 403, resp.text
    # The set stays draft — the privileged transition never applied.
    assert ns.certification_status == "draft"


@pytest.mark.parametrize("bad_status", ["banana", None])
@pytest.mark.asyncio
async def test_patch_invalid_certification_status_returns_422(client, bad_status):
    """Bug-6264: certification_status is a controlled enum. An unknown string
    or an explicit null must fail closed with 422, never bypass the enum/
    privileged gate or hit a NOT NULL 500."""
    ns = _named_set(name="Set", expression="{ A }", certification_status="draft")
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{ns.id}", json={"certification_status": bad_status}
        )
    assert resp.status_code == 422, resp.text
    assert ns.certification_status == "draft"


# ---------------------------------------------------------------------------
# sql_fixed create-time validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sql_fixed_valid(client):
    """A well-formed sql_fixed named list creates successfully with empty expression."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    # Bug-7927/Bug-7944: create now makes two db.execute calls:
    # 1. Case-insensitive uniqueness check (scalar_one_or_none -> None)
    # 2. Parameter namespace collision check (first -> None)
    no_match = MagicMock()
    no_match.scalar_one_or_none.return_value = None
    no_match.first.return_value = None
    db.execute = AsyncMock(return_value=no_match)

    ns = _named_set(
        name="TopChannels",
        expression="",
        list_type="sql_fixed",
        builder_definition={
            "type": "fixedMembers",
            "dimension": "channel",
            "data_type": "string",
            "members": ["online", "retail"],
        },
    )

    async def mock_refresh(obj):
        for attr, val in vars(ns).items():
            setattr(obj, attr, val)

    db.refresh = mock_refresh
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "TopChannels",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "channel",
                "data_type": "string",
                "members": ["online", "retail"],
            },
        })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["list_type"] == "sql_fixed"
    # expression is empty (not compiled to MDX)
    assert data["expression"] == ""


@pytest.mark.asyncio
async def test_create_sql_fixed_missing_builder_definition(client):
    """sql_fixed without builder_definition is rejected."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
        })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_sql_fixed_dynamic_type_requires_data_type(client):
    """sql_fixed with a dynamic builder type (topN) requires data_type field."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "TopCustomers",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "topN",
                "entity": "Customer",
                "count": 5,
                "measure": "Revenue",
            },
        })
    assert resp.status_code == 422
    assert "data_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_sql_fixed_missing_dimension(client):
    """sql_fixed without a 'dimension' field is rejected."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "data_type": "string",
                "members": ["a"],
            },
        })
    assert resp.status_code == 422
    assert "dimension" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_sql_fixed_missing_data_type(client):
    """sql_fixed without a 'data_type' field is rejected."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "channel",
                "members": ["a"],
            },
        })
    assert resp.status_code == 422
    assert "data_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_sql_fixed_invalid_data_type(client):
    """sql_fixed with data_type != string|number is rejected."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "channel",
                "data_type": "boolean",
                "members": ["a"],
            },
        })
    assert resp.status_code == 422
    assert "data_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_sql_fixed_empty_members(client):
    """sql_fixed with empty members list is rejected."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "channel",
                "data_type": "string",
                "members": [],
            },
        })
    assert resp.status_code == 422
    assert "non-empty" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_sql_fixed_member_count_exceeds_cap(client):
    """sql_fixed rejects when member count exceeds the configured cap."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.named_sets._settings") as mock_settings:
        mock_settings.NAMED_LIST_MEMBER_CAP = 3
        mock_settings.NAMED_LIST_MEMBER_CAP_CEILING = 5000
        resp = await client.post(PREFIX, json={
            "name": "TooBig",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "channel",
                "data_type": "string",
                "members": ["a", "b", "c", "d"],
            },
        })
    assert resp.status_code == 422
    assert "cap" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_sql_fixed_control_chars_rejected(client):
    """sql_fixed member values with control characters are rejected."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "channel",
                "data_type": "string",
                "members": ["online\x00injected"],
            },
        })
    assert resp.status_code == 422
    assert "control" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_sql_fixed_number_type_rejects_non_numeric(client):
    """sql_fixed with data_type=number rejects non-numeric members."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "region_id",
                "data_type": "number",
                "members": [10, "not_a_number", 30],
            },
        })
    assert resp.status_code == 422
    assert "numeric" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_sql_fixed_number_type_accepts_numeric(client):
    """sql_fixed with data_type=number accepts numeric members."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    no_match = MagicMock()
    no_match.scalar_one_or_none.return_value = None
    no_match.first.return_value = None
    db.execute = AsyncMock(return_value=no_match)

    ns = _named_set(
        name="TopRegions",
        expression="",
        list_type="sql_fixed",
        builder_definition={
            "type": "fixedMembers",
            "dimension": "region_id",
            "data_type": "number",
            "members": [10, 20, 30],
        },
    )

    async def mock_refresh(obj):
        for attr, val in vars(ns).items():
            setattr(obj, attr, val)

    db.refresh = mock_refresh
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "TopRegions",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "region_id",
                "data_type": "number",
                "members": [10, 20, 30],
            },
        })
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_create_sql_fixed_namespace_collision_with_parameter(client):
    """sql_fixed creation fails if a ModelParameter with the same name exists."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=_model())
    # First query: case-insensitive uniqueness check -> no match (no existing named set).
    no_match_result = MagicMock()
    no_match_result.scalar_one_or_none.return_value = None
    no_match_result.first.return_value = None
    # Second query: parameter namespace check -> finds a matching parameter.
    param_collision = MagicMock()
    param_collision.first.return_value = (uuid.uuid4(), "@Region")
    db.execute = AsyncMock(side_effect=[no_match_result, param_collision])

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Region",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "region",
                "data_type": "string",
                "members": ["EMEA"],
            },
        })
    assert resp.status_code == 409
    assert "parameter" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_sql_fixed_member_too_long(client):
    """sql_fixed rejects a single member that exceeds the per-member length cap."""
    db = make_mock_db()
    long_value = "x" * 1001
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "channel",
                "data_type": "string",
                "members": [long_value],
            },
        })
    assert resp.status_code == 422
    assert "1000-character" in resp.json()["detail"]


def test_sql_fixed_list_type_accepted_by_schema():
    """sql_fixed is an accepted list_type value in the Pydantic schema."""
    from shared.schemas.domains.governance_advanced import NamedSetCreate

    ns = NamedSetCreate(
        name="TopChannels",
        list_type="sql_fixed",
        builder_definition={
            "type": "fixedMembers",
            "dimension": "channel",
            "data_type": "string",
            "members": ["online"],
        },
    )
    assert ns.list_type == "sql_fixed"


@pytest.mark.asyncio
async def test_create_sql_fixed_dict_members_normalized_to_plain(client):
    """sql_fixed normalizes dict-form members ({key, caption}) to plain
    values so the persisted builder_definition is resolver-compatible.
    The SQL-path resolver renders members as typed literals; dict members
    would produce their Python repr instead of the intended value."""
    from src.api.named_sets import _validate_sql_fixed_builder

    bd = {
        "type": "fixedMembers",
        "dimension": "channel",
        "data_type": "string",
        "members": [{"key": "online", "caption": "Online Sales"}, "retail"],
    }
    _validate_sql_fixed_builder(bd, member_cap=1000)
    # After validation, dict members are normalized to plain strings.
    assert bd["members"] == ["online", "retail"]


def test_validate_sql_fixed_numeric_dict_members_normalized():
    """Numeric dict-form members are normalized to native numbers."""
    from src.api.named_sets import _validate_sql_fixed_builder

    bd = {
        "type": "fixedMembers",
        "dimension": "region_id",
        "data_type": "number",
        "members": [{"key": 42, "caption": "Region 42"}, 99],
    }
    _validate_sql_fixed_builder(bd, member_cap=1000)
    assert bd["members"] == [42, 99]


@pytest.mark.asyncio
async def test_create_sql_fixed_cap_clamped_to_ceiling(client):
    """When MEMBER_CAP > CEILING, the effective cap is CEILING (min wins)."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)), \
         patch("src.api.named_sets._settings") as mock_settings:
        mock_settings.NAMED_LIST_MEMBER_CAP = 10000
        mock_settings.NAMED_LIST_MEMBER_CAP_CEILING = 2
        resp = await client.post(PREFIX, json={
            "name": "CeilCapped",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "channel",
                "data_type": "string",
                "members": ["a", "b", "c"],
            },
        })
    assert resp.status_code == 422
    assert "cap (2)" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_sql_fixed_rejects_nan_member(client):
    """sql_fixed with data_type=number rejects NaN (sent as string since
    JSON does not support NaN literals)."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "region_id",
                "data_type": "number",
                "members": ["NaN"],
            },
        })
    assert resp.status_code == 422
    assert "finite" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_sql_fixed_rejects_inf_member(client):
    """sql_fixed with data_type=number rejects Infinity."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Bad",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "region_id",
                "data_type": "number",
                "members": ["Infinity"],
            },
        })
    assert resp.status_code == 422
    assert "finite" in resp.json()["detail"].lower()


def test_validate_sql_fixed_preserves_large_int_precision():
    """Integers beyond 2^53 must be preserved verbatim without float
    round-trip corruption (R3 fix, wrong-numbers class)."""
    from src.api.named_sets import _validate_sql_fixed_builder

    large_int = 9007199254740993  # 2^53 + 1
    bd = {
        "type": "fixedMembers",
        "dimension": "snowflake_id",
        "data_type": "number",
        "members": [large_int, 42],
    }
    _validate_sql_fixed_builder(bd, member_cap=1000)
    assert bd["members"][0] == large_int
    assert bd["members"][0] is not True  # not coerced
    assert bd["members"][1] == 42


def test_validate_sql_fixed_string_large_int_preserves_precision():
    """A large integer passed as a string is parsed via int(), not float(),
    so precision is preserved."""
    from src.api.named_sets import _validate_sql_fixed_builder

    bd = {
        "type": "fixedMembers",
        "dimension": "snowflake_id",
        "data_type": "number",
        "members": ["9007199254740993"],
    }
    _validate_sql_fixed_builder(bd, member_cap=1000)
    assert bd["members"][0] == 9007199254740993


def test_validate_sql_fixed_huge_int_string_accepted():
    """An extremely large integer string (10^400) is valid via int() parse
    and must not raise OverflowError or 500."""
    from src.api.named_sets import _validate_sql_fixed_builder

    huge = "1" + "0" * 400
    bd = {
        "type": "fixedMembers",
        "dimension": "region_id",
        "data_type": "number",
        "members": [huge],
    }
    _validate_sql_fixed_builder(bd, member_cap=1000)
    assert bd["members"][0] == int(huge)


def test_validate_sql_fixed_rejects_boolean_as_number():
    """JSON true/false are booleans in Python, not valid numeric members."""
    import pytest as _pytest
    from fastapi import HTTPException as _HTTPException
    from src.api.named_sets import _validate_sql_fixed_builder

    bd = {
        "type": "fixedMembers",
        "dimension": "flag",
        "data_type": "number",
        "members": [True],
    }
    with _pytest.raises(_HTTPException) as exc_info:
        _validate_sql_fixed_builder(bd, member_cap=1000)
    assert exc_info.value.status_code == 422
    assert "numeric" in exc_info.value.detail.lower()


def test_validate_sql_fixed_rejects_empty_member():
    """An empty-string member is rejected."""
    import pytest as _pytest
    from fastapi import HTTPException as _HTTPException
    from src.api.named_sets import _validate_sql_fixed_builder

    bd = {
        "type": "fixedMembers",
        "dimension": "region",
        "data_type": "string",
        "members": ["valid", ""],
    }
    with _pytest.raises(_HTTPException) as exc_info:
        _validate_sql_fixed_builder(bd, member_cap=1000)
    assert exc_info.value.status_code == 422
    assert "empty" in exc_info.value.detail.lower()


def test_validate_sql_fixed_rejects_native_float_nan():
    """A native float NaN (not from JSON, but from direct caller) is rejected."""
    import pytest as _pytest
    from fastapi import HTTPException as _HTTPException
    from src.api.named_sets import _validate_sql_fixed_builder

    bd = {
        "type": "fixedMembers",
        "dimension": "amount",
        "data_type": "number",
        "members": [float("nan")],
    }
    with _pytest.raises(_HTTPException) as exc_info:
        _validate_sql_fixed_builder(bd, member_cap=1000)
    assert exc_info.value.status_code == 422
    assert "finite" in exc_info.value.detail.lower()


def test_validate_sql_fixed_rejects_native_float_inf():
    """A native float Infinity (not from JSON, but from direct caller) is rejected."""
    import pytest as _pytest
    from fastapi import HTTPException as _HTTPException
    from src.api.named_sets import _validate_sql_fixed_builder

    bd = {
        "type": "fixedMembers",
        "dimension": "amount",
        "data_type": "number",
        "members": [float("inf")],
    }
    with _pytest.raises(_HTTPException) as exc_info:
        _validate_sql_fixed_builder(bd, member_cap=1000)
    assert exc_info.value.status_code == 422
    assert "finite" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# Refresh endpoint tests (Bug-7937 -- v1b refresh suite)
# ---------------------------------------------------------------------------

REFRESH_PREFIX = PREFIX  # inherits the same project/model path

_REFRESH_NS_ID = uuid.uuid4()


def _dynamic_ns(
    *,
    ns_id: uuid.UUID | None = None,
    btype: str = "topN",
    data_type: str = "string",
    members: list | None = None,
    query: str | None = None,
    entity: str = "Customer",
    measure: str = "Revenue",
    count: int = 10,
    direction: str = "top",
    conditions: list | None = None,
    logic: str = "AND",
    last_refreshed_at: str | None = None,
) -> types.SimpleNamespace:
    """Build a mock sql_fixed dynamic named set for refresh tests."""
    bd: dict = {"type": btype, "data_type": data_type, "members": members or []}
    if btype == "topN":
        bd.update({"entity": entity, "measure": measure, "count": count, "direction": direction})
    elif btype == "filter":
        bd.update({"entity": entity, "conditions": conditions or [], "logic": logic})
    elif btype == "sql_query":
        bd["query"] = query or "SELECT channel FROM source_channels"
    if last_refreshed_at:
        bd["last_refreshed_at"] = last_refreshed_at
    return _named_set(
        ns_id=ns_id or _REFRESH_NS_ID,
        name="TestDynamic",
        expression="",
        list_type="sql_fixed",
        builder_definition=bd,
    )


def _mock_execute_via_router(rows: list[dict]) -> AsyncMock:
    """Return a patched _execute_via_router that returns the given rows."""
    mock = AsyncMock(return_value={"rows": rows})
    return mock


def _refresh_db(ns):
    """Build a mock DB that works for the refresh endpoint.

    ensure_model_in_project needs db.get to return a model with project_id,
    and then db.get is called again for the named set. We chain side_effects.
    """
    db = make_mock_db()
    model = _model()
    db.get = AsyncMock(side_effect=[model, ns])
    return db


@pytest.mark.asyncio
async def test_refresh_topn_success(client):
    """Refresh a topN list returns updated members."""
    ns = _dynamic_ns(btype="topN")
    db = _refresh_db(ns)
    exec_rows = [{"Customer": "Alice"}, {"Customer": "Bob"}, {"Customer": "Charlie"}]

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._execute_via_router", _mock_execute_via_router(exec_rows)),
        patch("src.api.named_sets._resolve_dim_name", AsyncMock(return_value="Customer")),
        patch("src.api.named_sets._resolve_measure", AsyncMock(return_value=("Revenue", "sum"))),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 200
    data = resp.json()
    bd = data["builder_definition"]
    assert bd["members"] == ["Alice", "Bob", "Charlie"]
    assert bd["last_refreshed_at"] is not None


@pytest.mark.asyncio
async def test_refresh_named_list_exposes_refresh_vintage_in_response(client):
    """2026-08-11 named-list refresh vintage gap: success exposes the
    persisted refresh vintage as trust metadata for the UI and BI consumers."""
    ns = _dynamic_ns(btype="topN")
    ns.owner_user_id = "owner@example.com"
    db = _refresh_db(ns)

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets._execute_via_router",
            _mock_execute_via_router([{"Customer": "Alice"}]),
        ),
        patch("src.api.named_sets._resolve_dim_name", AsyncMock(return_value="Customer")),
        patch("src.api.named_sets._resolve_measure", AsyncMock(return_value=("Revenue", "sum"))),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")

    assert resp.status_code == 200
    data = resp.json()
    refreshed_at = data["builder_definition"]["last_refreshed_at"]
    assert data["trust_meta"] == {
        "last_refreshed_at": refreshed_at,
        "source_system": None,
        "owner": "owner@example.com",
    }


@pytest.mark.asyncio
async def test_refresh_filter_success(client):
    """Refresh a filter list returns updated members."""
    ns = _dynamic_ns(
        btype="filter",
        conditions=[{"field": "Revenue", "operator": ">", "value": 100}],
    )
    db = _refresh_db(ns)
    exec_rows = [{"Customer": "EMEA"}, {"Customer": "APAC"}]

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._execute_via_router", _mock_execute_via_router(exec_rows)),
        patch("src.api.named_sets._resolve_dim_name", AsyncMock(return_value="Customer")),
        patch("src.api.named_sets._resolve_measure_aggs", AsyncMock(return_value={"Revenue": 'SUM("Revenue")'})),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 200
    bd = resp.json()["builder_definition"]
    assert bd["members"] == ["EMEA", "APAC"]


@pytest.mark.asyncio
async def test_refresh_sql_query_success(client):
    """Refresh a sql_query list returns updated members."""
    ns = _dynamic_ns(btype="sql_query", query="SELECT channel FROM channels")
    db = _refresh_db(ns)
    exec_rows = [{"channel": "web"}, {"channel": "mobile"}]

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._execute_via_router", _mock_execute_via_router(exec_rows)),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 200
    bd = resp.json()["builder_definition"]
    assert bd["members"] == ["web", "mobile"]


@pytest.mark.asyncio
async def test_refresh_dml_rejection(client):
    """Refresh rejects sql_query containing DML keywords (defense-in-depth)."""
    ns = _dynamic_ns(btype="sql_query", query="INSERT INTO bad VALUES (1)")
    db = _refresh_db(ns)

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 422
    assert "DML" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_select_star_rejection(client):
    """Refresh rejects sql_query containing SELECT *."""
    ns = _dynamic_ns(btype="sql_query", query="SELECT * FROM bad_table")
    db = _refresh_db(ns)

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 422
    assert "SELECT *" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_dedup(client):
    """Refresh deduplicates member values."""
    ns = _dynamic_ns(btype="sql_query", query="SELECT channel FROM channels")
    db = _refresh_db(ns)
    exec_rows = [{"channel": "web"}, {"channel": "web"}, {"channel": "mobile"}]

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._execute_via_router", _mock_execute_via_router(exec_rows)),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 200
    assert resp.json()["builder_definition"]["members"] == ["web", "mobile"]


@pytest.mark.asyncio
async def test_refresh_cap_enforcement(client):
    """Refresh rejects when distinct values exceed the member cap."""
    ns = _dynamic_ns(btype="sql_query", query="SELECT id FROM big_table")
    db = _refresh_db(ns)
    # Create more rows than the default cap (1000)
    exec_rows = [{"id": str(i)} for i in range(1002)]

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._execute_via_router", _mock_execute_via_router(exec_rows)),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 400
    assert "cap" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_number_type_rejects_non_numeric(client):
    """Refresh rejects non-numeric values in a number-typed list."""
    ns = _dynamic_ns(btype="sql_query", query="SELECT val FROM nums", data_type="number")
    db = _refresh_db(ns)
    exec_rows = [{"val": 100}, {"val": "N/A"}, {"val": 200}]

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._execute_via_router", _mock_execute_via_router(exec_rows)),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 422
    assert "non-numeric" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_number_type_rejects_boolean(client):
    """Refresh rejects booleans in a number-typed list."""
    ns = _dynamic_ns(btype="sql_query", query="SELECT val FROM bools", data_type="number")
    db = _refresh_db(ns)
    exec_rows = [{"val": True}]

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._execute_via_router", _mock_execute_via_router(exec_rows)),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 422
    assert "boolean" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_fixed_members_rejected(client):
    """Refresh is rejected for fixedMembers definition type."""
    ns = _named_set(
        ns_id=_REFRESH_NS_ID,
        name="FixedList",
        expression="",
        list_type="sql_fixed",
        builder_definition={"type": "fixedMembers", "dimension": "Dim", "data_type": "string", "members": ["a"]},
    )
    db = _refresh_db(ns)

    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 400
    assert "fixedMembers" in resp.json()["detail"] or "dynamic" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refresh_dropped_condition_rejection(client):
    """Refresh rejects when a filter condition field is not a recognized measure."""
    ns = _dynamic_ns(
        btype="filter",
        conditions=[{"field": "FakeField", "operator": ">", "value": 100}],
    )
    db = _refresh_db(ns)

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._resolve_dim_name", AsyncMock(return_value="Customer")),
        patch("src.api.named_sets._resolve_measure_aggs", AsyncMock(return_value={})),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 422
    assert "FakeField" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_zero_row_success_with_warning(client):
    """Zero-row refresh succeeds and stores empty members with a timestamp."""
    ns = _dynamic_ns(btype="sql_query", query="SELECT channel FROM empty_table")
    db = _refresh_db(ns)

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._execute_via_router", _mock_execute_via_router([])),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 200
    bd = resp.json()["builder_definition"]
    assert bd["members"] == []
    assert bd["last_refreshed_at"] is not None


@pytest.mark.asyncio
async def test_refresh_preserves_members_on_failure(client):
    """When refresh query fails, previously stored members are preserved."""
    original_members = ["Alice", "Bob"]
    ns = _dynamic_ns(btype="topN", members=original_members)
    db = _refresh_db(ns)

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets._execute_via_router",
            AsyncMock(side_effect=Exception("Source unreachable")),
        ),
        patch("src.api.named_sets._resolve_dim_name", AsyncMock(return_value="Customer")),
        patch("src.api.named_sets._resolve_measure", AsyncMock(return_value=("Revenue", "sum"))),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")
    assert resp.status_code == 502
    # The named set object should not have been modified
    assert ns.builder_definition["members"] == original_members


@pytest.mark.asyncio
async def test_bug_8537_rls_denied_refresh_preserves_authoritative_members(client):
    """A deny-all router response is not an authoritative empty member list."""
    original_members = ["Alice", "Bob"]
    ns = _dynamic_ns(
        btype="sql_query",
        query="SELECT customer_name FROM customers",
        members=original_members,
    )
    db = _refresh_db(ns)

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "rows": [],
        "columns": ["customer_name"],
        "security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID],
    }
    http_client = MagicMock()
    http_client.post = AsyncMock(return_value=response)
    http_context = MagicMock()
    http_context.__aenter__ = AsyncMock(return_value=http_client)
    http_context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets.httpx.AsyncClient", return_value=http_context),
    ):
        resp = await client.post(f"{REFRESH_PREFIX}/{ns.id}/refresh")

    assert resp.status_code == 403
    assert "existing members have been left unchanged" in resp.json()["detail"]
    assert ns.builder_definition["members"] == original_members
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_sql_fixed_rejects_invalid_reference_name(client):
    """Create rejects sql_fixed list with invalid reference name (spaces)."""
    db = make_mock_db()
    with patch("src.api.named_sets.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={
            "name": "Top 5 Products",
            "list_type": "sql_fixed",
            "builder_definition": {
                "type": "fixedMembers",
                "dimension": "Product",
                "data_type": "string",
                "members": ["a", "b"],
            },
        })
    assert resp.status_code == 422
    assert "reference name" in resp.json()["detail"].lower() or "letter" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Bug-7982 (opus5 R5 finding 1): refresh_named_list must serialise its write
# with a concurrent definition change / delete detected UNDER the lock, and
# build the members onto the RE-READ definition — not the pre-call copy.
# ---------------------------------------------------------------------------


def _dynamic_named_list(**over):
    """A sql_fixed named list with a DYNAMIC (refreshable) builder definition."""
    ns = _named_set(
        name="Top Regions", list_type="sql_fixed",
        builder_definition={"type": "topN", "data_type": "string", "n": 5},
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _refresh_patches(db):
    """Common patches so the refresh reaches the lock/write stage: model-scope
    lookup, the SQL build, and the router execution returning two rows."""
    return (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch("src.api.named_sets._build_refresh_sql", new=AsyncMock(return_value="SELECT 1")),
        patch(
            "src.api.named_sets._execute_via_router",
            new=AsyncMock(return_value={"rows": [{"v": "north"}, {"v": "south"}]}),
        ),
    )


@pytest.mark.asyncio
async def test_refresh_409s_when_definition_changed_during_run(client):
    """If the list definition is edited (or reverted) WHILE the refresh query
    runs, the write must be rejected with 409 — never silently overwrite the
    concurrent change with members computed against the stale definition."""
    ns = _dynamic_named_list()
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    async def _refresh_rebinds_definition(obj, *a, **k):
        # A concurrent writer committed a DIFFERENT definition; the re-read under
        # the lock returns it (a new dict object, not equal to the pre-call bd).
        obj.builder_definition = {"type": "filter", "data_type": "string", "changed": True}

    db.refresh = _refresh_rebinds_definition

    p1, p2, p3 = _refresh_patches(db)
    with p1, p2, p3:
        resp = await client.post(f"{PREFIX}/{ns.id}/refresh", json={})

    assert resp.status_code == 409, resp.text
    assert "definition changed" in resp.json()["detail"].lower()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_409s_when_set_deleted_during_run(client):
    """If the set is dropped (e.g. by a concurrent model revert) while the
    refresh runs, re-reading it under the lock raises ObjectDeletedError, which
    must surface as a 409, not a 500."""
    from sqlalchemy.orm.exc import ObjectDeletedError

    ns = _dynamic_named_list()
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    async def _refresh_deleted(obj, *a, **k):
        # Build the instance without its message-formatting __init__ (which needs
        # a real InstanceState) — the endpoint only cares about the exception type.
        raise ObjectDeletedError.__new__(ObjectDeletedError)

    db.refresh = _refresh_deleted

    p1, p2, p3 = _refresh_patches(db)
    with p1, p2, p3:
        resp = await client.post(f"{PREFIX}/{ns.id}/refresh", json={})

    assert resp.status_code == 409, resp.text
    assert "removed" in resp.json()["detail"].lower()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_writes_members_onto_current_definition(client):
    """The happy path: an unchanged definition must NOT false-409, and the
    computed members are written onto the (re-read) builder definition."""
    ns = _dynamic_named_list()
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[_model(), ns])

    async def _refresh_noop(obj, *a, **k):
        # Definition unchanged during the run.
        return None

    db.refresh = _refresh_noop

    p1, p2, p3 = _refresh_patches(db)
    with p1, p2, p3:
        resp = await client.post(f"{PREFIX}/{ns.id}/refresh", json={})

    assert resp.status_code == 200, resp.text
    db.commit.assert_awaited()
    # Members from the router rows landed on the builder definition, and the
    # original definition keys (the type) are preserved.
    assert ns.builder_definition["members"] == ["north", "south"]
    assert ns.builder_definition["type"] == "topN"
    assert "last_refreshed_at" in ns.builder_definition
