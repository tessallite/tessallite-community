"""Single-model query execution: structured tool call -> SQL -> query-router.

The agent emits a structured `query` tool call. We translate it to a
SELECT statement against the model's slug (which is also the catalog
name on the JDBC gateway), POST to the query router's /execute endpoint
with the caller's JWT, and return the rows + metadata.

Keeping SQL composition here (rather than asking the LLM to write SQL)
lets us enforce identifier quoting, value escaping, and structural
constraints centrally. The query-router's binder then validates that
every measure / dimension actually exists.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.db.models import Measure, Model
from shared.security.execute_contract import (
    row_security_denied_all,
    security_rules_from_execute_response,
)
from src.tools.expressions import _quote_ident
from src.tools.spec import QueryToolCall

logger = logging.getLogger(__name__)
settings = get_settings()

# The LLM's default per-query row cap (see tools/spec.py — "Use 100 unless the
# user requests another limit"). Trend/time-series questions over a long window
# silently lose their tail at this cap (Bug-5351), so when a query groups by a
# date/time dimension and the LLM left the limit at the default, we raise the
# effective cap to a floor that covers e.g. a couple of years of daily rows.
_DEFAULT_LLM_LIMIT = 100
_TREND_LIMIT_FLOOR = 1000

# Date/time-shaped dimension names (mirrors the narrator's date detection):
# a query grouping by one of these is a trend whose tail matters.
_DATE_DIM_RE = re.compile(
    r"(date|month|quarter|year|week|day|_ts$|timestamp|period)", re.IGNORECASE
)


def _has_date_dimension(call: QueryToolCall) -> bool:
    # Bug-5349 R2 — inspect the typed dimension refs, not just bare strings, so
    # a DATE_TRUNC grained dimension (the case Bug-5349 enables) still triggers
    # Bug-5351's trend floor. A ref is date-shaped when it wraps a date function
    # (date_trunc/extract/date_part), or its alias or any underlying base field
    # matches the date/time name pattern.
    for ref in (call.dimension_refs or []):
        if ref.is_date_fn:
            return True
        if _DATE_DIM_RE.search(ref.alias):
            return True
        if any(_DATE_DIM_RE.search(f) for f in ref.base_fields):
            return True
    return False


def _is_chronological_trend(call: QueryToolCall) -> bool:
    """A chronological trend is grouped by a date/time dimension and sorted ONLY
    by date/time columns (or not sorted). A sort by any non-date column means
    the ranking — e.g. top-N months by revenue — IS the business question, so
    the limit must be preserved, not raised."""
    if not _has_date_dimension(call):
        return False
    for s in call.sort:
        name = s.get("name") if isinstance(s, dict) else None
        if isinstance(name, str) and name and not _DATE_DIM_RE.search(name):
            return False
    return True


def _trim_overfetch(rows: list, cap: int) -> tuple[list, bool]:
    """Given rows fetched with ``LIMIT cap+1``, return ``(trimmed_rows,
    truncated)``. More than ``cap`` rows proves the result is truncated (M-001):
    drop the sentinel and report exactly ``cap``; otherwise the result is
    complete and must not be flagged as partial."""
    if len(rows) > cap:
        return rows[:cap], True
    return rows, False


def effective_limit(call: QueryToolCall) -> int:
    """The row cap actually applied to the SQL.

    Bug-5351 — a plain time-series trend left at the LLM's default limit loses
    its tail; we raise it to the trend floor so the whole series returns.
    H2-001 (review) — raise ONLY when the limit was defaulted (not explicitly
    requested by the user) AND the query is a chronological trend (not a
    measure-ranked top-N). Any user-requested limit — including exactly 100 —
    is the user's intent and is preserved verbatim."""
    limit = int(call.limit)
    if not getattr(call, "limit_explicit", False) and _is_chronological_trend(call):
        return _TREND_LIMIT_FLOOR
    return limit


