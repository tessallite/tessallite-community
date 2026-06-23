"""Model documentation auto-generation — ERD diagrams, measure/dimension catalogs."""
from __future__ import annotations

import io
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from shared.db.models import Dimension, Join, Measure, Model, ModelColumn, ModelTable
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, get_current_user
from src.auth.rbac import require_role

router = APIRouter(tags=["model-docs"])


class DocCatalogEntry(BaseModel):
    name: str
    display_name: str
    description: str | None
    data_type: str | None
    table: str | None


class ModelDocResponse(BaseModel):
    model_name: str
    markdown: str
    tables: list[dict]
    dimensions: list[DocCatalogEntry]
    measures: list[DocCatalogEntry]
    joins: list[dict]
    mermaid_erd: str


def _render_markdown(doc: ModelDocResponse) -> str:
    """Render the structured doc response into a Markdown string."""
    lines = [
        f"# {doc.model_name}\n",
        "## Entity Relationship Diagram\n",
        "```mermaid",
        doc.mermaid_erd,
        "```\n",
        "## Tables\n",
        "| Alias | Display Name | Type | Description |",
        "|-------|-------------|------|-------------|",
    ]
    for t in doc.tables:
        lines.append(
            f"| {t['alias']} | {t['display_name']} | {t['table_type']} "
            f"| {t.get('description', '') or ''} |"
        )

    lines.extend([
        "\n## Dimensions\n",
        "| Name | Display Name | Table | Data Type | Description |",
        "|------|-------------|-------|-----------|-------------|",
    ])
    for d in doc.dimensions:
        lines.append(
            f"| {d.name} | {d.display_name} | {d.table or ''} "
            f"| {d.data_type or ''} | {d.description or ''} |"
        )

    lines.extend([
        "\n## Measures\n",
        "| Name | Display Name | Table | Data Type | Description |",
        "|------|-------------|-------|-----------|-------------|",
    ])
    for m in doc.measures:
        lines.append(
            f"| {m.name} | {m.display_name} | {m.table or ''} "
            f"| {m.data_type or ''} | {m.description or ''} |"
        )

    return "\n".join(lines)


@router.get(
    "/projects/{project_id}/models/{model_id}/docs/generate",
    response_model=ModelDocResponse,
    dependencies=[require_role("viewer")],
)
async def generate_model_docs(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> ModelDocResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if not model or model.project_id != project_id:
            raise HTTPException(status_code=404, detail="Model not found")

        tables = (await db.execute(
            select(ModelTable).where(ModelTable.model_id == model_id)
        )).scalars().all()
        dims = (await db.execute(
            select(Dimension).where(Dimension.model_id == model_id)
        )).scalars().all()
        meas = (await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )).scalars().all()
        joins = (await db.execute(
            select(Join).where(Join.model_id == model_id)
        )).scalars().all()

        table_map = {t.id: t for t in tables}

        # Fetch all columns for this model's tables.
        all_columns = (await db.execute(
            select(ModelColumn).where(
                ModelColumn.model_table_id.in_([t.id for t in tables])
            )
        )).scalars().all() if tables else []

        col_to_table: dict[UUID, UUID] = {c.id: c.model_table_id for c in all_columns}
        col_data_type: dict[UUID, str] = {
            c.id: c.data_type for c in all_columns if c.data_type
        }
        # Group columns by table for the ERD.
        table_columns: dict[UUID, list] = {}
        for c in all_columns:
            table_columns.setdefault(c.model_table_id, []).append(c)

        lines = ["erDiagram"]
        for t in tables:
            alias = t.alias or t.physical_name
            lines.append(f"    {alias} {{")
            for col in table_columns.get(t.id, []):
                dtype = (col.data_type or "string").split("(")[0].strip()
                lines.append(f"        {dtype} {col.column_name}")
            lines.append("    }")
        for j in joins:
            lt = table_map.get(j.left_table_id)
            rt = table_map.get(j.right_table_id)
            if lt and rt:
                la = lt.alias or lt.physical_name
                ra = rt.alias or rt.physical_name
                lines.append(f"    {la} ||--o{{ {ra} : \"{j.join_type}\"")
        mermaid = "\n".join(lines)

        dim_entries = []
        for d in dims:
            table_id = col_to_table.get(d.source_column_id) if d.source_column_id else None
            tbl = table_map.get(table_id) if table_id else None
            dtype = col_data_type.get(d.source_column_id) if d.source_column_id else None
            dim_entries.append(DocCatalogEntry(
                name=d.name,
                display_name=d.display_name or d.name,
                description=d.description,
                data_type=dtype,
                table=(tbl.alias or tbl.physical_name) if tbl else None,
            ))

        meas_entries = []
        for m in meas:
            table_id = col_to_table.get(m.source_column_id) if m.source_column_id else None
            tbl = table_map.get(table_id) if table_id else None
            meas_entries.append(DocCatalogEntry(
                name=m.name,
                display_name=m.display_name or m.name,
                description=m.description,
                data_type=m.data_type,
                table=(tbl.alias or tbl.physical_name) if tbl else None,
            ))

        doc = ModelDocResponse(
            model_name=model.display_name or model.slug,
            markdown="",
            tables=[
                {
                    "id": str(t.id),
                    "alias": t.alias,
                    "display_name": t.display_name,
                    "physical_name": t.physical_name,
                    "table_type": t.table_type,
                    "description": t.description,
                }
                for t in tables
            ],
            dimensions=dim_entries,
            measures=meas_entries,
            joins=[
                {
                    "id": str(j.id),
                    "left_table": (table_map.get(j.left_table_id).alias if table_map.get(j.left_table_id) else None),
                    "right_table": (table_map.get(j.right_table_id).alias if table_map.get(j.right_table_id) else None),
                    "join_type": j.join_type,
                }
                for j in joins
            ],
            mermaid_erd=mermaid,
        )
        doc.markdown = _render_markdown(doc)
        return doc
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get(
    "/projects/{project_id}/models/{model_id}/docs/markdown",
    dependencies=[require_role("viewer")],
)
async def export_model_docs_markdown(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
):
    """Generate a Markdown document with model catalog and ERD."""
    doc = await generate_model_docs(project_id, model_id, current_user=current_user)
    md = doc.markdown
    return StreamingResponse(
        io.BytesIO(md.encode("utf-8")),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{doc.model_name}_catalog.md"'},
    )
