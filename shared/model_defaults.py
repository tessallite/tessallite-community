"""Defaults shared by new-model producers and missing-field readers."""
from __future__ import annotations

# New models include all eligible measures. Explicit persisted choices remain
# authoritative; changing this default does not rewrite existing models.
DEFAULT_INCLUDE_ALL_MEASURES = True
