"""Usage & Downstream Assets — gateway query-log usage scan.

Scans Tessallite's OWN internal gateway query log (``QueryLog``) and derives
which of the model's physical tables and columns each logged query actually
used. It does NOT read the source database's query history (e.g.
``pg_stat_statements`` / BigQuery ``INFORMATION_SCHEMA.JOBS``); source-audit
collection is deferred (Bug-7761).

Authority order for "what did this query use" (Bug-8471):

  1. the binder-produced stable references on the bind ``RouteLog``
     (``column_usage_refs`` + ``semantic_object_refs``) — exact, and independent
     of how anything is spelled;
  2. for the semantic objects that have no direct physical column (a calculated
     measure, a UDA-backed field), the model dependency closure (Bug-8483);
  3. for LEGACY logs written before that contract existed, the two SQL texts:
     physical tables parsed out of ``rewritten_query``, plus the semantic
     measure/dimension names parsed out of ``raw_query`` and resolved through
     the same closure.

The previous implementation matched ``ModelTable.physical_name`` against
``QueryLog.raw_query``, which holds the SEMANTIC query text the BI client sent
(``SELECT SUM("Revenue") FROM "modelx"``). A physical name is essentially never
present there, so ``tables_matched`` was 0 on every real model and the Query
Audit tab was permanently empty (Bug-8471).

Route paths retain the ``/impact`` prefix as a stable internal API contract with
the frontend client; the user-facing feature name is "Usage & Downstream Assets".
"""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlglot
from sqlglot import exp

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update

