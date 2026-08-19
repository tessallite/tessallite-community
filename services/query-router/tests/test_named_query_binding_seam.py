"""Named Query reference binding — the parameter-binding seam (routes.py).

A ``SELECT * FROM @name`` reference reaches the step-1.6 interceptor only if
the parameter-binding step lets the placeholder through: it is whitelisted
against ``apply_parameters`` (a named-query name never resolves as a
parameter value) and exempted from the post-expansion leftover check. Two
escapes are pinned here:

  * the leftover check compared the placeholder span name (which INCLUDES the
    leading ``@``, per ``placeholder_spans``) against the FROM-position scan
    result (which does NOT) — so the exemption never matched and every exact
    reference died with "Unknown placeholder" before the interceptor ran;
  * the whitelist was populated only when the name resolved to a deployed
    Named Query, so an UNKNOWN reference died with the generic placeholder
    error instead of the interceptor's specific ``NQ_UNKNOWN_REFERENCE``.

Test escape: resolver unit tests exercised the recognisers in isolation, but
no test ran the real ``_bind_query_parameters`` path that gates the
interceptor over the JDBC route. Guard: this module. Tier: T1.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api import routes as routes_mod

pytestmark = pytest.mark.unit


def _body(sql: str) -> MagicMock:
    return MagicMock(
        raw_query=sql,
        model_id="00000000-0000-0000-0000-000000000001",
        protocol="jdbc",
        session_vars=None,
    )


async def _run_binding(sql: str, *, deployed_defs: dict) -> str:
    body = _body(sql)
    db = MagicMock()
    with (
        patch(
            "src.api.routes.load_named_lists",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.routes.load_named_queries",
            new=AsyncMock(return_value=deployed_defs),
        ),
        # apply_parameters: leave the SQL untouched (the placeholder is
        # declared, so the real one would pass it through unchanged).
        patch(
            "src.api.routes.apply_parameters",
            new=AsyncMock(side_effect=lambda **kw: kw["sql"]),
        ),
    ):
        await routes_mod._bind_query_parameters(body, db, None)
    return body.raw_query


async def test_exact_reference_survives_binding_when_deployed() -> None:
    """The deployed exact-shape reference is whitelisted and exempted — the
    step-1.6 interceptor, not the placeholder check, owns the query."""
    sql = await _run_binding(
        "SELECT * FROM @branch_3279863",
        deployed_defs={"@branch_3279863": MagicMock()},
    )
    assert sql == "SELECT * FROM @branch_3279863"


async def test_unknown_reference_survives_binding_for_interceptor_error() -> None:
    """An UNKNOWN FROM-position name must reach the interceptor too, so the
    caller gets NQ_UNKNOWN_REFERENCE instead of the generic placeholder 400."""
    sql = await _run_binding(
        "SELECT * FROM @nq_does_not_exist",
        deployed_defs={},
    )
    assert sql == "SELECT * FROM @nq_does_not_exist"


async def test_decorated_reference_survives_binding_for_shape_error() -> None:
    """A decorated reference reaches the interceptor's NQ_UNSUPPORTED_SHAPE
    surface instead of a placeholder error."""
    sql = await _run_binding(
        "SELECT branch_id FROM @branch_3279863",
        deployed_defs={"@branch_3279863": MagicMock()},
    )
    assert sql == "SELECT branch_id FROM @branch_3279863"


async def test_non_from_placeholder_still_rejected() -> None:
    """A bare unknown placeholder OUTSIDE FROM position keeps the ordinary
    reject surface (the whitelist must not leak past FROM position)."""
    body = _body("SELECT * FROM modely WHERE branch_id = @unknown_param")
    db = MagicMock()
    with (
        patch(
            "src.api.routes.load_named_lists",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.routes.load_named_queries",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.routes.apply_parameters",
            new=AsyncMock(side_effect=lambda **kw: kw["sql"]),
        ),
    ):
        with pytest.raises(Exception) as excinfo:
            await routes_mod._bind_query_parameters(body, db, None)
    assert "Unknown placeholder" in str(excinfo.value)
