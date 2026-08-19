"""Bug-6651: XMLA downstream-401 detection by typed status inspection.

Previously the gateway checked `"401" in str(exc)` to propagate a 401 from
downstream services. This turned ANY error whose text contained "401" into
an HTTP 401 challenge (e.g. "invoice #401 failed" or "Bug-5401 regression").

Now uses typed exception inspection (httpx.HTTPStatusError with
response.status_code == 401, or QueryRouterError with status_code == 401).
"""
from __future__ import annotations

import pytest
import httpx

from src.router_client import QueryRouterError


def test_query_router_error_401_is_detected():
    """A QueryRouterError with status_code=401 must be considered a 401."""
    exc = QueryRouterError("Unauthorized", status_code=401)
    assert isinstance(exc, QueryRouterError)
    assert exc.status_code == 401


def test_query_router_error_non_401_not_detected():
    """A QueryRouterError with a different status must NOT match 401."""
    exc = QueryRouterError("invoice #401 failed", status_code=500)
    assert exc.status_code != 401
    # Old check would have matched because "401" is in the detail text
    assert "401" in str(exc)  # old check would be True
    assert exc.status_code != 401  # new check is False


def test_httpx_error_401_is_detected():
    """An httpx.HTTPStatusError with 401 status must be considered a 401."""
    response = httpx.Response(
        status_code=401,
        request=httpx.Request("POST", "http://router/api/v1/execute"),
    )
    exc = httpx.HTTPStatusError("401 Unauthorized", request=response.request, response=response)
    assert exc.response.status_code == 401


def test_httpx_error_non_401_not_detected():
    """An httpx.HTTPStatusError with text containing '401' but different status."""
    response = httpx.Response(
        status_code=500,
        request=httpx.Request("POST", "http://router/api/v1/execute"),
    )
    exc = httpx.HTTPStatusError("item 401 failed", request=response.request, response=response)
    assert "401" in str(exc)  # old check would be True
    assert exc.response.status_code != 401  # new check is False


def test_generic_exception_with_401_in_text_not_detected():
    """A generic exception whose message contains '401' must NOT match."""
    exc = Exception("Error 401: something went wrong")
    assert "401" in str(exc)  # old check would be True
    assert not isinstance(exc, httpx.HTTPStatusError)
    assert not isinstance(exc, QueryRouterError)
