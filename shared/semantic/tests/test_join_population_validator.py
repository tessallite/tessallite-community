"""Join population classification + rollup (Bug-8615 governance phase G1).

Contract: ``docs/architecture/architecture_join-population-governance.md``.

What this file protects, in the order the contract states it:

* the DEFAULT causes zero behaviour change (the single most important
  invariant of this phase);
* a metadata gap never reads as ``neutral`` (invariant 1);
* absence of measurement is reported, never promoted to ``BLOCKED``;
* only measured, policy-relevant ``BLOCKED`` rows can refuse a deploy (G5);
* the probe SQL is dialect-neutral and goes through ``connector_qualify``.
"""
from __future__ import annotations

import uuid

import pytest

from shared.db.models import Join, JoinPopulationCheck
from shared.schemas.domains.aggregates_security import (
    DEFAULT_POPULATION_PARTICIPATION,
    POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
    POPULATION_PARTICIPATION_POPULATION_DEFINING,
    POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS,
    POPULATION_PARTICIPATION_UNDECLARED,
    POPULATION_PARTICIPATION_VALUES,
    JoinCreate,
    coerce_population_participation,
)
from shared.semantic.join_population_validator import (
    CLASSIFICATION_FILTERING,
    CLASSIFICATION_MULTIPLYING,
    CLASSIFICATION_NEUTRAL,
    CLASSIFICATIONS,
    DEFAULT_ROW_EFFECT_WARNING_THRESHOLD,
    REASON_MEASURED,
    REASON_MEASUREMENT_FAILED,
    REASON_ORIENTATION_AMBIGUOUS,
    REASON_UNIQUENESS_NOT_DECLARED,
    STATUS_BLOCKED,
    STATUS_OK,
    STATUS_WARNING,
    EdgeMeasurement,
    SideProbe,
    build_inner_join_count_sql,
    build_side_probe_sql,
    build_verdict,
    blocking_join_population_rows,
    classify_edge,
    join_status,
    preserved_sides,
    resolve_edge_effect_components,
    resolve_near_sides,
    roll_up_model_status,
    row_effect_ratio,
)

pytestmark = pytest.mark.unit


def _side(rows, key_non_null=None, key_distinct=None, matched=None) -> SideProbe:
    """A side probe defaulting to the healthy shape (no NULLs, all matched)."""
    return SideProbe(
        rows=rows,
        key_non_null=rows if key_non_null is None else key_non_null,
        key_distinct=rows if key_distinct is None else key_distinct,
        matched=rows if matched is None else matched,
    )


#: A textbook star edge: 100 fact rows, 10 dimension rows, full referential
#: integrity, unique dimension key. Nothing is lost and nothing fans out when
#: the fact is the retained side.
_CLEAN_STAR = EdgeMeasurement(
    left=_side(100, key_distinct=10),          # fact.dim_id, 10 distinct values
    right=_side(10),                           # dim.id, unique
    inner_join_rows=100,
)


# ---------------------------------------------------------------------------
# The default: zero behaviour change (the phase's central invariant)
# ---------------------------------------------------------------------------


def test_orm_default_is_preserve_base_rows():
    assert Join.__table__.c.population_participation.default.arg == (
        POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS
    )


def test_orm_server_default_is_preserve_base_rows():
    """A raw INSERT that omits the column (the snapshot rehydrate path builds
    its INSERT from the snapshot dict's keys, and a pre-G1 snapshot has no such
    key) must still land on the elidable default."""
    server_default = Join.__table__.c.population_participation.server_default
    assert server_default is not None
    assert POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS in server_default.arg.text


def test_orm_column_is_not_nullable():
    assert Join.__table__.c.population_participation.nullable is False


def test_create_api_default_is_preserve_base_rows():
    """A caller that does not know the field exists — every current frontend
    call until phase G2, and every existing script — creates the join it
    always created."""
    body = JoinCreate(
        left_table_id=uuid.uuid4(),
        right_table_id=uuid.uuid4(),
        left_column_name="a",
        right_column_name="b",
    )
    assert body.population_participation == (
        POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS
    )
    assert DEFAULT_POPULATION_PARTICIPATION == (
        POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS
    )


def test_the_default_never_reads_as_an_affirmative_declaration():
    """``preserve_base_rows`` is what every legacy join carries, so a
    non-neutral one must not be reported OK — but it must also never escalate
    to BLOCKED, whatever its magnitude, or phase G5 would block every model
    that predates this field without a modeller ever being asked."""
    for loss in (0.0, 0.005, 0.5, 0.83, 1.0):
        assert join_status(
            is_filtering=True, is_multiplying=False,
            population_participation=POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS,
            row_loss_ratio=loss, row_mult_ratio=0.0,
        ) == STATUS_WARNING


def test_a_clean_star_with_declared_keys_is_neutral_and_ok():
    """The whole point of the default being safe: a healthy model deploys OK
    with nobody touching the new field."""
    verdict = build_verdict(
        join_type="left",
        population_participation=DEFAULT_POPULATION_PARTICIPATION,
        measurement=_CLEAN_STAR,
        near_side="left",
        left_key_declared_unique=False,   # a fact key is never unique
        right_key_declared_unique=True,   # the dimension PK
    )
    assert verdict.classification == CLASSIFICATION_NEUTRAL
    assert verdict.status == STATUS_OK
    assert (verdict.row_loss_ratio, verdict.row_mult_ratio) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Vocabulary + coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", POPULATION_PARTICIPATION_VALUES)
