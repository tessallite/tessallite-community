"""Bug-9980: rehydration keeps profiler estimates available to consumers."""
from __future__ import annotations

import json
from pathlib import Path
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql.dml import Insert

from shared.model_snapshot.rehydrator import _insert_tables_and_columns


def _id() -> str:
    return str(uuid.uuid4())


def _capture_session():
    db = AsyncMock()
    calls: list[tuple[str | None, dict]] = []

    async def _execute(statement):
        if isinstance(statement, Insert):
            table = getattr(getattr(statement, "table", None), "name", None)
            calls.append((table, dict(statement.compile().params)))
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    return db, calls


def _rows(calls, table: str) -> list[dict]:
    return [values for table_name, values in calls if table_name == table]


@pytest.mark.asyncio
async def test_rehydrate_projects_source_statistics_into_missing_estimates():
    table_id = _id()
    column_id = _id()
    snapshot = {
        "tables": [{
            "id": table_id,
            "source_id": _id(),
            "table_type": "fact",
            "physical_name": "payment_transaction",
            "alias": "payment_transaction",
            "display_name": "Payment transaction",
            "row_count_estimate": None,
        }],
        "columns": [{
            "id": column_id,
            "model_table_id": table_id,
            "column_name": "payment_reference",
            "data_type": "varchar",
            "is_hidden": False,
            "is_primary_key": False,
            "is_nullable": False,
            "cardinality_estimate": None,
        }],
        "source_statistics": [{
            "id": _id(),
            "model_table_id": table_id,
            "row_count": 100_000,
            "columns": [{
                "id": _id(),
                "model_column_id": column_id,
                "distinct_count": 100_000,
            }],
        }],
    }
    db, calls = _capture_session()

    await _insert_tables_and_columns(uuid.uuid4(), snapshot, db)

    assert _rows(calls, "model_tables")[0]["row_count_estimate"] == 100_000
    assert _rows(calls, "model_columns")[0]["cardinality_estimate"] == 100_000


@pytest.mark.asyncio
async def test_rehydrate_preserves_explicit_estimates_over_profiler_projection():
    table_id = _id()
    column_id = _id()
    snapshot = {
        "tables": [{
            "id": table_id,
            "source_id": _id(),
            "table_type": "fact",
            "physical_name": "facts",
            "alias": "facts",
            "display_name": "Facts",
            "row_count_estimate": 90,
        }],
        "columns": [{
            "id": column_id,
            "model_table_id": table_id,
            "column_name": "category",
            "data_type": "varchar",
            "is_hidden": False,
            "is_primary_key": False,
            "is_nullable": True,
            "cardinality_estimate": 9,
        }],
        "source_statistics": [{
            "id": _id(),
            "model_table_id": table_id,
            "row_count": 100,
            "columns": [{
                "id": _id(),
                "model_column_id": column_id,
                "distinct_count": 10,
            }],
        }],
    }
    db, calls = _capture_session()

    await _insert_tables_and_columns(uuid.uuid4(), snapshot, db)

    assert _rows(calls, "model_tables")[0]["row_count_estimate"] == 90
    assert _rows(calls, "model_columns")[0]["cardinality_estimate"] == 9


@pytest.mark.asyncio
async def test_rehydrate_without_profiler_data_keeps_estimates_unknown():
    table_id = _id()
    snapshot = {
        "tables": [{
            "id": table_id,
            "source_id": _id(),
            "table_type": "dim_detail",
            "physical_name": "customer",
            "alias": "customer",
            "display_name": "Customer",
        }],
        "columns": [],
        "source_statistics": [],
    }
    db, calls = _capture_session()

    await _insert_tables_and_columns(uuid.uuid4(), snapshot, db)

    assert _rows(calls, "model_tables")[0].get("row_count_estimate") is None


@pytest.mark.asyncio
async def test_canonical_modely_dimensions_receive_saved_profiler_estimates():
    bundle_path = Path(__file__).resolve().parents[3] / "seeds/acme-demo/project.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    modely = next(
        model for model in bundle["models"] if model["model"]["slug"] == "modely"
    )
    source_dimension_ids = {
        dimension["source_column_id"]
        for dimension in modely["dimensions"]
        if dimension.get("source_column_id")
    }
    db, calls = _capture_session()

    await _insert_tables_and_columns(uuid.uuid4(), modely, db)

    inserted_columns = {
        str(row["id"]): row for row in _rows(calls, "model_columns")
    }
    assert len(source_dimension_ids) == 101
    assert all(
        inserted_columns[column_id].get("cardinality_estimate") is not None
        for column_id in source_dimension_ids
    )
