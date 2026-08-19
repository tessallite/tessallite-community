"""Dimension detail-attribute (bijection) lifecycle logic.

Handles:
  - Advisory validate endpoint (1:1 pre-check, supports both dim-table and
    fact-table details).
  - Auto-add dimension list entries for bijection details, with provenance.
  - Symmetric create/remove sync across the fact-dimension pair.
  - Referential lock (refuse delete of auto-added dimension while active detail).
  - Cascade delete: downstream usage check + aggregate-retire trigger.

All source DB access goes through ``shared/source_executor`` +
``shared/connector_qualify``. No direct DB access, no connector branches.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AggregateDefinition,
    DataSource,
    Dimension,
    DimensionAttributeRelationship,
    Join,
    ModelColumn,
    ModelTable,
    Persona,
    ProjectConnection,
)
from shared.semantic.attribute_relationship_hash import compute_declaration_hash
from shared.semantic.attribute_relationship_verifier import (
    RelationColumns,
    build_forward_check_sql,
    build_null_check_sql,
    build_reverse_check_sql,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table/connection resolution helpers
# ---------------------------------------------------------------------------

async def _resolve_table_connection(
    db: AsyncSession,
    table: ModelTable,
) -> tuple[DataSource, ProjectConnection]:
    """Resolve a table's DataSource and ProjectConnection."""
    source = await db.get(DataSource, table.source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Data source not found for table {table.alias}.",
        )
    conn = await db.get(ProjectConnection, source.project_connection_id)
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Source connection not found.",
        )
    return source, conn


async def _resolve_dim_source_table(
    db: AsyncSession,
    dim: Dimension,
) -> tuple[ModelTable, ModelColumn, DataSource, ProjectConnection]:
    """Resolve the dimension's source table, key column, data source, and connection."""
    if dim.source_column_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Validation requires a dimension backed by a physical key column.",
        )
    key_col = await db.get(ModelColumn, dim.source_column_id)
    if key_col is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Dimension key column not found.",
        )
    table = await db.get(ModelTable, key_col.model_table_id)
    if table is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Dimension source table not found.",
        )
    source, conn = await _resolve_table_connection(db, table)
    return table, key_col, source, conn


async def _find_join_for_dimension(
    db: AsyncSession,
    model_id: UUID,
    dim_table_id: UUID,
) -> tuple[Join | None, UUID | None]:
    """Find the Join connecting the dimension's table to the fact table.

    Returns (join, fact_table_id) or (None, None) if no join found.
    """
    joins_result = await db.execute(
        select(Join).where(Join.model_id == model_id)
    )
    for j in joins_result.scalars().all():
        if j.left_table_id == dim_table_id:
            return j, j.right_table_id
        if j.right_table_id == dim_table_id:
            return j, j.left_table_id
    return None, None


# ---------------------------------------------------------------------------
# Advisory validate endpoint logic
# ---------------------------------------------------------------------------

