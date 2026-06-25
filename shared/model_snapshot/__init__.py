"""Model snapshot — serialise / rehydrate / round-trip the per-model state.

Used by:
  - Phase 4 versioning (Save creates a snapshot row, Revert rehydrates one)
  - Phase 5 deploy resolver (the deployed snapshot is the runtime view)
  - Phase 6 export / import (the same JSON shape)

Schema version 1. Bump the ``SNAPSHOT_SCHEMA_VERSION`` constant when the
contract changes; rehydrator must accept older versions for read.
"""
from shared.model_snapshot.credential_envelope import (
    build_envelope,
    fernet_from_envelope,
    re_encrypt,
)
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.model_snapshot.project_rehydrator import ProjectImportError, import_project
from shared.model_snapshot.project_serialiser import export_project
from shared.model_snapshot.rehydrator import (
    SnapshotSchemaError,
    SnapshotVersionError,
    rehydrate_into_live,
)
from shared.model_snapshot.serialiser import SNAPSHOT_SCHEMA_VERSION, snapshot_model

__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotSchemaError",
    "SnapshotVersionError",
    "snapshot_model",
    "rehydrate_into_live",
    "prepare_snapshot_for_import",
    "build_envelope",
    "fernet_from_envelope",
    "re_encrypt",
    "export_project",
    "import_project",
    "ProjectImportError",
]
