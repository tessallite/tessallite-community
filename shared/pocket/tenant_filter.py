"""Shared tenant-scope filter detection for pocket matching and analysis.

Bug-6990: this module is the single source of truth for tenant-filter
detection. Both the query-router's pocket_matcher and the optimizer's
pocket_candidate_analyzer import from here, eliminating the independent
keyword lists that could drift apart and cause the optimizer to create
pockets the matcher rejects (or vice versa).

Status: active. Last update 2026-07-12.
"""
from __future__ import annotations

from typing import Any, Sequence

TENANT_FILTER_KEYS: frozenset[str] = frozenset({
    "tenant",
    "tenant_id",
    "org",
    "org_id",
    "organization_id",
    "account_id",
    "project_id",
})

_TENANT_SUFFIXES = ("_tenant_id", "_org_id", "_account_id")


def is_tenant_filter_name(name: str) -> bool:
    """Return True when *name* (lowercased) matches a tenant-scope naming
    convention: either an exact match in TENANT_FILTER_KEYS or a suffix
    match for ``_tenant_id`` / ``_org_id`` / ``_account_id``.
    """
    lower = name.lower()
    if lower in TENANT_FILTER_KEYS:
        return True
    for suffix in _TENANT_SUFFIXES:
        if lower.endswith(suffix):
            return True
    return False


def has_tenant_scope_filter_from_filters(query_filters: Sequence[Any]) -> bool:
    """Return True when at least one filter in ``query_filters`` targets a
    tenant-scope column. Each filter is expected to have a
    ``dimension_name`` attribute (LogicalFilter-shaped objects).
    """
    for f in query_filters:
        name = getattr(f, "dimension_name", None) or ""
        if is_tenant_filter_name(name):
            return True
    return False


def has_tenant_scope_predicate_from_dicts(predicates: Sequence[dict]) -> bool:
    """Return True when at least one predicate dict has a ``column_name``
    that matches a tenant-scope naming convention. Used by the optimizer's
    pocket_candidate_analyzer (predicate dicts, not LogicalFilter objects).
    """
    for pred in predicates:
        name = str(pred.get("column_name") or "")
        if is_tenant_filter_name(name):
            return True
    return False
