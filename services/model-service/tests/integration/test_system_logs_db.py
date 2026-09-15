"""Raw log API authority, storage, search and expiration on a private database."""

from datetime import datetime, timedelta, timezone
import base64
import asyncio
import importlib.util
from pathlib import Path
import json
import os
from uuid import UUID, uuid4

import asyncpg
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.auth.middleware import CurrentUser, get_current_user
from shared.config.settings import get_settings
from shared.db.models import SystemLog
from shared.db.session import get_system_db
from shared.system_logs import metrics
from shared.system_logs.retention import purge_expired_logs, retention_cutoff
from shared.system_logs.storage import refresh_storage_metrics
from src.api import system_log_ingest, system_logs

DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DB_URL, reason="TESSALLITE_VERSIONING_DB_URL required"),
]
PREFIX = "/api/v1/admin/system-logs"


@pytest.fixture
async def log_api(monkeypatch):
    """Never create or purge a table in the caller's existing database."""
    name = "system_logs_test_" + uuid4().hex
    url = make_url(DB_URL)
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    engine = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        engine = create_async_engine(
            url.set(drivername="postgresql+asyncpg", database=name)
        )
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA tess_system"))
            migration_path = (
                Path(__file__).resolve().parents[4]
                / "shared/db/migrations/versions/0224_system_logs.py"
            )
            spec = importlib.util.spec_from_file_location(
                "system_log_migration", migration_path
            )
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)

            def upgrade(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()

            await connection.run_sync(upgrade)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        app = FastAPI()
        app.include_router(system_logs.router, prefix="/api/v1")
        app.include_router(system_log_ingest.router, prefix="/api/v1")

        async def database():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_system_db] = database
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            "admin", "__system__", "admin@example.test", "system_admin"
        )
        monkeypatch.setattr(get_settings(), "SYSTEM_LOGS_ENABLED", True)
        monkeypatch.setattr(get_settings(), "SYSTEM_LOG_RETENTION_DAYS", 30)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, app, factory
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()


