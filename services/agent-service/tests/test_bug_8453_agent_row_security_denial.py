"""Bug-8453 — the conversational agent must not tell a business user "there is
no data for that" when the truthful answer is "your row-security policy grants
you no rows".

This is the most user-visible of the four remaining ``/execute`` consumers: the
other three render a blank surface, but the agent makes a positive FALSE
ASSERTION about the business in natural language, and hides a possibly
misconfigured policy from the person best placed to report it.

Two legs, because a producer fix with an unwired consumer is the exact gap this
codebase keeps hitting:
  1. ``QueryExecution`` actually carries the denial off the wire.
  2. The narration prompt actually branches on it.
"""
from __future__ import annotations

import pytest

from shared.security.execute_contract import ROW_SECURITY_DENY_ALL_RULE_ID
from src.narrate.narrate import _build_format_block, _build_narrate_prompt


def _execution(rows, *, denied: bool, columns=("region", "revenue")):
    from src.exec.query import QueryExecution

    return QueryExecution(
        sql="SELECT 1",
        columns=list(columns),
        rows=list(rows),
        rows_returned=len(rows),
        route_type="source",
        routed_sql=None,
        aggregate_id=None,
        pocket_id=None,
        execution_ms=1,
        truncated=False,
        security_rules_applied=(
            (ROW_SECURITY_DENY_ALL_RULE_ID,) if denied else ()
        ),
        row_security_denied=denied,
    )


class TestQueryExecutionCarriesTheDenial:
    def test_defaults_are_backwards_compatible(self):
        ex = _execution([], denied=False)
        assert ex.security_rules_applied == ()
        assert ex.row_security_denied is False

    def test_denial_is_recorded(self):
        ex = _execution([], denied=True)
        assert ex.row_security_denied is True
        assert ROW_SECURITY_DENY_ALL_RULE_ID in ex.security_rules_applied


class TestNarrationBranchesOnTheDenial:
    def _block(self, *, denied: bool, is_empty: bool = True) -> str:
        return _build_format_block(
            columns=["region", "revenue"],
            sample_rows=[],
            output_format="text",
            is_empty=is_empty,
            row_security_denied=denied,
        )

    def test_denied_prompt_names_the_permissions_restriction(self):
        block = self._block(denied=True).lower()
        assert "permission" in block
        assert "row-level security" in block or "row level security" in block

    def test_denied_prompt_forbids_asserting_the_data_does_not_exist(self):
        """The whole defect: the agent asserted absence of data. The prompt must
        explicitly forbid that framing, not merely omit it."""
        block = self._block(denied=True)
        assert "no data was found" not in block.lower()
        assert "does not exist" in block.lower()

    def test_denied_prompt_does_not_leak_the_rule_contents(self):
        """The agent is given rule IDS only; it must not be invited to describe
        the row-security policy itself."""
        block = self._block(denied=True).lower()
        assert "do not describe the security rules" in block

    def test_undenied_empty_result_keeps_the_plain_no_data_wording(self):
        """Regression guard the other way: a genuinely empty result must NOT
        start claiming a permissions problem."""
        block = self._block(denied=False).lower()
        assert "no data was found" in block
        assert "permission" not in block

    def test_denial_takes_priority_over_a_nonempty_row_set(self):
        """A deny-all can still return a row (COUNT(*) over WHERE 0 = 1 gives
        0), so the denial branch must not be gated on is_empty."""
        block = self._block(denied=True, is_empty=False).lower()
        assert "permission" in block


class TestNarratePromptAnswerBlock:
    def _prompt(self, *, denied: bool) -> str:
        _system, user = _build_narrate_prompt(
            project_system_prompt="sys",
            user_message="what were sales?",
            execution=_execution([], denied=denied),
        )
        return user

    def test_denied_answer_block_states_a_permissions_restriction(self):
        text = self._prompt(denied=True).lower()
        assert "permissions restriction" in text
        assert "not an absence of data" in text

    def test_undenied_answer_block_states_no_data(self):
        text = self._prompt(denied=False).lower()
        assert "no data was found for this query" in text
        assert "permissions restriction" not in text


