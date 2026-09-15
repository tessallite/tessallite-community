"""Focused guards for the shared tenant serving-readiness boundary."""
from __future__ import annotations

import asyncio
import gc
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import create_async_engine as sqlalchemy_create_async_engine

import shared.db.session as db_session
import shared.db.tenant_readiness as readiness
from shared.auth.backend import AuthChain
from shared.db.tenant_readiness import TenantReadinessError


_VERSIONING_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL", "")


class _Result:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Connection:
    def __init__(self, values):
        self.values = values
        self.statements: list[str] = []

    def begin(self):
        return _Transaction()

    async def execute(self, statement, _params=None):
        self.statements.append(str(statement))
        if "version_num" in str(statement):
            return _Result(self.values)
        return _Result([])


class _Engine:
    def __init__(self, values):
        self.connection = _Connection(values)

    def connect(self):
        engine = self

        class _ConnectionContext:
            async def __aenter__(self):
                return engine.connection

            async def __aexit__(self, *_exc):
                return False

        return _ConnectionContext()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "cause"),
    [
        ([], "tenant schema revision is missing"),
        (["0222"], "tenant schema revision is older than the service"),
        (["future-revision"], "tenant schema revision is unknown to the service"),
        (["0222", "0223"], "tenant schema revision is unreadable"),
    ],
)
async def test_schema_revision_states_refuse_with_typed_detail(
    monkeypatch, values, cause
):
    class _Scripts:
        def get_revision(self, revision):
            if revision == "0223":
                return SimpleNamespace(revision="0223")
            if revision == "0222":
                return SimpleNamespace(revision="0222")
            return None

    monkeypatch.setattr(readiness, "migration_scripts", lambda: _Scripts())
    monkeypatch.setattr(readiness, "required_tenant_revision", lambda: "0223")
    engine = _Engine(values)

    with pytest.raises(TenantReadinessError) as raised:
        await readiness.ensure_tenant_schema_ready(
            engine,
            tenant_slug="beta",
            quoted_schema='"beta_meta"',
            operation="tenant database session acquisition",
        )

    detail = raised.value.detail
    assert detail["condition"] == "tenant_database_unavailable"
    assert detail["tenant_slug"] == "beta"
    assert detail["operation"] == "tenant database session acquisition"
    assert detail["cause"] == cause
    assert detail["required_revision"] == "0223"
    if values == ["0222"]:
        assert detail["current_revision"] == "0222"
    assert any("pg_advisory_xact_lock" in statement for statement in engine.connection.statements)


@pytest.mark.asyncio
async def test_missing_version_table_refuses_as_missing_revision(monkeypatch):
    monkeypatch.setattr(readiness, "required_tenant_revision", lambda: "0223")

    class _UndefinedTable:
        sqlstate = "42P01"

    class _MissingTableEngine:
        def connect(self):
            raise sa_exc.ProgrammingError(
                'relation "beta_meta.alembic_version" does not exist',
                {},
                _UndefinedTable(),
            )

    with pytest.raises(TenantReadinessError) as raised:
        await readiness.ensure_tenant_schema_ready(
            _MissingTableEngine(),
            tenant_slug="beta",
            quoted_schema='"beta_meta"',
            operation="tenant database session acquisition",
        )

    detail = raised.value.detail
    assert detail["cause"] == "tenant schema revision is missing"
    assert detail["required_revision"] == "0223"


def test_required_revision_comes_from_the_shipped_graph():
    scripts = readiness.migration_scripts()
    assert readiness.required_tenant_revision() == scripts.get_revision("tenant@head").revision


def test_readiness_error_redacts_dsn_from_operator_log(caplog):
    error = readiness.make_tenant_readiness_error(
        tenant_slug="beta",
        operation="tenant database session acquisition",
        cause="tenant schema state could not be read",
        original=RuntimeError(
            "could not connect to postgresql+asyncpg://dbuser:secret@db.internal:5432/tessallite"
        ),
    )
    assert error.status_code == 503
    assert "dbuser" not in caplog.text
    assert "secret" not in caplog.text
    assert "<redacted-dsn>" in caplog.text


