"""R2 (F2) + intake-fix guards — judge distilled evidence pack.

The judge must receive ALL evidential context (project/model/grounding layers,
plus the retrieved GROUNDING MATCHES cards the planner saw in two-tier /
retrieval-only glossary modes) while NON-evidential planner boilerplate (TASK
examples, RUNTIME INPUT ROBUSTNESS, OUTPUT FORMAT schemas) is stripped. It must
NEVER judge on less than the planner had: any distillation failure falls back to
the full prompt.

Test escape: the judge re-received the full planner prompt verbatim and never
saw the two-tier retrieved cards (intake bug). Guard: these assert the evidence
pack keeps/strips the right sections, always includes the retrieved cards, and
falls back to full on every degenerate input. Tier: T1 (producer/consumer
contract — the judge's evidence view) + T2 (intake regression guard).
"""
import pytest

from src.judge.judge import (
    build_judge_evidence,
    _resolve_judge_context_mode,
    _JUDGE_EVIDENTIAL_HEADINGS,
    _JUDGE_NON_EVIDENTIAL_HEADINGS,
)


_SECTIONS = [
    ("## TASK", "12 worked examples ... boilerplate"),
    ("## RUNTIME INPUT ROBUSTNESS", "trust hierarchy ... boilerplate"),
    ("## PROJECT CONTEXT", "role: analyst; safety: no competitor mentions"),
    ("## AVAILABLE MODELS", "Model modely\nMeasures: base_amount\nDimensions: country"),
    ("## GROUNDING", "GLOSSARY: ARR = annual recurring revenue"),
    ("## CROSS-MODEL RECIPES", "Recipe: blended_margin"),
    ("## OUTPUT FORMAT", "JSON schema ... validation gate ... boilerplate"),
]

# Byte-for-byte reproduction of the planner ``system`` string from the sections
# (mirrors assemble_prompt's sys_parts construction).
def _joined(sections):
    parts = []
    for idx, (h, b) in enumerate(sections):
        if idx > 0:
            parts.append("")
        parts += [h, b]
    return "\n".join(parts)


_FULL_SYSTEM = _joined(_SECTIONS)


# ── Distilled mode: keep evidential, strip boilerplate ─────────────────────

def test_distilled_keeps_all_evidential_sections():
    out = build_judge_evidence(_FULL_SYSTEM, _SECTIONS, None, "distilled")
    assert "## PROJECT CONTEXT" in out
    assert "role: analyst" in out
    assert "## AVAILABLE MODELS" in out
    assert "base_amount" in out
    assert "## GROUNDING" in out
    assert "annual recurring revenue" in out
    assert "## CROSS-MODEL RECIPES" in out
    assert "blended_margin" in out


def test_distilled_strips_non_evidential_boilerplate():
    out = build_judge_evidence(_FULL_SYSTEM, _SECTIONS, None, "distilled")
    assert "## TASK" not in out
    assert "12 worked examples" not in out
    assert "## RUNTIME INPUT ROBUSTNESS" not in out
    assert "## OUTPUT FORMAT" not in out
    assert "validation gate" not in out


def test_strip_and_keep_sets_are_disjoint_and_complete():
    # Review R1 finding 1 — distillation is a STRIP-list: only known
    # boilerplate is removed; everything else (including unknown headings) is
    # kept. The two sets must be disjoint and together cover the assembler's
    # section headings (the producer-parity test in
    # test_agent_grounding_lane_b.py asserts the union against a real bundle).
    assert _JUDGE_NON_EVIDENTIAL_HEADINGS == frozenset({
        "## TASK",
        "## RUNTIME INPUT ROBUSTNESS",
        "## OUTPUT FORMAT",
    })
    assert _JUDGE_EVIDENTIAL_HEADINGS == frozenset({
        "## PROJECT CONTEXT",
        "## AVAILABLE MODELS",
        "## GROUNDING",
        "## CROSS-MODEL RECIPES",
    })
    assert not (_JUDGE_NON_EVIDENTIAL_HEADINGS & _JUDGE_EVIDENTIAL_HEADINGS)


