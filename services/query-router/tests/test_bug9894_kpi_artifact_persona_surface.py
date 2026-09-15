"""Bug-9894 / persona-layering rule 4, audit row A26.

``kpi_latest`` holds ONE value per KPI, pre-aggregated over ALL rows. Under the
rule it may serve a persona only when the persona's own narrowing is something
the artifact demonstrably carries. Two things were wrong:

1. ``persona.default_filters`` was never consulted. A persona narrowed by a
   default filter -- a mandatory, non-overridable security scope
   (``merge_default_filters``, F-008-01) -- was served the GLOBAL number as if
   its filter had been applied. That is a wrong number, not a refusal.
2. The row-security channel refused correctly but SILENTLY: the response was
   indistinguishable from "this model has no deployed KPIs", so no caller could
   recompute the scorecard on the persona path.

Both are now one admissibility proof that names its reason on
``ExecuteResponse.kpi_artifact_skip_reason``, in the vocabulary the Named Query
materialised gate already uses.

These tests reuse the harness in ``test_kpi_table_query.py``: the handler is the
real one, the deployment authority and the observation tail are bound per test.
"""
from __future__ import annotations

import sys
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_kpi_table_query import (  # noqa: E402
    _ScalarsResult,
    _StatefulDB,
    _deploy_row,
    _deployed_shape,
    _fake_model,
    _kpi_def,
    _kpi_latest,
    _make_logical_query,
    _patch_authority,
    _patch_observation,
    _patch_rls,
)


def _persona(*, default_filters=None, bypass=False):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        included_measure_ids=[],
        included_dimension_ids=[],
        included_hierarchy_ids=[],
        default_filters=default_filters,
        bypass_row_security=bypass,
    )


def _principal():
    return types.SimpleNamespace(
        user_identity="u@t.com", roles=[], groups=[], claims={},
    )


def _served_db(model_id, kpis):
    """A DB that answers the model load then the (KPILatest, KPI) fetch."""
    return _StatefulDB([
        _ScalarsResult([], scalar=_fake_model(model_id)),
        _ScalarsResult([(latest, _kpi_def()) for latest in kpis]),
        # The CLS probe (persona present, no tag restrictions).
        _ScalarsResult([]),
    ])


