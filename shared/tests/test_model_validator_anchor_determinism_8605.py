"""Bug-8605 seventh site — the validator's reachability anchor must be a pure
function of the rows.

``_load_model_structure`` picked ``facts[0] if facts else
next(iter(tables.values()))`` over an unordered ``select(ModelTable)`` and then
flood-filled ``reachable_from_anchor``. On a zero-fact model with a
DISCONNECTED join graph the reachable set depends on which component the anchor
lands in, so dimensions, measures and aggregates flip between valid and invalid
with no edit at all — and unlike the CTAS builders, this verdict is PERSISTED
(``is_invalid`` / ``invalid_reason`` on the row, surfaced in Model Health) and
gates whether each aggregate is allowed to refresh and serve.

Found by the round-1 deep review of the Bug-8605 fix: the first pass enumerated
six call sites of the primitive and missed this one, which is in the same
package as the shared rule.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from shared.db.models import Dimension, Join, ModelColumn, ModelTable
from shared.semantic.model_validator import _load_model_structure, validate_dimension

MODEL_ID = uuid.UUID(int=0xDE2)
# Fixed ids: canonical order IS id order, so random values would make which
# table anchors vary between runs.
T_CUST = uuid.UUID(int=0x10)
T_REGION = uuid.UUID(int=0x20)
T_STAGING = uuid.UUID(int=0x30)
C_CUST = uuid.UUID(int=0x11)
C_REGION = uuid.UUID(int=0x21)
C_STAGING = uuid.UUID(int=0x31)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    """Returns rows in a caller-chosen order, as an unordered SELECT may."""

    def __init__(self, tables):
        self._tables = tables

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"].__name__
        if entity == "ModelTable":
            return _Result(self._tables)
        if entity == "ModelColumn":
            return _Result([
                ModelColumn(id=C_CUST, model_table_id=T_CUST,
                            column_name="k", data_type="text"),
                ModelColumn(id=C_REGION, model_table_id=T_REGION,
                            column_name="k", data_type="text"),
                ModelColumn(id=C_STAGING, model_table_id=T_STAGING,
                            column_name="k", data_type="text"),
            ])
        if entity == "Join":
            # dim_customer <-> dim_region only. staging_customer is UNJOINED,
            # which is what makes the reachable set anchor-dependent.
            return _Result([
                Join(id=uuid.UUID(int=1), model_id=MODEL_ID,
                     left_table_id=T_CUST, right_table_id=T_REGION,
                     left_column_id=C_CUST, right_column_id=C_REGION,
                     join_type="left",
                     created_at=datetime(2026, 1, 9, tzinfo=timezone.utc)),
            ])
        return _Result([])


def _tables():
    def _t(tid, name):
        return ModelTable(
            id=tid, model_id=MODEL_ID, source_id=uuid.uuid4(),
            table_type="dim_detail", physical_name=name, alias=name,
            display_name=name,
        )
    return {
        T_CUST: _t(T_CUST, "dim_customer"),
        T_REGION: _t(T_REGION, "dim_region"),
        T_STAGING: _t(T_STAGING, "staging_customer"),
    }


def _structure_for(order):
    by_id = _tables()
    return asyncio.run(
        _load_model_structure(MODEL_ID, _Session([by_id[t] for t in order]))
    )


def test_reachability_anchor_does_not_depend_on_row_order():
    """Same rows, two legal orders, one reachable set."""
    first = _structure_for([T_CUST, T_REGION, T_STAGING])
    second = _structure_for([T_STAGING, T_REGION, T_CUST])
    assert first.anchor_id == second.anchor_id, (
        "the validator's anchor moved with nothing but the row order"
    )
    assert first.reachable_from_anchor == second.reachable_from_anchor, (
        "reachable_from_anchor moved with the row order; dimensions and "
        "measures on the losing component get is_invalid=true written to the "
        "database on one run and not the next"
    )


def test_the_anchor_is_the_canonically_first_table():
    """Pin the VALUE: the lowest-id table, matching every other builder."""
    for order in ([T_CUST, T_REGION, T_STAGING],
                  [T_STAGING, T_REGION, T_CUST],
                  [T_REGION, T_STAGING, T_CUST]):
        assert _structure_for(order).anchor_id == T_CUST


def test_dimension_validity_does_not_depend_on_row_order():
    """Pin the user-visible outcome, not just the internal set."""
    dim = Dimension(id=uuid.uuid4(), model_id=MODEL_ID, name="region",
                    source_column_id=C_REGION)
    verdicts = {
        validate_dimension(dim, _structure_for(order))
        for order in ([T_CUST, T_REGION, T_STAGING],
                      [T_STAGING, T_REGION, T_CUST])
    }
    assert len(verdicts) == 1, (
        f"the same dimension is valid on one row order and invalid on another: "
        f"{verdicts}"
    )