@dataclass
class QueryExecution:
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    rows_returned: int
    route_type: str
    routed_sql: str | None
    aggregate_id: str | None
    pocket_id: str | None
    execution_ms: int
    # True when the result filled the applied row cap, so more rows likely exist
    # beyond it (Bug-5351). The narrator must disclose this rather than imply the
    # data is complete.
    truncated: bool = False
    # Bug-8453: the row-security rule ids the router applied to THIS execution,
    # and whether they denied EVERY row. Without this the agent cannot tell
    # "there is no data for that question" (a fact about the business) from
    # "your row-security policy grants you no rows" (a fact about the caller's
    # permissions) — and it asserted the former, which is a false statement to
    # a business user, not merely a missing hint. Rule IDS only; never predicate
    # SQL, so surfacing it discloses no data and no policy logic.
    security_rules_applied: tuple[str, ...] = ()
    row_security_denied: bool = False


class QueryExecutionError(RuntimeError):
    """Wraps query-router 4xx/5xx so the pipeline can refuse politely."""


class RowSecurityDeniedQueryError(QueryExecutionError):
    """Bug-8453 / R2 finding B2 — row security denied the caller EVERY row.

    Raised by the ``execute_query`` chokepoint itself rather than left to each
    caller to notice. There are three call sites (direct query, compound step,
    recipe step) and only the direct one rendered the denial; the other two
    consumed the result as data. A deny-all rewrites the query to
    ``... WHERE 0 = 1``, over which ``COUNT(*)`` still returns a row containing
    **0**, so a denied step fed a real-looking zero into a combine expression
    and the agent narrated e.g. "you had 0 orders this period" -- an
    authoritative false statement about the business, the exact defect
    Bug-8453 set out to remove from the direct path.

    Subclasses ``QueryExecutionError`` deliberately: both other call sites
    already catch that and REFUSE, so they inherit fail-closed behaviour rather
    than needing to remember it, and so does any future caller. A caller that
    genuinely renders the denial (the direct path, which has narration for it)
    opts out with ``allow_row_security_denial=True``.
    """


class ModelNotAllowListedError(QueryExecutionError):
    """F-023-07 — the tool call targets a model outside the project
    agent's allow-list. Raised by the single execution chokepoint so the
    direct query branch, compound steps, and recipe steps are all
    enforced at the same point."""


class PersonaScopeViolationError(QueryExecutionError):
    """F-023-08 — the tool call references a model or fields outside the
    conversation's active persona scope. Enforced server-side at the
    execution chokepoint; the prompt-level filtering remains
    defence-in-depth only."""


