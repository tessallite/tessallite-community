"""Immutable artifact serving manifests for derived-grain routing.

Spec: ``docs/architecture/architecture_derived-grain-aggregate-routing.md``
§5.2 (artifact serving manifest) and §5.3 (attribute edges + evidence).

An artifact manifest DESCRIBES what a build actually materialised (invariant I8):
it is written in the SAME lifecycle transaction as the physical artifact + its
verified evidence, and it is immutable for the life of that build. Phase 3 writes
and hashes manifests; no route reads them yet (serving is shadow-only through
Phase 4). These are plain typed dataclasses with deterministic dict/hash forms so
they serialise into the ``grain_keys`` / ``attribute_edges`` / ``passenger_columns``
JSONB columns (aggregates) and ``row_manifest`` (pockets) and round-trip through
the snapshot serialiser as descriptive metadata.

Design rules honoured:
  - The detail passenger is functionally-dependent metadata, NOT an independent
    grain key (§5.3): edges/passengers are separate lists from ``grain_keys``.
  - The manifest hash folds only build-time truth (key ids, physical names,
    types, fingerprints, edge evidence/run ids), never wall-clock time, so two
    builds of the same rows over the same declaration hash the same.
  - No connector branching, no name heuristics: identity is by stable ids +
    canonical fingerprints, never display names.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# Manifest structural version. Bump when the manifest SHAPE changes in a way that
# should invalidate trust in older manifests (spec I8/I12): a router (Phase 4+)
# accepts only a manifest_version it understands.
# Bug-8813: bumped to 2 so every manifest written before the cross-project
# BigQuery guard landed (2026-08-05) is refused at serve time. Old manifests
# that were written in the ~7-day window between producer landing and the
# cross-project guard's deployment survive a simple version check (they still
# carry version 1) and would be admitted under RLS. A bump costs one refresh
# cycle to re-record and provides belt-and-suspenders defence alongside the
# serve-time project check.
MANIFEST_VERSION = 2


# Grain-key kinds (spec §5.2).
KIND_PHYSICAL_COLUMN = "PHYSICAL_COLUMN"
KIND_MODELLED_EXPRESSION = "MODELLED_EXPRESSION"
KIND_ARTIFACT_EXPRESSION = "ARTIFACT_EXPRESSION"

# Edge cardinalities — must match the verifier / ORM vocabulary.
BIJECTION = "BIJECTION"
FUNCTIONAL_N_TO_1 = "FUNCTIONAL_N_TO_1"


@dataclass
class MaterializedGrainKey:
    """One ordered grain key of a built aggregate (spec §5.2).

    ``key_id`` is ``dim:<uuid>`` for a physical/modelled key or ``expr:<sha256>``
    for an artifact-owned expression key. ``physical_column`` is the generated /
    bound physical name in the built table; the human expression stays metadata
    (a generated physical name never becomes a semantic guess — §5.2).
    """
    ordinal: int
    key_id: str
    kind: str
    physical_column: str
    logical_name: Optional[str] = None
    source_dimension_id: Optional[str] = None
    source_uda_id: Optional[str] = None
    canonical_expression: Optional[str] = None
    expression_fingerprint: Optional[str] = None
    input_column_ids: list[str] = field(default_factory=list)
    output_type: Optional[str] = None
    nullable: bool = True
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MaterializedAttributeEdge:
    """A carried key->detail relationship on a built artifact (spec §5.3).

    Names the relationship, the key grain column, the detail passenger column,
    the declared cardinality, the declaration hash it was built for, the
    verification evidence + refresh-run ids it was proved by, and the manifest
    hash. The router (Phase 4+) admits this edge only when EVERY one of these
    still matches the live declaration / evidence / active run (§7.6.4). Phase 3
    only writes it; no route consumes it.
    """
    relationship_id: str
    key_grain_column: str
    detail_passenger_column: str
    cardinality: str
    declaration_hash: str
    verification_id: Optional[str] = None
    artifact_refresh_run_id: Optional[str] = None
    source_data_version: Optional[str] = None
    scope_fingerprint: Optional[str] = None
    manifest_hash: Optional[str] = None
    # Stage-4 relabel serving (spec §2.4 / §3.3). ``attribute_key`` is the
    # canonical ``attr:<relationship-uuid>`` identity carried unchanged from the
    # binder's BoundAttributeRelabel through request -> edge -> passenger ->
    # candidate -> plan -> rewrite. ``key_grain_key_id`` is the canonical
    # ``dim:<uuid>`` / ``expr:<fingerprint>`` id of the grain key this edge sits
    # beside; it is compared BYTE-FOR-BYTE with one MaterializedGrainKey.key_id
    # (never a fingerprint, ``keyid:<...>``, logical name, or physical column) so
    # the §7.3 cond. 6 exact cover accounts for the relabelled key by canonical id.
    attribute_key: Optional[str] = None
    key_grain_key_id: Optional[str] = None
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PassengerColumn:
    """A detail column carried beside its key (spec §5.3, §7.6.3).

    NOT an independent grain key — it is functionally dependent on the key. Its
    source column id + data-tag lineage MUST be retained so CLS still applies
    (pitfall 22); Phase 3 records the lineage id so a later CLS check can resolve
    tags without a name lookup.
    """
    passenger_column: str
    source_column_id: Optional[str]
    output_type: Optional[str]
    nullable: bool
    relationship_id: Optional[str] = None
    # Stage-4 relabel serving (spec §2.4 / §3.3). The canonical
    # ``attr:<relationship-uuid>`` identity; ``build_candidate_manifest`` indexes
    # passengers by this key AND by ``relationship_id`` so the ATTRIBUTE proof
    # resolves the passenger for a relabel by identity, never by name.
    attribute_key: Optional[str] = None
    # Internal build-evidence diagnostic column final physical names (spec §3.6).
    # These prove forward-dependency (one distinct, non-NULL detail per key) at
    # build/activation time; they are NEVER served. Recorded so the artifact-local
    # verifier reads them by name from the immutable manifest, never a source guess.
    detail_ndistinct_column: Optional[str] = None
    detail_nullcount_column: Optional[str] = None
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MaterializedRowColumn:
    """One materialised OUTPUT column of a built pocket table (spec §5.2).

    SECURITY-LOAD-BEARING (Bug-8393). ``logical_name`` and ``physical_column``
    both carry the identifier the pocket TABLE exposes VERBATIM — read back from
    the target catalogue by ``shared/pocket/row_manifest.py``, never parsed from
    the materialisation SELECT and never inferred from the model shape. The
    query-router's RLS gate (Bug-8018) resolves the exposed name as
    ``logical_name or physical_column`` and matches security dimension columns
    against it EXACTLY (case-sensitive), so a paraphrased or case-folded name
    makes the gate fall back to source rather than serve.
    """
    ordinal: int
    logical_name: Optional[str] = None
    physical_column: Optional[str] = None
    output_type: Optional[str] = None
    nullable: bool = True
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RowManifest:
    """Versioned row-population manifest for a pocket (spec §5.2).

    Records the deployed model/version, exact row-population definition +
    fingerprint, ordered materialised column descriptors, source semantic
    context, build/refresh id, carried edge descriptors, and manifest hash.

    ``columns`` is NOT descriptive metadata (Bug-8393). It is the authoritative,
    branch-independent record of what the built pocket table exposes, and the
    query-router serves a pocket under active row-level security ONLY when every
    security column is present in it (Bug-8018). It is written by
    ``shared/pocket/row_manifest.write_pocket_row_manifest`` on every completed
    pocket refresh, in the same transaction as ``active_refresh_run_id``; the
    consumer trusts it only while ``build_refresh_run_id ==
    PocketDefinition.active_refresh_run_id`` and ``manifest_version ==
    MANIFEST_VERSION``.
    """
    deployed_version_id: Optional[str]
    row_definition_fingerprint: Optional[str]
    columns: list[dict[str, Any]] = field(default_factory=list)
    attribute_edges: list[dict[str, Any]] = field(default_factory=list)
    build_refresh_run_id: Optional[str] = None
    # Bug-8473: the routing identity of the storage this build wrote to —
    # ``{target_id, project_connection_id, routing_fingerprint}``. The column
    # names above identify a table only WITHIN a database; which database they
    # resolve to is decided by mutable control-plane state (a target's
    # connection and config). The query-router refuses to scan a pocket whose
    # recorded binding no longer matches live state, so a re-pointed target
    # cannot serve rows from a foreign same-named table.
    target_binding: Optional[dict[str, Any]] = None
    # Bug-8780: the routing identity of the SOURCE database this build read
    # from — ``{model_id, source_connection_id, source_connection_project_id,
    # routing_fingerprint}``. Pockets previously recorded only target binding;
    # adding source binding means the serve-time guard can detect a source
    # re-point (e.g. ``source_db.fallback_*`` changes) and refuse the pocket
    # rather than serving rows from the old database while the source route
    # reads the new one.
    source_binding: Optional[dict[str, Any]] = None
    manifest_hash: Optional[str] = None
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stable_hash(payload: Any) -> str:
    """Deterministic sha256 over a JSON-canonical payload (spec I8/I12).

    ``sort_keys`` + ``default=str`` make the hash order-insensitive at the dict
    level and UUID/str tolerant, so the same build always hashes the same.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:64]


