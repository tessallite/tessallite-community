"""Named Query star-definition expansion (Bug-9161 corrected Phase 1).

THE CORE GOTCHA (why ``force_route="source"`` alone is not enough):

``rewrite/source_sql.rewrite_for_source`` short-circuits a ``SELECT *`` whose
bound query has ``select_star=True`` and no persona-narrowed star: it returns
``_substitute_table_names(...)`` — ``SELECT * FROM <anchor_table>``, a SINGLE
physical table with NO joins and PHYSICAL column names (probe:
``SELECT * FROM "demo"."fact_sales"``). A Named Query defined as
``SELECT * FROM model`` therefore collapsed to the fact table only — wrong
columns, wrong rows (every declared dimension join and its INNER/LOSSY
semantics silently dropped).

THE FIX (guard-clean, no engine touch):

Expand a ``SELECT * FROM model`` definition (per the POPULATION predicate
``is_expandable_star_definition`` — wide: LIMIT/OFFSET/DISTINCT/functions
allowed) to an EXPLICIT column list of every exposed (non-hidden) model field
BEFORE compilation, in NQ-land. ``select_star`` is then False and the ordinary
source route falls through to ``_build_source_sql`` — the definition-scoped
closure built by the shared resolver over the model's DECLARED join types.
Plain measures bind as measure-as-dimension (raw columns at detail level, no
aggregation — probe-verified), exactly the star's detail semantics;
calculated/variant measures have no detail-level column (no source column /
aggregation-only), are never part of any existing star rendering, and are
therefore NOT expanded.

TWO PREDICATES, NEVER MERGED (NQ2C-F2/F6): the narrow
``is_row_preserving_star_definition`` stays frozen as the pocket RLS
materialised-serving SECURITY proof (it must stay narrow — a DISTINCT/LIMIT
definition is materialised-INELIGIBLE under RLS); the wide
``is_expandable_star_definition`` is the POPULATION trigger. The NQ serve
handler additionally pre-narrows the expansion to the persona/CLS-permitted
subset via ``allowed_fields`` (NQ2C-F1), so a restricted reader gets a narrowed
live result instead of the explicit-projection deny branch's 403.

The enumeration mirrors the binder's own ``SELECT *`` resolution for a deployed
model with ``include_hidden=False`` (``semantic/binder.py``): visible dimensions
+ visible measures from the deployed snapshot, hidden = source column id in the
snapshot's hidden-column set. Build and live both expand from the SAME deployed
snapshot, so the expanded definition — and everything derived from it — is
byte-identical on both sides.
"""
from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp


def is_row_preserving_star_definition(definition_sql: str) -> bool:
    """Structural half of the pocket row-preserving proof, on the definition.

    Mirrors pocket §5.1 rule 2: a row-preserving ``SELECT ... FROM <table>``
    with NO join, subquery, CTE, set operation, DISTINCT, GROUP BY, LIMIT,
    OFFSET, sample or table function. This is the same structural predicate the
    query-router's projection-shape RLS proof consumes
    (``named_query_resolver._definition_is_row_preserving_projection``, which
    delegates here). Returns True for an unparseable definition only when it is
    structurally a single select (fail closed on anything else).

    CONSUMER: the RLS materialised-serving SECURITY proof ONLY. This predicate
    is deliberately NARROW because it underwrites a security proof — a
    DISTINCT/LIMIT definition must stay materialised-INELIGIBLE under RLS
    (post-hoc row filtering of a DISTINCT/LIMIT result is not equivalent to
    filtering before it). The POPULATION side uses the WIDER sibling
    ``is_expandable_star_definition``. The two must never be merged: they have
    opposite widening pressure (NQ2C-F2/F6).
    """
    try:
        ast = sqlglot.parse_one(definition_sql)
    except Exception:
        return False
    if ast is None or not isinstance(ast, exp.Select):
        return False
    if ast.args.get("with_"):
        return False
    if ast.args.get("joins"):
        return False
    if ast.args.get("group"):
        return False
    if ast.args.get("having"):
        return False
    if ast.args.get("limit") is not None or ast.args.get("offset") is not None:
        return False
    if ast.args.get("distinct"):
        return False
    if ast.args.get("laterals"):
        return False
    if len(ast.expressions) != 1 or not isinstance(ast.expressions[0], exp.Star):
        # An explicit column list is still row-preserving BUT the pocket
        # proof requires SELECT * — an explicit list can silently omit the
        # security column, and the manifest check is the only guard.
        # Keep parity with the pocket proof: require the star.
        return False
    from_clause = ast.args.get("from_")
    if from_clause is None or not isinstance(from_clause.this, exp.Table):
        return False
    # No subqueries anywhere (the WHERE may carry none).
    for node in ast.walk():
        if isinstance(node, (exp.Subquery, exp.Union, exp.Intersect, exp.Except)):
            return False
        if isinstance(node, exp.Anonymous):
            return False
    return True


