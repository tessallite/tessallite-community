"""Fail-closed FastAPI version check shared by every pytest collection root."""

from __future__ import annotations

import re
from pathlib import Path


_LOCK_PATH = Path(__file__).resolve().parents[1] / "uv.lock"


def locked_fastapi_version(lock_path: Path = _LOCK_PATH) -> str:
    """Return the FastAPI version in the shared lock or fail closed."""
    if not lock_path.is_file():
        raise RuntimeError(f"Bug-8467: FastAPI lock is missing at {lock_path}")
    text = lock_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r'name\s*=\s*"fastapi".*?version\s*=\s*"([^"]+)"',
        text,
        re.DOTALL,
    )
    if match is None:
        raise RuntimeError(f"Bug-8467: FastAPI entry is missing from {lock_path}")
    return match.group(1)


def check_fastapi_version_drift(
    *,
    ambient_version: str | None = None,
    lock_path: Path = _LOCK_PATH,
) -> None:
    """Reject an interpreter whose FastAPI differs from the shipped lock."""
    if ambient_version is None:
        try:
            import fastapi
        except ImportError:
            return
        ambient_version = fastapi.__version__
    locked = locked_fastapi_version(lock_path)
    if ambient_version != locked:
        raise RuntimeError(
            f"Bug-8467: refusing to collect tests with ambient FastAPI "
            f"{ambient_version}; the checked-in service lock requires {locked}. "
            "Install from the service lock or use the CI/local parity gate."
        )
