"""Shared helpers for governance integration API routers and sync orchestrators.

Extracted from duplicated copies in solidatus.py, collibra.py,
solidatus_sync.py, and collibra_sync.py (SCI-003, SCI-008).
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Model
from shared.security.credential_crypto import decrypt_json, encrypt_json


# ---------------------------------------------------------------------------
# Fernet helpers
# ---------------------------------------------------------------------------


def encrypt_credentials(data: dict) -> bytes:
    return encrypt_json(data)


def decrypt_credentials(data: bytes) -> dict:
    return decrypt_json(data)


def decrypt_token(encrypted_credentials: bytes) -> str:
    """Convenience: decrypt and return the 'token' field."""
    return decrypt_credentials(encrypted_credentials).get("token", "")


# ---------------------------------------------------------------------------
# FastAPI helpers
# ---------------------------------------------------------------------------


def not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def get_model(db: AsyncSession, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise not_found("Model not found")
    return model


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------


def payload_hash(obj) -> str:
    """Stable SHA-256 hash of a mapped payload object for diffing."""
    d: dict = {
        "external_id": obj.external_id,
        "type": getattr(obj, "type", getattr(obj, "asset_type", "")),
        "name": getattr(obj, "name", getattr(obj, "display_name", "")),
        "attributes": getattr(obj, "attributes", getattr(obj, "properties", {})),
    }
    # Bug-6491: include lifecycle status so a status-only change (e.g.
    # active -> deprecated) is not invisible to the incremental sync diff.
    # Only assets carry a status; add it conditionally so a payload object
    # that never had one (relations/edges) keeps a stable hash.
    status_val = getattr(obj, "status", None)
    if status_val is not None:
        d["status"] = status_val
    if hasattr(obj, "source_external_id"):
        d["source"] = obj.source_external_id
        d["target"] = obj.target_external_id
        d["edge_type"] = getattr(obj, "type", getattr(obj, "relation_type", ""))
    raw = json.dumps(d, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def responsibility_key(resp) -> tuple[str, str]:
    """Incremental-diff key for a responsibility: ``("responsibility", ...)``.

    A responsibility's identity is the (asset, kind) pair — where ``kind`` is
    the reserved discriminator ``owner``/``steward`` — NOT (asset, role):
    owner and steward can map to the same Collibra role LABEL, so keying on
    role would collapse the two and silently drop one assignee (Codex-R2).
    The assignee and role label are the mutable *value* (hashed separately).
    Keyed under the reserved ``responsibility`` object-type so it never
    collides with asset/relation mapping keys. Falls back to ``role`` for any
    object that predates the ``kind`` field.
    """
    kind = getattr(resp, "kind", None) or resp.role
    return ("responsibility", f"{resp.asset_external_id}::{kind}")


def responsibility_hash(resp) -> str:
    """Stable SHA-256 hash of a responsibility for incremental diffing.

    Bug-6496: responsibilities previously had no incremental treatment, so an
    ownership change (same asset+kind, different assignee) — or a role-label
    relabel — was invisible to the sync diff. Hashing role + assignee makes
    both detectable.
    """
    d = {
        "asset": resp.asset_external_id,
        "kind": getattr(resp, "kind", None) or resp.role,
        "role": resp.role,
        "user_or_group": resp.user_or_group,
    }
    raw = json.dumps(d, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Graph validation (Bug-7522)
# ---------------------------------------------------------------------------


def validate_governance_graph(graph) -> list[dict]:
    """Return governance-quality warnings for the graph.

    Bug-7522: preview and sync endpoints previously hard-coded warnings=[]
    even when the graph contained objects missing descriptions, owners, or
    other governance-relevant metadata.  This shared validator inspects the
    graph and emits stable warning dicts that the preview/sync responses
    and frontend panels can surface.
    """
    warnings: list[dict] = []

    # F-035-01: surface expression-lineage warnings the exporter raised while
    # building the graph (calculated-measure / KPI expression that could not be
    # parsed or resolved). These identify incomplete lineage that the purely
    # structural checks below cannot see.
    warnings.extend(getattr(graph, "export_warnings", None) or [])

    missing_description: list[str] = []
    missing_owner: list[str] = []
    ownable_types = {"kpi", "downstream_asset"}

    for node in graph.nodes:
        if not node.description:
            missing_description.append(node.label)
        if node.object_type in ownable_types and not node.owner:
            missing_owner.append(node.label)

    if missing_description:
        warnings.append({
            "code": "MISSING_DESCRIPTION",
            "message": (
                f"{len(missing_description)} object(s) have no description. "
                f"Governance platforms rely on descriptions for cataloging."
            ),
            "count": len(missing_description),
        })

    if missing_owner:
        warnings.append({
            "code": "MISSING_OWNER",
            "message": (
                f"{len(missing_owner)} ownable object(s) have no owner assigned. "
                f"Owner responsibilities cannot be exported for these objects."
            ),
            "count": len(missing_owner),
        })

    orphan_edges = 0
    node_keys = {n.stable_key for n in graph.nodes}
    for edge in graph.edges:
        if edge.source_key not in node_keys or edge.target_key not in node_keys:
            orphan_edges += 1
    if orphan_edges:
        warnings.append({
            "code": "ORPHAN_EDGES",
            "message": (
                f"{orphan_edges} edge(s) reference nodes not present in the graph. "
                f"These relationships will be incomplete in the governance platform."
            ),
            "count": orphan_edges,
        })

    return warnings


# ---------------------------------------------------------------------------
# Deprecation scope (F-035-03)
# ---------------------------------------------------------------------------

# Governance node object-types grouped by the include-flag that governs them.
# Used to build the deprecation in-scope set from the RUN's request flags,
# independently of the current payload's contents — so a category that was
# requested but is now empty (its last object deleted) still deprecates its
# stranded mappings, instead of being silently skipped (F-035-03 root cause 2).
_ALWAYS_SCOPED_NODE_TYPES = frozenset({"domain", "semantic_model"})
_BUSINESS_NODE_TYPES = frozenset({"dimension", "measure", "kpi"})
_TECHNICAL_NODE_TYPES = frozenset(
    {"source_system", "data_target", "table", "column"}
)

# Every governance NODE object-type. A mapping whose type is NOT in this set is
# an edge/relationship or responsibility mapping — those are derived from their
# endpoints and are always in deprecation scope (an edge whose endpoint node was
# removed is genuinely removed), so they are never gated by a node category.
ALL_NODE_TYPES = frozenset(
    _ALWAYS_SCOPED_NODE_TYPES
    | _BUSINESS_NODE_TYPES
    | _TECHNICAL_NODE_TYPES
    | {"aggregate", "glossary_term", "data_tag", "downstream_asset"}
)


def is_in_deprecation_scope(obj_type: str, node_scope: set[str]) -> bool:
    """True when a mapping of ``obj_type`` may be deprecated by a run whose
    in-scope node types are ``node_scope``.

    Node types are gated by their category flag (``node_scope``); edge and
    responsibility types are always in scope because they are derived from their
    endpoints, which the run already governs.
    """
    if obj_type not in ALL_NODE_TYPES:
        return True
    return obj_type in node_scope


def deprecation_scope_node_types(
    *,
    include_technical: bool,
    include_aggregates: bool,
    include_glossary: bool,
    include_security_tags: bool,
    include_downstream_assets: bool,
    include_business_assets: bool = True,
    include_responsibilities: bool | None = None,
) -> set[str]:
    """Return the node object-types a sync run with these flags is authoritative
    for — its deprecation in-scope set.

    A removed object of an in-scope type is deprecated even if the whole
    category is now empty; a type NOT in scope (because its category was
    excluded from the run) is never touched (preserves Bug-7717 category-aware
    deprecation).
    """
    scoped: set[str] = set(_ALWAYS_SCOPED_NODE_TYPES)
    if include_business_assets:
        scoped |= set(_BUSINESS_NODE_TYPES)
    if include_technical:
        scoped |= set(_TECHNICAL_NODE_TYPES)
        if include_aggregates:
            scoped.add("aggregate")
    if include_glossary:
        scoped.add("glossary_term")
    if include_security_tags:
        scoped.add("data_tag")
    if include_downstream_assets:
        scoped.add("downstream_asset")
    if include_responsibilities:
        scoped.add("responsibility")
    return scoped


async def load_mappings(db: AsyncSession, connection_id: UUID, mapping_cls):
    """Load existing mappings keyed by (object_type, tessallite_object_id)."""
    result = await db.execute(
        select(mapping_cls).where(mapping_cls.connection_id == connection_id)
    )
    mappings: dict[tuple[str, str]] = {}
    for m in result.scalars().all():
        key = (m.tessallite_object_type, m.tessallite_object_id)
        mappings[key] = m
    return mappings
