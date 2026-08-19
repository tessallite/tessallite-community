"""Unit tests for the redundant-partner helper.

Bug-8647 (wrong numbers): the module claims a dimension-side join key carries
the same value as its fact-side partner, and both the aggregate-grain guard
and the XMLA catalogue act on that claim. It holds only for an INNER join.
The known-value tests below execute the two GROUP BYs on real rows so the
difference is a measured breakdown, not an argument.

Tier: T3 (wrong numbers). Scope: isolated / pure logic.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import UUID, uuid4

from shared.semantic.redundant_partner import compute_redundant_partners


@dataclass
class _FakeTable:
    id: UUID
    physical_name: str
    table_type: str


@dataclass
class _FakeColumn:
    id: UUID
    model_table_id: UUID
    column_name: str


@dataclass
class _FakeJoin:
    id: UUID
    left_table_id: UUID
    right_table_id: UUID
    left_column_id: UUID
    right_column_id: UUID
    join_type: str


def _build_inner_join_fixture():
    fact = _FakeTable(uuid4(), "demo_data.fact_payment", "fact")
    dim = _FakeTable(uuid4(), "demo_data.dim_account", "dim_aggregate")
    fact_col = _FakeColumn(uuid4(), fact.id, "account_type")
    dim_col = _FakeColumn(uuid4(), dim.id, "account_type_code")
    j = _FakeJoin(uuid4(), dim.id, fact.id, dim_col.id, fact_col.id, "inner")
    tables = {fact.id: fact, dim.id: dim}
    cols = {fact_col.id: fact_col, dim_col.id: dim_col}
    return j, fact, dim, fact_col, dim_col, tables, cols


def test_inner_join_marks_dim_side_redundant():
    j, fact, dim, fact_col, dim_col, tables, cols = _build_inner_join_fixture()
    hints = compute_redundant_partners([j], tables, cols)
    assert dim_col.id in hints
    assert fact_col.id not in hints
    h = hints[dim_col.id]
    assert h.partner_column_name == "account_type"
    assert h.join_type == "inner"


def test_full_outer_join_never_marks_redundant():
    j, fact, dim, fact_col, dim_col, tables, cols = _build_inner_join_fixture()
    j.join_type = "full"
    hints = compute_redundant_partners([j], tables, cols)
    assert hints == {}


def _build_outer_join_fixture(join_type: str, *, fact_on_left: bool):
    """A fact/dimension edge with the requested token and drawing direction."""
    fact = _FakeTable(uuid4(), "demo_data.fact_order", "fact")
    dim = _FakeTable(uuid4(), "demo_data.dim_country", "dim_aggregate")
    fact_col = _FakeColumn(uuid4(), fact.id, "country_code")
    dim_col = _FakeColumn(uuid4(), dim.id, "iso_code")
    if fact_on_left:
        j = _FakeJoin(uuid4(), fact.id, dim.id, fact_col.id, dim_col.id, join_type)
    else:
        j = _FakeJoin(uuid4(), dim.id, fact.id, dim_col.id, fact_col.id, join_type)
    return (
        j,
        fact_col,
        dim_col,
        {fact.id: fact, dim.id: dim},
        {fact_col.id: fact_col, dim_col.id: dim_col},
    )


def _assert_only_the_join_type_suppresses_the_hint(
    join_type: str, *, fact_on_left: bool
) -> None:
    """No hint for ``join_type``, and a hint for ``inner`` on the SAME graph.

    The positive control is the point: ``assert hints == {}`` alone also passes
    when a fixture never reaches the classifier at all (mismatched table ids, a
    column missing from the map), so an empty result is only evidence when the
    identical graph provably DOES produce a hint once the token is inner.
    """
    j, _fact_col, dim_col, tables, cols = _build_outer_join_fixture(
        join_type, fact_on_left=fact_on_left
    )
    assert compute_redundant_partners([j], tables, cols) == {}

    j.join_type = "inner"
    control = compute_redundant_partners([j], tables, cols)
    assert dim_col.id in control, (
        f"positive control failed: the {join_type} fixture "
        f"(fact_on_left={fact_on_left}) never reaches the join-type rule, so "
        "the empty result above proves nothing"
    )


def _left_join_breakdown() -> tuple[dict, dict]:
    """Execute the two GROUP BYs the hint claims are interchangeable.

    Three fact orders, two countries in the dimension, one order whose country
    was never onboarded. Returns ``(by_fact_key, by_dim_key)`` as
    ``{bucket: (row_count, amount_total)}`` — a mapping rather than a row list
    so the assertion does not depend on any engine's NULL sort order.
    """
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE fact_order (id INTEGER, country_code TEXT, amount INTEGER);
        CREATE TABLE dim_country (iso_code TEXT, region TEXT);
        INSERT INTO fact_order VALUES (1, 'US', 100), (2, 'FR', 40), (3, 'ZZ', 7);
        INSERT INTO dim_country VALUES ('US', 'NA'), ('FR', 'EU');
        """
    )
    join = (
        "FROM fact_order f LEFT JOIN dim_country d "
        "ON f.country_code = d.iso_code"
    )

    def breakdown(group_by: str) -> dict:
        rows = db.execute(
            f"SELECT {group_by}, COUNT(*), SUM(f.amount) {join} "
            f"GROUP BY {group_by}"
        ).fetchall()
        return {bucket: (count, total) for bucket, count, total in rows}

    by_fact = breakdown("f.country_code")
    by_dim = breakdown("d.iso_code")
    db.close()
    return by_fact, by_dim


