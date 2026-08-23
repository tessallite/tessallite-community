"""Tests for model-versioning remediation bugs (7146, 7150, 7151, 7140).

Bug-7151: deploy fails closed on empty/malformed/missing snapshots.
Bug-7150: save uses advisory lock to prevent mixed-time snapshots.
Bug-7146: revert stale-marks preserved aggregates.
Bug-7140: deploy/undeploy/revert bump deploy_epoch for cache invalidation.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import ModelVersion

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _valid_snapshot() -> dict:
    """Minimal valid snapshot with semantic content."""
    return {
        "schema_version": 3,
        "model": {"id": str(TEST_MODEL_ID)},
        "tables": [{"id": str(uuid.uuid4()), "model_id": str(TEST_MODEL_ID)}],
        "columns": [{"id": str(uuid.uuid4())}],
        "measures": [{"id": str(uuid.uuid4()), "name": "revenue"}],
        "dimensions": [{"id": str(uuid.uuid4()), "name": "country"}],
        "hierarchies": [],
    }


def _version(version_id: uuid.UUID, number: int, snapshot=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        version_number=number,
        summary=None,
        snapshot_json=snapshot if snapshot is not None else _valid_snapshot(),
        created_at=NOW,
        created_by="user@example.com",
    )


async def _rehydrate_noop(*_args, **_kwargs):
    return None


# -----------------------------------------------------------------------
# Bug-7151: deploy fails closed on empty/malformed/missing snapshots
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deploy_rejects_empty_snapshot(client):
    """Bug-7151: deploying a version with an empty snapshot (no measures,
    no dimensions, no columns) must return 409, not silently fall back to
    mutable live metadata."""
    v_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()

    empty_snap = {"schema_version": 3, "model": {}, "measures": [], "dimensions": [], "columns": []}
    ver = _version(v_id, 1, snapshot=empty_snap)

    mock_db.get = AsyncMock(side_effect=[model, ver])
    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(
            f"{PREFIX}/deploy",
            json={"version_id": str(v_id)},
        )

    assert resp.status_code == 409
    assert "empty snapshot" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_deploy_rejects_malformed_snapshot(client):
    """Bug-7151: deploying a version with a non-dict snapshot must return 409."""
    v_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()

    ver = _version(v_id, 1, snapshot="not-a-dict")
    mock_db.get = AsyncMock(side_effect=[model, ver])

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(
            f"{PREFIX}/deploy",
            json={"version_id": str(v_id)},
        )

    assert resp.status_code == 409
    assert "malformed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_deploy_rejects_snapshot_without_schema_version(client):
    """Bug-7151: deploying a version with no schema_version must return 409."""
    v_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()

    no_sv_snap = {"model": {}, "measures": [{"id": str(uuid.uuid4())}]}
    ver = _version(v_id, 1, snapshot=no_sv_snap)
    mock_db.get = AsyncMock(side_effect=[model, ver])

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(
            f"{PREFIX}/deploy",
            json={"version_id": str(v_id)},
        )

    assert resp.status_code == 409
    assert "schema_version" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_deploy_accepts_valid_snapshot(client):
    """A version with valid semantic content should deploy successfully."""
    v_id = uuid.uuid4()
    model = make_model()
    mock_db = make_mock_db()

    ver = _version(v_id, 1, snapshot=_valid_snapshot())
    mock_db.get = AsyncMock(side_effect=[model, ver])

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(
            f"{PREFIX}/deploy",
            json={"version_id": str(v_id)},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert model.deployed_version_id == v_id


@pytest.mark.asyncio
async def test_deploy_latest_rejects_empty_snapshot(client):
    """Bug-7151: deploying latest with an empty snapshot must return 409."""
    model = make_model()
    mock_db = make_mock_db()

    empty_snap = {"schema_version": 3, "model": {}, "measures": [], "dimensions": [], "columns": []}
    ver = _version(uuid.uuid4(), 1, snapshot=empty_snap)

    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = ver
    mock_db.execute = AsyncMock(return_value=execute_result)
    mock_db.get = AsyncMock(return_value=model)

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(
            f"{PREFIX}/deploy",
            json={},
        )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_deploy_latest_skips_snapshot_unavailable_placeholder(client):
    """Bug-6295: deploy-latest must NOT resolve an imported placeholder version
    (snapshot_unavailable=True, snapshot_json={}). The version-selection query
    must carry a snapshot_unavailable guard so the highest AUTHENTIC version is
    chosen. Capture the compiled SELECT and assert the guard is present."""
    model = make_model()
    mock_db = make_mock_db()

    captured: list[str] = []
    authentic = _version(uuid.uuid4(), 8, snapshot=_valid_snapshot())

    async def _execute(stmt, *args, **kwargs):
        try:
            captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        except Exception:
            captured.append(str(stmt))
        result = MagicMock()
        # Simulate the guarded query returning the authentic version only.
        result.scalar_one_or_none.return_value = authentic
        return result

    mock_db.execute = _execute
    mock_db.get = AsyncMock(return_value=model)

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(f"{PREFIX}/deploy", json={})

    assert resp.status_code == 200, f"deploy failed: {resp.json()}"
    # The version-selection query must exclude placeholder rows.
    select_sql = [c for c in captured if "model_versions" in c and "snapshot_unavailable" in c]
    assert select_sql, (
        "deploy-latest version query must guard on snapshot_unavailable so an "
        "imported placeholder is never deployed"
    )


@pytest.mark.asyncio
async def test_deploy_latest_no_authentic_version_fails_closed(client):
    """Bug-6295: a model with ONLY imported placeholder history (no authentic
    version) must fail deploy-latest with a clear 400, not deploy a {} snapshot."""
    model = make_model()
    mock_db = make_mock_db()

    execute_result = MagicMock()
    # The guarded query finds no authentic version.
    execute_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=execute_result)
    mock_db.get = AsyncMock(return_value=model)

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(f"{PREFIX}/deploy", json={})

    assert resp.status_code == 400
    assert "deployable" in resp.json()["detail"].lower()


# -----------------------------------------------------------------------
# Bug-7150: save uses advisory lock for point-in-time consistency
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_acquires_advisory_lock(client):
    """Bug-7150: the Save endpoint must acquire a PostgreSQL advisory lock
    before snapshotting to prevent concurrent edits from producing a
    mixed-time snapshot."""
    model = make_model()
    mock_db = make_mock_db()

    mock_db.get = AsyncMock(return_value=model)

    # Track all execute calls to verify the advisory lock.
    execute_calls = []
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    default_result.scalars.return_value.all.return_value = []

    async def _tracking_execute(*args, **kwargs):
        execute_calls.append(args)
        return default_result

    mock_db.execute = AsyncMock(side_effect=_tracking_execute)

    # The Save endpoint calls db.refresh(version) after commit to read
    # back the DB-generated id/created_at. We need the refresh to set
    # these attributes on the ModelVersion object that was added.
    added_objects = []
    original_add = mock_db.add

    def _tracking_add(obj):
        added_objects.append(obj)
        return original_add(obj)

    mock_db.add = MagicMock(side_effect=_tracking_add)

    async def _mock_refresh(obj, *args, **kwargs):
        # Simulate DB refresh: populate fields that have defaults. opus5 finding 5
        # also refreshes model.deployed_version_id under the lock (attr list arg).
        if hasattr(obj, 'version_number'):
            if obj.id is None:
                obj.id = uuid.uuid4()
            if obj.created_at is None:
                obj.created_at = NOW

    mock_db.refresh = AsyncMock(side_effect=_mock_refresh)

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions._consistent_snapshot", new_callable=AsyncMock, return_value=_valid_snapshot()),
    ):
        resp = await client.post(
            f"{PREFIX}/versions",
            json={"summary": "test"},
        )

    assert resp.status_code == 200, f"Save failed: {resp.json()}"

    # The endpoint should have called execute with the advisory lock SQL
    lock_calls = [
        c for c in execute_calls
        if len(c) > 0 and hasattr(c[0], 'text')
        and 'pg_advisory_xact_lock' in str(getattr(c[0], 'text', ''))
    ]
    assert len(lock_calls) == 1, (
        f"Expected exactly one advisory lock call, got {len(lock_calls)}. "
        f"All calls: {[str(getattr(c[0], 'text', c[0]))[:80] for c in execute_calls]}"
    )


# -----------------------------------------------------------------------
# Derived-grain §7.6.2: deploy serialises verification + epoch/pointer under a
# per-model advisory lock so a lost concurrent deploy cannot leave evidence the
# router trust predicate would wrongly accept.
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deploy_acquires_advisory_lock_and_rereads_epoch(client):
    """Deploy must take the per-model advisory lock and reread the model row
    (epoch) under it before bumping/staging evidence (spec §7.6.2)."""
    v_id = uuid.uuid4()
    model = make_model()
    model.deploy_epoch = 5
    mock_db = make_mock_db()

    ver = _version(v_id, 1, snapshot=_valid_snapshot())
    mock_db.get = AsyncMock(side_effect=[model, ver])

    execute_calls = []
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    default_result.scalars.return_value.all.return_value = []
    default_result.first.return_value = None

    async def _tracking_execute(*args, **kwargs):
        execute_calls.append(args)
        return default_result

    mock_db.execute = AsyncMock(side_effect=_tracking_execute)
    mock_db.refresh = AsyncMock()

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(f"{PREFIX}/deploy", json={"version_id": str(v_id)})

    assert resp.status_code == 200
    # Advisory lock acquired exactly once, keyed on the model.
    lock_calls = [
        c for c in execute_calls
        if len(c) > 0 and 'pg_advisory_xact_lock' in str(getattr(c[0], 'text', ''))
    ]
    assert len(lock_calls) == 1
    # The model row is reread under the lock before the epoch is bumped.
    mock_db.refresh.assert_awaited()
    assert model.deploy_epoch == 6


@pytest.mark.asyncio
async def test_undeploy_acquires_advisory_lock(client):
    """Undeploy clears the pointer + bumps the epoch, so it serialises against
    deploy/revert under the per-model advisory lock (spec §7.6.2)."""
    model = make_model()
    model.deployed_version_id = uuid.uuid4()
    model.deploy_epoch = 3
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.refresh = AsyncMock()

    execute_calls = []

    async def _tracking_execute(*args, **kwargs):
        execute_calls.append(args)
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        r.scalars.return_value.all.return_value = []
        return r

    mock_db.execute = AsyncMock(side_effect=_tracking_execute)

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(f"{PREFIX}/undeploy")

    assert resp.status_code == 200
    lock_calls = [
        c for c in execute_calls
        if len(c) > 0 and 'pg_advisory_xact_lock' in str(getattr(c[0], 'text', ''))
    ]
    assert len(lock_calls) == 1
    assert model.deploy_epoch == 4


# -----------------------------------------------------------------------
# Bug-7140: deploy/undeploy/revert bump deploy_epoch
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deploy_bumps_epoch(client):
    """Bug-7140: deploy must bump deploy_epoch for multi-replica cache key."""
    v_id = uuid.uuid4()
    model = make_model()
    model.deploy_epoch = 5
    mock_db = make_mock_db()

    ver = _version(v_id, 1, snapshot=_valid_snapshot())
    mock_db.get = AsyncMock(side_effect=[model, ver])

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(
            f"{PREFIX}/deploy",
            json={"version_id": str(v_id)},
        )

    assert resp.status_code == 200
    assert model.deploy_epoch == 6


@pytest.mark.asyncio
async def test_undeploy_bumps_epoch(client):
    """Bug-7140: undeploy must bump deploy_epoch."""
    model = make_model()
    model.deployed_version_id = uuid.uuid4()
    model.deploy_epoch = 3
    mock_db = make_mock_db()

    mock_db.get = AsyncMock(return_value=model)

    with patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)):
        resp = await client.post(f"{PREFIX}/undeploy")

    assert resp.status_code == 200
    assert model.deploy_epoch == 4


@pytest.mark.asyncio
async def test_revert_bumps_epoch(client):
    """Bug-7140: revert must bump deploy_epoch regardless of deploy state."""
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = uuid.uuid4()
    model.deploy_epoch = 7

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": str(v3_id)},
        )

    assert resp.status_code == 200
    assert model.deploy_epoch == 8


@pytest.mark.asyncio
async def test_revert_undeployed_bumps_epoch(client):
    """Bug-7140: revert on an undeployed model still bumps epoch."""
    v3_id = uuid.uuid4()
    model = make_model()
    assert model.deployed_version_id is None
    model.deploy_epoch = 0

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": str(v3_id)},
        )

    assert resp.status_code == 200
    assert model.deploy_epoch == 1


# -----------------------------------------------------------------------
# Bug-7906: revert is restore-as-new-version, never delete-history (DATA LOSS)
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug7906_revert_appends_version_and_never_deletes_history(client):
    """Bug-7906 (DATA LOSS): revert must NOT delete newer versions.

    It appends a NEW version carrying the reverted-to snapshot and moves the
    deploy pointer to that new version. Pre-fix, revert executed
    ``DELETE FROM model_versions WHERE version_number > N`` and pointed the
    deploy pointer at the OLD row — both assertions below fail on that code.
    """
    v3_id = uuid.uuid4()
    reverted_snapshot = _valid_snapshot()
    model = make_model()
    model.deployed_version_id = uuid.uuid4()
    model.deploy_epoch = 4

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(
        side_effect=[model, _version(v3_id, 3, snapshot=reverted_snapshot)]
    )

    added: list = []
    orig_add = mock_db.add

    def _tracking_add(obj):
        added.append(obj)
        return orig_add(obj)

    mock_db.add = MagicMock(side_effect=_tracking_add)

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": str(v3_id)},
        )

    assert resp.status_code == 200, resp.json()

    # 1) No DELETE against model_versions was issued — history is preserved.
    executed_sql = [
        str(c.args[0]) for c in mock_db.execute.call_args_list if c.args
    ]
    assert not any(
        "DELETE" in s.upper() and "MODEL_VERSIONS" in s.upper()
        for s in executed_sql
    ), f"revert must not delete versions; executed: {executed_sql}"

    # 2) Exactly one new ModelVersion was appended, carrying the reverted-to
    #    snapshot, tagged as a revert of v3.
    new_versions = [o for o in added if isinstance(o, ModelVersion)]
    assert len(new_versions) == 1, f"expected 1 appended version, got {new_versions}"
    nv = new_versions[0]
    assert nv.snapshot_json == reverted_snapshot
    assert nv.model_id == TEST_MODEL_ID
    assert (nv.summary or "").lower().startswith("revert to v3")

    # 3) The deploy pointer follows to the NEW version, not the old v3 row.
    assert model.deployed_version_id == nv.id


# -----------------------------------------------------------------------
# Bug-7146: revert stale-marks preserved aggregates
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_preserved_aggregates_stale_marks():
    """Bug-7146: _validate_preserved_aggregates must set is_stale=True on
    all surviving active aggregates when called with a snapshot, because the
    aggregate content was built against pre-revert definitions."""
    from shared.model_snapshot.rehydrator import _validate_preserved_aggregates

    model_id = uuid.uuid4()
    agg1_id = uuid.uuid4()
    agg2_id = uuid.uuid4()
    measure_id = uuid.uuid4()
    dim_name = "country"

    # Mock aggregates
    agg1 = types.SimpleNamespace(
        id=agg1_id,
        grain=[dim_name],
        is_stale=False,
    )
    agg2 = types.SimpleNamespace(
        id=agg2_id,
        grain=[dim_name],
        is_stale=False,
    )

    # Mock aggregate column
    agg_col = types.SimpleNamespace(
        aggregate_definition_id=agg1_id,
        measure_id=measure_id,
    )

    snapshot = {
        "measures": [{"id": str(measure_id), "name": "revenue", "expression": "SUM(amount)"}],
        "dimensions": [{"id": str(uuid.uuid4()), "name": dim_name}],
    }

    db = AsyncMock()

    # Simulate dimension query
    dim_result = MagicMock()
    dim_result.all.return_value = [(dim_name,)]

    # Simulate aggregate query
    agg_result = MagicMock()
    agg_result.scalars.return_value.all.return_value = [agg1, agg2]

    # Simulate aggregate column query
    agg_col_result = MagicMock()
    agg_col_result.scalars.return_value.all.return_value = [agg_col]

    call_count = [0]

    async def _mock_execute(stmt, *args, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx == 0:
            return dim_result
        elif idx == 1:
            return agg_result
        else:
            return agg_col_result

    db.execute = AsyncMock(side_effect=_mock_execute)

    await _validate_preserved_aggregates(model_id, db, snapshot=snapshot)

    # Verify that update calls were made to set is_stale=True
    update_calls = [
        c for c in db.execute.call_args_list
        if hasattr(c.args[0], 'is_update') or 'update' in str(c.args[0]).lower()
    ]
    # At least two updates (one per aggregate) for stale-marking
    assert len(update_calls) >= 2, (
        f"Expected at least 2 stale-mark update calls, got {len(update_calls)}"
    )


@pytest.mark.asyncio
async def test_validate_preserved_aggregates_invalid_on_missing_dim():
    """Bug-7146: aggregates with missing grain dimensions are marked invalid."""
    from shared.model_snapshot.rehydrator import _validate_preserved_aggregates

    model_id = uuid.uuid4()
    agg_id = uuid.uuid4()

    agg = types.SimpleNamespace(
        id=agg_id,
        grain=["missing_dimension"],
        is_stale=False,
    )

    snapshot = {
        "measures": [{"id": str(uuid.uuid4()), "name": "revenue"}],
        "dimensions": [],
    }

    db = AsyncMock()

    dim_result = MagicMock()
    dim_result.all.return_value = []  # no dimensions exist

    agg_result = MagicMock()
    agg_result.scalars.return_value.all.return_value = [agg]

    call_count = [0]

    async def _mock_execute(stmt, *args, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx == 0:
            return dim_result
        elif idx == 1:
            return agg_result
        return MagicMock()

    db.execute = AsyncMock(side_effect=_mock_execute)

    await _validate_preserved_aggregates(model_id, db, snapshot=snapshot)

    # Should have called execute with an update for status="invalid"
    update_calls = [
        c for c in db.execute.call_args_list
        if 'update' in str(c.args[0]).lower() and 'invalid' in str(c.args[0]).lower()
    ]
    assert len(update_calls) >= 1, (
        f"Expected at least 1 invalid-status update call, got {len(update_calls)}"
    )


# -----------------------------------------------------------------------
# Bug-7980 (F-013-06): Save's snapshot runs under REPEATABLE READ so a
# concurrent definition writer cannot be partially observed (no mixed version).
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consistent_snapshot_uses_repeatable_read_and_is_read_only():
    """The Save snapshot must be taken in a dedicated REPEATABLE READ
    transaction. Under snapshot isolation every read observes ONE consistent
    committed point-in-time, so a concurrent writer that commits mid-snapshot is
    seen atomically (all or none) and never partially — the property that
    prevents an internally inconsistent (mixed-time) version. The snapshot is
    read-only: it must never commit the dedicated session.

    Bug-8380: the implementation moved to shared.model_snapshot.consistent_read;
    versions._consistent_snapshot is now an alias. The test patches the shared
    module's imports.
    """
    from shared.model_snapshot import consistent_read as _cr_mod

    captured: dict = {}
    fake_session = AsyncMock()
    fake_session.info = {}

    async def _connection(execution_options=None):
        # snapshot_model must not have run before isolation is established.
        captured["snapshot_called_before_isolation"] = captured.get("snapshot_ran", False)
        captured["exec_opts"] = execution_options

    fake_session.connection = _connection

    class _CM:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *exc):
            return False

    def _factory():
        return _CM()

    async def _snapshot_model(model_id, db, include_versions=False):
        captured["snapshot_ran"] = True
        captured["snapshot_session"] = db
        return {"schema_version": 3, "model": {"id": str(model_id)}}

    with (
        patch(
            "shared.model_snapshot.consistent_read.get_tenant_snapshot_session_factory",
            new=AsyncMock(return_value=_factory),
        ),
        patch(
            "shared.model_snapshot.consistent_read.snapshot_model",
            new=_snapshot_model,
        ),
    ):
        result = await _cr_mod.consistent_snapshot(TEST_TENANT, TEST_MODEL_ID)

    assert result["model"]["id"] == str(TEST_MODEL_ID)
    # Isolation was set to REPEATABLE READ before any snapshot read.
    assert captured["exec_opts"] == {"isolation_level": "REPEATABLE READ"}
    assert captured["snapshot_called_before_isolation"] is False
    # snapshot_model read from the dedicated isolated session, not a shared one.
    assert captured["snapshot_session"] is fake_session
    # Read-only: the dedicated snapshot session is never committed.
    fake_session.commit.assert_not_called()
