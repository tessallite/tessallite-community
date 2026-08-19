"""dbt semantic model import endpoint.

POST /projects/{project_id}/import/dbt — accepts dbt schema.yml or zip,
parses dbt semantic models and persists them into the target project.
"""
from __future__ import annotations

import io
import logging
import uuid as _uuid
import zipfile
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi import status as http_status
from pydantic import BaseModel

from shared.auth.middleware import CurrentUser, require_tenant_admin
from shared.db.models import Model, ProjectConnection
from shared.db.session import get_tenant_db
from shared.importers.dbt_mapper import MapResult, map_dbt_to_tessallite
from shared.importers.dbt_parser import DbtParseError, parse_dbt_project, parse_dbt_yaml
from shared.importers.import_warnings import (
    ImportWarningResponse,
    make_import_warning,
    normalize_import_warnings,
)
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.db.model_write_lock_guard import model_write_lock_exempt
from shared.model_snapshot.rehydrator import rehydrate_into_live
from shared.model_snapshot.slug_utils import insert_model_with_slug_retry
from src.api.personas import seed_technical_persona
from src.licensing_guard import enforce_demo_source_locked, enforce_import_model_cap
from sqlalchemy import func, select

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


class DbtImportResponse(BaseModel):
    models_parsed: int
    models_created: int
    model_names: list[str]
    warnings: list[ImportWarningResponse]
    bundle: dict[str, Any]


@router.post(
    "/projects/{project_id}/import/dbt",
    response_model=DbtImportResponse,
    tags=["dbt-import"],
)
async def import_dbt_semantic_models(
    project_id: UUID,
    file: UploadFile = File(...),
    # Bug-7309: parse-only preview. When true, the file is parsed and mapped and
    # the loss/warning report is returned WITHOUT persisting anything
    # (models_created=0) so the admin can review what would not transfer before
    # committing. Plain bool default (not Query(...)) so a direct in-process call
    # of this handler still defaults to False rather than a truthy FieldInfo.
    dry_run: bool = False,
    current_user: CurrentUser = Depends(require_tenant_admin),
):
    enforce_demo_source_locked(current_user.tenant_id)

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

    try:
        if filename.endswith(".zip"):
            zf = zipfile.ZipFile(io.BytesIO(content))
            uncompressed_total = sum(info.file_size for info in zf.infolist())
            if uncompressed_total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Uncompressed zip contents exceed 50 MB limit")
            files: dict[str, str] = {}
            for name in zf.namelist():
                if ".." in name or name.startswith("/"):
                    continue
                if name.endswith((".yml", ".yaml")) and not name.startswith("__MACOSX"):
                    files[name] = zf.read(name).decode("utf-8")
            if not files:
                raise HTTPException(
                    status_code=400,
                    detail="No YAML files found in zip",
                )
            parsed = parse_dbt_project(files)
        elif filename.endswith((".yml", ".yaml")):
            parsed = parse_dbt_yaml(content.decode("utf-8"))
        else:
            raise HTTPException(
                status_code=400,
                detail="Upload must be a .yml, .yaml, or .zip file",
            )
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")
    except DbtParseError as exc:
        raise HTTPException(
            status_code=422,
            detail={"errors": exc.errors},
        )

    result: MapResult = map_dbt_to_tessallite(parsed)

    model_names = [
        m.get("model", {}).get("slug", "")
        for m in result.bundle.get("models", [])
    ]

    models_to_import = len(result.bundle.get("models", []))

    # Bug-7309: parse-only preview — return the mapped bundle + loss/warning
    # report WITHOUT opening a mutating tenant session or persisting anything.
    # The admin reviews unsupported/disabled objects, then re-submits with
    # dry_run=false to apply. Nothing has been mutated at this point.
    if dry_run:
        return DbtImportResponse(
            models_parsed=len(model_names),
            models_created=0,
            model_names=model_names,
            warnings=normalize_import_warnings(result.warnings, source="dbt"),
            bundle=result.bundle,
        )

    models_created = 0
    async for db in get_tenant_db(current_user.tenant_id):
        from shared.db.models import Project
        project = await db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        # Bug-7468: enforce the licensed model cap BEFORE creating any models.
        async def _count_models() -> int:
            r = await db.execute(select(func.count()).select_from(Model))
            return int(r.scalar() or 0)

        # Bug-6567: pass db so imports and direct creates serialise via
        # the same advisory lock, preventing concurrent cap bypass.
        await enforce_import_model_cap(models_to_import, _count_models, db=db)

        # Bug-7307: always create a clearly unconfigured placeholder
        # connection for imported models instead of silently binding to an
        # arbitrary existing project connection.  The previous code picked
        # ``limit(1)`` without ``order_by``, so in a project with multiple
        # connections the imported model could bind to the wrong one (e.g.
        # ``marketing_dev`` instead of ``finance_prod``).  The admin must
        # explicitly configure real credentials and rebind after import.
        from shared.security.credential_crypto import encrypt_json
        placeholder_conn = ProjectConnection(
            project_id=project_id,
            display_name="(dbt import — configure me)",
            connection_type="postgresql",
            encrypted_credentials=encrypt_json({}),
            config={"unconfigured": True, "import_placeholder": True},
        )
        db.add(placeholder_conn)
        await db.flush()
        default_conn_id = str(placeholder_conn.id)
        result.warnings.append(
            make_import_warning(
                code="dbt.connection_placeholder",
                params={"connection": placeholder_conn.display_name},
                detail=(
                    "Created an unconfigured placeholder connection for "
                    "imported models. Configure it with real credentials and "
                    "rebind data sources before querying."
                ),
            )
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
            # Bug-7622: a non-BI-safe slug raises ValueError from
            # validate_bi_safe_slug; surface it as a clean 422 (matching the
            # YAML/project import path) rather than an uncaught 500. The open
            # transaction rolls back on exit, so no partial models are committed.
            try:
                new_model, candidate = await insert_model_with_slug_retry(
                    db,
                    project_id=project_id,
                    base_slug=slug,
                    existing_slugs=existing_slugs,
                    display_name=snap_model.get("display_name") or slug,
                    new_model_id=new_model_id,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid model slug in dbt import: {exc}",
                ) from exc
            rewritten["model"]["slug"] = candidate

            # Bug-7982 R7: DELIBERATE non-holder. This rehydrates into a model created in THIS transaction, so no other writer can reference it yet and there is nothing to serialise against. Declared explicitly so the runtime write guard does not report (and thereby drown out) a benign wholesale rebuild.
            async with model_write_lock_exempt(
                db, "import: wholesale rebuild into a model created in this transaction"
            ):
                await rehydrate_into_live(
                    new_model_id, rewritten, db,
                    drop_orphan_aggregates=False,
                    actor=current_user.email or current_user.user_id,
                    force_aggregate_pending=True,
                    force_pocket_stale=True,
                    preserve_destination_seed=True,
                )
            # Bug-6138: importer-created models bypass create_model, so seed the
            # canonical Technical persona here too (idempotent — a no-op if the
            # imported bundle already carried one).
            await seed_technical_persona(db, new_model_id)
            models_created += 1

        await db.commit()

        return DbtImportResponse(
            models_parsed=len(model_names),
            models_created=models_created,
            model_names=model_names,
            warnings=normalize_import_warnings(result.warnings, source="dbt"),
            bundle=result.bundle,
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")