@pytest.mark.asyncio
async def test_cached_request_and_snapshot_factories_recheck_authority(monkeypatch):
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()
    resolved: list[str] = []
    checked: list[tuple[str, str]] = []

    async def resolve(tenant_id):
        resolved.append(tenant_id)
        return "postgresql+asyncpg://u:p@localhost/db", '"alpha_meta"'

    class _FakeEngine:
        async def dispose(self):
            return None

    def create(*_args, **_kwargs):
        return _FakeEngine()

    def factory(*_args, **kwargs):
        return SimpleNamespace(kw=kwargs)

    async def check(engine, *, tenant_slug, quoted_schema, operation):
        checked.append((tenant_slug, operation))

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    monkeypatch.setattr(db_session, "create_async_engine", create)
    monkeypatch.setattr(db_session, "async_sessionmaker", factory)
    monkeypatch.setattr(db_session, "ensure_tenant_schema_ready", check)
    monkeypatch.setattr(db_session, "_tenant_engine_cache_max", lambda: 8)

    first = await db_session.get_tenant_session_factory("alpha")
    second = await db_session.get_tenant_session_factory("alpha")
    snap_first = await db_session.get_tenant_snapshot_session_factory("alpha")
    snap_second = await db_session.get_tenant_snapshot_session_factory("alpha")

    assert second is first
    assert snap_second is snap_first
    assert resolved == ["alpha", "alpha", "alpha", "alpha"]
    assert checked == [
        ("alpha", "tenant database session acquisition"),
        ("alpha", "tenant database session acquisition"),
        ("alpha", "tenant snapshot session acquisition"),
        ("alpha", "tenant snapshot session acquisition"),
    ]
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()


@pytest.mark.asyncio
async def test_slow_tenant_readiness_does_not_block_another_tenant(monkeypatch):
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()
    alpha_started = asyncio.Event()
    release_alpha = asyncio.Event()

    async def resolve(tenant_id):
        return "postgresql+asyncpg://u:p@localhost/db", f'"{tenant_id}_meta"'

    class _FakeEngine:
        async def dispose(self):
            return None

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    monkeypatch.setattr(db_session, "create_async_engine", lambda *_a, **_k: _FakeEngine())
    monkeypatch.setattr(
        db_session,
        "async_sessionmaker",
        lambda *_a, **kwargs: SimpleNamespace(kw=kwargs),
    )

    async def check(engine, *, tenant_slug, quoted_schema, operation):
        if tenant_slug == "alpha":
            alpha_started.set()
            await release_alpha.wait()

    monkeypatch.setattr(db_session, "ensure_tenant_schema_ready", check)

    alpha_task = asyncio.create_task(db_session.get_tenant_session_factory("alpha"))
    await asyncio.wait_for(alpha_started.wait(), timeout=1)
    beta_factory = await asyncio.wait_for(
        db_session.get_tenant_session_factory("beta"),
        timeout=1,
    )
    assert beta_factory is not None
    assert not db_session._tenant_engines_lock.locked()

    release_alpha.set()
    assert await alpha_task is not None
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()


@pytest.mark.asyncio
async def test_unknown_slug_churn_does_not_retain_creation_locks(monkeypatch):
    """Unknown login slugs must not grow a process-lifetime lock registry."""
    db_session._tenant_creation_locks.clear()

    async def resolve(_tenant_id):
        raise ValueError("tenant not found")

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    for index in range(200):
        with pytest.raises(ValueError, match="tenant not found"):
            await db_session.get_tenant_session_factory(f"unknown-{index}")

    gc.collect()
    assert len(db_session._tenant_creation_locks) == 0


