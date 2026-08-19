"""Phase 5 LIVE serving adapter for the derived-grain proof engine.

Spec: architecture_derived-grain-aggregate-routing.md §16 Phase 5 (== rollout
stages 4 + 5), §7 (proof), §7.6.4 (trust predicate), §8.2 (exact direct read),
§9 (RLS/CLS), §14.2 (known-value cases).

SCOPE (critical). Phase 5 serves ONLY EXACT-verdict routes. Two EXACT routes exist:
  - Stage 5: exact EXPRESSION-KEY identity (an inline GROUP BY expression whose
    canonical fingerprint equals a materialised artifact grain key — §8.2).
    LIVE-WIRED this lane.
  - Stage 4: strict BIJECTION attribute relabel (group by a detail label, served
    from an id-keyed aggregate by projecting the detail passenger — §8.2). The
    adapter NOW emits kind=ATTRIBUTE requests (from the binder's
    ``BoundAttributeRelabel`` records) and kind=PHYSICAL requests for unchanged
    group keys, both carrying canonical ids + ordered lineage. The whole path is
    behind the operational kill-switch ``query.derived_expression_serving_enabled``
    (DEFAULT ON — strategy_derived-grain-operational-serving.md §A); an edge
    earns no serving trust unless it is CURRENTLY healthy (VERIFIED artifact-local
    evidence bound to the active run — the trust predicate), fail-closed.
ROLLUP / N:1 coarsening / time-coarsening serving is Phase 6 and is NOT served
here: a ROLLUP or SOURCE_ONLY proof falls back to SOURCE, byte-identical.

This module is the BRIDGE between the router and the pure Phase-4 proof engine:
  - ``build_candidate_manifest`` (pure): turns ONE candidate ``AggregateDefinition``
    manifest (grain_keys / attribute_edges / passenger_columns / active_refresh_run_id
    / manifest_hash) + already-loaded evidence + the deployed declaration into a
    ``CandidateManifest`` with REAL grain-key lineage + edge key_grain_fingerprint,
    so the Phase-4 §7.3 set-cover and §7.4 leaf-lineage guards operate on real data.
    It also resolves Bug-7806: the BUILT physical passenger/grain column NAMES come
    from the manifest, never a logical-name guess.
  - ``build_query_key_requests`` (pure): turns the bound query's group-by keys into
    ``QueryKeyRequest``s (physical / expression / attribute).
  - ``load_candidate_trust_inputs`` (async): loads the newest matching evidence row
    per carried relationship and the deployed declaration, so the pure builder stays
    DB-free.

Fail-closed everywhere: any missing manifest field, unresolved name, absent
evidence, or exception yields a candidate that the proof engine rejects to
SOURCE_ONLY. The router NEVER serves on a non-EXACT verdict in Phase 5.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    AggregateDefinition,
    DimensionAttributeRelationship,
    DimensionAttributeVerification,
)
from src.ir.logical_query import BoundQuery
from src.routing.derived_expression_proof import (
    EXACT,
    CandidateManifest,
    MeasureRequest,
    QueryKeyRequest,
    build_serve_proof,
)
from src.routing.derived_trust_predicate import TrustInputs

logger = logging.getLogger(__name__)

# Bug-7905 evidence-AGE bound. A VERIFIED relationship-health evidence row is a
# serving authority only while FRESH: within ``_EVIDENCE_AGE_MULTIPLIER`` x the
# model's ``optimizer.derived_relationship_sweep_interval_hours`` cadence. The
# multiplier tolerates a couple of missed/late sweeps (a single skipped run does
# not drop a healthy edge) while still bounding a stale VERIFIED serve: past the
# bound the sweep that would have demoted a broken edge either never ran (scheduler
# outage) or its demotion write kept rolling back, so the row is treated as EXPIRED
# and the relabel is NOT served (falls back to normal routing).
_EVIDENCE_AGE_MULTIPLIER = 3
# Fallback cadence (hours) mirroring the registry default for
# ``optimizer.derived_relationship_sweep_interval_hours`` — used only to size the
# age bound; the real cadence is read per-model. Kept aligned with the registry.
_DEFAULT_SWEEP_INTERVAL_HOURS = 24


async def _resolve_evidence_age_bound(
    db: AsyncSession, model_id: Any,
) -> Optional[timedelta]:
    """Resolve the max age a VERIFIED health-evidence row may serve at (Bug-7905).

    Returns ``_EVIDENCE_AGE_MULTIPLIER x`` the model's relationship-sweep cadence as
    a ``timedelta``, or ``None`` when the cadence cannot be read at all. ``None``
    means FAIL CLOSED: the caller then marks every edge's evidence as not-fresh so a
    doubtful cadence read never lets a stale VERIFIED row keep serving.
    """
    try:
        from shared.config.resolver import get_setting

        interval_hours = int(await get_setting(
            "optimizer.derived_relationship_sweep_interval_hours",
            tenant_session=db, model_id=model_id,
        ))
        if interval_hours <= 0:
            interval_hours = _DEFAULT_SWEEP_INTERVAL_HOURS
    except Exception:
        # Unreadable cadence -> fail closed (no bound the caller can trust).
        return None
    return timedelta(hours=interval_hours * _EVIDENCE_AGE_MULTIPLIER)


def _evidence_is_fresh(
    evidence: Any, now: datetime, age_bound: Optional[timedelta],
) -> bool:
    """True iff the evidence's ``checked_at`` is within ``age_bound`` of ``now``.

    Fail-closed: an unresolved age bound (None), a missing/None ``checked_at``, or an
    unparseable timestamp all return False, so the trust predicate rejects the edge
    and the query falls back to normal routing rather than serving un-rechecked data.
    A naive ``checked_at`` is treated as UTC (the sweep writes tz-aware UTC).
    """
    if evidence is None or age_bound is None:
        return False
    checked_at = getattr(evidence, "checked_at", None)
    if checked_at is None:
        return False
    if getattr(checked_at, "tzinfo", None) is None:
        try:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        except (AttributeError, TypeError):
            return False
    try:
        return (now - checked_at) <= age_bound
    except TypeError:
        return False


@dataclass
class DerivedServeContext:
    """Everything ``try_build_exact_proof`` needs, resolved by the router.

    Kept explicit so the proof build is a pure function of already-loaded state
    (no lazy ORM access, no hidden DB round-trips inside the proof engine).
    """
    bound_deployed_version_id: Optional[str]
    bound_deploy_epoch: int
    accepted_verifier_version: str
    # CLS/RLS lineage decision resolved by the router's existing security gates.
    # The query's directly-referenced columns are already CLS-checked upstream
    # (_check_column_restrictions); this flag covers that base decision.
    security_ok: bool = True
    # §9.1: for an attribute edge the router must ALSO confirm the internal KEY
    # column and the DETAIL column are CLS-authorised — the relabel must not
    # launder a restricted key through a permitted label. These are the persona's
    # restricted model_column_id strings; an edge whose key/detail id is in this
    # set is denied (security_ok=False for that edge). Empty => no CLS restriction.
    restricted_column_ids: frozenset[str] = frozenset()
    # Connector immutable source watermark when exposed, else None (§7.6.4 rule 6).
    connector_source_version: Optional[str] = None


def _manifest_list(value: Any) -> list[dict]:
    """Return a JSONB manifest column as a list of dicts, fail-closed to []."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def build_query_key_requests(bound_query: BoundQuery) -> list[QueryKeyRequest]:
    """Build the ordered ``QueryKeyRequest`` tuple for the query's GROUP BY.

    Emits three request kinds (spec §2.4):
      - EXPRESSION: an inline expression grain key (``BoundDerivedExpression`` with
        role GROUP_KEY) -> canonical fingerprint + DATE_TRUNC unit + ordered bound
        leaf column ids (§7.4 lineage) + the query's requested output alias (§8.2).
      - ATTRIBUTE: a resolved strict-bijection relabel (from the binder's
        ``BoundAttributeRelabel``) -> ``attr:<relationship-uuid>`` + ordered
        ``(key_column_id, detail_column_id)`` + declaration hash + owning dimension.
      - PHYSICAL: an unchanged group key -> ``dim:<dimension-uuid>`` +
        ``(source_column_id,)``. A bare grain the binder could not resolve to either
        is emitted as an identity-less PHYSICAL request -> never exact-covered ->
        the candidate rejects to source (fail-closed).

    The whole path is behind the operational kill-switch
    ``query.derived_expression_serving_enabled`` (DEFAULT ON); an ATTRIBUTE edge
    earns serving trust only while it is CURRENTLY healthy (the trust predicate).

    Returns an empty list when the shape is not a derived-grain candidate — no
    EXPRESSION key AND no resolved relabel — so the caller never invokes the proof
    engine and ordinary routing is unchanged.
    """
    lq = bound_query.logical_query
    keys: list[QueryKeyRequest] = []

    # Map each bound derived expression to the group-by keys it produces. A
    # BoundDerivedExpression whose occurrences include a GROUP_KEY role is a
    # group key; WHERE/ORDER-only expressions do not participate in the tuple.
    derived = list(getattr(bound_query, "bound_derived_expressions", []) or [])
    occ_by_id = {
        o.occurrence_id: o
        for o in (getattr(lq, "expression_occurrences", []) or [])
    }
    # §7.8 tuple completeness: EVERY group-key EXPRESSION occurrence must be covered
    # by a bound derived expression. If the binder failed to bind one (unknown
    # function, unresolved column), that group key has no proof coverage; serving
    # the remaining keys would drop a real grouping (e.g. a second CASE grain).
    # Track which GROUP_KEY occurrence ids a bound expression covers so any
    # uncovered one forces the candidate to reject (an unservable placeholder key).
    all_group_key_occ_ids = {
        o.occurrence_id
        for o in (getattr(lq, "expression_occurrences", []) or [])
        if getattr(o, "role", None) == "GROUP_KEY"
    }
    covered_group_key_occ_ids: set[str] = set()
    for bde in derived:
        roles = {
            occ_by_id[oid].role
            for oid in bde.occurrence_ids
            if oid in occ_by_id
        }
        if "GROUP_KEY" not in roles:
            continue
        covered_group_key_occ_ids.update(
            oid for oid in bde.occurrence_ids
            if oid in occ_by_id and occ_by_id[oid].role == "GROUP_KEY"
        )
        # DATE_TRUNC unit is a structural literal already retained in the
        # canonical AST / semantic context; a coarsening never serves in Phase 5
        # (EXACT only) but the unit is carried so the proof engine's exact path
        # and the (unserved) coarsening path both see identical inputs.
        time_unit = None
        sctx = bde.semantic_context or {}
        if isinstance(sctx, dict):
            time_unit = sctx.get("date_trunc_unit") or sctx.get("time_unit")
        input_column_ids = tuple(
            str(ref.column_id) for ref in (bde.inputs or []) if ref.column_id
        )
        # §8.2: project each key under the query's REQUESTED output alias so the
        # served column name matches the source path byte-for-byte for BI clients.
        # ``logical_name`` becomes ``DerivedKeyPlan.query_key`` -> the SQL alias.
        # Prefer the GROUP_KEY occurrence's output_alias; fall back to the SELECT
        # occurrence's alias, then the canonical SQL text when the query supplied
        # no alias at all.
        alias = _requested_alias_for(bde, occ_by_id) or bde.canonical_sql
        keys.append(
            QueryKeyRequest(
                logical_name=alias,
                kind="EXPRESSION",
                fingerprint=bde.expression_fingerprint,
                time_unit=str(time_unit) if time_unit else None,
                input_column_ids=input_column_ids,
                in_group_by=True,
            )
        )

    # A pure ordinary query is a derived candidate only when it has a derived
    # EXPRESSION key OR a resolved stage-4 relabel; otherwise return [] so the
    # caller never invokes the proof engine and ordinary routing is byte-identical.
    _has_relabel = bool(getattr(bound_query, "bound_attribute_relabels", None))
    if not keys and not _has_relabel:
        return []

    # §7.8: any GROUP_KEY expression occurrence NOT covered by a bound derived
    # expression (the binder could not bind it) leaves a grouping the proof cannot
    # account for. Emit an unservable placeholder key (no fingerprint) per uncovered
    # occurrence so the proof tuple is complete-but-uncoverable -> SOURCE_ONLY, never
    # a partial serve that drops the unbound grouping.
    for uncovered in (all_group_key_occ_ids - covered_group_key_occ_ids):
        keys.append(
            QueryKeyRequest(
                logical_name=f"unbound_group_key:{uncovered}",
                kind="EXPRESSION",
                fingerprint=None,   # never exact-covered -> forces SOURCE_ONLY
                in_group_by=True,
            )
        )

    # §2.4 / §7.8 tuple completeness: EVERY GROUP BY key must be in the proof tuple,
    # or a mixed query like ``GROUP BY DATE_TRUNC('month', ts), region`` would fake a
    # full cover on the month key alone. The parser puts ONLY bare (non-expression)
    # group columns in ``lq.grain``; each is emitted as an ATTRIBUTE request (a
    # resolved bijection relabel) or an unchanged-PHYSICAL request (``dim:<uuid>`` +
    # ``(source_column_id,)``). A bare grain the binder could NOT resolve to either
    # (no relabel, no stable source column id) is emitted as a PHYSICAL request with
    # no key_id/lineage -> never exact-covered -> the candidate rejects to source.
    #
    # The binder resolved these against the deployed snapshot: relabels by group
    # ordinal, plus one PHYSICAL projection per SELECT ordinal (deduped to group
    # ordinal here so the proof tuple has ONE request per group key).
    relabel_by_group = {
        rl.group_ordinal: rl
        for rl in (getattr(bound_query, "bound_attribute_relabels", []) or [])
    }
    physical_by_group: dict[int, Any] = {}
    for pj in (getattr(bound_query, "bound_group_key_projections", []) or []):
        if pj.kind == "PHYSICAL" and pj.group_ordinal not in physical_by_group:
            physical_by_group[pj.group_ordinal] = pj
    for group_ordinal, grain_name in enumerate(getattr(lq, "grain", None) or []):
        rl = relabel_by_group.get(group_ordinal)
        if rl is not None:
            # ATTRIBUTE relabel: the edge identifies the artifact key (key_id=None).
            keys.append(QueryKeyRequest(
                logical_name=rl.output_alias or rl.requested_name or str(grain_name),
                kind="ATTRIBUTE",
                key_id=None,
                attribute_key=rl.attribute_key,
                relationship_id=rl.relationship_id,
                input_column_ids=(rl.key_column_id, rl.detail_column_id),
                declaration_hash=rl.declaration_hash,
                owning_dimension_id=rl.owning_dimension_id,
                fingerprint=None,
                in_group_by=True,
            ))
            continue
        pj = physical_by_group.get(group_ordinal)
        if pj is not None and pj.key_id and pj.column_id:
            # Unchanged PHYSICAL key: dim:<dimension-uuid> + (source_column_id,).
            keys.append(QueryKeyRequest(
                logical_name=pj.output_alias or str(grain_name),
                kind="PHYSICAL",
                key_id=pj.key_id,
                input_column_ids=(str(pj.column_id),),
                fingerprint=None,
                in_group_by=True,
            ))
            continue
        # Unresolved bare grain -> a PHYSICAL request with no identity, which can
        # never be exact-covered -> candidate rejects to source (fail-closed).
        keys.append(QueryKeyRequest(
            logical_name=str(grain_name),
            kind="PHYSICAL",
            fingerprint=None,
            in_group_by=True,
        ))
    return keys


