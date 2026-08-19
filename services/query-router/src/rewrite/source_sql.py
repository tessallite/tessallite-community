"""Source-path SQL construction for the query rewriter.

Builds executable SQL against the model's source database when no aggregate or
pocket route exists: the raw-SQL pass-through (``rewrite_for_source``), the
persona ``SELECT *`` expansion (``_build_persona_star_sql``), table-name
substitution (``_substitute_table_names``), and the full semantic-to-physical
star builder (``_build_source_sql``).

``_resolve_target_dialect`` and ``emit_variant_expression`` are imported here so
the test suite patches them at ``src.rewrite.source_sql.*`` (the module where
the call actually resolves).

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import inspect
import re
from typing import Any

import sqlglot
from sqlglot import exp

from shared.aggregate_quantiles import quantile_suffix_to_fraction
from shared.connector_qualify import safe_ident
from shared.schemas.measure_formats import (
    SEMI_ADDITIVE_INELIGIBLE_FAMILIES,
    TIME_VARIANT_FAMILY,
    TIME_VARIANTS_NEEDING_CALENDAR,
    is_window_variant,
)
from shared.semantic.calculated_expression import expand_safe_helpers
from shared.semantic.calendar_dialects import TABLE_BOUND_CALENDAR_TYPES
from shared.semantic.calendar_types import normalize_calendar_type
from shared.semantic.time_variants_sql import (
    VariantBinding,
    VariantSqlError,
    emit_variant_expression,
    resolve_effective_variant_anchor,
    select_finest_time_dimension,
)

from src.ir.logical_query import (
    BoundQuery,
    DeployedSnapshotUnavailableError,
    SemanticBindingError,
)
from src.rewrite.calendar_support import (
    _SA_GRAIN_RANK,
    _build_calendar_columns,
    _calendar_type_is_expression_capable,
    _is_time_dimension,
    _resolve_calendar_binding,
    _resolve_hierarchy_calendar_rules,
    _semi_additive_agg,
)
from src.rewrite.conditions import (
    _render_condition,
    _render_where,
    is_integer_col_type,
    is_numeric_col_type,
    value_is_numeric_literal,
)
from src.rewrite.dialect_resolution import _resolve_target_dialect
from src.rewrite.dialects import (
    PassthroughTranspileError,
    RewriteReparseError,
    _connector_to_dialect,
    _dialect_to_connector,
    _render_for_dialect,
    _translate_raw_sql,
    _transpile_to_dialect,
    parse_one_strict,
)
from src.rewrite.joins import (
    _build_joined_from_clause,
    _coerce_join_pair,
    _missing_join_error_message,
)
from src.rewrite.table_resolution import (
    _load_model_graph,
    _resolve_required_and_base_tables,
)
from src.rewrite.uda import _render_uda_expression

from shared.sqlglot_compat import rewrite_bigquery_exact_quantiles


# Bug-3607: the date-family physical types that can legitimately back a
# time-variant's fact_date_column. A derived numeric grain dimension (e.g.
# business_date_month = EXTRACT(MONTH FROM business_date), output INTEGER) is
# NOT a valid anchor — the period emitter would wrap the number in
# EXTRACT(YEAR/QUARTER/... FROM <numeric>), a source-side type error. The set
# mirrors shared.connector_qualify's join-coercion type families (single source
# of truth for "what is a date").
from shared.connector_qualify import _DATE_TYPES as _CQ_DATE_TYPES
from shared.connector_qualify import _TIMESTAMP_TYPES as _CQ_TIMESTAMP_TYPES
from shared.connector_qualify import normalize_type_token as _cq_normalize_type_token

_VARIANT_DATE_ANCHOR_TYPES: frozenset[str] = _CQ_DATE_TYPES | _CQ_TIMESTAMP_TYPES

# Bug-5337: semi-additive ``default_agg`` values that are semantic behaviour
# tags, NOT valid SQL aggregate function names.  When no user-requested
# aggregate overrides them (headless / plugin API, or any path without
# ``select_expressions``), the rewriter must fall back to SUM instead of
# emitting an invalid ``LAST_NON_EMPTY("t"."col")`` SQL call.
_SA_NON_SQL_AGGS: frozenset[str] = frozenset({
    "LAST_NON_EMPTY", "FIRST_NON_EMPTY", "BY_ACCOUNT", "AVG_OF_CHILDREN",
})


def _bigquery_exact_quantile_sql(pg_sql: str, target_dialect: str) -> str | None:
    """Bug-982: capability-select the BigQuery quantile rendering at the
    transpile boundary.

    A percentile falling to a BigQuery source must return an EXACT value, not a
    silent ``APPROX_QUANTILES`` approximation. EXACT mode is the default (spec
    I9: absent policy means exact), so for a BigQuery target this rewrites the
    PostgreSQL-canonical ``PERCENTILE_CONT|DISC(p) WITHIN GROUP (ORDER BY col)``
    aggregate statement to the exact analytic ``PERCENTILE_CONT|DISC(col, frac
    IGNORE NULLS) OVER (PARTITION BY <grain>)`` form with ``SELECT DISTINCT`` and
    no ``GROUP BY`` (``shared.sqlglot_compat.rewrite_bigquery_exact_quantiles``).

    Returns:
    - ``None`` when no exact-quantile rewrite applies (non-BigQuery target, the
      statement carries no percentile ordered-set aggregate, or the PG SQL could
      not be parsed here) — the caller then runs its normal transpile boundary,
      so every non-quantile query is byte-identical to before.
    - the final BigQuery SQL string when the exact rewrite fired.

    The exact analytic form uses ``IGNORE NULLS``, which the PostgreSQL dialect
    cannot represent, so the transformed AST is emitted DIRECTLY as BigQuery
    here (a PG round-trip would drop ``IGNORE NULLS`` and collapse the analytic
    call back to the ordered-set form, re-triggering the APPROX transform). The
    emission is still a single sqlglot generator pass (SQL Rule 1); identifiers
    are already PG-canonical-quoted and the generator re-quotes them to BigQuery
    backticks.

    Propagates :class:`ExactQuantileUnavailable` (reason
    ``EXACT_QUANTILE_UNAVAILABLE``) when the statement shape cannot be rendered
    exactly, so the caller surfaces a typed error rather than a wrong number.
    """
    if target_dialect != "bigquery":
        return None
    try:
        tree = sqlglot.parse_one(pg_sql, read="postgres")
    except Exception:
        # Unparseable here — defer to the caller's normal transpile boundary.
        # Only percentile statements are affected and those are AST-built and
        # parseable, so a parse failure means there is nothing to rewrite.
        return None
    if not any(
        isinstance(n.this, (exp.PercentileCont, exp.PercentileDisc))
        for n in tree.find_all(exp.WithinGroup)
    ):
        return None
    tree = rewrite_bigquery_exact_quantiles(tree)
    # Codex gate R3: route through _render_for_dialect so WEEK->ISOWEEK fires.
    return _render_for_dialect(tree, "bigquery")


def _dim_anchors_a_date(dim: Any, columns_by_id: dict) -> bool:
    """True when *dim* is backed by a physical DATE/TIMESTAMP source column.

    A UDA-derived or calc-expression time dimension (whose value is a numeric
    period part such as month-of-year) is NOT date-anchored and cannot serve as
    a variant's fact_date_column (Bug-3607).
    """
    src_col_id = getattr(dim, "source_column_id", None)
    if src_col_id is None:
        return False
    mc = columns_by_id.get(src_col_id)
    if mc is None:
        return False
    # Normalise verbose spellings ("timestamp without time zone",
    # "TIMESTAMP(3)") to the canonical token before the membership check —
    # otherwise a genuine timestamp column is rejected as a date anchor and
    # the caller falls through to a sibling scan that can pick an unrelated
    # (possibly unresolvable) time dimension.
    dtype = _cq_normalize_type_token(getattr(mc, "data_type", None))
    return dtype in _VARIANT_DATE_ANCHOR_TYPES


def _finest_time_dimension_in_grain(
    resolved_dimensions: list,
    grain_set: set,
    fallback_time_dim: Any,
) -> Any:
    """Return the finest ranked time dimension present in the query grain."""
    return select_finest_time_dimension(resolved_dimensions, grain_set) or fallback_time_dim


def _resolve_variant_date_anchor(
    *,
    time_dim: Any,
    resolved_dimensions: list,
    candidate_dimensions: list | tuple | None = None,
    columns_by_id: dict,
    get_phys_expr,
    measure_name: str,
    pg_canonical: bool,
    resolved_date_col_id: Any = None,
    alias_by_table_id: dict | None = None,
    with_type: bool = False,
    window_anchor_col_id: Any = None,
    is_window: bool = False,
):
    """Resolve the physical DATE expression that anchors a time variant.

    Bug-3607: the dispatch previously passed the first grain time dimension's
    physical expression as ``fact_date_column`` unconditionally. For a derived
    numeric time dim (e.g. ``business_date_month``) that expression is a NUMBER,
    and the period emitter then renders ``EXTRACT(... FROM <numeric>)`` — a
    source-side ``extract(unknown, numeric) does not exist`` error.

    Resolution order:
      0a. **F-015-01** — for a pure-window variant (``is_window``), the
         modeller-selected ``window_anchor_col_id`` (``date_dimension_column_id``)
         is the AUTHORITATIVE ORDER BY anchor. If it maps to a
         DATE/TIMESTAMP-typed ModelColumn in the join graph, use it; if it is
         set but cannot be resolved to such a column, fail loud rather than
         silently fall back to a hierarchy-derived date (which would order the
         window by a different business date and change lag/trailing/moving-avg
         answers). The hierarchy-derived ``resolved_date_col_id`` is NOT
         consulted for window variants.
      0b. **Bug-5247** — otherwise (period-aware variant), if
         ``resolved_date_col_id`` is provided and maps to a
         DATE/TIMESTAMP-typed ModelColumn in ``columns_by_id``, use it directly.
         This is the denormalized shortcut set at variant-creation time by
         model-service and avoids the dimension-scan heuristic below.
      1. If ``time_dim`` is itself DATE-anchored, use it.
      2. Otherwise find a sibling DATE-anchored time dimension among the query's
         resolved dimensions or bound dimension map and use that.
      3. Otherwise raise a typed ``SemanticBindingError`` naming the usable date
         dimension (fail loud — never wrap a number in EXTRACT).

    Returns the resolved physical expression (already wrapped by the caller in
    ``MIN(...)``). When ``with_type`` is True, returns a
    ``(expr, source_data_type)`` tuple instead — the raw ``data_type`` of the
    SAME ModelColumn the anchor resolved to, so the calendar JOIN coerces the
    correct side (Codex finding: the JOIN previously derived its type from the
    SELECTED grain time dimension, which can differ from the anchor when
    ``resolved_date_col_id`` points at a timestamp on another column while the
    grain dim is a virtual/derived numeric part).
    """
    def _ret(expr: str, dtype: Any):
        return (expr, dtype) if with_type else expr

    # F-015-01: pure-window variants order by the modeller-selected
    # date_dimension_column_id, NOT the hierarchy-derived resolved_date_col_id.
    # The selected column is authoritative and validated fail-loud so a
    # misconfigured window can never silently order by a different date.
    if is_window:
        if window_anchor_col_id is not None and alias_by_table_id is not None:
            mc = columns_by_id.get(window_anchor_col_id)
            if mc is None:
                raise SemanticBindingError(
                    f"Window variant {measure_name!r} selects date column "
                    f"{window_anchor_col_id!r} for ordering, but that column is "
                    f"not present in the model snapshot. Re-resolve the "
                    f"variant's date dimension column."
                )
            dtype = _cq_normalize_type_token(getattr(mc, "data_type", None))
            if dtype not in _VARIANT_DATE_ANCHOR_TYPES:
                raise SemanticBindingError(
                    f"Window variant {measure_name!r} selects date column "
                    f"{mc.column_name!r} for ordering, but its type "
                    f"{getattr(mc, 'data_type', None)!r} is not a DATE/TIMESTAMP. "
                    f"Select a date/timestamp column for window ordering."
                )
            alias = alias_by_table_id.get(mc.model_table_id)
            if not alias:
                raise SemanticBindingError(
                    f"Window variant {measure_name!r} orders by column "
                    f"{mc.column_name!r}, but its table is not in the query's "
                    f"FROM/JOIN graph. Add the date dimension's table to the "
                    f"join path."
                )
            _qid = safe_ident  # Bug-7023: defense-in-depth
            return _ret(
                f'{_qid(alias)}.{_qid(mc.column_name)}',
                getattr(mc, "data_type", None),
            )
        # No explicit window date column configured: fall through to the
        # time-grain dimension / sibling-date heuristic (legacy window rows
        # created before date_dimension_column_id existed).

    # Bug-5247: honour the denormalized resolved_date_col_id when available.
    elif resolved_date_col_id is not None and alias_by_table_id is not None:
        mc = columns_by_id.get(resolved_date_col_id)
        if mc is not None:
            dtype = _cq_normalize_type_token(getattr(mc, "data_type", None))
            if dtype in _VARIANT_DATE_ANCHOR_TYPES:
                alias = alias_by_table_id.get(mc.model_table_id)
                if alias:
                    _qid = safe_ident  # Bug-7023: defense-in-depth
                    return _ret(
                        f'{_qid(alias)}.{_qid(mc.column_name)}',
                        getattr(mc, "data_type", None),
                    )

    if _dim_anchors_a_date(time_dim, columns_by_id):
        phys = get_phys_expr(time_dim.name, pg_canonical=pg_canonical)
        if phys is None:
            raise SemanticBindingError(
                f"Cannot resolve physical column for time dimension "
                f"{time_dim.name!r}."
            )
        _mc = columns_by_id.get(getattr(time_dim, "source_column_id", None))
        return _ret(phys, getattr(_mc, "data_type", None) if _mc else None)

    candidates = list(resolved_dimensions)
    if candidate_dimensions:
        seen = {id(d) for d in candidates}
        for dim in candidate_dimensions:
            if id(dim) not in seen:
                candidates.append(dim)
                seen.add(id(dim))

    same_hierarchy = [
        d for d in candidates
        if _is_time_dimension(d)
        and d.name != time_dim.name
        and getattr(d, "hierarchy_id", None) is not None
        and getattr(d, "hierarchy_id", None) == getattr(time_dim, "hierarchy_id", None)
        and _dim_anchors_a_date(d, columns_by_id)
    ]
    sibling = next(iter(same_hierarchy), None) or next(
        (
            d for d in candidates
            if _is_time_dimension(d)
            and d.name != time_dim.name
            and _dim_anchors_a_date(d, columns_by_id)
        ),
        None,
    )
    if sibling is not None:
        phys = get_phys_expr(sibling.name, pg_canonical=pg_canonical)
        if phys is None:
            raise SemanticBindingError(
                f"Cannot resolve physical column for time dimension "
                f"{sibling.name!r}."
            )
        _mc = columns_by_id.get(getattr(sibling, "source_column_id", None))
        return _ret(phys, getattr(_mc, "data_type", None) if _mc else None)

    raise SemanticBindingError(
        f"Time variant {measure_name!r} is grouped by the derived time "
        f"dimension {time_dim.name!r}, which is a numeric period part and "
        f"cannot anchor period math. Add the underlying date dimension "
        f"(a DATE/TIMESTAMP-backed time dimension) to the query grain so the "
        f"variant can resolve its date anchor."
    )


async def rewrite_for_source(bound_query: BoundQuery, db: Any = None, *, target_dialect: str | None = None) -> str:
    """
    Build a SQL query against the source table.

    Pass through the raw query unchanged when:
    - The query has passthrough expressions (e.g. SELECT * FROM subquery)
    - The query uses SELECT * (binder expanded all dims/measures but the
      original SQL should execute as-is against the source)

    Otherwise, if resolved measures/dimensions are available, build a proper
    SQL query using physical table/column names.
    """
    # F-27: Accept pre-resolved dialect to skip redundant DB lookup.
    if target_dialect is not None:
        _target_dialect = target_dialect
    elif db is not None:
        try:
            _target_dialect = await _resolve_target_dialect(db, bound_query.model.id)
        except Exception:
            _target_dialect = "postgres"
    else:
        _target_dialect = "postgres"
    _connector = _dialect_to_connector(_target_dialect)

    if getattr(bound_query, "has_passthrough_expressions", False):
        # Passthrough: raw SQL kept as-is, but substitute semantic table
        # names with physical names so the query can execute against the DB.
        rewritten = await _substitute_table_names(bound_query, db, _connector)
        if rewritten:
            return rewritten
        # Bug-904: apply dialect translation so passthrough SQL is not returned
        # in PostgreSQL syntax when the target is a different connector.
        return _translate_raw_sql(
            bound_query.logical_query.raw_query, _target_dialect,
            getattr(bound_query.logical_query, "input_dialect", "postgres"),
        )
    if bound_query.logical_query.select_star:
        if getattr(bound_query, "persona_narrowed_star", False) and db is not None:
            return await _build_persona_star_sql(bound_query, db, _connector, _target_dialect)
        rewritten = await _substitute_table_names(bound_query, db, _connector)
        if rewritten:
            return rewritten
        # Bug-904: apply dialect translation so SELECT * fallback SQL is not
        # returned in PostgreSQL syntax when the target is a different connector.
        return _translate_raw_sql(
            bound_query.logical_query.raw_query, _target_dialect,
            getattr(bound_query.logical_query, "input_dialect", "postgres"),
        )
    from_tables = getattr(bound_query.logical_query, "from_tables", [])
    if db is not None and (bound_query.resolved_measures or bound_query.resolved_dimensions or from_tables):
        return await _build_source_sql(bound_query, db, target_dialect=_target_dialect)
    # Final fallback: still try table name substitution before returning raw.
    rewritten = await _substitute_table_names(bound_query, db, _connector)
    if rewritten:
        return rewritten
    # Bug-904: apply dialect translation so the final raw-query fallback is not
    # returned in PostgreSQL syntax when the target is a different connector.
    return _translate_raw_sql(
            bound_query.logical_query.raw_query, _target_dialect,
            getattr(bound_query.logical_query, "input_dialect", "postgres"),
        )


async def _build_persona_star_sql(bound_query: BoundQuery, db: Any, connector: str = "postgresql", target_dialect: str = "postgres") -> str:
    """Expand a persona/CLS-narrowed ``SELECT *`` to explicit allowed columns.

    Called when a persona allow-list (``persona_gate``) or CLS tag narrowing
    (``_check_column_restrictions``, F-008-05) dropped hidden/restricted
    columns from the resolved lists of a ``SELECT *`` query. Instead of sending
    a raw ``SELECT *`` to the source (which would READ every physical column,
    including the ones just removed), this builds an EXPLICIT projection of the
    persona/CLS-allowed dimensions and measures, spanning the base (fact) table
    AND any joined dimension tables, then resolves WHERE/GROUP BY/ORDER BY
    against that allowed set.

    Bug-4467 (AKA Bug-809): a referenced column the persona does NOT permit
    causes a clean **403** rejection — the function NEVER falls back to a raw
    ``SELECT *`` that would read a hidden/restricted column. When narrowing
    leaves no projectable allowed column at all, it likewise rejects with 403
    (mirroring the CLS up-front rejection in ``_check_column_restrictions``).

    Bug-913: SQL is built in PostgreSQL-canonical form (double-quoted
    identifiers, ANSI syntax) throughout, then translated to the target
    dialect by ``_final_transpile`` at the single return boundary.
    """
    from fastapi import HTTPException

    # Local helpers — always emit PostgreSQL-canonical double-quoted form.
    # Bug-7023: use safe_ident (escapes embedded double-quotes) instead of
    # bare f'"{name}"' to prevent SQL injection via malicious identifiers.
    def _qid(name: str) -> str:
        return safe_ident(name)

    def _qtbl(dotted: str) -> str:
        return ".".join(safe_ident(p) for p in dotted.split("."))

    def _final_transpile(pg_sql: str) -> str:
        return _transpile_to_dialect(pg_sql, target_dialect)

    def _reject_no_allowed_columns() -> None:
        # No projectable allowed column remains for this restricted persona —
        # reject cleanly instead of a raw SELECT * that reads hidden columns.
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "COLUMN_RESTRICTED",
                "message": (
                    "This star query resolves to no column this persona is "
                    "permitted to read, so there is nothing to return."
                ),
            },
        )

    def _reject_restricted_reference(ref_name: str) -> None:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "COLUMN_RESTRICTED",
                "message": (
                    f"This query references column {ref_name!r}, which is not "
                    "readable for this persona, so it was rejected."
                ),
            },
        )

    # F-013-01 residual (Lane A2): resolve the base table + physical columns +
    # UDAs from the DEPLOYED snapshot for a deployed model (live ORM only for an
    # undeployed model). A draft table/column/UDA edit must not change a deployed
    # persona star before the next deploy. Fail-closed: a deployed model whose
    # snapshot cannot resolve yields no base table -> reject (never a raw star).
    from src.rewrite.snapshot_graph_resolvers import (
        resolve_base_table,
        resolve_columns_by_id,
        resolve_udas_by_id,
    )

    base_table = await resolve_base_table(
        bound_query.model,
        db,
        deployed_shape=getattr(bound_query, "deployed_shape", None),
    )
    if base_table is None:
        # No physical table to project against — a restricted persona's star
        # must not degrade to a raw SELECT * (Bug-4467/809).
        _reject_no_allowed_columns()

    # Collect source_column_ids and UDA ids from allowed objects.
    col_ids: set = set()
    uda_ids: set = set()
    for obj in list(bound_query.resolved_dimensions) + list(bound_query.resolved_measures):
        src = getattr(obj, "source_column_id", None)
        if src:
            col_ids.add(src)
        uid = getattr(obj, "user_defined_attribute_id", None)
        if uid:
            uda_ids.add(uid)

    if not col_ids and not uda_ids:
        # Nothing allowed resolves to a physical column or UDA — reject.
        _reject_no_allowed_columns()

    columns_by_id: dict = await resolve_columns_by_id(
        bound_query.model,
        db,
        col_ids,
        deployed_shape=getattr(bound_query, "deployed_shape", None),
    )
    uda_by_id: dict = await resolve_udas_by_id(
        bound_query.model,
        db,
        uda_ids,
        deployed_shape=getattr(bound_query, "deployed_shape", None),
    )

    # Bug-4467 (AKA Bug-809): the previous build projected ONLY fact-table
    # columns and, whenever a WHERE/GROUP BY/ORDER BY referenced a column that
    # lived on a joined dimension table, fell back to a RAW ``SELECT *`` via
    # ``_substitute_table_names``. That raw star READS every physical column
    # from the source — including the hidden/restricted columns the persona
    # (or CLS narrowing) just removed — and only the post-execute audit scrub
    # blocked egress AFTER the values were read. The fix builds an EXPLICIT
    # projection of the persona/CLS-allowed columns spanning the base table
    # AND any joined dimension tables, resolves every clause against that
    # allowed set, and REJECTS with 403 when a clause references a non-allowed
    # column — never a raw ``SELECT *``.
    #
    # Load the full model graph (tables + joins + all columns) so allowed
    # columns on joined dimension tables can be projected and qualified.
    tables_by_id, joins, all_columns_by_id, _uda_graph = await _load_model_graph(
        bound_query, db, uda_ids,
    )
    # Merge the targeted UDA load (allowed objects) with the graph UDAs so
    # joined-table UDAs are also resolvable.
    for _uid, _ua in (_uda_graph or {}).items():
        uda_by_id.setdefault(_uid, _ua)

    # Resolve a stable alias per table. The base table keeps its model alias
    # (or "base"); joined tables use their model alias (or a deterministic
    # fallback). Mirrors the aliasing in ``_build_source_sql``.
    alias_by_table_id: dict = {}

    def _alias_for(table_id: Any) -> str:
        a = alias_by_table_id.get(table_id)
        if a:
            return a
        tbl = tables_by_id.get(table_id)
        if str(table_id) == str(base_table.id):
            a = (getattr(tbl, "alias", None) if tbl else None) or getattr(base_table, "alias", None) or "base"
        else:
            a = (getattr(tbl, "alias", None) if tbl else None) or f"t_{len(alias_by_table_id)}"
        alias_by_table_id[table_id] = a
        return a

    _alias_for(base_table.id)

    def _qcol(alias: str, col: str) -> str:
        return f'{_qid(alias)}.{_qid(col)}'

    # Build the explicit column list across base + joined relations, and the
    # semantic-name → qualified-physical map for WHERE/GROUP BY/ORDER BY.
    seen: set[str] = set()
    select_parts: list[str] = []
    # Tables (besides base) that the projection / clauses require a JOIN to.
    required_table_ids: set = {base_table.id}
    # semantic name (lower) → qualified physical reference ("alias"."col" or
    # a UDA expression). Spans base + joined relations.
    name_to_physical: dict[str, str] = {}
    # Bug-6123: semantic name (lower) → source column type, so the persona-star
    # WHERE renderer applies the SAME numeric-literal typing the general path
    # enforces (a numeric filter value against an INT/NUMERIC column must render
    # as a bare token, never a quoted string, or it silently mis-compares on a
    # strictly-typed connector). Populated alongside name_to_physical.
    name_to_coltype: dict[str, str] = {}
    # lowercase physical column name → qualified physical reference, for the
    # raw-ORDER-BY reconstruction path (column names only, no object ids).
    colname_to_physical: dict[str, str] = {}

    for obj in list(bound_query.resolved_dimensions) + list(bound_query.resolved_measures):
        src_id = getattr(obj, "source_column_id", None)
        col = columns_by_id.get(src_id) or all_columns_by_id.get(src_id) if src_id else None
        if col is not None:
            _t_id = col.model_table_id
            if _t_id not in tables_by_id and str(_t_id) != str(base_table.id):
                # Column maps to a table outside the model graph — cannot
                # qualify or join it safely. Skip projection (still allowed
                # for clause resolution only if it is the base table).
                continue
            _alias = _alias_for(_t_id)
            required_table_ids.add(_t_id)
            _qualified = _qcol(_alias, col.column_name)
            name_to_physical.setdefault(obj.name.lower(), _qualified)
            _col_dtype = getattr(col, "data_type", None)
            if _col_dtype is not None:
                name_to_coltype.setdefault(obj.name.lower(), _col_dtype)
            colname_to_physical.setdefault(col.column_name.lower(), _qualified)
            _proj_key = (str(_t_id), col.column_name)
            if _proj_key not in seen:
                seen.add(_proj_key)
                select_parts.append(
                    f'{_qualified} AS {_qid(obj.name)}'
                    if col.column_name != obj.name
                    else _qualified
                )
            continue
        uid = getattr(obj, "user_defined_attribute_id", None)
        if uid and uid in uda_by_id:
            uda = uda_by_id[uid]
            _t_id = uda.table_id
            if _t_id not in tables_by_id and str(_t_id) != str(base_table.id):
                continue
            _alias = _alias_for(_t_id)
            required_table_ids.add(_t_id)
            if obj.name not in seen:
                seen.add(obj.name)
                # F-006-02: stored UDA expressions may carry backtick quoting
                # (BigQuery/MySQL style, e.g. EXTRACT(YEAR FROM `full_date`))
                # from legacy/imported rows. sqlglot's postgres parser rejects
                # backticks (ParseError); normalise to PG-canonical, qualify
                # against the owning table's alias, and on any residual parse
                # failure reject (never raw SELECT *) so a hidden column is
                # never read.
                try:
                    rendered = _render_uda_expression(
                        expression=uda.expression,
                        table_alias=_alias,
                        target_dialect="postgres",
                    )
                    select_parts.append(f'({rendered}) AS {_qid(obj.name)}')
                    name_to_physical.setdefault(obj.name.lower(), f'({rendered})')
                except Exception:
                    _reject_restricted_reference(obj.name)

    if not select_parts:
        # No projectable allowed column remains — reject cleanly (403) instead
        # of falling back to a raw SELECT * (Bug-4467/809).
        _reject_no_allowed_columns()

    # Build the FROM clause. When every allowed/required column lives on the
    # base table, a single-table FROM is correct (and identical to the prior
    # behaviour). When allowed columns span joined tables, build the join
    # graph so those columns can be qualified and read — without reverting to
    # a raw star.
    if required_table_ids - {base_table.id}:
        from_clause = _build_joined_from_clause(
            base_table_id=base_table.id,
            required_table_ids=required_table_ids,
            joins=joins,
            tables_by_id=tables_by_id,
            columns_by_id=all_columns_by_id,
            alias_by_table_id=alias_by_table_id,
            connector="postgresql",
        )
        if not from_clause:
            # The persona-allowed columns span tables that cannot be joined.
            # Refuse rather than read them via a raw star.
            _reject_restricted_reference("(joined dimension)")
        sql = f"SELECT {', '.join(select_parts)} FROM {from_clause}"
    else:
        base_alias = _alias_for(base_table.id)
        table_ref = _qtbl(base_table.physical_name)
        sql = f"SELECT {', '.join(select_parts)} FROM {table_ref} AS {_qid(base_alias)}"

    # Render WHERE from resolved_filters. Every clause is resolved against the
    # persona/CLS-allowed column map (which now spans base + joined relations).
    # A filter on a column the persona does NOT permit is REJECTED with 403 —
    # never falls back to a raw SELECT * (Bug-4467/809). A value-probing filter
    # on a restricted column is already blocked upstream (F-008-11), so reaching
    # an unresolved filter here means the column is genuinely not allowed.
    # ``name_to_physical`` values are fully-qualified ("alias"."col" or a UDA
    # expression) — emit them directly, do NOT re-quote.
    lq = bound_query.logical_query
    filters = getattr(bound_query, "resolved_filters", None) or []
    if filters:
        where_parts: list[str] = []
        for f in filters:
            phys = name_to_physical.get(f.dimension_name.lower())
            if not phys:
                _reject_restricted_reference(f.dimension_name)
            # Bug-6123: pass the column type so numeric literals are typed (bare
            # token vs quoted string) exactly as the general/aggregate paths do,
            # and Bug-6383: carry the LIKE escape char so contains/notContains
            # emit a portable ESCAPE clause.
            where_parts.append(
                _render_condition(
                    phys, f.operator, f.value,
                    name_to_coltype.get(f.dimension_name.lower()),
                    like_escape=getattr(f, "like_escape", None),
                    like_target_connector=connector,
                    # Bug-7918: ``name_to_coltype`` holds the SOURCE's own type
                    # spelling, so the tz-awareness of a bare ``timestamp``
                    # token can only be read against the source dialect.
                    source_connector=connector,
                )
            )
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)

    # Render GROUP BY from grain. A grain column the persona does not permit is
    # rejected with 403 rather than degraded to a raw SELECT *.
    grain = getattr(lq, "grain", None) or []
    if grain:
        group_parts = []
        for g in grain:
            phys = name_to_physical.get(g.lower())
            if not phys:
                _reject_restricted_reference(g)
            group_parts.append(phys)
        if group_parts:
            sql += " GROUP BY " + ", ".join(group_parts)

    # Render ORDER BY.
    # F-003-01 / Bug-1605: when the parser flagged the ORDER BY as unresolvable
    # (an expression sort key — LOWER(region), SUM(a)/COUNT(*), CASE — that the
    # IR cannot represent as a bare column), the extracted ``order_by`` list is
    # partial, so we must NOT emit it. We reconstruct the FULL raw ORDER BY from
    # the original SQL, mapping each referenced column to its physical column via
    # the persona-allowed maps (now spanning base + joined relations) — mirrors
    # the general path's raw-ORDER-BY preservation (``_build_source_sql``).
    #
    # Safety gate (Bug-1605): the raw expression may reference a column the
    # persona excluded. Reconstructing over such a column would leak it, so we
    # only emit the ORDER BY when EVERY column it references is persona-allowed.
    # When a referenced column is NOT allowed, we cannot safely order — and we
    # must also SUPPRESS the LIMIT: dropping the sort while keeping LIMIT returns
    # an ARBITRARY top-N (a silent wrong result, the exact F-003-01 class).
    # Returning the full unordered allowed set is correct-if-larger, never
    # silently-wrong, and never reads the excluded column.
    def _resolve_order_col(name: str) -> str | None:
        return name_to_physical.get(name.lower()) or colname_to_physical.get(name.lower())

    _suppress_limit = False
    if getattr(lq, "has_unresolvable_order", False):
        order_by = []
        raw_order = _extract_raw_order_node(lq)
        order_cols_allowed = raw_order is not None
        if raw_order is not None:
            for _node in raw_order.find_all(exp.Column):
                if _resolve_order_col(_node.name) is None:
                    order_cols_allowed = False
                    break
        if order_cols_allowed:
            def _qualify_order(node):
                if isinstance(node, exp.Column):
                    phys = _resolve_order_col(node.name)
                    if phys:
                        return sqlglot.parse_one(phys, read="postgres")
                return node
            rendered = raw_order.transform(_qualify_order).sql(dialect="postgres")
            sql += f" {rendered}"
        else:
            # Cannot safely reproduce the sort (references an excluded column).
            # Drop the LIMIT too so we never return a wrong top-N. The excluded
            # column is never emitted, so nothing leaks and nothing restricted
            # is read.
            _suppress_limit = True
    else:
        order_by = getattr(lq, "order_by", None) or []
    if order_by:
        order_parts = []
        for col_name, direction in order_by:
            phys = _resolve_order_col(col_name)
            if not phys:
                _reject_restricted_reference(col_name)
            d = "DESC" if direction.upper() == "DESC" else "ASC"
            order_parts.append(f"{phys} {d}")
        if order_parts:
            sql += " ORDER BY " + ", ".join(order_parts)

    if lq.limit is not None and not _suppress_limit:
        sql += f" LIMIT {lq.limit}"
    # An OFFSET without a stable sort selects an arbitrary window — drop it for
    # the same reason as the LIMIT when the sort could not be reproduced.
    if lq.offset is not None and not _suppress_limit:
        sql += f" OFFSET {lq.offset}"

    return _final_transpile(sql)


def _extract_raw_order_node(logical_query: Any) -> Any:
    """Re-parse the raw SQL and return its top-level ``ORDER BY`` node.

    Used by the F-003-01 raw-ORDER-BY preservation path: when the parser
    flagged ``has_unresolvable_order`` the extracted ``order_by`` list is
    partial, so the rewriter reconstructs the sort from the original SQL's
    ORDER BY node verbatim (with semantic→physical column substitution).
    Mirrors the raw-WHERE re-parse, including the subquery-wrapper case where
    the ORDER BY may live inside the wrapped SELECT.
    """
    _input_dialect = getattr(logical_query, "input_dialect", "postgres")
    # F-006-03: reparse strictly. A recovered tree from malformed SQL could
    # reproduce an altered ORDER BY (silently different sort -> arbitrary
    # top-N). RewriteReparseError fails loud; other parse issues (a tree that
    # simply has no ORDER BY node) still return None below.
    try:
        raw_ast = parse_one_strict(logical_query.raw_query, read=_input_dialect)
    except RewriteReparseError:
        raise
    except Exception:
        return None
    raw_select = raw_ast if isinstance(raw_ast, exp.Select) else raw_ast.find(exp.Select)
    if raw_select is None:
        return None
    order = raw_select.args.get("order")
    if order is None:
        from_clause = raw_select.args.get("from_")
        if from_clause is not None and isinstance(from_clause.this, exp.Subquery):
            inner = from_clause.this.this
            if isinstance(inner, exp.Select):
                order = inner.args.get("order")
    return order


async def _substitute_table_names(
    bound_query: BoundQuery,
    db: Any,
    connector: str = "postgresql",
) -> str | None:
    """Replace semantic table names with physical table names in raw SQL.
    Used for SELECT * queries where we keep the star but fix the FROM clause.
    Returns None if substitution cannot be performed."""
    if db is None:
        return None

    from_tables = getattr(bound_query.logical_query, "from_tables", [])
    # Even if from_tables is empty (e.g. malformed AST from WARN-mode parse),
    # proceed if the raw SQL contains the model slug — we can still substitute.
    model_slug_check = (getattr(bound_query.model, "slug", "") or "").lower()
    if not from_tables and model_slug_check and model_slug_check not in bound_query.logical_query.raw_query.lower():
        return None

    # F-013-01 residual (Lane A2): resolve the physical tables from the DEPLOYED
    # snapshot for a deployed model (live ORM only for undeployed). A draft
    # physical-name change must not alter production table substitution before
    # the next deploy. Fail closed: a deployed model with an unresolvable
    # snapshot yields no tables -> return None (no substitution).
    from src.rewrite.snapshot_graph_resolvers import resolve_all_tables
    try:
        all_tables = await resolve_all_tables(
            bound_query.model,
            db,
            deployed_shape=getattr(bound_query, "deployed_shape", None),
        )
    except DeployedSnapshotUnavailableError:
        # Bug-7981: a BLOCKED deployment must stay loud on every rewrite path.
        # Returning None here means "no substitution", which sends the raw
        # SEMANTIC table name to the source instead of failing closed — the
        # exact fail-open the typed error exists to prevent.
        raise
    except Exception:
        return None

    if not all_tables:
        return None

    # Pick the base/fact table for substitution.  Prefer the fact table;
    # fall back to the first table in canonical order if no fact table exists.
    # Bug-8605: through the shared rule rather than a fourth local copy of it —
    # a second, independent anchor expression is how these sites drift apart.
    from shared.semantic.graph_order import pick_anchor_table

    base_table = pick_anchor_table(all_tables)

    # Build a set of names to match against the SQL table reference.
    # Only match the MODEL SLUG — NOT the display name (which can collide
    # with real physical table names, causing wrong-table substitution —
    # Bug-7915) and NOT individual table aliases.  Dim tables referenced by
    # their physical names (e.g. demo_data.dim_payment_status) should not
    # be substituted.
    model_slug = (getattr(bound_query.model, "slug", "") or "").lower()
    # Bug-7915: display_name removed from match_names.  A single-word
    # display name (e.g. "sales") that equals a physical table referenced
    # in a JOIN was silently substituted, routing the query to the wrong
    # table.  The slug is the stable programmatic identifier and is always
    # present in FROM when the model is referenced.
    match_names: set[str] = {model_slug}
    # Persona-suffixed references (e.g. modely_technical, modely_business)
    # appear in FROM clauses when the query targets a persona view. Add
    # them so subqueries / CTEs referencing the persona name are rewritten.
    if model_slug:
        for ft in getattr(bound_query.logical_query, "from_tables", []) or []:
            ft_lower = ft.lower()
            if ft_lower.startswith(model_slug + "_") or ft_lower == model_slug:
                match_names.add(ft_lower)
    match_names.discard("")

    # Use regex-based replacement for the table-name swap itself (avoids a
    # full-statement sqlglot rewrite at this stage and keeps the SQL byte-exact
    # except for the FROM/JOIN table reference).
    #
    # Bug-5339 / Bug-5340: substitute the physical table in PostgreSQL-CANONICAL
    raw = bound_query.logical_query.raw_query
    physical_parts = base_table.physical_name.split(".")

    # Bug-7915 / Codex gate: AST-based table substitution.  Substitute only
    # exp.Table identifier nodes whose name matches the model slug (from
    # match_names).  This inherently protects string literals, dollar-quoted
    # strings, E-strings, and comments -- the structural boundary the
    # Codex review requires.  Falls back to the regex path ONLY when the
    # AST parse fails (and even then only for non-string-literal-quoting
    # dialects where un-transpiled PG SQL is acceptable).
    _input_dialect = getattr(
        bound_query.logical_query, "input_dialect", "postgres",
    )
    _match_lower = {n.lower() for n in match_names if n}
    _sub_dialect = _connector_to_dialect(connector)

    # Bug-7915 / Codex gate: AST-based table substitution for ALL targets.
    # Substitute only exp.Table identifier nodes whose name matches the
    # model slug. This structural boundary inherently protects string
    # literals, dollar-quoted strings, E-strings, and comments.

    # Codex gate R2 finding 4: track whether _replace_table actually fired
    # so a no-op substitution returns None (byte-identical-when-off).
    _sub_fired = False

    def _replace_table(node):
        nonlocal _sub_fired
        if isinstance(node, exp.Table):
            if node.name.lower() in _match_lower:
                # Codex gate R3 finding 3: identity mapping (slug ==
                # physical table name) is a semantic no-op; skip to
                # preserve byte-identical-when-off.
                _node_full = node.name.lower()
                if node.args.get("db"):
                    _node_full = f"{node.args['db'].name.lower()}.{_node_full}"
                _phys_full = ".".join(p.lower() for p in physical_parts)
                if _node_full == _phys_full:
                    return node  # Identity: no change needed.
                _sub_fired = True
                # Codex gate R3 finding 2: preserve the table alias.
                _alias = node.args.get("alias")
                if len(physical_parts) == 2:
                    return exp.Table(
                        this=exp.to_identifier(physical_parts[1], quoted=True),
                        db=exp.to_identifier(physical_parts[0], quoted=True),
                        alias=_alias,
                    )
                return exp.Table(
                    this=exp.to_identifier(physical_parts[0], quoted=True),
                    alias=_alias,
                )
        return node

    try:
        tree = sqlglot.parse_one(raw, read=_input_dialect)
        tree = tree.transform(_replace_table)
        if not _sub_fired:
            return None  # No table matched: byte-identical-when-off.

        if _sub_dialect in ("postgres", "postgresql"):
            # Postgres target: emit as postgres (no dialect translation).
            result = tree.sql(dialect="postgres")
        elif _input_dialect in ("postgres", "postgresql") or _input_dialect is None:
            # PG-canonical input, non-postgres target: emit as PG-canonical
            # first, then route through _transpile_to_dialect which applies
            # all pre-generation AST rewrites (WEEK->ISOWEEK, semi-additive
            # Spark rewrite, fail-loud policies).
            pg_sql = tree.sql(dialect="postgres")
            result = _transpile_to_dialect(pg_sql, _sub_dialect)
        else:
            # Non-PG-authored input (e.g. BQ-authored): emit via
            # _render_for_dialect with pg_canonical=False so the author's
            # explicit WEEK/ISOWEEK intent is preserved.
            result = _render_for_dialect(tree, _sub_dialect, pg_canonical=False)
    except (PassthroughTranspileError, SemanticBindingError):
        raise
    except Exception:
        # AST parse failed. For BQ/Spark the downstream fail-loud policy
        # catches unparseable SQL via _translate_raw_sql.
        return None

    return result


async def _build_no_columns_sql(
    bound_query: BoundQuery, db: Any, target_dialect: str,
) -> str:
    """Build SQL for a query that resolves to no physical columns/UDAs.

    Covers SELECT TRUE / SELECT COUNT(*) and single-table passthrough by
    substituting the semantic FROM table with the model's physical base
    table.  Always returns a final-transpiled SQL string.
    """
    # Bug-7023: use safe_ident to escape embedded quotes in identifiers.
    def _qid(name: str) -> str:
        return safe_ident(name)

    def _qtbl(dotted: str) -> str:
        return ".".join(safe_ident(p) for p in dotted.split("."))

    def _final_transpile(pg_sql: str) -> str:
        return _transpile_to_dialect(pg_sql, target_dialect)

    from_tables = getattr(bound_query.logical_query, "from_tables", [])
    # For single-table queries with no resolvable columns (e.g.
    # SELECT TRUE, SELECT COUNT(*)), substitute the semantic table
    # name with the model's physical base table.
    base_table = None
    if len(from_tables) <= 1:
        # F-013-01 residual (Lane A2): resolve the base table from the DEPLOYED
        # snapshot for a deployed model (live ORM only for undeployed). Prefer the
        # fact table so COUNT(*) counts fact rows, not dim rows (Bug-119). Fail
        # closed: a deployed model with an unresolvable snapshot yields no base
        # table (the query falls through to its no-base path).
        from src.rewrite.snapshot_graph_resolvers import resolve_base_table
        try:
            base_table = await resolve_base_table(
                bound_query.model,
                db,
                deployed_shape=getattr(bound_query, "deployed_shape", None),
            )
            if inspect.isawaitable(base_table):
                base_table = await base_table
        except DeployedSnapshotUnavailableError:
            # Bug-7981: see _substitute_table_names — falling through here
            # returns the raw semantic SQL for a blocked deployment.
            raise
        except Exception:
            base_table = None
        if base_table is not None and not isinstance(getattr(base_table, "physical_name", None), str):
            base_table = None
        if base_table:
            has_row_count = any(m.name == "__row_count" for m in bound_query.resolved_measures)
            if has_row_count:
                # Preserve the user's original alias (e.g. COUNT(*) AS cnt);
                # if none given, use "count" to match Postgres's default
                # column naming instead of leaking the internal
                # "__row_count" sentinel (Bug-920). Mirrors the measure-loop
                # path below.
                _rc_alias = "count"
                for _e in getattr(bound_query.logical_query, "select_expressions", []):
                    if _e.classification == "literal" and _e.agg_function == "count" and _e.alias:
                        _rc_alias = _e.alias
                        break
                table_ref = _qtbl(base_table.physical_name)
                count_sql = f"SELECT COUNT(*) AS {_qid(_rc_alias)} FROM {table_ref}"
                if bound_query.logical_query.limit is not None:
                    count_sql += f" LIMIT {bound_query.logical_query.limit}"
                if bound_query.logical_query.offset is not None:
                    count_sql += f" OFFSET {bound_query.logical_query.offset}"
                return _final_transpile(count_sql)

            # General case: replace the FROM table name in the raw SQL
            # with the physical table reference.
            if from_tables:
                raw = bound_query.logical_query.raw_query
                input_dialect = getattr(bound_query.logical_query, "input_dialect", "postgres")
                try:
                    # Bug-7916 / Codex gate: use default error level (not
                    # WARN) so a recovered/meaning-changed tree (e.g.
                    # WHERE x = 1 !! -> WHERE x = NOT 1) is never produced.
                    tree = sqlglot.parse_one(raw, read=input_dialect)
                    def _replace_table(node):
                        if isinstance(node, exp.Table) and node.name == from_tables[0]:
                            parts = base_table.physical_name.split(".")
                            if len(parts) == 2:
                                return exp.Table(this=exp.to_identifier(parts[1], quoted=True),
                                                 db=exp.to_identifier(parts[0], quoted=True))
                            return exp.Table(this=exp.to_identifier(parts[0], quoted=True))
                        return node
                    return _final_transpile(tree.transform(_replace_table).sql(dialect="postgres"))
                except Exception:
                    pass
    return _final_transpile(bound_query.logical_query.raw_query)


def _qualify_calc_expression(
    calc_expression: str,
    columns_by_id: dict,
    alias_by_table_id: dict,
    *,
    pg_canonical: bool = True,
) -> str:
    """Parse a calculated-dimension expression and qualify each column with its
    table alias, returning a parenthesised PostgreSQL-canonical expression.

    Shared by ``_build_dimension_select_pieces`` (SELECT / GROUP BY) and
    ``_get_phys_expr`` (WHERE / PARTITION BY / HAVING) so a calc dimension
    renders to the SAME physical expression everywhere (F-006-04). Previously
    ``_get_phys_expr`` returned the SELECT *alias* for calc dims, which is
    invalid in WHERE / PARTITION BY / HAVING (PostgreSQL and BigQuery reject
    SELECT aliases there) — producing a "column does not exist" source error
    or, worse, binding to a physical column that happens to share the name.

    Raises ``SemanticBindingError`` if the expression cannot be parsed (the
    same fail-loud contract the SELECT path already enforced), so a calc
    dimension is never silently dropped from a predicate.
    """
    try:
        _calc_tree = sqlglot.parse_one(calc_expression, read="postgres")
    except Exception as exc:
        raise SemanticBindingError(
            f"Cannot resolve calculated dimension expression "
            f"{calc_expression!r}: failed to parse: {exc}"
        ) from exc

    def _qualify_calc(node, _cols=columns_by_id, _aliases=alias_by_table_id):
        if isinstance(node, exp.Column):
            for _c in _cols.values():
                if _c.column_name.lower() == node.name.lower():
                    _a = _aliases.get(_c.model_table_id)
                    if _a:
                        return exp.column(node.name, table=_a, quoted=True)
        return node

    return f"({_calc_tree.transform(_qualify_calc).sql(dialect='postgres')})"


def _build_dimension_select_pieces(
    bound_query: BoundQuery,
    *,
    columns_by_id: dict,
    tables_by_id: dict,
    alias_by_table_id: dict,
    uda_expr_by_id: dict,
    _se_list: list,
    _standalone_dim_names: set,
    grain_names_set: set,
    _dim_user_alias: dict,
    _position_for_column,
) -> tuple:
    """Render bare/grain dimension SELECT pieces and GROUP BY expressions.

    Returns ``(select_pieces, dim_group_exprs, dim_group_expr_by_name,
    field_expr_by_name)`` contributions for the caller to merge.  All
    identifier quoting is PostgreSQL-canonical (double quotes).
    """
    # Bug-7023: use safe_ident to escape embedded quotes in identifiers.
    def _qid(name: str) -> str:
        return safe_ident(name)

    def _qcol(alias: str, col: str) -> str:
        return f'{safe_ident(alias)}.{safe_ident(col)}'

    select_pieces: list[tuple[int, str]] = []
    dim_group_exprs: list[str] = []
    dim_group_expr_by_name: dict[str, str] = {}
    field_expr_by_name: dict[str, str] = {}
    for dim in bound_query.resolved_dimensions:
        mc = columns_by_id.get(getattr(dim, "source_column_id", None))
        if mc is not None:
            alias = alias_by_table_id.get(mc.model_table_id)
            if not alias:
                # Phase 2 fail-loud (supersedes the Bug-893 bare-identifier
                # fallback): the dimension's physical column maps to a table
                # that is not in the query's FROM/JOIN graph.  Emitting an
                # unqualified identifier would mask the binding failure and
                # risk wrong results, so refuse and raise instead.
                raise SemanticBindingError(
                    f"Cannot resolve dimension {dim.name!r}: its physical "
                    f"column {mc.column_name!r} belongs to a table that is "
                    f"not in the query's FROM/JOIN graph."
                )
            expr = _qcol(alias, mc.column_name)
        elif getattr(dim, "calc_expression", None):
            # Phase 2 fail-loud: a calculated dimension whose expression cannot
            # be parsed must not be silently dropped from SELECT / GROUP BY
            # (which would change results). _qualify_calc_expression raises
            # SemanticBindingError on parse failure. Shared with _get_phys_expr
            # so WHERE / PARTITION BY / HAVING render the SAME expression
            # (F-006-04).
            try:
                expr = _qualify_calc_expression(
                    dim.calc_expression, columns_by_id, alias_by_table_id,
                )
            except SemanticBindingError as exc:
                raise SemanticBindingError(
                    f"Cannot resolve calculated dimension {dim.name!r}: {exc}"
                ) from exc
        else:
            uda_id = getattr(dim, "user_defined_attribute_id", None)
            expr_body = uda_expr_by_id.get(uda_id)
            if not expr_body:
                # Phase 2 fail-loud (supersedes the Bug-893 bare-name
                # fallback): the dimension has no resolvable physical mapping
                # (orphaned source_column_id or missing/unjoined UDA).
                # Refuse to emit a bare semantic identifier.
                raise SemanticBindingError(
                    f"Cannot resolve dimension {dim.name!r} to a physical "
                    f"column: no source column, calculated expression, or "
                    f"user-defined attribute mapping was found."
                )
            expr = f"({expr_body})"
        # Only add as standalone SELECT column if the dim appears as a bare
        # column in the original SELECT.  Grain-only dimensions (in GROUP BY
        # but not in SELECT) are included in dim_group_exprs for GROUP BY
        # but not duplicated into SELECT — the passthrough loop will emit
        # any complex expressions (CASE, COALESCE, etc.) that reference them.
        # Grain dimensions that ARE also bare standalone SELECT columns are
        # emitted here; grain dimensions that are ONLY referenced inside
        # complex expressions are NOT emitted here (Bug-879).
        is_standalone = dim.name in _standalone_dim_names
        # Add grain-only dims (in GROUP BY but nowhere in SELECT) so
        # PostgreSQL doesn't reject them.  But skip dims that will be
        # emitted by the passthrough loop via a complex expression.
        is_grain_only = (
            dim.name in grain_names_set
            and dim.name not in _standalone_dim_names
            and not any(
                _e.classification == "passthrough"
                and _e.inner_column == dim.name
                and not _e.agg_function
                and _e.inner_column not in _standalone_dim_names
                for _e in _se_list
            )
        )
        if is_standalone or is_grain_only:
            pos = _position_for_column(dim.name)
            out_alias = _dim_user_alias.get(dim.name, dim.name)
            select_pieces.append((pos, f"{expr} AS {_qid(out_alias)}"))
        dim_group_exprs.append(expr)
        dim_group_expr_by_name[dim.name] = expr  # Bug-874
        field_expr_by_name[dim.name] = _qid(dim.name)
    return select_pieces, dim_group_exprs, dim_group_expr_by_name, field_expr_by_name


def _build_measure_select_pieces(
    bound_query: BoundQuery,
    *,
    columns_by_id: dict,
    alias_by_table_id: dict,
    uda_expr_by_id: dict,
    _variant_base_map: dict,
    calendar_columns: dict | None,
    _variant_calendar_type: str | None,
    _variant_fiscal_start: int | None,
    candidate_dimensions: list,
    calc_parsed_by_name: dict,
    calc_ref_measures_by_name: dict,
    _se_list: list,
    grain_names_set: set,
    _sa_has_time_in_grain: bool,
    _sa_finest_time_col_id: Any,
    _qid,
    _qcol,
    _pg_qcol,
    _get_phys_expr,
    _position_for_column,
) -> tuple:
    """Render measure SELECT pieces (extracted from ``_build_source_sql``;
    Phase 3 internal decomposition, behaviour-identical).

    Returns ``(select_pieces, field_expr_by_name, _variant_extra_group_by)``
    to be merged into the caller's accumulators.
    """
    select_pieces: list[tuple[int, str]] = []
    field_expr_by_name: dict[str, str] = {}
    _variant_extra_group_by: list[str] = []
    _TAIL = 10**9  # sentinel for items appended after SELECT items
    # Identify measures handled by passthrough expressions (e.g. SUM(col * 2))
    # so the measure loop below can skip them and avoid duplicate SELECT items.
    passthrough_measure_names: set[str] = set()
    for e in _se_list:
        if e.classification == "passthrough" and e.inner_column and e.agg_function:
            passthrough_measure_names.add(e.inner_column)

    # Bug-1066: a composable aggregate expression (e.g. SUM(fee)/SUM(base),
    # CASE/COALESCE/ratio over aggregates) is rendered IN FULL by the
    # literal/passthrough loop. Its COMPONENT columns (fee, base) are recorded
    # as ``requested_measures`` by the parser so the matcher knows every column
    # the expression needs — but on the SOURCE path the measure loop must NOT
    # also emit each component as a standalone ``SUM(fee) AS fee`` column, or
    # the result leaks extra columns (``?column?, fee, base`` instead of one).
    # Collect the component columns of every composable expression, then skip
    # any that does NOT also appear as a standalone analytical/literal measure
    # of its own (a measure used both inside a composite AND on its own stays).
    _composable_component_names: set[str] = set()
    for e in _se_list:
        if getattr(e, "composable", False):
            for _col, _fn in getattr(e, "inner_aggregates", []) or []:
                if _col and _col != "__row_count":
                    _composable_component_names.add(_col)
    _standalone_measure_names: set[str] = set()
    for e in _se_list:
        if e.classification == "analytical" and e.inner_column:
            _standalone_measure_names.add(e.inner_column)
        elif e.classification == "literal" and e.agg_function == "count":
            _standalone_measure_names.add("__row_count")
    passthrough_measure_names |= (
        _composable_component_names - _standalone_measure_names
    )

    # Build measure → user alias map from select_expressions so we can
    # preserve user-provided aliases (e.g. SUM(amount) AS total_amount).
    _measure_user_alias: dict[str, str] = {}
    for _e in _se_list:
        if _e.classification == "analytical" and _e.inner_column and _e.alias:
            _measure_user_alias[_e.inner_column] = _e.alias

    # Build measure → user-requested agg map so AVG/MIN/MAX queries are not
    # silently coerced to the measure's stored default_agg (Bug-118).
    _measure_requested_agg: dict[str, str] = {}
    for _e in _se_list:
        if _e.classification == "analytical" and _e.inner_column and _e.agg_function:
            _measure_requested_agg[_e.inner_column] = _e.agg_function
    # Bug-7796: merge DAX inline aggregate overrides (from
    # measure_agg_overrides on LogicalQuery). These carry the REQUESTED
    # aggregate function from inline DAX expressions like SUM(Sales[Amount])
    # so the rewriter applies SUM even when the measure's default_agg differs.
    # The override keys are the column names as parsed from DAX (may differ
    # in case from the resolved measure's canonical name), so we match them
    # case-insensitively against the resolved measures to re-key correctly.
    # select_expressions-based overrides take precedence (SQL protocol).
    _dax_agg_overrides = getattr(
        bound_query.logical_query, "measure_agg_overrides", None
    )
    if _dax_agg_overrides:
        # Build a case-insensitive lookup from override keys to canonical names.
        _override_lower = {k.lower(): v for k, v in _dax_agg_overrides.items()}
        for _meas in bound_query.resolved_measures:
            _canonical = _meas.name
            _func = _override_lower.get(_canonical.lower())
            if _func and _canonical not in _measure_requested_agg:
                _measure_requested_agg[_canonical] = _func

    # Bug-1066: a SCALAR-WRAPPED aggregate (ROUND(AVG(x), 2),
    # CAST(AVG(x) AS NUMERIC(18,4)), ABS(SUM(x)), …) is classified
    # "analytical" by the parser — inner_column=x, agg_function=avg — so it can
    # route to a matching aggregate. But on the SOURCE path the measure loop
    # below only re-emitted ``AGG(x)`` via ``_wrap_agg`` and DROPPED the outer
    # ROUND/CAST/ABS wrapper, returning the raw aggregate (wrong number, not
    # just wrong type). Capture the wrapped raw_text per measure so the loop can
    # render the FULL expression with the aggregate's inner column substituted
    # by its physical column. Keyed by inner_column; only stored when the
    # raw_text is NOT the bare ``AGG(col)`` form (a true wrapper).
    # Keyed by inner_column (single-occurrence measures) AND by select-
    # expression index (so the multi-agg branch — SELECT CAST(SUM(x)),
    # CAST(AVG(x)) where the same measure appears twice — can preserve each
    # wrapper independently).
    _measure_scalar_wrapped_raw: dict[str, str] = {}
    _idx_scalar_wrapped_raw: dict[int, str] = {}
    for _idx_sw, _e in enumerate(_se_list):
        if (
            _e.classification == "analytical"
            and _e.inner_column
            and _e.agg_function
            and _e.raw_text
        ):
            _raw_norm = _e.raw_text.strip()
            # Strip a trailing "AS alias" for the bare-form comparison.
            _raw_no_alias = re.sub(
                r"\s+AS\s+\S+$", "", _raw_norm, flags=re.IGNORECASE
            ).strip()
            try:
                _raw_ast = sqlglot.parse_one(_raw_no_alias, read="postgres")
                _top = _raw_ast.this if isinstance(_raw_ast, exp.Alias) else _raw_ast
                # Bare aggregate (SUM(x)/AVG(x)/COUNT(DISTINCT x)/…) → top node
                # is the AggFunc itself; nothing to preserve. A scalar wrapper
                # (Func/Cast/arithmetic) around the aggregate → preserve it.
                _is_bare_agg = isinstance(_top, exp.AggFunc)
            except Exception:
                _is_bare_agg = True
            if not _is_bare_agg:
                _measure_scalar_wrapped_raw[_e.inner_column] = _raw_no_alias
                _idx_scalar_wrapped_raw[_idx_sw] = _raw_no_alias

    # Bug-880: Build ordered list of (measure_name, agg_func, alias, pos)
    # for *every* analytical select_expression so that queries like
    # SELECT MIN(x), MAX(x) produce two SELECT columns, not one.
    # When a measure appears in multiple expressions, we emit one SELECT
    # piece per expression (not per resolved measure) and disambiguate
    # aliases using the same _emitted_aliases pattern as rewrite_for_aggregate.
    _multi_agg_expressions: list[tuple[str, str, str | None, int]] = []
    _measure_expr_count: dict[str, int] = {}
    for _idx, _e in enumerate(_se_list):
        if _e.classification == "analytical" and _e.inner_column:
            _multi_agg_expressions.append(
                (_e.inner_column, _e.agg_function or "", _e.alias, _idx)
            )
            _measure_expr_count[_e.inner_column] = (
                _measure_expr_count.get(_e.inner_column, 0) + 1
            )
    _has_multi_agg_measures = any(c > 1 for c in _measure_expr_count.values())
    _emitted_aliases: set[str] = set()

    def _phys_for_measure(ref_meas: Any) -> str | None:
        mc = columns_by_id.get(getattr(ref_meas, "source_column_id", None))
        if mc is not None:
            a = alias_by_table_id.get(mc.model_table_id)
            if a:
                return _qcol(a, mc.column_name)
        uda_id = getattr(ref_meas, "user_defined_attribute_id", None)
        if uda_id:
            body = uda_expr_by_id.get(uda_id)
            if body:
                return f"({body})"
        return None

    def _wrap_agg(agg: str, inner: str) -> str:
        # Median/percentile: the internal marker is a pNN stat suffix, not a
        # real SQL function. When the query cannot be served from a quantile
        # aggregate column (coarser grain, or a source whose quantiles are
        # approximate), render the native PERCENTILE_CONT so the source query
        # is valid SQL — emitting `P50(col)` would error (no such function).
        _frac = quantile_suffix_to_fraction((agg or "").lower())
        if _frac is not None:
            return f"PERCENTILE_CONT({_frac:g}) WITHIN GROUP (ORDER BY {inner})"
        agg = (agg or "SUM").upper()
        if agg == "COUNT_DISTINCT":
            return f"COUNT(DISTINCT {inner})"
        if agg == "COUNT":
            return f"COUNT({inner})"
        return f"{agg}({inner})"

    def _calc_agg_or_fail(
        agg: str, measure_name: str, ref_name: str | None = None
    ) -> str:
        # F-015-12: inside a calculated-measure expansion, a semi-additive
        # ``default_agg`` token (LAST_NON_EMPTY, FIRST_NON_EMPTY, …) carried by
        # a measure whose ``semi_additive_behavior`` is unset — a data
        # inconsistency from an import or a row created before the save-time
        # gate existed — was previously replaced with SUM.  That silently turns
        # a point-in-time balance into a meaningless total of period snapshots:
        # a wrong number with no error.  Fail loud instead, mirroring the
        # semi-additive-reference gate above; the row must be repaired
        # (revalidation marks it ``is_invalid``), never summed.  The normal
        # (non-calc) measure path keeps the documented Bug-5337 SUM fallback,
        # which fires only as a last resort when the model has no time-ordering
        # column and is not a calc expansion.
        if (agg or "").upper() in _SA_NON_SQL_AGGS:
            if ref_name is not None:
                raise SemanticBindingError(
                    f"Calculated measure {measure_name!r} references "
                    f"{ref_name!r}, whose default aggregation {agg!r} is a "
                    f"semi-additive balance token but no semi_additive_behavior "
                    f"is set; it cannot be safely aggregated inside a calculated "
                    f"expression. Repair the measure definition — its "
                    f"aggregation is inconsistent."
                )
            raise SemanticBindingError(
                f"Calculated measure {measure_name!r} has default aggregation "
                f"{agg!r}, a semi-additive balance token but no "
                f"semi_additive_behavior is set; it cannot be aggregated as the "
                f"outer step of a per-row calculated measure. Repair the measure "
                f"definition — its aggregation is inconsistent."
            )
        return agg

    for meas in bound_query.resolved_measures:
        if meas.name in passthrough_measure_names:
            continue
        if getattr(meas, "measure_type", None) == "calculated":
            parsed = calc_parsed_by_name.get(meas.name)
            if parsed is None:
                raise SemanticBindingError(
                    f"Calculated measure {meas.name!r} is missing its "
                    "parsed expression."
                )
            mode = getattr(meas, "calc_agg_mode", None) or "expression_as_written"

            replacements: dict[str, str] = {}
            for ref in parsed.references:
                ref_meas = calc_ref_measures_by_name.get(ref.name)
                if ref_meas is None:
                    raise SemanticBindingError(
                        f"Calculated measure {meas.name!r} references "
                        f"unknown measure {ref.name!r}."
                    )
                # F-015-04 (defence-in-depth): the expansion below resolves a
                # reference to the raw snapshot column wrapped in the default
                # agg, ignoring variant_kind, so a calc that references a
                # time-variant measure would silently compute against the
                # variant's BASE (e.g. measure("revenue_ytd") -> SUM(revenue)).
                # Save-time validation now rejects such references; this
                # fail-loud guard catches any measure persisted before that
                # gate existed rather than returning a silently wrong number.
                if getattr(ref_meas, "variant_kind", None) is not None:
                    raise SemanticBindingError(
                        f"Calculated measure {meas.name!r} references the "
                        f"time-variant measure {ref.name!r}; variant references "
                        f"cannot be computed inside a calculated measure. "
                        f"Reference the base measure instead."
                    )
                # Bug-7183: a semi-additive measure (last_non_empty,
                # first_non_empty, etc.) represents a point-in-time balance,
                # not a summable flow.  The expansion below resolves each
                # reference to ``AGG(physical_col)`` using default_agg; for a
                # semi-additive measure the default_agg is a non-SQL token
                # (LAST_NON_EMPTY, etc.) that was previously silently replaced
                # with SUM — producing wrong numbers (e.g. turning an ending
                # balance into a meaningless total of monthly snapshots).
                # Fail loud, mirroring the variant-reference gate above.
                # A model-service save-time gate should also prevent creation
                # of such references (scope_request).
                _ref_sa_behavior = getattr(ref_meas, "semi_additive_behavior", None)
                if _ref_sa_behavior:
                    raise SemanticBindingError(
                        f"Calculated measure {meas.name!r} references the "
                        f"semi-additive measure {ref.name!r} "
                        f"(behavior: {_ref_sa_behavior}); semi-additive "
                        f"measures cannot be used inside a calculated "
                        f"expression because their aggregation semantics "
                        f"(point-in-time balance) cannot be preserved in a "
                        f"formula. Reference a non-semi-additive measure "
                        f"instead."
                    )
                phys = _phys_for_measure(ref_meas)
                if phys is None:
                    raise SemanticBindingError(
                        f"Cannot resolve physical expression for "
                        f"{ref.name!r} referenced by {meas.name!r}."
                    )
                if mode == "per_row_then_aggregate":
                    replacements[ref.placeholder] = phys
                else:
                    ref_agg = getattr(ref_meas, "default_agg", None) or "sum"
                    # F-015-12: was a silent SUM fallback for a semi-additive
                    # default_agg token with no semi_additive_behavior — a wrong
                    # number.  Now fail-loud (see _calc_agg_or_fail).
                    ref_agg = _calc_agg_or_fail(ref_agg, meas.name, ref.name)
                    replacements[ref.placeholder] = _wrap_agg(ref_agg, phys)

            def _substitute(node: exp.Expression) -> exp.Expression:
                if isinstance(node, exp.Column) and node.name in replacements:
                    return sqlglot.parse_one(replacements[node.name], read="postgres")
                return node

            expanded_ast = parsed.ast.copy().transform(_substitute)
            expanded_ast = expand_safe_helpers(expanded_ast)
            try:
                expanded_sql = expanded_ast.sql(dialect="postgres")
            except Exception as exc:
                raise SemanticBindingError(
                    f"Failed to render calculated measure {meas.name!r}: {exc}"
                ) from exc

            if mode == "per_row_then_aggregate":
                outer_agg = getattr(meas, "default_agg", None) or "sum"
                # F-015-12: fail-loud on an inconsistent semi-additive outer
                # aggregation token instead of silently summing.
                outer_agg = _calc_agg_or_fail(outer_agg, meas.name)
                expanded_sql = _wrap_agg(outer_agg, expanded_sql)

            # F-015-16: this branch is currently unreachable from persisted
            # rows — the Pydantic gate (dimensions_measures.py) rejects
            # variant_kind on calculated measures at both create and update,
            # so meas.variant_kind is always None for a calculated measure.
            # Retained (not deleted, sensitive rewriter) as the ready
            # implementation should a "variant of calculated" feature be
            # wired in future; the gate is pinned by tests/test_calc_variants.py.
            calc_variant_kind = getattr(meas, "variant_kind", None)
            if calc_variant_kind is not None:
                # Bug-6222 guard (calculated variant path — currently
                # unreachable, retained for future "variant of calculated").
                _sa_behavior_calc = getattr(meas, "semi_additive_behavior", None)
                _calc_family = TIME_VARIANT_FAMILY.get(calc_variant_kind)
                if (
                    _sa_behavior_calc
                    and _calc_family in SEMI_ADDITIVE_INELIGIBLE_FAMILIES
                ):
                    raise SemanticBindingError(
                        f"Measure {meas.name!r} is semi-additive "
                        f"(behavior: {_sa_behavior_calc}) and cannot use the "
                        f"{calc_variant_kind!r} variant (family: {_calc_family}). "
                        f"Cumulation and window variants are not supported for "
                        f"semi-additive measures because they represent balances, "
                        f"not flows; cumulating them produces incorrect results. "
                        f"Use lag or prior-period variants instead."
                    )
                _grain_set_calc = set(bound_query.logical_query.grain)
                _time_dim_fallback_calc = next(
                    (d for d in bound_query.resolved_dimensions
                     if _is_time_dimension(d)),
                    None,
                )
                time_dim = _finest_time_dimension_in_grain(
                    bound_query.resolved_dimensions,
                    _grain_set_calc,
                    _time_dim_fallback_calc,
                )
                if time_dim is None:
                    raise SemanticBindingError(
                        f"Time variant {meas.name!r} requires a time dimension "
                        f"in the query grain; none was found."
                    )
                other_dims = [
                    d for d in bound_query.resolved_dimensions
                    if not _is_time_dimension(d) and d.name in _grain_set_calc
                ]
                pg_partition_exprs_calc: list[str] = []
                for d in other_dims:
                    phys = _get_phys_expr(d.name, pg_canonical=True)
                    if phys is None:
                        raise SemanticBindingError(
                            f"Cannot resolve physical column for grain dimension "
                            f"{d.name!r} required by calculated variant {meas.name!r}."
                        )
                    pg_partition_exprs_calc.append(phys)
                # Bug-3607: anchor on a DATE/TIMESTAMP column, never a derived
                # numeric grain dim.
                # Bug-5247: prefer the denormalized resolved_date_col_id when
                # set on the measure.
                _calc_is_window = is_window_variant(calc_variant_kind)
                _calc_anchor = resolve_effective_variant_anchor(meas, time_dim)
                pg_time_phys = _resolve_variant_date_anchor(
                    time_dim=time_dim,
                    resolved_dimensions=bound_query.resolved_dimensions,
                    candidate_dimensions=candidate_dimensions,
                    columns_by_id=columns_by_id,
                    get_phys_expr=_get_phys_expr,
                    measure_name=meas.name,
                    pg_canonical=True,
                    resolved_date_col_id=(
                        _calc_anchor.effective_column_id
                        if not _calc_is_window else None
                    ),
                    alias_by_table_id=alias_by_table_id,
                    is_window=_calc_is_window,
                    window_anchor_col_id=(
                        _calc_anchor.effective_column_id
                        if _calc_is_window else None
                    ),
                )
                pg_time_phys = f"MIN({pg_time_phys})"
                pg_expanded_sql = expanded_ast.sql(dialect="postgres")
                if mode == "per_row_then_aggregate":
                    outer_agg = getattr(meas, "default_agg", None) or "sum"
                    # F-015-12: fail-loud (see _calc_agg_or_fail).  This
                    # calc-variant branch is itself unreachable from persisted
                    # rows (F-015-16 Pydantic gate); kept uniform with the
                    # reachable per-row path above.
                    outer_agg = _calc_agg_or_fail(outer_agg, meas.name)
                    pg_expanded_sql = _wrap_agg(outer_agg, pg_expanded_sql)
                variant_dialect = "postgresql"
                # Bug-6681: choose the finest time grain in the GROUP BY set,
                # independent of projection/GROUP BY ordering.
                _grain_time_dim_calc = _finest_time_dimension_in_grain(
                    bound_query.resolved_dimensions,
                    _grain_set_calc,
                    time_dim,
                )
                _resolved_time_grain_calc = getattr(
                    _grain_time_dim_calc, "time_grain", None
                )
                try:
                    variant_result = emit_variant_expression(
                        calc_variant_kind,
                        VariantBinding(
                            base_expression=pg_expanded_sql,
                            base_unaggregated=None,
                            fact_date_column=pg_time_phys,
                            calendar_alias="cal",
                            calendar_columns=calendar_columns,
                            dialect=variant_dialect,
                            n=getattr(meas, "variant_n", None),
                            partition_by=tuple(pg_partition_exprs_calc),
                            calendar_type=_variant_calendar_type,
                            fiscal_year_start_month=_variant_fiscal_start,
                            time_grain=_resolved_time_grain_calc,
                        ),
                    )
                except VariantSqlError as exc:
                    raise SemanticBindingError(
                        f"Failed to emit time variant {meas.name!r}: {exc}"
                    ) from exc
                if calendar_columns and variant_result.referenced_calendar_keys:
                    for key in variant_result.referenced_calendar_keys:
                        col_name = calendar_columns.get(key)
                        if col_name:
                            expr = f"{_qid('cal')}.{_qid(col_name)}"
                            if expr not in _variant_extra_group_by:
                                _variant_extra_group_by.append(expr)
                out_alias = _measure_user_alias.get(meas.name, meas.name)
                pos = _position_for_column(meas.name)
                select_pieces.append((pos, f"{variant_result.sql} AS {_qid(out_alias)}"))
                field_expr_by_name[meas.name] = _qid(out_alias)
                if out_alias != meas.name:
                    field_expr_by_name[out_alias] = _qid(out_alias)
            else:
                out_alias = _measure_user_alias.get(meas.name, meas.name)
                pos = _position_for_column(meas.name)
                select_pieces.append((pos, f"{expanded_sql} AS {_qid(out_alias)}"))
                field_expr_by_name[meas.name] = _qid(out_alias)
                if out_alias != meas.name:
                    field_expr_by_name[out_alias] = _qid(out_alias)
            continue

        if meas.name == "__row_count":
            # Preserve the user's original alias (e.g. COUNT(*) AS cnt);
            # if none given, use "count" to match Postgres's default column
            # naming instead of leaking the internal "__row_count" sentinel.
            _rc_alias = "count"
            _rc_pos = _TAIL
            for _idx, _e in enumerate(_se_list):
                if _e.classification == "literal" and _e.agg_function == "count":
                    _rc_pos = _idx
                    if _e.alias:
                        _rc_alias = _e.alias
                    break
            select_pieces.append((_rc_pos, f"COUNT(*) AS {_qid(_rc_alias)}"))
            field_expr_by_name["__row_count"] = _qid(_rc_alias)
            continue

        _effective_meas = _variant_base_map.get(meas.name, meas)
        mc = columns_by_id.get(getattr(_effective_meas, "source_column_id", None))
        if mc is not None:
            alias = alias_by_table_id.get(mc.model_table_id)
            if not alias:
                # Phase 2 fail-loud (measure twin of the Finding 2
                # dimension fallback): the measure's physical column maps to
                # a table that is not in the FROM/JOIN graph.  Silently
                # dropping the measure would return wrong results, so raise.
                raise SemanticBindingError(
                    f"Cannot resolve measure {meas.name!r}: its physical "
                    f"column {mc.column_name!r} belongs to a table that is "
                    f"not in the query's FROM/JOIN graph."
                )
            phys_name = _qcol(alias, mc.column_name)
        else:
            uda_id = getattr(_effective_meas, "user_defined_attribute_id", None)
            expr_body = uda_expr_by_id.get(uda_id)
            if not expr_body:
                # Phase 2 fail-loud: measure has no resolvable physical
                # mapping (orphaned source_column_id or missing/unjoined
                # UDA).  Refuse to silently drop it.
                raise SemanticBindingError(
                    f"Cannot resolve measure {meas.name!r} to a physical "
                    f"column: no source column or user-defined attribute "
                    f"mapping was found."
                )
            phys_name = f"({expr_body})"
        # Bug-880: When this measure appears in multiple analytical
        # select_expressions (e.g. SELECT MIN(x), MAX(x)), emit one SELECT
        # piece per expression instead of collapsing to a single column.
        if _has_multi_agg_measures and _measure_expr_count.get(meas.name, 0) > 1:
            _exprs_for_meas = [
                t for t in _multi_agg_expressions if t[0] == meas.name
            ]
            for _m_name, _m_agg, _m_alias, _m_pos in _exprs_for_meas:
                _m_agg_upper = (_m_agg or meas.default_agg or "SUM").upper()
                if _m_agg_upper in _SA_NON_SQL_AGGS:
                    _m_agg_upper = "SUM"
                # Disambiguate: explicit alias first, then measure name if
                # not yet used, then fall back to agg function name.
                if _m_alias:
                    _chosen = _m_alias
                elif _m_name not in _emitted_aliases:
                    _chosen = _m_name
                else:
                    _chosen = _m_agg.lower() if _m_agg else _m_name
                _emitted_aliases.add(_chosen)
                # Bug-1066: preserve a scalar wrapper (CAST/ROUND/ABS …) on the
                # specific expression at this index — the same measure can
                # appear twice with different wrappers (CAST(SUM(x)),
                # CAST(AVG(x))), so resolve per select-expression index.
                _m_wrapped = _idx_scalar_wrapped_raw.get(_m_pos)
                _m_piece = None
                if _m_wrapped is not None:
                    try:
                        _mw_ast = sqlglot.parse_one(_m_wrapped, read="postgres")
                        _mp_node = sqlglot.parse_one(phys_name, read="postgres")

                        def _sub_m_col(node, _pn=_mp_node):
                            if isinstance(node, exp.Column):
                                return _pn.copy()
                            return node

                        _m_piece = _mw_ast.transform(_sub_m_col).sql(dialect="postgres")
                    except Exception:
                        _m_piece = None
                if _m_piece is None:
                    _m_piece = _wrap_agg(_m_agg_upper, phys_name)
                select_pieces.append((_m_pos, f"{_m_piece} AS {_qid(_chosen)}"))
                field_expr_by_name[_chosen] = _qid(_chosen)
            continue

        _raw_agg = (_measure_requested_agg.get(meas.name) or meas.default_agg or "SUM").upper()
        # Bug-5337: semi-additive default_agg values are semantic behaviour
        # tags, not SQL functions.  Fall back to SUM when no user-requested
        # aggregate overrides them.  Bug-7192: the semi-additive code path
        # above now fires at all grains (not just when time is in the grain),
        # so this fallback is reached only when no time ordering column is
        # available in the model — a last resort.
        agg = "SUM" if _raw_agg in _SA_NON_SQL_AGGS else _raw_agg
        out_alias = _measure_user_alias.get(meas.name, meas.name)
        pos = _position_for_column(meas.name)

        # Phase 2 Step 4 — Time-variant dispatch. Variant measures carry
        # `variant_kind` as a first-class column. Pure-window variants
        # (lag/trailing_n/moving_avg_n) wrap the aggregate with a window
        # ordered by the time-grain dimension and require no FROM-clause
        # change. Period-aware variants additionally consume the calendar
        # JOIN injected above (Bug-090 Part B) via calendar_columns.
        # Phase 6 DAX bridge: fall back to time_variant_hints from the
        # LogicalQuery when the ORM measure has no variant_kind (DAX path).
        variant_kind = getattr(meas, "variant_kind", None)
        if variant_kind is None:
            _dax_hints = getattr(bound_query.logical_query, "time_variant_hints", None)
            if _dax_hints:
                variant_kind = _dax_hints.get(meas.name)
        if variant_kind is not None:
            # Bug-6222 (F-015-27): semi-additive measures (balances) must
            # not be combined with cumulation/window variant families
            # (period_to_date, moving_window).  These families sum per-period
            # balances instead of carrying the period-end balance forward,
            # producing SUM(SUM(balance)) OVER -- meaningless running totals.
            # Fail loud rather than return silently wrong numbers.
            _sa_behavior_variant = getattr(meas, "semi_additive_behavior", None)
            _variant_family = TIME_VARIANT_FAMILY.get(variant_kind)
            if (
                _sa_behavior_variant
                and _variant_family in SEMI_ADDITIVE_INELIGIBLE_FAMILIES
            ):
                raise SemanticBindingError(
                    f"Measure {meas.name!r} is semi-additive "
                    f"(behavior: {_sa_behavior_variant}) and cannot use the "
                    f"{variant_kind!r} variant (family: {_variant_family}). "
                    f"Cumulation and window variants are not supported for "
                    f"semi-additive measures because they represent balances, "
                    f"not flows; cumulating them produces incorrect results. "
                    f"Use lag or prior-period variants instead."
                )
            # Bug-6229 [correctness]: COUNT_DISTINCT is NON-ADDITIVE. Cumulation
            # and moving-window variants wrap the per-period base aggregate in a
            # window SUM (period_to_date -> SUM(COUNT(DISTINCT x)) OVER ...;
            # moving_window likewise). Summing per-period distinct counts is not
            # the distinct count over the cumulative window (a customer active in
            # two months is counted twice), so it silently returns a wrong,
            # inflated number. Fail loud rather than serve it. Parallel-period
            # and lag families reference a single prior period's distinct count,
            # which is correct, so they stay allowed.
            if (
                agg == "COUNT_DISTINCT"
                and _variant_family in SEMI_ADDITIVE_INELIGIBLE_FAMILIES
            ):
                raise SemanticBindingError(
                    f"Measure {meas.name!r} is a distinct count (count_distinct), "
                    f"which is not additive, and cannot use the {variant_kind!r} "
                    f"variant (family: {_variant_family}). Cumulation and "
                    f"moving-window variants sum the per-period distinct counts, "
                    f"which double-counts values that recur across periods and "
                    f"produces an inflated, incorrect running total. Use a "
                    f"prior-period or lag variant instead, or base the "
                    f"cumulation on an additive measure (sum/count)."
                )
            _grain_set = set(bound_query.logical_query.grain)
            _time_dim_fallback = next(
                (d for d in bound_query.resolved_dimensions
                 if _is_time_dimension(d)),
                None,
            )
            time_dim = _finest_time_dimension_in_grain(
                bound_query.resolved_dimensions,
                _grain_set,
                _time_dim_fallback,
            )
            if time_dim is None:
                raise SemanticBindingError(
                    f"Time variant {meas.name!r} requires a time dimension "
                    f"in the query grain; none was found in resolved_dimensions."
                )
            # Bug-090 Part C: every non-time grain dimension must appear in
            # PARTITION BY so window functions don't bleed across slices.
            # Filter to grain only — resolved_dimensions may include
            # SELECT-only columns that are not in GROUP BY.
            other_dims = [
                d for d in bound_query.resolved_dimensions
                if not _is_time_dimension(d) and d.name in _grain_set
            ]
            pg_partition_exprs: list[str] = []
            for d in other_dims:
                phys = _get_phys_expr(d.name, pg_canonical=True)
                if phys is None:
                    raise SemanticBindingError(
                        f"Cannot resolve physical column for grain dimension "
                        f"{d.name!r} required by time variant {meas.name!r}."
                    )
                pg_partition_exprs.append(phys)
            # Bug-3607: resolve the variant's date anchor to a DATE/TIMESTAMP
            # column, never a derived numeric grain dim (would render
            # EXTRACT(... FROM <numeric>) and die at the source).
            # Bug-5247: prefer the denormalized resolved_date_col_id when set.
            _is_window_variant = is_window_variant(variant_kind)
            _anchor_decision = resolve_effective_variant_anchor(meas, time_dim)
            pg_time_phys = _resolve_variant_date_anchor(
                time_dim=time_dim,
                resolved_dimensions=bound_query.resolved_dimensions,
                candidate_dimensions=candidate_dimensions,
                columns_by_id=columns_by_id,
                get_phys_expr=_get_phys_expr,
                measure_name=meas.name,
                pg_canonical=True,
                resolved_date_col_id=(
                    None if _is_window_variant
                    else _anchor_decision.effective_column_id
                ),
                alias_by_table_id=alias_by_table_id,
                is_window=_is_window_variant,
                window_anchor_col_id=(
                    _anchor_decision.effective_column_id
                    if _is_window_variant else None
                ),
            )
            # Keep the un-aggregated fact date anchor (Bug-6571): it is the
            # ORDER BY key for the semi-additive per-period aggregation below.
            pg_time_phys_raw = pg_time_phys
            # Wrap in MIN() so BigQuery accepts the expression in a window
            # ORDER BY within an aggregated query — bare column refs inside
            # UDA expressions are rejected as "neither grouped nor aggregated".
            pg_time_phys = f"MIN({pg_time_phys})"
            _eff = _variant_base_map.get(meas.name, meas)
            _eff_mc = columns_by_id.get(getattr(_eff, "source_column_id", None))
            if _eff_mc:
                _eff_alias = alias_by_table_id.get(_eff_mc.model_table_id)
                if not _eff_alias:
                    # Bug-6125: the base measure's table has no resolved alias in
                    # the join plan. Emitting ``_pg_qcol("", col)`` produced the
                    # malformed ``""."col"`` that the source DB rejects with a
                    # confusing syntax error. Fail loud with a diagnostic instead
                    # of a silently broken window base expression.
                    raise SemanticBindingError(
                        f"Time variant {meas.name!r}: cannot resolve a table "
                        f"alias for base measure column "
                        f"{_eff_mc.column_name!r} (table "
                        f"{_eff_mc.model_table_id!r} is not in the join plan)."
                    )
                pg_phys = _pg_qcol(_eff_alias, _eff_mc.column_name)
            else:
                pg_phys = phys_name
            if _sa_behavior_variant:
                # Bug-6571 [WRONG NUMBERS]: semi-additive measures are balances,
                # not flows. An admissible lag/parallel_period variant must wrap
                # the semi-additive per-period aggregation (last_non_empty picks
                # the period-end balance, first_non_empty the opening balance,
                # min/max/avg_of_children per their rule), NEVER SUM. Cumulation
                # and moving-window families were already rejected above, so
                # reaching here means the per-period value is a single
                # period-end balance. Emitting SUM(balance) would sum every
                # intra-period row before the window compares periods, so the
                # prior-period value becomes a nonsense grand total. Order the
                # per-period pick by the fact date so the window sees the
                # correct period-boundary balance. by_account fails loud inside
                # _semi_additive_agg (per-account dispatch unimplemented).
                pg_base_expr = _semi_additive_agg(
                    _sa_behavior_variant, pg_phys, pg_time_phys_raw, "postgres"
                )
            elif agg == "COUNT_DISTINCT":
                pg_base_expr = f"COUNT(DISTINCT {pg_phys})"
            elif agg == "COUNT":
                pg_base_expr = f"COUNT({pg_phys})"
            else:
                pg_base_expr = f"{agg}({pg_phys})"
            variant_dialect = "postgresql"
            # Bug-6681: resolve time_grain from the finest ranked time
            # dimension present in the GROUP BY grain, not the first one in
            # query order. Year-first and quarter-first groupings must feed
            # the same quarter-grain prior-period emitter.
            _grain_time_dim = _finest_time_dimension_in_grain(
                bound_query.resolved_dimensions,
                _grain_set,
                time_dim,
            )
            _resolved_time_grain = getattr(
                _grain_time_dim, "time_grain", None
            )
            try:
                variant_result = emit_variant_expression(
                    variant_kind,
                    VariantBinding(
                        base_expression=pg_base_expr,
                        base_unaggregated=None,
                        fact_date_column=pg_time_phys,
                        calendar_alias="cal",
                        calendar_columns=calendar_columns,
                        dialect=variant_dialect,
                        n=getattr(meas, "variant_n", None),
                        partition_by=tuple(pg_partition_exprs),
                        calendar_type=_variant_calendar_type,
                        fiscal_year_start_month=_variant_fiscal_start,
                        time_grain=_resolved_time_grain,
                    ),
                )
            except VariantSqlError as exc:
                raise SemanticBindingError(
                    f"Failed to emit time variant {meas.name!r}: {exc}"
                ) from exc
            if calendar_columns and variant_result.referenced_calendar_keys:
                for key in variant_result.referenced_calendar_keys:
                    col_name = calendar_columns.get(key)
                    if col_name:
                        expr = f"{_qid('cal')}.{_qid(col_name)}"
                        if expr not in _variant_extra_group_by:
                            _variant_extra_group_by.append(expr)
            select_pieces.append((pos, f"{variant_result.sql} AS {_qid(out_alias)}"))
            field_expr_by_name[meas.name] = _qid(out_alias)
            if out_alias != meas.name:
                field_expr_by_name[out_alias] = _qid(out_alias)
            continue

        # Semi-additive: override aggregation when the measure carries a
        # semi_additive_behavior.  Skip when the caller explicitly requested
        # a different aggregate (e.g. the XMLA gateway sends SUM as a proxy
        # for LAST_NON_EMPTY and handles the real semantics itself).
        #
        # Bug-7192: semi-additive semantics must apply at ALL grains, not
        # only when a time dimension is projected in the GROUP BY.  A grand
        # total (no GROUP BY) or a non-time GROUP BY (e.g. GROUP BY region)
        # must return the LAST/FIRST non-empty balance across the time
        # domain, not SUM the period balances (which would be a meaningless
        # total of point-in-time snapshots).
        sa_behavior = getattr(meas, "semi_additive_behavior", None)
        user_requested = meas.name in _measure_requested_agg
        # F-015-06: `by_account` requires per-account dispatch (each account's
        # additivity rule resolved through the configured account column,
        # then rolled up). That needs a per-account sub-aggregation the flat
        # GROUP BY path here cannot express, and the prior behaviour silently
        # emitted plain last-non-empty with the account column unused — wrong
        # for flow accounts. Until the per-account engine exists, fail loud
        # rather than return a silently wrong number. Tracked in
        # docs/execution/execution_future_features.md (per-account semi-additivity).
        if (
            sa_behavior
            and str(sa_behavior).lower() == "by_account"
            and not user_requested
        ):
            raise SemanticBindingError(
                f"Measure {meas.name!r} uses the 'by_account' semi-additive "
                f"behaviour, which is not yet supported at query time "
                f"(per-account aggregation dispatch is unimplemented). "
                f"Choose another semi-additive behaviour (last_non_empty, "
                f"first_non_empty, min, max, avg_of_children) until by_account "
                f"is available."
            )
        if sa_behavior and not user_requested:
            sa_order_col = None
            if _sa_finest_time_col_id:
                _sa_mc = columns_by_id.get(_sa_finest_time_col_id)
                if _sa_mc:
                    _sa_alias = alias_by_table_id.get(_sa_mc.model_table_id)
                    if _sa_alias:
                        sa_order_col = _qcol(_sa_alias, _sa_mc.column_name)
            if sa_order_col is None:
                # Try a time dimension in the grain first.
                _sa_time_dim = next(
                    (d for d in bound_query.resolved_dimensions
                     if _is_time_dimension(d)
                     and d.name in grain_names_set),
                    None,
                )
                if _sa_time_dim:
                    sa_order_col = _get_phys_expr(_sa_time_dim.name)
            if sa_order_col is None:
                # Bug-7192: fallback — try ANY resolved time dimension even
                # if it is not in the GROUP BY.  This covers the grand-total
                # case (no GROUP BY) and the non-time grain case (GROUP BY
                # region).  The time column is used only for ordering inside
                # the ARRAY_AGG, not as a projection.
                _sa_time_dim_any = next(
                    (d for d in bound_query.resolved_dimensions
                     if _is_time_dimension(d)),
                    None,
                )
                if _sa_time_dim_any:
                    sa_order_col = _get_phys_expr(_sa_time_dim_any.name)
            if sa_order_col:
                sa_sql = _semi_additive_agg(sa_behavior, phys_name, sa_order_col, "postgres")
                select_pieces.append((pos, f"{sa_sql} AS {_qid(out_alias)}"))
                field_expr_by_name[meas.name] = _qid(out_alias)
                if out_alias != meas.name:
                    field_expr_by_name[out_alias] = _qid(out_alias)
                continue
            # Bug-7192 (Codex R1): when no time ordering column is
            # available AND the behavior requires temporal ordering
            # (last_non_empty, first_non_empty), fail loud rather than
            # silently falling through to SUM (which produces a
            # meaningless total of point-in-time snapshots).
            # Bug-7022 (Fable R1): time-independent behaviors (min,
            # max, avg_of_children) do not need an ordering column but
            # must NOT fall through to the normal aggregation path --
            # that path uses default_agg (often SUM), not the
            # semi-additive behavior, so a measure with
            # semi_additive_behavior="min" and default_agg="sum" would
            # silently emit SUM instead of MIN (a wrong number).
            # Dispatch them through _semi_additive_agg directly.
            _sa_needs_time = str(sa_behavior).lower() in (
                "last_non_empty", "first_non_empty",
            )
            if _sa_needs_time:
                raise SemanticBindingError(
                    f"Measure {meas.name!r} is semi-additive "
                    f"(behavior: {sa_behavior}) but the model has no "
                    f"resolvable time dimension to determine temporal "
                    f"order. Add a time dimension to the model or "
                    f"change the measure's semi-additive behavior."
                )
            # Time-independent behaviors: _semi_additive_agg emits
            # MIN/MAX/AVG without using the time expression.
            _sa_time_independent = str(sa_behavior).lower() in (
                "min", "max", "avg_of_children",
            )
            if _sa_time_independent:
                sa_sql = _semi_additive_agg(sa_behavior, phys_name, "NULL", "postgres")
                select_pieces.append((pos, f"{sa_sql} AS {_qid(out_alias)}"))
                field_expr_by_name[meas.name] = _qid(out_alias)
                if out_alias != meas.name:
                    field_expr_by_name[out_alias] = _qid(out_alias)
                continue

        # Bug-1066: preserve a scalar wrapper around the aggregate
        # (ROUND(AVG(x), 2), CAST(AVG(x) AS NUMERIC), ABS(SUM(x)), …). Render
        # the FULL original expression with every column reference inside it
        # rewritten to this measure's physical column, instead of dropping the
        # wrapper and emitting the bare aggregate.
        _wrapped_raw = _measure_scalar_wrapped_raw.get(meas.name)
        if _wrapped_raw is not None:
            try:
                _w_ast = sqlglot.parse_one(_wrapped_raw, read="postgres")
                _phys_node = sqlglot.parse_one(phys_name, read="postgres")

                def _sub_meas_col(node):
                    if isinstance(node, exp.Column):
                        return _phys_node.copy()
                    return node

                _w_sql = _w_ast.transform(_sub_meas_col).sql(dialect="postgres")
                select_pieces.append((pos, f"{_w_sql} AS {_qid(out_alias)}"))
                field_expr_by_name[meas.name] = _qid(out_alias)
                if out_alias != meas.name:
                    field_expr_by_name[out_alias] = _qid(out_alias)
                continue
            except Exception:
                # Fall through to the bare-aggregate emit on any parse failure
                # (still better than crashing; the wrapper loss is logged via
                # the issue registry as the known boundary).
                pass

        select_pieces.append((pos, f"{_wrap_agg(agg, phys_name)} AS {_qid(out_alias)}"))
        field_expr_by_name[meas.name] = _qid(out_alias)
        if out_alias != meas.name:
            field_expr_by_name[out_alias] = _qid(out_alias)

    return select_pieces, field_expr_by_name, _variant_extra_group_by


def _build_literal_passthrough_pieces(
    bound_query: BoundQuery,
    *,
    _se_list: list,
    field_expr_by_name: dict,
    _get_phys_expr,
) -> list:
    """Render literal and raw-passthrough SELECT pieces (extracted from
    ``_build_source_sql``; Phase 3 internal decomposition, behaviour-identical).

    Returns a list of ``(position, sql)`` pieces to extend the caller's
    ``select_pieces``.
    """
    select_pieces: list[tuple[int, str]] = []
    # Handle literal expressions (CURRENT_DATE, CURRENT_TIMESTAMP, etc.)
    # These don't reference columns.  Re-render through SQLGlot with the
    # target dialect to normalise syntax (e.g. strip empty parens on
    # CURRENT_TIMESTAMP for PostgreSQL).
    for idx, expr in enumerate(_se_list):
        if expr.classification == "literal" and not expr.agg_function:
            try:
                lit_ast = sqlglot.parse_one(expr.raw_text, read="postgres")
                select_pieces.append((idx, lit_ast.sql(dialect="postgres")))
            except Exception:
                select_pieces.append((idx, expr.raw_text))

    # Handle raw passthrough expressions.
    # Exclude bare column passthroughs (inner_column set, no agg_function,
    # raw_text is just the column name) — those are dimensions or measures
    # handled by the loops above.  Include everything else: opaque expressions,
    # complex aggregates, CASE/COALESCE/function expressions with inner_column.
    _names_already_emitted = (
        {d.name for d in bound_query.resolved_dimensions} | set(field_expr_by_name)
    )
    passthrough_exprs: list[tuple[int, Any]] = []
    for idx, _e in enumerate(_se_list):
        if _e.classification != "passthrough":
            continue
        if not _e.inner_column or _e.agg_function:
            # Opaque passthrough or complex aggregate — always include
            passthrough_exprs.append((idx, _e))
        elif _e.inner_column and _e.inner_column in _names_already_emitted:
            # Has inner_column that was already emitted as a dimension or
            # measure — skip only if the raw_text is just the column name
            # or a simple alias of it (col AS alias).  Complex expressions
            # (CASE, COALESCE, CONCAT, SUBSTRING, etc.) must NOT be skipped
            # even when they have an alias — Bug-879.
            raw_stripped = _e.raw_text.strip()
            is_bare = raw_stripped.split(".")[-1].strip('"').lower() == _e.inner_column.lower()
            # A "simple alias" is ONLY when the raw text is literally
            # `column_name AS alias` — nothing else.  Check by stripping
            # any AS clause and seeing if what remains is just the column.
            is_simple_alias = False
            if _e.alias and not _e.agg_function:
                # Remove trailing " AS alias" (case-insensitive) and check
                # if the remainder is just the bare column reference.
                import re as _re
                _before_as = _re.sub(
                    r'\s+AS\s+.*$', '', raw_stripped, flags=_re.IGNORECASE
                ).strip().strip('"').split('.')[-1].strip('"')
                is_simple_alias = _before_as.lower() == _e.inner_column.lower()
            if not is_bare and not is_simple_alias:
                passthrough_exprs.append((idx, _e))
        else:
            passthrough_exprs.append((idx, _e))
    # Bug-919: parse passthrough expressions in the dialect the producer wrote
    # them in (matches the other re-parse sites, e.g. the WHERE handler below).
    # A hardcoded read="postgres" silently fails to parse BigQuery/Spark syntax
    # and falls through to appending the raw text verbatim (unqualified).
    _passthrough_input_dialect = getattr(
        bound_query.logical_query, "input_dialect", "postgres"
    )
    for idx, expr in passthrough_exprs:
        try:
            ast = sqlglot.parse_one(expr.raw_text, read=_passthrough_input_dialect)
            def _qualify(node):
                # _get_phys_expr returns canonical Postgres, so its result is
                # always re-parsed as postgres regardless of the input dialect.
                if isinstance(node, exp.Column) and _get_phys_expr(node.name):
                    return sqlglot.parse_one(_get_phys_expr(node.name), read="postgres")
                return node
            select_pieces.append((idx, ast.transform(_qualify).sql(dialect="postgres")))
        except Exception:
            select_pieces.append((idx, expr.raw_text))

    return select_pieces


def _build_where_clause(
    sql: str,
    bound_query: BoundQuery,
    *,
    dimensions_by_name: dict,
    _order_measures: list,
    field_expr_by_name: dict,
    filter_col_type_by_name: dict,
    _get_phys_expr,
    _get_col_type=None,
    _get_col_type_for_field=None,
    target_dialect: str = "postgres",
) -> str:
    """Append the WHERE clause to ``sql`` (extracted from
    ``_build_source_sql``; Phase 3 internal decomposition, behaviour-identical).

    Returns the SQL string with the WHERE clause appended.
    """
    # Bug-5538 (Codex round-2 finding 4): the raw-WHERE numeric gate resolves a
    # rewritten field's type via the qualified (table_alias.column) resolver when
    # available, so a semantic/physical bare-name collision cannot mis-type the
    # literal. When the caller does not supply one (e.g. unit harness), fall back
    # to the bare-name resolver keyed on the field node's column name — which is
    # itself collision-safe (returns None on ambiguity).
    if _get_col_type_for_field is None:
        def _get_col_type_for_field(field_node):  # type: ignore[misc]
            name = getattr(field_node, "name", None)
            if name and _get_col_type is not None:
                return _get_col_type(name)
            return None

    # WHERE clause: if the query has unresolvable predicates (OR, EXISTS,
    # subqueries), preserve the raw WHERE clause from the original SQL with
    # column name substitution.  Otherwise, reconstruct from extracted filters.
    if getattr(bound_query.logical_query, "has_unresolvable_where", False):
        _input_dialect_for_where = getattr(
            bound_query.logical_query, "input_dialect", "postgres"
        )
        # F-006-03: reparse strictly (fail-loud). Preserving a raw WHERE from a
        # recovered tree (e.g. ``x = 1 !!`` -> ``x = NOT 1``) would carry an
        # altered predicate into the rewritten query -> silently wrong rows.
        raw_ast = parse_one_strict(
            bound_query.logical_query.raw_query, read=_input_dialect_for_where,
        )
        raw_select = raw_ast if isinstance(raw_ast, exp.Select) else raw_ast.find(exp.Select)
        raw_where = raw_select.args.get("where") if raw_select else None
        # Bug-457: when the original SQL is a subquery wrapper
        # (e.g. SELECT COUNT(*) FROM (SELECT * FROM T WHERE ...) q),
        # the WHERE lives inside the subquery, not the outer SELECT.
        if raw_where is None and raw_select is not None:
            from_clause = raw_select.args.get("from_")
            if from_clause and isinstance(from_clause.this, exp.Subquery):
                inner = from_clause.this.this
                if isinstance(inner, exp.Select):
                    raw_where = inner.args.get("where")
        if raw_where:
            _known_fields_lower = (
                {n.lower() for n in dimensions_by_name}
                | {m.name.lower() for m in bound_query.resolved_measures}
                | {m.name.lower() for m in _order_measures}
            )

            def _is_known_field(name: str) -> bool:
                # Recognise the semantic name, a name resolvable to a physical
                # expression, AND a bare physical column name. The last case
                # matters because the sqlglot transform (pre-order, left child
                # before right) rewrites the field operand (``.this``) to its
                # PHYSICAL column before the value operand is visited, so the
                # value-side check sees the physical name — e.g. semantic
                # ``year`` already rewritten to ``"dt"."d_year"`` when ``'1999'``
                # is examined. ``_get_col_type`` also resolves by physical name
                # as a fallback for the value-left / symmetric case.
                return (
                    name.lower() in _known_fields_lower
                    or _get_phys_expr(name, pg_canonical=True) is not None
                    or (_get_col_type is not None and _get_col_type(name) is not None)
                )

            def _value_literal(value: str, field_node):
                """Build the literal for a quoted value-side token, typed by
                the target FIELD's source data type.

                Bug-5462: MDX/XMLA slicer members (and other BI clients) render
                every member key as a quoted string, e.g. ``"d_year" = '1999'``.
                When the target column is INTEGER/NUMERIC, BigQuery rejects the
                INT64-vs-STRING comparison. Emit ``exp.Literal.number`` for a
                numeric-typed column so sqlglot transpiles a bare numeric
                literal (``= 1999``) for every dialect; keep the string literal
                for text/date columns (unchanged). A non-numeric value against a
                numeric column fails loud rather than silently corrupting the
                predicate.
                """
                col_type = (
                    _get_col_type_for_field(field_node)
                    if isinstance(field_node, exp.Column)
                    else None
                )
                if is_numeric_col_type(col_type):
                    if value_is_numeric_literal(value):
                        # Bug-5546: an INTEGER column rejects a fractional literal
                        # (year = 19.99) — BigQuery would silently coerce + return
                        # empty. Fractions stay valid for FLOAT/NUMERIC columns.
                        if is_integer_col_type(col_type) and "." in str(value):
                            raise SemanticBindingError(
                                f"Fractional value {value!r} compared to integer "
                                f"column '{getattr(field_node, 'name', '?')}' in "
                                f"WHERE clause"
                            )
                        return exp.Literal.number(value)
                    raise SemanticBindingError(
                        f"Non-numeric value {value!r} compared to numeric "
                        f"column '{getattr(field_node, 'name', '?')}' in WHERE "
                        f"clause"
                    )
                return exp.Literal.string(value)

            # Bug-5538 (Codex findings 2 & 3): genuine ``exp.Literal`` string
            # members (e.g. ``d_year = '1999'`` / ``IN ('1999','2000')`` /
            # ``IN (SELECT '1999')``) carry their text in ``.this`` rather than
            # ``.name``; ``_value_literal`` re-types them identically.
            _value_literal_from_str = _value_literal

            def _in_subselect_field(node):
                """If ``node`` is a top-level projection literal of a SELECT that
                forms the RHS subquery of an outer ``exp.In``, return that IN's
                LHS field column; else None.

                Bug-5538 (Codex finding 3): a subselect filter form such as
                ``d_year IN (SELECT '1999')`` projects the member as a string
                literal inside the inner SELECT, so it never sits directly under
                the IN. Walk up projection -> (Alias) -> Select -> Subquery -> In
                to inherit the OUTER numeric column's type.
                """
                cur = node.parent
                # Skip an enclosing alias on the projection (SELECT '1999' AS y).
                if isinstance(cur, exp.Alias):
                    cur = cur.parent
                if not isinstance(cur, exp.Select):
                    return None
                # The literal must be a top-level projection, not buried in a
                # nested expression / WHERE of the inner select.
                if node not in cur.expressions and not any(
                    proj is node or (isinstance(proj, exp.Alias) and proj.this is node)
                    for proj in cur.expressions
                ):
                    return None
                sub = cur.parent
                if not isinstance(sub, exp.Subquery):
                    return None
                in_node = sub.parent
                if isinstance(in_node, exp.In) and isinstance(in_node.this, exp.Column) \
                        and _is_known_field(in_node.this.name):
                    return in_node.this
                return None

            def _numeric_target_field(node):
                """If ``node`` is a value-side literal (or a ``-literal``) whose
                target FIELD is a known NUMERIC column, return that field node;
                else None.

                Bug-5538 (Codex round-2 finding 2): the value-side numeric gate
                must cover EVERY RHS literal/token against a numeric column — not
                only string literals. A numeric/raw literal RHS (``d_year = 1e9``,
                ``= +1``, a dialect-parsed bare token) otherwise rendered as-is,
                bypassing ``value_is_numeric_literal``. This resolves the target
                field for the IN-member, binary-comparison and subselect shapes so
                the caller can validate the literal text the same way for both
                string and numeric literals.

                Bug-5539 (Codex round-3 findings 1 & 2): two more value-side
                shapes are resolved here so the SAME validator gates them too:
                  - ``exp.Between`` low/high bounds — the target field is the
                    BETWEEN's ``.this`` (``int_dim BETWEEN 1e9 AND 2e9`` under an
                    OR / unresolvable WHERE previously skipped validation).
                  - a sign-wrapped literal (``-1e9`` parses as ``Neg(Literal)``,
                    so the literal's parent is the ``Neg``, not the comparison) —
                    step over the ``Neg`` to find the real predicate parent so a
                    negative scientific form is validated in its full spelling.
                """
                # Step over a unary-sign wrapper: a negative literal parses as
                # Neg(Literal(...)), so the literal sits one level below the real
                # predicate node. Treat the Neg as the value node for parent
                # resolution; the caller validates the signed spelling.
                value_node = node
                if isinstance(node.parent, exp.Neg):
                    value_node = node.parent
                parent = value_node.parent
                if isinstance(parent, exp.In) and value_node is not parent.this:
                    field_node = parent.this
                    if isinstance(field_node, exp.Column) and _is_known_field(field_node.name):
                        return field_node
                    return None
                if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                    other = parent.right if value_node is parent.left else parent.left
                    if isinstance(other, exp.Column) and _is_known_field(other.name):
                        return other
                    return None
                if isinstance(parent, exp.Between):
                    field_node = parent.this
                    if isinstance(field_node, exp.Column) and _is_known_field(field_node.name):
                        return field_node
                    return None
                return _in_subselect_field(node)

            def _qualify_where(node):
                # Bug-5538 (Codex findings 2 & 3): re-type / validate value-side
                # literals so a numeric column gets a numeric literal in the raw
                # OR / IN / subselect shapes (Excel set & multi-member slicers,
                # subselect filters). Columns are handled below; this branch
                # covers genuine ``exp.Literal`` members that the column-only path
                # never visited.
                if isinstance(node, exp.Literal):
                    field_node = _numeric_target_field(node)
                    if field_node is not None:
                        if node.is_string:
                            # String member: re-type for numeric columns, keep the
                            # string literal for text/date columns (unchanged).
                            return _value_literal_from_str(node.this, field_node)
                        # Codex round-2 finding 2: a non-string (numeric/raw) RHS
                        # token against a known NUMERIC column must clear the SAME
                        # strict validator. A non-conforming token (``1e9``,
                        # ``+1`` — sqlglot folds the sign, but defence-in-depth)
                        # fails loud; a conforming integer/decimal is rebuilt as a
                        # canonical numeric literal rather than passed through raw.
                        col_type = (
                            _get_col_type_for_field(field_node)
                            if isinstance(field_node, exp.Column)
                            else None
                        )
                        if is_numeric_col_type(col_type):
                            # Bug-5539 (Codex round-3 finding 2): validate the
                            # literal's ORIGINAL signed spelling. ``transform`` is
                            # post-order, so a ``-1e9`` literal is visited (as the
                            # inner ``Literal('1e9')``) BEFORE its ``Neg`` parent.
                            # Build the signed token (``-1e9``) and run the strict
                            # grammar on it so a negative scientific form fails
                            # loud instead of leaving the ``Neg`` to emit a bare
                            # ``-1e9``. Rebuild only the inner (unsigned) literal;
                            # the surviving ``Neg`` re-applies the sign.
                            is_negated = isinstance(node.parent, exp.Neg)
                            spelling = ("-" + node.this) if is_negated else node.this
                            if value_is_numeric_literal(spelling):
                                # Bug-5546: integer column rejects a fractional
                                # literal (see _value_literal above).
                                if is_integer_col_type(col_type) and "." in spelling:
                                    raise SemanticBindingError(
                                        f"Fractional value {spelling!r} compared to "
                                        f"integer column "
                                        f"'{getattr(field_node, 'name', '?')}' in "
                                        f"WHERE clause"
                                    )
                                return exp.Literal.number(node.this)
                            raise SemanticBindingError(
                                f"Non-numeric value {spelling!r} compared to "
                                f"numeric column "
                                f"'{getattr(field_node, 'name', '?')}' in WHERE "
                                f"clause"
                            )
                    return node
                if isinstance(node, exp.Column):
                    phys = _get_phys_expr(node.name, pg_canonical=True)
                    if phys:
                        return sqlglot.parse_one(phys, read="postgres")
                    # Bug-457: double-quoted values (e.g. "WEB") are parsed as
                    # Column nodes but are not known fields. Only convert to a
                    # string literal when on the VALUE side of a comparison
                    # whose other child is a known semantic field. Unquoted
                    # unknown columns always raise — they are real field refs.
                    if node.name.lower() not in _known_fields_lower:
                        is_quoted = getattr(node.this, "quoted", False)
                        parent = node.parent
                        # Case 1: inside an IN expression list — the field is
                        # parent.this, values are in parent.expressions.
                        if isinstance(parent, exp.In):
                            field_node = parent.this
                            if (
                                is_quoted
                                and isinstance(field_node, exp.Column)
                                and _is_known_field(field_node.name)
                                and node is not parent.this
                            ):
                                return _value_literal(node.name, field_node)
                            raise SemanticBindingError(
                                f"Unknown column '{node.name}' in WHERE clause"
                            )
                        # Case 2: binary comparison — symmetric value-side
                        # detection: convert only quoted tokens when the
                        # OTHER side is a known field.
                        if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is)):
                            other = parent.right if node is parent.left else parent.left
                            other_is_known = (
                                isinstance(other, exp.Column)
                                and _is_known_field(other.name)
                            )
                            if is_quoted and other_is_known:
                                return _value_literal(node.name, other)
                            raise SemanticBindingError(
                                f"Unknown column '{node.name}' in WHERE clause"
                            )
                return node
            sql += " WHERE " + raw_where.this.transform(_qualify_where).sql(dialect="postgres")
        else:
            where = _render_where(
                bound_query.resolved_filters, field_expr_by_name, "postgresql", filter_col_type_by_name,
                like_target_connector=_dialect_to_connector(target_dialect),
                # Bug-7918: ``filter_col_type_by_name`` carries source-native
                # type spellings; the tz-awareness of a bare ``timestamp``
                # token is only decidable against the source dialect.
                source_connector=_dialect_to_connector(target_dialect),
            )
            if where:
                sql += f" WHERE {where}"
    else:
        where = _render_where(
            bound_query.resolved_filters, field_expr_by_name, "postgresql", filter_col_type_by_name,
            like_target_connector=_dialect_to_connector(target_dialect),
            source_connector=_dialect_to_connector(target_dialect),  # Bug-7918
        )
        if where:
            sql += f" WHERE {where}"

    return sql


async def _build_source_sql(
    bound_query: BoundQuery, db: Any, *, target_dialect: str | None = None,
) -> str:
    """Build SQL using physical table/column names from the semantic model."""
    # Resolve the target dialect so _final_transpile knows where to send the SQL.
    if target_dialect is None:
        target_dialect = await _resolve_target_dialect(db, bound_query.model.id)

    # All identifier and table quoting inside _build_source_sql uses
    # PostgreSQL double-quote form.  The single _final_transpile() call on
    # every return path converts the canonical PG SQL to the target dialect.
    # Bug-7023: use safe_ident (escapes embedded double-quotes) instead of
    # bare f'"{name}"' to prevent identifier-context SQL injection.
    def _qid(name: str) -> str:
        return safe_ident(name)

    def _qtbl(dotted: str) -> str:
        return ".".join(safe_ident(p) for p in dotted.split("."))

    def _qcol(alias: str, col: str) -> str:
        return f'{safe_ident(alias)}.{safe_ident(col)}'

    # Kept for callers that explicitly need PG form regardless of the
    # surrounding context (e.g. variant expression builders).
    def _pg_qid(name: str) -> str:
        return safe_ident(name)

    def _pg_qcol(alias: str, col: str) -> str:
        return f'{safe_ident(alias)}.{safe_ident(col)}'

    def _final_transpile(pg_sql: str) -> str:
        """Translate PostgreSQL-canonical SQL to target_dialect.

        This is the architectural boundary between logical SQL construction
        (always PostgreSQL syntax) and physical execution (target dialect).
        Every return path in _build_source_sql must pass through here.

        Bug-982: for a BigQuery target, a percentile ordered-set aggregate is
        rendered in the EXACT analytic form (or raises
        ``ExactQuantileUnavailable``) rather than the silent ``APPROX_QUANTILES``
        transform. ``_bigquery_exact_quantile_sql`` returns None for every non-
        BigQuery target and every statement without a percentile, so the normal
        transpile boundary is used unchanged in all other cases.
        """
        exact_bq = _bigquery_exact_quantile_sql(pg_sql, target_dialect)
        if exact_bq is not None:
            return exact_bq
        return _transpile_to_dialect(pg_sql, target_dialect)

    # Collect source ids referenced by selected fields and filters.
    col_ids = set()
    uda_ids = set()
    dimensions_by_name = dict(
        getattr(bound_query, "resolved_dimensions_by_name", {}) or {}
    )
    if not dimensions_by_name:
        dimensions_by_name = {dim.name: dim for dim in bound_query.resolved_dimensions}
    filter_dim_names = {f.dimension_name for f in bound_query.resolved_filters}
    # Bug-5488: dimensions referenced ONLY inside an unresolvable WHERE
    # predicate (function-wrapped or OR-compound) never produce a LogicalFilter,
    # so they are absent from ``resolved_filters``. The binder collected them by
    # walking the raw WHERE AST; fold them in here so their physical columns are
    # loaded (``col_ids``) and their tables joined (``required_table_ids``),
    # which lets ``_get_phys_expr`` resolve them in ``_qualify_where`` instead of
    # leaking the bare semantic name to the source DB ("column does not exist").
    filter_dim_names |= set(getattr(bound_query, "where_referenced_dimensions", None) or set())

    # F-013-01 residual (Lane A2): dimensions referenced only in an
    # unresolvable-WHERE backfill (not in SELECT) are resolved from the DEPLOYED
    # snapshot for a deployed model (live ORM only for undeployed). A draft
    # dimension edit must not change a deployed model's WHERE resolution. Fail
    # closed: a deployed model with an unresolvable snapshot resolves nothing.
    from src.rewrite.snapshot_graph_resolvers import resolve_dimensions_by_name

    missing_filter_dims = filter_dim_names - set(dimensions_by_name)
    if missing_filter_dims:
        for name, dim in (
            await resolve_dimensions_by_name(
                bound_query.model,
                db,
                set(missing_filter_dims),
                deployed_shape=getattr(bound_query, "deployed_shape", None),
            )
        ).items():
            dimensions_by_name[name] = dim

    still_missing = missing_filter_dims - set(dimensions_by_name)
    if still_missing:
        for name, dim in (
            await resolve_dimensions_by_name(
                bound_query.model,
                db,
                set(still_missing),
                case_insensitive=True,
                deployed_shape=getattr(bound_query, "deployed_shape", None),
            )
        ).items():
            dimensions_by_name[name] = dim

    # Collect source_column / UDA IDs from filter-only dimensions so they
    # are included in the bulk column/UDA load below.
    for fname in filter_dim_names:
        fdim = dimensions_by_name.get(fname)
        if not fdim:
            continue
        fsc = getattr(fdim, "source_column_id", None)
        if fsc:
            col_ids.add(fsc)
        fuda = getattr(fdim, "user_defined_attribute_id", None)
        if fuda:
            uda_ids.add(fuda)

    # Resolve ORDER BY column names that aren't already in dimensions or
    # measures.  These may be dimensions or measures referenced only in
    # ORDER BY (not in SELECT), so the binder never resolved them.
    order_col_names = {col for col, _ in bound_query.logical_query.order_by}
    resolved_names = set(dimensions_by_name) | {m.name for m in bound_query.resolved_measures}
    missing_order_names = order_col_names - resolved_names
    _order_measures: list = []
    if missing_order_names:
        # F-013-01 residual (Lane A2): ORDER BY-only dimensions/measures resolve
        # from the DEPLOYED snapshot for a deployed model (live ORM only for
        # undeployed); fail closed otherwise.
        from src.rewrite.snapshot_graph_resolvers import (
            resolve_dimensions_by_name,
            resolve_measures_by_name,
        )
        for name, dim in (
            await resolve_dimensions_by_name(
                bound_query.model,
                db,
                set(missing_order_names),
                deployed_shape=getattr(bound_query, "deployed_shape", None),
            )
        ).items():
            dimensions_by_name[name] = dim
            missing_order_names.discard(name)
        if missing_order_names:
            _order_measures = await resolve_measures_by_name(
                bound_query.model,
                db,
                set(missing_order_names),
                deployed_shape=getattr(bound_query, "deployed_shape", None),
            )

    for dim in bound_query.resolved_dimensions:
        source_col_id = getattr(dim, "source_column_id", None)
        if source_col_id:
            col_ids.add(source_col_id)
        uda_id = getattr(dim, "user_defined_attribute_id", None)
        if uda_id:
            uda_ids.add(uda_id)
    _variant_base_map: dict[str, Any] = {}
    _variant_base_ids_to_load: set[str] = set()
    for meas in bound_query.resolved_measures:
        source_col_id = getattr(meas, "source_column_id", None)
        if source_col_id:
            col_ids.add(source_col_id)
        uda_id = getattr(meas, "user_defined_attribute_id", None)
        if uda_id:
            uda_ids.add(uda_id)
        if getattr(meas, "variant_of_measure_id", None):
            _variant_base_ids_to_load.add(str(meas.variant_of_measure_id))

    if _variant_base_ids_to_load and db is not None:
        # F-013-01 residual (Lane A2): a time-variant measure's BASE measure
        # (variant_of_measure_id) resolves from the DEPLOYED snapshot for a
        # deployed model (live ORM only for undeployed). A draft edit to the base
        # measure must not silently change deployed time-variant SQL. Fail closed:
        # a deployed model with an unresolvable snapshot resolves no base.
        from src.rewrite.snapshot_graph_resolvers import resolve_measures_by_id
        _base_measures = await resolve_measures_by_id(
            bound_query.model,
            db,
            set(_variant_base_ids_to_load),
            deployed_shape=getattr(bound_query, "deployed_shape", None),
        )
        for meas in bound_query.resolved_measures:
            base_id = getattr(meas, "variant_of_measure_id", None)
            if base_id and str(base_id) in _base_measures:
                base_m = _base_measures[str(base_id)]
                _variant_base_map[meas.name] = base_m
                if base_m.source_column_id:
                    col_ids.add(base_m.source_column_id)
                if getattr(base_m, "user_defined_attribute_id", None):
                    uda_ids.add(base_m.user_defined_attribute_id)

    # Phase 4A — calculated measures. Parse the expression for each
    # calculated measure and load any referenced base measures that are
    # not already in resolved_measures, so their source columns / UDAs
    # drive join planning in the same pass as regular measures.
    calc_parsed_by_name: dict[str, Any] = {}
    calc_ref_measures_by_name: dict[str, Any] = {}
    _calc_ref_names_to_load: set[str] = set()
    for _meas in bound_query.resolved_measures:
        if getattr(_meas, "measure_type", None) != "calculated":
            continue
        from shared.semantic.calculated_expression import (
            ExpressionValidationError,
            parse_expression,
        )
        try:
            _parsed = parse_expression(_meas.expression or "")
        except ExpressionValidationError as exc:
            raise SemanticBindingError(
                f"Calculated measure {_meas.name!r} has an invalid "
                f"expression: {exc}"
            ) from exc
        calc_parsed_by_name[_meas.name] = _parsed
        for _ref in _parsed.references:
            _calc_ref_names_to_load.add(_ref.name)

    _resolved_measure_names = {m.name for m in bound_query.resolved_measures}
    _calc_ref_names_to_load -= _resolved_measure_names

    if _calc_ref_names_to_load:
        # Bug-7803: resolve calc-DEPENDENCY base measures from the DEPLOYED
        # snapshot (fail-closed), not live draft Measure rows. The binder pins the
        # SELECTED calc measure to the deployed snapshot; resolving its referenced
        # base measures from live draft ORM split the authority (a post-deploy
        # draft edit to a referenced base measure's source_column_id / UDA /
        # variant / default_agg would change the calc's emitted SQL before
        # redeployment). Mirrors the Bug-7784 aggregate_matcher precedent and the
        # raw route (raw_sql.py). A deployed model whose snapshot cannot be
        # resolved leaves the dependency absent -> fail closed under deployed
        # semantics (never draft state); live ORM only for an UNDEPLOYED model.
        from src.semantic.snapshot_resolver import (
            resolve_calc_dependency_measures,
        )
        _dep_measures = await resolve_calc_dependency_measures(
            bound_query.model, db, _calc_ref_names_to_load,
        )
        for _name, _m in _dep_measures.items():
            calc_ref_measures_by_name[_name] = _m
            _src_id = getattr(_m, "source_column_id", None)
            if _src_id:
                col_ids.add(_src_id)
            _uda_id = getattr(_m, "user_defined_attribute_id", None)
            if _uda_id:
                uda_ids.add(_uda_id)

    for _m in bound_query.resolved_measures:
        if getattr(_m, "measure_type", None) != "calculated":
            calc_ref_measures_by_name[_m.name] = _m
    for dim_name in filter_dim_names:
        dim = dimensions_by_name.get(dim_name)
        source_col_id = getattr(dim, "source_column_id", None) if dim else None
        if source_col_id:
            col_ids.add(source_col_id)
        uda_id = getattr(dim, "user_defined_attribute_id", None) if dim else None
        if uda_id:
            uda_ids.add(uda_id)
    # ORDER BY dimensions already added to dimensions_by_name above; collect
    # their column IDs.  ORDER BY measures need separate handling.
    for dim_name in order_col_names:
        dim = dimensions_by_name.get(dim_name)
        if dim:
            source_col_id = getattr(dim, "source_column_id", None)
            if source_col_id:
                col_ids.add(source_col_id)
    for meas in _order_measures:
        source_col_id = getattr(meas, "source_column_id", None)
        if source_col_id:
            col_ids.add(source_col_id)

    _dax_hints_for_col_ids = getattr(bound_query.logical_query, "time_variant_hints", None) or {}
    _period_aware_for_anchor = any(
        (getattr(m, "variant_kind", None) or _dax_hints_for_col_ids.get(m.name))
        in TIME_VARIANTS_NEEDING_CALENDAR
        for m in bound_query.resolved_measures
    )
    if _period_aware_for_anchor:
        for dim in dimensions_by_name.values():
            if not _is_time_dimension(dim):
                continue
            source_col_id = getattr(dim, "source_column_id", None)
            if source_col_id:
                col_ids.add(source_col_id)

    # Semi-additive: when any measure has semi_additive_behavior, pre-load
    # the finest-grain time dimension's physical column so
    # LAST/FIRST_NON_EMPTY can order within each GROUP BY bucket.
    #
    # Bug-7192: semi-additive semantics must apply at ALL grains, not just
    # when a time dimension is projected.  A grand total or a non-time
    # GROUP BY (e.g. GROUP BY region) must still return the last/first
    # non-empty balance across the time domain, not SUM the balances.
    # The time ordering column is always needed so the aggregation can
    # determine temporal order even when time is not in the GROUP BY.
    _sa_finest_time_col_id: Any | None = None
    _sa_has_time_in_grain = False
    _sa_behaviors_present = any(
        getattr(m, "semi_additive_behavior", None)
        for m in bound_query.resolved_measures
        if getattr(m, "measure_type", None) != "calculated"
    )
    if _sa_behaviors_present:
        _grain_early = set(bound_query.logical_query.grain)
        _sa_has_time_in_grain = any(
            d.name in _grain_early
            for d in bound_query.resolved_dimensions
            if _is_time_dimension(d)
        )
        # Bug-7192: load the finest time column ALWAYS when semi-additive
        # measures are present, not only when time is in the grain.  At
        # grand-total or non-time grains (GROUP BY region) the time column
        # provides the ordering axis for LAST/FIRST_NON_EMPTY.
        # F-013-01 residual (Lane A2): the finest time dimension resolves from
        # the DEPLOYED snapshot for a deployed model (live ORM only for
        # undeployed). A draft time_grain change must not silently change the
        # deployed semi-additive ordering column. Fail closed otherwise.
        from src.rewrite.snapshot_graph_resolvers import resolve_time_dimensions
        _all_time_dims = sorted(
            await resolve_time_dimensions(
                bound_query.model,
                db,
                deployed_shape=getattr(bound_query, "deployed_shape", None),
            ),
            key=lambda d: _SA_GRAIN_RANK.get(
                getattr(d, "time_grain", None) or "", 99
            ),
        )
        if _all_time_dims:
            _ftd_col = getattr(_all_time_dims[0], "source_column_id", None)
            if _ftd_col:
                _sa_finest_time_col_id = _ftd_col
                col_ids.add(_ftd_col)

    if not col_ids and not uda_ids:
        return await _build_no_columns_sql(bound_query, db, target_dialect)

    # Load model graph + resolve required/base tables (extracted to
    # table_resolution.py — Phase 3 internal decomposition).
    tables_by_id, joins, columns_by_id, uda_by_id = await _load_model_graph(
        bound_query, db, uda_ids
    )

    required_table_ids, base_table = _resolve_required_and_base_tables(
        bound_query,
        columns_by_id,
        tables_by_id,
        uda_by_id,
        dimensions_by_name,
        filter_dim_names,
        order_col_names,
        _order_measures,
        calc_ref_measures_by_name,
        _sa_finest_time_col_id,
        _sa_has_time_in_grain,
    )

    alias_by_table_id = {
        table_id: (tables_by_id[table_id].alias or f"t_{idx}")
        for idx, table_id in enumerate(sorted(required_table_ids, key=str))
        if table_id in tables_by_id
    }
    alias_by_table_id.setdefault(base_table.id, base_table.alias or "base")

    from_clause = _build_joined_from_clause(
        base_table_id=base_table.id,
        required_table_ids=required_table_ids,
        joins=joins,
        tables_by_id=tables_by_id,
        columns_by_id=columns_by_id,
        alias_by_table_id=alias_by_table_id,
        connector="postgresql",
        preferred_join_ids=getattr(bound_query.logical_query, "drill_join_path_ids", None),
    )

    if not from_clause:
        if len(required_table_ids) > 1:
            raise ValueError(
                _missing_join_error_message(
                    base_table_id=base_table.id,
                    required_table_ids=required_table_ids,
                    joins=joins,
                    tables_by_id=tables_by_id,
                )
            )
        # Phase 2 fail-loud (Finding 3): a single required table but no
        # FROM clause could be built — refuse to fall back to the raw
        # (semantic) query.
        raise SemanticBindingError(
            "Cannot rewrite query to source SQL: failed to build a FROM "
            f"clause for base table "
            f"{getattr(base_table, 'physical_name', base_table.id)!r}."
        )

    # Build translated UDA expressions keyed by UDA id.
    uda_expr_by_id: dict[Any, str] = {}
    for uda_id, uda in uda_by_id.items():
        if uda.validated is False:
            detail = uda.validation_error or "validation failed"
            raise ValueError(f"User-defined attribute '{uda.name}' is invalid: {detail}")
        table_alias = alias_by_table_id.get(uda.table_id)
        if not table_alias:
            continue
        uda_expr_by_id[uda_id] = _render_uda_expression(
            expression=uda.expression,
            table_alias=table_alias,
            target_dialect="postgres",
        )

    def _get_phys_expr(semantic_name: str, *, pg_canonical: bool = False) -> str | None:
        _q_col = _pg_qcol if pg_canonical else _qcol
        _q_id = _pg_qid if pg_canonical else _qid
        dim = dimensions_by_name.get(semantic_name)
        if dim:
            mc = columns_by_id.get(getattr(dim, "source_column_id", None))
            if mc:
                alias = alias_by_table_id.get(mc.model_table_id)
                if alias: return _q_col(alias, mc.column_name)
            calc_expr = getattr(dim, "calc_expression", None)
            if calc_expr:
                # F-006-04: return the QUALIFIED calculated expression, not the
                # SELECT alias. This value is consumed by WHERE (_qualify_where
                # and _render_where via field_expr_by_name), window PARTITION BY,
                # and HAVING qualification — all of which reject a SELECT alias
                # (PostgreSQL/BigQuery: aliases are valid only in ORDER BY). The
                # alias previously emitted here produced a "column does not
                # exist" source error or, worse, bound to a same-named physical
                # column (wrong rows). The expression is PG-canonical and shared
                # with the SELECT/GROUP BY builder so the same calc dimension
                # renders identically everywhere; the single boundary transpile
                # then translates it to the target dialect.
                return _qualify_calc_expression(
                    calc_expr, columns_by_id, alias_by_table_id,
                )
            uda_id = getattr(dim, "user_defined_attribute_id", None)
            if uda_id:
                uda_expr = uda_expr_by_id.get(uda_id)
                if uda_expr is not None:
                    return f"({uda_expr})"
                # Bug-917: the UDA's table is not in the FROM clause, so its
                # expression was never built. Treat as unresolved (fall through)
                # rather than emitting the literal string "(None)".
        for m in list(bound_query.resolved_measures) + _order_measures:
            if m.name == semantic_name:
                mc = columns_by_id.get(getattr(m, "source_column_id", None))
                if mc:
                    alias = alias_by_table_id.get(mc.model_table_id)
                    if alias: return _q_col(alias, mc.column_name)
                uda_id = getattr(m, "user_defined_attribute_id", None)
                if uda_id:
                    uda_expr = uda_expr_by_id.get(uda_id)
                    if uda_expr is not None:
                        return f"({uda_expr})"
                    # Bug-917: unresolved UDA (table not in FROM) — return None,
                    # never the literal string "(None)".
                    return None
        return None

    # Physical-column-name -> data_type fallback. The raw ``_qualify_where``
    # path rewrites the field side to its PHYSICAL column before the value side
    # is visited (sqlglot transform is bottom-up, left-to-right), so a
    # by-semantic-name lookup misses there. Keying physical names too lets
    # ``_get_col_type`` type the value correctly in both paths.
    # When two tables share a column name but declare DIFFERENT types, the
    # physical-name lookup is ambiguous — record None so the renderer keeps its
    # safe string-literal default rather than guessing a wrong (possibly
    # numeric) type. Same-name same-type is fine.
    _data_type_by_phys_name: dict[str, str | None] = {}
    for _mc in columns_by_id.values():
        _cn = getattr(_mc, "column_name", None)
        _dt = getattr(_mc, "data_type", None)
        if not _cn or not _dt:
            continue
        _key = _cn.lower()
        if _key in _data_type_by_phys_name:
            if (_data_type_by_phys_name[_key] or "").upper() != _dt.upper():
                _data_type_by_phys_name[_key] = None  # ambiguous
        else:
            _data_type_by_phys_name[_key] = _dt

    # Bug-5538 (Codex round-2 finding 4): a QUALIFIED physical lookup keyed by
    # ``table_alias.column`` (lower-cased) is unambiguous even when a bare column
    # name collides with a semantic dimension name or repeats across tables.
    # After ``_qualify_where`` rewrites a semantic field to its physical column,
    # the field node carries the table alias, so the qualified key resolves the
    # CORRECT type. The bare-name path stays a last resort and refuses to guess
    # on any semantic/physical name collision (see ``_bare_name_is_ambiguous``).
    _data_type_by_qualified_phys: dict[str, str] = {}
    for _mc in columns_by_id.values():
        _cn = getattr(_mc, "column_name", None)
        _dt = getattr(_mc, "data_type", None)
        _alias = alias_by_table_id.get(getattr(_mc, "model_table_id", None))
        if not _cn or not _dt or not _alias:
            continue
        _data_type_by_qualified_phys[f"{_alias.lower()}.{_cn.lower()}"] = _dt

    # Semantic dimension names whose lower-cased form collides with a physical
    # column name of a DIFFERENT type. A bare lookup of such a name cannot decide
    # which type is meant (semantic-for-INT vs physical-for-string), so it must
    # resolve to UNKNOWN — the renderer then keeps its safe string-literal
    # default rather than emitting a wrong-typed (possibly numeric) bare token.
    _ambiguous_bare_names: set[str] = set()
    for _dname, _dim in dimensions_by_name.items():
        _key = _dname.lower()
        _phys_dt = _data_type_by_phys_name.get(_key)
        if _phys_dt is None:
            continue
        _mc = columns_by_id.get(getattr(_dim, "source_column_id", None))
        _sem_dt = getattr(_mc, "data_type", None) if _mc is not None else None
        if _sem_dt and _phys_dt.upper() != _sem_dt.upper():
            _ambiguous_bare_names.add(_key)

    def _get_col_type(name: str) -> str | None:
        """Resolve a field's source data type (e.g. ``INT64``, ``STRING``,
        ``DATE``) so WHERE rendering can emit a type-correct literal. Resolution
        order mirrors ``_get_phys_expr``: dimension physical column / UDA, then
        measure physical column / UDA, then a direct physical-column-name match.

        Returns None when no type is known (the renderers then keep their
        existing string-literal default). Used by BOTH WHERE paths — the
        extracted-filter path (``filter_col_type_by_name``) and the raw
        ``_qualify_where`` path — so an INTEGER-keyed dimension filter emits a
        numeric literal regardless of which path the query takes.
        """
        dim = dimensions_by_name.get(name)
        if dim is not None:
            mc = columns_by_id.get(getattr(dim, "source_column_id", None))
            if mc is not None and getattr(mc, "data_type", None):
                return mc.data_type
            uda_id = getattr(dim, "user_defined_attribute_id", None)
            uda = uda_by_id.get(uda_id) if uda_id else None
            if uda is not None and getattr(uda, "output_data_type", None):
                return uda.output_data_type
        for m in list(bound_query.resolved_measures) + _order_measures:
            if m.name == name:
                mc = columns_by_id.get(getattr(m, "source_column_id", None))
                if mc is not None and getattr(mc, "data_type", None):
                    return mc.data_type
                uda_id = getattr(m, "user_defined_attribute_id", None)
                uda = uda_by_id.get(uda_id) if uda_id else None
                if uda is not None and getattr(uda, "output_data_type", None):
                    return uda.output_data_type
                break
        # Bug-5538 (Codex round-2 finding 4): a bare name that collides between a
        # semantic dimension and a physical column of a DIFFERENT type is
        # ambiguous; refuse to guess (fail safe). Reached only when neither the
        # semantic dimension nor a measure above produced a type — i.e. the bare
        # name is the rewritten PHYSICAL column whose semantic twin has a
        # different declared type. Prefer the qualified resolver for these.
        if name.lower() in _ambiguous_bare_names:
            return None
        # Fallback: the raw-WHERE path passes the already-rewritten PHYSICAL
        # column name.
        return _data_type_by_phys_name.get(name.lower())

    def _get_col_type_for_field(field_node) -> str | None:
        """Type resolver for the raw ``_qualify_where`` path that prefers the
        UNAMBIGUOUS qualified physical key (``table_alias.column``) when the
        rewritten field node carries a table alias. Falls back to the bare-name
        ``_get_col_type`` (which itself refuses ambiguous collisions). Bug-5538
        (Codex round-2 finding 4): resolving by qualified physical expression
        first prevents a semantic/physical bare-name collision from assigning the
        wrong type (string-for-INT or numeric-for-string)."""
        if isinstance(field_node, exp.Column):
            tbl = field_node.table
            col = field_node.name
            if tbl and col:
                qualified = _data_type_by_qualified_phys.get(
                    f"{tbl.lower()}.{col.lower()}"
                )
                if qualified is not None:
                    return qualified
            if col:
                return _get_col_type(col)
        return None

    # Period-aware variant resolution: if any measure carries a period-aware
    # variant (YTD, prior-period, YoY, etc.), resolve calendar rules.
    # Path B (expression-first): derive period boundaries from the hierarchy's
    #   calendar_type — no JOIN needed.
    # Path A (calendar table fallback): only for calendar types that require
    #   a physical table (e.g. retail_445 with complex week patterns).
    _dax_hints_preflight = getattr(bound_query.logical_query, "time_variant_hints", None) or {}
    def _effective_variant_kind(m):
        return getattr(m, "variant_kind", None) or _dax_hints_preflight.get(m.name)

    _period_aware_present = any(
        _effective_variant_kind(m) in TIME_VARIANTS_NEEDING_CALENDAR
        for m in bound_query.resolved_measures
    )
    calendar_columns: dict[str, str] | None = None
    _variant_calendar_type: str | None = None
    _variant_fiscal_start: int | None = None
    if _period_aware_present:
        _time_dim_for_join = next(
            (d for d in bound_query.resolved_dimensions
             if _is_time_dimension(d)),
            None,
        )
        if _time_dim_for_join is None:
            raise SemanticBindingError(
                "Period-aware time variant requires a time dimension in "
                "the query grain; none was found."
            )
        # Bug-3607: the calendar-table JOIN must match cal.date against a real
        # DATE/TIMESTAMP column, not a derived numeric grain dim. Resolve the
        # anchor the same way the variant dispatch does. (_time_dim_for_join is
        # kept as the selected time dim so hierarchy calendar-rule lookup below
        # reads the right hierarchy.)
        _period_aware_measures = [
            m for m in bound_query.resolved_measures
            if _effective_variant_kind(m) in TIME_VARIANTS_NEEDING_CALENDAR
        ]
        # Bug-5247: use the first period-aware measure's resolved_date_col_id
        # when available so the calendar JOIN anchors on the model-service-
        # resolved date column rather than the dimension-scan heuristic.
        _cal_join_date_col_id = next(
            (getattr(m, "resolved_date_col_id", None)
             for m in _period_aware_measures
             if getattr(m, "resolved_date_col_id", None) is not None),
            None,
        )
        _time_phys_for_join, _anchor_dtype = _resolve_variant_date_anchor(
            time_dim=_time_dim_for_join,
            resolved_dimensions=bound_query.resolved_dimensions,
            candidate_dimensions=list(dimensions_by_name.values()),
            columns_by_id=columns_by_id,
            get_phys_expr=_get_phys_expr,
            measure_name="period-aware variant",
            pg_canonical=False,
            resolved_date_col_id=_cal_join_date_col_id,
            alias_by_table_id=alias_by_table_id,
            with_type=True,
        )
        _variant_calendar_type, _variant_fiscal_start = (
            await _resolve_hierarchy_calendar_rules(
                db, _time_dim_for_join, bound_query.model.id
            )
        )
        # Variant-calendar decision (intake 2026-07-07, dominant facet of
        # Bug-6682): a variant measure is DEFINED against its own pinned
        # calendar (resolved_calendar_id, snapshotted at variant-creation
        # time). The expression-vs-table decision must honour THAT calendar,
        # not the query-grain time dimension's hierarchy. Otherwise a
        # retail_445/hijri variant grouped by a Gregorian date dimension
        # takes the expression path, emits Gregorian EXTRACT math, and never
        # joins the retail calendar — silently wrong numbers. When the
        # measure pins a calendar, its calendar_type and fiscal year start
        # override the hierarchy's rules.
        _measure_calendar = await _resolve_calendar_binding(
            db, _period_aware_measures
        )
        _pinned_calendar_type = (
            normalize_calendar_type(
                getattr(_measure_calendar, "calendar_type", None)
            )
            or "standard"
        ) if _measure_calendar is not None else None
        # Bug #1: a variant is DEFINED against its own pinned calendar. The
        # override only matters when that calendar is TABLE-BOUND (retail_445/
        # hijri): those need a materialised-table JOIN + calendar-column math,
        # so the pinned calendar MUST win over the query-grain hierarchy (the
        # expression path would emit wrong Gregorian numbers). For an
        # expression-capable pinned calendar the hierarchy-derived type already
        # produces correct expression math, so we keep HEAD behaviour and do
        # NOT override — this also avoids the Codex over-broad-guard regression
        # of rejecting a pinned-standard + unpinned-standard mix that computes
        # identically.
        if _pinned_calendar_type in TABLE_BOUND_CALENDAR_TYPES:
            # Codex finding (mixed calendars): the single-calendar preflight
            # applies this table-bound calendar to EVERY period-aware measure
            # below. A measure that does NOT share it (unpinned → would use its
            # hierarchy's expression calendar) would be silently forced onto
            # the table calendar. Fail loud. (_resolve_calendar_binding already
            # rejects two measures pinned to DIFFERENT calendars; this covers
            # the table-bound-pinned-vs-unpinned mix.)
            def _measure_is_pinned(m: Any) -> bool:
                return (
                    getattr(m, "resolved_calendar_id", None) is not None
                    or getattr(m, "calendar_model_table_id", None) is not None
                )

            if not all(_measure_is_pinned(m) for m in _period_aware_measures):
                raise VariantSqlError(
                    "This query mixes a period-aware measure pinned to a "
                    "table-bound calendar (retail_445/hijri) with one that is "
                    "not; they cannot share a single calendar context. Split "
                    "the query so each pivot uses a single calendar."
                )
            _variant_calendar_type = _pinned_calendar_type
            _variant_fiscal_start = getattr(
                _measure_calendar, "fiscal_year_start_month", None
            )
        # F-016-04 / F-016-09: the expression-vs-table decision uses the
        # canonical calendar vocabulary. Expression-capable types (standard,
        # fiscal, iso_week, thai_buddhist) compute period boundaries from the
        # fact date. Table-bound types (retail_445, hijri) MUST join a
        # materialised calendar table — computing their periods from the bare
        # Gregorian date would silently return wrong (Gregorian) numbers.
        _expr_capable = _calendar_type_is_expression_capable(_variant_calendar_type)
        if not _expr_capable:
            calendar = _measure_calendar
            if calendar is not None and calendar.date_column:
                calendar_columns = _build_calendar_columns(calendar)
                _cal_lhs = _qcol('cal', calendar.date_column)
                _cal_rhs = _time_phys_for_join
                # Codex finding: coerce using the type of the SAME column the
                # anchor resolved to (_anchor_dtype), not the SELECTED grain
                # time dim's type. When resolved_date_col_id points at a
                # TIMESTAMP while the grain dim is virtual/derived, the old code
                # emitted DATE = TIMESTAMP with no CAST; coerce_join_types now
                # wraps the timestamp side in CAST(... AS DATE).
                _cal_lhs, _cal_rhs = _coerce_join_pair(
                    _cal_lhs, "DATE",
                    _cal_rhs, _anchor_dtype,
                )
                from_clause += (
                    f" LEFT JOIN {_qtbl(calendar.table_name)} AS cal"
                    f" ON {_cal_lhs} = {_cal_rhs}"
                )
                # Bug-6682: KEEP _variant_calendar_type on the table-bound
                # branch. The emitter needs the real type (retail_445/hijri)
                # so _h_ytd_prior_year routes to _table_bound_order_key, which
                # builds the whole composite ORDER BY key (year AND within-year
                # position) from the calendar's OWN columns. Nulling it here
                # made the emitter pair the calendar year key with a Gregorian
                # month*31+day position — a non-monotone key that dropped early
                # prior-year rows and leaked the prior January.
            else:
                # F-016-09: a non-expression calendar type with no bound
                # calendar table cannot be computed correctly. Fail loud
                # instead of silently falling back to Gregorian period math.
                raise VariantSqlError(
                    f"The time hierarchy uses calendar type "
                    f"{(_variant_calendar_type or 'standard')!r}, which requires "
                    f"a bound calendar table for period-aware measures. "
                    f"Create and bind a {(_variant_calendar_type or 'standard')!r} "
                    f"calendar table on this source, then attach it to the "
                    f"hierarchy's date column."
                )

    # Build SELECT parts.  Each piece is stored as (position, sql) so we
    # can preserve the user's original SELECT order at the end — the source
    # rewriter used to emit dimensions, then measures, then literals, then
    # passthroughs, which silently reordered the output columns.
    select_pieces: list[tuple[int, str]] = []
    _TAIL = 10**9  # sentinel for items that must be appended after SELECT items
    dim_group_exprs: list[str] = []
    dim_group_expr_by_name: dict[str, str] = {}  # Bug-874: name-keyed lookup
    field_expr_by_name: dict[str, str] = {}
    _variant_extra_group_by: list[str] = []

    _se_list = list(getattr(bound_query.logical_query, "select_expressions", []))

    def _position_for_column(col_name: str) -> int:
        """Find the first select_expression that originates from this
        column/measure/dimension name.  Returns _TAIL if not directly
        referenced (e.g. grain-only columns added for GROUP BY)."""
        for idx, _se in enumerate(_se_list):
            if _se.inner_column == col_name:
                return idx
            if _se.alias == col_name:
                return idx
        return _TAIL

    # Identify dimension names that appear as standalone bare columns in the
    # original SELECT.  Dimensions wrapped in scalar functions (CASE,
    # COALESCE, CONCAT, SUBSTRING, etc.) are NOT standalone — those are
    # emitted by the passthrough expression loop with the full expression
    # tree intact (Bug-879).
    _standalone_dim_names: set[str] = set()
    _dim_user_alias: dict[str, str] = {}
    for _e in _se_list:
        if _e.classification == "passthrough" and _e.inner_column and not _e.agg_function:
            # Check if the raw_text is actually just the bare column
            # (possibly with AS alias), not a complex expression wrapper.
            _raw = _e.raw_text.strip()
            # Strip trailing " AS alias"
            _before_alias = re.sub(
                r'\s+AS\s+\S+$', '', _raw, flags=re.IGNORECASE
            ).strip().strip('"').split('.')[-1].strip('"')
            _is_bare_col = _before_alias.lower() == _e.inner_column.lower()
            if _is_bare_col:
                _standalone_dim_names.add(_e.inner_column)
                if _e.alias:
                    _dim_user_alias[_e.inner_column] = _e.alias
    # Grain-only dimensions (in GROUP BY but not in SELECT) should also be
    # emitted as standalone columns so the DB can group by them.
    grain_names_set = set(bound_query.logical_query.grain)

    (
        _dim_pieces,
        _dim_group_exprs,
        _dim_group_expr_by_name,
        _dim_field_exprs,
    ) = _build_dimension_select_pieces(
        bound_query,
        columns_by_id=columns_by_id,
        tables_by_id=tables_by_id,
        alias_by_table_id=alias_by_table_id,
        uda_expr_by_id=uda_expr_by_id,
        _se_list=_se_list,
        _standalone_dim_names=_standalone_dim_names,
        grain_names_set=grain_names_set,
        _dim_user_alias=_dim_user_alias,
        _position_for_column=_position_for_column,
    )
    select_pieces.extend(_dim_pieces)
    dim_group_exprs.extend(_dim_group_exprs)
    dim_group_expr_by_name.update(_dim_group_expr_by_name)
    field_expr_by_name.update(_dim_field_exprs)

    # Build measure SELECT pieces (extracted to _build_measure_select_pieces
    # — Phase 3 internal decomposition).
    (
        _meas_pieces, _meas_field_exprs, _meas_variant_group_by,
    ) = _build_measure_select_pieces(
        bound_query,
        columns_by_id=columns_by_id,
        alias_by_table_id=alias_by_table_id,
        uda_expr_by_id=uda_expr_by_id,
        _variant_base_map=_variant_base_map,
        calendar_columns=calendar_columns,
        _variant_calendar_type=_variant_calendar_type,
        _variant_fiscal_start=_variant_fiscal_start,
        candidate_dimensions=list(dimensions_by_name.values()),
        calc_parsed_by_name=calc_parsed_by_name,
        calc_ref_measures_by_name=calc_ref_measures_by_name,
        _se_list=_se_list,
        grain_names_set=grain_names_set,
        _sa_has_time_in_grain=_sa_has_time_in_grain,
        _sa_finest_time_col_id=_sa_finest_time_col_id,
        _qid=_qid,
        _qcol=_qcol,
        _pg_qcol=_pg_qcol,
        _get_phys_expr=_get_phys_expr,
        _position_for_column=_position_for_column,
    )
    select_pieces.extend(_meas_pieces)
    field_expr_by_name.update(_meas_field_exprs)
    _variant_extra_group_by.extend(_meas_variant_group_by)
    # Literal + raw-passthrough SELECT pieces (extracted to
    # _build_literal_passthrough_pieces — Phase 3 internal decomposition).
    select_pieces.extend(
        _build_literal_passthrough_pieces(
            bound_query,
            _se_list=_se_list,
            field_expr_by_name=field_expr_by_name,
            _get_phys_expr=_get_phys_expr,
        )
    )
    # Stable sort by SELECT-expression position, then fall back to insertion
    # order within the same position.  This preserves the user's original
    # SELECT order while keeping grain-only / ORDER-BY-only columns at the end.
    select_pieces.sort(key=lambda p: p[0])
    select_parts: list[str] = [sql for _, sql in select_pieces]

    if not select_parts:
        # Phase 2 fail-loud (Finding 3): nothing could be rendered into the
        # SELECT list despite a resolved FROM clause — refuse to fall back
        # to the raw (semantic) query.
        raise SemanticBindingError(
            "Cannot rewrite query to source SQL: no SELECT expressions "
            "could be rendered from the bound query."
        )

    filter_col_type_by_name: dict[str, str] = {}
    for dim_name in filter_dim_names:
        dim = dimensions_by_name.get(dim_name)
        mc = columns_by_id.get(getattr(dim, "source_column_id", None)) if dim else None
        alias = alias_by_table_id.get(mc.model_table_id) if mc else None
        if alias and mc:
            field_expr_by_name[dim_name] = _qcol(alias, mc.column_name)
            if getattr(mc, "data_type", None):
                filter_col_type_by_name[dim_name] = mc.data_type
            continue
        # F-006-04: calculated-dimension filters must render the qualified
        # calc EXPRESSION in WHERE, not the SELECT alias (line ~812 seeds the
        # alias for ORDER BY use). Without this override the extracted-filter
        # path (_render_where via field_expr_by_name) emits the alias into
        # WHERE, which PostgreSQL/BigQuery reject. Mirrors the calc branch in
        # _get_phys_expr (used by the raw-WHERE / PARTITION BY / HAVING paths).
        calc_expr = getattr(dim, "calc_expression", None) if dim else None
        if calc_expr:
            field_expr_by_name[dim_name] = _qualify_calc_expression(
                calc_expr, columns_by_id, alias_by_table_id,
            )
            continue
        uda_id = getattr(dim, "user_defined_attribute_id", None) if dim else None
        uda = uda_by_id.get(uda_id) if uda_id else None
        expr_body = uda_expr_by_id.get(uda_id) if uda_id else None
        if expr_body:
            field_expr_by_name[dim_name] = f"({expr_body})"
            if uda and getattr(uda, "output_data_type", None):
                filter_col_type_by_name[dim_name] = uda.output_data_type

    distinct = "DISTINCT " if getattr(bound_query.logical_query, "has_distinct", False) else ""
    sql = f"SELECT {distinct}{', '.join(select_parts)} FROM {from_clause}"

    # WHERE clause (extracted to _build_where_clause — Phase 3 internal
    # decomposition).
    sql = _build_where_clause(
        sql,
        bound_query,
        dimensions_by_name=dimensions_by_name,
        _order_measures=_order_measures,
        field_expr_by_name=field_expr_by_name,
        filter_col_type_by_name=filter_col_type_by_name,
        _get_phys_expr=_get_phys_expr,
        _get_col_type=_get_col_type,
        _get_col_type_for_field=_get_col_type_for_field,
        target_dialect=target_dialect,  # Bug-6895: pass through for bracket escaping
    )
    has_explicit_agg = any(e.agg_function is not None for e in getattr(bound_query.logical_query, "select_expressions", []))
    # Use the original grain (GROUP BY columns) not all dimensions.
    # When grain is empty (no GROUP BY in original), do NOT fabricate one.
    grain_names = set(bound_query.logical_query.grain)
    if grain_names:
        # Bug-874: use name-keyed dict instead of positional index.
        # dim_group_exprs may be shorter than resolved_dimensions when
        # a dimension's physical column could not be resolved (skipped
        # during the dimension loop above).
        grain_group_exprs = [
            dim_group_expr_by_name[dim.name]
            for dim in bound_query.resolved_dimensions
            if dim.name in grain_names and dim.name in dim_group_expr_by_name
        ]
        if _variant_extra_group_by:
            grain_group_exprs.extend(_variant_extra_group_by)
        # Bug-7015: emit GROUP BY whenever grain is non-empty AND the
        # query requires grouping. The old guard
        # ``(resolved_measures or has_explicit_agg)`` silently dropped the
        # GROUP BY for dimension-only queries with an explicit GROUP BY
        # (e.g. ``SELECT region FROM model GROUP BY region``), producing
        # duplicate rows on the source route. The fix emits GROUP BY when:
        #   - there are measures or explicit aggregates (original condition),
        #     OR
        #   - the query is NOT a DISTINCT query (a non-DISTINCT, non-aggregate
        #     query with grain means the user wrote GROUP BY explicitly).
        # A DISTINCT query that has grain but no measures/aggs does NOT need
        # a redundant GROUP BY -- DISTINCT already handles deduplication.
        _needs_group_by = (
            bound_query.resolved_measures
            or has_explicit_agg
            or not getattr(bound_query.logical_query, "has_distinct", False)
        )
        if grain_group_exprs and _needs_group_by:
            sql += f" GROUP BY {', '.join(grain_group_exprs)}"

    # Build SELECT alias map so HAVING and ORDER BY can reference aliases like
    # "total_amount" from "SUM(transaction_amount) AS total_amount".
    # Bug-5196: moved BEFORE HAVING so aliases are resolved there too.
    _select_alias_map: dict[str, str] = {}
    # Bug-6122: map each SELECT alias to the UNDERLYING expression text (the
    # aggregate/column before ``AS``). PostgreSQL and SQL Server reject a SELECT
    # alias in HAVING (unlike ORDER BY / GROUP BY), so a HAVING that references
    # ``total`` from ``SUM(amount) AS total`` must be rewritten to the source
    # aggregate ``SUM("f"."amount")`` — emitting the quoted alias produces a
    # "column does not exist" error at the source.
    _alias_to_raw_expr: dict[str, str] = {}
    for _e in getattr(bound_query.logical_query, "select_expressions", []):
        if _e.alias and _e.alias not in field_expr_by_name:
            _select_alias_map[_e.alias] = _qid(_e.alias)
        if _e.alias and getattr(_e, "raw_text", None):
            # Strip the trailing ``AS <alias>`` to recover the underlying
            # expression. Parse the SELECT item and take the aliased node's
            # child so a quoted alias containing a space (``... AS "my total"``)
            # is handled correctly; fall back to a regex strip only if the raw
            # text does not parse standalone.
            try:
                _raw_ast = sqlglot.parse_one(_e.raw_text, read="postgres")
                if isinstance(_raw_ast, exp.Alias):
                    _raw_underlying = _raw_ast.this.sql(dialect="postgres")
                else:
                    _raw_underlying = _e.raw_text
            except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
                _raw_underlying = re.sub(
                    r"\s+AS\s+\S+$", "", _e.raw_text, flags=re.IGNORECASE
                )
            _alias_to_raw_expr[_e.alias] = _raw_underlying

    # HAVING: preserve from original query if present.
    # Bug-5196: resolve SELECT aliases in HAVING qualification, not just
    # physical column names. A HAVING clause like ``HAVING total > 100``
    # where ``total`` is ``SUM(amount) AS total`` must resolve to the
    # underlying aggregate (Bug-6122) so the source DB can evaluate it.
    having_raw = getattr(bound_query.logical_query, "having_raw", None)
    if having_raw:
        try:
            having_ast = sqlglot.parse_one(
                f"SELECT 1 {having_raw}", read="postgres"
            )
            having_node = having_ast.find(exp.Having)
            if having_node:
                def _qualify_cols_only(node):
                    # Qualify bare column references to their physical form
                    # WITHOUT re-expanding SELECT aliases — used when inlining an
                    # alias's underlying aggregate so a self-named alias
                    # (``SUM(amount) AS amount``) cannot recurse forever.
                    if isinstance(node, exp.Column):
                        phys = _get_phys_expr(node.name)
                        if phys:
                            return sqlglot.parse_one(phys, read="postgres")
                        fld = field_expr_by_name.get(node.name)
                        if fld:
                            return sqlglot.parse_one(fld, read="postgres")
                    return node

                def _qualify_having(node):
                    if isinstance(node, exp.Column):
                        # Bug-6122: a BARE alias reference (one NOT nested
                        # anywhere inside an aggregate) must expand to the
                        # underlying aggregate, not the quoted alias. A column
                        # that has ANY AggFunc ancestor is already an aggregate
                        # argument (``SUM(amount)``, ``SUM(amount * 2)``) where
                        # the alias must NOT apply — otherwise
                        # ``SUM(amount) AS amount`` + ``HAVING SUM(amount * 2)``
                        # would double-aggregate to ``SUM(SUM(...) * 2)``.
                        # ``find_ancestor`` walks the whole parent chain, so it
                        # catches columns nested inside arithmetic within the
                        # aggregate (``node.parent`` alone would miss them).
                        if node.find_ancestor(exp.AggFunc) is None:
                            raw_underlying = _alias_to_raw_expr.get(node.name)
                            if raw_underlying is not None:
                                try:
                                    inner = sqlglot.parse_one(
                                        raw_underlying, read="postgres"
                                    )
                                    return inner.transform(_qualify_cols_only)
                                except (sqlglot.errors.ParseError,
                                        sqlglot.errors.TokenError):
                                    pass
                        # Try physical column expression (dimension / measure
                        # name -> qualified physical reference).
                        phys = _get_phys_expr(node.name)
                        if phys:
                            return sqlglot.parse_one(phys, read="postgres")
                        # Try field expression map (semantic name -> physical).
                        fld = field_expr_by_name.get(node.name)
                        if fld:
                            return sqlglot.parse_one(fld, read="postgres")
                        # Last resort: quoted alias (only valid where the target
                        # dialect accepts SELECT aliases in HAVING).
                        alias_expr = _select_alias_map.get(node.name)
                        if alias_expr:
                            return sqlglot.parse_one(alias_expr, read="postgres")
                    return node
                sql += " " + having_node.transform(_qualify_having).sql(dialect="postgres")
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
            # Parse failure: fall back to the raw HAVING text. Non-parse
            # errors (SemanticBindingError, ValueError) propagate so the
            # user gets a clear diagnostic instead of a confusing source-DB
            # "column does not exist" error.
            sql += " " + having_raw

    # F-003-01: the parser only extracts ORDER BY items that are faithfully
    # representable as ``(bare_column, direction)``. When the ORDER BY contains
    # any expression sort key (function, arithmetic, CASE, aggregate), the
    # parser sets ``has_unresolvable_order`` and the extracted ``order_by`` list
    # would be partial — emitting it would silently change the sort (and, with
    # LIMIT, the returned rows). Preserve the FULL raw ORDER BY instead, mapping
    # each semantic column to its physical expression (mirrors the raw-WHERE
    # preservation above), so the user's exact sort expression is honoured.
    if getattr(bound_query.logical_query, "has_unresolvable_order", False):
        base_alias = base_table.alias if base_table and base_table.alias else "base"
        raw_order = _extract_raw_order_node(bound_query.logical_query)
        if raw_order is not None:
            def _qualify_order(node):
                if isinstance(node, exp.Column):
                    name = node.name
                    expr = field_expr_by_name.get(name)
                    if not expr:
                        expr = _select_alias_map.get(name)
                    if not expr:
                        expr = _get_phys_expr(name)
                    if not expr:
                        expr = _qcol(base_alias, name)
                    return sqlglot.parse_one(expr, read="postgres")
                return node
            rendered = raw_order.transform(_qualify_order).sql(dialect="postgres")
            sql += f" {rendered}"
    elif bound_query.logical_query.order_by:
        order_parts = []
        for col, direction in bound_query.logical_query.order_by:
            # Resolve ORDER BY columns via field_expr_by_name first,
            # then SELECT aliases, then _get_phys_expr for columns not
            # in SELECT.
            #
            # F-006-12 (Bug-2740): if all three fail the column is unknown
            # to the semantic model. The previous behaviour qualified the
            # bare name with the base (fact) alias — a guess that produced a
            # source error naming an INTERNAL alias (e.g. ``"base"."typo"
            # does not exist``) instead of a clean, user-facing binding
            # error. A column that DOES resolve is always returned qualified
            # by one of the three maps, so the base-alias path could only
            # ever fire for a genuinely-unresolvable name. Fail loud instead
            # of guessing (mirrors the Phase 2 fail-loud sweep in the
            # SELECT/WHERE builders).
            expr = field_expr_by_name.get(col)
            if not expr:
                expr = _select_alias_map.get(col)
            if not expr:
                expr = _get_phys_expr(col)
            if not expr:
                raise SemanticBindingError(
                    f"Cannot resolve ORDER BY column {col!r}: it is not a "
                    f"selected expression, a SELECT alias, or a known "
                    f"dimension/measure of this model."
                )
            order_parts.append(f"{expr} {direction.upper()}")
        sql += f" ORDER BY {', '.join(order_parts)}"

    if bound_query.logical_query.limit is not None:
        sql += f" LIMIT {bound_query.logical_query.limit}"

    if bound_query.logical_query.offset is not None:
        sql += f" OFFSET {bound_query.logical_query.offset}"

    return _final_transpile(sql)
