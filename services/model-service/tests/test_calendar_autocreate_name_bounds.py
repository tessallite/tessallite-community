"""Guards for the calendar-auto-create name compounding overflow bug.

Repeated calendar auto-creates on one model used to re-consume the date-typed
components the generator had already emitted onto calendar-alias tables, so each
run wrapped the previous run's auto-generated name and the model_tables.alias /
display_name INSERT overflowed varchar(255) (StringDataRightTruncationError)
after ~5 calendars.

Root-cause fixes guarded here:
  1. `_get_unassigned_date_cols` excludes columns/UDAs on calendar-alias tables
     (calendar_table_id IS NOT NULL) so generated calendar-internal components
     are never re-consumed -> repeated auto-creates stay idempotent/bounded.
  2. Bug-6683: `_get_unassigned_date_cols` also excludes GENERATED UDAs
     (`is_generated` = True) regardless of which table they sit on. The
     `generate-date` endpoint places its date-typed day-component UDA on the
     FACT table (calendar_table_id IS NULL), so guard (1) does not catch it;
     consuming it wrapped its "Auto-generated for hierarchy '<name>' (day)"
     description into a new "<...> Calendar" hierarchy, compounding one level
     per save/deploy cycle until varchar(255) overflowed. Generated component
     UDAs are internal artifacts of an existing hierarchy and never seed their
     own auto-calendar.
  3. `_clamp_to_limit` backstop clamps every persisted auto-generated name to
     varchar(255), preserving uniqueness with a hash suffix on truncation.

Test escape: no prior test ran the name-derivation past one generation, asserted
the persisted names stay within the column limit, or exercised the fact-table
generated-UDA re-consumption vector that survived guard (1).
Guard tier: T1 unit/contract.
"""
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.hierarchies import (
    _NAME_COLUMN_LIMIT,
    _auto_create_date_hierarchies_for_model,
    _calendar_hier_label,
    _clamp_to_limit,
    _get_unassigned_date_cols,
    _UnassignedDateAttr,
)


# ---------------------------------------------------------------------------
# _clamp_to_limit: bounded, idempotent, uniqueness-preserving
# ---------------------------------------------------------------------------

def test_clamp_leaves_short_values_unchanged():
    assert _clamp_to_limit("Order Date Calendar") == "Order Date Calendar"


def test_clamp_leaves_exact_limit_unchanged():
    value = "x" * _NAME_COLUMN_LIMIT
    assert _clamp_to_limit(value) == value
    assert len(_clamp_to_limit(value)) == _NAME_COLUMN_LIMIT


def test_clamp_truncates_over_limit_to_bound():
    value = "y" * (_NAME_COLUMN_LIMIT * 5)
    out = _clamp_to_limit(value)
    assert len(out) <= _NAME_COLUMN_LIMIT


def test_clamp_is_idempotent():
    """Clamping already-clamped (bounded) output is a no-op — re-running the
    generator must not keep shrinking or mutating a stable name."""
    value = "z" * (_NAME_COLUMN_LIMIT * 3)
    once = _clamp_to_limit(value)
    twice = _clamp_to_limit(once)
    assert once == twice


def test_clamp_preserves_uniqueness_for_distinct_long_inputs():
    a = "a" * 300 + "_business_date"
    b = "a" * 300 + "_ship_date"
    assert _clamp_to_limit(a) != _clamp_to_limit(b)
    assert len(_clamp_to_limit(a)) <= _NAME_COLUMN_LIMIT
    assert len(_clamp_to_limit(b)) <= _NAME_COLUMN_LIMIT


def test_clamp_handles_none():
    assert _clamp_to_limit(None) is None


def test_generate_endpoint_name_patterns_stay_bounded():
    """The user-invoked generate_date_hierarchy / generate_segment_hierarchy
    endpoints build generated UDA/dimension names as '<base>_<component>',
    '<base>_seg_<n>' and dimension display_name '<level> (<hier name>)' from a
    base attribute / hierarchy name that may legally be up to 255 chars. Each
    is clamped before persisting to a varchar(255) column so a long but valid
    user name cannot raise StringDataRightTruncationError (a 500 the
    IntegrityError guard does not catch)."""
    base = "b" * 255
    hier = "h" * 255
    assert len(_clamp_to_limit(f"{base}_year")) <= _NAME_COLUMN_LIMIT
    assert len(_clamp_to_limit(f"{base}_seg_12")) <= _NAME_COLUMN_LIMIT
    assert len(_clamp_to_limit(f"Level 1 ({hier})")) <= _NAME_COLUMN_LIMIT


