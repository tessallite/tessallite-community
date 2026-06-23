"""Query Rewriter — translates a BoundQuery into executable SQL.

Phase 3 decomposition: this module is now a thin facade.  The implementation
lives in focused sibling modules; this file re-exports the public surface and
the symbols the test suite imports/patches so existing callers and tests keep
working unchanged.

Module map:
- dialects             — dialect token maps + transpile helpers
- dialect_resolution   — DB-backed target-dialect resolution
- join_graph_cache     — TTL cache for the per-model join graph
- conditions           — WHERE / value rendering + identifier quoting
- uda                  — user-defined-attribute expression rendering
- joins                — JOIN-clause construction
- calendar_support     — time-dimension / calendar / semi-additive support
- aggregate            — rewrite_for_aggregate (aggregate-table path)
- pocket               — rewrite_for_pocket (pocket-table path)
- source_sql           — rewrite_for_source + _build_source_sql (source path)

Two query paths:
1. rewrite_for_aggregate: SELECT against the aggregate's physical table.
2. rewrite_for_source: semantic-to-physical star (or raw pass-through).
"""
from __future__ import annotations

from src.ir.logical_query import SemanticBindingError

from src.rewrite.aggregate import (
    AggregateRewriteUnsupported,
    _build_col_lookup,
    _build_dim_phys_lookup,
    _dim_phys_name,
    _full_table_ref,
    rewrite_for_aggregate,
)
from src.rewrite.calendar_support import (
    _build_calendar_columns,
    _is_time_dimension,
    _resolve_calendar_binding,
    _resolve_hierarchy_calendar_rules,
    _semi_additive_agg,
)
from src.rewrite.conditions import (
    _coerce_value,
    _qualified_column,
    _quote,
    _quote_compound,
    _render_condition,
    _render_value,
    _render_where,
)
from src.rewrite.dialect_resolution import (
    _resolve_target_dialect,
    resolve_target_dialect,
    resolve_target_dialect_for_bound,
)
from src.rewrite.dialects import (
    _dialect_from_connection_type,
    _dialect_to_connector,
    _requote_identifiers_for_bigquery,
    _translate_raw_sql,
    _transpile_to_dialect,
    dialect_from_connection_type,
    dialect_to_connector,
)
from src.rewrite.join_graph_cache import (
    _get_join_graph,
    _put_join_graph,
    invalidate_join_graph_cache,
)
from src.rewrite.joins import (
    _build_joined_from_clause,
    _coerce_join_pair,
    _join_keyword,
    _missing_join_error_message,
)
from src.rewrite.pocket import rewrite_for_pocket
from src.rewrite.source_sql import (
    _build_persona_star_sql,
    _build_source_sql,
    _substitute_table_names,
    rewrite_for_source,
)
from src.rewrite.uda import (
    _normalize_uda_expression_quoting,
    _render_uda_expression,
)

__all__ = [
    # Public API (router / routes / headless)
    "rewrite_for_aggregate",
    "rewrite_for_pocket",
    "rewrite_for_source",
    "resolve_target_dialect",
    "resolve_target_dialect_for_bound",
    "dialect_to_connector",
    "dialect_from_connection_type",
    "invalidate_join_graph_cache",
    # Errors
    "SemanticBindingError",
    "AggregateRewriteUnsupported",
    # Test-imported internals
    "_build_source_sql",
    "_build_persona_star_sql",
    "_substitute_table_names",
    "_build_joined_from_clause",
    "_missing_join_error_message",
    "_coerce_join_pair",
    "_join_keyword",
    "_coerce_value",
    "_render_where",
    "_render_condition",
    "_render_value",
    "_quote",
    "_quote_compound",
    "_qualified_column",
    "_normalize_uda_expression_quoting",
    "_render_uda_expression",
    "_semi_additive_agg",
    "_is_time_dimension",
    "_resolve_calendar_binding",
    "_build_calendar_columns",
    "_resolve_hierarchy_calendar_rules",
    "_build_col_lookup",
    "_build_dim_phys_lookup",
    "_dim_phys_name",
    "_full_table_ref",
    "_dialect_from_connection_type",
    "_dialect_to_connector",
    "_requote_identifiers_for_bigquery",
    "_translate_raw_sql",
    "_transpile_to_dialect",
    "_resolve_target_dialect",
    "_get_join_graph",
    "_put_join_graph",
]
