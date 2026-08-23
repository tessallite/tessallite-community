"""Known-value source/CTAS parity guards for G3 mandatory joins."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from shared.semantic.sql_builder import build_from_clause


@dataclass
class _Table:
    id: UUID
    physical_name: str
    table_type: str
    alias: str | None = None


@dataclass
class _Column:
    id: UUID
    model_table_id: UUID
    column_name: str
    data_type: str = "integer"


@dataclass
class _Join:
    id: UUID
    left_table_id: UUID
    right_table_id: UUID
    left_column_id: UUID
    right_column_id: UUID
    join_type: str = "inner"
    population_participation: str = "preserve_base_rows"


def _db(participation: str):
    model_id = uuid4()
    fact_id, dim_id = uuid4(), uuid4()
    fact_key, dim_key = uuid4(), uuid4()
    tables = [
        _Table(fact_id, "source.fact", "fact"),
        _Table(dim_id, "source.dim", "dim"),
    ]
    columns = [
        _Column(fact_key, fact_id, "dim_id"),
        _Column(dim_key, dim_id, "id"),
    ]
    joins = [_Join(uuid4(), fact_id, dim_id, fact_key, dim_key, population_participation=participation)]

    from shared.db.models import Join, ModelColumn, ModelTable

    def _result(rows):
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    async def _execute(statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is ModelTable:
            return _result(tables)
        if entity is Join:
            return _result(joins)
        if entity is ModelColumn:
            return _result(columns)
        return _result([])

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db, model_id, fact_id, dim_id


def test_population_defining_from_clause_is_projection_independent():
    db, model_id, fact_id, dim_id = _db("population_defining")
    sql, aliases = asyncio.run(
        build_from_clause(db, model_id, needed_table_ids={fact_id})
    )
    assert '"source"."fact"' in sql
    assert '"source"."dim"' in sql
    assert dim_id in aliases


def test_non_population_join_remains_elidable():
    db, model_id, fact_id, dim_id = _db("enrichment_only")
    sql, aliases = asyncio.run(
        build_from_clause(db, model_id, needed_table_ids={fact_id})
    )
    assert '"source"."fact"' in sql
    assert '"source"."dim"' not in sql
    assert dim_id not in aliases


def test_known_count_is_invariant_across_ungrouped_fact_and_dimension_groups():
    """A filtering PD edge defines one population for every projection shape."""
    db, model_id, fact_id, _dim_id = _db("population_defining")
    from_clause, _aliases = asyncio.run(
        build_from_clause(db, model_id, needed_table_ids={fact_id})
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("ATTACH DATABASE ':memory:' AS source")
        connection.execute("CREATE TABLE source.fact (dim_id INTEGER)")
        connection.execute("CREATE TABLE source.dim (id INTEGER, name TEXT)")
        connection.executemany("INSERT INTO source.fact VALUES (?)", [(10,), (10,), (20,)])
        connection.execute("INSERT INTO source.dim VALUES (10, 'kept')")
        total = connection.execute(
            f"SELECT COUNT(*) FROM {from_clause}"
        ).fetchone()[0]
        fact_groups = connection.execute(
            f'SELECT base."dim_id", COUNT(*) FROM {from_clause} '
            'GROUP BY base."dim_id"'
        ).fetchall()
        dimension_groups = connection.execute(
            f'SELECT t1."name", COUNT(*) FROM {from_clause} '
            'GROUP BY t1."name"'
        ).fetchall()
    finally:
        connection.close()
    assert total == 2
    assert sum(row[1] for row in fact_groups) == total
    assert sum(row[1] for row in dimension_groups) == total


def test_known_count_negative_control_keeps_projection_only_elision():
    db, model_id, fact_id, _dim_id = _db("preserve_base_rows")
    from_clause, _aliases = asyncio.run(
        build_from_clause(db, model_id, needed_table_ids={fact_id})
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("ATTACH DATABASE ':memory:' AS source")
        connection.execute("CREATE TABLE source.fact (dim_id INTEGER)")
        connection.executemany("INSERT INTO source.fact VALUES (?)", [(10,), (10,), (20,)])
        total = connection.execute(
            f"SELECT COUNT(*) FROM {from_clause}"
        ).fetchone()[0]
    finally:
        connection.close()
    assert total == 3
