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


def _kpi_latest(name, value=100.0, *, kpi_id=None):
    r = MagicMock()
    # A real id, not the auto-generated MagicMock attribute: the handler keys
    # the deployed KPI definition off it, so the fixtures must be able to bind
    # a snapshot entry to the same row.
    r.kpi_id = kpi_id if kpi_id is not None else uuid.uuid4()
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

    async def get(self, *_a, **_kw):
        """Report "no such row".

        These tests bind the deployment authority explicitly via
        ``_patch_authority``, so nothing on the handler path should reach this.
        It stays so that an unpatched path degrades to a missing row rather than
        an AttributeError, which is far easier to read in a failure.
        """
        return None


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


def _fake_model(model_id, *, deployed_version_id="v1", deploy_epoch=1):
    return types.SimpleNamespace(
        id=uuid.UUID(model_id),
        display_name="ModelX",
        project=types.SimpleNamespace(display_name="ProjectX"),
        deployed_version_id=deployed_version_id,
        deploy_epoch=deploy_epoch,
    )


def _deployed_shape(*, kpi_rows=(), measures=()):
    """A minimal stand-in for the pinned ``DeployedShape``.

    $KPIs serves ONLY rows whose KPI is present in the deployed snapshot: the
    served value was evaluated under the deployed definition, so that is the
    only definition it can be authorised against. There is no live-definition
    fallback, so every test expecting a row to be served must place that KPI in
    ``kpi_rows``.
    """
    return types.SimpleNamespace(
        measures=list(measures),
        kpi_rows=list(kpi_rows),
        uda_column_ref_rows=[],
        columns_by_id={},
    )


def _deploy_row(kpi_latest, *, name=None, expression=None,
                target_expression=None, target_measure_id=None,
                parent_kpi_id=None):
    """A deployed snapshot KPI entry bound to a ``kpi_latest`` row's id.

    ``name`` defaults to the live name; pass it explicitly to model a draft
    rename, where the deployed and live names deliberately differ.
    """
    return {
        "id": str(kpi_latest.kpi_id),
        "name": kpi_latest.kpi_name if name is None else name,
        "expression": expression,
        "target_expression": target_expression,
        "target_measure_id": target_measure_id,
        "parent_kpi_id": parent_kpi_id,
        "value_measure_id": None,
        "goal_measure_id": None,
    }


