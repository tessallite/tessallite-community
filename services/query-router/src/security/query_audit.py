"""Post-rewrite / post-execute security guardrails.

Two hard-fail audits that sit in ``_handle_execute`` to catch silent
filter drops and column-level security leaks before results leave the
query-router.

Layer 1 — ``audit_filters_present``: runs **before** execution.
    Verifies every resolved filter is structurally present in the
    rewritten SQL's WHERE clause.  A missing filter means the rewriter
    silently dropped a predicate — potential data exfiltration.

Layer 2 — ``audit_result_columns``: runs **after** execution.
    Verifies the columns returned by the source database are a subset
    of what the binder / persona gate authorised.  An extra column
    means a hidden-column leak or persona-gate bypass.

Both raise ``SecurityAuditError`` (treated as HTTP 500 — this is an
internal integrity failure, never the user's fault).
"""
from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

from src.ir.logical_query import BoundQuery
from src.parsing.sql_parser import _flatten_top_level_and

logger = logging.getLogger(__name__)


class SecurityAuditError(Exception):
    pass


# -----------------------------------------------------------------------
# Layer 1 — pre-execute filter presence audit
# -----------------------------------------------------------------------

_STRIP_QUOTES_RE = re.compile(r"[`\"\[\]]")


def _normalise(name: str) -> str:
    return _STRIP_QUOTES_RE.sub("", name).lower().strip()


def _sql_contains_identifier(sql_lower: str, name: str) -> bool:
    """Check if *name* appears as a word-bounded identifier in *sql_lower*."""
    pattern = re.compile(
        r"(?:^|[^a-z0-9_])" + re.escape(name) + r"(?:$|[^a-z0-9_])"
    )
    return pattern.search(sql_lower) is not None


def _parse_sql(sql: str, dialect: str | None = None) -> exp.Expression | None:
    normalised = sql.replace("`", '"')
    try:
        return sqlglot.parse_one(
            normalised,
            read=dialect or "postgres",
            error_level=sqlglot.ErrorLevel.WARN,
        )
    except Exception:
        logger.warning("Security audit: could not parse SQL")
        return None


# ---------------------------------------------------------------------------
# Bug-1045 — dimension-ANCHORED AST filter-presence check
#
# The rewriter legitimately replaces a derived dimension's semantic name
# with its physical expansion (e.g. ``business_date_month`` becomes
# ``EXTRACT(MONTH FROM "payment_transaction"."business_date")``).  When the
# dimension is also projected, the ``AS "business_date_month"`` alias keeps
# the identifier check satisfied — but FILTER-ONLY shapes (exactly what
# Excel multi-member keep-only and timeline slicers produce) carry no alias,
# so presence must be proven structurally from the AST.
#
# Round-3 hardening (B10 deep review round 2, HIGH finding): the AST check
# is ANCHORED to the filter's dimension.  A predicate counts as rendering a
# filter only when ALL of the following hold:
#
#   1. Its left-hand side resolves to the filter's dimension — the semantic
#      name, the dimension's physical column, or its derivation expression
#      (calc expression / user-defined attribute), canonicalised via sqlglot
#      (case, quoting, table qualifiers, redundant parens and CAST wrappers
#      are normalised away).  A predicate on a DIFFERENT column or
#      expression that happens to carry the same values must NOT count.
#   2. Its operator class matches the filter's operator (with polarity:
#      ``NOT IN`` never satisfies an ``in`` filter and vice versa).
#   3. Its value literals are EXACTLY the filter's values (a superset
#      IN-list is a different — leakier — predicate, so it does not count).
#   4. It sits in a scope that constrains the final result set (the outer
#      WHERE/HAVING chain through FROM subqueries / CTEs / INNER joins).
#      Predicate-side subqueries (EXISTS, IN (SELECT …)) and LEFT-joined
#      sources do not constrain outer rows, so they never count.
#      Round-4 (B10 deep review round 3): within a constraining clause the
#      predicate must additionally be a TOP-LEVEL AND-CONJUNCT — an
#      anchored predicate under an OR-disjunct or inside a CASE condition
#      does not enforce the filter and never counts.  And the identifier
#      short-circuit applies only to value-less (IS [NOT] NULL) filters:
#      a projection alias proves reference, not filtering, so filters
#      that carry values always require the anchored AST proof.
#
# Anchors are resolved by ``resolve_filter_anchors`` (async, model metadata
# lookup) at the routes layer and passed into ``audit_filters_present``.
# Without an anchor a derived-dimension predicate cannot be proven and the
# audit blocks — fail-closed is the prime directive.
#
# Round-3 also REMOVES the legacy ``str(f.value) in sql`` substring
# fallback: every shape it could legitimately rescue is either caught by
# the identifier check (name/alias survives) or provable via the anchored
# AST check; any remaining rescue would by construction be dimension-blind
# — exactly the wrong-pass class the round-2 review demonstrated.
# ---------------------------------------------------------------------------

