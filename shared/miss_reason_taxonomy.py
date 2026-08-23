"""What a query-miss reason means for remediation (Bug-8071).

``QueryMissLog.miss_reason`` records WHY the router fell back to the source. The
optimizer's job is to decide what to DO about it — and those are not the same
question. Three reason classes call for three different actions:

``BUILD``
    No artifact could serve this shape: the grain, a SELECT dimension, a measure
    or a stat was absent. Building an aggregate is the correct remediation, and
    these occurrences are the evidence for it.

``REPAIR``
    An artifact for this shape EXISTS but was not servable at query time —
    stale, overdue, unbuilt, or bound to a superseded model version. Building a
    SECOND aggregate fixes nothing: it duplicates storage and CTAS cost while
    the original keeps failing. The remediation is a refresh or a redeploy.

``INELIGIBLE``
    The query itself cannot be accelerated by an aggregate in its current form
    (a passthrough, a multi-table shape, an unresolvable WHERE/ORDER BY, a
    non-additive time-period rollup, or an unproven quantile or period
    variant). No amount of building helps.

Before this split, the optimizer read no reason at all: every miss occurrence
counted equally toward "build an aggregate here", so a pattern that missed 400
times purely because its aggregate was stale looked exactly like a pattern with
no aggregate at all.

The vocabulary mirrors ``AggregateSkipReason`` in
``query-router/src/routing/aggregate_matcher.py``. That class is the producer;
this module is the single place that says what each of its values IMPLIES, so
the router and the optimizer cannot disagree.

Unknown reason codes classify as ``BUILD``: that is the historical behaviour
(every miss counted as build evidence), so an unrecognised or future code
degrades to what the system did before rather than silently dropping the
occurrence from the optimizer's view.

That default is deliberately fail-OPEN, which makes vocabulary drift silent:
a skip reason added to the producer and never added here does not raise, it
just starts counting as build evidence. Bug-8779 is exactly that — the
``join_population_mismatch`` code (Bug-8664/8580) was added to both matchers
long after this table and never classified, so nobody had ever DECIDED what it
implies. (Its correct class turned out to be the same ``BUILD`` the default
would have given it, but that was luck, not a decision — and the first attempt
at deciding it got the answer wrong in the starve-the-user direction.)
``classified_reason_codes()`` exists so a producer/consumer parity test can
enumerate the producer's vocabulary and fail when any value is not explicitly
classified here. Extend that test, not just this table, when a new reason code
lands.

A reason code carries a SECOND implication beyond its remediation class
(Bug-8792): whether its presence disproves the optimizer's grain-subset coverage
test. A code emitted only AFTER the matcher's grain-coverage gate is the router's
own record that a grain-covering artifact was evaluated and REFUSED, so counting
that artifact as coverage suppresses the very build the ``BUILD`` class exists to
produce. See ``_COVERAGE_REFUTING_REASONS``.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Remediation classes.
BUILD = "build"
REPAIR = "repair"
INELIGIBLE = "ineligible"

# Reason code -> remediation class. Keys are the ``AggregateSkipReason`` values.
_REASON_CLASS: dict[str, str] = {
    # --- BUILD: nothing covers this shape ---
    "grain_missing": BUILD,
    "select_dim_missing": BUILD,
    "measure_missing": BUILD,
    "stat_type_mismatch": BUILD,
    "exact_grain_mismatch": BUILD,
    "no_measures_no_grain": BUILD,
    "no_aggregate": BUILD,
    # Bug-8779 (classification corrected by deep review 2026-08-05). Emitted by
    # BOTH matchers (aggregate Bug-8664, pocket Bug-8580) when an artifact's
    # materialised ROW POPULATION is not provably the one the query's own
    # compiled plan would produce. It is EXPLICITLY BUILD — not left to the
    # fail-open default, and NOT ineligible.
    #
    # The proof is over THIS CANDIDATE'S plan, not over the query: ``needed`` is
    # derived from the candidate's own grain and measure columns
    # (``aggregate_population.aggregate_needed_table_ids``), and the dominant
    # refusal is ``extra = plan - kept`` — relations the AGGREGATE joined and the
    # query did not. The matcher admits candidates whose grain is a SUPERSET of
    # the query's, so a refused candidate is typically BROADER than the query and
    # its extra grain dimensions are what drag the lossy relation in. An
    # aggregate built at exactly the query's grain — a candidate's grain comes
    # from the miss row's ``requested_grain``, which IS that grain — joins
    # exactly the query's relations and is provable WHEN its measure set does not
    # drag in a further relation. Under ``include_all_measures`` (the default)
    # the built aggregate holds EVERY measure, so a measure sourced across a
    # lossy edge can still leave it unprovable — bounded waste, never a wrong
    # number. Executed proof of the grain half:
    # query-router/tests/test_bug_8664_aggregate_join_population.py::
    # test_a_narrower_aggregate_at_the_query_grain_is_proven_where_the_broad_one_is_not
    #
    # Only two of the seven refusal paths are terminal (a cyclic component,
    # Bug-8637; an unresolvable anchor). Classifying the whole token INELIGIBLE
    # optimises for that minority and permanently starves the majority — the
    # Bug-8466 failure class. It also mislabels a genuine coverage gap, because
    # this gate runs BEFORE the measure gates: an artifact that fails population
    # AND lacks the query's measure reports only this code. A build costs one
    # CTAS plus its refresh cadence until cap eviction, and can never serve wrong
    # numbers because the same population gate runs again at serve time.
    #
    # Bug-8792 (fixed): this code is ALSO coverage-refuting — see
    # ``_COVERAGE_REFUTING_REASONS`` below. Classifying it BUILD was necessary
    # but not sufficient; the deterministic sweep additionally had to stop
    # counting the artifact the router refused as coverage for the shape it
    # refused it for.
    #
    # Distinguishing the sub-causes needs a producer-side token split in the
    # matcher (a sensitive-component-guard file) — escalated as Bug-8789. The
    # coverage rule below is deliberately correct for BOTH halves of that split,
    # so it does not depend on Bug-8789 landing.
    "join_population_mismatch": BUILD,
    # Bug-8789: split from join_population_mismatch. Emitted when a
    # candidate COULD satisfy the query if rebuilt at a narrower grain
    # (the five fixable sub-causes: stale binding, kept ⊄ plan,
    # legacy many_to_one, non-forest join plan, extra not loss-free).
    "population_plan_mismatch": BUILD,
    # Bug-8789: split from join_population_mismatch. Emitted when NO
    # aggregate can satisfy the query because of a model-level fault
    # (cyclic join component or unresolvable anchor table).
    "population_unprovable_model": INELIGIBLE,
    # --- REPAIR: an artifact exists but could not serve ---
    "stale": REPAIR,
    "stale_overdue": REPAIR,
    "freshness": REPAIR,
    "version_mismatch": REPAIR,
    # --- INELIGIBLE: an aggregate cannot serve this query as written ---
    "passthrough": INELIGIBLE,
    "multi_table": INELIGIBLE,
    "unresolvable_where": INELIGIBLE,
    "unresolvable_order": INELIGIBLE,
    "quantile_proof_failed": INELIGIBLE,
    "time_period_non_additive": INELIGIBLE,
    "variant_anchor_unproven": INELIGIBLE,
    "period_variant_unproven": INELIGIBLE,
}

# ``persona_scope`` is classified CONDITIONALLY, which is why it is not in the
# table above.
#
# The producer (aggregate_matcher.py, the Phase 8.C.2 persona gate) emits it when
# a candidate belongs to a DIFFERENT persona — not when the query cannot be
# accelerated. Miss rows are keyed per persona and the analyzer scans per
# persona, so on its own it IS build evidence: a persona-scoped aggregate for
# THIS persona is the remediation. Treating it as INELIGIBLE would starve a
# second persona of acceleration permanently and silently (Fable R1 finding 3).
#
# But the stored reason is a FLATTENED join of every candidate's skip code, so
# candidate identity is lost. ``aggregate_skip:stale,persona_scope`` means one
# candidate was other-persona AND another was stale — and the stale one must
# have been persona-COMPATIBLE, because the persona gate runs before staleness
# is evaluated. The right remediation for that shape is to refresh the stale
# aggregate, not to build a second one (Fable R2 finding 3). So persona_scope
# only decides the class when no other code in the join says anything.
_CONDITIONAL_BUILD_REASONS = frozenset({"persona_scope"})

# Prefix the router uses when it joins several skip tokens into one reason
# string: ``aggregate_skip:grain_missing,stale``.
_AGGREGATE_SKIP_PREFIX = "aggregate_skip:"


# ---------------------------------------------------------------------------
# F-009-11 / F-102-09 (Bug-8754) — prose refusals that are DELIBERATE, not gaps.
# ---------------------------------------------------------------------------
#
# The router falls back to source for reasons that are a deliberate choice, not
# "no artifact covers this shape": a ``force_route=source`` drill, a disabled
# model, or a model with aggregations turned off. Building an aggregate never
# helps any of them and pollutes optimizer demand with intentional source
# traffic — a live drill could BUY a table the user explicitly asked not to use.
#
# These are free-text ``RouteDecision.reason`` strings, not ``AggregateSkipReason``
# codes, so they never appear in ``_REASON_CLASS`` and used to hit the fail-open
# ``BUILD`` default. Classify the KNOWN prose tokens as ``INELIGIBLE``. This is
# prose-only and deliberately narrow: a genuinely NEW matcher CODE still fails
# open to BUILD and is caught by the ``classified_reason_codes()`` parity test
# (F-009-16) — inverting that to fail-closed would need a migration for old rows.
#
# The router emits these at:
#   - router.py force_route=source guard  -> "force_route=source ..." prefix
#   - router.py model-disabled guard       -> "Model is disabled; ..."
#   - router.py aggregations-disabled guard -> "Aggregations are disabled ..."
_PROSE_INELIGIBLE_PREFIXES = (
    "force_route=source",
)
_PROSE_INELIGIBLE_REASONS = frozenset({
    "Model is disabled; aggregate routing bypassed",
    "Aggregations are disabled for this model",
})


def _prose_is_ineligible(code: str) -> bool:
    """True for a known deliberate-source-fallback prose reason (F-009-11)."""
    if not code:
        return False
    if code in _PROSE_INELIGIBLE_REASONS:
        return True
    return any(code.startswith(prefix) for prefix in _PROSE_INELIGIBLE_PREFIXES)


# ---------------------------------------------------------------------------
# Bug-8792 — codes that REFUTE the optimizer's grain-subset coverage test.
# ---------------------------------------------------------------------------
#
# The optimizer's coverage gate (``miss_analyzer._is_served``) asks "is an
# existing artifact's grain a superset of this candidate's?" and treats yes as
# "already served". That conflates two different questions:
#
#   (1) does an artifact exist that is BROAD ENOUGH to roll up to this grain
#   (2) does an artifact exist that the router will actually SERVE this shape from
#
# For most reason codes (1) is a good proxy for (2). For the codes below it is
# provably WRONG, because the code can only be emitted by a candidate that has
# ALREADY passed the matcher's grain-coverage gate. Its presence on a miss row is
# therefore the router's own record that an artifact covering this grain was
# evaluated and REFUSED — so the very artifact the coverage gate is about to
# count is the one that did not serve.
#
# Admission criteria for this set (all three, or it does not belong here):
#
#   A. The producer emits the code only AFTER the grain-coverage gate passes, so
#      the refused artifact's grain necessarily covers ``requested_grain``.
#   B. An artifact built at EXACTLY the query's own grain can be servable where
#      the refused broader one was not — with executed proof, not reasoning.
#   C. Building at exactly the query's grain CONVERGES: the new artifact is an
#      equal-grain artifact next sweep, which this rule still counts as coverage,
#      so at most one build per (model, persona, grain) can ever result — even
#      when the refusal turns out to be terminal and the new artifact is refused
#      too. Without (C) a code here would produce a daily CTAS loop.
#
# ``join_population_mismatch`` satisfies all three: (A) gate order in
# ``aggregate_matcher.find_best_aggregate`` is persona -> version -> grain
# coverage -> select-dim coverage -> population proof; (B)
# ``query-router/tests/test_bug_8664_aggregate_join_population.py::
# test_a_narrower_aggregate_at_the_query_grain_is_proven_where_the_broad_one_is_not``;
# (C) ``create_aggregate`` persists the candidate's grain verbatim.
#
# It is deliberately the ONLY member. Other post-grain-gate codes
# (``select_dim_missing``, ``exact_grain_mismatch``, the quantile/period proofs)
# may or may not satisfy (B) — none has an executed proof today, and a code
# admitted here without one buys storage with no evidence it buys acceleration.
#
# The evidence must be RECENT, and the analyzer's own recency gate does not
# supply that. An earlier version of this rule assumed it did — the argument was
# "the analyzer only reaches the coverage gate for a pattern still missing this
# week, and a shape still missing this week is not being served by anything".
# Round-1 review falsified it with an executed counter-example: the recency gate
# bounds when the ROW last missed, while ``occurrence_count`` is a LIFETIME
# BUILD total, so a pattern can clear the weekly-RATE branch on months-old
# population refusals while every miss this week is a REPAIR miss against a
# broader artifact that will serve correctly the moment it refreshes. Refuting
# that artifact spends a CTAS plus its refresh cadence forever on evidence the
# model has outgrown. Hence ``since`` below: a refusal only refutes coverage
# while it is inside the same window the recency gate applies to the row.
_COVERAGE_REFUTING_REASONS = frozenset({
    "join_population_mismatch",
    "population_plan_mismatch",
})


def classified_reason_codes() -> frozenset[str]:
    """Every reason code this module classifies EXPLICITLY (Bug-8779).

    Excludes the unknown-code fallback. A parity test enumerates the producers'
    skip-reason vocabularies and asserts each value appears here, so a new code
    cannot silently inherit the fail-open ``BUILD`` default.
    """
    return frozenset(_REASON_CLASS) | _CONDITIONAL_BUILD_REASONS


def coverage_refuting_reason_codes() -> frozenset[str]:
    """Codes whose presence disproves a grain-superset coverage claim (Bug-8792).

    See ``_COVERAGE_REFUTING_REASONS`` for the admission criteria. Exposed so a
    producer/consumer parity test can assert every member is a code some matcher
    actually emits — a renamed producer token would otherwise silently disable
    the coverage rule with every test still green.

    Bug-8789: ``population_plan_mismatch`` is included — it satisfies all three
    admission criteria (the same analysis that admitted
    ``join_population_mismatch`` applies to the narrower-buildable subset of it).
    ``population_unprovable_model`` is intentionally EXCLUDED: criteria (B)
    requires an executed proof that building at the query's grain converges, and
    a model-level fault (cyclic component / unresolvable anchor) cannot be
    repaired by building a new aggregate — it would still fail the same gate.
    """
    return _COVERAGE_REFUTING_REASONS


def reason_refutes_broader_coverage(miss_reason: str | None) -> bool:
    """True when one stored ``miss_reason`` string carries a coverage-refuting code.

    The router joins several candidates' skip codes into one string, so this is a
    membership test over the split codes, not an equality test.
    """
    return any(
        code in _COVERAGE_REFUTING_REASONS
        for code in split_reason_codes(miss_reason)
    )


def reason_counts_refute_broader_coverage(
    reason_counts: list[dict] | None,
    fallback_reason: str | None,
    *,
    since: datetime | None = None,
) -> bool:
    """True when a miss row RECORDED a coverage-refuting refusal (Bug-8792).

    Reads the same two inputs as ``classify_reason_counts`` — the bounded
    per-reason history and the row's current ``miss_reason`` fallback — because
    the current reason is only the LAST event (the whole reason Bug-8071 added
    the history), and a shape can miss for a population refusal one day and a
    different cause the next.

    ``since`` bounds how OLD a recorded refusal may be and still speak for the
    model as it is today. Callers should pass the same window they gate recency
    on. Without it a months-old refusal keeps discounting an artifact whose only
    problem today is staleness, and the optimizer spends a CTAS plus its refresh
    cadence forever on evidence the model has outgrown (round-1 review of this
    fix, executed counter-example). An entry with no usable ``last_seen_at``
    cannot prove its own recency, so it does not refute.

    The ``fallback_reason`` leg is deliberately NOT windowed: it is the row's
    current reason, i.e. its most recent event by construction. That makes it a
    PRECONDITION on the caller — it must not call this with a row it has not
    already established is recent, or the window it passed is bypassed through
    this leg. ``miss_analyzer.find_miss_candidates`` establishes it explicitly
    with a per-ROW ``last_seen >= week_ago`` test; its group-level recency gate
    is NOT sufficient, because that one runs on the group's maximum
    ``last_seen_at`` and a months-old row can ride into a group kept alive by a
    different fingerprint at the same grain (round-2 review, executed
    counter-example).

    Fails SAFE in the directions that matter:

    * An entry evicted into a ``class_totals`` summary has lost its reason code.
      That entry cannot refute anything, so the row degrades to the ordinary
      grain-subset coverage test — the pre-Bug-8792 behaviour, never a build the
      evidence does not support.
    * A row written before the history column existed has no entries at all, so
      the ``fallback_reason`` decides, exactly as ``classify_reason_counts`` does.

    Unlike ``classify_reason_counts`` the fallback is consulted even when the
    history is non-empty. That is deliberate: this is a PRESENCE test, not a
    count split, so there is no double-counting to avoid, and the writer's
    follow-up UPDATE that merges the current reason into the history can fail
    independently of the upsert that set ``miss_reason`` (query_logger.py) —
    losing the most recent refusal would be the starve-the-user direction.
    """
    if isinstance(reason_counts, list):
        for entry in reason_counts:
            if not isinstance(entry, dict) or "reason" not in entry:
                continue
            try:
                count = int(entry.get("occurrence_count") or 0)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            if not reason_refutes_broader_coverage(entry.get("reason")):
                continue
            if since is not None and not _entry_seen_since(entry, since):
                continue
            return True
    return reason_refutes_broader_coverage(fallback_reason)


def _entry_seen_since(entry: dict, since: datetime) -> bool:
    """Was this reason-history entry last observed at or after ``since``?

    ``_merge_miss_reason`` (query-router/src/logging/query_logger.py) writes an
    ISO ``last_seen_at`` on every tracked entry. An entry that lacks one, or
    whose value will not parse, cannot demonstrate its own recency and is
    treated as too old — the conservative direction, since the alternative is to
    spend storage on evidence of unknown age.
    """
    raw = entry.get("last_seen_at")
    if not raw:
        return False
    try:
        seen = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return seen >= since


def split_reason_codes(miss_reason: str | None) -> list[str]:
    """Split a stored ``miss_reason`` into its individual codes.

    The router stores either a single reason or the joined form
    ``aggregate_skip:<code>,<code>`` produced when several candidates were each
    skipped for a different cause.
    """
    if not miss_reason:
        return []
    text = miss_reason.strip()
    if text.startswith(_AGGREGATE_SKIP_PREFIX):
        text = text[len(_AGGREGATE_SKIP_PREFIX):]
    return [part.strip() for part in text.split(",") if part.strip()]


def classify_reason(miss_reason: str | None) -> str:
    """Classify one stored ``miss_reason`` string into a remediation class.

    A joined reason is classified by its STRONGEST build signal: if any code in
    it says "nothing covers this shape", the miss is build evidence. Only when
    every code is REPAIR or INELIGIBLE does the miss stop counting toward a new
    aggregate — so the split can never suppress a genuine coverage gap, which is
    the failure mode that would cost a user their acceleration.

    Between REPAIR and INELIGIBLE, REPAIR wins: an existing-but-unservable
    artifact is actionable, and reporting it is more useful than reporting that
    some other candidate was structurally ineligible.

    ``persona_scope`` is conditional — see ``_CONDITIONAL_BUILD_REASONS``. It
    decides the class only when nothing else in the join does.
    """
    codes = split_reason_codes(miss_reason)
    if not codes:
        return BUILD  # unknown / unrecorded -> historical behaviour
    classes: set[str] = set()
    for code in codes:
        if code in _CONDITIONAL_BUILD_REASONS:
            continue
        # F-009-11 / F-102-09: a deliberate source-fallback prose reason is
        # do-not-build, not build evidence. Checked before the fail-open default
        # so a force_route=source drill or a disabled model never counts as
        # demand for a new aggregate.
        if _prose_is_ineligible(code):
            classes.add(INELIGIBLE)
            continue
        classes.add(_REASON_CLASS.get(code, BUILD))
    if BUILD in classes:
        return BUILD
    if REPAIR in classes:
        return REPAIR
    if INELIGIBLE in classes:
        return INELIGIBLE
    # Only conditional codes (or nothing recognised) — persona_scope on its own
    # is build evidence, and an empty set degrades to the historical behaviour.
    return BUILD


def classify_reason_counts(
    reason_counts: list[dict] | None,
    fallback_reason: str | None,
    fallback_occurrences: int,
) -> dict[str, int]:
    """Split a miss row's occurrences across remediation classes.

    ``reason_counts`` is ``QueryMissLog.miss_reason_counts_json`` — the bounded
    per-reason history. When it is absent (a row last written before the column
    existed, or telemetry that failed to merge) the whole occurrence count is
    attributed using ``fallback_reason``, i.e. the row's current ``miss_reason``.
    That keeps a pre-upgrade row behaving exactly as it did before.

    Returns ``{BUILD: n, REPAIR: n, INELIGIBLE: n}`` summing to the row's
    occurrences (or to the recorded history's total when the two disagree —
    see below).
    """
    totals = {BUILD: 0, REPAIR: 0, INELIGIBLE: 0}
    if not reason_counts or not isinstance(reason_counts, list):
        totals[classify_reason(fallback_reason)] = max(0, int(fallback_occurrences or 0))
        return totals

    recorded = 0
    for entry in reason_counts:
        if not isinstance(entry, dict):
            continue
        class_totals = entry.get("class_totals")
        if isinstance(class_totals, dict):
            for reason_class in (BUILD, REPAIR, INELIGIBLE):
                try:
                    count = int(class_totals.get(reason_class) or 0)
                except (TypeError, ValueError):
                    continue
                if count > 0:
                    totals[reason_class] += count
                    recorded += count
            continue
        try:
            count = int(entry.get("occurrence_count") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        totals[classify_reason(entry.get("reason"))] += count
        recorded += count

    if recorded == 0:
        totals[classify_reason(fallback_reason)] = max(0, int(fallback_occurrences or 0))
        return totals

    # New writers preserve bounded evictions in a class-total summary. A
    # shortfall can still exist on rows written by the earlier implementation;
    # retain its compatibility fallback until those rows receive another event
    # and the producer folds the shortfall into the prior reason's class.
    shortfall = max(0, int(fallback_occurrences or 0) - recorded)
    if shortfall:
        totals[classify_reason(fallback_reason)] += shortfall
    return totals
