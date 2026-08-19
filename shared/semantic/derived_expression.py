"""Canonicalisation and semantic identity for derived-grain expressions.

Spec: ``docs/architecture/architecture_derived-grain-aggregate-routing.md`` §6.

This is the ONE shared canonicaliser (spec §6, §11.1): the query-router and the
optimizer import it; neither keeps a private normaliser. It turns a SQLGlot
expression node — already selected from a parsed query AST, never regex-extracted
from raw SQL — into a deterministic canonical form and a stable fingerprint.

Phase 1 scope (capture + diagnostics only, no serving):
  - proof-preserving normalisation (spec §6.1 steps 6-8): strip aliases and
    redundant parentheses; normalise quoted-identifier spelling after binding;
    normalise documented function aliases that the registry declares identical;
    keep semantic literals ('month', timezone, substring position, cast type).
  - a deterministic canonical serialisation + fingerprint that folds together
    harmless quote/alias/paren/registered-alias differences but keeps genuinely
    different expressions (DATE_TRUNC('month', ts) vs EXTRACT(month FROM ts))
    distinct (spec I1, I11).
  - the function-semantics registry loader (data, not connector ``if`` branches).

It deliberately does NOT (yet) prove derivation theorems, roll up measures, or
authorise any route. Those land in later phases. Nothing here changes routed SQL.

Non-negotiables honoured:
  - No name heuristic establishes identity (spec I1): the fingerprint is computed
    over the structural typed AST, not over display names.
  - Commutative operands are NOT reordered (spec §6.1 step 7): overflow, float,
    collation, and 3VL make that observable.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

import sqlglot
from sqlglot import exp


# ---------------------------------------------------------------------------
# Function-semantics registry (spec §6.2). Data-driven; no connector branches.
# ---------------------------------------------------------------------------

_REGISTRY_FILENAME = "derived_expression_semantics.json"


@lru_cache(maxsize=1)
def load_semantics_registry() -> dict[str, Any]:
    """Load and cache the function-semantics registry JSON (spec §6.2).

    The registry is DATA: each entry declares a semantic function id, the
    SQLGlot node/function aliases that canonicalise to it, volatility, totality,
    NULL behaviour, collation/timezone dependencies, and (later) derivation
    theorems. Unknown functions are ``SOURCE_ONLY`` by omission.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), _REGISTRY_FILENAME)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def registry_version() -> str:
    """Registry content version tag (spec §12, §17: participates in proof hash)."""
    return str(load_semantics_registry().get("registry_version", "unknown"))


# Canonicaliser algorithm version. Bump when the normalisation pipeline changes
# in a way that could change a fingerprint, so mixed replicas never disagree
# about whether two expressions are identical (spec I12, §17 pitfall 14).
CANONICALIZER_VERSION = "v1"


def _alias_map() -> dict[str, str]:
    """Build ``sqlglot-node-key -> semantic_function_id`` from the registry.

    Keys are lowercase SQLGlot node class names or function names the registry
    lists under ``aliases``. Only aliases the registry explicitly declares as
    identical semantics are folded (spec §6.1 step 6).
    """
    reg = load_semantics_registry()
    out: dict[str, str] = {}
    for fn_id, entry in reg.get("functions", {}).items():
        for alias in entry.get("aliases", []):
            out[str(alias).lower()] = fn_id
    return out


# ---------------------------------------------------------------------------
# Canonical form + fingerprint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalLeaf:
    """One derived-expression leaf column reference, qualifier-preserving.

    Spec §3.1 producer/consumer leaf-order contract. ``qualifier`` is the table
    qualifier the author wrote (``orders`` in ``orders.created_at``), lower-cased,
    or ``None`` for a bare unqualified reference. ``name`` is the column name,
    lower-cased. Equality/order of the ordered leaf tuple this class builds is the
    ONE contract both the build canonicaliser and the query binder resolve against
    — same-named leaves with different qualifiers are DISTINCT, later occurrences
    of the same complete reference are removed, and first-seen AST-walk order is
    preserved. Case-folding matches PostgreSQL unquoted identifier folding and the
    binder's case-folded id map; quoted mixed-case leaves already fingerprint
    distinctly (the fingerprint separates them before lineage is compared).
    """

    qualifier: Optional[str]
    name: str


