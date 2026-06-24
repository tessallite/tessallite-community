"""Map parsed AtScale SML project to Tessallite project bundle format.

Converts SmlParseResult into the same JSON structure that
parse_model_yaml / parse_project_yaml produce, so the existing
project_rehydrator can consume it directly.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from shared.importers.atscale_parser import (
    SmlCalculation,
    SmlDataset,
    SmlDimension,
    SmlMetric,
    SmlModel,
    SmlParseResult,
    SmlRelationship,
)


_CALC_METHOD_MAP: dict[str, str] = {
    "sum": "sum",
    "sum distinct": "sum",
    "count": "count",
    "count distinct": "count_distinct",
    "count non-null": "count",
    "estimated count distinct": "count_distinct",
    "count_distinct": "count_distinct",
    "average": "average",
    "avg": "average",
    "minimum": "min",
    "min": "min",
    "maximum": "max",
    "max": "max",
    "median": "median",
    "percentile": "percentile",
}

# F-020-08: statistical aggregations Tessallite cannot represent. They are
# deliberately NOT in _CALC_METHOD_MAP so they fall to the warning path and
# import as disabled measures (default_agg=None) rather than silently mapping
# to SUM, which produced confidently wrong business numbers (e.g. stddev of
# 10,20,30 → 8.16 expected, SUM → 60).
_UNREPRESENTABLE_METHODS: frozenset[str] = frozenset({
    "stddev_pop", "stddev_samp", "var_pop", "var_samp",
})

# AtScale semi-additive position → valid Tessallite semi_additive_behavior
# enum (F-020-09). The previous code wrote f"{position}_value" (e.g.
# "last_value"), an invalid enum the rewriter could not interpret.
_SEMI_ADDITIVE_POSITION_MAP: dict[str, str] = {
    "last": "last_non_empty",
    "first": "first_non_empty",
    "min": "min",
    "max": "max",
    "average": "avg_of_children",
}


@dataclass
class MapResult:
    bundle: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def map_atscale_to_tessallite(
    parsed: SmlParseResult,
    project_name: str = "atscale-import",
    project_display_name: str = "AtScale Import",
) -> MapResult:
    warnings: list[str] = list(parsed.warnings)
    gen = _id_gen()

    dataset_map = {ds.unique_name: ds for ds in parsed.datasets}
    dimension_map = {d.unique_name: d for d in parsed.dimensions}
    metric_map = {m.unique_name: m for m in parsed.metrics}
    calc_map = {c.unique_name: c for c in parsed.calculations}
    conn_map = {c.unique_name: c for c in parsed.connections}

    # Resolve default schema from the first SML connection referenced by datasets.
    default_schema = ""
    for ds in parsed.datasets:
        if ds.connection_id and ds.connection_id in conn_map:
            schema = conn_map[ds.connection_id].schema
            if schema:
                default_schema = schema
                break

    models_out: list[dict[str, Any]] = []

    for sml_model in parsed.models:
        snap = _map_model(
            sml_model, dataset_map, dimension_map, metric_map, calc_map,
            gen, warnings, default_schema=default_schema,
        )
        models_out.append(snap)

    if not models_out and parsed.metrics:
        snap = _map_standalone_metrics(
            parsed.metrics, parsed.calculations, dataset_map, gen, warnings,
            default_schema=default_schema,
        )
        models_out.append(snap)

    bundle: dict[str, Any] = {
        "schema_version": 1,
        "export_format": "tessallite-project/v1",
        "project": {
            "slug": _slugify(project_name),
            "display_name": project_display_name,
        },
        "connections": [],
        "models": models_out,
    }

    return MapResult(bundle=bundle, warnings=warnings)


def _map_model(
    sml_model: SmlModel,
    dataset_map: dict[str, SmlDataset],
    dimension_map: dict[str, SmlDimension],
    metric_map: dict[str, SmlMetric],
    calc_map: dict[str, SmlCalculation],
    gen, warnings: list[str],
    default_schema: str = "",
) -> dict[str, Any]:
    model_id = gen()
    source_id = gen()

    resolved_metrics = [
        metric_map[ref] for ref in sml_model.metric_refs
        if ref in metric_map
    ]
    fact_datasets = _identify_fact_datasets(sml_model.relationships, resolved_metrics)
    tables_out: list[dict[str, Any]] = []
    columns_out: list[dict[str, Any]] = []
    table_id_map: dict[str, str] = {}
    col_id_map: dict[tuple[str, str], str] = {}

    for ds_name in fact_datasets:
        ds = dataset_map.get(ds_name)
        if not ds:
            warnings.append(f"Dataset '{ds_name}' referenced but not found")
            continue
        table_id = gen()
        table_id_map[ds_name] = table_id
        tables_out.append(_make_table_dict(
            table_id, model_id, source_id, ds, "fact",
        ))
        for col in ds.columns:
            cid = gen()
            col_id_map[(ds_name, col.name)] = cid
            columns_out.append({
                "id": cid,
                "model_table_id": table_id,
                "column_name": col.name,
                "data_type": _normalize_data_type(col.data_type),
            })

    dims_out: list[dict[str, Any]] = []
    hier_out: list[dict[str, Any]] = []
    used_dims = _get_all_dimensions(sml_model.relationships, sml_model.dimension_refs)
    for dim_name in used_dims:
        dim = dimension_map.get(dim_name)
        if not dim:
            continue

        _import_dimension_datasets(
            dim, dataset_map, model_id, source_id, gen,
            tables_out, columns_out, table_id_map, col_id_map, warnings,
        )

        first_level = dim.level_attributes[0] if dim.level_attributes else None
        dim_source_col = None
        if first_level and first_level.name_column:
            dim_source_col = col_id_map.get((first_level.dataset, first_level.name_column))

        dim_id = gen()
        dims_out.append({
            "id": dim_id,
            "model_id": model_id,
            "name": dim.unique_name,
            "display_name": dim.label or dim.unique_name,
            "description": dim.description or None,
            "source_column_id": dim_source_col,
            "is_time_dim": dim.dim_type == "time",
        })
        la_map = {la.unique_name: la for la in dim.level_attributes}
        for hier in dim.hierarchies:
            h_id = gen()
            levels_out = []
            skipped_levels = []
            for i, lvl in enumerate(hier.levels):
                la = la_map.get(lvl)
                lvl_key_col = None
                lvl_name_col = None
                if la:
                    if la.key_columns:
                        lvl_key_col = col_id_map.get((la.dataset, la.key_columns[0]))
                    if la.name_column:
                        lvl_name_col = col_id_map.get((la.dataset, la.name_column))
                if lvl_key_col is None:
                    skipped_levels.append(lvl)
                    continue
                lvl_entry: dict[str, Any] = {
                    "id": gen(),
                    "hierarchy_id": h_id,
                    "name": lvl,
                    "ordinal": len(levels_out),
                    "key_attribute_id": lvl_key_col,
                    "key_attribute_source": "physical_column",
                    "time_unit": la.time_unit if la else None,
                }
                if lvl_name_col and lvl_name_col != lvl_key_col:
                    lvl_entry["attributes"] = [{
                        "id": gen(),
                        "level_id": lvl_entry["id"],
                        "attribute_id": lvl_name_col,
                        "attribute_source": "physical_column",
                        "role": "display",
                    }]
                levels_out.append(lvl_entry)
            if skipped_levels:
                warnings.append(
                    f"Hierarchy '{hier.unique_name or dim.unique_name}' in dimension "
                    f"'{dim.unique_name}': skipped levels {skipped_levels} — "
                    f"source column could not be resolved"
                )
            if not levels_out:
                continue
            hier_out.append({
                "id": h_id,
                "model_id": model_id,
                "name": hier.unique_name or dim.unique_name,
                "type": "explicit",
                "dimension_kind": "time" if dim.dim_type == "time" else None,
                "calendar_type": "standard" if dim.dim_type == "time" else None,
                "levels": levels_out,
            })

    measures_out: list[dict[str, Any]] = []
    for ref_name in sml_model.metric_refs:
        metric = metric_map.get(ref_name)
        calc = calc_map.get(ref_name)
        if metric:
            agg, invalid_reason = _resolve_calc_method(
                metric.calculation_method, metric.unique_name, warnings,
            )
            semi_additive = _resolve_semi_additive(
                metric.semi_additive, metric.unique_name, warnings,
            )
            source_col_id = col_id_map.get((metric.dataset, metric.column))
            measures_out.append({
                "id": gen(),
                "model_id": model_id,
                "name": metric.unique_name,
                "display_name": metric.label or metric.unique_name,
                "description": metric.description or None,
                "source_column_id": source_col_id,
                "measure_type": "standard",
                "default_agg": agg,
                "is_invalid": invalid_reason is not None,
                "invalid_reason": invalid_reason,
                "format": metric.format or None,
                "semi_additive_behavior": semi_additive,
            })
        elif calc:
            warnings.append(
                f"Calculated metric '{calc.unique_name}' uses MDX expression — "
                f"create a calculated measure manually in Tessallite"
            )
            measures_out.append({
                "id": gen(),
                "model_id": model_id,
                "name": calc.unique_name,
                "display_name": calc.label or calc.unique_name,
                "description": f"MDX: {calc.expression[:100]}",
                "source_column_id": None,
                "measure_type": "calculated",
                "default_agg": "sum",
                "format": calc.format or None,
                "semi_additive_behavior": None,
            })
        else:
            warnings.append(f"Metric ref '{ref_name}' not found in project")

    joins_out: list[dict[str, Any]] = []
    for rel in sml_model.relationships:
        if not rel.to_dimension or not rel.from_join_columns:
            continue

        left_tid = table_id_map.get(rel.from_dataset)
        left_cid = col_id_map.get((rel.from_dataset, rel.from_join_columns[0]))

        # Resolve right side via the dimension's level_attributes
        right_tid = None
        right_cid = None
        dim = dimension_map.get(rel.to_dimension)
        if dim:
            target_la = None
            if rel.to_level:
                for la in dim.level_attributes:
                    if la.unique_name == rel.to_level:
                        target_la = la
                        break
            if not target_la and dim.level_attributes:
                target_la = dim.level_attributes[0]
            if target_la and target_la.dataset and target_la.key_columns:
                right_tid = table_id_map.get(target_la.dataset)
                right_cid = col_id_map.get(
                    (target_la.dataset, target_la.key_columns[0])
                )

        if not all([left_tid, left_cid, right_tid, right_cid]):
            warnings.append(
                f"Join '{rel.unique_name}' skipped — could not resolve "
                f"table/column IDs for {rel.from_dataset} -> {rel.to_dimension}"
            )
            continue

        joins_out.append({
            "id": gen(),
            "model_id": model_id,
            "left_table_id": left_tid,
            "left_column_id": left_cid,
            "right_table_id": right_tid,
            "right_column_id": right_cid,
            "join_type": "many_to_one",
        })

    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "model_id": model_id,
        "model": {
            "id": model_id,
            "slug": _slugify(sml_model.unique_name),
            "display_name": sml_model.label or sml_model.unique_name,
            "description": None,
            "refresh_strategy": "manual",
            "max_aggregates": 20,
            "aggregations_enabled": True,
            "include_all_measures": True,
        },
        "tables": tables_out,
        "columns": columns_out,
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
        "user_defined_attributes": [],
        "uda_column_refs": [],
        "aggregates": [],
        "data_sources": [{
            "id": source_id,
            "model_id": model_id,
            "source_type": "postgresql",
            "display_name": "AtScale Import Source",
            "default_schema": default_schema or None,
            "config": {},
        }],
        "data_targets": [],
    }
    return snapshot


def _map_standalone_metrics(
    metrics: list[SmlMetric],
    calculations: list[SmlCalculation],
    dataset_map: dict[str, SmlDataset],
    gen, warnings: list[str],
    default_schema: str = "",
) -> dict[str, Any]:
    """Fallback: if no model file exists, create a single model from metrics."""
    model_id = gen()
    source_id = gen()

    tables_out: list[dict[str, Any]] = []
    columns_out: list[dict[str, Any]] = []
    table_id_map: dict[str, str] = {}
    col_id_map: dict[tuple[str, str], str] = {}

    metric_datasets = _identify_fact_datasets([], metrics)
    for ds_name in metric_datasets:
        ds = dataset_map.get(ds_name)
        if not ds:
            warnings.append(f"Dataset '{ds_name}' referenced by metric but not found")
            continue
        table_id = gen()
        table_id_map[ds_name] = table_id
        tables_out.append(_make_table_dict(
            table_id, model_id, source_id, ds, "fact",
        ))
        for col in ds.columns:
            cid = gen()
            col_id_map[(ds_name, col.name)] = cid
            columns_out.append({
                "id": cid,
                "model_table_id": table_id,
                "column_name": col.name,
                "data_type": _normalize_data_type(col.data_type),
            })

    measures_out = []
    for metric in metrics:
        agg, invalid_reason = _resolve_calc_method(
            metric.calculation_method, metric.unique_name, warnings,
        )
        semi_additive = _resolve_semi_additive(
            metric.semi_additive, metric.unique_name, warnings,
        )
        source_col_id = col_id_map.get((metric.dataset, metric.column))
        measures_out.append({
            "id": gen(),
            "model_id": model_id,
            "name": metric.unique_name,
            "display_name": metric.label or metric.unique_name,
            "description": None,
            "source_column_id": source_col_id,
            "measure_type": "standard",
            "default_agg": agg,
            "is_invalid": invalid_reason is not None,
            "invalid_reason": invalid_reason,
            "format": metric.format or None,
            "semi_additive_behavior": semi_additive,
        })
    for calc in calculations:
        warnings.append(
            f"Calculated metric '{calc.unique_name}' uses MDX — manual setup needed"
        )
    return {
        "schema_version": 2,
        "model_id": model_id,
        "model": {
            "id": model_id,
            "slug": "atscale_model",
            "display_name": "AtScale Model",
            "description": None,
            "refresh_strategy": "manual",
            "max_aggregates": 20,
            "aggregations_enabled": True,
            "include_all_measures": True,
        },
        "tables": tables_out,
        "columns": columns_out,
        "joins": [],
        "dimensions": [],
        "measures": measures_out,
        "hierarchies": [],
        "personas": [{
            "id": gen(),
            "model_id": model_id,
            "slug": "everyone",
            "name": "Everyone",
            "description": "Default persona — full access",
        }],
        "user_defined_attributes": [],
        "uda_column_refs": [],
        "aggregates": [],
        "data_sources": [{
            "id": source_id,
            "model_id": model_id,
            "source_type": "postgresql",
            "display_name": "AtScale Import Source",
            "default_schema": default_schema or None,
            "config": {},
        }],
        "data_targets": [],
    }


def _resolve_calc_method(
    method: str | None, metric_name: str, warnings: list[str],
) -> tuple[str, str | None]:
    """Resolve an AtScale calculation_method to a Tessallite agg.

    Returns ``(default_agg, invalid_reason)``. ``invalid_reason`` is non-None
    when the method cannot be represented (statistical aggregations, or any
    unknown method): the measure is then imported as invalid/disabled with a
    warning rather than silently mapped to SUM (F-020-08).
    """
    raw = (method or "").lower().strip()
    if raw in _UNREPRESENTABLE_METHODS:
        reason = (
            f"AtScale metric '{metric_name}' uses '{method}', which "
            f"Tessallite cannot compute — imported as a disabled measure. "
            f"Recreate it as a calculated measure if needed."
        )
        warnings.append(reason)
        return "sum", reason
    if raw in _CALC_METHOD_MAP:
        return _CALC_METHOD_MAP[raw], None
    reason = (
        f"Unsupported calculation_method '{method}' for metric "
        f"'{metric_name}' — imported as a disabled measure (defaulted to "
        f"'sum'). Review and re-enable after import."
    )
    warnings.append(reason)
    return "sum", reason


def _resolve_semi_additive(
    semi_additive: Any, metric_name: str, warnings: list[str],
) -> str | None:
    """Map an AtScale semi-additive position to a valid Tessallite enum.

    F-020-09: the previous code emitted f"{position}_value" (e.g.
    "last_value"), an invalid ``semi_additive_behavior`` enum that the
    rewriter could not interpret. Unknown positions warn and import as null.
    """
    if not semi_additive:
        return None
    position = (getattr(semi_additive, "position", "") or "").lower().strip()
    mapped = _SEMI_ADDITIVE_POSITION_MAP.get(position)
    if mapped is None:
        warnings.append(
            f"Semi-additive position '{position}' on metric "
            f"'{metric_name}' has no Tessallite equivalent — imported as "
            f"fully additive. Set the semi-additive behaviour manually."
        )
        return None
    return mapped


def _identify_fact_datasets(
    relationships: list[SmlRelationship],
    metrics: list[SmlMetric] | None = None,
) -> list[str]:
    """Extract unique dataset names from relationships and metric bindings."""
    seen: set[str] = set()
    result: list[str] = []
    for rel in relationships:
        if rel.from_dataset and rel.from_dataset not in seen:
            seen.add(rel.from_dataset)
            result.append(rel.from_dataset)
    for metric in (metrics or []):
        if metric.dataset and metric.dataset not in seen:
            seen.add(metric.dataset)
            result.append(metric.dataset)
    return result


def _get_all_dimensions(
    relationships: list[SmlRelationship],
    dimension_refs: list[str] | None = None,
) -> list[str]:
    """Extract unique dimension names from relationships and model dimension_refs.

    Degenerate dimensions (e.g. transaction detail attributes) appear in the
    model's dimension_refs but have no relationship entry.  Including them
    ensures their datasets/columns are imported so hierarchy levels resolve.
    """
    seen: set[str] = set()
    result: list[str] = []
    for rel in relationships:
        if rel.to_dimension and rel.to_dimension not in seen:
            seen.add(rel.to_dimension)
            result.append(rel.to_dimension)
    for ref in (dimension_refs or []):
        if ref and ref not in seen:
            seen.add(ref)
            result.append(ref)
    return result


def _make_table_dict(
    table_id: str, model_id: str, source_id: str,
    ds: SmlDataset, table_type: str,
) -> dict[str, Any]:
    """Build a table dict matching the ModelTable ORM column names."""
    physical = ds.table or ds.unique_name
    return {
        "id": table_id,
        "model_id": model_id,
        "source_id": source_id,
        "physical_name": physical,
        "alias": physical,
        "display_name": ds.label or physical,
        "description": ds.description or None,
        "table_type": table_type,
    }


def _import_dimension_datasets(
    dim: SmlDimension,
    dataset_map: dict[str, SmlDataset],
    model_id: str,
    source_id: str,
    gen,
    tables_out: list[dict[str, Any]],
    columns_out: list[dict[str, Any]],
    table_id_map: dict[str, str],
    col_id_map: dict[tuple[str, str], str],
    warnings: list[str],
) -> None:
    for la in dim.level_attributes:
        ds_name = la.dataset
        if not ds_name or ds_name in table_id_map:
            continue
        ds = dataset_map.get(ds_name)
        if not ds:
            warnings.append(f"Dimension dataset '{ds_name}' referenced but not found")
            continue
        table_id = gen()
        table_id_map[ds_name] = table_id
        tables_out.append(_make_table_dict(
            table_id, model_id, source_id, ds, "dim_detail",
        ))
        for col in ds.columns:
            cid = gen()
            col_id_map[(ds_name, col.name)] = cid
            columns_out.append({
                "id": cid,
                "model_table_id": table_id,
                "column_name": col.name,
                "data_type": _normalize_data_type(col.data_type),
            })


def _find_level_label(dim: SmlDimension, level_name: str) -> str:
    for la in dim.level_attributes:
        if la.unique_name == level_name:
            return la.label or level_name
    return level_name


def _normalize_data_type(dt: str) -> str:
    dt_lower = dt.lower().strip()
    if "decimal" in dt_lower or "numeric" in dt_lower or "float" in dt_lower or "double" in dt_lower:
        return "numeric"
    if "int" in dt_lower or "long" in dt_lower:
        return "integer"
    if "date" in dt_lower or "time" in dt_lower:
        return "timestamp"
    if "bool" in dt_lower:
        return "boolean"
    return "string"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower().strip())
    return slug.strip("_")


def _id_gen():
    def _next() -> str:
        return str(uuid.uuid4())
    return _next
