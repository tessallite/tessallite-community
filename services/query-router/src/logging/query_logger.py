"""
Query Logger — writes QueryLog, QueryMissLog, and RouteLog entries
to the tenant metadata DB after each query execution.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import logging

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from shared.db.models import PocketDefinition, QueryLog, QueryMissLog, RouteLog
from shared.miss_reason_taxonomy import (
    BUILD,
    INELIGIBLE,
    REPAIR,
    classify_reason,
)
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

    Bug-6426: the result cache stores source-routed results, so an in-TTL
    re-serve writes a ``route_type="source"`` row with ``execution_ms=0`` and
    ``cache_status="cache_hit"``. Averaging those zeros drags the baseline toward
    0 and UNDERSTATES the pocket "time saved" credited on real executions.
    Exclude cache re-serves so the baseline reflects real source runtimes only.
    """
    if not model_id or not fingerprint:
        return 0.0
    result = await db.execute(
        select(func.avg(QueryLog.execution_ms)).where(
            QueryLog.model_id == model_id,
            QueryLog.query_fingerprint == fingerprint,
            QueryLog.route_type == "source",
            QueryLog.status == "success",
            QueryLog.cache_status.is_distinct_from("cache_hit"),
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


# Bug-8071: cap on distinct reasons tracked per miss row. Reason codes come from
# the closed ``AggregateSkipReason`` vocabulary, but the joined
# ``aggregate_skip:a,b`` form makes the stored string's cardinality
# combinatorial, so the list is bounded like ``predicate_variants_json`` is.
_MAX_MISS_REASONS = 20
_MISS_REASON_CLASSES = (BUILD, REPAIR, INELIGIBLE)


def _reason_summary_entry(
    totals: dict[str, int],
    first_seen_at: str,
    last_seen_at: str,
) -> dict:
    clean = {key: max(0, int(totals.get(key, 0))) for key in _MISS_REASON_CLASSES}
    return {
        "class_totals": clean,
        "occurrence_count": sum(clean.values()),
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
    }


def _merge_miss_reason(
    existing: Optional[list],
    reason: str,
    now: datetime,
    *,
    legacy_reason: str | None = None,
    legacy_occurrence_count: int = 0,
    legacy_first_seen_at: datetime | str | None = None,
) -> list[dict]:
    """Accumulate a per-reason history on a miss row (Bug-8071).

    A QueryMissLog row is a rollup whose conflict-update OVERWRITES
    ``miss_reason``, so the row remembered only the last event: a pattern that
    missed 400 times for a missing grain and 3 times because the aggregate was
    stale read as "stale". A modeller could not prove why a query kept missing,
    and the optimizer could not tell an absent aggregate from an unservable one.

    Keeps ``{reason, occurrence_count, first_seen_at, last_seen_at}`` per
    distinct reason string. Bounded (least-recently-seen evicted first) so a
    combinatorial joined reason cannot grow the row without limit. Evicted
    entries are folded into a class-total summary, preserving the optimizer's
    build/repair/ineligible evidence exactly.

    ``legacy_*`` describes the row before this event. It initializes history
    for rows created before the histogram column existed and preserves any
    shortfall left by the old eviction logic without reclassifying it as the
    new reason.
    """
    reasons: list[dict] = []
    class_totals = {key: 0 for key in _MISS_REASON_CLASSES}
    summary_first = ""
    summary_last = ""
    recorded = 0
    for entry in existing or []:
        if not isinstance(entry, dict):
            continue
        entry_totals = entry.get("class_totals")
        if isinstance(entry_totals, dict):
            for key in _MISS_REASON_CLASSES:
                try:
                    count = max(0, int(entry_totals.get(key, 0)))
                except (TypeError, ValueError):
                    count = 0
                class_totals[key] += count
                recorded += count
            summary_first = min(
                filter(None, [summary_first, str(entry.get("first_seen_at") or "")]),
                default="",
            )
            summary_last = max(summary_last, str(entry.get("last_seen_at") or ""))
            continue
        if "reason" not in entry:
            continue
        reasons.append(dict(entry))
        try:
            recorded += max(0, int(entry.get("occurrence_count", 0)))
        except (TypeError, ValueError):
            pass

    iso_now = now.isoformat()
    legacy_count = max(0, int(legacy_occurrence_count or 0))
    legacy_shortfall = max(0, legacy_count - recorded)
    if legacy_shortfall and legacy_reason:
        class_totals[classify_reason(legacy_reason)] += legacy_shortfall
        legacy_first = (
            legacy_first_seen_at.isoformat()
            if isinstance(legacy_first_seen_at, datetime)
            else str(legacy_first_seen_at or iso_now)
        )
        summary_first = min(filter(None, [summary_first, legacy_first]), default=legacy_first)
        summary_last = max(summary_last, iso_now)

    matched = False
    for entry in reasons:
        if entry.get("reason") == reason:
            entry["occurrence_count"] = int(entry.get("occurrence_count", 0)) + 1
            entry["last_seen_at"] = iso_now
            matched = True
            break
    if not matched:
        reasons.append({
            "reason": reason,
            "occurrence_count": 1,
            "first_seen_at": iso_now,
            "last_seen_at": iso_now,
        })

    has_summary = any(class_totals.values())
    reason_slots = _MAX_MISS_REASONS - (1 if has_summary else 0)
    if len(reasons) > reason_slots:
        reasons.sort(key=lambda x: x.get("last_seen_at", ""), reverse=True)
        if not has_summary:
            reason_slots = _MAX_MISS_REASONS - 1
        evicted = reasons[reason_slots:]
        reasons = reasons[:reason_slots]
        for entry in evicted:
            try:
                count = max(0, int(entry.get("occurrence_count", 0)))
            except (TypeError, ValueError):
                count = 0
            class_totals[classify_reason(entry.get("reason"))] += count
            summary_first = min(
                filter(None, [summary_first, str(entry.get("first_seen_at") or "")]),
                default=summary_first,
            )
            summary_last = max(summary_last, str(entry.get("last_seen_at") or ""))

    if any(class_totals.values()):
        reasons.append(_reason_summary_entry(
            class_totals,
            summary_first or iso_now,
            summary_last or iso_now,
        ))
    return reasons


def _extract_group_by(sql: str) -> list[str]:
    """Extract GROUP BY column names from a SQL string."""
    if not sql:
        return []
    import re
    match = re.search(r"GROUP\s+BY\s+(.+?)(?:\s+ORDER\s+|\s+LIMIT\s+|\s+OFFSET\s+|\s+HAVING\s+|$)", sql, re.IGNORECASE)
    if not match:
        return []
    return [col.strip().strip('"') for col in match.group(1).split(",")]


def _usage_refs(bound_query: BoundQuery) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Stable usage references produced by semantic binding.

    Returns ``(column_usage_refs, semantic_object_refs)``, both persisted on the
    bind RouteLog so model-service does not need to guess table identity later
    from free-form SQL. Roles remain separate: one column selected and filtered
    in the same query is two semantic references.

    ``column_usage_refs`` is the PHYSICAL layer — one entry per bound
    ``ModelColumn``. Unchanged contract.

    ``semantic_object_refs`` closes the hole that leaves (Bug-8483). A
    calculated measure, a variant measure, or a UDA-backed measure/dimension has
    no ``source_column_id``, so binding produces no physical column for it and
    the consumer previously saw NOTHING — a physical column feeding a heavily
    used calculated measure reported zero usage, and a modeller reading that can
    drop it and break every downstream consumer. Recording the semantic object
    identity lets model-service expand it through the dependency closure it
    already owns (calc references, variant lineage, UDA column refs), which is
    where that knowledge lives; the query-router does not carry the
    expression-resolution machinery and this is the hot query path.

    Only objects that contributed ZERO physical columns are listed, so an
    ordinary directly-bound measure is still counted exactly once, through
    ``column_usage_refs`` alone.
    """
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    # (object_type, object_id, role) -> object name, for objects that produced
    # no physical column; and the same key set for those that did.
    observed: dict[tuple[str, str, str], str] = {}
    covered: set[tuple[str, str, str]] = set()

    def add(column_id, semantic_type: str, semantic_id, name: str, role: str) -> bool:
        """Record a physical column reference. True when one was supplied.

        The return value reports whether the SEMANTIC OBJECT resolved to a
        physical column, not whether a new row was appended — a repeated
        reference is deduped but the object is still covered.
        """
        if not column_id:
            return False
        key = (str(column_id), semantic_type, str(semantic_id or ""), role)
        if key not in seen:
            seen.add(key)
            refs.append({
                "column_id": str(column_id),
                "semantic_type": semantic_type,
                "semantic_id": str(semantic_id or ""),
                "semantic_name": str(name or ""),
                "role": role,
            })
        return True

    def note_object(object_type: str, object_id, name: str, role: str, has_column: bool) -> None:
        if not object_id:
            return
        key = (object_type, str(object_id), role)
        if has_column:
            covered.add(key)
        else:
            observed.setdefault(key, str(name or ""))

    def add_dimension(dimension, role: str) -> None:
        if dimension is None:
            return
        # The binder wraps a measure referenced outside an aggregate as a
        # synthetic dimension carrying the MEASURE's id. Record BOTH the physical
        # ref and the object note under its real object type — writing
        # ``semantic_type: "dimension"`` next to a measure id makes the persisted
        # trace self-inconsistent and would send any future consumer of
        # ``semantic_type`` looking up a dimension that does not exist.
        object_type = (
            "measure" if getattr(dimension, "is_measure_as_dimension", False) else "dimension"
        )
        # "Covered" means the dimension's OWN VALUE resolved to a physical
        # column. Only ``source_column_id`` says that. ``display_column_id`` is
        # a CAPTION choice — the column whose value is shown INSTEAD of the key
        # — so it is an AUXILIARY dependency exactly like a measure's account
        # or date column, and it must never make the dimension count as bound.
        #
        # Bug-8697 (AKA source Bug-8737; the dimension half of the Bug-8483
        # rule applied to measures below): a dimension whose key column is gone
        # but whose display column survives — reachable because BOTH FKs are
        # ``ON DELETE SET NULL`` (``models.py`` Dimension.source_column_id /
        # .display_column_id), so dropping the key column from the source
        # nulls one and leaves the other — was marked covered by its caption
        # alone. That suppressed its ``semantic_object_ref``, so the consumer's
        # dependency closure (``impact_usage.py``, which walks
        # ``source_column_id``, ``display_column_id``, the UDA columns and
        # ``calc_expression_column_ids``) never ran and the columns the
        # dimension really reads reported ZERO usage — a modeller reading that
        # can drop a column every query depends on.
        has_column = add(
            getattr(dimension, "source_column_id", None),
            object_type,
            getattr(dimension, "id", None),
            getattr(dimension, "name", ""),
            role,
        )
        # The caption column is emitted ONLY for a covered dimension, so the
        # two lists keep partitioning the query's semantic references. An
        # uncovered dimension contributes nothing here and gets its display
        # column back from the dependency closure instead, which walks
        # ``display_column_id`` too — either way it is credited exactly once.
        if has_column:
            add(
                getattr(dimension, "display_column_id", None),
                object_type,
                getattr(dimension, "id", None),
                getattr(dimension, "name", ""),
                f"{role}_display",
            )
        note_object(
            object_type,
            getattr(dimension, "id", None),
            getattr(dimension, "name", ""),
            role,
            has_column,
        )

    for dimension in bound_query.resolved_dimensions:
        add_dimension(dimension, "select")
    for measure in bound_query.resolved_measures:
        # "Covered" means the measure's OWN VALUE resolved to a physical column.
        # Only ``source_column_id`` says that. Everything below is an AUXILIARY
        # dependency — a column the measure additionally reads, not the column it
        # IS — and an auxiliary column must never make the measure count as
        # bound. If it did, a UDA-backed or calculated semi-additive measure
        # would be marked covered by its account column alone, emit no
        # ``semantic_object_ref``, and its real inputs (the UDA's columns, the
        # calc expression's measures) would never be expanded — zero usage for
        # columns the measure cannot run without, which is Bug-8483 exactly.
        has_column = add(
            getattr(measure, "source_column_id", None),
            "measure",
            getattr(measure, "id", None),
            getattr(measure, "name", ""),
            "measure",
        )
        # Auxiliary columns are emitted ONLY for a covered measure, so the two
        # lists keep partitioning the query's semantic references. An uncovered
        # measure contributes nothing here and gets every one of these columns
        # from its dependency closure instead, which already walks
        # ``semi_additive_account_column_id``, ``resolved_date_col_id`` and
        # ``date_dimension_column_id``. Either way each column is recorded
        # exactly once, so Bug-8696 stays fixed for both shapes.
        #
        # ``measure_date`` is ONE role for two pointers, not one role each. They
        # are two schema fields naming the SAME dependency — "the calendar column
        # this measure reads for its period boundaries" — and in a real model
        # they routinely resolve to the same physical column (verified live:
        # modely's base_amount_ytd has resolved_date_col_id ==
        # date_dimension_column_id == business_date). Under two roles that one
        # dependency produced two references and the panel showed hit_count=2 for
        # a single query, while a model whose two fields differ showed 1 each.
        # ``seen`` dedupes on (column_id, type, semantic_id, role), so a single
        # role collapses the coincident case and still records both columns when
        # they genuinely differ.
        if has_column:
            for attr, role in (
                ("semi_additive_account_column_id", "measure_account"),
                ("resolved_date_col_id", "measure_date"),
                ("date_dimension_column_id", "measure_date"),
            ):
                add(
                    getattr(measure, attr, None),
                    "measure",
                    getattr(measure, "id", None),
                    getattr(measure, "name", ""),
                    role,
                )
        note_object(
            "measure",
            getattr(measure, "id", None),
            getattr(measure, "name", ""),
            "measure",
            has_column,
        )

    dimensions_by_name: dict[str, object] = {}
    for key, dimension in (
        getattr(bound_query, "resolved_dimensions_by_name", {}) or {}
    ).items():
        dimensions_by_name[str(key).casefold()] = dimension
        name = getattr(dimension, "name", None)
        if name:
            dimensions_by_name[str(name).casefold()] = dimension
    for dimension in bound_query.resolved_dimensions:
        name = getattr(dimension, "name", None)
        if name:
            dimensions_by_name.setdefault(str(name).casefold(), dimension)

    filtered_names: set[str] = set()
    for query_filter in bound_query.resolved_filters:
        filtered_names.add(str(query_filter.dimension_name).casefold())
        add_dimension(
            dimensions_by_name.get(str(query_filter.dimension_name).casefold()),
            "filter",
        )
    # Bug-5488's ``where_referenced_dimensions`` is the WHOLE WHERE subtree, so a
    # dimension that already produced a resolvable LogicalFilter appears in both
    # collections whenever ANY OTHER conjunct was unrepresentable. Recording both
    # made one predicate two references — and the count then depended on whether
    # an unrelated conjunct happened to be parseable, which is the same
    # "identical reference, different number" harm as the calendar pointers.
    # Only genuinely unfiltered WHERE dimensions get the ``where`` role.
    for name in getattr(bound_query, "where_referenced_dimensions", set()) or set():
        if str(name).casefold() in filtered_names:
            continue
        add_dimension(dimensions_by_name.get(str(name).casefold()), "where")

    for expression in getattr(bound_query, "bound_derived_expressions", []) or []:
        for column in getattr(expression, "inputs", []) or []:
            add(
                getattr(column, "column_id", None),
                "derived_expression",
                getattr(expression, "expression_fingerprint", ""),
                getattr(column, "logical_name", None)
                or getattr(column, "physical_column", ""),
                "derived",
            )

    semantic_objects = [
        {
            "object_type": object_type,
            "object_id": object_id,
            "object_name": name,
            "role": role,
        }
        for (object_type, object_id, role), name in observed.items()
        if (object_type, object_id, role) not in covered
    ]
    return refs, semantic_objects


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
    cache_status: Optional[str] = None,
) -> QueryLog:
    """Write a QueryLog row and pipeline trace RouteLog rows. Return the QueryLog.

    Bug-6426: ``cache_status`` distinguishes an in-TTL result-cache re-serve
    (``"cache_hit"``) from a real route execution (``"live"``/None). The row
    keeps its original ``route_type`` for volume/top-user analytics, but a cache
    re-serve carries ``execution_ms=0``/``bytes_processed=0`` that are NOT real
    measurements — the acceleration-rate and cost-savings rollups exclude
    ``cache_hit`` rows so a cache re-serve is never counted as an acceleration
    hit nor averaged into savings.
    """
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
        cache_status=cache_status,
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
    column_usage_refs, semantic_object_refs = _usage_refs(bound_query)
    db.add(RouteLog(
        query_log_id=log.id,
        route_stage="bind",
        detail={
            "resolved_dimensions": [d.name for d in bound_query.resolved_dimensions],
            "resolved_measures": [m.name for m in bound_query.resolved_measures],
            "has_passthrough": getattr(bound_query, "has_passthrough_expressions", False),
            "filter_count": len(bound_query.resolved_filters),
            "hierarchy_level_dims": hierarchy_level_dims,
            "column_usage_refs": column_usage_refs,
            # Bug-8483: semantic objects with no direct physical column, for the
            # consumer's dependency-closure expansion. Always present (possibly
            # empty) once this producer version is deployed, so the consumer can
            # tell "no unexpanded objects" from "legacy log".
            "semantic_object_refs": semantic_object_refs,
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

    # Bug-6426: a cache re-serve of a pocket-routed result did NOT execute the
    # pocket, so it must not re-increment the pocket's hit_count or credit more
    # time_saved — that would inflate pocket ROI/eviction telemetry the same way
    # it inflated acceleration metrics. Only a real pocket execution counts.
    if (
        decision.route_type == "pocket"
        and decision.pocket_id
        and cache_status != "cache_hit"
    ):
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
    raw_query_override: Optional[str] = None,
    protocol_override: Optional[str] = None,
) -> QueryLog:
    """Persist a failed query as a QueryLog row with status='error'.

    Bug-7674: pre-execution failures (parse/bind/persona-gate/route rejection)
    have no ``bound_query`` yet, so ``raw_query_override`` / ``protocol_override``
    let the caller supply the request's raw query and protocol for the row —
    without them a pre-execution failure would log an empty ``raw_query`` and a
    placeholder protocol, defeating support triage from the log viewer.
    """
    log = QueryLog(
        model_id=bound_query.model.id if bound_query and bound_query.model else None,
        user_identity=user_identity,
        protocol=(
            bound_query.logical_query.protocol if bound_query
            else (protocol_override or "unknown")
        ),
        raw_query=(
            bound_query.logical_query.raw_query if bound_query
            else (raw_query_override or "")
        ),
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
    required_grain: list[str] | None = None,
) -> None:
    """
    Upsert a QueryMissLog entry keyed on
    ``(model_id, query_fingerprint, persona_id)``.

    Bug-6726: uses PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE`` so
    concurrent queries with the same key do not raise an IntegrityError
    on the unique constraint ``uq_query_miss_logs_model_fingerprint_persona``.
    The previous SELECT-then-INSERT pattern raced under concurrent identical
    queries and the resulting 500 propagated into the user query path.

    Misses under different personas are tracked as separate rows so
    the optimizer can partition its workload scan per persona.
    Global (persona_id = NULL) misses still dedupe across repeat
    occurrences because the unique constraint declares ``NULLS NOT
    DISTINCT`` (migration 0041).
    """
    fingerprint = bound_query.logical_query.query_fingerprint
    model_id = bound_query.model.id
    stored_reason = miss_reason[:_MISS_REASON_MAX_LEN]
    now = datetime.now(timezone.utc)

    lq = bound_query.logical_query

    # F-009-20: passthrough / complex-SQL binding keeps RAW (unresolved) filter
    # names on the query object so the SOURCE rewrite can still see them
    # (binder.py's ``has_complex_sql`` branch). Those names may not be model
    # columns, and they must not reach optimizer telemetry — a pocket or
    # predicate variant built from a column the model does not have can never
    # match. Drop unbound filter names from the telemetry predicates ONLY on the
    # passthrough path; a non-passthrough query's filters are all guaranteed
    # bound by the binder (it raises ``SemanticBindingError`` on an unknown
    # filter column otherwise). Hidden dimensions used only in a WHERE are BOUND
    # — ``dim_type_by_name`` (typed for both visible and hidden filter dims) and
    # ``where_referenced_dimensions`` cover them — so they are kept.
    # ``is True`` (not truthiness): the real LogicalQuery flags are bools; the
    # strict check keeps a mock/stub logical query from being treated as a
    # passthrough and silently dropping filters.
    _is_passthrough = (
        getattr(lq, "has_complex_sql", False) is True
        or getattr(lq, "has_unresolvable_where", False) is True
    )
    _bound_filter_names = (
        set(bound_query.resolved_dimensions_by_name or {})
        | {getattr(d, "name", None) for d in bound_query.resolved_dimensions}
        | {getattr(m, "name", None) for m in bound_query.resolved_measures}
        | set(getattr(bound_query, "dim_type_by_name", {}) or {})
        | set(getattr(bound_query, "where_referenced_dimensions", set()) or set())
    )
    telemetry_filters = [
        f
        for f in bound_query.resolved_filters
        if (not _is_passthrough) or f.dimension_name in _bound_filter_names
    ]

    predicates_json = [
        {"column_name": f.dimension_name, "operator": f.operator, "value": f.value}
        for f in telemetry_filters
    ]

    # The aggregate matcher requires both GROUP BY and WHERE filter dimensions
    # in the grain (aggregate_matcher.py: required_grain = requested_grain |
    # filter_dimension_names). Record the same full set here so the telemetry
    # snapshot delivered to the AI optimiser contains a complete grain — omitting
    # filter dimensions would cause the LLM to recommend under-specified aggregates
    # that the matcher would immediately reject.
    filter_dim_names = {
        f.dimension_name
        for f in telemetry_filters
        if f.dimension_name
    }
    all_dimensions = sorted(
        {d.name for d in bound_query.resolved_dimensions} | filter_dim_names
    )
    measures_list = [m.name for m in bound_query.resolved_measures]
    full_grain: list[str]
    if required_grain is not None:
        full_grain = list(required_grain)
    else:
        full_grain = sorted(
            set(lq.grain) | filter_dim_names
        )

    initial_variants = _merge_predicate_variant(None, predicates_json, now)
    initial_reasons = _merge_miss_reason(None, stored_reason, now)

    table = QueryMissLog.__table__

    insert_stmt = pg_insert(table).values(
        id=uuid.uuid4(),
        model_id=model_id,
        query_fingerprint=fingerprint,
        persona_id=persona_id,
        miss_reason=stored_reason,
        normalized_query=lq.raw_query,
        requested_dimensions=all_dimensions,
        requested_measures=measures_list,
        requested_grain=full_grain,
        occurrence_count=1,
        first_seen_at=now,
        last_seen_at=now,
        predicates_json=predicates_json,
        predicate_variants_json=initial_variants,
        miss_reason_counts_json=initial_reasons,
        has_unresolvable_where=lq.has_unresolvable_where,
        has_complex_sql=lq.has_complex_sql,
    )

    upsert_stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_query_miss_logs_model_fingerprint_persona",
        set_={
            "occurrence_count": table.c.occurrence_count + 1,
            "last_seen_at": now,
            "requested_dimensions": all_dimensions,
            "requested_measures": measures_list,
            "requested_grain": full_grain,
            "candidate_aggregate_id": None,
            "predicates_json": predicates_json,
            "has_unresolvable_where": lq.has_unresolvable_where,
            "has_complex_sql": lq.has_complex_sql,
        },
    ).returning(
        table.c.id,
        table.c.occurrence_count,
        table.c.predicate_variants_json,
        table.c.miss_reason_counts_json,
        table.c.miss_reason,
        table.c.first_seen_at,
    )

    result = await db.execute(upsert_stmt)
    row = result.fetchone()

    # F-005-14: merge per-literal-variant hit breakdown.  On insert
    # (occurrence_count == 1) the initial_variants are already correct;
    # on conflict (occurrence_count > 1) we must merge the new variant
    # into the existing list using the Python helper.
    if row is not None and row.occurrence_count > 1:
        merged_variants = _merge_predicate_variant(
            row.predicate_variants_json, predicates_json, now,
        )
        # Bug-8071: merge the per-reason history in the same follow-up UPDATE.
        # It cannot be done in the ON CONFLICT SET clause for the same reason
        # the variants cannot: accumulating into a JSONB list needs the existing
        # value, which the conflict clause does not expose to Python.
        merged_reasons = _merge_miss_reason(
            row.miss_reason_counts_json,
            stored_reason,
            now,
            legacy_reason=row.miss_reason,
            legacy_occurrence_count=row.occurrence_count - 1,
            legacy_first_seen_at=row.first_seen_at,
        )
        await db.execute(
            update(table)
            .where(table.c.id == row.id)
            .values(
                predicate_variants_json=merged_variants,
                miss_reason_counts_json=merged_reasons,
                # Kept out of ON CONFLICT so RETURNING can expose the prior
                # reason used to initialize pre-histogram rows above.
                miss_reason=stored_reason,
            )
        )

    await db.commit()
