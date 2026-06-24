"""ML12 business-outcome tests for the versioning endpoints.

F-013-08 — Save emits a ``model.save`` audit event; Revert emits a
            ``model.revert`` audit event and a ``model.reverted`` webhook.
F-013-10 — concurrent Save retries once on a unique-violation, then 409s
            (never 500s).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _version(version_id: uuid.UUID, number: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        version_number=number,
        summary=None,
        snapshot_json={"schema_version": 3, "model": {"id": str(TEST_MODEL_ID)}},
        created_at=NOW,
        created_by="user@example.com",
    )


async def _rehydrate_noop(*_args, **_kwargs):
    return None


def _result(scalar=None):
    """A MagicMock that mimics a SQLAlchemy Result for both access shapes:
    ``.scalar_one_or_none()`` and ``.scalars().first()`` (bootstrap-admin
    binding probe in _ensure_model_access)."""
    r = MagicMock()
    r.scalar_one_or_none.return_value = scalar
    r.scalars.return_value.first.return_value = None  # no binding -> bootstrap
    r.scalars.return_value.all.return_value = []
    return r


def _unique_violation() -> IntegrityError:
    return IntegrityError(
        "INSERT", {}, Exception("duplicate key value violates unique constraint")
    )


class _ExpirableModel:
    """Simulates SQLAlchemy's post-rollback expiration semantics.

    After ``expire()`` is called (which should be triggered by the mock
    session's ``rollback()``), attribute reads on the underlying base
    object are still allowed but each access increments
    ``_refresh_count`` — mirroring the implicit lazy-load that a real
    AsyncSession performs when accessing expired attributes.  This lets
    the test verify that the retry path works even when the ORM
    identity-map is invalidated, rather than silently relying on
    MagicMock's accept-anything behaviour.

    Note: ``_expired`` stays True once set (it is never cleared), so
    every subsequent attribute access is counted as a simulated refresh.
    """

    def __init__(self, base: types.SimpleNamespace):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_expired", False)
        object.__setattr__(self, "_refresh_count", 0)

    def expire(self) -> None:
        object.__setattr__(self, "_expired", True)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        base = object.__getattribute__(self, "_base")
        if object.__getattribute__(self, "_expired"):
            # Simulate the lazy-load: the attribute is available (a real
            # session would issue a SELECT to refresh), but we record that
            # the refresh happened so the test can assert it.
            object.__setattr__(
                self, "_refresh_count",
                object.__getattribute__(self, "_refresh_count") + 1,
            )
        return getattr(base, name)

    def __setattr__(self, name: str, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_base"), name, value)


# ---------------------------------------------------------------------------
# F-013-08 — Save audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_emits_audit_event(client):
    """A Save writes a model.save audit event."""
    model = make_model()

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    # 1st execute = _ensure_model_access binding probe (None -> bootstrap);
    # 2nd execute = last-version lookup (v4 -> next v5).
    mock_db.execute = AsyncMock(side_effect=[_result(None), _result(4)])

    async def _refresh(version):
        version.id = uuid.uuid4()
        version.created_at = NOW

    mock_db.refresh = _refresh

    audit_calls = []

    async def _capture_audit(_db, **kwargs):
        audit_calls.append(kwargs)

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.snapshot_model", new=AsyncMock(return_value={})),
        patch("src.api.versions.audit", new=_capture_audit),
    ):
        resp = await client.post(f"{PREFIX}/versions", json={"summary": "edit"})

    assert resp.status_code == 200
    assert any(c.get("action") == "model.save" for c in audit_calls), audit_calls
    save = next(c for c in audit_calls if c["action"] == "model.save")
    assert save["severity"] == "info"
    assert save["detail"]["version_number"] == 5


# ---------------------------------------------------------------------------
# F-013-10 — concurrent Save retries once, then 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_save_retries_then_succeeds(client):
    """First flush hits a unique violation; the retry succeeds (no 500).

    Uses _ExpirableModel to simulate real AsyncSession rollback-expiration
    semantics: after rollback(), all loaded ORM objects are expired, and
    attribute access triggers an implicit refresh (lazy load).  This
    verifies the retry path genuinely works under identity-map invalidation,
    not just with a MagicMock that silently accepts everything.
    """
    model = _ExpirableModel(make_model())

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    # binding probe, then a version lookup per attempt (2 attempts).
    mock_db.execute = AsyncMock(
        side_effect=[_result(None), _result(1), _result(1)]
    )
    # First flush raises (race lost), second flush succeeds.
    mock_db.flush = AsyncMock(side_effect=[_unique_violation(), None])

    # Wire rollback to expire the model, simulating real session behaviour.
    _original_rollback = mock_db.rollback

    async def _rollback_with_expiration():
        model.expire()
        return await _original_rollback()

    mock_db.rollback = _rollback_with_expiration

    async def _refresh(version):
        version.id = uuid.uuid4()
        version.created_at = NOW

    mock_db.refresh = _refresh

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.snapshot_model", new=AsyncMock(return_value={})),
        patch("src.api.versions.audit", new=AsyncMock()),
        # F-013-17: retention prune runs after a successful Save; this test
        # asserts the retry/commit behaviour, not retention, so stub it out
        # (it issues its own reads the fixed execute side_effect list does not
        # account for).
        patch("src.api.versions._prune_old_versions", new=AsyncMock()),
    ):
        resp = await client.post(f"{PREFIX}/versions", json={})

    assert resp.status_code == 200
    assert mock_db.flush.await_count == 2
    # Verify rollback was called (expiring the model's identity-map state).
    assert _original_rollback.await_count == 1
    # After rollback, the retry path accessed the model's attributes
    # (display_name in audit, deployed_version_id in _to_item); those
    # accesses triggered simulated lazy-loads on the expired model.
    assert model._refresh_count > 0, (
        "model attributes were never accessed after rollback-expiration; "
        "the retry path should read model.display_name / "
        "model.deployed_version_id after the rollback"
    )


@pytest.mark.asyncio
async def test_concurrent_save_double_collision_returns_409(client):
    """Both attempts collide -> 409, not an unhandled 500.

    Uses _ExpirableModel to verify the 409 path works even when the
    session's identity map is invalidated by rollbacks between attempts.
    """
    model = _ExpirableModel(make_model())

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=model)
    mock_db.execute = AsyncMock(
        side_effect=[_result(None), _result(1), _result(1)]
    )
    mock_db.flush = AsyncMock(
        side_effect=[_unique_violation(), _unique_violation()]
    )

    # Wire both rollbacks to expire the model — each collision triggers
    # a rollback that invalidates the identity map.
    _original_rollback = mock_db.rollback

    async def _rollback_with_expiration():
        model.expire()
        return await _original_rollback()

    mock_db.rollback = _rollback_with_expiration

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.snapshot_model", new=AsyncMock(return_value={})),
        patch("src.api.versions.audit", new=AsyncMock()),
    ):
        resp = await client.post(f"{PREFIX}/versions", json={})

    assert resp.status_code == 409
    assert "collided" in resp.json()["detail"].lower()
    # Both collisions triggered rollback-expiration.
    assert _original_rollback.await_count == 2


# ---------------------------------------------------------------------------
# F-013-08 — Revert audit + webhook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revert_emits_audit_and_webhook(client):
    """Revert writes a critical model.revert audit event and a
    model.reverted webhook."""
    v3_id = uuid.uuid4()
    model = make_model()
    model.deployed_version_id = uuid.uuid4()  # deployed

    mock_db = make_mock_db()
    mock_db.get = AsyncMock(side_effect=[model, _version(v3_id, 3)])

    audit_calls = []
    webhook_calls = []

    async def _capture_audit(_db, **kwargs):
        audit_calls.append(kwargs)

    async def _capture_webhook(tenant_id, event, payload):
        webhook_calls.append((event, payload))

    with (
        patch("src.api.versions.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.versions.rehydrate_into_live", new=_rehydrate_noop),
        patch("src.api.versions.audit", new=_capture_audit),
        patch("src.api.versions.emit_webhook", new=_capture_webhook),
    ):
        resp = await client.post(
            f"{PREFIX}/versions/{v3_id}/revert",
            json={"confirm": "revert to v3"},
        )

    assert resp.status_code == 200
    revert_audit = next(
        (c for c in audit_calls if c.get("action") == "model.revert"), None
    )
    assert revert_audit is not None, audit_calls
    assert revert_audit["severity"] == "critical"
    assert revert_audit["detail"]["reverted_to_version"] == 3
    assert revert_audit["detail"]["was_deployed"] is True

    assert ("model.reverted", ) == tuple(e for e, _ in webhook_calls)
    _event, payload = webhook_calls[0]
    assert payload["reverted_to_version"] == 3
    assert payload["model_id"] == str(TEST_MODEL_ID)
