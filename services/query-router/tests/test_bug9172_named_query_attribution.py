"""Bug-9172 regression guards for the existing QueryLog observation seam.

The Named Query handler must attach identity/reason only at its own serving
boundary. Ordinary success/failure writers remain backwards-compatible and
persist nullable attribution as NULL.

Test escape: QueryLog already recorded timing/bytes, but no contract test
asserted that materialised and canonical live Named Query calls carried their
identity into the shared writer. Guard: this module. Tier: T2.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.api import routes

pytestmark = pytest.mark.unit


def _bound_and_decision(*, route_type: str = "named_query"):
    model = types.SimpleNamespace(
        id=uuid.uuid4(), display_name="sales", project=None,
    )
    bound = types.SimpleNamespace(
        model=model,
        logical_query=types.SimpleNamespace(
            protocol="jdbc", raw_query="SELECT 1", query_fingerprint="f" * 64,
        ),
    )
    decision = types.SimpleNamespace(
        route_type=route_type,
        aggregate_id=None,
        pocket_id=None,
        rewritten_query="SELECT 1",
        reason="source",
        aggregate_skipped_reasons=None,
    )
    return bound, decision


@pytest.mark.asyncio
async def test_materialized_named_query_success_is_attributed():
    """A materialised NQ success keeps the existing cost row and adds NQ id."""
    bound, decision = _bound_and_decision()
    named_query_id = uuid.uuid4()
    log_query = AsyncMock()
    with patch.object(routes, "log_query", log_query), patch.object(
        routes, "audit", new=AsyncMock()
    ):
        await routes.record_query_success(
            AsyncMock(),
            bound=bound,
            decision=decision,
            elapsed_ms=23,
            rows_returned=4,
            bytes_processed=512,
            user_identity="user@example.com",
            tenant_id="tenant-a",
            log_miss=False,
            named_query_id=named_query_id,
            named_query_fallback_reason=None,
        )

    assert log_query.await_args.kwargs["named_query_id"] == named_query_id
    assert log_query.await_args.kwargs["named_query_fallback_reason"] is None
    assert log_query.await_args.kwargs["bytes_processed"] == 512


@pytest.mark.asyncio
async def test_live_named_query_fallback_passes_id_and_reason_to_inner_pipeline():
    """The canonical source fallback stamps its reason before recursion."""
    named_query_id = uuid.uuid4()
    body = routes.ExecuteRequest(
        model_id=str(uuid.uuid4()),
        raw_query="SELECT * FROM @sales",
        protocol="jdbc",
    )
    response = types.SimpleNamespace(route_type="source", reason="source")
    inner_execute = AsyncMock(return_value=response)
    nq = types.SimpleNamespace(id=str(named_query_id), name="sales")

    with patch.object(routes, "_handle_execute", inner_execute):
        result = await routes._execute_named_query_live(
            AsyncMock(),
            body,
            nq,
            "SELECT * FROM sales",
            persona=None,
            principal=None,
            user_identity="user@example.com",
            tenant_id="tenant-a",
            skip_reason="artifact_not_fresh",
            reference=None,
            server_row_cap=None,
        )

    assert result is response
    assert inner_execute.await_args.kwargs["named_query_id"] == named_query_id
    assert (
        inner_execute.await_args.kwargs["named_query_fallback_reason"]
        == "artifact_not_fresh"
    )


@pytest.mark.asyncio
async def test_live_named_query_preexec_failure_keeps_attribution():
    """A failure before binding still writes the attributed QueryLog row."""
    named_query_id = uuid.uuid4()
    body = routes.ExecuteRequest(
        model_id=str(uuid.uuid4()),
        raw_query="SELECT * FROM @sales",
        protocol="jdbc",
    )
    failure = routes.HTTPException(status_code=422, detail="invalid definition")
    inner_execute = AsyncMock(side_effect=failure)
    preexec = AsyncMock()
    nq = types.SimpleNamespace(id=str(named_query_id), name="sales")

    with patch.object(routes, "_handle_execute", inner_execute), patch.object(
        routes, "_log_preexec_failure", preexec
    ):
        with pytest.raises(routes.HTTPException) as raised:
            await routes._execute_named_query_live(
                AsyncMock(),
                body,
                nq,
                "SELECT * FROM sales",
                persona=None,
                principal=None,
                user_identity="user@example.com",
                tenant_id="tenant-a",
                skip_reason="artifact_not_fresh",
                reference=None,
                server_row_cap=None,
            )

    assert raised.value is failure
    assert preexec.await_args.kwargs["named_query_id"] == named_query_id
    assert (
        preexec.await_args.kwargs["named_query_fallback_reason"]
        == "artifact_not_fresh"
    )


@pytest.mark.asyncio
async def test_ordinary_query_success_keeps_nullable_nq_fields_empty():
    """Non-NQ callers do not acquire synthetic attribution."""
    bound, decision = _bound_and_decision(route_type="source")
    log_query = AsyncMock()
    with patch.object(routes, "log_query", log_query), patch.object(
        routes, "audit", new=AsyncMock()
    ):
        await routes.record_query_success(
            AsyncMock(),
            bound=bound,
            decision=decision,
            elapsed_ms=4,
            rows_returned=1,
            bytes_processed=32,
            user_identity="user@example.com",
            tenant_id="tenant-a",
            log_miss=False,
        )

    assert log_query.await_args.kwargs["named_query_id"] is None
    assert log_query.await_args.kwargs["named_query_fallback_reason"] is None


@pytest.mark.asyncio
async def test_named_query_source_fallback_does_not_emit_actionable_miss():
    """NQ fallback observes QueryLog only; the source decision suppresses misses."""
    bound, decision = _bound_and_decision(route_type="source")
    decision.log_miss = False
    named_query_id = uuid.uuid4()
    log_query_miss = AsyncMock()
    with patch.object(routes, "log_query", new=AsyncMock()), patch.object(
        routes, "log_query_miss", log_query_miss
    ), patch.object(routes, "audit", new=AsyncMock()):
        await routes.record_query_success(
            AsyncMock(),
            bound=bound,
            decision=decision,
            elapsed_ms=20,
            rows_returned=2,
            bytes_processed=256,
            user_identity="user@example.com",
            tenant_id="tenant-a",
            log_miss=decision.log_miss,
            named_query_id=named_query_id,
            named_query_fallback_reason="artifact_not_fresh",
        )

    log_query_miss.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_query_failure_persists_nullable_nq_fields():
    """The shared failure writer remains valid without NQ context."""
    from src.logging.query_logger import log_query_failure

    class _DB:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

        async def commit(self):
            return None

    db = _DB()
    row = await log_query_failure(
        db,
        bound_query=None,
        decision=None,
        execution_ms=1,
        user_identity="user@example.com",
        error_type="binding_error",
        error_detail="bad query",
    )

    assert row.named_query_id is None
    assert row.named_query_fallback_reason is None
