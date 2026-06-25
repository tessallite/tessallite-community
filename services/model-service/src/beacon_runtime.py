"""Model-service wiring for the licence beacon emitter (Bug-5459).

The product knows its ``license_id`` through the license manager
(``get_license_manager().status()``), which model-service builds at startup from the
persisted/active licence. This module turns that into a periodic, license_id-only
beacon by binding the shared ``BeaconEmitter`` to the manager's status.

Default-OFF: ``build_beacon_emitter()`` returns ``None`` when ``LICENSE_BEACON_URL``
is unset, so no beacon runs in source-only/unconfigured deployments. The payload
carries NO PII (the shared emitter enforces a license_id-only allow-list).
"""
from __future__ import annotations

import logging
from typing import Optional

from shared.config.settings import get_settings
from shared.licensing.beacon import BeaconEmitter

from src.licensing_guard import get_license_manager

logger = logging.getLogger(__name__)


def _current_license_id() -> Optional[str]:
    """The active licence id, or None when unactivated. Read live each tick so an
    after-startup activation/replacement is picked up. Never raises."""
    try:
        return get_license_manager().status().get("license_id")
    except Exception:  # noqa: BLE001 — display/telemetry only; degrade silently
        return None


def _current_edition() -> Optional[str]:
    try:
        return get_license_manager().status().get("edition")
    except Exception:  # noqa: BLE001
        return None


def build_beacon_emitter() -> Optional[BeaconEmitter]:
    """Build the emitter from settings, or None when no beacon URL is configured."""
    s = get_settings()
    url = (s.LICENSE_BEACON_URL or "").strip()
    if not url:
        return None
    interval_seconds = float(s.LICENSE_BEACON_INTERVAL_HOURS) * 3600.0
    return BeaconEmitter(
        url=url,
        license_id_fn=_current_license_id,
        interval_seconds=interval_seconds,
        version=(s.LICENSE_BEACON_VERSION or None),
        edition_fn=_current_edition,
    )
