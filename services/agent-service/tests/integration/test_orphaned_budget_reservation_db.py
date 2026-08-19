"""DB-backed guard for orphaned pessimistic budget reservations.

Finding: intake ``2026-08-11-orphaned-budget-reservations-hold-budget-and-
inflate-the-cost-report.md``.

``reserve_budget`` (Bug-7366) commits an ~8096-token estimate row in its own
transaction and ``reconcile_budget_reservation`` deletes it. Every code exit
path reconciles; a process death between the two does not. The orphan then
(a) holds the project's daily allowance until UTC midnight, and (b) is reported
as spend by ``GET /cost`` for the entire lookback window, forever.

Test escape: every existing reservation test mocks the session
(``test_budget_guardrails.py`` patches ``reserve_budget`` /
``reconcile_budget_reservation`` outright, or compiles the statement without
running it), so nothing ever executed the sum against rows that survive a
crash. Guard: real PostgreSQL rows, the real ``check_budget`` sum and the real
``get_cost`` query. Tier: T1 db-integration.
"""
from __future__ import annotations

import os
import types
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db.models import AgentCostEntry, Project, TenantBase
from src.api import kpis as kpis_module
from src.guardrails import budget

pytestmark = pytest.mark.integration

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)

_MAX_AGE = budget._DEFAULT_RESERVATION_MAX_AGE_MINUTES


@asynccontextmanager
async def _isolated_schema() -> AsyncIterator[async_sessionmaker]:
    schema = f"agent_budget_orphan_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET search_path TO "{schema}"'))
        await connection.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


def _cfg(project_id: uuid.UUID, token_budget: int = 0, cost_budget: float = 0.0):
    return types.SimpleNamespace(
        project_id=project_id,
        daily_token_budget=token_budget,
        daily_cost_budget_usd=cost_budget,
    )


async def _seed_project(factory, project_id: uuid.UUID) -> None:
    async with factory() as db:
        db.add(
            Project(
                id=project_id,
                slug=f"p-{project_id.hex[:8]}",
                display_name="Budget project",
            )
        )
        await db.commit()


