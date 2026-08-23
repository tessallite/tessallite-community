"""Bug-8580 — a pocket must not serve the JOIN-NARROWED row population.

A pocket caches ``SELECT * FROM <model>``, which the router compiles to the
model's FULL star join. A consuming query is compiled with join elision — only
the tables owning a referenced column are joined. When an elided join is
INNER/RIGHT/FULL (or can fan rows out), the pocket holds a different row
multiset than the query's own plan and every additive aggregate served from it
is silently understated. Measured on the acme-demo seed: 16,722 materialised
rows for a 100,000-row fact table, and ``SUM(transaction_count) WHERE
country_code='US'`` returning 1905 through the pocket against 11133 through
source.

The gate proves row-population equivalence and fails closed to source.

Test escape this file closes: the pocket suites only ever compared pocket
results to other pocket results, and the live pocket scenario asserted the SET
of grouping values rather than the counts, so a pocket serving a 6x-narrow
population passed green. Guard: population-equivalence unit matrix + a matcher
end-to-end case in the exact Bug-8580 shape. Tier: T1 (contract).
"""
from __future__ import annotations

import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from conftest import make_bound_query, make_dimension, make_measure
from src.ir.logical_query import LogicalFilter
from src.rewrite.joins import _build_joined_from_clause
from src.routing.pocket_matcher import (
    PocketSkipReason,
    _graph_from_snapshot,
    _query_plan_table_ids,
    find_best_pocket,
    invalidate_model_join_graph_cache,
    invalidate_model_table_cache,
)
from src.routing.pocket_population import (
    JoinEdge,
    ModelJoinGraph,
    population_proven,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Pure proof matrix
# ---------------------------------------------------------------------------

FACT = "t-fact"
DIM = "t-dim"
DIM2 = "t-dim2"
FACT_FK = "c-fact-fk"
FACT_FK2 = "c-fact-fk2"
DIM_PK = "c-dim-pk"
DIM2_PK = "c-dim2-pk"
DIM_BRIDGE = "c-dim-bridge"


def _graph(join_type: str, *, dim_key_is_pk: bool = True) -> ModelJoinGraph:
    """A two-relation star: fact --(join_type)-- dim, keyed on the dim's PK."""
    return ModelJoinGraph(
        table_ids=frozenset({FACT, DIM}),
        edges=(
            JoinEdge(
                left_table_id=FACT,
                right_table_id=DIM,
                left_column_id=FACT_FK,
                right_column_id=DIM_PK,
                join_type=join_type,
            ),
        ),
        pk_column_ids=frozenset({DIM_PK}) if dim_key_is_pk else frozenset(),
        table_id_by_column_id={FACT_FK: FACT, DIM_PK: DIM},
    )


@pytest.mark.parametrize(
    ("join_type", "expected"),
    [
        # INNER drops every fact row with no matching dimension row -> the
        # pocket's population is a strict subset of a bare fact scan. This is
        # the exact acme-demo shape that produced 1905 instead of 11133.
        ("inner", False),
        # FULL OUTER preserves the fact side but ADDS unmatched dimension rows,
        # so the row multiset is not preserved either.
        ("full", False),
        ("full outer", False),
        # A modeller LEFT join with the FACT as the modeller's left table
        # preserves every fact row, whichever direction the traversal took.
        ("left", True),
        # A modeller RIGHT join preserves the DIMENSION, not the fact.
        ("right", False),
        # Legacy / unrecognised vocabulary (``many_to_one`` is the schema
        # DEFAULT) renders as an un-flipped LEFT JOIN onto whichever side the
        # traversal accumulated first, which is decided by the plan's BASE
        # table. The pocket's base is measure-driven and the query's need not
        # be, so the same edge can preserve opposite relations in the two
        # plans -- see test_legacy_token_does_not_preserve_a_kept_table_that_
        # is_not_base_ward for the worked numbers. Not comparable -> refuse.
        ("many_to_one", False),
        ("", False),
    ],
)
def test_join_type_decides_whether_an_elided_relation_is_lossless(
    join_type, expected,
):
    assert population_proven(
        graph=_graph(join_type), query_table_ids={FACT},
    ) is expected


def test_left_join_without_a_unique_far_key_can_fan_out_and_is_refused():
    # A LEFT join drops nothing, but a non-unique far key duplicates each fact
    # row per match, inflating SUM/COUNT. Not provable -> refuse.
    assert population_proven(
        graph=_graph("left", dim_key_is_pk=False), query_table_ids={FACT},
    ) is False


def test_identical_plans_serve_even_over_a_lossy_join():
    # When the query itself joins the dimension, both plans apply the SAME
    # INNER edge, so the populations are identical and the pocket is exact.
    assert population_proven(
        graph=_graph("inner"), query_table_ids={FACT, DIM},
    ) is True


def test_single_relation_model_is_always_equivalent():
    graph = ModelJoinGraph(table_ids=frozenset({FACT}))
    assert population_proven(graph=graph, query_table_ids={FACT}) is True


def test_unresolvable_graph_is_unproven():
    assert population_proven(graph=None, query_table_ids={FACT}) is False


def test_unknown_query_plan_is_unproven_not_unconstrained():
    # ``SELECT count(*) FROM model`` with no filters resolves no physical
    # relation here. That is UNKNOWN, never "no constraint".
    assert population_proven(graph=_graph("left"), query_table_ids=set()) is False
    assert population_proven(graph=_graph("left"), query_table_ids=None) is False


def test_query_spanning_two_components_still_judges_every_reachable_arm():
    """Both components enter the plan; a lossy edge in either one refuses."""
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM, DIM2, "t-island"}),
        edges=(
            JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "left"),
            JoinEdge("t-island", DIM2, DIM_BRIDGE, DIM2_PK, "inner"),
        ),
        pk_column_ids=frozenset({DIM_PK, DIM2_PK}),
        table_id_by_column_id={
            FACT_FK: FACT, DIM_PK: DIM, DIM_BRIDGE: "t-island", DIM2_PK: DIM2,
        },
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is True
    assert population_proven(
        graph=graph, query_table_ids={FACT, "t-island"},
    ) is False


def test_edge_between_two_elided_relations_is_refused():
    # fact -- dim -- dim2, both dim and dim2 elided by the query: a multi-hop
    # attachment chain, refused conservatively.
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM, DIM2}),
        edges=(
            JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "left"),
            # The middle relation joins its child on its OWN sole primary key,
            # so preservation, uniqueness and column ownership all hold on both
            # hops. The multi-hop rule is then the ONLY thing refusing this --
            # which is what makes this test a real guard for it.
            JoinEdge(DIM, DIM2, DIM_PK, DIM2_PK, "left"),
        ),
        pk_column_ids=frozenset({DIM_PK, DIM2_PK}),
        table_id_by_column_id={FACT_FK: FACT, DIM_PK: DIM, DIM2_PK: DIM2},
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is False
    # ... but once the query itself plans over the middle relation, the only
    # elided table hangs directly off the kept set and is provable again.
    assert population_proven(graph=graph, query_table_ids={FACT, DIM}) is True


