"""Bug-8779 — the miss-reason taxonomy must cover the producers' whole vocabulary.

``shared/miss_reason_taxonomy.py`` calls itself "the single place that says what
each of [AggregateSkipReason's] values IMPLIES", and the optimizer trusts that:
``find_miss_candidates`` and the LLM telemetry collector both decide whether a
miss is evidence to BUILD an aggregate purely from that classification.

But an unrecognised code classifies as ``BUILD`` — a deliberate fail-OPEN so a
future code degrades to the pre-Bug-8071 behaviour instead of vanishing from the
optimizer's view. The cost of that choice is that drift is SILENT: a skip reason
added to a matcher and never added to the table does not raise, it just starts
counting as evidence to spend a CTAS.

That is not hypothetical. ``join_population_mismatch`` was added to BOTH matchers
(Bug-8664 aggregate, Bug-8580 pocket) long after the table was written, and sat
unclassified: nobody had ever DECIDED what it implies for remediation, it merely
inherited the fail-open default. Its correct class turned out to be that same
BUILD — the population proof is over the CANDIDATE's plan, so an aggregate built
at the query's own grain can be provable where the refused broader one was not —
but that was luck, not a decision, and the first attempt at deciding it got the
answer wrong in the starve-the-user direction. Bug-8779.

SCOPE LIMIT (be honest about what this cannot see). ``QueryMissLog.miss_reason``
has TWO producers, and this guard enumerates only one. ``api/routes.py`` stores
the structured ``aggregate_skip:<code>,<code>`` join when the matcher ran and
refused candidates; when it did NOT (``force_route=source``), or when the matcher
RETURNED a candidate that a later gate then rejected (the percentile-exactness
gate, ``AggregateRewriteUnsupported``, validation rejection), it stores
``decision.reason`` verbatim — an English sentence that ``split_reason_codes``
comma-splits into pseudo-codes and the fail-open default counts as BUILD. Those
producers emit prose, not a closed vocabulary, so no enumeration guard can cover
them; the fix belongs at the producer (a structured code) and is tracked as
Bug-8788. Do NOT extend this guard to pattern-match sentences.

This test is the guard that makes the next such addition fail loudly at the
producer's own suite, instead of silently at a customer's storage bill. It is
deliberately an ENUMERATION over the producer classes rather than a hand-listed
set of codes, so it cannot itself go stale (the CLAUDE.md coverage-tool
blind-spot rule: a guard that proves "property X holds for every occurrence"
must discover the occurrences, not be told them).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from shared.miss_reason_taxonomy import (
    BUILD,
    INELIGIBLE,
    REPAIR,
    classified_reason_codes,
    classify_reason,
    coverage_refuting_reason_codes,
    reason_counts_refute_broader_coverage,
    reason_refutes_broader_coverage,
)
from src.routing.aggregate_matcher import AggregateSkipReason
from src.routing.pocket_matcher import PocketSkipReason

pytestmark = pytest.mark.unit


def _declared_codes(cls) -> dict[str, str]:
    """Every public string constant on a skip-reason class, INCLUDING inherited.

    ``vars(cls)`` returns only the class's OWN ``__dict__``. Extracting a few
    shared tokens into a base class (plausible — both matchers already share
    ``join_population_mismatch`` deliberately) would make those codes invisible
    here while the ``>= 15`` canary below still passed, so the guard would go
    partially blind without failing. Walk the MRO instead; subclass values win.
    """
    codes: dict[str, str] = {}
    for klass in reversed(inspect.getmro(cls)):
        if klass is object:
            continue
        for name, value in vars(klass).items():
            if not name.startswith("_") and isinstance(value, str):
                codes[name] = value
    return codes


def test_the_discovery_mechanism_sees_inherited_constants():
    """Guard the guard, part 1. ``vars(cls)`` returns only the class's OWN
    ``__dict__``, so hoisting shared tokens into a base class would hide them
    from every parity assertion below while the size canary still passed —
    a silent partial blindness in the very tool that exists to prevent silent
    drift (CLAUDE.md coverage-tool blind-spot audit). Pin the MRO walk directly
    rather than waiting for a producer refactor to expose it."""
    class _Base:
        ALPHA = "alpha"
        BETA = "beta"

    class _Child(_Base):
        GAMMA = "gamma"
        BETA = "beta_overridden"

    found = _declared_codes(_Child)
    assert found["ALPHA"] == "alpha", "inherited constant was not discovered"
    assert found["GAMMA"] == "gamma"
    # Subclass wins on override, matching normal attribute lookup.
    assert found["BETA"] == "beta_overridden"


def test_the_producer_classes_actually_declare_codes():
    """Guard the guard: if the enumeration mechanism ever stops discovering the
    constants (a refactor to an Enum, a rename), the parity tests below would
    pass vacuously. Fail closed on an empty discovery instead."""
    assert len(_declared_codes(AggregateSkipReason)) >= 15
    assert len(_declared_codes(PocketSkipReason)) >= 5


def test_every_aggregate_skip_reason_is_explicitly_classified():
    """``AggregateSkipReason`` values are what the router joins into
    ``QueryMissLog.miss_reason`` as ``aggregate_skip:<code>,<code>``, so every
    one of them reaches the optimizer's remediation decision."""
    classified = classified_reason_codes()
    unclassified = sorted(
        value
        for value in _declared_codes(AggregateSkipReason).values()
        if value not in classified
    )
    assert not unclassified, (
        "AggregateSkipReason value(s) missing from "
        "shared/miss_reason_taxonomy.py: "
        f"{unclassified}. An unclassified code falls through to the fail-open "
        "BUILD default, so the optimizer will treat it as evidence to build an "
        "aggregate. Decide the remediation class deliberately and add it to "
        "_REASON_CLASS (Bug-8779)."
    )