def _entry(project_id: uuid.UUID, provider, minutes_ago: int, tokens: int = 1000):
    return AgentCostEntry(
        id=uuid.uuid4(),
        project_id=project_id,
        turn_id=None,
        llm_config_id=None,
        provider=provider,
        input_tokens=tokens,
        output_tokens=tokens,
        estimated_cost_usd=1.0,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no integration DB URL configured")
async def test_orphaned_reservation_stops_holding_the_daily_budget():
    """A reservation older than the max age no longer counts, but a LIVE one
    still does — the pessimistic hold that Bug-7366 exists for is preserved."""
    async with _isolated_schema() as factory:
        project_id = uuid.uuid4()
        await _seed_project(factory, project_id)

        async with factory() as db:
            db.add(_entry(project_id, budget._RESERVATION_PROVIDER, _MAX_AGE + 5))
            await db.commit()

        async with factory() as db:
            tokens, _cost = await budget._today_usage(db, project_id)
        assert tokens == 0, "an orphaned reservation must not count as usage"

        async with factory() as db:
            db.add(_entry(project_id, budget._RESERVATION_PROVIDER, 1))
            await db.commit()

        async with factory() as db:
            tokens, _cost = await budget._today_usage(db, project_id)
        assert tokens == 2000, "a LIVE reservation must still hold budget"


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no integration DB URL configured")
async def test_orphaned_reservation_no_longer_refuses_the_next_turn():
    """The user-visible outcome: a crashed turn's reservation used to exhaust a
    modest daily_token_budget for the rest of the UTC day."""
    async with _isolated_schema() as factory:
        project_id = uuid.uuid4()
        await _seed_project(factory, project_id)

        async with factory() as db:
            # Three crashed turns, each holding the real 4000+4096 estimate.
            for _ in range(3):
                db.add(
                    _entry(
                        project_id,
                        budget._RESERVATION_PROVIDER,
                        _MAX_AGE + 5,
                        tokens=4048,
                    )
                )
            await db.commit()

        async with factory() as db:
            reason = await budget.check_budget(
                db, _cfg(project_id, token_budget=20000)
            )
        assert reason is None, (
            "orphaned reservations from crashed turns must not refuse the "
            f"next turn; got {reason!r}"
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no integration DB URL configured")
async def test_real_spend_with_a_null_provider_still_counts_against_the_budget():
    """NULL-safety. ``record_turn_cost`` normalises an empty provider string to
    NULL, and pre-migration rows carry NULL too. A naive ``provider !=
    sentinel`` predicate is NULL for those rows, so negating it drops them from
    the WHERE clause — real spend would vanish from the budget sum, which is a
    budget BYPASS, not a false refusal."""
    async with _isolated_schema() as factory:
        project_id = uuid.uuid4()
        await _seed_project(factory, project_id)

        async with factory() as db:
            db.add(_entry(project_id, None, _MAX_AGE + 5, tokens=5000))
            db.add(_entry(project_id, "anthropic", _MAX_AGE + 5, tokens=5000))
            await db.commit()

        async with factory() as db:
            tokens, _cost = await budget._today_usage(db, project_id)
        assert tokens == 20000, (
            "real spend must keep counting regardless of provider nullability"
        )

        async with factory() as db:
            reason = await budget.check_budget(
                db, _cfg(project_id, token_budget=15000)
            )
        assert reason == "daily_token_budget_exceeded"


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no integration DB URL configured")
async def test_reserve_budget_stamps_the_reservation_provider():
    """The identity the orphan predicate depends on is written by the real
    ``reserve_budget``, through the real session, not only asserted in the
    predicate."""
    async with _isolated_schema() as factory:
        project_id = uuid.uuid4()
        await _seed_project(factory, project_id)

        async def _fake_tenant_db(_tenant_id):
            async with factory() as db:
                yield db

        # ``reserve_budget`` imports get_tenant_db lazily inside the function
        # body, so the patch must land on the source module.
        with patch("shared.db.session.get_tenant_db", _fake_tenant_db):
            reservation_id = await budget.reserve_budget(
                "tenant", project_id, provider="unknown", max_output_tokens=4096
            )

        assert reservation_id is not None
        async with factory() as db:
            row = await db.get(AgentCostEntry, reservation_id)
            assert row is not None
            assert row.provider == budget._RESERVATION_PROVIDER


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no integration DB URL configured")
async def test_cost_report_never_reports_a_reservation_as_spend():
    """``GET /cost`` reads the ledger directly. A reservation is a budget
    placeholder, not money spent: a live one double-counts an in-flight turn,
    and an orphan is reported forever. Real NULL-provider spend must survive
    under the "unknown" bucket."""
    async with _isolated_schema() as factory:
        project_id = uuid.uuid4()
        await _seed_project(factory, project_id)

        async with factory() as db:
            db.add(_entry(project_id, budget._RESERVATION_PROVIDER, _MAX_AGE + 5))
            db.add(_entry(project_id, budget._RESERVATION_PROVIDER, 1))
            db.add(_entry(project_id, "anthropic", 10, tokens=700))
            db.add(_entry(project_id, None, 10, tokens=300))
            await db.commit()

        async def _fake_tenant_db(_tenant_id):
            async with factory() as db:
                yield db

        user = types.SimpleNamespace(tenant_id="tenant", user_id="u")
        with patch.object(kpis_module, "get_tenant_db", _fake_tenant_db), \
                patch.object(
                    kpis_module, "_require_project_viewer", AsyncMock()
                ):
            report = await kpis_module.get_cost(
                project_id=project_id, window_days=30, current_user=user
            )

        providers = {p.provider: p for p in report.per_provider}
        assert budget._RESERVATION_PROVIDER not in providers, (
            "a reservation row must never appear in the cost report"
        )
        assert set(providers) == {"anthropic", "unknown"}
        assert providers["anthropic"].input_tokens == 700
        assert providers["unknown"].input_tokens == 300
