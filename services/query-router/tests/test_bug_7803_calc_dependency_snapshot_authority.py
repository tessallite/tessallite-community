"""Bug-7803 — calc-measure DEPENDENCY base measures must resolve from the
DEPLOYED SNAPSHOT (fail-closed), not the live draft ORM.

A calculated measure's expression may reference base measures that are NOT
themselves selected. The raw route (``rewrite/raw_sql.py``) and the non-raw
source route (``rewrite/source_sql.py`` Phase 4A) both load those referenced
base measures so their source columns / UDAs drive join planning and physical
rendering. Historically both read LIVE draft ``Measure`` rows by
``(model_id, name)`` — splitting the authority from the binder (which pins the
SELECTED calc measure to the immutable deployed snapshot). A post-deploy DRAFT
edit to a referenced base measure could then change the calc's emitted SQL
before redeployment (wrong numbers under an active draft edit).

Both routes now call one shared helper,
``snapshot_resolver.resolve_calc_dependency_measures``, which enforces the
Bug-7784 fail-closed authority. These tests pin that helper's contract:

* deployed model -> resolve from the deployed snapshot (never live ORM);
* deployed model whose snapshot cannot resolve -> fail closed (unresolved,
  NEVER a live-draft fallback);
* undeployed model -> live ORM is the authority.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_7803_calc_dependency_snapshot_authority.py -v
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from src.semantic.snapshot_resolver import resolve_calc_dependency_measures

pytestmark = pytest.mark.integration


def _measure(name: str, source_column_id: str):
    return types.SimpleNamespace(
        name=name,
        source_column_id=source_column_id,
        user_defined_attribute_id=None,
    )


def _model(deployed: bool):
    return types.SimpleNamespace(
        id="model-1",
        deployed_version_id="v1" if deployed else None,
    )


def _db_returning(measures):
    """AsyncSession stub whose execute() yields the given measure rows."""
    db = AsyncMock()
    result = types.SimpleNamespace()
    result.scalars = lambda: types.SimpleNamespace(all=lambda: list(measures))
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_deployed_model_resolves_from_snapshot_not_live_orm():
    """A deployed model resolves dependency base measures from the deployed
    snapshot. The snapshot's ``price`` points at column ``col_snap``; the live
    ORM (which points ``price`` at a DIFFERENT column) must never be read."""
    snapshot_price = _measure("price", "col_snap")
    shape = types.SimpleNamespace(measures=[snapshot_price])

    # Live ORM would return the DRAFT column — proves it is not used.
    db = _db_returning([_measure("price", "col_draft")])
    model = _model(deployed=True)

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        AsyncMock(return_value=shape),
    ):
        out = await resolve_calc_dependency_measures(model, db, {"price"})

    assert out["price"].source_column_id == "col_snap"
    db.execute.assert_not_called()  # live ORM never touched for a deployed model


@pytest.mark.asyncio
async def test_deployed_model_snapshot_none_fails_closed_no_live_fallback():
    """A deployed model whose snapshot cannot resolve (None) leaves the
    dependency UNRESOLVED — it must NOT fall back to the live draft ORM."""
    db = _db_returning([_measure("price", "col_draft")])
    model = _model(deployed=True)

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        AsyncMock(return_value=None),
    ):
        out = await resolve_calc_dependency_measures(model, db, {"price"})

    assert out == {}  # fail closed
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_deployed_model_snapshot_exception_fails_closed():
    """A transient snapshot-resolution failure fails closed, never live-draft."""
    db = _db_returning([_measure("price", "col_draft")])
    model = _model(deployed=True)

    async def _boom(*a, **k):
        raise RuntimeError("transient snapshot failure")

    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        side_effect=_boom,
    ):
        out = await resolve_calc_dependency_measures(model, db, {"price"})

    assert out == {}
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_undeployed_model_uses_live_orm():
    """An UNDEPLOYED model has no deploy pointer, so its live tables ARE the
    authority — the live ORM is the correct source."""
    db = _db_returning([_measure("price", "col_live")])
    model = _model(deployed=False)

    # resolve_deployed_shape must not even be consulted for an undeployed model.
    with patch(
        "src.semantic.snapshot_resolver.resolve_deployed_shape",
        AsyncMock(side_effect=AssertionError("should not resolve shape")),
    ):
        out = await resolve_calc_dependency_measures(model, db, {"price"})

    assert out["price"].source_column_id == "col_live"
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_ref_names_short_circuits():
    """No referenced names -> empty result, no DB / snapshot work."""
    db = _db_returning([])
    model = _model(deployed=True)
    out = await resolve_calc_dependency_measures(model, db, set())
    assert out == {}
    db.execute.assert_not_called()
