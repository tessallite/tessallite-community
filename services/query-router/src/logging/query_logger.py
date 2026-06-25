"""
Query Logger — writes QueryLog, QueryMissLog, and RouteLog entries
to the tenant metadata DB after each query execution.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import PocketDefinition, QueryLog, QueryMissLog, RouteLog
from shared.pocket.fingerprint import predicate_set_hash
from src.ir.logical_query import BoundQuery, RouteDecision

# F-004-11: matches the widened query_miss_logs.miss_reason column (migration
# 0135). The aggregate-skip reason joins several skip tokens and overran 64.
_MISS_REASON_MAX_LEN = 255


async def _source_baseline_ms(
    db: AsyncSession,
    model_id: Optional[uuid.UUID],
    fingerprint: str,
) -> float:
    """Average source-route execution time (ms) for this query fingerprint.

    F-005-08 (Bug-2251): the baseline against which pocket "time saved" is
    measured — what the same query cost when it ran against the source. Returns
    0.0 when no source-routed run is on record (then no saving is credited).
    """
    if not model_id or not fingerprint:
        return 0.0
    result = await db.execute(
        select(func.avg(QueryLog.execution_ms)).where(
            QueryLog.model_id == model_id,
            QueryLog.query_fingerprint == fingerprint,
            QueryLog.route_type == "source",
            QueryLog.status == "success",
        )
    )
    avg = result.scalar_one_or_none()
    return float(avg) if avg is not None else 0.0


def _merge_predicate_variant(
    existing: Optional[list],
    predicates: list[dict],
    now: datetime,
) -> list[dict]:
    """Accumulate a per-literal-variant predicate breakdown on a miss row.

    F-005-14 (Bug-2256): a QueryMissLog row is keyed on the literal-free
    fingerprint, so ``predicates_json`` only ever holds the latest variant. This
    keeps a list of ``{predicate_set_hash, predicates, occurrence_count,
    last_seen_at}`` so the pocket suggester can attribute hits to the SPECIFIC
    literal slice it would cache, instead of crediting every variant's hits to
    the latest one. Bounded to keep the JSONB small.
    """
    variants: list[dict] = list(existing or [])
    pred_hash = predicate_set_hash(predicates)
    iso_now = now.isoformat()
    for v in variants:
        if v.get("predicate_set_hash") == pred_hash:
            v["occurrence_count"] = int(v.get("occurrence_count", 0)) + 1
            v["last_seen_at"] = iso_now
            v["predicates"] = predicates
            return variants
    variants.append({
        "predicate_set_hash": pred_hash,
        "predicates": predicates,
        "occurrence_count": 1,
        "last_seen_at": iso_now,
    })
    # Cap the number of tracked variants (keep the most recently seen) so a
    # high-cardinality literal column cannot grow the row without bound.
    _MAX_VARIANTS = 50
    if len(variants) > _MAX_VARIANTS:
        variants.sort(key=lambda x: x.get("last_seen_at", ""), reverse=True)
        variants = variants[:_MAX_VARIANTS]
    return variants


def _extract_group_by(sql: str) -> list[str]:
    """Extract GROUP BY column names from a SQL string."""
    if not sql:
        return []
    import re
    match = re.search(r"GROUP\s+BY\s+(.+?)(?:\s+ORDER\s+|\s+LIMIT\s+|\s+OFFSET\s+|\s+HAVING\s+|$)", sql, re.IGNORECASE)
    if not match:
        return []
    return [col.strip().strip('"') for col in match.group(1).split(",")]


async def log_query(
    db: AsyncSession,
    bound_query: BoundQuery,
    decision: RouteDecision,
    execution_ms: int,
    rows_returned: int,
    bytes_processed: int,
    user_identity: Optional[str] = None,
    persona_id: Optional[uuid.UUID] = None,
    client_kind: Optional[str] = None,
    security_rules_applied: Optional[list[dict]] = None,
) -> QueryLog:
    """Write a QueryLog row and pipeline trace RouteLog rows. Return the QueryLog."""
    log = QueryLog(
        model_id=bound_query.model.id,
        user_identity=user_identity,
        protocol=bound_query.logical_query.protocol,
        raw_query=bound_query.logical_query.raw_query,
        query_fingerprint=bound_query.logical_query.query_fingerprint,
        route_type=decision.route_type,
        aggregate_id=decision.aggregate_id,
        pocket_id=decision.pocket_id,
        persona_id=persona_id,
        client_kind=client_kind,
        rewritten_query=decision.rewritten_query,
        execution_ms=execution_ms,
        rows_returned=rows_returned,
        bytes_processed=bytes_processed,
        security_rules_applied=security_rules_applied or getattr(decision, "security_rules_applied", None),
    )
    db.add(log)
    await db.flush()

    lq = bound_query.logical_query

    # Stage 1: Parser trace
    select_expr_summary = []
    for e in getattr(lq, "select_expressions", []):
        select_expr_summary.append({
            "raw": e.raw_text[:120],
            "class": e.classification,
            "agg_fn": e.agg_function,
            "col": e.inner_column,
            "lit": e.inner_literal,
            "alias": e.alias,
        })

    db.add(RouteLog(
        query_log_id=log.id,
        route_stage="parse",
        detail={
            "select_star": lq.select_star,
            "grain": lq.grain,
            "measures": lq.requested_measures,
            "dimensions": lq.requested_dimensions,
            "filters": [{"dim": f.dimension_name, "op": f.operator} for f in lq.filters],
            "order_by": lq.order_by,
            "limit": lq.limit,
            "offset": lq.offset,
            "fingerprint": lq.query_fingerprint[:16],
            "from_tables": getattr(lq, "from_tables", []),
            "syntax_warnings": getattr(lq, "syntax_warnings", []),
            "select_expressions": select_expr_summary,
            "having_raw": getattr(lq, "having_raw", None),
            "having_columns": getattr(lq, "having_columns", []),
            "has_unresolvable_where": getattr(lq, "has_unresolvable_where", False),
        },
    ))

    # Stage 2: Binder trace
    hierarchy_level_dims = [
        {
            "name": d.name,
            "hierarchy_id": str(getattr(d, "hierarchy_id", "")),
            "hierarchy_name": getattr(d, "hierarchy_name", ""),
            "ordinal": getattr(d, "hierarchy_level_ordinal", None),
        }
        for d in bound_query.resolved_dimensions
        if getattr(d, "is_hierarchy_level", False)
    ]
    db.add(RouteLog(
        query_log_id=log.id,
        route_stage="bind",
        detail={
            "resolved_dimensions": [d.name for d in bound_query.resolved_dimensions],
            "resolved_measures": [m.name for m in bound_query.resolved_measures],
            "has_passthrough": getattr(bound_query, "has_passthrough_expressions", False),
            "filter_count": len(bound_query.resolved_filters),
            "hierarchy_level_dims": hierarchy_level_dims,
        },
    ))

    # Stage 3: Route decision trace
    rewritten_group_by = _extract_group_by(decision.rewritten_query)
    route_detail = {
        "route_type": decision.route_type,
        "reason": decision.reason,
        "aggregate_id": decision.aggregate_id,
        "pocket_id": decision.pocket_id,
        "rewritten_group_by": rewritten_group_by,
    }
    pocket_skipped_reason = getattr(decision, "pocket_skipped_reason", None)
    if pocket_skipped_reason:
        route_detail["pocket_skipped_reason"] = pocket_skipped_reason
    agg_skipped = getattr(decision, "aggregate_skipped_reasons", None)
    if agg_skipped:
        route_detail["aggregate_skipped_reasons"] = agg_skipped
    db.add(RouteLog(
        query_log_id=log.id,
        route_stage="route",
        detail=route_detail,
    ))

    if decision.route_type == "pocket" and decision.pocket_id:
        try:
            pocket_id = uuid.UUID(str(decision.pocket_id))
        except (TypeError, ValueError):
            pocket_id = None
        if pocket_id is not None:
            # F-030-19: atomic counter increment. A read-modify-write on the ORM
            # instance loses increments when two pocket-routed queries run
            # concurrently (and the loss compounds across replicas) — pocket
            # eviction/ROI decisions consume these counters, so the undercount
            # is load-bearing. An UPDATE ... SET col = col + :n expression makes
            # the increment a single atomic DB operation. COALESCE guards the
            # historical NULL rows the old code defended against.
            now = datetime.now(timezone.utc)
            # F-005-08 (Bug-2251): "time saved" is the time the pocket AVOIDED —
            # (source baseline for this query − pocket execution) — not the time
            # the pocket query itself took (the old `+ execution_ms` meant a
            # SLOWER pocket reported MORE saved time). Baseline = average
            # source-route execution recorded for the same fingerprint; if no
            # prior source run exists, credit 0 this turn rather than overstate.
            baseline_ms = await _source_baseline_ms(
                db,
                bound_query.model.id if bound_query and bound_query.model else None,
                bound_query.logical_query.query_fingerprint if bound_query else "",
            )
            saved = max(int(baseline_ms) - max(execution_ms, 0), 0) if baseline_ms else 0
            await db.execute(
                update(PocketDefinition)
                .where(PocketDefinition.id == pocket_id)
                .values(
                    last_access_at=now,
                    last_match_at=now,
                    hit_count=func.coalesce(PocketDefinition.hit_count, 0) + 1,
                    time_saved_ms_total=(
                        func.coalesce(PocketDefinition.time_saved_ms_total, 0)
                        + saved
                    ),
                )
            )

    await db.commit()
    return log


async def log_query_failure(
    db: AsyncSession,
    bound_query: BoundQuery,
    decision: RouteDecision,
    execution_ms: int,
    user_identity: Optional[str] = None,
    persona_id: Optional[uuid.UUID] = None,
    client_kind: Optional[str] = None,
    error_type: str = "execution_error",
    error_detail: str = "",
) -> QueryLog:
    """Persist a failed query as a QueryLog row with status='error'."""
    log = QueryLog(
        model_id=bound_query.model.id if bound_query and bound_query.model else None,
        user_identity=user_identity,
        protocol=bound_query.logical_query.protocol if bound_query else "unknown",
        raw_query=bound_query.logical_query.raw_query if bound_query else "",
        query_fingerprint=bound_query.logical_query.query_fingerprint if bound_query else "",
        route_type=decision.route_type if decision else "unknown",
        aggregate_id=getattr(decision, "aggregate_id", None) if decision else None,
        pocket_id=getattr(decision, "pocket_id", None) if decision else None,
        persona_id=persona_id,
        client_kind=client_kind,
        rewritten_query=getattr(decision, "rewritten_query", None) if decision else None,
        execution_ms=execution_ms,
        rows_returned=0,
        bytes_processed=0,
        status="error",
        error_type=error_type[:64],
        error_detail=error_detail[:2000],
    )
    db.add(log)
    await db.commit()
    return log


async def log_query_miss(
    db: AsyncSession,
    bound_query: BoundQuery,
    miss_reason: str,
    persona_id: Optional[uuid.UUID] = None,
) -> None:
    """
    Upsert a QueryMissLog entry keyed on
    ``(model_id, query_fingerprint, persona_id)``.

    Misses under different personas are tracked as separate rows so
    the optimizer can partition its workload scan per persona.
    Global (persona_id = NULL) misses still dedupe across repeat
    occurrences because the unique constraint declares ``NULLS NOT
    DISTINCT`` (migration 0041).
    """
    from sqlalchemy import select

    fingerprint = bound_query.logical_query.query_fingerprint
    model_id = bound_query.model.id
    stored_reason = miss_reason[:_MISS_REASON_MAX_LEN]

    miss_filter = [
        QueryMissLog.model_id == model_id,
        QueryMissLog.query_fingerprint == fingerprint,
    ]
    if persona_id is None:
        miss_filter.append(QueryMissLog.persona_id.is_(None))
    else:
        miss_filter.append(QueryMissLog.persona_id == persona_id)

    result = await db.execute(select(QueryMissLog).where(*miss_filter))
    existing = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    lq = bound_query.logical_query
    predicates_json = [
        {"column_name": f.dimension_name, "operator": f.operator, "value": f.value}
        for f in bound_query.resolved_filters
    ]

    if existing is not None:
        existing.occurrence_count += 1
        existing.last_seen_at = now
        existing.miss_reason = stored_reason
        filter_dim_names = {
            f.dimension_name
            for f in bound_query.resolved_filters
            if f.dimension_name
        }
        existing.requested_dimensions = sorted(
            {d.name for d in bound_query.resolved_dimensions} | filter_dim_names
        )
        existing.requested_grain = sorted(
            set(lq.grain) | filter_dim_names
        )
        existing.requested_measures = [m.name for m in bound_query.resolved_measures]
        existing.candidate_aggregate_id = None
        existing.predicates_json = predicates_json
        # F-005-14: track per-literal-variant hit counts for the pocket path.
        existing.predicate_variants_json = _merge_predicate_variant(
            existing.predicate_variants_json, predicates_json, now
        )
        existing.has_unresolvable_where = lq.has_unresolvable_where
        existing.has_complex_sql = lq.has_complex_sql
    else:
        # The aggregate matcher requires both GROUP BY and WHERE filter dimensions
        # in the grain (aggregate_matcher.py: required_grain = requested_grain |
        # filter_dimension_names). Record the same full set here so the telemetry
        # snapshot delivered to the AI optimiser contains a complete grain — omitting
        # filter dimensions would cause the LLM to recommend under-specified aggregates
        # that the matcher would immediately reject.
        filter_dim_names = {
            f.dimension_name
            for f in bound_query.resolved_filters
            if f.dimension_name
        }
        all_dimensions = sorted(
            {d.name for d in bound_query.resolved_dimensions} | filter_dim_names
        )
        full_grain = sorted(
            set(lq.grain) | filter_dim_names
        )
        miss = QueryMissLog(
            model_id=model_id,
            query_fingerprint=fingerprint,
            miss_reason=stored_reason,
            normalized_query=lq.raw_query,
            requested_dimensions=all_dimensions,
            requested_measures=[m.name for m in bound_query.resolved_measures],
            requested_grain=full_grain,
            occurrence_count=1,
            first_seen_at=now,
            last_seen_at=now,
            persona_id=persona_id,
            predicates_json=predicates_json,
            # F-005-14: seed the per-variant breakdown with this first occurrence.
            predicate_variants_json=_merge_predicate_variant(None, predicates_json, now),
            has_unresolvable_where=lq.has_unresolvable_where,
            has_complex_sql=lq.has_complex_sql,
        )
        db.add(miss)

    await db.commit()
