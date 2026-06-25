"""In-memory query result cache with TTL and model-keyed eviction.

Thread-safe via a plain dict (CPython GIL). Single process only.
Cache key: (model_id, tenant_id, principal_hash, query_hash, force_route, include_hidden)
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

CacheKey = tuple[str, ...]


class ResultCache:
    def __init__(self, ttl_seconds: int = 60) -> None:
        self._ttl = ttl_seconds
        self._store: dict[CacheKey, tuple[float, Any]] = {}

    @staticmethod
    def make_query_hash(bound_query_dict: dict) -> str:
        canonical = json.dumps(bound_query_dict, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    @staticmethod
    def make_principal_hash(principal_dict: dict) -> str:
        canonical = json.dumps(principal_dict, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def get(self, key: CacheKey) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return data

    def set(self, key: CacheKey, data: Any) -> None:
        if self._ttl <= 0:
            return  # TTL=0 means caching disabled
        self._store[key] = (time.monotonic() + self._ttl, data)

    def delete(self, key: CacheKey) -> None:
        self._store.pop(key, None)

    def evict_model(self, model_id: str) -> None:
        stale = [k for k in self._store if k[0] == model_id]
        for k in stale:
            del self._store[k]

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
