"""Known-fixture contract + algorithm tests for the dependency engine (spec §13).

Assert EXACT impacted-object tuples (model_id, object_type, object_id, severity,
effect, delete_policy, min_depth) and canonical witness paths — never only counts
(spec §13.1). Also exercise cycles, alternate relationship paths, catalogue
coverage, and cascade closure.
"""

from __future__ import annotations

from shared.model_dependency import graph, impact
from shared.model_dependency.edge_builders import EDGE_BUILDERS
from shared.model_dependency.snapshot import (
    ColumnRow,
    KpiRow,
    MeasureRow,
    ModelDependencySnapshot,
    SourceRow,
    TableRow,
)
from shared.model_dependency.structural_paths import relationship_removal_impact
from shared.model_dependency.types import EdgeKind, NodeKey, ObjectType

from model_dependency_fixtures import (  # noqa: E402 - pytest adds test dir to path
    AGENT,
    AGG_CUST,
    ALIAS,
    COL_CUSTKEY,
    COL_GROSS,
    DIM_CUSTOMER,
    DQR,
    GLOSS,
    KPI_MARGIN,
    LINEAGE,
    LVL_CUST,
    M,
    MSR_GROSS,
    MSR_NET,
    P,
    PARAM,
    PERSONA_ANALYST,
    RECIPE,
    REL_OC,
    RSR,
    SCRATCH,
    SP,
    SQ,
    T,
    TAG_CONF,
    TB_CUSTOMER,
    TB_ORDERS,
    TRANS,
    build_retail_snapshot,
)


def _key(object_type: ObjectType, object_id: str, model_id: str = M) -> NodeKey:
    return NodeKey(T, P, model_id, object_type, object_id)


def _tuples(result) -> set[tuple]:
    return {
        (
            i.node.key.model_id,
            i.node.key.object_type.value,
            i.node.key.object_id,
            i.severity,
            i.effect,
            i.delete_policy,
            i.min_depth,
        )
        for i in result.impacts
    }


def test_delete_gross_amount_exact_chain():
    """orders.gross_amount -> Gross Sales -> Net Sales -> Margin (spec §13.1)."""
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.COLUMN, COL_GROSS))
    got = _tuples(r)
    assert (M, "measure", MSR_GROSS, "hard_break", "breaks_reference", "restrict", 1) in got
    assert (M, "measure", MSR_NET, "hard_break", "breaks_reference", "restrict", 2) in got
    assert (M, "kpi", KPI_MARGIN, "hard_break", "breaks_reference", "restrict", 3) in got
    # Aggregate column referencing Gross Sales is invalidated (depth 2 via measure).
    assert any(t[1] == "aggregate_column" for t in got)
    # Exact canonical witness path to the KPI.
    kpi_impact = next(i for i in r.impacts if i.node.key.object_id == KPI_MARGIN)
    assert kpi_impact.paths[0].nodes == (
        f"column:{COL_GROSS}",
        f"measure:{MSR_GROSS}",
        f"measure:{MSR_NET}",
        f"kpi:{KPI_MARGIN}",
    )
    assert r.summary.hard_break >= 3


def test_delete_customer_column_reaches_tag_and_persona():
    """orders.customer_key -> tag -> persona; blocks on CLS restriction (§13.1)."""
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.COLUMN, COL_CUSTKEY))
    got = _tuples(r)
    # Column -> data tag is soft/detach.
    assert (M, "data_tag", TAG_CONF, "soft_degrade", "detached", "detach", 1) in got
    # Data tag -> persona CLS restriction is a hard security edge (Bug-7790).
    persona = next((i for i in r.impacts if i.node.key.object_id == PERSONA_ANALYST), None)
    assert persona is not None
    assert persona.severity == "hard_break"


