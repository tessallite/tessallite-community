"""Hierarchy/UDA lifecycle tests (H10 — F-016-06, F-016-07).

F-016-06: a generated UDA's system-authored expression (EXTRACT/CASE) must
round-trip through the validator on a rename/description edit instead of being
rejected by the user-editor function allow-list.

F-016-07: deleting a hierarchy (or one of its levels) must not hard-delete a
level-key UDA that another hierarchy level still references.
"""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException
from sqlglot import exp

from src.api.user_defined_attributes import _parse_expression, _validate_ast
from src.api.hierarchies import _delete_unreferenced_generated_udas
from src.api._uda_refs import assert_uda_deletable

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# F-016-06 — generated expressions round-trip the validator
# ---------------------------------------------------------------------------

def _table() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        alias="payments",
        display_name="Payments",
        physical_name="public.payments",
    )


def _col(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=uuid.uuid4(), column_name=name)


def test_generated_extract_rejected_without_skip():
    """A generated EXTRACT/CASE expression hits the user allow-list and 422s."""
    table = _table()
    cols = {"payment_date": _col("payment_date")}
    ast = _parse_expression("EXTRACT(YEAR FROM (payment_date))")
    with pytest.raises(HTTPException) as exc:
        _validate_ast(ast, table=table, columns_by_name=cols)
    assert exc.value.status_code == 422
    assert "Unsupported function" in str(exc.value.detail)


def test_generated_extract_allowed_when_skip_allowlist():
    """With skip_function_allowlist the generator's own expression validates,
    while the structural guards (column resolution) still run."""
    table = _table()
    cols = {"payment_date": _col("payment_date")}
    ast = _parse_expression("EXTRACT(YEAR FROM (payment_date))")
    result = _validate_ast(
        ast, table=table, columns_by_name=cols, skip_function_allowlist=True
    )
    assert result.parse_valid is True
    assert result.referenced_columns == ["payment_date"]


def test_generated_case_half_year_allowed_when_skip_allowlist():
    table = _table()
    cols = {"payment_date": _col("payment_date")}
    ast = _parse_expression(
        "CASE WHEN EXTRACT(MONTH FROM (payment_date)) <= 6 THEN 1 ELSE 2 END"
    )
    result = _validate_ast(
        ast, table=table, columns_by_name=cols, skip_function_allowlist=True
    )
    assert result.parse_valid is True
    assert result.referenced_columns == ["payment_date"]


def test_skip_allowlist_still_resolves_columns():
    """Skipping the function allow-list does NOT skip column resolution: an
    unknown column still fails so a generated UDA cannot reference a column the
    table no longer has."""
    table = _table()
    cols = {"payment_date": _col("payment_date")}
    ast = _parse_expression("EXTRACT(YEAR FROM (nonexistent_col))")
    with pytest.raises(HTTPException) as exc:
        _validate_ast(
            ast, table=table, columns_by_name=cols, skip_function_allowlist=True
        )
    assert exc.value.status_code == 422
    assert "not found" in str(exc.value.detail)


def test_skip_allowlist_still_blocks_subqueries():
    """Structural guards (no subquery) survive the allow-list skip."""
    table = _table()
    cols = {"payment_date": _col("payment_date")}
    # A scalar subquery inside the expression.
    ast = _parse_expression("(SELECT 1)")
    with pytest.raises(HTTPException) as exc:
        _validate_ast(
            ast, table=table, columns_by_name=cols, skip_function_allowlist=True
        )
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# F-016-07 — shared-UDA-aware deletion
# ---------------------------------------------------------------------------

class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _FakeDB:
    """Minimal async DB stub that answers the reference-scan SELECTs and records
    DELETEs. Reference state is supplied as id sets per producer; the helper's
    SELECTs are routed by the selected column's parent table/name."""

    def __init__(self, *, level_keys, level_attrs, measure_refs):
        self._level_keys = set(level_keys)
        self._level_attrs = set(level_attrs)
        self._measure_refs = set(measure_refs)
        self.deleted_dimensions: list[uuid.UUID] = []
        self.deleted_uda_refs: list[uuid.UUID] = []
        self.deleted_udas: list[uuid.UUID] = []

    async def execute(self, stmt):
        compiled = str(stmt)
        if stmt.is_select:
            # Identify which producer is being scanned by the FROM table name.
            if "hierarchy_levels" in compiled and "key_attribute_id" in compiled:
                return _Rows([(i,) for i in self._level_keys])
            if "hierarchy_level_attributes" in compiled:
                return _Rows([(i,) for i in self._level_attrs])
            if "measures" in compiled:
                return _Rows([(i,) for i in self._measure_refs])
            return _Rows([])
        # DELETE statements — record the target table.
        table_name = stmt.table.name
        if table_name == "dimensions":
            self.deleted_dimensions.append("dimensions")
        elif table_name == "user_defined_attribute_column_refs":
            self.deleted_uda_refs.append("refs")
        elif table_name == "user_defined_attributes":
            self.deleted_udas.append("udas")
        return _Rows([])


