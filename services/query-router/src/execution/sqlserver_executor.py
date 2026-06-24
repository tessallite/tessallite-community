"""SQL Server executor for the query router.

Connects via aioodbc (async wrapper over pyodbc/ODBC).

Interface mirrors PostgresExecutor:
    executor.execute(sql) -> tuple[list[dict], int, list[str]]

Credential fields expected in ProjectConnection.encrypted_credentials:
    host     : str
    port     : int   (default 1433)
    database : str
    username : str
    password : str
    driver   : str   (optional, default "ODBC Driver 17 for SQL Server")

bytes_processed is always 0 (ODBC does not expose this).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.config.bootstrap import system_snapshot_get
from shared.security.credential_crypto import decrypt_json
from shared.source_executor import QueryTimeoutError, _build_sqlserver_dsn
from src.ir.logical_query import ResultTooLargeError

logger = logging.getLogger(__name__)


class SqlServerExecutor:
    """Execute SQL on SQL Server via aioodbc."""

    def __init__(self, project_connection: Any) -> None:
        # Rotation-aware decrypt: current key first, then any previous key.
        self._creds: dict = decrypt_json(project_connection.encrypted_credentials)
        self._config: dict = project_connection.config or {}
        self._dsn = _build_sqlserver_dsn(self._creds, self._config)

    async def execute(self, sql: str) -> tuple[list[dict], int, list[str]]:
        import aioodbc

        statement_timeout = int(
            system_snapshot_get("query.statement_timeout_seconds")
        )
        max_rows = int(system_snapshot_get("result.max_rows"))

        try:
            conn = await asyncio.wait_for(
                aioodbc.connect(dsn=self._dsn), timeout=30
            )
        except asyncio.TimeoutError as exc:
            raise QueryTimeoutError(
                "SQL Server connection timed out after 30s."
            ) from exc

        try:
            cursor = await conn.cursor()
            try:
                await asyncio.wait_for(
                    cursor.execute(sql), timeout=statement_timeout
                )
            except asyncio.TimeoutError as exc:
                raise QueryTimeoutError(
                    f"SQL Server query exceeded {statement_timeout}s timeout."
                ) from exc

            column_names = [
                desc[0] for desc in (cursor.description or [])
            ]

            rows: list[dict] = []
            if cursor.description:
                raw_rows = await cursor.fetchmany(max_rows + 1)
                if len(raw_rows) > max_rows:
                    raise ResultTooLargeError(
                        f"Result exceeds {max_rows:,} rows"
                    )
                for raw in raw_rows:
                    rows.append(dict(zip(column_names, raw)))

            await cursor.close()
            return rows, 0, column_names
        finally:
            await conn.close()
