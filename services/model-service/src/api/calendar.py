"""Calendar table provisioning routes (Phase RA-1+, dimension aliases).

Calendars are no longer "bound" at the source level. Instead each
physical calendar table is registered as a ``CalendarTable`` row owned
by the source, and a companion ``ModelTable`` alias is created so the
calendar can participate in joins and queries like any other dimension.

Multiple calendars per source are supported. Time-variant measures pick
their calendar by selecting one of the ModelTable aliases (see
``Measure.calendar_model_table_id``).

Endpoints (rooted under a source the modeller already owns):

  GET    .../calendars                       → list calendars on this source
  POST   .../calendars/script                → DDL string for the chosen dialect
  POST   .../calendars/auto-create           → execute DDL + register + alias
  POST   .../calendars/bind                  → register existing table + alias
  PUT    .../calendars/{calendar_id}         → update column meanings
  DELETE .../calendars/{calendar_id}         → delete calendar (alias survives)

The dialect for a calendar must match the dialect of the source's
project_connection (postgresql / bigquery / hadoop_spark). The
auto-create path is only available when the source has write access;
the script path always works and lets the modeller run the DDL
themselves and then call /bind.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from shared.config.settings import get_settings
from shared.db.models import (
    CalendarTable,
    DataSource,
    Model,
    ModelColumn,
    ModelTable,
    ProjectConnection,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import CalendarTableResponse
from shared.semantic.calendar_dialects import (
    CALENDAR_COLUMN_SETS,
    CALENDAR_DIALECTS,
    STANDARD_COLUMNS,
    emit_calendar_ddl,
)
from shared.semantic.calendar_types import CALENDAR_TYPES, normalize_calendar_type
from shared.source_executor import DDL_CAPABLE_CONNECTORS
from src.api._scope import resolve_source_connection
from src.api._table_qualify import qualify_physical_name
from src.api.hierarchies import (
    _auto_create_date_hierarchies_for_model,
    _ensure_model_in_project,
    _introspect_batch_via_router,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/sources/{source_id}/calendars",
    tags=["calendar"],
)


_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DEFAULT_ALIAS_BASE = "calendar"
_CALENDAR_COLUMN_TYPES: dict[str, str] = {
    "date_column": "date",
    "year_column": "integer",
    "half_column": "integer",
    "quarter_column": "integer",
    "month_column": "integer",
    "week_column": "integer",
    "day_column": "integer",
}

_CALENDAR_SLOT_PATTERNS: dict[str, re.Pattern] = {
    "date_column": re.compile(r"date.?key|calendar.?date|date.?id|^date$|^dt$", re.I),
    "year_column": re.compile(r"year", re.I),
    "half_column": re.compile(r"half|semester", re.I),
    "quarter_column": re.compile(r"quarter|qtr", re.I),
    "month_column": re.compile(r"month", re.I),
    "week_column": re.compile(r"week", re.I),
    "day_column": re.compile(r"day.?no|day.?of|day.?num|^day$", re.I),
}


def _detect_calendar_columns(
    columns: list["ModelColumn"],
) -> dict[str, str | None]:
    """Match column names to calendar slots by standard names then patterns."""
    result: dict[str, str | None] = {s: None for s in _CALENDAR_COLUMN_TYPES}
    name_to_col = {c.column_name.lower(): c.column_name for c in columns}

    for slot, std_name in STANDARD_COLUMNS.items():
        if slot in result and std_name.lower() in name_to_col:
            result[slot] = name_to_col[std_name.lower()]

    for slot, pattern in _CALENDAR_SLOT_PATTERNS.items():
        if result[slot] is not None:
            continue
        for col in columns:
            if pattern.search(col.column_name):
                result[slot] = col.column_name
                break

    if result["date_column"] is None:
        for col in columns:
            dtype = (col.data_type or "").lower()
            if dtype in ("date", "timestamp", "timestamptz", "datetime",
                         "timestamp with time zone", "timestamp without time zone"):
                result["date_column"] = col.column_name
                break

    return result


async def auto_register_calendar_from_classification(
    db,
    table: "ModelTable",
) -> "CalendarTable | None":
    """Create a CalendarTable when a ModelTable is classified as calendar.

    Called by ``tables.update_table`` when ``table_type`` changes to
    ``"calendar"``.  Auto-detects column mappings from the table's
    ModelColumns.  Idempotent — reuses an existing CalendarTable if one
    exists for the same (source, physical_name).
    """
    source = await db.get(DataSource, table.source_id)
    if source is None:
        return None
    # Bug-5325: resolve the connection fail-closed — a legacy/imported source
    # whose connection points at another project must NOT be used to classify
    # a calendar against the wrong project's dialect. Derive the source's
    # owning project from the table's model.
    model = await db.get(Model, table.model_id)
    if model is None:
        return None
    connection = await resolve_source_connection(
        db, source, expected_project_id=model.project_id
    )

    conn_type = (connection.connection_type or "").lower()
    if conn_type in CALENDAR_DIALECTS:
        dialect = conn_type
    elif conn_type == "jdbc":
        dialect = "hadoop_spark"
    else:
        dialect = "postgresql"

    existing = (
        await db.execute(
            select(CalendarTable).where(
                CalendarTable.data_source_id == source.id,
                CalendarTable.table_name == table.physical_name,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        table.calendar_table_id = existing.id
        await db.flush()
        return existing

    cols = (
        await db.execute(
            select(ModelColumn).where(ModelColumn.model_table_id == table.id)
        )
    ).scalars().all()

    col_map = _detect_calendar_columns(cols)

    cal = CalendarTable(
        data_source_id=source.id,
        table_name=table.physical_name,
        dialect=dialect,
        autocreated=False,
        **col_map,
    )
    db.add(cal)
    await db.flush()

    table.calendar_table_id = cal.id
    await db.flush()
    return cal


# ---------------------------------------------------------------------------
# Request / response shapes specific to this router
# ---------------------------------------------------------------------------


class CalendarScriptRequest(BaseModel):
    table_name: str = Field(max_length=512)
    start_date: date
    end_date: date
    calendar_type: str = Field(default="standard")
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)


class CalendarScriptResponse(BaseModel):
    dialect: str
    table_name: str
    ddl: str
    standard_columns: dict[str, str]


class CalendarAutoCreateRequest(BaseModel):
    table_name: str = Field(max_length=512)
    start_date: date = Field(default_factory=lambda: date(2020, 1, 1))
    end_date: date = Field(default_factory=lambda: date.today() + timedelta(days=365 * 3))
    alias: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Alias for the ModelTable created alongside the calendar. Auto-sequenced from 'calendar' when omitted. Must match ^[a-z][a-z0-9_]*$.",
    )
    display_name: Optional[str] = Field(default=None, max_length=255)
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    calendar_type: str = Field(default="standard")


class CalendarBindRequest(BaseModel):
    table_name: str = Field(max_length=512)
    dialect: Optional[str] = Field(default=None, description="Defaults to the source's connection dialect when omitted")
    date_column: Optional[str] = None
    year_column: Optional[str] = None
    half_column: Optional[str] = None
    quarter_column: Optional[str] = None
    month_column: Optional[str] = None
    week_column: Optional[str] = None
    day_column: Optional[str] = None
    alias: Optional[str] = Field(default=None, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=255)
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    calendar_type: str = Field(default="standard")


class CalendarUpdateRequest(BaseModel):
    date_column: Optional[str] = None
    year_column: Optional[str] = None
    half_column: Optional[str] = None
    quarter_column: Optional[str] = None
    month_column: Optional[str] = None
    week_column: Optional[str] = None
    day_column: Optional[str] = None
    fiscal_year_start_month: Optional[int] = Field(default=None, ge=1, le=12)
    calendar_type: Optional[str] = None


class CalendarCoverageResponse(BaseModel):
    """F-016-23: result of comparing a fact date range against the calendar's
    date range. ``covered`` is False when any fact rows fall outside the
    calendar range — those rows would LEFT JOIN to NULL period values and drop
    silently from period rollups, the silent-wrong-number class the
    architecture doc's near-term item #1 calls out."""

    covered: bool
    calendar_min: Optional[str] = None
    calendar_max: Optional[str] = None
    fact_min: Optional[str] = None
    fact_max: Optional[str] = None
    # "below" (facts predate the calendar), "above" (facts run past it),
    # "both", or None when fully covered / indeterminate.
    gap: Optional[str] = None
    warning: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_source_with_connection(
    db, source_id: UUID, model_id: UUID, *, project_id: UUID
) -> tuple[DataSource, ProjectConnection]:
    source = await db.get(DataSource, source_id)
    if source is None or source.model_id != model_id:
        raise HTTPException(status_code=404, detail="DataSource not found")
    # Bug-5325: fail closed when the source's connection belongs to another
    # project (legacy/imported malformed row). project_id is the source's
    # owning project — every caller runs _ensure_model_in_project first.
    connection = await resolve_source_connection(
        db, source, expected_project_id=project_id
    )
    return source, connection


