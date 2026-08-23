"""NQ-1 regression guard — row-security compile failure fails closed on the
Named Query surface (CRITICAL RLS bypass).

``_handle_named_query_reference`` used to swallow ``RowSecurityCompileError``
into ``_compiled_rls = None``. ``_rls_active`` then computed ``False``, every
materialised-vs-live security branch was skipped, and the decision fell through
to ``serve_materialised = True`` — the handler then served the ENTIRE
unfiltered physical result table (``SELECT *`` with no injected predicate and
``security_compiled=None`` at the generation guard). Every other surface
(/execute, /explain, /discover/members, $KPIs) already fails closed on a
compile error; the Named Query surface was the only one that fell through to
serving data. Reachable via a persisted row-security rule that fails
compilation (model import, a direct DB edit, or model drift).

Test escape: no test exercised the Named Query serve path with a
``RowSecurityCompileError`` raised from ``compile_row_security``.
Guard: this file. Tier: T2 (CRITICAL security regression).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from src.api import routes as _routes
from src.api.routes import ExecuteRequest
from src.routing.named_query_resolver import NamedQueryDefinition
from src.security import Principal, RowSecurityCompileError

pytestmark = pytest.mark.unit

_MODEL_ID = uuid.uuid4()
_VERSION = uuid.uuid4()
_PROJECT_ID = uuid.uuid4()
_NQ_ID = uuid.uuid4()
_ARTIFACT_ID = uuid.uuid4()
_TARGET_ID = uuid.uuid4()


def _definition() -> NamedQueryDefinition:
    return NamedQueryDefinition(
        id=str(_NQ_ID),
        name="leads",
        definition_sql="SELECT * FROM modely",
        output_columns=[{"name": "*", "type": "string"}],
        shape="projection",
    )


def _model_row() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=_MODEL_ID,
        slug="acme",
        deployed_version_id=str(_VERSION),
        deploy_epoch=7,
        project_id=_PROJECT_ID,
    )


def _artifact_row() -> types.SimpleNamespace:
    """A fresh artifact that passes BOTH the version gate and the overdue
    gate, so without the NQ-1 fix the handler WOULD serve it materialised."""
    return types.SimpleNamespace(
        id=_ARTIFACT_ID,
        named_query_id=str(_NQ_ID),
        status="fresh",
        built_for_version_id=str(_VERSION),
        built_for_epoch=7,
        last_refresh_at=datetime.now(timezone.utc),
        row_manifest={"columns": [{"logical_name": "branch_id"}]},
        active_refresh_run_id="run-1",
        physical_table_name="nq_acme_abc123",
        target_schema="public",
        target_id=str(_TARGET_ID),
    )


def _policy_row() -> types.SimpleNamespace:
    # No cron, disabled policy -> never overdue.
    return types.SimpleNamespace(cron_expression=None, is_enabled=False)


def _target_row() -> types.SimpleNamespace:
    return types.SimpleNamespace(id=_TARGET_ID, model_id=_MODEL_ID)


class _ScalarOne:
    """A fake ``db.execute`` result whose ``scalar_one_or_none`` returns ``row``.

    Bug-8924 antipattern honoured: this is NOT a fake ``_ScalarResult`` and its
    ``scalars()`` never returns ``self`` — it returns a separate result object
    (no ``scalars()`` at all here), so call-order dispatch stays honest.
    """

    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object:
        return self._row


def _make_db() -> MagicMock:
    db = MagicMock()
    model = _model_row()
    target = _target_row()

    db.get = AsyncMock(
        side_effect=lambda cls, _id: {
            "Model": model,
            "DataTarget": target,
        }.get(cls.__name__)
    )

    # Execution order in the handler: (1) NamedQueryArtifact select,
    # (2) NamedQueryRefreshPolicy select, (3) observation Model select — the
    # third is reached only on the (pre-fix) materialised serving path.
    results = [
        _ScalarOne(_artifact_row()),
        _ScalarOne(_policy_row()),
        _ScalarOne(model),
    ]
    calls: list[object] = []

    async def _execute(stmt: object) -> object:
        calls.append(stmt)
        if len(calls) > len(results):
            return _ScalarOne(None)
        return results[len(calls) - 1]

    db.execute = AsyncMock(side_effect=_execute)
    db.execute_calls = calls
    return db


async def test_nq1_rls_compile_error_fails_closed_422_never_serves_materialised() -> None:
    """NQ-1: a row-security rule that fails to compile must raise the canonical
    422 (``row_security_misconfigured`` detail) and must NEVER fall through to
    serving the unfiltered materialised table."""
    db = _make_db()
    principal = Principal(
        user_identity="analyst@acme-demo.com",
        roles=frozenset({"analyst"}),
    )
    body = ExecuteRequest(
        model_id=str(_MODEL_ID),
        raw_query="SELECT * FROM @leads",
    )
    logical_query = types.SimpleNamespace(limit=None)
    conn = types.SimpleNamespace(connection_type="postgresql")

    exec_mock = AsyncMock(
        return_value=([{"branch_id": "every-row"}], 5, ["branch_id"])
    )
    compile_error = RowSecurityCompileError(
        "row-security mapping table UUID(...) not found"
    )

    with (
        patch.object(
            _routes, "load_named_queries",
            new=AsyncMock(return_value={"@leads": _definition()}),
        ),
        patch.object(
            _routes, "compile_row_security",
            new=AsyncMock(side_effect=compile_error),
        ),
        patch(
            "shared.aggregate_connection.resolve_source_connection",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
        patch(
            "shared.source_executor.resolve_connector_type",
            new=AsyncMock(return_value="postgresql"),
        ),
        patch.object(
            _routes, "resolve_endpoint_connection",
            new=AsyncMock(return_value=conn),
        ),
        patch.object(_routes, "execute_on_connection", new=exec_mock),
        patch.object(_routes, "record_query_success", new=AsyncMock()),
        patch(
            "src.routing.named_query_generation_guard.assert_named_query_route_admissible",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
        patch(
            "src.routing.named_query_generation_guard.assert_named_query_generation_unchanged",
            new=AsyncMock(),
        ),
        patch(
            "src.routing.named_query_generation_guard.read_named_query_generation",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await _routes._handle_named_query_reference(
                db,
                body,
                logical_query,
                ref_name="leads",
                persona=None,
                principal=principal,
                user_identity=principal.user_identity,
                tenant_id="acme-demo",
            )

    # The same typed 422 every other surface raises.
    assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = excinfo.value.detail
    assert detail["error_type"] == "row_security_misconfigured"
    assert "fail closed" in detail["message"]
    # Zero materialised rows: the physical result table is never read.
    exec_mock.assert_not_awaited()


class _NoRows:
    """A ``db.execute`` result for the PersonaTagRestriction probe."""

    def first(self) -> object | None:
        return None


def _make_db_with_persona() -> MagicMock:
    db = MagicMock()
    model = _model_row()
    target = _target_row()
    db.get = AsyncMock(
        side_effect=lambda cls, _id: {
            "Model": model, "DataTarget": target,
        }.get(cls.__name__)
    )
    # With a persona present the order is: artifact, policy,
    # PersonaTagRestriction, observation Model — the last two are reached
    # only on the (pre-fix) materialised path, past the raise.
    results = [
        _ScalarOne(_artifact_row()), _ScalarOne(_policy_row()),
        _NoRows(), _ScalarOne(model),
    ]
    calls: list[object] = []

    async def _execute(stmt: object) -> object:
        calls.append(stmt)
        if len(calls) > len(results):
            return _ScalarOne(None)
        return results[len(calls) - 1]

    db.execute = AsyncMock(side_effect=_execute)
    return db


async def test_nq1_compile_error_fails_closed_even_for_a_bypass_row_security_persona() -> None:
    """NQ-1, carve-out drift: ``bypass_row_security`` authorises skipping a KNOWN
    policy; it is never a licence to serve when the policy is UNKNOWN.

    No sibling surface carves the bypass out of its compile-error path ($KPIs
    withholds unconditionally at routes.py:4597; ``route_query`` raises at
    router.py:477 before ``rls_bypass`` is read at 479). If a later change moves
    the NQ raise behind ``if not _rls_bypass``, a bypass persona would again be
    served the whole unfiltered materialised table on a broken rule.
    """
    db = _make_db_with_persona()
    principal = Principal(
        user_identity="exec@acme-demo.com", roles=frozenset({"executive"}),
    )
    persona = types.SimpleNamespace(
        id=uuid.uuid4(), bypass_row_security=True, default_filters={},
    )
    body = ExecuteRequest(model_id=str(_MODEL_ID), raw_query="SELECT * FROM @leads")
    exec_mock = AsyncMock(return_value=([{"branch_id": "every-row"}], 5, ["branch_id"]))

    with (
        patch.object(
            _routes, "load_named_queries",
            new=AsyncMock(return_value={"@leads": _definition()}),
        ),
        patch.object(
            _routes, "compile_row_security",
            new=AsyncMock(side_effect=RowSecurityCompileError("boom")),
        ),
        patch(
            "shared.aggregate_connection.resolve_source_connection",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
        patch(
            "shared.source_executor.resolve_connector_type",
            new=AsyncMock(return_value="postgresql"),
        ),
        patch.object(
            _routes, "resolve_endpoint_connection",
            new=AsyncMock(
                return_value=types.SimpleNamespace(connection_type="postgresql")
            ),
        ),
        patch.object(_routes, "execute_on_connection", new=exec_mock),
        patch.object(_routes, "record_query_success", new=AsyncMock()),
        patch(
            "src.routing.named_query_generation_guard.assert_named_query_route_admissible",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
        patch(
            "src.routing.named_query_generation_guard.assert_named_query_generation_unchanged",
            new=AsyncMock(),
        ),
        patch(
            "src.routing.named_query_generation_guard.read_named_query_generation",
            new=AsyncMock(return_value=types.SimpleNamespace()),
        ),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await _routes._handle_named_query_reference(
                db, body, types.SimpleNamespace(limit=None),
                ref_name="leads", persona=persona, principal=principal,
                user_identity=principal.user_identity, tenant_id="acme-demo",
            )

    assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert excinfo.value.detail["error_type"] == "row_security_misconfigured"
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_f007_22_nq_connector_resolve_failure_is_422_not_postgres_guess() -> None:
    """F-007-22: Named Query RLS must not fall back to quoting as postgresql
    when the source connector cannot be resolved. Guard: this test.
    """
    db = _make_db()
    principal = Principal(
        user_identity="analyst@acme-demo.com",
        roles=frozenset({"analyst"}),
    )
    body = ExecuteRequest(
        model_id=str(_MODEL_ID),
        raw_query="SELECT * FROM @leads",
    )
    compile_mock = AsyncMock()

    with (
        patch.object(
            _routes, "load_named_queries",
            new=AsyncMock(return_value={"@leads": _definition()}),
        ),
        patch.object(_routes, "compile_row_security", new=compile_mock),
        patch(
            "shared.aggregate_connection.resolve_source_connection",
            new=AsyncMock(side_effect=RuntimeError("source missing")),
        ),
        patch.object(_routes, "execute_on_connection", new=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await _routes._handle_named_query_reference(
                db,
                body,
                types.SimpleNamespace(limit=None),
                ref_name="leads",
                persona=None,
                principal=principal,
                user_identity=principal.user_identity,
                tenant_id="acme-demo",
            )

    assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert excinfo.value.detail["error_type"] == "row_security_misconfigured"
    compile_mock.assert_not_awaited()
