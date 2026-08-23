"""Drill-through persona scoping — CLS column removal + RLS pagination.

These tests pin the two security-paused drill findings from the unit-019
review (S-DRILL run):

* **F-019-09** — a drill must not expose a column the persona's CLS / allow
  list forbids. The drill SQL is a plain, *explicit* (non-``SELECT *``)
  projection routed through ``_handle_execute`` → ``route_query`` with the
  resolved persona + principal. The execute pipeline's column-restriction
  gate therefore evaluates the drill exactly like any main query: a drill
  projecting a restricted column fails CLOSED with 403, so the restricted
  column never reaches the drill response. These tests prove the drill
  honours the same persona scope as the main query and that the 403
  propagates verbatim (no silent fallback that could leak the column).

* **F-019-17** — the drill embeds ``LIMIT n+1`` after a keyset predicate. Row security is
  applied by the execute pipeline via **per-scan WHERE injection**
  (``_inject_security_where``), which ANDs the security predicate into the
  same SELECT that scans the physical table — *before* GROUP BY / keyset /
  LIMIT (Bug-915). The review's premise (an outer ``SELECT * FROM (<planned
  -with-LIMIT>) WHERE <pred>`` wrap, inner LIMIT applied first) no longer
  describes the implementation since the F-007-01 rewrite. These tests prove
  the predicate lands before LIMIT on both drill SQL shapes (leaf and
  hierarchy step-down) — so a page returns up to ``limit`` *allowed* rows,
  ``has_more`` is keyed on the post-security row count, and no allowed row
  beyond the window is unreachable. The predicate is column-name scoped on
  the scan, independent of the drill's projection, so the drill does NOT
  spuriously under-page nor leak.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from shared.security import Principal
from shared.security.predicate_compiler import CompiledPredicate
from src.api.drill_routes import DrillThroughRequest, _handle_drill_through
from src.drill.cursor import CursorOrderTerm, DrillCursorSpec
from src.routing.router import _inject_security_where

# Async tests opt in individually; the predicate-shape tests below are
# synchronous (pure sqlglot), so no module-wide asyncio mark.


def _uuid():
    return uuid.uuid4()


def _pred(expr: str = "region_code = 'NORTH'") -> CompiledPredicate:
    return CompiledPredicate(sql_expression=expr, active_rule_ids=("r1",))


def _build_sql_return(**overrides):
    defaults = dict(
        sql='SELECT "region", "amount" FROM "modely" '
            'ORDER BY "region", "amount" LIMIT 3',
        model_id_str=str(_uuid()),
        cursor_spec=DrillCursorSpec.build(
            scope={"fixture": "security"},
            order_terms=[CursorOrderTerm("region"), CursorOrderTerm("amount", True)],
            stable=True,
        ),
        effective_limit=2,
        drill_dim=None,
        drill_mode="leaf",
        hierarchy_path=[],
        drillable=[],
        fact_table="orders",
        source_join_path=[],
    )
    defaults.update(overrides)
    return (
        defaults["sql"],
        defaults["model_id_str"],
        defaults["cursor_spec"],
        defaults["effective_limit"],
        defaults["drill_dim"],
        defaults["drill_mode"],
        defaults["hierarchy_path"],
        defaults["drillable"],
        defaults["fact_table"],
        defaults["source_join_path"],
    )


def _execute_response(rows=None, columns=None):
    rows = rows or []
    return types.SimpleNamespace(
        rows=rows,
        columns=columns or ["region", "amount"],
        route_type="source",
        execution_ms=10,
        bytes_processed=512,
        rows_returned=len(rows),
    )


# ---------------------------------------------------------------------------
# F-019-17 — RLS predicate lands BEFORE LIMIT on drill SQL shapes
# ---------------------------------------------------------------------------


def test_rls_predicate_before_limit_on_leaf_drill():
    """Leaf drill: ``SELECT cols FROM t ... ORDER BY ... LIMIT n+1``.

    The security predicate must be injected into the scan's WHERE, ahead of
    LIMIT, so the database applies row security *before* paging.
    """
    sql = (
        'SELECT "region", "amount" FROM "orders" '
        'ORDER BY "region", "amount" LIMIT 51'
    )
    out = _inject_security_where(sql, _pred())
    assert "WHERE" in out and "NORTH" in out
    assert out.index("NORTH") < out.index("LIMIT")


def test_rls_predicate_before_limit_on_hierarchy_drill():
    """Hierarchy step-down: ``... GROUP BY level ORDER BY ... LIMIT n+1``.

    The predicate must precede GROUP BY and LIMIT so the aggregate rolls up
    only the allowed rows and the page is over allowed groups.
    """
    sql = (
        'SELECT "month", SUM("amount") AS "amount" FROM "orders" '
        'GROUP BY "month" ORDER BY "month" LIMIT 51'
    )
    out = _inject_security_where(sql, _pred())
    assert "WHERE" in out and "NORTH" in out
    assert out.index("NORTH") < out.index("GROUP BY")
    assert out.index("NORTH") < out.index("LIMIT")


def test_rls_predicate_scoped_to_scan_not_projection():
    """The predicate references the security column on the *scan*, not the
    projection — so a drill that does NOT project the security column is
    still filtered (no spurious under-paging, no leak)."""
    # Projection is region/amount only; security column is region_code.
    sql = 'SELECT "amount" FROM "orders" LIMIT 51'
    out = _inject_security_where(sql, _pred())
    assert "region_code = 'NORTH'" in out
    assert out.index("region_code") < out.index("LIMIT")


# ---------------------------------------------------------------------------
# F-019-17 — has_more / pagination accounting keys on POST-security rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drill_has_more_keys_on_post_security_row_count():
    """The execute pipeline applies RLS before LIMIT n+1, so ``rows`` are the
    *allowed* rows. ``has_more`` must be ``len(allowed_rows) > effective_limit``
    and the page must return exactly ``effective_limit`` allowed rows — a full
    page of allowed rows, never under-paged, never leaking the n+1 probe row.
    """
    db = AsyncMock()
    body = DrillThroughRequest(limit=2)
    # Pipeline returns effective_limit+1 = 3 allowed rows (RLS already applied).
    allowed = [
        {"region": "NORTH", "amount": 1},
        {"region": "NORTH", "amount": 2},
        {"region": "NORTH", "amount": 3},
    ]

    async def _fake_execute(req, db, **kwargs):
        return _execute_response(rows=list(allowed))

    with (
        patch("src.api.drill_routes.build_drill_sql",
              new=AsyncMock(return_value=_build_sql_return(effective_limit=2))),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        resp = await _handle_drill_through(
            _uuid(), body, db,
            principal=Principal(user_identity="a@x.com", roles=frozenset({"r"})),
        )

    assert resp.page.has_more is True
    assert len(resp.rows) == 2  # full page of allowed rows, n+1 probe trimmed
    assert all(r["region"] == "NORTH" for r in resp.rows)
    spec = _build_sql_return(effective_limit=2)[2]
    decoded = spec.decode(resp.page.next_cursor)
    assert decoded is not None
    assert [value.value for value in decoded] == ["NORTH", 2]


@pytest.mark.asyncio
async def test_drill_last_page_has_more_false_with_fewer_allowed_rows():
    """When RLS narrows the result so fewer than ``effective_limit+1`` rows
    come back, ``has_more`` is False and every returned row is allowed — a
    short last page, correctly terminated (not a premature ``has_more=False``
    hiding reachable allowed rows, because RLS ran before the LIMIT)."""
    db = AsyncMock()
    body = DrillThroughRequest(limit=10)
    allowed = [{"region": "NORTH", "amount": 1}, {"region": "NORTH", "amount": 2}]

    async def _fake_execute(req, db, **kwargs):
        return _execute_response(rows=list(allowed))

    with (
        patch("src.api.drill_routes.build_drill_sql",
              new=AsyncMock(return_value=_build_sql_return(effective_limit=10))),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        resp = await _handle_drill_through(_uuid(), body, db)

    assert resp.page.has_more is False
    assert resp.page.next_cursor is None
    assert len(resp.rows) == 2


# ---------------------------------------------------------------------------
# F-019-09 — drill cannot expose a CLS / persona-restricted column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drill_restricted_column_403_propagates_fail_closed():
    """A drill that projects a column the persona's CLS forbids is rejected by
    the execute pipeline's column-restriction gate (403). The drill handler
    must propagate that 403 verbatim — never swallow it into a 200 that would
    leak the restricted column."""
    db = AsyncMock()
    body = DrillThroughRequest(limit=5, persona_id=str(_uuid()))

    async def _fake_execute(req, db, **kwargs):
        raise HTTPException(
            status_code=403,
            detail={"error_code": "COLUMN_RESTRICTED",
                    "message": "restricted for this persona"},
        )

    with (
        patch("src.api.drill_routes.build_drill_sql",
              new=AsyncMock(return_value=_build_sql_return())),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "COLUMN_RESTRICTED"


@pytest.mark.asyncio
async def test_drill_persona_blocked_dimension_403_propagates():
    """Same fail-closed contract for the persona allow-list gate: a drill
    projecting a dimension not included in the persona is 403, not a 200 with
    the dimension in the rows."""
    db = AsyncMock()
    body = DrillThroughRequest(limit=5, persona_id=str(_uuid()))

    async def _fake_execute(req, db, **kwargs):
        raise HTTPException(
            status_code=403,
            detail={"error_code": "PERSONA_OBJECT_NOT_INCLUDED",
                    "object_kind": "dimension", "object_name": "country_code"},
        )

    with (
        patch("src.api.drill_routes.build_drill_sql",
              new=AsyncMock(return_value=_build_sql_return())),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_drill_through(_uuid(), body, db)

    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "PERSONA_OBJECT_NOT_INCLUDED"


@pytest.mark.asyncio
async def test_drill_propagates_persona_and_principal_to_execute():
    """The drill must hand the resolved persona_id AND principal to
    ``_handle_execute`` so the CLS gate and RLS injection both fire with the
    same scope the main query uses. (Belt-and-braces for F-019-09/F-019-17:
    if either is dropped, the drill would run unscoped.)"""
    db = AsyncMock()
    pid = str(_uuid())
    body = DrillThroughRequest(limit=2, persona_id=pid)
    principal = Principal(user_identity="alice@acme.com", roles=frozenset({"rm_north"}))
    captured = {}

    async def _fake_execute(req, db, **kwargs):
        captured["persona_id"] = kwargs.get("persona_id")
        captured["principal"] = kwargs.get("principal")
        captured["req_persona_id"] = req.persona_id
        return _execute_response(rows=[])

    with (
        patch("src.api.drill_routes.build_drill_sql",
              new=AsyncMock(return_value=_build_sql_return())),
        patch("src.api.drill_routes._handle_execute", new=_fake_execute),
    ):
        await _handle_drill_through(
            _uuid(), body, db,
            principal=principal, user_identity="alice@acme.com",
        )

    assert captured["persona_id"] == pid
    assert captured["req_persona_id"] == pid
    assert captured["principal"] is principal