def _normalise_dialect(connection_type: str) -> str:
    if connection_type in CALENDAR_DIALECTS:
        return connection_type
    if connection_type == "jdbc":
        return "hadoop_spark"
    raise HTTPException(
        status_code=400,
        detail=f"Connection type {connection_type!r} has no calendar dialect",
    )


async def _execute_ddl(connection: ProjectConnection, ddl: str) -> None:
    from shared.source_executor import execute_source_ddl
    await execute_source_ddl(connection, ddl)


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1]
    cookie = request.cookies.get("access_token")
    if cookie:
        return cookie
    raise HTTPException(status_code=401, detail="Bearer token required")


async def _verify_table_exists(
    qualified_name: str,
    connection: ProjectConnection,
    *,
    model_id: UUID,
    source_id: UUID,
    bearer: str,
) -> None:
    """Raise HTTPException(404) if the table does not exist on the source.

    The existence probe runs through the query-router ``/introspect``
    endpoint so that all source-database access is centrally executed,
    audited (QueryLog), and routed — never issued directly from
    model-service against the source connection.
    """
    from shared.connector_qualify import quote_table_ref, transpile_preview_sql
    from shared.schemas.connection_type import normalize_connection_type

    connector = normalize_connection_type((connection.connection_type or "").lower())
    pg_quoted = quote_table_ref("postgresql", qualified_name)
    canonical = f"SELECT 1 AS chk FROM {pg_quoted} LIMIT 1"
    sql = transpile_preview_sql(connector, canonical)

    _settings = get_settings()
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/introspect"
    headers = {"Authorization": f"Bearer {bearer}"}
    body = {
        "model_id": str(model_id),
        "raw_sql": sql,
        "source_id": str(source_id),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=404,
                detail=f"Table {qualified_name!r} does not exist on the source database.",
            )
        rows = resp.json().get("rows", [])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"Table {qualified_name!r} does not exist on the source database.",
        )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"Table {qualified_name!r} does not exist on the source database.",
        )