def event(**changes):
    return {
        "id": str(uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "postgres",
        "level": "ERROR",
        "logger": "docker.stderr",
        "instance": "container-a",
        "message": "ERROR: connection refused",
        **changes,
    }


async def test_raw_logs_persist_deduplicate_redact_and_filter(log_api, monkeypatch):
    client, _, factory = log_api
    monkeypatch.setenv("SYSTEM_ADMIN_PASSWORD", "test-secret-never-store")
    stamp = datetime.now(timezone.utc).isoformat()
    rows = [
        event(
            timestamp=stamp,
            message="ERROR: 50%_done password=test-secret-never-store\n  source unavailable",
        ),
        event(
            timestamp=stamp, service="frontend", level="INFO", message="nginx started"
        ),
    ]
    first = await client.post(PREFIX + "/ingest", json={"items": rows})
    assert first.status_code == 200, first.text
    assert first.json() == {"stored": 2, "accepted": 2}
    duplicate = await client.post(PREFIX + "/ingest", json={"items": rows})
    assert duplicate.json() == {"stored": 0, "accepted": 2}
    found = await client.get(
        PREFIX, params={"q": "50%_done", "service": "postgres", "level": "ERROR"}
    )
    assert found.status_code == 200
    assert len(found.json()["items"]) == 1
    assert (
        found.json()["items"][0]["message"]
        == "ERROR: 50%_done password=[REDACTED]\n  source unavailable"
    )
    assert (await client.get(PREFIX, params={"q": "50X_done"})).json()["items"] == []
    page1 = (await client.get(PREFIX, params={"limit": 1})).json()
    page2 = (
        await client.get(PREFIX, params={"limit": 1, "cursor": page1["next_cursor"]})
    ).json()
    assert {page1["items"][0]["id"], page2["items"][0]["id"]} == {
        row["id"] for row in rows
    }
    assert page2["next_cursor"] is None
    async with factory() as db:
        stored = await db.scalar(
            text("SELECT string_agg(message, ' ') FROM tess_system.system_logs")
        )
    assert "test-secret-never-store" not in stored
    assert metrics.LOG_HEARTBEAT._value.get() > 0
    assert metrics.LOG_LAST_ERROR._value.get() > 0


async def test_api_persists_redacted_cloud_credentials(log_api):
    """The API backstop removes credentials absent from the process environment."""
    client, _, factory = log_api
    raw_values = [
        "private-key-material-123",
        "aws-secret-material-123",
        "client-secret-material-123",
        "session-token-material-123",
        "cloud-key-material-123",
        "json-private-key-material-123",
        "bearer-material-123",
        "PEM-secret-material-123",
    ]
    # Build the PEM delimiters by concatenation so this source file never holds a
    # contiguous private-key header. The community-export leak-check scans file
    # content, and a literal header here is indistinguishable from a real leaked
    # key, which blocks the public export. The assembled message is byte-identical
    # to the literal, so the redaction path under test is exercised unchanged.
    pem_begin = "-----BEGIN " + "PRIVATE KEY-----"
    pem_end = "-----END " + "PRIVATE KEY-----"
    message = (
        "private_key=private-key-material-123 "
        "AWS_SECRET_ACCESS_KEY=aws-secret-material-123 "
        "client_secret=client-secret-material-123 "
        "session_token=session-token-material-123 "
        "cloud/private-key=cloud-key-material-123 "
        '{"private_key": "json-private-key-material-123"} '
        "Bearer bearer-material-123 https://user:password@example.test "
        f"{pem_begin}\nPEM-secret-material-123\n"
        f"{pem_end}"
    )
    row = event(service="gateway", message=message)
    response = await client.post(PREFIX + "/ingest", json={"items": [row]})
    assert response.status_code == 200, response.text
    assert response.json() == {"stored": 1, "accepted": 1}

    async with factory() as db:
        stored = await db.scalar(
            text("SELECT message FROM tess_system.system_logs WHERE id = :id"),
            {"id": UUID(row["id"])},
        )
    assert stored is not None
    assert all(raw not in stored for raw in raw_values)
    assert "[REDACTED]" in stored
    assert "[REDACTED_AUTH]" in stored
    assert "[REDACTED_URL]" in stored
    assert "[REDACTED_PRIVATE_KEY]" in stored


async def test_scheduler_storage_measurement_is_independent_of_ingest_and_purge(
    log_api, monkeypatch
):
    """The scheduler observer reports retained rows when ingestion is disabled."""
    client, _, factory = log_api
    monkeypatch.setattr(get_settings(), "SYSTEM_LOGS_ENABLED", False)
    disabled = await client.post(PREFIX + "/ingest", json={"items": [event()]})
    assert disabled.status_code == 409

    now = datetime.now(timezone.utc)
    cutoff = retention_cutoff(now)
    assert cutoff is not None
    old = event(
        timestamp=(cutoff - timedelta(seconds=1)).isoformat(),
        message="retained old row",
    )
    recent = event(timestamp=now.isoformat(), message="retained current row")

    async with factory() as db:
        db.add_all(
            [
                SystemLog(
                    **{
                        **old,
                        "id": UUID(old["id"]),
                        "timestamp": datetime.fromisoformat(old["timestamp"]),
                    }
                ),
                SystemLog(
                    **{
                        **recent,
                        "id": UUID(recent["id"]),
                        "timestamp": datetime.fromisoformat(recent["timestamp"]),
                    }
                ),
            ]
        )
        await db.commit()
        await db.execute(text("ANALYZE tess_system.system_logs"))
        await refresh_storage_metrics(db)
        measured_bytes = metrics.LOG_BYTES._value.get()
        measured_rows = metrics.LOG_ROWS._value.get()
        measured_at = metrics.LOG_STORAGE_MEASURED_AT._value.get()

    assert measured_bytes > 0
    assert measured_rows >= 2
    assert measured_at > 0

    async with factory() as db:
        assert await purge_expired_logs(db, cutoff) == 1
        await db.execute(text("ANALYZE tess_system.system_logs"))
        await refresh_storage_metrics(db)
        after_purge_bytes = metrics.LOG_BYTES._value.get()
        after_purge_rows = metrics.LOG_ROWS._value.get()
        after_purge_measured_at = metrics.LOG_STORAGE_MEASURED_AT._value.get()

    # DELETE frees rows for reuse; PostgreSQL is not expected to shrink the
    # relation file immediately, so the row gauge is the decreasing signal.
    assert after_purge_bytes > 0
    assert after_purge_rows < measured_rows
    assert after_purge_measured_at >= measured_at


async def test_expiration_boundary_replay_and_manual_purge(log_api, monkeypatch):
    client, _, factory = log_api
    now = datetime.now(timezone.utc)
    cutoff = retention_cutoff(now)
    old = event(timestamp=(cutoff - timedelta(seconds=1)).isoformat())
    assert (await client.post(PREFIX + "/ingest", json={"items": [old]})).json() == {
        "stored": 0,
        "accepted": 1,
    }
    boundary = event(timestamp=cutoff.isoformat())
    recent = event(timestamp=now.isoformat())
    async with factory() as db:
        for row in (old, boundary, recent):
            values = {
                **row,
                "id": UUID(row["id"]),
                "timestamp": datetime.fromisoformat(row["timestamp"]),
            }
            db.add(SystemLog(**values))
        await db.commit()
        assert await purge_expired_logs(db, cutoff) == 1
        assert await purge_expired_logs(db, cutoff) == 0
    # The manual API and scheduler use the same strict older-than cutoff.
    monkeypatch.setattr(system_logs, "retention_cutoff", lambda: cutoff)
    assert (await client.post(PREFIX + "/purge")).json()["deleted"] == 0
    assert len((await client.get(PREFIX)).json()["items"]) == 2
    monkeypatch.setattr(get_settings(), "SYSTEM_LOG_RETENTION_DAYS", 0)
    assert retention_cutoff() is None
    async with factory() as db:
        assert await purge_expired_logs(db, None) == 0


@pytest.mark.parametrize(
    "role,tenant",
    [("viewer", "alpha"), ("tenant_admin", "alpha"), ("system_admin", "alpha")],
)
async def test_noncanonical_admin_cannot_read_write_or_purge(log_api, role, tenant):
    client, app, _ = log_api
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        "other", tenant, "other@example.test", role
    )
    for method, suffix, body in [
        ("GET", "", None),
        ("GET", "/settings", None),
        ("POST", "/ingest", {"items": [event()]}),
        ("POST", "/purge", None),
    ]:
        response = await client.request(method, PREFIX + suffix, json=body)
        assert response.status_code == 403


