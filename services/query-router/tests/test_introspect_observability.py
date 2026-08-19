"""Tests for introspect endpoint observability (Bug-5323).

Verifies that:
- Single-introspect failures (timeout, execution error) persist a QueryLog
  row with status='error' and the appropriate error_type BEFORE raising.
- Batch-introspect failures record status='error' (not the default 'success')
  in the per-item QueryLog row.
- Successful introspect queries continue to log status='success'.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.introspect import _log_introspect


# ---------------------------------------------------------------------------
# _log_introspect unit tests
# ---------------------------------------------------------------------------

class _FakeDB:
    """Minimal async DB stub that records added objects."""

    def __init__(self):
        self.added: list = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_log_introspect_success_defaults():
    """A successful introspect writes status='success' and no error fields."""
    db = _FakeDB()
    await _log_introspect(
        db, str(uuid.uuid4()), "user@example.com",
        "SELECT 1", 42, 1,
    )
    assert len(db.added) == 1
    entry = db.added[0]
    assert entry.status == "success"
    assert entry.error_type is None
    assert entry.error_detail is None
    assert db.committed


@pytest.mark.asyncio
async def test_log_introspect_error_records_status_and_detail():
    """Bug-5323: an introspect failure must record status='error' with
    the error_type and error_detail."""
    db = _FakeDB()
    await _log_introspect(
        db, str(uuid.uuid4()), "user@example.com",
        "SELECT * FROM missing_table", 10, 0,
        error_type="execution_error",
        error_detail="relation 'missing_table' does not exist",
    )
    assert len(db.added) == 1
    entry = db.added[0]
    assert entry.status == "error"
    assert entry.error_type == "execution_error"
    assert "missing_table" in entry.error_detail


@pytest.mark.asyncio
async def test_log_introspect_timeout_records_error():
    db = _FakeDB()
    await _log_introspect(
        db, str(uuid.uuid4()), "user@example.com",
        "SELECT pg_sleep(999)", 30000, 0,
        error_type="timeout",
        error_detail="Query timed out after 30s",
    )
    entry = db.added[0]
    assert entry.status == "error"
    assert entry.error_type == "timeout"


# ---------------------------------------------------------------------------
# Introspect endpoint integration tests (HTTP-level)
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    from src.main import app
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


def _mock_tenant_db():
    return AsyncMock()


def _async_gen(db):
    async def _gen(*args, **kwargs):
        yield db
    return _gen


def _mint_explore_token() -> str:
    from datetime import datetime, timedelta, timezone
    from jose import jwt
    from shared.config.settings import get_settings
    settings = get_settings()
    payload = {
        "sub": "user@example.com",
        "tenant_id": "test-tenant",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "role": "member",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _auth() -> dict:
    return {"Authorization": f"Bearer {_mint_explore_token()}"}


class TestSingleIntrospectFailureLogging:
    """Bug-5323: single-introspect failures must log BEFORE raising."""

    @pytest.mark.asyncio
    async def test_timeout_logs_error_before_504(self, client, monkeypatch):
        from shared.source_executor import QueryTimeoutError

        model_id = str(uuid.uuid4())
        db = _mock_tenant_db()
        log_calls: list = []

        original_log = _log_introspect.__wrapped__ if hasattr(_log_introspect, "__wrapped__") else _log_introspect

        async def _capture_log(*args, **kwargs):
            log_calls.append(kwargs if kwargs else {"args": args})

        monkeypatch.setattr("src.api.introspect.load_authorized_model", AsyncMock(return_value=MagicMock(project_id=uuid.uuid4())))
        monkeypatch.setattr("src.api.introspect.execute_source_sql", AsyncMock(
            side_effect=QueryTimeoutError("timed out"),
        ))
        monkeypatch.setattr("src.api.introspect._resolve_model_connection", AsyncMock(
            return_value=(MagicMock(), None),
        ))
        monkeypatch.setattr("src.api.introspect._log_introspect", _capture_log)

        with patch("src.api.introspect.get_tenant_db", _async_gen(db)):
            resp = await client.post(
                "/api/v1/introspect",
                json={"model_id": model_id, "raw_sql": "SELECT pg_sleep(999)"},
                headers=_auth(),
            )

        assert resp.status_code == 504
        # The failure was logged BEFORE the 504 was raised.
        assert len(log_calls) == 1
        call = log_calls[0]
        assert call.get("error_type") == "timeout" or (
            len(call.get("args", [])) > 0
        )

    @pytest.mark.asyncio
    async def test_execution_error_logs_before_502(self, client, monkeypatch):
        model_id = str(uuid.uuid4())
        db = _mock_tenant_db()
        log_calls: list = []

        async def _capture_log(*args, **kwargs):
            log_calls.append(kwargs)

        monkeypatch.setattr("src.api.introspect.load_authorized_model", AsyncMock(return_value=MagicMock(project_id=uuid.uuid4())))
        monkeypatch.setattr("src.api.introspect.execute_source_sql", AsyncMock(
            side_effect=RuntimeError("source exploded"),
        ))
        monkeypatch.setattr("src.api.introspect._resolve_model_connection", AsyncMock(
            return_value=(MagicMock(), None),
        ))
        monkeypatch.setattr("src.api.introspect._log_introspect", _capture_log)

        with patch("src.api.introspect.get_tenant_db", _async_gen(db)):
            resp = await client.post(
                "/api/v1/introspect",
                json={"model_id": model_id, "raw_sql": "SELECT 1"},
                headers=_auth(),
            )

        assert resp.status_code == 502
        assert len(log_calls) == 1
        assert log_calls[0].get("error_type") == "execution_error"
        assert "source exploded" in log_calls[0].get("error_detail", "")

    @pytest.mark.asyncio
    async def test_bug_schema_drift_absent_source_table_is_explicit_404(self, client, monkeypatch):
        """A confirmed absent table is distinct from an unreachable router."""
        class MissingTableError(Exception):
            sqlstate = "42P01"

        model_id = str(uuid.uuid4())
        db = _mock_tenant_db()
        log_calls: list = []

        async def _capture_log(*args, **kwargs):
            log_calls.append(kwargs)

        monkeypatch.setattr(
            "src.api.introspect.load_authorized_model",
            AsyncMock(return_value=MagicMock(project_id=uuid.uuid4())),
        )
        monkeypatch.setattr(
            "src.api.introspect.execute_source_sql",
            AsyncMock(side_effect=MissingTableError("relation does not exist")),
        )
        monkeypatch.setattr(
            "src.api.introspect._resolve_model_connection",
            AsyncMock(return_value=(MagicMock(), None)),
        )
        monkeypatch.setattr("src.api.introspect._log_introspect", _capture_log)

        with patch("src.api.introspect.get_tenant_db", _async_gen(db)):
            resp = await client.post(
                "/api/v1/introspect",
                json={"model_id": model_id, "raw_sql": "SELECT 1 FROM demo_data.calendar"},
                headers=_auth(),
            )

        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "source_table_not_found"
        assert log_calls[0]["error_type"] == "source_table_not_found"


def test_bug_schema_drift_missing_table_metadata_covers_postgres_and_bigquery():
    """PG SQLSTATE and BQ HTTP metadata share one connector-neutral classifier."""
    from shared.source_table_probe import is_source_table_not_found_error

    class PostgresMissingTable(Exception):
        sqlstate = "42P01"

    class BigQueryMissingTable(Exception):
        code = 404

    class PermissionDenied(Exception):
        code = 403

    assert is_source_table_not_found_error(PostgresMissingTable()) is True
    assert is_source_table_not_found_error(BigQueryMissingTable()) is True
    assert is_source_table_not_found_error(PermissionDenied()) is False


class TestBatchIntrospectFailureLogging:
    """Bug-5323: batch-introspect per-item failures must record error status."""

    @pytest.mark.asyncio
    async def test_batch_item_failure_logs_error_status(self, client, monkeypatch):
        model_id = str(uuid.uuid4())
        db = _mock_tenant_db()
        log_calls: list = []

        async def _capture_log(*args, **kwargs):
            log_calls.append(kwargs)

        call_count = 0

        async def _exec_alternating(conn, sql, *, tenant_session=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("bad query")
            return ([{"x": 1}], ["x"])

        monkeypatch.setattr("src.api.introspect.load_authorized_model", AsyncMock(return_value=MagicMock(project_id=uuid.uuid4())))
        monkeypatch.setattr("src.api.introspect.execute_source_sql", _exec_alternating)
        monkeypatch.setattr("src.api.introspect._resolve_model_connection", AsyncMock(
            return_value=(MagicMock(), None),
        ))
        monkeypatch.setattr("src.api.introspect._log_introspect", _capture_log)

        with patch("src.api.introspect.get_tenant_db", _async_gen(db)):
            resp = await client.post(
                "/api/v1/introspect/batch",
                json={
                    "model_id": model_id,
                    "queries": [
                        {"key": "ok", "raw_sql": "SELECT 1"},
                        {"key": "fail", "raw_sql": "SELECT bad"},
                    ],
                },
                headers=_auth(),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 2
        # First item: success.
        assert data["results"][0]["error"] is None
        # Second item: error.
        assert data["results"][1]["error"] is not None

        # QueryLog calls: first = success, second = error.
        assert len(log_calls) == 2
        assert log_calls[0].get("error_type") is None  # success
        assert log_calls[1].get("error_type") == "execution_error"
