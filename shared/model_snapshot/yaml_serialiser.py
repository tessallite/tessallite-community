"""YAML model serialiser — human-readable export for Git version control.

Converts an internal model snapshot dict (produced by serialiser.snapshot_model)
into a clean YAML string designed to be readable by a non-technical person.

Design rules:
  - No UUIDs in output — names are the primary keys.
  - No internal metadata (created_at, is_invalid, etc.).
  - Plain English field names.
  - Sorted lists for stable diffs.

Lossiness is DECLARED, never silent (Bug-6294)
----------------------------------------------
This format deliberately carries the readable semantic core of a model, not the
whole model. That is a fine design choice; what was not fine was that the loss
was INVISIBLE. A user could export a model to YAML, edit it, re-import it, and
silently lose configuration with no error and no warning — and the format
specification told them the format was "roundtrippable", which it is not.

Three things changed here, and they are the contract this module now keeps:

1. Fields that are cheap to represent and CHANGE THE NUMBERS or the VISIBILITY
   of a model are now carried and read back: ``additive`` (an ``is_additive``
   reset to its True default turns a non-summable measure summable —
   Bug-8257's exact wrong-numbers vector), ``hidden`` on measures and
   dimensions (a hidden field silently becoming visible is an exposure, and the
   spec already documented ``hidden`` while the code never emitted it),
   ``calc_agg_mode`` on calculated measures (``expression_as_written`` vs
   ``per_row_then_aggregate`` are different numbers, and the field is REQUIRED
   by the create API), and an explicit ``time`` flag so a non-time dimension on
   a date column does not become a time dimension on the way back.

2. Everything the format still cannot represent is DECLARED per export in a
   ``not_exported`` block, listing only what THIS model actually carries. A
   simple model exports a clean file; a model with KPIs, aggregates or row
   security says so, in the file, next to the data.

3. The specification document was corrected to match this code.

When adding a field to the export, add it to the deserialiser in the same
change, or add it to ``_NOT_EXPORTED`` so the disclosure stays honest.
"""
from __future__ import annotations

from typing import Any

import yaml

from shared.schemas.domains.aggregates_security import (
    DEFAULT_POPULATION_PARTICIPATION,
    POPULATION_PARTICIPATION_SOURCE_DEFAULT,
)


_JOIN_TYPE_MAP = {
    "many_to_one": "many-to-one",
    "one_to_many": "one-to-many",
    "one_to_one": "one-to-one",
    "many_to_many": "many-to-many",
}

# Bug-6294: snapshot sections this format cannot represent, and the plain
# sentence each one contributes to the exported ``not_exported`` disclosure.
# Only sections the model ACTUALLY populates are listed on any given export, so
# a simple model still produces a clean file.
#
# Ordered deliberately: the entries that change NUMBERS or ACCESS come first,
# because that is the order a reader needs to evaluate the risk of editing and
# re-importing the file.
_NOT_EXPORTED: tuple[tuple[str, str], ...] = (
    (
        "row_security_rules",
        "row-level security rules — re-importing this file creates a model with "
        "NO row filtering",
    ),
    (
        "persona_tag_restrictions",
        "persona column/tag restrictions",
    ),
    ("data_tags", "data classification tags"),
    ("kpis", "KPI definitions, targets and thresholds"),
    ("aggregates", "aggregate table definitions"),
    ("pockets", "pocket table definitions"),
    ("calendar_tables", "custom calendar tables (retail 4-5-4, fiscal, hijri)"),
    (
        "attribute_relationships",
        "declared dimension attribute relationships (derived-grain routing)",
    ),
    (
        "user_defined_attributes",
        "user-defined attributes (derived columns), except the ones a date "
        "hierarchy level carries inline",
    ),
    ("model_settings", "per-model settings"),
    ("named_sets", "named sets"),
    (
        "named_queries",
        "named queries (saved semantic queries and their refresh policies)",
    ),
    ("glossary_entries", "business glossary entries"),
    ("data_quality_rules", "data quality rules"),
    ("drill_through_sets", "drill-through column sets"),
    ("model_parameters", "model parameters"),
    ("lineage_mappings", "external lineage mappings"),
    ("entity_translations", "entity translations"),
    ("refresh_sla_config", "refresh SLA configuration"),
    ("ai_scheduler_config", "AI scheduler configuration"),
)