async def validate_detail_columns(
    db: AsyncSession,
    dim: Dimension,
    detail_specs: list[tuple[str, UUID]],
    model_id: UUID,
) -> list[dict[str, Any]]:
    """Run advisory 1:1 checks for each detail column.

    detail_specs: list of (column_name, table_id) pairs.

    For dimension-table details: check key <-> detail on the dim table (cheap).
    For fact-table details: check fk <-> detail on the fact table (bounded).

    Returns per-column: {column, table_id, is_bijection, reason, error?}.
    Uses the governed source executor and verifier SQL builders.
    """
    from shared.source_executor import execute_source_sql, resolve_connector_type
    from src.api._table_qualify import qualify_physical_name

    dim_table, key_col, dim_source, dim_conn = await _resolve_dim_source_table(db, dim)
    dim_connector = await resolve_connector_type(dim_conn)

    # Find the join and fact table for fact-side detail resolution.
    join_obj, fact_table_id = await _find_join_for_dimension(
        db, model_id, dim_table.id,
    )
    fact_table: ModelTable | None = None
    fact_fk_col: ModelColumn | None = None
    fact_connector: str | None = None
    fact_conn: ProjectConnection | None = None
    fact_source: DataSource | None = None
    if fact_table_id is not None and join_obj is not None:
        fact_table = await db.get(ModelTable, fact_table_id)
        if fact_table is not None:
            fact_source, fact_conn = await _resolve_table_connection(db, fact_table)
            fact_connector = await resolve_connector_type(fact_conn)
            # The FK column on the fact table (the join column).
            if join_obj.left_table_id == dim_table.id:
                fact_fk_col = await db.get(ModelColumn, join_obj.right_column_id)
            else:
                fact_fk_col = await db.get(ModelColumn, join_obj.left_column_id)

    results: list[dict[str, Any]] = []
    for col_name, table_id in detail_specs:
        # Resolve the detail column on the specified table.
        col_result = await db.execute(
            select(ModelColumn).where(
                ModelColumn.model_table_id == table_id,
                ModelColumn.column_name == col_name,
            )
        )
        col = col_result.scalars().first()
        if col is None:
            results.append({
                "column": col_name,
                "table_id": str(table_id),
                "is_bijection": False,
                "reason": "error",
                "error": f"Column '{col_name}' not found in the specified table.",
            })
            continue

        is_dim_table = (table_id == dim_table.id)

        if is_dim_table:
            # Dim-table detail: check key <-> detail on the dim table.
            if col.id == key_col.id:
                results.append({
                    "column": col_name,
                    "table_id": str(table_id),
                    "is_bijection": False,
                    "reason": "error",
                    "error": "Detail column must differ from the key column.",
                })
                continue
            table_ref = qualify_physical_name(dim_table.physical_name, dim_conn, dim_source)
            check_cols = RelationColumns(
                table_ref=table_ref,
                key_physical=key_col.column_name,
                detail_physical=col.column_name,
                detail_type=(col.data_type or "").upper(),
                key_type=(key_col.data_type or "").upper(),
            )
            connector = dim_connector
            conn_obj = dim_conn
        else:
            # Fact-table detail: check fk <-> detail on the fact table.
            if fact_table is None or fact_fk_col is None or fact_conn is None or fact_source is None:
                results.append({
                    "column": col_name,
                    "table_id": str(table_id),
                    "is_bijection": False,
                    "reason": "error",
                    "error": "Could not resolve the fact table or join for this dimension.",
                })
                continue
            if col.id == fact_fk_col.id:
                results.append({
                    "column": col_name,
                    "table_id": str(table_id),
                    "is_bijection": False,
                    "reason": "error",
                    "error": "Detail column must differ from the join (FK) column.",
                })
                continue
            table_ref = qualify_physical_name(fact_table.physical_name, fact_conn, fact_source)
            check_cols = RelationColumns(
                table_ref=table_ref,
                key_physical=fact_fk_col.column_name,
                detail_physical=col.column_name,
                detail_type=(col.data_type or "").upper(),
                key_type=(fact_fk_col.data_type or "").upper(),
            )
            connector = fact_connector
            conn_obj = fact_conn

        try:
            # NULL check
            null_sql = build_null_check_sql(check_cols, connector)
            null_rows, _ = await execute_source_sql(
                conn_obj, null_sql, tenant_session=db,
            )
            if null_rows:
                results.append({
                    "column": col_name,
                    "table_id": str(table_id),
                    "is_bijection": False,
                    "reason": "null_endpoint",
                })
                continue

            # Forward check (key -> detail)
            fwd_sql = build_forward_check_sql(check_cols, connector)
            fwd_rows, _ = await execute_source_sql(
                conn_obj, fwd_sql, tenant_session=db,
            )
            if fwd_rows:
                results.append({
                    "column": col_name,
                    "table_id": str(table_id),
                    "is_bijection": False,
                    "reason": "forward_violation",
                })
                continue

            # Reverse check (detail -> key, bijection only)
            rev_sql = build_reverse_check_sql(check_cols, connector)
            rev_rows, _ = await execute_source_sql(
                conn_obj, rev_sql, tenant_session=db,
            )
            if rev_rows:
                results.append({
                    "column": col_name,
                    "table_id": str(table_id),
                    "is_bijection": False,
                    "reason": "reverse_violation",
                })
                continue

            results.append({
                "column": col_name,
                "table_id": str(table_id),
                "is_bijection": True,
                "reason": "ok",
            })
        except Exception as exc:
            logger.warning(
                "Advisory validate for column %s on table %s failed: %s",
                col_name, table_id, exc,
            )
            results.append({
                "column": col_name,
                "table_id": str(table_id),
                "is_bijection": False,
                "reason": "error",
                "error": str(exc)[:200],
            })
    return results


# ---------------------------------------------------------------------------
# Auto-add dimension for bijection detail (provenance)
# ---------------------------------------------------------------------------

