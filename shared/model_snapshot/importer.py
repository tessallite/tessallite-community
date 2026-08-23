"""Cross-tenant snapshot rewriter (Phase 6).

Takes an export bundle produced by ``snapshot_model`` (or wrapped via
the export endpoint) and produces a *new* snapshot with:

  - every primary key replaced by a fresh UUID
  - every foreign-key reference rewritten to track the new PKs
  - ``project_connection_id`` on data_sources / data_targets remapped
    via the caller-supplied mapping (per F-11: connections never travel
    cross-tenant; the importer must rebind to local connections)

The rewritten snapshot is then handed to ``rehydrate_into_live`` against
a freshly-created Model row. No existing model state is touched.
"""
from __future__ import annotations

import copy
import uuid
from typing import Any
from uuid import UUID

from shared.semantic.join_keyword import split_join_token
from shared.semantic.join_orientation_backfill import (
    backfill_orientation,
    resolves_no_fan_out,
)


_UUID_LEN = 36


def _is_uuid_string(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != _UUID_LEN or value.count("-") != 4:
        return False
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def _fresh() -> str:
    return str(uuid.uuid4())


def _collect_pks(
    snapshot: dict[str, Any], mapping: dict[str, str] | None = None
) -> dict[str, str]:
    """Walk the snapshot and produce {old_uuid: new_uuid} for every PK we know about.

    When ``mapping`` is supplied it is EXTENDED in place (ids already present keep
    their assigned new id) and returned. This lets a model's live shape and each
    of its version snapshots share ONE map so a given source id re-keys to the
    SAME new id everywhere — id continuity a revert relies on to re-attach
    preserved governance (CLS/RLS/KPI) and to match preserved aggregates by id
    (Bug-7623 R2). Ids that appear only in a historical version extend the shared
    map with a fresh id, still self-consistent within that version.
    """
    if mapping is None:
        mapping = {}

    # Tables, columns, UDAs, joins, dimensions, measures, sources, targets:
    # each row's "id" is a primary key in its respective table. v2 families
    # whose top-level rows carry a flat "id" are listed here too.
    for key in (
        "tables", "columns", "user_defined_attributes", "joins",
        "dimensions", "measures", "data_sources", "data_targets",
        "lineage_mappings",
        # Bug-1022: kpis and named_sets were never PK-remapped, so any
        # snapshot import into the same tenant 500'd on kpis_pkey /
        # named_sets_pkey collisions.
        "kpis", "named_sets",
        # v2 — flat rows
        "drill_through_sets", "calendar_tables",
        "personas", "row_security_rules",
        "aggregate_lifecycle_events",
        "source_join_statistics",
        # F-008-09 — data tags (column_ids / persona_tag_restrictions
        # references are rewritten by the generic UUID pass via the
        # column / persona / tag pk entries).
        "data_tags",
        # v3 (F-013-06) — model-scoped config families with flat "id" PKs.
        # data_quality_rules.target_id and entity_translations.entity_id are
        # soft references to measures/dimensions/etc; the generic UUID pass
        # rewrites them once those families' PKs are in the map. model_alias_map
        # is a single dict keyed by model_id (no own PK) and is rewritten by the
        # model-PK entry, so it is not listed here.
        "model_parameters",
        "data_quality_rules",
        "entity_translations",
        # v4 (Bug-7359, derived-grain §5.3) — dimension attribute relationships.
        # Flat "id" PK; dimension_id / key_column_id / detail_column_id / model_id
        # are soft references rewritten by the generic UUID pass once the
        # dimensions / columns / model PKs are in the map.
        "attribute_relationships",
    ):
        for row in snapshot.get(key, []) or []:
            old = row.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()

    # Hierarchies have nested levels (and level attributes); each carries its own PK.
    for h in snapshot.get("hierarchies", []) or []:
        old = h.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()
        for lvl in h.get("levels", []) or []:
            old = lvl.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()
            for attr in lvl.get("attributes", []) or []:
                old = attr.get("id")
                if _is_uuid_string(old) and old not in mapping:
                    mapping[old] = _fresh()

    # Aggregates have nested columns and refresh_policy.
    for a in snapshot.get("aggregates", []) or []:
        old = a.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()
        for c in a.get("columns", []) or []:
            old = c.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()
        rp = a.get("refresh_policy")
        if isinstance(rp, dict):
            old = rp.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()

    # AI scheduler config row PK (if any)
    sched = snapshot.get("ai_scheduler_config")
    if isinstance(sched, dict):
        old = sched.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()

    # v3 (F-013-06) — refresh_sla_config is a single dict with its own "id" PK.
    # Without a fresh PK, a same-tenant clone would collide on
    # refresh_sla_configs_pkey. (model_alias_map keys on model_id only and is
    # remapped via the model PK; data_quality_rules / model_parameters /
    # entity_translations are handled in the list loop above.)
    sla = snapshot.get("refresh_sla_config")
    if isinstance(sla, dict):
        old = sla.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()

    # v2 — Pockets carry nested predicates + refresh_policy.
    for p in snapshot.get("pockets", []) or []:
        old = p.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()
        for pr in p.get("predicates", []) or []:
            old = pr.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()
        rp = p.get("refresh_policy")
        if isinstance(rp, dict):
            old = rp.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()

    # Named Queries carry nested artifact and refresh-policy rows. Keep these
    # ids in the same map as every other snapshot family so project/model
    # imports rewrite every real source identity before rehydration. The
    # rehydrator still mints definition/artifact ids defensively (it is not
    # called only through this importer), but policy ids must be remapped here
    # because they are nested model content (Bug-9222).
    for nq in snapshot.get("named_queries", []) or []:
        old = nq.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()
        artifact = nq.get("artifact")
        if isinstance(artifact, dict):
            old = artifact.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()
        policy = nq.get("refresh_policy")
        if isinstance(policy, dict):
            old = policy.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()

    # v2 — Glossary entries carry nested synonyms + attachments.
    for g in snapshot.get("glossary_entries", []) or []:
        old = g.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()
        for s in g.get("synonyms", []) or []:
            old = s.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()
        for a in g.get("attachments", []) or []:
            old = a.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()

    # v2 — Source statistics carry nested column statistics.
    for st in snapshot.get("source_statistics", []) or []:
        old = st.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()
        for c in st.get("columns", []) or []:
            old = c.get("id")
            if _is_uuid_string(old) and old not in mapping:
                mapping[old] = _fresh()

    # uda_column_refs PK (if present in the row)
    for r in snapshot.get("uda_column_refs", []) or []:
        old = r.get("id")
        if _is_uuid_string(old) and old not in mapping:
            mapping[old] = _fresh()

    # Model PK (top-level "model" dict carries the old model id)
    m = snapshot.get("model") or {}
    old = m.get("id")
    if _is_uuid_string(old) and old not in mapping:
        mapping[old] = _fresh()

    return mapping


def _rewrite_node(node: Any, pk_map: dict[str, str]) -> Any:
    """Recursively rewrite any UUID-shaped string that appears as a key value.

    Only string values are rewritten. Keys are left alone (we never key by UUID).
    """
    if isinstance(node, dict):
        return {k: _rewrite_node(v, pk_map) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_node(v, pk_map) for v in node]
    if isinstance(node, str) and _is_uuid_string(node) and node in pk_map:
        return pk_map[node]
    return node


def _remap_connections(
    snapshot: dict[str, Any], connection_mapping: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """Replace ``project_connection_id`` on every source/target.

    Returns (snapshot, missing) where ``missing`` lists the source/target
    display names whose connection id had no mapping entry. The caller
    decides whether to fail the import or accept partial rebinding.
    """
    missing: list[str] = []
    for s in snapshot.get("data_sources", []) or []:
        old = s.get("project_connection_id")
        if isinstance(old, str) and old in connection_mapping:
            s["project_connection_id"] = connection_mapping[old]
        else:
            missing.append(f"source:{s.get('display_name') or s.get('id')}")
    for t in snapshot.get("data_targets", []) or []:
        old = t.get("project_connection_id")
        if isinstance(old, str) and old in connection_mapping:
            t["project_connection_id"] = connection_mapping[old]
        else:
            missing.append(f"target:{t.get('display_name') or t.get('id')}")
    return snapshot, missing


def _strip_cross_tenant_only_refs(snapshot: dict[str, Any]) -> None:
    """Null out fields that reference rows living outside the per-model
    snapshot scope (tenant or system) so a cross-tenant import doesn't
    import a stale FK to a row that doesn't exist in the target tenant.

    Currently:
      - ``model.llm_config_id`` references ``llm_provider_configs`` which
        is a tenant-level table. The target tenant may have a different
        active config or none at all; the FK is ON DELETE SET NULL, but
        an INSERT with a non-existent llm_config_id would still error.
        Strip it; the user re-selects an LLM config post-import.
      - ``glossary_entries[].created_by`` carries the source-tenant user
        UUID, which doesn't exist in the target tenant. No FK constraint
        on this column, but leaving the value in place silently links
        glossary entries to phantom users. Strip it.
    """
    m = snapshot.get("model")
    if isinstance(m, dict) and m.get("llm_config_id"):
        m["llm_config_id"] = None
    for g in snapshot.get("glossary_entries", []) or []:
        if g.get("created_by"):
            g["created_by"] = None


def _normalise_imported_join_orientations(snapshot: dict[str, Any]) -> None:
    """Apply migration 0194's join policy to a snapshot entering by import.

    Migration 0194 is a one-time repair.  A pre-0194 bundle restored after the
    migration completed would otherwise recreate a legacy cardinality token in
    ``join_type`` and remain undeclared forever.  The import rewriter is the
    shared boundary for both a bundle's live shape and each historical version
    snapshot, while an ordinary revert bypasses it and remains verbatim.

    The existing backfill policy is authoritative: infer the explicit physical
    orientation, and move a legacy fan-out token into ``cardinality`` when the
    bundle does not already carry a usable cardinality declaration.
    """
    joins = snapshot.get("joins")
    if not isinstance(joins, list):
        return
    for join in joins:
        if not isinstance(join, dict):
            continue
        raw_join_type = join.get("join_type")
        orientation = backfill_orientation(raw_join_type)
        if orientation is None:
            continue
        join["join_type"] = orientation
        _orientation, legacy_cardinality = split_join_token(raw_join_type)
        if (
            resolves_no_fan_out(join.get("cardinality"))
            and legacy_cardinality is not None
        ):
            join["cardinality"] = legacy_cardinality


def prepare_snapshot_for_import(
    snapshot: dict[str, Any],
    *,
    new_model_id: UUID,
    connection_mapping: dict[str, str] | None = None,
    shared_pk_map: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Rewrite a snapshot for cross-tenant import.

    The returned snapshot is safe to feed into ``rehydrate_into_live``
    against a freshly-created Model row whose id is ``new_model_id``.

    ``shared_pk_map``: when supplied, the PK re-key uses/EXTENDS this map instead
    of building a fresh one, so a model's live shape and each of its version
    snapshots re-key identically (Bug-7623 R2 — id continuity a revert needs to
    re-attach preserved governance and match preserved aggregates by id). The
    caller passes the SAME dict for the live shape and every version of that
    model. The model PK is forced to ``new_model_id`` in every case.

    Returns (snapshot, missing_connections).
    """
    snapshot = copy.deepcopy(snapshot)

    # Step 1: collect all old PKs and assign new ones (extending the shared map
    # when one is provided, so overlapping ids re-key consistently).
    pk_map = _collect_pks(snapshot, shared_pk_map)
    # Force the model PK to the caller-chosen new id (so the freshly
    # inserted Model row matches what we rehydrate into).
    old_model_id = (snapshot.get("model") or {}).get("id")
    if isinstance(old_model_id, str):
        pk_map[old_model_id] = str(new_model_id)

    # Step 2: rewrite every UUID-shaped value anywhere in the tree.
    snapshot = _rewrite_node(snapshot, pk_map)

    # Step 3: strip references to rows outside the per-model snapshot
    # scope (these can't survive a tenant boundary).
    _strip_cross_tenant_only_refs(snapshot)

    # Step 4: repair pre-0194 join declarations. This function is used only at
    # import boundaries, so normal version reverts continue to restore verbatim.
    _normalise_imported_join_orientations(snapshot)

    # Step 5: rebind project_connection_id values via the caller mapping.
    snapshot, missing = _remap_connections(snapshot, connection_mapping or {})
    return snapshot, missing