@pytest.mark.asyncio
async def test_cancelled_unknown_slug_lookup_never_takes_creation_lock(monkeypatch):
    """Authority lookup I/O must happen before the per-tenant creation lock."""
    db_session._tenant_creation_locks.clear()
    started = asyncio.Event()
    release = asyncio.Event()

    async def resolve(_tenant_id):
        started.set()
        await release.wait()
        raise ValueError("tenant not found")

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    task = asyncio.create_task(
        db_session.get_tenant_session_factory("cancelled-unknown")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert "cancelled-unknown" not in db_session._tenant_creation_locks

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    del task
    gc.collect()
    assert "cancelled-unknown" not in db_session._tenant_creation_locks


@pytest.mark.asyncio
async def test_concurrent_cold_lookup_reuses_one_request_engine(monkeypatch):
    """M-04: publish once, then run readiness without holding creation."""
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()
    db_session._tenant_creation_locks.clear()
    readiness_started = asyncio.Event()
    release_readiness = asyncio.Event()
    engines: list[object] = []

    async def resolve(tenant_id):
        return "postgresql+asyncpg://u:p@localhost/db", f'"{tenant_id}_meta"'

    class _FakeEngine:
        async def dispose(self):
            return None

    def create(*_args, **_kwargs):
        engine = _FakeEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    monkeypatch.setattr(db_session, "create_async_engine", create)
    monkeypatch.setattr(
        db_session,
        "async_sessionmaker",
        lambda *_a, **kwargs: SimpleNamespace(kw=kwargs),
    )

    async def check(*_args, **_kwargs):
        readiness_started.set()
        await release_readiness.wait()

    monkeypatch.setattr(db_session, "ensure_tenant_schema_ready", check)

    first_task = asyncio.create_task(db_session.get_tenant_session_factory("alpha"))
    await asyncio.wait_for(readiness_started.wait(), timeout=1)
    second_task = asyncio.create_task(
        db_session.get_tenant_session_factory("alpha")
    )
    await asyncio.sleep(0)
    assert len(engines) == 1
    assert len(db_session._tenant_creation_locks) == 0

    release_readiness.set()
    first, second = await asyncio.gather(first_task, second_task)
    assert first is second
    assert len(engines) == 1
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()
    db_session._tenant_creation_locks.clear()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _VERSIONING_DB_URL,
    reason="TESSALLITE_VERSIONING_DB_URL required for the bounded-pool proof",
)
async def test_cached_request_pool_wait_does_not_block_snapshot_factory(
    monkeypatch,
):
    """A Save snapshot must bypass readiness waiting on a full request pool.

    The two checked-out connections model concurrent Saves holding the bounded
    request pool. The third request's readiness check waits for that pool while
    snapshot acquisition uses its separately cached NullPool path.
    """
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()
    db_session._tenant_creation_locks.clear()
    request_waiting = asyncio.Event()
    snapshot_checked = asyncio.Event()
    db_url = _VERSIONING_DB_URL
    request_engine = sqlalchemy_create_async_engine(
        db_url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1,
    )

    class _SnapshotEngine:
        async def dispose(self):
            return None

    request_factory = SimpleNamespace(
        kw={"info": {"tenant_schema": '"alpha_meta"'}}
    )
    snapshot_factory = SimpleNamespace(
        kw={"info": {"tenant_schema": '"alpha_meta"'}}
    )
    snapshot_engine = _SnapshotEngine()
    db_session._tenant_engines["alpha"] = (request_engine, request_factory)
    db_session._tenant_snapshot_engines["alpha"] = (
        snapshot_engine,
        snapshot_factory,
    )

    async def resolve(_tenant_id):
        return db_url, '"alpha_meta"'

    async def check(engine, **_kwargs):
        if engine is snapshot_engine:
            snapshot_checked.set()
            return
        request_waiting.set()
        async with engine.connect():
            return

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    monkeypatch.setattr(db_session, "ensure_tenant_schema_ready", check)

    first = await request_engine.connect()
    second = await request_engine.connect()
    request_task = asyncio.create_task(
        db_session.get_tenant_session_factory("alpha")
    )
    try:
        await asyncio.wait_for(request_waiting.wait(), timeout=1)
        result = await asyncio.wait_for(
            db_session.get_tenant_snapshot_session_factory("alpha"),
            timeout=0.25,
        )
        assert result is snapshot_factory
        assert snapshot_checked.is_set()
        assert not db_session._tenant_creation_locks

        await first.close()
        assert await asyncio.wait_for(request_task, timeout=1) is request_factory
    finally:
        if not request_task.done():
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
        await second.close()
        await request_engine.dispose()
        db_session._tenant_engines.clear()
        db_session._tenant_snapshot_engines.clear()
        db_session._tenant_creation_locks.clear()


