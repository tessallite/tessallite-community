"""Bug-9192/Bug-9608 — retained engines have bounded pools.

RFGPT-002: the per-engine pool_size bound is necessary but not sufficient —
the process must also bound how many tenant engines it retains.
"""
from __future__ import annotations

import ast
import inspect
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

import shared.db.session as db_session
from shared.config.settings import Settings


def test_bug9192_all_session_engines_are_bounded_or_null_pool() -> None:
    tree = ast.parse(inspect.getsource(db_session))
    factory_names = {"create_async_engine"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy.ext.asyncio":
            factory_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "create_async_engine"
            )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in factory_names)
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_async_engine"
            )
        )
    ]
    assert len(calls) == 3

    pooled_calls = 0
    for call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        poolclass = keywords.get("poolclass")
        if isinstance(poolclass, ast.Name) and poolclass.id == "NullPool":
            continue

        pooled_calls += 1
        pool_size = keywords["pool_size"]
        if isinstance(pool_size, ast.Attribute):
            assert isinstance(pool_size.value, ast.Name)
            assert pool_size.value.id == "settings"
            assert pool_size.attr in {
                "SYSTEM_DB_POOL_SIZE",
                "TENANT_DB_POOL_SIZE",
            }
        else:
            assert ast.literal_eval(pool_size) == 2
        assert ast.literal_eval(keywords["max_overflow"]) == 0

    assert pooled_calls == 2
    assert db_session._system_engine.pool.size() == db_session.settings.SYSTEM_DB_POOL_SIZE


def test_bug9613_retained_tenant_connection_product_is_bounded() -> None:
    selected = Settings(
        TENANT_DB_POOL_SIZE=4,
        TENANT_ENGINE_CACHE_MAX=8,
    )
    assert selected.TENANT_DB_POOL_SIZE * selected.TENANT_ENGINE_CACHE_MAX == 32

    with pytest.raises(
        ValidationError,
        match=r"TENANT_DB_POOL_SIZE \* TENANT_ENGINE_CACHE_MAX must be <= 32",
    ):
        Settings(
            TENANT_DB_POOL_SIZE=4,
            TENANT_ENGINE_CACHE_MAX=9,
        )


def test_bug9646_optimizer_values_reuse_the_shared_d20_resource_contract() -> None:
    selected = Settings(
        TENANT_DB_POOL_SIZE=4,
        TENANT_ENGINE_CACHE_MAX=8,
    )
    assert selected.TENANT_DB_POOL_SIZE == 4
    assert selected.TENANT_ENGINE_CACHE_MAX == 8
    assert selected.TENANT_DB_POOL_SIZE * selected.TENANT_ENGINE_CACHE_MAX == 32


@pytest.mark.asyncio
async def test_bug9192_tenant_engine_cache_is_lru_bounded_and_disposes(monkeypatch):
    """More tenant identities than the cache limit must dispose LRU engines."""
    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()
    monkeypatch.setattr(db_session, "_tenant_engine_cache_max", lambda: 2)

    disposed: list[object] = []

    class _FakeEngine:
        async def dispose(self) -> None:
            disposed.append(self)

    async def _resolve(tenant_id: str):
        return "postgresql+asyncpg://u:p@localhost/db", f'"{tenant_id}_meta"'

    def _create_engine(*_a, **_k):
        assert _k["pool_size"] == db_session.settings.TENANT_DB_POOL_SIZE
        assert _k["max_overflow"] == 0
        return _FakeEngine()

    monkeypatch.setattr(db_session, "_resolve_tenant_dsn", _resolve)
    monkeypatch.setattr(db_session, "create_async_engine", _create_engine)
    monkeypatch.setattr(
        db_session, "ensure_tenant_schema_ready", AsyncMock()
    )
    monkeypatch.setattr(
        db_session, "async_sessionmaker", lambda *_a, **_k: object()
    )

    await db_session.get_tenant_session_factory("t1")
    await db_session.get_tenant_session_factory("t2")
    assert set(db_session._tenant_engines) == {"t1", "t2"}

    # Touch t1 so t2 becomes the LRU victim when t3 arrives.
    await db_session.get_tenant_session_factory("t1")
    await db_session.get_tenant_session_factory("t3")

    assert set(db_session._tenant_engines) == {"t1", "t3"}
    assert "t2" not in db_session._tenant_engines
    assert len(disposed) == 1

    # Re-creating t2 should dispose another LRU entry (t1) and keep the bound.
    await db_session.get_tenant_session_factory("t2")
    assert len(db_session._tenant_engines) == 2
    assert "t2" in db_session._tenant_engines
    assert len(disposed) == 2

    db_session._tenant_engines.clear()
    db_session._tenant_snapshot_engines.clear()


@pytest.mark.parametrize("size", [0, 1, 4, 64])
def test_system_pool_accepts_unlimited_or_explicit_positive_size(size):
    assert Settings(SYSTEM_DB_POOL_SIZE=size).SYSTEM_DB_POOL_SIZE == size


def test_system_pool_rejects_negative_size():
    with pytest.raises(ValidationError):
        Settings(SYSTEM_DB_POOL_SIZE=-1)


@pytest.mark.parametrize("size,count", [(0, 40), (4, 4)])
def test_system_pool_checkout_respects_configured_limit(size, count):
    """Exercise SQLAlchemy pool semantics with real connections held together."""
    import sqlite3
    from sqlalchemy.pool import QueuePool
    from sqlalchemy.exc import TimeoutError

    pool = QueuePool(
        lambda: sqlite3.connect(":memory:"),
        pool_size=Settings(SYSTEM_DB_POOL_SIZE=size).SYSTEM_DB_POOL_SIZE,
        max_overflow=0, timeout=0.01,
    )
    connections = []
    try:
        for _ in range(count):
            connections.append(pool.connect())
        assert len(connections) == count
        if size:
            with pytest.raises(TimeoutError):
                pool.connect()
    finally:
        for connection in connections:
            connection.close()
        pool.dispose()
