"""Derive semantic lineage from the model's own definitions (Bug-8073).

Native lineage previously obtained every field and column node exclusively from
``LineageMapping`` rows. A repository-wide search finds readers, snapshot
serialisation and test fixtures for that table — but no production writer. An
ordinary modelling workflow (add a table, add a measure, add a dimension, add a
KPI) creates none, so the graph rendered sources, the model, aggregates and
targets and then simply stopped: a live ``modely`` read returned 225 nodes with
ZERO field or column nodes. A modeller assessing the blast radius of a change
saw a populated-looking graph that omitted exactly the dependencies that matter,
which is worse than an empty one — it invites a breaking edit with false
confidence.

The dependency information was never missing; it is on the definitions
themselves:

    ModelColumn --source_column_id--> Measure / Dimension --> Model
    ModelColumn --column_refs--> UserDefinedAttribute --> Measure / Dimension
    Measure --value/goal/target--> KPI

so lineage is DERIVED from those, and ``LineageMapping`` becomes what its name
suggests: optional enrichment. An explicit mapping row still contributes its
edge, and a mapping that names a column the definition does not reference is
kept as an override/addition rather than discarded.

This module is pure: it takes already-loaded ORM rows and returns
``(nodes, edges)``. No session, no request context — so the graph's shape can be
asserted against known answers instead of through a mocked endpoint.

Node ``type`` values are unchanged (``column`` / ``field``), so the existing
ReactFlow renderer needs no new case. A KPI is emitted as a ``field`` node whose
``meta["Field type"]`` is ``kpi``, matching how measures and dimensions are
already distinguished.
"""
from __future__ import annotations

from typing import Any, Iterable

from shared.schemas.domains.governance_advanced import LineageEdge, LineageNode
from shared.semantic.calculated_expression import (
    ExpressionValidationError,
    parse_expression,
)
from shared.semantic.kpi_dependency import extract_kpi_references
from shared.semantic.kpi_expression import extract_measure_names

FIELD_TYPE_MEASURE = "measure"
FIELD_TYPE_DIMENSION = "dimension"
FIELD_TYPE_KPI = "kpi"


def field_node_id(field_type: str, field_name: str) -> str:
    return f"field:{field_type}:{field_name}"


def column_node_id(column_id: Any) -> str:
    return f"col:{column_id}"


def _column_node(column: Any, table: Any) -> LineageNode:
    return LineageNode(
        id=column_node_id(column.id),
        type="column",
        label=getattr(column, "display_name", None) or column.column_name,
        description="Source column feeding semantic fields.",
        meta={
            "Table": (
                getattr(table, "display_name", None)
                or getattr(table, "alias", None)
                or getattr(table, "physical_name", "")
            ) if table is not None else "",
            "Column": column.column_name,
            "Data type": getattr(column, "data_type", "") or "",
            "Hidden": "yes" if getattr(column, "is_hidden", False) else "no",
        },
    )


def _field_node(field_type: str, name: str, meta: dict[str, str]) -> LineageNode:
    return LineageNode(
        id=field_node_id(field_type, name),
        type="field",
        label=name,
        description="Semantic field exposed by the model.",
        meta={"Field type": field_type, **meta},
    )


def _uda_column_ids(uda: Any) -> list[Any]:
    """Column ids a user-defined attribute reads.

    A UDA is an expression over one or more physical columns; its
    ``column_refs`` rows are the resolved set. A measure or dimension bound to a
    UDA therefore depends on every one of them, not on a single source column.
    """
    refs = getattr(uda, "column_refs", None) or []
    return [getattr(r, "column_id", None) for r in refs if getattr(r, "column_id", None)]


