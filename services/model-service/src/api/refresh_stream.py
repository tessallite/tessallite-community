"""
SSE endpoint for refresh run progress.

GET /api/v1/projects/{project_id}/models/{model_id}/refresh/stream

Streams Server-Sent Events for active AggregateRefreshRun and
PocketRefreshRun rows belonging to the model. The endpoint polls
the DB every 2 seconds for new or updated rows, pushes each as a
JSON event, and closes the stream when:
  - all active runs have reached a terminal state (completed / failed), or
  - the 5-minute timeout is reached.

Auth: viewer or above.
Uses Starlette StreamingResponse — no extra dependency required.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from shared.db.models import AggregateDefinition, AggregateRefreshRun, PocketDefinition, PocketRefreshRun
from shared.db.session import get_tenant_db
from src.api._scope import ensure_model_in_project
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/refresh",
    tags=["refresh-stream"],
)

# Canonical terminal vocabulary lives on the ORM (Bug-1025: a local drifted
# set omitted "completed", so the stream never emitted "done" and every
# connection polled for the full timeout). Pocket runs share the vocabulary.
_TERMINAL = AggregateRefreshRun.TERMINAL_STATUSES
_POLL_INTERVAL = 2.0       # seconds between DB polls
_TIMEOUT = 300.0           # hard stop after 5 minutes


def _run_to_dict(run, run_type: str) -> dict:
    return {
        "run_type": run_type,
        "id": str(run.id),
        "status": run.status,
        "refresh_mode": run.refresh_mode,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "rows_written": run.rows_written,
        "error_message": run.error_message,
    }


async def _stream_events(
    model_id: UUID,
    tenant_id: str,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE-formatted strings."""
    start = asyncio.get_event_loop().time()
    seen: dict[str, str] = {}  # run_id -> last_status

    # Initial heartbeat so the client knows the connection is live.
    yield "event: connected\ndata: {}\n\n"

    while True:
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed >= _TIMEOUT:
            yield "event: timeout\ndata: {}\n\n"
            return

        async for db in get_tenant_db(tenant_id):
            # Aggregate refresh runs for this model
            agg_stmt = (
                select(AggregateRefreshRun)
                .join(
                    AggregateDefinition,
                    AggregateRefreshRun.aggregate_definition_id == AggregateDefinition.id,
                )
                .where(AggregateDefinition.model_id == model_id)
                .order_by(AggregateRefreshRun.started_at.desc())
                .limit(100)
            )
            agg_result = await db.execute(agg_stmt)
            agg_runs = list(agg_result.scalars().all())

            # Pocket refresh runs for this model
            pocket_stmt = (
                select(PocketRefreshRun)
                .join(
                    PocketDefinition,
                    PocketRefreshRun.pocket_definition_id == PocketDefinition.id,
                )
                .where(PocketDefinition.model_id == model_id)
                .order_by(PocketRefreshRun.started_at.desc())
                .limit(100)
            )
            pocket_result = await db.execute(pocket_stmt)
            pocket_runs = list(pocket_result.scalars().all())

        all_runs = [(_run_to_dict(r, "aggregate"), r.status) for r in agg_runs] + [
            (_run_to_dict(r, "pocket"), r.status) for r in pocket_runs
        ]

        for run_dict, current_status in all_runs:
            run_id = run_dict["id"]
            if seen.get(run_id) != current_status:
                seen[run_id] = current_status
                yield f"data: {json.dumps(run_dict)}\n\n"

        # Close when all known active runs are in terminal state.
        if all_runs:
            all_terminal = all(s in _TERMINAL for _, s in all_runs)
            if all_terminal:
                yield "event: done\ndata: {}\n\n"
                return

        await asyncio.sleep(_POLL_INTERVAL)


@router.get(
    "/stream",
    dependencies=[require_role("viewer")],
    response_class=StreamingResponse,
    # Tell OpenAPI this is a text/event-stream endpoint.
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_refresh_runs(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> StreamingResponse:
    # Bug-8862: prove project -> model BEFORE the response starts. require_role
    # gates the caller's ROLE on the path project but never proves the model
    # belongs to it, so without this a caller bound to project A could stream
    # another project's refresh-run history by substituting model_id.
    #
    # The check runs HERE and not inside ``_stream_events``: once the generator
    # is handed to StreamingResponse the 200 and the SSE content-type are
    # already committed, so an HTTPException raised in the generator cannot
    # become a clean 404 — it would surface as a broken stream instead.
    #
    # FAIL CLOSED: the ``return`` sits INSIDE the loop and the fall-through
    # raises. The only path that reaches StreamingResponse is the one that ran
    # the guard. A guard followed by an unconditional ``return`` outside the
    # loop would open the stream unchecked if ``get_tenant_db`` ever yielded
    # zero sessions — unreachable today (it always yields exactly once), but
    # the wrong shape for a tenant-isolation check, and unlike every other
    # handler in this service, whose whole body sits inside the loop so a
    # zero-yield short-circuits to empty rather than to protected data.
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        return StreamingResponse(
            _stream_events(model_id, current_user.tenant_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Tenant database unavailable",
    )
