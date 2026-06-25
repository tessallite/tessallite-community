"""
DataSource CRUD routes (physical table/view registrations).
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.config.settings import get_settings
from shared.db.models import DataSource, ProjectConnection
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import DataSourceCreate, DataSourceResponse, DataSourceUpdate
from src.api._scope import ensure_model_in_project, resolve_source_connection
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/sources", tags=["sources"]
)


async def _validate_source_connection(
    db, project_id: UUID, project_connection_id: UUID
) -> None:
    conn = await db.get(ProjectConnection, project_connection_id)
    if conn is None:
        raise HTTPException(
            status_code=422,
            detail="project_connection_id does not reference an existing connection",
        )
    if conn.project_id != project_id:
        raise HTTPException(
            status_code=422,
            detail="Source connection belongs to a different project",
        )


@router.post(
    "",
    response_model=DataSourceResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_source(
    project_id: UUID,
    model_id: UUID,
    body: DataSourceCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataSourceResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await _validate_source_connection(db, project_id, body.project_connection_id)
        source = DataSource(model_id=model_id, **body.model_dump())
        db.add(source)
        await db.commit()
        await db.refresh(source)
        return DataSourceResponse.model_validate(source)


@router.get("", response_model=list[DataSourceResponse], dependencies=[require_role("viewer")])
async def list_sources(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DataSourceResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(DataSource).where(DataSource.model_id == model_id)
        )
        return [DataSourceResponse.model_validate(s) for s in result.scalars().all()]


@router.get("/{source_id}", response_model=DataSourceResponse, dependencies=[require_role("viewer")])
async def get_source(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataSourceResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        s = await db.get(DataSource, source_id)
        if s is None or s.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")
        return DataSourceResponse.model_validate(s)


@router.patch(
    "/{source_id}",
    response_model=DataSourceResponse,
    dependencies=[require_role("modeler")],
)
async def update_source(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    body: DataSourceUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataSourceResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        s = await db.get(DataSource, source_id)
        if s is None or s.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")
        updates = body.model_dump(exclude_unset=True)
        new_conn_id = updates.get("project_connection_id")
        if new_conn_id is not None and new_conn_id != s.project_connection_id:
            await _validate_source_connection(db, project_id, new_conn_id)
        for k, v in updates.items():
            setattr(s, k, v)
        await db.commit()
        await db.refresh(s)
        return DataSourceResponse.model_validate(s)


@router.delete(
    "/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_source(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        s = await db.get(DataSource, source_id)
        if s is None or s.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")
        await db.delete(s)
        await db.commit()


@router.get(
    "/{source_id}/schemas",
    dependencies=[require_role("viewer")],
)
async def list_schemas(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[str]:
    """Return available schemas/datasets from the source connection."""
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        source = await db.get(DataSource, source_id)
        if source is None or source.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")
        # Bug-5325: fail closed if this source's connection belongs to another
        # project (legacy/imported malformed row). project_id is the source's
        # owning project — ensure_model_in_project confirmed the model lives in
        # it and the source belongs to that model.
        await resolve_source_connection(
            db, source, expected_project_id=project_id
        )
        try:
            return await _fetch_schemas(
                str(model_id), current_user.raw_token,
                source_id=str(source_id),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Schema fetch failed: {exc}"
            ) from exc
    return []


async def _fetch_schemas(
    model_id: str, bearer: str, *, source_id: str | None = None,
) -> list[str]:
    """Fetch schema/dataset names via the query-router /introspect endpoint."""
    sql = (
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name NOT IN ('information_schema','pg_catalog','pg_toast') "
        "ORDER BY schema_name"
    )
    _settings = get_settings()
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/introspect"
    headers = {"Authorization": f"Bearer {bearer}"}
    body: dict = {"model_id": model_id, "raw_sql": sql}
    if source_id:
        body["source_id"] = source_id
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else resp.text
            except Exception:
                detail = resp.text or f"Introspect returned HTTP {resp.status_code}"
            raise HTTPException(status_code=resp.status_code, detail=detail)
        rows = resp.json()["rows"]
    return [r["schema_name"] for r in rows]