def test_every_declared_value_round_trips_through_coercion(value):
    assert coerce_population_participation(value) == value


@pytest.mark.parametrize("value", ["banana", "", None, 7, "PRESERVE_BASE_ROWS  "])
def test_unknown_values_coerce_to_undeclared_not_to_the_default(value):
    """An unrecognised token means "we cannot tell what the modeller meant",
    which is exactly ``undeclared`` — folding it onto the default would let a
    tampered bundle read as an affirmative declaration. Both states are equally
    elidable, so the coercion never changes served numbers."""
    coerced = coerce_population_participation(value)
    if str(value or "").strip().lower() in POPULATION_PARTICIPATION_VALUES:
        assert coerced == str(value).strip().lower()
    else:
        assert coerced == POPULATION_PARTICIPATION_UNDECLARED


# ---------------------------------------------------------------------------
# preserved_sides — mirrors shared.semantic.join_keyword
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [
        ("inner", set()),
        ("left", {"left"}),
        ("LEFT OUTER", {"left"}),
        ("right", {"right"}),
        ("full", {"left", "right"}),
        ("full_outer", {"left", "right"}),
    ],
)
def test_preserved_sides_matches_the_orientation_contract(token, expected):
    assert set(preserved_sides(token)) == expected


@pytest.mark.parametrize("token", ["many_to_one", "outer", "banana", None, ""])
def test_a_legacy_or_unknown_token_provably_preserves_nothing(token):
    """``join_keyword`` renders these as an un-flipped LEFT JOIN onto whichever
    relation the traversal accumulated first, which is a property of the plan's
    base table and not of the join. Nothing is provably preserved."""
    assert preserved_sides(token) == frozenset()


