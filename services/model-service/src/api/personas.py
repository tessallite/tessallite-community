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

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from shared.db.models import (
    Dimension,
    HierarchyDefinition,
    Measure,
    Persona,
    PersonaTagRestriction,
    data_tag_columns,
)
from shared.schemas.domains.aggregates_security import PERSONA_FILTER_OPERATORS
from shared.db.session import get_tenant_db
from shared.security.persona_resolver import (
    caller_roles,
    is_in_audience,
    is_privileged_by_role,
)
from shared.schemas.pydantic_models import (
    PersonaCreate,
    PersonaResolution,
    PersonaResponse,
    PersonaUpdate,
)
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
    return touched


def _to_response(
    p: Persona, restricted_column_ids: list[UUID] | None = None,
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
    """F-008-16: one default_filters value shape check (no DB needed).

    Scalar -> eq, list -> in, dict -> {operator: value} with the operator in
    the canonical supported set and BETWEEN carrying exactly two bounds.
    """
    if isinstance(raw, dict):
        if len(raw) != 1:
            return False
        op, val = next(iter(raw.items()))
        if op not in PERSONA_FILTER_OPERATORS:
            return False
        if op == "between":
            return isinstance(val, (list, tuple)) and len(val) == 2
        return True
    # scalar or list — always coercible
    return True


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

    # F-008-16: default_filters keys must be real dimension names and values
    # must use a supported operator (BETWEEN arity 2).
    if default_filters:
        dim_names = set(
            (
                await db.execute(
                    select(Dimension.name).where(Dimension.model_id == model_id)
                )
            ).scalars().all()
        )
        bad_keys = [k for k in default_filters if k not in dim_names]
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
        bad_vals = [
            k for k, v in default_filters.items() if not _filter_value_is_valid(v)
        ]
        if bad_vals:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "PERSONA_DEFAULT_FILTER_INVALID",
                    "message": (
                        "These default-filter values use an unsupported "
                        "operator or shape (a dict must be a single "
                        "{operator: value}; BETWEEN needs exactly two bounds): "
                        f"{', '.join(bad_vals)}."
                    ),
                },
            )


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
        # F-008-16 / F-008-21: reject foreign include ids and malformed
        # default filters before persisting (fail closed).
        await _validate_persona_scope(
            db, model_id,
            included_measure_ids=body.included_measure_ids,
            included_dimension_ids=body.included_dimension_ids,
            included_hierarchy_ids=body.included_hierarchy_ids,
            default_filters=body.default_filters,
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
        return _to_response(p)


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
            items = [p for p in items if is_in_audience(p, roles)]
        restricted = await _restricted_columns_by_persona(
            db, [p.id for p in items],
        )
        return [_to_response(p, restricted.get(p.id)) for p in items]


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
        return _to_response(p, restricted.get(p.id))


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
        restricted = await _restricted_columns_by_persona(db, [p.id])
        return _to_response(p, restricted.get(p.id))


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
        p = await _load_or_404(db, model_id, persona_id)
        await db.delete(p)
        await db.commit()


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