# LogicalFilter.operator → sqlglot predicate node classes that can
# legitimately render it.  ``in``/``not_in`` accept an equality node too
# (a one-element list may be rendered as ``=`` / ``!=``), and vice versa.
_OPERATOR_PREDICATE_NODES: dict[str, tuple[type[exp.Expression], ...]] = {
    "eq": (exp.EQ, exp.In),
    "neq": (exp.NEQ, exp.In),
    "gt": (exp.GT,),
    "gte": (exp.GTE,),
    "lt": (exp.LT,),
    "lte": (exp.LTE,),
    "in": (exp.In, exp.EQ),
    "not_in": (exp.In, exp.NEQ),
    "between": (exp.Between,),
    "like": (exp.Like, exp.ILike),
    # Bug-5326: a ``not_like`` filter (notContains / canonical not_like) renders
    # as ``NOT LIKE`` / ``NOT ILIKE``.  sqlglot encodes that EITHER as a single
    # ``exp.Like(negate=True)`` (sqlglot 30.8.x) OR as ``exp.Not(exp.Like)``
    # (sqlglot 30.4.x); both surface as ``exp.Like``/``exp.ILike`` predicate
    # nodes, with the negation distinguished by ``_negation_parity`` (which now
    # reads ``Like.negate`` as well as enclosing ``Not`` wrappers).
    "not_like": (exp.Like, exp.ILike),
}


def _like_node_negate(node: exp.Expression) -> bool:
    """True when *node* is a ``Like``/``ILike`` carrying ``negate=True``.

    Bug-5326: sqlglot 30.8.x collapses ``col NOT LIKE '%x%'`` into a single
    ``exp.Like`` with a ``negate`` attribute instead of wrapping a positive
    ``Like`` in ``exp.Not``.  Mirrors ``sql_parser._like_is_negated``: the
    attribute is read defensively so versions that omit it (and emit the
    ``Not(Like)`` shape, counted by the parent walk) read as not-negated here.
    """
    if not isinstance(node, (exp.Like, exp.ILike)):
        return False
    return bool(node.args.get("negate") or getattr(node, "negate", False))


def _norm_literal_text(text: str) -> str:
    """Normalise a literal's text for comparison.

    Strips one level of quoting and collapses numeric spellings so the
    filter value ``'4'`` (string) matches the rendered literal ``4``
    (the rewriter coerces string values against numeric column types,
    e.g. for EXTRACT() derivations).
    """
    s = str(text).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    try:
        num = float(s)
    except (TypeError, ValueError):
        return s.lower()
    if num.is_integer():
        return str(int(num))
    return repr(num)


def _strip_value_wrappers(node: exp.Expression) -> exp.Expression:
    """Unwrap parens and CAST coercions around a value node.

    The rewriter wraps value literals in CAST(... AS DATE) / TIMESTAMP '…'
    coercions; those wrappers are irrelevant to value identity.
    """
    while isinstance(node, (exp.Paren, exp.Cast)):
        node = node.this
    return node


def _canonical_expr_key(node: exp.Expression) -> str | None:
    """Canonical comparison key for an expression (LHS anchoring).

    Normalises: redundant parens, CAST wrappers, table/schema qualifiers
    on columns, identifier quoting, whitespace and case.  sqlglot already
    canonicalises cross-dialect function shapes on parse (EXTRACT /
    DATE_PART, DATE_TRUNC / TIMESTAMP_TRUNC), so rendering the stripped
    tree back to PostgreSQL text yields a dialect-stable key.

    Returns ``None`` when the expression cannot be canonicalised — the
    caller treats that as "no match" (fail-closed).
    """
    def _strip(n: exp.Expression) -> exp.Expression:
        if isinstance(n, (exp.Paren, exp.Cast)):
            return n.this
        if isinstance(n, exp.Column):
            return exp.column(n.name)
        return n

    try:
        # sqlglot's ``transform`` does not recurse into REPLACED nodes, so a
        # single pass leaves wrappers nested under a replacement untouched.
        # Re-apply until the rendering reaches a fixpoint (wrapper depth is
        # bounded, so this converges in a few passes).
        tree = node.copy()
        prev: str | None = None
        sql = tree.sql(dialect="postgres")
        while sql != prev:
            prev = sql
            tree = tree.transform(_strip)
            sql = tree.sql(dialect="postgres")
    except Exception:
        return None
    key = re.sub(r"\s+", " ", sql).strip().lower()
    return key or None


def build_anchor_keys(
    *,
    column_names: tuple[str, ...] | list[str] = (),
    expressions: tuple[str, ...] | list[str] = (),
) -> set[str]:
    """Build the canonical anchor-key set for one dimension.

    ``column_names`` are bare identifiers (semantic name, physical column);
    ``expressions`` are SQL derivation expressions (calc expression / UDA).
    Unparseable expressions contribute nothing (fail-closed).
    """
    keys: set[str] = set()
    for c in column_names:
        if c:
            keys.add(_normalise(str(c)))
    for e in expressions:
        if not e:
            continue
        try:
            tree = sqlglot.parse_one(
                str(e).replace("`", '"'),
                read="postgres",
                error_level=sqlglot.ErrorLevel.RAISE,
            )
        except Exception:
            continue
        key = _canonical_expr_key(tree)
        if key:
            keys.add(key)
    return keys


