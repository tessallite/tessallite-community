"""Canonical enumeration order for a model's physical graph (Bug-8605).

Why this module exists
----------------------
Several builders turn a model's ``ModelTable`` / ``Join`` / ``ModelColumn`` rows
into physical SQL, and every one of them makes at least one **positional**
decision:

* the FROM/JOIN **anchor** — ``facts[0] if facts else <first table>`` for
  incomplete drafts and legacy rows;
* the **BFS expansion order**, which decides both the ``t1/t2/...`` alias
  numbering and, when a join graph has two equally short paths between the
  anchor and a needed table, *which intermediate tables end up in the FROM
  clause at all*;
* the **projection alias** a pocket CTAS materialises when two tables carry the
  same column name — first arrival keeps the plain name, later ones get an
  alias prefix, and those names are the pocket's contract with every query
  written against it.

Those decisions were being made over the result of a ``select(...)`` carrying
no ``ORDER BY``. SQL does not promise an order for such a read, and PostgreSQL
genuinely varies it: round-3 review measured an index scan returning
``dim_customer`` first and a sequential scan of the same rows returning
``dim_region`` first, with a routine ``VACUUM FULL`` making the second order
permanent. Because a LEFT JOIN preserves the BASE relation's rows, the anchor
flip changed the materialised totals of an entirely unedited model —
``COUNT(*)=2, SUM=350.00`` versus ``COUNT(*)=1, SUM=100.00``.

The deployed-definition closure (Bug-8250) structurally cannot catch this: it
compares row SETS between the live graph and the deployed snapshot, and this is
a property of the ORDER of the read that produced them. Every row is identical
on both sides, so no drift is reported and the artifact is stamped
version-compatible.

The rule
--------
**Canonical order is ``id``.** Tables and joins sort by ``id``; columns sort by
``(model_table_id, id)`` so a table's columns stay grouped.

**The anchor is the fact table if the model has one, otherwise the first table
in canonical order for incomplete drafts and legacy rows.** At most one fact
table per model is enforced at the storage layer
(``uq_model_tables_one_fact_per_model``, migration 0136), so the fact branch is
unique by construction. Deploy/import validation rejects multi-table zero-fact
models under Bug-8614 Option 1; the canonical fallback is not a deployable-
model contract.

Why ``id`` and not ``(created_at, id)``
---------------------------------------
The first version of this module ordered by ``(created_at, id)``, to match the
order ``shared/model_snapshot/serialiser.py`` writes rows into the deployed
snapshot. Round-1 deep review disproved that by execution on PostgreSQL 15, and
the reasoning is worth keeping because it is the trap any future change here
will fall into:

``rehydrate_into_live`` (revert-to-version, and project import) DELETEs every
``ModelTable``/``Join`` row and re-INSERTs it from the snapshot.
``_strip_pk_and_uuids`` carries the ``id`` through verbatim, but the snapshot
row bodies do not contain ``created_at`` — the serialiser excludes it — so the
re-inserted rows fall back to ``server_default=func.now()``. PostgreSQL's
``now()`` is the TRANSACTION timestamp, so every re-inserted row gets the SAME
value and any ``created_at`` ordering collapses. Measured: a model authored as
``dim_customer -> dim_region -> dim_product`` came back as
``dim_region -> dim_product -> dim_customer``. The revert then points
``deployed_version_id`` at the OLD, unchanged snapshot, so the router keeps
anchoring on ``dim_customer`` while every CTAS rebuilt from live rows anchors on
``dim_region`` — and ``compare_closures`` reports nothing, because ``created_at``
is not a field the snapshot carries and therefore not a field it can compare.

``id`` has none of that exposure. It is present on live rows, carried verbatim
in the snapshot, preserved verbatim by rehydration, and immutable for the life
of a row. The live graph, the deployed snapshot and a rehydrated live graph
therefore enumerate identically **by construction**, with no dependence on a
field that gets dropped or regenerated. The ordering is arbitrary from a
business point of view — so was creation order — and the fallback remains only
for incomplete drafts and legacy rows; deployable models must declare exactly
one fact table under Bug-8614 Option 1.

A consequence worth stating: because ``id`` is a random UUID, a table added to
an incomplete zero-fact draft CAN sort ahead of the existing ones and take the
legacy fallback anchor. That is not a deploy hole — ``fact_anchor_violation``
rejects a multi-table zero-fact model before deployment, and
``definition_closure._fact_anchor_additions`` reports every live-only table as
anchor-moving drift when an older deployed set has no fact table, so refresh is
refused before it can build on the new anchor.

Two layers, on purpose
----------------------
1. ``select_model_tables`` / ``select_model_joins`` / ``select_model_columns``
   build the read WITH the ``ORDER BY`` already attached, so the database
   returns canonical order and no caller has to remember to sort.
2. ``canonical_table_order`` / ``canonical_join_order`` /
   ``canonical_column_order`` / ``pick_anchor_table`` re-sort in Python, so the
   choice is a pure function of the ROWS and does not depend on how they were
   obtained — an unordered read, a process cache, a snapshot-hydrated graph, or
   a future caller that forgets layer 1.

Layer 2 sorts UNCONDITIONALLY. The earlier version skipped the sort when a row
lacked ``created_at``, which is the snapshot-hydrated shape; that special case
is gone along with the key that needed it, so there is no shape in which this
module quietly stops applying.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable, TypeVar

from sqlalchemy import Select, select

from shared.db.models import Join, ModelColumn, ModelTable

__all__ = [
    "CANONICAL_ORDER_DESCRIPTION",
    "FACT_TABLE_TYPE",
    "MODEL_COLUMN_ORDER",
    "MODEL_JOIN_ORDER",
    "MODEL_TABLE_ORDER",
    "anchor_is_by_convention",
    "canonical_column_order",
    "canonical_join_order",
    "canonical_table_order",
    "fact_anchor_violation",
    "is_fact_table",
    "order_model_columns",
    "order_model_joins",
    "order_model_tables",
    "pick_anchor_table",
    "select_model_columns",
    "select_model_joins",
    "select_model_tables",
]

CANONICAL_ORDER_DESCRIPTION = "id"

#: The canonical ORDER BY columns, exported so a producer that cannot use the
#: ``order_model_*`` helpers (the snapshot serialiser builds its statements
#: differently) still spells the key in exactly ONE place. Round-3 review
#: caught the serialiser copying ``.order_by(ModelTable.id)`` inline: the key
#: had then moved once already in this lane, and a copy is what lets the
#: producer and the builders silently drift apart again.
MODEL_TABLE_ORDER = (ModelTable.id,)
MODEL_JOIN_ORDER = (Join.id,)
MODEL_COLUMN_ORDER = (ModelColumn.model_table_id, ModelColumn.id)

#: The exact string the storage layer's partial unique index tests
#: (``postgresql_where=text("table_type = 'fact'")``).
FACT_TABLE_TYPE = "fact"

_T = TypeVar("_T")


def is_fact_table(row: Any) -> bool:
    """Is this row the model's fact table?

    ONE fact test for the whole codebase, for the same reason there is one
    ordering rule. Round-2 review found the two drifting apart within a single
    commit: ``pick_anchor_table`` was made case-sensitive to match the read
    path while ``definition_closure._fact_anchor_additions`` still lowercased,
    so a table stored as ``"Fact"`` was a fact table to the drift guard and NOT
    a fact table to the anchor rule. The guard then skipped a live-only
    addition that provably DID take the anchor — Bug-8600's fail-open,
    re-opened by a fix.

    Compared case-SENSITIVELY, matching the partial unique index's own SQL
    literal. The index is what actually caps a model at one fact table, so a
    looser match here would let this module call a row the fact table when the
    storage layer, ``source_sql`` and ``table_resolution`` all do not.

    Accepts an ORM row or a snapshot dict, because the drift closure compares
    serialised dicts while the builders hold ORM instances.
    """
    if isinstance(row, Mapping):
        value = row.get("table_type")
    else:
        value = getattr(row, "table_type", None)
    return value == FACT_TABLE_TYPE


def fact_anchor_violation(tables: Iterable[Any]) -> str | None:
    """Return a deploy-time error for an invalid model population anchor.

    A single-table model is implicitly its own fact table.  Multi-table models
    must instead carry exactly one explicitly declared ``table_type='fact'``
    row; canonical ordering remains the fallback only for callers handling
    incomplete drafts, never for a deployable model.  This helper accepts ORM
    and snapshot rows so deploy, import, and rehydrate cannot drift.
    """
    rows = list(tables)
    if len(rows) <= 1:
        return None
    facts = [row for row in rows if is_fact_table(row)]
    if len(facts) == 1:
        return None
    names = [
        str(
            row.get("physical_name") or row.get("alias") or "?"
            if isinstance(row, Mapping)
            else getattr(row, "physical_name", None)
            or getattr(row, "alias", None)
            or "?"
        )
        for row in rows
    ]
    if not facts:
        return (
            "a multi-table model must declare exactly one fact table; "
            f"none is declared ({', '.join(names)})."
        )
    fact_names = [
        str(
            row.get("physical_name") or row.get("alias") or "?"
            if isinstance(row, Mapping)
            else getattr(row, "physical_name", None)
            or getattr(row, "alias", None)
            or "?"
        )
        for row in facts
    ]
    return (
        "a multi-table model must declare exactly one fact table; "
        f"{len(facts)} are declared ({', '.join(fact_names)})."
    )


def order_model_tables(stmt: Select) -> Select:
    """Attach the canonical ``ORDER BY`` to a ``ModelTable`` select."""
    return stmt.order_by(*MODEL_TABLE_ORDER)


def order_model_joins(stmt: Select) -> Select:
    """Attach the canonical ``ORDER BY`` to a ``Join`` select."""
    return stmt.order_by(*MODEL_JOIN_ORDER)


def order_model_columns(stmt: Select) -> Select:
    """Attach the canonical ``ORDER BY`` to a ``ModelColumn`` select.

    Grouped by table first so a table's columns stay together, which is also
    how the snapshot serialiser writes them.
    """
    return stmt.order_by(*MODEL_COLUMN_ORDER)


def select_model_tables(model_id: Any) -> Select:
    """Canonically ordered read of a model's ``ModelTable`` rows."""
    return order_model_tables(
        select(ModelTable).where(ModelTable.model_id == model_id)
    )


