"""KPI CRUD + evaluate + validate routes."""
from __future__ import annotations

import logging
from datetime import date
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func as sa_func, select

from shared.audit.logger import audit
from shared.config.settings import get_settings
from shared.db.models import (
    KPI,
    KPILatest,
    KPISnapshot,
    Dimension,
    KPIUsage,
    KPIVersion,
    Measure,
    Model,
    ModelColumn,
    ModelParameter,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    CertifyRequest,
    DeprecateRequest,
    EntityUsageCreate,
    EntityUsageResponse,
    KPIAdhocRequest,
    KPIBatchRequest,
    KPIBatchResponse,
    KPICreate,
    KPIEvaluateResponse,
    KPIResponse,
    KPISnapshotResponse,
    KPITrendPoint,
    KPIUpdate,
    KPIValidateExpressionRequest,
    KPIValidationDiagnostic,
    KPIValidationResponse,
    VersionResponse,
)
from shared.middleware.internal_bypass import (
    INTERNAL_BYPASS_HEADER,
    is_internal_request_header,
)
from shared.semantic.kpi_dependency import analyse_dependencies, build_graph, topological_sort
from shared.semantic.kpi_expression import extract_measure_names, validate_expression
from shared.auth.project_access import load_authorized_model
from src.api._persona_scope import parse_allowed_ids, resolve_effective_persona
from src.api._scope import ensure_model_in_project, purge_entity_soft_references
from src.auth.middleware import CurrentUser, get_current_user
from src.kpi_threshold import validate_bands
from src.auth.rbac import require_role
from src.kpi_cache import get_kpi_cache
from src.kpi_compiler import (
    CompilerContext,
    compile_expression,
    derive_ti_decomposition,
    derive_ti_decomposition_from_node,
    expression_has_time_intelligence,
)
from src.kpi_composite import (
    COMPOSITE_STATUS_ERROR,
    CompositeResult,
    build_composite_children,
    evaluate_composite,
    get_normalisation_config,
)
from src.kpi_evaluator import (
    EvaluationContext,
    MeasureValueProvider,
    evaluate_kpi as run_evaluation_pipeline,
)

log = logging.getLogger(__name__)
_settings = get_settings()


_CLOSER_RATIO_BANDS = [
    {"label": "Off Target",  "color": "#D32F2F", "min": None, "max": 0.80},
    {"label": "Near Target", "color": "#F57C00", "min": 0.80, "max": 0.90},
    {"label": "On Track",    "color": "#388E3C", "min": 0.90, "max": None},
]


async def _ensure_kpi_model_scope(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await load_authorized_model(
            db,
            current_user,
            project_id=project_id,
            model_id=model_id,
            min_role="viewer",
        )
        return
    raise HTTPException(status_code=500, detail="DB session exhausted")


def _coerce_closer_absolute(data: dict) -> None:
    """Coerce closer_is_better + absolute_value to percentage_of_target.

    One-sided absolute bands cannot express two-sided closeness, so
    closer KPIs must always use percentage_of_target evaluation.
    Both the type AND the bands must be converted — flipping the type
    alone leaves target-scaled bands that mis-match the ratio evaluation.
    """
    if data.get("direction") != "closer_is_better":
        return
    pm = data.get("presentation_meta")
    if not pm or not isinstance(pm, dict):
        return
    if pm.get("evaluation_type") == "absolute_value":
        pm["evaluation_type"] = "percentage_of_target"
        pm["bands"] = [dict(b) for b in _CLOSER_RATIO_BANDS]

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/kpis",
    tags=["kpis"],
    dependencies=[Depends(_ensure_kpi_model_scope)],
)


def _kpi_visible_to_persona(
    kpi: KPI,
    allowed_measure_ids: list[UUID] | None,
    measure_name_to_id: dict[str, UUID],
) -> bool:
    """Return True if all KPI measure dependencies are in the persona scope.

    If *allowed_measure_ids* is ``None`` the persona is unrestricted.
    """
    if allowed_measure_ids is None:
        return True
    allowed_set = set(allowed_measure_ids)
    referenced: set[str] = set()
    for expression in (
        getattr(kpi, "expression", None),
        getattr(kpi, "target_expression", None),
    ):
        if expression:
            referenced.update(extract_measure_names(expression))
    for name in referenced:
        mid = measure_name_to_id.get(name)
        if mid is None or mid not in allowed_set:
            return False
    target_measure_id = getattr(kpi, "target_measure_id", None)
    if target_measure_id is not None:
        try:
            target_measure_uuid = (
                target_measure_id
                if isinstance(target_measure_id, UUID)
                else UUID(str(target_measure_id))
            )
        except (TypeError, ValueError):
            return False
        if target_measure_uuid not in allowed_set:
            return False
    return True


def _assert_kpi_expressions_in_measure_scope(
    expressions: list[str | None],
    allowed_names: set[str],
) -> None:
    referenced: set[str] = set()
    for expression in expressions:
        if expression:
            referenced.update(extract_measure_names(expression))
    if referenced - allowed_names:
        raise HTTPException(status_code=404, detail="KPI measure not found")


async def _load_visible_kpi_or_404(
    db,
    *,
    project_id: UUID,
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser,
    persona_id: UUID | None = None,
) -> KPI:
    await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
    kpi = await db.get(KPI, kpi_id)
    if kpi is None or kpi.model_id != model_id:
        raise HTTPException(status_code=404, detail="KPI not found")

    is_privileged = current_user.role in (
        "modeler", "admin", "tenant_admin", "system_admin",
    )
    if kpi.certification_status == "draft" and not is_privileged:
        raise HTTPException(status_code=404, detail="KPI not found")

    persona = await resolve_effective_persona(
        db, current_user=current_user, model_id=model_id,
        requested_persona_id=persona_id,
    )
    if persona:
        allowed = parse_allowed_ids(persona.included_measure_ids)
        if allowed is not None:
            measures_result = await db.execute(
                select(Measure).where(Measure.model_id == model_id)
            )
            name_to_id = {m.name: m.id for m in measures_result.scalars().all()}
            if not _kpi_visible_to_persona(kpi, allowed, name_to_id):
                raise HTTPException(status_code=404, detail="KPI not found")
    return kpi


