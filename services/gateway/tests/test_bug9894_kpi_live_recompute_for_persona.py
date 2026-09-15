"""Bug-9894 / persona-layering rule 4, audit row A26 -- the gateway half.

``kpi_latest`` is one value per KPI computed over ALL rows. When the
query-router proves that artifact cannot carry the caller's own narrowing it
serves no row and names the channel on ``kpi_artifact_skip_reason``. Owner
decision 4.2(c) is that such a persona is then served ITS OWN values, live --
not an empty scorecard, which reads as "this model has no KPIs".

This seam is what makes the JDBC scorecard agree with the XMLA one: the XMLA
KPI members already evaluate live per persona through ``evaluate_kpi_batch``,
and the JDBC ``$KPIs`` relation now takes the same route whenever the shared
pre-computed value does not apply to the caller.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client  # noqa: E402
from src.jdbc.server import PGWireServer  # noqa: E402

MODEL = "11111111-1111-1111-1111-111111111111"
PERSONA = "22222222-2222-2222-2222-222222222222"
KPI_ID = "33333333-3333-3333-3333-333333333333"

# What the router returns once it refuses the artifact: zero rows, and the
# reason that says why.
_WITHHELD = {
    "columns": [
        "kpi_name", "value", "target", "status",
        "status_label", "trend_pct", "formatted_value", "evaluated_at",
    ],
    "rows": [],
    "route_type": "kpi_metadata",
    "kpi_artifact_skip_reason": "default_filters_live",
}

# What it returns when the artifact DOES apply: the global value, no reason.
_SERVED = {
    "columns": _WITHHELD["columns"],
    "rows": [{
        "kpi_name": "Revenue KPI", "value": 1_000_000.0, "target": 900_000.0,
        "status": 1, "status_label": "Good", "trend_pct": 0.02,
        "formatted_value": "1000000", "evaluated_at": "2026-06-14T12:00:00+00:00",
    }],
    "route_type": "kpi_metadata",
    "kpi_artifact_skip_reason": None,
}


def _server():
    server = PGWireServer()
    server._tenant_slug = "acme"
    server._jwt_token = "jwt"
    server._table_model_id = {"sales$KPIs": MODEL}
    return server


def _patch_live(monkeypatch, *, value=250_000.0, kpis=None, raises=None):
    calls: dict = {}

    async def _get_model_kpis(model_id, tenant_slug, jwt_token, project_id=""):
        calls["kpi_list"] = model_id
        return [{"id": KPI_ID, "name": "Revenue KPI"}] if kpis is None else kpis

    async def _evaluate(kpi_ids, model_id, project_id, tenant_slug, jwt_token,
                        *, filters=None, persona_id=None):
        if raises is not None:
            raise raises
        calls["evaluate"] = {
            "kpi_ids": list(kpi_ids),
            "model_id": model_id,
            "jwt_token": jwt_token,
            "persona_id": persona_id,
        }
        return {KPI_ID: {
            "value": value, "target": 900_000.0, "status": 2,
            "status_label": "Watch", "trend_pct": -0.01,
            "formatted_value": f"{value:.0f}",
        }}

    monkeypatch.setattr(router_client, "get_model_kpis", _get_model_kpis)
    monkeypatch.setattr(router_client, "evaluate_kpi_batch", _evaluate)
    return calls


class TestANarrowedPersonaIsServedItsOwnNumbers:
    @pytest.mark.asyncio
    async def test_withheld_artifact_is_recomputed_live(self, monkeypatch):
        calls = _patch_live(monkeypatch, value=250_000.0)
        server = _server()

        columns, rows, err = await server._kpi_rows_for_persona(
            _WITHHELD, MODEL, PERSONA,
        )

        assert err is None
        assert [r["kpi_name"] for r in rows] == ["Revenue KPI"]
        assert rows[0]["value"] == 250_000.0, (
            "the persona must be served ITS value, not the global 1,000,000"
        )
        assert rows[0]["status_label"] == "Watch"
        assert columns == _WITHHELD["columns"], (
            "the live rows must carry the advertised $KPIs column shape so "
            "projection/WHERE/ORDER BY shaping is unchanged"
        )

    @pytest.mark.asyncio
    async def test_the_live_evaluation_carries_the_caller_and_the_persona(
        self, monkeypatch,
    ):
        """Equivalent by construction: the recompute re-enters the governed
        evaluate path under the CALLER'S OWN bearer and the relation persona,
        so the router applies that identity's full surface."""
        calls = _patch_live(monkeypatch)
        server = _server()

        await server._kpi_rows_for_persona(_WITHHELD, MODEL, PERSONA)

        assert calls["evaluate"]["jwt_token"] == "jwt"
        assert calls["evaluate"]["persona_id"] == PERSONA
        assert calls["evaluate"]["model_id"] == MODEL
        assert calls["evaluate"]["kpi_ids"] == [KPI_ID]

    @pytest.mark.asyncio
    async def test_base_surface_recompute_resolves_the_callers_own_persona(
        self, monkeypatch,
    ):
        """On the base ``<slug>$KPIs`` relation there is no relation persona.
        Passing None is correct -- model-service then resolves the caller's own
        effective persona from the same bearer."""
        calls = _patch_live(monkeypatch)
        server = _server()

        await server._kpi_rows_for_persona(_WITHHELD, MODEL, None)

        assert calls["evaluate"]["persona_id"] is None


