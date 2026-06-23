"""Public-key registry for license verification (PUBLIC, shipped in the shell).

Maps ``key_id`` -> public key so the verifier supports key rotation: a license
carries the ``key_id`` it was signed with, and the registry holds current + prior
public keys. Only PUBLIC keys live here; the private signing key never ships
(it stays in the issuer / Cloud KMS, see spec §14, D18).

Phase 0 supports Ed25519. ECDSA P-256 (KMS-held) can be added as a second
algorithm without touching callers — verification dispatches on the signature's
algorithm prefix.
"""
from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ALG_ED25519 = "ed25519"


@dataclass(frozen=True)
class PublicKeyEntry:
    key_id: str
    algorithm: str
    public_key: object  # algorithm-specific public-key object


class KeyRegistry:
    """In-memory registry of verification keys, keyed by ``key_id``."""

    def __init__(self) -> None:
        self._keys: dict[str, PublicKeyEntry] = {}

    def add_ed25519(self, key_id: str, public_key_raw: bytes) -> None:
        self._keys[key_id] = PublicKeyEntry(
            key_id=key_id,
            algorithm=ALG_ED25519,
            public_key=Ed25519PublicKey.from_public_bytes(public_key_raw),
        )

    def get(self, key_id: str) -> PublicKeyEntry | None:
        return self._keys.get(key_id)

    def __contains__(self, key_id: str) -> bool:
        return key_id in self._keys

    def __len__(self) -> int:
        return len(self._keys)
