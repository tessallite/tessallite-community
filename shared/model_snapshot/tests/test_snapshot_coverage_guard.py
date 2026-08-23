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
    "named_queries": "named_queries",
    "named_query_artifacts": "nested under named_queries",
    "named_query_refresh_policies": "nested under named_queries",
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
    # v4 (Bug-7359, derived-grain §5.3) — declared key-to-detail relationships.
    # The DECLARATION is pinned model content and travels; verification EVIDENCE
    # and active-run pointers are live state (Phase 2) and are EXCLUDED below.
    "dimension_attribute_relationships": "attribute_relationships",
    # v5 (Bug-7852, pNN aggregate coverage). Per-measure quantile coverage records
    # proving pNN aggregate columns were built with the correct input fingerprint.
    # Travels so an imported/reverted model carries its coverage proof; the consumer
    # (quantile_routing proof gate) needs it to serve pNN from aggregates.
    "quantile_coverage": "nested under aggregates",
}

# NOTE: calendar_history_provenance is classified under EXCLUDED below.

# Tables intentionally NOT in the snapshot, with the reason. Runtime telemetry,
# history, version rows themselves, per-tenant identity, project-scoped config,
# and secret tokens.
EXCLUDED: dict[str, str] = {
    # Bug-8140: deliberately detached from models/projects/connections so the
    # cleanup identity and retry/audit evidence survive their deletion. The
    # FK-derived discovery above is structurally blind to this table; the
    # dedicated fail-closed guard below asserts its table/column/no-FK contract.
    "physical_cleanup_tasks": "durable detached target-cleanup outbox; runtime state, never semantic definition",
    # Bug-7982 R7: the durable post-deploy KPI re-evaluation outbox (added by
    # R6 / migration 0182, which never classified it here — this guard has been
    # RED since). It is transient operational state: a row exists only between a
    # deploy/revert commit and the re-evaluation that satisfies it. Carrying it
    # in a snapshot would make an import/revert re-fire a re-eval for an epoch
    # that no longer exists.
    "pending_kpi_reeval": "durable re-eval outbox; transient operational state",
    # Server-issued undo/redo capability for auto-created calendar history.
    # Runtime provenance token — not portable semantic model config.
    "calendar_history_provenance": "runtime calendar undo/redo provenance token; not portable model config",
    "aggregate_refresh_runs": "runtime telemetry",
    "pocket_refresh_runs": "runtime telemetry",
    "named_query_refresh_runs": "runtime telemetry",
    # v4 (Bug-7359, derived-grain §5.3 / §7.6): live verification evidence tied
    # to a deployed version + physical refresh run. NOT model content — a
    # rehydrated model must be re-verified against its reverted/imported version,
    # so this deliberately never travels in a snapshot. The pinned relationship
    # DECLARATION does travel (COVERED above); the verification EVIDENCE does not.
    "dimension_attribute_verifications": "live verification evidence, re-established after deploy/rehydrate; not model config",
    # Bug-8615 phase G1: the same rule as the row above. The DECLARATION
    # (joins.population_participation) is pinned model content and travels
    # inside the "joins" key; the deploy-time row-loss/row-multiplication
    # MEASUREMENT describes the source data's shape under one deployed version +
    # deploy epoch and must be re-taken after a revert/import, never restored.
    "join_population_checks": "live deploy-time join row-loss/multiplication evidence, re-measured on the next deploy; not model config",
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


def test_bug_8140_detached_cleanup_outbox_is_explicitly_guarded():
    """The FK-derived coverage tool cannot discover an intentionally detached
    table, so pin the outbox contract directly instead of claiming the generic
    graph walk proves it."""
    table = TenantBase.metadata.tables["physical_cleanup_tasks"]
    assert not table.foreign_keys, (
        "cleanup retry identity must not be cascaded away with model/project/"
        "connection metadata"
    )
    required = {
        "artifact_kind", "artifact_id", "model_id", "project_id",
        "connection_id", "connection_type", "encrypted_credentials",
        "connection_config", "target_schema", "qualified_table_name",
        "status", "attempts", "next_attempt_at", "error_message",
    }
    column_names = set(table.columns.keys())
    assert required <= column_names, (
        "detached cleanup outbox lost complete target/retry identity: "
        f"{sorted(required - column_names)}"
    )
    assert "physical_cleanup_tasks" in EXCLUDED
