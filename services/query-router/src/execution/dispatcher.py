"""Centralised connector dispatch for query execution.

Bug-905: executor dispatch was previously duplicated inline in routes.py.

F-014-02 / Bug-7984: routed user-query physical I/O now flows through the SINGLE
public shared execution gateway, ``shared.source_executor.execute_routed_query``.
The query-router no longer opens its own connector driver, decrypts credentials,
or imports private ``shared.source_executor`` helpers. Any safety, cancellation,
credential, cost, or audit control added in the shared executor therefore covers
the primary user-query path — not just background/control-plane callers.

Return contract (unchanged): ``(rows, bytes_processed, columns)``
  - rows:            list of dicts keyed by column name
  - bytes_processed: integer bytes billed (meaningful only for BigQuery;
                     zero for all other connectors)
  - columns:         list of column name strings in result order

The ``result.max_rows`` cap and duplicate-column disambiguation (Bug-AGG-001)
that used to live in the per-connector query-router executors are now enforced
inside the shared gateway; the shared ``SourceResultTooLargeError`` is translated
here into the query-router ``ResultTooLargeError`` so the HTTP layer's existing
handling is unchanged.

F-027-11: the routed-SQL audit marker is applied centrally so EVERY dialect
executor emits it. A leading ``/* ... */`` block comment is standard SQL accepted
by PostgreSQL/Redshift, BigQuery, Snowflake, Spark SQL and SQL Server, so the
marker is dialect-neutral and never alters the parsed statement.
"""
from __future__ import annotations

from typing import Any

from shared.config.bootstrap import system_snapshot_get
from shared.source_executor import (
    SourceResultTooLargeError,
    execute_routed_query,
)
from src.ir.logical_query import ResultTooLargeError

_ROUTED_MARKER = "/* tessallite:routed */"


def _tag_routed(sql: str) -> str:
    """Prefix ``sql`` with the routed-SQL audit marker (idempotent)."""
    if sql.lstrip().startswith(_ROUTED_MARKER):
        return sql
    return f"{_ROUTED_MARKER} {sql}"


async def execute_on_connection(
    sql: str,
    conn: Any,  # ProjectConnection ORM object
    db: Any,    # AsyncSession — required for PostgreSQL host resolution
) -> tuple[list[dict], int, list[str]]:
    """Dispatch SQL execution to the shared execution gateway.

    Parameters
    ----------
    sql:
        Fully-rewritten SQL ready to execute against the source database.
    conn:
        ``ProjectConnection`` ORM object carrying ``connection_type`` and
        encrypted credentials.
    db:
        Async SQLAlchemy session used by the PostgreSQL path for fallback host
        resolution.  Ignored by non-PostgreSQL connectors.

    Returns
    -------
    tuple[list[dict], int, list[str]]
        ``(rows, bytes_processed, column_names)``.
    """
    tagged = _tag_routed(sql)
    max_rows = int(system_snapshot_get("result.max_rows"))
    try:
        return await execute_routed_query(
            conn, tagged, tenant_session=db, max_rows=max_rows,
        )
    except SourceResultTooLargeError as exc:
        # Preserve the query-router HTTP contract: the routes layer catches
        # ResultTooLargeError specifically.
        raise ResultTooLargeError(str(exc)) from exc