# ── Intake fix: retrieved GROUNDING MATCHES cards reach the judge ──────────

def test_distilled_includes_retrieved_grounding_cards():
    cards = "ARR — annual recurring revenue (matched to this question)"
    out = build_judge_evidence(_FULL_SYSTEM, _SECTIONS, cards, "distilled")
    assert "## GROUNDING MATCHES" in out
    assert "matched to this question" in out


def test_full_mode_includes_retrieved_grounding_cards():
    # In two-tier mode the cards live in the user suffix even under 'full'
    # judge context — they must still reach the judge.
    cards = "CRR — customer retention rate (matched)"
    out = build_judge_evidence(_FULL_SYSTEM, _SECTIONS, cards, "full")
    assert out.startswith(_FULL_SYSTEM)
    assert "## GROUNDING MATCHES" in out
    assert "customer retention rate" in out


def test_no_grounding_matches_omits_the_block():
    out = build_judge_evidence(_FULL_SYSTEM, _SECTIONS, "", "distilled")
    assert "## GROUNDING MATCHES" not in out
    out2 = build_judge_evidence(_FULL_SYSTEM, _SECTIONS, None, "distilled")
    assert "## GROUNDING MATCHES" not in out2


# ── Full mode: verbatim planner prompt ─────────────────────────────────────

def test_full_mode_returns_verbatim_system():
    out = build_judge_evidence(_FULL_SYSTEM, _SECTIONS, None, "full")
    assert out == _FULL_SYSTEM
    assert "## TASK" in out
    assert "## OUTPUT FORMAT" in out


# ── Correctness fallbacks: never judge on less evidence ────────────────────

def test_missing_sections_falls_back_to_full():
    # An older caller that only passes the joined string must get the FULL
    # prompt — distilling blindly would drop evidence.
    out = build_judge_evidence(_FULL_SYSTEM, None, None, "distilled")
    assert out == _FULL_SYSTEM


def test_empty_sections_falls_back_to_full():
    out = build_judge_evidence(_FULL_SYSTEM, [], None, "distilled")
    assert out == _FULL_SYSTEM


def test_unknown_heading_is_kept_not_dropped():
    # Review R1 finding 1 — fail-safe direction: a renamed or brand-new planner
    # section the judge does not recognise must be KEPT (extra distractor
    # tokens), never dropped (lost evidence).
    drifted = [
        ("## SOMETHING NEW", "evidential content the judge must see"),
        ("## GROUNDING RULES", "renamed grounding layer"),
        ("## TASK", "boilerplate"),
    ]
    out = build_judge_evidence(_joined(drifted), drifted, None, "distilled")
    assert "evidential content the judge must see" in out
    assert "renamed grounding layer" in out
    # known boilerplate still stripped
    assert "boilerplate" not in out


def test_all_sections_stripped_falls_back_to_full():
    # Degenerate input: every section is in the strip-list — never hand the
    # judge an empty evidence view; fall back to the full prompt.
    only_boilerplate = [
        ("## TASK", "examples"),
        ("## OUTPUT FORMAT", "schemas"),
    ]
    joined = _joined(only_boilerplate)
    out = build_judge_evidence(joined, only_boilerplate, None, "distilled")
    assert out == joined


def test_fallback_still_includes_grounding_cards():
    # Even on a fallback to full, the retrieved cards must be appended.
    out = build_judge_evidence(_FULL_SYSTEM, None, "MATCHED CARD", "distilled")
    assert out.startswith(_FULL_SYSTEM)
    assert "## GROUNDING MATCHES" in out
    assert "MATCHED CARD" in out


# ── Escape-hatch mode resolution (PER-PROJECT, spec section 7) ─────────────

def _mock_session():
    """AsyncMock session whose begin_nested() mirrors the real AsyncSession:
    a SYNC call returning a NON-suppressing async context manager.

    A bare AsyncMock gets this wrong twice: ``begin_nested()`` would return a
    coroutine (no __aenter__ → TypeError inside the try, silently forcing the
    fallback path), and a mocked ``__aexit__`` would return a truthy AsyncMock
    (swallowing the in-block exception). Either way the code path under test
    silently changes.
    """
    from unittest.mock import AsyncMock, MagicMock

    db = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=cm)
    return db


