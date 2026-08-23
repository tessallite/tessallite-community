"""Bug-8932 / Bug-8950 — rehydrate/import membership checks for cross-model FKs.

The rehydrator pours import-bundle rows straight into the live tables, bypassing
the CRUD API's ownership guards (ensure_target_in_model / _validate_attachment_
target / the Bug-8878 calendar-binding guard). Four tenant-schema-wide foreign
keys therefore travelled verbatim from a hand-edited or cross-project bundle into
the DB, where the FK constraint (satisfied by ANY tenant row) does not catch a
foreign-model reference:

  * ``model_tables.calendar_table_id``     (Bug-8932)
  * ``kpis.time_dimension_id``             (Bug-8950)
  * ``models.target_id``                   (Bug-8950)
  * ``data_quality_rules.target_id``       (Bug-8950, polymorphic column ref)

The fix mirrors the established dangling-drop precedent (Bug-6725 hierarchies,
revert_kpi_version): the ONLY valid post-rehydrate ids are the snapshot's own
rows (only those are (re)inserted for this model), so a reference absent from
that set is dropped to NULL — or, where the column is NOT NULL
(data_quality_rules.target_id), the whole rule is skipped. A self-consistent
same-model snapshot always passes, so a legitimate revert/export never loses a
reference.
"""
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.sql.dml import Insert, Update

from shared.db.models import DataQualityRule, KPI, ModelTable
from shared.model_snapshot import rehydrator
from shared.model_snapshot.rehydrator import (
    RehydrationMode,
    _insert_data_quality_rules,
    _insert_kpis,
    _insert_tables_and_columns,
    rehydrate_into_live,
)


def _u() -> str:
    return str(uuid.uuid4())


def _capture():
    """Mock AsyncSession recording (table_name, compiled-params) per DML call."""
    db = AsyncMock()
    calls: list[tuple[str | None, dict]] = []

    async def _execute(stmt):
        if isinstance(stmt, (Insert, Update)):
            tbl = getattr(getattr(stmt, "table", None), "name", None)
            # Compile is NOT guarded: a test row that names a non-existent
            # column must fail loudly here, not be silently swallowed.
            calls.append((tbl, dict(stmt.compile().params)))
        res = MagicMock()
        res.all.return_value = []
        res.first.return_value = None
        scalars = MagicMock()
        scalars.all.return_value = []
        scalars.first.return_value = None
        res.scalars.return_value = scalars
        return res

    db.execute = AsyncMock(side_effect=_execute)
    db._calls = calls
    return db


def _rows_for(db, table_name: str) -> list[dict]:
    return [params for tbl, params in db._calls if tbl == table_name]


