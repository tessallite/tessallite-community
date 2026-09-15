"""Exercise startup migration and tenant readiness against real PostgreSQL."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from shared.security.credential_crypto import encrypt_str

DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL")
SERVICE = Path(__file__).resolve().parents[2]
TESSALLITE = SERVICE.parents[1]
INI = TESSALLITE / "shared/db/migrations/alembic.ini"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DB_URL, reason="TESSALLITE_VERSIONING_DB_URL required"),
]


def _run(env: dict[str, str], *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """Run one migration or entrypoint command without printing credentials."""
    return subprocess.run(
        list(args),
        cwd=TESSALLITE,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _assert_ok(result: subprocess.CompletedProcess, label: str) -> None:
    # Keep subprocess diagnostics out of assertion text: migration output can
    # contain connector details, and test failures must not expose credentials.
    assert result.returncode == 0, f"{label} failed"


def _readiness_probe_code() -> str:
    """Probe pooled and snapshot factories and print only safe typed details."""
    return """
import asyncio
import json
from shared.db.session import (
    TenantReadinessError,
    get_tenant_session_factory,
    get_tenant_snapshot_session_factory,
)

async def main():
    outcomes = []
    for name, getter in (
        ("pooled", get_tenant_session_factory),
        ("snapshot", get_tenant_snapshot_session_factory),
    ):
        try:
            await getter("beta")
        except TenantReadinessError as exc:
            outcomes.append({"path": name, "status": "refused", "detail": exc.detail})
        else:
            outcomes.append({"path": name, "status": "ready"})
    print("READINESS " + json.dumps(outcomes, sort_keys=True))

asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_startup_isolates_bad_tenant_and_shared_boundary_recovers():
    """Prove healthy startup, stale refusal, repair recovery, and concurrency."""
    db_name = "r17_readiness_" + uuid.uuid4().hex
    admin_url = make_url(DB_URL)
    admin = await asyncpg.connect(
        admin_url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    conn = None
    env = dict(os.environ)
    target_url = admin_url.set(database=db_name)
    target_dsn = target_url.render_as_string(hide_password=False)
    env.update(
        SYSTEM_DATABASE_URL=target_dsn,
        DATABASE_URL=target_dsn,
        PYTHONPATH=os.pathsep.join([str(SERVICE), str(TESSALLITE)]),
    )

    async def stamp(schema: str):
        return await conn.fetchval(f'SELECT version_num FROM "{schema}".alembic_version')

    def migrate(slug: str, target: str) -> subprocess.CompletedProcess:
        return _run(
            {**env, "MIGRATE_MODE": "tenant", "TENANT_SLUG": slug},
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(INI),
            "upgrade",
            target,
        )

    def downgrade(slug: str, target: str) -> subprocess.CompletedProcess:
        return _run(
            {**env, "MIGRATE_MODE": "tenant", "TENANT_SLUG": slug},
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(INI),
            "downgrade",
            target,
        )

    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
        system = _run(
            {**env, "MIGRATE_MODE": "system"},
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(INI),
            "upgrade",
            "system@head",
        )
        _assert_ok(system, "system fixture migration")
        conn = await asyncpg.connect(
            target_url.set(drivername="postgresql").render_as_string(hide_password=False)
        )
        scripts = ScriptDirectory.from_config(Config(str(INI)))
        tenant_head = scripts.get_revision("tenant@head")
        assert tenant_head is not None and tenant_head.down_revision
        old_revision = str(tenant_head.down_revision)
        encrypted_target = encrypt_str(target_dsn)

        # Include an inactive tenant: administrative activity must not be
        # mistaken for a runtime failure, and its schema remains owned/readied.
        for slug, active in (("alpha", True), ("beta", False)):
            await conn.execute(
                "INSERT INTO tess_system.tenants "
                "(id,slug,display_name,encrypted_db_url,db_schema_prefix,is_active) "
                "VALUES ($1,$2,$2,$3,$2,$4)",
                uuid.uuid4(),
                slug,
                encrypted_target,
                active,
            )
            result = migrate(slug, old_revision)
            _assert_ok(result, f"old {slug} migration")
            assert await stamp(slug + "_meta") == old_revision

        # The image gate sees both persisted tenants and brings both to the
        # graph-resolved head before serving.
        command = (
            "sh",
            str(SERVICE / "entrypoint.sh"),
            sys.executable,
            "-c",
            "print('SERVE_COMMAND_REACHED')",
        )
        startup = _run(env, *command)
        _assert_ok(startup, "healthy startup gate")
        assert "SERVE_COMMAND_REACHED" in startup.stdout
        assert await stamp("alpha_meta") == tenant_head.revision
        assert await stamp("beta_meta") == tenant_head.revision
        assert await conn.fetchval(
            "SELECT is_active FROM tess_system.tenants WHERE slug='beta'"
        ) is False

        # Recreate a real failed tenant: stale schema plus an unreadable stored
        # credential. The gate continues for alpha and the serving boundary
        # refuses both pooled and snapshot factory acquisition for beta.
        result = downgrade("beta", old_revision)
        _assert_ok(result, "stale beta fixture downgrade")
        assert await stamp("beta_meta") == old_revision
        await conn.execute(
            "UPDATE tess_system.tenants SET encrypted_db_url=$1 WHERE slug='beta'",
            b"invalid-encrypted-url",
        )
        failed_startup = _run(env, *command)
        _assert_ok(failed_startup, "startup with one unreadable tenant")
        assert "SERVE_COMMAND_REACHED" in failed_startup.stdout
        assert await stamp("alpha_meta") == tenant_head.revision
        assert await stamp("beta_meta") == old_revision
        assert "stored DB credentials cannot be decrypted with configured keys" in failed_startup.stderr
        assert "invalid-encrypted-url" not in failed_startup.stderr

        refused = _run(env, sys.executable, "-c", _readiness_probe_code())
        _assert_ok(refused, "credential refusal probe")
        refusal = json.loads(refused.stdout.split("READINESS ", 1)[1])
        assert {item["path"] for item in refusal} == {"pooled", "snapshot"}
        assert all(item["status"] == "refused" for item in refusal)
        assert all(
            item["detail"]["condition"] == "tenant_database_unavailable"
            for item in refusal
        )
        assert all(item["detail"]["tenant_slug"] == "beta" for item in refusal)
        assert all(
            "stored DB credentials cannot be decrypted with configured keys"
            in item["detail"]["cause"]
            for item in refusal
        )

        # Restore the actual encrypted credential. A stale schema is still
        # refused, proving credential recovery does not bypass revision safety.
        await conn.execute(
            "UPDATE tess_system.tenants SET encrypted_db_url=$1 WHERE slug='beta'",
            encrypted_target,
        )
        stale_refused = _run(env, sys.executable, "-c", _readiness_probe_code())
        _assert_ok(stale_refused, "stale revision refusal probe")
        stale = json.loads(stale_refused.stdout.split("READINESS ", 1)[1])
        assert all(item["status"] == "refused" for item in stale)
        assert all(item["detail"]["current_revision"] == old_revision for item in stale)
        assert all(item["detail"]["required_revision"] == tenant_head.revision for item in stale)
        assert all("older than the service" in item["detail"]["cause"] for item in stale)

        # Existing system-admin repair uses Alembic and the system DB; no
        # quarantine clear flag is involved. A fresh check admits beta.
        repaired = migrate("beta", "tenant@head")
        _assert_ok(repaired, "system-admin-equivalent beta repair")
        recovered = _run(env, sys.executable, "-c", _readiness_probe_code())
        _assert_ok(recovered, "post-repair readiness probe")
        ready = json.loads(recovered.stdout.split("READINESS ", 1)[1])
        assert all(item["status"] == "ready" for item in ready)
        assert await stamp("beta_meta") == tenant_head.revision
        assert await conn.fetchval(
            "SELECT is_active FROM tess_system.tenants WHERE slug='beta'"
        ) is False

        # Hold a cached pooled factory, downgrade the schema in a separate
        # process, then ask the same process again. The second lookup must not
        # trust its healthy cached factory.
        cached_code = f"""
import asyncio
import json
import os
import subprocess
import sys
from shared.db.session import TenantReadinessError, get_tenant_session_factory

root = {str(TESSALLITE)!r}
ini = {str(INI)!r}
env = dict(os.environ, MIGRATE_MODE="tenant", TENANT_SLUG="alpha")

async def main():
    first = await get_tenant_session_factory("alpha")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ini, "downgrade", {old_revision!r}],
        cwd=root, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, "cached factory fixture downgrade failed"
    try:
        await get_tenant_session_factory("alpha")
    except TenantReadinessError as exc:
        assert exc.detail["current_revision"] == {old_revision!r}
        print("CACHED_REFUSED " + json.dumps(exc.detail, sort_keys=True))
    else:
        raise AssertionError("cached tenant factory bypassed stale schema")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ini, "upgrade", "tenant@head"],
        cwd=root, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, "cached factory fixture upgrade failed"
    second = await get_tenant_session_factory("alpha")
    assert second is first
    print("CACHED_RECOVERED")

asyncio.run(main())
"""
        cached = _run(env, sys.executable, "-c", cached_code)
        _assert_ok(cached, "cached factory refusal and recovery")
        assert "CACHED_REFUSED" in cached.stdout
        assert "CACHED_RECOVERED" in cached.stdout

        # A system DB outage is an unreadable authority, so tenant acquisition
        # still returns the same typed 503 condition instead of a raw driver
        # error or a credential-shaped response.
        authority_env = {
            **env,
            "SYSTEM_DATABASE_URL": "postgresql+asyncpg://127.0.0.1:1/unavailable",
        }
        authority = _run(
            authority_env,
            sys.executable,
            "-c",
            """
import asyncio
from shared.db.session import TenantReadinessError, get_tenant_session_factory

async def main():
    try:
        await get_tenant_session_factory("alpha")
    except TenantReadinessError as exc:
        assert exc.detail["cause"] == "tenant registry could not be read"
        print("AUTHORITY_REFUSED", exc.detail["condition"])
    else:
        raise AssertionError("unreadable system authority was accepted")

asyncio.run(main())
""",
        )
        _assert_ok(authority, "unreadable authority probe")
        assert "AUTHORITY_REFUSED tenant_database_unavailable" in authority.stdout

        # Two migration runners for one schema must both complete safely. The
        # existing env.py transaction advisory lock serializes their stamp and
        # DDL work; no process-level lock is added here.
        downgrade_alpha = downgrade("alpha", old_revision)
        _assert_ok(downgrade_alpha, "alpha concurrency fixture downgrade")
        concurrent_env = {**env, "MIGRATE_MODE": "tenant", "TENANT_SLUG": "alpha"}
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "alembic", "-c", str(INI), "upgrade", "tenant@head"],
                cwd=TESSALLITE,
                env=concurrent_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=180) for process in processes]
        assert all(process.returncode == 0 for process in processes), results
        assert await stamp("alpha_meta") == tenant_head.revision
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        await admin.close()
