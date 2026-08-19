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
- **Every other join → never mark redundant (Bug-8647).**
- fact-to-fact or dim-to-dim joins → never mark redundant.

Why only ``inner`` (Bug-8647)
-----------------------------
The claim this module makes is that the dimension-side key carries the same
value as the fact-side key on every row the aggregate will see. That is true
only when the join discards unmatched rows. Any OUTER join keeps them: a fact
row with no matching dimension row keeps its own populated key and gets NULL
on the dimension side, so ``GROUP BY dim.key`` produces a NULL bucket that
``GROUP BY fact.key`` does not — different rows, different totals. ``full``
was always excluded for exactly this reason; ``left`` and ``right`` carry the
same exposure in one direction and were wrongly admitted.

This supersedes the reversed-orientation request tracked in the v6 archive as
its Bug-8713 (emit a hint for a declared ``left`` join whose fact is the
modeller's RIGHT table). That shape preserves the DIMENSION side, so its
dimension key is precisely the one whose NULL behaviour differs — it must not
be called redundant either. No outer join emits a hint at all now, so that
request is moot rather than deferred.

One join vocabulary
-------------------
Orientation is classified through :func:`shared.semantic.join_keyword.split_join_token`,
the same classifier the SQL builders render from. This module previously kept
its own token table that folded legacy CARDINALITY tokens (``many_to_one`` and
friends) onto ``inner`` — while ``join_keyword`` renders every one of them as
an un-flipped ``LEFT JOIN`` (its invariant 4). The executed SQL was therefore
an outer join while the redundancy claim was granted as if it were inner, which
is the same wrong-numbers defect reaching through the legacy-token path. Those
tokens are still reachable after migration ``0194``: ``model_snapshot/rehydrator``
writes an imported bundle's ``join_type`` verbatim (Bug-8702). An unrecognised
token also renders as ``LEFT JOIN``, and ``split_join_token`` returns ``None``
for it, so it likewise emits no hint.

The guardrail fails OPEN, not wrong: a column that no longer earns a hint stays
visible in the XMLA catalogue and pickable in the grain picker.

The helper is pure: callers hand in already-loaded ORM rows and it
returns a dict keyed by ``ModelColumn.id``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional
from uuid import UUID

from shared.db.models import Join, ModelColumn, ModelTable
from shared.semantic.graph_order import canonical_join_order, is_fact_table
from shared.semantic.join_keyword import split_join_token

#: The one orientation whose rows are all matched rows, so the two key columns
#: provably carry identical values. Spelled once; every other token — outer,
#: legacy cardinality, or unrecognised — is refused by comparison against it.
_REDUNDANCY_SAFE_JOIN_TYPE = "inner"


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
    # Bug-8605 round-3 review (finding 3): canonical join order. ``hints`` is
    # keyed by dimension column and written last-write-wins, so a dimension
    # column joined to the fact table more than once took whichever join the
    # unordered read returned last — the same grain could be accepted on one
    # request and rejected on the next with no edit. Not a wrong number (no SQL
    # is generated from this), but a non-deterministic user-facing 400.
    for j in canonical_join_order(joins):
        lt = tables.get(j.left_table_id)
        rt = tables.get(j.right_table_id)
        lc = columns.get(j.left_column_id)
        rc = columns.get(j.right_column_id)
        if not (lt and rt and lc and rc):
            continue

        fact_side = _fact_side(lt, rt)
        if fact_side is None:
            continue

        # Bug-8647: classify through the SAME vocabulary the SQL builders
        # render from, so this module cannot claim "inner" for an edge that
        # executes as an outer join. A legacy cardinality token resolves to
        # its inferred orientation (``many_to_one`` -> ``left``); an
        # unrecognised token resolves to None, which join_keyword renders as
        # an un-flipped LEFT JOIN. Neither is inner, so neither earns a hint.
        join_type, _cardinality = split_join_token(j.join_type)
        if join_type != _REDUNDANCY_SAFE_JOIN_TYPE:
            continue

        dim_col: ModelColumn = rc if fact_side == "left" else lc
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
    # Bug-8605 round-3 review (finding 3): the ONE shared fact test, so this
    # cannot disagree with the anchor rule about which table is the fact table.
    left_is_fact = is_fact_table(left)
    right_is_fact = is_fact_table(right)
    if left_is_fact and not right_is_fact:
        return "left"
    if right_is_fact and not left_is_fact:
        return "right"
    return None


def _short_table_name(table: ModelTable) -> str:
    raw = table.physical_name or ""
    return raw.split(".")[-1] if raw else str(table.id)
