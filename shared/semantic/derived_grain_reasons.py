"""Stable reason codes for derived-grain aggregate routing.

Spec: ``docs/architecture/architecture_derived-grain-aggregate-routing.md`` §10.1.

Every fail-closed decision along the derived-grain path (bind, proof, trust,
rewrite, telemetry) emits one of these stable enum values. Callers MUST depend
on the enum member, never on log-message text (spec §18: "Use an enum for every
rejection reason. Do not depend on log-message text.").

This module is deliberately behaviour-free in Phase 0/1: it only defines the
vocabulary. The binder, router, and query logger consume it as the phases that
introduce diagnostics and (later) serving land.
"""
from __future__ import annotations

from enum import Enum


class DerivedReasonCode(str, Enum):
    """Stable fail-closed reason for a derived-grain routing decision.

    ``str`` mixin so the value serialises directly to JSON/telemetry as its
    name string and compares equal to that string in tests and logs.
    """

    # --- expression / function-semantics rejections (spec §10.1) ---
    DERIVED_EXACT_KEY_NOT_FOUND = "DERIVED_EXACT_KEY_NOT_FOUND"
    DERIVED_INPUT_KEY_MISSING = "DERIVED_INPUT_KEY_MISSING"
    DERIVED_FUNCTION_UNKNOWN = "DERIVED_FUNCTION_UNKNOWN"
    DERIVED_NONDETERMINISTIC = "DERIVED_NONDETERMINISTIC"
    DERIVED_MAY_ERROR = "DERIVED_MAY_ERROR"
    DERIVED_DIALECT_SEMANTICS_UNPROVEN = "DERIVED_DIALECT_SEMANTICS_UNPROVEN"
    DERIVED_TIMEZONE_UNPINNED = "DERIVED_TIMEZONE_UNPINNED"
    DERIVED_COLLATION_UNPINNED = "DERIVED_COLLATION_UNPINNED"
    DERIVED_PREDICATE_NOT_MOVABLE = "DERIVED_PREDICATE_NOT_MOVABLE"
    DERIVED_RLS_KEY_MISSING = "DERIVED_RLS_KEY_MISSING"
    DERIVED_MEASURE_NOT_ROLLUP_SAFE = "DERIVED_MEASURE_NOT_ROLLUP_SAFE"
    DERIVED_LEGACY_MANIFEST_UNPROVEN = "DERIVED_LEGACY_MANIFEST_UNPROVEN"

    # --- data-verified attribute-relationship rejections (spec §10.1) ---
    ATTRIBUTE_RELATIONSHIP_UNDECLARED = "ATTRIBUTE_RELATIONSHIP_UNDECLARED"
    ATTRIBUTE_RELATIONSHIP_UNVERIFIED = "ATTRIBUTE_RELATIONSHIP_UNVERIFIED"
    ATTRIBUTE_RELATIONSHIP_BROKEN = "ATTRIBUTE_RELATIONSHIP_BROKEN"
    ATTRIBUTE_RELATIONSHIP_STALE = "ATTRIBUTE_RELATIONSHIP_STALE"
    # Evidence-AGE bound (Bug-7905): a VERIFIED health row whose ``checked_at`` is
    # older than N x the model's relationship-sweep cadence is expired — the sweep
    # that would have demoted a broken edge may never have re-run (scheduler outage
    # or a persistently-failing demotion write), so a stale VERIFIED row must not
    # keep serving indefinitely. Distinct from ..._STALE (hash/version/scope drift):
    # this is a freshness/liveness bound on otherwise-matching evidence.
    ATTRIBUTE_EVIDENCE_EXPIRED = "ATTRIBUTE_EVIDENCE_EXPIRED"
    ATTRIBUTE_RELATIONSHIP_SCOPE_MISMATCH = "ATTRIBUTE_RELATIONSHIP_SCOPE_MISMATCH"
    ATTRIBUTE_RELATIONSHIP_NULL_ENDPOINT = "ATTRIBUTE_RELATIONSHIP_NULL_ENDPOINT"
    ATTRIBUTE_PASSENGER_MISSING = "ATTRIBUTE_PASSENGER_MISSING"
    ATTRIBUTE_ACTIVE_REFRESH_MISMATCH = "ATTRIBUTE_ACTIVE_REFRESH_MISMATCH"
    # Stage-4 relabel identity guards (spec §2.4 / §3.2 / §3.3). The bound
    # declaration hash disagrees with the built edge's hash (post-deploy rebind),
    # or the edge's covered grain key / passenger does not carry exactly the
    # relationship's key / detail column — either would serve the wrong column's
    # values under the query's label, so both fail closed to source.
    ATTRIBUTE_DECLARATION_HASH_MISMATCH = "ATTRIBUTE_DECLARATION_HASH_MISMATCH"
    ATTRIBUTE_LINEAGE_MISMATCH = "ATTRIBUTE_LINEAGE_MISMATCH"


# The full stable vocabulary, useful for schema enums, telemetry validation,
# and coverage guards that assert every code is exercised by a test.
ALL_DERIVED_REASON_CODES: tuple[str, ...] = tuple(c.value for c in DerivedReasonCode)
