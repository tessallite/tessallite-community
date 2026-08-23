"""Canonical hash functions for pocket table identity.

``fingerprint_shape`` produces the query-shape hash shared by the
query-router (at parse time) and the pocket matcher (at match time).

``predicate_set_hash`` produces the value-aware hash that distinguishes
pockets with the same shape but different WHERE literal values.

All SQL parsing, predicate extraction, and structural validation is
done exclusively by the query-router. This module contains only the
hash algorithms consumed by both sides.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def fingerprint_shape(
    *,
    measures: Iterable[str],
    dimensions: Iterable[str],
    grain: Iterable[str],
    filter_cols: Iterable[str],
    having_cols: Iterable[str] | None = None,
    expr_fingerprints: Iterable[str] | None = None,
) -> str:
    """SHA-256 (hex[:64]) of a canonical column-shape payload.

    The payload intentionally excludes literal filter values so that
    ``country = 'GB'`` and ``country = 'US'`` hash identically — the
    router then narrows via predicate-subset matching on the stored
    ``pocket_predicates`` rows.

    ``expr_fingerprints`` (spec §5.1 / I11) carries the derived-grain expression
    identities of a function-grain query — canonical expression fingerprints for
    GROUP BY / SELECT / WHERE / HAVING / ORDER occurrences — so two queries that
    differ only in their inline expression (e.g. ``DATE_TRUNC('month', ts)`` vs
    ``EXTRACT(month FROM ts)``) hash distinctly instead of colliding on the
    expression-blind ``has_function_grain`` boolean. Structural expression
    literals (the ``'month'`` unit, timezone, substring position) are part of
    each fingerprint; ordinary predicate VALUES are not.

    Byte-identity guarantee: the ``derived_exprs`` key is added to the payload
    ONLY when ``expr_fingerprints`` is non-empty. Every existing caller that
    passes no expression fingerprints therefore produces exactly the pre-feature
    digest — ordinary and source-path fingerprints are unchanged.
    """
    data = {
        "measures": sorted(str(m) for m in measures),
        "dimensions": sorted(str(d) for d in dimensions),
        "grain": sorted(str(g) for g in grain),
        "filter_cols": sorted(str(c) for c in filter_cols),
        "having_cols": sorted(str(h) for h in (having_cols or [])),
    }
    _exprs = [str(e) for e in (expr_fingerprints or [])]
    if _exprs:
        # Order-preserving: expression order carries meaning (SELECT/GROUP/ORDER
        # position), unlike the sorted column lists above.
        data["derived_exprs"] = _exprs
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:64]


def predicate_set_hash(predicates: Iterable[dict[str, Any]]) -> str:
    """SHA-256 (hex[:64]) of a canonical predicate-set payload.

    Pocket identity is ``(model_id, query_fingerprint, predicate_set_hash)``
    — two pockets with the same defining query shape but different WHERE
    values are distinct rows. Unlike ``fingerprint_shape``, this hash
    DOES include literal values.

    Canonicalisation: each predicate is normalised to ``{column, operator,
    value}`` with lowercased column+operator, then the list is sorted by
    the JSON-encoded tuple. Empty predicate set hashes to a stable,
    non-empty digest so the UNIQUE constraint still distinguishes rows.

    The payload is namespaced with ``"kind": "predicate_set"`` so it
    cannot collide with any shape-fingerprint domain.
    """
    normalised: list[tuple[str, str, str]] = []
    for p in predicates or []:
        col = str(p.get("column_name") or "").lower()
        op_ = str(p.get("operator") or "").lower()
        val_json = json.dumps(p.get("value"), sort_keys=True, default=str)
        normalised.append((col, op_, val_json))
    normalised.sort()
    data = {"kind": "predicate_set", "predicates": normalised}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:64]
