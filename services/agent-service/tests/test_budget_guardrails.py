"""Tests for budget guardrails — configurable cost table, cost estimation, complexity check."""
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from src.guardrails import budget


class TestEstimateCostUsd:
    def test_known_provider(self):
        cost = budget.estimate_cost_usd("anthropic", 1000, 1000)
        assert cost > 0

    def test_unknown_provider_charged_fallback_rate(self):
        """Bug-1103 — an unmapped provider is charged the non-zero fallback
        rate, never silently zeroed (which would bypass the cost budget)."""
        cost = budget.estimate_cost_usd("unknown_provider_xyz", 1000, 1000)
        expected = (
            1000 * budget._FALLBACK_PER_1K["input"]
            + 1000 * budget._FALLBACK_PER_1K["output"]
        ) / 1000.0
        assert cost == expected
        assert cost > 0

    def test_unknown_provider_warns_once(self):
        """The unmapped-provider warning de-duplicates per provider string."""
        budget._warned_providers.discard("brand_new_provider_q")
        budget.estimate_cost_usd("brand_new_provider_q", 10, 10)
        assert "brand_new_provider_q" in budget._warned_providers

    def test_zero_tokens(self):
        assert budget.estimate_cost_usd("openai", 0, 0) == 0.0


class TestLoadCostTable:
    def test_loads_from_json(self, tmp_path):
        cfg = {
            "test_provider": {"input_per_1k": 0.01, "output_per_1k": 0.05},
        }
        cfg_file = tmp_path / "costs.json"
        cfg_file.write_text(json.dumps(cfg))

        original = budget._COST_CONFIG_PATH
        budget._COST_CONFIG_PATH = str(cfg_file)
        try:
            table = budget._load_cost_table()
        finally:
            budget._COST_CONFIG_PATH = original

        assert "test_provider" in table
        assert table["test_provider"]["input"] == 0.01
        assert table["test_provider"]["output"] == 0.05

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        original = budget._COST_CONFIG_PATH
        budget._COST_CONFIG_PATH = str(tmp_path / "nonexistent.json")
        try:
            table = budget._load_cost_table()
        finally:
            budget._COST_CONFIG_PATH = original

        assert "anthropic" in table
        assert "openai" in table
        assert "gemini" in table

    def test_malformed_json_falls_back(self, tmp_path):
        cfg_file = tmp_path / "bad.json"
        cfg_file.write_text("not json")

        original = budget._COST_CONFIG_PATH
        budget._COST_CONFIG_PATH = str(cfg_file)
        try:
            table = budget._load_cost_table()
        finally:
            budget._COST_CONFIG_PATH = original

        assert "anthropic" in table


class TestCheckQueryComplexity:
    def _cfg(self, max_complexity: int) -> MagicMock:
        cfg = MagicMock()
        cfg.max_query_complexity = max_complexity
        cfg.project_id = "test"
        return cfg

    def _call(self, measures=None, dimensions=None, where=None):
        call = MagicMock()
        call.measures = measures or []
        call.dimensions = dimensions or []
        call.where = where or []
        call.having = []
        call.sort = []
        return call

    def test_within_limit(self):
        assert budget.check_query_complexity(
            self._cfg(10), self._call(measures=["a"], dimensions=["b"])
        ) is None

    def test_exceeds_limit(self):
        result = budget.check_query_complexity(
            self._cfg(2), self._call(measures=["a"], dimensions=["b", "c"])
        )
        assert result == "query_too_complex"

    def test_no_limit(self):
        assert budget.check_query_complexity(
            self._cfg(0), self._call(measures=["a"] * 100)
        ) is None


