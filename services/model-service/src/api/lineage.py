"""
Lineage graph routes.

Returns nodes + edges for the ReactFlow canvas in the frontend.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from shared.db.models import (
    AggregateDefinition,
    DataSource,
    DataTarget,
    DownstreamAsset,
    LineageMapping,
    Model,
    ModelColumn,
    ModelTable,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import LineageEdge, LineageGraphResponse, LineageNode
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/lineage", tags=["lineage"]
)


def _config_location(config: dict | None) -> str:
    """Pick the most relevant location label from a connection config."""
    if not config:
        return ""
    for key in ("schema", "dataset", "database", "host"):
        value = config.get(key)
        if value:
            return str(value)
    return ""


@router.get("", response_model=LineageGraphResponse, dependencies=[require_role("viewer")])
async def get_lineage_graph(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> LineageGraphResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")

        sources_result = await db.execute(
            select(DataSource).where(DataSource.model_id == model_id)
        )
        sources = sources_result.scalars().all()

        targets_result = await db.execute(
            select(DataTarget).where(DataTarget.model_id == model_id)
        )
        targets = targets_result.scalars().all()

        aggs_result = await db.execute(
            select(AggregateDefinition).where(
                AggregateDefinition.model_id == model_id
            )
        )
        aggs = aggs_result.scalars().all()

        lineage_result = await db.execute(
            select(LineageMapping).where(LineageMapping.model_id == model_id)
        )
        lineage_rows = lineage_result.scalars().all()
        source_column_ids = sorted(
            {row.source_column_id for row in lineage_rows if row.source_column_id},
            key=str,
        )

        # Per-source table count for the source tooltip
        table_counts_result = await db.execute(
            select(ModelTable.source_id, func.count(ModelTable.id))
            .where(ModelTable.model_id == model_id)
            .group_by(ModelTable.source_id)
        )
        table_counts: dict[UUID, int] = {row[0]: row[1] for row in table_counts_result.all()}

        asset_count_result = await db.execute(
            select(func.count(DownstreamAsset.id))
            .where(DownstreamAsset.model_id == model_id)
        )
        downstream_asset_count: int = asset_count_result.scalar() or 0
        source_column_rows: dict[UUID, tuple[ModelColumn, ModelTable]] = {}
        if source_column_ids:
            source_columns_result = await db.execute(
                select(ModelColumn, ModelTable)
                .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
                .where(
                    ModelColumn.id.in_(source_column_ids),
                    ModelTable.model_id == model_id,
                )
            )
            source_column_rows = {
                column.id: (column, table)
                for column, table in source_columns_result.all()
            }

        nodes: list[LineageNode] = []
        edges: list[LineageEdge] = []

        # Model node (rendered as "semantic" in the frontend graph)
        nodes.append(
            LineageNode(
                id=str(model_id),
                type="semantic",
                label=model.display_name or model.slug,
                description="Semantic model — single source of truth for dimensions and measures.",
                meta={
                    "Slug": model.slug or "",
                    "Status": getattr(model, "status", "") or "",
                    "Sources": str(len(sources)),
                    "Aggregates": str(len(aggs)),
                },
                downstream_asset_count=downstream_asset_count,
            )
        )

        # Source nodes + edges to model
        for src in sources:
            nid = f"source:{src.id}"
            location = _config_location(src.config)
            nodes.append(
                LineageNode(
                    id=nid,
                    type="source",
                    label=src.display_name,
                    description="Upstream data source feeding the semantic model.",
                    meta={
                        "Type": src.source_type,
                        "Location": location,
                        "Tables": str(table_counts.get(src.id, 0)),
                    },
                )
            )
            edges.append(
                LineageEdge(source=nid, target=str(model_id), label="feeds")
            )

        # Target nodes — destination connection where aggregates are materialised
        for tgt in targets:
            nid = f"target:{tgt.id}"
            nodes.append(
                LineageNode(
                    id=nid,
                    type="target",
                    label=tgt.display_name,
                    description="Destination connection where aggregate tables are written.",
                    meta={
                        "Type": tgt.target_type,
                        "Location": _config_location(tgt.config),
                    },
                )
            )

        # Aggregate nodes + edges from model and to target
        for agg in aggs:
            nid = f"agg:{agg.id}"
            grain_label = ", ".join(agg.grain) if agg.grain else "—"
            agg_meta: dict[str, str] = {
                "Grain": grain_label,
                "Status": agg.status,
                "Generator": agg.creation_reason,
            }
            if agg.last_refreshed_at:
                agg_meta["Last refresh"] = agg.last_refreshed_at.isoformat()
            if agg.estimated_hit_rate is not None:
                agg_meta["Hit rate"] = f"{agg.estimated_hit_rate * 100:.0f}%"
            if agg.is_stale:
                agg_meta["Stale"] = "yes"

            nodes.append(
                LineageNode(
                    id=nid,
                    type="aggregate",
                    label=agg.physical_table_name,
                    description="Pre-computed aggregate table accelerating model queries.",
                    meta=agg_meta,
                    creation_reason=agg.creation_reason,
                    status=agg.status,
                    last_refreshed_at=agg.last_refreshed_at,
                )
            )
            edges.append(
                LineageEdge(source=str(model_id), target=nid, label="produces")
            )
            # Wire each aggregate to its destination target
            edges.append(
                LineageEdge(
                    source=nid,
                    target=f"target:{agg.target_id}",
                    label="writes to",
                )
            )

        # Lineage mappings: source column → semantic field → model.
        emitted_columns: set[UUID] = set()
        emitted_fields: set[str] = set()
        for row in lineage_rows:
            if row.source_column_id:
                column_table = source_column_rows.get(row.source_column_id)
                if column_table and row.source_column_id not in emitted_columns:
                    column, table = column_table
                    emitted_columns.add(row.source_column_id)
                    nodes.append(
                        LineageNode(
                            id=f"col:{column.id}",
                            type="column",
                            label=column.display_name or column.column_name,
                            description="Source column feeding semantic fields.",
                            meta={
                                "Table": table.display_name or table.alias or table.physical_name,
                                "Column": column.column_name,
                                "Data type": column.data_type,
                                "Hidden": "yes" if column.is_hidden else "no",
                            },
                        )
                    )
                field_id = f"field:{row.semantic_field_type}:{row.semantic_field_name}"
                if field_id not in emitted_fields:
                    emitted_fields.add(field_id)
                    nodes.append(
                        LineageNode(
                            id=field_id,
                            type="field",
                            label=row.semantic_field_name,
                            description="Semantic field exposed by the model.",
                            meta={
                                "Field type": row.semantic_field_type,
                            },
                        )
                    )
                    edges.append(
                        LineageEdge(
                            source=field_id,
                            target=str(model_id),
                            label="defined by",
                        )
                    )
                edges.append(
                    LineageEdge(
                        source=f"col:{row.source_column_id}",
                        target=field_id,
                        label="feeds",
                    )
                )

        return LineageGraphResponse(nodes=nodes, edges=edges)