def test_left_join_key_columns_produce_different_breakdowns():
    """Bug-8647, the measured reason: an outer join's keys are not the same column.

    This is the business fact the old rule contradicted. Substituting the
    fact-side key for the dimension-side key in an aggregate grain does not
    preserve the answer: the unmatched order moves out of the NULL bucket and
    into a 'ZZ' bucket of its own. Same three source rows, two different
    answers — which is the definition of a wrong number for whoever reads the
    breakdown.
    """
    by_fact, by_dim = _left_join_breakdown()

    assert by_fact == {"US": (1, 100), "FR": (1, 40), "ZZ": (1, 7)}
    assert by_dim == {"US": (1, 100), "FR": (1, 40), None: (1, 7)}
    assert by_fact != by_dim


def test_left_join_with_fact_on_left_never_marks_redundant():
    """Bug-8647: the LEFT case the old rule admitted.

    Route 2 of the failing-test triage policy retired the previous assertion
    (``test_left_join_with_fact_on_left_marks_right_redundant``). The design
    change that made it obsolete is stated in
    ``shared/semantic/join_keyword.py``: a LEFT join preserves the fact rows,
    so the dimension key is NULL wherever the fact row has no match and the
    NULL bucket measured in the test above appears. The two columns are not
    interchangeable, so no hint may steer the modeller from one to the other.
    """
    _, by_dim = _left_join_breakdown()
    assert by_dim[None] == (1, 7), "fixture must contain an unmatched fact row"
    _assert_only_the_join_type_suppresses_the_hint("left", fact_on_left=True)


def test_right_join_with_fact_on_right_never_marks_redundant():
    """Bug-8647, mirrored: fact on the modeller's RIGHT under a RIGHT join.

    A RIGHT join preserves the modeller's right table, so this is the same
    fact-preserving shape drawn the other way round and carries the identical
    NULL exposure on the dimension key.
    """
    _assert_only_the_join_type_suppresses_the_hint("right", fact_on_left=False)


def test_left_join_with_fact_on_right_not_redundant():
    """The dimension-preserving direction — never admitted, and still isn't.

    Recorded because the v6 archive's Bug-8713 asked for a hint here. That
    request is moot rather than deferred: no outer join emits a hint at all.
    """
    _assert_only_the_join_type_suppresses_the_hint("left", fact_on_left=False)


def test_fact_to_fact_join_not_redundant():
    a = _FakeTable(uuid4(), "fact_a", "fact")
    b = _FakeTable(uuid4(), "fact_b", "fact")
    ca = _FakeColumn(uuid4(), a.id, "id")
    cb = _FakeColumn(uuid4(), b.id, "a_id")
    j = _FakeJoin(uuid4(), a.id, b.id, ca.id, cb.id, "inner")
    hints = compute_redundant_partners(
        [j],
        {a.id: a, b.id: b},
        {ca.id: ca, cb.id: cb},
    )
    assert hints == {}


def test_legacy_cardinality_join_type_never_marks_redundant():
    """Bug-8647: a legacy cardinality token renders as an OUTER join.

    Route 2 of the failing-test triage policy retired the previous assertion
    (``test_legacy_cardinality_join_type_treated_as_inner``, which demanded a
    hint). The design change that made it obsolete is the orientation /
    cardinality split: ``join_keyword.split_join_token`` resolves
    ``many_to_one`` to ``left`` and ``join_keyword.join_keyword`` renders it as
    an un-flipped ``LEFT JOIN`` (invariant 4), which migration ``0194``
    then backfills into the column. The executed SQL was always an outer join,
    so folding the token onto ``inner`` here granted the redundancy claim to a
    join that does not discard unmatched rows — the same wrong-numbers defect
    through the legacy-token path, still reachable because
    ``model_snapshot/rehydrator`` writes an imported bundle's ``join_type``
    verbatim (Bug-8702).
    """
    for token in ("many_to_one", "one_to_many", "one_to_one", "many_to_many"):
        j, fact, dim, fact_col, dim_col, tables, cols = _build_inner_join_fixture()
        j.join_type = token
        assert compute_redundant_partners([j], tables, cols) == {}, token


def test_unrecognised_join_type_never_marks_redundant():
    """An unknown token renders as ``LEFT JOIN`` too, so it earns no hint.

    ``split_join_token`` returns ``(None, None)`` for it and the guardrail
    fails open — the column stays visible and pickable rather than being
    hidden from the XMLA catalogue on an unprovable equivalence claim.
    """
    for token in ("outer", "cross", "", None):
        j, fact, dim, fact_col, dim_col, tables, cols = _build_inner_join_fixture()
        j.join_type = token
        assert compute_redundant_partners([j], tables, cols) == {}, token


def test_long_spelling_inner_token_still_marks_redundant():
    """The one vocabulary is ``join_keyword``'s, not a private copy.

    Importers and hand-seeded rows carry long spellings; ``inner`` is the only
    orientation that earns a hint, however it is spelled or cased.
    """
    for token in ("INNER", " inner "):
        j, fact, dim, fact_col, dim_col, tables, cols = _build_inner_join_fixture()
        j.join_type = token
        hints = compute_redundant_partners([j], tables, cols)
        assert dim_col.id in hints, token
        assert hints[dim_col.id].join_type == "inner"
