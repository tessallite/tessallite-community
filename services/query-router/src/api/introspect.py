"""Raw SQL introspection endpoint — preview / member-sampling queries.

model-service uses this to run physical-table queries (table preview,
hierarchy member sampling) through the query-router instead of calling
execute_source_sql directly.  The endpoint does NOT do semantic binding
or aggregate routing — it exists so that all source-database access is
centrally audited and executed by the query-router.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

import sqlglot
from sqlglot import exp

from shared.auth.middleware import (
    CurrentUser,
    enforce_model_scope,
    require_capability,
)
from shared.auth.project_access import load_authorized_model
from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection,
)
from shared.db.models import DataSource, ProjectConnection, QueryLog
from shared.db.session import get_tenant_db
from shared.source_executor import QueryTimeoutError, execute_source_sql

logger = logging.getLogger(__name__)

router = APIRouter(tags=["introspect"])

_MUTATING_NODE_TYPES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
    exp.Merge, exp.Alter, exp.Command, exp.Transaction,
    exp.Commit, exp.Rollback, exp.Set, exp.Into, exp.Lock,
)


def _contains_mutating_node(tree: exp.Expression) -> str | None:
    """Walk the full AST and return a description if any mutating node is found."""
    for node in tree.walk():
        if isinstance(node, _MUTATING_NODE_TYPES):
            return type(node).__name__
        if isinstance(node, exp.Select) and node.args.get("into"):
            return "SELECT INTO"
        if isinstance(node, exp.Lock):
            return "locking read"
    return None


def _assert_read_only(raw_sql: str) -> None:
    """Reject any SQL that is not a purely read-only SELECT/WITH statement."""
    try:
        stmts = sqlglot.parse(raw_sql, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unable to parse introspection SQL",
        )
    if not stmts or stmts[0] is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Empty introspection SQL",
        )
    if len(stmts) > 1 and any(s is not None for s in stmts[1:]):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Introspection allows only a single statement",
        )
    stmt = stmts[0]
    kind = stmt.key.upper()
    if kind not in ("SELECT", "WITH"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Introspection only allows read-only queries, got {kind}",
        )
    mutating = _contains_mutating_node(stmt)
    if mutating:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Introspection SQL contains mutating construct: {mutating}",
        )


class IntrospectRequest(BaseModel):
    model_id: str
    raw_sql: str
    source_id: str | None = None


class IntrospectBatchItem(BaseModel):
    key: str
    raw_sql: str


class IntrospectBatchRequest(BaseModel):
    model_id: str
    queries: list[IntrospectBatchItem]
    source_id: str | None = None


class IntrospectResponse(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    execution_ms: int


class IntrospectBatchResultItem(BaseModel):
    key: str
    columns: list[str]
    rows: list[dict[str, Any]]
    execution_ms: int
    error: str | None = None


class IntrospectBatchResponse(BaseModel):
    results: list[IntrospectBatchResultItem]


async def _resolve_model_connection(
    db, model_id: str, source_id: str | None = None,
    *, expected_project_id=None,
) -> tuple[ProjectConnection | None, str | None]:
    if source_id:
        source = await db.get(DataSource, UUID(source_id))
        if source is None or str(source.model_id) != model_id:
            return None, "Specified data source not found for this model."
    else:
        source = (
            await db.execute(
                select(DataSource).where(DataSource.model_id == UUID(model_id)).limit(1)
            )
        ).scalar_one_or_none()
        if source is None:
            return None, "No data source configured for this model."
    # Bug-5325: read-time fail-closed defense via the shared resolver (single
    # source of truth shared with the model-service read sites and the
    # normal-query / aggregate-target / pocket-target execution paths). A
    # legacy/imported source row whose project_connection_id points at a
    # connection in a DIFFERENT project must NOT be used to execute
    # introspection against another project's source. expected_project_id is the
    # model's project (from load_authorized_model in the caller).
    if expected_project_id is None:
        # Defensive: callers always pass the model's project_id. Refuse to
        # resolve a connection without a project to validate against rather
        # than fall back to an unchecked lookup.
        return None, "Cannot resolve source connection without model project."
    try:
        conn = await resolve_endpoint_connection(
            db, source, expected_project_id=expected_project_id
        )
    except CrossProjectConnectionError:
        return None, "Source connection belongs to a different project."
    except ValueError:
        return None, "Project connection for model source was not found."
    return conn, None


async def _log_introspect(
    db, model_id: str, user_identity: str,
    raw_sql: str, execution_ms: int, rows_returned: int,
    *,
    error_type: str | None = None,
    error_detail: str | None = None,
) -> None:
    """Persist a QueryLog row for an introspection query.

    Bug-5323: accepts optional ``error_type`` / ``error_detail`` so
    failed introspect queries are recorded with ``status='error'``
    instead of the default ``'success'``.
    """
    try:
        log_status = "error" if error_type else "success"
        entry = QueryLog(
            model_id=UUID(model_id),
            user_identity=user_identity,
            protocol="introspect",
            raw_query=raw_sql[:4000],
            query_fingerprint="",
            route_type="introspect",
            execution_ms=execution_ms,
            rows_returned=rows_returned,
            bytes_processed=0,
            status=log_status,
            error_type=error_type,
            error_detail=(error_detail or "")[:4000] if error_detail else None,
        )
        db.add(entry)
        await db.commit()
    except Exception:
        await db.rollback()


@router.post("/introspect", response_model=IntrospectResponse)
async def introspect_query(
    body: IntrospectRequest,
    current_user: CurrentUser = Depends(require_capability("explore")),
) -> IntrospectResponse:
    enforce_model_scope(current_user, body.model_id)
    _assert_read_only(body.raw_sql)

    async for db in get_tenant_db(current_user.tenant_id):
        model = await load_authorized_model(
            db,
            current_user,
            model_id=body.model_id,
            min_role="viewer",
        )
        conn_obj, err = await _resolve_model_connection(
            db, body.model_id, source_id=body.source_id,
            expected_project_id=model.project_id,
        )
        if err or conn_obj is None:
            raise HTTPException(status_code=422, detail=err or "Connection not found")

        t0 = time.monotonic()
        try:
            rows, columns = await execute_source_sql(
                conn_obj, body.raw_sql, tenant_session=db,
            )
        except QueryTimeoutError as exc:
            # Bug-5323: log the failure BEFORE raising so the timeout
            # leaves an audit trail in QueryLog.
            execution_ms = int((time.monotonic() - t0) * 1000)
            await _log_introspect(
                db, body.model_id, current_user.email,
                body.raw_sql, execution_ms, 0,
                error_type="timeout",
                error_detail=str(exc),
            )
            raise HTTPException(status_code=504, detail=str(exc))
        except Exception as exc:
            # Bug-5323: log execution errors before raising.
            execution_ms = int((time.monotonic() - t0) * 1000)
            await _log_introspect(
                db, body.model_id, current_user.email,
                body.raw_sql, execution_ms, 0,
                error_type="execution_error",
                error_detail=str(exc),
            )
            raise HTTPException(
                status_code=502, detail=f"Introspect query failed: {exc}",
            )
        execution_ms = int((time.monotonic() - t0) * 1000)

        await _log_introspect(
            db, body.model_id, current_user.email,
            body.raw_sql, execution_ms, len(rows),
        )

        return IntrospectResponse(
            columns=columns, rows=rows, execution_ms=execution_ms,
        )


@router.post("/introspect/batch", response_model=IntrospectBatchResponse)
async def introspect_batch(
    body: IntrospectBatchRequest,
    current_user: CurrentUser = Depends(require_capability("explore")),
) -> IntrospectBatchResponse:
    enforce_model_scope(current_user, body.model_id)
    if len(body.queries) > 20:
        raise HTTPException(
            status_code=422,
            detail="Batch introspect limited to 20 queries per request.",
        )
    for item in body.queries:
        _assert_read_only(item.raw_sql)

    async for db in get_tenant_db(current_user.tenant_id):
        model = await load_authorized_model(
            db,
            current_user,
            model_id=body.model_id,
            min_role="viewer",
        )
        conn_obj, err = await _resolve_model_connection(
            db, body.model_id, source_id=body.source_id,
            expected_project_id=model.project_id,
        )
        if err or conn_obj is None:
            raise HTTPException(status_code=422, detail=err or "Connection not found")

        results: list[IntrospectBatchResultItem] = []
        for item in body.queries:
            t0 = time.monotonic()
            row_count = 0
            error_type: str | None = None
            error_detail: str | None = None
            try:
                rows, columns = await execute_source_sql(
                    conn_obj, item.raw_sql, tenant_session=db,
                )
                execution_ms = int((time.monotonic() - t0) * 1000)
                row_count = len(rows)
                results.append(IntrospectBatchResultItem(
                    key=item.key,
                    columns=columns,
                    rows=rows,
                    execution_ms=execution_ms,
                ))
            except Exception as exc:
                execution_ms = int((time.monotonic() - t0) * 1000)
                # Bug-5323: classify the error for QueryLog.
                error_type = (
                    "timeout"
                    if isinstance(exc, QueryTimeoutError)
                    else "execution_error"
                )
                error_detail = str(exc)
                results.append(IntrospectBatchResultItem(
                    key=item.key,
                    columns=[],
                    rows=[],
                    execution_ms=execution_ms,
                    error=str(exc),
                ))

            await _log_introspect(
                db, body.model_id, current_user.email,
                item.raw_sql, execution_ms, row_count,
                error_type=error_type,
                error_detail=error_detail,
            )

        return IntrospectBatchResponse(results=results)
