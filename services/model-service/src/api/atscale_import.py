"""AtScale SML import endpoint.

POST /projects/{project_id}/import/atscale — accepts AtScale SML zip,
parses SML objects and persists them into the target project.
"""
from __future__ import annotations

import io
import logging
import uuid as _uuid
import zipfile
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from shared.auth.middleware import CurrentUser, require_tenant_admin
from shared.db.models import Model, ProjectConnection
from shared.db.session import get_tenant_db
from shared.importers.atscale_mapper import MapResult, map_atscale_to_tessallite
from shared.importers.atscale_parser import SmlParseError, parse_sml_project
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.model_snapshot.rehydrator import rehydrate_into_live
from shared.model_snapshot.slug_utils import insert_model_with_slug_retry
from sqlalchemy import select

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


class AtScaleImportResponse(BaseModel):
    models_parsed: int
    models_created: int
    model_names: list[str]
    warnings: list[str]
    bundle: dict[str, Any]


@router.post(
    "/projects/{project_id}/import/atscale",
    response_model=AtScaleImportResponse,
    tags=["atscale-import"],
)
async def import_atscale_sml(
    project_id: UUID,
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(require_tenant_admin),
):
    chunks = []
    total = 0
    while True:
        chunk = await file.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Upload exceeds 50 MB limit")
        chunks.append(chunk)
    content = b"".join(chunks)
    filename = file.filename or ""

    if not filename.endswith(".zip"):
        raise HTTPException(
            status_code=400,
            detail="Upload must be a .zip file containing an AtScale SML project directory",
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        uncompressed_total = sum(info.file_size for info in zf.infolist())
        if uncompressed_total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Uncompressed zip contents exceed 50 MB limit")
        files: dict[str, str] = {}
        for name in zf.namelist():
            if ".." in name or name.startswith("/"):
                continue
            # Skip __MACOSX artefacts and combined/ convenience aggregations
            # which duplicate objects already present in individual files.
            parts = name.replace("\\", "/").split("/")
            if any(p in ("__MACOSX", "combined") for p in parts):
                continue
            if name.endswith((".yml", ".yaml")):
                files[name] = zf.read(name).decode("utf-8")
        if not files:
            raise HTTPException(
                status_code=400,
                detail="No YAML files found in zip",
            )
        parsed = parse_sml_project(files)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")
    except SmlParseError as exc:
        raise HTTPException(
            status_code=422,
            detail={"errors": exc.errors},
        )

    result: MapResult = map_atscale_to_tessallite(parsed)

    model_names = [
        m.get("model", {}).get("slug", "")
        for m in result.bundle.get("models", [])
    ]

    models_created = 0
    async for db in get_tenant_db(current_user.tenant_id):
        from shared.db.models import Project
        project = await db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        # Pick the first project connection to bind placeholder data sources.
        # If the project has no connections, create a placeholder.
        conn_q = await db.execute(
            select(ProjectConnection.id)
            .where(ProjectConnection.project_id == project_id)
            .limit(1)
        )
        conn_row = conn_q.first()
        if conn_row is not None:
            default_conn_id = str(conn_row[0])
        else:
            from shared.security.credential_crypto import encrypt_json
            placeholder_conn = ProjectConnection(
                project_id=project_id,
                display_name="(imported — configure me)",
                connection_type="postgresql",
                encrypted_credentials=encrypt_json({}),
                config={},
            )
            db.add(placeholder_conn)
            await db.flush()
            default_conn_id = str(placeholder_conn.id)
            result.warnings.append(
                "No project connection found — created a placeholder. "
                "Configure it with real credentials before querying."
            )

        existing_q = await db.execute(
            select(Model.slug).where(Model.project_id == project_id)
        )
        existing_slugs = {r[0] for r in existing_q.all()}

        for model_snap in result.bundle.get("models", []):
            snap_model = model_snap.get("model", {})
            slug = snap_model.get("slug", "")

            # Inject project_connection_id into placeholder data sources.
            for ds in model_snap.get("data_sources", []):
                if not ds.get("project_connection_id"):
                    ds["project_connection_id"] = default_conn_id

            new_model_id = _uuid.uuid4()
            rewritten, missing = prepare_snapshot_for_import(
                model_snap, new_model_id=new_model_id,
            )
            rewritten.setdefault("model", {})
            if snap_model.get("display_name"):
                rewritten["model"]["display_name"] = snap_model["display_name"]

            # F-020-19/22: headroom-safe collision suffixing + race-safe insert.
            new_model, candidate = await insert_model_with_slug_retry(
                db,
                project_id=project_id,
                base_slug=slug,
                existing_slugs=existing_slugs,
                display_name=snap_model.get("display_name") or slug,
                new_model_id=new_model_id,
            )
            rewritten["model"]["slug"] = candidate

            await rehydrate_into_live(
                new_model_id, rewritten, db,
                drop_orphan_aggregates=False,
                actor=current_user.email or current_user.user_id,
                force_aggregate_pending=True,
                force_pocket_stale=True,
                preserve_destination_seed=True,
            )
            models_created += 1

        await db.commit()

        return AtScaleImportResponse(
            models_parsed=len(model_names),
            models_created=models_created,
            model_names=model_names,
            warnings=result.warnings,
            bundle=result.bundle,
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")
