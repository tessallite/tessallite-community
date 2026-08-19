"""Platform-aware Tree-sitter grammar artifact resolution."""
from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GrammarArtifact:
    """Resolved paths for one Tree-sitter grammar."""

    grammar_dir: Path
    library_path: Path
    legacy_library_path: Path


_SYSTEM_SUFFIX = {
    "Windows": ".dll",
    "Linux": ".so",
    "Darwin": ".dylib",
}


def resolve_grammar_artifact(
    *,
    grammar_name: str,
    base_dir: Path | str,
    system: str | None = None,
    machine: str | None = None,
) -> GrammarArtifact:
    """Return the native artifact path for the current platform.

    Generated host-local artifacts live under ``grammars/.cache`` and include
    OS plus CPU architecture in the filename. The existing committed Linux
    artifacts are accepted only when the detected platform is Linux, so Windows
    tests never try to load a Linux ``.so``.
    """

    root = Path(base_dir)
    current_system = system or platform.system()
    current_machine = machine or platform.machine()
    suffix = _SYSTEM_SUFFIX.get(current_system)
    if suffix is None:
        suffix = ".dll" if os.name == "nt" else ".so"

    normalized_system = _slug(current_system or "unknown")
    normalized_machine = _slug(current_machine or "unknown")
    cache_path = root / ".cache" / (
        f"{grammar_name}-{normalized_system}-{normalized_machine}{suffix}"
    )
    legacy_path = root / f"{grammar_name}.so"
    library_path = legacy_path if current_system == "Linux" and legacy_path.exists() else cache_path

    return GrammarArtifact(
        grammar_dir=root / f"tree-sitter-{grammar_name}",
        library_path=library_path,
        legacy_library_path=legacy_path,
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"
