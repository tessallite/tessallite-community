"""
DataTarget CRUD routes (aggregate output destinations).
"""
from __future__ import annotations

import json

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from shared.db.models import (
    AggregateDefinition,
    DataTarget,
    Model,
    PocketDefinition,
    ProjectConnection,
)
from shared.artifact_target_binding import invalidate_artifacts_for_target
from shared.config.source_db import (
    BigQueryProjectResolutionError,
    resolve_connection_bq_project,
)
from shared.db.session import get_tenant_db
from shared.schemas.connection_type import (
    ALLOWED_CONNECTION_TYPES,
    normalize_connection_type,
)
from shared.schemas.pydantic_models import DataTargetCreate, DataTargetResponse, DataTargetUpdate
from src.api._model_lock import acquire_model_definition_lock
from src.api._scope import ensure_model_in_project
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

# Connector types Tessallite can materialise an aggregate INTO. A subset of
# ALLOWED_CONNECTION_TYPES — every canonical connector is a valid aggregate
# target today (the optimizer/scheduler dispatch on PG-family, bigquery,
# hadoop_spark), so the supported-target matrix equals the canonical set.
SUPPORTED_TARGET_TYPES = frozenset(ALLOWED_CONNECTION_TYPES)


async def _validate_target_connection(
    db, project_id, model_id, project_connection_id
) -> ProjectConnection:
    """F-009-18: fail early when a DataTarget references an invalid connection.

    The optimizer/scheduler otherwise surface an unsupported or cross-project
    target only later, as a confusing source-side SQL syntax error. Validate
    at write time that the connection exists, belongs to the same project as
    the model, and is a supported target connector.

    Returns the validated ProjectConnection so callers can perform additional
    connection-dependent checks without a second round-trip.
    """
    model = await ensure_model_in_project(
        db, project_id=project_id, model_id=model_id
    )
    conn = await db.get(ProjectConnection, project_connection_id)
    if conn is None:
        raise HTTPException(
            status_code=422,
            detail="project_connection_id does not reference an existing connection",
        )
    if conn.project_id != model.project_id:
        raise HTTPException(
            status_code=422,
            detail="Target connection belongs to a different project",
        )
    canonical = normalize_connection_type(conn.connection_type)
    if canonical not in SUPPORTED_TARGET_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Connection type {conn.connection_type!r} is not a supported "
                f"aggregate target"
            ),
        )
    return conn


def _strip_bigquery_target_project(
    target_config: dict, conn: ProjectConnection,
) -> dict:
    """Remove a redundant project_id from the target config.

    When config.project_id matches the connection's project, it is stripped
    so the stored configuration does not carry a second project authority.
    """
    from shared.schemas.connection_type import normalize_connection_type

    config = dict(target_config or {})
    if "project_id" not in config:
        return config
    canonical = normalize_connection_type(conn.connection_type)
    if canonical != "bigquery":
        return config
    conn_project = _resolve_connection_bq_project(conn)
    if conn_project and config["project_id"] == conn_project:
        config.pop("project_id")
    return config


def _validate_target_type_alignment(
    conn: ProjectConnection,
    target_type: str,
) -> str:
    """Require the API label to describe the referenced connection.

    ``DataTarget.target_type`` is legacy descriptive metadata; it must never
    decide connector policy. The connection is the persisted authority used by
    every build and serving path. Keep accepting supported legacy aliases such
    as ``jdbc`` by normalising both values for comparison, while preserving the
    caller's stored label for backwards-compatible API responses.
    """
    connection_type = normalize_connection_type(conn.connection_type)
    requested_type = normalize_connection_type(target_type)
    if requested_type not in SUPPORTED_TARGET_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Target type {target_type!r} is not a supported aggregate "
                "target"
            ),
        )
    if requested_type != connection_type:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "target_connection_type_mismatch",
                "message": (
                    f"DataTarget target_type ({target_type!r}) must match the "
                    f"referenced ProjectConnection type ({conn.connection_type!r})."
                ),
            },
        )
    return connection_type


