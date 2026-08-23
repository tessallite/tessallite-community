"""F-013-01 / F-016-02 / F-101-03 — calendar identity is deploy-pinned.

Period-aware measures pin ``resolved_calendar_id``, but the calendar ROW (type,
``fiscal_year_start_month``, physical column map, physical table name) and the
owning hierarchy's calendar rules used to be read LIVE at serve time. A draft
fiscal-start / column-remap edit therefore moved deployed YTD/QTD numbers before
the next Deploy — the exact "serving != authored pin" defect B15 exists to stop.

These tests assert the KNOWN VALUES that must survive a divergent live edit:

* A DEPLOYED model resolves the calendar and hierarchy calendar rules from the
  request-pinned snapshot, so a live edit to a different fiscal start is ignored.
* An UNDEPLOYED model still resolves from live rows (authoring / editor preview).
* A DEPLOYED model whose snapshot lacks the pinned calendar id fails CLOSED
  rather than silently reading the mutable live row.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import CalendarTable, Model
from src.ir.logical_query import SemanticBindingError
from src.rewrite.calendar_support import (
    _resolve_calendar_binding,
    _resolve_hierarchy_calendar_rules,
)
from src.semantic import snapshot_resolver as sr

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_pins():
    sr.reset_request_pins()
    sr.invalidate()
    yield
    sr.reset_request_pins()
    sr.invalidate()


CAL_ID = uuid.uuid4()
HIER_ID = uuid.uuid4()
SRC_COL_ID = uuid.uuid4()


def _deployed_model():
    return types.SimpleNamespace(
        id=uuid.uuid4(), deployed_version_id=uuid.uuid4(), deploy_epoch=7
    )


def _snapshot(*, fiscal_start=4, cal_type="fiscal", include_calendar=True):
    """Snapshot pinning a fiscal calendar (start month 4) and a fiscal hierarchy."""
    snap = {
        # A minimal semantic shape so _build_shape / _snapshot_has_shape accept it.
        "measures": [{"id": str(uuid.uuid4()), "name": "revenue"}],
        "columns": [{"id": str(uuid.uuid4()), "column_name": "d", "model_table_id": str(uuid.uuid4())}],
        "tables": [{"id": str(uuid.uuid4()), "physical_name": "fact"}],
        "hierarchies": [
            {
                "id": str(HIER_ID),
                "calendar_type": cal_type,
                "fiscal_year_start_month": fiscal_start,
                "dimension_kind": "time",
                "levels": [
                    {
                        "id": str(uuid.uuid4()),
                        "ordinal": 0,
                        "key_attribute_source": "physical_column",
                        "key_attribute_id": str(SRC_COL_ID),
                    }
                ],
            }
        ],
        "calendar_tables": [],
    }
    if include_calendar:
        snap["calendar_tables"] = [
            {
                "id": str(CAL_ID),
                "table_name": "dim_cal_snapshot",
                "dialect": "postgres",
                "calendar_type": cal_type,
                "date_column": "cal_date",
                "year_column": "cal_year",
                "month_column": "cal_month",
                "fiscal_year_start_month": fiscal_start,
            }
        ]
    return snap


def _live_calendar(fiscal_start):
    """A DRAFT-edited live calendar row with a DIFFERENT fiscal start."""
    return CalendarTable(
        id=CAL_ID,
        data_source_id=uuid.uuid4(),
        table_name="dim_cal_live",
        dialect="postgres",
        calendar_type="fiscal",
        date_column="cal_date",
        fiscal_year_start_month=fiscal_start,
    )


def _deployed_shape(snapshot):
    """Build the real ``DeployedShape`` the pinned snapshot would resolve to.

    The query-router conftest autouse-patches ``resolve_deployed_shape`` to an
    empty shape, so each deployed test re-patches it (below) with this real shape
    built from ``_build_shape`` — the same builder production uses.
    """
    return sr._build_shape(uuid.uuid4(), snapshot)


def _deployed_db(model, live_calendar):
    """AsyncMock db: Model lookup -> deployed model; CalendarTable -> live row.

    The live CalendarTable row is the draft-edited value that MUST NOT be served
    on the deployed path (proves the snapshot pin wins).
    """
    db = AsyncMock()

    async def _get(cls, ident):
        if cls is Model:
            return model
        if cls is CalendarTable:
            return live_calendar
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _undeployed_db(model, live_calendar):
    """AsyncMock db for an undeployed model: live CalendarTable is authority."""
    db = AsyncMock()

    async def _get(cls, ident):
        if cls is Model:
            return model
        if cls is CalendarTable:
            return live_calendar
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _period_measure(model_id):
    return types.SimpleNamespace(
        model_id=model_id,
        resolved_calendar_id=CAL_ID,
        calendar_model_table_id=None,
    )


def _pin_patch(shape):
    """Patch the (conftest-mocked) resolver so a deployed model yields *shape*."""
    return patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=AsyncMock(return_value=shape),
    )


async def test_deployed_calendar_binding_ignores_draft_fiscal_start():
    # Snapshot pins fiscal start 4; a concurrent DRAFT edit set live to 7.
    model = _deployed_model()
    db = _deployed_db(model, _live_calendar(fiscal_start=7))

    with _pin_patch(_deployed_shape(_snapshot(fiscal_start=4))):
        cal = await _resolve_calendar_binding(db, [_period_measure(model.id)])

    # The pinned snapshot value wins; the live draft edit (7) is NOT served.
    assert cal.fiscal_year_start_month == 4
    assert cal.table_name == "dim_cal_snapshot"


async def test_deployed_hierarchy_rules_ignore_draft_fiscal_start():
    model = _deployed_model()
    db = _deployed_db(model, _live_calendar(fiscal_start=7))
    # If the code fell through to live, db.execute would answer with 9.
    db.execute.return_value = types.SimpleNamespace(first=lambda: ("fiscal", 9))

    time_dim = types.SimpleNamespace(
        hierarchy_id=HIER_ID, source_column_id=None, is_time_dim=True
    )
    with _pin_patch(_deployed_shape(_snapshot(fiscal_start=4))):
        cal_type, fy = await _resolve_hierarchy_calendar_rules(db, time_dim, model.id)

    assert cal_type == "fiscal"
    assert fy == 4  # snapshot, not the live 9
    db.execute.assert_not_called()


async def test_deployed_hierarchy_rules_pin_via_physical_column():
    model = _deployed_model()
    db = _deployed_db(model, _live_calendar(fiscal_start=7))
    db.execute.return_value = types.SimpleNamespace(first=lambda: ("fiscal", 9))

    # No hierarchy_id: resolves by the level's physical column id instead.
    time_dim = types.SimpleNamespace(
        hierarchy_id=None, source_column_id=SRC_COL_ID, is_time_dim=True
    )
    with _pin_patch(_deployed_shape(_snapshot(fiscal_start=4))):
        cal_type, fy = await _resolve_hierarchy_calendar_rules(db, time_dim, model.id)

    assert (cal_type, fy) == ("fiscal", 4)
    db.execute.assert_not_called()


async def test_undeployed_calendar_binding_uses_live_row():
    # Authoring / editor preview: no deploy pointer -> live rows are authority.
    model = types.SimpleNamespace(
        id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=0
    )
    db = _undeployed_db(model, _live_calendar(fiscal_start=7))

    cal = await _resolve_calendar_binding(db, [_period_measure(model.id)])

    assert cal.fiscal_year_start_month == 7
    assert cal.table_name == "dim_cal_live"


async def test_deployed_calendar_missing_from_snapshot_fails_closed():
    # Deployed model whose snapshot has NO calendar_tables row for the pinned id.
    model = _deployed_model()
    db = _deployed_db(model, _live_calendar(fiscal_start=7))

    with _pin_patch(_deployed_shape(_snapshot(include_calendar=False))):
        with pytest.raises(SemanticBindingError):
            await _resolve_calendar_binding(db, [_period_measure(model.id)])


# ---------------------------------------------------------------------------
# Bug-9200 — the hierarchy-RULES half must fail closed too
# ---------------------------------------------------------------------------
#
# ``_resolve_calendar_binding`` (the calendar ROW) has raised on an unusable
# deployed snapshot since F-013-01. Its sibling ``_resolve_hierarchy_calendar_rules``
# (the calendar RULES — calendar_type and fiscal_year_start_month) did NOT: it
# fell through to the live HierarchyDefinition query on the argument that "the
# binder 503s first". Two problems with that argument:
#
#   * it is fail-OPEN by construction — the guarantee lives in a DIFFERENT
#     function, so it holds only for callers that happen to route through the
#     binder, and nothing makes a new caller do that; and
#   * these two values decide period MATH, so a leaked draft edit does not
#     produce an error, it produces a DIFFERENT NUMBER for YTD/QTD — the exact
#     silent-wrong-number class deploy pinning exists to prevent.
#
# Both halves of the calendar family now refuse identically.


def _invalid_snapshot_patch():
    """Deploy pointer present, snapshot unusable -> DEPLOYED_SNAPSHOT_INVALID."""
    return patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        new=AsyncMock(return_value=None),
    )


async def test_bug_9200_hierarchy_rules_refuse_an_unusable_deployed_snapshot():
    """Pre-fix this returned ("fiscal", 9) — the LIVE draft rules — instead of
    raising, so a deployed query silently computed its fiscal year from an
    unpublished edit."""
    model = _deployed_model()
    db = _deployed_db(model, _live_calendar(fiscal_start=7))
    db.execute.return_value = types.SimpleNamespace(first=lambda: ("fiscal", 9))
    time_dim = types.SimpleNamespace(
        hierarchy_id=HIER_ID, source_column_id=None, is_time_dim=True
    )

    with _invalid_snapshot_patch():
        with pytest.raises(SemanticBindingError):
            await _resolve_hierarchy_calendar_rules(db, time_dim, model.id)

    db.execute.assert_not_called()


async def test_bug_9200_hierarchy_rules_refuse_via_the_physical_column_path_too():
    """The same refusal on the second resolution path.

    Path 2 (a plain date dimension matched by its physical column) is a separate
    branch, and a fix applied to path 1 alone would leave it leaking.
    """
    model = _deployed_model()
    db = _deployed_db(model, _live_calendar(fiscal_start=7))
    db.execute.return_value = types.SimpleNamespace(first=lambda: ("fiscal", 9))
    time_dim = types.SimpleNamespace(
        hierarchy_id=None, source_column_id=SRC_COL_ID, is_time_dim=True
    )

    with _invalid_snapshot_patch():
        with pytest.raises(SemanticBindingError):
            await _resolve_hierarchy_calendar_rules(db, time_dim, model.id)

    db.execute.assert_not_called()


async def test_bug_9200_both_calendar_halves_refuse_the_same_unusable_snapshot():
    """The two halves must agree.

    They diverged for months — the ROW raised, the RULES fell through to live —
    which is how a family-wide invariant becomes a half-invariant. Asserting
    them together is what stops one being hardened and the other forgotten.
    """
    model = _deployed_model()
    db = _deployed_db(model, _live_calendar(fiscal_start=7))
    db.execute.return_value = types.SimpleNamespace(first=lambda: ("fiscal", 9))
    time_dim = types.SimpleNamespace(
        hierarchy_id=HIER_ID, source_column_id=None, is_time_dim=True
    )

    with _invalid_snapshot_patch():
        with pytest.raises(SemanticBindingError):
            await _resolve_calendar_binding(db, [_period_measure(model.id)])
        with pytest.raises(SemanticBindingError):
            await _resolve_hierarchy_calendar_rules(db, time_dim, model.id)


async def test_undeployed_hierarchy_rules_still_read_live():
    """The refusal must not break AUTHORING: with no deploy pointer the live
    HierarchyDefinition rows ARE the authority, and the model builder's preview
    depends on it."""
    model = types.SimpleNamespace(
        id=uuid.uuid4(), deployed_version_id=None, deploy_epoch=0
    )
    db = _undeployed_db(model, _live_calendar(fiscal_start=7))
    db.execute.return_value = types.SimpleNamespace(first=lambda: ("fiscal", 9))
    time_dim = types.SimpleNamespace(
        hierarchy_id=HIER_ID, source_column_id=None, is_time_dim=True
    )

    cal_type, fy = await _resolve_hierarchy_calendar_rules(db, time_dim, model.id)

    assert (cal_type, fy) == ("fiscal", 9)
