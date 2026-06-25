"""HTTP route tests for src.api.row_security (Phase 5.1.D)."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import Model, ModelTable, RowSecurityRule

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/row-security"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_rule(
    rule_id=None,
    rule_type="role_predicate",
    name="north",
    path="region.region_code",
    expr="dimension_equals('region.region_code', 'NORTH')",
    roles=("region_manager_north",),
    mapping_table_id=None,
    mapping_user_column=None,
    mapping_value_column=None,
):
    return types.SimpleNamespace(
        id=rule_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        dimension_path=path,
        rule_type=rule_type,
        predicate_expression=expr if rule_type == "role_predicate" else None,
        applies_to_roles=list(roles) if rule_type == "role_predicate" else None,
        mapping_table_id=mapping_table_id,
        mapping_user_column=mapping_user_column,
        mapping_value_column=mapping_value_column,
        is_enabled=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _result_with(items):
    # scalar_one_or_none returns the first row (or None) so the simulate
    # endpoint's connector-resolution query (F-007-07, which loads the
    # model's DataSource) works against the mock the same way the rule
    # loader's scalars().all() does.
    return types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: items),
        scalar_one_or_none=lambda: (items[0] if items else None),
    )


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_rejects_wrong_project_scope(client):
    db = make_mock_db()
    wrong_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=uuid.uuid4()
    )
    db.get = AsyncMock(return_value=wrong_model)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


# ---------------------------------------------------------------------------
# List + get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_rules_for_model(client):
    db = make_mock_db()
    r1 = _make_rule(name="north")
    r2 = _make_rule(name="south", roles=("region_manager_south",))

    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(return_value=_result_with([r1, r2]))

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert {r["name"] for r in data} == {"north", "south"}


@pytest.mark.asyncio
async def test_get_single_rule(client):
    db = make_mock_db()
    rule = _make_rule()

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{rule.id}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "north"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_role_predicate_rule(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    # Bug-5206 validation queries for a Dimension matching the path's last
    # segment. Return a matching row so the validation passes.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.refresh = AsyncMock(side_effect=_refresh)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "north-only",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "predicate_expression": "dimension_equals('region.region_code', 'NORTH')",
                "applies_to_roles": ["region_manager_north"],
            },
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["rule_type"] == "role_predicate"
    assert db.add.call_count >= 1
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_create_user_mapping_rule_validates_mapping_table_scope(client):
    db = make_mock_db()
    mapping_table_id = uuid.uuid4()
    # mapping table belongs to a DIFFERENT model → must be rejected.
    other_model_table = types.SimpleNamespace(
        id=mapping_table_id, model_id=uuid.uuid4()
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is ModelTable and str(obj_id) == str(mapping_table_id):
            return other_model_table
        return None

    db.get = AsyncMock(side_effect=_get)

    # Bug-5206 validation queries for a Dimension matching the path's last
    # segment. Return a matching row so the validation passes and the test
    # reaches the mapping_table_id scope check.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "per-user-region",
                "dimension_path": "region.region_code",
                "rule_type": "user_mapping",
                "mapping_table_id": str(mapping_table_id),
                "mapping_user_column": "user_id",
                "mapping_value_column": "region_code",
            },
        )

    assert resp.status_code == 400
    assert "mapping_table_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_rejects_role_predicate_without_expression(client):
    """Pydantic shape validator rejects at the 422 layer before hitting DB."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "bad",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "applies_to_roles": ["x"],
                # missing predicate_expression
            },
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Update + delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_rule_patches_fields(client):
    db = make_mock_db()
    rule = _make_rule()

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={"is_enabled": False, "name": "north-disabled"},
        )

    assert resp.status_code == 200
    assert rule.is_enabled is False
    assert rule.name == "north-disabled"


@pytest.mark.asyncio
async def test_delete_rule(client):
    db = make_mock_db()
    rule = _make_rule()

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{rule.id}")

    assert resp.status_code == 204
    db.delete.assert_awaited_once_with(rule)


