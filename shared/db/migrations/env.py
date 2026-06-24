import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from shared.db.models import SystemBase, TenantBase

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Which schema mode are we migrating?
# MODE=system  → migrates tess_system schema (SystemBase)
# MODE=tenant  → migrates {slug}_meta schema (TenantBase), DB URL from env
#
# NOTE: this chain has two INTENTIONAL heads — one per branch label
# (`system` rooted at 0001, `tenant` rooted at 0002), kept separate so tenant
# tables never land in tess_system and vice versa. Bare `alembic upgrade head`
# is UNSUPPORTED and errors with "Multiple head revisions are present"; always
# target the mode-scoped head: `system@head` or `tenant@head` (see
# model-service/src/api/admin.py:_run_alembic and scripts/reset_acme_*_tenant.py).
# By design — do NOT author a merge revision; it would collapse both heads and
# break MIGRATE_MODE schema isolation. See Bug-3575 /
# docs/questions/questions_alembic-two-heads-merge.md.
MODE = os.environ.get("MIGRATE_MODE", "system")
TENANT_SLUG = os.environ.get("TENANT_SLUG", "")


def get_target_metadata():
    if MODE == "system":
        return SystemBase.metadata
    return TenantBase.metadata


def get_database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        from shared.config.settings import get_settings
        s = get_settings()
        url = s.SYSTEM_DATABASE_URL
    return url


def run_migrations_offline() -> None:
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=get_target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    import sqlalchemy

    if MODE == "system":
        schema = "tess_system"
    elif TENANT_SLUG:
        schema = f"{TENANT_SLUG}_meta"
    else:
        schema = "public"

    # Ensure the target schema exists before Alembic tries to use it.
    # Double-quote the identifier so slugs containing hyphens (allowed by
    # TenantCreate's slug pattern ^[a-z0-9_-]+$) survive the DDL. The
    # schema name itself is derived from env vars, not SQL literals, and
    # SystemTenant.slug is validated by the pydantic pattern upstream.
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    connection.execute(sqlalchemy.text(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema}"))
    connection.execute(sqlalchemy.text(f"SET search_path TO {quoted_schema}, public"))

    context.configure(
        connection=connection,
        target_metadata=get_target_metadata(),
        include_schemas=True,
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = get_database_url()
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
