"""Bug-8070 — KPI evaluations must declare their origin, not blend into JDBC.

Observability-correctness guard. ``_execute_via_router`` posted only
``protocol="jdbc"``, so every KPI evaluation — scheduled sweeps and interactive
scorecard opens alike — was recorded in QueryLog as generic BI-client traffic.
An operator separating workloads (top users, latency, route mix) therefore read
KPI cost as BI cost and got the wrong operational story. Every other internal
client already declares itself: drill, headless, plugin, agent.

The bridge is a single chokepoint for all six KPI evaluation call sites, so the
contract is asserted there, plus the producer/consumer ends: the query-router
request model must ACCEPT the value and the model-service log filter must allow
FILTERING by it. All three now derive from one canonical domain.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_kpi_bridge_declares_client_kind_kpi():
    """The body posted to /execute must carry client_kind="kpi"."""
    from src.api.kpis import _execute_via_router

    captured: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"rows": [{"v": 1}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured.update(json or {})
            return _Resp()

    with patch("httpx.AsyncClient", return_value=_Client()):
        await _execute_via_router(uuid4(), "SELECT 1", "token")

    assert captured.get("client_kind") == "kpi"
    # protocol must stay "jdbc": the SQL still needs strict-parser treatment.
    # client_kind is the attribution axis, not a parser switch.
    assert captured.get("protocol") == "jdbc"


def test_query_router_request_model_accepts_kpi_origin():
    """Producer/consumer: the router must accept what the bridge now sends.
    A value the bridge declares but the request model rejects is a 422, i.e.
    every KPI evaluation failing outright."""
    from shared.query_log_client_kinds import (
        REQUEST_DECLARABLE_CLIENT_KINDS,
        QUERY_LOG_CLIENT_KINDS,
    )

    assert "kpi" in REQUEST_DECLARABLE_CLIENT_KINDS
    assert "kpi" in QUERY_LOG_CLIENT_KINDS


def test_query_log_filter_domain_covers_every_written_client_kind():
    """Bug-7451's recurrence guard: a value that writers produce but the log
    filter rejects gives a 422 when an operator filters by it. The filter now
    derives from the canonical domain, so this asserts the derivation holds
    rather than restating the list a third time."""
    import typing

    from shared.query_log_client_kinds import QUERY_LOG_CLIENT_KINDS
    from src.api.logs import _CLIENT_KIND_LITERAL

    assert set(typing.get_args(_CLIENT_KIND_LITERAL)) == set(QUERY_LOG_CLIENT_KINDS)


def test_request_declarable_kinds_are_a_subset_of_the_log_domain():
    """Anything a caller may declare must be a value the log filter accepts,
    otherwise a request succeeds and its rows become unfilterable."""
    from shared.query_log_client_kinds import (
        QUERY_LOG_CLIENT_KINDS,
        REQUEST_DECLARABLE_CLIENT_KINDS,
    )

    assert set(REQUEST_DECLARABLE_CLIENT_KINDS) <= set(QUERY_LOG_CLIENT_KINDS)
