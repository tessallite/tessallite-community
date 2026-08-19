"""Tests for agent session memory management (Phase 3, Block E)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

import copy
import types

from src.prompt import assembler as _assembler_module
from src.prompt.assembler import (
    _format_history,
    _normalise_previous_plan,
    _strip_answer_markup,
    _truncation_boundary,
    assemble_prompt,
)
from .conftest import (
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
    make_turn,
)


class TestTruncationBoundary:

    def test_depth_greater_than_turns(self):
        turns = [make_turn(turn_index=i) for i in range(5)]
        assert _truncation_boundary(turns, 10) == 0

    def test_depth_equal_to_turns(self):
        turns = [make_turn(turn_index=i) for i in range(5)]
        assert _truncation_boundary(turns, 5) == 0

    def test_depth_less_than_turns(self):
        turns = [make_turn(turn_index=i) for i in range(10)]
        assert _truncation_boundary(turns, 5) == 5

    def test_depth_one(self):
        turns = [make_turn(turn_index=i) for i in range(3)]
        assert _truncation_boundary(turns, 1) == 2

    def test_depth_zero_returns_zero(self):
        turns = [make_turn(turn_index=i) for i in range(5)]
        assert _truncation_boundary(turns, 0) == 0

    def test_empty_turns(self):
        assert _truncation_boundary([], 5) == 0


class TestFormatHistory:

    def test_no_turns(self):
        assert _format_history([]) == "(first turn — no prior context)"

    def test_all_within_depth(self):
        turns = [
            make_turn(turn_index=0, user_message="Q1", answer_text="A1"),
            make_turn(turn_index=1, user_message="Q2", answer_text="A2"),
        ]
        result = _format_history(turns, session_history_depth=5)
        assert "User (turn 1): Q1" in result
        assert "Assistant: A1" in result
        assert "User (turn 2): Q2" in result
        assert "Assistant: A2" in result
        assert "[earlier question]" not in result

    def test_truncation_older_turns(self):
        conv_id = uuid.uuid4()
        turns = [
            make_turn(conversation_id=conv_id, turn_index=i, user_message=f"Q{i}", answer_text=f"A{i}")
            for i in range(10)
        ]
        result = _format_history(turns, session_history_depth=5)
        for i in range(5):
            assert f"[earlier question] (turn {i + 1}): Q{i}" in result
            assert f"Assistant: A{i}" not in result
        for i in range(5, 10):
            assert f"User (turn {i + 1}): Q{i}" in result
            assert f"Assistant: A{i}" in result

    def test_depth_one_only_last_full(self):
        turns = [
            make_turn(turn_index=0, user_message="old", answer_text="old-answer"),
            make_turn(turn_index=1, user_message="recent", answer_text="recent-answer"),
        ]
        result = _format_history(turns, session_history_depth=1)
        assert "[earlier question] (turn 1): old" in result
        assert "old-answer" not in result
        assert "User (turn 2): recent" in result
        assert "Assistant: recent-answer" in result

    def test_no_answer_text(self):
        turns = [make_turn(turn_index=0, user_message="Q", answer_text=None)]
        result = _format_history(turns, session_history_depth=5)
        assert "User (turn 1): Q" in result
        assert "Assistant" not in result


class TestStripAnswerMarkup:
    """R7 (F8, SOFTENED): strip formatting markup from replayed answers WITHOUT
    dropping any referent values and WITHOUT truncation. Follow-ups resolve
    against the prior answer's business content, so every number, name, and
    label the answer carried must survive the strip. The strip is gated by the
    answer output format: only formats whose narrator was instructed to emit
    markup (html/rich_html/markup) are stripped; plain/json replay verbatim."""

    def test_plain_text_unchanged(self):
        # The spec keeps raw replay for plain answers — the transform must be a
        # no-op on text that carries no markup.
        text = "Total revenue last month was USD 2,847,391 across 5 regions."
        assert _strip_answer_markup(text, "plain") == text

    def test_plain_text_with_angle_brackets_verbatim(self):
        # Review R1 finding 1 regression: a plain answer may contain "<"/">"
        # as REAL content (comparison operators). The HTML pass must not run,
        # or "1000" and "score >" would be silently deleted.
        text = "Rows kept where amount < 1000 and score > 5; 42 rows matched."
        assert _strip_answer_markup(text, "plain") == text
        assert _strip_answer_markup(text, "json") == text

    def test_plain_text_with_markers_verbatim(self):
        # Plain answers keep "*"/"_" content untouched (no markdown pass).
        text = "Columns customer_id and product_id; sold 3*4 and 5*6 units."
        assert _strip_answer_markup(text, "plain") == text

    def test_unknown_format_verbatim(self):
        text = "<p>USD 100</p>"
        assert _strip_answer_markup(text, "something_new") == text

    def test_html_tags_removed_content_kept(self):
        answer = (
            "<p><strong>Total revenue:</strong> USD 2,847,391</p>"
            "<ul><li>North: USD 1,200,000</li><li>South: USD 900,391</li></ul>"
        )
        result = _strip_answer_markup(answer, "html")
        # Every referent value survives.
        for referent in ("Total revenue:", "USD 2,847,391", "North:",
                         "USD 1,200,000", "South:", "USD 900,391"):
            assert referent in result
        # No HTML tags remain.
        assert "<" not in result and ">" not in result

    def test_list_items_stay_separated(self):
        # Killer risk: a follow-up like "the second one" needs the items to
        # remain individually identifiable — block tags must not fuse them.
        answer = "<ul><li>Acme Corp</li><li>Globex</li><li>Initech</li></ul>"
        result = _strip_answer_markup(answer, "rich_html")
        assert "Acme Corp" in result
        assert "Globex" in result
        assert "Initech" in result
        # They are on separate lines, not fused into one token.
        assert "AcmeGlobex" not in result.replace(" ", "")
        lines = [ln for ln in result.splitlines() if ln]
        assert lines == ["Acme Corp", "Globex", "Initech"]

    def test_table_cells_stay_word_separated(self):
        answer = (
            "<table><thead><tr><th>Merchant</th><th>Revenue</th></tr></thead>"
            "<tbody><tr><td>Acme</td><td>USD 500</td></tr>"
            "<tr><td>Globex</td><td>USD 300</td></tr></tbody></table>"
        )
        result = _strip_answer_markup(answer, "rich_html")
        for referent in ("Merchant", "Revenue", "Acme", "USD 500",
                         "Globex", "USD 300"):
            assert referent in result
        # Cells stay word-separated (whitespace between a cell value and the
        # next), so no two referents fuse into one token.
        assert "AcmeUSD" not in result
        # Each table row is on its own line, so "the second row" is resolvable.
        assert "Acme" in result.splitlines()[1]
        assert "Globex" in result.splitlines()[2]

    def test_html_entities_decoded(self):
        # Referent values with entities must read correctly for follow-up match.
        answer = "<p>Ben &amp; Jerry&#39;s: &pound;42 &lt;threshold&gt;</p>"
        result = _strip_answer_markup(answer, "html")
        assert "Ben & Jerry's" in result
        assert "£42" in result
        assert "<threshold>" in result

    def test_html_comment_removed(self):
        answer = "<p>USD 500</p><!-- note > here --><p>USD 300</p>"
        result = _strip_answer_markup(answer, "html")
        assert "USD 500" in result
        assert "USD 300" in result
        assert "note" not in result
        assert "-->" not in result

    def test_html_unescaped_comparison_prose_preserved(self):
        # Review R2 finding 1 regression: LLM html answers routinely leave
        # prose comparisons unescaped. A bare <[^>]+> tag regex deleted
        # "<1% but >" — the surviving replay then asserted a DIFFERENT number
        # (silent wrong-numbers class). The letter-anchored regex must leave
        # non-tag-shaped "<"/">" spans untouched.
        answer = "<p>Growth was <1% but >2% in Q2</p>"
        result = _strip_answer_markup(answer, "html")
        assert "Growth was <1% but >2% in Q2" in result

    def test_html_unescaped_lt_gt_never_swallows_lines(self):
        # Review R2 finding 1 regression (cross-line): a "<" on one line and a
        # ">" on a later line must never pair up and delete the lines between.
        answer = (
            "<p>a < b</p>\n"
            "<p>keep me 42</p>\n"
            "<p>c > d</p>"
        )
        result = _strip_answer_markup(answer, "html")
        assert "a < b" in result
        assert "keep me 42" in result
        assert "c > d" in result

    def test_html_score_threshold_prose_preserved(self):
        answer = "<p>Score <3 and grade > B</p>"
        result = _strip_answer_markup(answer, "html")
        assert "Score <3 and grade > B" in result

    def test_html_non_whitelisted_tag_shaped_span_preserved(self):
        # Review R3 finding 1 regression: "<cost and qty>" is tag-SHAPED but
        # not a narrator tag — a letter-anchored generic regex deleted it and
        # fused "price0". The whitelist must leave unknown names as literal
        # text (fail-safe residue, never referent loss).
        answer = "<p>price<cost and qty>0</p>"
        result = _strip_answer_markup(answer, "html")
        assert "price<cost and qty>0" in result
        answer2 = "<p>revenue<threshold> is 5</p>"
        result2 = _strip_answer_markup(answer2, "rich_html")
        assert "revenue<threshold> is 5" in result2

    def test_html_single_letter_tag_exact_form_only(self):
        # <b>/<i> strip only in exact attribute-less form; "<b and c>" is
        # prose, not markup — stripping it would fuse "qty2".
        answer = "<p><b>USD 500</b> where qty<b and c>2</p>"
        result = _strip_answer_markup(answer, "html")
        assert "USD 500" in result
        assert "<b>" not in result.split("qty")[0]
        assert "qty<b and c>2" in result

    def test_html_inline_whitelist_tags_stripped(self):
        answer = "<p><em>Net</em> was <code>42%</code> (<i>approx</i>)</p>"
        result = _strip_answer_markup(answer, "html")
        assert "Net" in result
        assert "42%" in result
        assert "approx" in result
        for tag in ("<em>", "</em>", "<code>", "</code>", "<i>", "</i>"):
            assert tag not in result

    def test_html_sectioning_tags_leave_line_boundary(self):
        # Review R4 finding 1 regression: sectioning/boundary tags removed to
        # empty fused adjacent referents ("Q2 ResultsRevenue", "1B: 2"). They
        # must leave a line boundary like the other block tags.
        answer = (
            "<header>Q2 Results</header>"
            "<section>Revenue: USD 500</section>"
            "<footer>Total: USD 900</footer>"
        )
        result = _strip_answer_markup(answer, "html")
        lines = [ln for ln in result.splitlines() if ln]
        assert lines == ["Q2 Results", "Revenue: USD 500", "Total: USD 900"]
        answer2 = "<section>A: 1</section><section>B: 2</section>"
        result2 = _strip_answer_markup(answer2, "html")
        assert "1B" not in result2
        assert [ln for ln in result2.splitlines() if ln] == ["A: 1", "B: 2"]

    def test_html_hr_and_img_leave_boundary(self):
        answer = "Before<hr>After: 42"
        result = _strip_answer_markup(answer, "html")
        assert "BeforeAfter" not in result
        assert "Before" in result and "After: 42" in result
        answer2 = "See chart<img src='x.png'>Total 500"
        result2 = _strip_answer_markup(answer2, "rich_html")
        assert "chartTotal" not in result2
        assert "See chart" in result2 and "Total 500" in result2

    def test_html_offscript_block_tags_leave_boundary(self):
        # blockquote/pre/tfoot are common off-script LLM emissions — block
        # boundary, no fusion, no residue.
        answer = (
            "<blockquote>Note: partial data</blockquote>"
            "<pre>SUM = 900</pre><tfoot>Total: USD 900</tfoot>"
        )
        result = _strip_answer_markup(answer, "html")
        assert "Note: partial data" in result
        assert "SUM = 900" in result
        assert "Total: USD 900" in result
        assert "dataSUM" not in result
        assert "900Total" not in result
        for tag in ("<blockquote>", "<pre>", "<tfoot>"):
            assert tag not in result

    def test_markdown_emphasis_unwrapped_value_kept(self):
        answer = "**Total:** USD 2,847,391 and *net* was 42%."
        result = _strip_answer_markup(answer, "markup")
        assert "Total:" in result
        assert "USD 2,847,391" in result
        assert "net" in result
        assert "42%" in result
        assert "**" not in result
        # Emphasis markers gone but the wrapped text preserved.
        assert "*net*" not in result

    def test_markdown_snake_case_names_preserved(self):
        # Review R1 finding 2 regression: intra-word "_" pairs must NOT be
        # treated as emphasis — snake_case column names appear routinely in
        # narrated markup answers and were being fused ("customerid").
        answer = "Top columns: customer_id and product_id for 12 rows."
        result = _strip_answer_markup(answer, "markup")
        assert "customer_id" in result
        assert "product_id" in result
        assert "customerid" not in result

    def test_markdown_intra_word_asterisks_preserved(self):
        # Review R1 finding 2 regression: "3*4 and 5*6" are numeric referents,
        # not emphasis — the naive pairing turned them into "34 and 56" (the
        # exact wrong-numbers class R7 exists to prevent).
        answer = "Grouped by customer_first_name; sold 3*4 and 5*6 units."
        result = _strip_answer_markup(answer, "markup")
        assert "customer_first_name" in result
        assert "3*4" in result
        assert "5*6" in result
        assert "34 and 56" not in result

    def test_markdown_bullets_removed_items_kept(self):
        answer = "- Acme: USD 500\n- Globex: USD 300\n- Initech: USD 100"
        result = _strip_answer_markup(answer, "markup")
        assert "Acme: USD 500" in result
        assert "Globex: USD 300" in result
        assert "Initech: USD 100" in result
        # Leading bullet markers removed.
        for line in result.splitlines():
            assert not line.lstrip().startswith("- ")

    def test_markdown_nested_emphasis_fully_unwrapped(self):
        # Review R2 NIT: the inner pair only becomes strippable after the
        # outer unwrap — the bounded re-application clears it without ever
        # touching content.
        answer = "**bold *ital USD 500* bold**"
        result = _strip_answer_markup(answer, "markup")
        assert result == "bold ital USD 500 bold"

    def test_markdown_blank_lines_preserved(self):
        # Markdown line structure is content-bearing — no whitespace collapse.
        answer = "Total: USD 500\n\nBy region:\n- North: USD 300"
        result = _strip_answer_markup(answer, "markup")
        assert "Total: USD 500\n\nBy region:\nNorth: USD 300" == result

    def test_no_truncation_of_long_answer(self):
        # The core R7 guarantee: NEVER truncate. A long enumerated answer (the
        # exact kind a "which was the 400th" follow-up would need) must survive
        # in full.
        items = [f"<li>Merchant {n}: USD {n * 7}</li>" for n in range(1, 400)]
        answer = "<ul>" + "".join(items) + "</ul>"
        result = _strip_answer_markup(answer, "rich_html")
        assert "Merchant 1: USD 7" in result
        assert "Merchant 399: USD 2793" in result
        # Every merchant's referent survives — no cap dropped the tail.
        for n in range(1, 400):
            assert f"Merchant {n}: USD {n * 7}" in result

    def test_lone_emphasis_char_in_value_preserved(self):
        # Stray "*"/"_" inside referents (SKUs, math) must not be eaten even in
        # markup mode — with word-boundary guards no intra-word pair matches.
        answer = "SKU AB_12 sold 3*4 units; codes X_1 and Y_2."
        result = _strip_answer_markup(answer, "markup")
        assert "AB_12" in result
        assert "3*4" in result
        assert "X_1" in result
        assert "Y_2" in result

    def test_empty_and_none_safe(self):
        assert _strip_answer_markup("", "html") == ""
        assert _strip_answer_markup(None, "rich_html") is None

    def test_format_history_strips_markup_in_recent_window(self):
        turn = make_turn(
            turn_index=0,
            user_message="Top merchants?",
            answer_text="<p><strong>Top:</strong> Acme USD 500, Globex USD 300</p>",
        )
        result = _format_history(
            [turn], session_history_depth=5, answer_output_format="rich_html"
        )
        assert "Assistant: " in result
        assert "Acme USD 500" in result
        assert "Globex USD 300" in result
        assert "<p>" not in result and "<strong>" not in result

    def test_format_history_default_format_replays_verbatim(self):
        # Without an explicit format the replay is verbatim (plain default) —
        # angle-bracket content in a plain answer must survive untouched.
        turn = make_turn(
            turn_index=0,
            user_message="How many?",
            answer_text="Rows where amount < 1000: 42.",
        )
        result = _format_history([turn], session_history_depth=5)
        assert "Assistant: Rows where amount < 1000: 42." in result

    def test_format_history_disclosure_then_markup_strip(self):
        # Disclosure removal runs first, then markup strip on the residue; the
        # answer's referents survive both passes.
        turn = make_turn(
            turn_index=0,
            user_message="Revenue?",
            answer_text="<p>USD 100</p> AI-generated, verify independently.",
        )
        result = _format_history(
            [turn],
            session_history_depth=5,
            disclosure_text="AI-generated, verify independently.",
            answer_output_format="html",
        )
        assert "USD 100" in result
        assert "<p>" not in result
        assert "AI-generated" not in result


class TestNormalisePreviousPlanNoMutation:
    """Intake 2026-07-21-format-history-mutates-orm-llm-plan: normalising the
    replayed plan must NOT mutate the shared ORM ``llm_plan`` object (Bug-7935
    copy-on-write class). Deep-copy at the root means an in-session reader of
    the same turn sees the original plan, not a silently migrated one."""

    def test_source_plan_not_mutated_filters_rename(self):
        original = {
            "tool": "query",
            "query": {
                "model_id": "m1",
                "measures": ["rev"],
                "filters": [{"name": "d", "op": "eq", "value": 1}],
            },
        }
        snapshot = copy.deepcopy(original)
        out = _normalise_previous_plan(original)
        # Output migrated filters -> where and injected having/sort.
        assert "where" in out["query"]
        assert out["query"].setdefault("having", "MISSING") == []
        # Source is byte-identical to before the call — no aliasing.
        assert original == snapshot
        assert "filters" in original["query"]
        assert "where" not in original["query"]
        assert "having" not in original["query"]
        assert "sort" not in original["query"]

    def test_nested_query_is_a_copy(self):
        original = {"query": {"model_id": "m1", "filters": []}}
        out = _normalise_previous_plan(original)
        # Mutating the output must not touch the source's nested dict.
        out["query"]["injected"] = True
        assert "injected" not in original["query"]

    def test_nested_lists_and_element_dicts_are_copies(self):
        # Review R1 finding 5: a two-level shallow copy would still alias the
        # filters LIST and its element dicts. Mutate the output's list and its
        # element and prove the source is untouched at every depth.
        original = {
            "query": {
                "model_id": "m1",
                "filters": [{"name": "d", "op": "eq", "value": 1}],
                "sort": [{"name": "rev", "direction": "desc"}],
            },
        }
        out = _normalise_previous_plan(original)
        out["query"]["where"][0]["value"] = 999
        out["query"]["where"].append({"name": "x"})
        out["query"]["sort"][0]["direction"] = "asc"
        assert original["query"]["filters"] == [
            {"name": "d", "op": "eq", "value": 1}
        ]
        assert original["query"]["sort"] == [
            {"name": "rev", "direction": "desc"}
        ]

    def test_format_history_does_not_mutate_turn_plan(self):
        plan = {
            "tool": "query",
            "query": {
                "model_id": "m1",
                "measures": ["rev"],
                "filters": [{"name": "d", "op": "eq", "value": 1}],
            },
        }
        snapshot = copy.deepcopy(plan)
        turn = make_turn(turn_index=0, user_message="Q", answer_text="A")
        turn.llm_plan = plan
        result = _format_history([turn], session_history_depth=5)
        assert "PREVIOUS QUERY PLAN" in result
        # The turn's shared plan object is untouched after rendering.
        assert plan == snapshot


class TestAssemblePromptFormatWiring:
    """Review R3 finding 2: cover the answer_output_format wire END TO END
    (assemble_prompt -> _format_history -> _strip_answer_markup). Without an
    assemble-level test, dropping or mis-defaulting the kwarg at the call site
    keeps every unit test green while the R7 strip silently stops running for
    html/markup tenants (or plain answers get corrupted)."""

    @staticmethod
    def _cfg(output_format: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            primary_model_id=None,
            pinned_model_id=None,
            session_history_depth=20,
            agent_role="data analyst",
            safety_policy="",
            default_locale="en-GB",
            project_brief="",
            disclosure_text="",
            brand_guidelines="",
            content_rules="",
            chart_type_selector="auto",
            chart_renderer="echarts",
            agent_output_format=output_format,
        )

    @staticmethod
    def _db() -> AsyncMock:
        empty_result = MagicMock()
        empty_result.scalars.return_value.all.return_value = []
        empty_result.scalar_one_or_none.return_value = None
        db = AsyncMock()
        db.execute = AsyncMock(return_value=empty_result)
        return db

    async def _assemble(self, cfg, turns):
        with patch.object(
            _assembler_module, "_load_model_profiles", AsyncMock(return_value=[])
        ), patch.object(
            _assembler_module, "list_glossary_cards", AsyncMock(return_value=[])
        ), patch.object(
            _assembler_module, "retrieve_alias_maps", AsyncMock(return_value=[])
        ), patch.object(
            _assembler_module, "_conversation_history",
            AsyncMock(return_value=turns),
        ):
            return await assemble_prompt(
                self._db(),
                cfg,
                conversation_id=uuid.uuid4(),
                user_message="and by region?",
            )

    @pytest.mark.asyncio
    async def test_rich_html_history_stripped_in_user_prompt(self):
        turn = make_turn(
            turn_index=0,
            user_message="Top merchants?",
            answer_text="<p><strong>Acme</strong> USD 500</p>",
        )
        bundle = await self._assemble(self._cfg("rich_html"), [turn])
        # Referent survives, markup gone, in the per-turn USER prompt.
        assert "Acme USD 500" in bundle.user
        assert "<strong>" not in bundle.user
        assert "<p>" not in bundle.user
        # History never enters the cacheable system prefix.
        assert "Acme" not in bundle.system
        # Lane G contract: system_sections joining reproduces bundle.system.
        rebuilt = []
        for idx, (heading, body) in enumerate(bundle.system_sections):
            if idx > 0:
                rebuilt.append("")
            rebuilt += [heading, body]
        assert "\n".join(rebuilt) == bundle.system

    @pytest.mark.asyncio
    async def test_plain_history_replays_verbatim(self):
        # R4 finding 2: the text must NOT be invariant under the html strip —
        # "&amp;" would be rewritten to "&" and "<em>" would be removed by a
        # wrong-direction mis-wire (always-html), so verbatim survival of both
        # proves the plain gate actually reached _strip_answer_markup.
        turn = make_turn(
            turn_index=0,
            user_message="How many?",
            answer_text="Ben &amp; Jerry rows where amount < 1000: <em>42</em>.",
        )
        bundle = await self._assemble(self._cfg("plain"), [turn])
        assert (
            "Assistant: Ben &amp; Jerry rows where amount < 1000: <em>42</em>."
            in bundle.user
        )


class TestConversationStats:

    @pytest.mark.asyncio
    async def test_stats_empty(self, client):
        db = make_mock_db()
        db.scalar = AsyncMock(side_effect=[0, None])

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/admin/agent/conversations/stats?project_id={TEST_PROJECT_ID}"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["oldest_at"] is None

    @pytest.mark.asyncio
    async def test_stats_with_data(self, client):
        db = make_mock_db()
        oldest = datetime(2025, 6, 1, tzinfo=timezone.utc)
        db.scalar = AsyncMock(side_effect=[42, oldest])

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"/api/v1/admin/agent/conversations/stats?project_id={TEST_PROJECT_ID}"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 42
        assert "2025-06-01" in data["oldest_at"]


class TestPurgeConversations:

    @pytest.mark.asyncio
    async def test_purge_by_age(self, client):
        db = make_mock_db()
        conv_id = uuid.uuid4()

        conv_result = MagicMock()
        conv_result.all.return_value = [(conv_id,)]
        db.execute = AsyncMock(return_value=conv_result)

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(
                f"/api/v1/admin/agent/conversations/purge?project_id={TEST_PROJECT_ID}&older_than_days=30"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_conversations"] == 1
        assert data["older_than_days"] == 30

    @pytest.mark.asyncio
    async def test_purge_all(self, client):
        db = make_mock_db()
        ids = [uuid.uuid4() for _ in range(5)]

        conv_result = MagicMock()
        conv_result.all.return_value = [(i,) for i in ids]
        db.execute = AsyncMock(return_value=conv_result)

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(
                f"/api/v1/admin/agent/conversations/purge?project_id={TEST_PROJECT_ID}&older_than_days=0"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_conversations"] == 5
        assert data["older_than_days"] == 0

    @pytest.mark.asyncio
    async def test_purge_nothing(self, client):
        db = make_mock_db()

        conv_result = MagicMock()
        conv_result.all.return_value = []
        db.execute = AsyncMock(return_value=conv_result)

        with patch("src.api.maintenance.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(
                f"/api/v1/admin/agent/conversations/purge?project_id={TEST_PROJECT_ID}&older_than_days=365"
            )
        assert resp.status_code == 200
        assert resp.json()["deleted_conversations"] == 0