def test_legacy_token_chain_is_refused_at_the_plan_level():
    """A legacy token anywhere in the plan refuses it, elided or not.

    Reviewer R1 disproved the earlier reading that an un-flipped LEFT JOIN is
    safe in either direction. The plan-level orientation rule now refuses this
    chain before the multi-hop rule is even reached, and it refuses it for the
    kept-middle case too -- which the multi-hop rule alone would have allowed.
    """
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM, DIM2}),
        edges=(
            JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "many_to_one"),
            JoinEdge(DIM, DIM2, DIM_PK, DIM2_PK, "many_to_one"),
        ),
        pk_column_ids=frozenset({DIM_PK, DIM2_PK}),
        table_id_by_column_id={FACT_FK: FACT, DIM_PK: DIM, DIM2_PK: DIM2},
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is False
    assert population_proven(graph=graph, query_table_ids={FACT, DIM}) is False


def test_modeller_left_join_pointing_the_other_way_does_not_preserve_the_fact():
    """A modeller LEFT join preserves the modeller's LEFT table, whichever way
    the traversal renders it. With the DIMENSION on that side, the fact rows
    are the ones that can be dropped."""
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM}),
        edges=(JoinEdge(DIM, FACT, DIM_PK, FACT_FK, "left"),),
        pk_column_ids=frozenset({DIM_PK}),
        table_id_by_column_id={FACT_FK: FACT, DIM_PK: DIM},
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is False
    # ... and the mirror image (modeller RIGHT with the dim on the left) DOES
    # preserve the fact, so the same topology accelerates when declared right.
    mirrored = ModelJoinGraph(
        table_ids=graph.table_ids,
        edges=(JoinEdge(DIM, FACT, DIM_PK, FACT_FK, "right"),),
        pk_column_ids=graph.pk_column_ids,
        table_id_by_column_id=graph.table_id_by_column_id,
    )
    assert population_proven(graph=mirrored, query_table_ids={FACT}) is True


def test_two_elided_star_arms_are_each_proven_independently():
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM, DIM2}),
        edges=(
            JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "left"),
            JoinEdge(DIM2, FACT, DIM2_PK, FACT_FK2, "right"),
        ),
        pk_column_ids=frozenset({DIM_PK, DIM2_PK}),
        table_id_by_column_id={
            FACT_FK: FACT, FACT_FK2: FACT, DIM_PK: DIM, DIM2_PK: DIM2,
        },
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is True
    # One lossy arm poisons the whole pocket, not just that arm: the pocket
    # table itself already lost the rows.
    lossy = ModelJoinGraph(
        table_ids=graph.table_ids,
        edges=(graph.edges[0], JoinEdge(DIM2, FACT, DIM2_PK, FACT_FK2, "inner")),
        pk_column_ids=graph.pk_column_ids,
        table_id_by_column_id=graph.table_id_by_column_id,
    )
    assert population_proven(graph=lossy, query_table_ids={FACT}) is False


def test_unreachable_relation_is_excluded_from_the_plan_not_counted_against_it():
    """A relation in a different connected component can never be joined.

    ``_build_joined_from_clause`` grows the FROM clause outward from the base
    along declared edges and fails the compile outright if it cannot reach a
    required relation, so an unreachable table is in no plan at all. Counting it
    as an unproven "extra" would refuse every query on any model that declares a
    detached relation. Measured on acme-demo ``modely``: 29 declared relations,
    only 23 reachable from the fact, and the live compiled ``SELECT *`` joins
    exactly those 23.
    """
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM}),
        edges=(),
        pk_column_ids=frozenset({DIM_PK}),
        table_id_by_column_id={DIM_PK: DIM},
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is True
    # The reachable arm is still judged on its own merits: attach a lossy edge
    # and the same query is refused.
    lossy = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM, DIM2}),
        edges=(JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "inner"),),
        pk_column_ids=frozenset({DIM_PK}),
        table_id_by_column_id={FACT_FK: FACT, DIM_PK: DIM},
    )
    assert population_proven(graph=lossy, query_table_ids={FACT}) is False


def test_query_naming_a_relation_outside_the_model_graph_is_unproven():
    graph = _graph("left")
    assert population_proven(
        graph=graph, query_table_ids={FACT, "t-not-in-this-model"},
    ) is False


def test_primary_key_borrowed_from_another_relation_is_not_a_uniqueness_proof():
    # The far column is a declared PK but belongs to the FACT, not the dim, so
    # it proves nothing about how many dim rows match.
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM}),
        edges=(JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "left"),),
        pk_column_ids=frozenset({DIM_PK}),
        table_id_by_column_id={FACT_FK: FACT, DIM_PK: FACT},
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is False


def test_self_join_is_refused():
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM}),
        edges=(
            JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "left"),
            JoinEdge(DIM, DIM, DIM_PK, DIM_BRIDGE, "left"),
        ),
        pk_column_ids=frozenset({DIM_PK, DIM_BRIDGE}),
        table_id_by_column_id={FACT_FK: FACT, DIM_PK: DIM, DIM_BRIDGE: DIM},
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is False


# ---------------------------------------------------------------------------
# End-to-end through find_best_pocket, in the Bug-8580 shape
# ---------------------------------------------------------------------------

_FACT_PHYSICAL = "public.payment_transaction"
_DIM_PHYSICAL = "public.country_dim"


@pytest.fixture(autouse=True)
def _clear_matcher_caches():
    invalidate_model_table_cache()
    invalidate_model_join_graph_cache()
    yield
    invalidate_model_table_cache()
    invalidate_model_join_graph_cache()


def _snapshot_shape(join_type: str, *, dim_key_is_pk: bool = True):
    """A deployed shape for a two-relation star model."""
    return types.SimpleNamespace(
        tables_by_id={
            FACT: {"id": FACT, "physical_name": _FACT_PHYSICAL, "alias": "f",
                   "table_type": "fact"},
            DIM: {"id": DIM, "physical_name": _DIM_PHYSICAL, "alias": "d",
                  "table_type": "dim_detail"},
        },
        columns_by_id={
            "c-country": {"id": "c-country", "model_table_id": FACT,
                          "column_name": "country_code", "is_primary_key": False},
            "c-amount": {"id": "c-amount", "model_table_id": FACT,
                         "column_name": "transaction_count", "is_primary_key": False},
            FACT_FK: {"id": FACT_FK, "model_table_id": FACT,
                      "column_name": "country_key", "is_primary_key": False},
            DIM_PK: {"id": DIM_PK, "model_table_id": DIM,
                     "column_name": "country_key", "is_primary_key": dim_key_is_pk},
            "c-dim-label": {"id": "c-dim-label", "model_table_id": DIM,
                            "column_name": "country_name", "is_primary_key": False},
        },
        join_rows=[{
            "left_table_id": FACT, "right_table_id": DIM,
            "left_column_id": FACT_FK, "right_column_id": DIM_PK,
            "join_type": join_type,
        }],
        user_defined_attribute_rows=[],
    )


def _bound_query(*, include_dim_table_column: bool = False):
    """``SUM(transaction_count) ... WHERE country_code = 'US'`` — fact only."""
    country = make_dimension("country_code")
    country.source_column_id = "c-country"
    dims = [country]
    if include_dim_table_column:
        label = make_dimension("country_name")
        label.source_column_id = "c-dim-label"
        dims.append(label)
    amount = make_measure("transaction_count")
    amount.source_column_id = "c-amount"
    bq = make_bound_query(
        dims,
        [amount],
        filters=[LogicalFilter("country_code", "eq", "US")],
        raw_sql=(
            "SELECT SUM(transaction_count) AS c FROM test_model "
            "WHERE country_code = 'US'"
        ),
    )
    bq.logical_query.query_fingerprint = "fp-e1"
    return bq


