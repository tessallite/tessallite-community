"""License verification (PUBLIC — used by the gateway guard / license manager).

Offline, public-key-only. Verifies the signature against the canonical payload
and checks expiry. Never has access to the private signing key.
"""
from __future__ import annotations

import base64
from datetime import datetime

from cryptography.exceptions import InvalidSignature as _CryptoInvalidSignature

from .errors import (
    InvalidSignature,
    LicenseExpired,
    MalformedLicense,
    UnknownKeyId,
    UnsupportedAlgorithm,
)
from .keys import ALG_ED25519, KeyRegistry
from .schema import License


def _split_signature(sig: str) -> tuple[str, bytes]:
    """``"ed25519:<base64>"`` -> ``("ed25519", raw_bytes)``."""
    if not sig or ":" not in sig:
        raise MalformedLicense("signature must be '<algorithm>:<base64>'")
    algorithm, b64 = sig.split(":", 1)
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise MalformedLicense(f"signature is not valid base64: {exc}") from exc
    return algorithm, raw


def verify_license(
    doc_or_license: dict | License,
    registry: KeyRegistry,
    *,
    now: datetime | None = None,
    check_expiry: bool = True,
) -> License:
    """Verify signature + (optionally) expiry. Returns the validated License.

    Raises MalformedLicense / UnknownKeyId / UnsupportedAlgorithm / InvalidSignature
    / LicenseExpired. Pure and offline.
    """
    lic = (
        doc_or_license
        if isinstance(doc_or_license, License)
        else License.from_dict(doc_or_license)
    )

    sig = lic.signature
    if not sig:
        raise MalformedLicense("license has no signature")
    algorithm, raw_sig = _split_signature(sig)

    entry = registry.get(lic.key_id)
    if entry is None:
        raise UnknownKeyId(f"no verification key for key_id {lic.key_id!r}")
    if algorithm != entry.algorithm:
        raise UnsupportedAlgorithm(
            f"signature algorithm {algorithm!r} != key algorithm {entry.algorithm!r}"
        )

    if algorithm == ALG_ED25519:
        try:
            entry.public_key.verify(raw_sig, lic.canonical_bytes())
        except _CryptoInvalidSignature as exc:
            raise InvalidSignature("license signature does not verify") from exc
    else:
        raise UnsupportedAlgorithm(f"unsupported signature algorithm: {algorithm!r}")

    if check_expiry and lic.is_expired(now):
        raise LicenseExpired(f"license {lic.license_id} expired at {lic.expires_at}")

    return lic