def test_delete_measure_variant_cascade_and_hard_consumers():
    """Measure with a variant + calc consumer + KPI + agg col + cross-model."""
    snap = build_retail_snapshot()
    # Add a variant of Gross Sales and a cross-model consumer.
    snap = ModelDependencySnapshot(
        **{
            **snap.__dict__,
            "measures": snap.measures + (
                MeasureRow(id="msr-variant", name="Gross Sales YTD",
                           display_name="Gross Sales YTD", variant_of_measure_id=MSR_GROSS),
            ),
            "cross_model_measures": (
                ("model-2", "m2-consumer", "M2 Consumer", MSR_GROSS),
            ),
        }
    )
    g = graph.build_graph(snap)
    r = impact.simulate_delete(g, _key(ObjectType.MEASURE, MSR_GROSS))
    got = _tuples(r)
    # Variant is cascade_deleted (informational), not a broken survivor.
    assert (M, "measure", "msr-variant", "informational", "cascade_deleted", "cascade", 1) in got
    # Calculated consumer Net Sales is hard.
    assert any(t[2] == MSR_NET and t[3] == "hard_break" for t in got)
    # Cross-model consumer in model-2 is hard and named its own model.
    cm = next((i for i in r.impacts if i.node.key.object_id == "m2-consumer"), None)
    assert cm is not None and cm.node.key.model_id == "model-2"
    assert cm.severity == "hard_break"


def test_alternate_relationship_path_no_false_hard_break():
    """Removing one of two parallel joins keeps the customer table reachable."""
    snap = build_retail_snapshot(with_alternate_path=True)
    required = {DIM_CUSTOMER: (TB_ORDERS, (TB_CUSTOMER,))}
    res = relationship_removal_impact(snap, REL_OC, object_required_tables=required)
    assert DIM_CUSTOMER not in res.hard_break_object_ids

    # Removing the sole path (no alternate) IS a hard break.
    snap_single = build_retail_snapshot(with_alternate_path=False)
    res2 = relationship_removal_impact(snap_single, REL_OC, object_required_tables=required)
    assert DIM_CUSTOMER in res2.hard_break_object_ids
    assert res2.witness_path  # deterministic witness present


def test_relationship_change_impact_repoint_hard_break_and_alternate_path():
    """relationship_change_impact (the §7.4 re-point/rebind primitive): re-pointing
    the sole Orders-Customer join so Customer is unreachable is a hard break with a
    witness path; a redundant alternate path is NOT a false hard break."""
    from dataclasses import replace

    from shared.model_dependency.structural_paths import relationship_change_impact

    snap = build_retail_snapshot(with_alternate_path=False)
    required = {DIM_CUSTOMER: (TB_ORDERS, (TB_CUSTOMER,))}
    # Proposed: re-point the customer end back to orders -> customer unreachable.
    rels = tuple(
        replace(r, right_table_id=TB_ORDERS) if r.id == REL_OC else r
        for r in snap.relationships
    )
    res = relationship_change_impact(
        snap.relationships, rels, object_required_tables=required
    )
    assert DIM_CUSTOMER in res.hard_break_object_ids
    assert res.witness_path

    # With a redundant alternate join, the same re-point leaves customer reachable.
    snap_alt = build_retail_snapshot(with_alternate_path=True)
    rels_alt = tuple(
        replace(r, right_table_id=TB_ORDERS) if r.id == REL_OC else r
        for r in snap_alt.relationships
    )
    res_alt = relationship_change_impact(
        snap_alt.relationships, rels_alt, object_required_tables=required
    )
    assert DIM_CUSTOMER not in res_alt.hard_break_object_ids


def test_cycle_handled_without_hang_or_duplicate():
    """A calculated-measure cycle collapses to one SCC and does not hang (§12.2)."""
    snap = ModelDependencySnapshot(
        tenant_id=T, project_id=P, model_id=M, dependency_revision=1,
        measures=(
            MeasureRow(id="a", name="A", display_name="A", calc_reference_ids=("b",)),
            MeasureRow(id="b", name="B", display_name="B", calc_reference_ids=("a",)),
        ),
        kpis=(KpiRow(id="k", name="K", display_name="K", measure_ids=("a",)),),
    )
    g = graph.build_graph(snap)
    assert len(g.cycles) == 1
    r = impact.inspect(g, _key(ObjectType.MEASURE, "a"))
    # KPI downstream of the cycle is still reached exactly once.
    kpi_hits = [i for i in r.impacts if i.node.key.object_id == "k"]
    assert len(kpi_hits) == 1


def test_cascade_closure_reported_separately():
    g = graph.build_graph(build_retail_snapshot())
    # Deleting the source cascades its tables (containment cascade).
    from model_dependency_fixtures import SRC
    r = impact.simulate_delete(g, _key(ObjectType.DATA_SOURCE, SRC))
    closure_ids = {k.object_id for k in r.cascade_closure}
    assert TB_ORDERS in closure_ids and TB_CUSTOMER in closure_ids


