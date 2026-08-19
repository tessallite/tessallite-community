"""Regression tests for the agent-service / conversational-client remediation
batch (Bug-6045, Bug-6330, Bug-6331, Bug-6332, Bug-6334).

Each test asserts the user-visible / security-relevant behaviour the fix
guarantees, not an implementation detail:

  - Bug-6045 [SEC] the agent webhook dispatcher applies the shared SSRF guard
    at BOTH config-write and dispatch time (no bypass, fail CLOSED to DLQ).
  - Bug-6330 the combine ``sum`` operator totals scalar arguments instead of
    silently evaluating to None.
  - Bug-6331 the query-complexity guardrail counts structured predicates and
    projections, so a query built entirely from structured forms cannot slip
    past ``max_query_complexity``.
  - Bug-6332 [SEC] sync judge mode fails CLOSED: an ``unknown`` verdict (judge
    could not run) withholds the answer rather than releasing it.
  - Bug-6334 the eval harness records LLM spend in the budget ledger and
    stops when the daily budget is exhausted.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch as _patch

import pytest


# ---------------------------------------------------------------------------
# Bug-6045 — SSRF guard on the agent webhook dispatcher
# ---------------------------------------------------------------------------


def _mock_tenant_db_factory(cfg, sink: list | None = None):
    async def _gen(tenant_id):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = cfg
        db.execute = AsyncMock(return_value=result)
        if sink is not None:
            db.add = lambda row: sink.append(row)
            db.commit = AsyncMock()
        yield db
    return _gen


class TestWebhookSSRFGuard:
    """Bug-6045 — a tenant modeller must not be able to point the agent webhook
    at an internal / metadata / loopback host."""

    def test_config_write_rejects_internal_url(self):
        """PUT/PATCH validation refuses an SSRF-unsafe URL with HTTP 400."""
        from fastapi import HTTPException
        from src.api.agent_config import _validate_webhook_url

        for bad in (
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8001/internal",
            "http://127.0.0.1/hook",
            "http://10.0.0.5/hook",
            "ftp://example.com/hook",
        ):
            with pytest.raises(HTTPException) as exc:
                _validate_webhook_url(bad)
            assert exc.value.status_code == 400

    def test_config_write_accepts_public_url_and_blank(self):
        from src.api.agent_config import _validate_webhook_url

        # Public https and a blank/None (clearing the webhook) are accepted.
        _validate_webhook_url("https://hooks.example.com/agent")
        _validate_webhook_url(None)
        _validate_webhook_url("   ")

    @pytest.mark.asyncio
    async def test_dispatch_rejects_internal_url_to_dlq_without_http(self):
        """Even a URL stored before the guard existed must be refused at
        dispatch time — parked in the DLQ, never POSTed."""
        from src.webhooks import dispatcher

        cfg = types.SimpleNamespace(
            enabled=True,
            webhook_url="http://169.254.169.254/latest/meta-data/",
            webhook_signing_secret=None,
        )
        dlq_rows: list = []
        post_calls: list = []

        async def fake_post_once(tenant_id, url, body, sig_header, event_type):
            post_calls.append(url)
            return True, 200, None

        with (
            _patch.object(dispatcher, "get_tenant_db", _mock_tenant_db_factory(cfg, dlq_rows)),
            _patch.object(dispatcher, "_post_once", side_effect=fake_post_once),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
            )

        assert post_calls == [], "SSRF-unsafe URL must never be POSTed"
        assert len(dlq_rows) == 1, "rejected dispatch must be parked in the DLQ"
        assert "SSRF" in (dlq_rows[0].last_error or "")

    @pytest.mark.asyncio
    async def test_dispatch_allows_public_url(self):
        """A public URL still dispatches normally (guard does not over-block).

        Bug-8349: dispatch also requires a valid signing secret now (a
        missing secret is refused terminally, independent of the URL), so
        this SSRF-focused test must supply one -- otherwise it would be
        testing the (unrelated) signing-refusal path instead of what it
        says it tests."""
        from src.webhooks import dispatcher

        cfg = types.SimpleNamespace(
            enabled=True,
            webhook_url="https://hooks.example.com/agent",
            webhook_signing_secret=b"encrypted-bytes",
        )
        post_calls: list = []

        async def fake_post_once(tenant_id, url, body, sig_header, event_type):
            post_calls.append(url)
            return True, 200, None

        with (
            _patch.object(dispatcher, "get_tenant_db", _mock_tenant_db_factory(cfg)),
            _patch.object(dispatcher, "_decrypt_secret", return_value="a-real-strong-secret"),
            _patch.object(dispatcher, "_post_once", side_effect=fake_post_once),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
            )

        assert post_calls == ["https://hooks.example.com/agent"]

    def test_shared_client_transport_backed_by_ssrf_backend(self):
        """The shared long-lived httpx client pins resolved IPs via the SSRF
        network backend, so a target that resolves to a private/metadata
        address is refused at connect time (DNS-rebinding TOCTOU close).
        Asserted on the actual constructed transport, not by source text."""
        import asyncio

        from shared.webhooks.ssrf import _SSRFSafeBackend
        from src.webhooks import dispatcher

        asyncio.run(dispatcher.init_client())
        try:
            backend = dispatcher._shared_client._transport._pool._network_backend
            assert isinstance(backend, _SSRFSafeBackend)
        finally:
            asyncio.run(dispatcher.close_client())

    @pytest.mark.asyncio
    async def test_post_once_fallback_client_uses_ssrf_backend(self):
        """When the shared client is not yet initialised, the one-shot fallback
        client must ALSO be built with the SSRF-safe transport — otherwise the
        fallback path would be an unguarded send route."""
        from shared.webhooks.ssrf import _SSRFSafeBackend
        from src.webhooks import dispatcher

        captured: dict = {}
        resp = MagicMock(status_code=200)

        class _FakeStream:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return False

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                captured["transport"] = kwargs.get("transport")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, *args, **kwargs):
                # Bug-7334 class (Bug-8349 R2 gate residual) — _post_once
                # now streams via client.stream() instead of client.post()
                # for bounded response reading.
                return _FakeStream()

        with (
            _patch.object(dispatcher, "_shared_client", None),
            _patch.object(dispatcher.httpx, "AsyncClient", _FakeClient),
        ):
            ok, status, err = await dispatcher._post_once(
                "acme", "https://example.com/hook", b"{}", "", "turn.completed",
            )

        assert ok is True
        transport = captured["transport"]
        assert transport is not None, "fallback client built without a transport"
        assert isinstance(transport._pool._network_backend, _SSRFSafeBackend)


# ---------------------------------------------------------------------------
# Bug-6330 — combine ``sum`` totals scalar arguments
# ---------------------------------------------------------------------------


class TestCombineSumScalar:
    def _sum(self, *args):
        from src.recipes.eval import evaluate_combine

        node = {"op": "sum", "args": [{"const": a} for a in args]}
        return evaluate_combine(node, {})

    def test_sum_two_scalars(self):
        assert self._sum(100, 200) == 300

    def test_sum_three_scalars(self):
        assert self._sum(10, 20, 30) == 60

    def test_sum_single_scalar(self):
        assert self._sum(42) == 42

    def test_sum_none_arg_propagates_none(self):
        from src.recipes.eval import evaluate_combine

        node = {"op": "sum", "args": [{"const": 10}, {"ref": {"step": "s", "measure": "m"}}]}
        assert evaluate_combine(node, {"s": {"m": None}}) is None

    def test_min_max_single_scalar(self):
        from src.recipes.eval import evaluate_combine

        assert evaluate_combine({"op": "min", "args": [{"const": 5}]}, {}) == 5
        assert evaluate_combine({"op": "max", "args": [{"const": 5}]}, {}) == 5

    def test_min_max_multi_scalar(self):
        from src.recipes.eval import evaluate_combine

        assert evaluate_combine(
            {"op": "min", "args": [{"const": 9}, {"const": 3}, {"const": 7}]}, {}
        ) == 3
        assert evaluate_combine(
            {"op": "max", "args": [{"const": 9}, {"const": 3}, {"const": 7}]}, {}
        ) == 9


# ---------------------------------------------------------------------------
# Bug-6331 — complexity guardrail counts structured predicates + projections
# ---------------------------------------------------------------------------


class TestComplexityCountsStructured:
    def _cfg(self, max_complexity):
        cfg = MagicMock()
        cfg.max_query_complexity = max_complexity
        cfg.project_id = "p"
        return cfg

    def _query(self, body):
        from src.tools.spec import _parse_query

        return _parse_query(body)

    def test_structured_or_predicate_counts_leaves(self):
        """A single ``where`` entry that is an OR of 3 comparisons contributes
        3 to the complexity, not 0 (it lives in where_refs, not the flat list)."""
        from src.guardrails.budget import check_query_complexity

        call = self._query({
            "model_id": str(uuid.uuid4()),
            "measures": ["revenue"],
            "dimensions": [],
            "where": [
                {"or": [
                    {"left": {"field": "country"}, "op": "eq", "right": {"literal": "GB"}},
                    {"left": {"field": "country"}, "op": "eq", "right": {"literal": "US"}},
                    {"left": {"field": "country"}, "op": "eq", "right": {"literal": "DE"}},
                ]}
            ],
        })
        # measures(1) + 3 leaf comparisons = 4. Limit 3 -> blocked.
        assert check_query_complexity(self._cfg(3), call) == "query_too_complex"
        # Limit 4 -> exactly at limit, allowed.
        assert check_query_complexity(self._cfg(4), call) is None

    def test_projection_refs_count(self):
        from src.guardrails.budget import check_query_complexity

        call = self._query({
            "model_id": str(uuid.uuid4()),
            "measures": ["revenue"],
            "dimensions": [],
            "projections": [
                {"expr": {"arith": "mul", "left": {"field": "revenue"},
                          "right": {"literal": 2}}, "alias": "double_rev"},
            ],
        })
        # measures(1) + projection(1) = 2. Limit 1 -> blocked.
        assert check_query_complexity(self._cfg(1), call) == "query_too_complex"

    def test_pure_structured_query_not_invisible(self):
        """The original bug: a query with only structured predicates had
        where=[] so complexity was under-counted and never blocked."""
        from src.guardrails.budget import check_query_complexity

        call = self._query({
            "model_id": str(uuid.uuid4()),
            "measures": ["revenue"],
            "dimensions": [],
            "where": [
                {"and": [
                    {"left": {"field": "a"}, "op": "gt", "right": {"literal": 1}},
                    {"left": {"field": "b"}, "op": "lt", "right": {"literal": 9}},
                ]}
            ],
        })
        # measures(1) + 2 leaves = 3 > limit 2 -> blocked.
        assert check_query_complexity(self._cfg(2), call) == "query_too_complex"


# ---------------------------------------------------------------------------
# Bug-6332 — sync judge fails CLOSED on an unknown verdict
# ---------------------------------------------------------------------------


class TestJudgeFailClosed:
    def _cfg(self, judge_mode):
        return types.SimpleNamespace(
            judge_mode=judge_mode,
            judge_block_visibility="transparent",
        )

    def _turn(self):
        return types.SimpleNamespace(
            answer_text="The total is 42.",
            llm_plan={},
            status="ok",
            guardrail_actions=[],
        )

    def _outcome(self, verdict):
        from src.judge.judge import JudgeOutcome

        return JudgeOutcome(verdict=verdict, reasoning="", metrics={})

    def test_sync_unknown_verdict_blocks(self):
        """A judge that could not run (verdict 'unknown') must WITHHOLD the
        answer in sync mode — the core no-unvetted-answer guarantee."""
        from src.guardrails.block import apply_judge_block

        cfg, turn = self._cfg("sync"), self._turn()
        msg = apply_judge_block(cfg, turn, self._outcome("unknown"))
        assert turn.status == "judge_blocked"
        assert turn.answer_text == msg
        assert "The total is 42." not in turn.answer_text
        # The original answer is preserved for admin trace, not shown.
        assert turn.llm_plan["original_answer_blocked"] == "The total is 42."

    def test_sync_fail_verdict_blocks(self):
        from src.guardrails.block import apply_judge_block

        cfg, turn = self._cfg("sync"), self._turn()
        apply_judge_block(cfg, turn, self._outcome("fail"))
        assert turn.status == "judge_blocked"

    def test_sync_pass_and_warn_released(self):
        from src.guardrails.block import apply_judge_block

        for verdict in ("pass", "warn"):
            cfg, turn = self._cfg("sync"), self._turn()
            apply_judge_block(cfg, turn, self._outcome(verdict))
            assert turn.status == "ok"
            assert turn.answer_text == "The total is 42."

    def test_async_unknown_not_retroactively_blocked(self):
        """Async mode delivers before vetting by design; an 'unknown' verdict
        must NOT retroactively withhold an already-shown answer (only 'fail'
        blocks in async)."""
        from src.guardrails.block import apply_judge_block

        cfg, turn = self._cfg("async"), self._turn()
        apply_judge_block(cfg, turn, self._outcome("unknown"))
        assert turn.status == "ok"
        assert turn.answer_text == "The total is 42."

    def _fresh_turn(self):
        return types.SimpleNamespace(
            id=uuid.uuid4(),
            answer_text="The secret total is 42.",
            llm_plan={},
            status="ok",
            guardrail_actions=[],
            judge_verdict=None,
            judge_reasoning=None,
            judge_metrics=None,
        )

    @pytest.mark.asyncio
    async def test_fail_closed_helper_blocks_on_orchestration_error(self):
        """Bug-6332 (Codex finding) — when the sync-judge orchestration raises
        AFTER the 'ok' turn is committed, _fail_closed_judge_block must withhold
        the original answer (re-fetch on a FRESH session, synthesize 'unknown',
        block) so no unvetted answer stays persisted/retrievable."""
        from src.api import conversations
        from src.api.conversations import _fail_closed_judge_block

        turn = self._fresh_turn()
        cfg = self._cfg("sync")

        caller_db = MagicMock()
        caller_db.rollback = AsyncMock()

        # Fresh session (R2 fix): the authoritative block runs on a NEW session,
        # not the possibly-poisoned caller session.
        fresh_db = MagicMock()
        fresh_db.get = AsyncMock(return_value=turn)
        fresh_db.commit = AsyncMock()
        fresh_db.refresh = AsyncMock()

        async def _fresh_session(tenant_id):
            yield fresh_db

        with _patch.object(conversations, "get_tenant_db", _fresh_session):
            result = await _fail_closed_judge_block(caller_db, "acme", cfg, turn)

        assert result.status == "judge_blocked"
        assert "The secret total is 42." not in result.answer_text
        assert result.llm_plan["original_answer_blocked"] == "The secret total is 42."
        assert result.judge_verdict == "unknown"
        caller_db.rollback.assert_awaited()  # caller session rolled back
        fresh_db.commit.assert_awaited()      # blocked state persisted on fresh session

    @pytest.mark.asyncio
    async def test_fail_closed_helper_never_raises_and_blocks_in_memory(self):
        """R2 residual — even if the FRESH session is also unusable (total DB
        failure), the helper must NOT raise and must still block the in-memory
        turn so the response/stream withholds the original answer."""
        from src.api import conversations
        from src.api.conversations import _fail_closed_judge_block

        turn = self._fresh_turn()
        cfg = self._cfg("sync")

        caller_db = MagicMock()
        caller_db.rollback = AsyncMock(side_effect=RuntimeError("poisoned session"))

        async def _broken_session(tenant_id):
            raise RuntimeError("DB down")
            yield  # pragma: no cover

        with _patch.object(conversations, "get_tenant_db", _broken_session):
            result = await _fail_closed_judge_block(caller_db, "acme", cfg, turn)

        # No exception propagated, and the returned turn is blocked in-memory.
        assert result.status == "judge_blocked"
        assert "The secret total is 42." not in result.answer_text


# ---------------------------------------------------------------------------
# Bug-6334 — eval harness meters + records LLM spend
# ---------------------------------------------------------------------------


class TestEvalLedgerAndBudget:
    @pytest.mark.asyncio
    async def test_eval_stops_when_budget_exhausted(self):
        """The eval run must not spend a single turn once the budget is
        exhausted (fail CLOSED, no blind spend)."""
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(), answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[{"q": "How many sales?"}],
        )
        db = MagicMock()

        def _exec(stmt):
            result = MagicMock()
            # First call: agent config; then allow-list; then contexts.
            return result

        db.execute = AsyncMock()
        # cfg lookup -> cfg, allow-list -> one id, contexts -> [ctx]
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute.side_effect = [cfg_res, allow_res, ctx_res]

        run_turn_mock = AsyncMock()
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value="daily_token_budget_exceeded")),
            _patch.object(runner, "run_turn", run_turn_mock),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.budget_stopped == "daily_token_budget_exceeded"
        assert report.total == 0
        run_turn_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_eval_records_turn_cost(self):
        """Each eval turn's LLM spend is written to the ledger (record_turn_cost)
        so daily budgets and the /cost report see it."""
        from src.eval import runner

        cfg = types.SimpleNamespace(
            project_id=uuid.uuid4(), answer_llm_config_id=uuid.uuid4(),
        )
        ctx = types.SimpleNamespace(
            model_id=uuid.uuid4(),
            example_questions=[{"q": "How many sales?"}],
        )
        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        cfg_res, allow_res, ctx_res = MagicMock(), MagicMock(), MagicMock()
        cfg_res.scalar_one_or_none.return_value = cfg
        allow_res.all.return_value = [(ctx.model_id,)]
        ctx_res.scalars.return_value.all.return_value = [ctx]
        db.execute = AsyncMock(side_effect=[cfg_res, allow_res, ctx_res])

        outcome = types.SimpleNamespace(
            status="ok", plan={}, answer_text="ok",
            provider="anthropic", usage_input_tokens=100, usage_output_tokens=50,
        )
        record_mock = AsyncMock()
        with (
            _patch.object(runner, "check_budget", AsyncMock(return_value=None)),
            _patch.object(runner, "run_turn", AsyncMock(return_value=outcome)),
            _patch.object(runner, "record_turn_cost", record_mock),
        ):
            report = await runner.run_eval_for_project(db, cfg.project_id)

        assert report.total == 1
        assert record_mock.await_count == 1
        kwargs = record_mock.await_args.kwargs
        assert kwargs["provider"] == "anthropic"
        assert kwargs["input_tokens"] == 100
        assert kwargs["output_tokens"] == 50
        assert kwargs["project_id"] == cfg.project_id
