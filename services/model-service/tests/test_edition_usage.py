"""Edition /limits usage payload — current counts for capped resources (Bug-5458).

`_tenant_usage` must report a current count for every resource the UI compares
against an entitlement max: models, users (per-tenant), projects (per-tenant), and
tenants (platform-level, own/non-demo only). Counts must be scoped to match the
entitlement each is compared against:
- models / users / projects -> tenant DB (per-tenant caps).
- tenants -> system DB, demo excluded via classify_tenant (platform `own_tenants` cap).

Run from tessallite/services/model-service/:
    pytest tests/test_edition_usage.py
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.api import edition as mod

pytestmark = pytest.mark.unit


class _TenantDB:
    """Fake tenant-DB session returning a fixed count per select-from model name."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    async def execute(self, stmt):  # noqa: ANN001 — test double
        # The model class is the entity selected_from; read its name to pick a count.
        name = stmt.get_final_froms()[0].name
        return MagicMock(scalar=MagicMock(return_value=self._counts.get(name, 0)))


class _SystemDB:
    """Fake system-DB session returning a fixed set of tenant slugs for SystemTenant."""

    def __init__(self, slugs: list[str]) -> None:
        self._slugs = slugs

    async def execute(self, stmt):  # noqa: ANN001 — test double
        scalars = MagicMock()
        scalars.all.return_value = list(self._slugs)
        return MagicMock(scalars=MagicMock(return_value=scalars))


def _async_gen_from(value):
    async def _gen(*_a, **_k):
        yield value
    return _gen


def _table_name(model) -> str:
    return model.__tablename__


@pytest.fixture
def patched(monkeypatch):
    # Tenant DB: distinct counts per resource so a mis-mapped field is caught.
    counts = {
        _table_name(mod.Model): 7,
        _table_name(mod.LocalUser): 3,
        _table_name(mod.Project): 2,
    }
    monkeypatch.setattr(mod, "get_tenant_db", _async_gen_from(_TenantDB(counts)))

    # System DB: three slugs, one of which is the demo tenant (must be excluded).
    monkeypatch.setattr(
        mod, "get_system_db", _async_gen_from(_SystemDB(["acme", "beta", "demo"]))
    )

    mgr = MagicMock()
    mgr.classify_tenant.side_effect = lambda s: "demo" if s == "demo" else "own"
    monkeypatch.setattr(mod, "get_license_manager", MagicMock(return_value=mgr))
    return mgr


@pytest.mark.asyncio
async def test_usage_includes_models_users_projects_and_own_tenants(patched):
    usage = await mod._tenant_usage("acme")
    # Per-tenant counts (tenant DB), each field aligned to its own resource.
    assert usage["models"] == 7
    assert usage["users"] == 3
    assert usage["projects"] == 2
    # Platform-level own-tenant count (system DB), demo excluded -> 2 of 3 slugs.
    assert usage["tenants"] == 2


@pytest.mark.asyncio
async def test_usage_omits_tenants_when_system_db_unavailable(monkeypatch, patched):
    def _raise(*_a, **_k):
        raise RuntimeError("system DB down")

    monkeypatch.setattr(mod, "get_system_db", _raise)
    usage = await mod._tenant_usage("acme")
    # Per-tenant counts still present; tenants omitted rather than a wrong 0.
    assert usage["models"] == 7
    assert usage["projects"] == 2
    assert "tenants" not in usage


@pytest.mark.asyncio
async def test_usage_degrades_to_only_tenants_when_tenant_db_unavailable(
    monkeypatch, patched
):
    def _raise(*_a, **_k):
        raise RuntimeError("no tenant context")

    monkeypatch.setattr(mod, "get_tenant_db", _raise)
    usage = await mod._tenant_usage("acme")
    # Tenant-scoped counts unavailable; the own-tenant count still resolves.
    assert "models" not in usage
    assert "projects" not in usage
    assert usage["tenants"] == 2
