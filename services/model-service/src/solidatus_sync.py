"""Solidatus sync orchestrator — incremental push with mapping persistence.

Phase 5: Runs the full sync cycle:
  1. Build governance graph
  2. Map to Solidatus payload
  3. Hash every node/edge
  4. Compare against solidatus_object_mappings
  5. Push changed/new nodes/edges
  6. Deprecate removed objects
  7. Persist mappings and run result
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    SolidatusConnection,
    SolidatusObjectMapping,
    SolidatusSyncRun,
)
from src.governance_exporter import build_governance_graph
from src.governance_helpers import (
    ALL_NODE_TYPES,
    decrypt_token,
    deprecation_scope_node_types,
    is_in_deprecation_scope,
    load_mappings,
    payload_hash,
    validate_governance_graph,
)
from src.solidatus_client import SolidatusClient
from src.solidatus_mapper import map_graph_to_solidatus

logger = logging.getLogger(__name__)


async def run_solidatus_sync(
    db: AsyncSession,
    *,
    connection_id: UUID,
    project_id: UUID,
    model_id: UUID,
    project_slug: str = "",
    model_slug: str = "",
    dry_run: bool = True,
    include_technical: bool = True,
    include_aggregates: bool = True,
    include_downstream_assets: bool = True,
    include_glossary: bool = True,
    include_security_tags: bool = True,
    include_hidden_objects: bool = True,
    deprecate_missing: bool = True,
    export_draft: bool = False,
) -> SolidatusSyncRun:
    """Execute a full Solidatus sync cycle.

    Returns the ``SolidatusSyncRun`` row with its real, persisted id and
    final ``status`` — ``"succeeded"`` or ``"failed"`` (Bug-5987: this used
    to raise on failure, forcing the caller to fabricate a fake run id
    since it had lost the real one). Still raises for failures that occur
    before the run row exists (e.g. the connection was deleted between the
    caller's own lookup and this call) — there is no persisted run to
    return in that case.
    """
    conn = await db.get(SolidatusConnection, connection_id)
    if conn is None:
        raise ValueError(f"Solidatus connection {connection_id} not found")

    # ---- create sync run ------------------------------------------------
    run = SolidatusSyncRun(
        connection_id=connection_id,
        project_id=project_id,
        model_id=model_id,
        mode="dry_run" if dry_run else "push",
        status="running",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    try:
        # ---- build graph ------------------------------------------------
        graph = await build_governance_graph(
            db,
            project_id=project_id,
            model_id=model_id,
            project_slug=project_slug,
            model_slug=model_slug,
            include_technical=include_technical,
            include_aggregates=include_aggregates,
            include_downstream_assets=include_downstream_assets,
            include_glossary=include_glossary,
            include_security_tags=include_security_tags,
            include_hidden_objects=include_hidden_objects,
            export_draft=export_draft,
        )

        # Bug-7522: produce governance-quality warnings from the graph.
        graph_warnings = validate_governance_graph(graph)

        # F-035-06: record the exact exported snapshot identity on the run so a
        # governance administrator can prove which deployed model version
        # produced this preview / future remote change. Set at build time (not
        # just completion) so even a run that fails mid-push retains the
        # snapshot it attempted.
        if graph.snapshot is not None:
            run.tessallite_snapshot_hash = graph.snapshot.content_hash
        run.solidatus_target_ref = conn.model_ref
        await db.commit()

        # ---- map to Solidatus payload -----------------------------------
        payload = map_graph_to_solidatus(graph)

        # ---- hash and diff ----------------------------------------------
        node_hashes: dict[str, str] = {}
        for node in payload.nodes:
            node_hashes[node.external_id] = payload_hash(node)

        edge_hashes: dict[str, str] = {}
        for edge in payload.edges:
            edge_hashes[edge.external_id] = payload_hash(edge)

        existing = await load_mappings(db, connection_id, SolidatusObjectMapping)
        current_keys: set[tuple[str, str]] = set()

        new_nodes = []
        changed_nodes = []
        for node in payload.nodes:
            key = (node.properties.get("tessallite_object_type", ""),
                   node.properties.get("tessallite_object_id", ""))
            current_keys.add(key)
            h = node_hashes[node.external_id]
            mapping = existing.get(key)
            if mapping is None:
                new_nodes.append(node)
            elif mapping.last_payload_hash != h or mapping.is_deprecated:
                # Bug-7525: is_deprecated -- an object that was removed then
                # re-added must be re-pushed and un-deprecated even if its
                # content is unchanged.
                changed_nodes.append(node)

        new_edges = []
        changed_edges = []
        for edge in payload.edges:
            key = (edge.properties.get("tessallite_relationship_type", "edge"),
                   edge.external_id)
            current_keys.add(key)
            h = edge_hashes[edge.external_id]
            mapping = existing.get(key)
            if mapping is None:
                new_edges.append(edge)
            elif mapping.last_payload_hash != h or mapping.is_deprecated:
                changed_edges.append(edge)

        nodes_created = len(new_nodes)
        nodes_updated = len(changed_nodes)
        edges_created = len(new_edges)
        edges_updated = len(changed_edges)

        # ---- push (if not dry run) --------------------------------------
        if not dry_run:
            token = decrypt_token(conn.encrypted_credentials)
            client = SolidatusClient(base_url=conn.base_url, token=token)
            node_remote_ids: dict[str, str] = {}
            edge_remote_ids: dict[str, str] = {}

            if new_nodes or changed_nodes:
                upsert_nodes = new_nodes + changed_nodes
                result = await client.upsert_nodes(
                    upsert_nodes, workspace_id=conn.workspace_id or ""
                )
                nodes_created = result.created
                nodes_updated = result.updated
                node_remote_ids.update(result.object_mappings or {})

            if new_edges or changed_edges:
                upsert_edges = new_edges + changed_edges
                result = await client.upsert_edges(
                    upsert_edges, workspace_id=conn.workspace_id or ""
                )
                edges_created = result.created
                edges_updated = result.updated
                edge_remote_ids.update(result.object_mappings or {})

            # ---- persist/update mappings ---------------------------------
            for node in payload.nodes:
                key = (node.properties.get("tessallite_object_type", ""),
                       node.properties.get("tessallite_object_id", ""))
                h = node_hashes[node.external_id]
                mapping = existing.get(key)
                if mapping is None:
                    mapping = SolidatusObjectMapping(
                        connection_id=connection_id,
                        tessallite_object_type=node.properties.get("tessallite_object_type", ""),
                        tessallite_object_id=node.properties.get("tessallite_object_id", ""),
                        tessallite_stable_key=node.external_id,
                        last_payload_hash=h,
                        last_sync_run_id=run.id,
                    )
                    db.add(mapping)
                elif mapping.last_payload_hash != h or mapping.is_deprecated:
                    # Bug-7525: clear is_deprecated on reactivation.
                    mapping.last_payload_hash = h
                    mapping.is_deprecated = False
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id
                # F-035-02: refresh the stored stable key if it drifted from the
                # current external id (repairs any legacy slug-based key so a
                # rename cannot leave a stale identity). The diff key is the
                # stable object UUID, so the mapping row is the same one.
                if mapping.tessallite_stable_key != node.external_id:
                    mapping.tessallite_stable_key = node.external_id
                remote_id = node_remote_ids.get(node.external_id)
                if remote_id:
                    mapping.solidatus_object_id = remote_id
                mapping.solidatus_object_ref = conn.model_ref

            for edge in payload.edges:
                edge_type = edge.properties.get("tessallite_relationship_type", "edge")
                key = (edge_type, edge.external_id)
                h = edge_hashes[edge.external_id]
                mapping = existing.get(key)
                if mapping is None:
                    mapping = SolidatusObjectMapping(
                        connection_id=connection_id,
                        tessallite_object_type=edge_type,
                        tessallite_object_id=edge.external_id,
                        tessallite_stable_key=edge.external_id,
                        last_payload_hash=h,
                        last_sync_run_id=run.id,
                    )
                    db.add(mapping)
                elif mapping.last_payload_hash != h or mapping.is_deprecated:
                    mapping.last_payload_hash = h
                    mapping.is_deprecated = False
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id
                # F-035-02: keep the stored stable key aligned with the edge id.
                if mapping.tessallite_stable_key != edge.external_id:
                    mapping.tessallite_stable_key = edge.external_id
                remote_id = edge_remote_ids.get(edge.external_id)
                if remote_id:
                    mapping.solidatus_object_id = remote_id
                mapping.solidatus_object_ref = conn.model_ref

            # ---- deprecate removed objects ------------------------------
            # F-035-03: build the in-scope type set from the RUN's request
            # flags, not from the current payload's contents. Deriving it from
            # the payload meant deleting the LAST object of a category made that
            # category absent from ``current_keys`` and every stranded mapping
            # of that category was skipped (Bug-7717 over-corrected). Node types
            # are gated by their category flag; edge types are always in scope.
            node_scope = deprecation_scope_node_types(
                include_technical=include_technical,
                include_aggregates=include_aggregates,
                include_glossary=include_glossary,
                include_security_tags=include_security_tags,
                include_downstream_assets=include_downstream_assets,
            )
            removed_node_remote_ids: list[str] = []
            removed_edge_remote_ids: list[str] = []
            # Fable-R1: collect mappings to deprecate FIRST, call remote
            # deprecation, and only flip is_deprecated AFTER the remote call
            # succeeds. If the remote call raises, the exception handler
            # commits the run as failed WITHOUT the is_deprecated flags, so
            # a subsequent run will retry the deprecation rather than
            # permanently skipping the stranded mappings.
            mappings_to_deprecate: list = []
            if deprecate_missing:
                for (obj_type, obj_id), mapping in existing.items():
                    if not is_in_deprecation_scope(obj_type, node_scope):
                        continue
                    if (obj_type, obj_id) in current_keys:
                        continue
                    if mapping.is_deprecated:
                        continue
                    remote_id = (
                        getattr(mapping, "solidatus_object_id", None)
                        or mapping.tessallite_stable_key
                    )
                    if obj_type in ALL_NODE_TYPES:
                        removed_node_remote_ids.append(remote_id)
                    else:
                        removed_edge_remote_ids.append(remote_id)
                    mappings_to_deprecate.append(mapping)

                # Remote deprecation calls BEFORE marking local state.
                if removed_node_remote_ids:
                    await client.deprecate_nodes(removed_node_remote_ids)
                if removed_edge_remote_ids:
                    await client.deprecate_edges(removed_edge_remote_ids)

                # Remote succeeded (or was a no-op): now mark local.
                for mapping in mappings_to_deprecate:
                    mapping.is_deprecated = True
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id

            await db.commit()

        # ---- update run --------------------------------------------------
        run.nodes_total = len(graph.nodes)
        run.edges_total = len(graph.edges)
        run.nodes_created = nodes_created
        run.nodes_updated = nodes_updated
        run.edges_created = edges_created
        run.edges_updated = edges_updated
        # Bug-7522: persist governance-quality warnings on the run row.
        run.result_json = {"warnings": graph_warnings}
        run.status = "succeeded"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()

        return run

    except Exception as exc:
        # Bug-5987 (F-030-03): return the persisted failed run instead of
        # re-raising. The caller (solidatus_sync endpoint) previously had
        # no way to recover this run's real id after catching the
        # re-raised exception, so it substituted a zero UUID
        # (00000000-...) — a fake identity that cannot be correlated to
        # this row in solidatus_sync_runs, breaking exactly the audit
        # trail an external-governance push failure needs most.
        logger.exception("Solidatus sync failed for run %s", run.id)
        run.status = "failed"
        run.error_message = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return run