def _pocket(*, status="fresh"):
    return types.SimpleNamespace(
        id="pocket-8580",
        model_id="model-1",
        status=status,
        failure_reason=None,
        built_for_version_id="v1",
        built_for_epoch=0,
        query_fingerprint="fp-pocket-star",
        defining_sql="SELECT * FROM test_model WHERE country_code = 'US'",
        last_refresh_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        predicates=[types.SimpleNamespace(
            column_name="country_code", operator="eq", value_json={"value": "US"},
        )],
    )


async def _match(bq, shape, *, pocket=None):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(
            all=lambda: [pocket or _pocket()]
            if (pocket is None or pocket.status == "fresh") else []
        ),
    ))
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=AsyncMock(return_value=shape),
    ), patch("src.routing.pocket_matcher.system_snapshot_get") as snap, patch(
        "src.routing.pocket_matcher.get_setting", new_callable=AsyncMock
    ) as get_setting:
        snap.side_effect = lambda key: {
            "pocket.enabled": True,
            "pocket.require_tenant_filter": False,
            "pocket.tenant_scope_from_context": True,
        }.get(key)
        get_setting.return_value = True
        return await find_best_pocket(bq, db)


async def test_inner_joined_star_pocket_does_not_serve_a_fact_only_query():
    """The Bug-8580 reproduction, end to end through the matcher.

    Every containment check the matcher used to run passes here: the pocket's
    predicate column is a subset of the query's filter columns, its ``eq 'US'``
    predicate is implied, its FROM is the model, and it covers the required
    table. Only the row population differs — and that is what must stop it.
    """
    result = await _match(_bound_query(), _snapshot_shape("inner"))
    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.JOIN_POPULATION_MISMATCH


async def test_row_preserving_star_pocket_still_serves_the_same_query():
    """Guard against over-correction: a LEFT join onto a unique key drops and
    duplicates nothing, so the pocket is exact and must still accelerate."""
    result = await _match(_bound_query(), _snapshot_shape("left"))
    assert result.pocket is not None
    assert result.skipped_reason is None


async def test_left_join_on_a_non_unique_key_does_not_serve():
    result = await _match(
        _bound_query(), _snapshot_shape("left", dim_key_is_pk=False),
    )
    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.JOIN_POPULATION_MISMATCH


async def test_population_mismatch_parks_the_candidate_with_an_exact_reason():
    """G4: a mismatch parks eligibility without rewriting freshness."""
    pocket = _pocket()
    result = await _match(_bound_query(), _snapshot_shape("inner"), pocket=pocket)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.JOIN_POPULATION_MISMATCH
    assert pocket.status == "fresh"
    assert pocket.population_eligibility == "ineligible"
    assert pocket.population_eligibility_reason == "Ineligible: population mismatch"
    assert pocket.failure_reason == "Ineligible: population mismatch"


async def test_population_proof_reactivates_only_a_matching_ineligible_pocket():
    """G4: eligibility returns only after the same plan proves lossless."""
    pocket = _pocket(status="fresh")
    pocket.population_eligibility = "ineligible"
    pocket.failure_reason = "Ineligible: population mismatch"

    result = await _match(_bound_query(), _snapshot_shape("left"), pocket=pocket)

    assert result.pocket is pocket
    assert pocket.status == "fresh"
    assert pocket.population_eligibility == "eligible"
    assert pocket.failure_reason is None


async def test_g4_sol_r1_b02_definition_edit_cannot_reactivate_old_generation():
    """A definition edit leaves the old physical generation unservable."""
    pocket = _pocket(status="stale")
    pocket.population_eligibility = "ineligible"
    result = await _match(_bound_query(), _snapshot_shape("left"), pocket=pocket)
    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.NO_CANDIDATES
    assert pocket.status == "stale"
    assert pocket.population_eligibility == "ineligible"


async def test_g4_sol_r1_b02_ineligible_revert_and_target_invalidation_clear_build_trust():
    """Control-plane invalidation clears both proof and physical trust."""
    from shared.artifact_target_binding import _invalidate_artifacts

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[types.SimpleNamespace(rowcount=0), types.SimpleNamespace(rowcount=1)]
    )
    await _invalidate_artifacts(
        db,
        pocket_scope=True,
        aggregate_scope=True,
        reason="target repointed",
    )
    pocket_stmt = db.execute.await_args_list[1].args[0]
    rendered = str(pocket_stmt)
    assert "population_eligibility" in rendered
    assert "built_for_version_id" in rendered
    assert "active_refresh_run_id" in rendered


async def test_g4_sol_r1_b03_explain_lifecycle_policy_is_explicit_and_persisted():
    """Explain evaluates the matcher without a lifecycle write."""
    db = AsyncMock()
    pocket = _pocket()
    db.execute = AsyncMock(return_value=types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: [pocket]),
    ))
    shape = _snapshot_shape("inner")
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=AsyncMock(return_value=shape),
    ), patch("src.routing.pocket_matcher.system_snapshot_get") as snap, patch(
        "src.routing.pocket_matcher.get_setting", new_callable=AsyncMock
    ) as get_setting:
        snap.side_effect = lambda key: {
            "pocket.enabled": True,
            "pocket.require_tenant_filter": False,
            "pocket.tenant_scope_from_context": True,
        }.get(key)
        get_setting.return_value = True
        result = await find_best_pocket(
            _bound_query(), db, persist_population_observation=False,
        )
    assert result.skipped_reason == PocketSkipReason.JOIN_POPULATION_MISMATCH
    assert db.execute.await_count == 1
    assert not hasattr(pocket, "population_eligibility")


async def test_query_spanning_the_whole_star_serves_from_an_inner_join_pocket():
    """Both plans join the same relations, so the populations are identical."""
    result = await _match(
        _bound_query(include_dim_table_column=True), _snapshot_shape("inner"),
    )
    assert result.pocket is not None
    assert result.skipped_reason is None


@pytest.mark.real_population_resolvers
async def test_unresolvable_deployed_snapshot_refuses_every_pocket():
    """The POPULATION gate must be what refuses an unresolvable snapshot.

    Accepting either skip reason here would pass against a deleted gate, because
    ``_required_physical_tables`` independently fails closed on the same input.
    Stub that out so only the population verdict can produce the refusal.
    """
    with patch(
        "src.routing.pocket_matcher._required_physical_tables",
        new=AsyncMock(return_value=set()),
    ):
        result = await _match(_bound_query(), None)
    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.JOIN_POPULATION_MISMATCH


# ---------------------------------------------------------------------------
# Reviewer R1: traversal direction, key multiplicity, snapshot fail-closed.
# The proof reasons over table SETS, but the emitted row population is decided
# by the spanning tree AND its orientation. These pin the places where a
# set-only argument is not enough.
# ---------------------------------------------------------------------------

_R_FACT, _R_DIM = "r-fact", "r-dim"
_R_FK, _R_DIM_PK, _R_DIM_NAME, _R_AMT = "r-fk", "r-dim-pk", "r-dim-name", "r-amt"


