"""Every row-returning query-router ROUTE must publish the denial channel.

R5 review, finding F1. The string-based guard
(``tessallite/tests/unit/test_execute_consumer_enumeration.py``) discovers by
literal path markers and is structurally blind to any route whose path is
composed from an ``APIRouter`` prefix. It therefore never saw
``POST /api/v1/headless/query`` -- a shipped, customer-facing API that runs
``route_query`` with the caller's principal and returned an RLS deny-all as
``rows: [], complete: true``, i.e. the Bug-8453 defect with the Bug-7998
completeness contract actively asserting that the empty set was the whole
dataset. That was the SIXTH missed consumer in this lane.

This guard derives coverage from the LIVE router objects instead of from source
text, so a new row-returning route is in scope the moment it is declared, no
matter how its path is composed. It fails closed: an unrecognised row-returning
response model must either carry ``security_rules_applied`` or earn an explicit,
reasoned entry below.

Its own limits, stated honestly (CLAUDE.md coverage-tool rule):

* It proves a route DECLARES the field, not that it POPULATES it correctly --
  each route's own tests own that. Its job is to stop a surface being forgotten
  entirely, which is the failure that actually kept happening.
* It only sees routers reachable from ``src.api``. A row-returning route
  declared outside that package would be invisible; ``test_discovery_is_not_
  vacuous`` pins the known set so a refactor cannot silently empty the scan.
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest
from fastapi import APIRouter

pytestmark = pytest.mark.integration

# A response model carrying any of these is returning router-produced rows.
ROW_FIELDS = ("rows", "members", "data", "results")

# "METHOD path" -> why this route needs no denial channel.
ACKNOWLEDGED_GAPS: dict[str, str] = {
    "POST /introspect":
        "raw source profiling for the data-quality SERVICE principal "
        "(SCOPE_DATA_QUALITY); it does not run route_query and applies no "
        "semantic row security, so there is no denial to report",
    "POST /introspect/batch": "as /introspect",
    "GET /diagnostics/query-rewrites":
        "tenant-admin diagnostic listing of persisted (raw, rewritten) pairs; "
        "not a routed read of user data",
}


def _row_returning_routes() -> set[tuple[str, str, bool]]:
    import src.api as api_pkg

    found: set[tuple[str, str, bool]] = set()
    for mi in pkgutil.iter_modules(api_pkg.__path__):
        mod = importlib.import_module(f"src.api.{mi.name}")
        for attr in vars(mod).values():
            if not isinstance(attr, APIRouter):
                continue
            for route in attr.routes:
                model = getattr(route, "response_model", None)
                fields = getattr(model, "model_fields", None) or {}
                if not any(f in fields for f in ROW_FIELDS):
                    continue
                for method in sorted(getattr(route, "methods", None) or []):
                    found.add((
                        f"{method} {route.path}",
                        model.__name__,
                        "security_rules_applied" in fields,
                    ))
    return found


def test_every_row_returning_route_publishes_security_rules_applied():
    missing = sorted(
        f"{key} ({model})"
        for key, model, ok in _row_returning_routes()
        if not ok and key not in ACKNOWLEDGED_GAPS
    )
    assert not missing, (
        "these routes return router-produced rows but publish no "
        "security_rules_applied, so a row-security deny-all reaches the caller "
        "as an ordinary empty (or zero) result: " + ", ".join(missing) +
        " -- add the field and populate it from _security_rule_ids(decision), "
        "or add an ACKNOWLEDGED_GAPS entry stating why no denial can occur."
    )


def test_acknowledged_gaps_do_not_go_stale():
    live = {key for key, _, _ in _row_returning_routes()}
    stale = sorted(set(ACKNOWLEDGED_GAPS) - live)
    assert not stale, f"remove stale ACKNOWLEDGED_GAPS entries: {stale}"


def test_discovery_is_not_vacuous():
    """Guard the discovery mechanism itself: if a refactor stops this scanner
    seeing the known routes, every assertion above passes for the wrong
    reason."""
    live = {key for key, _, _ in _row_returning_routes()}
    for expected in (
        "POST /execute",
        "POST /discover/members",
        "POST /api/v1/plugin/execute",
        "POST /api/v1/headless/query",
        "POST /measures/{measure_id}/drill-through",
    ):
        assert expected in live, (
            f"the row-returning-route scanner no longer discovers {expected}; "
            f"its verdict is now vacuous"
        )
