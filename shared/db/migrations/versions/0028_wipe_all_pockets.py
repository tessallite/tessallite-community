"""Wipe all pocket data: physical tables on targets and control-plane rows.

Revision ID: 0028
Revises: 0027
Create Date: 2026-04-21

Destructive. Removes every row in ``pocket_definitions`` (CASCADE
clears ``pocket_predicates``, ``pocket_refresh_runs``, and
``pocket_refresh_policies``). ``query_logs.pocket_id`` auto-nulls via
FK ``ON DELETE SET NULL``; logs are preserved.

Before the metadata is dropped we also issue ``DROP TABLE IF EXISTS``
against each target Postgres so the materialized pocket tables do not
linger. Target failures (unreachable host, stale credentials, missing
driver) are logged and swallowed — the metadata wipe still runs.
"""
from __future__ import annotations

import asyncio
import json
import logging

from alembic import op
import sqlalchemy as sa

from cryptography.fernet import Fernet

from shared.config.settings import get_settings


revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.wipe_pockets")


def upgrade() -> None:
    conn = op.get_bind()

    pockets = conn.execute(
        sa.text(
            "SELECT p.id, p.physical_table_name, p.target_schema, "
            "p.target_id, t.project_connection_id, t.config "
            "FROM pocket_definitions p "
            "JOIN data_targets t ON t.id = p.target_id"
        )
    ).fetchall()

    for pid, table, schema, _tid, project_connection_id, target_config in pockets:
        row = conn.execute(
            sa.text(
                "SELECT connection_type, encrypted_credentials, config, project_id "
                "FROM project_connections WHERE id = :c"
            ),
            {"c": project_connection_id},
        ).fetchone()
        if row is None:
            continue
        conn_type, creds_blob, pc_config, project_id = row
        if (conn_type or "").lower() != "postgresql":
            continue
        try:
            asyncio.run(
                _drop_physical_table(
                    creds_blob=creds_blob,
                    pc_config=pc_config or {},
                    target_config=target_config or {},
                    schema=schema,
                    table=table,
                    project_id=project_id,
                )
            )
        except Exception as exc:
            logger.warning("pocket %s: physical drop failed: %s", pid, exc)

    conn.execute(sa.text("DELETE FROM pocket_definitions"))


def downgrade() -> None:
    # One-way wipe. Reverting would require restoring from a backup.
    pass


async def _drop_physical_table(
    *,
    creds_blob: bytes,
    pc_config: dict,
    target_config: dict,
    schema: str | None,
    table: str | None,
    project_id,
) -> None:
    import asyncpg

    settings = get_settings()
    fernet = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY.encode())
    creds = json.loads(fernet.decrypt(creds_blob).decode())

    target_schema = (schema or target_config.get("schema") or target_config.get("dataset") or "public").strip()
    target_table = (table or "").strip()
    if not target_table:
        return

    if "." in target_table and not schema:
        left, right = target_table.split(".", 1)
        ref = f'"{left}"."{right}"'
    else:
        ref = f'"{target_schema}"."{target_table}"'

    dsn = creds.get("dsn") or creds.get("url") or creds.get("connection_string")
    if dsn:
        pg = await asyncpg.connect(dsn=dsn)
    else:
        pg = await asyncpg.connect(
            host=creds.get("host") or pc_config.get("host"),
            port=int(creds.get("port") or pc_config.get("port") or 5432),
            database=creds.get("database") or pc_config.get("database"),
            user=creds.get("username") or creds.get("user", ""),
            password=creds.get("password", ""),
        )
    try:
        await pg.execute(f"DROP TABLE IF EXISTS {ref}")
    finally:
        await pg.close()
