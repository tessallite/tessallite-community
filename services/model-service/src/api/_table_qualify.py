"""Connector-aware physical-table-name qualification for model-service.

Resolves a stored ``physical_name`` to a fully-qualified, *unquoted* dotted
table reference appropriate for the source connector, applying the source's
default schema/dataset (and, for BigQuery, the GCP project ID). Quoting is the
caller's responsibility — use :func:`shared.connector_qualify.quote_table_ref`
on the result.

This mirrors the resolution contract documented in the Sources panel help
("If blank, the full table path must be used in physical_name (schema.table)"):

- A ``physical_name`` that already contains a ``.`` is treated as fully
  qualified for non-BigQuery connectors and passed through unchanged.
- For BigQuery, a two-part ``dataset.table`` name gets the project prepended
  (when known); a three-part ``project.dataset.table`` name is left alone.
- A bare name gets the resolved schema/dataset (and BQ project) applied.

Status: active. Created for Bug-5470 (table-preview BigQuery qualification).
Last meaningful update: 2026-06-24.

Consolidated from Bug-5480: ``src/api/calendar.py`` previously carried an
equivalent private ``_qualify_table_name`` which has been replaced with a
direct import of this module's ``qualify_physical_name``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from shared.connector_qualify import qualify_table_name as _shared_qualify

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shared.db.models import DataSource, ProjectConnection


def _decrypt_credentials(enc: bytes) -> dict:
    """Rotation-aware decrypt: current key first, then any previous key."""
    from shared.security.credential_crypto import decrypt_json

    return decrypt_json(enc)


def qualify_physical_name(
    physical_name: str,
    connection: "ProjectConnection",
    source: "DataSource | None" = None,
) -> str:
    """Return the fully-qualified, **unquoted** dotted table reference.

    Parameters
    ----------
    physical_name:
        The stored physical table name. May be bare (``table``), schema/dataset
        qualified (``schema.table``), or fully qualified
        (``project.dataset.table`` for BigQuery).
    connection:
        The owning :class:`ProjectConnection` (carries connection type, config,
        and encrypted credentials).
    source:
        The owning :class:`DataSource`, when available (carries the
        source-level config and ``default_schema``).

    Returns
    -------
    str
        The dotted, unquoted table reference. Quote it for the target dialect
        via :func:`shared.connector_qualify.quote_table_ref`.
    """
    conn_cfg = (connection.config if connection else None) or {}
    src_cfg = (source.config if source else None) or {}
    conn_type = ((connection.connection_type if connection else "") or "").lower()

    parts = physical_name.split(".")

    # Non-BigQuery: an already-dotted name is treated as fully qualified
    # (schema.table) and passed through unchanged — preserves existing
    # PostgreSQL preview behaviour exactly.
    if conn_type != "bigquery" and len(parts) > 1:
        return physical_name

    schema = (
        src_cfg.get("dataset")
        or src_cfg.get("schema")
        or conn_cfg.get("dataset")
        or conn_cfg.get("schema")
        or (source.default_schema if source else None)
    )

    project_id: str | None = None
    if conn_type == "bigquery":
        creds: dict = {}
        if connection is not None and connection.encrypted_credentials:
            try:
                creds = _decrypt_credentials(bytes(connection.encrypted_credentials))
            except Exception:
                creds = {}
        project_id = creds.get("project_id") or conn_cfg.get("project_id")

        # Already fully qualified (project.dataset.table) — leave alone.
        if len(parts) >= 3:
            return physical_name
        # dataset.table — prepend the project when known.
        if len(parts) == 2:
            return f"{project_id}.{physical_name}" if project_id else physical_name

    return _shared_qualify(
        conn_type, physical_name, schema=schema, project_id=project_id,
    )
