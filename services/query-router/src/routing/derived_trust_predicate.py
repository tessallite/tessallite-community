"""Exact router trust predicate for data-verified attribute edges.

Spec: architecture_derived-grain-aggregate-routing.md §7.6.4. The router admits a
data-verified attribute edge (a Phase-3 artifact-local VERIFIED relationship)
ONLY when ALL of these hold at candidate-selection time:

  1. the declaration is enabled and its hash equals the deployed snapshot hash;
  2. evidence status is VERIFIED and the verifier version is accepted;
  3. tenant, model, deployed version, deploy epoch, key/detail ids, cardinality,
     semantic profile, and security scope all match the bound query;
  4. the evidence artifact id, artifact_refresh_run_id, and manifest hash equal the
     candidate's ACTIVE run and physical manifest;
  5. the aggregate is active and is_stale=false (or the pocket is fresh);
  6. when the connector exposes an immutable source version/watermark it equals
     source_data_version; otherwise the successful refresh run IS the data version;
  7. the passenger column exists with the recorded physical name/type, contains no
     NULL endpoint by evidence, and CLS/RLS checks pass.

This module is PURE: it decides admit/reject over already-loaded ORM objects +
evidence and returns a typed result with a stable DerivedReasonCode on rejection.
It executes NO SQL, writes NO evidence, and NEVER repairs or rechecks an edge on
the query path (§7.6.4: "The router never repairs or rechecks an edge"). Any doubt
-> reject -> the caller continues to another proven candidate or to source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from shared.semantic.derived_grain_reasons import DerivedReasonCode

# Evidence status vocabulary (mirrors shared verifier / ORM).
VERIFIED = "VERIFIED"


@dataclass
class TrustInputs:
    """Everything the predicate needs, resolved by the caller (no DB here)."""
    # The deployed declaration the bound query resolved (enabled + its hash).
    declaration_enabled: bool
    deployed_declaration_hash: Optional[str]
    # The newest evidence row for this relationship (or None).
    evidence: Optional[Any]
    # Accepted verifier version(s) from config.
    accepted_verifier_version: str
    # Candidate artifact live state.
    artifact_active_refresh_run_id: Optional[Any]
    artifact_manifest_hash: Optional[str]
    artifact_is_active: bool
    artifact_is_stale: bool
    # Bound-query scope for the profile/scope match (rule 3).
    bound_deployed_version_id: Optional[Any]
    bound_deploy_epoch: int
    # Connector source watermark when the connector exposes one, else None.
    connector_source_version: Optional[str] = None
    # The manifest edge descriptor for this relationship on the candidate (rule 7).
    manifest_edge: Optional[dict] = None
    # Whether CLS/RLS checks for the edge lineage passed (resolved by caller).
    security_ok: bool = True
    # Bug-7905 evidence-AGE bound. True ONLY when the caller has confirmed the
    # VERIFIED evidence's ``checked_at`` is fresh relative to the model's
    # relationship-sweep cadence (within N x the sweep interval). The caller owns
    # the time/config read; the predicate stays pure. Defaults to False so a caller
    # that does not resolve the bound FAILS CLOSED (expired -> reject -> fall back to
    # normal routing), never serves a stale VERIFIED row that a rolled-back demotion
    # or a scheduler outage never got to re-check.
    evidence_age_ok: bool = False


@dataclass
class TrustResult:
    admitted: bool
    reason_code: Optional[str] = None
    # The evidence + run ids that authorised the edge (for the proof record).
    evidence_id: Optional[str] = None
    trace: list[str] = field(default_factory=list)


def _ev(evidence: Any, attr: str) -> Any:
    return getattr(evidence, attr, None)


def evaluate_trust(inp: TrustInputs) -> TrustResult:
    """Return admit/reject for one data-verified attribute edge (§7.6.4).

    Fails closed on the FIRST failing rule with its stable reason code. The rule
    order follows the spec; each rule's failure maps to a distinct
    DerivedReasonCode so telemetry can attribute the rejection.
    """
    trace: list[str] = []

    # Rule 1 — declaration enabled + hash equals the deployed snapshot hash.
    if not inp.declaration_enabled:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_UNDECLARED.value, trace=trace)
    ev = inp.evidence
    if ev is None:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_UNVERIFIED.value, trace=trace)
    decl_hash = inp.deployed_declaration_hash
    if not decl_hash or _ev(ev, "declaration_hash") != decl_hash:
        # Evidence for a superseded declaration hash is stale by definition.
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_STALE.value, trace=trace)
    trace.append("rule1_declaration_hash_ok")

    # Rule 2 — status VERIFIED and verifier version accepted.
    status = _ev(ev, "status")
    if status != VERIFIED:
        code = (
            DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_BROKEN.value
            if status in ("BROKEN", "ERROR")
            else DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_UNVERIFIED.value
        )
        return TrustResult(False, code, trace=trace)
    if str(_ev(ev, "verifier_version")) != str(inp.accepted_verifier_version):
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_STALE.value, trace=trace)
    trace.append("rule2_verified_and_version_ok")

    # Rule 2b (Bug-7905) — evidence-AGE bound. A VERIFIED verdict is authoritative
    # for serving ONLY while it is FRESH: the periodic health sweep must have
    # re-proved (or had the chance to re-prove) this edge within N x the model's
    # sweep cadence. If a demotion write (VERIFIED->BROKEN) rolled back, or the
    # scheduler stalled, the stale VERIFIED row could otherwise keep serving drifted
    # data indefinitely. The caller resolves ``evidence_age_ok`` from ``checked_at``
    # + the model cadence (fail-closed to False on a missing checked_at or an
    # unreadable cadence), so an expired row is rejected here -> the query falls back
    # to normal routing (correct numbers) rather than serving an un-rechecked relabel.
    if not inp.evidence_age_ok:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_EVIDENCE_EXPIRED.value, trace=trace)
    trace.append("rule2b_evidence_age_ok")

    # Rule 3 — deployed version + deploy epoch match the bound query.
    if _ev(ev, "deployed_version_id") != inp.bound_deployed_version_id:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_STALE.value, trace=trace)
    if int(_ev(ev, "deploy_epoch") or 0) != int(inp.bound_deploy_epoch or 0):
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_STALE.value, trace=trace)
    trace.append("rule3_version_epoch_ok")

    # Rule 4 — evidence artifact refresh run + manifest hash equal the candidate's
    # ACTIVE run and physical manifest.
    if inp.artifact_active_refresh_run_id is None:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_ACTIVE_REFRESH_MISMATCH.value, trace=trace)
    if _ev(ev, "artifact_refresh_run_id") != inp.artifact_active_refresh_run_id:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_ACTIVE_REFRESH_MISMATCH.value, trace=trace)
    ev_manifest = _ev(ev, "artifact_manifest_hash")
    if not ev_manifest or ev_manifest != inp.artifact_manifest_hash:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_ACTIVE_REFRESH_MISMATCH.value, trace=trace)
    trace.append("rule4_active_run_manifest_ok")

    # Rule 5 — aggregate active + not stale (or pocket fresh — caller sets flags).
    if not inp.artifact_is_active or inp.artifact_is_stale:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_STALE.value, trace=trace)
    trace.append("rule5_artifact_fresh_ok")

    # Rule 6 — connector watermark equals evidence source_data_version when present.
    if inp.connector_source_version is not None:
        if str(_ev(ev, "source_data_version") or "") != str(inp.connector_source_version):
            return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_STALE.value, trace=trace)
    trace.append("rule6_source_version_ok")

    # Rule 7 — passenger present with the recorded name, no NULL endpoint by
    # evidence, and CLS/RLS pass. NULL is a fail-closed evidence property.
    edge = inp.manifest_edge or {}
    if not edge.get("detail_passenger_column"):
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_PASSENGER_MISSING.value, trace=trace)
    if int(_ev(ev, "violation_count") or 0) != 0:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_NULL_ENDPOINT.value, trace=trace)
    if not inp.security_ok:
        return TrustResult(False, DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_SCOPE_MISMATCH.value, trace=trace)
    trace.append("rule7_passenger_null_security_ok")

    return TrustResult(
        True, None, evidence_id=str(_ev(ev, "id")) if _ev(ev, "id") else None, trace=trace,
    )
