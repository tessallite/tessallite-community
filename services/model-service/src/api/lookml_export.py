"""Export a deployed semantic model as a generated LookML project archive."""
from __future__ import annotations

import io
import zipfile
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from scripts.lookml_export.emitter import EmissionError, emit_project
from scripts.lookml_export.snapshot import ModelSnapshot
from shared.db.models import ModelVersion
from shared.db.session import get_tenant_db
from src.api.import_export import _ensure_model_access
from src.auth.middleware import CurrentUser, forbid_embed_user

router = APIRouter(tags=["lookml-export"])


class LookMLExportRequest(BaseModel):
    connection: str = Field(min_length=1, max_length=255)

    @field_validator("connection")
    @classmethod
    def validate_connection(cls, value: str) -> str:
        connection = value.strip()
        if not connection:
            raise ValueError("connection must not be blank")
        if "\r" in connection or "\n" in connection:
            raise ValueError("connection must be a single line")
        return connection


@router.post("/projects/{project_id}/models/{model_id}/export/lookml")
async def export_model_lookml(
    project_id: UUID,
    model_id: UUID,
    body: LookMLExportRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> StreamingResponse:
    """Generate LookML from the current deployed snapshot and return a ZIP file."""
    archive: bytes | None = None
    filename: str | None = None
    model_hash: str | None = None
    warning_count = 0

    async for tenant_db in get_tenant_db(current_user.tenant_id):
        model = await _ensure_model_access(project_id, model_id, current_user, tenant_db)
        if model.deployed_version_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Deploy the model before exporting LookML.",
            )
        version = await tenant_db.get(ModelVersion, model.deployed_version_id)
        if version is None or version.model_id != model_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The deployed model version could not be found.",
            )
        snapshot = dict(version.snapshot_json)
        snapshot["exported_deployed_version_id"] = str(model.deployed_version_id)
        source = ModelSnapshot(
            project_key=str(project_id),
            project_name=model.slug,
            model_key=str(model_id),
            model_slug=model.slug,
            model_display_name=model.display_name,
            snapshot=snapshot,
        )
        try:
            emitted = emit_project(source, connection=body.connection)
        except EmissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"LookML export cannot represent this model: {exc}",
            ) from exc

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path, content in sorted(emitted.files.items()):
                output.writestr(path, content)
        archive = buffer.getvalue()
        filename = f"{model.slug}-lookml.zip"
        model_hash = emitted.model_hash
        warning_count = len(emitted.warnings)

    assert archive is not None and filename is not None and model_hash is not None
    return StreamingResponse(
        io.BytesIO(archive),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Tessallite-Model-SHA256": model_hash,
            "X-Tessallite-LookML-Warnings": str(warning_count),
        },
    )
