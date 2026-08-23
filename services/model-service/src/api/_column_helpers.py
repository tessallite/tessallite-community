"""
Shared helpers for resolving column names to ModelColumn records.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import ModelColumn, ModelTable
from src.api._scope import ensure_ref_in_model


async def resolve_column(
    db: AsyncSession,
    table_id: UUID,
    column_name: str,
    data_type: str = "unknown",
    *,
    model_id: UUID,
    project_id: UUID,
    field_name: str = "source_table_id",
) -> ModelColumn:
    """Look up a ModelColumn by name in the given table; create if not found.

    ``model_id`` and ``project_id`` are REQUIRED and must both come from the
    URL path.

    Why they are required rather than optional (Bug intake
    ``model-service-resolve-column-unscoped-table-id``): every caller reaches
    this helper with a ``table_id`` taken from a REQUEST BODY, and the create
    branch below INSERTS a ``ModelColumn`` into whatever table it is handed.
    Without a model context a modeller authorised for project A could name
    project B's ``model_tables.id`` and this helper would fabricate a column
    inside that foreign table, then bind their own measure / dimension / join
    to it. ``require_role`` only proves the caller owns the PATH project; it
    never looks at the body. Making the model context a required keyword makes
    the unscoped call unrepresentable — a caller that forgets it gets a
    TypeError at import/call time, not a silent cross-project write.

    The ownership proof is delegated to the canonical body-FK primitive
    (``_scope.ensure_ref_in_model``) rather than hand-rolled here: four
    hand-rolled re-inventions of that check already existed in this service and
    are exactly why the defect kept reappearing. It answers 422 with the
    family's uniform detail, and "no such table anywhere" is indistinguishable
    from "a table in another project" — no existence oracle.

    Pass ``data_type`` when the caller knows the real type (e.g. from schema
    profiling) so the record is stored with accurate metadata.  Existing
    records whose data_type is still ``"unknown"`` are upgraded in-place.
    """
    # Prove the body-supplied table belongs to the path project+model BEFORE
    # any read or write touches it. ``required=True``: reaching this helper at
    # all means the caller intends to resolve a column in a specific table, so
    # a missing table id is a payload error, not an absent optional reference.
    await ensure_ref_in_model(
        db,
        ModelTable,
        ref_id=table_id,
        model_id=model_id,
        project_id=project_id,
        field_name=field_name,
        required=True,
        noun="a table in this model",
        error_code="TABLE_NOT_IN_MODEL",
    )
    result = await db.execute(
        select(ModelColumn).where(
            ModelColumn.model_table_id == table_id,
            ModelColumn.column_name == column_name,
        )
    )
    col = result.scalar_one_or_none()
    if col is not None:
        # Upgrade stale "unknown" entries when the real type is now available
        if col.data_type == "unknown" and data_type != "unknown":
            col.data_type = data_type
            await db.flush()
        return col
    col = ModelColumn(
        model_table_id=table_id,
        column_name=column_name,
        data_type=data_type,
        is_nullable=True,
    )
    db.add(col)
    await db.flush()
    return col
