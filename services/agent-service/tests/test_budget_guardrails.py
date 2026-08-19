"""Tests for budget guardrails — configurable cost table, cost estimation, complexity check."""
from __future__ import annotations

import json
import os
import tempfile
import uuid as _uuid
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


class TestBudgetReservationExclusion:
    """Bug-7777 — the pessimistic budget reservation must not count against
    check_budget when the reservation_id is passed.  Without this, projects
    with daily_token_budget <= 8096 (the reservation estimate) refuse every
    turn including the first on a fresh day."""

    def _cfg(self, *, daily_token_budget=0, daily_cost_budget_usd=0):
        cfg = MagicMock()
        cfg.project_id = "test-project"
        cfg.daily_token_budget = daily_token_budget
        cfg.daily_cost_budget_usd = daily_cost_budget_usd
        return cfg

    @pytest.mark.asyncio
    async def test_check_budget_excludes_reservation_id(self):
        """check_budget forwards exclude_reservation_id to _today_usage."""
        from unittest.mock import AsyncMock
        import uuid

        db = AsyncMock()
        res_id = uuid.uuid4()
        # With reservation included, usage = 8000 tokens, which exceeds 5000 budget.
        # With reservation excluded, usage = 0, which is within budget.
        calls = []

        async def mock_today_usage(db_, project_id, exclude_reservation_id=None):
            calls.append(exclude_reservation_id)
            if exclude_reservation_id == res_id:
                return (0, 0.0)
            return (8000, 0.50)

        with patch.object(budget, "_today_usage", side_effect=mock_today_usage):
            result = await budget.check_budget(
                db, self._cfg(daily_token_budget=5000),
                exclude_reservation_id=res_id,
            )
        assert result is None
        assert calls == [res_id]

    @pytest.mark.asyncio
    async def test_check_budget_without_reservation_includes_all(self):
        """When no reservation_id is passed, all rows count."""
        from unittest.mock import AsyncMock

        db = AsyncMock()
        calls = []

        async def mock_today_usage(db_, project_id, exclude_reservation_id=None):
            calls.append(exclude_reservation_id)
            return (8000, 0.50)

        with patch.object(budget, "_today_usage", side_effect=mock_today_usage):
            result = await budget.check_budget(
                db, self._cfg(daily_token_budget=5000),
            )
        assert result == "daily_token_budget_exceeded"
        assert calls == [None]

    @pytest.mark.asyncio
    async def test_small_budget_not_bricked_by_reservation(self):
        """A project with daily_token_budget=5000 (below the 8096 reservation
        estimate) must not be refused on the first turn of a fresh day when
        the only usage is the reservation itself."""
        from unittest.mock import AsyncMock
        import uuid

        db = AsyncMock()
        res_id = uuid.uuid4()

        async def mock_today_usage(db_, project_id, exclude_reservation_id=None):
            if exclude_reservation_id == res_id:
                return (0, 0.0)  # nothing besides the reservation
            return (8096, 0.10)  # reservation included

        with patch.object(budget, "_today_usage", side_effect=mock_today_usage):
            result = await budget.check_budget(
                db, self._cfg(daily_token_budget=5000),
                exclude_reservation_id=res_id,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_today_usage_compiles_exclusion_predicate(self):
        """Arithmetic guard — the mocked check_budget tests above verify only
        that check_budget forwards the id; they never exercise the SQL. This
        test compiles the statement _today_usage actually builds and asserts
        the reservation row is filtered at the query level (``id != :id``) when
        an exclude id is given, and NOT filtered when it is absent. A regression
        that dropped or inverted the exclusion (e.g. ``==`` or wrong column)
        would silently re-introduce the self-count Bug-7777 despite the mocks
        passing."""
        from unittest.mock import AsyncMock
        import uuid

        from shared.db.models import AgentCostEntry

        res_id = uuid.uuid4()
        project_id = uuid.uuid4()
        captured = {}

        async def _execute(stmt):
            compiled = stmt.compile(compile_kwargs={"literal_binds": False})
            captured["sql"] = str(compiled)
            captured["params"] = dict(compiled.params)
            row = MagicMock()
            row.in_tok, row.out_tok, row.cost = 0, 0, 0.0
            result = MagicMock()
            result.one.return_value = row
            return result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)

        # With exclusion: the predicate must be present and target the PK column.
        await budget._today_usage(db, project_id, exclude_reservation_id=res_id)
        with_excl = captured["sql"]
        col = AgentCostEntry.id.name  # actual mapped column name
        assert f"{AgentCostEntry.__tablename__}.{col} !=" in with_excl
        # The bound value must be the reservation id itself — a regression that
        # binds a different UUID (e.g. project_id) into the predicate would
        # otherwise pass on predicate shape alone.
        assert res_id in captured["params"].values()
        assert project_id in captured["params"].values()  # WHERE project_id = ...

        # Without exclusion: no id-inequality predicate is emitted, and the
        # reservation id is bound nowhere.
        await budget._today_usage(db, project_id)
        without_excl = captured["sql"]
        assert f"{AgentCostEntry.__tablename__}.{col} !=" not in without_excl
        assert res_id not in captured["params"].values()


