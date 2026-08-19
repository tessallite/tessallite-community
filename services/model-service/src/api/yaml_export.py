"""YAML model export/import endpoints.

POST /projects/{project_id}/export/yaml  — export project as YAML zip
POST /projects/{project_id}/import/yaml  — import YAML zip, persist models

Version diff lives on the model-versions surface (``versions.py``), which is
the single, wired diff implementation (``shared/model_snapshot/differ.py``).
The previously-duplicated, never-wired YAML diff endpoint + its ``diff.py``
module were removed (F-020-10).
"""
from __future__ import annotations

import io
import logging
import uuid as _uuid
import zipfile
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from shared.auth.middleware import CurrentUser, require_tenant_admin
from shared.db.models import Model, ProjectConnection
from shared.db.session import get_tenant_db
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.db.model_write_lock_guard import model_write_lock_exempt
from shared.model_snapshot.rehydrator import SnapshotSchemaError, rehydrate_into_live
from src.api.personas import seed_technical_persona
from shared.model_snapshot.consistent_read import consistent_read_session
from shared.model_snapshot.serialiser import snapshot_model
from shared.model_snapshot.slug_utils import insert_model_with_slug_retry
from shared.model_snapshot.yaml_deserialiser import YamlImportError, parse_project_yaml
from shared.model_snapshot.yaml_serialiser import project_to_yaml, snapshot_to_yaml
from sqlalchemy import func, select
from src.licensing_guard import enforce_demo_source_locked, enforce_import_model_cap
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


class YamlImportResponse(BaseModel):
    models_parsed: int
    models_created: int
    warnings: list[str]
    model_names: list[str]