def _patch_get_setting(monkeypatch, value=None, raise_exc=False):
    calls = {}

    async def _fake_get_setting(key, *, tenant_session=None, project_id=None, **kw):
        calls["key"] = key
        calls["project_id"] = project_id
        if raise_exc:
            raise RuntimeError("resolver down")
        return value

    import shared.config.resolver as resolver_mod
    monkeypatch.setattr(resolver_mod, "get_setting", _fake_get_setting)
    return calls


@pytest.mark.asyncio
async def test_resolve_mode_defaults_distilled(monkeypatch):
    import uuid
    from unittest.mock import AsyncMock

    calls = _patch_get_setting(monkeypatch, value=None)
    pid = uuid.uuid4()
    assert await _resolve_judge_context_mode(_mock_session(), pid) == "distilled"
    # resolved through the project-level setting for THIS project
    assert calls["key"] == "agent.judge_context_mode"
    assert calls["project_id"] == pid


@pytest.mark.asyncio
async def test_resolve_mode_full_when_project_sets_it(monkeypatch):
    import uuid
    from unittest.mock import AsyncMock

    _patch_get_setting(monkeypatch, value="full")
    assert await _resolve_judge_context_mode(_mock_session(), uuid.uuid4()) == "full"


@pytest.mark.asyncio
async def test_resolve_mode_unknown_value_is_distilled(monkeypatch):
    import uuid
    from unittest.mock import AsyncMock

    _patch_get_setting(monkeypatch, value="banana")
    assert await _resolve_judge_context_mode(_mock_session(), uuid.uuid4()) == "distilled"


@pytest.mark.asyncio
async def test_resolve_mode_runs_select_under_savepoint(monkeypatch):
    # Review R2 finding 1 / R3 finding 1: the settings SELECT must run inside
    # db.begin_nested() (SAVEPOINT). A DB failure would otherwise poison the
    # shared session (PendingRollbackError on the caller's later commit —
    # verdict discarded), while a full db.rollback() would expire every loaded
    # ORM instance (MissingGreenlet on the next lazy read — same discarded
    # verdict). The savepoint confines the failure to the SELECT alone.
    # NOTE (test escape): AsyncMock has no expiry/transaction semantics, so
    # this asserts the savepoint CONTRACT, not live-session behaviour — the
    # live guard is the deployed-session judge scenarios.
    import uuid
    from unittest.mock import AsyncMock

    _patch_get_setting(monkeypatch, value="distilled")
    db = _mock_session()
    await _resolve_judge_context_mode(db, uuid.uuid4())
    db.begin_nested.assert_called_once()
    db.rollback.assert_not_awaited()  # never a full-session rollback


@pytest.mark.asyncio
async def test_resolve_mode_resolver_failure_is_distilled_without_full_rollback(monkeypatch):
    # A broken resolver must never take the judge down — default distilled —
    # and must NOT issue a full-session rollback (see savepoint test above).
    import uuid
    from unittest.mock import AsyncMock

    _patch_get_setting(monkeypatch, raise_exc=True)
    db = _mock_session()
    assert await _resolve_judge_context_mode(db, uuid.uuid4()) == "distilled"
    db.begin_nested.assert_called_once()
    db.rollback.assert_not_awaited()


# ── run_judge end-to-end: distilled pack reaches the judge LLM ─────────────