class TestRunTurnBudgetReservationThreading:
    """Bug-7777 — pins the CALL-SITE threading: run_turn must forward its
    ``budget_reservation_id`` to check_budget as ``exclude_reservation_id``.

    The TestBudgetReservationExclusion tests exercise check_budget directly,
    so silently dropping the kwarg at the run_turn call site (or the
    ``budget_reservation_id=`` kwarg in conversations.py) would keep them
    green while re-introducing the user-facing brick (every turn refused for
    daily_token_budget <= 8096). This test fails if the pipeline call site
    stops forwarding the id."""

    @pytest.mark.asyncio
    async def test_run_turn_forwards_reservation_id_to_check_budget(self):
        import types
        import uuid
        from unittest.mock import AsyncMock

        from src import pipeline

        res_id = uuid.uuid4()
        db = AsyncMock()
        cfg = MagicMock()
        cfg.judge_mode = "async"
        conversation = MagicMock()
        conversation.id = uuid.uuid4()

        bundle = types.SimpleNamespace(
            system="s", user="u", allow_list_model_ids=[uuid.uuid4()],
        )
        # Refusal reason short-circuits run_turn right after the budget gate,
        # so nothing beyond the call under test executes.
        check_budget_mock = AsyncMock(return_value="daily_token_budget_exceeded")

        with (
            patch.object(
                pipeline, "scan_input_message",
                return_value=types.SimpleNamespace(
                    ok=True, reason=None, matched_topic=None
                ),
            ),
            patch.object(pipeline, "assemble_prompt", AsyncMock(return_value=bundle)),
            patch.object(
                pipeline, "resolve_agent_llm_failover_configs",
                AsyncMock(return_value=[MagicMock()]),
            ),
            patch.object(pipeline, "check_budget", check_budget_mock),
        ):
            outcome = await pipeline.run_turn(
                db=db, cfg=cfg, conversation=conversation,
                user_message="q", jwt_token="t",
                budget_reservation_id=res_id,
            )

        assert outcome.status == "refused"
        check_budget_mock.assert_awaited_once_with(
            db, cfg, exclude_reservation_id=res_id,
        )

    @pytest.mark.asyncio
    async def test_run_turn_passes_none_when_no_reservation(self):
        """When no reservation was written (budgets disabled), run_turn must
        pass exclude_reservation_id=None so nothing is excluded."""
        import types
        import uuid
        from unittest.mock import AsyncMock

        from src import pipeline

        db = AsyncMock()
        cfg = MagicMock()
        cfg.judge_mode = "async"
        conversation = MagicMock()
        conversation.id = uuid.uuid4()

        bundle = types.SimpleNamespace(
            system="s", user="u", allow_list_model_ids=[uuid.uuid4()],
        )
        check_budget_mock = AsyncMock(return_value="daily_token_budget_exceeded")

        with (
            patch.object(
                pipeline, "scan_input_message",
                return_value=types.SimpleNamespace(
                    ok=True, reason=None, matched_topic=None
                ),
            ),
            patch.object(pipeline, "assemble_prompt", AsyncMock(return_value=bundle)),
            patch.object(
                pipeline, "resolve_agent_llm_failover_configs",
                AsyncMock(return_value=[MagicMock()]),
            ),
            patch.object(pipeline, "check_budget", check_budget_mock),
        ):
            await pipeline.run_turn(
                db=db, cfg=cfg, conversation=conversation,
                user_message="q", jwt_token="t",
            )

        check_budget_mock.assert_awaited_once_with(
            db, cfg, exclude_reservation_id=None,
        )


