"""Snapshot coverage guard (F-013-06).

Keeps the snapshot contract honest: every model-scoped ORM table must be
*deliberately* classified as either COVERED (it travels in the snapshot, with
the snapshot key that carries it) or EXCLUDED (with a reason). A newly-added
model-scoped table that is in neither list fails this test, forcing the author
to decide — instead of silently dropping configuration out of export / import /
revert the way ModelParameter, ModelAliasMap, RefreshSLAConfig, DataQualityRule
and EntityTranslation did before this batch.

The model-scoped set is derived live from the ORM metadata graph (any tenant
table whose FK chain roots at ``models``), so the guard tracks the schema, not
a hand-maintained snapshot.
"""
from __future__ import annotations

from shared.db.models import TenantBase


def _model_scoped_tables() -> set[str]:
    md = TenantBase.metadata
    fk_parents: dict[str, set[tuple[str, str]]] = {}
    for t in md.tables.values():
        fk_parents[t.name] = {
            (fk.parent.name, fk.column.table.name) for fk in t.foreign_keys
        }

    def roots_at_model(tname: str, seen: set[str] | None = None) -> bool:
        if seen is None:
            seen = set()
        if tname in seen:
            return False
        seen.add(tname)
        if tname == "models":
            return True
        for _col, ref in fk_parents.get(tname, ()):
            if ref == "models" or roots_at_model(ref, seen):
                return True
        return False

    return {t for t in md.tables if t != "models" and roots_at_model(t)}


# Tables that travel in the snapshot. Value = the top-level snapshot key (or a
# note for rows nested under a parent key). The serialiser must produce each
# listed key; the rehydrator must consume it.
COVERED: dict[str, str] = {
    "data_sources": "data_sources",
    "data_targets": "data_targets",
    "calendar_tables": "calendar_tables",
    "model_tables": "tables",
    "model_columns": "columns",
    "user_defined_attributes": "user_defined_attributes",
    "user_defined_attribute_column_refs": "uda_column_refs",
    "joins": "joins",
    "hierarchy_definitions": "hierarchies",
    "hierarchy_levels": "nested under hierarchies",
    "hierarchy_level_attributes": "nested under hierarchies.levels",
    "dimensions": "dimensions",
    "measures": "measures",
    "named_sets": "named_sets",
    "kpis": "kpis",
    "drill_through_sets": "drill_through_sets",
    "lineage_mappings": "lineage_mappings",
    "aggregate_definitions": "aggregates",
    "aggregate_columns": "nested under aggregates",
    "aggregate_refresh_policies": "nested under aggregates",
    "aggregate_lifecycle_events": "aggregate_lifecycle_events",
    "personas": "personas",
    "data_tags": "data_tags",
    "data_tag_columns": "nested column_ids under data_tags",
    "persona_tag_restrictions": "persona_tag_restrictions",
    "pocket_definitions": "pockets",
    "pocket_predicates": "nested under pockets",
    "pocket_refresh_policies": "nested under pockets",
    "row_security_rules": "row_security_rules",
    "glossary_entry": "glossary_entries",
    "glossary_synonym": "nested under glossary_entries",
    "glossary_attachment": "nested under glossary_entries",
    "source_statistics": "source_statistics",
    "source_column_statistics": "nested under source_statistics",
    "source_join_statistics": "source_join_statistics",
    "model_ai_scheduler_config": "ai_scheduler_config",
    "model_settings": "model_settings",
    # v3 (F-013-06)
    "model_parameters": "model_parameters",
    "model_alias_maps": "model_alias_map",
    "refresh_sla_configs": "refresh_sla_config",
    "data_quality_rules": "data_quality_rules",
    "entity_translations": "entity_translations",
}

