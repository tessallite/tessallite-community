"""Row-population equivalence proof for AGGREGATE serving (Bug-8664).

The aggregate sibling of ``routing/pocket_population`` (Bug-8580). Read that
module's header first: it owns the rule ("does joining these extra relations
change the row multiset?") and this module owns only the aggregate-specific
question of WHICH relations the aggregate joined.

The gap this closes
-------------------
An aggregate's CTAS compiles ``{anchor} ∪ grain-dimension tables ∪
measure-source tables`` (plus any stepping stone the traversal needed) and
GROUPs. A query served from it is compiled with JOIN ELISION — only the
relations owning a column it references. When the aggregate joined a relation
the query's own plan would not, and that join is not row-preserving, the
pre-aggregated numbers are computed over a DIFFERENT row population than the
source route would scan:

* an ``INNER`` edge to a grain dimension drops every fact row with no partner,
  so ``SUM`` over a roll-up that does not reference that dimension is
  understated;
* an edge whose far key is not a declared single-column primary key fans the
  fact out, so the same ``SUM`` is overstated.

The pocket route has refused this since Bug-8580; the aggregate route — which
serves far more production traffic — had nothing. That is the CLAUDE.md
shared-primitive gap, and this module is the aggregate half.

Why the pocket estimate could not simply be reused
--------------------------------------------------
``population_proven`` with no explicit plan estimates the artifact's plan as
the whole reachable component, which is exactly right for a pocket
(``SELECT *`` joins everything) and grossly wrong for an aggregate. Measured
offline against the acme-demo seed: the reachability estimate refuses the
``inventory``, ``modell`` and ``onboarding`` models WHOLESALE, while a bound
derived from each aggregate's own grain/measures proves 82 of 86 seed
rollup cases and refuses 4 (legacy ``many_to_one`` tokens on edges inside those
aggregates' own plans). Precision is the difference between a correctness gate
and a platform-wide acceleration outage.

The plan bound comes from ``shared.semantic.aggregate_plan_bound``, which is a
provable UPPER BOUND over both real FROM-clause builders rather than a third
copy of either traversal. See that module for the proof.

Authority
---------
Every input is read from the DEPLOYED snapshot for a deployed model (the same
authority ``pocket_matcher._load_model_join_graph`` and
``_get_canonical_dims_cached`` use), never from mutable live rows. A model that
is genuinely undeployed reads live rows. Anything that cannot be resolved is
UNPROVEN and refuses the aggregate route — never "unconstrained".
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.semantic.aggregate_plan_bound import (
    aggregate_plan_upper_bound,
    component_is_acyclic,
)
from src.routing.pocket_population import ModelJoinGraph, population_proven

logger = logging.getLogger(__name__)

#: Sentinel recorded when an object DECLARES a physical binding this graph cannot
#: resolve. Distinct from "absent" (declares no binding, contributes no relation
#: to either builder) — an unresolvable declared binding means the builders
#: WOULD add a relation this module cannot name, so the plan bound would be an
#: under-estimate and the aggregate must be refused.
UNRESOLVED = "\x00unresolved"

#: ``Measure.measure_type`` value both builders test through
#: ``optimizer/src/ddl/_calculated_columns.is_calculated``.
_CALCULATED = "calculated"

_CONTEXT_CACHE_TTL_SECONDS = 300
_CONTEXT_CACHE_MAX_ENTRIES = 256
# (model_id, deployed_version_id, epoch) -> (expires_at, AggregateObjectIndex)
_CONTEXT_CACHE: dict[tuple[str, str, int], tuple[float, "AggregateObjectIndex"]] = {}


def invalidate_aggregate_population_cache(model_id: object | None = None) -> None:
    """Drop cached object indexes. No arg clears everything (test hook)."""
    if model_id is None:
        _CONTEXT_CACHE.clear()
        return
    mid = str(model_id)
    for key in [k for k in _CONTEXT_CACHE if k[0] == mid]:
        _CONTEXT_CACHE.pop(key, None)


@dataclass(frozen=True)
class AggregateObjectIndex:
    """The name/id -> owning-relation lookups an aggregate's plan needs.

    Deliberately mirrors what ``shared.semantic.grain_resolver
    .resolve_aggregate_layout`` reads, because that resolver is what BOTH
    builders derive their ``needed_table_ids`` from:

    * a grain entry is a ``Dimension`` NAME; its relation is the owning table of
      ``source_column_id``, or the ``table_id`` of ``user_defined_attribute_id``;
    * an aggregate column names a ``Measure`` — by NAME, matching
      ``full_refresh``'s ``measure_specs`` (``col.measure.name``), which is the
      identity the refresh actually resolves against the model; its relation is
      the owning table of ``source_column_id``;
    * a CALCULATED measure owns no source column — the builders chase its
      expression's referenced measures instead, so their relations are needed
      too.

    ``dimension_names`` / ``measure_id_by_name`` are the FULL declared
    vocabularies, so a name absent from them is a genuine resolution failure
    (fail closed) rather than an object that merely owns no relation
    (contributes nothing, exactly as in the builders).
    """

    dimension_names: frozenset[str] = frozenset()
    table_by_dimension_name: dict[str, str] = field(default_factory=dict)
    measure_id_by_name: dict[str, str] = field(default_factory=dict)
    table_by_measure_id: dict[str, str] = field(default_factory=dict)
    expression_by_measure_id: dict[str, str] = field(default_factory=dict)


def _index_from_rows(
    dimensions: Iterable[Any],
    measures: Iterable[Any],
    *,
    table_by_column_id: dict[str, str],
    table_by_uda_id: dict[str, str],
) -> AggregateObjectIndex:
    dim_names: set[str] = set()
    dim_tables: dict[str, str | None] = {}
    for dim in dimensions:
        name = getattr(dim, "name", None)
        if not name:
            continue
        name = str(name)
        resolved: str | None = None
        col_id = getattr(dim, "source_column_id", None)
        uda_id = getattr(dim, "user_defined_attribute_id", None)
        if col_id is not None:
            # R1 finding 4: an object that DECLARES a binding whose column this
            # graph does not know is NOT the same as an object that declares no
            # binding. When the column exists in the LIVE rows the builders read
            # but not in the snapshot this graph came from, they resolve it and
            # DO add its relation to ``needed``, so recording "no relation" here
            # would UNDER-estimate the plan and let a lossy join through
            # unchecked. When it is missing on both sides
            # (``grain_resolver.resolve_aggregate_layout`` leaves
            # ``src_table_id=None``) the builders add nothing and this is an
            # over-refusal — accepted, because the two cases are
            # indistinguishable from here and only one of them is safe.
            # ``UNRESOLVED`` propagates as a refusal in both.
            resolved = table_by_column_id.get(str(col_id)) or UNRESOLVED
        elif uda_id is not None:
            resolved = table_by_uda_id.get(str(uda_id)) or UNRESOLVED
        # else: no binding declared at all (an expression dimension) —
        # contributes nothing to either builder's ``needed`` set, so nothing
        # here either.
        if name in dim_names and dim_tables.get(name) != resolved:
            # A flat dimension and a hierarchy level (or two levels) share this
            # name and disagree about the owning relation. The two builders
            # ALSO disagree about which wins — the optimizer appends hierarchy
            # levels AFTER the flat dimensions so a level wins its dict
            # comprehension, while the scheduler's refresh passes flat
            # dimensions only. Nothing here can honestly say which relation the
            # CTAS joined, so refuse rather than pick.
            dim_tables[name] = UNRESOLVED
            continue
        dim_names.add(name)
        if resolved is not None:
            dim_tables[name] = resolved

    measure_tables: dict[str, str | None] = {}
    measure_id_by_name: dict[str, str] = {}
    expressions: dict[str, str] = {}
    for measure in measures:
        mid = getattr(measure, "id", None)
        name = getattr(measure, "name", None)
        if mid is None or not name:
            continue
        mid = str(mid)
        measure_id_by_name[str(name)] = mid
        # R1 finding 9: gate on the same predicate BOTH builders use
        # (``_calculated_columns.is_calculated`` -> ``measure_type ==
        # "calculated"``), not on "carries an expression". A non-calculated
        # measure that happens to hold an expression must not be parsed as a
        # calculated one.
        if str(getattr(measure, "measure_type", "") or "") == _CALCULATED:
            expressions[mid] = str(getattr(measure, "expression", "") or "")
        col_id = getattr(measure, "source_column_id", None)
        if col_id is not None:
            measure_tables[mid] = table_by_column_id.get(str(col_id)) or UNRESOLVED

    return AggregateObjectIndex(
        dimension_names=frozenset(dim_names),
        table_by_dimension_name=dim_tables,
        measure_id_by_name=measure_id_by_name,
        table_by_measure_id=measure_tables,
        expression_by_measure_id=expressions,
    )


async def load_aggregate_object_index(
    model: Any,
    db: AsyncSession,
    *,
    graph: ModelJoinGraph | None,
    table_id_by_uda_id: dict[str, str],
) -> AggregateObjectIndex | None:
    """Resolve (and cache) the dimension/measure -> relation lookups.

    Returns ``None`` when the model's objects cannot be resolved. A ``None``
    refuses every aggregate on this request (fail closed) and is NOT cached, so
    a transient snapshot error does not disable aggregate serving for the whole
    TTL — the same rule ``pocket_matcher._load_model_join_graph`` applies.
    """
    if graph is None:
        return None
    deployed_version_id = getattr(model, "deployed_version_id", None)
    epoch = int(getattr(model, "deploy_epoch", 0) or 0)
    key = (str(getattr(model, "id", "")), str(deployed_version_id or ""), epoch)
    now = _time.monotonic()
    cached = _CONTEXT_CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    index: AggregateObjectIndex | None = None
    try:
        if deployed_version_id is not None:
            from src.semantic.snapshot_resolver import resolve_deployed_shape

            shape = await resolve_deployed_shape(model, db)
            if shape is None:
                logger.warning(
                    "Bug-8664: deployed model %s has no resolvable snapshot for "
                    "the aggregate row-population proof; refusing all aggregates",
                    getattr(model, "id", None),
                )
            else:
                from src.semantic.snapshot_resolver import (
                    hierarchy_level_dimensions_from_snapshot,
                )

                # R1 finding 5: a grain entry can be a HIERARCHY LEVEL name, not
                # only a flat ``Dimension`` name — the optimizer appends
                # ``load_hierarchy_level_dimensions`` to the vocabulary it hands
                # ``resolve_aggregate_layout`` precisely so a grain of
                # ``calendar_month`` or ``date_hierarchy.Year`` resolves. Reading
                # ``shape.dimensions`` alone made every such aggregate report
                # ``JOIN_POPULATION_MISMATCH`` forever — fail-closed, so no wrong
                # numbers, but a silent and mislabelled acceleration outage.
                index = _index_from_rows(
                    list(shape.dimensions or [])
                    + list(hierarchy_level_dimensions_from_snapshot(shape)),
                    shape.measures or [],
                    table_by_column_id=graph.table_id_by_column_id,
                    table_by_uda_id=table_id_by_uda_id,
                )
        else:
            from shared.db.models import Dimension, Measure
            from shared.semantic.hierarchy_resolver import (
                load_hierarchy_level_dimensions,
            )

            model_id = getattr(model, "id", None)
            if model_id is not None:
                dims = (
                    await db.execute(
                        select(Dimension).where(Dimension.model_id == model_id)
                    )
                ).scalars().all()
                measures = (
                    await db.execute(
                        select(Measure).where(Measure.model_id == model_id)
                    )
                ).scalars().all()
                # Same vocabulary as the optimizer's live read, from the same
                # shared helper it uses.
                levels = await load_hierarchy_level_dimensions(model_id, db)
                index = _index_from_rows(
                    list(dims) + list(levels), measures,
                    table_by_column_id=graph.table_id_by_column_id,
                    table_by_uda_id=table_id_by_uda_id,
                )
    except Exception:
        logger.error(
            "Bug-8664: object-index resolution failed for model %s; refusing "
            "all aggregates (fail closed)",
            getattr(model, "id", None), exc_info=True,
        )
        index = None

    if index is None:
        return None
    if len(_CONTEXT_CACHE) >= _CONTEXT_CACHE_MAX_ENTRIES:
        oldest = min(_CONTEXT_CACHE, key=lambda k: _CONTEXT_CACHE[k][0])
        _CONTEXT_CACHE.pop(oldest, None)
    _CONTEXT_CACHE[key] = (now + _CONTEXT_CACHE_TTL_SECONDS, index)
    return index


def _calculated_reference_measure_ids(
    measure_id: str, index: AggregateObjectIndex
) -> set[str] | None:
    """Measure ids a calculated measure's expression references.

    ``None`` when the expression cannot be parsed or names a measure this model
    does not declare: the builders would then join a relation this function
    cannot name, so the plan bound would be an UNDER-estimate. Fail closed.

    Only measures the index recorded as CALCULATED reach the parser (the index
    records an entry for exactly those, matching ``is_calculated`` in both
    builders). A measure with no entry is not calculated and references nothing;
    a calculated measure with an EMPTY expression is a model the builders cannot
    render either, so it is unproven rather than reference-free.
    """
    if measure_id not in index.expression_by_measure_id:
        return set()
    expression = index.expression_by_measure_id[measure_id]
    if not expression:
        return None
    from shared.semantic.calculated_expression import (
        ExpressionValidationError,
        parse_expression,
    )

    try:
        parsed = parse_expression(expression)
    except ExpressionValidationError:
        return None
    except Exception:  # pragma: no cover - defensive
        return None
    referenced: set[str] = set()
    for ref in parsed.references:
        ref_id = index.measure_id_by_name.get(str(getattr(ref, "name", "")))
        if ref_id is None:
            return None
        if ref_id in index.expression_by_measure_id:
            # The reference is ITSELF calculated. This function resolves ONE
            # hop, matching what both builders do
            # (``creator._resolve_calculated_context`` and
            # ``full_refresh``'s ``parse_expression`` loop each read the
            # referenced measure's ``source_column_id`` and add nothing when it
            # is a calculated measure with none). Nested calculation is
            # explicitly unsupported at materialise time — the creator's own
            # docstring records it as v2 work — so such an aggregate cannot be
            # built correctly today and there is nothing here to prove.
            #
            # Refusing rather than ignoring is what keeps this SAFE if nested
            # calculation is ever supported: a chain A -> B -> C would then make
            # the CTAS join C's relation, and a one-hop resolver would return a
            # plan bound that omits it — an UNDER-estimate, which is the one
            # direction that yields a wrong number. External gate finding,
            # 2026-08-05.
            return None
        referenced.add(ref_id)
    return referenced


def aggregate_needed_table_ids(
    aggregate: Any, index: AggregateObjectIndex
) -> frozenset[str] | None:
    """The relations an aggregate's CTAS FROM clause must reach.

    A SUPERSET of ``needed_table_ids`` as both builders compute it
    (``scheduler/jobs/full_refresh`` from ``layout.grain_cols`` +
    ``layout.measure_cols`` + calculated references;
    ``optimizer/lifecycle/creator`` from the same three sources). Over-shooting
    is safe — it only widens the plan bound, which only adds refusals — but
    UNDER-shooting would hide a lossy join, so anything unresolvable returns
    ``None`` (UNPROVEN) rather than a partial set.

    Three outcomes per object, and the middle one is the one R1 finding 4 was
    about:

    * declares a binding this graph resolves -> its relation joins ``needed``;
    * declares a binding this graph CANNOT resolve -> ``UNRESOLVED`` -> the whole
      aggregate is unproven, because both builders resolve that binding from live
      rows and WOULD join its relation;
    * declares no binding at all (an expression dimension, a UDA with no table)
      -> contributes nothing, exactly as it contributes nothing to the builders'
      ``needed`` sets.

    An object the model does not DECLARE at all is likewise a resolution failure.
    """
    needed: set[str] = set()

    for raw_name in (getattr(aggregate, "grain", None) or []):
        name = str(raw_name)
        if name not in index.dimension_names:
            # ``resolve_aggregate_layout`` raises ``GrainResolutionError`` on an
            # unknown grain name, so an aggregate in this state cannot even be
            # refreshed. Its plan is unknowable here.
            return None
        table = index.table_by_dimension_name.get(name)
        if table is UNRESOLVED:
            return None
        if table:
            needed.add(table)

    for column in (getattr(aggregate, "columns", None) or []):
        measure = getattr(column, "measure", None)
        if measure is None:
            # The ``__row_count`` column and any other measure-free column bind
            # no relation; ``full_refresh`` skips them the same way when it
            # builds ``measure_specs``.
            continue
        measure_name = str(getattr(measure, "name", "") or "")
        measure_id = index.measure_id_by_name.get(measure_name)
        if measure_id is None:
            # The DEPLOYED model does not declare this measure, so the refresh
            # resolver could not resolve it either. Unknowable plan.
            return None
        table = index.table_by_measure_id.get(measure_id)
        if table is UNRESOLVED:
            return None
        if table:
            needed.add(table)
        referenced = _calculated_reference_measure_ids(measure_id, index)
        if referenced is None:
            return None
        for ref_id in referenced:
            ref_table = index.table_by_measure_id.get(ref_id)
            if ref_table is UNRESOLVED:
                return None
            if ref_table:
                needed.add(ref_table)

    return frozenset(needed)


# ---------------------------------------------------------------------------
# Miss-reason tokens EMITTED by the row-population proof.
#
# Defined HERE, in the emitter, and re-exported by
# ``aggregate_matcher.AggregateSkipReason`` rather than being spelled twice.
# Bug-8789 originally declared them on the enum AND wrote them as string
# literals in this module; the enum members then had zero production
# references, so ``test_miss_reason_vocabulary_parity`` (which enumerates the
# ENUM) would have stayed green while a typo'd literal here fell through to the
# fail-open BUILD default in ``shared.miss_reason_taxonomy``. One definition,
# imported by every consumer, makes that drift unrepresentable.
# ---------------------------------------------------------------------------

#: Fixable: a narrower aggregate built at the query's own grain could serve.
POPULATION_PLAN_MISMATCH = "population_plan_mismatch"
#: Terminal: a model-level fault (cyclic component / unresolvable anchor) that
#: no new aggregate can repair.
POPULATION_UNPROVABLE_MODEL = "population_unprovable_model"
#: The single legacy token both sub-causes collapse to when the tenant's
#: ``query.population_mismatch_reason_mode`` is "legacy" (the default).
LEGACY_POPULATION_MISMATCH = "join_population_mismatch"


class AggregatePopulationChecker:
    """Per-request gate shared by EVERY route that can serve an aggregate.

    There are three ``RouteDecision(route_type="aggregate")`` producers — the
    ordinary matcher loop, the period-variant loop (both in
    ``routing/aggregate_matcher``) and ``router._try_derived_exact_route`` — and
    they all read the SAME materialised rows, so they all carry the same
    population exposure. Round-1 review found the derived-exact route serving
    with no proof at all because the gate had been wired into the matcher rather
    than into a primitive every producer calls; this class is that primitive, so
    a fourth producer has one obvious thing to call instead of a fourth copy to
    forget.

    The model-level inputs (join graph, the query's own plan relations, the
    dimension/measure -> relation index) are resolved LAZILY and at most once per
    instance: a query with no aggregate candidate pays nothing, and a query with
    fifty candidates pays once.
    """

    __slots__ = ("_bound_query", "_db", "_resolved", "_graph", "_index",
                 "_query_table_ids", "_split_codes")

    def __init__(
        self, bound_query: Any, db: AsyncSession, *, split_codes: bool = False,
    ) -> None:
        self._bound_query = bound_query
        self._db = db
        self._resolved = False
        self._graph: ModelJoinGraph | None = None
        self._index: AggregateObjectIndex | None = None
        self._query_table_ids: set[str] | None = None
        # Bug-8789: the reason-code VOCABULARY is a caller concern, not the
        # proof's. This class stays a pure proof primitive with no settings
        # dependency: the caller resolves the mode once (see
        # ``aggregate_matcher._resolve_population_reason_split``) and passes
        # the decision in. The original implementation read the setting here
        # with ``system_session=self._db`` — but ``self._db`` is the TENANT
        # session, so the read resolved against the wrong scope. Any caller
        # holding a mock/stub session saw a truthy row and silently flipped
        # the flag ON, which is how six pre-existing Bug-8664 tests turned red.
        self._split_codes = bool(split_codes)

    async def _resolve(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        # Reached through the module object, not imported by name, so the two
        # routes provably share ONE resolution of the model's join graph and of
        # the query's own plan (and one test seam) rather than growing a second
        # opinion about either.
        from src.routing import pocket_matcher as _pm

        graph, uda_tables = await _pm._load_model_join_graph(
            getattr(self._bound_query, "model", None), self._db,
        )
        self._graph = graph
        self._query_table_ids = _pm._query_plan_table_ids(
            self._bound_query, graph, uda_tables,
        )
        self._index = await load_aggregate_object_index(
            getattr(self._bound_query, "model", None), self._db,
            graph=graph, table_id_by_uda_id=uda_tables,
        )

    async def proven(self, aggregate: Any) -> tuple[bool, str | None]:
        """(proven, reason) — proven is True when serving ``aggregate`` returns
        the query's row population; when False, ``reason`` records the sub-cause."""
        await self._resolve()
        return aggregate_population_proven(
            aggregate=aggregate,
            graph=self._graph,
            index=self._index,
            query_table_ids=self._query_table_ids,
            split_codes=bool(self._split_codes),
        )


