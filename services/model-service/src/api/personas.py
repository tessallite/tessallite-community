"""Personas CRUD — Phase 8.B core.

A persona is an analyst-facing scope over a model. Empty include lists
mean "no restriction on this object class"; populated lists are
allow-lists. Cascade-on-delete (per Q-B1.1=b) is implemented via
``strip_id_from_personas``, called from the measures/dimensions/
hierarchies delete routes — when an included id disappears, it is
removed from every persona's include list and the response carries a
warning naming each affected persona.

At the gateway, each persona is emitted as a sibling virtual catalog
named ``<model.slug>_<persona.slug>`` alongside the base ``<model.slug>``
catalog. A seeded ``technical`` persona per model stands in for the
former hard-coded ``_technical`` variant.
"""
from __future__ import annotations

import math
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from shared.db.model_write_lock_guard import model_write_lock_exempt
from shared.db.models import (
    DataTag,
    Dimension,
    HierarchyDefinition,
    Measure,
    Model,
    ModelParameter,
    ModelVersion,
    ModelColumn,
    ModelTable,
    Persona,
    PersonaTagRestriction,
    ProjectPersonaModelScope,
    UserDefinedAttributeColumnRef,
    data_tag_columns,
)
from shared.schemas.domains.aggregates_security import persona_filter_value_is_valid
from shared.db.session import get_tenant_db
from shared.auth.roles import MODEL_TECHNICAL_ROLE
from shared.security.persona_resolver import (
    PersonaAudienceNarrowingError,
    caller_roles,
    is_in_audience,
    is_privileged_by_role,
    payload_narrows_visibility,
    reject_empty_audience_narrowing,
)
from shared.schemas.pydantic_models import (
    PersonaCreate,
    PersonaResolution,
    PersonaResponse,
    PersonaUpdate,
)
from shared.audit.logger import audit_required
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from src.api._model_lock import acquire_model_definition_lock
from src.api._scope import ensure_model_in_project
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role


router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}",
    tags=["personas"],
)


_INCLUDED_FIELDS = (
    "included_measure_ids",
    "included_dimension_ids",
    "included_hierarchy_ids",
)


# Canonical seeded "Technical" persona. Every model gets one at creation so the
# hidden-columns technical catalogue (surfaced by the gateway as
# ``<slug>_technical``) is available immediately. Mirrors migrations 0042 (seed)
# + 0124 (audience gate); those only seeded models that predated the migration,
# so models created afterwards had an inert technical flow (Bug-6138).
TECHNICAL_PERSONA_SLUG = "technical"
TECHNICAL_PERSONA_NAME = "Technical"
TECHNICAL_PERSONA_DESCRIPTION = (
    "Auto-seeded technical view — shows every column including those "
    "marked hidden on the business view."
)


class PersonaParameterCollision(BaseModel):
    """One persisted bare persona key shadowing a deployed parameter.

    The value is deliberately not returned: this is a read-only authoring
    preflight and filter values may contain sensitive business data. The
    canonical remediation is explicit ``@parameter`` targeting.
    """

    persona_id: UUID
    persona_name: str
    persona_slug: str
    default_filter_key: str
    parameter_name: str
    suggested_key: str


class PersonaParameterCollisionPreflightResponse(BaseModel):
    """Read-only persisted-persona/deployed-parameter collision report."""

    model_id: UUID
    deployed_version_id: UUID | None
    collisions: list[PersonaParameterCollision]


