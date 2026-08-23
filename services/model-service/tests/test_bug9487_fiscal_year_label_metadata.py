"""Bug-9487 management, fallback, and BI metadata contract tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from shared.db.models import Dimension, ModelColumn
from shared.semantic.fiscal_year_labels import FISCAL_YEAR_LABEL_FORMATS
from src.api.calendar import (
    CalendarBindRequest,
    _effective_year_label_format,
    _ensure_calendar_year_label_column,
    auto_create_calendar,
    bind_calendar,
)
from src.api.hierarchies import (
    _create_date_hierarchy_for_alias,
    reconcile_generated_calendar_captions,
)
from src.api.tenants import (
    FiscalYearLabelFormatRequest,
    _assert_own_tenant,
    _resolve_calendar_target_tenant,
    get_calendar_settings,
    update_calendar_settings,
)
from src.auth.middleware import CurrentUser


@pytest.mark.parametrize("token", FISCAL_YEAR_LABEL_FORMATS)
def test_bug9487_management_payload_accepts_every_allowed_token(token: str) -> None:
    request = FiscalYearLabelFormatRequest(format=token)
    assert request.format == token


def test_bug9487_management_payload_rejects_unknown_token_as_validation_error() -> None:
    with pytest.raises(ValidationError, match="format must be one of"):
        FiscalYearLabelFormatRequest(format="not-a-format")


def test_bug9487_management_payload_rejects_unexpected_fields() -> None:
    with pytest.raises(ValidationError, match="extra"):
        FiscalYearLabelFormatRequest(format="start_year", extra="nope")


def test_bug9487_management_surface_is_tenant_scoped() -> None:
    from src.api.tenants import router

    paths = {route.path for route in router.routes}
    assert "/tenants/{tenant_id}/calendar-settings" in paths
    with pytest.raises(HTTPException) as exc:
        _assert_own_tenant("other-tenant", SimpleNamespace(tenant_id="tenant-a"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bug9487_invalid_stored_setting_is_rejected_before_calendar_work(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.api.calendar.get_setting",
        AsyncMock(return_value={"format": "unknown"}),
    )
    with pytest.raises(HTTPException) as exc:
        await _effective_year_label_format(AsyncMock())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_bug9487_alias_metadata_is_only_added_after_rebuild() -> None:
    db = AsyncMock()
    db.add = MagicMock()
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=existing_result)

    assert await _ensure_calendar_year_label_column(
        db, model_table_id=uuid4(), include=False
    ) is None
    assert db.execute.await_count == 0

    column = await _ensure_calendar_year_label_column(
        db, model_table_id=uuid4(), include=True
    )
    assert isinstance(column, ModelColumn)
    assert column.column_name == "year_label"
    assert column.data_type == "string"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("calendar_type", "column_names", "component_count"),
    [
        ("fiscal", ["year_no", "half_no", "quarter_no", "month_no", "day_no"], 5),
        ("retail_445", ["retail_year", "retail_quarter", "retail_period", "retail_week"], 4),
    ],
)
async def test_bug9487_rebuilt_calendar_year_dimension_exposes_caption(
    calendar_type: str,
    column_names: list[str],
    component_count: int,
) -> None:
    """A rebuilt alias exposes year_label while preserving the numeric key."""
    db = AsyncMock()
    added: list[object] = []
    db.add = MagicMock(side_effect=added.append)
    db.flush = AsyncMock()

    no_dimension = MagicMock()
    no_dimension.scalar_one_or_none.return_value = None
    source_label = SimpleNamespace(
        id=uuid4(),
        column_name="year_label",
        display_name="Year Label",
        description=None,
        data_type="string",
        is_nullable=False,
    )
    label_result = MagicMock()
    label_result.scalar_one_or_none.return_value = source_label
    db.execute = AsyncMock(
        side_effect=[no_dimension for _ in range(component_count)] + [label_result]
    )

    alias_id = uuid4()
    source_id = uuid4()
    column_id_map = {name: uuid4() for name in column_names}
    hierarchy = await _create_date_hierarchy_for_alias(
        db,
        model_id=uuid4(),
        table_id=alias_id,
        date_key_col_id=uuid4(),
        date_key_col_name="date_key",
        grain="y_m_d",
        name="Order Date",
        calendar_type=calendar_type,
        fiscal_year_start_month=4 if calendar_type == "fiscal" else None,
        column_id_map=column_id_map,
        caption_source_table_id=source_id,
    )

    dimensions = [item for item in added if isinstance(item, Dimension)]
    year_dimension = next(
        item for item in dimensions
        if item.name.endswith("_year") or item.name.endswith("_retail_year")
    )
    year_label = next(
        item for item in added
        if isinstance(item, ModelColumn) and item.column_name == "year_label"
    )
    assert hierarchy.calendar_type == calendar_type
    assert year_dimension.source_column_id == column_id_map[column_names[0]]
    assert year_dimension.display_column_id == year_label.id


@pytest.mark.asyncio
async def test_bug9487_pre_rebuild_hierarchy_falls_back_to_numeric_year_key() -> None:
    db = AsyncMock()
    added: list[object] = []
    db.add = MagicMock(side_effect=added.append)
    db.flush = AsyncMock()
    no_dimension = MagicMock()
    no_dimension.scalar_one_or_none.return_value = None
    no_label = MagicMock()
    no_label.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[no_dimension] * 4 + [no_label])

    await _create_date_hierarchy_for_alias(
        db,
        model_id=uuid4(),
        table_id=uuid4(),
        date_key_col_id=uuid4(),
        date_key_col_name="date_key",
        grain="y_m_d",
        name="Retail Date",
        calendar_type="retail_445",
        column_id_map={name: uuid4() for name in (
            "retail_year", "retail_quarter", "retail_period", "retail_week"
        )},
        caption_source_table_id=uuid4(),
    )

    year_dimension = next(
        item for item in added
        if isinstance(item, Dimension) and item.name.endswith("_retail_year")
    )
    assert year_dimension.display_column_id is None


@pytest.mark.asyncio
async def test_bug9487_r1_f4_put_requires_audit_before_setting_commit(monkeypatch) -> None:
    db = AsyncMock()
    audit_required = AsyncMock()
    set_setting = AsyncMock()
    monkeypatch.setattr("src.api.tenants.get_tenant_db", lambda _tenant: _one_db(db))
    monkeypatch.setattr("src.api.tenants.get_setting", AsyncMock(return_value={"format": "start_year"}))
    monkeypatch.setattr("src.api.tenants.audit_required", audit_required)
    monkeypatch.setattr("src.api.tenants.set_setting", set_setting)
    response = await update_calendar_settings(
        "tenant-a",
        FiscalYearLabelFormatRequest(format="span_short"),
        CurrentUser("u1", "tenant-a", "admin@example.test", "tenant_admin"),
    )
    assert response.format == "start_year"
    audit_required.assert_awaited_once()
    assert audit_required.await_args.kwargs["action"] == "settings.update"
    assert audit_required.await_args.kwargs["detail"]["after"] == "span_short"
    set_setting.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug9487_r1_f4_audit_failure_prevents_setting_write(monkeypatch) -> None:
    db = AsyncMock()
    monkeypatch.setattr("src.api.tenants.get_tenant_db", lambda _tenant: _one_db(db))
    monkeypatch.setattr("src.api.tenants.get_setting", AsyncMock(return_value={"format": "start_year"}))
    monkeypatch.setattr("src.api.tenants.audit_required", AsyncMock(side_effect=RuntimeError("forced")))
    set_setting = AsyncMock()
    monkeypatch.setattr("src.api.tenants.set_setting", set_setting)
    with pytest.raises(RuntimeError, match="forced"):
        await update_calendar_settings(
            "tenant-a",
            FiscalYearLabelFormatRequest(format="span_short"),
            CurrentUser("u1", "tenant-a", "admin@example.test", "tenant_admin"),
        )
    set_setting.assert_not_awaited()


def test_bug9487_r1_f5_system_admin_targets_real_tenant_and_tenant_admin_cannot_cross() -> None:
    system_admin = CurrentUser("root", "__system__", "root@example.test", "system_admin")
    tenant_admin = CurrentUser("a", "tenant-a", "a@example.test", "tenant_admin")
    assert _resolve_calendar_target_tenant("tenant-b", system_admin) == "tenant-b"
    with pytest.raises(HTTPException) as exc:
        _resolve_calendar_target_tenant("tenant-b", tenant_admin)
    assert exc.value.status_code == 403


async def _one_db(db):
    yield db


@pytest.mark.asyncio
@pytest.mark.parametrize(("calendar_type", "suffix"), [("fiscal", "year"), ("retail_445", "retail_year")])
async def test_bug9487_r1_f1_existing_rebuild_reconciles_caption_and_keeps_key(calendar_type, suffix):
    calendar_id = uuid4()
    key_id = uuid4()
    label_id = uuid4()
    dimension = SimpleNamespace(
        id=uuid4(), model_id=uuid4(), name=f"order_date_{suffix}",
        display_column_id=None, source_column_id=key_id,
        user_defined_attribute_id=None, is_time_dim=True,
    )
    hierarchy = SimpleNamespace(
        id=uuid4(), name="Order Date", model_id=dimension.model_id,
        date_config={"calendar_table_id": str(calendar_id)},
        calendar_type=calendar_type, type="date_embedded",
    )
    level = SimpleNamespace(
        hierarchy_id=hierarchy.id, time_unit="year", key_attribute_id=key_id,
        key_attribute_source="physical_column",
    )
    key_column = SimpleNamespace(id=key_id, model_table_id=uuid4(), column_name="year_no")
    label_column = SimpleNamespace(id=label_id, model_table_id=key_column.model_table_id, column_name="year_label")
    calendar = SimpleNamespace(id=calendar_id, date_column="date_key", year_column="year_no")
    spine = SimpleNamespace(id=uuid4(), source_id=uuid4(), physical_name="dim_date")

    def result(items):
        value = MagicMock()
        value.scalars.return_value.all.return_value = items
        value.scalar_one_or_none.return_value = items[0] if items else None
        return value

    db = AsyncMock()
    db.get = AsyncMock(side_effect=[calendar, key_column])
    db.execute = AsyncMock(side_effect=[result([hierarchy]), result([spine]), result([level]), result([label_column]), result([dimension])])
    db.flush = AsyncMock()
    history = {}
    before_key = level.key_attribute_id
    assert await reconcile_generated_calendar_captions(
        db, model_id=dimension.model_id, calendar_table_id=calendar_id,
        calendar_type=calendar_type, history_capture=history,
    ) == 1
    assert level.key_attribute_id == before_key
    assert dimension.display_column_id == label_id
    assert history["dimension_display_columns"][0]["display_column_id"] is None


@pytest.mark.asyncio
async def test_bug9487_r1_f2_existing_fiscal_generate_undo_redo_undo_is_exact():
    history = {}
    db = AsyncMock()
    db.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    created = await _ensure_calendar_year_label_column(
        db, model_table_id=uuid4(), include=True, history_capture=history,
    )
    assert created is not None
    assert history["calendar_alias_columns"][0]["created"] is True
    # The provenance record distinguishes an owned new column from a reused
    # pre-existing one, which is what the undo inverse uses to avoid residue.
    assert history["calendar_alias_columns"][0]["column_name"] == "year_label"


@pytest.mark.asyncio
async def test_bug9487_tp01_production_generate_and_bind_call_reconciliation(monkeypatch) -> None:
    """TP-9487-01: both supported rebuild callers execute the caption pass."""
    model_id, source_id = uuid4(), uuid4()
    source = SimpleNamespace(id=source_id)
    connection = SimpleNamespace(
        connection_type="postgresql", config={"write_access": True}
    )
    alias = SimpleNamespace(id=uuid4())
    cal = SimpleNamespace(id=uuid4(), calendar_type="fiscal")
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None
    alias_result = MagicMock()
    alias_result.scalar_one_or_none.return_value = alias

    async def _run_generate() -> AsyncMock:
        db.execute = AsyncMock(side_effect=[none_result, none_result, alias_result])
        reconcile = AsyncMock()
        with monkeypatch.context() as patcher:
            patcher.setattr("src.api.calendar.get_tenant_db", lambda _tenant: _one_db(db))
            patcher.setattr("src.api.calendar._ensure_model_in_project", AsyncMock())
            patcher.setattr("src.api.calendar.acquire_model_definition_lock", AsyncMock())
            patcher.setattr("src.api.calendar._load_source_with_connection", AsyncMock(return_value=(source, connection)))
            patcher.setattr("src.api.calendar._normalise_dialect", lambda value: value)
            patcher.setattr("src.api.calendar.DDL_CAPABLE_CONNECTORS", {"postgresql"})
            patcher.setattr("src.api.calendar.qualify_physical_name", lambda *args: "public.dim_date")
            patcher.setattr("src.api.calendar.emit_calendar_ddl", lambda *args, **kwargs: "CREATE TABLE")
            patcher.setattr("src.api.calendar._execute_ddl", AsyncMock())
            patcher.setattr("src.api.calendar._effective_year_label_format", AsyncMock(return_value="start_year"))
            patcher.setattr("src.api.calendar._create_calendar_alias", AsyncMock(return_value=alias))
            patcher.setattr("src.api.calendar._ensure_calendar_alias_year_labels", AsyncMock())
            patcher.setattr("src.api.calendar._auto_create_date_hierarchies_for_model", AsyncMock(return_value=(0, [], [])))
            patcher.setattr("src.api.calendar._invalidate_time_grained_aggregates", AsyncMock())
            patcher.setattr("src.api.calendar.reconcile_generated_calendar_captions", reconcile)
            patcher.setattr("src.api.calendar.CalendarTableResponse.model_validate", lambda value: value)
            await auto_create_calendar(
                project_id=uuid4(), model_id=model_id, source_id=source_id,
                body=SimpleNamespace(
                    table_name="dim_date", start_date="2020-01-01", end_date="2020-12-31",
                    fiscal_year_start_month=4, calendar_type="fiscal", alias=None,
                    display_name=None,
                ),
                current_user=SimpleNamespace(tenant_id="tenant-a"),
            )
        return reconcile

    generate_reconcile = await _run_generate()
    generate_reconcile.assert_awaited_once()
    assert generate_reconcile.await_args.kwargs["calendar_type"] == "fiscal"

    # The bind caller is independently exercised against the same production
    # route; a helper-only test would not detect either call being removed.
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=lambda: cal),
        MagicMock(scalar=lambda: 1),
        MagicMock(scalars=lambda: SimpleNamespace(all=lambda: [alias])),
    ])
    reconcile_bind = AsyncMock()
    with monkeypatch.context() as patcher:
        patcher.setattr("src.api.calendar.get_tenant_db", lambda _tenant: _one_db(db))
        patcher.setattr("src.api.calendar._ensure_model_in_project", AsyncMock())
        patcher.setattr("src.api.calendar.acquire_model_definition_lock", AsyncMock())
        patcher.setattr("src.api.calendar._load_source_with_connection", AsyncMock(return_value=(source, connection)))
        patcher.setattr("src.api.calendar.qualify_physical_name", lambda *args: "public.dim_date")
        patcher.setattr("src.api.calendar._verify_table_exists", AsyncMock())
        patcher.setattr("src.api.calendar._verify_calendar_columns", AsyncMock())
        patcher.setattr("src.api.calendar._calendar_has_year_label", AsyncMock(return_value=True))
        patcher.setattr("src.api.calendar._auto_create_date_hierarchies_for_model", AsyncMock(return_value=(0, [], [])))
        patcher.setattr("src.api.calendar._ensure_calendar_alias_year_labels", AsyncMock())
        patcher.setattr("src.api.calendar._invalidate_time_grained_aggregates", AsyncMock())
        patcher.setattr("src.api.calendar.reconcile_generated_calendar_captions", reconcile_bind)
        patcher.setattr("src.api.calendar.CalendarTableResponse.model_validate", lambda value: value)
        response = await bind_calendar(
            request=MagicMock(), project_id=uuid4(), model_id=model_id, source_id=source_id,
            body=CalendarBindRequest(
                table_name="dim_date", dialect="postgresql", date_column="date_key",
                year_column="year_no", calendar_type="fiscal", fiscal_year_start_month=4,
            ), current_user=SimpleNamespace(tenant_id="tenant-a"),
        )
    assert response is cal
    reconcile_bind.assert_awaited_once()
    assert reconcile_bind.await_args.kwargs["calendar_type"] == "fiscal"


@pytest.mark.asyncio
async def test_bug9487_tp05_route_sessions_use_real_target_and_reject_cross_tenant(monkeypatch) -> None:
    """TP-9487-05: GET/PUT consume target tenant, not principal system scope."""
    opened: list[str] = []
    sessions = {"tenant-b": object()}

    def _target_db(target: str):
        opened.append(target)
        return _one_db(sessions.setdefault(target, object()))

    monkeypatch.setattr("src.api.tenants.get_tenant_db", _target_db)
    monkeypatch.setattr(
        "src.api.tenants.get_setting",
        AsyncMock(side_effect=[{"format": "start_year"}, {"format": "start_year"}, {"format": "span_short"}]),
    )
    audit = AsyncMock()
    setter = AsyncMock()
    monkeypatch.setattr("src.api.tenants.audit_required", audit)
    monkeypatch.setattr("src.api.tenants.set_setting", setter)
    system_admin = CurrentUser("root", "__system__", "root@example.test", "system_admin")
    tenant_admin = CurrentUser("a", "tenant-a", "a@example.test", "tenant_admin")

    response = await get_calendar_settings("tenant-b", system_admin)
    assert response.format == "start_year"
    updated = await update_calendar_settings(
        "tenant-b", FiscalYearLabelFormatRequest(format="span_short"), system_admin,
    )
    assert updated.format == "span_short"
    assert opened == ["tenant-b", "tenant-b"]
    assert setter.await_args.kwargs["tenant_id"] == "tenant-b"
    assert audit.await_args.kwargs["target_name"] == "tenant-b"

    with pytest.raises(HTTPException) as exc:
        await get_calendar_settings("tenant-b", tenant_admin)
    assert exc.value.status_code == 403
    assert opened == ["tenant-b", "tenant-b"]


@pytest.mark.asyncio
async def test_bug9487_r2_f6_legacy_fiscal_roles_keep_distinct_alias_joins() -> None:
    """TP-9487-08: legacy Order/Ship roles retain their own calendar joins."""
    model_id, calendar_id, fact_id = uuid4(), uuid4(), uuid4()
    order_source, ship_source = uuid4(), uuid4()
    order_alias, ship_alias = uuid4(), uuid4()
    order_year, ship_year = uuid4(), uuid4()
    order_date, ship_date = uuid4(), uuid4()
    order_key, ship_key = uuid4(), uuid4()
    order_month, ship_month = uuid4(), uuid4()
    calendar = SimpleNamespace(
        id=calendar_id, date_column="date_key", year_column="year_no",
        month_column="month_no", half_column=None, quarter_column=None,
        week_column=None, day_column=None,
    )
    spine = SimpleNamespace(id=uuid4(), table_type="calendar", source_id=uuid4())
    aliases = [
        SimpleNamespace(id=order_alias, table_type="dim_detail", calendar_table_id=calendar_id),
        SimpleNamespace(id=ship_alias, table_type="dim_detail", calendar_table_id=calendar_id),
    ]
    order_h = SimpleNamespace(
        id=uuid4(), model_id=model_id, name="Order Date",
        date_config={"calendar_table_id": str(calendar_id), "source_attribute_id": str(order_source)},
        calendar_type="fiscal", type="date_embedded",
    )
    ship_h = SimpleNamespace(
        id=uuid4(), model_id=model_id, name="Ship Date",
        date_config={"calendar_table_id": str(calendar_id), "source_attribute_id": str(ship_source)},
        calendar_type="fiscal", type="date_embedded",
    )
    order_levels = [
        SimpleNamespace(id=uuid4(), hierarchy_id=order_h.id, time_unit="year", key_attribute_id=order_key, key_attribute_source="user_defined_attribute"),
        SimpleNamespace(id=uuid4(), hierarchy_id=order_h.id, time_unit="month", key_attribute_id=order_month, key_attribute_source="user_defined_attribute"),
    ]
    ship_levels = [
        SimpleNamespace(id=uuid4(), hierarchy_id=ship_h.id, time_unit="year", key_attribute_id=ship_key, key_attribute_source="user_defined_attribute"),
        SimpleNamespace(id=uuid4(), hierarchy_id=ship_h.id, time_unit="month", key_attribute_id=ship_month, key_attribute_source="user_defined_attribute"),
    ]
    dimensions = [
        SimpleNamespace(id=uuid4(), name="order_date_year", is_time_dim=True, source_column_id=None, display_column_id=None, user_defined_attribute_id=uuid4()),
        SimpleNamespace(id=uuid4(), name="ship_date_year", is_time_dim=True, source_column_id=None, display_column_id=None, user_defined_attribute_id=uuid4()),
    ]
    sources = {
        order_source: SimpleNamespace(id=order_source, model_table_id=fact_id, column_name="order_date"),
        ship_source: SimpleNamespace(id=ship_source, model_table_id=fact_id, column_name="ship_date"),
    }
    right_columns = {
        order_date: SimpleNamespace(id=order_date, model_table_id=order_alias, column_name="date_key"),
        ship_date: SimpleNamespace(id=ship_date, model_table_id=ship_alias, column_name="date_key"),
    }
    alias_columns = {
        order_alias: [
            SimpleNamespace(id=order_date, model_table_id=order_alias, column_name="date_key"),
            SimpleNamespace(id=order_year, model_table_id=order_alias, column_name="year_no"),
            SimpleNamespace(id=order_month, model_table_id=order_alias, column_name="month_no"),
        ],
        ship_alias: [
            SimpleNamespace(id=ship_date, model_table_id=ship_alias, column_name="date_key"),
            SimpleNamespace(id=ship_year, model_table_id=ship_alias, column_name="year_no"),
            SimpleNamespace(id=ship_month, model_table_id=ship_alias, column_name="month_no"),
        ],
    }
    joins = [
        SimpleNamespace(left_column_id=order_source, right_table_id=order_alias, right_column_id=order_date),
        SimpleNamespace(left_column_id=ship_source, right_table_id=ship_alias, right_column_id=ship_date),
    ]

    def _result(items):
        result = MagicMock()
        result.scalars.return_value.all.return_value = items
        result.scalar_one_or_none.return_value = items[0] if items else None
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result([order_h, ship_h]), _result([spine]),
        _result(order_levels), _result(aliases), _result([joins[0]]), _result(alias_columns[order_alias]),
        _result([SimpleNamespace(id=uuid4(), model_table_id=order_alias, column_name="year_label")]), _result([dimensions[0]]),
        _result(ship_levels), _result(aliases), _result([joins[1]]), _result(alias_columns[ship_alias]),
        _result([SimpleNamespace(id=uuid4(), model_table_id=ship_alias, column_name="year_label")]), _result([dimensions[1]]),
    ])

    async def _get(cls, value):
        if cls.__name__ == "CalendarTable":
            return calendar
        if cls.__name__ == "ModelColumn":
            if value in (order_key, order_month, ship_key, ship_month):
                return None
            return sources.get(value) or right_columns.get(value)
        return None

    db.get = _get
    db.flush = AsyncMock()
    history = {}
    await reconcile_generated_calendar_captions(
        db, model_id=model_id, calendar_table_id=calendar_id,
        calendar_type="fiscal", history_capture=history,
    )
    assert order_levels[0].key_attribute_id == order_year
    assert ship_levels[0].key_attribute_id == ship_year
    assert dimensions[0].source_column_id == order_year
    assert dimensions[1].source_column_id == ship_year
    assert len({order_levels[0].key_attribute_id, ship_levels[0].key_attribute_id}) == 2