class TestRecordTurnCostProvider:
    """F-023-04 — a completed answer turn must yield a costed ledger row.

    The bug was that the answer path passed provider="" to record_turn_cost,
    so estimate_cost_usd found no rates and wrote estimated_cost_usd=None,
    leaving daily_cost_budget_usd blind to answer/narration spend.
    """

    @pytest.mark.asyncio
    async def test_known_provider_writes_nonnull_cost(self):
        import uuid
        from unittest.mock import AsyncMock

        db = AsyncMock()
        added = []
        db.add = lambda entry: added.append(entry)

        await budget.record_turn_cost(
            db=db,
            project_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            llm_config_id=uuid.uuid4(),
            provider="anthropic",
            input_tokens=1000,
            output_tokens=500,
        )

        assert len(added) == 1
        entry = added[0]
        assert entry.estimated_cost_usd is not None
        assert entry.estimated_cost_usd > 0

    @pytest.mark.asyncio
    async def test_unmapped_provider_writes_nonnull_fallback_cost(self):
        """Bug-1103 — an unmapped/empty provider must write a concrete non-NULL
        cost (fallback rate), never NULL. NULL would drop the spend from the
        daily_cost_budget_usd sum, silently reopening the F-023-04 bypass."""
        import uuid
        from unittest.mock import AsyncMock

        db = AsyncMock()
        added = []
        db.add = lambda entry: added.append(entry)

        await budget.record_turn_cost(
            db=db,
            project_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            llm_config_id=None,
            provider="",
            input_tokens=1000,
            output_tokens=500,
        )

        assert len(added) == 1
        entry = added[0]
        assert entry.estimated_cost_usd is not None
        expected = (
            1000 * budget._FALLBACK_PER_1K["input"]
            + 500 * budget._FALLBACK_PER_1K["output"]
        ) / 1000.0
        assert entry.estimated_cost_usd == expected
        assert entry.estimated_cost_usd > 0

    @pytest.mark.asyncio
    async def test_zero_token_turn_writes_concrete_zero_not_null(self):
        """A genuine 0-token turn writes a real 0.0 (non-NULL), so the budget
        sum stays a clean number and the ledger never carries NULL costs."""
        import uuid
        from unittest.mock import AsyncMock

        db = AsyncMock()
        added = []
        db.add = lambda entry: added.append(entry)

        await budget.record_turn_cost(
            db=db,
            project_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            llm_config_id=None,
            provider="anthropic",
            input_tokens=0,
            output_tokens=0,
        )

        assert added[0].estimated_cost_usd == 0.0
        assert added[0].estimated_cost_usd is not None

    @pytest.mark.asyncio
    async def test_provider_persisted_on_ledger_row(self):
        """F-023-28 — the ledger row stores the provider so the /cost report
        can split spend per provider from data."""
        import uuid
        from unittest.mock import AsyncMock

        db = AsyncMock()
        added = []
        db.add = lambda entry: added.append(entry)

        await budget.record_turn_cost(
            db=db,
            project_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            llm_config_id=uuid.uuid4(),
            provider="anthropic",
            input_tokens=100,
            output_tokens=50,
        )
        assert added[0].provider == "anthropic"

    @pytest.mark.asyncio
    async def test_empty_provider_persisted_as_none(self):
        """F-023-28 — an empty provider string is normalised to None so it
        reports as "unknown" rather than an empty-string bucket."""
        import uuid
        from unittest.mock import AsyncMock

        db = AsyncMock()
        added = []
        db.add = lambda entry: added.append(entry)

        await budget.record_turn_cost(
            db=db,
            project_id=uuid.uuid4(),
            turn_id=uuid.uuid4(),
            llm_config_id=None,
            provider="",
            input_tokens=10,
            output_tokens=10,
        )
        assert added[0].provider is None


class TestPostTurnBudgetCheck:
    """Bug-5283 — post-turn budget enforcement catches a single expensive
    request that sneaks past the pre-turn gate."""

    def _cfg(self, *, daily_token_budget=0, daily_cost_budget_usd=0):
        cfg = MagicMock()
        cfg.project_id = "test-project"
        cfg.daily_token_budget = daily_token_budget
        cfg.daily_cost_budget_usd = daily_cost_budget_usd
        return cfg

    @pytest.mark.asyncio
    async def test_post_turn_detects_token_budget_breach(self):
        from unittest.mock import AsyncMock
        # Simulate that today's usage is 900 tokens already
        db = AsyncMock()
        with patch.object(budget, "_today_usage", return_value=(900, 0.0)):
            result = await budget.check_budget_post_turn(
                db, self._cfg(daily_token_budget=1000),
                turn_input_tokens=200,
                turn_output_tokens=100,
                provider="anthropic",
            )
        assert result == "daily_token_budget_exceeded"

    @pytest.mark.asyncio
    async def test_post_turn_no_breach_within_limit(self):
        from unittest.mock import AsyncMock
        db = AsyncMock()
        with patch.object(budget, "_today_usage", return_value=(500, 0.0)):
            result = await budget.check_budget_post_turn(
                db, self._cfg(daily_token_budget=1000),
                turn_input_tokens=100,
                turn_output_tokens=50,
                provider="anthropic",
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_post_turn_detects_cost_budget_breach(self):
        from unittest.mock import AsyncMock
        db = AsyncMock()
        # _today_usage returns cost just under the $1.0 limit; adding
        # the turn cost pushes it over.
        with patch.object(budget, "_today_usage", return_value=(0, 0.99)):
            result = await budget.check_budget_post_turn(
                db, self._cfg(daily_cost_budget_usd=1.0),
                turn_input_tokens=1000,
                turn_output_tokens=1000,
                provider="anthropic",
            )
        assert result == "daily_cost_budget_exceeded"

    @pytest.mark.asyncio
    async def test_post_turn_no_limit_returns_none(self):
        from unittest.mock import AsyncMock
        db = AsyncMock()
        result = await budget.check_budget_post_turn(
            db, self._cfg(),
            turn_input_tokens=100000,
            turn_output_tokens=100000,
            provider="anthropic",
        )
        assert result is None
