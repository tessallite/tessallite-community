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
from shared.importers.import_warnings import (
    ImportWarningResponse,
    extend_known_import_warnings,
    make_import_warning,
)
from shared.model_snapshot.slug_utils import slugify
from shared.model_defaults import DEFAULT_INCLUDE_ALL_MEASURES


_AGG_MAP: dict[str, str] = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    # Bug-6591: emit CANONICAL default_agg tokens (VALID_DEFAULT_AGGS:
    # sum/avg/min/max/count/count_distinct + pNN). "average"/"median"/
    # "percentile" saved cleanly but failed LATE at query time (no such SQL
    # function). median == exact 50th percentile → p50. A bare "percentile"
    # has no fraction here so it is intentionally absent — _map_measures then
    # imports it as a disabled measure rather than guessing a percentile.
    "average": "avg",
    "avg": "avg",
    "min": "min",
    "max": "max",
    "median": "p50",
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
    warnings: list[ImportWarningResponse] = field(default_factory=list)


def map_dbt_to_tessallite(
    parsed: DbtParseResult,
    project_name: str = "dbt-import",
    project_display_name: str = "dbt Import",
) -> MapResult:
    warnings: list[ImportWarningResponse] = []
    extend_known_import_warnings(warnings, parsed.warnings)

    models: list[dict[str, Any]] = []
    for sm in parsed.semantic_models:
        snap, sm_warnings = _map_semantic_model(sm)
        models.append(snap)
        extend_known_import_warnings(warnings, sm_warnings)

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
) -> tuple[dict[str, Any], list[ImportWarningResponse]]:
    warnings: list[ImportWarningResponse] = []
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
            warnings.append(make_import_warning(
                code="dbt.expression_unvalidated",
                params={"element_type": "dimension", "element_name": dim.name},
                detail=(
                    f"Dimension '{dim.name}' has SQL expression "
                    f"'{col_name}' — imported as unvalidated UDA"
                ),
            ))

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
            warnings.append(make_import_warning(
                code="dbt.expression_unvalidated",
                params={"element_type": "measure", "element_name": measure.name},
                detail=(
                    f"Measure '{measure.name}' has SQL expression "
                    f"'{col_name}' — imported as unvalidated UDA"
                ),
            ))

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
            warnings.append(make_import_warning(
                code="dbt.measure_disabled",
                params={"measure": m.name, "reason": raw_agg},
                detail=invalid_reason,
            ))
            agg = "sum"
        else:
            agg = _AGG_MAP.get(raw_agg)
            if agg is None:
                invalid_reason = (
                    f"Unsupported aggregation '{m.agg}' for measure '{m.name}' "
                    f"in model '{sm.name}' — imported as a disabled measure "
                    f"(defaulted to 'sum')."
                )
                warnings.append(make_import_warning(
                    code="dbt.measure_disabled",
                    params={"measure": m.name, "reason": raw_agg or "unknown"},
                    detail=invalid_reason,
                ))
                agg = "sum"

        semi_additive = None
        if m.non_additive_dimension:
            nad_name = m.non_additive_dimension.get("name", "")
            nad_agg = (m.non_additive_dimension.get("agg", "") or "").lower().strip()
            if nad_name and nad_agg:
                semi_additive = _NAD_AGG_TO_SEMI_ADDITIVE.get(nad_agg)
                if semi_additive is None:
                    warnings.append(make_import_warning(
                        code="dbt.semi_additive_omitted",
                        params={"measure": m.name, "behavior": nad_agg},
                        detail=(
                            f"Non-additive dimension agg '{nad_agg}' on measure "
                            f"'{m.name}' has no Tessallite semi-additive equivalent "
                            "— imported as fully additive. Set it manually."
                        ),
                    ))

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
            warnings.append(make_import_warning(
                code="dbt.join_manual",
                params={"entity": entity.name, "model": sm.name},
                detail=(
                    f"Foreign entity '{entity.name}' in model '{sm.name}' "
                    "detected — join will need manual configuration in Tessallite"
                ),
            ))

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
            "include_all_measures": DEFAULT_INCLUDE_ALL_MEASURES,
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
    warnings: list[ImportWarningResponse],
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
                warnings.append(make_import_warning(
                    code="dbt.metric_reference_missing",
                    params={"metric": metric.name, "reference": str(measure_name)},
                    detail=(
                        f"Simple metric '{metric.name}' references unknown "
                        f"measure '{measure_name}'"
                    ),
                ))

        elif metric.metric_type == "derived":
            warnings.append(make_import_warning(
                code="dbt.metric_manual",
                params={"metric": metric.name, "metric_type": "derived"},
                detail=(
                    f"Derived metric '{metric.name}' imported as a note — "
                    "create a calculated measure manually if needed"
                ),
            ))

        elif metric.metric_type == "cumulative":
            warnings.append(make_import_warning(
                code="dbt.metric_manual",
                params={"metric": metric.name, "metric_type": "cumulative"},
                detail=(
                    f"Cumulative metric '{metric.name}' — Tessallite represents "
                    "time-variant measures differently; review after import"
                ),
            ))

        elif metric.metric_type in ("ratio", "conversion"):
            warnings.append(make_import_warning(
                code="dbt.metric_manual",
                params={"metric": metric.name, "metric_type": metric.metric_type},
                detail=(
                    f"{metric.metric_type.title()} metric '{metric.name}' "
                    "requires manual setup as a calculated measure"
                ),
            ))

        else:
            warnings.append(make_import_warning(
                code="dbt.metric_type_unknown",
                params={"metric": metric.name, "metric_type": metric.metric_type},
                detail=(
                    f"Unknown metric type '{metric.metric_type}' for '{metric.name}'"
                ),
            ))


