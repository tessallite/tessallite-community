"""Bug-9827 real-PostgreSQL regression tests for login lockout concurrency.

The first-failure path must count two simultaneous absent-row failures without
turning one request into an HTTP 500.  The reset path must also remain ordered
with a concurrent failure update.  Mocked sessions cannot prove either
property, so these tests use a disposable PostgreSQL database and the real
model-service login route/helper.

Run against a throwaway database only::

    TESSALLITE_LOCKOUT_TEST_DB_URL=postgresql+asyncpg://user:pw@localhost/db \
        pytest tests/integration/test_bug9827_login_lockout_concurrency_db.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import Select

from shared.auth import lockout as lockout_module
from shared.auth.lockout import record_login_failure, record_login_success
from shared.db.models import LoginLockout


@pytest.fixture(autouse=True)
def _lockout_enabled(monkeypatch):
    """Bug-10060: the account lock defaults to OFF (threshold 0) because it is
    keyed on the account and could be used to deny access to any known address.
    This module tests the lock MECHANISM - concurrency, counting and expiry - so
    it switches the lock on. The mechanism itself is unchanged; only its default.
    """
    monkeypatch.setattr(lockout_module, "_MAX_FAILURES", 5)

from src.main import app

pytestmark = [pytest.mark.integration]

_DB_URL = os.environ.get("TESSALLITE_LOCKOUT_TEST_DB_URL") or os.environ.get(
    "TESSALLITE_VERSIONING_DB_URL"
)


@asynccontextmanager
async def _lockout_database(scope: str):
    """Create the migrated lockout table and clean only this test's rows."""
    if not _DB_URL:
        pytest.skip("set TESSALLITE_LOCKOUT_TEST_DB_URL to a disposable PostgreSQL URL")

    engine = create_async_engine(_DB_URL, future=True)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE SCHEMA IF NOT EXISTS tess_system"))
        await connection.run_sync(LoginLockout.__table__.create, checkfirst=True)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                delete(LoginLockout).where(LoginLockout.scope_key == scope)
            )
            await cleanup.commit()
        await engine.dispose()


class _FailingAuthChain:
    def __init__(self) -> None:
        self.both_requests_reached_auth = asyncio.Barrier(2)

    async def authenticate(self, **_kwargs):
        await self.both_requests_reached_auth.wait()
        return None


class _ObservedSession:
    """Mark the first SQL call while preserving the real session boundary."""

    def __init__(self, session, execute_started: asyncio.Event) -> None:
        self._session = session
        self._execute_started = execute_started

    async def execute(self, *args, **kwargs):
        self._execute_started.set()
        return await self._session.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


