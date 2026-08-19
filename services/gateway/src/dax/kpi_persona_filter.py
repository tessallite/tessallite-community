"""Persona lineage filtering for the XMLA/JDBC KPI catalogue (Bug-7227).

The gateway used to blank the ENTIRE KPI list for any persona carrying a
populated ``included_measure_ids`` allow-list (XMLA Discover MDSCHEMA_KPIS, XMLA
Execute KPI member functions, and JDBC inline KPI columns all did
``kpis = []``). A measure-restricted persona therefore saw ZERO KPIs in
Excel/Power BI, even the KPIs whose underlying measures it WAS allowed to read.

The query-router ``$KPIs`` data path already resolves this correctly with a
transitive-lineage allow-list gate (``_kpi_allowed_by_persona`` /
``_kpi_lineage_measure_ids`` in ``query-router/src/api/routes.py``). This module
mirrors that SAME policy on the gateway catalogue surface — it is not a second
policy. A KPI is advertised iff EVERY measure in its transitive lineage is in
the persona allow-list; anything whose lineage cannot be fully verified is
withheld (fail closed).

Scope note (deliberate): the CLS *column-value* channel
(``_kpi_cls_blocked_measure_ids``) stays enforced at query-router execution —
the gateway forwards ``persona_id`` on ``execute_query`` and cannot resolve
``PersonaTagRestriction`` without a direct-DB gateway bypass. This module
therefore covers the persona **measure allow-list** channel only, exactly the
split the gateway already uses for measures/dimensions in ``router_client``.
"""
from __future__ import annotations

from typing import Any

from shared.semantic.kpi_expression import _collect_references, parse_kpi_expression

# KPI dict keys that bind a measure by id DIRECTLY (no expression). Mirrors
# query-router ``_KPI_DIRECT_MEASURE_ID_FIELDS`` (routes.py). Includes the legacy
# v1 value/goal bindings and the v2 measure target.
_KPI_DIRECT_MEASURE_ID_FIELDS = (
    "value_measure_id",
    "goal_measure_id",
    "target_measure_id",
)

# KPI dict keys carrying a v2 DSL expression whose ``measure()`` refs feed the
# served value/target. Mirrors query-router ``_KPI_EXPRESSION_FIELDS``. The
# legacy v1 ``status_expression`` / ``trend_expression`` are DELIBERATELY
# excluded (they are DAX over the KPI's own value/goal, introducing no NEW
# measure lineage — including them would fail-close a clean v2 KPI carrying
# stale non-v2 text, an availability regression with no security benefit).
_KPI_EXPRESSION_FIELDS = (
    "expression",
    "target_expression",
)


def _extract_kpi_references(expression: Any) -> tuple[list[str], list[str], bool]:
    """Return ``(measure_names, kpi_names, parse_ok)`` for a KPI DSL expression.

    ``parse_ok=False`` signals a parse failure so the caller can FAIL CLOSED on
    an unparseable expression rather than serving it as if it had no lineage.
    Mirrors query-router ``_extract_kpi_references``.
    """
    if expression is None or not str(expression).strip():
        return [], [], True
    try:
        ast = parse_kpi_expression(str(expression))
    except Exception:
        return [], [], False
    measures, kpis, _dims = _collect_references(ast)
    return list(dict.fromkeys(measures)), list(dict.fromkeys(kpis)), True