def _reviewer_tbl(tid, phys, alias):
    return types.SimpleNamespace(
        id=tid, physical_name=phys, alias=alias, table_type="fact",
    )


def _reviewer_col(cid, tid, name, dtype="string"):
    return types.SimpleNamespace(
        id=cid, model_table_id=tid, column_name=name, data_type=dtype,
    )


_REVIEWER_TABLES = {
    _R_FACT: _reviewer_tbl(_R_FACT, "demo.sales", "a"),
    _R_DIM: _reviewer_tbl(_R_DIM, "demo.country", "b"),
}
_REVIEWER_COLS = {
    _R_FK: _reviewer_col(_R_FK, _R_FACT, "country_code"),
    _R_DIM_PK: _reviewer_col(_R_DIM_PK, _R_DIM, "code"),
    _R_DIM_NAME: _reviewer_col(_R_DIM_NAME, _R_DIM, "country_name"),
    _R_AMT: _reviewer_col(_R_AMT, _R_FACT, "amount", "numeric"),
}


def _legacy_token_graph() -> ModelJoinGraph:
    """fact --(many_to_one, the SCHEMA DEFAULT)-- dim, both keys PK-flagged."""
    return ModelJoinGraph(
        table_ids=frozenset({_R_FACT, _R_DIM}),
        edges=(JoinEdge(_R_FACT, _R_DIM, _R_FK, _R_DIM_PK, "many_to_one"),),
        pk_column_ids=frozenset({_R_FK, _R_DIM_PK}),
        table_id_by_column_id={
            _R_FK: _R_FACT, _R_DIM_PK: _R_DIM,
            _R_DIM_NAME: _R_DIM, _R_AMT: _R_FACT,
        },
    )


def test_legacy_token_does_not_preserve_a_kept_table_that_is_not_base_ward():
    """An un-flipped LEFT JOIN preserves the ACCUMULATED side, not the kept one.

    The pocket's base is the fact (``SELECT *`` resolves a measure first), so a
    dimension-only query's own plan is a bare ``country`` scan while the pocket
    holds ``sales LEFT JOIN country``. Countries with no sales are missing and a
    NULL group appears: SELECT country_name, count(*) GROUP BY country_name
    returns {United States: 1, Canada: 1} from the query's own plan and
    {United States: 2, NULL: 1} from the pocket.
    """
    assert population_proven(
        graph=_legacy_token_graph(), query_table_ids={_R_DIM},
    ) is False


def test_identical_table_sets_are_not_identical_plans_under_a_legacy_token():
    """``extra`` empty is not sufficient: the two plans can traverse the same
    legacy edge in opposite directions because their BASE tables differ."""
    joins = [types.SimpleNamespace(
        id="J1", left_table_id=_R_FACT, left_column_id=_R_FK,
        right_table_id=_R_DIM, right_column_id=_R_DIM_PK,
        join_type="many_to_one",
    )]
    pocket_from = _build_joined_from_clause(
        base_table_id=_R_FACT, required_table_ids={_R_FACT, _R_DIM}, joins=joins,
        tables_by_id=_REVIEWER_TABLES, columns_by_id=_REVIEWER_COLS,
        alias_by_table_id={_R_FACT: "a", _R_DIM: "b"},
    )
    query_from = _build_joined_from_clause(
        base_table_id=_R_DIM, required_table_ids={_R_FACT, _R_DIM}, joins=joins,
        tables_by_id=_REVIEWER_TABLES, columns_by_id=_REVIEWER_COLS,
        alias_by_table_id={_R_DIM: "b"},
    )
    # Sanity: the renderer really does preserve a different physical side.
    assert pocket_from.split(" LEFT JOIN ")[0] != query_from.split(" LEFT JOIN ")[0]
    assert population_proven(
        graph=_legacy_token_graph(), query_table_ids={_R_FACT, _R_DIM},
    ) is False


def test_composite_primary_key_is_not_a_uniqueness_proof():
    """``is_primary_key`` is a per-column modeller flag with no multiplicity
    validation. Joining on ONE half of a composite key fans the fact out:
    F=(f1,did=1,100)(f2,did=1,200), D=(1,v1)(1,v2) -> SUM(amt) 300 -> 600."""
    alloc = "r-alloc"
    graph = ModelJoinGraph(
        table_ids=frozenset({_R_FACT, alloc}),
        edges=(JoinEdge(_R_FACT, alloc, _R_FK, "r-alloc-id", "left"),),
        pk_column_ids=frozenset({"r-alloc-id", "r-alloc-ver"}),
        table_id_by_column_id={
            _R_FK: _R_FACT, "r-alloc-id": alloc, "r-alloc-ver": alloc,
        },
    )
    assert population_proven(graph=graph, query_table_ids={_R_FACT}) is False


def test_query_plan_table_ids_maps_a_real_bound_query():
    """The BoundQuery -> Q mapping is the proof's riskiest approximation and was
    patched out of every other suite. Pin it directly."""
    graph = _legacy_token_graph()
    dim = types.SimpleNamespace(name="country_name", source_column_id=_R_DIM_NAME)
    meas = types.SimpleNamespace(name="amount", source_column_id=_R_AMT)

    bq = types.SimpleNamespace(
        resolved_dimensions=[dim], resolved_measures=[meas],
        resolved_filters=[], resolved_dimensions_by_name={"country_name": dim},
    )
    assert _query_plan_table_ids(bq, graph, {}) == {_R_DIM, _R_FACT}

    # A filter naming a dimension the matcher cannot resolve is UNKNOWN, never
    # "no extra table" -- the real plan would join whatever relation owns it.
    bq_unknown = types.SimpleNamespace(
        resolved_dimensions=[], resolved_measures=[meas],
        resolved_filters=[types.SimpleNamespace(dimension_name="region")],
        resolved_dimensions_by_name={},
    )
    assert _query_plan_table_ids(bq_unknown, graph, {}) is None

    # A UDA-backed dimension contributes its owning table.
    uda_dim = types.SimpleNamespace(
        name="bucket", source_column_id=None, user_defined_attribute_id="u1",
    )
    bq_uda = types.SimpleNamespace(
        resolved_dimensions=[uda_dim], resolved_measures=[],
        resolved_filters=[], resolved_dimensions_by_name={"bucket": uda_dim},
    )
    assert _query_plan_table_ids(bq_uda, graph, {"u1": _R_DIM}) == {_R_DIM}


def test_case_colliding_dimension_names_are_poisoned_not_guessed():
    """Two dimensions differing only in case must not let a filter bind to the
    wrong relation. Adding a table to Q is the UNSAFE direction (it shrinks
    ``extra``), so an ambiguous name has to fail closed."""
    graph = _legacy_token_graph()
    lower = types.SimpleNamespace(name="code", source_column_id=_R_DIM_PK)
    upper = types.SimpleNamespace(name="Code", source_column_id=_R_AMT)
    meas = types.SimpleNamespace(name="amount", source_column_id=_R_AMT)
    bq = types.SimpleNamespace(
        resolved_dimensions=[], resolved_measures=[meas],
        resolved_filters=[types.SimpleNamespace(dimension_name="CODE")],
        resolved_dimensions_by_name={"code": lower, "Code": upper},
    )
    assert _query_plan_table_ids(bq, graph, {}) is None


