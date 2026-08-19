"""F-013-03 — deploy/revert stales incompatible Named Query artifacts.

``_stale_incompatible_artifacts`` staled aggregates and pockets but NOT Named
Query artifacts, so after a Deploy that moved the definition a fresh NQ artifact
kept advertising "fresh" while the serve-time built-for gate silently refused it
— acceleration missed until the next cron tick. The staling must cover NQ
artifacts in the SAME transaction as the pointer/epoch move.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from src.api.versions import _stale_incompatible_artifacts


def _updated_tables(execute_mock):
    tables = []
    for call in execute_mock.await_args_list:
        stmt = call.args[0]
        table = getattr(getattr(stmt, "table", None), "name", None)
        if table:
            tables.append(table)
    return tables


@pytest.mark.asyncio
async def test_deploy_stales_named_query_artifacts_in_same_transaction():
    tenant_db = AsyncMock()
    await _stale_incompatible_artifacts(
        tenant_db, uuid.uuid4(), uuid.uuid4(), new_epoch=5
    )
    tables = _updated_tables(tenant_db.execute)
    # All three materialised families are staled in the one call sequence.
    assert "aggregate_definitions" in tables
    assert "pocket_definitions" in tables
    assert "named_query_artifacts" in tables, (
        "Named Query artifacts are not staled on deploy/revert (F-013-03)"
    )
