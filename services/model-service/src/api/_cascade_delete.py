"""Backwards-compatible re-export of the canonical cascade-delete path.

The implementation moved to ``shared/model_snapshot/cascade_delete.py``
(F-020-06) so the project import rehydrator can reuse the same hardened
bottom-up delete. Existing model-service imports keep working via this shim.
"""
from __future__ import annotations

from shared.model_snapshot.cascade_delete import (  # noqa: F401
    delete_model_cascade,
    delete_project_cascade,
)

__all__ = ["delete_model_cascade", "delete_project_cascade"]
