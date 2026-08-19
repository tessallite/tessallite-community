"""Batched model-dependency loader (Bug-7787, spec §6.2).

Reads the LIVE DRAFT of one model with a fixed number of batched queries and
resolves every name / JSON / expression reference to a stable object ID, then
returns an immutable ``ModelDependencySnapshot`` for the pure engine
(``shared/model_dependency``). The engine resolves references by ID only; ALL
name/JSON/expression resolution is this module's job so the engine stays pure.

Invariants honoured (see the build plan):

- Fixed query count: no object-count-dependent SQL. Every family is one query
  scoped by ``model_id`` (cross-model index adds a small fixed set of
  project-scoped queries).
- Parse failures POPULATE ``unresolved_definitions`` — never silently dropped.
  The engine emits an owner -> unresolved node so the guard fails closed.
- Cross-project IDs are classified per spec §12.3: a same-project other-model
  measure that references THIS model becomes a resolvable cross-model reverse
  edge; a reference into a DIFFERENT project (or an unresolved model) is left
  unresolved and blocks destructive actions.
- Read-only: this module NEVER writes and NEVER touches a source/target DB.

The loader adapts the existing parsers rather than re-implementing them:
``shared/semantic/calculated_expression`` (calc-measure ``measure("name")``),
``shared/semantic/kpi_expression`` (KPI expression measure/kpi/dimension refs),
``shared/semantic/calc_dimension_validator`` (calc-dimension column refs), the
named-list ``builder_definition`` JSON, and SQLGlot over pocket ``defining_sql``
plus persisted ``PocketPredicate`` column names.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Optional

import sqlglot
from sqlglot import exp
from sqlalchemy import select

from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    CalendarTable,
    DataQualityRule,
    DataSource,
    DataTag,
    DataTarget,
    Dimension,
    DrillThroughSet,
    EntityTranslation,
    GlossaryAttachment,
    GlossaryEntry,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    Join,
    KPI,
    LineageMapping,
    Measure,
    Model,
    ModelAliasMap,
    ModelColumn,
    ModelParameter,
    ModelTable,
    NamedSet,
    Persona,
    PersonaTagRestriction,
    PocketDefinition,
    PocketPredicate,
    ProjectAgentModel,
    ProjectCrossModelRecipe,
    RefreshDependency,
    RowSecurityRule,
    ScratchpadMeasure,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
    UserEntityPreference,
    data_tag_columns,
)
from shared.model_dependency.snapshot import (
    AgentGroundingRow,
    AggregateColumnRow,
    AggregateRow,
    AliasMapRow,
    CalendarRow,
    ColumnRow,
    CrossModelRecipeRow,
    DataQualityRuleRow,
    DataTagRow,
    DimensionRow,
    DrillThroughRow,
    GlossaryAttachmentRow,
    HierarchyLevelRow,
    HierarchyRow,
    KpiRow,
    LineageMappingRow,
    MeasureRow,
    ModelDependencySnapshot,
    ModelParameterRow,
    NamedListRow,
    PersonaRow,
    PocketRow,
    RowSecurityRuleRow,
    ScratchpadMeasureRow,
    SourceRow,
    TableRow,
    TargetRow,
    TranslationRow,
    UdaRow,
    UserPreferenceRow,
)
from shared.semantic.graph_order import is_fact_table

# Route templates keyed by object_type; the engine formats them with
# project_id/model_id/object_id. Non-secret, so kept here rather than in code as
# literals scattered through the engine. The frontend builder route contract.
_ROUTE_TEMPLATES: dict[str, str] = {
    "measure": "/projects/{project_id}/models/{model_id}/measures/{object_id}",
    "dimension": "/projects/{project_id}/models/{model_id}/dimensions/{object_id}",
    "kpi": "/projects/{project_id}/models/{model_id}/kpis/{object_id}",
    "named_list": "/projects/{project_id}/models/{model_id}/named-sets/{object_id}",
    "column": "/projects/{project_id}/models/{model_id}/columns/{object_id}",
    "table": "/projects/{project_id}/models/{model_id}/tables/{object_id}",
    "hierarchy": "/projects/{project_id}/models/{model_id}/hierarchies/{object_id}",
    "aggregate": "/projects/{project_id}/models/{model_id}/aggregates/{object_id}",
    "pocket": "/projects/{project_id}/models/{model_id}/pockets/{object_id}",
    "persona": "/projects/{project_id}/models/{model_id}/personas/{object_id}",
    "row_security_rule": "/projects/{project_id}/models/{model_id}/row-security/{object_id}",
    "data_tag": "/projects/{project_id}/models/{model_id}/data-tags/{object_id}",
    "drill_through_set": "/projects/{project_id}/models/{model_id}/measures/{object_id}",
    "user_defined_attribute": "/projects/{project_id}/models/{model_id}/attributes/{object_id}",
}


def _s(value: Any) -> str:
    """Stable string form for a UUID/str ID (the engine keys nodes by string)."""
    return str(value) if value is not None else ""


def _opt(value: Any) -> Optional[str]:
    return _s(value) if value is not None else None


class ModelDependencyLoader:
    """Loads and normalizes one model's draft into a ``ModelDependencySnapshot``.

    Construct with a live tenant DB session, then ``await load(project_id,
    model_id)``. All queries are batched (one per family, model-scoped) and no ORM
    lazy loads are triggered — rows are read eagerly into in-memory dicts.
    """

    def __init__(self, db) -> None:
        self._db = db
        # Populated by _fetch. Kept as ID-keyed dicts / name indexes so every
        # resolver is O(1) and no per-parent SQL runs.
        self._model: Optional[Model] = None
        self._tables: dict[str, ModelTable] = {}
        self._columns: dict[str, ModelColumn] = {}
        self._calendars: dict[str, CalendarTable] = {}
        self._udas: dict[str, UserDefinedAttribute] = {}
        self._dimensions: dict[str, Dimension] = {}
        self._measures: dict[str, Measure] = {}
        self._hierarchies: dict[str, HierarchyDefinition] = {}
        # Name indexes (case-normalized) for expression/JSON name resolution.
        self._measure_by_name: dict[str, list[str]] = defaultdict(list)
        self._dimension_by_name: dict[str, list[str]] = defaultdict(list)
        self._kpi_by_name: dict[str, list[str]] = defaultdict(list)
        self._hierarchy_by_name: dict[str, list[str]] = defaultdict(list)
        self._namedset_by_name: dict[str, list[str]] = defaultdict(list)
        self._param_by_name: dict[str, list[str]] = defaultdict(list)
        # Column name -> [column_id] (may be ambiguous across tables).
        self._column_by_name: dict[str, list[str]] = defaultdict(list)
        # (table_id, column_name_lower) -> column_id for scoped resolution.
        self._column_in_table: dict[tuple[str, str], str] = {}
        # table alias -> table_id (calc-dimension alias resolution).
        self._table_alias_to_id: dict[str, str] = {}
        # table alias OR physical name (case-normalized) -> table_id, for pocket
        # SQL that may name the physical table rather than the model alias.
        self._table_by_ref: dict[str, str] = {}
        # The model slug / <slug>_technical — the canonical pocket FROM target.
        self._model_slug_refs: set[str] = set()
        # Raw fetched row lists (kept for family builders).
        self._raw: dict[str, list] = {}
        # Definition parse failures (spec §5.5): (owner_type, owner_id, field, reason).
        self._unresolved: list[tuple[str, str, str, str]] = []
        # Set by _unique(): True when the last name lookup was ambiguous (>1 match).
        self._last_ambiguous: bool = False

    # -- public -------------------------------------------------------------

    async def load(self, project_id: uuid.UUID, model_id: uuid.UUID) -> ModelDependencySnapshot:
        await self._fetch(project_id, model_id)
        if self._model is None:
            raise KeyError(f"model not found: {model_id}")

        cross_model_measures = await self._load_cross_model_index(project_id, model_id)

        snapshot = ModelDependencySnapshot(
            tenant_id=_tenant_id(self._db),
            project_id=_s(project_id),
            model_id=_s(model_id),
            dependency_revision=int(self._model.dependency_revision or 0),
            model_default_target_id=_opt(self._model.target_id),
            sources=self._build_sources(),
            targets=self._build_targets(),
            tables=self._build_tables(),
            columns=self._build_columns(),
            calendars=self._build_calendars(),
            udas=self._build_udas(),
            dimensions=self._build_dimensions(),
            hierarchies=self._build_hierarchies(),
            hierarchy_levels=self._build_hierarchy_levels(),
            measures=self._build_measures(),
            relationships=self._build_relationships(),
            aggregates=self._build_aggregates(),
            aggregate_columns=self._build_aggregate_columns(),
            pockets=self._build_pockets(),
            kpis=self._build_kpis(),
            drill_through_sets=self._build_drill_through(),
            named_lists=self._build_named_lists(),
            saved_queries=(),  # v1: saved queries are a frontend/gateway artifact
            saved_pivots=(),   # v1: saved pivots are a frontend artifact
            data_tags=self._build_data_tags(),
            personas=self._build_personas(),
            project_persona_scopes=(),  # project personas resolved at project scope
            row_security_rules=self._build_row_security(),
            model_parameters=self._build_model_parameters(),
            glossary_attachments=self._build_glossary(),
            data_quality_rules=self._build_data_quality(),
            scratchpad_measures=self._build_scratchpad(),
            lineage_mappings=self._build_lineage(),
            translations=self._build_translations(),
            user_preferences=self._build_preferences(),
            alias_maps=self._build_alias_maps(),
            cross_model_recipes=self._build_recipes(project_id),
            agent_groundings=self._build_agent_groundings(),
            cross_model_measures=cross_model_measures,
            unresolved_definitions=tuple(sorted(set(self._unresolved))),
            route_templates=dict(_ROUTE_TEMPLATES),
        )
        return snapshot

    async def object_required_tables(
        self,
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        """Anchor + required-table set per object for structural_paths (spec §7.4).

        A dimension anchors on its backing column's table and, to be queryable,
        must remain join-reachable to the FACT table (where measures aggregate) —
        so its required set is {own_table, fact_table}. A measure anchors on the
        fact table and requires it. Derived from already-fetched rows — no new SQL.
        A relationship change that disconnects a dimension's table from the fact
        therefore hard-breaks that dimension (§7.4), while a redundant alternate
        join path leaves it reachable (no false hard break).
        """
        out: dict[str, tuple[str, tuple[str, ...]]] = {}
        col_table = {cid: _s(c.model_table_id) for cid, c in self._columns.items()}
        # The fact table is the single fact-typed model table (F-013-11 enforces
        # at most one). Iterate tables in stable id order so the result is
        # deterministic regardless of DB fetch order (§6.2 step 5).
        fact_id: Optional[str] = None
        for tid in sorted(self._tables):
            if is_fact_table(self._tables[tid]):
                fact_id = tid
                break
        # Deterministic fallback when no fact-typed table exists (mid-build draft):
        # the lowest-id table backing any measure.
        if fact_id is None:
            measure_tables = sorted(
                {col_table[_s(m.source_column_id)]
                 for m in self._measures.values()
                 if m.source_column_id and _s(m.source_column_id) in col_table}
            )
            fact_id = measure_tables[0] if measure_tables else None
        for mid, m in self._measures.items():
            if m.source_column_id:
                t = col_table.get(_s(m.source_column_id))
                if t:
                    out[mid] = (t, (t,))
        for did, d in self._dimensions.items():
            if d.source_column_id:
                t = col_table.get(_s(d.source_column_id))
                if t:
                    required = (t,) if (fact_id is None or fact_id == t) else (t, fact_id)
                    out[did] = (t, required)
        return out

    # -- batched fetch ------------------------------------------------------

    async def _fetch(self, project_id: uuid.UUID, model_id: uuid.UUID) -> None:
        db = self._db
        self._model = await db.get(Model, model_id)
        if self._model is None or self._model.project_id != project_id:
            self._model = None
            return

        async def q(stmt) -> list:
            return list((await db.execute(stmt)).scalars().all())

        # One query per family, all scoped by model_id (fixed query count).
        sources = await q(
            select(DataSource).where(DataSource.model_id == model_id)
        )
        targets = await q(
            select(DataTarget).where(DataTarget.model_id == model_id)
        )
        tables = await q(select(ModelTable).where(ModelTable.model_id == model_id))
        table_ids = [t.id for t in tables]
        columns = (
            await q(select(ModelColumn).where(ModelColumn.model_table_id.in_(table_ids)))
            if table_ids else []
        )
        source_ids = [sc.id for sc in sources]
        calendars = (
            await q(select(CalendarTable).where(CalendarTable.data_source_id.in_(source_ids)))
            if source_ids else []
        )
        udas = await q(select(UserDefinedAttribute).where(UserDefinedAttribute.model_id == model_id))
        uda_ids = [u.id for u in udas]
        uda_refs = (
            await q(select(UserDefinedAttributeColumnRef).where(
                UserDefinedAttributeColumnRef.attribute_id.in_(uda_ids)))
            if uda_ids else []
        )
        dimensions = await q(select(Dimension).where(Dimension.model_id == model_id))
        measures = await q(select(Measure).where(Measure.model_id == model_id))
        hierarchies = await q(select(HierarchyDefinition).where(HierarchyDefinition.model_id == model_id))
        hier_ids = [h.id for h in hierarchies]
        levels = (
            await q(select(HierarchyLevel).where(HierarchyLevel.hierarchy_id.in_(hier_ids)))
            if hier_ids else []
        )
        level_ids = [lv.id for lv in levels]
        level_attrs = (
            await q(select(HierarchyLevelAttribute).where(
                HierarchyLevelAttribute.level_id.in_(level_ids)))
            if level_ids else []
        )
        joins = await q(select(Join).where(Join.model_id == model_id))
        aggregates = await q(select(AggregateDefinition).where(AggregateDefinition.model_id == model_id))
        agg_ids = [a.id for a in aggregates]
        agg_cols = (
            await q(select(AggregateColumn).where(
                AggregateColumn.aggregate_definition_id.in_(agg_ids)))
            if agg_ids else []
        )
        refresh_deps = (
            await q(select(RefreshDependency).where(
                RefreshDependency.downstream_aggregate_id.in_(agg_ids)))
            if agg_ids else []
        )
        pockets = await q(select(PocketDefinition).where(
            PocketDefinition.model_id == model_id, PocketDefinition.retired_at.is_(None)))
        pocket_ids = [p.id for p in pockets]
        predicates = (
            await q(select(PocketPredicate).where(
                PocketPredicate.pocket_definition_id.in_(pocket_ids)))
            if pocket_ids else []
        )
        kpis = await q(select(KPI).where(KPI.model_id == model_id))
        named_sets = await q(select(NamedSet).where(NamedSet.model_id == model_id))
        measure_ids = [m.id for m in measures]
        drill_sets = (
            await q(select(DrillThroughSet).where(DrillThroughSet.measure_id.in_(measure_ids)))
            if measure_ids else []
        )
        data_tags = await q(select(DataTag).where(DataTag.model_id == model_id))
        data_tag_ids = [t.id for t in data_tags]
        tag_column_rows = (
            list((await db.execute(
                select(data_tag_columns.c.tag_id, data_tag_columns.c.model_column_id)
                .where(data_tag_columns.c.tag_id.in_(data_tag_ids))
            )).all())
            if data_tag_ids else []
        )
        personas = await q(select(Persona).where(Persona.model_id == model_id))
        persona_ids = [p.id for p in personas]
        tag_restrictions = (
            await q(select(PersonaTagRestriction).where(
                PersonaTagRestriction.persona_id.in_(persona_ids)))
            if persona_ids else []
        )
        row_security = await q(select(RowSecurityRule).where(RowSecurityRule.model_id == model_id))
        parameters = await q(select(ModelParameter).where(ModelParameter.model_id == model_id))
        glossary_entries = await q(select(GlossaryEntry).where(GlossaryEntry.model_id == model_id))
        entry_ids = [g.id for g in glossary_entries]
        glossary_attachments = (
            await q(select(GlossaryAttachment).where(GlossaryAttachment.entry_id.in_(entry_ids)))
            if entry_ids else []
        )
        data_quality = await q(select(DataQualityRule).where(DataQualityRule.model_id == model_id))
        scratchpad = await q(select(ScratchpadMeasure).where(ScratchpadMeasure.model_id == model_id))
        lineage = await q(select(LineageMapping).where(LineageMapping.model_id == model_id))
        translations = await q(select(EntityTranslation).where(EntityTranslation.model_id == model_id))
        preferences = await q(select(UserEntityPreference).where(UserEntityPreference.model_id == model_id))
        alias_map = await db.get(ModelAliasMap, model_id)
        agent_links = await q(select(ProjectAgentModel).where(ProjectAgentModel.model_id == model_id))
        recipes = await q(
            select(ProjectCrossModelRecipe).where(ProjectCrossModelRecipe.project_id == project_id)
        )

        # Index the entity-level rows.
        self._tables = {_s(t.id): t for t in tables}
        self._columns = {_s(c.id): c for c in columns}
        self._calendars = {_s(c.id): c for c in calendars}
        self._udas = {_s(u.id): u for u in udas}
        self._dimensions = {_s(d.id): d for d in dimensions}
        self._measures = {_s(m.id): m for m in measures}
        self._hierarchies = {_s(h.id): h for h in hierarchies}

        for t in tables:
            self._table_alias_to_id[(t.alias or "")] = _s(t.id)
            # Index by ALIAS and by PHYSICAL name (case-normalized). Pocket
            # ``defining_sql`` frequently names the physical table, not the model
            # alias; matching only the alias would silently drop the pocket's
            # table/column edges (a MISSED impact tuple, §12.7).
            if t.alias:
                self._table_by_ref[t.alias.lower()] = _s(t.id)
            if t.physical_name:
                # Match the last dotted segment too (schema.table -> table).
                phys = t.physical_name.lower()
                self._table_by_ref[phys] = _s(t.id)
                self._table_by_ref[phys.rsplit(".", 1)[-1]] = _s(t.id)
        # Pocket ``defining_sql`` is SEMANTIC SQL whose FROM is the MODEL SLUG
        # (or ``<slug>_technical``), NOT a model table — the enforced pocket
        # grammar (shared/pocket/structure.py) requires ``SELECT * FROM <slug>``.
        # These are recognized self-references so the slug FROM is never treated
        # as an unknown external table (which would fail-close every valid pocket).
        slug = (self._model.slug or "").lower() if self._model else ""
        self._model_slug_refs = {slug, f"{slug}_technical"} if slug else set()
        for c in columns:
            name = (c.column_name or "").lower()
            self._column_by_name[name].append(_s(c.id))
            self._column_in_table[(_s(c.model_table_id), name)] = _s(c.id)
        for m in measures:
            self._measure_by_name[(m.name or "").lower()].append(_s(m.id))
        for d in dimensions:
            self._dimension_by_name[(d.name or "").lower()].append(_s(d.id))
        for k in kpis:
            self._kpi_by_name[(k.name or "").lower()].append(_s(k.id))
        for h in hierarchies:
            self._hierarchy_by_name[(h.name or "").lower()].append(_s(h.id))
        for ns in named_sets:
            self._namedset_by_name[(ns.name or "").lower()].append(_s(ns.id))
        for p in parameters:
            self._param_by_name[(p.name or "").lower()].append(_s(p.id))

        # UDA -> column ref ids.
        uda_col_refs: dict[str, list[str]] = defaultdict(list)
        for r in uda_refs:
            uda_col_refs[_s(r.attribute_id)].append(_s(r.column_id))
        # persona -> restricted tag ids.
        persona_tags: dict[str, list[str]] = defaultdict(list)
        for r in tag_restrictions:
            persona_tags[_s(r.persona_id)].append(_s(r.data_tag_id))
        # aggregate -> upstream (refresh) agg ids.
        agg_refresh: dict[str, list[str]] = defaultdict(list)
        for r in refresh_deps:
            agg_refresh[_s(r.downstream_aggregate_id)].append(_s(r.upstream_aggregate_id))
        # pocket -> predicate column names.
        pocket_preds: dict[str, list[str]] = defaultdict(list)
        for r in predicates:
            pocket_preds[_s(r.pocket_definition_id)].append(r.column_name)
        # hierarchy level -> attribute rows.
        level_attr_map: dict[str, list[HierarchyLevelAttribute]] = defaultdict(list)
        for r in level_attrs:
            level_attr_map[_s(r.level_id)].append(r)
        # glossary entry lookup for attachment name.
        entry_by_id = {_s(g.id): g for g in glossary_entries}
        # data tag -> column ids (from the association table, no lazy load).
        tag_cols: dict[str, list[str]] = defaultdict(list)
        for tag_id, col_id in tag_column_rows:
            tag_cols[_s(tag_id)].append(_s(col_id))

        self._raw = {
            "sources": sources, "targets": targets, "tables": tables,
            "columns": columns, "calendars": calendars, "udas": udas,
            "uda_col_refs": uda_col_refs, "dimensions": dimensions,
            "measures": measures, "hierarchies": hierarchies, "levels": levels,
            "level_attr_map": level_attr_map, "joins": joins,
            "aggregates": aggregates, "agg_cols": agg_cols, "agg_refresh": agg_refresh,
            "pockets": pockets, "pocket_preds": pocket_preds, "kpis": kpis,
            "named_sets": named_sets, "drill_sets": drill_sets, "data_tags": data_tags,
            "tag_cols": tag_cols,
            "personas": personas, "persona_tags": persona_tags,
            "row_security": row_security, "parameters": parameters,
            "glossary_attachments": glossary_attachments, "entry_by_id": entry_by_id,
            "data_quality": data_quality, "scratchpad": scratchpad, "lineage": lineage,
            "translations": translations, "preferences": preferences,
            "alias_map": alias_map, "agent_links": agent_links,
            "recipes": recipes,
        }

    async def _load_cross_model_index(
        self, project_id: uuid.UUID, model_id: uuid.UUID
    ) -> tuple[tuple[str, str, str, str], ...]:
        """Same-project measures in OTHER models that reference THIS model's
        measures (spec §12.3 reverse index). Cross-PROJECT ids are impossible by
        construction here: the query is scoped to the same project, so a measure
        naming a model in another project simply is not returned. Every same-project
        measure with a non-null cross_model_source_measure_id IS returned in the
        index (including one whose referenced id no longer exists in THIS model);
        the engine's `_build_cross_model_reverse` then fails such a dangling
        reference closed (records it as an unresolved diagnostic on the referencing
        measure) rather than dropping it — a fail-safe direction."""
        db = self._db
        # All models in this project except the target model.
        other_model_ids = list(
            (await db.execute(
                select(Model.id).where(
                    Model.project_id == project_id, Model.id != model_id
                )
            )).scalars().all()
        )
        if not other_model_ids:
            return ()
        rows = list((await db.execute(
            select(Measure).where(
                Measure.model_id.in_(other_model_ids),
                Measure.cross_model_source_model_id == model_id,
                Measure.cross_model_source_measure_id.isnot(None),
            )
        )).scalars().all())
        index: list[tuple[str, str, str, str]] = []
        for m in rows:
            index.append((
                _s(m.model_id), _s(m.id), m.name or "",
                _s(m.cross_model_source_measure_id),
            ))
        return tuple(sorted(set(index)))

    # -- change-simulation re-resolution (spec §7.3) ------------------------

    def resolve_calc_measure_expression(self, expression: str) -> Optional[tuple[str, ...]]:
        """Re-resolve a PROPOSED calculated-measure expression to measure IDs for
        change simulation. Returns None when the proposed definition CANNOT be fully
        resolved — either a parse failure OR any referenced measure NAME that does
        not resolve to a unique live measure (including an ambiguous duplicate name,
        §12.4). The caller treats None as a change that FAILS CLOSED: a proposed
        definition that references a missing/ambiguous measure must block, not be
        silently treated as "no references". Requires a prior ``load()``."""
        from shared.semantic.calculated_expression import (
            ExpressionValidationError,
            parse_expression,
        )
        if not expression:
            return ()
        try:
            parsed = parse_expression(expression)
        except ExpressionValidationError:
            return None
        ids: list[str] = []
        for name in parsed.referenced_names:
            rid = self._resolve_measure_name(name)
            if not rid:
                # Unresolvable (missing or ambiguous) referenced name -> fail closed.
                return None
            ids.append(rid)
        return tuple(dict.fromkeys(ids))

    def resolve_calc_dimension_expression(
        self, expression: str
    ) -> Optional[tuple[tuple[str, ...], tuple[str, ...]]]:
        """Re-resolve a PROPOSED calc-dimension expression to (table_ids,
        column_ids). Returns None when the proposed definition CANNOT be fully
        resolved — a parse failure OR any referenced column that does not resolve
        to a live model column. The caller fails such a change closed (a proposed
        calc dimension that names a missing column must block, not silently drop
        the reference)."""
        from shared.semantic.calc_dimension_validator import (
            CalcDimensionValidationError,
            validate_calc_expression,
        )
        if not expression:
            return ((), ())
        try:
            result = validate_calc_expression(
                expression, model_columns=self._raw["columns"],
                model_tables=self._raw["tables"],
            )
        except CalcDimensionValidationError:
            return None
        table_ids = {_s(t) for t in result.table_ids}
        col_ids: set[str] = set()
        for ref in result.column_refs:
            resolved = self._resolve_column_ref(ref.table, ref.column)
            if not resolved:
                # A referenced column name resolves to no live column -> fail closed.
                return None
            for cid in resolved:
                col_ids.add(cid)
        return tuple(sorted(table_ids)), tuple(sorted(col_ids))

    # -- reference resolution helpers ---------------------------------------

    def _unique(self, index: dict[str, list[str]], name: str) -> Optional[str]:
        """Resolve a case-normalized name to a UNIQUE ID. Spec §12.4: the loader
        never chooses an arbitrary match — a duplicate/ambiguous name resolves to
        None (fail closed), and the caller records the ambiguity so the engine
        emits a fail-closed unresolved node instead of silently binding the wrong
        object. ``self._last_ambiguous`` lets the caller label the reason."""
        matches = index.get((name or "").lower(), [])
        if len(matches) == 1:
            self._last_ambiguous = False
            return matches[0]
        self._last_ambiguous = len(matches) > 1
        return None

    def _resolve_measure_name(self, name: str) -> Optional[str]:
        return self._unique(self._measure_by_name, name)

    def _resolve_dimension_name(self, name: str) -> Optional[str]:
        return self._unique(self._dimension_by_name, name)

    def _resolve_kpi_name(self, name: str) -> Optional[str]:
        return self._unique(self._kpi_by_name, name)

    def _resolve_hierarchy_name(self, name: str) -> Optional[str]:
        return self._unique(self._hierarchy_by_name, name)

    def _resolve_named_list_name(self, name: str) -> Optional[str]:
        return self._unique(self._namedset_by_name, name)

    def _resolve_param_name(self, name: str) -> Optional[str]:
        return self._unique(self._param_by_name, name)

    def _reason(self, prefix: str, name: str) -> str:
        """Reason code for an unresolved name reference; distinguishes an ambiguous
        (duplicate) name from a genuinely missing one (spec §12.4)."""
        kind = "ambiguous" if getattr(self, "_last_ambiguous", False) else "unresolved"
        return f"{kind}_{prefix}:{name}"

    def _resolve_column_ref(self, table_alias: Optional[str], column: str) -> list[str]:
        """Resolve a (table_alias?, column_name) reference to column IDs.

        When the alias is known, resolve within that table; otherwise return all
        columns of that name (ambiguity is carried, not silently collapsed)."""
        name = (column or "").lower()
        if table_alias:
            tid = self._table_alias_to_id.get(table_alias)
            if tid:
                cid = self._column_in_table.get((tid, name))
                return [cid] if cid else []
        return list(self._column_by_name.get(name, []))

    def _note_unresolved(self, owner_type: str, owner_id: str, field: str, reason: str) -> None:
        self._unresolved.append((owner_type, _s(owner_id), field, reason))

    # -- family builders ----------------------------------------------------

    def _build_sources(self) -> tuple[SourceRow, ...]:
        return tuple(
            SourceRow(id=_s(s.id), name=s.display_name, display_name=s.display_name,
                      project_connection_id=_opt(s.project_connection_id))
            for s in self._raw["sources"]
        )

    def _build_targets(self) -> tuple[TargetRow, ...]:
        return tuple(
            TargetRow(id=_s(t.id), name=t.display_name, display_name=t.display_name,
                      project_connection_id=_opt(t.project_connection_id))
            for t in self._raw["targets"]
        )

    def _build_tables(self) -> tuple[TableRow, ...]:
        return tuple(
            TableRow(id=_s(t.id), name=t.alias or t.physical_name, display_name=t.display_name,
                     source_id=_s(t.source_id), calendar_table_id=_opt(t.calendar_table_id))
            for t in self._raw["tables"]
        )

    def _build_columns(self) -> tuple[ColumnRow, ...]:
        return tuple(
            ColumnRow(id=_s(c.id), name=c.column_name,
                      display_name=c.display_name or c.column_name,
                      table_id=_s(c.model_table_id))
            for c in self._raw["columns"]
        )

    def _build_calendars(self) -> tuple[CalendarRow, ...]:
        return tuple(
            CalendarRow(id=_s(c.id), name=c.table_name, display_name=c.table_name,
                        source_id=_opt(c.data_source_id))
            for c in self._raw["calendars"]
        )

    def _build_udas(self) -> tuple[UdaRow, ...]:
        col_refs = self._raw["uda_col_refs"]
        return tuple(
            UdaRow(id=_s(u.id), name=u.name, display_name=u.name, table_id=_s(u.table_id),
                   column_ref_ids=tuple(col_refs.get(_s(u.id), ())))
            for u in self._raw["udas"]
        )

    def _build_dimensions(self) -> tuple[DimensionRow, ...]:
        rows: list[DimensionRow] = []
        for d in self._raw["dimensions"]:
            calc_tables: tuple[str, ...] = ()
            calc_cols: tuple[str, ...] = ()
            if d.calc_expression:
                calc_tables, calc_cols = self._resolve_calc_dimension(d)
            rows.append(DimensionRow(
                id=_s(d.id), name=d.name, display_name=d.display_name or d.name,
                source_column_id=_opt(d.source_column_id),
                display_column_id=_opt(d.display_column_id),
                user_defined_attribute_id=_opt(d.user_defined_attribute_id),
                calc_expression=d.calc_expression,
                calc_expression_tables=calc_tables,
                calc_expression_column_ids=calc_cols,
                hierarchy_json=d.hierarchy if isinstance(d.hierarchy, dict) else None,
            ))
        return tuple(rows)

    def _resolve_calc_dimension(self, d: Dimension) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Resolve a calc-dimension expression to (table_ids, column_ids). On parse
        failure populate unresolved_definitions and fall back to the persisted
        ``calc_expression_tables`` (already IDs) so the edge is not lost."""
        from shared.semantic.calc_dimension_validator import (
            CalcDimensionValidationError,
            validate_calc_expression,
        )
        persisted_tables = tuple(_s(t) for t in (d.calc_expression_tables or []) if t)
        try:
            result = validate_calc_expression(
                d.calc_expression,
                model_columns=self._raw["columns"],
                model_tables=self._raw["tables"],
            )
        except CalcDimensionValidationError:
            self._note_unresolved("dimension", d.id, "calc_expression", "calc_dimension_parse_failure")
            return persisted_tables, ()
        table_ids = set(persisted_tables) | {_s(t) for t in result.table_ids}
        col_ids: set[str] = set()
        for ref in result.column_refs:
            for cid in self._resolve_column_ref(ref.table, ref.column):
                col_ids.add(cid)
        return tuple(sorted(table_ids)), tuple(sorted(col_ids))

    def _build_hierarchies(self) -> tuple[HierarchyRow, ...]:
        return tuple(
            HierarchyRow(id=_s(h.id), name=h.name, display_name=h.name)
            for h in self._raw["hierarchies"]
        )

    def _build_hierarchy_levels(self) -> tuple[HierarchyLevelRow, ...]:
        level_attr_map = self._raw["level_attr_map"]
        rows: list[HierarchyLevelRow] = []
        for lv in self._raw["levels"]:
            attrs = tuple(
                (_s(a.attribute_id), a.attribute_source, a.role)
                for a in level_attr_map.get(_s(lv.id), [])
            )
            rows.append(HierarchyLevelRow(
                id=_s(lv.id), name=lv.name, display_name=lv.name,
                hierarchy_id=_s(lv.hierarchy_id),
                key_attribute_id=_s(lv.key_attribute_id),
                key_attribute_source=lv.key_attribute_source,
                attributes=attrs,
            ))
        return tuple(rows)

    def _build_measures(self) -> tuple[MeasureRow, ...]:
        rows: list[MeasureRow] = []
        for m in self._raw["measures"]:
            calc_refs: tuple[str, ...] = ()
            if (m.measure_type or "").lower() in ("calculated", "calc") and m.expression:
                calc_refs = self._resolve_calc_measure(m)
            rows.append(MeasureRow(
                id=_s(m.id), name=m.name, display_name=m.display_name or m.name,
                source_column_id=_opt(m.source_column_id),
                semi_additive_account_column_id=_opt(m.semi_additive_account_column_id),
                resolved_date_col_id=_opt(m.resolved_date_col_id),
                date_dimension_column_id=_opt(m.date_dimension_column_id),
                user_defined_attribute_id=_opt(m.user_defined_attribute_id),
                calendar_model_table_id=_opt(m.calendar_model_table_id),
                hierarchy_id=_opt(m.hierarchy_id),
                resolved_calendar_id=_opt(m.resolved_calendar_id),
                variant_of_measure_id=_opt(m.variant_of_measure_id),
                cross_model_source_model_id=_opt(m.cross_model_source_model_id),
                cross_model_source_measure_id=_opt(m.cross_model_source_measure_id),
                calc_expression=m.expression,
                calc_reference_ids=calc_refs,
            ))
        return tuple(rows)

    def _resolve_calc_measure(self, m: Measure) -> tuple[str, ...]:
        """Resolve a calculated measure's ``measure("name")`` refs to IDs. On parse
        failure, populate unresolved_definitions (fail closed)."""
        from shared.semantic.calculated_expression import (
            ExpressionValidationError,
            parse_expression,
        )
        try:
            parsed = parse_expression(m.expression)
        except ExpressionValidationError:
            self._note_unresolved("measure", m.id, "expression", "calc_measure_parse_failure")
            return ()
        ids: list[str] = []
        for name in parsed.referenced_names:
            rid = self._resolve_measure_name(name)
            if rid:
                ids.append(rid)
            else:
                self._note_unresolved("measure", m.id, "expression",
                                      self._reason("measure_name", name))
        return tuple(dict.fromkeys(ids))

    def _build_relationships(self):
        from shared.model_dependency.snapshot import RelationshipRow
        return tuple(
            RelationshipRow(
                id=_s(j.id), name=f"join_{_s(j.id)[:8]}", display_name=f"Join {_s(j.id)[:8]}",
                left_table_id=_s(j.left_table_id), right_table_id=_s(j.right_table_id),
                left_column_id=_opt(j.left_column_id), right_column_id=_opt(j.right_column_id),
            )
            for j in self._raw["joins"]
        )

    def _build_aggregates(self) -> tuple[AggregateRow, ...]:
        agg_refresh = self._raw["agg_refresh"]
        rows: list[AggregateRow] = []
        for a in self._raw["aggregates"]:
            grain_ids = self._resolve_agg_grain(a)
            # §7.6: ``serves_when_stale`` marks an aggregate that would KEEP serving
            # stale results with NO safe source fallback if a grain/measure
            # dependency were lost — the only case where losing that dependency
            # stays a HARD impact. It is NOT operational refresh state:
            # ``AggregateDefinition.is_stale`` is toggled by the scheduler on
            # refresh failure/success and says nothing about fallback safety, so it
            # must NOT drive this flag (doing so wrongly hardens every mid-staleness
            # aggregate's soft grain/measure impact and nullifies the §7.6
            # relaxation). In v1 aggregates are transparent accelerators that fall
            # back to source, so this is always False. A future non-invalidatable /
            # materialized-serving property would set it; there is none today.
            serves_stale = False
            rows.append(AggregateRow(
                id=_s(a.id), name=a.physical_table_name, display_name=a.physical_table_name,
                target_id=_opt(a.target_id), persona_id=_opt(a.persona_id),
                grain_dimension_ids=grain_ids,
                refresh_dependency_ids=tuple(agg_refresh.get(_s(a.id), ())),
                serves_when_stale=serves_stale,
            ))
        return tuple(rows)

    def _resolve_agg_grain(self, a: AggregateDefinition) -> tuple[str, ...]:
        """Resolve an aggregate's grain (dimension names in ``grain`` JSON) to
        dimension IDs. ``grain`` is a list of dimension names or dicts."""
        ids: list[str] = []
        for entry in (a.grain or []):
            name = entry if isinstance(entry, str) else (
                entry.get("dimension") or entry.get("name") if isinstance(entry, dict) else None
            )
            if not name:
                continue
            rid = self._resolve_dimension_name(name)
            if rid:
                ids.append(rid)
            else:
                self._note_unresolved("aggregate", a.id, "grain",
                                      self._reason("dimension_name", name))
        return tuple(dict.fromkeys(ids))

    def _build_aggregate_columns(self) -> tuple[AggregateColumnRow, ...]:
        return tuple(
            AggregateColumnRow(id=_s(c.id), name=c.physical_col_name,
                               display_name=c.physical_col_name,
                               aggregate_id=_s(c.aggregate_definition_id),
                               measure_id=_opt(c.measure_id))
            for c in self._raw["agg_cols"]
        )

    def _build_pockets(self) -> tuple[PocketRow, ...]:
        pocket_preds = self._raw["pocket_preds"]
        rows: list[PocketRow] = []
        for p in self._raw["pockets"]:
            dims, measures, tables = self._resolve_pocket(p, pocket_preds.get(_s(p.id), []))
            rows.append(PocketRow(
                id=_s(p.id), name=p.physical_table_name, display_name=p.physical_table_name,
                target_id=_opt(p.target_id), persona_id=_opt(p.persona_id),
                referenced_dimension_ids=dims,
                referenced_measure_ids=measures,
                referenced_table_ids=tables,
            ))
        return tuple(rows)

    def _resolve_pocket(
        self, p: PocketDefinition, predicate_columns: list[str]
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Resolve pocket references from ``defining_sql`` (SQLGlot parse-only, no
        source access) + persisted predicate column names. On parse failure,
        populate unresolved_definitions (fail closed)."""
        table_ids: set[str] = set()
        column_ids: set[str] = set()
        try:
            tree = sqlglot.parse_one(p.defining_sql, read="postgres")
        except Exception:
            self._note_unresolved("pocket", p.id, "defining_sql", "pocket_sql_parse_failure")
            tree = None
        if tree is not None:
            referenced_tables = list(tree.find_all(exp.Table))
            unresolved_table = False
            for tbl in referenced_tables:
                name = (tbl.name or "").lower()
                # A FROM naming the MODEL SLUG (or <slug>_technical) is the canonical
                # pocket shape (SELECT * FROM <slug>): it references the WHOLE model,
                # so include every model table — a table/column delete then reaches
                # the pocket, and it is NOT an unresolved external table.
                if name in self._model_slug_refs or \
                        name.rsplit(".", 1)[-1] in self._model_slug_refs:
                    table_ids.update(self._tables.keys())
                    continue
                # Otherwise resolve by model alias OR physical name (last segment).
                tid = self._table_by_ref.get(name) or self._table_by_ref.get(
                    name.rsplit(".", 1)[-1]
                )
                if tid:
                    table_ids.add(tid)
                else:
                    unresolved_table = True
            # §12.7: a pocket whose SQL parses but names a table that is neither a
            # model table NOR the model slug is an OPAQUE definition — fail closed
            # so the guard blocks a delete whose target the pocket might reference,
            # rather than treating the empty resolved set as "no references".
            if unresolved_table:
                self._note_unresolved("pocket", p.id, "defining_sql",
                                      "pocket_sql_unresolved_table")
            # SELECT/projection columns: physical resolution only (no fail-close;
            # a projection alias is not a dependency signal).
            for col in tree.find_all(exp.Column):
                for cid in self._resolve_column_ref(col.table or None, col.name):
                    column_ids.add(cid)
        dim_ids: set[str] = set()
        measure_ids: set[str] = set()
        # Pocket SQL is SEMANTIC (over the model slug): WHERE / predicate
        # identifiers are dimension/measure NAMES matched at runtime against
        # bound_query field names (query-router pocket_matcher), NOT physical
        # column names. Resolve each WHERE/predicate identifier as a
        # dimension/measure name FIRST, then fall back to a physical column, and
        # fail closed when none resolves — otherwise a pocket whose predicate names
        # a dimension whose name differs from its backing column silently drops
        # that dimension edge and deleting the dimension would preview "allowed"
        # (§5.3 pocket=hard, §12.7).
        where_names: list[str] = []
        if tree is not None:
            where = tree.args.get("where")
            if where is not None:
                for col in where.find_all(exp.Column):
                    where_names.append(col.name)
        where_names.extend(predicate_columns)
        for name in where_names:
            did = self._resolve_dimension_name(name)
            # Capture ambiguity of the PRIMARY (dimension) namespace before the
            # measure/column lookups overwrite _last_ambiguous, so the fail-close
            # reason labels an ambiguous dimension name correctly (§12.4).
            dim_ambiguous = self._last_ambiguous
            if did:
                dim_ids.add(did)
                continue
            mid = self._resolve_measure_name(name)
            if mid:
                measure_ids.add(mid)
                continue
            phys = self._resolve_column_ref(None, name)
            if phys:
                for cid in phys:
                    column_ids.add(cid)
            elif name:
                # A predicate identifier that is neither a dimension, measure, nor
                # physical column is an opaque reference -> fail closed. Preserve
                # the dimension-namespace ambiguity label.
                self._last_ambiguous = dim_ambiguous
                self._note_unresolved("pocket", p.id, "predicate",
                                      self._reason("pocket_field", name))
        # Map referenced physical columns/tables to the dimensions/measures they
        # back (covers physical-column-named predicates + SELECT columns).
        for d in self._raw["dimensions"]:
            if d.source_column_id and _s(d.source_column_id) in column_ids:
                dim_ids.add(_s(d.id))
        for m in self._raw["measures"]:
            if m.source_column_id and _s(m.source_column_id) in column_ids:
                measure_ids.add(_s(m.id))
        return (
            tuple(sorted(dim_ids)),
            tuple(sorted(measure_ids)),
            tuple(sorted(table_ids)),
        )

    def _build_kpis(self) -> tuple[KpiRow, ...]:
        rows: list[KpiRow] = []
        for k in self._raw["kpis"]:
            measure_ids, dim_ids, kpi_ids = self._resolve_kpi(k)
            rows.append(KpiRow(
                id=_s(k.id), name=k.name, display_name=k.display_name or k.name,
                measure_ids=measure_ids, dimension_ids=dim_ids,
                time_dimension_id=_opt(k.time_dimension_id),
                referenced_kpi_ids=kpi_ids,
                parent_kpi_id=_opt(k.parent_kpi_id),
                replacement_kpi_id=_opt(k.replacement_id),
            ))
        return tuple(rows)

    def _resolve_kpi(self, k: KPI) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Resolve a KPI expression's measure/kpi/dimension name refs to IDs, plus
        the legacy FK measure fields. On parse failure, populate
        unresolved_definitions (fail closed)."""
        measure_ids: list[str] = []
        dim_ids: list[str] = []
        kpi_ids: list[str] = []
        # Legacy value/goal/target measure FKs are real references.
        for fk in (k.value_measure_id, k.goal_measure_id, k.target_measure_id):
            if fk:
                measure_ids.append(_s(fk))
        if k.expression:
            from shared.semantic.kpi_expression import (
                KPIExpressionError,
                _collect_references,
                parse_kpi_expression,
            )
            try:
                ast = parse_kpi_expression(k.expression)
                m_names, kpi_names, d_names = _collect_references(ast)
            except (KPIExpressionError, Exception):  # noqa: BLE001 — parse failure -> fail closed
                self._note_unresolved("kpi", k.id, "expression", "kpi_expression_parse_failure")
                m_names = kpi_names = d_names = []
            for name in m_names:
                rid = self._resolve_measure_name(name)
                if rid:
                    measure_ids.append(rid)
                else:
                    self._note_unresolved("kpi", k.id, "expression", self._reason("measure_name", name))
            for name in kpi_names:
                rid = self._resolve_kpi_name(name)
                if rid:
                    kpi_ids.append(rid)
                else:
                    self._note_unresolved("kpi", k.id, "expression", self._reason("kpi_name", name))
            for name in d_names:
                rid = self._resolve_dimension_name(name)
                if rid:
                    dim_ids.append(rid)
                else:
                    self._note_unresolved("kpi", k.id, "expression", self._reason("dimension_name", name))
        return (
            tuple(dict.fromkeys(measure_ids)),
            tuple(dict.fromkeys(dim_ids)),
            tuple(dict.fromkeys(kpi_ids)),
        )

    def _build_drill_through(self) -> tuple[DrillThroughRow, ...]:
        rows: list[DrillThroughRow] = []
        for dt in self._raw["drill_sets"]:
            detail_cols = tuple(_s(c) for c in (dt.detail_columns or []) if c)
            joined_dims = tuple(_s(d) for d in (dt.joined_dimension_ids or []) if d)
            join_rels = tuple(_s(r) for r in (dt.source_join_path or []) if r)
            rows.append(DrillThroughRow(
                id=_s(dt.id),
                name=f"drill_{_s(dt.measure_id)[:8]}",
                display_name=f"Drill-through {_s(dt.measure_id)[:8]}",
                measure_id=_opt(dt.measure_id),
                source_table_id=_opt(dt.source_table_id),
                detail_column_ids=detail_cols,
                joined_dimension_ids=joined_dims,
                join_path_relationship_ids=join_rels,
            ))
        return tuple(rows)

    def _build_named_lists(self) -> tuple[NamedListRow, ...]:
        rows: list[NamedListRow] = []
        for ns in self._raw["named_sets"]:
            dim_ids, hier_ids, measure_ids = self._resolve_named_list(ns)
            rows.append(NamedListRow(
                id=_s(ns.id), name=ns.name, display_name=ns.display_name or ns.name,
                dimension_ids=dim_ids, hierarchy_ids=hier_ids, measure_ids=measure_ids,
                replacement_id=_opt(ns.replacement_id),
            ))
        return tuple(rows)

    def _resolve_named_list(self, ns: NamedSet) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Resolve a named list's ``builder_definition`` JSON (dimension/hierarchy/
        measure names) to IDs. A malformed builder def populates
        unresolved_definitions (fail closed)."""
        dim_ids: list[str] = []
        hier_ids: list[str] = []
        measure_ids: list[str] = []
        bd = ns.builder_definition
        if not isinstance(bd, dict):
            return (), (), ()
        # entity is "dimension.hierarchy.level" or plain "dimension".
        entity = bd.get("entity") or bd.get("dimension")
        if isinstance(entity, str) and entity:
            parts = entity.split(".")
            dim_name = parts[0]
            rid = self._resolve_dimension_name(dim_name)
            if rid:
                dim_ids.append(rid)
            else:
                self._note_unresolved("named_list", ns.id, "builder_definition",
                                      self._reason("dimension_name", dim_name))
            if len(parts) > 1:
                hid = self._resolve_hierarchy_name(parts[1])
                if hid:
                    hier_ids.append(hid)
        hier_field = bd.get("hierarchy")
        if isinstance(hier_field, str) and hier_field and "." not in (entity or ""):
            hid = self._resolve_hierarchy_name(hier_field)
            if hid:
                hier_ids.append(hid)
        measure_name = bd.get("measure")
        if isinstance(measure_name, str) and measure_name:
            mid = self._resolve_measure_name(measure_name)
            if mid:
                measure_ids.append(mid)
            else:
                self._note_unresolved("named_list", ns.id, "builder_definition",
                                      self._reason("measure_name", measure_name))
        return (
            tuple(dict.fromkeys(dim_ids)),
            tuple(dict.fromkeys(hier_ids)),
            tuple(dict.fromkeys(measure_ids)),
        )

    def _build_data_tags(self) -> tuple[DataTagRow, ...]:
        tag_cols = self._raw["tag_cols"]
        rows: list[DataTagRow] = []
        for t in self._raw["data_tags"]:
            col_ids = tuple(tag_cols.get(_s(t.id), ()))
            rows.append(DataTagRow(id=_s(t.id), name=t.tag_name, display_name=t.tag_name,
                                   column_ids=col_ids))
        return tuple(rows)

    def _build_personas(self) -> tuple[PersonaRow, ...]:
        persona_tags = self._raw["persona_tags"]
        rows: list[PersonaRow] = []
        for p in self._raw["personas"]:
            default_dims = self._resolve_persona_default_filters(p)
            rows.append(PersonaRow(
                id=_s(p.id), name=p.name, display_name=p.name,
                included_dimension_ids=tuple(_s(d) for d in (p.included_dimension_ids or [])),
                included_measure_ids=tuple(_s(m) for m in (p.included_measure_ids or [])),
                included_hierarchy_ids=tuple(_s(h) for h in (p.included_hierarchy_ids or [])),
                default_filter_dimension_ids=default_dims,
                restricted_data_tag_ids=tuple(persona_tags.get(_s(p.id), ())),
            ))
        return tuple(rows)

    def _resolve_persona_default_filters(self, p: Persona) -> tuple[str, ...]:
        """Resolve ``Persona.default_filters`` JSON keys to dimension IDs. Keys may
        be dimension IDs or dimension names."""
        dims: list[str] = []
        default_filters = p.default_filters or {}
        if not isinstance(default_filters, dict):
            return ()
        for key in default_filters.keys():
            skey = _s(key)
            if skey in self._dimensions:
                dims.append(skey)
                continue
            rid = self._resolve_dimension_name(skey)
            if rid:
                dims.append(rid)
            else:
                # §12.4: a default-filter key that is neither a live dimension id
                # nor a uniquely-resolvable dimension name (missing OR ambiguous)
                # is recorded, not silently dropped — so deleting the referenced
                # dimension is not missed and an ambiguous name never binds to an
                # arbitrary match.
                self._note_unresolved("persona", p.id, "default_filters",
                                      self._reason("dimension_name", skey))
        return tuple(dict.fromkeys(dims))

    def _build_row_security(self) -> tuple[RowSecurityRuleRow, ...]:
        rows: list[RowSecurityRuleRow] = []
        for r in self._raw["row_security"]:
            # dimension_path may be a plain name or a dotted path; the bound
            # attribute is the LAST segment — this MUST match the runtime wrap
            # column resolver ``predicate_compiler._path_to_column`` (rsplit last)
            # and the create-time validator (row_security.py, Bug-5206/Bug-7035),
            # which both key on the last segment against Dimension.name. Resolving
            # the FIRST segment binds the rule to the wrong dimension and misses the
            # real one, so deleting the actually-bound dimension would NOT block
            # (§12.6 security fail-open). A rule whose attribute cannot be resolved
            # fails closed against the rule itself, not a phantom dimension.
            dim_name = (r.dimension_path or "").rsplit(".", 1)[-1] if r.dimension_path else ""
            dim_id = self._resolve_dimension_name(dim_name) if dim_name else None
            if r.dimension_path and dim_id is None:
                self._note_unresolved("row_security_rule", r.id, "dimension_path",
                                      self._reason("dimension_path", r.dimension_path))
            mapping_cols = self._resolve_mapping_columns(r)
            rows.append(RowSecurityRuleRow(
                id=_s(r.id), name=r.name, display_name=r.name,
                dimension_id=dim_id, mapping_table_id=_opt(r.mapping_table_id),
                mapping_column_ids=mapping_cols,
            ))
        return tuple(rows)

    def _resolve_mapping_columns(self, r: RowSecurityRule) -> tuple[str, ...]:
        """Resolve the row-security rule's user/value mapping column NAMES to IDs
        within its mapping table."""
        if not r.mapping_table_id:
            return ()
        tid = _s(r.mapping_table_id)
        ids: list[str] = []
        for cname in (r.mapping_user_column, r.mapping_value_column):
            if not cname:
                continue
            cid = self._column_in_table.get((tid, (cname or "").lower()))
            if cid:
                ids.append(cid)
        return tuple(dict.fromkeys(ids))

    def _build_model_parameters(self) -> tuple[ModelParameterRow, ...]:
        return tuple(
            ModelParameterRow(id=_s(p.id), name=p.name, display_name=p.display_name or p.name)
            for p in self._raw["parameters"]
        )

    def _build_glossary(self) -> tuple[GlossaryAttachmentRow, ...]:
        entry_by_id = self._raw["entry_by_id"]
        rows: list[GlossaryAttachmentRow] = []
        for a in self._raw["glossary_attachments"]:
            if a.target_id is None:
                continue
            entry = entry_by_id.get(_s(a.entry_id))
            term = entry.term if entry else "glossary"
            rows.append(GlossaryAttachmentRow(
                id=_s(a.id), name=term, display_name=term,
                target_type=a.target_type, target_id=_s(a.target_id),
            ))
        return tuple(rows)

    def _build_data_quality(self) -> tuple[DataQualityRuleRow, ...]:
        return tuple(
            DataQualityRuleRow(id=_s(r.id), name=r.name, display_name=r.name,
                               target_type=r.target_type, target_id=_s(r.target_id),
                               block_on_failure=bool(r.block_on_failure))
            for r in self._raw["data_quality"]
        )

    def _build_scratchpad(self) -> tuple[ScratchpadMeasureRow, ...]:
        rows: list[ScratchpadMeasureRow] = []
        for sm in self._raw["scratchpad"]:
            ref_ids = self._resolve_scratchpad(sm)
            rows.append(ScratchpadMeasureRow(
                id=_s(sm.id), name=sm.name, display_name=sm.display_name or sm.name,
                calc_expression=sm.expression, reference_ids=ref_ids,
            ))
        return tuple(rows)

    def _resolve_scratchpad(self, sm: ScratchpadMeasure) -> tuple[str, ...]:
        from shared.semantic.calculated_expression import (
            ExpressionValidationError,
            parse_expression,
        )
        if not sm.expression:
            return ()
        try:
            parsed = parse_expression(sm.expression)
        except ExpressionValidationError:
            self._note_unresolved("measure", sm.id, "expression", "scratchpad_parse_failure")
            return ()
        ids: list[str] = []
        for name in parsed.referenced_names:
            rid = self._resolve_measure_name(name)
            if rid:
                ids.append(rid)
        return tuple(dict.fromkeys(ids))

    def _build_lineage(self) -> tuple[LineageMappingRow, ...]:
        return tuple(
            LineageMappingRow(id=_s(lm.id), name=lm.semantic_field_name,
                              display_name=lm.semantic_field_name,
                              source_column_id=_opt(lm.source_column_id),
                              aggregate_col_id=_opt(lm.aggregate_col_id))
            for lm in self._raw["lineage"]
        )

    def _build_translations(self) -> tuple[TranslationRow, ...]:
        return tuple(
            TranslationRow(id=_s(t.id), name=f"{t.entity_type}:{t.locale}",
                           display_name=f"{t.entity_type} ({t.locale})",
                           entity_type=t.entity_type, entity_id=_s(t.entity_id))
            for t in self._raw["translations"]
        )

    def _build_preferences(self) -> tuple[UserPreferenceRow, ...]:
        return tuple(
            UserPreferenceRow(id=_s(p.id), name=p.preference_type, display_name=p.preference_type,
                              entity_type=p.entity_type, entity_id=_s(p.entity_id))
            for p in self._raw["preferences"]
        )

    def _build_alias_maps(self) -> tuple[AliasMapRow, ...]:
        am = self._raw["alias_map"]
        if am is None or not isinstance(am.alias_map, dict) or not am.alias_map:
            return ()
        object_ids = self._resolve_alias_map(am.alias_map)
        return (AliasMapRow(
            id=_s(am.model_id), name="alias_map", display_name="Alias Map",
            referenced_object_ids=object_ids,
        ),)

    def _resolve_alias_map(self, alias_map: dict) -> tuple[str, ...]:
        """Resolve alias-map canonical-attribute targets to object IDs. Values may
        be object IDs or {"type","id"}/{"name"} shapes; unknown names are skipped
        (the engine's _find_any handles a truly missing ID by failing closed)."""
        ids: list[str] = []
        for value in alias_map.values():
            candidate: Optional[str] = None
            if isinstance(value, str):
                candidate = value
            elif isinstance(value, dict):
                candidate = value.get("id") or value.get("object_id")
                name = value.get("name")
                if candidate is None and isinstance(name, str):
                    candidate = (self._resolve_dimension_name(name)
                                 or self._resolve_measure_name(name))
            if candidate:
                ids.append(_s(candidate))
        return tuple(dict.fromkeys(ids))

    def _build_recipes(self, project_id: uuid.UUID) -> tuple[CrossModelRecipeRow, ...]:
        """Project-scoped cross-model recipes (spec §12.3). Resolve object refs
        (measure/dimension names in steps) to THIS model's IDs and record any
        model id/slug the recipe targets so a recipe pointing at THIS model blocks
        its deletion. A recipe naming a DIFFERENT project's model is not returned
        by the project-scoped query at all (cross-project isolation)."""
        rows: list[CrossModelRecipeRow] = []
        my_model_id = _s(self._model.id) if self._model else ""
        my_slug = (self._model.slug or "") if self._model else ""
        for r in self._raw["recipes"]:
            object_ids: set[str] = set()
            model_ids: set[str] = set()
            self._walk_recipe_json(r.steps, object_ids, model_ids, my_model_id, my_slug)
            self._walk_recipe_json(r.combine, object_ids, model_ids, my_model_id, my_slug)
            rows.append(CrossModelRecipeRow(
                id=_s(r.id), name=r.name, display_name=r.name,
                referenced_object_ids=tuple(sorted(object_ids)),
                referenced_model_ids=tuple(sorted(model_ids)),
            ))
        return tuple(rows)

    def _walk_recipe_json(
        self, node: Any, object_ids: set[str], model_ids: set[str],
        my_model_id: str, my_slug: str,
    ) -> None:
        """Structurally walk a recipe steps/combine JSON tree, collecting model
        references (by id or slug) and object references (measure/dimension names
        resolved to THIS model's IDs)."""
        if isinstance(node, dict):
            for key, value in node.items():
                lk = key.lower()
                if lk in ("model_id", "source_model_id", "target_model_id") and isinstance(value, str):
                    if value == my_model_id:
                        model_ids.add(my_model_id)
                elif lk in ("model_slug", "source_model", "model") and isinstance(value, str):
                    if value == my_slug:
                        model_ids.add(my_model_id)
                elif lk in ("measure", "measure_name") and isinstance(value, str):
                    rid = self._resolve_measure_name(value)
                    if rid:
                        object_ids.add(rid)
                elif lk in ("dimension", "dimension_name") and isinstance(value, str):
                    rid = self._resolve_dimension_name(value)
                    if rid:
                        object_ids.add(rid)
                else:
                    self._walk_recipe_json(value, object_ids, model_ids, my_model_id, my_slug)
        elif isinstance(node, list):
            for item in node:
                self._walk_recipe_json(item, object_ids, model_ids, my_model_id, my_slug)

    def _build_agent_groundings(self) -> tuple[AgentGroundingRow, ...]:
        return tuple(
            AgentGroundingRow(id=_s(a.model_id), name="agent_grounding", display_name="Agent Grounding")
            for a in self._raw["agent_links"]
        )


def _tenant_id(db) -> str:
    """Tenant id from the session's info dict (``get_tenant_db`` stores it there).
    The engine keys nodes by (tenant, project, model); a session never crosses
    tenants by loader construction, so any stable non-empty value is safe."""
    info = getattr(db, "info", None)
    if isinstance(info, dict):
        tid = info.get("tenant_id")
        if tid:
            return str(tid)
    return "tenant"
