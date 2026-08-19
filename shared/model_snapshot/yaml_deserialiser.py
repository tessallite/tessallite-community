"""YAML model deserialiser — import human-readable YAML into Tessallite.

Parses the YAML format defined in architecture_yaml-model-format.md and
converts it into the internal project bundle JSON structure that the
existing project_rehydrator can consume.
"""
from __future__ import annotations

import uuid
from typing import Any

import yaml


from shared.schemas.domains.aggregates_security import (
    DEFAULT_POPULATION_PARTICIPATION,
    coerce_population_participation,
    persona_filter_value_is_valid,
)
from shared.security.persona_resolver import (
    PersonaAudienceNarrowingError,
    reject_empty_audience_narrowing,
)
from shared.semantic.join_keyword import normalise_cardinality, split_join_token

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


class YamlSyntaxError(YamlImportError):
    """Bug-8139: the document is not even parseable YAML (bad indentation, an
    unclosed quote, a tab where YAML forbids one, etc.) — distinct from
    ``YamlImportError``'s semantic validation errors (a well-formed document
    missing a required field or shaped wrong).

    ``yaml.safe_load`` raising ``yaml.YAMLError`` on malformed input was
    never caught here, so it propagated as a raw ``yaml.YAMLError`` past
    every handler in ``yaml_export.py`` straight to FastAPI's default
    handler — a 500, even though a malformed bundle is client input, not a
    server fault. Subclassing ``YamlImportError`` means the existing
    ``except YamlImportError`` in the import endpoint already maps this to
    422 with no further change there; ``line``/``column`` (1-indexed, the
    way editors and CI report them) are carried as attributes so a caller
    that wants to point the admin at the exact bad line can, instead of a
    bare "import failed".
    """

    def __init__(
        self, message: str, *, line: int | None = None, column: int | None = None
    ):
        self.line = line
        self.column = column
        super().__init__([message])


def _wrap_yaml_syntax_error(exc: "yaml.YAMLError", *, source: str) -> YamlSyntaxError:
    """Convert a raw ``yaml.YAMLError`` into a ``YamlSyntaxError`` carrying
    1-indexed line/column when the parser located the problem (a
    ``yaml.error.MarkedYAMLError`` — the common case: scanner/parser
    errors). Some ``YAMLError`` subclasses carry no mark; the message still
    reports the underlying problem in that case, just without a position.
    """
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc)
    if mark is not None:
        line = mark.line + 1
        column = mark.column + 1
        message = (
            f"{source} is not valid YAML: {problem} (line {line}, column {column})"
        )
    else:
        line = None
        column = None
        message = f"{source} is not valid YAML: {problem}"
    return YamlSyntaxError(message, line=line, column=column)


