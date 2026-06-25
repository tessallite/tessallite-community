from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

from shared.db.models import DataSource, KPI, Model, ProjectConnection
from src.auth.middleware import CurrentEmbedUser, CurrentUser
import src.api.kpis as kpis_module
from src.api.kpis import (
    _assert_kpi_expressions_in_measure_scope,
    _ensure_kpi_model_scope,
    _kpi_visible_to_persona,
    _load_visible_kpi_or_404,
    router as kpi_router,
)
from src.api._scope import resolve_source_connection
from src.api.sources import _validate_source_connection
from src.api.targets import _validate_target_connection


class _DB:
    def __init__(self, *, model=None, conn=None, kpi=None, measures=None):
        self.model = model
        self.conn = conn
        self.kpi = kpi
        self.measures = measures or []

    async def get(self, cls, key):
        if cls is Model:
            return self.model
        if cls is ProjectConnection:
            return self.conn
        if cls is KPI:
            return self.kpi
        return None

    async def execute(self, stmt):
        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return self._rows

        return _Result(self.measures)


def _model(project_id):
    return types.SimpleNamespace(id=uuid.uuid4(), project_id=project_id)


def _conn(project_id, connection_type="postgresql"):
    return types.SimpleNamespace(project_id=project_id, connection_type=connection_type)


def test_kpi_router_enforces_model_scope_for_all_routes():
    dependency_callables = {
        dependency.dependency for dependency in kpi_router.dependencies
    }

    assert _ensure_kpi_model_scope in dependency_callables


def test_kpi_target_expression_must_stay_in_persona_measure_scope():
    with pytest.raises(HTTPException) as exc:
        _assert_kpi_expressions_in_measure_scope(
            ['measure("visible_revenue")', 'measure("hidden_margin")'],
            {"visible_revenue"},
        )

    assert exc.value.status_code == 404


def test_saved_kpi_visibility_rejects_hidden_target_expression():
    visible_id = uuid.uuid4()
    hidden_id = uuid.uuid4()
    kpi = types.SimpleNamespace(
        expression='measure("visible_revenue")',
        target_expression='measure("hidden_margin")',
        target_measure_id=None,
    )

    assert not _kpi_visible_to_persona(
        kpi,
        [visible_id],
        {
            "visible_revenue": visible_id,
            "hidden_margin": hidden_id,
        },
    )


def test_saved_kpi_visibility_rejects_hidden_target_measure_id():
    visible_id = uuid.uuid4()
    hidden_id = uuid.uuid4()
    kpi = types.SimpleNamespace(
        expression='measure("visible_revenue")',
        target_expression=None,
        target_measure_id=hidden_id,
    )

    assert not _kpi_visible_to_persona(
        kpi,
        [visible_id],
        {
            "visible_revenue": visible_id,
            "hidden_margin": hidden_id,
        },
    )


def test_saved_kpi_visibility_allows_visible_target_dependencies():
    visible_id = uuid.uuid4()
    target_id = uuid.uuid4()
    kpi = types.SimpleNamespace(
        expression='measure("visible_revenue")',
        target_expression='measure("visible_target")',
        target_measure_id=str(target_id),
    )

    assert _kpi_visible_to_persona(
        kpi,
        [visible_id, target_id],
        {
            "visible_revenue": visible_id,
            "visible_target": target_id,
        },
    )


@pytest.mark.asyncio
async def test_source_connection_must_belong_to_url_project():
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    db = _DB(conn=_conn(other_project_id))

    with pytest.raises(HTTPException) as exc:
        await _validate_source_connection(db, project_id, uuid.uuid4())

    assert exc.value.status_code == 422
    assert "different project" in exc.value.detail


@pytest.mark.asyncio
async def test_target_validation_rejects_model_outside_url_project():
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model = _model(other_project_id)
    db = _DB(model=model, conn=_conn(other_project_id))

    with pytest.raises(HTTPException) as exc:
        await _validate_target_connection(
            db, project_id, model.id, uuid.uuid4()
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_visible_kpi_guard_hides_draft_from_regular_viewer():
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=project_id)
    kpi = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=model_id,
        certification_status="draft",
        expression="revenue",
    )
    db = _DB(model=model, kpi=kpi)
    user = CurrentUser(
        user_id="viewer@example.com",
        tenant_id="tenant-1",
        email="viewer@example.com",
        role="member",
    )

    with pytest.raises(HTTPException) as exc:
        await _load_visible_kpi_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            kpi_id=kpi.id,
            current_user=user,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_visible_kpi_guard_rejects_model_outside_url_project():
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=other_project_id)
    kpi = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=model_id,
        certification_status="deployed",
        expression="revenue",
    )
    db = _DB(model=model, kpi=kpi)
    user = CurrentUser(
        user_id="viewer@example.com",
        tenant_id="tenant-1",
        email="viewer@example.com",
        role="member",
    )

    with pytest.raises(HTTPException) as exc:
        await _load_visible_kpi_or_404(
            db,
            project_id=project_id,
            model_id=model_id,
            kpi_id=kpi.id,
            current_user=user,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_kpi_router_scope_dependency_rejects_embed_outside_project(monkeypatch):
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    model_id = uuid.uuid4()
    model = types.SimpleNamespace(id=model_id, project_id=project_id)
    db = _DB(model=model)
    user = CurrentEmbedUser(
        user_id="embed@example.com",
        tenant_id="tenant-1",
        email="embed@example.com",
        project_ids=[str(other_project_id)],
        model_ids=None,
    )

    async def _tenant_db(_tenant_id):
        yield db

    monkeypatch.setattr(kpis_module, "get_tenant_db", _tenant_db)

    with pytest.raises(HTTPException) as exc:
        await _ensure_kpi_model_scope(project_id, model_id, user)

    assert exc.value.status_code == 403
    assert "Project not in embed token scope" in exc.value.detail


# ---------------------------------------------------------------------------
# Bug-5325: read-time DataSource.project_connection_id defense
# ---------------------------------------------------------------------------


def _source(model_id, connection_id):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=model_id,
        project_connection_id=connection_id,
    )


@pytest.mark.asyncio
async def test_resolve_source_connection_rejects_cross_project_row():
    """A legacy/imported source whose connection belongs to a DIFFERENT
    project must be rejected at read time (fail-closed), not silently used."""
    owning_project = uuid.uuid4()
    other_project = uuid.uuid4()
    connection_id = uuid.uuid4()
    source = _source(uuid.uuid4(), connection_id)
    db = _DB(conn=_conn(other_project))

    with pytest.raises(HTTPException) as exc:
        await resolve_source_connection(
            db, source, expected_project_id=owning_project
        )

    assert exc.value.status_code == 422
    assert "different project" in exc.value.detail


@pytest.mark.asyncio
async def test_resolve_source_connection_allows_matching_project_row():
    """A well-formed source whose connection belongs to the SAME project
    resolves to that connection."""
    owning_project = uuid.uuid4()
    connection_id = uuid.uuid4()
    conn = _conn(owning_project)
    source = _source(uuid.uuid4(), connection_id)
    db = _DB(conn=conn)

    result = await resolve_source_connection(
        db, source, expected_project_id=owning_project
    )

    assert result is conn


@pytest.mark.asyncio
async def test_resolve_source_connection_missing_connection_is_404():
    """A dangling project_connection_id (row deleted) is rejected, not used."""
    owning_project = uuid.uuid4()
    source = _source(uuid.uuid4(), uuid.uuid4())
    db = _DB(conn=None)

    with pytest.raises(HTTPException) as exc:
        await resolve_source_connection(
            db, source, expected_project_id=owning_project
        )

    assert exc.value.status_code == 404
