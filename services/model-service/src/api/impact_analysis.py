"""Impact Analysis read-only API (Bug-7787, spec §9).

Two endpoints on a new router:

- ``GET  .../impact-analysis/objects`` — paginated, searchable object catalogue
  for the explorer (safe object refs only).
- ``POST .../impact-analysis/query`` — inspect / delete / change what-if against
  the LIVE DRAFT, returning the full transitive impact set grouped by type with
  witness paths, severity, and a guard decision.

Both are PURE METADATA: no source/target DB access, no mutation. The engine
computes the complete reachable set for enforcement; only display paths are
capped (spec §7.5). ``analysis_id`` is a content hash safe to log (spec §9.3).

Viewer permission inspects; a change/delete preview additionally requires
modeller authority and rejects embed identities (spec §9.1).
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from shared.config.settings import get_settings
from shared.db.models import Model
from shared.db.session import get_tenant_db
from shared.model_dependency.graph import build_graph
from shared.model_dependency.impact import inspect as engine_inspect
from shared.model_dependency.impact import simulate_delete
from shared.model_dependency.snapshot import ModelDependencySnapshot
from shared.model_dependency.structural_paths import relationship_change_impact
from shared.model_dependency.types import (
    CONTRACT_VERSION,
    ImpactedObject,
    ImpactPath,
    ImpactResult,
    ImpactSummary,
    NodeKey,
    ObjectType,
)
from shared.schemas.domains.model_impact import (
    ImpactCatalogueItem,
    ImpactCatalogueResponse,
    ImpactGuard,
    ImpactItem,
    ImpactObjectRef,
    ImpactPathEdge,
    ImpactPathModel,
    ImpactQueryRequest,
    ImpactResponse,
    ImpactSummaryModel,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import caller_has_role, require_role
from src.dependencies.loader import ModelDependencyLoader

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/impact-analysis",
    tags=["impact-analysis"],
)

settings = get_settings()

# Object types whose deletion the guard treats as security-critical (mirror of the
# engine's SECURITY_OBJECT_TYPES; a hard/unresolved edge into these fails closed).
_VALID_OBJECT_TYPES = frozenset(t.value for t in ObjectType)


def _not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def _get_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise _not_found("Model not found")
    return model


async def _build_snapshot(db, project_id: UUID, model_id: UUID) -> ModelDependencySnapshot:
    loader = ModelDependencyLoader(db)
    return await loader.load(project_id, model_id)


# ---------------------------------------------------------------------------
# GET /objects — catalogue
# ---------------------------------------------------------------------------


@router.get("/objects", response_model=ImpactCatalogueResponse)
async def list_objects(
    project_id: UUID,
    model_id: UUID,
    object_types: Optional[str] = Query(
        None, description="Comma-separated object types to filter (e.g. measure,kpi)."
    ),
    search: Optional[str] = Query(None, description="Case-insensitive name substring filter."),
    cursor: Optional[str] = Query(None, description="Opaque pagination cursor from a prior page."),
    limit: Optional[int] = Query(None, ge=1),
    current_user: CurrentUser = Depends(forbid_embed_user),
    _auth=require_role("viewer"),
) -> ImpactCatalogueResponse:
    type_filter: Optional[set[str]] = None
    if object_types:
        requested = {t.strip() for t in object_types.split(",") if t.strip()}
        unknown = requested - _VALID_OBJECT_TYPES
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown object_type(s): {sorted(unknown)}",
            )
        type_filter = requested

    page_limit = min(
        limit or settings.IMPACT_CATALOGUE_PAGE_LIMIT,
        settings.IMPACT_CATALOGUE_MAX_LIMIT,
    )

    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)
        snapshot = await _build_snapshot(db, project_id, model_id)
        graph = build_graph(snapshot)

        # Deterministic order over all real nodes (exclude synthetic unresolved
        # nodes and cross-model foreign nodes — the catalogue lists THIS model's
        # objects the user can act on).
        nodes = [
            n for n in graph.nodes.values()
            if n.key.object_type != ObjectType.UNRESOLVED_REFERENCE
            and n.key.model_id == snapshot.model_id
        ]
        if type_filter is not None:
            nodes = [n for n in nodes if n.key.object_type.value in type_filter]
        if search:
            needle = search.lower()
            nodes = [
                n for n in nodes
                if needle in (n.name or "").lower() or needle in (n.display_name or "").lower()
            ]
        nodes.sort(key=lambda n: (n.key.object_type.value, n.display_name.lower(), n.key.object_id))

        total = len(nodes)
        start = _decode_cursor(cursor)
        page = nodes[start:start + page_limit]
        next_cursor = (
            _encode_cursor(start + page_limit) if start + page_limit < total else None
        )

        items = [
            ImpactCatalogueItem(
                object_type=n.key.object_type.value,
                object_id=n.key.object_id,
                model_id=n.key.model_id,
                name=n.name,
                display_name=n.display_name,
                container_ids=dict(n.container_ids),
                route=n.route,
            )
            for n in page
        ]
        return ImpactCatalogueResponse(
            project_id=str(project_id),
            model_id=str(model_id),
            dependency_revision=snapshot.dependency_revision,
            total=total,
            items=items,
            next_cursor=next_cursor,
        )
    raise _not_found()


def _encode_cursor(offset: int) -> str:
    return str(offset)


def _decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        offset = int(cursor)
        return max(0, offset)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor"
        )


# ---------------------------------------------------------------------------
# POST /query — inspect / delete / change what-if
# ---------------------------------------------------------------------------


@router.post("/query", response_model=ImpactResponse)
async def query_impact(
    project_id: UUID,
    model_id: UUID,
    body: ImpactQueryRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _auth=require_role("viewer"),
) -> ImpactResponse:
    # Validate the request shape (spec §9.2).
    if body.target.object_type not in _VALID_OBJECT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown target object_type: {body.target.object_type}",
        )
    if body.operation == "change" and body.change is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="change is required for operation=change",
        )
    if body.operation in ("inspect", "delete") and body.change is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="change is forbidden for inspect/delete",
        )

    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)

        # A change/delete preview matches the eventual action's authority: it
        # requires modeller (spec §9.1). inspect only needs viewer (already
        # enforced by the route dependency).
        if body.operation in ("delete", "change"):
            is_modeler = await caller_has_role(
                db, current_user, project_id, "modeler", model_id=model_id
            )
            if not is_modeler:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="change/delete preview requires modeler authority",
                )

        loader = ModelDependencyLoader(db)
        snapshot = await loader.load(project_id, model_id)

        max_paths = settings.IMPACT_MAX_PATHS_PER_OBJECT
        max_display = settings.IMPACT_MAX_DISPLAY_IMPACTS

        def _target(snap: ModelDependencySnapshot) -> NodeKey:
            return NodeKey(
                tenant_id=snap.tenant_id, project_id=snap.project_id,
                model_id=snap.model_id,
                object_type=ObjectType(body.target.object_type),
                object_id=str(body.target.object_id),
            )

        if body.operation == "change":
            # §7.3: apply the validated delta, build the proposed graph, and DIFF it
            # against the baseline — a change is NOT a deletion of the target. A
            # rename yields an empty change set; a rebind/definition surfaces only
            # the objects whose reachability actually changed.
            baseline_graph = build_graph(snapshot)
            proposed = _apply_change_delta(snapshot, body, loader)
            proposed_graph = build_graph(proposed)
            target_key = _target(snapshot)
            if baseline_graph.node(target_key) is None:
                raise _not_found("target object not found in model")
            # §7.4 structural reachability: a relationship change can make a
            # required table unreachable without touching any ID edge, so the
            # loader supplies each object's required-table set and the change
            # simulator runs a differential reachability pass for it.
            required_tables = await loader.object_required_tables()
            result = _simulate_change(
                baseline_graph, proposed_graph, target_key,
                baseline_snapshot=snapshot, proposed_snapshot=proposed,
                object_required_tables=required_tables,
                max_paths=max_paths, max_display=max_display,
            )
            return _to_response(result, proposed, body, str(project_id), str(model_id))

        graph = build_graph(snapshot)
        target_key = _target(snapshot)
        if graph.node(target_key) is None:
            raise _not_found("target object not found in model")
        if body.operation == "inspect":
            result = engine_inspect(
                graph, target_key,
                max_paths_per_object=max_paths, max_display_impacts=max_display,
            )
        else:  # delete
            result = simulate_delete(
                graph, target_key,
                max_paths_per_object=max_paths, max_display_impacts=max_display,
            )
        return _to_response(result, snapshot, body, str(project_id), str(model_id))
    raise _not_found()


# Severity ordering (mirror of the engine's _RESULT_SEVERITY_RANK): lower = more
# severe. Used to sort the change set hard-first and to merge by max severity.
_SEV_RANK = {"hard_break": 0, "soft_degrade": 1, "informational": 2}


# A change that leaves the target with a newly-invalid OWN binding fails closed:
# the target itself is hard_break so the guard blocks (spec §5.5, §7.6). Reason
# key for that synthesized target impact.
_TARGET_BROKEN_REASON = "impactAnalysis.reason.targetBindingBroken"


def _simulate_change(
    baseline_graph,
    proposed_graph,
    target: NodeKey,
    *,
    baseline_snapshot: Optional[ModelDependencySnapshot] = None,
    proposed_snapshot: Optional[ModelDependencySnapshot] = None,
    object_required_tables: Optional[dict] = None,
    max_paths: int,
    max_display: int,
) -> ImpactResult:
    """Change what-if (spec §7.3): DIFF the proposed graph against the baseline.

    The target SURVIVES a change (it is not deleted), so the simulator must
    capture two dimensions the delete-sim cannot:

    1. The target's DEPENDENTS whose impact state changed (symmetric diff over the
       baseline and proposed inspects — newly broken/degraded AND newly satisfied).
    2. The target's OWN resulting validity: a rebind/definition that leaves the
       target pointing at a missing binding makes the TARGET itself hard_break.
       inspect() seeds the target as ``removed`` and cascade-absorbs the target's
       owned unresolved node, so it alone would report that as informational and
       the guard would NOT block. We therefore inspect the target's OWN
       ``owner -> unresolved`` edges in the proposed graph directly and, if any
       are NEW (not present in baseline), synthesize a hard_break on the target so
       the guard fails closed (§5.5, §7.6 "a binding that cannot resolve is hard").

    A rename (no edge moves) yields identical inspects and no new target-owned
    unresolved edge -> empty change set."""
    from dataclasses import replace as _replace

    # Guard-complete: diff over the FULL reachable set (large display cap) so
    # truncation can never drop a hard break from the change guard (§7.5). Display
    # is capped afterwards.
    guard_cap = max(max_display, 10 ** 9)
    base = engine_inspect(
        baseline_graph, target, max_paths_per_object=max_paths,
        max_display_impacts=guard_cap,
    )
    prop = engine_inspect(
        proposed_graph, target, max_paths_per_object=max_paths,
        max_display_impacts=guard_cap,
    )
    base_by_key = {i.node.key: i for i in base.impacts}
    prop_by_key = {i.node.key: i for i in prop.impacts}

    changed: list = []
    # Symmetric diff over the union of impacted keys.
    for key in prop_by_key.keys() | base_by_key.keys():
        pimp = prop_by_key.get(key)
        bimp = base_by_key.get(key)
        p_sev = pimp.severity if pimp else None
        b_sev = bimp.severity if bimp else None
        if p_sev == b_sev:
            continue  # unchanged (incl. the common target-removal effect)
        if pimp is not None:
            # Newly impacted or severity escalated/de-escalated by the change.
            changed.append(pimp)
        else:
            # Impacted in baseline, no longer impacted in proposed: the change
            # SATISFIED it. Surface as informational/cleanup so the preview shows
            # what the change fixed (the wire contract has no "satisfied" severity;
            # informational/cleanup is the neutral representation).
            changed.append(_replace(
                bimp, severity="informational", effect="cleanup",
                reason_key="impactAnalysis.reason.changeResolved",
            ))

    # The target's OWN newly-invalid bindings (spec §5.5, §7.6). Any owned
    # unresolved node present in proposed but not baseline means the change left
    # the target with an unresolvable required binding -> hard_break on the target.
    new_target_unresolved = _new_owned_unresolved(baseline_graph, proposed_graph, target)
    if new_target_unresolved:
        tnode = proposed_graph.node(target)
        changed.insert(0, ImpactedObject(
            node=tnode, severity="hard_break", effect="breaks_reference",
            delete_policy="restrict", direct=True, min_depth=0,
            reason_key=_TARGET_BROKEN_REASON,
            reason_params={"field": new_target_unresolved[0]},
            paths=(), scc_id=None,
        ))

    # §12.6 / §10.2 CLS weakening: a classification change that REMOVES a column
    # from a CLS-restricted data tag, or REMOVES a tag restriction from a persona,
    # unmasks previously-restricted data. The dependent-severity diff is empty
    # (removing the C -> tag membership edge does not change the tag's dependents),
    # so without this the weakening previews "allowed". Surface it as a
    # soft_degrade so the guard requires acknowledgement (silently weakening a
    # policy is not acceptable, §7.6).
    if (
        target.object_type in (ObjectType.DATA_TAG, ObjectType.PERSONA)
        and baseline_snapshot is not None
        and proposed_snapshot is not None
    ):
        for imp in _classification_weakening_impacts(
            baseline_snapshot, proposed_snapshot, target, proposed_graph
        ):
            if not any(i.node.key == imp.node.key for i in changed):
                changed.append(imp)

    # §7.4 structural reachability: a RELATIONSHIP change can make a required table
    # unreachable (e.g. a re-point or an endpoint rebind to a missing table) without
    # touching any ID edge the inspect diff sees. Compute, per object, whether a
    # required table reachable in the baseline join graph is UNREACHABLE in the
    # proposed one -> hard_break. Fail closed: an object that loses required-table
    # reachability blocks the change (spec §7.4, §7.6).
    if (
        target.object_type == ObjectType.RELATIONSHIP
        and baseline_snapshot is not None
        and proposed_snapshot is not None
    ):
        # (a) A relationship endpoint rebound to a table/column that does not exist
        # in the proposed model fails the change closed on the relationship itself.
        # The engine's relationship edge builder resolves endpoints directly (not
        # via the unresolved-node path), so a missing endpoint would otherwise be a
        # silently-dropped dangling edge — a fail-open preview.
        missing_field = _relationship_missing_endpoint(proposed_snapshot, target.object_id)
        if missing_field:
            tnode = proposed_graph.node(target)
            if tnode is not None and not any(
                i.node.key == target for i in changed
            ):
                changed.insert(0, ImpactedObject(
                    node=tnode, severity="hard_break", effect="breaks_reference",
                    delete_policy="restrict", direct=True, min_depth=0,
                    reason_key=_TARGET_BROKEN_REASON,
                    reason_params={"field": missing_field}, paths=(), scc_id=None,
                ))
        # (b) Differential join reachability (§7.4): an object whose required table
        # becomes unreachable. Uses the loader's per-object required-table sets;
        # alternate valid join paths are NOT a false hard break.
        if object_required_tables:
            # Merge by MAX severity, not first-come: a reachability hard_break must
            # never be shadowed by an already-present lower-severity (e.g.
            # informational "changeResolved") entry for the same object. Today the
            # sets are disjoint (RELATIONSHIP_PATH edges are unwired), but wiring
            # them later must not silently downgrade a hard break to allowed.
            by_id = {i.node.key.object_id: idx for idx, i in enumerate(changed)}
            for imp in _relationship_reachability_impacts(
                baseline_snapshot, proposed_snapshot, object_required_tables, proposed_graph
            ):
                oid = imp.node.key.object_id
                if oid not in by_id:
                    by_id[oid] = len(changed)
                    changed.append(imp)
                else:
                    existing = changed[by_id[oid]]
                    if _SEV_RANK.get(imp.severity, 3) < _SEV_RANK.get(existing.severity, 3):
                        changed[by_id[oid]] = imp

    # Summary counts over the FULL change set (never truncated), mirroring the
    # engine's ImpactSummary contract.
    hard = sum(1 for i in changed if i.severity == "hard_break")
    soft = sum(1 for i in changed if i.severity == "soft_degrade")
    cascade = sum(1 for i in changed if i.effect == "cascade_deleted")
    by_type: dict[str, int] = {}
    max_depth = 0
    unresolved = 0
    for i in changed:
        by_type[i.node.key.object_type.value] = by_type.get(i.node.key.object_type.value, 0) + 1
        max_depth = max(max_depth, i.min_depth)
        if i.node.key.object_type == ObjectType.UNRESOLVED_REFERENCE:
            unresolved += 1

    # Severity-sort BEFORE truncating (mirror the engine's _impact_sort_key:
    # hard_break < soft_degrade < informational). This guarantees every hard break
    # survives the display cap, so the guard — which reads result.impacts in
    # _to_response — can never miss a blocking hard break to truncation (§7.5:
    # truncation must never change a guard decision). Without the sort, a change
    # that hard-breaks >max_display dependents could push a blocker past the cap.
    changed.sort(key=lambda i: (
        _SEV_RANK.get(i.severity, 3),
        i.min_depth,
        i.node.key.object_type.value,
        i.node.display_name.lower(),
        i.node.key.object_id,
    ))
    truncated = len(changed) > max_display
    display = tuple(changed[:max_display]) if truncated else tuple(changed)
    summary = ImpactSummary(
        total=len(changed), hard_break=hard, soft_degrade=soft,
        cascade_deleted=cascade, direct=sum(1 for i in changed if i.direct),
        max_depth=max_depth, by_object_type=by_type,
        truncated=truncated, unresolved=unresolved,
    )
    return _replace(
        prop, operation="change", impacts=display, summary=summary,
    )


def _classification_weakening_impacts(
    baseline_snapshot: ModelDependencySnapshot,
    proposed_snapshot: ModelDependencySnapshot,
    target: NodeKey,
    proposed_graph,
) -> list[ImpactedObject]:
    """Impacts for a classification change that WEAKENS CLS (§12.6, §10.2).

    - data_tag target: a column REMOVED from a tag that is CLS-restricted by any
      persona unmasks that column for restricted personas.
    - persona target: a tag restriction REMOVED from the persona drops a CLS
      policy target.

    Both are surfaced as ``soft_degrade`` so the guard requires acknowledgement —
    a deliberate policy weakening must never preview as a silent no-op."""
    out: list[ImpactedObject] = []
    tid = target.object_id
    if target.object_type == ObjectType.DATA_TAG:
        base_tag = next((t for t in baseline_snapshot.data_tags if t.id == tid), None)
        prop_tag = next((t for t in proposed_snapshot.data_tags if t.id == tid), None)
        if base_tag is None or prop_tag is None:
            return out
        removed = set(base_tag.column_ids) - set(prop_tag.column_ids)
        cls_restricted = any(
            tid in p.restricted_data_tag_ids for p in proposed_snapshot.personas
        )
        if removed and cls_restricted:
            node = proposed_graph.node(target)
            if node is not None:
                out.append(ImpactedObject(
                    node=node, severity="soft_degrade", effect="loses_visibility",
                    delete_policy="detach", direct=True, min_depth=0,
                    reason_key="impactAnalysis.reason.clsWeakened",
                    reason_params={"removed_column_count": str(len(removed))},
                    paths=(), scc_id=None,
                ))
    elif target.object_type == ObjectType.PERSONA:
        base_p = next((p for p in baseline_snapshot.personas if p.id == tid), None)
        prop_p = next((p for p in proposed_snapshot.personas if p.id == tid), None)
        if base_p is None or prop_p is None:
            return out
        removed = set(base_p.restricted_data_tag_ids) - set(prop_p.restricted_data_tag_ids)
        if removed:
            node = proposed_graph.node(target)
            if node is not None:
                out.append(ImpactedObject(
                    node=node, severity="soft_degrade", effect="loses_visibility",
                    delete_policy="detach", direct=True, min_depth=0,
                    reason_key="impactAnalysis.reason.clsWeakened",
                    reason_params={"removed_restriction_count": str(len(removed))},
                    paths=(), scc_id=None,
                ))
    return out


def _relationship_missing_endpoint(
    proposed_snapshot: ModelDependencySnapshot, relationship_id: str
) -> Optional[str]:
    """Return the first relationship endpoint field whose target table/column does
    not exist in the proposed model, else None. Used to fail a relationship change
    closed when an endpoint was rebound to a missing table/column (§7.4, §7.6)."""
    rel = next((r for r in proposed_snapshot.relationships if r.id == relationship_id), None)
    if rel is None:
        return None
    table_ids = {t.id for t in proposed_snapshot.tables}
    column_ids = {c.id for c in proposed_snapshot.columns}
    for tid, field in ((rel.left_table_id, "left_table_id"),
                       (rel.right_table_id, "right_table_id")):
        if tid and tid not in table_ids:
            return field
    # A join column endpoint is required (Join.*_column_id is NOT NULL): an
    # explicit-null OR missing column endpoint cannot compile -> fail closed for
    # preview consistency (the write would also reject it).
    for cid, field in ((rel.left_column_id, "left_column_id"),
                       (rel.right_column_id, "right_column_id")):
        if not cid or cid not in column_ids:
            return field
    return None


def _relationship_reachability_impacts(
    baseline_snapshot: ModelDependencySnapshot,
    proposed_snapshot: ModelDependencySnapshot,
    object_required_tables: dict,
    proposed_graph,
) -> list[ImpactedObject]:
    """Objects whose join reachability changed after a relationship change (§7.4),
    delegated to the SEALED engine module ``structural_paths.relationship_change_
    impact`` (single source of truth — no re-implemented reachability here). It
    returns hard-break object ids (a required table became unreachable; an
    alternate valid path is NOT a false hard break), soft-degrade object ids (a
    required table stayed reachable but at a changed shortest-path cost, §7.4.7),
    and one deterministic witness table path. The API maps those ids to
    ``ImpactedObject``s and carries the witness path on the first hard break so the
    §11.2 panel can render the lost route."""
    rel_impact = relationship_change_impact(
        baseline_snapshot.relationships, proposed_snapshot.relationships,
        object_required_tables=object_required_tables,
    )
    witness = _table_witness_path(rel_impact.witness_path)
    out: list[ImpactedObject] = []
    for i, object_id in enumerate(rel_impact.hard_break_object_ids):
        node = _object_node(proposed_snapshot, proposed_graph, object_id)
        if node is None:
            continue
        out.append(ImpactedObject(
            node=node, severity="hard_break", effect="breaks_reference",
            delete_policy="restrict", direct=True, min_depth=1,
            reason_key="impactAnalysis.reason.relationshipPath",
            reason_params={}, paths=(witness,) if (i == 0 and witness) else (),
            scc_id=None,
        ))
    for object_id in rel_impact.soft_degrade_object_ids:
        node = _object_node(proposed_snapshot, proposed_graph, object_id)
        if node is None:
            continue
        out.append(ImpactedObject(
            node=node, severity="soft_degrade", effect="changes_semantics",
            delete_policy="recompute", direct=True, min_depth=1,
            reason_key="impactAnalysis.reason.relationshipPath",
            reason_params={}, paths=(), scc_id=None,
        ))
    return out


def _table_witness_path(table_tokens: tuple[str, ...]):
    """Wrap the engine's deterministic table-id witness path (§7.4.8) as an
    ImpactPath of table node tokens so it renders in the impact panel."""
    if not table_tokens:
        return None
    return ImpactPath(
        nodes=tuple(f"{ObjectType.TABLE.value}:{tid}" for tid in table_tokens),
        edges=(),
    )


def _object_node(proposed_snapshot: ModelDependencySnapshot, proposed_graph, object_id: str):
    return proposed_graph.node(NodeKey(
        tenant_id=proposed_snapshot.tenant_id, project_id=proposed_snapshot.project_id,
        model_id=proposed_snapshot.model_id,
        object_type=_object_type_for(object_id, proposed_snapshot),
        object_id=object_id,
    ))


def _object_type_for(object_id: str, snapshot: ModelDependencySnapshot) -> ObjectType:
    """Resolve an object_id from object_required_tables back to its ObjectType.
    The loader only builds required-tables for measures and dimensions."""
    if any(m.id == object_id for m in snapshot.measures):
        return ObjectType.MEASURE
    return ObjectType.DIMENSION


def _new_owned_unresolved(baseline_graph, proposed_graph, target: NodeKey) -> list[str]:
    """Fields of the target that became newly unresolved by the change: an
    ``owner(target) -> unresolved_reference`` edge present in the PROPOSED graph
    but not the baseline. These are the target's own broken bindings that must
    fail the change closed (§5.5, §7.6)."""
    def _owned(graph) -> set[str]:
        out: set[str] = set()
        for e in graph.dependents_of(target):
            if e.dependent.object_type == ObjectType.UNRESOLVED_REFERENCE:
                out.add(e.source_field)
        return out
    base_fields = _owned(baseline_graph)
    return sorted(_owned(proposed_graph) - base_fields)


# Allow-list of (change_kind -> object_type -> settable snapshot fields) the
# change simulator can apply (spec §7.3). A field outside its class returns 422
# with a stable code — never a misleading generic preview (spec §7.3).
_CHANGE_ALLOW: dict[str, dict[str, set[str]]] = {
    # rename does not move an ID-keyed dependency edge (edges bind IDs, not
    # names); the loader has already resolved every reference to an ID, so a
    # rename produces the same dependency structure. Accepted for the full set of
    # renamable objects (spec §7.3) as a no-op-on-graph preview.
    "rename": {
        "table": {"name"}, "column": {"name"}, "user_defined_attribute": {"name"},
        "dimension": {"name"}, "hierarchy": {"name"}, "measure": {"name"},
        "kpi": {"name"}, "named_list": {"name"},
    },
    "rebind": {
        "measure": {"source_column_id", "user_defined_attribute_id",
                    "calendar_model_table_id", "hierarchy_id", "resolved_calendar_id"},
        "dimension": {"source_column_id", "display_column_id", "user_defined_attribute_id"},
    },
    "definition": {
        "measure": {"calc_expression"},
        "dimension": {"calc_expression"},
    },
    "relationship": {
        "relationship": {"left_table_id", "right_table_id",
                         "left_column_id", "right_column_id"},
    },
    "classification": {
        "data_tag": {"column_ids"},
        "persona": {"restricted_data_tag_ids"},
    },
}


def _apply_change_delta(
    snapshot: ModelDependencySnapshot,
    body: ImpactQueryRequest,
    loader: ModelDependencyLoader,
) -> ModelDependencySnapshot:
    """Change simulation (spec §7.3): copy the normalized snapshot, apply a
    validated field delta to the target's row, rebuild, and let the caller diff.
    Supports every v1 change class — rename, rebind, definition, relationship,
    classification. A field outside its change_kind's allow-list returns 422 with
    a stable code (never a misleading generic preview)."""
    from dataclasses import replace

    change = body.change
    target_id = str(body.target.object_id)
    target_type = body.target.object_type

    class_map = _CHANGE_ALLOW.get(change.change_kind, {})
    supported = class_map.get(target_type, set())
    unsupported = [f for f in change.changed_fields if f not in supported]
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "IMPACT_CHANGE_UNSUPPORTED",
                "message_key": "impactAnalysis.change.unsupported",
                "message": (
                    f"unsupported {change.change_kind} field(s) for "
                    f"{target_type}: {unsupported}"
                ),
            },
        )
    # Every declared changed_field must carry a proposed value; otherwise the delta
    # would silently null the field (dropping a real binding / producing a None
    # object_id). Reject with the same stable code rather than a misleading preview.
    missing = [f for f in change.changed_fields if f not in change.proposed_values]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "IMPACT_CHANGE_MISSING_VALUE",
                "message_key": "impactAnalysis.change.missingValue",
                "message": f"changed_fields missing a proposed value: {missing}",
            },
        )

    def _proposed(field: str):
        return change.proposed_values.get(field)

    def _proposed_id(field: str) -> Optional[str]:
        val = change.proposed_values.get(field)
        return str(val) if val is not None else None

    # rename: no edge moves; return the snapshot unchanged (the graph structure is
    # identical, so the preview shows the target's current dependents — which is
    # the correct answer: renaming an object breaks nothing structurally).
    if change.change_kind == "rename":
        return snapshot

    if target_type == "measure":
        return _apply_measure_change(snapshot, target_id, change, loader, _proposed_id, replace)
    if target_type == "dimension":
        return _apply_dimension_change(snapshot, target_id, change, loader, _proposed_id, replace)
    if target_type == "relationship":
        rels = list(snapshot.relationships)
        for i, r in enumerate(rels):
            if r.id == target_id:
                updates = {f: _proposed_id(f) for f in change.changed_fields}
                rels[i] = replace(r, **updates)
        return replace(snapshot, relationships=tuple(rels))
    if target_type == "data_tag":
        tags = list(snapshot.data_tags)
        for i, t in enumerate(tags):
            if t.id == target_id and "column_ids" in change.changed_fields:
                new_cols = tuple(str(c) for c in (_proposed("column_ids") or []))
                tags[i] = replace(t, column_ids=new_cols)
        return replace(snapshot, data_tags=tuple(tags))
    if target_type == "persona":
        personas = list(snapshot.personas)
        for i, p in enumerate(personas):
            if p.id == target_id and "restricted_data_tag_ids" in change.changed_fields:
                new_tags = tuple(str(x) for x in (_proposed("restricted_data_tag_ids") or []))
                personas[i] = replace(p, restricted_data_tag_ids=new_tags)
        return replace(snapshot, personas=tuple(personas))
    return snapshot


# Required binding fields per object type: a rebind that sets one of these to
# NULL/empty leaves the object with no binding at all and must fail closed (the
# engine only creates an unresolved node for a NON-None dangling id, so an
# explicit null would otherwise slip through with an empty diff, §5.5/§7.6).
# Only a base measure's source_column_id is unconditionally required: a dimension
# may legitimately be calc-backed (no source column), so nulling a dimension's
# source_column_id is a valid unbind, not a fail-closed condition.
_REQUIRED_REBIND_FIELDS = {
    "measure": {"source_column_id"},
}


def _rebind_nulls_required(change, target_type: str) -> Optional[str]:
    """Return the first required binding field a rebind sets to null/empty, else
    None. An explicit ``null`` (or empty string) on a required binding is a
    fail-closed condition, not a valid unbind, for a base measure/dimension."""
    required = _REQUIRED_REBIND_FIELDS.get(target_type, set())
    for f in change.changed_fields:
        if f in required:
            val = change.proposed_values.get(f)
            if val is None or (isinstance(val, str) and not val.strip()):
                return f
    return None


def _apply_measure_change(snapshot, target_id, change, loader, _proposed_id, replace):
    measures = list(snapshot.measures)

    def _fail_closed(reason: str, field: str = "calc_expression"):
        snap = replace(snapshot, measures=tuple(measures))
        return replace(snap, unresolved_definitions=tuple(
            snap.unresolved_definitions
            + (("measure", target_id, field, reason),)
        ))

    for i, m in enumerate(measures):
        if m.id != target_id:
            continue
        if change.change_kind == "definition":
            expr = change.proposed_values.get("calc_expression")
            expr_str = str(expr) if expr else ""
            # An emptied calc expression fails closed only for a CALCULATED measure
            # (no source column backing). A base measure with a live source_column_id
            # survives an emptied vestigial expression (mirrors the dimension rule).
            if not expr_str.strip():
                measures[i] = replace(m, calc_expression=None, calc_reference_ids=())
                if not m.source_column_id:
                    return _fail_closed("proposed_empty_definition")
                continue
            refs = loader.resolve_calc_measure_expression(expr_str)
            if refs is None:
                # Proposed definition does not parse OR references a missing/
                # ambiguous measure -> fail closed so the guard blocks (§5.5, §12.7).
                measures[i] = replace(m, calc_expression=expr_str, calc_reference_ids=())
                return _fail_closed("proposed_parse_failure")
            measures[i] = replace(m, calc_expression=expr_str, calc_reference_ids=refs)
        else:  # rebind
            nulled = _rebind_nulls_required(change, "measure")
            updates = {f: _proposed_id(f) for f in change.changed_fields}
            measures[i] = replace(m, **updates)
            if nulled:
                # Report the actually-nulled binding field, not calc_expression.
                return _fail_closed("proposed_null_required_binding", field=nulled)
    return replace(snapshot, measures=tuple(measures))


def _apply_dimension_change(snapshot, target_id, change, loader, _proposed_id, replace):
    dims = list(snapshot.dimensions)

    def _fail_closed(reason: str):
        snap = replace(snapshot, dimensions=tuple(dims))
        return replace(snap, unresolved_definitions=tuple(
            snap.unresolved_definitions
            + (("dimension", target_id, "calc_expression", reason),)
        ))

    for i, d in enumerate(dims):
        if d.id != target_id:
            continue
        if change.change_kind == "definition":
            expr = change.proposed_values.get("calc_expression")
            expr_str = str(expr) if expr else ""
            # An emptied definition on a CALCULATED dimension (one with no source
            # column backing) leaves it undefined -> fail closed. A dimension that
            # still has a source_column_id survives an emptied calc expression.
            if not expr_str.strip():
                if not d.source_column_id:
                    dims[i] = replace(d, calc_expression=None,
                                      calc_expression_tables=(), calc_expression_column_ids=())
                    return _fail_closed("proposed_empty_definition")
                dims[i] = replace(d, calc_expression=None,
                                  calc_expression_tables=(), calc_expression_column_ids=())
                continue
            resolved = loader.resolve_calc_dimension_expression(expr_str)
            if resolved is None:
                dims[i] = replace(d, calc_expression=expr_str,
                                  calc_expression_tables=(), calc_expression_column_ids=())
                return _fail_closed("proposed_parse_failure")
            table_ids, col_ids = resolved
            dims[i] = replace(d, calc_expression=expr_str,
                              calc_expression_tables=table_ids,
                              calc_expression_column_ids=col_ids)
        else:  # rebind
            updates = {f: _proposed_id(f) for f in change.changed_fields}
            dims[i] = replace(d, **updates)
    return replace(snapshot, dimensions=tuple(dims))


# ---------------------------------------------------------------------------
# Result -> wire contract mapping
# ---------------------------------------------------------------------------


def _analysis_id(
    snapshot: ModelDependencySnapshot, body: ImpactQueryRequest
) -> str:
    """Content hash of tenant/project/model + revision + normalized request +
    contract version (spec §9.3). Safe to log; reveals no model content."""
    payload = {
        "tenant_id": snapshot.tenant_id,
        "project_id": snapshot.project_id,
        "model_id": snapshot.model_id,
        "dependency_revision": snapshot.dependency_revision,
        "operation": body.operation,
        "target_type": body.target.object_type,
        "target_id": str(body.target.object_id),
        "change": body.change.model_dump(mode="json") if body.change else None,
        "include_cross_model": body.include_cross_model,
        "contract_version": CONTRACT_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _object_ref(node) -> ImpactObjectRef:
    return ImpactObjectRef(
        object_type=node.key.object_type.value,
        object_id=node.key.object_id,
        model_id=node.key.model_id,
        name=node.name,
        display_name=node.display_name,
        route=node.route,
    )


def _guard_decision(result: ImpactResult) -> ImpactGuard:
    """Compute the guard decision from the impact result (spec §10.1).

    - block on any surviving hard_break (including unresolved refs, which fail
      closed as hard) — read from the impacts, not summary.unresolved;
    - require acknowledgement for soft_degrade / detach / invalidate / recompute;
    - allow pure cleanup / cascade without a separate acknowledgement.

    ``blocked_unresolved`` is surfaced when any unresolved reference is present
    (spec §9.4 IMPACT_UNRESOLVED_REFERENCE), so the caller can show the precise
    fail-closed reason. This is READ-ONLY — it never changes a delete outcome.
    """
    hard_ids: list[str] = []
    ack_needed = False
    has_unresolved = False
    for imp in result.impacts:
        if imp.node.key.object_type == ObjectType.UNRESOLVED_REFERENCE:
            has_unresolved = True
        if imp.severity == "hard_break":
            hard_ids.append(_impact_id(imp))
        elif imp.severity == "soft_degrade":
            ack_needed = True
        elif imp.effect in ("detached", "stale") or imp.delete_policy in (
            "detach", "invalidate", "recompute"
        ):
            # cascade_deleted / cleanup do not by themselves require ack.
            if imp.effect not in ("cascade_deleted", "cleanup"):
                ack_needed = True

    if hard_ids:
        decision = "blocked_unresolved" if has_unresolved and _all_unresolved(result, hard_ids) else "blocked"
        return ImpactGuard(
            decision=decision, blocking_impact_ids=hard_ids, acknowledgement_required=False
        )
    if ack_needed:
        return ImpactGuard(
            decision="acknowledgement_required", blocking_impact_ids=[],
            acknowledgement_required=True,
        )
    return ImpactGuard(decision="allowed", blocking_impact_ids=[], acknowledgement_required=False)


def _all_unresolved(result: ImpactResult, hard_ids: list[str]) -> bool:
    hard_set = set(hard_ids)
    for imp in result.impacts:
        if _impact_id(imp) in hard_set and imp.node.key.object_type != ObjectType.UNRESOLVED_REFERENCE:
            return False
    return True


def _impact_id(imp) -> str:
    """Stable id for one impacted object within an analysis (its node token)."""
    return imp.node.key.token()


def _to_response(
    result: ImpactResult,
    snapshot: ModelDependencySnapshot,
    body: ImpactQueryRequest,
    project_id: str,
    model_id: str,
) -> ImpactResponse:
    # The guard and summary ALWAYS run on the FULL engine result (cross-model
    # included) — a guard decision may never depend on a display flag (spec §9.2).
    # ``include_cross_model=false`` is honoured only as a DISPLAY filter for
    # exploratory inspect: it hides foreign-model impacts from the returned list
    # without changing summary counts or the guard. It is ignored for delete/change
    # (correctness must include cross-model there).
    guard = _guard_decision(result)
    hide_cross_model = (
        result.operation == "inspect" and not body.include_cross_model
    )

    def _shown(imp) -> bool:
        if not hide_cross_model:
            return True
        return imp.node.key.model_id == snapshot.model_id

    impacts = [
        ImpactItem(
            impact_id=_impact_id(imp),
            object=_object_ref(imp.node),
            severity=imp.severity,
            effect=imp.effect,
            delete_policy=imp.delete_policy,
            direct=imp.direct,
            min_depth=imp.min_depth,
            reason_key=imp.reason_key,
            reason_params=dict(imp.reason_params),
            paths=[
                ImpactPathModel(
                    nodes=list(p.nodes),
                    edges=[
                        ImpactPathEdge(kind=e.kind.value, source_field=e.source_field)
                        for e in p.edges
                    ],
                )
                for p in imp.paths
            ],
            scc_id=imp.scc_id,
        )
        for imp in result.impacts
        if _shown(imp)
    ]
    summary = ImpactSummaryModel(
        total=result.summary.total,
        hard_break=result.summary.hard_break,
        soft_degrade=result.summary.soft_degrade,
        cascade_deleted=result.summary.cascade_deleted,
        direct=result.summary.direct,
        max_depth=result.summary.max_depth,
        by_object_type=dict(result.summary.by_object_type),
        truncated=result.summary.truncated,
        unresolved=result.summary.unresolved,
    )
    return ImpactResponse(
        analysis_id=_analysis_id(snapshot, body),
        authority="live_draft",
        project_id=project_id,
        model_id=model_id,
        dependency_revision=snapshot.dependency_revision,
        operation=result.operation,
        target=_object_ref(result.target),
        guard=guard,
        summary=summary,
        impacts=impacts,
        cycles=[list(c) for c in result.cycles],
        diagnostics=[dict(d) for d in result.diagnostics],
    )
