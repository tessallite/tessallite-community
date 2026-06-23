"""Tests for pocket violation summary endpoint and pocket_id on DataQualityViolation."""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)

API_PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-quality-rules"


def _make_violation(rule_id, pocket_id=None, aggregate_id=None, count=1):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        rule_id=rule_id,
        detected_at=NOW,
        violation_count=count,
        sample_values=None,
        aggregate_id=aggregate_id,
        pocket_id=pocket_id,
    )


# ------------------------------------------------------------------ #
# pocket_id accepted on DataQualityViolation
# ------------------------------------------------------------------ #

class TestPocketViolationField:
    def test_violation_has_pocket_id(self):
        pocket_id = uuid.uuid4()
        v = _make_violation(uuid.uuid4(), pocket_id=pocket_id)
        assert v.pocket_id == pocket_id

    def test_violation_pocket_id_defaults_none(self):
        v = _make_violation(uuid.uuid4())
        assert v.pocket_id is None


# ------------------------------------------------------------------ #
# GET /pocket-violations
# ------------------------------------------------------------------ #

class TestPocketViolationSummary:
    @pytest.mark.anyio
    async def test_pocket_violations_returns_counts(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        rule_id1 = uuid.uuid4()
        pocket_id_a = uuid.uuid4()
        pocket_id_b = uuid.uuid4()

        rule_result = MagicMock()
        rule_result.scalars.return_value.all.return_value = [rule_id1]

        violation_rows = [
            types.SimpleNamespace(pocket_id=pocket_id_a, total=5),
            types.SimpleNamespace(pocket_id=pocket_id_b, total=3),
        ]
        violation_result = MagicMock()
        violation_result.all.return_value = violation_rows

        db.execute = AsyncMock(side_effect=[rule_result, violation_result])

        with patch("src.api.data_quality.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{API_PREFIX}/pocket-violations")
        assert resp.status_code == 200
        data = resp.json()
        assert data[str(pocket_id_a)] == 5
        assert data[str(pocket_id_b)] == 3

    @pytest.mark.anyio
    async def test_pocket_violations_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        rule_result = MagicMock()
        rule_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=rule_result)

        with patch("src.api.data_quality.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{API_PREFIX}/pocket-violations")
        assert resp.status_code == 200
        assert resp.json() == {}

    @pytest.mark.anyio
    async def test_pocket_violations_no_pocket_violations(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        rule_id1 = uuid.uuid4()
        rule_result = MagicMock()
        rule_result.scalars.return_value.all.return_value = [rule_id1]

        violation_result = MagicMock()
        violation_result.all.return_value = []

        db.execute = AsyncMock(side_effect=[rule_result, violation_result])

        with patch("src.api.data_quality.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{API_PREFIX}/pocket-violations")
        assert resp.status_code == 200
        assert resp.json() == {}