def _lhs_matches_anchor(node: exp.Expression | None, anchor_keys: set[str]) -> bool:
    if node is None or not anchor_keys:
        return False
    key = _canonical_expr_key(node)
    return key is not None and key in anchor_keys


def _node_value_norm(node: exp.Expression | None) -> str | None:
    """Single canonical comparison token for one value-side node."""
    if node is None:
        return None
    n = _strip_value_wrappers(node)
    if isinstance(n, exp.Null):
        return "null"
    if isinstance(n, exp.Boolean):
        return "true" if n.this else "false"
    if isinstance(n, exp.Neg):
        inner = _strip_value_wrappers(n.this)
        if isinstance(inner, exp.Literal):
            return _norm_literal_text(f"-{inner.name}")
    if isinstance(n, exp.Literal):
        return _norm_literal_text(n.name)
    # Non-literal value expression (CURRENT_DATE, arithmetic, …): compare
    # by canonical expression key so parser RawSQL fragments still match.
    return _canonical_expr_key(n)


def _element_norm(value: Any) -> str | None:
    """Canonical comparison token for one filter-value element.

    Plain scalars normalise like literals.  SQL-fragment values (the
    parser's ``RawSQL`` str-subclass marker, e.g.
    ``CAST('2024-01-01' AS DATE)`` or ``CURRENT_DATE``) are parsed so the
    token matches what ``_node_value_norm`` derives from the rendered AST.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    from src.parsing.sql_parser import RawSQL
    text = str(value)
    if isinstance(value, RawSQL) or not isinstance(value, (str, int, float)):
        try:
            frag = sqlglot.parse_one(
                text.replace("`", '"'),
                read="postgres",
                error_level=sqlglot.ErrorLevel.RAISE,
            )
            return _node_value_norm(frag)
        except Exception:
            return _norm_literal_text(text)
    return _norm_literal_text(text)


def _negation_parity(node: exp.Expression) -> bool:
    """True when *node* sits under an ODD number of enclosing NOTs.

    Round-4 (Finding 2): the parent walk must look THROUGH transparent
    ``Paren`` wrappers — ``NOT ((x IN (…)))`` parses as
    ``Not(Paren(Paren(In)))``, so inspecting only the immediate parent
    reads a negated predicate as positive.  NOT wrappers are counted to
    a parity so double negation reads positive again.

    Bug-5326: a ``Like``/``ILike`` node may itself carry ``negate=True``
    (sqlglot 30.8.x ``col NOT LIKE '%x%'`` → ``Like(negate=True)``); that
    in-node negation is folded into the parity so a ``NOT LIKE`` reads as
    negated under BOTH the ``Like(negate=True)`` and the legacy
    ``Not(Like)`` shapes.
    """
    negated = _like_node_negate(node)
    parent = node.parent
    while isinstance(parent, (exp.Paren, exp.Not)):
        if isinstance(parent, exp.Not):
            negated = not negated
        parent = parent.parent
    return negated


def _polarity_ok(op: str, node: exp.Expression) -> bool:
    """Reject polarity mismatches: a predicate under NOT inverts semantics
    (sqlglot parses ``x NOT IN (…)`` as ``Not(In(…))``).

    Bug-5326: ``_negation_parity`` now also folds in a ``Like.negate``
    attribute, so for a ``Like``/``ILike`` predicate the negated parity
    distinguishes ``NOT LIKE`` from ``LIKE``: a ``not_like`` filter matches
    ONLY a negated Like, and a positive ``like`` filter matches ONLY a
    non-negated Like (a positive ``like`` is NEVER satisfied by a ``NOT
    LIKE`` and vice-versa)."""
    negated = _negation_parity(node)
    if isinstance(node, exp.In):
        return negated == (op in ("not_in", "neq"))
    if isinstance(node, (exp.Like, exp.ILike)):
        return negated == (op == "not_like")
    return not negated


def _anchored_value_norms(
    op: str, node: exp.Expression, anchor_keys: set[str]
) -> list[str | None] | None:
    """Value tokens of *node* when its LHS is anchored to the filter's
    dimension; ``None`` when the predicate is not anchored (or not provable,
    e.g. IN (subquery))."""
    if isinstance(node, exp.In):
        if node.args.get("query") is not None:
            return None  # IN (SELECT …) — membership not provable
        if not _lhs_matches_anchor(node.this, anchor_keys):
            return None
        return [_node_value_norm(v) for v in node.expressions]
    if isinstance(node, exp.Between):
        if not _lhs_matches_anchor(node.this, anchor_keys):
            return None
        return [
            _node_value_norm(node.args.get("low")),
            _node_value_norm(node.args.get("high")),
        ]
    if isinstance(node, (exp.Like, exp.ILike)):
        if not _lhs_matches_anchor(node.this, anchor_keys):
            return None
        return [_node_value_norm(node.args.get("expression"))]
    # Binary comparisons.  Equality is symmetric; ordered comparisons
    # require the dimension on the LEFT (the rewriter always renders the
    # column on the left — a mirrored shape is not provably the filter).
    left, right = node.this, node.args.get("expression")
    if _lhs_matches_anchor(left, anchor_keys):
        return [_node_value_norm(right)]
    if isinstance(node, (exp.EQ, exp.NEQ)) and _lhs_matches_anchor(right, anchor_keys):
        return [_node_value_norm(left)]
    return None


def _collect_predicate_scopes(ast: exp.Expression) -> list[exp.Expression]:
    """WHERE / HAVING subtrees that CONSTRAIN the final result set.

    Walks the outer query chain: the root SELECT (or each branch of a set
    operation), its FROM source and INNER-joined sources, recursing through
    subqueries and CTE definitions. Row security itself no longer produces a
    wrapper for this to find — F-007-01 replaced the outer
    ``SELECT * FROM (…) __rls WHERE sec = …`` with per-scan WHERE injection, so
    the security predicate now lives in the same SELECT as each scan. The
    recursion is still required: a CALLER-authored subquery or CTE is exactly
    where an injected predicate lands, and a set-operation branch is a separate
    constraining scope.

    Deliberately EXCLUDED (their predicates do not restrict outer rows):
    predicate-side subqueries (EXISTS (…), IN (SELECT …)) and LEFT / RIGHT /
    FULL / CROSS join sources.
    """
    scopes: list[exp.Expression] = []
    seen: set[int] = set()

    def _walk_select(select: exp.Select, cte_map: dict[str, exp.Expression]) -> None:
        if id(select) in seen:
            return
        seen.add(id(select))
        local_ctes = dict(cte_map)
        # sqlglot arg-key rename across versions ("with" → "with_").
        with_ = select.args.get("with_") or select.args.get("with")
        if with_ is not None:
            for cte in with_.expressions:
                alias = getattr(cte, "alias", None)
                if alias:
                    local_ctes[str(alias).lower()] = cte.this
        for key in ("where", "having"):
            clause = select.args.get(key)
            if clause is not None:
                scopes.append(clause)
        sources: list[exp.Expression] = []
        # sqlglot renamed the FROM arg key ("from" → "from_") across
        # versions; accept either.
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is not None:
            sources.append(from_.this)
        for join in select.args.get("joins") or []:
            side = str(join.args.get("side") or "").upper()
            kind = str(join.args.get("kind") or "").upper()
            if side in ("LEFT", "RIGHT", "FULL") or kind == "CROSS":
                continue
            sources.append(join.this)
        for src in sources:
            _walk_source(src, local_ctes)

    def _walk_source(node: exp.Expression, cte_map: dict[str, exp.Expression]) -> None:
        if isinstance(node, exp.Subquery):
            _walk_source(node.this, cte_map)
        elif isinstance(node, exp.Select):
            _walk_select(node, cte_map)
        elif isinstance(node, (exp.Union, exp.Except, exp.Intersect)):
            _walk_source(node.this, cte_map)
            _walk_source(node.args.get("expression"), cte_map)
        elif isinstance(node, exp.Table):
            target = cte_map.get(node.name.lower())
            if target is not None:
                _walk_source(target, cte_map)

    _walk_source(ast, {})
    return scopes


def _scope_predicate_nodes(
    scope: exp.Expression,
    classes: tuple[type[exp.Expression], ...] | type[exp.Expression],
) -> list[exp.Expression]:
    """Predicate nodes of *classes* that are TOP-LEVEL AND-conjuncts of
    this scope's clause.

    Round-4 (Finding 1): a clause-level walk wrongly counted predicates
    in NON-CONSTRAINING positions — under an OR-disjunct
    (``… WHERE month IN (4,5) OR country = 'ZZ'``) or inside a CASE
    condition (``… WHERE flag = CASE WHEN month IN (4,5) THEN 1 END``)
    — neither of which enforces the filter on the result rows.  Only a
    top-level AND-conjunct of the WHERE/HAVING body constrains every
    row, so only those nodes may prove a filter present.

    Each conjunct is unwrapped through transparent ``Paren`` / ``Not``
    wrappers so the DIRECT negation of a conjunct is still surfaced —
    polarity is then judged by ``_polarity_ok`` / ``_negation_parity``
    (a wrong polarity never matches).  This also subsumes the previous
    nested-SELECT prune: predicates inside EXISTS (…) / IN (SELECT …) /
    scalar subqueries are not conjunct heads and are never returned.
    """
    nodes: list[exp.Expression] = []
    for conjunct in _flatten_top_level_and(scope.this):
        node = conjunct
        while isinstance(node, (exp.Paren, exp.Not)):
            node = node.this
        if isinstance(node, classes):
            nodes.append(node)
    return nodes


def _filter_predicate_in_ast(
    f: Any, scopes: list[exp.Expression], anchor_keys: set[str]
) -> bool:
    """True when some constraining WHERE/HAVING predicate node provably
    renders this filter: LHS anchored to the filter's dimension, operator
    class + polarity compatible, and value tokens EXACTLY the filter's.

    Returns False whenever presence cannot be PROVEN — the caller then
    blocks (fail-closed)."""
    op = (getattr(f, "operator", "") or "").lower()
    if not anchor_keys:
        return False

    if op in ("is_null", "is_not_null"):
        # No literal payload — match an anchored IS [NOT] NULL node of the
        # same polarity (sqlglot parses ``x IS NOT NULL`` as Not(Is)).
        for scope in scopes:
            for node in _scope_predicate_nodes(scope, exp.Is):
                if not isinstance(node.args.get("expression"), exp.Null):
                    continue
                negated = _negation_parity(node)
                if (op == "is_not_null") == negated and _lhs_matches_anchor(
                    node.this, anchor_keys
                ):
                    return True
        return False

    node_classes = _OPERATOR_PREDICATE_NODES.get(op)
    if node_classes is None:
        return False

    value = getattr(f, "value", None)
    elements = list(value) if isinstance(value, (list, tuple, set)) else [value]
    if not elements:
        return False

    if op == "between":
        if len(elements) != 2:
            return False
        required_low = _element_norm(elements[0])
        required_high = _element_norm(elements[1])
        for scope in scopes:
            for node in _scope_predicate_nodes(scope, exp.Between):
                if not _polarity_ok(op, node):
                    continue
                norms = _anchored_value_norms(op, node, anchor_keys)
                if norms is None:
                    continue
                if (
                    norms[0] is not None
                    and norms[1] is not None
                    and norms[0] == required_low
                    and norms[1] == required_high
                ):
                    return True
        return False

    required = {_element_norm(v) for v in elements}
    if None in required:
        return False

    for scope in scopes:
        for cls in node_classes:
            for node in _scope_predicate_nodes(scope, cls):
                if not _polarity_ok(op, node):
                    continue
                norms = _anchored_value_norms(op, node, anchor_keys)
                if norms is None or any(n is None for n in norms):
                    continue
                # EXACT value-set equality: a superset IN-list (or a
                # different scalar) is a different predicate, not this
                # filter.  Duplicates collapse on both sides.
                if set(norms) == required:
                    return True
    return False


def _dims_by_anchor_key(bound_query: Any) -> dict[str, Any]:
    """Resolved Dimension objects keyed by normalised semantic name."""
    dims: dict[str, Any] = {}
    for k, v in (getattr(bound_query, "resolved_dimensions_by_name", {}) or {}).items():
        dims[_normalise(str(k))] = v
    for d in getattr(bound_query, "resolved_dimensions", []) or []:
        name = getattr(d, "name", None)
        if name:
            dims.setdefault(_normalise(str(name)), d)
    return dims


def _sync_filter_anchors(bound_query: Any) -> dict[str, set[str]]:
    """Anchor keys derivable WITHOUT a DB session: the semantic name plus
    the dimension's calc expression (an ORM column attribute).  Physical
    column names and UDA expressions need the async resolver."""
    anchors: dict[str, set[str]] = {}
    filters = getattr(bound_query, "resolved_filters", None) or []
    if not filters:
        return anchors
    dims = _dims_by_anchor_key(bound_query)
    for f in filters:
        key = _normalise(str(getattr(f, "dimension_name", "") or ""))
        if not key or key in anchors:
            continue
        keys = {key}
        dim = dims.get(key)
        if dim is not None:
            calc = getattr(dim, "calc_expression", None)
            if calc:
                keys |= build_anchor_keys(expressions=[calc])
        anchors[key] = keys
    return anchors


async def resolve_filter_anchors(bound_query: Any, db: Any) -> dict[str, set[str]]:
    """Resolve each resolved filter's dimension to its anchor-key set.

    Anchors are what `_filter_predicate_in_ast` matches predicate LHS
    expressions against: the semantic name, the dimension's physical
    column name (ModelColumn), its calc expression, and its user-defined
    attribute expression — the same physical mappings the rewriter renders.

    Resolution failures degrade to FEWER anchor keys, never more: an
    unresolvable dimension can only cause a block (fail-closed), never a
    wrong pass.
    """
    anchors = _sync_filter_anchors(bound_query)
    if not anchors or db is None:
        return anchors
    try:
        from sqlalchemy import func as sa_func, select as sa_select
        from shared.db.models import Dimension, ModelColumn, UserDefinedAttribute

        dims = _dims_by_anchor_key(bound_query)
        model_id = getattr(getattr(bound_query, "model", None), "id", None)
        missing = [k for k in anchors if k not in dims]
        if missing and model_id is not None:
            result = await db.execute(
                sa_select(Dimension).where(
                    Dimension.model_id == model_id,
                    sa_func.lower(Dimension.name).in_(missing),
                )
            )
            for dim in result.scalars().all():
                dims.setdefault(_normalise(dim.name), dim)

        anchor_dims = {k: dims[k] for k in anchors if k in dims}
        col_ids = {
            getattr(d, "source_column_id", None) for d in anchor_dims.values()
        } - {None}
        uda_ids = {
            getattr(d, "user_defined_attribute_id", None) for d in anchor_dims.values()
        } - {None}

        cols_by_id: dict[Any, Any] = {}
        if col_ids:
            result = await db.execute(
                sa_select(ModelColumn).where(ModelColumn.id.in_(col_ids))
            )
            cols_by_id = {c.id: c for c in result.scalars().all()}
        udas_by_id: dict[Any, Any] = {}
        if uda_ids:
            result = await db.execute(
                sa_select(UserDefinedAttribute).where(
                    UserDefinedAttribute.id.in_(uda_ids)
                )
            )
            udas_by_id = {u.id: u for u in result.scalars().all()}

        for key, dim in anchor_dims.items():
            mc = cols_by_id.get(getattr(dim, "source_column_id", None))
            if mc is not None:
                anchors[key] |= build_anchor_keys(column_names=[mc.column_name])
            calc = getattr(dim, "calc_expression", None)
            if calc:
                anchors[key] |= build_anchor_keys(expressions=[calc])
            uda = udas_by_id.get(getattr(dim, "user_defined_attribute_id", None))
            if uda is not None and getattr(uda, "expression", None):
                anchors[key] |= build_anchor_keys(expressions=[uda.expression])
    except Exception:
        logger.warning(
            "Security audit: filter anchor resolution failed; anchors limited "
            "to semantic names + calc expressions (fail-closed).",
            exc_info=True,
        )
    return anchors


def _has_where_clause(sql: str, dialect: str | None = None) -> bool:
    """Check whether the SQL contains any WHERE clause, using AST."""
    ast = _parse_sql(sql, dialect)
    if ast is None:
        return False
    return any(
        select.args.get("where") is not None
        for select in ast.find_all(exp.Select)
    )


def _count_where_conjuncts(sql: str, dialect: str | None = None) -> int:
    """Count top-level AND'd conjuncts across all WHERE clauses in the SQL.

    Uses sqlglot AST parsing and the parser's ``_flatten_top_level_and``
    so the guardrail counts predicates with the same logic the query
    analyser uses — no regex approximations.
    """
    ast = _parse_sql(sql, dialect)
    if ast is None:
        return 0

    total = 0
    for select in ast.find_all(exp.Select):
        where = select.args.get("where")
        if where:
            total += len(_flatten_top_level_and(where.this))
    return total


def audit_filters_present(
    bound_query: BoundQuery,
    rewritten_sql: str,
    route_type: str,
    filter_anchors: dict[str, set[str]] | None = None,
) -> None:
    """Verify every resolved filter appears in the rewritten SQL.

    ``filter_anchors`` maps each filter's normalised dimension name to the
    canonical anchor-key set produced by ``resolve_filter_anchors`` /
    ``build_anchor_keys``.  Callers with a DB session (the routes layer)
    MUST resolve and pass anchors — without them, filters on derived
    dimensions whose identifier the rewrite replaced cannot be proven
    present and the audit blocks (fail-closed).  When ``None``, a reduced
    anchor set is built synchronously from the bound query (semantic name
    + calc expression only).

    Skips the audit for passthrough / complex-SQL queries where the
    rewriter preserves the raw WHERE verbatim — filter extraction is
    best-effort for those shapes and false positives would block valid
    queries.
    """
    lq = getattr(bound_query, "logical_query", None)
    if lq is None:
        return

    if getattr(lq, "has_complex_sql", False):
        return
    if getattr(lq, "has_passthrough_expressions", False):
        return
    if getattr(bound_query, "has_passthrough_expressions", False):
        return

    filters = getattr(bound_query, "resolved_filters", None)
    if not filters:
        if getattr(lq, "has_unresolvable_where", False):
            raw_sql = getattr(lq, "raw_query", "")
            input_dialect = getattr(lq, "input_dialect", "postgres")
            orig_count = _count_where_conjuncts(raw_sql, dialect=input_dialect)
            rewrite_count = _count_where_conjuncts(rewritten_sql)
            if orig_count > 0 and rewrite_count < orig_count:
                raise SecurityAuditError(
                    f"SECURITY AUDIT FAILURE: original SQL has {orig_count} "
                    f"WHERE condition(s) but rewritten SQL has only "
                    f"{rewrite_count}. Filter(s) silently dropped — query "
                    f"blocked to prevent data leakage."
                )
        return

    if not _has_where_clause(rewritten_sql):
        missing = [f.dimension_name for f in filters]
        raise SecurityAuditError(
            f"SECURITY AUDIT FAILURE: rewritten SQL has no WHERE clause but "
            f"{len(missing)} filter(s) were expected: {missing}"
        )

    sql_lower = rewritten_sql.lower()
    ast = _parse_sql(rewritten_sql)
    scopes = _collect_predicate_scopes(ast) if ast is not None else []
    anchors = (
        filter_anchors if filter_anchors is not None
        else _sync_filter_anchors(bound_query)
    )

    missing: list[str] = []
    for f in filters:
        name_lower = _normalise(f.dimension_name)
        op = (getattr(f, "operator", "") or "").lower()
        # 1. Identifier check: the semantic name survives in the SQL —
        #    either as a physical column of the same name, a projection
        #    alias, or an aggregate-table grain column.
        #    Round-4 (Finding 3): identifier presence proves only that the
        #    dimension is REFERENCED (e.g. an ``AS "dim"`` projection
        #    alias), not that its predicate survived — projection is not
        #    filtering.  So the short-circuit applies only to value-less
        #    filters (IS [NOT] NULL); a filter that carries VALUES must
        #    additionally be proven by the anchored AST check below.
        if op in ("is_null", "is_not_null") and _sql_contains_identifier(
            sql_lower, name_lower
        ):
            continue
        # 2. Bug-1045 (round-3 anchored): AST predicate check — the
        #    predicate's LHS must resolve to THIS dimension's semantic
        #    name / physical column / derivation expression, with operator
        #    class, polarity and EXACT values, in a top-level AND-conjunct
        #    of a constraining WHERE/HAVING clause.  There is deliberately
        #    NO further fallback: the legacy str(value)-substring check was
        #    dimension-blind and could rescue a genuinely dropped filter
        #    (round-2 HIGH finding).
        if _filter_predicate_in_ast(f, scopes, anchors.get(name_lower) or {name_lower}):
            continue
        missing.append(f.dimension_name)

    if missing:
        raise SecurityAuditError(
            f"SECURITY AUDIT FAILURE: {len(missing)} filter(s) missing from "
            f"rewritten SQL: {missing}. Filters may have been silently "
            f"dropped — query blocked to prevent data leakage."
        )

    if getattr(lq, "has_unresolvable_where", False):
        raw_sql = getattr(lq, "raw_query", "")
        input_dialect = getattr(lq, "input_dialect", "postgres")
        orig_count = _count_where_conjuncts(raw_sql, dialect=input_dialect)
        rewrite_count = _count_where_conjuncts(rewritten_sql)
        if orig_count > 0 and rewrite_count < orig_count:
            raise SecurityAuditError(
                f"SECURITY AUDIT FAILURE: original SQL has {orig_count} "
                f"WHERE condition(s) but rewritten SQL has only "
                f"{rewrite_count}. Filter(s) silently dropped — query "
                f"blocked to prevent data leakage."
            )


# -----------------------------------------------------------------------
# Layer 2 — post-execute column-level security audit
# -----------------------------------------------------------------------

_SYNTHETIC_COLUMNS = frozenset({
    "?column?", "f0_", "f1_", "f2_", "f3_", "_col0", "_c0",
    "count", "cnt", "__row_count",
})

_AGG_STAT_RE = re.compile(r"^(.+)__(\w+)$")
# Matches a DB-generated deduplication suffix: <base_name>_<integer>
# e.g. "documents_submitted_1" when the SELECT list has duplicate aliases.
_DEDUP_SUFFIX_RE = re.compile(r"^(.+)_(\d+)$")


def audit_result_columns(
    bound_query: BoundQuery,
    result_columns: list[str],
    persona: Any | None = None,
) -> None:
    """Verify every column in the result set was authorised by the binder.

    Skips the audit for passthrough / complex-SQL queries where the
    binder doesn't resolve individual columns.
    """
    lq = getattr(bound_query, "logical_query", None)
    if lq is None:
        return

    # F-003-02: complex SQL no longer returns early UNCONDITIONALLY. The binder
    # now validates every physical column reference against the deployed model
    # (fail-closed) and publishes the validated model physical set on
    # ``allowed_physical_columns``, so the result audit stays ACTIVE as a
    # defence-in-depth guard over the returned columns. It is skipped only for an
    # outer ``SELECT *`` complex query, whose result column names come straight
    # from the (already binder-validated) inner scan and can legitimately be
    # renames the outer projection never named — auditing those would false-fail.
    _is_complex = getattr(lq, "has_complex_sql", False)
    if _is_complex:
        _has_validated_physical = bool(
            getattr(bound_query, "allowed_physical_columns", None)
        )
        if getattr(lq, "select_star", False) or not _has_validated_physical:
            return
    else:
        if getattr(lq, "has_passthrough_expressions", False):
            return
        if getattr(bound_query, "has_passthrough_expressions", False):
            return

    if not result_columns:
        return

    # Build allowed set from what the binder authorised
    allowed: set[str] = set()

    for d in getattr(bound_query, "resolved_dimensions", []):
        allowed.add(d.name.lower())

    for m in getattr(bound_query, "resolved_measures", []):
        allowed.add(m.name.lower())
        # Aggregate physical column pattern: revenue__sum, count__count
        default_agg = getattr(m, "default_agg", "sum") or "sum"
        allowed.add(f"{m.name}__{default_agg}".lower())
        # All stat types that could appear for a multi-stat aggregate
        for st in ("sum", "count", "min", "max", "avg"):
            allowed.add(f"{m.name}__{st}".lower())

    # User aliases from select_expressions
    for se in getattr(lq, "select_expressions", []):
        if se.alias:
            allowed.add(se.alias.lower())
        # Literal expressions produce columns like '1', 'TRUE'
        if se.classification == "literal" and se.raw_text:
            allowed.add(se.raw_text.strip().lower())
        # Bug-AGG-001: aggregate function names used as disambiguated
        # aliases when the same measure appears with multiple agg
        # functions (e.g. SELECT MIN(x), MAX(x) → columns "x", "max").
        if se.agg_function:
            allowed.add(se.agg_function.lower())
        # Bug-1066: a composable / scalar-wrapped aggregate expression
        # (SUM(a)/SUM(b), CASE/COALESCE over aggregates, ROUND(AVG(x))) is a
        # COMPUTED scalar over columns the binder already authorised via the
        # expression's component measures — it exposes no new column. When the
        # user gives no alias, the source DB names the output column after the
        # top function (``case``, ``coalesce``, ``?column?`` for arithmetic).
        # Authorise that DB-default output name so the result-column audit does
        # not mistake a legitimate computed column for a CLS bypass.
        if getattr(se, "composable", False) and se.raw_text and not se.alias:
            try:
                _se_ast = sqlglot.parse_one(se.raw_text, read="postgres")
                _top = _se_ast.this if isinstance(_se_ast, exp.Alias) else _se_ast
                # PostgreSQL names an unaliased function/CASE column after the
                # function/construct (``coalesce``, ``case``, ``round`` …);
                # bare arithmetic gets ``?column?`` (already synthetic).
                _out = (_se_ast.output_name or "").lower()
                if _out:
                    allowed.add(_out)
                if isinstance(_top, exp.Case):
                    allowed.add("case")
                elif isinstance(_top, exp.Func):
                    _key = (getattr(_top, "sql_name", lambda: "")() or _top.key or "").lower()
                    if _key:
                        allowed.add(_key)
            except Exception:
                pass

    # Physical column names for SELECT * (source returns physical names)
    physical = getattr(bound_query, "allowed_physical_columns", None)
    if physical:
        allowed.update(physical)

    # Complex SQL: names the QUERY ITSELF defines in its outermost projection —
    # a SELECT-list alias, or a CTE / derived-table output name projected onward
    # (``SELECT total_amount FROM (SELECT SUM(x) AS total_amount ...) s``). They
    # are not model columns, so they are absent from ``allowed_physical_columns``
    # and the parser records no alias for the bare-reference form; the audit used
    # to read them as unauthorised columns and block the whole result. The binder
    # publishes this set only after proving every PHYSICAL read in the query is a
    # modelled column, so each name here is already-authorised data under a name
    # the query chose.
    if _is_complex:
        allowed.update(
            getattr(bound_query, "complex_projection_names", None) or set()
        )

    # Synthetic / well-known columns
    allowed.update(_SYNTHETIC_COLUMNS)

    # Check each result column
    violations: list[str] = []
    for col in result_columns:
        col_norm = _normalise(col)
        if col_norm in allowed:
            continue

        # Check aggregate pattern: <measure>__<stat>
        m = _AGG_STAT_RE.match(col_norm)
        if m and m.group(1) in allowed:
            continue

        # DB-generated deduplication suffix: the source DB auto-renames a
        # duplicate alias by appending _N (e.g. documents_submitted_1 when
        # documents_submitted appears twice in the SELECT).  Strip the suffix
        # and re-check — if the base name was authorised, so is the renamed column.
        dedup_m = _DEDUP_SUFFIX_RE.match(col_norm)
        if dedup_m and dedup_m.group(1) in allowed:
            continue

        # Numeric-only column names from expression evaluation (BigQuery)
        if col_norm.startswith("f") and col_norm[1:].replace("_", "").isdigit():
            continue

        violations.append(col)

    if violations:
        raise SecurityAuditError(
            f"SECURITY AUDIT FAILURE: result contains {len(violations)} "
            f"unauthorised column(s): {violations}. These columns were not "
            f"in the binder's resolved set — possible column-level security "
            f"bypass. Query result blocked."
        )
