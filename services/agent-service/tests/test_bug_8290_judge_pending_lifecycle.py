"""Bug-8290 [SEC] — judge_pending turn lifecycle: a sync-mode answer turn must
be WITHHELD on every user-reachable read path until the sync judge clears it.

Test escape (the named Bug-8290 escape): in validated-first ("sync") judge mode
the answer turn was persisted status="ok" with the original answer_text BEFORE
run_judge executed, so during the judge window two user-reachable read paths
served the UNVETTED answer — (1) GET .../turns (list_turns/_redact_trace withheld
only status=="judge_blocked") and (2) an Idempotency-Key retry replayed the
persisted "ok" row. No existing test exercised either read path during the judge
step, so the leak escaped coverage.

Guard: sync answer turns are now committed in the non-releasable ``judge_pending``
state; _redact_trace/list_turns withhold answer_text + every answer-derived
artefact for judge_pending exactly as for judge_blocked; the idempotency replay
(_reserve_turn classification + _replay_turn_into_publisher) never serves a
judge_pending row's unvetted answer; the sync judge flips judge_pending -> ok
(release) on pass/warn or -> judge_blocked on block/unknown; ANY failure leaves
the durable row judge_pending = withheld (durable fail-closed, also closing the
Bug-6587 total-DB-outage residual). Each test below fails if the fix is reverted.

Tier: T1 (producer/consumer security contract — fail-closed answer exposure).
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.guardrails.block import apply_judge_block
from src.judge.judge import JudgeOutcome
from src.sse.events import EventPublisher, _END_SENTINEL


SECRET_ANSWER = "Secret revenue for Acme is 4.2M and the CFO is resigning."


def _cfg(visibility: str = "transparent", judge_mode: str = "sync"):
    return types.SimpleNamespace(
        project_id=uuid.uuid4(),
        enabled=True,
        show_thought_process=True,
        show_semantic_query=True,
        show_physical_query=True,
        judge_block_visibility=visibility,
        judge_mode=judge_mode,
        judge_llm_config_id=None,
        answer_llm_config_id=None,
    )


def _turn(status: str = "judge_pending"):
    """A committed sync answer turn in the given status. Defaults to the
    pre-verdict ``judge_pending`` state Bug-8290 introduces."""
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        turn_index=0,
        user_message="What is revenue?",
        answer_text=SECRET_ANSWER,
        status=status,
        latency_ms=100,
        llm_plan={"tool": "query"},
        thought_summary="thinking about the secret",
        semantic_query={"model_id": "m", "shape": {"shape": "kpi"}},
        routed_sql="SELECT 1",
        route="source",
        citations=[{"id": "c1"}],
        user_feedback=None,
        judge_verdict=None,
        judge_reasoning=None,
        judge_metrics=None,
        guardrail_actions=[],
        usage_input_tokens=10,
        usage_output_tokens=10,
        rendered_output="<table><tr><td>4.2M</td></tr></table>",
        chart_type="kpi",
        calculation_steps=[{"step": 1, "description": "sum"}],
        query_result_sample=[{"revenue": 4200000}],
    )


async def _drain_events(publisher: EventPublisher) -> list[dict]:
    events: list[dict] = []
    while True:
        item = await publisher._queue.get()
        if item is _END_SENTINEL:
            return events
        events.append({"event": item.name, "data": item.data})


# ---------------------------------------------------------------------------
# (a) Read path 1 — list_turns / _redact_trace withholds a judge_pending turn
# ---------------------------------------------------------------------------


class TestRedactTraceWithholdsJudgePending:
    def test_pending_turn_answer_and_artifacts_withheld(self):
        """The single serialisation gate for the REST turn list must withhold
        the unvetted answer AND every answer-derived artefact for a pre-verdict
        judge_pending turn — exactly as it does for judge_blocked."""
        from src.api.conversations import _redact_trace

        cfg = _cfg()
        resp = _redact_trace(_turn("judge_pending"), cfg)

        assert resp.status == "judge_pending"
        # The unvetted answer itself is withheld; users receive only the
        # generic held-for-review/retry message (Bug-9024).
        assert resp.answer_text == (
            "This answer is being held for review. Please try again shortly."
        )
        # Answer-derived artefacts are all withheld, same as judge_blocked.
        assert resp.rendered_output is None
        assert resp.chart_type is None
        assert resp.calculation_steps is None
        assert resp.query_result_sample is None
        assert resp.citations is None
        assert resp.semantic_query is None
        assert resp.thought_summary is None
        # The whole serialised payload leaks none of the secret.
        assert SECRET_ANSWER not in resp.model_dump_json()

    def test_ok_turn_still_visible(self):
        """No over-redaction: a released (ok) turn still serves its answer, so
        the withhold is scoped to the pending window only."""
        from src.api.conversations import _redact_trace

        resp = _redact_trace(_turn("ok"), _cfg())
        assert resp.status == "ok"
        assert resp.answer_text == SECRET_ANSWER
        assert resp.query_result_sample == [{"revenue": 4200000}]

    @pytest.mark.asyncio
    async def test_list_turns_endpoint_withholds_pending_row(self):
        """End-to-end through the real list_turns handler: a conversation whose
        turn is judge_pending returns a row with no answer content."""
        from src.api import conversations

        cfg = _cfg()
        conv = types.SimpleNamespace(
            id=uuid.uuid4(), project_id=cfg.project_id, persona_id=None,
            caller_ref="u", deleted_at=None,
        )
        pending = _turn("judge_pending")
        pending.conversation_id = conv.id

        db = MagicMock()
        turns_result = MagicMock()
        turns_result.scalars.return_value.all.return_value = [pending]
        db.execute = AsyncMock(return_value=turns_result)
        db.get = AsyncMock(return_value=conv)

        async def _gen(*a, **kw):
            yield db

        user = types.SimpleNamespace(
            tenant_id="t", user_id="u", is_embed=False, project_ids=None,
        )
        with (
            patch.object(conversations, "get_tenant_db", _gen),
            patch.object(conversations, "_require_project_access_and_agent",
                         AsyncMock(return_value=cfg)),
            patch.object(conversations, "_enforce_conversation_ownership",
                         lambda *a, **k: None),
            patch.object(conversations, "_resolve_persona_name",
                         AsyncMock(return_value=None)),
        ):
            rows = await conversations.list_turns(
                cfg.project_id, conv.id, current_user=user,
            )

        assert len(rows) == 1
        assert rows[0].status == "judge_pending"
        assert rows[0].answer_text == (
            "This answer is being held for review. Please try again shortly."
        )
        assert SECRET_ANSWER not in json.dumps(
            [r.model_dump() for r in rows], default=str
        )


# ---------------------------------------------------------------------------
# (a) Read path 2 — Idempotency-Key replay withholds a judge_pending turn
# ---------------------------------------------------------------------------


class TestIdempotencyReplayWithholdsJudgePending:
    @pytest.mark.asyncio
    async def test_stream_replay_of_pending_turn_withholds_answer(self):
        """A retried stream POST whose keyed turn is still judge_pending (the
        in-flight original's verdict has not resolved) must NOT replay the
        unvetted answer over SSE: the emitted turn.completed carries no answer
        text and signals judge_pending so the client keeps waiting."""
        from src.api import conversations

        cfg = _cfg()
        conv = types.SimpleNamespace(
            id=uuid.uuid4(), project_id=cfg.project_id, persona_id=None,
        )
        pending = _turn("judge_pending")
        pending.conversation_id = conv.id

        db = MagicMock()
        cfg_result = MagicMock()
        cfg_result.scalar_one_or_none.return_value = cfg
        db.execute = AsyncMock(return_value=cfg_result)

        async def _get(model, _id):
            # conversations.py get order: turn first, then conversation.
            return pending if model.__name__ == "AgentTurn" else conv
        db.get = AsyncMock(side_effect=_get)

        async def _gen(*a, **kw):
            yield db

        publisher = EventPublisher()
        with (
            patch.object(conversations, "get_tenant_db", _gen),
            patch.object(conversations, "_resolve_persona_name",
                         AsyncMock(return_value=None)),
        ):
            await conversations._replay_turn_into_publisher(
                tenant_id="t",
                project_id=cfg.project_id,
                conversation_id=conv.id,
                turn_id=pending.id,
                publisher=publisher,
            )

        events = await _drain_events(publisher)
        stream_text = json.dumps(events, default=str)
        assert SECRET_ANSWER not in stream_text, "replay leaked the unvetted answer"

        completed = [e for e in events if e["event"] == "turn.completed"]
        assert len(completed) == 1
        data = completed[0]["data"]
        assert data["status"] == "judge_pending"
        assert data["answer_text"] == (
            "This answer is being held for review. Please try again shortly."
        )
        assert data["judge_pending"] is True
        assert data["citations"] is None
        assert data["result_sample"] is None

    @pytest.mark.asyncio
    async def test_reserve_turn_does_not_classify_pending_as_streaming_reuse(self):
        """A judge_pending row is committed (not an in-flight streaming
        placeholder), so _reserve_turn must NOT reuse it as a live streaming
        row to re-run into — that would let a retry race/overwrite the
        in-flight original. It is returned as a duplicate whose replay is
        withheld by the redaction gate (asserted above)."""
        from src.api.conversations import _reserve_turn

        pending = _turn("judge_pending")
        existing_result = MagicMock()
        existing_result.scalar_one_or_none.return_value = pending
        db = MagicMock()
        db.execute = AsyncMock(return_value=existing_result)

        reservation = await _reserve_turn(
            db, pending.conversation_id,
            user_message="q", idempotency_key="key-1",
        )
        # Not treated as a live streaming reservation to re-run into.
        assert reservation.existing_status == "judge_pending"
        assert reservation.is_duplicate is True


# ---------------------------------------------------------------------------
# (b) Judge pass/warn RELEASES the answer (judge_pending -> ok, now visible)
# ---------------------------------------------------------------------------


class TestJudgeReleasesPendingTurn:
    @pytest.mark.parametrize("verdict", ["pass", "warn"])
    def test_pass_warn_flips_pending_to_ok_and_reveals_answer(self, verdict):
        """apply_judge_block on a cleared verdict releases a judge_pending row
        to ok; _redact_trace then serves the real answer."""
        from src.api.conversations import _redact_trace

        cfg = _cfg()
        turn = _turn("judge_pending")
        apply_judge_block(cfg, turn, JudgeOutcome(verdict=verdict, reasoning="", metrics={}))

        assert turn.status == "ok", "cleared verdict must release the pending row"
        assert turn.answer_text == SECRET_ANSWER

        resp = _redact_trace(turn, cfg)
        assert resp.status == "ok"
        assert resp.answer_text == SECRET_ANSWER

    def test_release_does_not_disturb_non_pending_status(self):
        """The release path is scoped to judge_pending: an async ok turn or a
        refused/error turn is never flipped by a cleared verdict."""
        cfg = _cfg()
        for status in ("ok", "refused", "error"):
            turn = _turn(status)
            apply_judge_block(cfg, turn, JudgeOutcome(verdict="pass", reasoning="", metrics={}))
            assert turn.status == status


# ---------------------------------------------------------------------------
# (c) Judge block/unknown -> judge_blocked withheld
# ---------------------------------------------------------------------------


class TestJudgeBlocksPendingTurn:
    @pytest.mark.parametrize("verdict", ["fail", "unknown"])
    def test_block_and_unknown_flip_pending_to_blocked_and_withhold(self, verdict):
        """A fail or (in sync mode) unvetted unknown verdict flips the pending
        row to judge_blocked and withholds the original answer."""
        from src.api.conversations import _redact_trace

        cfg = _cfg()
        turn = _turn("judge_pending")
        apply_judge_block(cfg, turn, JudgeOutcome(verdict=verdict, reasoning="", metrics={}))

        assert turn.status == "judge_blocked"
        assert turn.answer_text != SECRET_ANSWER
        assert turn.llm_plan["original_answer_blocked"] == SECRET_ANSWER

        resp = _redact_trace(turn, cfg)
        assert resp.status == "judge_blocked"
        assert SECRET_ANSWER not in resp.model_dump_json()


# ---------------------------------------------------------------------------
# (d) A judge-step FAILURE leaves the durable row judge_pending + withheld
#     (durable fail-closed — closes the Bug-6587 total-DB-outage residual)
# ---------------------------------------------------------------------------


class TestDurableFailClosedLeavesPending:
    @pytest.mark.asyncio
    async def test_total_db_outage_leaves_durable_row_pending_and_withheld(self):
        """When the sync-judge orchestration fails AND the fresh recovery
        session is also unusable (total DB outage), _fail_closed_judge_block
        must not raise and blocks the IN-MEMORY turn for the immediate caller.

        The durable-row guarantee comes from the row having been committed as
        judge_pending BEFORE the judge ran: on a total outage NO commit lands
        that could flip it to ok, so a later reader fetches judge_pending and
        the redaction gate withholds it — the unvetted answer is never durably
        released (Bug-6587 residual, closed by Bug-8290)."""
        from src.api import conversations
        from src.api.conversations import _fail_closed_judge_block, _redact_trace

        cfg = _cfg()
        # The IN-MEMORY caller turn the pipeline holds (a distinct object from
        # the committed DB row; on total outage its mutation is never persisted).
        caller_turn = _turn("judge_pending")

        caller_db = MagicMock()
        caller_db.rollback = AsyncMock(side_effect=RuntimeError("poisoned session"))

        async def _broken_session(tenant_id):
            raise RuntimeError("DB down")
            yield  # pragma: no cover

        with patch.object(conversations, "get_tenant_db", _broken_session):
            result = await _fail_closed_judge_block(caller_db, "t", cfg, caller_turn)

        # The immediate caller's in-memory turn is withheld (no exception
        # raised, no unvetted answer returned).
        assert result.status == "judge_blocked"
        assert SECRET_ANSWER not in (result.answer_text or "")

        # The DURABLE row was committed judge_pending before the judge ran and
        # was never re-committed (total outage), so any later reader fetches a
        # judge_pending row that the redaction gate withholds. This is the
        # durable fail-closed property: no release to ok happened.
        durable_read = _redact_trace(_turn("judge_pending"), cfg)
        assert durable_read.status == "judge_pending"
        assert durable_read.answer_text == (
            "This answer is being held for review. Please try again shortly."
        )
        assert SECRET_ANSWER not in durable_read.model_dump_json()


# ---------------------------------------------------------------------------
# PRODUCER contract — the two commit-before-judge sites MUST commit the answer
# turn as ``judge_pending`` BEFORE run_judge executes. Without this the
# redaction/replay gates above guard a status no row ever enters (dead code):
# reverting only the two ``turn.status = "judge_pending"`` producer lines would
# leave every read-path test green while BOTH leaks reopen. These tests spy the
# real db.commit ordering on the actual production paths (send_message and
# _run_turn_into_publisher), so they FAIL on a producer revert.
# ---------------------------------------------------------------------------


def _stream_outcome():
    return types.SimpleNamespace(
        status="ok",
        plan={"tool": "query"},
        answer_text=SECRET_ANSWER,
        result_sample=[{"revenue": 4200000}],
        result_row_count=1,
        provider="anthropic",
        calculation_steps=None,
    )


class TestProducerCommitsJudgePendingBeforeVerdict:
    @pytest.mark.parametrize("verdict", ["pass", "warn", "fail", "unknown"])
    @pytest.mark.asyncio
    async def test_stream_path_commits_judge_pending_before_run_judge(self, verdict):
        """_run_turn_into_publisher must commit the answer turn as judge_pending
        (durable, non-releasable) BEFORE run_judge runs, then flip it to ok
        (pass/warn) or judge_blocked (fail/unknown) on the post-verdict commit."""
        from src.api import conversations

        cfg = _cfg("transparent", judge_mode="sync")
        conv = types.SimpleNamespace(
            id=uuid.uuid4(), project_id=cfg.project_id, persona_id=None,
            caller_ref="u", title="t", last_active_at=None,
        )
        turn = _turn("ok")  # persist_turn returns an ok turn; producer must flip it
        turn.conversation_id = conv.id
        outcome = _stream_outcome()
        judge_outcome = JudgeOutcome(verdict=verdict, reasoning="r", metrics={})

        events_log: list[str] = []

        db = AsyncMock()
        cfg_result = MagicMock()
        cfg_result.scalar_one_or_none.return_value = cfg
        db.execute = AsyncMock(return_value=cfg_result)
        db.get = AsyncMock(return_value=conv)
        db.refresh = AsyncMock()

        async def _commit_spy():
            # Record the turn status AT each commit, in order.
            events_log.append(f"commit:{turn.status}")
        db.commit = AsyncMock(side_effect=_commit_spy)

        async def _run_judge_spy(**kwargs):
            events_log.append(f"run_judge:{turn.status}")
            return judge_outcome

        async def _gen(*a, **kw):
            yield db

        publisher = EventPublisher()
        with (
            patch.object(conversations, "get_tenant_db", _gen),
            patch.object(conversations, "run_turn", AsyncMock(return_value=outcome)),
            patch.object(conversations, "persist_turn", AsyncMock(return_value=turn)),
            patch("src.prompt.assembler.assemble_prompt",
                  AsyncMock(return_value=types.SimpleNamespace(
                      system="sys",
                      system_sections=[("## PROJECT CONTEXT", "sys")],
                      grounding_matches="", date_anchor=None,
                  ))),
            patch.object(conversations, "run_judge", AsyncMock(side_effect=_run_judge_spy)),
            patch.object(conversations, "_prior_turns_for_judge", AsyncMock(return_value=[])),
            patch.object(conversations, "record_turn_cost", AsyncMock()),
            patch.object(conversations, "dispatch_event", AsyncMock()),
        ):
            await conversations._run_turn_into_publisher(
                tenant_id="t",
                project_id=cfg.project_id,
                conversation_id=conv.id,
                turn_index=0,
                user_message="What is revenue?",
                jwt_token="jwt",
                started=0.0,
                publisher=publisher,
            )
        await _drain_events(publisher)

        # First commit MUST be judge_pending (the pre-judge durable commit).
        assert events_log[0] == "commit:judge_pending", (
            f"first commit not judge_pending: {events_log}"
        )
        # run_judge MUST run while the durable row is still judge_pending.
        assert "run_judge:judge_pending" in events_log, (
            f"run_judge did not run against a judge_pending row: {events_log}"
        )
        # Ordering: the pending commit precedes run_judge.
        assert events_log.index("commit:judge_pending") < events_log.index(
            "run_judge:judge_pending"
        )
        # Terminal status per verdict.
        expected = "ok" if verdict in ("pass", "warn") else "judge_blocked"
        assert turn.status == expected

    @pytest.mark.parametrize("verdict", ["pass", "fail"])
    @pytest.mark.asyncio
    async def test_sync_rest_path_commits_judge_pending_before_run_judge(self, verdict):
        """send_message (sync REST) must ALSO commit the answer turn as
        judge_pending before run_judge, then release/block on the verdict."""
        from src.api import conversations
        from src.auth.middleware import CurrentUser

        cfg = _cfg("transparent", judge_mode="sync")
        cfg.daily_token_budget = 0
        cfg.daily_cost_budget_usd = 0
        conv = types.SimpleNamespace(
            id=uuid.uuid4(), project_id=cfg.project_id, persona_id=None,
            pinned_model_id=None, caller_ref="u", last_active_at=None,
        )
        turn = _turn("ok")
        turn.conversation_id = conv.id
        outcome = _stream_outcome()
        judge_outcome = JudgeOutcome(verdict=verdict, reasoning="r", metrics={})

        events_log: list[str] = []

        db = AsyncMock()
        db.get = AsyncMock(return_value=conv)
        db.refresh = AsyncMock()

        async def _commit_spy():
            events_log.append(f"commit:{turn.status}")
        db.commit = AsyncMock(side_effect=_commit_spy)

        async def _run_judge_spy(**kwargs):
            events_log.append(f"run_judge:{turn.status}")
            return judge_outcome

        async def _gen(*a, **kw):
            yield db

        reservation = conversations._TurnReservation(
            turn_index=0, existing_turn_id=turn.id,
            existing_status="streaming", is_duplicate=False,
        )
        user = CurrentUser(
            user_id="u", tenant_id="t", email="u@example.com", role="tenant_admin",
        )
        body = types.SimpleNamespace(text="What is revenue?")
        bg = MagicMock()
        bg.add_task = MagicMock()

        with (
            patch.object(conversations, "get_tenant_db", _gen),
            patch.object(conversations, "_require_project_access_and_agent",
                         AsyncMock(return_value=cfg)),
            patch.object(conversations, "_enforce_conversation_ownership",
                         lambda *a, **k: None),
            patch.object(conversations, "_reserve_turn",
                         AsyncMock(return_value=reservation)),
            patch.object(conversations, "run_turn", AsyncMock(return_value=outcome)),
            patch.object(conversations, "persist_turn", AsyncMock(return_value=turn)),
            patch("src.prompt.assembler.assemble_prompt",
                  AsyncMock(return_value=types.SimpleNamespace(
                      system="sys",
                      system_sections=[("## PROJECT CONTEXT", "sys")],
                      grounding_matches="", date_anchor=None,
                  ))),
            patch.object(conversations, "run_judge", AsyncMock(side_effect=_run_judge_spy)),
            patch.object(conversations, "_prior_turns_for_judge", AsyncMock(return_value=[])),
            patch.object(conversations, "_repair_narration_after_sync_judge_fail",
                         AsyncMock(return_value=None)),
            patch.object(conversations, "record_turn_cost", AsyncMock()),
            patch.object(conversations, "reserve_budget", AsyncMock(return_value=None)),
            patch.object(conversations, "reconcile_budget_reservation", AsyncMock()),
            patch.object(conversations, "_resolve_persona_name", AsyncMock(return_value=None)),
            patch.object(conversations, "dispatch_event", AsyncMock()),
        ):
            await conversations.send_message(
                cfg.project_id, conv.id, body, bg,
                current_user=user, idempotency_key=None,
            )

        assert events_log[0] == "commit:judge_pending", (
            f"first REST commit not judge_pending: {events_log}"
        )
        assert "run_judge:judge_pending" in events_log
        assert events_log.index("commit:judge_pending") < events_log.index(
            "run_judge:judge_pending"
        )
        expected = "ok" if verdict == "pass" else "judge_blocked"
        assert turn.status == expected