def test_multi_relation_snapshot_with_no_join_family_fails_closed():
    """``join_rows`` defaults to [] in DeployedShape. A multi-relation model with
    no edges makes reach(Q)==Q, so ``extra`` is empty and EVERY pocket is
    'proven'. The tables branch fails closed here; the edges branch must too."""
    shape = types.SimpleNamespace(
        tables_by_id={_R_FACT: {}, _R_DIM: {}},
        join_rows=[],
        columns_by_id={_R_AMT: {"model_table_id": _R_FACT}},
        user_defined_attribute_rows=[],
    )
    graph, _ = _graph_from_snapshot(shape)
    assert graph is None


def test_cyclic_plan_subgraph_is_refused():
    """With a cycle the FROM clause is a SPANNING TREE choice, so the two plans
    can join over the same relations on DIFFERENT predicates -- and which edge
    is dropped depends on set iteration order."""
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM, DIM2}),
        edges=(
            JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "left"),
            JoinEdge(FACT, DIM2, FACT_FK2, DIM2_PK, "left"),
            JoinEdge(DIM, DIM2, DIM_BRIDGE, DIM2_PK, "left"),
        ),
        pk_column_ids=frozenset({DIM_PK, DIM2_PK, DIM_BRIDGE}),
        table_id_by_column_id={
            FACT_FK: FACT, FACT_FK2: FACT,
            DIM_PK: DIM, DIM_BRIDGE: DIM, DIM2_PK: DIM2,
        },
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is False
    assert population_proven(
        graph=graph, query_table_ids={FACT, DIM, DIM2},
    ) is False


def test_parallel_edges_between_one_pair_are_refused():
    """Two edges over the same pair are a cycle of length two: the renderer uses
    one and silently drops the other, and the choice is not deterministic."""
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM}),
        edges=(
            JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "left"),
            JoinEdge(FACT, DIM, FACT_FK2, DIM_PK, "left"),
        ),
        pk_column_ids=frozenset({DIM_PK}),
        table_id_by_column_id={FACT_FK: FACT, FACT_FK2: FACT, DIM_PK: DIM},
    )
    assert population_proven(graph=graph, query_table_ids={FACT}) is False


async def test_deploy_eviction_hook_clears_the_join_graph_cache():
    """Producer/consumer wiring: model-service calls ``evict_model_cache``
    after a deploy. A DEPLOYED model's graph key carries the deploy pointer and
    self-invalidates, but an UNDEPLOYED model keys on ``(id, "", 0)`` -- without
    this hook, changing a join from ``left`` to ``inner`` would keep the pocket
    route open on the previous, now-wrong population proof for the cache TTL.
    """
    from src.api.routes import evict_model_cache
    from src.routing import pocket_matcher as _pm

    invalidate_model_join_graph_cache()
    key = ("model-evict", "", 0)
    _pm._JOIN_GRAPH_CACHE[key] = (
        _pm._time.monotonic() + 300,
        ModelJoinGraph(table_ids=frozenset({FACT})),
        {},
    )
    assert key in _pm._JOIN_GRAPH_CACHE

    await evict_model_cache("model-evict")

    assert key not in _pm._JOIN_GRAPH_CACHE, (
        "evict_model_cache must clear the pocket row-population join graph, "
        "or a draft join edit keeps serving on the previous proof"
    )


# ---------------------------------------------------------------------------
# Reviewer R2: the proof must be ROOT-independent.
#
# `population_proven` reasons about WHICH edges exist and WHICH side each one
# preserves. It does not model the ORDER they are applied in — and that order is
# decided by the plan's BASE table, which differs between the two plans: a
# pocket's `SELECT *` roots the tree at the first resolved MEASURE's table
# (binder.py select_star -> all measures; table_resolution picks measures first),
# while a measure-free projection query roots it at its first resolved DIMENSION.
# An outer join does not commute with a row-reducing (INNER) or row-adding (FULL)
# join on the same path, so the same edges in a different order are different
# rows. This harness renders BOTH real plans and executes them.
# ---------------------------------------------------------------------------

import sqlite3

_O_FACT, _O_MID, _O_LEAF = "o-fact", "o-mid", "o-leaf"
_O_FK, _O_MID_PK, _O_MID_FK, _O_LEAF_PK = "o-fk", "o-mid-pk", "o-mid-fk", "o-leaf-pk"
_O_AMT = "o-amt"


def _order_graph(second_edge_type: str) -> ModelJoinGraph:
    """fact --left(preserves fact)--> mid --<second_edge_type>--> leaf."""
    return ModelJoinGraph(
        table_ids=frozenset({_O_FACT, _O_MID, _O_LEAF}),
        edges=(
            JoinEdge(_O_FACT, _O_MID, _O_FK, _O_MID_PK, "left"),
            JoinEdge(_O_MID, _O_LEAF, _O_MID_FK, _O_LEAF_PK, second_edge_type),
        ),
        pk_column_ids=frozenset({_O_MID_PK, _O_LEAF_PK}),
        table_id_by_column_id={
            _O_FK: _O_FACT, _O_AMT: _O_FACT,
            _O_MID_PK: _O_MID, _O_MID_FK: _O_MID, _O_LEAF_PK: _O_LEAF,
        },
    )


_ORDER_TABLES = {
    _O_FACT: types.SimpleNamespace(
        id=_O_FACT, physical_name="fact_a", alias="a", table_type="fact"),
    _O_MID: types.SimpleNamespace(
        id=_O_MID, physical_name="dim_b", alias="b", table_type="dimension"),
    _O_LEAF: types.SimpleNamespace(
        id=_O_LEAF, physical_name="dim_c", alias="c", table_type="dimension"),
}
_ORDER_COLS = {
    _O_FK: types.SimpleNamespace(
        id=_O_FK, model_table_id=_O_FACT, column_name="b_fk", data_type="int"),
    _O_AMT: types.SimpleNamespace(
        id=_O_AMT, model_table_id=_O_FACT, column_name="amount", data_type="int"),
    _O_MID_PK: types.SimpleNamespace(
        id=_O_MID_PK, model_table_id=_O_MID, column_name="b_pk", data_type="int"),
    _O_MID_FK: types.SimpleNamespace(
        id=_O_MID_FK, model_table_id=_O_MID, column_name="c_fk", data_type="int"),
    _O_LEAF_PK: types.SimpleNamespace(
        id=_O_LEAF_PK, model_table_id=_O_LEAF, column_name="c_pk", data_type="int"),
}


def _order_joins(second_edge_type: str) -> list:
    return [
        types.SimpleNamespace(
            id="oj1", left_table_id=_O_FACT, left_column_id=_O_FK,
            right_table_id=_O_MID, right_column_id=_O_MID_PK, join_type="left"),
        types.SimpleNamespace(
            id="oj2", left_table_id=_O_MID, left_column_id=_O_MID_FK,
            right_table_id=_O_LEAF, right_column_id=_O_LEAF_PK,
            join_type=second_edge_type),
    ]


def _render_order_plan(base: str, second_edge_type: str) -> str:
    """The FROM clause the REAL renderer emits for this plan, rooted at `base`."""
    return _build_joined_from_clause(
        base_table_id=base,
        required_table_ids={_O_FACT, _O_MID, _O_LEAF},
        joins=_order_joins(second_edge_type),
        tables_by_id=_ORDER_TABLES,
        columns_by_id=_ORDER_COLS,
        alias_by_table_id={t: _ORDER_TABLES[t].alias for t in _ORDER_TABLES},
        connector="postgresql",
    ).replace('"', '')


