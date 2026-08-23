"""Named Query population contract — the build/serve identity of an NQ artifact.

Bug-9161 (NQ-2) decision of record:
``docs/questions/questions_named-query-population.md``. A Named Query is
materialised over its DEPLOYED definition's canonical semantic relation closure
(the ``force_route="source"`` route: exactly the relations the definition needs,
joined through the model's DECLARED join types) and served live over that SAME
route, so the two populations are identical by construction. The artifact
records a fingerprint of that population contract; a missing or mismatched
fingerprint at serve falls back to live until rebuilt — no unsafe grandfathering
of legacy raw-built artifacts.

This module is DELIBERATELY dependency-light (stdlib only, no httpx, no ORM
imports): the query-router serve path imports it without dragging the build-side
refresh module onto its import graph (Bug-9174/NQ2R1-F7).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

# Bump whenever the NQ population compile logic changes in a way that changes
# what a build materialises: every artifact carrying an older (or no)
# fingerprint is refused at serve and falls back to live until rebuilt.
NQ_POPULATION_CONTRACT_VERSION = 2  # G3 mandatory population-defining joins

# The ONE canonical compile body both refresh build and live serving use.
# A Named Query's population is a function of its deployed definition ALONE:
# the outer ``@name`` reference's shape, its gateway raw/source classification,
# the caller's include_hidden/dialect/protocol, and body.force_route never
# influence it (Bug-9173/NQ2R1-F6).
NQ_CANONICAL_FORCE_ROUTE = "source"
NQ_CANONICAL_PROTOCOL = "jdbc"
NQ_CANONICAL_DIALECT = "postgres"
NQ_CANONICAL_INCLUDE_HIDDEN = False

_NQ_CANONICAL_OPTIONS: dict[str, Any] = {
    "force_route": NQ_CANONICAL_FORCE_ROUTE,
    "protocol": NQ_CANONICAL_PROTOCOL,
    "dialect": NQ_CANONICAL_DIALECT,
    "include_hidden": NQ_CANONICAL_INCLUDE_HIDDEN,
}


def _stable_hash(payload: Any) -> str:
    """Deterministic sha256 over a JSON-canonical payload.

    Same contract as ``shared/semantic/artifact_manifest._stable_hash``
    (``sort_keys`` + ``default=str``) but local, so this module stays
    importable without pulling the manifest machinery onto the serve graph.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:64]


def named_query_population_fingerprint(
    *,
    model_id: Any,
    named_query_id: Any,
    deployed_version_id: Any,
    deploy_epoch: Any,
    definition_sql: str,
) -> str:
    """The population-contract fingerprint an NQ artifact is built under.

    Covers everything that changes what the canonical compile materialises:

    * the population CONTRACT version (``NQ_POPULATION_CONTRACT_VERSION``);
    * the canonical compile options (force_route/protocol/dialect/include_hidden);
    * the deployed model pointer the build was pinned to (version + epoch) and
      the identity of the named query;
    * a sha256 of the EXPANDED definition SQL (the star definition expanded to
      its explicit exposed-field projection — see
      ``shared/named_query/star_expansion`` — so a definition edit, a hidden-
      flag flip, or a field rename all change the fingerprint).

    Build and serve compute it over the SAME inputs (the deployed snapshot +
    deployed definition), so a matching fingerprint means the artifact's
    population and the live compile agree by construction. The deployed
    version/epoch are ALSO covered here so a legacy artifact that happens to
    carry a fingerprint is not re-trusted across a deploy — belt-and-suspenders
    on top of the separate ``artifact_built_for_current`` version gate.
    """
    payload = {
        "schema": NQ_POPULATION_CONTRACT_VERSION,
        "options": dict(_NQ_CANONICAL_OPTIONS),
        "model_id": str(model_id),
        "named_query_id": str(named_query_id),
        "deployed_version_id": str(deployed_version_id),
        "deploy_epoch": int(deploy_epoch or 0),
        "definition_sha256": hashlib.sha256(
            (definition_sql or "").encode()
        ).hexdigest()[:64],
    }
    return _stable_hash(payload)


def named_query_population_manifest_matches(
    *,
    manifest: Optional[dict],
    active_refresh_run_id: Any,
    expected_fingerprint: Optional[str],
) -> bool:
    """True when the artifact's row manifest proves THIS population contract.

    All three must hold (anything unproven -> live fallback):

    1. ``expected_fingerprint`` is present (a caller that cannot name the
       contract it proved proves nothing);
    2. the manifest carries the current manifest version
       (``MANIFEST_VERSION``) and is bound to the artifact's LIVE build
       (``build_refresh_run_id == active_refresh_run_id``) — a manifest from a
       superseded generation describes a different population;
    3. the manifest's ``row_definition_fingerprint`` equals the expected
       fingerprint byte-for-byte.

    A missing/None manifest (legacy raw-built artifact) never matches.
    """
    if not expected_fingerprint:
        return False
    if not isinstance(manifest, dict):
        return False
    if not active_refresh_run_id:
        return False
    try:
        from shared.semantic.artifact_manifest import MANIFEST_VERSION
    except Exception:  # pragma: no cover - defensive
        return False
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        return False
    if str(manifest.get("build_refresh_run_id") or "") != str(
        active_refresh_run_id
    ):
        return False
    return manifest.get("row_definition_fingerprint") == expected_fingerprint
