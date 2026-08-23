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
    deprecation_scope_node_types,
    is_in_deprecation_scope,
    load_mappings,
    payload_hash,
    responsibility_hash,
    responsibility_key,
    validate_governance_graph,
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

        # Bug-7522: produce governance-quality warnings from the graph.
        graph_warnings = validate_governance_graph(graph)

        # F-035-06: record the exact exported snapshot identity on the run so a
        # governance administrator can prove which deployed model version
        # produced this preview / future remote change.
        if graph.snapshot is not None:
            run.tessallite_snapshot_hash = graph.snapshot.content_hash
        await db.commit()

        # ---- map to Collibra payload ------------------------------------
        payload = map_graph_to_collibra(
            graph,
            domain_id=conn.domain_id or "",
            asset_type_mapping=conn.asset_type_mapping or {},
            relation_type_mapping=conn.relation_type_mapping or {},
            responsibility_mapping=conn.responsibility_mapping or {},
        )

        # Bug-6496: honor include_responsibilities on every path. Previously
        # the flag only gated the live push; the dry-run count and the
        # incremental diff ignored it. Dropping them here makes the flag
        # authoritative for counts, diff, and push alike.
        if not include_responsibilities:
            payload.responsibilities = []

        # ---- hash and diff ----------------------------------------------
        asset_hashes: dict[str, str] = {}
        for asset in payload.assets:
            asset_hashes[asset.external_id] = payload_hash(asset)

        relation_hashes: dict[str, str] = {}
        for rel in payload.relations:
            relation_hashes[rel.external_id] = payload_hash(rel)

        # Bug-6496: responsibilities now carry an incremental hash keyed on
        # (asset, role) so an ownership change is detected as a change and a
        # removed responsibility can be deprecated like any other object.
        responsibility_hashes: dict[tuple[str, str], str] = {}
        for resp in payload.responsibilities:
            responsibility_hashes[responsibility_key(resp)] = responsibility_hash(resp)

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
            elif mapping.last_payload_hash != h or mapping.is_deprecated:
                # is_deprecated: an object that was removed then re-added must
                # be re-pushed and un-deprecated even if its content is
                # unchanged, or it stays deprecated on the remote.
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
            elif mapping.last_payload_hash != h or mapping.is_deprecated:
                changed_relations.append(rel)

        new_responsibilities = []
        changed_responsibilities = []
        for resp in payload.responsibilities:
            key = responsibility_key(resp)
            current_keys.add(key)
            h = responsibility_hashes[key]
            mapping = existing.get(key)
            if mapping is None:
                new_responsibilities.append(resp)
            elif mapping.last_payload_hash != h or mapping.is_deprecated:
                changed_responsibilities.append(resp)

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

            # Bug-6496: push only new/changed responsibilities (incremental)
            # rather than the full set every run.
            resp_to_push = new_responsibilities + changed_responsibilities
            if resp_to_push:
                await client.upsert_responsibilities(resp_to_push)

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
                elif mapping.last_payload_hash != h or mapping.is_deprecated:
                    mapping.last_payload_hash = h
                    mapping.is_deprecated = False
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id
                # F-035-02: refresh a drifted stable key (repairs legacy
                # slug-based keys). The diff key is the stable object UUID.
                if mapping.tessallite_stable_key != asset.external_id:
                    mapping.tessallite_stable_key = asset.external_id
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
                elif mapping.last_payload_hash != h or mapping.is_deprecated:
                    mapping.last_payload_hash = h
                    mapping.is_deprecated = False
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id
                # F-035-02: keep the stored stable key aligned with the id.
                if mapping.tessallite_stable_key != rel.external_id:
                    mapping.tessallite_stable_key = rel.external_id
                remote_id = relation_remote_ids.get(rel.external_id)
                if remote_id:
                    mapping.collibra_resource_id = remote_id

            # Bug-6496: persist responsibility mappings + hashes so future
            # syncs detect ownership changes and can deprecate removals.
            for resp in payload.responsibilities:
                key = responsibility_key(resp)
                _, r_stable = key
                h = responsibility_hashes[key]
                mapping = existing.get(key)
                if mapping is None:
                    mapping = CollibraObjectMapping(
                        connection_id=connection_id,
                        tessallite_object_type="responsibility",
                        tessallite_object_id=r_stable,
                        tessallite_stable_key=r_stable,
                        collibra_resource_type="responsibility",
                        last_payload_hash=h,
                        last_sync_run_id=run.id,
                    )
                    db.add(mapping)
                elif mapping.last_payload_hash != h or mapping.is_deprecated:
                    mapping.last_payload_hash = h
                    mapping.is_deprecated = False
                    mapping.last_synced_at = datetime.now(timezone.utc)
                    mapping.last_sync_run_id = run.id

            # ---- deprecate removed objects ------------------------------
            # F-035-03: derive the in-scope type set from the RUN's request
            # flags, not the current payload contents, so deleting the last
            # object of a category still deprecates its stranded mappings.
            node_scope = deprecation_scope_node_types(
                include_technical=include_technical,
                include_aggregates=include_aggregates,
                include_glossary=include_glossary,
                include_security_tags=include_security_tags,
                include_downstream_assets=include_downstream_assets,
                include_responsibilities=include_responsibilities,
            )
            removed_asset_remote_ids: list[str] = []
            removed_relation_remote_ids: list[str] = []
            # Fable-R1: collect first, call remote, then mark local — same
            # pattern as Solidatus to prevent the failure path from
            # permanently skipping stranded mappings.
            mappings_to_deprecate: list = []
            if deprecate_missing:
                for (obj_type, obj_id), mapping in existing.items():
                    # Responsibilities stay gated on include_responsibilities.
                    if obj_type == "responsibility" and not include_responsibilities:
                        continue
                    if not is_in_deprecation_scope(obj_type, node_scope):
                        continue
                    if (obj_type, obj_id) in current_keys:
                        continue
                    if mapping.is_deprecated:
                        continue
                    remote_id = (
                        getattr(mapping, "collibra_resource_id", None)
                        or mapping.tessallite_stable_key
                    )
                    resource_type = getattr(
                        mapping, "collibra_resource_type", "asset"
                    )
                    if resource_type == "relation":
                        removed_relation_remote_ids.append(remote_id)
                    elif resource_type == "responsibility":
                        # No remote responsibility-deprecation method; deprecate
                        # locally only (future live client handles removal).
                        pass
                    else:
                        removed_asset_remote_ids.append(remote_id)
                    mappings_to_deprecate.append(mapping)

                # Remote deprecation calls BEFORE marking local state.
                if removed_asset_remote_ids:
                    await client.deprecate_assets(removed_asset_remote_ids)
                if removed_relation_remote_ids:
                    await client.deprecate_relations(removed_relation_remote_ids)

                # Remote succeeded: now mark local.
                for mapping in mappings_to_deprecate:
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
        # Bug-7522: persist governance-quality warnings.
        run.warnings_json = graph_warnings
        run.status = "succeeded"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()

        return run

    except Exception as exc:
        # Bug-6027 (sibling of Bug-5987/F-030-03, same defect pattern found
        # in collibra.py while fixing the Solidatus case): return the
        # persisted failed run instead of re-raising, so the caller
        # (collibra_sync endpoint) can report the real run_id instead of
        # substituting a fake zero UUID it cannot correlate to any
        # collibra_sync_runs row.
        logger.exception("Collibra sync failed for run %s", run.id)
        run.status = "failed"
        run.error_message = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        return run
