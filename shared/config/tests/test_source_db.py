"""Tests for the source-DB endpoint helper.

The helper sits between the project_connection's stored credentials/config
and the resolver. Precedence is creds → config → system setting → registry default.

Post 2026-04 admin-config restructure, ``source_db.*`` fallback keys live
at the system level (surfaced=False) — so an admin-set system override
beats the registry default but creds/config still beat both.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.config.resolver import clear_cache
from shared.config.source_db import (
    resolve_aggregate_target_defaults,
    resolve_source_db_endpoint,
    resolve_spark_thrift_defaults,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def _session_returning(value):
    s = AsyncMock()
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    s.execute = AsyncMock(return_value=r)
    return s


@pytest.mark.asyncio
async def test_creds_win_over_everything():
    host, port, db = await resolve_source_db_endpoint(
        {"host": "explicit.example.com", "port": 9999, "database": "live"},
        {"host": "config.example.com"},
    )
    assert (host, port, db) == ("explicit.example.com", 9999, "live")


@pytest.mark.asyncio
async def test_config_fills_in_when_creds_missing():
    host, port, db = await resolve_source_db_endpoint(
        {},
        {"host": "config.example.com", "port": 5433, "database": "cfg"},
    )
    assert (host, port, db) == ("config.example.com", 5433, "cfg")


@pytest.mark.asyncio
async def test_falls_through_to_registry_default_when_nothing_set():
    """No tenant_session, no creds, no config → registry defaults."""
    host, port, db = await resolve_source_db_endpoint({}, {})
    assert (host, port, db) == ("localhost", 5432, "postgres")


@pytest.mark.asyncio
async def test_system_setting_overrides_registry_default():
    """System-stored values supersede the registry default for the relevant keys.

    Pre-restructure ``source_db.fallback_*`` lived at tenant level; after
    the 2026-04 restructure these are system-level (surfaced=False), so
    the override now flows through ``system_session``."""
    sequence = ["system-host.example.com", 6543, "systemdb"]

    async def fake_execute(*args, **kwargs):
        result = MagicMock()
        result.scalar_one_or_none.return_value = sequence.pop(0)
        return result

    system_session = AsyncMock()
    system_session.execute = AsyncMock(side_effect=fake_execute)

    host, port, db = await resolve_source_db_endpoint(
        {}, {}, system_session=system_session,
    )
    assert (host, port, db) == ("system-host.example.com", 6543, "systemdb")


@pytest.mark.asyncio
async def test_aggregate_target_defaults_returns_all_three():
    out = await resolve_aggregate_target_defaults()
    assert set(out.keys()) == {"schema", "dataset", "database"}
    assert out["schema"] == "aggregates"
    assert out["dataset"] == "default"
    assert out["database"] == "public"


@pytest.mark.asyncio
async def test_spark_thrift_defaults_returns_typed_values():
    out = await resolve_spark_thrift_defaults()
    assert out["port"] == 10000
    assert out["database"] == "default"
    assert out["auth_mode"] == "NOSASL"