class TestTheArtifactPathIsUntouchedWhenItApplies:
    @pytest.mark.asyncio
    async def test_served_artifact_is_passed_through_without_a_live_call(
        self, monkeypatch,
    ):
        calls = _patch_live(monkeypatch)
        server = _server()

        columns, rows, err = await server._kpi_rows_for_persona(
            _SERVED, MODEL, None,
        )

        assert err is None
        assert rows == _SERVED["rows"]
        assert columns == _SERVED["columns"]
        assert calls == {}, "no extra hop when the artifact already applied"

    @pytest.mark.asyncio
    async def test_a_response_without_the_field_is_passed_through(
        self, monkeypatch,
    ):
        """Back-compatibility: a router that predates the field is treated as
        "the artifact served", which is what it meant."""
        calls = _patch_live(monkeypatch)
        server = _server()

        legacy = {k: v for k, v in _SERVED.items()
                  if k != "kpi_artifact_skip_reason"}
        _columns, rows, err = await server._kpi_rows_for_persona(
            legacy, MODEL, None,
        )

        assert err is None
        assert rows == _SERVED["rows"]
        assert calls == {}


class TestFailClosedAndLoud:
    @pytest.mark.asyncio
    async def test_a_failed_recompute_reports_an_error_not_an_empty_scorecard(
        self, monkeypatch,
    ):
        """An empty scorecard reads as "this model has no KPIs". A caller whose
        values could not be computed must be told that instead."""
        _patch_live(monkeypatch, raises=ValueError("router refused"))
        server = _server()

        _columns, _rows, err = await server._kpi_rows_for_persona(
            _WITHHELD, MODEL, PERSONA,
        )

        assert err is not None
        message, sqlstate = err
        assert "could not be computed" in message
        assert "router refused" in message
        assert sqlstate == "58000"

    @pytest.mark.asyncio
    async def test_a_failed_kpi_list_fetch_is_caught_too(self, monkeypatch):
        """Both hops are required, so both are inside the guard: a failing KPI
        list must not escape as an unhandled transport error."""
        async def _boom(*_a, **_kw):
            raise RuntimeError("model-service unreachable")

        monkeypatch.setattr(router_client, "get_model_kpis", _boom)
        server = _server()

        _columns, _rows, err = await server._kpi_rows_for_persona(
            _WITHHELD, MODEL, PERSONA,
        )

        assert err is not None
        assert "model-service unreachable" in err[0]
        assert err[1] == "58000"

    @pytest.mark.asyncio
    async def test_a_kpi_the_persona_may_not_see_is_omitted_not_globalised(
        self, monkeypatch,
    ):
        """evaluate-batch omits a KPI this persona's visibility gate withholds.
        The seam must drop that row, never fall back to a global value."""
        async def _get_model_kpis(model_id, tenant_slug, jwt_token, project_id=""):
            return [
                {"id": KPI_ID, "name": "Revenue KPI"},
                {"id": "44444444-4444-4444-4444-444444444444", "name": "Cost KPI"},
            ]

        async def _evaluate(kpi_ids, model_id, project_id, tenant_slug,
                            jwt_token, *, filters=None, persona_id=None):
            return {KPI_ID: {"value": 250_000.0}}

        monkeypatch.setattr(router_client, "get_model_kpis", _get_model_kpis)
        monkeypatch.setattr(router_client, "evaluate_kpi_batch", _evaluate)
        server = _server()

        _columns, rows, err = await server._kpi_rows_for_persona(
            _WITHHELD, MODEL, PERSONA,
        )

        assert err is None
        assert [r["kpi_name"] for r in rows] == ["Revenue KPI"]

    @pytest.mark.asyncio
    async def test_a_model_with_no_deployed_kpi_is_an_honest_empty_scorecard(
        self, monkeypatch,
    ):
        _patch_live(monkeypatch, kpis=[])
        server = _server()

        _columns, rows, err = await server._kpi_rows_for_persona(
            _WITHHELD, MODEL, PERSONA,
        )

        assert err is None
        assert rows == []