def test_tokens_shared_with_the_pocket_matcher_carry_one_classification():
    """Pocket skip reasons do NOT reach ``QueryMissLog.miss_reason`` on their own
    — ``decision.pocket_skipped_reason`` is surfaced only in /explain router_data
    and the QueryLog ``route_detail``, while ``miss_reason`` is built from
    ``decision.aggregate_skipped_reasons`` (query-router/src/api/routes.py).

    So the invariant is not "classify every pocket reason", it is: any token the
    two matchers SHARE — deliberately, so /explain reports one cause for one
    defect class across both routes — must carry one agreed classification. This
    is derived by intersection rather than by an allowlist, so it cannot go
    stale as either vocabulary grows."""
    shared = (
        set(_declared_codes(AggregateSkipReason).values())
        & set(_declared_codes(PocketSkipReason).values())
    )
    # The shared token that motivated this guard (Bug-8664 / Bug-8580).
    assert PocketSkipReason.JOIN_POPULATION_MISMATCH in shared
    assert PocketSkipReason.JOIN_POPULATION_MISMATCH == (
        AggregateSkipReason.JOIN_POPULATION_MISMATCH
    )

    classified = classified_reason_codes()
    unclassified = sorted(token for token in shared if token not in classified)
    assert not unclassified, (
        f"Token(s) emitted by BOTH matchers but unclassified: {unclassified}. "
        "The aggregate matcher's copy reaches miss_reason, so it reaches the "
        "optimizer's build decision and must not inherit the fail-open BUILD "
        "default (Bug-8779)."
    )
    # BUILD, and deliberately so: the population proof is over the CANDIDATE's
    # own plan, so an aggregate built at the query's grain can be provable where
    # the refused (broader) one was not. What matters for THIS guard is that the
    # class was DECIDED, not that it differs from the fail-open default.
    assert classify_reason(PocketSkipReason.JOIN_POPULATION_MISMATCH) == BUILD


