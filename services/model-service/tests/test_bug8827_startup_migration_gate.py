"""Bug-8827 regression guards for the model-service startup migration gate."""
from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import startup_migrations
from .result_fakes import FakeResult


REPO_ROOT = Path(__file__).resolve().parents[4]


class _SystemSession:
    def __init__(self, tenants):
        self._tenants = tenants

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement):
        assert "ORDER BY tess_system.tenants.slug" in str(statement)
        assert "WHERE" not in str(statement)
        return FakeResult(sorted(self._tenants, key=lambda tenant: tenant.slug))


@pytest.fixture(autouse=True)
def _snapshot(monkeypatch):
    monkeypatch.setattr(startup_migrations, "refresh_system_snapshot", AsyncMock())


def _tenant(slug: str, *, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        encrypted_db_url=f"url-{slug}".encode(),
        is_active=active,
    )


@pytest.mark.asyncio
async def test_startup_gate_migrates_system_and_every_persisted_tenant(monkeypatch):
    """The gate must cover all tenants, not only active or first-listed ones."""
    tenants = [_tenant("zeta"), _tenant("alpha", active=False)]
    calls: list[tuple[str, str | None, str | None]] = []

    def fake_run(mode, *, tenant_slug="", database_url=""):
        calls.append((mode, tenant_slug or None, database_url or None))

    monkeypatch.setattr(startup_migrations, "_run_alembic", fake_run)
    monkeypatch.setattr(
        startup_migrations,
        "SystemSessionLocal",
        lambda: _SystemSession(tenants),
    )
    monkeypatch.setattr(
        startup_migrations,
        "decrypt_str",
        lambda encrypted: encrypted.decode(),
    )
    monkeypatch.setattr(
        startup_migrations,
        "normalize_tenant_db_url",
        lambda url, slug: f"normalised-{slug}:{url}",
    )

    await startup_migrations.migrate_before_serve()

    assert calls == [
        ("system", None, None),
        ("tenant", "alpha", "normalised-alpha:url-alpha"),
        ("tenant", "zeta", "normalised-zeta:url-zeta"),
    ]


@pytest.mark.asyncio
async def test_startup_gate_isolates_one_failure_and_continues(monkeypatch, caplog):
    """One tenant failure is logged while other tenants still migrate."""
    tenants = [_tenant("alpha"), _tenant("beta"), _tenant("gamma")]
    calls: list[str] = []

    def fake_run(mode, *, tenant_slug="", database_url=""):
        calls.append(tenant_slug or mode)
        if tenant_slug == "beta":
            raise RuntimeError("tenant migration failed")

    monkeypatch.setattr(startup_migrations, "_run_alembic", fake_run)
    monkeypatch.setattr(
        startup_migrations,
        "SystemSessionLocal",
        lambda: _SystemSession(tenants),
    )
    monkeypatch.setattr(
        startup_migrations,
        "decrypt_str",
        lambda encrypted: encrypted.decode(),
    )
    monkeypatch.setattr(
        startup_migrations,
        "normalize_tenant_db_url",
        lambda url, slug: url,
    )

    await startup_migrations.migrate_before_serve()

    assert calls == ["system", "alpha", "beta", "gamma"]
    assert "tenant=beta" in caplog.text
    assert "access_refused=true" in caplog.text
    assert "repair=system-admin-tenant-migration" in caplog.text


@pytest.mark.asyncio
async def test_startup_gate_logs_decrypt_failure_and_migrates_remaining_tenants(
    monkeypatch,
    caplog,
):
    """A credential failure is explicit while healthy tenants continue."""
    tenants = [_tenant("alpha"), _tenant("beta"), _tenant("gamma")]
    calls: list[str] = []

    def fake_run(mode, *, tenant_slug="", database_url=""):
        calls.append(tenant_slug or mode)

    def fake_decrypt(encrypted):
        if encrypted == b"url-beta":
            raise ValueError("cannot decrypt tenant credential")
        return encrypted.decode()

    monkeypatch.setattr(startup_migrations, "_run_alembic", fake_run)
    monkeypatch.setattr(
        startup_migrations,
        "SystemSessionLocal",
        lambda: _SystemSession(tenants),
    )
    monkeypatch.setattr(startup_migrations, "decrypt_str", fake_decrypt)
    monkeypatch.setattr(
        startup_migrations,
        "normalize_tenant_db_url",
        lambda url, slug: url,
    )

    await startup_migrations.migrate_before_serve()

    assert calls == ["system", "alpha", "gamma"]
    assert "tenant=beta" in caplog.text
    assert "stored DB credentials cannot be decrypted with configured keys" in caplog.text


