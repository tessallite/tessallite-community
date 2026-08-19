"""F-009-18 — DataTarget connection validation at write time.

``_validate_target_connection`` rejects a target whose referenced connection
is missing, belongs to a different project, or is an unsupported connector,
so the optimizer/scheduler never surface those errors later as a confusing
source-side SQL syntax error.
"""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException

from shared.schemas.pydantic_models import DataTargetCreate, DataTargetUpdate
from src.api import targets as targets_api
from src.api.targets import _validate_target_connection, _validate_target_type_alignment


def _model(project_id):
    return types.SimpleNamespace(id=uuid.uuid4(), project_id=project_id)


def _conn(project_id, connection_type="postgresql"):
    return types.SimpleNamespace(project_id=project_id, connection_type=connection_type)


class _DB:
    def __init__(self, model=None, conn=None):
        self._model = model
        self._conn = conn

    async def get(self, cls, _id):
        from shared.db.models import Model, ProjectConnection
        if cls is Model:
            return self._model
        if cls is ProjectConnection:
            return self._conn
        return None


async def _tenant_db(db):
    yield db


class _TargetWriteDB(_DB):
    def __init__(self, *, model, conn, target=None):
        super().__init__(model=model, conn=conn)
        self._target = target

    async def get(self, cls, _id):
        from shared.db.models import DataTarget

        if cls is DataTarget:
            return self._target
        return await super().get(cls, _id)


async def _no_lock(*_args, **_kwargs):
    return None


async def _return_model(*_args, **_kwargs):
    return _args[0]._model


@pytest.mark.asyncio
async def test_valid_connection_passes():
    pid = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=_conn(pid, "postgresql"))
    # No exception == pass.
    await _validate_target_connection(db, pid, model.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_missing_connection_rejected():
    pid = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=None)
    with pytest.raises(HTTPException) as exc:
        await _validate_target_connection(db, pid, model.id, uuid.uuid4())
    assert exc.value.status_code == 422
    assert "existing connection" in exc.value.detail


@pytest.mark.asyncio
async def test_cross_project_connection_rejected():
    pid = uuid.uuid4()
    other = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=_conn(other, "postgresql"))
    with pytest.raises(HTTPException) as exc:
        await _validate_target_connection(db, pid, model.id, uuid.uuid4())
    assert exc.value.status_code == 422
    assert "different project" in exc.value.detail


@pytest.mark.asyncio
async def test_unsupported_connector_rejected():
    pid = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=_conn(pid, "mongodb"))
    with pytest.raises(HTTPException) as exc:
        await _validate_target_connection(db, pid, model.id, uuid.uuid4())
    assert exc.value.status_code == 422
    assert "not a supported aggregate target" in exc.value.detail


@pytest.mark.asyncio
async def test_legacy_jdbc_alias_accepted():
    """jdbc normalises to hadoop_spark, a supported target."""
    pid = uuid.uuid4()
    model = _model(pid)
    db = _DB(model=model, conn=_conn(pid, "jdbc"))
    await _validate_target_connection(db, pid, model.id, uuid.uuid4())


# ---------------------------------------------------------------------------
# Bug-8790 / Bug-8452 — BigQuery cross-project DataTarget rejection
# ---------------------------------------------------------------------------

from src.api.targets import _validate_bigquery_target_project


def _bq_conn(project_id, owning_project_id=None):
    """A BigQuery ProjectConnection stub with config.project_id set."""
    return types.SimpleNamespace(
        connection_type="bigquery",
        config={"project_id": project_id} if project_id else {},
        encrypted_credentials=None,
        project_id=owning_project_id,
    )


def _bq_conn_adc():
    """A BigQuery connection stub that resolves its project via ADC (no
    project_id in config)."""
    return types.SimpleNamespace(
        connection_type="bigquery", config={}, encrypted_credentials=None,
    )


def test_bq_target_without_project_id_inherits_connection_project():
    """Target that omits project_id uses the connection's project — safe."""
    _validate_bigquery_target_project(
        _bq_conn("proj-a"), {},
    )  # No exception == pass.


def test_bq_target_project_matches_connection_project():
    """Matching projects — safe."""
    _validate_bigquery_target_project(
        _bq_conn("proj-a"), {"project_id": "proj-a"},
    )