def _validate_bigquery_target_project(
    conn: ProjectConnection,
    target_config: dict,
) -> None:
    """Bug-8790: enforce the one-project rule for BigQuery DataTargets.

    1. Reject a dataset name that contains a dot (``project.dataset``).
    2. Reject ``config.project_id`` when it differs from the connection's
       project — the connection is the single authoritative project.
    3. Reject an ADC-only connection at save time (fail-closed) — the
       connection's project must be explicit in its config or credentials.

    When ``config.project_id`` equals the connection's project, it is
    accepted and then the caller strips it from the stored configuration
    (the DataTarget no longer carries an independent project_id).
    """
    canonical = normalize_connection_type(conn.connection_type)
    if canonical != "bigquery":
        return

    config = target_config or {}

    # ``DataTarget*.config`` deliberately accepts arbitrary JSON options, but
    # these three keys are routing identifiers. Validate their types before any
    # string operation so malformed client JSON becomes a stable 422 rather
    # than an uncaught TypeError/500. Do not coerce: ``123`` is not a dataset
    # named ``"123"`` and silently changing its routing meaning is unsafe.
    for key in ("dataset", "schema", "project_id"):
        value = config.get(key)
        if value is not None and not isinstance(value, str):
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "bigquery_target_config_identifier_invalid",
                    "message": (
                        f"BigQuery DataTarget config.{key} must be a string"
                    ),
                },
            )

    # Reject dotted dataset names.
    dataset = config.get("dataset") or config.get("schema") or ""
    if "." in dataset:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "bigquery_dotted_dataset",
                "message": (
                    f"BigQuery dataset must not contain a dot: {dataset!r}. "
                    "Use a view in the connection's project to reference "
                    "external tables."
                ),
            },
        )

    # Resolve the connection's canonical project from config/creds/service-account.
    conn_project = _resolve_connection_bq_project(conn)

    target_project = config.get("project_id")
    if not target_project:
        # No target project override — target inherits the connection's project.
        # If the connection itself has no explicit project (ADC-only), reject.
        if not conn_project:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "bigquery_adc_project_required",
                    "message": (
                        "BigQuery connection must have an explicit project_id "
                        "in its config or credentials. ADC-only connections are "
                        "not accepted: set 'project_id' on the connection."
                    ),
                },
            )
        return

    if not conn_project:
        # Target has a project but the connection resolves only through ADC.
        # Cannot verify equivalence — reject fail-closed.
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "bigquery_adc_project_required",
                "message": (
                    "BigQuery connection must have an explicit project_id "
                    "in its config or credentials. ADC-only connections are "
                    "not accepted: set 'project_id' on the connection."
                ),
            },
        )

    if target_project != conn_project:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "bigquery_target_project_mismatch",
                "message": (
                    f"BigQuery DataTarget project_id ({target_project!r}) must "
                    f"match the ProjectConnection's project ({conn_project!r}). "
                    "Remove 'project_id' from the DataTarget config — the "
                    "connection's project is authoritative."
                ),
            },
        )


def _resolve_connection_bq_project(conn: ProjectConnection) -> str | None:
    """Resolve a target connection project or reject unreadable credentials.

    The shared resolver distinguishes a genuine ADC-only connection from a
    persisted configuration/decryption failure. The write boundary maps the
    latter to a stable client error rather than silently treating it as ADC.
    """
    try:
        return resolve_connection_bq_project(conn)
    except BigQueryProjectResolutionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "bigquery_connection_project_invalid",
                "message": "BigQuery connection project configuration is invalid",
            },
        ) from exc

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/targets", tags=["targets"]
)