def enumerate_canonical_leaves(node: exp.Expression) -> list[CanonicalLeaf]:
    """Ordered, qualifier-aware leaf enumeration (spec §3.1).

    THE shared producer/consumer leaf-order contract. Walks the (already
    proof-preserving-normalised) AST in walk order, keeps the FIRST occurrence of
    each complete ``(qualifier, name)`` reference, removes only LATER occurrences
    of that same complete reference, and keeps same-named leaves with different
    qualifiers distinct. Both the build planner/canonicaliser and the query binder
    call this on the SAME normalised node so ``input_columns`` /
    ``input_column_ids`` are identical ordered tuples for identical expressions.
    """
    seen: set[tuple[Optional[str], str]] = set()
    out: list[CanonicalLeaf] = []
    for sub in node.walk():
        n = sub[0] if isinstance(sub, tuple) else sub
        if not isinstance(n, exp.Column):
            continue
        name = (n.name or "")
        if not name:
            continue
        # ``table`` on a sqlglot Column is the qualifier token if present.
        qual = n.table or None
        key = (qual.lower() if qual else None, name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(CanonicalLeaf(qualifier=key[0], name=key[1]))
    return out


@dataclass
class CanonicalExpression:
    """Result of canonicalising one derived-grain expression node.

    ``fingerprint`` is stable across harmless quote/alias/paren/registered-alias
    differences but distinct for genuinely different expressions. It does NOT yet
    fold in model/deployed-version identity or the full semantic profile — the
    binder combines those (spec §6.1 step 9) when it builds the proof-scoped hash
    in later phases. Phase 1 uses ``fingerprint`` for shape telemetry only.

    ``leaves`` is the spec §3.1 ordered, qualifier-aware leaf tuple — the ONE
    producer/consumer contract for grain-key lineage. ``input_columns`` is the
    de-duplicated bare-name view kept for the pre-existing CLS/telemetry callers;
    it now follows the SAME first-seen order as ``leaves`` so the build producer
    and the query binder agree on lineage column order (the fingerprint payload is
    unchanged, so stage-5 identity is unaffected).
    """
    canonical_sql: str
    fingerprint: str
    input_columns: list[str] = field(default_factory=list)
    leaves: list[CanonicalLeaf] = field(default_factory=list)
    semantic_function_ids: list[str] = field(default_factory=list)
    has_unknown_function: bool = False
    canonicalizer_version: str = CANONICALIZER_VERSION
    registry_version: str = field(default_factory=registry_version)


def _strip_proof_preserving(node: exp.Expression) -> exp.Expression:
    """Apply only the spec §6.1 proof-preserving normalisations.

    - remove aliases (``... AS x``): output alias is tracked at the occurrence
      level, not inside the identity;
    - unwrap redundant Paren wrappers;
    - leave operand order, literals, casts, and function choice otherwise intact
      (function-alias folding is applied at fingerprint time via the registry
      alias map, so the canonical SQL still renders the author's function while
      the identity folds the alias).
    """
    node = node.copy()

    # Unwrap a top-level alias wrapper.
    if isinstance(node, exp.Alias):
        node = node.this.copy()

    # Remove redundant parentheses everywhere.
    for paren in list(node.find_all(exp.Paren)):
        inner = paren.this
        if inner is not None:
            paren.replace(inner.copy())

    # Re-unwrap in case the top node itself was a Paren.
    while isinstance(node, exp.Paren):
        node = node.this.copy()

    # Quote-spelling normalisation is deliberately NOT done here. Spec §6.1
    # sequences it as step 6 ("normalise quoted identifier spelling AFTER
    # binding", step 3), because in PostgreSQL an unquoted ``Foo`` folds to
    # ``foo`` while ``"Foo"`` names a genuinely different column — clearing the
    # quoted flag before column binding would conflate two distinct columns into
    # one identity (an I1/I2 violation once these expressions can serve). The
    # binder performs quote normalisation as part of replacing each leaf column
    # with its stable BoundColumnRef against the deployed snapshot; the
    # pre-binding canonical form must preserve the author's quoting so the
    # binding step still sees the correct physical identity.

    return node


def _semantic_key(node: exp.Expression, alias_map: dict[str, str]) -> tuple[list[str], bool]:
    """Collect semantic function ids + unknown-function flag (spec §6.1 step 6).

    Function nodes map through the registry alias map to their semantic id; a
    function absent from the registry sets ``has_unknown_function`` so the caller
    can mark the expression ``SOURCE_ONLY`` downstream. Leaf column enumeration is
    handled separately by :func:`enumerate_canonical_leaves` (spec §3.1), the
    shared ordered producer/consumer lineage contract.
    """
    fn_ids: list[str] = []
    unknown = False
    for sub in node.walk():
        n = sub[0] if isinstance(sub, tuple) else sub
        if isinstance(n, exp.Func):
            key = n.key.lower() if n.key else type(n).__name__.lower()
            sem = alias_map.get(key)
            if sem is None:
                # Also try the sql-name of the function (e.g. DATE_TRUNC).
                name = (n.sql_name() or "").lower() if hasattr(n, "sql_name") else ""
                sem = alias_map.get(name)
            if sem is None:
                unknown = True
                fn_ids.append(f"?{key}")
            else:
                fn_ids.append(sem)
    return fn_ids, unknown


def canonicalise_ast(
    node: exp.Expression,
    *,
    input_dialect: str = "postgres",
) -> CanonicalExpression:
    """Canonicalise one already-parsed expression node (spec §6.1).

    ``node`` MUST come from the query's own parsed AST (spec §18: never re-parse
    GROUP BY from raw SQL). The returned fingerprint hashes the normalised
    structural form together with the canonicaliser + registry versions, so a
    registry or canonicaliser change invalidates stale fingerprints (spec I12).
    """
    alias_map = _alias_map()
    normalised = _strip_proof_preserving(node)
    fn_ids, unknown = _semantic_key(normalised, alias_map)
    # Spec §3.1: the ordered, qualifier-aware leaf tuple is the producer/consumer
    # lineage contract. ``input_columns`` (bare-name CLS/telemetry view) is derived
    # from the SAME first-seen order so build and query lineage agree; ``leaves``
    # carries the full qualifier-preserving tuple for the binder's id resolution.
    leaves = enumerate_canonical_leaves(normalised)
    columns = []
    _seen_names: set[str] = set()
    for lf in leaves:
        if lf.name not in _seen_names:
            _seen_names.add(lf.name)
            columns.append(lf.name)

    # Canonical SQL renders in the canonical internal dialect (postgres) so quote
    # spelling, whitespace, redundant parens, and alias wrappers are all
    # normalised away by the renderer while literals, operand order, casts, and
    # semantic units ('month', timezone) are preserved verbatim. This rendered
    # form — NOT node.dump() — is the identity basis: .dump() embeds source-
    # position metadata (line/col/start/end) that shifts with whitespace and
    # parens and would make identical expressions fingerprint differently.
    canonical_sql = normalised.sql(dialect="postgres")

    # Identity payload: the position-free canonical SQL, the folded semantic-
    # function ids (so a registered alias folds to the same identity while an
    # unknown function keeps its distinct '?name' marker), and the version tags
    # (spec I12: a registry/canonicalizer change invalidates stale fingerprints).
    payload = {
        "canonical_sql": canonical_sql,
        "semantic_fn_ids": fn_ids,
        "canonicalizer_version": CANONICALIZER_VERSION,
        "registry_version": registry_version(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:64]

    return CanonicalExpression(
        canonical_sql=canonical_sql,
        fingerprint=fingerprint,
        input_columns=columns,
        leaves=leaves,
        semantic_function_ids=fn_ids,
        has_unknown_function=unknown,
    )


def canonicalise_sql(
    expression_sql: str,
    *,
    input_dialect: str = "postgres",
) -> Optional[CanonicalExpression]:
    """Convenience: parse a single expression string then canonicalise it.

    Used by unit tests and by producers that only hold the rendered expression
    text (e.g. a stored UDA expression). Returns ``None`` when the string does
    not parse to a single expression. Route-time callers must use
    :func:`canonicalise_ast` on the query's own AST node instead.
    """
    try:
        node = sqlglot.parse_one(expression_sql, read=input_dialect)
    except Exception:
        return None
    if node is None:
        return None
    return canonicalise_ast(node, input_dialect=input_dialect)
