"""Pocket row-manifest producer (Bug-8393).

Writes ``PocketDefinition.row_manifest`` + ``active_refresh_run_id`` for EVERY
completed pocket refresh, so the query-router's RLS-safe pocket gate (Bug-8018,
``routing/router.py::_pocket_materialised_columns``) can prove that every
row-security column is a materialised output column of the pocket table.

Why this lives here and not in ``advance_artifact_manifest``
-----------------------------------------------------------
The previous (and only) writer of a pocket ``row_manifest`` was the POCKET branch
of ``shared/semantic/attribute_relationship_deploy_verify.advance_artifact_manifest``.
That function's contract is *attribute-relationship (BIJECTION) verification*: it
returns early at ``if not rels:`` and earns its trust pointer only when every
declared edge verified. A pocket over a model with no enabled BIJECTION
relationship therefore never reached the manifest write at all, and one that did
never carried ``columns``. Row-population truth is not a property of attribute
relationships, so the producer belongs on the pocket refresh chokepoint, which
runs for every pocket regardless of the model's relationship declarations.

Contract (consumed by the query-router, do not weaken without changing it too)
-----------------------------------------------------------------------------
* ``columns`` carries ONE descriptor per materialised OUTPUT column, in ordinal
  order, with BOTH ``logical_name`` and ``physical_column`` set to the identifier
  the pocket TABLE exposes VERBATIM. The consumer resolves the exposed name as
  ``logical_name or physical_column`` and matches security dimension columns
  EXACTLY (case-sensitive), so a paraphrased or case-folded name would make the
  gate fall back to source (or, if it named an absent column, fail closed at the
  database).
* The names are read back from the TARGET CATALOGUE of the table that was just
  built — never parsed out of the materialisation SELECT and never inferred from
  the model shape. A pocket is a branch-dependent visible projection (semantic
  aliases on a persona-star build, physical names on a plain-star build) built by
  one of four drivers (same-DB CTAS, BigQuery CREATE OR REPLACE, cross-database
  staging swap, incremental DELETE/INSERT); the catalogue is the only
  branch-independent record of what the table actually exposes.
* ``build_refresh_run_id`` is the run whose rows the manifest describes, and
  ``active_refresh_run_id`` is set to the SAME run in the same transaction. The
  consumer trusts ``columns`` only while the two match and
  ``manifest_version == artifact_manifest.MANIFEST_VERSION``.
* Fail closed: any error resolving the columns clears the MANIFEST, so the pocket
  falls back to source under RLS rather than serving on an unproven description of
  its own contents. The liveness POINTER still advances to the completed run —
  see ``clear_pocket_row_manifest`` and the generation-stamp invariant below.

Generation-stamp invariant (Bug-8392)
-------------------------------------
Because the pointer is now advanced on EVERY completed refresh — including one
whose manifest could not be proved, and regardless of whether any attribute edge
verified — ``(status, active_refresh_run_id)`` is a sound generation stamp for
the pocket's physical table: ``refresh_pocket_definition`` commits
``status="invalidating"`` before any physical mutation and only returns to
``status="fresh"`` with a NEW run id. The query-router's
``routing/pocket_generation_guard`` relies on that. ANY change that lets a
completed refresh leave the pointer unchanged — including "clear it when the
manifest write fails" — silently blinds that guard.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _ConnectionConfigOverlay:
    """Delegate a connection row while replacing only its config mapping."""

    def __init__(self, connection: Any, config: dict[str, Any]) -> None:
        self._connection = connection
        self.config = config

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def resolve_build_bq_project(target: Any, target_conn: Any) -> str | None:
    """The GCP project the BigQuery build actually wrote its table into.

    Bug-8790: the connection's own project (config/creds/service-account) is
    the authoritative BigQuery project. The DataTarget's ``config.project_id``
    override is removed; builds, catalogue read-back, and serving all resolve
    against the same connection project.

    Returns ``None`` for a non-BigQuery target or an unreadable/ADC-only
    BigQuery connection. Callers must treat ``None`` as unprovable rather than
    silently falling back to the process default project.
    """
    from shared.schemas.connection_type import normalize_connection_type

    connector = normalize_connection_type(
        str(getattr(target_conn, "connection_type", "") or "").lower()
    )
    if connector != "bigquery":
        return None

    try:
        from shared.config.source_db import (
            BigQueryProjectResolutionError,
            resolve_connection_bq_project,
        )

        return resolve_connection_bq_project(target_conn)
    except (BigQueryProjectResolutionError, TypeError, ValueError):
        logger.warning(
            "Could not resolve the BigQuery project for connection %s; "
            "build/serve project guard will fail closed",
            getattr(target_conn, "id", None),
            exc_info=True,
        )
        return None


def build_project_is_addressable_by_the_serving_path(
    target: Any, target_conn: Any,
) -> bool:
    """True when the BigQuery build and serve paths address the same project.

    Bug-8790: the connection's own project is the SINGLE authoritative project
    for both build and serve. This function's old cross-project guard is
    retired - the one-project rule makes the mismatch structurally impossible
    because the DataTarget no longer carries an independent project_id.

    The function remains to give callers a single "is this pocket's project
    resolvable" check point: non-BigQuery paths are always addressable; a
    BigQuery path is addressable only with an explicit project resolved from
    the connection. An ADC-only or unreadable stored connection is not safe to
    prove, and is refused before its target catalogue is read.
    """
    from shared.schemas.connection_type import normalize_connection_type

    connector = normalize_connection_type(
        str(getattr(target_conn, "connection_type", "") or "").lower()
    )
    if connector != "bigquery":
        return True
    return resolve_build_bq_project(target, target_conn) is not None


async def resolve_materialised_columns(
    target_conn: Any,
    *,
    schema: str | None,
    table: str,
    tenant_session: Any = None,
    system_session: Any = None,
) -> list[dict[str, Any]]:
    """Read the pocket table's real output columns from the target catalogue.

    Returns ordered descriptors ``{ordinal, logical_name, physical_column,
    output_type, nullable, manifest_version}``. Raises when the table cannot be
    described; returns ``[]`` when the catalogue reports no such table (the
    caller treats both as fail-closed).
    """
    from shared.artifact_target_binding import resolve_connection_source_db_endpoint
    from shared.semantic.artifact_manifest import MaterializedRowColumn
    from shared.source_introspection import discover_columns

    # ``discover_columns`` historically accepted only the tenant session. Give
    # it a complete effective endpoint so its catalogue read reaches the same
    # persisted system-fallback location as materialisation and execution.
    endpoint = await resolve_connection_source_db_endpoint(
        target_conn,
        tenant_session=tenant_session,
        system_session=system_session,
    )
    catalogue_conn = target_conn
    if endpoint is not None:
        effective_config = dict(getattr(target_conn, "config", None) or {})
        for key, value in endpoint.items():
            if not effective_config.get(key):
                effective_config[key] = value
        catalogue_conn = _ConnectionConfigOverlay(target_conn, effective_config)

    raw = await discover_columns(
        catalogue_conn,
        schema=schema or "",
        table=table,
        tenant_session=tenant_session,
        # Bug-8795: the manifest records ordinal/name/type/nullability only —
        # ``MaterializedRowColumn`` has no key field and nothing downstream reads
        # one — so the second catalogue round trip the key probe costs (a real
        # billed ``client.query()`` against INFORMATION_SCHEMA on BigQuery, once
        # per pocket refresh) buys nothing and adds a failure surface.
        with_primary_keys=False,
    )
    descriptors: list[dict[str, Any]] = []
    for ordinal, col in enumerate(raw or []):
        name = str((col or {}).get("column_name") or "")
        if not name:
            # A nameless column means the catalogue read is not trustworthy as a
            # complete description of the table -> fail closed (no manifest).
            raise ValueError(
                "Pocket column introspection returned an unnamed column; "
                "refusing to write an incomplete row manifest"
            )
        descriptors.append(
            MaterializedRowColumn(
                ordinal=ordinal,
                # BOTH fields carry the VERBATIM exposed identifier: the consumer
                # resolves `logical_name or physical_column` and compares it
                # case-sensitively against the security dimension column.
                logical_name=name,
                physical_column=name,
                output_type=(col.get("data_type") or None),
                nullable=bool(col.get("is_nullable", True)),
            ).to_dict()
        )
    return descriptors


def clear_pocket_row_manifest(pocket: Any, *, generation_run_id: Any = None) -> None:
    """Fail-closed reset: the manifest no longer describes the physical table.

    Dropping the manifest is what makes the query-router's RLS gate fall back to
    source instead of proving column coverage from a description of a table that
    may no longer exist.

    ``generation_run_id`` decides what happens to the LIVENESS POINTER, and the
    two cases are not interchangeable:

    * ``None`` (a FAILED refresh) — clear the pointer too. The pocket is left
      ``status="failed"``, so it cannot be served at all and no generation stamp
      is needed.
    * a run id (a COMPLETED refresh whose manifest could not be proved) — the
      pocket returns to ``status="fresh"`` and IS servable on non-RLS routes, so
      the pointer MUST advance to this run. Invariant I2 of the query-router's
      generation guard is "returning to fresh always carries a NEW
      ``active_refresh_run_id``"; leaving it NULL here would make two
      consecutive manifest-write failures produce the IDENTICAL stamp
      ``(fresh, None, ...)``, and a refresh landing inside the guard's window
      would then be undetectable — the pocket would serve a different row
      population than the one the matcher's containment proof admitted. That is
      not hypothetical: a BigQuery target naming a different project than its
      connection is left in this state on EVERY refresh, deliberately and
      permanently, until Bug-8790 makes the serving path carry the project —
      see ``build_project_is_addressable_by_the_serving_path``.
    """
    try:
        pocket.row_manifest = None
        pocket.active_refresh_run_id = generation_run_id
    except Exception:  # pragma: no cover - defensive, never break the refresh
        logger.warning(
            "Could not clear row manifest for pocket %s",
            getattr(pocket, "id", None), exc_info=True,
        )


async def write_pocket_row_manifest(
    *,
    pocket: Any,
    run_id: Any,
    target_conn: Any,
    target_schema: str | None,
    target_table: str,
    target: Any = None,
    deployed_version_id: Any = None,
    tenant_session: Any = None,
    system_session: Any = None,
    # Bug-8816: the ALREADY-CAPTURED, already-proven target build binding.
    # When provided, it is recorded verbatim rather than re-derived from live
    # control-plane state, so the manifest carries the binding the BUILD proved,
    # not whatever the connection/target resolver produces at manifest-write
    # time (which may differ if a re-point landed mid-build).  When None the
    # legacy re-derivation path is taken.
    target_binding_dict: dict[str, str] | None = None,
    # Bug-8780: the ALREADY-CAPTURED source build binding.  Recorded verbatim
    # so the query-router's serve-time guard can detect a source re-point
    # (``source_db.fallback_*`` changes, DataSource pointer swap) and refuse the
    # pocket rather than serving rows from the old database.
    source_binding_dict: dict[str, str] | None = None,
) -> bool:
    """Write the pocket's row manifest + liveness pointer for ``run_id``.

    ``target`` is the ``DataTarget`` the build wrote through; together with
    ``target_conn`` it pins the routing identity of the storage (Bug-8473).
    Passing it is required for the pocket to be servable under row-level
    security: without it the manifest cannot record which database its columns
    describe, and the query-router's guard refuses an unpinned manifest.

    Returns True when the manifest was written, False when it was cleared
    fail-closed. Never raises: a manifest failure must not fail an otherwise
    successful refresh, it must only cost the pocket its RLS-serving proof.
    """
    from shared.artifact_target_binding import resolve_target_binding_dict
    from shared.semantic.artifact_manifest import (
        RowManifest,
        compute_row_manifest_hash,
    )

    if system_session is None:
        try:
            from shared.db.session import SystemSessionLocal

            async with SystemSessionLocal() as sys_db:
                return await write_pocket_row_manifest(
                    pocket=pocket,
                    run_id=run_id,
                    target_conn=target_conn,
                    target_schema=target_schema,
                    target_table=target_table,
                    target=target,
                    deployed_version_id=deployed_version_id,
                    tenant_session=tenant_session,
                    system_session=sys_db,
                    target_binding_dict=target_binding_dict,
                    source_binding_dict=source_binding_dict,
                )
        except Exception:
            logger.warning(
                "Pocket row manifest not written for pocket %s (run %s); the "
                "system-scoped endpoint could not be resolved",
                getattr(pocket, "id", None), run_id, exc_info=True,
            )
            clear_pocket_row_manifest(pocket, generation_run_id=run_id)
            return False

    try:
        # Bug-8452/Bug-8790: refuse BEFORE reading anything. A build the serving
        # path cannot address must not get a manifest, because the manifest is
        # what ADMITS the pocket under row security. See
        # ``build_project_is_addressable_by_the_serving_path`` for why proving
        # the columns in the build's project would be the dangerous fix.
        if not build_project_is_addressable_by_the_serving_path(
            target, target_conn,
        ):
            raise ValueError(
                "Bug-8790: this BigQuery build wrote to a project the serving "
                "path cannot name (the pocket FROM clause carries no project, "
                "so the scan resolves against the connection's project); "
                "refusing to write a manifest for a table the query will never "
                "read"
            )
        columns = await resolve_materialised_columns(
            target_conn,
            schema=target_schema,
            table=target_table,
            tenant_session=tenant_session,
            system_session=system_session,
        )
        if not columns:
            raise ValueError(
                f"Target catalogue reports no columns for pocket table "
                f"{target_schema or ''}.{target_table}"
            )
        if target is None:
            raise ValueError(
                "Bug-8473: no DataTarget supplied, so the build's routing "
                "identity cannot be recorded; refusing to write a manifest that "
                "does not say which database its columns describe"
            )

        # Carry the descriptive attribute edges written by
        # ``advance_artifact_manifest`` EARLIER IN THIS SAME RUN. Edges stamped
        # with any other run describe a previous build and must not ride along.
        edges: list[dict[str, Any]] = []
        existing = getattr(pocket, "row_manifest", None)
        if isinstance(existing, dict) and str(
            existing.get("build_refresh_run_id") or ""
        ) == str(run_id):
            prior_edges = existing.get("attribute_edges")
            if isinstance(prior_edges, list):
                edges = prior_edges

        # Bug-8816: use the ALREADY-CAPTURED target binding when available —
        # the one the build proved before the first physical write. Re-deriving
        # it here from live control-plane state could produce a different value
        # if a re-point landed mid-build, and that value would be UNPROVEN
        # (never compared under the finalisation locks). The aggregate writers
        # already persist their frozen capture; the pocket path was the last
        # holdout.
        _target_binding = target_binding_dict
        if _target_binding is None:
            _target_binding = await resolve_target_binding_dict(
                target,
                target_conn,
                tenant_session=tenant_session,
                system_session=system_session,
            )

        manifest = RowManifest(
            deployed_version_id=(
                str(deployed_version_id) if deployed_version_id else None
            ),
            row_definition_fingerprint=getattr(pocket, "query_fingerprint", None),
            columns=columns,
            attribute_edges=edges,
            build_refresh_run_id=str(run_id),
            # Bug-8473: the column names above identify a table only WITHIN a
            # database. Which database they resolve to is decided by the
            # target's connection and config, both mutable control-plane state.
            # Record the routing identity this build actually wrote to, so the
            # query-router can refuse to scan when it no longer holds.
            target_binding=_target_binding,
            # Bug-8780: record the SOURCE routing identity too, so the
            # query-router can also detect a source re-point (``fallback_*``
            # changes, DataSource pointer swap) at serve time.
            source_binding=source_binding_dict,
        )
        manifest.manifest_hash = compute_row_manifest_hash(manifest)
        pocket.row_manifest = manifest.to_dict()
        # Same transaction as the manifest: the consumer's liveness check is
        # `row_manifest.build_refresh_run_id == active_refresh_run_id`, so the two
        # must never be written apart.
        pocket.active_refresh_run_id = run_id
        return True
    except Exception:
        logger.warning(
            "Pocket row manifest not written for pocket %s (run %s); the pocket "
            "will not be served under row-level security until a later refresh "
            "records its materialised columns",
            getattr(pocket, "id", None), run_id, exc_info=True,
        )
        # No manifest (RLS stays fail-closed) but the pointer STILL advances to
        # this run: the pocket is about to return to ``status="fresh"`` and is
        # servable on non-RLS routes, so its generation stamp must move. See
        # ``clear_pocket_row_manifest`` for why leaving it NULL breaks the
        # query-router's generation guard.
        clear_pocket_row_manifest(pocket, generation_run_id=run_id)
        return False
