"""YAML model serialiser — human-readable export for Git version control.

Converts an internal model snapshot dict (produced by serialiser.snapshot_model)
into a clean YAML string designed to be readable by a non-technical person.

Design rules:
  - No UUIDs in output — names are the primary keys.
  - No internal metadata (created_at, is_invalid, etc.).
  - Plain English field names.
  - Sorted lists for stable diffs.
"""
from __future__ import annotations

from typing import Any

import yaml


_JOIN_TYPE_MAP = {
    "many_to_one": "many-to-one",
    "one_to_many": "one-to-many",
    "one_to_one": "one-to-one",
    "many_to_many": "many-to-many",
}

_DIM_TYPE_MAP = {
    "string": "text",
    "varchar": "text",
    "text": "text",
    "integer": "number",
    "int": "number",
    "bigint": "number",
    "float": "number",
    "double": "number",
    "numeric": "number",
    "decimal": "number",
    "date": "date",
    "timestamp": "date",
    "timestamptz": "date",
    "datetime": "date",
    "boolean": "boolean",
    "bool": "boolean",
}


def snapshot_to_yaml(
    snapshot: dict[str, Any],
    *,
    project_name: str | None = None,
    connection_name: str | None = None,
) -> str:
    model_meta = snapshot.get("model", {})

    id_to_table: dict[str, str] = {}
    id_to_column: dict[str, tuple[str, str]] = {}

    for t in snapshot.get("tables", []):
        tid = t.get("id", "")
        # Real snapshot_model() output keys the table name on `alias`
        # (the per-model handle), falling back to physical_name/display_name.
        tname = (
            t.get("alias")
            or t.get("display_name")
            or t.get("physical_name")
            or ""
        )
        id_to_table[tid] = tname

    id_to_col_type: dict[str, str] = {}
    id_to_col_primary_key: dict[str, bool] = {}

    for c in snapshot.get("columns", []):
        cid = c.get("id", "")
        table_id = c.get("model_table_id", "")
        tname = id_to_table.get(table_id, "")
        cname = c.get("column_name") or ""
        id_to_column[cid] = (tname, cname)
        id_to_col_type[cid] = c.get("data_type", "")
        id_to_col_primary_key[cid] = bool(c.get("is_primary_key"))

    def _resolve_column(col_id: str | None) -> tuple[str, str] | None:
        if not col_id:
            return None
        return id_to_column.get(col_id)

    def _resolve_column_type(col_id: str | None) -> str:
        if not col_id:
            return ""
        return id_to_col_type.get(col_id, "")

    def _resolve_column_primary_key(col_id: str | None) -> bool:
        if not col_id:
            return False
        return id_to_col_primary_key.get(col_id, False)

    doc: dict[str, Any] = {}

    id_to_measure_name: dict[str, str] = {
        m.get("id", ""): m.get("name", "")
        for m in snapshot.get("measures", [])
    }

    doc["model"] = _build_model_section(model_meta, connection_name)
    doc["tables"] = _build_tables(snapshot)
    doc["joins"] = _build_joins(snapshot, id_to_table, id_to_column)
    doc["measures"] = _build_measures(snapshot, _resolve_column, id_to_measure_name)
    doc["dimensions"] = _build_dimensions(
        snapshot,
        _resolve_column,
        _resolve_column_type,
        _resolve_column_primary_key,
    )
    doc["hierarchies"] = _build_hierarchies(snapshot, id_to_column)
    id_to_dim_name: dict[str, str] = {
        d.get("id", ""): d.get("name", "")
        for d in snapshot.get("dimensions", [])
    }
    doc["personas"] = _build_personas(snapshot, id_to_dim_name, id_to_measure_name)

    for key in list(doc):
        if not doc[key]:
            del doc[key]

    return yaml.dump(
        doc,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )


def project_to_yaml(
    project: dict[str, Any],
    connections: list[dict[str, Any]] | None = None,
) -> str:
    doc: dict[str, Any] = {
        "project": {
            "name": project.get("slug") or project.get("name", ""),
            "display_name": project.get("display_name", ""),
        },
    }

    if connections:
        doc["connections"] = [
            {
                "name": c.get("display_name") or c.get("name", ""),
                "type": c.get("connection_type", ""),
            }
            for c in sorted(connections, key=lambda x: x.get("display_name", ""))
        ]

    return yaml.dump(
        doc,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )


def _build_model_section(
    model: dict[str, Any],
    connection_name: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": model.get("slug") or model.get("name", ""),
        "display_name": model.get("display_name", ""),
    }
    if model.get("description"):
        out["description"] = model["description"]
    if connection_name:
        out["connection"] = connection_name
    refresh = model.get("refresh_strategy")
    if refresh:
        out["refresh"] = refresh
    max_agg = model.get("max_aggregates")
    if max_agg:
        out["max_aggregates"] = max_agg
    return out