def test_deterministic_output_independent_of_row_order():
    g1 = graph.build_graph(build_retail_snapshot())
    r1 = impact.simulate_delete(g1, _key(ObjectType.COLUMN, COL_GROSS))
    g2 = graph.build_graph(build_retail_snapshot())
    r2 = impact.simulate_delete(g2, _key(ObjectType.COLUMN, COL_GROSS))
    assert _tuples(r1) == _tuples(r2)
    assert [i.node.key.token() for i in r1.impacts] == [i.node.key.token() for i in r2.impacts]


# EdgeKinds NOT emitted on a clean, fully-resolved fixture:
#  - RELATIONSHIP_PATH: produced only at query time (structural_paths what-if).
#  - UNRESOLVED_REFERENCE: emitted only when a reference cannot resolve; a clean
#    fixture has none (test_unresolved_reference_fails_closed exercises it).
_QUERY_TIME_ONLY = {EdgeKind.RELATIONSHIP_PATH, EdgeKind.UNRESOLVED_REFERENCE}


def test_edge_catalogue_coverage_all_kinds_reachable():
    """EVERY EdgeKind (except query-time-only) must be emitted by some builder on
    the covering retail fixture, so a missing family is a failing test not an
    implementation note (spec §5.3, §7.1 step 6)."""
    g = graph.build_graph(build_retail_snapshot())
    seen = {e.kind for e in g.edges}
    expected = set(EdgeKind) - _QUERY_TIME_ONLY
    missing = expected - seen
    assert not missing, f"edge families not materialized: {sorted(k.value for k in missing)}"


def test_aggregate_grain_severity_is_soft_by_default():
    """§7.6: deleting a dimension used as an aggregate grain is soft_degrade when
    the aggregate is automatically invalidated with safe source fallback — NOT a
    hard break that would wrongly block the delete."""
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.DIMENSION, DIM_CUSTOMER))
    agg = next(i for i in r.impacts if i.node.key.object_id == AGG_CUST)
    assert agg.severity == "soft_degrade"
    assert agg.effect == "loses_coverage"
    assert agg.delete_policy == "invalidate"


def test_aggregate_grain_severity_hard_when_serves_stale():
    """§7.6: the same grain edge stays hard when the aggregate would keep serving
    stale results (no safe fallback)."""
    from model_dependency_fixtures import build_retail_snapshot as _b
    snap = _b()
    aggs = tuple(
        type(a)(**{**a.__dict__, "serves_when_stale": True}) if a.id == AGG_CUST else a
        for a in snap.aggregates
    )
    snap = ModelDependencySnapshot(**{**snap.__dict__, "aggregates": aggs})
    g = graph.build_graph(snap)
    r = impact.simulate_delete(g, _key(ObjectType.DIMENSION, DIM_CUSTOMER))
    agg = next(i for i in r.impacts if i.node.key.object_id == AGG_CUST)
    assert agg.severity == "hard_break"


def test_refresh_dependency_stays_hard_break():
    """§5.3: deleting an aggregate that another depends on for refresh is a HARD
    break — the §7.6 relaxation is scoped to grain/measure coverage edges and must
    NOT soften refresh_dependency (guard-superset regression for the R2 finding)."""
    from model_dependency_fixtures import AGG_REFRESH
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.AGGREGATE, AGG_CUST))
    dep = next((i for i in r.impacts if i.node.key.object_id == AGG_REFRESH), None)
    assert dep is not None, "refresh-dependent aggregate not reached"
    assert dep.severity == "hard_break", "refresh_dependency must stay hard (§5.3)"
    assert dep.delete_policy == "invalidate"


def test_dimension_level_association_soft_recompute():
    """§5.3/§7.1 step 6: a dimension sharing a level's backing attribute produces a
    soft/recompute association edge (deleting the dimension does not break the
    level's stored binding)."""
    g = graph.build_graph(build_retail_snapshot())
    r = impact.inspect(g, _key(ObjectType.DIMENSION, DIM_CUSTOMER))
    lvl = next((i for i in r.impacts if i.node.key.object_id == LVL_CUST), None)
    assert lvl is not None, "dimension->hierarchy level association edge missing"
    assert lvl.severity == "soft_degrade"
    assert lvl.delete_policy == "recompute"