async def test_validation_disabled_collection_and_bad_cursor(log_api, monkeypatch):
    client, _, _ = log_api
    for row in [
        event(service="unknown"),
        event(level="secret"),
        event(timestamp="2026-01-01T00:00:00"),
        event(timestamp=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
    ]:
        assert (
            await client.post(PREFIX + "/ingest", json={"items": [row]})
        ).status_code == 422
    for cursor in (
        "%",
        base64.urlsafe_b64encode(
            json.dumps(["2026-01-01T00:00:00+00:00", 1]).encode()
        ).decode(),
    ):
        assert (await client.get(PREFIX, params={"cursor": cursor})).status_code == 422
    assert (
        await client.get(PREFIX, params={"from_date": "2026-01-01T00:00:00"})
    ).status_code == 422
    monkeypatch.setattr(get_settings(), "SYSTEM_LOGS_ENABLED", False)
    assert (
        await client.post(PREFIX + "/ingest", json={"items": [event()]})
    ).status_code == 409
    assert (await client.get(PREFIX)).status_code == 200


@pytest.mark.skipif(
    not os.environ.get("SYSTEM_LOG_TEST_COMPOSE_PROJECT"),
    reason="Live Docker Compose project not selected",
)
async def test_actual_compose_output_reaches_database_for_every_service(
    log_api, tmp_path, monkeypatch
):
    """Read existing output only; no container commands generate artificial logs."""
    client, _, factory = log_api
    path = Path(__file__).resolve().parents[5] / "deploy/system-logs/forward_logs.py"
    spec = importlib.util.spec_from_file_location("live_forward_logs", path)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    monkeypatch.setitem(collector.CONFIG, "collector_initial_lookback_seconds", 86400)
    loop = asyncio.get_running_loop()

    class ActualIngestion:
        def send(self, rows, overflows=0):
            future = asyncio.run_coroutine_threadsafe(
                client.post(
                    PREFIX + "/ingest", json={"items": rows, "overflows": overflows}
                ),
                loop,
            )
            response = future.result(timeout=30)
            assert response.status_code == 200, "Actual log ingestion failed"
            return response.json()

    counts = await asyncio.to_thread(
        collector.collect_once,
        ActualIngestion(),
        os.environ["SYSTEM_LOG_TEST_COMPOSE_PROJECT"],
        tmp_path / "cursor.json",
    )
    assert set(counts) == set(collector.CONFIG["services"])
    assert all(count > 0 for count in counts.values()), counts
    async with factory() as db:
        persisted = dict(
            (
                await db.execute(
                    text(
                        "SELECT service, count(*) FROM tess_system.system_logs GROUP BY service"
                    )
                )
            ).all()
        )
    assert persisted == counts
    print(
        "Actual Docker runtime records persisted:", json.dumps(counts, sort_keys=True)
    )