@router.post(
    "/projects/{project_id}/export/yaml",
    tags=["yaml-export"],
)
async def export_project_yaml(
    project_id: UUID,
    current_user: CurrentUser = Depends(require_tenant_admin),
):
    # Bug-8380: project metadata, connections, model enumeration, and every
    # model snapshot must come from one committed point in time.
    async with consistent_read_session(current_user.tenant_id) as db:
        from shared.db.models import Project
        project = await db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        conn_result = await db.execute(
            select(ProjectConnection).where(ProjectConnection.project_id == project_id)
        )
        connections = list(conn_result.scalars().all())

        conn_map = {str(c.id): c.display_name or str(c.id) for c in connections}

        model_result = await db.execute(
            select(Model).where(Model.project_id == project_id)
        )
        models = list(model_result.scalars().all())

        # Bug-5727: track invalid entities that the YAML serialiser will
        # silently drop (is_invalid=True measures/dimensions). Log a warning
        # for each so the export is auditable, and surface the count in a
        # response header.
        total_dropped = 0

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            project_yaml = project_to_yaml(
                {"slug": project.slug, "display_name": project.display_name},
                [
                    {"display_name": c.display_name, "connection_type": c.connection_type}
                    for c in connections
                ],
            )
            zf.writestr("project.yaml", project_yaml)

            for m in models:
                snap = await snapshot_model(m.id, db)
                ds = snap.get("data_sources", [])
                conn_name = None
                if ds:
                    conn_id = ds[0].get("project_connection_id", "")
                    conn_name = conn_map.get(conn_id)

                # Bug-5727: detect invalid measures/dimensions before
                # serialisation so dropped entities are logged, not silent.
                for meas in snap.get("measures", []):
                    if meas.get("is_invalid"):
                        total_dropped += 1
                        logger.warning(
                            "YAML export: dropping invalid measure %r "
                            "(id=%s) from model %r",
                            meas.get("name"), meas.get("id"), m.slug,
                        )
                for dim in snap.get("dimensions", []):
                    if dim.get("is_invalid"):
                        total_dropped += 1
                        logger.warning(
                            "YAML export: dropping invalid dimension %r "
                            "(id=%s) from model %r",
                            dim.get("name"), dim.get("id"), m.slug,
                        )

                model_yaml = snapshot_to_yaml(
                    snap,
                    project_name=project.slug,
                    connection_name=conn_name,
                )
                slug = m.slug or str(m.id)
                zf.writestr(f"models/{slug}.yaml", model_yaml)

        buf.seek(0)
        filename = f"{project.slug}-export.zip"
        headers: dict[str, str] = {
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if total_dropped > 0:
            headers["X-Tessallite-Export-Dropped"] = str(total_dropped)
            logger.warning(
                "YAML export for project %s: %d invalid entities omitted",
                project.slug, total_dropped,
            )
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers=headers,
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/projects/{project_id}/import/yaml",
    response_model=YamlImportResponse,
    tags=["yaml-export"],
)
async def import_project_yaml(
    project_id: UUID,
    file: UploadFile = File(...),
    current_user: CurrentUser = Depends(require_tenant_admin),
):
    enforce_demo_source_locked(current_user.tenant_id)

    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload must be a .zip file")

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

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")

    uncompressed_total = sum(info.file_size for info in zf.infolist())
    if uncompressed_total > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Uncompressed zip contents exceed 50 MB limit")

    project_content = None
    model_contents: dict[str, str] = {}

    for name in zf.namelist():
        if ".." in name or name.startswith("/"):
            continue
        if name == "project.yaml":
            project_content = zf.read(name).decode("utf-8")
        elif name.startswith("models/") and name.endswith((".yaml", ".yml")):
            model_contents[name] = zf.read(name).decode("utf-8")

    if not project_content:
        raise HTTPException(status_code=400, detail="Missing project.yaml in zip")

    try:
        bundle = parse_project_yaml(project_content, model_contents)
    except YamlImportError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})

    all_warnings: list[str] = []
    model_names: list[str] = []
    models_created = 0

    models_to_import = len(bundle.get("models", []))

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

        # YAML carries no connection credentials; bind placeholder data
        # sources to an existing project connection. F-020-17: rebind each
        # model to the SAME-NAMED project connection the export recorded
        # (model.connection), not just the first one — multi-source projects
        # lost their per-model binding before. Fall back to the first
        # connection, then to a created placeholder. DataSource.
        # project_connection_id is NOT NULL, so this must resolve before
        # rehydration.
        conn_q = await db.execute(
            select(ProjectConnection.id, ProjectConnection.display_name)
            .where(ProjectConnection.project_id == project_id)
        )
        conn_rows = conn_q.all()
        conn_by_name: dict[str, str] = {
            (r[1] or ""): str(r[0]) for r in conn_rows
        }
        first_conn_id: str | None = str(conn_rows[0][0]) if conn_rows else None
        placeholder_conn_id: str | None = None

        async def _resolve_conn_id(name: str | None) -> str:
            nonlocal placeholder_conn_id
            if name and name in conn_by_name:
                return conn_by_name[name]
            if first_conn_id is not None:
                if name:
                    all_warnings.append(
                        f"No project connection named '{name}' — bound to the "
                        f"first available connection. Re-point if needed."
                    )
                return first_conn_id
            if placeholder_conn_id is None:
                from shared.security.credential_crypto import encrypt_json
                placeholder_conn = ProjectConnection(
                    project_id=project_id,
                    display_name="(imported — configure me)",
                    connection_type="postgresql",
                    encrypted_credentials=encrypt_json({}),
                    config={"unconfigured": True, "import_placeholder": True},
                )
                db.add(placeholder_conn)
                await db.flush()
                placeholder_conn_id = str(placeholder_conn.id)
                all_warnings.append(
                    "No project connection found — created a placeholder. "
                    "Configure it with real credentials before querying."
                )
            return placeholder_conn_id

        existing_q = await db.execute(
            select(Model.slug).where(Model.project_id == project_id)
        )
        existing_slugs = {r[0] for r in existing_q.all()}

        for model_snap in bundle.get("models", []):
            snap_model = model_snap.get("model", {})
            slug = snap_model.get("slug", "")
            model_names.append(slug)
            all_warnings.extend(model_snap.get("warnings", []))

            # Inject the resolved project_connection_id into placeholder data
            # sources (by recorded connection name where present).
            model_conn_id = await _resolve_conn_id(model_snap.get("connection_name"))
            for ds in model_snap.get("data_sources", []):
                if not ds.get("project_connection_id"):
                    ds["project_connection_id"] = model_conn_id

            new_model_id = _uuid.uuid4()
            rewritten, missing = prepare_snapshot_for_import(
                model_snap, new_model_id=new_model_id,
            )
            rewritten.setdefault("model", {})
            if snap_model.get("display_name"):
                rewritten["model"]["display_name"] = snap_model["display_name"]

            # F-020-19/22: headroom-safe collision suffixing + race-safe insert.
            # Bug-6291: insert_model_with_slug_retry now enforces the BI-safe
            # slug contract.  Catch the ValueError and surface a clear 422
            # naming the offending model so YAML authors know what to fix.
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
                    status_code=422,
                    detail=(
                        f"YAML model '{slug}' has an invalid slug: {exc}. "
                        f"Rename the model in the YAML file using only "
                        f"letters, digits, and underscores (e.g. "
                        f"'sales_model' instead of 'sales-model')."
                    ),
                ) from exc
            rewritten["model"]["slug"] = candidate

            # Bug-6291: rehydrate_into_live -> _insert_personas now
            # validates persona slugs against the BI-safe contract.
            # Catch ValueError and surface as a clear 422.
            try:
                # Bug-7982 R7: DELIBERATE non-holder. Rehydrates into a model
                # created in THIS transaction, so no other writer can reference
                # it yet and there is nothing to serialise against. Declared
                # explicitly so the runtime write guard does not report (and
                # thereby drown out) a benign wholesale rebuild.
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
            except SnapshotSchemaError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error_code": "PERSONA_IMPORT_VALIDATION",
                        "message": str(exc),
                    },
                ) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"YAML model '{slug}' contains an invalid "
                        f"persona slug: {exc}. "
                        f"Rename the persona in the YAML file using "
                        f"only letters, digits, and underscores."
                    ),
                ) from exc
            # Bug-6138: importer-created models bypass create_model, so seed the
            # canonical Technical persona here too (idempotent).
            await seed_technical_persona(db, new_model_id)
            models_created += 1

        await db.commit()

        return YamlImportResponse(
            models_parsed=len(model_names),
            models_created=models_created,
            warnings=all_warnings,
            model_names=model_names,
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")