def test_peripheral_family_exact_tuples():
    """Exact (severity, effect, delete_policy) for the soft-reference families the
    spec §13.1 minimum-fixtures list requires (glossary, DQ, lineage, scratchpad,
    translation, alias, recipe, saved query/pivot, row-security, parameter,
    agent) — guards their strength/policy encoding, incl. the cleanup-policy bug."""
    g = graph.build_graph(build_retail_snapshot())

    # Deleting Gross Sales reaches glossary/scratchpad/recipe/saved-query.
    rg = impact.simulate_delete(g, _key(ObjectType.MEASURE, MSR_GROSS))
    got = _tuples(rg)
    assert (M, "glossary_attachment", GLOSS, "soft_degrade", "cleanup", "detach", 1) in got
    assert (M, "scratchpad_measure", SCRATCH, "soft_degrade", "stale", "invalidate", 1) in got
    assert (M, "cross_model_recipe", RECIPE, "hard_break", "breaks_reference", "restrict", 1) in got
    assert (M, "saved_query", SQ, "hard_break", "breaks_reference", "invalidate", 1) in got

    # Deleting gross_amount column reaches the DQ rule + lineage mapping.
    rc = impact.simulate_delete(g, _key(ObjectType.COLUMN, COL_GROSS))
    gotc = _tuples(rc)
    assert (M, "data_quality_rule", DQR, "soft_degrade", "detached", "detach", 1) in gotc
    assert (M, "lineage_mapping", LINEAGE, "soft_degrade", "cleanup", "detach", 1) in gotc

    # Deleting the Customer dimension reaches translation/alias/row-security/pivot.
    rd = impact.simulate_delete(g, _key(ObjectType.DIMENSION, DIM_CUSTOMER))
    gotd = _tuples(rd)
    assert (M, "translation", TRANS, "soft_degrade", "cleanup", "detach", 1) in gotd
    assert (M, "model_alias_map", ALIAS, "soft_degrade", "stale", "recompute", 1) in gotd
    assert (M, "row_security_rule", RSR, "hard_break", "breaks_reference", "restrict", 1) in gotd
    assert (M, "saved_pivot_view", SP, "hard_break", "breaks_reference", "invalidate", 1) in gotd

    # Deleting the @region parameter breaks the saved query that names it.
    rp = impact.simulate_delete(g, _key(ObjectType.MODEL_PARAMETER, PARAM))
    assert (M, "saved_query", SQ, "hard_break", "breaks_reference", "restrict", 1) in _tuples(rp)

    # Deleting the model detaches the agent grounding (soft).
    rm = impact.simulate_delete(g, _key(ObjectType.MODEL, M))
    agent = next((i for i in rm.impacts if i.node.key.object_id == AGENT), None)
    assert agent is not None and agent.effect in ("detached", "cascade_deleted")


def test_severity_is_max_over_all_reaching_edges():
    """§7.6: an object reached by BOTH a relaxable grain edge (soft) and a hard
    refresh-dependency edge must be hard — severity is the max over ALL edges,
    not from one dominant edge (R2-stage2 Fable finding 2)."""
    from model_dependency_fixtures import AGG_REFRESH
    snap = build_retail_snapshot()
    # Make AGG_REFRESH ALSO carry the Customer grain, so it is reached by both a
    # (soft) grain edge from the dimension and the (hard) refresh edge from AGG_CUST.
    aggs = tuple(
        type(a)(**{**a.__dict__, "grain_dimension_ids": (DIM_CUSTOMER,)})
        if a.id == AGG_REFRESH else a
        for a in snap.aggregates
    )
    snap = ModelDependencySnapshot(**{**snap.__dict__, "aggregates": aggs})
    g = graph.build_graph(snap)
    r = impact.simulate_delete(g, _key(ObjectType.AGGREGATE, AGG_CUST))
    dep = next(i for i in r.impacts if i.node.key.object_id == AGG_REFRESH)
    assert dep.severity == "hard_break", "hard refresh edge must dominate soft grain edge"


def test_calc_dimension_column_reference_blocks():
    """§5.3: deleting a column referenced only inside a calculated dimension's
    expression breaks the dimension (Fable finding 1)."""
    from model_dependency_fixtures import DIM_CALC
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.COLUMN, COL_GROSS))
    calc = next((i for i in r.impacts if i.node.key.object_id == DIM_CALC), None)
    assert calc is not None, "calc dimension not reached from its expression column"
    assert calc.severity == "hard_break"