def _validate_calendar_type(calendar_type: str | None) -> str | None:
    """Normalise and validate a calendar_type against the canonical 6-type set.

    F-016-11: ``bind`` / ``update`` previously persisted any string (e.g.
    ``"banana"``), which downstream ``CALENDAR_COLUMN_SETS.get(...)`` /
    expression-capability checks then silently coerced toward standard-ish
    behaviour. Returns the normalised type; raises 400 on an unknown value.
    """
    normalized = normalize_calendar_type(calendar_type)
    if normalized is not None and normalized not in CALENDAR_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"calendar_type {calendar_type!r} is invalid. "
                f"Must be one of {sorted(CALENDAR_TYPES)}."
            ),
        )
    return normalized


def _validate_alias(alias: str) -> None:
    if not _ALIAS_RE.match(alias):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Alias {alias!r} is invalid. Must start with a lowercase "
                "letter and contain only lowercase letters, digits, and "
                "underscores."
            ),
        )


async def _next_alias(db, model_id: UUID, base: str) -> str:
    """Return ``base`` if no ModelTable in the model already uses it,
    otherwise ``base_2``, ``base_3``, ... — same scheme as tables.py.

    The read-then-insert has a check-then-act race window; Bug-5246
    adds an IntegrityError retry in ``_create_calendar_alias`` so a
    concurrent duplicate alias triggers a re-pick rather than a
    silent duplication or crash.
    """
    existing = (
        await db.execute(
            select(ModelTable.alias).where(ModelTable.model_id == model_id)
        )
    ).scalars().all()
    used = set(existing)
    if base not in used:
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


