"""Canonical connection_type normalisation.

Phase C of the first external code review (CR-003) unified the Spark/Hive
connection label from ``jdbc`` to ``hadoop_spark``. Write-path validators in
``pydantic_models.ConnectionCreate`` reject ``jdbc`` outright; legacy rows
still stored on disk must be normalised at every dispatch site before the
code branches on the value.

The first remediation pass only normalised the test-connection path in
``services/model-service/src/api/connections.py``. Three discovery paths
(discover-tables, discover-columns, profile) still branched on the literal
``"jdbc"`` as PostgreSQL, which broke legacy Spark/Hive connections at
introspection time. See CR-002 Finding 4.

This module is the single source of truth. Every site that dispatches on
``connection_type`` imports ``normalize_connection_type`` and calls it
before branching. New writes go through ``ConnectionCreate`` which is the
only code path allowed to reject the legacy alias.
"""
from __future__ import annotations

ALLOWED_CONNECTION_TYPES: tuple[str, ...] = (
    "bigquery",
    "postgresql",
    "hadoop_spark",
    "redshift",
    "snowflake",
    "sqlserver",
)

_LEGACY_ALIASES: dict[str, str] = {
    # Historical label for Spark/Hive Thrift connections. Collapsed to the
    # canonical ``hadoop_spark`` value by every dispatch site.
    "jdbc": "hadoop_spark",
}


def normalize_connection_type(raw: str | None) -> str | None:
    """Return the canonical connection type, collapsing legacy aliases.

    Passes through unknown values unchanged so the caller can still
    produce a useful error message (``Unsupported connection type: X``).
    Accepts None and returns None for the common "maybe set, maybe not"
    pattern.
    """
    if raw is None:
        return None
    return _LEGACY_ALIASES.get(raw, raw)


def is_allowed_canonical(value: str | None) -> bool:
    """True if ``value`` is a canonical type (after normalisation)."""
    if value is None:
        return False
    return normalize_connection_type(value) in ALLOWED_CONNECTION_TYPES
