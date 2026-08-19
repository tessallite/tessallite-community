"""Deterministic readiness checks for the opt-in live integration profile."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.integration import live_profile


def test_bug_8692_localhost_health_probe_falls_back_to_ipv4(monkeypatch):
    """An IPv6-only localhost resolution must not skip a reachable IPv4 stack."""
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        if "localhost" in url:
            raise OSError("simulated ::1 connection refusal")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(live_profile.httpx, "get", fake_get)

    assert live_profile.api_up("http://localhost:8001/api/v1") is True
    assert calls == [
        "http://localhost:8001/health",
        "http://127.0.0.1:8001/health",
    ]


def test_bug_8692_non_localhost_profile_is_not_redirected(monkeypatch):
    """Fallback must not hide an unavailable or mistyped explicit host."""
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        raise OSError("unreachable")

    monkeypatch.setattr(live_profile.httpx, "get", fake_get)

    assert live_profile.api_up("http://model-service:8001/api/v1") is False
    assert calls == ["http://model-service:8001/health"]


@pytest.mark.parametrize(
    ("api_base", "expected"),
    [
        ("", []),
        ("not a URL", []),
        (
            "http://localhost/api/v1",
            ["http://localhost", "http://127.0.0.1"],
        ),
    ],
)
def test_health_probe_urls_are_explicit_and_path_free(api_base, expected):
    assert live_profile.health_probe_urls(api_base) == expected


def test_bug_8692_health_probe_preserves_a_proxy_path_prefix():
    """Bug-9009 / P1-MS-TEST-INFRA-SELF-002 preserves the existing root."""
    assert live_profile.health_probe_urls(
        "https://localhost/model-service/api/v1"
    ) == [
        "https://localhost/model-service",
        "https://127.0.0.1/model-service",
    ]
