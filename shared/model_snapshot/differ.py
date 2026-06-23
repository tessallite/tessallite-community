"""Compute a structured diff between two model snapshots.

Each category produces {"added": [...], "removed": [...], "changed": [...]}.
Items are identified by their stable "id" field.
"""
from __future__ import annotations

from typing import Any

DIFF_CATEGORIES = [
    "tables", "dimensions", "measures", "joins",
    "hierarchies", "aggregates", "pockets", "personas",
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


def diff_snapshots(
    old_snapshot: dict[str, Any],
    new_snapshot: dict[str, Any],
) -> dict[str, dict[str, list]]:
    """Return per-category diff between two model snapshots."""
    result: dict[str, dict[str, list]] = {}
    for cat in DIFF_CATEGORIES:
        old_items = old_snapshot.get(cat, []) or []
        new_items = new_snapshot.get(cat, []) or []
        result[cat] = _diff_category(old_items, new_items, cat)
    return result
