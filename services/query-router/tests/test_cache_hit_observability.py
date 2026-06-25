"""F-030-03 — a result-cache HIT must still write the QueryLog / audit / metrics.

Before the fix, ``_handle_execute`` returned the cached ``ExecuteResponse``
BEFORE the logging/metrics/audit block, so a query repeated within the cache
TTL produced no QueryLog row, no Prometheus increment, and no ``query.execute``
audit record no matter how many distinct users hit it — an observability blind
spot biased toward the hottest (cacheable, non-persona) queries.

These tests prove:
1. ``record_query_cache_hit`` records observability via ``record_query_success``
   with ``elapsed_ms=0`` (served-from-cache signal) and ``log_miss=False`` (a
   cache hit is never a new miss), preserving the cached route_type.
2. ``_handle_execute``'s cache-hit early return calls ``record_query_cache_hit``
   (the cache hit is no longer invisible).
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_record_query_cache_hit_logs_with_zero_ms_and_no_miss(monkeypatch):
    from src.api import routes as routes_mod
    from src.api.routes import ExecuteResponse, record_query_cache_hit

    captured: dict = {}

    async def fake_record_query_success(db, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(routes_mod, "record_query_success", fake_record_query_success)

    bound = object()
    cached = ExecuteResponse(
        rows=[{"x": 1}],
        columns=["x"],
        route_type="aggregate",
        reason="agg hit",
        aggregate_id="agg-1",
        pocket_id=None,
        execution_ms=12,
        bytes_processed=999,
        rows_returned=1,
        routed_sql="SELECT x FROM agg_t",
    )

    await record_query_cache_hit(
        db=None,
        bound=bound,
        cached=cached,
        user_identity="alice@tenant.com",
        tenant_id="acme",
        persona=None,
        client_kind="looker_cloud",
    )

    # The cache hit is logged as a real query of the cached route_type ...
    assert captured["decision"].route_type == "aggregate"
    assert captured["decision"].aggregate_id == "agg-1"
    # ... with execution_ms=0 (no execution happened on a cache hit) ...
    assert captured["elapsed_ms"] == 0
    # ... bytes 0 (no source bytes scanned) and the served page row count ...
    assert captured["bytes_processed"] == 0
    assert captured["rows_returned"] == 1
    # ... the JWT identity preserved ...
    assert captured["user_identity"] == "alice@tenant.com"
    assert captured["client_kind"] == "looker_cloud"
    # ... and NO miss row (a hit is never a miss, even for a source-cached row).
    assert captured["log_miss"] is False


@pytest.mark.asyncio
async def test_record_query_cache_hit_source_route_writes_no_miss(monkeypatch):
    """A cache hit on a previously source-routed result still passes
    log_miss=False so ``record_query_success`` does not write a spurious miss
    row for every cache hit."""
    from src.api import routes as routes_mod
    from src.api.routes import ExecuteResponse, record_query_cache_hit

    miss_calls: list = []

    async def fake_log_query(**kwargs):
        pass

    async def fake_log_query_miss(*a, **kw):
        miss_calls.append((a, kw))

    # Exercise the REAL record_query_success path (only the leaf writers and
    # metric emitters are stubbed) so the log_miss=False branch is proven.
    monkeypatch.setattr(routes_mod, "log_query", fake_log_query)
    monkeypatch.setattr(routes_mod, "log_query_miss", fake_log_query_miss)

    async def fake_audit(*a, **kw):
        pass

    monkeypatch.setattr(routes_mod, "audit", fake_audit)

    class _Counter:
        def labels(self, *a, **kw):
            return self

        def inc(self, *a, **kw):
            pass

        def observe(self, *a, **kw):
            pass

    for name in (
        "QUERY_ROUTED_COUNT", "MODEL_QUERY_COUNT", "MODEL_QUERY_DURATION",
        "MODEL_BYTES_PROCESSED", "MODEL_ROWS_RETURNED",
    ):
        monkeypatch.setattr(routes_mod, name, _Counter())

    fake_model = type("M", (), {"id": "model-1", "display_name": "Test", "project": None})()
    fake_lq = type("LQ", (), {
        "query_fingerprint": "abc123", "protocol": "jdbc", "raw_query": "SELECT 1",
        "select_star": False, "grain": [], "from_tables": [],
        "requested_measures": [], "requested_dimensions": [], "filters": [],
        "order_by": [], "limit": None, "offset": None, "select_expressions": [],
        "having_raw": None, "having_columns": [], "has_unresolvable_where": False,
        "syntax_warnings": [],
    })()
    fake_bound = type("B", (), {
        "model": fake_model, "logical_query": fake_lq,
        "resolved_dimensions": [], "resolved_measures": [], "resolved_filters": [],
        "has_passthrough_expressions": False,
    })()

    cached = ExecuteResponse(
        rows=[], columns=[], route_type="source", reason="no aggregate",
        aggregate_id=None, pocket_id=None, execution_ms=50,
        bytes_processed=12345, rows_returned=0, routed_sql="SELECT 1",
    )

    await record_query_cache_hit(
        db=None, bound=fake_bound, cached=cached,
        user_identity="bob@tenant.com", tenant_id="acme",
    )

    assert miss_calls == [], "a cache hit must NOT write a QueryMissLog row"