def parse_model_yaml(content: str) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise _wrap_yaml_syntax_error(exc, source="model YAML") from exc
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
                        # Bug-6294: ``hidden`` is a ModelColumn property that the
                        # API cascades onto the measure/dimension it backs, not a
                        # Dimension/Measure column — writing it onto the
                        # dimension row would fail the insert outright. Set it
                        # where the product actually reads it from.
                        "is_hidden": bool(d.get("hidden", False)),
                    })
            else:
                for existing_column in columns_out:
                    if existing_column["id"] == table_col_to_id[key]:
                        if d.get("primary_key"):
                            existing_column["is_primary_key"] = True
                        # Hidden is a property of the shared column: if ANY
                        # field on it is exported hidden, the column is hidden.
                        if d.get("hidden"):
                            existing_column["is_hidden"] = True
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
                        "is_hidden": bool(m.get("hidden", False)),
                    })
            elif m.get("hidden"):
                for existing_column in columns_out:
                    if existing_column["id"] == table_col_to_id[key]:
                        existing_column["is_hidden"] = True
                        break

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

        # ``type`` is the join ORIENTATION and ``cardinality`` the fan-out.
        # A file written before the join-orientation contract split them can
        # still carry a hyphenated cardinality in ``type``; ``split_join_token``
        # routes it to the cardinality field and infers the orientation that
        # reproduces its historical rendering, so nothing is collapsed to a
        # default and nothing is silently dropped (Bug-1097). An explicit
        # ``cardinality`` key always wins over one inferred from ``type``.
        raw_type = j.get("type", "inner")
        inferred_join_type, inferred_cardinality = split_join_token(raw_type)
        join_type = inferred_join_type or _JOIN_TYPE_MAP.get(raw_type, raw_type)
        cardinality = (
            normalise_cardinality(j.get("cardinality")) or inferred_cardinality
        )
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
            "cardinality": cardinality,
            # Bug-8615 phase G1. An absent key restores the default, which is
            # what every file written before this field carries — so importing
            # an older YAML produces exactly the joins it always did. A present
            # but unrecognised value is coerced to ``undeclared`` rather than
            # rejected, matching how this format treats every other legacy
            # token, and surfaces as a validator warning instead of silently
            # reading as an affirmative declaration.
            "population_participation": (
                coerce_population_participation(j["population_participation"])
                if j.get("population_participation") is not None
                else DEFAULT_POPULATION_PARTICIPATION
            ),
        })

    dims_out = []
    for d in dims_sec:
        table = d.get("table", "")
        column = d.get("column", "")
        col_id = table_col_to_id.get((table, column))

        dim_type = d.get("type", "text")
        # Bug-6294: ``type: date`` alone cannot distinguish "a time dimension"
        # from "a plain dimension that happens to sit on a date column". The
        # exporter now writes an explicit ``time`` flag for the ambiguous case;
        # prefer it when present and fall back to the type inference for files
        # written before it existed (and for hand-written files).
        declared_time = d.get("time")
        is_time = bool(declared_time) if declared_time is not None else (
            dim_type == "date"
        )

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
            # Bug-6294 + Bug-8257: read back the additivity flag rather than
            # letting the NOT NULL True column default make every non-additive
            # measure summable again. Absent (an older or hand-written file) is
            # NOT "additive": the rehydrator's ``_coerce_measure_additivity``
            # derives it from the measure's own shape, and passing None lets
            # that derivation run instead of asserting a default here.
            "is_additive": m.get("additive"),
        }
        if row["is_additive"] is None:
            del row["is_additive"]
        if measure_type == "calculated":
            # Bug-6294: REQUIRED by the create API and it changes the number.
            # Default to the historical implicit behaviour for files that
            # predate the field, matching what the compiler assumes when the
            # column is null.
            row["calc_agg_mode"] = m.get("calc_mode") or "expression_as_written"

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
        # Bug-6294: ``default_filters`` is a NOT NULL JSONB *dict* that the
        # persona gate reads by key. The format specification documented (and
        # its example showed) a LIST of {dimension, operator, value} entries,
        # which the deserialiser would have stored verbatim into that column —
        # a shape the gate cannot read, so the persona's filtering would
        # silently not apply. Refuse the import instead; the spec has been
        # corrected to the mapping form.
        _filters = p.get("filters")
        if _filters is not None and not isinstance(_filters, dict):
            errors.append(
                f"Persona '{p.get('name', '')}' has a 'filters' value of type "
                f"{type(_filters).__name__}; it must be a mapping of "
                "dimension name to filter value. A list here would import as a "
                "persona whose row filters never apply."
            )
            continue
        default_filters = _filters or {}
        bad_filter_keys = [
            k for k, v in default_filters.items()
            if not persona_filter_value_is_valid(v)
        ]
        if bad_filter_keys:
            errors.append(
                f"Persona '{p.get('name', '')}' has invalid default-filter "
                f"operators or shapes for: {', '.join(str(k) for k in bad_filter_keys)}."
            )
            continue
        allowed_dim_ids = [
            dim_name_to_id[n] for n in p.get("allowed_dimensions", [])
            if n in dim_name_to_id
        ]
        allowed_measure_ids = [
            measure_name_to_id[n] for n in p.get("allowed_measures", [])
            if n in measure_name_to_id
        ]
        raw_audience = p.get("audience_roles")
        if raw_audience is None:
            audience_roles: list[str] = []
        elif not isinstance(raw_audience, list) or any(
            not isinstance(r, str) for r in raw_audience
        ):
            errors.append(
                f"Persona '{p.get('name', '')}' has an 'audience_roles' value "
                "that is not a list of role strings."
            )
            continue
        else:
            audience_roles = list(raw_audience)
        # Bug-9266 / F-008-03: a narrowing persona with no audience imports
        # as inert — no regular caller is assigned, so the allow-list never
        # applies. Refuse here (and again at _insert_personas).
        try:
            reject_empty_audience_narrowing(
                audience_roles,
                included_measure_ids=allowed_measure_ids,
                included_dimension_ids=allowed_dim_ids,
                default_filters=default_filters,
            )
        except PersonaAudienceNarrowingError as exc:
            errors.append(f"Persona '{p.get('name', '')}': {exc}")
            continue
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
            "default_filters": default_filters,
            "included_dimension_ids": allowed_dim_ids,
            "included_measure_ids": allowed_measure_ids,
            "audience_roles": audience_roles,
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
    try:
        project_doc = yaml.safe_load(project_content)
    except yaml.YAMLError as exc:
        raise _wrap_yaml_syntax_error(exc, source="project.yaml") from exc
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