class TestDefaultFilterPersonaIsNeverServedTheGlobalValue:
    """The defect: a persona narrowed ONLY by default filters (no row-security
    rule at all) read the artifact's global number."""

    @pytest.mark.asyncio
    async def test_default_filter_persona_gets_no_artifact_row(self, monkeypatch):
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=False)
        model_id = str(uuid.uuid4())
        kpi = _kpi_latest("Revenue KPI", value=1_000_000.0)
        _patch_authority(monkeypatch, _deployed_shape(kpi_rows=[_deploy_row(kpi)]))

        resp = await _handle_kpi_table_query(
            _served_db(model_id, [kpi]),
            model_id,
            _make_logical_query(model_id),
            persona=_persona(default_filters={"region": {"op": "eq", "value": "EMEA"}}),
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert resp.rows == [], (
            "a persona narrowed by default filters must NOT be served the "
            "global pre-aggregated KPI value"
        )
        assert resp.kpi_artifact_skip_reason == "default_filters_live"

    @pytest.mark.asyncio
    async def test_withheld_response_does_not_claim_a_kpi_latest_read(
        self, monkeypatch,
    ):
        """The audit row and the caller-facing reason must not print a read
        that never happened."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=False)
        model_id = str(uuid.uuid4())
        kpi = _kpi_latest("Revenue KPI")
        _patch_authority(monkeypatch, _deployed_shape(kpi_rows=[_deploy_row(kpi)]))

        resp = await _handle_kpi_table_query(
            _served_db(model_id, [kpi]),
            model_id,
            _make_logical_query(model_id),
            persona=_persona(default_filters={"region": {"op": "eq", "value": "EMEA"}}),
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert "SELECT * FROM kpi_latest" not in (resp.routed_sql or "")
        assert "withheld" in resp.reason

    @pytest.mark.asyncio
    async def test_persona_without_default_filters_is_still_served(
        self, monkeypatch,
    ):
        """The gate must narrow only what it has to: an unnarrowed persona
        keeps the pre-aggregated fast path."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=False)
        model_id = str(uuid.uuid4())
        kpi = _kpi_latest("Revenue KPI", value=1_000_000.0)
        _patch_authority(monkeypatch, _deployed_shape(kpi_rows=[_deploy_row(kpi)]))

        resp = await _handle_kpi_table_query(
            _served_db(model_id, [kpi]),
            model_id,
            _make_logical_query(model_id),
            persona=_persona(default_filters=None),
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == ["Revenue KPI"]
        assert resp.kpi_artifact_skip_reason is None
        assert "kpi_latest" in (resp.routed_sql or "")

    @pytest.mark.asyncio
    async def test_empty_default_filters_is_not_a_narrowing(self, monkeypatch):
        """``default_filters == {}`` is "no filters", exactly as
        ``merge_default_filters`` reads it -- it must not withhold."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=False)
        model_id = str(uuid.uuid4())
        kpi = _kpi_latest("Revenue KPI")
        _patch_authority(monkeypatch, _deployed_shape(kpi_rows=[_deploy_row(kpi)]))

        resp = await _handle_kpi_table_query(
            _served_db(model_id, [kpi]),
            model_id,
            _make_logical_query(model_id),
            persona=_persona(default_filters={}),
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == ["Revenue KPI"]
        assert resp.kpi_artifact_skip_reason is None

    @pytest.mark.asyncio
    async def test_no_persona_is_unaffected(self, monkeypatch):
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=False)
        model_id = str(uuid.uuid4())
        kpi = _kpi_latest("Revenue KPI")
        _patch_authority(monkeypatch, _deployed_shape(kpi_rows=[_deploy_row(kpi)]))

        resp = await _handle_kpi_table_query(
            _StatefulDB([
                _ScalarsResult([], scalar=_fake_model(model_id)),
                _ScalarsResult([(kpi, _kpi_def())]),
            ]),
            model_id,
            _make_logical_query(model_id),
            persona=None,
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == ["Revenue KPI"]
        assert resp.kpi_artifact_skip_reason is None


class TestRefusalIsNamedSoTheCallerCanRecomputeLive:
    """Withholding is only half the answer. A caller that cannot tell "your
    persona narrows the data" from "this model has no KPIs" cannot serve the
    persona its OWN rows, which is what owner decision 4.2(c) requires."""

    @pytest.mark.asyncio
    async def test_row_security_names_its_channel(self, monkeypatch):
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=True)
        model_id = str(uuid.uuid4())

        resp = await _handle_kpi_table_query(
            _StatefulDB([_ScalarsResult([], scalar=_fake_model(model_id))]),
            model_id,
            _make_logical_query(model_id),
            persona=None,
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert resp.rows == []
        assert resp.kpi_artifact_skip_reason == "rls_live"

    @pytest.mark.asyncio
    async def test_rls_compile_failure_names_its_channel(self, monkeypatch):
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=True, raises=True)
        model_id = str(uuid.uuid4())

        resp = await _handle_kpi_table_query(
            _StatefulDB([_ScalarsResult([], scalar=_fake_model(model_id))]),
            model_id,
            _make_logical_query(model_id),
            persona=None,
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert resp.rows == []
        assert resp.kpi_artifact_skip_reason == "rls_live"

    @pytest.mark.asyncio
    async def test_row_security_wins_over_default_filters_in_the_reason(
        self, monkeypatch,
    ):
        """Both channels active: one reason, and it names the stronger one so
        an operator reading the audit is not told the lesser cause."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=True)
        model_id = str(uuid.uuid4())

        resp = await _handle_kpi_table_query(
            _StatefulDB([_ScalarsResult([], scalar=_fake_model(model_id))]),
            model_id,
            _make_logical_query(model_id),
            persona=_persona(default_filters={"region": {"op": "eq", "value": "EMEA"}}),
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert resp.rows == []
        assert resp.kpi_artifact_skip_reason == "rls_live"

    @pytest.mark.asyncio
    async def test_authorised_bypass_still_serves_the_artifact(self, monkeypatch):
        """A persona with an authorised row-security bypass and no default
        filters sees every row of the global artifact, as before."""
        from src.api.routes import _handle_kpi_table_query

        _patch_observation(monkeypatch)
        _patch_rls(monkeypatch, active=True)
        model_id = str(uuid.uuid4())
        kpi = _kpi_latest("Revenue KPI")
        _patch_authority(monkeypatch, _deployed_shape(kpi_rows=[_deploy_row(kpi)]))

        resp = await _handle_kpi_table_query(
            _served_db(model_id, [kpi]),
            model_id,
            _make_logical_query(model_id),
            persona=_persona(bypass=True),
            principal=_principal(),
            user_identity="u@t.com",
            tenant_id="acme",
        )

        assert [r["kpi_name"] for r in resp.rows] == ["Revenue KPI"]
        assert resp.kpi_artifact_skip_reason is None