@router.post(
    "",
    response_model=DataTargetResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_target(
    project_id: UUID,
    model_id: UUID,
    body: DataTargetCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataTargetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-8441 / Bug-8437: this writes TWO revert-owned rows — the
        # ``data_targets`` row (upserted, and hard-deleted when absent from the
        # reverted-to snapshot) and ``models.target_id`` (a rehydrated model
        # scalar). Both are lost to a concurrent revert without the lock.
        #
        # Bug-8730 / Bug-8741: the Model-only ownership check is done HERE, up
        # front, so the lock sits in the same place ``sources.create_source``
        # puts it — after ownership, before everything else. The previous
        # ordering (validate the connection first, lock second) meant a caller
        # could be rejected 422 before ever reaching the lock, which made the
        # endpoint impossible to cover with the live serialisation probe and
        # left the asymmetry with ``create_source`` unexplained.
        # ``_validate_target_connection`` still performs its own
        # ``ensure_model_in_project`` (it has no other caller and its contract
        # is self-contained); the duplicate is a ``db.get`` that hits the
        # identity map, not a second round trip. Neither validator does
        # external I/O — both are plain ``db.get`` reads — so nothing slow is
        # held under the cluster-wide lock. One observable change: the 422
        # connection-validation failures now happen AFTER the lock, so during a
        # long revert they surface as the retryable 503 instead.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)
        conn = await _validate_target_connection(
            db, project_id, model_id, body.project_connection_id
        )
        # Bug-8761: connection_type is the connector authority. The target
        # label must agree but never controls the BigQuery policy branch.
        _validate_target_type_alignment(conn, body.target_type)
        _validate_bigquery_target_project(conn, body.config)
        # Strip equal project_id from stored config — the connection is authoritative.
        _clean_target_config = _strip_bigquery_target_project(body.config, conn)
        target = DataTarget(model_id=model_id, **{
            **body.model_dump(), "config": _clean_target_config,
        })
        db.add(target)
        await db.flush()
        model = await db.get(Model, model_id)
        if model is not None and model.target_id is None:
            model.target_id = target.id
        await db.commit()
        await db.refresh(target)
        return DataTargetResponse.model_validate(target)


@router.get("", response_model=list[DataTargetResponse], dependencies=[require_role("viewer")])
async def list_targets(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DataTargetResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(DataTarget).where(DataTarget.model_id == model_id)
        )
        return [DataTargetResponse.model_validate(t) for t in result.scalars().all()]


@router.get("/{target_id}", response_model=DataTargetResponse, dependencies=[require_role("viewer")])
async def get_target(
    project_id: UUID,
    model_id: UUID,
    target_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataTargetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        t = await db.get(DataTarget, target_id)
        if t is None or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataTarget not found")
        return DataTargetResponse.model_validate(t)


@router.patch(
    "/{target_id}",
    response_model=DataTargetResponse,
    dependencies=[require_role("modeler")],
)
async def update_target(
    project_id: UUID,
    model_id: UUID,
    target_id: UUID,
    body: DataTargetUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DataTargetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-8441: read-modify-write on a revert-owned row (the revert upserts
        # every column of this target from the snapshot). READ-UNDER-LOCK, so the
        # routing-state comparison below is made against the row the write lands
        # on rather than a pre-revert copy of it.
        await acquire_model_definition_lock(db, model_id)
        t = await db.get(DataTarget, target_id)
        if t is None or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataTarget not found")
        updates = body.model_dump(exclude_unset=True)
        # Re-validate the EFFECTIVE connection on every write. A legacy row can
        # otherwise bypass the connector-authority rule by patching only its
        # config/display name while carrying a mismatched target_type.
        new_conn_id = updates.get("project_connection_id")
        # Bug-8761/Bug-8452: use the connection's normalised type for every
        # BigQuery decision, then require the legacy target label to align.
        _new_target_type = updates.get("target_type", t.target_type)
        _new_config = updates.get("config", t.config or {})
        _effective_conn_id = new_conn_id if new_conn_id is not None else t.project_connection_id
        _conn = await _validate_target_connection(
            db, project_id, model_id, _effective_conn_id
        )
        _validate_target_type_alignment(_conn, _new_target_type)
        _validate_bigquery_target_project(_conn, _new_config)
        # Strip equal project_id from stored config.
        _cleaned_config = _strip_bigquery_target_project(_new_config, _conn)
        if "config" in updates:
            updates["config"] = _cleaned_config
        # Bug-8473: capture the routing-affecting state BEFORE the edit. A
        # target's connection, connector type and config decide WHICH database
        # its artifacts' (schema, table) names resolve to; every aggregate and
        # pocket already built on this target holds rows from the OLD location.
        # If the new location happens to hold a same-named table, those cached
        # artifacts would serve its rows — under row-level security the injected
        # predicate is then evaluated against a foreign table's column, so a row
        # that was never part of the model population can be returned.
        def _routing_state() -> str:
            # Canonical JSON so the comparison cannot be defeated by dict
            # identity (a config replaced with an equal dict is NOT a change;
            # an in-place mutation of the same dict still IS one).
            return json.dumps(
                {
                    "connection": str(t.project_connection_id),
                    "target_type": str(t.target_type),
                    "config": t.config or {},
                },
                sort_keys=True, default=str,
            )

        _routing_before = _routing_state()
        for k, v in updates.items():
            setattr(t, k, v)
        _routing_after = _routing_state()
        if _routing_after != _routing_before:
            # Same transaction as the edit: a reader sees either the old
            # location with servable artifacts, or the new one with none.
            await invalidate_artifacts_for_target(
                db, t.id,
                reason=(
                    "The target's storage location changed (connection, "
                    "connector type or config), so this cache was built on a "
                    "different database and must be rebuilt before it can "
                    "serve again."
                ),
            )
        await db.commit()
        await db.refresh(t)
        return DataTargetResponse.model_validate(t)


@router.delete(
    "/{target_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_target(
    project_id: UUID,
    model_id: UUID,
    target_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-8441 / Bug-8437: deletes a revert-owned ``data_targets`` row and may
        # clear ``models.target_id`` (a rehydrated model scalar). The row-level
        # FOR UPDATE below serialises against aggregate/pocket creation, not
        # against a revert — only the per-model definition lock does that.
        # READ-UNDER-LOCK: the dependency scan must see post-revert state.
        await acquire_model_definition_lock(db, model_id)
        t = await db.get(DataTarget, target_id)
        if t is None or t.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataTarget not found")

        # Bug-7795: lock the target row FOR UPDATE so a concurrent aggregate/
        # pocket create referencing this target blocks until this transaction
        # resolves — closing the check-then-delete window where a new
        # materialisation could slip past the dependency scan below and then
        # trip the FK at commit as a 500.
        await db.execute(
            select(DataTarget.id)
            .where(DataTarget.id == target_id)
            .with_for_update()
        )

        # Bug-7795: AggregateDefinition.target_id / PocketDefinition.target_id
        # FK data_targets.id with NO ondelete, so deleting an in-use target
        # raised a raw IntegrityError → HTTP 500. Guard with a 409 + the names
        # of the blocking materialisations so the modeller can retire them
        # first. Do NOT delete-cascade the materialisations — they hold physical
        # tables in the source and their own refresh state.
        agg_names = (
            await db.execute(
                select(AggregateDefinition.physical_table_name).where(
                    AggregateDefinition.target_id == target_id
                )
            )
        ).scalars().all()
        pocket_names = (
            await db.execute(
                select(PocketDefinition.physical_table_name).where(
                    PocketDefinition.target_id == target_id
                )
            )
        ).scalars().all()
        if agg_names or pocket_names:
            refs = []
            if agg_names:
                refs.append(f"aggregates: {', '.join(agg_names)}")
            if pocket_names:
                refs.append(f"pockets: {', '.join(pocket_names)}")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot delete target; it still holds "
                    f"{'; '.join(refs)}. Retire these materialisations first."
                ),
            )

        # Bug-7795: only clear Model.target_id when it pointed at THIS target.
        # The previous code silently repointed the model to an arbitrary
        # remaining target — a hidden change of the model's materialisation
        # destination the modeller never chose. Clear it instead and let the
        # modeller pick the new default explicitly.
        model = await db.get(Model, model_id)
        if model is not None and model.target_id == target_id:
            model.target_id = None
        await db.delete(t)
        try:
            await db.commit()
        except IntegrityError as exc:
            # Safety net: if a materialisation FK still trips at commit despite
            # the guard + row lock (e.g. an edge race), surface a clean 409
            # instead of a raw 500.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Cannot delete target; it is still referenced by an "
                    "aggregate or pocket. Retire those materialisations first."
                ),
            ) from exc