def _requested_alias_for(bde, occ_by_id: dict) -> Optional[str]:
    """The query's requested output alias for a served expression (§8.2).

    Prefers a GROUP_KEY occurrence's ``output_alias``, then any occurrence's
    alias; returns None when the query gave the expression no alias (the caller
    falls back to the canonical SQL text).
    """
    group_alias = None
    any_alias = None
    for oid in bde.occurrence_ids:
        occ = occ_by_id.get(oid)
        if occ is None:
            continue
        oa = getattr(occ, "output_alias", None)
        if not oa:
            continue
        if getattr(occ, "role", None) == "GROUP_KEY" and group_alias is None:
            group_alias = str(oa)
        if any_alias is None:
            any_alias = str(oa)
    return group_alias or any_alias


def served_expression_leaf_physical_names(bound_query: BoundQuery) -> frozenset[str]:
    """Lower-cased physical column names of EVERY GROUP-BY derived-expression leaf.

    §9.1 CLS: a user who cannot reference an input column cannot group by a
    FUNCTION of it — "the derived alias does not hide restricted inputs". Each
    expression leaf records its physical column NAME (BoundColumnRef.physical_column);
    the router resolves the persona's restricted columns to physical NAMES and
    denies the derived route when any served expression leaf is in that set. This
    deny is NAME-based (independent of whether the leaf also carries a stable
    column id — the id binding is used only by the §7.3 cond. 2 lineage gate). It
    is the expression-key analogue of the attribute-edge key/detail CLS deny.
    Names are lower-cased because unquoted SQL identifiers case-fold in PostgreSQL.
    """
    lq = bound_query.logical_query
    occ_by_id = {
        o.occurrence_id: o
        for o in (getattr(lq, "expression_occurrences", []) or [])
    }
    out: set[str] = set()
    for bde in (getattr(bound_query, "bound_derived_expressions", []) or []):
        roles = {
            occ_by_id[oid].role for oid in bde.occurrence_ids if oid in occ_by_id
        }
        if "GROUP_KEY" not in roles:
            continue
        for ref in (bde.inputs or []):
            name = getattr(ref, "physical_column", None)
            if name:
                out.add(str(name).lower())
    return frozenset(out)