class _LockoutSelectBarrierSession:
    """Synchronize every lockout read while retaining the real session."""

    def __init__(self, session, select_barrier: asyncio.Barrier) -> None:
        self._session = session
        self._select_barrier = select_barrier

    async def execute(self, statement, *args, **kwargs):
        result = await self._session.execute(statement, *args, **kwargs)
        sql = str(statement).lower()
        if isinstance(statement, Select) and "login_lockouts" in sql:
            await self._select_barrier.wait()
        return result

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no disposable lockout DB URL configured")
async def test_bug9827_concurrent_first_failures_return_401_and_count_exactly_two():
    """Bug-9827: two first failures are supported auth responses, not 500s.

    Test escape: the existing auth tests replace the system DB with an
    ``AsyncMock`` and never exercise two transactions racing to create the
    unique lockout row.  Guard: this test drives two real ASGI login requests
    through separate real PostgreSQL sessions and checks the persisted count.
    Tier: T3 (authentication correctness and PostgreSQL concurrency).
    """
    suffix = uuid.uuid4().hex
    scope = f"bug9827-{suffix}"
    email = f"first-failure-{suffix}@example.test"
    async with _lockout_database(scope) as factory:
        auth_chain = _FailingAuthChain()
        select_barrier = asyncio.Barrier(2)

        async def _system_db():
            async with factory() as session:
                yield _LockoutSelectBarrierSession(session, select_barrier)

        async def _tenant_db(_tenant_id):
            # The failure route only audits and commits this tenant session;
            # keep that unrelated transaction out of the database proof.
            tenant_db = AsyncMock()
            yield tenant_db

        async def _post_login():
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.post(
                    "/api/v1/auth/login",
                    json={
                        "tenant_id": scope,
                        "email": email,
                        "password": "wrong-password",
                    },
                )

        with (
            patch("src.api.auth.get_system_db", _system_db),
            patch("src.api.auth.get_tenant_db", _tenant_db),
            patch("src.api.auth.get_auth_chain", return_value=auth_chain),
            patch("src.api.auth.audit", new=AsyncMock()),
        ):
            first, second = await asyncio.gather(_post_login(), _post_login())

        assert [first.status_code, second.status_code] == [401, 401]
        assert first.json()["detail"] == "Invalid credentials"
        assert second.json()["detail"] == "Invalid credentials"

        async with factory() as verify:
            row = (
                await verify.execute(
                    select(LoginLockout).where(
                        LoginLockout.scope_key == scope,
                        LoginLockout.email_canonical == email,
                    )
                )
            ).scalar_one()
            assert row.failed_count == 2
            assert row.locked_until is None


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no disposable lockout DB URL configured")
async def test_bug9827_reset_serializes_before_failure_and_preserves_new_count():
    """Bug-9827: a failure waits for a successful reset/delete transaction.

    Test escape: mock-only reset tests can assert that ``record_login_success``
    was called but cannot observe its uncommitted row lock.  Guard: this test
    flushes a real delete, verifies the real failure statement remains blocked,
    commits the reset, and then checks the post-reset failure row.
    Tier: T3 (authentication state serialization).
    """
    suffix = uuid.uuid4().hex
    scope = f"bug9827-{suffix}"
    email = f"reset-race-{suffix}@example.test"
    async with _lockout_database(scope) as factory:
        async with factory() as seed:
            seed.add(
                LoginLockout(
                    id=uuid.uuid4(),
                    scope_key=scope,
                    email_canonical=email,
                    failed_count=1,
                    locked_until=None,
                )
            )
            await seed.commit()

        async with factory() as reset_db, factory() as failure_db:
            await record_login_success(reset_db, scope, email)

            execute_started = asyncio.Event()
            failure_task = asyncio.create_task(
                record_login_failure(
                    _ObservedSession(failure_db, execute_started), scope, email
                )
            )
            await asyncio.wait_for(execute_started.wait(), timeout=2)

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(failure_task), timeout=2)

            await reset_db.commit()
            await asyncio.wait_for(failure_task, timeout=5)

        async with factory() as verify:
            row = (
                await verify.execute(
                    select(LoginLockout).where(
                        LoginLockout.scope_key == scope,
                        LoginLockout.email_canonical == email,
                    )
                )
            ).scalar_one()
            assert row.failed_count == 1
            assert row.locked_until is None


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no disposable lockout DB URL configured")
async def test_bug9827_failure_upsert_preserves_threshold_and_expiry_semantics():
    """The atomic path keeps the existing threshold and expired-row behavior.

    Test escape: the old implementation's Python branches were not covered by
    a real database contract, so a SQL expression could change the reset or
    threshold boundary while fixing the race.  Guard: real PostgreSQL asserts
    the fifth failure locks for 15 minutes and an expired lock restarts at one.
    Tier: T3 (authentication throttle correctness).
    """
    suffix = uuid.uuid4().hex
    scope = f"bug9827-{suffix}"
    email = f"semantics-{suffix}@example.test"
    async with _lockout_database(scope) as factory:
        async with factory() as seed:
            seed.add(
                LoginLockout(
                    id=uuid.uuid4(),
                    scope_key=scope,
                    email_canonical=email,
                    failed_count=4,
                    locked_until=None,
                )
            )
            await seed.commit()

        async with factory() as failure_db:
            await record_login_failure(failure_db, scope, email)

        async with factory() as verify:
            row = (
                await verify.execute(
                    select(LoginLockout).where(
                        LoginLockout.scope_key == scope,
                        LoginLockout.email_canonical == email,
                    )
                )
            ).scalar_one()
            assert row.failed_count == 5
            assert row.locked_until is not None
            remaining = row.locked_until - datetime.now(timezone.utc)
            assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)

            row.failed_count = 5
            row.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            await verify.commit()

        async with factory() as failure_db:
            await record_login_failure(failure_db, scope, email)

        async with factory() as verify:
            row = (
                await verify.execute(
                    select(LoginLockout).where(
                        LoginLockout.scope_key == scope,
                        LoginLockout.email_canonical == email,
                    )
                )
            ).scalar_one()
            assert row.failed_count == 1
            assert row.locked_until is None