class TestPostTurnReservationExclusion:
    """Bug-7777 (post-turn) — the post-turn check must also exclude this
    turn's reservation row. On the happy path the callers reconcile (delete)
    the reservation before persist_turn, so the exclusion is a no-op; when
    reconcile FAILS (its exception is swallowed with a warning) the stale
    8096-token reservation would otherwise be counted ON TOP of the turn's
    real spend, spuriously flagging a budget breach. The exclusion also makes
    the check independent of the reconcile/persist call ordering."""

    @pytest.mark.asyncio
    async def test_check_budget_post_turn_forwards_exclusion(self):
        from unittest.mock import AsyncMock
        import uuid

        db = AsyncMock()
        res_id = uuid.uuid4()
        cfg = MagicMock()
        cfg.project_id = uuid.uuid4()
        cfg.daily_token_budget = 5000
        cfg.daily_cost_budget_usd = 0
        calls = []

        async def mock_today_usage(db_, project_id, exclude_reservation_id=None):
            calls.append(exclude_reservation_id)
            # Stale reservation excluded -> only real spend remains.
            return (0, 0.0) if exclude_reservation_id == res_id else (8096, 0.10)

        with patch.object(budget, "_today_usage", side_effect=mock_today_usage):
            result = await budget.check_budget_post_turn(
                db, cfg,
                turn_input_tokens=100, turn_output_tokens=50,
                provider="anthropic",
                exclude_reservation_id=res_id,
            )
        assert calls == [res_id]
        # 150 turn tokens against a 5000 budget: within limits once the stale
        # reservation is excluded; counting it would spuriously breach.
        assert result is None

    @pytest.mark.asyncio
    async def test_persist_turn_forwards_reservation_id_to_post_turn_check(self):
        """Call-site pin — persist_turn must forward budget_reservation_id to
        check_budget_post_turn; dropping the kwarg would keep the direct unit
        test green while reintroducing the reconcile-failure double-count."""
        import time as _time
        import uuid
        from unittest.mock import AsyncMock

        from src import pipeline

        res_id = uuid.uuid4()
        db = AsyncMock()
        lookup = MagicMock()
        lookup.scalar_one_or_none.return_value = None  # no existing turn row
        db.execute = AsyncMock(return_value=lookup)
        db.add = MagicMock()

        cfg = MagicMock()
        conversation = MagicMock()
        conversation.id = uuid.uuid4()

        outcome = MagicMock()
        outcome.usage_input_tokens = 10
        outcome.usage_output_tokens = 5
        outcome.rows_returned = 0
        outcome.guardrail_actions = []

        post_check = AsyncMock(return_value=None)
        with (
            patch.object(pipeline, "check_budget_post_turn", post_check),
            patch.object(pipeline, "record_turn_cost", AsyncMock()),
        ):
            await pipeline.persist_turn(
                db=db,
                conversation=conversation,
                turn_index=0,
                user_message="q",
                outcome=outcome,
                started_monotonic=_time.monotonic(),
                cfg=cfg,
                llm_provider="anthropic",
                budget_reservation_id=res_id,
            )

        post_check.assert_awaited_once_with(
            db, cfg,
            turn_input_tokens=10,
            turn_output_tokens=5,
            provider="anthropic",
            exclude_reservation_id=res_id,
        )


