"""
DataSource CRUD routes (physical table/view registrations).
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from shared.aggregate_connection import model_ids_reading_source
from shared.artifact_target_binding import invalidate_artifacts_for_model
from shared.db.models import DataSource, ProjectConnection
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import DataSourceCreate, DataSourceResponse, DataSourceUpdate
from src.api._scope import ensure_model_in_project, resolve_source_connection
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

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
        # Bug-8441: ``data_sources`` IS revert-owned. On the revert path the
        # rehydrator upserts every column of each snapshot source AND
        # hard-deletes every live source absent from the snapshot
        # (``_reconcile_sources_and_targets``, Bug-7147). A source created here
        # without the lock, committing just before that reconcile read, is
        # deleted outright with no error and no audit trail.
        await acquire_model_definition_lock(db, model_id)
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
        # Bug-8441: read-modify-write on a revert-owned row. The revert UPSERTS
        # every column of this source from the snapshot, so an edit applied to a
        # pre-revert copy of the row is silently overwritten (or overwrites the
        # restored value). READ-UNDER-LOCK: acquire before the entity fetch so
        # the row this handler edits cannot be replaced underneath it.
        await acquire_model_definition_lock(db, model_id)
        s = await db.get(DataSource, source_id)
        if s is None or s.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")
        updates = body.model_dump(exclude_unset=True)
        new_conn_id = updates.get("project_connection_id")
        if new_conn_id is not None and new_conn_id != s.project_connection_id:
            await _validate_source_connection(db, project_id, new_conn_id)
        _conn_before = s.project_connection_id
        for k, v in updates.items():
            setattr(s, k, v)
        # Bug-8602: re-pointing a source at another connection changes WHICH
        # physical database every already-built aggregate and pocket of this
        # model was materialised FROM, without touching either ProjectConnection
        # row. A connection-keyed invalidator therefore cannot see it, and the
        # definition closure only refuses the next REBUILD — the artifact
        # already in the serving pool keeps answering from the previous
        # database while the source-route fallback for the same query reads the
        # new one. Invalidate in THIS transaction so the two can never be
        # observed apart.
        if _conn_before != s.project_connection_id:
            # Round-2 review finding 3: also cover any OTHER model whose tables
            # read through this DataSource. Normally none, but ModelTable has no
            # composite FK back to (model_id, source_id), so the state is
            # representable and the reverse enumeration already claims to catch
            # it.
            await invalidate_artifacts_for_model(
                db, model_id,
                also_models=await model_ids_reading_source(source_id, db),
                reason=(
                    "The model's source database changed, so this cache was "
                    "built against a different database and must be rebuilt "
                    "before it can serve again."
                ),
            )
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
    # Bug-7794 [SECURITY / integrity]: ModelTable.source_id is ondelete=CASCADE,
    # so a bare ``db.delete(source)`` DB-cascades every table (and their
    # columns/UDAs) out from under the model — bypassing the entire Bug-6225
    # table-delete cleanup. That leaves dangling hierarchy levels, half-alive
    # dims/measures in persona allow-lists, un-purged soft references, and no
    # revalidation. Route EVERY table under the source through the same shared
    # cleanup the single-table delete uses, then revalidate once.
    from sqlalchemy.exc import IntegrityError

    from shared.semantic.model_validator import revalidate_model
    from shared.db.models import ModelTable
    from src.api._table_cleanup import (
        assert_table_not_rls_mapping,
        cleanup_table_dependents,
        is_rls_mapping_integrity_error,
    )

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982 R6 (round-3 BLOCKER): although the ``data_sources`` row itself is
        # preserved-in-place on revert, this DELETE cascades (FK ondelete=CASCADE +
        # cleanup_table_dependents) to ModelTable/ModelColumn/Dimension/Measure/
        # Hierarchy — the truncate-reinserted snapshot-owned family. It is therefore
        # a read-modify-write that MUST serialise with deploy/revert: without the
        # lock, a concurrent revert reinserts tables/columns while this cascade
        # deletes the pre-revert set, leaving the live model out of sync with the
        # version it was reverted to. Read the table enumeration below under the lock.
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        s = await db.get(DataSource, source_id)
        if s is None or s.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")

        # Lock the source row FOR UPDATE so a concurrent create_table on this
        # source blocks until this delete resolves — otherwise a table created
        # after the enumeration below would miss cleanup and then be
        # cascade-deleted with dangling dependents.
        await db.execute(
            select(DataSource.id)
            .where(DataSource.id == source_id)
            .with_for_update()
        )

        table_ids = (
            await db.execute(
                select(ModelTable.id).where(ModelTable.source_id == source_id)
            )
        ).scalars().all()

        # Reject the whole delete up front if ANY table under the source is an
        # RLS mapping table, so we never partially clean up before a 409 on a
        # later table. (assert_table_not_rls_mapping also locks each table row.)
        for table_id in table_ids:
            await assert_table_not_rls_mapping(
                db, model_id=model_id, table_id=table_id
            )

        for table_id in table_ids:
            await cleanup_table_dependents(db, model_id=model_id, table_id=table_id)

        await db.delete(s)
        try:
            await db.flush()
            # Revalidate once after the whole source (and its cascade) is gone,
            # so the model snapshot reflects the removed tables/measures/dims.
            await revalidate_model(model_id, db)
            await db.commit()
        except IntegrityError as exc:
            # Safety net: a residual race (an RLS mapping-rule insert that
            # committed past the guard + row lock) must surface as a clean 409,
            # not a raw 500.
            await db.rollback()
            if is_rls_mapping_integrity_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot delete this source; one of its tables is the "
                        "mapping table for a row-security rule. Retire or "
                        "re-point those rules first."
                    ),
                ) from exc
            raise


@router.get(
    "/{source_id}/schemas",
    # Bug-7153: raised from viewer to modeler to match the model-service
    # discover_tables/discover_columns gates and the query-router
    # connection-introspect RBAC (Bug-7167: min_role="modeler").
    dependencies=[require_role("modeler")],
)
async def list_schemas(
    request: Request,
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[str]:
    """Return available schemas/datasets from the source connection.

    Bug-7153 / Bug-7156: rerouted through the query-router's
    connection-introspect ``discover-tables`` endpoint (the same path used
    by ``connections.py:discover_tables``), replacing the raw-SQL
    ``/introspect`` path with hand-built connector-specific SQL in
    ``_build_schema_listing_sql``. This closes the second front door to
    source databases and removes the architectural layering violation where
    per-connector SQL lived in model-service routes rather than in
    ``shared/source_introspection.py``.

    Unique schema names are extracted from the discover-tables response.
    """
    from src.api.connections import (
        _extract_bearer,
        _connection_introspect_via_router,
        tables_from_discover_payload,
    )

    bearer = _extract_bearer(request)

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        source = await db.get(DataSource, source_id)
        if source is None or source.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")
        # Bug-5325: fail closed if this source's connection belongs to another
        # project (legacy/imported malformed row). project_id is the source's
        # owning project — ensure_model_in_project confirmed the model lives in
        # it and the source belongs to that model.
        conn = await resolve_source_connection(
            db, source, expected_project_id=project_id
        )
        try:
            payload = await _connection_introspect_via_router(
                "discover-tables",
                {
                    "connection_id": str(conn.id),
                    "project_id": str(project_id),
                },
                bearer,
            )
            tables = tables_from_discover_payload(payload)
            schemas = sorted({
                t["schema"]
                for t in tables
                if isinstance(t, dict)
                and "schema" in t
                and t.get("schema") != "__tessallite__"
            })
            return schemas
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Schema fetch failed: {exc}"
            ) from exc
    return []