async def seed_technical_persona(db, model_id: UUID) -> Persona:
    """Seed (or return the existing) canonical Technical persona for a model.

    Idempotent: if a persona with ``slug='technical'`` already exists on the
    model (imported bundle, migration 0042, or a re-run) it is returned
    unchanged. The row is flushed but NOT committed — the caller owns the
    transaction boundary so model creation and persona seeding commit atomically.

    The persona exposes hidden columns (``includes_hidden_columns=True``) and is
    gated to the ``model_technical`` audience role, matching migration 0124 so a
    non-technical viewer is not force-locked into the technical view.
    """
    # Bug-7982 R7 (review round 5, F1): this helper is part of a WHOLESALE
    # REBUILD of snapshot-owned state into a model created in the caller's own
    # transaction, so it is a DELIBERATE non-holder of the per-model
    # definition lock. The exemption is declared HERE, not at each call site:
    # round 5 found 13 call sites where the caller exempted
    # rehydrate_into_live and then wrote personas/model_versions unexempt two
    # lines later, flooding the runtime write guard's report and muting it for
    # real violations.
    async with model_write_lock_exempt(
        db, "seed: canonical Technical persona for a model created in this transaction"
    ):
        existing = (
            await db.execute(
                select(Persona).where(
                    Persona.model_id == model_id,
                    Persona.slug == TECHNICAL_PERSONA_SLUG,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        persona = Persona(
            model_id=model_id,
            name=TECHNICAL_PERSONA_NAME,
            slug=TECHNICAL_PERSONA_SLUG,
            description=TECHNICAL_PERSONA_DESCRIPTION,
            included_measure_ids=[],
            included_dimension_ids=[],
            included_hierarchy_ids=[],
            audience_roles=[MODEL_TECHNICAL_ROLE],
            default_filters={},
            bypass_row_security=False,
            includes_hidden_columns=True,
        )
        db.add(persona)
        await db.flush()
        return persona


async def strip_id_from_personas(
    db,
    *,
    model_id: UUID,
    object_id: UUID,
    object_class: str,
) -> list[str]:
    """Remove ``object_id`` from every persona on ``model_id``.

    ``object_class`` is one of ``measure | dimension | hierarchy``. Returns
    the names of personas that were touched, so the caller can surface
    them to the user as a warning.

    Bug-7793: also sweeps ``ProjectPersonaModelScope`` rows for the same
    model_id. Project persona scopes carry ``included_measure_ids`` and
    ``included_dimension_ids`` JSONB arrays that reference the same objects
    as model-level personas. Without this sweep, deleting a measure or
    dimension left dangling UUIDs in the project-persona scope arrays,
    silently shrinking the agent's grounded attribute set with no
    operator-visible cause.
    """
    field_map = {
        "measure": "included_measure_ids",
        "dimension": "included_dimension_ids",
        "hierarchy": "included_hierarchy_ids",
    }
    field = field_map.get(object_class)
    if field is None:
        raise ValueError(f"Unknown persona object_class {object_class!r}")
    result = await db.execute(
        select(Persona).where(Persona.model_id == model_id)
    )
    touched: list[str] = []
    target = str(object_id)
    for p in result.scalars().all():
        ids = list(getattr(p, field) or [])
        if target in ids:
            setattr(p, field, [i for i in ids if i != target])
            touched.append(p.name)

    # Bug-7793: sweep project-persona model scopes (agent-facing). These
    # carry included_measure_ids and included_dimension_ids but NOT
    # included_hierarchy_ids, so we only sweep for measure/dimension.
    scope_field_map = {
        "measure": "included_measure_ids",
        "dimension": "included_dimension_ids",
    }
    scope_field = scope_field_map.get(object_class)
    if scope_field is not None:
        scope_result = await db.execute(
            select(ProjectPersonaModelScope).where(
                ProjectPersonaModelScope.model_id == model_id
            )
        )
        for scope in scope_result.scalars().all():
            ids = list(getattr(scope, scope_field) or [])
            if target in ids:
                setattr(scope, scope_field, [i for i in ids if i != target])

    return touched


def _to_response(
    p: Persona,
    restricted_column_ids: list[UUID] | None = None,
    cls_blocked_measure_ids: list[UUID] | None = None,
    cls_blocked_dimension_ids: list[UUID] | None = None,
) -> PersonaResponse:
    return PersonaResponse(
        id=p.id,
        model_id=p.model_id,
        name=p.name,
        slug=p.slug,
        description=p.description,
        included_measure_ids=[UUID(s) for s in (p.included_measure_ids or [])],
        included_dimension_ids=[UUID(s) for s in (p.included_dimension_ids or [])],
        included_hierarchy_ids=[UUID(s) for s in (p.included_hierarchy_ids or [])],
        audience_roles=list(p.audience_roles or []),
        default_filters=dict(p.default_filters or {}),
        bypass_row_security=bool(getattr(p, "bypass_row_security", False)),
        includes_hidden_columns=bool(getattr(p, "includes_hidden_columns", False)),
        restricted_column_ids=restricted_column_ids or [],
        cls_blocked_measure_ids=cls_blocked_measure_ids or [],
        cls_blocked_dimension_ids=cls_blocked_dimension_ids or [],
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


async def _restricted_columns_by_persona(
    db, persona_ids: list[UUID],
) -> dict[UUID, list[UUID]]:
    """Map persona id -> model_column ids restricted via tag restrictions.

    F-008-05 residual: the gateway needs these to exclude restricted
    column names from each persona's catalogue metadata (values are
    enforced by the query-router on every path; this hides the names).
    """
    if not persona_ids:
        return {}
    rows = await db.execute(
        select(
            PersonaTagRestriction.persona_id,
            data_tag_columns.c.model_column_id,
        )
        .join(
            data_tag_columns,
            data_tag_columns.c.tag_id == PersonaTagRestriction.data_tag_id,
        )
        .where(PersonaTagRestriction.persona_id.in_(persona_ids))
    )
    out: dict[UUID, list[UUID]] = {}
    for persona_id, column_id in rows.all():
        out.setdefault(persona_id, []).append(column_id)
    return out


def _payload_narrows_visibility(
    *,
    included_measure_ids=None,
    included_dimension_ids=None,
    included_hierarchy_ids=None,
    default_filters=None,
    restricted_tag_ids=None,
) -> bool:
    return payload_narrows_visibility(
        included_measure_ids=included_measure_ids,
        included_dimension_ids=included_dimension_ids,
        included_hierarchy_ids=included_hierarchy_ids,
        default_filters=default_filters,
        restricted_tag_ids=restricted_tag_ids,
    )


def _reject_empty_audience_narrowing(
    audience_roles,
    *,
    included_measure_ids=None,
    included_dimension_ids=None,
    included_hierarchy_ids=None,
    default_filters=None,
    restricted_tag_ids=None,
) -> None:
    """F-008-03: a narrowing persona with an empty audience is inert after
    explicit-audience assignment. Reject at save time (shared helper).
    """
    try:
        reject_empty_audience_narrowing(
            audience_roles,
            included_measure_ids=included_measure_ids,
            included_dimension_ids=included_dimension_ids,
            included_hierarchy_ids=included_hierarchy_ids,
            default_filters=default_filters,
            restricted_tag_ids=restricted_tag_ids,
        )
    except PersonaAudienceNarrowingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "PERSONA_EMPTY_AUDIENCE_NARROWING",
                "message": str(exc),
            },
        ) from exc


class ClsClosureBase:
    """Model-wide CLS closure inputs (measures, dimensions, column rows) loaded
    ONCE per request and reused across every persona.

    ``list_personas`` computes the CLS overlay for every persona in a model; the
    measures/dimensions/columns are identical for all of them, so re-loading them
    per persona turned an N-persona listing into N model-wide scans (quadratic on
    large models). Load them once and pass this bundle to each
    ``_cls_blocked_object_ids`` call. Only the persona-specific restricted set
    stays per-call."""

    __slots__ = ("measures", "dims", "col_rows")

    def __init__(self, measures, dims, col_rows):
        self.measures = measures
        self.dims = dims
        self.col_rows = col_rows


async def _load_cls_closure_base(db, model_id: UUID) -> "ClsClosureBase":
    measures = list(
        (
            await db.execute(select(Measure).where(Measure.model_id == model_id))
        ).scalars().all()
    )
    dims = list(
        (
            await db.execute(
                select(Dimension).where(Dimension.model_id == model_id)
            )
        ).scalars().all()
    )
    col_rows = (
        await db.execute(
            select(ModelColumn.id, ModelColumn.column_name, ModelTable.physical_name)
            .join(ModelTable, ModelTable.id == ModelColumn.model_table_id)
            .where(ModelTable.model_id == model_id)
        )
    ).all()
    return ClsClosureBase(measures, dims, col_rows)


async def _cls_blocked_object_ids(
    db, model_id: UUID, restricted_column_ids: list[UUID] | None,
    *, base: "ClsClosureBase | None" = None,
) -> tuple[list[UUID], list[UUID]]:
    """Measure/dimension ids the CLS closure blocks for this persona (F-008-06).

    Uses ``object_touches_restricted`` so calculated objects with no single
    ``source_column_id`` still appear in the canvas overlay.

    ``base`` lets a caller iterating personas load the model-wide inputs once and
    reuse them (see ``ClsClosureBase``). When omitted they are loaded here, so a
    single-persona call is unchanged.
    """
    if not restricted_column_ids:
        return [], []
    from shared.security.restricted_column_closure import (
        ClosureContext,
        normalise_id_set,
        object_touches_restricted,
    )

    if base is None:
        base = await _load_cls_closure_base(db, model_id)
    measures = base.measures
    dims = base.dims
    col_rows = base.col_rows

    restricted = normalise_id_set(restricted_column_ids)
    restricted_phys = {
        str(name).lower()
        for cid, name, _phys in col_rows
        if str(cid) in restricted and name
    }
    known_phys = {str(name).lower() for _cid, name, _phys in col_rows if name}
    table_idents = {
        str(phys).lower() for _cid, _name, phys in col_rows if phys
    }
    # Scope the attribute-ref lookup to THIS persona's restricted columns (an
    # indexed IN over a small set) instead of scanning the whole
    # UserDefinedAttributeColumnRef table on every call. The result is identical:
    # only refs whose column is restricted contribute to ``restricted_uda``.
    uda_rows = (
        await db.execute(
            select(
                UserDefinedAttributeColumnRef.attribute_id,
                UserDefinedAttributeColumnRef.column_id,
            ).where(
                UserDefinedAttributeColumnRef.column_id.in_(list(restricted_column_ids))
            )
        )
    ).all()
    restricted_uda = {
        str(attr_id)
        for attr_id, col_id in uda_rows
        if str(col_id) in restricted
    }
    ctx = ClosureContext(
        restricted_uda_ids=restricted_uda,
        measures_by_id={str(m.id): m for m in measures},
        measures_by_name={m.name: m for m in measures if getattr(m, "name", None)},
        restricted_physical_names=restricted_phys,
        known_physical_names=known_phys,
        table_identifiers=table_idents,
    )
    blocked_m = [
        m.id for m in measures if object_touches_restricted(m, restricted, ctx)
    ]
    blocked_d = [
        d.id for d in dims if object_touches_restricted(d, restricted, ctx)
    ]
    return blocked_m, blocked_d


def _serialise_uuid_list(values) -> list[str]:
    return [str(v) for v in (values or [])]


async def _load_or_404(db, model_id: UUID, persona_id: UUID) -> Persona:
    p = await db.get(Persona, persona_id)
    if p is None or p.model_id != model_id:
        raise HTTPException(status_code=404, detail="Persona not found")
    return p


async def _existing_ids(db, table, model_id: UUID, ids) -> set[str]:
    wanted = [str(i) for i in (ids or [])]
    if not wanted:
        return set()
    rows = (
        await db.execute(
            select(table.id).where(
                table.model_id == model_id, table.id.in_(wanted)
            )
        )
    ).scalars().all()
    return {str(r) for r in rows}


def _filter_value_is_valid(raw) -> bool:
    """F-008-16: one default_filters value shape check (no DB needed)."""
    return persona_filter_value_is_valid(raw)


def _date_range_parameter_is_valid(raw: object) -> bool:
    """Validate the persisted ``@parameter`` date-range wire shape.

    Persona parameters are execution inputs, not dimension operator objects:
    the canonical value is exactly ``{"from": ..., "to": ...}``. Keeping
    this check here in model-service makes the authoring boundary agree with
    the query-time resolver and prevents a save from silently losing one bound.
    """
    if not isinstance(raw, dict) or set(raw) != {"from", "to"}:
        return False
    lower = raw.get("from")
    upper = raw.get("to")
    if not isinstance(lower, str) or not isinstance(upper, str):
        return False
    try:
        return datetime.fromisoformat(lower.strip()) <= datetime.fromisoformat(upper.strip())
    except (TypeError, ValueError):
        return False


def _parameter_value_is_valid(raw: object, param_type: str) -> bool:
    """Match the query-router's declared parameter coercion contract."""
    def _finite_number(value: object) -> bool:
        try:
            return isinstance(value, (int, float)) and math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False

    if param_type == "string":
        return isinstance(raw, str)
    if param_type == "number":
        return (
            _finite_number(raw)
            and not isinstance(raw, bool)
        )
    if param_type == "boolean":
        return isinstance(raw, bool)
    if param_type == "multi_value":
        return (
            isinstance(raw, list)
            and bool(raw)
            and all(
                item is not None
                and not isinstance(item, bool)
                and (
                        isinstance(item, str)
                    or (
                        _finite_number(item)
                    )
                )
                for item in raw
            )
        )
    if param_type == "date_range":
        return _date_range_parameter_is_valid(raw)
    return False


async def _validate_persona_scope(
    db,
    model_id: UUID,
    *,
    included_measure_ids=None,
    included_dimension_ids=None,
    included_hierarchy_ids=None,
    default_filters=None,
) -> None:
    """Reject persona payloads that reference objects outside the model or
    carry malformed default filters (F-008-16, F-008-21), fail closed.

    Stale/foreign include ids silently behaved as "deny everything they were
    meant to allow"; an unvalidated default-filter key surfaced as a runtime
    502 for the whole audience. Both are caught at save time with 422.
    """
    # F-008-21: every include id must exist in the model.
    for ids, table, kind in (
        (included_measure_ids, Measure, "measure"),
        (included_dimension_ids, Dimension, "dimension"),
        (included_hierarchy_ids, HierarchyDefinition, "hierarchy"),
    ):
        wanted = [str(i) for i in (ids or [])]
        if not wanted:
            continue
        found = await _existing_ids(db, table, model_id, wanted)
        missing = [i for i in wanted if i not in found]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "PERSONA_INCLUDE_NOT_IN_MODEL",
                    "message": (
                        f"These {kind} ids do not belong to this model and "
                        f"cannot be included: {', '.join(missing)}."
                    ),
                },
            )

    # F-008-16 / L13-PERSONA-AT: bare keys are dimension targets and explicit
    # ``@Name`` keys are parameter targets. Do not turn a bare key into a
    # parameter name at save time: Q2 Option B intentionally permits both
    # namespaces to coexist and the read-only preflight below calls out the
    # persisted collision before deployment.
    if default_filters:
        dim_names = set(
            (
                await db.execute(
                    select(Dimension.name).where(Dimension.model_id == model_id)
                )
            ).scalars().all()
        )
        parameter_descriptors: dict[str, tuple[str, str]] = {}
        for row in (
            await db.execute(
                select(ModelParameter).where(ModelParameter.model_id == model_id)
            )
        ).scalars().all():
            # The test seam supplies plain names; production supplies ORM rows.
            # Treat an untyped legacy row as a string parameter while preserving
            # the same namespace lookup for both forms.
            authored = str(getattr(row, "name", row))
            canonical = authored if authored.startswith("@") else f"@{authored}"
            param_type = str(getattr(row, "param_type", "string"))
            parameter_descriptors[authored.lower()] = (canonical, param_type)
            parameter_descriptors[canonical.lower()] = (canonical, param_type)
        unknown_parameter_keys = [
            k for k in default_filters
            if k.startswith("@") and k.lower() not in parameter_descriptors
        ]
        if unknown_parameter_keys:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "PERSONA_DEFAULT_FILTER_UNKNOWN_PARAMETER",
                    "message": (
                        "These explicit parameter targets are not declared on "
                        f"this model: {', '.join(unknown_parameter_keys)}."
                    ),
                },
            )
        bad_keys = [
            k for k in default_filters
            if not k.startswith("@") and k not in dim_names
        ]
        if bad_keys:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "PERSONA_DEFAULT_FILTER_UNKNOWN_DIMENSION",
                    "message": (
                        "These default-filter keys are not dimensions on this "
                        f"model: {', '.join(bad_keys)}."
                    ),
                },
            )
        bad_dimension_values = [
            k for k, v in default_filters.items()
            if not k.startswith("@") and not _filter_value_is_valid(v)
        ]
        bad_parameter_values = [
            k for k, v in default_filters.items()
            if k.startswith("@")
            and k.lower() in parameter_descriptors
            and not _parameter_value_is_valid(v, parameter_descriptors[k.lower()][1])
        ]
        if bad_dimension_values:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "PERSONA_DEFAULT_FILTER_INVALID",
                    "message": (
                        "These default-filter values use an unsupported "
                        "operator or shape (a dict must be a single "
                        "{operator: value}; BETWEEN needs exactly two bounds): "
                        f"{', '.join(bad_dimension_values)}."
                    ),
                },
            )
        if bad_parameter_values:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "PERSONA_PARAMETER_VALUE_INVALID",
                    "message": (
                        "These explicit parameter values do not match their "
                        "declared types (strings, numbers, booleans, non-empty "
                        "scalar arrays, or exact ordered date ranges): "
                        f"{', '.join(bad_parameter_values)}."
                    ),
                },
            )


