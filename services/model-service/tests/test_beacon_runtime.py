"""Model-service beacon wiring (Bug-5459).

The emitter is default-OFF (no URL -> None) and, when configured, binds its
license_id to the active license manager status (license_id-only; no PII).

Run from tessallite/services/model-service/:
    pytest tests/test_beacon_runtime.py
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src import beacon_runtime as br

pytestmark = pytest.mark.unit


def test_build_returns_none_when_url_unset(monkeypatch):
    from shared.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "LICENSE_BEACON_URL", "")
    assert br.build_beacon_emitter() is None


def test_build_emitter_when_url_configured(monkeypatch):
    from shared.config.settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "LICENSE_BEACON_URL", "https://issuer/beacon")
    monkeypatch.setattr(s, "LICENSE_BEACON_INTERVAL_HOURS", 24.0)
    monkeypatch.setattr(s, "LICENSE_BEACON_VERSION", "1.0.0")

    emitter = br.build_beacon_emitter()
    assert emitter is not None
    assert emitter.enabled is True
    assert emitter._interval == 24.0 * 3600.0
    assert emitter._version == "1.0.0"


def test_license_id_fn_reads_manager_status(monkeypatch):
    mgr = MagicMock()
    mgr.status.return_value = {"license_id": "lic_abc", "edition": "community"}
    monkeypatch.setattr(br, "get_license_manager", MagicMock(return_value=mgr))
    assert br._current_license_id() == "lic_abc"
    assert br._current_edition() == "community"


def test_license_id_fn_degrades_to_none_on_error(monkeypatch):
    def _boom():
        raise RuntimeError("manager unavailable")
    monkeypatch.setattr(br, "get_license_manager", _boom)
    assert br._current_license_id() is None
    assert br._current_edition() is None


def test_unactivated_manager_yields_no_license_id(monkeypatch):
    """An unactivated instance has no license_id, so the beacon tick is skipped."""
    mgr = MagicMock()
    mgr.status.return_value = {"edition": "community", "activated": False}
    monkeypatch.setattr(br, "get_license_manager", MagicMock(return_value=mgr))
    assert br._current_license_id() is None
