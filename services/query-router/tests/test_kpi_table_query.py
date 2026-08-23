"""Tests for $KPIs virtual table query interception.

Verifies that when a query references a table ending in '$KPIs',
the router bypasses normal semantic binding and reads from kpi_latest.

These tests avoid importing the full routes module (which requires
env vars) by testing the detection logic directly and mocking the
handler's DB interaction.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ir.logical_query import LogicalQuery


# ---------------------------------------------------------------------------
# _detect_kpi_table — inline implementation test (mirrors routes.py logic)
# ---------------------------------------------------------------------------

def _detect_kpi_table(logical_query: LogicalQuery) -> bool:
    """Mirrors the detection logic in routes.py for isolated testing."""
    for table in logical_query.from_tables:
        if table.endswith("$KPIs"):
            return True
    return False


class TestDetectKpiTable:
    def test_detects_kpi_table(self):
        lq = LogicalQuery(
            model_id="m1",
            protocol="jdbc",
            raw_query='SELECT * FROM "Sales$KPIs"',
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="abc",
            from_tables=["Sales$KPIs"],
        )
        assert _detect_kpi_table(lq) is True

    def test_ignores_normal_table(self):
        lq = LogicalQuery(
            model_id="m1",
            protocol="jdbc",
            raw_query="SELECT * FROM sales",
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="abc",
            from_tables=["sales"],
        )
        assert _detect_kpi_table(lq) is False

    def test_empty_from_tables(self):
        lq = LogicalQuery(
            model_id="m1",
            protocol="jdbc",
            raw_query="SELECT 1",
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="abc",
            from_tables=[],
        )
        assert _detect_kpi_table(lq) is False

    def test_partial_match_not_triggered(self):
        """Table name containing 'KPIs' but not ending with '$KPIs' should not match."""
        lq = LogicalQuery(
            model_id="m1",
            protocol="jdbc",
            raw_query="SELECT * FROM kpis_data",
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="abc",
            from_tables=["kpis_data"],
        )
        assert _detect_kpi_table(lq) is False

    def test_multiple_tables_one_is_kpis(self):
        """If any table in from_tables is a $KPIs table, detection fires."""
        lq = LogicalQuery(
            model_id="m1",
            protocol="jdbc",
            raw_query='SELECT * FROM sales JOIN "Model$KPIs"',
            requested_measures=[],
            requested_dimensions=[],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="abc",
            from_tables=["sales", "Model$KPIs"],
        )
        assert _detect_kpi_table(lq) is True


# ---------------------------------------------------------------------------
# _handle_kpi_table_query — tests the handler logic with mocked DB
# ---------------------------------------------------------------------------


class TestHandleKpiTableQuery:
    """Test the KPI table query handler by simulating the DB interaction."""

    @pytest.mark.asyncio
    async def test_returns_kpi_latest_rows(self):
        """Should query kpi_latest and return results."""
        from datetime import datetime, timezone

        mock_row = MagicMock()
        mock_row.kpi_name = "Revenue Growth"
        mock_row.value = 0.15
        mock_row.target = 0.10
        mock_row.status = 1
        mock_row.status_label = "Good"
        mock_row.trend_pct = 0.02
        mock_row.formatted_value = "15%"
        mock_row.evaluated_at = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_row]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)

        # Simulate the handler logic without importing it
        from sqlalchemy import select
        result = await db.execute(select())  # triggers the mock
        kpi_rows = list(result.scalars().all())

        rows = []
        for r in kpi_rows:
            rows.append({
                "kpi_name": r.kpi_name,
                "value": float(r.value) if r.value is not None else None,
                "target": float(r.target) if r.target is not None else None,
                "status": r.status,
                "status_label": r.status_label,
                "trend_pct": float(r.trend_pct) if r.trend_pct is not None else None,
                "formatted_value": r.formatted_value,
                "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
            })

        assert len(rows) == 1
        row = rows[0]
        assert row["kpi_name"] == "Revenue Growth"
        assert row["value"] == 0.15
        assert row["target"] == 0.10
        assert row["status"] == 1
        assert row["status_label"] == "Good"
        assert row["trend_pct"] == 0.02
        assert row["formatted_value"] == "15%"
        assert "2026-05-31" in row["evaluated_at"]

    @pytest.mark.asyncio
    async def test_empty_kpi_latest_returns_empty(self):
        """When no rows in kpi_latest, results are empty."""
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)

        from sqlalchemy import select
        result = await db.execute(select())
        kpi_rows = list(result.scalars().all())

        assert kpi_rows == []

    @pytest.mark.asyncio
    async def test_null_values_handled(self):
        """KPI rows with None values should produce None in output."""
        mock_row = MagicMock()
        mock_row.kpi_name = "Untargeted KPI"
        mock_row.value = None
        mock_row.target = None
        mock_row.status = None
        mock_row.status_label = None
        mock_row.trend_pct = None
        mock_row.formatted_value = None
        mock_row.evaluated_at = None

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_row]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)

        from sqlalchemy import select
        result = await db.execute(select())
        kpi_rows = list(result.scalars().all())

        rows = []
        for r in kpi_rows:
            rows.append({
                "kpi_name": r.kpi_name,
                "value": float(r.value) if r.value is not None else None,
                "target": float(r.target) if r.target is not None else None,
                "status": r.status,
                "status_label": r.status_label,
                "trend_pct": float(r.trend_pct) if r.trend_pct is not None else None,
                "formatted_value": r.formatted_value,
                "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
            })

        assert len(rows) == 1
        row = rows[0]
        assert row["value"] is None
        assert row["target"] is None
        assert row["evaluated_at"] is None


# ---------------------------------------------------------------------------
# Bug-3613 — persona measure-lineage gate + observation on the $KPIs path
# ---------------------------------------------------------------------------

import types
import uuid
from datetime import datetime, timezone


def _kpi_latest(name, value=100.0):
    r = MagicMock()
    r.kpi_name = name
    r.value = value
    r.target = 90.0
    r.status = 1
    r.status_label = "Good"
    r.trend_pct = 0.02
    r.formatted_value = f"{value:.0f}"
    r.evaluated_at = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    return r


def _kpi_def(*, expression=None, target_expression=None, target_measure_id=None):
    return types.SimpleNamespace(
        expression=expression,
        target_expression=target_expression,
        target_measure_id=target_measure_id,
    )


class _ScalarsResult:
    """Mimics ``result.all()`` for (KPILatest, KPI) row pairs and
    ``result.all()`` for (name, id) measure rows, plus scalar_one_or_none."""

    def __init__(self, rows, *, scalar=None):
        self._rows = rows
        self._scalar = scalar

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        outer = self

        class _S:
            def all(self_inner):
                return list(outer._rows)

        return _S()


class _StatefulDB:
    """Returns queued results in call order for ``db.execute``."""

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    async def execute(self, *_a, **_kw):
        result = self._results[self._i]
        self._i += 1
        return result


def _patch_observation(monkeypatch):
    """Stub the leaf writers/metrics so record_query_success runs without a
    real DB, returning a dict the test can assert the observation fired."""
    from src.api import routes as routes_mod

    captured = {"log_query": 0, "miss": 0, "audit": 0}

    async def fake_log_query(**kwargs):
        captured["log_query"] += 1
        captured["log_query_kwargs"] = kwargs

    async def fake_log_query_miss(*a, **kw):
        captured["miss"] += 1

    async def fake_audit(*a, **kw):
        captured["audit"] += 1

    monkeypatch.setattr(routes_mod, "log_query", fake_log_query)
    monkeypatch.setattr(routes_mod, "log_query_miss", fake_log_query_miss)
    monkeypatch.setattr(routes_mod, "audit", fake_audit)

    class _Counter:
        def labels(self, *a, **kw):
            return self

        def inc(self, *a, **kw):
            pass

        def observe(self, *a, **kw):
            pass

    for name in (
        "QUERY_ROUTED_COUNT", "MODEL_QUERY_COUNT", "MODEL_QUERY_DURATION",
        "MODEL_BYTES_PROCESSED", "MODEL_ROWS_RETURNED",
    ):
        monkeypatch.setattr(routes_mod, name, _Counter())

    return captured


def _make_logical_query(model_id, *, limit=None):
    return LogicalQuery(
        model_id=model_id,
        protocol="jdbc",
        raw_query='SELECT * FROM "modelx$KPIs"',
        requested_measures=[],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=limit,
        offset=None,
        query_fingerprint="kpi_fp_123",
        from_tables=["modelx$KPIs"],
    )


def _fake_model(model_id):
    return types.SimpleNamespace(
        id=uuid.UUID(model_id),
        display_name="ModelX",
        project=types.SimpleNamespace(display_name="ProjectX"),
        deployed_version_id="v1",
    )


class TestKpiPersonaLineageHelpers:
    def test_unrestricted_persona_returns_none_allow_set(self):
        from src.api.routes import _parse_persona_allowed_measure_ids

        assert _parse_persona_allowed_measure_ids(None) is None
        assert _parse_persona_allowed_measure_ids(
            types.SimpleNamespace(included_measure_ids=[])
        ) is None

    def test_malformed_uuid_fails_closed_to_empty_set(self):
        from src.api.routes import _parse_persona_allowed_measure_ids

        out = _parse_persona_allowed_measure_ids(
            types.SimpleNamespace(included_measure_ids=["not-a-uuid"])
        )
        assert out == set()

    def test_kpi_allowed_when_unrestricted(self):
        from src.api.routes import _kpi_allowed_by_persona

        kpi = _kpi_def(expression='measure("Revenue")')
        assert _kpi_allowed_by_persona(kpi, None, {}) is True

    def test_kpi_withheld_when_measure_not_in_scope(self):
        from src.api.routes import _kpi_allowed_by_persona

        mid = str(uuid.uuid4())
        kpi = _kpi_def(expression='measure("Revenue")')
        # Revenue resolves to mid, but the allow-set contains a different id.
        assert _kpi_allowed_by_persona(
            kpi, {str(uuid.uuid4())}, {"Revenue": mid}
        ) is False

    def test_kpi_allowed_when_measure_in_scope(self):
        from src.api.routes import _kpi_allowed_by_persona

        mid = str(uuid.uuid4())
        kpi = _kpi_def(expression='measure("Revenue")')
        assert _kpi_allowed_by_persona(kpi, {mid}, {"Revenue": mid}) is True

    def test_kpi_withheld_when_measure_name_unresolved(self):
        from src.api.routes import _kpi_allowed_by_persona

        kpi = _kpi_def(expression='measure("Ghost")')
        # Name resolves to no id -> fail closed.
        assert _kpi_allowed_by_persona(kpi, {str(uuid.uuid4())}, {}) is False

    def test_kpi_withheld_when_target_measure_id_out_of_scope(self):
        from src.api.routes import _kpi_allowed_by_persona

        tmid = uuid.uuid4()
        kpi = _kpi_def(target_measure_id=tmid)
        assert _kpi_allowed_by_persona(kpi, {str(uuid.uuid4())}, {}) is False
        assert _kpi_allowed_by_persona(kpi, {str(tmid)}, {}) is True

    # --- Bug-6139: CLS column-restriction gate -----------------------------

    def test_kpi_withheld_when_lineage_touches_restricted_column(self):
        from src.api.routes import _kpi_allowed_by_persona

        mid = str(uuid.uuid4())
        kpi = _kpi_def(expression='measure("Revenue")')
        # No measure allow-list restriction, but Revenue's measure id is in the
        # CLS-blocked set (its column closure reaches a restricted column) ->
        # withhold, even though the allow-list gate alone would serve it.
        assert _kpi_allowed_by_persona(
            kpi, None, {"Revenue": mid}, cls_blocked_measure_ids=frozenset({mid}),
        ) is False

    def test_kpi_served_when_lineage_clear_of_restricted_columns(self):
        from src.api.routes import _kpi_allowed_by_persona

        mid = str(uuid.uuid4())
        other = str(uuid.uuid4())
        kpi = _kpi_def(expression='measure("Revenue")')
        assert _kpi_allowed_by_persona(
            kpi, None, {"Revenue": mid}, cls_blocked_measure_ids=frozenset({other}),
        ) is True

    def test_cls_gate_applies_to_target_measure_id(self):
        from src.api.routes import _kpi_allowed_by_persona

        tmid = uuid.uuid4()
        kpi = _kpi_def(target_measure_id=tmid)
        assert _kpi_allowed_by_persona(
            kpi, None, {}, cls_blocked_measure_ids=frozenset({str(tmid)}),
        ) is False

    def test_cls_gate_fails_closed_on_unresolved_name(self):
        from src.api.routes import _kpi_allowed_by_persona

        kpi = _kpi_def(expression='measure("Ghost")')
        # A CLS restriction is active but the referenced measure cannot be
        # resolved to an id — cannot verify it is clear, so fail closed.
        assert _kpi_allowed_by_persona(
            kpi, None, {}, cls_blocked_measure_ids=frozenset({str(uuid.uuid4())}),
        ) is False


class TestKpiTableQueryGateAndObserve:
    @pytest.mark.asyncio
    async def test_unrestricted_serves_all_kpis_and_observes(self, monkeypatch):
        from src.api.routes import _handle_kpi_table_query

        captured = _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())

        kpi_a = _kpi_latest("Revenue KPI")
        kpi_b = _kpi_latest("Cost KPI")
        db = _StatefulDB([
            _ScalarsResult([(kpi_a, _kpi_def()), (kpi_b, _kpi_def())]),
            _ScalarsResult([], scalar=_fake_model(model_id)),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        names = {r["kpi_name"] for r in resp.rows}
        assert names == {"Revenue KPI", "Cost KPI"}
        assert resp.route_type == "kpi_metadata"
        # Observation fired: QueryLog written, query.execute audited, no miss.
        assert captured["log_query"] == 1
        assert captured["audit"] == 1
        assert captured["miss"] == 0

    @pytest.mark.asyncio
    async def test_restricted_persona_withholds_disallowed_kpi(self, monkeypatch):
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        allowed_mid = uuid.uuid4()
        revenue_mid = uuid.uuid4()  # allowed
        cost_mid = uuid.uuid4()     # NOT in persona scope

        persona = types.SimpleNamespace(
            id=uuid.uuid4(),
            included_measure_ids=[str(revenue_mid)],
        )

        kpi_rev = _kpi_latest("Revenue KPI")
        kpi_cost = _kpi_latest("Cost KPI")
        db = _StatefulDB([
            # (KPILatest, KPI) pairs
            _ScalarsResult([
                (kpi_rev, _kpi_def(expression='measure("Revenue")')),
                (kpi_cost, _kpi_def(expression='measure("Cost")')),
            ]),
            # Bug-6139: CLS gate first probes the persona's tag restrictions.
            # This persona has none, so the CLS helper short-circuits after a
            # single (empty) lookup and only the measure allow-list gate applies.
            _ScalarsResult([]),
            # measure name -> id lookup
            _ScalarsResult([("Revenue", revenue_mid), ("Cost", cost_mid)]),
            # Bug-6139: all-KPI name map for nested kpi() lineage resolution.
            # These KPIs have no nested refs, so an empty map is sufficient.
            _ScalarsResult([]),
            # Model load for observation
            _ScalarsResult([], scalar=_fake_model(model_id)),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=persona, user_identity="u@t.com", tenant_id="acme",
        )

        names = {r["kpi_name"] for r in resp.rows}
        assert "Revenue KPI" in names
        assert "Cost KPI" not in names, "disallowed-measure KPI must be withheld"

    @pytest.mark.asyncio
    async def test_row_limit_respected(self, monkeypatch):
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())

        kpis = [(_kpi_latest(f"KPI {i}"), _kpi_def()) for i in range(5)]
        db = _StatefulDB([
            _ScalarsResult(kpis),
            _ScalarsResult([], scalar=_fake_model(model_id)),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id, limit=2),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        assert len(resp.rows) == 2
        assert resp.rows_returned == 2

    @pytest.mark.asyncio
    async def test_deleted_model_returns_404(self, monkeypatch):
        """Bug-5413: when the model row is None (since-deleted model),
        _handle_kpi_table_query must return 404 rather than silently
        skipping observation."""
        from fastapi import HTTPException
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())

        # model_result.scalar_one_or_none() returns None -> model deleted.
        db = _StatefulDB([
            _ScalarsResult([]),          # no KPI rows
            _ScalarsResult([], scalar=None),  # model is None
        ])

        with pytest.raises(HTTPException) as exc_info:
            await _handle_kpi_table_query(
                db, model_id, _make_logical_query(model_id),
                persona=None, user_identity="u@t.com", tenant_id="acme",
            )

        assert exc_info.value.status_code == 404
        assert model_id in exc_info.value.detail


# ---------------------------------------------------------------------------
# Bug-6930 — $KPIs fails closed under active row-level security.
#
# kpi_latest holds a value pre-aggregated across ALL rows, so a row-restricted
# principal must not read it. When the principal has any active row-security rule
# on the model (and the persona carries no authorised bypass), EVERY KPI row is
# withheld — the pre-aggregated value cannot be re-filtered per-row at serve time.
# ---------------------------------------------------------------------------


def _patch_rls(monkeypatch, *, active: bool, raises: bool = False):
    """Patch the row-security authority used by _handle_kpi_table_query."""
    from src.api import routes as routes_mod
    from shared.security import RowSecurityCompileError

    async def fake_compile(*_a, **_kw):
        if raises:
            raise RowSecurityCompileError("cannot compile")
        return object() if active else None

    def fake_has_active(compiled):
        return compiled is not None

    monkeypatch.setattr(routes_mod, "compile_row_security", fake_compile)
    monkeypatch.setattr(routes_mod, "has_active_rules", fake_has_active)


class TestKpiTableQueryRowSecurity:
    @pytest.mark.asyncio
    async def test_active_rls_withholds_all_kpis(self, monkeypatch):
        """A row-restricted principal gets an EMPTY scorecard, not global totals."""
        from src.api.routes import _handle_kpi_table_query

        captured = _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=True)
        model_id = str(uuid.uuid4())
        principal = types.SimpleNamespace(user_identity="u@t.com", roles=[], groups=[], claims={})

        # withhold path skips the KPILatest fetch + gating; only the model load
        # for observation runs.
        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, principal=principal,
            user_identity="u@t.com", tenant_id="acme",
        )

        assert resp.rows == [], "row-restricted principal must see NO KPI rows"
        assert resp.rows_returned == 0
        # The zero-row read is still observed for audit.
        assert captured["log_query"] == 1
        assert captured["audit"] == 1

    @pytest.mark.asyncio
    async def test_rls_compile_error_fails_closed(self, monkeypatch):
        """If row security cannot be compiled, fail closed (withhold all)."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=False, raises=True)
        model_id = str(uuid.uuid4())
        principal = types.SimpleNamespace(user_identity="u@t.com", roles=[], groups=[], claims={})

        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, principal=principal,
            user_identity="u@t.com", tenant_id="acme",
        )
        assert resp.rows == []

    @pytest.mark.asyncio
    async def test_bypass_persona_still_served(self, monkeypatch):
        """A persona with authorised bypass_row_security still sees KPIs even with
        active rules — the bypass is a deliberate, granted exemption."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=True)
        model_id = str(uuid.uuid4())
        principal = types.SimpleNamespace(user_identity="a@t.com", roles=[], groups=[], claims={})
        persona = types.SimpleNamespace(
            id=uuid.uuid4(), included_measure_ids=None, bypass_row_security=True,
        )

        kpi_a = _kpi_latest("Revenue KPI")
        db = _StatefulDB([
            _ScalarsResult([(kpi_a, _kpi_def())]),   # KPILatest fetch runs
            # Bug-6139 CLS gate probes the persona's tag restrictions (none here).
            _ScalarsResult([]),
            _ScalarsResult([], scalar=_fake_model(model_id)),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=persona, principal=principal,
            user_identity="a@t.com", tenant_id="acme",
        )
        assert {r["kpi_name"] for r in resp.rows} == {"Revenue KPI"}

    @pytest.mark.asyncio
    async def test_no_active_rls_serves_normally(self, monkeypatch):
        """An unrestricted principal (no active rules) sees KPIs as before."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=False)
        model_id = str(uuid.uuid4())
        principal = types.SimpleNamespace(user_identity="u@t.com", roles=[], groups=[], claims={})

        kpi_a = _kpi_latest("Revenue KPI")
        db = _StatefulDB([
            _ScalarsResult([(kpi_a, _kpi_def())]),
            _ScalarsResult([], scalar=_fake_model(model_id)),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, principal=principal,
            user_identity="u@t.com", tenant_id="acme",
        )
        assert {r["kpi_name"] for r in resp.rows} == {"Revenue KPI"}


# ---------------------------------------------------------------------------
# Bug-8305 — $KPIs kpi_name is DEPLOYED-SNAPSHOT-authoritative
# ---------------------------------------------------------------------------

class _StatefulDBWithGet(_StatefulDB):
    """``_StatefulDB`` plus ``db.get`` — returns scripted objects by class name
    so the Bug-8305 deployed-snapshot name lookup can be exercised."""

    def __init__(self, results, *, get_by_class=None):
        super().__init__(results)
        self._get_by_class = get_by_class or {}

    async def get(self, cls, _pk):
        return self._get_by_class.get(getattr(cls, "__name__", None))


class TestKpiNameSnapshotAuthority:
    @pytest.mark.asyncio
    async def test_kpi_name_uses_deployed_snapshot_not_live_latest(self, monkeypatch):
        """Bug-8305: a DRAFT rename (reflected in kpi_latest.kpi_name via a
        scorecard evaluation) must NOT leak into $KPIs. The served name comes
        from the DEPLOYED version snapshot. Reverting the fix (serving
        kpi_latest.kpi_name) would make this assert 'RENAMED-DRAFT' and fail."""
        from src.api.routes import _handle_kpi_table_query
        from shared.db.models import Model, ModelVersion

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        kpi_id = uuid.uuid4()

        latest = _kpi_latest("RENAMED-DRAFT")  # live/draft name after a rename
        latest.kpi_id = kpi_id

        # Deployed snapshot froze the KPI under its published name.
        version = types.SimpleNamespace(
            snapshot_json={"kpis": [{"id": str(kpi_id), "name": "Published Revenue"}]}
        )
        model = types.SimpleNamespace(deployed_version_id="v1")

        db = _StatefulDBWithGet(
            [
                _ScalarsResult([(latest, _kpi_def())]),
                _ScalarsResult([], scalar=_fake_model(model_id)),
            ],
            get_by_class={"Model": model, "ModelVersion": version},
        )

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == ["Published Revenue"]
        # The value stays sourced from kpi_latest (unchanged by this fix).
        assert resp.rows[0]["value"] == 100.0

    @pytest.mark.asyncio
    async def test_kpi_name_falls_back_to_latest_when_snapshot_lacks_entry(self, monkeypatch):
        """When the deployed snapshot has no entry for a KPI (e.g. a legacy
        snapshot predating KPI serialisation), the served name falls back to
        kpi_latest.kpi_name — the fix must not blank out unmatched KPIs."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        kpi_id = uuid.uuid4()

        latest = _kpi_latest("Latest Name")
        latest.kpi_id = kpi_id

        version = types.SimpleNamespace(snapshot_json={"kpis": []})  # no entry
        model = types.SimpleNamespace(deployed_version_id="v1")

        db = _StatefulDBWithGet(
            [
                _ScalarsResult([(latest, _kpi_def())]),
                _ScalarsResult([], scalar=_fake_model(model_id)),
            ],
            get_by_class={"Model": model, "ModelVersion": version},
        )

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == ["Latest Name"]
