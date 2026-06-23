"""
Router — combines matching + validation into a single RouteDecision,
then delegates logging.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit.logger import audit
from shared.db.models import (
    DataTarget,
    Dimension,
    ModelColumn,
    PersonaTagRestriction,
    Persona,
    ProjectConnection,
    UserDefinedAttribute,
    data_tag_columns,
)
from shared.aggregate_quantiles import (
    is_quantile_stat_type,
    quantile_materialization_is_exact,
)
from src.ir.logical_query import BoundQuery, NoAggregateMatchError, RouteDecision, UnsupportedSQL
from src.routing.aggregate_matcher import find_best_aggregate
from src.routing.pocket_matcher import PocketSkipReason, find_best_pocket
from src.routing.exactness_validator import validate_aggregate_route
from src.rewrite.query_rewriter import (
    AggregateRewriteUnsupported,
    rewrite_for_aggregate,
    rewrite_for_pocket,
    rewrite_for_source,
    resolve_target_dialect_for_bound,
    dialect_to_connector,
    dialect_from_connection_type,
)
from src.security import (
    CompiledPredicate,
    Principal,
    compile_row_security,
    has_active_rules,
)

logger = logging.getLogger(__name__)

_MULTI_RELATION_WINDOW_UNSUPPORTED = (
    "Window functions across multiple semantic relations are not yet supported "
    "— simplify the query to reference a single model relation"
)
_COMPLEX_MULTI_RELATION_UNSUPPORTED = (
    "Complex SQL over multiple semantic relations is not yet supported "
    "— simplify the query to reference a single model relation"
)


def _uuid_or_none(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _record_rls_bypass_audit(
    db: AsyncSession,
    *,
    bound_query: BoundQuery,
    principal: Principal | None,
    persona: Persona,
    rule_ids: list[str],
) -> None:
    """Persist a structured platform audit event when a persona with
    ``bypass_row_security=true`` causes active RLS rules to be skipped.

    F-008-24: an INFO service log is not durable, tenant-scoped, or visible
    in the audit trail. This records WHO bypassed (actor identity), on WHICH
    model, under WHICH persona, and the COUNT/identity of the skipped rules,
    so a security operator can review bypasses via the audit surface.
    Wrapped so an audit-write failure never blocks the query — the bypass
    decision and its authorization are unchanged.
    """
    try:
        model = bound_query.model
        logical = bound_query.logical_query
        await audit(
            db,
            action="query.rls_bypass",
            severity="warn",
            actor_email=principal.user_identity if principal is not None else None,
            target_type="model",
            target_id=_uuid_or_none(getattr(model, "id", None)),
            target_name=(
                getattr(model, "display_name", None)
                or getattr(model, "slug", None)
                or str(getattr(model, "id", ""))
            ),
            detail={
                "persona_id": str(persona.id),
                "persona_name": getattr(persona, "name", ""),
                "rules_skipped": rule_ids,
                "rule_count": len(rule_ids),
                "query_fingerprint": (getattr(logical, "query_fingerprint", "") or "")[:16],
                "protocol": getattr(logical, "protocol", ""),
            },
        )
    except Exception:
        logger.warning("Failed to persist RLS-bypass audit event", exc_info=True)


def _query_uses_percentile(bound_query: BoundQuery) -> bool:
    """True if any projection maps to a materialised quantile column (pNN)."""
    return any(
        is_quantile_stat_type(getattr(e, "agg_function", None))
        for e in getattr(bound_query.logical_query, "select_expressions", []) or []
    )


def _distinct_physical_relation_count(query) -> int:
    """Count distinct physical relations (scans) in the raw SQL.

    Bug-3641: the bare-name ``from_tables`` minus ``cte_aliases`` set difference
    excluded a *physical* scan whose unqualified name collides with a CTE alias
    (e.g. the scan inside ``WITH sales AS (SELECT … FROM sales) SELECT … FROM
    sales, other``), so a genuinely multi-relation complex query slipped past
    the gate. This mirrors ``_inject_security_where``'s scope-aware
    discrimination: a table reference whose scope source is another Scope is a
    CTE / derived-table read; only references resolving to a physical
    ``exp.Table`` count as relations.

    Falls back to the bare-name count when raw SQL is unavailable or cannot be
    scope-analysed — fail loud by *over*-counting (the old behaviour, which
    never under-counted except on the collision case this fix targets), never by
    silently dropping a relation.
    """
    raw_sql = getattr(query, "raw_query", "") or ""
    cte_aliases = {name.lower() for name in getattr(query, "cte_aliases", [])}

    def _bare_name_count() -> int:
        return len({
            name.lower().split(".")[-1]
            for name in getattr(query, "from_tables", [])
            if name.lower().split(".")[-1] not in cte_aliases
        })

    if not raw_sql.strip():
        return _bare_name_count()

    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.scope import Scope, traverse_scope

    dialect = getattr(query, "input_dialect", "postgres") or "postgres"
    try:
        ast = sqlglot.parse_one(raw_sql, read=dialect)
        scopes = traverse_scope(ast)
    except Exception:
        return _bare_name_count()

    physical: set[str] = set()
    for scope in scopes:
        for table in scope.tables:
            source = scope.sources.get(table.alias_or_name)
            if isinstance(source, Scope):
                continue  # CTE / derived-table reference, not a physical scan
            if isinstance(source, exp.Table):
                physical.add(source.name.lower().split(".")[-1])
    return len(physical)


def _validate_complex_passthrough(bound_query: BoundQuery) -> None:
    """Reject raw complex SQL whose adapter substitution cannot preserve meaning."""
    query = bound_query.logical_query
    if not getattr(query, "has_complex_sql", False):
        return

    relation_count = _distinct_physical_relation_count(query)
    if relation_count > 1 and not getattr(query, "has_window_functions", False):
        raise UnsupportedSQL(_COMPLEX_MULTI_RELATION_UNSUPPORTED)
    if (
        getattr(query, "has_window_functions", False)
        and relation_count > 1
    ):
        raise UnsupportedSQL(_MULTI_RELATION_WINDOW_UNSUPPORTED)


async def route_query(
    bound_query: BoundQuery,
    db: AsyncSession,
    principal: Principal | None = None,
    force_route: str | None = None,
    persona: Persona | None = None,
) -> RouteDecision:
    """
    1. Find the best aggregate candidate.
    2. Validate the candidate against exactness rules.
    3. Rewrite the query for the chosen path.
    4. Return a RouteDecision (logging is handled by the caller).

    If the bound query references any semantic objects currently
    marked ``is_invalid=True`` (see Phase 1 of the model-health plan),
    skip the aggregate matcher and go straight to the source path so
    the end user gets a usable result whenever structurally possible,
    and record a ``query_fallback`` alert so the modeler sees the
    query attempt in the Model Health tab.

    ``force_route="source"`` (Phase 5.2) bypasses aggregate and pocket
    matching and rewrites against the source directly. Row-security
    still wraps the result — a force-live override cannot expand the
    caller's row set.

    ``persona`` (Phase 8.C.1) — when supplied and its
    ``bypass_row_security`` flag is true, the router skips the Phase 5.1
    row-security wrap and re-enables aggregate + pocket matching. The
    bypass is structured-logged AND persisted as a platform audit event
    (F-008-24) so operators can review it in the audit trail.
    """
    _validate_complex_passthrough(bound_query)
    await _ensure_valid_user_defined_attributes(bound_query, db)

    # Phase 9 H.6 — column-level persona tag restriction (CLS).
    # Runs BEFORE the Phase 5.1 row-security early-return so row-level and
    # column-level security compose (F-008-02): callers under active RLS
    # rules must still be blocked from tag-restricted columns. The check
    # fails closed for complex SQL and silently narrows SELECT * (see
    # _check_column_restrictions).
    if persona is not None:
        restricted = await _check_column_restrictions(bound_query, persona, db)
        if restricted:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail=await _cls_restricted_detail(restricted, persona, db),
            )

    # Bug-899: Use touched-source dialect so the rewrite dialect matches the
    # execution dialect. resolve_target_dialect_for_bound walks the bound
    # query's column mappings to find the DataSource the query will actually
    # hit, instead of picking an arbitrary first source for the model.
    _target_dialect_early = await resolve_target_dialect_for_bound(db, bound_query)
    _connector = dialect_to_connector(_target_dialect_early)

    # Phase 5.1 — row security. If any rule applies to the principal,
    # bypass aggregate + pocket matching and wrap the source rewrite.
    # Aggregates/pockets were built without knowing the audience, so
    # reusing them would leak rows outside the rule.
    row_security = None
    if principal is not None:
        row_security = await compile_row_security(
            bound_query.model.id, principal, db, connector=_connector,
        )
    rls_bypass = bool(persona is not None and persona.bypass_row_security)
    if has_active_rules(row_security) and not rls_bypass:
        # F-004-08: ``force_route="aggregate"/"pocket"`` is a request to pin a
        # SPECIFIC fast path. Active row-security forces the source route
        # (aggregates/pockets were built without an audience and would leak
        # rows), which is incompatible with the force. Honour the documented
        # contract — "an incompatible force produces an error rather than
        # silently returning [a different route]" — by raising, never by
        # weakening security.
        if force_route in ("aggregate", "pocket"):
            raise NoAggregateMatchError(
                f"force_route={force_route!r} is incompatible with the active "
                "row-level security rules on this query (security requires the "
                "source route). Remove force_route to run the query."
            )
        return await _route_with_row_security(bound_query, db, row_security, target_dialect=_target_dialect_early)
    if rls_bypass and has_active_rules(row_security):
        active_rule_ids = row_security.active_rule_ids if row_security else []
        logger.info(
            "persona_bypass_row_security=true model_id=%s persona_id=%s "
            "rules_skipped=%s",
            bound_query.model.id,
            persona.id,
            ",".join(active_rule_ids),
        )
        await _record_rls_bypass_audit(
            db,
            bound_query=bound_query,
            principal=principal,
            persona=persona,
            rule_ids=list(active_rule_ids),
        )

    # Phase 5.2 — per-query force-live override. Skips aggregate and
    # pocket matching; row-security is handled earlier and unaffected.
    if force_route == "source":
        rewritten = await rewrite_for_source(bound_query, db, target_dialect=_target_dialect_early)
        return RouteDecision(
            route_type="source",
            rewritten_query=rewritten,
            reason="force_route=source set on request; aggregate + pocket matchers bypassed",
            aggregate_id=None,
            pocket_id=None,
            target_dialect=_target_dialect_early,
        )

    # F-004-08: every structural reason below FORCES the source route
    # (aggregate/pocket matching is structurally impossible or unsafe). When the
    # caller pinned a specific fast path, that force is incompatible — raise
    # rather than silently returning source, per the HTTP-boundary contract.
    def _reject_incompatible_force(why: str) -> None:
        if force_route in ("aggregate", "pocket"):
            raise NoAggregateMatchError(
                f"force_route={force_route!r} is incompatible with this query: "
                f"{why}. Remove force_route to run the query against the source."
            )

    model_status = str(getattr(bound_query.model, "status", "") or "").lower()
    if model_status == "disabled":
        _reject_incompatible_force("the model is disabled")
        rewritten = await rewrite_for_source(bound_query, db, target_dialect=_target_dialect_early)
        return RouteDecision(
            route_type="source",
            rewritten_query=rewritten,
            reason="Model is disabled; aggregate routing bypassed",
            aggregate_id=None,
            target_dialect=_target_dialect_early,
        )

    if not bool(getattr(bound_query.model, "aggregations_enabled", True)):
        _reject_incompatible_force("aggregations are disabled for this model")
        rewritten = await rewrite_for_source(bound_query, db, target_dialect=_target_dialect_early)
        return RouteDecision(
            route_type="source",
            rewritten_query=rewritten,
            reason="Aggregations are disabled for this model",
            aggregate_id=None,
            target_dialect=_target_dialect_early,
        )

    # Fallback path for queries that touch invalid semantic objects:
    # aggregates for the same objects will also be invalid (the
    # revalidator guarantees consistency), so skip matching, go to
    # source, and leave a persistent alert the modeler can act on.
    if bound_query.uses_invalid_objects:
        _reject_incompatible_force("it references invalid semantic objects")
        rewritten = await rewrite_for_source(bound_query, db, target_dialect=_target_dialect_early)
        await _record_query_fallback_alert(bound_query, db)
        names = ", ".join(f"{kind}:{name}" for kind, name in bound_query.uses_invalid_objects)
        return RouteDecision(
            route_type="source",
            rewritten_query=rewritten,
            reason=f"Query references invalid semantic objects — routed to source ({names})",
            aggregate_id=None,
            target_dialect=_target_dialect_early,
        )

    # SELECT-vs-GROUP-BY validation: when aggregate measures are present,
    # every non-aggregated SELECT dimension must appear in GROUP BY.
    # Skip when: has_complex_sql (passthrough), has_function_grain
    # (EXTRACT/DATE_TRUNC/UPPER in GROUP BY — cannot match bare dim names),
    # or has_passthrough_expressions (compound aggregate passthrough).
    lq = bound_query.logical_query
    if (
        bound_query.resolved_measures
        and lq.requested_dimensions
        and not lq.select_star
        and not lq.has_complex_sql
        and not getattr(lq, "has_function_grain", False)
        and not getattr(bound_query, "has_passthrough_expressions", False)
    ):
        grain_set = set(lq.grain) if lq.grain else set()
        resolved_measure_names = {m.name for m in bound_query.resolved_measures}
        ungrouped = [
            d for d in lq.requested_dimensions
            if d not in grain_set and d not in resolved_measure_names
        ]
        if ungrouped:
            from fastapi import HTTPException
            detail = "; ".join(
                f"Dimension '{d}' appears in SELECT but not in GROUP BY"
                for d in ungrouped
            )
            raise HTTPException(
                status_code=422,
                detail=f"{detail}. When aggregate measures are used, all non-aggregated dimensions must be grouped.",
            )

    # DAX time-variant hints (TOTALYTD, SAMEPERIODLASTYEAR, etc.) transform
    # the base measure into a period-aware variant at source-rewrite time.
    # Aggregates store the base measure, not the variant, so routing to an
    # aggregate would return plain Revenue instead of YTD Revenue.
    _dax_hints = getattr(bound_query.logical_query, "time_variant_hints", None)
    if _dax_hints:
        _reject_incompatible_force("it carries DAX time-variant hints that require a source rewrite")
        rewritten = await rewrite_for_source(bound_query, db, target_dialect=_target_dialect_early)
        return RouteDecision(
            route_type="source",
            rewritten_query=rewritten,
            reason="DAX time-variant hints require source rewrite; aggregate routing bypassed",
            aggregate_id=None,
            target_dialect=_target_dialect_early,
        )

    # F-004-08: ``force_route`` names a SPECIFIC fast path. Honour it by gating
    # which matcher runs:
    #   - force_route="pocket"    → run the pocket matcher only.
    #   - force_route="aggregate" → skip the pocket matcher entirely, so a
    #     matching pocket can never win over the forced aggregate (the bug:
    #     pocket matching ran before the force check, so force_route="aggregate"
    #     could silently return a POCKET route).
    # When neither matcher yields a route, the ``force_route in (...)`` guard
    # near the end raises NoAggregateMatchError, per the contract.
    if force_route == "pocket":
        pocket_result = await find_best_pocket(
            bound_query,
            db,
            persona_id=str(persona.id) if persona is not None else None,
        )
        pocket = pocket_result.pocket
        pocket_skipped_reason = pocket_result.skipped_reason
    elif force_route == "aggregate":
        pocket = None
        pocket_skipped_reason = None
    else:
        # Pocket matching runs BEFORE aggregate matching. This priority is
        # load-bearing: when a query can be satisfied by both a fresh pocket and
        # a valid aggregate, pocket always wins (invariant documented in
        # docs/architecture/architecture_pocket-tables.md §5). Do not reorder.
        pocket_result = await find_best_pocket(
            bound_query,
            db,
            persona_id=str(persona.id) if persona is not None else None,
        )
        pocket = pocket_result.pocket
        pocket_skipped_reason = pocket_result.skipped_reason

    _target_dialect = _target_dialect_early

    if pocket is not None:
        pocket_dialect = await _resolve_aggregate_target_dialect(pocket, db) or _target_dialect
        rewritten = rewrite_for_pocket(bound_query, pocket, pocket_dialect)
        if rewritten == bound_query.logical_query.raw_query:
            source_sql = await rewrite_for_source(bound_query, db, target_dialect=_target_dialect)
            return RouteDecision(
                route_type="source",
                rewritten_query=source_sql,
                reason=f"Pocket {pocket.id} matched but rewrite was not safe; fell back to source",
                aggregate_id=None,
                pocket_id=None,
                pocket_skipped_reason=PocketSkipReason.REWRITE_UNSAFE,
                target_dialect=_target_dialect,
                source_dialect=_target_dialect,
            )
        return RouteDecision(
            route_type="pocket",
            rewritten_query=rewritten,
            reason=f"Matched pocket {pocket.id} (fingerprint + predicate subset)",
            aggregate_id=None,
            pocket_id=str(pocket.id),
            # F-004-14: the pocket SQL is rendered with ``pocket_dialect`` (the
            # pocket's own target connection), so the RouteDecision must carry
            # that same dialect — not the SOURCE dialect — for logging and any
            # downstream dialect-aware handling. The aggregate branch already
            # uses ``agg_dialect`` correctly; this mirrors it. Execution is
            # unaffected (the executor resolves the pocket's own connection),
            # but the propagated metadata was wrong when pocket target != source.
            target_dialect=pocket_dialect,
            source_dialect=_target_dialect,
        )

    # F-004-08: under force_route="pocket" the aggregate matcher is skipped so a
    # matching aggregate can never win over the forced (but unmatched) pocket —
    # the ``force_route in (...)`` guard below then raises rather than silently
    # returning an aggregate route the caller did not ask for.
    agg_result = None
    if force_route == "pocket":
        aggregate = None
        agg_skip_reasons = None
    else:
        agg_result = await find_best_aggregate(
            bound_query,
            db,
            persona_id=str(persona.id) if persona is not None else None,
        )
        aggregate = agg_result.aggregate
        agg_skip_reasons = agg_result.skip_reasons or None

    # Bug-5197: pass the canonical name map from the matcher to the
    # validator so filter dimension canonicalization is consistent.
    _agg_name_to_canonical = getattr(agg_result, "name_to_canonical", None) if agg_result is not None else None

    if aggregate is not None:
        valid, reason = validate_aggregate_route(
            bound_query, aggregate, name_to_canonical=_agg_name_to_canonical,
        )
        if valid:
            agg_dialect = await _resolve_aggregate_target_dialect(aggregate, db) or _target_dialect
            # Exact-where-possible (Q9): never serve an exact-semantics
            # percentile query from an aggregate whose quantile columns are
            # approximate or were never materialised. Exactness depends on BOTH
            # the SOURCE dialect the aggregation ran in AND the TARGET dialect it
            # was materialised into (Bug-987, Bug-1007):
            #   - BigQuery source -> APPROX_QUANTILES (approximate) -> not exact.
            #   - Spark source -> exact only for a SAME-ENGINE (Spark target)
            #     refresh; cross-engine refresh skips pNN materialisation, so a
            #     stale coverage row would otherwise route to a missing column.
            #   - PostgreSQL/Redshift source -> PERCENTILE_CONT exact either way.
            quantile_dialect = (
                await _resolve_aggregate_source_dialect(aggregate, db) or agg_dialect
            )
            if _query_uses_percentile(bound_query) and not quantile_materialization_is_exact(
                quantile_dialect, agg_dialect
            ):
                fallback_reason = (
                    f"Aggregate {aggregate.id} has approximate or unmaterialised "
                    f"quantile columns (source {quantile_dialect}, target {agg_dialect}); "
                    f"percentile query routed to source for exact results"
                )
            else:
                try:
                    rewritten = rewrite_for_aggregate(bound_query, aggregate, agg_dialect)
                except AggregateRewriteUnsupported as exc:
                    # Defence-in-depth: the matcher should already have refused
                    # an aggregate that cannot serve this shape, but if one
                    # reaches the rewriter (e.g. a HAVING stat the aggregate
                    # does not store, F-004-04), fall back to source rather
                    # than emitting silently-wrong SQL.
                    fallback_reason = (
                        f"Aggregate {aggregate.id} cannot serve query shape: {exc}"
                    )
                else:
                    # Bug-5195: the hit credit is deferred to the caller so
                    # it can be applied ONLY after the routed query executes
                    # successfully. ``pending_hit_credit`` carries the
                    # aggregate object; the caller calls
                    # ``record_aggregate_hit(decision.pending_hit_credit, db)``
                    # after a successful execute and skips it on failure.
                    # All execution endpoints (routes.py execute_with_observation,
                    # which plugin.py and headless.py both delegate to) consume
                    # ``pending_hit_credit`` post-success — no pre-execution
                    # credit here.
                    return RouteDecision(
                        route_type="aggregate",
                        rewritten_query=rewritten,
                        reason=f"Matched aggregate {aggregate.id} (grain={aggregate.grain})",
                        aggregate_id=str(aggregate.id),
                        pocket_id=None,
                        pocket_skipped_reason=pocket_skipped_reason,
                        target_dialect=agg_dialect,
                        source_dialect=_target_dialect,
                        pending_hit_credit=aggregate,
                    )
        else:
            # Validation failed — fall through to source with the validation reason
            fallback_reason = f"Aggregate {aggregate.id} rejected: {reason}"
    else:
        fallback_reason = "No active aggregate covers the requested grain and measures"

    if force_route in ("aggregate", "pocket"):
        raise NoAggregateMatchError(
            f"force_route={force_route!r} but no matching aggregate or pocket was found. "
            "Remove force_route or create an aggregate for this query shape."
        )

    rewritten = await rewrite_for_source(bound_query, db, target_dialect=_target_dialect)
    return RouteDecision(
        route_type="source",
        rewritten_query=rewritten,
        reason=fallback_reason,
        aggregate_id=None,
        pocket_id=None,
        pocket_skipped_reason=pocket_skipped_reason,
        aggregate_skipped_reasons=agg_skip_reasons,
        target_dialect=_target_dialect,
    )


async def _route_with_row_security(
    bound_query: BoundQuery,
    db: AsyncSession,
    compiled: CompiledPredicate,
    *,
    target_dialect: str | None = None,
) -> RouteDecision:
    """Force source route and apply the security predicate.

    Matcher bypass is not a performance choice — aggregates and pockets
    pre-compute rows without knowing which audience will read them, so
    letting either path run would leak rows across audiences. Phase 9
    can revisit per-audience aggregate materialisation.

    The security predicate is injected into the WHERE clause of **every**
    SELECT that scans a physical table (F-007-01), so UNION branches,
    scalar subqueries, subquery-first FROMs and CTE bodies are all
    constrained, and the predicate always applies before any LIMIT
    (Bug-915). Shapes that cannot be safely constrained are rejected
    with 403 — never executed unfiltered.
    """
    rewritten = await rewrite_for_source(bound_query, db, target_dialect=target_dialect)

    final_sql = _inject_security_where(
        rewritten, compiled, dialect=target_dialect or "postgres",
    )

    rules_list = ", ".join(compiled.active_rule_ids)
    return RouteDecision(
        route_type="source",
        rewritten_query=final_sql,
        reason=(
            f"Row security active ({len(compiled.active_rule_ids)} rule(s): "
            f"{rules_list}); aggregate + pocket matchers bypassed"
        ),
        aggregate_id=None,
        pocket_id=None,
        security_rules_applied=list(compiled.applied_rules) if compiled.applied_rules else None,
        target_dialect=target_dialect,
    )


def _reject_row_security_shape(reason: str):
    """Fail closed: refuse to run a query whose shape cannot be safely
    constrained by the row-security predicate (F-007-01)."""
    from fastapi import HTTPException

    raise HTTPException(
        status_code=403,
        detail={
            "error_code": "row_security_unsupported_shape",
            "message": (
                "This query cannot be safely constrained by the active "
                f"row-level security rules ({reason}). Rewrite it as a "
                "plain SELECT (or a UNION of plain SELECTs) over the model."
            ),
        },
    )


def _inject_security_where(
    sql: str, compiled: CompiledPredicate, dialect: str = "postgres",
) -> str:
    """Inject the security predicate into every SELECT that scans a table.

    F-007-01 fix — design: **per-scan injection, fail closed.** The old
    implementation ANDed the predicate into the *first* WHERE found
    depth-first, which mis-scoped or skipped filtering on UNION, scalar
    subquery and subquery-first FROM shapes. Now:

    * Every ``exp.Table`` scan that is not a CTE reference is resolved to
      its nearest enclosing SELECT, and each such SELECT gets the
      predicate ANDed into its own WHERE (created when absent). Every
      physical scan lives in exactly one nearest enclosing SELECT, so no
      branch of any set operation, subquery or CTE escapes the filter.
    * Injecting inside the same SELECT as the scan keeps the predicate
      ahead of LIMIT (Bug-915 stays fixed: ``LIMIT N`` returns up to N
      *allowed* rows).
    * Predicates are column-name scoped by design (same semantics the
      original subquery wrap had): scopes where the security column is
      not visible fail in the database ("column does not exist") rather
      than running unfiltered, and over-filtering a non-protected table
      that shares the column name can only narrow, never widen, results.
    * Parse failures, scans outside any SELECT, or zero injection sites
      reject with 403 (no silent subquery-wrap fallback — the wrap
      re-broke LIMIT semantics and could not fix scalar-subquery leaks).

    F-007-03 fix (in passing): parse and render use the *target* dialect
    instead of hard-coded postgres, so injection actually runs for
    non-PostgreSQL targets instead of silently falling back to the wrap.

    Bug-1070 fix: the predicate is combined with an existing WHERE via
    sqlglot's precedence-aware ``exp.and_`` (which parenthesizes operand
    connectors) instead of a raw ``exp.And`` node. A raw AND around a
    top-level ``OR`` WHERE rendered as ``A OR B AND pred``, which the
    database parses as ``A OR (B AND pred)`` — every row matching ``A``
    bypassed the security filter.

    Bug-1071 fix: scan-vs-CTE discrimination is alias-scope-aware via
    sqlglot scope analysis instead of bare-name matching against the
    statement-wide CTE alias set. Bare-name matching wrongly excluded a
    *physical* scan whose unqualified name collides with a CTE alias
    (e.g. the scan inside ``WITH sales AS (SELECT … FROM sales)``).
    Any table reference scope analysis cannot account for is rejected
    with 403 — never executed unfiltered.
    """
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.scope import Scope, traverse_scope

    try:
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        _reject_row_security_shape("the SQL could not be parsed for predicate injection")

    try:
        security_ast = sqlglot.parse_one(
            f"SELECT 1 WHERE {compiled.sql_expression}", read=dialect
        )
        security_where = security_ast.find(exp.Where)
    except Exception:
        security_where = None
    if security_where is None:
        _reject_row_security_shape("the security predicate could not be parsed")
    predicate = security_where.this

    # Scope-resolve every table reference (Bug-1071). A reference whose
    # scope source is another Scope is a CTE / derived-table read — its
    # body is its own scope and is constrained there, so the reference
    # itself needs no (and may not support a) second filter. A reference
    # whose source is the physical ``exp.Table`` is a scan to constrain.
    try:
        scopes = traverse_scope(ast)
    except Exception:
        _reject_row_security_shape(
            "the SQL could not be scope-analysed for predicate injection"
        )

    # Collect injection targets BEFORE mutating the tree so the copies of
    # the predicate (which may contain its own subquery, e.g. user_mapping
    # rules) are never re-walked.
    targets: list[exp.Select] = []
    seen: set[int] = set()
    visited: set[int] = set()
    for scope in scopes:
        for table in scope.tables:
            visited.add(id(table))
            source = scope.sources.get(table.alias_or_name)
            if isinstance(source, Scope):
                continue  # CTE / derived-table reference, not a scan
            if not isinstance(source, exp.Table):
                _reject_row_security_shape(
                    f"table reference {table.name!r} could not be resolved "
                    "to a physical scan"
                )
            select = table.find_ancestor(exp.Select)
            if select is None:
                _reject_row_security_shape(
                    f"table {table.name!r} is scanned outside any SELECT scope"
                )
            if id(select) not in seen:
                seen.add(id(select))
                targets.append(select)

    # Fail-closed sweep: any table node scope analysis did not visit
    # (INSERT/CTAS targets, non-SELECT statements, exotic positions) must
    # reject rather than run unfiltered.
    for table in ast.find_all(exp.Table):
        if id(table) not in visited:
            _reject_row_security_shape(
                f"table {table.name!r} is scanned outside any SELECT scope"
            )

    if not targets:
        _reject_row_security_shape("no table scan found to constrain")

    for select in targets:
        existing_where = select.args.get("where")
        if existing_where is not None:
            # exp.and_ parenthesizes connector operands, so a top-level
            # OR in the user's WHERE cannot absorb the predicate into
            # one branch (Bug-1070).
            combined = exp.and_(
                existing_where.this, predicate.copy(), copy=False
            )
            existing_where.set("this", combined)
        else:
            select.set("where", exp.Where(this=predicate.copy()))

    return ast.sql(dialect=dialect)


async def _record_query_fallback_alert(
    bound_query: BoundQuery,
    db: AsyncSession,
) -> None:
    """Record one ``query_fallback`` alert per invalid object touched.

    Dedup is handled at the database layer, so repeated fallbacks for
    the same object just bump ``occurrence_count``. Wrapped in a
    broad try/except so alert-write failures never block a query.
    """
    try:
        from shared.semantic.model_alerts import (
            CATEGORY_QUERY_FALLBACK,
            OBJECT_DIMENSION,
            OBJECT_MEASURE,
            SEVERITY_WARNING,
            record_alert,
        )
    except Exception as import_exc:
        logger.warning(
            "query_fallback alert module unavailable for model %s: %s",
            getattr(bound_query.model, "id", "?"),
            import_exc,
        )
        return

    dims_by_name = dict(getattr(bound_query, "resolved_dimensions_by_name", {}) or {})
    for dim in bound_query.resolved_dimensions:
        dims_by_name.setdefault(dim.name, dim)
    measures_by_name = {m.name: m for m in bound_query.resolved_measures}

    raw_sql = (getattr(bound_query.logical_query, "raw_query", "") or "")[:500]

    for kind, name in bound_query.uses_invalid_objects:
        try:
            if kind == "dimension":
                obj = dims_by_name.get(name)
                obj_id = getattr(obj, "id", None) if obj else None
                await record_alert(
                    db,
                    model_id=bound_query.model.id,
                    severity=SEVERITY_WARNING,
                    category=CATEGORY_QUERY_FALLBACK,
                    title=f"Query used invalid dimension: {name}",
                    detail=raw_sql,
                    related_object_type=OBJECT_DIMENSION,
                    related_object_id=obj_id,
                )
            elif kind == "measure":
                obj = measures_by_name.get(name)
                obj_id = getattr(obj, "id", None) if obj else None
                await record_alert(
                    db,
                    model_id=bound_query.model.id,
                    severity=SEVERITY_WARNING,
                    category=CATEGORY_QUERY_FALLBACK,
                    title=f"Query used invalid measure: {name}",
                    detail=raw_sql,
                    related_object_type=OBJECT_MEASURE,
                    related_object_id=obj_id,
                )
        except Exception as alert_exc:
            # Never let an alert write block the user query — but log
            # the failure so the operator notices if every attempt
            # is failing (e.g. a broken connection or a migration
            # that hasn't run).
            logger.warning(
                "query_fallback alert failed for %s:%s on model %s: %s",
                kind,
                name,
                getattr(bound_query.model, "id", "?"),
                alert_exc,
            )
            continue


async def _ensure_valid_user_defined_attributes(
    bound_query: BoundQuery,
    db: AsyncSession,
) -> None:
    """Fail fast when the query references invalidated UDAs."""
    uda_ids: set[object] = set()

    for dim in bound_query.resolved_dimensions:
        uda_id = getattr(dim, "user_defined_attribute_id", None)
        if uda_id:
            uda_ids.add(uda_id)

    for measure in bound_query.resolved_measures:
        uda_id = getattr(measure, "user_defined_attribute_id", None)
        if uda_id:
            uda_ids.add(uda_id)

    dims_by_name = dict(
        getattr(bound_query, "resolved_dimensions_by_name", {}) or {}
    )
    if not dims_by_name:
        dims_by_name = {d.name: d for d in bound_query.resolved_dimensions}

    filter_names = {f.dimension_name for f in bound_query.resolved_filters}
    unresolved_filter_names: set[str] = set()
    for name in filter_names:
        dim = dims_by_name.get(name)
        uda_id = getattr(dim, "user_defined_attribute_id", None) if dim is not None else None
        if uda_id:
            uda_ids.add(uda_id)
        elif dim is None:
            unresolved_filter_names.add(name)

    if unresolved_filter_names:
        result = await db.execute(
            select(Dimension.user_defined_attribute_id).where(
                Dimension.model_id == bound_query.model.id,
                Dimension.name.in_(unresolved_filter_names),
                Dimension.user_defined_attribute_id.is_not(None),
            )
        )
        uda_ids.update(row[0] for row in result.fetchall() if row[0] is not None)

    if not uda_ids:
        return

    result = await db.execute(
        select(UserDefinedAttribute).where(UserDefinedAttribute.id.in_(uda_ids))
    )
    for uda in result.scalars().all():
        if uda.validated is False:
            detail = uda.validation_error or "validation failed"
            raise ValueError(f"User-defined attribute '{uda.name}' is invalid: {detail}")


class _ClsClosure:
    """Lookup context for the CLS column-closure checks (F-008-06).

    Built lazily by ``_build_cls_closure`` — each map is only populated
    when the resolved objects actually need it, so the common path (plain
    source-column measures/dimensions) adds no extra queries.
    """

    __slots__ = (
        "restricted_uda_ids",
        "measures_by_id",
        "measures_by_name",
        "restricted_physical_names",
    )

    def __init__(self):
        self.restricted_uda_ids: set[str] = set()
        self.measures_by_id: dict = {}
        self.measures_by_name: dict = {}
        self.restricted_physical_names: set[str] | None = None


async def _build_cls_closure(
    bound_query: BoundQuery,
    restricted_col_ids: list,
    db: AsyncSession,
) -> _ClsClosure:
    """Preload the lookups needed to expand object column closures."""
    from shared.db.models import Measure, UserDefinedAttributeColumnRef

    ctx = _ClsClosure()
    objs = list(bound_query.resolved_measures) + list(bound_query.resolved_dimensions)

    needs_measures = any(
        getattr(m, "measure_type", None) == "calculated"
        or getattr(m, "variant_of_measure_id", None) is not None
        for m in bound_query.resolved_measures
    )
    # Calculated measures can reference UDA-backed measures, so the UDA
    # closure is needed whenever the measure map is.
    needs_uda = needs_measures or any(
        getattr(o, "user_defined_attribute_id", None) is not None for o in objs
    )
    needs_phys_names = any(
        getattr(d, "calc_expression", None) for d in bound_query.resolved_dimensions
    )

    if needs_uda:
        uda_rows = (
            await db.execute(
                select(UserDefinedAttributeColumnRef.attribute_id)
                .where(UserDefinedAttributeColumnRef.column_id.in_(restricted_col_ids))
            )
        ).scalars().all()
        ctx.restricted_uda_ids = {str(a) for a in uda_rows}

    if needs_measures:
        meas_rows = (
            await db.execute(
                select(Measure).where(Measure.model_id == bound_query.model.id)
            )
        ).scalars().all()
        for meas in meas_rows:
            ctx.measures_by_id[str(meas.id)] = meas
            ctx.measures_by_name[meas.name] = meas

    if needs_phys_names:
        ctx.restricted_physical_names = await _restricted_physical_names(
            restricted_col_ids, db,
        )

    return ctx


async def _restricted_physical_names(
    restricted_col_ids: list, db: AsyncSession,
) -> set[str]:
    phys_rows = (
        await db.execute(
            select(ModelColumn.column_name)
            .where(ModelColumn.id.in_(restricted_col_ids))
        )
    ).scalars().all()
    return {str(n).lower() for n in phys_rows}


def _touches_restricted_columns(
    obj,
    restricted_ids: set[str],
    ctx: _ClsClosure,
    _visited: set[str] | None = None,
) -> bool:
    """True when the object's column closure intersects the restricted set.

    Closure rules (F-008-06):
    * direct ``source_column_id`` membership;
    * UDA-backed objects — the UDA's materialised column refs;
    * variant measures — the base measure's closure;
    * calculated measures — every ``measure("name")`` reference's closure
      (fail closed when the expression cannot be parsed);
    * calculated dimensions — physical column names referenced by the
      expression (the rewriter qualifies them by name, so name-level
      matching mirrors what would execute; fail closed on parse errors).
    """
    src = getattr(obj, "source_column_id", None)
    if src is not None and str(src) in restricted_ids:
        return True

    uda_id = getattr(obj, "user_defined_attribute_id", None)
    if uda_id is not None and str(uda_id) in ctx.restricted_uda_ids:
        return True

    if _visited is None:
        _visited = set()

    base_id = getattr(obj, "variant_of_measure_id", None)
    if base_id is not None:
        base = ctx.measures_by_id.get(str(base_id))
        if base is not None and str(base_id) not in _visited:
            _visited.add(str(base_id))
            if _touches_restricted_columns(base, restricted_ids, ctx, _visited):
                return True

    if getattr(obj, "measure_type", None) == "calculated":
        from shared.semantic.calculated_expression import parse_expression

        try:
            parsed = parse_expression(getattr(obj, "expression", None) or "")
            ref_names = [r.name for r in parsed.references]
        except Exception:
            # Cannot enumerate the expression's column closure — fail
            # closed: an unverifiable object is treated as restricted.
            return True
        for ref_name in ref_names:
            ref = ctx.measures_by_name.get(ref_name)
            if ref is None:
                continue
            ref_key = str(getattr(ref, "id", ref_name))
            if ref_key in _visited:
                continue
            _visited.add(ref_key)
            if _touches_restricted_columns(ref, restricted_ids, ctx, _visited):
                return True

    calc_expr = getattr(obj, "calc_expression", None)
    if calc_expr and ctx.restricted_physical_names:
        import sqlglot
        from sqlglot import exp as _sg_exp

        try:
            tree = sqlglot.parse_one(calc_expr, read="postgres")
        except Exception:
            return True
        for node in tree.find_all(_sg_exp.Column):
            if node.name and node.name.lower() in ctx.restricted_physical_names:
                return True

    return False


async def _cls_restricted_detail(
    columns: list[str],
    persona: Persona,
    db: AsyncSession,
) -> dict:
    """Build the CLS 403 payload (F-008-19).

    Consistent error code (``COLUMN_RESTRICTED``, upper-snake to match the
    persona gate's ``PERSONA_OBJECT_NOT_INCLUDED``), the persona name, and
    the names of the data tags that restrict this persona, so a client can
    show the documented message: "Column 'x' is restricted by tag 'PII'
    for persona 'External Partner'." Tag-name resolution failures degrade
    gracefully (the denial still stands; only the names go missing).
    """
    from shared.db.models import DataTag

    tag_names: list[str] = []
    try:
        tag_names = list(
            (
                await db.execute(
                    select(DataTag.tag_name)
                    .join(
                        PersonaTagRestriction,
                        PersonaTagRestriction.data_tag_id == DataTag.id,
                    )
                    .where(PersonaTagRestriction.persona_id == persona.id)
                    .order_by(DataTag.tag_name)
                )
            ).scalars().all()
        )
    except Exception:  # pragma: no cover - name enrichment is best-effort
        tag_names = []

    tag_clause = f" by tag(s) {', '.join(repr(t) for t in tag_names)}" if tag_names else ""
    cols_clause = ", ".join(repr(c) for c in columns)
    persona_name = getattr(persona, "name", None) or str(persona.id)
    return {
        "error_code": "COLUMN_RESTRICTED",
        "message": (
            f"Column(s) {cols_clause} are restricted{tag_clause} for "
            f"persona {persona_name!r}."
        ),
        "columns": columns,
        "tags": tag_names,
        "persona_id": str(persona.id),
        "persona_name": persona_name,
    }


async def _restricted_filter_columns(
    bound_query: BoundQuery,
    restricted_ids: set[str],
    ctx: _ClsClosure,
    restricted_col_rows: list,
    db: AsyncSession,
) -> list[str]:
    """Filter names that reach a restricted column (F-008-11).

    A tag-restricted column whose values are the secret must not be usable
    as a value-probing oracle: ``WHERE salary > 100000`` on an allowed
    measure leaks ``salary`` one bisection at a time. Registry Bug-884
    holds that a filter "can never expand visibility" for persona ALLOW
    lists, but that rationale does not extend to per-column PII
    restrictions, where the column's values are exactly what must be hidden
    — so filters are checked too, fail closed.

    Resolution per filter ``dimension_name``:
      * resolve the name to a Dimension ORM row (the binder's
        ``resolved_dimensions_by_name`` first, then the resolved-dimension
        list) and run the full column closure;
      * if the name does not resolve to a known dimension, fall back to a
        physical-name match against the restricted columns — a raw column
        name in the WHERE that hits a restricted physical column is blocked
        rather than executed.
    """
    filters = getattr(bound_query, "resolved_filters", None) or []
    if not filters:
        return []

    dims_by_name = dict(getattr(bound_query, "resolved_dimensions_by_name", {}) or {})
    for d in bound_query.resolved_dimensions:
        dims_by_name.setdefault(getattr(d, "name", None), d)

    if ctx.restricted_physical_names is None:
        ctx.restricted_physical_names = await _restricted_physical_names(
            list(restricted_col_rows), db,
        )
    restricted_phys = ctx.restricted_physical_names or set()

    blocked: list[str] = []
    for f in filters:
        name = getattr(f, "dimension_name", None)
        if not name:
            continue
        dim = dims_by_name.get(name)
        if dim is not None:
            if _touches_restricted_columns(dim, restricted_ids, ctx):
                blocked.append(name)
            continue
        # Unresolved name — could be a raw physical column in the WHERE.
        # Match it by physical name and fail closed if it hits a
        # restricted column.
        if str(name).lower() in restricted_phys:
            blocked.append(name)
    return blocked


async def _check_column_restrictions(
    bound_query: BoundQuery,
    persona: Persona,
    db: AsyncSession,
) -> list[str]:
    """Enforce persona tag restrictions (column-level security).

    Returns the list of restricted object names the query explicitly
    requests (the caller raises 403), or ``[]`` when nothing is blocked.
    Enforcement covers the full column closure of every resolved object —
    direct source columns, UDA-backed objects, variant measures, and
    calculated measures/dimensions whose expressions reach a restricted
    column (F-008-06).

    Additional behaviours when restrictions exist for the persona:

    * Complex SQL (F-008-03): CTEs/subqueries/window functions skip
      binder column resolution, so the restriction cannot be evaluated
      column-by-column. Security fails CLOSED — the query is rejected
      here rather than executed unrestricted with service credentials.
    * ``SELECT *`` (F-008-05): restricted columns are silently dropped
      from the resolved lists (mirroring the persona allow-list gate's
      star narrowing) so BI tools get the permitted columns instead of
      a hard 403 on the whole table. Explicit requests still 403. When
      narrowing would leave no projectable object at all, the query is
      rejected immediately instead of falling through to the raw-star
      rewrite fallback and the post-execute audit (Bug-809 shape).
    """
    restrictions = (
        await db.execute(
            select(PersonaTagRestriction.data_tag_id)
            .where(PersonaTagRestriction.persona_id == persona.id)
        )
    ).scalars().all()

    if not restrictions:
        return []

    restricted_col_rows = (
        await db.execute(
            select(data_tag_columns.c.model_column_id)
            .where(data_tag_columns.c.tag_id.in_(restrictions))
        )
    ).scalars().all()

    if not restricted_col_rows:
        return []

    restricted_ids = {str(c) for c in restricted_col_rows}
    lq = bound_query.logical_query

    if getattr(lq, "has_complex_sql", False):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "COLUMN_RESTRICTED",
                "message": (
                    "This query uses advanced SQL constructs (for example "
                    "subqueries, CTEs, set operations, window functions, or "
                    "non-standard aggregates) that cannot be verified against "
                    "this persona's column restrictions, so it was rejected. "
                    "Rewrite the query as a plain SELECT over the model."
                ),
                "persona_id": str(persona.id),
                "persona_name": getattr(persona, "name", None) or str(persona.id),
            },
        )

    ctx = await _build_cls_closure(bound_query, list(restricted_col_rows), db)

    if getattr(lq, "select_star", False):
        kept_measures = [
            m for m in bound_query.resolved_measures
            if not _touches_restricted_columns(m, restricted_ids, ctx)
        ]
        kept_dimensions = [
            d for d in bound_query.resolved_dimensions
            if not _touches_restricted_columns(d, restricted_ids, ctx)
        ]
        narrowed = (
            len(kept_measures) != len(bound_query.resolved_measures)
            or len(kept_dimensions) != len(bound_query.resolved_dimensions)
        )
        if narrowed:
            if not kept_measures and not kept_dimensions:
                # M-2 (Bug-809 shape): an empty narrowed projection would
                # fall back to a raw SELECT * against the source, which the
                # post-execute audit then blocks with an opaque error AFTER
                # the restricted values were read. Reject cleanly up front.
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error_code": "COLUMN_RESTRICTED",
                        "message": (
                            "Every column this star query resolves to is "
                            "restricted for this persona, so there is "
                            "nothing left to return."
                        ),
                        "persona_id": str(persona.id),
                        "persona_name": getattr(persona, "name", None) or str(persona.id),
                    },
                )
            bound_query.resolved_measures = kept_measures
            bound_query.resolved_dimensions = kept_dimensions
            bound_query.persona_narrowed_star = True
            # Keep the post-execute audit whitelist consistent: physical
            # names of restricted columns must no longer be authorised.
            if ctx.restricted_physical_names is None:
                ctx.restricted_physical_names = await _restricted_physical_names(
                    list(restricted_col_rows), db,
                )
            allowed_phys = getattr(bound_query, "allowed_physical_columns", None)
            if allowed_phys:
                bound_query.allowed_physical_columns = {
                    c for c in allowed_phys
                    if str(c).lower() not in ctx.restricted_physical_names
                }
        # F-008-11: a SELECT * is silently narrowed, but a WHERE on a
        # restricted column is still a value-probing oracle — it must 403
        # even when the projection narrows cleanly. Filters are never
        # "narrowed away".
        filter_blocked = await _restricted_filter_columns(
            bound_query, restricted_ids, ctx, list(restricted_col_rows), db,
        )
        return filter_blocked

    blocked: list[str] = []
    for m in bound_query.resolved_measures:
        if _touches_restricted_columns(m, restricted_ids, ctx):
            blocked.append(getattr(m, "name", str(getattr(m, "id", "?"))))
    for d in bound_query.resolved_dimensions:
        if _touches_restricted_columns(d, restricted_ids, ctx):
            blocked.append(getattr(d, "name", str(getattr(d, "id", "?"))))
    # F-008-11: block filter-only references to restricted columns
    # (value-probing oracle) — fail closed.
    blocked.extend(
        await _restricted_filter_columns(
            bound_query, restricted_ids, ctx, list(restricted_col_rows), db,
        )
    )

    return blocked


async def _resolve_aggregate_target_dialect(
    aggregate, db: AsyncSession,
) -> str | None:
    """Resolve the SQL dialect from the aggregate/pocket target connection."""
    tid = getattr(aggregate, "target_id", None)
    if tid is None:
        return None
    target = await db.get(DataTarget, tid)
    if target is None:
        return None
    conn = await db.get(ProjectConnection, target.project_connection_id)
    if conn is None:
        return None
    from shared.schemas.connection_type import normalize_connection_type
    ct = normalize_connection_type((conn.connection_type or "").lower())
    return dialect_from_connection_type(ct)


async def _resolve_aggregate_source_dialect(
    aggregate, db: AsyncSession,
) -> str | None:
    """Resolve the SQL dialect of the aggregate's SOURCE connection.

    Quantile exactness is determined by the engine that computed the column
    during refresh (the source), not the target table it was stored in
    (Bug-987). Returns None when the source cannot be resolved (caller falls
    back to the target dialect).
    """
    model_id = getattr(aggregate, "model_id", None)
    if model_id is None:
        return None
    try:
        from shared.aggregate_connection import resolve_source_connection

        conn = await resolve_source_connection(model_id, db)
    except Exception:
        return None
    if conn is None:
        return None
    from shared.schemas.connection_type import normalize_connection_type

    ct = normalize_connection_type((conn.connection_type or "").lower())
    return dialect_from_connection_type(ct)
