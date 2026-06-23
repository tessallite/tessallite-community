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


class QueryExecutionError(RuntimeError):
    """Wraps query-router 4xx/5xx so the pipeline can refuse politely."""


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
    for ref in (call.dimension_refs or []):
        violations += [f for f in ref.base_fields if f not in scope.dimensions]
    # DR-B5349-P1-01 — a selected expression dimension's generated alias is a
    # legitimate reference target for sort/having once its base fields have
    # passed scope above (e.g. ORDER BY "business_date_month"). Add the selected
    # aliases to the visible set so an explicit sort on an aliased expression is
    # not falsely rejected. Bare-dimension aliases equal their names and are
    # already covered by scope.dimensions, so this only widens for expressions.
    dim_aliases = {ref.alias for ref in (call.dimension_refs or [])}
    visible = scope.measures | scope.dimensions | dim_aliases
    referenced: list[str] = []
    for f in list(call.where) + list(call.having) + list(call.sort):
        name = f.get("name") if isinstance(f, dict) else None
        if isinstance(name, str) and name:
            referenced.append(name)
    violations += [n for n in referenced if n not in visible]
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


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


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
    if not select_parts:
        select_parts = ["*"]

    sql = f"SELECT {', '.join(select_parts)} FROM {_quote_ident(model_slug)}"

    where_preds: list[str] = []
    for f in call.where:
        pred = _filter_to_sql(f)
        if pred:
            where_preds.append(pred)
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
        "protocol": "jdbc",
        "include_hidden": False,
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
    )
