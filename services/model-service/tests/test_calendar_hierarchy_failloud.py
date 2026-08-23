"""Bug-6242: calendar endpoints must FAIL LOUD (rollback + 500) when the
date-hierarchy auto-generation raises, instead of swallowing it and returning a
success response over a committed-but-half-built calendar.

These guard the endpoint boundary that originally failed — the swallow lived in
``calendar.py``, not in ``_auto_create_date_hierarchies_for_model``. A revert to
``try/except: logger.warning`` around the hierarchy call would make these fail.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.calendar import (
    CalendarBindRequest,
    CalendarUndoRequest,
    auto_create_calendar,
    bind_calendar,
    undo_auto_created_calendar,
)
from shared.db.models import (
    CalendarHistoryProvenance,
    CalendarTable,
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
    Join,
    ModelColumn,
    ModelTable,
)
from .conftest import async_gen_from, make_mock_db

pytestmark = pytest.mark.unit


def _body():
    return types.SimpleNamespace(
        dialect="postgresql",
        calendar_type="standard",
        date_column="date_key",
        year_column=None,
        half_column=None,
        quarter_column=None,
        month_column=None,
        week_column=None,
        day_column=None,
        table_name="dim_date",
        alias=None,
        display_name=None,
        fiscal_year_start_month=1,
    )


@pytest.mark.asyncio
async def test_bind_calendar_fails_loud_and_rolls_back_on_hierarchy_error():
    db = make_mock_db()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    source = types.SimpleNamespace(id=uuid.uuid4())
    connection = types.SimpleNamespace(connection_type="postgresql", config={})
    alias_mt = types.SimpleNamespace(id=uuid.uuid4())

    with (
        patch("src.api.calendar._extract_bearer", return_value="tok"),
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.calendar._load_source_with_connection",
            new=AsyncMock(return_value=(source, connection)),
        ),
        patch("src.api.calendar._verify_table_exists", new=AsyncMock()),
        patch("src.api.calendar._verify_calendar_columns", new=AsyncMock()),
        patch(
            "src.api.calendar.qualify_physical_name",
            return_value="public.dim_date",
        ),
        patch(
            "src.api.calendar._create_calendar_alias",
            new=AsyncMock(return_value=alias_mt),
        ),
        patch(
            "src.api.calendar._auto_create_date_hierarchies_for_model",
            new=AsyncMock(side_effect=RuntimeError("hierarchy boom")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await bind_calendar(
                request=MagicMock(),
                project_id=uuid.uuid4(),
                model_id=uuid.uuid4(),
                source_id=source.id,
                body=_body(),
                current_user=types.SimpleNamespace(tenant_id="t1"),
            )

    # Fail-loud: the failure surfaces as a 500 (not a 200 success).
    assert exc.value.status_code == 500
    assert "date-hierarchy" in str(exc.value.detail).lower()
    # Atomic: the calendar changes are rolled back, never committed.
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_create_calendar_fails_loud_and_rolls_back_on_hierarchy_error():
    """The sibling auto-create/Generate endpoint has its own independent
    fail-loud block; a revert of only that block to a swallow must be caught
    here just as the bind path is."""
    from src.api.calendar import auto_create_calendar

    db = make_mock_db()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    # Bug-7208 moved the existing-calendar lookup before DDL execution,
    # adding a new db.execute call:
    # 1st execute = pre-DDL existing calendar lookup (None -> no type conflict);
    # 2nd execute = post-DDL "existing calendar" lookup (None -> create new);
    # 3rd execute = alias_mt_row lookup (must be non-None to reach the
    # hierarchy call).
    pre_ddl_none = MagicMock()
    pre_ddl_none.scalar_one_or_none.return_value = None
    existing_none = MagicMock()
    existing_none.scalar_one_or_none.return_value = None
    alias_row = MagicMock()
    alias_row.scalar_one_or_none.return_value = types.SimpleNamespace(id=uuid.uuid4())
    db.execute = AsyncMock(side_effect=[pre_ddl_none, existing_none, alias_row])

    source = types.SimpleNamespace(id=uuid.uuid4())
    connection = types.SimpleNamespace(
        connection_type="postgresql", config={"write_access": True}
    )

    auto_body = types.SimpleNamespace(
        table_name="dim_date",
        start_date="2020-01-01",
        end_date="2020-12-31",
        fiscal_year_start_month=1,
        calendar_type="standard",
        alias=None,
        display_name=None,
    )

    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.calendar._load_source_with_connection",
            new=AsyncMock(return_value=(source, connection)),
        ),
        patch("src.api.calendar._normalise_dialect", return_value="postgresql"),
        patch("src.api.calendar.DDL_CAPABLE_CONNECTORS", {"postgresql"}),
        patch(
            "src.api.calendar.qualify_physical_name",
            return_value="public.dim_date",
        ),
        patch(
            "src.api.calendar.emit_calendar_ddl",
            return_value="CREATE TABLE public.dim_date AS SELECT 1",
        ),
        patch("src.api.calendar._execute_ddl", new=AsyncMock()) as exec_ddl,
        patch(
            "src.api.calendar._create_calendar_alias",
            new=AsyncMock(return_value=types.SimpleNamespace(id=uuid.uuid4())),
        ),
        patch(
            "src.api.calendar._auto_create_date_hierarchies_for_model",
            new=AsyncMock(side_effect=RuntimeError("hierarchy boom")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await auto_create_calendar(
                project_id=uuid.uuid4(),
                model_id=uuid.uuid4(),
                source_id=source.id,
                body=auto_body,
                current_user=types.SimpleNamespace(tenant_id="t1"),
            )

    assert exc.value.status_code == 500
    assert "date-hierarchy" in str(exc.value.detail).lower()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    # F-016-01: the DESTRUCTIVE source DDL must NOT have run when hierarchy
    # generation failed. The DDL is deferred to AFTER hierarchy work, so a
    # hierarchy failure leaves the source table untouched — source and
    # metadata stay in sync (both at their prior state).
    exec_ddl.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_create_calendar_executes_ddl_only_after_metadata_ready():
    """F-016-01 success path: the source DDL must run AFTER the hierarchy work
    (deferred to just before commit), and the commit must follow the DDL, so a
    failure anywhere before the DDL cannot leave source and metadata divergent."""
    from src.api.calendar import auto_create_calendar

    call_order: list[str] = []

    db = make_mock_db()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    async def _commit():
        call_order.append("commit")
    db.commit = AsyncMock(side_effect=_commit)

    pre_ddl_none = MagicMock()
    pre_ddl_none.scalar_one_or_none.return_value = None
    existing_none = MagicMock()
    existing_none.scalar_one_or_none.return_value = None
    alias_row = MagicMock()
    alias_row.scalar_one_or_none.return_value = types.SimpleNamespace(id=uuid.uuid4())
    # 4th execute: time-dimension lookup in _invalidate_time_grained_aggregates
    time_dims = MagicMock()
    time_dims.all.return_value = []
    db.execute = AsyncMock(side_effect=[pre_ddl_none, existing_none, alias_row, time_dims])

    source = types.SimpleNamespace(id=uuid.uuid4())
    connection = types.SimpleNamespace(
        connection_type="postgresql", config={"write_access": True}
    )
    auto_body = types.SimpleNamespace(
        table_name="dim_date", start_date="2020-01-01", end_date="2020-12-31",
        fiscal_year_start_month=1, calendar_type="standard",
        alias=None, display_name=None,
    )

    async def _ddl(*a, **k):
        call_order.append("ddl")

    async def _hier(*a, **k):
        call_order.append("hierarchy")
        return (0, [], [])

    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.calendar._load_source_with_connection",
            new=AsyncMock(return_value=(source, connection)),
        ),
        patch("src.api.calendar._normalise_dialect", return_value="postgresql"),
        patch("src.api.calendar.DDL_CAPABLE_CONNECTORS", {"postgresql"}),
        patch("src.api.calendar.qualify_physical_name", return_value="public.dim_date"),
        patch(
            "src.api.calendar.emit_calendar_ddl",
            return_value="CREATE TABLE public.dim_date AS SELECT 1",
        ),
        patch("src.api.calendar._execute_ddl", new=AsyncMock(side_effect=_ddl)),
        patch(
            "src.api.calendar._create_calendar_alias",
            new=AsyncMock(return_value=types.SimpleNamespace(id=uuid.uuid4())),
        ),
        patch(
            "src.api.calendar._auto_create_date_hierarchies_for_model",
            new=AsyncMock(side_effect=_hier),
        ),
        patch(
            "src.api.calendar.CalendarTableResponse.model_validate",
            return_value=types.SimpleNamespace(auto_created_aliases=[]),
        ),
    ):
        await auto_create_calendar(
            project_id=uuid.uuid4(),
            model_id=uuid.uuid4(),
            source_id=source.id,
            body=auto_body,
            current_user=types.SimpleNamespace(tenant_id="t1"),
        )

    # Ordering invariant: hierarchy work first, THEN the destructive source
    # DDL, THEN the metadata commit.
    assert call_order == ["hierarchy", "ddl", "commit"]


@pytest.mark.asyncio
async def test_history_undo_requires_server_provenance_and_never_runs_source_ddl():
    """F7/F4: only the matching server token can remove auto-create metadata."""
    db = make_mock_db()
    model_id = uuid.uuid4()
    source_id = uuid.uuid4()
    calendar_id = uuid.uuid4()
    token = uuid.uuid4()
    cal = types.SimpleNamespace(
        id=calendar_id, data_source_id=source_id,
        table_name="public.dim_date", autocreated=True,
    )
    provenance = CalendarHistoryProvenance(
        token=token, model_id=model_id, data_source_id=source_id,
        calendar_id=calendar_id, physical_table="public.dim_date",
        generated_metadata={"auto_created_aliases": ["calendar"]},
    )
    alias = types.SimpleNamespace(id=uuid.uuid4())
    generated = types.SimpleNamespace(
        id=uuid.uuid4(), model_id=model_id,
        date_config={"calendar_table_id": str(calendar_id)},
    )
    results = iter([
        types.SimpleNamespace(scalar_one_or_none=lambda: provenance),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [alias])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [generated])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [])),
    ])
    db.get = AsyncMock(side_effect=lambda cls, value: cal if cls is CalendarTable else None)
    db.execute = AsyncMock(side_effect=lambda _stmt: next(results))
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    source = types.SimpleNamespace(id=source_id)
    connection = types.SimpleNamespace(connection_type="postgresql")

    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.calendar._load_source_with_connection", new=AsyncMock(return_value=(source, connection))),
        patch("src.api.calendar._execute_ddl", new=AsyncMock()) as execute_ddl,
    ):
        await undo_auto_created_calendar(
            project_id=uuid.uuid4(), model_id=model_id, source_id=source_id,
            calendar_id=calendar_id,
            body=CalendarUndoRequest(history_provenance=str(token)),
            current_user=types.SimpleNamespace(tenant_id="t1"),
        )

    assert provenance.calendar_id is None
    db.commit.assert_awaited_once()
    assert db.delete.await_count == 3  # join set empty, hierarchy + alias + calendar
    execute_ddl.assert_not_awaited()


@pytest.mark.asyncio
async def test_l13_r1_f4_exact_history_refuses_a_user_join_without_deleting_it():
    """Owned-ID history must not turn a later user join into an undo casualty."""
    model_id, source_id, calendar_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token = uuid.uuid4()
    alias_id, owned_join_id, user_join_id = (uuid.uuid4() for _ in range(3))
    cal = types.SimpleNamespace(
        id=calendar_id,
        data_source_id=source_id,
        table_name="public.dim_date",
        autocreated=True,
    )
    provenance = CalendarHistoryProvenance(
        token=token,
        model_id=model_id,
        data_source_id=source_id,
        calendar_id=calendar_id,
        physical_table="public.dim_date",
        generated_metadata={
            "calendar_created": True,
            "generated_model_table_ids": [str(alias_id)],
            "generated_hierarchy_ids": [],
            "generated_join_ids": [str(owned_join_id)],
            "generated_uda_ids": [],
            "generated_dimension_ids": [],
            "reused_model_tables": [],
        },
    )
    alias = types.SimpleNamespace(id=alias_id, model_id=model_id)
    user_join = types.SimpleNamespace(
        id=user_join_id,
        left_table_id=alias_id,
        right_table_id=uuid.uuid4(),
    )
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda cls, value: cal if cls is CalendarTable else None)
    db.execute = AsyncMock(side_effect=[
        types.SimpleNamespace(scalar_one_or_none=lambda: provenance),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [alias])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [user_join])),
    ])
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch(
            "src.api.calendar._load_source_with_connection",
            new=AsyncMock(return_value=(types.SimpleNamespace(id=source_id), types.SimpleNamespace())),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await undo_auto_created_calendar(
                project_id=uuid.uuid4(),
                model_id=model_id,
                source_id=source_id,
                calendar_id=calendar_id,
                body=CalendarUndoRequest(history_provenance=str(token)),
                current_user=types.SimpleNamespace(tenant_id="t1"),
            )

    assert exc.value.status_code == 409
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


class _CalendarStateResult:
    def __init__(self, rows: list[object], scalar: object | None = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def scalars(self):
        return _CalendarStateScalars(self._rows)

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar if self._scalar is not None else len(self._rows)


class _CalendarStateScalars:
    """Separate SQLAlchemy ``ScalarResult`` view for calendar-state rows."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)