@router.get("", response_model=list[KPIResponse])
async def list_kpis(
    model_id: UUID,
    persona_id: UUID | None = Query(default=None),
    deployed_only: bool = Query(
        default=False,
        description=(
            "When true, return only deployed KPIs. The gateway passes this for "
            "BI catalogue surfaces (XMLA MDSCHEMA_KPIS, JDBC $KPIs) so an "
            "undeployed KPI is invisible to BI clients; the model builder omits "
            "it so modellers keep seeing drafts (F-017-05)."
        ),
    ),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[KPIResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        query = select(KPI).where(KPI.model_id == model_id).order_by(KPI.name)

        # Draft KPIs are only visible to modelers/admins (Section 13.1)
        is_privileged = current_user.role in (
            "modeler", "admin", "tenant_admin", "system_admin",
        )
        if not is_privileged:
            query = query.where(KPI.certification_status != "draft")

        # F-017-05: BI catalogue surfaces request deployed-only. Undeployed
        # KPIs never reach JDBC/XMLA clients; modellers (builder) omit the flag
        # and continue to see undeployed drafts.
        if deployed_only:
            query = query.where(KPI.is_deployed.is_(True))

        result = await db.execute(query)
        kpis = list(result.scalars().all())

        # Persona-based measure filtering (Section 13.1)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        if persona:
            allowed = parse_allowed_ids(persona.included_measure_ids)
            if allowed is not None:
                measures_result = await db.execute(
                    select(Measure).where(Measure.model_id == model_id)
                )
                name_to_id = {m.name: m.id for m in measures_result.scalars().all()}
                kpis = [k for k in kpis if _kpi_visible_to_persona(k, allowed, name_to_id)]

        return [KPIResponse.model_validate(k) for k in kpis]
    return []


@router.get("/{kpi_id}", response_model=KPIResponse)
async def get_kpi(
    model_id: UUID,
    kpi_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        # Draft KPIs are only visible to modelers/admins (Section 13.1)
        is_privileged = current_user.role in (
            "modeler", "admin", "tenant_admin", "system_admin",
        )
        if kpi.certification_status == "draft" and not is_privileged:
            raise HTTPException(status_code=404, detail="KPI not found")

        # Persona-based measure filtering (Section 13.1)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        if persona:
            allowed = parse_allowed_ids(persona.included_measure_ids)
            if allowed is not None:
                measures_result = await db.execute(
                    select(Measure).where(Measure.model_id == model_id)
                )
                name_to_id = {m.name: m.id for m in measures_result.scalars().all()}
                if not _kpi_visible_to_persona(kpi, allowed, name_to_id):
                    raise HTTPException(status_code=404, detail="KPI not found")

        return KPIResponse.model_validate(kpi)
    raise HTTPException(status_code=500, detail="DB session exhausted")


async def _would_cycle(db, parent_id: UUID, child_id: UUID) -> bool:
    """Walk up the ancestor chain from parent_id; return True if child_id is found."""
    visited: set[UUID] = set()
    current = parent_id
    while current:
        if current == child_id:
            return True
        if current in visited:
            return True
        visited.add(current)
        node = await db.get(KPI, current)
        current = node.parent_kpi_id if node else None
    return False


# ---- Expression validation ----

async def _load_model_names(
    db, model_id: UUID,
) -> tuple[set[str], set[str], set[str]]:
    """Load measure, KPI, and dimension names for a model."""
    measure_rows = await db.execute(
        select(Measure.name).where(Measure.model_id == model_id)
    )
    measure_names = {row[0] for row in measure_rows}

    kpi_rows = await db.execute(
        select(KPI.name).where(KPI.model_id == model_id)
    )
    kpi_names = {row[0] for row in kpi_rows}

    dim_rows = await db.execute(
        select(Dimension.name).where(Dimension.model_id == model_id)
    )
    dim_names = {row[0] for row in dim_rows}

    return measure_names, kpi_names, dim_names


def _validation_result_to_response(result) -> KPIValidationResponse:
    """Convert a kpi_expression.ValidationResult to a Pydantic response."""
    return KPIValidationResponse(
        valid=result.valid,
        errors=[
            KPIValidationDiagnostic(
                code=e.code,
                message=e.message,
                position=e.position,
                suggestion=e.suggestion,
            )
            for e in result.errors
        ],
        warnings=[
            KPIValidationDiagnostic(
                code=w.code,
                message=w.message,
                position=w.position,
                suggestion=w.suggestion,
            )
            for w in result.warnings
        ],
        referenced_measures=result.referenced_measures,
        referenced_kpis=result.referenced_kpis,
        referenced_dimensions=result.referenced_dimensions,
        has_time_intelligence=result.has_time_intelligence,
        requires_time_dimension=result.requires_time_dimension,
        detected_agg_mode=result.detected_agg_mode,
        expression_tree=result.expression_tree,
    )


async def _validate_kpi_expression(
    db, model_id: UUID, expression: str | None,
) -> KPIValidationResponse | None:
    """Validate a KPI expression against the model. Returns None if no expression."""
    if not expression:
        return None
    measure_names, kpi_names, dim_names = await _load_model_names(db, model_id)
    result = validate_expression(
        expression,
        model_measures=measure_names,
        model_kpis=kpi_names,
        model_dimensions=dim_names,
    )
    return _validation_result_to_response(result)


async def _check_expression_cycles(
    db, model_id: UUID, kpi_name: str, expression: str | None,
) -> list[str] | None:
    """Check if the expression would create cycles in the KPI dependency graph.

    Returns a list of cycle path strings if cycles are found, or None.
    """
    if not expression:
        return None

    # Load all KPIs for this model
    result = await db.execute(
        select(KPI.id, KPI.name, KPI.expression)
        .where(KPI.model_id == model_id)
    )
    kpis = [
        {"id": row[0], "name": row[1], "expression": row[2]}
        for row in result
    ]

    # Replace the current KPI's expression or add it if new
    found = False
    for k in kpis:
        if k["name"] == kpi_name:
            k["expression"] = expression
            found = True
            break
    if not found:
        import uuid
        kpis.append({"id": uuid.uuid4(), "name": kpi_name, "expression": expression})

    dep_result = analyse_dependencies(kpis)
    if dep_result.cycles:
        return [c.message for c in dep_result.cycles]
    return None


@router.post(
    "/validate-expression",
    response_model=KPIValidationResponse,
    dependencies=[require_role("modeler")],
)
async def validate_kpi_expression(
    model_id: UUID,
    body: KPIValidateExpressionRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIValidationResponse:
    """Validate a KPI expression against the model without creating a KPI."""
    async for db in get_tenant_db(current_user.tenant_id):
        measure_names, kpi_names, dim_names = await _load_model_names(db, model_id)

        result = validate_expression(
            body.expression,
            model_measures=measure_names,
            model_kpis=kpi_names,
            model_dimensions=dim_names,
        )
        response = _validation_result_to_response(result)

        # Also validate target expression if provided
        if body.target_expression:
            target_result = validate_expression(
                body.target_expression,
                model_measures=measure_names,
                model_kpis=kpi_names,
                model_dimensions=dim_names,
            )
            if not target_result.valid:
                response.valid = False
                response.errors.extend([
                    KPIValidationDiagnostic(
                        code=f"TARGET_{e.code}",
                        message=f"Target expression: {e.message}",
                        position=e.position,
                        suggestion=e.suggestion,
                    )
                    for e in target_result.errors
                ])

        return response
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- CRUD ----

# Fields that constitute the KPI's "definition" for versioning and
# certification-status reset purposes.
_DEFINITION_FIELDS = {
    "name", "display_name", "description", "display_folder",
    # v2 expression
    "kpi_type", "expression", "calc_agg_mode",
    "inner_agg", "inner_grain", "outer_agg",
    # Semi-additive
    "at_grain", "non_additive_agg", "carry_forward",
    # Target
    "target_type", "target_value", "target_measure_id",
    "target_expression", "target_period",
    # Direction and thresholds
    "direction", "presentation_type", "presentation_meta",
    # Trend
    "trend_period", "trend_threshold", "trend_sparkline_periods",
    # Formatting
    "format_token", "format_custom", "unit_label", "null_display_value",
    # Hierarchy
    "weight", "parent_kpi_id", "indicator_type",
    # Time dimension
    "time_dimension_id",
    # Snapshots
    "snapshot_frequency", "snapshot_retention",
    "status_graphic", "trend_graphic",
}


async def _create_kpi_version(
    db, kpi: KPI, changed_by: str | None, summary: str | None,
) -> None:
    max_ver = await db.scalar(
        select(sa_func.max(KPIVersion.version_number))
        .where(KPIVersion.kpi_id == kpi.id)
    )
    snap = {
        # Core
        "name": kpi.name,
        "display_name": kpi.display_name,
        "description": kpi.description,
        "display_folder": kpi.display_folder,
        # v2 expression
        "kpi_type": kpi.kpi_type,
        "expression": kpi.expression,
        "calc_agg_mode": kpi.calc_agg_mode,
        "inner_agg": kpi.inner_agg,
        "inner_grain": kpi.inner_grain,
        "outer_agg": kpi.outer_agg,
        # Semi-additive
        "at_grain": kpi.at_grain,
        "non_additive_agg": kpi.non_additive_agg,
        "carry_forward": kpi.carry_forward,
        # Target
        "target_type": kpi.target_type,
        "target_value": float(kpi.target_value) if kpi.target_value is not None else None,
        "target_measure_id": str(kpi.target_measure_id) if kpi.target_measure_id else None,
        "target_expression": kpi.target_expression,
        "target_period": kpi.target_period,
        # Direction and thresholds
        "direction": kpi.direction,
        "presentation_type": kpi.presentation_type,
        "presentation_meta": kpi.presentation_meta,
        # Trend
        "trend_period": kpi.trend_period,
        "trend_threshold": float(kpi.trend_threshold) if kpi.trend_threshold is not None else None,
        "trend_sparkline_periods": kpi.trend_sparkline_periods,
        # Formatting
        "format_token": kpi.format_token,
        "format_custom": kpi.format_custom,
        "unit_label": kpi.unit_label,
        "null_display_value": kpi.null_display_value,
        # Hierarchy
        "weight": kpi.weight,
        "parent_kpi_id": str(kpi.parent_kpi_id) if kpi.parent_kpi_id else None,
        "indicator_type": kpi.indicator_type,
        # Time dimension
        "time_dimension_id": str(kpi.time_dimension_id) if kpi.time_dimension_id else None,
        # Governance
        "certification_status": kpi.certification_status,
        # Snapshots
        "snapshot_frequency": kpi.snapshot_frequency,
        "snapshot_retention": kpi.snapshot_retention,
        "status_graphic": kpi.status_graphic,
        "trend_graphic": kpi.trend_graphic,
    }
    db.add(KPIVersion(
        kpi_id=kpi.id,
        version_number=(max_ver or 0) + 1,
        changed_by=changed_by,
        change_summary=summary,
        snapshot=snap,
    ))


_DATE_TYPE_MARKERS = ("date", "time", "timestamp")


async def _dimension_data_types(db, dims: list[Dimension]) -> dict[str, str]:
    """Return source-column data types keyed by dimension id."""
    source_column_ids = {d.source_column_id for d in dims if d.source_column_id}
    if not source_column_ids:
        return {}

    result = await db.execute(
        select(ModelColumn).where(ModelColumn.id.in_(source_column_ids))
    )
    col_types = {c.id: (c.data_type or "").lower() for c in result.scalars().all()}
    return {str(d.id): col_types.get(d.source_column_id, "") for d in dims}


def _is_time_dimension(dim: Dimension, data_types: dict[str, str]) -> bool:
    if dim.is_time_dim:
        return True
    data_type = data_types.get(str(dim.id), "")
    return any(marker in data_type for marker in _DATE_TYPE_MARKERS)


async def _validate_and_compile_business_definition(
    db, model_id: UUID, data: dict,
) -> None:
    """Validate and compile business_definition, merging results into *data*.

    If ``business_definition`` is present, this function validates it against
    the model's measures/dimensions/parameters, compiles it to a KPI
    expression + filter/time-window predicates, and populates the legacy
    fields (``expression``, ``kpi_type``, ``time_dimension_id``, etc.) so
    that the existing evaluation pipeline works without changes.
    """
    from src.kpi_business_builder import (
        compile_business_definition,
        validate_business_definition,
    )

    bd = data.get("business_definition")
    if not bd or not isinstance(bd, dict):
        return

    measures_result = await db.execute(
        select(Measure).where(Measure.model_id == model_id)
    )
    measures = list(measures_result.scalars().all())
    measure_ids = {str(m.id) for m in measures}
    measure_name_map = {str(m.id): m.name for m in measures}

    dims_result = await db.execute(
        select(Dimension).where(Dimension.model_id == model_id)
    )
    dims = list(dims_result.scalars().all())
    dimension_ids = {str(d.id) for d in dims}
    dimension_name_map = {str(d.id): d.name for d in dims}
    dimension_data_types = await _dimension_data_types(db, dims)
    time_dimension_ids = {
        str(d.id) for d in dims
        if _is_time_dimension(d, dimension_data_types)
    }
    time_dim_name_map = {str(d.id): d.name for d in dims if str(d.id) in time_dimension_ids}

    params_result = await db.execute(
        select(ModelParameter).where(ModelParameter.model_id == model_id)
    )
    param_names = {p.name for p in params_result.scalars().all()}

    errors = validate_business_definition(
        bd, measure_ids, dimension_ids, time_dimension_ids, param_names,
    )
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"message": "Invalid business definition", "errors": errors},
        )

    compiled = compile_business_definition(
        bd, measure_name_map, dimension_name_map, time_dim_name_map,
    )

    if not data.get("expression"):
        data["expression"] = compiled.expression
    if compiled.kpi_type and not data.get("kpi_type"):
        data["kpi_type"] = compiled.kpi_type
    if compiled.direction:
        data.setdefault("direction", compiled.direction)
    if compiled.target_type:
        data.setdefault("target_type", compiled.target_type)
    if compiled.target_value is not None:
        data.setdefault("target_value", compiled.target_value)
    if compiled.target_expression:
        data.setdefault("target_expression", compiled.target_expression)

    if compiled.time_dimension_name and not data.get("time_dimension_id"):
        for d in dims:
            if d.name == compiled.time_dimension_name:
                data["time_dimension_id"] = d.id
                break

    bd["_compiled"] = {
        "expression": compiled.expression,
        "filter_predicates": compiled.filter_predicates,
        "time_window_predicates": compiled.time_window_predicates,
        "where_clause": compiled.where_clause,
        "summary": compiled.summary,
        "summary_tokens": compiled.summary_tokens,
        "base_expression": compiled.base_expression,
        "ti_type": compiled.ti_type,
        "ti_grain": compiled.ti_grain,
        "ti_n_periods": compiled.ti_n_periods,
        "time_window_start_sql": compiled.time_window_start_sql,
        "time_window_end_sql": compiled.time_window_end_sql,
        "share_type": compiled.share_type,
        "share_dimension": compiled.share_dimension,
        "share_n": compiled.share_n,
    }
    data["business_definition"] = bd


@router.post(
    "",
    response_model=KPIResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_kpi(
    model_id: UUID,
    body: KPICreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        data = body.model_dump()

        # Validate and compile business definition (v3 builder)
        await _validate_and_compile_business_definition(db, model_id, data)

        # Validate parent_kpi_id
        if data.get("parent_kpi_id"):
            parent = await db.get(KPI, data["parent_kpi_id"])
            if not parent or parent.model_id != model_id:
                raise HTTPException(
                    status_code=400,
                    detail="parent_kpi_id does not belong to this model",
                )

        # Validate expression (if provided)
        if data.get("expression"):
            validation = await _validate_kpi_expression(db, model_id, data["expression"])
            if validation and not validation.valid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Invalid KPI expression",
                        "errors": [e.model_dump() for e in validation.errors],
                    },
                )

            # Check for dependency cycles
            cycles = await _check_expression_cycles(
                db, model_id, data["name"], data["expression"],
            )
            if cycles:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "KPI expression would create a dependency cycle",
                        "cycles": cycles,
                    },
                )

        # Validate target expression (if provided)
        if data.get("target_expression"):
            validation = await _validate_kpi_expression(
                db, model_id, data["target_expression"],
            )
            if validation and not validation.valid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Invalid target expression",
                        "errors": [e.model_dump() for e in validation.errors],
                    },
                )

        # Validate target_measure_id
        if data.get("target_measure_id"):
            m = await db.get(Measure, data["target_measure_id"])
            if not m or m.model_id != model_id:
                raise HTTPException(
                    status_code=400,
                    detail="target_measure_id does not belong to this model",
                )

        # Validate threshold bands in presentation_meta
        pm = data.get("presentation_meta")
        if pm and isinstance(pm, dict) and "bands" in pm:
            band_errors = validate_bands(pm["bands"])
            if band_errors:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Invalid threshold band configuration",
                        "errors": band_errors,
                    },
                )

        _coerce_closer_absolute(data)

        kpi = KPI(model_id=model_id, created_by=current_user.email, **data)
        db.add(kpi)
        await db.flush()
        await _create_kpi_version(db, kpi, current_user.email, "Created")
        await audit(
            db, action="kpi.create", severity="info",
            actor_email=current_user.email,
            target_type="kpi", target_id=kpi.id,
            target_name=kpi.display_name or kpi.name,
            detail={"expression": kpi.expression, "model_id": str(model_id)},
        )
        await db.commit()
        await db.refresh(kpi)
        return KPIResponse.model_validate(kpi)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.patch(
    "/{kpi_id}",
    response_model=KPIResponse,
    dependencies=[require_role("modeler")],
)
async def update_kpi(
    model_id: UUID,
    kpi_id: UUID,
    body: KPIUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        updates = body.model_dump(exclude_unset=True)

        # Validate and compile business definition (v3 builder)
        if "business_definition" in updates:
            await _validate_and_compile_business_definition(db, model_id, updates)

        # Validate parent_kpi_id
        if updates.get("parent_kpi_id"):
            parent = await db.get(KPI, updates["parent_kpi_id"])
            if not parent or parent.model_id != model_id:
                raise HTTPException(
                    status_code=400,
                    detail="parent_kpi_id does not belong to this model",
                )
            if await _would_cycle(db, updates["parent_kpi_id"], kpi_id):
                raise HTTPException(
                    status_code=400,
                    detail="parent_kpi_id would create a cycle",
                )

        # Validate expression (if changed)
        if "expression" in updates and updates["expression"]:
            validation = await _validate_kpi_expression(
                db, model_id, updates["expression"],
            )
            if validation and not validation.valid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Invalid KPI expression",
                        "errors": [e.model_dump() for e in validation.errors],
                    },
                )

            # Check for dependency cycles with the updated expression
            kpi_name = updates.get("name", kpi.name)
            cycles = await _check_expression_cycles(
                db, model_id, kpi_name, updates["expression"],
            )
            if cycles:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "KPI expression would create a dependency cycle",
                        "cycles": cycles,
                    },
                )

        # Validate target expression (if changed)
        if "target_expression" in updates and updates["target_expression"]:
            validation = await _validate_kpi_expression(
                db, model_id, updates["target_expression"],
            )
            if validation and not validation.valid:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Invalid target expression",
                        "errors": [e.model_dump() for e in validation.errors],
                    },
                )

        # Validate target_measure_id
        if updates.get("target_measure_id"):
            m = await db.get(Measure, updates["target_measure_id"])
            if not m or m.model_id != model_id:
                raise HTTPException(
                    status_code=400,
                    detail="target_measure_id does not belong to this model",
                )

        # Validate threshold bands in presentation_meta
        pm = updates.get("presentation_meta")
        if pm and isinstance(pm, dict) and "bands" in pm:
            band_errors = validate_bands(pm["bands"])
            if band_errors:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Invalid threshold band configuration",
                        "errors": band_errors,
                    },
                )

        eff_dir = updates.get("direction", kpi.direction)
        eff_pm = updates.get("presentation_meta")
        if eff_pm is None and kpi.presentation_meta:
            eff_pm = dict(kpi.presentation_meta)
        if eff_pm:
            coerce_bag = {"direction": eff_dir, "presentation_meta": eff_pm}
            _coerce_closer_absolute(coerce_bag)
            updates["presentation_meta"] = coerce_bag["presentation_meta"]

        # Certification guard
        is_admin = current_user.role in ("admin", "tenant_admin", "system_admin")
        if "certification_status" in updates:
            requested = updates["certification_status"]
            if not is_admin and requested in ("certified", "deprecated"):
                raise HTTPException(
                    status_code=403,
                    detail="Only admins can set certified or deprecated status",
                )

        changed_definition = any(k in _DEFINITION_FIELDS for k in updates)
        was_certified = kpi.certification_status in ("certified", "shared")

        # Capture old name before mutation for dependent cache invalidation
        old_kpi_name = kpi.name

        for k, v in updates.items():
            setattr(kpi, k, v)

        if changed_definition and was_certified:
            kpi.certification_status = "draft"

        if changed_definition:
            changed_keys = [k for k in updates if k in _DEFINITION_FIELDS]
            await _create_kpi_version(
                db, kpi, current_user.email,
                f"Updated: {', '.join(changed_keys)}",
            )

        await audit(
            db, action="kpi.update", severity="info",
            actor_email=current_user.email,
            target_type="kpi", target_id=kpi.id,
            target_name=kpi.display_name or kpi.name,
            detail={"fields": list(updates.keys())},
        )
        await db.commit()
        await db.refresh(kpi)

        # Invalidate cache for this KPI and any dependents
        _cache = get_kpi_cache()
        _cache.invalidate_kpi(kpi_id)
        # Invalidate any KPI that references this one by name (old or new).
        # Use the expression parser to reliably extract kpi() references
        # rather than brittle string matching.
        from shared.semantic.kpi_dependency import extract_kpi_references
        names_to_check = {kpi.name, old_kpi_name}
        all_kpis_result = await db.execute(
            select(KPI).where(KPI.model_id == model_id)
        )
        all_kpis = list(all_kpis_result.scalars().all())
        for other in all_kpis:
            if other.id == kpi_id or not other.expression:
                continue
            try:
                refs = extract_kpi_references(other.expression)
            except Exception:
                refs = set()
            if refs & names_to_check:
                _cache.invalidate_kpi(other.id)

        return KPIResponse.model_validate(kpi)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete(
    "/{kpi_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_kpi(
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        kpi_name = kpi.display_name or kpi.name
        await audit(
            db, action="kpi.delete", severity="warn",
            actor_email=current_user.email,
            target_type="kpi", target_id=kpi.id,
            target_name=kpi_name,
        )
        get_kpi_cache().invalidate_kpi(kpi_id)
        # Purge the soft-referencing translation/preference rows that have no
        # FK back to the KPI and would otherwise linger forever (F-029-15).
        await purge_entity_soft_references(db, model_id=model_id, entity_id=kpi_id)
        await db.delete(kpi)
        await db.commit()


# ---- Version History endpoints (Phase 4) ----

@router.get(
    "/{kpi_id}/versions",
    response_model=list[VersionResponse],
    dependencies=[require_role("viewer")],
)
async def list_kpi_versions(
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[VersionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        result = await db.execute(
            select(KPIVersion)
            .where(KPIVersion.kpi_id == kpi_id)
            .order_by(KPIVersion.version_number.desc())
        )
        return [VersionResponse.model_validate(v, from_attributes=True) for v in result.scalars().all()]
    return []


@router.post(
    "/{kpi_id}/versions/{version_number}/revert",
    response_model=KPIResponse,
    dependencies=[require_role("modeler")],
)
async def revert_kpi_version(
    model_id: UUID,
    kpi_id: UUID,
    version_number: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        result = await db.execute(
            select(KPIVersion)
            .where(KPIVersion.kpi_id == kpi_id)
            .where(KPIVersion.version_number == version_number)
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise HTTPException(
                status_code=404,
                detail=f"Version {version_number} not found",
            )

        snap = version.snapshot

        # Restore scalar fields from snapshot
        _SCALAR_SNAP_FIELDS = (
            "name", "display_name", "description", "display_folder",
            "kpi_type", "expression", "calc_agg_mode",
            "inner_agg", "inner_grain", "outer_agg",
            "at_grain", "non_additive_agg", "carry_forward",
            "target_type", "target_expression", "target_period",
            "direction", "presentation_type", "presentation_meta",
            "trend_period", "trend_threshold", "trend_sparkline_periods",
            "format_token", "format_custom", "unit_label", "null_display_value",
            "weight", "indicator_type", "certification_status",
            "snapshot_frequency", "snapshot_retention",
            "status_graphic", "trend_graphic",
        )
        for k in _SCALAR_SNAP_FIELDS:
            if k in snap:
                setattr(kpi, k, snap[k])

        # Restore numeric fields that need type coercion
        if "target_value" in snap:
            kpi.target_value = float(snap["target_value"]) if snap["target_value"] is not None else None

        # Restore UUID foreign keys
        _UUID_SNAP_FIELDS = (
            "parent_kpi_id", "target_measure_id", "time_dimension_id",
        )
        for fk_field in _UUID_SNAP_FIELDS:
            if fk_field not in snap:
                continue
            val = snap[fk_field]
            if val:
                ref_id = UUID(val)
                # Validate that measure/kpi refs still exist in this model
                if fk_field == "target_measure_id":
                    ref = await db.get(Measure, ref_id)
                    if ref is None or ref.model_id != model_id:
                        setattr(kpi, fk_field, None)
                        continue
                elif fk_field == "parent_kpi_id":
                    parent = await db.get(KPI, ref_id)
                    if parent is None or parent.model_id != model_id or await _would_cycle(db, ref_id, kpi_id):
                        setattr(kpi, fk_field, None)
                        continue
                setattr(kpi, fk_field, ref_id)
            else:
                setattr(kpi, fk_field, None)

        await _create_kpi_version(
            db, kpi, current_user.email,
            f"Reverted to version {version_number}",
        )
        await db.commit()
        await db.refresh(kpi)
        return KPIResponse.model_validate(kpi)
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Certification endpoints (Phase 4) ----

@router.post(
    "/{kpi_id}/certify",
    response_model=KPIResponse,
    dependencies=[require_role("admin")],
)
async def certify_kpi(
    model_id: UUID,
    kpi_id: UUID,
    _body: CertifyRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        # Ownership gate: only owner or admin can certify
        is_admin = current_user.role in ("admin", "tenant_admin", "system_admin")
        if kpi.owner_user_id and kpi.owner_user_id != current_user.email and not is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only the KPI owner or an admin can certify",
            )

        if kpi.certification_status == "certified":
            return KPIResponse.model_validate(kpi)
        kpi.certification_status = "certified"
        await _create_kpi_version(db, kpi, current_user.email, "Certified")
        await audit(
            db, action="kpi.certify", severity="info",
            actor_email=current_user.email,
            target_type="kpi", target_id=kpi.id,
            target_name=kpi.display_name or kpi.name,
            detail={"certifier": current_user.email},
        )
        await db.commit()
        await db.refresh(kpi)
        return KPIResponse.model_validate(kpi)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/{kpi_id}/deprecate",
    response_model=KPIResponse,
    dependencies=[require_role("admin")],
)
async def deprecate_kpi(
    model_id: UUID,
    kpi_id: UUID,
    body: DeprecateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        # Ownership gate: only owner or admin can deprecate
        is_admin = current_user.role in ("admin", "tenant_admin", "system_admin")
        if kpi.owner_user_id and kpi.owner_user_id != current_user.email and not is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only the KPI owner or an admin can deprecate",
            )

        # Bug-5259: validate replacement KPI exists and belongs to same model
        if body.replacement_id:
            replacement = await db.get(KPI, body.replacement_id)
            if replacement is None or replacement.model_id != model_id:
                raise HTTPException(
                    status_code=422,
                    detail="Replacement KPI not found in this model",
                )
            if replacement.id == kpi.id:
                raise HTTPException(
                    status_code=422,
                    detail="A KPI cannot replace itself",
                )

        kpi.certification_status = "deprecated"
        kpi.replacement_id = body.replacement_id
        summary = "Deprecated"
        if body.replacement_id:
            summary += f" (replacement: {body.replacement_id})"
        await _create_kpi_version(db, kpi, current_user.email, summary)
        await audit(
            db, action="kpi.deprecate", severity="info",
            actor_email=current_user.email,
            target_type="kpi", target_id=kpi.id,
            target_name=kpi.display_name or kpi.name,
            detail={"replacement_id": str(body.replacement_id) if body.replacement_id else None},
        )
        await db.commit()
        await db.refresh(kpi)
        return KPIResponse.model_validate(kpi)
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Usage Tracking endpoint (Phase 5) ----

@router.post(
    "/{kpi_id}/usage",
    response_model=EntityUsageResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("viewer")],
)
async def report_kpi_usage(
    model_id: UUID,
    kpi_id: UUID,
    body: EntityUsageCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> EntityUsageResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        usage = KPIUsage(
            kpi_id=kpi_id,
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
    "/{kpi_id}/usage",
    response_model=list[EntityUsageResponse],
    dependencies=[require_role("viewer")],
)
async def list_kpi_usage(
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[EntityUsageResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        result = await db.execute(
            select(KPIUsage)
            .where(KPIUsage.kpi_id == kpi_id)
            .order_by(KPIUsage.reported_at.desc())
        )
        return [EntityUsageResponse.model_validate(u, from_attributes=True) for u in result.scalars().all()]
    return []


# ---- Query-router bridge ----

async def _execute_via_router(
    model_id: UUID,
    query: str,
    bearer: str,
    timeout_s: float = 30.0,
    persona_id: str | None = None,
) -> dict:
    """POST a SQL query to the query-router's /execute endpoint."""
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/execute"
    headers = {"Authorization": f"Bearer {bearer}"}
    body: dict = {
        "model_id": str(model_id),
        "raw_query": query,
        "protocol": "jdbc",
    }
    if persona_id is not None:
        body["persona_id"] = persona_id
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


from shared.connector_qualify import safe_ident as _safe_ident


# ---------------------------------------------------------------------------
# Time-intelligence decomposition — multi-query evaluation
# ---------------------------------------------------------------------------
# The query-router cannot resolve semantic measure names inside CTEs or
# window-function queries (they go through passthrough with table-name
# substitution only). TI evaluation is therefore decomposed into simple
# queries the router can bind, with aggregation done in Python.

_TI_INTERVAL_MAP = {
    "day": "1 day",
    "week": "7 days",
    "month": "1 month",
    "quarter": "3 months",
    "year": "1 year",
}

# Every TI type the decomposed evaluator below implements. Covers both the
# business-builder vocabulary and the wizard/formula DSL functions mapped
# through derive_ti_decomposition (F-017-01).
_TI_CTE_TYPES = {
    "moving_avg", "trailing_sum", "prior_period", "growth_pct", "cagr",
    "period_to_date", "fiscal_period_to_date", "lag", "lead",
}


def _fiscal_period_start(grain: str, fiscal_year_start_month: int) -> "date | None":
    """Concrete start date of the current fiscal year/quarter (Python-side).

    Standard-grain PTD uses DATE_TRUNC on the database; fiscal boundaries
    depend on fiscal_year_start_month, so they are computed here and inlined
    as a DATE literal.
    """
    from datetime import date as _date

    from shared.semantic.calendar_utils import period_to_date_range

    if grain not in ("year", "quarter"):
        return None
    today = _date.today()
    fy_start = period_to_date_range(
        today, "year", fiscal_year_start_month=fiscal_year_start_month,
    ).start
    if grain == "year":
        return fy_start
    # Fiscal quarters are anchored to the fiscal year start month.
    months_since = (today.year - fy_start.year) * 12 + (today.month - fy_start.month)
    total = fy_start.month - 1 + (months_since // 3) * 3
    return _date(fy_start.year + total // 12, total % 12 + 1, 1)


_ADHOC_TIMEOUT_S: float = 10.0


async def _evaluate_ti_decomposed(
    base_expression: str,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    measure_map: dict,
    *,
    ti_type: str,
    ti_grain: str | None = None,
    ti_n_periods: int | None = None,
    time_column: str | None = None,
    time_window_start_sql: str | None = None,
    time_window_end_sql: str | None = None,
    filter_where_clause: str | None = None,
    persona_id: str | None = None,
    calc_agg_mode: str = "automatic",
    fiscal_year_start_month: int | None = None,
    sql_sink: list[str] | None = None,
    timeout_s: float = 30.0,
) -> float | None:
    """Evaluate a time-intelligence KPI via decomposed simple queries.

    Returns the scalar float result or None if insufficient data.
    Raises ValueError on query execution failure.
    """
    from src.kpi_compiler import CompilerContext, compile_expression, interval_literal

    grain = ti_grain or "month"
    interval = _TI_INTERVAL_MAP.get(grain, "1 month")
    n = ti_n_periods or 3
    time_col = _safe_ident(time_column or "date")
    start_sql = time_window_start_sql or f"DATE_TRUNC('{grain}', CURRENT_DATE)"
    end_sql = time_window_end_sql or "CURRENT_DATE"

    measure_aggs = {
        name: (m.default_agg or "sum")
        for name, m in measure_map.items()
    }

    def _compile_base(expr: str) -> str:
        """Compile a base expression to a simple SELECT (no TI wrapping)."""
        ctx = CompilerContext(
            model_slug=model_slug,
            calc_agg_mode=calc_agg_mode,
            default_agg="sum",
            measure_aggs=measure_aggs,
        )
        compiled = compile_expression(expr, ctx)
        return compiled.select_expr

    select_expr = _compile_base(base_expression)

    async def _exec_scalar(sql: str) -> float | None:
        if sql_sink is not None:
            sql_sink.append(sql)
        result = await _execute_via_router(model_id, sql, bearer, timeout_s=timeout_s, persona_id=persona_id)
        rows = result.get("rows", [])
        if rows:
            row = rows[0]
            if isinstance(row, dict):
                for v in row.values():
                    if v is None:
                        return None
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(row, (list, tuple)) and row:
                return float(row[0]) if row[0] is not None else None
        return None

    async def _exec_period_values(n_periods: int) -> list[float]:
        """Query each period individually via simple bounded aggregates."""
        values = []
        for i in range(n_periods):
            offset_start = f"{end_sql} - INTERVAL '{interval_literal(i + 1, grain)}'"
            offset_end = f"{end_sql} - INTERVAL '{interval_literal(i, grain)}'"
            val = await _exec_period_scalar(offset_start, offset_end)
            if val is not None:
                values.append(val)
        return values

    async def _exec_period_scalar(start: str, end: str) -> float | None:
        """Execute a simple aggregation over a specific time window."""
        model = _safe_ident(model_slug)
        where_parts = [f"{time_col} >= {start}", f"{time_col} < {end}"]
        if filter_where_clause:
            where_parts.append(filter_where_clause)
        where = " AND ".join(where_parts)
        sql = f"SELECT {select_expr} AS value FROM {model} WHERE {where}"
        return await _exec_scalar(sql)

    if ti_type == "moving_avg":
        values = await _exec_period_values(n)
        if not values:
            return None
        return sum(values) / len(values)

    if ti_type == "trailing_sum":
        values = await _exec_period_values(n)
        if not values:
            return None
        return sum(values)

    if ti_type == "prior_period":
        prior_start = f"{start_sql} - INTERVAL '{interval}'"
        prior_end = f"{end_sql} - INTERVAL '{interval}'"
        return await _exec_period_scalar(prior_start, prior_end)

    if ti_type == "growth_pct":
        current_val = await _exec_period_scalar(start_sql, end_sql)
        prior_start = f"{start_sql} - INTERVAL '{interval}'"
        prior_end = f"{end_sql} - INTERVAL '{interval}'"
        prior_val = await _exec_period_scalar(prior_start, prior_end)
        if current_val is None or prior_val is None or prior_val == 0:
            return None
        return (current_val - prior_val) / abs(prior_val)

    if ti_type == "cagr":
        n_cagr = ti_n_periods or 1
        current_val = await _exec_period_scalar(start_sql, end_sql)
        prior_start = f"{start_sql} - INTERVAL '{n_cagr} year'"
        prior_end = f"{end_sql} - INTERVAL '{n_cagr} year'"
        prior_val = await _exec_period_scalar(prior_start, prior_end)
        if current_val is None or prior_val is None or prior_val <= 0:
            return None
        return (current_val / prior_val) ** (1.0 / n_cagr) - 1

    if ti_type in ("period_to_date", "fiscal_period_to_date"):
        ptd_start = f"DATE_TRUNC('{grain}', CURRENT_DATE)"
        if (
            ti_type == "fiscal_period_to_date"
            and fiscal_year_start_month
            and fiscal_year_start_month != 1
        ):
            fiscal_start = _fiscal_period_start(grain, fiscal_year_start_month)
            if fiscal_start is not None:
                ptd_start = f"DATE '{fiscal_start.isoformat()}'"
        return await _exec_period_scalar(
            ptd_start, "CURRENT_DATE + INTERVAL '1 day'",
        )

    if ti_type in ("lag", "lead"):
        shift = ti_n_periods or 1
        op = "-" if ti_type == "lag" else "+"
        iv = interval_literal(shift, grain)
        shifted_start = f"{start_sql} {op} INTERVAL '{iv}'"
        shifted_end = f"{end_sql} {op} INTERVAL '{iv}'"
        return await _exec_period_scalar(shifted_start, shifted_end)

    return None


_AGG_FUNCS = {"sum", "count", "avg", "min", "max", "count_distinct"}


async def _get_measure_value(
    model_id: UUID, measure_name: str, bearer: str, model_slug: str = "Model",
    default_agg: str = "sum", persona_id: str | None = None,
    where_clause: str | None = None,
) -> float | None:
    """Execute a semantic measure query via SQL and return the scalar value.

    *where_clause* applies the KPI's business-definition filter/time-window
    scope so a measure-based target is resolved under the SAME slice as the
    value (F-017-14): "EMEA revenue" must compare against "EMEA target", not
    the unfiltered grand total.
    """
    agg = default_agg.lower() if default_agg.lower() in _AGG_FUNCS else "sum"
    if agg == "count_distinct":
        measure_expr = f"COUNT(DISTINCT {_safe_ident(measure_name)})"
    else:
        measure_expr = f"{agg.upper()}({_safe_ident(measure_name)})"
    sql = f"SELECT {measure_expr} FROM {_safe_ident(model_slug)}"
    if where_clause:
        sql += f" WHERE {where_clause}"
    try:
        result = await _execute_via_router(model_id, sql, bearer, persona_id=persona_id)
        rows = result.get("rows", [])
        if rows:
            row = rows[0]
            if isinstance(row, dict):
                for v in row.values():
                    if v is None:
                        continue
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(row, (list, tuple)) and row:
                return float(row[0]) if row[0] is not None else None
        cells = result.get("cells", [])
        if cells:
            cell = cells[0]
            if isinstance(cell, dict):
                return float(cell.get("value", 0))
            return float(cell) if cell is not None else None
    except Exception as exc:
        log.warning("Measure query for %s failed: %s", measure_name, exc)
    return None


# ---- Evaluation helpers ----


async def _batch_get_measure_values(
    model_id: UUID,
    measure_names: list[str],
    bearer: str,
    model_slug: str,
    measure_map: dict[str, "Measure"],
    persona_id: str | None = None,
) -> dict[str, float | None]:
    """Fetch multiple measure values in a single query-router call.

    Builds a SELECT with one aggregate column per measure:
      SELECT SUM("Revenue") AS m0, AVG("Cost") AS m1 FROM "Model"

    Falls back to individual calls if the batch query fails.
    """
    from shared.connector_qualify import safe_ident as _si

    if not measure_names:
        return {}

    # Build multi-column aggregate query
    selects: list[str] = []
    aliases: list[str] = []
    for i, name in enumerate(measure_names):
        m = measure_map.get(name)
        agg = (m.default_agg or "sum").lower() if m else "sum"
        agg = agg if agg in _AGG_FUNCS else "sum"
        alias = f"m{i}"
        if agg == "count_distinct":
            selects.append(f"COUNT(DISTINCT {_si(name)}) AS {alias}")
        else:
            selects.append(f"{agg.upper()}({_si(name)}) AS {alias}")
        aliases.append(alias)

    sql = f"SELECT {', '.join(selects)} FROM {_si(model_slug)}"
    result_map: dict[str, float | None] = {n: None for n in measure_names}

    try:
        result = await _execute_via_router(model_id, sql, bearer, persona_id=persona_id)
        rows = result.get("rows", [])
        if rows:
            row = rows[0]
            if isinstance(row, dict):
                for i, alias in enumerate(aliases):
                    v = row.get(alias)
                    if v is not None:
                        try:
                            result_map[measure_names[i]] = float(v)
                        except (TypeError, ValueError):
                            pass
            elif isinstance(row, (list, tuple)):
                for i, v in enumerate(row):
                    if i < len(measure_names) and v is not None:
                        try:
                            result_map[measure_names[i]] = float(v)
                        except (TypeError, ValueError):
                            pass
    except Exception as exc:
        log.warning("Batch measure query failed, falling back to individual: %s", exc)
        # Fall back to individual queries
        for name in measure_names:
            m = measure_map.get(name)
            agg = (m.default_agg or "sum") if m else "sum"
            result_map[name] = await _get_measure_value(model_id, name, bearer, model_slug, agg, persona_id=persona_id)

    return result_map


# Sentinel value indicating the SQL compiler cannot handle this expression
_COMPILER_UNSUPPORTED = object()
# Sentinel indicating the SQL executed but failed (router/SQL error, not "no data")
_EVALUATION_ERROR = object()
# Sentinel indicating a time-intelligence expression with no time dimension
# bound — the KPI needs a time dimension before it can evaluate.
_TI_NO_TIME_DIMENSION = object()

_TI_NO_TIME_DIMENSION_LABEL = (
    "Evaluation failed — this time-intelligence KPI needs a time dimension"
)


class _TIDecompositionFailed:
    """Sentinel-with-detail: a derived (wizard/formula) time-intelligence
    decomposition failed at evaluation time.

    The legacy fall-through would re-compile the expression into
    window-over-aggregate SQL the query-router is known to reject, wasting a
    router round-trip and masking the real cause behind a generic error.
    Derived-TI failures therefore fail loud here with the precise cause.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail

    @property
    def label(self) -> str:
        return f"Time-intelligence evaluation failed — {self.detail}"


# Maximum levels of composite-of-composite nesting evaluated recursively.
# Deeper hierarchies fail loud instead of returning silent placeholder values.
_MAX_COMPOSITE_DEPTH = 5

_COMPOSITE_CYCLE_LABEL = (
    "Composite evaluation failed — circular composite reference"
)
_COMPOSITE_DEPTH_LABEL = (
    "Composite evaluation failed — composite nesting deeper than "
    f"{_MAX_COMPOSITE_DEPTH} levels"
)


class CompositeEvaluationError(Exception):
    """Raised when a composite KPI cannot be scored (cycle or depth limit).

    Composite evaluation must never silently fall back to the placeholder
    expression value; callers convert this into an explicit error response.
    """


# Bug-4255: prefixes of the status labels a KPI evaluation emits when it
# genuinely FAILED (as opposed to legitimately having no data). A child whose
# response carries one of these as its label is an *errored* composite child,
# surfaced as a degraded/error signal on the parent. A no-data child (label
# "No Data" / "Unclassified" / "No expression configured" / a normal threshold
# band) is silently excluded, unchanged. Matching on the canonical
# fail-loud families keeps the discriminator stable as new error variants
# reuse these prefixes.
_CHILD_ERROR_LABEL_PREFIXES = (
    "Evaluation failed",
    "Composite evaluation failed",
    "Time-intelligence evaluation failed",
)


def _child_error_reason(response: "KPIEvaluateResponse") -> str | None:
    """Return the failure reason if a child evaluation ERRORED, else None.

    A child errored when it produced no value AND its status label is one of
    the canonical fail-loud families. A no-data child (null value with a
    benign label, or any successfully-evaluated value) returns None so the
    parent treats it as a silent no-data exclusion (Bug-4255).
    """
    if response is None:
        return None
    if response.value is not None:
        return None
    label = response.status_label
    if not label:
        return None
    if label.startswith(_CHILD_ERROR_LABEL_PREFIXES):
        return label
    return None


def _apply_composite_signal(
    resp: "KPIEvaluateResponse", result: "CompositeResult | None",
) -> "KPIEvaluateResponse":
    """Stamp the Bug-4255 composite health signal onto a finalized response.

    ``composite_status`` is always set for a composite (even "ok"), so a
    consumer can tell a composite from a non-composite and react to a
    ``degraded`` / ``error`` state. ``errored_children`` lists the failed
    children with their reasons; an empty list means no child errored. When
    every child errored the parent label is overridden so the card shows the
    failure rather than a benign "No Data".
    """
    if result is None:
        return resp
    resp.composite_status = result.status
    resp.errored_children = [
        {
            "kpi_id": ec.kpi_id,
            "kpi_name": ec.kpi_name,
            "error_reason": ec.error_reason,
        }
        for ec in result.errored_children
    ]
    if result.status == COMPOSITE_STATUS_ERROR:
        resp.status_label = (
            "Evaluation failed — every child KPI errored"
        )
    return resp


# Persistence guard for kpi_latest.status_label / kpi_snapshots.status_label.
# Matches the String(255) column width (migration 0127). Fail-loud labels can
# embed arbitrary router detail; the DB write must never be the thing that 500s
# a batch, so labels are truncated to this width before persisting. The explicit
# error label is still returned to the caller in full — only the materialised
# $KPIs copy is bounded.
_STATUS_LABEL_MAX_LEN = 255


def _truncate_status_label(label: str | None) -> str | None:
    """Bound a status label to the persistence column width."""
    if label is None:
        return None
    if len(label) <= _STATUS_LABEL_MAX_LEN:
        return label
    # Reserve one char for an ellipsis marker so truncation is visible.
    return label[: _STATUS_LABEL_MAX_LEN - 1] + "…"


async def _evaluate_expression_via_sql(
    expression: str,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    measure_map: dict[str, Measure],
    ctx: EvaluationContext,
    time_column: str | None = None,
    calendar_type: str | None = None,
    fiscal_year_start_month: int | None = None,
    *,
    at_grain: str | None = None,
    non_additive_agg: str | None = None,
    carry_forward: bool = False,
    inner_agg: str | None = None,
    inner_grain: str | None = None,
    outer_agg: str | None = None,
    persona_id: str | None = None,
    where_clause: str | None = None,
    filter_where_clause: str | None = None,
    time_where_clause: str | None = None,
    enable_ti_subquery: bool = False,
    ti_type: str | None = None,
    ti_grain: str | None = None,
    ti_n_periods: int | None = None,
    base_expression: str | None = None,
    time_window_start_sql: str | None = None,
    time_window_end_sql: str | None = None,
    share_type: str | None = None,
    share_dimension: str | None = None,
    share_n: int | None = None,
    filter_predicate_list: list[str] | None = None,
    sql_sink: list[str] | None = None,
    timeout_s: float = 30.0,
) -> float | object | None:
    """Compile a KPI expression to SQL and execute via the query-router.

    When *sql_sink* is provided, every SQL statement sent to the query gateway
    is appended to it (in execution order) so callers can surface the exact
    SQL — used by the builder's "Show SQL" preview.

    Returns the scalar float result, None if no data, or _COMPILER_UNSUPPORTED
    if the expression contains features that can't be compiled to SQL
    (KPI cross-references inside time functions).
    """
    # Wizard/formula path (no business definition): derive the same TI
    # decomposition metadata business-builder KPIs carry, so both paths
    # share the decomposed evaluation machine (F-017-01). The legacy
    # window-over-aggregate SQL emission cannot be bound by the router.
    ti_type_derived = False
    if not ti_type:
        derived_ti = derive_ti_decomposition(expression)
        if derived_ti is not None:
            if not time_column:
                return _TI_NO_TIME_DIMENSION
            ti_type = derived_ti.ti_type
            ti_grain = derived_ti.ti_grain
            ti_n_periods = derived_ti.ti_n_periods
            base_expression = derived_ti.base_expression
            ti_type_derived = True
        elif expression_has_time_intelligence(expression):
            # Nested TI (e.g. pct_change(...) * 100) — the SQL compiler
            # would emit window-over-aggregate SQL the router cannot bind.
            # Route to the Python pipeline, whose provider TI hook resolves
            # each TI subtree via decomposed queries.
            return _COMPILER_UNSUPPORTED

    # Decomposed TI evaluation: the query-router cannot resolve semantic
    # measure names inside CTEs/window-functions.  For TI types that would
    # generate complex SQL, decompose into simple router-friendly queries.
    if ti_type and ti_type in _TI_CTE_TYPES and base_expression:
        try:
            result = await _evaluate_ti_decomposed(
                base_expression, model_id, model_slug, bearer, measure_map,
                ti_type=ti_type,
                ti_grain=ti_grain,
                ti_n_periods=ti_n_periods,
                time_column=time_column,
                time_window_start_sql=time_window_start_sql,
                time_window_end_sql=time_window_end_sql,
                filter_where_clause=filter_where_clause,
                persona_id=persona_id,
                calc_agg_mode=ctx.calc_agg_mode,
                fiscal_year_start_month=fiscal_year_start_month,
                sql_sink=sql_sink,
                timeout_s=timeout_s,
            )
            return result
        except Exception as exc:
            if ti_type_derived:
                # Derived (wizard/formula) TI: the fall-through below would
                # emit window-over-aggregate SQL the router is known to
                # reject — a wasted round-trip ending in a generic error
                # that masks the real cause. Fail loud with the cause.
                log.error(
                    "Derived TI decomposed evaluation failed (%s %s n=%s): %s",
                    ti_type, ti_grain, ti_n_periods, exc,
                )
                return _TIDecompositionFailed(str(exc))
            log.warning(
                "TI decomposed evaluation failed, falling through: %s", exc,
            )

    try:
        measure_aggs = {
            name: (m.default_agg or "sum")
            for name, m in measure_map.items()
        }
        compiler_ctx = CompilerContext(
            model_slug=model_slug,
            calc_agg_mode=ctx.calc_agg_mode,
            default_agg="sum",
            measure_aggs=measure_aggs,
            time_column=time_column,
            calendar_type=calendar_type,
            fiscal_year_start_month=fiscal_year_start_month,
            at_grain=at_grain,
            non_additive_agg=non_additive_agg,
            carry_forward=carry_forward,
            inner_agg=inner_agg,
            inner_grain=inner_grain,
            outer_agg=outer_agg,
            where_clause=where_clause,
            filter_where_clause=filter_where_clause,
            time_where_clause=time_where_clause,
            enable_ti_subquery=enable_ti_subquery,
            ti_type=ti_type,
            ti_grain=ti_grain,
            ti_n_periods=ti_n_periods,
            base_expression=base_expression,
            time_window_start_sql=time_window_start_sql,
            time_window_end_sql=time_window_end_sql,
            share_type=share_type,
            share_dimension=share_dimension,
            share_n=share_n,
            filter_predicate_list=filter_predicate_list,
        )
        compiled = compile_expression(expression, compiler_ctx)
    except Exception:
        return _COMPILER_UNSUPPORTED

    # KPI cross-refs can't be compiled to SQL — fall back to Python evaluator
    if compiled.kpi_names:
        return _COMPILER_UNSUPPORTED

    # F-017-22: share_of_total()/rank_over() in a plain expression (no grouped
    # share CTE) degenerate to share 1 / rank 1 over a single ungrouped row.
    # Fail closed rather than serve a silently-wrong number — these belong in
    # the builder's share_rank family (grouped CTE path).
    if compiled.has_ungrouped_window:
        log.warning(
            "share_of_total/rank_over used outside a grouped share context; "
            "failing closed (would yield degenerate single-row window).",
        )
        return _EVALUATION_ERROR

    sql = compiled.sql
    if sql_sink is not None:
        sql_sink.append(sql)

    try:
        result = await _execute_via_router(model_id, sql, bearer, timeout_s=timeout_s, persona_id=persona_id)
        rows = result.get("rows", [])
        if rows:
            row = rows[0]
            if isinstance(row, dict):
                for v in row.values():
                    if v is None:
                        return None
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(row, (list, tuple)) and row:
                return float(row[0]) if row[0] is not None else None
        cells = result.get("cells", [])
        if cells:
            cell = cells[0]
            if isinstance(cell, dict):
                return float(cell.get("value", 0))
            return float(cell) if cell is not None else None
    except Exception as exc:
        log.warning(
            "KPI SQL evaluation failed — expression: %.200s | SQL: %.400s | error: %s",
            expression, sql, exc,
        )
        return _EVALUATION_ERROR
    return None


async def _build_evaluation_context(
    kpi: KPI,
    db,
    model_id: UUID,
) -> EvaluationContext:
    """Build an EvaluationContext from a KPI ORM object."""
    # Resolve target value -- static targets use the stored value;
    # measure-based targets are resolved live in the evaluate endpoint
    # via _get_measure_value, so we leave target_val as None here.
    target_val: float | None = None
    if kpi.target_type == "static":
        target_val = float(kpi.target_value) if kpi.target_value is not None else None

    return EvaluationContext(
        kpi_id=kpi.id,
        kpi_name=kpi.name,
        expression=kpi.expression,
        kpi_type=kpi.kpi_type,
        target_type=kpi.target_type,
        target_value=target_val,
        target_measure_id=kpi.target_measure_id,
        target_expression=kpi.target_expression,
        direction=kpi.direction,
        presentation_type=kpi.presentation_type,
        presentation_meta=kpi.presentation_meta,
        calc_agg_mode=kpi.calc_agg_mode,
        trend_period=kpi.trend_period,
        trend_threshold=float(kpi.trend_threshold),
        format_token=kpi.format_token,
        format_custom=kpi.format_custom,
        unit_label=kpi.unit_label,
        null_display_value=kpi.null_display_value,
    )


def _derive_calendar_type(model) -> str:
    """Derive effective calendar type from model fiscal settings.

    The Model ORM does not have an explicit ``calendar_type`` field;
    it is inferred: if ``fiscal_year_start_month`` is set and is not
    January (1), the model uses a fiscal calendar. Otherwise standard.
    """
    fym = getattr(model, "fiscal_year_start_month", None)
    if fym is not None and fym != 1:
        return "fiscal"
    return "standard"


async def _resolve_time_column(
    db, kpi: KPI, model_id: UUID,
) -> str | None:
    """Resolve the time column name from the KPI's time_dimension_id."""
    if kpi.time_dimension_id:
        dim = await db.get(Dimension, kpi.time_dimension_id)
        if dim and dim.model_id == model_id and dim.is_time_dim:
            return dim.name
    return None


def _build_ti_evaluator(
    model_id: UUID,
    model_slug: str,
    bearer: str,
    measure_map: dict[str, Measure],
    *,
    time_column: str | None,
    calc_agg_mode: str = "automatic",
    fiscal_year_start_month: int | None = None,
    filter_where_clause: str | None = None,
    time_window_start_sql: str | None = None,
    time_window_end_sql: str | None = None,
    persona_id: str | None = None,
    sql_sink: list[str] | None = None,
    timeout_s: float = 30.0,
):
    """Build the provider hook that resolves a time-intelligence AST subtree
    via decomposed query-router queries (Python pipeline path for nested TI)."""

    async def evaluate_time_intelligence(node) -> float | None:
        decomp = derive_ti_decomposition_from_node(node)
        if decomp is None or not time_column:
            log.warning(
                "Time-intelligence subtree cannot be decomposed "
                "(node=%s, time_column=%s) — returning None.",
                getattr(node, "name", type(node).__name__), time_column,
            )
            return None
        try:
            return await _evaluate_ti_decomposed(
                decomp.base_expression, model_id, model_slug, bearer, measure_map,
                ti_type=decomp.ti_type,
                ti_grain=decomp.ti_grain,
                ti_n_periods=decomp.ti_n_periods,
                time_column=time_column,
                time_window_start_sql=time_window_start_sql,
                time_window_end_sql=time_window_end_sql,
                filter_where_clause=filter_where_clause,
                persona_id=persona_id,
                calc_agg_mode=calc_agg_mode,
                fiscal_year_start_month=fiscal_year_start_month,
                sql_sink=sql_sink,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            log.warning(
                "Decomposed TI evaluation of nested subtree failed: %s", exc,
            )
            return None

    return evaluate_time_intelligence


def _build_measure_provider(
    model_id: UUID,
    model_slug: str,
    bearer: str,
    measure_map: dict[str, Measure],
    kpi_value_cache: dict[str, float | None] | None = None,
    measure_value_cache: dict[str, float | None] | None = None,
    persona_id: str | None = None,
    ti_evaluator=None,
) -> MeasureValueProvider:
    """Build a MeasureValueProvider that resolves measures via the query-router.

    If *measure_value_cache* is provided, values are served from it (batch
    pre-fetched) instead of issuing individual query-router calls.
    *ti_evaluator* (from _build_ti_evaluator) resolves time-intelligence
    subtrees in the Python pipeline via decomposed router queries.
    """

    async def get_measure_value(name: str) -> float | None:
        if measure_value_cache is not None and name in measure_value_cache:
            return measure_value_cache[name]
        m = measure_map.get(name)
        agg = m.default_agg if m else "sum"
        return await _get_measure_value(
            model_id, name, bearer, model_slug, agg, persona_id=persona_id,
        )

    async def get_kpi_value(name: str) -> float | None:
        if kpi_value_cache is None:
            return None
        return kpi_value_cache.get(name)

    return MeasureValueProvider(
        get_measure_value=get_measure_value,
        get_kpi_value=get_kpi_value if kpi_value_cache is not None else None,
        evaluate_time_intelligence=ti_evaluator,
    )


def _result_to_response(result) -> KPIEvaluateResponse:
    """Convert an EvaluationResult to a Pydantic response."""
    return KPIEvaluateResponse(
        kpi_id=result.kpi_id,
        value=result.value,
        value_str=result.value_str,
        target=result.target,
        status=result.status,
        status_label=result.status_label,
        status_color=result.status_color,
        trend=result.trend,
        trend_label=result.trend_label,
        trend_pct=result.trend_pct,
        formatted_value=result.formatted_value,
        formatted_target=result.formatted_target,
        formatted_variance=result.formatted_variance,
        evaluation_ms=result.evaluation_ms,
        goal=result.goal,
        formatted_goal=result.formatted_goal,
    )


def _serialize_bands(bands) -> list[dict] | None:
    """Serialize the threshold ``Band`` objects the status was matched against
    into plain dicts for the API response (Bug-1226).

    The frontend gauge plots its needle from ``status_position`` against these
    bands. Returning the exact bands the backend used — custom or resolved
    default preset — guarantees the needle, band colour and badge share one
    scale.
    """
    if not bands:
        return None
    return [
        {"label": b.label, "color": b.color, "min": b.min, "max": b.max}
        for b in bands
    ]


async def _resolve_peer_values(
    kpi: KPI,
    meta: dict,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    persona_id: str | None,
    measure_map: dict[str, "Measure"] | None = None,
) -> list[float] | None:
    """Resolve the peer-group values for a percentile_rank KPI (F-017-03).

    Groups the KPI's single measure by ``presentation_meta.peer_dimension`` via
    one query-router query and returns the per-group scalar values. The KPI's
    own value is ranked against these peers by ``_compute_percentile_rank``.

    Returns None (→ "No Data") when the peer dimension is unset or the KPI is
    not a single-measure expression — peer ranking of an arbitrary DSL
    expression is out of scope. All execution flows through the query-router
    gateway (no source-DB bypass).
    """
    peer_dim = meta.get("peer_dimension")
    if not peer_dim or not kpi.expression:
        return None
    # Only single-measure KPIs are peer-rankable: the grouped query must reduce
    # to one aggregate column per peer-dimension member.
    measures = extract_measure_names(kpi.expression)
    if len(measures) != 1:
        return None
    measure_name = measures[0]
    from shared.connector_qualify import safe_ident as _si

    # Bug-1225: rank the KPI's value against peers computed with the SAME
    # aggregate as the measure itself. Hardcoding SUM ranked an AVG /
    # COUNT_DISTINCT KPI (e.g. average order value, distinct customers) against
    # summed peers — a different quantity — yielding a business-wrong rank.
    # Mirror _batch_get_measure_values: read default_agg from measure_map.
    m = (measure_map or {}).get(measure_name)
    agg = (m.default_agg or "sum").lower() if m else "sum"
    agg = agg if agg in _AGG_FUNCS else "sum"
    dim_col = _si(peer_dim)
    if agg == "count_distinct":
        measure_expr = f"COUNT(DISTINCT {_si(measure_name)})"
    else:
        measure_expr = f"{agg.upper()}({_si(measure_name)})"
    sql = (
        f"SELECT {dim_col}, {measure_expr} "
        f"FROM {_si(model_slug)} GROUP BY {dim_col}"
    )
    try:
        result = await _execute_via_router(
            model_id, sql, bearer, persona_id=persona_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Peer-value query for KPI %s failed: %s", kpi.id, exc)
        return None
    rows = result.get("rows", []) or []
    values: list[float] = []
    for row in rows:
        cells = list(row.values()) if isinstance(row, dict) else list(row)
        # The aggregate is the last column; the dimension member is first.
        if not cells:
            continue
        raw = cells[-1]
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return values or None


# Period-grain keywords accepted for the ad-hoc preview sparkline (F-017-26).
# These are the canonical PostgreSQL DATE_TRUNC field names; the single
# downstream sqlglot transpile point renders the right dialect per connector.
_ADHOC_TREND_GRAINS = {"day", "week", "month", "quarter", "year"}


async def _build_adhoc_trend_series(
    expression: str,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    measure_map: dict[str, "Measure"],
    ctx,
    *,
    time_column: str | None,
    trend_period: str | None,
    filter_where_clause: str | None,
    n_periods: int,
    persona_id: str | None = None,
    sql_sink: list[str] | None = None,
    timeout_s: float = 30.0,
) -> list[KPITrendPoint] | None:
    """Compute a real per-period sparkline for the ad-hoc preview (F-017-26).

    An unsaved KPI has no snapshots, so the saved-path sparkline (which reads
    KPISnapshot rows) is always empty in preview. Spec 11.6/15.2.1 promises a
    live sparkline. This issues ONE grouped time-series query through the
    query-router gateway:

        SELECT DATE_TRUNC('<grain>', <time_col>) AS period,
               <compiled-value-expr> AS value
        FROM "<slug>" [WHERE <filters>]
        GROUP BY 1 ORDER BY 1 DESC LIMIT <n>

    The value expression is the KPI's own compiled select expression, so a
    ratio / multi-measure formula produces the same number per period that the
    scalar evaluation produces overall — no second formula to drift.

    Returns ``None`` (honest No-Data, never a wrong number) when there is no
    time dimension, the grain is unrecognised, the expression cannot be
    compiled to a single grouped SELECT (time-intelligence / KPI cross-refs),
    or the query fails. All execution flows through the gateway.
    """
    if not time_column:
        return None
    grain = (trend_period or "month").lower()
    if grain not in _ADHOC_TREND_GRAINS:
        return None
    # A time-intelligence or KPI-reference expression cannot be rendered as a
    # single GROUP BY select; the per-period number would be wrong. Skip.
    if expression_has_time_intelligence(expression):
        return None

    from src.kpi_compiler import CompilerContext, compile_expression

    measure_aggs = {
        name: (m.default_agg or "sum") for name, m in measure_map.items()
    }
    try:
        compiled = compile_expression(
            expression,
            CompilerContext(
                model_slug=model_slug,
                calc_agg_mode=getattr(ctx, "calc_agg_mode", "automatic"),
                default_agg="sum",
                measure_aggs=measure_aggs,
            ),
        )
        value_expr = compiled.select_expr
    except Exception as exc:  # noqa: BLE001 — preview is best-effort
        log.debug("Ad-hoc trend series compile failed: %s", exc)
        return None

    n = max(2, min(int(n_periods or 12), 60))
    time_col = _safe_ident(time_column)
    model = _safe_ident(model_slug)
    period_expr = f"DATE_TRUNC('{grain}', {time_col})"
    where = f" WHERE {filter_where_clause}" if filter_where_clause else ""
    sql = (
        f"SELECT {period_expr} AS period, {value_expr} AS value "
        f"FROM {model}{where} "
        f"GROUP BY {period_expr} ORDER BY {period_expr} DESC LIMIT {n}"
    )
    if sql_sink is not None:
        sql_sink.append(sql)
    try:
        result = await _execute_via_router(
            model_id, sql, bearer, timeout_s=timeout_s, persona_id=persona_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Bug-5340: keep the honest No-Data fallback (never surface a wrong
        # number or a 500 to the preview), but log at WARNING so a swallowed
        # router/source error — e.g. an un-transpiled passthrough query against
        # a non-PG source — is observable rather than silently rendering an
        # empty sparkline.
        log.warning("Ad-hoc trend series query failed (sparkline empty): %s", exc)
        return None

    rows = result.get("rows", []) or []
    points: list[KPITrendPoint] = []
    for row in rows:
        if isinstance(row, dict):
            period = row.get("period")
            value = row.get("value")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            period, value = row[0], row[1]
        else:
            continue
        try:
            fval = float(value) if value is not None else None
        except (TypeError, ValueError):
            fval = None
        points.append(KPITrendPoint(
            period=str(period) if period is not None else "",
            value=fval,
        ))
    # Query returned newest-first for a bounded LIMIT; chart wants oldest-first.
    points.reverse()
    # A single point is not a sparkline; require ≥2 to avoid a misleading dot.
    if len(points) < 2:
        return None
    return points


# Approximate calendar duration per trend_period grain, used to pick the
# prior snapshot nearest to (now - trend_period) for trend comparison (F-017-13).
_TREND_PERIOD_DAYS: dict[str, float] = {
    "day": 1.0,
    "week": 7.0,
    "month": 30.0,
    "quarter": 91.0,
    "year": 365.0,
}


def _select_prior_snapshot_value(
    snap_rows: list,
    trend_period: str | None,
) -> float | None:
    """Pick the prior-period snapshot value for trend comparison (F-017-13).

    ``snap_rows`` is ordered oldest -> newest. When ``trend_period`` names a
    grain (month/quarter/...), select the snapshot whose ``snapshot_at`` is
    closest to ``newest - period`` so "Improving (vs month)" compares against a
    month ago rather than the immediately-preceding snapshot. Falls back to the
    newest snapshot when the period is unknown or there is no older snapshot.
    """
    valued = [s for s in snap_rows if s.value is not None]
    if not valued:
        return None
    if len(valued) == 1:
        return float(valued[0].value)

    newest = valued[-1]
    days = _TREND_PERIOD_DAYS.get((trend_period or "").lower())
    if days is None:
        return float(valued[-1].value)

    from datetime import timedelta

    newest_at = getattr(newest, "snapshot_at", None)
    if newest_at is None:
        return float(valued[-1].value)
    target_at = newest_at - timedelta(days=days)

    # Among snapshots strictly older than the newest, pick the one whose
    # timestamp is closest to the target instant.
    candidates = [s for s in valued[:-1] if getattr(s, "snapshot_at", None)]
    if not candidates:
        return float(valued[-1].value)
    best = min(candidates, key=lambda s: abs((s.snapshot_at - target_at).total_seconds()))
    return float(best.value)


async def _finalize_kpi_response(
    kpi: KPI,
    ctx,
    db,
    value: float | None,
    target: float | None,
    *,
    model_id: UUID | None = None,
    model_slug: str | None = None,
    bearer: str | None = None,
    persona_id: str | None = None,
    measure_map: dict[str, "Measure"] | None = None,
) -> KPIEvaluateResponse:
    """Shared post-score pipeline: threshold → trend → format → response.

    Every endpoint that produces a KPI value (single /evaluate,
    /evaluate-batch passes including the composite third pass) MUST
    converge here so all indicators — status, trend, formatted value —
    agree on the same KPI everywhere it renders: KpisPanel, Scorecard,
    and the $KPIs virtual table fed by the kpi_latest upsert.
    """
    from src.kpi_evaluator import _sanitize_float
    from src.kpi_formatter import format_value, format_variance, value_str_if_needed
    from src.kpi_threshold import evaluate_threshold
    from src.kpi_trend import evaluate_trend

    value = _sanitize_float(value)
    target = _sanitize_float(target)

    # Sparkline: load recent snapshots for trend_series. Loaded before the
    # threshold call so statistical evaluation types (z_score) can consume the
    # historical series — F-017-03.
    sparkline_limit = kpi.trend_sparkline_periods or 12
    snap_result = await db.execute(
        select(KPISnapshot)
        .where(KPISnapshot.kpi_id == kpi.id)
        .order_by(KPISnapshot.snapshot_at.desc())
        .limit(sparkline_limit)
    )
    snap_rows = list(snap_result.scalars().all())
    snap_rows.reverse()
    trend_series = [
        KPITrendPoint(
            period=s.snapshot_at.isoformat(),
            value=float(s.value) if s.value is not None else None,
        )
        for s in snap_rows
    ] or None
    # Trailing historical values (oldest→newest) for statistical evaluation.
    historical_values = [
        float(s.value) for s in snap_rows if s.value is not None
    ] or None

    # Threshold — use custom bands from presentation_meta when available,
    # matching the Python evaluator pipeline behaviour. z_score consumes the
    # trailing snapshot history; percentile_rank consumes peer values resolved
    # from presentation_meta.peer_dimension (F-017-03).
    status_val, status_label, status_color = None, None, None
    status_position, status_bands = None, None
    meta = ctx.presentation_meta or {}
    eval_type = meta.get("evaluation_type", "percentage_of_target")
    peer_values = None
    if eval_type == "percentile_rank" and value is not None and bearer and model_slug:
        peer_values = await _resolve_peer_values(
            kpi, meta, model_id or kpi.model_id, model_slug, bearer, persona_id,
            measure_map=measure_map,
        )
    th = None
    if meta.get("bands") and value is not None:
        th = evaluate_threshold(
            value=value, target=target, direction=ctx.direction,
            evaluation_type=eval_type,
            bands=meta.get("bands"),
            historical_values=historical_values,
            peer_values=peer_values,
        )
    elif eval_type in ("z_score", "percentile_rank") and value is not None:
        th = evaluate_threshold(
            value=value, target=target, direction=ctx.direction,
            evaluation_type=eval_type,
            historical_values=historical_values,
            peer_values=peer_values,
        )
    elif value is not None and target is not None:
        # Bug-1226: thread the evaluation_type so variance KPIs without custom
        # bands score on their own deviation-ordered default preset (F-017-02),
        # not the percentage_of_target default. Otherwise the badge would read
        # the variance label while the position/bands carried a different scale.
        th = evaluate_threshold(
            value=value, target=target, direction=ctx.direction,
            evaluation_type=eval_type,
        )
    if th is not None:
        status_val = th.status
        status_label = th.status_label
        status_color = th.status_color
        # Bug-1226: expose the authoritative gauge position and the bands the
        # status was matched against so the frontend plots the needle on the
        # same scale the backend scored — no second-layer rescaling.
        status_position = th.ratio
        status_bands = _serialize_bands(th.bands_used)

    # Trend: select the prior snapshot nearest to (now - trend_period) so the
    # "vs month" chip actually compares against a month ago, not whatever the
    # newest snapshot happens to be (F-017-13). Falls back to the most recent
    # snapshot when trend_period is unset or no older snapshot exists.
    prior_value = _select_prior_snapshot_value(
        snap_rows, getattr(ctx, "trend_period", None),
    )
    tr = evaluate_trend(
        value, prior_value,
        threshold=ctx.trend_threshold, direction=ctx.direction, target=target,
    )

    # Format
    fv = format_value(
        value, format_token=ctx.format_token,
        null_display_value=ctx.null_display_value, unit_label=ctx.unit_label,
    )
    ft = format_value(
        target, format_token=ctx.format_token,
        null_display_value=ctx.null_display_value,
    )
    abs_var, _ = format_variance(
        value, target, format_token=ctx.format_token, direction=ctx.direction,
    )

    return KPIEvaluateResponse(
        kpi_id=kpi.id,
        value=value,
        value_str=value_str_if_needed(value),
        target=target,
        status=status_val,
        status_label=status_label,
        status_color=status_color,
        status_position=status_position,
        status_bands=status_bands,
        trend=tr.trend,
        trend_label=tr.trend_label,
        trend_pct=tr.trend_pct,
        formatted_value=fv.display,
        formatted_target=ft.display,
        formatted_variance=abs_var,
        goal=target,
        formatted_goal=ft.display,
        trend_series=trend_series,
    )


async def _apply_snapshot_bounds_for_min_max(
    db,
    children,  # list[ChildScore]
    children_kpis,  # list[KPI]
    method: str,
    bound_min: float | None,
    bound_max: float | None,
) -> None:
    """F-017-09: for a min_max composite with no configured bounds, derive each
    child's normalisation bounds from its trailing snapshots (spec 9.1) so the
    child still scores instead of being silently excluded (-> NULL composite).

    No-op for pct_target, or when explicit bounds are present. Mutates the
    ChildScore objects in place, setting bound_min/bound_max per child.
    """
    if method != "min_max" or bound_min is not None or bound_max is not None:
        return
    by_id = {str(c.id): c for c in children_kpis}
    for child in children:
        kpi = by_id.get(str(child.kpi_id))
        if kpi is None:
            continue
        snap_result = await db.execute(
            select(KPISnapshot.value)
            .where(KPISnapshot.kpi_id == kpi.id)
            .where(KPISnapshot.value.is_not(None))
            .order_by(KPISnapshot.snapshot_at.desc())
            .limit(kpi.trend_sparkline_periods or 12)
        )
        vals = [float(v) for v in snap_result.scalars().all() if v is not None]
        # Include the live value so a single-snapshot history still yields a
        # non-degenerate range (the value sits at one bound).
        if child.raw_value is not None:
            vals.append(float(child.raw_value))
        if len(vals) >= 2 and min(vals) != max(vals):
            child.bound_min = min(vals)
            child.bound_max = max(vals)


async def _evaluate_composite_score(
    parent: KPI,
    db,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    measure_map: dict,
    *,
    model,
    is_privileged: bool,
    effective_persona_id: str | None,
    allowed_measure_ids: list[UUID] | None = None,
    name_to_id: dict[str, UUID] | None = None,
    _path: frozenset[str] = frozenset(),
) -> CompositeResult:
    """Evaluate a composite KPI's weighted child score (single-KPI path).

    Composite KPIs carry a placeholder expression; presenting its raw
    value would be silently wrong. Children are loaded with the same
    visibility rules as the batch path, evaluated through the standard
    machinery, then normalised and weighted (kpi_composite.py).

    Composite children are evaluated recursively (their weighted score is
    the child's raw value, normalised against the child's own target like
    any other child). Recursion is cycle-guarded via *_path* and bounded
    by _MAX_COMPOSITE_DEPTH; both conditions raise CompositeEvaluationError
    instead of silently feeding placeholder values upward.

    Returns the full ``CompositeResult`` so the caller can surface the
    Bug-4255 degraded/error signal. A child whose evaluation FAILED (errored
    leaf, or a nested composite that itself errored / hit the cycle/depth
    guard) is recorded as an errored child rather than silently dropped, so
    the parent renders its score yet flags the broken input.
    """
    parent_id_str = str(parent.id)
    if parent_id_str in _path:
        raise CompositeEvaluationError(_COMPOSITE_CYCLE_LABEL)
    if len(_path) >= _MAX_COMPOSITE_DEPTH:
        raise CompositeEvaluationError(_COMPOSITE_DEPTH_LABEL)
    path = _path | {parent_id_str}

    child_result = await db.execute(
        select(KPI).where(
            KPI.parent_kpi_id == parent.id,
            KPI.model_id == model_id,
        )
    )
    children_kpis: list[KPI] = []
    for child in child_result.scalars().all():
        if child.certification_status == "draft" and not is_privileged:
            continue
        if not _kpi_visible_to_persona(
            child, allowed_measure_ids, name_to_id or {},
        ):
            continue
        children_kpis.append(child)
    if not children_kpis:
        return CompositeResult(composite_score=None)

    eval_cache: dict[str, dict] = {}
    for child in children_kpis:
        provider = _build_measure_provider(
            model_id, model_slug, bearer, measure_map,
            persona_id=effective_persona_id,
        )
        resp = await _evaluate_single_kpi(
            child, db, model_id, model_slug, bearer, provider,
            measure_map=measure_map,
            calendar_type=_derive_calendar_type(model),
            fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
            persona_id=effective_persona_id,
        )
        child_value = resp.value
        # Bug-4255: an errored leaf child (failed evaluation, null value) is
        # recorded distinctly so the parent flags it instead of treating it
        # as silent no-data.
        child_error = _child_error_reason(resp)
        if child.kpi_type == "composite":
            # Nested composite: its score (not the placeholder expression
            # value _evaluate_single_kpi just produced) is the raw value. If
            # the nested composite itself errored or tripped the cycle/depth
            # guard, that becomes this child's error reason — the parent stays
            # scoreable from its other children rather than the whole tree
            # collapsing.
            try:
                nested = await _evaluate_composite_score(
                    child, db, model_id, model_slug, bearer, measure_map,
                    model=model,
                    is_privileged=is_privileged,
                    effective_persona_id=effective_persona_id,
                    allowed_measure_ids=allowed_measure_ids,
                    name_to_id=name_to_id,
                    _path=path,
                )
            except CompositeEvaluationError as exc:
                child_value = None
                child_error = str(exc)
            else:
                child_value = nested.composite_score
                if nested.status == COMPOSITE_STATUS_ERROR:
                    child_error = child_error or "child composite errored"
        eval_cache[str(child.id)] = {
            "value": child_value,
            "target": resp.target,
            "error_reason": child_error,
        }

    children = build_composite_children(
        str(parent.id),
        [
            {
                "id": c.id,
                "name": c.name,
                "parent_kpi_id": c.parent_kpi_id,
                "weight": c.weight,
                "direction": c.direction,
                "target_value": c.target_value,
            }
            for c in children_kpis
        ],
        eval_cache,
    )
    if not children:
        return CompositeResult(composite_score=None)
    method, bound_min, bound_max = get_normalisation_config(parent.presentation_meta)
    await _apply_snapshot_bounds_for_min_max(
        db, children, children_kpis, method, bound_min, bound_max,
    )
    return evaluate_composite(children, method, bound_min, bound_max)


async def _resolve_referenced_kpi_values(
    expression: str,
    db,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    measure_map: dict,
    *,
    model,
    is_privileged: bool,
    persona_id: str | None,
    allowed_measure_ids: list[UUID] | None,
    name_to_id: dict[str, UUID] | None,
    _path: frozenset[str] = frozenset(),
) -> dict[str, float | None]:
    """Resolve the values of KPIs referenced via ``kpi("Name")`` (F-017-08).

    The single /evaluate Python-fallback previously built its provider without a
    ``kpi_value_cache``, so any ``kpi()`` cross-reference resolved to None and
    composite/derived KPIs showed "No Data" in KpisPanel while the Scorecard's
    batch path computed them. This loads each referenced KPI by name (same
    draft/persona visibility as the batch path), evaluates it through the shared
    single-KPI machinery, and returns a name -> value cache. Recursion is
    cycle-guarded by *_path*.
    """
    from shared.semantic.kpi_dependency import extract_kpi_references

    ref_names = extract_kpi_references(expression)
    if not ref_names:
        return {}
    cache: dict[str, float | None] = {}
    for ref_name in ref_names:
        if ref_name in _path:
            cache[ref_name] = None
            continue
        ref_result = await db.execute(
            select(KPI).where(KPI.name == ref_name, KPI.model_id == model_id)
        )
        ref_kpi = ref_result.scalars().first()
        if ref_kpi is None:
            cache[ref_name] = None
            continue
        if ref_kpi.certification_status == "draft" and not is_privileged:
            cache[ref_name] = None
            continue
        if not _kpi_visible_to_persona(
            ref_kpi, allowed_measure_ids, name_to_id or {},
        ):
            cache[ref_name] = None
            continue
        if ref_kpi.kpi_type == "composite":
            try:
                cache[ref_name] = (
                    await _evaluate_composite_score(
                        ref_kpi, db, model_id, model_slug, bearer, measure_map,
                        model=model,
                        is_privileged=is_privileged,
                        effective_persona_id=persona_id,
                        allowed_measure_ids=allowed_measure_ids,
                        name_to_id=name_to_id,
                    )
                ).composite_score
            except CompositeEvaluationError:
                cache[ref_name] = None
            continue
        provider = _build_measure_provider(
            model_id, model_slug, bearer, measure_map,
            kpi_value_cache=await _resolve_referenced_kpi_values(
                ref_kpi.expression or "", db, model_id, model_slug, bearer,
                measure_map, model=model, is_privileged=is_privileged,
                persona_id=persona_id, allowed_measure_ids=allowed_measure_ids,
                name_to_id=name_to_id, _path=_path | {ref_name},
            ),
            persona_id=persona_id,
        )
        resp = await _evaluate_single_kpi(
            ref_kpi, db, model_id, model_slug, bearer, provider,
            measure_map=measure_map,
            calendar_type=_derive_calendar_type(model),
            fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
            persona_id=persona_id,
        )
        cache[ref_name] = resp.value
    return cache


# ---- Evaluate endpoints ----


def _kpi_cache_key_components(kpi: KPI) -> tuple[str | None, list[dict] | None, dict | None]:
    """Derive the (calc_agg_mode, filters, time_context) components of the KPI
    evaluation cache key from the KPI's own definition.

    F-017-28: the cache key already *accepts* calc_agg_mode / filters /
    time_context (kpi_cache._make_key), but the single-KPI evaluate endpoint
    used to pass none — the key was correct only because the endpoint applies
    no request-level filters and the KPI's definitional filters are immutable
    between cache invalidations (any KPI edit calls invalidate_kpi). That made
    the key *safe* rather than *correct by construction*: two KPIs that differ
    only in their definitional filters/calc-mode would still key distinctly via
    kpi_id, but the documented key fields stayed empty, so the spec'd contract
    was unmet and a future change that fed request filters through the same
    code path would have keyed identically and bled results across slices.

    We now populate the key from the KPI definition so the key reflects every
    input that affects the value. The result value is unchanged (the same KPI
    keys to the same components every render); the key is simply faithful to
    the spec and robust if request-level filters are ever wired in.
    """
    calc_agg_mode = getattr(kpi, "calc_agg_mode", None)
    filters: list[dict] | None = None
    time_context: dict | None = None
    bd = getattr(kpi, "business_definition", None)
    if isinstance(bd, dict):
        raw_filters = bd.get("filters")
        if isinstance(raw_filters, list) and raw_filters:
            filters = raw_filters
        # The time window (relative or absolute) is the time-context input that
        # shifts the value. Exclude the volatile ``_compiled`` SQL cache — it is
        # derived from these inputs, not an independent dimension.
        tw = bd.get("time_window")
        if isinstance(tw, dict) and tw:
            time_context = tw
    return calc_agg_mode, filters, time_context


@router.post(
    "/{kpi_id}/evaluate",
    response_model=KPIEvaluateResponse,
    dependencies=[require_role("viewer")],
)
async def evaluate_kpi(
    model_id: UUID,
    kpi_id: UUID,
    request: Request,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIEvaluateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        # Draft KPIs are only visible to modelers/admins (Section 13.1)
        is_privileged = current_user.role in (
            "modeler", "admin", "tenant_admin", "system_admin",
        )
        if kpi.certification_status == "draft" and not is_privileged:
            raise HTTPException(status_code=404, detail="KPI not found")

        model = await db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found")

        # Persona-based measure filtering (Section 13.1)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        effective_persona_id: str | None = str(persona.id) if persona else None
        allowed_measure_ids: list[UUID] | None = None
        name_to_id: dict[str, UUID] = {}
        if persona:
            allowed_measure_ids = parse_allowed_ids(persona.included_measure_ids)
            if allowed_measure_ids is not None:
                measures_result = await db.execute(
                    select(Measure).where(Measure.model_id == model_id)
                )
                name_to_id = {m.name: m.id for m in measures_result.scalars().all()}
                if not _kpi_visible_to_persona(kpi, allowed_measure_ids, name_to_id):
                    raise HTTPException(
                        status_code=403,
                        detail="KPI references measures outside persona scope",
                    )

        # Cache check — AFTER authorization (draft + persona) to prevent
        # leaking results across privilege levels. user_id and persona_id are
        # included in the cache key because query-router applies row-security
        # and persona-specific policies per caller/persona combination.
        _cache = get_kpi_cache()
        # F-017-28: key on the KPI's own calc-mode / filters / time-window so
        # the cache key reflects every value-affecting input (see
        # _kpi_cache_key_components). These are stable per KPI between
        # invalidations, so the cached value is unchanged.
        _ck_calc_mode, _ck_filters, _ck_time_ctx = _kpi_cache_key_components(kpi)
        cached = _cache.get(
            current_user.tenant_id, model_id, kpi_id,
            calc_agg_mode=_ck_calc_mode, filters=_ck_filters,
            time_context=_ck_time_ctx,
            user_id=current_user.user_id, persona_id=effective_persona_id,
        )
        if cached is not None:
            return cached

        bearer = (
            getattr(current_user, "raw_token", None)
            or request.headers.get("Authorization", "").replace("Bearer ", "")
        )

        # Load measures for provider
        measure_rows = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        measure_map = {m.name: m for m in measure_rows.scalars().all()}

        ctx = await _build_evaluation_context(kpi, db, model_id)

        # Resolve time column for time intelligence SQL compilation
        time_column = await _resolve_time_column(db, kpi, model_id)

        # Extract business-definition WHERE clauses (filters + time window)
        bd_where: str | None = None
        bd_filter_where: str | None = None
        bd_time_where: str | None = None
        bd_ti_type: str | None = None
        bd_ti_grain: str | None = None
        bd_ti_n_periods: int | None = None
        bd_base_expression: str | None = None
        bd_tw_start_sql: str | None = None
        bd_tw_end_sql: str | None = None
        bd_share_type: str | None = None
        bd_share_dimension: str | None = None
        bd_share_n: int | None = None
        bd_filter_predicate_list: list[str] | None = None
        has_bd = False
        bd = getattr(kpi, "business_definition", None)
        if bd and isinstance(bd, dict):
            has_bd = True
            compiled_scope = bd.get("_compiled")
            if compiled_scope and isinstance(compiled_scope, dict):
                bd_where = compiled_scope.get("where_clause")
                fp = compiled_scope.get("filter_predicates", [])
                tp = compiled_scope.get("time_window_predicates", [])
                bd_filter_where = " AND ".join(fp) if fp else None
                bd_time_where = " AND ".join(tp) if tp else None
                bd_filter_predicate_list = fp if fp else None
                bd_ti_type = compiled_scope.get("ti_type")
                bd_ti_grain = compiled_scope.get("ti_grain")
                bd_ti_n_periods = compiled_scope.get("ti_n_periods")
                bd_base_expression = compiled_scope.get("base_expression")
                bd_tw_start_sql = compiled_scope.get("time_window_start_sql")
                bd_tw_end_sql = compiled_scope.get("time_window_end_sql")
                bd_share_type = compiled_scope.get("share_type")
                bd_share_dimension = compiled_scope.get("share_dimension")
                bd_share_n = compiled_scope.get("share_n")

        value: float | None = None
        target: float | None = None
        composite_result: CompositeResult | None = None

        if kpi.expression:
            if kpi.kpi_type == "composite":
                # Composite KPIs score their children; the stored expression
                # is a placeholder and must never be presented as the value.
                try:
                    composite_result = await _evaluate_composite_score(
                        kpi, db, model_id, model.slug, bearer, measure_map,
                        model=model,
                        is_privileged=is_privileged,
                        effective_persona_id=effective_persona_id,
                        allowed_measure_ids=allowed_measure_ids,
                        name_to_id=name_to_id,
                    )
                except CompositeEvaluationError as exc:
                    log.error("Composite evaluation failed for %s: %s", kpi_id, exc)
                    return KPIEvaluateResponse(
                        kpi_id=kpi_id,
                        value=None,
                        status_label=str(exc),
                    )
                value = composite_result.composite_score
            else:
                # v2 path: try SQL compiler first, fall back to Python evaluator
                value = await _evaluate_expression_via_sql(
                    kpi.expression, model_id, model.slug, bearer, measure_map, ctx,
                    time_column=time_column,
                    calendar_type=_derive_calendar_type(model),
                    fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
                    at_grain=kpi.at_grain,
                    non_additive_agg=kpi.non_additive_agg,
                    carry_forward=kpi.carry_forward,
                    inner_agg=kpi.inner_agg,
                    inner_grain=kpi.inner_grain,
                    outer_agg=kpi.outer_agg,
                    persona_id=effective_persona_id,
                    where_clause=bd_where,
                    filter_where_clause=bd_filter_where,
                    time_where_clause=bd_time_where,
                    enable_ti_subquery=has_bd,
                    ti_type=bd_ti_type,
                    ti_grain=bd_ti_grain,
                    ti_n_periods=bd_ti_n_periods,
                    base_expression=bd_base_expression,
                    time_window_start_sql=bd_tw_start_sql,
                    time_window_end_sql=bd_tw_end_sql,
                    share_type=bd_share_type,
                    share_dimension=bd_share_dimension,
                    share_n=bd_share_n,
                    filter_predicate_list=bd_filter_predicate_list,
                )
                if value is _TI_NO_TIME_DIMENSION:
                    return KPIEvaluateResponse(
                        kpi_id=kpi_id,
                        value=None,
                        status_label=_TI_NO_TIME_DIMENSION_LABEL,
                    )
                if isinstance(value, _TIDecompositionFailed):
                    return KPIEvaluateResponse(
                        kpi_id=kpi_id,
                        value=None,
                        status_label=value.label,
                    )
                if value is _EVALUATION_ERROR:
                    return KPIEvaluateResponse(
                        kpi_id=kpi_id,
                        value=None,
                        status_label="Evaluation failed — check KPI expression and model scope",
                    )
                if value is _COMPILER_UNSUPPORTED:
                    # Expression has time intelligence or KPI refs; use Python evaluator.
                    # Pre-fetch all referenced measures in a single batch call.
                    ref_names = extract_measure_names(kpi.expression)
                    mv_cache = await _batch_get_measure_values(
                        model_id, ref_names, bearer, model.slug, measure_map,
                        persona_id=effective_persona_id,
                    )
                    # F-017-08: resolve referenced KPIs so kpi() cross-references
                    # compute on the single /evaluate path too (KpisPanel), not
                    # only in the Scorecard batch path.
                    kpi_ref_cache = await _resolve_referenced_kpi_values(
                        kpi.expression, db, model_id, model.slug, bearer,
                        measure_map,
                        model=model,
                        is_privileged=is_privileged,
                        persona_id=effective_persona_id,
                        allowed_measure_ids=allowed_measure_ids,
                        name_to_id=name_to_id,
                    )
                    provider = _build_measure_provider(
                        model_id, model.slug, bearer, measure_map,
                        measure_value_cache=mv_cache,
                        kpi_value_cache=kpi_ref_cache or None,
                        persona_id=effective_persona_id,
                        ti_evaluator=_build_ti_evaluator(
                            model_id, model.slug, bearer, measure_map,
                            time_column=time_column,
                            calc_agg_mode=ctx.calc_agg_mode,
                            fiscal_year_start_month=getattr(
                                model, "fiscal_year_start_month", None,
                            ),
                            filter_where_clause=bd_filter_where,
                            persona_id=effective_persona_id,
                        ) if time_column else None,
                    )
                    result = await run_evaluation_pipeline(ctx, provider)
                    resp = _result_to_response(result)
                    _cache.put(current_user.tenant_id, model_id, kpi_id, resp,
                               calc_agg_mode=_ck_calc_mode, filters=_ck_filters,
                               time_context=_ck_time_ctx,
                               user_id=current_user.user_id, persona_id=effective_persona_id)
                    return resp

            # Also compile target if it's an expression — same filter/time scope
            if kpi.target_expression:
                target = await _evaluate_expression_via_sql(
                    kpi.target_expression, model_id, model.slug, bearer,
                    measure_map, ctx, time_column=time_column,
                    persona_id=effective_persona_id,
                    where_clause=bd_where,
                    filter_where_clause=bd_filter_where,
                    time_where_clause=bd_time_where,
                )
                if target in (
                    _COMPILER_UNSUPPORTED, _EVALUATION_ERROR, _TI_NO_TIME_DIMENSION,
                ) or isinstance(target, _TIDecompositionFailed):
                    target = None
                ctx.target_value = target
            elif kpi.target_type == "measure" and kpi.target_measure_id:
                # Resolve target measure value live via query-router
                target_measure = measure_map.get(None)  # lookup by id below
                for m in measure_map.values():
                    if m.id == kpi.target_measure_id:
                        target_measure = m
                        break
                if target_measure:
                    target = await _get_measure_value(
                        model_id, target_measure.name, bearer,
                        model.slug, target_measure.default_agg or "sum",
                        persona_id=effective_persona_id,
                        where_clause=bd_where,  # F-017-14: same slice as the value
                    )
                    ctx.target_value = target
            elif kpi.target_type == "static" and kpi.target_value is not None:
                target = float(kpi.target_value)
                ctx.target_value = target

            # Run remaining pipeline steps (threshold, trend, formatting)
            # via the shared post-score pipeline so single and batch agree.
            resp = await _finalize_kpi_response(
                kpi, ctx, db, value, ctx.target_value,
                model_id=model_id, model_slug=model.slug, bearer=bearer,
                persona_id=effective_persona_id, measure_map=measure_map,
            )
            # Bug-4255: surface the composite degraded/error signal so a broken
            # child KPI is visible on the parent card.
            resp = _apply_composite_signal(resp, composite_result)
            _cache.put(current_user.tenant_id, model_id, kpi_id, resp,
                       calc_agg_mode=_ck_calc_mode, filters=_ck_filters,
                       time_context=_ck_time_ctx,
                       user_id=current_user.user_id, persona_id=effective_persona_id)
            return resp

        # No expression configured — return error response
        return KPIEvaluateResponse(
            kpi_id=kpi.id,
            value=None,
            status_label="No expression configured",
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# Ad-hoc evaluation (F-01)
# ---------------------------------------------------------------------------

@router.post(
    "/evaluate-adhoc",
    response_model=KPIEvaluateResponse,
    dependencies=[require_role("viewer")],
)
async def evaluate_adhoc(
    project_id: UUID,
    model_id: UUID,
    body: KPIAdhocRequest,
    request: Request,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIEvaluateResponse:
    """Evaluate an ad-hoc KPI expression without saving.

    Used by the wizard live preview to test expressions against real data.
    """
    import time as _time
    start_ms = _time.monotonic_ns()

    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id
        )

        bearer = (
            getattr(current_user, "raw_token", None)
            or request.headers.get("Authorization", "").replace("Bearer ", "")
        )

        # Load measures for the model
        measure_rows = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        measure_map = {m.name: m for m in measure_rows.scalars().all()}

        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        effective_persona_id: str | None = str(persona.id) if persona else None
        allowed_measure_ids: list[UUID] | None = None
        allowed_dimension_ids: list[UUID] | None = None
        if persona:
            allowed_measure_ids = parse_allowed_ids(persona.included_measure_ids)
            allowed_dimension_ids = parse_allowed_ids(persona.included_dimension_ids)
        if allowed_measure_ids is not None:
            allowed_measure_set = set(allowed_measure_ids)
            allowed_names = {
                m.name for m in measure_map.values() if m.id in allowed_measure_set
            }
            _assert_kpi_expressions_in_measure_scope(
                [body.expression, body.target_expression],
                allowed_names,
            )

        # Build a minimal evaluation context from the request
        from src.kpi_evaluator import EvaluationContext, _sanitize_float
        from src.kpi_formatter import format_value, format_variance, value_str_if_needed
        from src.kpi_threshold import evaluate_threshold
        from src.kpi_trend import evaluate_trend

        # If business_definition is provided, compile it first
        adhoc_compiled_expression: str | None = None
        adhoc_compiled_scope: dict | None = None
        adhoc_where: str | None = None
        adhoc_filter_where: str | None = None
        adhoc_time_where: str | None = None
        adhoc_has_bd = False
        adhoc_ti_type: str | None = None
        adhoc_ti_grain: str | None = None
        adhoc_ti_n_periods: int | None = None
        adhoc_base_expression: str | None = None
        adhoc_tw_start_sql: str | None = None
        adhoc_tw_end_sql: str | None = None
        adhoc_share_type: str | None = None
        adhoc_share_dimension: str | None = None
        adhoc_share_n: int | None = None
        adhoc_filter_predicate_list: list[str] | None = None
        adhoc_time_column: str | None = None
        adhoc_expression = body.expression
        adhoc_target_expression = body.target_expression
        adhoc_target_value: float | None = None
        adhoc_direction = body.direction

        if body.business_definition and isinstance(body.business_definition, dict):
            from src.kpi_business_builder import (
                compile_business_definition,
                validate_business_definition,
            )

            measures_all = list(measure_map.values())
            if allowed_measure_ids is not None:
                allowed_measure_set = set(allowed_measure_ids)
                measures_all = [m for m in measures_all if m.id in allowed_measure_set]
            m_ids = {str(m.id) for m in measures_all}
            m_name_map = {str(m.id): m.name for m in measures_all}

            dims_result = await db.execute(
                select(Dimension).where(Dimension.model_id == model_id)
            )
            dims = list(dims_result.scalars().all())
            if allowed_dimension_ids is not None:
                allowed_dimension_set = set(allowed_dimension_ids)
                dims = [d for d in dims if d.id in allowed_dimension_set]
            d_ids = {str(d.id) for d in dims}
            d_name_map = {str(d.id): d.name for d in dims}
            dimension_data_types = await _dimension_data_types(db, dims)
            td_ids = {
                str(d.id) for d in dims
                if _is_time_dimension(d, dimension_data_types)
            }
            td_name_map = {str(d.id): d.name for d in dims if str(d.id) in td_ids}

            bd_errors = validate_business_definition(
                body.business_definition, m_ids, d_ids, td_ids,
            )
            if bd_errors:
                raise HTTPException(
                    status_code=400,
                    detail={"message": "Invalid business definition", "errors": bd_errors},
                )

            compiled_bd = compile_business_definition(
                body.business_definition, m_name_map, d_name_map, td_name_map,
            )
            adhoc_expression = compiled_bd.expression
            adhoc_compiled_expression = compiled_bd.expression
            adhoc_compiled_scope = {
                "summary": compiled_bd.summary,
                "summary_tokens": compiled_bd.summary_tokens,
                "filter_predicates": compiled_bd.filter_predicates,
                "time_window_predicates": compiled_bd.time_window_predicates,
                "direction": compiled_bd.direction,
            }
            adhoc_where = compiled_bd.where_clause
            adhoc_filter_where = (
                " AND ".join(compiled_bd.filter_predicates)
                if compiled_bd.filter_predicates else None
            )
            adhoc_time_where = (
                " AND ".join(compiled_bd.time_window_predicates)
                if compiled_bd.time_window_predicates else None
            )
            adhoc_has_bd = True
            adhoc_direction = compiled_bd.direction
            adhoc_time_column = compiled_bd.time_dimension_name
            adhoc_ti_type = compiled_bd.ti_type
            adhoc_ti_grain = compiled_bd.ti_grain
            adhoc_ti_n_periods = compiled_bd.ti_n_periods
            adhoc_base_expression = compiled_bd.base_expression
            adhoc_tw_start_sql = compiled_bd.time_window_start_sql
            adhoc_tw_end_sql = compiled_bd.time_window_end_sql
            adhoc_share_type = compiled_bd.share_type
            adhoc_share_dimension = compiled_bd.share_dimension
            adhoc_share_n = compiled_bd.share_n
            adhoc_filter_predicate_list = (
                compiled_bd.filter_predicates
                if compiled_bd.filter_predicates else None
            )
            if compiled_bd.target_expression:
                adhoc_target_expression = compiled_bd.target_expression
                if allowed_measure_ids is not None:
                    _assert_kpi_expressions_in_measure_scope(
                        [adhoc_target_expression],
                        allowed_names,
                    )
            # F-017-14: a builder-compiled static target value must surface in
            # the preview too — otherwise a target_comparison builder KPI shows
            # no target/status until saved.
            if compiled_bd.target_value is not None:
                adhoc_target_value = compiled_bd.target_value

        if not adhoc_expression:
            raise HTTPException(
                status_code=400,
                detail="Either expression or business_definition is required",
            )

        # Wizard/formula preview path: resolve the requested time dimension
        # (accepts a dimension id or name) so time-intelligence expressions
        # can evaluate through the decomposed machinery (F-017-01).
        if not adhoc_time_column and body.time_dimension:
            dims_result = await db.execute(
                select(Dimension).where(Dimension.model_id == model_id)
            )
            wanted = body.time_dimension.strip()
            for d in dims_result.scalars().all():
                if d.is_time_dim and (str(d.id) == wanted or d.name == wanted):
                    adhoc_time_column = d.name
                    break

        ctx = EvaluationContext(
            kpi_id=UUID("00000000-0000-0000-0000-000000000000"),
            kpi_name="__adhoc__",
            expression=adhoc_expression,
            kpi_type=None,
            target_type="expression" if adhoc_target_expression else None,
            target_value=None,
            target_expression=adhoc_target_expression,
            direction=adhoc_direction,
            presentation_type=None,
            presentation_meta=body.presentation_meta,
            calc_agg_mode=body.calc_agg_mode,
            trend_period=body.trend_period or "month",
            trend_threshold=0.01,
            format_token=body.format_token,
            format_custom=body.format_custom,
            unit_label=body.unit_label,
            null_display_value="N/A",
        )

        # Evaluate value expression — capture the SQL sent to the gateway so the
        # builder's "Show SQL" preview can display the real executed statements.
        adhoc_sql_parts: list[str] = []
        value = await _evaluate_expression_via_sql(
            adhoc_expression, model_id, model.slug, bearer,
            measure_map, ctx,
            persona_id=effective_persona_id,
            time_column=adhoc_time_column,
            where_clause=adhoc_where,
            filter_where_clause=adhoc_filter_where,
            time_where_clause=adhoc_time_where,
            enable_ti_subquery=adhoc_has_bd,
            ti_type=adhoc_ti_type,
            ti_grain=adhoc_ti_grain,
            ti_n_periods=adhoc_ti_n_periods,
            base_expression=adhoc_base_expression,
            time_window_start_sql=adhoc_tw_start_sql,
            time_window_end_sql=adhoc_tw_end_sql,
            share_type=adhoc_share_type,
            share_dimension=adhoc_share_dimension,
            share_n=adhoc_share_n,
            filter_predicate_list=adhoc_filter_predicate_list,
            sql_sink=adhoc_sql_parts,
            timeout_s=_ADHOC_TIMEOUT_S,
        )
        # De-duplicate while preserving order (repeated period queries collapse).
        adhoc_compiled_sql = (
            "\n\n".join(dict.fromkeys(adhoc_sql_parts)) or None
        )
        if value is _TI_NO_TIME_DIMENSION:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Time-intelligence expression requires a time dimension — "
                    "pass time_dimension in the request or set one on the KPI"
                ),
            )
        if isinstance(value, _TIDecompositionFailed):
            raise HTTPException(status_code=400, detail=value.label)
        if value is _EVALUATION_ERROR or value is _COMPILER_UNSUPPORTED:
            ref_names = extract_measure_names(adhoc_expression)
            mv_cache = await _batch_get_measure_values(
                model_id, ref_names, bearer, model.slug, measure_map,
                persona_id=effective_persona_id,
            )
            provider = _build_measure_provider(
                model_id, model.slug, bearer, measure_map,
                measure_value_cache=mv_cache,
                persona_id=effective_persona_id,
                ti_evaluator=_build_ti_evaluator(
                    model_id, model.slug, bearer, measure_map,
                    time_column=adhoc_time_column,
                    calc_agg_mode=body.calc_agg_mode,
                    fiscal_year_start_month=getattr(
                        model, "fiscal_year_start_month", None,
                    ),
                    filter_where_clause=adhoc_filter_where,
                    time_window_start_sql=adhoc_tw_start_sql,
                    time_window_end_sql=adhoc_tw_end_sql,
                    sql_sink=adhoc_sql_parts,
                    timeout_s=_ADHOC_TIMEOUT_S,
                ) if adhoc_time_column else None,
            )
            result = await run_evaluation_pipeline(ctx, provider)
            if result.value is None and value is _EVALUATION_ERROR:
                raise HTTPException(
                    status_code=400,
                    detail="KPI evaluation failed — check expression and model scope",
                )
            return _result_to_response(result)

        # Evaluate target — same filter/time scope as the value (F-017-14).
        # Use adhoc_target_expression (which carries the builder-compiled target
        # when a business_definition was provided), not the raw body field;
        # otherwise the preview shows no target/status for KPIs whose saved form
        # will have one. Static builder targets fall back to adhoc_target_value.
        target: float | None = None
        if adhoc_target_expression:
            target = await _evaluate_expression_via_sql(
                adhoc_target_expression, model_id, model.slug, bearer,
                measure_map, ctx,
                persona_id=effective_persona_id,
                time_column=adhoc_time_column,
                where_clause=adhoc_where,
                filter_where_clause=adhoc_filter_where,
                time_where_clause=adhoc_time_where,
                filter_predicate_list=adhoc_filter_predicate_list,
                timeout_s=_ADHOC_TIMEOUT_S,
            )
            if target in (
                _COMPILER_UNSUPPORTED, _EVALUATION_ERROR, _TI_NO_TIME_DIMENSION,
            ) or isinstance(target, _TIDecompositionFailed):
                target = None
        if target is None and adhoc_target_value is not None:
            target = float(adhoc_target_value)

        value = _sanitize_float(value)
        target = _sanitize_float(target)

        # Threshold — use custom bands from presentation_meta when available,
        # matching the full evaluator pipeline in kpi_evaluator.py.
        status_val, status_label, status_color = None, None, None
        status_position, status_bands = None, None
        meta = body.presentation_meta or {}
        adhoc_eval_type = meta.get("evaluation_type", "percentage_of_target")
        th = None
        if meta.get("bands") and value is not None:
            th = evaluate_threshold(
                value=value, target=target, direction=ctx.direction,
                evaluation_type=adhoc_eval_type,
                bands=meta.get("bands"),
            )
        elif value is not None and target is not None:
            # Bug-1226: thread the evaluation_type so a variance preview scores on
            # the deviation preset, keeping badge/needle/band agreement.
            th = evaluate_threshold(
                value=value, target=target, direction=ctx.direction,
                evaluation_type=adhoc_eval_type,
            )
        if th is not None:
            status_val = th.status
            status_label = th.status_label
            status_color = th.status_color
            # Bug-1226: authoritative gauge position + bands, so the preview
            # gauge agrees with the status badge for every evaluation type.
            status_position = th.ratio
            status_bands = _serialize_bands(th.bands_used)

        # Trend (no prior value in ad-hoc)
        tr = evaluate_trend(value, None, threshold=ctx.trend_threshold, direction=ctx.direction, target=target)

        # Format
        fv = format_value(
            value, format_token=ctx.format_token,
            format_custom=ctx.format_custom,
            null_display_value=ctx.null_display_value,
            unit_label=ctx.unit_label,
        )
        ft_target = format_value(
            target, format_token=ctx.format_token,
            format_custom=ctx.format_custom,
            null_display_value=ctx.null_display_value,
            unit_label=ctx.unit_label,
        ) if target is not None else None

        var = format_variance(value, target, direction=ctx.direction) if value is not None and target is not None else None

        # F-017-26: a live sparkline for the preview. Unsaved KPIs have no
        # snapshots, so build a real per-period series from a single grouped
        # time-series query when a time dimension is available. Returns None
        # (honest No-Data, never a wrong number) when not computable.
        adhoc_trend_series = await _build_adhoc_trend_series(
            adhoc_expression, model_id, model.slug, bearer, measure_map, ctx,
            time_column=adhoc_time_column,
            trend_period=body.trend_period,
            filter_where_clause=adhoc_filter_where,
            n_periods=(body.presentation_meta or {}).get("trend_sparkline_periods", 12),
            persona_id=effective_persona_id,
            sql_sink=adhoc_sql_parts,
            timeout_s=_ADHOC_TIMEOUT_S,
        )
        # Refresh the compiled-SQL surface so the preview's "Show SQL" panel
        # also exposes the sparkline query (kept in execution order, de-duped).
        if adhoc_trend_series is not None:
            adhoc_compiled_sql = "\n\n".join(dict.fromkeys(adhoc_sql_parts)) or None

        elapsed = int((_time.monotonic_ns() - start_ms) / 1_000_000)

        return KPIEvaluateResponse(
            kpi_id=ctx.kpi_id,
            value=value,
            value_str=value_str_if_needed(value),
            target=target,
            status=status_val,
            status_label=status_label,
            status_color=status_color,
            status_position=status_position,
            status_bands=status_bands,
            trend=tr.trend,
            trend_label=tr.trend_label,
            trend_pct=tr.trend_pct,
            formatted_value=fv.display,
            formatted_target=ft_target.display if ft_target else None,
            formatted_variance=var[0] if var else None,
            compiled_expression=adhoc_compiled_expression,
            compiled_scope=adhoc_compiled_scope,
            compiled_sql=adhoc_compiled_sql,
            goal=target,
            formatted_goal=ft_target.display if ft_target else None,
            trend_series=adhoc_trend_series,
            evaluation_ms=elapsed,
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/evaluate-batch",
    response_model=KPIBatchResponse,
    dependencies=[require_role("viewer")],
)
async def evaluate_batch(
    model_id: UUID,
    body: KPIBatchRequest,
    request: Request,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIBatchResponse:
    """Evaluate multiple KPIs in a single request.

    Handles composite KPIs by evaluating children first (topological order)
    and feeding cached results into composite score calculation.
    """
    import time as _time
    start_ms = _time.monotonic_ns()

    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found")

        bearer = (
            getattr(current_user, "raw_token", None)
            or request.headers.get("Authorization", "").replace("Bearer ", "")
        )

        # Load measures once for all KPIs
        measure_rows = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        measure_map = {m.name: m for m in measure_rows.scalars().all()}

        # Persona-based measure filtering (Section 13.1)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        effective_persona_id: str | None = str(persona.id) if persona else None
        allowed_measure_ids: list[UUID] | None = None
        name_to_id: dict[str, UUID] = {}
        if persona:
            allowed_measure_ids = parse_allowed_ids(persona.included_measure_ids)
            if allowed_measure_ids is not None:
                name_to_id = {m.name: m.id for m in measure_map.values()}

        # Load all requested KPIs in a single query (F-23 fix: eliminates N+1)
        is_privileged = current_user.role in (
            "modeler", "admin", "tenant_admin", "system_admin",
        )
        requested_ids = set(body.kpi_ids)
        kpi_query = select(KPI).where(
            KPI.id.in_(list(requested_ids)),
            KPI.model_id == model_id,
        )
        kpi_result = await db.execute(kpi_query)
        kpi_objs: dict[UUID, KPI] = {}
        for kpi in kpi_result.scalars().all():
            # Draft KPIs hidden from non-privileged users
            if kpi.certification_status == "draft" and not is_privileged:
                continue
            # Persona filtering: skip KPIs referencing excluded measures
            if not _kpi_visible_to_persona(kpi, allowed_measure_ids, name_to_id):
                continue
            kpi_objs[kpi.id] = kpi

        # Composite KPIs score their children — auto-load any child the
        # caller did not explicitly request, otherwise the composite would
        # silently return its placeholder expression value. Children are
        # evaluated into the cache but excluded from the response (results
        # are built from body.kpi_ids only). The load is TRANSITIVE: a
        # composite child of a composite pulls its own children in turn,
        # so nested composites score recursively instead of feeding their
        # placeholder upward. The visited set bounds the loop even if the
        # write-time cycle guard (_would_cycle) were ever bypassed.
        loaded_parent_ids: set[UUID] = set()
        frontier = [
            k.id for k in kpi_objs.values() if k.kpi_type == "composite"
        ]
        while frontier:
            loaded_parent_ids.update(frontier)
            child_result = await db.execute(
                select(KPI).where(
                    KPI.parent_kpi_id.in_(frontier),
                    KPI.model_id == model_id,
                    KPI.id.notin_(list(kpi_objs.keys())),
                )
            )
            frontier = []
            for kpi in child_result.scalars().all():
                if kpi.certification_status == "draft" and not is_privileged:
                    continue
                if not _kpi_visible_to_persona(kpi, allowed_measure_ids, name_to_id):
                    continue
                kpi_objs[kpi.id] = kpi
                if kpi.kpi_type == "composite" and kpi.id not in loaded_parent_ids:
                    frontier.append(kpi.id)

        # Build dependency graph for topological ordering
        kpi_dicts = [
            {"id": k.id, "name": k.name, "expression": k.expression}
            for k in kpi_objs.values()
        ]
        graph = build_graph(kpi_dicts)
        eval_order, _ = topological_sort(graph)

        # Map name -> KPI for ordered iteration
        name_to_kpi = {k.name: k for k in kpi_objs.values()}

        # Evaluate in topological order, caching results
        # kpi_value_cache: name -> evaluated value (for kpi() cross-references)
        kpi_value_cache: dict[str, float | None] = {}
        # eval_cache: str(id) -> {value, target} (for composite children)
        eval_cache: dict[str, dict] = {}
        result_map: dict[UUID, KPIEvaluateResponse] = {}

        # Pre-fetch all measures referenced by any KPI in the batch (F-20 fix).
        # This eliminates N+1 individual measure queries in the Python evaluator.
        all_referenced_measures: set[str] = set()
        for k in kpi_objs.values():
            if k.expression:
                all_referenced_measures.update(extract_measure_names(k.expression))
        batch_measure_cache: dict[str, float | None] = {}
        if all_referenced_measures:
            batch_measure_cache = await _batch_get_measure_values(
                model_id, list(all_referenced_measures), bearer, model.slug, measure_map,
                persona_id=effective_persona_id,
            )

        # Bug-5252: compile request-level filters into SQL predicates so they
        # are forwarded to _evaluate_single_kpi alongside the per-KPI
        # business-definition filters.  Load the model's parameter defaults
        # so parameter-mode filters resolve correctly (mirrors the single-
        # evaluate path which compiles at save time with real defaults).
        compiled_request_filters: list[str] | None = None
        if body.filters:
            from shared.db.models import Dimension, ModelParameter
            dim_rows = await db.execute(
                select(Dimension.id, Dimension.name)
                .where(Dimension.model_id == model_id)
            )
            dim_name_map = {str(r[0]): r[1] for r in dim_rows.all()}
            if dim_name_map:
                param_rows = await db.execute(
                    select(ModelParameter).where(
                        ModelParameter.model_id == model_id
                    )
                )
                param_defaults: dict | None = None
                _params = param_rows.scalars().all()
                if _params:
                    param_defaults = {
                        p.name: p.default_value
                        for p in _params
                        if p.default_value is not None
                    }
                from src.kpi_business_builder import _compile_filters
                preds = _compile_filters(body.filters, dim_name_map, param_defaults)
                if preds:
                    compiled_request_filters = preds

        # Build all_kpis list for composite children lookup
        all_kpis_for_composite = [
            {
                "id": k.id,
                "name": k.name,
                "parent_kpi_id": k.parent_kpi_id,
                "weight": k.weight,
                "direction": k.direction,
                "target_value": k.target_value,
            }
            for k in kpi_objs.values()
        ]

        # First pass: evaluate in topological order
        for kpi_name in eval_order:
            kpi = name_to_kpi.get(kpi_name)
            if kpi is None:
                continue

            provider = _build_measure_provider(
                model_id, model.slug, bearer, measure_map,
                kpi_value_cache=kpi_value_cache,
                measure_value_cache=batch_measure_cache,
                persona_id=effective_persona_id,
            )

            response = await _evaluate_single_kpi(
                kpi, db, model_id, model.slug, bearer, provider,
                measure_map=measure_map,
                calendar_type=_derive_calendar_type(model),
                fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
                persona_id=effective_persona_id,
                request_filter_predicates=compiled_request_filters,
            )
            result_map[kpi.id] = response
            kpi_value_cache[kpi.name] = response.value
            eval_cache[str(kpi.id)] = {
                "value": response.value,
                "target": response.target,
                # Bug-4255: flag a failed child evaluation so a composite
                # parent surfaces it as degraded instead of silent no-data.
                "error_reason": _child_error_reason(response),
            }

        # Second pass: evaluate KPIs not in the graph (missing expressions)
        for kpi_id, kpi in kpi_objs.items():
            if kpi_id in result_map:
                continue
            provider = _build_measure_provider(
                model_id, model.slug, bearer, measure_map,
                kpi_value_cache=kpi_value_cache,
                measure_value_cache=batch_measure_cache,
                persona_id=effective_persona_id,
            )
            response = await _evaluate_single_kpi(
                kpi, db, model_id, model.slug, bearer, provider,
                measure_map=measure_map,
                calendar_type=_derive_calendar_type(model),
                fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
                persona_id=effective_persona_id,
                request_filter_predicates=compiled_request_filters,
            )
            result_map[kpi.id] = response
            kpi_value_cache[kpi.name] = response.value
            eval_cache[str(kpi.id)] = {
                "value": response.value,
                "target": response.target,
                # Bug-4255: flag a failed child evaluation so a composite
                # parent surfaces it as degraded instead of silent no-data.
                "error_reason": _child_error_reason(response),
            }

        # Third pass: compute composite scores for composite KPIs.
        # Composites are processed child-first (nesting depth order) so a
        # composite child's SCORE — not its placeholder expression value —
        # feeds its parent. Every composite result is rebuilt through the
        # shared post-score pipeline (_finalize_kpi_response) so status,
        # trend, and formatting agree exactly with the single /evaluate
        # endpoint (and with the kpi_latest upsert that feeds $KPIs).
        composites = [k for k in kpi_objs.values() if k.kpi_type == "composite"]
        children_by_parent: dict[UUID, list[KPI]] = {}
        for k in kpi_objs.values():
            if k.parent_kpi_id is not None:
                children_by_parent.setdefault(k.parent_kpi_id, []).append(k)

        def _nesting_depth(comp_id: UUID, path: frozenset) -> int:
            """Depth of the composite-of-composite chain rooted at comp_id.

            Raises CompositeEvaluationError on a cycle (fail loud — never
            score from placeholder values).
            """
            if comp_id in path:
                raise CompositeEvaluationError(_COMPOSITE_CYCLE_LABEL)
            child_depths = [
                _nesting_depth(c.id, path | {comp_id})
                for c in children_by_parent.get(comp_id, [])
                if c.kpi_type == "composite"
            ]
            return 1 + max(child_depths, default=0)

        ordered: list[tuple[int, KPI]] = []
        for kpi in composites:
            try:
                depth = _nesting_depth(kpi.id, frozenset())
            except CompositeEvaluationError as exc:
                log.error("Composite evaluation failed for %s: %s", kpi.id, exc)
                err_resp = KPIEvaluateResponse(
                    kpi_id=kpi.id, value=None, status_label=str(exc),
                )
                result_map[kpi.id] = err_resp
                kpi_value_cache[kpi.name] = None
                eval_cache[str(kpi.id)] = {"value": None, "target": None}
                continue
            if depth > _MAX_COMPOSITE_DEPTH:
                log.error(
                    "Composite evaluation failed for %s: %s",
                    kpi.id, _COMPOSITE_DEPTH_LABEL,
                )
                err_resp = KPIEvaluateResponse(
                    kpi_id=kpi.id, value=None,
                    status_label=_COMPOSITE_DEPTH_LABEL,
                )
                result_map[kpi.id] = err_resp
                kpi_value_cache[kpi.name] = None
                eval_cache[str(kpi.id)] = {"value": None, "target": None}
                continue
            ordered.append((depth, kpi))
        ordered.sort(key=lambda pair: pair[0])

        for _, kpi in ordered:
            children = build_composite_children(
                str(kpi.id), all_kpis_for_composite, eval_cache,
            )
            composite_result: CompositeResult | None = None
            if children:
                method, bound_min, bound_max = get_normalisation_config(
                    kpi.presentation_meta,
                )
                # F-017-09: derive per-child min_max bounds from snapshots when
                # the composite has none configured (spec 9.1 historical fallback).
                child_kpi_objs = [
                    ko for c in children
                    if (ko := kpi_objs.get(UUID(c.kpi_id))) is not None
                ]
                await _apply_snapshot_bounds_for_min_max(
                    db, children, child_kpi_objs, method, bound_min, bound_max,
                )
                composite_result = evaluate_composite(
                    children, method, bound_min, bound_max,
                )
                score = composite_result.composite_score
            else:
                # No scoreable children — present None, never the
                # placeholder expression value pass 1 produced.
                score = None

            existing = result_map.get(kpi.id)
            target = existing.target if existing else None
            ctx = await _build_evaluation_context(kpi, db, model_id)
            final = await _finalize_kpi_response(kpi, ctx, db, score, target)
            # Bug-4255: stamp the degraded/error signal so the scorecard shows
            # which child KPI is broken instead of silently dropping it.
            final = _apply_composite_signal(final, composite_result)
            result_map[kpi.id] = final
            kpi_value_cache[kpi.name] = final.value
            eval_cache[str(kpi.id)] = {
                "value": final.value,
                "target": final.target,
                # Bug-4255: a composite that errored (every child errored)
                # propagates as an errored child to its own parent composite.
                "error_reason": (
                    final.status_label
                    if composite_result is not None
                    and composite_result.status == COMPOSITE_STATUS_ERROR
                    else None
                ),
            }

        # Build final results list preserving original request order
        results: list[KPIEvaluateResponse] = []
        for kpi_id in body.kpi_ids:
            if kpi_id in result_map:
                results.append(result_map[kpi_id])
            else:
                results.append(KPIEvaluateResponse(kpi_id=kpi_id))

        # F-017-11: publish the governed (unscoped) value to ``kpi_latest`` —
        # the row that feeds the JDBC ``$KPIs`` virtual table served to ALL
        # users — ONLY from the service-context sweep, never from a per-user
        # scorecard render.
        #
        # ``evaluate-batch`` runs under the CALLER's identity: the query-router
        # applies that caller's row-security predicate and persona measure
        # scope, so ``result_map`` holds a value NARROWED to the caller. If a
        # per-user render were allowed to upsert ``kpi_latest``, whichever user
        # last opened the scorecard would publish their row-scoped number to
        # every JDBC user — a nondeterministic, cross-user row-scope leak.
        #
        # The scheduler KPI snapshot sweep calls this same endpoint under a
        # service token (no persona, governed row context) and carries the
        # HMAC-authenticated internal-service marker. Only that path may
        # publish, and only when no persona narrowed the evaluation, so the
        # ``$KPIs`` value is the single deterministic model-level governed
        # number — the same way a deployed measure is model-level. The sweep's
        # own ``_upsert_kpi_latest`` is the primary writer; this call keeps the
        # endpoint and the sweep on one shared publish helper. Fail-closed:
        # any caller without the verified marker is excluded.
        is_service_context = is_internal_request_header(
            request.headers.get(INTERNAL_BYPASS_HEADER)
        )
        if is_service_context and effective_persona_id is None:
            await _upsert_kpi_latest_batch(db, model_id, kpi_objs, result_map)

        elapsed = int((_time.monotonic_ns() - start_ms) / 1_000_000)
        return KPIBatchResponse(results=results, evaluation_ms=elapsed)

    raise HTTPException(status_code=500, detail="DB session exhausted")


def _kpi_latest_value_tuple(
    *,
    kpi_name: str,
    value,
    target,
    status,
    status_label,
    trend_pct,
    formatted_value,
) -> tuple:
    """Normalise the value-bearing kpi_latest columns into a comparable tuple.

    Numeric columns come back from the DB as ``Decimal`` but are written as
    ``float``; normalising both sides to ``float`` lets an unchanged render be
    recognised as a no-op. ``evaluated_at`` is deliberately excluded — it is a
    freshness stamp, not a published value, so refreshing it on every render is
    exactly the write amplification F-017-29 removes. The materialised value
    stays correct: an unchanged write would have stored the same numbers.
    """
    def _num(v):
        return None if v is None else float(v)

    return (
        kpi_name,
        _num(value),
        _num(target),
        status,
        status_label,
        _num(trend_pct),
        formatted_value,
    )


async def _upsert_kpi_latest_batch(
    db,
    model_id: UUID,
    kpi_objs: dict,
    result_map: dict,
) -> None:
    """Upsert evaluated KPI results into kpi_latest for the $KPIs virtual table.

    F-017-29: ``evaluate-batch`` is on the scorecard *read* path and was issuing
    one UPSERT plus a commit on every render even when the materialised value
    had not changed — write amplification on a hot read path. We now load the
    existing rows once and write only the KPIs whose value-bearing columns
    actually differ (or that have no row yet). When nothing changed we skip the
    writes and the commit entirely. The published $KPIs value is unaffected: a
    skipped write would have stored identical numbers, so the governed value
    fed to the JDBC ``$KPIs`` surface stays correct. ``evaluated_at`` is not
    part of the change set, so an unchanged render no longer bumps it — that is
    intentional (it is a freshness stamp, not a value).

    Each changed row is still upserted independently inside a SAVEPOINT so a
    single failing row (e.g. a constraint or persistence error on one KPI)
    cannot abort the whole batch — the others are still materialised, and the
    failure is logged per-KPI. Status labels are truncated to the column width
    before the write; the explicit fail-loud label is still returned to the
    caller in full via the response, only the materialised $KPIs copy is
    bounded.
    """
    from datetime import datetime, timezone
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not result_map:
        return

    # Load existing rows once so we can dedupe unchanged writes.
    existing_rows = (
        await db.execute(
            select(KPILatest).where(
                KPILatest.model_id == model_id,
                KPILatest.kpi_id.in_(list(result_map.keys())),
            )
        )
    ).scalars().all()
    existing_by_kpi = {
        row.kpi_id: _kpi_latest_value_tuple(
            kpi_name=row.kpi_name,
            value=row.value,
            target=row.target,
            status=row.status,
            status_label=row.status_label,
            trend_pct=row.trend_pct,
            formatted_value=row.formatted_value,
        )
        for row in existing_rows
    }

    now = datetime.now(timezone.utc)
    persisted = 0
    for kpi_id, response in result_map.items():
        kpi = kpi_objs.get(kpi_id)
        if kpi is None:
            continue
        status_label = _truncate_status_label(response.status_label)
        new_tuple = _kpi_latest_value_tuple(
            kpi_name=kpi.name,
            value=response.value,
            target=response.target,
            status=response.status,
            status_label=status_label,
            trend_pct=response.trend_pct,
            formatted_value=response.formatted_value,
        )
        # Skip the write when the materialised value is byte-equal to what we
        # would store — the common scorecard-re-render case.
        if existing_by_kpi.get(kpi_id) == new_tuple:
            continue
        stmt = pg_insert(KPILatest).values(
            model_id=model_id,
            kpi_id=kpi_id,
            kpi_name=kpi.name,
            value=response.value,
            target=response.target,
            status=response.status,
            status_label=status_label,
            trend_pct=response.trend_pct,
            formatted_value=response.formatted_value,
            evaluated_at=now,
        ).on_conflict_do_update(
            constraint="uq_kpi_latest_model_kpi",
            set_={
                "kpi_name": kpi.name,
                "value": response.value,
                "target": response.target,
                "status": response.status,
                "status_label": status_label,
                "trend_pct": response.trend_pct,
                "formatted_value": response.formatted_value,
                "evaluated_at": now,
            },
        )
        try:
            # SAVEPOINT: isolate each row so one failure cannot poison the
            # surrounding transaction and abort the rest of the batch.
            async with db.begin_nested():
                await db.execute(stmt)
            persisted += 1
        except Exception as exc:  # noqa: BLE001 — log + continue, never 500 the batch
            log.warning(
                "kpi_latest upsert skipped for kpi %s (%s) on model %s: %s",
                kpi_id, getattr(kpi, "name", "?"), model_id, exc,
            )
    if persisted:
        try:
            await db.commit()
        except Exception:
            await db.rollback()


async def _evaluate_single_kpi(
    kpi: KPI,
    db,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    provider: MeasureValueProvider,
    measure_map: dict | None = None,
    calendar_type: str | None = None,
    fiscal_year_start_month: int | None = None,
    persona_id: str | None = None,
    request_filter_predicates: list[str] | None = None,
) -> KPIEvaluateResponse:
    """Evaluate a single KPI and return a response object.

    Tries the SQL compiler path first (faster, single query) and falls
    back to the Python evaluator if the expression can't be compiled.
    """
    ctx = await _build_evaluation_context(kpi, db, model_id)

    # F-017-14: resolve the business-definition scope WHERE up-front so a
    # measure-based target is sliced the same way as the value (EMEA target vs
    # EMEA revenue), not against the unfiltered grand total.
    _bd_obj = getattr(kpi, "business_definition", None)
    _bd_where_for_target: str | None = None
    if _bd_obj and isinstance(_bd_obj, dict):
        _compiled = _bd_obj.get("_compiled")
        if _compiled and isinstance(_compiled, dict):
            _bd_where_for_target = _compiled.get("where_clause")

    # Resolve measure-based target live via query-router
    if kpi.target_type == "measure" and kpi.target_measure_id and ctx.target_value is None:
        target_m = await db.get(Measure, kpi.target_measure_id)
        if target_m and target_m.model_id == model_id:
            ctx.target_value = await _get_measure_value(
                model_id, target_m.name, bearer,
                model_slug, target_m.default_agg or "sum",
                persona_id=persona_id,
                where_clause=_bd_where_for_target,
            )

    if not kpi.expression:
        return KPIEvaluateResponse(
            kpi_id=kpi.id,
            value=None,
            status_label="No expression configured",
        )

    # Extract business-definition WHERE clauses
    batch_bd_where: str | None = None
    batch_bd_filter_where: str | None = None
    batch_bd_time_where: str | None = None
    batch_ti_type: str | None = None
    batch_ti_grain: str | None = None
    batch_ti_n_periods: int | None = None
    batch_base_expression: str | None = None
    batch_tw_start_sql: str | None = None
    batch_tw_end_sql: str | None = None
    batch_share_type: str | None = None
    batch_share_dimension: str | None = None
    batch_share_n: int | None = None
    batch_filter_predicate_list: list[str] | None = None
    batch_has_bd = False
    batch_bd = getattr(kpi, "business_definition", None)
    if batch_bd and isinstance(batch_bd, dict):
        batch_has_bd = True
        compiled_scope = batch_bd.get("_compiled")
        if compiled_scope and isinstance(compiled_scope, dict):
            batch_bd_where = compiled_scope.get("where_clause")
            fp = compiled_scope.get("filter_predicates", [])
            tp = compiled_scope.get("time_window_predicates", [])
            batch_bd_filter_where = " AND ".join(fp) if fp else None
            batch_bd_time_where = " AND ".join(tp) if tp else None
            batch_filter_predicate_list = fp if fp else None
            batch_ti_type = compiled_scope.get("ti_type")
            batch_ti_grain = compiled_scope.get("ti_grain")
            batch_ti_n_periods = compiled_scope.get("ti_n_periods")
            batch_base_expression = compiled_scope.get("base_expression")
            batch_tw_start_sql = compiled_scope.get("time_window_start_sql")
            batch_tw_end_sql = compiled_scope.get("time_window_end_sql")
            batch_share_type = compiled_scope.get("share_type")
            batch_share_dimension = compiled_scope.get("share_dimension")
            batch_share_n = compiled_scope.get("share_n")

    # Bug-5252: merge request-level filter predicates from the batch request
    # into the per-KPI business-definition filters so the evaluation SQL
    # includes them in its WHERE clause.
    if request_filter_predicates:
        all_fp = list(batch_filter_predicate_list or []) + request_filter_predicates
        batch_filter_predicate_list = all_fp
        batch_bd_filter_where = " AND ".join(all_fp)
        # Rebuild the combined where_clause (filters + time window)
        parts = [p for p in (batch_bd_filter_where, batch_bd_time_where) if p]
        batch_bd_where = " AND ".join(parts) if parts else batch_bd_where

    # Try SQL compiler path first (matches single-evaluate endpoint pattern)
    if measure_map is not None:
        time_column = await _resolve_time_column(db, kpi, model_id)
        value = await _evaluate_expression_via_sql(
            kpi.expression, model_id, model_slug, bearer, measure_map, ctx,
            time_column=time_column,
            calendar_type=calendar_type,
            fiscal_year_start_month=fiscal_year_start_month,
            at_grain=kpi.at_grain,
            non_additive_agg=kpi.non_additive_agg,
            carry_forward=kpi.carry_forward,
            inner_agg=kpi.inner_agg,
            inner_grain=kpi.inner_grain,
            outer_agg=kpi.outer_agg,
            persona_id=persona_id,
            where_clause=batch_bd_where,
            filter_where_clause=batch_bd_filter_where,
            time_where_clause=batch_bd_time_where,
            enable_ti_subquery=batch_has_bd,
            ti_type=batch_ti_type,
            ti_grain=batch_ti_grain,
            ti_n_periods=batch_ti_n_periods,
            base_expression=batch_base_expression,
            time_window_start_sql=batch_tw_start_sql,
            time_window_end_sql=batch_tw_end_sql,
            share_type=batch_share_type,
            share_dimension=batch_share_dimension,
            share_n=batch_share_n,
            filter_predicate_list=batch_filter_predicate_list,
        )
        if value is _TI_NO_TIME_DIMENSION:
            return KPIEvaluateResponse(
                kpi_id=kpi.id,
                value=None,
                status_label=_TI_NO_TIME_DIMENSION_LABEL,
            )
        if isinstance(value, _TIDecompositionFailed):
            return KPIEvaluateResponse(
                kpi_id=kpi.id,
                value=None,
                status_label=value.label,
            )
        if value is _EVALUATION_ERROR:
            return KPIEvaluateResponse(
                kpi_id=kpi.id,
                value=None,
                status_label="Evaluation failed — check KPI expression and model scope",
            )
        if value is _COMPILER_UNSUPPORTED and time_column:
            # Python pipeline fallback resolves TI subtrees via the
            # decomposed machinery when a time dimension is bound.
            provider.evaluate_time_intelligence = _build_ti_evaluator(
                model_id, model_slug, bearer, measure_map,
                time_column=time_column,
                calc_agg_mode=ctx.calc_agg_mode,
                fiscal_year_start_month=fiscal_year_start_month,
                filter_where_clause=batch_bd_filter_where,
                time_window_start_sql=batch_tw_start_sql,
                time_window_end_sql=batch_tw_end_sql,
                persona_id=persona_id,
            )
        if value is not _COMPILER_UNSUPPORTED:
            # SQL path succeeded — resolve target, then run the shared
            # post-score pipeline so single and batch indicators agree.
            target: float | None = ctx.target_value
            if kpi.target_expression:
                t_val = await _evaluate_expression_via_sql(
                    kpi.target_expression, model_id, model_slug, bearer,
                    measure_map, ctx, time_column=time_column,
                    persona_id=persona_id,
                    where_clause=batch_bd_where,
                    filter_where_clause=batch_bd_filter_where,
                    time_where_clause=batch_bd_time_where,
                )
                if t_val not in (
                    _COMPILER_UNSUPPORTED, _EVALUATION_ERROR, _TI_NO_TIME_DIMENSION,
                ) and not isinstance(t_val, _TIDecompositionFailed):
                    target = t_val
            elif kpi.target_type == "static" and kpi.target_value is not None:
                target = float(kpi.target_value)

            return await _finalize_kpi_response(
                kpi, ctx, db, value, target,
                model_id=model_id, model_slug=model_slug, bearer=bearer,
                persona_id=persona_id, measure_map=measure_map,
            )

    # Fallback: Python evaluator
    result = await run_evaluation_pipeline(ctx, provider)
    return _result_to_response(result)


# ---------------------------------------------------------------------------
# Deploy / Undeploy (F-03)
# ---------------------------------------------------------------------------

@router.post(
    "/{kpi_id}/deploy",
    response_model=KPIResponse,
    dependencies=[require_role("modeler")],
)
async def deploy_kpi(
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    """Mark a KPI as deployed (visible to JDBC/XMLA clients)."""
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        if kpi.is_deployed:
            raise HTTPException(status_code=409, detail="KPI is already deployed")
        if not kpi.expression:
            raise HTTPException(status_code=400, detail="Cannot deploy a KPI without an expression")

        from datetime import datetime, timezone
        kpi.is_deployed = True
        kpi.deployed_at = datetime.now(timezone.utc)

        # F-017-12: audit BEFORE commit so the event shares the same
        # transaction as the flag change. audit() only flushes; the session
        # closes without a second commit, so an audit() issued after commit()
        # is rolled back and the deploy event is silently dropped (Bug-861).
        await audit(
            db, action="kpi.deploy", severity="info",
            actor_email=current_user.email,
            target_type="kpi", target_id=kpi.id,
            target_name=kpi.display_name or kpi.name,
            detail={"model_id": str(model_id)},
        )
        await db.commit()
        await db.refresh(kpi)

        get_kpi_cache().invalidate_kpi(kpi_id)
        return KPIResponse.model_validate(kpi)

    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/{kpi_id}/undeploy",
    response_model=KPIResponse,
    dependencies=[require_role("modeler")],
)
async def undeploy_kpi(
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    """Mark a KPI as undeployed (hidden from JDBC/XMLA clients)."""
    async for db in get_tenant_db(current_user.tenant_id):
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        if not kpi.is_deployed:
            raise HTTPException(status_code=409, detail="KPI is not deployed")

        kpi.is_deployed = False
        kpi.deployed_at = None

        # F-017-12: audit BEFORE commit (see deploy_kpi) — otherwise the
        # post-commit audit() only flushes and is rolled back on session close.
        await audit(
            db, action="kpi.undeploy", severity="info",
            actor_email=current_user.email,
            target_type="kpi", target_id=kpi.id,
            target_name=kpi.display_name or kpi.name,
            detail={"model_id": str(model_id)},
        )
        await db.commit()
        await db.refresh(kpi)

        get_kpi_cache().invalidate_kpi(kpi_id)
        return KPIResponse.model_validate(kpi)

    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# Snapshot endpoints (read-only)
# ---------------------------------------------------------------------------


@router.get(
    "/{kpi_id}/snapshots",
    response_model=list[KPISnapshotResponse],
    dependencies=[require_role("viewer")],
)
async def list_kpi_snapshots(
    project_id: UUID,
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    persona_id: UUID | None = Query(default=None),
    offset: int = 0,
    limit: int = 100,
) -> list[KPISnapshotResponse]:
    """Return historical snapshots for a KPI, newest first."""
    if limit > 1000:
        limit = 1000

    async for db in get_tenant_db(current_user.tenant_id):
        await _load_visible_kpi_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            kpi_id=kpi_id,
            current_user=current_user,
            persona_id=persona_id,
        )

        result = await db.execute(
            select(KPISnapshot)
            .where(KPISnapshot.kpi_id == kpi_id)
            .order_by(KPISnapshot.snapshot_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list(result.scalars().all())
        return [
            KPISnapshotResponse(
                id=s.id,
                kpi_id=s.kpi_id,
                snapshot_at=s.snapshot_at,
                value=float(s.value) if s.value is not None else None,
                target=float(s.target) if s.target is not None else None,
                status=s.status,
                status_label=s.status_label,
                trend_pct=float(s.trend_pct) if s.trend_pct is not None else None,
                filters_applied=s.filters_applied,
                evaluation_ms=s.evaluation_ms,
                created_at=s.created_at,
            )
            for s in rows
        ]

    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# Trend series (F-06)
# ---------------------------------------------------------------------------

@router.get(
    "/{kpi_id}/trend-series",
    response_model=list[KPITrendPoint],
    dependencies=[require_role("viewer")],
)
async def get_trend_series(
    project_id: UUID,
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    persona_id: UUID | None = Query(default=None),
    periods: int = 12,
) -> list[KPITrendPoint]:
    """Return sparkline data from historical snapshots.

    Returns up to ``periods`` most recent snapshots as trend points,
    oldest first (chronological order for charting).
    """
    if periods > 120:
        periods = 120

    async for db in get_tenant_db(current_user.tenant_id):
        await _load_visible_kpi_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            kpi_id=kpi_id,
            current_user=current_user,
            persona_id=persona_id,
        )

        result = await db.execute(
            select(KPISnapshot)
            .where(KPISnapshot.kpi_id == kpi_id)
            .order_by(KPISnapshot.snapshot_at.desc())
            .limit(periods)
        )
        rows = list(result.scalars().all())
        # Reverse to chronological order (oldest first)
        rows.reverse()
        return [
            KPITrendPoint(
                period=s.snapshot_at.isoformat(),
                value=float(s.value) if s.value is not None else None,
            )
            for s in rows
        ]

    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get(
    "/{kpi_id}/snapshots/latest",
    response_model=KPISnapshotResponse | None,
    dependencies=[require_role("viewer")],
)
async def get_latest_kpi_snapshot(
    project_id: UUID,
    model_id: UUID,
    kpi_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    persona_id: UUID | None = Query(default=None),
) -> KPISnapshotResponse | None:
    """Return the most recent snapshot for a KPI, or null if none exist."""
    async for db in get_tenant_db(current_user.tenant_id):
        await _load_visible_kpi_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            kpi_id=kpi_id,
            current_user=current_user,
            persona_id=persona_id,
        )

        result = await db.execute(
            select(KPISnapshot)
            .where(KPISnapshot.kpi_id == kpi_id)
            .order_by(KPISnapshot.snapshot_at.desc())
            .limit(1)
        )
        s = result.scalars().first()
        if s is None:
            return None

        return KPISnapshotResponse(
            id=s.id,
            kpi_id=s.kpi_id,
            snapshot_at=s.snapshot_at,
            value=float(s.value) if s.value is not None else None,
            target=float(s.target) if s.target is not None else None,
            status=s.status,
            status_label=s.status_label,
            trend_pct=float(s.trend_pct) if s.trend_pct is not None else None,
            filters_applied=s.filters_applied,
            evaluation_ms=s.evaluation_ms,
            created_at=s.created_at,
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")
