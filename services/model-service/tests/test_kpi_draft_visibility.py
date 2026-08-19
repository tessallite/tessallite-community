"""F-017-12 / Bug-8728: KPI draft visibility follows the caller's EFFECTIVE
project/model binding (``caller_has_role``), not the coarse token role.

This is the guard the F-017-12 fix terminates in — it does NOT use the
``kpi_effective_role`` shim other KPI unit tests apply. Instead it patches
``caller_has_role`` per test to a binding-specific decision and proves:

  * a MEMBER token with a modeler BINDING sees drafts (token role alone would
    have hidden them — the pre-fix bug), and
  * a coarse ADMIN token with a restrictive binding does NOT see drafts.

Mirrors ``test_named_sets_draft_visibility`` (the sibling that already migrated
to ``caller_has_role``).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
)

# NOTE: deliberately NOT using the kpi_effective_role shim here — this file
# exercises the real caller_has_role wiring, patched per test.
pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/kpis"


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)


def _draft_kpi(name: str = "Draft KPI") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        value_measure_id=None,
        goal_measure_id=None,
        status_expression=None,
        trend_expression=None,
        status_graphic="Traffic Light",
        trend_graphic="Standard Arrow",
        weight=1.0,
        parent_kpi_id=None,
        certification_status="draft",
        owner_user_id=None,
        created_at=NOW,
        updated_at=NOW,
        expression=None,
        kpi_type=None,
        calc_agg_mode="automatic",
        inner_agg=None,
        inner_grain=None,
        outer_agg=None,
        at_grain=None,
        non_additive_agg=None,
        carry_forward=False,
        target_type=None,
        target_value=None,
        target_measure_id=None,
        target_expression=None,
        target_period=None,
        direction="higher_is_better",
        presentation_type=None,
        presentation_meta=None,
        trend_period="month",
        trend_threshold=0.01,
        trend_sparkline_periods=12,
        format_token=None,
        format_custom=None,
        unit_label=None,
        null_display_value="N/A",
        indicator_type=None,
        time_dimension_id=None,
        snapshot_frequency=None,
        snapshot_retention=None,
        created_by=None,
        is_deployed=False,
        deployed_at=None,
        evaluation_order=None,
        replacement_id=None,
    )


def _make_user(role: str) -> CurrentUser:
    return CurrentUser(
        user_id="user@example.com",
        tenant_id=TEST_TENANT,
        email="user@example.com",
        role=role,
    )


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_current_user, None)


class TestListDraftVisibilityUsesBinding:
    @pytest.mark.asyncio
    async def test_member_token_with_modeler_binding_sees_drafts(self, client):
        # A MEMBER token — under the pre-fix coarse-role gate this caller was
        # NOT privileged and drafts were hidden. With the effective binding
        # (caller_has_role -> True) the modeller sees the draft.
        member = _make_user(role="member")
        app.dependency_overrides[get_current_user] = lambda: member
        db = make_mock_db()
        db.execute = AsyncMock(return_value=_ScalarResult([_draft_kpi()]))

        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.kpis.caller_has_role", new=AsyncMock(return_value=True)
            ) as has_role,
            patch(
                "src.api.kpis.resolve_effective_persona",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        assert [row["name"] for row in resp.json()] == ["Draft KPI"]
        # The gate consulted the EFFECTIVE binding with the modeler threshold.
        has_role.assert_awaited_once_with(
            db, member, TEST_PROJECT_ID, "modeler", TEST_MODEL_ID,
        )

    @pytest.mark.asyncio
    async def test_restrictive_binding_hides_drafts_despite_admin_token(self, client):
        # A coarse ADMIN token whose effective binding is restrictive
        # (caller_has_role -> False) must have drafts filtered out in SQL.
        admin = _make_user(role="admin")
        app.dependency_overrides[get_current_user] = lambda: admin
        db = make_mock_db()
        db.execute = AsyncMock(return_value=_ScalarResult([]))

        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.kpis.caller_has_role", new=AsyncMock(return_value=False)
            ),
            patch(
                "src.api.kpis.resolve_effective_persona",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200
        assert resp.json() == []
        statement = str(db.execute.await_args.args[0])
        assert "certification_status" in statement


class TestGetDraftVisibilityUsesBinding:
    @pytest.mark.asyncio
    async def test_member_with_modeler_binding_can_get_draft(self, client):
        member = _make_user(role="member")
        app.dependency_overrides[get_current_user] = lambda: member
        draft = _draft_kpi()
        db = make_mock_db()
        db.get = AsyncMock(return_value=draft)

        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.kpis.caller_has_role", new=AsyncMock(return_value=True)
            ) as has_role,
            patch(
                "src.api.kpis.resolve_effective_persona",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(f"{PREFIX}/{draft.id}")

        assert resp.status_code == 200
        assert resp.json()["name"] == "Draft KPI"
        has_role.assert_awaited_once_with(
            db, member, TEST_PROJECT_ID, "modeler", TEST_MODEL_ID,
        )

    @pytest.mark.asyncio
    async def test_non_privileged_binding_cannot_get_draft(self, client):
        viewer = _make_user(role="admin")  # coarse admin, restrictive binding
        app.dependency_overrides[get_current_user] = lambda: viewer
        draft = _draft_kpi()
        db = make_mock_db()
        db.get = AsyncMock(return_value=draft)

        with (
            patch("src.api.kpis.get_tenant_db", async_gen_from(db)),
            patch(
                "src.api.kpis.caller_has_role", new=AsyncMock(return_value=False)
            ),
            patch(
                "src.api.kpis.resolve_effective_persona",
                new=AsyncMock(return_value=None),
            ),
        ):
            resp = await client.get(f"{PREFIX}/{draft.id}")

        assert resp.status_code == 404