async def _validate_restricted_tags(
    db, model_id: UUID, tag_ids: list[UUID],
) -> None:
    """Reject tag ids that do not belong to the target model (Bug-7051).

    Mirrors the validation in data_tags._validate_tags_in_model but lives
    here so the persona create/update path can validate without importing
    the data_tags module.
    """
    if not tag_ids:
        return
    found = set(
        (
            await db.execute(
                select(DataTag.id).where(
                    DataTag.id.in_(tag_ids), DataTag.model_id == model_id,
                )
            )
        ).scalars().all()
    )
    missing = [str(t) for t in tag_ids if t not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "PERSONA_RESTRICTION_TAG_NOT_IN_MODEL",
                "message": (
                    "These data-tag ids do not belong to this model "
                    f"and cannot be used as restrictions: {', '.join(missing)}."
                ),
            },
        )


async def _persist_tag_restrictions(
    db, persona_id: UUID, tag_ids: list[UUID],
) -> None:
    """Replace all tag restrictions for a persona within the current transaction.

    Bug-7051: called inside the same transaction as persona create/update so
    persona row and restriction rows either both commit or both roll back.
    """
    # Delete existing restrictions (idempotent for create where none exist yet)
    await db.execute(
        delete(PersonaTagRestriction).where(
            PersonaTagRestriction.persona_id == persona_id,
        )
    )
    # Insert the new set
    for tag_id in tag_ids:
        db.add(PersonaTagRestriction(persona_id=persona_id, data_tag_id=tag_id))


