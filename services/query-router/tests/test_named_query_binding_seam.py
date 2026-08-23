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

import re
from types import SimpleNamespace
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


def _undeployed_db() -> AsyncMock:
    """A db whose model has no deploy pointer.

    Bug-9397: ``_bind_query_parameters`` now CLASSIFIES the model's parameter
    authority before binding, and refuses (503) when it cannot. A bare
    ``MagicMock()`` db therefore no longer reaches the seam under test — it
    used to only because the old code swallowed the failure in
    ``except Exception: pass`` and silently fell back to live draft values.
    Modelling a real undeployed model keeps this module testing the SEAM
    instead of testing the swallow.
    """
    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(deployed_version_id=None))
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=[])
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars)
    db.execute = AsyncMock(return_value=result)
    return db


async def _run_binding(sql: str, *, deployed_defs: dict) -> str:
    body = _body(sql)
    db = _undeployed_db()
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


# ---------------------------------------------------------------------------
# L2-F2 — the Law-6 parameter/Named-Query collision refusal.
#
# ``sql_references_named_query_position`` returns the BARE token text
# (``leads``), while model-service REQUIRES a ``ModelParameter.name`` to carry
# the sigil (``_PARAM_NAME_RE = ^@[A-Za-z_]\w*$``). The refusal compared the two
# directly, so ``"leads" in {"@leads"}`` was never true and a documented
# governance check never fired ONCE. The parameter then won the substitution
# and the user got a downstream parse error naming a table nobody created.
#
# Nothing tested this check anywhere before these cells.
# ---------------------------------------------------------------------------


def _deployed_param_shape(*names: str) -> SimpleNamespace:
    return SimpleNamespace(
        model_parameter_names=set(names), model_parameters=[],
    )


async def _run_binding_deployed(
    body: MagicMock, *, deployed_defs: dict, param_names: tuple[str, ...],
) -> MagicMock:
    """Bind ``body`` against a DEPLOYED model declaring ``param_names``.

    The caller owns the body so it can assert on ``raw_query`` even when the
    call raises. ``apply_parameters`` SUBSTITUTES here rather than passing
    through, so a collision that is NOT refused is visible as a rewritten
    ``raw_query`` instead of looking identical to a refusal.
    """
    from src.semantic.snapshot_resolver import SnapshotAuthority

    db = _undeployed_db()

    async def _substituting(**kw):
        # Mirror the real resolver: a DECLARED parameter is substituted; any
        # other ``@name`` (a whitelisted Named Query reference) passes through.
        sql = kw["sql"]
        for _name in param_names:
            sql = re.sub(
                rf"@{re.escape(_name.lstrip('@'))}\b",
                "'substituted'",
                sql,
                flags=re.IGNORECASE,
            )
        return sql

    with (
        patch(
            "src.api.routes.load_named_lists",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.routes.load_named_queries",
            new=AsyncMock(return_value=deployed_defs),
        ),
        patch(
            "src.api.routes._resolve_parameter_authority",
            new=AsyncMock(
                return_value=(
                    SnapshotAuthority.DEPLOYED,
                    _deployed_param_shape(*param_names),
                )
            ),
        ),
        patch(
            "src.api.routes.apply_parameters",
            new=AsyncMock(side_effect=_substituting),
        ),
    ):
        await routes_mod._bind_query_parameters(body, db, None)
    return body


async def test_l2_f2_a_from_position_name_colliding_with_a_parameter_is_refused() -> None:
    """A deployed model declaring parameter ``@leads`` AND a Named Query
    ``leads``: ``SELECT * FROM @leads`` is ambiguous and must be refused BEFORE
    substitution, not resolved silently in the parameter's favour.

    The ``raw_query`` assertion is the load-bearing half — the refusal has to
    land ahead of ``apply_parameters``. When it did not, the parameter won and
    the caller got a parse error about a table nobody created.
    """
    from fastapi import HTTPException

    body = _body("SELECT * FROM @leads")
    with pytest.raises(HTTPException) as excinfo:
        await _run_binding_deployed(
            body,
            deployed_defs={"@leads": MagicMock()},
            param_names=("@leads",),
        )
    assert excinfo.value.status_code == 400
    assert "matches both a model parameter and a Named Query" in str(
        excinfo.value.detail
    )
    assert body.raw_query == "SELECT * FROM @leads"


async def test_l2_f2_a_case_variant_collision_is_also_refused() -> None:
    """``@Leads`` the parameter vs ``leads`` the Named Query. The @-namespace is
    case-insensitive, so a case variant is the SAME collision."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await _run_binding_deployed(
            _body("SELECT * FROM @leads"),
            deployed_defs={"@leads": MagicMock()},
            param_names=("@Leads",),
        )
    assert excinfo.value.status_code == 400


async def test_l2_f2_a_non_colliding_parameter_still_binds() -> None:
    """Control: a deployed parameter with a DIFFERENT name is not a collision.
    Without this cell the fix could have been "always refuse"."""
    body = await _run_binding_deployed(
        _body("SELECT * FROM @leads"),
        deployed_defs={"@leads": MagicMock()},
        param_names=("@region",),
    )
    assert body.raw_query == "SELECT * FROM @leads"


async def test_l2_f2_a_parameter_in_from_position_with_no_named_query_is_not_a_collision() -> None:
    """Control on the other side: the FROM-position whitelist is populated for
    ANY ``@name``, so testing it alone would refuse ``SELECT * FROM @leads`` on
    a model with a ``@leads`` PARAMETER and no Named Query at all — with an
    error message that is simply untrue. No Named Query, no collision."""
    body = await _run_binding_deployed(
        _body("SELECT * FROM @leads"),
        deployed_defs={},
        param_names=("@leads",),
    )
    assert body.raw_query == "SELECT * FROM 'substituted'"


async def test_non_from_placeholder_still_rejected() -> None:
    """A bare unknown placeholder OUTSIDE FROM position keeps the ordinary
    reject surface (the whitelist must not leak past FROM position)."""
    body = _body("SELECT * FROM modely WHERE branch_id = @unknown_param")
    db = _undeployed_db()
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