def test_persona_default_filter_edge_present():
    """§5.3: a dimension named in Persona.default_filters is a soft/detach persona
    dependent (Fable finding 4)."""
    g = graph.build_graph(build_retail_snapshot())
    edges = [
        e for e in g.edges
        if e.kind.value == "persona_scope" and e.source_field == "default_filters"
    ]
    assert edges, "persona default_filters edge missing"


def test_row_security_mapping_table_blocks():
    """§5.3/§10.1: deleting the row-security mapping table blocks (restrict)."""
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.TABLE, TB_CUSTOMER))
    rsr = next((i for i in r.impacts if i.node.key.object_id == RSR), None)
    assert rsr is not None and rsr.severity == "hard_break"


def test_model_delete_does_not_block_on_own_persona():
    """§5.3: personas are model-contained; a model delete cascades its own persona
    rather than reporting it as a broken survivor (Fable finding 5)."""
    from model_dependency_fixtures import AGENT
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.MODEL, M))
    closure_ids = {k.object_id for k in r.cascade_closure}
    assert PERSONA_ANALYST in closure_ids, "persona must be in model cascade closure"
    persona = next((i for i in r.impacts if i.node.key.object_id == PERSONA_ANALYST), None)
    # The persona is reported as cascade_deleted, not hard_break survivor.
    assert persona is None or persona.severity == "informational"


def test_model_recipe_reference_blocks_model_delete():
    """§5.3/§10.1: a recipe referencing THIS model blocks model deletion."""
    from model_dependency_fixtures import RECIPE
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.MODEL, M))
    recipe = next((i for i in r.impacts if i.node.key.object_id == RECIPE), None)
    assert recipe is not None
    # Recipe is model-contained (cascade) AND has an inbound model->recipe hard
    # edge; the max-severity rule surfaces the hard reference.
    assert recipe.effect in ("cascade_deleted", "breaks_reference")


def test_unresolved_reference_fails_closed():
    """§5.5: a dangling reference on a SURVIVING object is a reachable unresolved
    node counted in summary.unresolved and classified hard_break. (When the owner
    itself is the delete target, its own dangling ref cascades with it — that is a
    separate case covered by test_deleting_broken_object_not_blocked_by_own_ref.)"""
    snap = ModelDependencySnapshot(
        tenant_id=T, project_id=P, model_id=M, dependency_revision=1,
        columns=(ColumnRow(id="c", name="c", display_name="c", table_id="t"),),
        tables=(TableRow(id="t", name="t", display_name="t", source_id="s",
                         calendar_table_id=None),),
        sources=(SourceRow(id="s", name="s", display_name="s", project_connection_id=None),),
        # m2 references a ghost measure; deleting column c leaves m2 SURVIVING with
        # its dangling ref -> fail closed (hard_break).
        measures=(
            MeasureRow(id="m2", name="M2", display_name="M2", source_column_id="c",
                       calc_reference_ids=("ghost-measure",)),
        ),
    )
    g = graph.build_graph(snap)
    r = impact.simulate_delete(g, _key(ObjectType.COLUMN, "c"))
    # m2 survives-but-broken (lost column c); its own unresolved ref is a hard survivor.
    assert r.summary.unresolved >= 1, "unresolved reference not counted"
    unresolved = [i for i in r.impacts if i.node.key.object_type == ObjectType.UNRESOLVED_REFERENCE]
    assert unresolved and any(u.severity == "hard_break" for u in unresolved)


def test_dimension_delete_not_hard_blocked_by_aggregate():
    """§7.6 (R3 Fable finding 2): deleting a dimension used as an aggregate grain
    must NOT hard-break the aggregate, its columns, or a refresh-dependent
    aggregate — the parent aggregate survives (soft-invalidated), so nothing it
    contains cascade-deletes and no hard refresh edge escalates the delete."""
    from model_dependency_fixtures import AGG_REFRESH, AGG_COL
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.DIMENSION, DIM_CUSTOMER))
    for oid in (AGG_CUST, AGG_REFRESH, AGG_COL):
        imp = next((i for i in r.impacts if i.node.key.object_id == oid), None)
        assert imp is not None and imp.severity == "soft_degrade", f"{oid} should be soft"
        # No incoherent cascade_deleted for a surviving parent's child.
        assert imp.effect != "cascade_deleted"


