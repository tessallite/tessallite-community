"""Collibra sync orchestrator — incremental push with mapping persistence.

Phase 5: Runs the full sync cycle:
  1. Build governance graph
  2. Map to Collibra assets/relations/responsibilities
  3. Hash every asset/relation
  4. Compare against collibra_object_mappings
  5. Push changed/new
  6. Deprecate removed objects
  7. Persist mappings and run result
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    CollibraConnection,
    CollibraObjectMapping,
    CollibraSyncRun,
)
from src.collibra_client import CollibraClient
from src.collibra_mapper import map_graph_to_collibra
from src.governance_exporter import build_governance_graph
from src.governance_helpers import (
    decrypt_token,
    load_mappings,
    payload_hash,
)

logger = logging.getLogger(__name__)


async def run_collibra_sync(
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
    include_responsibilities: bool = True,
    deprecate_missing: bool = True,
    export_draft: bool = False,
) -> CollibraSyncRun:
    """Execute a full Collibra sync cycle."""

    conn = await db.get(CollibraConnection, connection_id)
    if conn is None:
        raise ValueError(f"Collibra connection {connection_id} not found")

    run = CollibraSyncRun(
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

        # ---- map to Collibra payload ------------------------------------
        payload = map_graph_to_collibra(
            graph,
            domain_id=conn.domain_id or "",
            asset_type_mapping=conn.asset_type_mapping or {},
            relation_type_mapping=conn.relation_type_mapping or {},
            responsibility_mapping=conn.responsibility_mapping or {},
        )

        # ---- hash and diff ----------------------------------------------
        asset_hashes: dict[str, str] = {}
        for asset in payload.assets:
            asset_hashes[asset.external_id] = payload_hash(asset)

        relation_hashes: dict[str, str] = {}
        for rel in payload.relations:
            relation_hashes[rel.external_id] = payload_hash(rel)

        existing = await load_mappings(db, connection_id, CollibraObjectMapping)
        current_keys: set[tuple[str, str]] = set()

        new_assets = []
        changed_assets = []
        for asset in payload.assets:
            obj_type = asset.attributes.get("Tessallite Object Type", "")
            obj_id = asset.attributes.get("Tessallite Object ID", "")
            key = (obj_type, obj_id)
            current_keys.add(key)
            h = asset_hashes[asset.external_id]
            mapping = existing.get(key)
            if mapping is None:
                new_assets.append(asset)
            elif mapping.last_payload_hash != h:
                changed_assets.append(asset)

        new_relations = []
        changed_relations = []
        for rel in payload.relations:
            obj_type = rel.attributes.get("Tessallite Relationship Type", "relation")
            key = (obj_type, rel.external_id)
            current_keys.add(key)
            h = relation_hashes[rel.external_id]
            mapping = existing.get(key)
            if mapping is None:
                new_relations.append(rel)
            elif mapping.last_payload_hash != h:
                changed_relations.append(rel)

        assets_created = len(new_assets)
        assets_updated = len(changed_assets)
        relations_created = len(new_relations)
        relations_updated = len(changed_relations)

        # ---- push (if not dry run) --------------------------------------
        if not dry_run:
            token = decrypt_token(conn.encrypted_credentials)
            client = CollibraClient(base_url=conn.base_url, token=token)
            asset_remote_ids: dict[str, str] = {}
            relation_remote_ids: dict[str, str] = {}

            if new_assets or changed_assets:
                result = await client.upsert_assets(
                    new_assets + changed_assets,
                    domain_id=conn.domain_id or "",
                )
                assets_created = result.created
                assets_updated = result.updated
                asset_remote_ids.update(result.object_mappings or {})

            if new_relations or changed_relations:
                result = await client.upsert_relations(
                    new_relations + changed_relations
                )
                relations_created = result.created
                relations_updated = result.updated
                relation_remote_ids.update(result.object_mappings or {})

            if include_responsibilities and payload.responsibilities:
                await client.upsert_responsibilities(payload.responsibilities)

            # ---- persist mappings ----------------------------------------
            for asset in payload.assets:
                obj_type = asset.attributes.get("Tessallite Object Type", "")
                obj_id = asset.attributes.get("Tessallite Object ID", "")
                key = (obj_type, obj_id)
                h = asset_hashes[asset.external_id]
                mapping = existing.get(key)
                if mapping is None:
                    mapping = CollibraObjectMapping(
                        connection_id=connection_id,
                        tessallite_object_type=obj_type,
                        tessallite_object_id=obj_id,
                        tessallite_stable_key=asset.external_id,
                        collibra_resource_type="asset",
                        last_payload_hash=h,
                        last_sync_run_id=run.id,
                    )
                    db.add(mapping)
                elif mapping.last_payload_hash != h:
                    mapping.last_payload_hash = h
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id
                remote_id = asset_remote_ids.get(asset.external_id)
                if remote_id:
                    mapping.collibra_resource_id = remote_id

            for rel in payload.relations:
                obj_type = rel.attributes.get("Tessallite Relationship Type", "relation")
                key = (obj_type, rel.external_id)
                h = relation_hashes[rel.external_id]
                mapping = existing.get(key)
                if mapping is None:
                    mapping = CollibraObjectMapping(
                        connection_id=connection_id,
                        tessallite_object_type=obj_type,
                        tessallite_object_id=rel.external_id,
                        tessallite_stable_key=rel.external_id,
                        collibra_resource_type="relation",
                        last_payload_hash=h,
                        last_sync_run_id=run.id,
                    )
                    db.add(mapping)
                elif mapping.last_payload_hash != h:
                    mapping.last_payload_hash = h
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id
                remote_id = relation_remote_ids.get(rel.external_id)
                if remote_id:
                    mapping.collibra_resource_id = remote_id

            # ---- deprecate removed objects ------------------------------
            if deprecate_missing:
                for (obj_type, obj_id), mapping in existing.items():
                    if (obj_type, obj_id) not in current_keys:
                        mapping.is_deprecated = True
                        mapping.last_synced_at = datetime.now(timezone.utc)
                        mapping.last_sync_run_id = run.id

            await db.commit()

        # ---- update run --------------------------------------------------
        run.assets_total = len(graph.nodes)
        run.relations_total = len(graph.edges)
        run.attributes_total = sum(len(a.attributes) for a in payload.assets)
        run.responsibilities_total = len(payload.responsibilities)
        run.assets_created = assets_created
        run.assets_updated = assets_updated
        run.relations_created = relations_created
        run.relations_updated = relations_updated
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
