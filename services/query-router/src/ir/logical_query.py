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
    operator: str   # eq | neq | gt | gte | lt | lte | in | not_in | between | like | not_like | is_null | is_not_null
    value: Any      # scalar, list (for IN), or tuple (for BETWEEN)
    # Bug-6383: LIKE/NOT LIKE escape character. Set (to "\\") by the JSON
    # filter contract when the pattern was produced by the ``contains`` /
    # ``notContains`` aliases, which backslash-escape wildcard metacharacters
    # in the user's search text. The renderer emits ``ESCAPE '<char>'`` so the
    # escape is portable across dialects (sqlglot transpiles it). Raw ``like``
    # patterns leave this None so their wildcards pass through unchanged.
    like_escape: Optional[str] = None


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
    # Bug-6969/5891: for an ordered-set percentile SELECT item
    # (``PERCENTILE_CONT/DISC(frac) WITHIN GROUP (ORDER BY col [ASC|DESC])``),
    # the parser's recogniser attaches the semantic quantile facts the pNN
    # suffix alone cannot carry: ``{"method": continuous|discrete, "direction":
    # asc|desc, "fraction_text": exact-decimal-str}``. None for every non-
    # percentile item and for ``MEDIAN`` (which the binder treats as continuous
    # p50 ASC). The binder reads this to build the QuantileRequest inventory that
    # the coverage proof gates; leaving it None keeps every existing consumer and
    # the query fingerprint byte-identical for non-percentile queries.
    quantile_meta: Optional[dict] = None
    # Per-node ``(inner_column, agg_function)`` pairs for a composable
    # expression. Carries the EXACT stat each aggregate node requested (e.g.
    # ``MAX(a)/MAX(b)`` -> ``[("a", "max"), ("b", "max")]``; ``COUNT(*)`` ->
    # ``("__row_count", "count")``) so the matcher can require the matching
    # stat column rather than falling back to the measure's default_agg.
    inner_aggregates: list[tuple[str, str]] = field(default_factory=list)

@dataclass
class ExpressionOccurrence:
    """One textual appearance of a non-column expression in the query.

    Spec §5.1. Captured by the parser during its existing AST traversal (never
    re-parsed from ``raw_query``). Repeated SELECT/GROUP BY/ORDER BY expressions
    that share one bound expression by fingerprint keep distinct occurrences so
    output order and aliases are preserved.

    Phase 1 is diagnostic-only: occurrences are captured and bound, but the
    router still source-routes any function-grain query. They carry no serving
    authority until the proof engine (later phases) consumes them.
    """
    occurrence_id: str
    role: str                        # GROUP_KEY | SELECT | WHERE_LEFT | WHERE_RIGHT | HAVING_KEY | ORDER_KEY
    raw_sql: str                     # exact rendered text of the selected AST node
    input_dialect: str               # dialect the raw_query was parsed with
    # sqlglot ``.dump()`` of the selected node — a JSON-serialisable structure
    # (a list of node records), NOT a bare dict. Retained as the captured-AST
    # producer contract so later phases bind against the query's own AST instead
    # of re-parsing raw_sql (spec §18). ``Any`` because .dump() returns a list.
    ast_json: Any
    output_alias: Optional[str] = None


# Valid ExpressionOccurrence.role values (spec §5.1). Kept as a frozenset so the
# parser and binder validate against one source of truth rather than free text.
EXPRESSION_ROLES: frozenset[str] = frozenset(
    {"GROUP_KEY", "SELECT", "WHERE_LEFT", "WHERE_RIGHT", "HAVING_KEY", "ORDER_KEY"}
)


@dataclass
class BoundColumnRef:
    """Stable physical column identity for a derived-expression leaf (spec §5.1).

    Uses model/table/column UUIDs and the deployed physical column identity,
    never a free-form name. Populated by the binder against the deployed
    snapshot.
    """
    model_id: str
    table_id: str
    column_id: str
    physical_column: str
    logical_name: Optional[str] = None