def compute_manifest_hash(
    *,
    grain_keys: list[MaterializedGrainKey],
    attribute_edges: list[MaterializedAttributeEdge],
    passenger_columns: list[PassengerColumn],
) -> str:
    """Immutable build-truth hash over an aggregate manifest (spec §5.3 §7.6.3).

    Folds ONLY the build-time structural content — ordered grain keys, edges
    (minus their own ``manifest_hash`` back-reference, which is filled from this
    value), and passengers. Excludes wall-clock so a rebuild of identical rows
    over an identical declaration produces an identical hash; a changed key,
    column, type, or edge evidence changes it (I8).
    """
    edge_payload = []
    for e in attribute_edges:
        d = e.to_dict()
        d.pop("manifest_hash", None)  # back-reference filled from this hash
        edge_payload.append(d)
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "grain_keys": [k.to_dict() for k in grain_keys],
        "attribute_edges": edge_payload,
        "passenger_columns": [p.to_dict() for p in passenger_columns],
    }
    return _stable_hash(payload)


def compute_row_manifest_hash(manifest: RowManifest) -> str:
    """Immutable build-truth hash over a pocket row manifest (spec §5.2)."""
    d = manifest.to_dict()
    d.pop("manifest_hash", None)
    d.pop("build_refresh_run_id", None)  # run id is a live binding, not identity
    return _stable_hash(d)
