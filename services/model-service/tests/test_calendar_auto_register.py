"""Tests for _auto_create_date_hierarchies_for_model."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch, call

from src.api.hierarchies import _UnassignedDateAttr


def _make_attr(col_id=None, col_name="date_key", display_name="Date Key",
               data_type="date", table_id=None, table_alias="fact_sales",
               is_uda=False, physical_column_id=None):
    return _UnassignedDateAttr(
        id=col_id or uuid4(),
        column_name=col_name,
        display_name=display_name,
        data_type=data_type,
        table_id=table_id or uuid4(),
        table_alias=table_alias,
        is_uda=is_uda,
        physical_column_id=physical_column_id,
    )


def _make_db_with_date_key():
    db = AsyncMock()
    cal_mt = MagicMock()
    cal_mt.calendar_table_id = uuid4()
    cal_mt.id = uuid4()
    cal_mt.source_id = uuid4()
    cal_mt.physical_name = "dim_date"
    cal_info = MagicMock()
    cal_info.date_column = "date_key"
    db.get = AsyncMock(side_effect=[cal_mt, cal_info])

    col_result = MagicMock()
    col_result.scalar_one_or_none.return_value = MagicMock(
        column_name="date_key",
        display_name="Date Key",
        data_type="date",
        is_nullable=False,
        is_hidden=False,
    )
    db.execute = AsyncMock(return_value=col_result)
    return db, cal_mt


@pytest.mark.asyncio
async def test_no_unassigned_cols_returns_zero():
    """Returns (0, [], []) when no unassigned date columns exist."""
    from src.api.hierarchies import _auto_create_date_hierarchies_for_model

    db, _ = _make_db_with_date_key()

    async def _fake_unassigned(*args):
        return []

    with (
        patch("src.api.hierarchies._get_unassigned_date_cols", side_effect=_fake_unassigned),
    ):
        created, skipped, aliases = await _auto_create_date_hierarchies_for_model(
            db, model_id=uuid4(), calendar_model_table_id=uuid4(), grain="y_m_d"
        )

    assert created == 0
    assert skipped == []


@pytest.mark.asyncio
async def test_missing_calendar_date_key_returns_three_tuple():
    """Bug-6242 regression: when the calendar date-key column is not present on
    the calendar alias, the function must return a well-formed 3-tuple that the
    callers can unpack. It previously returned a 2-tuple, raising 'not enough
    values to unpack' — which the calendar.py swallow hid, leaving a committed
    calendar with no date hierarchy and a success response."""
    from src.api.hierarchies import _auto_create_date_hierarchies_for_model

    db = AsyncMock()
    cal_mt = MagicMock()
    cal_mt.calendar_table_id = uuid4()
    cal_mt.id = uuid4()
    cal_info = MagicMock()
    cal_info.date_column = "date_key"
    db.get = AsyncMock(side_effect=[cal_mt, cal_info])
    col_result = MagicMock()
    col_result.scalar_one_or_none.return_value = None  # date-key column absent
    db.execute = AsyncMock(return_value=col_result)

    # Must NOT raise a ValueError on unpack.
    created, skipped, aliases = await _auto_create_date_hierarchies_for_model(
        db, model_id=uuid4(), calendar_model_table_id=cal_mt.id, grain="y_m_d"
    )

    assert created == 0
    assert aliases == []
    assert len(skipped) == 1
    assert "not found in calendar alias" in skipped[0]


@pytest.mark.asyncio
async def test_col_belonging_to_calendar_is_skipped():
    """Column that lives on the calendar table itself is skipped."""
    from src.api.hierarchies import _auto_create_date_hierarchies_for_model

    db, cal_mt = _make_db_with_date_key()

    attr = _make_attr(col_name="date_key", table_id=cal_mt.id, table_alias="dim_date")

    async def _fake_unassigned(*args):
        return [attr]

    with (
        patch("src.api.hierarchies._get_unassigned_date_cols", side_effect=_fake_unassigned),
    ):
        created, skipped, aliases = await _auto_create_date_hierarchies_for_model(
            db, model_id=uuid4(), calendar_model_table_id=cal_mt.id, grain="y_m_d"
        )

    assert created == 0
    assert len(skipped) == 1
    assert "calendar table" in skipped[0]


@pytest.mark.asyncio
async def test_existing_hierarchy_is_skipped():
    """Column whose hierarchy already exists is added to skipped list."""
    from src.api.hierarchies import _auto_create_date_hierarchies_for_model

    db, cal_mt = _make_db_with_date_key()

    fact_table_id = uuid4()
    attr = _make_attr(col_name="order_date", display_name="Order Date",
                      table_id=fact_table_id, table_alias="fact_sales")

    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = uuid4()
    db.get = AsyncMock(side_effect=[cal_mt, MagicMock(date_column="date_key")])
    date_key_col_result = MagicMock()
    date_key_col_result.scalar_one_or_none.return_value = MagicMock(
        column_name="date_key", display_name="Date Key", data_type="date",
        is_nullable=False, is_hidden=False,
    )
    db.execute = AsyncMock(side_effect=[date_key_col_result, existing_result])

    async def _fake_unassigned(*args):
        return [attr]

    with (
        patch("src.api.hierarchies._get_unassigned_date_cols", side_effect=_fake_unassigned),
    ):
        created, skipped, aliases = await _auto_create_date_hierarchies_for_model(
            db, model_id=uuid4(), calendar_model_table_id=cal_mt.id, grain="y_m_d"
        )

    assert created == 0
    assert len(skipped) == 1
    assert "already exists" in skipped[0]


@pytest.mark.asyncio
async def test_invalid_calendar_table_raises():
    """Raises ValueError when calendar_model_table_id does not reference a calendar alias."""
    from src.api.hierarchies import _auto_create_date_hierarchies_for_model

    db = AsyncMock()
    mt = MagicMock()
    mt.calendar_table_id = None
    db.get = AsyncMock(return_value=mt)

    with (
        pytest.raises(ValueError, match="calendar ModelTable"),
    ):
        await _auto_create_date_hierarchies_for_model(
            db, model_id=uuid4(), calendar_model_table_id=uuid4(), grain="y_m_d"
        )


@pytest.mark.asyncio
async def test_auto_setup_creates_join_for_fact_date_to_alias():
    """Auto-setup path creates a Join row from fact date column to alias date key."""
    from shared.db.models import Join
    from src.api.hierarchies import _auto_create_date_hierarchies_for_model

    db, cal_mt = _make_db_with_date_key()

    fact_table_id = uuid4()
    attr = _make_attr(col_name="order_date", display_name="Order Date",
                      table_id=fact_table_id, table_alias="fact_sales")

    hier_check = MagicMock()
    hier_check.scalar_one_or_none.return_value = None

    date_key_col_result = MagicMock()
    date_key_col = MagicMock(
        id=uuid4(), column_name="date_key", display_name="Date Key",
        data_type="date", is_nullable=False, is_hidden=False,
    )
    date_key_col_result.scalar_one_or_none.return_value = date_key_col
    alias_query = MagicMock()
    alias_query.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[date_key_col_result, hier_check, alias_query])
    db.get = AsyncMock(side_effect=[cal_mt, MagicMock(date_column="date_key")])

    added_objects: list = []
    db.add = MagicMock(side_effect=lambda o: added_objects.append(o))
    db.flush = AsyncMock()

    async def _fake_unassigned(*args):
        return [attr]

    with (
        patch("src.api.hierarchies._get_unassigned_date_cols", side_effect=_fake_unassigned),
        patch("src.api.hierarchies._create_date_hierarchy_for_alias", new=AsyncMock()),
    ):
        created, skipped, aliases = await _auto_create_date_hierarchies_for_model(
            db, model_id=uuid4(), calendar_model_table_id=cal_mt.id, grain="y_m_d"
        )

    assert created == 1
    join_objs = [o for o in added_objects if isinstance(o, Join)]
    assert len(join_objs) == 1
    assert join_objs[0].left_column_id == attr.id
    # Join orientation and cardinality are SEPARATE fields (join-orientation
    # contract, invariant 3). This edge runs owning-table -> calendar alias:
    # many rows to one calendar day, preserved on the owning (many, anchor-ward)
    # side, which is a LEFT join. Persisting the cardinality label into
    # ``join_type`` — as this path used to — leaves the field that decides
    # which rows survive undeclared, and the pocket row-population proof then
    # refuses the whole model.
    assert join_objs[0].join_type == "left"
    assert join_objs[0].cardinality == "many_to_one"


@pytest.mark.asyncio
async def test_default_grain_creates_hierarchy_without_keyerror():
    """Calling without explicit grain uses y_m_d default (not 'standard')."""
    from shared.db.models import Join
    from src.api.hierarchies import _auto_create_date_hierarchies_for_model

    db, cal_mt = _make_db_with_date_key()

    fact_table_id = uuid4()
    attr = _make_attr(col_name="ship_date", display_name="Ship Date",
                      table_id=fact_table_id, table_alias="fact_sales")

    hier_check = MagicMock()
    hier_check.scalar_one_or_none.return_value = None

    date_key_col_result = MagicMock()
    date_key_col = MagicMock(
        id=uuid4(), column_name="date_key", display_name="Date Key",
        data_type="date", is_nullable=False, is_hidden=False,
    )
    date_key_col_result.scalar_one_or_none.return_value = date_key_col
    alias_query = MagicMock()
    alias_query.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[date_key_col_result, hier_check, alias_query])
    db.get = AsyncMock(side_effect=[cal_mt, MagicMock(date_column="date_key")])

    added_objects: list = []
    db.add = MagicMock(side_effect=lambda o: added_objects.append(o))
    db.flush = AsyncMock()

    async def _fake_unassigned(*args):
        return [attr]

    with (
        patch("src.api.hierarchies._get_unassigned_date_cols", side_effect=_fake_unassigned),
        patch("src.api.hierarchies._create_date_hierarchy_for_alias", new=AsyncMock()),
    ):
        created, skipped, aliases = await _auto_create_date_hierarchies_for_model(
            db, model_id=uuid4(), calendar_model_table_id=cal_mt.id
        )

    assert created == 1
    join_objs = [o for o in added_objects if isinstance(o, Join)]
    assert len(join_objs) == 1


@pytest.mark.asyncio
async def test_unknown_grain_raises_valueerror():
    """Passing an unsupported grain value raises ValueError."""
    from src.api.hierarchies import _auto_create_date_hierarchies_for_model

    db, cal_mt = _make_db_with_date_key()

    with (
        pytest.raises(ValueError, match="Unknown date hierarchy grain"),
    ):
        await _auto_create_date_hierarchies_for_model(
            db, model_id=uuid4(), calendar_model_table_id=uuid4(), grain="invalid_grain"
        )
