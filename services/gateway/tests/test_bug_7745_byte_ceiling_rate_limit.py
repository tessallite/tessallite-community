"""Bug-7745: gateway query byte ceiling + per-tenant query rate limit.

The public gateway query path must enforce:
  1. A cumulative byte ceiling per query response -- fail-closed.
  2. A per-tenant query rate limit -- fail-closed.

Both controls are configurable via .env / system_settings.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.router_client import (
    GatewayQueryRateLimitExceeded,
    QueryByteCeilingExceeded,
    _enforce_byte_ceiling,
    _GatewayTenantBucket,
    _gw_rate_buckets,
    _gw_rate_lock,
    check_gateway_query_rate,
    execute_query,
)


# ---------------------------------------------------------------------------
# Byte ceiling tests
# ---------------------------------------------------------------------------


class TestByteCeiling:
    """Verify _enforce_byte_ceiling rejects oversized responses."""

    def _make_response(self, content_bytes: int) -> httpx.Response:
        """Build a fake httpx.Response with a specific content length."""
        content = b"x" * content_bytes
        return httpx.Response(200, content=content)

    def test_ceiling_exceeded_raises(self):
        """A response larger than the ceiling is rejected."""
        resp = self._make_response(1024)
        with patch(
            "src.router_client._query_byte_ceiling", return_value=512
        ):
            with pytest.raises(QueryByteCeilingExceeded) as exc_info:
                _enforce_byte_ceiling(resp)
            assert exc_info.value.response_bytes == 1024
            assert exc_info.value.ceiling == 512
            assert "1,024 bytes" in str(exc_info.value)

    def test_ceiling_not_exceeded_passes(self):
        """A response within the ceiling passes silently."""
        resp = self._make_response(256)
        with patch(
            "src.router_client._query_byte_ceiling", return_value=512
        ):
            _enforce_byte_ceiling(resp)  # must not raise

    def test_ceiling_exact_boundary_passes(self):
        """A response exactly at the ceiling passes (boundary is >, not >=)."""
        resp = self._make_response(512)
        with patch(
            "src.router_client._query_byte_ceiling", return_value=512
        ):
            _enforce_byte_ceiling(resp)  # must not raise

    def test_ceiling_disabled_when_zero(self):
        """A ceiling of 0 disables the check entirely."""
        resp = self._make_response(999_999_999)
        with patch(
            "src.router_client._query_byte_ceiling", return_value=0
        ):
            _enforce_byte_ceiling(resp)  # must not raise

    def test_exception_message_contains_actionable_advice(self):
        """The error message tells the user how to fix it."""
        resp = self._make_response(2048)
        with patch(
            "src.router_client._query_byte_ceiling", return_value=1024
        ):
            with pytest.raises(QueryByteCeilingExceeded) as exc_info:
                _enforce_byte_ceiling(resp)
            msg = str(exc_info.value)
            assert "Narrow the query" in msg
            assert "LIMIT" in msg


# ---------------------------------------------------------------------------
# Rate limit tests
# ---------------------------------------------------------------------------


class TestGatewayQueryRateLimit:
    """Verify per-tenant query rate limiting on the gateway path."""

    @pytest.fixture(autouse=True)
    def _clear_buckets(self):
        """Isolate each test from the module-global bucket state."""
        _gw_rate_buckets.clear()
        yield
        _gw_rate_buckets.clear()

    @pytest.mark.asyncio
    async def test_rate_limit_allows_under_capacity(self):
        """Queries under the capacity pass without error."""
        with patch(
            "src.router_client._query_rate_limit_per_minute", return_value=10
        ):
            # 10 queries should all succeed
            for _ in range(10):
                await check_gateway_query_rate("tenant-a")

    @pytest.mark.asyncio
    async def test_rate_limit_rejects_over_capacity(self):
        """The 11th query in a minute is rejected when capacity is 10."""
        with patch(
            "src.router_client._query_rate_limit_per_minute", return_value=10
        ):
            for _ in range(10):
                await check_gateway_query_rate("tenant-a")
            with pytest.raises(GatewayQueryRateLimitExceeded) as exc_info:
                await check_gateway_query_rate("tenant-a")
            assert exc_info.value.retry_after_seconds >= 1
            assert "rate limit exceeded" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_rate_limit_per_tenant_isolation(self):
        """Each tenant has its own independent bucket."""
        with patch(
            "src.router_client._query_rate_limit_per_minute", return_value=2
        ):
            await check_gateway_query_rate("tenant-a")
            await check_gateway_query_rate("tenant-a")
            # tenant-a exhausted, but tenant-b still has capacity
            await check_gateway_query_rate("tenant-b")
            with pytest.raises(GatewayQueryRateLimitExceeded):
                await check_gateway_query_rate("tenant-a")

    @pytest.mark.asyncio
    async def test_rate_limit_disabled_when_zero(self):
        """A capacity of 0 disables the rate limiter entirely."""
        with patch(
            "src.router_client._query_rate_limit_per_minute", return_value=0
        ):
            for _ in range(1000):
                await check_gateway_query_rate("tenant-a")

    @pytest.mark.asyncio
    async def test_rate_limit_refills_over_time(self):
        """Tokens refill over time (token bucket behaviour)."""
        with patch(
            "src.router_client._query_rate_limit_per_minute", return_value=2
        ):
            await check_gateway_query_rate("tenant-a")
            await check_gateway_query_rate("tenant-a")
            # Exhaust the bucket. Manually advance the bucket's last_refill
            # to simulate elapsed time (60s / 2 = 30s per token).
            async with _gw_rate_lock:
                bucket = _gw_rate_buckets["tenant-a"]
                # Simulate 31 seconds passing
                bucket.last_refill -= 31
            await check_gateway_query_rate("tenant-a")  # should succeed


# ---------------------------------------------------------------------------
# Integration: execute_query enforces both controls
# ---------------------------------------------------------------------------


class TestExecuteQueryEnforcement:
    """Verify execute_query wires both controls into the query path."""

    @pytest.fixture(autouse=True)
    def _clear_rate_buckets(self):
        _gw_rate_buckets.clear()
        yield
        _gw_rate_buckets.clear()

    @pytest.mark.asyncio
    async def test_rate_limit_checked_before_request(self):
        """The rate limiter fires BEFORE the HTTP request is sent."""
        with patch(
            "src.router_client._query_rate_limit_per_minute", return_value=1
        ):
            # First call uses the token
            with patch(
                "src.router_client.httpx.AsyncClient"
            ) as mock_client_cls:
                mock_resp = AsyncMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"rows": [], "columns": []}
                mock_resp.content = b'{"rows":[],"columns":[]}'
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                with patch(
                    "src.router_client._query_byte_ceiling", return_value=0
                ):
                    await execute_query(
                        model_id="m1", sql="SELECT 1",
                        tenant_slug="t1", jwt_token="tok1",
                    )

            # Second call: rate limit fires before any HTTP
            with pytest.raises(GatewayQueryRateLimitExceeded):
                await execute_query(
                    model_id="m1", sql="SELECT 1",
                    tenant_slug="t1", jwt_token="tok1",
                )

    @pytest.mark.asyncio
    async def test_byte_ceiling_checked_after_response(self):
        """The byte ceiling fires AFTER the response is received."""
        with patch(
            "src.router_client._query_rate_limit_per_minute", return_value=0
        ), patch(
            "src.router_client._query_byte_ceiling", return_value=10
        ):
            with patch(
                "src.router_client.httpx.AsyncClient"
            ) as mock_client_cls:
                # Build a response whose content exceeds 10 bytes
                mock_resp = AsyncMock()
                mock_resp.status_code = 200
                large_body = b'{"rows":[{"a":1,"b":2,"c":3}],"columns":["a","b","c"]}'
                mock_resp.content = large_body
                mock_resp.json.return_value = {"rows": [{"a": 1}], "columns": ["a"]}
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                with pytest.raises(QueryByteCeilingExceeded):
                    await execute_query(
                        model_id="m1", sql="SELECT 1",
                        tenant_slug="t1", jwt_token="tok1",
                    )