class TestConversationsReservationThreading:
    """Bug-7777 — pins the OUTER hop of the reservation-id chain: the
    conversations.py entry points must pass the id returned by
    reserve_budget into BOTH run_turn (pre-turn gate) and persist_turn
    (post-turn gate). The pipeline-level threading tests cover the inner
    hops only; dropping any of the four ``budget_reservation_id=`` kwargs
    in conversations.py would default the id to None and silently revert
    the fix while every other test stays green."""

    def _cfg(self):
        cfg = MagicMock()
        cfg.project_id = _uuid.uuid4()
        cfg.enabled = True
        cfg.daily_token_budget = 5000  # reservation path active
        cfg.daily_cost_budget_usd = 0
        cfg.judge_mode = "async"
        return cfg

    def _outcome(self):
        outcome = MagicMock()
        outcome.status = "refused"  # skips judge/webhook-heavy branches
        outcome.provider = "anthropic"
        return outcome

    @pytest.mark.asyncio
    async def test_sync_send_message_threads_reservation_id(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.api import conversations as conv_mod

        res_id = _uuid.uuid4()
        project_id = _uuid.uuid4()
        conversation_id = _uuid.uuid4()

        db = AsyncMock()
        conv = MagicMock()
        conv.project_id = project_id
        conv.persona_id = None
        db.get = AsyncMock(return_value=conv)

        async def fake_get_tenant_db(tenant_id):
            yield db

        cfg = self._cfg()
        run_turn_mock = AsyncMock(return_value=self._outcome())
        persist_mock = AsyncMock(return_value=MagicMock())
        reconcile_mock = AsyncMock()

        with (
            patch.object(conv_mod, "_enforce_project_scope", MagicMock()),
            patch.object(conv_mod, "get_tenant_db", fake_get_tenant_db),
            patch.object(
                conv_mod, "_require_project_access_and_agent",
                AsyncMock(return_value=cfg),
            ),
            patch.object(conv_mod, "_enforce_conversation_ownership", MagicMock()),
            patch.object(
                conv_mod, "_reserve_turn",
                AsyncMock(return_value=SimpleNamespace(
                    is_duplicate=False, existing_turn_id=None, turn_index=0,
                )),
            ),
            patch.object(conv_mod, "reserve_budget", AsyncMock(return_value=res_id)),
            patch.object(conv_mod, "run_turn", run_turn_mock),
            patch.object(conv_mod, "reconcile_budget_reservation", reconcile_mock),
            patch.object(conv_mod, "persist_turn", persist_mock),
            patch.object(conv_mod, "_emit_turn_webhook", MagicMock()),
            patch.object(conv_mod, "_resolve_persona_name", AsyncMock(return_value=None)),
            patch.object(conv_mod, "_redact_trace", MagicMock(return_value=MagicMock())),
        ):
            current_user = MagicMock()
            current_user.tenant_id = "t1"
            current_user.raw_token = "tok"
            body = MagicMock()
            body.text = "q"

            await conv_mod.send_message(
                project_id=project_id,
                conversation_id=conversation_id,
                body=body,
                background_tasks=MagicMock(),
                current_user=current_user,
                idempotency_key=None,
            )

        assert run_turn_mock.await_args.kwargs["budget_reservation_id"] == res_id
        assert persist_mock.await_args.kwargs["budget_reservation_id"] == res_id
        reconcile_mock.assert_awaited_once_with("t1", res_id)

    @pytest.mark.asyncio
    async def test_streaming_run_turn_into_publisher_threads_reservation_id(self):
        import time as _time
        from unittest.mock import AsyncMock

        from src.api import conversations as conv_mod

        res_id = _uuid.uuid4()
        project_id = _uuid.uuid4()
        conversation_id = _uuid.uuid4()

        cfg = self._cfg()
        conv = MagicMock()
        conv.project_id = project_id
        conv.caller_ref = None
        conv.persona_id = None

        db = AsyncMock()
        cfg_row = MagicMock()
        cfg_row.scalar_one_or_none.return_value = cfg
        db.execute = AsyncMock(return_value=cfg_row)
        db.get = AsyncMock(return_value=conv)

        async def fake_get_tenant_db(tenant_id):
            yield db

        publisher = AsyncMock()
        run_turn_mock = AsyncMock(return_value=self._outcome())
        persist_mock = AsyncMock(return_value=MagicMock())
        reconcile_mock = AsyncMock()

        redacted = MagicMock()
        with (
            patch.object(conv_mod, "get_tenant_db", fake_get_tenant_db),
            patch.object(conv_mod, "reserve_budget", AsyncMock(return_value=res_id)),
            patch.object(conv_mod, "run_turn", run_turn_mock),
            patch.object(conv_mod, "reconcile_budget_reservation", reconcile_mock),
            patch.object(conv_mod, "persist_turn", persist_mock),
            patch.object(conv_mod, "_emit_turn_webhook", MagicMock()),
            patch.object(conv_mod, "_resolve_persona_name", AsyncMock(return_value=None)),
            patch.object(conv_mod, "_redact_trace", MagicMock(return_value=redacted)),
        ):
            await conv_mod._run_turn_into_publisher(
                tenant_id="t1",
                project_id=project_id,
                conversation_id=conversation_id,
                turn_index=0,
                user_message="q",
                jwt_token="tok",
                started=_time.monotonic(),
                publisher=publisher,
            )

        assert run_turn_mock.await_args.kwargs["budget_reservation_id"] == res_id
        assert persist_mock.await_args.kwargs["budget_reservation_id"] == res_id
        reconcile_mock.assert_awaited_once_with("t1", res_id)


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


class TestOrphanedReservationDetection:
    """Finding: intake ``2026-08-11-orphaned-budget-reservations-hold-budget-
    and-inflate-the-cost-report.md``.

    ``reserve_budget`` commits an ~8096-token estimate row and
    ``reconcile_budget_reservation`` deletes it. A process death between the two
    (SIGKILL, OOM, container restart, post-grace-period task cancellation)
    leaves the estimate behind: it holds the project's daily allowance until UTC
    midnight and is reported as spend by ``/cost`` for the whole lookback
    window.

    The behavioural proof runs against real PostgreSQL in
    ``tests/integration/test_orphaned_budget_reservation_db.py``. These are the
    no-database guards so the invariant is still protected when that suite
    skips.
    """

    def test_reservation_predicate_is_null_safe(self):
        """The predicate must render ``IS NOT DISTINCT FROM``, not ``=``.

        ``provider = '__reservation__'`` evaluates to NULL for a row whose
        provider is NULL — which ``record_turn_cost`` writes whenever the
        provider string is empty, and which every pre-migration row carries.
        Negating NULL drops the row from the WHERE clause, so real spend would
        disappear from both the daily budget sum and the cost report. That is a
        budget bypass, not a false refusal, so the two-valued form is
        load-bearing."""
        sql = str(budget.is_reservation_row().compile())
        assert "IS NOT DISTINCT FROM" in sql.upper()

    def test_orphan_predicate_requires_both_identity_and_age(self):
        """A LIVE reservation must keep holding budget (that is the whole point
        of Bug-7366); only one past the max age is treated as orphaned."""
        sql = str(
            budget.is_orphaned_reservation().compile(
                compile_kwargs={"literal_binds": False}
            )
        ).upper()
        assert "IS NOT DISTINCT FROM" in sql
        assert "CREATED_AT <" in sql

    @pytest.mark.asyncio
    async def test_today_usage_emits_the_orphan_exclusion(self):
        """The sum ``check_budget`` runs must carry the exclusion. A regression
        that dropped it silently restores the crashed-turn budget hold."""
        from unittest.mock import AsyncMock

        captured = {}

        async def _execute(stmt):
            compiled = stmt.compile(compile_kwargs={"literal_binds": False})
            captured["sql"] = str(compiled)
            captured["params"] = dict(compiled.params)
            row = MagicMock()
            row.in_tok, row.out_tok, row.cost = 0, 0, 0.0
            result = MagicMock()
            result.one.return_value = row
            return result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)
        await budget._today_usage(db, _uuid.uuid4())

        assert "IS NOT DISTINCT FROM" in captured["sql"].upper()
        assert budget._RESERVATION_PROVIDER in captured["params"].values()

    def test_reservation_provider_cannot_collide_with_real_spend(self):
        """``record_turn_cost`` only runs after a successful LLM call, and
        ``build_adapter`` refuses any provider outside its closed set — so no
        completed turn can ever write the sentinel, whatever a tenant admin
        types into the unconstrained ``LLMProviderConfig.provider`` column."""
        from shared.llm.adapter import LLMConfig, build_adapter

        cfg = LLMConfig(
            provider=budget._RESERVATION_PROVIDER,
            display_name="spoof",
            base_url=None,
            api_key="k",
            model_name="m",
            max_tokens=16,
            temperature=0.0,
            timeout_seconds=5,
            config={},
        )
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            build_adapter(cfg)

    def test_max_age_falls_back_on_a_bad_override(self):
        """A malformed or non-positive AGENT_RESERVATION_MAX_AGE_MINUTES must
        not disable orphan detection (a 0 or negative cutoff would classify
        every LIVE reservation as an orphan and remove the pessimistic hold)."""
        default = budget._DEFAULT_RESERVATION_MAX_AGE_MINUTES
        for bad in ("", "abc", "0", "-5"):
            with patch.dict(
                os.environ, {"AGENT_RESERVATION_MAX_AGE_MINUTES": bad}
            ):
                assert budget._reservation_max_age_minutes() == default
        with patch.dict(os.environ, {"AGENT_RESERVATION_MAX_AGE_MINUTES": "45"}):
            assert budget._reservation_max_age_minutes() == 45


