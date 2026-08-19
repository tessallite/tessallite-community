"""Unified derived-grain serving PROOF engine (SHADOW only — no serving).

Spec: architecture_derived-grain-aggregate-routing.md §5.4, §7 (proof algorithm),
§14.2/§14.4. Given a bound query and ONE candidate artifact, decide whether the
query COULD be served from the artifact and produce an immutable ``DerivedServeProof``
with a verdict, per-key and per-measure plans, trusted edges, reason codes, and a
proof trace. It does NOT rewrite, execute, or route anything — Phase 4 is shadow.

The engine composes three trusted-edge providers, all fail-closed:
  - EXACT key identity (§7.3): query key fingerprint == materialised key fingerprint
    (matched via the Phase-3 manifest ``grain_keys``), OR a verified bijection relabel
    (attribute edge, cardinality BIJECTION, trust predicate passes).
  - FUNCTION_SEMANTICS coarsening (§7.4/§7.5): a query time unit derivable from a
    finer stored DATE_TRUNC key via a boundary-aligned theorem (derived_time_theorems).
  - DATA_VERIFIED_ATTRIBUTE coarsening (§7.5): an N:1 verified attribute edge maps a
    query key from a materialised passenger; always ROLLUP.

Two structural invariants are enforced here, verbatim from the spec:
  - §7.3 / pitfall 24 (F-004-06 parity): FILTERS NEVER CONSUME A KEY for exactness.
    Exactness is judged on the GROUP BY tuple ALONE. A WHERE/IN/range predicate on an
    extra artifact key does NOT remove it from the tuple comparison -> verdict ROLLUP,
    and every requested statistic must pass I4 (direct-only stats refuse).
  - §7.8: proof is tuple-level. One unproved query key, or one unproved measure,
    rejects the whole candidate (verdict SOURCE_ONLY).

PURE: no DB access, no SQL, no route mutation. Inputs are already-resolved ORM
objects / bound IR / evidence, supplied by the (future, gated) caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from shared.semantic.derived_grain_reasons import DerivedReasonCode
from src.routing.derived_measure_proof import (
    MeasureRollupPlan,
    VERDICT_EXACT,
    VERDICT_ROLLUP,
    all_measures_servable,
    classify_measure_rollup,
)
from src.routing.derived_time_theorems import coarsen_edge
from src.routing.derived_trust_predicate import TrustInputs, evaluate_trust

# DerivedServeProof verdicts (spec §5.4).
EXACT = "EXACT"
ROLLUP = "ROLLUP"
EXACT_ROW_RECOMPUTE = "EXACT_ROW_RECOMPUTE"
SOURCE_ONLY = "SOURCE_ONLY"

# DerivedKeyPlan kinds (spec §5.4).
DIRECT_PHYSICAL_KEY = "DIRECT_PHYSICAL_KEY"
DIRECT_EXPRESSION_KEY = "DIRECT_EXPRESSION_KEY"
DIRECT_ATTRIBUTE_RELABEL = "DIRECT_ATTRIBUTE_RELABEL"
DERIVED_COARSENING = "DERIVED_COARSENING"

# TrustedDerivationEdge provenance (spec §5.4).
FUNCTION_SEMANTICS = "FUNCTION_SEMANTICS"
DATA_VERIFIED_ATTRIBUTE = "DATA_VERIFIED_ATTRIBUTE"


@dataclass
class QueryKeyRequest:
    """One key the query groups by, in canonical form for the proof.

    ``kind`` is PHYSICAL | EXPRESSION | ATTRIBUTE. For EXPRESSION, ``fingerprint``
    is the canonical expression fingerprint and ``time_unit`` (if a DATE_TRUNC) the
    unit. For ATTRIBUTE, ``relationship_id`` names the declared edge.
    """
    logical_name: str
    kind: str
    fingerprint: Optional[str] = None
    time_unit: Optional[str] = None
    relationship_id: Optional[str] = None
    in_group_by: bool = True   # False => appears only in a WHERE/filter (§7.3)
    # Bound leaf column id(s) the expression truncates/derives from (§7.4/§7.2:
    # the DAG must terminate at the query expression's ACTUAL bound inputs). For a
    # time-coarsening edge, the stored finer key's leaf column MUST equal this, or
    # the derivation is cross-column (e.g. year-of-order from month-of-ship) and is
    # refused. Empty tuple => no lineage asserted => a coarsening edge is refused.
    input_column_ids: tuple[str, ...] = ()
    # Stage-4 additive identities (spec §2.4). ``key_id`` is ``dim:<uuid>`` for an
    # unchanged PHYSICAL key (never a fingerprint or synthetic ``keyid:``);
    # ``attribute_key`` is ``attr:<relationship-uuid>`` for an ATTRIBUTE relabel
    # (the edge identifies the artifact key, so key_id stays None there). Neither
    # renames ``fingerprint`` — an EXPRESSION request still matches by fingerprint.
    key_id: Optional[str] = None
    attribute_key: Optional[str] = None
    # The deployed declaration hash the ATTRIBUTE relabel was bound against (§3.3):
    # the proof rejects when it disagrees with the matched edge's declaration_hash,
    # so a post-deploy relationship edit (rebound detail, new hash) cannot serve the
    # old dimension's label over the new artifact rows.
    declaration_hash: Optional[str] = None
    # The relationship key dimension (§3.2): the ATTRIBUTE edge's covered grain key
    # MUST be exactly ``dim:<owning_dimension_id>`` (kind PHYSICAL_COLUMN, matching
    # source_dimension_id) — so a relabel can never be served from an ``expr:`` key
    # (a transformed partition) that merely shares the key column as a leaf.
    owning_dimension_id: Optional[str] = None


@dataclass
class DerivedKeyPlan:
    query_key: str
    plan: str
    artifact_key_fingerprint: Optional[str] = None
    passenger_column: Optional[str] = None
    reason: Optional[str] = None
    # Stage-4 additive identities (spec §2.4). ``artifact_key_id`` is the canonical
    # ``dim:<uuid>`` / ``expr:<fingerprint>`` id of the matched manifest grain key
    # — a NEW field, NOT a rename of ``artifact_key_fingerprint`` (the live stage-5
    # expression path still populates the fingerprint). ``attribute_key`` carries
    # the ``attr:<uuid>`` for a relabel plan so rewrite selects the passenger by
    # identity. EXPRESSION populates both fingerprint + id; PHYSICAL populates id
    # only; ATTRIBUTE populates id + attribute_key + passenger_column.
    artifact_key_id: Optional[str] = None
    attribute_key: Optional[str] = None


@dataclass
class TrustedDerivationEdge:
    provenance: str          # FUNCTION_SEMANTICS | DATA_VERIFIED_ATTRIBUTE
    direction: str           # e.g. "day->month", "country_id->country_name"
    cardinality: str         # BIJECTION | FUNCTIONAL_N_TO_1 | EXACT
    evidence_id: Optional[str] = None


@dataclass
class DerivedServeProof:
    """Immutable proof object (spec §5.4). The exactness validator checks its
    verdict/artifact/manifest; it does NOT recompute the proof (I9)."""
    verdict: str
    artifact_id: Optional[str]
    query_key_plans: list[DerivedKeyPlan] = field(default_factory=list)
    measure_plans: list[MeasureRollupPlan] = field(default_factory=list)
    trusted_edges: list[TrustedDerivationEdge] = field(default_factory=list)
    relationship_evidence_ids: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    proof_trace: list[str] = field(default_factory=list)
    source_profile_hash: Optional[str] = None
    target_profile_hash: Optional[str] = None
    proof_registry_version: Optional[str] = None

    @property
    def servable(self) -> bool:
        return self.verdict in (EXACT, ROLLUP, EXACT_ROW_RECOMPUTE)


@dataclass
class CandidateManifest:
    """The Phase-3 manifest view of one candidate artifact, resolved by the caller.

    ``grain_keys`` / ``attribute_edges`` are the artifact's manifest lists;
    ``measure_components`` is the set of stored component suffixes (e.g.
    {"amount__sum","amount__count"}); trust_by_relationship maps relationship_id ->
    a pre-evaluated TrustInputs so the engine stays DB-free.
    """
    artifact_id: str
    is_active: bool
    is_stale: bool
    active_refresh_run_id: Optional[Any]
    manifest_hash: Optional[str]
    grain_key_fingerprints: frozenset[str] = frozenset()
    grain_key_units: dict[str, str] = field(default_factory=dict)  # fingerprint -> DATE_TRUNC unit
    # fingerprint -> the stored key's bound leaf column id(s). A time-coarsening
    # edge is admitted only when the stored finer key's leaf column equals the
    # query expression's leaf column (§7.4 leaf-lineage; prevents deriving
    # year-of-order from month-of-ship). Missing/empty => no coarsening from it.
    grain_key_lineage: dict[str, tuple[str, ...]] = field(default_factory=dict)
    attribute_edges: list[dict] = field(default_factory=list)      # manifest edge dicts
    measure_components: frozenset[str] = frozenset()
    trust_by_relationship: dict[str, TrustInputs] = field(default_factory=dict)
    week_start_pinned: bool = False
    # --- Canonical key-id cover vocabulary (spec §2.1) ------------------------
    # The proof cover set uses ONE vocabulary: canonical MaterializedGrainKey.key_id
    # strings (``dim:<uuid>`` / ``expr:<fingerprint>``). ``grain_key_fingerprints``
    # above stays the EXPRESSION-identity index for the live stage-5 path; exact
    # cover accounting uses only ``grain_key_ids``. The synthetic ``keyid:<...>``
    # form is outlawed and never appears here.
    grain_key_ids: frozenset[str] = frozenset()
    grain_key_id_by_fingerprint: dict[str, str] = field(default_factory=dict)
    grain_key_lineage_by_id: dict[str, tuple[str, ...]] = field(default_factory=dict)
    grain_key_physical_by_id: dict[str, str] = field(default_factory=dict)
    # PHYSICAL grain key metadata by key_id: source_dimension_id (the uuid encoded
    # in ``dim:<uuid>``) so the §2.4 physical exact-match can require agreement.
    grain_key_source_dim_by_id: dict[str, str] = field(default_factory=dict)
    # Attribute edges indexed by attribute_key + relationship_id; passenger cols
    # indexed by attribute_key (§2.4). Empty when the artifact carries no edges.
    edge_by_attribute_key: dict[str, dict] = field(default_factory=dict)
    passenger_by_attribute_key: dict[str, dict] = field(default_factory=dict)
    # True when the manifest was rejected during indexing (duplicate ids, a
    # fingerprint mapping to several ids, or key_id/kind/fingerprint disagreement).
    # A poisoned candidate can never produce EXACT (§2.1).
    poisoned: bool = False


@dataclass
class MeasureRequest:
    measure_name: str
    requested_stat: str


def _source_only(artifact_id: Optional[str], codes: list[str], trace: list[str]) -> DerivedServeProof:
    return DerivedServeProof(
        verdict=SOURCE_ONLY, artifact_id=artifact_id,
        reason_codes=codes or [DerivedReasonCode.DERIVED_EXACT_KEY_NOT_FOUND.value],
        proof_trace=trace,
    )


def build_serve_proof(
    *,
    query_keys: list[QueryKeyRequest],
    measures: list[MeasureRequest],
    candidate: CandidateManifest,
    security_ok: bool = True,
) -> DerivedServeProof:
    """Construct the DerivedServeProof for one candidate (§7). Fail-closed.

    Returns SOURCE_ONLY on the FIRST unproved key or measure. A proof is EXACT only
    when every GROUP BY key maps one-to-one to exactly one artifact key with no
    extra independent artifact key remaining (§7.3 cond. 6) AND every direct-only
    measure is satisfiable at exact grain; otherwise ROLLUP when every key is at
    least a proven coarsening and every measure has a non-SOURCE_ONLY plan.
    """
    trace: list[str] = []
    reason_codes: list[str] = []
    key_plans: list[DerivedKeyPlan] = []
    edges: list[TrustedDerivationEdge] = []
    evidence_ids: list[str] = []

    # Artifact liveness gate (rule 5 short-circuit for the whole candidate).
    if not candidate.is_active or candidate.is_stale or candidate.active_refresh_run_id is None:
        return _source_only(
            candidate.artifact_id,
            [DerivedReasonCode.ATTRIBUTE_ACTIVE_REFRESH_MISMATCH.value], trace,
        )

    # A poisoned manifest (duplicate ids, fp->many ids, key_id/kind/fp
    # disagreement, §2.1) can never be exactly covered — fail closed immediately.
    if candidate.poisoned:
        return _source_only(
            candidate.artifact_id,
            [DerivedReasonCode.DERIVED_EXACT_KEY_NOT_FOUND.value], trace,
        )

    # §7.3 / pitfall 24: exactness is judged on the GROUP BY tuple ALONE. Keys that
    # appear only in a filter do NOT participate in the tuple and NEVER make the
    # verdict exact. We prove only the in-group-by keys; a filtered extra artifact
    # key remaining means the candidate is coarser (ROLLUP) for those group keys.
    group_keys = [k for k in query_keys if k.in_group_by]
    if not group_keys:
        return _source_only(candidate.artifact_id, reason_codes, trace)

    any_coarsening = False
    # Set of DISTINCT artifact grain-key IDs consumed by an exact/relabel identity
    # (§2.1 / §7.3 cond. 6 is a SET cover over canonical key_id strings, NOT a
    # count — two query keys resolving to the SAME artifact key must not mask an
    # uncovered extra key and fake an EXACT verdict). Keys consumed by a coarsening
    # are tracked separately (a coarsened key is aggregated away, not an exact
    # cover). Both use canonical MaterializedGrainKey.key_id values only.
    exact_covered_ids: set[str] = set()
    coarsened_covered_ids: set[str] = set()

    def _ordered_lineage_ok(query_ids: tuple[str, ...], stored: tuple[str, ...]) -> bool:
        # §2.4 ordered non-empty tuple equality (never a set, never sorted).
        return bool(query_ids) and bool(stored) and tuple(query_ids) == tuple(stored)

    for qk in group_keys:
        if qk.kind == "EXPRESSION":
            # Exact expression-key identity (§2.1): match by fingerprint AND ordered
            # lineage, then consume the matched entry's canonical ``expr:<fp>`` id.
            matched = False
            if qk.fingerprint and qk.fingerprint in candidate.grain_key_fingerprints:
                key_id = candidate.grain_key_id_by_fingerprint.get(qk.fingerprint)
                stored_lineage = candidate.grain_key_lineage_by_id.get(key_id or "", ())
                if key_id and _ordered_lineage_ok(qk.input_column_ids, stored_lineage):
                    key_plans.append(DerivedKeyPlan(
                        query_key=qk.logical_name,
                        plan=DIRECT_EXPRESSION_KEY,
                        artifact_key_fingerprint=qk.fingerprint,
                        artifact_key_id=key_id,
                    ))
                    exact_covered_ids.add(key_id)
                    trace.append(f"key {qk.logical_name}: exact expression identity {key_id}")
                    matched = True
            if matched:
                continue
            # No exact identity — try a boundary-aligned time coarsening from a
            # finer stored DATE_TRUNC key (§7.5). Only for time expressions, and
            # only when the stored key's LEAF COLUMN is the same column the query
            # expression truncates (§7.4/§7.2: derive year-of-order from a
            # month-of-order key, NEVER from a month-of-ship key). A query key with
            # no asserted lineage cannot prove a coarsening. Coarsening lineage
            # stays a set compare (it is not an exact identity claim).
            derived = False
            if qk.time_unit and qk.input_column_ids:
                q_leaves = frozenset(qk.input_column_ids)
                for fp, finer_unit in candidate.grain_key_units.items():
                    stored_leaves = frozenset(candidate.grain_key_lineage.get(fp, ()))
                    if not stored_leaves or stored_leaves != q_leaves:
                        continue
                    edge = coarsen_edge(
                        finer_unit=finer_unit, coarse_unit=qk.time_unit,
                        week_start_pinned=candidate.week_start_pinned,
                    )
                    if edge.admissible:
                        _cfp_id = candidate.grain_key_id_by_fingerprint.get(fp, fp)
                        key_plans.append(DerivedKeyPlan(
                            query_key=qk.logical_name, plan=DERIVED_COARSENING,
                            artifact_key_fingerprint=fp, artifact_key_id=_cfp_id,
                        ))
                        edges.append(TrustedDerivationEdge(
                            provenance=FUNCTION_SEMANTICS,
                            direction=f"{finer_unit}->{qk.time_unit}", cardinality="EXACT",
                        ))
                        trace.append(f"key {qk.logical_name}: time coarsening {finer_unit}->{qk.time_unit}")
                        any_coarsening = True
                        coarsened_covered_ids.add(_cfp_id)
                        derived = True
                        break
                    elif edge.reason:
                        reason_codes.append(edge.reason)
            if not derived:
                reason_codes.append(DerivedReasonCode.DERIVED_INPUT_KEY_MISSING.value)
                return _source_only(candidate.artifact_id, reason_codes, trace)

        elif qk.kind == "PHYSICAL":
            # Unchanged physical key exact identity (§2.4): match request key_id AND
            # ordered lineage to exactly one manifest key; consume that key_id. A
            # physical key exact-matches only when kind=PHYSICAL_COLUMN and the
            # encoded source_dimension_id agrees. Never resolved by name.
            key_id = qk.key_id
            covered = False
            if key_id and key_id in candidate.grain_key_ids:
                stored_lineage = candidate.grain_key_lineage_by_id.get(key_id, ())
                src_dim = candidate.grain_key_source_dim_by_id.get(key_id)
                encoded_dim = key_id.split(":", 1)[1] if ":" in key_id else None
                if (
                    _ordered_lineage_ok(qk.input_column_ids, stored_lineage)
                    and src_dim and encoded_dim and str(src_dim) == str(encoded_dim)
                ):
                    key_plans.append(DerivedKeyPlan(
                        query_key=qk.logical_name,
                        plan=DIRECT_PHYSICAL_KEY,
                        artifact_key_id=key_id,
                    ))
                    exact_covered_ids.add(key_id)
                    trace.append(f"key {qk.logical_name}: exact physical identity {key_id}")
                    covered = True
            if not covered:
                reason_codes.append(DerivedReasonCode.DERIVED_INPUT_KEY_MISSING.value)
                return _source_only(candidate.artifact_id, reason_codes, trace)

        elif qk.kind == "ATTRIBUTE":
            # A verified attribute edge (§2.4 / §7.6.4). The edge is selected by the
            # query's ``attribute_key`` (identity), then the trust predicate must
            # pass and the edge's ``key_grain_key_id`` resolves the covered key.
            ti = candidate.trust_by_relationship.get(qk.relationship_id or "")
            edge_meta = None
            if qk.attribute_key:
                edge_meta = candidate.edge_by_attribute_key.get(qk.attribute_key)
            if ti is None or edge_meta is None:
                reason_codes.append(DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_UNDECLARED.value)
                return _source_only(candidate.artifact_id, reason_codes, trace)
            # §2.4 identity + endpoint-lineage validation (fail-closed): the
            # canonical attribute_key must encode the SAME relationship the request
            # names, and the selected edge must agree — a request cannot forge an
            # attribute_key that points at a different relationship's edge.
            _encoded_rel = (
                qk.attribute_key.split(":", 1)[1] if qk.attribute_key and ":" in qk.attribute_key
                else None
            )
            if (
                _encoded_rel != str(qk.relationship_id or "")
                or str(edge_meta.get("relationship_id") or "") != str(qk.relationship_id or "")
            ):
                reason_codes.append(DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_UNDECLARED.value)
                return _source_only(candidate.artifact_id, reason_codes, trace)
            # §3.3 declaration-hash agreement: the deployed declaration the binder
            # bound against MUST equal the built edge's declaration_hash. A
            # post-deploy relationship edit (rebound detail, new hash) that the
            # snapshot has not yet re-pinned would otherwise serve the OLD dimension
            # label over the NEW artifact rows (a wrong-numbers path). Fail-closed
            # when either side is missing or they disagree.
            _q_decl = str(qk.declaration_hash or "")
            _edge_decl = str(edge_meta.get("declaration_hash") or "")
            if not _q_decl or not _edge_decl or _q_decl != _edge_decl:
                reason_codes.append(DerivedReasonCode.ATTRIBUTE_DECLARATION_HASH_MISMATCH.value)
                return _source_only(candidate.artifact_id, reason_codes, trace)
            # Identity selects the edge; it does NOT replace trust evaluation.
            ti.manifest_edge = edge_meta
            ti.security_ok = ti.security_ok and security_ok
            tr = evaluate_trust(ti)
            trace.extend(tr.trace)
            if not tr.admitted:
                reason_codes.append(tr.reason_code)
                return _source_only(candidate.artifact_id, reason_codes, trace)
            cardinality = str(edge_meta.get("cardinality", "FUNCTIONAL_N_TO_1"))
            passenger_meta = candidate.passenger_by_attribute_key.get(
                qk.attribute_key or "", {}
            )
            passenger = passenger_meta.get("passenger_column") or edge_meta.get(
                "detail_passenger_column"
            )
            # The canonical id of the grain key this edge sits beside, compared
            # byte-for-byte with one MaterializedGrainKey.key_id (§2.4). Absent or
            # unknown => cannot claim a covered key -> ROLLUP (never a false EXACT).
            edge_key_id = edge_meta.get("key_grain_key_id")
            if edge_key_id and edge_key_id not in candidate.grain_key_ids:
                edge_key_id = None
            # §2.4 / §3.2 endpoint-lineage validation (fail-closed): the covered
            # grain key must materialise EXACTLY the relationship KEY column, and the
            # passenger must carry EXACTLY the relationship DETAIL column. The binder
            # resolved these into ``input_column_ids=(key_column_id, detail_column_id)``
            # from the deployed snapshot; a bijection relabel serves ONLY when both
            # endpoints agree with the built artifact, so a rebound/aliased edge can
            # never launder a different column's values under the query's label.
            if cardinality == "BIJECTION":
                # A bijection relabel MUST carry the ordered (key, detail) endpoints.
                if len(qk.input_column_ids) != 2:
                    reason_codes.append(DerivedReasonCode.ATTRIBUTE_LINEAGE_MISMATCH.value)
                    return _source_only(candidate.artifact_id, reason_codes, trace)
                _q_key_col, _q_detail_col = qk.input_column_ids[0], qk.input_column_ids[1]
                _stored_key_lineage = (
                    candidate.grain_key_lineage_by_id.get(edge_key_id, ())
                    if edge_key_id else ()
                )
                _passenger_src = str(passenger_meta.get("source_column_id") or "")
                # §3.2: the covered grain key MUST be the owning dimension's PHYSICAL
                # key — ``dim:<owning_dimension_id>`` with a matching source_dimension_id
                # — so a relabel can never be served from an ``expr:`` key (a
                # transformed partition) whose single leaf merely IS the key column.
                _owning = str(qk.owning_dimension_id or "")
                _expected_key_id = f"dim:{_owning}" if _owning else None
                _src_dim = candidate.grain_key_source_dim_by_id.get(edge_key_id or "")
                if (
                    _stored_key_lineage != (_q_key_col,)
                    or not _passenger_src
                    or _passenger_src != str(_q_detail_col)
                    or not _owning
                    or edge_key_id != _expected_key_id
                    or str(_src_dim or "") != _owning
                ):
                    reason_codes.append(DerivedReasonCode.ATTRIBUTE_LINEAGE_MISMATCH.value)
                    return _source_only(candidate.artifact_id, reason_codes, trace)
            if cardinality == "BIJECTION":
                key_plans.append(DerivedKeyPlan(
                    query_key=qk.logical_name, plan=DIRECT_ATTRIBUTE_RELABEL,
                    passenger_column=passenger, artifact_key_id=edge_key_id,
                    attribute_key=qk.attribute_key,
                ))
                if edge_key_id:
                    exact_covered_ids.add(edge_key_id)
                trace.append(f"key {qk.logical_name}: verified bijection relabel")
            else:
                # N:1 is a real coarsening; NEVER opportunistically upgraded (I14).
                key_plans.append(DerivedKeyPlan(
                    query_key=qk.logical_name, plan=DERIVED_COARSENING,
                    passenger_column=passenger, artifact_key_id=edge_key_id,
                    attribute_key=qk.attribute_key,
                ))
                any_coarsening = True
                if edge_key_id:
                    coarsened_covered_ids.add(edge_key_id)
                trace.append(f"key {qk.logical_name}: N:1 attribute coarsening")
            edges.append(TrustedDerivationEdge(
                provenance=DATA_VERIFIED_ATTRIBUTE,
                direction=f"{qk.relationship_id}", cardinality=cardinality,
                evidence_id=tr.evidence_id,
            ))
            if tr.evidence_id:
                evidence_ids.append(tr.evidence_id)
        else:
            reason_codes.append(DerivedReasonCode.DERIVED_FUNCTION_UNKNOWN.value)
            return _source_only(candidate.artifact_id, reason_codes, trace)

    # §2.1 / §7.3 cond. 6 — SET cover over canonical key_id values. The verdict is
    # EXACT only when EVERY materialised artifact grain key id is covered by an
    # exact/relabel identity (exact_covered_ids), no artifact key was coarsened, and
    # no artifact key is left uncovered. Any coarsening, or any artifact key id NOT
    # in the exact cover, means a real coarsening -> ROLLUP.
    artifact_ids = set(candidate.grain_key_ids)
    fully_covered_exact = (
        artifact_ids.issubset(exact_covered_ids)
        and not any_coarsening
        and not (coarsened_covered_ids & artifact_ids)
    )
    # EXACT requires a POSITIVE, non-empty proven cover: at least one artifact grain
    # key id, fully covered by exact/relabel identities, nothing coarsened. An
    # artifact with no supplied grain key ids cannot prove exactness (nothing to
    # prove the cover against) -> ROLLUP, which still serves additive measures but
    # refuses direct-only statistics (fail-closed, never a false EXACT).
    if fully_covered_exact and artifact_ids:
        verdict = EXACT
    else:
        verdict = ROLLUP
    key_verdict = VERDICT_EXACT if verdict == EXACT else VERDICT_ROLLUP

    # Measure proof (§7.7 / I4): every requested measure must have a non-SOURCE_ONLY
    # plan at the resolved key verdict; one failure rejects the candidate (§7.8).
    measure_plans: list[MeasureRollupPlan] = []
    for mr in measures:
        mp = classify_measure_rollup(
            measure_name=mr.measure_name, requested_stat=mr.requested_stat,
            key_verdict=key_verdict, available_components=candidate.measure_components,
        )
        measure_plans.append(mp)
        if mp.reason:
            reason_codes.append(mp.reason)

    if not all_measures_servable(measure_plans):
        # A direct-only statistic at ROLLUP (or a missing component) sinks the
        # whole candidate to source — no partial serving (§7.8, pitfall 24).
        return DerivedServeProof(
            verdict=SOURCE_ONLY, artifact_id=candidate.artifact_id,
            query_key_plans=key_plans, measure_plans=measure_plans,
            trusted_edges=edges, relationship_evidence_ids=evidence_ids,
            reason_codes=reason_codes or [DerivedReasonCode.DERIVED_MEASURE_NOT_ROLLUP_SAFE.value],
            proof_trace=trace,
        )

    return DerivedServeProof(
        verdict=verdict, artifact_id=candidate.artifact_id,
        query_key_plans=key_plans, measure_plans=measure_plans,
        trusted_edges=edges, relationship_evidence_ids=evidence_ids,
        reason_codes=reason_codes, proof_trace=trace,
    )
