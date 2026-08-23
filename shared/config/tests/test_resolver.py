"""Tests for the setting resolver — precedence, fall-through, cache."""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.config import resolver as r
from shared.config.resolver import clear_cache, get_setting, set_setting


def _mk_session_returning(value: Any):
    """Build an AsyncMock session whose execute().scalar_one_or_none()
    returns ``value``."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.fixture(autouse=True)
def _clear_cache_each_test():
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_registry_default_when_nothing_stored():
    sys_session = _mk_session_returning(None)
    val = await get_setting("auth.jwt_expire_minutes", system_session=sys_session)
    assert val == 60  # registry default


@pytest.mark.asyncio
async def test_system_value_wins_over_default():
    sys_session = _mk_session_returning(120)
    val = await get_setting("auth.jwt_expire_minutes", system_session=sys_session)
    assert val == 120


@pytest.mark.asyncio
async def test_g5_join_threshold_uses_explicit_system_override_not_default():
    """G5 resolver boundary: deploy policy reads the system tier.

    Test escape: a tenant-only deploy resolver call previously ignored stored
    system overrides and silently used the registry default. Guard: resolve
    the exact G5 key once without a system session and once with a real
    system-session-shaped resolver, asserting the values differ. Tier: T3.
    """
    from shared.semantic.join_population_validator import (
        DEFAULT_ROW_EFFECT_WARNING_THRESHOLD,
        SETTING_ROW_EFFECT_THRESHOLD,
    )

    default_value = await get_setting(SETTING_ROW_EFFECT_THRESHOLD)
    explicit_system = _mk_session_returning(0.25)
    overridden_value = await get_setting(
        SETTING_ROW_EFFECT_THRESHOLD,
        system_session=explicit_system,
    )

    assert default_value == DEFAULT_ROW_EFFECT_WARNING_THRESHOLD
    assert overridden_value == 0.25
    assert overridden_value != default_value


@pytest.mark.asyncio
async def test_model_value_wins_over_system_for_dual_level_key():
    """For a key defined at both system and model (e.g. ``result.max_rows``),
    the model-level value wins when a model_id is in scope.

    Pre-restructure this test used a tenant-level override; after the
    2026-04 admin-config restructure tenant has no surfaced keys, so the
    same precedence intent is now exercised at model level."""
    sys_session = _mk_session_returning(99999)
    tenant_session = _mk_session_returning(50000)  # holds model rows too
    val = await get_setting(
        "result.max_rows",
        system_session=sys_session,
        tenant_session=tenant_session,
        model_id=uuid.uuid4(),
    )
    assert val == 50000


@pytest.mark.asyncio
async def test_model_value_wins_for_model_key():
    """For a key defined at the model level, model value wins over project/tenant/system."""
    tenant_session = _mk_session_returning("0 9 * * *")
    val = await get_setting(
        "aggregate.default_cron",
        tenant_session=tenant_session,
        model_id=uuid.uuid4(),
    )
    assert val == "0 9 * * *"


@pytest.mark.asyncio
async def test_falls_through_when_lower_returns_null():
    """A stored ``None`` at the model level must fall through to the system layer.

    Pre-restructure this exercised project→tenant fallthrough using
    ``source_db.fallback_host``; after the 2026-04 restructure the same
    fall-through invariant is exercised at model→system using
    ``result.max_rows`` (declared at both levels)."""
    model_session = _mk_session_returning(None)        # model: null → fall through
    system_session = _mk_session_returning(75000)      # system: value

    val = await get_setting(
        "result.max_rows",
        system_session=system_session,
        tenant_session=model_session,  # holds model rows
        model_id=uuid.uuid4(),
    )
    assert val == 75000


@pytest.mark.asyncio
async def test_unknown_scope_silently_skipped():
    """Calling get_setting() with no tenant_session for a tenant-level key
    should still return the registry default, not raise."""
    val = await get_setting("agg_target.default_schema")
    assert val == "aggregates"  # registry default


# ---------------------------------------------------------------------------
# Type coercion through the resolver
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolver_coerces_string_int_from_storage():
    """Defensive: if a JSONB stored a number-as-string somehow, coerce it."""
    sys_session = _mk_session_returning("180")
    val = await get_setting("auth.jwt_expire_minutes", system_session=sys_session)
    assert val == 180
    assert isinstance(val, int)


# ---------------------------------------------------------------------------
# set_setting validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_setting_rejects_invalid_value():
    sys_session = AsyncMock()
    with pytest.raises(ValueError):
        await set_setting(
            "auth.jwt_expire_minutes", -1,
            actor="test", system_session=sys_session,
        )


@pytest.mark.asyncio
async def test_set_setting_rejects_unknown_key_at_level():
    sys_session = AsyncMock()
    with pytest.raises(KeyError):
        await set_setting(
            "aggregate.default_cron",  # model-level only
            "0 1 * * *",
            actor="test", system_session=sys_session,
        )


@pytest.mark.asyncio
async def test_set_setting_requires_a_scope():
    with pytest.raises(ValueError):
        await set_setting("auth.jwt_expire_minutes", 30, actor="test")


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_returns_value_on_second_read():
    sys_session = _mk_session_returning(45)
    v1 = await get_setting("auth.jwt_expire_minutes", system_session=sys_session)
    v2 = await get_setting("auth.jwt_expire_minutes", system_session=sys_session)
    assert v1 == v2 == 45
    # The second call should not have hit execute() a second time.
    assert sys_session.execute.await_count == 1


@pytest.mark.asyncio
async def test_clear_cache_forces_re_read():
    sys_session = _mk_session_returning(45)
    await get_setting("auth.jwt_expire_minutes", system_session=sys_session)
    clear_cache()
    await get_setting("auth.jwt_expire_minutes", system_session=sys_session)
    assert sys_session.execute.await_count == 2


# ---------------------------------------------------------------------------
# Cross-tenant cache isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_cache_isolation():
    """Two tenants with different values for the same key must each see their
    own value, never the other's."""
    tenant_a = _mk_session_returning("warn")
    tenant_a.info = {"tenant_id": "tenant-a"}

    tenant_b = _mk_session_returning("critical")
    tenant_b.info = {"tenant_id": "tenant-b"}

    val_a = await get_setting("audit.log_level", tenant_session=tenant_a)
    val_b = await get_setting("audit.log_level", tenant_session=tenant_b)

    assert val_a == "warn"
    assert val_b == "critical"

    val_a2 = await get_setting("audit.log_level", tenant_session=tenant_a)
    assert val_a2 == "warn"
    assert tenant_a.execute.await_count == 1, "second read for tenant-a should come from cache"


@pytest.mark.asyncio
async def test_tenant_cache_without_tenant_id_bypasses_cache():
    """When session.info has no tenant_id, every read hits the DB (safe fallback)."""
    session = _mk_session_returning("warn")
    session.info = {}

    await get_setting("audit.log_level", tenant_session=session)
    await get_setting("audit.log_level", tenant_session=session)
    assert session.execute.await_count == 2, "without tenant_id, cache should be bypassed"