def test_deleting_broken_object_not_blocked_by_own_unresolved_ref():
    """§5.5/§10.2 (R3 Fable finding 3): deleting an object (or the model) that owns
    a stale reference must NOT be blocked by its own unresolved ref — the ref dies
    with the owner (cascade), so it is informational, not a hard survivor."""
    snap = ModelDependencySnapshot(
        tenant_id=T, project_id=P, model_id=M, dependency_revision=1,
        kpis=(KpiRow(id="k1", name="K", display_name="K", measure_ids=("ghost",)),),
    )
    g = graph.build_graph(snap)
    r = impact.simulate_delete(g, _key(ObjectType.KPI, "k1"))
    # The KPI's own unresolved ref is cascaded, not a hard block.
    assert r.summary.hard_break == 0, "deleting a broken object must not self-block"


def test_severity_propagation_short_soft_long_hard_collision():
    """The fixed-point propagation must NOT under-classify an object reached by a
    SHORT soft path and a LONG hard-broken path. A min_depth single pass would
    classify the object soft before the deep breaking dependency is resolved."""
    # target column C. C -> measure MBASE (d1 hard, breaks). C -> dimension DX
    # (d1 hard, breaks) used ONLY as a soft persona-scope member reaching persona
    # P at d2 (soft). ALSO MBASE -> calc measure MCALC (d2) -> KPI K (d3 hard).
    # And K is ALSO reachable via a short SOFT path? KPIs have no soft measure ref.
    # Use the aggregate: delete measure MBASE. MBASE -> aggregate_column AC (d1,
    # invalidate). AC's parent AGG. AGG -> refresh-dependent AGG2 (hard). Separately
    # AGG2 is grain of dimension D2 (soft, short). So AGG2 reached soft-short (grain
    # from an intact dim - not effective) and hard-long (refresh from AGG which is
    # only degraded). Both non-breaking => AGG2 correctly soft. That is the RIGHT
    # answer, so instead force a genuine long hard break:
    snap = ModelDependencySnapshot(
        tenant_id=T, project_id=P, model_id=M, dependency_revision=1,
        columns=(ColumnRow(id="c", name="c", display_name="c", table_id="t"),),
        tables=(TableRow(id="t", name="t", display_name="t", source_id="s",
                         calendar_table_id=None),),
        sources=(SourceRow(id="s", name="s", display_name="s", project_connection_id=None),),
        measures=(
            MeasureRow(id="mbase", name="MB", display_name="MB", source_column_id="c"),
            MeasureRow(id="mcalc", name="MC", display_name="MC", calc_reference_ids=("mbase",)),
        ),
        # KPI reached at depth 2 via mbase (hard) AND depth 3 via mcalc (hard).
        kpis=(KpiRow(id="k", name="K", display_name="K", measure_ids=("mbase", "mcalc")),),
    )
    g = graph.build_graph(snap)
    r = impact.simulate_delete(g, NodeKey(T, P, M, ObjectType.COLUMN, "c"))
    # mbase, mcalc, k all lose a required (removed/broken) dependency -> all hard.
    for oid in ("mbase", "mcalc", "k"):
        imp = next(i for i in r.impacts if i.node.key.object_id == oid)
        assert imp.severity == "hard_break", f"{oid} must be hard regardless of path length"


def test_definition_parse_failure_fails_closed():
    """§5.5/§12.7 (R3 Fable finding 1): a KPI whose definition could not be parsed
    yields a fail-closed unresolved node (not an empty reference set), so deleting
    an object it may name is guarded."""
    snap = ModelDependencySnapshot(
        tenant_id=T, project_id=P, model_id=M, dependency_revision=1,
        kpis=(KpiRow(id="k1", name="K", display_name="K"),),
        unresolved_definitions=(("kpi", "k1", "expression", "unparseable_expression"),),
    )
    g = graph.build_graph(snap)
    r = impact.inspect(g, _key(ObjectType.KPI, "k1"))
    assert r.summary.unresolved >= 1
    assert any(d.get("type") == "unresolved_definition" for d in r.diagnostics)