@dataclass
class _GrainIndex:
    """Canonical-id index of a candidate's ``grain_keys`` manifest (spec §2.1)."""
    ids: frozenset[str]
    fingerprints: frozenset[str]
    id_by_fingerprint: dict[str, str]
    lineage_by_id: dict[str, tuple[str, ...]]
    lineage_by_fingerprint: dict[str, tuple[str, ...]]
    units_by_fingerprint: dict[str, str]
    physical_by_id: dict[str, str]
    source_dim_by_id: dict[str, str]
    poisoned: bool


def _grain_key_index(agg: AggregateDefinition) -> _GrainIndex:
    """Index a candidate aggregate's ``grain_keys`` manifest for the proof (§2.1).

    The cover vocabulary is the canonical ``MaterializedGrainKey.key_id`` string
    (``dim:<uuid>`` / ``expr:<fingerprint>``) — the synthetic ``keyid:<...>`` form
    is OUTLAWED. EVERY materialised grain key contributes its canonical ``key_id``,
    so an uncovered physical or expression key keeps the set cover honest (a
    `GROUP BY month` query over a (month, region) artifact cannot fake EXACT).

    Poisons the candidate (``poisoned=True``, never EXACT) on: a duplicate key_id,
    a duplicate fingerprint, a fingerprint mapping to several key_ids, an
    ``expr:`` id whose fingerprint disagrees with its stored ``expression_
    fingerprint``, or a grain key with no canonical ``key_id`` — any of which would
    let a same-name/ambiguous artifact key mis-cover.
    """
    ids: set[str] = set()
    fingerprints: set[str] = set()
    id_by_fp: dict[str, str] = {}
    lineage: dict[str, tuple[str, ...]] = {}
    lineage_by_fp: dict[str, tuple[str, ...]] = {}
    units_by_fp: dict[str, str] = {}
    physical: dict[str, str] = {}
    source_dim: dict[str, str] = {}
    poisoned = False
    for gk in _manifest_list(agg.grain_keys):
        key_id = gk.get("key_id")
        if not key_id:
            poisoned = True
            continue
        key_id = str(key_id)
        if key_id in ids:
            poisoned = True  # duplicate canonical id
            continue
        ids.add(key_id)
        # §2.1: key_id, kind, and fingerprint must agree. A ``dim:`` id must be a
        # PHYSICAL_COLUMN with no fingerprint and whose ``source_dimension_id``
        # (when present) equals the uuid encoded in the id; an ``expr:`` id must be
        # an expression kind carrying a fingerprint and NO source_dimension_id. Any
        # disagreement poisons the candidate (never a mis-covered key).
        kind = gk.get("kind")
        fp = gk.get("expression_fingerprint")
        _sdi = gk.get("source_dimension_id")
        if key_id.startswith("dim:"):
            _encoded = key_id.split(":", 1)[1]
            if kind not in (None, "PHYSICAL_COLUMN") or fp or (_sdi and str(_sdi) != _encoded):
                poisoned = True
        elif key_id.startswith("expr:"):
            if kind == "PHYSICAL_COLUMN" or _sdi:
                poisoned = True
        inputs = gk.get("input_column_ids") or []
        ordered_lineage = tuple(str(c) for c in inputs if c) if isinstance(inputs, list) else ()
        lineage[key_id] = ordered_lineage
        unit = gk.get("date_trunc_unit") or gk.get("time_unit")
        if fp:
            fp = str(fp)
            fingerprints.add(fp)
            if fp in id_by_fp and id_by_fp[fp] != key_id:
                poisoned = True  # one fingerprint mapped to several key ids
            id_by_fp[fp] = key_id
            # An expr:<fp> id must agree with its stored fingerprint.
            if key_id.startswith("expr:") and key_id.split(":", 1)[1] != fp:
                poisoned = True
            # Fingerprint-keyed views for the (unserved) coarsening path.
            lineage_by_fp[fp] = ordered_lineage
            if unit:
                units_by_fp[fp] = str(unit)
        phys = gk.get("physical_column")
        if phys:
            physical[key_id] = str(phys)
        sdi = gk.get("source_dimension_id")
        if sdi:
            source_dim[key_id] = str(sdi)
    return _GrainIndex(
        ids=frozenset(ids), fingerprints=frozenset(fingerprints),
        id_by_fingerprint=id_by_fp, lineage_by_id=lineage,
        lineage_by_fingerprint=lineage_by_fp, units_by_fingerprint=units_by_fp,
        physical_by_id=physical,
        source_dim_by_id=source_dim, poisoned=poisoned,
    )