async def auto_add_detail_dimension(
    db: AsyncSession,
    *,
    model_id: UUID,
    owning_dimension: Dimension,
    relationship: DimensionAttributeRelationship,
    detail_column: ModelColumn,
) -> Dimension | None:
    """Auto-create a Dimension list entry for a bijection detail column if missing.

    Sets provenance fields so the dimension is identified as "detail of [X]".
    Returns the created/existing dimension, or None on error.
    """
    # Check if a dimension already exists for this detail column.
    existing_result = await db.execute(
        select(Dimension).where(
            Dimension.model_id == model_id,
            Dimension.source_column_id == detail_column.id,
        )
    )
    existing = existing_result.scalars().first()
    if existing is not None:
        # A pre-existing dimension for this column already exists. Do NOT
        # stamp provenance on it -- it was independently created and should
        # not become cascade-deletable (F-5: provenance must only be set on
        # dimensions this feature creates, not pre-existing ones).
        return existing

    # Auto-create.
    dim_name = detail_column.column_name
    # Ensure uniqueness by appending a suffix if needed.
    name_result = await db.execute(
        select(Dimension.name).where(
            Dimension.model_id == model_id,
            Dimension.name == dim_name,
        )
    )
    if name_result.first() is not None:
        dim_name = f"{dim_name}_detail"

    new_dim = Dimension(
        model_id=model_id,
        name=dim_name,
        display_name=detail_column.display_name or detail_column.column_name.replace("_", " ").title(),
        source_column_id=detail_column.id,
        is_time_dim=False,
        detail_of_relationship_id=relationship.id,
        detail_of_dimension_id=owning_dimension.id,
    )
    db.add(new_dim)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        # Savepoint rolled back; outer transaction intact.
        return None
    return new_dim


# ---------------------------------------------------------------------------
# Symmetric pair sync (across the fact-dimension Join)
# ---------------------------------------------------------------------------

async def _find_paired_dimension(
    db: AsyncSession,
    *,
    model_id: UUID,
    dim: Dimension,
) -> Dimension | None:
    """Find the dimension on the other side of the fact-dimension Join pair.

    Looks up the Join that connects this dimension's source table to the fact
    table, then finds a dimension on the fact table that shares the join column.
    Returns None if no paired dimension exists.
    """
    if dim.source_column_id is None:
        return None
    key_col = await db.get(ModelColumn, dim.source_column_id)
    if key_col is None:
        return None
    dim_table = await db.get(ModelTable, key_col.model_table_id)
    if dim_table is None:
        return None

    # Find joins connecting this dimension's table to another table.
    joins_result = await db.execute(
        select(Join).where(Join.model_id == model_id)
    )
    joins = joins_result.scalars().all()

    for j in joins:
        if j.left_table_id == dim_table.id:
            other_join_col_id = j.right_column_id
        elif j.right_table_id == dim_table.id:
            other_join_col_id = j.left_column_id
        else:
            continue

        paired_result = await db.execute(
            select(Dimension).where(
                Dimension.model_id == model_id,
                Dimension.source_column_id == other_join_col_id,
            )
        )
        paired = paired_result.scalars().first()
        if paired is not None and paired.id != dim.id:
            return paired

    return None