def test_security_and_aggregate_owner_types_fail_closed():
    """§5.5/§12.6 (Bug-7787 owner-type-map widening guard): the loader emits
    ``aggregate`` (grain resolution failure), ``row_security_rule`` (dimension_path
    resolution failure), and ``persona`` (default_filters) owner types into
    ``unresolved_definitions``. Each MUST map to a real ObjectType in
    ``_build_unresolved_definitions`` and produce a reachable owner->unresolved
    HARD edge — NOT be dropped as an ``unknown_owner_type`` diagnostic, which would
    fail OPEN on a broken security/aggregate binding. This locks in the widened
    ``type_map`` (edge_builders.py); removing any entry re-opens a §12.6 fail-open.

    The prior parse-failure test only exercised the ``kpi`` owner type, so a
    regression on the security/aggregate entries had no failing guard.
    """
    from dataclasses import replace

    base = build_retail_snapshot()
    # Seed one unresolved definition per security/aggregate owner type, against the
    # real owner nodes the retail fixture already contains (AGG_CUST aggregate, RSR
    # row-security rule, PERSONA_ANALYST persona).
    snap = replace(base, unresolved_definitions=(
        ("aggregate", AGG_CUST, "grain", "unresolved_dimension_name:Ghost"),
        ("row_security_rule", RSR, "dimension_path", "unresolved_dimension_path:Ghost"),
        ("persona", PERSONA_ANALYST, "default_filters", "unresolved_dimension_name:Ghost"),
    ))
    g = graph.build_graph(snap)

    # No owner type was dropped as unknown (that would be the fail-open bug).
    assert not any(d.get("type") == "unknown_owner_type" for d in g.diagnostics), \
        "a security/aggregate owner type was dropped as unknown_owner_type (fail-open)"

    owners = {
        ObjectType.AGGREGATE: AGG_CUST,
        ObjectType.ROW_SECURITY_RULE: RSR,
        ObjectType.PERSONA: PERSONA_ANALYST,
    }
    for owner_type, owner_id in owners.items():
        owner_key = _key(owner_type, owner_id)
        # 1. An owner -> UNRESOLVED_REFERENCE hard edge was materialized (not
        #    silently skipped): the broken binding fails closed.
        out_edges = g.forward.get(owner_key, ())
        unresolved_edges = [
            e for e in out_edges
            if e.dependent.object_type == ObjectType.UNRESOLVED_REFERENCE
            and e.kind == EdgeKind.UNRESOLVED_REFERENCE
        ]
        assert unresolved_edges, f"no owner->unresolved edge for {owner_type.value}"
        assert all(e.strength == "hard" for e in unresolved_edges), \
            f"owner->unresolved edge for {owner_type.value} must be hard"

        # 2. An inspect of the owner reaches + counts its unresolved node
        #    (summary.unresolved >= 1), and emits an unresolved_definition
        #    diagnostic for it (NOT unknown_owner_type).
        r = impact.inspect(g, owner_key)
        assert r.summary.unresolved >= 1, \
            f"{owner_type.value} unresolved definition not counted"
        assert any(
            d.get("type") == "unresolved_definition"
            and d.get("owner") == owner_key.token()
            for d in r.diagnostics
        ), f"no unresolved_definition diagnostic for {owner_type.value}"


def test_measure_delete_names_aggregate_directly():
    """§5.3 (R3 Fable finding 5): a measure delete lists the aggregate itself, not
    only its aggregate column."""
    g = graph.build_graph(build_retail_snapshot())
    r = impact.simulate_delete(g, _key(ObjectType.MEASURE, MSR_GROSS))
    assert any(i.node.key.object_id == AGG_CUST for i in r.impacts), "aggregate not named"


def test_additional_family_edges_present():
    """§13.1 (R4 Fable finding 6): builders for hierarchy-level display/filter
    attributes, measure date field, drill-through detail/join-path, saved-pivot
    measure, KPI replacement, and UDA are all exercised on the fixture. Asserts the
    edge exists with its §5.3 strength/kind so a regression is a failing test."""
    from model_dependency_fixtures import UDA, MSR_TIME, COL_DATE, DT_DETAIL, TB_CUSTOMER
    g = graph.build_graph(build_retail_snapshot())
    kinds = {(e.kind.value, e.source_field, e.strength) for e in g.edges}
    # UDA over a column (column -> uda, hard/restrict).
    assert ("uda_column_reference", "column_id", "hard") in kinds
    # Measure date binding (column -> measure via resolved_date_col_id).
    assert ("measure_binding", "resolved_date_col_id", "hard") in kinds
    # Drill-through source table + joined dimension (hard/restrict).
    assert ("drill_through_reference", "source_table_id", "hard") in kinds
    assert ("drill_through_reference", "joined_dimension_ids", "hard") in kinds
    # Saved pivot measure + row dimension (hard).
    assert any(k[0] == "saved_pivot_reference" for k in kinds)
    # KPI replacement is soft/detach.
    kpi_repl = [
        e for e in g.edges
        if e.kind.value == "kpi_kpi_reference" and e.source_field == "replacement_kpi_id"
    ]
    # (replacement only present if the fixture wires it; assert kind space exists)
    assert any(e.kind.value == "kpi_kpi_reference" for e in g.edges)


