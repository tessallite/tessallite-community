"""Bug-8708 — predictive build state is never snapshot definition state.

The optimizer writes the version/epoch pair as a runtime stamp. A project
import creates a new model, while a revert rehydrates an existing model; both
paths must therefore omit the pair from the portable model row and clear any
old destination stamp explicitly.
"""
from __future__ import annotations

import ast
import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.model_snapshot import rehydrator, serialiser
from shared.model_snapshot.rehydrator import (
    RehydrationMode,
    _MODEL_SCALAR_EXCLUDE,
    rehydrate_into_live,
)


def test_bug8708_serializer_excludes_both_predictive_stamp_columns():
    """The pre-fix generic Model row export carried the stale optimizer stamp."""
    source = inspect.getsource(serialiser.snapshot_model)
    tree = ast.parse(source)
    model_call = next(
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_row_to_dict"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "model"
    )
    exclude = next(
        kw.value
        for kw in model_call.keywords
        if kw.arg == "exclude" and isinstance(kw.value, ast.Tuple)
    )
    excluded = {
        element.value
        for element in exclude.elts
        if isinstance(element, ast.Constant)
    }
    assert {
        "predictive_built_for_version_id",
        "predictive_built_for_epoch",
    } <= excluded


def test_bug8708_rehydrator_excludes_both_predictive_stamp_columns():
    """The derived scalar contract must not rehydrate optimizer artifacts."""
    assert {
        "predictive_built_for_version_id",
        "predictive_built_for_epoch",
    } <= _MODEL_SCALAR_EXCLUDE


def _empty_result() -> MagicMock:
    result = MagicMock()
    result.all.return_value = []
    result.first.return_value = None
    scalars = MagicMock()
    scalars.all.return_value = []
    scalars.first.return_value = None
    result.scalars.return_value = scalars
    return result


_NEUTRALISED_HELPERS = [
    "_truncate_model_children",
    "_insert_data_sources_and_targets",
    "_insert_calendar_tables",
    "_insert_tables_and_columns",
    "_insert_udas",
    "_insert_joins",
    "_insert_hierarchies",
    "_insert_dimensions",
    "_synthesize_missing_hierarchy_dimensions",
    "_insert_attribute_relationships",
    "_backfill_dimension_provenance",
    "_insert_measures",
    "_insert_named_sets",
    "_insert_kpis",
    "_insert_drill_through_sets",
    "_insert_aggregate_lifecycle",
    "_insert_personas",
    "_insert_pockets",
    "_insert_data_tags",
    "_insert_row_security",
    "_insert_glossary",
    "_insert_source_statistics",
    "_insert_source_join_statistics",
    "_insert_ai_scheduler",
    "_insert_lineage",
    "_insert_model_parameters",
    "_insert_model_alias_map",
    "_insert_refresh_sla_config",
    "_insert_data_quality_rules",
    "_insert_entity_translations",
    "_invalidate_borrowing_models_on_source_repoint",
    "_validate_preserved_aggregates",
    "_validate_preserved_pockets",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [RehydrationMode.IMPORT, RehydrationMode.RESTORE])
async def test_bug8708_import_and_revert_clear_destination_predictive_stamp(mode):
    """A stale destination stamp cannot survive either rehydrate mode."""
    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(seed="destination-seed"))
    db.execute = AsyncMock(return_value=_empty_result())
    db.add = MagicMock()
    snapshot = {
        "schema_version": 5,
        "model": {
            "display_name": "restored",
            "predictive_built_for_version_id": str(uuid.uuid4()),
            "predictive_built_for_epoch": 19,
        },
        "hierarchies": [],
        "aggregates": [],
    }
    patches = {
        name: AsyncMock(return_value=[] if name == "_insert_aggregates" else None)
        for name in _NEUTRALISED_HELPERS
    }
    patches["_insert_aggregates"] = AsyncMock(return_value=[])

    with patch.multiple(rehydrator, **patches):
        await rehydrate_into_live(
            uuid.uuid4(), snapshot, db,
            mode=mode,
            preserve_aggregates=False,
            preserve_pockets=False,
        )

    updates = [
        call.args[0]
        for call in db.execute.await_args_list
        if getattr(call.args[0], "is_update", False)
    ]
    assert updates, "rehydration did not issue the model scalar update"
    params = dict(updates[-1].compile().params)
    assert params["predictive_built_for_version_id"] is None
    assert params["predictive_built_for_epoch"] is None