def test_bug8827_refresh_paths_use_the_image_gate_without_an_entrypoint_override():
    """The direct and wrapped refresh paths must all retain the image gate."""
    entrypoint = (
        REPO_ROOT / "tessallite/services/model-service/entrypoint.sh"
    ).read_text(encoding="utf-8")
    assert entrypoint.index("python -m src.startup_migrations") < entrypoint.index(
        'exec "$@"'
    )

    for dockerfile_path in (
        REPO_ROOT / "tessallite/services/model-service/Dockerfile",
        REPO_ROOT / "tessallite/infra/cloud-run/Dockerfile.model-service",
    ):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        assert "entrypoint.sh" in dockerfile
        assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in dockerfile

    for compose_path, next_service in (
        (REPO_ROOT / "tessallite/infra/docker-compose.yml", "query-router"),
        (REPO_ROOT / "deploy/community/docker-compose.yml", "query-router"),
    ):
        compose = compose_path.read_text(encoding="utf-8")
        block = compose.split("\n  model-service:\n", 1)[1].split(
            f"\n  {next_service}:", 1
        )[0]
        assert "entrypoint:" not in block
        assert "command:" not in block
        for name in ("query-router", "optimizer", "scheduler", "agent-service"):
            service_block = re.split(
                r"\n  (?=\S)", compose.split(f"\n  {name}:\n", 1)[1], maxsplit=1
            )[0]
            assert "model-service:" in service_block

    fast_bash = (
        REPO_ROOT / "deploy/fast-rebuild-deploy/fast-deploy.sh"
    ).read_text(encoding="utf-8")
    fast_windows = (
        REPO_ROOT / "deploy/fast-rebuild-deploy/fast-deploy.bat"
    ).read_text(encoding="utf-8")
    community_install = (
        REPO_ROOT / "deploy/community/install.sh"
    ).read_text(encoding="utf-8")
    for refresh_path in (fast_bash, fast_windows, community_install):
        assert "--entrypoint" not in refresh_path
        assert "migrate/tenant" in refresh_path or "startup migration gate" in refresh_path
    assert "startup migration gate already ran" in fast_bash
    assert "startup migration gate already ran" in fast_windows
    assert "docker compose --env-file \"$ENV_FILE\" up -d" in community_install
    assert '--wait --wait-timeout "$HEALTH_WAIT_SECS" model-service' in fast_bash
    assert '--wait --wait-timeout %HEALTH_WAIT_SECS% model-service' in fast_windows
    chart = (REPO_ROOT / "deploy/community/helm/tessallite-community/templates/services.yaml").read_text()
    assert 'if ne $name "model-service"' in chart
    assert "name: wait-for-model-migrations" in chart
    assert "startupProbe:" in chart


def test_bug8827_helm_upgrade_migrates_new_image_before_replacing_workloads():
    """Old healthy model pods cannot satisfy an upgrade's migration gate."""
    job = (
        REPO_ROOT / "deploy/community/helm/tessallite-community/templates/pre-upgrade-migrate-job.yaml"
    ).read_text()
    assert '"helm.sh/hook": pre-upgrade' in job
    assert 'include "tc.image" (dict "root" . "name" "model-service")' in job
    assert 'include "tc.commonEnv" .' in job
    assert job.count('include "tc.labels" .') == 2  # Job AND pod: intra-release NetworkPolicy.
    assert 'args: ["true"]' in job
    assert "command:" not in job  # Retain the image's migrate-then-serve entrypoint.