class TestSweepOrphanedReservations:
    """ws2#26 — the housekeeping half of orphan handling. ``_today_usage`` and
    the cost report already EXCLUDE orphaned reservation rows from their sums;
    ``sweep_orphaned_reservations`` physically DELETEs them so the ledger does
    not grow one stranded ~8096-token row per crash forever. It reuses the SAME
    NULL-safe, config-bounded orphan predicate the exclusion uses, so a LIVE
    reservation or a real-spend row (even one with a NULL provider) is never
    deleted."""

    async def _capture(self, project_id=None, rowcount=0):
        from unittest.mock import AsyncMock

        captured = {}

        async def _execute(stmt):
            compiled = stmt.compile(compile_kwargs={"literal_binds": False})
            captured["sql"] = str(compiled)
            captured["params"] = dict(compiled.params)
            result = MagicMock()
            result.rowcount = rowcount
            return result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)
        deleted = await budget.sweep_orphaned_reservations(db, project_id=project_id)
        return deleted, captured

    @pytest.mark.asyncio
    async def test_sweep_targets_only_orphaned_reservation_rows(self):
        """The DELETE must carry the NULL-safe reservation predicate AND the
        age bound, so it can only remove rows the exclusion already treats as
        orphaned — never a live reservation, never real spend."""
        deleted, captured = await self._capture(rowcount=3)
        sql = captured["sql"].upper()
        assert sql.strip().startswith("DELETE")
        from shared.db.models import AgentCostEntry
        assert AgentCostEntry.__tablename__ in captured["sql"]
        # NULL-safe reservation identity + age bound (== is_orphaned_reservation).
        assert "IS NOT DISTINCT FROM" in sql
        assert "CREATED_AT <" in sql
        assert budget._RESERVATION_PROVIDER in captured["params"].values()
        # rowcount is returned as the reclaimed count.
        assert deleted == 3

    @pytest.mark.asyncio
    async def test_sweep_scopes_to_project_when_given(self):
        import uuid

        from shared.db.models import AgentCostEntry

        project_id = uuid.uuid4()
        _, captured = await self._capture(project_id=project_id)
        assert project_id in captured["params"].values()
        assert f"{AgentCostEntry.__tablename__}.project_id =".lower() \
            in captured["sql"].lower()

    @pytest.mark.asyncio
    async def test_sweep_tenant_wide_has_no_project_predicate(self):
        import uuid

        project_id = uuid.uuid4()
        _, captured = await self._capture(project_id=None)
        # No project scoping bound when project_id is omitted.
        assert project_id not in captured["params"].values()
        assert "project_id =" not in captured["sql"].lower()

    @pytest.mark.asyncio
    async def test_sweep_uses_the_configured_max_age(self):
        """The age bound is the config-driven cutoff (shared with the exclusion),
        not a literal — a bad override falls back to the default rather than
        reaping live reservations."""
        default = budget._DEFAULT_RESERVATION_MAX_AGE_MINUTES
        for bad in ("", "abc", "0", "-5"):
            with patch.dict(
                os.environ, {"AGENT_RESERVATION_MAX_AGE_MINUTES": bad}
            ):
                assert budget._reservation_max_age_minutes() == default


