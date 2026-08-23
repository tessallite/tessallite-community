"""Per-column collation determinism checker for text relabel certification.

Spec: architecture_derived-grain-aggregate-routing.md I16 (text collation
certification). A text (VARCHAR/CHAR/STRING) column in a BIJECTION relabel is
certifiable ONLY when its effective GROUP BY-equality collation is DETERMINISTIC:
two distinct byte-sequences are never treated as equal by GROUP BY. A non-
deterministic collation (ICU ci/ai, NOCASE) folds distinct values (e.g. 'ABC'
and 'abc') into one group, breaking the 1:1 guarantee and producing wrong
numbers (merged partitions).

This module reads the DECLARED per-column collation from the source/target
catalog and checks its determinism property. This is provably correct and
complete for every folding axis (case, accent, NFC/NFD, ligature, etc.)
because the database itself declares whether it treats distinct byte-strings as
equal:

  - PostgreSQL (12+): ``pg_collation.collisdeterministic = true`` means the
    collation never folds. The column's effective collation OID is read from
    ``pg_attribute.attcollation`` (nonzero for text/collatable types, pointing
    at the column's collation whether default or overridden). The determinism
    flag is resolved via ``pg_collation.collisdeterministic``. A text column
    with ``attcollation = 0`` is unexpected (non-collatable type) and fails
    closed.

  - BigQuery: STRING comparison is binary (case-sensitive) by default. Dataset-
    or column-level ``und:ci`` collation is case-insensitive and non-certifiable.
    Fail closed for BigQuery (cannot read per-column collation via SQL catalog
    without BigQuery-specific metadata APIs).

  - Other connectors (Snowflake, SQL Server, Spark, Redshift): fail closed
    (cannot determine per-column collation determinism). This means text relabels
    on those connectors stay source-only -- safe, not wrong.

Design constraints honoured:
  - The catalog query is executed through ``shared/source_executor`` (gateway
    boundary);
  - the query is a read-only metadata lookup against the system catalog;
  - no per-connector branches in SQL GENERATION (the user-data query path);
    the catalog lookup is infrastructure metadata dispatch, matching the
    existing pattern in ``source_introspection.py``.

Fail-closed: any execution error, timeout, unrecognised connector, or unknown
collation returns False (collation NOT proven deterministic -> text
certification refused -> source-only fallback).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PostgreSQL: per-column collation determinism via pg_attribute + pg_collation
# ---------------------------------------------------------------------------

def _build_pg_collation_check_sql(
    schema: str, table: str, column: str, connector: str,
) -> str:
    """Build a PostgreSQL catalog query that returns whether a column's
    effective collation is deterministic.

    Returns exactly ONE row with ``is_deterministic`` = true/false when the
    column exists and has a resolvable collation. Returns ZERO rows when the
    column does not exist, the schema/table is wrong, or the collation cannot
    be resolved -- the caller treats zero rows as FAIL-CLOSED (not certified).

    Logic: join ``pg_attribute`` -> ``pg_collation`` on ``attcollation``.
    A text (collatable) column in PostgreSQL ALWAYS has ``attcollation > 0``
    pointing at its effective collation OID (either the database default or an
    explicit COLLATE override). ``attcollation = 0`` means the column's TYPE
    is non-collatable (e.g. integer, date) -- unexpected for a text column we
    are trying to certify. The query returns ``false`` for ``attcollation = 0``
    (fail-closed: do not certify an unexpectedly non-collatable column).

    For ``attcollation > 0``, the query joins ``pg_collation`` and reads
    ``collisdeterministic`` (PG 12+). On PG < 12 the column does not exist
    in ``pg_collation``, so the query errors -> fail-closed (the exception
    handler returns False).
    """
    # Escape single quotes in schema/table/column to prevent SQL injection
    # from crafted physical names (defense in depth; these come from the
    # model's governed metadata, not user input).
    s = schema.replace("'", "''")
    t = table.replace("'", "''")
    c = column.replace("'", "''")
    return (
        "SELECT CASE "
        "  WHEN a.attcollation = 0 THEN false "
        "  ELSE COALESCE(col.collisdeterministic, false) "
        "END AS is_deterministic "
        "FROM pg_catalog.pg_attribute a "
        "JOIN pg_catalog.pg_class r ON r.oid = a.attrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = r.relnamespace "
        "LEFT JOIN pg_catalog.pg_collation col ON col.oid = a.attcollation "
        f"WHERE n.nspname = '{s}' "
        f"AND r.relname = '{t}' "
        f"AND a.attname = '{c}' "
        "AND a.attnum > 0 "
        "AND NOT a.attisdropped "
        "LIMIT 1"
    )


async def _check_pg_column_collation_deterministic(
    *,
    conn_obj: Any,
    schema: str,
    table: str,
    column: str,
    connector: str,
    tenant_session: Any = None,
) -> bool:
    """Check if a PostgreSQL column's effective collation is deterministic.

    Returns True when the column exists AND its collation is deterministic.
    Returns False when: the column does not exist (zero rows -> fail-closed),
    the collation is non-deterministic, or on any error (fail-closed).
    """
    from shared.source_executor import execute_source_sql, QueryTimeoutError

    sql = _build_pg_collation_check_sql(schema, table, column, connector)
    try:
        rows, _cols = await execute_source_sql(
            conn_obj, sql, tenant_session=tenant_session,
        )
        # Zero rows -> column not found -> fail closed (not certified).
        # One row with is_deterministic=true -> deterministic (certified).
        # One row with is_deterministic=false -> non-deterministic (refused).
        if not rows:
            return False
        return bool(rows[0].get("is_deterministic", False))
    except QueryTimeoutError:
        logger.warning(
            "pg collation check timed out for %s.%s.%s (fail-closed)",
            schema, table, column,
        )
        return False
    except Exception:
        logger.warning(
            "pg collation check failed for %s.%s.%s (fail-closed)",
            schema, table, column, exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Public API: per-column collation determinism check
# ---------------------------------------------------------------------------


async def check_column_collation_deterministic(
    *,
    conn_obj: Any,
    connector: str,
    schema: str,
    table: str,
    column: str,
    tenant_session: Any = None,
) -> bool:
    """Check whether a specific column's effective collation is deterministic.

    Returns True ONLY when the column's collation is provably deterministic
    (distinct byte-strings stay distinct under GROUP BY equality). Returns
    False when the collation is non-deterministic, unknown, or when the
    connector does not support per-column collation introspection.

    Connector support:
      - postgresql / redshift: reads ``pg_collation.collisdeterministic``
        for the column's effective collation (PG 12+).
      - bigquery / snowflake / hadoop_spark / sqlserver / unknown: fail
        closed (returns False). Text relabels on these connectors stay
        source-only.
    """
    c = (connector or "").lower()
    if c in ("postgresql", "redshift"):
        return await _check_pg_column_collation_deterministic(
            conn_obj=conn_obj, schema=schema, table=table, column=column,
            connector=connector, tenant_session=tenant_session,
        )
    # All other connectors: fail closed. Cannot prove collation determinism
    # without connector-specific catalog queries. Text columns on these
    # connectors stay source-only -- safe (never wrong numbers), just
    # un-optimised.
    return False


async def check_columns_collation_deterministic(
    *,
    conn_obj: Any,
    connector: str,
    table_ref: str,
    columns: list[str],
    tenant_session: Any = None,
) -> bool:
    """Check that ALL listed columns have deterministic collation.

    ``table_ref`` is a dotted ``schema.table`` reference. Returns True only
    when every column's collation is provably deterministic. Any failure or
    unknown -> False (fail-closed).
    """
    if not columns:
        return False
    parts = table_ref.split(".", 1)
    if len(parts) != 2:
        # Cannot determine schema.table -> fail closed.
        return False
    schema, table = parts[0], parts[1]
    for col in columns:
        if not await check_column_collation_deterministic(
            conn_obj=conn_obj, connector=connector,
            schema=schema, table=table, column=col,
            tenant_session=tenant_session,
        ):
            return False
    return True
