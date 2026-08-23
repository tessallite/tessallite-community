"""
B2: queries against a model whose ``deployed_version_id`` is None return
HTTP 409 Conflict, not 422.

F-2 says an undeployed model is metadata-only. A BI tool hitting an
undeployed target should see a distinct signal from a malformed query, so
the gateway can surface "model not deployed" without treating it like a
bind failure.

Three things tested here:
  1. The binder raises ``ModelNotDeployedError`` (subclass of
     ``SemanticBindingError``) when deployed_version_id is None.
  2. ``_handle_execute`` maps ModelNotDeployedError → 409.
  3. ``_handle_explain`` maps ModelNotDeployedError → 409.

``_handle_validate`` intentionally keeps the existing behaviour — it
returns a 200 with errors in the body — because its contract is ok/errors
pairs, not HTTP status codes.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

# Collection-order landmine: test_demo_data_integration.py and
# test_query_trace.py install empty-stub ``shared.db.session`` /
# ``shared.schemas.*`` modules into ``sys.modules`` at import time so
# their own pipeline mocks work. Once a stub module lands in
# ``sys.modules`` without a ``__file__``, Python's import machinery
# cannot re-resolve the real module from disk — so any later test that
# chains through ``src.api.routes`` (which itself does
# ``from shared.db.session import get_tenant_db``) fails collection.
#
# Defer the ``src.api.routes`` / ``src.semantic.binder`` imports into
# the test bodies and skip this file in a polluted process, matching
# the pattern in ``test_query_identity_logging.py``. Running this file
# alone still exercises every assertion.
_SHARED_DB_SESSION = sys.modules.get("shared.db.session")
_POLLUTED = _SHARED_DB_SESSION is not None and getattr(
    _SHARED_DB_SESSION, "__file__", None
) is None

pytestmark = pytest.mark.skipif(
    _POLLUTED,
    reason="sys.modules polluted by another test; run this file alone",
)

from src.ir.logical_query import (
    LogicalQuery,
    ModelNotDeployedError,
    SemanticBindingError,
)


def _logical_query() -> LogicalQuery:
    return LogicalQuery(
        model_id="model-1",
        protocol="jdbc",
        raw_query="SELECT 1",
        requested_measures=[],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp",
    )


# ---------------------------------------------------------------------------
# Binder raises ModelNotDeployedError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_binder_raises_model_not_deployed_for_undeployed_model():
    from src.semantic.binder import bind_query_to_model

    model = types.SimpleNamespace(id="model-1", slug="m", deployed_version_id=None)
    db = AsyncMock()

    with patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)):
        with pytest.raises(ModelNotDeployedError) as exc:
            await bind_query_to_model(_logical_query(), db)

    # Subclass relationship preserved so legacy ``except SemanticBindingError``
    # handlers keep working.
    assert isinstance(exc.value, SemanticBindingError)
    assert "not deployed" in str(exc.value)


@pytest.mark.asyncio
async def test_binder_returns_normally_when_model_is_deployed():
    from src.semantic.binder import bind_query_to_model

    from src.semantic.snapshot_resolver import DeployedShape

    model = types.SimpleNamespace(id="model-1", slug="m", deployed_version_id="v1")
    shape = DeployedShape(
        measures=[], dimensions=[], hidden_column_ids=set(),
        physical_columns_all=set(), physical_columns_visible=set(),
        hierarchy_rows=[],
    )
    db = AsyncMock()

    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape",
              new=AsyncMock(return_value=shape)),
    ):
        bound = await bind_query_to_model(_logical_query(), db)

    assert bound.model is model


# ---------------------------------------------------------------------------
# Route handlers map ModelNotDeployedError → 409
# ---------------------------------------------------------------------------

def _make_request():
    from src.api.routes import ExecuteRequest

    return ExecuteRequest(
        model_id="model-1",
        raw_query="SELECT 1",
        protocol="jdbc",
    )


@pytest.mark.asyncio
async def test_execute_returns_409_when_model_not_deployed():
    from src.api.routes import _handle_execute

    db = AsyncMock()
    err = ModelNotDeployedError("Model model-1 is not deployed; click Deploy…")

    with patch(
        "src.api.routes.bind_query_to_model",
        new=AsyncMock(side_effect=err),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_execute(_make_request(), db, user_identity="u@example.com")

    assert exc.value.status_code == 409
    assert "not deployed" in exc.value.detail


@pytest.mark.asyncio
async def test_explain_returns_409_when_model_not_deployed():
    from src.api.routes import _handle_explain

    db = AsyncMock()
    err = ModelNotDeployedError("Model model-1 is not deployed; click Deploy…")

    with patch(
        "src.api.routes.bind_query_to_model",
        new=AsyncMock(side_effect=err),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_explain(_make_request(), db)

    assert exc.value.status_code == 409
    assert "not deployed" in exc.value.detail


@pytest.mark.asyncio
async def test_execute_still_returns_422_for_other_binding_errors():
    """Regression guard: a plain SemanticBindingError must keep mapping to
    422 so the two cases stay distinguishable on the wire."""
    from src.api.routes import _handle_execute

    db = AsyncMock()
    err = SemanticBindingError("Unknown column: 'foo' in model 'm'")

    with patch(
        "src.api.routes.bind_query_to_model",
        new=AsyncMock(side_effect=err),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_execute(_make_request(), db, user_identity="u@example.com")

    assert exc.value.status_code == 422
    assert "Unknown column" in exc.value.detail
