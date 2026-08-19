"""Declaration-hash for dimension attribute relationships.

Spec: architecture_derived-grain-aggregate-routing.md §5.3 / §7.6.1.

The declaration hash is a stable digest over the MEANING of a declared
key-to-detail relationship: the pinned key column, the detail column, the
cardinality, and the null policy. Any edit that changes meaning changes the
hash. In Phase 2 that hash-change stales all prior verification evidence for the
row (a key rebind must not silently retarget a trusted edge).

This is computed in ONE place so the model-service API and the snapshot
serialiser/rehydrator agree byte-for-byte on the hash for the same declaration.
It deliberately excludes ``enabled`` and timestamps: toggling enablement or
re-saving does not change what the relationship asserts about the data.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional


def compute_declaration_hash(
    *,
    key_column_id: Optional[str],
    detail_column_id: Optional[str],
    cardinality: str,
    null_policy: str = "REJECT_NULL",
) -> str:
    """Return the SHA-256 (hex[:64]) declaration hash for a relationship.

    Inputs are the STABLE identity of the declaration. Column ids are stringified
    so a UUID and its string form hash identically. ``cardinality`` and
    ``null_policy`` are upper-cased for canonical form.
    """
    payload = {
        "kind": "attribute_relationship_declaration",
        "key_column_id": str(key_column_id) if key_column_id is not None else None,
        "detail_column_id": str(detail_column_id) if detail_column_id is not None else None,
        "cardinality": (cardinality or "").strip().upper(),
        "null_policy": (null_policy or "REJECT_NULL").strip().upper(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:64]