def test_no_classified_code_is_orphaned_from_the_producers():
    """The reverse direction: a code classified here but emitted by nobody is
    dead vocabulary that makes the table look more complete than it is. One
    legacy exception is kept deliberately."""
    producer_codes = (
        set(_declared_codes(AggregateSkipReason).values())
        | set(_declared_codes(PocketSkipReason).values())
    )
    # ``no_aggregate`` predates AggregateSkipReason and is kept as a defensive
    # alias for historical rows written before the enum existed.
    legacy_allowed = {"no_aggregate"}
    orphans = sorted(
        code
        for code in classified_reason_codes()
        if code not in producer_codes and code not in legacy_allowed
    )
    assert not orphans, (
        f"Classified reason code(s) no producer emits: {orphans}. Either the "
        "producer renamed them (fix the table) or they are dead (remove them)."
    )

# ---------------------------------------------------------------------------
# Bug-8792 — the SECOND implication a reason code carries.
# ---------------------------------------------------------------------------
#
# A code's remediation class is not the only thing the optimizer reads from it.
# ``_COVERAGE_REFUTING_REASONS`` says whether the code's presence disproves the
# grain-subset coverage test in ``miss_analyzer._is_served``. That set is
# vocabulary-dependent in exactly the way ``_REASON_CLASS`` is, so it needs the
# same parity guard: a producer rename would otherwise leave a set of dead
# strings, silently restoring the population-blind coverage gate with every
# other test in this file still green (the CLAUDE.md coverage-tool blind-spot
# rule — the verification tool's own enumeration is a first-class review target).


def test_every_coverage_refuting_code_is_an_aggregate_producer_code():
    """Tightened for Bug-8792 finding 6. QueryMissLog.miss_reason is composed
    ONLY from decision.aggregate_skipped_reasons (query-router api/routes.py),
    so a pocket-only code could never fire the coverage refutation. Union-ing
    PocketSkipReason made the guard accept a dead code."""
    producer_codes = set(_declared_codes(AggregateSkipReason).values())
    orphans = sorted(
        c for c in coverage_refuting_reason_codes() if c not in producer_codes
    )
    assert not orphans, (
        f"Coverage-refuting code(s) the AGGREGATE matcher never emits: {orphans}"
    )


def test_every_coverage_refuting_code_is_classified_build():
    """A code that says 'a covering artifact was refused' but does not classify
    as BUILD is incoherent: the refutation would let a candidate past the
    coverage gate that the class split had already zeroed out."""
    for code in coverage_refuting_reason_codes():
        assert classify_reason(code) == BUILD, (
            f"{code} refutes coverage but classifies {classify_reason(code)!r}"
        )


def test_the_coverage_refuting_set_is_not_empty():
    """Guard the guard: an emptied set makes every assertion above vacuous while
    quietly reverting the fix."""
    assert coverage_refuting_reason_codes(), (
        "no reason code refutes grain coverage — Bug-8792's fix is inert"
    )
    assert AggregateSkipReason.JOIN_POPULATION_MISMATCH in (
        coverage_refuting_reason_codes()
    )


def test_a_coverage_refuting_code_is_recognised_inside_a_joined_reason():
    """The router stores ``aggregate_skip:<code>,<code>``, so the predicate must
    split before testing membership — an equality test would miss every miss row
    that had more than one refused candidate."""
    assert reason_refutes_broader_coverage(
        "aggregate_skip:stale,join_population_mismatch"
    )
    assert not reason_refutes_broader_coverage("aggregate_skip:stale,grain_missing")
    assert not reason_refutes_broader_coverage(None)


def test_an_evicted_history_entry_cannot_refute_coverage():
    """``_merge_miss_reason`` folds evicted entries into a ``class_totals``
    summary that no longer carries the reason code. The predicate must fail SAFE
    there — degrade to the ordinary coverage test rather than infer a refusal
    from a class total, which would spend a CTAS on evidence it cannot see."""
    evicted_only = [
        {"class_totals": {BUILD: 400, REPAIR: 0, INELIGIBLE: 0},
         "occurrence_count": 400},
    ]
    assert not reason_counts_refute_broader_coverage(evicted_only, None)
    # ... but a still-tracked entry alongside it does refute.
    assert reason_counts_refute_broader_coverage(
        evicted_only + [
            {"reason": "aggregate_skip:join_population_mismatch",
             "occurrence_count": 3},
        ],
        None,
    )


