"""Compute a structured diff between two model snapshots.

Each category produces {"added": [...], "removed": [...], "changed": [...]}.
Items are identified by their stable "id" field.

Singleton/dict snapshot keys (model, model_alias_map, refresh_sla_config,
ai_scheduler_config, model_settings) are compared field-by-field and returned
under ``SINGLETON_CATEGORIES`` in the diff output (Bug-5916).
"""
from __future__ import annotations

from typing import Any

# Bug-5868: every list-type category from the snapshot serialiser must be
# present here so the diff endpoint returns additions/removals/changes for
# them. The previous 8-entry list silently ignored ~20 categories.
DIFF_CATEGORIES = [
    # Core model structure
    "tables",
    "columns",
    "joins",
    # Semantic layer
    "dimensions",
    "measures",
    "hierarchies",
    "named_sets",
    "kpis",
    "drill_through_sets",
    # Data connectivity
    "data_sources",
    "data_targets",
    "calendar_tables",
    "lineage_mappings",
    # Acceleration
    "aggregates",
    "pockets",
    "aggregate_lifecycle_events",
    # Source statistics
    "source_statistics",
    "source_join_statistics",
    # Governance / security
    "personas",
    "data_tags",
    "persona_tag_restrictions",
    "row_security_rules",
    # Glossary
    "glossary_entries",
    # User-defined attributes
    "user_defined_attributes",
    "uda_column_refs",
    # v3 additions
    "model_parameters",
    "data_quality_rules",
    "entity_translations",
    # Model versions (present when include_versions=True)
    "model_versions",
]

_ID_FIELD = "id"

_LABEL_FIELDS: dict[str, str] = {
    "tables": "alias",
    "dimensions": "slug",
    "measures": "slug",
    "joins": "id",
    "hierarchies": "name",
    "aggregates": "grain_key",
    "pockets": "id",
    "personas": "slug",
    "columns": "column_name",
    "named_sets": "slug",
    "kpis": "slug",
    "drill_through_sets": "id",
    "data_sources": "id",
    "data_targets": "id",
    "calendar_tables": "id",
    "lineage_mappings": "id",
    "aggregate_lifecycle_events": "id",
    "source_statistics": "id",
    "source_join_statistics": "id",
    "data_tags": "slug",
    "persona_tag_restrictions": "id",
    "row_security_rules": "id",
    "glossary_entries": "term",
    "user_defined_attributes": "slug",
    "uda_column_refs": "id",
    "model_parameters": "slug",
    "data_quality_rules": "id",
    "entity_translations": "id",
    "model_versions": "id",
}


def _item_label(category: str, item: dict[str, Any]) -> str:
    field = _LABEL_FIELDS.get(category, "id")
    return str(item.get(field, item.get("id", "?")))


def _diff_category(
    old_items: list[dict], new_items: list[dict], category: str
) -> dict[str, list]:
    old_map = {item[_ID_FIELD]: item for item in old_items if _ID_FIELD in item}
    new_map = {item[_ID_FIELD]: item for item in new_items if _ID_FIELD in item}

    label_field = _LABEL_FIELDS.get(category, "id")

    added = [
        {label_field: _item_label(category, v), **v}
        for k, v in new_map.items() if k not in old_map
    ]
    removed = [
        {label_field: _item_label(category, v), **v}
        for k, v in old_map.items() if k not in new_map
    ]
    changed = []
    for k in old_map:
        if k not in new_map:
            continue
        old, new = old_map[k], new_map[k]
        field_changes: dict[str, Any] = {}
        for fk in set(old.keys()) | set(new.keys()):
            if fk == _ID_FIELD:
                continue
            ov, nv = old.get(fk), new.get(fk)
            if ov != nv:
                field_changes[fk] = {"from": ov, "to": nv}
        if field_changes:
            changed.append({
                "id": k,
                label_field: _item_label(category, new),
                "changes": field_changes,
            })

    return {"added": added, "removed": removed, "changed": changed}


# Bug-5916: singleton/dict snapshot keys that are not list-of-rows.
# These are compared field-by-field and any changes are surfaced so the
# version diff viewer shows model settings, alias maps, SLA config, etc.
SINGLETON_CATEGORIES = [
    "model",
    "model_alias_map",
    "refresh_sla_config",
    "ai_scheduler_config",
    "model_settings",
]

# Fields excluded from singleton diff because they are metadata noise,
# not meaningful model content changes.
_SINGLETON_EXCLUDE = {
    "schema_version",
    "exported_at",
}

#: Excluded from the ``model`` category ONLY (R7 review round 5, O1). These are
#: monotonic counters / draft control metadata that no longer travel in the
#: snapshot, so diffing a pre-exclusion version against a post-exclusion one
#: would otherwise show a phantom "model.data_epoch: 7 -> None" that is not a
#: model change. Scoped to ``model`` because ``model_settings`` is an arbitrary
#: user-keyed dict — a setting literally named ``data_epoch`` must still diff.
_MODEL_ONLY_EXCLUDE = {"deploy_epoch", "data_epoch", "dependency_revision"}


def _diff_singleton(
    old_val: dict | None,
    new_val: dict | None,
    extra_exclude: set[str] | None = None,
) -> dict[str, Any]:
    """Produce a field-level diff for a singleton/dict snapshot key.

    Returns ``{"changes": {field: {"from": ..., "to": ...}}}`` when
    differences exist, or an empty dict when both sides are identical.
    """
    old_val = old_val or {}
    new_val = new_val or {}
    changes: dict[str, Any] = {}
    for fk in set(old_val.keys()) | set(new_val.keys()):
        if fk in _SINGLETON_EXCLUDE or (extra_exclude and fk in extra_exclude):
            continue
        ov, nv = old_val.get(fk), new_val.get(fk)
        if ov != nv:
            changes[fk] = {"from": ov, "to": nv}
    if not changes:
        return {}
    return {"changes": changes}


def diff_snapshots(
    old_snapshot: dict[str, Any],
    new_snapshot: dict[str, Any],
) -> dict[str, dict[str, list]]:
    """Return per-category diff between two model snapshots."""
    result: dict[str, dict[str, Any]] = {}
    for cat in DIFF_CATEGORIES:
        old_items = old_snapshot.get(cat, []) or []
        new_items = new_snapshot.get(cat, []) or []
        result[cat] = _diff_category(old_items, new_items, cat)

    # Bug-5916: diff singleton/dict snapshot keys so version comparison
    # surfaces model scalar, alias map, SLA, scheduler, and settings changes.
    for cat in SINGLETON_CATEGORIES:
        old_val = old_snapshot.get(cat)
        new_val = new_snapshot.get(cat)
        diff = _diff_singleton(
            old_val, new_val,
            extra_exclude=_MODEL_ONLY_EXCLUDE if cat == "model" else None,
        )
        if diff:
            result[cat] = diff
    return result
