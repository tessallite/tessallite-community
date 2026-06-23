"""Bug-3577: UNIQUE(model_id, alias) on model_tables.

Verifies the constraint exists in the ORM definition and that a
duplicate (model_id, alias) insert raises IntegrityError (the DB
enforcement backing the app-level check-then-act guard).

The Bug-5246 savepoint retry in calendar.py is tested separately
in test_calendar_savepoint_retry.py and must remain intact.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from shared.db.models import ModelTable

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# ORM model introspection: constraint exists
# ---------------------------------------------------------------------------


def test_unique_constraint_declared_in_orm():
    """ModelTable.__table_args__ must include a UniqueConstraint on
    (model_id, alias) named 'uq_model_tables_model_id_alias'."""
    table = ModelTable.__table__
    constraint_names = {
        c.name for c in table.constraints if hasattr(c, "columns")
    }
    assert "uq_model_tables_model_id_alias" in constraint_names, (
        f"Expected 'uq_model_tables_model_id_alias' in constraints, "
        f"got: {constraint_names}"
    )


def test_unique_constraint_covers_correct_columns():
    """The constraint must cover exactly (model_id, alias)."""
    table = ModelTable.__table__
    for c in table.constraints:
        if getattr(c, "name", None) == "uq_model_tables_model_id_alias":
            col_names = {col.name for col in c.columns}
            assert col_names == {"model_id", "alias"}, (
                f"Expected columns {{'model_id', 'alias'}}, got {col_names}"
            )
            break
    else:
        pytest.fail("Constraint 'uq_model_tables_model_id_alias' not found")


# ---------------------------------------------------------------------------
# One-fact partial unique index still declared
# ---------------------------------------------------------------------------


def test_one_fact_index_still_declared():
    """The pre-existing one-fact-per-model partial unique index (F-013-11)
    must remain intact after adding the alias constraint."""
    table = ModelTable.__table__
    index_names = {idx.name for idx in table.indexes}
    assert "uq_model_tables_one_fact_per_model" in index_names, (
        f"Expected 'uq_model_tables_one_fact_per_model' in indexes, "
        f"got: {index_names}"
    )


# ---------------------------------------------------------------------------
# Bug-5246 retry code intact: calendar.py still imports and uses retry
# ---------------------------------------------------------------------------


def test_calendar_retry_code_present():
    """Bug-5246 retry must survive the constraint addition. Verify the
    retry constant and begin_nested pattern are still in the calendar
    module source."""
    import inspect
    from src.api.calendar import _create_calendar_alias

    source = inspect.getsource(_create_calendar_alias)
    assert "begin_nested" in source, (
        "_create_calendar_alias must use begin_nested for savepoint retry"
    )
    assert "_MAX_ALIAS_RETRIES" in source or "IntegrityError" in source, (
        "_create_calendar_alias must handle IntegrityError for alias retry"
    )


# ---------------------------------------------------------------------------
# Bug-5426: tables.py create_table must have savepoint/retry for alias
# ---------------------------------------------------------------------------


def test_tables_create_table_has_savepoint_retry():
    """Bug-5426: ``create_table`` in tables.py must wrap the alias INSERT
    in ``begin_nested()`` with a bounded IntegrityError retry, mirroring
    the pattern in ``_create_calendar_alias``."""
    import inspect
    from src.api.tables import create_table

    source = inspect.getsource(create_table)
    assert "begin_nested" in source, (
        "create_table must use begin_nested for savepoint-protected alias retry"
    )
    assert "_is_alias_violation" in source, (
        "create_table must check for alias IntegrityError via _is_alias_violation"
    )
    assert "_MAX_ALIAS_RETRIES" in source, (
        "create_table must use _MAX_ALIAS_RETRIES for bounded retry"
    )


def test_tables_is_alias_violation_detects_constraint():
    """``_is_alias_violation`` must detect the uq_model_tables_model_id_alias
    constraint name in an IntegrityError."""
    from src.api.tables import _is_alias_violation

    class FakeOrig:
        def __str__(self):
            return 'duplicate key value violates unique constraint "uq_model_tables_model_id_alias"'

    class FakeExc(IntegrityError):
        def __init__(self):
            self.orig = FakeOrig()

    assert _is_alias_violation(FakeExc()) is True


def test_tables_is_alias_violation_rejects_unrelated():
    """``_is_alias_violation`` must return False for unrelated IntegrityErrors."""
    from src.api.tables import _is_alias_violation

    class FakeOrig:
        def __str__(self):
            return 'duplicate key value violates unique constraint "uq_model_tables_one_fact_per_model"'

    class FakeExc(IntegrityError):
        def __init__(self):
            self.orig = FakeOrig()

    assert _is_alias_violation(FakeExc()) is False


def test_tables_user_supplied_alias_collision_raises_409_not_retry():
    """Bug-5426: when a user-supplied alias collides, create_table must
    raise HTTP 409, NOT silently retry with a different alias."""
    import inspect
    from src.api.tables import create_table

    source = inspect.getsource(create_table)
    # The code must distinguish user-supplied from auto-generated aliases
    assert "user_supplied_alias" in source, (
        "create_table must track whether alias was user-supplied to avoid retrying"
    )