def _patch_authority(monkeypatch, shape):
    """Bind the handler's single deployment authority for one test.

    The handler captures deployment state exactly once per request and derives
    the value epoch, the CLS blocked set, the row authorisation and the served
    name from that one capture. Passing ``None`` models an undeployed model.
    """
    from src.api import routes as routes_mod
    from src.semantic.snapshot_resolver import SnapshotAuthority

    async def _authority(_model, _db):
        if shape is None:
            return SnapshotAuthority.UNDEPLOYED, None
        return SnapshotAuthority.DEPLOYED, shape

    monkeypatch.setattr(routes_mod, "resolve_snapshot_authority", _authority)


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
        _patch_authority(monkeypatch, _deployed_shape(
            kpi_rows=[_deploy_row(kpi_a), _deploy_row(kpi_b)],
        ))
        # The model is loaded FIRST: deployment state is captured once, before
        # anything reads it, and every later step derives from that capture.
        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult([(kpi_a, _kpi_def()), (kpi_b, _kpi_def())]),
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
        # Measure names and KPI definitions both come from the deployed shape,
        # so neither needs a database lookup.
        _patch_authority(monkeypatch, _deployed_shape(
            measures=[
                types.SimpleNamespace(id=revenue_mid, name="Revenue"),
                types.SimpleNamespace(id=cost_mid, name="Cost"),
            ],
            kpi_rows=[
                _deploy_row(kpi_rev, expression='measure("Revenue")'),
                _deploy_row(kpi_cost, expression='measure("Cost")'),
            ],
        ))
        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            # (KPILatest, KPI) pairs
            _ScalarsResult([
                (kpi_rev, _kpi_def(expression='measure("Revenue")')),
                (kpi_cost, _kpi_def(expression='measure("Cost")')),
            ]),
            # Bug-6139: CLS gate probes the persona's tag restrictions. This
            # persona has none, so the helper short-circuits after that single
            # (empty) lookup and only the measure allow-list gate applies.
            _ScalarsResult([]),
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
        _patch_authority(monkeypatch, _deployed_shape(
            kpi_rows=[_deploy_row(latest) for latest, _ in kpis],
        ))
        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult(kpis),
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

        # The model load is the FIRST statement the handler runs, and the only
        # one it may run before the 404: a single queued result proves nothing
        # else is read for a model that no longer exists.
        db = _StatefulDB([
            _ScalarsResult([], scalar=None),  # model is None -> deleted
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

        # The withhold path still captures the model (the deleted-model 404 and
        # the observation tail both need it) but skips the KPILatest fetch, the
        # authority resolution and all gating.
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
        _patch_authority(monkeypatch, _deployed_shape(
            kpi_rows=[_deploy_row(kpi_a)],
        ))
        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult([(kpi_a, _kpi_def())]),   # KPILatest fetch runs
            # Bug-6139 CLS gate probes the persona's tag restrictions (none here).
            _ScalarsResult([]),
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
        _patch_authority(monkeypatch, _deployed_shape(
            kpi_rows=[_deploy_row(kpi_a)],
        ))
        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult([(kpi_a, _kpi_def())]),
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

class TestKpiNameSnapshotAuthority:
    @pytest.mark.asyncio
    async def test_kpi_name_uses_deployed_snapshot_not_live_latest(self, monkeypatch):
        """Bug-8305: a DRAFT rename (reflected in kpi_latest.kpi_name via a
        scorecard evaluation) must NOT leak into $KPIs. The served name comes
        from the deployed definition — the same one that authorised the row, so
        the name and the authorisation cannot come from different versions."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())

        latest = _kpi_latest("RENAMED-DRAFT")  # live/draft name after a rename
        # The deployment froze the KPI under its published name.
        _patch_authority(monkeypatch, _deployed_shape(
            kpi_rows=[_deploy_row(latest, name="Published Revenue")],
        ))

        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult([(latest, _kpi_def())]),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == ["Published Revenue"]
        # The value stays sourced from kpi_latest (unchanged by this fix).
        assert resp.rows[0]["value"] == 100.0

    @pytest.mark.asyncio
    async def test_kpi_absent_from_the_deployment_is_withheld_even_unrestricted(
        self, monkeypatch,
    ):
        """A KPI the deployed snapshot does not describe is WITHHELD, including
        for a caller under no persona or row restriction at all.

        This replaces an earlier contract in which such a row was served under
        its live name. That fallback existed for deployments predating KPI
        serialisation, whose snapshots carry no KPI section, but it meant a
        value could be served with nothing authoritative to authorise it
        against — decided by the age of the deployment rather than by any
        security property. The rule is now unconditional (user decision,
        2026-09-01): those deployments serve an empty scorecard until they are
        redeployed, which is the behaviour operations expects.
        """
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())

        latest = _kpi_latest("Latest Name")
        _patch_authority(monkeypatch, _deployed_shape(kpi_rows=[]))  # no entry

        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult([(latest, _kpi_def())]),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        assert resp.rows == [], (
            "a KPI absent from the deployed snapshot has no authoritative "
            "definition to be checked against and must not be served"
        )
        # The zero-row read is still observed for audit.
        assert resp.rows_returned == 0


class TestKpiDeployedDefinitionAuthority:
    """Bug-9490 (review F1) — authorise the DEPLOYED KPI definition.

    The scorecard serves a value evaluated under the deployed definition. It
    used to authorise that value against the LIVE one, so editing a KPI in
    draft decided what an already-deployed value had been checked for.

    These drive the real handler. A unit test of the authorisation function
    alone proves the function can tell two definitions apart; it does NOT prove
    the handler hands it the deployed one, which is the whole defect. Reverting
    the fix must fail a test here.
    """

    @pytest.mark.asyncio
    async def test_deployed_definition_withholds_a_kpi_a_clean_draft_would_serve(
        self, monkeypatch,
    ):
        from src.api import routes as routes_mod
        from src.api.routes import _handle_kpi_table_query
        from src.semantic.snapshot_resolver import SnapshotAuthority

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        salary_mid = uuid.uuid4()      # restricted by the persona
        headcount_mid = uuid.uuid4()   # clean

        persona = types.SimpleNamespace(
            id=uuid.uuid4(), included_measure_ids=[str(headcount_mid)],
        )

        kpi_id = uuid.uuid4()
        payroll = _kpi_latest("Payroll KPI")
        payroll.kpi_id = kpi_id

        # DEPLOYED definition reads the restricted measure.
        deployed_shape = types.SimpleNamespace(
            # With a pinned shape the handler resolves measure names from it,
            # not from the database — so no name lookup is queued below.
            measures=[
                types.SimpleNamespace(id=salary_mid, name="Salary"),
                types.SimpleNamespace(id=headcount_mid, name="Headcount"),
            ],
            uda_column_ref_rows=[], columns_by_id={},
            kpi_rows=[{
                "id": str(kpi_id), "name": "Payroll KPI",
                "expression": 'measure("Salary")', "parent_kpi_id": None,
                "value_measure_id": None, "goal_measure_id": None,
                "target_measure_id": None,
            }],
        )

        async def _authority(*_a, **_kw):
            return SnapshotAuthority.DEPLOYED, deployed_shape

        monkeypatch.setattr(routes_mod, "resolve_snapshot_authority", _authority)

        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            # The LIVE row the join returns reads the CLEAN measure — this is
            # the draft edit that used to decide the outcome.
            _ScalarsResult([(payroll, _kpi_def(expression='measure("Headcount")'))]),
            _ScalarsResult([]),  # persona tag restrictions: none
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=persona, user_identity="u@t.com", tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == [], (
            "the DEPLOYED definition reads a measure outside the persona scope, "
            "so the served value must be withheld — authorising the clean draft "
            "definition instead is the Bug-9490 F1 bypass"
        )

    @pytest.mark.asyncio
    async def test_a_kpi_absent_from_the_deployment_is_withheld(self, monkeypatch):
        """No deployed definition means nothing authoritative to check against."""
        from src.api import routes as routes_mod
        from src.api.routes import _handle_kpi_table_query
        from src.semantic.snapshot_resolver import SnapshotAuthority

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        headcount_mid = uuid.uuid4()
        persona = types.SimpleNamespace(
            id=uuid.uuid4(), included_measure_ids=[str(headcount_mid)],
        )

        ghost = _kpi_latest("Draft Only KPI")
        ghost.kpi_id = uuid.uuid4()

        deployed_shape = types.SimpleNamespace(
            measures=[types.SimpleNamespace(id=headcount_mid, name="Headcount")],
            uda_column_ref_rows=[], columns_by_id={}, kpi_rows=[],
        )

        async def _authority(*_a, **_kw):
            return SnapshotAuthority.DEPLOYED, deployed_shape

        monkeypatch.setattr(routes_mod, "resolve_snapshot_authority", _authority)

        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult([(ghost, _kpi_def(expression='measure("Headcount")'))]),
            _ScalarsResult([]),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=persona, user_identity="u@t.com", tenant_id="acme",
        )
        assert [r["kpi_name"] for r in resp.rows] == []

    @pytest.mark.asyncio
    async def test_unrestricted_persona_still_receives_its_scorecard(
        self, monkeypatch,
    ):
        """Review finding 1A — an unrestricted persona must not get an empty
        scorecard.

        An empty ``included_measure_ids`` means UNRESTRICTED (the parser returns
        None for it), so no lineage gate runs. The deployed-definition map was
        only built when a gate WAS active, while the row loop demanded a entry
        from it whenever a deployed shape existed — so every row was discarded
        and the persona saw nothing at all. Personas that restrict only
        dimensions, and RLS-bypass personas, hit the same state.
        """
        from src.api import routes as routes_mod
        from src.api.routes import _handle_kpi_table_query
        from src.semantic.snapshot_resolver import SnapshotAuthority

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        kpi_id = uuid.uuid4()
        row = _kpi_latest("Revenue KPI")
        row.kpi_id = kpi_id

        persona = types.SimpleNamespace(id=uuid.uuid4(), included_measure_ids=[])

        deployed_shape = types.SimpleNamespace(
            measures=[], uda_column_ref_rows=[], columns_by_id={},
            kpi_rows=[{
                "id": str(kpi_id), "name": "Revenue KPI", "expression": None,
                "parent_kpi_id": None, "value_measure_id": None,
                "goal_measure_id": None, "target_measure_id": None,
            }],
        )

        async def _authority(*_a, **_kw):
            return SnapshotAuthority.DEPLOYED, deployed_shape

        monkeypatch.setattr(routes_mod, "resolve_snapshot_authority", _authority)

        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult([(row, _kpi_def())]),
            _ScalarsResult([]),  # persona tag restrictions: none
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=persona, user_identity="u@t.com", tenant_id="acme",
        )
        assert [r["kpi_name"] for r in resp.rows] == ["Revenue KPI"], (
            "an unrestricted persona received an empty scorecard"
        )


class TestKpiSingleDeploymentAuthority:
    """Consolidation (review 1B/1C) — ONE deployment authority per request.

    The handler used to decide deployment three separate times: the value query
    joined live ``Model.deploy_epoch``, the security shape was resolved only
    when a persona happened to exist, and the served names came from an
    independent ``ModelVersion`` reload. Nothing tied the three to one moment,
    so a deploy landing mid-request could pair a value evaluated under the NEW
    definition with lineage and metadata from the OLD one.

    These guard the property that replaced it: deployment state is captured
    once, and every consumer derives from that single capture.
    """

    class _RecordingDB(_StatefulDB):
        """``_StatefulDB`` that keeps the text of every statement executed."""

        def __init__(self, results):
            super().__init__(results)
            self.statements: list[str] = []

        async def execute(self, stmt, *a, **kw):
            self.statements.append(str(stmt))
            return await super().execute(stmt, *a, **kw)

    @pytest.mark.asyncio
    async def test_deployment_state_is_read_exactly_once(self, monkeypatch):
        """One model load and one authority resolution for the whole request.

        A second read of either is a second point in time, which is the defect
        this consolidation removed.
        """
        from src.api import routes as routes_mod
        from src.api.routes import _handle_kpi_table_query
        from src.semantic.snapshot_resolver import SnapshotAuthority

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        latest = _kpi_latest("Revenue KPI")
        shape = _deployed_shape(kpi_rows=[_deploy_row(latest)])

        calls = {"authority": 0}

        async def _authority(_model, _db):
            calls["authority"] += 1
            return SnapshotAuthority.DEPLOYED, shape

        monkeypatch.setattr(routes_mod, "resolve_snapshot_authority", _authority)

        db = self._RecordingDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
            _ScalarsResult([(latest, _kpi_def())]),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == ["Revenue KPI"]
        assert calls["authority"] == 1, (
            "the deployment authority must be resolved once per request"
        )
        model_reads = [t for t in db.statements if "FROM models " in t]
        assert len(model_reads) == 1, (
            f"expected exactly one model load, got {len(model_reads)}: {db.statements}"
        )

    @pytest.mark.asyncio
    async def test_value_query_pins_the_captured_epoch_without_rereading_it(
        self, monkeypatch,
    ):
        """The value query filters on the CAPTURED epoch, not a fresh join.

        Joining ``Model`` re-read deployment state at statement time, so a
        deploy landing between the capture and the value fetch could hand back
        a value from one deployment to be authorised against another's lineage.
        """
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        latest = _kpi_latest("Revenue KPI")
        _patch_authority(monkeypatch, _deployed_shape(
            kpi_rows=[_deploy_row(latest)],
        ))

        db = self._RecordingDB([
            _ScalarsResult([], scalar=_fake_model(model_id, deploy_epoch=7)),
            _ScalarsResult([(latest, _kpi_def())]),
        ])

        await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        value_queries = [t for t in db.statements if "kpi_latest" in t]
        assert len(value_queries) == 1, db.statements
        value_query = value_queries[0]
        assert "evaluated_for_epoch" in value_query, (
            "the served value must be pinned to a deploy epoch"
        )
        assert "FROM models " not in value_query and "JOIN models " not in value_query, (
            "the value query must use the captured epoch, not re-read the "
            f"model's deployment state: {value_query}"
        )

    @pytest.mark.asyncio
    async def test_undeployed_model_serves_nothing(self, monkeypatch):
        """No deploy pointer means no captured version, so no rows are served
        even if stale ``kpi_latest`` rows survive an undeploy."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        latest = _kpi_latest("Stale KPI")
        _patch_authority(monkeypatch, None)  # UNDEPLOYED

        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(
                model_id, deployed_version_id=None,
            )),
            _ScalarsResult([(latest, _kpi_def())]),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )
        assert resp.rows == []

    @pytest.mark.asyncio
    async def test_unreadable_deployment_snapshot_fails_closed_with_503(
        self, monkeypatch,
    ):
        """A deployed model whose snapshot cannot be read is NOT 'undeployed'.

        Serving it would fall back to live draft metadata. It is refused, and —
        unlike before this consolidation — the check runs for every row-serving
        caller, not only when a persona happens to be bound.
        """
        from fastapi import HTTPException
        from src.api import routes as routes_mod
        from src.api.routes import _handle_kpi_table_query
        from src.semantic.snapshot_resolver import SnapshotAuthority

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())

        async def _authority(_model, _db):
            return SnapshotAuthority.DEPLOYED_SNAPSHOT_INVALID, None

        monkeypatch.setattr(routes_mod, "resolve_snapshot_authority", _authority)

        db = _StatefulDB([
            _ScalarsResult([], scalar=_fake_model(model_id)),
        ])

        with pytest.raises(HTTPException) as exc_info:
            await _handle_kpi_table_query(
                db, model_id, _make_logical_query(model_id),
                persona=None, user_identity="u@t.com", tenant_id="acme",
            )
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_missing_deploy_epoch_serves_nothing(self, monkeypatch):
        """A deployed model with no epoch stamp must serve no rows.

        The value predicate compares the CAPTURED epoch, and ``column == None``
        compiles to ``IS NULL`` — which MATCHES every unstamped legacy
        ``kpi_latest`` row rather than rejecting it. The join this replaced
        failed closed on that case for free, because SQL ``NULL = NULL`` is not
        true, so the capture has to refuse it explicitly.
        """
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        model_id = str(uuid.uuid4())
        latest = _kpi_latest("Unstamped KPI")
        _patch_authority(monkeypatch, _deployed_shape(
            kpi_rows=[_deploy_row(latest)],
        ))

        db = self._RecordingDB([
            _ScalarsResult([], scalar=_fake_model(model_id, deploy_epoch=None)),
            # Queued but must never be consumed: the epoch check comes first.
            _ScalarsResult([(latest, _kpi_def())]),
        ])

        resp = await _handle_kpi_table_query(
            db, model_id, _make_logical_query(model_id),
            persona=None, user_identity="u@t.com", tenant_id="acme",
        )

        assert resp.rows == []
        assert not [t for t in db.statements if "kpi_latest" in t], (
            "no value query should be issued when the epoch cannot be pinned"
        )
