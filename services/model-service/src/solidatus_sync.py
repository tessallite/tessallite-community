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
    decrypt_token,
    load_mappings,
    payload_hash,
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
    deprecate_missing: bool = True,
    export_draft: bool = False,
) -> SolidatusSyncRun:
    """Execute a full Solidatus sync cycle.

    Returns the completed ``SolidatusSyncRun`` row.
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
            export_draft=export_draft,
        )

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
            elif mapping.last_payload_hash != h:
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
            elif mapping.last_payload_hash != h:
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
                elif mapping.last_payload_hash != h:
                    mapping.last_payload_hash = h
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id
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
                elif mapping.last_payload_hash != h:
                    mapping.last_payload_hash = h
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id
                remote_id = edge_remote_ids.get(edge.external_id)
                if remote_id:
                    mapping.solidatus_object_id = remote_id
                mapping.solidatus_object_ref = conn.model_ref

            # ---- deprecate removed objects ------------------------------
            if deprecate_missing:
                for (obj_type, obj_id), mapping in existing.items():
                    if (obj_type, obj_id) not in current_keys:
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
        run.status = "succeeded"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()

        return run

    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        raise