# ---------------------------------------------------------------------------
# Shared label helper: both generators (auto-create + batch-date) must clamp
# identically so they persist byte-identical, varchar(255)-safe names.
# ---------------------------------------------------------------------------

def test_calendar_hier_label_is_bounded():
    long_display = "Extremely Long Column Display Name " * 30
    assert len(_calendar_hier_label(long_display, "col")) <= _NAME_COLUMN_LIMIT


def test_calendar_hier_label_prefers_display_name():
    assert _calendar_hier_label("Order Date", "order_dt") == "Order Date Calendar"


def test_calendar_hier_label_falls_back_to_column_name():
    assert _calendar_hier_label(None, "order_dt") == "order_dt Calendar"


def test_calendar_hier_label_deterministic_across_consumers():
    """Auto-create and batch-date derive the name via the same helper, so a
    given (display_name, column_name) yields one stable, bounded label — the
    two paths never diverge and re-runs stay idempotent."""
    long_display = "y" * 400
    first = _calendar_hier_label(long_display, "c")
    second = _calendar_hier_label(long_display, "c")
    assert first == second
    assert len(first) <= _NAME_COLUMN_LIMIT


# ---------------------------------------------------------------------------
# Root cause: calendar-internal generated components are not re-consumed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unassigned_scan_excludes_calendar_alias_tables():
    """Both the physical-column and UDA scans must filter out attributes that
    live on a calendar-alias table (calendar_table_id set). Those are the
    generated day-component date attributes; re-consuming them is what
    compounded the names."""
    captured: list = []

    async def _capture(stmt):
        captured.append(str(stmt))
        result = MagicMock()
        result.fetchall.return_value = []
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_capture)

    rows = await _get_unassigned_date_cols(db, uuid4())

    assert rows == []
    assert len(captured) == 2  # physical scan + UDA scan
    for sql in captured:
        # Bug-6683: both scans must negate the shared calendar-instance
        # predicate (_calendar_instance_clause): marked aliases
        # (calendar_table_id set), calendar spine tables (table_type =
        # 'calendar' even with a NULL backlink), and UNMARKED aliases created
        # by batch-date against an unbound calendar — identified by sharing a
        # spine's SOURCE-SCOPED physical_name via correlated EXISTS, so a
        # same-named table in a different source stays eligible. The deep
        # behavioral proof lives in test_unassigned_date_scan_db_contract.py;
        # this guards the producer/consumer contract shape.
        low = sql.lower()
        assert "calendar_table_id is not null" in low
        assert "table_type = " in low
        assert "exists" in low
        assert "physical_name" in low
        assert "source_id" in low

    # Bug-6683: the UDA scan (second query) must ALSO exclude generated
    # hierarchy-component UDAs, which live on the fact table and slipped past the
    # calendar-alias filter above.
    uda_sql = captured[1]
    assert "is_generated IS false" in uda_sql


@pytest.mark.asyncio
async def test_unassigned_scan_excludes_generated_date_udas(monkeypatch):
    """Bug-6683 regression. A `generate-date` day-component UDA is date-typed,
    is_generated=True, and lives on the FACT table (calendar_table_id IS NULL),
    so the calendar-alias filter does not exclude it. The scan must drop it via
    the is_generated filter so calendar auto-create never re-consumes it and
    wraps its "Auto-generated for hierarchy ..." description into a new name.

    Rows are driven from the executed statements: the physical scan returns a
    real fact date column (surfaced) and the UDA scan returns nothing because a
    correct WHERE clause filtered the generated UDA in the database.
    """
    from src.api import hierarchies as H

    model_id = uuid4()
    fact_table = MagicMock()
    fact_table.id = uuid4()
    fact_table.alias = "fact_sales"
    phys_col = MagicMock()
    phys_col.id = uuid4()
    phys_col.column_name = "business_date"
    phys_col.display_name = "Business Date"
    phys_col.data_type = "date"

    captured: list[str] = []

    async def _exec(stmt):
        sql = str(stmt)
        captured.append(sql)
        result = MagicMock()
        # First query = physical-column scan -> one real fact date column.
        # Second query = UDA scan -> empty (generated UDA filtered by WHERE).
        if "user_defined_attributes" in sql:
            result.fetchall.return_value = []
        else:
            result.fetchall.return_value = [(phys_col, fact_table)]
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_exec)

    rows = await _get_unassigned_date_cols(db, model_id)

    # Only the physical fact date column is surfaced; no generated UDA leaks in.
    assert [r.column_name for r in rows] == ["business_date"]
    assert all(not r.is_uda for r in rows)
    # The UDA scan must carry the is_generated exclusion so the DB drops
    # generated component UDAs before they reach Python.
    uda_sql = next(s for s in captured if "user_defined_attributes" in s)
    assert "is_generated IS false" in uda_sql


