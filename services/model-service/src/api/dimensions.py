"""
Dimension CRUD routes.

Role requirements:
  GET (list / get) → viewer+
  POST / PATCH     → modeler+
  DELETE           → modeler+
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pydantic import BaseModel

from shared.db.models import (
    AggregateDefinition,
    Dimension,
    DimensionAttributeRelationship,
    HierarchyDefinition,
    HierarchyLevel,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
    Persona,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    DimensionAttributeRelationshipCreate,
    DimensionAttributeRelationshipResponse,
    DimensionAttributeRelationshipUpdate,
    DimensionCreate,
    DimensionResponse,
    DimensionUpdate,
    RedundantPartnerInfo,
)
from shared.semantic.attribute_relationship_hash import compute_declaration_hash
from shared.semantic.redundant_partner import compute_redundant_partners
from shared.security.restricted_column_closure import (
    ClosureContext,
    calc_expression_touches_restricted,
    normalise_id_set,
    object_touches_restricted,
)
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, enforce_model_scope, get_current_user
from src.auth.rbac import require_role
from src.api._column_helpers import resolve_column
from src.api._persona_scope import (
    get_restricted_column_ids,
    parse_allowed_ids,
    resolve_effective_persona,
)
from src.api._scope import (
    ensure_model_in_project,
    ensure_refs_in_model,
    glossary_text_for_target as _glossary_text_for_target,
    glossary_texts_for_targets as _glossary_texts_for_targets,
    purge_entity_soft_references,
)
from src.measure_rename import UnsafeMeasureRename, propagate_measure_renames

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/dimensions", tags=["dimensions"]
)

# Model-level router for cross-attribute operations (different prefix).
bulk_router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}", tags=["dimensions"]
)


async def _source_table_id_for_dim(db, dim) -> UUID | None:
    """Return the ModelTable id that the dimension's KEY column belongs to, or
    ``None`` when the dimension is not bound to a physical column."""
    if getattr(dim, "source_column_id", None) is None:
        return None
    col = await db.get(ModelColumn, dim.source_column_id)
    return col.model_table_id if col is not None else None


async def _resolve_display_column_id(
    db,
    *,
    display_column_name: str | None,
    source_table_id: UUID | None,
    source_column_id: UUID | None,
    user_defined_attribute_id: UUID | None,
    model_id: UUID,
    project_id: UUID,
) -> UUID | None:
    """Bug-5434: resolve a flat dimension's optional DISPLAY column name to a
    ``model_columns.id``, validating it is a legitimate distinct caption source.

    Returns ``None`` when no display column is requested (or an empty string is
    passed to clear it). Raises HTTP 422 when the request is invalid:
      - a display column requires a physical-column (key-backed) flat dimension
        (not a UDA dimension), so ``source_column_id`` must be set;
      - the display column must resolve in the same source table as the key;
      - the display column must differ from the key column.
    """
    if display_column_name is None:
        return None
    name = display_column_name.strip()
    if not name:
        # Explicit clear.
        return None
    if user_defined_attribute_id is not None or source_column_id is None or source_table_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "A display column can only be set on a physical-column dimension "
                "(provide source_table_id and source_column_name)."
            ),
        )
    # ``source_table_id`` reaches this helper straight from the request body on
    # the create path, so the model context is threaded in and enforced by
    # resolve_column rather than assumed from the caller having checked it.
    disp_col = await resolve_column(
        db, source_table_id, name,
        model_id=model_id, project_id=project_id,
    )
    if disp_col.id == source_column_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The display column must differ from the key (source) column.",
        )
    return disp_col.id


def _attr_rel_response(
    rel: DimensionAttributeRelationship,
    *,
    column_names: dict[UUID, str],
    status_map: dict[UUID, tuple[str, Any]] | None = None,
) -> DimensionAttributeRelationshipResponse:
    """Serialise one declared relationship, resolving column names for the UI.

    ``verification_status`` projects the newest evidence row for this
    relationship whose ``declaration_hash`` still matches the current declaration
    (spec §5.3 denormalised current-status view). Evidence for a superseded
    declaration hash does not count — the relationship reads ``DECLARED`` again
    after any meaning-changing edit, mirroring the router trust predicate (§7.6.4)
    even though Phase 2 authorises no route. No evidence -> ``DECLARED``.
    """
    verification_status = "DECLARED"
    verified_at = None
    if status_map is not None:
        entry = status_map.get(rel.id)
        if entry is not None:
            verification_status, verified_at = entry
    return DimensionAttributeRelationshipResponse(
        id=rel.id,
        model_id=rel.model_id,
        dimension_id=rel.dimension_id,
        key_column_id=rel.key_column_id,
        key_column_name=column_names.get(rel.key_column_id) if rel.key_column_id else None,
        detail_column_id=rel.detail_column_id,
        detail_column_name=column_names.get(rel.detail_column_id) if rel.detail_column_id else None,
        cardinality=rel.cardinality,
        null_policy=rel.null_policy,
        enabled=rel.enabled,
        declaration_hash=rel.declaration_hash,
        verification_status=verification_status,
        verified_at=verified_at,
        created_at=rel.created_at,
        updated_at=rel.updated_at,
    )


def _attr_rels_from_rows(
    rows, column_names: dict[UUID, str],
    status_map: dict[UUID, tuple[str, Any]] | None = None,
) -> list[DimensionAttributeRelationshipResponse]:
    return [
        _attr_rel_response(r, column_names=column_names, status_map=status_map)
        for r in rows
    ]


async def _latest_verification_status(
    db, rels,
) -> dict[UUID, tuple[str, Any]]:
    """Project the newest verification-evidence status per relationship (§5.3).

    Only evidence whose ``declaration_hash`` matches the CURRENT declaration is
    projected — evidence for a superseded hash is ignored so an edited
    relationship reads ``DECLARED`` again (mirrors the §7.6.4 trust predicate;
    Phase 2 authorises no route but the health view follows the same rule).
    Returns {} when there is no evidence, so this adds no cost for freshly
    declared, never-deployed relationships.
    """
    from shared.db.models import DimensionAttributeVerification

    rel_ids = [r.id for r in rels]
    if not rel_ids:
        return {}
    rows = (
        await db.execute(
            select(
                DimensionAttributeVerification.relationship_id,
                DimensionAttributeVerification.status,
                DimensionAttributeVerification.declaration_hash,
                DimensionAttributeVerification.checked_at,
            )
            .where(DimensionAttributeVerification.relationship_id.in_(rel_ids))
            # ``id`` is the deterministic tie-break: two evidence rows for the
            # same relationship written in the same ``func.now()`` tick (e.g. a
            # deploy check and an artifact check) would otherwise project
            # nondeterministically. Both share the current hash so either is a
            # valid current status, but a stable order keeps the projection
            # reproducible across requests.
            .order_by(
                DimensionAttributeVerification.checked_at.desc(),
                DimensionAttributeVerification.id.desc(),
            )
        )
    ).all()
    current_hash = {r.id: r.declaration_hash for r in rels}
    out: dict[UUID, tuple[str, Any]] = {}
    for rel_id, status_, decl_hash, checked_at in rows:
        if rel_id in out:
            continue  # newest wins (ordered desc)
        if decl_hash != current_hash.get(rel_id):
            continue  # evidence for a superseded declaration — ignore
        verified_at = checked_at if status_ == "VERIFIED" else None
        out[rel_id] = (status_, verified_at)
    return out


async def _load_model_attribute_relationships(
    db, model_id: UUID,
) -> dict[UUID, list[DimensionAttributeRelationshipResponse]]:
    """Batch-load ALL declared relationships for a model, grouped by dimension.

    One query for the rows plus one for the referenced column names — no N+1
    across the dimension list. Returns an empty dict when the model has no
    declared relationship (the ordinary case), so list responses are unchanged
    for such models. This mirrors ``_load_redundant_partners`` and is patchable
    the same way in tests.
    """
    rows = (
        await db.execute(
            select(DimensionAttributeRelationship)
            .where(DimensionAttributeRelationship.model_id == model_id)
            .order_by(
                DimensionAttributeRelationship.created_at,
                DimensionAttributeRelationship.id,
            )
        )
    ).scalars().all()
    if not rows:
        return {}
    col_ids = {
        cid
        for r in rows
        for cid in (r.key_column_id, r.detail_column_id)
        if cid is not None
    }
    column_names: dict[UUID, str] = {}
    if col_ids:
        cols = (
            await db.execute(
                select(ModelColumn.id, ModelColumn.column_name).where(
                    ModelColumn.id.in_(list(col_ids))
                )
            )
        ).all()
        column_names = {cid: name for cid, name in cols}
    status_map = await _latest_verification_status(db, rows)
    out: dict[UUID, list[DimensionAttributeRelationshipResponse]] = {}
    for r in rows:
        out.setdefault(r.dimension_id, []).append(
            _attr_rel_response(r, column_names=column_names, status_map=status_map)
        )
    return out


async def _load_attribute_relationships(
    db, dimension_id: UUID,
) -> list[DimensionAttributeRelationshipResponse]:
    """Load ONE dimension's declared relationships with resolved column names.

    Used by the single-dimension response path and the relationship CRUD
    endpoints. Returns [] for a dimension with no declared relationship — the
    ordinary case — so the response is byte-identical to pre-feature.
    """
    rows = (
        await db.execute(
            select(DimensionAttributeRelationship)
            .where(DimensionAttributeRelationship.dimension_id == dimension_id)
            .order_by(
                DimensionAttributeRelationship.created_at,
                DimensionAttributeRelationship.id,
            )
        )
    ).scalars().all()
    if not rows:
        return []
    col_ids = {
        cid
        for r in rows
        for cid in (r.key_column_id, r.detail_column_id)
        if cid is not None
    }
    column_names: dict[UUID, str] = {}
    if col_ids:
        cols = (
            await db.execute(
                select(ModelColumn.id, ModelColumn.column_name).where(
                    ModelColumn.id.in_(list(col_ids))
                )
            )
        ).all()
        column_names = {cid: name for cid, name in cols}
    status_map = await _latest_verification_status(db, rows)
    return _attr_rels_from_rows(rows, column_names, status_map)


async def _build_response(
    db,
    dim: Dimension,
    redundant_partners: dict | None = None,
    warnings: list[str] | None = None,
    glossary_texts: dict[UUID, str] | None = None,
    restricted_cols: set[UUID] | None = None,
    attr_rels_by_dim: dict[UUID, list[DimensionAttributeRelationshipResponse]] | None = None,
) -> DimensionResponse:
    """Build a DimensionResponse enriched with column name and table id.

    Cascades the source column's `is_hidden` flag onto the dimension itself
    so the gateway can filter the catalog without a second round trip
    (Phase 1 of the semantic-layer plan).

    Resolves `effective_description` from the glossary precedence chain
    `approved glossary entry > dimension.description > none` so the gateway
    can surface curated glossary text in Excel tooltips automatically
    (Phase 4 of the semantic-layer plan).

    Attaches a ``redundant_partner`` hint when the source column is on the
    dim side of an inner/left join to the fact table. The aggregate
    picker uses this to disable the entry and suggest the fact-side
    canonical column.
    """
    col_name = None
    col_data_type = None
    table_id = None
    table_alias = None
    table_display_name = None
    uda_name = None
    is_hidden = False
    high_cardinality: bool | None = None
    cardinality_estimate: int | None = None
    display_col_name = None
    if dim.source_column_id:
        col = await db.get(ModelColumn, dim.source_column_id)
        if col:
            col_name = col.column_name
            col_data_type = col.data_type
            table_id = col.model_table_id
            is_hidden = bool(col.is_hidden)
            cardinality_estimate = col.cardinality_estimate
    # Bug-5434: resolve the distinct display column's name for the response so the
    # authoring UI / importers can round-trip it by name.
    if getattr(dim, "display_column_id", None):
        disp_col = await db.get(ModelColumn, dim.display_column_id)
        if disp_col:
            display_col_name = disp_col.column_name
    if dim.user_defined_attribute_id:
        uda = await db.get(UserDefinedAttribute, dim.user_defined_attribute_id)
        if uda:
            uda_name = uda.name
            table_id = uda.table_id
    if table_id is not None:
        mt = await db.get(ModelTable, table_id)
        if mt:
            table_alias = mt.alias
            table_display_name = mt.display_name
            if cardinality_estimate is not None and mt.row_count_estimate and mt.row_count_estimate > 0:
                high_cardinality = (cardinality_estimate / mt.row_count_estimate) > 0.5

    if glossary_texts is not None:
        glossary_text = glossary_texts.get(dim.id)
    else:
        # Bug-9392: no direct attachment falls back to the physical column's
        # term, matching the deployed snapshot (serialiser) exactly.
        glossary_text = await _glossary_text_for_target(
            db, dim.model_id, "dimension", dim.id,
            fallback_column_id=dim.source_column_id,
        )
    effective_description = glossary_text or dim.description

    partner_info = None
    if (
        redundant_partners is not None
        and dim.source_column_id is not None
        and dim.source_column_id in redundant_partners
    ):
        hint = redundant_partners[dim.source_column_id]
        # Bug-6141 (fail-closed): the redundant-partner hint echoes the partner
        # column's NAME (and a reason string built from it). If that partner
        # column is CLS-restricted for the effective persona, suppress the hint
        # entirely so a restricted column name does not leak on an otherwise
        # visible dimension.
        partner_restricted = (
            restricted_cols is not None
            and getattr(hint, "partner_column_id", None) in restricted_cols
        )
        if not partner_restricted:
            partner_info = RedundantPartnerInfo(
                partner_column_name=hint.partner_column_name,
                partner_table_name=hint.partner_table_name,
                partner_physical_table=hint.partner_physical_table,
                join_type=hint.join_type,
                reason=hint.reason,
            )

    # Provenance: resolve the owning dimension's name for display.
    detail_of_dim_name: str | None = None
    detail_of_rel_id = getattr(dim, "detail_of_relationship_id", None)
    detail_of_dim_id = getattr(dim, "detail_of_dimension_id", None)
    if detail_of_dim_id is not None:
        owning_dim = await db.get(Dimension, detail_of_dim_id)
        if owning_dim is not None:
            detail_of_dim_name = owning_dim.name

    return DimensionResponse(
        id=dim.id,
        model_id=dim.model_id,
        name=dim.name,
        display_name=dim.display_name,
        description=dim.description,
        effective_description=effective_description,
        display_folder=dim.display_folder,
        is_hidden=is_hidden,
        source_column_id=dim.source_column_id,
        source_column_name=col_name,
        display_column_id=getattr(dim, "display_column_id", None),
        display_column_name=display_col_name,
        data_type=col_data_type,
        source_table_id=table_id,
        source_table_alias=table_alias,
        source_table_display_name=table_display_name,
        user_defined_attribute_id=dim.user_defined_attribute_id,
        user_defined_attribute_name=uda_name,
        is_time_dim=dim.is_time_dim,
        time_grain=dim.time_grain,
        is_invalid=bool(getattr(dim, "is_invalid", False)),
        invalid_reason=getattr(dim, "invalid_reason", None),
        redundant_partner=partner_info,
        high_cardinality=high_cardinality,
        warnings=warnings or [],
        detail_of_relationship_id=detail_of_rel_id,
        detail_of_dimension_id=detail_of_dim_id,
        detail_of_dimension_name=detail_of_dim_name,
        # Use the prefetched per-model map when available (list path, no N+1);
        # otherwise load just this dimension's relationships (single-response
        # path). ``None`` map means "not prefetched"; an empty dict means
        # "prefetched, this model has none" -> [] without a query.
        attribute_relationships=(
            attr_rels_by_dim.get(dim.id, [])
            if attr_rels_by_dim is not None
            else await _load_attribute_relationships(db, dim.id)
        ),
        created_at=dim.created_at,
        updated_at=dim.updated_at,
    )


async def _load_uda_column_map(
    db, model_id: UUID,
) -> dict[UUID, set[UUID]]:
    """Load a mapping of UDA id -> set of referenced column ids for the model.

    Bug-7606: UDA-backed dimensions reference physical columns through
    ``user_defined_attribute_column_refs``. CLS metadata hiding must check
    these transitive column references, not just ``source_column_id``.
    Pre-loading avoids N+1 queries in the list endpoint.
    """
    rows = await db.execute(
        select(
            UserDefinedAttributeColumnRef.attribute_id,
            UserDefinedAttributeColumnRef.column_id,
        ).join(
            UserDefinedAttribute,
            UserDefinedAttributeColumnRef.attribute_id == UserDefinedAttribute.id,
        ).where(UserDefinedAttribute.model_id == model_id)
    )
    mapping: dict[UUID, set[UUID]] = {}
    for uda_id, col_id in rows.all():
        mapping.setdefault(uda_id, set()).add(col_id)
    return mapping


class _CalcClsContext:
    """Physical-name lookups for the calc-dimension CLS gate (Bug-7607).

    Mirrors the query-router runtime closure
    (``router._ClsClosure`` / ``_touches_restricted_columns`` calc branch) so
    the model-service catalogue applies the SAME name-disclosure rule the
    serving path applies to values. Built once per model by
    ``_load_calc_cls_context`` only when the persona has restrictions AND a
    served dimension carries a ``calc_expression``.

    - ``restricted_names``: physical column names (lowercased) the persona is
      CLS-restricted from — a calc expression naming one leaks the name.
    - ``known_names``: EVERY physical column name in the model (lowercased) —
      an identifier that is not a known column may be a whole-row/table
      reference that would serialise restricted columns, so fail closed.
    - ``table_identifiers``: model table physical-names + aliases (lowercased)
      — an identifier matching one is a whole-row reference even if a
      same-named column also exists.
    """

    __slots__ = ("restricted_names", "known_names", "table_identifiers")

    def __init__(
        self,
        restricted_names: set[str],
        known_names: set[str],
        table_identifiers: set[str],
    ) -> None:
        self.restricted_names = restricted_names
        self.known_names = known_names
        self.table_identifiers = table_identifiers


async def _load_calc_cls_context(
    db, model_id: UUID, restricted_cols: set[UUID],
) -> _CalcClsContext:
    """Load the physical-name lookups the calc-dimension CLS gate needs.

    Bug-7607: the catalogue hide-predicate must parse a calc dimension's
    ``calc_expression`` and match referenced column NAMES against the restricted
    set, exactly as the query-router value-blocking gate does. This loads the
    three name sets (restricted / all-known / table identifiers) in three small
    model-scoped queries. Called only when restrictions apply and a served
    dimension is a calc dimension, so plain source-column models pay nothing.
    """
    restricted_names: set[str] = set()
    if restricted_cols:
        rows = (
            await db.execute(
                select(ModelColumn.column_name).where(
                    ModelColumn.id.in_(list(restricted_cols))
                )
            )
        ).scalars().all()
        restricted_names = {str(n).lower() for n in rows if n}

    known_rows = (
        await db.execute(
            select(ModelColumn.column_name)
            .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
            .where(ModelTable.model_id == model_id)
        )
    ).scalars().all()
    known_names = {str(n).lower() for n in known_rows if n}

    table_rows = (
        await db.execute(
            select(ModelTable.physical_name, ModelTable.alias).where(
                ModelTable.model_id == model_id
            )
        )
    ).all()
    table_identifiers: set[str] = set()
    for phys, alias in table_rows:
        if phys:
            table_identifiers.add(str(phys).lower())
        if alias:
            table_identifiers.add(str(alias).lower())

    return _CalcClsContext(restricted_names, known_names, table_identifiers)


def _restricted_uda_ids(
    restricted_cols: set[UUID],
    uda_col_map: dict[UUID, set[UUID]] | None,
) -> set[str]:
    """UDA ids whose referenced columns intersect the restricted set (as str).

    The shared closure keys UDA restriction by UDA id; collapse the per-UDA
    column map here (Bug-7606 / Bug-7608).
    """
    if not uda_col_map or not restricted_cols:
        return set()
    restricted = set(restricted_cols)
    return {
        str(uda_id)
        for uda_id, cols in uda_col_map.items()
        if cols & restricted
    }


def _calc_cls_context_to_shared(ctx: _CalcClsContext) -> ClosureContext:
    """Adapt the catalogue ``_CalcClsContext`` to the shared ``ClosureContext``.

    Bug-7608 / Bug-7045: the calc-expression gate lives in the shared closure
    module; the model-service loads the three name sets into ``_CalcClsContext``,
    which this maps onto the shared context fields.
    """
    return ClosureContext(
        restricted_physical_names=ctx.restricted_names,
        known_physical_names=ctx.known_names,
        table_identifiers=ctx.table_identifiers,
    )


def _calc_expression_touches_restricted(
    calc_expr: str, ctx: _CalcClsContext,
) -> bool:
    """True when a calc dimension's expression references a restricted column.

    Bug-7607 (catalogue half): mirrors the query-router runtime calc-dimension
    gate. Bug-7608 / Bug-7045: both halves now delegate to the shared
    ``calc_expression_touches_restricted`` so the name-disclosure rule the
    catalogue applies and the value-blocking rule the serving path applies are
    literally the same code. Fail-closed on parse error, star, restricted name,
    table (whole-row) reference, or unknown identifier.
    """
    return calc_expression_touches_restricted(
        calc_expr, _calc_cls_context_to_shared(ctx),
    )


def _dim_touches_restricted_column(
    dim,
    restricted_cols: set[UUID],
    uda_col_map: dict[UUID, set[UUID]] | None = None,
    calc_ctx: "_CalcClsContext | None" = None,
) -> bool:
    """True when a dimension is backed by a CLS-restricted column.

    Fail-closed: a dimension whose key OR display column is restricted for
    the persona must be hidden entirely, because ``_build_response`` echoes
    both column names and would otherwise leak a restricted column name.

    Bug-7606: UDA-backed dimensions have ``source_column_id=None`` but
    reference physical columns through their UDA expression column refs.
    If ANY of those columns is CLS-restricted, the dimension must be hidden.
    The caller must pre-load ``uda_col_map`` via ``_load_uda_column_map``
    when restricting.

    Bug-7607: a CALCULATED dimension has ``source_column_id=None`` but its
    ``calc_expression`` may reference a restricted physical column by name.
    The catalogue must hide the dimension so the restricted column NAME does
    not leak (the query-router already blocks the VALUES at serving). The
    caller must pre-load ``calc_ctx`` via ``_load_calc_cls_context`` when
    restricting; a calc dimension reached with no context fails closed.

    Bug-7608 / Bug-7045: the direct / display / UDA / calc-expression closure
    delegates to the shared ``object_touches_restricted``. The one model-service
    guard kept here is the fail-closed on a calc dimension reached with no
    ``calc_ctx`` loaded (the shared calc branch requires the physical-name sets).
    """
    if not restricted_cols:
        return False

    # A calc dimension with no name context under an active restriction cannot
    # be verified — fail closed (kept ahead of the shared delegation).
    calc_expr = getattr(dim, "calc_expression", None)
    if calc_expr and calc_ctx is None:
        return True

    ctx = ClosureContext(
        restricted_uda_ids=_restricted_uda_ids(restricted_cols, uda_col_map),
    )
    if calc_ctx is not None:
        ctx.restricted_physical_names = calc_ctx.restricted_names
        ctx.known_physical_names = calc_ctx.known_names
        ctx.table_identifiers = calc_ctx.table_identifiers
    return object_touches_restricted(dim, normalise_id_set(restricted_cols), ctx)


async def _load_redundant_partners(db, model_id: UUID) -> dict:
    """Load model joins / tables / columns and compute the partner map."""
    tables_result = await db.execute(
        select(ModelTable).where(ModelTable.model_id == model_id)
    )
    tables = {t.id: t for t in tables_result.scalars().all()}
    joins_result = await db.execute(
        select(Join).where(Join.model_id == model_id)
    )
    joins = list(joins_result.scalars().all())
    col_ids: set = set()
    for j in joins:
        col_ids.add(j.left_column_id)
        col_ids.add(j.right_column_id)
    columns: dict = {}
    if col_ids:
        cols_result = await db.execute(
            select(ModelColumn).where(ModelColumn.id.in_(list(col_ids)))
        )
        columns = {c.id: c for c in cols_result.scalars().all()}
    return compute_redundant_partners(joins, tables, columns)


# F-018-20: `_glossary_text_for_target` was copy-pasted here and in measures.py.
# It now lives in `_scope.py` and is imported above (aliased to the same name).


@router.post(
    "",
    response_model=DimensionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_dimension(
    project_id: UUID,
    model_id: UUID,
    body: DimensionCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> DimensionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        if body.user_defined_attribute_id and (body.source_table_id or body.source_column_name):
            raise HTTPException(
                status_code=400,
                detail="Provide either source column fields or user_defined_attribute_id, not both",
            )
        source_column_id = None
        user_defined_attribute_id = body.user_defined_attribute_id
        if body.source_table_id and body.source_column_name:
            col = await resolve_column(
                db,
                body.source_table_id,
                body.source_column_name,
                body.data_type or "unknown",
                model_id=model_id,
                project_id=project_id,
            )
            source_column_id = col.id
        elif user_defined_attribute_id:
            uda = await db.get(UserDefinedAttribute, user_defined_attribute_id)
            if uda is None or uda.model_id != model_id:
                raise HTTPException(status_code=404, detail="User-defined attribute not found")

        # Bug-5434: resolve the optional distinct DISPLAY column. Only a
        # physical-column flat dimension may carry one, and it must differ from
        # the key column.
        display_column_id = await _resolve_display_column_id(
            db,
            display_column_name=body.display_column_name,
            source_table_id=body.source_table_id,
            source_column_id=source_column_id,
            user_defined_attribute_id=user_defined_attribute_id,
            model_id=model_id,
            project_id=project_id,
        )

        dim = Dimension(
            model_id=model_id,
            name=body.name,
            display_name=body.display_name or body.name,
            source_column_id=source_column_id,
            display_column_id=display_column_id,
            user_defined_attribute_id=user_defined_attribute_id,
            is_time_dim=body.is_time_dim,
            time_grain=body.time_grain,
        )
        db.add(dim)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if "dimensions_model_id_name_key" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"A dimension named '{body.name}' already exists in this model.",
                )
            raise
        await db.refresh(dim)
        return await _build_response(db, dim)


@router.get("", response_model=list[DimensionResponse])
async def list_dimensions(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[DimensionResponse]:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        stmt = select(Dimension).where(Dimension.model_id == model_id)
        if persona:
            allowed = parse_allowed_ids(persona.included_dimension_ids)
            if allowed is not None:
                stmt = stmt.where(Dimension.id.in_(allowed))
        stmt = stmt.order_by(Dimension.name)
        partners = await _load_redundant_partners(db, model_id)
        result = await db.execute(stmt)
        dims = result.scalars().all()
        restricted_cols: set[UUID] = set()
        uda_col_map: dict[UUID, set[UUID]] | None = None
        calc_ctx: _CalcClsContext | None = None
        if persona:
            restricted_cols = await get_restricted_column_ids(db, persona.id)
            # Bug-7606: load UDA column refs so UDA-backed dimensions
            # are CLS-checked against the columns their expression
            # references — not just source_column_id / display_column_id.
            if restricted_cols:
                uda_col_map = await _load_uda_column_map(db, model_id)
                # Bug-7607: load the physical-name context for the calc-dimension
                # gate only when a served dimension is a calc dimension, so plain
                # source-column models pay no extra query.
                if any(getattr(d, "calc_expression", None) for d in dims):
                    calc_ctx = await _load_calc_cls_context(
                        db, model_id, restricted_cols
                    )
            dims = [
                d for d in dims
                if not _dim_touches_restricted_column(
                    d, restricted_cols, uda_col_map, calc_ctx
                )
            ]
        glossary_texts = await _glossary_texts_for_targets(
            db, model_id, "dimension", [d.id for d in dims],
            fallback_column_ids={d.id: d.source_column_id for d in dims},
        )
        # Prefetch declared attribute relationships once for the whole model so
        # the per-dimension response build does not fire an extra query each.
        attr_rels_by_dim = await _load_model_attribute_relationships(db, model_id)
        return [
            await _build_response(
                db, d, partners, glossary_texts=glossary_texts,
                restricted_cols=restricted_cols,
                attr_rels_by_dim=attr_rels_by_dim,
            )
            for d in dims
        ]


@router.get("/{dimension_id}", response_model=DimensionResponse)
async def get_dimension(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> DimensionResponse:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        d = await db.get(Dimension, dimension_id)
        if d is None or d.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        restricted_cols: set[UUID] = set()
        if persona:
            allowed = parse_allowed_ids(persona.included_dimension_ids)
            if allowed is not None and d.id not in allowed:
                raise HTTPException(status_code=404, detail="Dimension not found")
            restricted_cols = await get_restricted_column_ids(db, persona.id)
            # Bug-7606: load UDA column refs so UDA-backed dimensions
            # are CLS-checked against the columns their expression references.
            uda_col_map = await _load_uda_column_map(db, model_id) if restricted_cols else None
            # Bug-7607: load the calc-dimension name context so a calculated
            # dimension referencing a restricted column is hidden (404) rather
            # than disclosing the restricted column NAME via the catalogue.
            calc_ctx = (
                await _load_calc_cls_context(db, model_id, restricted_cols)
                if restricted_cols and getattr(d, "calc_expression", None)
                else None
            )
            if _dim_touches_restricted_column(d, restricted_cols, uda_col_map, calc_ctx):
                raise HTTPException(status_code=404, detail="Dimension not found")
        partners = await _load_redundant_partners(db, model_id)
        return await _build_response(db, d, partners, restricted_cols=restricted_cols)


@router.patch(
    "/{dimension_id}",
    response_model=DimensionResponse,
    dependencies=[require_role("modeler")],
)
async def update_dimension(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    body: DimensionUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> DimensionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        d = await db.get(Dimension, dimension_id)
        if d is None or d.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        updates = body.model_dump(exclude_unset=True)
        if "user_defined_attribute_id" in updates and (
            "source_table_id" in updates or "source_column_name" in updates
        ):
            raise HTTPException(
                status_code=400,
                detail="Provide either source column fields or user_defined_attribute_id, not both",
            )
        # Bug-5434: snapshot the key column's table BEFORE any source change so a
        # later table move can drop a now-cross-table display column.
        _prev_key_table_id = await _source_table_id_for_dim(db, d)
        # Resolve column name if provided
        if "source_table_id" in updates and "source_column_name" in updates:
            table_id = updates.pop("source_table_id")
            col_name = updates.pop("source_column_name")
            if table_id and col_name:
                col = await resolve_column(
                    db, table_id, col_name,
                    model_id=model_id, project_id=project_id,
                )
                d.source_column_id = col.id
                d.user_defined_attribute_id = None
            else:
                d.source_column_id = None
        else:
            updates.pop("source_table_id", None)
            updates.pop("source_column_name", None)
        if "user_defined_attribute_id" in updates:
            uda_id = updates["user_defined_attribute_id"]
            if uda_id:
                uda = await db.get(UserDefinedAttribute, uda_id)
                if uda is None or uda.model_id != model_id:
                    raise HTTPException(status_code=404, detail="User-defined attribute not found")
                d.source_column_id = None
        # Bug-5434: resolve / clear the distinct DISPLAY column. Re-resolve against
        # the dimension's CURRENT source binding (after any source change above).
        # Switching to a UDA binding or clearing the key column also clears the
        # display column so it never dangles on a non-physical dimension.
        if "display_column_name" in updates:
            disp_name = updates.pop("display_column_name", None)
            disp_table_id = await _source_table_id_for_dim(db, d)
            d.display_column_id = await _resolve_display_column_id(
                db,
                display_column_name=disp_name,
                source_table_id=disp_table_id,
                source_column_id=d.source_column_id,
                user_defined_attribute_id=d.user_defined_attribute_id,
                model_id=model_id,
                project_id=project_id,
            )
        elif d.source_column_id is None:
            # Source binding moved off a physical column — drop any stale display.
            d.display_column_id = None
        elif getattr(d, "display_column_id", None) is not None:
            # Key column moved to a DIFFERENT table while display_column_name was
            # not in the payload — a display column bound to the old table would
            # dangle / force an unintended join at discovery. Drop it when the key
            # table changed.
            _new_key_table_id = await _source_table_id_for_dim(db, d)
            if _new_key_table_id != _prev_key_table_id:
                d.display_column_id = None
        new_name = updates.get("name")
        old_name = d.name
        if new_name and "display_name" not in updates and (not d.display_name or d.display_name == d.name):
            updates["display_name"] = new_name
        if "display_name" in updates and (updates["display_name"] is None or not str(updates["display_name"]).strip()):
            updates["display_name"] = new_name or d.name
        for k, v in updates.items():
            setattr(d, k, v)
        if new_name and new_name != old_name:
            agg_result = await db.execute(
                select(AggregateDefinition).where(
                    AggregateDefinition.model_id == model_id
                )
            )
            for agg in agg_result.scalars().all():
                if isinstance(agg.grain, list) and old_name in agg.grain:
                    agg.grain = [
                        new_name if g == old_name else g for g in agg.grain
                    ]
                    # Physical columns in the materialized table still use
                    # the old name — mark aggregate pending so it is rebuilt
                    # before routing uses the new layout.
                    if agg.status == "active":
                        agg.status = "pending"
            persona_result = await db.execute(
                select(Persona).where(Persona.model_id == model_id)
            )
            for persona in persona_result.scalars().all():
                df = persona.default_filters
                if isinstance(df, dict) and old_name in df:
                    df[new_name] = df.pop(old_name)
                    persona.default_filters = df
        rename_warnings: list[str] = []
        if new_name and new_name != old_name:
            hlevel_result = await db.execute(
                select(HierarchyLevel.name, HierarchyDefinition.name)
                .join(HierarchyDefinition, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
                .where(HierarchyDefinition.model_id == model_id)
                .where(HierarchyLevel.name == old_name)
            )
            for lvl_name, hier_name in hlevel_result.all():
                rename_warnings.append(
                    f"Hierarchy level '{lvl_name}' on hierarchy "
                    f"'{hier_name}' still uses the old name"
                )
        # If the source binding changed, re-run the model validator so
        # the dim and any aggregates that reference it flip between
        # invalid/valid based on the new structural state.
        if (
            "source_table_id" in body.model_dump(exclude_unset=True)
            or "source_column_name" in body.model_dump(exclude_unset=True)
            or "user_defined_attribute_id" in body.model_dump(exclude_unset=True)
        ):
            from shared.semantic.model_validator import revalidate_model

            await db.flush()
            await revalidate_model(model_id, db)
        await db.commit()
        await db.refresh(d)
        return await _build_response(db, d, warnings=rename_warnings)


@router.delete(
    "/{dimension_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_dimension(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    from shared.semantic.model_validator import revalidate_model
    from src.api.personas import strip_id_from_personas
    from src.api.dimension_detail_lifecycle import check_detail_provenance_lock

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        d = await db.get(Dimension, dimension_id)
        if d is None or d.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")

        # Bug-7787 Phase 3: impact guard — block or require acknowledgement
        # before proceeding with the delete.
        from src.dependencies.guard import evaluate_delete_impact

        await evaluate_delete_impact(
            db, current_user.tenant_id, project_id, model_id,
            "dimension", dimension_id, request=request,
        )
        # Referential lock: refuse to delete an auto-added detail dimension while
        # its source relationship is still active.
        await check_detail_provenance_lock(db, d)
        await strip_id_from_personas(
            db, model_id=model_id, object_id=dimension_id, object_class="dimension"
        )
        # Bug-5607: clean stale keys from persona default_filters when a
        # dimension is deleted. default_filters is keyed by dimension NAME,
        # so remove the deleted dimension's name from every persona.
        dim_name = d.name
        persona_result = await db.execute(
            select(Persona).where(Persona.model_id == model_id)
        )
        for persona in persona_result.scalars().all():
            df = persona.default_filters
            if isinstance(df, dict) and dim_name in df:
                # Copy the dict so SQLAlchemy detects the mutation (JSONB
                # column tracking compares object identity, not contents).
                updated = {k: v for k, v in df.items() if k != dim_name}
                persona.default_filters = updated
        # Soft-referencing translation/preference rows have no FK back to the
        # dimension and would otherwise linger forever (F-029-15).
        await purge_entity_soft_references(db, model_id=model_id, entity_id=dimension_id)
        await db.delete(d)
        await db.flush()
        await revalidate_model(model_id, db)
        await db.commit()


# ---------------------------------------------------------------------------
# Bulk rename — model-level endpoint, shared between dimensions and measures
# ---------------------------------------------------------------------------

def _auto_display_name(name: str) -> str:
    """Auto-generate display_name from a snake_case identifier."""
    return name.replace("_", " ").title()


class BulkRenameItem(BaseModel):
    type: str    # "dimension" | "measure"
    id: UUID
    name: str


class BulkRenameRequest(BaseModel):
    renames: list[BulkRenameItem]


class BulkRenameResult(BaseModel):
    renamed: int


@bulk_router.post(
    "/bulk-rename-attributes",
    response_model=BulkRenameResult,
    dependencies=[require_role("modeler")],
)
async def bulk_rename_attributes(
    project_id: UUID,
    model_id: UUID,
    body: BulkRenameRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> BulkRenameResult:
    """Atomically rename a batch of dimensions and/or measures.

    Validates model-wide uniqueness before committing.  Preserves
    display_name when it was manually customised (i.e. differs from
    the auto-generated prettification of the current name).
    """
    if not body.renames:
        return BulkRenameResult(renamed=0)

    # --- basic validation ---
    incoming_names = [r.name.strip() for r in body.renames]
    for n in incoming_names:
        if not n:
            raise HTTPException(status_code=422, detail="Rename name must not be empty.")
        if len(n) > 255:
            raise HTTPException(status_code=422, detail=f"Name too long: {n!r}")

    if len(set(n.lower() for n in incoming_names)) != len(incoming_names):
        raise HTTPException(
            status_code=422, detail="Duplicate names within the request."
        )

    dim_ids = {r.id for r in body.renames if r.type == "dimension"}
    meas_ids = {r.id for r in body.renames if r.type == "measure"}

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock

        # Build the set of names already taken by attrs NOT in this batch.
        existing_dim_names = set(
            (
                await db.execute(
                    select(Dimension.name).where(
                        Dimension.model_id == model_id,
                        Dimension.id.not_in(dim_ids) if dim_ids else True,
                    )
                )
            ).scalars().all()
        )
        existing_meas_names = set(
            (
                await db.execute(
                    select(Measure.name).where(
                        Measure.model_id == model_id,
                        Measure.id.not_in(meas_ids) if meas_ids else True,
                    )
                )
            ).scalars().all()
        )
        taken = {n.lower() for n in existing_dim_names | existing_meas_names}

        for n in incoming_names:
            if n.lower() in taken:
                raise HTTPException(
                    status_code=409,
                    detail=f"An attribute named '{n}' already exists in this model.",
                )
            taken.add(n.lower())  # reserve within the batch

        # Apply renames.
        rename_map = {r.id: r.name.strip() for r in body.renames}
        renamed = 0

        measure_renames: dict[UUID, tuple[str, str]] = {}
        for meas_id in meas_ids:
            measure = await db.get(Measure, meas_id)
            if measure is None or measure.model_id != model_id:
                raise HTTPException(
                    status_code=404, detail=f"Measure {meas_id} not found."
                )
            measure_renames[meas_id] = (measure.name, rename_map[meas_id])
        try:
            await propagate_measure_renames(db, model_id, measure_renames)
        except UnsafeMeasureRename as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=exc.detail_for(current_user.email or current_user.user_id),
            ) from exc

        for dim_id in dim_ids:
            d = await db.get(Dimension, dim_id)
            if d is None or d.model_id != model_id:
                raise HTTPException(status_code=404, detail=f"Dimension {dim_id} not found.")
            new_name = rename_map[dim_id]
            if _auto_display_name(d.name) == (d.display_name or ""):
                d.display_name = _auto_display_name(new_name)
            d.name = new_name
            renamed += 1

        for meas_id in meas_ids:
            m = await db.get(Measure, meas_id)
            if m is None or m.model_id != model_id:
                raise HTTPException(status_code=404, detail=f"Measure {meas_id} not found.")
            new_name = rename_map[meas_id]
            if _auto_display_name(m.name) == (m.display_name or ""):
                m.display_name = _auto_display_name(new_name)
            m.name = new_name
            renamed += 1

        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if "model_id_name_key" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail="One or more names conflict with existing attributes.",
                )
            raise

        await db.commit()
        return BulkRenameResult(renamed=renamed)


# ---------------------------------------------------------------------------
# Dimension attribute relationships (derived-grain routing, spec section 5.3)
# ---------------------------------------------------------------------------
# CRUD for the modeller-declared key-to-detail relationships. Phase 1b persists
# and returns the declaration only -- NO serving, NO verification. These are kept
# strictly separate from ``display_column_id`` (a caption choice); declaring a
# relationship never touches the display column and vice versa.


async def _lookup_existing_column(db, table_id: UUID, column_name: str) -> ModelColumn:
    """Look up an EXISTING physical column by name in a table (reject on miss).

    Unlike the shared ``resolve_column`` (which CREATES a column when missing),
    a relationship declaration must reference a column that already exists in the
    governed relation (spec §5.3/§7.6.1): fabricating a phantom ``unknown``-typed
    column would later feed the Phase-2 verifier a column with no real data.
    """
    col = (
        await db.execute(
            select(ModelColumn).where(
                ModelColumn.model_table_id == table_id,
                ModelColumn.column_name == column_name,
            )
        )
    ).scalar_one_or_none()
    if col is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Column {column_name!r} does not exist in the dimension's source table.",
        )
    return col


async def _resolve_relationship_key(
    db, *, dim: Dimension, key_column_name: str | None,
) -> UUID:
    """Resolve the pinned key column id for a declaration.

    An explicit ``key_column_name`` resolves within the dimension's key table;
    otherwise the dimension's current physical key column is pinned. Raises 422
    when the dimension has no physical key column.
    """
    key_table_id = await _source_table_id_for_dim(db, dim)
    if key_column_name:
        if key_table_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="An explicit key column requires a physical-column dimension.",
            )
        return (await _lookup_existing_column(db, key_table_id, key_column_name)).id
    if dim.source_column_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "This dimension has no physical key column; a key-to-detail "
                "relationship requires a physical-column dimension."
            ),
        )
    return dim.source_column_id


async def _resolve_relationship_detail(
    db, *, dim: Dimension, detail_column_name: str, key_column_id: UUID,
    model_id: UUID | None = None,
) -> UUID:
    """Resolve the detail column id, rejecting a detail equal to the key.

    First checks the dimension's source table; if not found and model_id is
    provided, also checks tables joined to the dimension (fact-table details).
    """
    detail_table_id = await _source_table_id_for_dim(db, dim)
    if detail_table_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A detail column requires a physical-column dimension.",
        )
    # Try the dimension table first.
    detail_col = (
        await db.execute(
            select(ModelColumn).where(
                ModelColumn.model_table_id == detail_table_id,
                ModelColumn.column_name == detail_column_name,
            )
        )
    ).scalar_one_or_none()
    # If not found, try the joined fact table (fact-side details).
    if detail_col is None and model_id is not None:
        joins_result = await db.execute(
            select(Join).where(Join.model_id == model_id)
        )
        for j in joins_result.scalars().all():
            other_table_id = None
            if j.left_table_id == detail_table_id:
                other_table_id = j.right_table_id
            elif j.right_table_id == detail_table_id:
                other_table_id = j.left_table_id
            if other_table_id is not None:
                detail_col = (
                    await db.execute(
                        select(ModelColumn).where(
                            ModelColumn.model_table_id == other_table_id,
                            ModelColumn.column_name == detail_column_name,
                        )
                    )
                ).scalar_one_or_none()
                if detail_col is not None:
                    break
    if detail_col is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Column {detail_column_name!r} does not exist in the dimension's source table or joined tables.",
        )
    if detail_col.id == key_column_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The detail column must differ from the key column.",
        )
    return detail_col.id


async def _resolve_relationship_columns(
    db,
    *,
    dim: Dimension,
    model_id: UUID,
    detail_column_name: str,
    key_column_name: str | None,
) -> tuple[UUID, UUID]:
    """Resolve key + detail column ids for a NEW relationship declaration.

    The key defaults to the dimension's current key column when not supplied.
    Both must be EXISTING physical columns in the governed relation (spec §5.3).
    Raises HTTP 422 on an invalid declaration.
    """
    key_column_id = await _resolve_relationship_key(
        db, dim=dim, key_column_name=key_column_name
    )
    detail_column_id = await _resolve_relationship_detail(
        db, dim=dim, detail_column_name=detail_column_name,
        key_column_id=key_column_id, model_id=model_id,
    )
    return key_column_id, detail_column_id


@router.get(
    "/{dimension_id}/attribute-relationships",
    response_model=list[DimensionAttributeRelationshipResponse],
)
async def list_attribute_relationships(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[DimensionAttributeRelationshipResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        dim = await db.get(Dimension, dimension_id)
        if dim is None or dim.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        return await _load_attribute_relationships(db, dimension_id)


# ---------------------------------------------------------------------------
# Advisory validate endpoint (1:1 pre-check on the dimension source table)
# ---------------------------------------------------------------------------

class ValidateDetailColumnSpec(BaseModel):
    name: str
    table_id: str


class ValidateDetailColumnsRequest(BaseModel):
    detail_columns: list[ValidateDetailColumnSpec]


class ValidateDetailColumnResult(BaseModel):
    column: str
    table_id: str | None = None
    is_bijection: bool
    reason: str
    error: str | None = None


@router.post(
    "/{dimension_id}/attribute-relationships/validate",
    response_model=list[ValidateDetailColumnResult],
    dependencies=[require_role("modeler")],
)
async def validate_detail_columns(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    body: ValidateDetailColumnsRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ValidateDetailColumnResult]:
    """Advisory 1:1 pre-check on candidate detail columns.

    Runs forward + reverse + null checks against the table the detail column
    lives on (dimension table or fact table) via the governed source executor.
    ADVISORY ONLY -- never writes verification evidence and never grants serving.
    """
    from src.api.dimension_detail_lifecycle import validate_detail_columns as _validate

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        dim = await db.get(Dimension, dimension_id)
        if dim is None or dim.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        # Body-supplied table ids. ``_validate`` looks a ModelColumn up by
        # (model_table_id, column_name) with no owner check and then feeds the
        # resolved column NAME into SQL executed against THIS model's source
        # connection. Unguarded that is both a cross-project existence oracle
        # ("column not found" vs a real probe result) and a path for a foreign
        # model's column name to reach a governed query. Prove every table
        # belongs to the path project+model first; the whole request fails
        # closed rather than the offenders being silently skipped.
        #
        # The guard runs on the RAW strings, before ``UUID(...)``. The schema
        # types ``table_id`` as ``str``, so a value that is not a UUID at all
        # reached the constructor and raised an unhandled ValueError (HTTP 500).
        # The primitive normalises ids itself and answers the same 422 for a
        # malformed id as for a foreign one, which both removes the 500 and
        # keeps the malformed case from being a distinguishable response.
        await ensure_refs_in_model(
            db,
            ModelTable,
            ref_ids=[s.table_id for s in body.detail_columns],
            model_id=model_id,
            project_id=project_id,
            field_name="detail_columns[].table_id",
            noun="a table in this model",
        )
        specs = [(s.name, UUID(s.table_id)) for s in body.detail_columns]
        results = await _validate(db, dim, specs, model_id)
        return [ValidateDetailColumnResult(**r) for r in results]


@router.post(
    "/{dimension_id}/attribute-relationships",
    response_model=DimensionAttributeRelationshipResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_attribute_relationship(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    body: DimensionAttributeRelationshipCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> DimensionAttributeRelationshipResponse:
    from src.api.dimension_detail_lifecycle import (
        auto_add_detail_dimension,
        sync_detail_to_pair,
    )

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        dim = await db.get(Dimension, dimension_id)
        if dim is None or dim.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        key_column_id, detail_column_id = await _resolve_relationship_columns(
            db,
            dim=dim,
            model_id=model_id,
            detail_column_name=body.detail_column_name,
            key_column_name=body.key_column_name,
        )
        declaration_hash = compute_declaration_hash(
            key_column_id=str(key_column_id),
            detail_column_id=str(detail_column_id),
            cardinality=body.cardinality,
            null_policy="REJECT_NULL",
        )
        rel = DimensionAttributeRelationship(
            model_id=model_id,
            dimension_id=dimension_id,
            key_column_id=key_column_id,
            detail_column_id=detail_column_id,
            cardinality=body.cardinality,
            null_policy="REJECT_NULL",
            enabled=body.enabled,
            declaration_hash=declaration_hash,
        )
        db.add(rel)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if "uq_dim_attr_rel_dimension_detail_cardinality" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A relationship for this detail column and cardinality "
                        "already exists on this dimension."
                    ),
                )
            raise

        # Auto-add dimension for bijection detail (provenance).
        if body.cardinality == "BIJECTION" and detail_column_id is not None:
            detail_col = await db.get(ModelColumn, detail_column_id)
            if detail_col is not None:
                await auto_add_detail_dimension(
                    db,
                    model_id=model_id,
                    owning_dimension=dim,
                    relationship=rel,
                    detail_column=detail_col,
                )
                # Symmetric sync across the fact-dimension pair.
                await sync_detail_to_pair(
                    db,
                    model_id=model_id,
                    owning_dimension=dim,
                    relationship=rel,
                    detail_column=detail_col,
                )

        await db.commit()
        await db.refresh(rel)
        rels = await _load_attribute_relationships(db, dimension_id)
        for r in rels:
            if r.id == rel.id:
                return r
        return _attr_rel_response(rel, column_names={})


@router.patch(
    "/{dimension_id}/attribute-relationships/{relationship_id}",
    response_model=DimensionAttributeRelationshipResponse,
    dependencies=[require_role("modeler")],
)
async def update_attribute_relationship(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    relationship_id: UUID,
    body: DimensionAttributeRelationshipUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> DimensionAttributeRelationshipResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        dim = await db.get(Dimension, dimension_id)
        if dim is None or dim.model_id != model_id:
            raise HTTPException(status_code=404, detail="Dimension not found")
        rel = await db.get(DimensionAttributeRelationship, relationship_id)
        if rel is None or rel.dimension_id != dimension_id or rel.model_id != model_id:
            raise HTTPException(status_code=404, detail="Relationship not found")
        updates = body.model_dump(exclude_unset=True)

        meaning_changed = any(
            k in updates for k in ("detail_column_name", "key_column_name", "cardinality")
        )
        if meaning_changed:
            # Spec §5.3 / §7.6.1: key_column_id is PINNED. ONLY an explicit
            # key_column_name in THIS request may retarget it; a cardinality- or
            # detail-only edit MUST preserve the stored key so a later dimension
            # key-rebind cannot silently retarget a declared (and, in Phase 2,
            # verified) edge.
            if "key_column_name" in updates:
                rel.key_column_id = await _resolve_relationship_key(
                    db, dim=dim, key_column_name=updates["key_column_name"]
                )
            # Detail: re-resolve when supplied; otherwise re-validate the existing
            # detail against the (possibly new) key so key!=detail still holds.
            if "detail_column_name" in updates:
                detail_name = updates["detail_column_name"]
            else:
                _dc = (
                    await db.get(ModelColumn, rel.detail_column_id)
                    if rel.detail_column_id is not None
                    else None
                )
                detail_name = _dc.column_name if _dc is not None else ""
            if detail_name and rel.key_column_id is not None:
                rel.detail_column_id = await _resolve_relationship_detail(
                    db, dim=dim, detail_column_name=detail_name,
                    key_column_id=rel.key_column_id,
                    model_id=model_id,
                )
            if "cardinality" in updates:
                rel.cardinality = updates["cardinality"]
            rel.declaration_hash = compute_declaration_hash(
                key_column_id=str(rel.key_column_id) if rel.key_column_id else None,
                detail_column_id=str(rel.detail_column_id) if rel.detail_column_id else None,
                cardinality=rel.cardinality,
                null_policy=rel.null_policy,
            )
        if "enabled" in updates:
            rel.enabled = updates["enabled"]

        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if "uq_dim_attr_rel_dimension_detail_cardinality" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A relationship for this detail column and cardinality "
                        "already exists on this dimension."
                    ),
                )
            raise
        await db.refresh(rel)
        rels = await _load_attribute_relationships(db, dimension_id)
        for r in rels:
            if r.id == rel.id:
                return r
        return _attr_rel_response(rel, column_names={})


class RelationshipDownstreamUsageResponse(BaseModel):
    linked_dimensions: list[dict]
    affected_aggregates: list[dict]


@router.get(
    "/{dimension_id}/attribute-relationships/{relationship_id}/downstream-usage",
    response_model=RelationshipDownstreamUsageResponse,
    dependencies=[require_role("modeler")],
)
async def get_relationship_downstream_usage(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    relationship_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> RelationshipDownstreamUsageResponse:
    """Preview downstream usage before deleting a relationship.

    Returns linked auto-added dimensions and affected aggregates so the UI
    can show a confirmation dialog.
    """
    from src.api.dimension_detail_lifecycle import compute_relationship_downstream_usage

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        rel = await db.get(DimensionAttributeRelationship, relationship_id)
        if rel is None or rel.dimension_id != dimension_id or rel.model_id != model_id:
            raise HTTPException(status_code=404, detail="Relationship not found")
        usage = await compute_relationship_downstream_usage(db, rel, model_id)
        return RelationshipDownstreamUsageResponse(**usage)


@router.delete(
    "/{dimension_id}/attribute-relationships/{relationship_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_attribute_relationship(
    project_id: UUID,
    model_id: UUID,
    dimension_id: UUID,
    relationship_id: UUID,
    retire_aggregates: bool = Query(
        default=False,
        description="If true, retire aggregates that carried this relationship's columns.",
    ),
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    """Delete a relationship with cascade cleanup.

    Removes the relationship, its auto-added dimensions (both sides), and
    symmetric paired relationships. When retire_aggregates=true, also retires
    affected aggregates.
    """
    from src.api.dimension_detail_lifecycle import cascade_delete_relationship

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        rel = await db.get(DimensionAttributeRelationship, relationship_id)
        if rel is None or rel.dimension_id != dimension_id or rel.model_id != model_id:
            raise HTTPException(status_code=404, detail="Relationship not found")
        await cascade_delete_relationship(
            db, rel, model_id, retire_aggregates=retire_aggregates,
        )
        await db.commit()
