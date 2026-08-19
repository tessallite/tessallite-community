"""F-013-06 / F-013-07 unit tests.

F-013-06 — the five v3 families (model_parameters, model_alias_map,
            refresh_sla_config, data_quality_rules, entity_translations) are
            re-inserted on rehydrate (they previously fell out of the contract).
F-013-07 — Model scalar restore is derived from the ORM, so the eight columns
            the old fixed tuple dropped now travel; the exclude set never
            rehydrates the deploy pointer / timestamps.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import Model
from shared.model_snapshot.rehydrator import (
    _MODEL_SCALAR_EXCLUDE,
    _insert_data_quality_rules,
    _insert_entity_translations,
    _insert_model_alias_map,
    _insert_model_parameters,
    _insert_refresh_sla_config,
    _model_scalar_fields,
    _model_uuid_fields,
)


def _capture_db():
    db = AsyncMock()
    captured: list[dict] = []

    async def _execute(stmt):
        try:
            captured.append(dict(stmt.compile().params))
        except Exception:
            pass
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db._captured = captured
    return db


# ---------------------------------------------------------------------------
# F-013-07 — derived scalar field list
# ---------------------------------------------------------------------------

def test_scalar_fields_include_previously_dropped_columns():
    fields = set(_model_scalar_fields())
    # The eight columns the old 16-name tuple silently dropped.
    for col in (
        "predictive_eviction_policy",
        "predictive_storage_budget_bytes",
        "predictive_storage_budget_count",
        "predictive_requires_approval",
        "pocket_size_budget_bytes",
        "glossary_max_distinct",
        "expose_kpis_inline",
        "fiscal_year_start_month",
    ):
        # Only assert for columns that actually exist on the current ORM.
        if col in {c.name for c in Model.__table__.columns}:
            assert col in fields, f"{col} fell out of the rehydrate contract"


def test_scalar_fields_exclude_identity_and_deploy_pointer():
    fields = set(_model_scalar_fields())
    assert _MODEL_SCALAR_EXCLUDE.isdisjoint(fields)
    for excluded in ("id", "project_id", "deployed_version_id",
                     "last_deployed_at", "created_at", "updated_at",
                     "deploy_epoch"):
        assert excluded not in fields


def test_deploy_epoch_is_excluded_from_rehydration():
    """opus5 completion-round R2 (finding 3.5): ``deploy_epoch`` must NEVER be
    rehydrated from a snapshot (import or revert) — it is a MONOTONIC counter
    the KPILatest epoch-monotonicity guard (Bug-7982;
    ``kpi_latest.py``/``sweep.py`` ``on_conflict_do_update(where=...)``)
    structurally depends on. If a revert ever restored an older snapshot
    value here, the epoch would move BACKWARDS and that guard's
    ``existing.evaluated_for_epoch <= eval_epoch`` condition would then
    suppress every subsequent kpi_latest write for the model permanently —
    worse than having no guard at all, and silently (see
    ``SnapshotSweepResult.latest_suppressed``, which only reports a COUNT, not
    the root cause). Standalone, dedicated assertion (in addition to the
    general exclude-set test above) so this specific precondition cannot
    silently regress if the general test is ever relaxed or reworded.
    """
    assert "deploy_epoch" in _MODEL_SCALAR_EXCLUDE
    assert "deploy_epoch" not in _model_scalar_fields()


def test_scalar_fields_track_every_non_excluded_orm_column():
    expected = {
        c.name for c in Model.__table__.columns
        if c.name not in _MODEL_SCALAR_EXCLUDE
    }
    assert set(_model_scalar_fields()) == expected


def test_uuid_fields_include_target_and_llm_config():
    uuid_fields = _model_uuid_fields()
    # These were the only two hand-coerced before; derivation must still catch them.
    for fk in ("target_id", "llm_config_id"):
        if fk in {c.name for c in Model.__table__.columns}:
            assert fk in uuid_fields


# ---------------------------------------------------------------------------
# F-013-06 — v3 inserters re-create each family
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_model_parameters_round_trips():
    mid = uuid.uuid4()
    snap = {"model_parameters": [
        {"id": str(uuid.uuid4()), "name": "as_of", "param_type": "date"},
    ]}
    db = _capture_db()
    await _insert_model_parameters(mid, snap, db)
    assert len(db._captured) == 1
    assert db._captured[0]["name"] == "as_of"
    assert db._captured[0]["model_id"] == mid


@pytest.mark.asyncio
async def test_insert_model_alias_map_single_row():
    mid = uuid.uuid4()
    snap = {"model_alias_map": {"model_id": str(mid), "alias_map": {"rev": "revenue"}}}
    db = _capture_db()
    await _insert_model_alias_map(mid, snap, db)
    assert len(db._captured) == 1
    assert db._captured[0]["model_id"] == mid


@pytest.mark.asyncio
async def test_insert_model_alias_map_skips_when_absent():
    db = _capture_db()
    await _insert_model_alias_map(uuid.uuid4(), {"model_alias_map": None}, db)
    assert db._captured == []


@pytest.mark.asyncio
async def test_insert_refresh_sla_config_single_row():
    mid = uuid.uuid4()
    snap = {"refresh_sla_config": {
        "id": str(uuid.uuid4()),
        "target_completion_time": "07:00",
        "grace_period_minutes": 15,
    }}
    db = _capture_db()
    await _insert_refresh_sla_config(mid, snap, db)
    assert len(db._captured) == 1
    assert db._captured[0]["target_completion_time"] == "07:00"
    assert db._captured[0]["model_id"] == mid


@pytest.mark.asyncio
async def test_insert_data_quality_rules_round_trips():
    mid = uuid.uuid4()
    snap = {"data_quality_rules": [
        {
            "id": str(uuid.uuid4()),
            "name": "no_nulls",
            "target_type": "column",
            "target_id": str(uuid.uuid4()),
            "rule_type": "not_null",
            "severity": "warn",
        },
    ]}
    db = _capture_db()
    await _insert_data_quality_rules(mid, snap, db)
    assert len(db._captured) == 1
    assert db._captured[0]["name"] == "no_nulls"
    assert db._captured[0]["model_id"] == mid


@pytest.mark.asyncio
async def test_insert_entity_translations_round_trips():
    mid = uuid.uuid4()
    snap = {"entity_translations": [
        {
            "id": str(uuid.uuid4()),
            "entity_type": "measure",
            "entity_id": str(uuid.uuid4()),
            "field_name": "display_name",
            "locale": "fr",
            "translated_text": "Revenu",
        },
    ]}
    db = _capture_db()
    await _insert_entity_translations(mid, snap, db)
    assert len(db._captured) == 1
    assert db._captured[0]["translated_text"] == "Revenu"
    assert db._captured[0]["model_id"] == mid


@pytest.mark.asyncio
async def test_v3_inserters_no_op_on_v1_snapshot():
    """A pre-v3 snapshot (keys absent) inserts nothing — backward compatible."""
    mid = uuid.uuid4()
    for fn in (
        _insert_model_parameters,
        _insert_model_alias_map,
        _insert_refresh_sla_config,
        _insert_data_quality_rules,
        _insert_entity_translations,
    ):
        db = _capture_db()
        await fn(mid, {"schema_version": 1}, db)
        assert db._captured == [], fn.__name__
