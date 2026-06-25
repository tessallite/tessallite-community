"""
Snowflake executor for the query router.

Connects to a Snowflake account via the snowflake-connector-python DB-API 2.0
driver.

Interface mirrors PostgresExecutor:
    executor.execute(sql) -> tuple[list[dict], int, list[str]]

Credential fields expected in ProjectConnection.encrypted_credentials:
    account      : str   — Snowflake account identifier
    username     : str
    password     : str
    database     : str
    schema       : str
    warehouse    : str
    role         : str

bytes_processed is always 0 (not exposed by the DB-API cursor).
"""
from __future__ import annotations

import logging
from typing import Any

from shared.config.bootstrap import system_snapshot_get
from shared.security.credential_crypto import decrypt_json
from shared.source_executor import QueryTimeoutError, _open_snowflake
from src.ir.logical_query import ResultTooLargeError

logger = logging.getLogger(__name__)


class SnowflakeExecutor:
    """Execute SQL on Snowflake via the snowflake-connector-python driver."""

    def __init__(self, project_connection: Any) -> None:
        # Rotation-aware decrypt: current key first, then any previous key.
        self._creds: dict = decrypt_json(project_connection.encrypted_credentials)
        self._config: dict = project_connection.config or {}

    async def execute(self, sql: str) -> tuple[list[dict], int, list[str]]:
        import asyncio

        statement_timeout = int(system_snapshot_get("query.statement_timeout_seconds"))
        connect_timeout = int(system_snapshot_get("query.connect_timeout_seconds"))

        def _run_sync() -> tuple[list[dict], int, list[str]]:
            conn = _open_snowflake(
                self._creds, self._config,
                login_timeout=connect_timeout,
                network_timeout=statement_timeout,
            )
            try:
                cursor = conn.cursor()
                cursor.execute(sql)
                column_names = [desc[0] for desc in (cursor.description or [])]

                max_rows = int(system_snapshot_get("result.max_rows"))
                rows: list[dict] = []
                if cursor.description:
                    raw_rows = cursor.fetchmany(max_rows + 1)
                    if len(raw_rows) > max_rows:
                        raise ResultTooLargeError(
                            f"Result exceeds {max_rows:,} rows"
                        )
                    for raw in raw_rows:
                        rows.append(dict(zip(column_names, raw)))

                return rows, 0, column_names
            finally:
                conn.close()

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_run_sync),
                timeout=statement_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise QueryTimeoutError(
                f"Snowflake query exceeded {statement_timeout}s timeout."
            ) from exc