def select_model_joins(model_id: Any) -> Select:
    """Canonically ordered read of a model's ``Join`` rows."""
    return order_model_joins(select(Join).where(Join.model_id == model_id))


def select_model_columns(model_id: Any) -> Select:
    """Canonically ordered read of every ``ModelColumn`` in a model."""
    return order_model_columns(
        select(ModelColumn)
        .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
        .where(ModelTable.model_id == model_id)
    )


def _id_key(row: Any) -> str:
    # ``str(id)`` rather than the raw value because snapshot-hydrated rows can
    # carry the id as text while live rows carry a UUID. Canonical UUID text
    # sorts identically to PostgreSQL's bytewise ``uuid`` ordering (lowercase
    # hex, dashes at fixed positions), so the Python sort and the SQL
    # ``ORDER BY`` agree.
    return str(getattr(row, "id", ""))


def canonical_table_order(tables: Iterable[_T]) -> list[_T]:
    """Return ``tables`` in canonical ``id`` order."""
    return sorted(tables, key=_id_key)


def canonical_join_order(joins: Iterable[_T]) -> list[_T]:
    """Return ``joins`` in canonical ``id`` order.

    Join order is load-bearing, not cosmetic: it drives BFS expansion, which
    fixes the ``t1/t2`` alias numbering and breaks ties between equally short
    anchor-to-table paths — and a different tie-break puts different
    intermediate tables in the FROM clause, changing row multiplicity.
    """
    return sorted(joins, key=_id_key)