@router.post(
    "/personas",
    response_model=PersonaResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_persona(
    project_id: UUID,
    model_id: UUID,
    body: PersonaCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PersonaResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        # F-008-16 / F-008-21: reject foreign include ids and malformed
        # default filters before persisting (fail closed).
        await _validate_persona_scope(
            db, model_id,
            included_measure_ids=body.included_measure_ids,
            included_dimension_ids=body.included_dimension_ids,
            included_hierarchy_ids=body.included_hierarchy_ids,
            default_filters=body.default_filters,
        )
        # Bug-7051: validate restricted_tag_ids before any mutation so a
        # bad tag id never reaches the insert path.
        if body.restricted_tag_ids is not None:
            await _validate_restricted_tags(db, model_id, body.restricted_tag_ids)
        _reject_empty_audience_narrowing(
            body.audience_roles,
            included_measure_ids=body.included_measure_ids,
            included_dimension_ids=body.included_dimension_ids,
            included_hierarchy_ids=body.included_hierarchy_ids,
            default_filters=body.default_filters,
            restricted_tag_ids=body.restricted_tag_ids or [],
        )

        p = Persona(
            model_id=model_id,
            name=body.name,
            slug=body.slug,
            description=body.description,
            included_measure_ids=_serialise_uuid_list(body.included_measure_ids),
            included_dimension_ids=_serialise_uuid_list(body.included_dimension_ids),
            included_hierarchy_ids=_serialise_uuid_list(body.included_hierarchy_ids),
            audience_roles=list(body.audience_roles or []),
            default_filters=dict(body.default_filters or {}),
            bypass_row_security=bool(body.bypass_row_security),
            includes_hidden_columns=bool(body.includes_hidden_columns),
        )
        db.add(p)
        # Bug-7051: flush (not commit) to get the persona id, then insert
        # tag restrictions before committing — both writes in one
        # transaction. On any failure the whole transaction rolls back so
        # a persona can never exist without its intended restrictions.
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "PERSONA_NAME_CONFLICT",
                    "message": (
                        f"A persona named '{body.name}' or with slug "
                        f"'{body.slug}' already exists in this model."
                    ),
                },
            )

        # Bug-7051: persist tag restrictions in the SAME transaction
        if body.restricted_tag_ids is not None:
            await _persist_tag_restrictions(db, p.id, body.restricted_tag_ids)

        # Bug-7052 / ER-RLS-001 / Bug-8261: fail-closed audit BEFORE commit so
        # the persona mutation and its audit record are in the SAME
        # transaction. If the audit write fails, the create rolls back too —
        # a security persona is never created without durable evidence.
        await audit_required(
            db, action="security.persona_create", severity="critical",
            actor_email=current_user.email,
            target_type="persona", target_id=p.id,
            target_name=p.name,
            detail={
                "model_id": str(model_id),
                "bypass_row_security": bool(p.bypass_row_security),
                "has_allow_lists": bool(
                    p.included_measure_ids or p.included_dimension_ids
                    or p.included_hierarchy_ids
                ),
                "has_default_filters": bool(p.default_filters),
                "has_tag_restrictions": body.restricted_tag_ids is not None
                    and len(body.restricted_tag_ids or []) > 0,
            },
        )

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "PERSONA_NAME_CONFLICT",
                    "message": (
                        f"A persona named '{body.name}' or with slug "
                        f"'{body.slug}' already exists in this model."
                    ),
                },
            )
        await db.refresh(p)
        await emit_webhook(current_user.tenant_id, "security.persona_create", {
            "persona_id": str(p.id),
            "name": p.name,
            "model_id": str(model_id),
            "actor": current_user.email,
        })
        restricted = await _restricted_columns_by_persona(db, [p.id])
        blocked_m, blocked_d = await _cls_blocked_object_ids(
            db, model_id, restricted.get(p.id),
        )
        return _to_response(
            p, restricted.get(p.id), blocked_m, blocked_d,
        )