def test_bq_target_project_differs_from_connection_project():
    """Target project != connection project — REJECTED."""
    with pytest.raises(HTTPException) as exc:
        _validate_bigquery_target_project(
            _bq_conn("proj-a"), {"project_id": "proj-b"},
        )
    assert exc.value.status_code == 422
    assert "proj-b" in exc.value.detail["message"]
    assert "proj-a" in exc.value.detail["message"]
    assert exc.value.detail["error_code"] == "bigquery_target_project_mismatch"


def test_bq_target_project_with_adc_connection_rejected():
    """Bug-8790/Bug-8814: ADC-only connections are rejected fail-closed at
    save time — the connection's project must be explicit in config or
    credentials."""
    with pytest.raises(HTTPException) as exc:
        _validate_bigquery_target_project(
            _bq_conn_adc(), {"project_id": "proj-b"},
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "bigquery_adc_project_required"


def test_non_bigquery_connection_skipped():
    """PostgreSQL connection — the BQ check is a no-op regardless of config."""
    _validate_bigquery_target_project(
        _conn(uuid.uuid4(), "postgresql"), {"project_id": "proj-b"},
    )  # No exception.


def test_bq_target_null_config_allowed():
    """Edge case: target_config is None (e.g., from a PATCH that didn't
    touch config)."""
    _validate_bigquery_target_project(
        _bq_conn("proj-a"), None,
    )  # No exception.


def test_bq_conn_null_config_rejected():
    """Bug-8790: connection with config=None resolves as ADC-only — rejected
    fail-closed at save time."""
    conn = types.SimpleNamespace(connection_type="bigquery", config=None)
    with pytest.raises(HTTPException) as exc:
        _validate_bigquery_target_project(
            conn, {"project_id": "proj-b"},
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "bigquery_adc_project_required"


def test_bq_target_empty_config_allowed():
    """Empty config dict — inherits connection project, safe."""
    _validate_bigquery_target_project(
        _bq_conn("proj-a"), {},
    )  # No exception.


@pytest.mark.parametrize(
    ("config", "field"),
    [
        ({"dataset": 7}, "dataset"),
        ({"schema": ["analytics"]}, "schema"),
        ({"project_id": {"name": "project-a"}}, "project_id"),
    ],
)
def test_bq_target_rejects_non_string_routing_identifier(config, field):
    with pytest.raises(HTTPException) as exc:
        _validate_bigquery_target_project(_bq_conn("proj-a"), config)
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "bigquery_target_config_identifier_invalid"
    assert field in exc.value.detail["message"]


def test_bq_target_rejects_unreadable_persisted_connection_credentials():
    """A decrypt failure is not an ADC-only connection and must fail closed."""
    conn = types.SimpleNamespace(
        connection_type="bigquery",
        config={},
        encrypted_credentials=b"not-a-fernet-token",
    )
    with pytest.raises(HTTPException) as exc:
        _validate_bigquery_target_project(conn, {})
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "bigquery_connection_project_invalid"


def test_bigquery_connection_rejects_postgresql_target_label_on_create():
    """Bug-8761: caller-controlled target_type cannot bypass BQ enforcement."""
    with pytest.raises(HTTPException) as exc:
        _validate_target_type_alignment(_bq_conn("proj-a"), "postgresql")
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "target_connection_type_mismatch"


def test_target_type_alignment_preserves_legacy_jdbc_alias():
    conn = _conn(uuid.uuid4(), "jdbc")
    assert _validate_target_type_alignment(conn, "hadoop_spark") == "hadoop_spark"


def test_bigquery_effective_update_rejects_mismatched_label_and_dotted_config():
    """The same effective-connection helper is used by PATCH before mutation."""
    conn = _bq_conn("proj-a")
    with pytest.raises(HTTPException) as label_exc:
        _validate_target_type_alignment(conn, "postgresql")
    assert label_exc.value.detail["error_code"] == "target_connection_type_mismatch"
    with pytest.raises(HTTPException) as config_exc:
        _validate_bigquery_target_project(conn, {"dataset": "other.dataset"})
    assert config_exc.value.detail["error_code"] == "bigquery_dotted_dataset"


@pytest.mark.asyncio
async def test_create_target_uses_connection_type_not_caller_target_type(monkeypatch):
    """Bug-8761 create path: the persisted BQ connection owns the policy."""
    pid, mid, conn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    model = _model(pid)
    model.id = mid
    db = _TargetWriteDB(model=model, conn=_bq_conn("project-a", pid))
    monkeypatch.setattr(targets_api, "get_tenant_db", lambda _tenant: _tenant_db(db))
    monkeypatch.setattr(targets_api, "ensure_model_in_project", _return_model)
    monkeypatch.setattr(targets_api, "acquire_model_definition_lock", _no_lock)

    with pytest.raises(HTTPException) as exc:
        await targets_api.create_target(
            pid,
            mid,
            DataTargetCreate(
                project_connection_id=conn_id,
                target_type="postgresql",
                display_name="unsafe",
                config={"dataset": "analytics"},
            ),
            types.SimpleNamespace(tenant_id="tenant"),
        )
    assert exc.value.detail["error_code"] == "target_connection_type_mismatch"


@pytest.mark.asyncio
async def test_update_target_revalidates_effective_connection_authority(monkeypatch):
    """Bug-8761 PATCH cannot preserve a BQ target while relabelling it PG."""
    pid, mid, target_id, conn_id = (uuid.uuid4() for _ in range(4))
    model = _model(pid)
    model.id = mid
    target = types.SimpleNamespace(
        id=target_id,
        model_id=mid,
        project_connection_id=conn_id,
        target_type="bigquery",
        config={"dataset": "analytics"},
    )
    db = _TargetWriteDB(model=model, conn=_bq_conn("project-a", pid), target=target)
    monkeypatch.setattr(targets_api, "get_tenant_db", lambda _tenant: _tenant_db(db))
    monkeypatch.setattr(targets_api, "ensure_model_in_project", _return_model)
    monkeypatch.setattr(targets_api, "acquire_model_definition_lock", _no_lock)

    with pytest.raises(HTTPException) as exc:
        await targets_api.update_target(
            pid,
            mid,
            target_id,
            DataTargetUpdate(target_type="postgresql"),
            types.SimpleNamespace(tenant_id="tenant"),
        )
    assert exc.value.detail["error_code"] == "target_connection_type_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {"dataset": 7},
        {"schema": ["analytics"]},
        {"project_id": {"name": "project-a"}},
    ],
)
async def test_create_target_rejects_non_string_bigquery_config_at_route_boundary(
    monkeypatch, config
):
    pid, mid, conn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    model = _model(pid)
    model.id = mid
    db = _TargetWriteDB(model=model, conn=_bq_conn("project-a", pid))
    monkeypatch.setattr(targets_api, "get_tenant_db", lambda _tenant: _tenant_db(db))
    monkeypatch.setattr(targets_api, "ensure_model_in_project", _return_model)
    monkeypatch.setattr(targets_api, "acquire_model_definition_lock", _no_lock)

    with pytest.raises(HTTPException) as exc:
        await targets_api.create_target(
            pid,
            mid,
            DataTargetCreate(
                project_connection_id=conn_id,
                target_type="bigquery",
                display_name="unsafe",
                config=config,
            ),
            types.SimpleNamespace(tenant_id="tenant"),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "bigquery_target_config_identifier_invalid"


@pytest.mark.asyncio
async def test_update_target_rejects_non_string_bigquery_config_at_route_boundary(monkeypatch):
    pid, mid, target_id, conn_id = (uuid.uuid4() for _ in range(4))
    model = _model(pid)
    model.id = mid
    target = types.SimpleNamespace(
        id=target_id,
        model_id=mid,
        project_connection_id=conn_id,
        target_type="bigquery",
        config={"dataset": "analytics"},
    )
    db = _TargetWriteDB(model=model, conn=_bq_conn("project-a", pid), target=target)
    monkeypatch.setattr(targets_api, "get_tenant_db", lambda _tenant: _tenant_db(db))
    monkeypatch.setattr(targets_api, "ensure_model_in_project", _return_model)
    monkeypatch.setattr(targets_api, "acquire_model_definition_lock", _no_lock)

    with pytest.raises(HTTPException) as exc:
        await targets_api.update_target(
            pid,
            mid,
            target_id,
            DataTargetUpdate(config={"dataset": ["analytics"]}),
            types.SimpleNamespace(tenant_id="tenant"),
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "bigquery_target_config_identifier_invalid"