def canonical_column_order(columns: Iterable[_T]) -> list[_T]:
    """Return ``columns`` in canonical ``(model_table_id, id)`` order.

    A pocket CTAS resolves duplicate column names by arrival position, and the
    resulting names are materialised into the pocket table, so an unordered
    read makes the pocket's column contract depend on the storage engine.
    """
    return sorted(
        columns,
        key=lambda c: (str(getattr(c, "model_table_id", "")), _id_key(c)),
    )


def pick_anchor_table(tables: Iterable[Any]) -> Any | None:
    """The model's FROM/JOIN anchor, as a pure function of the rows.

    The fact table when the model has one (the storage layer allows at most
    one), otherwise the first table in canonical ``id`` order for incomplete
    drafts and legacy rows. Deploy/import validation rejects multi-table
    zero-fact models. Returns ``None`` for an empty input so callers can raise
    their own, context-specific error.
    """
    ordered = canonical_table_order(tables)
    if not ordered:
        return None
    for table in ordered:
        if is_fact_table(table):
            return table
    return ordered[0]


def anchor_is_by_convention(tables: Iterable[Any]) -> bool:
    """True when the anchor is a platform convention rather than a modelling act.

    An incomplete draft or legacy row set with no fact table and more than one
    table has no metadata that says which table the FROM clause should be built
    around, so the anchor is decided by ``pick_anchor_table``'s ordering rule.
    Deploy/import validation rejects that shape for a deployable model; callers
    handling the fallback log it so a surprising draft/legacy total is
    explainable.
    """
    rows = list(tables)
    if len(rows) < 2:
        return False
    return not any(is_fact_table(t) for t in rows)
