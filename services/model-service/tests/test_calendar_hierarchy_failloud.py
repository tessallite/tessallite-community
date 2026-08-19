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

from src.api.calendar import bind_calendar
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