def test_a_legacy_token_is_still_neutral_when_the_data_is_clean():
    """Conservative must not mean punitive: a legacy token over totally-covered
    data measures zero loss in BOTH directions, so it is honestly neutral."""
    verdict = build_verdict(
        join_type="many_to_one",
        population_participation=DEFAULT_POPULATION_PARTICIPATION,
        measurement=EdgeMeasurement(
            left=_side(10), right=_side(10), inner_join_rows=10,
        ),
        near_side=None,
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert verdict.classification == CLASSIFICATION_NEUTRAL
    assert verdict.status == STATUS_OK


def test_a_legacy_token_is_filtering_when_the_near_side_is_not_covered():
    """Same token, incomplete coverage: because nothing is provably preserved,
    the unmatched near rows count as loss."""
    verdict = build_verdict(
        join_type="many_to_one",
        population_participation=DEFAULT_POPULATION_PARTICIPATION,
        measurement=EdgeMeasurement(
            left=_side(100, matched=17), right=_side(10), inner_join_rows=17,
        ),
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=True,
    )
    assert verdict.classification == CLASSIFICATION_FILTERING
    assert verdict.row_loss_ratio == pytest.approx(0.83)


# ---------------------------------------------------------------------------
# Classification (invariant 1)
# ---------------------------------------------------------------------------


def test_an_inner_join_that_drops_rows_is_filtering():
    """The acme-demo ``modely`` shape: an INNER edge whose fact side has no
    matching dimension row for 83% of its rows."""
    classification, loss, mult, reason = classify_edge(
        join_type="inner",
        measurement=EdgeMeasurement(
            left=_side(100_000, key_distinct=20, matched=16_722),
            right=_side(20),
            inner_join_rows=16_722,
        ),
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=True,
    )
    assert classification == CLASSIFICATION_FILTERING
    assert loss == pytest.approx(0.83278)
    assert mult == 0.0
    assert reason == REASON_MEASURED


def test_a_left_join_does_not_lose_its_preserved_side():
    """Same unmatched data as above, declared LEFT: the near (left) side is
    preserved, so there is no loss at all."""
    classification, loss, _mult, _reason = classify_edge(
        join_type="left",
        measurement=EdgeMeasurement(
            left=_side(100_000, key_distinct=20, matched=16_722),
            right=_side(20),
            inner_join_rows=16_722,
        ),
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=True,
    )
    assert loss == 0.0
    assert classification == CLASSIFICATION_NEUTRAL


def test_a_non_unique_far_key_multiplies_the_retained_side():
    """200 join rows out of 100 retained rows = each retained row duplicated
    once."""
    classification, loss, mult, _reason = classify_edge(
        join_type="left",
        measurement=EdgeMeasurement(
            left=_side(100, key_distinct=10),
            right=_side(20, key_distinct=10),   # 2 rows per key
            inner_join_rows=200,
        ),
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=False,
    )
    assert classification == CLASSIFICATION_MULTIPLYING
    assert loss == 0.0
    assert mult == pytest.approx(1.0)


def test_a_full_join_that_adds_far_side_rows_is_not_neutral():
    """Bug-8658. A FULL join preserves BOTH sides, so nothing is lost — but the
    far side's unpartnered rows are EMITTED, with NULLs on the near side. The
    compiled plan therefore returns more rows than the plan that elides the far
    table, which is the same grand-total disagreement Bug-8615 is about. Only
    counting fan-out made this read as a clean ``neutral``.
    """
    classification, loss, mult, _reason = classify_edge(
        join_type="full",
        measurement=EdgeMeasurement(
            left=_side(100),                    # fact, all matched
            right=_side(50, matched=10),        # 40 dimension rows with no fact
            inner_join_rows=100,
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert loss == 0.0, "a FULL join drops nothing"
    assert mult == pytest.approx(0.4), "40 extra rows over a 100-row baseline"
    assert classification == CLASSIFICATION_MULTIPLYING


def test_a_right_join_entered_from_its_non_preserved_end_adds_rows():
    """Same defect class as the FULL case, and just as reachable: a modeller
    declares ``fact RIGHT JOIN dim``, so every dimension row survives whether or
    not it has a fact row."""
    classification, loss, mult, _reason = classify_edge(
        join_type="right",
        measurement=EdgeMeasurement(
            left=_side(100),
            right=_side(50, matched=10),
            inner_join_rows=100,
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    # The near (left) side is NOT preserved by a right join, but every one of
    # its rows happens to match here, so nothing is lost.
    assert loss == 0.0
    assert mult == pytest.approx(0.4)
    assert classification == CLASSIFICATION_MULTIPLYING


def test_a_left_join_does_not_count_far_side_only_rows():
    """Mutation partner for the two above. The SAME data under a LEFT join
    drops the unmatched dimension rows instead of emitting them, so the near
    population is untouched and the edge really is neutral. If the row-gain
    term were applied unconditionally this would go red."""
    classification, loss, mult, _reason = classify_edge(
        join_type="left",
        measurement=EdgeMeasurement(
            left=_side(100),
            right=_side(50, matched=10),
            inner_join_rows=100,
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert (loss, mult) == (0.0, 0.0)
    assert classification == CLASSIFICATION_NEUTRAL


def test_far_side_row_gain_is_counted_even_when_the_join_probe_was_skipped():
    """The third (join-cardinality) query is skipped when both keys are unique
    in the data. Row GAIN comes from the side probes, not that query, so
    skipping it must not hide the effect."""
    _classification, _loss, mult, _reason = classify_edge(
        join_type="full",
        measurement=EdgeMeasurement(
            left=_side(100), right=_side(50, matched=10), inner_join_rows=None,
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert mult == pytest.approx(0.4)


def test_an_empty_retained_table_with_far_side_rows_is_not_neutral():
    """Bug-8663. The retained table is empty and the far side still emits its
    unpartnered rows, so the compiled plan returns rows where the elided plan
    returns none — the largest row gain there is. The ratio is 0/0, and
    returning 0.0 for it read as a clean ``neutral``."""
    classification, loss, mult, _reason = classify_edge(
        join_type="full",
        measurement=EdgeMeasurement(
            left=_side(0), right=_side(40, matched=0), inner_join_rows=0,
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert loss == 0.0, "you cannot lose rows you do not have"
    assert mult == pytest.approx(1.0)
    assert classification == CLASSIFICATION_MULTIPLYING


def test_an_empty_retained_table_with_no_far_rows_is_still_neutral():
    """Mutation partner: two empty tables really are neutral, so the
    zero-baseline branch must not fire unconditionally."""
    classification, loss, mult, _reason = classify_edge(
        join_type="full",
        measurement=EdgeMeasurement(
            left=_side(0), right=_side(0), inner_join_rows=0,
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert (loss, mult) == (0.0, 0.0)
    assert classification == CLASSIFICATION_NEUTRAL


def test_the_row_effect_ratio_stays_a_finite_json_safe_number():
    """The zero-baseline case is reported as 1.0, not infinity: the ratio is
    persisted to a Float column and serialised into the health payload, and
    ``Infinity`` is not valid JSON."""
    import json
    import math

    _c, _l, mult, _r = classify_edge(
        join_type="right",
        measurement=EdgeMeasurement(
            left=_side(0), right=_side(5, matched=0), inner_join_rows=0,
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert math.isfinite(mult)
    assert json.loads(json.dumps({"row_effect_ratio": mult}))[
        "row_effect_ratio"
    ] == mult


def test_fan_out_and_far_side_gain_add_up():
    """Both sources of extra rows are real and independent; taking only the
    larger would under-report a join that does both."""
    _classification, _loss, mult, _reason = classify_edge(
        join_type="full",
        measurement=EdgeMeasurement(
            left=_side(100, key_distinct=50),
            right=_side(60, key_distinct=50, matched=50),
            inner_join_rows=120,   # 20 extra rows from fan-out
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    # (120 - 100) fan-out + (60 - 50) far-only = 30 extra over 100 rows.
    assert mult == pytest.approx(0.30)


def test_zero_effect_without_declared_keys_is_not_neutral():
    """Invariant 1: "a join cannot be honestly classified against a metadata
    gap". Unique in TODAY's data is not a constraint. This is Bug-8618's gap,
    and it must surface rather than be assumed away."""
    classification, loss, mult, reason = classify_edge(
        join_type="left",
        measurement=_CLEAN_STAR,
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=False,   # dim PK not introspected yet
    )
    assert classification != CLASSIFICATION_NEUTRAL
    assert reason == REASON_UNIQUENESS_NOT_DECLARED
    assert (loss, mult) == (0.0, 0.0)


def test_the_metadata_gap_warns_and_never_blocks():
    """Its measured effect is genuinely 0.0, so even an ``undeclared`` join
    with this shape stays a WARNING. Closing Bug-8618 turns it into OK; it must
    never turn a healthy model's deploy into a BLOCKED finding meanwhile."""
    verdict = build_verdict(
        join_type="left",
        population_participation=POPULATION_PARTICIPATION_UNDECLARED,
        measurement=_CLEAN_STAR,
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=False,
    )
    assert verdict.status == STATUS_WARNING


def test_only_the_far_key_uniqueness_matters_for_the_resolved_direction():
    """A fact key is never unique, and that is fine — it cannot multiply the
    fact population. Requiring BOTH keys to be declared unique would make every
    star schema on earth non-neutral."""
    classification, _loss, _mult, reason = classify_edge(
        join_type="left",
        measurement=_CLEAN_STAR,
        near_side="left",
        left_key_declared_unique=False,   # the fact side
        right_key_declared_unique=True,   # the dimension PK
    )
    assert classification == CLASSIFICATION_NEUTRAL
    assert reason == REASON_MEASURED


def test_an_ambiguous_orientation_requires_both_keys_declared():
    """With no resolvable near side both directions are live, so BOTH far keys
    must be constraint-backed. A 1:1 edge with zero measured effect either way
    isolates the metadata gap from any real row effect."""
    classification, loss, mult, reason = classify_edge(
        join_type="left",
        measurement=EdgeMeasurement(
            left=_side(10), right=_side(10), inner_join_rows=10,
        ),
        near_side=None,
        left_key_declared_unique=False,   # only one side introspected
        right_key_declared_unique=True,
    )
    assert (loss, mult) == (0.0, 0.0)
    assert classification != CLASSIFICATION_NEUTRAL
    assert reason == REASON_UNIQUENESS_NOT_DECLARED


def test_the_same_edge_is_neutral_once_both_keys_are_declared():
    """Mutation partner for the test above: the ONLY difference is the second
    declared key, so the non-neutral verdict there is genuinely caused by the
    metadata gap and not by the measurement."""
    classification, _loss, _mult, reason = classify_edge(
        join_type="left",
        measurement=EdgeMeasurement(
            left=_side(10), right=_side(10), inner_join_rows=10,
        ),
        near_side=None,
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert classification == CLASSIFICATION_NEUTRAL
    assert reason == REASON_ORIENTATION_AMBIGUOUS


def test_an_ambiguous_orientation_takes_the_worse_direction():
    """Left is fully covered; right is half covered. With no resolvable near
    side the worse (right-retained) direction wins, so the edge is filtering
    even though a fact-rooted plan entering from the left would lose nothing.

    Keys are unique on both sides, so nothing multiplies and the verdict
    isolates the LOSS half of the comparison.
    """
    classification, loss, mult, reason = classify_edge(
        join_type="inner",
        measurement=EdgeMeasurement(
            left=_side(100),                 # 100 distinct keys, all matched
            right=_side(200, matched=100),   # 200 distinct keys, half matched
            inner_join_rows=100,
        ),
        near_side=None,
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert mult == 0.0
    assert loss == pytest.approx(0.5)
    assert classification == CLASSIFICATION_FILTERING
    assert reason == REASON_ORIENTATION_AMBIGUOUS


def test_the_resolved_direction_ignores_the_other_side_s_loss():
    """Mutation partner: the same measurement with the near side resolved to
    the fully-covered left endpoint is neutral. This is what makes the
    orientation walk load-bearing rather than decorative."""
    classification, loss, _mult, reason = classify_edge(
        join_type="inner",
        measurement=EdgeMeasurement(
            left=_side(100),
            right=_side(200, matched=100),
            inner_join_rows=100,
        ),
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
    )
    assert loss == 0.0
    assert classification == CLASSIFICATION_NEUTRAL
    assert reason == REASON_MEASURED


def test_a_dimension_retained_plan_sees_the_fan_out_a_fact_plan_does_not():
    """The clean star measured from the dimension side multiplies 10x. The
    near/far walk is what keeps that out of the fact-rooted verdict."""
    ambiguous, _l, ambiguous_mult, _r = classify_edge(
        join_type="left", measurement=_CLEAN_STAR, near_side=None,
        left_key_declared_unique=True, right_key_declared_unique=True,
    )
    resolved, _l2, resolved_mult, _r2 = classify_edge(
        join_type="left", measurement=_CLEAN_STAR, near_side="left",
        left_key_declared_unique=True, right_key_declared_unique=True,
    )
    assert ambiguous == CLASSIFICATION_MULTIPLYING
    assert ambiguous_mult == pytest.approx(9.0)
    assert resolved == CLASSIFICATION_NEUTRAL
    assert resolved_mult == 0.0


def test_an_empty_far_table_loses_the_whole_near_population():
    _classification, loss, _mult, _reason = classify_edge(
        join_type="inner",
        measurement=EdgeMeasurement(
            left=_side(100, matched=0), right=_side(0), inner_join_rows=0,
        ),
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=True,
    )
    assert loss == pytest.approx(1.0)


def test_null_keys_count_as_unmatched():
    """A NULL key never matches in an equi-join, so it is real row loss on an
    INNER edge — not something the probe should quietly exclude."""
    _classification, loss, _mult, _reason = classify_edge(
        join_type="inner",
        measurement=EdgeMeasurement(
            left=SideProbe(rows=100, key_non_null=90, key_distinct=9, matched=90),
            right=_side(9),
            inner_join_rows=90,
        ),
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=True,
    )
    assert loss == pytest.approx(0.1)


@pytest.mark.parametrize(
    "join_type,near,l_unique,r_unique",
    [
        ("inner", "left", False, True),
        ("left", "right", True, False),
        ("full", None, True, True),
        ("many_to_one", None, False, False),
    ],
)
def test_classify_edge_only_ever_returns_declared_classifications(
    join_type, near, l_unique, r_unique,
):
    classification, _l, _m, _r = classify_edge(
        join_type=join_type,
        measurement=EdgeMeasurement(
            left=_side(30, key_distinct=6, matched=25),
            right=_side(12, key_distinct=6, matched=11),
            inner_join_rows=44,
        ),
        near_side=near,
        left_key_declared_unique=l_unique,
        right_key_declared_unique=r_unique,
    )
    assert classification in CLASSIFICATIONS


# ---------------------------------------------------------------------------
# Absence of measurement
# ---------------------------------------------------------------------------


def test_no_measurement_classifies_conservatively_but_warns_only():
    verdict = build_verdict(
        join_type="inner",
        population_participation=POPULATION_PARTICIPATION_UNDECLARED,
        measurement=None,
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
        unmeasured_reason=REASON_MEASUREMENT_FAILED,
    )
    assert verdict.classification == CLASSIFICATION_FILTERING  # non-neutral
    assert verdict.measured is False
    assert verdict.row_effect_ratio is None
    # An unreachable source must never become a deploy block once G5 lands.
    assert verdict.status == STATUS_WARNING
    assert verdict.reason == REASON_MEASUREMENT_FAILED


def test_unmeasured_effect_is_none_not_zero():
    """"No effect" and "no measurement" must be distinguishable by a consumer."""
    assert row_effect_ratio(None, None) is None
    assert row_effect_ratio(0.0, None) == 0.0
    assert row_effect_ratio(0.2, 0.5) == 0.5


# ---------------------------------------------------------------------------
# Status mapping (the G1 rules)
# ---------------------------------------------------------------------------


def test_a_neutral_join_is_ok_whatever_it_is_flagged():
    for participation in POPULATION_PARTICIPATION_VALUES:
        assert join_status(
            is_filtering=False, is_multiplying=False,
            population_participation=participation,
            row_loss_ratio=0.0, row_mult_ratio=0.0,
        ) == STATUS_OK


def test_population_defining_excuses_a_lossy_join():
    """An intentionally filtering join is a legitimate design, not a mistake —
    that is why option 2a alone was rejected. ``population_defining`` is never
    elided, so it excuses BOTH components."""
    assert join_status(
        is_filtering=True, is_multiplying=False,
        population_participation=POPULATION_PARTICIPATION_POPULATION_DEFINING,
        row_loss_ratio=0.83, row_mult_ratio=0.0,
    ) == STATUS_OK


def test_population_defining_excuses_a_join_that_both_filters_and_multiplies():
    assert join_status(
        is_filtering=True, is_multiplying=True,
        population_participation=POPULATION_PARTICIPATION_POPULATION_DEFINING,
        row_loss_ratio=0.83, row_mult_ratio=0.5,
    ) == STATUS_OK


def test_enrichment_only_excuses_a_purely_multiplying_join():
    """The contract's own definition: "any row multiplication it causes is
    accepted"."""
    assert join_status(
        is_filtering=False, is_multiplying=True,
        population_participation=POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
        row_loss_ratio=0.0, row_mult_ratio=0.83,
    ) == STATUS_OK


def test_enrichment_only_does_not_excuse_a_filtering_join():
    """Bug-8652. ``enrichment_only`` says nothing about excusing row loss —
    G1's rollup treated ANY non-neutral ``enrichment_only`` join as OK, which
    let a genuinely row-FILTERING join declared ``enrichment_only`` report a
    clean bill of health it never earned."""
    assert join_status(
        is_filtering=True, is_multiplying=False,
        population_participation=POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
        row_loss_ratio=0.83, row_mult_ratio=0.0,
    ) == STATUS_BLOCKED


def test_enrichment_only_still_blocks_on_the_filtering_component_alone():
    """Same shape, but the excused multiplying ratio is the LARGER of the two
    — proving the effect used for the threshold is the unexcused (filtering)
    ratio, not the combined max, or a huge excused multiplication ratio would
    wrongly drag an otherwise-fine declaration to BLOCKED, or a huge excused
    ratio could otherwise mask a real small filtering effect."""
    assert join_status(
        is_filtering=True, is_multiplying=True,
        population_participation=POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
        row_loss_ratio=0.83, row_mult_ratio=0.99,
    ) == STATUS_BLOCKED


def test_enrichment_only_with_an_unexcused_filtering_component_can_warn():
    """The unexcused filtering component is judged by the SAME threshold rule
    as ``undeclared`` — small enough stays a WARNING, not an automatic
    BLOCKED."""
    assert join_status(
        is_filtering=True, is_multiplying=True,
        population_participation=POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
        row_loss_ratio=0.001, row_mult_ratio=0.99,
    ) == STATUS_WARNING


def test_undeclared_above_the_threshold_is_blocked():
    assert join_status(
        is_filtering=True, is_multiplying=False,
        population_participation=POPULATION_PARTICIPATION_UNDECLARED,
        row_loss_ratio=0.83, row_mult_ratio=0.0,
    ) == STATUS_BLOCKED


def test_undeclared_at_or_below_the_threshold_is_only_a_warning():
    for effect in (0.0, 0.005, DEFAULT_ROW_EFFECT_WARNING_THRESHOLD):
        assert join_status(
            is_filtering=False, is_multiplying=True,
            population_participation=POPULATION_PARTICIPATION_UNDECLARED,
            row_loss_ratio=0.0, row_mult_ratio=effect,
        ) == STATUS_WARNING


def test_the_threshold_is_a_named_constant_at_one_percent():
    assert DEFAULT_ROW_EFFECT_WARNING_THRESHOLD == 0.01


def test_the_threshold_is_honoured_when_overridden():
    assert join_status(
        is_filtering=True, is_multiplying=False,
        population_participation=POPULATION_PARTICIPATION_UNDECLARED,
        row_loss_ratio=0.05, row_mult_ratio=0.0, threshold=0.10,
    ) == STATUS_WARNING


def test_an_out_of_enum_flag_is_treated_as_undeclared_by_the_status_rule():
    assert join_status(
        is_filtering=True, is_multiplying=False,
        population_participation="banana",
        row_loss_ratio=0.83, row_mult_ratio=0.0,
    ) == STATUS_BLOCKED


def test_an_out_of_enum_flag_does_not_excuse_multiplying_either():
    """"banana" coerces to ``undeclared``, which excuses neither component —
    unlike ``enrichment_only`` it must not wave through a purely multiplying
    join."""
    assert join_status(
        is_filtering=False, is_multiplying=True,
        population_participation="banana",
        row_loss_ratio=0.0, row_mult_ratio=0.83,
    ) == STATUS_BLOCKED


# ---------------------------------------------------------------------------
# resolve_edge_effect_components — the per-component fact recovery (Bug-8652)
# ---------------------------------------------------------------------------


def test_a_purely_filtering_edge_reads_as_filtering_only():
    assert resolve_edge_effect_components(
        row_loss_ratio=0.83, row_mult_ratio=0.0, reason=REASON_MEASURED,
    ) == (True, False)


def test_a_purely_multiplying_edge_reads_as_multiplying_only():
    assert resolve_edge_effect_components(
        row_loss_ratio=0.0, row_mult_ratio=0.4, reason=REASON_MEASURED,
    ) == (False, True)


def test_an_edge_that_both_filters_and_multiplies_reads_as_both():
    """The exact shape a single worst-of ``classification`` cannot represent:
    ``classify_edge`` would label this ``multiplying`` (mult >= loss), but the
    edge genuinely also filters."""
    assert resolve_edge_effect_components(
        row_loss_ratio=0.02, row_mult_ratio=0.05, reason=REASON_MEASURED,
    ) == (True, True)


def test_a_genuinely_neutral_edge_reads_as_neither():
    assert resolve_edge_effect_components(
        row_loss_ratio=0.0, row_mult_ratio=0.0, reason=REASON_MEASURED,
    ) == (False, False)


def test_an_unmeasured_edge_is_conservatively_filtering_only():
    """Mirrors ``classify_edge``'s own conservative choice for the same case,
    so an unmeasured edge is never excused by ``enrichment_only``."""
    assert resolve_edge_effect_components(
        row_loss_ratio=None, row_mult_ratio=None,
        reason=REASON_MEASUREMENT_FAILED,
    ) == (True, False)


def test_the_metadata_gap_reads_as_multiplying_only():
    """Invariant 1's uniqueness gap is purely a multiplication-side
    uncertainty — loss is independently, genuinely measured as 0.0."""
    assert resolve_edge_effect_components(
        row_loss_ratio=0.0, row_mult_ratio=0.0,
        reason=REASON_UNIQUENESS_NOT_DECLARED,
    ) == (False, True)


# ---------------------------------------------------------------------------
# End-to-end: the two resolve_edge_effect_components special cases, wired
# through build_verdict (not just unit-tested against join_status directly),
# crossed with the two participation states that excuse unconditionally.
# Deep-review (Bug-8652 fix review, 2026-08-04): join_status-level tests cover
# the special cases' effect on the status rule, but nothing previously drove
# them through classify_edge -> resolve_edge_effect_components -> join_status
# as one pipeline for population_defining/enrichment_only, which is exactly
# the seam a wiring mistake (wrong reason string, booleans not actually
# reaching join_status) would hide from every other test in this file.
# ---------------------------------------------------------------------------


def test_build_verdict_unmeasured_enrichment_only_is_warning_not_ok():
    """An unmeasured edge is conservatively filtering-only (see above), and
    ``enrichment_only`` never excuses filtering — so the unmeasured case must
    NOT read as a clean OK just because the join is declared enrichment_only.
    Ratios are null, so it lands on WARNING (property 5), never BLOCKED."""
    verdict = build_verdict(
        join_type="inner",
        population_participation=POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
        measurement=None,
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
        unmeasured_reason=REASON_MEASUREMENT_FAILED,
    )
    assert verdict.status == STATUS_WARNING


def test_build_verdict_unmeasured_population_defining_is_ok():
    """``population_defining`` is never elided at all, so an inability to
    measure its magnitude does not matter — unlike every other participation
    state, this one is OK even unmeasured."""
    verdict = build_verdict(
        join_type="inner",
        population_participation=POPULATION_PARTICIPATION_POPULATION_DEFINING,
        measurement=None,
        near_side="left",
        left_key_declared_unique=True,
        right_key_declared_unique=True,
        unmeasured_reason=REASON_MEASUREMENT_FAILED,
    )
    assert verdict.status == STATUS_OK


def test_build_verdict_metadata_gap_enrichment_only_is_ok():
    """The metadata-gap case resolves to multiplying-only with a genuine 0.0
    effect. ``enrichment_only`` excuses multiplying, and the edge has no
    filtering component at all, so this is a clean OK end-to-end — not just
    at the ``join_status``-with-explicit-booleans level."""
    verdict = build_verdict(
        join_type="left",
        population_participation=POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
        measurement=_CLEAN_STAR,
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=False,   # undeclared -> the metadata gap
    )
    assert verdict.reason == REASON_UNIQUENESS_NOT_DECLARED
    assert verdict.status == STATUS_OK


def test_build_verdict_metadata_gap_population_defining_is_ok():
    verdict = build_verdict(
        join_type="left",
        population_participation=POPULATION_PARTICIPATION_POPULATION_DEFINING,
        measurement=_CLEAN_STAR,
        near_side="left",
        left_key_declared_unique=False,
        right_key_declared_unique=False,
    )
    assert verdict.reason == REASON_UNIQUENESS_NOT_DECLARED
    assert verdict.status == STATUS_OK


# ---------------------------------------------------------------------------
# Near/far resolution
# ---------------------------------------------------------------------------


class _J:
    def __init__(self, left, right):
        self.id = uuid.uuid4()
        self.left_table_id = left
        self.right_table_id = right


def test_the_fact_side_is_the_near_side_whichever_way_the_join_is_drawn():
    fact, dim = uuid.uuid4(), uuid.uuid4()
    forward = _J(fact, dim)
    backward = _J(dim, fact)
    near = resolve_near_sides([forward, backward], fact_table_id=fact)
    assert near[forward.id] == "left"
    assert near[backward.id] == "right"


def test_a_snowflake_arm_is_oriented_by_distance_from_the_fact():
    fact, dim1, dim2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    inner = _J(fact, dim1)
    outer = _J(dim2, dim1)   # drawn away from the fact
    near = resolve_near_sides([inner, outer], fact_table_id=fact)
    assert near[inner.id] == "left"
    # dim1 is depth 1, dim2 depth 2 -> the RIGHT endpoint (dim1) is nearer.
    assert near[outer.id] == "right"


def test_a_model_with_no_fact_table_is_ambiguous_everywhere():
    a, b = uuid.uuid4(), uuid.uuid4()
    j = _J(a, b)
    assert resolve_near_sides([j], fact_table_id=None) == {j.id: None}


def test_an_endpoint_disconnected_from_the_fact_is_ambiguous():
    fact, island_a, island_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    connected = _J(fact, island_a)
    orphan = _J(island_b, uuid.uuid4())
    near = resolve_near_sides([connected, orphan], fact_table_id=fact)
    assert near[connected.id] == "left"
    assert near[orphan.id] is None


def test_equidistant_endpoints_are_ambiguous():
    """Two arms of a cycle meeting at the same depth: neither endpoint is the
    one a fact-rooted plan retains."""
    fact, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    joins = [_J(fact, a), _J(fact, b), _J(a, b)]
    near = resolve_near_sides(joins, fact_table_id=fact)
    assert near[joins[2].id] is None


# ---------------------------------------------------------------------------
# Model rollup (invariant 6)
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, status, measured=True):
        self.status = status
        self.measured = measured


def test_the_model_rollup_is_the_worst_join_status():
    rollup = roll_up_model_status(
        [_Row(STATUS_OK), _Row(STATUS_WARNING), _Row(STATUS_BLOCKED)],
        join_count=3,
    )
    assert rollup.status == STATUS_BLOCKED
    assert (rollup.warning_count, rollup.blocked_count) == (1, 1)
    assert rollup.evaluated is True


def test_a_model_with_no_joins_is_vacuously_evaluated_and_ok():
    rollup = roll_up_model_status([], join_count=0)
    assert (rollup.status, rollup.evaluated) == (STATUS_OK, True)


def test_a_model_with_joins_and_no_verdicts_is_not_evaluated():
    """The distinction phase G5 depends on: "nothing is wrong" and "nothing was
    checked" must not both read as a clean OK."""
    rollup = roll_up_model_status([], join_count=4)
    assert rollup.status == STATUS_OK
    assert rollup.evaluated is False
    assert rollup.evaluated_count == 0


def test_a_partially_measured_model_is_not_evaluated():
    rollup = roll_up_model_status(
        [_Row(STATUS_OK), _Row(STATUS_WARNING, measured=False)], join_count=2,
    )
    assert rollup.evaluated is False
    assert rollup.evaluated_count == 1


def test_the_rollup_reads_persisted_orm_rows_too():
    """The deploy path and the health endpoint must share ONE rollup, so it has
    to accept the persisted row shape, not only the in-memory verdict."""
    row = JoinPopulationCheck(
        join_id=uuid.uuid4(), model_id=uuid.uuid4(),
        classification=CLASSIFICATION_FILTERING,
        population_participation=POPULATION_PARTICIPATION_UNDECLARED,
        status=STATUS_BLOCKED, measured=True,
    )
    rollup = roll_up_model_status([row], join_count=1)
    assert (rollup.status, rollup.blocked_count, rollup.evaluated) == (
        STATUS_BLOCKED, 1, True,
    )


@pytest.mark.parametrize(
    "participation,status,measured,expected",
    [
        (POPULATION_PARTICIPATION_UNDECLARED, STATUS_BLOCKED, True, True),
        (POPULATION_PARTICIPATION_ENRICHMENT_ONLY, STATUS_BLOCKED, True, True),
        (POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS, STATUS_BLOCKED, True, False),
        (POPULATION_PARTICIPATION_UNDECLARED, STATUS_BLOCKED, False, False),
        (POPULATION_PARTICIPATION_UNDECLARED, STATUS_WARNING, True, False),
    ],
)
def test_g5_block_candidates_are_measured_and_policy_relevant(
    participation, status, measured, expected,
):
    row = type("Row", (), {
        "population_participation": participation,
        "status": status,
        "measured": measured,
    })()
    assert bool(blocking_join_population_rows([row])) is expected


def test_g5_mixed_model_keeps_measured_blocker_visible():
    measured_blocker = type("Row", (), {
        "population_participation": POPULATION_PARTICIPATION_UNDECLARED,
        "status": STATUS_BLOCKED,
        "measured": True,
    })()
    unmeasured = type("Row", (), {
        "population_participation": POPULATION_PARTICIPATION_UNDECLARED,
        "status": STATUS_WARNING,
        "measured": False,
    })()
    assert blocking_join_population_rows([measured_blocker, unmeasured]) == [
        measured_blocker
    ]


def test_g5_status_alone_cannot_block_below_authoritative_threshold():
    row = type("Row", (), {
        "population_participation": POPULATION_PARTICIPATION_UNDECLARED,
        "status": STATUS_BLOCKED,
        "measured": True,
        "row_effect_ratio": 0.05,
    })()
    assert blocking_join_population_rows([row], threshold=0.10) == []


# ---------------------------------------------------------------------------
# Probe SQL — dialect neutrality and identifier quoting
# ---------------------------------------------------------------------------


_CONNECTORS = ("postgresql", "bigquery", "hadoop_spark", "snowflake", "sqlserver")


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_the_side_probe_renders_on_every_supported_connector(connector):
    sql = build_side_probe_sql(
        connector=connector,
        retained_table="demo.fact", retained_column="dim id",
        joined_table="demo.dim", joined_column="id",
    )
    assert "COUNT(*)" in sql.upper()
    assert "LEFT JOIN" in sql.upper()
    # The far side is reduced to DISTINCT keys, so a matched count can never be
    # inflated by far-side duplicates — multiplication is measured separately.
    assert "DISTINCT" in sql.upper()
    # No raw identifier leaks: a column name with a space would break the
    # statement if it were not quoted through connector_qualify.
    assert "AS dim id" not in sql


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_the_join_count_probe_renders_on_every_supported_connector(connector):
    sql = build_inner_join_count_sql(
        connector=connector,
        left_table="demo.fact", left_column="dim_id",
        right_table="demo.dim", right_column="id",
    )
    upper = sql.upper()
    assert "COUNT(*)" in upper
    assert "JOIN" in upper
    assert "LEFT JOIN" not in upper


def test_bigquery_uses_backticks_and_postgres_double_quotes():
    """The dialect difference comes from ``connector_qualify``, not from a
    per-connector branch in this module (SQL-generation rule 1)."""
    bq = build_side_probe_sql(
        connector="bigquery",
        retained_table="p.d.fact", retained_column="k",
        joined_table="p.d.dim", joined_column="k",
    )
    pg = build_side_probe_sql(
        connector="postgresql",
        retained_table="s.fact", retained_column="k",
        joined_table="s.dim", joined_column="k",
    )
    assert "`" in bq and '"' not in bq
    assert '"' in pg and "`" not in pg


def test_the_module_contains_no_per_connector_branch():
    """Coverage-tool blind-spot discipline: assert the property directly on the
    source rather than trusting that the two rendering tests above happened to
    exercise every path."""
    import inspect

    from shared.semantic import join_population_validator as module

    source = inspect.getsource(module)
    for token in ('== "bigquery"', "== 'bigquery'", '== "postgresql"'):
        assert token not in source, (
            "dialect handling must go through connector_qualify / sqlglot, "
            f"never a literal connector comparison ({token})"
        )