def _apply_metric_filters(
    metrics: list[DbtMetric],
    models: list[dict[str, Any]],
    warnings: list[ImportWarningResponse],
) -> None:
    """Report dbt metric filters — never persist them as persona defaults.

    Bug-7301 [WRONG NUMBERS]: a dbt metric ``filter`` is scoped to that ONE
    metric (it constrains the rows that metric aggregates over). The previous
    implementation (F-020-16) wrote every parsed filter into EVERY persona's
    ``default_filters`` dict. Persona ``default_filters`` apply to the whole
    persona — to every measure and dimension it exposes — so a filter meant for
    a single metric silently changed the numbers for every other measure, for
    everyone using that persona. That is a wrong-numbers / data-scope defect,
    not a shape mismatch, so correcting the JSONB *shape* did not fix it.

    Tessallite's import model has no per-metric row-filter store, so there is
    nowhere correct to land a metric-scoped filter on import. Rather than
    mis-scope it to the persona, we persist NOTHING and surface each filter as a
    per-metric warning instructing the modeller to recreate it deliberately
    (e.g. as a calculated measure with an explicit predicate, or a scoped
    persona filter if that persona genuinely should be constrained). Fail loud,
    not silently wrong.

    ``models`` is intentionally left untouched — no persona ``default_filters``
    are written here.
    """
    _ = models  # deliberately not mutated (see docstring)
    described: list[str] = []

    for metric in metrics:
        if not metric.filter:
            continue
        described.append(f"{metric.name}: {metric.filter}")

    if described:
        # Name EVERY skipped metric filter (do not truncate) so the modeller can
        # recreate each one — a silently dropped filter is the wrong-numbers trap
        # this fix exists to close.
        warnings.append(make_import_warning(
            code="dbt.metric_filters_omitted",
            params={"count": len(described)},
            detail=(
                f"{len(described)} dbt metric filter(s) were NOT imported because a "
                "dbt metric filter is scoped to its own metric only; applying it "
                "as a persona-wide filter would change results for every other "
                "measure. Recreate each one manually (as a calculated measure "
                "predicate, or a scoped persona filter if intended): "
                + "; ".join(described)
            ),
        ))


def _apply_saved_queries(
    saved_queries: list[DbtSavedQuery],
    models: list[dict[str, Any]],
    warnings: list[ImportWarningResponse],
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
        warnings.append(make_import_warning(
            code="dbt.saved_query_manual",
            params={"saved_query": sq.name},
            detail=(
                f"{' | '.join(parts)} — "
                "create a Tessallite report template or saved view to replicate"
            ),
        ))


def _extract_model_ref(ref_str: str) -> str:
    match = re.match(r"ref\(['\"](\w+)['\"]\)", ref_str)
    if match:
        return match.group(1)
    return ref_str


def _slugify(name: str) -> str:
    # Bug-7622: delegate to the shared BI-safe generator so digit-leading and
    # symbol-only names produce a valid slug (fallback + leading-underscore +
    # 64-char bound) instead of a slug that later trips validate_bi_safe_slug
    # and raises an uncaught 500 in the import endpoint.
    return slugify(name, fallback="dbt_model", separator="_")


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