def _kpi_lineage_measure_ids(
    kpi: dict[str, Any],
    kpi_by_name: dict[str, dict[str, Any]],
    measure_name_to_id: dict[str, str],
    children_by_parent: dict[str, list[dict[str, Any]]],
    _seen: set[str] | None = None,
) -> tuple[set[str], bool]:
    """Resolve the FULL transitive measure-id lineage of a KPI dict.

    Returns ``(measure_ids, fully_resolved)``. ``fully_resolved`` is False when
    ANY lineage channel could not be verified — an unparseable expression, an
    expression measure name that resolves to no id, or a nested ``kpi()`` whose
    name is not found — so the caller fails CLOSED.

    Dict-based mirror of query-router ``_kpi_lineage_measure_ids``. Channels:
    direct id bindings; ``expression`` / ``target_expression`` ``measure()``
    refs; nested ``kpi()`` refs (transitive, cycle-guarded); and composite
    children bound via ``parent_kpi_id`` (a composite's served value is the
    weighted score of its children, so its lineage is the union of theirs).
    """
    seen = _seen if _seen is not None else set()
    kid = kpi.get("id")
    if kid is not None:
        skid = str(kid)
        if skid in seen:
            # Cycle: already accounted for higher in the recursion.
            return set(), True
        seen.add(skid)

    measure_ids: set[str] = set()
    fully_resolved = True

    for attr in _KPI_DIRECT_MEASURE_ID_FIELDS:
        v = kpi.get(attr)
        if v is not None and str(v).strip():
            measure_ids.add(str(v))

    for field_name in _KPI_EXPRESSION_FIELDS:
        measures, kpis, parse_ok = _extract_kpi_references(kpi.get(field_name))
        if not parse_ok:
            fully_resolved = False
            continue
        for name in measures:
            mid = measure_name_to_id.get(name)
            if mid is None:
                fully_resolved = False
            else:
                measure_ids.add(mid)
        for kpi_name in kpis:
            nested = kpi_by_name.get(kpi_name) or kpi_by_name.get(kpi_name.lower())
            if nested is None:
                fully_resolved = False
                continue
            nested_ids, nested_ok = _kpi_lineage_measure_ids(
                nested, kpi_by_name, measure_name_to_id, children_by_parent, seen,
            )
            measure_ids |= nested_ids
            fully_resolved = fully_resolved and nested_ok

    if kid is not None:
        for child in children_by_parent.get(str(kid), []):
            child_ids, child_ok = _kpi_lineage_measure_ids(
                child, kpi_by_name, measure_name_to_id, children_by_parent, seen,
            )
            measure_ids |= child_ids
            fully_resolved = fully_resolved and child_ok

    return measure_ids, fully_resolved


def _kpi_allowed_by_persona(
    kpi: dict[str, Any],
    allowed_measure_ids: set[str] | None,
    measure_name_to_id: dict[str, str],
    kpi_by_name: dict[str, dict[str, Any]],
    children_by_parent: dict[str, list[dict[str, Any]]],
) -> bool:
    """Return True when a persona may see a KPI, given its measure lineage.

    ``allowed_measure_ids is None`` (or empty) means unrestricted — serve the
    KPI. Otherwise every measure in the KPI's transitive lineage must be in the
    allow-list. Fail closed on any lineage that cannot be fully verified. Mirrors
    the persona-allow-list gate of query-router ``_kpi_allowed_by_persona`` (the
    CLS column channel is enforced at query-router execution, see module docs).
    """
    if not allowed_measure_ids:
        return True

    referenced_ids, fully_resolved = _kpi_lineage_measure_ids(
        kpi, kpi_by_name, measure_name_to_id, children_by_parent,
    )
    if not fully_resolved:
        return False
    for mid in referenced_ids:
        if mid not in allowed_measure_ids:
            return False
    return True


def _measure_name_to_id(measures: list[dict[str, Any]]) -> dict[str, str]:
    """Map measure NAME -> id from the gateway measure metadata dicts."""
    out: dict[str, str] = {}
    for m in measures:
        name = m.get("name")
        mid = m.get("id")
        if name and mid is not None:
            out[str(name)] = str(mid)
    return out


def filter_kpis_for_persona(
    kpis: list[dict[str, Any]],
    measures: list[dict[str, Any]],
    allowed_measure_ids: set[str] | None,
) -> list[dict[str, Any]]:
    """Filter a KPI list to those a measure-restricted persona may see (Bug-7227).

    ``allowed_measure_ids`` is the persona ``included_measure_ids`` allow-list as
    a set of stringified ids. ``None`` or empty means unrestricted — the list is
    returned unchanged. Otherwise each KPI is kept iff its transitive measure
    lineage is fully contained in the allow-list; KPIs whose lineage cannot be
    verified are withheld (fail closed).

    The order of the input list is preserved.
    """
    if not allowed_measure_ids:
        return list(kpis)

    measure_name_to_id = _measure_name_to_id(measures)

    kpi_by_name: dict[str, dict[str, Any]] = {}
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for k in kpis:
        name = k.get("name")
        if name:
            # First writer wins on a name collision (deterministic).
            kpi_by_name.setdefault(str(name), k)
            kpi_by_name.setdefault(str(name).lower(), k)
        parent = k.get("parent_kpi_id")
        if parent is not None and str(parent).strip():
            children_by_parent.setdefault(str(parent), []).append(k)

    return [
        k
        for k in kpis
        if _kpi_allowed_by_persona(
            k, allowed_measure_ids, measure_name_to_id,
            kpi_by_name, children_by_parent,
        )
    ]
