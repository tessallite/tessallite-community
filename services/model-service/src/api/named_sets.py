"""Named Set CRUD + validation + preview routes."""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func as sa_func, select
from sqlalchemy.exc import IntegrityError

from shared.config.settings import get_settings
from shared.connector_qualify import safe_ident as _safe_ident
from shared.db.models import Dimension, Measure, NamedSet, NamedSetUsage, NamedSetVersion
from shared.db.session import get_tenant_db
from shared.named_list_compiler import CompilationError, compile_definition, explain_definition
from shared.schemas.pydantic_models import (
    CertifyRequest,
    DeprecateRequest,
    EntityUsageCreate,
    EntityUsageResponse,
    NamedSetCreate,
    NamedSetPreviewResponse,
    NamedSetResponse,
    NamedSetUpdate,
    NamedSetValidateRequest,
    NamedSetValidateResponse,
    VersionResponse,
)
from src.api._scope import ensure_model_in_project, purge_entity_soft_references
from src.auth.middleware import CurrentUser, get_current_user
from src.auth.rbac import require_role

log = logging.getLogger(__name__)
_settings = get_settings()

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/named-sets",
    tags=["named-sets"],
)


def _auto_compile(body_dict: dict, model_metadata: dict | None = None) -> dict:
    """If builder_definition is present, compile it to expression."""
    builder_def = body_dict.get("builder_definition")
    if builder_def:
        try:
            body_dict["expression"] = compile_definition(builder_def, model_metadata)
        except CompilationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Builder definition compilation failed: {exc}",
            )
    if not body_dict.get("expression"):
        raise HTTPException(
            status_code=422,
            detail="Either 'expression' or a valid 'builder_definition' is required",
        )
    return body_dict


