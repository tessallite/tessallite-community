"""Gateway query log analysis for impact analysis."""
from __future__ import annotations

import hashlib
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from shared.db.models import (
    GatewayQueryReference,
    Model,
    ModelTable,
    QueryLog,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import ImpactScanResponse
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/impact",
    tags=["impact-analysis"],
)


def _not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def _get_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise _not_found("Model not found")
    return model


def _hash_query(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _table_matches(table_name: str, raw_lower: str) -> bool:
    """Identifier-aware table-name match (F-030-10).

    A plain substring test makes table ``order`` match ``orders`` and
    ``order_items``. Require the table name to appear as a whole SQL identifier
    token — bounded by non-identifier characters (``[a-z0-9_]`` is the SQL
    identifier class) on both sides — so ``order`` no longer matches ``orders``.
    """
    return re.search(rf"(?<![a-z0-9_]){re.escape(table_name)}(?![a-z0-9_])", raw_lower) is not None


@router.post(
    "/scan",
    response_model=ImpactScanResponse,
    dependencies=[require_role("modeler")],
)
async def run_impact_scan(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ImpactScanResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)

        table_rows = (
            await db.execute(
                select(ModelTable.physical_name)
                .where(ModelTable.model_id == model_id)
            )
        ).scalars().all()
        table_names = {t.lower() for t in table_rows if t}

        if not table_names:
            return ImpactScanResponse(references_upserted=0, tables_matched=0)

        # F-030-10: only count log rows newer than the last-processed watermark.
        # The watermark is the max last_seen_at already recorded for this model;
        # re-running the scan on an unchanged log therefore counts nothing
        # (idempotent), instead of re-incrementing every recorded (table, hash)
        # pair on each press of the scan button.
        watermark = (
            await db.execute(
                select(func.max(GatewayQueryReference.last_seen_at))
                .where(GatewayQueryReference.model_id == model_id)
            )
        ).scalar_one_or_none()

        log_stmt = (
            select(QueryLog)
            .where(QueryLog.model_id == model_id, QueryLog.status == "success")
        )
        if watermark is not None:
            log_stmt = log_stmt.where(QueryLog.created_at > watermark)
        logs = (
            await db.execute(
                log_stmt.order_by(QueryLog.created_at.asc()).limit(5000)
            )
        ).scalars().all()

        upserted = 0
        matched_tables: set[str] = set()

        for log_entry in logs:
            raw = (log_entry.raw_query or "").lower()
            for tname in table_names:
                if not _table_matches(tname, raw):
                    continue
                matched_tables.add(tname)
                qhash = _hash_query(log_entry.raw_query or "")

                existing = (
                    await db.execute(
                        select(GatewayQueryReference).where(
                            GatewayQueryReference.model_id == model_id,
                            GatewayQueryReference.queried_table == tname,
                            GatewayQueryReference.query_text_hash == qhash,
                        )
                    )
                ).scalar_one_or_none()

                if existing:
                    existing.hit_count += 1
                    # F-030-10: never move last_seen_at backwards. Logs are
                    # iterated oldest-first now, but guard explicitly so an
                    # out-of-order row cannot rewind the recency timestamp.
                    if existing.last_seen_at is None or log_entry.created_at > existing.last_seen_at:
                        existing.last_seen_at = log_entry.created_at
                else:
                    db.add(
                        GatewayQueryReference(
                            model_id=model_id,
                            queried_table=tname,
                            query_user=log_entry.user_identity,
                            query_text_hash=qhash,
                            last_seen_at=log_entry.created_at,
                        )
                    )
                    upserted += 1

        await db.commit()
        return ImpactScanResponse(
            references_upserted=upserted,
            tables_matched=len(matched_tables),
        )
