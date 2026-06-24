"""
Internal Representation (IR) for all queries entering the router.

Both SQL (JDBC) and DAX paths produce a LogicalQuery before routing begins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class LogicalFilter:
    dimension_name: str
    operator: str   # eq | neq | gt | gte | lt | lte | in | between | like | is_null | is_not_null
    value: Any      # scalar, list (for IN), or tuple (for BETWEEN)


@dataclass
class SelectExpression:
    """A single item in the SELECT list with classification metadata."""
    raw_text: str                    # Original SQL text: "COUNT(1)", "SUM(revenue)"
    alias: Optional[str]             # AS alias if present
    classification: str              # "analytical" | "literal" | "passthrough"
    agg_function: Optional[str]      # "count", "sum", etc. (lowercased)
    inner_column: Optional[str]      # Column name inside aggregate, or None for literals
    inner_literal: Optional[str]     # "1", "*" for literal aggregates
    # Composable aggregate expression: a scalar composition of simple,
    # re-aggregatable aggregates (e.g. SUM(a)/SUM(b), CASE over SUMs). Stays
    # classification="passthrough" so the source rewriter / audit are unchanged,
    # but it is aggregate-ROUTABLE (the matcher does not bail on it). The
    # aggregate functions used are listed for the matcher's additivity gating.
    composable: bool = False
    agg_functions: list[str] = field(default_factory=list)
    # Per-node ``(inner_column, agg_function)`` pairs for a composable
    # expression. Carries the EXACT stat each aggregate node requested (e.g.
    # ``MAX(a)/MAX(b)`` -> ``[("a", "max"), ("b", "max")]``; ``COUNT(*)`` ->
    # ``("__row_count", "count")``) so the matcher can require the matching
    # stat column rather than falling back to the measure's default_agg.
    inner_aggregates: list[tuple[str, str]] = field(default_factory=list)

@dataclass
class LogicalQuery:
    model_id: str
    protocol: str                           # jdbc | dax
    raw_query: str
    requested_measures: list[str]           # semantic measure names
    requested_dimensions: list[str]         # semantic dimension names in SELECT
    filters: list[LogicalFilter]
    grain: list[str]                        # dimensions in GROUP BY (drives aggregate matching)
    order_by: list[tuple[str, str]]         # [(field_name, "asc"|"desc")]
    limit: Optional[int]
    offset: Optional[int]
    query_fingerprint: str                  # SHA-256 (hex[:64]) of normalised structure
    select_star: bool = False               # True when query uses SELECT *
    select_expressions: list[SelectExpression] = field(default_factory=list)
    from_tables: list[str] = field(default_factory=list)  # Table names from FROM / JOIN
    syntax_warnings: list[str] = field(default_factory=list)
    having_raw: Optional[str] = None        # Raw HAVING clause SQL text
    having_columns: list[str] = field(default_factory=list)  # Column names referenced in HAVING
    has_unresolvable_where: bool = False     # True when WHERE has predicates not representable as LogicalFilter
    has_unresolvable_order: bool = False     # True when ORDER BY has expressions not representable as (bare_column, direction)
    has_distinct: bool = False               # True when query uses SELECT DISTINCT
    has_function_grain: bool = False         # True when GROUP BY contains function expressions (DATE_TRUNC, EXTRACT, etc.)
    has_complex_sql: bool = False            # True when query has CTEs, derived tables, window functions, or correlated subqueries
    has_window_functions: bool = False       # True when any query scope contains an OVER (...) expression
    has_window_aggregate: bool = False       # True when an OVER (...) expression wraps an aggregate function
    cte_aliases: list[str] = field(default_factory=list)  # CTE alias names (WITH x AS ...)
    # Sqlglot dialect the raw_query was parsed with. The rewriter's
    # raw-AST re-parses must use the same dialect or identifier quoting
    # (``"x"`` in Postgres vs string literals in BigQuery) shifts under
    # them. Defaults to ``postgres`` — the canonical internal dialect.
    input_dialect: str = "postgres"
    # DAX time-intelligence hints: {measure_name: variant_kind}
    # Populated by the DAX normalizer when the gateway pre-parses the DAX
    # and sends time_variant_hints in the request body. Maps e.g.
    # {"Revenue": "ytd"} (from TOTALYTD) or {"Sales": "prior_year"}
    # (from SAMEPERIODLASTYEAR). The rewriter uses these when the resolved
    # Measure ORM object has no variant_kind set.
    time_variant_hints: Optional[dict[str, str]] = None
    # Trusted drill-only join path selected by semantic_builder after curation
    # validation. This is not parsed from raw SQL so external SQL comments
    # cannot influence semantic join selection.
    drill_join_path_ids: list[str] = field(default_factory=list)


@dataclass
class BoundQuery:
    """LogicalQuery after all names are resolved against the semantic model."""
    logical_query: LogicalQuery
    model: Any                              # shared.db.models.Model ORM object
    resolved_measures: list[Any]            # Measure ORM objects
    resolved_dimensions: list[Any]          # Dimension ORM objects
    resolved_filters: list[LogicalFilter]   # filters with verified dimension names
    resolved_dimensions_by_name: dict[str, Any] = field(default_factory=dict)
    has_passthrough_expressions: bool = False
    # Semantic objects (dims/measures) the query references that are
    # currently marked ``is_invalid=True``. The router skips the
    # aggregate matcher when this list is non-empty and falls back to
    # the source path, then records a ``query_fallback`` alert so the
    # modeler knows an end-user query hit a broken object.
    uses_invalid_objects: list[tuple[str, str]] = field(default_factory=list)
    persona_narrowed_star: bool = False
    # Physical source column names for the model's tables. Used by the
    # security audit to validate SELECT * results where the source DB
    # returns physical names rather than semantic dimension/measure names.
    allowed_physical_columns: set[str] = field(default_factory=set)


@dataclass
class RouteDecision:
    route_type: str                         # pocket | aggregate | source
    rewritten_query: str
    reason: str
    aggregate_id: Optional[str] = None
    pocket_id: Optional[str] = None
    # When the pocket matcher is invoked but does not produce a match,
    # this carries the specific reason (e.g. flag_disabled, no_candidates,
    # fingerprint_mismatch, predicate_mismatch, passthrough). Surfaced
    # in route_logs so Diagnostics can show why the pocket path was skipped.
    pocket_skipped_reason: Optional[str] = None
    aggregate_skipped_reasons: Optional[list[str]] = None
    security_rules_applied: Optional[list[dict]] = None
    # F-27: Resolved dialect of ``rewritten_query`` — for an aggregate route
    # this is the aggregate TARGET connection's dialect (where the aggregate
    # table lives); for source/pocket routes it is the source dialect. Passed
    # through so downstream rewriter calls can skip the redundant DB lookup.
    target_dialect: Optional[str] = None
    # F-006-01: Resolved dialect of the SOURCE connection. The missing-relation
    # fallback re-rewrites the query for the SOURCE (not the cache target) and
    # executes it against the source connection, so it must transpile in the
    # source dialect — using ``target_dialect`` would emit (e.g.) PostgreSQL
    # quoting against a BigQuery/Spark source for a cross-database aggregate.
    # Defaults to ``target_dialect`` when unset (same-database deployments).
    source_dialect: Optional[str] = None
    # Bug-5195: the aggregate object whose hit_count should be credited AFTER
    # successful execution. The router sets this instead of crediting at route
    # time (pre-execution) so that only queries that actually execute against
    # the aggregate are counted. The caller must call
    # ``record_aggregate_hit(pending_hit_credit, db)`` after the query
    # succeeds, and MUST NOT credit on execution failure.
    pending_hit_credit: Any = None


@dataclass
class PocketMatchResult:
    """Outcome of `find_best_pocket`.

    Either `pocket` is set (match), or `skipped_reason` is set (skip). The
    reason is a short enum string consumed by the router + logger.
    """
    pocket: Any = None
    skipped_reason: Optional[str] = None


class SemanticBindingError(ValueError):
    """Raised when a measure or dimension name cannot be resolved."""


class ModelNotDeployedError(SemanticBindingError):
    """Raised when a query targets a model whose deployed_version_id is None.

    Subclasses SemanticBindingError so existing ``except SemanticBindingError``
    clauses keep catching it (route_handlers still short-circuit before the
    router / executor run), while handlers that want to return HTTP 409 can
    match this more specific type first.
    """


class ResultTooLargeError(RuntimeError):
    """Raised when result row count exceeds MAX_RESULT_ROWS."""


class NoAggregateMatchError(Exception):
    """Raised when force_route=aggregate/pocket but no fast-path exists."""


class UnsupportedSQL(ValueError):
    """Raised for SQL constructs that cannot be executed without changing results."""

    sqlstate = "0A000"


class CrossModelNotResolvedError(Exception):
    """Raised when a query references a cross-model measure within the same project.

    Full cross-model SQL resolution is deferred to Phase 11.
    The measure's cross_model_source_model_id and cross_model_source_measure_id
    carry the reference metadata.
    """
    def __init__(self, measure_slug: str, source_model_id: str) -> None:
        super().__init__(
            f"Measure '{measure_slug}' references model {source_model_id} in this project (cross-model). "
            "Cross-model query resolution is not yet supported. "
            "Remove the cross-model reference or query each model separately."
        )
        self.measure_slug = measure_slug
        self.source_model_id = source_model_id
