"""Map parsed Cube.dev models to Tessallite project bundle format.

Converts CubeParseResult into the same JSON structure that
parse_model_yaml / parse_project_yaml produce, so the existing
project_rehydrator can consume it directly.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from shared.importers.cube_parser import (
    CubeDefinition,
    CubeParseResult,
)
from shared.importers.import_warnings import (
    ImportWarningResponse,
    extend_known_import_warnings,
    make_import_warning,
)
from shared.model_snapshot.slug_utils import slugify


_MEASURE_TYPE_MAP: dict[str, str] = {
    "count": "count",
    "count_distinct": "count_distinct",
    "count_distinct_approx": "count_distinct",
    "sum": "sum",
    # Bug-6591: canonical token is "avg" (VALID_DEFAULT_AGGS), not "average".
    # "average" saved cleanly but failed late at query time (no AVERAGE() SQL
    # function); the runtime renderer only understands the canonical set.
    "avg": "avg",
    "min": "min",
    "max": "max",
}

# F-020-08: Cube measure types Tessallite cannot represent faithfully. They
# are kept OUT of _MEASURE_TYPE_MAP so they fall to the warning path and
# import as disabled measures instead of silently collapsing to SUM:
#   - running_total: a cumulative series, not a SUM of the slice;
#   - number: an arbitrary SQL expression over the cube, not a column agg.
# The coverage matrix documents these as "Warning"; previously no warning
# fired and the queried result was a confidently wrong flat SUM.
_UNREPRESENTABLE_MEASURE_TYPES: dict[str, str] = {
    "running_total": (
        "Cube measure '%s' is a running_total (cumulative series) — "
        "Tessallite cannot compute it as a column aggregation. Imported as "
        "a disabled measure; recreate it as a time-variant calculated measure."
    ),
    "number": (
        "Cube measure '%s' is a calculated 'number' type — Tessallite cannot "
        "bind it to a single column aggregation. Imported as a disabled "
        "measure; recreate it as a calculated measure."
    ),
}

_DIM_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "number": "numeric",
    "time": "timestamp",
    "boolean": "boolean",
    "geo": "string",
}

_SIMPLE_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
_CUBE_MACRO_RE = re.compile(r"^\{[a-zA-Z_][a-zA-Z0-9_]*\}\.([a-zA-Z_][a-zA-Z0-9_]*)$")


@dataclass
class MapResult:
    bundle: dict[str, Any]
    warnings: list[ImportWarningResponse] = field(default_factory=list)


def map_cube_to_tessallite(
    parsed: CubeParseResult,
    project_name: str = "cube-import",
    project_display_name: str = "Cube Import",
) -> MapResult:
    warnings: list[ImportWarningResponse] = []
    extend_known_import_warnings(warnings, parsed.warnings)

    models: list[dict[str, Any]] = []
    for cube in parsed.cubes:
        if not cube.public:
            warnings.append(make_import_warning(
                code="cube.cube_skipped",
                params={"cube": cube.name, "reason": "private"},
                detail=f"Cube '{cube.name}' is not public — skipped",
            ))
            continue
        snap = _map_cube(cube, warnings)
        models.append(snap)

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


def _map_cube(
    cube: CubeDefinition,
    warnings: list[ImportWarningResponse],
) -> dict[str, Any]:
    gen = _id_gen()
    model_id = gen()
    source_id = gen()

    table_ref = cube.sql_table or cube.name
    table_id = gen()
    tables = [{
        "id": table_id,
        "model_id": model_id,
        "source_id": source_id,
        "physical_name": table_ref,
        "alias": table_ref,
        "display_name": table_ref,
        "description": cube.description or "",
        "table_type": "fact",
    }]

    columns: list[dict[str, Any]] = []
    col_map: dict[str, str] = {}
    phys_col_seen: set[str] = set()

    for dim in cube.dimensions:
        if not dim.public:
            warnings.append(make_import_warning(
                code="cube.element_skipped",
                params={
                    "element_type": "dimension",
                    "element_name": dim.name,
                    "cube": cube.name,
                    "reason": "private",
                },
                detail=(
                    f"Dimension '{dim.name}' in cube '{cube.name}' is private "
                    "and was skipped"
                ),
            ))
            continue
        phys_name, is_expr = _physical_col_name(dim.sql, dim.name)
        if is_expr:
            warnings.append(make_import_warning(
                code="cube.expression_manual",
                params={
                    "element_type": "dimension",
                    "element_name": dim.name,
                    "cube": cube.name,
                },
                detail=(
                    f"Dimension '{dim.name}' in cube '{cube.name}' uses an SQL "
                    "expression — source column binding requires manual setup"
                ),
            ))
        if phys_name not in phys_col_seen:
            col_id = gen()
            phys_col_seen.add(phys_name)
            columns.append({
                "id": col_id,
                "model_table_id": table_id,
                "column_name": phys_name,
                "data_type": _DIM_TYPE_MAP.get(dim.dim_type, "string"),
            })
        else:
            col_id = next(c["id"] for c in columns if c["column_name"] == phys_name)
        col_map[dim.name] = col_id

    for measure in cube.measures:
        if not measure.public:
            warnings.append(make_import_warning(
                code="cube.element_skipped",
                params={
                    "element_type": "measure",
                    "element_name": measure.name,
                    "cube": cube.name,
                    "reason": "private",
                },
                detail=(
                    f"Measure '{measure.name}' in cube '{cube.name}' is private "
                    "and was skipped"
                ),
            ))
            continue
        phys_name, is_expr = _physical_col_name(measure.sql, measure.name)
        if is_expr:
            warnings.append(make_import_warning(
                code="cube.expression_manual",
                params={
                    "element_type": "measure",
                    "element_name": measure.name,
                    "cube": cube.name,
                },
                detail=(
                    f"Measure '{measure.name}' in cube '{cube.name}' uses an SQL "
                    "expression — source column binding requires manual setup"
                ),
            ))
        if phys_name not in phys_col_seen:
            col_id = gen()
            phys_col_seen.add(phys_name)
            columns.append({
                "id": col_id,
                "model_table_id": table_id,
                "column_name": phys_name,
                "data_type": "numeric",
            })
        else:
            col_id = next(c["id"] for c in columns if c["column_name"] == phys_name)
        col_map[measure.name] = col_id

    dims_out: list[dict[str, Any]] = []
    for dim in cube.dimensions:
        if not dim.public:
            continue
        is_time = dim.dim_type == "time"
        dims_out.append({
            "id": gen(),
            "model_id": model_id,
            "name": dim.name,
            "display_name": dim.title or _humanize(dim.name),
            "description": dim.description or None,
            "source_column_id": col_map.get(dim.name),
            "is_time_dim": is_time,
        })

    measures_out: list[dict[str, Any]] = []
    for m in cube.measures:
        if not m.public:
            continue
        agg = _MEASURE_TYPE_MAP.get(m.measure_type, "sum")
        invalid_reason: str | None = None
        if m.measure_type in _UNREPRESENTABLE_MEASURE_TYPES:
            invalid_reason = (
                _UNREPRESENTABLE_MEASURE_TYPES[m.measure_type] % m.name
            )
            warnings.append(make_import_warning(
                code="cube.measure_disabled",
                params={
                    "measure": m.name,
                    "cube": cube.name,
                    "reason": m.measure_type,
                },
                detail=invalid_reason,
            ))
        elif m.measure_type not in _MEASURE_TYPE_MAP:
            invalid_reason = (
                f"Unsupported measure type '{m.measure_type}' for '{m.name}' "
                f"in cube '{cube.name}' — imported as a disabled measure "
                f"(defaulted to 'sum')."
            )
            warnings.append(make_import_warning(
                code="cube.measure_disabled",
                params={
                    "measure": m.name,
                    "cube": cube.name,
                    "reason": m.measure_type or "unknown",
                },
                detail=invalid_reason,
            ))

        measure_type = "standard"
        if m.rolling_window:
            measure_type = "calculated"
            warnings.append(make_import_warning(
                code="cube.rolling_window_manual",
                params={"measure": m.name, "cube": cube.name},
                detail=(
                    f"Measure '{m.name}' in cube '{cube.name}' uses "
                    "rolling_window — manual configuration needed in Tessallite"
                ),
            ))
        if m.multi_stage:
            measure_type = "calculated"
            warnings.append(make_import_warning(
                code="cube.multi_stage_manual",
                params={"measure": m.name, "cube": cube.name},
                detail=(
                    f"Measure '{m.name}' in cube '{cube.name}' is multi_stage — "
                    "review calculation in Tessallite"
                ),
            ))

        measures_out.append({
            "id": gen(),
            "model_id": model_id,
            "name": m.name,
            "display_name": m.title or _humanize(m.name),
            "description": m.description or None,
            "source_column_id": col_map.get(m.name),
            "measure_type": measure_type,
            "default_agg": agg,
            "is_invalid": invalid_reason is not None,
            "invalid_reason": invalid_reason,
            "format": m.format or None,
            "semi_additive_behavior": None,
        })

    # F-020-07: Cube joins point at OTHER cubes, which become separate
    # Tessallite models — they cannot be represented as intra-model joins.
    # The previous code emitted rows with `to_cube`/`relationship` columns the
    # `Join` ORM does not have, so `insert(Join).values(**row)` raised
    # "Unconsumed column names" and crashed every import of a cube with joins.
    # Drop the join rows and warn per join, matching the dbt foreign-entity
    # approach.
    joins_out: list[dict[str, Any]] = []
    for join in cube.joins:
        warnings.append(make_import_warning(
            code="cube.join_manual",
            params={"cube": cube.name, "join": join.name},
            detail=(
                f"Cube '{cube.name}' join to '{join.name}' "
                f"(relationship '{join.relationship}') skipped — Cube joins "
                "reference other cubes, which import as separate Tessallite "
                "models. Configure the join manually in the Model Builder."
            ),
        ))

    hier_out: list[dict[str, Any]] = []
    for hier in cube.hierarchies:
        h_id = gen()
        levels_out = []
        skipped = []
        for lvl in hier.levels:
            key_col = col_map.get(lvl)
            if key_col is None:
                skipped.append(lvl)
                continue
            levels_out.append({
                "id": gen(),
                "hierarchy_id": h_id,
                "name": lvl,
                "ordinal": len(levels_out),
                "key_attribute_id": key_col,
                "key_attribute_source": "physical_column",
            })
        if skipped:
            warnings.append(make_import_warning(
                code="cube.hierarchy_levels_skipped",
                params={
                    "hierarchy": hier.name,
                    "cube": cube.name,
                    "count": len(skipped),
                },
                detail=(
                    f"Hierarchy '{hier.name}' in cube '{cube.name}': skipped "
                    f"levels {skipped} — dimension not found or not public"
                ),
            ))
        if not levels_out:
            continue
        hier_out.append({
            "id": h_id,
            "model_id": model_id,
            "name": _slugify(hier.name or cube.name),
            "type": "explicit",
            "levels": levels_out,
        })

    udas_out: list[dict[str, Any]] = []
    uda_refs_out: list[dict[str, Any]] = []
    time_dims = [d for d in cube.dimensions if d.dim_type == "time" and d.public]
    for td in time_dims:
        if any(td.name in h.levels for h in cube.hierarchies):
            continue
        h_id = gen()
        time_col_id = col_map.get(td.name)
        phys_name, is_expr = _physical_col_name(td.sql, td.name)
        if is_expr:
            extract_source = f"({td.sql})"
        else:
            extract_source = f"\"{phys_name}\""
        level_entries = []
        for i, lvl in enumerate(["Year", "Quarter", "Month", "Day"]):
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
            "slug": _slugify(cube.name),
            "display_name": cube.title or _humanize(cube.name),
            "description": cube.description or None,
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
            "display_name": "Cube Import Source",
            "config": {},
        }],
        "data_targets": [],
    }
    return snapshot


def _slugify(name: str) -> str:
    # Bug-7622: delegate to the shared BI-safe generator so digit-leading and
    # symbol-only names produce a valid slug instead of one that later trips
    # validate_bi_safe_slug and raises an uncaught 500 in the import endpoint.
    return slugify(name, fallback="cube_model", separator="_")


def _humanize(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


def _physical_col_name(sql: str, logical_name: str) -> tuple[str, bool]:
    """Resolve the physical column name from a Cube sql field.

    Returns (column_name, is_expression). Simple identifiers use the sql
    value directly; expressions fall back to the logical name.
    """
    if not sql:
        return logical_name, False
    if _SIMPLE_IDENT_RE.match(sql):
        return sql, False
    m = _CUBE_MACRO_RE.match(sql)
    if m:
        return m.group(1), False
    return logical_name, True


def _id_gen():
    def _next() -> str:
        return str(uuid.uuid4())
    return _next
