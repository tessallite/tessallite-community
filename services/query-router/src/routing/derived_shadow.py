"""Shadow comparison of a derived-grain proof result against the source result.

Spec: architecture_derived-grain-aggregate-routing.md §16 Phase 4 and §20 Q10.
When ``derived_expression_routing_mode == 'shadow'`` and a candidate proof succeeds,
the platform may compute the would-be DERIVED result AND the authoritative SOURCE
result, compare typed result hashes, and record the outcome. The SOURCE result stays
authoritative and is what the user receives — shadow NEVER changes any returned
value (byte-identical guarantee).

This module is the standalone EVALUATOR + RECORDER:
  - it takes the two already-executed result sets (it does NOT execute SQL);
  - it computes an order-insensitive typed result hash for each;
  - it classifies MATCH | MISMATCH | DERIVED_ERROR | SKIPPED;
  - it respects the sample-rate + cost/privacy controls (§20 Q10: retain typed
    result hashes, not raw values);
  - it returns a typed ``ShadowRecord`` for the caller to persist/log.

Result-set contract: a list of row dicts (col -> value), as the executors return.
The typed hash canonicalises each row to a sorted tuple of (col, typed-repr) pairs,
then hashes the MULTISET of rows (order-insensitive, duplicate-preserving), so two
result sets that differ only in row order hash equal, but a merged/dropped row does
not. Raw values are never stored — only the hash and counts (privacy, §13/§20 Q10).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

# Shadow outcome vocabulary.
MATCH = "MATCH"
MISMATCH = "MISMATCH"
DERIVED_ERROR = "DERIVED_ERROR"
SKIPPED = "SKIPPED"


@dataclass
class ShadowRecord:
    """Typed, privacy-preserving outcome of one shadow comparison (§20 Q10)."""
    outcome: str
    artifact_id: Optional[str]
    verdict: Optional[str]
    source_hash: Optional[str] = None
    derived_hash: Optional[str] = None
    source_row_count: Optional[int] = None
    derived_row_count: Optional[int] = None
    reason_codes: list[str] = field(default_factory=list)
    detail: Optional[str] = None


def _typed_repr(value: Any) -> str:
    """A stable, type-aware string for one cell.

    Distinguishes types that compare unequal in SQL but equal as Python str/num
    (e.g. int 1 vs str "1", None vs "", Decimal vs float) so a genuine type
    discrepancy between the derived and source paths is not hidden by str().
    """
    if value is None:
        return "\x00NULL"
    # Bool BEFORE int (bool is an int subclass) so True/1 stay distinct.
    if isinstance(value, bool):
        return f"b:{int(value)}"
    if isinstance(value, int):
        return f"i:{value}"
    if isinstance(value, float):
        # Normalise -0.0 and format without locale drift.
        return f"f:{value + 0.0!r}"
    return f"s:{type(value).__name__}:{value!r}"


def _row_key(row: dict) -> tuple:
    return tuple(sorted((str(k), _typed_repr(v)) for k, v in row.items()))


def result_hash(rows: list[dict]) -> str:
    """Order-insensitive, duplicate-preserving typed hash of a result set.

    Sorting the per-row keys makes row ORDER irrelevant (BI clients apply ORDER BY
    on their side), while keeping the MULTISET so a dropped/merged/added row
    changes the hash. Cell values are typed (``_typed_repr``) so an int-vs-string
    or NULL-vs-empty discrepancy is caught.
    """
    row_keys = sorted(repr(_row_key(r)) for r in rows)
    h = hashlib.sha256()
    h.update(str(len(rows)).encode())
    for rk in row_keys:
        h.update(b"\x1e")
        h.update(rk.encode())
    return h.hexdigest()[:64]


def should_sample(sample_rate: float, roll: float) -> bool:
    """Decide whether this query is in the shadow sample (§20 Q10 cost control).

    ``roll`` is a caller-provided uniform [0,1) draw (kept as a param so the
    decision is deterministic + unit-testable; the caller uses a real RNG). A rate
    of 0 disables comparison even in shadow mode (matches the config default).
    """
    try:
        rate = float(sample_rate)
    except (TypeError, ValueError):
        return False
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    return roll < rate


def compare_results(
    *,
    artifact_id: Optional[str],
    verdict: Optional[str],
    source_rows: Optional[list[dict]],
    derived_rows: Optional[list[dict]],
    reason_codes: Optional[list[str]] = None,
) -> ShadowRecord:
    """Compare an executed derived result against the authoritative source result.

    ``derived_rows is None`` means the derived execution errored (or was not run) ->
    DERIVED_ERROR, never a false MATCH. Source rows are authoritative; a hash
    mismatch is MISMATCH and MUST quarantine the edge/artifact upstream (the caller
    acts on the record — this module only classifies). Raw rows are never retained.
    """
    codes = list(reason_codes or [])
    if source_rows is None:
        return ShadowRecord(
            outcome=SKIPPED, artifact_id=artifact_id, verdict=verdict,
            reason_codes=codes, detail="no source result to compare",
        )
    src_hash = result_hash(source_rows)
    if derived_rows is None:
        return ShadowRecord(
            outcome=DERIVED_ERROR, artifact_id=artifact_id, verdict=verdict,
            source_hash=src_hash, source_row_count=len(source_rows),
            reason_codes=codes, detail="derived execution produced no result",
        )
    der_hash = result_hash(derived_rows)
    outcome = MATCH if der_hash == src_hash else MISMATCH
    return ShadowRecord(
        outcome=outcome, artifact_id=artifact_id, verdict=verdict,
        source_hash=src_hash, derived_hash=der_hash,
        source_row_count=len(source_rows), derived_row_count=len(derived_rows),
        reason_codes=codes,
    )
