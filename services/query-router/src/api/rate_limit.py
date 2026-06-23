"""Per-tenant in-memory rate limiter for the JSON query surfaces.

Shared by ``/api/v1/headless/*`` and ``/api/v1/plugin/execute`` so the two
internet-facing JSON endpoints throttle identically (F-027-07: the plugin
endpoint previously had no limiter at all).

Design notes
------------
- The bucket store is a per-process module global. On a single instance this
  is exact; across N replicas (e.g. Cloud Run) the effective per-tenant limit
  is ``N x HEADLESS_RATE_LIMIT`` and resets on scale-to-zero. As a protection
  control this degrades gracefully (limit too high, never too low), so it is
  an operability characteristic, not a security boundary — documented in
  ``help/integrations/headless-api.md`` and the README. A cross-replica
  limiter would move the bucket to PostgreSQL (advisory-lock pattern).
- All bucket access is guarded by a single ``asyncio.Lock`` to close the
  check-then-decrement TOCTOU race (Bug-839).
- ``capacity <= 0`` disables the limiter (returns the configured capacity).
"""
from __future__ import annotations

import math
import time

from fastapi import HTTPException, status

from shared.config.settings import get_settings


class _TenantBucket:
    __slots__ = ("tokens", "last_refill")

    def __init__(self, capacity: int):
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()


# Module-global bucket store, shared across endpoints. Keyed by tenant_id so
# headless and plugin traffic for one tenant draws from the same bucket.
_buckets: dict[str, _TenantBucket] = {}
_buckets_lock = __import__("asyncio").Lock()


async def check_rate_limit(tenant_id: str) -> int:
    """Consume one token for ``tenant_id``; return remaining tokens.

    Raises ``HTTPException`` 429 with a ``Retry-After`` header (seconds until
    one token refills) when the bucket is empty.
    """
    settings = get_settings()
    capacity = settings.HEADLESS_RATE_LIMIT
    if capacity <= 0:
        return capacity

    refill_per_second = capacity / 60.0
    now = time.monotonic()
    async with _buckets_lock:
        bucket = _buckets.get(tenant_id)
        if bucket is None:
            bucket = _TenantBucket(capacity)
            _buckets[tenant_id] = bucket

        # Clamp elapsed to >= 0: a freshly created bucket sets last_refill
        # from its own (slightly later) monotonic read, which would make
        # ``now - last_refill`` negative and spuriously shave a fraction of a
        # token off the first request (visible at capacity=1).
        elapsed = max(0.0, now - bucket.last_refill)
        bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_second)
        bucket.last_refill = now

        if bucket.tokens < 1:
            # Seconds until one whole token is available again.
            deficit = 1.0 - bucket.tokens
            retry_after = max(1, math.ceil(deficit / refill_per_second)) if refill_per_second > 0 else 60
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.tokens -= 1
        return int(bucket.tokens)