def build_semantic_lineage(
    *,
    model_id: Any,
    measures: Iterable[Any],
    dimensions: Iterable[Any],
    kpis: Iterable[Any],
    udas: Iterable[Any],
    columns_by_id: dict[Any, tuple[Any, Any]],
    lineage_rows: Iterable[Any] = (),
) -> tuple[list[LineageNode], list[LineageEdge]]:
    """Build the semantic half of the lineage graph.

    ``columns_by_id`` maps ``ModelColumn.id -> (ModelColumn, ModelTable)`` for
    every column of this model; a referenced id absent from it is skipped rather
    than emitted as a dangling node (it belongs to another model, or the column
    was deleted and the FK nulled).

    ``lineage_rows`` are ``LineageMapping`` rows, applied as ENRICHMENT on top of
    the derived graph: a row for a field that derivation already produced adds
    its column edge, and a row for a field derivation did not produce still
    creates that field. Nothing depends on these rows existing.
    """
    nodes: list[LineageNode] = []
    edges: list[LineageEdge] = []
    emitted_columns: set[str] = set()
    emitted_fields: set[str] = set()
    emitted_edges: set[tuple[str, str, str]] = set()

    uda_by_id = {getattr(u, "id", None): u for u in udas}
    measures = list(measures)
    kpis = list(kpis)
    measure_ids_by_name: dict[str, list[Any]] = {}
    for measure in measures:
        name = getattr(measure, "name", None)
        measure_id = getattr(measure, "id", None)
        if name and measure_id is not None:
            measure_ids_by_name.setdefault(str(name).casefold(), []).append(measure_id)

    def _resolve_measure_ids(names: Iterable[str]) -> list[Any]:
        """Resolve parser-produced names to unambiguous stable IDs."""
        resolved: list[Any] = []
        for name in names:
            candidates = measure_ids_by_name.get(str(name).casefold(), [])
            if len(candidates) == 1 and candidates[0] not in resolved:
                resolved.append(candidates[0])
        return resolved

    kpi_ids_by_name: dict[str, list[Any]] = {}
    for kpi in kpis:
        name = getattr(kpi, "name", None)
        kpi_id = getattr(kpi, "id", None)
        if name and kpi_id is not None:
            kpi_ids_by_name.setdefault(str(name).casefold(), []).append(kpi_id)

    def _resolve_kpi_ids(names: Iterable[str]) -> list[Any]:
        """Resolve KPI names only when their case-insensitive match is unique."""
        resolved: list[Any] = []
        for name in names:
            candidates = kpi_ids_by_name.get(str(name).casefold(), [])
            if len(candidates) == 1 and candidates[0] not in resolved:
                resolved.append(candidates[0])
        return resolved

    def _add_column(column_id: Any) -> str | None:
        entry = columns_by_id.get(column_id)
        if entry is None:
            return None
        column, table = entry
        nid = column_node_id(column.id)
        if nid not in emitted_columns:
            emitted_columns.add(nid)
            nodes.append(_column_node(column, table))
        return nid

    def _add_field(field_type: str, name: str, meta: dict[str, str]) -> str:
        nid = field_node_id(field_type, name)
        if nid not in emitted_fields:
            emitted_fields.add(nid)
            nodes.append(_field_node(field_type, name, meta))
            _add_edge(nid, str(model_id), "defined by")
        return nid

    def _add_edge(source: str, target: str, label: str) -> None:
        key = (source, target, label)
        if key in emitted_edges:
            return
        emitted_edges.add(key)
        edges.append(LineageEdge(source=source, target=target, label=label))

    def _link_definition_columns(field_nid: str, definition: Any) -> None:
        """Wire every physical column a definition depends on to its field."""
        direct = [
            getattr(definition, "source_column_id", None),
            getattr(definition, "display_column_id", None),
            getattr(definition, "semi_additive_account_column_id", None),
        ]
        uda_id = getattr(definition, "user_defined_attribute_id", None)
        if uda_id is not None:
            direct.extend(_uda_column_ids(uda_by_id.get(uda_id)))
        for column_id in direct:
            if column_id is None:
                continue
            column_nid = _add_column(column_id)
            if column_nid is not None:
                _add_edge(column_nid, field_nid, "feeds")

    # --- Measures -----------------------------------------------------------
    measure_nid_by_id: dict[Any, str] = {}
    for measure in measures:
        name = getattr(measure, "name", None)
        if not name:
            continue
        meta = {
            "Aggregation": getattr(measure, "default_agg", "") or "",
            "Measure type": getattr(measure, "measure_type", "") or "",
            "Object ID": str(getattr(measure, "id", "")),
        }
        if getattr(measure, "is_invalid", False):
            meta["Invalid"] = getattr(measure, "invalid_reason", None) or "yes"
        nid = _add_field(FIELD_TYPE_MEASURE, name, meta)
        measure_nid_by_id[getattr(measure, "id", None)] = nid
        _link_definition_columns(nid, measure)

    # A calculated measure reads other measures. Those edges are what make an
    # impact assessment correct: editing a base measure changes every calc that
    # references it, and a modeller cannot see that from the base measure alone.
    for measure in measures:
        nid = measure_nid_by_id.get(getattr(measure, "id", None))
        if nid is None:
            continue
        variant_of = getattr(measure, "variant_of_measure_id", None)
        if variant_of is not None:
            base_nid = measure_nid_by_id.get(variant_of)
            if base_nid is not None:
                _add_edge(base_nid, nid, "feeds")
        expression = getattr(measure, "expression", None)
        if expression:
            try:
                referenced_names = parse_expression(expression).referenced_names
            except ExpressionValidationError:
                referenced_names = ()
            for referenced_id in _resolve_measure_ids(referenced_names):
                other_nid = measure_nid_by_id.get(referenced_id)
                if other_nid is not None and other_nid != nid:
                    _add_edge(other_nid, nid, "feeds")

    # --- Dimensions ---------------------------------------------------------
    for dimension in dimensions:
        name = getattr(dimension, "name", None)
        if not name:
            continue
        meta = {"Object ID": str(getattr(dimension, "id", ""))}
        if getattr(dimension, "is_time_dim", False):
            meta["Time dimension"] = "yes"
        if getattr(dimension, "is_invalid", False):
            meta["Invalid"] = getattr(dimension, "invalid_reason", None) or "yes"
        nid = _add_field(FIELD_TYPE_DIMENSION, name, meta)
        _link_definition_columns(nid, dimension)

    # --- KPIs ---------------------------------------------------------------
    kpi_nid_by_id: dict[Any, str] = {}
    for kpi in kpis:
        name = getattr(kpi, "name", None)
        if not name:
            continue
        meta = {
            "KPI type": getattr(kpi, "kpi_type", "") or "",
            "Object ID": str(getattr(kpi, "id", "")),
        }
        nid = _add_field(FIELD_TYPE_KPI, name, meta)
        kpi_nid_by_id[getattr(kpi, "id", None)] = nid

    for kpi in kpis:
        nid = kpi_nid_by_id.get(getattr(kpi, "id", None))
        if nid is None:
            continue
        for attr in ("value_measure_id", "goal_measure_id", "target_measure_id"):
            measure_id = getattr(kpi, attr, None)
            if measure_id is None:
                continue
            measure_nid = measure_nid_by_id.get(measure_id)
            if measure_nid is not None:
                _add_edge(measure_nid, nid, "feeds")
        for expression_attr in ("expression", "target_expression"):
            expression = getattr(kpi, expression_attr, None)
            if not expression:
                continue
            for measure_id in _resolve_measure_ids(extract_measure_names(expression)):
                measure_nid = measure_nid_by_id.get(measure_id)
                if measure_nid is not None:
                    _add_edge(measure_nid, nid, "feeds")
            try:
                referenced_kpis = extract_kpi_references(expression)
            except Exception:
                referenced_kpis = ()
            for referenced_id in _resolve_kpi_ids(referenced_kpis):
                referenced_nid = kpi_nid_by_id.get(referenced_id)
                if referenced_nid is not None:
                    _add_edge(referenced_nid, nid, "feeds")

    # --- Explicit mappings as ENRICHMENT ------------------------------------
    for row in lineage_rows:
        field_name = getattr(row, "semantic_field_name", None)
        field_type = getattr(row, "semantic_field_type", None)
        if not field_name or not field_type:
            continue
        nid = _add_field(field_type, field_name, {})
        column_id = getattr(row, "source_column_id", None)
        if column_id is None:
            continue
        column_nid = _add_column(column_id)
        if column_nid is not None:
            _add_edge(column_nid, nid, "feeds")

    return nodes, edges