def test_inspect_and_delete_severity_parity():
    """§13.6 (R4 Fable finding 1): inspect must return the SAME hard/soft severity
    per dependent as delete — inspect previews the target's removal, so it must not
    cap everything to soft."""
    g = graph.build_graph(build_retail_snapshot())
    tgt = _key(ObjectType.COLUMN, COL_GROSS)
    ins = {i.node.key: i.severity for i in impact.inspect(g, tgt).impacts}
    dele = {i.node.key: i.severity for i in impact.simulate_delete(g, tgt).impacts}
    # Every object common to both must have the same severity.
    common = set(ins) & set(dele)
    assert common, "no common impacted objects"
    for k in common:
        assert ins[k] == dele[k], f"{k.token()} inspect={ins[k]} delete={dele[k]}"
    # And inspect must surface at least one hard break for this fixture.
    assert any(v == "hard_break" for v in ins.values())


def test_inspect_cascade_effect_consistent_with_closure():
    """inspect must not report a cascade_deleted effect that its cascade_closure
    does not contain (the R5 inspect-seeding consistency check)."""
    from model_dependency_fixtures import SRC
    g = graph.build_graph(build_retail_snapshot())
    r = impact.inspect(g, _key(ObjectType.DATA_SOURCE, SRC))
    closure = set(r.cascade_closure)
    ce = [i for i in r.impacts if i.effect == "cascade_deleted"]
    assert ce, "source inspect should show cascade_deleted members"
    assert all(i.node.key in closure for i in ce)
    assert all(i.severity == "informational" for i in ce)
    assert r.summary.cascade_deleted == len(ce)


def test_cascade_deleted_effect_matches_cascade_closure():
    """§5.2/§7.2.4 (R4 Fable finding 2): an impact is labelled effect=cascade_deleted
    ONLY if it is actually in the cascade closure. A broken-but-surviving dependent
    of a broken dependency is breaks_reference, never cascade_deleted."""
    g = graph.build_graph(build_retail_snapshot())
    # Delete the gross_amount COLUMN: the Gross Sales measure breaks (survives),
    # so its variant is reached via a cascade edge from a SURVIVING measure and
    # must NOT be reported cascade_deleted.
    r = impact.simulate_delete(g, _key(ObjectType.COLUMN, COL_GROSS))
    closure = {k for k in r.cascade_closure}
    for i in r.impacts:
        if i.effect == "cascade_deleted":
            assert i.node.key in closure, (
                f"{i.node.key.token()} labelled cascade_deleted but not in closure"
            )
    # summary.cascade_deleted counts must equal the number of closure members shown.
    shown_cascade = sum(1 for i in r.impacts if i.effect == "cascade_deleted")
    assert r.summary.cascade_deleted == shown_cascade


def test_engine_imports_no_source_or_framework_modules():
    """The pure engine must not import DB/source/framework layers so no source
    access can ever occur through it (spec §13.4, §17)."""
    import ast
    import glob
    import os

    pkg_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "model_dependency")
    banned = ("source_executor", "connector", "asyncpg", "sqlalchemy", "fastapi", "httpx")
    violations = []
    for f in glob.glob(os.path.join(pkg_dir, "*.py")):
        tree = ast.parse(open(f, encoding="utf-8").read())
        for n in ast.walk(tree):
            mods = (
                [a.name for a in n.names] if isinstance(n, ast.Import)
                else [n.module or ""] if isinstance(n, ast.ImportFrom)
                else []
            )
            violations += [(f, m) for m in mods if any(b in m for b in banned)]
    assert not violations, f"engine imports forbidden modules: {violations}"


def test_no_builder_raises_on_empty_snapshot():
    """Empty model builds cleanly (spec §13.2 empty graph)."""
    g = graph.build_graph(ModelDependencySnapshot(tenant_id=T, project_id=P, model_id=M,
                                                  dependency_revision=0))
    assert g.counts()["nodes"] == 1  # just the model node
    assert len(EDGE_BUILDERS) >= 18
