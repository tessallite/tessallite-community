"""Bug-9268 / Bug-9266 — snapshot import audience-narrowing at rehydrate.

CRUD already refuses a tag-restriction-only empty-audience persona.
``_insert_personas`` must resolve ``persona_tag_restrictions`` and pass
``restricted_tag_ids`` into the same helper, and must still refuse a
measure allow-list with an empty audience (Bug-9266 rehydrator guard).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from shared.model_snapshot.rehydrator import (
    SnapshotSchemaError,
    _insert_personas,
    _restricted_tag_ids_for_persona,
    _validate_imported_persona,
)


def _row(*, slug="cls_only", audience_roles=None, measure_ids=None):
    return {
        "slug": slug,
        "name": slug,
        "audience_roles": list(audience_roles or []),
        "included_measure_ids": list(measure_ids or []),
        "included_dimension_ids": [],
        "included_hierarchy_ids": [],
        "default_filters": {},
    }


@pytest.mark.asyncio
async def test_bug_9268_tag_only_empty_audience_is_refused():
    """Bug-9268: tag restrictions + empty audience + no allow-lists
    must raise SnapshotSchemaError naming the persona.
    """
    pid = uuid.uuid4()
    tid = uuid.uuid4()
    mid = uuid.uuid4()
    snap = {
        "personas": [
            {
                "id": str(pid),
                **_row(slug="cls_only", audience_roles=[]),
            }
        ],
        "persona_tag_restrictions": [
            {"persona_id": str(pid), "data_tag_id": str(tid)},
        ],
    }
    db = AsyncMock()
    with pytest.raises(SnapshotSchemaError) as exc:
        await _insert_personas(mid, snap, db)
    assert "cls_only" in str(exc.value)
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_bug_9268_tag_only_with_audience_imports():
    """Bug-9268: the same tag-only persona with an explicit audience passes."""
    pid = uuid.uuid4()
    tid = uuid.uuid4()
    mid = uuid.uuid4()
    snap = {
        "personas": [
            {
                "id": str(pid),
                **_row(slug="cls_only", audience_roles=["analyst"]),
            }
        ],
        "persona_tag_restrictions": [
            {"persona_id": str(pid), "data_tag_id": str(tid)},
        ],
    }
    db = AsyncMock()
    await _insert_personas(mid, snap, db)
    db.execute.assert_awaited()
    assert _restricted_tag_ids_for_persona(snap, pid) == [str(tid)]


@pytest.mark.asyncio
async def test_bug_9266_allow_list_empty_audience_refused_at_rehydrate():
    """Bug-9266: measure allow-list + empty audience is SnapshotSchemaError
    via ``_validate_imported_persona`` (not only the YAML deserialiser).
    """
    row = _row(
        slug="regional",
        audience_roles=[],
        measure_ids=[str(uuid.uuid4())],
    )
    with pytest.raises(SnapshotSchemaError) as exc:
        await _validate_imported_persona(
            AsyncMock(), uuid.uuid4(), row, restricted_tag_ids=[],
        )
    assert "regional" in str(exc.value)


@pytest.mark.asyncio
async def test_filter_only_empty_persona_still_imports():
    """Unrestricted empty-everything + empty audience remains importable."""
    await _validate_imported_persona(
        AsyncMock(), uuid.uuid4(), _row(slug="everyone", audience_roles=[]),
        restricted_tag_ids=[],
    )