def _build_tables(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    tables = []
    for t in sorted(
        snapshot.get("tables", []),
        key=lambda x: (x.get("alias") or x.get("display_name") or ""),
    ):
        # Real ModelTable columns: alias (handle), physical_name (the
        # schema-qualified source reference), display_name, table_type.
        name = t.get("alias") or t.get("display_name") or t.get("physical_name", "")
        entry: dict[str, Any] = {"name": name}
        source = t.get("physical_name") or ""
        if source and source != name:
            entry["source_table"] = source
        if t.get("description"):
            entry["description"] = t["description"]
        role = t.get("table_type")
        if role:
            entry["role"] = role
        tables.append(entry)
    return tables


def _build_joins(
    snapshot: dict[str, Any],
    id_to_table: dict[str, str],
    id_to_column: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    joins = []
    for j in snapshot.get("joins", []):
        left_id = j.get("left_table_id", "")
        right_id = j.get("right_table_id", "")
        left_name = id_to_table.get(left_id, "")
        right_name = id_to_table.get(right_id, "")
        # Round-trip the join cardinality faithfully. Canonical four-way
        # cardinalities export as their human-friendly hyphenated form
        # (reversible 1:1 on import). Any other ORM value (e.g. a directional
        # `right`/`left`) is emitted verbatim so the deserialiser can restore
        # it exactly — never silently collapsed to a default (Bug-1097).
        raw_join_type = j.get("join_type", "many_to_one")
        join_type = _JOIN_TYPE_MAP.get(raw_join_type, raw_join_type)

        # Real Join ORM stores left_column_id / right_column_id; resolve to
        # table-qualified column names so the condition round-trips.
        left_col = id_to_column.get(j.get("left_column_id", ""), ("", ""))[1]
        right_col = id_to_column.get(j.get("right_column_id", ""), ("", ""))[1]
        condition = ""
        if left_col and right_col:
            condition = f"{left_name}.{left_col} = {right_name}.{right_col}"

        entry: dict[str, Any] = {
            "left": left_name,
            "right": right_name,
            "on": condition,
            "type": join_type,
        }
        joins.append(entry)
    return sorted(joins, key=lambda x: (x.get("left", ""), x.get("right", "")))


def _build_measures(
    snapshot: dict[str, Any],
    resolve_column,
    id_to_measure_name: dict[str, str],
) -> list[dict[str, Any]]:
    measures = []
    for m in sorted(snapshot.get("measures", []), key=lambda x: x.get("name", "")):
        if m.get("is_invalid"):
            continue
        entry: dict[str, Any] = {"name": m.get("name", "")}
        if m.get("display_name"):
            entry["display_name"] = m["display_name"]
        if m.get("description"):
            entry["description"] = m["description"]

        col = resolve_column(m.get("source_column_id"))
        if col:
            entry["table"] = col[0]
            entry["column"] = col[1]

        if m.get("expression"):
            entry["expression"] = m["expression"]

        agg = m.get("default_agg", "sum")
        if not m.get("expression"):
            entry["aggregation"] = agg

        if m.get("format"):
            entry["format"] = m["format"]
        if m.get("display_folder"):
            entry["folder"] = m["display_folder"]

        if m.get("semi_additive_behavior"):
            entry["semi_additive"] = m["semi_additive_behavior"]

        if m.get("variant_kind"):
            entry["variant"] = m["variant_kind"]
            # Carry the base-measure linkage by NAME so the variant
            # round-trips (Bug-1099). The deserialiser resolves this name to
            # the base measure's new id in a second pass (the same name-based
            # resolution already used for personas and table.column refs).
            base_name = id_to_measure_name.get(m.get("variant_of_measure_id", ""))
            if base_name:
                entry["variant_of"] = base_name
            if m.get("variant_n") is not None:
                entry["variant_n"] = m["variant_n"]

        measures.append(entry)
    return measures


def _build_dimensions(
    snapshot: dict[str, Any],
    resolve_column,
    resolve_column_type,
    resolve_column_primary_key,
) -> list[dict[str, Any]]:
    dimensions = []
    for d in sorted(snapshot.get("dimensions", []), key=lambda x: x.get("name", "")):
        if d.get("is_invalid"):
            continue
        entry: dict[str, Any] = {"name": d.get("name", "")}
        if d.get("display_name"):
            entry["display_name"] = d["display_name"]
        if d.get("description"):
            entry["description"] = d["description"]

        col = resolve_column(d.get("source_column_id"))
        if col:
            entry["table"] = col[0]
            entry["column"] = col[1]

        if d.get("is_time_dim"):
            entry["type"] = "date"
        else:
            col_type = resolve_column_type(d.get("source_column_id"))
            entry["type"] = _DIM_TYPE_MAP.get(col_type, "text")

        if d.get("display_folder"):
            entry["folder"] = d["display_folder"]
        if resolve_column_primary_key(d.get("source_column_id")):
            entry["primary_key"] = True

        dimensions.append(entry)
    return dimensions


def _build_hierarchies(
    snapshot: dict[str, Any],
    id_to_column: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    # Index user-defined attributes so UDA-keyed levels can carry their
    # derivation (expression + source column + grain) onto the YAML surface
    # and round-trip faithfully (Bug-1098). Without this, date-hierarchy
    # levels (Year/Quarter/Month derived from one date column) were dropped.
    uda_by_id: dict[str, dict[str, Any]] = {
        str(u.get("id", "")): u for u in snapshot.get("user_defined_attributes", [])
    }
    uda_source_col: dict[str, str] = {}
    for ref in snapshot.get("uda_column_refs", []):
        attr_id = str(ref.get("attribute_id", ""))
        col_ref = id_to_column.get(ref.get("column_id", ""))
        if attr_id and col_ref and col_ref[0] and col_ref[1]:
            # First column ref wins (the derivation base).
            uda_source_col.setdefault(attr_id, f"{col_ref[0]}.{col_ref[1]}")

    hierarchies = []
    for h in sorted(snapshot.get("hierarchies", []), key=lambda x: x.get("name", "")):
        entry: dict[str, Any] = {"name": h.get("name", "")}
        if h.get("display_name"):
            entry["display_name"] = h["display_name"]
        if h.get("description"):
            entry["description"] = h["description"]

        # Real HierarchyDefinition column is `type`
        # (explicit | date_embedded | segment).
        htype = h.get("type", "explicit")
        entry["type"] = htype

        if htype == "date_embedded":
            entry["type"] = "date"
            cal = h.get("calendar_type")
            if cal:
                entry["calendar"] = cal

        levels = h.get("levels", [])
        level_list = []
        for lvl in sorted(levels, key=lambda x: x.get("ordinal", 0)):
            lvl_name = lvl.get("name", "")
            key_source = lvl.get("key_attribute_source")
            key_id = lvl.get("key_attribute_id")

            if key_source == "physical_column":
                col_ref = id_to_column.get(key_id, (None, None)) if key_id else (None, None)
                if col_ref[0] and col_ref[1]:
                    level_list.append({
                        "name": lvl_name,
                        "column": f"{col_ref[0]}.{col_ref[1]}",
                    })
                else:
                    level_list.append({"name": lvl_name})
            elif key_source == "user_defined_attribute":
                # Emit the UDA derivation so the level reconstructs faithfully
                # on import: the source column it derives from, the UDA
                # expression, the output type, and the time grain.
                uda = uda_by_id.get(str(key_id), {})
                lvl_entry: dict[str, Any] = {"name": lvl_name, "derived": True}
                from_col = uda_source_col.get(str(key_id))
                if from_col:
                    lvl_entry["from"] = from_col
                if uda.get("name"):
                    lvl_entry["attribute"] = uda["name"]
                if uda.get("expression"):
                    lvl_entry["expression"] = uda["expression"]
                if uda.get("output_data_type"):
                    lvl_entry["output_type"] = uda["output_data_type"]
                if lvl.get("time_unit"):
                    lvl_entry["grain"] = lvl["time_unit"]
                level_list.append(lvl_entry)
            else:
                level_list.append({"name": lvl_name})

        entry["levels"] = level_list
        hierarchies.append(entry)
    return hierarchies


def _build_personas(
    snapshot: dict[str, Any],
    id_to_dim_name: dict[str, str],
    id_to_measure_name: dict[str, str],
) -> list[dict[str, Any]]:
    personas = []
    for p in sorted(snapshot.get("personas", []), key=lambda x: x.get("slug", "")):
        entry: dict[str, Any] = {
            "name": p.get("slug") or p.get("name", ""),
        }
        if p.get("display_name"):
            entry["display_name"] = p["display_name"]
        if p.get("description"):
            entry["description"] = p["description"]

        default_filters = p.get("default_filters")
        if default_filters:
            entry["filters"] = default_filters

        included_dims = p.get("included_dimension_ids") or []
        if included_dims:
            names = sorted(
                id_to_dim_name.get(did, did) for did in included_dims
            )
            entry["allowed_dimensions"] = names

        included_measures = p.get("included_measure_ids") or []
        if included_measures:
            names = sorted(
                id_to_measure_name.get(mid, mid) for mid in included_measures
            )
            entry["allowed_measures"] = names

        personas.append(entry)
    return personas
