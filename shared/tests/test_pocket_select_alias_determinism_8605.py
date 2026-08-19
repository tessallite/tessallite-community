"""Bug-8605 class — the pocket CTAS projection must not depend on row order.

``build_pocket_select_sql`` read every ModelColumn with no ORDER BY and
resolves duplicate column names by ARRIVAL POSITION: the first keeps the plain
name, later ones become ``{alias}_{name}``. Those names are materialised into
the pocket table, and the pocket route rewrites only the TABLE reference — the
user's column references are left verbatim. A flip therefore serves a different
table's values under the same column name, with no edit to the model.

Two tables carrying ``region_id`` is the ordinary case, not an exotic one.
Found by the round-1 deep review of the Bug-8605 fix: the same defect class as
the FROM anchor, in the same file, one function down.
"""
from __future__ import annotations

import asyncio
import uuid

from shared.db.models import Join, ModelColumn, ModelTable, PocketDefinition
from shared.semantic.sql_builder import build_pocket_select_sql

MODEL_ID = uuid.UUID(int=0xDE3)
T_A = uuid.UUID(int=0xA1)
T_B = uuid.UUID(int=0xB2)
C_A = uuid.UUID(int=0xC1)
C_B = uuid.UUID(int=0xC2)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _order_by_names(stmt) -> list[str]:
    names = []
    for clause in getattr(stmt, "_order_by_clauses", ()) or ():
        element = getattr(clause, "element", clause)
        name = getattr(element, "name", None) or getattr(element, "key", None)
        if name:
            names.append(str(name))
    return names


class _Session:
    """Honours an ORDER BY when the statement carries one, otherwise returns
    the caller's order — as an unordered SELECT legitimately may."""

    def __init__(self, columns):
        self._columns = columns
        self.seen_order_by: dict[str, list[str]] = {}
        self._tables = [
            ModelTable(id=T_A, model_id=MODEL_ID, source_id=uuid.uuid4(),
                       table_type="dim_aggregate", physical_name="dim_customer",
                       alias="dim_customer", display_name="Customer"),
            ModelTable(id=T_B, model_id=MODEL_ID, source_id=uuid.uuid4(),
                       table_type="dim_detail", physical_name="dim_region",
                       alias="dim_region", display_name="Region"),
        ]
        self._joins = [
            Join(id=uuid.UUID(int=1), model_id=MODEL_ID,
                 left_table_id=T_A, right_table_id=T_B,
                 left_column_id=C_A, right_column_id=C_B, join_type="left"),
        ]

    def _apply(self, rows, stmt, entity):
        names = _order_by_names(stmt)
        self.seen_order_by[entity] = names
        if not names:
            return rows
        return sorted(
            rows, key=lambda r: tuple(str(getattr(r, n, "")) for n in names)
        )

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"].__name__
        if entity == "ModelTable":
            return _Result(self._apply(self._tables, stmt, entity))
        if entity == "Join":
            return _Result(self._apply(self._joins, stmt, entity))
        if entity == "ModelColumn":
            return _Result(self._apply(self._columns, stmt, entity))
        return _Result([])


def _session_for(column_order):
    """Both tables carry ``region_id``."""
    by_id = {
        C_A: ModelColumn(id=C_A, model_table_id=T_A,
                         column_name="region_id", data_type="text"),
        C_B: ModelColumn(id=C_B, model_table_id=T_B,
                         column_name="region_id", data_type="text"),
    }
    return _Session([by_id[c] for c in column_order])


def _render(session):
    pocket = PocketDefinition(
        id=uuid.uuid4(), model_id=MODEL_ID,
        defining_sql="SELECT * FROM modelx",
    )
    return asyncio.run(
        build_pocket_select_sql(pocket, session, connector="postgresql")
    )


def test_pocket_projection_aliases_do_not_depend_on_column_row_order():
    first = _render(_session_for([C_A, C_B]))
    second = _render(_session_for([C_B, C_A]))
    assert first == second, (
        "the pocket's materialised column names moved with nothing but the "
        f"row order:\n  {first!r}\nvs\n  {second!r}\n"
        "The pocket route rewrites only the table reference, so the same user "
        "query then reads a different table's column under the same name."
    )


def test_the_plain_column_name_belongs_to_the_canonically_first_table():
    """Pin the VALUE: which table owns the unprefixed name is the contract."""
    for order in ([C_A, C_B], [C_B, C_A]):
        sql = _render(_session_for(order))
        assert 'base."region_id" AS "region_id"' in sql, sql
        assert 't1."region_id" AS "t1_region_id"' in sql, sql


def test_the_column_read_carries_the_canonical_order_by():
    """The database-side layer, so a streaming caller is covered too."""
    session = _session_for([C_B, C_A])
    _render(session)
    assert session.seen_order_by.get("ModelColumn") == ["model_table_id", "id"], (
        session.seen_order_by
    )
