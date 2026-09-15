"""F-RC-05: startup migration failures retain safe, actionable diagnostics."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src import startup_migrations
from src.api import admin


def test_admin_alembic_failure_logs_phase_target_and_redacted_stderr(
    monkeypatch, caplog
):
    """The admin migration path keeps error text but emits no DSN component."""
    failure = SimpleNamespace(
        returncode=1,
        stderr=(
            "asyncpg: connection refused for "
            "postgresql+asyncpg://dbuser:do-not-log@db.internal:5432/tessallite"
            "\nrevision 0212 failed"
        ),
    )
    monkeypatch.setattr(admin, "_find_alembic_ini", lambda: "alembic.ini")
    monkeypatch.setattr(admin, "system_snapshot_get", lambda _key: 30)
    monkeypatch.setattr(admin.subprocess, "run", lambda *args, **kwargs: failure)

    with caplog.at_level(logging.ERROR, logger=admin.logger.name):
        with pytest.raises(admin.AlembicMigrationError):
            admin._run_alembic(
                "tenant",
                tenant_slug="acme-demo",
                database_url="postgresql+asyncpg://dbuser:do-not-log@db.internal:5432/tessallite",
            )

    message = caplog.text
    assert "phase=tenant" in message
    assert "tenant=acme-demo" in message
    assert "target=tenant@head" in message
    assert "revision 0212 failed" in message
    assert "<redacted-dsn>" in message
    for dsn_component in (
        "postgresql+asyncpg://",
        "dbuser",
        "do-not-log",
        "db.internal",
        "5432",
        "tessallite",
    ):
        assert dsn_component not in message


@pytest.mark.asyncio
async def test_startup_gate_logs_safe_alembic_failure_before_refusing_to_serve(
    monkeypatch, caplog
):
    """A failed tenant is logged and refused while healthy service continues."""
    failure = admin.AlembicMigrationError(
        phase="tenant",
        tenant_slug="acme-demo",
        target_revision="tenant@head",
        stderr=(
            "password leak candidate "
            "postgresql+asyncpg://dbuser:do-not-log@db.internal:5432/tessallite "
            "AlembicError: revision 0223 failed"
        ),
    )

    async def targets():
        return [("acme-demo", "redacted-database-url")]

    async def refresh_snapshot():
        return None

    monkeypatch.setattr(startup_migrations, "_tenant_migration_targets", targets)
    monkeypatch.setattr(startup_migrations, "refresh_system_snapshot", refresh_snapshot)

    def fake_run(phase, **_kwargs):
        if phase == "tenant":
            raise failure

    monkeypatch.setattr(startup_migrations, "_run_alembic", fake_run)

    with caplog.at_level(logging.ERROR, logger=startup_migrations.logger.name):
        await startup_migrations.migrate_before_serve()

    message = caplog.text
    assert "phase=tenant" in message
    assert "tenant=acme-demo" in message
    assert "target=tenant@head" in message
    assert "AlembicError: revision 0223 failed" in message
    assert "<redacted-dsn>" in message
    for dsn_component in (
        "postgresql+asyncpg://",
        "dbuser",
        "do-not-log",
        "db.internal",
        "5432",
        "tessallite",
    ):
        assert dsn_component not in message
    assert "access_refused=true" in message
    assert "repair=system-admin-tenant-migration" in message