class _CalendarStateDb:
    """Small behavior-faithful tenant DB for TP-9487-02's real mutators."""

    def __init__(self, *, calendar, tables, columns, hierarchy, levels, dimension, join):
        self.info = {"tenant_id": "tenant-a"}
        self.calendar = calendar
        self.tables = list(tables)
        self.columns = list(columns)
        self.hierarchies = [hierarchy]
        self.levels = list(levels)
        self.dimensions = [dimension]
        self.joins = [join]
        self.provenance = None
        self.commit_count = 0

    async def execute(self, statement):
        entity = next(
            (item.get("entity") for item in statement.column_descriptions if item.get("entity")),
            None,
        )
        sql = str(statement).lower()
        if entity is HierarchyDefinition and " is null" in sql:
            return _CalendarStateResult([])
        if "count(" in sql:
            if "model_tables" in sql:
                return _CalendarStateResult([], scalar=len(self.tables))
            return _CalendarStateResult([], scalar=0)
        if entity is CalendarTable:
            return _CalendarStateResult([self.calendar])
        if entity is CalendarHistoryProvenance:
            return _CalendarStateResult([self.provenance] if self.provenance else [])
        if entity is ModelTable:
            return _CalendarStateResult(self.tables)
        if entity is ModelColumn:
            params = statement.compile().params
            if "year_label" in params.values():
                return _CalendarStateResult(
                    [column for column in self.columns if column.column_name == "year_label"]
                )
            return _CalendarStateResult(self.columns)
        if entity is HierarchyDefinition:
            return _CalendarStateResult(self.hierarchies)
        if entity is HierarchyLevel:
            return _CalendarStateResult(self.levels)
        if entity is Dimension:
            if " is null" in sql:
                return _CalendarStateResult([])
            return _CalendarStateResult(self.dimensions)
        if entity is Join:
            return _CalendarStateResult(self.joins)
        return _CalendarStateResult([])

    async def get(self, entity, object_id):
        values = {
            CalendarTable: [self.calendar],
            ModelTable: self.tables,
            ModelColumn: self.columns,
            HierarchyDefinition: self.hierarchies,
            HierarchyLevel: self.levels,
            Dimension: self.dimensions,
            CalendarHistoryProvenance: [self.provenance] if self.provenance else [],
        }.get(entity, [])
        return next((item for item in values if item is not None and item.id == object_id), None)

    def add(self, value):
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        name = value.__class__.__name__
        if name == "CalendarHistoryProvenance":
            self.provenance = value
        elif name == "ModelColumn":
            self.columns.append(value)

    async def delete(self, value):
        for collection in (self.tables, self.columns, self.hierarchies, self.levels, self.dimensions, self.joins):
            if value in collection:
                collection.remove(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        return None

    async def refresh(self, _value):
        return None


@pytest.mark.asyncio
async def test_bug9487_tp02_existing_fiscal_generate_undo_redo_undo_exact_inverse():
    """TP-9487-02: real mutators make Generate/undo/redo/undo an exact inverse."""
    model_id, source_id, calendar_id = (uuid.uuid4() for _ in range(3))
    fact_table_id, fact_date_id = uuid.uuid4(), uuid.uuid4()
    spine_id, companion_id = uuid.uuid4(), uuid.uuid4()
    spine_date_id, spine_year_id, spine_month_id = (uuid.uuid4() for _ in range(3))
    companion_date_id, companion_year_id, companion_month_id = (uuid.uuid4() for _ in range(3))
    hierarchy_id, year_level_id, month_level_id = (uuid.uuid4() for _ in range(3))
    dimension_id, join_id = uuid.uuid4(), uuid.uuid4()
    legacy_year_id, legacy_month_id = uuid.uuid4(), uuid.uuid4()
    source = types.SimpleNamespace(id=source_id)
    connection = types.SimpleNamespace(connection_type="postgresql", config={"write_access": True})
    calendar = types.SimpleNamespace(
        id=calendar_id, data_source_id=source_id, table_name="public.dim_date",
        dialect="postgresql", calendar_type="fiscal", date_column="date_key",
        year_column="year_no", half_column=None, quarter_column=None,
        month_column="month_no", week_column=None, day_column=None,
        autocreated=False, fiscal_year_start_month=4,
    )

    def table(table_id, table_type, alias):
        return types.SimpleNamespace(
            id=table_id, model_id=model_id, source_id=source_id,
            calendar_table_id=calendar_id, table_type=table_type,
            physical_name="public.dim_date", alias=alias, display_name=alias,
        )

    spine = table(spine_id, "calendar", "calendar")
    companion = table(companion_id, "dim_detail", "order_date_calendar")
    fact = types.SimpleNamespace(
        id=fact_table_id, model_id=model_id, source_id=source_id,
        calendar_table_id=None, table_type="fact", physical_name="public.orders",
        alias="orders", display_name="Orders",
    )

    def column(column_id, table_id, name, data_type):
        return types.SimpleNamespace(
            id=column_id, model_table_id=table_id, column_name=name,
            display_name=name.replace("_", " ").title(), description=None,
            is_hidden=False, hidden_reason=None, is_primary_key=False,
            data_type=data_type, is_nullable=False,
        )

    fact_date = column(fact_date_id, fact_table_id, "order_date", "date")
    spine_columns = [
        column(spine_date_id, spine_id, "date_key", "date"),
        column(spine_year_id, spine_id, "year_no", "integer"),
        column(spine_month_id, spine_id, "month_no", "integer"),
    ]
    companion_columns = [
        column(companion_date_id, companion_id, "date_key", "date"),
        column(companion_year_id, companion_id, "year_no", "integer"),
        column(companion_month_id, companion_id, "month_no", "integer"),
    ]
    hierarchy = types.SimpleNamespace(
        id=hierarchy_id, model_id=model_id, name="Order Date", type="date_embedded",
        calendar_type="fiscal", date_config={
            "calendar_table_id": str(calendar_id),
            "source_attribute_id": str(fact_date_id),
        },
    )
    levels = [
        types.SimpleNamespace(
            id=year_level_id, hierarchy_id=hierarchy_id, name="Year", ordinal=0,
            key_attribute_id=legacy_year_id, key_attribute_source="user_defined_attribute",
            time_unit="year",
        ),
        types.SimpleNamespace(
            id=month_level_id, hierarchy_id=hierarchy_id, name="Month", ordinal=1,
            key_attribute_id=legacy_month_id, key_attribute_source="user_defined_attribute",
            time_unit="month",
        ),
    ]
    dimension = types.SimpleNamespace(
        id=dimension_id, model_id=model_id, name="order_date_year", is_time_dim=True,
        source_column_id=None, display_column_id=None,
        user_defined_attribute_id=legacy_year_id,
    )
    existing_join = types.SimpleNamespace(
        id=join_id, model_id=model_id, left_table_id=fact_table_id,
        right_table_id=companion_id, left_column_id=fact_date_id,
        right_column_id=companion_date_id,
    )
    db = _CalendarStateDb(
        calendar=calendar, tables=[fact, spine, companion],
        columns=[fact_date, *spine_columns, *companion_columns], hierarchy=hierarchy,
        levels=levels, dimension=dimension, join=existing_join,
    )

    def snapshot_generated_state():
        return {
            "calendar": tuple(
                getattr(calendar, field)
                for field in (
                    "dialect", "calendar_type", "date_column", "year_column",
                    "half_column", "quarter_column", "month_column", "week_column",
                    "day_column", "autocreated", "fiscal_year_start_month",
                )
            ),
            "year_label_columns": tuple(sorted(
                (str(item.model_table_id), item.column_name,
                 item.display_name, item.description, item.is_hidden, item.hidden_reason,
                 item.is_primary_key, item.data_type, item.is_nullable)
                for item in db.columns
                if item.column_name == "year_label"
            )),
            "dimension": (
                str(dimension.id), dimension.source_column_id and str(dimension.source_column_id),
                dimension.display_column_id and str(dimension.display_column_id),
                dimension.user_defined_attribute_id and str(dimension.user_defined_attribute_id),
            ),
            "hierarchy_levels": tuple(sorted(
                (str(item.id), str(item.key_attribute_id), item.key_attribute_source, item.time_unit)
                for item in db.levels
            )),
        }

    before = snapshot_generated_state()
    before_display_column_id = dimension.display_column_id
    body = types.SimpleNamespace(
        table_name="dim_date", start_date="2020-01-01", end_date="2020-12-31",
        fiscal_year_start_month=4, calendar_type="fiscal", alias=None, display_name=None,
    )
    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.calendar._load_source_with_connection", new=AsyncMock(return_value=(source, connection))),
        patch("src.api.calendar._normalise_dialect", return_value="postgresql"),
        patch("src.api.calendar.DDL_CAPABLE_CONNECTORS", {"postgresql"}),
        patch("src.api.calendar.qualify_physical_name", return_value="public.dim_date"),
        patch("src.api.calendar.emit_calendar_ddl", return_value="CREATE TABLE public.dim_date AS SELECT 1"),
        patch("src.api.calendar._execute_ddl", new_callable=AsyncMock) as execute_ddl,
        patch("src.api.calendar._effective_year_label_format", new=AsyncMock(return_value="span_short")),
        patch("src.api.calendar._auto_create_date_hierarchies_for_model", new=AsyncMock(return_value=(0, [], []))),
        patch("src.api.calendar._invalidate_time_grained_aggregates", new=AsyncMock()),
        patch("src.api.calendar.CalendarTableResponse.model_validate", return_value=types.SimpleNamespace()),
    ):
        await auto_create_calendar(
            project_id=uuid.uuid4(), model_id=model_id, source_id=source_id,
            body=body, current_user=types.SimpleNamespace(tenant_id="tenant-a"),
        )

    assert db.provenance is not None
    token = db.provenance.token
    generated = snapshot_generated_state()
    generated_provenance = repr(db.provenance.generated_metadata)
    assert generated != before
    assert any(item.column_name == "year_label" for item in db.columns)
    assert year_level_id and next(item for item in db.levels if item.id == year_level_id).key_attribute_source == "physical_column"
    assert next(item for item in db.levels if item.id == year_level_id).key_attribute_id == companion_year_id
    assert next(item for item in db.levels if item.id == month_level_id).key_attribute_id == companion_month_id
    assert dimension.source_column_id == companion_year_id
    assert dimension.display_column_id in {item.id for item in db.columns if item.column_name == "year_label"}
    generated_display_column_id = dimension.display_column_id
    assert db.provenance.generated_metadata["reused_join_ids"] == [str(join_id)]
    assert execute_ddl.await_count == 1

    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.calendar._load_source_with_connection", new=AsyncMock(return_value=(source, connection))),
        patch("src.api.calendar._invalidate_time_grained_aggregates", new=AsyncMock()),
        patch("src.api.calendar._delete_unreferenced_generated_udas", new=AsyncMock()),
    ):
        await undo_auto_created_calendar(
            project_id=uuid.uuid4(), model_id=model_id, source_id=source_id,
            calendar_id=calendar_id,
            body=CalendarUndoRequest(history_provenance=str(token)),
            current_user=types.SimpleNamespace(tenant_id="tenant-a"),
        )
    assert snapshot_generated_state() == before
    assert dimension.display_column_id == before_display_column_id
    assert db.provenance.calendar_id is None
    assert repr(db.provenance.generated_metadata) == generated_provenance
    assert db.joins == [existing_join]

    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.calendar._load_source_with_connection", new=AsyncMock(return_value=(source, connection))),
        patch("src.api.calendar.qualify_physical_name", return_value="public.dim_date"),
        patch("src.api.calendar._verify_table_exists", new=AsyncMock()),
        patch("src.api.calendar._verify_calendar_columns", new=AsyncMock()),
        patch("src.api.calendar._calendar_has_year_label", new=AsyncMock(return_value=True)),
        patch("src.api.calendar._auto_create_date_hierarchies_for_model", new=AsyncMock(return_value=(0, [], []))),
        patch("src.api.calendar._invalidate_time_grained_aggregates", new=AsyncMock()),
        patch("src.api.calendar.CalendarTableResponse.model_validate", return_value=types.SimpleNamespace()),
    ):
        response = await bind_calendar(
            request=MagicMock(), project_id=uuid.uuid4(), model_id=model_id,
            source_id=source_id,
            body=CalendarBindRequest(
                table_name="dim_date", dialect="postgresql", date_column="date_key",
                    year_column="year_no", half_column="half_no", quarter_column="quarter_no",
                    month_column="month_no", week_column="week_no", day_column="day_no",
                    calendar_type="fiscal",
                fiscal_year_start_month=4, history_provenance=str(token),
            ),
            current_user=types.SimpleNamespace(tenant_id="tenant-a"),
        )
    assert response.history_provenance == {"token": str(token)}
    redo_snapshot = snapshot_generated_state()
    assert redo_snapshot["calendar"] == generated["calendar"]
    assert redo_snapshot["year_label_columns"] == generated["year_label_columns"]
    assert redo_snapshot["hierarchy_levels"] == generated["hierarchy_levels"]
    assert redo_snapshot["dimension"][:2] == generated["dimension"][:2]
    assert redo_snapshot["dimension"][3] == generated["dimension"][3]
    assert db.provenance.calendar_id == calendar_id
    assert dimension.display_column_id in {item.id for item in db.columns if item.column_name == "year_label"}
    assert dimension.display_column_id != generated_display_column_id
    assert db.provenance.generated_metadata["reused_join_ids"] == [str(join_id)]
    assert {record["id"] for record in db.provenance.generated_metadata["hierarchy_level_keys"]} == {
        str(year_level_id), str(month_level_id),
    }
    redo_provenance = repr(db.provenance.generated_metadata)
    assert execute_ddl.await_count == 1

    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.calendar._load_source_with_connection", new=AsyncMock(return_value=(source, connection))),
        patch("src.api.calendar._invalidate_time_grained_aggregates", new=AsyncMock()),
        patch("src.api.calendar._delete_unreferenced_generated_udas", new=AsyncMock()),
    ):
        await undo_auto_created_calendar(
            project_id=uuid.uuid4(), model_id=model_id, source_id=source_id,
            calendar_id=calendar_id,
            body=CalendarUndoRequest(history_provenance=str(token)),
            current_user=types.SimpleNamespace(tenant_id="tenant-a"),
        )
    assert snapshot_generated_state() == before
    assert dimension.display_column_id == before_display_column_id
    assert db.provenance.calendar_id is None
    assert repr(db.provenance.generated_metadata) == redo_provenance
    assert db.joins == [existing_join]
    assert execute_ddl.await_count == 1


