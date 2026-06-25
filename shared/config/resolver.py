"""Setting resolver — read/write API with strict precedence.

Precedence (per C-2 in docs/archive/archive_config-revamp-questions.md):

    model_settings  ->  project_settings  ->  tenant_settings
    ->  system_settings  ->  registry default

Lower level wins when present; a stored ``None`` means "explicitly unset,
fall through". Reads are cached in-process for 30 seconds; the cache is
invalidated on every successful write.

Scoping rules
-------------
- ``get_setting("ai_scheduler.cron", model_id=...)`` walks all four levels.
- ``get_setting("agg_target.default_schema", project_id=...)`` walks
  project -> tenant -> system -> default.
- ``get_setting("rate_limit.per_minute")`` walks system -> default only.
- A scope identifier (model_id, project_id, tenant_id) is OPTIONAL even
  for keys defined at that level — callers may not always have one
  (e.g. system bootstrap reads). Missing scopes are silently skipped.

Writes
------
``set_setting`` writes at exactly one level. The level is inferred from
which scope id was provided (model > project > tenant > system).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.registry import (
    REGISTRY,
    SettingDef,
    coerce,
    get_def,
    has_key,
    validate,
)
from shared.db.models import (
    ModelSetting,
    ProjectSetting,
    SystemRestartPending,
    SystemSetting,
    TenantSetting,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-process cache
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 30


class _ScopeCache:
    """Tiny TTL cache keyed by (scope_kind, scope_id, key)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], tuple[float, Any]] = {}

    def get(self, kind: str, scope_id: str, key: str) -> tuple[bool, Any]:
        entry = self._store.get((kind, scope_id, key))
        if entry is None:
            return False, None
        expires, value = entry
        if expires < time.monotonic():
            self._store.pop((kind, scope_id, key), None)
            return False, None
        return True, value

    def put(self, kind: str, scope_id: str, key: str, value: Any) -> None:
        self._store[(kind, scope_id, key)] = (
            time.monotonic() + _CACHE_TTL_SECONDS,
            value,
        )

    def invalidate(self, kind: str, scope_id: str, key: str) -> None:
        self._store.pop((kind, scope_id, key), None)

    def invalidate_key_everywhere(self, key: str) -> None:
        for k in list(self._store.keys()):
            if k[2] == key:
                self._store.pop(k, None)

    def clear(self) -> None:
        self._store.clear()


_cache = _ScopeCache()


