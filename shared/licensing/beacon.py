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
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class BeaconConfigError(RuntimeError):
    """Bug-8374: a FATAL, must-fix beacon-encryption configuration error.

    Raised by the startup validators when a beacon ENC key is set but malformed
    (not a valid Fernet key), so a misconfigured deploy FAILS FAST at startup
    instead of silently losing every beacon. Distinct from the valid half-config
    case (product key set, sink key missing), which is only a WARNING because the
    two sides may be deployed separately and telemetry recovers once the matching
    key lands. Startup/cold-start boundaries must NOT catch this — that is the
    point of raising it.
    """

# Fields the beacon is ALLOWED to carry. license_id is the only identifier; the
# rest are non-identifying telemetry the sink already stores. This allow-list is
# the guard that keeps PII off the wire — the payload is built from it explicitly.
ALLOWED_BEACON_FIELDS = ("license_id", "version", "edition", "sent_at")

# Floor on cadence so a misconfigured tiny interval can't hammer the endpoint.
_MIN_INTERVAL_SECONDS = 60.0

# Bug-8296: optional shared symmetric key (a Fernet key) that encrypts the beacon
# payload on the wire. Set BEACON_ENC_KEY here on the product AND the SAME value as
# ISSUER_BEACON_ENC_KEY on the issuer sink; the sink then DROPS any beacon it cannot
# decrypt. Unset = plaintext (graceful pre-8296 behavior). NEVER hardcode the key —
# it comes from the environment only.
_ENC_KEY_ENV = "BEACON_ENC_KEY"

# Bug-8374: the matching issuer-sink env var. Named here only so the product-side
# startup validator can warn about the half-config trap — a product that encrypts
# every beacon while the sink is not configured to decrypt silently loses all
# telemetry. This module never READS the issuer key (that belongs to the sink); it
# only checks whether the operator remembered to wire it.
_ISSUER_ENC_KEY_ENV = "ISSUER_BEACON_ENC_KEY"


def _beacon_enc_key() -> str | None:
    value = os.environ.get(_ENC_KEY_ENV, "").strip()
    return value or None


def beacon_startup_validate() -> None:
    """Bug-8374: fail-fast on an unsafe beacon-encryption configuration (product side).

    ``BEACON_ENC_KEY`` opts the product emitter into Fernet-encrypting every beacon
    (see ``encrypt_beacon_body``); the issuer sink must hold the SAME key as
    ``ISSUER_BEACON_ENC_KEY`` or it drops every beacon as undecryptable. Two hazards
    this catches at startup, before any beacon is silently lost:

      1. **Malformed key.** ``BEACON_ENC_KEY`` is set but is not a valid Fernet key,
         so every ``encrypt_beacon_body`` call would fail. This RAISES
         ``BeaconConfigError`` so the caller fails fast — an explicitly misconfigured
         key must not degrade to silently-lost telemetry.
      2. **Half-config.** ``BEACON_ENC_KEY`` is set and valid but the operator has
         not also set ``ISSUER_BEACON_ENC_KEY`` on the sink. The emitter would send
         ``{"enc": ...}`` while the sink expects plaintext and DROPS every beacon —
         the worst case, because the operator believes telemetry is flowing. Logged
         as a strong WARNING naming the missing issuer variable; NOT fatal, because
         the sink half may be deployed separately and recovers once its key lands.

    Returns ``None`` when the configuration is safe (including the common default:
    no key set at all, plaintext mode) or a valid half-config; RAISES
    ``BeaconConfigError`` on hazard (1).
    """
    key = os.environ.get(_ENC_KEY_ENV, "").strip()
    if not key:
        return None  # plaintext mode (default) — nothing to validate
    try:
        from cryptography.fernet import Fernet

        Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any construction failure == bad key
        raise BeaconConfigError(
            f"{_ENC_KEY_ENV} is set but is not a valid Fernet key: {exc}"
        ) from exc
    return None


def encrypt_beacon_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap the beacon payload for the wire.

    When ``BEACON_ENC_KEY`` (a Fernet key) is configured, the payload JSON is
    encrypted with that shared symmetric key and returned as ``{"enc": <token>}``;
    the issuer sink decrypts it and DROPS anything it cannot decrypt. With no key
    configured the plaintext payload is returned unchanged, so an unconfigured or
    older deployment keeps working exactly as before. Reuses the project's existing
    symmetric primitive (Fernet — the same one that encrypts connection
    credentials); no bespoke crypto."""
    key = _beacon_enc_key()
    if not key:
        return payload
    from cryptography.fernet import Fernet  # local import: keeps the module light

    token = (
        Fernet(key.encode("utf-8"))
        .encrypt(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
        .decode("ascii")
    )
    return {"enc": token}


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

        # Bug-8296: encrypt the payload when a shared key is configured (else plaintext).
        # Inside the try so a misconfigured key degrades to a swallowed no-op, never a
        # raise out of this best-effort emitter.
        body = encrypt_beacon_body(payload)
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(url, json=body)
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
        # Best-effort beacon on startup, then on the configured cadence. Each
        # tick is isolated so a stray error cannot stop later heartbeats (F-031-18).
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — offline tolerance
                logger.debug("beacon tick failed", exc_info=True)
            await asyncio.sleep(self._interval)

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


__all__ = [
    "BeaconEmitter",
    "BeaconConfigError",
    "emit_once",
    "build_beacon_payload",
    "encrypt_beacon_body",
    "beacon_startup_validate",
    "ALLOWED_BEACON_FIELDS",
]
