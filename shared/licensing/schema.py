"""License schema + canonical serialization.

PUBLIC (shipped in the shell). Defines the signed-license document, its required
fields, canonical byte serialization for signing/verification, and entitlement
access. Signing/verification live in ``issuer.sign`` (private) and ``verify`` (public).

Canonicalization rule (used identically by signer and verifier): JSON of the
payload **without** the ``signature`` field, sorted keys, compact separators,
UTF-8, ``ensure_ascii=False``. Any divergence breaks verification, so this is the
single source of truth.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import MalformedLicense

SCHEMA_VERSION = 1
SIGNATURE_FIELD = "signature"

# Fields required on every license (spec §7).
_REQUIRED = (
    "schema_version",
    "license_id",
    "key_id",
    "issuer",
    "edition",
    "issued_at",
    "product",
    "entitlements",
)


def canonical_bytes(doc: dict[str, Any]) -> bytes:
    """Canonical bytes that the signature covers: the doc minus ``signature``."""
    payload = {k: v for k, v in doc.items() if k != SIGNATURE_FIELD}
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class License:
    """A parsed, structurally-valid license document.

    Holds the raw ``doc`` (the exact dict that was signed/loaded) plus typed
    accessors. Signature validity is established separately by ``verify``.
    """

    doc: dict[str, Any]

    @property
    def license_id(self) -> str:
        return str(self.doc["license_id"])

    @property
    def key_id(self) -> str:
        return str(self.doc["key_id"])

    @property
    def edition(self) -> str:
        return str(self.doc["edition"])

    @property
    def entitlements(self) -> dict[str, Any]:
        return dict(self.doc.get("entitlements") or {})

    @property
    def expires_at(self) -> datetime | None:
        return _parse_ts(self.doc.get("expires_at"))

    @property
    def signature(self) -> str | None:
        sig = self.doc.get(SIGNATURE_FIELD)
        return str(sig) if sig is not None else None

    def is_expired(self, now: datetime | None = None) -> bool:
        exp = self.expires_at
        if exp is None:
            return False  # perpetual (community default)
        now = now or datetime.now(timezone.utc)
        return now >= exp

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.doc)

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> "License":
        if not isinstance(doc, dict):
            raise MalformedLicense("license must be a JSON object")
        missing = [k for k in _REQUIRED if k not in doc]
        if missing:
            raise MalformedLicense(f"license missing required fields: {', '.join(missing)}")
        if doc.get("schema_version") != SCHEMA_VERSION:
            raise MalformedLicense(
                f"unsupported schema_version: {doc.get('schema_version')!r}"
            )
        if not isinstance(doc.get("entitlements"), dict):
            raise MalformedLicense("entitlements must be an object")
        return cls(doc=doc)

    @classmethod
    def from_json(cls, text: str) -> "License":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedLicense(f"invalid JSON: {exc}") from exc
        return cls.from_dict(doc)
