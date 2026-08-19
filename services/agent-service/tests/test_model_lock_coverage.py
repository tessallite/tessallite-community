"""Producer-derived guard for the AGENT-SERVICE's model-scoped mutating routes
(Bug-7982 R6 round-3 finding 5).

Today the agent-service's model-scoped routes are OPERATIONAL — they refresh /
upsert per-project agent context derived from the model (``ProjectAgentModel`` and
friends), which is NOT part of the model definition/governance snapshot the
rehydrator truncate-reinserts, so none needs the per-model lock. This guard exists
so a FUTURE model-scoped writer of snapshot-owned state added here FAILS loud. It
reuses the SAME shared engine as the other services' guards.
"""
from __future__ import annotations

import pytest

import src.api as _api_pkg
from shared.db.model_lock_coverage import (
    discover_api_modules,
    lock_is_effective,
    model_scoped_mutating_endpoints,
)

pytestmark = pytest.mark.unit

_ALLOW_NO_LOCK: dict[str, str] = {
    "refresh_derived_context": "agent derived-context refresh trigger (operational), "
                               "not snapshot-owned",
    "upsert_model_context": "per-project agent model context (ProjectAgentModel), "
                            "not in the model definition snapshot",
}


def test_all_agent_api_modules_import():
    _mods, failures = discover_api_modules(_api_pkg)
    assert not failures, f"agent-service src.api modules failed to import: {failures}"


def test_every_agent_model_scoped_mutating_endpoint_effectively_locks():
    failures: list[str] = []
    for module, name, fn in sorted(
        model_scoped_mutating_endpoints(_api_pkg), key=lambda t: (t[0], t[1])
    ):
        if name in _ALLOW_NO_LOCK:
            continue
        ok, reason = lock_is_effective(fn)
        if not ok:
            failures.append(f"{module}:{name}: {reason}")
    assert not failures, (
        "these agent-service model-scoped mutating endpoints do not EFFECTIVELY "
        "acquire the per-model lock and are not allow-listed (Bug-7982):\n  "
        + "\n  ".join(failures)
    )


def test_agent_allow_list_has_no_stale_entries():
    real = {name for _m, name, _f in model_scoped_mutating_endpoints(_api_pkg)}
    stale = sorted(set(_ALLOW_NO_LOCK) - real)
    assert not stale, f"stale _ALLOW_NO_LOCK entries (no such endpoint): {stale}"