@router.get("", response_model=list[NamedSetResponse])
async def list_named_sets(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[NamedSetResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        # F-018-04: verify the model belongs to the project in the path.
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        result = await db.execute(
            select(NamedSet).where(NamedSet.model_id == model_id).order_by(NamedSet.name)
        )
        return [NamedSetResponse.model_validate(ns) for ns in result.scalars().all()]
    return []


@router.get("/{named_set_id}", response_model=NamedSetResponse)
async def get_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        return NamedSetResponse.model_validate(ns)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "",
    response_model=NamedSetResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_named_set(
    project_id: UUID,
    model_id: UUID,
    body: NamedSetCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    data = _auto_compile(body.model_dump())
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = NamedSet(model_id=model_id, **data)
        db.add(ns)
        try:
            await db.flush()
            await _create_version(db, ns, current_user.email, "Created")
            await db.commit()
        except IntegrityError:
            # F-018-19: `UniqueConstraint(model_id, name)` collision — surface a
            # 409 with a clear message instead of leaking a raw 500.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A named set called '{data.get('name')}' already exists in this model.",
            )
        await db.refresh(ns)
        return NamedSetResponse.model_validate(ns)
    raise HTTPException(status_code=500, detail="DB session exhausted")


_DEFINITION_FIELDS = {"expression", "builder_definition", "list_type", "name", "display_name", "description", "display_folder", "scope", "dimensions"}


async def _create_version(db, ns: NamedSet, changed_by: str | None, summary: str | None) -> None:
    snap = {
        "name": ns.name,
        "display_name": ns.display_name,
        "description": ns.description,
        "display_folder": ns.display_folder,
        "expression": ns.expression,
        "builder_definition": ns.builder_definition,
        "list_type": ns.list_type,
        "scope": ns.scope,
        "dimensions": ns.dimensions,
        "certification_status": ns.certification_status,
    }
    # F-018-18: `max(version_number)+1` races between two concurrent updates.
    # The unique constraint `uq_named_set_version_number` makes the collision
    # loud; retry under a savepoint with a freshly recomputed max so the second
    # writer lands on the next free number instead of duplicating one.
    for _attempt in range(5):
        max_ver = await db.scalar(
            select(sa_func.max(NamedSetVersion.version_number))
            .where(NamedSetVersion.named_set_id == ns.id)
        )
        try:
            async with db.begin_nested():
                db.add(NamedSetVersion(
                    named_set_id=ns.id,
                    version_number=(max_ver or 0) + 1,
                    changed_by=changed_by,
                    change_summary=summary,
                    snapshot=snap,
                ))
            return
        except IntegrityError:
            # Another transaction took this version number first — recompute.
            continue
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Could not allocate a version number; please retry.",
    )


@router.patch(
    "/{named_set_id}",
    response_model=NamedSetResponse,
    dependencies=[require_role("modeler")],
)
async def update_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    body: NamedSetUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        updates = body.model_dump(exclude_unset=True)

        is_admin = current_user.role in ("admin", "tenant_admin", "system_admin")
        if "certification_status" in updates:
            requested = updates["certification_status"]
            if not is_admin and requested in ("certified", "deprecated"):
                raise HTTPException(status_code=403, detail="Only admins can set certified or deprecated status")

        if "builder_definition" in updates and updates["builder_definition"]:
            try:
                updates["expression"] = compile_definition(updates["builder_definition"])
            except CompilationError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

        changed_definition = any(k in _DEFINITION_FIELDS for k in updates)
        was_certified = ns.certification_status in ("certified", "shared")

        for k, v in updates.items():
            setattr(ns, k, v)

        if changed_definition and was_certified:
            ns.certification_status = "draft"

        if changed_definition:
            changed_keys = [k for k in updates if k in _DEFINITION_FIELDS]
            await _create_version(db, ns, current_user.email, f"Updated: {', '.join(changed_keys)}")

        try:
            await db.commit()
        except IntegrityError:
            # F-018-19: a rename can collide with the (model_id, name) unique
            # constraint just like create — return a 409 instead of a raw 500.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A named set called '{updates.get('name')}' already exists in this model.",
            )
        await db.refresh(ns)
        return NamedSetResponse.model_validate(ns)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete(
    "/{named_set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        # Purge the soft-referencing translation/preference rows that have no
        # FK back to the named set and would otherwise linger forever (F-029-15).
        await purge_entity_soft_references(db, model_id=model_id, entity_id=named_set_id)
        await db.delete(ns)
        await db.commit()


# ---- Validation endpoint (1C) ----

async def _get_model_metadata(db, model_id: UUID) -> dict:
    """Fetch dimensions and measures for validation context."""
    dims_q = await db.execute(
        select(Dimension).where(Dimension.model_id == model_id)
    )
    dims = [d.name for d in dims_q.scalars().all()]

    meas_q = await db.execute(
        select(Measure).where(Measure.model_id == model_id)
    )
    measures = [m.name for m in meas_q.scalars().all()]

    return {"dimensions": dims, "measures": measures}


_BLOCKED_MDX_FUNCTIONS = {
    "DRILLDOWNLEVEL", "DRILLUPLEVEL", "DRILLDOWNMEMBER",
    "DRILLDOWNMEMBERBOTTOM", "DRILLDOWNMEMBERTOP",
}


def _validate_expression(expression: str, model_metadata: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for an MDX expression."""
    errors: list[str] = []
    warnings: list[str] = []

    if not expression or not expression.strip():
        errors.append("Expression is empty")
        return errors, warnings

    upper = expression.upper()
    for blocked in _BLOCKED_MDX_FUNCTIONS:
        if blocked in upper:
            errors.append(f"Function {blocked} is not allowed in named sets")

    if upper.strip().startswith("SELECT"):
        warnings.append("Expression looks like a SELECT query rather than a set expression")

    dim_names = {d.lower() for d in model_metadata.get("dimensions", [])}
    measure_names = {m.lower() for m in model_metadata.get("measures", [])}

    # F-018-11: member keys are written as `&[key]`. Strip those tokens first —
    # `re.findall(r"\[([^\]]+)\]")` captures only the text inside the brackets,
    # so the `&` never reaches the per-ref loop and every fixed-member key
    # previously produced a false "not found" warning. Member keys are arbitrary
    # source values and are not part of the model's dimension/measure metadata,
    # so they must not be validated as references.
    expr_without_keys = re.sub(r"&\[[^\]]*\]", "", expression)

    refs = re.findall(r"\[([^\]]+)\]", expr_without_keys)
    for ref in refs:
        if ref.lower() == "measures":
            continue
        if ref.lower() in measure_names or ref.lower() in dim_names:
            continue
        if ref.lower() in ("model", "members", "all"):
            continue
        warnings.append(f"Reference [{ref}] not found in model metadata")

    return errors, warnings


@router.post(
    "/validate",
    response_model=NamedSetValidateResponse,
    dependencies=[require_role("viewer")],
)
async def validate_named_set(
    project_id: UUID,
    model_id: UUID,
    body: NamedSetValidateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetValidateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        model_metadata = await _get_model_metadata(db, model_id)
        expression = body.expression
        explanation = None
        compiled = None

        if body.builder_definition:
            try:
                expression = compile_definition(body.builder_definition, model_metadata)
                compiled = expression
                explanation = explain_definition(body.builder_definition)
            except CompilationError as exc:
                return NamedSetValidateResponse(
                    is_valid=False,
                    errors=[str(exc)],
                )

        if not expression:
            return NamedSetValidateResponse(
                is_valid=False,
                errors=["No expression or builder_definition provided"],
            )

        errors, warnings = _validate_expression(expression, model_metadata)

        cost_band = "low"
        upper = expression.upper()
        if "TOPCOUNT" in upper or "BOTTOMCOUNT" in upper or "FILTER" in upper:
            cost_band = "medium"
        if "CROSSJOIN" in upper or "GENERATE" in upper:
            cost_band = "high"

        return NamedSetValidateResponse(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            explanation=explanation,
            compiled_expression=compiled,
            estimated_cost_band=cost_band,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Preview endpoints (1D) ----

_FILTER_OPS = {"=": "=", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}




async def _resolve_dim_name(db, model_id: UUID, raw: str) -> str | None:
    result = await db.execute(
        select(Dimension.name).where(Dimension.model_id == model_id, Dimension.name == raw)
    )
    return result.scalar_one_or_none()


async def _resolve_measure(db, model_id: UUID, raw: str) -> tuple[str, str] | None:
    result = await db.execute(
        select(Measure.name, Measure.default_agg).where(
            Measure.model_id == model_id, Measure.name == raw
        )
    )
    row = result.one_or_none()
    return (row[0], row[1]) if row else None


_AGG_FUNCS = {"sum", "count", "avg", "min", "max", "count_distinct"}


def _agg_expr(measure_name: str, default_agg: str) -> str:
    agg = default_agg.lower() if default_agg.lower() in _AGG_FUNCS else "sum"
    ident = _safe_ident(measure_name)
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {ident})"
    return f"{agg.upper()}({ident})"


def _format_having_value(value: Any) -> str:
    """Quote string values, render numerics bare — mirrors the compiler's
    value handling (`named_list_compiler._compile_filter`)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # A numeric-looking string is compared numerically (the MDX compiler emits
    # `[Measures].[x] > 1000` with a bare literal); otherwise quote it.
    try:
        float(text)
        return text
    except ValueError:
        return "'" + text.replace("'", "''") + "'"


def _build_filter_having(
    conditions: list[dict],
    measure_aggs: dict[str, str],
    logic: str = "AND",
) -> tuple[str, list[str], list[str]]:
    """Build a HAVING clause whose semantics match the compiled MDX
    ``Filter([entity].Members, [Measures].[field] op value)``.

    The MDX `Filter` over a measure threshold is an aggregate test per member,
    so the preview must aggregate the measure per entity and apply the test in
    HAVING (not a row-level WHERE). Conditions whose field is not a known
    measure are dropped and reported as warnings so the preview never silently
    contradicts the deployed set (F-018-02).

    Returns ``(having_clause, dropped_field_warnings, used_agg_exprs)``. The
    aggregate expressions are surfaced so the caller can also project them in
    the SELECT list — the query-router's semantic binder requires an aggregated
    GROUP BY query to carry its aggregate in SELECT (the working topN preview
    does the same), otherwise it rejects the bare grouped dimension.
    """
    clauses: list[str] = []
    dropped: list[str] = []
    used_aggs: list[str] = []
    for cond in conditions:
        field = cond.get("field", "")
        op = _FILTER_OPS.get(cond.get("operator", ""), None)
        value = cond.get("value", "")
        if not field or op is None or value == "":
            if field:
                dropped.append(field)
            continue
        agg = measure_aggs.get(field)
        if agg is None:
            dropped.append(field)
            continue
        clauses.append(f"{agg} {op} {_format_having_value(value)}")
        if agg not in used_aggs:
            used_aggs.append(agg)
    if not clauses:
        return "", dropped, used_aggs
    joiner = f" {logic} " if logic in ("AND", "OR") else " AND "
    return " HAVING " + joiner.join(clauses), dropped, used_aggs


async def _resolve_measure_aggs(db, model_id: UUID, fields: set[str]) -> dict[str, str]:
    """Map each requested field that is a real measure to its aggregate SQL
    expression (e.g. ``SUM("total_sales")``), used to build the HAVING clause
    so the preview's aggregate semantics match the compiled MDX `Filter`."""
    fields = {f for f in fields if f}
    if not fields:
        return {}
    result = await db.execute(
        select(Measure.name, Measure.default_agg).where(
            Measure.model_id == model_id, Measure.name.in_(fields)
        )
    )
    return {name: _agg_expr(name, default_agg or "sum") for name, default_agg in result.all()}


async def _preview_from_builder(
    bd: dict | None,
    expression: str | None,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    db,
) -> tuple[list[dict], str | None, list[str]]:
    explanation = None
    items: list[dict] = []
    warnings: list[str] = []

    if bd:
        try:
            explanation = explain_definition(bd)
        except Exception:
            pass

    if bd and bd.get("type") == "fixedMembers":
        members = bd.get("members", [])
        # Bug-5257: a member can be a plain string or a dict with key/caption
        # fields. Extract the string values so both fields are always strings,
        # not a whole dict.
        resolved: list[dict] = []
        for i, m in enumerate(members[:100]):
            if isinstance(m, dict):
                key = str(m.get("key", ""))
                caption = str(m.get("caption", key))
            else:
                key = str(m)
                caption = key
            resolved.append({"ordinal": i + 1, "caption": caption, "key": key})
        items = resolved
    elif bd and bd.get("type") == "topN":
        dim = await _resolve_dim_name(db, model_id, bd.get("entity", ""))
        measure_info = await _resolve_measure(db, model_id, bd.get("measure", ""))
        count = bd.get("count", 10)
        direction = "DESC" if bd.get("direction", "top") == "top" else "ASC"
        if dim and measure_info:
            measure_name, default_agg = measure_info
            agg = _agg_expr(measure_name, default_agg)
            sql = (
                f"SELECT {_safe_ident(dim)}, {agg} AS _rank_val "
                f"FROM {_safe_ident(model_slug)} "
                f"GROUP BY {_safe_ident(dim)} "
                f"ORDER BY _rank_val {direction} LIMIT {int(count)}"
            )
            try:
                result = await _execute_via_router(model_id, sql, bearer)
                rows = result.get("rows", [])
                for i, row in enumerate(rows[:100]):
                    if isinstance(row, dict):
                        val = next(iter(row.values()), "")
                        items.append({"ordinal": i + 1, "caption": str(val), "key": str(val)})
            except Exception as exc:
                log.warning("Preview query failed: %s", exc)
    elif bd and bd.get("type") == "filter":
        dim = await _resolve_dim_name(db, model_id, bd.get("entity", ""))
        if dim:
            conditions = bd.get("conditions", [])
            cond_fields = {c.get("field", "") for c in conditions}
            # The compiler emits each condition field as `[Measures].[field]`
            # with aggregate-threshold semantics, so resolve fields against
            # measures and apply the test in HAVING over a GROUP BY entity —
            # the same membership the deployed MDX `Filter` produces (F-018-02).
            measure_aggs = await _resolve_measure_aggs(db, model_id, cond_fields)
            having, dropped, used_aggs = _build_filter_having(
                conditions, measure_aggs, bd.get("logic", "AND")
            )
            if dropped:
                warnings.append(
                    "These condition field(s) are not measures and were ignored "
                    "in the preview, which may differ from the deployed set: "
                    + ", ".join(sorted(set(dropped)))
                )
            entity_ident = _safe_ident(dim)
            if having:
                # Project the aggregate(s) alongside the entity so the binder
                # accepts the grouped query (same shape as the topN preview);
                # only the first column (the entity) is read into members.
                select_aggs = "".join(
                    f", {agg} AS _agg_{i}" for i, agg in enumerate(used_aggs)
                )
                sql = (
                    f"SELECT {entity_ident}{select_aggs} "
                    f"FROM {_safe_ident(model_slug)} "
                    f"GROUP BY {entity_ident}{having} LIMIT 100"
                )
            else:
                # No applicable condition survived — fall back to the unfiltered
                # member list, but the warning above tells the modeller why.
                sql = (
                    f"SELECT DISTINCT {entity_ident} "
                    f"FROM {_safe_ident(model_slug)} LIMIT 100"
                )
            try:
                result = await _execute_via_router(model_id, sql, bearer)
                rows = result.get("rows", [])
                for i, row in enumerate(rows[:100]):
                    if isinstance(row, dict):
                        val = next(iter(row.values()), "")
                        items.append({"ordinal": i + 1, "caption": str(val), "key": str(val)})
            except Exception as exc:
                log.warning("Preview query failed: %s", exc)
    else:
        if not explanation and expression:
            explanation = "Preview is not supported for raw/advanced MDX expressions. Deploy the model and use a BI tool (Excel, Power BI) with an XMLA connection to see member results."

    return items, explanation, warnings


@router.post(
    "/preview-by-definition",
    response_model=NamedSetPreviewResponse,
    dependencies=[require_role("viewer")],
)
async def preview_named_set_by_definition(
    project_id: UUID,
    model_id: UUID,
    body: NamedSetValidateRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetPreviewResponse:
    """Preview a named set from expression/builder_definition without saving."""
    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        bearer = request.headers.get("Authorization", "").replace("Bearer ", "")
        items, explanation, warnings = await _preview_from_builder(
            body.builder_definition, body.expression, model_id, model.slug, bearer, db,
        )
        return NamedSetPreviewResponse(
            items=items,
            total_count=len(items),
            truncated=len(items) >= 100,
            explanation=explanation,
            warnings=warnings,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/{named_set_id}/preview",
    response_model=NamedSetPreviewResponse,
    dependencies=[require_role("viewer")],
)
async def preview_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetPreviewResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        bearer = request.headers.get("Authorization", "").replace("Bearer ", "")
        items, explanation, warnings = await _preview_from_builder(
            ns.builder_definition, ns.expression, model_id, model.slug, bearer, db,
        )
        return NamedSetPreviewResponse(
            items=items,
            total_count=len(items),
            truncated=len(items) >= 100,
            explanation=explanation,
            warnings=warnings,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Version History endpoints (Phase 4) ----

@router.get(
    "/{named_set_id}/versions",
    response_model=list[VersionResponse],
    dependencies=[require_role("viewer")],
)
async def list_named_set_versions(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[VersionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        result = await db.execute(
            select(NamedSetVersion)
            .where(NamedSetVersion.named_set_id == named_set_id)
            .order_by(NamedSetVersion.version_number.desc())
        )
        return [VersionResponse.model_validate(v, from_attributes=True) for v in result.scalars().all()]
    return []


@router.post(
    "/{named_set_id}/versions/{version_number}/revert",
    response_model=NamedSetResponse,
    dependencies=[require_role("modeler")],
)
async def revert_named_set_version(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    version_number: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        result = await db.execute(
            select(NamedSetVersion)
            .where(NamedSetVersion.named_set_id == named_set_id)
            .where(NamedSetVersion.version_number == version_number)
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise HTTPException(status_code=404, detail=f"Version {version_number} not found")

        snap = version.snapshot
        # F-018-05: a revert restores the *definition*, never the governance
        # state. `certification_status` is deliberately excluded so a modeler
        # cannot re-certify a set by reverting to a snapshot that was certified
        # before an admin deprecated it (certify/deprecate require admin). The
        # set keeps its current certification status; if the reverted definition
        # differs from the certified one, the standard auto-demote in
        # `update_named_set` does not apply here, so we demote a certified set to
        # draft when the restored definition is not identical.
        prior_status = ns.certification_status
        restorable = (
            "name", "display_name", "description", "display_folder",
            "expression", "builder_definition", "list_type", "scope", "dimensions",
        )
        definition_changed = any(
            k in snap and getattr(ns, k) != snap[k] for k in restorable
        )
        for k in restorable:
            if k in snap:
                setattr(ns, k, snap[k])
        if definition_changed and prior_status in ("certified", "shared"):
            ns.certification_status = "draft"

        await _create_version(db, ns, current_user.email, f"Reverted to version {version_number}")
        await db.commit()
        await db.refresh(ns)
        return NamedSetResponse.model_validate(ns)
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Certification endpoints (Phase 4) ----

@router.post(
    "/{named_set_id}/certify",
    response_model=NamedSetResponse,
    dependencies=[require_role("admin")],
)
async def certify_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    _body: CertifyRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        if ns.certification_status == "certified":
            return NamedSetResponse.model_validate(ns)
        ns.certification_status = "certified"
        await _create_version(db, ns, current_user.email, "Certified")
        await db.commit()
        await db.refresh(ns)
        return NamedSetResponse.model_validate(ns)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/{named_set_id}/deprecate",
    response_model=NamedSetResponse,
    dependencies=[require_role("admin")],
)
async def deprecate_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    body: DeprecateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        # Bug-5259: validate that replacement_id (if supplied) references
        # an existing named set in the same model. Without this check, a
        # caller could point the deprecation at a nonexistent or cross-model
        # named set, producing a dangling reference.
        if body.replacement_id is not None:
            replacement = await db.get(NamedSet, body.replacement_id)
            if replacement is None or replacement.model_id != model_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "replacement_id must reference an existing named set "
                        "in the same model."
                    ),
                )
        ns.certification_status = "deprecated"
        ns.replacement_id = body.replacement_id
        summary = "Deprecated"
        if body.replacement_id:
            summary += f" (replacement: {body.replacement_id})"
        await _create_version(db, ns, current_user.email, summary)
        await db.commit()
        await db.refresh(ns)
        return NamedSetResponse.model_validate(ns)
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Usage Tracking endpoint (Phase 5) ----

@router.post(
    "/{named_set_id}/usage",
    response_model=EntityUsageResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("viewer")],
)
async def report_named_set_usage(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    body: EntityUsageCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> EntityUsageResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        usage = NamedSetUsage(
            named_set_id=named_set_id,
            workbook_id=body.workbook_id,
            worksheet=body.worksheet,
            cell_reference=body.cell_reference,
            usage_type=body.usage_type,
            reported_by=current_user.email,
        )
        db.add(usage)
        await db.commit()
        await db.refresh(usage)
        return EntityUsageResponse.model_validate(usage, from_attributes=True)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get(
    "/{named_set_id}/usage",
    response_model=list[EntityUsageResponse],
    dependencies=[require_role("viewer")],
)
async def list_named_set_usage(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[EntityUsageResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        result = await db.execute(
            select(NamedSetUsage)
            .where(NamedSetUsage.named_set_id == named_set_id)
            .order_by(NamedSetUsage.reported_at.desc())
        )
        return [EntityUsageResponse.model_validate(u, from_attributes=True) for u in result.scalars().all()]
    return []


async def _execute_via_router(
    model_id: UUID,
    query: str,
    bearer: str,
    timeout_s: float = 30.0,
) -> dict:
    """POST a SQL query to the query-router's /execute endpoint."""
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/execute"
    headers = {"Authorization": f"Bearer {bearer}"}
    body = {
        "model_id": str(model_id),
        "raw_query": query,
        "protocol": "jdbc",
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if not isinstance(detail, str) or not detail:
                    detail = resp.text
            except Exception:
                detail = resp.text or f"Query router returned HTTP {resp.status_code}"
            raise ValueError(detail)
        return resp.json()