def _sum_amount_over(from_clause: str) -> int:
    """Execute the rendered plan over an adversarial 3-row fact."""
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("create table fact_a (b_fk int, amount int)")
    cur.execute("create table dim_b (b_pk int, c_fk int)")
    cur.execute("create table dim_c (c_pk int)")
    # a1 joins b1 which joins c1; a2 joins b2 which matches no c; a3 joins no b.
    cur.executemany("insert into fact_a values (?,?)", [(1, 100), (2, 200), (None, 300)])
    cur.executemany("insert into dim_b values (?,?)", [(1, 10), (2, 99)])
    cur.executemany("insert into dim_c values (?)", [(10,)])
    rows = cur.execute(f"SELECT a.amount FROM {from_clause}").fetchall()
    return sum(r[0] for r in rows if r[0] is not None)


@pytest.mark.skipif(
    tuple(int(p) for p in sqlite3.sqlite_version.split(".")[:2]) < (3, 39),
    reason="the reverse-rooted plan renders RIGHT JOIN, which needs SQLite >= 3.39",
)
@pytest.mark.parametrize("second_edge_type", ["inner", "full", "left", "right"])
def test_population_proof_is_root_independent(second_edge_type):
    """PROVEN must imply the same numbers whichever relation the FROM is rooted at.

    Every edge here is orientation-declared, the subgraph is acyclic, and the
    query spans all three relations so ``extra`` is empty — the proof's rule 5
    returns PROVEN unless rule 4 refuses first. But the pocket roots at the fact
    and the query roots at the leaf dimension, and with a row-reducing edge
    behind a row-preserving one the two renderings disagree:

        base=fact_a : fact_a LEFT JOIN dim_b ... INNER JOIN dim_c ...  -> SUM 100
        base=dim_c  : dim_c INNER JOIN dim_b ... RIGHT JOIN fact_a ... -> SUM 600
    """
    proven = population_proven(
        graph=_order_graph(second_edge_type),
        query_table_ids={_O_FACT, _O_MID, _O_LEAF},
    )
    pocket_total = _sum_amount_over(_render_order_plan(_O_FACT, second_edge_type))
    query_total = _sum_amount_over(_render_order_plan(_O_LEAF, second_edge_type))
    if proven:
        assert pocket_total == query_total, (
            f"population_proven returned PROVEN for a second edge of "
            f"{second_edge_type!r}, but the pocket's plan (base=fact) totals "
            f"{pocket_total} while the query's own plan (base=dim_c) totals "
            f"{query_total}. The proof does not model the FROM clause's ROOT."
        )


def test_row_reducing_edge_behind_a_row_preserving_one_is_refused():
    """The pure assertion behind the executed harness above, so the property
    stays guarded on a runner where the SQLite RIGHT JOIN skip applies."""
    assert population_proven(
        graph=_order_graph("inner"),
        query_table_ids={_O_FACT, _O_MID, _O_LEAF},
    ) is False
    assert population_proven(
        graph=_order_graph("full"),
        query_table_ids={_O_FACT, _O_MID, _O_LEAF},
    ) is False


def test_an_all_inner_plan_stays_provable_when_the_query_spans_it():
    """Guard against over-correction: inner joins are associative and
    commutative, so an all-INNER plan is order-free and the identical-plan case
    must still accelerate."""
    graph = ModelJoinGraph(
        table_ids=frozenset({_O_FACT, _O_MID, _O_LEAF}),
        edges=(
            JoinEdge(_O_FACT, _O_MID, _O_FK, _O_MID_PK, "inner"),
            JoinEdge(_O_MID, _O_LEAF, _O_MID_FK, _O_LEAF_PK, "inner"),
        ),
        pk_column_ids=frozenset({_O_MID_PK, _O_LEAF_PK}),
        table_id_by_column_id={
            _O_FK: _O_FACT, _O_MID_PK: _O_MID,
            _O_MID_FK: _O_MID, _O_LEAF_PK: _O_LEAF,
        },
    )
    assert population_proven(
        graph=graph, query_table_ids={_O_FACT, _O_MID, _O_LEAF},
    ) is True


def test_a_row_preserving_snowflake_chain_stays_provable():
    """Guard against over-correction the other way: outer edges that all point
    their preserved side back toward one core are order-free, so a clean
    snowflake still accelerates -- including when the query elides the leaf."""
    graph = _order_graph("left")
    assert population_proven(
        graph=graph, query_table_ids={_O_FACT, _O_MID, _O_LEAF},
    ) is True
    assert population_proven(
        graph=graph, query_table_ids={_O_FACT, _O_MID},
    ) is True


def test_a_cycle_in_one_component_is_not_masked_by_another_component():
    """The tree test must be PER COMPONENT.

    A whole-plan forest bound (``edges <= nodes - 1``) lets a cycle in one
    component hide behind a second component's spare node budget: three
    relations joined in a triangle plus one isolated relation is 3 edges over
    4 relations, which a global bound accepts even though the triangle makes
    the FROM clause a spanning-tree choice.
    """
    island = "t-island"
    graph = ModelJoinGraph(
        table_ids=frozenset({FACT, DIM, DIM2, island}),
        edges=(
            JoinEdge(FACT, DIM, FACT_FK, DIM_PK, "left"),
            JoinEdge(FACT, DIM2, FACT_FK2, DIM2_PK, "left"),
            JoinEdge(DIM, DIM2, DIM_BRIDGE, DIM2_PK, "left"),
        ),
        pk_column_ids=frozenset({DIM_PK, DIM2_PK, DIM_BRIDGE}),
        table_id_by_column_id={
            FACT_FK: FACT, FACT_FK2: FACT,
            DIM_PK: DIM, DIM_BRIDGE: DIM, DIM2_PK: DIM2,
        },
    )
    # 3 edges over 4 relations passes a whole-plan forest bound, and the
    # core walk cannot see the cycle either (its BFS skips the edge whose
    # endpoints are both already visited). Only the PER-COMPONENT tree test
    # catches it. The query spans every relation so nothing else can refuse.
    assert population_proven(
        graph=graph, query_table_ids={FACT, DIM, DIM2, island},
    ) is False


# ---------------------------------------------------------------------------
# Reviewer R3: Q must never name a relation the REAL plan does not join.
#
# ``_query_plan_table_ids`` is the proof's only bridge from a BoundQuery to the
# relation set, and the whole soundness argument rests on it being an
# UNDER-estimate: a table wrongly added to Q is silently removed from ``extra``
# and never tested for lossless attachment, which re-opens Bug-8580 itself.
# ---------------------------------------------------------------------------

_H_FACT, _H_D1, _H_D2 = "h-fact", "h-d1", "h-d2"
_H_AMT, _H_FK1, _H_FK2, _H_D1_PK, _H_D2_PK = (
    "h-amt", "h-fk1", "h-fk2", "h-d1-pk", "h-d2-pk",
)