@pytest.mark.asyncio
async def test_l13_r1_f4_exact_history_undo_deletes_owned_metadata_and_invalidates():
    """F4: undo removes generated aliases/joins/hierarchies and preserves source data."""
    model_id, source_id, calendar_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token = uuid.uuid4()
    alias_id, hierarchy_id, join_id, dimension_id, uda_id = (
        uuid.uuid4() for _ in range(5)
    )
    cal = types.SimpleNamespace(
        id=calendar_id,
        data_source_id=source_id,
        table_name="public.dim_date",
        autocreated=True,
    )
    provenance = CalendarHistoryProvenance(
        token=token,
        model_id=model_id,
        data_source_id=source_id,
        calendar_id=calendar_id,
        physical_table="public.dim_date",
        generated_metadata={
            "calendar_created": True,
            "generated_model_table_ids": [str(alias_id)],
            "generated_hierarchy_ids": [str(hierarchy_id)],
            "generated_join_ids": [str(join_id)],
            "generated_uda_ids": [str(uda_id)],
            "generated_dimension_ids": [str(dimension_id)],
            "reused_model_tables": [],
        },
    )
    alias = types.SimpleNamespace(id=alias_id, model_id=model_id)
    owned_join = types.SimpleNamespace(
        id=join_id,
        left_table_id=alias_id,
        right_table_id=uuid.uuid4(),
    )
    hierarchy = types.SimpleNamespace(id=hierarchy_id, model_id=model_id)
    dimension = types.SimpleNamespace(id=dimension_id, model_id=model_id)
    db = make_mock_db()
    db.get = AsyncMock(side_effect=lambda cls, value: cal if cls is CalendarTable else None)
    db.execute = AsyncMock(side_effect=[
        types.SimpleNamespace(scalar_one_or_none=lambda: provenance),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [alias])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [owned_join])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [dimension])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [hierarchy])),
    ])
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    deleted: list[object] = []
    db.delete.side_effect = lambda obj: deleted.append(obj)
    invalidate = AsyncMock(return_value=1)
    delete_udas = AsyncMock(return_value=[uda_id])
    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch(
            "src.api.calendar._load_source_with_connection",
            new=AsyncMock(return_value=(types.SimpleNamespace(id=source_id), types.SimpleNamespace())),
        ),
        patch("src.api.calendar._invalidate_time_grained_aggregates", new=invalidate),
        patch("src.api.calendar._delete_unreferenced_generated_udas", new=delete_udas),
        patch("src.api.calendar._execute_ddl", new=AsyncMock()) as execute_ddl,
    ):
        await undo_auto_created_calendar(
            project_id=uuid.uuid4(),
            model_id=model_id,
            source_id=source_id,
            calendar_id=calendar_id,
            body=CalendarUndoRequest(history_provenance=str(token)),
            current_user=types.SimpleNamespace(tenant_id="t1"),
        )

    assert {getattr(obj, "id", None) for obj in deleted} == {
        alias_id, hierarchy_id, join_id, dimension_id, calendar_id,
    }
    delete_udas.assert_awaited_once()
    assert delete_udas.await_args.kwargs["candidate_uda_ids"] == [uda_id]
    invalidate.assert_awaited_once_with(db, model_id=model_id)
    assert provenance.calendar_id is None
    db.commit.assert_awaited_once()
    execute_ddl.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_undo_rejects_manual_calendar_without_mutation():
    """A client flow flag cannot authorize undo of a manually bound calendar."""
    db = make_mock_db()
    model_id, source_id, calendar_id, token = (uuid.uuid4() for _ in range(4))
    cal = types.SimpleNamespace(
        id=calendar_id, data_source_id=source_id,
        table_name="public.dim_date", autocreated=False,
    )
    provenance = CalendarHistoryProvenance(
        token=token, model_id=model_id, data_source_id=source_id,
        calendar_id=calendar_id, physical_table="public.dim_date",
        generated_metadata={},
    )
    result = types.SimpleNamespace(scalar_one_or_none=lambda: provenance)
    db.get = AsyncMock(side_effect=lambda cls, value: cal if cls is CalendarTable else None)
    db.execute = AsyncMock(return_value=result)
    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.calendar._load_source_with_connection", new=AsyncMock(return_value=(types.SimpleNamespace(id=source_id), types.SimpleNamespace()))),
    ):
        with pytest.raises(HTTPException) as exc:
            await undo_auto_created_calendar(
                project_id=uuid.uuid4(), model_id=model_id, source_id=source_id,
                calendar_id=calendar_id,
                body=CalendarUndoRequest(history_provenance=str(token)),
                current_user=types.SimpleNamespace(tenant_id="t1"),
            )
    assert exc.value.status_code == 409
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_rebind_then_second_undo_reuses_server_token():
    """A legitimate redo reattaches provenance, enabling the next undo."""
    model_id, source_id = uuid.uuid4(), uuid.uuid4()
    token = uuid.uuid4()
    provenance = CalendarHistoryProvenance(
        token=token, model_id=model_id, data_source_id=source_id,
        calendar_id=None, physical_table="public.dim_date",
        generated_metadata={
            "dialect": "postgresql", "calendar_type": "standard",
            "date_column": "date_key", "year_column": None,
        },
    )
    db = make_mock_db()
    source = types.SimpleNamespace(id=source_id)
    connection = types.SimpleNamespace(connection_type="postgresql")
    existing_none = types.SimpleNamespace(scalar_one_or_none=lambda: None)
    alias_count_zero = types.SimpleNamespace(scalar=lambda: 0)
    db.execute = AsyncMock(side_effect=[
        types.SimpleNamespace(scalar_one_or_none=lambda: provenance),
        existing_none,
        alias_count_zero,
    ])
    db.get = AsyncMock(return_value=None)
    def _add(obj):
        if isinstance(obj, CalendarTable) and obj.id is None:
            obj.id = uuid.uuid4()
    db.add.side_effect = _add
    alias = types.SimpleNamespace(id=uuid.uuid4())
    with (
        patch("src.api.calendar._extract_bearer", return_value="tok"),
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.calendar._load_source_with_connection", new=AsyncMock(return_value=(source, connection))),
        patch("src.api.calendar._verify_table_exists", new=AsyncMock()),
        patch("src.api.calendar._verify_calendar_columns", new=AsyncMock()),
        patch("src.api.calendar.qualify_physical_name", return_value="public.dim_date"),
        patch("src.api.calendar._create_calendar_alias", new=AsyncMock(return_value=alias)),
        patch("src.api.calendar._auto_create_date_hierarchies_for_model", new=AsyncMock(return_value=(0, [], ["calendar"]))),
        patch("src.api.calendar.CalendarTableResponse.model_validate", return_value=types.SimpleNamespace(auto_created_aliases=[])),
    ):
        from src.api.calendar import bind_calendar
        response = await bind_calendar(
            request=MagicMock(), project_id=uuid.uuid4(), model_id=model_id,
            source_id=source_id,
            body=CalendarBindRequest(
                table_name="dim_date", dialect="postgresql", date_column="date_key",
                calendar_type="standard", history_provenance=str(token),
            ),
            current_user=types.SimpleNamespace(tenant_id="t1"),
        )
    assert response.history_provenance == {"token": str(token)}
    assert provenance.calendar_id is not None
    rebound_calendar_id = provenance.calendar_id

    # The same server record now authorizes the second undo; client flow flags
    # are absent from this call entirely.
    cal = types.SimpleNamespace(
        id=rebound_calendar_id, data_source_id=source_id,
        table_name="public.dim_date", autocreated=True,
    )
    db.get = AsyncMock(return_value=cal)
    db.execute = AsyncMock(side_effect=[
        types.SimpleNamespace(scalar_one_or_none=lambda: provenance),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [alias])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [])),
        types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: [])),
    ])
    db.delete = AsyncMock()
    with (
        patch("src.api.calendar.get_tenant_db", async_gen_from(db)),
        patch("src.api.calendar._ensure_model_in_project", new=AsyncMock()),
        patch("src.api.calendar.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.calendar._load_source_with_connection", new=AsyncMock(return_value=(source, connection))),
    ):
        await undo_auto_created_calendar(
            project_id=uuid.uuid4(), model_id=model_id, source_id=source_id,
            calendar_id=rebound_calendar_id,
            body=CalendarUndoRequest(history_provenance=str(token)),
            current_user=types.SimpleNamespace(tenant_id="t1"),
        )
    assert provenance.calendar_id is None
    assert db.commit.await_count == 2  # atomic bind+provenance, then second undo
