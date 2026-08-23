"""Which stored artifacts a join-orientation fix invalidates (Bug-8628, Bug-8639).

Contract: ``docs/architecture/architecture_join-orientation-and-cardinality.md``
invariant 6 — "a join-orientation fix invalidates existing artifacts it could
have miscompiled. Any aggregate/pocket artifact built from a model containing
an outer join must be rebuilt or flagged incompatible when the shared
join-keyword function changes — never left marked fresh while its physical
rows no longer match what the corrected builder would produce."

Two independent pre-fix renderers are covered, each with its OWN historical
bug and its OWN legacy emulation below: ``sql_builder.py`` (Bug-8628 —
``ctas_rendering_changed`` / ``model_ids_needing_rebuild``, used by migration
``0191``) and ``optimizer/lifecycle/creator.py`` (Bug-8639 —
``creator_ctas_rendering_changed`` / ``creator_model_ids_needing_rebuild``,
used by migration ``0192``). They must NOT be conflated: see the Bug-8639
section below for the concrete token where the two histories disagree.

Why this is a module and not a hand-written token list
------------------------------------------------------
The obvious implementation is ``WHERE join_type IN ('left', 'right')``. That
list is wrong in two directions and the repo has been bitten by exactly this
class before (a coverage tool whose own enumeration has a blind spot):

* it MISSES ``full_outer`` / ``full outer`` / ``fullouter``. The pre-fix CTAS
  map keyed only on the literal ``full``, so those spellings fell through to
  its ``LEFT JOIN`` default while the shared renderer emits
  ``FULL OUTER JOIN``. Their rendering changes without a flip being involved
  at all.
* it MISSES ``right_outer`` for the same reason (pre-fix default ``LEFT
  JOIN``, now ``RIGHT JOIN``).
* it would INCLUDE ``inner``, whose only change is the spelling ``JOIN`` ->
  ``INNER JOIN`` — identical rows, so staling those artifacts would cost a
  rebuild for nothing.

So the predicate is DERIVED by running both renderers over the token and
comparing, rather than restated. Adding a token to the vocabulary, or
changing what the shared renderer emits for one, updates this automatically.
"""
from __future__ import annotations

from typing import Iterable

from shared.semantic.join_keyword import join_keyword

#: The EXACT pre-fix keyword map from ``shared/semantic/sql_builder.py``
#: (``_JOIN_SQL``), preserved verbatim so the comparison below is against what
#: shipped rather than against a recollection of it. It is history, not a
#: renderer: nothing may render SQL from this map.
_LEGACY_CTAS_JOIN_SQL: dict[str, str] = {
    "inner": "JOIN",
    "left": "LEFT JOIN",
    "right": "RIGHT JOIN",
    "full": "FULL OUTER JOIN",
}
_LEGACY_CTAS_DEFAULT = "LEFT JOIN"


def _semantic(keyword: str) -> str:
    """Fold the ANSI shorthand so this compares ROWS, not spelling."""
    return "INNER JOIN" if keyword == "JOIN" else keyword


def _legacy_ctas_keyword(join_type: str | None) -> str:
    """What ``build_from_clause`` emitted for this token BEFORE Bug-8628.

    Note the absence of a ``flipped`` argument: that is the defect. The
    pre-fix builder resolved the keyword once and appended the identical
    string from both its forward and its reversed traversal branch.

    The lookup key is ``.lower()`` and NOT ``normalise_token`` — the shipped
    line was literally ``_JOIN_SQL.get(j.join_type.lower(), "LEFT JOIN")``,
    with no ``strip()``. That one-character difference is load-bearing: a
    stored ``" full "`` missed the shipped map and rendered as the LEFT JOIN
    default, while the corrected renderer strips it and emits FULL OUTER JOIN.
    Emulating with a strip would report "rendering unchanged" for exactly the
    rows whose rows DID move, and the artifact would keep serving the old
    population — a blind spot in the invalidation mechanism itself, which is
    the recurring failure class this module's docstring warns about. The
    emulation must be byte-faithful to what shipped, not to what the shipped
    code meant.

    ``None`` maps to the default here. The shipped line raised
    ``AttributeError`` on a NULL ``join_type``, so no artifact can exist that
    was built from one; the corrected renderer also returns ``LEFT JOIN`` for
    it, so either reading gives "unchanged".
    """
    return _LEGACY_CTAS_JOIN_SQL.get(
        (join_type or "").lower(), _LEGACY_CTAS_DEFAULT
    )


