"""Canonical calendar-type vocabulary — single source of truth.

Before H9 there were two incompatible ``calendar_type`` vocabularies:

* the hierarchy enum ``{standard, fiscal, hijri, iso}`` (model-service /
  frontend), and
* the calendar-table enum ``{standard, fiscal, iso_week, retail_445, hijri,
  thai_buddhist}`` (``calendar_dialects``).

A user could materialise a ``retail_445`` or ``thai_buddhist`` calendar table
but never mark a hierarchy with that type, and a hierarchy typed ``iso`` never
aligned with a calendar table typed ``iso_week`` (F-016-04). This module
collapses the two onto the 6-type set and provides the one legacy mapping
(``iso`` → ``iso_week``) so stored values converge.

The expression-vs-table capability sets live in ``calendar_dialects`` (which
pulls in sqlglot); the bare vocabulary lives here so lightweight consumers
(API validators, schemas) can import it without the SQL machinery.
"""
from __future__ import annotations

# The 6 canonical calendar types. Kept in sync with
# ``calendar_dialects.CALENDAR_TYPES`` via a parity test.
CALENDAR_TYPES: frozenset[str] = frozenset({
    "standard",
    "fiscal",
    "iso_week",
    "retail_445",
    "hijri",
    "thai_buddhist",
})

# Legacy hierarchy tokens that map onto a canonical type. ``iso`` was the
# pre-H9 hierarchy spelling of the ISO-week calendar; it is accepted on input
# and normalised to ``iso_week`` so the hierarchy side and the calendar-table
# side agree.
_LEGACY_CALENDAR_TYPE_ALIASES: dict[str, str] = {
    "iso": "iso_week",
}


def normalize_calendar_type(value: str | None) -> str | None:
    """Map a (possibly legacy) calendar_type token to its canonical form.

    ``None`` passes through unchanged. Unknown values pass through unchanged
    so the caller's validation step can reject them with a precise error
    rather than this helper masking them.
    """
    if value is None:
        return None
    return _LEGACY_CALENDAR_TYPE_ALIASES.get(value, value)


def is_valid_calendar_type(value: str | None) -> bool:
    """True when *value* normalises to one of the canonical calendar types."""
    if value is None:
        return False
    return normalize_calendar_type(value) in CALENDAR_TYPES


__all__ = [
    "CALENDAR_TYPES",
    "normalize_calendar_type",
    "is_valid_calendar_type",
]
