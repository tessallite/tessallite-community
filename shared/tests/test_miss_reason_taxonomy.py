"""Bug-8071 — a miss reason must say what remediation it implies.

``QueryMissLog.miss_reason`` was write-only state: the router overwrote it on
every repeat and no optimizer consumer read it. So a query pattern that missed
400 times because its aggregate was STALE was indistinguishable from one that
missed 400 times because no aggregate covered its grain, and both counted
equally as evidence to build a new aggregate. Building a second aggregate for a
stale one duplicates storage and CTAS cost and fixes nothing.

This module is the single place that maps a reason code to a remediation class,
so the router (producer) and the optimizer (consumer) cannot disagree.
"""
from __future__ import annotations

import pytest

from shared.miss_reason_taxonomy import (
    BUILD,
    INELIGIBLE,
    REPAIR,
    classified_reason_codes,
    classify_reason,
    classify_reason_counts,
    split_reason_codes,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("code", [
    "grain_missing", "select_dim_missing", "measure_missing",
    "stat_type_mismatch", "exact_grain_mismatch", "no_measures_no_grain",
])
def test_coverage_gaps_classify_as_build(code):
    """Nothing covers this shape -> a new aggregate is the right remediation."""
    assert classify_reason(code) == BUILD


@pytest.mark.parametrize("code", [
    "stale", "stale_overdue", "freshness", "version_mismatch",
])
def test_unservable_artifact_classifies_as_repair(code):
    """An artifact EXISTS but could not serve. A second aggregate fixes nothing;
    the remediation is a refresh or a redeploy."""
    assert classify_reason(code) == REPAIR


@pytest.mark.parametrize("code", [
    "passthrough", "multi_table", "unresolvable_where", "unresolvable_order",
    "quantile_proof_failed", "time_period_non_additive",
    "variant_anchor_unproven", "period_variant_unproven",
    "population_unprovable_model",
])
def test_unacceleratable_queries_classify_as_ineligible(code):
    assert classify_reason(code) == INELIGIBLE


def test_population_unprovable_model_is_ineligible():
    """Bug-8789: a model-level fault (cyclic component / unresolvable anchor)
    means NO aggregate can serve — the optimizer must not attempt a build."""
    assert classify_reason("population_unprovable_model") == INELIGIBLE


def test_join_population_mismatch_is_build_evidence_a_narrower_aggregate_can_serve():
    """Bug-8779. Both matchers emit this when an artifact's materialised row
    population is not provably the query's own (Bug-8664 aggregate / Bug-8580
    pocket).

    The proof is over the CANDIDATE's plan, not the query's shape: ``needed``
    comes from the candidate's OWN grain and measures, and the dominant refusal
    is ``extra = plan - kept`` — relations the aggregate joined and the query did
    not. The matcher admits candidates whose grain is a SUPERSET of the query's,
    so an aggregate built at exactly the query's grain joins exactly the query's
    relations and IS provable (query-router
    test_bug_8664_aggregate_join_population.py::
    test_a_narrower_aggregate_at_the_query_grain_is_proven_where_the_broad_one_is_not).

    Classifying it INELIGIBLE removed the only build evidence for a shape a
    narrower aggregate would serve correctly — Bug-8466's failure class."""
    assert classify_reason("join_population_mismatch") == BUILD
    assert classify_reason("aggregate_skip:join_population_mismatch") == BUILD
    # It is nonetheless EXPLICITLY classified, not fail-open: the parity guard
    # is what makes the next new code a loud failure (Bug-8779).
    assert "join_population_mismatch" in classified_reason_codes()
    # Any BUILD code wins the join, so it stays BUILD beside a coverage gap...
    assert classify_reason(
        "aggregate_skip:join_population_mismatch,grain_missing"
    ) == BUILD
    # ...and beside a REPAIR code too, matching every other BUILD entry.
    assert classify_reason(
        "aggregate_skip:join_population_mismatch,stale"
    ) == BUILD


def test_every_classified_code_is_reachable_through_classify_reason():
    """``classified_reason_codes`` is what the producer/consumer parity guard
    enumerates, so it must not drift from the table it reports on."""
    codes = classified_reason_codes()
    assert "join_population_mismatch" in codes
    assert "population_plan_mismatch" in codes
    assert "population_unprovable_model" in codes
    assert "persona_scope" in codes          # conditional, but classified
    assert "some_future_reason" not in codes  # the fail-open default is not a class
    for code in codes:
        assert classify_reason(code) in {BUILD, REPAIR, INELIGIBLE}


def test_persona_scope_alone_is_build_evidence_not_ineligible():
    """Fable R1 finding 3. The matcher emits persona_scope when a candidate
    belongs to a DIFFERENT persona, not when the query cannot be accelerated.
    Miss rows are keyed per persona and the analyzer scans per persona, so a
    persona-scoped aggregate for THIS persona is a real remediation. Calling it
    INELIGIBLE would starve a second persona of acceleration permanently and
    silently."""
    assert classify_reason("persona_scope") == BUILD
    assert classify_reason("aggregate_skip:persona_scope") == BUILD


def test_persona_scope_defers_to_any_other_code_in_a_joined_reason():
    """Fable R2 finding 3. The stored reason flattens every candidate's skip
    code, losing candidate identity. In ``stale,persona_scope`` the STALE
    candidate must have been persona-COMPATIBLE — the persona gate runs before
    staleness is evaluated — so the remediation is to refresh it, not to build a
    second aggregate. persona_scope only decides when nothing else does."""
    assert classify_reason("aggregate_skip:stale,persona_scope") == REPAIR
    assert classify_reason("aggregate_skip:version_mismatch,persona_scope") == REPAIR
    assert classify_reason("aggregate_skip:passthrough,persona_scope") == INELIGIBLE
    # A genuine coverage gap alongside it still wins.
    assert classify_reason("aggregate_skip:grain_missing,persona_scope") == BUILD


def test_unknown_code_degrades_to_build_not_to_silence():
    """A future or misspelled code must behave as the system did before this
    split (every miss was build evidence), never vanish from the optimizer."""
    assert classify_reason("some_future_reason") == BUILD
    assert classify_reason(None) == BUILD
    assert classify_reason("") == BUILD


def test_joined_reason_is_split_and_build_wins():
    """The router joins several skip tokens into one string. A joined reason
    that contains ANY coverage gap is build evidence — the split must never be
    able to suppress a genuine gap."""
    assert split_reason_codes("aggregate_skip:stale,grain_missing") == [
        "stale", "grain_missing",
    ]
    assert classify_reason("aggregate_skip:stale,grain_missing") == BUILD
    # REPAIR beats INELIGIBLE: an existing-but-unservable artifact is actionable.
    assert classify_reason("aggregate_skip:stale,passthrough") == REPAIR
    # persona_scope is conditional and does not override REPAIR here — see
    # test_persona_scope_defers_to_any_other_code_in_a_joined_reason.
    assert classify_reason("aggregate_skip:passthrough,multi_table") == INELIGIBLE


def test_history_splits_occurrences_across_classes():
    history = [
        {"reason": "grain_missing", "occurrence_count": 400},
        {"reason": "stale", "occurrence_count": 3},
        {"reason": "passthrough", "occurrence_count": 2},
    ]
    totals = classify_reason_counts(history, "stale", 405)
    assert totals == {BUILD: 400, REPAIR: 3, INELIGIBLE: 2}


def test_missing_history_falls_back_to_the_current_reason():
    """A row written before the histogram column existed must behave exactly as
    it did before — the whole count attributed via its current miss_reason."""
    assert classify_reason_counts(None, "stale", 50) == {
        BUILD: 0, REPAIR: 50, INELIGIBLE: 0,
    }
    assert classify_reason_counts([], "grain_missing", 50) == {
        BUILD: 50, REPAIR: 0, INELIGIBLE: 0,
    }


def test_evicted_history_shortfall_is_not_lost():
    """The history is bounded, so a high-cardinality row can record fewer
    occurrences than its counter. The shortfall must be attributed, not dropped:
    a vanished occurrence is a silently under-counted coverage gap."""
    history = [{"reason": "stale", "occurrence_count": 10}]
    totals = classify_reason_counts(history, "grain_missing", 100)
    assert totals[REPAIR] == 10
    assert totals[BUILD] == 90
    assert sum(totals.values()) == 100


def test_evicted_class_summary_preserves_the_original_class_totals():
    history = [
        {
            "class_totals": {BUILD: 7, REPAIR: 11, INELIGIBLE: 3},
            "occurrence_count": 21,
        },
        {"reason": "grain_missing", "occurrence_count": 2},
    ]

    assert classify_reason_counts(history, "stale", 23) == {
        BUILD: 9, REPAIR: 11, INELIGIBLE: 3,
    }


def test_malformed_history_entries_are_ignored_not_fatal():
    history = [
        "not a dict",
        {"reason": "grain_missing", "occurrence_count": "seven"},
        {"reason": "grain_missing", "occurrence_count": 5},
        {"reason": "stale", "occurrence_count": -3},
    ]
    totals = classify_reason_counts(history, "grain_missing", 5)
    assert totals == {BUILD: 5, REPAIR: 0, INELIGIBLE: 0}


# ---------------------------------------------------------------------------
# F-009-11 / F-102-09 (Bug-8754) — deliberate source-fallback prose is
# INELIGIBLE (do-not-build), not fail-open BUILD evidence.
# ---------------------------------------------------------------------------


def test_force_route_source_prose_is_ineligible():
    """A force_route=source drill must not buy an aggregate. The exact prose the
    router emits (router.py force_route=source guard) classifies INELIGIBLE."""
    reason = "force_route=source set on request; aggregate + pocket matchers bypassed"
    assert classify_reason(reason) == INELIGIBLE


def test_disabled_model_prose_is_ineligible():
    """A disabled model / disabled aggregations is do-not-build: building an
    aggregate cannot serve a model whose routing is off."""
    assert classify_reason("Model is disabled; aggregate routing bypassed") == INELIGIBLE
    assert classify_reason("Aggregations are disabled for this model") == INELIGIBLE


def test_unknown_prose_still_fails_open_to_build():
    """F-009-16: the prose classification is deliberately NARROW — a genuinely
    unknown reason still fails open to BUILD (a new matcher code is caught by the
    parity test, not by inverting the default to fail-closed)."""
    assert classify_reason("some brand new reason nobody classified") == BUILD


def test_build_signal_still_wins_over_prose_in_a_join():
    """A joined reason with a real BUILD code is still BUILD — the prose leg only
    decides when it is the sole code."""
    assert classify_reason("aggregate_skip:grain_missing,stale") == BUILD