def ctas_rendering_changed(join_type: str | None) -> bool:
    """True when the Bug-8628 fix changes the rows this edge contributes.

    Compares the pre-fix rendering against the corrected one under BOTH
    traversal directions, because whether a given edge is traversed forward or
    reversed depends on the model's anchor — which this function cannot see
    and which the anchor rules (Bug-8605) can themselves move. Treating "could
    differ under either direction" as changed keeps the invalidation an
    over-approximation, which is the safe side: a needless rebuild costs
    target-database time, a missed one serves wrong numbers.
    """
    legacy = _semantic(_legacy_ctas_keyword(join_type))
    return any(
        _semantic(join_keyword(join_type, flipped=flipped)) != legacy
        for flipped in (False, True)
    )


def model_ids_needing_rebuild(
    join_rows: Iterable[tuple[object, str | None]],
) -> set[object]:
    """Given ``(model_id, join_type)`` pairs, the model ids to invalidate.

    Pure function over rows the caller has already read, so the rollout
    migration and its test exercise the same logic.
    """
    return {
        model_id
        for model_id, join_type in join_rows
        if ctas_rendering_changed(join_type)
    }


# --- Bug-8639: optimizer/lifecycle/creator.py's OWN pre-fix history --------
#
# ``creator.py::_build_source_from_clause`` is a THIRD independent renderer of
# ``Join.join_type`` (docs/architecture/architecture_join-orientation-and-
# cardinality.md's "Known gap"): it emits the aggregate CTAS at CREATION time,
# while ``sql_builder.py`` emits the scheduler's REFRESH CTAS for the same
# aggregate. Its pre-fix bug was WORSE than ``sql_builder.py``'s Bug-8628: it
# had no per-token map at all, only ``"LEFT JOIN" if join_type.lower() !=
# "inner" else "INNER JOIN"`` — every right/full/legacy token collapsed onto
# LEFT JOIN, not just the flip being missing.
#
# ``_legacy_ctas_keyword`` above emulates ``sql_builder.py``'s history, NOT
# this one, and the two genuinely disagree for a bare ``"full"`` token:
# ``sql_builder.py`` already rendered ``FULL OUTER JOIN`` for it pre-fix (only
# the flip was missing), so the ``sql_builder``-shaped comparison reports
# "unchanged". ``creator.py`` rendered ``LEFT JOIN`` for the identical token,
# so its rows DID move. Reusing ``ctas_rendering_changed`` for aggregates
# built by ``creator.py`` would silently under-invalidate exactly that case —
# the same enumeration-blind-spot class this module's own docstring warns
# about, just one level up (two distinct historical renderers, not two
# distinct token spellings). Two renderers with two distinct bugs need two
# distinct legacy emulations; the shared ``join_keyword()`` call and the
# flip-under-either-direction comparison stay the ONE piece of logic both
# share, so this is additive coverage, not a restatement.
_LEGACY_CREATOR_CTAS_DEFAULT = "LEFT JOIN"


def _legacy_creator_ctas_keyword(join_type: str | None) -> str:
    """What ``creator.py::_build_source_from_clause`` emitted BEFORE Bug-8639.

    Byte-faithful to the shipped line: ``"LEFT JOIN" if (j.join_type or
    "").lower() != "inner" else "INNER JOIN"`` — ``.lower()`` only, no
    ``.strip()``, so a padded token (``" inner "``) fell through to the
    ``LEFT JOIN`` default exactly like every other non-matching token.
    """
    normalized = (join_type or "").lower()
    return "INNER JOIN" if normalized == "inner" else _LEGACY_CREATOR_CTAS_DEFAULT


def creator_ctas_rendering_changed(join_type: str | None) -> bool:
    """True when the Bug-8639 fix changes the rows this edge contributes to
    an aggregate that was CREATED (not merely refreshed) by ``creator.py``'s
    pre-fix renderer.

    Same over-approximating comparison as :func:`ctas_rendering_changed`,
    checked under BOTH traversal directions because whether a given edge was
    traversed forward or reversed when the aggregate was built depends on the
    model's anchor, which this function cannot see.
    """
    legacy = _semantic(_legacy_creator_ctas_keyword(join_type))
    return any(
        _semantic(join_keyword(join_type, flipped=flipped)) != legacy
        for flipped in (False, True)
    )


def creator_model_ids_needing_rebuild(
    join_rows: Iterable[tuple[object, str | None]],
) -> set[object]:
    """Given ``(model_id, join_type)`` pairs, the model ids whose aggregates
    (built by ``creator.py``'s pre-Bug-8639 renderer) need staling.

    Pure function over rows the caller has already read, so the rollout
    migration and its test exercise the same logic.
    """
    return {
        model_id
        for model_id, join_type in join_rows
        if creator_ctas_rendering_changed(join_type)
    }