async def sync_detail_to_pair(
    db: AsyncSession,
    *,
    model_id: UUID,
    owning_dimension: Dimension,
    relationship: DimensionAttributeRelationship,
    detail_column: ModelColumn,
) -> None:
    """Symmetric create: if the owning dimension has a paired dimension across a
    Join, also create the relationship and auto-added dimension on the other side."""
    paired = await _find_paired_dimension(
        db, model_id=model_id, dim=owning_dimension,
    )
    if paired is None:
        return

    # Check if the paired dimension already has a relationship for this detail column.
    existing_result = await db.execute(
        select(DimensionAttributeRelationship).where(
            DimensionAttributeRelationship.dimension_id == paired.id,
            DimensionAttributeRelationship.detail_column_id == detail_column.id,
            DimensionAttributeRelationship.cardinality == "BIJECTION",
        )
    )
    if existing_result.scalars().first() is not None:
        return  # Already synced.

    paired_key_col_id = paired.source_column_id
    if paired_key_col_id is None:
        return

    # Detect cross-table relationship (key and detail on different tables).
    # The v1 deploy verifier requires same-table key/detail, so cross-table
    # rels are created disabled to prevent ERROR evidence noise on every deploy.
    paired_key_col = await db.get(ModelColumn, paired_key_col_id)
    is_cross_table = (
        paired_key_col is not None
        and paired_key_col.model_table_id != detail_column.model_table_id
    )

    declaration_hash = compute_declaration_hash(
        key_column_id=str(paired_key_col_id),
        detail_column_id=str(detail_column.id),
        cardinality="BIJECTION",
        null_policy="REJECT_NULL",
    )
    paired_rel = DimensionAttributeRelationship(
        model_id=model_id,
        dimension_id=paired.id,
        key_column_id=paired_key_col_id,
        detail_column_id=detail_column.id,
        cardinality="BIJECTION",
        null_policy="REJECT_NULL",
        enabled=False if is_cross_table else relationship.enabled,
        declaration_hash=declaration_hash,
    )
    db.add(paired_rel)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        # Savepoint rolled back; outer transaction intact.
        return

    # Auto-add dimension for the paired side too.
    await auto_add_detail_dimension(
        db,
        model_id=model_id,
        owning_dimension=paired,
        relationship=paired_rel,
        detail_column=detail_column,
    )


# ---------------------------------------------------------------------------
# Referential lock: refuse dimension delete if it is an active detail
# ---------------------------------------------------------------------------

async def check_detail_provenance_lock(
    db: AsyncSession,
    dim: Dimension,
) -> None:
    """Raise HTTP 409 if the dimension is an auto-added detail that is still active.

    The modeller must unselect it as a detail attribute first.
    """
    if getattr(dim, "detail_of_relationship_id", None) is not None:
        rel = await db.get(
            DimensionAttributeRelationship, dim.detail_of_relationship_id
        )
        if rel is not None:
            owning_dim = await db.get(Dimension, dim.detail_of_dimension_id) if dim.detail_of_dimension_id else None
            owning_name = owning_dim.name if owning_dim else "another dimension"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This dimension is a detail attribute of '{owning_name}'. "
                    f"Remove it as a detail attribute first, then delete."
                ),
            )


# ---------------------------------------------------------------------------
# Shared dimension-delete cleanup (reuses the existing delete lifecycle)
# ---------------------------------------------------------------------------

async def _cleanup_and_delete_dimension(
    db: AsyncSession,
    dim: Dimension,
    model_id: UUID,
) -> None:
    """Delete a dimension with proper lifecycle cleanup.

    Mirrors the cleanup in the dimension delete endpoint: persona reference
    cleanup, default_filter key removal, soft-reference purge, then delete.
    """
    from src.api.personas import strip_id_from_personas
    from src.api._scope import purge_entity_soft_references

    # Clear provenance so this dim is no longer locked.
    dim.detail_of_relationship_id = None
    dim.detail_of_dimension_id = None

    await strip_id_from_personas(
        db, model_id=model_id, object_id=dim.id, object_class="dimension"
    )
    # Bug-5607: clean stale keys from persona default_filters.
    dim_name = dim.name
    persona_result = await db.execute(
        select(Persona).where(Persona.model_id == model_id)
    )
    for persona in persona_result.scalars().all():
        df = persona.default_filters
        if isinstance(df, dict) and dim_name in df:
            updated = {k: v for k, v in df.items() if k != dim_name}
            persona.default_filters = updated
    await purge_entity_soft_references(db, model_id=model_id, entity_id=dim.id)
    await db.delete(dim)


# ---------------------------------------------------------------------------
# Cascade delete: downstream usage + aggregate retirement
# ---------------------------------------------------------------------------

