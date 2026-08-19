"""The single flip-aware join-keyword renderer every SQL builder must call.

Contract: ``docs/architecture/architecture_join-orientation-and-cardinality.md``
(invariants 1-4). Two independent builders compile a model's join graph into
physical SQL — the query-router's source route
(``query-router/src/rewrite/joins.py``) and the aggregate/pocket CTAS builder
(``shared/semantic/sql_builder.py``) — and before this module each owned its
own keyword logic. The source route flipped ``LEFT``/``RIGHT`` when a join was
traversed from the far side of the modeller's declared direction (Bug-7775);
the CTAS builder never did (Bug-8628), so an aggregate materialised the
OPPOSITE row population to the source route it was supposed to accelerate.

Orientation, stated once
------------------------
A modeller declares a join relative to THEIR ``left_table_id`` /
``right_table_id``. A compiler's traversal, however, decides which physical
table lands on the FROM side by the order it reached them — which is a
function of the anchor, not of the modeller's drawing direction. When the
already-accumulated table is the modeller's RIGHT table, the newly added table
is the modeller's LEFT table and lands on the physical right of the JOIN; the
keyword must then FLIP so the PRESERVED PHYSICAL RELATION is unchanged:

    declared ``dim LEFT JOIN fact``, traversal anchored on ``fact``
        -> emit ``fact RIGHT JOIN dim``   (still preserves ``dim``)

``INNER`` and ``FULL OUTER`` are direction-symmetric and never flip.

Legacy tokens are coerced, never rejected (invariant 4)
-------------------------------------------------------
``Join.join_type`` historically defaulted to ``many_to_one`` — a CARDINALITY
label, not a join type (see ``split_join_token``). Rows created before the
orientation/cardinality split still carry it, and their historical rendering
is a plain, UN-FLIPPED ``LEFT JOIN``. That rendering is preserved exactly:
flipping an undeclared token would silently change which relation an existing
model preserves. Rejecting unrecognised tokens is reserved for deploy-time
validation of new or redeployed models, never for runtime rendering.

Keywords are plain ANSI SQL. The surrounding SELECT is transpiled to the
target dialect by sqlglot downstream, so there is no per-connector branching
here (SQL-generation rule 1).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# --- Orientation vocabulary (what rows survive) ---------------------------
#
# The frontend JoinsPanel offers inner / left / right / full and ``JoinCreate``
# constrains the write path to exactly those four. The longer spellings are
# accepted because imported models (LookML, AtScale, Cube) and hand-seeded
# rows have historically carried them.
INNER_TOKENS: frozenset[str] = frozenset({"inner"})
LEFT_TOKENS: frozenset[str] = frozenset(
    {"left", "left_outer", "left outer", "left join", "leftouter"}
)
RIGHT_TOKENS: frozenset[str] = frozenset(
    {"right", "right_outer", "right outer", "right join", "rightouter"}
)
# Bare ``outer`` is deliberately NOT mapped to FULL — it is ambiguous and
# historically fell through to the LEFT default. Promoting it would need a
# producer contract, so it stays in the unknown path and behaviour is
# unchanged (Codex R1 finding 2 on the Bug-7775 lane).
FULL_TOKENS: frozenset[str] = frozenset(
    {"full", "full_outer", "full outer", "full join", "fullouter"}
)

#: Every token that names WHICH ROWS SURVIVE, independent of traversal.
ORIENTATION_TOKENS: frozenset[str] = (
    INNER_TOKENS | LEFT_TOKENS | RIGHT_TOKENS | FULL_TOKENS
)

#: The canonical four-way orientation vocabulary a new/redeployed model should
#: use. ``shared.schemas.domains.aggregates_security.JoinType`` pins the same
#: set on the write path.
CANONICAL_JOIN_TYPES: tuple[str, ...] = ("inner", "left", "right", "full")

# --- Cardinality vocabulary (how many rows on each side match) ------------
#
# Orthogonal to orientation (invariant 3): cardinality never changes the
# rendered keyword. It lives in ``Join.cardinality``; the tokens below are the
# ones that were historically written into ``join_type`` instead, and are
# recognised here so ``split_join_token`` can route them to the right field.
CARDINALITY_TOKENS: frozenset[str] = frozenset(
    {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
)

#: Hyphenated spellings used by the YAML model-snapshot format.
_CARDINALITY_ALIASES: dict[str, str] = {
    "one-to-one": "one_to_one",
    "one-to-many": "one_to_many",
    "many-to-one": "many_to_one",
    "many-to-many": "many_to_many",
}


def normalise_token(join_type: str | None) -> str:
    """Lower-case and trim a raw token. ``None`` normalises to ``""``."""
    return (join_type or "").strip().lower()


def join_keyword(join_type: str | None, *, flipped: bool = False) -> str:
    """Return the ANSI JOIN keyword for a modeller ``join_type``.

    ``flipped`` is True when the traversal added the modeller's LEFT table as
    the JOIN/right side — i.e. the already-joined table is the modeller's
    RIGHT table. A modeller LEFT outer join then renders as RIGHT and a
    modeller RIGHT outer join as LEFT, so the preserved side stays the same
    physical relation. INNER and FULL OUTER are unaffected.

    An unrecognised or legacy token renders as a plain, UN-FLIPPED
    ``LEFT JOIN`` — its exact historical behaviour (invariant 4).
    """
    normalized = normalise_token(join_type)
    if normalized in INNER_TOKENS:
        return "INNER JOIN"
    if normalized in LEFT_TOKENS:
        return "RIGHT JOIN" if flipped else "LEFT JOIN"
    if normalized in RIGHT_TOKENS:
        return "LEFT JOIN" if flipped else "RIGHT JOIN"
    if normalized in FULL_TOKENS:
        return "FULL OUTER JOIN"
    if normalized:
        logger.warning(
            "Unrecognised join_type %r coerced to LEFT JOIN; "
            "expected 'inner', 'left', 'right', or 'full'. "
            "Check model join definitions.",
            join_type,
        )
    # Historical, un-flipped LEFT JOIN for unknown tokens (see module docstring).
    return "LEFT JOIN"


def is_orientation_declared(join_type: str | None) -> bool:
    """True when the token names which relation the join preserves.

    A legacy/cardinality token does not: :func:`join_keyword` renders it as an
    un-flipped ``LEFT JOIN``, which preserves whichever relation the traversal
    accumulated FIRST — and that is decided by the plan's base table, which is
    not a property of the join. Two plans over the same model with different
    bases therefore render the same legacy edge preserving opposite sides.
    ``routing/pocket_population.py`` refuses any plan containing one.
    """
    return normalise_token(join_type) in ORIENTATION_TOKENS


def normalise_cardinality(value: str | None) -> str | None:
    """Return the canonical cardinality token, or None when unrecognised."""
    token = normalise_token(value)
    token = _CARDINALITY_ALIASES.get(token, token)
    return token if token in CARDINALITY_TOKENS else None


def split_join_token(raw: str | None) -> tuple[str | None, str | None]:
    """Split one historically-conflated token into ``(join_type, cardinality)``.

    ``Join.join_type`` has carried BOTH kinds of value (invariant 3). This is
    the one classifier every importer, deserialiser and seed path uses so a
    cardinality token can never again be persisted into the orientation field.

    * an orientation token  -> ``(token, None)``
    * a cardinality token   -> ``(inferred_join_type, canonical_cardinality)``
    * anything else         -> ``(None, None)`` — the caller keeps its own
      default and the raw value is left for :func:`join_keyword` to coerce.

    The inferred orientation preserves the cardinality label's MANY side,
    because that is what the legacy rendering did on a conventionally drawn
    model: an un-flipped ``LEFT JOIN`` preserves the already-accumulated
    (anchor-ward) relation, and on a star schema the anchor-ward relation IS
    the many side. Measured against the acme-demo seed bundle, every one of
    the 20 legacy ``many_to_one`` edges across its five models is drawn with
    the many side as the modeller's LEFT table, so this inference reproduces
    the legacy rendering exactly on real data rather than moving numbers.
    ``many_to_many`` has no single many side; it keeps the legacy ``left``
    rendering and cannot be proven row-preserving in either direction anyway.
    """
    cardinality = normalise_cardinality(raw)
    if cardinality is not None:
        return _CARDINALITY_TO_JOIN_TYPE[cardinality], cardinality
    token = normalise_token(raw)
    if token in ORIENTATION_TOKENS:
        return token if token in CANONICAL_JOIN_TYPES else _canonical_of(token), None
    return None, None


def edge_cardinality(join: object) -> str | None:
    """The declared fan-out of one join edge, or None when undeclared.

    Reads ``Join.cardinality`` first and falls back to a cardinality token
    still parked in ``join_type`` on a row written before the split. This is
    the ONE place that fallback lives: every consumer that reasons about
    fan-out (drill-through join-path classification in both model-service and
    query-router) reads through here, so they cannot disagree about whether a
    given edge collapses or expands.

    Returns None when neither field names a cardinality — including for a
    perfectly valid ``inner``/``left``/``right``/``full`` join whose modeller
    has not declared one. Callers must treat None as UNKNOWN and stay
    conservative; inventing a cardinality from the orientation is exactly the
    conflation this contract removes.

    Accepts an ORM row, a snapshot-hydrated namespace, or a mapping.
    """
    if isinstance(join, dict):
        declared = join.get("cardinality")
        raw_type = join.get("join_type")
    else:
        declared = getattr(join, "cardinality", None)
        raw_type = getattr(join, "join_type", None)
    return normalise_cardinality(declared) or split_join_token(raw_type)[1]


def _canonical_of(token: str) -> str:
    """Fold a long-spelling orientation token onto the canonical four."""
    if token in INNER_TOKENS:
        return "inner"
    if token in LEFT_TOKENS:
        return "left"
    if token in RIGHT_TOKENS:
        return "right"
    return "full"


_CARDINALITY_TO_JOIN_TYPE: dict[str, str] = {
    # left = the modeller's left table is the many side -> preserve it.
    "many_to_one": "left",
    # right = the modeller's right table is the many side -> preserve it.
    "one_to_many": "right",
    # 1:1 preserves the same rows from either end; ``left`` matches the legacy
    # un-flipped rendering in the conventional forward draw order.
    "one_to_one": "left",
    # No single many side. Keeps the legacy rendering rather than inventing a
    # preservation claim the data cannot support.
    "many_to_many": "left",
}
