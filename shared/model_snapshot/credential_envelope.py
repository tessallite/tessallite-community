"""Passphrase-based credential envelope for project export bundles.

Derives a Fernet key from a user-supplied passphrase via PBKDF2HMAC,
re-encrypts credentials from the system key to the derived key (export)
and back (import).
"""
from __future__ import annotations

import base64
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

KDF_ITERATIONS = 480_000
KDF_VERSION = 1

# F-020-14: a bundle is untrusted input (routinely shared as a file). The KDF
# parameters carried in its envelope must be clamped so a crafted bundle cannot
# (a) pin a worker CPU for minutes with iterations=2_000_000_000, or
# (b) silently weaken the derivation with iterations=1.
KDF_ITERATIONS_MIN = 100_000
KDF_ITERATIONS_MAX = 1_000_000


class EnvelopeError(ValueError):
    """Raised when a credential envelope carries unsupported/invalid params."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def build_envelope(passphrase: str) -> tuple[Fernet, dict[str, Any]]:
    """Return (fernet_instance, envelope_dict) for encrypting credentials."""
    salt = os.urandom(16)
    key = _derive_key(passphrase, salt)
    envelope = {
        "method": "passphrase-fernet",
        "kdf": {
            "algorithm": "PBKDF2HMAC",
            "hash": "SHA256",
            "iterations": KDF_ITERATIONS,
            "version": KDF_VERSION,
        },
        "salt": base64.b64encode(salt).decode("ascii"),
    }
    return Fernet(key), envelope


def fernet_from_envelope(passphrase: str, envelope: dict[str, Any]) -> Fernet:
    """Reconstruct the Fernet instance from a stored envelope + passphrase.

    Validates the envelope's KDF parameters against the known algorithm and a
    sane iteration range (F-020-14) before deriving — the bundle is untrusted.
    """
    salt = base64.b64decode(envelope["salt"])
    kdf_params = envelope.get("kdf", {})

    # Only the known PBKDF2HMAC-SHA256 method is supported. Reject anything
    # else loudly rather than silently deriving with the wrong scheme.
    algorithm = kdf_params.get("algorithm", "PBKDF2HMAC")
    hash_name = kdf_params.get("hash", "SHA256")
    if algorithm != "PBKDF2HMAC" or hash_name != "SHA256":
        raise EnvelopeError(
            f"Unsupported KDF in envelope: {algorithm}/{hash_name} "
            f"(only PBKDF2HMAC/SHA256 is accepted)"
        )

    iterations = kdf_params.get("iterations", KDF_ITERATIONS)
    if not isinstance(iterations, int) or not (
        KDF_ITERATIONS_MIN <= iterations <= KDF_ITERATIONS_MAX
    ):
        raise EnvelopeError(
            f"KDF iteration count {iterations!r} is outside the accepted "
            f"range [{KDF_ITERATIONS_MIN}, {KDF_ITERATIONS_MAX}]"
        )

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    derived = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return Fernet(derived)


def re_encrypt(
    blob: bytes, *, source_fernet: Fernet, target_fernet: Fernet
) -> bytes:
    """Decrypt with source key, re-encrypt with target key."""
    plaintext = source_fernet.decrypt(blob)
    return target_fernet.encrypt(plaintext)


def re_encrypt_b64(
    b64_blob: str, *, source_fernet: Fernet, target_fernet: Fernet
) -> str:
    """Base64 variant: decode -> decrypt -> re-encrypt -> encode."""
    raw = base64.b64decode(b64_blob)
    plaintext = source_fernet.decrypt(raw)
    return base64.b64encode(target_fernet.encrypt(plaintext)).decode("ascii")
