"""YAML model deserialiser — import human-readable YAML into Tessallite.

Parses the YAML format defined in architecture_yaml-model-format.md and
converts it into the internal project bundle JSON structure that the
existing project_rehydrator can consume.
"""
from __future__ import annotations

import uuid
from typing import Any

import yaml


_JOIN_TYPE_MAP = {
    "many-to-one": "many_to_one",
    "one-to-many": "one_to_many",
    "one-to-one": "one_to_one",
    "many-to-many": "many_to_many",
}

_DIM_TYPE_MAP = {
    "text": "string",
    "number": "numeric",
    "date": "timestamp",
    "boolean": "boolean",
}

_HIER_TYPE_MAP = {
    "date": "date_embedded",
    "explicit": "explicit",
    "segment": "segment",
}


class YamlImportError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s): {'; '.join(errors[:5])}")


def parse_model_yaml(content: str) -> dict[str, Any]:
    doc = yaml.safe_load(content)
    if not isinstance(doc, dict):
        raise YamlImportError(["YAML root must be a mapping"])

    errors: list[str] = []
    warnings: list[str] = []

    model_sec = doc.get("model")
    if not model_sec or not isinstance(model_sec, dict):
        errors.append("Missing required 'model' section")
        raise YamlImportError(errors)

    model_name = model_sec.get("name", "")
    if not model_name:
        errors.append("model.name is required")

    gen_id = _id_gen()
    model_id = gen_id()

    # Synthesise a placeholder data source. The YAML format carries no
    # connection credentials; ModelTable.source_id is NOT NULL, so every
    # table must bind to a source. The import endpoint injects the real
    # project_connection_id afterwards (mirrors dbt_import.py).
    source_id = gen_id()
    data_sources_out = [{
        "id": source_id,
        "model_id": model_id,
        "source_type": "import_placeholder",
        "display_name": "YAML Import Source",
        "config": {},
    }]

    tables_sec = doc.get("tables", [])
    table_name_to_id: dict[str, str] = {}
    table_col_to_id: dict[tuple[str, str], str] = {}
    tables_out = []
    columns_out = []

    for t in tables_sec:
        t_id = gen_id()
        t_name = t.get("name", "")
        if not t_name:
            errors.append("Each table must have a name")
            continue
        table_name_to_id[t_name] = t_id

        # `source_table` is the schema-qualified physical reference; the
        # ORM stores it verbatim in physical_name. `name` is the per-model
        # handle (alias).
        physical_name = t.get("source_table") or t_name
        role = t.get("role") or "dim_detail"

        tables_out.append({
            "id": t_id,
            "model_id": model_id,
            "source_id": source_id,
            "physical_name": physical_name,
            "alias": t_name,
            "display_name": t_name,
            "table_type": role,
            "description": t.get("description"),
        })

    dims_sec = doc.get("dimensions", [])
    for d in dims_sec:
        table = d.get("table", "")
        column = d.get("column", "")
        if table and column:
            key = (table, column)
            if key not in table_col_to_id:
                col_id = gen_id()
                table_col_to_id[key] = col_id
                t_id = table_name_to_id.get(table)
                if t_id:
                    columns_out.append({
                        "id": col_id,
                        "model_table_id": t_id,
                        "column_name": column,
                        "data_type": _DIM_TYPE_MAP.get(d.get("type", "text"), "string"),
                        "is_primary_key": bool(d.get("primary_key", False)),
                    })
            elif d.get("primary_key"):
                for existing_column in columns_out:
                    if existing_column["id"] == table_col_to_id[key]:
                        existing_column["is_primary_key"] = True
                        break

    measures_sec = doc.get("measures", [])
    for m in measures_sec:
        table = m.get("table", "")
        column = m.get("column", "")
        if table and column:
            key = (table, column)
            if key not in table_col_to_id:
                col_id = gen_id()
                table_col_to_id[key] = col_id
                t_id = table_name_to_id.get(table)
                if t_id:
                    columns_out.append({
                        "id": col_id,
                        "model_table_id": t_id,
                        "column_name": column,
                        "data_type": "numeric",
                    })

    def _resolve_or_create_column(table: str, column: str, default_type: str) -> str | None:
        """Return the column id for table.column, creating a column row if
        it does not yet exist. Returns None if the table is unknown."""
        if not table or not column:
            return None
        key = (table, column)
        col_id = table_col_to_id.get(key)
        if col_id:
            return col_id
        t_id = table_name_to_id.get(table)
        if not t_id:
            return None
        col_id = gen_id()
        table_col_to_id[key] = col_id
        columns_out.append({
            "id": col_id,
            "model_table_id": t_id,
            "column_name": column,
            "data_type": default_type,
        })
        return col_id

    joins_out = []
    for j in doc.get("joins", []):
        left = j.get("left", "")
        right = j.get("right", "")
        if left not in table_name_to_id:
            errors.append(f"Join references unknown table '{left}'")
        if right not in table_name_to_id:
            errors.append(f"Join references unknown table '{right}'")

        # Reverse the human-friendly hyphenation for the four canonical
        # cardinalities; any other value (e.g. directional `right`/`left`)
        # is preserved verbatim rather than collapsed to a default, so the
        # source join cardinality round-trips faithfully (Bug-1097).
        raw_type = j.get("type", "many-to-one")
        join_type = _JOIN_TYPE_MAP.get(raw_type, raw_type)
        condition = j.get("on", "")

        left_col = ""
        right_col = ""
        if "=" in condition:
            parts = condition.split("=", 1)
            l_part = parts[0].strip()
            r_part = parts[1].strip()
            if "." in l_part:
                left_col = l_part.split(".", 1)[1]
            if "." in r_part:
                right_col = r_part.split(".", 1)[1]

        # The Join ORM requires NOT NULL left_column_id / right_column_id.
        # A join with no resolvable column condition cannot be rehydrated;
        # surface it as a validation error rather than insert a broken row.
        left_col_id = _resolve_or_create_column(left, left_col, "integer")
        right_col_id = _resolve_or_create_column(right, right_col, "integer")
        if not left_col_id or not right_col_id:
            errors.append(
                f"Join '{left}' -> '{right}' has no resolvable column "
                f"condition (expected 'left.col = right.col' in 'on')"
            )
            continue

        joins_out.append({
            "id": gen_id(),
            "model_id": model_id,
            "left_table_id": table_name_to_id.get(left, ""),
            "right_table_id": table_name_to_id.get(right, ""),
            "left_column_id": left_col_id,
            "right_column_id": right_col_id,
            "join_type": join_type,
        })

    dims_out = []
    for d in dims_sec:
        table = d.get("table", "")
        column = d.get("column", "")
        col_id = table_col_to_id.get((table, column))

        dim_type = d.get("type", "text")
        is_time = dim_type == "date"

        dims_out.append({
            "id": gen_id(),
            "model_id": model_id,
            "name": d.get("name", ""),
            "display_name": d.get("display_name"),
            "description": d.get("description"),
            "display_folder": d.get("folder"),
            "source_column_id": col_id,
            "is_time_dim": is_time,
        })

    # First pass: assign an id to every measure (including variants) so the
    # variant-to-base name linkage can resolve in a second pass. Variant
    # measures (YTD/QTD/… time-intelligence derivations) are first-class rows
    # that FK to their base measure via variant_of_measure_id; the DB
    # constraint measures_variant_consistency requires variant_kind and
    # variant_of_measure_id together. The YAML carries the base measure NAME
    # (`variant_of`), which the deserialiser resolves to the base's new id —
    # exactly the name-based resolution already used for personas (Bug-1099).
    measure_name_to_new_id: dict[str, str] = {}
    measure_records: list[tuple[dict[str, Any], str]] = []
    for m in measures_sec:
        m_name = m.get("name", "")
        m_id = gen_id()
        measure_name_to_new_id[m_name] = m_id
        measure_records.append((m, m_id))

    measures_out = []
    for m, m_id in measure_records:
        table = m.get("table", "")
        column = m.get("column", "")
        col_id = table_col_to_id.get((table, column))

        measure_type = "calculated" if m.get("expression") else "standard"

        row: dict[str, Any] = {
            "id": m_id,
            "model_id": model_id,
            "name": m.get("name", ""),
            "display_name": m.get("display_name"),
            "description": m.get("description"),
            "display_folder": m.get("folder"),
            "source_column_id": col_id,
            "measure_type": measure_type,
            "expression": m.get("expression"),
            "default_agg": m.get("aggregation", "sum"),
            "format": m.get("format"),
            "semi_additive_behavior": m.get("semi_additive"),
        }

        if m.get("variant"):
            base_name = m.get("variant_of", "")
            base_id = measure_name_to_new_id.get(base_name)
            if not base_id:
                warnings.append(
                    f"Measure '{m.get('name', '')}' is a '{m['variant']}' "
                    f"variant of '{base_name}', which is not present in the "
                    f"import — skipped (the base measure must be exported too)"
                )
                continue
            row["variant_kind"] = m["variant"]
            row["variant_of_measure_id"] = base_id
            if m.get("variant_n") is not None:
                row["variant_n"] = m["variant_n"]

        measures_out.append(row)

    hier_out = []
    udas_out: list[dict[str, Any]] = []
    uda_refs_out: list[dict[str, Any]] = []

    def _resolve_table_id_of_column(col_id: str) -> str | None:
        for c in columns_out:
            if c["id"] == col_id:
                return c.get("model_table_id")
        return None

    for h in doc.get("hierarchies", []):
        htype = _HIER_TYPE_MAP.get(h.get("type", "explicit"), "explicit")

        # HierarchyDefinition column is `type` (not `hierarchy_type`); it has
        # no display_name column. Use dimension_kind="time" for date hierarchies.
        h_entry: dict[str, Any] = {
            "id": gen_id(),
            "model_id": model_id,
            "name": h.get("name", ""),
            "description": h.get("description"),
            "type": htype,
        }

        if htype == "date_embedded":
            h_entry["calendar_type"] = h.get("calendar", "standard")
            h_entry["dimension_kind"] = "time"

        levels_raw = h.get("levels", [])
        levels_out = []
        ordinal = 0
        for lvl in levels_raw:
            # A HierarchyLevel requires a NOT NULL key_attribute_id +
            # key_attribute_source. Two faithful shapes round-trip (Bug-1098):
            #   - physical_column levels carry a `column: table.col` ref.
            #   - derived levels (the dominant date-hierarchy shape: Year /
            #     Quarter / Month from one date column) carry a UDA derivation
            #     (`derived: true`, `from: table.col`, `expression`, `grain`),
            #     which we reconstruct into a UserDefinedAttribute + column ref
            #     and key the level on it.
            if isinstance(lvl, dict):
                lvl_name = lvl.get("name", "")
                col_ref = lvl.get("column", "")
            else:
                lvl_name = str(lvl)
                col_ref = ""

            is_derived = isinstance(lvl, dict) and (
                lvl.get("derived") or lvl.get("expression") or lvl.get("from")
            )

            if is_derived:
                from_ref = lvl.get("from", "")
                source_col_id = None
                if from_ref and "." in from_ref:
                    f_tbl, f_col = from_ref.split(".", 1)
                    source_col_id = _resolve_or_create_column(f_tbl, f_col, "timestamp")
                if not source_col_id:
                    warnings.append(
                        f"Hierarchy '{h.get('name', '')}' level '{lvl_name}' is "
                        f"derived but has no resolvable source column "
                        f"(expected 'from: table.col') — skipped on import"
                    )
                    continue
                table_id = _resolve_table_id_of_column(source_col_id)
                if not table_id:
                    warnings.append(
                        f"Hierarchy '{h.get('name', '')}' level '{lvl_name}' "
                        f"source column has no resolvable table — skipped on import"
                    )
                    continue

                uda_id = gen_id()
                uda_name = lvl.get("attribute") or f"{h.get('name', '')}_{lvl_name}"
                udas_out.append({
                    "id": uda_id,
                    "model_id": model_id,
                    "table_id": table_id,
                    "name": uda_name,
                    "expression": lvl.get("expression") or lvl_name,
                    "output_data_type": lvl.get("output_type") or "string",
                    "validated": True,
                    # Derived hierarchy-level UDAs are generator output (H10).
                    "is_generated": True,
                })
                uda_refs_out.append({
                    "id": gen_id(),
                    "attribute_id": uda_id,
                    "column_id": source_col_id,
                })
                level_entry: dict[str, Any] = {
                    "id": gen_id(),
                    "hierarchy_id": h_entry["id"],
                    "name": lvl_name,
                    "ordinal": ordinal,
                    "key_attribute_id": uda_id,
                    "key_attribute_source": "user_defined_attribute",
                }
                if lvl.get("grain"):
                    level_entry["time_unit"] = lvl["grain"]
                levels_out.append(level_entry)
                ordinal += 1
                continue

            key_attr_id = None
            if col_ref and "." in col_ref:
                tbl, col = col_ref.split(".", 1)
                key_attr_id = _resolve_or_create_column(tbl, col, "string")

            if not key_attr_id:
                warnings.append(
                    f"Hierarchy '{h.get('name', '')}' level '{lvl_name}' has "
                    f"no resolvable column key — skipped on import"
                )
                continue

            levels_out.append({
                "id": gen_id(),
                "hierarchy_id": h_entry["id"],
                "name": lvl_name,
                "ordinal": ordinal,
                "key_attribute_id": key_attr_id,
                "key_attribute_source": "physical_column",
            })
            ordinal += 1

        h_entry["levels"] = levels_out
        hier_out.append(h_entry)

    dim_name_to_id = {d["name"]: d["id"] for d in dims_out}
    measure_name_to_id = {m["name"]: m["id"] for m in measures_out}

    personas_out = []
    for p in doc.get("personas", []):
        allowed_dim_ids = [
            dim_name_to_id[n] for n in p.get("allowed_dimensions", [])
            if n in dim_name_to_id
        ]
        allowed_measure_ids = [
            measure_name_to_id[n] for n in p.get("allowed_measures", [])
            if n in measure_name_to_id
        ]
        slug = p.get("name", "")
        # Persona ORM has no display_name column; NOT NULL name carries the
        # human label (falls back to slug).
        personas_out.append({
            "id": gen_id(),
            "model_id": model_id,
            "name": p.get("display_name") or slug,
            "slug": slug,
            "description": p.get("description"),
            # default_filters is a NOT NULL JSONB dict; never None.
            "default_filters": p.get("filters") or {},
            "included_dimension_ids": allowed_dim_ids,
            "included_measure_ids": allowed_measure_ids,
        })

    if errors:
        raise YamlImportError(errors)

    snapshot = {
        "schema_version": 2,
        "model_id": model_id,
        # F-020-17: carry the per-model connection NAME so the import endpoint
        # can rebind the placeholder data source to a same-named project
        # connection (the YAML export side writes it in the model section).
        "connection_name": model_sec.get("connection"),
        "model": {
            "id": model_id,
            "slug": model_name,
            "display_name": model_sec.get("display_name", model_name),
            "description": model_sec.get("description"),
            "refresh_strategy": model_sec.get("refresh", "manual"),
            "max_aggregates": model_sec.get("max_aggregates", 20),
            "aggregations_enabled": True,
            "include_all_measures": True,
        },
        "tables": tables_out,
        "columns": columns_out,
        "user_defined_attributes": udas_out,
        "uda_column_refs": uda_refs_out,
        "joins": joins_out,
        "dimensions": dims_out,
        "measures": measures_out,
        "hierarchies": hier_out,
        "personas": personas_out,
        "aggregates": [],
        "data_sources": data_sources_out,
        "data_targets": [],
        "warnings": warnings,
    }
    return snapshot


def parse_project_yaml(
    project_content: str,
    model_contents: dict[str, str],
) -> dict[str, Any]:
    project_doc = yaml.safe_load(project_content)
    if not isinstance(project_doc, dict):
        raise YamlImportError(["project.yaml root must be a mapping"])

    project_sec = project_doc.get("project", {})

    models = []
    for filename, content in sorted(model_contents.items()):
        snap = parse_model_yaml(content)
        models.append(snap)

    # F-020-21: the YAML import endpoint rebinds each model to a live project
    # connection by NAME (see yaml_export.import_project_yaml / F-020-17) — it
    # never consumed a synthesised `connections` list, so emitting one with
    # random UUIDs was dead. The per-model `connection_name` carries the
    # binding instead.
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "export_format": "tessallite-project/v1",
        "project": {
            "slug": project_sec.get("name", ""),
            "display_name": project_sec.get("display_name", ""),
        },
        "models": models,
    }
    return bundle


def _id_gen():
    def _next() -> str:
        return str(uuid.uuid4())
    return _next
