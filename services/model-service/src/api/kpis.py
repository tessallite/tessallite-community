"""KPI CRUD + evaluate + validate routes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func as sa_func, select
from sqlalchemy.exc import IntegrityError

from shared.audit.logger import audit
from shared.config.bootstrap import system_snapshot_get
from shared.config.settings import get_settings
from shared.middleware.action_throttle import consume_action_quota
from shared.db.models import (
    KPI,
    KPISnapshot,
    Dimension,
    KPIUsage,
    KPIVersion,
    Measure,
    Model,
    ModelColumn,
    ModelParameter,
    RowSecurityRule,
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
    internal_request_headers,
    is_internal_request_header,
)
from shared.semantic.kpi_dependency import (
    analyse_dependencies,
    build_graph,
    extract_kpi_references,
    topological_sort,
)
from shared.semantic.kpi_expression import extract_measure_names, validate_expression
from shared.auth.project_access import load_authorized_model
from src.api._persona_scope import parse_allowed_ids, resolve_effective_persona
from src.api._model_lock import acquire_model_definition_lock
from src.api._scope import (
    ensure_model_in_project,
    ensure_ref_in_model,
    purge_entity_soft_references,
)
from src.auth.middleware import CurrentUser, CurrentServiceUser, get_current_user, require_capability_or_service_scope
from shared.auth.service_principal import (
    SCOPE_KPI_EVALUATE,
    SCOPE_KPI_QUERY_EXECUTE,
)
# Bug-8453: the single definition of the /execute row-security contract.
from shared.security.execute_contract import (
    ROW_SECURITY_DENY_ALL_RULE_ID as _SHARED_DENY_ALL_RULE_ID,
    row_security_denied_all as _shared_denied_all,
    security_rules_from_execute_response,
)
from src.kpi_threshold import get_preset_bands, list_presets, validate_bands
from src.auth.rbac import caller_has_role, require_role
from src.kpi_cache import get_kpi_cache
from src.kpi_deploy_resolver import (
    KpiSnapshotInvalidError,
    ResolvedKpi,
    Withheld,
    resolve_served_filter_metadata,
    resolve_served_kpi,
    resolve_served_kpis,
)
from src.kpi_compiler import (
    CompilerContext,
    KPIUnsupportedAggregationError,
    compile_expression,
    derive_ti_decomposition,
    derive_ti_decomposition_from_node,
    expression_has_time_intelligence,
)
from src.kpi_composite import (
    COMPOSITE_STATUS_ERROR,
    COMPOSITE_STATUS_RESTRICTED,
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

# Bug-7219: CRUD helper functions extracted to kpi_crud_helpers.py.
# Re-exported here so existing imports (from src.api.kpis import X) work.
from src.api.kpi_crud_helpers import (  # noqa: E402
    _ALLOWED_CERTIFICATION_STATUSES,
    _CLOSER_RATIO_BANDS,  # noqa: F401 - re-export; tests/test_kpi_threshold.py imports it from here
    _DATE_TYPE_MARKERS,  # noqa: F401 - re-export kept for `from src.api.kpis import X` callers
    _DEFINITION_FIELDS,
    _assert_kpi_expressions_in_measure_scope,
    _coerce_closer_absolute,
    _enforce_kpi_create_certification_guard,
    _is_time_dimension,
    _kpi_snapshot_dict,
    _kpi_visible_to_persona,
)


async def _ensure_kpi_model_scope(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    # Bug-7774: service tokens carrying SCOPE_KPI_EVALUATE bypass human RBAC
    # binding checks (which categorically reject service users after AUTH-RR-01).
    # The service token is scoped and the endpoint body validates it further via
    # require_capability_or_service_scope. We still verify the model exists and
    # belongs to the project (fail-closed) but skip binding-based role checks.
    if isinstance(current_user, CurrentServiceUser):
        scopes = getattr(current_user, "service_scopes", None) or []
        if SCOPE_KPI_EVALUATE in scopes:
            async for db in get_tenant_db(current_user.tenant_id):
                from shared.db.models import Model as _Model
                model = await db.get(_Model, model_id)
                if model is None or model.project_id != project_id:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Model not found",
                    )
                return
            raise HTTPException(status_code=500, detail="DB session exhausted")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service token does not carry KPI evaluate scope",
        )
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


router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/kpis",
    tags=["kpis"],
    dependencies=[Depends(_ensure_kpi_model_scope)],
)


# _kpi_visible_to_persona and _assert_kpi_expressions_in_measure_scope
# extracted to kpi_crud_helpers.py (Bug-7219); imported above.


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

    # F-017-12 / Bug-8728: draft visibility follows the caller's EFFECTIVE
    # project/model binding, not the coarse token role — mirrors the named-set
    # sibling. A member token with a modeler binding must see drafts; a coarse
    # privileged token with a restrictive binding must not.
    is_privileged = await caller_has_role(
        db, current_user, project_id, "modeler", model_id,
    )
    if kpi.certification_status == "draft" and not is_privileged:
        raise HTTPException(status_code=404, detail="KPI not found")

    persona = await resolve_effective_persona(
        db, current_user=current_user, model_id=model_id,
        requested_persona_id=persona_id,
    )
    if persona:
        allowed_m = parse_allowed_ids(persona.included_measure_ids)
        allowed_d = parse_allowed_ids(getattr(persona, "included_dimension_ids", None))
        if allowed_m is not None or allowed_d is not None:
            measures_result = await db.execute(
                select(Measure).where(Measure.model_id == model_id)
            )
            measures = list(measures_result.scalars().all())
            m_name_to_id = {m.name: m.id for m in measures}
            dims_result = await db.execute(
                select(Dimension).where(Dimension.model_id == model_id)
            )
            dims = list(dims_result.scalars().all())
            kpis_result = await db.execute(
                select(KPI).where(KPI.model_id == model_id)
            )
            scope = {
                "allowed_dimension_ids": allowed_d,
                "dimension_name_to_id": {d.name: d.id for d in dims},
                "measure_id_to_name": {m.id: m.name for m in measures},
                "dimension_id_to_name": {d.id: d.name for d in dims},
                "all_kpis_by_name": {
                    k.name: k for k in kpis_result.scalars().all()
                },
            }
            if not _kpi_visible_to_persona(
                kpi, allowed_m, m_name_to_id, dim_scope=scope,
            ):
                raise HTTPException(status_code=404, detail="KPI not found")
    return kpi


@router.get("", response_model=list[KPIResponse])
async def list_kpis(
    project_id: UUID,
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

        # Draft KPIs are only visible to modelers/admins (Section 13.1).
        # F-017-12 / Bug-8728: use the effective project/model binding, not the
        # coarse token role (mirrors list_named_sets).
        is_privileged = await caller_has_role(
            db, current_user, project_id, "modeler", model_id,
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

        # F-017-01 / F-013-04: BI catalogue surfaces (XMLA MDSCHEMA_KPIS, JDBC
        # $KPIs) request deployed_only. For those, the DEFINITION each client
        # sees must come from the deployed model snapshot, not the live row —
        # an undeployed edit must never reach a BI client until model deploy.
        # Resolve each deployed KPI's served definition from the snapshot;
        # withhold any KPI absent from the deployed version; fail closed on an
        # invalid snapshot. Modeller/builder surfaces (deployed_only=False) keep
        # seeing live drafts.
        # Bug-8712: the guard is `if deployed_only`, NOT `if deployed_only and
        # kpis`. With the empty-list short-circuit, a model whose deployed
        # snapshot is missing or malformed returned HTTP 200 with an empty list
        # whenever no live row survived the filters above, while the named-set
        # family (`list_named_sets`) failed closed with 409 on the same broken
        # snapshot. Two surfaces of the same deploy authority disagreed about
        # the same model, and the BI client saw "this model has no KPIs"
        # instead of a diagnosable error. Resolving an empty list is cheap and
        # still validates the authority.
        if deployed_only:
            model = await db.get(Model, model_id)
            if model is not None:
                try:
                    resolved, _withheld = await resolve_served_kpis(db, model, kpis)
                except KpiSnapshotInvalidError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{KpiSnapshotInvalidError.error_code}: {exc}",
                    )
                kpis = [r.kpi for r in resolved]

        # Persona-based measure + dimension filtering (Section 13.1, Bug-6329)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        if persona:
            allowed_m = parse_allowed_ids(persona.included_measure_ids)
            allowed_d = parse_allowed_ids(getattr(persona, "included_dimension_ids", None))
            if allowed_m is not None or allowed_d is not None:
                measures_result = await db.execute(
                    select(Measure).where(Measure.model_id == model_id)
                )
                measures = list(measures_result.scalars().all())
                m_name_to_id = {m.name: m.id for m in measures}
                dims_result = await db.execute(
                    select(Dimension).where(Dimension.model_id == model_id)
                )
                dims = list(dims_result.scalars().all())
                scope = {
                    "allowed_dimension_ids": allowed_d,
                    "dimension_name_to_id": {d.name: d.id for d in dims},
                    "measure_id_to_name": {m.id: m.name for m in measures},
                    "dimension_id_to_name": {d.id: d.name for d in dims},
                    "all_kpis_by_name": {k.name: k for k in kpis},
                }
                kpis = [
                    k for k in kpis
                    if _kpi_visible_to_persona(
                        k, allowed_m, m_name_to_id, dim_scope=scope,
                    )
                ]

        return [KPIResponse.model_validate(k) for k in kpis]
    return []


@router.get("/{kpi_id}", response_model=KPIResponse)
async def get_kpi(
    project_id: UUID,
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

        # Draft KPIs are only visible to modelers/admins (Section 13.1).
        # F-017-12 / Bug-8728: effective binding, not coarse token role.
        is_privileged = await caller_has_role(
            db, current_user, project_id, "modeler", model_id,
        )
        if kpi.certification_status == "draft" and not is_privileged:
            raise HTTPException(status_code=404, detail="KPI not found")

        # Persona-based measure + dimension filtering (Section 13.1, Bug-6329)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        if persona:
            allowed_m = parse_allowed_ids(persona.included_measure_ids)
            allowed_d = parse_allowed_ids(getattr(persona, "included_dimension_ids", None))
            if allowed_m is not None or allowed_d is not None:
                measures_result = await db.execute(
                    select(Measure).where(Measure.model_id == model_id)
                )
                measures = list(measures_result.scalars().all())
                m_name_to_id = {m.name: m.id for m in measures}
                dims_result = await db.execute(
                    select(Dimension).where(Dimension.model_id == model_id)
                )
                dims = list(dims_result.scalars().all())
                kpis_result = await db.execute(
                    select(KPI).where(KPI.model_id == model_id)
                )
                scope = {
                    "allowed_dimension_ids": allowed_d,
                    "dimension_name_to_id": {d.name: d.id for d in dims},
                    "measure_id_to_name": {m.id: m.name for m in measures},
                    "dimension_id_to_name": {d.id: d.name for d in dims},
                    "all_kpis_by_name": {
                        k.name: k for k in kpis_result.scalars().all()
                    },
                }
                if not _kpi_visible_to_persona(
                    kpi, allowed_m, m_name_to_id, dim_scope=scope,
                ):
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
    db,
    model_id: UUID,
    kpi_name: str,
    expression: str | None,
    target_expression: str | None = None,
    parent_kpi_id: UUID | None = None,
    replace_parent: bool = False,
) -> list[str] | None:
    """Check if value or target expressions create a KPI dependency cycle.

    Returns a list of cycle path strings if cycles are found, or None.
    Composite ownership (parent_kpi_id) is included so that a child whose
    expression or target references its composite parent is detected as a
    cycle: parent -> child (ownership) -> parent (expression/target ref).
    """
    if not expression and not target_expression:
        return None

    # Load all KPIs for this model -- include parent_kpi_id so build_graph
    # can see composite ownership edges.
    result = await db.execute(
        select(
            KPI.id, KPI.name, KPI.expression,
            KPI.target_expression, KPI.parent_kpi_id,
        )
        .where(KPI.model_id == model_id)
    )
    kpis = [
        {
            "id": row[0],
            "name": row[1],
            "expression": row[2],
            "target_expression": row[3],
            "parent_kpi_id": row[4],
        }
        for row in result
    ]

    # Replace the current KPI's expression or add it if new
    found = False
    for k in kpis:
        if k["name"] == kpi_name:
            k["expression"] = expression
            k["target_expression"] = target_expression
            if parent_kpi_id is not None or replace_parent:
                k["parent_kpi_id"] = parent_kpi_id
            found = True
            break
    if not found:
        import uuid
        kpis.append({
            "id": uuid.uuid4(),
            "name": kpi_name,
            "expression": expression,
            "target_expression": target_expression,
            "parent_kpi_id": parent_kpi_id,
        })

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

# _DEFINITION_FIELDS, _ALLOWED_CERTIFICATION_STATUSES,
# _enforce_kpi_create_certification_guard, _kpi_snapshot_dict,
# _DATE_TYPE_MARKERS, _is_time_dimension extracted to kpi_crud_helpers.py
# (Bug-7219); imported above. (_PRIVILEGED_CERTIFICATION_STATUSES is no longer
# imported here — its only kpis.py use, the PATCH cert-authority re-check, was
# removed as dead in the F-021-04 round-2 cleanup; it still lives in
# kpi_crud_helpers.py for the create-guard.)


async def _create_kpi_version(
    db, kpi: KPI, changed_by: str | None, summary: str | None,
) -> None:
    max_ver = await db.scalar(
        select(sa_func.max(KPIVersion.version_number))
        .where(KPIVersion.kpi_id == kpi.id)
    )
    snap = _kpi_snapshot_dict(kpi)
    db.add(KPIVersion(
        kpi_id=kpi.id,
        version_number=(max_ver or 0) + 1,
        changed_by=changed_by,
        change_summary=summary,
        snapshot=snap,
    ))


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
        dimension_data_types=dimension_data_types,
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
    project_id: UUID,
    model_id: UUID,
    body: KPICreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-7982: serialise KPI definition/governance writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)
        data = body.model_dump()

        # Bug-6264: certification is admin-only governance, applied through the
        # dedicated /certify endpoint — never minted at birth by a modeler. A KPI
        # is always created in a non-privileged state (mirrors NamedSetCreate,
        # which omits the field entirely). KPICreate exposes certification_status
        # (Bug-6893) so clients can specify "draft", but the guard below ensures
        # a non-admin cannot mint a born-shared/certified KPI.
        # Bug-9443 (Option A): authority is the caller's EFFECTIVE project/model
        # admin binding (via caller_has_role), not the coarse token role.
        await _enforce_kpi_create_certification_guard(
            data, current_user, db=db, project_id=project_id, model_id=model_id,
        )

        # Bug-6893: when certification_status was not sent by the client, the
        # schema default (None) would override the ORM column default ("draft")
        # because SQLAlchemy treats explicit None differently from absent keys.
        # Strip it so the DB default applies.
        if data.get("certification_status") is None:
            data.pop("certification_status", None)

        # ``time_dimension_id`` is the THIRD body foreign key on KPICreate, and
        # the only one that was never checked. ``parent_kpi_id`` and
        # ``target_measure_id`` each have a hand-rolled guard below; this field
        # was applied by the ``KPI(model_id=..., **data)`` splat with nothing
        # proving the dimension belonged to the path model. ``dimensions.id`` is
        # tenant-schema-wide, so a project-B dimension id satisfies the foreign
        # key added by migration 0157 and persists.
        #
        # It is dereferenced without an ownership re-check by
        # ``_resolve_time_column`` (``db.get(Dimension, kpi.time_dimension_id)``),
        # whose result becomes the time column of the compiled KPI SQL, and by
        # ``shared/model_dependency/edge_builders.py``, which draws a
        # KPI_DIMENSION_REFERENCE edge from it into the lineage graph. A foreign
        # binding is therefore a wrong number (the KPI's periods are cut on
        # another model's date column) as well as a cross-project leak.
        #
        # Validated under the definition lock, before the business-definition
        # compile and before ``db.add``. The compile may itself SET
        # ``time_dimension_id`` (kpis.py:688-692), but only by resolving a name
        # against THIS model's own dimensions, so that value needs no check —
        # what needs checking is the id the CLIENT sent, which is what ``data``
        # still holds at this point.
        await ensure_ref_in_model(
            db,
            Dimension,
            ref_id=data.get("time_dimension_id"),
            model_id=model_id,
            project_id=project_id,
            field_name="time_dimension_id",
            noun="a dimension",
        )

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

        # Validate the complete prospective graph. Expression and ownership
        # edges are only safe when considered together, including target-only
        # KPIs whose value expression is intentionally absent.
        if data.get("expression") or data.get("target_expression"):
            create_parent_id = data.get("parent_kpi_id")
            if isinstance(create_parent_id, str):
                create_parent_id = UUID(create_parent_id)
            cycles = await _check_expression_cycles(
                db,
                model_id,
                data["name"],
                data.get("expression"),
                data.get("target_expression"),
                parent_kpi_id=create_parent_id,
            )
            if cycles:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "KPI definition would create a dependency cycle",
                        "cycles": cycles,
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

        # A new child under a composite parent changes the parent's score.
        # Evict the parent and its transitive consumers so a stale cached
        # composite score is never served after a child is added.
        await _invalidate_kpi_and_dependents(
            db, model_id, kpi.id, kpi.name,
        )

        return KPIResponse.model_validate(kpi)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.patch(
    "/{kpi_id}",
    response_model=KPIResponse,
    dependencies=[require_role("modeler")],
)
async def update_kpi(
    project_id: UUID,
    model_id: UUID,
    kpi_id: UUID,
    body: KPIUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        # Bug-7982: serialise KPI definition/governance writes against Save/revert.
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")
        updates = body.model_dump(exclude_unset=True)

        # Same unchecked body foreign key as ``create_kpi`` (see the comment
        # there for what dereferences it), entering through the PATCH body and
        # applied by the blanket ``setattr`` loop below.
        #
        # Keyed on PRESENCE, not truthiness. The two sibling guards below use
        # ``updates.get(...)``, which is truthy-keyed and therefore treats an
        # explicit ``null`` as "not supplied" — that happens to be safe for them
        # because ``None`` is exactly the unbind they would allow anyway. Keying
        # on membership states the intent directly: an explicit ``null`` unbinds
        # the time dimension and stays legal, and a field that was not sent is
        # not validated because it is not written.
        if "time_dimension_id" in updates:
            await ensure_ref_in_model(
                db,
                Dimension,
                ref_id=updates["time_dimension_id"],
                model_id=model_id,
                project_id=project_id,
                field_name="time_dimension_id",
                noun="a dimension",
            )

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

        # Validate the merged definition for every dependency-graph mutation.
        # The ancestry-only _would_cycle check cannot see a stored kpi() edge
        # when parent_kpi_id changes by itself.
        if any(
            field in updates
            for field in ("expression", "target_expression", "parent_kpi_id")
        ):
            prospective_parent = updates.get("parent_kpi_id", kpi.parent_kpi_id)
            if isinstance(prospective_parent, str):
                prospective_parent = UUID(prospective_parent)
            cycles = await _check_expression_cycles(
                db,
                model_id,
                updates.get("name", kpi.name),
                updates.get("expression", kpi.expression),
                updates.get("target_expression", kpi.target_expression),
                parent_kpi_id=prospective_parent,
                replace_parent="parent_kpi_id" in updates,
            )
            if cycles:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "KPI definition would create a dependency cycle",
                        "cycles": cycles,
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

        # Certification guard.
        # Bug-9402 / Wave C decision #8: the write authority for a privileged
        # certification status is the caller's EFFECTIVE project/model binding
        # (modeler+), NOT the coarse token role. A coarse "admin" token with a
        # restrictive binding cannot mint certified/shared/deprecated; a modeler
        # binding can. Human tenant/system admins keep their bypass through the
        # shared helper. Uses the same effective-binding helper as draft READ
        # (F-017-12), so the two entitlement decisions can no longer disagree.
        # The binding lookup is only performed when a privileged status
        # transition is actually requested (short-circuit) — a plain field edit
        # never issues the extra query.
        prev_cert_status = kpi.certification_status
        if "certification_status" in updates:
            requested = updates["certification_status"]
            # Bug-6264: reject arbitrary strings AND an explicit null —
            # certification_status is a NOT NULL controlled enum, not free text.
            # Skipping null here would fail open (bypass the privileged-status
            # gate) and hit a NOT NULL 500 on commit; fail closed with 422.
            if requested not in _ALLOWED_CERTIFICATION_STATUSES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "certification_status must be one of: "
                        + ", ".join(_ALLOWED_CERTIFICATION_STATUSES)
                    ),
                )
            # Authority to set a privileged ("certified"/"shared"/"deprecated")
            # status via PATCH is modeler+ only (Wave C decision #8), enforced at
            # the route dependency ``require_role("modeler")`` — which checks the
            # caller's EFFECTIVE binding, not the coarse token role, and lets a
            # human tenant/system admin bypass. The former inner
            # ``not caller_has_role("modeler")`` re-check was dead: any caller
            # reaching this body already holds modeler+ (or is a human admin), so
            # the check could never deny. Removed in the F-021-04 hard-cutover
            # round-2 cleanup (F1); the enum guard above stays (fail-closed 422).

        changed_definition = any(k in _DEFINITION_FIELDS for k in updates)
        was_certified = kpi.certification_status in ("certified", "shared")

        # Capture old name before mutation for dependent cache invalidation
        old_kpi_name = kpi.name
        old_parent_kpi_id = kpi.parent_kpi_id

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
        elif kpi.certification_status != prev_cert_status:
            # Bug-6264: a certification transition applied via PATCH is a
            # governance event; record a version row so the trail is traceable
            # even when no definition field changed.
            await _create_kpi_version(
                db, kpi, current_user.email,
                f"Certification: {prev_cert_status} -> {kpi.certification_status}",
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

        # Definition changes can alter a direct or target-only dependency.
        # Evict the complete reverse closure; the cache-key fingerprint covers
        # sibling replicas that cannot receive this in-process eviction.
        await _invalidate_kpi_and_dependents(
            db, model_id, kpi_id, old_kpi_name, kpi.name,
            prior_parent_kpi_id=old_parent_kpi_id,
        )

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
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        # Bug-7982: serialise KPI definition/governance writes against Save/revert.
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
        # Capture the reverse closure before deletion so target-only and
        # transitive consumers are still discoverable after the row is gone.
        invalidated_kpi_ids = _dependent_kpi_ids(
            list((await db.execute(
                select(KPI).where(KPI.model_id == model_id)
            )).scalars().all()),
            kpi_id,
            {kpi.name},
        )
        # Purge the soft-referencing translation/preference rows that have no
        # FK back to the KPI and would otherwise linger forever (F-029-15).
        await purge_entity_soft_references(db, model_id=model_id, entity_id=kpi_id)
        await db.delete(kpi)
        await db.commit()
        get_kpi_cache().invalidate_kpis(invalidated_kpi_ids)


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
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        # Bug-7982: serialise KPI definition/governance writes against Save/revert.
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

        previous_kpi_name = kpi.name
        previous_parent_kpi_id = kpi.parent_kpi_id
        snap = version.snapshot

        # Bug-6264 (mirror named-sets F-018-05): a revert restores the KPI
        # DEFINITION only, never governance. ``certification_status`` is
        # deliberately EXCLUDED from the restored fields so a modeler cannot
        # re-certify a KPI by reverting to a snapshot that was certified before
        # it was demoted — certify/deprecate require admin. The KPI keeps its
        # current certification status across the revert; a certified/shared KPI
        # whose reverted definition differs is demoted to draft below, matching
        # the auto-demote on definition change in update_kpi.
        prior_cert_status = kpi.certification_status
        # Bug-6613: compare the CURRENT definition against the snapshot using the
        # SAME serialisation the snapshot was written with (_kpi_snapshot_dict),
        # so every definition field participates — UUID FKs and numerics are
        # coerced identically on both sides, avoiding both false positives
        # (spurious over-demote) and false negatives (a modeler keeping a
        # certified badge after reverting a change confined to target_value /
        # target_measure_id / parent_kpi_id / time_dimension_id / weight /
        # trend_threshold, which an exclusion-based compare would have missed).
        current_snap = _kpi_snapshot_dict(kpi)
        definition_changed = any(
            current_snap.get(k) != snap.get(k) for k in _DEFINITION_FIELDS
        )

        # Restore scalar fields from snapshot (certification_status excluded).
        _SCALAR_SNAP_FIELDS = (
            "name", "display_name", "description", "display_folder",
            "kpi_type", "expression", "calc_agg_mode",
            "inner_agg", "inner_grain", "outer_agg",
            "at_grain", "non_additive_agg", "carry_forward",
            "target_type", "target_expression", "target_period",
            "direction", "presentation_type", "presentation_meta",
            "trend_period", "trend_threshold", "trend_sparkline_periods",
            "format_token", "format_custom", "unit_label", "null_display_value",
            "weight", "indicator_type",
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
                # Validate that measure/kpi/dimension refs still exist in this
                # model. This loop is the SECOND writer of every column the
                # create/update body-FK guards now cover, so it has to enforce
                # the same membership rule or the guard is bypassable by
                # reverting to a version that predates it.
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
                elif fk_field == "time_dimension_id":
                    # This branch did not exist: the loop validated its two
                    # siblings and restored the time dimension unconditionally.
                    # A KPIVersion snapshot is written by ``_create_kpi_version``
                    # from whatever the live row held, so any foreign or since-
                    # deleted binding persisted before the create/update guards
                    # landed is preserved in the version history and reinstated
                    # verbatim by a revert. Drop the dangling reference the way
                    # the two siblings already do, rather than restoring a
                    # binding ``_resolve_time_column`` would walk into another
                    # model's date column.
                    dim = await db.get(Dimension, ref_id)
                    if dim is None or dim.model_id != model_id:
                        setattr(kpi, fk_field, None)
                        continue
                setattr(kpi, fk_field, ref_id)
            else:
                setattr(kpi, fk_field, None)

        # Bug-6264: demote a certified/shared KPI to draft when the reverted
        # definition is not identical to the current one. This never ELEVATES
        # (certification_status was not restored from the snapshot), so a modeler
        # can only ever hold or lower the trust signal via revert — never mint it.
        if definition_changed and prior_cert_status in ("certified", "shared"):
            kpi.certification_status = "draft"

        await _create_kpi_version(
            db, kpi, current_user.email,
            f"Reverted to version {version_number}",
        )
        # Bug-6264: a revert is a governance-relevant action — it can demote a
        # certified/shared KPI to draft — so emit an audit row like update/
        # certify/deprecate do, recording any certification transition.
        await audit(
            db, action="kpi.revert", severity="info",
            actor_email=current_user.email,
            target_type="kpi", target_id=kpi.id,
            target_name=kpi.display_name or kpi.name,
            detail={
                "reverted_to_version": version_number,
                "certification_status": (
                    f"{prior_cert_status} -> {kpi.certification_status}"
                    if prior_cert_status != kpi.certification_status
                    else prior_cert_status
                ),
            },
        )
        await db.commit()
        await db.refresh(kpi)
        await _invalidate_kpi_and_dependents(
            db, model_id, kpi_id, previous_kpi_name, kpi.name,
            prior_parent_kpi_id=previous_parent_kpi_id,
        )
        return KPIResponse.model_validate(kpi)
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Certification endpoints (Phase 4) ----

@router.post(
    "/{kpi_id}/certify",
    response_model=KPIResponse,
    # Bug-9402 / Wave C decision #8: KPI certify/share/deprecate authority is
    # modeler+ (effective project/model binding), unified with the
    # certification-status PATCH. Lowered from admin so the floor matches the
    # decision; human tenant/system admins still bypass via require_role.
    dependencies=[require_role("modeler")],
)
async def certify_kpi(
    project_id: UUID,
    model_id: UUID,
    kpi_id: UUID,
    _body: CertifyRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        # Bug-7982: serialise KPI definition/governance writes against Save/revert.
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        # Authority is modeler+ only (Wave C decision #8), enforced at the route
        # dependency ``require_role("modeler")`` above — which checks the caller's
        # EFFECTIVE project/model binding (not the coarse token role) and lets a
        # human tenant/system admin bypass. The former inner owner-or-privileged
        # re-check was dead code: every caller that reaches this body already
        # holds modeler+ (or is a human admin), so an owner-without-modeler can
        # never arrive here. Removed in the F-021-04 hard-cutover round-2 cleanup
        # (F1) — it neither widened nor narrowed real authority.
        if kpi.certification_status == "certified":
            return KPIResponse.model_validate(kpi)
        kpi.certification_status = "certified"
        await _create_kpi_version(db, kpi, current_user.email, "Certified")
        await audit(
            db, action="kpi.certify", severity="warn",
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
    # Bug-9402 / Wave C decision #8: KPI certify/share/deprecate authority is
    # modeler+ (effective project/model binding), unified with the
    # certification-status PATCH. Lowered from admin so the floor matches the
    # decision; human tenant/system admins still bypass via require_role.
    dependencies=[require_role("modeler")],
)
async def deprecate_kpi(
    project_id: UUID,
    model_id: UUID,
    kpi_id: UUID,
    body: DeprecateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> KPIResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        # Bug-7982: serialise KPI definition/governance writes against Save/revert.
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        # Authority is modeler+ only (Wave C decision #8), enforced at the route
        # dependency ``require_role("modeler")`` above — which checks the caller's
        # EFFECTIVE project/model binding (not the coarse token role) and lets a
        # human tenant/system admin bypass. The former inner owner-or-privileged
        # re-check was dead code: every caller that reaches this body already
        # holds modeler+ (or is a human admin), so an owner-without-modeler can
        # never arrive here. Removed in the F-021-04 hard-cutover round-2 cleanup
        # (F1) — it neither widened nor narrowed real authority.

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
            db, action="kpi.deprecate", severity="warn",
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
            id=uuid4(),
            kpi_id=kpi_id,
            workbook_id=body.workbook_id,
            worksheet=body.worksheet,
            cell_reference=body.cell_reference,
            usage_type=body.usage_type,
            reported_by=current_user.email,
            reported_at=datetime.now(timezone.utc),
        )
        db.add(usage)
        try:
            await db.commit()
        except IntegrityError:
            # opus5 finding 8: usage is fire-and-forget telemetry; a concurrent
            # model revert (delete+reinsert of the KPI) can transiently fail the
            # FK check. Never surface a 500 to the Excel client — swallow it and
            # acknowledge from the in-memory record.
            await db.rollback()
            log.warning(
                "KPI usage insert for %s lost to a concurrent revert; "
                "acknowledged without persisting.", kpi_id,
            )
            return EntityUsageResponse.model_validate(usage, from_attributes=True)
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

# Bug-8453: the sentinel and the denial predicate now live in ONE place
# (``shared.security.execute_contract``) so every /execute consumer classifies a
# denial identically — a second private copy here is exactly how the other four
# consumers were able to drift. Re-exported under the name this module uses.
ROW_SECURITY_DENY_ALL_RULE_ID = _SHARED_DENY_ALL_RULE_ID

# Status label for a KPI whose evaluation returned no value BECAUSE row
# security denied the caller every row. Deliberately NOT prefixed with any of
# ``_CHILD_ERROR_LABEL_PREFIXES``: a governance denial is not an evaluation
# failure, so a composite parent must not report a restricted child as errored.
ROW_SECURITY_RESTRICTED_LABEL = "Restricted by row security"


def _absorb_security_rules(result: dict, security_sink: set[str] | None) -> None:
    """Record the router's applied row-security rule ids into *security_sink*.

    Out-parameter in the same style as ``sql_sink``: the scalar value pathway is
    left completely untouched (a denied query still legitimately yields no
    value), while the CAUSE of that emptiness becomes visible to the caller.
    Without this, "row security denied you every row" and "there is no data" are
    the same ``None`` — the Bug-8427 symptom.
    """
    if security_sink is None:
        return
    security_sink.update(security_rules_from_execute_response(result))


def row_security_denied_all(security_sink: set[str] | None) -> bool:
    """True when the router reported the deny-all coverage predicate.

    Bug-8453: delegates to the shared execute-contract predicate so the KPI
    evaluator and the other /execute consumers cannot drift apart.
    """
    return _shared_denied_all(security_sink)


def _stamp_row_security_restriction(resp, security_sink: set[str] | None):
    """Mark *resp* restricted, and REDACT it, when row security denied all rows.

    Applied on EVERY KPI evaluation exit (single, ad-hoc, batch) so the three
    endpoints cannot drift.

    Bug-8449 (Codex gate finding 3): this deliberately does NOT test
    ``value is None``. A deny-all rewrites the query to ``... WHERE 0 = 1``,
    and over an empty scan ``COUNT(*)`` / ``COUNT(DISTINCT x)`` return **0**,
    not NULL — as does any expression wrapped in ``COALESCE``. Gating on a null
    value therefore let a denied caller be shown an authoritative "0
    transactions" with no Restricted badge: a silently wrong business number,
    which is worse than the blank card this lane set out to explain.

    A denial is a GOVERNANCE outcome about the caller, never a measurement, so
    every derived numeric surface is cleared rather than merely unlabelled — a
    stale 0 in ``formatted_value``, a ``status`` band, or a trend arrow would
    each independently reassert the number the redaction is removing. Only the
    deny-all sentinel triggers this; a narrowing rule leaves a correct,
    caller-scoped value untouched.
    """
    if resp is None:
        return resp
    if not row_security_denied_all(security_sink):
        return resp

    resp.row_security_restricted = True
    resp.status_label = ROW_SECURITY_RESTRICTED_LABEL
    # Scalar + its formatted twin.
    resp.value = None
    resp.value_str = None
    resp.formatted_value = None
    # Target / goal, and the variance derived from them.
    resp.target = None
    resp.goal = None
    resp.formatted_target = None
    resp.formatted_goal = None
    resp.formatted_variance = None
    # Threshold scoring: a band, colour or needle position all restate the
    # redacted value on the gauge.
    resp.status = None
    resp.status_color = None
    resp.status_position = None
    resp.status_bands = None
    # Trend: direction, percentages and the sparkline series.
    resp.trend = None
    resp.trend_label = None
    resp.trend_pct = None
    resp.trend_pct_normalised = None
    resp.trend_series = None
    return resp


async def _execute_via_router(
    model_id: UUID,
    query: str,
    bearer: str,
    timeout_s: float = 30.0,
    persona_id: str | None = None,
    security_sink: set[str] | None = None,
) -> dict:
    """POST a SQL query to the query-router's /execute endpoint.

    When *security_sink* is supplied, the row-security rule ids the router
    applied to this execution are added to it (Bug-8449).
    """
    import httpx as _httpx
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/execute"
    headers = {"Authorization": f"Bearer {bearer}"}
    # The query-router accepts the KPI-specific service scope only with this
    # rotating platform marker and client_kind="kpi". Forward it on the
    # model-service -> query-router hop; user-facing scorecard requests never
    # enter this helper.
    headers.update(internal_request_headers())
    body: dict = {
        "model_id": str(model_id),
        "raw_query": query,
        "protocol": "jdbc",
        # Bug-8070: declare the origin. Without it every KPI evaluation —
        # scheduled sweeps and interactive scorecard opens alike — was logged as
        # generic JDBC traffic, so an operator could not separate KPI workload
        # from BI-client workload and top-user, latency and route analyses all
        # told the wrong operational story. protocol stays "jdbc" because the SQL
        # must keep strict-parser treatment; client_kind is the attribution axis
        # (same pattern as drill/headless/plugin/agent).
        "client_kind": "kpi",
    }
    if persona_id is not None:
        body["persona_id"] = persona_id
    async with _httpx.AsyncClient(timeout=timeout_s) as client:
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
        payload = resp.json()
        _absorb_security_rules(payload, security_sink)
        return payload


from shared.connector_qualify import safe_ident as _safe_ident


# ---------------------------------------------------------------------------
# Time-intelligence decomposition -- multi-query evaluation
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

# Bug-6664: module-level bounded-concurrency semaphore for parallel period
# queries (moving_avg/trailing_sum). Module-level so that concurrent KPI
# evaluations (e.g. batch endpoint) share a single cap, preventing the
# router from being overwhelmed.
_PERIOD_CONCURRENCY = 4
_period_sem = asyncio.Semaphore(_PERIOD_CONCURRENCY)


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
    at_grain: str | None = None,
    non_additive_agg: str | None = None,
    carry_forward: bool = False,
    sql_sink: list[str] | None = None,
    timeout_s: float = 30.0,
    security_sink: set[str] | None = None,
) -> float | None:
    """Evaluate a time-intelligence KPI via decomposed simple queries.

    Returns the scalar float result or None if insufficient data.
    Raises ValueError on query execution failure.

    Bug-8570: *at_grain* / *non_additive_agg* carry the KPI's semi-additive
    reduction into EVERY per-period query. Each period is reduced to its
    closing/average value BEFORE the time-intelligence arithmetic runs, which is
    what a "trailing 3 months of the closing balance" KPI actually means. The
    prior behaviour summed every row in the period (daily balances 100/120/90
    contributed 310 instead of the 90 closing balance) and was therefore
    fail-closed by the wrong-numbers wave; threading the reduction is the
    product-correct answer that replaces the refusal.

    L7B-01: *carry_forward* is the THIRD field of that same reduction and must
    be threaded with the other two. Bug-8570 threaded only the first two, so a
    KPI authoring all three evaluated under a configuration the modeller did
    not author — and the refusal it replaced had at least been honest. See
    ``_period_sql`` for the boundary the per-period window imposes on the fill.
    """
    from src.kpi_compiler import CompilerContext, compile_expression, interval_literal

    grain = ti_grain or "month"
    interval = _TI_INTERVAL_MAP.get(grain, "1 month")
    n = ti_n_periods or 3
    # Bug-7228: never fall back to a hardcoded "date" column.  Callers
    # must resolve a valid time column before entering decomposed evaluation.
    if not time_column:
        raise ValueError(
            "time_column is required for TI decomposed evaluation; "
            "callers must resolve the KPI's time dimension first"
        )
    time_col = _safe_ident(time_column)
    start_sql = time_window_start_sql or f"DATE_TRUNC('{grain}', CURRENT_DATE)"
    # Bug-9478: exclusive end includes the anchor day (parity with period_to_date).
    end_sql = time_window_end_sql or "CURRENT_DATE + INTERVAL '1 day'"

    measure_aggs = {
        name: (m.default_agg or "sum")
        for name, m in measure_map.items()
    }

    def _compile_base(expr: str) -> tuple[str, list[str]]:
        """Compile a base expression to a simple SELECT (no TI wrapping).

        Returns (select_expr, referenced_measure_names).

        L7B-01, enumeration note: this context deliberately carries NO
        semi-additive field, ``carry_forward`` included. It exists to obtain the
        bare ``select_expr`` (for the un-reduced per-period query and for the
        Bug-6831 COUNT(DISTINCT) check); ``select_expr`` is the aggregate
        expression alone and no reduction field can change it — every one of
        them shapes the SQL *around* it. Passing ``carry_forward`` here would
        also raise ``KPITimeContextError``, since this context binds no time
        column. The reduction is applied in ``_period_sql``, which builds its
        own context.
        """
        ctx = CompilerContext(
            model_slug=model_slug,
            calc_agg_mode=calc_agg_mode,
            default_agg="sum",
            measure_aggs=measure_aggs,
        )
        compiled = compile_expression(expr, ctx)
        return compiled.select_expr, compiled.measure_names

    select_expr, referenced_measures = _compile_base(base_expression)

    # Bug-6831: COUNT(DISTINCT) + trailing_sum/moving_avg guard.
    if ti_type in ("trailing_sum", "moving_avg"):
        _has_cd = (
            "COUNT(DISTINCT" in (select_expr or "").upper()
            or any(
                (measure_aggs.get(name) or "").upper() == "COUNT_DISTINCT"
                for name in referenced_measures
            )
        )
        if _has_cd:
            raise ValueError(
                f"COUNT(DISTINCT) measures cannot use {ti_type}. "
                f"Per-period decomposition would double-count distinct "
                f"values across period boundaries."
            )

    async def _exec_scalar(sql: str) -> float | None:
        if sql_sink is not None:
            sql_sink.append(sql)
        result = await _execute_via_router(
            model_id, sql, bearer, timeout_s=timeout_s, persona_id=persona_id,
            security_sink=security_sink,
        )
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
                return None
            elif isinstance(row, (list, tuple)) and row:
                return float(row[0]) if row[0] is not None else None
        return None

    async def _exec_period_values(n_periods: int) -> list[float]:
        """Query each period concurrently via bounded-semaphore gather."""

        async def _query_one(i: int) -> float | None:
            async with _period_sem:
                offset_start = f"{end_sql} - INTERVAL '{interval_literal(i + 1, grain)}'"
                offset_end = f"{end_sql} - INTERVAL '{interval_literal(i, grain)}'"
                return await _exec_period_scalar(offset_start, offset_end)

        results = await asyncio.gather(*[_query_one(i) for i in range(n_periods)])
        return [v for v in results if v is not None]

    def _period_sql(start: str, end: str) -> str:
        """Build the per-period query for one time window.

        Bug-8570: when the KPI carries a semi-additive reduction, the period
        query must REDUCE (closing/average/min/max value per ``at_grain``
        bucket) instead of summing every row in the window. The compiler's
        semi-additive builder emits exactly that shape — a plain subquery, no
        CTE and no window function, so the query-router can still bind the
        semantic measure names — and ``CompilerContext.where_clause`` injects
        the period window into its innermost FROM scope.

        L7B-01: the dispatch mirrors ``compile_expression``'s own — the
        semi-additive branch on ``non_additive_agg or at_grain``, then the
        ``elif ctx.carry_forward`` branch that routes a carry-forward-only KPI
        through the same bucketed builder. Testing only the first two here made
        the per-period query answer a different question from the scalar the
        same compiler serves for the same KPI without time intelligence.

        Boundary of the fill on THIS path: the period window is injected as
        ``where_clause``, i.e. INSIDE the fill's own FROM scope, so the fill
        orders over the rows of one period only. It can carry forward from that
        window's first non-NULL bucket onwards and can never reach into the
        preceding period; a leading NULL bucket therefore stays NULL. That is
        the correct reading for a decomposed KPI — each period is an
        independently evaluated scalar — but it does mean the fill cannot
        rescue a period that is empty end to end (there is no row to carry
        into; see Bug-9481's sibling limitation for absent periods).
        """
        where_parts = [f"{time_col} >= {start}", f"{time_col} < {end}"]
        if filter_where_clause:
            where_parts.append(filter_where_clause)
        where = " AND ".join(where_parts)
        if non_additive_agg or at_grain or carry_forward:
            reduced_ctx = CompilerContext(
                model_slug=model_slug,
                calc_agg_mode=calc_agg_mode,
                default_agg="sum",
                measure_aggs=measure_aggs,
                time_column=time_column,
                at_grain=at_grain,
                non_additive_agg=non_additive_agg,
                carry_forward=carry_forward,
                where_clause=where,
            )
            return compile_expression(base_expression, reduced_ctx).sql
        model = _safe_ident(model_slug)
        return f"SELECT {select_expr} AS value FROM {model} WHERE {where}"

    async def _exec_period_scalar(start: str, end: str) -> float | None:
        """Execute the per-period aggregation over a specific time window."""
        return await _exec_scalar(_period_sql(start, end))

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
            ptd_start, end_sql,
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
    security_sink: set[str] | None = None,
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
        result = await _execute_via_router(
            model_id, sql, bearer, persona_id=persona_id,
            security_sink=security_sink,
        )
    except Exception as exc:
        # Bug-6663: surface query failures instead of silently returning
        # None (which conflates QUERY FAILURE with NO DATA for measure-based
        # targets). Raise so the caller can label the KPI "Evaluation failed".
        log.warning("Measure query for %s failed: %s", measure_name, exc)
        raise
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
    return None


# ---- Evaluation helpers ----


_MODEL_NOT_DEPLOYED_LABEL = (
    "Model is not deployed — deploy the model before evaluating KPIs"
)


class _ModelNotDeployedError(RuntimeError):
    """Internal signal preserving the router's undeployed-model refusal."""


def _is_model_not_deployed_error(exc: BaseException) -> bool:
    return isinstance(exc, _ModelNotDeployedError) or "not deployed" in str(exc).lower()


def _raise_if_model_not_deployed(exc: BaseException) -> None:
    if _is_model_not_deployed_error(exc):
        if isinstance(exc, _ModelNotDeployedError):
            raise exc
        raise _ModelNotDeployedError(_MODEL_NOT_DEPLOYED_LABEL) from exc


async def _batch_get_measure_values(
    model_id: UUID,
    measure_names: list[str],
    bearer: str,
    model_slug: str,
    measure_map: dict[str, "Measure"],
    persona_id: str | None = None,
    security_sink: set[str] | None = None,
    where_clause: str | None = None,
) -> dict[str, float | None]:
    """Fetch multiple measure values in a single query-router call.

    Builds a SELECT with one aggregate column per measure:
      SELECT SUM("Revenue") AS m0, AVG("Cost") AS m1 FROM "Model"

    Bug-8575: *where_clause* applies the KPI's business-definition scope
    (filters + time window) so the Python fallback answers the SAME filtered
    question as the SQL path. Without it, a KPI filtered to "EMEA" is answered
    with the unfiltered model-wide total, which is silently wrong.

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
    if where_clause:
        sql += f" WHERE {where_clause}"
    result_map: dict[str, float | None] = {n: None for n in measure_names}

    try:
        result = await _execute_via_router(
            model_id, sql, bearer, persona_id=persona_id,
            security_sink=security_sink,
        )
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
        _raise_if_model_not_deployed(exc)
        log.warning("Batch measure query failed, falling back to individual: %s", exc)
        # Fall back to individual queries
        for name in measure_names:
            m = measure_map.get(name)
            agg = (m.default_agg or "sum") if m else "sum"
            try:
                result_map[name] = await _get_measure_value(
                    model_id, name, bearer, model_slug, agg,
                    persona_id=persona_id,
                    where_clause=where_clause,
                    security_sink=security_sink,
                )
            except Exception as exc:  # noqa: BLE001 -- Bug-6663: individual failures degrade to None
                _raise_if_model_not_deployed(exc)
                result_map[name] = None

    return result_map


# Sentinel value indicating the SQL compiler cannot handle this expression
_COMPILER_UNSUPPORTED = object()
# Sentinel indicating the SQL executed but failed (router/SQL error, not "no data")
_EVALUATION_ERROR = object()
# Bug-8568 (deep-review R6 finding 7). ``_EVALUATION_ERROR`` was overloaded:
# it meant BOTH "a correctness guard refused to serve this KPI" and "the
# router call raised" (a timeout, a 5xx, or the row-security 403). Those two
# need OPPOSITE dispositions, and conflating them cost the ad-hoc endpoint its
# Bug-8449 deny-all branch and made a transient timeout read as "check your
# expression". A refusal is deterministic and about the DEFINITION; an
# execution failure is transient and about the RUN. This sentinel is the
# first; ``_EVALUATION_ERROR`` keeps the second. Consumers treat them alike
# via ``_is_evaluation_failure`` except where the difference is the point.
_GUARD_REFUSED = object()
# Bug-8487: sentinel indicating the query-router refused because the model
# has no deployed snapshot. All three evaluate endpoints must surface this
# consistently rather than masking it as "no data" or a generic failure.
_MODEL_NOT_DEPLOYED = object()

def _is_evaluation_failure(value: object) -> bool:
    """True for any 'this KPI produced no value' sentinel."""
    return value in (_EVALUATION_ERROR, _GUARD_REFUSED, _MODEL_NOT_DEPLOYED)
# Sentinel indicating a time-intelligence expression with no time dimension
# bound -- the KPI needs a time dimension before it can evaluate.
_TI_NO_TIME_DIMENSION = object()

_TI_NO_TIME_DIMENSION_LABEL = (
    "Evaluation failed — this time-intelligence KPI needs a time dimension"
)

# Bug-8569 [wrong numbers]. The VALUE leg passed ~25 arguments to
# ``_evaluate_expression_via_sql``; the TARGET leg at the same three endpoints
# passed six. Value and target were therefore computed under DIFFERENT rules and
# the verdict beside a correct headline number was wrong — a semi-additive
# closing balance of 90 compared against a target SUM of 600 reads 15%
# attainment instead of 50%, an April fiscal year compared its
# fiscal-period-to-date target against a January window, and a "last 90 days"
# value was compared against a target over the last calendar period.
#
# The fix is structural: each site builds ONE kwargs block for the value leg and
# derives the target leg from it by removing only the keys that describe the
# VALUE EXPRESSION'S OWN shape. Those must never be reused, because the target
# is a DIFFERENT expression: ``ti_type`` + ``base_expression`` would make the
# decomposed branch evaluate the value's base expression and return it as the
# target, and ``share_*`` would rank the target as if it were the share KPI.
# The target's own time intelligence is derived from the target expression
# itself by ``derive_ti_decomposition``.
_VALUE_ONLY_LEG_KEYS = frozenset({
    "ti_type",
    "ti_grain",
    "ti_n_periods",
    "base_expression",
    "share_type",
    "share_dimension",
    "share_n",
    "enable_ti_subquery",
})


def _target_leg_kwargs(value_leg_kwargs: dict) -> dict:
    """Derive the TARGET leg's evaluation context from the VALUE leg's (Bug-8569).

    Everything that describes the SLICE and the REDUCTION rules is shared, so
    both legs answer the same question; only the value expression's own
    decomposition/share metadata is dropped.
    """
    return {
        k: v for k, v in value_leg_kwargs.items()
        if k not in _VALUE_ONLY_LEG_KEYS
    }


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


def _apply_composite_result(
    resp: "KPIEvaluateResponse",
    result: "CompositeResult | None",
    security_sink: set[str] | None = None,
) -> "KPIEvaluateResponse":
    """Apply composite health and governance outcomes to one response."""
    resp = _apply_composite_signal(resp, result)
    if result is not None and result.status == COMPOSITE_STATUS_RESTRICTED:
        if security_sink is None:
            security_sink = set()
        security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
    return _stamp_row_security_restriction(resp, security_sink)


# Bug-7219: kpi_latest helpers extracted to kpi_latest.py
from shared.db.kpi_eval_generation import (  # noqa: E402
    allocate_kpi_eval_marker,
    clamp_supplied_marker,
)
from shared.db.kpi_latest_write import KpiPublishOutcome  # noqa: E402
from src.api.kpi_latest import (  # noqa: E402
    _kpi_latest_value_tuple,  # noqa: F401 - re-export kept for `from src.api.kpis import X` callers
    _STATUS_LABEL_MAX_LEN,  # noqa: F401 - re-export; tests/test_kpi_latest_upsert.py imports it
    _truncate_status_label,  # noqa: F401 - re-export; tests/test_kpi_latest_upsert.py imports it
    _upsert_kpi_latest_batch,
)


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
    security_sink: set[str] | None = None,
) -> float | object | None:
    """Compile a KPI expression to SQL and execute via the query-router.

    When *sql_sink* is provided, every SQL statement sent to the query gateway
    is appended to it (in execution order) so callers can surface the exact
    SQL — used by the builder's "Show SQL" preview.

    When *security_sink* is provided, the row-security rule ids the router
    applied are added to it (Bug-8449), so a caller can tell a value of ``None``
    caused by a row-security DENIAL from one caused by an empty result set.

    Returns the scalar float result, None if no data, or _COMPILER_UNSUPPORTED
    if the expression contains features that can't be compiled to SQL
    (KPI cross-references inside time functions).
    """
    # Bug-6252 (deep-review R4 finding 1) -- ONE invariant, enforced ONCE.
    #
    # A KPI carrying at_grain / non_additive_agg must NEVER reach the Python
    # evaluator. That evaluator applies no at_grain bucketing and no
    # semi-additive reduction at all: ``_get_measure_value`` emits a bare
    # ``SELECT SUM("balance") FROM "<model>"``, so a daily-balance KPI over
    # 100/120/90 reports 310 instead of the 90 closing balance -- and without
    # the time window, since ``_build_measure_provider`` passes no
    # where_clause. On a pct_change shape the divergence even flips sign
    # (-10% becomes +3.3%).
    #
    # R3 fixed this at ONE exit (the decomposed-TI branch). There are THREE:
    # a nested TI expression returns _COMPILER_UNSUPPORTED before that branch
    # is reached, and a kpi() cross-reference compiles WITH the semi-additive
    # context and then throws the compiled SQL away. Guarding exits one at a
    # time is how this bug has now been declared closed three times, so the
    # rule lives here instead: every route to the Python evaluator goes
    # through ``_python_fallback()``, which refuses when a reduction was
    # requested.
    #
    # Reducing correctly on those paths is the right product answer and is
    # filed separately; it is a real change to TI/kpi-ref evaluation
    # semantics and must not be improvised inside a correctness fix.
    # Bug-9486: carry_forward is the third reduction field (with at_grain /
    # non_additive_agg). Omitting it let carry_forward-only nested TI / kpi-ref
    # fall through to a bare model-wide SUM.
    _semi_additive_requested = bool(non_additive_agg or at_grain or carry_forward)

    def _python_fallback(reason: str):
        """Hand off to the Python evaluator, or refuse if it cannot be correct."""
        if _semi_additive_requested:
            log.error(
                "KPI needs the Python evaluator (%s) but carries a semi-additive "
                "reduction (at_grain=%s, non_additive_agg=%s, carry_forward=%s), "
                "which that evaluator cannot apply; failing the KPI closed rather "
                "than serving an un-reduced model-wide SUM",
                reason, at_grain, non_additive_agg, carry_forward,
            )
            return _GUARD_REFUSED
        return _COMPILER_UNSUPPORTED

    # Wizard/formula path (no business definition): derive the same TI
    # decomposition metadata business-builder KPIs carry, so both paths
    # share the decomposed evaluation machine (F-017-01). The legacy
    # window-over-aggregate SQL emission cannot be bound by the router.
    if not ti_type:
        derived_ti = derive_ti_decomposition(expression)
        if derived_ti is not None:
            if not time_column:
                return _TI_NO_TIME_DIMENSION
            ti_type = derived_ti.ti_type
            ti_grain = derived_ti.ti_grain
            ti_n_periods = derived_ti.ti_n_periods
            base_expression = derived_ti.base_expression
        elif expression_has_time_intelligence(expression):
            # Nested TI (e.g. pct_change(...) * 100) — the SQL compiler
            # would emit window-over-aggregate SQL the router cannot bind.
            # Route to the Python pipeline, whose provider TI hook resolves
            # each TI subtree via decomposed queries.
            return _python_fallback("nested time intelligence")

    # Decomposed TI evaluation: the query-router cannot resolve semantic
    # measure names inside CTEs/window-functions.  For TI types that would
    # generate complex SQL, decompose into simple router-friendly queries.
    if ti_type and ti_type in _TI_CTE_TYPES and base_expression:
        # Bug-6252 (deep-review R3 finding 2) / Bug-8570 [wrong numbers].
        #
        # This DECOMPOSED-TI branch is taken BEFORE the semi-additive
        # CompilerContext is built below. ``_evaluate_ti_decomposed`` used to
        # receive neither ``at_grain`` nor ``non_additive_agg``, so each
        # per-period query was a plain SUM over every row in the period with no
        # reduction at all: for daily balances 100/120/90 a month contributed
        # 310 instead of the 90 closing balance. Bug-6252 fail-closed that
        # combination, which stopped the wrong number but denied an ordinary
        # finance shape (a trailing-3-month view of a closing balance).
        #
        # Bug-8570 supplies the product-correct answer instead of the refusal:
        # the reduction is threaded INTO each per-period query, so every period
        # reduces to its closing/average value BEFORE the time-intelligence
        # arithmetic. The emitted per-period SQL is the compiler's own
        # semi-additive subquery — the same shape already served for a
        # non-TI semi-additive KPI, and router-bindable for the same reason.
        #
        # L7B-01: the reduction is THREE fields, not two. Bug-8570 threaded
        # at_grain and non_additive_agg and left carry_forward behind, so the
        # shape it un-refused evaluated under a configuration the modeller did
        # not author. All three travel together from here; anything added to
        # the semi-additive vocabulary must be added at this call site, in
        # ``_period_sql``'s dispatch, and in ``reduced_ctx`` together.
        #
        # Bug-7228: guard against missing time column on the business-builder
        # path, matching the derived-TI guard above.  Without this, the
        # evaluator falls through to a hardcoded "date" column default which
        # silently produces wrong numbers or a cryptic error.
        if not time_column:
            return _TI_NO_TIME_DIMENSION
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
                at_grain=at_grain,
                non_additive_agg=non_additive_agg,
                carry_forward=carry_forward,
                sql_sink=sql_sink,
                timeout_s=timeout_s,
                security_sink=security_sink,
            )
            return result
        except KPIUnsupportedAggregationError:
            # Bug-8570: the reduction itself is unusable (an unrecognised
            # reducer, a non-keyword at_grain, or no bound time column). Fail
            # the KPI closed — never fall through to the un-reduced per-period
            # SUM this threading exists to replace.
            log.error(
                "KPI time intelligence (%s) carries an unusable semi-additive "
                "reduction (at_grain=%s, non_additive_agg=%s); refusing to "
                "serve rather than returning an un-reduced per-period sum",
                ti_type, at_grain, non_additive_agg,
            )
            return _GUARD_REFUSED
        except Exception as exc:
            if _is_model_not_deployed_error(exc):
                return _MODEL_NOT_DEPLOYED
            # Bug-7225: both derived AND business-builder TI must fail loud
            # when the decomposed evaluation fails.  The CTE compiler
            # fall-through uses different window semantics (includes partial
            # current period, anchors on DATE_TRUNC buckets instead of
            # offset intervals), so a silent switch changes the KPI's
            # number — a wrong-number-on-fallback defect.
            log.error(
                "TI decomposed evaluation failed (%s %s n=%s): %s",
                ti_type, ti_grain, ti_n_periods, exc,
            )
            return _TIDecompositionFailed(str(exc))


    # Bug-6252 (deep-review R6 finding 1) -- REGRESSION GUARD, and it is mine.
    #
    # R5 widened the compiler's semi-additive dispatch from
    # ``non_additive_agg AND at_grain`` to OR so a half-configured KPI could
    # not fall through to an un-reduced SUM. That was right for the shape it
    # targeted and WRONG for this one: the share/rank branch is the very next
    # ``elif`` in that chain (kpi_compiler.py), so a share-of-total KPI that
    # also carries ``at_grain`` is now captured by the semi-additive branch
    # before share/rank is ever reached. A share KPI over region with
    # at_grain='day' went from the correct 220/310 = 71% to a raw
    # semi-additive scalar of 90 -- rendered as a percentage, nonsense.
    #
    # So my R5 claim that "there is no input for which the old AND gave a
    # better answer" was false: for THIS combination AND was right.
    #
    # The fix is NOT to reorder the elif chain -- picking either branch
    # silently answers a question the modeller did not ask. Two mutually
    # exclusive reductions of the same rows were requested and only one can
    # be applied, so refuse, exactly as the decomposed-TI path above refuses
    # the same clash.
    #
    # Deep-review R7 finding 2: aggregate-of-aggregate is the FIRST branch
    # of that same chain and swallows the reduction identically
    # (inner=sum/outer=avg at inner_grain=month drops both at_grain and
    # 'last'), and unlike at_grain it IS settable in the shipped KPI
    # wizard. Guarding only share/rank would have been asymmetric against
    # the very invariant stated above.
    if (non_additive_agg or at_grain) and (
        share_type or (inner_agg and outer_agg)
    ):
        log.error(
            "KPI combines a second reduction (share_type=%s dimension=%s, "
            "or aggregate-of-aggregate inner=%s outer=%s) with a "
            "semi-additive grain (at_grain=%s, non_additive_agg=%s). These "
            "are two different reductions of the same rows and only one "
            "can be applied; failing the KPI closed rather than silently "
            "serving whichever branch of the compiler chain wins",
            share_type, share_dimension, inner_agg, outer_agg,
            at_grain, non_additive_agg,
        )
        return _GUARD_REFUSED
    # Bug-9481: carry_forward × share/rank has no defined fill vocabulary.
    # (carry_forward × agg-of-agg remains supported via _build_agg_of_agg_sql.)
    if carry_forward and share_type:
        log.error(
            "KPI combines carry_forward with share/rank (share_type=%s "
            "dimension=%s); refuse rather than silently drop the fill",
            share_type, share_dimension,
        )
        return _GUARD_REFUSED

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
    except KPIUnsupportedAggregationError:
        # Bug-6252 (deep-review finding 2): MUST precede the generic catch.
        # Downgrading this to _COMPILER_UNSUPPORTED sends the KPI to the Python
        # evaluator, which applies NO at_grain bucketing and NO semi-additive
        # reduction — so an unsupported reducer would still serve the plain
        # model-wide SUM (the sum of every day's balance instead of the closing
        # balance), silently, with a legitimate-looking number in the cell.
        # Fail the KPI closed instead, exactly as the sibling
        # ``has_ungrouped_window`` check below does for the same reason.
        # Bug-8573/Bug-9233: ``KPITimeContextError`` subclasses this type, so an
        # unresolvable time column or a non-keyword ``at_grain`` lands here too
        # and gets the same fail-closed disposition — never the Python evaluator,
        # which would serve the un-reduced model-wide SUM.
        log.error(
            "KPI expression carries an unsupported semi-additive aggregation, "
            "an unusable at_grain, or no bound time dimension; refusing to "
            "serve rather than falling back to an un-reduced sum",
            exc_info=True,
        )
        return _GUARD_REFUSED
    except Exception:
        return _python_fallback("expression could not be compiled")

    # KPI cross-refs can't be compiled to SQL — fall back to Python evaluator
    if compiled.kpi_names:
        return _python_fallback("expression references another KPI")

    # F-017-22: share_of_total()/rank_over() in a plain expression (no grouped
    # share CTE) degenerate to share 1 / rank 1 over a single ungrouped row.
    # Fail closed rather than serve a silently-wrong number — these belong in
    # the builder's share_rank family (grouped CTE path).
    if compiled.has_ungrouped_window:
        log.warning(
            "share_of_total/rank_over used outside a grouped share context; "
            "failing closed (would yield degenerate single-row window).",
        )
        return _GUARD_REFUSED

    sql = compiled.sql
    if sql_sink is not None:
        sql_sink.append(sql)

    try:
        result = await _execute_via_router(
            model_id, sql, bearer, timeout_s=timeout_s, persona_id=persona_id,
            security_sink=security_sink,
        )
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
                return None  # all values were None
            elif isinstance(row, (list, tuple)) and row:
                return float(row[0]) if row[0] is not None else None
        cells = result.get("cells", [])
        if cells:
            cell = cells[0]
            if isinstance(cell, dict):
                return float(cell.get("value", 0))
            return float(cell) if cell is not None else None
    except Exception as exc:
        # Bug-8487: detect the router's "Model is not deployed" refusal so
        # callers can surface a clear message instead of a generic failure.
        if _is_model_not_deployed_error(exc):
            log.info(
                "KPI evaluation failed — model not deployed: %.200s",
                expression,
            )
            return _MODEL_NOT_DEPLOYED
        log.warning(
            "KPI SQL evaluation failed — expression: %.200s | SQL: %.400s | error: %s",
            expression, sql, exc,
        )
        return _EVALUATION_ERROR
    return None


def _prior_period_target_expression(kpi: KPI) -> str | None:
    """Bug-6251 (F-017-04): synthesize the ``prior_period(...)`` target
    expression for a ``prior_period`` target when the client did not persist an
    explicit target_expression.

    The wizard already emits ``prior_period(measure(...), "grain")`` as
    target_expression, so both SQL and Python paths resolve it there. This
    fallback only fires for API-created KPIs that set target_type='prior_period'
    without a target_expression: wrap the KPI's own expression so the target is
    "the prior-period value of this KPI", resolved through the same decomposed
    time-intelligence machinery. Returns None when there is nothing to wrap.
    """
    if kpi.target_type != "prior_period" or not kpi.expression:
        return None
    # Allow-list the grain: it is interpolated into the DSL string, so an
    # arbitrary/free-form target_period (API-created KPIs are not constrained by
    # the wizard) could otherwise inject quotes or an unknown grain that makes
    # the synthesized target silently unparseable. Fall back to a safe default.
    grain = kpi.target_period or kpi.trend_period or "month"
    if grain not in _TI_INTERVAL_MAP:
        grain = "month"
    return f'prior_period({kpi.expression}, "{grain}")'


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
        # Bug-6251: a prior_period target is carried as its prior_period(...)
        # expression; synthesize it when the client stored none so the Python
        # fallback pipeline (_resolve_target) resolves it via the provider's TI
        # hook instead of returning None.
        target_expression=kpi.target_expression or _prior_period_target_expression(kpi),
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
    security_sink: set[str] | None = None,
):
    """Build the provider hook that resolves a time-intelligence AST subtree
    via decomposed query-router queries (Python pipeline path for nested TI).

    Bug-8570 caller audit: ``_evaluate_ti_decomposed`` now also applies the KPI's
    semi-additive reduction (``at_grain`` / ``non_additive_agg``). This caller
    deliberately passes neither, because it is ONLY reachable through
    ``_python_fallback``, which returns ``_GUARD_REFUSED`` — never
    ``_COMPILER_UNSUPPORTED`` — whenever a reduction was requested. The Python
    pipeline cannot apply a reduction OUTSIDE the TI subtree either, so a
    reduction-carrying KPI must not reach this hook at all; passing the fields
    here would be unreachable code that suggested otherwise.
    """

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
                security_sink=security_sink,
            )
        except Exception as exc:
            _raise_if_model_not_deployed(exc)
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
    kpi_value_security_rules: dict[str, set[str]] | None = None,
    measure_value_cache: dict[str, float | None] | None = None,
    measure_value_security_rules: set[str] | None = None,
    persona_id: str | None = None,
    ti_evaluator=None,
    security_sink: set[str] | None = None,
    where_clause: str | None = None,
) -> MeasureValueProvider:
    """Build a MeasureValueProvider that resolves measures via the query-router.

    If *kpi_value_security_rules* is provided, restriction metadata follows
    each cached ``kpi()`` value into the KPI that consumes it. If
    *measure_value_cache* is provided, values are served from it (batch
    pre-fetched) instead of issuing individual query-router calls.
    *measure_value_security_rules* carries the governance metadata returned by
    the query that populated *measure_value_cache*. It is absorbed only when a
    cached measure is actually consumed, so a literal KPI in the same batch is
    not incorrectly marked restricted. *ti_evaluator* (from
    _build_ti_evaluator) resolves time-intelligence subtrees in the Python
    pipeline via decomposed router queries. Bug-8575: *where_clause* is
    threaded through to on-miss individual ``_get_measure_value`` calls so the
    fallback answers the same filtered question as the SQL path.

    Bug-8682: the slice is read from ``provider.measure_where_clause`` at CALL
    time rather than captured at construction, because all five
    ``_evaluate_single_kpi`` call sites build the provider BEFORE the per-KPI
    business-definition WHERE has been resolved. ``_evaluate_single_kpi`` sets
    the attribute as soon as it knows the scope; callers that already know it
    keep passing *where_clause* and get exactly the same behaviour.
    """
    provider = MeasureValueProvider(measure_where_clause=where_clause)

    async def get_measure_value(name: str) -> float | None:
        if measure_value_cache is not None and name in measure_value_cache:
            if security_sink is not None and measure_value_security_rules:
                security_sink.update(measure_value_security_rules)
            return measure_value_cache[name]
        m = measure_map.get(name)
        agg = m.default_agg if m else "sum"
        try:
            return await _get_measure_value(
                model_id, name, bearer, model_slug, agg, persona_id=persona_id,
                where_clause=provider.measure_where_clause,
                security_sink=security_sink,
            )
        except Exception as exc:
            _raise_if_model_not_deployed(exc)
            # Bug-6897: the Python-evaluator pipeline calls this provider and
            # has no per-measure exception handling.  Degrade to None (no-data)
            # so one broken measure does not 500 the entire evaluation.  The
            # direct callers of _get_measure_value handle errors with labelled
            # responses; this provider path cannot surface that label, so None
            # is the safest fallback.
            return None

    async def get_kpi_value(name: str) -> float | None:
        if kpi_value_cache is None:
            return None
        if security_sink is not None and kpi_value_security_rules:
            security_sink.update(kpi_value_security_rules.get(name, set()))
        return kpi_value_cache.get(name)

    provider.get_measure_value = get_measure_value
    provider.get_kpi_value = get_kpi_value if kpi_value_cache is not None else None
    provider.evaluate_time_intelligence = ti_evaluator
    return provider


_PYTHON_FALLBACK_REASON = (
    "The SQL compiler could not evaluate this expression (time intelligence or "
    "KPI references), so the model-service Python evaluator produced the value."
)


def _result_to_response(
    result,
    *,
    evaluation_path: str = "python",
    fallback_reason: str | None = _PYTHON_FALLBACK_REASON,
) -> KPIEvaluateResponse:
    """Convert an EvaluationResult to a Pydantic response.

    Bug-5922: includes ``status_position`` and ``status_bands`` so the
    Python fallback path returns the same authoritative visual scale fields
    as the normal SQL evaluation path.

    F-017-09: every caller of this converter is a Python-evaluator fallback
    (the SQL compiler returned _COMPILER_UNSUPPORTED), so the response is marked
    ``evaluation_path="python"`` with a human-readable ``fallback_reason``. The
    SQL success path builds its response through ``_finalize_kpi_response`` /
    direct construction, which default to ``evaluation_path="sql"``.
    """
    return KPIEvaluateResponse(
        evaluation_path=evaluation_path,
        fallback_reason=fallback_reason,
        kpi_id=result.kpi_id,
        value=result.value,
        value_str=result.value_str,
        target=result.target,
        status=result.status,
        status_label=result.status_label,
        status_color=result.status_color,
        status_position=result.status_position,
        status_bands=result.status_bands,
        trend=result.trend,
        trend_label=result.trend_label,
        trend_pct=result.trend_pct,
        trend_pct_normalised=result.trend_pct_normalised,  # Bug-7238
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
    security_sink: set[str] | None = None,
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
            security_sink=security_sink,
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
    at_grain: str | None = None,
    non_additive_agg: str | None = None,
    carry_forward: bool = False,
    sql_sink: list[str] | None = None,
    timeout_s: float = 30.0,
    security_sink: set[str] | None = None,
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
    a semi-additive reduction is requested (Bug-8570 — see below), or the query
    fails. All execution flows through the gateway.
    """
    if not time_column:
        return None

    # Bug-8570 [wrong numbers]: this builder emits ONE grouped SELECT per
    # period. A semi-additive KPI needs a per-period REDUCTION (the closing /
    # average value inside each bucket) before the per-period value exists, and
    # for first/last that needs a window function the query-router cannot bind.
    # Rendering the un-reduced per-period SUM would draw a sparkline that
    # contradicts the scalar the same KPI serves — the sum of every day's
    # balance next to the closing balance. No sparkline is honest; a wrong one
    # is not.
    #
    # L7B-01: ``carry_forward`` belongs in this gate for the same reason and was
    # missing from it. Since the compiler routes a carry-forward-only KPI
    # through the bucketed builder (Bug-9482), the preview SCALAR for that shape
    # is a per-bucket reduction while this builder would draw un-reduced
    # per-period sums beside it — the same contradiction, arrived at from the
    # third field of the same reduction instead of the first two.
    if non_additive_agg or at_grain or carry_forward:
        log.info(
            "Ad-hoc sparkline skipped: a semi-additive reduction (at_grain=%s, "
            "non_additive_agg=%s, carry_forward=%s) cannot be applied per "
            "period in one grouped SELECT; returning no series rather than "
            "un-reduced per-period sums",
            at_grain, non_additive_agg, carry_forward,
        )
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
            security_sink=security_sink,
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

    F-017-09: every caller of this helper is a SQL-compiler success path, so the
    response keeps the KPIEvaluateResponse default ``evaluation_path="sql"``. The
    Python fallback builds its response through ``_result_to_response`` instead,
    which stamps ``evaluation_path="python"``.

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
    # Bug-7240: extract the colorblind preference so default preset selection
    # uses the accessible palette instead of the standard red/amber/green.
    _colorblind = bool(meta.get("colorblind", False))
    peer_values = None
    if eval_type == "percentile_rank" and value is not None and bearer and model_slug:
        # Bug-8449: no security_sink is threaded here on purpose. This call is
        # gated on ``value is not None``, so a deny-all evaluation (which yields
        # None) can never reach it — there is no denial for a sink to report.
        # ``_resolve_peer_values`` still ACCEPTS a sink so the enumeration guard
        # in tests/test_kpi_row_security_restricted_8449.py stays exhaustive and
        # a future caller on a null-value path can supply one.
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
            colorblind=_colorblind,  # Bug-7240
        )
    elif eval_type in ("z_score", "percentile_rank") and value is not None:
        th = evaluate_threshold(
            value=value, target=target, direction=ctx.direction,
            evaluation_type=eval_type,
            historical_values=historical_values,
            peer_values=peer_values,
            colorblind=_colorblind,  # Bug-7240
        )
    elif value is not None and target is not None:
        # Bug-1226: thread the evaluation_type so variance KPIs without custom
        # bands score on their own deviation-ordered default preset (F-017-02),
        # not the percentage_of_target default. Otherwise the badge would read
        # the variance label while the position/bands carried a different scale.
        th = evaluate_threshold(
            value=value, target=target, direction=ctx.direction,
            evaluation_type=eval_type,
            colorblind=_colorblind,  # Bug-7240
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

    # Bug-6663: when a target measure query FAILED (not "no data"), surface
    # a clear label so the user sees the problem rather than silently losing
    # the threshold comparison as if no target were configured.
    if ctx.target_query_failed and status_label is None:
        status_label = "Target query failed"

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
        trend_pct_normalised=tr.trend_pct_normalised,  # Bug-7238
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
    dim_scope: dict | None = None,
    _path: frozenset[str] = frozenset(),
    security_sink: set[str] | None = None,
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
        # F-017-01 (Fable R1): composite children on a DEPLOYED model must use
        # the deployed-snapshot definition so an undeployed edit to a child never
        # silently changes the parent's served score. Resolve each child through
        # the deployed-snapshot authority (Withheld children are excluded — they
        # were never part of the deployed version). Undeployed models keep the
        # live draft (builder-only).
        if getattr(model, "deployed_version_id", None) is not None:
            _child_resolved = await resolve_served_kpi(db, model, child)
            if isinstance(_child_resolved, ResolvedKpi):
                child = _child_resolved.kpi
            elif isinstance(_child_resolved, Withheld):
                continue  # child not in deployed version
            # Undeployed returns won't happen here (model is deployed); snapshot
            # invalid is already raised upstream by the parent's resolve.
        if not _kpi_visible_to_persona(
            child, allowed_measure_ids, name_to_id or {},
            dim_scope=dim_scope,
        ):
            continue
        children_kpis.append(child)
    if not children_kpis:
        return CompositeResult(composite_score=None)

    eval_cache: dict[str, dict] = {}
    for child in children_kpis:
        child_security_rules: set[str] = set()
        provider = _build_measure_provider(
            model_id, model_slug, bearer, measure_map,
            persona_id=effective_persona_id,
            security_sink=child_security_rules,
        )
        resp = await _evaluate_single_kpi(
            child, db, model_id, model_slug, bearer, provider,
            measure_map=measure_map,
            calendar_type=_derive_calendar_type(model),
            fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
            persona_id=effective_persona_id,
            security_sink=child_security_rules,
            model=model,
            is_privileged=is_privileged,
            allowed_measure_ids=allowed_measure_ids,
            name_to_id=name_to_id,
            dim_scope=dim_scope,
        )
        child_restricted = bool(
            getattr(resp, "row_security_restricted", False)
            or row_security_denied_all(child_security_rules)
        )
        child_value = resp.value
        # Bug-4255: an errored leaf child (failed evaluation, null value) is
        # recorded distinctly so the parent flags it instead of treating it
        # as silent no-data.
        child_error = _child_error_reason(resp)
        if child.kpi_type == "composite":
            # ``_evaluate_single_kpi`` skips the composite's non-authoritative
            # placeholder expression, so its sink/response records only the
            # composite's OWN target governance. Preserve that verdict while
            # recursively evaluating the nested composite's children.
            target_restricted = child_restricted
            # Nested composite: its score (not the placeholder expression
            # value _evaluate_single_kpi just produced) is the raw value. If
            # the nested composite itself errored or tripped the cycle/depth
            # guard, that becomes this child's error reason — the parent stays
            # scoreable from its other children rather than the whole tree
            # collapsing.
            # Bug-8449 (round-2 deep review, finding 2): a nested composite's
            # OWN recursion decides whether it is restricted. The ``resp`` above
            # came from evaluating the nested composite's PLACEHOLDER expression,
            # so counting that would depend on whether the placeholder happened
            # to reach the router. Give the recursion its own sink and fold the
            # verdict in explicitly.
            _nested_sink: set[str] = set()
            try:
                nested = await _evaluate_composite_score(
                    child, db, model_id, model_slug, bearer, measure_map,
                    model=model,
                    is_privileged=is_privileged,
                    effective_persona_id=effective_persona_id,
                    allowed_measure_ids=allowed_measure_ids,
                    name_to_id=name_to_id,
                    dim_scope=dim_scope,
                    _path=path,
                    security_sink=_nested_sink,
                )
            except CompositeEvaluationError as exc:
                child_value = None
                child_error = str(exc)
            else:
                child_restricted = target_restricted or (
                    nested.status == COMPOSITE_STATUS_RESTRICTED
                    or row_security_denied_all(_nested_sink)
                )
                child_value = nested.composite_score
                if nested.status == COMPOSITE_STATUS_ERROR:
                    child_error = child_error or "child composite errored"
        eval_cache[str(child.id)] = {
            "value": child_value,
            "target": resp.target,
            "error_reason": child_error,
            "restricted": child_restricted,
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
    if security_sink is not None and any(c.restricted for c in children):
        security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
    if not children:
        return CompositeResult(composite_score=None)
    method, bound_min, bound_max = get_normalisation_config(parent.presentation_meta)
    await _apply_snapshot_bounds_for_min_max(
        db, children, children_kpis, method, bound_min, bound_max,
    )
    return evaluate_composite(children, method, bound_min, bound_max)


def _kpi_dependency_expressions(kpi: KPI) -> tuple[str, ...]:
    """Return every expression tree that can reference another KPI."""
    return tuple(
        expression
        for expression in (
            getattr(kpi, "expression", None),
            getattr(kpi, "target_expression", None),
        )
        if expression
    )


def _kpi_reference_names(kpi: KPI) -> set[str]:
    """Extract direct ``kpi()`` references from value and target expressions."""
    names: set[str] = set()
    for expression in _kpi_dependency_expressions(kpi):
        try:
            names.update(extract_kpi_references(expression))
        except Exception:
            # Validation owns parse failures. Dependency maintenance must not
            # fail open by guessing at an invalid expression.
            continue
    return names


def _kpi_dependency_cache_version(
    kpi: KPI,
    kpis_by_name: dict[str, KPI],
    kpis_by_id: dict[UUID, KPI],
    definition_version: str | None,
) -> str:
    """Fingerprint a draft KPI and its transitive value/target dependencies.

    Local eviction handles the current replica. This fingerprint is read from
    the authoritative model rows on each cache admission, so another replica
    naturally misses a warm dependent after update, delete, or revert without
    relying on a cross-process invalidation event. Composite parent-to-child
    ownership is also a dependency edge: child scoring definitions affect the
    parent even when neither expression contains ``kpi()``.
    """
    pending = [kpi]
    visited: set[UUID] = set()
    components: list[dict[str, object]] = []
    children_by_parent: dict[UUID, list[KPI]] = {}
    for candidate in kpis_by_id.values():
        parent_kpi_id = getattr(candidate, "parent_kpi_id", None)
        if parent_kpi_id is not None:
            children_by_parent.setdefault(parent_kpi_id, []).append(candidate)

    while pending:
        current = pending.pop()
        current_id = getattr(current, "id", None)
        if current_id is None or current_id in visited:
            continue
        visited.add(current_id)
        updated_at = getattr(current, "updated_at", None)
        components.append({
            "id": str(current_id),
            "name": getattr(current, "name", None),
            "updated_at": updated_at.isoformat() if updated_at else None,
            "expression": getattr(current, "expression", None),
            "target_expression": getattr(current, "target_expression", None),
            "target_type": getattr(current, "target_type", None),
            "target_value": getattr(current, "target_value", None),
            "target_measure_id": str(getattr(current, "target_measure_id", "") or ""),
            "kpi_type": getattr(current, "kpi_type", None),
            "parent_kpi_id": str(getattr(current, "parent_kpi_id", "") or ""),
            "weight": getattr(current, "weight", None),
            "direction": getattr(current, "direction", None),
            "presentation_meta": getattr(current, "presentation_meta", None),
            "business_definition": getattr(current, "business_definition", None),
            "calc_agg_mode": getattr(current, "calc_agg_mode", None),
        })
        for reference_name in sorted(_kpi_reference_names(current)):
            referenced_kpi = kpis_by_name.get(reference_name)
            if referenced_kpi is None:
                # A deleted or as-yet-unresolved reference is value-affecting:
                # preserve that state in the fingerprint rather than reusing a
                # value calculated while the referenced KPI existed.
                components.append({
                    "id": None,
                    "name": reference_name,
                    "updated_at": None,
                    "expression": None,
                    "target_expression": None,
                })
            else:
                pending.append(referenced_kpi)
        pending.extend(children_by_parent.get(current_id, ()))

    canonical = json.dumps(
        sorted(components, key=lambda component: (str(component["id"]), str(component["name"]))),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{definition_version or '_'}:deps:{digest}"


def _dependent_kpi_ids(
    kpis: list[KPI],
    seed_kpi_id: UUID,
    seed_names: set[str],
    *,
    prior_parent_kpi_id: UUID | None = None,
) -> list[UUID]:
    """Return reverse closure through expression/target and ownership edges."""
    reverse_dependencies: dict[UUID, set[UUID]] = {}
    ids_by_name: dict[str, set[UUID]] = {}
    known_ids = {candidate.id for candidate in kpis}

    # Textual references resolve by name, so all names must be indexed before
    # adding reverse edges. Building both in one unordered pass loses an edge
    # whenever the referring KPI appears before its referent in the DB result.
    for candidate in kpis:
        ids_by_name.setdefault(candidate.name, set()).add(candidate.id)

    for candidate in kpis:
        for reference_name in _kpi_reference_names(candidate):
            for referenced_id in ids_by_name.get(reference_name, ()):
                reverse_dependencies.setdefault(referenced_id, set()).add(candidate.id)
            # Retain reverse invalidation through a rename/deletion where the
            # reference is no longer resolvable by its old name.
            if reference_name in seed_names:
                reverse_dependencies.setdefault(seed_kpi_id, set()).add(candidate.id)
        parent_kpi_id = getattr(candidate, "parent_kpi_id", None)
        if parent_kpi_id in known_ids:
            reverse_dependencies.setdefault(candidate.id, set()).add(parent_kpi_id)

    # Update/revert query after committing the new definition. Preserve the
    # prior ownership edge as well so a child reassignment evicts both its old
    # composite parent and every textual consumer of that parent.
    if prior_parent_kpi_id in known_ids:
        reverse_dependencies.setdefault(seed_kpi_id, set()).add(
            prior_parent_kpi_id,
        )

    pending = [seed_kpi_id]
    seen_ids = {seed_kpi_id}
    while pending:
        referenced_id = pending.pop()
        for dependent_id in reverse_dependencies.get(referenced_id, ()):
            if dependent_id not in seen_ids:
                seen_ids.add(dependent_id)
                pending.append(dependent_id)
    return sorted(seen_ids, key=str)


async def _invalidate_kpi_and_dependents(
    db,
    model_id: UUID,
    kpi_id: UUID,
    *names: str,
    prior_parent_kpi_id: UUID | None = None,
) -> None:
    """Evict one KPI and every transitive value/target dependent locally."""
    result = await db.execute(select(KPI).where(KPI.model_id == model_id))
    kpis = list(result.scalars().all())
    get_kpi_cache().invalidate_kpis(
        _dependent_kpi_ids(
            kpis,
            kpi_id,
            {name for name in names if name},
            prior_parent_kpi_id=prior_parent_kpi_id,
        ),
    )


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
    dim_scope: dict | None = None,
    _path: frozenset[str] = frozenset(),
    request_filter_predicates: list[str] | None = None,
    security_sink: set[str] | None = None,
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
        # F-017-01 (Fable R1): kpi() cross-references on a DEPLOYED model must
        # evaluate the deployed-snapshot definition, not a live edit.
        if getattr(model, "deployed_version_id", None) is not None:
            _ref_resolved = await resolve_served_kpi(db, model, ref_kpi)
            if isinstance(_ref_resolved, ResolvedKpi):
                ref_kpi = _ref_resolved.kpi
            elif isinstance(_ref_resolved, Withheld):
                cache[ref_name] = None
                continue
        if not _kpi_visible_to_persona(
            ref_kpi, allowed_measure_ids, name_to_id or {},
            dim_scope=dim_scope,
        ):
            cache[ref_name] = None
            continue
        if ref_kpi.kpi_type == "composite":
            try:
                # A composite's stored expression is a scoring placeholder, but
                # its target remains a governed expression. Resolve the union
                # before evaluating that target so a referenced composite cannot
                # publish a score while its own target is denied.
                reference_security_rules: set[str] = set()
                nested_kpi_values: dict[str, float | None] = {}
                for dependency_expression in _kpi_dependency_expressions(ref_kpi):
                    nested_kpi_values.update(
                        await _resolve_referenced_kpi_values(
                            dependency_expression, db, model_id, model_slug,
                            bearer, measure_map, model=model,
                            is_privileged=is_privileged, persona_id=persona_id,
                            allowed_measure_ids=allowed_measure_ids,
                            name_to_id=name_to_id, dim_scope=dim_scope,
                            _path=_path | {ref_name},
                            request_filter_predicates=request_filter_predicates,
                            security_sink=reference_security_rules,
                        )
                    )
                provider = _build_measure_provider(
                    model_id, model_slug, bearer, measure_map,
                    kpi_value_cache=nested_kpi_values or None,
                    persona_id=persona_id,
                    security_sink=reference_security_rules,
                )
                governed_response = await _evaluate_single_kpi(
                    ref_kpi, db, model_id, model_slug, bearer, provider,
                    measure_map=measure_map,
                    calendar_type=_derive_calendar_type(model),
                    fiscal_year_start_month=getattr(
                        model, "fiscal_year_start_month", None,
                    ),
                    persona_id=persona_id,
                    security_sink=reference_security_rules,
                    model=model,
                    is_privileged=is_privileged,
                    allowed_measure_ids=allowed_measure_ids,
                    name_to_id=name_to_id,
                    dim_scope=dim_scope,
                    request_filter_predicates=request_filter_predicates,
                )
                composite_security_rules: set[str] = set()
                composite_result = await _evaluate_composite_score(
                    ref_kpi, db, model_id, model_slug, bearer, measure_map,
                    model=model,
                    is_privileged=is_privileged,
                    effective_persona_id=persona_id,
                    allowed_measure_ids=allowed_measure_ids,
                    name_to_id=name_to_id,
                    dim_scope=dim_scope,
                    security_sink=composite_security_rules,
                )
                restricted = bool(
                    getattr(governed_response, "row_security_restricted", False)
                    or composite_result.status == COMPOSITE_STATUS_RESTRICTED
                    or row_security_denied_all(reference_security_rules)
                    or row_security_denied_all(composite_security_rules)
                )
                if restricted:
                    cache[ref_name] = None
                    if security_sink is not None:
                        security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
                else:
                    cache[ref_name] = composite_result.composite_score
            except CompositeEvaluationError:
                cache[ref_name] = None
            continue
        nested_kpi_values: dict[str, float | None] = {}
        # A referenced KPI's target is evaluated by the same Python pipeline as
        # its value.  Its provider must therefore include the transitive union
        # of both expression trees.  Resolving only ``expression`` made a
        # target-only descendant disappear and dropped any deny-all rule it
        # carried before the outer KPI could redact its result.
        for dependency_expression in _kpi_dependency_expressions(ref_kpi):
            nested_kpi_values.update(
                await _resolve_referenced_kpi_values(
                    dependency_expression, db, model_id, model_slug, bearer,
                    measure_map, model=model, is_privileged=is_privileged,
                    persona_id=persona_id, allowed_measure_ids=allowed_measure_ids,
                    name_to_id=name_to_id, dim_scope=dim_scope,
                    _path=_path | {ref_name},
                    request_filter_predicates=request_filter_predicates,
                    security_sink=security_sink,
                )
            )
        provider = _build_measure_provider(
            model_id, model_slug, bearer, measure_map,
            kpi_value_cache=nested_kpi_values or None,
            persona_id=persona_id,
            security_sink=security_sink,
        )
        resp = await _evaluate_single_kpi(
            ref_kpi, db, model_id, model_slug, bearer, provider,
            measure_map=measure_map,
            calendar_type=_derive_calendar_type(model),
            fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
            persona_id=persona_id,
            security_sink=security_sink,
            model=model,
            is_privileged=is_privileged,
                    allowed_measure_ids=allowed_measure_ids,
                    name_to_id=name_to_id,
                    dim_scope=dim_scope,
                    request_filter_predicates=request_filter_predicates,
                )
        # Bug-8449 (round-2 deep review, finding 2): a KPI whose expression is
        # ONLY ``kpi("...")`` references makes no direct router call of its own —
        # ``extract_measure_names`` finds nothing and ``_batch_get_measure_values``
        # early-returns — so the AST call-site guard structurally cannot see this
        # route. Propagate the REFERENCED KPI's restriction up instead, from its
        # response. Row security is per-(principal, model), so a restricted
        # referent means the parent is restricted too; it cannot false-positive a
        # sibling on a different policy.
        if security_sink is not None and getattr(
            resp, "row_security_restricted", False,
        ):
            security_sink.add(ROW_SECURITY_DENY_ALL_RULE_ID)
        cache[ref_name] = resp.value
    return cache


# ---- Evaluate endpoints ----


async def _kpi_outer_cache_allowed(db, model_id: UUID) -> bool:
    """Return whether the model-service KPI result cache is safe to use.

    Row-security identity is compiled in query-router from the caller's full
    roles, groups, claims, and current rule definitions. Reproducing that
    policy compiler in model-service would create a second security authority,
    and user-mapping rows can change outside the control plane. Bypass this
    outer cache whenever any enabled RLS rule exists; query-router retains its
    own principal/policy-aware cache.
    """
    result = await db.execute(
        select(RowSecurityRule.id).where(
            RowSecurityRule.model_id == model_id,
            RowSecurityRule.is_enabled.is_(True),
        ).limit(1)
    )
    return not bool(result.scalars().all())


def _batch_outer_cache_allowed_for_request(
    request_filters: list[dict] | None,
    effective_persona_id: str | None,
    model_cache_allowed: bool,
) -> bool:
    """Return whether an evaluate-batch request may use the outer cache."""
    return (
        not request_filters
        and effective_persona_id is None
        and model_cache_allowed
    )


def _batch_prefetch_allowed_for_request(request_filters: list[dict] | None) -> bool:
    """Model-wide measure prefetch is safe only for an unfiltered request."""
    return not request_filters


async def _evaluate_python_expression(
    expression: str,
    ctx: EvaluationContext,
    provider: MeasureValueProvider,
) -> float | None:
    """Evaluate one expression through the shared Python DSL evaluator."""
    expression_ctx = EvaluationContext(
        kpi_id=ctx.kpi_id,
        kpi_name=ctx.kpi_name,
        expression=expression,
        calc_agg_mode=ctx.calc_agg_mode,
    )
    return (await run_evaluation_pipeline(expression_ctx, provider)).value


def _kpi_cache_key_components(kpi: KPI) -> tuple[str | None, list[dict] | None, dict | None, str | None]:
    """Derive the (calc_agg_mode, filters, time_context, definition_version)
    components of the KPI evaluation cache key from the KPI's own definition.

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

    Bug-7242: ``definition_version`` (the KPI's ``updated_at`` timestamp)
    ensures that a definition change on one replica naturally misses caches
    on other replicas that read the new row, bounding cross-replica staleness
    to the DB read rather than the TTL.
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
    # Bug-7242: include the definition's updated_at as a version stamp
    updated_at = getattr(kpi, "updated_at", None)
    def_version = updated_at.isoformat() if updated_at else None
    return calc_agg_mode, filters, time_context, def_version


async def _single_kpi_cache_definition_version(
    db,
    model_id: UUID,
    kpi: KPI,
    served_definition_version: str | None,
    definition_version: str | None,
) -> str:
    """Return the version stamp for a single-KPI cache admission.

    A deployed snapshot version already pins every served KPI definition. Draft
    serving instead fingerprints the target-aware dependency closure read from
    the current model rows so another replica cannot reuse a dependent entry
    warmed before a mutation.
    """
    if served_definition_version:
        return served_definition_version
    result = await db.execute(select(KPI).where(KPI.model_id == model_id))
    all_kpis = list(result.scalars().all())
    return _kpi_dependency_cache_version(
        kpi,
        {candidate.name: candidate for candidate in all_kpis},
        {candidate.id: candidate for candidate in all_kpis},
        definition_version,
    )


@router.post(
    "/{kpi_id}/evaluate",
    response_model=KPIEvaluateResponse,
    dependencies=[require_role("viewer")],
)
async def evaluate_kpi(
    project_id: UUID,
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

        # Draft KPIs are only visible to modelers/admins (Section 13.1).
        # F-017-12 / Bug-8728: effective binding, not coarse token role. The
        # same is_privileged flag is threaded into composite-child and kpi()
        # reference resolution below, so drafts are hidden consistently there.
        is_privileged = await caller_has_role(
            db, current_user, project_id, "modeler", model_id,
        )
        if kpi.certification_status == "draft" and not is_privileged:
            raise HTTPException(status_code=404, detail="KPI not found")

        model = await db.get(Model, model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found")

        # F-017-01 / F-013-04: the deployed model snapshot is the SINGLE serving
        # authority for the KPI DEFINITION. For a deployed model, resolve the
        # served definition from the snapshot (governance overlaid from live);
        # a definition edit is only live to the scorecard after model deploy. An
        # undeployed model has no serving authority, so the builder keeps
        # evaluating the live draft. ``served_def_version`` anchors the cache key
        # to the deployed (version, epoch) so a cached value is pinned to the
        # deployed definition and only changes on redeploy.
        served_def_version: str | None = None
        try:
            _resolved = await resolve_served_kpi(db, model, kpi)
        except KpiSnapshotInvalidError as exc:
            # DEPLOYED_SNAPSHOT_INVALID — fail closed, never fall back to live.
            raise HTTPException(
                status_code=409,
                detail=f"{KpiSnapshotInvalidError.error_code}: {exc}",
            )
        if isinstance(_resolved, ResolvedKpi):
            kpi = _resolved.kpi
            served_def_version = f"{_resolved.deployed_version_id}:{_resolved.deploy_epoch}"
        elif isinstance(_resolved, Withheld):
            # The KPI id is not part of the deployed model version — it has not
            # been deployed through model Save + Deploy, so it is not served.
            raise HTTPException(status_code=404, detail="KPI not found")
        # else Undeployed: keep the live draft for builder evaluation.

        # Persona-based measure + dimension filtering (Section 13.1, Bug-6329)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        effective_persona_id: str | None = str(persona.id) if persona else None
        allowed_measure_ids: list[UUID] | None = None
        name_to_id: dict[str, UUID] = {}
        _dim_scope: dict | None = None
        if persona:
            allowed_measure_ids = parse_allowed_ids(persona.included_measure_ids)
            allowed_dim_ids = parse_allowed_ids(getattr(persona, "included_dimension_ids", None))
            if allowed_measure_ids is not None or allowed_dim_ids is not None:
                measures_result = await db.execute(
                    select(Measure).where(Measure.model_id == model_id)
                )
                measures_list = list(measures_result.scalars().all())
                name_to_id = {m.name: m.id for m in measures_list}
                dims_result = await db.execute(
                    select(Dimension).where(Dimension.model_id == model_id)
                )
                dims_list = list(dims_result.scalars().all())
                kpis_result = await db.execute(
                    select(KPI).where(KPI.model_id == model_id)
                )
                _dim_scope = {
                    "allowed_dimension_ids": allowed_dim_ids,
                    "dimension_name_to_id": {d.name: d.id for d in dims_list},
                    "measure_id_to_name": {m.id: m.name for m in measures_list},
                    "dimension_id_to_name": {d.id: d.name for d in dims_list},
                    "all_kpis_by_name": {
                        k.name: k for k in kpis_result.scalars().all()
                    },
                }
                if not _kpi_visible_to_persona(
                    kpi, allowed_measure_ids, name_to_id,
                    dim_scope=_dim_scope,
                ):
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
        _ck_calc_mode, _ck_filters, _ck_time_ctx, _ck_def_ver = _kpi_cache_key_components(kpi)
        # F-017-01: for a deployed model the served definition IS the deployed
        # snapshot, so key on (version, epoch); it changes only on redeploy. For
        # an undeployed draft fall back to the KPI's updated_at (Bug-7242).
        _ck_def_ver = served_def_version or _ck_def_ver
        # F-017-03 (Bug-7989): fold the model data-freshness epoch into the key so
        # a successful data/aggregate/pocket refresh invalidates stale entries on
        # every replica without waiting out the TTL.
        _ck_data_epoch = int(getattr(model, "data_epoch", 0) or 0)
        _outer_cache_allowed = (
            effective_persona_id is None
            and await _kpi_outer_cache_allowed(db, model_id)
        )
        if (
            _outer_cache_allowed
            and (_kpi_reference_names(kpi) or kpi.kpi_type == "composite")
        ):
            _ck_def_ver = await _single_kpi_cache_definition_version(
                db, model_id, kpi, served_def_version, _ck_def_ver,
            )
        cached = (
            _cache.get(
                current_user.tenant_id, model_id, kpi_id,
                calc_agg_mode=_ck_calc_mode, filters=_ck_filters,
                time_context=_ck_time_ctx,
                user_id=current_user.user_id, persona_id=effective_persona_id,
                definition_version=_ck_def_ver,  # Bug-7242 / F-017-01
                data_epoch=_ck_data_epoch,  # F-017-03
            )
            if _outer_cache_allowed else None
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
        # Bug-8449: row-security rule ids the router applied to this evaluation.
        eval_security_rules: set[str] = set()

        # Bug-8569: ONE evaluation context, shared by the value and target legs
        # so both answer the same question. Built before the composite branch
        # because a composite's target leg needs it too.
        #
        # ``security_sink`` is deliberately NOT in this block: it stays an
        # explicit keyword at every governed call site so the Bug-8449 AST
        # coverage guard can still see it (that guard fails closed on a
        # ``**kwargs`` splat, which is the right behaviour — a governance sink
        # must not be provable only by reading another function).
        value_leg_kwargs = dict(
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
                        dim_scope=_dim_scope,
                        security_sink=eval_security_rules,
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
                # v2 path: try SQL compiler first, fall back to Python evaluator.
                value = await _evaluate_expression_via_sql(
                    kpi.expression, model_id, model.slug, bearer, measure_map, ctx,
                    security_sink=eval_security_rules,
                    **value_leg_kwargs,
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
                if value is _MODEL_NOT_DEPLOYED:
                    return KPIEvaluateResponse(
                        kpi_id=kpi_id,
                        value=None,
                        status_label=_MODEL_NOT_DEPLOYED_LABEL,
                    )
                if _is_evaluation_failure(value):
                    # Bug-8449 (round-1 deep review, finding 4): the same
                    # denied-not-failed branch the ad-hoc and batch endpoints
                    # have. Without it the three endpoints DRIFT — a governance
                    # denial reads "check KPI expression" here and "Restricted
                    # by row security" there — and "Evaluation failed" would
                    # additionally make ``_child_error_reason`` treat every
                    # child of a composite as an ERRORED input.
                    if row_security_denied_all(eval_security_rules):
                        return KPIEvaluateResponse(
                            kpi_id=kpi_id,
                            value=None,
                            status_label=ROW_SECURITY_RESTRICTED_LABEL,
                            row_security_restricted=True,
                        )
                    return KPIEvaluateResponse(
                        kpi_id=kpi_id,
                        value=None,
                        status_label="Evaluation failed — check KPI expression and model scope",
                    )
                if value is _COMPILER_UNSUPPORTED:
                    # Expression has time intelligence or KPI refs; use Python
                    # evaluator (F-017-09: _result_to_response marks the response
                    # evaluation_path="python" so the fallback is visible).
                    # Pre-fetch dependencies from BOTH the value and target.
                    # The shared pipeline evaluates both, so a target-only kpi()
                    # or measure reference must be available to the provider.
                    fallback_expressions = [kpi.expression]
                    target_expr = (
                        kpi.target_expression
                        or _prior_period_target_expression(kpi)
                    )
                    if target_expr:
                        fallback_expressions.append(target_expr)
                    ref_names: set[str] = set()
                    for expression in fallback_expressions:
                        ref_names.update(extract_measure_names(expression))
                    target_measure = None
                    if kpi.target_type == "measure" and kpi.target_measure_id:
                        target_measure = next(
                            (
                                m for m in measure_map.values()
                                if m.id == kpi.target_measure_id
                            ),
                            None,
                        )
                        if target_measure is not None:
                            ref_names.add(target_measure.name)
                    try:
                        mv_cache = await _batch_get_measure_values(
                            model_id, list(ref_names), bearer, model.slug, measure_map,
                            persona_id=effective_persona_id,
                            security_sink=eval_security_rules,
                            where_clause=bd_where,
                        )
                    except _ModelNotDeployedError:
                        return KPIEvaluateResponse(
                            kpi_id=kpi.id,
                            value=None,
                            status_label=_MODEL_NOT_DEPLOYED_LABEL,
                        )
                    # F-017-08: resolve referenced KPIs so kpi() cross-references
                    # compute on the single /evaluate path too (KpisPanel), not
                    # only in the Scorecard batch path.
                    kpi_ref_cache: dict[str, float | None] = {}
                    for expression in fallback_expressions:
                        kpi_ref_cache.update(
                            await _resolve_referenced_kpi_values(
                                expression,
                                db,
                                model_id,
                                model.slug,
                                bearer,
                                measure_map,
                                model=model,
                                is_privileged=is_privileged,
                                persona_id=effective_persona_id,
                                allowed_measure_ids=allowed_measure_ids,
                                name_to_id=name_to_id,
                                dim_scope=_dim_scope,
                                security_sink=eval_security_rules,
                            )
                        )
                    if target_measure is not None:
                        ctx.target_value = mv_cache.get(target_measure.name)
                    provider = _build_measure_provider(
                        model_id, model.slug, bearer, measure_map,
                        measure_value_cache=mv_cache,
                        kpi_value_cache=kpi_ref_cache or None,
                        persona_id=effective_persona_id,
                        security_sink=eval_security_rules,
                        where_clause=bd_where,
                        ti_evaluator=_build_ti_evaluator(
                            model_id, model.slug, bearer, measure_map,
                            time_column=time_column,
                            calc_agg_mode=ctx.calc_agg_mode,
                            fiscal_year_start_month=getattr(
                                model, "fiscal_year_start_month", None,
                            ),
                            filter_where_clause=bd_filter_where,
                            persona_id=effective_persona_id,
                            security_sink=eval_security_rules,
                        ) if time_column else None,
                    )
                    try:
                        result = await run_evaluation_pipeline(ctx, provider)
                    except _ModelNotDeployedError:
                        return KPIEvaluateResponse(
                            kpi_id=kpi.id,
                            value=None,
                            status_label=_MODEL_NOT_DEPLOYED_LABEL,
                        )
                    resp = _result_to_response(result)
                    # Bug-8449: stamp BEFORE the cache write so a replayed hit
                    # carries the same honest explanation as the live miss.
                    _stamp_row_security_restriction(resp, eval_security_rules)
                    if _outer_cache_allowed:
                        _cache.put(
                            current_user.tenant_id, model_id, kpi_id, resp,
                            calc_agg_mode=_ck_calc_mode, filters=_ck_filters,
                            time_context=_ck_time_ctx,
                            user_id=current_user.user_id,
                            persona_id=effective_persona_id,
                            definition_version=_ck_def_ver,
                            data_epoch=_ck_data_epoch,
                        )
                    return resp

            # Also compile target if it's an expression — same filter/time scope.
            # Bug-6251: a prior_period target is carried as target_expression
            # (prior_period(...)); when absent (API-created KPI) synthesize it so
            # the SQL target path resolves it via the TI machinery too.
            target_expr = kpi.target_expression or _prior_period_target_expression(kpi)
            if target_expr:
                # Bug-8569: the SAME context as the value leg (slice, calendar,
                # fiscal start, semi-additive reduction, business-definition
                # window), minus the value expression's own decomposition.
                target = await _evaluate_expression_via_sql(
                    target_expr, model_id, model.slug, bearer,
                    measure_map, ctx,
                    security_sink=eval_security_rules,
                    **_target_leg_kwargs(value_leg_kwargs),
                )
                if target is _MODEL_NOT_DEPLOYED:
                    return KPIEvaluateResponse(
                        kpi_id=kpi.id,
                        value=None,
                        status_label=_MODEL_NOT_DEPLOYED_LABEL,
                    )
                if target is _COMPILER_UNSUPPORTED:
                    target_measure_names = extract_measure_names(target_expr)
                    try:
                        target_measure_cache = await _batch_get_measure_values(
                            model_id,
                            target_measure_names,
                            bearer,
                            model.slug,
                            measure_map,
                            persona_id=effective_persona_id,
                            security_sink=eval_security_rules,
                            where_clause=bd_where,
                        )
                    except _ModelNotDeployedError:
                        return KPIEvaluateResponse(
                            kpi_id=kpi.id,
                            value=None,
                            status_label=_MODEL_NOT_DEPLOYED_LABEL,
                        )
                    target_kpi_cache = await _resolve_referenced_kpi_values(
                        target_expr,
                        db,
                        model_id,
                        model.slug,
                        bearer,
                        measure_map,
                        model=model,
                        is_privileged=is_privileged,
                        persona_id=effective_persona_id,
                        allowed_measure_ids=allowed_measure_ids,
                        name_to_id=name_to_id,
                        dim_scope=_dim_scope,
                        security_sink=eval_security_rules,
                    )
                    target_provider = _build_measure_provider(
                        model_id,
                        model.slug,
                        bearer,
                        measure_map,
                        measure_value_cache=target_measure_cache,
                        kpi_value_cache=target_kpi_cache or None,
                        persona_id=effective_persona_id,
                        security_sink=eval_security_rules,
                        where_clause=bd_where,
                        ti_evaluator=_build_ti_evaluator(
                            model_id,
                            model.slug,
                            bearer,
                            measure_map,
                            time_column=time_column,
                            calc_agg_mode=ctx.calc_agg_mode,
                            fiscal_year_start_month=getattr(
                                model, "fiscal_year_start_month", None,
                            ),
                            filter_where_clause=bd_filter_where,
                            persona_id=effective_persona_id,
                            security_sink=eval_security_rules,
                        ) if time_column else None,
                    )
                    try:
                        target = await _evaluate_python_expression(
                            target_expr, ctx, target_provider,
                        )
                    except _ModelNotDeployedError:
                        return KPIEvaluateResponse(
                            kpi_id=kpi.id,
                            value=None,
                            status_label=_MODEL_NOT_DEPLOYED_LABEL,
                        )
                elif target in (
                    _EVALUATION_ERROR, _GUARD_REFUSED, _TI_NO_TIME_DIMENSION,
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
                    # Bug-6663: catch query failures so a broken target
                    # query does not 500 the entire batch; label as failed.
                    try:
                        target = await _get_measure_value(
                            model_id, target_measure.name, bearer,
                            model.slug, target_measure.default_agg or "sum",
                            persona_id=effective_persona_id,
                            where_clause=bd_where,  # F-017-14: same slice as the value
                            security_sink=eval_security_rules,
                        )
                        ctx.target_value = target
                    except Exception as exc:  # noqa: BLE001
                        if _is_model_not_deployed_error(exc):
                            return KPIEvaluateResponse(
                                kpi_id=kpi.id,
                                value=None,
                                status_label=_MODEL_NOT_DEPLOYED_LABEL,
                            )
                        log.warning("Bug-6663: target measure query failed for KPI %s", kpi.id)
                        ctx.target_value = None
                        ctx.target_query_failed = True
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
            # Apply health and governance before the cache write so a replayed
            # composite preserves the same fail-loud, redacted response.
            resp = _apply_composite_result(
                resp, composite_result, eval_security_rules,
            )
            if _outer_cache_allowed:
                _cache.put(
                    current_user.tenant_id, model_id, kpi_id, resp,
                    calc_agg_mode=_ck_calc_mode, filters=_ck_filters,
                    time_context=_ck_time_ctx,
                    user_id=current_user.user_id,
                    persona_id=effective_persona_id,
                    definition_version=_ck_def_ver,
                    data_epoch=_ck_data_epoch,
                )
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

    # Bug-7231: each ad-hoc evaluation runs a live query against the source, and
    # the wizard preview fires one per keystroke. Cap it PER USER so a single
    # client cannot storm the source with unsaved preview queries. This reuses
    # the bounded/shared action-quota engine (the same ``limits`` store the
    # platform limiter uses -- per-replica by default, a single ceiling across
    # replicas when ``RATE_LIMIT_STORAGE_URI`` is set), NOT an unbounded
    # per-process counter. Config-driven; 0 disables the cap.
    _adhoc_per_min = int(system_snapshot_get("rate_limit.adhoc_kpi_per_minute"))
    if _adhoc_per_min > 0 and not consume_action_quota(
        "kpi.evaluate_adhoc",
        str(current_user.tenant_id),
        str(current_user.user_id),
        limit=f"{_adhoc_per_min}/minute",
    ):
        _retry_after = str(int(system_snapshot_get("rate_limit.retry_after_seconds")))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Ad-hoc KPI evaluation is limited to {_adhoc_per_min} previews "
                "per minute. Pause a moment and try again."
            ),
            headers={"Retry-After": _retry_after},
        )

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
        # F-017-12 / Bug-8728: effective binding, not coarse token role.
        is_privileged = await caller_has_role(
            db, current_user, project_id, "modeler", model_id,
        )
        allowed_measure_ids: list[UUID] | None = None
        allowed_dimension_ids: list[UUID] | None = None
        adhoc_name_to_id = {m.name: m.id for m in measure_map.values()}
        adhoc_dim_scope: dict | None = None
        if persona:
            allowed_measure_ids = parse_allowed_ids(persona.included_measure_ids)
            allowed_dimension_ids = parse_allowed_ids(getattr(persona, "included_dimension_ids", None))
            if allowed_measure_ids is not None or allowed_dimension_ids is not None:
                dims_result = await db.execute(
                    select(Dimension).where(Dimension.model_id == model_id)
                )
                dims_for_scope = list(dims_result.scalars().all())
                kpis_result = await db.execute(
                    select(KPI).where(KPI.model_id == model_id)
                )
                adhoc_dim_scope = {
                    "allowed_dimension_ids": allowed_dimension_ids,
                    "dimension_name_to_id": {
                        d.name: d.id for d in dims_for_scope
                    },
                    "measure_id_to_name": {
                        m.id: m.name for m in measure_map.values()
                    },
                    "dimension_id_to_name": {
                        d.id: d.name for d in dims_for_scope
                    },
                    "all_kpis_by_name": {
                        k.name: k for k in kpis_result.scalars().all()
                    },
                }
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
                dimension_data_types=dimension_data_types,
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
        # Bug-8449: collect the row-security rule ids the router applied so a
        # null value caused by a DENIAL is not reported as "no data".
        adhoc_security_rules: set[str] = set()
        # Bug-8569/Bug-8570: ONE evaluation context. It carries the semi-additive
        # reduction and the calendar/fiscal settings the SAVED KPI will use, so
        # the preview number is the number the product will serve, and the
        # target leg below is computed under the same rules as the value.
        value_leg_kwargs = dict(
            persona_id=effective_persona_id,
            time_column=adhoc_time_column,
            calendar_type=_derive_calendar_type(model),
            fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
            at_grain=body.at_grain,
            non_additive_agg=body.non_additive_agg,
            carry_forward=body.carry_forward,
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
        value = await _evaluate_expression_via_sql(
            adhoc_expression, model_id, model.slug, bearer,
            measure_map, ctx,
            security_sink=adhoc_security_rules,
            **value_leg_kwargs,
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
        if value is _MODEL_NOT_DEPLOYED:
            raise HTTPException(
                status_code=400,
                detail=_MODEL_NOT_DEPLOYED_LABEL,
            )
        if value is _GUARD_REFUSED:
            # Bug-8568 (deep-review R6 finding 7): a REFUSAL only. R4 made
            # this branch fire for the whole overloaded _EVALUATION_ERROR,
            # which (a) made the Bug-8449 deny-all branch below unreachable
            # on this endpoint while /evaluate and batch still consulted the
            # sink -- the three-endpoint drift Bug-8449 existed to remove --
            # and (b) turned a 10s _ADHOC_TIMEOUT_S trip or a transient
            # router error on the builder preview into 'check your
            # expression', sending the modeller to debug a correct one.
            # A refusal is deterministic and IS about the definition, so a
            # 400 is right for it and wrong for everything else.
            #
            # Deep-review R4 finding 2: this branch used to accept
            # _EVALUATION_ERROR too and run the Python evaluator anyway, so
            # EVERY fail-closed in ``_evaluate_expression_via_sql`` -- the
            # Bug-6252 semi-additive refusals AND the F-017-22
            # ``has_ungrouped_window`` degenerate share/rank refusal, which
            # ad-hoc can absolutely reach since it passes share_type /
            # share_dimension / share_n -- was silently downgraded to "try
            # the un-guarded path" on the builder preview. A modeller then
            # sees a plausible number for exactly the case /evaluate and the
            # batch endpoint refuse to serve. Preview and serve must agree.
            raise HTTPException(
                status_code=400,
                detail=(
                    "This KPI cannot be evaluated as defined — check "
                    "the expression, the aggregation grain and the model "
                    "scope."
                ),
            )
        if value is _EVALUATION_ERROR:
            # Bug-8568 (deep-review R7 finding 1) -- REGRESSION GUARD, mine.
            #
            # R6 split the overloaded sentinel correctly and then routed the
            # EXECUTION-failure half into the Python fallback below. That
            # fallback resolves measures through ``_batch_get_measure_values``,
            # whose signature takes NO filter arguments at all: it can only
            # ever emit ``SELECT SUM("X") FROM "<model>"``. So the moment the
            # real, correctly-filtered query failed transiently, the endpoint
            # re-answered the question with a DIFFERENT, unfiltered one and
            # returned it at HTTP 200. Verified end-to-end: a ratio KPI
            # filtered to EMEA (220/11 = 20.0) came back as 10.00, the
            # all-regions number, with no warning -- and the modeller saves
            # the KPI on that basis. Worse than the message problem R6 set out
            # to fix.
            #
            # A failed run must never be answered by a different query. Say so
            # honestly instead, and keep the Bug-8449 deny-all label ahead of
            # the generic message so a row-security refusal is not reported as
            # an outage.
            if row_security_denied_all(adhoc_security_rules):
                return _stamp_row_security_restriction(
                    KPIEvaluateResponse(
                        kpi_id=None,
                        value=None,
                        status_label=ROW_SECURITY_RESTRICTED_LABEL,
                        row_security_restricted=True,
                    ),
                    adhoc_security_rules,
                )
            raise HTTPException(
                status_code=503,
                detail=(
                    "The KPI query could not be run just now (the source or "
                    "query service did not respond in time). The definition "
                    "itself may be fine — try the preview again."
                ),
            )
        if value is _COMPILER_UNSUPPORTED:
            # F-017-09: the wizard preview falls back to the Python evaluator;
            # _result_to_response marks evaluation_path="python" on the response.
            fallback_expressions = [adhoc_expression]
            if adhoc_target_expression:
                fallback_expressions.append(adhoc_target_expression)
            ref_names: set[str] = set()
            for expression in fallback_expressions:
                ref_names.update(extract_measure_names(expression))
            try:
                mv_cache = await _batch_get_measure_values(
                    model_id, list(ref_names), bearer, model.slug, measure_map,
                    persona_id=effective_persona_id,
                    security_sink=adhoc_security_rules,
                    where_clause=adhoc_where,
                )
            except _ModelNotDeployedError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=_MODEL_NOT_DEPLOYED_LABEL,
                ) from exc
            adhoc_kpi_cache: dict[str, float | None] = {}
            for expression in fallback_expressions:
                adhoc_kpi_cache.update(
                    await _resolve_referenced_kpi_values(
                        expression,
                        db,
                        model_id,
                        model.slug,
                        bearer,
                        measure_map,
                        model=model,
                        is_privileged=is_privileged,
                        persona_id=effective_persona_id,
                        allowed_measure_ids=allowed_measure_ids,
                        name_to_id=adhoc_name_to_id,
                        dim_scope=adhoc_dim_scope,
                        security_sink=adhoc_security_rules,
                    )
                )
            provider = _build_measure_provider(
                model_id, model.slug, bearer, measure_map,
                measure_value_cache=mv_cache,
                kpi_value_cache=adhoc_kpi_cache or None,
                persona_id=effective_persona_id,
                security_sink=adhoc_security_rules,
                where_clause=adhoc_where,
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
                    security_sink=adhoc_security_rules,
                ) if adhoc_time_column else None,
            )
            try:
                result = await run_evaluation_pipeline(ctx, provider)
            except _ModelNotDeployedError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=_MODEL_NOT_DEPLOYED_LABEL,
                ) from exc
            # Bug-8568 (L7 clean-up): the branch that used to sit here read
            # ``if result.value is None and _is_evaluation_failure(value)``.
            # Inside this block ``value IS _COMPILER_UNSUPPORTED``, which is not
            # a failure sentinel, so the condition was ALWAYS False — a 400 that
            # could never fire. Firing it would also have been wrong: a Python
            # fallback that legitimately finds no data must report No Data, not
            # "check expression and model scope". Every failure sentinel is
            # already dispositioned above (_GUARD_REFUSED -> 400,
            # _EVALUATION_ERROR -> 503, _MODEL_NOT_DEPLOYED -> 400), and the
            # Bug-8449 row-security stamp below applies unconditionally, so the
            # deny-all case the dead branch guarded is still reported honestly.
            return _stamp_row_security_restriction(
                _result_to_response(result), adhoc_security_rules,
            )

        # Evaluate target — same filter/time scope as the value (F-017-14).
        # Use adhoc_target_expression (which carries the builder-compiled target
        # when a business_definition was provided), not the raw body field;
        # otherwise the preview shows no target/status for KPIs whose saved form
        # will have one. Static builder targets fall back to adhoc_target_value.
        target: float | None = None
        if adhoc_target_expression:
            # Bug-8569: same context as the value leg, minus the value
            # expression's own decomposition/share metadata.
            target = await _evaluate_expression_via_sql(
                adhoc_target_expression, model_id, model.slug, bearer,
                measure_map, ctx,
                security_sink=adhoc_security_rules,
                **_target_leg_kwargs(value_leg_kwargs),
            )
            if target is _MODEL_NOT_DEPLOYED:
                raise HTTPException(
                    status_code=400,
                    detail=_MODEL_NOT_DEPLOYED_LABEL,
                )
            if target in (
                _EVALUATION_ERROR, _GUARD_REFUSED, _TI_NO_TIME_DIMENSION,
            ) or isinstance(target, _TIDecompositionFailed):
                target = None
            elif target is _COMPILER_UNSUPPORTED:
                try:
                    target_measure_cache = await _batch_get_measure_values(
                        model_id,
                        extract_measure_names(adhoc_target_expression),
                        bearer,
                        model.slug,
                        measure_map,
                        persona_id=effective_persona_id,
                        security_sink=adhoc_security_rules,
                        where_clause=adhoc_where,
                    )
                except _ModelNotDeployedError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=_MODEL_NOT_DEPLOYED_LABEL,
                    ) from exc
                target_kpi_cache = await _resolve_referenced_kpi_values(
                    adhoc_target_expression,
                    db,
                    model_id,
                    model.slug,
                    bearer,
                    measure_map,
                    model=model,
                    is_privileged=is_privileged,
                    persona_id=effective_persona_id,
                    allowed_measure_ids=allowed_measure_ids,
                    name_to_id=adhoc_name_to_id,
                    dim_scope=adhoc_dim_scope,
                    security_sink=adhoc_security_rules,
                )
                target_provider = _build_measure_provider(
                    model_id,
                    model.slug,
                    bearer,
                    measure_map,
                    measure_value_cache=target_measure_cache,
                    kpi_value_cache=target_kpi_cache or None,
                    persona_id=effective_persona_id,
                    security_sink=adhoc_security_rules,
                    where_clause=adhoc_where,
                    ti_evaluator=_build_ti_evaluator(
                        model_id,
                        model.slug,
                        bearer,
                        measure_map,
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
                        security_sink=adhoc_security_rules,
                    ) if adhoc_time_column else None,
                )
                try:
                    target = await _evaluate_python_expression(
                        adhoc_target_expression, ctx, target_provider,
                    )
                except _ModelNotDeployedError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=_MODEL_NOT_DEPLOYED_LABEL,
                    ) from exc
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
        _adhoc_colorblind = bool(meta.get("colorblind", False))  # Bug-7240
        # Bug-8488: ``threshold_preset`` was an ACCEPTED-BUT-UNWIRED field. A
        # request with value 1, target 2 and ``threshold_preset=standard_4_band``
        # came back "Off Target" — the DEFAULT preset's verdict — instead of the
        # requested preset's "Critical", so the builder preview showed a band the
        # saved KPI would not use. Explicit ``presentation_meta.bands`` still win
        # (they are the more specific instruction).
        _preset_bands = None
        if body.threshold_preset:
            if body.threshold_preset not in list_presets():
                # ``get_preset_bands`` silently substitutes standard_3_band for
                # an unknown name, which would score the preview against bands
                # the caller never asked for. Reject instead.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown threshold_preset '{body.threshold_preset}'. "
                        f"Must be one of: {sorted(list_presets())}."
                    ),
                )
            _preset_bands = _serialize_bands(
                get_preset_bands(body.threshold_preset, colorblind=_adhoc_colorblind)
            )
        th = None
        if meta.get("bands") and value is not None:
            th = evaluate_threshold(
                value=value, target=target, direction=ctx.direction,
                evaluation_type=adhoc_eval_type,
                bands=meta.get("bands"),
                colorblind=_adhoc_colorblind,  # Bug-7240
            )
        elif value is not None and target is not None:
            # Bug-1226: thread the evaluation_type so a variance preview scores on
            # the deviation preset, keeping badge/needle/band agreement.
            th = evaluate_threshold(
                value=value, target=target, direction=ctx.direction,
                evaluation_type=adhoc_eval_type,
                bands=_preset_bands,  # Bug-8488
                colorblind=_adhoc_colorblind,  # Bug-7240
            )
        if th is not None:
            status_val = th.status
            status_label = th.status_label
            status_color = th.status_color
            # Bug-1226: authoritative gauge position + bands, so the preview
            # gauge agrees with the status badge for every evaluation type.
            status_position = th.ratio
            status_bands = _serialize_bands(th.bands_used)

        # Bug-8449 / Bug-8427: a null value caused by the row-security deny-all
        # coverage predicate is a GOVERNANCE outcome, not missing data. Report
        # it explicitly instead of letting the preview render "N/A" with no
        # explanation. The value itself stays None — that IS the correct value
        # for a caller entitled to zero rows — so no number changes here.
        # Bug-8449 (Codex gate finding 3): no ``value is None`` gate — a
        # deny-all COUNT returns 0, not NULL. The response is assembled normally
        # and then redacted wholesale by the shared helper at the return, so the
        # ad-hoc preview can never show a scalar the caller may not see.

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
            at_grain=body.at_grain,
            non_additive_agg=body.non_additive_agg,
            carry_forward=body.carry_forward,
            sql_sink=adhoc_sql_parts,
            timeout_s=_ADHOC_TIMEOUT_S,
            security_sink=adhoc_security_rules,
        )
        # Refresh the compiled-SQL surface so the preview's "Show SQL" panel
        # exposes every statement this request actually ran, in execution order
        # and de-duped. Bug-8569 put the TARGET leg on the same sql_sink as the
        # value, so this must refresh unconditionally: gating it on the
        # sparkline hid the target query whenever no series was produced.
        adhoc_compiled_sql = "\n\n".join(dict.fromkeys(adhoc_sql_parts)) or None

        elapsed = int((_time.monotonic_ns() - start_ms) / 1_000_000)

        _adhoc_resp = KPIEvaluateResponse(
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
            trend_pct_normalised=tr.trend_pct_normalised,  # Bug-7238
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
        # Bug-8449: single redaction point covering every ad-hoc exit.
        return _stamp_row_security_restriction(_adhoc_resp, adhoc_security_rules)

    raise HTTPException(status_code=500, detail="DB session exhausted")


# Bug-7774: extract the inner viewer-check function from require_role so the
# composite dependency below can delegate to it for human/embed users without
# going through a second Depends resolution layer.
_viewer_role_check = require_role("viewer").dependency


async def _require_viewer_or_kpi_service_scope(
    project_id: UUID,
    model_id: UUID | None = None,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    """Bug-7774: composite dependency that allows either a human/embed viewer
    OR a service token carrying ``SCOPE_KPI_EVALUATE``. Without this, the
    Auth-RR-01 fix that blocks service tokens from human RBAC paths also
    blocks the scheduler's KPI-snapshot sweep from calling evaluate-batch.
    """
    if isinstance(current_user, CurrentServiceUser):
        # Service tokens bypass human RBAC entirely — they must carry
        # the KPI evaluate scope.  Fail closed for any other scope.
        scopes = getattr(current_user, "service_scopes", None) or []
        if SCOPE_KPI_EVALUATE in scopes:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service token does not carry KPI evaluate scope",
        )
    # Human / embed user: delegate to the standard viewer role check.
    await _viewer_role_check(
        project_id=project_id,
        model_id=model_id,
        current_user=current_user,
    )


@router.post(
    "/evaluate-batch",
    response_model=KPIBatchResponse,
    dependencies=[Depends(_require_viewer_or_kpi_service_scope)],
)
async def evaluate_batch(
    project_id: UUID,
    model_id: UUID,
    body: KPIBatchRequest,
    request: Request,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(
        require_capability_or_service_scope("query", SCOPE_KPI_EVALUATE)
    ),
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

        # Bug-7982 completion round (wrong-number stamp-timing): capture the
        # deploy pointer/epoch NOW — at the very start of evaluation, before any
        # served definition is resolved or any KPI is evaluated — and thread it
        # UNCHANGED into ``_upsert_kpi_latest_batch`` at the end of this request.
        # A revert can commit and bump ``deploy_epoch`` WHILE this evaluation is
        # in flight (measure/dimension resolution below can call out to the
        # query-router); the values this request computes are evaluated under
        # THIS (already in-memory, unaffected by a later commit) definition, so
        # the write must be stamped with the epoch that was current when
        # evaluation STARTED, never a value re-read fresh from the DB at write
        # time (which could already reflect the concurrent revert).
        _eval_version_id_at_start = getattr(model, "deployed_version_id", None)
        _eval_epoch_at_start = int(getattr(model, "deploy_epoch", 0) or 0)
        # R6 findings 1/7/8 (within-epoch ordering marker): captured once when the
        # evaluation begins, before any source read. Sourced from the DB SERVER
        # clock (``clock_timestamp()``) so ALL writers — this handler, the sweep's
        # own _upsert_kpi_latest, and the post-deploy trigger — order by one
        # monotonic clock, immune to cross-process wall-clock skew (#8). When the
        # internal-service sweep supplies its own marker, honour it so the sweep's
        # write shares this handler's marker and is not falsely suppressed (#7).
        # Only an internal-service publish call (the sweep / post-deploy trigger)
        # ever writes kpi_latest, so only it needs the ordering marker. A regular
        # user render never publishes — leave its marker None (no extra DB query).
        # Bug-9524: the rotating internal marker is a transport/rate-limit
        # signal, not publication authority.  A model-wide ``kpi_latest``
        # write is allowed only for the real typed KPI service principal, on
        # the two-hop scopes that authorize model-service evaluation and its
        # query-router execution, with a valid marker and no request slice.
        # Keep this one decision for both ordering-marker allocation and the
        # final write gate: a filtered or human request must still return its
        # correctly scoped response, but must never allocate/publish a global
        # ordering marker or durable model-wide value.
        _service_scopes = set(
            getattr(current_user, "service_scopes", None) or []
        )
        _publish_authority = (
            isinstance(current_user, CurrentServiceUser)
            and bool(getattr(current_user, "service_principal", None))
            and {
                SCOPE_KPI_EVALUATE,
                SCOPE_KPI_QUERY_EXECUTE,
            }.issubset(_service_scopes)
            and is_internal_request_header(
                request.headers.get(INTERNAL_BYPASS_HEADER)
            )
            and not body.filters
        )
        # R7 finding 2: the ordering token is now a strictly-increasing DB
        # sequence value (``eval_generation``), not the non-unique
        # ``clock_timestamp()``. Both are allocated in ONE round-trip at
        # evaluation start; the timestamp stays as metadata / fallback order.
        if _publish_authority:
            # The clamp rules (and why a caller-supplied token must be bounded at
            # all) live in ``shared/db/kpi_eval_generation.clamp_supplied_marker``
            # so they are testable without the full request stack — R7 review
            # round 1, finding 6.
            _eval_started_at, _eval_generation = clamp_supplied_marker(
                body.eval_started_at,
                body.eval_generation,
                await allocate_kpi_eval_marker(db),
            )
        else:
            _eval_started_at = None
            _eval_generation = None

        # F-017-01 / F-013-04: evaluate-batch produces the KPILatest rows that
        # JDBC $KPIs serves and drives the scorecard. For a DEPLOYED model, every
        # KPI's DEFINITION must come from the deployed snapshot (governance
        # overlaid from live); an undeployed edit must not change a served /
        # published KPI. Build a served-definition map keyed by KPI id once, then
        # substitute it wherever a live KPI is loaded below. An undeployed model
        # keeps the live drafts for builder evaluation. ``_batch_data_epoch``
        # anchors the batch cache keys to the model data-freshness epoch (F-017-03).
        _batch_deployed = getattr(model, "deployed_version_id", None) is not None
        _served_by_id: dict[UUID, KPI] = {}
        _served_version_tag: str | None = None
        if _batch_deployed:
            all_live = (
                await db.execute(select(KPI).where(KPI.model_id == model_id))
            ).scalars().all()
            try:
                _resolved_batch, _ = await resolve_served_kpis(db, model, list(all_live))
            except KpiSnapshotInvalidError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"{KpiSnapshotInvalidError.error_code}: {exc}",
                )
            _served_by_id = {r.kpi.id: r.kpi for r in _resolved_batch}
            # opus5 completion-round R2 (finding 8.1): derive this cache-key tag
            # from the SAME captured _eval_version_id_at_start/_eval_epoch_at_start
            # above, never by re-reading ``model`` again here. Today this is a
            # second read of an unchanged in-memory attribute, so it is harmless —
            # but it is a second, independent read of the exact quantity this
            # round centralised specifically to eliminate that pattern. If a
            # future change ever adds a ``db.commit()``/``db.refresh(model)``
            # between the capture above and here, THIS line would silently pick
            # up a NEW epoch while the cached values were computed under the OLD
            # definition — a wrong number served from the batch cache, and a
            # SILENT one (unlike the fail-closed kpi_latest path).
            _served_version_tag = (
                f"{_eval_version_id_at_start}:{_eval_epoch_at_start}"
            )
        _batch_data_epoch = int(getattr(model, "data_epoch", 0) or 0)

        def _served(live_kpi: KPI) -> KPI | None:
            """Return the served definition for a live KPI.

            For a deployed model, the snapshot-pinned definition (or None when
            the KPI id is absent from the deployed version — withheld). For an
            undeployed model, the live draft unchanged.
            """
            if not _batch_deployed:
                return live_kpi
            return _served_by_id.get(live_kpi.id)

        bearer = (
            getattr(current_user, "raw_token", None)
            or request.headers.get("Authorization", "").replace("Bearer ", "")
        )

        # Load measures once for all KPIs
        measure_rows = await db.execute(
            select(Measure).where(Measure.model_id == model_id)
        )
        measure_map = {m.name: m for m in measure_rows.scalars().all()}

        # Persona-based measure + dimension filtering (Section 13.1, Bug-6329)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        effective_persona_id: str | None = str(persona.id) if persona else None
        allowed_measure_ids: list[UUID] | None = None
        name_to_id: dict[str, UUID] = {}
        _batch_dim_scope: dict | None = None
        if persona:
            allowed_measure_ids = parse_allowed_ids(persona.included_measure_ids)
            allowed_dim_ids = parse_allowed_ids(getattr(persona, "included_dimension_ids", None))
            if allowed_measure_ids is not None or allowed_dim_ids is not None:
                name_to_id = {m.name: m.id for m in measure_map.values()}
                measures_for_scope = list(measure_map.values())
                dims_result = await db.execute(
                    select(Dimension).where(Dimension.model_id == model_id)
                )
                dims_list = list(dims_result.scalars().all())
                all_kpis_result = await db.execute(
                    select(KPI).where(KPI.model_id == model_id)
                )
                _batch_dim_scope = {
                    "allowed_dimension_ids": allowed_dim_ids,
                    "dimension_name_to_id": {d.name: d.id for d in dims_list},
                    "measure_id_to_name": {
                        m.id: m.name for m in measures_for_scope
                    },
                    "dimension_id_to_name": {d.id: d.name for d in dims_list},
                    "all_kpis_by_name": {
                        k.name: k for k in all_kpis_result.scalars().all()
                    },
                }

        # Load all requested KPIs in a single query (F-23 fix: eliminates N+1)
        # F-017-12 / Bug-8728: effective binding, not coarse token role. Service
        # principals (gateway BI surfaces) resolve to non-privileged here, so a
        # draft KPI never reaches a BI client through batch evaluate.
        is_privileged = await caller_has_role(
            db, current_user, project_id, "modeler", model_id,
        )
        requested_ids = set(body.kpi_ids)
        kpi_query = select(KPI).where(
            KPI.id.in_(list(requested_ids)),
            KPI.model_id == model_id,
        )
        kpi_result = await db.execute(kpi_query)
        kpi_objs: dict[UUID, KPI] = {}
        for kpi in kpi_result.scalars().all():
            # Draft KPIs hidden from non-privileged users. Certification is a
            # LIVE governance field, so gate on the live row before substituting
            # the served (snapshot) definition.
            if kpi.certification_status == "draft" and not is_privileged:
                continue
            # F-017-01: substitute the deployed-snapshot definition (withheld =>
            # skip) before persona filtering, so the visibility decision runs on
            # the served definition.
            served = _served(kpi)
            if served is None:
                continue
            kpi = served
            # Persona filtering: skip KPIs referencing excluded measures/dims
            if not _kpi_visible_to_persona(
                kpi, allowed_measure_ids, name_to_id,
                dim_scope=_batch_dim_scope,
            ):
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
                # F-017-01: substitute the deployed-snapshot definition for the
                # child before scoring so a composite scores its deployed
                # children, not an undeployed edit.
                served = _served(kpi)
                if served is None:
                    continue
                kpi = served
                if not _kpi_visible_to_persona(
                    kpi, allowed_measure_ids, name_to_id,
                    dim_scope=_batch_dim_scope,
                ):
                    continue
                kpi_objs[kpi.id] = kpi
                if kpi.kpi_type == "composite" and kpi.id not in loaded_parent_ids:
                    frontier.append(kpi.id)

        # Load transitive kpi() dependencies from both value and target
        # expressions. A target dependency is needed even when the caller did
        # not request it directly, and a referenced composite must bring its
        # own child tree into the batch before topological evaluation.
        while True:
            loaded_names = {k.name for k in kpi_objs.values()}
            missing_names: set[str] = set()
            for kpi in kpi_objs.values():
                for expression in (kpi.expression, kpi.target_expression):
                    if not expression:
                        continue
                    try:
                        missing_names.update(extract_kpi_references(expression))
                    except Exception:
                        continue
            missing_names.difference_update(loaded_names)
            if not missing_names:
                break

            dependency_result = await db.execute(
                select(KPI).where(
                    KPI.model_id == model_id,
                    KPI.name.in_(sorted(missing_names)),
                )
            )
            new_composites: list[UUID] = []
            added = False
            for live_kpi in dependency_result.scalars().all():
                if live_kpi.certification_status == "draft" and not is_privileged:
                    continue
                dependency = _served(live_kpi)
                if dependency is None or not _kpi_visible_to_persona(
                    dependency,
                    allowed_measure_ids,
                    name_to_id,
                    dim_scope=_batch_dim_scope,
                ):
                    continue
                kpi_objs[dependency.id] = dependency
                added = True
                if dependency.kpi_type == "composite":
                    new_composites.append(dependency.id)

            if not added:
                break

            frontier = new_composites
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
                for live_child in child_result.scalars().all():
                    if live_child.certification_status == "draft" and not is_privileged:
                        continue
                    child = _served(live_child)
                    if child is None or not _kpi_visible_to_persona(
                        child,
                        allowed_measure_ids,
                        name_to_id,
                        dim_scope=_batch_dim_scope,
                    ):
                        continue
                    kpi_objs[child.id] = child
                    if child.kpi_type == "composite" and child.id not in loaded_parent_ids:
                        frontier.append(child.id)

        children_by_parent: dict[UUID, list[KPI]] = {}
        for k in kpi_objs.values():
            if k.parent_kpi_id is not None:
                children_by_parent.setdefault(k.parent_kpi_id, []).append(k)

        # Composite ownership is a value dependency just like kpi(): a
        # composite must finalize after its children and before any KPI that
        # references its score.
        kpi_dicts = [
            {
                "id": k.id,
                "name": k.name,
                "expression": k.expression,
                "target_expression": k.target_expression,
            }
            for k in kpi_objs.values()
        ]
        graph = build_graph(kpi_dicts)
        for parent_id, child_kpis in children_by_parent.items():
            parent = kpi_objs.get(parent_id)
            node = graph.get(parent.name) if parent is not None else None
            if node is None or parent.kpi_type != "composite":
                continue
            node.depends_on = list(dict.fromkeys(
                [*node.depends_on, *(child.name for child in child_kpis)]
            ))
        eval_order, _ = topological_sort(graph)

        # Map name -> KPI for ordered iteration
        name_to_kpi = {k.name: k for k in kpi_objs.values()}

        # Evaluate in topological order, caching results
        # kpi_value_cache: name -> evaluated value (for kpi() cross-references)
        kpi_value_cache: dict[str, float | None] = {}
        # A scalar alone cannot distinguish no-data from a restricted value.
        # Carry governance metadata beside each cached kpi() result so a
        # coalesce() consumer cannot turn a denied composite into a visible 0.
        kpi_value_security_rules: dict[str, set[str]] = {}
        # Fresh composite entries contain only rules applied while resolving
        # that composite's own target. Child governance is merged separately
        # by ``evaluate_composite``. Cached responses expose only the merged
        # restricted bit, so preserve that final verdict fail-closed here.
        composite_finalization_security_rules: dict[UUID, set[str]] = {}
        # eval_cache: str(id) -> {value, target} (for composite children)
        eval_cache: dict[str, dict] = {}
        result_map: dict[UUID, KPIEvaluateResponse] = {}

        # Bug-7226: wire the evaluation cache into the batch path so repeated
        # scorecard renders within the TTL window do not re-execute every KPI's
        # SQL.  The same authorization-scoped key components as the single
        # /evaluate endpoint are used (tenant, model, kpi_id, user, persona).
        _batch_cache = get_kpi_cache()
        _batch_outer_cache_allowed = _batch_outer_cache_allowed_for_request(
            body.filters,
            effective_persona_id,
            await _kpi_outer_cache_allowed(db, model_id),
        )
        _batch_cache_definition_versions: dict[UUID, str] = {}
        if _batch_outer_cache_allowed:
            # Composite children are loaded later for scoring and are not
            # guaranteed to be part of ``kpi_objs``. Cache identity must still
            # include their stable ownership edges, so fingerprint against the
            # current authoritative model set rather than only requested nodes.
            cache_kpis_by_id = kpi_objs
            if any(kpi.kpi_type == "composite" for kpi in kpi_objs.values()):
                cache_kpi_rows = await db.execute(
                    select(KPI).where(KPI.model_id == model_id)
                )
                cache_kpis_by_id = {
                    candidate.id: candidate
                    for candidate in cache_kpi_rows.scalars().all()
                }
            cache_kpis_by_name = {
                candidate.name: candidate
                for candidate in cache_kpis_by_id.values()
            }
            for batch_kpi in kpi_objs.values():
                batch_key = _kpi_cache_key_components(batch_kpi)
                _batch_cache_definition_versions[batch_kpi.id] = (
                    _served_version_tag
                    or _kpi_dependency_cache_version(
                        batch_kpi, cache_kpis_by_name,
                        cache_kpis_by_id, batch_key[3],
                    )
                )

        def _batch_cache_get(*args, **kwargs):
            if not _batch_outer_cache_allowed:
                return None
            return _batch_cache.get(*args, **kwargs)

        def _batch_cache_put(*args, **kwargs) -> None:
            if _batch_outer_cache_allowed:
                _batch_cache.put(*args, **kwargs)

        # Pre-fetch all measures referenced by any KPI in the batch (F-20 fix).
        # This eliminates N+1 individual measure queries in the Python evaluator.
        all_referenced_measures: set[str] = set()
        for k in kpi_objs.values():
            if k.expression:
                all_referenced_measures.update(extract_measure_names(k.expression))
            if k.target_expression:
                all_referenced_measures.update(
                    extract_measure_names(k.target_expression)
                )
        batch_measure_cache: dict[str, float | None] = {}
        batch_measure_security_rules: set[str] = set()
        # Bug-9511: the shared prefetch is intentionally model-wide.  A
        # filtered request must reach the provider's bound WHERE path instead
        # of reusing those unfiltered values.
        if all_referenced_measures and _batch_prefetch_allowed_for_request(body.filters):
            try:
                batch_measure_cache = await _batch_get_measure_values(
                    model_id, list(all_referenced_measures), bearer, model.slug, measure_map,
                    persona_id=effective_persona_id,
                    security_sink=batch_measure_security_rules,
                )
            except _ModelNotDeployedError:
                results = [
                    KPIEvaluateResponse(
                        kpi_id=kpi_id,
                        value=None,
                        status_label=_MODEL_NOT_DEPLOYED_LABEL,
                    )
                    if kpi_id in kpi_objs
                    else KPIEvaluateResponse(kpi_id=kpi_id)
                    for kpi_id in body.kpi_ids
                ]
                elapsed = int((_time.monotonic_ns() - start_ms) / 1_000_000)
                return KPIBatchResponse(results=results, evaluation_ms=elapsed)

        # Bug-5252: compile request-level filters into SQL predicates so they
        # are forwarded to _evaluate_single_kpi alongside the per-KPI
        # business-definition filters.  Load the model's parameter defaults
        # so parameter-mode filters resolve correctly (mirrors the single-
        # evaluate path which compiles at save time with real defaults).
        compiled_request_filters: list[str] | None = None
        if body.filters:
            # Bug-9512: deployed models resolve filter ids from their snapshot;
            # draft dimensions must never silently widen a deployed request.
            try:
                deployed_filter_metadata = await resolve_served_filter_metadata(
                    db, model
                )
            except KpiSnapshotInvalidError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"{KpiSnapshotInvalidError.error_code}: {exc}",
                )
            if deployed_filter_metadata is not None:
                dim_name_map, dim_type_map = deployed_filter_metadata
            else:
                dim_rows = await db.execute(
                    select(Dimension).where(Dimension.model_id == model_id)
                )
                dim_objs = list(dim_rows.scalars().all())
                dim_name_map = {str(d.id): d.name for d in dim_objs}
                dim_type_map = await _dimension_data_types(db, dim_objs)
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
                try:
                    preds = _compile_filters(
                        body.filters, dim_name_map, param_defaults, dim_type_map,
                        strict=True,
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid request filters: {exc}",
                    ) from exc
                compiled_request_filters = preds
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid request filters: no deployed dimensions match the request.",
                )

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

        async def _finalize_batch_composite(
            kpi: KPI,
            existing: KPIEvaluateResponse | None,
            finalization_security_rules: set[str] | None = None,
        ) -> tuple[KPIEvaluateResponse, CompositeResult | None]:
            children = build_composite_children(
                str(kpi.id), all_kpis_for_composite, eval_cache,
            )
            composite_result: CompositeResult | None = None
            if children:
                method, bound_min, bound_max = get_normalisation_config(
                    kpi.presentation_meta,
                )
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
                score = None

            target = existing.target if existing else None
            ctx = await _build_evaluation_context(kpi, db, model_id)
            final = await _finalize_kpi_response(kpi, ctx, db, score, target)
            # Work on a copy: a restricted child may add the deny-all sentinel
            # in _apply_composite_result, but must not be relabelled as target
            # governance in the request-level map.
            merged_rules = set(finalization_security_rules or ())
            return (
                _apply_composite_result(final, composite_result, merged_rules),
                composite_result,
            )

        # First pass: evaluate in topological order
        for kpi_name in eval_order:
            kpi = name_to_kpi.get(kpi_name)
            if kpi is None:
                continue

            # Bug-7226: check evaluation cache before re-executing
            _ck = _kpi_cache_key_components(kpi)
            cached_resp = _batch_cache_get(
                current_user.tenant_id, model_id, kpi.id,
                calc_agg_mode=_ck[0], filters=_ck[1],
                time_context=_ck[2],
                user_id=current_user.user_id,
                persona_id=effective_persona_id,
                definition_version=_batch_cache_definition_versions.get(kpi.id, _ck[3]),
                data_epoch=_batch_data_epoch,  # F-017-03
            )
            if cached_resp is not None:
                if kpi.kpi_type == "composite":
                    composite_finalization_security_rules[kpi.id] = (
                        {ROW_SECURITY_DENY_ALL_RULE_ID}
                        if cached_resp.row_security_restricted else set()
                    )
                result_map[kpi.id] = cached_resp
                kpi_value_cache[kpi.name] = cached_resp.value
                kpi_value_security_rules[kpi.name] = (
                    {ROW_SECURITY_DENY_ALL_RULE_ID}
                    if cached_resp.row_security_restricted else set()
                )
                eval_cache[str(kpi.id)] = {
                    "value": cached_resp.value,
                    "target": cached_resp.target,
                    "error_reason": _child_error_reason(cached_resp),
                    "restricted": bool(cached_resp.row_security_restricted),
                }
                continue

            kpi_security_rules: set[str] = set()
            provider = _build_measure_provider(
                model_id, model.slug, bearer, measure_map,
                kpi_value_cache=kpi_value_cache,
                kpi_value_security_rules=kpi_value_security_rules,
                measure_value_cache=batch_measure_cache,
                measure_value_security_rules=batch_measure_security_rules,
                persona_id=effective_persona_id,
                security_sink=kpi_security_rules,
            )

            response = await _evaluate_single_kpi(
                kpi, db, model_id, model.slug, bearer, provider,
                measure_map=measure_map,
                calendar_type=_derive_calendar_type(model),
                fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
                persona_id=effective_persona_id,
                request_filter_predicates=compiled_request_filters,
                security_sink=kpi_security_rules,
                model=model,
                is_privileged=is_privileged,
                allowed_measure_ids=allowed_measure_ids,
                name_to_id=name_to_id,
                dim_scope=_batch_dim_scope,
            )
            if kpi.kpi_type == "composite":
                # Composite placeholders are skipped by _evaluate_single_kpi,
                # so this sink contains only the composite's target rules.
                composite_finalization_security_rules[kpi.id] = set(
                    kpi_security_rules,
                )
                response, _ = await _finalize_batch_composite(
                    kpi,
                    response,
                    composite_finalization_security_rules[kpi.id],
                )
            result_map[kpi.id] = response
            kpi_value_cache[kpi.name] = response.value
            kpi_value_security_rules[kpi.name] = (
                {ROW_SECURITY_DENY_ALL_RULE_ID}
                if response.row_security_restricted else set()
            )
            eval_cache[str(kpi.id)] = {
                "value": response.value,
                "target": response.target,
                # Bug-4255: flag a failed child evaluation so a composite
                # parent surfaces it as degraded instead of silent no-data.
                "error_reason": _child_error_reason(response),
                "restricted": bool(response.row_security_restricted),
            }
            # Bug-7226: put freshly evaluated response into cache
            _batch_cache_put(
                current_user.tenant_id, model_id, kpi.id, response,
                calc_agg_mode=_ck[0], filters=_ck[1],
                time_context=_ck[2],
                user_id=current_user.user_id,
                persona_id=effective_persona_id,
                definition_version=_batch_cache_definition_versions.get(kpi.id, _ck[3]),
                data_epoch=_batch_data_epoch,  # F-017-03
            )

        # Second pass: evaluate KPIs not in the graph (missing expressions)
        for kpi_id, kpi in kpi_objs.items():
            if kpi_id in result_map:
                continue

            # Bug-7226: cache check for second-pass KPIs
            _ck2 = _kpi_cache_key_components(kpi)
            cached_resp2 = _batch_cache_get(
                current_user.tenant_id, model_id, kpi.id,
                calc_agg_mode=_ck2[0], filters=_ck2[1],
                time_context=_ck2[2],
                user_id=current_user.user_id,
                persona_id=effective_persona_id,
                definition_version=_batch_cache_definition_versions.get(kpi.id, _ck2[3]),
                data_epoch=_batch_data_epoch,  # F-017-03
            )
            if cached_resp2 is not None:
                if kpi.kpi_type == "composite":
                    composite_finalization_security_rules[kpi.id] = (
                        {ROW_SECURITY_DENY_ALL_RULE_ID}
                        if cached_resp2.row_security_restricted else set()
                    )
                result_map[kpi.id] = cached_resp2
                kpi_value_cache[kpi.name] = cached_resp2.value
                kpi_value_security_rules[kpi.name] = (
                    {ROW_SECURITY_DENY_ALL_RULE_ID}
                    if cached_resp2.row_security_restricted else set()
                )
                eval_cache[str(kpi.id)] = {
                    "value": cached_resp2.value,
                    "target": cached_resp2.target,
                    "error_reason": _child_error_reason(cached_resp2),
                    "restricted": bool(cached_resp2.row_security_restricted),
                }
                continue

            kpi_security_rules = set()
            provider = _build_measure_provider(
                model_id, model.slug, bearer, measure_map,
                kpi_value_cache=kpi_value_cache,
                kpi_value_security_rules=kpi_value_security_rules,
                measure_value_cache=batch_measure_cache,
                measure_value_security_rules=batch_measure_security_rules,
                persona_id=effective_persona_id,
                security_sink=kpi_security_rules,
            )
            response = await _evaluate_single_kpi(
                kpi, db, model_id, model.slug, bearer, provider,
                measure_map=measure_map,
                calendar_type=_derive_calendar_type(model),
                fiscal_year_start_month=getattr(model, "fiscal_year_start_month", None),
                persona_id=effective_persona_id,
                request_filter_predicates=compiled_request_filters,
                security_sink=kpi_security_rules,
                model=model,
                is_privileged=is_privileged,
                allowed_measure_ids=allowed_measure_ids,
                name_to_id=name_to_id,
                dim_scope=_batch_dim_scope,
            )
            if kpi.kpi_type == "composite":
                composite_finalization_security_rules[kpi.id] = set(
                    kpi_security_rules,
                )
                response, _ = await _finalize_batch_composite(
                    kpi,
                    response,
                    composite_finalization_security_rules[kpi.id],
                )
            result_map[kpi.id] = response
            kpi_value_cache[kpi.name] = response.value
            kpi_value_security_rules[kpi.name] = (
                {ROW_SECURITY_DENY_ALL_RULE_ID}
                if response.row_security_restricted else set()
            )
            eval_cache[str(kpi.id)] = {
                "value": response.value,
                "target": response.target,
                # Bug-4255: flag a failed child evaluation so a composite
                # parent surfaces it as degraded instead of silent no-data.
                "error_reason": _child_error_reason(response),
                "restricted": bool(response.row_security_restricted),
            }
            # Bug-7226: put freshly evaluated response into cache
            _batch_cache_put(
                current_user.tenant_id, model_id, kpi.id, response,
                calc_agg_mode=_ck2[0], filters=_ck2[1],
                time_context=_ck2[2],
                user_id=current_user.user_id,
                persona_id=effective_persona_id,
                definition_version=_batch_cache_definition_versions.get(kpi.id, _ck2[3]),
                data_epoch=_batch_data_epoch,  # F-017-03
            )

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
                kpi_value_security_rules[kpi.name] = set()
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
                kpi_value_security_rules[kpi.name] = set()
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
            # The composite engine is the shared decision point. Any restricted
            # child yields a restricted result, and this mapper gives single and
            # batch callers identical health metadata and full redaction.
            final = _apply_composite_result(
                final,
                composite_result,
                set(composite_finalization_security_rules.get(kpi.id, set())),
            )
            result_map[kpi.id] = final
            kpi_value_cache[kpi.name] = final.value
            kpi_value_security_rules[kpi.name] = (
                {ROW_SECURITY_DENY_ALL_RULE_ID}
                if final.row_security_restricted else set()
            )
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
                "restricted": bool(final.row_security_restricted),
            }
            # Bug-7226: cache composite third-pass results
            _ck3 = _kpi_cache_key_components(kpi)
            _batch_cache_put(
                current_user.tenant_id, model_id, kpi.id, final,
                calc_agg_mode=_ck3[0], filters=_ck3[1],
                time_context=_ck3[2],
                user_id=current_user.user_id,
                persona_id=effective_persona_id,
                definition_version=_batch_cache_definition_versions.get(kpi.id, _ck3[3]),
                data_epoch=_batch_data_epoch,  # F-017-03
            )

        # Composite finalisation happens after the first evaluation pass so
        # nested scores can use complete child values. Re-propagate its final
        # governance verdict through ``kpi()`` consumers now that a composite
        # may have changed from provisional no-data to restricted. This also
        # repairs cached/fresh ordering differences and overwrites any earlier
        # consumer cache entry with the redacted response.
        for kpi_name in eval_order:
            kpi = name_to_kpi.get(kpi_name)
            response = result_map.get(kpi.id) if kpi is not None else None
            if response is None:
                continue
            referenced_security_rules: set[str] = set()
            for reference_name in _kpi_reference_names(kpi):
                referenced_security_rules.update(
                    kpi_value_security_rules.get(reference_name, set())
                )
            if not row_security_denied_all(referenced_security_rules):
                continue
            _stamp_row_security_restriction(response, referenced_security_rules)
            kpi_value_cache[kpi.name] = response.value
            kpi_value_security_rules[kpi.name] = {
                ROW_SECURITY_DENY_ALL_RULE_ID,
            }
            cache_key = _kpi_cache_key_components(kpi)
            _batch_cache_put(
                current_user.tenant_id, model_id, kpi.id, response,
                calc_agg_mode=cache_key[0], filters=cache_key[1],
                time_context=cache_key[2], user_id=current_user.user_id,
                persona_id=effective_persona_id,
                definition_version=_batch_cache_definition_versions.get(
                    kpi.id, cache_key[3],
                ),
                data_epoch=_batch_data_epoch,
            )

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
        #
        # R6 (round-3 #3): reuse the SAME ``_publish_authority`` computed at
        # evaluation start
        # for the eval_started_at marker decision — do NOT recompute the header
        # predicate here. The internal-service marker is a rotating-HMAC that
        # accepts the current+previous window; two independent evaluations of it,
        # an entire (multi-second) evaluation apart, could disagree across a window
        # boundary and decouple the marker decision from the publish decision (a
        # publish with eval_started_at=None silently reverts to epoch-only
        # ordering, reintroducing the finding-1 clobber). One evaluation, one gate.
        is_service_context = _publish_authority
        # F-017-01 (Opus R1): the upsert must ALSO gate on the model being
        # deployed, mirroring the sweep's guard (sweep.py:1446). Otherwise an
        # internal-service caller evaluating a KPI on an undeployed model
        # publishes a live-draft value into kpi_latest, which JDBC $KPIs would
        # serve — bypassing the deployed-snapshot authority. The sweep already
        # skips undeployed models, but this endpoint must independently refuse.
        # R7 finding 5: the caller (the post-deploy re-eval trigger / the sweep
        # drain) deletes the DURABLE ``pending_kpi_reeval`` outbox row on the
        # strength of this response. HTTP 200 alone is not proof that
        # ``kpi_latest`` was written — a per-row upsert failure is deliberately
        # isolated so it cannot starve sibling KPIs. The publish outcome is
        # therefore reported EXPLICITLY, and callers gate the outbox clear on it.
        # ``kpi_latest_published is None`` = this request never attempted a
        # publish (user render, persona-narrowed, or undeployed model).
        _publish: KpiPublishOutcome | None = None
        if (
            is_service_context
            and effective_persona_id is None
            and _batch_deployed
        ):
            # Bug-7982 completion round: pass the epoch/version captured at
            # evaluation START (above), never re-derived here — see the
            # docstring on _upsert_kpi_latest_batch for why a fresh read at
            # write time is the wrong-number bug.
            _publish = await _upsert_kpi_latest_batch(
                db, model_id, kpi_objs, result_map,
                eval_version_id=_eval_version_id_at_start,
                eval_epoch=_eval_epoch_at_start,
                eval_started_at=_eval_started_at,
                eval_generation=_eval_generation,
            )

        elapsed = int((_time.monotonic_ns() - start_ms) / 1_000_000)
        return KPIBatchResponse(
            results=results,
            evaluation_ms=elapsed,
            kpi_latest_published=None if _publish is None else _publish.succeeded,
            kpi_latest_failed=0 if _publish is None else _publish.failed,
            kpi_latest_persisted=0 if _publish is None else _publish.persisted,
            kpi_latest_suppressed=0 if _publish is None else _publish.suppressed,
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")


# _kpi_latest_value_tuple and _upsert_kpi_latest_batch extracted to
# kpi_latest.py (Bug-7219); imported above.


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
    security_sink: set[str] | None = None,
    *,
    model=None,
    is_privileged: bool = True,
    allowed_measure_ids: list[UUID] | None = None,
    name_to_id: dict[str, UUID] | None = None,
    dim_scope: dict | None = None,
) -> KPIEvaluateResponse:
    """Evaluate a single KPI and return a response object.

    Tries the SQL compiler path first (faster, single query) and falls
    back to the Python evaluator if the expression can't be compiled.

    Bug-8486: *model*, *is_privileged*, *allowed_measure_ids*, *name_to_id*,
    and *dim_scope* are threaded through so that a target expression containing
    ``kpi()`` references can resolve them via ``_resolve_referenced_kpi_values``
    with full security-sink propagation. Without these, a ``kpi()`` in a target
    expression evaluated through the Python fallback silently resolves to
    ``None``, losing both the value and any deny-all governance from the
    referenced KPI.
    """
    ctx = await _build_evaluation_context(kpi, db, model_id)
    batch_security_rules = security_sink if security_sink is not None else set()

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

    # Bug-8682: the provider was built by the CALLER, before the per-KPI scope
    # above existed, so its on-miss ``_get_measure_value`` fallback issued an
    # UNFILTERED ``SELECT SUM("X") FROM "<model>"`` — an EMEA KPI answered with
    # the all-regions number, at HTTP 200, in a cell that looks legitimate. All
    # five ``_evaluate_single_kpi`` call sites share this defect, so the slice is
    # bound HERE (the one place that knows it) rather than at each call site.
    provider.measure_where_clause = batch_bd_where

    # F-017-14: a measure-based target must be sliced the same way as the value
    # (EMEA target vs EMEA revenue), not against the unfiltered grand total.
    # Resolved AFTER the scope above so the slice includes the request-level
    # filters merged in by Bug-5252 — reading only ``_compiled.where_clause``
    # here compared an unfiltered target against a request-filtered value.
    if kpi.target_type == "measure" and kpi.target_measure_id and ctx.target_value is None:
        target_m = await db.get(Measure, kpi.target_measure_id)
        if target_m and target_m.model_id == model_id:
            # Bug-6663: catch query failures so a broken target query
            # surfaces as target_value=None (Evaluation failed label)
            # rather than an unhandled 500.
            try:
                ctx.target_value = await _get_measure_value(
                    model_id, target_m.name, bearer,
                    model_slug, target_m.default_agg or "sum",
                    persona_id=persona_id,
                    where_clause=batch_bd_where,
                    security_sink=batch_security_rules,
                )
            except Exception as exc:  # noqa: BLE001
                if _is_model_not_deployed_error(exc):
                    return KPIEvaluateResponse(
                        kpi_id=kpi.id,
                        value=None,
                        status_label=_MODEL_NOT_DEPLOYED_LABEL,
                    )
                log.warning("Bug-6663: target measure query failed for KPI %s", kpi.id)
                ctx.target_value = None
                ctx.target_query_failed = True

    if not kpi.expression:
        return KPIEvaluateResponse(
            kpi_id=kpi.id,
            value=None,
            status_label="No expression configured",
        )

    # Try SQL compiler path first (matches single-evaluate endpoint pattern)
    if measure_map is not None:
        time_column = await _resolve_time_column(db, kpi, model_id)
        # Bug-8569: ONE evaluation context, shared by the value and target legs.
        # ``security_sink`` stays explicit at each call site (see the identical
        # note on the single-evaluate endpoint).
        value_leg_kwargs = dict(
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
        if kpi.kpi_type == "composite":
            # A composite's stored expression is a placeholder. Its value is
            # discarded and replaced by the weighted child score, so executing
            # it can only introduce a false governance verdict. Target queries
            # below remain authoritative and continue to populate this sink.
            value = None
        else:
            value = await _evaluate_expression_via_sql(
                kpi.expression, model_id, model_slug, bearer, measure_map, ctx,
                security_sink=batch_security_rules,
                **value_leg_kwargs,
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
        if value is _MODEL_NOT_DEPLOYED:
            return KPIEvaluateResponse(
                kpi_id=kpi.id,
                value=None,
                status_label=_MODEL_NOT_DEPLOYED_LABEL,
            )
        if _is_evaluation_failure(value):
            # Bug-8449: a row-security deny-all is a governance outcome, not a
            # broken expression. Labelling it "Evaluation failed" would also
            # make _child_error_reason treat every child of a composite as
            # ERRORED and flip the parent to degraded/error for what is really
            # a permissions decision.
            if row_security_denied_all(batch_security_rules):
                return KPIEvaluateResponse(
                    kpi_id=kpi.id,
                    value=None,
                    status_label=ROW_SECURITY_RESTRICTED_LABEL,
                    row_security_restricted=True,
                )
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
                security_sink=batch_security_rules,
            )
        if value is not _COMPILER_UNSUPPORTED:
            # SQL path succeeded — resolve target, then run the shared
            # post-score pipeline so single and batch indicators agree.
            target: float | None = ctx.target_value
            # Bug-6251: resolve a prior_period target via its synthesized
            # prior_period(...) expression when no explicit target_expression was
            # stored, matching the single-evaluate path.
            target_expr = kpi.target_expression or _prior_period_target_expression(kpi)
            if target_expr:
                # Bug-8569: same context as the value leg, minus the value
                # expression's own decomposition/share metadata.
                t_val = await _evaluate_expression_via_sql(
                    target_expr, model_id, model_slug, bearer,
                    measure_map, ctx,
                    security_sink=batch_security_rules,
                    **_target_leg_kwargs(value_leg_kwargs),
                )
                if t_val is _COMPILER_UNSUPPORTED:
                    # Bug-8486: resolve kpi() references in the target
                    # expression so their values and security metadata
                    # propagate into the Python fallback evaluation.
                    target_kpi_cache: dict[str, float | None] = {}
                    if model is not None and measure_map is not None:
                        target_kpi_cache = await _resolve_referenced_kpi_values(
                            target_expr,
                            db,
                            model_id,
                            model_slug,
                            bearer,
                            measure_map,
                            model=model,
                            is_privileged=is_privileged,
                            persona_id=persona_id,
                            allowed_measure_ids=allowed_measure_ids,
                            name_to_id=name_to_id,
                            dim_scope=dim_scope,
                            request_filter_predicates=request_filter_predicates,
                            security_sink=batch_security_rules,
                        )
                    if target_kpi_cache:
                        target_provider = _build_measure_provider(
                            model_id,
                            model_slug,
                            bearer,
                            measure_map or {},
                            kpi_value_cache=target_kpi_cache,
                            persona_id=persona_id,
                            security_sink=batch_security_rules,
                            where_clause=batch_bd_where,
                        )
                        target = await _evaluate_python_expression(
                            target_expr, ctx, target_provider,
                        )
                    else:
                        target = await _evaluate_python_expression(
                            target_expr, ctx, provider,
                        )
                elif t_val is _MODEL_NOT_DEPLOYED:
                    return KPIEvaluateResponse(
                        kpi_id=kpi.id,
                        value=None,
                        status_label=_MODEL_NOT_DEPLOYED_LABEL,
                    )
                elif t_val not in (
                    _EVALUATION_ERROR, _GUARD_REFUSED, _TI_NO_TIME_DIMENSION,
                ) and not isinstance(t_val, _TIDecompositionFailed):
                    target = t_val
            elif kpi.target_type == "static" and kpi.target_value is not None:
                target = float(kpi.target_value)

            return _stamp_row_security_restriction(
                await _finalize_kpi_response(
                    kpi, ctx, db, value, target,
                    model_id=model_id, model_slug=model_slug, bearer=bearer,
                    persona_id=persona_id, measure_map=measure_map,
                ),
                batch_security_rules,
            )

    # Fallback: Python evaluator
    try:
        result = await run_evaluation_pipeline(ctx, provider)
    except _ModelNotDeployedError:
        return KPIEvaluateResponse(
            kpi_id=kpi.id,
            value=None,
            status_label=_MODEL_NOT_DEPLOYED_LABEL,
        )
    return _stamp_row_security_restriction(
        _result_to_response(result), batch_security_rules,
    )


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
    """Mark a KPI as deployed (visible to JDBC/XMLA clients).

    F-017-01 (Opus R1): ``is_deployed`` is a publication flag layered on top of
    the deployed snapshot. Setting it on an UNDEPLOYED model has no serving
    effect (the snapshot authority withholds the KPI), but would leave an
    inconsistent governance state — refuse it so the modeller does not believe
    the KPI is live when it is not.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        # Bug-7982: serialise KPI definition/governance writes against Save/revert.
        kpi = await db.get(KPI, kpi_id)
        if kpi is None or kpi.model_id != model_id:
            raise HTTPException(status_code=404, detail="KPI not found")

        # F-017-01: the model must be deployed before a KPI can be published.
        model = await db.get(Model, model_id)
        if model is None or getattr(model, "deployed_version_id", None) is None:
            raise HTTPException(
                status_code=409,
                detail="Cannot deploy a KPI on an undeployed model — deploy the model first",
            )

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
            db, action="kpi.deploy", severity="warn",
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
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        # Bug-7982: serialise KPI definition/governance writes against Save/revert.
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
            db, action="kpi.undeploy", severity="warn",
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