@pytest.mark.asyncio
async def test_shared_uda_is_kept_when_referenced_by_sibling_level():
    """A level-key UDA still keyed by another hierarchy's surviving level is not
    deleted (F-016-07 core repro)."""
    model_id = uuid.uuid4()
    shared = uuid.uuid4()
    orphan = uuid.uuid4()
    # After deleting hierarchy A, sibling hierarchy B still keys `shared`.
    db = _FakeDB(level_keys={shared}, level_attrs=set(), measure_refs=set())
    deleted = await _delete_unreferenced_generated_udas(
        db, model_id=model_id, candidate_uda_ids=[shared, orphan]
    )
    assert deleted == [orphan]
    assert db.deleted_udas == ["udas"]  # only the orphan batch
    assert db.deleted_dimensions == ["dimensions"]


@pytest.mark.asyncio
async def test_all_udas_deleted_when_unreferenced():
    model_id = uuid.uuid4()
    a = uuid.uuid4()
    b = uuid.uuid4()
    db = _FakeDB(level_keys=set(), level_attrs=set(), measure_refs=set())
    deleted = await _delete_unreferenced_generated_udas(
        db, model_id=model_id, candidate_uda_ids=[a, b]
    )
    assert set(deleted) == {a, b}
    assert db.deleted_udas == ["udas"]


@pytest.mark.asyncio
async def test_measure_reference_pins_uda():
    model_id = uuid.uuid4()
    pinned = uuid.uuid4()
    db = _FakeDB(level_keys=set(), level_attrs=set(), measure_refs={pinned})
    deleted = await _delete_unreferenced_generated_udas(
        db, model_id=model_id, candidate_uda_ids=[pinned]
    )
    assert deleted == []
    assert db.deleted_udas == []
    assert db.deleted_dimensions == []


@pytest.mark.asyncio
async def test_level_attribute_reference_pins_uda():
    model_id = uuid.uuid4()
    pinned = uuid.uuid4()
    db = _FakeDB(level_keys=set(), level_attrs={pinned}, measure_refs=set())
    deleted = await _delete_unreferenced_generated_udas(
        db, model_id=model_id, candidate_uda_ids=[pinned]
    )
    assert deleted == []


@pytest.mark.asyncio
async def test_empty_candidate_list_is_noop():
    db = _FakeDB(level_keys=set(), level_attrs=set(), measure_refs=set())
    deleted = await _delete_unreferenced_generated_udas(
        db, model_id=uuid.uuid4(), candidate_uda_ids=[]
    )
    assert deleted == []
    assert db.deleted_udas == []


# ---------------------------------------------------------------------------
# Bug-1505 / F-016-08 — single-UDA delete guard (table_attributes +
# user_defined_attributes both route through assert_uda_deletable)
# ---------------------------------------------------------------------------

class _RefScanDB:
    """Async DB stub for the assert_uda_deletable reference scan.

    Routes the four reference SELECTs by their compiled FROM/columns and
    returns name pairs so the helper can build its rejection message.
    """

    def __init__(self, *, dims=(), measures=(), level_keys=(), level_attrs=()):
        self._dims = list(dims)
        self._measures = list(measures)
        self._level_keys = list(level_keys)  # list of (hierarchy, level)
        self._level_attrs = list(level_attrs)  # list of (hierarchy, level)

    async def execute(self, stmt):
        compiled = str(stmt)
        if "hierarchy_level_attributes" in compiled:
            return _Rows(self._level_attrs)
        if "hierarchy_levels" in compiled and "key_attribute_id" in compiled:
            return _Rows(self._level_keys)
        if "FROM measures" in compiled or "measures.name" in compiled:
            return _Rows([(m,) for m in self._measures])
        if "FROM dimensions" in compiled or "dimensions.name" in compiled:
            return _Rows([(d,) for d in self._dims])
        return _Rows([])


@pytest.mark.asyncio
async def test_delete_guard_rejects_hierarchy_level_keyed_uda():
    """A generated UDA that keys a hierarchy level cannot be deleted via the
    shared guard — the path the table-attributes Delete button reaches."""
    db = _RefScanDB(level_keys=[("Order Date", "Year")])
    with pytest.raises(HTTPException) as exc:
        await assert_uda_deletable(
            db, model_id=uuid.uuid4(), attribute_id=uuid.uuid4()
        )
    assert exc.value.status_code == 409
    assert "hierarchy levels (key)" in str(exc.value.detail)
    assert "Order Date.Year" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_delete_guard_rejects_hierarchy_level_attribute_uda():
    """A UDA attached as a hierarchy-level display/filter attribute is pinned."""
    db = _RefScanDB(level_attrs=[("Order Date", "Month")])
    with pytest.raises(HTTPException) as exc:
        await assert_uda_deletable(
            db, model_id=uuid.uuid4(), attribute_id=uuid.uuid4()
        )
    assert exc.value.status_code == 409
    assert "hierarchy levels (attribute)" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_delete_guard_rejects_dimension_or_measure_ref():
    """The pre-existing dimension/measure guard still rejects."""
    db = _RefScanDB(dims=["Customer"], measures=["Revenue"])
    with pytest.raises(HTTPException) as exc:
        await assert_uda_deletable(
            db, model_id=uuid.uuid4(), attribute_id=uuid.uuid4()
        )
    assert exc.value.status_code == 409
    assert "dimensions: Customer" in str(exc.value.detail)
    assert "measures: Revenue" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_delete_guard_allows_unreferenced_uda():
    """An unreferenced UDA passes the guard with no exception."""
    db = _RefScanDB()
    # No raise == deletable.
    await assert_uda_deletable(db, model_id=uuid.uuid4(), attribute_id=uuid.uuid4())
