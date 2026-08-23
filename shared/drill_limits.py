"""Shared drill-through pagination limits.

Bug-5935 (F-019-04): the row-limit-override ceiling must be identical at
every layer that touches it -- the public schema (validation), model-service
(curation save), and query-router (runtime clamp) -- so a modeller cannot
save a value the runtime silently rejects or reduces. This constant is the
single source of truth; import it everywhere the ceiling is enforced instead
of re-declaring the number.
"""
from __future__ import annotations

DRILL_MAX_ROW_LIMIT = 10_000
