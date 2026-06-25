"""
PostgreSQL / JDBC executor (asyncpg-based).

Credentials are decrypted from project_connections.encrypted_credentials (Fernet).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg
from shared.config.bootstrap import system_snapshot_get
from shared.config.source_db import resolve_source_db_endpoint
from shared.security.credential_crypto import decrypt_json
from shared.source_executor import QueryTimeoutError
from src.ir.logical_query import ResultTooLargeError

logger = logging.getLogger(__name__)


def _decrypt(encrypted: bytes) -> dict:
    # Rotation-aware decrypt: current key first, then any previous key.
    return decrypt_json(encrypted)


async def _build_connect_kwargs(
    creds: dict, config: dict, *, tenant_session=None, project_id=None,
) -> dict:
    """Build asyncpg keyword connection args from decrypted credentials.

    Falls back to the tenant-level ``source_db.fallback_*`` settings when
    creds and config both omit the host/port/database, then to the registry
    defaults when the tenant settings are also absent.

    Returns keyword args (not a DSN string) so that special characters in
    usernames (e.g. ``@`` in email addresses) are handled correctly.
    """
    if "url" in creds:
        return {"dsn": creds["url"]}
    host, port, database = await resolve_source_db_endpoint(
        creds, config,
        tenant_session=tenant_session,
        project_id=project_id,
    )
    user = creds.get("user") or creds.get("username", "postgres")
    password = creds.get("password", "")
    return dict(host=host, port=port, database=database,
                user=user, password=password)


class PostgresExecutor:
    def __init__(self, connect_kwargs: dict, *, schema: str = "") -> None:
        self._connect_kwargs = connect_kwargs
        self._schema = schema

    @classmethod
    async def create(
        cls,
        project_connection: Any,
        *,
        tenant_session=None,
    ) -> "PostgresExecutor":
        raw = _decrypt(project_connection.encrypted_credentials)
        config = project_connection.config or {}
        kwargs = await _build_connect_kwargs(
            raw,
            config,
            tenant_session=tenant_session,
            project_id=getattr(project_connection, "project_id", None),
        )
        schema = config.get("schema") or config.get("dataset") or ""
        return cls(kwargs, schema=schema)

    @staticmethod
    def _tag(sql: str) -> str:
        if sql.lstrip().startswith("/* tessallite:routed */"):
            return sql
        return f"/* tessallite:routed */ {sql}"

    async def execute(self, sql: str) -> tuple[list[dict], int, list[str]]:
        """
        Execute sql asynchronously.
        Returns (rows, bytes_processed, column_names).
        bytes_processed is 0 for PostgreSQL (no equivalent metric).
        Raises ResultTooLargeError if result exceeds the configured row cap.
        Raises QueryTimeoutError if connect or statement timeout is exceeded.
        """
        tagged = self._tag(sql)
        max_rows = int(system_snapshot_get("result.max_rows"))
        connect_timeout = int(system_snapshot_get("query.connect_timeout_seconds"))
        statement_timeout = int(system_snapshot_get("query.statement_timeout_seconds"))
        try:
            conn = await asyncpg.connect(
                **self._connect_kwargs, timeout=connect_timeout,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            raise QueryTimeoutError(
                f"Could not connect to source database within {connect_timeout}s."
            ) from exc
        try:
            stmt_timeout_ms = statement_timeout * 1000
            await conn.execute(f"SET statement_timeout = {stmt_timeout_ms}")
            if self._schema:
                safe_schema = self._schema.replace("'", "''")
                await conn.execute(
                    f"SET search_path TO '{safe_schema}', public"
                )
            records = await conn.fetch(tagged)
            if len(records) > max_rows:
                raise ResultTooLargeError(
                    f"Result exceeds {max_rows} rows. "
                    "Add filters or raise the result.max_rows setting."
                )
            raw_keys = list(records[0].keys()) if records else []
            # Bug-AGG-001: dict(r) silently loses duplicate column names
            # (e.g. SELECT MIN(x), MAX(x) both named "x").  Detect
            # duplicates and disambiguate by appending a suffix so that
            # dict conversion preserves all values.
            _seen: dict[str, int] = {}
            column_names: list[str] = []
            has_dups = False
            for name in raw_keys:
                count = _seen.get(name, 0)
                if count > 0:
                    column_names.append(f"{name}_{count}")
                    has_dups = True
                else:
                    column_names.append(name)
                _seen[name] = count + 1
            if has_dups:
                rows = [
                    {column_names[i]: r[i] for i in range(len(column_names))}
                    for r in records
                ]
            else:
                rows = [dict(r) for r in records]
            return rows, 0, column_names
        except asyncpg.QueryCanceledError as exc:
            raise QueryTimeoutError(
                f"Query cancelled by source database (statement timeout {statement_timeout}s exceeded)."
            ) from exc
        finally:
            await conn.close()

    async def execute_chunked(
        self, sql: str
    ):
        """
        Async generator that yields rows in result.chunk_size batches.
        Respects the result.max_rows cap.
        """
        tagged = self._tag(sql)
        max_rows = int(system_snapshot_get("result.max_rows"))
        chunk_size = int(system_snapshot_get("result.chunk_size"))
        connect_timeout = int(system_snapshot_get("query.connect_timeout_seconds"))
        statement_timeout = int(system_snapshot_get("query.statement_timeout_seconds"))
        try:
            conn = await asyncpg.connect(
                **self._connect_kwargs, timeout=connect_timeout,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            raise QueryTimeoutError(
                f"Could not connect to source database within {connect_timeout}s."
            ) from exc
        try:
            stmt_timeout_ms = statement_timeout * 1000
            await conn.execute(f"SET statement_timeout = {stmt_timeout_ms}")
            if self._schema:
                safe_schema = self._schema.replace("'", "''")
                await conn.execute(
                    f"SET search_path TO '{safe_schema}', public"
                )
            stmt = await conn.prepare(tagged)
            count = 0
            chunk: list[dict] = []
            async for record in stmt.cursor():
                if count >= max_rows:
                    raise ResultTooLargeError(
                        f"Result exceeds {max_rows} rows."
                    )
                chunk.append(dict(record))
                count += 1
                if len(chunk) >= chunk_size:
                    yield chunk
                    chunk = []
            if chunk:
                yield chunk
        except asyncpg.QueryCanceledError as exc:
            raise QueryTimeoutError(
                f"Query cancelled by source database (statement timeout {statement_timeout}s exceeded)."
            ) from exc
        finally:
            await conn.close()
