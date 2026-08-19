"""Guards for Bug-8634 (Bug-8605 round-3 review finding 3).

``compute_redundant_partners`` decides whether the API answers 200 or 400 for
an aggregate-grain request, and every existing test of that endpoint MOCKS the
function out (``patch("src.api.aggregates.compute_redundant_partners")`` in
``services/model-service/tests/test_aggregates.py``), so the helper itself had
no behavioural coverage at all. Both halves of the Bug-8634 fix were therefore
silently revertible:

1. ``hints`` is keyed by the dimension column and written last-write-wins, so a
   dimension column joined to the fact table by more than one join took
   whichever join the caller's UNORDERED ``select(Join)`` read returned last.
   Both callers (``api/measures.py:_load_redundant_partners`` and
   ``api/dimensions.py:_load_redundant_partners``) read joins with no
   ``ORDER BY``, so the same model could report a different partner — and, for
   a mixed join-type pair, accept a grain on one request and 400 on the next —
   with no edit in between.
2. ``_fact_side`` carried its own lowercasing fact test, which disagreed with
   ``graph_order.is_fact_table`` (case-SENSITIVE, matching the partial unique
   index's SQL literal). A table stored as ``"Fact"`` was the fact table to
   this guard and not the fact table to the anchor rule.

Tier: T2 (fixed-bug regression guard). Scope: isolated / pure logic.
"""
from __future__ import annotations

import uuid

from shared.db.models import Join, ModelColumn, ModelTable
from shared.semantic.redundant_partner import compute_redundant_partners

_MODEL = uuid.uuid4()

# Deliberately chosen so canonical id order (J_LOW then J_HIGH) is the OPPOSITE
# of the order the "creation order" list below presents them in.
_J_LOW = uuid.UUID("00000000-0000-4000-8000-00000000000a")
_J_HIGH = uuid.UUID("ffffffff-0000-4000-8000-00000000000b")

_T_FACT = uuid.UUID(int=0x01)
_T_DIM = uuid.UUID(int=0x02)
_C_FACT_A = uuid.UUID(int=0x11)
_C_FACT_B = uuid.UUID(int=0x12)
_C_DIM = uuid.UUID(int=0x13)


def _graph(fact_table_type: str = "fact"):
    fact = ModelTable(
        id=_T_FACT, model_id=_MODEL, physical_name="public.fact_sales",
        alias="fact_sales", table_type=fact_table_type,
    )
    dim = ModelTable(
        id=_T_DIM, model_id=_MODEL, physical_name="public.dim_customer",
        alias="dim_customer", table_type="dim_detail",
    )
    cols = {
        _C_FACT_A: ModelColumn(id=_C_FACT_A, model_table_id=_T_FACT,
                               column_name="customer_key", data_type="uuid"),
        _C_FACT_B: ModelColumn(id=_C_FACT_B, model_table_id=_T_FACT,
                               column_name="bill_to_customer_key",
                               data_type="uuid"),
        _C_DIM: ModelColumn(id=_C_DIM, model_table_id=_T_DIM,
                            column_name="id", data_type="uuid"),
    }
    # The SAME dimension column reached by two different fact-side columns —
    # a dimension alias, which is a normal, supported modelling shape.
    j_low = Join(id=_J_LOW, model_id=_MODEL, left_table_id=_T_FACT,
                 left_column_id=_C_FACT_A, right_table_id=_T_DIM,
                 right_column_id=_C_DIM, join_type="inner")
    j_high = Join(id=_J_HIGH, model_id=_MODEL, left_table_id=_T_FACT,
                  left_column_id=_C_FACT_B, right_table_id=_T_DIM,
                  right_column_id=_C_DIM, join_type="inner")
    return [j_low, j_high], {_T_FACT: fact, _T_DIM: dim}, cols


def test_the_hint_does_not_depend_on_the_order_the_joins_were_read_in():
    """Same rows, two read orders, one answer.

    Reverting ``canonical_join_order`` in ``compute_redundant_partners`` makes
    the two calls disagree on ``via_join_id`` / ``partner_column_name``.
    """
    joins, tables, cols = _graph()
    forward = compute_redundant_partners(joins, tables, cols)
    reversed_read = compute_redundant_partners(list(reversed(joins)), tables, cols)

    assert set(forward) == set(reversed_read) == {_C_DIM}
    assert forward[_C_DIM] == reversed_read[_C_DIM], (
        "the redundant-partner hint changed with the order the joins were read "
        "in.\n  forward: {}\n  reversed: {}\n"
        "Both callers (measures.py / dimensions.py _load_redundant_partners) "
        "read select(Join) with no ORDER BY, so this makes the aggregate-grain "
        "400 non-deterministic for an unedited model.".format(
            forward[_C_DIM], reversed_read[_C_DIM]
        )
    )
    # Last-write-wins over the CANONICAL order, so the canonically LAST join is
    # the one that lands — pinning the answer, not merely its stability.
    assert forward[_C_DIM].via_join_id == _J_HIGH
    assert forward[_C_DIM].partner_column_name == "bill_to_customer_key"


def test_the_fact_test_is_the_shared_case_sensitive_one():
    """``_fact_side`` must not re-implement a looser fact test.

    ``graph_order.is_fact_table`` compares case-SENSITIVELY against the exact
    literal the partial unique index tests, because that index is what caps a
    model at one fact table. Reverting ``_fact_side`` to its own
    ``(table_type or "").lower() == "fact"`` makes this module call a row the
    fact table when the anchor rule, source_sql and table_resolution all do
    not — the divergence Bug-8634 closed.
    """
    joins, tables, cols = _graph(fact_table_type="Fact")
    assert compute_redundant_partners(joins, tables, cols) == {}, (
        "a table stored as 'Fact' was treated as the fact table here while "
        "graph_order.is_fact_table (and therefore the FROM anchor) does not"
    )