async def compute_relationship_downstream_usage(
    db: AsyncSession,
    relationship: DimensionAttributeRelationship,
    model_id: UUID,
) -> dict[str, Any]:
    """Compute downstream usage for a relationship's auto-added dimensions.

    Computes over the whole detail-column family (all relationships sharing
    the same detail_column_id, including symmetric pairs) to give an accurate
    preview from either side of the pair.
    """
    # Find ALL relationship IDs in the same detail-column family.
    family_rel_ids = [relationship.id]
    if relationship.detail_column_id is not None:
        family_result = await db.execute(
            select(DimensionAttributeRelationship.id).where(
                DimensionAttributeRelationship.model_id == model_id,
                DimensionAttributeRelationship.detail_column_id == relationship.detail_column_id,
                DimensionAttributeRelationship.cardinality == "BIJECTION",
            )
        )
        family_rel_ids = [r[0] for r in family_result.all()]

    linked_dims_result = await db.execute(
        select(Dimension).where(
            Dimension.model_id == model_id,
            Dimension.detail_of_relationship_id.in_(family_rel_ids),
        )
    )
    linked_dims = linked_dims_result.scalars().all()
    dim_names = [d.name for d in linked_dims]

    # Find aggregates that include any of these dimensions in their grain.
    affected_aggregates: list[dict[str, Any]] = []
    if dim_names:
        agg_result = await db.execute(
            select(AggregateDefinition).where(
                AggregateDefinition.model_id == model_id,
                AggregateDefinition.status != "retired",
            )
        )
        for agg in agg_result.scalars().all():
            grain = agg.grain or []
            if any(dn in grain for dn in dim_names):
                affected_aggregates.append({
                    "id": str(agg.id),
                    "physical_table_name": agg.physical_table_name,
                    "grain": grain,
                    "status": agg.status,
                })

    return {
        "linked_dimensions": [{"id": str(d.id), "name": d.name} for d in linked_dims],
        "affected_aggregates": affected_aggregates,
    }


async def cascade_delete_relationship(
    db: AsyncSession,
    relationship: DimensionAttributeRelationship,
    model_id: UUID,
    *,
    retire_aggregates: bool = False,
) -> dict[str, Any]:
    """Delete a relationship and cascade-remove its auto-added dimensions.

    If retire_aggregates is True, also retire affected aggregates.
    Returns summary of what was removed/retired.
    """
    usage = await compute_relationship_downstream_usage(db, relationship, model_id)

    # Retire aggregates if requested using the existing retirement mechanism.
    retired_aggs: list[str] = []
    if retire_aggregates and usage["affected_aggregates"]:
        from datetime import datetime, timezone

        for agg_info in usage["affected_aggregates"]:
            try:
                agg = await db.get(AggregateDefinition, UUID(agg_info["id"]))
                # Bug-7903 (Fable R3 #2): do not retire an aggregate mid-refresh.
                # While the refresh pending-guard holds it "pending"/"invalid" and
                # durably rebuilds the physical table under the per-aggregate refresh
                # lock, flipping it "retired" here would race the refresh's success
                # restore (which could resurrect it) and, on a refresh failure, the
                # aggregate would be perpetually re-swept. Skip it; the relationship
                # is being deleted, so the aggregate stops matching regardless, and a
                # subsequent explicit retire (once it settles) reclaims it.
                if (
                    agg is not None
                    and agg.status not in ("retired", "pending", "invalid")
                ):
                    agg.status = "retired"
                    agg.retired_at = datetime.now(timezone.utc)
                    retired_aggs.append(agg_info["physical_table_name"])
            except Exception as exc:
                logger.warning(
                    "Failed to retire aggregate %s during relationship cascade: %s",
                    agg_info["id"], exc,
                )

    # Remove auto-added dimensions linked to this relationship (both sides),
    # using the shared dimension-delete cleanup helpers.
    removed_dims: list[str] = []
    for dim_info in usage["linked_dimensions"]:
        dim = await db.get(Dimension, UUID(dim_info["id"]))
        if dim is not None:
            removed_dims.append(dim.name)
            await _cleanup_and_delete_dimension(db, dim, model_id)

    # Also find and remove symmetric paired relationships.
    if relationship.detail_column_id is not None:
        paired_rels_result = await db.execute(
            select(DimensionAttributeRelationship).where(
                DimensionAttributeRelationship.model_id == model_id,
                DimensionAttributeRelationship.detail_column_id == relationship.detail_column_id,
                DimensionAttributeRelationship.cardinality == "BIJECTION",
                DimensionAttributeRelationship.id != relationship.id,
            )
        )
        for paired_rel in paired_rels_result.scalars().all():
            paired_dims_result = await db.execute(
                select(Dimension).where(
                    Dimension.model_id == model_id,
                    Dimension.detail_of_relationship_id == paired_rel.id,
                )
            )
            for pd in paired_dims_result.scalars().all():
                removed_dims.append(pd.name)
                await _cleanup_and_delete_dimension(db, pd, model_id)
            await db.delete(paired_rel)

    # Delete the relationship itself.
    await db.delete(relationship)

    return {
        "removed_dimensions": removed_dims,
        "retired_aggregates": retired_aggs,
    }
