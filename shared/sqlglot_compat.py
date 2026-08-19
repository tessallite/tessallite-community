"""Shared sqlglot dialect patches for gaps in sqlglot 30.x.

Bug-982: BigQuery has no aggregate (GROUP BY) exact quantile. Its
``PERCENTILE_CONT`` / ``PERCENTILE_DISC`` are analytic-only (they require an
``OVER()`` clause) and the only aggregate quantile is the APPROXIMATE
``APPROX_QUANTILES``. The rendering of an ordered-set percentile for BigQuery is
therefore **capability-selected**:

- EXACT mode (the default) rewrites the whole statement to the exact analytic
  form ``PERCENTILE_CONT|PERCENTILE_DISC(col, frac IGNORE NULLS)
  OVER (PARTITION BY <grain>)`` with ``SELECT DISTINCT`` and no ``GROUP BY``
  (``rewrite_bigquery_exact_quantiles``). When the statement shape cannot be
  rendered exactly, it raises the typed :class:`ExactQuantileUnavailable`
  (reason ``EXACT_QUANTILE_UNAVAILABLE``) so the caller routes on it rather than
  silently returning an approximate number.
- APPROX mode (explicit caller opt-in) keeps the ``APPROX_QUANTILES`` generator
  transform (``_bq_within_group_sql``).

The exact rewrite is a whole-statement operation (it must add the PARTITION BY
grain and dedupe the per-row analytic), so it runs before the generator on the
sqlglot AST; the generator-level ``_bq_within_group_sql`` transform is used only
for the APPROX path and, in EXACT mode, fails closed if a percentile
``WITHIN GROUP`` node ever survives to generation.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery as _BigQueryDialect


EXACT_QUANTILE_UNAVAILABLE = "EXACT_QUANTILE_UNAVAILABLE"


class ExactQuantileUnavailable(Exception):
    """Raised when an EXACT percentile cannot be rendered for the target dialect
    and statement shape, so the caller must route to an exact plan or surface a
    typed error — never a silent approximation (Bug-982, spec §5.8 / Gap F).

    Carries a stable ``reason`` code (``EXACT_QUANTILE_UNAVAILABLE``) and a safe,
    modeller-facing message (no physical column names, credentials, or security
    detail).
    """

    reason = EXACT_QUANTILE_UNAVAILABLE

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# Bug-982 fail-closed backstop. EXACT is the default accuracy mode (spec I9:
# absent policy means exact). The BigQuery generator transform
# ``_bq_within_group_sql`` therefore REFUSES to emit an approximate
# ``APPROX_QUANTILES`` for a percentile ordered-set aggregate unless approximate
# mode is EXPLICITLY entered via ``bigquery_approx_quantiles()``. Without this,
# any percentile ``WITHIN GROUP`` reaching ANY ``.sql(dialect="bigquery")`` call
# (the raw/passthrough/no-columns/derived-expression source paths, not only the
# ``_build_source_sql`` boundary that runs the exact statement rewrite) would
# silently return an approximate value — the exact wrong-number class Bug-982
# closes. In exact mode such a node raises :class:`ExactQuantileUnavailable`
# (fail closed), closing the silent-approximation class on EVERY BigQuery
# generation path.
#
# Error VISIBILITY differs by path. On the ``_build_source_sql`` boundary the
# raise propagates as the typed ``ExactQuantileUnavailable`` the caller can route
# on. On the raw/passthrough sibling paths the raise is CAUGHT by the broad
# ``except Exception`` fallbacks in ``dialects.py`` (``_transpile_to_dialect`` /
# ``_translate_raw_sql`` / ``_requote_identifiers_for_bigquery``): those return
# the un-transpiled PG SQL, which then fails LOUD at BigQuery (a source syntax
# error) rather than returning a wrong number. Threading the typed code through
# those wrappers touches out-of-scope ``dialects.py`` and is deferred (see the
# Bug-982 registry note); the safety-critical invariant — never a silent
# approximate — holds on all paths.
import contextlib as _contextlib
from contextvars import ContextVar as _ContextVar

# Task-scoped (NOT thread-local): the query-router is an asyncio service, so a
# thread-local would leak the accuracy mode across concurrent tasks that share
# the event-loop thread (a caller holding approx mode across an ``await`` would
# make a concurrent EXACT request silently approximate — the Bug-982 wrong-number
# class). A ``ContextVar`` is copied per asyncio Task, so each request keeps its
# own accuracy mode.
_APPROX_ENABLED: _ContextVar[bool] = _ContextVar(
    "bq_approx_quantiles_enabled", default=False
)


def _approx_quantiles_enabled() -> bool:
    return _APPROX_ENABLED.get()


@_contextlib.contextmanager
def bigquery_approx_quantiles():
    """Enter APPROXIMATE quantile mode for BigQuery percentile rendering.

    Inside this context the BigQuery generator transform emits
    ``APPROX_QUANTILES`` for a percentile ordered-set aggregate; outside it (the
    default EXACT mode) an un-rewritten percentile ``WITHIN GROUP`` reaching the
    BigQuery generator raises :class:`ExactQuantileUnavailable`.

    Task-scoped via a ``ContextVar`` so concurrent asyncio requests do not leak
    each other's accuracy mode. Callers opt into approximation explicitly per the
    accuracy policy (spec I9).
    """
    token = _APPROX_ENABLED.set(True)
    try:
        yield
    finally:
        _APPROX_ENABLED.reset(token)


def _bq_splitpart_sql(
    self: _BigQueryDialect.Generator, expression: exp.SplitPart
) -> str:
    """SPLIT_PART(s, d, n) -> SPLIT(s, d)[OFFSET(n - 1)]"""
    s = self.sql(expression, "this")
    d = self.sql(expression, "delimiter")
    idx = expression.args.get("part_index")
    if isinstance(idx, exp.Literal) and idx.is_int:
        offset = str(int(idx.this) - 1)
    else:
        offset = f"{self.sql(idx)} - 1"
    return f"SPLIT({s}, {d})[OFFSET({offset})]"


# ---------------------------------------------------------------------------
# Bug-982 EXACT-mode analytic rewrite
# ---------------------------------------------------------------------------

# Percentile ordered-set aggregate node types handled by the exact rewrite.
_PERCENTILE_NODES = (exp.PercentileCont, exp.PercentileDisc)


def _percentile_within_group(node: exp.Expression) -> bool:
    return isinstance(node, exp.WithinGroup) and isinstance(
        node.this, _PERCENTILE_NODES
    )


def _ordered_column(within_group: exp.WithinGroup) -> exp.Ordered | None:
    """Return the single ``Ordered`` node of a percentile WITHIN GROUP, or None
    when the ordered column cannot be uniquely identified (unsupported shape)."""
    order = within_group.args.get("expression")
    if not isinstance(order, exp.Order):
        return None
    exprs = order.expressions
    if len(exprs) != 1:
        return None
    ordered = exprs[0]
    if isinstance(ordered, exp.Ordered):
        return ordered
    # Bare column with no explicit direction: wrap so callers see one shape.
    return exp.Ordered(this=ordered)


def _literal_fraction(percentile: exp.Expression) -> Decimal | None:
    """Exact ``Decimal`` fraction of a percentile node, or None if not a
    numeric literal (parameterised fractions cannot be rendered as an exact
    analytic call with a constant fraction)."""
    frac = percentile.this
    if isinstance(frac, exp.Literal) and not frac.is_string:
        try:
            return Decimal(str(frac.this))
        except (TypeError, ValueError, ArithmeticError):
            return None
    return None


def _has_column_free_function(node: exp.Expression) -> bool:
    """True when *node* contains a FUNCTION call that takes NO column argument.

    A column-free function is the structural signature of a volatile /
    session-state / non-deterministic value: ``CURRENT_TIMESTAMP``,
    ``UTC_TIMESTAMP()``, ``LOCALTIMESTAMP``, ``SESSION_USER``, ``RANDOM()``,
    ``RANDSTR(10, 1)``, ``UUID()``, ``NEXTVAL('seq')`` and every dialect synonym
    are ``exp.Func`` subclasses (or ``exp.Anonymous``) whose argument subtree
    contains no ``exp.Column``. This is a NAME-INDEPENDENT test, so it closes the
    whole volatile class permanently — the earlier hand-maintained volatile-name
    list was inherently incomplete against sqlglot's evolving node taxonomy
    (LOCALTIMESTAMP/UTC_TIMESTAMP/RANDSTR/SESSION_USER/CURRENT_SESSION each
    escaped it before being patched).

    A DETERMINISTIC function of a grouping key — ``UPPER(region)``,
    ``DATE_TRUNC('month', d)`` — is NOT column-free (its subtree references the
    grain column), so it is correctly treated as constant per partition. The
    only conservative over-rejection is a rare column-free DETERMINISTIC function
    (``CAST(1 AS BIGINT)``), which routes to source instead of being served —
    always correctness-safe, never a wrong number.
    """
    for sub in node.walk():
        if isinstance(sub, (exp.Func, exp.Anonymous)):
            if not any(isinstance(c, exp.Column) for c in sub.walk()):
                return True
    return False


def _is_constant_per_partition(
    projection: exp.Expression, grain_fingerprints: set[str]
) -> bool:
    """True when ``projection`` provably yields one value per partition (group).

    A projection is constant per partition when it equals a grain expression, or
    when it references only grain columns AND contains no column-free function
    call (the structural signature of a volatile/session value — see
    ``_has_column_free_function``), or when it is a pure literal/operator
    constant. A bare non-grain column, an expression over a non-grain column, or
    any volatile / non-deterministic function is NOT constant per partition —
    selecting it alongside the analytic percentile under ``SELECT DISTINCT``
    would return one row per distinct value (row multiplication / row-count
    divergence), not one row per group — so it fails closed to the typed error.
    """
    if projection.sql(dialect="postgres") in grain_fingerprints:
        return True
    if _has_column_free_function(projection):
        return False
    columns = list(projection.find_all(exp.Column))
    if not columns:
        # No columns and no column-free function — a pure literal/operator
        # constant (e.g. 1, 'x', 1 + 2).
        return True
    return all(c.sql(dialect="postgres") in grain_fingerprints for c in columns)


def _build_analytic_percentile(
    *,
    is_cont: bool,
    column: exp.Expression,
    fraction: Decimal,
    partition_by: list[exp.Expression],
) -> exp.Expression:
    """Build ``PERCENTILE_CONT|DISC(col, frac IGNORE NULLS) OVER (PARTITION BY …)``
    as a sqlglot AST node (rendered to BigQuery by the generator).

    Null policy is rendered EXPLICITLY as ``IGNORE NULLS`` (spec §5.8 reviewer
    note) — engine defaults differ and a silent ``RESPECT NULLS`` would change
    the population and the returned value.
    """
    percentile_cls = exp.PercentileCont if is_cont else exp.PercentileDisc
    percentile = percentile_cls(
        this=column.copy(),
        expression=exp.Literal.number(_format_fraction(fraction)),
    )
    ignore_nulls = exp.IgnoreNulls(this=percentile)
    window = exp.Window(this=ignore_nulls, over="OVER")
    if partition_by:
        window.set("partition_by", [p.copy() for p in partition_by])
    return window


def _format_fraction(fraction: Decimal) -> str:
    """Render a Decimal fraction without spurious exponent/trailing-zero noise
    so the emitted literal reads like the authored one (0.9, 0.5, 0.125)."""
    normalized = fraction.normalize()
    text = format(normalized, "f")
    return text


def rewrite_bigquery_exact_quantiles(tree: exp.Expression) -> exp.Expression:
    """EXACT-mode BigQuery quantile rewrite (Bug-982, spec §5.8 / Gap F).

    Rewrites a ``SELECT <grain>, PERCENTILE_CONT|DISC(p) WITHIN GROUP
    (ORDER BY col) … FROM … GROUP BY <grain>`` statement into the exact analytic
    form::

        SELECT DISTINCT <grain>,
               PERCENTILE_CONT|DISC(col, frac IGNORE NULLS)
                   OVER (PARTITION BY <grain>) AS <alias>
        FROM <filtered source>

    The ``GROUP BY`` is removed and the SELECT is marked ``DISTINCT`` to collapse
    the per-row analytic result to one row per group. The rewrite is applied to
    the (already filtered / joined) PostgreSQL-canonical statement; the caller
    transpiles the result to BigQuery so identifier quoting and syntax are
    produced by the generator (SQL Rule 1).

    Raises :class:`ExactQuantileUnavailable` when the statement shape cannot be
    rendered exactly (Phase-0 minimum core): the ``SELECT DISTINCT`` dedupe is
    only proven when every selected expression is constant per partition, so a
    statement that mixes the percentile with another aggregate at the same
    level, a percentile outside the top-level projection, a non-literal
    fraction, an unidentifiable ordered column, or a discrete DESC request
    (BigQuery's analytic ``PERCENTILE_DISC`` is ascending-only and has no
    data-independent ascending equivalent) is not achievable here.

    Only mutates ``Select`` statements that actually contain a percentile
    ordered-set aggregate; every other statement is returned unchanged.
    """
    percentile_groups = [
        n for n in tree.find_all(exp.WithinGroup) if _percentile_within_group(n)
    ]
    if not percentile_groups:
        return tree

    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        raise ExactQuantileUnavailable(
            "An exact percentile can only be rendered inside a SELECT statement "
            "for this source."
        )

    # Every percentile must be a top-level projection of THIS select. A
    # percentile nested in HAVING / a subquery / an arithmetic expression is not
    # covered by the Phase-0 minimum-core renderer.
    projections = list(select.expressions)
    projection_within_groups: list[exp.WithinGroup] = []
    for proj in projections:
        target = proj.this if isinstance(proj, exp.Alias) else proj
        if _percentile_within_group(target):
            projection_within_groups.append(target)

    if len(projection_within_groups) != len(percentile_groups):
        raise ExactQuantileUnavailable(
            "An exact percentile is only supported as a direct selected column "
            "for this source; it cannot appear inside HAVING, a subquery, or a "
            "compound expression here."
        )

    # Resolve the GROUP BY grain first — it is both the analytic PARTITION BY and
    # the reference set for the constant-per-partition proof below. ROLLUP / CUBE
    # / GROUPING SETS produce MULTIPLE grains (subtotal rows) that a single
    # PARTITION BY cannot express — reading only ``group.expressions`` would
    # silently emit a global (or wrong) partition and mislabel subtotal rows
    # (spec §5.4: grouping sets route to source). A non-column grain item
    # (ordinal literal, expression) likewise cannot be a trustworthy partition
    # key here. Both fail closed (Fable R1 finding 3).
    group = select.args.get("group")
    partition_by: list[exp.Expression] = []
    if isinstance(group, exp.Group):
        for _multi_key in ("rollup", "cube", "grouping_sets", "totals"):
            if group.args.get(_multi_key):
                raise ExactQuantileUnavailable(
                    "An exact percentile with ROLLUP/CUBE/GROUPING SETS cannot "
                    "be served for this source; each grouping set needs its own "
                    "proof."
                )
        for _grain_item in group.expressions:
            if isinstance(_grain_item, exp.Literal):
                raise ExactQuantileUnavailable(
                    "An exact percentile with a positional/literal GROUP BY key "
                    "cannot be served exactly for this source."
                )
        partition_by = list(group.expressions)

    # Canonical SQL text of each grain expression — the set of expressions that
    # are constant within a partition.
    grain_fingerprints = {g.sql(dialect="postgres") for g in partition_by}

    # After the GROUP BY is dropped and the SELECT is marked DISTINCT, dedupe is
    # only sound when EVERY non-percentile projection is CONSTANT PER PARTITION
    # (spec §5.8). A projection is constant per partition only when it is a
    # literal/constant or references solely grain expressions. Anything else — a
    # bare non-grain source column (row explosion under DISTINCT), another
    # aggregate (hybrid plan, deferred), or a window function that numbers source
    # rows — is not constant and fails closed with the typed error.
    for proj in projections:
        target = proj.this if isinstance(proj, exp.Alias) else proj
        if _percentile_within_group(target):
            continue
        if any(isinstance(n, exp.AggFunc) for n in target.walk()):
            raise ExactQuantileUnavailable(
                "An exact percentile cannot be combined with another aggregate "
                "in the same query for this source; request the percentile on "
                "its own or use a source that supports aggregate quantiles."
            )
        if any(isinstance(n, exp.Window) for n in target.walk()):
            raise ExactQuantileUnavailable(
                "An exact percentile cannot be combined with another window "
                "function in the same query for this source."
            )
        if not _is_constant_per_partition(target, grain_fingerprints):
            raise ExactQuantileUnavailable(
                "An exact percentile can only be selected alongside its grouping "
                "keys for this source; a non-grouped column would multiply the "
                "returned rows."
            )

    # Grain ⊆ projections: the projected tuple must UNIQUELY IDENTIFY each
    # partition, or ``SELECT DISTINCT`` merges distinct groups that happen to
    # share equal projected values — a silent row collapse (Fable R2 finding 1).
    # Aggregate GROUP BY semantics return one row per group EVEN WHEN a grain key
    # is not projected (duplicate labels allowed, spec §5.4); DISTINCT cannot
    # reproduce that when a grain key is missing from the projection. A grain key
    # bound only inside a complex expression (Bug-879: a CASE/COALESCE bucket
    # over the key) is likewise not a bare projected key, so groups sharing the
    # same bucket value and equal percentile collapse. Fail closed unless every
    # partition-by expression appears verbatim among the projected expressions.
    projection_fingerprints: set[str] = set()
    for proj in projections:
        target = proj.this if isinstance(proj, exp.Alias) else proj
        if _percentile_within_group(target):
            continue
        projection_fingerprints.add(target.sql(dialect="postgres"))
    missing_grain = grain_fingerprints - projection_fingerprints
    if missing_grain:
        raise ExactQuantileUnavailable(
            "An exact percentile for this source requires every grouping key to "
            "be selected so each group is returned exactly once; a grouping key "
            "is missing from the selected columns."
        )

    # HAVING / QUALIFY filter the aggregated groups. Dropping the GROUP BY to
    # emit the analytic form would orphan a HAVING (invalid BigQuery) and a
    # HAVING aggregate cannot be reproduced over the analytic rewrite — fail
    # closed rather than emit invalid or wrong SQL (Fable R1 finding 2).
    if select.args.get("having") is not None or select.args.get("qualify") is not None:
        raise ExactQuantileUnavailable(
            "An exact percentile with a HAVING/QUALIFY filter cannot be served "
            "for this source; the group filter cannot be reproduced over the "
            "analytic rewrite."
        )

    # An ORDER BY that references an aggregate is likewise not constant per
    # partition after the GROUP BY is dropped.
    order = select.args.get("order")
    if isinstance(order, exp.Order):
        if any(isinstance(n, exp.AggFunc) for n in order.walk()):
            raise ExactQuantileUnavailable(
                "An exact percentile with an aggregate in ORDER BY cannot be "
                "served for this source."
            )

    # Build the replacement analytic node for each percentile projection.
    for within_group in projection_within_groups:
        percentile = within_group.this
        is_cont = isinstance(percentile, exp.PercentileCont)

        ordered = _ordered_column(within_group)
        if ordered is None:
            raise ExactQuantileUnavailable(
                "The percentile's ordered column could not be identified for an "
                "exact rewrite on this source."
            )
        column = ordered.this
        is_desc = bool(ordered.args.get("desc"))

        fraction = _literal_fraction(percentile)
        if fraction is None:
            raise ExactQuantileUnavailable(
                "An exact percentile requires a literal fraction for this "
                "source; a parameterised or computed fraction cannot be served "
                "exactly."
            )

        if is_desc:
            if is_cont:
                # Continuous interpolation is positionally symmetric:
                # CONT(p) DESC == CONT(1 - p) ASC for every multiset.
                fraction = Decimal(1) - fraction
            else:
                # Discrete selects the value at a rank in the authored
                # direction; BigQuery's analytic PERCENTILE_DISC is
                # ascending-only and no ascending fraction reproduces a DESC
                # rank for all n (spec §4.1). Fail closed.
                raise ExactQuantileUnavailable(
                    "A discrete percentile in descending order cannot be served "
                    "exactly on this source; build the requested direction or "
                    "use an exact-capable source."
                )

        analytic = _build_analytic_percentile(
            is_cont=is_cont,
            column=column,
            fraction=fraction,
            partition_by=partition_by,
        )
        within_group.replace(analytic)

    # Collapse the per-row analytic to one row per group and drop the GROUP BY.
    select.set("distinct", exp.Distinct())
    select.set("group", None)

    # Bug-7841: grainless (no GROUP BY / no PARTITION BY) percentile over an
    # EMPTY input returns 0 rows from the analytic SELECT DISTINCT ... OVER ().
    # The PostgreSQL-canonical ordered-set aggregate (PERCENTILE_CONT(p) WITHIN
    # GROUP (ORDER BY col) with no GROUP BY) returns exactly ONE row with a NULL
    # value for empty input.  Wrap the grainless rewrite in an outer one-row
    # aggregate so the cardinality invariant holds.
    #
    # Codex gate finding A: MIN-wrap ONLY the analytic/percentile columns.
    # Constant/literal (non-analytic) projections must be emitted DIRECTLY
    # (un-wrapped) in the outer SELECT -- a bare constant in an aggregate-
    # no-GROUP-BY query returns exactly one row with the correct literal value.
    # MIN(constant) over 0 rows returns NULL, silently nulling the constant on
    # empty input, which diverges from PG semantics (constants survive an empty
    # aggregate group).
    if not partition_by:
        # Regression 2: when the percentile SELECT is a branch of a set
        # operation (UNION/INTERSECT/EXCEPT), `select is not tree`.  Splicing
        # the wrapped subtree back reliably is fragile; fail closed instead
        # of silently dropping the other branches.
        if select is not tree:
            raise ExactQuantileUnavailable(
                "A grainless percentile inside a set operation "
                "(UNION/INTERSECT/EXCEPT) cannot be served exactly for "
                "this source."
            )

        inner_select = select.copy()

        # Regression 1: hoist LIMIT/OFFSET/ORDER off the inner copy so they
        # apply AFTER the outer aggregate (PG semantics: aggregate first,
        # then limit/offset).  Without this, LIMIT 0 inside the subquery
        # empties it and the outer aggregate manufactures a spurious NULL row.
        hoisted_limit = inner_select.args.pop("limit", None)
        hoisted_offset = inner_select.args.pop("offset", None)
        hoisted_order = inner_select.args.pop("order", None)

        outer_projections = []
        for idx, proj in enumerate(inner_select.expressions):
            inner_expr = proj.this if isinstance(proj, exp.Alias) else proj

            # Regression 3: explicit fail-closed for SELECT * alongside a
            # percentile in the grainless case.  Star has no column and
            # would render as `* AS _q0` (invalid SQL).
            if isinstance(inner_expr, exp.Star):
                raise ExactQuantileUnavailable(
                    "A grainless percentile with SELECT * cannot be served "
                    "exactly for this source."
                )
            if isinstance(inner_expr, exp.Column) and isinstance(
                inner_expr.this, exp.Star
            ):
                raise ExactQuantileUnavailable(
                    "A grainless percentile with a qualified SELECT * "
                    "cannot be served exactly for this source."
                )

            is_analytic = any(isinstance(n, exp.Window) for n in inner_expr.walk())

            if isinstance(proj, exp.Alias):
                alias_name = proj.alias
            else:
                alias_name = f"_q{idx}"
                aliased = exp.Alias(
                    this=proj.copy(), alias=exp.to_identifier(alias_name),
                )
                inner_select.expressions[idx] = aliased

            if is_analytic:
                outer_projections.append(
                    exp.Alias(
                        this=exp.Min(this=exp.Column(this=exp.to_identifier(alias_name))),
                        alias=exp.to_identifier(alias_name),
                    )
                )
            else:
                # Constant/literal: emit directly so its value survives
                # empty input.  Invariant: every non-percentile projection
                # was validated constant by _is_constant_per_partition.
                outer_projections.append(
                    exp.Alias(
                        this=inner_expr.copy(),
                        alias=exp.to_identifier(alias_name),
                    )
                )

        outer = exp.Select().from_(
            exp.Subquery(this=inner_select, alias=exp.to_identifier("_sub"))
        )
        for op in outer_projections:
            outer = outer.select(op, copy=False)

        # Hoist limit/offset/order onto the outer select.
        if hoisted_order is not None:
            outer.set("order", hoisted_order)
        if hoisted_limit is not None:
            outer.set("limit", hoisted_limit)
        if hoisted_offset is not None:
            outer.set("offset", hoisted_offset)

        return outer

    return tree


def _bq_within_group_sql(
    self: _BigQueryDialect.Generator, expression: exp.WithinGroup
) -> str:
    """APPROX-mode BigQuery rendering of ``PERCENTILE_CONT/DISC(p) WITHIN GROUP
    (ORDER BY col)`` -> ``APPROX_QUANTILES(col, 100)[OFFSET(ROUND(p * 100))]``.

    This is the APPROXIMATE renderer, reachable only when the caller has
    explicitly opted into approximate mode. In EXACT mode (the default) the
    whole-statement :func:`rewrite_bigquery_exact_quantiles` consumes the
    percentile WITHIN GROUP nodes before generation, so this transform never
    sees a percentile node on the exact path.

    BigQuery's ``PERCENTILE_CONT`` / ``PERCENTILE_DISC`` are analytic-only (they
    require an ``OVER()`` clause) and are invalid inside a GROUP BY aggregate
    query, so the ordered-set ``WITHIN GROUP`` form errors on real BigQuery.
    ``APPROX_QUANTILES(col, N)`` returns the N+1 ascending quantile boundaries;
    the requested percentile is the boundary at ``ROUND(p * 100)``.

    ORDER direction: ``APPROX_QUANTILES`` always sorts ASCENDING, so a
    ``WITHIN GROUP (ORDER BY col DESC)`` maps to the ascending boundary at
    ``100 - ROUND(p * 100)``.

    Rounding: BigQuery's ``ROUND`` is half-up; a folded literal offset uses
    Decimal ``ROUND_HALF_UP`` to match.

    Falls through to the default ``WITHIN GROUP`` rendering for any non-
    percentile aggregate (e.g. ``STRING_AGG ... WITHIN GROUP``).
    """
    inner = expression.this
    if not isinstance(inner, _PERCENTILE_NODES):
        return self.withingroup_sql(expression)

    # Bug-982 fail-closed backstop: in EXACT mode (the default) a percentile
    # ordered-set aggregate must NOT be approximated. The exact analytic rewrite
    # (rewrite_bigquery_exact_quantiles) consumes these nodes before generation
    # on the certified source path; if one reaches the generator here in exact
    # mode it came from an un-rewritten sibling path (raw/passthrough/no-columns/
    # derived expression), so refuse rather than silently return an approximate
    # value. Approximate mode is entered explicitly via bigquery_approx_quantiles().
    if not _approx_quantiles_enabled():
        raise ExactQuantileUnavailable(
            "An exact percentile against this source cannot be rendered for this "
            "query shape; it is not eligible for the exact analytic rewrite and "
            "approximate mode was not requested."
        )

    fraction_node = inner.this
    order = expression.args.get("expression")
    ordered = None
    if isinstance(order, exp.Order):
        exprs = order.expressions
        if exprs:
            ordered = exprs[0]
    if ordered is None:
        # Cannot identify the ordered column — defer to default rendering.
        return self.withingroup_sql(expression)

    is_desc = bool(isinstance(ordered, exp.Ordered) and ordered.args.get("desc"))
    col_node = ordered.this if isinstance(ordered, exp.Ordered) else ordered
    col_sql = self.sql(col_node)

    # Resolve the OFFSET index. ASCENDING => ROUND(p * 100); DESCENDING =>
    # 100 - ROUND(p * 100) (APPROX_QUANTILES always sorts ascending).
    if isinstance(fraction_node, exp.Literal) and not fraction_node.is_string:
        try:
            idx = int(
                (Decimal(str(fraction_node.this)) * 100).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            offset = str(100 - idx if is_desc else idx)
        except (TypeError, ValueError, ArithmeticError):
            offset = _bq_round_offset_expr(self.sql(fraction_node), is_desc)
    else:
        offset = _bq_round_offset_expr(self.sql(fraction_node), is_desc)

    return f"APPROX_QUANTILES({col_sql}, 100)[OFFSET({offset})]"


def _bq_round_offset_expr(fraction_sql: str, is_desc: bool) -> str:
    """Runtime OFFSET expression for a non-literal percentile fraction:
    ``ROUND(p * 100)`` ascending, ``100 - ROUND(p * 100)`` descending."""
    rounded = f"CAST(ROUND({fraction_sql} * 100) AS INT64)"
    return f"100 - {rounded}" if is_desc else rounded


def register_bigquery_patches() -> None:
    """Register all BigQuery dialect patches. Safe to call multiple times."""
    if exp.SplitPart not in _BigQueryDialect.Generator.TRANSFORMS:
        _BigQueryDialect.Generator.TRANSFORMS[exp.SplitPart] = _bq_splitpart_sql
    if exp.WithinGroup not in _BigQueryDialect.Generator.TRANSFORMS:
        _BigQueryDialect.Generator.TRANSFORMS[exp.WithinGroup] = _bq_within_group_sql