@dataclass
class BoundDerivedExpression:
    """A function-grain expression bound to the deployed model snapshot.

    Spec §5.1. One per distinct expression fingerprint; ``occurrence_ids`` links
    back to every ``ExpressionOccurrence`` that produced it. A bindable but
    unproved expression is still valid for the source path — ``proof_rejection``
    explains why it is not acceleratable. Phase 1 populates the identity and
    lineage fields for diagnostics; the semantic-profile / determinism / totality
    fields are filled by the canonicaliser as later phases enrich the registry.
    """
    occurrence_ids: list[str]
    model_id: str
    canonical_sql: str
    expression_fingerprint: str
    inputs: list[BoundColumnRef] = field(default_factory=list)
    deployed_version_id: Optional[str] = None
    canonical_ast: Optional[dict] = None
    output_type: Optional[str] = None
    nullable: bool = True
    # SemanticContext is a structured dict in Phase 1 (source connector family,
    # timestamp type, semantic timezone, collation profile, NULL mode, sqlglot
    # profile hash, canonicalizer + registry versions). Kept as a dict here so
    # the IR does not depend on the shared canonicaliser's concrete type.
    semantic_context: dict = field(default_factory=dict)
    determinism: str = "UNKNOWN"     # IMMUTABLE | STABLE | VOLATILE | UNKNOWN
    totality: str = "UNKNOWN"        # TOTAL | GUARDED | MAY_ERROR | UNKNOWN
    supported_roles: set[str] = field(default_factory=set)
    # Stable DerivedReasonCode value (string) when the expression is bindable but
    # not acceleratable; None means "no rejection recorded yet". Never gates the
    # source path — purely diagnostic in Phase 1.
    proof_rejection: Optional[str] = None


@dataclass
class BoundAttributeRelabel:
    """A resolvable strict-bijection relabel group key (spec §2.2 / §3.2).

    One per query GROUP BY detail dimension that resolves to exactly one enabled,
    deployed BIJECTION relationship. Every identity field is a stable id, never a
    logical/display/source/passenger name. Only a record with complete stable ids,
    one unambiguous deployed relationship, ``enabled=True``, ``cardinality=
    BIJECTION`` and no ``proof_rejection`` may become a serving request; anything
    else routes to source. For CLS the lineage pair is ``[key_column_id,
    detail_column_id]`` — both endpoints remain security inputs.
    """
    group_ordinal: int
    select_ordinals: list[int]
    query_dimension_id: str
    owning_dimension_id: str
    relationship_id: str
    attribute_key: str                       # attr:<relationship_id>
    key_column_id: str
    detail_column_id: str
    cardinality: str                         # stage 4 accepts only BIJECTION
    declaration_hash: str
    requested_name: str                      # parsed terminal spelling for output
    output_alias: Optional[str] = None
    proof_rejection: Optional[str] = None