# ---------------------------------------------------------------------------
# R2 deep-review findings B2 + S1, applied.
#
# S1: every test above builds ``QueryExecution`` DIRECTLY, so the producer half
# was unproven — deleting the two lines in ``execute_query`` that derive the
# denial left this file fully green. These exercise the real wire path.
#
# B2: ``execute_query`` is the single agent-service /execute chokepoint with
# THREE call sites (direct query, compound step, recipe step) and only the
# direct one consulted the denial. A denied step returned COUNT(*) = 0 from
# ``WHERE 0 = 1`` straight into a combine expression, so the agent stated a
# fabricated business figure. The invariant now lives at the chokepoint.
# ---------------------------------------------------------------------------

import types as _types
import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

from src.exec.query import (
    QueryExecutionError,
    RowSecurityDeniedQueryError,
    execute_query,
)
from src.tools.spec import QueryToolCall

_MODEL = _uuid.uuid4()


def _wire_client(payload):
    class _Resp:
        status_code = 200

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    return _Client


async def _execute_with_payload(payload, **kw):
    db = AsyncMock()
    db.get = AsyncMock(
        return_value=_types.SimpleNamespace(id=_MODEL, slug="test-model")
    )
    meas = MagicMock()
    meas.all.return_value = [("revenue", "SUM")]
    db.execute = AsyncMock(return_value=meas)
    call = QueryToolCall(
        model_id=str(_MODEL), measures=["revenue"],
        dimensions=[], where=[], having=[], sort=[],
    )
    with patch("src.exec.query.httpx.AsyncClient", _wire_client(payload)):
        return await execute_query(
            db, call, "jwt", allowed_model_ids={_MODEL}, **kw
        )


_DENIED_WIRE = {
    # The realistic deny-all shape: a row IS returned and it contains 0.
    "rows": [{"revenue": 0}],
    "columns": ["revenue"],
    "route_type": "source",
    "security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID],
}


class TestDenialIsReadOffTheWire:
    """The producer half: execute_query must derive the denial from the
    /execute payload, and a returned row must NOT suppress it."""

    @pytest.mark.asyncio
    async def test_deny_all_sentinel_is_read_from_the_payload(self):
        ex = await _execute_with_payload(
            _DENIED_WIRE, allow_row_security_denial=True,
        )
        assert ex.row_security_denied is True
        assert ROW_SECURITY_DENY_ALL_RULE_ID in ex.security_rules_applied

    @pytest.mark.asyncio
    async def test_narrowing_rule_is_carried_but_is_not_a_denial(self):
        ex = await _execute_with_payload({
            "rows": [{"revenue": 5}], "columns": ["revenue"],
            "route_type": "source", "security_rules_applied": ["region-rule"],
        })
        assert ex.row_security_denied is False
        assert ex.security_rules_applied == ("region-rule",)

    @pytest.mark.asyncio
    async def test_absent_field_is_not_a_denial(self):
        ex = await _execute_with_payload(
            {"rows": [], "columns": [], "route_type": "source"}
        )
        assert ex.row_security_denied is False
        assert ex.security_rules_applied == ()


class TestChokepointFailsClosed:
    """B2: the invariant lives at the chokepoint, so a caller that has NOT been
    taught to render a denial cannot consume one as data."""

    @pytest.mark.asyncio
    async def test_denial_raises_by_default(self):
        with pytest.raises(RowSecurityDeniedQueryError) as exc:
            await _execute_with_payload(_DENIED_WIRE)
        # The message must name the restriction, not blame the query.
        assert "permissions restriction" in str(exc.value).lower()
        assert "not an absence of data" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_the_denial_error_is_a_query_execution_error(self):
        """Compound steps and recipe steps already catch QueryExecutionError
        and REFUSE, so subclassing it is what makes them fail closed without
        each having to be taught about row security."""
        assert issubclass(RowSecurityDeniedQueryError, QueryExecutionError)

    @pytest.mark.asyncio
    async def test_only_an_opted_in_caller_receives_a_denied_execution(self):
        ex = await _execute_with_payload(
            _DENIED_WIRE, allow_row_security_denial=True,
        )
        assert ex.row_security_denied is True

    @pytest.mark.asyncio
    async def test_a_narrowed_result_is_never_refused(self):
        """Rows the caller IS permitted to see are correct data; refusing them
        would break every legitimately row-restricted user."""
        ex = await _execute_with_payload({
            "rows": [{"revenue": 5}], "columns": ["revenue"],
            "route_type": "source", "security_rules_applied": ["region-rule"],
        })
        assert ex.rows == [{"revenue": 5}]


