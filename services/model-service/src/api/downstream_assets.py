"""CRUD endpoints for manual downstream-asset tagging (Usage & Downstream Assets).

The user-facing feature is "Usage & Downstream Assets": a manual list of
downstream consumers (dashboards, reports, pipelines, jobs) plus a read-only view
of table-level usage recorded by the gateway query-log scan (see ``impact_scan``).
Route paths keep the ``/downstream-assets`` and ``/impact`` prefixes as a stable
internal API contract with the frontend client.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, insert, select

from shared.db.models import (
    DownstreamAsset,
    GatewayQueryReference,
    Model,
    ModelColumn,
    downstream_asset_columns,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DownstreamAssetCreate,
    DownstreamAssetResponse,
    DownstreamAssetSummaryResponse,
    DownstreamAssetUpdate,
    GatewayQueryReferenceResponse,
)
from src.api._scope import ensure_refs_in_model, scoped_select
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/downstream-assets",
    tags=["usage-downstream-assets"],
)


def _not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def _get_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise _not_found("Model not found")
    return model


def _asset_in_model(asset_id: UUID, *, project_id: UUID, model_id: UUID):
    """SELECT for one asset addressed by PATH id, scoped to project+model.

    The predicate is IN the query. The previous shape —
    ``select(DownstreamAsset).where(id == asset_id)`` followed by
    ``if asset.model_id != model_id`` — had already read another project's
    asset row (and, with an eager loader attached, every ``ModelColumn``
    associated with it) by the time it decided to answer 404. Refusing after
    the read is not refusing.
    """
    return scoped_select(
        DownstreamAsset, model_id=model_id, project_id=project_id
    ).where(DownstreamAsset.id == asset_id)


async def _owned_column_ids(
    db,
    *,
    project_id: UUID,
    model_id: UUID,
    asset_id: UUID | None = None,
) -> dict[UUID, list[UUID]]:
    """asset id -> the associated column ids THIS project+model actually own.

    Covers every asset in the path model, or one asset when ``asset_id`` is
    given. The asset side is constrained by a JOIN on ``model_id`` rather than
    by an ``IN`` list of ids the caller assembled: there is no per-row bind, so
    the query cannot grow into the driver's parameter limit as a model
    accumulates downstream assets, and the asset's own ownership is re-proven
    in the same statement.

    ``ensure_refs_in_model`` protects a ``column_ids`` collection the client
    SUBMITS. It says nothing about rows already in ``downstream_asset_columns``
    — the pre-guard handler resolved ``select(ModelColumn).where(id.in_(...))``
    with no ownership predicate at all, so associations pointing at another
    project's columns can be sitting in the table, and the relationship load
    that used to build the response returned every one of them. A rename of an
    otherwise ordinary asset therefore answered 200 carrying a foreign column
    UUID, which is a successful disclosure rather than a refusal.

    Filtering the READ closes that. The association rows are left alone: a GET
    must not perform a destructive write, and refusing the whole request would
    make the asset permanently unreadable over data the user never knowingly
    created. A client that rewrites ``column_ids`` still purges them, because
    that is a write it asked for.
    """
    stmt = (
        scoped_select(ModelColumn, model_id=model_id, project_id=project_id)
        .join(
            downstream_asset_columns,
            downstream_asset_columns.c.model_column_id == ModelColumn.id,
        )
        .join(
            DownstreamAsset,
            DownstreamAsset.id == downstream_asset_columns.c.asset_id,
        )
        .where(DownstreamAsset.model_id == model_id)
        .add_columns(downstream_asset_columns.c.asset_id)
        .order_by(ModelColumn.id)
    )
    if asset_id is not None:
        stmt = stmt.where(downstream_asset_columns.c.asset_id == asset_id)
    owned: dict[UUID, list[UUID]] = {}
    for column, owning_asset_id in (await db.execute(stmt)).all():
        owned.setdefault(owning_asset_id, []).append(column.id)
    return owned


def _to_response(
    asset: DownstreamAsset,
    *,
    column_ids: list[UUID],
) -> DownstreamAssetResponse:
    """``column_ids`` is REQUIRED and never defaults to ``asset.columns``.

    The relationship is unfiltered; every caller must hand in a list it has
    proven this model owns, so no future edit can reintroduce the unfiltered
    read by simply omitting the argument.
    """
    data = DownstreamAssetResponse.model_validate(asset)
    data.column_ids = list(column_ids)
    return data


@router.get(
    "",
    response_model=list[DownstreamAssetResponse],
    dependencies=[require_role("viewer")],
)
async def list_downstream_assets(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[DownstreamAssetResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        result = await db.execute(
            select(DownstreamAsset)
            .where(DownstreamAsset.model_id == model_id)
            .order_by(DownstreamAsset.created_at)
        )
        assets = list(result.scalars().all())
        owned = await _owned_column_ids(
            db, project_id=project_id, model_id=model_id,
        )
        return [
            _to_response(a, column_ids=owned.get(a.id, [])) for a in assets
        ]


@router.get(
    "/summary",
    response_model=DownstreamAssetSummaryResponse,
    dependencies=[require_role("viewer")],
)
async def downstream_asset_summary(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DownstreamAssetSummaryResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        result = await db.execute(
            select(
                DownstreamAsset.asset_type,
                func.count(DownstreamAsset.id),
            )
            .where(DownstreamAsset.model_id == model_id)
            .group_by(DownstreamAsset.asset_type)
        )
        rows = result.all()
        by_type = {r[0]: r[1] for r in rows}
        return DownstreamAssetSummaryResponse(
            total=sum(by_type.values()),
            by_type=by_type,
        )


@router.post(
    "",
    response_model=DownstreamAssetResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_downstream_asset(
    project_id: UUID,
    model_id: UUID,
    body: DownstreamAssetCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DownstreamAssetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)

        # ``column_ids`` is a body-supplied COLLECTION of foreign keys, and the
        # lookup it replaced (``select(ModelColumn).where(id.in_(...))``) carried
        # no ownership predicate at all: ``model_columns.id`` is tenant-schema-
        # wide, so a modeler authorized for project A could attach their asset to
        # column ids belonging to project B's model. That association is not
        # inert: ``lineage.py`` counts the asset into this model's impact badge,
        # and ``governance_exporter.py`` walks ``downstream_asset_columns`` when
        # building the Collibra / Solidatus graph.
        #
        # Correction to this comment's original claim, checked against the
        # exporter rather than assumed: the export does NOT publish a foreign
        # column. ``governance_exporter.py`` resolves each linked column id
        # through ``column_keys``, which is built only from THIS model's own
        # columns, so an id it does not recognise yields no edge. The exporter
        # fails closed today. The reason to guard the WRITE anyway is that
        # nothing makes that dependency explicit — the containment is a
        # by-product of how the exporter happens to build its key map — and the
        # association itself is still persisted, counted, and (before the read
        # path was scoped) returned by this API.
        #
        # The guard runs BEFORE the row is constructed and added, not merely
        # before commit: the helper's SELECT autoflushes, so a guard placed after
        # ``db.add`` would already have sent the unvalidated association to the
        # database.
        #
        # All-or-nothing, deliberately. The replaced query silently DROPPED ids
        # it could not resolve, so a request naming five columns could persist
        # three and still answer 201 — the modeller was never told which
        # reference was wrong. ``ensure_refs_in_model`` refuses the whole request
        # and names the offending ids (the ``data_tags.py`` precedent).
        response_columns: list[ModelColumn] = list(
            await ensure_refs_in_model(
                db,
                ModelColumn,
                ref_ids=body.column_ids,
                model_id=model_id,
                project_id=project_id,
                field_name="column_ids",
                noun="a model column",
            )
        )

        asset = DownstreamAsset(
            model_id=model_id,
            asset_type=body.asset_type,
            asset_name=body.asset_name,
            asset_url=body.asset_url,
            owner=body.owner,
            notes=body.notes,
        )
        asset.columns = response_columns

        db.add(asset)
        await db.commit()
        await db.refresh(asset)
        # Do not read asset.columns here. After commit/refresh SQLAlchemy may
        # expire the relationship, and async lazy-loading it during response
        # serialization raises MissingGreenlet. The create path already knows
        # the columns that were attached, and they came out of the guard, so
        # they are already proven to belong to this project and model.
        return _to_response(
            asset, column_ids=[c.id for c in response_columns]
        )


@router.put(
    "/{asset_id}",
    response_model=DownstreamAssetResponse,
    dependencies=[require_role("modeler")],
)
async def update_downstream_asset(
    project_id: UUID,
    model_id: UUID,
    asset_id: UUID,
    body: DownstreamAssetUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> DownstreamAssetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        # The ownership predicate is in the SELECT, so an asset belonging to
        # another project simply does not resolve — its row is never read, and
        # neither is anything hanging off it. The previous shape loaded by id
        # and compared ``asset.model_id`` afterwards, with
        # ``selectinload(DownstreamAsset.columns)`` attached, so submitting
        # another project's asset UUID on this route pulled that asset AND
        # every ``ModelColumn`` associated with it into memory before the
        # comparison refused.
        asset = (
            await db.execute(
                _asset_in_model(
                    asset_id, project_id=project_id, model_id=model_id
                )
            )
        ).scalars().one_or_none()
        if asset is None:
            raise _not_found("Downstream asset not found")

        # Same defect class as ``create_downstream_asset`` above, entering
        # through the PATCH body. The guard runs BEFORE the scalar setattr loop
        # because the helper's SELECT autoflushes: validating afterwards would
        # send this request's asset_name / owner / notes edits to the database
        # before the column references could be refused.
        #
        # ``is not None`` is the PRESENCE test for this schema, not a truthiness
        # test — ``[]`` is falsy but means "detach every column", and it must
        # stay legal. ``None`` means the field was not sent and the existing
        # associations are left alone; that is the schema's current contract and
        # is not changed here.
        response_columns: list[ModelColumn] | None = None
        if body.column_ids is not None:
            response_columns = list(
                await ensure_refs_in_model(
                    db,
                    ModelColumn,
                    ref_ids=body.column_ids,
                    model_id=model_id,
                    project_id=project_id,
                    field_name="column_ids",
                    noun="a model column",
                )
            )

        for field in ("asset_type", "asset_name", "asset_url", "owner", "notes"):
            val = getattr(body, field, None)
            if val is not None:
                setattr(asset, field, val)

        if response_columns is not None:
            # Rewrite the association rows directly instead of assigning to
            # ``asset.columns``. Assigning makes SQLAlchemy load the EXISTING
            # collection to compute the delta — which is what forced the eager
            # loader (a lazy load on a fresh per-request session raises
            # MissingGreenlet under asyncio, and answered HTTP 500 for every
            # PUT carrying column_ids), and which would read any legacy foreign
            # ModelColumn row this asset happens to be associated with. The
            # explicit DELETE + INSERT needs no prior state, so nothing outside
            # this project is read, and a legacy foreign association is purged
            # by the rewrite rather than surviving it.
            await db.execute(
                delete(downstream_asset_columns).where(
                    downstream_asset_columns.c.asset_id == asset.id
                )
            )
            if response_columns:
                await db.execute(
                    insert(downstream_asset_columns).values(
                        [
                            {"asset_id": asset.id, "model_column_id": c.id}
                            for c in response_columns
                        ]
                    )
                )

        await db.commit()
        await db.refresh(asset)
        if response_columns is not None:
            column_ids = [c.id for c in response_columns]
        else:
            # ``column_ids`` was not sent, so the stored associations stand —
            # but only the ones this project and model own are reported.
            column_ids = (
                await _owned_column_ids(
                    db,
                    project_id=project_id,
                    model_id=model_id,
                    asset_id=asset.id,
                )
            ).get(asset.id, [])
        return _to_response(asset, column_ids=column_ids)


@router.delete(
    "/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_downstream_asset(
    project_id: UUID,
    model_id: UUID,
    asset_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        # Same scoped SELECT as the update path: a foreign asset must not be
        # loaded at all, let alone loaded and then compared. Sharing the
        # helper is what keeps the two from drifting.
        asset = (
            await db.execute(
                _asset_in_model(
                    asset_id, project_id=project_id, model_id=model_id
                )
            )
        ).scalars().one_or_none()
        if asset is None:
            raise _not_found("Downstream asset not found")
        # Explicit DML rather than ``db.delete(asset)``. Deleting a parent of a
        # ``secondary`` relationship makes SQLAlchemy LOAD the collection at
        # flush time so it can clear the association rows — which reads every
        # associated ``ModelColumn``, including a legacy one belonging to
        # another project. The row set is already known from the asset id, so
        # nothing needs loading to delete it.
        await db.execute(
            delete(downstream_asset_columns).where(
                downstream_asset_columns.c.asset_id == asset.id
            )
        )
        await db.execute(
            delete(DownstreamAsset).where(DownstreamAsset.id == asset.id)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Query references (read-only — populated by impact scan)
# ---------------------------------------------------------------------------

# Route prefix kept as ``/impact`` for API back-compat (frontend client depends
# on it); the feature is named "Usage & Downstream Assets" in the UI and docs.
query_ref_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/impact/query-references",
    tags=["usage-downstream-assets"],
)


@query_ref_router.get(
    "",
    response_model=list[GatewayQueryReferenceResponse],
    dependencies=[require_role("viewer")],
)
async def list_query_references(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[GatewayQueryReferenceResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        result = await db.execute(
            select(GatewayQueryReference)
            .where(GatewayQueryReference.model_id == model_id)
            .order_by(GatewayQueryReference.hit_count.desc())
        )
        return [
            GatewayQueryReferenceResponse.model_validate(r)
            for r in result.scalars().all()
        ]
