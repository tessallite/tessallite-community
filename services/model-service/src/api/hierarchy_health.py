"""Hierarchy health check: validate level chains, column refs, calendar bindings."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    HierarchyDefinition,
    HierarchyLevel,
    Join,
    Model,
    ModelColumn,
    ModelTable,
    QueryMissLog,
    UserDefinedAttribute,
)
from shared.db.session import get_tenant_db
from shared.semantic.calendar_dialects import TABLE_BOUND_CALENDAR_TYPES
from shared.semantic.calendar_types import normalize_calendar_type
from src.auth.middleware import CurrentUser, enforce_model_scope, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/hierarchy-health",
    tags=["hierarchy-health"],
)


async def _ensure_model_in_project(
    db: AsyncSession, *, project_id: UUID, model_id: UUID
) -> None:
    """F-016-12: reject a model_id that does not belong to the path project.

    The hierarchy-health endpoints addressed models by id without checking
    that the model belonged to the path ``project_id`` — every sibling
    hierarchies.py endpoint applies this chain, so apply it here too.
    """
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Model not found")


async def _check_hierarchy_health(
    db: AsyncSession, hier: Any
) -> list[dict[str, Any]]:
    """Return list of issue dicts for the hierarchy. Empty = healthy."""
    issues: list[dict[str, Any]] = []

    levels_result = await db.execute(
        select(HierarchyLevel)
        .where(HierarchyLevel.hierarchy_id == hier.id)
        .order_by(HierarchyLevel.ordinal)
    )
    levels = list(levels_result.scalars().all())

    if len(levels) == 0 and hier.type == "explicit":
        issues.append({"issue_type": "empty_levels", "severity": "error", "detail": {}})

    ordinals = [l.ordinal for l in levels]
    if len(ordinals) != len(set(ordinals)):
        issues.append({
            "issue_type": "duplicate_level_ordinals",
            "severity": "error",
            "detail": {"ordinals": ordinals},
        })

    if hier.type == "date_embedded":
        dc = getattr(hier, "date_config", None)
        if dc and isinstance(dc, dict) and not dc.get("source_attribute_id"):
            issues.append({
                "issue_type": "missing_date_config",
                "severity": "error",
                "detail": {},
            })

    for lvl in levels:
        attr_id = getattr(lvl, "key_attribute_id", None)
        source = getattr(lvl, "key_attribute_source", None)
        if not attr_id:
            issues.append({
                "issue_type": "missing_key_attribute",
                "severity": "error",
                "detail": {"level_name": lvl.name, "ordinal": lvl.ordinal},
            })
            continue
        if source == "physical_column":
            col = await db.get(ModelColumn, attr_id)
            if col is None:
                issues.append({
                    "issue_type": "dangling_key_attribute",
                    "severity": "error",
                    "detail": {
                        "level_name": lvl.name,
                        "ordinal": lvl.ordinal,
                        "key_attribute_id": str(attr_id),
                        "key_attribute_source": source,
                    },
                })
        elif source == "user_defined_attribute":
            uda = await db.get(UserDefinedAttribute, attr_id)
            if uda is None:
                issues.append({
                    "issue_type": "dangling_key_attribute",
                    "severity": "error",
                    "detail": {
                        "level_name": lvl.name,
                        "ordinal": lvl.ordinal,
                        "key_attribute_id": str(attr_id),
                        "key_attribute_source": source,
                    },
                })
        elif source:
            issues.append({
                "issue_type": "unknown_key_attribute_source",
                "severity": "warning",
                "detail": {
                    "level_name": lvl.name,
                    "key_attribute_source": source,
                },
            })

    join_result = await db.execute(
        select(Join.left_table_id, Join.right_table_id)
        .where(Join.model_id == hier.model_id)
    )
    join_graph: dict[UUID, set[UUID]] = defaultdict(set)
    for left_id, right_id in join_result.all():
        join_graph[left_id].add(right_id)
        join_graph[right_id].add(left_id)

    fact_result = await db.execute(
        select(ModelTable.id)
        .where(ModelTable.model_id == hier.model_id, ModelTable.table_type == "fact")
        .limit(1)
    )
    fact_table_id = fact_result.scalar_one_or_none()

    reachable: set[UUID] = set()
    if fact_table_id is not None:
        queue: deque[UUID] = deque([fact_table_id])
        reachable.add(fact_table_id)
        while queue:
            node = queue.popleft()
            for neighbour in join_graph.get(node, set()):
                if neighbour not in reachable:
                    reachable.add(neighbour)
                    queue.append(neighbour)

    for lvl in levels:
        attr_id = getattr(lvl, "key_attribute_id", None)
        source = getattr(lvl, "key_attribute_source", None)
        table_id = None
        if source == "physical_column" and attr_id:
            col = await db.get(ModelColumn, attr_id)
            if col:
                table_id = col.model_table_id
        elif source == "user_defined_attribute" and attr_id:
            uda = await db.get(UserDefinedAttribute, attr_id)
            if uda:
                table_id = uda.table_id
        if table_id and table_id not in reachable:
            issues.append({
                "issue_type": "unreachable_level_table",
                "severity": "warning",
                "detail": {
                    "level_name": lvl.name,
                    "ordinal": lvl.ordinal,
                    "table_id": str(table_id),
                },
            })

    # F-016-15: calendar-binding check. A time hierarchy whose calendar_type
    # needs a materialised calendar table (retail_445 / hijri) silently
    # computes Gregorian periods at query time when no calendar table is
    # reachable (F-016-09 makes the query path fail loud, but the health panel
    # should surface the gap before a query is ever run). Expression-capable
    # types (standard / fiscal / iso_week / thai_buddhist) need no table.
    cal_type = normalize_calendar_type(getattr(hier, "calendar_type", None))
    is_time_hier = (
        getattr(hier, "dimension_kind", None) == "time"
        or hier.type == "date_embedded"
    )
    if is_time_hier and cal_type in TABLE_BOUND_CALENDAR_TYPES:
        cal_alias_result = await db.execute(
            select(ModelTable.id).where(
                ModelTable.model_id == hier.model_id,
                ModelTable.calendar_table_id.is_not(None),
            )
        )
        calendar_table_ids = {r[0] for r in cal_alias_result.all()}
        has_reachable_calendar = bool(calendar_table_ids & reachable)
        if not has_reachable_calendar:
            issues.append({
                "issue_type": "calendar_table_not_bound",
                "severity": "warning",
                "detail": {
                    "calendar_type": cal_type,
                    "reason": (
                        "This calendar type requires a calendar table joined to "
                        "the fact table; none is reachable, so time-variant "
                        "measures cannot compute its period boundaries."
                    ),
                },
            })

    return issues


async def _get_model_hierarchy_health(
    db: AsyncSession, model_id: UUID
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(HierarchyDefinition).where(HierarchyDefinition.model_id == model_id)
    )
    hierarchies = list(result.scalars().all())

    output = []
    for hier in hierarchies:
        issues = await _check_hierarchy_health(db, hier)
        status = "ok"
        if any(i["severity"] == "error" for i in issues):
            status = "error"
        elif any(i["severity"] == "warning" for i in issues):
            status = "warning"
        output.append({
            "hierarchy_id": str(hier.id),
            "hierarchy_name": hier.name,
            "status": status,
            "issues": issues,
        })
    return output


class HierarchyHealthResponse(BaseModel):
    hierarchy_id: str
    hierarchy_name: str
    status: str
    issues: list[dict]


@router.get(
    "",
    response_model=list[HierarchyHealthResponse],
    dependencies=[require_role("viewer")],
)
async def get_hierarchy_health(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[HierarchyHealthResponse]:
    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        health = await _get_model_hierarchy_health(db, model_id)
        return [HierarchyHealthResponse(**h) for h in health]
    return []


class GrainSuggestion(BaseModel):
    label: str
    grain: list[str]
    source: str
    hierarchy_id: str


@router.get(
    "/grain-suggestions",
    response_model=list[GrainSuggestion],
    dependencies=[require_role("viewer")],
)
async def get_hierarchy_grain_suggestions(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[GrainSuggestion]:
    """Suggest aggregate grains at hierarchy rollup levels above recent miss grains."""
    from datetime import datetime, timedelta, timezone

    enforce_model_scope(current_user, str(model_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        miss_result = await db.execute(
            select(QueryMissLog)
            .where(
                QueryMissLog.model_id == model_id,
                QueryMissLog.last_seen_at >= cutoff,
            )
            .limit(100)
        )
        misses = list(miss_result.scalars().all())

        hier_result = await db.execute(
            select(HierarchyDefinition).where(HierarchyDefinition.model_id == model_id)
        )
        hierarchies = list(hier_result.scalars().all())

        for h in hierarchies:
            level_result = await db.execute(
                select(HierarchyLevel)
                .where(HierarchyLevel.hierarchy_id == h.id)
                .order_by(HierarchyLevel.ordinal)
            )
            h._loaded_levels = list(level_result.scalars().all())

        suggestions: list[GrainSuggestion] = []
        seen: set[str] = set()

        for miss in misses:
            miss_grain = getattr(miss, "grain", None) or []
            if not miss_grain:
                continue
            grain_set = {g.lower() for g in miss_grain}
            for h in hierarchies:
                levels = getattr(h, "_loaded_levels", [])
                matched_ordinal = 0
                for lvl in levels:
                    slug = lvl.name.lower().replace(" ", "_")
                    if slug in grain_set:
                        matched_ordinal = max(matched_ordinal, lvl.ordinal)
                if matched_ordinal == 0:
                    continue
                for lvl in levels:
                    if lvl.ordinal < matched_ordinal:
                        key = f"{h.id}:{lvl.ordinal}"
                        if key not in seen:
                            seen.add(key)
                            suggestions.append(GrainSuggestion(
                                label=f"{lvl.name} (hierarchy rollup)",
                                grain=[lvl.name.lower().replace(" ", "_")],
                                source="hierarchy_advisor",
                                hierarchy_id=str(h.id),
                            ))
        return suggestions
    return []
