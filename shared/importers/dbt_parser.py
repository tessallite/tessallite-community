"""Parse dbt v1.7+ semantic model YAML into intermediate structures.

Handles dbt's ``semantic_models`` and ``metrics`` top-level keys.
Produces a list of parsed semantic models and metric definitions that
dbt_mapper.py then converts to Tessallite project bundles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class DbtEntity:
    name: str
    entity_type: str  # primary, foreign, unique, natural
    expr: str | None = None


@dataclass
class DbtDimension:
    name: str
    dim_type: str  # categorical, time
    expr: str | None = None
    type_params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    label: str = ""


@dataclass
class DbtMeasure:
    name: str
    agg: str  # sum, count, count_distinct, average, min, max, median, percentile
    expr: str | None = None
    description: str = ""
    label: str = ""
    create_metric: bool = False
    non_additive_dimension: dict[str, Any] | None = None
    agg_time_dimension: str | None = None


@dataclass
class DbtSemanticModel:
    name: str
    model: str  # ref('model_name') or table reference
    description: str = ""
    label: str = ""
    entities: list[DbtEntity] = field(default_factory=list)
    dimensions: list[DbtDimension] = field(default_factory=list)
    measures: list[DbtMeasure] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)


@dataclass
class DbtMetric:
    name: str
    metric_type: str  # simple, derived, cumulative, ratio, conversion
    label: str = ""
    description: str = ""
    type_params: dict[str, Any] = field(default_factory=dict)
    filter: str | None = None


@dataclass
class DbtSavedQuery:
    name: str
    description: str = ""
    label: str = ""
    metrics: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    exports: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DbtParseResult:
    semantic_models: list[DbtSemanticModel] = field(default_factory=list)
    metrics: list[DbtMetric] = field(default_factory=list)
    saved_queries: list[DbtSavedQuery] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class DbtParseError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} parse error(s): {'; '.join(errors[:5])}")


def parse_dbt_yaml(content: str) -> DbtParseResult:
    doc = yaml.safe_load(content)
    if not isinstance(doc, dict):
        raise DbtParseError(["YAML root must be a mapping"])

    result = DbtParseResult()

    for sm_raw in doc.get("semantic_models", []):
        sm = _parse_semantic_model(sm_raw, result)
        if sm:
            result.semantic_models.append(sm)

    for m_raw in doc.get("metrics", []):
        metric = _parse_metric(m_raw, result)
        if metric:
            result.metrics.append(metric)

    for sq_raw in doc.get("saved_queries", []):
        sq = _parse_saved_query(sq_raw, result)
        if sq:
            result.saved_queries.append(sq)

    if not result.semantic_models and not result.metrics and not result.saved_queries:
        result.errors.append(
            "No semantic_models, metrics, or saved_queries found. "
            "Ensure the YAML uses dbt v1.7+ semantic layer format."
        )

    if result.errors:
        raise DbtParseError(result.errors)

    return result


def parse_dbt_project(files: dict[str, str]) -> DbtParseResult:
    combined = DbtParseResult()

    for filename, content in sorted(files.items()):
        if not filename.endswith((".yml", ".yaml")):
            continue
        try:
            doc = yaml.safe_load(content)
        except yaml.YAMLError:
            combined.warnings.append(f"Skipped {filename}: invalid YAML")
            continue

        if not isinstance(doc, dict):
            continue
        has_semantic = "semantic_models" in doc or "metrics" in doc or "saved_queries" in doc
        if not has_semantic:
            continue

        try:
            partial = parse_dbt_yaml(content)
        except DbtParseError as exc:
            combined.warnings.append(
                f"Skipped {filename}: {'; '.join(exc.errors[:3])}"
            )
            continue
        combined.semantic_models.extend(partial.semantic_models)
        combined.metrics.extend(partial.metrics)
        combined.saved_queries.extend(partial.saved_queries)
        combined.warnings.extend(partial.warnings)

    if not combined.semantic_models:
        combined.errors.append("No semantic_models found in any YAML file")
        raise DbtParseError(combined.errors)

    return combined


def _parse_semantic_model(
    raw: dict[str, Any], result: DbtParseResult
) -> DbtSemanticModel | None:
    name = raw.get("name", "")
    if not name:
        result.errors.append("semantic_model entry missing 'name'")
        return None

    model_ref = raw.get("model", "")
    if not model_ref:
        result.errors.append(f"semantic_model '{name}' missing 'model'")
        return None

    sm = DbtSemanticModel(
        name=name,
        model=model_ref,
        description=raw.get("description", ""),
        label=raw.get("label", ""),
        defaults=raw.get("defaults", {}),
    )

    for e in raw.get("entities", []):
        sm.entities.append(DbtEntity(
            name=e.get("name", ""),
            entity_type=e.get("type", "primary"),
            expr=e.get("expr"),
        ))

    for d in raw.get("dimensions", []):
        sm.dimensions.append(DbtDimension(
            name=d.get("name", ""),
            dim_type=d.get("type", "categorical"),
            expr=d.get("expr"),
            type_params=d.get("type_params", {}),
            description=d.get("description", ""),
            label=d.get("label", ""),
        ))

    for m in raw.get("measures", []):
        nad = m.get("non_additive_dimension")
        sm.measures.append(DbtMeasure(
            name=m.get("name", ""),
            agg=m.get("agg", "sum"),
            expr=m.get("expr"),
            description=m.get("description", ""),
            label=m.get("label", ""),
            create_metric=m.get("create_metric", False),
            non_additive_dimension=nad if isinstance(nad, dict) else None,
            agg_time_dimension=m.get("agg_time_dimension"),
        ))

    return sm


def _parse_metric(
    raw: dict[str, Any], result: DbtParseResult
) -> DbtMetric | None:
    name = raw.get("name", "")
    if not name:
        result.errors.append("metric entry missing 'name'")
        return None

    metric_type = raw.get("type", "")
    if not metric_type:
        result.errors.append(f"metric '{name}' missing 'type'")
        return None

    raw_filter = raw.get("filter")
    if isinstance(raw_filter, str):
        filter_str = raw_filter
    elif isinstance(raw_filter, list):
        filter_str = " AND ".join(
            f.get("where_sql_template", str(f)) if isinstance(f, dict) else str(f)
            for f in raw_filter
        )
    elif isinstance(raw_filter, dict):
        filter_str = raw_filter.get("where_sql_template", str(raw_filter))
    else:
        filter_str = None

    return DbtMetric(
        name=name,
        metric_type=metric_type,
        label=raw.get("label", name),
        description=raw.get("description", ""),
        type_params=raw.get("type_params", {}),
        filter=filter_str,
    )


def _parse_saved_query(
    raw: dict[str, Any], result: DbtParseResult
) -> DbtSavedQuery | None:
    name = raw.get("name", "")
    if not name:
        result.warnings.append("saved_query entry missing 'name' — skipped")
        return None

    query_params = raw.get("query_params", {})
    metrics = query_params.get("metrics", [])
    group_by_raw = query_params.get("group_by", [])
    group_by = [
        g if isinstance(g, str) else str(g) for g in group_by_raw
    ]
    where_raw = query_params.get("where", [])
    where = [
        w.get("where_sql_template", str(w)) if isinstance(w, dict) else str(w)
        for w in (where_raw if isinstance(where_raw, list) else [where_raw])
    ] if where_raw else []

    exports = raw.get("exports", [])
    if exports:
        result.warnings.append(
            f"saved_query '{name}' has {len(exports)} export(s) — "
            f"Tessallite does not import dbt export configs"
        )

    return DbtSavedQuery(
        name=name,
        description=raw.get("description", ""),
        label=raw.get("label", ""),
        metrics=metrics,
        group_by=group_by,
        where=where,
        exports=exports if isinstance(exports, list) else [],
    )
