"""Bug-7982 completion round (test gap): exercise the REAL ``$KPIs`` serve
handler against a real Postgres row, not a hand-copied predicate.

``test_versioning_consistency_db.py`` (model-service) and
``test_kpi_table_query.py`` (query-router) both cover the residual-2
``evaluated_for_epoch == Model.deploy_epoch`` serve gate, but neither goes
through the ACTUAL production code path end to end against a real database:

- The model-service test hand-copies the predicate into a local ``_serve``
  helper — a future refactor of the real predicate in ``routes.py`` could
  silently drift from what that test asserts and it would never notice.
- The query-router unit tests (``TestKpiTableQueryGateAndObserve`` etc.) call
  the real ``_handle_kpi_table_query`` handler, but with ``db.execute`` fully
  mocked (``_StatefulDB``) — the WHERE clause text is never actually executed
  against a database, so a typo or logic error in the predicate itself would
  not be caught.

This test calls the REAL ``_handle_kpi_table_query`` handler with a REAL
``AsyncSession`` against an isolated Postgres schema, so the exact SQL
predicate that ships in ``routes.py`` is what runs.

Requires ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL) to
point at a reachable Postgres — skipped otherwise, same convention as the
model-service versioning-consistency live-DB suite.

Run:
    cd tessallite/services/query-router
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_kpi_epoch_serve_live.py -v
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.ir.logical_query import LogicalQuery

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)


def _mk_engine(schema: str):
    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cur = dbapi_connection.cursor()
        cur.execute(f'SET search_path TO "{schema}"')
        cur.close()

    return engine


@asynccontextmanager
async def _isolated_schema() -> AsyncIterator[tuple[async_sessionmaker, str]]:
    """Isolated Postgres schema seeded with the tenant metadata tables.

    Mirrors ``tests/integration/test_versioning_consistency_db.py`` in
    model-service (same DB, disjoint schema per test run) so this test can
    share the developer's already-running Postgres without colliding with
    other suites.
    """
    from shared.db.models import TenantBase

    schema = f"kpi_epoch_serve_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = _mk_engine(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, schema
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await drop.dispose()
        await engine.dispose()


def _make_logical_query(model_id: str) -> LogicalQuery:
    return LogicalQuery(
        model_id=model_id,
        protocol="jdbc",
        raw_query='SELECT * FROM "modelx$KPIs"',
        requested_measures=[],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="kpi_epoch_live_fp",
        from_tables=["modelx$KPIs"],
    )


def _patch_observation(monkeypatch):
    """Stub the leaf writers/metrics so record_query_success runs without
    needing the full QueryLog/audit/Prometheus wiring — mirrors
    ``test_kpi_table_query.py``'s ``_patch_observation``. The DB READ predicate
    under test (the actual point of this file) still runs for real."""
    from src.api import routes as routes_mod

    async def fake_log_query(**kwargs):
        return None

    async def fake_log_query_miss(*a, **kw):
        return None

    async def fake_audit(*a, **kw):
        return None

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


async def _seed_model_kpi_and_latest(
    s: AsyncSession, *, deploy_epoch: int, latest_epoch: int,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a deployed Model + deployed KPI + one KPILatest row.

    Returns (model_id, kpi_id).
    """
    from shared.db.models import KPI, KPILatest, Model, Project

    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    kpi_id = uuid.uuid4()
    deployed_v = uuid.uuid4()

    s.add(Project(id=project_id, slug=f"p-{project_id.hex[:8]}", display_name="P"))
    s.add(
        Model(
            id=model_id,
            project_id=project_id,
            slug=f"m-{model_id.hex[:8]}",
            display_name="ModelX",
            seed=uuid.uuid4().hex,
            deployed_version_id=deployed_v,
            deploy_epoch=deploy_epoch,
        )
    )
    s.add(KPI(id=kpi_id, model_id=model_id, name="Revenue", is_deployed=True))
    await s.flush()
    s.add(
        KPILatest(
            id=uuid.uuid4(), model_id=model_id, kpi_id=kpi_id,
            kpi_name="Revenue", value=100.0,
            evaluated_for_version_id=deployed_v, evaluated_for_epoch=latest_epoch,
        )
    )
    await s.commit()
    return model_id, kpi_id


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_real_handler_serves_matching_epoch(monkeypatch):
    """The real ``_handle_kpi_table_query`` handler, run against a real
    Postgres row whose ``evaluated_for_epoch`` matches the model's current
    ``deploy_epoch``, serves the KPI."""
    from src.api.routes import _handle_kpi_table_query

    _patch_observation(monkeypatch)

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id, kpi_id = await _seed_model_kpi_and_latest(
                s, deploy_epoch=5, latest_epoch=5,
            )

        async with factory() as s:
            resp = await _handle_kpi_table_query(
                s, str(model_id), _make_logical_query(str(model_id)),
                persona=None, principal=None,
                user_identity="u@t.com", tenant_id="acme",
            )

        names = {r["kpi_name"] for r in resp.rows}
        assert names == {"Revenue"}, (
            "a kpi_latest row whose evaluated_for_epoch matches the model's "
            "current deploy_epoch must be served by the real handler"
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
async def test_real_handler_withholds_stale_epoch_after_revert(monkeypatch):
    """Bug-7982 residual 2, exercised through the REAL production handler
    (not a hand-copied predicate): a definition-changing revert bumps
    ``deploy_epoch``; the real ``_handle_kpi_table_query`` handler must
    withhold a ``kpi_latest`` row still stamped with the OLD epoch.

    Reverting the ``evaluated_for_epoch == Model.deploy_epoch`` predicate in
    ``routes.py`` (e.g. dropping the epoch join clause, or comparing against
    the wrong column) makes this test FAIL — the stale value would be served
    through the exact code path a BI client hits.
    """
    from src.api.routes import _handle_kpi_table_query
    from shared.db.models import Model

    _patch_observation(monkeypatch)

    async with _isolated_schema() as (factory, _schema):
        async with factory() as s:
            model_id, kpi_id = await _seed_model_kpi_and_latest(
                s, deploy_epoch=5, latest_epoch=5,
            )

        # Sanity: served before the revert (proves the withhold below is the
        # epoch gate reacting to the revert, not a seeding mistake).
        async with factory() as s:
            resp = await _handle_kpi_table_query(
                s, str(model_id), _make_logical_query(str(model_id)),
                persona=None, principal=None,
                user_identity="u@t.com", tenant_id="acme",
            )
            assert {r["kpi_name"] for r in resp.rows} == {"Revenue"}

        # A definition-changing revert bumps deploy_epoch; the kpi_latest row
        # still carries the OLD epoch (5).
        async with factory() as s:
            model = await s.get(Model, model_id)
            model.deploy_epoch = 6
            await s.commit()

        # The real handler must now withhold the stale row.
        async with factory() as s:
            resp = await _handle_kpi_table_query(
                s, str(model_id), _make_logical_query(str(model_id)),
                persona=None, principal=None,
                user_identity="u@t.com", tenant_id="acme",
            )
            names = {r["kpi_name"] for r in resp.rows}
            assert "Revenue" not in names, (
                "the real $KPIs handler served a kpi_latest row stamped with a "
                "STALE epoch after a revert bumped deploy_epoch — wrong number"
            )
            assert resp.rows == []
