"""Bug-6623(2): router_client.get_model_hierarchies completeness contract.

With ``include_details=True`` the enriched hierarchy list is authoritative ONLY
when every hierarchy's detail payload was fetched. A per-hierarchy detail-fetch
failure must PROPAGATE (raise), not silently degrade to a shallow, level-less
item — otherwise the XMLA metadata cache (``_load_model_metadata_cached``, which
caches only a non-raising return) freezes a wrong-shape (flat) hierarchy set for
the whole cache TTL. These tests pin the producer side of that contract; the
consumer side (partial not cached, re-fetch next call) is pinned by
``test_bug_6602_member_discover_freeze.py::...partial_preserve_and_no_cache``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.router_client import get_model_hierarchies  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://model-service/x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _FakeClient:
    """Returns the hierarchy list on the base URL and a per-hierarchy detail
    payload (with a configurable status) on ``.../hierarchies/{hid}``."""

    def __init__(self, *, detail_status: int):
        self.detail_status = detail_status
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, headers=None, params=None):
        self.calls.append(url)
        if url.endswith("/hierarchies"):
            return _FakeResponse(
                200,
                [{"id": "h1", "name": "Time"}, {"id": "h2", "name": "Geo"}],
            )
        hid = url.rsplit("/", 1)[-1]
        return _FakeResponse(
            self.detail_status,
            {"id": hid, "name": hid, "levels": [{"name": f"{hid}-lvl"}]},
        )


def _patch_client(monkeypatch, *, detail_status: int) -> list[_FakeClient]:
    made: list[_FakeClient] = []

    def _factory(**_kwargs):
        client = _FakeClient(detail_status=detail_status)
        made.append(client)
        return client

    monkeypatch.setattr("src.router_client.httpx.AsyncClient", _factory)
    return made


@pytest.mark.asyncio
async def test_detail_failure_propagates_not_swallowed(monkeypatch):
    """A failing per-hierarchy detail fetch must raise, not return a shallow
    item — so the caller never caches an incomplete (flat) hierarchy set."""
    _patch_client(monkeypatch, detail_status=500)
    with pytest.raises(httpx.HTTPStatusError):
        await get_model_hierarchies(
            "m1", "acme", "tok", project_id="p1", include_details=True,
        )


@pytest.mark.asyncio
async def test_all_details_ok_returns_enriched_list(monkeypatch):
    """When every detail fetch succeeds the enriched (level-bearing) list is
    returned in full — the happy path is unchanged."""
    _patch_client(monkeypatch, detail_status=200)
    result = await get_model_hierarchies(
        "m1", "acme", "tok", project_id="p1", include_details=True,
    )
    assert [h["id"] for h in result] == ["h1", "h2"]
    assert all(h.get("levels") for h in result), (
        "each hierarchy must carry its detail levels on the happy path"
    )


class _FakePerHidClient(_FakeClient):
    """Like _FakeClient but with a per-hierarchy detail status map."""

    def __init__(self, *, status_by_hid: dict[str, int]):
        super().__init__(detail_status=200)
        self.status_by_hid = status_by_hid

    async def get(self, url, headers=None, params=None):
        self.calls.append(url)
        if url.endswith("/hierarchies"):
            return _FakeResponse(
                200,
                [{"id": "h1", "name": "Time"}, {"id": "h2", "name": "Geo"}],
            )
        hid = url.rsplit("/", 1)[-1]
        return _FakeResponse(
            self.status_by_hid.get(hid, 200),
            {"id": hid, "name": hid, "levels": [{"name": f"{hid}-lvl"}]},
        )


def _patch_per_hid_client(monkeypatch, status_by_hid: dict[str, int]):
    def _factory(**_kwargs):
        return _FakePerHidClient(status_by_hid=status_by_hid)

    monkeypatch.setattr("src.router_client.httpx.AsyncClient", _factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_status", [404, 410])
async def test_stale_detail_row_is_skipped_not_fatal(monkeypatch, stale_status):
    """Bug-5534 follow-up: a 404/410 detail row means the hierarchy was
    deleted after the listing — its absence is authoritative, so the enriched
    list is complete WITHOUT it. Re-raising made a permanently-dead row block
    the XMLA metadata cache from ever filling (Excel metadata slowness)."""
    _patch_per_hid_client(monkeypatch, {"h1": stale_status})
    result = await get_model_hierarchies(
        "m1", "acme", "tok", project_id="p1", include_details=True,
    )
    assert [h["id"] for h in result] == ["h2"]
    assert result[0].get("levels"), "surviving hierarchy keeps its details"


@pytest.mark.asyncio
async def test_transient_detail_failure_still_propagates(monkeypatch):
    """The Bug-6623(2) contract stands for NON-stale failures: a 500 detail
    fetch still raises so a partial list is never cached as complete."""
    _patch_per_hid_client(monkeypatch, {"h2": 500})
    with pytest.raises(httpx.HTTPStatusError):
        await get_model_hierarchies(
            "m1", "acme", "tok", project_id="p1", include_details=True,
        )


@pytest.mark.asyncio
async def test_include_details_false_skips_detail_fetch(monkeypatch):
    """With include_details=False no per-hierarchy detail call is made, so a
    detail-endpoint outage cannot affect the shallow list."""
    made = _patch_client(monkeypatch, detail_status=500)
    result = await get_model_hierarchies(
        "m1", "acme", "tok", project_id="p1", include_details=False,
    )
    assert [h["id"] for h in result] == ["h1", "h2"]
    # Only the base list URL was hit; no detail URLs.
    assert len(made[0].calls) == 1
    assert made[0].calls[0].endswith("/hierarchies")
