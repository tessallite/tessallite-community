"""
Glossary v1 — modeller-curated business term catalog.

Phase 3 + Phase 4 of the semantic-layer plan (docs/architecture/architecture_semantic-layer.md).

Authenticated endpoints (Phase 3):
  GET    /projects/{p}/models/{m}/glossary                 — list entries
  POST   /projects/{p}/models/{m}/glossary                 — create user entry
  POST   /projects/{p}/models/{m}/glossary/bootstrap       — heuristic LLM-style proposal pass
  POST   /projects/{p}/models/{m}/glossary/{eid}/approve   — approve a proposal
  PATCH  /projects/{p}/models/{m}/glossary/{eid}           — edit (creates a new version)
  POST   /projects/{p}/models/{m}/glossary/{eid}/reject    — soft-reject a proposal
  DELETE /projects/{p}/models/{m}/glossary/{eid}           — hard-delete an entry
  POST   /projects/{p}/models/{m}/glossary/share           — issue a public token

Public endpoints (Phase 4 — no auth, token in path):
  GET    /glossary/public/{token}                          — JSON payload
  GET    /glossary/public/{token}/download.csv             — CSV download
  GET    /glossary/public/{token}/download.xlsx            — XLSX download

The approve/edit actions cascade `proposed_is_hidden` onto the underlying
ModelColumn so the curation workflow doubles as the visibility-flag editor.
The public payload only includes entries with status='approved' so
unreviewed LLM proposals never reach business users.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging

import httpx
import re
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID
from xml.sax.saxutils import escape as _xml_escape

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from jose import JWTError, jwt
from sqlalchemy import delete, select

from shared.config.settings import get_settings
from shared.db.models import (
    Dimension,
    GlossaryAttachment,
    GlossaryBootstrapJob,
    GlossaryEntry,
    GlossaryShareToken,
    GlossarySynonym,
    Join,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    SourceColumnStatistics,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    GlossaryAttachmentResponse,
    GlossaryBootstrapResponse,
    GlossaryBulkApproveResponse,
    GlossaryBulkDeleteRequest,
    GlossaryBulkDeleteResponse,
    GlossaryEntryCreate,
    GlossaryEntryResponse,
    GlossaryEntryUpdate,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project

logger = logging.getLogger(__name__)

_settings = get_settings()
_PUBLIC_TOKEN_PURPOSE = "glossary_public"

# F-018-03: bootstrap job status is persisted to the per-tenant DB
# (``glossary_bootstrap_jobs``) so any replica can answer a poll and a restart
# does not lose the job. We keep a strong reference to the in-flight asyncio
# task only to stop CPython garbage-collecting an un-referenced task mid-run;
# it is not the source of truth and is intentionally not shared across replicas.
_GLOSSARY_BOOTSTRAP_TASKS: set[asyncio.Task] = set()

# Bounds on the durable job table so it cannot grow without limit.
_JOB_TTL = timedelta(hours=24)        # completed/failed rows expire after this
_JOB_RETENTION_PER_MODEL = 20         # keep at most this many recent rows/model

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/glossary",
    tags=["glossary"],
)


def _coerce_user_uuid(user_id: Any) -> Optional[UUID]:
    """Cast the middleware's string user id to a UUID for the created_by
    column, falling back to None for system callers or malformed ids.
    """
    if user_id in (None, "", "__system__"):
        return None
    try:
        return UUID(str(user_id))
    except (TypeError, ValueError):
        return None


def _mint_glossary_service_token(tenant_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "glossary-bootstrap-service",
        "tenant_id": tenant_id,
        "role": "tenant_admin",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=30)).timestamp()),
    }
    return jwt.encode(payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM)


def _job_response(
    job_id: UUID,
    status_value: str,
    message: str | None = None,
) -> GlossaryBootstrapResponse:
    return GlossaryBootstrapResponse(
        proposed_count=0,
        job_id=job_id,
        job_status=status_value,
        message=message,
    )


# ---------------------------------------------------------------------------
# Durable bootstrap-job registry (F-018-03)
# ---------------------------------------------------------------------------

def _serialize_job_result(resp: GlossaryBootstrapResponse | None) -> dict | None:
    """JSON-safe dump of the bootstrap result for the JSONB column."""
    if resp is None:
        return None
    return resp.model_dump(mode="json")


def _deserialize_job_result(data: Any) -> GlossaryBootstrapResponse | None:
    """Rebuild a GlossaryBootstrapResponse from the stored JSONB payload."""
    if not isinstance(data, dict):
        return None
    try:
        return GlossaryBootstrapResponse.model_validate(data)
    except Exception:  # pragma: no cover - defensive against schema drift
        logger.warning("Could not deserialize stored bootstrap job result", exc_info=True)
        return None


async def _sweep_bootstrap_jobs(db: Any, model_id: UUID) -> None:
    """Keep the durable job table bounded: drop rows older than the TTL and
    trim per-model history to the most recent N rows."""
    cutoff = datetime.now(timezone.utc) - _JOB_TTL
    await db.execute(
        delete(GlossaryBootstrapJob).where(GlossaryBootstrapJob.created_at < cutoff)
    )
    keep_ids = (
        await db.execute(
            select(GlossaryBootstrapJob.id)
            .where(GlossaryBootstrapJob.model_id == model_id)
            .order_by(GlossaryBootstrapJob.created_at.desc())
            .limit(_JOB_RETENTION_PER_MODEL)
        )
    ).scalars().all()
    if keep_ids:
        await db.execute(
            delete(GlossaryBootstrapJob)
            .where(GlossaryBootstrapJob.model_id == model_id)
            .where(GlossaryBootstrapJob.id.notin_(keep_ids))
        )


async def _create_bootstrap_job(
    db: Any,
    job_id: UUID,
    project_id: UUID,
    model_id: UUID,
    status_value: str,
    message: str | None,
) -> None:
    await _sweep_bootstrap_jobs(db, model_id)
    db.add(
        GlossaryBootstrapJob(
            id=job_id,
            project_id=project_id,
            model_id=model_id,
            status=status_value,
            message=message,
            result=None,
        )
    )
    await db.commit()


async def _update_bootstrap_job(
    tenant_id: str,
    job_id: UUID,
    *,
    status_value: str,
    message: str | None = None,
    result: GlossaryBootstrapResponse | None = None,
) -> None:
    """Update a job row in its own session (the background task has no request
    session). No-op if the row was swept away."""
    async for db in get_tenant_db(tenant_id):
        job = await db.get(GlossaryBootstrapJob, job_id)
        if job is None:
            return
        job.status = status_value
        if message is not None:
            job.message = message
        if result is not None:
            job.result = _serialize_job_result(result)
        await db.commit()
        return


# ---------------------------------------------------------------------------
# Heuristic bootstrap rules
# ---------------------------------------------------------------------------

def _humanise(name: str) -> str:
    """`total_revenue_usd` → `Total revenue usd`."""
    cleaned = re.sub(r"[_\-\.]+", " ", name).strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else name


# F-018-09: ReportLab Platypus paragraphs parse intra-paragraph XML markup, so
# an unescaped `&` or `<` in a definition raised a parse error and 500'd the
# public PDF. Escaping before constructing Paragraphs also blocks `<b>`-style
# markup injection from glossary content.
def _pdf_escape(text: Any) -> str:
    return _xml_escape(str(text)) if text is not None else ""


# F-018-10: a glossary term/definition that starts with `= + - @` (or a leading
# tab/CR) becomes an executing formula (`=HYPERLINK`, DDE) when the exported
# CSV/XLSX — distributed through a public, no-login link aimed at non-technical
# users — is opened in Excel. Prefix such cells with a single quote (the
# standard OWASP CSV-injection mitigation) so the spreadsheet treats them as
# text. Applied to both CSV and XLSX cell values.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _formula_guard(value: Any) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in _FORMULA_TRIGGERS:
        return "'" + text
    return text


_MAX_STATS_SAMPLE_VALUES = 20


def _attach_stats(item: dict, s: "SourceColumnStatistics") -> None:
    if s.distinct_count is not None and s.distinct_count > 0:
        item["distinct_count"] = s.distinct_count
    if s.null_ratio is not None:
        item["null_ratio"] = s.null_ratio
    if s.min_value is not None:
        item["min_value"] = s.min_value
    if s.max_value is not None:
        item["max_value"] = s.max_value
    if s.top_values:
        vals = set()
        for entry in s.top_values[:_MAX_STATS_SAMPLE_VALUES]:
            v = entry.get("value") if isinstance(entry, dict) else entry
            if v is not None:
                vals.add(str(v))
        if vals:
            item["sample_values"] = sorted(vals)


def _sample_values_from_item_stats(
    item: dict | None,
    max_distinct: int,
) -> list[str] | None:
    """Return bounded sample values already captured in column statistics."""
    if not item:
        return None
    raw_values = item.get("sample_values")
    if not raw_values:
        return None
    values = sorted({str(v) for v in raw_values if v is not None})
    if not values or len(values) > max_distinct:
        return None
    return values


def _heuristic_definition_for_dimension(
    dim: Dimension,
    relationships: list[str] | None = None,
) -> str:
    label = dim.display_name or _humanise(dim.name)
    if dim.is_time_dim:
        base = f"{label} — time dimension used for trend analysis and time-based grouping."
    else:
        base = f"{label} — categorical attribute used to group and filter results."
    if relationships:
        base += " Related tables: " + ", ".join(relationships) + "."
    return base


# F-018-14: additivity depends on the aggregation, so the heuristic definition
# must not assert "additive" for every measure. sum/count are additive across
# any grain; min/max are semi-additive (they aggregate over non-time dimensions
# but a max-of-max is still a max, so they are safe to roll up — described as
# "aggregated, not summed"); avg and count_distinct are non-additive (re-deriving
# them from sub-totals is wrong).
_ADDITIVE_AGGS = {"sum", "count"}
_SEMI_ADDITIVE_AGGS = {"min", "max"}


def _additivity_phrase(agg: str) -> str:
    if agg in _ADDITIVE_AGGS:
        return "additive across the model's grain."
    if agg in _SEMI_ADDITIVE_AGGS:
        return (
            "semi-additive — it can be rolled up across dimensions but is not a "
            "running total."
        )
    if agg == "count_distinct":
        return (
            "non-additive — distinct counts cannot be summed from sub-totals and "
            "must be recomputed at each grain."
        )
    if agg == "avg":
        return (
            "non-additive — averages cannot be summed from sub-totals and must be "
            "recomputed at each grain."
        )
    return "aggregated at the model's grain."


def _heuristic_definition_for_measure(
    meas: Measure,
    relationships: list[str] | None = None,
) -> str:
    label = meas.display_name or _humanise(meas.name)
    fn = (meas.default_agg or "sum").lower()
    base = f"{label} — {fn} of the underlying value, {_additivity_phrase(fn)}"
    if relationships:
        base += " Related tables: " + ", ".join(relationships) + "."
    return base


def _heuristic_synonyms(name: str) -> list[str]:
    """Drop the obvious snake_case → camelCase / Title Case variants."""
    out: list[str] = []
    if "_" in name:
        out.append(name.replace("_", " "))
        out.append(_humanise(name))
    return out


async def _refresh_source_statistics_background(
    source_ids: list[UUID],
    bearer: str,
    low_cardinality_threshold: int,
) -> None:
    """Trigger optimizer source-statistics refreshes before glossary generation."""
    if not source_ids or not bearer:
        return
    async with httpx.AsyncClient(timeout=600.0) as client:
        for source_id in source_ids:
            try:
                resp = await client.post(
                    f"{_settings.OPTIMIZER_URL}/api/v1/sources/{source_id}/statistics/refresh",
                    params={"low_cardinality_threshold": low_cardinality_threshold},
                    headers={"Authorization": f"Bearer {bearer}"},
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "Glossary bootstrap source-statistics refresh failed for source %s: %s %s",
                        source_id, resp.status_code, resp.text[:300],
                    )
            except Exception:
                logger.warning(
                    "Glossary bootstrap source-statistics refresh errored for source %s",
                    source_id, exc_info=True,
                )


async def _run_glossary_bootstrap_job(
    job_id: UUID,
    tenant_id: str,
    user_id: str,
    email: str,
    project_id: UUID,
    model_id: UUID,
    source_ids: list[UUID],
    max_distinct: int,
) -> None:
    service_token = _mint_glossary_service_token(tenant_id)
    try:
        await _update_bootstrap_job(
            tenant_id, job_id,
            status_value="refreshing_statistics",
            message="Refreshing source statistics before glossary generation.",
        )
        await _refresh_source_statistics_background(
            source_ids=source_ids,
            bearer=service_token,
            low_cardinality_threshold=max_distinct,
        )

        await _update_bootstrap_job(
            tenant_id, job_id,
            status_value="generating_glossary",
            message="Generating glossary definitions from refreshed statistics.",
        )
        job_user = CurrentUser(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            role="tenant_admin",
            raw_token=service_token,
        )
        setattr(job_user, "_glossary_run_now", True)
        result = await bootstrap(project_id, model_id, current_user=job_user)
        result.job_id = job_id
        result.job_status = "completed"
        result.message = "Glossary bootstrap completed."
        await _update_bootstrap_job(
            tenant_id, job_id,
            status_value="completed",
            message=result.message,
            result=result,
        )
    except Exception as exc:
        logger.exception("Glossary bootstrap job failed for model %s", model_id)
        await _update_bootstrap_job(
            tenant_id, job_id,
            status_value="failed",
            message=str(exc),
            result=GlossaryBootstrapResponse(
                proposed_count=0,
                job_id=job_id,
                job_status="failed",
                message=str(exc),
                llm_error=str(exc),
            ),
        )


async def _cascade_hidden_to_columns(
    db: Any, attachments: list[GlossaryAttachment], hidden: bool
) -> int:
    """Flip ``ModelColumn.is_hidden`` for every column an entry attaches to.

    The "Hide column" checkbox in the glossary curation panel persists its
    intent on ``GlossaryEntry.proposed_is_hidden``. The canonical visibility
    flag, however, lives on ``ModelColumn.is_hidden`` — the dimension/measure
    response builders and the gateway catalog both derive visibility from the
    source column (``dimensions.py``/``measures.py`` ``is_hidden = col.is_hidden``).

    Bootstrap only ever produces ``dimension`` and ``measure`` attachments
    (never ``column``), so the original cascade — which matched
    ``target_type == "column"`` only — was dead code. This helper resolves the
    underlying ``source_column_id`` for dimension and measure attachments (and
    still honours a direct ``column`` attachment from the manual-create path),
    then flips the column so hiding propagates everywhere the column surfaces.

    Returns the number of columns whose visibility was changed.
    """
    column_ids: set[UUID] = set()
    for att in attachments:
        if att.target_id is None:
            continue
        if att.target_type == "column":
            column_ids.add(att.target_id)
        elif att.target_type == "dimension":
            dim = await db.get(Dimension, att.target_id)
            if dim is not None and dim.source_column_id is not None:
                column_ids.add(dim.source_column_id)
        elif att.target_type == "measure":
            meas = await db.get(Measure, att.target_id)
            if meas is not None and meas.source_column_id is not None:
                column_ids.add(meas.source_column_id)

    changed = 0
    for col_id in column_ids:
        col = await db.get(ModelColumn, col_id)
        if col is not None and bool(col.is_hidden) != hidden:
            col.is_hidden = hidden
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------

def _entry_to_response(
    entry: GlossaryEntry,
    target_names: dict[UUID, str] | None = None,
) -> GlossaryEntryResponse:
    names = target_names or {}
    return GlossaryEntryResponse(
        id=entry.id,
        model_id=entry.model_id,
        term=entry.term,
        definition=entry.definition,
        context_notes=entry.context_notes,
        source=entry.source,
        status=entry.status,
        version=entry.version,
        superseded_by=entry.superseded_by,
        created_by=entry.created_by,
        proposed_is_hidden=entry.proposed_is_hidden,
        visibility=entry.visibility,
        confidence=entry.confidence,
        sample_values=entry.sample_values,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        synonyms=[s.synonym for s in (entry.synonyms or [])],
        attachments=[
            GlossaryAttachmentResponse(
                id=a.id,
                entry_id=a.entry_id,
                target_type=a.target_type,
                target_id=a.target_id,
                target_name=names.get(a.target_id) if a.target_id else None,
            )
            for a in (entry.attachments or [])
        ],
    )


async def _resolve_attachment_target_names(
    db, entries: list[GlossaryEntry]
) -> dict[UUID, str]:
    """Map each attached dimension/measure target_id to its display name so the
    curation panel can show names instead of truncated UUIDs (F-018-21)."""
    dim_ids: set[UUID] = set()
    meas_ids: set[UUID] = set()
    for e in entries:
        for a in (e.attachments or []):
            if a.target_id is None:
                continue
            if a.target_type == "dimension":
                dim_ids.add(a.target_id)
            elif a.target_type == "measure":
                meas_ids.add(a.target_id)
    names: dict[UUID, str] = {}
    if dim_ids:
        rows = (await db.execute(
            select(Dimension.id, Dimension.display_name, Dimension.name)
            .where(Dimension.id.in_(dim_ids))
        )).all()
        for did, disp, nm in rows:
            names[did] = disp or nm
    if meas_ids:
        rows = (await db.execute(
            select(Measure.id, Measure.display_name, Measure.name)
            .where(Measure.id.in_(meas_ids))
        )).all()
        for mid, disp, nm in rows:
            names[mid] = disp or nm
    return names


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=list[GlossaryEntryResponse],
    dependencies=[require_role("viewer")],
)
async def list_entries(
    project_id: UUID,
    model_id: UUID,
    status_filter: Optional[str] = None,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[GlossaryEntryResponse]:
    # F-018-15: a project-binding check (viewer) is required, consistent with
    # the named-set list endpoint. Without it any authenticated tenant user
    # with no binding could read pending_review/rejected drafts for any model.
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        from sqlalchemy.orm import selectinload

        stmt = (
            select(GlossaryEntry)
            .where(GlossaryEntry.model_id == model_id)
            .where(GlossaryEntry.superseded_by.is_(None))
            .options(
                selectinload(GlossaryEntry.synonyms),
                selectinload(GlossaryEntry.attachments),
            )
            .order_by(GlossaryEntry.term)
        )
        if status_filter:
            stmt = stmt.where(GlossaryEntry.status == status_filter)
        result = await db.execute(stmt)
        entries = result.scalars().all()
        target_names = await _resolve_attachment_target_names(db, entries)
        return [_entry_to_response(e, target_names) for e in entries]


@router.post(
    "",
    response_model=GlossaryEntryResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_entry(
    project_id: UUID,
    model_id: UUID,
    body: GlossaryEntryCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GlossaryEntryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        entry = GlossaryEntry(
            model_id=model_id,
            term=body.term,
            definition=body.definition,
            context_notes=body.context_notes,
            source="user",
            status="approved",
            version=1,
            proposed_is_hidden=body.proposed_is_hidden,
            visibility=body.visibility or "show",
            confidence=body.confidence or "high",
            created_by=_coerce_user_uuid(current_user.user_id),
        )
        db.add(entry)
        await db.flush()
        for syn in body.synonyms or []:
            db.add(GlossarySynonym(entry_id=entry.id, synonym=syn))
        db.add(
            GlossaryAttachment(
                entry_id=entry.id,
                target_type=body.target_type,
                target_id=body.target_id,
            )
        )
        await db.commit()
        await db.refresh(entry)
        return await _reload(db, entry.id)


@router.post(
    "/bootstrap",
    response_model=GlossaryBootstrapResponse,
    dependencies=[require_role("modeler")],
)
async def bootstrap(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GlossaryBootstrapResponse:
    """Generate proposed glossary entries for every dimension, measure, and
    physical column that does not yet have one.

    Calls the project's configured LLM to generate context-aware business
    definitions. Falls back to heuristic templates if the LLM is not
    configured or the call fails.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)

        model = await db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found")

        source_ids = list((await db.execute(
            select(ModelTable.source_id)
            .where(ModelTable.model_id == model_id)
            .distinct()
        )).scalars().all())
        if (
            source_ids
            and current_user.raw_token
            and not getattr(current_user, "_glossary_run_now", False)
        ):
            job_id = _uuid.uuid4()
            max_distinct = model.glossary_max_distinct or 50
            queued_msg = "Queued source statistics refresh before glossary generation."
            # Persist the job durably so any replica can answer the poll and a
            # restart does not wedge the UI spinner (F-018-03).
            await _create_bootstrap_job(
                db, job_id, project_id, model_id, "queued", queued_msg,
            )
            # Hold a strong reference so the un-awaited task is not GC'd mid-run.
            task = asyncio.create_task(
                _run_glossary_bootstrap_job(
                    job_id=job_id,
                    tenant_id=current_user.tenant_id,
                    user_id=current_user.user_id,
                    email=current_user.email,
                    project_id=project_id,
                    model_id=model_id,
                    source_ids=source_ids,
                    max_distinct=max_distinct,
                )
            )
            _GLOSSARY_BOOTSTRAP_TASKS.add(task)
            task.add_done_callback(_GLOSSARY_BOOTSTRAP_TASKS.discard)
            return _job_response(job_id, "queued", queued_msg)

        existing = await db.execute(
            select(
                GlossaryAttachment.target_type,
                GlossaryAttachment.target_id,
                GlossaryAttachment.entry_id,
            )
            .join(GlossaryEntry, GlossaryEntry.id == GlossaryAttachment.entry_id)
            .where(GlossaryEntry.model_id == model_id)
            .where(GlossaryEntry.superseded_by.is_(None))
        )
        existing_entry_map: dict[tuple[str, UUID], UUID] = {
            (t, tid): eid for t, tid, eid in existing.all()
        }

        items: list[dict] = []
        dims = []
        measures = []

        table_alias_cache: dict[UUID, str] = {}

        async def _resolve_table_alias(source_column_id: UUID | None) -> str:
            if source_column_id is None:
                return ""
            col = await db.get(ModelColumn, source_column_id)
            if col is None:
                return ""
            tid = col.model_table_id
            if tid not in table_alias_cache:
                tbl = await db.get(ModelTable, tid)
                table_alias_cache[tid] = tbl.alias if tbl else ""
            return table_alias_cache[tid]

        dim_result = await db.execute(
            select(Dimension).where(Dimension.model_id == model_id)
        )
        for dim in dim_result.scalars().all():
            dims.append(dim)
            items.append({
                "id": str(dim.id), "kind": "dimension", "name": dim.name,
                "display_name": dim.display_name,
                "is_time_dim": bool(dim.is_time_dim),
                "table_name": await _resolve_table_alias(dim.source_column_id),
            })

        meas_result = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        for meas in meas_result.scalars().all():
            measures.append(meas)
            items.append({
                "id": str(meas.id), "kind": "measure", "name": meas.name,
                "display_name": meas.display_name,
                "default_agg": meas.default_agg,
                "table_name": await _resolve_table_alias(meas.source_column_id),
            })

        skipped = 0

        if not items:
            return GlossaryBootstrapResponse(proposed_count=0)

        # Build table-relationship map from joins for enriched glossary definitions
        joins_result = await db.execute(
            select(Join).where(Join.model_id == model_id)
        )
        table_relationships: dict[UUID, list[str]] = {}
        for j in joins_result.scalars().all():
            lt_alias = table_alias_cache.get(j.left_table_id)
            rt_alias = table_alias_cache.get(j.right_table_id)
            if lt_alias is None:
                lt = await db.get(ModelTable, j.left_table_id)
                lt_alias = (lt.alias or lt.physical_name) if lt else ""
                table_alias_cache[j.left_table_id] = lt_alias
            if rt_alias is None:
                rt = await db.get(ModelTable, j.right_table_id)
                rt_alias = (rt.alias or rt.physical_name) if rt else ""
                table_alias_cache[j.right_table_id] = rt_alias
            if lt_alias:
                table_relationships.setdefault(j.left_table_id, [])
                if rt_alias and rt_alias not in table_relationships[j.left_table_id]:
                    table_relationships[j.left_table_id].append(rt_alias)
            if rt_alias:
                table_relationships.setdefault(j.right_table_id, [])
                if lt_alias and lt_alias not in table_relationships[j.right_table_id]:
                    table_relationships[j.right_table_id].append(lt_alias)

        col_to_table: dict[UUID, UUID] = {}

        def _get_relationships_for_column_sync(source_column_id: UUID | None) -> list[str] | None:
            if source_column_id is None:
                return None
            table_id = col_to_table.get(source_column_id)
            if table_id is None:
                return None
            return table_relationships.get(table_id)

        # Attach source statistics to items for richer LLM context
        all_source_column_ids: set[UUID] = set()
        for dim in dims:
            if dim.source_column_id:
                all_source_column_ids.add(dim.source_column_id)
        for meas in measures:
            if meas.source_column_id:
                all_source_column_ids.add(meas.source_column_id)

        if all_source_column_ids:
            col_rows = (await db.execute(
                select(ModelColumn.id, ModelColumn.model_table_id)
                .where(ModelColumn.id.in_(all_source_column_ids))
            )).all()
            col_to_table.update({r.id: r.model_table_id for r in col_rows})

        item_by_entity_id: dict[str, dict] = {it["id"]: it for it in items}

        if all_source_column_ids:
            stats_q = await db.execute(
                select(SourceColumnStatistics).where(
                    SourceColumnStatistics.model_column_id.in_(all_source_column_ids)
                )
            )
            stats_by_col: dict[UUID, SourceColumnStatistics] = {
                s.model_column_id: s for s in stats_q.scalars().all()
            }
            for dim in dims:
                s = stats_by_col.get(dim.source_column_id) if dim.source_column_id else None
                if s and str(dim.id) in item_by_entity_id:
                    _attach_stats(item_by_entity_id[str(dim.id)], s)
            for meas in measures:
                s = stats_by_col.get(meas.source_column_id) if meas.source_column_id else None
                if s and str(meas.id) in item_by_entity_id:
                    _attach_stats(item_by_entity_id[str(meas.id)], s)

        # Call the optimizer's LLM glossary endpoint
        llm_defs: dict[str, dict] = {}
        llm_provider = None
        llm_model_name = None
        llm_error = None
        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                resp = await client.post(
                    f"{_settings.OPTIMIZER_URL}/api/v1/optimize/glossary/generate",
                    json={
                        "model_id": str(model_id),
                        "model_slug": model.slug,
                        "model_display_name": model.display_name,
                        "model_description": model.description,
                        "items": items,
                    },
                    params={"tenant_id": current_user.tenant_id},
                    headers={"Authorization": f"Bearer {current_user.raw_token}"},
                )
            if resp.is_success:
                data = resp.json()
                llm_provider = data.get("llm_provider")
                llm_model_name = data.get("llm_model")
                llm_error = data.get("error")
                for d in data.get("definitions", []):
                    llm_defs[d["id"]] = {
                        "definition": d["definition"],
                        "synonyms": d.get("synonyms") or [],
                        "confidence": d.get("confidence"),
                    }
                if llm_error:
                    logger.warning(
                        "Glossary bootstrap LLM returned partial error for model %s: %s",
                        model_id, llm_error,
                    )
                else:
                    logger.info(
                        "Glossary bootstrap LLM returned %d definitions for model %s",
                        len(llm_defs), model_id,
                    )
            else:
                llm_error = f"Optimizer returned {resp.status_code}: {resp.text[:300]}"
                logger.error(
                    "Glossary bootstrap optimizer call failed for model %s: %s",
                    model_id, llm_error,
                )
        except Exception as exc:
            llm_error = str(exc)
            logger.error(
                "Glossary bootstrap optimizer call exception for model %s: %s",
                model_id, llm_error, exc_info=True,
            )

        llm_total_failure = bool(llm_error and not llm_defs)

        author_uuid = _coerce_user_uuid(current_user.user_id)
        proposed = 0
        updated = 0
        fallback_count = 0
        max_distinct = model.glossary_max_distinct or 50

        async def _replace_synonyms(entry_id: UUID, new_synonyms: list[str]) -> None:
            await db.execute(
                delete(GlossarySynonym).where(GlossarySynonym.entry_id == entry_id)
            )
            for syn in new_synonyms:
                db.add(GlossarySynonym(entry_id=entry_id, synonym=syn))

        for dim in dims:
            sample = None
            if not dim.is_time_dim:
                sample = _sample_values_from_item_stats(
                    item_by_entity_id.get(str(dim.id)),
                    max_distinct,
                )
            llm = llm_defs.get(str(dim.id))
            if llm:
                definition = llm["definition"]
                synonyms = llm["synonyms"] or _heuristic_synonyms(dim.name)
                vis = "show"
                conf = llm.get("confidence")
                item_source = "llm"
            else:
                rels = _get_relationships_for_column_sync(dim.source_column_id)
                definition = _heuristic_definition_for_dimension(dim, rels)
                synonyms = _heuristic_synonyms(dim.name)
                vis = "review"
                conf = "low"
                item_source = "heuristic"

            existing_eid = existing_entry_map.get(("dimension", dim.id))
            if existing_eid:
                entry = await db.get(GlossaryEntry, existing_eid)
                if sample is not None:
                    entry.sample_values = sample
                if entry.status in ("approved", "rejected") or entry.source == "user":
                    skipped += 1
                    continue
                if llm_total_failure and entry.source != "heuristic":
                    skipped += 1
                    continue
                entry.term = dim.display_name or _humanise(dim.name)
                entry.definition = definition
                entry.source = item_source
                entry.status = "pending_review"
                entry.visibility = vis
                entry.confidence = conf
                await _replace_synonyms(entry.id, synonyms)
                updated += 1
                if item_source == "heuristic":
                    fallback_count += 1
            else:
                entry = GlossaryEntry(
                    model_id=model_id,
                    term=dim.display_name or _humanise(dim.name),
                    definition=definition,
                    source=item_source,
                    status="pending_review",
                    version=1,
                    created_by=author_uuid,
                    visibility=vis,
                    confidence=conf,
                )
                db.add(entry)
                await db.flush()
                for syn in synonyms:
                    db.add(GlossarySynonym(entry_id=entry.id, synonym=syn))
                db.add(GlossaryAttachment(
                    entry_id=entry.id, target_type="dimension", target_id=dim.id,
                ))
                if sample is not None:
                    entry.sample_values = sample
                proposed += 1
                if item_source == "heuristic":
                    fallback_count += 1
            # F-018-20: sample_values was assigned twice in this path (once for
            # the existing entry above, once again here). The existing-entry
            # refresh happens at the top of the `if existing_eid` branch so a
            # skipped (approved/user) entry still gets fresh samples; the new
            # entry is set inside the `else` branch. The trailing duplicate
            # assignment was removed.

        for meas in measures:
            llm = llm_defs.get(str(meas.id))
            if llm:
                definition = llm["definition"]
                synonyms = llm["synonyms"] or _heuristic_synonyms(meas.name)
                vis = "show"
                conf = llm.get("confidence")
                item_source = "llm"
            else:
                rels = _get_relationships_for_column_sync(meas.source_column_id)
                definition = _heuristic_definition_for_measure(meas, rels)
                synonyms = _heuristic_synonyms(meas.name)
                vis = "review"
                conf = "low"
                item_source = "heuristic"

            existing_eid = existing_entry_map.get(("measure", meas.id))
            if existing_eid:
                entry = await db.get(GlossaryEntry, existing_eid)
                if entry.status in ("approved", "rejected") or entry.source == "user":
                    skipped += 1
                    continue
                if llm_total_failure and entry.source != "heuristic":
                    skipped += 1
                    continue
                entry.term = meas.display_name or _humanise(meas.name)
                entry.definition = definition
                entry.source = item_source
                entry.status = "pending_review"
                entry.visibility = vis
                entry.confidence = conf
                await _replace_synonyms(entry.id, synonyms)
                updated += 1
                if item_source == "heuristic":
                    fallback_count += 1
            else:
                entry = GlossaryEntry(
                    model_id=model_id,
                    term=meas.display_name or _humanise(meas.name),
                    definition=definition,
                    source=item_source,
                    status="pending_review",
                    version=1,
                    created_by=author_uuid,
                    visibility=vis,
                    confidence=conf,
                )
                db.add(entry)
                await db.flush()
                for syn in synonyms:
                    db.add(GlossarySynonym(entry_id=entry.id, synonym=syn))
                db.add(GlossaryAttachment(
                    entry_id=entry.id, target_type="measure", target_id=meas.id,
                ))
                proposed += 1
                if item_source == "heuristic":
                    fallback_count += 1

        await db.commit()
        return GlossaryBootstrapResponse(
            proposed_count=proposed,
            updated_count=updated,
            skipped_count=skipped,
            llm_provider=llm_provider,
            llm_model=llm_model_name,
            llm_error=llm_error,
            used_llm=bool(llm_defs),
            fallback_count=fallback_count,
        )