def _extract_router_error_detail(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return resp.text
    if not isinstance(payload, dict):
        return resp.text
    detail = payload.get("detail")
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail")
        if isinstance(message, str) and message:
            return message
    if isinstance(detail, str) and detail:
        return detail
    return resp.text


@dataclass(frozen=True)
class PersonaFieldScope:
    """Effective allowed semantic field names for one model under the
    conversation's project persona (already widened to the full model
    lists when the persona's include lists are empty)."""

    measures: frozenset[str]
    dimensions: frozenset[str]


def _persona_scope_violations(
    call: QueryToolCall, scope: PersonaFieldScope
) -> list[str]:
    """Names referenced by the call that fall outside the persona scope."""
    violations: list[str] = [
        m for m in call.measures if m not in scope.measures
    ]
    # Bug-5349 R1 — walk the base fields of every dimension expression, not the
    # generated alias, so a function (e.g. DATE_TRUNC('month', hidden_col))
    # cannot smuggle a field that is outside the persona scope. Violations
    # report the underlying base field, never the alias or function output.
    # The same field-walk runs over every expression in EVERY clause
    # (projection, WHERE, HAVING) — Bug-5349 Phase 2/3 SECURITY TRAP: a scoped
    # column must never leak past persona filtering inside an expression of any
    # clause. A base field is checked against measures OR dimensions because an
    # expression operand may legitimately be a measure (e.g. ROUND(amount, 0))
    # or a dimension (e.g. LOWER(city)).
    allowed_base = scope.measures | scope.dimensions
    for ref in (call.dimension_refs or []):
        violations += [f for f in ref.base_fields if f not in allowed_base]
    for proj in (getattr(call, "projection_refs", None) or []):
        violations += [f for f in proj.base_fields if f not in allowed_base]
    for pref in (getattr(call, "where_refs", None) or []):
        violations += [f for f in pref.base_fields if f not in allowed_base]
    for href in (getattr(call, "having_refs", None) or []):
        violations += [f for f in href.base_fields if f not in allowed_base]
    # Flat {name,op,value} references. A flat WHERE / HAVING name binds to a
    # REAL model column at execution time — SQL does not resolve SELECT aliases
    # in WHERE / HAVING — so these MUST be validated against real scoped fields
    # only. A generated expression alias is an arbitrary LLM-chosen string; it
    # must NEVER widen the WHERE/HAVING visible set, or an expression aliased to
    # a persona-hidden column name would let a flat filter reference that hidden
    # column (Codex-1 persona-scope bypass). Only SORT may reference an alias,
    # because ORDER BY resolves SELECT aliases in PostgreSQL (DR-B5349-P1-01 — a
    # legitimate ``ORDER BY "business_date_month"`` on a selected expression).
    real_scoped = scope.measures | scope.dimensions
    dim_aliases = {ref.alias for ref in (call.dimension_refs or [])}
    proj_aliases = {p.alias for p in (getattr(call, "projection_refs", None) or [])}
    sort_visible = real_scoped | dim_aliases | proj_aliases
    for f in list(call.where) + list(call.having):
        name = f.get("name") if isinstance(f, dict) else None
        if isinstance(name, str) and name and name not in real_scoped:
            violations.append(name)
    for f in list(call.sort):
        name = f.get("name") if isinstance(f, dict) else None
        if isinstance(name, str) and name and name not in sort_visible:
            violations.append(name)
    return sorted(set(violations))


def enforce_execution_scope(
    call: QueryToolCall,
    *,
    allowed_model_ids: Collection[UUID],
    persona_scopes: Mapping[UUID, PersonaFieldScope] | None,
) -> UUID:
    """Single enforcement chokepoint for every agent query execution
    (direct query, compound step, recipe step).

    Raises :class:`ModelNotAllowListedError` when the model is outside
    the project agent allow-list and :class:`PersonaScopeViolationError`
    when the conversation's persona excludes the model or any referenced
    field. ``persona_scopes is None`` means the conversation has no
    persona; an empty mapping means the persona exposes no models at
    all (fail closed)."""
    try:
        model_uuid = UUID(call.model_id)
    except ValueError as exc:
        raise QueryExecutionError(
            f"Invalid model_id from LLM: {call.model_id!r}"
        ) from exc

    if model_uuid not in set(allowed_model_ids):
        raise ModelNotAllowListedError(
            f"Model {call.model_id} is not allow-listed for this project's agent."
        )

    if persona_scopes is not None:
        scope = persona_scopes.get(model_uuid)
        if scope is None:
            raise PersonaScopeViolationError(
                f"Model {call.model_id} is not available under the active persona."
            )
        violations = _persona_scope_violations(call, scope)
        if violations:
            raise PersonaScopeViolationError(
                "Fields outside the active persona scope: "
                + ", ".join(violations)
            )
    return model_uuid


def _quote_value(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "NULL"
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _filter_to_sql(f: dict[str, Any], *, agg_wrap: str | None = None) -> str | None:
    name = f.get("name")
    op = (f.get("op") or "eq").lower()
    val = f.get("value")
    if not isinstance(name, str) or not name:
        return None
    col = _quote_ident(name)
    if agg_wrap:
        col = f"{agg_wrap}({col})"
    if op == "eq":
        return f"{col} = {_quote_value(val)}"
    if op == "neq":
        return f"{col} <> {_quote_value(val)}"
    if op in ("gt", "gte", "lt", "lte"):
        symbol = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
        return f"{col} {symbol} {_quote_value(val)}"
    if op == "in":
        if not isinstance(val, list) or not val:
            return None
        items = ", ".join(_quote_value(v) for v in val)
        return f"{col} IN ({items})"
    if op == "between":
        if not isinstance(val, (list, tuple)) or len(val) != 2:
            return None
        lo, hi = val
        return f"{col} BETWEEN {_quote_value(lo)} AND {_quote_value(hi)}"
    if op == "like":
        return f"{col} LIKE {_quote_value(val)}"
    if op == "is_null":
        return f"{col} IS NULL"
    if op == "is_not_null":
        return f"{col} IS NOT NULL"
    return None


def build_sql(
    model_slug: str,
    call: QueryToolCall,
    measure_aggs: dict[str, str] | None = None,
) -> str:
    # Bug-5349 — dimensions render from typed refs. A bare dimension renders
    # exactly as before (``"col"`` with no alias) so legacy SQL is byte-for-byte
    # identical; a grained/expression dimension renders its PG-canonical
    # function with a deterministic alias (e.g.
    # ``DATE_TRUNC('month', "business_date") AS "business_date_month"``).
    dim_refs = call.dimension_refs or []
    select_parts: list[str] = [ref.render_select() for ref in dim_refs]
    for m in call.measures:
        agg = (measure_aggs or {}).get(m, "SUM").upper()
        select_parts.append(f"{agg}({_quote_ident(m)}) AS {_quote_ident(m)}")
    # Bug-5349 Phase 3 — computed projection columns (CASE / arithmetic /
    # CONCAT / ROUND(SUM(..)) ...). Rendered after the bare measures so legacy
    # SELECT order is unchanged when no projection is present.
    for proj in (getattr(call, "projection_refs", None) or []):
        select_parts.append(proj.render_select())
    if not select_parts:
        select_parts = ["*"]

    sql = f"SELECT {', '.join(select_parts)} FROM {_quote_ident(model_slug)}"

    where_preds: list[str] = []
    for f in call.where:
        pred = _filter_to_sql(f)
        # Legacy contract: a None here is the documented "empty IN list is
        # ignored" / no-op case (see tool spec). It is NOT dropped silently for
        # a populated filter because the flat filter ops are all total.
        if pred:
            where_preds.append(pred)
    # Bug-5349 Phase 2 — structured predicates (function-on-column,
    # column-to-column, OR/NOT) render from the typed AST. These were validated
    # at parse time (R3 fail-closed there), so render() never returns None.
    for ref in (getattr(call, "where_refs", None) or []):
        where_preds.append(ref.render())
    if where_preds:
        sql += " WHERE " + " AND ".join(where_preds)

    if dim_refs:
        # GROUP BY repeats the expression (not the alias) so the query-router
        # parser flags it as function grain and routes via the passthrough/
        # source path (binder.has_function_grain).
        sql += " GROUP BY " + ", ".join(ref.render_group_by() for ref in dim_refs)

    having_preds: list[str] = []
    for h in call.having:
        h_name = h.get("name", "")
        agg = (measure_aggs or {}).get(h_name, "SUM").upper() if h_name else None
        pred = _filter_to_sql(h, agg_wrap=agg)
        if pred:
            having_preds.append(pred)
    # Bug-5349 Phase 3 — structured HAVING predicates (ratio-of-aggregates,
    # computed aggregate expressions, OR).
    for ref in (getattr(call, "having_refs", None) or []):
        having_preds.append(ref.render())
    if having_preds:
        sql += " HAVING " + " AND ".join(having_preds)

    if call.sort:
        order_parts = []
        for s in call.sort:
            name = s.get("name")
            direction = s.get("direction", "desc").upper()
            if name:
                order_parts.append(f"{_quote_ident(name)} {direction}")
        if order_parts:
            sql += " ORDER BY " + ", ".join(order_parts)
    elif dim_refs:
        # Default chronological/lexical order. Bare dims order by the quoted
        # name (byte-for-byte with legacy); expression dims order by the same
        # expression used in GROUP BY.
        sql += " ORDER BY " + ", ".join(
            ref.render_order_by("ASC") for ref in dim_refs
        )

    # Over-fetch one row beyond the cap (M-001) so execute_query can PROVE
    # truncation: rows beyond the cap mean more data exists; exactly the cap
    # means the result is complete. The sentinel row is trimmed before return.
    sql += f" LIMIT {effective_limit(call) + 1}"
    return sql


async def execute_query(
    db: AsyncSession,
    call: QueryToolCall,
    jwt_token: str,
    *,
    allowed_model_ids: Collection[UUID],
    persona_scopes: Mapping[UUID, PersonaFieldScope] | None = None,
    allow_row_security_denial: bool = False,
) -> QueryExecution:
    # F-023-07 / F-023-08 — every execution path (direct query, compound
    # step, recipe step) flows through this one enforcement point. The
    # caller cannot opt out: allowed_model_ids is mandatory.
    model_uuid = enforce_execution_scope(
        call,
        allowed_model_ids=allowed_model_ids,
        persona_scopes=persona_scopes,
    )

    model = await db.get(Model, model_uuid)
    if model is None:
        raise QueryExecutionError(f"Model {call.model_id} not found.")

    meas_result = await db.execute(
        select(Measure.name, Measure.default_agg)
        .where(Measure.model_id == model_uuid)
    )
    measure_aggs = {
        name: (agg or "sum").upper()
        for name, agg in meas_result.all()
    }

    sql = build_sql(model.slug, call, measure_aggs)
    url = f"{settings.QUERY_ROUTER_URL}/api/v1/execute"
    body = {
        "model_id": str(model_uuid),
        "raw_query": sql,
        # protocol stays "jdbc" -- the parser's strict-syntax and GROUP BY
        # enforcement branches key off protocol == "jdbc" (see
        # query-router/src/api/routes.py::_parse); agent-generated SQL is
        # the same dialect and must not skip that strictness. client_kind
        # carries the "agent" observability label instead (matches the
        # headless/plugin client_kind pattern), so QueryLog/metrics can
        # attribute agent-service traffic without weakening parsing.
        "protocol": "jdbc",
        "include_hidden": False,
        "client_kind": "agent",
    }
    headers = {"Authorization": f"Bearer {jwt_token}"} if jwt_token else {}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise QueryExecutionError(f"Query router unreachable: {exc}") from exc

    if resp.status_code >= 400:
        detail = _extract_router_error_detail(resp)
        raise QueryExecutionError(
            f"Query rejected by router (HTTP {resp.status_code}): {detail}"
        )

    data = resp.json()
    # M-001 — the SQL over-fetched cap+1; trim the sentinel and prove truncation.
    rows, truncated = _trim_overfetch(list(data.get("rows") or []), effective_limit(call))
    # Bug-8453: classify through the shared execute contract, never by testing
    # "rows is empty" (a deny-all rewrites to WHERE 0 = 1, over which COUNT(*)
    # still returns a row containing 0).
    _security_rules = security_rules_from_execute_response(data)
    _denied = row_security_denied_all(_security_rules)
    if _denied and not allow_row_security_denial:
        # Fail closed at the chokepoint (R2 finding B2). Only a caller that can
        # actually TELL the user about the restriction may proceed with a
        # denied execution; everyone else must refuse rather than treat
        # ``WHERE 0 = 1`` output as a measurement.
        raise RowSecurityDeniedQueryError(
            "Row-level security denies you access to every row of this model, "
            "so no result can be produced. This is a permissions restriction, "
            "not an absence of data."
        )
    return QueryExecution(
        sql=sql,
        columns=list(data.get("columns") or []),
        rows=rows,
        rows_returned=len(rows),
        route_type=str(data.get("route_type") or ""),
        routed_sql=data.get("routed_sql") or data.get("rewritten_query"),
        aggregate_id=data.get("aggregate_id"),
        pocket_id=data.get("pocket_id"),
        execution_ms=int(data.get("execution_ms") or 0),
        truncated=truncated,
        security_rules_applied=tuple(sorted(_security_rules)),
        row_security_denied=_denied,
    )
