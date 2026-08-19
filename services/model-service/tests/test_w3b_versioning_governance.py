"""Lane W3-B — model-service versioning + governance happy-path guards.

Behavior tests for the in-scope wave-3 fixes:

  Bug-7294  ProjectExportRequest rejects unknown section names (was silently
            intersected away).
  Bug-7195  HierarchyDefinition carries a DB-level CHECK on ``type``.
  Bug-7198  hierarchy-health flags a reachable calendar table that is missing
            the physical period columns its calendar type needs.
  Bug-7005  manual pocket create is refused once the effective storage budget
            is exhausted.
  Bug-7279  a scratchpad expression that does not bind against the model is
            rejected at create time (never persisted to render an all-NULL
            column).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from shared.db.models import Model, ScratchpadMeasure

from tests.conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    make_mock_db,
)

pytestmark = pytest.mark.unit

POCKET_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/pockets"
)
SCRATCHPAD_PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    "/scratchpad-measures"
)
AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def _router_ok(**overrides) -> dict:
    base = {
        "ok": True,
        "errors": [],
        "select_star": True,
        "from_tables": ["modely"],
        "has_complex_sql": False,
        "has_unresolvable_where": False,
        "grain": [],
        "query_fingerprint": "abc123",
        "filters": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Bug-7294 — ProjectExportRequest section-name validation
# ---------------------------------------------------------------------------

class TestExportSectionValidation:
    def test_valid_sections_accepted(self):
        from src.api.project_import_export import ProjectExportRequest

        req = ProjectExportRequest(sections=["connections", "llm_configs"])
        assert set(req.sections) == {"connections", "llm_configs"}

    def test_default_sections_accepted(self):
        from src.api.project_import_export import (
            DEFAULT_SECTIONS,
            ProjectExportRequest,
        )

        req = ProjectExportRequest()
        assert set(req.sections) == DEFAULT_SECTIONS

    def test_unknown_section_rejected(self):
        from pydantic import ValidationError

        from src.api.project_import_export import ProjectExportRequest

        with pytest.raises(ValidationError) as exc:
            ProjectExportRequest(sections=["connections", "connectons"])
        # The invalid value must be named in the error so the caller can fix it.
        assert "connectons" in str(exc.value)

    def test_all_unknown_sections_rejected(self):
        from pydantic import ValidationError

        from src.api.project_import_export import ProjectExportRequest

        with pytest.raises(ValidationError):
            ProjectExportRequest(sections=["secrets", "everything"])


# ---------------------------------------------------------------------------
# Bug-7195 — DB-level CHECK on hierarchy_definitions.type
# ---------------------------------------------------------------------------

class TestHierarchyTypeCheckConstraint:
    def test_check_constraint_present_on_orm(self):
        from sqlalchemy import CheckConstraint

        from shared.db.models import HierarchyDefinition

        checks = [
            c
            for c in HierarchyDefinition.__table__.constraints
            if isinstance(c, CheckConstraint)
        ]
        assert any(
            c.name == "ck_hierarchy_definitions_type" for c in checks
        ), "hierarchy_definitions.type CHECK constraint missing (Bug-7195)"

    def test_check_constraint_lists_allowed_types(self):
        from sqlalchemy import CheckConstraint

        from shared.db.models import HierarchyDefinition

        check = next(
            c
            for c in HierarchyDefinition.__table__.constraints
            if isinstance(c, CheckConstraint)
            and c.name == "ck_hierarchy_definitions_type"
        )
        sqltext = str(check.sqltext)
        for allowed in ("explicit", "date_embedded", "segment"):
            assert allowed in sqltext

    def test_orm_check_matches_api_allowed_types(self):
        # Producer/consumer alignment: the DB CHECK must stay in lock-step with
        # the API-layer allowed-type set.
        from sqlalchemy import CheckConstraint

        from shared.db.models import HierarchyDefinition
        from src.api.hierarchies import ALLOWED_HIERARCHY_TYPES

        check = next(
            c
            for c in HierarchyDefinition.__table__.constraints
            if isinstance(c, CheckConstraint)
            and c.name == "ck_hierarchy_definitions_type"
        )
        sqltext = str(check.sqltext)
        for allowed in ALLOWED_HIERARCHY_TYPES:
            assert allowed in sqltext


def test_migration_0167_chains_after_0166():
    # Parse the revision fields textually so the test does not require alembic
    # to be importable in the model-service venv (the migration imports it).
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    src = (
        root / "shared/db/migrations/versions/0167_hierarchy_type_check.py"
    ).read_text(encoding="utf-8")
    assert re.search(r'^revision\s*=\s*"0167"', src, re.MULTILINE)
    assert re.search(r'^down_revision\s*=\s*"0166"', src, re.MULTILINE)
    # Guard the required migration properties are present.
    assert "get_table_names" in src  # tenant-schema guarded
    assert "get_check_constraints" in src  # idempotent
    assert "def downgrade" in src  # reversible
    assert "ck_hierarchy_definitions_type" in src


# ---------------------------------------------------------------------------
# Bug-7198 — hierarchy-health physical calendar column check
# ---------------------------------------------------------------------------

def _base_time_hier():
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "date_embedded"
    hier.date_config = {"source_attribute_id": str(uuid4())}
    hier.dimension_kind = "time"
    hier.calendar_type = "retail_445"
    hier.fiscal_year_start_month = None
    return hier


def _make_calendar_row(cal_type, **col_overrides):
    """A CalendarTable-like row whose configured mapping defaults to the
    canonical CALENDAR_COLUMN_SETS names for *cal_type* unless overridden."""
    from shared.semantic.calendar_dialects import CALENDAR_COLUMN_SETS

    mapping = dict(CALENDAR_COLUMN_SETS.get(cal_type) or {})
    row = SimpleNamespace(
        calendar_type=cal_type,
        date_column=mapping.get("date_column"),
        year_column=mapping.get("year_column"),
        half_column=mapping.get("half_column"),
        quarter_column=mapping.get("quarter_column"),
        month_column=mapping.get("month_column"),
        week_column=mapping.get("week_column"),
        day_column=mapping.get("day_column"),
    )
    for k, v in col_overrides.items():
        setattr(row, k, v)
    return row


@pytest.mark.asyncio
async def test_calendar_binding_flags_missing_physical_columns():
    """A reachable retail_445 calendar whose CONFIGURED period column is absent
    as a physical ModelColumn must raise ``calendar_source_columns_missing``
    (Bug-7198)."""
    from src.api import hierarchy_health as hh

    hier = _base_time_hier()
    fact_id = uuid4()
    cal_alias_id = uuid4()
    cal_table_id = uuid4()

    cal_row = _make_calendar_row("retail_445")

    db = AsyncMock()
    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []
    join_result = MagicMock()
    join_result.all.return_value = [(fact_id, cal_alias_id)]
    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id
    cal_alias_result = MagicMock()
    cal_alias_result.all.return_value = [(cal_alias_id, cal_table_id)]
    # present physical columns on the reachable alias — only date_key present.
    present_cols_result = MagicMock()
    present_cols_result.all.return_value = [("date_key",)]
    # Bug-5680 type-mismatch block re-queries aliases (matching type => no issue).
    cal_alias_result2 = MagicMock()
    cal_alias_result2.all.return_value = [(cal_alias_id, cal_table_id)]

    db.execute = AsyncMock(
        side_effect=[
            levels_result,
            join_result,
            fact_result,
            cal_alias_result,
            present_cols_result,
            cal_alias_result2,
        ]
    )
    # db.get(CalendarTable, ...) is called by both the columns check and the
    # type-mismatch check.
    db.get = AsyncMock(return_value=cal_row)

    issues = await hh._check_hierarchy_health(db, hier)
    missing = [
        i for i in issues if i["issue_type"] == "calendar_source_columns_missing"
    ]
    assert missing, f"expected calendar_source_columns_missing, got {issues}"
    assert missing[0]["severity"] == "error"
    assert "retail_year" in missing[0]["detail"]["missing_columns"]


@pytest.mark.asyncio
async def test_calendar_binding_ok_when_configured_columns_present():
    """When every CONFIGURED period column exists as a physical ModelColumn the
    missing-columns issue is not raised (Bug-7198)."""
    from src.api import hierarchy_health as hh
    from shared.semantic.calendar_dialects import CALENDAR_COLUMN_SETS

    hier = _base_time_hier()
    fact_id = uuid4()
    cal_alias_id = uuid4()
    cal_table_id = uuid4()

    cal_row = _make_calendar_row("retail_445")
    present = list(CALENDAR_COLUMN_SETS["retail_445"].values())

    db = AsyncMock()
    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []
    join_result = MagicMock()
    join_result.all.return_value = [(fact_id, cal_alias_id)]
    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id
    cal_alias_result = MagicMock()
    cal_alias_result.all.return_value = [(cal_alias_id, cal_table_id)]
    present_cols_result = MagicMock()
    present_cols_result.all.return_value = [(c,) for c in present]
    cal_alias_result2 = MagicMock()
    cal_alias_result2.all.return_value = [(cal_alias_id, cal_table_id)]

    db.execute = AsyncMock(
        side_effect=[
            levels_result,
            join_result,
            fact_result,
            cal_alias_result,
            present_cols_result,
            cal_alias_result2,
        ]
    )
    db.get = AsyncMock(return_value=cal_row)

    issues = await hh._check_hierarchy_health(db, hier)
    assert not [
        i for i in issues if i["issue_type"] == "calendar_source_columns_missing"
    ]


@pytest.mark.asyncio
async def test_calendar_binding_flags_unmapped_period_key():
    """Bug-7198 R2 finding 1: a bound calendar whose required period key is
    UNMAPPED (field NULL) — even if all its OTHER columns physically exist —
    must be flagged, because runtime reads only the configured mapping and
    fails at query time on the unmapped key."""
    from src.api import hierarchy_health as hh

    hier = _base_time_hier()
    fact_id = uuid4()
    cal_alias_id = uuid4()
    cal_table_id = uuid4()

    # retail_445 with year_column left UNMAPPED (None) but everything else set.
    cal_row = _make_calendar_row("retail_445", year_column=None)
    # Every physically-present column that IS mapped exists — so the only gap is
    # the unmapped key, not a missing physical column.
    from shared.semantic.calendar_dialects import CALENDAR_COLUMN_SETS

    mapping = CALENDAR_COLUMN_SETS["retail_445"]
    present = [
        v for k, v in mapping.items() if k != "year_column"
    ]

    db = AsyncMock()
    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []
    join_result = MagicMock()
    join_result.all.return_value = [(fact_id, cal_alias_id)]
    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id
    cal_alias_result = MagicMock()
    cal_alias_result.all.return_value = [(cal_alias_id, cal_table_id)]
    present_cols_result = MagicMock()
    present_cols_result.all.return_value = [(c,) for c in present]
    cal_alias_result2 = MagicMock()
    cal_alias_result2.all.return_value = [(cal_alias_id, cal_table_id)]

    db.execute = AsyncMock(
        side_effect=[
            levels_result,
            join_result,
            fact_result,
            cal_alias_result,
            present_cols_result,
            cal_alias_result2,
        ]
    )
    db.get = AsyncMock(return_value=cal_row)

    issues = await hh._check_hierarchy_health(db, hier)
    gap = [
        i for i in issues if i["issue_type"] == "calendar_source_columns_missing"
    ]
    assert gap, f"expected an unmapped-key gap, got {issues}"
    assert "year_column" in gap[0]["detail"]["unmapped_period_keys"]


@pytest.mark.asyncio
async def test_calendar_binding_uses_non_canonical_configured_names():
    """Review finding 1: a calendar registered under NON-canonical physical
    names must validate against its CONFIGURED mapping, not the canonical DDL
    names — no false alarm when the configured columns are all present."""
    from src.api import hierarchy_health as hh

    hier = _base_time_hier()
    fact_id = uuid4()
    cal_alias_id = uuid4()
    cal_table_id = uuid4()

    # Retail calendar whose physical columns are named ry/rq/rp/rw + dt.
    cal_row = _make_calendar_row(
        "retail_445",
        date_column="dt",
        year_column="ry",
        quarter_column="rq",
        month_column="rp",
        week_column="rw",
    )
    present = ["dt", "ry", "rq", "rp", "rw"]

    db = AsyncMock()
    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []
    join_result = MagicMock()
    join_result.all.return_value = [(fact_id, cal_alias_id)]
    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id
    cal_alias_result = MagicMock()
    cal_alias_result.all.return_value = [(cal_alias_id, cal_table_id)]
    present_cols_result = MagicMock()
    present_cols_result.all.return_value = [(c,) for c in present]
    cal_alias_result2 = MagicMock()
    cal_alias_result2.all.return_value = [(cal_alias_id, cal_table_id)]

    db.execute = AsyncMock(
        side_effect=[
            levels_result,
            join_result,
            fact_result,
            cal_alias_result,
            present_cols_result,
            cal_alias_result2,
        ]
    )
    db.get = AsyncMock(return_value=cal_row)

    issues = await hh._check_hierarchy_health(db, hier)
    assert not [
        i for i in issues if i["issue_type"] == "calendar_source_columns_missing"
    ], f"non-canonical names should not false-alarm; got {issues}"


# ---------------------------------------------------------------------------
# Bug-7005 — pocket budget helpers + enforcement
# ---------------------------------------------------------------------------

class TestPocketBudgetResolution:
    @pytest.mark.asyncio
    async def test_model_budget_overrides_project(self):
        from src.api.pockets import _resolve_effective_pocket_budget

        model = SimpleNamespace(pocket_size_budget_bytes=1000, project_id=uuid4())
        db = AsyncMock()
        db.get = AsyncMock(
            return_value=SimpleNamespace(pocket_size_budget_bytes=9999)
        )
        assert await _resolve_effective_pocket_budget(db, model) == 1000

    @pytest.mark.asyncio
    async def test_inherits_project_budget_when_model_null(self):
        from src.api.pockets import _resolve_effective_pocket_budget

        model = SimpleNamespace(pocket_size_budget_bytes=None, project_id=uuid4())
        db = AsyncMock()
        db.get = AsyncMock(
            return_value=SimpleNamespace(pocket_size_budget_bytes=500)
        )
        assert await _resolve_effective_pocket_budget(db, model) == 500

    @pytest.mark.asyncio
    async def test_unlimited_when_both_null(self):
        from src.api.pockets import _resolve_effective_pocket_budget

        model = SimpleNamespace(pocket_size_budget_bytes=None, project_id=uuid4())
        db = AsyncMock()
        db.get = AsyncMock(
            return_value=SimpleNamespace(pocket_size_budget_bytes=None)
        )
        assert await _resolve_effective_pocket_budget(db, model) is None

    @pytest.mark.asyncio
    async def test_current_usage_sums_live_pocket_bytes(self):
        from src.api.pockets import _current_pocket_usage_bytes

        db = AsyncMock()
        scalar_result = MagicMock()
        scalar_result.scalar_one.return_value = 4096
        db.execute = AsyncMock(return_value=scalar_result)
        assert await _current_pocket_usage_bytes(db, uuid4()) == 4096


# ---------------------------------------------------------------------------
# Bug-7279 — scratchpad expression must bind against the model
# ---------------------------------------------------------------------------

class TestScratchpadModelValidation:
    @pytest.mark.asyncio
    async def test_router_ok_passes(self, monkeypatch):
        from src.api import scratchpad_measures as sm

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(sm.httpx, "AsyncClient", _Client)
        # No exception == accepted.
        await sm._validate_expression_against_model(
            uuid4(), "sales", "amount * 0.9", "tok"
        )

    @pytest.mark.asyncio
    async def test_router_rejects_unbindable_expression(self, monkeypatch):
        from src.api import scratchpad_measures as sm

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": False, "errors": ["unknown column no_such_col"]}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(sm.httpx, "AsyncClient", _Client)
        with pytest.raises(HTTPException) as exc:
            await sm._validate_expression_against_model(
                uuid4(), "sales", "no_such_col + 1", "tok"
            )
        assert exc.value.status_code == 400
        assert "no_such_col" in exc.value.detail

    @pytest.mark.asyncio
    async def test_router_http_error_status_rejects(self, monkeypatch):
        from src.api import scratchpad_measures as sm

        class _Resp:
            status_code = 400

            def json(self):
                return {"detail": "parse failed"}

            text = "parse failed"

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(sm.httpx, "AsyncClient", _Client)
        with pytest.raises(HTTPException) as exc:
            await sm._validate_expression_against_model(
                uuid4(), "sales", "1 +", "tok"
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_bug_8162_router_unreachable_fails_closed(self, monkeypatch):
        """Bug-8162: a network error is UNKNOWN, not permission to persist.

        Supersedes the former ``test_router_unreachable_fails_open``. That test
        asserted the opposite contract, which was reversed by the user's
        decision on 2026-08-11 (``work/porting-remediation-lane-plan.md`` D-7):
        an unvalidated expression persisted during an outage renders later as a
        silent all-NULL column, which is the exact defect this validation
        exists to prevent.
        """
        from src.api import scratchpad_measures as sm

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise httpx.ConnectError("router down")

        monkeypatch.setattr(sm.httpx, "AsyncClient", _Client)
        with pytest.raises(HTTPException) as exc:
            await sm._validate_expression_against_model(
                uuid4(), "sales", "amount", "tok"
            )
        # 503, not 400: the expression was never judged, so the caller must be
        # told to retry — not told their (correct) expression is invalid.
        assert exc.value.status_code == 503
        assert "validator_unavailable" in exc.value.detail
        assert "not been rejected" in exc.value.detail

    @pytest.mark.asyncio
    async def test_bug_8162_router_5xx_fails_closed(self, monkeypatch):
        """Bug-8162: a 5xx (router deploy / proxy outage) is unavailability.

        Supersedes the former ``test_router_5xx_fails_open``. Unavailability is
        still not a verdict — the difference is that it now REFUSES instead of
        admitting, while still not blaming the user's expression.
        """
        from src.api import scratchpad_measures as sm

        class _Resp:
            status_code = 503
            text = "service unavailable"

            def json(self):
                return {"detail": "unavailable"}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(sm.httpx, "AsyncClient", _Client)
        with pytest.raises(HTTPException) as exc:
            await sm._validate_expression_against_model(
                uuid4(), "sales", "amount", "tok"
            )
        assert exc.value.status_code == 503
        assert "validator_unavailable" in exc.value.detail
        assert "not been rejected" in exc.value.detail

    @pytest.mark.asyncio
    async def test_bug_8162_real_rejection_still_blames_the_expression(
        self, monkeypatch
    ):
        """Bug-8162 (the other half): a VERDICT must not read as an outage.

        Failing closed is only half the decision; failing VISIBLY is the
        binding other half. A refusal that says "retry" for a genuinely wrong
        expression is as misleading as a 400 that says "you are wrong" for an
        outage, so both directions are pinned here in one test.
        """
        from src.api import scratchpad_measures as sm

        class _Resp:
            status_code = 200

            def json(self):
                return {"ok": False, "errors": ["unknown column no_such_col"]}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(sm.httpx, "AsyncClient", _Client)
        with pytest.raises(HTTPException) as exc:
            await sm._validate_expression_against_model(
                uuid4(), "sales", "no_such_col + 1", "tok"
            )
        assert exc.value.status_code == 400
        assert "not valid for this model" in exc.value.detail
        assert "no_such_col" in exc.value.detail
        # The outage vocabulary must NOT appear on a real verdict.
        assert "validator_unavailable" not in exc.value.detail
        assert "retry" not in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_router_4xx_rejects(self, monkeypatch):
        from src.api import scratchpad_measures as sm

        class _Resp:
            status_code = 400
            text = "bad expression"

            def json(self):
                return {"detail": "column not found"}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(sm.httpx, "AsyncClient", _Client)
        with pytest.raises(HTTPException) as exc:
            await sm._validate_expression_against_model(
                uuid4(), "sales", "bad_col", "tok"
            )
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Endpoint wiring (review finding 4): the guard must fire at the boundary.
# ---------------------------------------------------------------------------

class TestPocketBudgetEndpointWiring:
    @pytest.mark.asyncio
    async def test_create_pocket_refused_when_budget_exhausted(self, client):
        """Bug-7005: create_pocket must return 409 when the effective budget is
        exhausted — proving the guard is wired at the endpoint, not just the
        helper."""
        db = make_mock_db()
        scoped_model = SimpleNamespace(
            id=TEST_MODEL_ID,
            project_id=TEST_PROJECT_ID,
            slug="modely",
            display_name="Model Y",
            seed="deadbeef",
        )
        target = SimpleNamespace(
            id=uuid.uuid4(), model_id=TEST_MODEL_ID, project_connection_id=None,
        )

        async def _get(cls, obj_id):
            from shared.db.models import DataTarget

            if cls is Model:
                return scoped_model
            if cls is DataTarget:
                return target
            return None

        db.get = AsyncMock(side_effect=_get)

        with patch("src.api.pockets.get_tenant_db", async_gen_from(db)), \
             patch(
                 "src.api.pockets._validate_via_router",
                 AsyncMock(return_value=_router_ok()),
             ), \
             patch(
                 "src.api.pockets._check_pocket_structure",
                 MagicMock(return_value=[]),
             ), \
             patch(
                 "src.api.pockets._pocket_combo_error",
                 AsyncMock(return_value=None),
             ), \
             patch(
                 "src.api.pockets._resolve_effective_pocket_budget",
                 AsyncMock(return_value=1000),
             ), \
             patch(
                 "src.api.pockets._current_pocket_usage_bytes",
                 AsyncMock(return_value=1000),
             ):
            resp = await client.post(
                POCKET_PREFIX,
                json={
                    "target_id": str(target.id),
                    "defining_sql": "SELECT * FROM modely",
                    "refresh_policy": "manual",
                    "ttl_days": 14,
                },
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 409, resp.text
        assert "budget" in resp.json()["detail"].lower()
        # The pocket row must never be committed.
        db.commit.assert_not_called()


class TestScratchpadEndpointWiring:
    @pytest.mark.asyncio
    async def test_create_calls_model_validation(self, client):
        """Bug-7279: create endpoint must invoke the model-binding validation."""
        db = make_mock_db()
        scoped_model = SimpleNamespace(
            id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        )

        async def _get(cls, obj_id):
            if cls is Model:
                return scoped_model
            return None

        db.get = AsyncMock(side_effect=_get)

        created = ScratchpadMeasure(
            model_id=TEST_MODEL_ID,
            name="m1",
            expression="amount * 0.9",
            data_type="numeric",
            created_by="admin@test.com",
        )
        from datetime import datetime, timezone

        created.id = uuid.uuid4()
        created.created_at = datetime.now(timezone.utc)
        created.updated_at = datetime.now(timezone.utc)

        async def _refresh(obj):
            obj.id = created.id
            obj.created_at = created.created_at
            obj.updated_at = created.updated_at

        db.refresh = AsyncMock(side_effect=_refresh)

        validate_mock = AsyncMock()
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)), \
             patch(
                 "src.api.scratchpad_measures._validate_expression_against_model",
                 validate_mock,
             ):
            resp = await client.post(
                SCRATCHPAD_PREFIX,
                json={"name": "m1", "expression": "amount * 0.9"},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 201, resp.text
        validate_mock.assert_awaited_once()
        # The expression under test must be the one validated.
        assert validate_mock.await_args.args[2] == "amount * 0.9"

    @pytest.mark.asyncio
    async def test_create_rejects_when_model_validation_fails(self, client):
        """Bug-7279: a rejected expression must 400 and never persist."""
        db = make_mock_db()
        scoped_model = SimpleNamespace(
            id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        )

        async def _get(cls, obj_id):
            if cls is Model:
                return scoped_model
            return None

        db.get = AsyncMock(side_effect=_get)

        reject = AsyncMock(
            side_effect=HTTPException(
                status_code=400, detail="Expression is not valid for this model"
            )
        )
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)), \
             patch(
                 "src.api.scratchpad_measures._validate_expression_against_model",
                 reject,
             ):
            resp = await client.post(
                SCRATCHPAD_PREFIX,
                json={"name": "m1", "expression": "no_such_col"},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 400, resp.text
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_bug_8162_create_refuses_and_503s_when_router_unavailable(
        self, client, monkeypatch
    ):
        """Bug-8162 END-TO-END: an outage refuses the write at the endpoint.

        Unlike the helper tests above, this drives the real HTTP boundary with
        the real ``_validate_expression_against_model`` (only ``httpx`` is
        stubbed), so it proves the refusal reaches the CALLER as a 503 and that
        nothing was persisted — the two facts the decision turns on. A helper
        test alone would not catch the 503 being swallowed or downgraded on the
        way out.
        """
        from src.api import scratchpad_measures as sm

        db = make_mock_db()
        scoped_model = SimpleNamespace(
            id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, slug="modely",
        )

        async def _get(cls, obj_id):
            if cls is Model:
                return scoped_model
            return None

        db.get = AsyncMock(side_effect=_get)

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise httpx.ConnectError("router down")

        monkeypatch.setattr(sm.httpx, "AsyncClient", _Client)
        with patch("src.api.scratchpad_measures.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                SCRATCHPAD_PREFIX,
                # A perfectly CORRECT expression: the point is that the caller
                # must not be told it is wrong.
                json={"name": "m1", "expression": "amount * 0.9"},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert "validator_unavailable" in detail
        assert "not been rejected" in detail
        # Fail CLOSED: the unvalidated expression must never reach storage, or
        # it renders later as a silent all-NULL column.
        db.commit.assert_not_called()
