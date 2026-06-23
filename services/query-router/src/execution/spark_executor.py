"""
Spark Thrift Server executor for the query router.

Connects to a Hadoop cluster via the HiveServer2 (Thrift) endpoint exposed
by Spark Thrift Server or Apache Hive.

Interface mirrors PostgresExecutor:
    executor.execute(sql) -> tuple[list[dict], int, list[str]]

Credential fields expected in ProjectConnection.encrypted_credentials:
    host         : str   — Thrift Server hostname
    port         : int   — default 10000
    database     : str   — Hive database (default "default")
    username     : str
    password     : str
    auth_method  : str   — NOSASL | LDAP | KERBEROS (default NOSASL)

bytes_processed is always 0 (Spark Thrift Server does not expose bytes scanned).
"""
from __future__ import annotations

import logging
from typing import Any

from shared.config.bootstrap import system_snapshot_get
from shared.security.credential_crypto import decrypt_json
from shared.source_executor import QueryTimeoutError
from src.ir.logical_query import ResultTooLargeError

logger = logging.getLogger(__name__)


class SparkExecutor:
    """Execute Hive/Spark SQL via the Spark Thrift Server (HiveServer2)."""

    def __init__(self, project_connection: Any) -> None:
        # Rotation-aware decrypt: current key first, then any previous key.
        self._creds: dict = decrypt_json(project_connection.encrypted_credentials)
        self._conn_type = project_connection.connection_type

    def _open_connection(self):
        """Open a synchronous pyhive connection."""
        from pyhive import hive  # type: ignore

        return hive.connect(
            host=self._creds["host"],
            port=int(self._creds.get("port", 10000)),
            database=self._creds.get("database", "default"),
            username=self._creds.get("username", ""),
            password=self._creds.get("password", ""),
            auth=self._creds.get("auth_method", "NOSASL"),
        )

    async def execute(self, sql: str) -> tuple[list[dict], int, list[str]]:
        """
        Execute *sql* and return (rows, bytes_processed, column_names).

        Runs the synchronous pyhive call in a thread pool to avoid blocking
        the event loop.
        """
        import asyncio

        statement_timeout = int(system_snapshot_get("query.statement_timeout_seconds"))

        def _run_sync() -> tuple[list[dict], int, list[str]]:
            conn = self._open_connection()
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
                f"Spark query exceeded {statement_timeout}s timeout."
            ) from exc
