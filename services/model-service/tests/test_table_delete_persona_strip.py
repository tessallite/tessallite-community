"""Bug-6225 [SECURITY] regression — deleting a table must strip the measures,
dimensions and now-empty hierarchies it removes from persona allow-lists.

A bare table delete bulk-removes measures/dimensions (and cascades variants)
without touching persona ``included_*_ids`` / ``default_filters`` or the
soft-referencing rows that point at them. Left behind, dangling UUIDs silently
shrink filtered persona allow-lists — a governance/data-exposure hazard. This
test locks the strip+purge cleanup, which previously shipped with no coverage.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit


class _Res:
    """Flexible result supporting ``.scalars().all()``, ``.all()`` and ``.scalar()``."""

    def __init__(self, rows=None, scalar_val=None):
        self._rows = list(rows or [])
        self._scalar = scalar_val

    def scalars(self):
        return FakeScalarResult(self._rows)

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar


def _execute_queue(*results):
    queue = list(results)

    async def _side(*_a, **_kw):
        if queue:
            return queue.pop(0)
        return _Res([])  # trailing deletes / empty lookups

    return AsyncMock(side_effect=_side)


@pytest.mark.asyncio
async def test_delete_table_strips_doomed_entities_from_personas(client):
    source_id = uuid.uuid4()
    table_id = uuid.uuid4()
    col_id = uuid.uuid4()
    m_id = uuid.uuid4()
    d_id = uuid.uuid4()
    h_id = uuid.uuid4()

    table = types.SimpleNamespace(
        id=table_id, source_id=source_id, model_id=TEST_MODEL_ID,
    )
    db = make_mock_db()

    async def _get(entity, pk):
        # Bug-8862: delete_table now proves project -> model before the table.
        if entity.__name__ == "Model":
            return make_model()
        return table

    db.get = AsyncMock(side_effect=_get)
    # Ordered results mirroring delete_table's query sequence; a now-empty
    # hierarchy (h_id) must also be stripped from personas.
    strip = AsyncMock()
    purge = AsyncMock()
    db.execute = _execute_queue(
        _Res([]),                  # 0a FOR UPDATE lock on the table row
        _Res([]),                  # 0b assert_table_not_rls_mapping -> no rules
        _Res([col_id]),            # 1 col_ids for this table
        _Res([]),                  # 2 uda_ids
        _Res([m_id]),              # 3 base measure ids bound to col_id
        _Res([]),                  # 4 variant ids
        _Res([(d_id, "region")]),  # 5 dimension (id, name) rows
        _Res([]),                  # 6 persona load (default_filters)
        _Res([]),                  # 7 delete(Dimension)
        _Res([]),                  # 8 delete(Measure)
        _Res([h_id]),              # 9 physical hierarchy-level hierarchy ids
        _Res([]),                  # 10 delete(HierarchyLevel)
        _Res(scalar_val=0),        # 11 remaining-level count for h_id -> empty
    )

    with (
        patch("src.api.tables.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.strip_id_from_personas", strip),
        patch("src.api._scope.purge_entity_soft_references", purge),
        patch(
            "shared.semantic.model_validator.revalidate_model", new=AsyncMock()
        ),
    ):
        resp = await client.delete(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
            f"/sources/{source_id}/tables/{table_id}"
        )

    assert resp.status_code == 204, resp.text
    stripped = {
        (c.kwargs["object_id"], c.kwargs["object_class"])
        for c in strip.call_args_list
    }
    assert (m_id, "measure") in stripped
    assert (d_id, "dimension") in stripped
    assert (h_id, "hierarchy") in stripped  # now-empty hierarchy stripped too
    # Soft references purged for the doomed measure + dimension.
    purged_ids = {c.kwargs["entity_id"] for c in purge.call_args_list}
    assert {m_id, d_id} <= purged_ids