@dataclass
class BoundGroupKeyProjection:
    """One bound SELECT projection of a group key (spec §3.4).

    Associates a parsed SELECT ordinal with the group key it projects, so the
    rewrite SELECT-walk reproduces every projection (including duplicates) under
    its own alias and the arity guard counts BOUND OUTPUT projections, not unique
    proof plans.
    """
    select_ordinal: int
    group_ordinal: int
    kind: str                                # EXPRESSION | PHYSICAL | ATTRIBUTE
    query_dimension_id: Optional[str] = None
    column_id: Optional[str] = None
    occurrence_id: Optional[str] = None
    key_id: Optional[str] = None             # dim:<uuid> for PHYSICAL
    attribute_key: Optional[str] = None      # attr:<uuid> for ATTRIBUTE
    output_alias: Optional[str] = None


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
    # F-003-11: ORDER BY names that are SELECT aliases of a non-column
    # (aggregate / expression). The aggregate rewriter must ORDER BY the
    # output alias, not a grain column of the same name.
    order_by_alias_names: set[str] = field(default_factory=set)
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
    # Derived-grain routing (spec §5.1, Phase 1). Typed capture of every
    # non-column expression occurrence (GROUP BY / SELECT / WHERE / HAVING /
    # ORDER) alongside the existing ``has_function_grain`` boolean, which is
    # retained as the compatibility/fallback signal. Empty for ordinary queries,
    # so the fingerprint and every existing consumer are byte-identical when no
    # function grain is present.
    expression_occurrences: list[ExpressionOccurrence] = field(default_factory=list)
    # Bug-7796: DAX inline aggregate overrides: {measure_name: agg_function}.
    # Populated by the DAX normalizer when an inline ``AGG(Table[Column])``
    # is resolved to a measure name. The source rewriter uses this to apply
    # the REQUESTED aggregate function instead of the measure's default_agg,
    # preventing wrong numbers when SUM(col) binds to a measure whose default
    # is not SUM.
    measure_agg_overrides: dict[str, str] = field(default_factory=dict)
    # Bug-7359 / F-003-08: recognized time-period grains from GROUP BY.
    # Each entry is (unit, column_name). DATE_TRUNC units are bare
    # ('month', 'year', 'quarter', …). EXTRACT(month|year|quarter FROM col)
    # is stored as ('extract_month', col) so fingerprints and rewrite stay
    # distinct from DATE_TRUNC (spec I11). Non-empty ONLY when ALL
    # function-grain GROUP BY items are recognized — if any item is
    # unrecognized, this stays empty and the query falls through to source
    # via has_function_grain passthrough.
    time_period_grains: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class BoundQuery:
    """LogicalQuery after all names are resolved against the semantic model."""
    logical_query: LogicalQuery
    model: Any                              # shared.db.models.Model ORM object
    resolved_measures: list[Any]            # Measure ORM objects
    resolved_dimensions: list[Any]          # Dimension ORM objects
    resolved_filters: list[LogicalFilter]   # filters with verified dimension names
    resolved_dimensions_by_name: dict[str, Any] = field(default_factory=dict)
    # Bug-5546: logical dimension name -> source-column data type (e.g. "INT64").
    # Resolved by the binder (which has the DB session) so the SYNCHRONOUS
    # aggregate rewriter can type WHERE literals correctly — the aggregate route
    # has no DB access of its own, so without this it renders int filters as
    # string literals (INT64 = STRING → BigQuery 400).
    dim_type_by_name: dict[str, str] = field(default_factory=dict)
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
    # Complex-SQL passthrough only: the OUTERMOST projection's output names,
    # lower-cased — the column names the client actually receives. These are the
    # names the QUERY defines for itself (a SELECT-list alias, or a CTE /
    # derived-table output name it projects onward); they are not physical model
    # columns and never appear in ``allowed_physical_columns``. Published by the
    # binder only after ``_validate_complex_sql_columns`` has proven every
    # physical read contained, so the post-execute result-column audit can tell a
    # query-defined name apart from an unauthorised column instead of blocking
    # ordinary ``SELECT dim, SUM(m) AS total`` output. Empty for every
    # non-complex query.
    complex_projection_names: set[str] = field(default_factory=set)
    # Bug-5488: canonical model DIMENSION names referenced ONLY inside an
    # unresolvable WHERE predicate (a function-wrapped or OR-compound shape
    # that the parser's strict ``_extract_filters`` cannot turn into a
    # ``LogicalFilter``). The binder collects these by walking the raw WHERE
    # AST so the source rewriter can load their physical columns and join
    # their tables even though they never appear in ``resolved_filters``,
    # SELECT, or ORDER BY. Kept separate from ``resolved_filters`` so it does
    # NOT feed the aggregate/pocket matchers, miss-log fingerprint, security
    # audit, or rendered WHERE — only the source-path column/table collection.
    # Dimensions only: a measure referenced solely in the WHERE cannot be
    # resolved by the source path's dimension-backfill, so it is excluded (see
    # ``_collect_where_referenced_fields``).
    where_referenced_dimensions: set[str] = field(default_factory=set)
    # Derived-grain routing (spec §5.1, Phase 1). One BoundDerivedExpression per
    # distinct expression fingerprint captured in the LogicalQuery. Diagnostic
    # only in Phase 1: populated by the binder for explain/telemetry, and it does
    # NOT change the ``has_passthrough_expressions`` decision. Empty for ordinary
    # queries.
    bound_derived_expressions: list[BoundDerivedExpression] = field(default_factory=list)
    # Bug-6969/5891, spec §4.1/§5.1. The complete semantic quantile inventory:
    # one QuantileRequest per quantile the query READS — SELECT ordered-set
    # percentiles and MEDIAN (from ``quantile_meta``), AND bare measures whose
    # ``default_agg`` is a quantile stat (origin=measure_default), so a stored
    # ``m__pNN`` column read via the plain (measure, stat) path is ALSO
    # coverage-proof-gated under enforce (Fable R2 MEDIUM-1 — the I8 hole). An
    # explicit aggregation over such a measure overrides its default_agg and is
    # NOT inventoried. Populated by the binder. Empty for every non-quantile
    # query, so the matcher's coverage-proof branch is only entered when a stored
    # quantile could be served. Typed as ``Any`` list to avoid an IR import cycle
    # with shared.quantile_contracts.
    quantile_requests: list[Any] = field(default_factory=list)
    # Stage-4 relabel serving (spec §2.2 / §3.2 / §3.4). One BoundAttributeRelabel
    # per resolvable bijection-relabel GROUP BY detail dimension, and one
    # BoundGroupKeyProjection per bound group-key SELECT projection (incl.
    # duplicates). Empty for ordinary queries and when the routing mode is off, so
    # every existing consumer is byte-identical. Populated by the binder against
    # the deployed snapshot; consumed only by the derived-serving request builder,
    # proof engine and exact rewrite (all gated behind the OFF-by-default flag).
    bound_attribute_relabels: list[BoundAttributeRelabel] = field(default_factory=list)
    bound_group_key_projections: list[BoundGroupKeyProjection] = field(default_factory=list)
    # Bug-7981: request-selected immutable deployed snapshot authority. Source
    # and raw rewriting must use this same shape for the physical join graph;
    # they must not independently resolve a version row that a concurrent
    # backward revert may delete after binding. Appended to preserve the
    # positional constructor contract for older integrations and fixtures.
    deployed_shape: Any = None


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
    # Bug-7038 / codex R1: when the aggregate route was chosen under active
    # RLS (Bug-7033), the compiled predicate is carried here so the
    # missing-table fallback (execute_with_observation) can re-inject the
    # security predicate into the fallback source SQL. Without this, a
    # missing aggregate table causes the fallback to serve unfiltered rows.
    security_compiled: Any = None
    # Bug-8455 (pocket) / Bug-8457 (aggregate): the ADMISSION-time generation
    # stamp of the cache artifact this decision was proved against — an
    # ``ArtifactGeneration`` (status, active_refresh_run_id,
    # physical_table_name, target_schema, target_id) captured from the exact
    # ORM row the matcher admitted.
    #
    # An artifact's physical table is reused in place across refreshes, so a
    # route names a TABLE, not a GENERATION of it. Without this field the
    # execution-time guard can only re-prove whatever is LIVE, which is a
    # strictly weaker statement: the matcher's own admission proofs (the
    # pocket's ``query subset-of pocket`` containment, the aggregate's
    # grain/measure coverage) are NOT re-run at execution time, so a definition
    # edit plus a complete refresh landing inside the routing tail would be
    # accepted and the query served from a row population the admission proof
    # never covered. Carrying the stamp makes that case detectable.
    #
    # ``None`` on a source/raw route and on duck-typed decisions built by
    # internal callers; the guards treat ``None`` as "no binding available" and
    # fall back to the Bug-8392 live-only re-proof rather than losing
    # acceleration. Typed ``Any`` to avoid an IR import cycle with the routing
    # package.
    admitted_generation: Any = None
    # Bug-8788: suppress the miss-log row when the caller explicitly chose
    # source. ``force_route="source"`` is a deliberate bypass of
    # acceleration — logging it as a "miss" feeds the optimizer false
    # BUILD evidence and wastes storage/CPU on shapes the user wants live.
    log_miss: bool = True
    # Bug-8800: the grain the matcher requires, so the miss-log
    # telemetry stores the same set the matcher used (required_grain | filter_dims,
    # with DISTINCT fallback + DATE_TRUNC substitution).  Only set on source
    # decisions arriving from the aggregate matcher; None everywhere else, making
    # the existing log_query_miss derivation the fallback.
    required_grain: list[str] | None = None
    # F-004-05: filter dimension names the matcher could not cover. Empty on HIT.
    filter_columns_missing: list[str] = field(default_factory=list)


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


class DeployedSnapshotUnavailableError(SemanticBindingError):
    """Raised when a model HAS a deploy pointer but its deployed snapshot is
    unusable (missing version row, non-dict snapshot, or empty semantic shape).

    F-013-05 / F-003-03 / F-001-02 (Bug-7979): the deployed snapshot is the sole
    runtime semantic authority. A DEPLOYED model whose snapshot cannot be
    resolved must FAIL CLOSED with a typed unavailable/corrupt-deployment error —
    it must NEVER silently fall back to live/draft metadata, because that would
    expose undeployed fields/definitions to BI clients as if they were deployed.

    This is distinct from ``ModelNotDeployedError`` (no deploy pointer at all —
    a metadata-only model). Handlers map this to HTTP 503 (deployment temporarily
    unusable / corrupt) so operators can distinguish it from a 409 not-deployed.

    Subclasses ``SemanticBindingError`` so existing ``except SemanticBindingError``
    clauses keep short-circuiting the pipeline before the router/executor run.
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