async def load_candidate_trust_inputs(
    *,
    db: AsyncSession,
    agg: AggregateDefinition,
    ctx: DerivedServeContext,
) -> dict[str, TrustInputs]:
    """Load newest evidence + deployed declaration per carried relationship.

    Returns a map relationship_id -> TrustInputs for every attribute edge the
    candidate's manifest carries. Any relationship with no manifest edge, no
    enabled declaration, or no evidence is simply omitted (the proof engine then
    rejects a query that needs it, fail-closed). No SQL is executed by the proof
    engine — all state is resolved here.
    """
    edges = _manifest_list(agg.attribute_edges)
    if not edges:
        return {}

    # Bug-7905 evidence-AGE bound. Resolve ONCE per candidate (single model) the max
    # age a VERIFIED health row may serve at, and capture ``now`` once so every edge
    # is judged against the same instant. A None bound (unreadable cadence) fails
    # every edge closed below.
    _age_bound = await _resolve_evidence_age_bound(db, getattr(agg, "model_id", None))
    _now = datetime.now(timezone.utc)

    out: dict[str, TrustInputs] = {}
    for edge in edges:
        rel_id = edge.get("relationship_id")
        if not rel_id:
            continue
        rel = await db.get(DimensionAttributeRelationship, rel_id)
        if rel is None:
            continue
        # §9.1 CLS: the edge is authorised only when NEITHER the internal key
        # column NOR the detail column is persona-restricted. A restricted key or
        # detail denies the edge (security_ok=False) so a relabel cannot launder a
        # restricted key through a permitted label, and a restricted detail is
        # never exposed just because it is a physically-present passenger.
        edge_security_ok = ctx.security_ok
        if ctx.restricted_column_ids:
            key_id = str(rel.key_column_id) if rel.key_column_id else None
            detail_id = str(rel.detail_column_id) if rel.detail_column_id else None
            if (key_id and key_id in ctx.restricted_column_ids) or (
                detail_id and detail_id in ctx.restricted_column_ids
            ):
                edge_security_ok = False
        # Newest ARTIFACT-LOCAL evidence row for THIS relationship on THIS
        # aggregate, whose declaration hash still matches the live declaration.
        # Scoping by artifact_id (and the artifact refresh run) is required: a
        # newer refresh of a DIFFERENT aggregate must not shadow this candidate's
        # own VERIFIED edge, and a DEPLOY_CHECK (model-health) evidence row is NOT
        # a serving authority (§7.6.2) — only artifact-local evidence bound to this
        # aggregate's active run can authorise a route. Older/superseded hashes and
        # other-artifact rows are excluded here so the trust predicate sees only
        # this candidate's own evidence.
        ev = (
            await db.execute(
                select(DimensionAttributeVerification)
                .where(
                    DimensionAttributeVerification.relationship_id == rel.id,
                    DimensionAttributeVerification.declaration_hash == rel.declaration_hash,
                    DimensionAttributeVerification.artifact_id == agg.id,
                    DimensionAttributeVerification.artifact_refresh_run_id
                    == agg.active_refresh_run_id,
                )
                .order_by(
                    DimensionAttributeVerification.checked_at.desc(),
                    DimensionAttributeVerification.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        # Bug-7905: fresh iff the newest evidence's checked_at is within the age
        # bound. Fail-closed on a None bound / missing checked_at.
        _age_ok = _evidence_is_fresh(ev, _now, _age_bound)
        out[str(rel_id)] = TrustInputs(
            declaration_enabled=bool(rel.enabled),
            deployed_declaration_hash=rel.declaration_hash,
            evidence=ev,
            accepted_verifier_version=ctx.accepted_verifier_version,
            evidence_age_ok=_age_ok,
            artifact_active_refresh_run_id=agg.active_refresh_run_id,
            artifact_manifest_hash=edge.get("manifest_hash"),
            artifact_is_active=(str(getattr(agg, "status", "")).lower() == "active"),
            artifact_is_stale=bool(getattr(agg, "is_stale", False)),
            bound_deployed_version_id=ctx.bound_deployed_version_id,
            bound_deploy_epoch=ctx.bound_deploy_epoch,
            connector_source_version=ctx.connector_source_version,
            manifest_edge=edge,
            security_ok=edge_security_ok,
        )
    return out


def build_candidate_manifest(
    *,
    agg: AggregateDefinition,
    trust_by_relationship: dict[str, TrustInputs],
) -> CandidateManifest:
    """Build the ``CandidateManifest`` for one aggregate (pure).

    Populates the Phase-3 manifest view the proof engine consumes: grain-key
    fingerprints + units + leaf lineage, the attribute-edge dicts (each carrying
    its ``key_grain_fingerprint`` and ``detail_passenger_column`` for the §7.3
    set cover and §8.2 rewrite), the measure component suffix set, and the live
    active-run / manifest-hash / freshness state.
    """
    gidx = _grain_key_index(agg)
    # Index attribute edges by attribute_key + relationship_id, and passenger
    # columns by attribute_key (§2.4). Poison the candidate if an edge/passenger
    # attribute_key is duplicated, disagrees with its relationship_id, or an
    # attribute_key present on the edge is missing on the passenger side (and vice
    # versa) — an identity mismatch that could select the wrong passenger.
    edge_by_ak: dict[str, dict] = {}
    passenger_by_ak: dict[str, dict] = {}
    poisoned = gidx.poisoned
    for edge in _manifest_list(agg.attribute_edges):
        ak = edge.get("attribute_key")
        rel_id = edge.get("relationship_id")
        if ak:
            if ak in edge_by_ak:
                poisoned = True
            # attr:<uuid> must agree with the edge's relationship_id.
            if ":" in ak and rel_id and ak.split(":", 1)[1] != str(rel_id):
                poisoned = True
            edge_by_ak[ak] = edge
    for pc in _manifest_list(agg.passenger_columns):
        ak = pc.get("attribute_key")
        if ak:
            if ak in passenger_by_ak:
                poisoned = True
            passenger_by_ak[ak] = pc
    # An attribute_key on one side but not the other cannot be trusted to select
    # the right passenger for the right edge.
    if set(edge_by_ak) != set(passenger_by_ak) and (edge_by_ak or passenger_by_ak):
        # Only poison when edges are present (a pure expression/physical artifact
        # carries neither and is unaffected).
        if edge_by_ak:
            poisoned = True
    fps = gidx.fingerprints
    # ``grain_key_units`` / ``grain_key_lineage`` stay fingerprint-keyed for the
    # (unserved-in-stage-5) coarsening path; the exact cover uses the id-keyed views.
    units = gidx.units_by_fingerprint
    lineage = gidx.lineage_by_fingerprint
    # measure_components is the derived measure classifier's OPTIONAL availability
    # gate: when non-empty it must be the set of stored measure component SUFFIXES
    # (``x__sum`` / ``x__count`` / ...), NOT passenger detail columns — supplying
    # passenger names would make the classifier reject every additive measure
    # (``revenue__sum`` is never a passenger name) and defeat serving. We leave it
    # EMPTY and let the single authoritative availability gate run in the rewrite:
    # ``_phys_expr_for_node`` returns "NULL" for an absent stored stat column and
    # ``rewrite_for_derived_exact`` fails closed on that (derived_exact.py). One
    # gate, no double source of truth, no passenger/measure suffix confusion.
    return CandidateManifest(
        artifact_id=str(agg.id),
        is_active=(str(getattr(agg, "status", "")).lower() == "active"),
        is_stale=bool(getattr(agg, "is_stale", False)),
        active_refresh_run_id=agg.active_refresh_run_id,
        manifest_hash=_candidate_manifest_hash(agg),
        grain_key_fingerprints=fps,
        grain_key_units=units,
        grain_key_lineage=lineage,
        attribute_edges=_manifest_list(agg.attribute_edges),
        measure_components=frozenset(),  # availability enforced in the rewrite
        trust_by_relationship=trust_by_relationship,
        week_start_pinned=_week_start_pinned(agg),
        grain_key_ids=gidx.ids,
        grain_key_id_by_fingerprint=gidx.id_by_fingerprint,
        grain_key_lineage_by_id=gidx.lineage_by_id,
        grain_key_physical_by_id=gidx.physical_by_id,
        grain_key_source_dim_by_id=gidx.source_dim_by_id,
        edge_by_attribute_key=edge_by_ak,
        passenger_by_attribute_key=passenger_by_ak,
        poisoned=poisoned,
    )


def _candidate_manifest_hash(agg: AggregateDefinition) -> Optional[str]:
    """The candidate's active physical manifest hash (from any carried edge).

    All edges of one built manifest share the same ``manifest_hash`` (they are
    written in one lifecycle transaction, §7.6.3). We surface it so the proof
    trace / trust predicate compare against the built value. None when no edge is
    carried (a pure expression-key aggregate) — the exact identity path does not
    need it, and the trust predicate is only consulted for attribute edges.
    """
    for edge in _manifest_list(agg.attribute_edges):
        h = edge.get("manifest_hash")
        if h:
            return str(h)
    return None


def _week_start_pinned(agg: AggregateDefinition) -> bool:
    """Whether the artifact pins a week-start convention on any grain key.

    Only relevant to the (unserved-in-Phase-5) week coarsening path; carried so
    the proof engine sees identical inputs. Fail-closed to False (unpinned week
    edges are SOURCE_ONLY, §7.5).
    """
    for gk in _manifest_list(agg.grain_keys):
        sctx = gk.get("semantic_context")
        if isinstance(sctx, dict) and sctx.get("week_start_pinned"):
            return True
    return False


async def try_build_exact_proof(
    *,
    db: AsyncSession,
    bound_query: BoundQuery,
    agg: AggregateDefinition,
    measures: list[MeasureRequest],
    ctx: DerivedServeContext,
):
    """Build a ``DerivedServeProof`` for one candidate and return it ONLY when it
    is a Phase-5-servable EXACT verdict; otherwise return None.

    Phase 5 serves EXACT only (stages 4 + 5). A ROLLUP or SOURCE_ONLY verdict
    returns None so the router falls back to source / ordinary routing —
    byte-identical, never a wrong number. Any exception is swallowed (fail-closed
    to None). The caller has already resolved ``ctx.security_ok`` from the
    existing CLS/RLS gates.
    """
    try:
        query_keys = build_query_key_requests(bound_query)
        if not query_keys:
            return None
        trust = await load_candidate_trust_inputs(db=db, agg=agg, ctx=ctx)
        candidate = build_candidate_manifest(agg=agg, trust_by_relationship=trust)
        proof = build_serve_proof(
            query_keys=query_keys,
            measures=measures,
            candidate=candidate,
            security_ok=ctx.security_ok,
        )
        if proof.verdict != EXACT:
            return None
        # §7.3 cond. 2 — bound-column-ID compatibility. The exact expression-key
        # match is by canonical fingerprint, which hashes canonical SQL over column
        # NAMES; two model columns of the same name (orders.created_at vs
        # returns.created_at) produce identical fingerprints for different grains.
        # Require the query expression's bound leaf column IDs to be NON-EMPTY and
        # EQUAL to the manifest grain key's stored lineage before an EXACT serve.
        # The binder now populates stable leaf ids against the deployed snapshot
        # (§7.1) ALL-OR-NOTHING: an unresolved, ambiguous (same-name/multi-table),
        # or source-only expression carries NO leaf ids, so this gate still FAILS
        # CLOSED (-> source) for it — a genuine same-name cross-relation collision
        # never serves. Only a fully-resolved leaf tuple that EQUALS the manifest's
        # stored lineage authorises an EXACT serve.
        if not _exact_lineage_matches(query_keys, candidate):
            return None
        return proof
    except Exception:  # noqa: BLE001 — fail-closed to source on any doubt.
        logger.warning(
            "derived exact proof build failed for aggregate %s; routing to source",
            getattr(agg, "id", "?"), exc_info=True,
        )
        return None


def _exact_lineage_matches(
    query_keys: list[QueryKeyRequest], candidate: CandidateManifest,
) -> bool:
    """True iff every EXPRESSION query key's leaf lineage matches its artifact key.

    §2.4 / §7.3 cond. 2: an exact expression-key identity requires the bound INPUT
    COLUMN IDs to match, not just the canonical fingerprint (which is name-based and
    can collide across same-named columns in different relations). For each
    EXPRESSION key whose fingerprint matches a stored grain key, the query's leaf
    column IDs must be non-empty AND equal as an ORDERED TUPLE (never a set, never
    sorted) to that grain key's stored ``input_column_ids``. Fails closed when
    EITHER side has empty lineage — so a binding that has not populated stable leaf
    IDs cannot serve an exact match on fingerprint alone. Order matters: a query
    ``a - b`` and a stored ``b - a`` share no exact identity even if the fingerprint
    layer folded them, because the canonical leaf order differs.
    """
    for qk in query_keys:
        if qk.kind != "EXPRESSION":
            continue
        fp = qk.fingerprint
        if not fp or fp not in candidate.grain_key_fingerprints:
            # This expression key was not served by exact identity (it may be a
            # coarsening the proof handled) — lineage is checked at that layer.
            continue
        key_id = candidate.grain_key_id_by_fingerprint.get(fp)
        stored = candidate.grain_key_lineage_by_id.get(key_id or "", ())
        q_leaves = tuple(qk.input_column_ids)
        if not q_leaves or not stored:
            return False  # empty lineage on either side -> fail closed
        if q_leaves != tuple(stored):
            return False
    return True
