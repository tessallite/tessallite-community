"""ML6 row-level-security fail-closed fixes (unit 007).

Covers the model-service halves of the ML6 batch:

* F-007-04 — the row-security DSL is compiled at save time, so a malformed
  predicate is rejected with a 422 here instead of 500ing every matched
  caller at query time.
* F-007-07 — the simulate preview resolves the model's real source
  connector so the previewed predicate is byte-identical to the runtime
  predicate (BigQuery backticks vs PostgreSQL double-quotes).

The router-side halves (F-007-04 typed catch, F-007-11 simulate headers)
live in the query-router test suite; the schema half of F-007-08 lives in
``test_row_security_schema.py``.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.row_security import (
    _resolve_model_connector,
    _validate_predicate_compiles,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# F-007-04 — save-time DSL validation
# ---------------------------------------------------------------------------


class TestSaveTimeDslValidation:
    def test_valid_predicate_passes(self):
        # A well-formed predicate compiles cleanly — no exception.
        _validate_predicate_compiles(
            "dimension_equals('region.region_code', 'NORTH')"
        )

    def test_valid_nested_predicate_passes(self):
        _validate_predicate_compiles(
            "and(in('region.code','N','S'), not(dimension_equals('region.code','X')))"
        )

    def test_none_predicate_is_noop(self):
        # user_mapping rules carry no predicate; None must not raise.
        _validate_predicate_compiles(None)

    def test_empty_predicate_is_noop(self):
        _validate_predicate_compiles("")

    def test_malformed_predicate_rejected_422(self):
        # F-007-04 fail closed: garbage must be rejected at save with a 422,
        # not stored to 500 every matched caller later.
        with pytest.raises(HTTPException) as exc:
            _validate_predicate_compiles("garbage(")
        assert exc.value.status_code == 422

    def test_unknown_function_rejected_422(self):
        with pytest.raises(HTTPException) as exc:
            _validate_predicate_compiles("drop_table('x')")
        assert exc.value.status_code == 422

    def test_unquoted_value_rejected_422(self):
        # Values must be single-quoted string literals; a bare token is a
        # configuration error, caught at save.
        with pytest.raises(HTTPException) as exc:
            _validate_predicate_compiles("dimension_equals('region.code', NORTH)")
        assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# F-007-07 — simulate preview compiles with the model's real connector
# ---------------------------------------------------------------------------


def _fake_db(*, source, connection):
    """A minimal async DB stub: execute() returns the source row, get()
    returns the connection."""
    db = types.SimpleNamespace()

    class _Result:
        def scalar_one_or_none(self_inner):
            return source

    db.execute = AsyncMock(return_value=_Result())
    db.get = AsyncMock(return_value=connection)
    return db


class TestSimulateConnectorResolution:
    @pytest.mark.asyncio
    async def test_resolves_bigquery_connector(self):
        source = types.SimpleNamespace(project_connection_id=uuid.uuid4())
        conn = types.SimpleNamespace(connection_type="BigQuery")
        db = _fake_db(source=source, connection=conn)
        connector = await _resolve_model_connector(db, uuid.uuid4())
        assert connector == "bigquery"

    @pytest.mark.asyncio
    async def test_resolves_postgres_connector(self):
        source = types.SimpleNamespace(project_connection_id=uuid.uuid4())
        conn = types.SimpleNamespace(connection_type="postgresql")
        db = _fake_db(source=source, connection=conn)
        connector = await _resolve_model_connector(db, uuid.uuid4())
        assert connector == "postgresql"

    @pytest.mark.asyncio
    async def test_no_source_falls_back_to_postgresql(self):
        # A freshly-created model with no source still previews — default to
        # the compiler's own default rather than erroring.
        db = _fake_db(source=None, connection=None)
        connector = await _resolve_model_connector(db, uuid.uuid4())
        assert connector == "postgresql"

    @pytest.mark.asyncio
    async def test_missing_connection_falls_back_to_postgresql(self):
        source = types.SimpleNamespace(project_connection_id=uuid.uuid4())
        db = _fake_db(source=source, connection=None)
        connector = await _resolve_model_connector(db, uuid.uuid4())
        assert connector == "postgresql"