def is_expandable_star_definition(definition_sql: str) -> bool:
    """Population-side predicate: which star definitions the expansion expands.

    A single ``exp.Star`` projection over a single ``exp.Table`` with no joins,
    CTE, set operation, subquery, GROUP BY or HAVING. LIMIT / OFFSET / DISTINCT
    and function calls are ALLOWED — they are population-irrelevant: a
    ``SELECT * FROM model LIMIT n`` still means "the model's exposed fields",
    and the expansion preserves them because it only replaces
    ``ast.args["expressions"]`` (NQ2C-F2).

    CONSUMER: the population expansion ONLY (``expand_named_query_star_definition``).
    This is deliberately WIDER than ``is_row_preserving_star_definition``, which
    must stay NARROW for the RLS security proof. The two must never be merged:
    widening the security predicate would silently admit DISTINCT/LIMIT
    definitions to the RLS materialised fast path (NQ2C-F2/F6).
    """
    try:
        ast = sqlglot.parse_one(definition_sql)
    except Exception:
        return False
    if ast is None or not isinstance(ast, exp.Select):
        return False
    if ast.args.get("with_"):
        return False
    if ast.args.get("joins"):
        return False
    if ast.args.get("group"):
        return False
    if ast.args.get("having"):
        return False
    if ast.args.get("laterals"):
        return False
    if len(ast.expressions) != 1 or not isinstance(ast.expressions[0], exp.Star):
        return False
    from_clause = ast.args.get("from_")
    if from_clause is None or not isinstance(from_clause.this, exp.Table):
        return False
    # No subqueries anywhere (the WHERE may carry none). Function calls in the
    # WHERE / ORDER BY are allowed — unlike the security predicate, the
    # population question does not exclude them.
    for node in ast.walk():
        if isinstance(node, (exp.Subquery, exp.Union, exp.Intersect, exp.Except)):
            return False
    return True


def _snapshot_hidden_column_ids(snapshot: dict[str, Any]) -> set[str]:
    """Ids of hidden model columns from the deployed snapshot's ``columns``
    family (the same authority the binder's ``hidden_column_ids`` uses)."""
    out: set[str] = set()
    for col in snapshot.get("columns") or []:
        if not isinstance(col, dict):
            continue
        if col.get("is_hidden") and col.get("id"):
            out.add(str(col["id"]))
    return out


def _is_hidden(obj: dict[str, Any], hidden_ids: set[str]) -> bool:
    """Mirror ``binder._is_semantic_object_hidden``: hidden iff the object's
    source column id is in the snapshot's hidden-column set."""
    source_column_id = obj.get("source_column_id")
    if source_column_id is None:
        return False
    return str(source_column_id) in hidden_ids


