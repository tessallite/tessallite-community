"""Drill-through — semantic gateway path.

All drill queries route through the query-router's parse → bind →
route → execute pipeline.  The semantic builder produces model-level
SQL; the pipeline handles UDA expansion, row security, persona gating,
and aggregate routing.
"""
from .cursor import CursorOrderTerm, CursorValidationError, DrillCursorSpec
from .semantic_builder import (
    DrillSemanticError,
    DrillableHierarchy,
    DrillDimension,
    HierarchyPathEntry,
    build_drill_sql,
    resolve_drill_options,
)

__all__ = [
    "CursorOrderTerm",
    "CursorValidationError",
    "DrillCursorSpec",
    "DrillSemanticError",
    "DrillableHierarchy",
    "DrillDimension",
    "HierarchyPathEntry",
    "build_drill_sql",
    "resolve_drill_options",
]