def _hidden_filter_graph() -> ModelJoinGraph:
    """fact --INNER--> d1 (LOSSY) and fact --left(preserves fact)--> d2."""
    return ModelJoinGraph(
        table_ids=frozenset({_H_FACT, _H_D1, _H_D2}),
        edges=(
            JoinEdge(_H_FACT, _H_D1, _H_FK1, _H_D1_PK, "inner"),
            JoinEdge(_H_FACT, _H_D2, _H_FK2, _H_D2_PK, "left"),
        ),
        pk_column_ids=frozenset({_H_D1_PK, _H_D2_PK}),
        table_id_by_column_id={
            _H_AMT: _H_FACT, _H_FK1: _H_FACT, _H_FK2: _H_FACT,
            _H_D1_PK: _H_D1, _H_D2_PK: _H_D2,
        },
    )


def test_filter_on_a_name_absent_from_the_dimension_map_never_widens_q():
    """A filter the matcher cannot resolve EXACTLY must fail closed, not guess.

    ``binder.py`` canonicalises ``LogicalFilter.dimension_name`` into
    ``dimension_map`` — which IS ``resolved_dimensions_by_name`` — for every
    visible dimension, so the exact lookup always hits for those. The name only
    misses when the binder bound the filter to a HIDDEN dimension or a MEASURE,
    and those live outside ``dimension_map`` entirely. Resolving such a name by
    case-folding onto a visible dimension binds Q to the WRONG relation.

    Traced end to end (rendered through ``_build_joined_from_clause`` and run on
    SQLite, 3 fact rows of 100/200/300 where only the first matches ``d1``):

        pocket materialise : fact INNER JOIN d1 LEFT JOIN d2   -> SUM 100
        query's own plan   : fact LEFT JOIN d2                 -> SUM 600

    i.e. exactly the Bug-8580 defect, re-opened through name resolution.
    """
    graph = _hidden_filter_graph()
    meas = types.SimpleNamespace(name="amount", source_column_id=_H_AMT)
    # The model's visible dimension map holds "Code" (on d1). The binder bound
    # the WHERE to the HIDDEN dimension "code" (on d2), whose name is absent
    # from that map.
    visible_code = types.SimpleNamespace(name="Code", source_column_id=_H_D1_PK)
    bq = types.SimpleNamespace(
        resolved_dimensions=[],
        resolved_measures=[meas],
        resolved_filters=[types.SimpleNamespace(dimension_name="code")],
        resolved_dimensions_by_name={"Code": visible_code},
    )

    q = _query_plan_table_ids(bq, graph, {})
    assert q != {_H_FACT, _H_D1}, (
        "the filter was case-folded onto the visible dimension 'Code' (d1), so "
        "Q names a relation the query's own plan never joins; d1's lossy INNER "
        "edge then drops out of `extra` and is never proven"
    )
    # Unknown is UNPROVEN, never 'no extra relation'.
    assert q is None or q == {_H_FACT}
    assert population_proven(graph=graph, query_table_ids=q) is False


def test_query_plan_table_ids_still_resolves_an_exact_visible_filter():
    """Guard against over-correction: the ordinary case must still resolve."""
    graph = _hidden_filter_graph()
    meas = types.SimpleNamespace(name="amount", source_column_id=_H_AMT)
    code = types.SimpleNamespace(name="code", source_column_id=_H_D2_PK)
    bq = types.SimpleNamespace(
        resolved_dimensions=[],
        resolved_measures=[meas],
        resolved_filters=[types.SimpleNamespace(dimension_name="code")],
        resolved_dimensions_by_name={"code": code},
    )
    assert _query_plan_table_ids(bq, graph, {}) == {_H_FACT, _H_D2}


# ---------------------------------------------------------------------------
# Reviewer R3: exhaustive EXECUTED proof of the root/order-independence claim.
#
# The single hand-built shape in ``test_population_proof_is_root_independent``
# pins one counterexample family. This enumerates EVERY labelled tree over three
# relations, every modeller orientation, every join-type assignment, and every
# query subset; for each verdict of PROVEN it renders the real FROM clause from
# every possible root of BOTH plans and compares the executed row multiset.
# Runs in well under a second.
# ---------------------------------------------------------------------------

_X_UNIQUE_KEYS = [1, 2, 9]   # distinct -> honours a declared sole primary key
_X_DUP_KEYS = [1, 1, 2, 9]   # duplicated -> exercises fan-out inside Q


def _x_build(edges, orient, join_types):
    """Physical model + graph for one 3-relation shape."""
    n = 3
    tables, columns, col_tbl = {}, {}, {}
    table_cols = {i: ["v"] for i in range(n)}
    for i in range(n):
        tid = f"x{i}"
        tables[tid] = types.SimpleNamespace(
            id=tid, physical_name=f"x{i}", alias=f"q{i}", table_type="fact")
        columns[f"x{i}_v"] = types.SimpleNamespace(
            id=f"x{i}_v", model_table_id=tid, column_name="v", data_type="int")
        col_tbl[f"x{i}_v"] = tid
    joins, edge_objs, join_cols = [], [], {i: [] for i in range(n)}
    for k, ((u0, v0), flip, jt) in enumerate(zip(edges, orient, join_types)):
        u, v = (v0, u0) if flip else (u0, v0)
        lcid, rcid = f"x{u}_e{k}a", f"x{v}_e{k}b"
        for cid, tt, nm in ((lcid, u, f"e{k}a"), (rcid, v, f"e{k}b")):
            columns[cid] = types.SimpleNamespace(
                id=cid, model_table_id=f"x{tt}", column_name=nm, data_type="int")
            col_tbl[cid] = f"x{tt}"
            table_cols[tt].append(nm)
            join_cols[tt].append(cid)
        joins.append(types.SimpleNamespace(
            id=f"xj{k}", left_table_id=f"x{u}", left_column_id=lcid,
            right_table_id=f"x{v}", right_column_id=rcid, join_type=jt))
        edge_objs.append(JoinEdge(f"x{u}", f"x{v}", lcid, rcid, jt))
    # Only a relation with exactly ONE join column can be a rule-6 "extra", and
    # only such a column is declared (and populated) unique.
    pk_ids = {join_cols[i][0] for i in range(n) if len(join_cols[i]) == 1}
    unique = {i for i in range(n) if len(join_cols[i]) == 1}
    graph = ModelJoinGraph(
        table_ids=frozenset(f"x{i}" for i in range(n)),
        edges=tuple(edge_objs), pk_column_ids=frozenset(pk_ids),
        table_id_by_column_id=col_tbl)
    return tables, columns, joins, graph, table_cols, unique


def _x_db(table_cols, unique):
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    for i, cols in table_cols.items():
        cur.execute(f"create table x{i} ({', '.join(c + ' int' for c in cols)})")
        keys = _X_UNIQUE_KEYS if i in unique else _X_DUP_KEYS
        rows = []
        for r, k in enumerate(keys):
            rows.append(tuple(
                (i * 10 + r + 1) if c == "v"
                else (k if cols.index(c) % 2 else ((k + r) % 3) + 1)
                for c in cols))
        cur.executemany(
            f"insert into x{i} values ({','.join('?' * len(cols))})", rows)
    db.commit()
    return db


def _x_rows(db, from_clause, q, tables):
    proj = ", ".join(f"{tables[t].alias}.v" for t in sorted(q))
    return sorted(
        db.execute(f"SELECT {proj} FROM {from_clause}".replace('"', ""))
        .fetchall(),
        key=repr,
    )


