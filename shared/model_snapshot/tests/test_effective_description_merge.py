"""Bug-7959: deployment-pinned effective_description merge in serialiser.

At snapshot serialisation time, the serialiser merges approved/show glossary
definitions into dimension and measure snapshot rows as an additive
``effective_description`` field. This test validates:

1. The merge produces ``effective_description`` matching the approved
   glossary definition for attached targets.
2. Dimensions/measures without a glossary attachment fall back to the
   raw ``description``.
3. Only approved, non-superseded, show-visibility entries are eligible.
4. When multiple qualifying entries attach to the same target, the one
   with the highest ``version`` wins.
5. The field is present on every dimension and measure, even when no
   glossary entries exist at all (fallback to raw description).
6. Previously-deployed snapshots that lack ``effective_description``
   will not break consumers (additive field with documented fallback).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from shared.model_snapshot.rehydrator import _insert_dimensions, _insert_measures
from shared.model_snapshot.serialiser import _merge_effective_descriptions


def _u() -> str:
    return str(uuid.uuid4())


def _snap(
    *,
    dimensions: list | None = None,
    measures: list | None = None,
    glossary_entries: list | None = None,
) -> dict:
    return {
        "dimensions": dimensions or [],
        "measures": measures or [],
        "glossary_entries": glossary_entries or [],
    }


class TestMergeEffectiveDescriptions:
    """Core merge logic tests."""

    def test_approved_show_glossary_sets_effective_description(self):
        dim_id = _u()
        entry_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Region", "description": "raw desc"},
            ],
            glossary_entries=[
                {
                    "id": entry_id,
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Glossary definition for Region",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "Glossary definition for Region"

    def test_measure_gets_effective_description(self):
        meas_id = _u()
        entry_id = _u()
        snap = _snap(
            measures=[
                {"id": meas_id, "name": "Revenue", "description": "raw measure"},
            ],
            glossary_entries=[
                {
                    "id": entry_id,
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Total revenue across all channels",
                    "attachments": [
                        {"target_type": "measure", "target_id": meas_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["measures"][0]["effective_description"] == "Total revenue across all channels"

    def test_no_glossary_falls_back_to_raw_description(self):
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Product", "description": "Product dimension"},
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "Product dimension"

    def test_empty_glossary_entries_falls_back(self):
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Product", "description": "raw desc"},
            ],
            glossary_entries=[],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "raw desc"

    def test_pending_review_entry_not_eligible(self):
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Region", "description": "raw desc"},
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "pending_review",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Should not appear",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "raw desc"

    def test_superseded_entry_not_eligible(self):
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Region", "description": "raw desc"},
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": _u(),
                    "visibility": "show",
                    "version": 1,
                    "definition": "Superseded text",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "raw desc"

    def test_hidden_visibility_not_eligible(self):
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Region", "description": "raw desc"},
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "hide",
                    "version": 1,
                    "definition": "Hidden glossary text",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "raw desc"

    def test_null_visibility_is_eligible(self):
        """Legacy entries with visibility=None are treated as 'show' (Bug-5926)."""
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Region", "description": "raw desc"},
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": None,
                    "version": 1,
                    "definition": "Legacy entry text",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "Legacy entry text"

    def test_highest_version_wins_when_multiple_entries(self):
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Region", "description": "raw desc"},
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Version 1 text",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 3,
                    "definition": "Version 3 text",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 2,
                    "definition": "Version 2 text",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "Version 3 text"

    def test_no_description_falls_back_to_empty_string(self):
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {"id": dim_id, "name": "Region"},
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == ""

    def test_column_attachment_does_not_affect_dimension(self):
        """A glossary entry attached to a column (not a dimension) must not
        set effective_description on a dimension sharing the same id."""
        shared_id = _u()
        snap = _snap(
            dimensions=[
                {"id": shared_id, "name": "Region", "description": "raw desc"},
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Column glossary text",
                    "attachments": [
                        {"target_type": "column", "target_id": shared_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "raw desc"

    def test_column_attachment_maps_to_dimension_via_source_column_id(self):
        """Bug-9392: a column-type glossary attachment must surface as the
        effective_description of the dimension whose source_column_id is that
        column, so column-only terms reach the JDBC/XMLA catalogue instead of
        being silently dropped. FAILS pre-fix (merge only read dim/measure
        attachments; column attachments were computed and then ignored)."""
        col_id = _u()
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {
                    "id": dim_id,
                    "name": "Region",
                    "description": "raw desc",
                    "source_column_id": col_id,
                },
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Column-level business definition",
                    "attachments": [
                        {"target_type": "column", "target_id": col_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert (
            snap["dimensions"][0]["effective_description"]
            == "Column-level business definition"
        )

    def test_column_attachment_maps_to_measure_via_source_column_id(self):
        """Bug-9392: same column-to-source_column_id fallback for measures."""
        col_id = _u()
        meas_id = _u()
        snap = _snap(
            measures=[
                {
                    "id": meas_id,
                    "name": "Revenue",
                    "description": "raw measure",
                    "source_column_id": col_id,
                },
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Amount billed net of returns",
                    "attachments": [
                        {"target_type": "column", "target_id": col_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert (
            snap["measures"][0]["effective_description"]
            == "Amount billed net of returns"
        )

    def test_direct_dimension_attachment_precedes_column_attachment(self):
        """Bug-9392: a term attached DIRECTLY to the dimension wins over a term
        attached to its source column, even when the column term has a higher
        version. The column attachment is only a fallback."""
        col_id = _u()
        dim_id = _u()
        snap = _snap(
            dimensions=[
                {
                    "id": dim_id,
                    "name": "Region",
                    "description": "raw desc",
                    "source_column_id": col_id,
                },
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Direct dimension term",
                    "attachments": [
                        {"target_type": "dimension", "target_id": dim_id},
                    ],
                },
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 9,
                    "definition": "Column term (should be shadowed)",
                    "attachments": [
                        {"target_type": "column", "target_id": col_id},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert (
            snap["dimensions"][0]["effective_description"]
            == "Direct dimension term"
        )

    def test_column_attachment_for_other_column_does_not_leak(self):
        """Bug-9392: the column fallback must match on source_column_id, so a
        term on an UNRELATED column must not surface on this dimension."""
        snap = _snap(
            dimensions=[
                {
                    "id": _u(),
                    "name": "Region",
                    "description": "raw desc",
                    "source_column_id": _u(),
                },
            ],
            glossary_entries=[
                {
                    "id": _u(),
                    "status": "approved",
                    "superseded_by": None,
                    "visibility": "show",
                    "version": 1,
                    "definition": "Unrelated column term",
                    "attachments": [
                        {"target_type": "column", "target_id": _u()},
                    ],
                },
            ],
        )
        _merge_effective_descriptions(snap)
        assert snap["dimensions"][0]["effective_description"] == "raw desc"

    def test_empty_snap_no_crash(self):
        snap: dict = {"dimensions": [], "measures": [], "glossary_entries": []}
        _merge_effective_descriptions(snap)

    def test_missing_keys_no_crash(self):
        snap: dict = {}
        _merge_effective_descriptions(snap)


@pytest.mark.asyncio
async def test_dimension_rehydration_strips_snapshot_only_effective_description():
    model_id = uuid.uuid4()
    db = AsyncMock()
    await _insert_dimensions(
        model_id,
        {
            "dimensions": [
                {
                    "id": _u(),
                    "model_id": _u(),
                    "name": "Region",
                    "description": "Raw region",
                    "effective_description": "Pinned glossary region",
                }
            ]
        },
        db,
    )

    params = db.execute.await_args.args[0].compile().params
    assert "effective_description" not in params
    assert params["description"] == "Raw region"


@pytest.mark.asyncio
async def test_measure_rehydration_strips_snapshot_only_effective_description():
    model_id = uuid.uuid4()
    measure_id = _u()
    db = AsyncMock()
    await _insert_measures(
        model_id,
        {
            "measures": [
                {
                    "id": measure_id,
                    "model_id": _u(),
                    "name": "Revenue",
                    "description": "Raw revenue",
                    "effective_description": "Pinned glossary revenue",
                    "measure_type": "standard",
                    "data_type": "numeric",
                    "default_agg": "sum",
                }
            ]
        },
        db,
    )

    params = db.execute.await_args.args[0].compile().params
    assert "effective_description" not in params
    assert params["description"] == "Raw revenue"