def test_no_name_doubling_from_single_wrap():
    """Guard the concrete overflow signature: a single application of the
    calendar label helper to a base date column must not contain the doubled
    "auto generated for hierarchy auto generated for hierarchy" marker that the
    compounding bug produced. (The idempotency that prevents a SECOND wrap is
    enforced by the is_generated / calendar-alias scan exclusions above.)"""
    label = _calendar_hier_label("Business Date", "business_date")
    slug = label.lower().replace("'", "").replace(" ", "_")
    assert "auto_generated_for_hierarchy" not in slug
    assert label == "Business Date Calendar"


# ---------------------------------------------------------------------------
# Backstop: even a pathologically long base label yields bounded persisted names
# ---------------------------------------------------------------------------

def _make_db_with_date_key():
    db = AsyncMock()
    cal_mt = MagicMock()
    cal_mt.calendar_table_id = uuid4()
    cal_mt.id = uuid4()
    cal_mt.source_id = uuid4()
    cal_mt.physical_name = "dim_date"
    return db, cal_mt


@pytest.mark.asyncio
async def test_long_base_label_produces_bounded_alias_and_names():
    """Simulates the 5th-generation compounded label the old code fed back in.
    The created ModelTable.alias / display_name and the hierarchy name passed to
    the generator must all stay within varchar(255)."""
    from shared.db.models import ModelTable

    db, cal_mt = _make_db_with_date_key()

    # A label longer than any single generation would produce naturally,
    # standing in for the compounded name that used to overflow.
    long_label = "Auto-generated for hierarchy 'business date quarter hierarchy' (day) Calendar " * 20
    attr = _UnassignedDateAttr(
        id=uuid4(),
        column_name=long_label,
        display_name=long_label,
        data_type="date",
        table_id=uuid4(),
        table_alias="fact_sales",
    )

    date_key_col_result = MagicMock()
    date_key_col_result.scalar_one_or_none.return_value = MagicMock(
        id=uuid4(), column_name="date_key", display_name="Date Key",
        data_type="date", is_nullable=False, is_hidden=False,
    )
    hier_check = MagicMock()
    hier_check.scalar_one_or_none.return_value = None
    alias_query = MagicMock()
    alias_query.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[date_key_col_result, hier_check, alias_query])
    db.get = AsyncMock(side_effect=[cal_mt, MagicMock(date_column="date_key")])

    added_objects: list = []
    db.add = MagicMock(side_effect=lambda o: added_objects.append(o))
    db.flush = AsyncMock()

    captured_hier_names: list = []

    async def _capture_hier(*args, **kwargs):
        captured_hier_names.append(kwargs.get("name"))

    async def _fake_unassigned(*args):
        return [attr]

    with (
        patch("src.api.hierarchies._get_unassigned_date_cols", side_effect=_fake_unassigned),
        patch("src.api.hierarchies._create_date_hierarchy_for_alias", side_effect=_capture_hier),
    ):
        created, skipped, aliases = await _auto_create_date_hierarchies_for_model(
            db, model_id=uuid4(), calendar_model_table_id=cal_mt.id, grain="y_m_d"
        )

    assert created == 1
    alias_tables = [o for o in added_objects if isinstance(o, ModelTable)]
    assert len(alias_tables) == 1
    alias_mt = alias_tables[0]
    assert len(alias_mt.alias) <= _NAME_COLUMN_LIMIT
    assert len(alias_mt.display_name) <= _NAME_COLUMN_LIMIT
    assert captured_hier_names and len(captured_hier_names[0]) <= _NAME_COLUMN_LIMIT
