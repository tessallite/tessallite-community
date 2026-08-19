"""Unified persona restricted-column closure (Bug-7608 / Bug-7045).

Single source of truth for "which columns is a persona CLS-restricted from,
transitively through measures, UDAs, hierarchies, variant chains, and
calculated/derived expressions". BOTH the model-service catalogue
(name-disclosure hiding) and the query-router serving gate (value-blocking)
call this module, so a measure that transitively touches a restricted column
can never be hidden in one service but served by the other.

Design
------
The closure is expressed over duck-typed model objects (SQLAlchemy ``Measure``
/ ``Dimension`` ORM rows or any object exposing the same attributes) and a
:class:`ClosureContext` lookup bundle that each service populates from its own
DB session. The algorithm itself lives here ONCE:

* direct ``source_column_id`` membership;
* direct ``display_column_id`` membership (Bug-7804 — a flat dimension whose
  CAPTION column is restricted while the KEY column is clean);
* UDA-backed objects — the UDA's materialised column refs (Bug-7606);
* variant measures — the base measure's closure (cycle-guarded);
* calculated measures — every ``measure("name")`` reference's closure, resolved
  transitively (cycle-guarded, fail-closed on parse error);
* calculated dimensions — physical column names referenced by the expression,
  with fail-closed handling of stars / whole-row references / unknown
  identifiers (Bug-7607);
* derived-expression leaves — the bound leaf columns of a query's
  ``bound_derived_expressions`` (function grains such as
  ``GROUP BY UPPER(restricted_col)``), id-first with a physical-name fallback.

Identifiers are compared as lowercased strings for name-level checks and as
``str()``-ed UUIDs for id-level checks, so callers can pass either
``set[UUID]`` or ``set[str]`` — everything is normalised on the way in.

Fail-closed is the invariant: any object whose closure cannot be enumerated
(unparseable expression, missing lookup context under an active restriction)
is treated as restricted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

__all__ = [
    "ClosureContext",
    "object_touches_restricted",
    "calc_expression_touches_restricted",
    "derived_expression_leaves_restricted",
    "compute_transitive_hidden_measure_names",
    "normalise_id_set",
]


def normalise_id_set(ids: Iterable[Any] | None) -> set[str]:
    """UUID/string id set as strings (accepts set[UUID] or set[str])."""
    if not ids:
        return set()
    return {str(i) for i in ids if i is not None}


@dataclass
class ClosureContext:
    """Shared lookup bundle for the closure algorithm.

    Each field is populated by the calling service from its own DB session.
    Every collection defaults empty so a plain source-column model pays for
    nothing it does not use.

    * ``restricted_uda_ids`` — UDA ids that reference a restricted column
      (``str``). An object whose ``user_defined_attribute_id`` is in this set
      is restricted.
    * ``measures_by_id`` / ``measures_by_name`` — the model's measures keyed by
      ``str(id)`` and by name, so variant-base and calc-measure references
      resolve without a DB round-trip.
    * ``restricted_physical_names`` — restricted-column physical names
      (lowercased). ``None`` means "not loaded" for callers that populate it
      lazily; an empty set is a valid loaded value.
    * ``known_physical_names`` — EVERY model physical column name (lowercased),
      used by the calc-dimension gate to fail closed on an identifier that is
      not a known column (possible whole-row reference).
    * ``table_identifiers`` — model table physical-names + aliases (lowercased);
      an identifier matching one is a whole-row reference even if a same-named
      column also exists (Bug-7607 R2-2).
    """

    restricted_uda_ids: set[str] = field(default_factory=set)
    measures_by_id: dict[str, Any] = field(default_factory=dict)
    measures_by_name: dict[str, Any] = field(default_factory=dict)
    restricted_physical_names: Optional[set[str]] = None
    known_physical_names: Optional[set[str]] = None
    table_identifiers: Optional[set[str]] = None


def _parse_measure_reference_names(expression: str | None) -> set[str]:
    """``measure("name")`` reference names of a calculated-measure expression.

    Raises on parse failure so the caller applies fail-closed policy.
    """
    from shared.semantic.calculated_expression import parse_expression

    if not expression:
        return set()
    parsed = parse_expression(expression)
    return set(parsed.referenced_names)


def object_touches_restricted(
    obj: Any,
    restricted_ids: set[str],
    ctx: ClosureContext,
    _visited: set[str] | None = None,
) -> bool:
    """True when the object's full column closure intersects the restricted set.

    ``restricted_ids`` are stable ``ModelColumn`` ids as strings. ``obj`` is a
    duck-typed measure/dimension. See the module docstring for the closure
    rules. Fail-closed on any unverifiable branch.
    """
    src = getattr(obj, "source_column_id", None)
    if src is not None and str(src) in restricted_ids:
        return True

    # Bug-7804: a flat dimension can surface a SEPARATE display column
    # (``display_column_id``) as its member CAPTION while the KEY column is
    # clean. Serving reads the display column's VALUE, so a restricted display
    # column leaks even when the key is unrestricted. Only dimensions carry
    # this attribute; measures return None.
    display_id = getattr(obj, "display_column_id", None)
    if display_id is not None and str(display_id) in restricted_ids:
        return True

    # Bug-7606: UDA-backed objects reference physical columns through their UDA
    # column refs; ``restricted_uda_ids`` is the set of UDAs that touch a
    # restricted column.
    uda_id = getattr(obj, "user_defined_attribute_id", None)
    if uda_id is not None and str(uda_id) in ctx.restricted_uda_ids:
        return True

    if _visited is None:
        _visited = set()

    # Variant measure — the base measure's closure.
    base_id = getattr(obj, "variant_of_measure_id", None)
    if base_id is not None:
        base = ctx.measures_by_id.get(str(base_id))
        if base is not None and str(base_id) not in _visited:
            _visited.add(str(base_id))
            if object_touches_restricted(base, restricted_ids, ctx, _visited):
                return True

    # Calculated measure — every ``measure("name")`` reference's closure.
    if getattr(obj, "measure_type", None) == "calculated":
        try:
            ref_names = _parse_measure_reference_names(
                getattr(obj, "expression", None)
            )
        except Exception:
            # Cannot enumerate the expression's column closure — fail closed.
            return True
        for ref_name in ref_names:
            ref = ctx.measures_by_name.get(ref_name)
            if ref is None:
                # F-008-09: an unresolved measure() name cannot be proven
                # clean (filtered list, rename, draft/deploy skew). Fail closed.
                return True
            ref_key = str(getattr(ref, "id", ref_name))
            if ref_key in _visited:
                continue
            _visited.add(ref_key)
            if object_touches_restricted(ref, restricted_ids, ctx, _visited):
                return True

    # Calculated dimension — physical column names referenced by the expression.
    # F-008-08 / Bug-7813: ``restricted_physical_names is None`` means the
    # caller did not load the set. Skipping the branch was fail-open. Treat
    # unloaded names as unverifiable and fail closed whenever a calc
    # expression is present. An empty loaded set is a valid "nothing
    # restricted" value and still runs the name gate.
    calc_expr = getattr(obj, "calc_expression", None)
    if calc_expr:
        if ctx.restricted_physical_names is None:
            return True
        if calc_expression_touches_restricted(calc_expr, ctx):
            return True

    return False


def calc_expression_touches_restricted(
    calc_expr: str,
    ctx: ClosureContext,
) -> bool:
    """True when a calc/derived SQL expression references a restricted column.

    Fail-closed (Bug-7607): on parse error, on any star (``*`` / ``t.*``), on
    any identifier that names a restricted column, matches a model table
    (whole-row reference), or is NOT a known model physical column (a possible
    whole-row/table reference the rewriter would leave verbatim so the source
    DB expands the whole row). The rewriter qualifies calc expressions by name,
    so name-level matching mirrors what would execute.
    """
    import sqlglot
    from sqlglot import exp as _sg_exp

    restricted_phys = ctx.restricted_physical_names or set()

    try:
        tree = sqlglot.parse_one(calc_expr, read="postgres")
    except Exception:
        return True
    if tree is None:
        return True

    if any(True for _ in tree.find_all(_sg_exp.Star)):
        return True

    known = ctx.known_physical_names
    tables = ctx.table_identifiers or set()
    for node in tree.find_all(_sg_exp.Column):
        name = (node.name or "").lower()
        if not name:
            continue
        if name in restricted_phys:
            return True
        # An identifier matching a TABLE name/alias is a whole-row reference
        # (``to_jsonb(orders)``) even if a same-named column exists.
        if name in tables:
            return True
        # An identifier we cannot confirm is a real column is treated as a
        # possible whole-row reference — fail closed. When the known-column set
        # is unavailable the absence of confirmation is itself grounds to fail
        # closed.
        if known is None or name not in known:
            return True
    return False


def derived_expression_leaves_restricted(
    bound_derived_expressions: Iterable[Any] | None,
    restricted_ids: set[str],
    restricted_physical_names: set[str] | None,
) -> bool:
    """True when any bound derived-expression LEAF column is restricted.

    CLS derived-expression leaf gap (intake
    2026-07-14-cls-derived-expression-leaf-not-checked-source-path): a function
    grain such as ``GROUP BY UPPER(restricted_col)`` contributes no bare grain
    dimension, so a restricted leaf referenced ONLY inside the expression was
    never in the touched-column set and slipped past CLS on the source route
    (UPPER = disclosure modulo case; DATE_TRUNC = partial disclosure).

    Each ``BoundDerivedExpression`` exposes ``inputs`` (a list of leaf
    ``BoundColumnRef``). Every leaf carries a stable ``column_id`` when the
    binder bound the whole expression cleanly against the deployed snapshot
    (spec §7.1 all-or-nothing) and a ``physical_column`` name always. The check
    is id-first (exact, cross-relation-safe) with a physical-name fallback for
    the diagnostic case where the binder withheld ids (``column_id == ""``):
    fall back to the restricted physical-name set so a restricted leaf still
    blocks. Fail-closed — a leaf we cannot resolve to either an id or a name
    match still blocks when its name is restricted; a leaf with neither a bound
    id nor a restricted name is a non-restricted leaf and does not block.
    """
    if not bound_derived_expressions:
        return False
    phys = restricted_physical_names or set()
    for bde in bound_derived_expressions:
        for leaf in getattr(bde, "inputs", None) or []:
            col_id = getattr(leaf, "column_id", None)
            if col_id and str(col_id) in restricted_ids:
                return True
            # Fallback for a leaf the binder left unbound (column_id == "").
            name = (getattr(leaf, "physical_column", None) or "").lower()
            if name and name in phys:
                return True
    return False


def compute_transitive_hidden_measure_names(
    all_measures: Iterable[Any],
    restricted_ids: set[str],
    ctx: ClosureContext,
) -> set[str]:
    """Fixed-point transitive closure of CLS-hidden measure NAMES (Bug-6896).

    A measure is hidden if it directly touches a restricted column OR if its
    expression references any measure that is (transitively) hidden. Seeded via
    the unified :func:`object_touches_restricted` so the model-service
    catalogue's hidden set is derived from the SAME algorithm the query-router
    serving gate uses. Fixed-point iteration terminates at a stable set or after
    ``len(all_measures)`` rounds (cycle-safe).
    """
    measures = list(all_measures)
    hidden: set[str] = set()
    for m in measures:
        if object_touches_restricted(m, restricted_ids, ctx):
            hidden.add(getattr(m, "name", None))
    hidden.discard(None)
    if not hidden:
        return hidden

    # Pre-compute reference edges for calculated measures so the loop does not
    # re-parse expressions on every iteration. Fail-closed: an unparseable
    # calc expression hides the measure.
    calc_refs: dict[str, set[str]] = {}
    for m in measures:
        name = getattr(m, "name", None)
        if name is None or name in hidden:
            continue
        if getattr(m, "measure_type", None) != "calculated":
            continue
        try:
            refs = _parse_measure_reference_names(getattr(m, "expression", None))
        except Exception:
            hidden.add(name)
            continue
        if refs:
            calc_refs[name] = refs

    max_rounds = len(measures)
    for _ in range(max_rounds):
        newly_hidden: set[str] = set()
        for name, refs in calc_refs.items():
            if name not in hidden and (refs & hidden):
                newly_hidden.add(name)
        if not newly_hidden:
            break
        hidden |= newly_hidden
        for name in newly_hidden:
            calc_refs.pop(name, None)

    return hidden