@pytest.mark.asyncio
async def test_direct_query_path_opts_in_so_it_can_narrate_the_denial():
    """The one caller allowed to proceed on a denial is the direct-query path,
    because narrate.py has a branch that tells the user their access is
    restricted. If that opt-in is ever removed the user would get a bare
    refusal instead of the explanation this lane added."""
    import inspect

    import src.pipeline as pipeline

    src = inspect.getsource(pipeline)
    assert "allow_row_security_denial=True" in src, (
        "the direct-query path no longer opts in; a denied query would refuse "
        "instead of narrating the restriction"
    )
    # And exactly ONE caller opts in -- compound/recipe must stay fail-closed.
    assert src.count("allow_row_security_denial=True") == 1
    import src.exec.recipe as recipe

    assert "allow_row_security_denial" not in inspect.getsource(recipe), (
        "the recipe step must NOT opt out of the chokepoint refusal"
    )


def test_recipe_denial_names_the_permissions_restriction():
    """R3 finding B-3. Subclassing QueryExecutionError made the recipe path
    fail CLOSED (no fabricated zero reaches evaluate_combine) but NOT truthful:
    the reason ladder had no branch for it, so it fell to "recipe_failed" and
    told the user "The recipe could not be executed. Please try again" --
    advising a retry for something that did not fail and never will. Exactly
    the misattribution the compound branch got a dedicated handler to avoid."""
    import inspect
    import re

    import src.pipeline as pipeline

    src_text = inspect.getsource(pipeline)
    handler = src_text.split("except RecipeExecutionError as exc:")[1][:3000]
    assert "RowSecurityDeniedQueryError" in handler, (
        "the recipe reason ladder does not recognise a row-security denial"
    )
    assert "row_security_denied" in handler
    # The denial branch must not be the generic retry advice. Bound the slice
    # at the NEXT branch so the following else: (which legitimately says "try
    # again" for a real failure) is not read as part of it.
    denial_msg = handler.split('elif reason == "row_security_denied":')[1]
    denial_msg = re.split(r"\n        (?:elif|else)\b", denial_msg)[0]
    assert "please try again" not in denial_msg.lower(), denial_msg
    assert "permissions restriction" in denial_msg.lower()
    assert "not an absence of data" in denial_msg.lower()


def test_every_execute_query_call_site_is_accounted_for():
    """The enumeration that was wrong three times, made mechanical for this
    module: any NEW execute_query call site must either opt in explicitly (and
    therefore have a denial-rendering path) or inherit the chokepoint refusal.
    A silent third state is what produced the compound/recipe gap."""
    import inspect
    import re

    import src.pipeline as pipeline
    import src.exec.recipe as recipe

    sites = 0
    opted_in = 0
    for mod in (pipeline, recipe):
        text = inspect.getsource(mod)
        for m in re.finditer(r"await execute_query\((.*?)\n        \)", text, re.S):
            sites += 1
            if "allow_row_security_denial=True" in m.group(1):
                opted_in += 1
    assert sites >= 3, f"expected the 3 known call sites, found {sites}"
    assert opted_in == 1, (
        f"exactly ONE call site (the direct-query path, which narrates the "
        f"denial) may opt out of the chokepoint refusal; found {opted_in}"
    )
