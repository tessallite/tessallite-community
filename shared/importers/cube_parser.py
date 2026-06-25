"""Parse Cube.dev YAML data models into intermediate structures.

Cube uses YAML files with a top-level ``cubes:`` key containing cube
definitions. Each cube has measures, dimensions, joins, segments,
hierarchies, and pre_aggregations.

Only YAML models are supported; JavaScript dynamic models are not parseable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class CubeDimension:
    name: str
    sql: str = ""
    dim_type: str = "string"  # string, number, time, boolean, geo
    title: str = ""
    description: str = ""
    primary_key: bool = False
    public: bool = True
    format: str = ""
    sub_query: bool = False


@dataclass
class CubeMeasure:
    name: str
    sql: str = ""
    measure_type: str = "count"
    title: str = ""
    description: str = ""
    format: str = ""
    public: bool = True
    rolling_window: dict[str, Any] | None = None
    filters: list[dict[str, Any]] = field(default_factory=list)
    drill_members: list[str] = field(default_factory=list)
    multi_stage: bool = False


@dataclass
class CubeJoin:
    name: str
    sql: str = ""
    relationship: str = ""  # one_to_one, one_to_many, many_to_one, many_to_many


@dataclass
class CubeSegment:
    name: str
    sql: str = ""
    title: str = ""
    description: str = ""


@dataclass
class CubeHierarchy:
    name: str
    title: str = ""
    levels: list[str] = field(default_factory=list)


@dataclass
class CubeDefinition:
    name: str
    sql_table: str = ""
    sql: str = ""
    title: str = ""
    description: str = ""
    data_source: str = "default"
    public: bool = True
    extends: str = ""
    measures: list[CubeMeasure] = field(default_factory=list)
    dimensions: list[CubeDimension] = field(default_factory=list)
    joins: list[CubeJoin] = field(default_factory=list)
    segments: list[CubeSegment] = field(default_factory=list)
    hierarchies: list[CubeHierarchy] = field(default_factory=list)


@dataclass
class CubeParseResult:
    cubes: list[CubeDefinition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CubeParseError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} parse error(s): {'; '.join(errors[:5])}")


def parse_cube_yaml(content: str) -> CubeParseResult:
    """Parse a single Cube YAML file."""
    doc = yaml.safe_load(content)
    if not isinstance(doc, dict):
        raise CubeParseError(["YAML root must be a mapping"])

    result = CubeParseResult()

    for cube_raw in doc.get("cubes", []):
        cube = _parse_cube(cube_raw, result)
        if cube:
            result.cubes.append(cube)

    if not result.cubes:
        result.errors.append(
            "No cubes found. Ensure the YAML has a 'cubes:' top-level key "
            "with Cube.dev model definitions."
        )

    if result.errors:
        raise CubeParseError(result.errors)

    return result


def parse_cube_project(files: dict[str, str]) -> CubeParseResult:
    """Parse multiple Cube YAML files (a project directory)."""
    combined = CubeParseResult()

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
        if "cubes" not in doc:
            continue

        try:
            partial = parse_cube_yaml(content)
        except CubeParseError as exc:
            combined.warnings.append(
                f"Skipped {filename}: {'; '.join(exc.errors[:3])}"
            )
            continue
        combined.cubes.extend(partial.cubes)
        combined.warnings.extend(partial.warnings)

    if not combined.cubes:
        combined.errors.append("No cubes found in any YAML file")
        raise CubeParseError(combined.errors)

    return combined


def _parse_cube(raw: dict[str, Any], result: CubeParseResult) -> CubeDefinition | None:
    name = raw.get("name", "")
    if not name:
        result.errors.append("cube entry missing 'name'")
        return None

    cube = CubeDefinition(
        name=name,
        sql_table=raw.get("sql_table", ""),
        sql=raw.get("sql", ""),
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        data_source=raw.get("data_source", "default"),
        public=raw.get("public", True),
        extends=raw.get("extends", ""),
    )

    for m in raw.get("measures", []):
        cube.measures.append(_parse_measure(m))

    for d in raw.get("dimensions", []):
        cube.dimensions.append(_parse_dimension(d))

    for j in raw.get("joins", []):
        cube.joins.append(CubeJoin(
            name=j.get("name", ""),
            sql=j.get("sql", ""),
            relationship=j.get("relationship", ""),
        ))

    for s in raw.get("segments", []):
        cube.segments.append(CubeSegment(
            name=s.get("name", ""),
            sql=s.get("sql", ""),
            title=s.get("title", ""),
            description=s.get("description", ""),
        ))

    for h in raw.get("hierarchies", []):
        levels = h.get("levels", [])
        level_names = []
        for lvl in levels:
            if isinstance(lvl, str):
                level_names.append(lvl)
            elif isinstance(lvl, dict):
                level_names.append(lvl.get("name", ""))
        cube.hierarchies.append(CubeHierarchy(
            name=h.get("name", ""),
            title=h.get("title", ""),
            levels=level_names,
        ))

    return cube


def _parse_measure(raw: dict[str, Any]) -> CubeMeasure:
    rolling = raw.get("rolling_window")
    filters_raw = raw.get("filters", [])
    filters = filters_raw if isinstance(filters_raw, list) else []
    drill = raw.get("drill_members", [])

    return CubeMeasure(
        name=raw.get("name", ""),
        sql=raw.get("sql", ""),
        measure_type=raw.get("type", "number"),
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        format=raw.get("format", ""),
        public=raw.get("public", True),
        rolling_window=rolling if isinstance(rolling, dict) else None,
        filters=filters,
        drill_members=drill if isinstance(drill, list) else [],
        multi_stage=raw.get("multi_stage", False),
    )


def _parse_dimension(raw: dict[str, Any]) -> CubeDimension:
    return CubeDimension(
        name=raw.get("name", ""),
        sql=raw.get("sql", ""),
        dim_type=raw.get("type", "string"),
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        primary_key=raw.get("primary_key", False),
        public=raw.get("public", True),
        format=raw.get("format", ""),
        sub_query=raw.get("sub_query", False),
    )
