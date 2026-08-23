"""Bug-7775 — RIGHT / FULL join types must not be coerced to LEFT.

The frontend JoinsPanel offers inner / left / right / full and the schema
accepts them, but the rewriter historically coerced anything except ``inner``
to ``LEFT JOIN`` (Bug-7018 only added a warning). A modeller selecting RIGHT or
FULL got silently different row counts and aggregate totals (wrong numbers).

These tests assert the KNOWN emitted JOIN keyword — the load-bearing fact that
distinguishes a RIGHT/FULL join (which preserves the OTHER side's unmatched
rows) from a LEFT join. Two rows that a LEFT join drops and a RIGHT join keeps
are exactly the wrong-numbers class this bug covers, so the emitted keyword is
the correctness contract.
"""
from __future__ import annotations

import types

import pytest

from src.rewrite.joins import _build_joined_from_clause, _join_keyword


# ---------------------------------------------------------------------------
# _join_keyword unit contract (direction + flip)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "join_type,flipped,expected",
    [
        ("inner", False, "INNER JOIN"),
        ("inner", True, "INNER JOIN"),      # inner is symmetric
        ("left", False, "LEFT JOIN"),
        ("left", True, "RIGHT JOIN"),       # flip: modeller LEFT -> RIGHT
        ("right", False, "RIGHT JOIN"),     # NOT coerced to LEFT anymore
        ("right", True, "LEFT JOIN"),       # flip: modeller RIGHT -> LEFT
        ("full", False, "FULL OUTER JOIN"),
        ("full", True, "FULL OUTER JOIN"),  # full is symmetric
        ("full_outer", False, "FULL OUTER JOIN"),
        ("right_outer", False, "RIGHT JOIN"),
    ],
)
def test_join_keyword_honours_type_and_flip(join_type, flipped, expected):
    assert _join_keyword(join_type, flipped=flipped) == expected


def test_join_keyword_right_not_silently_left():
    """Regression guard: a RIGHT join must never render as LEFT (wrong numbers)."""
    assert _join_keyword("right") == "RIGHT JOIN"
    assert _join_keyword("right") != "LEFT JOIN"


def test_join_keyword_full_not_silently_left():
    assert _join_keyword("full") == "FULL OUTER JOIN"
    assert "LEFT" not in _join_keyword("full")


# ---------------------------------------------------------------------------
# End-to-end FROM-clause emission
# ---------------------------------------------------------------------------

def _dim_join_fixture(join_type: str, *, base: str = "fact"):
    """A single fact->dim join with the given modeller join_type.

    The modeller defines the join as left_table_id=fact, right_table_id=dim.
    """
    joins = [
        types.SimpleNamespace(
            left_table_id="fact",
            right_table_id="dim",
            left_column_id="fact_dim_id",
            right_column_id="dim_id",
            join_type=join_type,
        ),
    ]
    tables_by_id = {
        "fact": types.SimpleNamespace(id="fact", physical_name="fact_sales", alias="f"),
        "dim": types.SimpleNamespace(id="dim", physical_name="dim_region", alias="r"),
    }
    columns_by_id = {
        "fact_dim_id": types.SimpleNamespace(model_table_id="fact", column_name="region_id"),
        "dim_id": types.SimpleNamespace(model_table_id="dim", column_name="id"),
    }
    alias_by_table_id = {"fact": "f", "dim": "r"}
    return dict(
        base_table_id=base,
        required_table_ids={"fact", "dim"},
        joins=joins,
        tables_by_id=tables_by_id,
        columns_by_id=columns_by_id,
        alias_by_table_id=alias_by_table_id,
    )


def test_right_join_emits_right_when_base_is_modeller_left():
    """Base = fact (the modeller's LEFT table): traversal is NOT flipped, so a
    modeller RIGHT join renders as RIGHT JOIN — the dimension side is preserved,
    keeping unmatched dimension rows that a LEFT join would drop."""
    from_clause = _build_joined_from_clause(**_dim_join_fixture("right"))
    assert from_clause is not None
    assert "RIGHT JOIN" in from_clause
    assert "LEFT JOIN" not in from_clause


def test_full_join_emits_full_outer():
    from_clause = _build_joined_from_clause(**_dim_join_fixture("full"))
    assert from_clause is not None
    assert "FULL OUTER JOIN" in from_clause
    assert "LEFT JOIN" not in from_clause


def test_left_join_flips_to_right_when_traversed_reversed():
    """Base = dim (the modeller's RIGHT table): the traversal adds ``fact`` (the
    modeller's LEFT table) as the JOIN side, so a modeller LEFT join must render
    as RIGHT JOIN to preserve the SAME physical side's unmatched rows. Emitting
    LEFT here would silently swap which table's rows survive — wrong numbers."""
    fx = _dim_join_fixture("left", base="dim")
    from_clause = _build_joined_from_clause(**fx)
    assert from_clause is not None
    # fact is added on the right of the JOIN; modeller LEFT -> emitted RIGHT.
    assert "RIGHT JOIN" in from_clause
    assert from_clause.startswith('"dim_region" AS "r"')


def test_right_join_flips_to_left_when_traversed_reversed():
    """Mirror of the above: base = dim, modeller RIGHT -> emitted LEFT."""
    fx = _dim_join_fixture("right", base="dim")
    from_clause = _build_joined_from_clause(**fx)
    assert from_clause is not None
    assert "LEFT JOIN" in from_clause
    assert "RIGHT JOIN" not in from_clause


@pytest.mark.parametrize("flipped", [False, True])
def test_unknown_join_token_stays_unflipped_left(flipped):
    """Codex R1 finding 2: legacy/unknown tokens (e.g. ``many_to_one``) that the
    schema still accepts must preserve the EXACT historical behaviour — a plain,
    UN-FLIPPED LEFT JOIN — even under a reversed traversal. Flipping them to RIGHT
    would silently drop the fact-side rows the old code kept."""
    assert _join_keyword("many_to_one", flipped=flipped) == "LEFT JOIN"
    assert _join_keyword("one_to_many", flipped=flipped) == "LEFT JOIN"


def test_bare_outer_token_not_promoted_to_full():
    """Codex R1 finding 2: ambiguous bare ``outer`` must NOT become FULL OUTER —
    it historically fell through to LEFT. No producer contract promotes it."""
    assert _join_keyword("outer") == "LEFT JOIN"
    assert _join_keyword("outer", flipped=True) == "LEFT JOIN"


def test_inner_join_symmetric_under_flip():
    """INNER is direction-agnostic: same keyword whichever side is base."""
    fwd = _build_joined_from_clause(**_dim_join_fixture("inner", base="fact"))
    rev = _build_joined_from_clause(**_dim_join_fixture("inner", base="dim"))
    assert "INNER JOIN" in fwd
    assert "INNER JOIN" in rev
    assert "LEFT JOIN" not in fwd and "RIGHT JOIN" not in fwd
    assert "LEFT JOIN" not in rev and "RIGHT JOIN" not in rev
