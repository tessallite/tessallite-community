"""KPI CRUD helper functions — pure logic, no router calls.

Extracted from kpis.py (Bug-7219) to reduce the monolith's responsibility
count. Contains governance guards, visibility checks, snapshot serialisation,
and presentation coercion logic that is called by CRUD route handlers.

All public symbols are re-exported from kpis.py so existing imports
continue to work.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from shared.db.models import KPI, Dimension
from shared.semantic.kpi_expression import (
    _collect_references,
    extract_measure_names,
    parse_kpi_expression,
)
from src.auth.rbac import caller_has_role


# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_CLOSER_RATIO_BANDS = [
    {"label": "Off Target",  "color": "#D32F2F", "min": None, "max": 0.80},
    {"label": "Near Target", "color": "#F57C00", "min": 0.80, "max": 0.90},
    {"label": "On Track",    "color": "#388E3C", "min": 0.90, "max": None},
]


def _coerce_closer_absolute(data: dict) -> None:
    """Coerce closer_is_better + absolute_value to percentage_of_target.

    One-sided absolute bands cannot express two-sided closeness, so
    closer KPIs must always use percentage_of_target evaluation.
    Both the type AND the bands must be converted -- flipping the type
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


# ---------------------------------------------------------------------------
# Persona visibility
# ---------------------------------------------------------------------------

def _kpi_visible_to_persona(
    kpi: KPI,
    allowed_measure_ids: list[UUID] | None,
    measure_name_to_id: dict[str, UUID],
    *,
    dim_scope: dict | None = None,
) -> bool:
    """Return True if all KPI lineage is within the persona scope.

    Checks MEASURE lineage (measure() refs, legacy value/goal/target
    measure-id bindings) AND DIMENSION lineage (dimension() refs,
    time_dimension_id) across the full transitive closure of kpi() refs.

    If *allowed_measure_ids* is ``None`` AND the dimension scope is
    unrestricted the persona is unrestricted.  Fail-closed: returns
    ``False`` on any lineage uncertainty (unparseable expression,
    unresolved kpi() dep, dangling legacy measure binding, dangling
    time_dimension_id).

    *dim_scope* is a dict with keys ``allowed_dimension_ids``,
    ``dimension_name_to_id``, ``measure_id_to_name``,
    ``dimension_id_to_name``, ``all_kpis_by_name``. When ``None``,
    only the measure check is performed (backward-compatible).

    Bug-6329 model-side: mirrors agent-service
    ``_kpi_outside_persona_scope`` semantics so a restricted persona
    cannot evaluate a KPI whose full lineage includes a hidden measure
    or a hidden dimension.
    """
    allowed_d_ids = None
    d_name_to_id: dict[str, UUID] = {}
    m_id_to_name: dict[UUID, str] = {}
    d_id_to_name: dict[UUID, str] = {}
    all_kpis_by_name: dict[str, KPI] = {}
    if dim_scope is not None:
        allowed_d_ids = dim_scope.get("allowed_dimension_ids")
        d_name_to_id = dim_scope.get("dimension_name_to_id") or {}
        m_id_to_name = dim_scope.get("measure_id_to_name") or {}
        d_id_to_name = dim_scope.get("dimension_id_to_name") or {}
        all_kpis_by_name = dim_scope.get("all_kpis_by_name") or {}

    if allowed_measure_ids is None and allowed_d_ids is None:
        return True

    allowed_m_set = set(allowed_measure_ids) if allowed_measure_ids is not None else None
    allowed_d_set = set(allowed_d_ids) if allowed_d_ids is not None else None
    d_name_lower_to_id = {k.lower(): v for k, v in d_name_to_id.items()}

    referenced_measures: set[str] = set()
    referenced_dim_ids: set[UUID] = set()
    seen: set = set()
    stack = [kpi]

    while stack:
        cur = stack.pop()
        cur_id = getattr(cur, "id", None)
        if cur_id in seen:
            continue
        seen.add(cur_id)

        for expr in (
            getattr(cur, "expression", None),
            getattr(cur, "target_expression", None),
        ):
            if not expr or not str(expr).strip():
                continue
            try:
                ast = parse_kpi_expression(str(expr))
                m_names, k_names, d_names = _collect_references(ast)
            except Exception:
                return False  # unparseable expression -> fail closed

            referenced_measures.update(m_names)

            for dname in d_names:
                did = d_name_lower_to_id.get(dname.lower())
                if did is None:
                    return False  # unresolved dimension ref -> fail closed
                referenced_dim_ids.add(did)

            for kname in k_names:
                child = all_kpis_by_name.get(kname)
                if child is None:
                    # When dim_scope is not provided, kpi() refs cannot
                    # be resolved but measure-only callers should not
                    # fail closed on that missing data.
                    if dim_scope is not None:
                        return False
                    continue
                if getattr(child, "id", None) not in seen:
                    stack.append(child)

        # Legacy measure-id bindings (value/goal/target)
        for attr in ("value_measure_id", "goal_measure_id", "target_measure_id"):
            raw = getattr(cur, attr, None)
            if raw is None:
                continue
            try:
                muid = raw if isinstance(raw, UUID) else UUID(str(raw))
            except (TypeError, ValueError):
                return False  # unparseable measure id -> fail closed
            if m_id_to_name:
                name = m_id_to_name.get(muid)
                if name is None:
                    return False  # dangling legacy binding -> fail closed
                referenced_measures.add(name)
            else:
                # Backward compat: when dim_scope not provided, fall
                # back to direct id check against allowed_m_set.
                if allowed_m_set is not None and muid not in allowed_m_set:
                    return False

        # time_dimension_id -> dimension lineage
        tdid = getattr(cur, "time_dimension_id", None)
        if tdid is not None:
            try:
                tduid = tdid if isinstance(tdid, UUID) else UUID(str(tdid))
            except (TypeError, ValueError):
                return False  # unparseable -> fail closed
            if dim_scope is not None:
                if tduid not in d_id_to_name:
                    return False  # dangling time-dimension binding -> fail closed
                referenced_dim_ids.add(tduid)

    # Measure scope check
    if allowed_m_set is not None:
        for name in referenced_measures:
            mid = measure_name_to_id.get(name)
            if mid is None or mid not in allowed_m_set:
                return False

    # Dimension scope check
    if allowed_d_set is not None:
        for did in referenced_dim_ids:
            if did not in allowed_d_set:
                return False

    return True


