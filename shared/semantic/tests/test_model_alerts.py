"""Integration tests for the model_alerts recorder.

Exercises the real helpers against a fresh sqlite-in-memory database
so the dedup and transition logic is covered without mocking the
ORM. We do NOT go through FastAPI or the tenant database — the
helpers are pure SQLAlchemy and bound to whatever session is passed
in, which keeps the tests hermetic.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

# These tests need aiosqlite to spin up an in-memory database. The
# scheduler production image does not ship it, so skip cleanly when
# it's unavailable — dev + CI install it via pytest dev extras.
pytest.importorskip("aiosqlite")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from shared.db.models import ModelAlert, TenantBase
from shared.semantic.model_alerts import (
    CATEGORY_INVALID_DIMENSION,
    CATEGORY_REFRESH_FAILURE,
    OBJECT_AGGREGATE,
    OBJECT_DIMENSION,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    count_open_alerts,
    dismiss_alert,
    list_open_alerts,
    record_alert,
    resolve_alert,
)


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        # Only create the model_alerts table, not the whole tenant
        # schema — other ORM relationships require Postgres-specific
        # types. We build the one table we care about by hand so the
        # dedup / lifecycle logic can be exercised end-to-end.
        await conn.execute(
            text(
                "CREATE TABLE model_alerts ("
                "id CHAR(32) PRIMARY KEY, "
                "model_id CHAR(32) NOT NULL, "
                "severity VARCHAR(16) NOT NULL, "
                "category VARCHAR(32) NOT NULL, "
                "title VARCHAR(255) NOT NULL, "
                "detail TEXT, "
                "detail_hash VARCHAR(64), "
                "related_object_type VARCHAR(32), "
                "related_object_id CHAR(32), "
                "first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "occurrence_count INTEGER NOT NULL DEFAULT 1, "
                "resolved_at TIMESTAMP, "
                "dismissed_at TIMESTAMP, "
                "dismissed_by CHAR(32))"
            )
        )
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _run(coro):
    return asyncio.run(coro)


async def _session():
    factory = await _session_factory()
    return factory()


def test_record_alert_creates_row_then_bumps_on_repeat():
    async def go():
        session = await _session()
        async with session as db:
            mid = uuid4()
            dim_id = uuid4()
            a1 = await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_WARNING,
                category=CATEGORY_INVALID_DIMENSION,
                title="dim broken",
                related_object_type=OBJECT_DIMENSION,
                related_object_id=dim_id,
            )
            a2 = await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_WARNING,
                category=CATEGORY_INVALID_DIMENSION,
                title="dim broken",
                related_object_type=OBJECT_DIMENSION,
                related_object_id=dim_id,
            )
            assert a1.id == a2.id
            assert a2.occurrence_count == 2
            count = await count_open_alerts(db, model_id=mid)
            assert count == 1

    _run(go())


def test_record_alert_escalates_severity():
    async def go():
        session = await _session()
        async with session as db:
            mid = uuid4()
            dim_id = uuid4()
            a1 = await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_INFO,
                category=CATEGORY_INVALID_DIMENSION,
                title="dim",
                related_object_type=OBJECT_DIMENSION,
                related_object_id=dim_id,
            )
            a2 = await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_ERROR,
                category=CATEGORY_INVALID_DIMENSION,
                title="dim escalated",
                related_object_type=OBJECT_DIMENSION,
                related_object_id=dim_id,
            )
            assert a2.severity == SEVERITY_ERROR

    _run(go())


def test_resolve_alert_closes_open_alert():
    async def go():
        session = await _session()
        async with session as db:
            mid = uuid4()
            dim_id = uuid4()
            await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_WARNING,
                category=CATEGORY_INVALID_DIMENSION,
                title="dim broken",
                related_object_type=OBJECT_DIMENSION,
                related_object_id=dim_id,
            )
            closed = await resolve_alert(
                db,
                model_id=mid,
                category=CATEGORY_INVALID_DIMENSION,
                related_object_type=OBJECT_DIMENSION,
                related_object_id=dim_id,
            )
            assert closed == 1
            # recording again after resolve should create a NEW row
            a2 = await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_WARNING,
                category=CATEGORY_INVALID_DIMENSION,
                title="dim broken again",
                related_object_type=OBJECT_DIMENSION,
                related_object_id=dim_id,
            )
            assert a2.occurrence_count == 1
            count = await count_open_alerts(db, model_id=mid)
            assert count == 1

    _run(go())


def test_list_open_alerts_filters_by_severity_and_category():
    async def go():
        session = await _session()
        async with session as db:
            mid = uuid4()
            await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_WARNING,
                category=CATEGORY_INVALID_DIMENSION,
                title="A",
                related_object_type=OBJECT_DIMENSION,
                related_object_id=uuid4(),
            )
            await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_ERROR,
                category=CATEGORY_REFRESH_FAILURE,
                title="B",
                related_object_type=OBJECT_AGGREGATE,
                related_object_id=uuid4(),
            )
            only_errors = await list_open_alerts(
                db, model_id=mid, severity=SEVERITY_ERROR
            )
            assert len(only_errors) == 1
            assert only_errors[0].title == "B"
            only_invalid_dims = await list_open_alerts(
                db, model_id=mid, category=CATEGORY_INVALID_DIMENSION
            )
            assert len(only_invalid_dims) == 1
            assert only_invalid_dims[0].title == "A"

    _run(go())


def test_dismiss_alert_removes_from_open_set():
    async def go():
        session = await _session()
        async with session as db:
            mid = uuid4()
            a = await record_alert(
                db,
                model_id=mid,
                severity=SEVERITY_WARNING,
                category=CATEGORY_INVALID_DIMENSION,
                title="dim",
                related_object_type=OBJECT_DIMENSION,
                related_object_id=uuid4(),
            )
            dismissed = await dismiss_alert(db, alert_id=a.id, dismissed_by=uuid4())
            assert dismissed is not None
            assert dismissed.dismissed_at is not None
            assert await count_open_alerts(db, model_id=mid) == 0

    _run(go())
