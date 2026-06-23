"""Canonical governance-graph exporter — shared by Solidatus, Collibra, and
future governance-platform integrations.

Builds a ``GovernanceGraph`` (nodes + edges) from a Tessallite model snapshot
plus ancillary rows (Project, DownstreamAssets) that live outside the snapshot.

Phase 2 (this file):   export-preview from GovernanceGraph.
Phase 3 (separate):    mapper → platform-specific payload.
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    DownstreamAsset,
    Model,
    ModelVersion,
    Project,
    downstream_asset_columns,
)
from shared.model_snapshot.governance_graph import GovernanceEdge, GovernanceGraph, GovernanceNode
from shared.model_snapshot.serialiser import snapshot_model

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Node type constants
# ---------------------------------------------------------------------------

NODE_DOMAIN = "domain"
NODE_SEMANTIC_MODEL = "semantic_model"
NODE_SOURCE_SYSTEM = "source_system"
NODE_TABLE = "table"
NODE_COLUMN = "column"
NODE_DIMENSION = "dimension"
NODE_MEASURE = "measure"
NODE_KPI = "kpi"
NODE_GLOSSARY_TERM = "glossary_term"
NODE_DOWNSTREAM_ASSET = "downstream_asset"
NODE_AGGREGATE = "aggregate"
NODE_DATA_TAG = "data_tag"
NODE_DATA_TARGET = "data_target"

# ---------------------------------------------------------------------------
# Edge relationship type constants
# ---------------------------------------------------------------------------

EDGE_CONTAINS = "contains"
EDGE_CONTAINS_TABLE = "contains_table"
EDGE_CONTAINS_COLUMN = "contains_column"
EDGE_DEFINES_DIMENSION = "defines_dimension"
EDGE_DEFINES_MEASURE = "defines_measure"
EDGE_DERIVED_FROM = "derived_from"
EDGE_GOVERNED_BY_TERM = "governed_by_term"
EDGE_PRODUCES_AGGREGATE = "produces_aggregate"
EDGE_MATERIALIZED_TO = "materialized_to"
EDGE_CONSUMED_BY = "consumed_by"
EDGE_FEEDS_SEMANTIC = "feeds_semantic_field"
EDGE_USES_MEASURE = "uses_measure"
EDGE_CLASSIFIED_BY = "classified_by"


class GovernanceExportUnavailable(ValueError):
    """Raised when the requested governance snapshot cannot be exported."""


def _skey(*parts: str) -> str:
    """Build a stable key from ordered parts, e.g. ``("sales","revenue","measure","gross_revenue")``."""
    return ".".join(str(p) for p in parts if p is not None)


def _display(obj: dict, fields: tuple[str, ...] = ("display_name", "name")) -> str:
    """Return the best available display label for a snapshot dict row."""
    for f in fields:
        v = obj.get(f)
        if v:
            return str(v)
    return obj.get("id", "?")


async def _snapshot_for_export(
    db: AsyncSession,
    *,
    project_id: UUID,
    model_id: UUID,
    export_draft: bool,
) -> dict:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise GovernanceExportUnavailable(f"Model {model_id} not found")

    if export_draft:
        return await snapshot_model(model_id, db)

    if model.deployed_version_id is None:
        raise GovernanceExportUnavailable(
            "Model has no deployed version. Enable export_draft to export draft state."
        )

    version = await db.get(ModelVersion, model.deployed_version_id)
    if version is None or version.model_id != model_id:
        raise GovernanceExportUnavailable(
            f"Deployed version {model.deployed_version_id} was not found for model {model_id}"
        )

    snap = dict(version.snapshot_json)
    snap["exported_deployed_version_id"] = str(model.deployed_version_id)
    return snap


def _iter_measure_refs(obj: object) -> set[str]:
    """Return measure UUID references from KPI business-definition JSON."""
    refs: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.endswith("measure_id") and value:
                refs.add(str(value))
            elif key.endswith("measure_ids") and isinstance(value, list):
                refs.update(str(v) for v in value if v)
            refs.update(_iter_measure_refs(value))
    elif isinstance(obj, list):
        for item in obj:
            refs.update(_iter_measure_refs(item))
    return refs


async def build_governance_graph(
    db: AsyncSession,
    *,
    project_id: UUID,
    model_id: UUID,
    project_slug: str = "",
    model_slug: str = "",
    include_technical: bool = True,
    include_aggregates: bool = True,
    include_downstream_assets: bool = True,
    include_glossary: bool = True,
    include_security_tags: bool = True,
    include_business_assets: bool = True,
    include_hidden_objects: bool = True,
    export_draft: bool = False,
) -> GovernanceGraph:
    """Build the canonical governance graph for one model.

    Parameters control which object categories appear in the export.
    ``export_draft=False`` exports the model's deployed snapshot. Draft state
    is exported only when explicitly requested with ``export_draft=True``.
    """
    nodes: list[GovernanceNode] = []
    edges: list[GovernanceEdge] = []

    # ---- snapshot ---------------------------------------------------------
    snap = await _snapshot_for_export(
        db,
        project_id=project_id,
        model_id=model_id,
        export_draft=export_draft,
    )

    model_dict: dict = snap["model"]
    tables: list[dict] = snap.get("tables", [])
    columns: list[dict] = snap.get("columns", [])
    dimensions: list[dict] = snap.get("dimensions", [])
    measures: list[dict] = snap.get("measures", [])
    kpis: list[dict] = snap.get("kpis", [])
    data_sources: list[dict] = snap.get("data_sources", [])
    data_targets: list[dict] = snap.get("data_targets", [])
    aggregates: list[dict] = snap.get("aggregates", [])
    glossary_entries: list[dict] = snap.get("glossary_entries", [])
    data_tags: list[dict] = snap.get("data_tags", [])
    lineage_mappings: list[dict] = snap.get("lineage_mappings", [])

    # ---- project (domain) node -------------------------------------------
    project = await db.get(Project, project_id)
    if project:
        nodes.append(GovernanceNode(
            stable_key=_skey(project_slug or project.slug),
            object_type=NODE_DOMAIN,
            object_id=str(project.id),
            label=project.display_name,
            properties={"slug": project.slug},
        ))

    # ---- model node -------------------------------------------------------
    model_node_key = _skey(project_slug or "project", model_slug or model_dict.get("slug", ""))
    nodes.append(GovernanceNode(
        stable_key=model_node_key,
        object_type=NODE_SEMANTIC_MODEL,
        object_id=str(model_dict["id"]),
        label=_display(model_dict),
        description=model_dict.get("description"),
        status=model_dict.get("status"),
        properties={
            k: v for k, v in model_dict.items()
            if k not in ("id", "display_name", "name", "description", "status")
            and v is not None
        },
    ))
    if project:
        edges.append(GovernanceEdge(
            stable_key=_skey(project_slug or project.slug, "contains", model_node_key),
            source_key=_skey(project_slug or project.slug),
            target_key=model_node_key,
            relationship_type=EDGE_CONTAINS,
        ))

    # ---- source system nodes ---------------------------------------------
    if include_technical:
        for ds in data_sources:
            ds_key = _skey("source", str(ds["id"]))
            nodes.append(GovernanceNode(
                stable_key=ds_key,
                object_type=NODE_SOURCE_SYSTEM,
                object_id=str(ds["id"]),
                label=_display(ds),
                properties={
                    "source_type": ds.get("source_type"),
                    "default_schema": ds.get("default_schema"),
                },
            ))

    # ---- data target nodes -----------------------------------------------
    if include_technical:
        for dt in data_targets:
            dt_key = _skey("target", str(dt["id"]))
            nodes.append(GovernanceNode(
                stable_key=dt_key,
                object_type=NODE_DATA_TARGET,
                object_id=str(dt["id"]),
                label=_display(dt),
                properties={
                    "target_type": dt.get("target_type"),
                    "default_schema": dt.get("default_schema"),
                },
            ))

    # ---- table nodes + source → table edges ------------------------------
    table_keys: dict[str, str] = {}
    if include_technical:
        for t in tables:
            t_key = _skey("table", str(t["id"]))
            table_keys[str(t["id"])] = t_key
            nodes.append(GovernanceNode(
                stable_key=t_key,
                object_type=NODE_TABLE,
                object_id=str(t["id"]),
                label=_display(t, ("alias", "display_name", "physical_name")),
                description=t.get("description"),
                properties={
                    "physical_name": t.get("physical_name"),
                    "table_type": t.get("table_type"),
                },
            ))
            # source → table
            src_id = t.get("data_source_id")
            if src_id:
                ds_key = _skey("source", str(src_id))
                edges.append(GovernanceEdge(
                    stable_key=_skey(ds_key, "contains_table", t_key),
                    source_key=ds_key,
                    target_key=t_key,
                    relationship_type=EDGE_CONTAINS_TABLE,
                ))

    # ---- column nodes + table → column edges -----------------------------
    column_keys: dict[str, str] = {}
    if include_technical:
        for c in columns:
            if not include_hidden_objects and c.get("is_hidden"):
                continue
            c_key = _skey("column", str(c["id"]))
            column_keys[str(c["id"])] = c_key
            nodes.append(GovernanceNode(
                stable_key=c_key,
                object_type=NODE_COLUMN,
                object_id=str(c["id"]),
                label=_display(c, ("display_name", "column_name")),
                description=c.get("description"),
                properties={
                    "physical_column_name": c.get("column_name"),
                    "data_type": c.get("data_type"),
                    "is_nullable": c.get("is_nullable"),
                    "is_hidden": c.get("is_hidden"),
                    "is_primary_key": c.get("is_primary_key"),
                },
            ))
            # table → column
            t_id = str(c.get("model_table_id", ""))
            t_key = table_keys.get(t_id)
            if t_key:
                edges.append(GovernanceEdge(
                    stable_key=_skey(t_key, "contains_column", c_key),
                    source_key=t_key,
                    target_key=c_key,
                    relationship_type=EDGE_CONTAINS_COLUMN,
                ))

    # ---- dimension nodes + column → dimension edges ----------------------
    dim_keys: dict[str, str] = {}
    semantic_field_keys: dict[tuple[str, str], str] = {}
    if include_business_assets:
        for d in dimensions:
            if not include_hidden_objects and d.get("is_hidden"):
                continue
            d_key = _skey("dimension", str(d["id"]))
            dim_keys[str(d["id"])] = d_key
            if d.get("name"):
                semantic_field_keys[(NODE_DIMENSION, str(d["name"]))] = d_key
            if d.get("display_name"):
                semantic_field_keys[(NODE_DIMENSION, str(d["display_name"]))] = d_key
            nodes.append(GovernanceNode(
                stable_key=d_key,
                object_type=NODE_DIMENSION,
                object_id=str(d["id"]),
                label=_display(d),
                description=d.get("description") or d.get("effective_description"),
                status="hidden" if d.get("is_hidden") else None,
                properties={
                    "name": d.get("name"),
                    "display_folder": d.get("display_folder"),
                    "is_hidden": d.get("is_hidden"),
                    "is_time_dim": d.get("is_time_dim"),
                    "time_grain": d.get("time_grain"),
                    "calc_expression": d.get("calc_expression"),
                },
            ))
            # column → dimension
            col_id = d.get("source_column_id") or d.get("user_defined_attribute_id")
            if col_id:
                c_key = column_keys.get(str(col_id))
                if c_key:
                    edges.append(GovernanceEdge(
                        stable_key=_skey(c_key, "defines", d_key),
                        source_key=c_key,
                        target_key=d_key,
                        relationship_type=EDGE_DEFINES_DIMENSION,
                    ))

    # ---- measure nodes + column → measure edges --------------------------
    meas_keys: dict[str, str] = {}
    if include_business_assets:
        for m in measures:
            if not include_hidden_objects and m.get("is_hidden"):
                continue
            m_key = _skey("measure", str(m["id"]))
            meas_keys[str(m["id"])] = m_key
            if m.get("name"):
                semantic_field_keys[(NODE_MEASURE, str(m["name"]))] = m_key
            if m.get("display_name"):
                semantic_field_keys[(NODE_MEASURE, str(m["display_name"]))] = m_key
            nodes.append(GovernanceNode(
                stable_key=m_key,
                object_type=NODE_MEASURE,
                object_id=str(m["id"]),
                label=_display(m),
                description=m.get("description") or m.get("effective_description"),
                status="hidden" if m.get("is_hidden") else None,
                properties={
                    "name": m.get("name"),
                    "measure_type": m.get("measure_type"),
                    "expression": m.get("expression"),
                    "default_agg": m.get("default_agg"),
                    "is_additive": m.get("is_additive"),
                    "variant_kind": m.get("variant_kind"),
                    "display_folder": m.get("display_folder"),
                },
            ))
            # column → measure
            col_id = m.get("source_column_id")
            if col_id:
                c_key = column_keys.get(str(col_id))
                if c_key:
                    edges.append(GovernanceEdge(
                        stable_key=_skey(c_key, "defines", m_key),
                        source_key=c_key,
                        target_key=m_key,
                        relationship_type=EDGE_DEFINES_MEASURE,
                    ))
            # variant_of → base measure
            base_id = m.get("variant_of_measure_id")
            if base_id:
                base_key = meas_keys.get(str(base_id))
                if base_key:
                    edges.append(GovernanceEdge(
                        stable_key=_skey(m_key, "derived_from", base_key),
                        source_key=m_key,
                        target_key=base_key,
                        relationship_type=EDGE_DERIVED_FROM,
                    ))

    # ---- KPI nodes + KPI → measure edges ---------------------------------
    if include_business_assets:
        for k in kpis:
            k_key = _skey("kpi", str(k["id"]))
            nodes.append(GovernanceNode(
                stable_key=k_key,
                object_type=NODE_KPI,
                object_id=str(k["id"]),
                label=_display(k),
                description=k.get("description"),
                status=k.get("certification_status"),
                owner=k.get("owner_user_id"),
                properties={
                    "name": k.get("name"),
                    "kpi_type": k.get("kpi_type"),
                    "expression": k.get("expression"),
                    "direction": k.get("direction"),
                    "is_deployed": k.get("is_deployed"),
                },
            ))
            measure_refs = {
                str(v)
                for v in (
                    k.get("value_measure_id"),
                    k.get("goal_measure_id"),
                    k.get("target_measure_id"),
                )
                if v
            }
            measure_refs.update(_iter_measure_refs(k.get("business_definition")))
            for measure_id in sorted(measure_refs):
                m_key = meas_keys.get(measure_id)
                if m_key:
                    edges.append(GovernanceEdge(
                        stable_key=_skey(k_key, "uses", m_key),
                        source_key=k_key,
                        target_key=m_key,
                        relationship_type=EDGE_USES_MEASURE,
                    ))

    # ---- aggregate nodes + model → aggregate → target edges --------------
    if include_technical and include_aggregates:
        for agg in aggregates:
            a_key = _skey("aggregate", str(agg["id"]))
            nodes.append(GovernanceNode(
                stable_key=a_key,
                object_type=NODE_AGGREGATE,
                object_id=str(agg["id"]),
                label=agg.get("physical_table_name", agg.get("id")),
                status=agg.get("status"),
                properties={
                    "physical_table_name": agg.get("physical_table_name"),
                    "target_schema": agg.get("target_schema"),
                    "creation_reason": agg.get("creation_reason"),
                    "grain": agg.get("grain"),
                },
            ))
            # model → aggregate
            edges.append(GovernanceEdge(
                stable_key=_skey(model_node_key, "produces", a_key),
                source_key=model_node_key,
                target_key=a_key,
                relationship_type=EDGE_PRODUCES_AGGREGATE,
            ))
            # aggregate → target
            target_id = agg.get("target_id")
            if target_id:
                t_key = _skey("target", str(target_id))
                edges.append(GovernanceEdge(
                    stable_key=_skey(a_key, "materialized_to", t_key),
                    source_key=a_key,
                    target_key=t_key,
                    relationship_type=EDGE_MATERIALIZED_TO,
                ))

    # ---- glossary term nodes ---------------------------------------------
    if include_business_assets and include_glossary:
        for g in glossary_entries:
            g_key = _skey("glossary", str(g["id"]))
            nodes.append(GovernanceNode(
                stable_key=g_key,
                object_type=NODE_GLOSSARY_TERM,
                object_id=str(g["id"]),
                label=g.get("term", g.get("id")),
                description=g.get("definition"),
                status=g.get("status"),
                properties={
                    "provenance": g.get("provenance"),
                    "synonyms": [s.get("term") for s in g.get("synonyms", [])],
                },
            ))
            for attachment in g.get("attachments", []):
                target_type = attachment.get("target_type")
                target_id = attachment.get("target_id")
                if not target_id:
                    continue
                target_key = {
                    NODE_COLUMN: column_keys,
                    NODE_DIMENSION: dim_keys,
                    NODE_MEASURE: meas_keys,
                }.get(str(target_type), {}).get(str(target_id))
                if target_key:
                    edges.append(GovernanceEdge(
                        stable_key=_skey(target_key, "governed_by", g_key),
                        source_key=target_key,
                        target_key=g_key,
                        relationship_type=EDGE_GOVERNED_BY_TERM,
                    ))

    # ---- data tag nodes + tag → column edges -----------------------------
    if include_security_tags:
        for tag in data_tags:
            tag_key = _skey("tag", str(tag["id"]))
            nodes.append(GovernanceNode(
                stable_key=tag_key,
                object_type=NODE_DATA_TAG,
                object_id=str(tag["id"]),
                label=_display(tag),
                description=tag.get("description"),
                properties={
                    "tag_type": tag.get("tag_type"),
                    "sensitivity_level": tag.get("sensitivity_level"),
                },
            ))
            for col_id in tag.get("column_ids", []):
                c_key = column_keys.get(str(col_id))
                if c_key:
                    edges.append(GovernanceEdge(
                        stable_key=_skey(tag_key, "classifies", c_key),
                        source_key=tag_key,
                        target_key=c_key,
                        relationship_type=EDGE_CLASSIFIED_BY,
                    ))

    # ---- downstream asset nodes + model/column → asset edges -------------
    if include_business_assets and include_downstream_assets:
        da_q = await db.execute(
            select(DownstreamAsset)
            .where(DownstreamAsset.model_id == model_id)
            .order_by(DownstreamAsset.created_at)
        )
        downstream_assets = list(da_q.scalars().all())

        # Fetch column links for all downstream assets
        da_ids = [da.id for da in downstream_assets]
        da_col_links: dict[str, set[str]] = {}
        if da_ids:
            links_q = await db.execute(
                select(downstream_asset_columns.c.downstream_asset_id,
                       downstream_asset_columns.c.model_column_id)
                .where(downstream_asset_columns.c.downstream_asset_id.in_(da_ids))
            )
            for da_id, col_id in links_q.all():
                da_col_links.setdefault(str(da_id), set()).add(str(col_id))

        for da in downstream_assets:
            da_key = _skey("downstream", str(da.id))
            nodes.append(GovernanceNode(
                stable_key=da_key,
                object_type=NODE_DOWNSTREAM_ASSET,
                object_id=str(da.id),
                label=da.asset_name,
                owner=da.owner,
                properties={
                    "asset_type": da.asset_type,
                    "asset_url": da.asset_url or "",
                    "notes": da.notes,
                },
            ))
            # model → downstream asset
            edges.append(GovernanceEdge(
                stable_key=_skey(model_node_key, "consumed_by", da_key),
                source_key=model_node_key,
                target_key=da_key,
                relationship_type=EDGE_CONSUMED_BY,
            ))
            # downstream asset → specific columns
            for col_id in da_col_links.get(str(da.id), set()):
                c_key = column_keys.get(col_id)
                if c_key:
                    edges.append(GovernanceEdge(
                        stable_key=_skey(da_key, "uses", c_key),
                        source_key=da_key,
                        target_key=c_key,
                        relationship_type=EDGE_CONSUMED_BY,
                    ))

    # ---- lineage edges (source column → semantic field) ------------------
    if include_technical:
        for lm in lineage_mappings:
            source_column_id = lm.get("source_column_id")
            semantic_field_name = lm.get("semantic_field_name")
            semantic_field_type = lm.get("semantic_field_type")
            if source_column_id and semantic_field_name and semantic_field_type:
                src_key = column_keys.get(str(source_column_id))
                tgt_key = semantic_field_keys.get(
                    (str(semantic_field_type), str(semantic_field_name))
                )
                if src_key and tgt_key:
                    edges.append(GovernanceEdge(
                        stable_key=_skey("lineage", src_key, "feeds", tgt_key),
                        source_key=src_key,
                        target_key=tgt_key,
                        relationship_type=EDGE_FEEDS_SEMANTIC,
                        properties={
                            "source_column_id": str(source_column_id),
                            "semantic_field_name": str(semantic_field_name),
                            "semantic_field_type": str(semantic_field_type),
                            "aggregate_col_id": str(lm.get("aggregate_col_id") or ""),
                        },
                    ))

    node_keys = {node.stable_key for node in nodes}
    edges = [
        edge for edge in edges
        if edge.source_key in node_keys and edge.target_key in node_keys
    ]

    return GovernanceGraph(nodes=nodes, edges=edges)