def clear_cache() -> None:
    """Test/admin helper: drop all cached resolutions."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Internal table reads
# ---------------------------------------------------------------------------

async def _read_system(session: AsyncSession, key: str) -> tuple[bool, Any]:
    cached, value = _cache.get("system", "_", key)
    if cached:
        return value is not _SENTINEL, value if value is not _SENTINEL else None

    row = await session.execute(
        select(SystemSetting.value_json).where(SystemSetting.key == key)
    )
    val = row.scalar_one_or_none()
    if val is None:
        _cache.put("system", "_", key, _SENTINEL)
        return False, None
    _cache.put("system", "_", key, val)
    return True, val


async def _read_tenant(
    session: AsyncSession, key: str, tenant_id: str = "",
) -> tuple[bool, Any]:
    scope = tenant_id or "_uncached_"
    if tenant_id:
        cached, value = _cache.get("tenant", scope, key)
        if cached:
            return value is not _SENTINEL, value if value is not _SENTINEL else None

    row = await session.execute(
        select(TenantSetting.value_json).where(TenantSetting.key == key)
    )
    val = row.scalar_one_or_none()
    if val is None:
        if tenant_id:
            _cache.put("tenant", scope, key, _SENTINEL)
        return False, None
    if tenant_id:
        _cache.put("tenant", scope, key, val)
    return True, val


async def _read_project(
    session: AsyncSession, project_id: uuid.UUID, key: str
) -> tuple[bool, Any]:
    cached, value = _cache.get("project", str(project_id), key)
    if cached:
        return value is not _SENTINEL, value if value is not _SENTINEL else None

    row = await session.execute(
        select(ProjectSetting.value_json).where(
            ProjectSetting.project_id == project_id,
            ProjectSetting.key == key,
        )
    )
    val = row.scalar_one_or_none()
    if val is None:
        _cache.put("project", str(project_id), key, _SENTINEL)
        return False, None
    _cache.put("project", str(project_id), key, val)
    return True, val


async def _read_model(
    session: AsyncSession, model_id: uuid.UUID, key: str
) -> tuple[bool, Any]:
    cached, value = _cache.get("model", str(model_id), key)
    if cached:
        return value is not _SENTINEL, value if value is not _SENTINEL else None

    row = await session.execute(
        select(ModelSetting.value_json).where(
            ModelSetting.model_id == model_id,
            ModelSetting.key == key,
        )
    )
    val = row.scalar_one_or_none()
    if val is None:
        _cache.put("model", str(model_id), key, _SENTINEL)
        return False, None
    _cache.put("model", str(model_id), key, val)
    return True, val


# Sentinel used to remember "we looked, found nothing" so we don't re-query.
_SENTINEL = object()


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

async def get_setting(
    key: str,
    *,
    system_session: Optional[AsyncSession] = None,
    tenant_session: Optional[AsyncSession] = None,
    tenant_id: Optional[str] = None,
    model_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
) -> Any:
    """Resolve ``key`` walking model -> project -> tenant -> system -> default.

    The caller passes whichever sessions and ids are in scope. A null
    (None / JSON null) at any level is treated as "fall through".

    ``tenant_id`` is used as the cache scope for tenant-level settings. When
    omitted the function tries ``tenant_session.info["tenant_id"]`` (set
    automatically by ``get_tenant_db`` / ``get_tenant_session_factory``).
    If neither is available the cache is bypassed (safe but slower).
    """
    definition = _find_def_for_key(key)

    if tenant_id is None and tenant_session is not None:
        tenant_id = getattr(tenant_session, "info", {}).get("tenant_id")

    if model_id is not None and tenant_session is not None and has_key(key, "model"):
        present, val = await _read_model(tenant_session, model_id, key)
        if present and val is not None:
            return coerce(val, definition)

    if project_id is not None and tenant_session is not None and has_key(key, "project"):
        present, val = await _read_project(tenant_session, project_id, key)
        if present and val is not None:
            return coerce(val, definition)

    if tenant_session is not None and has_key(key, "tenant"):
        present, val = await _read_tenant(tenant_session, key, tenant_id=tenant_id or "")
        if present and val is not None:
            return coerce(val, definition)

    if system_session is not None and has_key(key, "system"):
        present, val = await _read_system(system_session, key)
        if present and val is not None:
            return coerce(val, definition)

    return _default_for_key(key)


async def get_settings_bulk(
    keys: list[str],
    *,
    system_session: Optional[AsyncSession] = None,
    tenant_session: Optional[AsyncSession] = None,
    tenant_id: Optional[str] = None,
    model_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        out[k] = await get_setting(
            k,
            system_session=system_session,
            tenant_session=tenant_session,
            tenant_id=tenant_id,
            model_id=model_id,
            project_id=project_id,
        )
    return out


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

async def set_setting(
    key: str,
    value: Any,
    *,
    actor: str,
    system_session: Optional[AsyncSession] = None,
    tenant_session: Optional[AsyncSession] = None,
    tenant_id: Optional[str] = None,
    model_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
    tenant_scope: bool = False,
) -> None:
    """Write ``value`` to exactly one level.

    Level is inferred from which scope is provided. Pass ``tenant_scope=True``
    for tenant-level writes (no scope id needed because the per-tenant DB
    is implicit). Validates against the registry first.
    """
    if model_id is not None:
        level = "model"
    elif project_id is not None:
        level = "project"
    elif tenant_scope:
        level = "tenant"
    elif system_session is not None:
        level = "system"
    else:
        raise ValueError("set_setting requires a scope (model/project/tenant/system)")

    definition = get_def(key, level)  # raises if key not declared at this level
    validate(value, definition)
    coerced = coerce(value, definition)

    if level == "model":
        assert tenant_session is not None and model_id is not None
        stmt = pg_insert(ModelSetting).values(
            model_id=model_id, key=key, value_json=coerced, updated_by=actor,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["model_id", "key"],
            set_={"value_json": coerced, "updated_by": actor},
        )
        await tenant_session.execute(stmt)
        await tenant_session.commit()
        _cache.invalidate("model", str(model_id), key)

    elif level == "project":
        assert tenant_session is not None and project_id is not None
        stmt = pg_insert(ProjectSetting).values(
            project_id=project_id, key=key, value_json=coerced, updated_by=actor,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["project_id", "key"],
            set_={"value_json": coerced, "updated_by": actor},
        )
        await tenant_session.execute(stmt)
        await tenant_session.commit()
        _cache.invalidate("project", str(project_id), key)

    elif level == "tenant":
        assert tenant_session is not None
        stmt = pg_insert(TenantSetting).values(
            key=key, value_json=coerced, updated_by=actor,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value_json": coerced, "updated_by": actor},
        )
        await tenant_session.execute(stmt)
        await tenant_session.commit()
        if tenant_id:
            _cache.invalidate("tenant", tenant_id, key)
        else:
            _cache.invalidate_key_everywhere(key)

    else:  # system
        assert system_session is not None
        stmt = pg_insert(SystemSetting).values(
            key=key, value_json=coerced, updated_by=actor,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value_json": coerced, "updated_by": actor},
        )
        await system_session.execute(stmt)

        if definition.restart_required:
            system_session.add(
                SystemRestartPending(setting_key=key, written_by=actor)
            )
        await system_session.commit()
        _cache.invalidate("system", "_", key)


async def delete_setting(
    key: str,
    *,
    actor: str,
    system_session: Optional[AsyncSession] = None,
    tenant_session: Optional[AsyncSession] = None,
    tenant_id: Optional[str] = None,
    model_id: Optional[uuid.UUID] = None,
    project_id: Optional[uuid.UUID] = None,
    tenant_scope: bool = False,
) -> None:
    """Remove a row at the specified level so the resolver falls back upward."""
    if model_id is not None:
        assert tenant_session is not None
        await tenant_session.execute(
            delete(ModelSetting).where(
                ModelSetting.model_id == model_id, ModelSetting.key == key
            )
        )
        await tenant_session.commit()
        _cache.invalidate("model", str(model_id), key)
    elif project_id is not None:
        assert tenant_session is not None
        await tenant_session.execute(
            delete(ProjectSetting).where(
                ProjectSetting.project_id == project_id, ProjectSetting.key == key
            )
        )
        await tenant_session.commit()
        _cache.invalidate("project", str(project_id), key)
    elif tenant_scope:
        assert tenant_session is not None
        await tenant_session.execute(
            delete(TenantSetting).where(TenantSetting.key == key)
        )
        await tenant_session.commit()
        if tenant_id:
            _cache.invalidate("tenant", tenant_id, key)
        else:
            _cache.invalidate_key_everywhere(key)
    else:
        assert system_session is not None
        await system_session.execute(
            delete(SystemSetting).where(SystemSetting.key == key)
        )
        await system_session.commit()
        _cache.invalidate("system", "_", key)


async def list_pending_restart(
    system_session: AsyncSession,
) -> list[dict[str, Any]]:
    """Return outstanding restart-required writes for the System UI banner."""
    rows = await system_session.execute(
        select(SystemRestartPending).order_by(SystemRestartPending.written_at.desc())
    )
    return [
        {
            "setting_key": r.setting_key,
            "written_at": r.written_at.isoformat() if r.written_at else None,
            "written_by": r.written_by,
        }
        for r in rows.scalars()
    ]


async def clear_pending_restart(
    system_session: AsyncSession, *, setting_keys: Optional[list[str]] = None
) -> None:
    """Drop pending-restart entries (called on service start when settings have applied)."""
    stmt = delete(SystemRestartPending)
    if setting_keys:
        stmt = stmt.where(SystemRestartPending.setting_key.in_(setting_keys))
    await system_session.execute(stmt)
    await system_session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_def_for_key(key: str) -> SettingDef:
    """Find the definition for ``key`` at any level for type/validation use.

    Walks model -> project -> tenant -> system so the most-specific
    definition wins (its validator is what should run on writes at that
    level). Defaults are looked up separately via ``_default_for_key``
    because override-level definitions intentionally default to None.
    """
    for level in ("model", "project", "tenant", "system"):
        if (level, key) in REGISTRY:
            return REGISTRY[(level, key)]
    raise KeyError(f"unknown setting key: {key!r}")


def _default_for_key(key: str) -> Any:
    """Walk levels from most-specific to least-specific and return the
    first non-None default, mirroring what would happen if every storage
    layer were empty.
    """
    for level in ("model", "project", "tenant", "system"):
        d = REGISTRY.get((level, key))
        if d is not None and d.default is not None:
            return d.default
    return None
