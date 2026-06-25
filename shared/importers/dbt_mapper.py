"""Map parsed dbt semantic models to Tessallite project bundle format.

Converts DbtParseResult into the same JSON structure that
parse_model_yaml / parse_project_yaml produce, so the existing
project_rehydrator can consume it directly.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from shared.importers.dbt_parser import (
    DbtMetric,
    DbtParseResult,
    DbtSavedQuery,
    DbtSemanticModel,
)


_AGG_MAP: dict[str, str] = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    "average": "average",
    "avg": "average",
    "min": "min",
    "max": "max",
    "median": "median",
    "percentile": "percentile",
}

# F-020-08: dbt sum_boolean counts only rows where the boolean is TRUE; the
# previous map collapsed it to "count", which counts ALL rows (e.g. flags
# true/false/true → expected 2, count → 3). Tessallite has no boolean-sum
# aggregation, so it is imported as a disabled measure with a warning rather
# than a silently wrong number.
_UNREPRESENTABLE_AGGS: dict[str, str] = {
    "sum_boolean": (
        "dbt measure '%s' uses sum_boolean (count of true rows) — Tessallite "
        "has no equivalent aggregation. Imported as a disabled measure; "
        "recreate it as a calculated measure (e.g. COUNT with a filter)."
    ),
}

# dbt non_additive_dimension agg → valid Tessallite semi_additive_behavior
# enum (F-020-09). The previous code wrote f"{nad_agg}_over_{nad_name}" (e.g.
# "max_over_order_date"), an invalid enum the rewriter could not interpret.
_NAD_AGG_TO_SEMI_ADDITIVE: dict[str, str] = {
    "last": "last_non_empty",
    "first": "first_non_empty",
    "min": "min",
    "max": "max",
    "average": "avg_of_children",
    "avg": "avg_of_children",
}

_DIM_TYPE_MAP: dict[str, str] = {
    "categorical": "string",
    "time": "timestamp",
}

_SIMPLE_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@dataclass
class MapResult:
    bundle: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def map_dbt_to_tessallite(
    parsed: DbtParseResult,
    project_name: str = "dbt-import",
    project_display_name: str = "dbt Import",
) -> MapResult:
    warnings: list[str] = list(parsed.warnings)

    models: list[dict[str, Any]] = []
    for sm in parsed.semantic_models:
        snap, sm_warnings = _map_semantic_model(sm)
        models.append(snap)
        warnings.extend(sm_warnings)

    _apply_derived_metrics(parsed.metrics, models, warnings)
    _apply_metric_filters(parsed.metrics, models, warnings)

    if parsed.saved_queries:
        _apply_saved_queries(parsed.saved_queries, models, warnings)

    bundle: dict[str, Any] = {
        "schema_version": 1,
        "export_format": "tessallite-project/v1",
        "project": {
            "slug": _slugify(project_name),
            "display_name": project_display_name,
        },
        "connections": [],
        "models": models,
    }

    return MapResult(bundle=bundle, warnings=warnings)


def _map_semantic_model(
    sm: DbtSemanticModel,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    gen = _id_gen()

    model_id = gen()
    source_id = gen()
    table_ref = _extract_model_ref(sm.model)
    table_name = table_ref.split(".")[-1] if "." in table_ref else table_ref

    table_id = gen()
    tables = [{
        "id": table_id,
        "model_id": model_id,
        "source_id": source_id,
        "physical_name": table_ref,
        "alias": table_name,
        "display_name": table_name,
        "description": sm.description,
        "table_type": "fact",
    }]

    columns: list[dict[str, Any]] = []
    col_map: dict[str, str] = {}
    expr_uda_map: dict[str, str] = {}
    udas_out: list[dict[str, Any]] = []
    uda_refs_out: list[dict[str, Any]] = []

    for dim in sm.dimensions:
        col_name = dim.expr or dim.name
        if _SIMPLE_IDENT_RE.match(str(col_name)):
            col_id = gen()
            col_map[dim.name] = col_id
            data_type = _DIM_TYPE_MAP.get(dim.dim_type, "string")
            columns.append({
                "id": col_id,
                "model_table_id": table_id,
                "column_name": col_name,
                "data_type": data_type,
            })
        else:
            uda_id = gen()
            expr_uda_map[dim.name] = uda_id
            udas_out.append({
                "id": uda_id,
                "table_id": table_id,
                "name": f"dbt_{dim.name}",
                "expression": str(col_name),
                "output_data_type": _DIM_TYPE_MAP.get(dim.dim_type, "string"),
                "description": f"dbt expr for dimension '{dim.name}'",
                "validated": False,
                "validation_error": None,
            })
            warnings.append(
                f"Dimension '{dim.name}' has SQL expression "
                f"'{col_name}' — imported as unvalidated UDA"
            )

    for measure in sm.measures:
        col_name = measure.expr or measure.name
        if _SIMPLE_IDENT_RE.match(str(col_name)):
            if col_name not in col_map:
                col_id = gen()
                col_map[col_name] = col_id
                columns.append({
                    "id": col_id,
                    "model_table_id": table_id,
                    "column_name": col_name,
                    "data_type": "numeric",
                })
        else:
            uda_id = gen()
            expr_uda_map[measure.name] = uda_id
            udas_out.append({
                "id": uda_id,
                "table_id": table_id,
                "name": f"dbt_{measure.name}",
                "expression": str(col_name),
                "output_data_type": "numeric",
                "description": f"dbt expr for measure '{measure.name}'",
                "validated": False,
                "validation_error": None,
            })
            warnings.append(
                f"Measure '{measure.name}' has SQL expression "
                f"'{col_name}' — imported as unvalidated UDA"
            )

    dims_out: list[dict[str, Any]] = []
    for dim in sm.dimensions:
        is_time = dim.dim_type == "time"
        dim_entry: dict[str, Any] = {
            "id": gen(),
            "model_id": model_id,
            "name": dim.name,
            "display_name": dim.label or _humanize(dim.name),
            "description": dim.description or None,
            "is_time_dim": is_time,
        }
        if dim.name in expr_uda_map:
            dim_entry["user_defined_attribute_id"] = expr_uda_map[dim.name]
            dim_entry["source_column_id"] = None
        else:
            dim_entry["source_column_id"] = col_map.get(dim.name)
        dims_out.append(dim_entry)

    measures_out: list[dict[str, Any]] = []
    for m in sm.measures:
        raw_agg = (m.agg or "").lower().strip()
        invalid_reason: str | None = None
        if raw_agg in _UNREPRESENTABLE_AGGS:
            invalid_reason = _UNREPRESENTABLE_AGGS[raw_agg] % m.name
            warnings.append(invalid_reason)
            agg = "sum"
        else:
            agg = _AGG_MAP.get(raw_agg)
            if agg is None:
                invalid_reason = (
                    f"Unsupported aggregation '{m.agg}' for measure '{m.name}' "
                    f"in model '{sm.name}' — imported as a disabled measure "
                    f"(defaulted to 'sum')."
                )
                warnings.append(invalid_reason)
                agg = "sum"

        semi_additive = None
        if m.non_additive_dimension:
            nad_name = m.non_additive_dimension.get("name", "")
            nad_agg = (m.non_additive_dimension.get("agg", "") or "").lower().strip()
            if nad_name and nad_agg:
                semi_additive = _NAD_AGG_TO_SEMI_ADDITIVE.get(nad_agg)
                if semi_additive is None:
                    warnings.append(
                        f"Non-additive dimension agg '{nad_agg}' on measure "
                        f"'{m.name}' has no Tessallite semi-additive equivalent "
                        f"— imported as fully additive. Set it manually."
                    )

        measure_entry: dict[str, Any] = {
            "id": gen(),
            "model_id": model_id,
            "name": m.name,
            "display_name": m.label or _humanize(m.name),
            "description": m.description or None,
            "measure_type": "standard",
            "default_agg": agg,
            "is_invalid": invalid_reason is not None,
            "invalid_reason": invalid_reason,
            "semi_additive_behavior": semi_additive,
        }
        if m.name in expr_uda_map:
            measure_entry["user_defined_attribute_id"] = expr_uda_map[m.name]
            measure_entry["source_column_id"] = None
        else:
            measure_entry["source_column_id"] = col_map.get(m.expr or m.name)
        measures_out.append(measure_entry)

    joins_out: list[dict[str, Any]] = []
    for entity in sm.entities:
        if entity.entity_type == "foreign":
            warnings.append(
                f"Foreign entity '{entity.name}' in model '{sm.name}' "
                f"detected — join will need manual configuration in Tessallite"
            )

    hier_out: list[dict[str, Any]] = []
    time_dims = [d for d in sm.dimensions if d.dim_type == "time"]
    for td in time_dims:
        grain = td.type_params.get("time_granularity", "day")
        levels = _time_levels_for_grain(grain)
        h_id = gen()
        time_col_id = col_map.get(td.name)
        col_name = td.expr or td.name
        is_bare_col = _SIMPLE_IDENT_RE.match(col_name) is not None
        if is_bare_col:
            extract_source = f"\"{col_name}\""
        else:
            extract_source = f"({col_name})"
        level_entries = []
        for i, lvl in enumerate(levels):
            uda_id = gen()
            uda_name = f"{td.name}_{lvl.lower()}"
            udas_out.append({
                "id": uda_id,
                "table_id": table_id,
                "name": uda_name,
                "expression": f"EXTRACT({lvl.upper()} FROM ({extract_source}))",
                "output_data_type": "integer",
                "description": f"Auto-generated for hierarchy '{td.name}_hierarchy' ({lvl.lower()})",
                "validated": False,
                "validation_error": None,
            })
            if time_col_id:
                uda_refs_out.append({
                    "id": gen(),
                    "attribute_id": uda_id,
                    "column_id": time_col_id,
                })
            level_entries.append({
                "id": gen(),
                "hierarchy_id": h_id,
                "name": lvl,
                "ordinal": i,
                "key_attribute_id": uda_id,
                "key_attribute_source": "user_defined_attribute",
                "time_unit": lvl.lower(),
            })
        hier_out.append({
            "id": h_id,
            "model_id": model_id,
            "name": f"{td.name}_hierarchy",
            "type": "date_embedded",
            "dimension_kind": "time",
            "calendar_type": "standard",
            "levels": level_entries,
        })

    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "model_id": model_id,
        "model": {
            "id": model_id,
            "slug": _slugify(sm.name),
            "display_name": sm.label or _humanize(sm.name),
            "description": sm.description or None,
            "refresh_strategy": "manual",
            "max_aggregates": 20,
            "aggregations_enabled": True,
            "include_all_measures": True,
        },
        "tables": tables,
        "columns": columns,
        "joins": joins_out,
        "dimensions": dims_out,
        "measures": measures_out,
        "hierarchies": hier_out,
        "personas": [{
            "id": gen(),
            "model_id": model_id,
            "slug": "everyone",
            "name": "Everyone",
            "description": "Default persona — full access",
        }],
        "user_defined_attributes": udas_out,
        "uda_column_refs": uda_refs_out,
        "aggregates": [],
        "data_sources": [{
            "id": source_id,
            "model_id": model_id,
            "source_type": "import_placeholder",
            "display_name": "dbt Import Source",
            "config": {},
        }],
        "data_targets": [],
    }
    return snapshot, warnings


def _apply_derived_metrics(
    metrics: list[DbtMetric],
    models: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    measure_map: dict[str, dict[str, Any]] = {}
    for model in models:
        for m in model.get("measures", []):
            measure_map[m["name"]] = m

    for metric in metrics:
        if metric.metric_type == "simple":
            measure_name = metric.type_params.get("measure", "")
            if isinstance(measure_name, dict):
                measure_name = measure_name.get("name", "")
            if measure_name in measure_map:
                m = measure_map[measure_name]
                if metric.label:
                    m["display_name"] = metric.label
                if metric.description:
                    m["description"] = metric.description
            else:
                warnings.append(
                    f"Simple metric '{metric.name}' references unknown "
                    f"measure '{measure_name}'"
                )

        elif metric.metric_type == "derived":
            warnings.append(
                f"Derived metric '{metric.name}' imported as a note — "
                f"create a calculated measure manually if needed"
            )

        elif metric.metric_type == "cumulative":
            warnings.append(
                f"Cumulative metric '{metric.name}' — Tessallite represents "
                f"time-variant measures differently; review after import"
            )

        elif metric.metric_type in ("ratio", "conversion"):
            warnings.append(
                f"{metric.metric_type.title()} metric '{metric.name}' "
                f"requires manual setup as a calculated measure"
            )

        else:
            warnings.append(
                f"Unknown metric type '{metric.metric_type}' for '{metric.name}'"
            )


def _apply_metric_filters(
    metrics: list[DbtMetric],
    models: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    """Translate dbt metric filters into the real persona ``default_filters``.

    F-020-16: the persona ``default_filters`` column is a JSONB *dict* keyed by
    dimension name (``{dim_name: value | {op: value}}``) — the shape the query
    router's ``merge_default_filters`` reads. The previous implementation
    appended ``{source_metric, filter_sql}`` dicts and turned the field into a
    list, which no consumer reads (junk accepted at the API, ignored at query
    time). We now parse only simple ``{{ Dimension('x') }} <op> <value>``
    equality/comparison predicates into the real shape; anything richer is
    reported as a warning and *not* stored, so we never persist data the model
    cannot use.
    """
    parsed_filters: dict[str, Any] = {}
    unparseable: list[str] = []

    for metric in metrics:
        if not metric.filter:
            continue
        if metric.metric_type != "simple":
            continue
        dim_name, op, value = _parse_simple_filter(metric.filter)
        if dim_name is None:
            unparseable.append(f"{metric.name}: {metric.filter}")
            continue
        # eq collapses to a bare scalar; other operators carry {op: value}.
        parsed_filters[dim_name] = value if op == "=" else {_OP_MAP[op]: value}

    if parsed_filters:
        for model in models:
            for persona in model.get("personas", []):
                existing = persona.get("default_filters") or {}
                if not isinstance(existing, dict):
                    existing = {}
                # New keys do not clobber an existing persona filter.
                merged = {**parsed_filters, **existing}
                persona["default_filters"] = merged
        warnings.append(
            f"{len(parsed_filters)} dbt metric filter(s) translated into "
            f"persona default filters."
        )

    if unparseable:
        warnings.append(
            f"{len(unparseable)} dbt metric filter(s) were too complex to "
            f"translate and were skipped (recreate as persona filters "
            f"manually): " + "; ".join(unparseable[:5])
        )


# Bug-2700: map to the canonical operator tokens the query-router consumer
# accepts (persona_gate._SUPPORTED_OPERATORS / rewrite/conditions.py). The
# not-equal token is "neq" — "ne" is silently rejected by _coerce_filter,
# dropping the persona scope-exclusion at query time (wrong data scope).
_OP_MAP = {"=": "eq", "!=": "neq", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}

# dbt filters reference dimensions/entities as Jinja:
#   {{ Dimension('order__status') }} = 'completed'
#   {{ Dimension('customer__tier') }} >= 3
_FILTER_RE = re.compile(
    r"""\{\{\s*(?:Dimension|Entity|TimeDimension)\(\s*['"]([^'"]+)['"]\s*\)\s*\}\}"""
    r"""\s*(!=|>=|<=|=|>|<)\s*(.+?)\s*$""",
    re.IGNORECASE,
)


def _parse_simple_filter(filter_sql: str) -> tuple[str | None, str | None, Any]:
    """Parse a single ``{{ Dimension('x') }} <op> <value>`` predicate.

    Returns ``(dimension_name, operator, value)`` or ``(None, None, None)`` if
    the filter is compound (AND/OR), references multiple columns, or otherwise
    is not a single simple comparison.
    """
    text = (filter_sql or "").strip()
    # Reject obviously compound predicates — we only translate single ones.
    lowered = text.lower()
    if " and " in lowered or " or " in lowered:
        return None, None, None
    m = _FILTER_RE.match(text)
    if not m:
        return None, None, None
    raw_ref, op, raw_value = m.group(1), m.group(2), m.group(3).strip()
    # dbt qualifies as entity__column; the dimension name is the last segment.
    dim_name = raw_ref.split("__")[-1]
    value = _coerce_filter_value(raw_value)
    return dim_name, op, value


def _coerce_filter_value(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        return raw[1:-1]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _apply_saved_queries(
    saved_queries: list[DbtSavedQuery],
    models: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    """Record saved_queries as informational notes on the bundle."""
    for sq in saved_queries:
        parts = [f"saved_query '{sq.name}'"]
        if sq.metrics:
            parts.append(f"metrics: {', '.join(sq.metrics[:5])}")
        if sq.group_by:
            dims = [g.replace("Dimension('", "").rstrip("')") for g in sq.group_by[:5]]
            parts.append(f"group_by: {', '.join(dims)}")
        if sq.where:
            parts.append(f"filters: {len(sq.where)}")
        warnings.append(
            f"{' | '.join(parts)} — "
            f"create a Tessallite report template or saved view to replicate"
        )


def _extract_model_ref(ref_str: str) -> str:
    match = re.match(r"ref\(['\"](\w+)['\"]\)", ref_str)
    if match:
        return match.group(1)
    return ref_str


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower().strip())
    return slug.strip("_")


def _humanize(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


def _time_levels_for_grain(grain: str) -> list[str]:
    all_levels = ["Year", "Quarter", "Month", "Week", "Day"]
    grain_idx = {
        "year": 0,
        "quarter": 1,
        "month": 2,
        "week": 3,
        "day": 4,
    }
    idx = grain_idx.get(grain.lower(), 4)
    return all_levels[: idx + 1]


def _id_gen():
    def _next() -> str:
        return str(uuid.uuid4())
    return _next