from shared.db.models import (
    DataSource,
    GatewayQueryReference,
    Model,
    ModelColumn,
    ModelTable,
    QueryLog,
    RouteLog,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import ImpactScanResponse
from shared.schemas.domains.governance_advanced import ColumnUsageItem, ColumnUsageResponse
from src.api.impact_usage import (
    build_semantic_closure,
    build_semantic_name_index,
    columns_for_objects,
    expand_objects,
    extract_physical_tables,
    is_sql_protocol,
    resolve_semantic_objects,
    source_dialects,
    split_table,
    tables_agree,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.dependencies.loader import ModelDependencyLoader

logger = logging.getLogger(__name__)

# Hard ceiling on log rows examined per window, for both routes.
_MAX_LOG_ROWS = 5000
# How many consecutive all-miss windows one scan press will advance through
# before giving up and reporting more_remaining. Bounds the work of a single
# request while still guaranteeing forward progress past a block of log rows
# that resolve to no model table.
_MAX_SCAN_WINDOWS = 4

# NOTE: prefix kept as ``/impact`` for API back-compat (frontend client depends
# on it); the feature is named "Usage & Downstream Assets" in the UI and docs.
router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/impact",
    tags=["usage-downstream-assets"],
)


def _not_found(msg: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


async def _get_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise _not_found("Model not found")
    return model


def _hash_query(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _extract_column_occurrences(raw_query: str) -> list[tuple[Optional[str], str]]:
    """Extract EVERY column reference from a query, qualifier intact (Bug-8074).

    Returns one ``(qualifier_lower_or_None, column_lower)`` entry per occurrence,
    in AST order — multiplicity preserved, table/alias identity preserved.

    Why not a set of bare names (the previous shape): flattening to a set of
    lowercase names destroyed the two things the caller needs.

      * Identity — ``orders.region`` and ``customers.region`` became the same
        entry, so per-table usage could be attributed to the wrong table.
      * Multiplicity — a column referenced three times in one query counted once,
        so a "total occurrences" figure was really a per-query count.

    The qualifier is whatever the author wrote (a table name OR a query alias);
    :func:`_resolve_table_aliases` maps it back to a physical table.

    Best-effort: returns an empty list on parse failure, unchanged.
    """
    try:
        parsed = sqlglot.parse(raw_query, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        return []
    occurrences: list[tuple[Optional[str], str]] = []
    for statement in parsed:
        if statement is None:
            continue
        for col in statement.find_all(exp.Column):
            col_name = col.name
            if not col_name:
                continue
            qualifier = col.table or ""
            occurrences.append(
                (qualifier.lower() if qualifier else None, col_name.lower())
            )
    return occurrences


def _resolve_table_aliases(raw_query: str) -> dict[str, str]:
    """Map every qualifier a column reference may use to the table it names.

    ``FROM sales.orders AS o`` yields ``o -> sales.orders``, ``orders ->
    sales.orders`` and ``sales.orders -> sales.orders``, so ``o.region`` and
    ``orders.region`` resolve to the same table.

    The VALUE keeps the SCHEMA-QUALIFIED spelling the query used (Bug-8474 shape
    3). Storing only the bare name erased a contradicting schema, so
    ``FROM other_schema.orders AS o`` was attributed to the model's
    ``demo_data.orders``.

    Names declared as CTEs are excluded (Bug-8474 shape 1). ``find_all(exp.Table)``
    yields a CTE reference in the outer FROM exactly like a real table, so
    ``WITH orders AS (SELECT ... FROM other_stuff) SELECT o.region FROM orders o``
    was counted as usage of the model's ``orders`` table.

    Best-effort: an unparseable query yields an empty map, and an unresolvable
    qualifier is then treated as "not this model's usage" by the caller rather
    than being silently attributed to whichever model table happens to share the
    column name — which is the Bug-8074 failure mode.
    """
    try:
        parsed = sqlglot.parse(raw_query, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        return {}
    aliases: dict[str, str] = {}
    for statement in parsed:
        if statement is None:
            continue
        cte_names = {
            str(cte.alias_or_name).lower()
            for cte in statement.find_all(exp.CTE)
            if cte.alias_or_name
        }
        for tbl in statement.find_all(exp.Table):
            physical = (tbl.name or "").lower()
            if not physical or physical in cte_names:
                continue
            schema = (getattr(tbl, "db", "") or "").lower()
            qualified = f"{schema}.{physical}" if schema else physical
            aliases[physical] = qualified
            if schema:
                aliases[qualified] = qualified
            alias = tbl.alias
            if alias:
                aliases[alias.lower()] = qualified
    return aliases


# Sentinel ``table_lower`` for a column reference that cannot be attributed to a
# single model table (unqualified, and carried by more than one table). NOT the
# empty string: ``table_lower`` is derived from ``ModelTable.physical_name``, so
# an empty physical_name would collide with the sentinel and take the ambiguous
# output branch with an empty candidate set — a StopIteration escaping as a 500.
# The wire contract still reports an ambiguous row as ``table_name: ""``.
_AMBIGUOUS_TABLE = "\x00ambiguous"


def aggregate_column_usage(
    entries: list[tuple[str, Optional[datetime]]],
    col_map: dict[tuple[str, str], tuple[str, str]],
    tables_by_col: dict[str, set[str]],
    *,
    stable_column_ids_by_index: list[Optional[list[str]]] | None = None,
    columns_by_id: dict[str, tuple[str, str]] | None = None,
    closure_column_ids_by_index: list[Optional[list[str]]] | None = None,
    sql_protocol_by_index: list[bool] | None = None,
) -> tuple[int, int, list[ColumnUsageItem]]:
    """Count column usage per (table, column) over the scanned log window.

    Pure function — no DB, no request context — so the counting contract can be
    asserted against known answers rather than through a mocked session.

    ``entries``        (raw_query, created_at) pairs, newest-first or not.
    ``col_map``        (table_lower, col_lower) -> (canonical column, physical table)
    ``tables_by_col``  col_lower -> {table_lower, ...} that carry that column

    ``stable_column_ids_by_index``  per entry, the binder's directly bound
        ModelColumn ids, or None for a legacy log with no bind trace.
    ``sql_protocol_by_index``       per entry, whether ``raw`` is SQL sqlglot can
        be trusted to read. False suppresses the whole legacy TEXT path for that
        entry: sqlglot turns a DAX statement into bogus identifiers, so a model
        with a physical column named ``evaluate`` would be credited usage by
        ``EVALUATE SUMMARIZECOLUMNS(...)``, which names no such column. Defaults
        to True so existing callers and tests keep the previous behaviour.
    ``closure_column_ids_by_index`` per entry, the ModelColumn ids reached by
        expanding semantic objects that bound to no physical column — a
        calculated measure's inputs, a UDA's source columns (Bug-8483). One id
        per (object, role) expansion, so multiplicity matches the direct path.

    Returns ``(parsed_count, skipped_count, items)``.

    Attribution rules (legacy text path only):
      * A QUALIFIED reference resolves its qualifier through the query's own
        alias map, then must name a model table that actually carries the column;
        otherwise it is not this model's usage (a CTE or foreign-schema alias).
      * An UNQUALIFIED reference is attributed only when exactly one model table
        carries the column name. When several do, the query text genuinely does
        not say which — it is reported as ambiguous, never charged to one.

    Counting rules:
      * ``hit_count`` counts bound semantic references for stable-ID logs and
        SQL token occurrences for legacy parsed logs.
      * ``query_count`` counts distinct queries.
    """
    col_hits: dict[tuple[str, str], int] = defaultdict(int)
    col_queries: dict[tuple[str, str], int] = defaultdict(int)
    col_last_seen: dict[tuple[str, str], object] = {}
    ambiguous_candidates: dict[tuple[str, str], set[str]] = defaultdict(set)
    parsed_count = 0
    skipped_count = 0

    def _credit(key, created_at, matched_in_query: set) -> None:
        col_hits[key] += 1
        if key not in matched_in_query:
            matched_in_query.add(key)
            col_queries[key] += 1
        if created_at and (
            key not in col_last_seen or created_at > col_last_seen[key]
        ):
            col_last_seen[key] = created_at

    def _at(series, index):
        if series is None or index >= len(series):
            return None
        return series[index]

    def _disambiguate(
        col_lower: str,
        carriers: set[str],
        closure_table_by_col: dict[str, str],
        ambiguous: dict[tuple[str, str], set[str]],
    ) -> Optional[tuple[str, str]]:
        """Resolve a text-ambiguous column through the semantic closure.

        The query text does not say which of several same-named tables a bare
        column belongs to, but the model definition behind the semantic field
        the query actually named does.

        Returns None when the closure knows this column name but attributes it
        to MORE than one table. The token occurrence is then dropped entirely
        and the closure loop owns the attribution — crediting both would count
        one reference twice AND leave a phantom "ambiguous" row next to the
        precise rows that resolved it, which is the Bug-8474 trust failure one
        layer up.

        When the closure names a table the qualifier does NOT allow, neither
        source can be trusted over the other, so the reference falls back to the
        ambiguous key rather than being silently dropped — an under-report is
        the dangerous direction for this feature.
        """
        resolved = closure_table_by_col.get(col_lower)
        if resolved is not None and resolved != _AMBIGUOUS_TABLE:
            if resolved in carriers:
                return (resolved, col_lower)
        elif resolved == _AMBIGUOUS_TABLE:
            return None
        key = (_AMBIGUOUS_TABLE, col_lower)
        ambiguous[key] |= carriers
        return key

    # A real corpus repeats the same SQL text thousands of times (BI clients
    # re-issue identical queries) and sqlglot parsing dominates this function:
    # measured 5000 modelx log rows over 48 distinct texts, 1.97s unmemoized vs
    # 0.018s memoized. Both parses are on the request's synchronous path.
    occurrence_cache: dict[str, list[tuple[Optional[str], str]]] = {}
    alias_cache: dict[str, dict[str, str]] = {}

    def _occurrences_for(raw: str) -> list[tuple[Optional[str], str]]:
        cached = occurrence_cache.get(raw)
        if cached is None:
            cached = _extract_column_occurrences(raw)
            occurrence_cache[raw] = cached
        return cached

    def _aliases_for(raw: str) -> dict[str, str]:
        cached = alias_cache.get(raw)
        if cached is None:
            cached = _resolve_table_aliases(raw)
            alias_cache[raw] = cached
        return cached

    for entry_index, (raw, created_at) in enumerate(entries):
        stable_ids = _at(stable_column_ids_by_index, entry_index)
        closure_ids = _at(closure_column_ids_by_index, entry_index)
        if stable_ids is not None:
            # New logs carry binder-produced stable IDs. An empty list is still
            # authoritative (for example COUNT(*) with no physical column), so
            # do not fall back to name guessing for it.
            parsed_count += 1
            matched_in_query: set[tuple[str, str]] = set()
            for column_id in list(stable_ids) + list(closure_ids or []):
                resolved = (columns_by_id or {}).get(str(column_id))
                if resolved is None:
                    continue
                canonical, table_name = resolved
                key = ((table_name or "").lower(), canonical.lower())
                _credit(key, created_at, matched_in_query)
            continue

        is_sql = True
        if sql_protocol_by_index is not None and entry_index < len(sql_protocol_by_index):
            is_sql = bool(sql_protocol_by_index[entry_index])
        occurrences = _occurrences_for(raw or "") if is_sql else []
        if not occurrences and not closure_ids:
            skipped_count += 1
            continue
        parsed_count += 1
        alias_map = _aliases_for(raw or "")
        matched_in_query = set()
        # Physical columns this legacy query's SEMANTIC names resolved to. Used
        # first to DISAMBIGUATE the token scan below: when the text alone cannot
        # say which of two same-named tables a bare column belongs to, the
        # model's own definitions can. Only a column whose name maps to exactly
        # one table in the closure disambiguates; the rest stay ambiguous.
        closure_table_by_col: dict[str, str] = {}
        for column_id in closure_ids or []:
            resolved = (columns_by_id or {}).get(str(column_id))
            if resolved is None:
                continue
            canonical, table_name = resolved
            name = canonical.lower()
            table = (table_name or "").lower()
            if closure_table_by_col.setdefault(name, table) != table:
                closure_table_by_col[name] = _AMBIGUOUS_TABLE
        for qualifier, col_lower in occurrences:
            candidate_tables = tables_by_col.get(col_lower)
            if not candidate_tables:
                continue  # not a column of this model
            if qualifier is not None:
                resolved_ref = alias_map.get(qualifier)
                if resolved_ref is None:
                    # Bug-8474 shape 2: the qualifier names nothing in this
                    # query's FROM (``SELECT orders.region FROM customers``).
                    # The old ``alias_map.get(q, q)`` fallback let it through and
                    # bare-matched a model table that the query never touched.
                    continue
                ref_schema, ref_table = split_table(resolved_ref)
                matches = [
                    t for t in candidate_tables
                    if tables_agree(t, ref_schema, ref_table)
                ]
                if not matches:
                    # Qualified against something that is not a model table
                    # carrying this column (a CTE, a subquery alias, another
                    # schema's table). Not this model's column usage.
                    continue
                if len(matches) == 1:
                    key = (matches[0], col_lower)
                else:
                    # Two model tables share this bare name in different
                    # schemas; the reference does not say which. Report it,
                    # unless the semantic closure already resolved it exactly.
                    key = _disambiguate(
                        col_lower, set(matches), closure_table_by_col, ambiguous_candidates,
                    )
            elif len(candidate_tables) == 1:
                key = (next(iter(candidate_tables)), col_lower)
            else:
                key = _disambiguate(
                    col_lower, candidate_tables, closure_table_by_col, ambiguous_candidates,
                )
            if key is None:
                # The closure owns this name in this query; see _disambiguate.
                continue
            _credit(key, created_at, matched_in_query)

        # Bug-8471 (b): a legacy raw_query names SEMANTIC fields, so when the
        # semantic name differs from the physical column name the token scan
        # above sees nothing. The caller resolved those names to physical
        # columns through the model definitions; credit whatever the token scan
        # did not already account for in THIS query. Text multiplicity wins
        # where it works (three mentions are three references); the closure only
        # fills genuine blanks, so a model whose semantic and physical names
        # coincide keeps exactly the counts it had before.
        for column_id in closure_ids or []:
            resolved = (columns_by_id or {}).get(str(column_id))
            if resolved is None:
                continue
            canonical, table_name = resolved
            key = ((table_name or "").lower(), canonical.lower())
            if key in matched_in_query:
                continue
            _credit(key, created_at, matched_in_query)

    items: list[ColumnUsageItem] = []
    for key in sorted(col_hits.keys()):
        table_lower, col_lower = key
        if table_lower == _AMBIGUOUS_TABLE:
            carriers = ambiguous_candidates[key]
            # sorted(), not next(iter()): set order is not stable across runs and
            # the canonical spelling shown to the modeller must be.
            canonical = col_map[(sorted(carriers)[0], col_lower)][0]
            items.append(ColumnUsageItem(
                column_name=canonical,
                table_name="",
                hit_count=col_hits[key],
                query_count=col_queries[key],
                last_seen_at=col_last_seen.get(key),
                ambiguous=True,
                candidate_tables=sorted(
                    col_map[(t, col_lower)][1] for t in carriers
                ),
            ))
            continue
        canonical, table_name = col_map[key]
        items.append(ColumnUsageItem(
            column_name=canonical,
            table_name=table_name,
            hit_count=col_hits[key],
            query_count=col_queries[key],
            last_seen_at=col_last_seen.get(key),
            ambiguous=False,
            candidate_tables=[],
        ))
    return parsed_count, skipped_count, items


# ---------------------------------------------------------------------------
# Bind-trace loading (the stable producer contract)
# ---------------------------------------------------------------------------


class _BindTraces:
    """Per-log stable references read off the bind ``RouteLog``.

    ``direct[log_id]`` is None for a LEGACY log — one written before the
    producer contract existed, which is ~94% of the shipped demo corpus. An
    empty list means "the binder ran and bound no physical column", which is
    authoritative and must not fall back to name guessing.
    """

    def __init__(self) -> None:
        self.direct: dict[str, list[str]] = {}
        self.objects: dict[str, list[tuple[str, str]]] = {}

    def has_contract(self, log_id: str) -> bool:
        return log_id in self.direct


async def _load_bind_traces(db, log_ids: list) -> _BindTraces:
    traces = _BindTraces()
    if not log_ids:
        return traces
    rows = (
        await db.execute(
            select(RouteLog.query_log_id, RouteLog.detail).where(
                RouteLog.query_log_id.in_(log_ids),
                RouteLog.route_stage == "bind",
            )
        )
    ).all()
    for query_log_id, detail in rows:
        if not isinstance(detail, dict) or "column_usage_refs" not in detail:
            continue
        key = str(query_log_id)
        refs = detail.get("column_usage_refs")
        bucket = traces.direct.setdefault(key, [])
        if isinstance(refs, list):
            bucket.extend(
                str(ref["column_id"])
                for ref in refs
                if isinstance(ref, dict) and ref.get("column_id")
            )
        # Bug-8483: semantic objects the binder could not express as a physical
        # column. Absent on logs written by an older producer, which is fine —
        # they simply contribute no closure expansion.
        objects = detail.get("semantic_object_refs")
        if isinstance(objects, list):
            object_bucket = traces.objects.setdefault(key, [])
            object_bucket.extend(
                (str(obj.get("object_type") or ""), str(obj.get("object_id") or ""))
                for obj in objects
                if isinstance(obj, dict) and obj.get("object_id")
            )
    return traces


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/scan",
    response_model=ImpactScanResponse,
    dependencies=[require_role("modeler")],
)
async def run_impact_scan(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ImpactScanResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)

        # Bug-8471: the model's tables AND the connector each is read from.
        # ``rewritten_query`` is written in the SOURCE dialect, so a BigQuery
        # model logs backtick-quoted identifiers that sqlglot's default dialect
        # cannot see at all.
        table_rows = (
            await db.execute(
                select(ModelTable.physical_name, DataSource.source_type)
                .join(DataSource, ModelTable.source_id == DataSource.id)
                .where(ModelTable.model_id == model_id)
            )
        ).all()
        table_names = {name.lower() for name, _source_type in table_rows if name}
        dialects = source_dialects({source_type for _name, source_type in table_rows})

        if not table_names:
            return ImpactScanResponse(
                references_upserted=0, tables_matched=0, columns_matched=0,
            )

        # Bug-8471: column id -> owning physical table, the join that turns a
        # stable binder reference into a table-usage fact without looking at any
        # SQL text.
        col_rows = (
            await db.execute(
                select(ModelColumn.id, ModelTable.physical_name)
                .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
                .where(ModelTable.model_id == model_id)
            )
        ).all()
        table_by_column_id = {
            str(column_id): (physical_name or "").lower()
            for column_id, physical_name in col_rows
            if physical_name
        }

        # F-030-10: only count log rows newer than the last-processed watermark.
        # The watermark is the max last_seen_at already recorded for this model;
        # re-running the scan on an unchanged log therefore counts nothing
        # (idempotent), instead of re-incrementing every recorded (table, hash)
        # pair on each press of the scan button.
        watermark = (
            await db.execute(
                select(func.max(GatewayQueryReference.last_seen_at))
                .where(GatewayQueryReference.model_id == model_id)
            )
        ).scalar_one_or_none()

        snapshot = await ModelDependencyLoader(db).load(project_id, model_id)
        closure = build_semantic_closure(snapshot)
        name_index = build_semantic_name_index(snapshot)

        matched_tables: set[str] = set()
        matched_columns: set[str] = set()
        # (queried_table, query_hash) -> accumulated evidence, so one UPDATE per
        # distinct pair replaces one SELECT+UPDATE per (log row, table). The
        # scan window is up to 5000 logs; per-row round trips made the first
        # non-empty scan unusable.
        pending: dict[tuple[str, str], dict] = {}
        # A real corpus repeats the same SQL text thousands of times (BI clients
        # re-issue identical queries). sqlglot parsing dominates the scan cost,
        # so memoize both derivations by text.
        semantic_cache: dict[str, frozenset[str]] = {}
        physical_cache: dict[str, frozenset[str]] = {}

        def _semantic_columns(raw: str) -> frozenset[str]:
            cached = semantic_cache.get(raw)
            if cached is None:
                cached = frozenset(columns_for_objects(
                    resolve_semantic_objects(raw, name_index), closure,
                ))
                semantic_cache[raw] = cached
            return cached

        def _physical_model_tables(rewritten: str) -> frozenset[str]:
            cached = physical_cache.get(rewritten)
            if cached is None:
                found = {
                    model_table
                    for schema, table in extract_physical_tables(rewritten, dialects)
                    for model_table in table_names
                    if tables_agree(model_table, schema, table)
                }
                cached = frozenset(found)
                physical_cache[rewritten] = cached
            return cached

        def _examine(log_entry) -> None:
            log_id = str(log_entry.id)
            column_ids: set[str] = set()
            if traces.has_contract(log_id):
                column_ids |= set(traces.direct.get(log_id, []))
                column_ids |= columns_for_objects(traces.objects.get(log_id, []), closure)
            elif is_sql_protocol(getattr(log_entry, "protocol", None)):
                # Legacy log: resolve the semantic names in raw_query through the
                # model's own definitions (Bug-8471 fix (b)). Only for protocols
                # whose raw_query is SQL — sqlglot turns a DAX statement into
                # bogus identifiers, which would invent usage.
                column_ids |= _semantic_columns(log_entry.raw_query or "")

            tables = {
                table_by_column_id[cid]
                for cid in column_ids
                if cid in table_by_column_id
            }
            matched_columns.update(c for c in column_ids if c in table_by_column_id)

            # The physical SQL that actually ran names the model tables it
            # touched, including a table joined only for a security predicate or
            # traversed by a join path — the ONLY source for those, on modern and
            # legacy logs alike. An accelerated query names the aggregate/pocket
            # target instead, so it contributes nothing there rather than a false
            # match (Bug-8471 fix (a)).
            tables |= _physical_model_tables(log_entry.rewritten_query or "")

            if not tables:
                return
            matched_tables.update(tables)
            qhash = _hash_query(log_entry.raw_query or "")
            for tname in tables:
                record = pending.setdefault(
                    (tname, qhash),
                    {"count": 0, "last": None, "user": log_entry.user_identity},
                )
                record["count"] += 1
                if log_entry.created_at and (
                    record["last"] is None or log_entry.created_at > record["last"]
                ):
                    record["last"] = log_entry.created_at

        # The scan resumes from ``watermark`` and reads a bounded window. The
        # watermark only advances from RECORDED usage, so a window in which
        # nothing matched would leave it where it was and every later press would
        # re-read the same rows forever, making everything past that block
        # permanently unreachable. Keep advancing through empty windows within
        # this request instead, bounded so one button press cannot run away.
        logs_scanned = 0
        more_remaining = False
        cursor = watermark
        for _window in range(_MAX_SCAN_WINDOWS):
            log_stmt = (
                select(QueryLog)
                .where(QueryLog.model_id == model_id, QueryLog.status == "success")
            )
            if cursor is not None:
                log_stmt = log_stmt.where(QueryLog.created_at > cursor)
            logs = (
                await db.execute(
                    log_stmt.order_by(QueryLog.created_at.asc()).limit(_MAX_LOG_ROWS)
                )
            ).scalars().all()
            if not logs:
                more_remaining = False
                break
            logs_scanned += len(logs)
            more_remaining = len(logs) == _MAX_LOG_ROWS
            traces = await _load_bind_traces(db, [entry.id for entry in logs])
            for log_entry in logs:
                _examine(log_entry)
            cursor = logs[-1].created_at
            if pending:
                break

        # ``more_remaining`` means "pressing Run scan again will examine NEW
        # rows". That is only true if this pass actually recorded something:
        # the resume point is the newest RECORDED usage, so a pass that matched
        # nothing leaves it where it was and the next press re-reads the exact
        # same rows. Claiming otherwise sends the modeller round a loop that
        # cannot terminate. ``logs_scanned`` still reports the real work done,
        # so "we read N rows and none of them touched this model" is told
        # honestly instead of as "0 tables checked".
        more_remaining = more_remaining and bool(pending)

        upserted = 0
        if pending:
            existing_rows = (
                await db.execute(
                    select(GatewayQueryReference).where(
                        GatewayQueryReference.model_id == model_id,
                        GatewayQueryReference.query_text_hash.in_(
                            {qhash for _t, qhash in pending}
                        ),
                    )
                )
            ).scalars().all()
            existing_by_key = {
                (row.queried_table, row.query_text_hash): row for row in existing_rows
            }
            for (tname, qhash), record in pending.items():
                existing = existing_by_key.get((tname, qhash))
                if existing is not None:
                    # Bug-5847: atomic hit_count increment via SQL expression to
                    # eliminate the read-modify-write race that occurs when two
                    # concurrent scans both read the same hit_count, increment in
                    # Python, and write back.
                    update_values: dict = {
                        "hit_count": GatewayQueryReference.hit_count + record["count"],
                    }
                    # F-030-10: never move last_seen_at backwards.
                    if record["last"] is not None and (
                        existing.last_seen_at is None
                        or record["last"] > existing.last_seen_at
                    ):
                        update_values["last_seen_at"] = record["last"]
                    await db.execute(
                        update(GatewayQueryReference)
                        .where(GatewayQueryReference.id == existing.id)
                        .values(**update_values)
                    )
                else:
                    row = GatewayQueryReference(
                        model_id=model_id,
                        queried_table=tname,
                        query_user=record["user"],
                        query_text_hash=qhash,
                        hit_count=record["count"],
                    )
                    # last_seen_at is NOT NULL with a server default; assigning
                    # None explicitly would render a NULL and fail the insert.
                    if record["last"] is not None:
                        row.last_seen_at = record["last"]
                    db.add(row)
                    upserted += 1

        await db.commit()
        return ImpactScanResponse(
            references_upserted=upserted,
            tables_matched=len(matched_tables),
            columns_matched=len(matched_columns),
            logs_scanned=logs_scanned,
            more_remaining=more_remaining,
        )


@router.get(
    "/column-usage",
    response_model=ColumnUsageResponse,
    dependencies=[require_role("modeler")],
)
async def get_column_usage(
    project_id: UUID,
    model_id: UUID,
    limit: Optional[int] = Query(None, ge=1, le=_MAX_LOG_ROWS, description="Max log entries to parse."),
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ColumnUsageResponse:
    """Column-level usage extracted from the gateway query log (Bug-7458).

    Uses binder-produced stable ModelColumn IDs when present, expands the
    semantic objects that bound to no physical column through the model
    dependency closure (Bug-8483), and falls back to SQL parsing for legacy
    logs. Returns per-column reference and query counts.

    This is a read-only, best-effort analysis. Queries that fail to parse and
    resolve to nothing are silently skipped. It does NOT access the source
    database.
    """
    log_limit = min(limit or _MAX_LOG_ROWS, _MAX_LOG_ROWS)
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_model(db, project_id, model_id)

        # Load the model's known physical column names keyed by lowercase name,
        # along with the table physical_name for each column.
        col_stmt = (
            select(ModelColumn.id, ModelColumn.column_name, ModelTable.physical_name)
            .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
            .where(ModelTable.model_id == model_id)
        )
        col_rows = (await db.execute(col_stmt)).all()
        # Bug-8074: key by (table_lower, column_lower), NOT by column name alone.
        # Keying on the bare column name made a second table carrying the same
        # column (``id``, ``region``, ``created_at`` — ordinary in a star schema)
        # overwrite the first, so the endpoint could report usage of
        # ``customers.region`` as usage of ``orders.region`` and a modeller could
        # drop a column that is genuinely depended on.
        # (table_lower, col_lower) -> (canonical_column_name, table_physical_name)
        col_map: dict[tuple[str, str], tuple[str, str]] = {}
        columns_by_id: dict[str, tuple[str, str]] = {}
        # col_lower -> set of table_lower carrying it, for unqualified references.
        tables_by_col: dict[str, set[str]] = defaultdict(set)
        for column_id, col_name, table_name in col_rows:
            if not col_name:
                continue
            col_lower = col_name.lower()
            table_lower = (table_name or "").lower()
            col_map[(table_lower, col_lower)] = (col_name, table_name or "")
            columns_by_id[str(column_id)] = (col_name, table_name or "")
            tables_by_col[col_lower].add(table_lower)

        if not col_map:
            return ColumnUsageResponse(
                model_id=str(model_id),
                total_queries_parsed=0,
                total_queries_skipped=0,
                logs_available=0,
                truncated=False,
                columns=[],
            )

        # How much traffic EXISTS, not just how much was read. The endpoint only
        # examines the newest window, so without this a column referenced solely
        # in older traffic yields no row at all and the panel says "no column
        # usage found" — the modeller then drops a column thousands of queries
        # depend on. The scan route already discloses its window through
        # ``logs_scanned``/``more_remaining``; this is the same disclosure for the
        # tab that actually answers "is this column safe to drop".
        logs_available = (
            await db.execute(
                select(func.count())
                .select_from(QueryLog)
                .where(QueryLog.model_id == model_id, QueryLog.status == "success")
            )
        ).scalar_one_or_none() or 0

        log_stmt = (
            select(QueryLog)
            .where(QueryLog.model_id == model_id, QueryLog.status == "success")
            .order_by(QueryLog.created_at.desc())
            .limit(log_limit)
        )
        logs = (await db.execute(log_stmt)).scalars().all()
        traces = await _load_bind_traces(db, [entry.id for entry in logs])

        legacy_log_ids = [
            str(entry.id) for entry in logs if not traces.has_contract(str(entry.id))
        ]
        needs_closure = bool(legacy_log_ids) or any(traces.objects.values())
        closure: dict[tuple[str, str], frozenset[str]] = {}
        name_index: dict[str, set[tuple[str, str]]] = {}
        if needs_closure:
            # Only pay for the dependency snapshot when something actually needs
            # expanding — a corpus of fully-bound modern logs does not.
            snapshot = await ModelDependencyLoader(db).load(project_id, model_id)
            closure = build_semantic_closure(snapshot)
            name_index = build_semantic_name_index(snapshot)

        # Memoized by SQL text: a real corpus repeats identical queries many
        # thousands of times and sqlglot parsing dominates the request cost.
        semantic_cache: dict[str, list[str]] = {}

        def _semantic_columns(raw: str) -> list[str]:
            cached = semantic_cache.get(raw)
            if cached is None:
                cached = sorted(columns_for_objects(
                    resolve_semantic_objects(raw, name_index), closure,
                ))
                semantic_cache[raw] = cached
            return cached

        stable_ids_by_index: list[Optional[list[str]]] = []
        closure_ids_by_index: list[Optional[list[str]]] = []
        # Whether each entry's raw_query is SQL. Gates BOTH legacy text paths —
        # the semantic-name resolution here and the physical-token scan inside
        # aggregate_column_usage. Gating only one left the other free to mine a
        # DAX statement for bogus identifiers.
        sql_protocol_by_index: list[bool] = []
        for entry in logs:
            log_id = str(entry.id)
            entry_is_sql = is_sql_protocol(getattr(entry, "protocol", None))
            sql_protocol_by_index.append(entry_is_sql)
            if traces.has_contract(log_id):
                stable_ids_by_index.append(traces.direct.get(log_id, []))
                # expand_objects, not a set: two calculated measures reading the
                # same base column are two references, exactly as two directly
                # bound measures on that column are.
                closure_ids_by_index.append(
                    expand_objects(traces.objects.get(log_id, []), closure)
                )
            else:
                stable_ids_by_index.append(None)
                usable = bool(name_index) and entry_is_sql
                closure_ids_by_index.append(
                    _semantic_columns(entry.raw_query or "") if usable else []
                )

        parsed_count, skipped_count, items = aggregate_column_usage(
            [(entry.raw_query or "", entry.created_at) for entry in logs],
            col_map,
            tables_by_col,
            stable_column_ids_by_index=stable_ids_by_index,
            columns_by_id=columns_by_id,
            closure_column_ids_by_index=closure_ids_by_index,
            sql_protocol_by_index=sql_protocol_by_index,
        )

        return ColumnUsageResponse(
            model_id=str(model_id),
            total_queries_parsed=parsed_count,
            total_queries_skipped=skipped_count,
            logs_available=int(logs_available),
            truncated=int(logs_available) > len(logs),
            columns=items,
        )
