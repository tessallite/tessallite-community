"""Licensing errors.

Part of the open-core Community Edition licensing foundation (Phase 0). These
types are part of the PUBLIC shell — the gateway guard / license manager verify
licenses with this package; only the issuer (tessallite.io) holds the signing key.
"""
from __future__ import annotations

# Bug-8164: error taxonomy. Every license error carries a machine-readable
# ``error_code`` so a consumer (gateway guard, license manager, audit UI, a
# stored failure record) can branch on a stable token instead of parsing prose.
# The token is a class attribute so it is available on the type AND on any
# instance, and it stays constant across message wording changes. The field name
# matches the existing codebase convention (``NamedSetSnapshotInvalidError``,
# ``KpiSnapshotInvalidError`` also expose ``error_code``). Keep these tokens
# stable — they are the contract consumers key off.


class LicenseError(Exception):
    """Base class for all license problems."""

    error_code: str = "license_error"


class MalformedLicense(LicenseError):
    """The license document is missing required fields or is structurally invalid."""

    error_code = "malformed_license"


class InvalidSignature(LicenseError):
    """The signature does not match the canonical payload / public key."""

    error_code = "invalid_signature"


class UnknownKeyId(LicenseError):
    """The license references a ``key_id`` not present in the verifier's key registry."""

    error_code = "unknown_key_id"


class UnsupportedAlgorithm(LicenseError):
    """The signature uses an algorithm this build does not support."""

    error_code = "unsupported_algorithm"


class LicenseExpired(LicenseError):
    """The license has an ``expires_at`` in the past."""

    error_code = "license_expired"