@pytest.mark.asyncio
async def test_run_judge_distills_evidence_and_includes_cards(monkeypatch):
    import types
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch as _patch
    from src.judge.judge import run_judge

    captured = {}

    async def _fake_complete(system, user, on_thinking=None,
                             response_json=False, cache_system_prefix=False):
        captured["system"] = system
        captured["user"] = user
        captured["cache_system_prefix"] = cache_system_prefix
        return '{"verdict": "pass", "reasoning": "ok", "metrics": {}}'

    mock_adapter = MagicMock()
    mock_adapter.complete = AsyncMock(side_effect=_fake_complete)
    mock_adapter.last_usage = {"input_tokens": 1, "output_tokens": 1}
    llm_config = types.SimpleNamespace(provider="anthropic", model_name="claude")
    cfg = types.SimpleNamespace(project_id=uuid.uuid4(), judge_rubric_id=None)

    _patch_get_setting(monkeypatch, value=None)  # default distilled

    with (
        _patch("src.judge.judge.resolve_agent_llm_config",
               AsyncMock(return_value=llm_config)),
        _patch("src.judge.judge.build_adapter", return_value=mock_adapter),
    ):
        await run_judge(
            db=_mock_session(), cfg=cfg,
            system_prompt=_FULL_SYSTEM,
            user_message="what is ARR?",
            plan={"query": {}},
            answer_text="ARR is 100",
            sample_rows=[{"arr": 100}],
            result_row_count=1,
            system_sections=_SECTIONS,
            grounding_matches="ARR — annual recurring revenue (matched)",
        )

    user_text = captured["user"]
    # Evidential context kept.
    assert "## AVAILABLE MODELS" in user_text
    assert "base_amount" in user_text
    # Non-evidential boilerplate stripped.
    assert "12 worked examples" not in user_text
    assert "## OUTPUT FORMAT" not in user_text
    # Retrieved cards (intake fix) present.
    assert "## GROUNDING MATCHES" in user_text
    assert "annual recurring revenue" in user_text
    # Judge instructions marked cacheable (R1).
    assert captured["cache_system_prefix"] is True


@pytest.mark.asyncio
async def test_run_judge_full_mode_keeps_verbatim_prompt(monkeypatch):
    import types
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch as _patch
    from src.judge.judge import run_judge

    captured = {}

    async def _fake_complete(system, user, on_thinking=None,
                             response_json=False, cache_system_prefix=False):
        captured["user"] = user
        return '{"verdict": "pass", "reasoning": "ok", "metrics": {}}'

    mock_adapter = MagicMock()
    mock_adapter.complete = AsyncMock(side_effect=_fake_complete)
    mock_adapter.last_usage = {}
    llm_config = types.SimpleNamespace(provider="anthropic", model_name="claude")
    cfg = types.SimpleNamespace(project_id=uuid.uuid4(), judge_rubric_id=None)

    _patch_get_setting(monkeypatch, value="full")

    with (
        _patch("src.judge.judge.resolve_agent_llm_config",
               AsyncMock(return_value=llm_config)),
        _patch("src.judge.judge.build_adapter", return_value=mock_adapter),
    ):
        await run_judge(
            db=_mock_session(), cfg=cfg,
            system_prompt=_FULL_SYSTEM,
            user_message="q",
            plan=None,
            answer_text="a",
            sample_rows=[{"a": 1}],
            result_row_count=1,
            system_sections=_SECTIONS,
        )

    # Full mode keeps the boilerplate the distilled pack would strip.
    assert "## TASK" in captured["user"]
    assert "12 worked examples" in captured["user"]


# ── Integration fix: the per-turn DATE ANCHOR reaches the judge ────────────
#
# Test escape: Lane G moved CURRENT_DATE out of the cacheable planner system
# prefix into a per-turn ``## DATE ANCHOR`` block in the planner user suffix.
# The judge evidence pack is built ONLY from system_sections/system_prompt +
# grounding_matches, so before this fix the judge saw no current date at all and
# JUDGE_INSTRUCTIONS step 2 (date-range verification) was unanchored, in both
# judge_context_mode values. Guard: assert the date-anchor text renders in the
# judge user prompt (never in the cacheable JUDGE_INSTRUCTIONS prefix) in both
# distilled and full modes. Tier: T1 (producer/consumer contract — the judge's
# date evidence view).

_DATE_ANCHOR = (
    "CURRENT_DATE: 2026-07-21 (Tuesday). Resolve relative dates against this.\n"
    "- \"last month\" -> 2026-06-01 to 2026-06-30"
)


