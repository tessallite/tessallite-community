"""Shared pocket model-subset structural grammar (F-005-03).

A pocket caches a row-subset of a single model: ``SELECT * FROM <model>
WHERE <simple predicates>``. The query-router's pocket matcher trusts that
every persisted pocket holds exactly such a slice — it routes a query onto a
pocket by fingerprint + predicate-subset containment, never re-checking the
pocket's own SQL shape. If a writer persists a pocket whose defining SQL is an
aggregate (``GROUP BY``), a projection (not ``SELECT *``), a multi-table join,
or complex SQL, the cached table holds rows of a different shape than the
matcher assumes and re-issued queries are rewritten onto columns that do not
exist (502s) or served shape-wrong rows.

The structural grammar was historically enforced in ONE consumer — the
model-service create/validate API (``_check_pocket_structure``). The optimizer
auto-create path and the scheduled-refresh path went through
``shared.pocket.refresh`` which checked only ``ok`` / ``has_unresolvable_where``
/ ``has_complex_sql``, not the full subset grammar. This module is the single
source of truth for the grammar so EVERY writer enforces the same invariant at
the shared chokepoint (``refresh_pocket_definition``) and the model-service API
delegates here instead of carrying a parallel copy.

The check is field-level only — it reads flags the query-router ``/validate``
endpoint already computed (``select_star``, ``from_tables``, ``grain``,
``has_complex_sql``, ``has_unresolvable_where``). No SQL parsing happens here and
no database-type branching is performed; this module never re-implements the
parser.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PocketStructureViolation:
    """One structural-grammar violation, with a stable machine code."""

    code: str
    message: str
    suggestion: str


def collect_pocket_structure_violations(
    validation: dict,
    model_slug: str = "",
) -> list[PocketStructureViolation]:
    """Return the model-subset grammar violations for a ``/validate`` response.

    ``validation`` is the query-router ``/validate`` response dict. An empty
    list means the SQL is a valid pocket subset. ``model_slug`` (optional)
    tightens the FROM check to the model's published slug / technical view.
    """
    violations: list[PocketStructureViolation] = []
    slug = (model_slug or "").lower()
    allowed_slugs = {slug, f"{slug}_technical"} if slug else set()

    if not validation.get("select_star"):
        violations.append(PocketStructureViolation(
            code="SELECT_MUST_BE_STAR",
            message="Pocket SELECT must be `*` (v1 limitation).",
            suggestion=(
                f"Replace the projection with `SELECT * FROM {slug or 'model'} …`."
            ),
        ))

    from_tables = validation.get("from_tables") or []
    if len(from_tables) != 1:
        violations.append(PocketStructureViolation(
            code="MULTIPLE_FROM_TABLES",
            message="Pocket SQL must reference exactly one table in FROM.",
            suggestion=(
                f"Use a single `FROM {slug or 'model'}`; joins live in the model."
            ),
        ))
    elif allowed_slugs and from_tables[0].lower() not in allowed_slugs:
        violations.append(PocketStructureViolation(
            code="FROM_NOT_MODEL",
            message=f"Pocket FROM `{from_tables[0]}` is not the model.",
            suggestion=f"Replace `{from_tables[0]}` with `{slug}`.",
        ))

    if validation.get("has_complex_sql"):
        violations.append(PocketStructureViolation(
            code="COMPLEX_SQL_NOT_ALLOWED",
            message="CTEs, subqueries, and window functions are not allowed.",
            suggestion=(
                f"Express the slice as `SELECT * FROM {slug or 'model'} WHERE …`."
            ),
        ))

    # F-005-01 (fail closed): predicate extraction could not fully describe the
    # WHERE clause, so the persisted PocketPredicate rows would under-describe
    # the cached slice and the matcher would serve incomplete results.
    if validation.get("has_unresolvable_where"):
        violations.append(PocketStructureViolation(
            code="UNRESOLVABLE_WHERE",
            message=(
                "The WHERE clause contains predicates the engine cannot fully "
                "extract, so the cached rows cannot be described exactly. A "
                "pocket like this would serve incomplete results."
            ),
            suggestion=(
                "Rewrite the WHERE clause using simple column comparisons "
                "(=, IN, NOT IN, <, >, BETWEEN) on model columns; remove "
                "expressions, functions, or subquery IN lists the engine "
                "cannot resolve."
            ),
        ))

    if validation.get("grain"):
        violations.append(PocketStructureViolation(
            code="GROUP_BY_NOT_ALLOWED",
            message="GROUP BY is not allowed.",
            suggestion="Aggregations belong to AggregateTables, not pockets.",
        ))

    return violations