@router.get(
    "/personas",
    response_model=list[PersonaResponse],
)
async def list_personas(
    project_id: UUID,
    model_id: UUID,
    for_audience: bool = Query(
        default=False,
        description=(
            "When true, return only personas whose audience_roles "
            "intersect the caller's roles. Admin tokens always see all."
        ),
    ),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> list[PersonaResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(Persona)
            .where(Persona.model_id == model_id)
            .order_by(Persona.name)
        )
        items = list(result.scalars().all())
        if for_audience and not is_privileged_by_role(current_user):
            # I-1: one shared audience predicate (persona_resolver) so the
            # listing and query-time enforcement can never drift apart.
            roles = caller_roles(current_user)
            tagged = set()
            if items:
                tagged = set(
                    (
                        await db.execute(
                            select(PersonaTagRestriction.persona_id).where(
                                PersonaTagRestriction.persona_id.in_(
                                    [p.id for p in items]
                                )
                            )
                        )
                    ).scalars().all()
                )
            items = [
                p for p in items
                if is_in_audience(
                    p, roles, has_tag_restrictions=p.id in tagged,
                )
            ]
        restricted = await _restricted_columns_by_persona(
            db, [p.id for p in items],
        )
        # Load the model-wide CLS closure inputs ONCE and reuse them for every
        # persona (was N model-wide scans for an N-persona listing).
        cls_base = await _load_cls_closure_base(db, model_id) if items else None
        out = []
        for p in items:
            blocked_m, blocked_d = await _cls_blocked_object_ids(
                db, model_id, restricted.get(p.id), base=cls_base,
            )
            out.append(
                _to_response(
                    p, restricted.get(p.id), blocked_m, blocked_d,
                )
            )
        return out


@router.get(
    "/personas/parameter-collision-preflight",
    response_model=PersonaParameterCollisionPreflightResponse,
)
async def persona_parameter_collision_preflight(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> PersonaParameterCollisionPreflightResponse:
    """Report persisted bare keys that collide with deployed parameters.

    This endpoint is intentionally read-only and uses the deployed version's
    immutable ``model_parameters`` snapshot, not draft ``ModelParameter`` rows.
    The editor calls it on open so an existing collision is visible before a
    modeler saves a persona; no persisted key is rewritten implicitly.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        model = await db.get(Model, model_id)
        deployed_version_id = getattr(model, "deployed_version_id", None)
        deployed = None
        if deployed_version_id is not None:
            deployed = await db.get(ModelVersion, deployed_version_id)
            if deployed is not None and deployed.model_id != model_id:
                # A corrupt cross-model pointer must not turn another model's
                # parameters into an authoring warning for this model.
                deployed = None

        snapshot = getattr(deployed, "snapshot_json", None)
        parameter_names: dict[str, str] = {}
        if isinstance(snapshot, dict):
            for item in snapshot.get("model_parameters", []) or []:
                if not isinstance(item, dict):
                    continue
                authored = str(item.get("name") or "").strip()
                if not authored:
                    continue
                canonical = authored if authored.startswith("@") else f"@{authored}"
                parameter_names[canonical.lstrip("@").lower()] = canonical

        result = await db.execute(
            select(Persona)
            .where(Persona.model_id == model_id)
            .order_by(Persona.name, Persona.id)
        )
        collisions: list[PersonaParameterCollision] = []
        for persona in result.scalars().all():
            for key in (persona.default_filters or {}):
                if key.startswith("@"):  # already explicit; no collision
                    continue
                parameter_name = parameter_names.get(key.lower())
                if parameter_name is None:
                    continue
                collisions.append(
                    PersonaParameterCollision(
                        persona_id=persona.id,
                        persona_name=persona.name,
                        persona_slug=persona.slug,
                        default_filter_key=key,
                        parameter_name=parameter_name,
                        suggested_key=parameter_name,
                    )
                )

        return PersonaParameterCollisionPreflightResponse(
            model_id=model_id,
            deployed_version_id=(
                deployed.id if deployed is not None else None
            ),
            collisions=collisions,
        )


@router.get(
    "/personas/{persona_id}",
    response_model=PersonaResponse,
)
async def get_persona(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> PersonaResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        p = await _load_or_404(db, model_id, persona_id)
        restricted = await _restricted_columns_by_persona(db, [p.id])
        blocked_m, blocked_d = await _cls_blocked_object_ids(
            db, model_id, restricted.get(p.id),
        )
        return _to_response(
            p, restricted.get(p.id), blocked_m, blocked_d,
        )


@router.patch(
    "/personas/{persona_id}",
    response_model=PersonaResponse,
    dependencies=[require_role("modeler")],
)
async def update_persona(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID,
    body: PersonaUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> PersonaResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        p = await _load_or_404(db, model_id, persona_id)
        updates = body.model_dump(exclude_unset=True)
        # F-008-16 / F-008-21: validate the EFFECTIVE post-update scope (the
        # incoming value where present, the stored value otherwise) so an
        # update cannot smuggle in a foreign include id or a bad default
        # filter. Fail closed before mutating.
        await _validate_persona_scope(
            db, model_id,
            included_measure_ids=updates.get("included_measure_ids", p.included_measure_ids),
            included_dimension_ids=updates.get("included_dimension_ids", p.included_dimension_ids),
            included_hierarchy_ids=updates.get("included_hierarchy_ids", p.included_hierarchy_ids),
            default_filters=updates.get("default_filters", p.default_filters),
        )
        # Bug-7051: validate restricted_tag_ids before any mutation
        if "restricted_tag_ids" in updates and updates["restricted_tag_ids"] is not None:
            await _validate_restricted_tags(db, model_id, updates["restricted_tag_ids"])
        _eff_tags = updates.get("restricted_tag_ids")
        if _eff_tags is None:
            _eff_tags = list(
                (
                    await db.execute(
                        select(PersonaTagRestriction.data_tag_id).where(
                            PersonaTagRestriction.persona_id == p.id
                        )
                    )
                ).scalars().all()
            )
        _reject_empty_audience_narrowing(
            updates.get("audience_roles", p.audience_roles),
            included_measure_ids=updates.get(
                "included_measure_ids", p.included_measure_ids
            ),
            included_dimension_ids=updates.get(
                "included_dimension_ids", p.included_dimension_ids
            ),
            included_hierarchy_ids=updates.get(
                "included_hierarchy_ids", p.included_hierarchy_ids
            ),
            default_filters=updates.get("default_filters", p.default_filters),
            restricted_tag_ids=_eff_tags,
        )

        if "name" in updates:
            p.name = updates["name"]
        if "slug" in updates:
            p.slug = updates["slug"]
        if "description" in updates:
            p.description = updates["description"]
        if "included_measure_ids" in updates:
            p.included_measure_ids = _serialise_uuid_list(updates["included_measure_ids"])
        if "included_dimension_ids" in updates:
            p.included_dimension_ids = _serialise_uuid_list(updates["included_dimension_ids"])
        if "included_hierarchy_ids" in updates:
            p.included_hierarchy_ids = _serialise_uuid_list(updates["included_hierarchy_ids"])
        if "audience_roles" in updates:
            p.audience_roles = list(updates["audience_roles"] or [])
        if "default_filters" in updates:
            p.default_filters = dict(updates["default_filters"] or {})
        if "bypass_row_security" in updates:
            p.bypass_row_security = bool(updates["bypass_row_security"])
        if "includes_hidden_columns" in updates:
            p.includes_hidden_columns = bool(updates["includes_hidden_columns"])

        # Bug-7051: persist tag restriction changes in the SAME transaction
        # as the persona field updates, so both commit or both roll back.
        if "restricted_tag_ids" in updates and updates["restricted_tag_ids"] is not None:
            await _persist_tag_restrictions(db, p.id, updates["restricted_tag_ids"])

        # Bug-7052 / ER-RLS-001: stage audit event BEFORE commit so
        # the mutation and its audit record commit atomically.
        # ER-RLS-002: includes_hidden_columns is security-relevant.
        _sec_fields = [
            f for f in (
                "bypass_row_security", "audience_roles", "default_filters",
                "included_measure_ids", "included_dimension_ids",
                "included_hierarchy_ids", "restricted_tag_ids",
                "includes_hidden_columns",
            ) if f in updates
        ]
        if _sec_fields:
            _widens = (
                "bypass_row_security" in updates and bool(updates["bypass_row_security"])
            ) or (
                "includes_hidden_columns" in updates and bool(updates["includes_hidden_columns"])
            ) or (
                "restricted_tag_ids" in updates
                and len(updates.get("restricted_tag_ids") or []) == 0
            )
            # Bug-8261: fail-closed audit before commit — a security-field
            # persona update (bypass/allow-lists/default-filters/tag
            # restrictions) never commits without its durable record.
            await audit_required(
                db, action="security.persona_update", severity="critical",
                actor_email=current_user.email,
                target_type="persona", target_id=p.id,
                target_name=p.name,
                detail={
                    "model_id": str(model_id),
                    "changed_fields": _sec_fields,
                    "widens_access": _widens,
                },
            )

        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error_code": "PERSONA_NAME_CONFLICT",
                    "message": (
                        f"A persona with name '{p.name}' or slug "
                        f"'{p.slug}' already exists in this model."
                    ),
                },
            )
        await db.refresh(p)
        if _sec_fields:
            await emit_webhook(current_user.tenant_id, "security.persona_update", {
                "persona_id": str(p.id),
                "name": p.name,
                "model_id": str(model_id),
                "changed_fields": _sec_fields,
                "actor": current_user.email,
            })
        restricted = await _restricted_columns_by_persona(db, [p.id])
        blocked_m, blocked_d = await _cls_blocked_object_ids(
            db, model_id, restricted.get(p.id),
        )
        return _to_response(
            p, restricted.get(p.id), blocked_m, blocked_d,
        )


@router.delete(
    "/personas/{persona_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_persona(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        p = await _load_or_404(db, model_id, persona_id)
        _persona_name = p.name
        _had_bypass = bool(p.bypass_row_security)
        await db.delete(p)
        # Bug-8261: durable, fail-closed audit in the SAME transaction, BEFORE
        # commit. Previously the delete committed first and audit() ran
        # fail-open afterwards, so a persona (including a bypass_row_security
        # surface) could be deleted with the audit write silently lost. Emitting
        # audit_required before the single commit makes the delete and its
        # evidence atomic — an audit-store failure rolls the delete back.
        await audit_required(
            db, action="security.persona_delete", severity="critical",
            actor_email=current_user.email,
            target_type="persona", target_id=persona_id,
            target_name=_persona_name,
            detail={
                "model_id": str(model_id),
                "had_bypass_row_security": _had_bypass,
            },
        )
        await db.commit()
        await emit_webhook(current_user.tenant_id, "security.persona_delete", {
            "persona_id": str(persona_id),
            "name": _persona_name,
            "model_id": str(model_id),
            "actor": current_user.email,
        })


async def _resolve_object(
    db, p: Persona, kind: str, object_id: UUID,
) -> tuple[bool, str | None]:
    """Explain whether one object is allowed under the persona (F-008-22).

    Covers the three allow-list kinds plus tag restrictions. Empty allow
    list means unrestricted for that kind; tag restrictions are deny-lists,
    so a column under a restricted tag is denied.
    """
    if kind == "measure":
        included = [str(x) for x in (p.included_measure_ids or [])]
        if not included:
            return True, "empty measure allow list — unrestricted"
        if str(object_id) in included:
            return True, None
        m = await db.get(Measure, object_id)
        name = m.name if m else str(object_id)
        return False, f"Measure '{name}' is not in persona '{p.name}'."
    if kind == "dimension":
        included = [str(x) for x in (p.included_dimension_ids or [])]
        if not included:
            return True, "empty dimension allow list — unrestricted"
        if str(object_id) in included:
            return True, None
        d = await db.get(Dimension, object_id)
        name = d.name if d else str(object_id)
        return False, f"Dimension '{name}' is not in persona '{p.name}'."
    if kind == "hierarchy":
        included = [str(x) for x in (p.included_hierarchy_ids or [])]
        if not included:
            return True, "empty hierarchy allow list — unrestricted"
        if str(object_id) in included:
            return True, None
        h = await db.get(HierarchyDefinition, object_id)
        name = h.name if h else str(object_id)
        return False, f"Hierarchy '{name}' is not in persona '{p.name}'."
    if kind == "tag":
        restricted = set(
            (
                await db.execute(
                    select(PersonaTagRestriction.data_tag_id).where(
                        PersonaTagRestriction.persona_id == p.id
                    )
                )
            ).scalars().all()
        )
        if str(object_id) in {str(t) for t in restricted}:
            return False, (
                f"Persona '{p.name}' is restricted from data tag {object_id} "
                "(its columns are hidden)."
            )
        return True, "tag is not restricted for this persona"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error_code": "PERSONA_RESOLUTION_UNKNOWN_KIND",
            "message": f"Unknown object kind {kind!r}.",
        },
    )


@router.get(
    "/personas/{persona_id}/resolution",
    response_model=PersonaResolution,
)
async def resolve_persona(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID,
    measure_id: UUID | None = Query(default=None),
    dimension_id: UUID | None = Query(default=None),
    hierarchy_id: UUID | None = Query(default=None),
    tag_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("viewer"),
) -> PersonaResolution:
    """Explain why a persona allows or denies one object.

    F-008-22: covers measures, dimensions, hierarchies and tag restrictions
    — exactly one of the four ``*_id`` params must be supplied. The legacy
    ``measure_id`` shape (with ``measure_allowed``) is preserved.
    """
    supplied = [
        ("measure", measure_id),
        ("dimension", dimension_id),
        ("hierarchy", hierarchy_id),
        ("tag", tag_id),
    ]
    chosen = [(k, v) for k, v in supplied if v is not None]
    if len(chosen) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "PERSONA_RESOLUTION_BAD_REQUEST",
                "message": (
                    "Supply exactly one of measure_id, dimension_id, "
                    "hierarchy_id or tag_id."
                ),
            },
        )
    kind, object_id = chosen[0]

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        p = await _load_or_404(db, model_id, persona_id)
        allowed, reason = await _resolve_object(db, p, kind, object_id)
        return PersonaResolution(
            persona_id=persona_id,
            object_kind=kind,
            object_id=object_id,
            allowed=allowed,
            # Legacy measure fields populated for the existing frontend caller.
            measure_id=object_id if kind == "measure" else None,
            measure_allowed=allowed if kind == "measure" else None,
            reason=reason,
        )
