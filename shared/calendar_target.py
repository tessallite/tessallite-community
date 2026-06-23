"""Ensure a shared calendar table exists on the aggregate target database.

Calendar tables are seed data — created once, never refreshed. Uses
``emit_calendar_ddl`` from ``calendar_dialects`` to generate the DDL for
the target's dialect, then executes it via ``execute_source_ddl``.

Naming convention: ``tess_cal_{calendar_type}_{fiscal_year_start_month}``
(e.g. ``tess_cal_standard_1``, ``tess_cal_fiscal_4``). One shared table
per (calendar_type, fiscal_start) combo on the target.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from shared.connector_qualify import quote_table_ref
from shared.schemas.connection_type import normalize_connection_type
from shared.semantic.calendar_dialects import CALENDAR_DIALECTS, emit_calendar_ddl
from shared.source_executor import execute_source_ddl, execute_source_sql

logger = logging.getLogger(__name__)

_DEFAULT_START = date(2015, 1, 1)
_DEFAULT_END_OFFSET_YEARS = 5


def target_calendar_table_name(calendar_type: str, fiscal_year_start_month: int) -> str:
    return f"tess_cal_{calendar_type}_{fiscal_year_start_month}"


def _normalise_target_dialect(conn_type: str) -> str:
    ct = normalize_connection_type(conn_type)
    if ct in CALENDAR_DIALECTS:
        return ct
    if ct == "jdbc":
        return "hadoop_spark"
    return "postgresql"


async def ensure_target_calendar_table(
    target_conn: Any,
    target_schema: str,
    calendar_type: str,
    fiscal_year_start_month: int = 1,
    *,
    bq_project: str = "",
    tenant_session: Any = None,
) -> str:
    """Create a calendar table on the target if it does not already exist.

    Returns the qualified target-side table name (e.g.
    ``"acme_aggregates"."tess_cal_standard_1"``).
    """
    base_name = target_calendar_table_name(calendar_type, fiscal_year_start_month)
    conn_type = (target_conn.connection_type or "").lower()
    dialect = _normalise_target_dialect(conn_type)

    if dialect == "bigquery" and bq_project:
        qualified = f"{bq_project}.{target_schema}.{base_name}"
    else:
        qualified = f"{target_schema}.{base_name}"

    quoted = quote_table_ref(dialect, qualified)
    try:
        rows, _ = await execute_source_sql(
            target_conn,
            f"SELECT 1 AS chk FROM {quoted} LIMIT 1",
            tenant_session=tenant_session,
        )
        if rows:
            logger.debug("Target calendar table %s already exists", qualified)
            return qualified
    except Exception:
        pass

    end_date = date.today() + timedelta(days=365 * _DEFAULT_END_OFFSET_YEARS)
    ddl = emit_calendar_ddl(
        dialect,
        qualified,
        _DEFAULT_START,
        end_date,
        fiscal_year_start_month=fiscal_year_start_month,
        calendar_type=calendar_type,
    )
    logger.info("Creating target calendar table %s (type=%s, fiscal=%d)",
                qualified, calendar_type, fiscal_year_start_month)
    await execute_source_ddl(target_conn, ddl, tenant_session=tenant_session)
    return qualified
