"""Build the license manager from runtime settings (PUBLIC glue).

One place to turn ``LICENSE_FILE`` + ``LICENSE_PUBLIC_KEYS`` settings into a manager so
services don't each reimplement the parsing. The closed manager (when present) receives
the parsed license doc + public-key registry; in source-only mode it falls back to the
open stub.
"""
from __future__ import annotations

import base64
import json

from .keys import KeyRegistry
from .manager import LicenseManager, load_manager


def build_registry(spec: str) -> KeyRegistry:
    """Parse ``key_id:base64,key_id:base64`` into a public-key registry.

    F-031-24: a present-but-unparseable fragment fails closed (no silent skip).
    Duplicate key_ids keep the first entry so the vendor key cannot be replaced
    by a later fragment in the same spec.
    """
    reg = KeyRegistry()
    for part in (p.strip() for p in (spec or "").split(",") if p.strip()):
        if ":" not in part:
            raise ValueError(f"unparseable public-key spec fragment: {part!r}")
        key_id, b64 = part.split(":", 1)
        key_id = key_id.strip()
        if key_id in reg:
            continue
        try:
            raw = base64.b64decode(b64.strip())
            reg.add_ed25519(key_id, raw)
        except Exception as exc:
            raise ValueError(f"unparseable public-key spec fragment: {part!r}") from exc
    return reg


def load_license_doc(path: str) -> dict | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def manager_from(license_file: str, public_keys: str) -> LicenseManager:
    """Build the license manager from explicit license-file path + public-key spec."""
    return load_manager(
        license_doc=load_license_doc(license_file),
        registry=build_registry(public_keys),
    )


def manager_from_settings() -> LicenseManager:
    from shared.config.settings import get_settings

    s = get_settings()
    return manager_from(s.LICENSE_FILE, s.LICENSE_PUBLIC_KEYS)