class TestCostReportExcludesReservations:
    """The cost report is the second consumer of the same ledger. The invariant
    "a reservation is not spend" has to hold at BOTH read sites, or an orphan
    keeps being reported as money spent even after the budget stops holding
    it."""

    @pytest.mark.asyncio
    async def test_cost_report_query_carries_the_reservation_exclusion(self):
        """Guards the shared-primitive gap directly: the ledger aggregation in
        ``get_cost`` is the only other place ``AgentCostEntry`` is summed, and
        it must carry the same NULL-safe reservation predicate the budget sum
        does."""
        import types
        from unittest.mock import AsyncMock

        from src.api import kpis

        statements = []

        async def _execute(stmt):
            compiled = stmt.compile(compile_kwargs={"literal_binds": False})
            statements.append((str(compiled), dict(compiled.params)))
            result = MagicMock()
            result.all.return_value = []
            return result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)

        async def _tenant_db(_tenant_id):
            yield db

        user = types.SimpleNamespace(tenant_id="t", user_id="u")
        with patch.object(kpis, "get_tenant_db", _tenant_db), patch.object(
            kpis, "_require_project_viewer", AsyncMock()
        ):
            await kpis.get_cost(
                project_id=_uuid.uuid4(), window_days=30, current_user=user
            )

        ledger_stmts = [
            (sql, params)
            for sql, params in statements
            if "agent_cost_ledger" in sql
        ]
        assert ledger_stmts, "the cost report must still read the ledger"
        for sql, params in ledger_stmts:
            # SQLAlchemy folds ``NOT (x IS NOT DISTINCT FROM y)`` into
            # ``x IS DISTINCT FROM y``; either rendering is the NULL-safe form,
            # a plain ``=``/``!=`` is not.
            assert "DISTINCT FROM" in sql.upper()
            assert budget._RESERVATION_PROVIDER in params.values()
