"""Redundant-partner analysis for aggregate picker UX.

When a ``ModelColumn`` is one side of an equi-join to a column on the
fact table, picking it for an aggregate grain is *redundant*: the
fact-side column carries the same values for every row the aggregate
will ever see. The UI renders such columns disabled with a tooltip
pointing at the canonical fact-side partner, and the API rejects
attempts to include them in a grain unless the caller explicitly
overrides with ``confirm_redundant_grain=true``.

Rule matrix (only when exactly one side of the join is a fact table):

- ``inner`` join → non-fact side is redundant.
- ``left`` join with fact on the left → right (non-fact) side is redundant.
- ``right`` join with fact on the right → left (non-fact) side is redundant.
- ``full`` outer joins → **never** mark redundant: unmatched rows on
  either side produce different grain breakdowns.
- fact-to-fact or dim-to-dim joins → never mark redundant.

The helper is pure: callers hand in already-loaded ORM rows and it
returns a dict keyed by ``ModelColumn.id``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional
from uuid import UUID

from shared.db.models import Join, ModelColumn, ModelTable


@dataclass(frozen=True)
class RedundantPartnerHint:
    """Points a dim-side join column at its canonical fact-side partner."""

    partner_column_id: UUID
    partner_column_name: str
    partner_table_id: UUID
    partner_table_name: str
    partner_physical_table: str
    via_join_id: UUID
    join_type: str

    @property
    def reason(self) -> str:
        return (
            f"Equivalent to {self.partner_physical_table}.{self.partner_column_name} "
            f"via {self.join_type} join — use the fact-side column instead."
        )


def compute_redundant_partners(
    joins: Iterable[Join],
    tables: Mapping[UUID, ModelTable],
    columns: Mapping[UUID, ModelColumn],
) -> dict[UUID, RedundantPartnerHint]:
    hints: dict[UUID, RedundantPartnerHint] = {}
    for j in joins:
        lt = tables.get(j.left_table_id)
        rt = tables.get(j.right_table_id)
        lc = columns.get(j.left_column_id)
        rc = columns.get(j.right_column_id)
        if not (lt and rt and lc and rc):
            continue

        fact_side = _fact_side(lt, rt)
        if fact_side is None:
            continue

        join_type = (j.join_type or "").lower()
        # Legacy values like "many_to_one" / "one_to_many" describe
        # cardinality rather than SQL semantics; treat them as inner.
        if join_type in ("many_to_one", "one_to_many", "one_to_one", "many_to_many"):
            join_type = "inner"

        dim_col: Optional[ModelColumn] = None
        if join_type == "inner":
            dim_col = rc if fact_side == "left" else lc
        elif join_type == "left" and fact_side == "left":
            dim_col = rc
        elif join_type == "right" and fact_side == "right":
            dim_col = lc
        else:
            # full, or outer with fact on the non-anchored side — skip.
            continue

        if dim_col is None:
            continue

        fact_col = lc if fact_side == "left" else rc
        fact_table = lt if fact_side == "left" else rt
        hints[dim_col.id] = RedundantPartnerHint(
            partner_column_id=fact_col.id,
            partner_column_name=fact_col.column_name,
            partner_table_id=fact_table.id,
            partner_table_name=_short_table_name(fact_table),
            partner_physical_table=fact_table.physical_name or _short_table_name(fact_table),
            via_join_id=j.id,
            join_type=join_type,
        )

    return hints


def _fact_side(left: ModelTable, right: ModelTable) -> Optional[str]:
    left_is_fact = (left.table_type or "").lower() == "fact"
    right_is_fact = (right.table_type or "").lower() == "fact"
    if left_is_fact and not right_is_fact:
        return "left"
    if right_is_fact and not left_is_fact:
        return "right"
    return None


def _short_table_name(table: ModelTable) -> str:
    raw = table.physical_name or ""
    return raw.split(".")[-1] if raw else str(table.id)