# ---------------------------------------------------------------------------
# Expression scope guard
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Governance guards
# ---------------------------------------------------------------------------

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

# Bug-6264: the certification lifecycle is a controlled enum, mirroring the
# frontend CertificationStatus type. A free string persisted here can render
# arbitrary markers in the BI catalogue.
_ALLOWED_CERTIFICATION_STATUSES = ("draft", "shared", "certified", "deprecated")
# Statuses that confer a "trusted/certified" signal in downstream BI clients
# (the XMLA catalogue renders "shared" as a [Certified] marker) and therefore
# require admin authority to set.
_PRIVILEGED_CERTIFICATION_STATUSES = ("certified", "deprecated", "shared")


async def _enforce_kpi_create_certification_guard(
    data: dict,
    current_user,
    *,
    db,
    project_id: UUID,
    model_id: UUID,
) -> None:
    """Keep KPI creation from minting privileged governance statuses.

    Bug-6264: born-certified/shared/deprecated is an admin-only exception —
    ordinary certification flows through the dedicated /certify endpoint. An
    effective modeler may create DRAFT KPIs and certify them through the
    separate /certify action; born-certification stays admin-only.

    Bug-9443 (Option A): authority is the caller's EFFECTIVE project/model
    binding, resolved through the SAME ``caller_has_role`` helper the /certify,
    /deprecate and PATCH-status endpoints use (F-017-12 / Bug-8728, decision
    #8) — NOT the coarse JWT token role. The prior check keyed on
    ``current_user.role``, so a JWT stamped ``role="admin"`` backing only a
    MODELER binding could born-certify while a plain modeler token was 403'd
    (RBAC-F2). ``caller_has_role`` preserves the human tenant_admin /
    canonical system_admin bypass, so those principals still born-certify;
    Bug-6264's admin-only rule is preserved, now measured by effective admin
    binding rather than token role.
    """
    requested_cert = data.get("certification_status")
    if requested_cert is None:
        return
    if requested_cert not in _ALLOWED_CERTIFICATION_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                "certification_status must be one of: "
                + ", ".join(_ALLOWED_CERTIFICATION_STATUSES)
            ),
        )
    if requested_cert in _PRIVILEGED_CERTIFICATION_STATUSES:
        is_admin = await caller_has_role(
            db, current_user, project_id, "admin", model_id,
        )
        if not is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only admins can set certified, shared, or deprecated status",
            )


# ---------------------------------------------------------------------------
# KPI snapshot serialisation
# ---------------------------------------------------------------------------

def _kpi_snapshot_dict(kpi: KPI) -> dict:
    """Serialise a KPI's versionable fields into a snapshot dict.

    This is the single source of truth for the snapshot shape, used both when
    writing a version row AND (Bug-6264/Bug-6613) when deciding on revert whether
    the reverted definition differs from the current one.
    """
    return {
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


# ---------------------------------------------------------------------------
# Time-dimension detection
# ---------------------------------------------------------------------------

_DATE_TYPE_MARKERS = ("date", "time", "timestamp")


def _is_time_dimension(dim: Dimension, data_types: dict[str, str]) -> bool:
    if dim.is_time_dim:
        return True
    data_type = data_types.get(str(dim.id), "")
    return any(marker in data_type for marker in _DATE_TYPE_MARKERS)
