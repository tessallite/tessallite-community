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
from .result_fakes import FakeScalarResult
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
    """A minimal async DB stub for the connector-resolution path.

    Bug-7035: ``_resolve_model_connector`` now first queries the model's
    enabled row-security rules (to resolve the protected-dimension source),
    then falls back to the model's primary source. These tests exercise the
    fallback with no RLS rules configured, so the rule query returns ``[]``
    (via ``.all()``) and the primary-source query returns ``source`` (via
    ``.scalar_one_or_none()``) — preserving the original F-007-07 intent that
    the source's connector wins and missing source/connection falls back to
    ``postgresql``.
    """
    db = types.SimpleNamespace()

    class _RuleResult:
        def all(self_inner):
            return []  # no enabled RLS rules -> no protected-dimension source

    class _SourceResult:
        def scalar_one_or_none(self_inner):
            return source

        def scalars(self_inner):
            return FakeScalarResult([source] if source is not None else [])

        def all(self_inner):
            return [source] if source is not None else []

    async def _execute(stmt):
        if "row_security_rules" in str(stmt).lower():
            return _RuleResult()
        return _SourceResult()

    db.execute = _execute
    db.get = AsyncMock(return_value=connection)
    return db


class TestSimulateConnectorResolution:
    # Bug-8904: ``_resolve_model_connector`` now returns ``(connector, note)``.
    # ``note`` is the warning surfaced to the modeller as
    # ``RowSecuritySimulateResponse.connector_note`` when the resolution was not
    # definitive. Every case in this class configures NO enabled RLS rules, so
    # there is no protected dimension and nothing to compile a predicate for —
    # the note must stay None rather than warning about the dialect of a preview
    # that is empty by construction.
    @pytest.mark.asyncio
    async def test_resolves_bigquery_connector(self):
        source = types.SimpleNamespace(project_connection_id=uuid.uuid4())
        conn = types.SimpleNamespace(connection_type="BigQuery")
        db = _fake_db(source=source, connection=conn)
        connector, note = await _resolve_model_connector(db, uuid.uuid4())
        assert connector == "bigquery"
        # Bug-7027/Bug-8904: resolved definitively, so no fallback caveat.
        assert note is None

    @pytest.mark.asyncio
    async def test_resolves_postgres_connector(self):
        source = types.SimpleNamespace(project_connection_id=uuid.uuid4())
        conn = types.SimpleNamespace(connection_type="postgresql")
        db = _fake_db(source=source, connection=conn)
        connector, note = await _resolve_model_connector(db, uuid.uuid4())
        assert connector == "postgresql"
        assert note is None

    @pytest.mark.asyncio
    async def test_no_source_falls_back_to_postgresql(self):
        # A freshly-created model with no source still previews — default to
        # the compiler's own default rather than erroring.
        db = _fake_db(source=None, connection=None)
        connector, note = await _resolve_model_connector(db, uuid.uuid4())
        assert connector == "postgresql"
        # Bug-7027/Bug-8904: no enabled rules means no predicate is compiled at
        # all, so there is no quoting for a caveat to be about. The default is
        # benign here and must stay silent.
        assert note is None

    @pytest.mark.asyncio
    async def test_missing_connection_falls_back_to_postgresql(self):
        source = types.SimpleNamespace(project_connection_id=uuid.uuid4())
        db = _fake_db(source=source, connection=None)
        connector, note = await _resolve_model_connector(db, uuid.uuid4())
        assert connector == "postgresql"
        # Bug-8904: the source's connection is unreachable, but this fixture has
        # NO enabled rules, so the preview is empty by construction and the
        # caveat is suppressed. The same unreachable-source state WITH enabled
        # rules does disclose — pinned by
        # test_resolve_model_connector_warns_when_source_connector_unresolvable_bug7027
        # in test_row_security_api.py.
        assert note is None