def aggregate_population_proven(
    *,
    aggregate: Any,
    graph: ModelJoinGraph | None,
    index: AggregateObjectIndex | None,
    query_table_ids: Iterable[str] | None,
    split_codes: bool = False,
) -> tuple[bool, str | None]:
    """(proven, reason) — True when serving ``aggregate`` returns the query's own
    row population; when False, ``reason`` records the sub-cause.

    Fails closed on every unresolved input. A False result never produces a
    wrong number; it only forgoes an acceleration.

    ``split_codes`` selects the reason VOCABULARY only — it can never change
    whether the proof holds, and therefore never changes which aggregate is
    admitted. When True (Bug-8789) the reason is one of the two distinct
    sub-codes; when False every False returns the legacy
    ``"join_population_mismatch"`` string unchanged.

    Seven sub-causes: five fixable → ``population_plan_mismatch`` (BUILD: a
    narrower aggregate could serve), two terminal → ``population_unprovable_model``
    (INELIGIBLE: a cyclic join component or unresolvable anchor, which no new
    aggregate can repair). Both collapse to ``join_population_mismatch`` when
    ``split_codes`` is False.

    This function reads NO settings. The caller resolves the vocabulary once
    (``aggregate_matcher._resolve_population_reason_split``) and passes it in,
    so the proof stays pure and testable without a session.
    """
    if graph is None or index is None or query_table_ids is None:
        return (False, POPULATION_PLAN_MISMATCH if split_codes else LEGACY_POPULATION_MISMATCH)
    needed = aggregate_needed_table_ids(aggregate, index)
    if needed is None:
        return (False, POPULATION_PLAN_MISMATCH if split_codes else LEGACY_POPULATION_MISMATCH)
    edges = [(e.left_table_id, e.right_table_id) for e in graph.edges]
    plan = aggregate_plan_upper_bound(
        table_ids=graph.table_ids,
        edges=edges,
        anchor_table_id=graph.anchor_table_id,
        needed_table_ids=needed,
    )
    if plan is None:
        # plan is None splits into two families:
        #   - anchor unresolved / empty universe → terminal (model-level fault)
        #   - needed ⊄ universe → fixable (narrower aggregate avoids the unknown relation)
        anchor = str(graph.anchor_table_id) if graph.anchor_table_id else ""
        if not graph.table_ids or anchor not in frozenset(str(t) for t in graph.table_ids if str(t)):
            return (
                False,
                POPULATION_UNPROVABLE_MODEL if split_codes else LEGACY_POPULATION_MISMATCH,
            )
        return (False, POPULATION_PLAN_MISMATCH if split_codes else LEGACY_POPULATION_MISMATCH)
    # Bug-8637: on a CYCLIC component the FROM clause is a spanning-tree choice,
    # and the served source route and the aggregate CTAS provably make different
    # choices — different edges, different ON columns, a different row
    # population, permanently and with no staleness signal. Checked over the
    # whole component rather than only the plan bound because the second path
    # can leave the bound and return, and the source route can traverse it.
    # ``population_proven``'s own per-component tree test then covers the
    # in-plan case; this covers the rest of the component it cannot see.
    if not component_is_acyclic(
        seeds=plan, table_ids=graph.table_ids, edges=edges,
    ):
        return (
            False,
            POPULATION_UNPROVABLE_MODEL if split_codes else LEGACY_POPULATION_MISMATCH,
        )
    proven = population_proven(
        graph=graph,
        query_table_ids=query_table_ids,
        plan_table_ids=plan,
    )
    if not proven:
        return (False, POPULATION_PLAN_MISMATCH if split_codes else LEGACY_POPULATION_MISMATCH)
    return (True, None)
