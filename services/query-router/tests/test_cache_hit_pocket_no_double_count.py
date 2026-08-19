"""Bug-6426 — a result-cache RE-SERVE of a pocket-routed result must not
re-increment the pocket's hit_count or credit more time_saved.

A cache hit did not execute the pocket, so counting it again would inflate the
pocket's ROI/eviction telemetry the same way it inflated the acceleration
metrics. ``log_query`` only runs the pocket UPDATE when ``cache_status`` is NOT
``"cache_hit"``.

These are source-level contract tests: they inspect the ``log_query`` source so
they do not need a live DB (the pocket UPDATE path is db-integration tier), which
keeps them in the isolated coding tier while still failing on a revert.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from src.logging import query_logger


def test_log_query_accepts_cache_status_param():
    """The producer contract: log_query must take a cache_status parameter so a
    cache re-serve can be marked on the QueryLog row."""
    params = inspect.signature(query_logger.log_query).parameters
    assert "cache_status" in params, (
        "log_query must accept cache_status to mark cache re-serves (Bug-6426)"
    )


def test_log_query_writes_cache_status_onto_row():
    """The QueryLog row constructed in log_query must set cache_status from the
    parameter, or the column is never populated and the rollup cannot separate
    cache re-serves from real acceleration."""
    src = inspect.getsource(query_logger.log_query)
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        # Look for QueryLog(...) keyword cache_status=cache_status
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "QueryLog":
                for kw in node.keywords:
                    if kw.arg == "cache_status":
                        found = True
    assert found, "log_query must set QueryLog(cache_status=...) (Bug-6426)"


def test_pocket_increment_is_guarded_against_cache_hit():
    """The pocket hit_count / time_saved UPDATE must be gated on cache_status !=
    'cache_hit', so a cache re-serve of a pocket result does not double-count the
    pocket. Assert the guard token appears in the pocket-increment branch."""
    src = inspect.getsource(query_logger.log_query)
    # The pocket branch and the cache guard must both be present, and the guard
    # must reference the cache_hit sentinel.
    assert 'route_type == "pocket"' in src
    assert 'cache_status != "cache_hit"' in src, (
        "the pocket increment must be skipped for cache re-serves (Bug-6426)"
    )
    # Structural check: the cache guard is part of the SAME condition that gates
    # the pocket UPDATE (both inside one boolean expression that also mentions
    # pocket_id), not an unrelated later statement.
    tree = ast.parse(src)
    guarded = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            cond = ast.dump(node.test)
            if "'pocket'" in cond and "'cache_hit'" in cond and "pocket_id" in cond:
                guarded = True
    assert guarded, (
        "the pocket-increment `if` must combine route_type=='pocket', a "
        "pocket_id check, and cache_status!='cache_hit' in one condition"
    )
