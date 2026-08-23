"""
Router — combines matching + validation into a single RouteDecision,
then delegates logging.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

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
    is_quantile_agg_token,
    quantile_materialization_is_exact,
)
# Bug-8121: imported at module scope ON PURPOSE. The only use is inside the
# audit-failure handler in ``_record_rls_bypass_audit``, whose entire contract
# is that an audit-write failure must NOT block the query. A deferred import
# there would put a second, unguarded failure mode (ImportError, prometheus
# duplicate registration) inside the handler that exists to swallow failures,
# and ``_record_rls_bypass_audit`` is awaited with no try/except around it --
# so a raise would fail the query for exactly the RLS-bypass personas whose
# audit trail just went missing.
from shared.metrics import RLS_BYPASS_AUDIT_FAILURES
from src.ir.logical_query import BoundQuery, NoAggregateMatchError, RouteDecision, UnsupportedSQL
from src.routing.aggregate_matcher import find_best_aggregate, record_aggregate_miss
from src.routing.aggregate_generation_guard import aggregate_generation_of
from src.routing.aggregate_population import AggregatePopulationChecker
from src.routing.pocket_matcher import PocketSkipReason, find_best_pocket
from src.routing.pocket_generation_guard import pocket_generation_of
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
from src.rewrite.raw_sql import RawRouteUnsupported, rewrite_for_raw
from src.rewrite.pocket import PocketRewriteUnsupported
from src.security import (
    CompiledPredicate,
    Principal,
    compile_row_security,
    has_active_rules,
    render_predicate_for_dialect,
)

if TYPE_CHECKING:
    from src.routing.derived_expression_proof import MeasureRequest as DerivedMeasureRequest

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
    inert: bool = False,
) -> None:
    """Persist a structured platform audit event for a persona carrying
    ``bypass_row_security=true``.

    Two cases:

    * ``inert=False`` (the flag actually skipped active rules): a WARN event
      recording the bypass — WHO bypassed (actor identity), on WHICH model,
      under WHICH persona, and the COUNT/identity of the skipped rules
      (F-008-24). An INFO service log alone is not durable, tenant-scoped, or
      visible in the audit trail.

    * ``inert=True`` (Bug-7046): the persona carries the flag but the model has
      NO active RLS rules for this principal, so the flag does nothing. It is
      still a security-relevant configuration, so record an INFO event with
      ``rules_skipped: []`` and a note that the model carries no active rules.
      This keeps the audit trail self-contained — a security auditor reviewing
      bypass events sees the inert flag without cross-referencing persona config.

    Wrapped so an audit-write failure never blocks the query — the bypass
    decision and its authorization are unchanged.
    """
    try:
        model = bound_query.model
        logical = bound_query.logical_query
        detail = {
            "persona_id": str(persona.id),
            "persona_name": getattr(persona, "name", ""),
            "rules_skipped": rule_ids,
            "rule_count": len(rule_ids),
            "query_fingerprint": (getattr(logical, "query_fingerprint", "") or "")[:16],
            "protocol": getattr(logical, "protocol", ""),
        }
        if inert:
            # Bug-7046: surface the inert-but-present bypass flag so the audit
            # trail is self-contained.
            detail["inert"] = True
            detail["note"] = (
                "persona carries bypass_row_security=true but the model has no "
                "active row-security rules for this principal; the flag is inert"
            )
        await audit(
            db,
            action="query.rls_bypass",
            severity="info" if inert else "warn",
            actor_email=principal.user_identity if principal is not None else None,
            target_type="model",
            target_id=_uuid_or_none(getattr(model, "id", None)),
            target_name=(
                getattr(model, "display_name", None)
                or getattr(model, "slug", None)
                or str(getattr(model, "id", ""))
            ),
            detail=detail,
        )
    except Exception:
        logger.warning("Failed to persist RLS-bypass audit event", exc_info=True)
        # Bug-8121: the audit-write failure must not block the query (correct),
        # but the gap must be observable. Increment a monitored failure counter
        # so operators can detect when the durable audit trail is incomplete.
        RLS_BYPASS_AUDIT_FAILURES.inc()


def _measure_is_calc(m: object) -> bool:
    return (
        getattr(m, "measure_type", None) == "calculated"
        and (getattr(m, "calc_agg_mode", None) or "expression_as_written")
        == "expression_as_written"
    )


def _calc_expression_refs(m: object) -> set[str] | None:
    """Parse a calculated measure's expression to its referenced measure names.

    Returns the set of reference names, or ``None`` when the expression is
    absent/blank or cannot be parsed — in either case the caller must fail
    closed. A calculated measure with an empty/whitespace expression is
    malformed (create-time validation forbids it, but a legacy or corrupt
    snapshot row is hydrated directly into an ORM object bypassing that gate);
    we cannot prove it is quantile-free, so it must fire the gate (Codex R3
    finding 3), not silently report "no dependencies".
    """
    expr = getattr(m, "expression", None) or ""
    if not expr.strip():
        return None
    try:
        from shared.semantic.calculated_expression import parse_expression

        parsed = parse_expression(expr)
        return {r.name for r in parsed.references}
    except Exception:
        return None


async def _query_uses_percentile(bound_query: BoundQuery, db: AsyncSession) -> bool:
    """True if the query requests a quantile through ANY semantic path.

    Bug-7779 (HIGH): the original check inspected only SELECT ``agg_function``,
    so a quantile arriving via a measure ``default_agg`` (a bare ``SELECT
    med_x`` where ``med_x`` has ``default_agg='p50'``), via a HAVING percentile,
    or via a calculated measure (at any nesting depth) whose expanded base stat
    is a quantile skipped the dialect-exactness gate entirely — a BigQuery-
    source aggregate served its ``APPROX_QUANTILES`` column in exact mode. The
    inventory must be SEMANTIC (spec I1): built from resolved measures, HAVING,
    and the COMPLETE expanded dependency DAG, never from surface syntax alone.

    This gate answers one question — "does this query read a quantile value at
    all?" — so that the exactness gate fires. It is deliberately conservative:
    ANY doubt (unparseable expression, missing dependency row, DB failure,
    unparseable HAVING) returns True and the query is held to exact-percentile
    evidence. It never fails open.
    """
    lq = bound_query.logical_query

    # (1) Explicit SELECT quantile agg functions (MEDIAN, PERCENTILE_*).
    if any(
        is_quantile_stat_type(getattr(e, "agg_function", None))
        for e in getattr(lq, "select_expressions", []) or []
    ):
        return True

    # (2) HAVING quantile aggregates. Reuse the matcher's HAVING parser so the
    #     inventory and the coverage check agree (Codex R1 finding 2). An
    #     unparseable HAVING yields the ``__unparseable__`` sentinel -> fail
    #     closed by firing the gate. The parser emits the sqlglot function key
    #     for a percentile in HAVING (``median`` / ``percentilecont`` /
    #     ``percentiledisc``), which is NOT a pNN suffix, so recognise those
    #     quantile function keys explicitly in addition to pNN stats.
    having_raw = getattr(lq, "having_raw", None)
    if having_raw:
        from src.routing.aggregate_matcher import _having_aggregate_pairs

        for _meas, _stat in _having_aggregate_pairs(having_raw):
            if _stat == "__unparseable__":
                return True
            if is_quantile_agg_token(_stat):
                return True

    resolved = getattr(bound_query, "resolved_measures", None) or []

    # (3) A resolved measure whose default_agg is a quantile stat (p01..p99, or
    #     the legacy ``median`` token — is_quantile_agg_token catches both). An
    #     explicit query aggregation overrides default_agg (its function was
    #     already checked in (1)/(2)); skip default_agg for such a measure so an
    #     explicit ``SUM(latency)`` over a p50-default measure is not needlessly
    #     held to exact-percentile evidence (Codex R3 finding 2).
    from src.routing.aggregate_matcher import _explicitly_aggregated_measure_names

    _explicit_names = _explicitly_aggregated_measure_names(bound_query)
    if any(
        (getattr(m, "name", None) or "").lower() not in _explicit_names
        and is_quantile_agg_token(getattr(m, "default_agg", None))
        for m in resolved
    ):
        return True

    # (4) Complete recursive traversal of the calculated-measure dependency DAG.
    #     A precomputed calculated column can match an aggregate by name
    #     (aggregate_matcher.py), so a nested calc whose deep base stat is a
    #     quantile MUST fire the gate even when the intermediate calc is not
    #     itself a quantile. One-level lookup is insufficient — traverse fully
    #     with cycle detection, failing closed on any unresolved/unparseable
    #     dependency (Codex R1 finding 1).
    resolved_by_name = {getattr(m, "name", None): m for m in resolved}
    pending: set[str] = set()
    for m in resolved:
        if not _measure_is_calc(m):
            continue
        refs = _calc_expression_refs(m)
        if refs is None:
            return True  # unparseable calc -> fail closed
        pending |= refs

    if not pending:
        return False

    # Dependency authority is the DEPLOYED SNAPSHOT the binder pins, NOT live
    # draft ORM rows (Codex R2 finding 2). A live draft that lowers a deployed
    # quantile base to ``sum`` must NOT make the gate return False while the
    # aggregate still stores the deployed quantile — that would serve an
    # approximate value in exact mode under version skew. Fall back to live ORM
    # only when no deployed shape is available (undeployed model / missing
    # snapshot), and fail closed if a dependency resolves in neither.
    snapshot_by_name: dict[str, object] = {}
    try:
        from src.semantic.snapshot_resolver import resolve_deployed_shape

        shape = await resolve_deployed_shape(bound_query.model, db)
        if shape is not None:
            snapshot_by_name = {
                getattr(mm, "name", None): mm for mm in getattr(shape, "measures", []) or []
            }
    except Exception:
        # Cannot resolve the pinned shape -> cannot prove the DAG is
        # quantile-free from the authoritative source; fail closed.
        return True

    from shared.db.models import Measure as _MeasureModel

    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue  # cycle / already-visited guard
        seen.add(name)

        # Prefer the query's resolved measures, then the deployed snapshot; only
        # fall back to a live ORM read when no deployed shape exists at all.
        node = resolved_by_name.get(name) or snapshot_by_name.get(name)
        if node is None:
            if snapshot_by_name:
                # The authoritative deployed shape does not contain this
                # dependency -> cannot prove it quantile-free; fail closed.
                return True
            try:
                result = await db.execute(
                    select(_MeasureModel).where(
                        _MeasureModel.model_id == bound_query.model.id,
                        _MeasureModel.name == name,
                    )
                )
                node = result.scalars().first()
            except Exception:
                # DB failure resolving a dependency: fail closed.
                return True
            if node is None:
                # Missing dependency row: cannot prove the DAG is quantile-free.
                return True

        if is_quantile_agg_token(getattr(node, "default_agg", None)):
            return True
        if _measure_is_calc(node):
            refs = _calc_expression_refs(node)
            if refs is None:
                return True  # unparseable nested calc -> fail closed
            pending |= refs - seen

    return False


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
    row_security: CompiledPredicate | None = None,
    persist_population_observation: bool = True,
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
    matching and rewrites against the source directly. The row-security
    predicate is still injected into every scan of that source query — a
    force-live override cannot expand the caller's row set.

    ``persona`` (Phase 8.C.1) — when supplied and its
    ``bypass_row_security`` flag is true, the router skips row-security
    entirely for that execution: no rule is compiled and no predicate is
    injected, so aggregate + pocket matching runs unconditionally rather
    than only for candidates that can prove they carry the predicate
    (Bug-8397 — an active rule does NOT disable the fast paths; see
    ``_route_with_row_security`` below and ``_aggregate_is_rls_safe`` /
    ``_pocket_is_rls_safe`` for the per-artifact proof each must pass). The
    bypass is structured-logged AND persisted as a platform audit event
    (F-008-24) so operators can review it in the audit trail.

    ``row_security`` (Bug-7038) — when supplied, the caller has already
    compiled the RLS rules for this principal (e.g. to include the policy
    hash in the cache key). The router uses this result instead of
    recompiling. When ``None`` and a principal is present, the router
    compiles row-security itself (backwards-compatible with all existing
    call sites).
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
    # route through the RLS-aware path. Bug-7038: callers that pre-compile
    # row_security for cache keying pass it in; otherwise compile here.
    if row_security is None and principal is not None:
        row_security = await compile_row_security(
            bound_query.model.id, principal, db, connector=_connector,
        )
    rls_bypass = bool(persona is not None and persona.bypass_row_security)
    if has_active_rules(row_security) and not rls_bypass:
        # Bug-7033: RLS-active queries now attempt aggregate/pocket serving
        # when the security dimension columns are all present in the
        # candidate's grain. force_route="aggregate"/"pocket" is allowed
        # and honoured when an RLS-safe candidate exists; if no safe
        # candidate matches, the function falls back to source (or raises
        # if force was specified). See _route_with_row_security.
        return await _route_with_row_security(
            bound_query, db, row_security,
            target_dialect=_target_dialect_early,
            force_route=force_route,
            persona=persona,
            persist_population_observation=persist_population_observation,
        )
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
    elif rls_bypass:
        # Bug-7046: the persona carries bypass_row_security=true but no active
        # RLS rules exist for this principal, so the flag is inert. It is still a
        # security-relevant configuration that deserves a durable, self-contained
        # audit record — emit an INFO event with rules_skipped=[] so a security
        # auditor sees the inert flag without cross-referencing persona config.
        logger.info(
            "persona_bypass_row_security=true (inert; no active RLS rules) "
            "model_id=%s persona_id=%s",
            bound_query.model.id,
            persona.id,
        )
        await _record_rls_bypass_audit(
            db,
            bound_query=bound_query,
            principal=principal,
            persona=persona,
            rule_ids=[],
            inert=True,
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
            # Bug-8788: the caller explicitly chose live execution.
            # Do not log a miss — the matchers were deliberately bypassed
            # and the optimizer must not build shapes the user rejected.
            log_miss=False,
        )

    # Bug-6916: force_route="raw" no longer short-circuits before aggregate/
    # pocket matching.  The raw route was designed as a first choice for
    # ungrouped detail queries, but silently bypassed aggregates that could
    # serve the same shape — degrading aggregate hit rate for the most common
    # BI-tool pattern (initial table exploration).  Now the code continues
    # into the normal aggregate/pocket matching flow below.  If no fast path
    # matches, the raw-route fallback runs just before the final source
    # fallback (see the ``_raw_route_flag`` guard near the end of this
    # function).
    _raw_route_flag = force_route == "raw"

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

    # Bug-8043 (F-015-03, Option A): the DAX time-variant source-only force was
    # REMOVED here. DAX time-variant hints (TOTALYTD, SAMEPERIODLASTYEAR, …) now flow
    # into aggregate matching, where the period-variant route either PROVES an
    # equivalent base-over-aggregate acceleration or fails closed to source. The
    # wrong-numbers protection the old gate provided (never serve the untransformed
    # base measure as the variant) is enforced inside ``find_best_aggregate``: a
    # DAX-hinted query the period-variant route did not serve returns no aggregate, so
    # the query still falls back to the correct source rewrite below.

    # Phase 5 — derived-grain LIVE EXACT serving (spec §16 Phase 5, stages 4+5).
    # Precondition is the operational kill-switch
    # query.derived_expression_serving_enabled (default ON) — NOT a manual serve
    # gate; the actual authority is the per-relationship trust predicate (health).
    # Only a derived-grain query (inline expression key / verified bijection
    # relabel) with a proven EXACT candidate is served here; every other case
    # returns None and continues to ordinary routing / source (byte-identical).
    # This runs only when NO RLS rules are active (the RLS path returned earlier)
    # and AFTER CLS enforcement above, so an EXACT serve cannot leak restricted
    # rows/columns. A pinned fast-path (force_route) skips the derived attempt so
    # the caller's explicit route contract is honoured.
    if force_route is None:
        _derived = await _try_derived_exact_route(
            bound_query, db, target_dialect=_target_dialect_early, persona=persona,
        )
        if _derived is not None:
            return _derived

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
            persist_population_observation=persist_population_observation,
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
            persist_population_observation=persist_population_observation,
        )
        pocket = pocket_result.pocket
        pocket_skipped_reason = pocket_result.skipped_reason

    _target_dialect = _target_dialect_early

    if pocket is not None:
        pocket_dialect = await _resolve_aggregate_target_dialect(pocket, db) or _target_dialect
        try:
            rewritten = rewrite_for_pocket(bound_query, pocket, pocket_dialect)
        except PocketRewriteUnsupported:
            rewritten = bound_query.logical_query.raw_query
        if rewritten == bound_query.logical_query.raw_query:
            # Bug-6976: a matched pocket whose rewrite is unsafe has NOT served
            # the query — the pocket-priority invariant ("pocket beats aggregate
            # when both can serve") no longer applies. Fall through to the
            # aggregate matcher instead of short-circuiting to source. If no
            # aggregate matches either, the source fallback at the bottom still
            # runs. pocket_skipped_reason is preserved on the eventual decision.
            pocket = None
            pocket_skipped_reason = PocketSkipReason.REWRITE_UNSAFE
        else:
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
                # Bug-8455: bind the decision to the generation the matcher
                # admitted, so the execution-time guard re-proves THAT build
                # rather than whatever is live by then. The containment proof
                # (query subset-of pocket) is not re-run at execution time, so
                # without this stamp a definition edit + complete refresh inside
                # the routing tail is undetectable.
                admitted_generation=pocket_generation_of(pocket),
            )

    # F-004-08: under force_route="pocket" the aggregate matcher is skipped so a
    # matching aggregate can never win over the forced (but unmatched) pocket —
    # the ``force_route in (...)`` guard below then raises rather than silently
    # returning an aggregate route the caller did not ask for.
    #
    # Bug-6916 safety: when force_route="raw" (ungrouped detail-row query) and
    # the query has no GROUP BY grain, skip the aggregate matcher.  Aggregates
    # store pre-grouped data; serving an ungrouped SELECT from an aggregate
    # would collapse detail rows into aggregated totals — wrong cardinality.
    # Pockets CAN serve detail rows (no aggregation), so pocket matching above
    # is safe.  When the query DOES have a grain (e.g. ungrouped classification
    # was wrong, or it has explicit aggregate functions), the aggregate matcher
    # runs as normal to maximise hit rate.
    _skip_agg_for_raw = (
        _raw_route_flag
        and not bound_query.logical_query.grain
    )
    agg_result = None
    if force_route == "pocket" or _skip_agg_for_raw:
        aggregate = None
        agg_skip_reasons = None
        agg_required_grain = None
    else:
        agg_result = await find_best_aggregate(
            bound_query,
            db,
            persona_id=str(persona.id) if persona is not None else None,
        )
        aggregate = agg_result.aggregate
        agg_skip_reasons = agg_result.skip_reasons or None
        # NOT ``or None``: ``[]`` is a meaningful answer (the matcher required
        # no grain) and must reach the miss-logger intact. Only ``None`` --
        # which the matcher sets when it early-returned before computing the
        # grain -- may fall back to the logger's own derivation.
        agg_required_grain = getattr(agg_result, "required_grain", None)

    _filter_missing = list(
        getattr(agg_result, "filter_columns_missing", None) or []
    ) if agg_result is not None else []

    # Bug-5197: pass the canonical name map from the matcher to the
    # validator so filter dimension canonicalization is consistent.
    _agg_name_to_canonical = getattr(agg_result, "name_to_canonical", None) if agg_result is not None else None
    _agg_logical_to_grain = (
        getattr(agg_result, "logical_to_aggregate_grain", None)
        if agg_result is not None
        else None
    )
    _agg_calc_expandable = (
        getattr(agg_result, "calc_expandable_measures", None)
        if agg_result is not None
        else None
    )
    _agg_period_variant_plan = (
        getattr(agg_result, "period_variant_plan", None)
        if agg_result is not None
        else None
    )

    if aggregate is not None:
        valid, reason = validate_aggregate_route(
            bound_query,
            aggregate,
            name_to_canonical=_agg_name_to_canonical,
            logical_to_aggregate_grain=_agg_logical_to_grain,
            period_variant_plan=_agg_period_variant_plan,
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
            # Bug-6095: do NOT fall back to the TARGET (agg) dialect when the
            # SOURCE dialect is unresolved. Falling open treated an unknown
            # source as the target's engine, so a percentile query against an
            # aggregate whose source was actually BigQuery (approximate) could
            # be served as if exact -> wrong numbers. Leaving it None makes
            # ``quantile_materialization_is_exact`` fail CLOSED (empty source is
            # not an exact dialect), so the exact-semantics query routes to
            # source where the engine computes the exact percentile.
            quantile_dialect = await _resolve_aggregate_source_dialect(aggregate, db)
            if await _query_uses_percentile(bound_query, db) and not quantile_materialization_is_exact(
                quantile_dialect, agg_dialect
            ):
                fallback_reason = (
                    f"Aggregate {aggregate.id} has approximate or unmaterialised "
                    f"quantile columns (source {quantile_dialect}, target {agg_dialect}); "
                    f"percentile query routed to source for exact results"
                )
            else:
                try:
                    rewritten = rewrite_for_aggregate(
                        bound_query,
                        aggregate,
                        agg_dialect,
                        logical_to_aggregate_grain=_agg_logical_to_grain,
                        calc_expandable_measures=_agg_calc_expandable,
                        period_variant_plan=_agg_period_variant_plan,
                    )
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
                        # Bug-8457: bind to the admitted generation; see the
                        # pocket site above and routing/artifact_generation_guard.
                        admitted_generation=aggregate_generation_of(aggregate),
                    )
        else:
            # Validation failed — fall through to source with the validation reason
            fallback_reason = f"Aggregate {aggregate.id} rejected: {reason}"
    else:
        fallback_reason = "No active aggregate covers the requested grain and measures"
        if _filter_missing:
            fallback_reason += (
                " (filter column missing: " + ", ".join(_filter_missing) + ")"
            )

    # Bug-7601: decay the estimated_hit_rate for an aggregate that was
    # matched (candidate found) but ultimately not served (validation,
    # percentile gate, or rewriter rejected it).  Without this miss-decay
    # leg the hit-only EMA saturates near 1.0 after ~30 lifetime hits,
    # degrading eviction/retirement ranking.
    if aggregate is not None and fallback_reason:
        await record_aggregate_miss(aggregate, db)

    if force_route in ("aggregate", "pocket"):
        details: list[str] = []
        if fallback_reason:
            details.append(fallback_reason)
        if force_route == "aggregate" and agg_skip_reasons:
            details.append(
                "aggregate skip reasons: " + ", ".join(str(r) for r in agg_skip_reasons)
            )
        if force_route == "pocket" and pocket_skipped_reason:
            details.append(f"pocket skip reason: {pocket_skipped_reason}")
        detail_text = " ".join(details).strip()
        if detail_text:
            detail_text = f" Route-specific reason: {detail_text}."
        raise NoAggregateMatchError(
            f"force_route={force_route!r} but no matching aggregate or pocket was found. "
            "Remove force_route or create an aggregate for this query shape."
            f"{detail_text}"
        )

    # Bug-6916: when the gateway requested the raw route and no aggregate/
    # pocket could serve the query, attempt the raw rewriter before falling
    # back to source.  The raw builder renders ungrouped flat-row detail
    # queries; it cannot handle complex WHERE or unresolvable predicates
    # (Bug-5880), in which case the source rewrite is the safe final fallback.
    if _raw_route_flag:
        _raw_fallback = None
        if getattr(bound_query.logical_query, "has_unresolvable_where", False) or getattr(
            bound_query.logical_query, "has_complex_sql", False
        ):
            _raw_fallback = "WHERE clause not fully representable as semantic filters"
        else:
            try:
                rewritten = await rewrite_for_raw(bound_query, db, target_dialect=_target_dialect)
                return RouteDecision(
                    route_type="raw",
                    rewritten_query=rewritten,
                    reason=(
                        "force_route=raw; no aggregate/pocket matched — "
                        "ungrouped flat-row query served via raw route"
                    ),
                    aggregate_id=None,
                    pocket_id=None,
                    pocket_skipped_reason=pocket_skipped_reason,
                    aggregate_skipped_reasons=agg_skip_reasons,
                    target_dialect=_target_dialect,
                )
            except RawRouteUnsupported as e:
                _raw_fallback = str(e)
        rewritten = await rewrite_for_source(bound_query, db, target_dialect=_target_dialect)
        return RouteDecision(
            route_type="source",
            rewritten_query=rewritten,
            reason=f"force_route=raw fell back to source: {_raw_fallback}",
            aggregate_id=None,
            pocket_id=None,
            pocket_skipped_reason=pocket_skipped_reason,
            aggregate_skipped_reasons=agg_skip_reasons,
            target_dialect=_target_dialect,
            # Bug-8800: this is a SOURCE decision reached after the matcher ran
            # (it carries that run's ``aggregate_skipped_reasons``), so the
            # miss-log must record the grain the MATCHER required, exactly as
            # the terminal source return below does. Omitting it silently
            # reverts this one path to ``log_query_miss``'s independent
            # ``sorted(set(lq.grain) | filter_dims)`` derivation — which is the
            # divergence Bug-8800 exists to close, and which drops the DISTINCT
            # fallback and DATE_TRUNC substitutions the matcher applied.
            required_grain=agg_required_grain,
            filter_columns_missing=_filter_missing,
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
        required_grain=agg_required_grain,
        filter_columns_missing=_filter_missing,
    )


def _build_measure_requests(bound_query: BoundQuery) -> "list[DerivedMeasureRequest] | None":
    """Map the query's requested measures to DerivedMeasureRequest(name, stat).

    The statistic MUST come from the query's EXPLICIT aggregate function, not the
    measure's ``default_agg`` — an explicit ``SUM(latency)`` over a measure whose
    default is ``p50`` must request ``sum``, or the derived route would serve the
    stored median under the SUM alias (Bug-7780 parity: the explicit function
    wins). Returns None on ANY shape this simple mapping cannot faithfully carry
    (composable/passthrough expressions, a literal COUNT that is not COUNT(*), or a
    measure requested with two different explicit stats) so the caller falls back
    to source rather than guessing. An empty measure list also returns None (a
    query with no measure never takes the derived route).
    """
    from src.routing.derived_expression_proof import MeasureRequest as DerivedMeasureRequest

    lq = bound_query.logical_query
    select_exprs = list(getattr(lq, "select_expressions", []) or [])

    # Build the explicit (measure_name -> stat) map from the parsed SELECT list.
    explicit: dict[str, str] = {}
    for expr in select_exprs:
        # A composable aggregate expression (SUM(a)/SUM(b), CASE over SUMs) is not a
        # single stat this exact read can serve — bail to source.
        if getattr(expr, "composable", False):
            return None
        if expr.classification == "literal" and (expr.agg_function or "").lower() == "count":
            # COUNT(*) / COUNT(1) -> the synthetic row-count measure.
            explicit["__row_count"] = "count"
            continue
        if expr.classification == "analytical" and expr.inner_column and expr.agg_function:
            # Key case-insensitively: the binder resolves measures case-insensitively
            # (measure_map_lower), so the query's spelling (SUM(revenue)) and the
            # model's canonical name (Revenue) can differ. Without the fold, the
            # explicit stat is not found by ``m.name`` and default_agg leaks through —
            # serving e.g. a stored median under an explicit SUM (Bug-7780 regression).
            name = expr.inner_column.lower()
            stat = str(expr.agg_function).lower()
            prev = explicit.get(name)
            if prev is not None and prev != stat:
                # Same measure requested with two different explicit stats — the
                # single-stat exact read cannot serve both; source-route.
                return None
            explicit[name] = stat
        elif expr.classification == "passthrough":
            # The served derived GROUP-BY key is ALSO projected as a ``passthrough``
            # SELECT item (you always ``SELECT DATE_TRUNC(...)`` the key you group
            # by) — a NON-aggregate projected expression. The exact read serves it
            # as a KEY, so it must NOT bail here (bailing on it made the whole
            # feature unreachable). Only a passthrough carrying AGGREGATE content (a
            # compound/embedded aggregate this single-stat exact read cannot map) is
            # a disqualifying passthrough -> bail to source. A projected key
            # expression carries no agg_function / agg_functions.
            if (
                getattr(expr, "agg_function", None)
                or getattr(expr, "agg_functions", None)
            ):
                return None
            # else: a projected non-aggregate expression (the derived key) — skip.

    # Bug-7796 / F-006-01: DAX inline aggregate overrides carry the REQUESTED
    # function that differs from the measure's default_agg. Apply the override
    # as requested_stat (SQL select_expressions still win via ``explicit``).
    _agg_overrides = getattr(lq, "measure_agg_overrides", None) or {}
    _override_lower = {
        (k or "").lower(): (v or "").lower()
        for k, v in _agg_overrides.items()
        if v
    }

    out: list[DerivedMeasureRequest] = []
    for m in getattr(bound_query, "resolved_measures", []) or []:
        # Prefer the query's explicit stat (matched case-insensitively); then
        # DAX override; fall back to default_agg for a bare measure reference.
        stat = (
            explicit.get(str(m.name).lower())
            or _override_lower.get(str(m.name).lower())
            or str(getattr(m, "default_agg", "sum") or "sum").lower()
        )
        out.append(DerivedMeasureRequest(measure_name=m.name, requested_stat=stat))
    # A COUNT(*) that resolves to no named measure still needs serving.
    if "__row_count" in explicit and not any(
        mr.measure_name == "__row_count" for mr in out
    ):
        out.append(DerivedMeasureRequest(measure_name="__row_count", requested_stat="count"))
    if not out:
        return None
    return out


async def _persona_restricted_column_ids(
    persona: Persona | None, db: AsyncSession,
) -> frozenset[str]:
    """Resolve the persona's CLS-restricted model_column_id set (§9.1).

    Empty when there is no persona or no tag restriction. Used by the derived
    route to deny an attribute edge whose internal key or detail column is
    restricted — the label must not launder a restricted key. Mirrors the id
    resolution in ``_check_column_restrictions``.
    """
    if persona is None:
        return frozenset()
    restrictions = (
        await db.execute(
            select(PersonaTagRestriction.data_tag_id)
            .where(PersonaTagRestriction.persona_id == persona.id)
        )
    ).scalars().all()
    if not restrictions:
        return frozenset()
    col_rows = (
        await db.execute(
            select(data_tag_columns.c.model_column_id)
            .where(data_tag_columns.c.tag_id.in_(restrictions))
        )
    ).scalars().all()
    return frozenset(str(c) for c in col_rows)


async def _try_derived_exact_route(
    bound_query: BoundQuery,
    db: AsyncSession,
    *,
    target_dialect: str | None,
    persona: Persona | None = None,
) -> RouteDecision | None:
    """Phase-5 LIVE derived-grain EXACT serving (spec §16 Phase 5, stages 4+5).

    Returns an aggregate-backed ``RouteDecision`` ONLY when ALL hold:
      - ``query.derived_expression_serving_enabled`` is ON (the operational
        kill-switch, DEFAULT ON — spec architecture_derived-grain-operational-
        serving.md §A). OFF -> None, byte-identical routing. This is a safety
        valve, not a turn-on gate: serving still requires the per-relationship
        trust predicate to pass over CURRENT health (VERIFIED artifact-local
        evidence bound to the active run);
      - the query is a derived-grain shape (a bound derived expression in GROUP BY);
      - some active aggregate's Phase-3 manifest yields a ``DerivedServeProof`` with
        verdict ``EXACT`` (exact expression-key identity OR verified bijection
        relabel), its trust predicate passing over live evidence;
      - the exact direct-read rewrite (§8.2) succeeds.
    Otherwise returns None and the caller continues to ordinary routing / source —
    NEVER a wrong number. This function is only reached when NO RLS rules are active
    (the RLS path returns earlier) and after CLS has been enforced, so an EXACT
    serve here cannot expose restricted rows/columns. Any doubt -> None (source).
    """
    from shared.config.resolver import get_setting
    from src.rewrite.derived_exact import (
        AggregateRewriteUnsupported,
        rewrite_for_derived_exact,
    )
    from src.routing.derived_serving import (
        DerivedServeContext,
        build_query_key_requests,
        try_build_exact_proof,
    )
    from src.semantic.binder import load_active_aggregates

    lq = bound_query.logical_query

    # I10 fail-closed: ANY error in the derived attempt must fall back to source,
    # never surface as a 500 or alter ordinary routing. The whole body is wrapped;
    # the only non-None return is a fully-built EXACT RouteDecision.
    try:
        # Kill-switch gate FIRST so a disabled deployment does zero derived work
        # (byte-identical, no shape parse cost). This is the operational safety
        # valve (spec strategy_derived-grain-operational-serving.md §A): it
        # DEFAULTS TO ENABLED — it replaces the removed manual
        # ``derived_expression_routing_mode == "serve"`` precondition. Serving is
        # NOT authorised by a stored value; the real authority is the per-edge
        # trust predicate over CURRENT health, evaluated below in
        # ``try_build_exact_proof``. This switch only lets an operator turn the
        # whole capability OFF instantly.
        #
        # ``query.derived_expression_serving_enabled`` is declared SYSTEM-scoped in
        # the registry, so the resolver reads its stored value only when a
        # ``system_session`` is supplied; open a short-lived system session for the
        # read. Default ENABLED on any resolver error (the trust predicate remains
        # the fail-closed authority — a resolver blip must not silently disable a
        # healthy, operationally-validated capability). OFF -> return None.
        from shared.db.session import get_system_db

        try:
            serving_enabled = True
            async for _sys_db in get_system_db():
                serving_enabled = bool(await get_setting(
                    "query.derived_expression_serving_enabled",
                    system_session=_sys_db,
                    tenant_session=db,
                    model_id=bound_query.model.id,
                ))
                break
        except Exception:
            # Default ENABLED on a resolver error (the trust predicate stays the
            # fail-closed authority for numbers). Log it: an operator's explicit OFF
            # is NOT honoured while this read errors, and that must be visible during
            # an incident rather than silently ignored (Fable R1 #7).
            logger.warning(
                "derived serving kill-switch read failed for model %s; defaulting "
                "to ENABLED (trust predicate remains the correctness authority) — "
                "an operator OFF is not honoured until this resolves",
                getattr(bound_query.model, "id", "?"), exc_info=True,
            )
            serving_enabled = True
        if not serving_enabled:
            return None

        # SELECT * bail: a star query expands to every model measure at default_agg
        # and records no explicit SELECT items, so the exact-read SELECT-shape guard
        # (which returns early on empty select_expressions) cannot vet it — serving
        # it would fabricate a (key, all-measures) answer for SQL the source may
        # reject (ungrouped columns). A star derived-grain query routes to source.
        if getattr(lq, "select_star", False):
            return None

        # Cheap shape gate: only a derived-grain query is a candidate.
        if not build_query_key_requests(bound_query):
            return None

        # Complex-SQL / window / CTE bail (genuinely unservable shapes — their
        # WHERE/window/CTE semantics cannot be reconstructed by an exact direct
        # read, so serving them would drop inner predicates or windows).
        #
        # Do NOT bail on ``has_passthrough_expressions`` here: the binder sets that
        # flag TRUE for every function-grain query (binder.py: has_function_grain
        # -> has_passthrough), which is precisely the derived-serving target shape.
        # Bailing on it made the whole feature unreachable. The genuinely-unservable
        # passthrough cases (compound-aggregate expressions) are caught downstream:
        # _build_measure_requests returns None on a composable/unmappable measure,
        # and the exact rewrite fails closed on any measure it cannot map. The
        # complex-SQL/window/CTE flags above still catch the unsafe passthrough
        # shapes (those set has_complex_sql, not merely has_function_grain).
        if (
            getattr(lq, "has_complex_sql", False)
            or getattr(lq, "has_window_functions", False)
            or getattr(lq, "has_window_aggregate", False)
            or getattr(lq, "cte_aliases", None)
        ):
            return None

        # Percentile-exactness gate (same as the ordinary route, Bug-987/6095): a
        # query requesting an exact percentile must not be served from an aggregate
        # whose quantile columns are approximate (e.g. BigQuery APPROX_QUANTILES) or
        # cross-engine-unmaterialised. Resolved per-candidate below.
        _uses_percentile = await _query_uses_percentile(bound_query, db)

        measures = _build_measure_requests(bound_query)
        if not measures:
            return None

        model = bound_query.model
        try:
            accepted_verifier = str(await get_setting(
                "model.attribute_relationship_verifier_version", tenant_session=db,
            ))
        except Exception:
            accepted_verifier = "v0"
        restricted_ids = await _persona_restricted_column_ids(persona, db)

        # §9.1 CLS for the EXPRESSION-KEY path: a user who cannot reference an input
        # column cannot group by a FUNCTION of it. A function grain contributes no
        # bare grain dimension, so the restricted leaf is NOT in the upstream
        # touched-column set — the derived route must deny it here or a
        # `GROUP BY DATE_TRUNC('month', restricted_ts)` would launder the restricted
        # input. (Deny is by restricted physical column NAME; the deeper §9.1
        # source-path lineage gate is tracked as a separate pre-existing issue.)
        if restricted_ids:
            from src.routing.derived_serving import served_expression_leaf_physical_names

            restricted_phys = await _restricted_physical_names(list(restricted_ids), db)
            if restricted_phys & served_expression_leaf_physical_names(bound_query):
                return None  # a served expression leaf is CLS-restricted -> source

        ctx = DerivedServeContext(
            bound_deployed_version_id=getattr(model, "deployed_version_id", None),
            bound_deploy_epoch=int(getattr(model, "deploy_epoch", 0) or 0),
            accepted_verifier_version=accepted_verifier,
            security_ok=True,
            restricted_column_ids=restricted_ids,
        )

        aggregates = await load_active_aggregates(model.id, db)
        # Bug-8664 (round-1 review finding 1): this route is the THIRD producer of
        # an aggregate RouteDecision and it runs BEFORE ``find_best_aggregate``, so
        # wiring the row-population gate into the matcher alone left it serving
        # with no proof at all. Worked example on the reviewer's shape: an
        # aggregate whose grain pulled in an INNER-joined relation answered 140
        # where the query's own elided source plan answers 390. Same shared
        # checker the matcher uses — never a second copy.
        _population = AggregatePopulationChecker(bound_query, db)
        for agg in aggregates:
            proof = await try_build_exact_proof(
                db=db, bound_query=bound_query, agg=agg, measures=measures, ctx=ctx,
            )
            if proof is None:
                continue
            if not (await _population.proven(agg))[0]:
                continue
            agg_dialect = await _resolve_aggregate_target_dialect(agg, db) or target_dialect or "postgres"
            # Percentile-exactness gate: if the query wants an exact percentile and
            # this aggregate's stored quantile columns are approximate/unmaterialised
            # for the (source, target) dialect pair, do NOT serve — source computes
            # the exact value. Fails CLOSED when the source dialect is unknown.
            if _uses_percentile:
                _q_src = await _resolve_aggregate_source_dialect(agg, db)
                if not quantile_materialization_is_exact(_q_src, agg_dialect):
                    continue
            try:
                rewritten = rewrite_for_derived_exact(
                    bound_query=bound_query, aggregate=agg,
                    proof=proof, target_dialect=agg_dialect,
                )
            except AggregateRewriteUnsupported:
                # This candidate cannot be faithfully served; try the next, else source.
                continue
            return RouteDecision(
                route_type="aggregate",
                rewritten_query=rewritten,
                reason=(
                    f"Derived-grain EXACT serve from aggregate {agg.id} "
                    f"(verdict={proof.verdict}); "
                    f"reasons={','.join(proof.reason_codes) or 'none'}"
                ),
                aggregate_id=str(agg.id),
                pocket_id=None,
                target_dialect=agg_dialect,
                source_dialect=target_dialect,
                pending_hit_credit=agg,
                # Bug-8457: bind to the admitted generation (derived-exact
                # serve reads the same physical artifact).
                admitted_generation=aggregate_generation_of(agg),
            )
        return None
    except Exception:  # noqa: BLE001 — I10: any doubt routes to source.
        logger.warning(
            "derived exact route attempt failed for model %s; routing to source",
            getattr(bound_query.model, "id", "?"), exc_info=True,
        )
        return None


def _aggregate_is_rls_safe(
    aggregate, compiled: CompiledPredicate,
) -> bool:
    """True iff every security dimension column appears in the aggregate grain
    AND the predicate can execute on the aggregate's target connection.

    Bug-7033: an aggregate whose grain includes all columns the RLS
    predicate references can be served safely — the predicate is injected
    into the SELECT that reads from the aggregate table, narrowing the
    result set exactly as it would narrow the source. When any security
    column is absent from the grain, the aggregate cannot be filtered and
    must not be served.

    Bug-7033-F1: the safety check must verify the PHYSICAL grain column
    name (collision-resolved via ``grain_physical_cols``), not the LOGICAL
    name. When grain columns collide (e.g. ``dim_region_region_code``),
    the aggregate table uses the physical name, and the security predicate
    references the logical name. If the logical name is not a valid column
    in the aggregate, the injected WHERE clause would reference a
    nonexistent column -> 502.  Build the physical lookup and verify
    resolvability.

    This function is PURE: it reads ``aggregate.grain`` /
    ``grain_physical_cols`` and returns a bool. It does NOT mutate ``compiled``
    -- building the logical->physical map for ``_inject_security_where`` is
    ``_rls_security_col_physical_map``'s job, below. The docstring used to claim
    that mutation, which matters now that ``aggregate_generation_guard`` re-runs
    this gate at EXECUTION time against a live column read (Bug-8457): a
    mutating gate would have corrupted the compiled predicate the
    source-fallback re-injection depends on. It does not, and saying so here
    saves the next reader from re-deriving it (deep-review R3 finding 8).

    Codex R1 finding: user_mapping predicates contain a subquery that
    references a mapping table on the SOURCE connection. That subquery
    cannot execute on the aggregate TARGET connection (the mapping table
    may not exist there). Force user_mapping rules to source.
    """
    if not compiled.security_dimension_columns:
        return False  # fail closed: unknown columns -> not safe
    # User-mapping predicates reference source-connection tables that are
    # not present on the aggregate target — not safe for aggregate serving.
    if compiled.mapping_source_ids:
        return False
    agg_grain = set(getattr(aggregate, "grain", None) or [])
    if not agg_grain:
        return False
    # Bug-7033-F1: resolve logical security column -> physical grain column.
    # Use the same ``_build_dim_phys_lookup`` the rewriter uses so the
    # physical name is consistent.
    from src.rewrite.aggregate import AggregateRewriteUnsupported, _build_dim_phys_lookup
    try:
        dim_phys = _build_dim_phys_lookup(aggregate)
    except AggregateRewriteUnsupported:
        return False
    for col in compiled.security_dimension_columns:
        if col not in agg_grain:
            return False
        # Verify the physical column is resolvable (not None / empty).
        phys = dim_phys.get(col, col)
        if not phys:
            return False
    return True


def _rls_security_col_physical_map(
    aggregate, compiled: CompiledPredicate,
) -> dict[str, str]:
    """Build logical->physical name map for security columns on this aggregate.

    Bug-7033-F1: ``_inject_security_where`` must reference the PHYSICAL
    grain column, not the logical alias. This helper builds the map once
    and the caller passes it to the injection function.
    """
    from src.rewrite.aggregate import AggregateRewriteUnsupported, _build_dim_phys_lookup
    try:
        dim_phys = _build_dim_phys_lookup(aggregate)
    except AggregateRewriteUnsupported:
        return {}
    return {
        col: dim_phys.get(col, col)
        for col in (compiled.security_dimension_columns or [])
    }


def _pocket_projects_all_columns(defining_sql: str | None) -> bool:
    """True iff the pocket's defining SQL is a provable ROW-preserving
    ``SELECT * FROM <table> [WHERE <simple preds>]`` — the only shape that
    guarantees the pocket contains EVERY row of its predicate slice (so the
    matcher's ``query ⊆ pocket`` containment holds) and drops no columns.

    Bug-8018: this is a ROUTE-TIME re-verification (column coverage itself is
    proven separately against the pocket's ``row_manifest`` — see
    ``_pocket_is_rls_safe``). It uses a POSITIVE whitelist (Bug-8018 review [C]):
    a bare ``*`` projection, a single plain physical-table FROM, an optional
    WHERE, and NOTHING else. Anything that reduces rows (LIMIT / OFFSET /
    DISTINCT / GROUP BY / QUALIFY / TABLESAMPLE), reorders/windows, expands rows
    (LATERAL / JOIN / comma-join), drops columns (projection list / ``t.*`` /
    ``* EXCEPT/REPLACE`` / derived-FROM / CTE), or reads a non-table source
    (table functions) is rejected. Fail closed on missing/unparseable.
    """
    if not defining_sql:
        return False
    import re as _re
    import sqlglot as _sg
    from sqlglot import exp as _exp
    # Defensive belt (dialect-independent): some read dialects (e.g. postgres)
    # silently DROP a ``* EXCEPT/REPLACE`` star modifier during parse. Reject if
    # the projection text (between SELECT and the first FROM) carries one.
    _head = _re.split(r"\bfrom\b", defining_sql, maxsplit=1, flags=_re.IGNORECASE)[0]
    if _re.search(r"\b(except|replace)\b", _head, flags=_re.IGNORECASE):
        return False
    try:
        tree = _sg.parse_one(defining_sql, read="postgres")
    except Exception:
        try:
            tree = _sg.parse_one(defining_sql)
        except Exception:
            return False
    if tree is None or not isinstance(tree, _exp.Select):
        return False
    # POSITIVE whitelist: the ONLY truthy Select args allowed are the projection
    # (``expressions``), the ``FROM`` (``from``/``from_`` across sqlglot versions),
    # and an optional ``WHERE``. Any other truthy arg — ``limit``, ``offset``,
    # ``order``, ``group``, ``distinct``, ``qualify``, ``windows``, ``with``/
    # ``with_``, ``laterals``, set-op modifiers — means the pocket is NOT a
    # row-preserving full slice, so reject.
    _allowed = {"expressions", "from", "from_", "where"}
    for _k, _v in tree.args.items():
        if _v in (None, [], False, "", {}):
            continue
        if _k not in _allowed:
            return False
    # Exactly one physical table scan (rejects comma/JOIN, derived-FROM subquery,
    # CTE refs, and any WHERE-subquery multi-source shape).
    if len(list(tree.find_all(_exp.Table))) != 1:
        return False
    _from = tree.find(_exp.From)
    _from_t = getattr(_from, "this", None) if _from is not None else None
    # The sole FROM must be a plain NAMED physical table: never a derived table
    # (``exp.Subquery``) and never a table function (``FROM generate_series(...)``
    # whose ``this`` is a function node, not an ``Identifier``), and it must carry
    # no TABLESAMPLE (which materialises a random subset -> wrong numbers).
    if not isinstance(_from_t, _exp.Table):
        return False
    if not isinstance(getattr(_from_t, "this", None), _exp.Identifier):
        return False
    # Reject row-reducing / dialect-quirk FROM modifiers (Bug-8018 review [5]):
    # PG ``ONLY`` (inheritance exclusion), TABLESAMPLE/``SAMPLE`` (random subset),
    # ``PARTITION`` selection, and ANY table alias — a row-preserving pocket is a
    # bare ``FROM <table>`` with none of these (some parse as a bogus alias under
    # ``read="postgres"``, hiding a row-reducing clause).
    if (
        _from_t.args.get("only")
        or _from_t.args.get("sample")
        or _from_t.args.get("partition")
        or _from_t.args.get("alias")
    ):
        return False
    # The single projection must be a modifier-free bare ``*`` (``exp.Star``).
    # ``t.*`` parses to ``exp.Column(this=Star)`` (rejected), ``* EXCEPT/REPLACE``
    # carries args that drop/transform columns (rejected).
    projections = tree.expressions
    if len(projections) != 1:
        return False
    proj = projections[0]
    if not isinstance(proj, _exp.Star):
        return False
    if proj.args.get("except") or proj.args.get("replace"):
        return False
    return True


def _pocket_materialised_columns(pocket) -> set[str] | None:
    """EXACT-CASE set of the column names a pocket ACTUALLY materialised, read
    from its authoritative ``row_manifest`` (spec §5.2: "ordered materialised
    column descriptors").

    Bug-8458: this docstring previously said "Lowercased set", which the code
    has never done and must never do. The injected row-security predicate quotes
    the security column case-sensitively (``quote_identifier``), so folding case
    here would ACCEPT a pocket whose column differs only by case and then fail
    at the database. Exact matching is what makes a case-only mismatch fall back
    to source. Do not "fix" the code to match prose — see the Bug-8018 review [4]
    note at the ``out.add`` line below.

    Each descriptor's output name is
    ``logical_name or physical_column`` — the name the pocket TABLE physically
    exposes (semantic alias for a persona-star build, physical name for a
    plain-star build).

    Returns ``None`` when the manifest (or its ``columns`` list) is absent — the
    fail-closed signal that we CANNOT prove the pocket's contents. A pocket is a
    branch-dependent VISIBLE projection, NOT a physical ``SELECT *`` of every
    model column, so the model shape cannot be substituted for this (Bug-8018
    review [A]): a hidden or joined-table security column can be absent from the
    pocket even when it is a valid model column.
    """
    manifest = getattr(pocket, "row_manifest", None)
    if not isinstance(manifest, dict):
        return None
    # Bug-8018 review [1]: the manifest must describe THIS pocket's LIVE build. A
    # refresh can flip status="fresh" while ``advance_artifact_manifest`` threw
    # (it is wrapped in a bare except -> warning), leaving the PREVIOUS build's
    # manifest in place — which could name a column the new build dropped and
    # false-accept into a 502. Bind the manifest to the pocket's active refresh
    # run and to a manifest version this router understands; fail closed
    # otherwise (source route).
    try:
        from shared.semantic.artifact_manifest import (
            MANIFEST_VERSION as _MANIFEST_VERSION,
        )
    except Exception:
        return None
    active_run = getattr(pocket, "active_refresh_run_id", None)
    if not active_run or str(manifest.get("build_refresh_run_id") or "") != str(active_run):
        return None
    if manifest.get("manifest_version") != _MANIFEST_VERSION:
        return None
    cols = manifest.get("columns")
    if not cols or not isinstance(cols, (list, tuple)):
        return None
    out: set[str] = set()
    for c in cols:
        if not isinstance(c, dict):
            return None  # malformed manifest -> fail closed
        name = c.get("logical_name") or c.get("physical_column")
        if name:
            # Bug-8018 review [4]: preserve EXACT case. The injected predicate
            # quotes the security column case-sensitively (``quote_identifier``),
            # so a case-only mismatch would pass a lowercased gate then fail at
            # the DB. Exact matching makes a mismatch fail closed to source.
            out.add(str(name))
    return out or None


def _pocket_is_rls_safe(pocket, compiled: CompiledPredicate) -> bool:
    """True iff a pocket may be served under active row-security without ever
    returning a row the principal's RLS forbids, and without a fail-closed 502.

    Bug-8018 (regression/residual of Bug-7033): the previous behaviour left
    pockets CATEGORICALLY unserved under RLS. A pocket may be served ONLY when we
    can PROVE:
      (1) ``security_dimension_columns`` is non-empty and no ``user_mapping`` rule
          is active (mapping subquery targets the source connection);
      (2) the defining SQL is a row-preserving ``SELECT * FROM <table> [WHERE]``
          (``_pocket_projects_all_columns``) — so the matcher's ``query ⊆ pocket``
          containment holds and no column/row is dropped; AND
      (3) every security column is a materialised output column of the pocket,
          proven against the pocket's ``row_manifest`` (``_pocket_materialised
          _columns``) — NOT inferred from the model shape, because a pocket
          materialises only its VISIBLE branch-dependent projection (Bug-8018
          review [A]).
    When all hold, the pocket table physically contains every security column
    under the name the predicate uses, so injecting the SAME compiled predicate
    the source route uses narrows the pocket exactly as the source
    (``pocket ∩ query_filters ∩ rls == source ∩ query_filters ∩ rls`` because a
    pocket only serves when ``query ⊆ pocket``). Rows are only removed.

    Fail closed on EVERY condition we cannot prove (mirrors
    ``_aggregate_is_rls_safe``). A ``False`` never serves an unsafe pocket; it
    only makes the pocket look unavailable so routing falls back to the RLS-safe
    aggregate / source paths, which are already fail-closed.

    THIS GATE IS LIVE (Bug-8458 corrects the note that used to stand here). The
    producer landed: ``shared/pocket/row_manifest.write_pocket_row_manifest``
    writes ``row_manifest.columns`` on EVERY completed pocket refresh, so this
    function admits real pockets today and is the decision point for whether a
    pocket may be scanned under active row-level security. It is also re-run
    against a FRESH read of the pocket row at execution time by
    ``routing/pocket_generation_guard.assert_pocket_route_admissible``. Treat it
    as security-load-bearing code, not as a dormant placeholder: the earlier note
    said the producer was "out of this lane's scope" and that the gate
    "correctly falls back to source for every pocket", which a reader could take
    as licence to remove or weaken it.
    """
    # Unknown security columns -> cannot prove the predicate references anything
    # real; the aggregate path fails closed identically.
    if not compiled.security_dimension_columns:
        return False
    # User-mapping predicates embed an IN-subquery over a mapping table on the
    # SOURCE connection; not provably safe on the pocket target -> fall to source
    # (mirrors the aggregate path, Bug-7039). Result caching is also bypassed for
    # user_mapping RLS at the API boundary.
    if compiled.mapping_source_ids:
        return False
    # Row-preserving shape (no dropped columns / reduced or expanded rows).
    if not _pocket_projects_all_columns(getattr(pocket, "defining_sql", None)):
        return False
    # Column coverage proven against the pocket's OWN materialised manifest.
    materialised = _pocket_materialised_columns(pocket)
    if materialised is None:
        return False
    # Exact-case match (Bug-8018 review [4]): the predicate references the
    # security column case-sensitively, so require an exact name in the pocket's
    # materialised output columns; a case-only mismatch fails closed to source.
    for col in compiled.security_dimension_columns:
        if str(col) not in materialised:
            return False
    return True


async def _route_with_row_security(
    bound_query: BoundQuery,
    db: AsyncSession,
    compiled: CompiledPredicate,
    *,
    target_dialect: str | None = None,
    force_route: str | None = None,
    persona: Persona | None = None,
    persist_population_observation: bool = True,
) -> RouteDecision:
    """Route with active row-security, attempting RLS-safe fast paths first.

    Bug-7033 fix: the old implementation unconditionally bypassed aggregate
    and pocket matching when RLS was active, on the (over-broad) premise that
    aggregates/pockets were built without knowing the audience. Now:

    * A pocket is "RLS-safe" when it is a PROVEN bare ``SELECT *`` subset of the
      model (``_pocket_is_rls_safe`` / ``_pocket_projects_all_columns``), so it
      retains every security dimension column under the same names the source
      scan exposes. The same compiled predicate is injected per-scan into the
      pocket-rewritten SQL, narrowing it exactly as on the source. Pockets are
      attempted first (the documented pocket-beats-aggregate precedence). This
      is Bug-8018: previously pockets were categorically unserved under RLS.
    * An aggregate is "RLS-safe" when every security dimension column in the
      compiled predicate appears in the aggregate's grain. The predicate is
      injected into the SELECT that reads from the aggregate table, so the
      result is narrowed exactly as it would be on the source scan.
    * A pocket/aggregate we cannot PROVE safe is never served — the query falls
      back to the source route with the predicate injected (unchanged behavior).
      No forbidden row is ever returned: injection only removes rows, and the
      per-scan injector fails closed (DB "column does not exist") rather than
      running unfiltered if a security column is somehow absent.

    The security predicate is injected into the WHERE clause of **every**
    SELECT that scans a physical table (F-007-01), so UNION branches,
    scalar subqueries, subquery-first FROMs and CTE bodies are all
    constrained, and the predicate always applies before any LIMIT
    (Bug-915). Shapes that cannot be safely constrained are rejected
    with 403 — never executed unfiltered.

    When ``force_route="raw"``, dispatches to ``rewrite_for_raw`` instead
    of ``rewrite_for_source`` so the ungrouped flat-row query is produced,
    then the security predicate is still injected.
    """
    rules_list = ", ".join(compiled.active_rule_ids)
    security_rules_meta = list(compiled.applied_rules) if compiled.applied_rules else None

    # ---- Attempt RLS-safe pocket serving (Bug-8018) ---------------------
    # Bug-7033 left pockets categorically unserved under RLS. A pocket is a
    # row-preserving subset of the model. ``_pocket_is_rls_safe`` serves it ONLY
    # when it is a proven row-preserving ``SELECT *`` AND its OWN row_manifest
    # proves every security column was materialised (not inferred from the model
    # shape — finding [A]); then the SAME compiled predicate the source route
    # injects is valid on the pocket table. Pockets are attempted FIRST to honour
    # the documented pocket-beats-aggregate precedence (architecture_pocket-
    # tables.md; asserted in route_query above).
    #
    # STRUCTURAL SOURCE-ONLY GATES (Bug-8018 review finding [2]): mirror the
    # pre-fast-path gates ``route_query`` applies before ANY pocket/aggregate
    # match. A DAX time-variant query, a disabled model, disabled aggregations,
    # or a query over invalid objects MUST go to source — a pocket would serve
    # the base measure (wrong numbers for a variant) or a structurally wrong
    # shape. The RLS aggregate block below applies these same gates via
    # ``_skip_agg``; the pocket attempt must not bypass them.
    _pkt_skip_reason: str | None = None
    _dax_hints_rls = getattr(bound_query.logical_query, "time_variant_hints", None)
    _structurally_source_only = (
        # Predicate-only preclusions (Bug-8018 review [3]): a deny-all principal
        # (no security columns) or a user_mapping rule can NEVER yield an RLS-safe
        # pocket, so skip the DB-hitting pocket match entirely on every such query.
        not compiled.security_dimension_columns
        or bool(compiled.mapping_source_ids)
        # Structural source-only gates (finding [2]).
        or force_route in ("source", "raw", "aggregate")
        or str(getattr(bound_query.model, "status", "") or "").lower() == "disabled"
        or not bool(getattr(bound_query.model, "aggregations_enabled", True))
        or bool(getattr(bound_query, "uses_invalid_objects", False))
        or bool(_dax_hints_rls)
    )
    if not _structurally_source_only:
        _pkt_result = await find_best_pocket(
            bound_query, db,
            persona_id=str(persona.id) if persona is not None else None,
            persist_population_observation=persist_population_observation,
        )
        _pkt = _pkt_result.pocket
        _pkt_skip_reason = _pkt_result.skipped_reason
        if _pkt is not None:
            # Prove the pocket physically contains every security column (via its
            # own row_manifest) before serving it (finding [A]/[1]).
            if _pocket_is_rls_safe(_pkt, compiled):
                _pkt_dialect = (
                    await _resolve_aggregate_target_dialect(_pkt, db) or target_dialect
                )
                try:
                    _pkt_rewritten = rewrite_for_pocket(bound_query, _pkt, _pkt_dialect)
                except PocketRewriteUnsupported:
                    _pkt_rewritten = bound_query.logical_query.raw_query
                # A no-op rewrite (Bug-6976) means the pocket did NOT actually
                # serve; fall through to the aggregate/source paths rather than
                # injecting the predicate into the unrewritten source SQL.
                if _pkt_rewritten != bound_query.logical_query.raw_query:
                    # R-001: the pocket rewrite scans exactly ONE materialised
                    # relation (``pocket_tbl``) — Bug-8018 forbids JOIN/subquery/
                    # CTE, so a bare predicate binds unambiguously. The SOURCE
                    # owner in ``compiled.security_column_owners`` is never
                    # scanned here, so leaving it populated would fail the
                    # owner-not-scanned guard (Bug-8896) with a 403. Suppress it
                    # on the injection copy ONLY; ``compiled`` keeps its owners
                    # for the cache-miss source fallback carried on the decision.
                    _pkt_final_sql = _inject_security_where(
                        _pkt_rewritten, _without_security_owners(compiled),
                        dialect=_pkt_dialect or "postgres",
                        force_route=force_route,
                    )
                    return RouteDecision(
                        route_type="pocket",
                        rewritten_query=_pkt_final_sql,
                        reason=(
                            f"Row security active ({len(compiled.active_rule_ids)} "
                            f"rule(s): {rules_list}); RLS-safe pocket {_pkt.id} "
                            f"served with predicate injection"
                        ),
                        aggregate_id=None,
                        pocket_id=str(_pkt.id),
                        security_rules_applied=security_rules_meta,
                        target_dialect=_pkt_dialect,
                        source_dialect=target_dialect,
                        # MANDATORY: carry the compiled predicate so the
                        # routed-cache-table-missing fallback (routes.py)
                        # re-injects it into the source SQL. Without this a
                        # missing pocket table would fall back to UNFILTERED
                        # source — a data leak.
                        security_compiled=compiled,
                        # Bug-8455: bind to the admitted generation.
                        admitted_generation=pocket_generation_of(_pkt),
                    )
                _pkt_skip_reason = PocketSkipReason.REWRITE_UNSAFE
            else:
                # A pocket matched but is not provably RLS-safe (hidden security
                # column, user_mapping rule, or non-bare-star). Surface WHY so
                # route diagnostics can explain the miss (finding [4]).
                _pkt_skip_reason = PocketSkipReason.NOT_RLS_SAFE

    # ---- Attempt aggregate matching (Bug-7033) --------------------------
    # Skip aggregate matching when:
    #   - force_route is "source" or "raw" (caller explicitly wants source)
    #   - force_route is "pocket" (the RLS-safe pocket attempt above owns that
    #     route; if it did not serve, the forced pocket raises below)
    #   - the model/query is structurally incompatible (same gates as the
    #     non-RLS path: disabled model, disabled aggregations, invalid objects,
    #     DAX time-variant, no grain for raw-flag)
    _skip_agg = force_route in ("source", "raw", "pocket")
    if not _skip_agg:
        model_status = str(getattr(bound_query.model, "status", "") or "").lower()
        if model_status == "disabled":
            _skip_agg = True
        if not bool(getattr(bound_query.model, "aggregations_enabled", True)):
            _skip_agg = True
        if bound_query.uses_invalid_objects:
            _skip_agg = True
        # Bug-8043 (F-015-03, Option A): the DAX time-variant aggregate-skip was
        # REMOVED here too, so a DAX time-variant hint can accelerate through the
        # PROVEN period-variant route under active row security (the aggregate must
        # still be RLS-safe, and the per-scan predicate injection lands in the inner
        # roll-up SELECT — source-parity order). Every unproven DAX-hinted query still
        # returns no aggregate from find_best_aggregate and falls back to source.

    if not _skip_agg:
        agg_result = await find_best_aggregate(
            bound_query,
            db,
            persona_id=str(persona.id) if persona is not None else None,
        )
        aggregate = agg_result.aggregate
        if aggregate is not None and _aggregate_is_rls_safe(aggregate, compiled):
            _agg_name_to_canonical = getattr(agg_result, "name_to_canonical", None)
            _agg_logical_to_grain = getattr(agg_result, "logical_to_aggregate_grain", None)
            _agg_calc_expandable_rls = getattr(agg_result, "calc_expandable_measures", None)
            _agg_period_variant_plan_rls = getattr(agg_result, "period_variant_plan", None)
            valid, reason = validate_aggregate_route(
                bound_query,
                aggregate,
                name_to_canonical=_agg_name_to_canonical,
                logical_to_aggregate_grain=_agg_logical_to_grain,
                period_variant_plan=_agg_period_variant_plan_rls,
            )
            if valid:
                agg_dialect = await _resolve_aggregate_target_dialect(aggregate, db) or target_dialect
                # Bug-7772: apply the SAME percentile-exactness gate the
                # non-RLS path uses (router.py:554-561).  Without this,
                # an RLS query could be served from an aggregate whose
                # quantile columns are approximate (e.g. BigQuery
                # APPROX_QUANTILES), returning wrong percentile values.
                _rls_pct_ok = True
                if await _query_uses_percentile(bound_query, db):
                    _q_dial = await _resolve_aggregate_source_dialect(aggregate, db)
                    if not quantile_materialization_is_exact(_q_dial, agg_dialect):
                        _rls_pct_ok = False
                if not _rls_pct_ok:
                    pass  # percentile gate rejected; fall through to source
                else:
                    try:
                        rewritten = rewrite_for_aggregate(
                            bound_query,
                            aggregate,
                            agg_dialect,
                            logical_to_aggregate_grain=_agg_logical_to_grain,
                            calc_expandable_measures=_agg_calc_expandable_rls,
                            period_variant_plan=_agg_period_variant_plan_rls,
                        )
                    except AggregateRewriteUnsupported:
                        pass  # fall through to source
                    else:
                        # Bug-7033-F1: on aggregates with collision-renamed
                        # grain columns, the security predicate references the
                        # LOGICAL column name but the aggregate table only has
                        # the PHYSICAL name. Rewrite the predicate SQL to use
                        # the physical column names before injection.
                        _sec_phys_map = _rls_security_col_physical_map(
                            aggregate, compiled,
                        )
                        _injection_compiled = compiled
                        _needs_rename = any(
                            log != phys for log, phys in _sec_phys_map.items()
                        )
                        if _needs_rename and compiled.sql_expression:
                            import sqlglot as _sg
                            from sqlglot import exp as _sg_exp
                            try:
                                # Codex R2: parse the predicate with the SOURCE
                                # dialect (target_dialect), not the aggregate
                                # dialect.  The compiled SQL was built against
                                # the source connection.  Parsing postgres-
                                # quoted identifiers with a BigQuery/Spark read
                                # dialect turns them into string literals,
                                # causing the rename to silently fail and
                                # potentially producing an always-true predicate
                                # that bypasses RLS.  Render with agg_dialect
                                # after renaming.
                                _source_dialect = target_dialect or "postgres"
                                _pred_ast = _sg.parse_one(
                                    f"SELECT 1 WHERE {compiled.sql_expression}",
                                    read=_source_dialect,
                                )
                                def _rename_sec_cols(node):
                                    if isinstance(node, _sg_exp.Column):
                                        phys = _sec_phys_map.get(node.name)
                                        if phys and phys != node.name:
                                            return _sg_exp.column(
                                                phys, quoted=True,
                                            )
                                    return node
                                _pred_ast = _pred_ast.transform(_rename_sec_cols)
                                # Codex R2: verify every required logical
                                # column was actually replaced.  If any
                                # remains, the rename silently failed (e.g.
                                # cross-dialect parse issue) and the predicate
                                # still references a nonexistent column.
                                _remaining_logical = {
                                    c.name
                                    for c in _pred_ast.find_all(_sg_exp.Column)
                                    if c.name in _sec_phys_map
                                    and _sec_phys_map[c.name] != c.name
                                }
                                if _remaining_logical:
                                    raise ValueError(
                                        f"RLS column rename incomplete: "
                                        f"{_remaining_logical} not replaced"
                                    )
                                _where_node = _pred_ast.find(_sg_exp.Where)
                                if _where_node:
                                    # Bug-8396: render the renamed predicate
                                    # back in the SOURCE dialect it was parsed
                                    # in, NOT in agg_dialect. This block owns
                                    # the logical->physical column RENAME only;
                                    # the source->execution dialect move is
                                    # owned by the single proven conversion in
                                    # ``render_predicate_for_dialect`` (called
                                    # from ``_inject_security_where``). Doing
                                    # the dialect move here as well would leave
                                    # ``compile_connector`` describing a string
                                    # it no longer matches, and would move the
                                    # predicate across dialects without the
                                    # column-set survival proof.
                                    _new_sql = _where_node.this.sql(
                                        dialect=_source_dialect,
                                    )
                                    # Build a copy of compiled with the
                                    # physical-column predicate.
                                    import types as _types
                                    _injection_compiled = _types.SimpleNamespace(
                                        **{
                                            k: getattr(compiled, k)
                                            for k in dir(compiled)
                                            if not k.startswith("_")
                                        },
                                    )
                                    _injection_compiled.sql_expression = _new_sql
                                    # The renamed predicate is still in the
                                    # source dialect; keep the recorded compile
                                    # dialect truthful for the conversion step.
                                    _injection_compiled.compile_connector = (
                                        dialect_to_connector(_source_dialect)
                                    )
                                    # The rename replaced the LOGICAL column
                                    # names with PHYSICAL ones, so the declared
                                    # logical columns no longer appear in the
                                    # expression. Re-declare them as the
                                    # physical names so the conversion's
                                    # "declares columns but parses to none"
                                    # degradation check stays meaningful
                                    # instead of comparing against stale names.
                                    _injection_compiled.security_dimension_columns = tuple(
                                        dict.fromkeys(
                                            _sec_phys_map.get(_c, _c)
                                            for _c in (
                                                compiled.security_dimension_columns or ()
                                            )
                                        )
                                    )
                            except Exception:
                                # Codex R1: remapping failure means the
                                # predicate still references the logical column
                                # name which does not exist on the aggregate
                                # table. Serving the aggregate would cause a
                                # missing-column DB error. Fall back to the
                                # RLS-protected source route instead.
                                _injection_compiled = None
                        if _injection_compiled is not None:
                            # R-001: the aggregate rewrite scans exactly ONE
                            # materialised GROUP-BY table, never the SOURCE owner
                            # in ``security_column_owners``, so a bare predicate
                            # binds unambiguously. Leaving the source owner
                            # populated would fail the owner-not-scanned guard
                            # (Bug-8896) with a 403. Suppress it on the injection
                            # copy ONLY; the full ``compiled`` still rides on the
                            # decision (``security_compiled``) for the fallback.
                            final_sql = _inject_security_where(
                                rewritten, _without_security_owners(_injection_compiled),
                                dialect=agg_dialect or "postgres",
                                force_route=force_route,
                            )
                            return RouteDecision(
                                route_type="aggregate",
                                rewritten_query=final_sql,
                                reason=(
                                    f"Row security active ({len(compiled.active_rule_ids)} "
                                    f"rule(s): {rules_list}); RLS-safe aggregate "
                                    f"{aggregate.id} served with predicate injection"
                                ),
                                aggregate_id=str(aggregate.id),
                                pocket_id=None,
                                pocket_skipped_reason=_pkt_skip_reason,
                                security_rules_applied=security_rules_meta,
                                target_dialect=agg_dialect,
                                source_dialect=target_dialect,
                                pending_hit_credit=aggregate,
                                security_compiled=compiled,
                                # Bug-8457: bind to the admitted generation.
                                admitted_generation=aggregate_generation_of(
                                    aggregate
                                ),
                            )
                        # else: rename failed, fall through to source

        # Bug-7601: apply miss-decay in the RLS path too.  If the matcher
        # found an aggregate but the RLS path did not serve it (unsafe,
        # validation failed, rewrite failed, or rename failed), the
        # aggregate's hit-rate should decay so eviction ranking stays fair
        # for RLS-heavy workloads.
        if aggregate is not None:
            await record_aggregate_miss(aggregate, db)

    # ---- force_route guard (after aggregate attempt) --------------------
    # If the caller forced aggregate/pocket and no safe candidate matched,
    # raise rather than silently returning source.
    if force_route == "aggregate":
        raise NoAggregateMatchError(
            "force_route='aggregate' but no RLS-safe aggregate matched "
            "for this query under the active row-security rules. "
            "Remove force_route to run the query against the source."
        )
    if force_route == "pocket":
        _pkt_reason = f" (pocket skip reason: {_pkt_skip_reason})" if _pkt_skip_reason else ""
        raise NoAggregateMatchError(
            "force_route='pocket' but no RLS-safe pocket matched under the "
            "active row-level security rules (a pocket is RLS-safe only when it "
            "is a provable row-preserving SELECT * whose materialised columns "
            f"cover every security column){_pkt_reason}. Remove force_route to "
            "run the query against the source."
        )

    # ---- Source / raw fallback -------------------------------------------
    # Bug-7029: a force_route="raw" request whose shape the raw builder cannot
    # render (unresolvable WHERE, or a complex shape such as a UNION) is
    # downgraded to the source route so the security predicate can still be
    # injected per-scan. Both routes are fail-closed (the predicate constrains
    # EVERY physical scan, including each UNION branch), so no rows leak — but
    # the downgrade must NOT be silent: it is surfaced in ``reason`` below so
    # the caller can see the forced route was not honoured (and why), rather
    # than assuming a raw route was served.
    _raw_requested = force_route == "raw"
    _raw_ok = _raw_requested and not (
        getattr(bound_query.logical_query, "has_unresolvable_where", False)
        or getattr(bound_query.logical_query, "has_complex_sql", False)
    )
    _raw_downgrade_reason: str | None = None
    if _raw_ok:
        try:
            rewritten = await rewrite_for_raw(bound_query, db, target_dialect=target_dialect)
            route_type = "raw"
        except RawRouteUnsupported as _raw_exc:
            # Bug-5880: same fallback as the non-RLS raw branch — the source
            # rewrite preserves predicates the raw builder cannot render.
            rewritten = await rewrite_for_source(bound_query, db, target_dialect=target_dialect)
            route_type = "source"
            _raw_downgrade_reason = str(_raw_exc)
    else:
        rewritten = await rewrite_for_source(bound_query, db, target_dialect=target_dialect)
        route_type = "source"
        if _raw_requested:
            # Bug-7029: raw was forced but the shape (unresolvable WHERE / UNION
            # / other complex SQL) is not raw-servable; record why for the reason.
            _raw_downgrade_reason = (
                "WHERE clause not fully representable as semantic filters"
                if getattr(bound_query.logical_query, "has_unresolvable_where", False)
                else "complex SQL shape (e.g. UNION/subquery/CTE) not raw-servable"
            )

    final_sql = _inject_security_where(
        rewritten, compiled, dialect=target_dialect or "postgres",
        force_route=force_route,
    )

    agg_note = "no RLS-safe aggregate matched" if not _skip_agg else "aggregate matching skipped"
    # Bug-7029: surface a forced-raw → source downgrade explicitly so it is not
    # silent. The query still runs correctly (predicate injected per-scan).
    _route_note = "source route with predicate injection"
    if _raw_downgrade_reason is not None:
        _route_note = (
            "force_route=raw downgraded to source route with predicate "
            f"injection ({_raw_downgrade_reason})"
        )
    return RouteDecision(
        route_type=route_type,
        rewritten_query=final_sql,
        reason=(
            f"Row security active ({len(compiled.active_rule_ids)} rule(s): "
            f"{rules_list}); {agg_note}; {_route_note}"
        ),
        aggregate_id=None,
        pocket_id=None,
        pocket_skipped_reason=_pkt_skip_reason,
        security_rules_applied=security_rules_meta,
        target_dialect=target_dialect,
        security_compiled=compiled,
    )


# Bug-8809. The CLOSED vocabulary of reasons this rejection can carry to a
# caller. Every member is a fixed token authored here — never a value derived
# from the query, the model or an exception message — which is what makes the
# 403 body safe for the lowest-trust caller by construction instead of by a
# sanitising pass that has to out-guess free prose.
ROW_SECURITY_REJECT_REASON_CODES: frozenset[str] = frozenset({
    # Emitted only when a call site passes a token that is not registered here
    # (see the fail-closed branch below) — never chosen deliberately.
    "unsupported_shape",
    "sql_unparseable",
    "predicate_dialect_unsupported",
    "predicate_unparseable",
    "scope_analysis_failed",
    "table_reference_unresolved",
    "table_outside_select_scope",
    "no_table_scan",
    # Bug-8896/9264: a security column's owning relation is not scanned in a
    # SELECT the predicate must constrain, so it cannot be bound to a proven
    # scan. Generic token — carries no column/table identifier.
    "security_column_owner_not_scanned",
})


def _reject_row_security_shape(
    reason_code: str,
    *,
    diagnostic: str = "",
    force_route: str | None = None,
):
    """Fail closed: refuse to run a query whose shape cannot be safely
    constrained by the row-security predicate (F-007-01).

    Bug-7029: when ``force_route`` pinned a fast path (raw/aggregate/pocket),
    add a hint that removing the pin may let the query run — the pinned route
    can narrow the set of shapes the security injector accepts, so the generic
    "rewrite as a plain SELECT" advice is unhelpful when the user's SQL is
    already a plain SELECT and only the forced route made it unservable.

    Bug-8809 [SECURITY]: this used to take a free-prose ``reason`` and
    interpolate it into the caller-visible ``message``. Two of the seven call
    sites fed it real identifiers — the ``RowSecurityDialectError`` text, which
    ``predicate_compiler`` builds out of the model's security-dimension COLUMN
    NAMES, and the two scan-resolution branches, which name the physical TABLE.
    Nothing downstream could take it back: an ``HTTPException`` bypasses every
    response-model sanitiser in ``api/_sql_disclosure.py``, and embed sessions
    (anonymous, link-shareable, and specifically designed to carry an RLS
    subject) reach this branch on all four row-returning routes. So a 403 that
    exists to protect row-level data was itself disclosing the schema the row
    security is written over.

    The channel is now closed rather than filtered. Callers pass a token from
    ``ROW_SECURITY_REJECT_REASON_CODES`` — which a client can localise — plus
    an optional ``diagnostic`` that goes to the SERVICE LOG only. The
    ``force_route`` hint stays in the body because it is the caller's own
    submitted value echoed back, not something it learns from us.
    """
    from fastapi import HTTPException

    if reason_code not in ROW_SECURITY_REJECT_REASON_CODES:
        # Fail closed on the guard itself: an unknown token means a call site
        # was added without registering its reason, and the safe response is to
        # publish nothing about it rather than to pass it through.
        logger.error(
            "unregistered row-security reject reason_code %r; "
            "publishing the generic code instead", reason_code,
        )
        reason_code = "unsupported_shape"

    logger.warning(
        "row-security injection rejected the query shape "
        "(reason_code=%s force_route=%s): %s",
        reason_code, force_route, diagnostic or "-",
    )

    message = (
        "This query cannot be safely constrained by the active "
        "row-level security rules. Rewrite it as a plain SELECT "
        "(or a UNION of plain SELECTs) over the model."
    )
    if force_route:
        message += (
            f" This request pinned force_route={force_route!r}; removing "
            "force_route (or using force_route='source') may allow it to run "
            "under the active row-security rules."
        )
    raise HTTPException(
        status_code=403,
        detail={
            "error_code": "row_security_unsupported_shape",
            "reason_code": reason_code,
            "message": message,
        },
    )


def _without_security_owners(compiled):
    """R-001: return a copy of ``compiled`` with ``security_column_owners``
    cleared, so ``_inject_security_where`` takes its bare-column inject path.

    Safe ONLY at a SINGLE-materialised-scan acceleration site: a pocket
    (row-preserving ``SELECT *`` of ONE table — Bug-8018 forbids JOIN/subquery/
    CTE), a single GROUP-BY aggregate table, or a materialised Named-Query
    result table (``SELECT * FROM <one table>``). With exactly one scanned
    relation a bare ``WHERE <col> = ...`` binds unambiguously — the pre-Bug-8896
    behaviour. ``security_column_owners`` exists only to qualify a SOURCE
    self-join of the owner (F-007-05/Bug-8896/Bug-9264); the source owner is
    never scanned in an acceleration rewrite, so a populated owner there makes
    ``_qualify_for_select`` fail closed (403 ``security_column_owner_not_
    scanned``) on a query it can serve safely.

    NEVER use on the source route or the cache-miss source-fallback path: those
    CAN self-join the owner, and suppressing owners there reopens the Bug-8896
    leak. Does not mutate the input — ``compiled`` is read again after injection
    (e.g. carried on the decision for the source fallback), so the full owners
    must survive on the original object.
    """
    import dataclasses

    if dataclasses.is_dataclass(compiled):
        return dataclasses.replace(compiled, security_column_owners=())
    import copy as _copy

    clone = _copy.copy(compiled)
    clone.security_column_owners = ()
    return clone


def _inject_security_where(
    sql: str, compiled: CompiledPredicate, dialect: str = "postgres",
    *, force_route: str | None = None,
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
        _reject_row_security_shape(
            "sql_unparseable",
            diagnostic="the SQL could not be parsed for predicate injection",
            force_route=force_route,
        )

    # Bug-8396 (RLS bypass): ``compiled.sql_expression`` is quoted in the dialect
    # of the connector it was COMPILED for (the model's SOURCE), but ``dialect``
    # here is the dialect of whatever we are actually serving from — which for an
    # aggregate may be a target on a DIFFERENT connector. Parsing a
    # PostgreSQL-quoted predicate with ``read="bigquery"`` turns ``"region"``
    # into a STRING LITERAL, so ``NOT "region" = 'EMEA'`` becomes the
    # constant-true ``NOT 'region' = 'EMEA'`` and every row is served.
    # ``render_predicate_for_dialect`` performs the conversion once, in one
    # place, and PROVES the column set survived it. It raises rather than
    # guessing, and a raise here must reject (never run unfiltered).
    try:
        _predicate_sql = render_predicate_for_dialect(compiled, dialect)
    except Exception as _dialect_err:
        # Bug-8809: ``_dialect_err`` names the model's security-dimension
        # COLUMNS. It goes to the log via ``diagnostic``, never to the caller.
        _reject_row_security_shape(
            "predicate_dialect_unsupported",
            diagnostic=(
                "the security predicate could not be safely rendered for the "
                f"execution dialect ({_dialect_err})"
            ),
            force_route=force_route,
        )
    try:
        security_ast = sqlglot.parse_one(
            f"SELECT 1 WHERE {_predicate_sql}", read=dialect
        )
        security_where = security_ast.find(exp.Where)
    except Exception:
        security_where = None
    if security_where is None:
        _reject_row_security_shape(
            "predicate_unparseable",
            diagnostic="the security predicate could not be parsed",
            force_route=force_route,
        )
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
            "scope_analysis_failed",
            diagnostic="the SQL could not be scope-analysed for predicate injection",
            force_route=force_route,
        )

    # Collect injection targets BEFORE mutating the tree so the copies of
    # the predicate (which may contain its own subquery, e.g. user_mapping
    # rules) are never re-walked.
    #
    # ONE target per distinct SELECT — deliberately NOT one per physical scan.
    # See the "Bug-7034 rejected fix" note at the injection loop below: a
    # per-scan target list fans the predicate out onto every relation in the
    # join, including dimension relations that do not carry the security
    # column at all.
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
                # Bug-8809: ``table.name`` is a PHYSICAL table name — log it,
                # do not publish it.
                _reject_row_security_shape(
                    "table_reference_unresolved",
                    diagnostic=(
                        f"table reference {table.name!r} could not be resolved "
                        "to a physical scan"
                    ),
                    force_route=force_route,
                )
            select = table.find_ancestor(exp.Select)
            if select is None:
                _reject_row_security_shape(
                    "table_outside_select_scope",
                    diagnostic=(
                        f"table {table.name!r} is scanned outside any SELECT scope"
                    ),
                    force_route=force_route,
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
                "table_outside_select_scope",
                diagnostic=(
                    f"table {table.name!r} is scanned outside any SELECT scope"
                ),
                force_route=force_route,
            )

    if not targets:
        _reject_row_security_shape(
            "no_table_scan",
            diagnostic="no table scan found to constrain",
            force_route=force_route,
        )

    # Bug-7034 (REJECTED FIX — read before "fixing" the ambiguity again).
    #
    # Bug-7034 reports a real defect: when TWO joined relations both expose the
    # security column (e.g. a self-join, or a fact and a dimension that both
    # carry ``region_code``), the bare predicate ``region_code = 'NORTH'`` is
    # ambiguous and the database rejects the query.
    #
    # The obvious fix — make ``targets`` one entry per PHYSICAL SCAN and
    # alias-qualify the predicate with each scan's alias — is WRONG and must
    # not be reinstated. ``scope.tables`` enumerates EVERY relation in the
    # query, not the relations that carry the security column, and nothing in
    # ``CompiledPredicate`` says which relation owns it. Fanning the predicate
    # out therefore emits, for the ordinary star-schema shape
    #     FROM sales f JOIN dim_product d ON f.product_id = d.id
    # the clause ``f."region_code" = 'NORTH' AND d."region_code" = 'NORTH'``.
    # ``dim_product`` has no ``region_code``, so EVERY row-security-protected
    # query over a joined model fails at the source. Where a second relation
    # does happen to carry the column, ANDing a second copy silently
    # over-filters and under-reports. Measured on this code:
    #   main    -> WHERE "region_code" = 'NORTH'                    (1 clause)
    #   fan-out -> WHERE f."region_code" = ... AND d."region_code" = ...
    # A correct fix must PROVE which relation owns the column (the model knows;
    # this function does not) and qualify only that one. See the intake filed
    # for Bug-7034 and the guard in
    # ``tests/test_bug_7034_rls_injection_must_not_fan_out.py``.
    #
    # F-007-05 / Bug-8896: when ``CompiledPredicate.security_column_owners``
    # names the owning physical table, qualify each security column with the
    # FIRST scan alias of that table in this SELECT. That disambiguates a
    # self-join (two scans of the owner) without fanning the predicate onto
    # dimension relations. Empty owners keep the historic bare column so a
    # fact-only star still works.
    owners = tuple(getattr(compiled, "security_column_owners", ()) or ())

    def _qualify_for_select(select_node: exp.Select, pred: exp.Expression) -> exp.Expression:
        qualified = pred.copy()
        if not owners:
            return qualified
        # F-007-05 / Bug-8896 / Bug-9264: bind the security column to the OWNING
        # relation. The prior cut recorded only the FIRST alias per physical
        # table, so a self-join of the owner (``FROM employees e1 JOIN employees
        # e2``) qualified only ``e1`` and left the ``e2`` scan row-UNSECURED.
        # Collect EVERY alias of each owner in this SELECT and constrain each one.
        aliases_by_table: dict[str, list[str]] = {}
        for table in select_node.find_all(exp.Table):
            enclosing = table.find_ancestor(exp.Select)
            if enclosing is not select_node:
                continue
            tname = (table.name or "").lower()
            if not tname:
                continue
            alias = table.alias_or_name
            aliases = aliases_by_table.setdefault(tname, [])
            if alias not in aliases:
                aliases.append(alias)
        owners_for_col: dict[str, list[str]] = {}
        for col_name, phys in owners:
            owners_for_col.setdefault((col_name or "").lower(), []).append(
                (phys or "").lower()
            )
        # Resolve each unqualified security column to the owner relation actually
        # scanned in THIS select (first candidate present; owners is an ordered
        # fallback list, never fanned out — Bug-7034).
        col_to_owner: dict[str, str] = {}
        referenced_tables: list[str] = []
        for col in qualified.find_all(exp.Column):
            if col.table:
                continue
            cname = (col.name or "").lower()
            candidates = owners_for_col.get(cname)
            if not candidates:
                continue  # not a security column -> leave bare (historic path)
            winner = next((t for t in candidates if t in aliases_by_table), None)
            if winner is None:
                # The model names this a security column but its owning relation
                # is not scanned in this SELECT, so we cannot prove which scan it
                # binds to. Leaving it bare could bind to an unrelated same-named
                # column (fail-open). Fail CLOSED instead (F-007-01).
                _reject_row_security_shape(
                    "security_column_owner_not_scanned",
                    diagnostic=(
                        "a row-security column could not be bound to the relation "
                        "that owns it in this query shape"
                    ),
                    force_route=force_route,
                )
            col_to_owner[cname] = winner
            if winner not in referenced_tables:
                referenced_tables.append(winner)
        if not referenced_tables:
            # No security column resolved to a scanned owner -> historic bare
            # inject (a fact-only star with empty/unmatched owners still works).
            return qualified
        # Emit one qualified copy of the predicate per alias-combination of the
        # referenced owner relations and AND them, so EVERY scan of EVERY owner
        # is constrained. Self-join (one owner, aliases [e1, e2]) -> P[e1] AND
        # P[e2]; a fact-only star (one owner, one alias) stays a single conjunct.
        import itertools

        alias_choice_lists = [aliases_by_table[t] for t in referenced_tables]
        combined: exp.Expression | None = None
        for combo in itertools.product(*alias_choice_lists):
            table_alias = dict(zip(referenced_tables, combo))
            one = pred.copy()
            for col in one.find_all(exp.Column):
                if col.table:
                    continue
                owner = col_to_owner.get((col.name or "").lower())
                if owner is not None:
                    col.set("table", exp.to_identifier(table_alias[owner]))
            combined = one if combined is None else exp.and_(combined, one)
        return combined

    for select in targets:
        this_pred = _qualify_for_select(select, predicate)
        existing_where = select.args.get("where")
        if existing_where is not None:
            # exp.and_ parenthesizes connector operands, so a top-level
            # OR in the user's WHERE cannot absorb the predicate into
            # one branch (Bug-1070).
            combined = exp.and_(
                existing_where.this, this_pred, copy=False
            )
            existing_where.set("this", combined)
        else:
            select.set("where", exp.Where(this=this_pred))

    # RLS render-boundary (intake rls-inject-security-render-boundary-bypass):
    # route the final emission through the single dialect render boundary in
    # rewrite/dialects.py instead of a raw ``ast.sql(dialect=...)`` so any
    # pre-generation dialect guard added there (semi-additive fail-loud, T-SQL
    # bracket escaping, week numbering) fires uniformly on the RLS path too.
    # The input SQL is ALREADY the target-dialect-rendered query (the rewriter
    # rendered it), so it is NOT PG-canonical — pass ``pg_canonical=False`` so
    # the WEEK->ISOWEEK rewrite (which only fires on PG-canonical input) is not
    # re-applied to already-emitted target SQL.
    from src.rewrite.dialects import render_tree_for_dialect
    return render_tree_for_dialect(ast, dialect, pg_canonical=False)


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
        "uda_restrictions_loaded",
        "measures_by_id",
        "measures_by_name",
        "restricted_physical_names",
        "known_physical_names",
        "table_identifiers",
    )

    def __init__(self):
        self.restricted_uda_ids: set[str] = set()
        # Bug-7812: distinguish "empty because not yet loaded" from "empty
        # because the model has no UDA touching a restricted column". A
        # UDA-backed dimension reached ONLY via a resolvable filter/order/having
        # (never projected) must still have this set loaded, or its closure is
        # checked against an empty set and served — a value-probing oracle over
        # the UDA's restricted backing column. The flag lets the filter/order/
        # having gates load it idempotently, mirroring the model-service
        # catalogue which always loads it under an active restriction.
        self.uda_restrictions_loaded: bool = False
        self.measures_by_id: dict = {}
        self.measures_by_name: dict = {}
        self.restricted_physical_names: set[str] | None = None
        # Bug-7607 (Codex R1 finding 1): the set of ALL model physical column
        # names (lowercased). Used by the calc-dimension gate to fail closed on
        # an identifier that is NOT a known column — it may be a table/row
        # reference (e.g. ``CAST(emp AS TEXT)``) that serialises every column of
        # the row, including restricted ones, past a name-only match.
        self.known_physical_names: set[str] | None = None
        # Bug-7607 R2-2: model table physical-names/aliases (lowercased). An
        # identifier matching one is a whole-row reference even if a same-named
        # column also exists (the table/column name-collision residual leak).
        self.table_identifiers: set[str] | None = None

    def as_context(self):
        """Return the shared ``ClosureContext`` backed by this loader's fields.

        Bug-7608 / Bug-7045: the unified closure algorithm lives in
        ``shared.security.restricted_column_closure``; this adapter hands it the
        already-loaded lookups. The two objects share field names, so the view
        is a direct pass-through — the shared module reads (never mutates) it.
        """
        from shared.security.restricted_column_closure import ClosureContext

        return ClosureContext(
            restricted_uda_ids=self.restricted_uda_ids,
            measures_by_id=self.measures_by_id,
            measures_by_name=self.measures_by_name,
            restricted_physical_names=self.restricted_physical_names,
            known_physical_names=self.known_physical_names,
            table_identifiers=self.table_identifiers,
        )


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
        ctx.uda_restrictions_loaded = True

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
        await _ensure_cls_physical_lookups(ctx, bound_query, restricted_col_ids, db)

    return ctx


async def _ensure_cls_physical_lookups(
    ctx: "_ClsClosure",
    bound_query: BoundQuery,
    restricted_col_ids: list,
    db: AsyncSession,
) -> None:
    """Populate the physical-name lookups the calc-dimension gate needs.

    Bug-7607: the calc-dimension CLS gate needs BOTH the restricted-column name
    set AND the full known-column set (to fail closed on a whole-row/table
    reference that is not a real column). These must be populated together —
    Round 2 finding R2-1 was a producer/consumer gap where a calc dimension
    referenced only in ORDER BY / WHERE / HAVING reached the gate with
    ``known_physical_names`` still None (only the projected-dimension path
    populated it), causing the gate to fail closed and over-block a clean query.
    Every site that populates ``restricted_physical_names`` for a possibly-calc
    dimension must go through here so the two sets stay in lockstep.

    Idempotent: only loads what is still missing, so lazy callers on the
    filter / order-by / having paths can call it unconditionally.

    The known-column and table-identifier sets are needed ONLY to verify calc
    dimensions, so they are loaded only when the bound query actually carries a
    calc dimension (projected or resolvable by name). Plain source-column
    filters/orders/havings pay no extra query — preserving the existing hot-path
    cost for the common case.
    """
    if ctx.restricted_physical_names is None:
        ctx.restricted_physical_names = await _restricted_physical_names(
            restricted_col_ids, db,
        )
    if _bound_query_has_calc_dimension(bound_query):
        if ctx.known_physical_names is None:
            ctx.known_physical_names = await _all_model_physical_names(
                bound_query.model.id, db,
            )
        if ctx.table_identifiers is None:
            ctx.table_identifiers = await _model_table_identifiers(
                bound_query.model.id, db,
            )
    # Bug-7812: a UDA-backed dimension reached ONLY via a resolvable
    # filter/order/having (never projected) never triggered the UDA restriction
    # load in _build_cls_closure (which inspects only PROJECTED objects), so its
    # closure was checked against an empty restricted_uda_ids and served — a
    # value-probing oracle over the UDA's restricted backing column, and a
    # divergence from model-service which always loads it. Every filter/order/
    # having gate routes through here, so load the UDA restrictions idempotently
    # whenever a UDA-backed object could be reached.
    await _ensure_restricted_uda_ids(ctx, bound_query, restricted_col_ids, db)


async def _ensure_restricted_uda_ids(
    ctx: "_ClsClosure",
    bound_query: BoundQuery,
    restricted_col_ids: list,
    db: AsyncSession,
) -> None:
    """Load the UDA-ids that reach a restricted column, once (Bug-7812).

    Idempotent via ``uda_restrictions_loaded``. Fires when the bound query can
    reach a UDA-backed object through a projected object, a known-by-name
    dimension, or ANY filter / ORDER BY / HAVING clause (whose referenced names
    resolve against the FULL model catalogue and may be UDA-backed). Plain
    source-column queries with no such clause pay nothing.
    """
    if ctx.uda_restrictions_loaded:
        return
    if not _bound_query_can_reach_uda(bound_query):
        return
    from shared.db.models import UserDefinedAttributeColumnRef

    uda_rows = (
        await db.execute(
            select(UserDefinedAttributeColumnRef.attribute_id)
            .where(UserDefinedAttributeColumnRef.column_id.in_(restricted_col_ids))
        )
    ).scalars().all()
    ctx.restricted_uda_ids = {str(a) for a in uda_rows}
    ctx.uda_restrictions_loaded = True


def _bound_query_can_reach_uda(bound_query: BoundQuery) -> bool:
    """True when a UDA-backed object could be reached by the CLS closure.

    A projected/known object that already carries ``user_defined_attribute_id``,
    OR any filter / ORDER BY / HAVING clause — those resolve their referenced
    names against the full model catalogue, so a non-projected UDA dimension can
    surface there (Bug-7812). Conservative on purpose: a false positive costs one
    small indexed query; a false negative re-opens the oracle.
    """
    for o in (
        list(getattr(bound_query, "resolved_measures", None) or [])
        + list(getattr(bound_query, "resolved_dimensions", None) or [])
        + list((getattr(bound_query, "resolved_dimensions_by_name", None) or {}).values())
    ):
        if getattr(o, "user_defined_attribute_id", None) is not None:
            return True
    lq = bound_query.logical_query
    if getattr(bound_query, "resolved_filters", None):
        return True
    if getattr(lq, "has_unresolvable_where", False):
        return True
    if getattr(lq, "order_by", None) or getattr(lq, "has_unresolvable_order", False):
        return True
    if getattr(lq, "having_raw", None) or getattr(lq, "having_columns", None):
        return True
    return False


def _bound_query_has_calc_dimension(bound_query: BoundQuery) -> bool:
    """True when any resolved dimension (projected or name-resolvable) carries a
    calc_expression, so the calc-dimension CLS lookups are worth loading."""
    for d in getattr(bound_query, "resolved_dimensions", None) or []:
        if getattr(d, "calc_expression", None):
            return True
    for d in (getattr(bound_query, "resolved_dimensions_by_name", None) or {}).values():
        if getattr(d, "calc_expression", None):
            return True
    return False


async def _all_model_physical_names(model_id, db: AsyncSession) -> set[str]:
    """Every physical COLUMN name in the model (lowercased).

    Used by the calc-dimension CLS gate to distinguish a real column reference
    from a whole-row/table reference that would serialise restricted columns.
    Table names/aliases are returned SEPARATELY by ``_model_table_identifiers``;
    the caller consults both sets (Bug-7607 R2-2 — a whole-row reference such as
    ``to_jsonb(orders)`` matches the TABLE identifier ``orders`` and must fail
    closed even when a same-named column exists).
    """
    from shared.db.models import ModelColumn, ModelTable

    col_rows = (
        await db.execute(
            select(ModelColumn.column_name)
            .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
            .where(ModelTable.model_id == model_id)
        )
    ).scalars().all()
    return {str(n).lower() for n in col_rows}


async def _model_table_identifiers(model_id, db: AsyncSession) -> set[str]:
    """Every model table physical name and alias (lowercased).

    Bug-7607 R2-2: an identifier matching a table name/alias is a whole-row
    reference regardless of any same-named column, so the calc-dimension gate
    fails closed on it.
    """
    from shared.db.models import ModelTable

    rows = (
        await db.execute(
            select(ModelTable.physical_name, ModelTable.alias)
            .where(ModelTable.model_id == model_id)
        )
    ).all()
    out: set[str] = set()
    for phys, alias in rows:
        if phys:
            out.add(str(phys).lower())
        if alias:
            out.add(str(alias).lower())
    return out


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

    Bug-7608 / Bug-7045: the closure algorithm (direct ``source_column_id`` /
    ``display_column_id`` membership, UDA refs, variant chain, transitive
    calc-measure refs, and calc-dimension expression leaves) now lives in the
    SHARED module ``shared.security.restricted_column_closure`` so this serving
    gate and the model-service catalogue-hide predicate produce IDENTICAL
    closures. ``_ClsClosure`` is the query-router loader; it exposes the shared
    ``ClosureContext`` via ``as_context()``. Fail-closed rules (F-008-06,
    Bug-7607, Bug-7804) are preserved by the shared implementation.
    """
    from shared.security.restricted_column_closure import object_touches_restricted

    return object_touches_restricted(obj, restricted_ids, ctx.as_context(), _visited)


async def _cls_restricted_detail(
    columns: list[str],
    persona: Persona,
    db: AsyncSession,
) -> dict:
    """Build the CLS 403 payload (F-008-02 — non-disclosing).

    A restricted-column 403 must be INDISTINGUISHABLE from an unknown-column
    403: echoing the restricted column names, the restricting tag names, or the
    persona identity turns the error into an existence oracle (an analyst can
    binary-search which columns exist / are classified PII). The client now
    receives a generic ``OBJECT_NOT_AVAILABLE`` message; the specific columns,
    tags, and persona go only to the server log for a privileged operator.

    Tag-name resolution failures degrade gracefully (the denial still stands;
    the log line simply omits the tags).
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

    persona_name = getattr(persona, "name", None) or str(persona.id)
    logger.info(
        "[CLS_DENY] columns=%r tags=%r persona_id=%s persona=%r",
        columns, tag_names, str(persona.id), persona_name,
    )
    return {
        "error_code": "OBJECT_NOT_AVAILABLE",
        "message": (
            "One or more requested columns are not available for this query. "
            "They may not exist or may not be accessible with your current "
            "access."
        ),
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

    Bug-7811: a FUNCTION-WRAPPED WHERE predicate (``WHERE UPPER(salary) = 'X'``,
    ``WHERE salary * 2 > N``) is not representable as a ``LogicalFilter``, so the
    parser drops it from ``resolved_filters`` and sets ``has_unresolvable_where``
    instead. The rewriter then emits that raw WHERE verbatim over a possibly
    restricted column — a value-probing oracle identical in kind to the ORDER BY
    (Bug-6140) and HAVING (Bug-6140-adjacent) oracles, which were hardened by
    re-parsing the raw clause while this filter gate was not. Mirror those gates:
    when ``has_unresolvable_where`` is set, re-parse the raw WHERE the rewriter
    renders (``source_sql``'s outer-SELECT ``where`` with the subquery-wrapper
    fallback), enumerate every referenced column, and fail closed on a restricted
    one or on any parse failure.
    """
    filters = getattr(bound_query, "resolved_filters", None) or []
    lq = bound_query.logical_query
    unresolvable_where = getattr(lq, "has_unresolvable_where", False)
    if not filters and not unresolvable_where:
        return []

    dims_by_name = dict(getattr(bound_query, "resolved_dimensions_by_name", {}) or {})
    for d in bound_query.resolved_dimensions:
        dims_by_name.setdefault(getattr(d, "name", None), d)

    # Bug-7607 R2-1: populate the calc-dim lookups together so a calc dimension
    # referenced only in a filter is verifiable (not fail-closed on missing
    # known_physical_names).
    await _ensure_cls_physical_lookups(ctx, bound_query, list(restricted_col_rows), db)
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

    # Bug-7811: harden the unresolvable (expression-wrapped) WHERE the same way
    # ORDER BY / HAVING are hardened — the columns inside it never reached
    # ``resolved_filters`` above.
    if unresolvable_where:
        blocked.extend(
            await _restricted_unresolvable_where_columns(
                bound_query, restricted_ids, ctx, restricted_col_rows, db,
            )
        )
    return blocked


async def _restricted_unresolvable_where_columns(
    bound_query: BoundQuery,
    restricted_ids: set[str],
    ctx: _ClsClosure,
    restricted_col_rows: list,
    db: AsyncSession,
) -> list[str]:
    """Columns inside an unresolvable (expression-wrapped) WHERE that reach a
    restricted column (Bug-7811). Fail closed on a parse failure.

    Mirrors ``source_sql``'s raw-WHERE extraction EXACTLY (outermost SELECT's
    ``where`` arg, with the Bug-457 subquery-wrapper fallback) so the gate
    inspects the same predicate the rewriter emits. Uses a STRICT parse (default
    ``ErrorLevel.RAISE``): a degraded parse could hide a restricted column as a
    non-``exp.Column`` token while the rewriter still emits it verbatim, so any
    parse failure blocks. Every referenced column is resolved through the shared
    ``_block_restricted_ref_names`` resolver (Dimension / Measure closure, then
    physical-name fallback) exactly as ORDER BY and HAVING do.
    """
    import sqlglot
    from sqlglot import exp as _sg_exp

    lq = bound_query.logical_query
    raw = getattr(lq, "raw_query", None) or ""
    read_dialect = getattr(lq, "input_dialect", "postgres") or "postgres"
    try:
        tree = sqlglot.parse_one(raw, read=read_dialect)
    except Exception:
        tree = None
    if tree is None:
        return ["(unverifiable WHERE)"]

    raw_select = (
        tree if isinstance(tree, _sg_exp.Select) else tree.find(_sg_exp.Select)
    )
    where_node = raw_select.args.get("where") if raw_select is not None else None
    # Bug-457: the WHERE can live inside a subquery wrapper
    # (``SELECT COUNT(*) FROM (SELECT * FROM t WHERE ...) q``).
    if where_node is None and raw_select is not None:
        from_clause = raw_select.args.get("from_")
        inner = getattr(from_clause, "this", None) if from_clause is not None else None
        if isinstance(inner, _sg_exp.Subquery) and isinstance(inner.this, _sg_exp.Select):
            where_node = inner.this.args.get("where")
    if where_node is None:
        # ``has_unresolvable_where`` was set but the top-level WHERE the rewriter
        # renders cannot be located — cannot prove no column is restricted, so
        # fail closed rather than let an unverified predicate execute.
        return ["(unverifiable WHERE)"]

    ref_names = [c.name for c in where_node.find_all(_sg_exp.Column)]
    return await _block_restricted_ref_names(
        ref_names, bound_query, restricted_ids, ctx, restricted_col_rows, db,
    )


async def _restricted_order_by_columns(
    bound_query: BoundQuery,
    restricted_ids: set[str],
    ctx: _ClsClosure,
    restricted_col_rows: list,
    db: AsyncSession,
) -> list[str]:
    """ORDER BY references that reach a restricted column (Bug-6140).

    A tag-restricted column is a RANKING ORACLE even when it is never
    projected or filtered: ``ORDER BY salary DESC LIMIT 10`` returns the ten
    highest earners' permitted columns, leaking the ordering of the hidden
    column one page at a time. The projection and filter gates
    (``_check_column_restrictions`` / ``_restricted_filter_columns``) do not
    see an ORDER BY-only reference, so a restricted column used solely to sort
    slipped through. Resolve each ORDER BY key the same way filters are
    resolved and fail closed on a restricted column.

    Resolution per ORDER BY ``field`` mirrors the rewriter's own order-key
    resolution (``source_sql._build_source_sql`` resolves an order-only name as
    a Dimension, then a Measure, from the model), so the gate sees exactly what
    would execute:
      * resolve the name to a Dimension (already-resolved first, then loaded by
        name from the model) and run the full column closure; a restricted
        dimension blocks — even one whose SEMANTIC name differs from its
        physical column;
      * else resolve it to a Measure (loaded by name) and run the closure — a
        measure that aggregates a restricted column (e.g. ``SUM(salary)``) used
        ONLY to sort is a ranking oracle and is blocked, even though it is
        never projected (so the projection gate never sees it);
      * else fall back to a physical-name match against the restricted columns —
        a raw column name in the ORDER BY that hits a restricted physical column
        is blocked rather than executed.
    """
    lq = bound_query.logical_query
    order_by = getattr(lq, "order_by", None) or []
    unresolvable = getattr(lq, "has_unresolvable_order", False)
    if not order_by and not unresolvable:
        return []

    # Collect every name referenced by the ORDER BY. For a plain (bare-column)
    # sort this is the tuple list; for an EXPRESSION sort key (LOWER(x),
    # amount*2, SUM(a)/COUNT(*), CASE ...) the parser sets
    # ``has_unresolvable_order`` and ``order_by`` is PARTIAL, so re-parse the
    # raw ORDER BY node and take every referenced column — fail closed if it
    # cannot be parsed (the rewriter would otherwise reconstruct that raw sort
    # over a possibly-restricted column).
    ref_names: list[Any] = []
    if unresolvable:
        import sqlglot
        from sqlglot import exp as _sg_exp

        raw = getattr(lq, "raw_query", None) or ""
        read_dialect = getattr(lq, "input_dialect", "postgres") or "postgres"
        try:
            # STRICT parse (default ErrorLevel.RAISE): a security gate must not
            # read a DEGRADED AST. Under a lenient parse, a syntactically bad
            # ORDER BY can yield an order node whose restricted column no longer
            # surfaces as an ``exp.Column`` (so ``find_all`` misses it) while the
            # rewriter still emits that column verbatim — a silent bypass. Any
            # parse failure falls through to the fail-closed sentinel below.
            tree = sqlglot.parse_one(raw, read=read_dialect)
        except Exception:
            tree = None
        # Target the TOP-LEVEL sort node the rewriter actually renders — mirror
        # ``source_sql._extract_raw_order_node`` exactly (outermost SELECT's
        # ``order`` arg, with the subquery-wrapper fallback). ``tree.find(Order)``
        # returns the first Order in walk order, which could be a NESTED sort
        # (window ``OVER (ORDER BY …)``, ``WITHIN GROUP (ORDER BY …)``,
        # ``array_agg(… ORDER BY …)``) while the rewriter still emits the
        # top-level sort — inspecting the wrong node would re-open the oracle.
        order_node = None
        if tree is not None:
            raw_select = (
                tree if isinstance(tree, _sg_exp.Select) else tree.find(_sg_exp.Select)
            )
            if raw_select is not None:
                order_node = raw_select.args.get("order")
                if order_node is None:
                    from_clause = raw_select.args.get("from_")
                    inner = getattr(from_clause, "this", None) if from_clause is not None else None
                    if isinstance(inner, _sg_exp.Subquery) and isinstance(inner.this, _sg_exp.Select):
                        order_node = inner.this.args.get("order")
        if order_node is None:
            # Cannot enumerate the top-level ORDER BY to prove none of its
            # columns are restricted — fail closed (block) rather than let the
            # rewriter emit an unverified sort over a possibly-restricted column.
            return ["(unverifiable ORDER BY)"]
        ref_names = [c.name for c in order_node.find_all(_sg_exp.Column)]
    else:
        for entry in order_by:
            # order_by entries are (field, direction) tuples; tolerate a bare name.
            ref_names.append(entry[0] if isinstance(entry, (tuple, list)) else entry)

    return await _block_restricted_ref_names(
        ref_names, bound_query, restricted_ids, ctx, restricted_col_rows, db,
    )


def _expand_select_alias_columns(
    ref_names: list,
    bound_query: BoundQuery,
) -> tuple[set[str], set[str]]:
    """Expand SELECT-alias references to their underlying column names (Bug-7047).

    The HAVING / ORDER BY security gate scans referenced names, but a bare
    reference can be a SELECT-list ALIAS whose underlying expression touches a
    restricted column. The source rewriter expands such an alias to its
    underlying expression before emitting SQL (``source_sql`` builds
    ``_alias_to_raw_expr`` by stripping the trailing ``AS <alias>`` from the
    SELECT item's raw text). Mirror that here so the gate checks the columns
    that would actually execute.

    Returns ``(underlying_column_names, unparseable_sentinels)``:
      * ``underlying_column_names`` — every column name referenced by the
        underlying expression of any alias present in ``ref_names``;
      * ``unparseable_sentinels`` — a fail-closed marker for each alias whose
        underlying expression cannot be parsed (the caller adds these to the
        blocked set so an unverifiable alias is rejected, never served).
    """
    import sqlglot
    from sqlglot import exp as _sg_exp
    from sqlglot import errors as _sg_err

    ref_lower = {str(n).lower() for n in ref_names if n}
    if not ref_lower:
        return set(), set()

    underlying: set[str] = set()
    unparseable: set[str] = set()
    for e in getattr(bound_query.logical_query, "select_expressions", []) or []:
        alias = getattr(e, "alias", None)
        raw_text = getattr(e, "raw_text", None)
        if not alias or not raw_text:
            continue
        if str(alias).lower() not in ref_lower:
            continue
        # Recover the underlying expression (strip the trailing ``AS <alias>``),
        # mirroring source_sql's alias-to-raw-expr construction.
        try:
            raw_ast = sqlglot.parse_one(raw_text, read="postgres")
            underlying_node = (
                raw_ast.this if isinstance(raw_ast, _sg_exp.Alias) else raw_ast
            )
        except (_sg_err.ParseError, _sg_err.TokenError):
            # Cannot enumerate the alias's column closure — fail closed.
            unparseable.add(f"(unverifiable alias {alias})")
            continue
        for col in underlying_node.find_all(_sg_exp.Column):
            if col.name:
                underlying.add(col.name)
    return underlying, unparseable


async def _block_restricted_ref_names(
    ref_names: list,
    bound_query: BoundQuery,
    restricted_ids: set[str],
    ctx: _ClsClosure,
    restricted_col_rows: list,
    db: AsyncSession,
) -> list[str]:
    """Return the names in *ref_names* that reach a persona-restricted column.

    Shared resolver for the ORDER BY (Bug-6140) and HAVING (Bug-6140-adjacent)
    threshold/ranking-oracle gates. Both clauses can reference a restricted
    column that is never projected or filtered, so both must resolve each
    referenced name EXACTLY as the source rewriter does and fail closed:

      * an already-resolved dimension, or a Dimension loaded by name from the
        model, whose column closure reaches a restricted column — blocked (even
        one whose SEMANTIC name differs from its restricted physical column);
      * a Measure loaded by name whose closure aggregates a restricted column
        (e.g. ``SUM(salary)``) — blocked even though it is never projected;
      * otherwise a physical-name match against the restricted columns.

    Loading the unresolved names against the model's Dimensions AND Measures and
    completing the CLS closure (so an order/having-only calculated / variant /
    UDA-backed measure is verified) mirrors the rewriter's own resolution — the
    gate sees exactly what would execute.
    """
    ref_names = [n for n in ref_names if n]
    if not ref_names:
        return []

    # Bug-7047: a HAVING / ORDER BY reference can be a SELECT-list ALIAS
    # (``... SUM(salary) AS avg_sal ... HAVING avg_sal > 1000``). The rewriter
    # (``source_sql._qualify_having`` / order-key resolution) expands a bare
    # alias to its UNDERLYING expression (``SUM(salary)``) before emitting SQL,
    # so a restricted column reached only through an alias would execute while
    # the raw-column scan above sees only the alias name and misses it. Expand
    # every alias reference to the column names its underlying expression
    # touches, exactly as the rewriter does, and fold those into the checked
    # set. Fail closed: an alias whose underlying expression cannot be parsed
    # is surfaced as an unverifiable reference and blocked.
    alias_underlying_cols, alias_unparseable = _expand_select_alias_columns(
        ref_names, bound_query,
    )
    if alias_unparseable:
        ref_names = list(ref_names) + list(alias_unparseable)
    if alias_underlying_cols:
        ref_names = list(ref_names) + list(alias_underlying_cols)

    dims_by_name = dict(getattr(bound_query, "resolved_dimensions_by_name", {}) or {})
    for d in bound_query.resolved_dimensions:
        dims_by_name.setdefault(getattr(d, "name", None), d)
    meas_by_name = {
        getattr(m, "name", None): m for m in bound_query.resolved_measures
    }

    # Bug-7607 R2-1: populate the calc-dim lookups together (see above).
    await _ensure_cls_physical_lookups(ctx, bound_query, list(restricted_col_rows), db)
    restricted_phys = ctx.restricted_physical_names or set()

    unresolved = [
        n for n in ref_names if n not in dims_by_name and n not in meas_by_name
    ]
    if unresolved:
        from shared.db.models import Dimension as _Dimension
        from shared.db.models import Measure as _Measure
        from shared.db.models import UserDefinedAttributeColumnRef as _UdaColRef

        model_id = bound_query.model.id
        dim_rows = (
            await db.execute(
                select(_Dimension).where(
                    _Dimension.model_id == model_id,
                    _Dimension.name.in_(unresolved),
                )
            )
        ).scalars().all()
        for d in dim_rows:
            dims_by_name.setdefault(getattr(d, "name", None), d)
        # Bug-7607 R2-1 (R3 completion): a Dimension freshly loaded here may be a
        # CALCULATED dimension that was absent from resolved_dimensions_by_name
        # when the pre-load ran, so the calc-dim lookups (known_physical_names /
        # table_identifiers) may not be populated. If any loaded dimension is a
        # calc dimension, load those lookups now so its calc-expr gate can verify
        # real columns instead of failing closed (over-blocking) or, worse,
        # under-detecting a whole-row reference.
        if any(getattr(d, "calc_expression", None) for d in dim_rows):
            if ctx.known_physical_names is None:
                ctx.known_physical_names = await _all_model_physical_names(
                    model_id, db,
                )
            if ctx.table_identifiers is None:
                ctx.table_identifiers = await _model_table_identifiers(
                    model_id, db,
                )
        still = [n for n in unresolved if n not in dims_by_name]
        if still:
            meas_rows = (
                await db.execute(
                    select(_Measure).where(
                        _Measure.model_id == model_id,
                        _Measure.name.in_(still),
                    )
                )
            ).scalars().all()
            for m in meas_rows:
                meas_by_name.setdefault(getattr(m, "name", None), m)
        # Complete the closure so an order/having-only calculated / variant /
        # UDA-backed measure is verified accurately (and fails closed on an
        # unparseable expression) rather than silently under-detected because ctx
        # was built only for the projected objects.
        if meas_by_name:
            all_meas = (
                await db.execute(select(_Measure).where(_Measure.model_id == model_id))
            ).scalars().all()
            for m in all_meas:
                ctx.measures_by_id.setdefault(str(m.id), m)
                ctx.measures_by_name.setdefault(m.name, m)
            # Bug-7812: gate on the load FLAG, not set-emptiness — the
            # _ensure_cls_physical_lookups call above already loads the UDA
            # restrictions idempotently (and a legitimately empty set is a valid
            # loaded state that must not trigger a re-query).
            if not ctx.uda_restrictions_loaded:
                uda_rows = (
                    await db.execute(
                        select(_UdaColRef.attribute_id)
                        .where(_UdaColRef.column_id.in_(list(restricted_col_rows)))
                    )
                ).scalars().all()
                ctx.restricted_uda_ids = {str(a) for a in uda_rows}
                ctx.uda_restrictions_loaded = True

    # Bug-6833: build case-insensitive lookup dicts for the security gate.
    # The filter gate's physical-name fallback (line 1253) is case-insensitive,
    # but the semantic-name lookup was exact-case --- an ORDER BY on
    # "Revenue" (capital R) with a measure named "revenue" (lower r) would
    # bypass the restricted-column check and let the rewriter emit a sort
    # over a restricted column. Fail closed: if the name matches a
    # semantic object case-insensitively, run the restriction closure.
    dims_by_name_ci = {(k or "").lower(): v for k, v in dims_by_name.items()}
    meas_by_name_ci = {(k or "").lower(): v for k, v in meas_by_name.items()}

    blocked: list[str] = []
    for name in ref_names:
        name_lower = str(name).lower()
        obj = (
            dims_by_name.get(name)
            or meas_by_name.get(name)
            or dims_by_name_ci.get(name_lower)
            or meas_by_name_ci.get(name_lower)
        )
        if obj is not None:
            if _touches_restricted_columns(obj, restricted_ids, ctx):
                blocked.append(name)
            continue
        if name_lower in restricted_phys:
            blocked.append(name)
    return blocked


async def _restricted_having_columns(
    bound_query: BoundQuery,
    restricted_ids: set[str],
    ctx: _ClsClosure,
    restricted_col_rows: list,
    db: AsyncSession,
) -> list[str]:
    """HAVING references that reach a restricted column (Bug-6140-adjacent).

    A tag-restricted column is a THRESHOLD ORACLE through HAVING even when it is
    never projected, filtered, or sorted: ``... GROUP BY dept HAVING
    SUM(salary) > 1000000`` returns only the groups whose hidden aggregate clears
    the probed threshold, leaking the restricted column's distribution one probe
    at a time. The projection, filter, and ORDER BY gates never see a HAVING-only
    reference, so — exactly like the ORDER BY oracle — a restricted column used
    solely in HAVING slipped through. Parse the HAVING strictly, enumerate every
    referenced column, and fail closed on a restricted column (or on a parse
    failure the rewriter would otherwise reconstruct verbatim).
    """
    lq = bound_query.logical_query
    having_raw = getattr(lq, "having_raw", None)
    having_columns = getattr(lq, "having_columns", None) or []
    if not having_raw and not having_columns:
        return []

    ref_names: list[Any] = list(having_columns)
    if having_raw:
        import sqlglot
        from sqlglot import exp as _sg_exp

        try:
            # STRICT parse (default ErrorLevel.RAISE) of the postgres-canonical
            # HAVING text the rewriter emits (``_extract_having`` round-trips it
            # via ``dialect="postgres"``). A degraded parse could hide a
            # restricted column as a non-``exp.Column`` token while the rewriter
            # still emits it verbatim, so any parse failure fails closed.
            having_tree = sqlglot.parse_one(f"SELECT 1 {having_raw}", read="postgres")
        except Exception:
            having_tree = None
        if having_tree is None:
            return ["(unverifiable HAVING)"]
        having_node = having_tree.args.get("having")
        if having_node is None:
            return ["(unverifiable HAVING)"]
        ref_names = [c.name for c in having_node.find_all(_sg_exp.Column)]

    return await _block_restricted_ref_names(
        ref_names, bound_query, restricted_ids, ctx, restricted_col_rows, db,
    )


async def _restricted_derived_expression_columns(
    bound_query: BoundQuery,
    restricted_ids: set[str],
    ctx: _ClsClosure,
    restricted_col_rows: list,
    db: AsyncSession,
) -> list[str]:
    """Bound derived-expression LEAF columns that reach a restricted column.

    CLS derived-expression leaf gap (intake
    2026-07-14-cls-derived-expression-leaf-not-checked-source-path): the
    projection / filter / order / having gates iterate resolved
    measures/dimensions and their expression closures, but a FUNCTION GRAIN such
    as ``SELECT UPPER(country) ... GROUP BY UPPER(country)`` contributes no bare
    grain dimension — the restricted leaf ``country`` is referenced ONLY inside
    the bound derived expression, so it was never in the touched-column set and
    the query source-routed and disclosed the restricted values (UPPER =
    disclosure modulo case; DATE_TRUNC = partial disclosure). This is a
    pre-existing source-route gap independent of the derived-serving feature.

    Root-cause fix: walk every ``bound_query.bound_derived_expressions`` leaf and
    block if any leaf is restricted. The binder resolves each leaf to a stable
    ``ModelColumn.id`` (spec §7.1) so the shared check is id-first (cross-relation
    exact); for a leaf the binder left unbound (``column_id == ""``) it falls back
    to the restricted physical-name set. Fail closed on that fallback path, so a
    restricted leaf blocks whether or not the binder pinned its id.
    """
    from shared.security.restricted_column_closure import (
        derived_expression_leaves_restricted,
    )

    derived = getattr(bound_query, "bound_derived_expressions", None) or []
    if not derived:
        return []

    # The name fallback needs the restricted physical-name set loaded. It is
    # loaded lazily elsewhere (calc-dim / narrowed-star paths); ensure it here so
    # a leaf the binder left unbound is still matched by physical name.
    if ctx.restricted_physical_names is None:
        ctx.restricted_physical_names = await _restricted_physical_names(
            list(restricted_col_rows), db,
        )

    if derived_expression_leaves_restricted(
        derived, restricted_ids, ctx.restricted_physical_names,
    ):
        return ["(restricted column in derived expression)"]
    return []


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
        # F-008-02: non-disclosing — do not name the persona or its restrictions.
        logger.info(
            "[CLS_DENY] reason=complex_sql persona_id=%s persona=%r",
            str(persona.id), getattr(persona, "name", None),
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "COLUMN_RESTRICTED",
                "message": (
                    "This query uses advanced SQL constructs (for example "
                    "subqueries, CTEs, set operations, window functions, or "
                    "non-standard aggregates) that cannot be verified against "
                    "your effective access, so it was rejected. Rewrite the "
                    "query as a plain SELECT over the model."
                ),
            },
        )

    ctx = await _build_cls_closure(bound_query, list(restricted_col_rows), db)

    # CLS derived-expression leaf gap (intake
    # 2026-07-14-cls-derived-expression-leaf-not-checked-source-path): a function
    # grain such as ``GROUP BY UPPER(restricted_col)`` contributes NO bare grain
    # dimension, so a restricted leaf referenced ONLY inside the expression was
    # never in the touched-column set and slipped past CLS on the SOURCE route
    # (UPPER = disclosure modulo case). The block applies on EVERY route (the
    # source route is what executes and discloses) and is never "narrowed away".
    # Fail closed: the shared check is id-first (stable leaf lineage, §7.1) with a
    # restricted-physical-name fallback for leaves the binder left unbound.
    derived_blocked = await _restricted_derived_expression_columns(
        bound_query, restricted_ids, ctx, list(restricted_col_rows), db,
    )

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
                # F-008-02: non-disclosing — a generic "nothing to return", not
                # "every column is restricted for persona X" (which confirms the
                # table is fully CLS-covered for this audience).
                logger.info(
                    "[CLS_DENY] reason=star_fully_restricted persona_id=%s persona=%r",
                    str(persona.id), getattr(persona, "name", None),
                )
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error_code": "OBJECT_NOT_AVAILABLE",
                        "message": (
                            "No columns are available to return for this query "
                            "with your current access."
                        ),
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
        # Bug-6140: an ORDER BY on a restricted column is a ranking oracle and
        # is likewise never "narrowed away".
        order_blocked = await _restricted_order_by_columns(
            bound_query, restricted_ids, ctx, list(restricted_col_rows), db,
        )
        # Bug-6140-adjacent: a HAVING on a restricted column is a threshold
        # oracle and is likewise never "narrowed away".
        having_blocked = await _restricted_having_columns(
            bound_query, restricted_ids, ctx, list(restricted_col_rows), db,
        )
        return filter_blocked + order_blocked + having_blocked + derived_blocked

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
    # Bug-6140: block ORDER BY-only references to restricted columns
    # (ranking oracle) — fail closed.
    blocked.extend(
        await _restricted_order_by_columns(
            bound_query, restricted_ids, ctx, list(restricted_col_rows), db,
        )
    )
    # Bug-6140-adjacent: block HAVING-only references to restricted columns
    # (threshold oracle) — fail closed.
    blocked.extend(
        await _restricted_having_columns(
            bound_query, restricted_ids, ctx, list(restricted_col_rows), db,
        )
    )
    # CLS derived-expression leaf gap: block a function grain over a restricted
    # leaf (``GROUP BY UPPER(restricted)``) — fail closed on the source route.
    blocked.extend(derived_blocked)

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