@pytest.mark.asyncio
async def test_delete_404_when_rule_on_other_model(client):
    db = make_mock_db()
    foreign_rule = _make_rule()
    foreign_rule.model_id = uuid.uuid4()  # different model

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return foreign_rule
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{foreign_rule.id}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Simulate-as-user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simulate_no_matching_rule_returns_empty(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    # Loader returns a rule that won't match the simulated roles.
    rule = _make_rule()

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return _result_with([rule])
        return _result_with([])

    db.execute = _execute

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            f"{PREFIX}/simulate",
            json={"user_identity": "alice@x", "roles": ["viewer"]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["active_rule_ids"] == []
    assert body["compiled_predicate"] is None


@pytest.mark.asyncio
async def test_simulate_matching_role_returns_compiled_predicate(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    rule = _make_rule()

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return _result_with([rule])
        return _result_with([])

    db.execute = _execute

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            f"{PREFIX}/simulate",
            json={
                "user_identity": "alice@x",
                "roles": ["region_manager_north"],
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["active_rule_ids"]) == 1
    assert body["active_rule_ids"][0] == str(rule.id)
    assert body["compiled_predicate"] == "\"region_code\" = 'NORTH'"


# ---------------------------------------------------------------------------
# Bug-5206: dimension_path / predicate mismatch rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_predicate_column_mismatch_bug5206(client):
    """A rule declaring dimension_path 'region.region_code' but filtering
    on 'country_code' inside the predicate must be rejected at create time."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    # Return a matching dimension so _validate_dimension_path_exists passes.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "mismatch-rule",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "predicate_expression": "dimension_equals('region.country_code', 'US')",
                "applies_to_roles": ["viewer"],
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "country_code" in detail
    assert "region_code" in detail


@pytest.mark.asyncio
async def test_update_rejects_predicate_column_mismatch_bug5206(client):
    """Updating a rule's predicate to reference a different column than the
    declared dimension_path must be rejected."""
    db = make_mock_db()
    rule = _make_rule(
        path="region.region_code",
        expr="dimension_equals('region.region_code', 'NORTH')",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={
                "predicate_expression": "dimension_equals('region.country_code', 'US')",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "country_code" in detail
    assert "region_code" in detail


@pytest.mark.asyncio
async def test_update_rejects_dimension_path_mismatch_with_existing_predicate_bug5206(client):
    """Changing only dimension_path to a column that doesn't match the existing
    predicate must be rejected."""
    db = make_mock_db()
    rule = _make_rule(
        path="region.region_code",
        expr="dimension_equals('region.region_code', 'NORTH')",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)

    # _validate_dimension_path_exists must pass — return a matching dimension.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={
                "dimension_path": "region.country_code",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "region_code" in detail
    assert "country_code" in detail


# ---------------------------------------------------------------------------
# Bug-5207: mapping column existence rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_nonexistent_mapping_columns_bug5207(client):
    """A user_mapping rule referencing columns that don't exist on the
    mapping table must be rejected at create time."""
    db = make_mock_db()
    mapping_table_id = uuid.uuid4()
    # Mapping table belongs to the SAME model — passes scope check.
    same_model_table = types.SimpleNamespace(
        id=mapping_table_id, model_id=TEST_MODEL_ID,
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is ModelTable and str(obj_id) == str(mapping_table_id):
            return same_model_table
        return None

    db.get = AsyncMock(side_effect=_get)

    # First execute: _validate_dimension_path_exists — return a match.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    # Second execute: _validate_mapping_columns — return empty (no columns found).
    col_result = MagicMock()
    col_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[dim_result, col_result])

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "per-user-region",
                "dimension_path": "region.region_code",
                "rule_type": "user_mapping",
                "mapping_table_id": str(mapping_table_id),
                "mapping_user_column": "nonexistent_user",
                "mapping_value_column": "nonexistent_val",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "nonexistent_user" in detail or "nonexistent_val" in detail
    assert "not found" in detail.lower()


@pytest.mark.asyncio
async def test_update_rejects_nonexistent_mapping_columns_bug5207(client):
    """Updating a user_mapping rule to reference a column that doesn't exist
    on the mapping table must be rejected."""
    db = make_mock_db()
    mapping_table_id = uuid.uuid4()
    rule = _make_rule(
        rule_type="user_mapping",
        path="region.region_code",
        expr=None,
        roles=None,
        mapping_table_id=mapping_table_id,
        mapping_user_column="user_id",
        mapping_value_column="region_code",
    )

    same_model_table = types.SimpleNamespace(
        id=mapping_table_id, model_id=TEST_MODEL_ID,
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        if cls is ModelTable and str(obj_id) == str(mapping_table_id):
            return same_model_table
        return None

    db.get = AsyncMock(side_effect=_get)

    # _validate_mapping_columns returns empty — column not found.
    col_result = MagicMock()
    col_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=col_result)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={
                "mapping_value_column": "bad_column",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "bad_column" in detail or "not found" in detail.lower()
