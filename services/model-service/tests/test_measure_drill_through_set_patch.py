"""Round-trip tests for the drill-through-set curation endpoints.

Phase 8.A.1 — backend coverage for:
  * GET    /measures/{id}/drill-through-set
  * PATCH  /measures/{id}/drill-through-set
  * DELETE /measures/{id}/drill-through-set

Validates the stable error codes that the editor consumes:
  DRILL_NO_SOURCE_TABLE
  DRILL_DETAIL_COLUMN_OFF_TABLE
  DRILL_DIMENSION_NOT_IN_MODEL
  DRILL_DIMENSION_NO_JOIN
  DRILL_SOURCE_TABLE_NOT_IN_MODEL
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from shared.db.models import (
    Dimension,
    DrillThroughSet,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
)

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def __iter__(self):
        return iter(self._items)


def _measure(
    *,
    measure_id: uuid.UUID,
    measure_type: str = "standard",
    source_column_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=measure_id,
        model_id=TEST_MODEL_ID,
        measure_type=measure_type,
        source_column_id=source_column_id,
        user_defined_attribute_id=None,
    )


def _drill(
    *,
    measure_id: uuid.UUID,
    source_table_id: uuid.UUID | None = None,
    detail_columns: list | None = None,
    joined_dimension_ids: list | None = None,
    row_limit_override: int | None = None,
    source_join_path: list | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        measure_id=measure_id,
        source_table_id=source_table_id,
        detail_columns=detail_columns,
        joined_dimension_ids=joined_dimension_ids,
        row_limit_override=row_limit_override,
        source_join_path=source_join_path,
        created_at=NOW,
        updated_at=NOW,
    )


def _scripted_get(*, model, measure, table=None, column=None, table_lookup=None):
    """Build an AsyncMock for db.get that dispatches by ORM class name.

    ``table_lookup`` is an optional dict[uuid, ModelTable] for multi-table
    resolution (used by the alternate-source-override path).
    """

    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Measure":
            return measure
        if name == "ModelTable":
            if table_lookup is not None:
                return table_lookup.get(key)
            return table
        if name == "ModelColumn":
            return column
        return None

    return AsyncMock(side_effect=_get)


def _execute_script(*results):
    """Sequence the responses for db.execute calls in the order the route fires them."""
    queue = list(results)

    async def _side(*_a, **_kw):
        if queue:
            return queue.pop(0)
        # Fall back to an empty result for trailing helper queries.
        empty = MagicMock()
        empty.scalar_one_or_none.return_value = None
        empty.scalars.return_value.all.return_value = []
        empty.all.return_value = []
        return empty

    return AsyncMock(side_effect=_side)


# ---------------------------------------------------------------------------
# GET — auto-create on first read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_drill_through_set_returns_existing_row(client):
    measure_id = uuid.uuid4()
    measure = _measure(measure_id=measure_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure)
    db.execute = _execute_script(_ScalarResult([drill]))

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{measure_id}/drill-through-set")

    assert resp.status_code == 200
    data = resp.json()
    assert data["measure_id"] == str(measure_id)
    assert data["detail_columns"] is None
    assert data["joined_dimension_ids"] is None
    assert "created_at" in data
    assert "updated_at" in data


@pytest.mark.asyncio
async def test_get_drill_through_set_with_detail_columns(client):
    measure_id = uuid.uuid4()
    col_id_1 = uuid.uuid4()
    col_id_2 = uuid.uuid4()
    measure = _measure(measure_id=measure_id)
    drill = _drill(measure_id=measure_id, detail_columns=[col_id_1, col_id_2])
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure)
    db.execute = _execute_script(_ScalarResult([drill]))

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{measure_id}/drill-through-set")

    assert resp.status_code == 200
    data = resp.json()
    assert data["measure_id"] == str(measure_id)
    assert data["detail_columns"] == [str(col_id_1), str(col_id_2)]


@pytest.mark.asyncio
async def test_get_drill_through_set_enriched_resolves_columns(client):
    measure_id = uuid.uuid4()
    col_id = uuid.uuid4()
    measure = _measure(measure_id=measure_id)
    drill = _drill(measure_id=measure_id, detail_columns=[col_id])
    model = make_model()

    fake_col = types.SimpleNamespace(
        id=col_id,
        column_name="region_name",
        display_name="Region Name",
    )

    db = make_mock_db()

    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Measure":
            return measure
        if name == "ModelColumn":
            return fake_col
        return None

    db.get = AsyncMock(side_effect=_get)
    db.execute = _execute_script(_ScalarResult([drill]))

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{measure_id}/drill-through-set/enriched")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["detail_columns"]) == 1
    col = data["detail_columns"][0]
    assert col["id"] == str(col_id)
    assert col["name"] == "region_name"
    assert col["display_name"] == "Region Name"
    assert col["source_type"] == "column"


@pytest.mark.asyncio
async def test_get_drill_through_set_404_when_measure_missing(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=None)

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{uuid.uuid4()}/drill-through-set")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_drill_through_set_404_for_calculated_measure(client):
    """Bug-6621(a): calculated measures now return 404 (not 400) so the
    error code does not function as an existence/type oracle."""
    measure_id = uuid.uuid4()
    measure = _measure(measure_id=measure_id, measure_type="calculated")
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure)

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{measure_id}/drill-through-set")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET join-paths — read-only under viewer (Bug-2570 / residual F-015-20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_drill_join_paths_does_not_write_when_no_drill_row(client):
    """GET .../join-paths must not auto-create or commit a DrillThroughSet.

    Bug-2570: the join-paths GET previously called the loader with the default
    ``create_if_missing=True`` and ``db.commit()``-ed, persisting an implicit
    DrillThroughSet under the read-only viewer role. With no drill row present,
    the loader must synthesize a transient set and the route must NOT add a row
    nor commit.
    """
    measure_id = uuid.uuid4()
    fact_table_id = uuid.uuid4()
    fact_column_id = uuid.uuid4()
    override_table_id = uuid.uuid4()

    fact_column = types.SimpleNamespace(id=fact_column_id, model_table_id=fact_table_id)
    fact_table = types.SimpleNamespace(
        id=fact_table_id, model_id=TEST_MODEL_ID, physical_name="orders"
    )
    override_table = types.SimpleNamespace(
        id=override_table_id, model_id=TEST_MODEL_ID, physical_name="orders"
    )
    measure = _measure(measure_id=measure_id, source_column_id=fact_column_id)
    model = make_model()

    table_lookup = {fact_table_id: fact_table, override_table_id: override_table}
    column_lookup = {fact_column_id: fact_column}

    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Measure":
            return measure
        if name == "ModelTable":
            return table_lookup.get(key)
        if name == "ModelColumn":
            return column_lookup.get(key)
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    # 1) loader's DrillThroughSet lookup → none exists (forces the transient
    #    no-write path); 2) _enumerate_join_paths edge query → a single hop.
    db.execute = _execute_script(
        _ScalarResult([]),  # no DrillThroughSet row for this measure
        _ScalarResult(
            [types.SimpleNamespace(
                id=uuid.uuid4(),
                left_table_id=override_table_id,
                right_table_id=fact_table_id,
                join_type="many_to_one",
            )]
        ),
    )

    with (
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        # No effective persona for this caller — the Bug-6614 visibility gate is
        # a no-op here, leaving the scripted join-edge result for the enumerator.
        patch(
            "src.api.measures.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(
            f"{PREFIX}/{measure_id}/drill-through-set/join-paths",
            params={"source_table_id": str(override_table_id)},
        )

    assert resp.status_code == 200, resp.text
    # The decisive assertions: a GET must not write under the viewer role.
    db.commit.assert_not_called()
    added_drill_sets = [
        call.args[0]
        for call in db.add.call_args_list
        if isinstance(call.args[0], DrillThroughSet)
    ]
    assert added_drill_sets == [], "GET must not persist a DrillThroughSet"
    db.flush.assert_not_called()


# ---------------------------------------------------------------------------
# PATCH — happy path on each curated field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_row_limit_override(client):
    measure_id = uuid.uuid4()
    measure = _measure(measure_id=measure_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure)
    db.execute = _execute_script(_ScalarResult([drill]))

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"row_limit_override": 500},
        )

    assert resp.status_code == 200, resp.text
    assert drill.row_limit_override == 500


@pytest.mark.asyncio
async def test_patch_row_limit_override_rejects_zero(client):
    """Bug-5935 (F-019-04): bounds now live on the DrillThroughSetUpdate
    schema (Field(ge=1, le=DRILL_MAX_ROW_LIMIT)), so an out-of-range value
    fails FastAPI body validation (422) before the handler runs — it no
    longer reaches the old manual ``val <= 0`` check that returned 400."""
    measure_id = uuid.uuid4()

    resp = await client.patch(
        f"{PREFIX}/{measure_id}/drill-through-set",
        json={"row_limit_override": 0},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_row_limit_override_rejects_above_ceiling(client):
    """Bug-5935 (F-019-04): a value above the runtime's DRILL_MAX_ROW_LIMIT
    clamp (10,000) must be rejected at save time, not silently clamped at
    drill time."""
    measure_id = uuid.uuid4()

    resp = await client.patch(
        f"{PREFIX}/{measure_id}/drill-through-set",
        json={"row_limit_override": 50000},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_row_limit_override_accepts_ceiling_value(client):
    """The ceiling itself (10,000) is a valid, accepted value."""
    measure_id = uuid.uuid4()
    measure = _measure(measure_id=measure_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure)
    db.execute = _execute_script(_ScalarResult([drill]))

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"row_limit_override": 10000},
        )

    assert resp.status_code == 200, resp.text
    assert drill.row_limit_override == 10000


@pytest.mark.asyncio
async def test_patch_detail_columns_validates_table_membership(client):
    """Invalid detail_columns must come back as DRILL_DETAIL_COLUMN_OFF_TABLE."""
    measure_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    foreign_column_id = uuid.uuid4()  # not on the source table

    column = types.SimpleNamespace(id=column_id, model_table_id=table_id)
    table = types.SimpleNamespace(id=table_id, model_id=TEST_MODEL_ID, physical_name="orders")
    measure = _measure(measure_id=measure_id, source_column_id=column_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure, table=table, column=column)
    # 1) load drill row, 2) detail-columns membership check returns just `column_id`
    db.execute = _execute_script(
        _ScalarResult([drill]),
        _ScalarResult([column_id]),  # only the valid id is found
    )

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"detail_columns": [str(column_id), str(foreign_column_id)]},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "DRILL_DETAIL_COLUMN_OFF_TABLE"
    assert str(foreign_column_id) in body["detail"]["invalid_ids"]


@pytest.mark.asyncio
async def test_patch_detail_columns_happy_path(client):
    measure_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()

    column = types.SimpleNamespace(id=column_id, model_table_id=table_id)
    table = types.SimpleNamespace(id=table_id, model_id=TEST_MODEL_ID, physical_name="orders")
    measure = _measure(measure_id=measure_id, source_column_id=column_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure, table=table, column=column)
    db.execute = _execute_script(
        _ScalarResult([drill]),
        _ScalarResult([column_id]),  # table-membership check
        _ScalarResult([column_id]),  # Bug-5933: dimension-projectability check
    )

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"detail_columns": [str(column_id)]},
        )

    assert resp.status_code == 200, resp.text
    assert drill.detail_columns == [str(column_id)]


@pytest.mark.asyncio
async def test_patch_detail_columns_rejects_non_projectable_column(client):
    """Bug-5933 (F-019-02): a column on the effective table but with no
    Dimension over it must be rejected at save time — it cannot be projected
    through the semantic layer, so query-router would fail the drill later
    with DRILL_DETAIL_COLUMN_NOT_PROJECTABLE."""
    measure_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()

    column = types.SimpleNamespace(id=column_id, model_table_id=table_id)
    table = types.SimpleNamespace(id=table_id, model_id=TEST_MODEL_ID, physical_name="orders")
    measure = _measure(measure_id=measure_id, source_column_id=column_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure, table=table, column=column)
    db.execute = _execute_script(
        _ScalarResult([drill]),
        _ScalarResult([column_id]),  # table-membership check passes
        _ScalarResult([]),  # no Dimension over this column — not projectable
    )

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"detail_columns": [str(column_id)]},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "DRILL_DETAIL_COLUMN_NOT_PROJECTABLE"
    assert str(column_id) in body["detail"]["invalid_ids"]


@pytest.mark.asyncio
async def test_patch_source_table_override_must_belong_to_model(client):
    measure_id = uuid.uuid4()
    measure = _measure(measure_id=measure_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()
    foreign_table = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),  # belongs to a different model
        physical_name="other",
    )

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure, table=foreign_table)
    db.execute = _execute_script(_ScalarResult([drill]))

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"source_table_id": str(foreign_table.id)},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "DRILL_SOURCE_TABLE_NOT_IN_MODEL"


@pytest.mark.asyncio
async def test_patch_source_table_override_auto_resolves_single_join_path(client):
    """Bug-5932 (F-019-01): saving an override source table with no explicit
    source_join_path, when exactly one BFS path exists back to the intrinsic
    fact table, must PERSIST that sole path — not just validate it exists and
    then leave source_join_path empty. An empty path here always fails at
    query-router drill time (DRILL_JOIN_PATH_REQUIRED)."""
    measure_id = uuid.uuid4()
    fact_table_id = uuid.uuid4()
    fact_column_id = uuid.uuid4()
    override_table_id = uuid.uuid4()
    join_id = uuid.uuid4()

    fact_column = types.SimpleNamespace(id=fact_column_id, model_table_id=fact_table_id)
    fact_table = types.SimpleNamespace(id=fact_table_id, model_id=TEST_MODEL_ID, physical_name="orders")
    override_table = types.SimpleNamespace(
        id=override_table_id, model_id=TEST_MODEL_ID, physical_name="customers"
    )
    measure = _measure(measure_id=measure_id, source_column_id=fact_column_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    join_row = types.SimpleNamespace(
        id=join_id,
        left_table_id=override_table_id,
        right_table_id=fact_table_id,
        join_type="many_to_one",
    )

    table_lookup = {fact_table_id: fact_table, override_table_id: override_table}
    column_lookup = {fact_column_id: fact_column}

    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Measure":
            return measure
        if name == "ModelTable":
            return table_lookup.get(key)
        if name == "ModelColumn":
            return column_lookup.get(key)
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    # 1) load drill row, 2) Join edges for BFS enumeration (single direct hop)
    db.execute = _execute_script(
        _ScalarResult([drill]),
        _ScalarResult([join_row]),
    )

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"source_table_id": str(override_table_id)},
        )

    assert resp.status_code == 200, resp.text
    assert drill.source_join_path == [str(join_id)]


@pytest.mark.asyncio
async def test_patch_source_table_override_rejects_when_zero_join_paths(client):
    """Bug-5932 (F-019-01) review follow-up: the `else` arm of the same block
    (paths != 1) must still reject the save with DRILL_OVERRIDE_NO_JOIN_PATH
    when NO path exists between the override table and the intrinsic fact —
    only the single-path case auto-resolves."""
    measure_id = uuid.uuid4()
    fact_table_id = uuid.uuid4()
    fact_column_id = uuid.uuid4()
    override_table_id = uuid.uuid4()

    fact_column = types.SimpleNamespace(id=fact_column_id, model_table_id=fact_table_id)
    fact_table = types.SimpleNamespace(id=fact_table_id, model_id=TEST_MODEL_ID, physical_name="orders")
    override_table = types.SimpleNamespace(
        id=override_table_id, model_id=TEST_MODEL_ID, physical_name="customers"
    )
    measure = _measure(measure_id=measure_id, source_column_id=fact_column_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    table_lookup = {fact_table_id: fact_table, override_table_id: override_table}
    column_lookup = {fact_column_id: fact_column}

    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Measure":
            return measure
        if name == "ModelTable":
            return table_lookup.get(key)
        if name == "ModelColumn":
            return column_lookup.get(key)
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    # 1) load drill row, 2) Join edges for BFS enumeration (no edges at all)
    db.execute = _execute_script(
        _ScalarResult([drill]),
        _ScalarResult([]),
    )

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"source_table_id": str(override_table_id)},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "DRILL_OVERRIDE_NO_JOIN_PATH"
    assert drill.source_join_path is None


@pytest.mark.asyncio
async def test_patch_joined_dimensions_rejects_unknown_dimension(client):
    """joined_dimension_ids belonging to another model → DRILL_DIMENSION_NOT_IN_MODEL."""
    measure_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    bad_dim_id = uuid.uuid4()

    column = types.SimpleNamespace(id=column_id, model_table_id=table_id)
    table = types.SimpleNamespace(id=table_id, model_id=TEST_MODEL_ID, physical_name="orders")
    measure = _measure(measure_id=measure_id, source_column_id=column_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure, table=table, column=column)
    db.execute = _execute_script(
        _ScalarResult([drill]),
        _ScalarResult([]),  # no dimensions found in this model
    )

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"joined_dimension_ids": [str(bad_dim_id)]},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "DRILL_DIMENSION_NOT_IN_MODEL"
    assert str(bad_dim_id) in body["detail"]["invalid_ids"]


@pytest.mark.asyncio
async def test_patch_joined_dimensions_rejects_dimension_with_no_join(client):
    """A dimension whose base table has no Join to the fact → DRILL_DIMENSION_NO_JOIN."""
    measure_id = uuid.uuid4()
    fact_table_id = uuid.uuid4()
    fact_column_id = uuid.uuid4()
    dim_table_id = uuid.uuid4()
    dim_id = uuid.uuid4()
    dim_source_col_id = uuid.uuid4()

    fact_column = types.SimpleNamespace(id=fact_column_id, model_table_id=fact_table_id)
    fact_table = types.SimpleNamespace(id=fact_table_id, model_id=TEST_MODEL_ID, physical_name="orders")
    dim_source_column = types.SimpleNamespace(id=dim_source_col_id, model_table_id=dim_table_id)
    measure = _measure(measure_id=measure_id, source_column_id=fact_column_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    dim_row = types.SimpleNamespace(
        id=dim_id, model_id=TEST_MODEL_ID, source_column_id=dim_source_col_id
    )

    # db.get fans out to multiple ModelColumn lookups (fact + dim source col).
    column_lookup = {fact_column_id: fact_column, dim_source_col_id: dim_source_column}

    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Measure":
            return measure
        if name == "ModelTable":
            return fact_table if key == fact_table_id else None
        if name == "ModelColumn":
            return column_lookup.get(key)
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    # 1) load drill row, 2) dimensions found, 3) join edges (none → orphaned dim)
    db.execute = _execute_script(
        _ScalarResult([drill]),
        _ScalarResult([dim_row]),
        _ScalarResult([]),  # no Join rows
    )

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"joined_dimension_ids": [str(dim_id)]},
        )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "DRILL_DIMENSION_NO_JOIN"
    assert str(dim_id) in body["detail"]["invalid_ids"]


@pytest.mark.asyncio
async def test_patch_joined_dimensions_happy_path_with_direct_join(client):
    measure_id = uuid.uuid4()
    fact_table_id = uuid.uuid4()
    fact_column_id = uuid.uuid4()
    dim_table_id = uuid.uuid4()
    dim_id = uuid.uuid4()
    dim_source_col_id = uuid.uuid4()

    fact_column = types.SimpleNamespace(id=fact_column_id, model_table_id=fact_table_id)
    fact_table = types.SimpleNamespace(id=fact_table_id, model_id=TEST_MODEL_ID, physical_name="orders")
    dim_source_column = types.SimpleNamespace(id=dim_source_col_id, model_table_id=dim_table_id)
    measure = _measure(measure_id=measure_id, source_column_id=fact_column_id)
    drill = _drill(measure_id=measure_id)
    model = make_model()

    dim_row = types.SimpleNamespace(
        id=dim_id, model_id=TEST_MODEL_ID, source_column_id=dim_source_col_id
    )

    column_lookup = {fact_column_id: fact_column, dim_source_col_id: dim_source_column}

    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Measure":
            return measure
        if name == "ModelTable":
            return fact_table if key == fact_table_id else None
        if name == "ModelColumn":
            return column_lookup.get(key)
        return None

    db = make_mock_db()
    db.get = AsyncMock(side_effect=_get)
    db.execute = _execute_script(
        _ScalarResult([drill]),
        _ScalarResult([dim_row]),
        _ScalarResult([(dim_table_id, fact_table_id)]),  # direct edge
    )

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{measure_id}/drill-through-set",
            json={"joined_dimension_ids": [str(dim_id)]},
        )

    assert resp.status_code == 200, resp.text
    assert drill.joined_dimension_ids == [str(dim_id)]


# ---------------------------------------------------------------------------
# DELETE — clears curation, retains row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_resets_curated_fields_and_retains_row(client):
    measure_id = uuid.uuid4()
    measure = _measure(measure_id=measure_id)
    drill = _drill(
        measure_id=measure_id,
        source_table_id=uuid.uuid4(),
        detail_columns=[str(uuid.uuid4())],
        joined_dimension_ids=[str(uuid.uuid4())],
        row_limit_override=1000,
    )
    model = make_model()

    db = make_mock_db()
    db.get = _scripted_get(model=model, measure=measure)
    db.execute = _execute_script(_ScalarResult([drill]))

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{measure_id}/drill-through-set")

    assert resp.status_code == 200, resp.text
    assert drill.source_table_id is None
    assert drill.detail_columns is None
    assert drill.joined_dimension_ids is None
    assert drill.row_limit_override is None
    # The row itself is not deleted from the session.
    db.delete.assert_not_called()
