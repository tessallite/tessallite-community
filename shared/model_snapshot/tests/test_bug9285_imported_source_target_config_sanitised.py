"""Bug-9285 — an imported bundle cannot reintroduce plaintext secrets.

``data_sources.config`` and ``data_targets.config`` are plaintext JSONB columns
that are echoed back to lower-privileged readers; a real secret belongs in the
Fernet-encrypted column beside them. The API write paths enforce that through
``DataSourceCreate`` / ``DataTargetCreate``, but import bypasses those schemas
entirely and the rehydrator wrote the bundle's bag verbatim. A hand-crafted or
legacy project bundle was therefore a way to put a password back into JSONB,
where it persists at rest and duplicates into every deployed snapshot.

These tests assert the value that actually reaches the column — the bound
parameters of the statement the rehydrator executes — for both the plain-insert
(import) and the upsert (revert) path.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import DataSource, DataTarget
from shared.model_snapshot.rehydrator import _insert_data_sources_and_targets

pytestmark = pytest.mark.unit


_MODEL_ID = uuid.uuid4()
_SECRET = "pl41nt3xt-s3cr3t"


def _snapshot() -> dict[str, Any]:
    """One source and one target, each carrying a plaintext secret and a
    nested one — a legacy bundle exported before the write gate existed."""
    return {
        "data_sources": [
            {
                "id": str(uuid.uuid4()),
                "model_id": str(_MODEL_ID),
                "project_connection_id": str(uuid.uuid4()),
                "source_type": "postgresql",
                "display_name": "pg-source",
                "default_schema": "public",
                "config": {
                    "host": "db.internal",
                    "password": _SECRET,
                    "nested": {"api_key": _SECRET, "region": "eu"},
                },
            }
        ],
        "data_targets": [
            {
                "id": str(uuid.uuid4()),
                "model_id": str(_MODEL_ID),
                "project_connection_id": str(uuid.uuid4()),
                "target_type": "postgresql",
                "display_name": "pg-target",
                "config": {
                    "dataset": "aggregates",
                    "aws_secret_access_key": _SECRET,
                    "project_id": "legacy-project",
                },
            }
        ],
    }


def _capturing_db() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    return db


def _written_configs(db: AsyncMock, model_cls: type) -> list[dict]:
    """Every ``config`` value the executed statements would persist.

    Reads the compiled statement's bound parameters — the values PostgreSQL
    receives — rather than the dict the caller happened to build, so a fix that
    sanitised a copy and inserted the original would still fail.
    """
    configs: list[dict] = []
    for call in db.execute.call_args_list:
        stmt = call.args[0]
        table = getattr(stmt, "table", None)
        if table is None or table.name != model_cls.__tablename__:
            continue
        params = stmt.compile().params
        for key, value in params.items():
            if key == "config" or key.startswith("config"):
                if isinstance(value, dict):
                    configs.append(value)
    return configs


def _flatten(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [item for v in value.values() for item in _flatten(v)]
    if isinstance(value, list):
        return [item for v in value for item in _flatten(v)]
    return [value]


@pytest.mark.asyncio
@pytest.mark.parametrize("upsert", [False, True], ids=["import", "revert-upsert"])
@pytest.mark.parametrize(
    "model_cls", [DataSource, DataTarget], ids=["data_sources", "data_targets"]
)
async def test_imported_config_never_persists_a_plaintext_secret(model_cls, upsert):
    db = _capturing_db()

    await _insert_data_sources_and_targets(
        _MODEL_ID, _snapshot(), db, None, upsert=upsert
    )

    configs = _written_configs(db, model_cls)
    assert configs, (
        f"no {model_cls.__tablename__} config value was captured — this test's "
        "own statement inspection has gone blind, not the code it verifies"
    )
    for config in configs:
        assert _SECRET not in _flatten(config), (
            f"import persisted a plaintext secret into "
            f"{model_cls.__tablename__}.config: {config}"
        )
        assert "password" not in config
        assert "aws_secret_access_key" not in config


@pytest.mark.asyncio
async def test_non_secret_configuration_survives_the_import():
    """Sanitising must not become a data-loss path: only secret-like keys go."""
    db = _capturing_db()

    await _insert_data_sources_and_targets(_MODEL_ID, _snapshot(), db, None)

    source_config = _written_configs(db, DataSource)[0]
    assert source_config["host"] == "db.internal"
    assert source_config["nested"] == {"region": "eu"}


@pytest.mark.asyncio
async def test_the_deprecated_target_project_id_is_still_dropped():
    """Bug-8790's strip runs on the sanitised bag, not instead of it."""
    db = _capturing_db()

    await _insert_data_sources_and_targets(_MODEL_ID, _snapshot(), db, None)

    target_config = _written_configs(db, DataTarget)[0]
    assert "project_id" not in target_config
    assert target_config["dataset"] == "aggregates"
