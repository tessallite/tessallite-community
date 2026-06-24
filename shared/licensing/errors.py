"""Licensing errors.

Part of the open-core Community Edition licensing foundation (Phase 0). These
types are part of the PUBLIC shell — the gateway guard / license manager verify
licenses with this package; only the issuer (tessallite.io) holds the signing key.
"""
from __future__ import annotations


class LicenseError(Exception):
    """Base class for all license problems."""


class MalformedLicense(LicenseError):
    """The license document is missing required fields or is structurally invalid."""


class InvalidSignature(LicenseError):
    """The signature does not match the canonical payload / public key."""


class UnknownKeyId(LicenseError):
    """The license references a ``key_id`` not present in the verifier's key registry."""


class UnsupportedAlgorithm(LicenseError):
    """The signature uses an algorithm this build does not support."""


class LicenseExpired(LicenseError):
    """The license has an ``expires_at`` in the past."""
