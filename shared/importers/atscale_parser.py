"""Parse AtScale SML (Semantic Modeling Language) projects into intermediate structures.

SML uses a multi-file YAML layout:
    catalog.yml (or atscale.yml)  — project root
    models/                       — one YAML per model (relationships + metric refs)
    dimensions/                   — dimension definitions with hierarchies + levels
    metrics/                      — simple metrics (calculation_method + column)
    calculations/                 — MDX/calculated metrics
    datasets/                     — table/column definitions
    connections/                  — connection configs (ignored)

Produces a list of parsed structures that atscale_mapper.py then converts to
Tessallite project bundles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SmlColumn:
    name: str
    data_type: str = ""
    sql: str | None = None


@dataclass
class SmlDataset:
    unique_name: str
    label: str = ""
    table: str = ""
    sql: str | None = None
    connection_id: str = ""
    description: str = ""
    columns: list[SmlColumn] = field(default_factory=list)


@dataclass
class SmlLevel:
    unique_name: str
    label: str = ""
    dataset: str = ""
    name_column: str = ""
    key_columns: list[str] = field(default_factory=list)
    sort_column: str = ""
    time_unit: str = ""
    is_unique_key: bool = False


@dataclass
class SmlHierarchy:
    unique_name: str
    label: str = ""
    levels: list[str] = field(default_factory=list)


@dataclass
class SmlDimension:
    unique_name: str
    label: str = ""
    description: str = ""
    dim_type: str = "standard"
    is_degenerate: bool = False
    hierarchies: list[SmlHierarchy] = field(default_factory=list)
    level_attributes: list[SmlLevel] = field(default_factory=list)


@dataclass
class SmlRelationship:
    unique_name: str = ""
    from_dataset: str = ""
    from_join_columns: list[str] = field(default_factory=list)
    to_dimension: str = ""
    to_level: str = ""
    role_play: str = ""


@dataclass
class SmlSemiAdditive:
    position: str = ""  # first, last, first_child, last_child


@dataclass
class SmlMetric:
    unique_name: str
    label: str = ""
    description: str = ""
    calculation_method: str = ""
    dataset: str = ""
    column: str = ""
    format: str = ""
    folder: str = ""
    is_hidden: bool = False
    semi_additive: SmlSemiAdditive | None = None
    unrelated_dimensions_handling: str = ""


@dataclass
class SmlCalculation:
    unique_name: str
    label: str = ""
    description: str = ""
    expression: str = ""
    format: str = ""
    is_hidden: bool = False
    mdx_aggregation_function: str = ""


@dataclass
class SmlRowSecurity:
    unique_name: str
    label: str = ""
    dimension: str = ""
    attribute: str = ""


@dataclass
class SmlConnection:
    unique_name: str
    label: str = ""
    schema: str = ""
    database: str = ""


@dataclass
class SmlModel:
    unique_name: str
    label: str = ""
    description: str = ""
    relationships: list[SmlRelationship] = field(default_factory=list)
    metric_refs: list[str] = field(default_factory=list)
    dimension_refs: list[str] = field(default_factory=list)


@dataclass
class SmlCatalog:
    unique_name: str
    label: str = ""


@dataclass
class SmlParseResult:
    catalog: SmlCatalog | None = None
    models: list[SmlModel] = field(default_factory=list)
    dimensions: list[SmlDimension] = field(default_factory=list)
    datasets: list[SmlDataset] = field(default_factory=list)
    metrics: list[SmlMetric] = field(default_factory=list)
    calculations: list[SmlCalculation] = field(default_factory=list)
    connections: list[SmlConnection] = field(default_factory=list)
    row_security_rules: list[SmlRowSecurity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class SmlParseError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} parse error(s): {'; '.join(errors[:5])}")


def parse_sml_project(files: dict[str, str]) -> SmlParseResult:
    """Parse a dict of {relative_path: yaml_content} representing an SML project."""
    result = SmlParseResult()

    for filepath, content in sorted(files.items()):
        if not filepath.endswith((".yml", ".yaml")):
            continue
        # Skip combined/ convenience aggregation files that duplicate
        # objects already present in individual files.
        parts = filepath.replace("\\", "/").split("/")
        if "combined" in parts:
            continue
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            result.warnings.append(f"Skipped {filepath}: invalid YAML")
            continue

        for doc in docs:
            if not isinstance(doc, dict):
                continue

            obj_type = doc.get("object_type", "")
            if not obj_type:
                if "unique_name" in doc and filepath.endswith(("atscale.yml", "catalog.yml")):
                    obj_type = "catalog"
                else:
                    continue

            _dispatch_object(doc, obj_type, filepath, result)

    # Deduplicate models by unique_name — LLM-generated SML files sometimes
    # contain repeated model documents (multi-doc YAML or duplicate files).
    if len(result.models) > 1:
        seen: dict[str, int] = {}
        deduped: list[SmlModel] = []
        for m in result.models:
            if m.unique_name in seen:
                result.warnings.append(
                    f"Duplicate model '{m.unique_name}' — keeping first occurrence"
                )
            else:
                seen[m.unique_name] = len(deduped)
                deduped.append(m)
        result.models = deduped

    if not result.models and not result.metrics:
        result.errors.append("No models or metrics found in SML project")
        raise SmlParseError(result.errors)

    if result.errors:
        raise SmlParseError(result.errors)

    return result


def parse_sml_directory(root: Path) -> SmlParseResult:
    """Parse an SML project from a filesystem directory."""
    files: dict[str, str] = {}
    for f in root.rglob("*.yml"):
        rel = str(f.relative_to(root)).replace("\\", "/")
        files[rel] = f.read_text(encoding="utf-8")
    for f in root.rglob("*.yaml"):
        rel = str(f.relative_to(root)).replace("\\", "/")
        if rel not in files:
            files[rel] = f.read_text(encoding="utf-8")
    return parse_sml_project(files)


def _dispatch_object(
    doc: dict[str, Any], obj_type: str, filepath: str, result: SmlParseResult
) -> None:
    if obj_type == "catalog":
        result.catalog = SmlCatalog(
            unique_name=doc.get("unique_name", ""),
            label=doc.get("label", ""),
        )
    elif obj_type == "model":
        _parse_model(doc, result)
    elif obj_type == "dimension":
        _parse_dimension(doc, result)
    elif obj_type == "dataset":
        _parse_dataset(doc, result)
    elif obj_type == "metric":
        _parse_metric(doc, result)
    elif obj_type == "metric_calc":
        _parse_calculation(doc, result)
    elif obj_type == "connection":
        result.connections.append(SmlConnection(
            unique_name=doc.get("unique_name", ""),
            label=doc.get("label", ""),
            schema=doc.get("schema", ""),
            database=doc.get("database", ""),
        ))
    elif obj_type == "row_security":
        _parse_row_security(doc, result)
    elif obj_type in ("composite_model", "package"):
        result.warnings.append(
            f"Object type '{obj_type}' in {filepath} is not imported — "
            f"review manually after import"
        )
    else:
        result.warnings.append(f"Unknown object_type '{obj_type}' in {filepath}")


def _parse_model(doc: dict[str, Any], result: SmlParseResult) -> None:
    name = doc.get("unique_name", "")
    if not name:
        result.errors.append("model missing 'unique_name'")
        return

    relationships: list[SmlRelationship] = []
    for rel in doc.get("relationships", []):
        from_block = rel.get("from", {})
        to_block = rel.get("to", {})
        relationships.append(SmlRelationship(
            unique_name=rel.get("unique_name", ""),
            from_dataset=from_block.get("dataset", ""),
            from_join_columns=from_block.get("join_columns", []),
            to_dimension=to_block.get("dimension", ""),
            to_level=to_block.get("level", ""),
            role_play=rel.get("role_play", ""),
        ))

    metric_refs: list[str] = []
    for m in doc.get("metrics", []):
        if isinstance(m, dict):
            metric_refs.append(m.get("unique_name", ""))
        elif isinstance(m, str):
            metric_refs.append(m)

    dimension_refs: list[str] = []
    for d in doc.get("dimensions", []):
        if isinstance(d, dict):
            dimension_refs.append(d.get("unique_name", ""))
        elif isinstance(d, str):
            dimension_refs.append(d)

    result.models.append(SmlModel(
        unique_name=name,
        label=doc.get("label", ""),
        description=doc.get("description", ""),
        relationships=relationships,
        metric_refs=metric_refs,
        dimension_refs=dimension_refs,
    ))


def _parse_dimension(doc: dict[str, Any], result: SmlParseResult) -> None:
    name = doc.get("unique_name", "")
    if not name:
        result.errors.append("dimension missing 'unique_name'")
        return

    hierarchies: list[SmlHierarchy] = []
    for h in doc.get("hierarchies", []):
        levels = []
        for lvl in h.get("levels", []):
            if isinstance(lvl, dict):
                levels.append(lvl.get("unique_name", ""))
            elif isinstance(lvl, str):
                levels.append(lvl)
        hierarchies.append(SmlHierarchy(
            unique_name=h.get("unique_name", ""),
            label=h.get("label", ""),
            levels=levels,
        ))

    level_attributes: list[SmlLevel] = []
    for la in doc.get("level_attributes", []):
        level_attributes.append(SmlLevel(
            unique_name=la.get("unique_name", ""),
            label=la.get("label", ""),
            dataset=la.get("dataset", ""),
            name_column=la.get("name_column", ""),
            key_columns=la.get("key_columns", []),
            sort_column=la.get("sort_column", ""),
            time_unit=la.get("time_unit", ""),
            is_unique_key=la.get("is_unique_key", False),
        ))

    result.dimensions.append(SmlDimension(
        unique_name=name,
        label=doc.get("label", ""),
        description=doc.get("description", ""),
        dim_type=doc.get("type", "standard"),
        is_degenerate=doc.get("is_degenerate", False),
        hierarchies=hierarchies,
        level_attributes=level_attributes,
    ))


def _parse_dataset(doc: dict[str, Any], result: SmlParseResult) -> None:
    name = doc.get("unique_name", "")
    if not name:
        result.errors.append("dataset missing 'unique_name'")
        return

    columns: list[SmlColumn] = []
    for col in doc.get("columns", []):
        columns.append(SmlColumn(
            name=col.get("name", ""),
            data_type=col.get("data_type", ""),
            sql=col.get("sql"),
        ))

    result.datasets.append(SmlDataset(
        unique_name=name,
        label=doc.get("label", ""),
        table=doc.get("table", ""),
        sql=doc.get("sql"),
        connection_id=doc.get("connection_id", ""),
        description=doc.get("description", ""),
        columns=columns,
    ))


def _parse_metric(doc: dict[str, Any], result: SmlParseResult) -> None:
    name = doc.get("unique_name", "")
    if not name:
        result.errors.append("metric missing 'unique_name'")
        return

    semi_additive = None
    sa_raw = doc.get("semi_additive")
    if isinstance(sa_raw, dict):
        semi_additive = SmlSemiAdditive(position=sa_raw.get("position", ""))

    result.metrics.append(SmlMetric(
        unique_name=name,
        label=doc.get("label", ""),
        description=doc.get("description", ""),
        calculation_method=doc.get("calculation_method", ""),
        dataset=doc.get("dataset", ""),
        column=doc.get("column", ""),
        format=doc.get("format", ""),
        folder=doc.get("folder", ""),
        is_hidden=doc.get("is_hidden", False),
        semi_additive=semi_additive,
        unrelated_dimensions_handling=doc.get("unrelated_dimensions_handling", ""),
    ))


def _parse_row_security(doc: dict[str, Any], result: SmlParseResult) -> None:
    name = doc.get("unique_name", "")
    if not name:
        result.warnings.append("row_security entry missing 'unique_name' — skipped")
        return
    result.row_security_rules.append(SmlRowSecurity(
        unique_name=name,
        label=doc.get("label", ""),
        dimension=doc.get("dimension", ""),
        attribute=doc.get("attribute", ""),
    ))
    result.warnings.append(
        f"Row security rule '{name}' found — imported model requires manual "
        f"security configuration in Tessallite before use"
    )


def _parse_calculation(doc: dict[str, Any], result: SmlParseResult) -> None:
    name = doc.get("unique_name", "")
    if not name:
        result.errors.append("calculation missing 'unique_name'")
        return

    result.calculations.append(SmlCalculation(
        unique_name=name,
        label=doc.get("label", ""),
        description=doc.get("description", ""),
        expression=doc.get("expression", ""),
        format=doc.get("format", ""),
        is_hidden=doc.get("is_hidden", False),
        mdx_aggregation_function=doc.get("mdx_aggregation_function", ""),
    ))