# ---------------------------------------------------------------------------
# Bug-8932 — model_tables.calendar_table_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug8932_drops_foreign_calendar_table_id(caplog):
    model_id = uuid.uuid4()
    valid_cal = _u()
    foreign_cal = _u()  # a calendar table absent from THIS snapshot
    snap = {
        "calendar_tables": [{"id": valid_cal, "table_name": "cal"}],
        "tables": [
            {"id": _u(), "display_name": "fact_valid", "calendar_table_id": valid_cal},
            {"id": _u(), "display_name": "fact_foreign", "calendar_table_id": foreign_cal},
        ],
        "columns": [],
    }
    db = _capture()
    with caplog.at_level(logging.WARNING):
        await _insert_tables_and_columns(model_id, snap, db)

    by_name = {r.get("display_name"): r for r in _rows_for(db, ModelTable.__tablename__)}
    # The in-model binding is preserved verbatim.
    assert by_name["fact_valid"].get("calendar_table_id") == uuid.UUID(valid_cal)
    # The cross-model binding is dropped (absent from the insert -> SQL NULL).
    assert by_name["fact_foreign"].get("calendar_table_id") is None
    assert any("Bug-8932" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_bug8932_null_calendar_table_id_passes_through():
    """A table with no calendar binding is untouched (no spurious warning)."""
    model_id = uuid.uuid4()
    snap = {
        "calendar_tables": [],
        "tables": [{"id": _u(), "display_name": "plain_fact"}],
        "columns": [],
    }
    db = _capture()
    await _insert_tables_and_columns(model_id, snap, db)
    rows = _rows_for(db, ModelTable.__tablename__)
    assert len(rows) == 1
    assert rows[0].get("calendar_table_id") is None


# ---------------------------------------------------------------------------
# Bug-8950 — kpis.time_dimension_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug8950_drops_foreign_kpi_time_dimension_id(caplog):
    model_id = uuid.uuid4()
    valid_dim = _u()
    foreign_dim = _u()
    snap = {
        "dimensions": [{"id": valid_dim, "name": "Date"}],
        "kpis": [
            {"id": _u(), "name": "kpi_valid", "time_dimension_id": valid_dim},
            {"id": _u(), "name": "kpi_foreign", "time_dimension_id": foreign_dim},
        ],
    }
    db = _capture()
    with caplog.at_level(logging.WARNING):
        await _insert_kpis(model_id, snap, db)

    by_name = {r.get("name"): r for r in _rows_for(db, KPI.__tablename__)}
    assert by_name["kpi_valid"].get("time_dimension_id") == uuid.UUID(valid_dim)
    # Dangling ref dropped: None-stripped out of the insert -> DB NULL default.
    assert by_name["kpi_foreign"].get("time_dimension_id") is None
    assert any("Bug-8950" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "field", ["value_measure_id", "goal_measure_id", "target_measure_id"],
)
@pytest.mark.asyncio
async def test_bug8950_drops_foreign_kpi_measure_ids(field, caplog):
    """L9-F2: the three sibling ``measures.id`` FKs on the SAME KPI row.

    ``time_dimension_id`` was guarded alone, but a KPI's value / goal / target
    measure FKs are the identical exposure on the identical import path: they
    are nullable, tenant-schema-wide, refused cross-model by the CRUD route
    (api/kpis.py), and satisfied by ANY tenant row at the DB constraint. None
    is inert — ``target_measure_id`` drives target evaluation, and the legacy
    value/goal pair are walked as real references by dependency resolution,
    lineage derivation and governance export — so a foreign one pulls another
    model's measure into this model's answers. Fails before the guard loop
    covered all four FKs.
    """
    model_id = uuid.uuid4()
    valid_measure = _u()
    foreign_measure = _u()
    snap = {
        "dimensions": [],
        "measures": [{"id": valid_measure, "name": "Revenue"}],
        "kpis": [
            {"id": _u(), "name": "kpi_valid", field: valid_measure},
            {"id": _u(), "name": "kpi_foreign", field: foreign_measure},
        ],
    }
    db = _capture()
    with caplog.at_level(logging.WARNING):
        await _insert_kpis(model_id, snap, db)

    by_name = {r.get("name"): r for r in _rows_for(db, KPI.__tablename__)}
    assert by_name["kpi_valid"].get(field) == uuid.UUID(valid_measure)
    assert by_name["kpi_foreign"].get(field) is None
    assert any(
        "Bug-8950" in r.getMessage() and field in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_bug8950_drops_every_foreign_kpi_fk_on_one_row(caplog):
    """All four FKs dangling on ONE KPI are ALL dropped in a single pass.

    Guards the loop itself: a per-field ``if`` chain that rebinds ``k`` can drop
    an earlier field's correction if the copies are not chained, so a row with
    every FK foreign is the case that proves the accumulation.
    """
    model_id = uuid.uuid4()
    snap = {
        "dimensions": [{"id": _u(), "name": "Date"}],
        "measures": [{"id": _u(), "name": "Revenue"}],
        "kpis": [
            {
                "id": _u(), "name": "kpi_all_foreign",
                "time_dimension_id": _u(),
                "value_measure_id": _u(),
                "goal_measure_id": _u(),
                "target_measure_id": _u(),
            },
        ],
    }
    db = _capture()
    with caplog.at_level(logging.WARNING):
        await _insert_kpis(model_id, snap, db)

    row = _rows_for(db, KPI.__tablename__)[0]
    for field in (
        "time_dimension_id", "value_measure_id",
        "goal_measure_id", "target_measure_id",
    ):
        assert row.get(field) is None, f"{field} survived the membership drop"


@pytest.mark.asyncio
async def test_bug8950_keeps_in_model_kpi_fks():
    """A self-consistent snapshot keeps all four references (no false drop)."""
    model_id = uuid.uuid4()
    dim_id = _u()
    measure_id = _u()
    snap = {
        "dimensions": [{"id": dim_id, "name": "Date"}],
        "measures": [{"id": measure_id, "name": "Revenue"}],
        "kpis": [
            {
                "id": _u(), "name": "kpi_in_model",
                "time_dimension_id": dim_id,
                "value_measure_id": measure_id,
                "goal_measure_id": measure_id,
                "target_measure_id": measure_id,
            },
        ],
    }
    db = _capture()
    await _insert_kpis(model_id, snap, db)

    row = _rows_for(db, KPI.__tablename__)[0]
    assert row.get("time_dimension_id") == uuid.UUID(dim_id)
    for field in ("value_measure_id", "goal_measure_id", "target_measure_id"):
        assert row.get(field) == uuid.UUID(measure_id)


# ---------------------------------------------------------------------------
# Bug-8950 — data_quality_rules.target_id (polymorphic, NOT NULL -> skip rule)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug8950_skips_foreign_data_quality_rule(caplog):
    model_id = uuid.uuid4()
    valid_col = _u()
    foreign_col = _u()
    snap = {
        "columns": [{"id": valid_col, "column_name": "amount"}],
        "dimensions": [],
        "measures": [],
        "data_quality_rules": [
            {
                "id": _u(), "name": "rule_valid", "target_type": "column",
                "target_id": valid_col, "rule_type": "not_null",
            },
            {
                "id": _u(), "name": "rule_foreign", "target_type": "column",
                "target_id": foreign_col, "rule_type": "not_null",
            },
        ],
    }
    db = _capture()
    with caplog.at_level(logging.WARNING):
        await _insert_data_quality_rules(model_id, snap, db)

    inserted = {r.get("name") for r in _rows_for(db, DataQualityRule.__tablename__)}
    assert "rule_valid" in inserted
    # The rule whose column is not in the snapshot is skipped entirely, so
    # validator.py can never dereference a foreign column on a service token.
    assert "rule_foreign" not in inserted
    assert any("Bug-8950" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_bug8950_skips_unknown_target_type_data_quality_rule(caplog):
    """An unrecognised target_type fails closed (skip), never inserted blind."""
    model_id = uuid.uuid4()
    col = _u()
    snap = {
        "columns": [{"id": col, "column_name": "amount"}],
        "dimensions": [],
        "measures": [],
        "data_quality_rules": [
            {
                "id": _u(), "name": "rule_weird", "target_type": "galaxy",
                "target_id": col, "rule_type": "not_null",
            },
        ],
    }
    db = _capture()
    with caplog.at_level(logging.WARNING):
        await _insert_data_quality_rules(model_id, snap, db)
    assert not _rows_for(db, DataQualityRule.__tablename__)


# ---------------------------------------------------------------------------
# Bug-8950 — models.target_id (inline model-scalar restore in rehydrate_into_live)
# ---------------------------------------------------------------------------

_NEUTRALISED_HELPERS = [
    "_truncate_model_children", "_insert_data_sources_and_targets",
    "_insert_calendar_tables", "_insert_tables_and_columns", "_insert_udas",
    "_insert_joins", "_insert_hierarchies", "_insert_dimensions",
    "_synthesize_missing_hierarchy_dimensions", "_insert_attribute_relationships",
    "_backfill_dimension_provenance", "_insert_measures", "_insert_named_sets",
    "_insert_kpis", "_insert_drill_through_sets", "_insert_aggregate_lifecycle",
    "_insert_personas", "_insert_pockets", "_insert_data_tags",
    "_insert_row_security", "_insert_glossary", "_insert_source_statistics",
    "_insert_source_join_statistics", "_insert_ai_scheduler", "_insert_lineage",
    "_insert_model_parameters", "_insert_model_alias_map",
    "_insert_refresh_sla_config", "_insert_data_quality_rules",
    "_insert_entity_translations", "_invalidate_borrowing_models_on_source_repoint",
    "_validate_preserved_aggregates", "_validate_preserved_pockets",
]


def _empty_result() -> MagicMock:
    result = MagicMock()
    result.all.return_value = []
    result.first.return_value = None
    scalars = MagicMock()
    scalars.all.return_value = []
    scalars.first.return_value = None
    result.scalars.return_value = scalars
    return result


async def _run_rehydrate(snapshot) -> dict:
    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(seed="destination-seed"))
    db.execute = AsyncMock(return_value=_empty_result())
    db.add = MagicMock()
    patches = {
        name: AsyncMock(return_value=[] if name == "_insert_aggregates" else None)
        for name in _NEUTRALISED_HELPERS
    }
    patches["_insert_aggregates"] = AsyncMock(return_value=[])
    with patch.multiple(rehydrator, **patches):
        await rehydrate_into_live(
            uuid.uuid4(), snapshot, db,
            mode=RehydrationMode.IMPORT,
            preserve_aggregates=False,
            preserve_pockets=False,
        )
    updates = [
        call.args[0]
        for call in db.execute.await_args_list
        if getattr(call.args[0], "is_update", False)
    ]
    assert updates, "rehydration did not issue the model scalar update"
    return dict(updates[-1].compile().params)


@pytest.mark.asyncio
async def test_bug8950_drops_foreign_model_target_id(caplog):
    foreign_target = _u()  # a DataTarget absent from THIS snapshot
    snapshot = {
        "schema_version": 5,
        "model": {"display_name": "imported", "target_id": foreign_target},
        "data_targets": [{"id": _u()}],  # a DIFFERENT (this-model) target
        "hierarchies": [],
        "aggregates": [],
    }
    with caplog.at_level(logging.WARNING):
        params = await _run_rehydrate(snapshot)
    assert params["target_id"] is None
    assert any("Bug-8950" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_bug8950_keeps_in_model_target_id():
    valid_target = _u()
    snapshot = {
        "schema_version": 5,
        "model": {"display_name": "imported", "target_id": valid_target},
        "data_targets": [{"id": valid_target}],
        "hierarchies": [],
        "aggregates": [],
    }
    params = await _run_rehydrate(snapshot)
    assert params["target_id"] == uuid.UUID(valid_target)
