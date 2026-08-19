"""The ``at_grain`` API gate and the SQL renderer must agree (Bug-8573).

Bug-8573 added a whitelist validator on ``KPICreate.at_grain`` /
``KPIUpdate.at_grain`` so a typo ("monthly", "Month") or a raw column name
cannot reach the compiler. The compiler keeps its own copy of the same keyword
set, and the two are now duplicated constants in different packages:

* ``shared.schemas.domains.governance_advanced._KPI_AT_GRAIN_KEYWORDS`` — the
  API boundary gate.
* ``kpi_compiler._GRAIN_KEYWORDS`` — what the renderer actually DATE_TRUNCs on.

They must stay equal in both directions:

* A keyword the compiler supports but the gate rejects → a legitimate grain
  becomes a 422 the modeller cannot work around.
* A keyword the gate accepts but the compiler does not → the value falls to
  ``_safe_ident(raw_grain)`` and the query GROUPs BY a raw COLUMN instead of
  reducing per period. That is the silent wrong-number path Bug-8573 exists to
  close, so the drift would reopen the very defect the validator closed.

This test lives service-side because ``shared/`` cannot import service code.

Tier: T1 producer/consumer contract.
"""
from __future__ import annotations

from shared.schemas.domains.governance_advanced import _KPI_AT_GRAIN_KEYWORDS

from src.kpi_compiler import _GRAIN_KEYWORDS


def test_at_grain_validator_matches_the_compiler_keyword_set():
    assert _KPI_AT_GRAIN_KEYWORDS == _GRAIN_KEYWORDS, (
        "The at_grain API gate and the SQL renderer disagree. Keywords only "
        f"the gate accepts: {sorted(_KPI_AT_GRAIN_KEYWORDS - _GRAIN_KEYWORDS)} "
        "(these would GROUP BY a raw column instead of reducing per period — "
        "the Bug-8573 wrong-number path). Keywords only the compiler supports: "
        f"{sorted(_GRAIN_KEYWORDS - _KPI_AT_GRAIN_KEYWORDS)} (these are "
        "rejected at the API with 422). Update both constants together."
    )


def test_the_shared_keyword_set_is_not_empty():
    """An empty set would make the comparison above vacuously true."""
    assert _KPI_AT_GRAIN_KEYWORDS
