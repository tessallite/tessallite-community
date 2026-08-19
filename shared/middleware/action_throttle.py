"""Per-action quotas for individual endpoints (Bug-6324).

``TenantRateLimitMiddleware`` throttles ingress volume: it stops a tenant
flooding the API. It is the wrong instrument for an action whose cost is not
the request but its SIDE EFFECT — sending mail through the platform's own
verified SMTP identity, for instance, where ten requests an hour is generous
and a hundred is reputational damage that lands on every other tenant.

This module adds a narrow, explicitly-invoked quota for such actions. It
reuses the ``limits`` engine and the ``RATE_LIMIT_STORAGE_URI`` the platform
rate limiter already reads, so:

* no new dependency and no new storage to operate;
* when the operator has pointed the platform at a shared store, these quotas
  are enforced once across all replicas too;
* when they have not, the default ``memory://`` gives per-replica semantics —
  the same documented caveat as the main limiter, and still a hard ceiling
  per process rather than none at all.

Buckets are named by the caller, so a quota can be scoped to whatever unit
actually bounds the abuse (a project, a tenant, a recipient address).
"""
from __future__ import annotations

import logging
from functools import lru_cache

from limits import RateLimitItem, parse as parse_limit  # type: ignore
from limits.storage import storage_from_string  # type: ignore
from limits.strategies import FixedWindowRateLimiter  # type: ignore

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _limiter() -> FixedWindowRateLimiter:
    from shared.config.settings import get_settings
    uri = (get_settings().RATE_LIMIT_STORAGE_URI or "").strip() or "memory://"
    return FixedWindowRateLimiter(storage_from_string(uri))


@lru_cache(maxsize=128)
def _item(limit_str: str) -> RateLimitItem:
    return parse_limit(limit_str)


def consume_action_quota(action: str, *scope: str, limit: str) -> bool:
    """Record one use of *action* in the bucket named by *scope*.

    Returns ``True`` when the use is within quota and ``False`` when it is
    over. A storage failure returns ``True`` (the quota is an abuse control,
    not an authorization decision — a broken counter must not take a working
    product feature offline), and is logged so it is visible.
    """
    try:
        return bool(_limiter().hit(_item(limit), action, *scope))
    except Exception:
        logger.warning(
            "Action quota storage unavailable for %s/%s — allowing the call",
            action, "/".join(scope), exc_info=True,
        )
        return True


def reset_action_quotas() -> None:
    """Drop all counters. Test-support only."""
    _limiter.cache_clear()
    _item.cache_clear()