@router.get(
    "/bootstrap/jobs/{job_id}",
    response_model=GlossaryBootstrapResponse,
    dependencies=[require_role("modeler")],
)
async def bootstrap_job_status(
    project_id: UUID,
    model_id: UUID,
    job_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GlossaryBootstrapResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Read the durable job row. Any replica can answer because the status
        # lives in the per-tenant DB rather than a per-process dict (F-018-03).
        job = await db.get(GlossaryBootstrapJob, job_id)
        if (
            job is None
            or job.project_id != project_id
            or job.model_id != model_id
        ):
            raise HTTPException(
                status_code=404, detail="Glossary bootstrap job not found"
            )
        result = _deserialize_job_result(job.result)
        if result is not None:
            return result
        return _job_response(job_id, job.status or "queued", job.message)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/{entry_id}/approve",
    response_model=GlossaryEntryResponse,
    dependencies=[require_role("modeler")],
)
async def approve_entry(
    project_id: UUID,
    model_id: UUID,
    entry_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GlossaryEntryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        entry = await db.get(GlossaryEntry, entry_id)
        if entry is None or entry.model_id != model_id:
            raise HTTPException(status_code=404, detail="Glossary entry not found")
        attachments: list[GlossaryAttachment] | None = None
        # Bootstrap attaches dimensions/measures, not raw columns, so we
        # resolve each attachment's source column (F-018-01). Only load the
        # attachments when a hidden cascade is actually requested.
        if entry.proposed_is_hidden is not None:
            from sqlalchemy.orm import selectinload

            full = await db.execute(
                select(GlossaryEntry)
                .where(GlossaryEntry.id == entry_id)
                .options(selectinload(GlossaryEntry.attachments))
            )
            full_entry = full.scalar_one()
            attachments = list(full_entry.attachments)
        await _approve_loaded_entry(db, entry, current_user, attachments=attachments)
        await db.commit()
        return await _reload(db, entry_id)


async def _approve_loaded_entry(
    db: Any,
    entry: GlossaryEntry,
    current_user: CurrentUser,
    *,
    attachments: list[GlossaryAttachment] | None = None,
) -> None:
    """Apply approval state to a loaded entry and cascade hidden columns.

    Shared by the single-entry approve endpoint and the bulk-approve endpoint
    so both honour the same source-promotion and ``proposed_is_hidden``
    cascade (F-018-01). When ``proposed_is_hidden`` is set the caller must
    supply the entry's attachments (eager-loaded) so the cascade can resolve
    each attachment's underlying source column.
    """
    entry.status = "approved"
    if entry.source == "llm":
        entry.source = "llm_approved"
    # Capture the approver as the first human owner of record when the
    # original LLM proposal had no author.
    if entry.created_by is None:
        entry.created_by = _coerce_user_uuid(current_user.user_id)
    if entry.proposed_is_hidden is not None:
        await _cascade_hidden_to_columns(
            db, attachments or [], bool(entry.proposed_is_hidden)
        )


@router.post(
    "/approve-bulk",
    response_model=GlossaryBulkApproveResponse,
    dependencies=[require_role("modeler")],
)
async def approve_bulk(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GlossaryBulkApproveResponse:
    """Approve all current pending glossary proposals for the model.

    Replaces the client-side approve-one-at-a-time loop with a single
    transactional pass, fixing the avoidable N+1 of one round trip per
    pending entry (F-018-22).
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(GlossaryEntry)
            .where(GlossaryEntry.model_id == model_id)
            .where(GlossaryEntry.status == "pending_review")
            .where(GlossaryEntry.superseded_by.is_(None))
            .options(selectinload(GlossaryEntry.attachments))
        )
        entries = result.scalars().all()
        for entry in entries:
            await _approve_loaded_entry(
                db, entry, current_user, attachments=list(entry.attachments)
            )
        await db.commit()
        return GlossaryBulkApproveResponse(approved_count=len(entries))
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.patch(
    "/{entry_id}",
    response_model=GlossaryEntryResponse,
    dependencies=[require_role("modeler")],
)
async def update_entry(
    project_id: UUID,
    model_id: UUID,
    entry_id: UUID,
    body: GlossaryEntryUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GlossaryEntryResponse:
    """Edit a glossary entry. Creates a new version row and marks the old
    one as `superseded_by` so the audit trail stays intact."""
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        from sqlalchemy.orm import selectinload

        existing_q = await db.execute(
            select(GlossaryEntry)
            .where(GlossaryEntry.id == entry_id)
            .options(
                selectinload(GlossaryEntry.synonyms),
                selectinload(GlossaryEntry.attachments),
            )
        )
        existing = existing_q.scalar_one_or_none()
        if existing is None or existing.model_id != model_id:
            raise HTTPException(status_code=404, detail="Glossary entry not found")
        if existing.superseded_by is not None:
            raise HTTPException(
                status_code=409,
                detail="Entry has already been superseded; edit the latest version.",
            )

        new_entry = GlossaryEntry(
            model_id=model_id,
            term=body.term if body.term is not None else existing.term,
            definition=body.definition if body.definition is not None else existing.definition,
            context_notes=(
                body.context_notes if body.context_notes is not None else existing.context_notes
            ),
            source="user",
            status="approved",
            version=existing.version + 1,
            proposed_is_hidden=(
                body.proposed_is_hidden
                if body.proposed_is_hidden is not None
                else existing.proposed_is_hidden
            ),
            visibility=(
                body.visibility if body.visibility is not None else existing.visibility
            ),
            confidence=(
                body.confidence if body.confidence is not None else existing.confidence
            ),
            created_by=_coerce_user_uuid(current_user.user_id),
        )
        db.add(new_entry)
        await db.flush()

        existing.superseded_by = new_entry.id

        # Re-attach synonyms (replace the whole list if the caller provided one)
        if body.synonyms is not None:
            for syn in body.synonyms:
                db.add(GlossarySynonym(entry_id=new_entry.id, synonym=syn))
        else:
            for syn in existing.synonyms:
                db.add(GlossarySynonym(entry_id=new_entry.id, synonym=syn.synonym))

        for att in existing.attachments:
            db.add(
                GlossaryAttachment(
                    entry_id=new_entry.id,
                    target_type=att.target_type,
                    target_id=att.target_id,
                )
            )

        # Cascade is_hidden if the modeller set it. The attachments were
        # copied from the superseded entry above; resolve each to its source
        # column so dimension/measure attachments hide too (F-018-01).
        if new_entry.proposed_is_hidden is not None:
            await _cascade_hidden_to_columns(
                db, list(existing.attachments), bool(new_entry.proposed_is_hidden)
            )

        await db.commit()
        return await _reload(db, new_entry.id)


@router.post(
    "/{entry_id}/reject",
    response_model=GlossaryEntryResponse,
    dependencies=[require_role("modeler")],
)
async def reject_entry(
    project_id: UUID,
    model_id: UUID,
    entry_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GlossaryEntryResponse:
    """Soft-delete a glossary entry by flipping status to 'rejected'.

    Phase 3 of the semantic-layer plan: the audit trail stays intact so
    rejected proposals can still be surfaced in history views. A rejected
    entry is excluded from the public payload and from gateway description
    lookups, but the row is kept for provenance.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        entry = await db.get(GlossaryEntry, entry_id)
        if entry is None or entry.model_id != model_id:
            raise HTTPException(status_code=404, detail="Glossary entry not found")
        entry.status = "rejected"
        await db.commit()
        return await _reload(db, entry_id)


@router.delete(
    "/{entry_id}",
    status_code=204,
    dependencies=[require_role("modeler")],
)
async def delete_entry(
    project_id: UUID,
    model_id: UUID,
    entry_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> Response:
    """Hard-delete a glossary entry and its full version chain.

    Only the latest version (superseded_by IS NULL) can be deleted.
    Removes the entry, all synonyms, all attachments, and every prior
    version linked through superseded_by.  ORM cascade handles child
    rows; we walk the version chain explicitly.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        entry = await db.get(GlossaryEntry, entry_id)
        if entry is None or entry.model_id != model_id:
            raise HTTPException(status_code=404, detail="Glossary entry not found")
        if entry.superseded_by is not None:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete a superseded entry; delete the latest version.",
            )

        ids_to_delete: list[UUID] = [entry.id]
        visited: set[UUID] = {entry.id}
        frontier = [entry.id]
        while frontier:
            result = await db.execute(
                select(GlossaryEntry.id)
                .where(GlossaryEntry.model_id == model_id)
                .where(GlossaryEntry.superseded_by.in_(frontier))
            )
            predecessors = [rid for (rid,) in result.all() if rid not in visited]
            ids_to_delete.extend(predecessors)
            visited.update(predecessors)
            frontier = predecessors

        for eid in ids_to_delete:
            e = await db.get(GlossaryEntry, eid)
            if e is not None:
                await db.delete(e)

        await db.commit()
        return Response(status_code=204)


@router.post(
    "/delete-bulk",
    response_model=GlossaryBulkDeleteResponse,
    dependencies=[require_role("modeler")],
)
async def delete_bulk(
    project_id: UUID,
    model_id: UUID,
    body: GlossaryBulkDeleteRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> GlossaryBulkDeleteResponse:
    """Bulk-delete glossary entries by scope.

    scope:
      - all         every entry for the model.
      - heuristic   entries whose latest version source is "heuristic".
      - non_manual  machine-proposed entries NOT yet approved by a human.

    F-018-23: ``non_manual`` previously matched ``source != "user"``, which swept
    away ``llm_approved`` entries (an LLM proposal a human reviewed and approved)
    and approved ``heuristic`` entries — curated content the label "non-manual"
    does not warn about. The scope now deletes only machine-proposed entries
    that are still un-approved (status != "approved" and source != "user"), so a
    human-approved definition is never destroyed by the bulk action.

    Walks each matched entry's version chain (superseded_by) so prior
    versions are removed too, mirroring single-entry delete.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)

        q = (
            select(GlossaryEntry)
            .where(GlossaryEntry.model_id == model_id)
            .where(GlossaryEntry.superseded_by.is_(None))
        )
        if body.scope == "heuristic":
            q = q.where(GlossaryEntry.source == "heuristic")
        elif body.scope == "non_manual":
            q = (
                q.where(GlossaryEntry.source != "user")
                .where(GlossaryEntry.status != "approved")
            )

        latest_entries = (await db.execute(q)).scalars().all()

        ids_to_delete: list[UUID] = []
        visited: set[UUID] = set()
        for entry in latest_entries:
            if entry.id in visited:
                continue
            visited.add(entry.id)
            ids_to_delete.append(entry.id)
            frontier = [entry.id]
            while frontier:
                result = await db.execute(
                    select(GlossaryEntry.id)
                    .where(GlossaryEntry.model_id == model_id)
                    .where(GlossaryEntry.superseded_by.in_(frontier))
                )
                preds = [rid for (rid,) in result.all() if rid not in visited]
                ids_to_delete.extend(preds)
                visited.update(preds)
                frontier = preds

        deleted = 0
        for eid in ids_to_delete:
            e = await db.get(GlossaryEntry, eid)
            if e is not None:
                await db.delete(e)
                deleted += 1

        await db.commit()
        return GlossaryBulkDeleteResponse(deleted_count=deleted)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/import",
    dependencies=[require_role("modeler")],
)
async def import_glossary_csv(
    project_id: UUID,
    model_id: UUID,
    payload: dict[str, Any],
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict[str, Any]:
    """Bulk-import glossary entries from a CSV blob.

    Body shape: `{"csv": "term,description\\nfoo,bar\\n..."}`.
    Each row creates a fresh `user`-source `approved` entry; existing rows
    are not touched. Reports per-line errors so the modeller can fix and
    retry.
    """
    raw = (payload or {}).get("csv")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(
            status_code=400,
            detail="Body must be {csv: '<csv-text>'} with a non-empty value.",
        )

    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV is empty.")

    header = [c.strip().lower() for c in rows[0]]
    if header[:2] != ["term", "description"]:
        raise HTTPException(
            status_code=400,
            detail="First CSV row must be the header 'term,description'.",
        )

    created = 0
    errors: list[dict[str, Any]] = []
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        author_uuid = _coerce_user_uuid(current_user.user_id)
        for line_no, row in enumerate(rows[1:], start=2):
            if not row or all(not c.strip() for c in row):
                continue
            if len(row) < 2:
                errors.append({"line": line_no, "error": "Need at least 2 columns."})
                continue
            term = row[0].strip()
            definition = row[1].strip()
            if not term or not definition:
                errors.append(
                    {"line": line_no, "error": "term and description must be non-empty."}
                )
                continue
            entry = GlossaryEntry(
                model_id=model_id,
                term=term,
                definition=definition,
                source="user",
                status="approved",
                version=1,
                visibility="show",
                confidence="high",
                created_by=author_uuid,
            )
            db.add(entry)
            created += 1
        await db.commit()
        return {"created": created, "errors": errors}
    raise HTTPException(status_code=500, detail="DB session exhausted")


async def _reload(db, entry_id: UUID) -> GlossaryEntryResponse:
    """Refetch an entry with synonyms and attachments eagerly loaded."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(GlossaryEntry)
        .where(GlossaryEntry.id == entry_id)
        .options(
            selectinload(GlossaryEntry.synonyms),
            selectinload(GlossaryEntry.attachments),
        )
    )
    return _entry_to_response(result.scalar_one())


# ---------------------------------------------------------------------------
# Phase 4 — public glossary distribution
# ---------------------------------------------------------------------------


def _issue_public_token(
    tenant_id: str, model_id: UUID, jti: UUID
) -> str:
    """Sign a long-lived JWT carrying tenant_id, model_id and a unique
    `jti` claim that ties the token back to its row in
    `glossary_share_token`. Revocation flips `revoked_at` on that row and
    the decoder rejects tokens whose `jti` is either missing from the
    registry or marked revoked.
    """
    payload = {
        "purpose": _PUBLIC_TOKEN_PURPOSE,
        "tenant_id": tenant_id,
        "model_id": str(model_id),
        "jti": str(jti),
        "iat": datetime.now(timezone.utc),
        # Long expiry — modeller can revoke or regenerate if it leaks.
        "exp": datetime.now(timezone.utc) + timedelta(days=365),
    }
    return jwt.encode(payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM)


def _decode_public_token(token: str) -> tuple[str, UUID, UUID]:
    try:
        decoded = jwt.decode(
            token, _settings.JWT_SECRET_KEY, algorithms=[_settings.JWT_ALGORITHM]
        )
    except JWTError as exc:
        raise HTTPException(status_code=404, detail="Invalid or expired token") from exc
    if decoded.get("purpose") != _PUBLIC_TOKEN_PURPOSE:
        raise HTTPException(status_code=404, detail="Token has wrong purpose")
    tenant_id = decoded.get("tenant_id")
    model_id = decoded.get("model_id")
    jti = decoded.get("jti")
    if not tenant_id or not model_id or not jti:
        raise HTTPException(status_code=404, detail="Token payload incomplete")
    try:
        return tenant_id, UUID(model_id), UUID(jti)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Token payload malformed") from exc


async def _ensure_token_active(tenant_id: str, jti: UUID) -> None:
    """Look up the token row by `jti` and 404 when missing or revoked.

    Phase 4 of the semantic-layer plan: the plan required token
    revocation as a hard gate. Without the registry, a leaked share link
    would remain usable for the JWT's full lifetime; with it, a single
    flip on `revoked_at` takes the link down instantly.
    """
    async for db in get_tenant_db(tenant_id):
        row = await db.get(GlossaryShareToken, jti)
        if row is None or row.revoked_at is not None:
            raise HTTPException(status_code=404, detail="Invalid or revoked token")


@router.post("/share", dependencies=[require_role("modeler")])
async def issue_share_token(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict[str, str]:
    """Issue a tokenized share link for the public glossary HTML page.

    The frontend route `/g/<token>` is what gets shared with non-technical
    co-workers; that page is served by the frontend nginx and fetches the
    JSON payload from `/api/v1/glossary/public/<token>`.

    Each issued token is registered in `glossary_share_token` so it can
    be revoked or regenerated later.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        jti = _uuid.uuid4()
        db.add(
            GlossaryShareToken(
                id=jti,
                model_id=model_id,
                created_by=_coerce_user_uuid(current_user.user_id),
            )
        )
        await db.commit()
        token = _issue_public_token(current_user.tenant_id, model_id, jti)
        return {"token": token, "frontend_path": f"/g/{token}"}


@router.post("/share/revoke", dependencies=[require_role("modeler")])
async def revoke_share_tokens(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict[str, int]:
    """Revoke every active share token for this model.

    Phase 4 of the semantic-layer plan — the token is the only auth, so
    a clean revocation path is mandatory. The caller flips `revoked_at`
    on every live row; any `/g/<token>` request bound to those rows
    starts returning 404 immediately.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(GlossaryShareToken)
            .where(GlossaryShareToken.model_id == model_id)
            .where(GlossaryShareToken.revoked_at.is_(None))
        )
        rows = result.scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            row.revoked_at = now
        await db.commit()
        return {"revoked_count": len(rows)}


@router.post("/share/regenerate", dependencies=[require_role("modeler")])
async def regenerate_share_token(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict[str, str]:
    """Revoke every outstanding token and issue a fresh one in a single
    call. Phase 4 of the semantic-layer plan — the "regenerate" button
    on the glossary panel maps to this endpoint.
    """
    await revoke_share_tokens(project_id, model_id, current_user)
    return await issue_share_token(project_id, model_id, current_user)


# A second router with a different prefix for the public, no-auth endpoints.
public_router = APIRouter(prefix="/glossary/public", tags=["glossary-public"])


async def _build_public_payload(tenant_id: str, model_id: UUID) -> dict[str, Any]:
    """Assemble the published glossary payload — only approved entries,
    no provenance noise that would confuse non-technical readers.
    """
    async for db in get_tenant_db(tenant_id):
        model = await db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found")

        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(GlossaryEntry)
            .where(GlossaryEntry.model_id == model_id)
            .where(GlossaryEntry.status == "approved")
            .where(GlossaryEntry.superseded_by.is_(None))
            .options(
                selectinload(GlossaryEntry.synonyms),
                selectinload(GlossaryEntry.attachments),
            )
            .order_by(GlossaryEntry.term)
        )
        entries = result.scalars().all()

        return {
            "model": {
                "id": str(model.id),
                "slug": model.slug,
                "display_name": model.display_name,
                "description": getattr(model, "description", None),
            },
            "entries": [
                {
                    "term": e.term,
                    "definition": e.definition,
                    "context_notes": e.context_notes,
                    "synonyms": [s.synonym for s in e.synonyms],
                    "attachments": [
                        {"target_type": a.target_type, "target_id": str(a.target_id) if a.target_id else None}
                        for a in e.attachments
                    ],
                    "version": e.version,
                    "updated_at": e.updated_at.isoformat(),
                }
                for e in entries
            ],
        }


@public_router.get("/{token}")
async def public_glossary_json(token: str) -> dict[str, Any]:
    tenant_id, model_id, jti = _decode_public_token(token)
    await _ensure_token_active(tenant_id, jti)
    return await _build_public_payload(tenant_id, model_id)


@public_router.get("/{token}/download.csv")
async def public_glossary_csv(token: str) -> Response:
    tenant_id, model_id, jti = _decode_public_token(token)
    await _ensure_token_active(tenant_id, jti)
    payload = await _build_public_payload(tenant_id, model_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["term", "definition", "synonyms", "context_notes", "version", "updated_at"])
    for e in payload["entries"]:
        writer.writerow(
            [
                _formula_guard(e["term"]),
                _formula_guard(e["definition"]),
                _formula_guard("; ".join(e["synonyms"])),
                _formula_guard(e["context_notes"] or ""),
                e["version"],
                e["updated_at"],
            ]
        )
    filename = f"{payload['model']['slug']}-glossary.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@public_router.get("/{token}/download.xlsx")
async def public_glossary_xlsx(token: str) -> StreamingResponse:
    tenant_id, model_id, jti = _decode_public_token(token)
    await _ensure_token_active(tenant_id, jti)
    payload = await _build_public_payload(tenant_id, model_id)
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="XLSX export requires openpyxl. Install it on the model-service image.",
        ) from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Glossary"
    ws.append(["Term", "Definition", "Synonyms", "Context", "Version", "Updated"])
    for e in payload["entries"]:
        ws.append(
            [
                _formula_guard(e["term"]),
                _formula_guard(e["definition"]),
                _formula_guard("; ".join(e["synonyms"])),
                _formula_guard(e["context_notes"] or ""),
                e["version"],
                e["updated_at"],
            ]
        )
    for col_letter, width in (("A", 24), ("B", 60), ("C", 30), ("D", 40), ("E", 8), ("F", 24)):
        ws.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"{payload['model']['slug']}-glossary.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@public_router.get("/{token}/download.pdf")
async def public_glossary_pdf(token: str) -> StreamingResponse:
    """Phase 4 of the semantic-layer plan — printable A4 glossary.

    Uses ReportLab's Platypus story builder for paragraph wrapping and
    page breaks. The layout mirrors the HTML page: model header, then
    one block per entry with term, definition, synonyms and context.
    """
    tenant_id, model_id, jti = _decode_public_token(token)
    await _ensure_token_active(tenant_id, jti)
    payload = await _build_public_payload(tenant_id, model_id)

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
        )
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="PDF export requires reportlab. Install it on the model-service image.",
        ) from exc

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"{payload['model']['display_name']} Glossary",
    )
    styles = getSampleStyleSheet()
    term_style = ParagraphStyle(
        "Term",
        parent=styles["Heading3"],
        spaceBefore=8,
        spaceAfter=2,
    )
    body_style = styles["BodyText"]
    meta_style = ParagraphStyle(
        "Meta",
        parent=styles["BodyText"],
        fontSize=8,
        textColor="#555555",
    )

    story: list[Any] = []
    story.append(
        Paragraph(_pdf_escape(payload["model"]["display_name"] or payload["model"]["slug"]), styles["Title"])
    )
    if payload["model"].get("description"):
        story.append(Paragraph(_pdf_escape(payload["model"]["description"]), body_style))
    story.append(Spacer(1, 6 * mm))

    if not payload["entries"]:
        story.append(Paragraph("No approved entries yet.", body_style))

    for entry in payload["entries"]:
        story.append(Paragraph(_pdf_escape(entry["term"]), term_style))
        story.append(Paragraph(_pdf_escape(entry["definition"]), body_style))
        if entry["context_notes"]:
            story.append(Paragraph("Context: " + _pdf_escape(entry["context_notes"]), meta_style))
        if entry["synonyms"]:
            story.append(
                Paragraph(
                    "Also known as: " + ", ".join(_pdf_escape(s) for s in entry["synonyms"]),
                    meta_style,
                )
            )

    doc.build(story)
    buf.seek(0)
    filename = f"{payload['model']['slug']}-glossary.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