# Measure fields the format does not carry. Listed separately because they are
# per-measure settings rather than whole sections, and because losing one of
# them is a silent behaviour change inside a measure that otherwise looks
# faithfully round-tripped.
_MEASURE_FIELDS_NOT_EXPORTED: tuple[tuple[str, str], ...] = (
    (
        "semi_additive_account_column_id",
        "the account column of by-account semi-additive measures",
    ),
    (
        "cross_model_source_measure_id",
        "cross-model measure references",
    ),
    (
        "calendar_model_table_id",
        "the calendar binding of time-variant measures",
    ),
    (
        "date_dimension_column_id",
        "the date column of window variant measures",
    ),
)

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
    id_to_col_hidden: dict[str, bool] = {}

    for c in snapshot.get("columns", []):
        cid = c.get("id", "")
        table_id = c.get("model_table_id", "")
        tname = id_to_table.get(table_id, "")
        cname = c.get("column_name") or ""
        id_to_column[cid] = (tname, cname)
        id_to_col_type[cid] = c.get("data_type", "")
        id_to_col_primary_key[cid] = bool(c.get("is_primary_key"))
        # Bug-6294: hidden-ness is a ModelColumn property that the API cascades
        # onto the measure/dimension it backs (see measures.py ``is_hidden``),
        # NOT a column on Measure/Dimension. Resolve it from the same place the
        # product does so the exported flag means what the UI shows.
        id_to_col_hidden[cid] = bool(c.get("is_hidden"))

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

    def _resolve_column_hidden(col_id: str | None) -> bool:
        if not col_id:
            return False
        return id_to_col_hidden.get(col_id, False)

    doc: dict[str, Any] = {}

    id_to_measure_name: dict[str, str] = {
        m.get("id", ""): m.get("name", "")
        for m in snapshot.get("measures", [])
    }

    doc["model"] = _build_model_section(model_meta, connection_name)
    doc["tables"] = _build_tables(snapshot)
    doc["joins"] = _build_joins(snapshot, id_to_table, id_to_column)
    doc["measures"] = _build_measures(
        snapshot, _resolve_column, id_to_measure_name, _resolve_column_hidden,
    )
    doc["dimensions"] = _build_dimensions(
        snapshot,
        _resolve_column,
        _resolve_column_type,
        _resolve_column_primary_key,
        _resolve_column_hidden,
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

    # Bug-6294: declare what this specific model carries that the format cannot
    # represent. Placed LAST in the document so it does not push the model
    # content below the fold, but always present when there is anything to say.
    not_exported = _build_not_exported(snapshot)
    if not_exported:
        doc["not_exported"] = not_exported

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


def _build_not_exported(snapshot: dict[str, Any]) -> list[str]:
    """Bug-6294: the honest list of what this export does NOT carry.

    Returns one plain sentence per thing the model actually has and the format
    cannot represent, so the loss is visible in the artifact itself rather than
    discovered after a re-import. An empty list (a model with none of these)
    produces no block at all.

    Deliberately DATA-DRIVEN off the snapshot: a section that exists but is
    empty says nothing, and a section this format later learns to carry is
    removed from ``_NOT_EXPORTED`` in the same change that teaches it.
    """
    lines: list[str] = []

    for key, description in _NOT_EXPORTED:
        if snapshot.get(key):
            lines.append(description)

    measures = snapshot.get("measures") or []
    for field, description in _MEASURE_FIELDS_NOT_EXPORTED:
        if any(m.get(field) for m in measures):
            lines.append(description)

    # A measure or dimension marked invalid is DROPPED from the export
    # entirely, not merely stripped of a field — the strongest loss of all, and
    # it also breaks any variant whose base was invalid.
    dropped = [
        m.get("name")
        for m in measures
        if m.get("is_invalid")
    ] + [
        d.get("name")
        for d in (snapshot.get("dimensions") or [])
        if d.get("is_invalid")
    ]
    if dropped:
        lines.append(
            "measures/dimensions currently flagged invalid are omitted "
            "entirely: " + ", ".join(sorted(str(n) for n in dropped if n))
        )

    if lines:
        lines.insert(
            0,
            "This file carries the readable semantic model only. Re-importing "
            "it creates a model WITHOUT the following, which this format cannot "
            "represent:",
        )
    return lines


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
        # ``type`` carries the join ORIENTATION (inner/left/right/full) and
        # ``cardinality`` the fan-out (one-to-many/...). They were ONE ORM
        # column until the join-orientation contract split them, so a value
        # written before the split can still be a cardinality token sitting in
        # ``join_type``; it is emitted in its human-friendly hyphenated form so
        # the deserialiser restores it exactly and never collapses it to a
        # default (Bug-1097). Anything else is emitted verbatim.
        raw_join_type = j.get("join_type", "inner")
        join_type = _JOIN_TYPE_MAP.get(raw_join_type, raw_join_type)
        raw_cardinality = j.get("cardinality")
        cardinality = (
            _JOIN_TYPE_MAP.get(raw_cardinality, raw_cardinality)
            if raw_cardinality
            else None
        )

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
        if cardinality:
            entry["cardinality"] = cardinality
        # Bug-8615 phase G1: the modeller's population intent is model-defining
        # content and MUST survive a YAML export/import cycle. Omitted when it
        # is the default, so an untouched model's YAML is byte-identical to
        # before this field existed; the deserialiser restores the same default
        # for an absent key. Dropping it here would silently reset a
        # deliberately-declared ``population_defining`` join back to elidable on
        # the next import — a wrong-numbers path once phase G3 reads the flag.
        participation = j.get("population_participation")
        if participation and participation != DEFAULT_POPULATION_PARTICIPATION:
            entry["population_participation"] = participation
        # Provenance is model content too.  Preserve a non-default source even
        # when its value is the compatibility participation token; otherwise a
        # later introspection pass could overwrite an explicit manual choice.
        source = j.get("population_participation_source")
        if source and source != POPULATION_PARTICIPATION_SOURCE_DEFAULT:
            entry["population_participation_source"] = source
        joins.append(entry)
    return sorted(joins, key=lambda x: (x.get("left", ""), x.get("right", "")))


def _build_measures(
    snapshot: dict[str, Any],
    resolve_column,
    id_to_measure_name: dict[str, str],
    resolve_column_hidden,
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
            # Bug-6294: calc_agg_mode is REQUIRED for a calculated measure at the
            # create API and it changes the NUMBER — 'expression_as_written'
            # combines pre-aggregated measures, 'per_row_then_aggregate'
            # evaluates at fact grain and then aggregates. Dropping it made the
            # re-imported measure compute something different from the one that
            # was exported.
            #
            # Emitted only when the snapshot carries an explicit mode. A measure
            # with a NULL calc_agg_mode (predates the field) is deliberately
            # exported WITHOUT calc_mode rather than fabricating a default the
            # modeller never chose; the deserialiser then applies the documented
            # historical default with a surfaced warning (Bug-9390), so the
            # result-affecting assumption is visible instead of silent.
            if m.get("calc_agg_mode"):
                entry["calc_mode"] = m["calc_agg_mode"]

        agg = m.get("default_agg", "sum")
        if not m.get("expression"):
            entry["aggregation"] = agg

        if m.get("format"):
            entry["format"] = m["format"]
        if m.get("display_folder"):
            entry["folder"] = m["display_folder"]

        # Bug-6294 + Bug-8257: additivity decides whether a consumer may SUM the
        # measure (client-side pivot totals, aggregate rollup planning). The ORM
        # column is NOT NULL defaulting to True, so omitting it here reset every
        # non-additive measure to summable on re-import — silent wrong numbers.
        # Emitted only when False so a plain additive measure keeps a clean file.
        if m.get("is_additive") is False:
            entry["additive"] = False

        # Bug-6294: a hidden measure silently becoming visible on re-import is an
        # exposure, not a cosmetic loss.
        if resolve_column_hidden(m.get("source_column_id")):
            entry["hidden"] = True

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
    resolve_column_hidden,
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

        col_type = resolve_column_type(d.get("source_column_id"))
        if d.get("is_time_dim"):
            entry["type"] = "date"
        else:
            entry["type"] = _DIM_TYPE_MAP.get(col_type, "text")
            # Bug-6294: ``type: date`` was doing double duty as both the column
            # type AND the is_time_dim flag, so a NON-time dimension sitting on a
            # date column round-tripped as a TIME dimension — a real semantic
            # change (time dimensions drive the calendar/variant resolution).
            # Declare the flag explicitly for that one ambiguous case; the
            # deserialiser prefers it over the type inference when present.
            if _DIM_TYPE_MAP.get(col_type) == "date":
                entry["time"] = False

        if d.get("display_folder"):
            entry["folder"] = d["display_folder"]
        if resolve_column_primary_key(d.get("source_column_id")):
            entry["primary_key"] = True
        # Bug-6294: same exposure as the measure ``hidden`` flag. The format
        # specification already documented this field; the code never wrote it.
        if resolve_column_hidden(d.get("source_column_id")):
            entry["hidden"] = True

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

        audience_roles = p.get("audience_roles") or []
        if audience_roles:
            entry["audience_roles"] = list(audience_roles)

        personas.append(entry)
    return personas
