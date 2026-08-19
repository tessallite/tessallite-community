"""Connection-level introspection — discover/profile routed through the query-router.

Bug-6213: discover_tables, discover_columns, and profile_table were called
directly from the model-service via shared.source_introspection, violating
the gateway-only-data-access invariant.  These endpoints centralise
connection-level introspection through the query-router, matching the
model-scoped ``/introspect`` path that table_preview, calendar, hierarchies,
and data_quality/validator already use.

All three operations accept a ``connection_id`` + ``project_id`` (ownership
guard) and delegate to the existing ``shared.source_introspection`` dispatch
layer, which emits ``[SOURCE_AUDIT]`` lines for every source touch.

Bug-7167: connection-introspection endpoints now enforce project RBAC via
``ensure_project_model_access`` (min_role="modeler") matching the model-service
proxy gate. ``require_capability("explore")`` remains for embed-token gating
but is no longer the sole authorization control.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from shared.auth.middleware import CurrentUser, require_capability
from shared.auth.project_access import ensure_project_model_access
from shared.db.models import ProjectConnection
from shared.db.session import get_tenant_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connection-introspect"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class DiscoverTablesRequest(BaseModel):
    connection_id: str
    project_id: str
    schema_filter: str | None = None


class DiscoverColumnsRequest(BaseModel):
    connection_id: str
    project_id: str
    schema_name: str = Field(alias="schema")
    table: str

    model_config = {"populate_by_name": True}


class _ProfileTableRef(BaseModel):
    schema_: str | None = Field("public", alias="schema")
    table: str = Field(min_length=1)
    model_config = {"populate_by_name": True}


class ProfileRequest(BaseModel):
    connection_id: str
    project_id: str
    tables: list[_ProfileTableRef] = Field(default_factory=list)


class TestStoredConnectionRequest(BaseModel):
    connection_id: str
    project_id: str


class TestDraftConnectionRequest(BaseModel):
    project_id: str
    connection_type: str
    credentials: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class ProfileTableResult(BaseModel):
    schema_name: str = Field(alias="schema")
    table: str
    columns: list[dict[str, Any]]
    row_count: int

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_connection(
    db, connection_id: str, project_id: str,
    current_user: CurrentUser,
    *,
    min_role: str = "modeler",
) -> ProjectConnection:
    """Look up a ProjectConnection and verify ownership + caller RBAC.

    Fail-closed: if the connection does not exist or belongs to a different
    project the request is rejected (Bug-5325 precedent).

    Bug-7167: the caller's project-level role is now verified via
    ``ensure_project_model_access`` (min_role="modeler"), matching the
    model-service proxy gates on ``discover_tables``, ``discover_columns``,
    and ``profile_tables``. Previously only ``require_capability("explore")``
    was checked, which is a no-op for regular (non-embed) users, allowing
    any authenticated tenant user to introspect any connection.

    Bug-7592 [SECURITY — existence oracle]: every rejection path returns an
    IDENTICAL 404 ``Connection not found`` — whether the connection is absent,
    belongs to a different project, or the caller lacks the required project
    role. The previous code answered "absent" with 404 but "exists but you may
    not touch it" with 403, so an unauthorized caller could probe whether any
    given ``connection_id`` existed simply by observing 403-vs-404. Collapsing
    all three to the same status AND body removes that oracle. A legitimate
    authorized caller for the owning project is unaffected and still receives
    the real connection (and its data).
    """
    # Single canonical rejection so absent / cross-project / unauthorized are
    # indistinguishable to the caller.
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Connection not found",
    )

    # A malformed (non-UUID) id cannot correspond to any connection; treat it
    # as not-found too rather than leaking a distinguishable 500/400.
    try:
        conn_uuid = UUID(connection_id)
    except (ValueError, AttributeError, TypeError):
        raise not_found

    conn = await db.get(ProjectConnection, conn_uuid)
    if conn is None:
        raise not_found
    if str(conn.project_id) != project_id:
        raise not_found
    # Enforce that the calling user has at least modeler role on the project
    # that owns this connection. This closes the RBAC gap where any
    # authenticated tenant user could enumerate/profile any connection
    # by supplying a matching (connection_id, project_id) pair. Bug-7592: an
    # authorization denial collapses to the same 404 as an absent connection
    # so it cannot be used to confirm the connection exists. Non-authorization
    # errors (e.g. a 5xx) are re-raised unchanged.
    try:
        await ensure_project_model_access(
            db, current_user,
            project_id=conn.project_id,
            min_role=min_role,
        )
    except HTTPException as exc:
        if exc.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        ):
            raise not_found from exc
        raise
    return conn


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/introspect/connection/discover-tables")
async def discover_tables_via_connection(
    body: DiscoverTablesRequest,
    current_user: CurrentUser = Depends(require_capability("explore")),
) -> dict:
    """Return tables from the source database identified by *connection_id*.

    Response: ``{"tables": [{"schema", "table", "type"}], "truncated": bool}``.
    """
    from shared.source_introspection import (
        discover_tables as _discover,
        normalize_discover_payload,
    )

    async for db in get_tenant_db(current_user.tenant_id):
        conn = await _resolve_connection(db, body.connection_id, body.project_id, current_user)
        try:
            raw = await _discover(conn, schema=body.schema_filter, tenant_session=db)
            return normalize_discover_payload(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error(
                "discover_tables failed for connection %s: %s",
                body.connection_id, exc, exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to discover tables: {exc}",
            )
    return {"tables": [], "truncated": False}


@router.post("/introspect/connection/discover-columns")
async def discover_columns_via_connection(
    body: DiscoverColumnsRequest,
    current_user: CurrentUser = Depends(require_capability("explore")),
) -> list[dict]:
    """Return columns for a single table on the source identified by *connection_id*.

    Response: ``[{"column_name": str, "data_type": str, "is_nullable": bool}]``.
    """
    from shared.source_introspection import discover_columns as _discover

    async for db in get_tenant_db(current_user.tenant_id):
        conn = await _resolve_connection(db, body.connection_id, body.project_id, current_user)
        try:
            return await _discover(
                conn, schema=body.schema_name, table=body.table, tenant_session=db,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error(
                "discover_columns failed for connection %s: %s",
                body.connection_id, exc, exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to discover columns: {exc}",
            )
    return []


@router.post("/introspect/connection/profile")
async def profile_tables_via_connection(
    body: ProfileRequest,
    current_user: CurrentUser = Depends(require_capability("explore")),
) -> list[dict]:
    """Profile tables on the source identified by *connection_id*.

    Returns raw column metadata and row counts; the model-service applies
    classification / role-suggestion logic on top.

    Response: ``[{"schema": str, "table": str, "columns": [...], "row_count": int}]``.
    """
    from shared.source_introspection import profile_table

    if not body.tables:
        return []

    from shared.source_introspection import is_truncation_profile_ref
    for tbl in body.tables:
        schema = tbl.schema_ or "public"
        if is_truncation_profile_ref(schema, tbl.table):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Truncation markers are not real tables and cannot be profiled.",
            )

    # Bug-7157: enforce the same table-count cap at the query-router level
    # that the model-service proxy applies, so direct callers cannot bypass
    # the batch-size guard. The model-service cap (PROFILE_MAX_TABLES, default
    # 25) is the canonical limit; read independently so this boundary is
    # self-contained.
    try:
        max_tables = int(os.getenv("PROFILE_MAX_TABLES", "25"))
    except (TypeError, ValueError):
        max_tables = 25
    if max_tables > 0 and len(body.tables) > max_tables:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Profile request exceeds the maximum of {max_tables} tables "
                f"per call ({len(body.tables)} requested). Split the request "
                f"into smaller batches."
            ),
        )

    async for db in get_tenant_db(current_user.tenant_id):
        conn = await _resolve_connection(db, body.connection_id, body.project_id, current_user)
        results: list[dict] = []
        try:
            for tbl in body.tables:
                schema = tbl.schema_ or "public"
                columns, row_count = await profile_table(
                    conn, schema=schema, table=tbl.table, tenant_session=db,
                )
                results.append({
                    "schema": schema,
                    "table": tbl.table,
                    "columns": columns,
                    "row_count": row_count,
                })
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error(
                "profile failed for connection %s: %s",
                body.connection_id, exc, exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to profile tables: {exc}",
            )
        return results
    return []


@router.post("/introspect/connection/test")
async def test_stored_connection(
    body: TestStoredConnectionRequest,
    current_user: CurrentUser = Depends(require_capability("explore")),
) -> dict:
    """Test stored credentials through the query-router (F-014-04).

    Model-service must not open the customer's source from ``/test``.
    Admin on the owning project — same bar as model-service ``require_role("admin")``.
    """
    from shared.source_introspection import test_connection as _shared_test

    async for db in get_tenant_db(current_user.tenant_id):
        conn = await _resolve_connection(
            db, body.connection_id, body.project_id, current_user,
            min_role="admin",
        )
        try:
            ok, detail = await _shared_test(conn, tenant_session=db)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error(
                "test_connection failed for connection %s: %s",
                body.connection_id, exc, exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to test connection: {exc}",
            )
        if ok:
            return {"ok": True}
        return {"ok": False, "detail": detail}
    return {"ok": False, "detail": "No tenant session"}


@router.post("/introspect/connection/test-draft")
async def test_draft_connection(
    body: TestDraftConnectionRequest,
    current_user: CurrentUser = Depends(require_capability("explore")),
) -> dict:
    """Test unsaved / merged credentials through the query-router (F-014-04)."""
    from shared.source_introspection import test_connection_raw

    try:
        project_uuid = UUID(body.project_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid project_id")

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_project_model_access(
            db, current_user,
            project_id=project_uuid,
            min_role="admin",
        )
        try:
            ok, detail = await test_connection_raw(
                body.connection_type, body.credentials, body.config or {},
                tenant_session=db,
                project_id=project_uuid,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error(
                "test_connection_raw failed for draft on project %s: %s",
                body.project_id, exc, exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to test connection: {exc}",
            )
        if ok:
            return {"ok": True}
        return {"ok": False, "detail": detail}
    return {"ok": False, "detail": "No tenant session"}
