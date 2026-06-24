"""Open-core Community Edition licensing foundation (Phase 0).

PUBLIC surface (shipped in the shell, used by the gateway guard / license manager):
    License, canonical_bytes      -- schema
    KeyRegistry                   -- public-key registry (rotation by key_id)
    verify_license                -- offline signature + expiry verification
    error types                   -- LicenseError and subclasses

PRIVATE: ``licensing.issuer`` holds the signing path and must NOT be exported to
the public repo (Phase 5 allowlist excludes it). The private key never ships.
"""
from __future__ import annotations

from .errors import (
    InvalidSignature,
    LicenseError,
    LicenseExpired,
    MalformedLicense,
    UnknownKeyId,
    UnsupportedAlgorithm,
)
from .keys import KeyRegistry
from .manager import (
    Decision,
    LicenseManager,
    UnactivatedManager,
    load_manager,
)
from .schema import License, canonical_bytes
from .verify import verify_license

__all__ = [
    "License",
    "canonical_bytes",
    "KeyRegistry",
    "verify_license",
    "LicenseManager",
    "Decision",
    "UnactivatedManager",
    "load_manager",
    "LicenseError",
    "MalformedLicense",
    "InvalidSignature",
    "UnknownKeyId",
    "UnsupportedAlgorithm",
    "LicenseExpired",
]