def test_build_judge_user_prompt_renders_date_anchor():
    from src.judge.judge import _build_judge_user_prompt

    out = _build_judge_user_prompt(
        rubric=None,
        user_message="revenue last month?",
        conversation_history=None,
        evidence="## AVAILABLE MODELS\nbase_amount",
        plan={"query": {}},
        sample_rows=[{"base_amount": 100}],
        total_rows=1,
        answer_text="100",
        date_anchor=_DATE_ANCHOR,
    )
    assert "## DATE ANCHOR" in out
    assert "CURRENT_DATE: 2026-07-21" in out
    assert "2026-06-01 to 2026-06-30" in out


def test_build_judge_user_prompt_omits_empty_date_anchor():
    from src.judge.judge import _build_judge_user_prompt

    out = _build_judge_user_prompt(
        rubric=None,
        user_message="q",
        conversation_history=None,
        evidence="ctx",
        plan=None,
        sample_rows=[{"a": 1}],
        total_rows=1,
        answer_text="a",
        date_anchor=None,
    )
    assert "## DATE ANCHOR" not in out


def test_judge_instructions_reference_anchor_without_a_current_date():
    # The DATE ANCHOR must live in the per-turn user prompt, NOT in
    # JUDGE_INSTRUCTIONS — that block is sent with cache_system_prefix=True and a
    # daily current date there would break the judge prompt cache. It should only
    # REFERENCE the DATE ANCHOR section, never embed a live "today". (The block's
    # worked EXAMPLES do contain illustrative dates like 2026-04-01; those are
    # fixed literals in static examples, not a per-turn current-date anchor, so
    # they are cache-stable — we assert only that no CURRENT_DATE anchor is
    # baked into the instructions.)
    from src.judge.judge import JUDGE_INSTRUCTIONS

    assert "DATE ANCHOR" in JUDGE_INSTRUCTIONS      # references the section
    assert "CURRENT_DATE:" not in JUDGE_INSTRUCTIONS  # no baked current-date


async def _run_judge_capture(monkeypatch, *, mode, date_anchor):
    import types
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch as _patch
    from src.judge.judge import run_judge

    captured = {}

    async def _fake_complete(system, user, on_thinking=None,
                             response_json=False, cache_system_prefix=False):
        captured["system"] = system
        captured["user"] = user
        return '{"verdict": "pass", "reasoning": "ok", "metrics": {}}'

    mock_adapter = MagicMock()
    mock_adapter.complete = AsyncMock(side_effect=_fake_complete)
    mock_adapter.last_usage = {}
    llm_config = types.SimpleNamespace(provider="anthropic", model_name="claude")
    cfg = types.SimpleNamespace(project_id=uuid.uuid4(), judge_rubric_id=None)

    _patch_get_setting(monkeypatch, value=(None if mode == "distilled" else "full"))

    with (
        _patch("src.judge.judge.resolve_agent_llm_config",
               AsyncMock(return_value=llm_config)),
        _patch("src.judge.judge.build_adapter", return_value=mock_adapter),
    ):
        await run_judge(
            db=_mock_session(), cfg=cfg,
            system_prompt=_FULL_SYSTEM,
            user_message="revenue last month?",
            plan={"query": {}},
            answer_text="100",
            sample_rows=[{"base_amount": 100}],
            result_row_count=1,
            system_sections=_SECTIONS,
            date_anchor=date_anchor,
        )
    return captured


@pytest.mark.asyncio
async def test_run_judge_threads_date_anchor_distilled(monkeypatch):
    captured = await _run_judge_capture(
        monkeypatch, mode="distilled", date_anchor=_DATE_ANCHOR
    )
    assert "## DATE ANCHOR" in captured["user"]
    assert "CURRENT_DATE: 2026-07-21" in captured["user"]
    # The date never enters the cacheable system prefix.
    assert "2026-07-21" not in captured["system"]


@pytest.mark.asyncio
async def test_run_judge_threads_date_anchor_full(monkeypatch):
    captured = await _run_judge_capture(
        monkeypatch, mode="full", date_anchor=_DATE_ANCHOR
    )
    assert "## DATE ANCHOR" in captured["user"]
    assert "CURRENT_DATE: 2026-07-21" in captured["user"]
    assert "2026-07-21" not in captured["system"]