# Tables intentionally NOT in the snapshot, with the reason. Runtime telemetry,
# history, version rows themselves, per-tenant identity, project-scoped config,
# and secret tokens.
EXCLUDED: dict[str, str] = {
    "aggregate_refresh_runs": "runtime telemetry",
    "pocket_refresh_runs": "runtime telemetry",
    "data_quality_violations": "runtime telemetry (child of data_quality_rules)",
    "ai_optimizer_runs": "history / telemetry",
    "ai_aggregate_recommendations": "history / telemetry",
    "model_telemetry_snapshots": "telemetry",
    "schema_change_events": "upstream-schema diff history, not model config",
    "model_alerts": "operator inbox, not portable",
    "hierarchy_health_issues": "runtime validation findings, not config",
    "model_versions": "the version rows themselves (project-level export only)",
    "kpi_latest": "runtime KPI evaluation cache",
    "kpi_snapshots": "runtime KPI evaluation history",
    "kpi_usage": "telemetry",
    "kpi_versions": "version history of KPIs, not live config",
    "named_set_usage": "telemetry",
    "named_set_versions": "version history of named sets",
    "saved_queries": "per-user artifacts, not model config",
    "saved_pivot_views": "per-user artifacts, not model config",
    "user_entity_preferences": "per-user pinned/favourite entity state, not model config",
    "scratchpad_measures": "per-user ephemeral expressions",
    "user_access_bindings": "tenant-local identity mapping; would leak users",
    "glossary_share_token": "per-tenant secret tokens; do not export",
    "glossary_bootstrap_jobs": "runtime job state",
    "downstream_assets": "impact-analysis catalog, populated at runtime",
    "downstream_asset_columns": "impact-analysis links, runtime",
    "gateway_query_references": "runtime query reference log",
    "refresh_dependencies": "runtime refresh-ordering state",
    # Project-scoped agent config that happens to chain to models via the
    # context/scope join tables — agent config is project scope, not model
    # snapshot scope.
    "project_agent_configs": "project-scoped agent config",
    "project_agent_models": "project-scoped agent allow-list",
    "project_agent_model_contexts": "project-scoped agent derived context",
    "project_persona_model_scopes": "project-scoped persona scope",
    # External governance integrations (Collibra / Solidatus). Connection rows
    # carry credentials and tenant-local endpoint config; object mappings + sync
    # runs are runtime integration state that chains to models via the mapping
    # tables. None of this is portable model semantic config, and the
    # connections must never travel in an export — so all six are excluded.
    "collibra_connections": "external-integration connection config + secrets; not portable",
    "collibra_object_mappings": "runtime governance-sync mapping, not model config",
    "collibra_sync_runs": "runtime integration sync telemetry",
    "solidatus_connections": "external-integration connection config + secrets; not portable",
    "solidatus_object_mappings": "runtime governance-sync mapping, not model config",
    "solidatus_sync_runs": "runtime integration sync telemetry",
    # Conversational-agent runtime data. These are per-tenant chat/cost/webhook
    # records keyed on project_id; they only chain to models via an OPTIONAL
    # pinned_model_id (conversations) / its child FKs (turns, cost ledger,
    # webhook DLQ). They are not model-definition config — the model-snapshot
    # serialiser (which "excludes runtime logs and anything at project scope")
    # never emits them, and only project_rehydrator handles them at project
    # scope. So all four are excluded from the model snapshot.
    "agent_conversations": "runtime agent conversation log (project-scoped, optional model pin)",
    "agent_turns": "runtime agent turn log (child of agent_conversations)",
    "agent_cost_ledger": "runtime per-turn LLM cost telemetry",
    "agent_webhook_dlq": "runtime webhook dead-letter queue (operator-managed)",
}


def test_every_model_scoped_table_is_classified():
    """No model-scoped table may be silently un-snapshotted."""
    tables = _model_scoped_tables()
    classified = set(COVERED) | set(EXCLUDED)
    unclassified = tables - classified
    assert not unclassified, (
        "Model-scoped tables added without a snapshot decision "
        "(add to COVERED or EXCLUDED in test_snapshot_coverage_guard.py): "
        f"{sorted(unclassified)}"
    )


def test_covered_and_excluded_are_disjoint():
    overlap = set(COVERED) & set(EXCLUDED)
    assert not overlap, f"tables in both COVERED and EXCLUDED: {sorted(overlap)}"


def test_covered_tables_are_actually_model_scoped():
    """Guard against a stale COVERED entry for a dropped table (e.g. the
    long-dead hierarchy_measure_links the old matrix still listed)."""
    tables = _model_scoped_tables()
    stale = set(COVERED) - tables
    assert not stale, (
        "COVERED lists tables that are no longer model-scoped / no longer "
        f"exist: {sorted(stale)}"
    )