def test_a_pre_history_row_falls_back_to_its_current_reason():
    """Rows written before ``miss_reason_counts_json`` existed carry no history;
    the row's current ``miss_reason`` is all there is."""
    assert reason_counts_refute_broader_coverage(
        None, "aggregate_skip:join_population_mismatch",
    )
    assert not reason_counts_refute_broader_coverage(None, "aggregate_skip:stale")


def test_a_zero_count_history_entry_does_not_refute():
    """An entry with no occurrences is not evidence of anything."""
    assert not reason_counts_refute_broader_coverage(
        [{"reason": "aggregate_skip:join_population_mismatch",
          "occurrence_count": 0}],
        None,
    )


def _dated_refusal(age_days: int) -> list[dict]:
    seen = datetime.now(timezone.utc) - timedelta(days=age_days)
    return [{
        "reason": "aggregate_skip:join_population_mismatch",
        "occurrence_count": 400,
        "first_seen_at": (seen - timedelta(days=1)).isoformat(),
        "last_seen_at": seen.isoformat(),
    }]


def test_a_refusal_older_than_the_window_does_not_refute():
    """Bug-8792 round-1 finding 2. ``occurrence_count`` is a LIFETIME build
    total, so a pattern can clear the optimizer's weekly-RATE gate on months-old
    population refusals while every miss this week is a REPAIR miss against a
    broader artifact that serves fine once refreshed. Refuting that artifact
    buys a redundant CTAS and its refresh cadence forever, so the evidence has
    to be inside the same window the caller gates recency on."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    assert not reason_counts_refute_broader_coverage(
        _dated_refusal(190), None, since=week_ago,
    )
    assert reason_counts_refute_broader_coverage(
        _dated_refusal(2), None, since=week_ago,
    )
    # Without a window the caller opts out of the recency test entirely.
    assert reason_counts_refute_broader_coverage(_dated_refusal(190), None)


def test_an_undated_entry_cannot_prove_its_own_recency():
    """``_merge_miss_reason`` stamps ``last_seen_at`` on every tracked entry, so
    an undated one is corrupt or hand-written. It cannot demonstrate it is
    current, and the conservative reading is the one that does not spend
    storage."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    undated = [{"reason": "aggregate_skip:join_population_mismatch",
                "occurrence_count": 400}]
    assert not reason_counts_refute_broader_coverage(undated, None, since=week_ago)
    assert not reason_counts_refute_broader_coverage(
        [{"reason": "aggregate_skip:join_population_mismatch",
          "occurrence_count": 400, "last_seen_at": "not-a-timestamp"}],
        None, since=week_ago,
    )
    # The row's CURRENT reason is its most recent event by construction, so the
    # fallback leg is deliberately never windowed.
    assert reason_counts_refute_broader_coverage(
        undated, "aggregate_skip:join_population_mismatch", since=week_ago,
    )


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing():
    """Postgres JSONB round-trips whatever the writer put in. ``datetime.now(utc)
    .isoformat()`` is offset-aware, but a legacy or hand-repaired row can carry a
    naive string; comparing it to an aware ``since`` would raise TypeError inside
    the sweep."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    naive_recent = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    ).isoformat()
    assert reason_counts_refute_broader_coverage(
        [{"reason": "aggregate_skip:join_population_mismatch",
          "occurrence_count": 400, "last_seen_at": naive_recent}],
        None, since=week_ago,
    )


# Deliberately NOT tested: "every producer code classifies to one of the three
# classes". ``classify_reason`` returns a member of that set for EVERY string by
# construction (unknown -> BUILD), so such a test has no input that can fail it.
# The real invariant — that each code is EXPLICITLY classified rather than
# inheriting the default — is what the two assertions above check.
