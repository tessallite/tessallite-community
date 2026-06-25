"""Drill-through — semantic gateway path.

All drill queries route through the query-router's parse → bind →
route → execute pipeline.  The semantic builder produces model-level
SQL; the pipeline handles UDA expansion, row security, persona gating,
and aggregate routing.
"""
from .semantic_builder import (
    DrillSemanticError,
    DrillableHierarchy,
    DrillDimension,
    HierarchyPathEntry,
    build_drill_sql,
    decode_cursor,
    encode_cursor,
    resolve_drill_options,
)

__all__ = [
    "DrillSemanticError",
    "DrillableHierarchy",
    "DrillDimension",
    "HierarchyPathEntry",
    "build_drill_sql",
    "decode_cursor",
    "encode_cursor",
    "resolve_drill_options",
]
