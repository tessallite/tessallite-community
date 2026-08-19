"""
B5: the versioning backfill script must gate on ``status`` only.

Before the fix, a model with ``aggregations_enabled=False`` but
``status='active'`` would be left undeployed by the one-shot migration —
even though that flag pre-0021 only paused aggregate refresh, not query
routing. Users who never touched the deploy UI suddenly found their
models invisible to BI tools after running the migration.

After the fix: any model whose status is not 'disabled' gets a v1 snapshot
that is immediately deployed, regardless of ``aggregations_enabled``.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from scripts.migrate_existing_models_to_versioned import backfill_tenant

pytestmark = pytest.mark.unit


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


def _model(*, status: str, aggregations_enabled: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        aggregations_enabled=aggregations_enabled,
        deployed_version_id=None,
        last_deployed_at=None,
    )


def _mock_db_for(models):
    """Return a db whose execute() returns the models list on the first call
    and an empty ModelVersion query on every subsequent call.

    db.add captures the ModelVersion and db.flush stamps a fresh UUID onto
    it — mimicking SQLAlchemy's default=uuid.uuid4 behaviour, which the real
    backfill relies on when it later assigns ``model.deployed_version_id =
    v.id``."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _ScalarResult(models),
        *[_ScalarResult([]) for _ in models],
    ])
    pending: list = []

    def _add(v):
        pending.append(v)

    async def _flush():
        for v in pending:
            if getattr(v, "id", None) is None:
                v.id = uuid.uuid4()

    db.add = _add
    db.flush = _flush
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_active_model_with_aggregations_disabled_gets_deployed():
    """B5's core case: the ``aggregations_enabled`` flag must NOT gate deploy."""
    model = _model(status="active", aggregations_enabled=False)
    db = _mock_db_for([model])

    with patch(
        "scripts.migrate_existing_models_to_versioned.snapshot_model",
        new=AsyncMock(return_value={"schema_version": 1, "model": {}}),
    ):
        seen, created = await backfill_tenant("t1", db)

    assert seen == 1
    assert created == 1
    assert model.deployed_version_id is not None, (
        "backfill must deploy active models regardless of aggregations_enabled"
    )
    assert model.last_deployed_at is not None


@pytest.mark.asyncio
async def test_active_model_with_aggregations_enabled_gets_deployed():
    """Regression guard for the common case — still deployed."""
    model = _model(status="active", aggregations_enabled=True)
    db = _mock_db_for([model])

    with patch(
        "scripts.migrate_existing_models_to_versioned.snapshot_model",
        new=AsyncMock(return_value={"schema_version": 1, "model": {}}),
    ):
        await backfill_tenant("t1", db)

    assert model.deployed_version_id is not None


@pytest.mark.asyncio
async def test_disabled_model_is_not_deployed():
    """status='disabled' still means deliberately off — don't auto-deploy."""
    model = _model(status="disabled", aggregations_enabled=True)
    db = _mock_db_for([model])

    with patch(
        "scripts.migrate_existing_models_to_versioned.snapshot_model",
        new=AsyncMock(return_value={"schema_version": 1, "model": {}}),
    ):
        seen, created = await backfill_tenant("t1", db)

    assert seen == 1
    assert created == 1  # snapshot is still created
    assert model.deployed_version_id is None
    assert model.last_deployed_at is None


@pytest.mark.asyncio
async def test_model_with_existing_versions_is_skipped():
    """Idempotency: a model that already has a ModelVersion row is left alone."""
    model = _model(status="active", aggregations_enabled=True)
    existing_version_id = uuid.uuid4()

    db = AsyncMock()
    # models query → models; existing-version probe → returns an id
    db.execute = AsyncMock(side_effect=[
        _ScalarResult([model]),
        _ScalarResult([existing_version_id]),
    ])
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = lambda _v: None

    with patch(
        "scripts.migrate_existing_models_to_versioned.snapshot_model",
        new=AsyncMock(return_value={"schema_version": 1, "model": {}}),
    ) as snap:
        seen, created = await backfill_tenant("t1", db)

    assert seen == 1
    assert created == 0
    snap.assert_not_awaited()
    assert model.deployed_version_id is None  # untouched