@pytest.mark.asyncio
async def test_schema_change_rebuilds_cached_request_engine(monkeypatch):
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()
    current_schema = '"alpha_meta"'
    engines: list[object] = []
    disposed: list[object] = []

    async def resolve(_tenant_id):
        return "postgresql+asyncpg://u:p@localhost/db", current_schema

    class _FakeEngine:
        async def dispose(self):
            disposed.append(self)

    def create(*_args, **_kwargs):
        engine = _FakeEngine()
        engines.append(engine)
        return engine

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    monkeypatch.setattr(db_session, "create_async_engine", create)
    monkeypatch.setattr(
        db_session,
        "async_sessionmaker",
        lambda *_a, **kwargs: SimpleNamespace(kw=kwargs),
    )
    async def check(*_args, **_kwargs):
        return None

    monkeypatch.setattr(db_session, "ensure_tenant_schema_ready", check)

    first = await db_session.get_tenant_session_factory("alpha")
    current_schema = '"beta_meta"'
    second = await db_session.get_tenant_session_factory("alpha")

    assert second is not first
    assert len(engines) == 2
    assert disposed == [engines[0]]
    assert second.kw["info"]["tenant_schema"] == '"beta_meta"'
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()


@pytest.mark.asyncio
async def test_retained_factory_refuses_after_schema_authority_changes(monkeypatch):
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()
    current_schema = '"alpha_meta"'
    checks: list[str] = []
    db_url = db_session.settings.SYSTEM_DATABASE_URL
    engine = sqlalchemy_create_async_engine(db_url)

    async def resolve(_tenant_id):
        return db_url, current_schema

    async def check(_engine, *, quoted_schema, **_kwargs):
        checks.append(quoted_schema)

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    monkeypatch.setattr(db_session, "create_async_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(db_session, "ensure_tenant_schema_ready", check)

    factory = await db_session.get_tenant_session_factory("alpha")
    current_schema = '"beta_meta"'
    session = factory()
    try:
        with pytest.raises(TenantReadinessError) as raised:
            await session.__aenter__()
    finally:
        await session.close()
        await db_session.evict_tenant_engine("alpha")

    detail = raised.value.detail
    assert raised.value.status_code == 503
    assert detail["tenant_slug"] == "alpha"
    assert detail["cause"] == (
        "tenant database authority changed; reacquire the tenant session"
    )
    assert checks == ['"alpha_meta"']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("getter_name", "operation"),
    [
        ("get_tenant_session_factory", "tenant database session acquisition"),
        (
            "get_tenant_snapshot_session_factory",
            "tenant snapshot session acquisition",
        ),
    ],
)
async def test_engine_construction_failure_is_typed_readiness_refusal(
    monkeypatch, getter_name, operation
):
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()

    async def resolve(_tenant_id):
        return "malformed-tenant-database-url", '"beta_meta"'

    def create(*_args, **_kwargs):
        raise ValueError("malformed database URL contains secret material")

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", resolve)
    monkeypatch.setattr(db_session, "create_async_engine", create)

    with pytest.raises(TenantReadinessError) as raised:
        await getattr(db_session, getter_name)("beta")

    detail = raised.value.detail
    assert raised.value.status_code == 503
    assert detail["condition"] == "tenant_database_unavailable"
    assert detail["tenant_slug"] == "beta"
    assert detail["operation"] == operation
    assert detail["cause"] == "tenant database connection could not be opened"


@pytest.mark.asyncio
async def test_auth_chain_preserves_tenant_readiness_503():
    class _BrokenBackend:
        name = "local"

        async def authenticate(self, **_kwargs):
            raise TenantReadinessError(
                tenant_slug="beta",
                operation="local login",
                cause="tenant schema revision is older than the service",
                current_revision="0222",
                required_revision="0223",
            )

    with pytest.raises(TenantReadinessError) as raised:
        await AuthChain([_BrokenBackend()]).authenticate(
            tenant_id="beta", email="user@example.test", password="secret"
        )
    assert raised.value.detail["condition"] == "tenant_database_unavailable"
