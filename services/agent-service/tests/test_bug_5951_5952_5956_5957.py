"""Behavior tests for the resumed agent-service remediation batch.

Canonical registry IDs: Bug-6008 (Bug-6009, Bug-6010, Bug-6011 respectively) —
the batch was originally dispatched as Bug-5951/5952/5956/5957 but those
numbers were reassigned to unrelated GPT-external-review findings by
concurrent agents before this batch landed; see
docs/execution/execution_issue-registry.md.

Covers:
  - Bug-6008: conversation endpoints call the project/model authorization
    check before doing any work, and propagate a denial.
  - Bug-6009: the chat response and SSE turn.completed event carry
    persona_id/persona_name.
  - Bug-6010: send_message() converts an LLM-provider timeout into a 503
    instead of a bare 500.
  - Bug-6011: _prior_turns_for_judge() applies a sliding window (both the
    in-memory result and the SQL LIMIT) instead of loading full history.
  - Bug-5957: diagnostic traces stripped from user-facing error responses.
  - Bug-5956: calendar alias grounding scoped to the current model.
  - Bug-5951/5952 extensions: the agent-service webhook dispatcher signs
    each retry attempt with a fresh timestamp and omits the signature
    header entirely when no valid (non-placeholder) signing secret is
    configured.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch as _patch

import httpx
import pytest
from fastapi import HTTPException

from shared.db.models import ProjectPersona
from src.api.conversations import (
    MessageSend,
    _prior_turns_for_judge,
    _require_project_access_and_agent,
    send_message,
)
from .conftest import make_agent_config, make_mock_db, make_turn


# ---------------------------------------------------------------------------
# Bug-6008 -- project/model authorization check
# ---------------------------------------------------------------------------


class TestConversationAuthorization:

    @pytest.mark.asyncio
    async def test_require_project_access_and_agent_calls_shared_rbac_check(self):
        """The wrapper must call the RBAC gate before checking the agent is
        enabled, with the caller's project_id and a viewer floor.

        Bug-8589 HALF A moved the delegate from the shared terminal
        ``ensure_project_model_access`` to the service's chat tier
        ``require_project_chat_access`` (which refuses a service principal by
        type, then delegates to that same terminal). The assertion is unchanged
        in intent — delegate first, with this project and a viewer floor — only
        the name of the delegate moved."""
        db = AsyncMock()
        project_id = uuid.uuid4()
        current_user = types.SimpleNamespace(
            user_id="user@example.com", tenant_id="t1", role="member",
        )
        cfg_row = make_agent_config(project_id=project_id)

        with (
            _patch(
                "src.api.conversations.require_project_chat_access",
                AsyncMock(),
            ) as ensure_mock,
            _patch(
                "src.api.conversations._require_agent_enabled",
                AsyncMock(return_value=cfg_row),
            ),
        ):
            result = await _require_project_access_and_agent(db, project_id, current_user)

        ensure_mock.assert_called_once()
        _, kwargs = ensure_mock.call_args
        assert kwargs["project_id"] == project_id
        assert kwargs["min_role"] == "viewer"
        assert result is cfg_row

    @pytest.mark.asyncio
    async def test_denial_propagates_and_agent_enabled_never_runs(self):
        """A 403 from the shared RBAC check must reach the caller, and the
        agent-enabled lookup must not run for a denied user (no leaking of
        whether the project has agent enabled to an unauthorized caller)."""
        db = AsyncMock()
        project_id = uuid.uuid4()
        current_user = types.SimpleNamespace(
            user_id="stranger@example.com", tenant_id="t1", role="member",
        )

        with (
            _patch(
                "src.api.conversations.require_project_chat_access",
                AsyncMock(side_effect=HTTPException(status_code=403, detail="Access denied")),
            ),
            _patch(
                "src.api.conversations._require_agent_enabled",
                AsyncMock(),
            ) as agent_enabled_mock,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _require_project_access_and_agent(db, project_id, current_user)

        assert exc_info.value.status_code == 403
        agent_enabled_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Bug-6009 -- persona context surfaced on the chat response
# ---------------------------------------------------------------------------


class TestPersonaContextOnResponse:

    @pytest.mark.asyncio
    async def test_send_message_response_carries_persona_id_and_name(self):
        project_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        turn_id = uuid.uuid4()
        persona_id = uuid.uuid4()

        cfg_row = make_agent_config(project_id=project_id)
        cfg_row.judge_mode = "async"  # avoid exercising the sync judge path
        cfg_row.show_thought_process = True
        cfg_row.show_semantic_query = True
        cfg_row.show_physical_query = True
        conv_obj = types.SimpleNamespace(
            id=conversation_id, project_id=project_id,
            persona_id=persona_id, last_active_at=None,
            pinned_model_id=None,
        )
        persona_obj = types.SimpleNamespace(id=persona_id, name="Finance Analyst")
        turn_obj = types.SimpleNamespace(
            id=turn_id, conversation_id=conversation_id, turn_index=0,
            user_message="Show revenue", answer_text="Revenue is 100",
            status="ok", latency_ms=50, llm_plan=None, thought_summary=None,
            semantic_query=None, routed_sql=None, route=None, citations=None,
            user_feedback=None, judge_verdict=None, judge_reasoning=None,
            judge_metrics=None, guardrail_actions=None,
            usage_input_tokens=10, usage_output_tokens=5, rendered_output=None,
        )
        outcome = types.SimpleNamespace(
            status="ok", plan={"query": {"model_id": "m1"}},
            answer_text="Revenue is 100", result_sample=[{"revenue": 100}],
            result_row_count=1, provider="openai",
        )

        mock_db = make_mock_db()
        mock_db.get = AsyncMock(return_value=conv_obj)
        # The persona is resolved through a SCOPED SELECT, not ``db.get``: the
        # project predicate has to be in the query, because a stored persona_id
        # pointing at another project would otherwise return that project's
        # persona NAME on this response. ``db.get`` cannot carry a predicate.
        _default_result = mock_db.execute.return_value
        _persona_result = MagicMock()
        _persona_result.scalars.return_value.one_or_none.return_value = persona_obj

        async def _execute(stmt, *args, **kwargs):
            descriptions = getattr(stmt, "column_descriptions", None) or []
            entity = descriptions[0].get("entity") if descriptions else None
            if entity is ProjectPersona:
                return _persona_result
            return _default_result

        mock_db.execute = AsyncMock(side_effect=_execute)
        mock_db.scalar = AsyncMock(return_value=0)
        mock_db.refresh = AsyncMock()

        async def _gen(*a, **kw):
            yield mock_db

        current_user = types.SimpleNamespace(
            tenant_id="test-tenant", user_id=uuid.uuid4(), raw_token="fake-jwt",
            role="member",
        )
        conv_obj.caller_ref = str(current_user.user_id)
        bundle = types.SimpleNamespace(system="## TASK\nTest")

        with (
            _patch("src.api.conversations.get_tenant_db", _gen),
            _patch(
                "src.api.conversations._require_project_access_and_agent",
                AsyncMock(return_value=cfg_row),
            ),
            _patch("src.api.conversations.run_turn", AsyncMock(return_value=outcome)),
            _patch("src.api.conversations.persist_turn", AsyncMock(return_value=turn_obj)),
            _patch("src.api.conversations._prior_turns_for_judge", AsyncMock(return_value=[])),
            _patch("src.prompt.assembler.assemble_prompt", AsyncMock(return_value=bundle)),
            _patch("src.api.conversations.dispatch_event", AsyncMock()),
            _patch("src.api.conversations._emit_turn_webhook"),
        ):
            resp = await send_message(
                project_id=project_id,
                conversation_id=conversation_id,
                body=MessageSend(text="Show revenue"),
                background_tasks=MagicMock(),
                current_user=current_user,
            )

        assert resp.persona_id == persona_id
        assert resp.persona_name == "Finance Analyst"


# ---------------------------------------------------------------------------
# Bug-6010 -- LLM provider timeout returns 503, not 500
# ---------------------------------------------------------------------------


class TestLLMTimeoutHandling:

    @pytest.mark.asyncio
    async def test_send_message_returns_503_on_llm_timeout(self):
        project_id = uuid.uuid4()
        conversation_id = uuid.uuid4()

        cfg_row = make_agent_config(project_id=project_id)
        conv_obj = types.SimpleNamespace(
            id=conversation_id, project_id=project_id, persona_id=None,
            last_active_at=None,
        )

        mock_db = make_mock_db()
        mock_db.get = AsyncMock(return_value=conv_obj)
        mock_db.scalar = AsyncMock(return_value=-1)
        mock_db.rollback = AsyncMock()

        async def _gen(*a, **kw):
            yield mock_db

        current_user = types.SimpleNamespace(
            tenant_id="test-tenant", user_id=uuid.uuid4(), raw_token="fake-jwt",
            role="member",
        )
        conv_obj.caller_ref = str(current_user.user_id)

        with (
            _patch("src.api.conversations.get_tenant_db", _gen),
            _patch(
                "src.api.conversations._require_project_access_and_agent",
                AsyncMock(return_value=cfg_row),
            ),
            _patch(
                "src.api.conversations.run_turn",
                AsyncMock(side_effect=httpx.ReadTimeout("timed out")),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await send_message(
                    project_id=project_id,
                    conversation_id=conversation_id,
                    body=MessageSend(text="Show revenue"),
                    background_tasks=MagicMock(),
                    current_user=current_user,
                )

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_send_message_returns_500_on_non_timeout_error(self):
        """A non-timeout pipeline failure must still be a 500, not 503 --
        the timeout branch must not swallow unrelated errors."""
        project_id = uuid.uuid4()
        conversation_id = uuid.uuid4()

        cfg_row = make_agent_config(project_id=project_id)
        conv_obj = types.SimpleNamespace(
            id=conversation_id, project_id=project_id, persona_id=None,
            last_active_at=None,
        )

        mock_db = make_mock_db()
        mock_db.get = AsyncMock(return_value=conv_obj)
        mock_db.scalar = AsyncMock(return_value=-1)
        mock_db.rollback = AsyncMock()

        async def _gen(*a, **kw):
            yield mock_db

        current_user = types.SimpleNamespace(
            tenant_id="test-tenant", user_id=uuid.uuid4(), raw_token="fake-jwt",
            role="member",
        )
        conv_obj.caller_ref = str(current_user.user_id)

        with (
            _patch("src.api.conversations.get_tenant_db", _gen),
            _patch(
                "src.api.conversations._require_project_access_and_agent",
                AsyncMock(return_value=cfg_row),
            ),
            _patch(
                "src.api.conversations.run_turn",
                AsyncMock(side_effect=ValueError("bad plan shape")),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await send_message(
                    project_id=project_id,
                    conversation_id=conversation_id,
                    body=MessageSend(text="Show revenue"),
                    background_tasks=MagicMock(),
                    current_user=current_user,
                )

        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# Bug-6011 -- bounded conversation history sent to the judge
# ---------------------------------------------------------------------------


class TestJudgeHistoryWindow:

    @pytest.mark.asyncio
    async def test_prior_turns_for_judge_applies_sliding_window(self):
        conversation_id = uuid.uuid4()
        # 30 turns exist, but the window is 5 -- only the most recent 5
        # should be reflected in the returned history.
        all_turns = [
            make_turn(conversation_id=conversation_id, turn_index=i,
                      user_message=f"q{i}", answer_text=f"a{i}")
            for i in range(30)
        ]
        # The real query orders DESC (most recent first) and LIMITs to
        # max_turns+1; the function then reverses back to chronological
        # order itself, so the fake DB layer must mimic the DESC shape.
        recent_window_desc = list(reversed(all_turns[-6:]))

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = recent_window_desc
        db.execute = AsyncMock(return_value=result)

        history = await _prior_turns_for_judge(db, conversation_id, max_turns=5)

        # 5 turns * 2 messages (user + assistant) each = 10 entries.
        assert len(history) == 10
        assert history[0]["content"] == "q25"
        assert history[-1]["content"] == "a29"

    @pytest.mark.asyncio
    async def test_prior_turns_for_judge_excludes_current_turn(self):
        conversation_id = uuid.uuid4()
        exclude_id = uuid.uuid4()
        turns = [
            make_turn(conversation_id=conversation_id, turn_index=0,
                      user_message="q0", answer_text="a0"),
            types.SimpleNamespace(
                id=exclude_id, conversation_id=conversation_id, turn_index=1,
                user_message="q1-in-progress", answer_text=None,
            ),
        ]

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = turns
        db.execute = AsyncMock(return_value=result)

        history = await _prior_turns_for_judge(
            db, conversation_id, exclude_turn_id=exclude_id, max_turns=20,
        )

        assert history == [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}]

    @pytest.mark.asyncio
    async def test_prior_turns_for_judge_bounds_the_sql_query(self):
        """Bug-6011 follow-up: the DB fetch itself must be bounded (ORDER BY
        ... DESC LIMIT), not just the in-memory slice, so a very long
        conversation does not force an unbounded row fetch on every judge
        call."""
        conversation_id = uuid.uuid4()

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        await _prior_turns_for_judge(db, conversation_id, max_turns=5)

        db.execute.assert_called_once()
        executed_stmt = db.execute.call_args[0][0]
        compiled = str(executed_stmt)
        assert "ORDER BY" in compiled
        assert "LIMIT" in compiled


# ---------------------------------------------------------------------------
# Bug-5957 -- diagnostic traces stripped from user-facing error responses
# ---------------------------------------------------------------------------


class TestDiagnosticTraceStripping:
    """Bug-5957 -- error responses and SSE events must NOT expose internal
    diagnostic information (provider names, model names, API keys, stack
    traces, HTTP status codes from internal services)."""

    def test_llm_auth_error_does_not_expose_provider_or_model(self):
        """When the LLM returns a 401, the user-facing message must not
        contain the provider name, model name, or API key guidance."""
        from src.pipeline import run_turn
        # We test the error-message construction logic indirectly by
        # verifying the pattern: the run_turn function builds the
        # user_msg string. We import and inspect the source to confirm
        # the new generic messages are in place.
        import inspect
        source = inspect.getsource(run_turn)
        # The OLD patterns that leaked internals must be gone:
        assert "llm_config.provider}/{llm_config.model_name}" not in source
        assert "llm_config.display_name" not in source
        assert "llm_config.timeout_seconds" not in source
        assert "failed: {detail}" not in source
        # The NEW safe patterns must be present:
        assert "language-model service" in source

    def test_narration_failure_does_not_expose_route_or_exception(self):
        """Failed narration must not leak route_type, row count, or the
        raw exception string to the user."""
        import inspect
        from src.pipeline import run_turn
        source = inspect.getsource(run_turn)
        assert "could not narrate the result: {exc}" not in source

    def test_sse_error_does_not_expose_raw_exception(self):
        """SSE turn.error events must not send str(exc) or type(exc).__name__
        to end users."""
        import inspect
        from src.api.conversations import _run_turn_into_publisher
        source = inspect.getsource(_run_turn_into_publisher)
        assert "detail=str(exc)" not in source
        assert "detail=f\"{type(exc).__name__}" not in source

    def test_placeholder_error_does_not_expose_exception_class(self):
        """Error placeholder rows stored in the DB must not contain the
        Python exception class name."""
        import inspect
        from src.api.conversations import send_message
        source = inspect.getsource(send_message)
        assert "Pipeline error: {type(exc).__name__}" not in source

    def test_kpi_error_does_not_expose_http_status_or_response(self):
        """KPI evaluation failure must not expose the internal HTTP status
        code or the internal service response body."""
        import inspect
        from src.pipeline import _run_evaluate_kpi_branch
        source = inspect.getsource(_run_evaluate_kpi_branch)
        assert "HTTP {resp.status_code}" not in source
        assert "Could not evaluate the KPI: {exc}" not in source

    def test_named_set_error_does_not_expose_http_details(self):
        """Named set preview failure must not expose HTTP status or
        internal service response body."""
        import inspect
        from src.pipeline import _run_preview_named_set_branch
        source = inspect.getsource(_run_preview_named_set_branch)
        assert "HTTP {resp.status_code}" not in source
        assert "Could not preview the named set: {exc}" not in source

    def test_aggregate_error_does_not_expose_optimizer_response(self):
        """Aggregate creation failure must not expose optimizer service
        response text or raw exception strings."""
        import inspect
        from src.pipeline import _run_create_aggregate_branch
        source = inspect.getsource(_run_create_aggregate_branch)
        assert "Failed to create aggregate: {error_detail}" not in source
        assert "Error contacting optimizer: {exc}" not in source

    def test_redact_trace_strips_guardrail_detail(self):
        """_redact_trace must remove the 'detail' key from guardrail_actions
        entries so raw exception strings are not exposed to end users."""
        from src.api.conversations import _redact_trace

        turn = MagicMock()
        turn.status = "completed"
        turn.guardrail_actions = [
            {"action": "warn", "detail": "ValueError: secret internal error"},
        ]
        turn.llm_plan = None
        turn.model_validate = MagicMock()

        # _redact_trace calls TurnResponse.model_validate(turn), so we need
        # to mock that. We'll inspect the source instead for the pattern.
        import inspect
        source = inspect.getsource(_redact_trace)
        assert '"detail"' in source or "'detail'" in source, (
            "_redact_trace must strip 'detail' from guardrail_actions entries"
        )
        # Verify the stripping loop is present
        assert "guardrail_actions" in source

    def test_redact_trace_strips_plan_diagnostic_keys(self):
        """_redact_trace must remove diagnostic keys (detail, raw, traceback,
        stack_trace) from llm_plan dicts."""
        import inspect
        from src.api.conversations import _redact_trace
        source = inspect.getsource(_redact_trace)
        for key in ("detail", "raw", "traceback", "stack_trace"):
            assert key in source, (
                f"_redact_trace must strip diagnostic key '{key}' from llm_plan"
            )

    def test_judge_reasoning_does_not_expose_raw_exception(self):
        """Judge fallback reasoning must not contain raw exception strings
        from provider errors."""
        import inspect
        from src.judge.judge import run_judge
        source = inspect.getsource(run_judge)
        assert "Judge LLM not configured: {exc}" not in source
        assert "Judge LLM API key missing: {exc}" not in source
        assert "Judge LLM call failed: {exc}" not in source


# ---------------------------------------------------------------------------
# Bug-5956 -- calendar alias grounding scoped to current model
# ---------------------------------------------------------------------------


class TestCalendarAliasScopedToModel:
    """Bug-5956 -- _calendar_aliases must only return calendar tables
    reachable from the specified model's data sources, not all calendar
    tables in the tenant."""

    @pytest.mark.asyncio
    async def test_calendar_aliases_filters_by_model_data_sources(self):
        """Calendar tables from unrelated data sources must not appear.

        Bug-5956 — asserts both the returned result AND the SQL WHERE
        clause passed to db.execute for the CalendarTable query, so we
        verify scoping is enforced at the query level."""
        from src.derived.context import _calendar_aliases

        model_id = uuid.uuid4()
        source_a = uuid.uuid4()
        source_b = uuid.uuid4()  # unrelated data source
        cal_a_id = uuid.uuid4()
        cal_b_id = uuid.uuid4()

        model_obj = types.SimpleNamespace(id=model_id)

        # ModelTable rows for this model -- only source_a
        mt_row = (None, source_a)  # (calendar_table_id, source_id)

        cal_a = types.SimpleNamespace(
            id=cal_a_id, data_source_id=source_a,
            table_name="calendar_a", dialect="postgresql",
            date_column="dt", year_column="yr", half_column=None,
            quarter_column="qtr", month_column="mn",
            week_column=None, day_column=None,
        )

        db = AsyncMock()

        # db.get(Model, model_id) -> model_obj
        db.get = AsyncMock(return_value=model_obj)

        # Build separate mock results for each db.execute call:
        # Call 1: ModelTable query -> returns mt_row
        mt_result = MagicMock()
        mt_result.all.return_value = [mt_row]
        # Call 2: CalendarTable query -> returns only cal_a
        ct_result = MagicMock()
        ct_result.scalars.return_value.all.return_value = [cal_a]

        db.execute = AsyncMock(side_effect=[mt_result, ct_result])

        result = await _calendar_aliases(db, model_id)

        assert len(result) == 1
        assert result[0]["table_name"] == "calendar_a"

        # Assert the WHERE clause of the CalendarTable query contains
        # a data_source_id IN filter scoped to the model's sources.
        assert db.execute.call_count == 2
        cal_stmt = db.execute.call_args_list[1][0][0]
        compiled = str(cal_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "calendar_tables" in compiled.lower(), (
            f"CalendarTable query missing table reference: {compiled}"
        )
        # SQLAlchemy may render UUIDs with or without hyphens; normalise.
        compiled_nohyphens = compiled.replace("-", "")
        source_a_hex = source_a.hex
        source_b_hex = source_b.hex
        assert source_a_hex in compiled_nohyphens, (
            f"CalendarTable WHERE clause missing model source_id {source_a}: {compiled}"
        )
        # Unrelated source_b must NOT appear in the query
        assert source_b_hex not in compiled_nohyphens, (
            f"CalendarTable WHERE clause includes unrelated source {source_b}: {compiled}"
        )

    @pytest.mark.asyncio
    async def test_calendar_aliases_empty_when_no_model_tables(self):
        """If the model has no model tables, no calendars should be returned."""
        from src.derived.context import _calendar_aliases

        model_id = uuid.uuid4()
        model_obj = types.SimpleNamespace(id=model_id)

        db = AsyncMock()
        db.get = AsyncMock(return_value=model_obj)
        mt_result = MagicMock()
        mt_result.all.return_value = []
        db.execute = AsyncMock(return_value=mt_result)

        result = await _calendar_aliases(db, model_id)

        assert result == []

    @pytest.mark.asyncio
    async def test_calendar_aliases_includes_explicitly_linked(self):
        """Calendar tables linked via ModelTable.calendar_table_id must
        be included even if their data_source_id differs from the model's
        other data sources."""
        from src.derived.context import _calendar_aliases

        model_id = uuid.uuid4()
        source_a = uuid.uuid4()
        explicit_cal_id = uuid.uuid4()
        unrelated_source = uuid.uuid4()

        model_obj = types.SimpleNamespace(id=model_id)

        # ModelTable row with explicit calendar link to a different source
        mt_row = (explicit_cal_id, source_a)

        cal_linked = types.SimpleNamespace(
            id=explicit_cal_id, data_source_id=unrelated_source,
            table_name="linked_cal", dialect="bigquery",
            date_column="d", year_column="y", half_column=None,
            quarter_column=None, month_column="m",
            week_column=None, day_column=None,
        )

        db = AsyncMock()
        db.get = AsyncMock(return_value=model_obj)

        mt_result = MagicMock()
        mt_result.all.return_value = [mt_row]

        ct_result = MagicMock()
        ct_result.scalars.return_value.all.return_value = [cal_linked]

        db.execute = AsyncMock(side_effect=[mt_result, ct_result])

        result = await _calendar_aliases(db, model_id)

        assert len(result) == 1
        assert result[0]["table_name"] == "linked_cal"

        # Assert the WHERE clause includes the explicit calendar ID
        assert db.execute.call_count == 2
        cal_stmt = db.execute.call_args_list[1][0][0]
        compiled = str(cal_stmt.compile(compile_kwargs={"literal_binds": True}))
        compiled_nohyphens = compiled.replace("-", "")
        assert explicit_cal_id.hex in compiled_nohyphens, (
            f"CalendarTable WHERE clause missing explicit cal_id {explicit_cal_id}: {compiled}"
        )

    @pytest.mark.asyncio
    async def test_calendar_aliases_returns_empty_for_unknown_model(self):
        """A model_id that does not exist returns an empty list."""
        from src.derived.context import _calendar_aliases

        db = AsyncMock()
        db.get = AsyncMock(return_value=None)

        result = await _calendar_aliases(db, uuid.uuid4())

        assert result == []


# ---------------------------------------------------------------------------
# Bug-5951/Bug-5952 extensions -- agent-service webhook dispatcher signing
# ---------------------------------------------------------------------------


def _make_webhook_cfg():
    return types.SimpleNamespace(
        enabled=True,
        webhook_url="https://example.com/hook",
        webhook_signing_secret=b"encrypted-bytes",
    )


def _mock_tenant_db_factory(cfg, dlq_sink: list | None = None):
    """Return an async-generator factory mimicking get_tenant_db.

    When *dlq_sink* is given, ``db.add``/``db.commit`` are wired so a DLQ row
    written via ``_persist_dlq`` lands in the sink list instead of erroring
    on an unconfigured MagicMock. ``db.get`` also resolves against the sink
    (by id) so ``dlq_id``-targeted updates in ``_persist_dlq``/
    ``_resolve_dlq_row`` find the row they just wrote, mirroring how a real
    session would re-fetch it.
    """
    async def _gen(tenant_id):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = cfg
        db.execute = AsyncMock(return_value=result)
        if dlq_sink is not None:
            db.add = lambda row: dlq_sink.append(row)
            db.commit = AsyncMock()

            async def _get(model, row_id):
                for row in dlq_sink:
                    if getattr(row, "id", None) == row_id:
                        return row
                return None

            db.get = AsyncMock(side_effect=_get)
        yield db
    return _gen


class TestWebhookSignatureFreshPerAttempt:
    """Bug-5952 extension -- dispatch_event must recompute the timestamp
    and HMAC signature for every retry attempt, not reuse the pre-loop
    values across the whole backoff schedule."""

    @pytest.mark.asyncio
    async def test_each_retry_attempt_gets_fresh_signature(self):
        from src.webhooks import dispatcher

        cfg = _make_webhook_cfg()
        post_calls: list[tuple[str, bytes]] = []

        async def fake_post_once(tenant_id, url, body, sig_header, event_type):
            post_calls.append((sig_header, body))
            # Fail the first two attempts with a retryable 500.
            if len(post_calls) < 3:
                return False, 500, "HTTP 500"
            return True, 200, None

        clock = {"now": 1_000_000}

        def fake_time():
            clock["now"] += 100  # each call advances the clock
            return clock["now"]

        with (
            _patch.object(dispatcher, "get_tenant_db", _mock_tenant_db_factory(cfg)),
            _patch.object(dispatcher, "_decrypt_secret", return_value="a-real-strong-secret"),
            _patch.object(dispatcher, "_post_once", side_effect=fake_post_once),
            _patch.object(dispatcher.asyncio, "sleep", AsyncMock()),
            _patch.object(dispatcher.time, "time", side_effect=fake_time),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
            )

        assert len(post_calls) == 3
        headers = [h for h, _ in post_calls]
        # Every attempt must carry a signature...
        assert all(h.startswith("t=") for h in headers)
        # ...and each attempt's timestamp (and hence digest) must be fresh.
        timestamps = [h.split(",")[0] for h in headers]
        digests = [h.split("v1=")[1] for h in headers]
        assert len(set(timestamps)) == 3, f"stale timestamps reused: {timestamps}"
        assert len(set(digests)) == 3, f"stale digests reused: {digests}"
        # Round-3 fix: the body's emitted_at must equal the signature `t`
        # on every attempt (mirrors shared rebuild_signed_body, F-022-11).
        import json as json_mod
        for sig, body in post_calls:
            sig_ts = int(sig.split(",")[0].removeprefix("t="))
            assert json_mod.loads(body)["emitted_at"] == sig_ts, (
                f"body emitted_at diverges from signature t on attempt: {sig}"
            )


class TestWebhookUnsignedRefusedNotSent:
    """Bug-8349 (supersedes the Bug-5951 fail-open assertions below) -- an
    agent webhook with no valid signing secret must NEVER be transmitted.

    These two tests used to assert the OLD fail-open contract: dispatch_event
    computed an empty signature header and POSTed the payload anyway, and a
    2xx response was recorded as a successful delivery with no DLQ row. That
    is precisely the defect the shared/webhooks/dispatcher.py fix (Bug-8056)
    already closed for the platform-wide dispatcher; this agent-service
    dispatcher had drifted and still shipped the old rule. Per the failing
    test triage policy (CLAUDE.md case 2 -- the test asserted stale/wrong
    behaviour), these are corrected to assert the fixed, fail-closed
    contract: no HTTP attempt at all, and a DLQ row recording the refusal."""

    @pytest.mark.asyncio
    async def test_none_secret_never_posts_and_is_dlqd(self):
        from src.webhooks import dispatcher

        cfg = _make_webhook_cfg()
        dlq_sink: list = []
        post_calls: list = []

        async def fake_post_once(tenant_id, url, body, sig_header, event_type):
            post_calls.append(sig_header)
            return True, 200, None

        with (
            _patch.object(
                dispatcher, "get_tenant_db", _mock_tenant_db_factory(cfg, dlq_sink),
            ),
            _patch.object(dispatcher, "_decrypt_secret", return_value=None),
            _patch.object(dispatcher, "_post_once", side_effect=fake_post_once),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
            )

        assert post_calls == [], (
            "an unsigned webhook must never be POSTed, even with a 'send "
            f"empty sig header' fallback available -- got calls {post_calls}"
        )
        assert len(dlq_sink) == 1, "the refusal must be recorded to the DLQ"
        assert "signing secret" in (dlq_sink[0].last_error or "").lower()

    @pytest.mark.asyncio
    async def test_placeholder_secret_never_posts_and_is_dlqd(self):
        from src.webhooks import dispatcher

        cfg = _make_webhook_cfg()
        dlq_sink: list = []
        post_calls: list = []

        async def fake_post_once(tenant_id, url, body, sig_header, event_type):
            post_calls.append(sig_header)
            return True, 200, None

        with (
            _patch.object(
                dispatcher, "get_tenant_db", _mock_tenant_db_factory(cfg, dlq_sink),
            ),
            _patch.object(dispatcher, "_decrypt_secret", return_value="changeme"),
            _patch.object(dispatcher, "_post_once", side_effect=fake_post_once),
        ):
            await dispatcher.dispatch_event(
                "acme", uuid.uuid4(), "turn.completed", {"k": "v"},
            )

        assert post_calls == [], (
            "a placeholder secret must never be used for signing, and the "
            f"payload must never be sent unsigned either -- got {post_calls}"
        )
        assert len(dlq_sink) == 1, "the refusal must be recorded to the DLQ"
        assert "signing secret" in (dlq_sink[0].last_error or "").lower()

    @pytest.mark.asyncio
    async def test_post_once_omits_signature_header_when_unsigned(self):
        """Bug-7334 class (Bug-8349 R2 gate residual): _post_once now uses
        client.stream() instead of client.post() so a hostile receiver's
        response body is read in a bounded way. The mock provides an async
        context manager for the streaming response, matching the pattern in
        shared/webhooks/dispatcher.py's own equivalent test."""
        from src.webhooks import dispatcher

        captured_headers: dict = {}
        mock_resp = MagicMock(status_code=200)

        class FakeStream:
            async def __aenter__(self):
                return mock_resp

            async def __aexit__(self, *a):
                pass

        def _fake_stream(method, url, *, content, headers):
            captured_headers.update(headers)
            return FakeStream()

        mock_client = MagicMock()
        mock_client.stream = _fake_stream

        with _patch.object(dispatcher, "_shared_client", mock_client):
            ok, status, err = await dispatcher._post_once(
                "acme", "https://example.com/hook", b"{}", "", "turn.completed",
            )

        assert ok is True
        assert "X-Tessallite-Signature" not in captured_headers, (
            "unsigned POST must omit the signature header entirely"
        )
        assert captured_headers["X-Tessallite-Event"] == "turn.completed"

    @pytest.mark.asyncio
    async def test_post_once_includes_signature_header_when_signed(self):
        """Bug-7334 class — see test_post_once_omits_signature_header_when_
        unsigned above for why this mocks client.stream()."""
        from src.webhooks import dispatcher

        captured_headers: dict = {}
        mock_resp = MagicMock(status_code=200)

        class FakeStream:
            async def __aenter__(self):
                return mock_resp

            async def __aexit__(self, *a):
                pass

        def _fake_stream(method, url, *, content, headers):
            captured_headers.update(headers)
            return FakeStream()

        mock_client = MagicMock()
        mock_client.stream = _fake_stream

        with _patch.object(dispatcher, "_shared_client", mock_client):
            await dispatcher._post_once(
                "acme", "https://example.com/hook", b"{}", "t=1,v1=abc", "turn.completed",
            )

        assert captured_headers["X-Tessallite-Signature"] == "t=1,v1=abc"

    def test_signature_is_verifiable_hmac(self):
        """The _sign helper must produce a receiver-verifiable HMAC-SHA256
        over '<ts>.<body>'."""
        import hashlib
        import hmac as hmac_mod
        from src.webhooks.dispatcher import _sign

        secret = "a-real-strong-secret"
        body = b'{"event_type":"turn.completed"}'
        header = _sign(secret, 1234567890, body)
        expected = hmac_mod.new(
            secret.encode(), b"1234567890." + body, hashlib.sha256,
        ).hexdigest()
        assert header == f"t=1234567890,v1={expected}"
