"""Hierarchy health check: validate level chains, column refs, calendar bindings."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    CalendarTable,
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
from shared.semantic.calendar_dialects import (
    CALENDAR_COLUMN_SETS,
    TABLE_BOUND_CALENDAR_TYPES,
)
from shared.semantic.calendar_types import normalize_calendar_type
from shared.semantic.graph_order import FACT_TABLE_TYPE
from src.auth.middleware import CurrentUser, enforce_model_scope, forbid_embed_user
from src.auth.rbac import caller_has_role, require_role

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
        .where(ModelTable.model_id == hier.model_id, ModelTable.table_type == FACT_TABLE_TYPE)
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
            select(ModelTable.id, ModelTable.calendar_table_id).where(
                ModelTable.model_id == hier.model_id,
                ModelTable.calendar_table_id.is_not(None),
            )
        )
        cal_alias_rows = cal_alias_result.all()
        # (alias_model_table_id, calendar_table_id) for reachable bound aliases.
        reachable_cal_aliases = [
            (mt_id, cal_tbl_id)
            for (mt_id, cal_tbl_id) in cal_alias_rows
            if mt_id in reachable
        ]
        has_reachable_calendar = bool(reachable_cal_aliases)
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
        else:
            # Bug-7198: a reachable calendar alias ROW existing is not enough —
            # the row-materialised period math (retail_445 / hijri, etc.) reads
            # the CalendarTable's CONFIGURED column mapping (date_column /
            # year_column / ... — see calendar_support / VariantBinding), which
            # calendar registration lets the modeler point at arbitrary physical
            # names. Verifying the CANONICAL CALENDAR_COLUMN_SETS names would
            # false-alarm on a healthy calendar registered under non-canonical
            # names AND miss a mapping that points at a dropped column. So per
            # reachable alias that matches this hierarchy's calendar type, load
            # its CalendarTable, take its configured period columns (falling back
            # to the canonical set only when the mapping is unset), and confirm
            # each exists as a physical ModelColumn ON THAT alias. Pooling across
            # aliases (a same-named column on a different calendar) would mask a
            # real gap, so each alias is checked independently.
            #
            # COVERAGE, not just existence: the runtime reads the CONFIGURED
            # mapping field for each period key the type needs (calendar_support
            # / time_variants_sql), with NO canonical fallback — an UNMAPPED
            # required key (e.g. retail_445 with year_column NULL) makes every
            # period-aware query fail loud at runtime. So for each key the type
            # requires, resolve its configured value and flag it when it is
            # unmapped (a mapping gap) OR mapped-but-physically-absent. The
            # CALENDAR_COLUMN_SETS keys are exactly the CalendarTable field
            # names (date_column / year_column / ...), so the required key set
            # maps 1:1 onto the configured fields.
            required_field_keys = sorted(
                (CALENDAR_COLUMN_SETS.get(cal_type) or {}).keys()
            )
            for mt_id, cal_tbl_id in reachable_cal_aliases:
                cal_row = (
                    await db.get(CalendarTable, cal_tbl_id)
                    if cal_tbl_id is not None
                    else None
                )
                # Only assess an alias whose calendar type matches the
                # hierarchy's expected type — a same-model alias of a different
                # type is not this hierarchy's binding (its own type mismatch is
                # reported by the Bug-5680 check below).
                if cal_row is not None:
                    alias_cal_type = normalize_calendar_type(cal_row.calendar_type)
                    if alias_cal_type is not None and alias_cal_type != cal_type:
                        continue

                present_cols_result = await db.execute(
                    select(ModelColumn.column_name).where(
                        ModelColumn.model_table_id == mt_id,
                    )
                )
                present = {
                    str(c).lower()
                    for (c,) in present_cols_result.all()
                    if c is not None
                }

                # Per-required-key resolution.
                unmapped_keys: list[str] = []
                missing: list[str] = []
                required_columns: list[str] = []
                for field_key in required_field_keys:
                    canonical_name = (
                        CALENDAR_COLUMN_SETS.get(cal_type) or {}
                    ).get(field_key)
                    configured_name = (
                        getattr(cal_row, field_key, None) if cal_row is not None
                        else None
                    )
                    # Fall back to the canonical name ONLY when the backlink is
                    # unbound (no CalendarTable row); a bound-but-unmapped field
                    # is a real gap the runtime would hit.
                    effective = configured_name or (
                        canonical_name if cal_row is None else None
                    )
                    if not effective:
                        unmapped_keys.append(field_key)
                        continue
                    required_columns.append(effective)
                    if effective.lower() not in present:
                        missing.append(effective)

                if missing or unmapped_keys:
                    issues.append({
                        "issue_type": "calendar_source_columns_missing",
                        "severity": "error",
                        "detail": {
                            "calendar_type": cal_type,
                            "calendar_alias_table_id": str(mt_id),
                            "missing_columns": missing,
                            "unmapped_period_keys": unmapped_keys,
                            "required_columns": sorted(set(required_columns)),
                            "reason": (
                                "The bound calendar table is reachable but its "
                                "period column mapping is incomplete or points "
                                "at column(s) missing from the physical table; "
                                "time-variant measures using this calendar would "
                                "fail at query time or silently fall back to "
                                "Gregorian (wrong) period boundaries."
                            ),
                        },
                    })

    # Bug-5680: validate that a reachable calendar table's type matches the
    # hierarchy's expected calendar type. A type mismatch (e.g. hierarchy
    # expects retail_445 but the bound calendar is standard) silently produces
    # wrong period boundaries.
    if is_time_hier and cal_type is not None:
        cal_alias_result2 = await db.execute(
            select(ModelTable.id, ModelTable.calendar_table_id).where(
                ModelTable.model_id == hier.model_id,
                ModelTable.calendar_table_id.is_not(None),
            )
        )
        for mt_id, cal_table_id in cal_alias_result2.all():
            if mt_id not in reachable:
                continue
            cal_row = await db.get(CalendarTable, cal_table_id)
            if cal_row is None:
                continue
            actual_type = normalize_calendar_type(cal_row.calendar_type)
            if actual_type is not None and actual_type != cal_type:
                issues.append({
                    "issue_type": "calendar_type_mismatch",
                    "severity": "warning",
                    "detail": {
                        "expected_type": cal_type,
                        "actual_type": actual_type,
                        "calendar_table_id": str(cal_table_id),
                        "reason": (
                            f"Hierarchy expects calendar type '{cal_type}' but "
                            f"the bound calendar table is '{actual_type}'. "
                            "Period boundaries may be incorrect."
                        ),
                    },
                })

    return issues


async def _get_model_hierarchy_health(
    db: AsyncSession,
    model_id: UUID,
    *,
    project_id: UUID | None = None,
    probe_members: bool = False,
    tenant_slug: str | None = None,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(HierarchyDefinition).where(HierarchyDefinition.model_id == model_id)
    )
    hierarchies = list(result.scalars().all())

    output = []
    for hier in hierarchies:
        issues = await _check_hierarchy_health(db, hier)
        # Bug-8510: fail closed. Member integrity counts as checked only when
        # the probe actually ran AND covered every adjacent level pair; every
        # other path (not requested, no project context, partial coverage)
        # leaves this false.
        members_probed = False
        # F-016-02: opt-in member-integrity probes read live source data
        # through the audited source-execution boundary to detect broken-drill
        # conditions (orphans, many-to-many parentage) that the metadata check
        # cannot see. Only run when explicitly requested and the owning project
        # is known (needed for fail-closed connection resolution).
        if probe_members and project_id is not None:
            from src.api.hierarchy_member_integrity import (
                probe_hierarchy_member_integrity,
            )
            probe = await probe_hierarchy_member_integrity(
                db, hier, project_id=project_id, tenant_slug=tenant_slug,
            )
            issues = issues + probe.issues
            members_probed = probe.fully_scanned
        # F-016-08: a clean metadata check with NO member probe must NOT report
        # "ok" — that reads as "healthy" while orphan members (broken drill /
        # double-count) remain undetected. Report the honest "unverified_members"
        # so the UI shows a neutral "members not checked" state, not a green tick,
        # until a modeler runs the probe. Real metadata findings (error/warning)
        # still take precedence and are reported without a probe.
        if any(i["severity"] == "error" for i in issues):
            status = "error"
        elif any(i["severity"] == "warning" for i in issues):
            status = "warning"
        elif members_probed:
            status = "ok"
        else:
            status = "unverified_members"
        output.append({
            "hierarchy_id": str(hier.id),
            "hierarchy_name": hier.name,
            "status": status,
            "members_probed": members_probed,
            "issues": issues,
        })
    return output


class HierarchyHealthResponse(BaseModel):
    """Health verdict for ONE hierarchy.

    Bug-8510 — ``status`` alone is not the whole truth. The endpoint answers two
    structurally different questions depending on ``probe_members``: with the
    probe off it runs metadata checks ONLY and says nothing whatsoever about
    member integrity, so a hierarchy whose source data is full of orphan members
    still reports ``status="ok"`` with an empty ``issues`` list. ``status="ok"``
    therefore means "nothing found by the checks that RAN", and only
    ``status == "ok" and members_probed`` means "healthy".

    Consumers must not reconstruct this from the request they sent, from the
    caller's role, or by scanning ``issues`` for ``member_integrity_unprobed``:
    the probe can run and still skip individual level pairs, and a 403 on a
    requested probe is reported by the HTTP status, not the body.
    """

    hierarchy_id: str
    hierarchy_name: str
    status: str
    members_probed: bool = Field(
        description=(
            "True only when the live member-integrity probe ran for this "
            "hierarchy AND scanned every adjacent level pair. False when the "
            "probe was not requested (probe_members=false), or ran but could "
            "not scan one or more pairs (a level not backed by a physical "
            "column, a cross-table parent/child pair, an unresolvable source "
            "connection, or a probe failure). When false, orphan members and "
            "members with multiple parents have NOT been ruled out, whatever "
            "`status` says."
        ),
    )
    issues: list[dict]


@router.get(
    "",
    response_model=list[HierarchyHealthResponse],
    dependencies=[require_role("viewer")],
)
async def get_hierarchy_health(
    project_id: UUID,
    model_id: UUID,
    probe_members: bool = Query(
        default=False,
        description=(
            "When true, run bounded member-integrity probes against the live "
            "source (orphan children, many-to-many parentage). Reads source "
            "data through the audited execution boundary; slower than the "
            "default metadata-only check."
        ),
    ),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[HierarchyHealthResponse]:
    """Report per-hierarchy health for a model.

    Bug-8510 — read ``members_probed`` on every entry, not just ``status``. A
    hierarchy is only genuinely healthy when ``status == "ok"`` AND
    ``members_probed`` is true; otherwise the member-integrity checks did not
    run (or did not cover every level pair) and orphan / multi-parent members
    have not been ruled out. A caller whose ``probe_members=true`` request is
    rejected with 403 may retry without it, but the retried response will
    truthfully report ``members_probed: false``.
    """
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # F-016-02 (Fable gate Bug-8292) [SECURITY]: the metadata health check is
        # viewer-safe, but ``probe_members`` reads RAW source member keys and
        # returns sampled offending values — data a viewer's persona/RLS would
        # normally restrict. The probe does not (yet) thread persona/RLS
        # predicates, so gate the live-data probe at MODELER, who is trusted with
        # the full model and its source. A viewer requesting probe_members is
        # denied the probe (403) rather than leaking out-of-scope member keys.
        if probe_members and not await caller_has_role(
            db, current_user, project_id, "modeler", model_id=model_id
        ):
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail=(
                    "Member-integrity probes read raw source data and require "
                    "the modeler role. Retry without probe_members for the "
                    "metadata-only health check."
                ),
            )
        health = await _get_model_hierarchy_health(
            db, model_id,
            project_id=project_id,
            probe_members=probe_members,
            tenant_slug=current_user.tenant_id,
        )
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

    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
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
