"""Product-side licence beacon EMITTER (PUBLIC — ships in the Community build).

Counterpart to the issuer ``/beacon`` SINK (``deploy/issuer-function``). The sink was
already wired server-side, but with no product emitter ``received_at`` stayed NULL
forever (Bug-5459). This module is that missing emitter: a best-effort, offline-tolerant
periodic POST of the running instance's licence heartbeat to the configured endpoint.

PII discipline (spec §beacon): the payload carries ``license_id`` ONLY (plus the
non-identifying ``version``/``edition`` the sink already records, and a ``sent_at``
timestamp). NO email/company/name/consent — those live only in the issuer registry +
leads sink and are joined later by ``license_id``. Do NOT add identifying fields here.

Config-driven and default-OFF: with no ``LICENSE_BEACON_URL`` configured the emitter
never starts, so source-only / air-gapped / unconfigured deployments are silent. Every
network path swallows failures — a beacon outage must never affect the product.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Fields the beacon is ALLOWED to carry. license_id is the only identifier; the
# rest are non-identifying telemetry the sink already stores. This allow-list is
# the guard that keeps PII off the wire — the payload is built from it explicitly.
ALLOWED_BEACON_FIELDS = ("license_id", "version", "edition", "sent_at")

# Floor on cadence so a misconfigured tiny interval can't hammer the endpoint.
_MIN_INTERVAL_SECONDS = 60.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_beacon_payload(
    *, license_id: str, version: str | None = None, edition: str | None = None
) -> dict[str, Any]:
    """Construct the license_id-only beacon body. NO PII by construction.

    Only the ``ALLOWED_BEACON_FIELDS`` are ever emitted; there is deliberately no
    parameter to pass arbitrary fields, so an identifier can't leak in.
    """
    payload: dict[str, Any] = {"license_id": license_id, "sent_at": _now_iso()}
    if version is not None:
        payload["version"] = version
    if edition is not None:
        payload["edition"] = edition
    return payload


async def emit_once(
    url: str,
    *,
    license_id: str,
    version: str | None = None,
    edition: str | None = None,
    timeout_seconds: float = 10.0,
) -> bool:
    """POST a single beacon. Best-effort: returns True on a 2xx, False otherwise.

    Never raises — every transport/HTTP failure is swallowed and logged at debug so an
    offline or unreachable issuer can't disturb the product (offline tolerance).
    """
    if not url or not license_id:
        return False
    payload = build_beacon_payload(license_id=license_id, version=version, edition=edition)
    try:
        import httpx  # local import keeps the module importable where httpx is absent

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(url, json=payload)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.debug("beacon non-2xx from %s: %s", url, resp.status_code)
        return ok
    except Exception:  # noqa: BLE001 — offline tolerance: never propagate
        logger.debug("beacon emit failed (offline?)", exc_info=True)
        return False


class BeaconEmitter:
    """Periodic background beacon. Default-OFF: ``start()`` no-ops without a URL.

    ``license_id_fn`` is read each tick so the emitter picks up a licence that is
    activated (or replaced) after startup — when no licence is active it yields no
    ``license_id`` and the tick is skipped (nothing to report, still no PII).
    """

    def __init__(
        self,
        *,
        url: str,
        license_id_fn: Callable[[], Optional[str]],
        interval_seconds: float,
        version: str | None = None,
        edition_fn: Callable[[], Optional[str]] | None = None,
        emit_fn: Callable[..., Awaitable[bool]] = emit_once,
    ) -> None:
        self._url = url or ""
        self._license_id_fn = license_id_fn
        self._interval = max(float(interval_seconds), _MIN_INTERVAL_SECONDS)
        self._version = version
        self._edition_fn = edition_fn
        self._emit_fn = emit_fn
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def _tick(self) -> None:
        license_id = self._license_id_fn()
        if not license_id:
            return  # no active licence yet — nothing to beacon, no PII
        edition = self._edition_fn() if self._edition_fn else None
        await self._emit_fn(
            self._url, license_id=license_id, version=self._version, edition=edition
        )

    async def _run(self) -> None:
        # Best-effort beacon on startup, then on the configured cadence. The whole
        # loop is wrapped so a stray error can never crash the host service.
        try:
            await self._tick()
            while True:
                await asyncio.sleep(self._interval)
                await self._tick()
        except asyncio.CancelledError:  # clean shutdown
            raise
        except Exception:  # noqa: BLE001 — offline tolerance
            logger.debug("beacon loop stopped on error", exc_info=True)

    def start(self) -> bool:
        """Launch the background loop. No-op (returns False) when unconfigured."""
        if not self.enabled or self._task is not None:
            return False
        self._task = asyncio.create_task(self._run(), name="license-beacon")
        logger.info("license beacon emitter started (interval=%.0fs)", self._interval)
        return True

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


__all__ = ["BeaconEmitter", "emit_once", "build_beacon_payload", "ALLOWED_BEACON_FIELDS"]