def exposed_star_fields_by_kind(
    snapshot: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The exposed (non-hidden) star fields as ``(dimensions, measures)``.

    The same enumeration ``exposed_star_fields`` returns, split by snapshot
    family so persona allow-list narrowing (NQ2C-F1) can apply the
    measure/dimension rules to exactly the rows the expansion would emit.
    """
    hidden_ids = _snapshot_hidden_column_ids(snapshot)
    dim_fields: list[dict[str, Any]] = []
    meas_fields: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _admit(rows: list, out: list) -> None:
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            if not name or _is_hidden(row, hidden_ids):
                continue
            key = str(name).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(row)

    _admit(snapshot.get("dimensions") or [], dim_fields)
    for meas in snapshot.get("measures") or []:
        if not isinstance(meas, dict):
            continue
        name = meas.get("name")
        if not name or _is_hidden(meas, hidden_ids):
            continue
        if (meas.get("measure_type") == "calculated"
                or meas.get("variant_kind") is not None):
            continue
        if (meas.get("source_column_id") is None
                and meas.get("user_defined_attribute_id") is None):
            continue
        key = str(name).lower()
        if key in seen:
            continue
        seen.add(key)
        meas_fields.append(meas)
    return dim_fields, meas_fields


def exposed_star_fields(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """The exposed (non-hidden) field ROWS of the deployed model's star.

    The same enumeration ``_exposed_star_columns`` names: snapshot dimensions
    first, then snapshot measures, minus anything whose source column is
    hidden. Measures that cannot render at detail level (calculated / variant /
    no physical source column or UDA) are excluded — no existing star
    rendering emits them, and including them would push the compile onto the
    AGGREGATED measure path (probe-verified), changing the star's row
    population.

    Public so the query-router's NQ handler can compute the persona/CLS-
    PERMITTED SUBSET of the same field set (NQ2C-F1) and pass it back as
    ``allowed_fields`` — the narrowing must be applied to exactly the fields
    the expansion would emit, never a parallel enumeration.
    """
    dim_fields, meas_fields = exposed_star_fields_by_kind(snapshot)
    return [*dim_fields, *meas_fields]


def _exposed_star_columns(snapshot: dict[str, Any]) -> list[str]:
    """The exposed (non-hidden) semantic field names of the deployed model."""
    return [str(f["name"]) for f in exposed_star_fields(snapshot)]


def expand_named_query_star_definition(
    definition_sql: str,
    deployed_snapshot: dict[str, Any],
    *,
    allowed_fields: set[str] | None = None,
) -> str:
    """Expand a star definition to an explicit projection of the model's
    exposed (non-hidden) fields.

    Triggers on the POPULATION predicate ``is_expandable_star_definition``
    (wide: LIMIT / OFFSET / DISTINCT / function calls allowed — NQ2C-F2), NOT
    the narrow RLS security predicate. Non-star definitions pass through
    unchanged. Expansion preserves the definition's other clauses (LIMIT /
    OFFSET / DISTINCT / WHERE / ORDER BY) because it only replaces
    ``ast.args["expressions"]``.

    ``allowed_fields`` (NQ2C-F1): a set of semantic field names the caller
    (the NQ serve handler) has pre-narrowed to the persona/CLS-PERMITTED
    subset of the exposed set. The expansion projects only those fields, so
    the live compile under a restricted principal narrows instead of hitting
    the explicit-projection DENY branch of the persona gate / CLS gate. Only
    the LIVE path ever passes this — the build never narrows, so build == live
    holds for every artifact a restricted principal can reach (they cannot
    reach the materialised path at all).

    Both the refresh build and the live serve path call this with the SAME
    deployed snapshot, so the expanded definition — the input to the canonical
    compile AND the population fingerprint — is deterministic and identical on
    both sides.

    Raises ``ValueError`` when the definition IS the star shape but nothing is
    left to project (the deployed model exposes no field, or ``allowed_fields``
    intersects the exposure to empty — the latter case is pre-empted by the
    handler's CLS-style 403, so this is fail-closed belt-and-braces).
    """
    if not isinstance(deployed_snapshot, dict):
        return definition_sql
    if not is_expandable_star_definition(definition_sql):
        return definition_sql
    names = _exposed_star_columns(deployed_snapshot)
    if allowed_fields is not None:
        names = [n for n in names if n in allowed_fields]
    if not names:
        raise ValueError(
            "The Named Query definition is a SELECT * but the deployed model "
            "exposes no non-hidden field to project; the star cannot be "
            "compiled to its canonical closure. Deploy a model with visible "
            "dimensions/measures or change the definition to an explicit "
            "projection."
        )
    ast = sqlglot.parse_one(definition_sql)
    ast.args["expressions"] = [
        exp.column(name, quoted=True) for name in names
    ]
    return ast.sql(dialect="postgres")
