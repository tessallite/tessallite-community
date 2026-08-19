"""Per-model definition/governance advisory lock — re-export shim.

The implementation moved to ``shared.db.model_lock`` (Bug-7982 R6 reviewer
finding 6) so the scheduler's model-scoped writers (``services/scheduler/src/api/
sla.py``) acquire the SAME lock on the SAME key as the model-service's writers and
version-control operations. This module re-exports it so the existing
``from src.api._model_lock import acquire_model_definition_lock`` import sites keep
working unchanged. See ``shared/db/model_lock.py`` for the full contract.
"""
from __future__ import annotations

from shared.db.model_lock import (  # noqa: F401
    _LOCK_KEY_MASK,
    _LOCK_NOT_AVAILABLE_SQLSTATE,
    acquire_model_definition_lock,
    model_advisory_lock_key,
)

__all__ = ["acquire_model_definition_lock", "model_advisory_lock_key"]
