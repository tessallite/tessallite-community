"""F-013-15 (Bug-9073) — serving-surface axis guard for the calendar family.

The snapshot COVERAGE guard proves a table TRAVELS in the snapshot. It cannot
prove every SERVE surface actually READS the pinned snapshot rather than a live
ORM row. F-013-01 was the exhibit: ``calendar_tables`` was in the snapshot for
export/revert, but the query rewrite path read the live ``CalendarTable`` /
``HierarchyDefinition`` calendar rules, so a draft fiscal-start edit moved
deployed numbers before Deploy. Coverage stayed green the whole time.

This guard closes the recurrence on the CALENDAR axis. It fails CLOSED: any
query-router serving module that reads the calendar family (the ``CalendarTable``
ORM, or a hierarchy's ``calendar_type`` / ``fiscal_year_start_month`` calendar
rules) from a LIVE query must either be the sanctioned deploy-gated consumer
(``calendar_support.py``, which routes deployed reads through
``resolve_calendar_serving_shape``) or carry an ACKNOWLEDGED_GAPS entry stating
why. A NEW module that reads a live calendar row turns this test red.

Per CLAUDE.md's coverage-tool blind-spot rule, this tool's own limits, honestly:

* Discovery is TEXTUAL (marker substrings), so a module that reaches the same
  columns through a renamed alias or a raw SQL string is invisible. Accepted
  because every serving reader today spells the ORM names literally, and the
  anti-vacuous test below fails if the sanctioned consumer stops being found.
* It proves a module DOES / DOES NOT reference the family and that the sanctioned
  consumer references the pin resolver — not that the branch is correct. The
  behaviour is proven by ``test_calendar_deploy_pin.py``. This guard's job is to
  stop a NEW serving surface reading live calendar rows from being forgotten.
"""
from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.unit

# <repo>/tessallite/services/query-router/tests/<this file> -> parents[1] is the
# query-router service root; ``src`` holds every serving module.
SERVICE_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = SERVICE_ROOT / "src"

# A read of the calendar family: the CalendarTable ORM, or a hierarchy's
# calendar RULES (type / fiscal start) that decide period math.
CALENDAR_FAMILY_MARKERS = (
    "CalendarTable",
    "HierarchyDefinition.calendar_type",
    "HierarchyDefinition.fiscal_year_start_month",
)

# The pin resolver every DEPLOYED calendar read must flow through.
PIN_RESOLVER_MARKER = "resolve_calendar_serving_shape"

# rel-path -> why this serving module may reference the calendar family without
# being the sanctioned deploy-gated consumer.
ACKNOWLEDGED_GAPS: dict[str, str] = {
    "src/semantic/snapshot_resolver.py":
        "PRODUCER of the pinned calendar family (builds calendar_tables into "
        "DeployedShape); the only CalendarTable mention is a docstring naming "
        "the live read it exists to replace. It issues no live calendar query.",
}


def _serving_modules():
    for path in SRC.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if any(mk in text for mk in CALENDAR_FAMILY_MARKERS):
            yield path.relative_to(SERVICE_ROOT).as_posix(), text


def test_only_the_deploy_gated_consumer_reads_the_calendar_family():
    offenders = []
    for rel, text in _serving_modules():
        if rel == "src/rewrite/calendar_support.py":
            continue  # sanctioned consumer, checked separately below
        if rel in ACKNOWLEDGED_GAPS:
            continue
        offenders.append(rel)
    assert not offenders, (
        "these query-router serving modules read the calendar family "
        "(CalendarTable / hierarchy calendar rules) live, so a deployed model "
        "can serve draft calendar edits before Deploy (F-013-01 class): "
        + ", ".join(sorted(offenders))
        + " -- route deployed reads through resolve_calendar_serving_shape "
        "(see calendar_support.py) or add an ACKNOWLEDGED_GAPS entry with a "
        "reason."
    )


def test_sanctioned_consumer_still_routes_through_the_pin_resolver():
    """Anti-regression: if calendar_support stops consulting the deploy-pin
    resolver, its live reads would silently leak again while this guard's
    allow-listing of that file stayed green."""
    path = SRC / "rewrite" / "calendar_support.py"
    text = path.read_text(encoding="utf-8")
    assert PIN_RESOLVER_MARKER in text, (
        "calendar_support.py no longer references "
        f"{PIN_RESOLVER_MARKER}; deployed calendar reads may no longer be "
        "pinned to the snapshot (F-013-01 regression risk)."
    )


def test_acknowledged_gaps_do_not_go_stale():
    live = {rel for rel, _ in _serving_modules()}
    stale = sorted(set(ACKNOWLEDGED_GAPS) - live)
    assert not stale, (
        f"remove stale ACKNOWLEDGED_GAPS entries (they no longer reference the "
        f"calendar family): {stale}"
    )


def test_the_sanctioned_consumer_is_still_discovered():
    """Guard the DISCOVERY mechanism itself so the verdict is not vacuous."""
    live = {rel for rel, _ in _serving_modules()}
    assert "src/rewrite/calendar_support.py" in live, (
        "the calendar-family scanner no longer discovers calendar_support.py; "
        "its verdict is now vacuous"
    )
