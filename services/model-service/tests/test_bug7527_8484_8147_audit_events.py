"""Lane L17 — audit, live-source-check and actor-email guards.

Covers three ported gaps, each test named for the Bug-N it protects and
written to FAIL against the pre-fix code:

* Bug-7527 — a rejected Collibra/Solidatus live push must leave a SANITIZED
  ``*.sync.rejected`` audit event, COMMITTED before the 501 so the caller's
  rollback cannot lose it.
* Bug-8484 — the manual "Re-check model" revalidate must actually contact the
  source (``queue_model_source_check``) and report it via ``live_source_checked``.
* Bug-8147 — dismiss and revalidate must record the acting user's email on the
  audit trail; dismiss must not write the email (the JWT subject) into the UUID
  ``dismissed_by`` column.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException

from shared.db.models import AuditEvent, SchemaChangeEvent
from shared.schemas.pydantic_models import (
    CollibraSyncRequest,
    SolidatusSyncRequest,
)

from src.api import alerts as alerts_api
from src.api import collibra as collibra_api
from src.api import solidatus as solidatus_api
from src.auth.middleware import CurrentUser

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
)


def _user() -> CurrentUser:
    return CurrentUser(
        user_id="modeler@example.com",
        tenant_id=TEST_TENANT,
        email="modeler@example.com",
    )


def _captured_audits(db) -> list[AuditEvent]:
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditEvent)]


# ---------------------------------------------------------------------------
# Bug-7527 — sanitized, durable rejection audit before the 501
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug_7527_collibra_push_rejection_audits_before_501():
    db = make_mock_db()
    model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
        deployed_version_id=uuid.uuid4(),
    )
    conn = types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, display_name="Prod Collibra",
    )

    async def _get(cls, ident):
        from shared.db.models import CollibraConnection, Model
        if cls is Model:
            return model
        if cls is CollibraConnection:
            return conn
        return None

    db.get = AsyncMock(side_effect=_get)
    body = CollibraSyncRequest(connection_id=conn.id, dry_run=False)

    with patch.object(collibra_api, "get_tenant_db", async_gen_from(db)):
        with pytest.raises(HTTPException) as exc:
            await collibra_api.collibra_sync(
                TEST_PROJECT_ID, TEST_MODEL_ID, body, _user()
            )

    assert exc.value.status_code == 501
    audits = _captured_audits(db)
    rejected = [a for a in audits if a.action == "collibra.sync.rejected"]
    assert rejected, "rejected push must emit a collibra.sync.rejected audit event"
    ev = rejected[0]
    # Durable: committed before the 501 rolls the request back.
    db.commit.assert_awaited()
    # Sanitized: no credentials, no full payload — only reason + safe flags.
    assert ev.actor_email == "modeler@example.com"
    keys = set((ev.detail or {}).keys())
    assert keys <= {"reason", "dry_run", "deprecate_missing", "export_draft"}
    blob = repr(ev.detail).lower()
    assert "credential" not in blob and "password" not in blob and "token" not in blob


@pytest.mark.asyncio
async def test_bug_7527_solidatus_push_rejection_audits_before_501():
    db = make_mock_db()
    model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
        deployed_version_id=uuid.uuid4(),
    )
    conn = types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, display_name="Prod Solidatus",
    )

    async def _get(cls, ident):
        from shared.db.models import Model, SolidatusConnection
        if cls is Model:
            return model
        if cls is SolidatusConnection:
            return conn
        return None

    db.get = AsyncMock(side_effect=_get)
    body = SolidatusSyncRequest(connection_id=conn.id, mode="push", dry_run=False)

    with patch.object(solidatus_api, "get_tenant_db", async_gen_from(db)):
        with pytest.raises(HTTPException) as exc:
            await solidatus_api.solidatus_sync(
                TEST_PROJECT_ID, TEST_MODEL_ID, body, _user()
            )

    assert exc.value.status_code == 501
    rejected = [a for a in _captured_audits(db) if a.action == "solidatus.sync.rejected"]
    assert rejected, "rejected push must emit a solidatus.sync.rejected audit event"
    db.commit.assert_awaited()
    ev = rejected[0]
    assert ev.actor_email == "modeler@example.com"
    assert set((ev.detail or {}).keys()) <= {"reason", "mode", "export_draft"}


# ---------------------------------------------------------------------------
# Bug-8484 — queue_model_source_check producer
# ---------------------------------------------------------------------------

def _table_with_source():
    conn = types.SimpleNamespace(connection_type="postgresql", config={})
    source = types.SimpleNamespace(project_connection=conn)
    return types.SimpleNamespace(
        model_id=TEST_MODEL_ID, source=source, source_id=uuid.uuid4(),
        physical_name="public.orders",
    )


@pytest.mark.asyncio
async def test_bug_8484_source_check_contacts_live_source():
    from shared.schema_drift import source_check as sc

    db = make_mock_db()
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    db.get = AsyncMock(return_value=model)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_table_with_source()]
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    with (
        patch.object(sc, "assert_connection_in_project", lambda *a, **k: None),
        patch.object(sc, "verify_source_table_exists", AsyncMock(return_value=None)) as probe,
    ):
        contacted = await sc.queue_model_source_check(
            db, model_id=TEST_MODEL_ID, tenant_id=TEST_TENANT
        )

    assert contacted is True, "a reachable source table means the live source was checked"
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_bug_8484_missing_table_records_durable_event():
    from shared.schema_drift import source_check as sc
    from shared.source_table_probe import SourceTableNotFoundError

    db = make_mock_db()
    model = types.SimpleNamespace(id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID)
    db.get = AsyncMock(return_value=model)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_table_with_source()]
    result.scalar_one_or_none.return_value = None  # no already-open table_removed
    db.execute = AsyncMock(return_value=result)

    with (
        patch.object(sc, "assert_connection_in_project", lambda *a, **k: None),
        patch.object(
            sc, "verify_source_table_exists",
            AsyncMock(side_effect=SourceTableNotFoundError("public.orders")),
        ),
    ):
        contacted = await sc.queue_model_source_check(
            db, model_id=TEST_MODEL_ID, tenant_id=TEST_TENANT
        )

    assert contacted is True
    added = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], SchemaChangeEvent)]
    assert added and added[0].change_type == "table_removed"


@pytest.mark.asyncio
async def test_bug_8484_revalidate_reports_live_source_checked():
    db = make_mock_db()
    result = MagicMock()
    result.one.return_value = (0, 0, 0, None)
    result.scalars.return_value.all.return_value = []
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    report = types.SimpleNamespace(
        invalid_dimensions=[], invalid_measures=[], invalid_aggregates=[],
        newly_valid_dimensions=[], newly_valid_measures=[], newly_valid_aggregates=[],
    )

    with (
        patch.object(alerts_api, "get_tenant_db", async_gen_from(db)),
        patch.object(alerts_api, "ensure_model_in_project", AsyncMock()),
        patch.object(alerts_api, "revalidate_model", AsyncMock(return_value=report)),
        patch.object(alerts_api, "queue_model_source_check", AsyncMock(return_value=True)) as probe,
    ):
        resp = await alerts_api.revalidate_model_endpoint(
            TEST_PROJECT_ID, TEST_MODEL_ID, _user()
        )

    probe.assert_awaited_once()
    assert resp.live_source_checked is True
    # Bug-8147: the re-check records the acting user's email.
    revalidated = [a for a in _captured_audits(db) if a.action == "model.revalidated"]
    assert revalidated and revalidated[0].actor_email == "modeler@example.com"


# ---------------------------------------------------------------------------
# Bug-8147 — dismiss records actor_email and never writes the email as a UUID
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug_8147_dismiss_audits_actor_email_and_uses_uuid_dismissed_by():
    db = make_mock_db()
    actor_uuid = uuid.uuid4()
    from datetime import datetime, timezone
    _now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    alert = types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, category="join_graph",
        severity="warning", title="Join graph disconnected", detail=None,
        related_object_type=None, related_object_id=None,
        first_seen_at=_now, last_seen_at=_now, occurrence_count=1,
        resolved_at=None, dismissed_at=None, dismissed_by=None,
    )
    result = MagicMock()
    # LocalUser.id lookup resolves the acting user's real UUID; the audit-level
    # read falls through to "info" for a non-str/dict scalar.
    result.scalar_one_or_none.return_value = actor_uuid
    db.execute = AsyncMock(return_value=result)

    dismiss_mock = AsyncMock(return_value=alert)

    with (
        patch.object(alerts_api, "get_tenant_db", async_gen_from(db)),
        patch.object(alerts_api, "ensure_model_in_project", AsyncMock()),
        patch.object(alerts_api, "dismiss_alert", dismiss_mock),
    ):
        await alerts_api.dismiss_alert_endpoint(
            TEST_PROJECT_ID, TEST_MODEL_ID, alert.id, _user()
        )

    # dismissed_by must be the resolved UUID, never the email (the JWT subject).
    passed = dismiss_mock.await_args.kwargs["dismissed_by"]
    assert passed == actor_uuid
    assert passed != "modeler@example.com"
    # Durable actor identity lands on the audit trail as actor_email.
    dismissed = [a for a in _captured_audits(db) if a.action == "model.alert.dismissed"]
    assert dismissed, "dismiss must emit a model.alert.dismissed audit event"
    assert dismissed[0].actor_email == "modeler@example.com"
