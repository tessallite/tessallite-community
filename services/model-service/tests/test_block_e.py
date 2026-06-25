"""Tests for Block E — Data Quality Rules."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from .conftest import (
    TEST_TENANT,
    TEST_USER_ID,
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_modeler():
    user = CurrentUser(user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID)
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def modeler_client(auth_modeler):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_model():
    return types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)


def _make_rule(rule_id: uuid.UUID | None = None):
    rid = rule_id or uuid.uuid4()
    return types.SimpleNamespace(
        id=rid,
        model_id=TEST_MODEL_ID,
        name="test-rule",
        target_type="column",
        target_id=uuid.uuid4(),
        rule_type="not_null",
        rule_config=None,
        severity="warn",
        is_enabled=True,
        block_on_failure=False,
        last_checked_at=None,
        last_violation_count=None,
        created_at=NOW,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------------
# E.1 — CRUD API tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_rule_returns_201(modeler_client):
    """POST /data-quality-rules creates a rule and returns 201."""
    rule_id = uuid.uuid4()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=_make_model())

    mock_db.add = MagicMock()

    async def _refresh(obj):
        obj.id = rule_id
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.last_checked_at = None
        obj.last_violation_count = None

    mock_db.refresh = AsyncMock(side_effect=_refresh)

    with patch("src.api.data_quality.get_tenant_db", async_gen_from(mock_db)):
        resp = await modeler_client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-quality-rules",
            json={
                "name": "no-nulls",
                "target_type": "column",
                "target_id": str(uuid.uuid4()),
                "rule_type": "not_null",
                "severity": "warn",
            },
        )

    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_list_rules_returns_200(modeler_client):
    """GET /data-quality-rules returns list of rules."""
    rule = _make_rule()
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=_make_model())

    result = MagicMock()
    result.scalars.return_value.all.return_value = [rule]
    mock_db.execute = AsyncMock(return_value=result)

    with patch("src.api.data_quality.get_tenant_db", async_gen_from(mock_db)):
        resp = await modeler_client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-quality-rules"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["name"] == "test-rule"


@pytest.mark.asyncio
async def test_delete_rule_returns_204(modeler_client):
    """DELETE /data-quality-rules/{id} returns 204."""
    rule_id = uuid.uuid4()
    rule = _make_rule(rule_id)

    mock_db = make_mock_db()

    def _get_dispatch(cls, pk):
        if hasattr(cls, "__tablename__") and cls.__tablename__ == "models":
            return _make_model()
        if hasattr(cls, "__tablename__") and cls.__tablename__ == "data_quality_rules":
            return rule if pk == rule_id else None
        return None

    mock_db.get = AsyncMock(side_effect=_get_dispatch)

    with patch("src.api.data_quality.get_tenant_db", async_gen_from(mock_db)):
        resp = await modeler_client.delete(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-quality-rules/{rule_id}"
        )

    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_create_rule_invalid_type_returns_422(modeler_client):
    """Creating a rule with invalid rule_type returns 422."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=_make_model())

    with patch("src.api.data_quality.get_tenant_db", async_gen_from(mock_db)):
        resp = await modeler_client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-quality-rules",
            json={
                "name": "bad-rule",
                "target_type": "column",
                "target_id": str(uuid.uuid4()),
                "rule_type": "invalid_type",
                "severity": "warn",
            },
        )

    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# E.2 — Validator unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_rules_empty_returns_no_violations():
    """validate_rules with no enabled rules returns empty list."""
    from shared.data_quality.validator import validate_rules

    mock_db = AsyncMock()
    mock_db.info = {"tenant_id": TEST_TENANT}
    rules_result = MagicMock()
    rules_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=rules_result)

    violations = await validate_rules(TEST_MODEL_ID, mock_db)
    assert violations == []


@pytest.mark.asyncio
async def test_validate_rules_no_source_returns_empty():
    """validate_rules uses the model fallback route when a column has no source."""
    from shared.data_quality.validator import validate_rules
    from shared.db.models import ModelColumn, ModelTable

    rule = _make_rule()
    mock_db = AsyncMock()
    mock_db.info = {"tenant_id": TEST_TENANT}
    table_id = uuid.uuid4()
    column = types.SimpleNamespace(model_table_id=table_id, column_name="order_id")
    table = types.SimpleNamespace(physical_name="orders", source_id=None)

    async def _get_dispatch(cls, pk):
        if cls is ModelColumn and pk == rule.target_id:
            return column
        if cls is ModelTable and pk == table_id:
            return table
        return None

    mock_db.get = AsyncMock(side_effect=_get_dispatch)
    mock_db.flush = AsyncMock()

    rules_result = MagicMock()
    rules_result.scalars.return_value.all.return_value = [rule]
    source_result = MagicMock()
    source_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[rules_result, source_result])

    with patch(
        "shared.data_quality.validator._count_violations",
        new=AsyncMock(return_value=(0, None)),
    ) as count_violations:
        violations = await validate_rules(TEST_MODEL_ID, mock_db)

    assert violations == []
    assert count_violations.await_args.kwargs["source_id"] is None


# ---------------------------------------------------------------------------
# E.3 — Manual validate API test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_validation_endpoint_returns_200(modeler_client):
    """POST /data-quality-rules/validate returns 200 with summary."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=_make_model())

    # rules_count query returns empty (0 rules)
    count_result = MagicMock()
    count_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=count_result)

    with (
        patch("src.api.data_quality.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.data_quality.validate_rules", AsyncMock(return_value=[])),
    ):
        resp = await modeler_client.post(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-quality-rules/validate"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rules_checked"] == 0
    assert body["violations_found"] == 0
    assert body["rule_results"] == []


# ---------------------------------------------------------------------------
# E.4 — Violation history API test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_violations_endpoint_returns_list(modeler_client):
    """GET /data-quality-rules/{id}/violations returns violation list."""
    rule_id = uuid.uuid4()
    rule = _make_rule(rule_id)

    violation = types.SimpleNamespace(
        id=uuid.uuid4(),
        rule_id=rule_id,
        detected_at=NOW,
        violation_count=5,
        sample_values={"values": ["NULL"]},
        aggregate_id=None,
    )

    mock_db = make_mock_db()

    def _get_dispatch(cls, pk):
        if hasattr(cls, "__tablename__") and cls.__tablename__ == "models":
            return _make_model()
        if hasattr(cls, "__tablename__") and cls.__tablename__ == "data_quality_rules":
            return rule if pk == rule_id else None
        return None

    mock_db.get = AsyncMock(side_effect=_get_dispatch)

    violations_result = MagicMock()
    violations_result.scalars.return_value.all.return_value = [violation]
    mock_db.execute = AsyncMock(return_value=violations_result)

    with patch("src.api.data_quality.get_tenant_db", async_gen_from(mock_db)):
        resp = await modeler_client.get(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-quality-rules/{rule_id}/violations"
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["violation_count"] == 5
