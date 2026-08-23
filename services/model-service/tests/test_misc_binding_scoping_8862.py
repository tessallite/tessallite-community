"""Bug-8862 (non-tables half): nested routes must bind project -> model -> resource.

Test escape
-----------
``schema_changes.list_schema_changes``, ``schema_changes.acknowledge_schema_change``,
``refresh_stream.stream_refresh_runs`` and ``validation.validate_model`` each
declared ``project_id`` (and, for acknowledge, ``model_id``) as a path parameter
and never referenced it. ``require_role`` (``src/auth/rbac.py``) reads
``project_id`` from the path and checks the CALLER'S BINDING for that project;
it never proves that the model or event named in the path belongs to it. So a
caller holding a legitimate binding in project A could pass ``project_id=A`` to
satisfy RBAC together with a model from project B of the same tenant, and read
B's schema-change history, B's refresh-run stream, B's full object inventory —
or ACKNOWLEDGE (mutate) B's schema-change events.

No test asserted that any of these routes rejects a foreign resource, so the
whole class was invisible to the suite.

Guard: this file. Tier: T2 (fixed-bug regression guard).

Design notes
------------
* Tests go through the real HTTP route, not just the handler function, so the
  route dependencies, the ``require_role`` gate and the handler body are all on
  the proven path. ``conftest.mock_rbac_get_tenant_db`` puts ``require_role`` on
  the bootstrap-admin path, which is exactly what makes these assertions
  meaningful: the ROLE gate is wide open, so a 404 can only come from the
  resource-ownership guard under test.
* Every denial asserts the rejection REASON, not only the status code. A bare
  ``== 404`` can pass for an unrelated reason (e.g. a mock returning no rows),
  which would leave the guard unproven.
* 404, never 403: confirming the resource exists elsewhere in the tenant would
  itself be a cross-project leak.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import AggregateRefreshRun, Model, SchemaChangeEvent

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, make_mock_db

OTHER_PROJECT_ID = uuid.uuid4()
OTHER_MODEL_ID = uuid.uuid4()
EVENT_ID = uuid.uuid4()

_SCHEMA_CHANGES_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/schema-changes"
)
_ACK_URL = f"{_SCHEMA_CHANGES_URL}/{EVENT_ID}/acknowledge"
_STREAM_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/refresh/stream"
)
_VALIDATE_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/validate"
)
_EVENT_TYPES_URL = f"/api/v1/projects/{TEST_PROJECT_ID}/notifications/event-types"


def _db(*, model_project_id, event_model_id=TEST_MODEL_ID):
    """A mock session whose Model / SchemaChangeEvent have the given owners.

    ``model_project_id=None`` simulates a model that does not exist at all.
    """
    db = make_mock_db()

    async def _get(entity, entity_id):
        if entity is Model:
            if model_project_id is None:
                return None
            return types.SimpleNamespace(id=entity_id, project_id=model_project_id)
        if entity is SchemaChangeEvent:
            return types.SimpleNamespace(
                id=entity_id,
                model_id=event_model_id,
                acknowledged_at=None,
            )
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _stream_db(*, model_project_id):
    """A stream-route session whose refresh-run queries return a TERMINAL run.

    This is what makes the two stream denial tests fail FAST instead of hanging.
    ``make_mock_db`` returns no rows by default, so if the guard were removed
    ``_stream_events`` would see zero runs, never reach a terminal state, and —
    with ``asyncio.sleep`` patched to an instant AsyncMock — busy-loop against
    the real-time ``_TIMEOUT`` of 300s for five wall-clock minutes per test.
    A regression guard whose failure mode is a CPU-spinning hang rather than a
    named red test is not a usable guard: the engineer who deletes the guard
    sees a stuck CI job, not the assertion that explains what they broke.

    Seeding one terminal run makes the unguarded path emit ``event: done`` and
    return immediately, so the assertions below fail in milliseconds.
    """
    db = _db(model_project_id=model_project_id)
    terminal_run = types.SimpleNamespace(
        id=uuid.uuid4(),
        status=AggregateRefreshRun.STATUS_COMPLETED,
        refresh_mode="full",
        started_at=None,
        completed_at=None,
        rows_written=0,
        error_message=None,
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [terminal_run]
    db.execute = AsyncMock(return_value=result)
    return db


def _patch(module: str, db):
    return patch(f"src.api.{module}.get_tenant_db", async_gen_from(db))


# ---------------------------------------------------------------------------
# schema_changes.py:61 — list_schema_changes (read)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_schema_changes_rejects_model_from_another_project(client):
    db = _db(model_project_id=OTHER_PROJECT_ID)

    with _patch("schema_changes", db):
        resp = await client.get(_SCHEMA_CHANGES_URL)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_list_schema_changes_allows_correctly_scoped_model(client):
    """The guard must not be a blanket denial — a valid chain still reads."""
    db = _db(model_project_id=TEST_PROJECT_ID)

    with _patch("schema_changes", db):
        resp = await client.get(_SCHEMA_CHANGES_URL)

    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# schema_changes.py:77 — acknowledge_schema_change (WRITE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acknowledge_rejects_model_from_another_project(client):
    """The write path is the one that matters most: no cross-project mutation."""
    db = _db(model_project_id=OTHER_PROJECT_ID)

    with _patch("schema_changes", db):
        resp = await client.post(_ACK_URL)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledge_rejects_event_from_another_model(client):
    """Second link of the chain: the event must belong to the path model.

    Proving project -> model is not sufficient here — the handler looked the
    event up by ``event_id`` alone, so an event under a DIFFERENT model (and
    therefore possibly a different project) was still mutable.
    """
    db = _db(model_project_id=TEST_PROJECT_ID, event_model_id=OTHER_MODEL_ID)

    with _patch("schema_changes", db):
        resp = await client.post(_ACK_URL)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Schema change event not found"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledge_fails_closed_when_no_tenant_session_is_yielded(client):
    """A 204 must never be answered when no write happened.

    Bug-8862 follow-up, raised by the external challenger. This route is
    declared ``status_code=204``, so falling out of the
    ``async for db in get_tenant_db(...)`` loop without writing would return
    ``None`` and FastAPI would answer a bare 204 — telling the caller the
    acknowledgement SUCCEEDED when nothing was acknowledged. Silent false
    success on a write path is worse than an error.

    Unreachable today (``get_tenant_db`` always yields exactly once); the test
    pins the CONTROL FLOW so the handler cannot regress into answering 204 on
    a path that performed no write.
    """
    async def _no_session(_tenant_id):
        return
        yield  # pragma: no cover — makes this an async generator

    with patch("src.api.schema_changes.get_tenant_db", _no_session):
        resp = await client.post(_ACK_URL)

    assert resp.status_code != 204
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Tenant database unavailable"


@pytest.mark.asyncio
async def test_acknowledge_allows_correctly_scoped_event(client):
    db = _db(model_project_id=TEST_PROJECT_ID, event_model_id=TEST_MODEL_ID)

    with _patch("schema_changes", db):
        resp = await client.post(_ACK_URL)

    assert resp.status_code == 204
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# refresh_stream.py:131 — stream_refresh_runs (SSE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_rejects_model_from_another_project_before_opening(client):
    """The denial must be a clean JSON 404, not a broken event-stream.

    Once ``StreamingResponse`` is returned the 200 and the ``text/event-stream``
    content-type are already committed, so a check inside the generator could
    not produce a 404. Asserting the content-type here is what proves the guard
    runs BEFORE the stream opens.
    """
    db = _stream_db(model_project_id=OTHER_PROJECT_ID)

    with (
        _patch("refresh_stream", db),
        patch("src.api.refresh_stream.asyncio.sleep", AsyncMock()),
    ):
        resp = await client.get(_STREAM_URL)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"
    assert "text/event-stream" not in resp.headers["content-type"]
    # The stream body never started: no SSE frame of any kind was emitted.
    assert "event: connected" not in resp.text


@pytest.mark.asyncio
async def test_stream_rejects_missing_model(client):
    """A model that does not exist at all is denied identically (no oracle)."""
    db = _stream_db(model_project_id=None)

    with (
        _patch("refresh_stream", db),
        patch("src.api.refresh_stream.asyncio.sleep", AsyncMock()),
    ):
        resp = await client.get(_STREAM_URL)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


@pytest.mark.asyncio
async def test_stream_fails_closed_when_no_tenant_session_is_yielded(client):
    """The guard must be un-skippable, not merely present.

    Bug-8862 follow-up. The first shape of this fix ran the guard inside
    ``async for db in get_tenant_db(...)`` and then returned the
    StreamingResponse UNCONDITIONALLY, outside the loop. A session generator
    that yielded zero times would skip the ownership check and still open the
    stream — fail-open on a tenant-isolation guard.

    ``get_tenant_db`` always yields exactly once today, so this is not a live
    exposure; the test pins the CONTROL FLOW so the endpoint cannot regress to
    a shape where the guard is bypassable. It must never answer with an SSE
    stream when no session was available to prove ownership.
    """
    async def _no_session(_tenant_id):
        return
        yield  # pragma: no cover — makes this an async generator

    with (
        patch("src.api.refresh_stream.get_tenant_db", _no_session),
        patch("src.api.refresh_stream.asyncio.sleep", AsyncMock()),
    ):
        resp = await client.get(_STREAM_URL)

    assert resp.status_code != 200
    assert "text/event-stream" not in resp.headers["content-type"]
    assert "event: connected" not in resp.text
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Tenant database unavailable"


# ---------------------------------------------------------------------------
# validation.py:59 — validate_model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_rejects_model_from_another_project(client):
    """Denied before any model structure is loaded.

    The violation payload names every dimension, measure and aggregate of the
    model, so an unbound chain here is a full object-inventory disclosure.
    """
    db = _db(model_project_id=OTHER_PROJECT_ID)
    structure = AsyncMock()

    with (
        _patch("validation", db),
        patch("src.api.validation._load_model_structure", structure),
    ):
        resp = await client.post(_VALIDATE_URL)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"
    structure.assert_not_awaited()


# ---------------------------------------------------------------------------
# notifications.py:283 — list_notification_event_types
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_types_is_a_static_catalogue_with_no_project_data(client):
    """Bug-8862 judged on its merits: this handler needs NO ownership guard.

    It is the one flagged handler with no nested resource to bind. The body
    touches no session and returns the process-wide ``EVENT_TYPES`` constant,
    so the payload is byte-identical for every project. This test pins that
    property: if someone later makes the response project-dependent, the
    "no guard needed" conclusion stops holding and this fails.
    """
    from shared.alerting.dispatcher import EVENT_TYPES

    opened: list[str] = []

    def _forbidden_session(tenant_id):
        opened.append(str(tenant_id))
        raise AssertionError(
            "list_notification_event_types opened a tenant session; it is "
            "documented as touching no project-scoped data"
        )

    with patch("src.api.notifications.get_tenant_db", _forbidden_session):
        resp_a = await client.get(_EVENT_TYPES_URL)
        resp_b = await client.get(
            f"/api/v1/projects/{OTHER_PROJECT_ID}/notifications/event-types"
        )

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    # Identical across two different projects -> nothing project-scoped leaks.
    assert resp_a.json() == resp_b.json()
    assert {row["value"] for row in resp_a.json()} == set(EVENT_TYPES)
    assert [row["label"] for row in resp_a.json()] == [
        row["label"] for row in resp_b.json()
    ]
    # And the handler never opened a tenant session at all.
    assert opened == []
