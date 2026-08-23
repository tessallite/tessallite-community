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


# Bug-5920: calendar types whose DDL emitter needs an optional runtime
# dependency. ``hijri`` requires the ``hijri-converter`` package (see
# ``shared/semantic/calendar_dialects.py:_emit_hijri``) — Gregorian calendar
# arithmetic alone cannot produce Hijri dates. ``hijri-converter`` is now a
# shipped dependency (pinned in ``shared/pyproject.toml`` and installed by
# each service Dockerfile), so the type is available in a standard deployment;
# availability is still re-checked at call time via ``find_spec`` so a stripped
# build that drops the package degrades gracefully instead of failing inside
# the DDL emitter. Keep this list in sync with any type whose emitter has an
# optional import.
_OPTIONAL_DEPENDENCY_TYPES: dict[str, str] = {
    "hijri": "hijri_converter",
}


def _dependency_available(module_name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module_name) is not None


def available_calendar_types() -> frozenset[str]:
    """Canonical calendar types that can actually be materialised in this
    deployment right now.

    Bug-5920: the API previously validated ``calendar_type`` against the
    full ``CALENDAR_TYPES`` vocabulary and only failed at DDL-emission time
    with a developer-oriented "pip install hijri-converter" message — the
    frontend compensated with a separate hardcoded ``HIJRI_AVAILABLE``
    flag that could drift from backend reality. This function is the
    single source of truth both sides should consult: it re-checks the
    optional dependency at call time, so installing the package makes the
    type available everywhere without a code change.
    """
    return frozenset(
        t for t in CALENDAR_TYPES
        if t not in _OPTIONAL_DEPENDENCY_TYPES
        or _dependency_available(_OPTIONAL_DEPENDENCY_TYPES[t])
    )


def is_available_calendar_type(value: str | None) -> bool:
    """True when *value* is both canonical and available in this deployment."""
    if value is None:
        return False
    return normalize_calendar_type(value) in available_calendar_types()


__all__ = [
    "CALENDAR_TYPES",
    "normalize_calendar_type",
    "is_valid_calendar_type",
    "available_calendar_types",
    "is_available_calendar_type",
]
