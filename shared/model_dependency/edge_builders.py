"""Materialize every edge family in the catalogue (spec §5.3).

Each ``_build_*`` function reads ONE snapshot family and registers its nodes +
edges on the ``GraphBuilder``. ``build_all_edges`` invokes them all. This is the
single place the complete edge catalogue is enforced; a missing family here is a
missing dependency (the exact Fable failure mode), so every §5.3 row has a
function and the coverage is asserted by ``test_edge_catalogue_coverage``.

Edge direction is always dependency -> dependent (spec §5.1). References that
point at a missing ID become ``unresolved_reference`` nodes (spec §5.5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .types import (
    DependencyEdge,
    EdgeKind,
    NodeKey,
    ObjectType,
)

if TYPE_CHECKING:  # pragma: no cover
    from .graph import GraphBuilder

# Closed value sets (mirror the Literals in types.py). Validated in ``_edge`` so a
# mis-encoded call site (e.g. "cleanup" in the policy slot) fails early rather
# than flowing to the Pydantic consumer and 500-ing the API.
_VALID_STRENGTH = frozenset({"hard", "soft"})
_VALID_POLICY = frozenset({"restrict", "cascade", "detach", "invalidate", "recompute"})
_VALID_EFFECT = frozenset({
    "breaks_reference", "changes_semantics", "loses_coverage", "loses_visibility",
    "cascade_deleted", "detached", "stale", "cleanup",
})
_VALID_RESOLUTION = frozenset({"foreign_key", "persisted_ref", "name", "json", "derived"})


def _edge(
    b: "GraphBuilder",
    dependency: NodeKey,
    dependent: NodeKey,
    kind: EdgeKind,
    source_field: str,
    strength,
    delete_policy,
    effect,
    resolution,
    evidence: Optional[dict[str, str]] = None,
) -> None:
    # ValueError, not assert: asserts are stripped under ``python -O`` which would
    # silently re-open the out-of-enum policy bug in an optimized deployment.
    if strength not in _VALID_STRENGTH:
        raise ValueError(f"invalid strength {strength!r} for {kind}")
    if delete_policy not in _VALID_POLICY:
        raise ValueError(f"invalid delete_policy {delete_policy!r} for {kind}")
    if effect not in _VALID_EFFECT:
        raise ValueError(f"invalid effect {effect!r} for {kind}")
    if resolution not in _VALID_RESOLUTION:
        raise ValueError(f"invalid resolution {resolution!r} for {kind}")
    b.add_edge(
        DependencyEdge(
            dependency=dependency,
            dependent=dependent,
            kind=kind,
            source_field=source_field,
            strength=strength,
            delete_policy=delete_policy,
            effect=effect,
            resolution=resolution,
            evidence=evidence or {},
        )
    )


def _ref_or_unresolved(
    b: "GraphBuilder",
    owner: NodeKey,
    expected_type: ObjectType,
    object_id: Optional[str],
    field_name: str,
) -> Optional[NodeKey]:
    """Return the node key for ``object_id`` if it exists, else register an
    unresolved_reference node owned by ``owner`` and return None (spec §5.5)."""
    if not object_id:
        return None
    key = NodeKey(
        tenant_id=owner.tenant_id,
        project_id=owner.project_id,
        model_id=owner.model_id,
        object_type=expected_type,
        object_id=object_id,
    )
    if key in b._nodes:  # noqa: SLF001 - builder-internal by design
        return key
    # Register an unresolved reference so the guard can fail closed, AND connect
    # it to its owner so it is reachable in traversal (a graph island would make
    # summary.unresolved always 0 and the fail-closed _classify branch dead code).
    unresolved_id = f"{owner.object_type.value}:{owner.object_id}:{field_name}"
    ukey = b.add_node(
        ObjectType.UNRESOLVED_REFERENCE,
        unresolved_id,
        name=field_name,
        display_name=field_name,
        valid=False,
        unresolved_reason=f"missing_{expected_type.value}",
    )
    # owner -> unresolved (hard/CASCADE): the broken reference belongs to the
    # owner and dies WITH it. cascade (not restrict) so deleting the owner does
    # NOT block on its own stale reference (§5.5 fail-closed applies only when the
    # delete TARGET could match the unresolved ref, i.e. inspect/change of a
    # surviving owner — the simulator classifies a cascaded unresolved node as
    # informational, and a surviving one as hard_break).
    _edge(b, owner, ukey, EdgeKind.UNRESOLVED_REFERENCE, field_name,
          "hard", "cascade", "breaks_reference", "derived",
          {"expected_type": expected_type.value, "missing_id": object_id})
    b.add_diagnostic(
        {
            "type": "unresolved_reference",
            "owner": owner.token(),
            "field": field_name,
            "expected_type": expected_type.value,
            "missing_id": object_id,
        }
    )
    return None


# --- node registration pass ------------------------------------------------


def _register_nodes(b: "GraphBuilder") -> None:
    s = b._s  # noqa: SLF001
    b.add_node(ObjectType.MODEL, s.model_id, name="model", display_name="Model")
    for r in s.sources:
        b.add_node(ObjectType.DATA_SOURCE, r.id, r.name, r.display_name)
    for r in s.targets:
        b.add_node(ObjectType.DATA_TARGET, r.id, r.name, r.display_name)
    for r in s.tables:
        b.add_node(ObjectType.TABLE, r.id, r.name, r.display_name,
                   container_ids={"source_id": r.source_id})
    for r in s.columns:
        b.add_node(ObjectType.COLUMN, r.id, r.name, r.display_name,
                   container_ids={"table_id": r.table_id})
    for r in s.calendars:
        b.add_node(ObjectType.CALENDAR, r.id, r.name, r.display_name)
    for r in s.udas:
        b.add_node(ObjectType.USER_DEFINED_ATTRIBUTE, r.id, r.name, r.display_name,
                   container_ids={"table_id": r.table_id})
    for r in s.dimensions:
        b.add_node(ObjectType.DIMENSION, r.id, r.name, r.display_name)
    for r in s.hierarchies:
        b.add_node(ObjectType.HIERARCHY, r.id, r.name, r.display_name)
    for r in s.hierarchy_levels:
        b.add_node(ObjectType.HIERARCHY_LEVEL, r.id, r.name, r.display_name,
                   container_ids={"hierarchy_id": r.hierarchy_id})
    for r in s.measures:
        b.add_node(ObjectType.MEASURE, r.id, r.name, r.display_name)
    for r in s.relationships:
        b.add_node(ObjectType.RELATIONSHIP, r.id, r.name, r.display_name)
    for r in s.aggregates:
        b.add_node(ObjectType.AGGREGATE, r.id, r.name, r.display_name)
    for r in s.aggregate_columns:
        b.add_node(ObjectType.AGGREGATE_COLUMN, r.id, r.name, r.display_name,
                   container_ids={"aggregate_id": r.aggregate_id})
    for r in s.pockets:
        b.add_node(ObjectType.POCKET, r.id, r.name, r.display_name)
    for r in s.kpis:
        b.add_node(ObjectType.KPI, r.id, r.name, r.display_name)
    for r in s.drill_through_sets:
        b.add_node(ObjectType.DRILL_THROUGH_SET, r.id, r.name, r.display_name)
    for r in s.named_lists:
        b.add_node(ObjectType.NAMED_LIST, r.id, r.name, r.display_name)
    for r in s.saved_queries:
        b.add_node(ObjectType.SAVED_QUERY, r.id, r.name, r.display_name)
    for r in s.saved_pivots:
        b.add_node(ObjectType.SAVED_PIVOT_VIEW, r.id, r.name, r.display_name)
    for r in s.data_tags:
        b.add_node(ObjectType.DATA_TAG, r.id, r.name, r.display_name)
    for r in s.personas:
        b.add_node(ObjectType.PERSONA, r.id, r.name, r.display_name)
    for r in s.project_persona_scopes:
        b.add_node(ObjectType.PROJECT_PERSONA_SCOPE, r.id, r.name, r.display_name)
    for r in s.row_security_rules:
        b.add_node(ObjectType.ROW_SECURITY_RULE, r.id, r.name, r.display_name)
    for r in s.model_parameters:
        b.add_node(ObjectType.MODEL_PARAMETER, r.id, r.name, r.display_name)
    for r in s.glossary_attachments:
        b.add_node(ObjectType.GLOSSARY_ATTACHMENT, r.id, r.name, r.display_name)
    for r in s.data_quality_rules:
        b.add_node(ObjectType.DATA_QUALITY_RULE, r.id, r.name, r.display_name)
    for r in s.scratchpad_measures:
        b.add_node(ObjectType.SCRATCHPAD_MEASURE, r.id, r.name, r.display_name)
    for r in s.lineage_mappings:
        b.add_node(ObjectType.LINEAGE_MAPPING, r.id, r.name, r.display_name)
    for r in s.translations:
        b.add_node(ObjectType.TRANSLATION, r.id, r.name, r.display_name)
    for r in s.user_preferences:
        b.add_node(ObjectType.USER_PREFERENCE, r.id, r.name, r.display_name)
    for r in s.alias_maps:
        b.add_node(ObjectType.MODEL_ALIAS_MAP, r.id, r.name, r.display_name)
    for r in s.cross_model_recipes:
        b.add_node(ObjectType.CROSS_MODEL_RECIPE, r.id, r.name, r.display_name)
    for r in s.agent_groundings:
        b.add_node(ObjectType.AGENT_MODEL_LINK, r.id, r.name, r.display_name)
    # Cross-model measures live in their own model. Register their nodes so
    # cross-model edges have a valid dependent (spec §12.3).
    for other_model_id, other_measure_id, other_name, _ref in s.cross_model_measures:
        b.add_node(ObjectType.MEASURE, other_measure_id, other_name, other_name,
                   model_id=other_model_id)


# --- edge families ---------------------------------------------------------


def _n(b, ot: ObjectType, oid: str, model_id=None) -> NodeKey:
    return NodeKey(
        tenant_id=b._s.tenant_id, project_id=b._s.project_id,  # noqa: SLF001
        model_id=model_id or b._s.model_id, object_type=ot, object_id=oid,  # noqa: SLF001
    )


def _build_containment(b: "GraphBuilder") -> None:
    """Model -> every contained object (hard/cascade); source/table/column/uda,
    hierarchy->level, aggregate->aggregate_column containment (spec §5.3)."""
    s = b._s  # noqa: SLF001
    model = _n(b, ObjectType.MODEL, s.model_id)
    contained = [
        (ObjectType.DATA_SOURCE, [r.id for r in s.sources]),
        (ObjectType.DATA_TARGET, [r.id for r in s.targets]),
        (ObjectType.TABLE, [r.id for r in s.tables]),
        (ObjectType.USER_DEFINED_ATTRIBUTE, [r.id for r in s.udas]),
        (ObjectType.DIMENSION, [r.id for r in s.dimensions]),
        (ObjectType.HIERARCHY, [r.id for r in s.hierarchies]),
        (ObjectType.MEASURE, [r.id for r in s.measures]),
        (ObjectType.RELATIONSHIP, [r.id for r in s.relationships]),
        (ObjectType.AGGREGATE, [r.id for r in s.aggregates]),
        (ObjectType.POCKET, [r.id for r in s.pockets]),
        (ObjectType.KPI, [r.id for r in s.kpis]),
        (ObjectType.NAMED_LIST, [r.id for r in s.named_lists]),
        (ObjectType.DRILL_THROUGH_SET, [r.id for r in s.drill_through_sets]),
        (ObjectType.ROW_SECURITY_RULE, [r.id for r in s.row_security_rules]),
        (ObjectType.DATA_TAG, [r.id for r in s.data_tags]),
        (ObjectType.MODEL_PARAMETER, [r.id for r in s.model_parameters]),
        (ObjectType.SAVED_QUERY, [r.id for r in s.saved_queries]),
        (ObjectType.SAVED_PIVOT_VIEW, [r.id for r in s.saved_pivots]),
        (ObjectType.CALENDAR, [r.id for r in s.calendars]),
        # Model-scoped families with a CASCADE FK on model_id: without these,
        # a model delete under-reports its cascade AND (once enforcing) wrongly
        # blocks on its own persona's inbound CLS restriction as a survivor.
        (ObjectType.PERSONA, [r.id for r in s.personas]),
        (ObjectType.SCRATCHPAD_MEASURE, [r.id for r in s.scratchpad_measures]),
        (ObjectType.MODEL_ALIAS_MAP, [r.id for r in s.alias_maps]),
        (ObjectType.DATA_QUALITY_RULE, [r.id for r in s.data_quality_rules]),
        (ObjectType.LINEAGE_MAPPING, [r.id for r in s.lineage_mappings]),
        (ObjectType.TRANSLATION, [r.id for r in s.translations]),
        (ObjectType.USER_PREFERENCE, [r.id for r in s.user_preferences]),
    ]
    for ot, ids in contained:
        for oid in ids:
            _edge(b, model, _n(b, ot, oid), EdgeKind.CONTAINMENT, "model_id",
                  "hard", "cascade", "cascade_deleted", "foreign_key")
    # Glossary attachments cascade with the model TRANSITIVELY via their glossary
    # entry (no direct model_id column), so the evidence is derived, not a
    # foreign_key on model_id (accurate §5.3 evidence labelling).
    for r in s.glossary_attachments:
        _edge(b, model, _n(b, ObjectType.GLOSSARY_ATTACHMENT, r.id),
              EdgeKind.CONTAINMENT, "glossary_entry", "hard", "cascade",
              "cascade_deleted", "derived")
    # source -> table, source -> calendar, table -> column, table -> uda
    for r in s.tables:
        _edge(b, _n(b, ObjectType.DATA_SOURCE, r.source_id),
              _n(b, ObjectType.TABLE, r.id), EdgeKind.CONTAINMENT, "source_id",
              "hard", "cascade", "cascade_deleted", "foreign_key")
    for r in s.calendars:
        if r.source_id:
            _edge(b, _n(b, ObjectType.DATA_SOURCE, r.source_id),
                  _n(b, ObjectType.CALENDAR, r.id), EdgeKind.CONTAINMENT, "source_id",
                  "hard", "cascade", "cascade_deleted", "foreign_key")
    for r in s.columns:
        _edge(b, _n(b, ObjectType.TABLE, r.table_id),
              _n(b, ObjectType.COLUMN, r.id), EdgeKind.CONTAINMENT, "model_table_id",
              "hard", "cascade", "cascade_deleted", "foreign_key")
    for r in s.udas:
        _edge(b, _n(b, ObjectType.TABLE, r.table_id),
              _n(b, ObjectType.USER_DEFINED_ATTRIBUTE, r.id), EdgeKind.CONTAINMENT,
              "table_id", "hard", "cascade", "cascade_deleted", "foreign_key")
    for r in s.hierarchy_levels:
        _edge(b, _n(b, ObjectType.HIERARCHY, r.hierarchy_id),
              _n(b, ObjectType.HIERARCHY_LEVEL, r.id), EdgeKind.CONTAINMENT,
              "hierarchy_id", "hard", "cascade", "cascade_deleted", "foreign_key")
    for r in s.aggregate_columns:
        _edge(b, _n(b, ObjectType.AGGREGATE, r.aggregate_id),
              _n(b, ObjectType.AGGREGATE_COLUMN, r.id), EdgeKind.CONTAINMENT,
              "aggregate_id", "hard", "cascade", "cascade_deleted", "foreign_key")


def _build_connections_targets(b: "GraphBuilder") -> None:
    """Project connection -> source/target; data target -> aggregate/pocket/model."""
    s = b._s  # noqa: SLF001
    for r in s.sources:
        if r.project_connection_id:
            b.add_node(ObjectType.PROJECT_CONNECTION, r.project_connection_id,
                       "connection", "Connection")
            _edge(b, _n(b, ObjectType.PROJECT_CONNECTION, r.project_connection_id),
                  _n(b, ObjectType.DATA_SOURCE, r.id), EdgeKind.CONNECTION_BINDING,
                  "project_connection_id", "hard", "restrict", "breaks_reference",
                  "foreign_key")
    for r in s.targets:
        if r.project_connection_id:
            b.add_node(ObjectType.PROJECT_CONNECTION, r.project_connection_id,
                       "connection", "Connection")
            _edge(b, _n(b, ObjectType.PROJECT_CONNECTION, r.project_connection_id),
                  _n(b, ObjectType.DATA_TARGET, r.id), EdgeKind.CONNECTION_BINDING,
                  "project_connection_id", "hard", "restrict", "breaks_reference",
                  "foreign_key")
    # data target -> aggregate / pocket (restrict), and target -> model (detach).
    for r in s.aggregates:
        if r.target_id:
            _edge(b, _n(b, ObjectType.DATA_TARGET, r.target_id),
                  _n(b, ObjectType.AGGREGATE, r.id), EdgeKind.TARGET_BINDING,
                  "target_id", "hard", "restrict", "breaks_reference", "foreign_key")
    for r in s.pockets:
        if r.target_id:
            _edge(b, _n(b, ObjectType.DATA_TARGET, r.target_id),
                  _n(b, ObjectType.POCKET, r.id), EdgeKind.TARGET_BINDING,
                  "target_id", "hard", "restrict", "breaks_reference", "foreign_key")
    # Model.target_id -> model (soft/detach) is emitted by
    # _build_target_model_binding from snapshot.model_default_target_id.


def _build_calendar_alias(b: "GraphBuilder") -> None:
    """Calendar -> alias table (hard/restrict) — the Fable-caught edge (§5.3)."""
    s = b._s  # noqa: SLF001
    for r in s.tables:
        if r.calendar_table_id:
            _edge(b, _n(b, ObjectType.CALENDAR, r.calendar_table_id),
                  _n(b, ObjectType.TABLE, r.id), EdgeKind.CALENDAR_ALIAS_BINDING,
                  "calendar_table_id", "hard", "restrict", "breaks_reference",
                  "foreign_key")


def _build_relationships(b: "GraphBuilder") -> None:
    """Table/column -> relationship endpoints (spec §5.3)."""
    s = b._s  # noqa: SLF001
    for r in s.relationships:
        rel = _n(b, ObjectType.RELATIONSHIP, r.id)
        for tid, fld in ((r.left_table_id, "left_table_id"), (r.right_table_id, "right_table_id")):
            _edge(b, _n(b, ObjectType.TABLE, tid), rel, EdgeKind.RELATIONSHIP_ENDPOINT,
                  fld, "hard", "cascade", "cascade_deleted", "foreign_key")
        for cid, fld in ((r.left_column_id, "left_column_id"), (r.right_column_id, "right_column_id")):
            if cid:
                _edge(b, _n(b, ObjectType.COLUMN, cid), rel, EdgeKind.RELATIONSHIP_ENDPOINT,
                      fld, "hard", "restrict", "breaks_reference", "foreign_key")


def _build_udas(b: "GraphBuilder") -> None:
    """Column -> UDA via persisted UserDefinedAttributeColumnRef (spec §5.3)."""
    s = b._s  # noqa: SLF001
    for r in s.udas:
        uda = _n(b, ObjectType.USER_DEFINED_ATTRIBUTE, r.id)
        for cid in r.column_ref_ids:
            col = _ref_or_unresolved(b, uda, ObjectType.COLUMN, cid, "column_ref")
            if col:
                _edge(b, col, uda, EdgeKind.UDA_COLUMN_REFERENCE, "column_id",
                      "hard", "restrict", "breaks_reference", "persisted_ref")


def _build_dimensions(b: "GraphBuilder") -> None:
    """Column/UDA -> dimension bindings + calculated dimension refs (spec §5.3)."""
    s = b._s  # noqa: SLF001
    for r in s.dimensions:
        dim = _n(b, ObjectType.DIMENSION, r.id)
        for cid, fld in ((r.source_column_id, "source_column_id"),
                         (r.display_column_id, "display_column_id")):
            col = _ref_or_unresolved(b, dim, ObjectType.COLUMN, cid, fld)
            if col:
                _edge(b, col, dim, EdgeKind.DIMENSION_BINDING, fld,
                      "hard", "restrict", "breaks_reference", "foreign_key")
        uda = _ref_or_unresolved(b, dim, ObjectType.USER_DEFINED_ATTRIBUTE,
                                 r.user_defined_attribute_id, "user_defined_attribute_id")
        if uda:
            _edge(b, uda, dim, EdgeKind.DIMENSION_BINDING, "user_defined_attribute_id",
                  "hard", "restrict", "breaks_reference", "foreign_key")
        # Calculated dimension: expression tables resolved to table IDs.
        for tid in r.calc_expression_tables:
            tkey = _ref_or_unresolved(b, dim, ObjectType.TABLE, tid, "calc_expression_tables")
            if tkey:
                _edge(b, tkey, dim, EdgeKind.CALCULATED_DIMENSION_REFERENCE,
                      "calc_expression_tables", "hard", "restrict", "breaks_reference",
                      "derived")
        # Calculated dimension: expression column references resolved to column IDs.
        # Deleting a column named only inside the expression must break the dim.
        for cid in r.calc_expression_column_ids:
            ckey = _ref_or_unresolved(b, dim, ObjectType.COLUMN, cid, "calc_expression")
            if ckey:
                _edge(b, ckey, dim, EdgeKind.CALCULATED_DIMENSION_REFERENCE,
                      "calc_expression", "hard", "restrict", "breaks_reference",
                      "derived")
        # Legacy hierarchy JSONB field: diagnostic only (spec §5.3 rule).
        if r.hierarchy_json:
            b.add_diagnostic({"type": "legacy_field_present", "owner": dim.token(),
                              "field": "hierarchy"})


def _build_measures(b: "GraphBuilder") -> None:
    """Column/UDA/table/hierarchy/calendar/variant/calc/cross-model -> measure."""
    s = b._s  # noqa: SLF001
    for r in s.measures:
        m = _n(b, ObjectType.MEASURE, r.id)
        for cid, fld in (
            (r.source_column_id, "source_column_id"),
            (r.semi_additive_account_column_id, "semi_additive_account_column_id"),
            (r.resolved_date_col_id, "resolved_date_col_id"),
            (r.date_dimension_column_id, "date_dimension_column_id"),
        ):
            col = _ref_or_unresolved(b, m, ObjectType.COLUMN, cid, fld)
            if col:
                _edge(b, col, m, EdgeKind.MEASURE_BINDING, fld,
                      "hard", "restrict", "breaks_reference", "foreign_key")
        uda = _ref_or_unresolved(b, m, ObjectType.USER_DEFINED_ATTRIBUTE,
                                 r.user_defined_attribute_id, "user_defined_attribute_id")
        if uda:
            _edge(b, uda, m, EdgeKind.MEASURE_BINDING, "user_defined_attribute_id",
                  "hard", "restrict", "breaks_reference", "foreign_key")
        tbl = _ref_or_unresolved(b, m, ObjectType.TABLE, r.calendar_model_table_id,
                                 "calendar_model_table_id")
        if tbl:
            _edge(b, tbl, m, EdgeKind.MEASURE_BINDING, "calendar_model_table_id",
                  "hard", "restrict", "breaks_reference", "foreign_key")
        hier = _ref_or_unresolved(b, m, ObjectType.HIERARCHY, r.hierarchy_id, "hierarchy_id")
        if hier:
            _edge(b, hier, m, EdgeKind.HIERARCHY_MEASURE_BINDING, "hierarchy_id",
                  "hard", "restrict", "breaks_reference", "foreign_key")
        cal = _ref_or_unresolved(b, m, ObjectType.CALENDAR, r.resolved_calendar_id,
                                 "resolved_calendar_id")
        if cal:
            _edge(b, cal, m, EdgeKind.CALENDAR_MEASURE_BINDING, "resolved_calendar_id",
                  "hard", "restrict", "breaks_reference", "foreign_key")
        for tid in r.expression_table_ids:
            tk = _ref_or_unresolved(b, m, ObjectType.TABLE, tid, "expression_tables")
            if tk:
                _edge(b, tk, m, EdgeKind.MEASURE_BINDING, "expression_tables",
                      "hard", "restrict", "breaks_reference", "derived")
        # Base measure -> variant measure (hard / cascade).
        if r.variant_of_measure_id:
            base = _ref_or_unresolved(b, m, ObjectType.MEASURE, r.variant_of_measure_id,
                                      "variant_of_measure_id")
            if base:
                _edge(b, base, m, EdgeKind.VARIANT_MEASURE, "variant_of_measure_id",
                      "hard", "cascade", "cascade_deleted", "foreign_key")
        # Cross-model source measure (spec §12.3): the referenced measure is in
        # another model; this measure depends on it (hard / restrict).
        if r.cross_model_source_measure_id:
            ref = NodeKey(
                tenant_id=b._s.tenant_id, project_id=b._s.project_id,  # noqa: SLF001
                model_id=r.cross_model_source_model_id or b._s.model_id,  # noqa: SLF001
                object_type=ObjectType.MEASURE,
                object_id=r.cross_model_source_measure_id,
            )
            if ref in b._nodes:  # noqa: SLF001
                _edge(b, ref, m, EdgeKind.CROSS_MODEL_MEASURE_REFERENCE,
                      "cross_model_source_measure_id", "hard", "restrict",
                      "breaks_reference", "persisted_ref",
                      {"source_model_id": r.cross_model_source_model_id or ""})
            else:
                b.add_diagnostic({"type": "unresolved_reference", "owner": m.token(),
                                  "field": "cross_model_source_measure_id",
                                  "expected_type": "measure",
                                  "missing_id": r.cross_model_source_measure_id})
    # Calculated-measure references resolved to IDs by the loader are carried on
    # each measure's calc reference set; the loader packs them into the measure
    # rows via expression_table_ids for tables and a separate calc reference on
    # the KPI/measure. Same-model calculated-measure edges are added here from a
    # measure that references another measure by name.
    _build_calculated_measures(b)


def _build_calculated_measures(b: "GraphBuilder") -> None:
    """Measure -> calculated measure references (name resolved to ID by loader,
    carried in the measure row's calc reference set). The loader resolves
    ``measure("name")`` tokens to measure IDs and stores them; here we read them
    off a convention field on the snapshot (``calc_reference_ids``) if present."""
    s = b._s  # noqa: SLF001
    for r in s.measures:
        refs = r.calc_reference_ids
        m = _n(b, ObjectType.MEASURE, r.id)
        for ref_id in refs:
            ref = _ref_or_unresolved(b, m, ObjectType.MEASURE, ref_id, "expression")
            if ref:
                _edge(b, ref, m, EdgeKind.CALCULATED_MEASURE_REFERENCE, "expression",
                      "hard", "restrict", "breaks_reference", "name")


def _build_cross_model_reverse(b: "GraphBuilder") -> None:
    """Same-project measures in OTHER models that reference THIS model's measures
    (reverse index, spec §12.3)."""
    for other_model_id, other_measure_id, _name, referenced_measure_id in b._s.cross_model_measures:  # noqa: SLF001
        dep = _n(b, ObjectType.MEASURE, referenced_measure_id)
        dependent = _n(b, ObjectType.MEASURE, other_measure_id, model_id=other_model_id)
        if dep in b._nodes and dependent in b._nodes:  # noqa: SLF001
            _edge(b, dep, dependent, EdgeKind.CROSS_MODEL_MEASURE_REFERENCE,
                  "cross_model_source_measure_id", "hard", "restrict",
                  "breaks_reference", "persisted_ref",
                  {"dependent_model_id": other_model_id})
        elif dependent in b._nodes:  # noqa: SLF001
            # Reverse ref names a measure in THIS model that no longer exists —
            # never silently skip (§5.5): record a diagnostic so it is visible.
            b.add_diagnostic({"type": "unresolved_reference", "owner": dependent.token(),
                              "field": "cross_model_source_measure_id",
                              "expected_type": "measure", "missing_id": referenced_measure_id})


def _build_hierarchy_levels(b: "GraphBuilder") -> None:
    """Column/UDA -> hierarchy level key + attributes; dimension<->level assoc."""
    s = b._s  # noqa: SLF001
    for r in s.hierarchy_levels:
        lvl = _n(b, ObjectType.HIERARCHY_LEVEL, r.id)
        key_type = (ObjectType.USER_DEFINED_ATTRIBUTE
                    if r.key_attribute_source == "user_defined_attribute"
                    else ObjectType.COLUMN)
        key = _ref_or_unresolved(b, lvl, key_type, r.key_attribute_id, "key_attribute_id")
        if key:
            _edge(b, key, lvl, EdgeKind.HIERARCHY_LEVEL_ATTRIBUTE, "key_attribute_id",
                  "hard", "restrict", "breaks_reference", "persisted_ref")
        for attr_id, attr_source, role in r.attributes:
            at = (ObjectType.USER_DEFINED_ATTRIBUTE
                  if attr_source == "user_defined_attribute" else ObjectType.COLUMN)
            ak = _ref_or_unresolved(b, lvl, at, attr_id, f"level_attribute_{role}")
            if ak:
                _edge(b, ak, lvl, EdgeKind.HIERARCHY_LEVEL_ATTRIBUTE,
                      f"level_attribute_{role}", "hard", "restrict",
                      "breaks_reference", "persisted_ref", {"role": role})


def _build_dimension_level_association(b: "GraphBuilder") -> None:
    """Dimension -> hierarchy level derived edge when a level and a dimension
    share the same physical/UDA backing attribute (spec §5.3, §7.1 step 6).

    Soft / recompute: the friendly semantic association changes but the stored
    level binding survives, so deleting the dimension does not break the level.
    """
    s = b._s  # noqa: SLF001
    # Index each dimension by its backing attribute keyed on (source, id) so a
    # physical-column id can never false-match a UDA id (the two id spaces are
    # distinct UUIDs, but the guard makes the match explicit).
    backing_to_dims: dict[tuple[str, str], list[str]] = {}
    for d in s.dimensions:
        if d.source_column_id:
            backing_to_dims.setdefault(("physical_column", d.source_column_id), []).append(d.id)
        if d.user_defined_attribute_id:
            backing_to_dims.setdefault(
                ("user_defined_attribute", d.user_defined_attribute_id), []
            ).append(d.id)
    for lvl in s.hierarchy_levels:
        backing = (lvl.key_attribute_source, lvl.key_attribute_id)
        for dim_id in backing_to_dims.get(backing, []):
            _edge(b, _n(b, ObjectType.DIMENSION, dim_id),
                  _n(b, ObjectType.HIERARCHY_LEVEL, lvl.id),
                  EdgeKind.DIMENSION_LEVEL_ASSOCIATION, "shared_backing_attribute",
                  "soft", "recompute", "changes_semantics", "derived",
                  {"backing_attribute_id": lvl.key_attribute_id})


def _build_aggregates(b: "GraphBuilder") -> None:
    """Dimension grain, measure column, persona scope, refresh dependency (§5.3)."""
    s = b._s  # noqa: SLF001
    # Which aggregates would keep serving stale results (§7.6). Used to mark the
    # edge so the simulator keeps the impact hard; default safe-fallback = soft.
    serves_stale = {r.id: r.serves_when_stale for r in s.aggregates}
    for r in s.aggregates:
        agg = _n(b, ObjectType.AGGREGATE, r.id)
        stale_ev = {"serves_when_stale": "true"} if r.serves_when_stale else {}
        for did in r.grain_dimension_ids:
            dk = _ref_or_unresolved(b, agg, ObjectType.DIMENSION, did, "grain")
            if dk:
                # §7.6: soft by default (safe source fallback); hard only when
                # the aggregate serves stale — the simulator reads the evidence.
                _edge(b, dk, agg, EdgeKind.AGGREGATE_GRAIN, "grain",
                      "hard", "invalidate", "loses_coverage", "name", stale_ev)
        if r.persona_id:
            pk = _ref_or_unresolved(b, agg, ObjectType.PERSONA, r.persona_id, "persona_id")
            if pk:
                _edge(b, pk, agg, EdgeKind.PERSONA_AGGREGATE_SCOPE, "persona_id",
                      "soft", "detach", "detached", "foreign_key")
        for dep_id in r.refresh_dependency_ids:
            src = _ref_or_unresolved(b, agg, ObjectType.AGGREGATE, dep_id, "refresh_dependency")
            if src:
                _edge(b, src, agg, EdgeKind.REFRESH_DEPENDENCY, "refresh_dependency",
                      "hard", "invalidate", "stale", "persisted_ref")
    for r in s.aggregate_columns:
        if r.measure_id:
            ac = _n(b, ObjectType.AGGREGATE_COLUMN, r.id)
            mk = _ref_or_unresolved(b, ac, ObjectType.MEASURE, r.measure_id, "measure_id")
            if mk:
                col_ev = ({"serves_when_stale": "true"}
                          if serves_stale.get(r.aggregate_id) else {})
                _edge(b, mk, ac, EdgeKind.AGGREGATE_MEASURE, "measure_id",
                      "hard", "invalidate", "loses_coverage", "foreign_key", col_ev)
                # §5.3 "Measure -> aggregate column/aggregate": also name the
                # aggregate itself so the preview/effect plan lists it directly,
                # not only its column. The aggregate node exists (containment).
                agg_key = _n(b, ObjectType.AGGREGATE, r.aggregate_id)
                if agg_key in b._nodes:  # noqa: SLF001
                    _edge(b, mk, agg_key, EdgeKind.AGGREGATE_MEASURE, "measure_id",
                          "hard", "invalidate", "loses_coverage", "foreign_key", col_ev)


def _build_pockets(b: "GraphBuilder") -> None:
    """Dimension/measure/table -> pocket (spec §5.3); persona pocket scope."""
    s = b._s  # noqa: SLF001
    for r in s.pockets:
        pk = _n(b, ObjectType.POCKET, r.id)
        for did in r.referenced_dimension_ids:
            dk = _ref_or_unresolved(b, pk, ObjectType.DIMENSION, did, "pocket_sql")
            if dk:
                _edge(b, dk, pk, EdgeKind.POCKET_REFERENCE, "pocket_sql",
                      "hard", "invalidate", "loses_coverage", "derived")
        for mid in r.referenced_measure_ids:
            mk = _ref_or_unresolved(b, pk, ObjectType.MEASURE, mid, "pocket_sql")
            if mk:
                _edge(b, mk, pk, EdgeKind.POCKET_REFERENCE, "pocket_sql",
                      "hard", "invalidate", "loses_coverage", "derived")
        for tid in r.referenced_table_ids:
            tk = _ref_or_unresolved(b, pk, ObjectType.TABLE, tid, "pocket_sql")
            if tk:
                _edge(b, tk, pk, EdgeKind.POCKET_REFERENCE, "pocket_sql",
                      "hard", "invalidate", "loses_coverage", "derived")
        if r.persona_id:
            pp = _ref_or_unresolved(b, pk, ObjectType.PERSONA, r.persona_id, "persona_id")
            if pp:
                _edge(b, pp, pk, EdgeKind.PERSONA_AGGREGATE_SCOPE, "persona_id",
                      "soft", "detach", "detached", "foreign_key")


def _build_kpis(b: "GraphBuilder") -> None:
    """Measure/dimension/KPI -> KPI (spec §5.3)."""
    s = b._s  # noqa: SLF001
    for r in s.kpis:
        kpi = _n(b, ObjectType.KPI, r.id)
        for mid in r.measure_ids:
            mk = _ref_or_unresolved(b, kpi, ObjectType.MEASURE, mid, "expression")
            if mk:
                _edge(b, mk, kpi, EdgeKind.KPI_MEASURE_REFERENCE, "expression",
                      "hard", "restrict", "breaks_reference", "name")
        for did in r.dimension_ids:
            dk = _ref_or_unresolved(b, kpi, ObjectType.DIMENSION, did, "dimension")
            if dk:
                _edge(b, dk, kpi, EdgeKind.KPI_DIMENSION_REFERENCE, "dimension",
                      "hard", "restrict", "breaks_reference", "name")
        if r.time_dimension_id:
            tk = _ref_or_unresolved(b, kpi, ObjectType.DIMENSION, r.time_dimension_id,
                                    "time_dimension_id")
            if tk:
                _edge(b, tk, kpi, EdgeKind.KPI_DIMENSION_REFERENCE, "time_dimension_id",
                      "hard", "restrict", "breaks_reference", "foreign_key")
        for kid in r.referenced_kpi_ids:
            rk = _ref_or_unresolved(b, kpi, ObjectType.KPI, kid, "expression")
            if rk:
                _edge(b, rk, kpi, EdgeKind.KPI_KPI_REFERENCE, "expression",
                      "hard", "restrict", "breaks_reference", "name")
        if r.parent_kpi_id:
            pk = _ref_or_unresolved(b, kpi, ObjectType.KPI, r.parent_kpi_id, "parent_kpi_id")
            if pk:
                _edge(b, pk, kpi, EdgeKind.KPI_KPI_REFERENCE, "parent_kpi_id",
                      "hard", "restrict", "breaks_reference", "foreign_key")
        if r.replacement_kpi_id:
            rk = _ref_or_unresolved(b, kpi, ObjectType.KPI, r.replacement_kpi_id,
                                    "replacement_kpi_id")
            if rk:
                _edge(b, rk, kpi, EdgeKind.KPI_KPI_REFERENCE, "replacement_kpi_id",
                      "soft", "detach", "detached", "foreign_key")


def _build_drill_through(b: "GraphBuilder") -> None:
    s = b._s  # noqa: SLF001
    for r in s.drill_through_sets:
        dt = _n(b, ObjectType.DRILL_THROUGH_SET, r.id)
        if r.measure_id:
            mk = _ref_or_unresolved(b, dt, ObjectType.MEASURE, r.measure_id, "measure_id")
            if mk:
                _edge(b, mk, dt, EdgeKind.DRILL_THROUGH_REFERENCE, "measure_id",
                      "hard", "cascade", "cascade_deleted", "foreign_key")
        if r.source_table_id:
            tk = _ref_or_unresolved(b, dt, ObjectType.TABLE, r.source_table_id, "source_table_id")
            if tk:
                _edge(b, tk, dt, EdgeKind.DRILL_THROUGH_REFERENCE, "source_table_id",
                      "hard", "restrict", "breaks_reference", "foreign_key")
        for cid in r.detail_column_ids:
            ck = _ref_or_unresolved(b, dt, ObjectType.COLUMN, cid, "detail_columns")
            if ck:
                _edge(b, ck, dt, EdgeKind.DRILL_THROUGH_REFERENCE, "detail_columns",
                      "hard", "restrict", "breaks_reference", "json")
        for did in r.joined_dimension_ids:
            dk = _ref_or_unresolved(b, dt, ObjectType.DIMENSION, did, "joined_dimension_ids")
            if dk:
                _edge(b, dk, dt, EdgeKind.DRILL_THROUGH_REFERENCE, "joined_dimension_ids",
                      "hard", "restrict", "breaks_reference", "json")
        for rid in r.join_path_relationship_ids:
            rk = _ref_or_unresolved(b, dt, ObjectType.RELATIONSHIP, rid, "source_join_path")
            if rk:
                _edge(b, rk, dt, EdgeKind.DRILL_THROUGH_REFERENCE, "source_join_path",
                      "hard", "restrict", "breaks_reference", "json")


def _build_named_lists(b: "GraphBuilder") -> None:
    s = b._s  # noqa: SLF001
    for r in s.named_lists:
        nl = _n(b, ObjectType.NAMED_LIST, r.id)
        for did in r.dimension_ids:
            dk = _ref_or_unresolved(b, nl, ObjectType.DIMENSION, did, "dimensions")
            if dk:
                _edge(b, dk, nl, EdgeKind.NAMED_LIST_REFERENCE, "dimensions",
                      "hard", "restrict", "breaks_reference", "json")
        for hid in r.hierarchy_ids:
            hk = _ref_or_unresolved(b, nl, ObjectType.HIERARCHY, hid, "hierarchy")
            if hk:
                _edge(b, hk, nl, EdgeKind.NAMED_LIST_REFERENCE, "hierarchy",
                      "hard", "restrict", "breaks_reference", "name")
        for mid in r.measure_ids:
            mk = _ref_or_unresolved(b, nl, ObjectType.MEASURE, mid, "measure")
            if mk:
                _edge(b, mk, nl, EdgeKind.NAMED_LIST_REFERENCE, "measure",
                      "hard", "restrict", "breaks_reference", "name")
        if r.replacement_id:
            rk = _ref_or_unresolved(b, nl, ObjectType.NAMED_LIST, r.replacement_id,
                                    "replacement_id")
            if rk:
                _edge(b, rk, nl, EdgeKind.NAMED_LIST_REPLACEMENT, "replacement_id",
                      "soft", "detach", "detached", "foreign_key")


def _build_saved_artifacts(b: "GraphBuilder") -> None:
    s = b._s  # noqa: SLF001
    for r in s.saved_queries:
        sq = _n(b, ObjectType.SAVED_QUERY, r.id)
        for mid in r.referenced_measure_ids:
            mk = _ref_or_unresolved(b, sq, ObjectType.MEASURE, mid, "query")
            if mk:
                _edge(b, mk, sq, EdgeKind.SAVED_QUERY_REFERENCE, "query",
                      "hard", "invalidate", "breaks_reference", "name")
        for did in r.referenced_dimension_ids:
            dk = _ref_or_unresolved(b, sq, ObjectType.DIMENSION, did, "query")
            if dk:
                _edge(b, dk, sq, EdgeKind.SAVED_QUERY_REFERENCE, "query",
                      "hard", "invalidate", "breaks_reference", "name")
        for lid in r.referenced_named_list_ids:
            lk = _ref_or_unresolved(b, sq, ObjectType.NAMED_LIST, lid, "query")
            if lk:
                _edge(b, lk, sq, EdgeKind.NAMED_LIST_SAVED_REFERENCE, "query",
                      "hard", "restrict", "breaks_reference", "name")
        for prm in r.referenced_parameter_ids:
            pk = _ref_or_unresolved(b, sq, ObjectType.MODEL_PARAMETER, prm, "query_param")
            if pk:
                _edge(b, pk, sq, EdgeKind.MODEL_PARAMETER_REFERENCE, "query",
                      "hard", "restrict", "breaks_reference", "name")
    for r in s.saved_pivots:
        sp = _n(b, ObjectType.SAVED_PIVOT_VIEW, r.id)
        for mid in r.measure_ids:
            mk = _ref_or_unresolved(b, sp, ObjectType.MEASURE, mid, "measure_id")
            if mk:
                _edge(b, mk, sp, EdgeKind.SAVED_PIVOT_REFERENCE, "measure_id",
                      "hard", "invalidate", "breaks_reference", "json")
        for did in list(r.row_dimension_ids) + list(r.column_dimension_ids):
            dk = _ref_or_unresolved(b, sp, ObjectType.DIMENSION, did, "config_json")
            if dk:
                _edge(b, dk, sp, EdgeKind.SAVED_PIVOT_REFERENCE, "config_json",
                      "hard", "invalidate", "breaks_reference", "json")
        for lid in r.referenced_named_list_ids:
            lk = _ref_or_unresolved(b, sp, ObjectType.NAMED_LIST, lid, "config_json")
            if lk:
                _edge(b, lk, sp, EdgeKind.NAMED_LIST_SAVED_REFERENCE, "config_json",
                      "hard", "restrict", "breaks_reference", "json")


def _build_data_tags(b: "GraphBuilder") -> None:
    """Column -> data tag (soft/detach); data tag -> persona CLS (hard/restrict)."""
    s = b._s  # noqa: SLF001
    for r in s.data_tags:
        tag = _n(b, ObjectType.DATA_TAG, r.id)
        for cid in r.column_ids:
            ck = _ref_or_unresolved(b, tag, ObjectType.COLUMN, cid, "data_tag_columns")
            if ck:
                _edge(b, ck, tag, EdgeKind.DATA_TAG_MEMBERSHIP, "data_tag_columns",
                      "soft", "detach", "detached", "persisted_ref")
    # data tag -> persona (semantic edge from PersonaTagRestriction). Deleting a
    # tag with a persona restriction is blocked (Bug-7790).
    for r in s.personas:
        persona = _n(b, ObjectType.PERSONA, r.id)
        for tag_id in r.restricted_data_tag_ids:
            tk = _ref_or_unresolved(b, persona, ObjectType.DATA_TAG, tag_id,
                                    "persona_tag_restriction")
            if tk:
                _edge(b, tk, persona, EdgeKind.DATA_TAG_RESTRICTION, "data_tag_id",
                      "hard", "restrict", "breaks_reference", "persisted_ref")


def _build_row_security(b: "GraphBuilder") -> None:
    s = b._s  # noqa: SLF001
    for r in s.row_security_rules:
        rule = _n(b, ObjectType.ROW_SECURITY_RULE, r.id)
        if r.dimension_id:
            dk = _ref_or_unresolved(b, rule, ObjectType.DIMENSION, r.dimension_id, "dimension_path")
            if dk:
                _edge(b, dk, rule, EdgeKind.ROW_SECURITY_BINDING, "dimension_path",
                      "hard", "restrict", "breaks_reference", "name")
        if r.mapping_table_id:
            tk = _ref_or_unresolved(b, rule, ObjectType.TABLE, r.mapping_table_id, "mapping_table_id")
            if tk:
                _edge(b, tk, rule, EdgeKind.ROW_SECURITY_BINDING, "mapping_table_id",
                      "hard", "restrict", "breaks_reference", "foreign_key")
        for cid in r.mapping_column_ids:
            ck = _ref_or_unresolved(b, rule, ObjectType.COLUMN, cid, "mapping_columns")
            if ck:
                _edge(b, ck, rule, EdgeKind.ROW_SECURITY_BINDING, "mapping_columns",
                      "hard", "restrict", "breaks_reference", "name")


def _build_personas(b: "GraphBuilder") -> None:
    """Dimension/measure/hierarchy -> persona and project-persona scope (soft)."""
    s = b._s  # noqa: SLF001
    for r in s.personas:
        persona = _n(b, ObjectType.PERSONA, r.id)
        for did in r.included_dimension_ids:
            dk = _ref_or_unresolved(b, persona, ObjectType.DIMENSION, did, "included_dimensions")
            if dk:
                _edge(b, dk, persona, EdgeKind.PERSONA_SCOPE, "included_dimensions",
                      "soft", "detach", "detached", "json")
        for mid in r.included_measure_ids:
            mk = _ref_or_unresolved(b, persona, ObjectType.MEASURE, mid, "included_measures")
            if mk:
                _edge(b, mk, persona, EdgeKind.PERSONA_SCOPE, "included_measures",
                      "soft", "detach", "detached", "json")
        for hid in r.included_hierarchy_ids:
            hk = _ref_or_unresolved(b, persona, ObjectType.HIERARCHY, hid, "included_hierarchies")
            if hk:
                _edge(b, hk, persona, EdgeKind.PERSONA_SCOPE, "included_hierarchies",
                      "soft", "detach", "detached", "json")
        # Dimensions named by Persona.default_filters JSON (spec §5.3, Bug-5607).
        for did in r.default_filter_dimension_ids:
            dk = _ref_or_unresolved(b, persona, ObjectType.DIMENSION, did, "default_filters")
            if dk:
                _edge(b, dk, persona, EdgeKind.PERSONA_SCOPE, "default_filters",
                      "soft", "detach", "detached", "json")
    for r in s.project_persona_scopes:
        scope = _n(b, ObjectType.PROJECT_PERSONA_SCOPE, r.id)
        for did in r.included_dimension_ids:
            dk = _ref_or_unresolved(b, scope, ObjectType.DIMENSION, did, "included_dimensions")
            if dk:
                _edge(b, dk, scope, EdgeKind.PROJECT_PERSONA_SCOPE, "included_dimensions",
                      "soft", "detach", "detached", "json")
        for mid in r.included_measure_ids:
            mk = _ref_or_unresolved(b, scope, ObjectType.MEASURE, mid, "included_measures")
            if mk:
                _edge(b, mk, scope, EdgeKind.PROJECT_PERSONA_SCOPE, "included_measures",
                      "soft", "detach", "detached", "json")


def _build_soft_references(b: "GraphBuilder") -> None:
    """Translation/preference/alias/recipe/glossary/DQ/scratchpad/lineage/agent."""
    s = b._s  # noqa: SLF001
    for r in s.translations:
        node = _n(b, ObjectType.TRANSLATION, r.id)
        owner = _resolve_polymorphic(b, node, r.entity_type, r.entity_id, "entity")
        if owner:
            _edge(b, owner, node, EdgeKind.TRANSLATION_REFERENCE, "entity_id",
                  "soft", "detach", "cleanup", "json")
    for r in s.user_preferences:
        node = _n(b, ObjectType.USER_PREFERENCE, r.id)
        owner = _resolve_polymorphic(b, node, r.entity_type, r.entity_id, "entity")
        if owner:
            _edge(b, owner, node, EdgeKind.TRANSLATION_REFERENCE, "entity_id",
                  "soft", "detach", "cleanup", "json")
    for r in s.alias_maps:
        node = _n(b, ObjectType.MODEL_ALIAS_MAP, r.id)
        for oid in r.referenced_object_ids:
            # alias map references are resolved to their concrete node by ID.
            owner = _find_any(b, oid, node, "canonical_attribute")
            if owner:
                _edge(b, owner, node, EdgeKind.ALIAS_MAP_REFERENCE, "canonical_attribute",
                      "soft", "recompute", "stale", "json")
    for r in s.cross_model_recipes:
        node = _n(b, ObjectType.CROSS_MODEL_RECIPE, r.id)
        for oid in r.referenced_object_ids:
            owner = _find_any(b, oid, node, "steps")
            if owner:
                _edge(b, owner, node, EdgeKind.CROSS_MODEL_RECIPE_REFERENCE, "steps",
                      "hard", "restrict", "breaks_reference", "json")
        # Model -> recipe: a recipe step referencing THIS model as a whole must
        # block model deletion (spec §5.3 "Model/measure/dimension -> recipe").
        for mid in r.referenced_model_ids:
            if mid == s.model_id:
                _edge(b, _n(b, ObjectType.MODEL, s.model_id), node,
                      EdgeKind.CROSS_MODEL_RECIPE_REFERENCE, "steps",
                      "hard", "restrict", "breaks_reference", "json",
                      {"referenced_model_id": mid})
    for r in s.glossary_attachments:
        node = _n(b, ObjectType.GLOSSARY_ATTACHMENT, r.id)
        owner = _resolve_polymorphic(b, node, r.target_type, r.target_id, "target")
        if owner:
            # policy=detach (the row is cleaned up on delete); effect=cleanup.
            # "cleanup" is an Effect, never a DeletePolicy (types.py).
            _edge(b, owner, node, EdgeKind.GLOSSARY_ATTACHMENT_REFERENCE, "target_id",
                  "soft", "detach", "cleanup", "json")
    for r in s.data_quality_rules:
        node = _n(b, ObjectType.DATA_QUALITY_RULE, r.id)
        owner = _resolve_polymorphic(b, node, r.target_type, r.target_id, "target")
        if owner:
            # block_on_failure rules require explicit acknowledgement (spec §5.3).
            _edge(b, owner, node, EdgeKind.DATA_QUALITY_REFERENCE, "target_id",
                  "soft", "detach", "detached", "json",
                  {"block_on_failure": "true" if r.block_on_failure else "false"})
    for r in s.scratchpad_measures:
        node = _n(b, ObjectType.SCRATCHPAD_MEASURE, r.id)
        for mid in r.reference_ids:
            mk = _ref_or_unresolved(b, node, ObjectType.MEASURE, mid, "expression")
            if mk:
                _edge(b, mk, node, EdgeKind.SCRATCHPAD_MEASURE_REFERENCE, "expression",
                      "soft", "invalidate", "stale", "name")
    for r in s.lineage_mappings:
        node = _n(b, ObjectType.LINEAGE_MAPPING, r.id)
        if r.source_column_id:
            ck = _ref_or_unresolved(b, node, ObjectType.COLUMN, r.source_column_id, "source_column_id")
            if ck:
                _edge(b, ck, node, EdgeKind.LINEAGE_REFERENCE, "source_column_id",
                      "soft", "detach", "cleanup", "foreign_key")
        if r.aggregate_col_id:
            ak = _ref_or_unresolved(b, node, ObjectType.AGGREGATE_COLUMN, r.aggregate_col_id, "aggregate_col_id")
            if ak:
                _edge(b, ak, node, EdgeKind.LINEAGE_REFERENCE, "aggregate_col_id",
                      "soft", "detach", "cleanup", "foreign_key")
    # Model -> agent grounding (soft / detach).
    model = _n(b, ObjectType.MODEL, s.model_id)
    for r in s.agent_groundings:
        node = _n(b, ObjectType.AGENT_MODEL_LINK, r.id)
        _edge(b, model, node, EdgeKind.AGENT_GROUNDING, "primary_model_id",
              "soft", "detach", "detached", "foreign_key")


def _build_unresolved_definitions(b: "GraphBuilder") -> None:
    """Emit an owner -> unresolved node for each definition the loader could not
    parse (spec §5.5, §12.7). Without this, a KPI/pocket/named-list/saved-query
    with an unparseable definition arrives as an EMPTY reference set —
    indistinguishable from "no references" — and its real dependency would be
    severable with no block. Fail closed instead."""
    s = b._s  # noqa: SLF001
    type_map = {
        "kpi": ObjectType.KPI,
        "pocket": ObjectType.POCKET,
        "named_list": ObjectType.NAMED_LIST,
        "saved_query": ObjectType.SAVED_QUERY,
        "saved_pivot_view": ObjectType.SAVED_PIVOT_VIEW,
        "measure": ObjectType.MEASURE,
        "dimension": ObjectType.DIMENSION,
        # A parse/resolution failure on an aggregate grain or a row-security /
        # CLS rule must fail closed too (§12.6 security is conservative): without
        # these owner types the loader's unresolved_definition would be dropped as
        # "unknown_owner_type" and the broken reference would NOT block. (Bug-7787
        # engine-defect fix; strictly widens fail-closed coverage.)
        "aggregate": ObjectType.AGGREGATE,
        "row_security_rule": ObjectType.ROW_SECURITY_RULE,
        "data_tag": ObjectType.DATA_TAG,
        "persona": ObjectType.PERSONA,
    }
    for owner_type, owner_id, field, reason in s.unresolved_definitions:
        ot = type_map.get(owner_type)
        if ot is None:
            b.add_diagnostic({"type": "unknown_owner_type", "owner_type": owner_type,
                              "owner_id": owner_id, "field": field})
            continue
        owner = _n(b, ot, owner_id)
        if owner not in b._nodes:  # noqa: SLF001
            continue
        unresolved_id = f"{owner_type}:{owner_id}:{field}:parse_failure"
        ukey = b.add_node(ObjectType.UNRESOLVED_REFERENCE, unresolved_id,
                          name=field, display_name=field, valid=False,
                          unresolved_reason=reason or "definition_parse_failure")
        _edge(b, owner, ukey, EdgeKind.UNRESOLVED_REFERENCE, field,
              "hard", "cascade", "breaks_reference", "derived",
              {"reason": reason or "definition_parse_failure"})
        b.add_diagnostic({"type": "unresolved_definition", "owner": owner.token(),
                          "field": field, "reason": reason or "definition_parse_failure"})


def _build_target_model_binding(b: "GraphBuilder") -> None:
    """Data target -> model default binding (soft / detach) — Model.target_id."""
    s = b._s  # noqa: SLF001
    default_target = s.model_default_target_id
    if default_target:
        tk = NodeKey(tenant_id=s.tenant_id, project_id=s.project_id, model_id=s.model_id,
                     object_type=ObjectType.DATA_TARGET, object_id=default_target)
        if tk in b._nodes:  # noqa: SLF001
            _edge(b, tk, _n(b, ObjectType.MODEL, s.model_id), EdgeKind.TARGET_BINDING,
                  "target_id", "soft", "detach", "detached", "foreign_key")


def _resolve_polymorphic(
    b: "GraphBuilder", owner: NodeKey, entity_type: str, entity_id: str, field: str
) -> Optional[NodeKey]:
    """Resolve a polymorphic (type, id) reference to a concrete node (spec §12.4)."""
    # Known entity types that intentionally have NO model-object node: a
    # model-level translation cascades with the model via containment, and a
    # glossary "concept" target is not a model object. Ignore silently — they are
    # not unknown, and emitting a diagnostic would be false noise on every analysis.
    if entity_type in _POLY_KNOWN_IGNORE:
        return None
    ot = _POLY_TYPE_MAP.get(entity_type)
    if ot is None:
        b.add_diagnostic({"type": "unknown_entity_type", "owner": owner.token(),
                          "field": field, "entity_type": entity_type})
        return None
    return _ref_or_unresolved(b, owner, ot, entity_id, field)


def _find_any(
    b: "GraphBuilder", object_id: str, owner: NodeKey, field_name: str
) -> Optional[NodeKey]:
    """Find a node by object_id across all types (alias-map/recipe references
    carry no type discriminator). On a miss, register an unresolved node + edge +
    diagnostic (spec §5.5) so a missing hard reference fails closed rather than
    being silently skipped."""
    for key in b._nodes:  # noqa: SLF001
        if key.object_id == object_id and key.object_type != ObjectType.UNRESOLVED_REFERENCE:
            return key
    # Miss: fail closed. Reuse _ref_or_unresolved's unresolved-node machinery by
    # registering directly (expected type unknown for a type-less reference).
    unresolved_id = f"{owner.object_type.value}:{owner.object_id}:{field_name}:{object_id}"
    ukey = b.add_node(
        ObjectType.UNRESOLVED_REFERENCE, unresolved_id,
        name=field_name, display_name=field_name, valid=False,
        unresolved_reason="missing_object",
    )
    # cascade (not restrict): the stale reference dies with its owner, so deleting
    # the owner must not self-block on it — consistent with _ref_or_unresolved.
    _edge(b, owner, ukey, EdgeKind.UNRESOLVED_REFERENCE, field_name,
          "hard", "cascade", "breaks_reference", "derived",
          {"missing_id": object_id})
    b.add_diagnostic({"type": "unresolved_reference", "owner": owner.token(),
                      "field": field_name, "expected_type": "unknown",
                      "missing_id": object_id})
    return None


_POLY_TYPE_MAP: dict[str, ObjectType] = {
    "dimension": ObjectType.DIMENSION,
    "measure": ObjectType.MEASURE,
    "column": ObjectType.COLUMN,
    "table": ObjectType.TABLE,
    "hierarchy": ObjectType.HIERARCHY,
    "kpi": ObjectType.KPI,
    "named_list": ObjectType.NAMED_LIST,
    "named_set": ObjectType.NAMED_LIST,
}

# Polymorphic entity types that legitimately map to NO model-object node.
# "model" translations cascade via model containment; glossary "concept" targets
# are not model objects. Ignored without an unknown-type diagnostic.
_POLY_KNOWN_IGNORE: frozenset[str] = frozenset({"model", "concept"})


# --- registry --------------------------------------------------------------

# One entry per §5.3 edge family group. ``test_edge_catalogue_coverage`` asserts
# every EdgeKind is emitted by some builder against the retail fixture.
EDGE_BUILDERS = (
    _build_containment,
    _build_connections_targets,
    _build_target_model_binding,
    _build_calendar_alias,
    _build_relationships,
    _build_udas,
    _build_dimensions,
    _build_measures,
    _build_cross_model_reverse,
    _build_hierarchy_levels,
    _build_dimension_level_association,
    _build_aggregates,
    _build_pockets,
    _build_kpis,
    _build_drill_through,
    _build_named_lists,
    _build_saved_artifacts,
    _build_data_tags,
    _build_row_security,
    _build_personas,
    _build_soft_references,
    _build_unresolved_definitions,
)


def build_all_edges(b: "GraphBuilder") -> None:
    """Register all nodes then materialize every edge family (spec §7.1)."""
    _register_nodes(b)
    for fn in EDGE_BUILDERS:
        fn(b)
