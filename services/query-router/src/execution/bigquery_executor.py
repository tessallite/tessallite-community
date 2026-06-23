"""
BigQuery executor.

Credentials are decrypted from project_connections.encrypted_credentials (Fernet).
Results are capped at MAX_RESULT_ROWS and returned in memory (chunked streaming
is reserved for a future streaming endpoint).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from shared.config.bootstrap import system_snapshot_get
from shared.security.credential_crypto import decrypt_json
from shared.source_executor import QueryTimeoutError
from src.ir.logical_query import ResultTooLargeError

logger = logging.getLogger(__name__)


def _decrypt(encrypted: bytes) -> dict:
    # Rotation-aware decrypt: current key first, then any previous key.
    return decrypt_json(encrypted)


class BigQueryExecutor:
    def __init__(self, project_connection: Any) -> None:
        """
        project_connection: ProjectConnection ORM object with encrypted_credentials.
        """
        from google.cloud import bigquery
        from google.oauth2 import service_account

        raw = _decrypt(project_connection.encrypted_credentials)
        sa_info = raw.get("service_account_json", raw)
        if isinstance(sa_info, str):
            sa_info = json.loads(sa_info)
        creds = service_account.Credentials.from_service_account_info(sa_info)
        self._project_id = (
            project_connection.config.get("project_id")
            or raw.get("project_id")
            or sa_info.get("project_id")
        )
        self._client = bigquery.Client(credentials=creds, project=self._project_id)

    def execute(self, sql: str) -> tuple[list[dict], int, list[str]]:
        """
        Execute sql and return (rows, bytes_processed, column_names).

        Raises ResultTooLargeError if result exceeds the result.max_rows setting.
        Raises QueryTimeoutError if the query exceeds statement timeout.
        """
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        max_rows = int(system_snapshot_get("result.max_rows"))
        statement_timeout = int(system_snapshot_get("query.statement_timeout_seconds"))
        job = self._client.query(sql)
        try:
            result = job.result(timeout=statement_timeout)
        except FuturesTimeoutError as exc:
            job.cancel()
            raise QueryTimeoutError(
                f"BigQuery query exceeded {statement_timeout}s timeout."
            ) from exc
        column_names = [field.name for field in result.schema]
        rows: list[dict] = []

        for i, row in enumerate(result):
            if i >= max_rows:
                raise ResultTooLargeError(
                    f"Result exceeds {max_rows} rows. "
                    "Add filters or raise the result.max_rows setting."
                )
            rows.append(dict(row))

        bytes_processed = job.total_bytes_processed or 0
        return rows, bytes_processed, column_names

    def close(self) -> None:
        self._client.close()