@pytest.mark.skipif(
    tuple(int(p) for p in sqlite3.sqlite_version.split(".")[:2]) < (3, 39),
    reason="reverse-rooted plans render RIGHT/FULL JOIN, which need SQLite >= 3.39",
)
def test_every_three_relation_plan_that_is_proven_is_executed_root_independent():
    """PROVEN must mean the pocket's rows ARE the query's rows, for real.

    Exhaustive over all three distinct labelled 3-relation trees, both modeller
    orientations per edge, all four join types per edge, and all seven query
    subsets: 1344 verdicts, of which 156 are PROVEN. Every PROVEN one is
    rendered from all three roots of the pocket plan and every root of the
    query plan and executed; all renderings must agree exactly.
    """
    import itertools

    # The three distinct labelled trees on {0,1,2}, by which relation is the
    # middle of the path.
    shapes = [[(0, 1), (1, 2)], [(0, 1), (0, 2)], [(0, 2), (1, 2)]]
    proven_seen = 0
    for edges in shapes:
        for orient in itertools.product((False, True), repeat=2):
            for join_types in itertools.product(
                ("inner", "left", "right", "full"), repeat=2,
            ):
                tables, columns, joins, graph, table_cols, unique = _x_build(
                    edges, orient, join_types)
                all_t = [f"x{i}" for i in range(3)]
                db = None
                for size in (1, 2, 3):
                    for q in itertools.combinations(all_t, size):
                        if not population_proven(
                            graph=graph, query_table_ids=set(q),
                        ):
                            continue
                        proven_seen += 1
                        if db is None:
                            db = _x_db(table_cols, unique)
                        renderings = {}
                        for base in all_t:
                            fc = _build_joined_from_clause(
                                base_table_id=base,
                                required_table_ids=set(all_t), joins=joins,
                                tables_by_id=tables, columns_by_id=columns,
                                alias_by_table_id={
                                    t: tables[t].alias for t in all_t},
                                connector="postgresql")
                            assert fc is not None
                            renderings[("pocket", base)] = _x_rows(
                                db, fc, q, tables)
                        for base in q:
                            fc = _build_joined_from_clause(
                                base_table_id=base, required_table_ids=set(q),
                                joins=joins, tables_by_id=tables,
                                columns_by_id=columns,
                                alias_by_table_id={
                                    t: tables[t].alias for t in q},
                                connector="postgresql")
                            if fc is not None:
                                renderings[("query", base)] = _x_rows(
                                    db, fc, q, tables)
                        reference = next(iter(renderings.values()))
                        for key, rows in renderings.items():
                            assert rows == reference, (
                                f"population_proven said PROVEN for shape "
                                f"{edges} orient={orient} types={join_types} "
                                f"Q={q}, but rendering {key} produced "
                                f"{len(rows)} rows against {len(reference)} "
                                f"for the reference plan"
                            )
    assert proven_seen == 156, (
        f"expected 156 PROVEN verdicts over the 3-relation matrix, got "
        f"{proven_seen} — the proof's admittance surface moved; re-derive the "
        f"executed comparison rather than editing this number"
    )


# ---------------------------------------------------------------------------
# Reviewer R4: the lossless-attachment leg over NULL-BEARING join keys.
#
# Rule 6 admits an elided relation on the claim that a row-preserving edge onto
# that relation's SOLE declared primary key matches AT MOST ONE row per kept
# row, so the kept side's cardinality is unchanged. Every executed case above
# populates join keys with non-NULL integers. NULL is exactly where that claim
# could break down differently between the two plans: SQL `=` never matches a
# NULL, so a NULL foreign key on the kept side and a NULL primary key on the
# elided side both produce a non-match that a LEFT/RIGHT join still has to
# preserve. Pin it with real execution.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    tuple(int(p) for p in sqlite3.sqlite_version.split(".")[:2]) < (3, 39),
    reason="reverse-rooted plans render RIGHT/FULL JOIN, which need SQLite >= 3.39",
)
@pytest.mark.parametrize("edge_type", ["left", "right"])
def test_lossless_attachment_holds_when_join_keys_are_null(edge_type):
    """A proven elided relation must not change the kept rows over NULL keys."""
    # kept = x0 (the queried relation), extra = x1 (elided, sole declared PK).
    # "left"  -> modeller wrote x0 LEFT  JOIN x1, preserving x0.
    # "right" -> modeller wrote x1 RIGHT JOIN x0, preserving x0 as well.
    lt, rt = ("x0", "x1") if edge_type == "left" else ("x1", "x0")
    lc, rc = (f"{lt}_fk", f"{rt}_pk") if edge_type == "left" else (f"{lt}_pk", f"{rt}_fk")
    col_tbl = {"x0_v": "x0", "x1_v": "x1", lc: lt, rc: rt}
    pk_id = "x1_pk"
    graph = ModelJoinGraph(
        table_ids=frozenset({"x0", "x1"}),
        edges=(JoinEdge(lt, rt, lc, rc, edge_type),),
        pk_column_ids=frozenset({pk_id}),
        table_id_by_column_id=col_tbl,
    )
    assert population_proven(graph=graph, query_table_ids={"x0"}) is True

    tables = {
        t: types.SimpleNamespace(id=t, physical_name=t, alias=t, table_type="fact")
        for t in ("x0", "x1")
    }
    columns = {
        "x0_v": types.SimpleNamespace(id="x0_v", model_table_id="x0", column_name="v", data_type="int"),
        "x1_v": types.SimpleNamespace(id="x1_v", model_table_id="x1", column_name="v", data_type="int"),
        "x0_fk": types.SimpleNamespace(id="x0_fk", model_table_id="x0", column_name="fk", data_type="int"),
        "x1_pk": types.SimpleNamespace(id="x1_pk", model_table_id="x1", column_name="pk", data_type="int"),
    }
    joins = [types.SimpleNamespace(
        id="j0", left_table_id=lt, left_column_id=lc,
        right_table_id=rt, right_column_id=rc, join_type=edge_type)]

    db = sqlite3.connect(":memory:")
    db.execute("create table x0 (v int, fk int)")
    db.execute("create table x1 (v int, pk int)")
    # NULL fk (no match possible), a matching fk, and an fk with no counterpart.
    db.executemany("insert into x0 values (?,?)", [(10, None), (20, 1), (30, 7)])
    # A NULL pk must never match anything, not even the NULL fk above.
    db.executemany("insert into x1 values (?,?)", [(100, 1), (200, None)])

    rendered = {}
    for base in ("x0", "x1"):
        fc = _build_joined_from_clause(
            base_table_id=base, required_table_ids={"x0", "x1"}, joins=joins,
            tables_by_id=tables, columns_by_id=columns,
            alias_by_table_id={t: t for t in ("x0", "x1")}, connector="postgresql")
        assert fc is not None
        rendered[base] = sorted(
            db.execute(f"SELECT x0.v FROM {fc}".replace(chr(34), "")).fetchall(),
            key=repr)
    query_only = sorted(db.execute("SELECT v FROM x0").fetchall(), key=repr)

    # Every kept row survives exactly once, whichever plan and whichever root.
    assert query_only == [(10,), (20,), (30,)]
    for base, rows in rendered.items():
        assert rows == query_only, (
            f"population_proven admitted this pocket, but rooting the "
            f"materialise plan at {base} produced {rows} instead of "
            f"{query_only} — the elided relation changed the kept rows")
    db.close()