async def _create_calendar_alias(
    db,
    *,
    model_id: UUID,
    source_id: UUID,
    calendar: CalendarTable,
    alias: Optional[str],
    display_name: Optional[str],
) -> ModelTable:
    """Create a ModelTable alias for ``calendar`` and pre-populate one
    ModelColumn per non-NULL standard column on the calendar.

    Returns the newly created ModelTable (already flushed)."""
    # If a ModelTable with the same physical table already exists in this model
    # (e.g. the user added it via Discover Tables before registering the calendar),
    # reuse that row instead of creating a duplicate. BQ names can be 3-part
    # (project.dataset.table) while Discover Tables produces 2-part (dataset.table),
    # so check both forms.
    parts = calendar.table_name.split(".")
    short_phys = ".".join(parts[-2:]) if len(parts) >= 3 else calendar.table_name
    existing_mt = (
        await db.execute(
            select(ModelTable)
            .where(
                ModelTable.model_id == model_id,
                or_(
                    ModelTable.physical_name == calendar.table_name,
                    ModelTable.physical_name == short_phys,
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing_mt is not None:
        existing_mt.calendar_table_id = calendar.id
        existing_mt.table_type = "calendar"
        await db.flush()
        return existing_mt

    if alias:
        _validate_alias(alias)
        existing_alias = (
            await db.execute(
                select(func.count()).where(
                    ModelTable.model_id == model_id,
                    ModelTable.alias == alias,
                )
            )
        ).scalar() or 0
        if existing_alias:
            raise HTTPException(
                status_code=409,
                detail=f"Alias {alias!r} is already in use within this model.",
            )
        chosen_alias = alias
    else:
        chosen_alias = await _next_alias(db, model_id, _DEFAULT_ALIAS_BASE)

    # Bug-5246: wrap alias allocation + flush in a SAVEPOINT-protected
    # retry loop so a concurrent duplicate alias triggers a re-pick
    # rather than a crash or silent duplication.  Using begin_nested()
    # ensures only the failed alias INSERT is rolled back; the parent
    # transaction's prior work (e.g. a CalendarTable flush in
    # auto_create_calendar or bind_calendar) survives intact.
    _MAX_ALIAS_RETRIES = 3
    for _attempt in range(_MAX_ALIAS_RETRIES):
        table = ModelTable(
            model_id=model_id,
            source_id=source_id,
            table_type="calendar",
            physical_name=calendar.table_name,
            alias=chosen_alias,
            display_name=display_name or chosen_alias.replace("_", " ").title(),
            calendar_table_id=calendar.id,
        )
        try:
            async with db.begin_nested():
                db.add(table)
                await db.flush()
            break
        except IntegrityError:
            if alias:
                # User-supplied alias collision — do not retry with a
                # different one; report the conflict.
                raise HTTPException(
                    status_code=409,
                    detail=f"Alias {alias!r} is already in use within this model.",
                )
            chosen_alias = await _next_alias(db, model_id, _DEFAULT_ALIAS_BASE)
    else:
        raise HTTPException(
            status_code=409,
            detail="Could not allocate a unique calendar alias after retries.",
        )

    for slot, type_token in _CALENDAR_COLUMN_TYPES.items():
        column_name = getattr(calendar, slot, None)
        if not column_name:
            continue
        db.add(
            ModelColumn(
                model_table_id=table.id,
                column_name=column_name,
                display_name=column_name.replace("_", " ").title(),
                data_type=type_token,
                is_nullable=False,
            )
        )
    await db.flush()
    return table


# ---------------------------------------------------------------------------
# GET — list calendars on this source
# ---------------------------------------------------------------------------


@router.get("", response_model=list[CalendarTableResponse], dependencies=[require_role("viewer")])
async def list_calendars(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[CalendarTableResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        source = await db.get(DataSource, source_id)
        if source is None or source.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")
        # Bug-5244: include auto-created calendars in the list response
        # so the UI can display them. The `autocreated` flag is preserved
        # on each row so callers can still distinguish if needed.
        rows = (
            await db.execute(
                select(CalendarTable)
                .where(CalendarTable.data_source_id == source_id)
                .order_by(CalendarTable.created_at)
            )
        ).scalars().all()
        return [CalendarTableResponse.model_validate(c) for c in rows]


# ---------------------------------------------------------------------------
# POST /script — emit DDL without executing it
# ---------------------------------------------------------------------------


@router.post(
    "/script",
    response_model=CalendarScriptResponse,
    dependencies=[require_role("modeler")],
)
async def emit_calendar_script(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    body: CalendarScriptRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CalendarScriptResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        source, connection = await _load_source_with_connection(db, source_id, model_id, project_id=project_id)
        dialect = _normalise_dialect(connection.connection_type)
        calendar_type = _validate_calendar_type(body.calendar_type) or "standard"
        qualified_name = qualify_physical_name(body.table_name, connection, source)
        try:
            ddl = emit_calendar_ddl(
                dialect, qualified_name, body.start_date, body.end_date,
                fiscal_year_start_month=body.fiscal_year_start_month,
                calendar_type=calendar_type,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        columns = CALENDAR_COLUMN_SETS.get(calendar_type, STANDARD_COLUMNS)
        return CalendarScriptResponse(
            dialect=dialect,
            table_name=qualified_name,
            ddl=ddl,
            standard_columns=dict(columns),
        )


# ---------------------------------------------------------------------------
# POST /auto-create — emit DDL, execute it, register the calendar, create alias
# ---------------------------------------------------------------------------


@router.post(
    "/auto-create",
    response_model=CalendarTableResponse,
    dependencies=[require_role("modeler")],
)
async def auto_create_calendar(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    body: CalendarAutoCreateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CalendarTableResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        source, connection = await _load_source_with_connection(db, source_id, model_id, project_id=project_id)
        dialect = _normalise_dialect(connection.connection_type)
        calendar_type = _validate_calendar_type(body.calendar_type) or "standard"
        qualified_name = qualify_physical_name(body.table_name, connection, source)
        columns = CALENDAR_COLUMN_SETS.get(calendar_type, STANDARD_COLUMNS)
        try:
            ddl = emit_calendar_ddl(
                dialect, qualified_name, body.start_date, body.end_date,
                fiscal_year_start_month=body.fiscal_year_start_month,
                calendar_type=calendar_type,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        executor_available = (connection.config or {}).get("write_access", False)
        if not executor_available:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail={
                    "message": (
                        "The connection does not have 'Allow Tessallite to run DDL "
                        "on this source' enabled. Edit the connection settings and "
                        "enable this option, then try again. Alternatively, run the "
                        "DDL below on your data source manually and use the Bind "
                        "existing tab to register the table."
                    ),
                    "ddl": ddl,
                },
            )

        if dialect not in DDL_CAPABLE_CONNECTORS:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail={
                    "message": (
                        f"Auto-create executor for dialect {dialect!r} is not "
                        "wired; use Get script + Bind existing."
                    ),
                    "ddl": ddl,
                },
            )

        try:
            await _execute_ddl(connection, ddl)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": f"Calendar DDL execution failed on source: {exc}",
                    "ddl": ddl,
                },
            ) from exc

        # Idempotent: re-pressing Generate with the same (source, table_name)
        # refreshes the existing calendar instead of duplicating.
        existing = (
            await db.execute(
                select(CalendarTable).where(
                    CalendarTable.data_source_id == source.id,
                    CalendarTable.table_name == qualified_name,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.dialect = dialect
            existing.date_column = columns.get("date_column")
            existing.year_column = columns.get("year_column")
            existing.half_column = columns.get("half_column")
            existing.quarter_column = columns.get("quarter_column")
            existing.month_column = columns.get("month_column")
            existing.week_column = columns.get("week_column")
            existing.day_column = columns.get("day_column")
            existing.autocreated = True
            existing.fiscal_year_start_month = body.fiscal_year_start_month
            existing.calendar_type = calendar_type
            cal = existing
            await db.flush()
            # Ensure the calendar has at least one alias in this model.
            alias_count = (
                await db.execute(
                    select(func.count()).where(
                        ModelTable.model_id == model_id,
                        ModelTable.calendar_table_id == cal.id,
                    )
                )
            ).scalar() or 0
            if alias_count == 0:
                await _create_calendar_alias(
                    db,
                    model_id=model_id,
                    source_id=source.id,
                    calendar=cal,
                    alias=body.alias,
                    display_name=body.display_name,
                )
        else:
            cal = CalendarTable(
                data_source_id=source.id,
                table_name=qualified_name,
                dialect=dialect,
                date_column=columns.get("date_column"),
                year_column=columns.get("year_column"),
                half_column=columns.get("half_column"),
                quarter_column=columns.get("quarter_column"),
                month_column=columns.get("month_column"),
                week_column=columns.get("week_column"),
                day_column=columns.get("day_column"),
                autocreated=True,
                fiscal_year_start_month=body.fiscal_year_start_month,
                calendar_type=calendar_type,
            )
            db.add(cal)
            await db.flush()
            await _create_calendar_alias(
                db,
                model_id=model_id,
                source_id=source.id,
                calendar=cal,
                alias=body.alias,
                display_name=body.display_name,
            )

        await db.commit()
        await db.refresh(cal)

        # Auto-create date hierarchies for unassigned fact-table date columns.
        # Locate the alias we just created/confirmed for this calendar.
        alias_mt_row = (
            await db.execute(
                select(ModelTable)
                .where(
                    ModelTable.model_id == model_id,
                    ModelTable.calendar_table_id == cal.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        auto_created_aliases: list[str] = []
        if alias_mt_row is not None:
            try:
                _, _, auto_created_aliases = await _auto_create_date_hierarchies_for_model(
                    db,
                    model_id=model_id,
                    calendar_model_table_id=alias_mt_row.id,
                )
                await db.commit()
            except Exception as _exc:
                logger.warning("auto_create_date_hierarchies failed for model %s: %s", model_id, _exc)

        resp = CalendarTableResponse.model_validate(cal)
        resp.auto_created_aliases = auto_created_aliases
        return resp


# ---------------------------------------------------------------------------
# POST /bind — register an existing physical calendar table
# ---------------------------------------------------------------------------


@router.post(
    "/bind",
    response_model=CalendarTableResponse,
    dependencies=[require_role("modeler")],
)
async def bind_calendar(
    request: Request,
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    body: CalendarBindRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CalendarTableResponse:
    bearer = _extract_bearer(request)

    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        source, connection = await _load_source_with_connection(db, source_id, model_id, project_id=project_id)
        dialect = body.dialect or _normalise_dialect(connection.connection_type)
        if dialect not in CALENDAR_DIALECTS:
            raise HTTPException(status_code=400, detail=f"Unsupported dialect {dialect!r}")
        calendar_type = _validate_calendar_type(body.calendar_type)
        if not (body.date_column or body.year_column):
            raise HTTPException(
                status_code=400,
                detail="At least one of date_column or year_column must be provided",
            )

        qualified_name = qualify_physical_name(body.table_name, connection, source)
        await _verify_table_exists(
            qualified_name, connection,
            model_id=model_id, source_id=source.id, bearer=bearer,
        )

        # Idempotent: if a CalendarTable for this (source, table_name) already
        # exists, update its column mappings rather than creating a duplicate.
        existing = (
            await db.execute(
                select(CalendarTable).where(
                    CalendarTable.data_source_id == source.id,
                    CalendarTable.table_name == qualified_name,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.dialect = dialect
            existing.date_column = body.date_column
            existing.year_column = body.year_column
            existing.half_column = body.half_column
            existing.quarter_column = body.quarter_column
            existing.month_column = body.month_column
            existing.week_column = body.week_column
            existing.day_column = body.day_column
            existing.autocreated = False
            existing.fiscal_year_start_month = body.fiscal_year_start_month
            existing.calendar_type = calendar_type
            cal = existing
            await db.flush()
            alias_count = (
                await db.execute(
                    select(func.count()).where(
                        ModelTable.model_id == model_id,
                        ModelTable.calendar_table_id == cal.id,
                    )
                )
            ).scalar() or 0
            if alias_count == 0:
                alias_mt = await _create_calendar_alias(
                    db,
                    model_id=model_id,
                    source_id=source.id,
                    calendar=cal,
                    alias=body.alias,
                    display_name=body.display_name,
                )
            else:
                alias_mt = (
                    await db.execute(
                        select(ModelTable)
                        .where(
                            ModelTable.model_id == model_id,
                            ModelTable.calendar_table_id == cal.id,
                        )
                        .limit(1)
                    )
                ).scalar_one()
        else:
            cal = CalendarTable(
                data_source_id=source.id,
                table_name=qualified_name,
                dialect=dialect,
                date_column=body.date_column,
                year_column=body.year_column,
                half_column=body.half_column,
                quarter_column=body.quarter_column,
                month_column=body.month_column,
                week_column=body.week_column,
                day_column=body.day_column,
                autocreated=False,
                fiscal_year_start_month=body.fiscal_year_start_month,
                calendar_type=calendar_type,
            )
            db.add(cal)
            await db.flush()
            alias_mt = await _create_calendar_alias(
                db,
                model_id=model_id,
                source_id=source.id,
                calendar=cal,
                alias=body.alias,
                display_name=body.display_name,
            )
        await db.commit()
        await db.refresh(cal)

        auto_created_aliases: list[str] = []
        try:
            _, _, auto_created_aliases = await _auto_create_date_hierarchies_for_model(
                db,
                model_id=model_id,
                calendar_model_table_id=alias_mt.id,
            )
            await db.commit()
        except Exception as _exc:
            logger.warning("auto_create_date_hierarchies failed for model %s: %s", model_id, _exc)

        resp = CalendarTableResponse.model_validate(cal)
        resp.auto_created_aliases = auto_created_aliases
        return resp


# ---------------------------------------------------------------------------
# GET /{calendar_id}/coverage — fact-vs-calendar date-range validation
# (F-016-23, architecture_multi-calendar.md near-term item #1)
# ---------------------------------------------------------------------------


def _build_minmax_sql(connector: str, *, table_name: str, date_col: str) -> str:
    """MIN/MAX of a date column, canonical-PG then transpiled to the source
    dialect. All identifiers are connector-quoted; no raw interpolation of
    table/column names beyond the quoter (the inputs are model-defined names,
    not user free-text)."""
    from shared.connector_qualify import (
        quote_identifier,
        quote_table_ref,
        transpile_preview_sql,
    )

    pg_table = quote_table_ref("postgresql", table_name)
    pg_col = quote_identifier("postgresql", date_col)
    canonical = (
        f"SELECT MIN({pg_col}) AS lo, MAX({pg_col}) AS hi FROM {pg_table}"
    )
    return transpile_preview_sql(connector, canonical)


@router.get(
    "/{calendar_id}/coverage",
    response_model=CalendarCoverageResponse,
    dependencies=[require_role("viewer")],
)
async def check_calendar_coverage(
    request: Request,
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    calendar_id: UUID,
    fact_table: str,
    fact_date_column: str,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CalendarCoverageResponse:
    """Compare the calendar's date range against a fact table's actual data
    range (F-016-23).

    Both MIN/MAX probes run through the query-router ``/introspect/batch``
    surface so every source-DB read is centrally routed and audited — never
    issued directly from model-service. The check is read-only and advisory:
    it returns ``covered=False`` with a human-readable ``warning`` when fact
    rows fall outside the calendar range so the UI can alert before those rows
    silently drop to NULL period values in a LEFT JOIN.
    """
    from shared.schemas.connection_type import normalize_connection_type

    bearer = _extract_bearer(request)
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        source, connection = await _load_source_with_connection(db, source_id, model_id, project_id=project_id)
        cal = await db.get(CalendarTable, calendar_id)
        if cal is None or cal.data_source_id != source_id:
            raise HTTPException(status_code=404, detail="Calendar not found")
        if not cal.date_column:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Coverage validation needs a date_column on the calendar; "
                    "this calendar maps only component columns."
                ),
            )

        connector = normalize_connection_type((connection.connection_type or "").lower())
        cal_qualified = cal.table_name  # already source-qualified at bind time
        fact_qualified = qualify_physical_name(fact_table, connection, source)

        queries = [
            ("cal", _build_minmax_sql(connector, table_name=cal_qualified, date_col=cal.date_column)),
            ("fact", _build_minmax_sql(connector, table_name=fact_qualified, date_col=fact_date_column)),
        ]
        results = await _introspect_batch_via_router(str(model_id), queries, bearer)

        def _range(key: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
            rows, _cols, err = results.get(key, ([], [], "no result"))
            if err:
                return None, None, err
            if not rows:
                return None, None, None
            row = rows[0]
            lo = row.get("lo")
            hi = row.get("hi")
            return (None if lo is None else str(lo), None if hi is None else str(hi), None)

        cal_lo, cal_hi, cal_err = _range("cal")
        fact_lo, fact_hi, fact_err = _range("fact")

        if cal_err or fact_err:
            # Probe failure is not a coverage failure; report indeterminate.
            return CalendarCoverageResponse(
                covered=True,
                calendar_min=cal_lo, calendar_max=cal_hi,
                fact_min=fact_lo, fact_max=fact_hi,
                gap=None,
                warning=(
                    "Coverage could not be determined: "
                    f"{cal_err or fact_err}"
                ),
            )

        # No fact rows (or no calendar rows) → nothing to validate.
        if fact_lo is None or fact_hi is None or cal_lo is None or cal_hi is None:
            return CalendarCoverageResponse(
                covered=True,
                calendar_min=cal_lo, calendar_max=cal_hi,
                fact_min=fact_lo, fact_max=fact_hi,
            )

        below = fact_lo < cal_lo
        above = fact_hi > cal_hi
        if not below and not above:
            return CalendarCoverageResponse(
                covered=True,
                calendar_min=cal_lo, calendar_max=cal_hi,
                fact_min=fact_lo, fact_max=fact_hi,
            )
        gap = "both" if below and above else ("below" if below else "above")
        parts = []
        if below:
            parts.append(f"fact rows from {fact_lo} predate the calendar start {cal_lo}")
        if above:
            parts.append(f"fact rows to {fact_hi} run past the calendar end {cal_hi}")
        warning = (
            "Some fact dates fall outside the calendar range ("
            + "; ".join(parts)
            + "). Those rows produce NULL period values and drop silently from "
            "period rollups. Extend the calendar to cover the fact range."
        )
        return CalendarCoverageResponse(
            covered=False,
            calendar_min=cal_lo, calendar_max=cal_hi,
            fact_min=fact_lo, fact_max=fact_hi,
            gap=gap, warning=warning,
        )

    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---------------------------------------------------------------------------
# PUT /{calendar_id} — update column meanings on an existing calendar
# ---------------------------------------------------------------------------


@router.put(
    "/{calendar_id}",
    response_model=CalendarTableResponse,
    dependencies=[require_role("modeler")],
)
async def update_calendar(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    calendar_id: UUID,
    body: CalendarUpdateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CalendarTableResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        # F-016-12: verify the source belongs to this model/project before
        # mutating, matching the chain every sibling endpoint applies.
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        cal = await db.get(CalendarTable, calendar_id)
        if cal is None or cal.data_source_id != source_id:
            raise HTTPException(status_code=404, detail="CalendarTable not found")
        await _load_source_with_connection(db, source_id, model_id, project_id=project_id)

        updates = body.model_dump(exclude_unset=True)

        # F-016-11: validate calendar_type and refuse a type change on an
        # auto-created calendar — the physical table's columns encode the old
        # type's semantics, so the type must be changed by regenerating the
        # table (auto-create), not by re-labelling the registration.
        if "calendar_type" in updates:
            new_type = _validate_calendar_type(updates["calendar_type"])
            updates["calendar_type"] = new_type
            if (
                cal.autocreated
                and new_type is not None
                and new_type != cal.calendar_type
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Changing calendar_type on an auto-created calendar would "
                        "leave the physical table's columns inconsistent with the "
                        "new type. Regenerate the calendar (Generate) with the new "
                        "type instead of editing it here."
                    ),
                )

        for k, v in updates.items():
            setattr(cal, k, v)
        await db.commit()
        await db.refresh(cal)
        return CalendarTableResponse.model_validate(cal)


# ---------------------------------------------------------------------------
# DELETE /{calendar_id} — drop the registration; aliases survive with NULL
# ---------------------------------------------------------------------------


@router.delete(
    "/{calendar_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_calendar(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    calendar_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        # F-016-12: verify source-to-model/project chain before deleting.
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await _load_source_with_connection(db, source_id, model_id, project_id=project_id)
        cal = await db.get(CalendarTable, calendar_id)
        if cal is None or cal.data_source_id != source_id:
            return
        await db.delete(cal)
        await db.commit()
