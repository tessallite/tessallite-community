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
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from shared.config.settings import get_settings
from shared.db.models import (
    AggregateDefinition,
    CalendarTable,
    DataSource,
    Dimension,
    Measure,
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
from shared.semantic.calendar_types import (
    CALENDAR_TYPES,
    available_calendar_types,
    is_available_calendar_type,
    normalize_calendar_type,
)
from shared.source_executor import DDL_CAPABLE_CONNECTORS
from shared.source_table_probe import (
    SourceProbeUnavailableError,
    SourceTableNotFoundError,
    verify_source_table_exists,
)
from src.api._model_lock import acquire_model_definition_lock
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
# Bug-7206: strict identifier pattern for calendar table names. Each dotted
# segment must be a valid SQL identifier (alphanumeric, underscores, hyphens).
# Semicolons, quotes, comments, and other SQL metacharacters are rejected at the
# API boundary before they can reach the DDL emitter.
_TABLE_NAME_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")
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
_CALENDAR_COLUMN_FIELDS = tuple(_CALENDAR_COLUMN_TYPES.keys())

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


def _validate_table_name(table_name: str) -> None:
    """Bug-7206: reject table names with SQL metacharacters at the API boundary.

    Accepts dotted names (schema.table, project.dataset.table) where each
    segment is a valid SQL identifier. Raises HTTPException 422 on invalid
    names.
    """
    if not table_name or not table_name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="table_name must not be empty",
        )
    segments = table_name.split(".")
    for segment in segments:
        if not segment or not _TABLE_NAME_SEGMENT_RE.match(segment):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Invalid table name segment: {segment!r}. Each part of "
                    "the table name must start with a letter or underscore "
                    "and contain only letters, digits, underscores, or hyphens."
                ),
            )


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


# Bug-7216: maximum allowed date span for calendar generation (~100 years).
_MAX_CALENDAR_SPAN_DAYS = 36525


class CalendarScriptRequest(BaseModel):
    table_name: str = Field(max_length=512)
    start_date: date
    end_date: date
    calendar_type: str = Field(default="standard")
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)

    @model_validator(mode="after")
    def _check_date_span(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        span = (self.end_date - self.start_date).days
        if span > _MAX_CALENDAR_SPAN_DAYS:
            raise ValueError(
                f"Calendar date span of {span} days exceeds the maximum "
                f"of {_MAX_CALENDAR_SPAN_DAYS} days (~100 years). "
                "Reduce the date range."
            )
        return self


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

    @model_validator(mode="after")
    def _check_date_span(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        span = (self.end_date - self.start_date).days
        if span > _MAX_CALENDAR_SPAN_DAYS:
            raise ValueError(
                f"Calendar date span of {span} days exceeds the maximum "
                f"of {_MAX_CALENDAR_SPAN_DAYS} days (~100 years). "
                "Reduce the date range."
            )
        return self


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
    architecture doc's near-term item #1 calls out.

    Bug-7197: ``interior_gap`` is True when the calendar covers the outer
    boundaries but is missing dates within the range (interior gaps).
    ``calendar_date_count`` and ``expected_date_count`` expose the
    underlying numbers so the UI can show the gap magnitude."""

    covered: bool
    calendar_min: Optional[str] = None
    calendar_max: Optional[str] = None
    fact_min: Optional[str] = None
    fact_max: Optional[str] = None
    # "below" (facts predate the calendar), "above" (facts run past it),
    # "both", "interior" (Bug-7197), or None when fully covered / indeterminate.
    gap: Optional[str] = None
    warning: Optional[str] = None
    # Bug-7197: interior gap detection
    interior_gap: Optional[bool] = None
    calendar_date_count: Optional[int] = None
    expected_date_count: Optional[int] = None


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


async def _execute_ddl(connection: ProjectConnection, ddl: str, db) -> None:
    # Bug-8039: the shared pool is tenant-keyed and fails closed without a
    # canonical tenant identity, so pass the tenant-bound session (its
    # ``info['tenant_id']`` is the tenant slug) through to the execution gateway.
    from shared.source_executor import execute_source_ddl
    await execute_source_ddl(
        connection, ddl, tenant_session=db, purpose="calendar_ddl",
    )


async def _invalidate_time_grained_aggregates(db, *, model_id: UUID) -> int:
    """F-016-01: invalidate aggregates whose grain depends on a time dimension.

    A regenerated calendar shifts period boundaries, so every aggregate grouped
    at a calendar-derived time grain now holds stale rows. Each SERVABLE
    ("active") such aggregate is flipped to non-servable ("pending") with its
    prior status durably preserved in ``refresh_prior_status`` (the same
    pre-physical-change contract the refresh guard uses), so the query binder
    (which serves ``status=="active"`` only) stops serving it until a refresh
    rebuilds it against the new calendar. Runs in the caller's transaction so
    invalidation commits atomically with the calendar change.

    Returns the number of aggregates invalidated.
    """
    # Logical names of this model's time dimensions (grain entries are logical
    # dimension names). Any aggregate whose grain intersects these is time
    # grained and therefore calendar-dependent.
    time_dim_rows = await db.execute(
        select(Dimension.name).where(
            Dimension.model_id == model_id,
            Dimension.is_time_dim.is_(True),
        )
    )
    time_dim_names = {n.lower() for (n,) in time_dim_rows.all() if n}
    if not time_dim_names:
        return 0

    agg_rows = await db.execute(
        select(AggregateDefinition).where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status == "active",
        )
    )
    invalidated = 0
    for agg in agg_rows.scalars().all():
        grain = {str(g).lower() for g in (agg.grain or [])}
        if not (grain & time_dim_names):
            continue
        # Preserve the durable prior status only if not already mid-flight.
        if getattr(agg, "refresh_prior_status", None) is None:
            agg.refresh_prior_status = agg.status
        agg.status = "pending"
        invalidated += 1
    if invalidated:
        await db.flush()
    return invalidated


def _calendar_period_math_changed(cal, updates: dict) -> bool:
    """F-016-03: True when an ``update_calendar`` payload changes a calendar's
    period math — its ``calendar_type``, ``fiscal_year_start_month``, or any
    period-column map — to a value different from what is stored.

    These are the changes that shift period boundaries, so time-grained
    aggregates built on the old grain must be invalidated. Editing an unrelated
    field (e.g. a label) or re-sending the same value returns False, so an
    aggregate is not needlessly flipped to pending.
    """
    for field in ("calendar_type", "fiscal_year_start_month", *_CALENDAR_COLUMN_FIELDS):
        if field in updates and updates[field] != getattr(cal, field, None):
            return True
    return False


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
    """Raise 404 only for a confirmed missing table, otherwise 503."""
    try:
        await verify_source_table_exists(
            qualified_name,
            connection,
            model_id=model_id,
            source_id=source_id,
            bearer=bearer,
        )
    except SourceTableNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc
    except SourceProbeUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "source_table_probe_unavailable: the source table could not be "
                f"verified; retry shortly ({exc})."
            ),
        ) from exc


async def _verify_calendar_columns(
    qualified_name: str,
    connection: ProjectConnection,
    *,
    model_id: UUID,
    source_id: UUID,
    bearer: str,
    column_mappings: dict[str, str | None],
) -> None:
    """Reject calendar column mappings that the source cannot compile.

    The probe is deliberately routed through query-router introspection, not a
    direct source connection. ``LIMIT 0`` keeps the check metadata-only while
    still forcing the source engine to resolve every mapped column.
    """
    from shared.connector_qualify import (
        quote_identifier,
        quote_table_ref,
        transpile_preview_sql,
    )
    from shared.schemas.connection_type import normalize_connection_type

    mapped_columns = sorted({c for c in column_mappings.values() if c})
    if not mapped_columns:
        return

    connector = normalize_connection_type((connection.connection_type or "").lower())
    pg_table = quote_table_ref("postgresql", qualified_name)
    projection = ", ".join(
        f"{quote_identifier('postgresql', col)} AS c{i}"
        for i, col in enumerate(mapped_columns)
    )
    canonical = f"SELECT {projection} FROM {pg_table} LIMIT 0"
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
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Could not validate columns for calendar table {qualified_name!r}.",
        ) from exc

    if resp.status_code >= 400:
        detail = {
            "message": (
                f"Calendar column mapping does not match source table "
                f"{qualified_name!r}."
            ),
            "columns": mapped_columns,
        }
        try:
            payload = resp.json()
            if payload:
                detail["source_error"] = payload
        except Exception:
            if resp.text:
                detail["source_error"] = resp.text
        raise HTTPException(status_code=400, detail=detail)


def _validate_calendar_type(calendar_type: str | None) -> str | None:
    """Normalise and validate a calendar_type against the canonical 6-type set.

    F-016-11: ``bind`` / ``update`` previously persisted any string (e.g.
    ``"banana"``), which downstream ``CALENDAR_COLUMN_SETS.get(...)`` /
    expression-capability checks then silently coerced toward standard-ish
    behaviour. Returns the normalised type; raises 400 on an unknown value.

    Bug-5920: a canonical type whose runtime dependency is not installed in
    this deployment (currently ``hijri``) is rejected here with a clear,
    user-facing "not available in this deployment" message instead of
    being accepted and failing later inside the DDL emitter with a
    developer-oriented "pip install ..." error.
    """
    normalized = normalize_calendar_type(calendar_type)
    if normalized is None:
        return normalized
    if normalized not in CALENDAR_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"calendar_type {calendar_type!r} is invalid. "
                f"Must be one of {sorted(CALENDAR_TYPES)}."
            ),
        )
    if not is_available_calendar_type(normalized):
        raise HTTPException(
            status_code=400,
            detail=(
                f"calendar_type {normalized!r} is not available in this "
                "deployment (its optional runtime dependency is not "
                "installed). Contact your administrator to enable it."
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


def _calendar_name_variants(table_name: str) -> set[str]:
    """Return exact and short physical names for one calendar table."""
    parts = table_name.split(".")
    variants = {table_name}
    if len(parts) >= 3:
        variants.add(".".join(parts[-2:]))
    return variants


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
    name_variants = _calendar_name_variants(calendar.table_name)
    existing_mt = (
        await db.execute(
            select(ModelTable)
            .where(
                ModelTable.model_id == model_id,
                ModelTable.physical_name.in_(name_variants),
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
# GET /types — which calendar types are usable in this deployment
# ---------------------------------------------------------------------------


class CalendarTypeAvailability(BaseModel):
    calendar_type: str
    available: bool


@router.get(
    "/types",
    response_model=list[CalendarTypeAvailability],
    dependencies=[require_role("viewer")],
)
async def list_calendar_types(
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[CalendarTypeAvailability]:
    """Bug-5920: backend-computed calendar type availability.

    The canonical 6-type vocabulary (``CALENDAR_TYPES``) is fixed, but a
    type can require an optional runtime dependency that is not installed
    in every deployment. The frontend must render its calendar-type picker
    from this endpoint rather than a hardcoded per-type flag, so enabling
    a type (e.g. installing ``hijri-converter``) takes effect without a
    frontend code change.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        source = await db.get(DataSource, source_id)
        if source is None or source.model_id != model_id:
            raise HTTPException(status_code=404, detail="DataSource not found")
        available = available_calendar_types()
        return [
            CalendarTypeAvailability(calendar_type=t, available=t in available)
            for t in sorted(CALENDAR_TYPES)
        ]
    return []


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
    _validate_table_name(body.table_name)  # Bug-7206
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
    _validate_table_name(body.table_name)  # Bug-7206
    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-7982 completion round: this lock is held across the source DDL
        # call below (network I/O) BY DESIGN — F-016-01 requires the
        # destructive DDL to run LAST, after all metadata/hierarchy work has
        # succeeded and is staged for commit, so a late-acquire (the
        # refresh_named_list pattern) is not safe here: it would let the
        # metadata be written non-atomically with the source change it
        # describes. `acquire_model_definition_lock`'s PostgreSQL
        # `lock_timeout` (`settings.MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS`)
        # bounds how long any OTHER writer will wait on this model's lock
        # instead of hanging indefinitely if this call's DDL is slow or hung.
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        source, connection = await _load_source_with_connection(db, source_id, model_id, project_id=project_id)
        dialect = _normalise_dialect(connection.connection_type)
        calendar_type = _validate_calendar_type(body.calendar_type) or "standard"
        qualified_name = qualify_physical_name(body.table_name, connection, source)
        columns = CALENDAR_COLUMN_SETS.get(calendar_type, STANDARD_COLUMNS)

        # Bug-7208 (codex F1): check for existing registration BEFORE DDL
        # generation/execution. The DDL is autocommitted on the source, so
        # rejecting after execution leaves the source table changed while
        # Tessallite metadata remains stale.
        _existing_pre = (
            await db.execute(
                select(CalendarTable).where(
                    CalendarTable.data_source_id == source.id,
                    CalendarTable.table_name == qualified_name,
                )
            )
        ).scalar_one_or_none()
        if (
            _existing_pre is not None
            and _existing_pre.calendar_type is not None
            and _existing_pre.calendar_type != calendar_type
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot change calendar type from "
                    f"'{_existing_pre.calendar_type}' to '{calendar_type}' "
                    "on an existing registration. Delete the calendar "
                    "and re-create it with the new type instead."
                ),
            )

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

        # F-016-01: the source calendar DDL is DESTRUCTIVE (DROP+recreate) and
        # AUTOCOMMITS on the source, which lives in a DIFFERENT transaction
        # domain than Tessallite's tenant metadata. Previously the DDL ran here,
        # BEFORE the metadata + hierarchy work; when hierarchy generation then
        # failed, only the tenant DB was rolled back, leaving the physical
        # calendar changed while the metadata reverted — a silent source/metadata
        # divergence that yields wrong period boundaries. The DDL is now deferred
        # to the very end, after ALL metadata/hierarchy work has succeeded and is
        # ready to commit, so any earlier failure leaves the source untouched
        # (source == old, metadata == old). See the deferred _execute_ddl below.

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
            # Bug-7208: type-change guard runs pre-DDL (above). This branch
            # only fires when the type matches or existing has no type.
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

        await db.flush()

        # Auto-create date hierarchies for unassigned fact-table date columns,
        # in the SAME transaction as the calendar. Locate the alias we just
        # created/confirmed for this calendar.
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
            except Exception as exc:
                # Bug-6242: do NOT swallow. Roll the whole unit of work back so a
                # calendar is never committed without its date hierarchy, then
                # fail loud so the operator sees the failure instead of a
                # success response over a half-built calendar.
                # F-016-01: the source DDL has NOT run yet (deferred below), so
                # this rollback truly leaves BOTH stores at their prior state —
                # the "changes were rolled back" message is now accurate.
                await db.rollback()
                logger.error(
                    "Calendar auto-create aborted: date-hierarchy generation "
                    "failed for model %s calendar %s: %s",
                    model_id, qualified_name, exc, exc_info=True,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        "Automatic date-hierarchy generation failed; no changes "
                        f"were made to the source or the model. Reason: {exc}. "
                        "Fix the underlying issue and retry, or bind the calendar "
                        "and add date hierarchies manually."
                    ),
                ) from exc

        # F-016-01: invalidate every servable aggregate whose grain depends on a
        # calendar-derived time dimension in this model. A regenerated calendar
        # changes period boundaries, so any aggregate grouped at a time grain now
        # holds stale rows. Flip it to non-servable ("pending") with its prior
        # status preserved, so the binder stops serving it until a refresh
        # rebuilds it. Done in the SAME tenant transaction as the calendar so
        # invalidation commits atomically with the metadata.
        await _invalidate_time_grained_aggregates(db, model_id=model_id)

        # F-016-01: execute the DESTRUCTIVE source DDL LAST — after all metadata
        # and hierarchy work has succeeded and is staged for commit. On failure
        # here the tenant transaction is rolled back and the source is left
        # untouched (source == metadata == old state).
        try:
            await _execute_ddl(connection, ddl, db)
        except Exception as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": (
                        f"Calendar DDL execution failed on source: {exc}. "
                        "No changes were made to the model."
                    ),
                    "ddl": ddl,
                },
            ) from exc

        try:
            await db.commit()
        except Exception as exc:
            # Narrow window: the source DDL succeeded but the metadata commit
            # failed. Surface it explicitly rather than claiming a clean rollback
            # — the operator must reconcile by re-running Generate (idempotent).
            await db.rollback()
            logger.error(
                "Calendar auto-create: source DDL applied but metadata commit "
                "failed for model %s calendar %s: %s",
                model_id, qualified_name, exc, exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "The source calendar table was rebuilt, but saving the model "
                    "metadata failed. Re-run Generate (it is idempotent) to "
                    "reconcile the metadata with the source."
                ),
            ) from exc
        await db.refresh(cal)

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
    _validate_table_name(body.table_name)  # Bug-7206
    bearer = _extract_bearer(request)

    async for db in get_tenant_db(current_user.tenant_id):
        # Bug-7982 completion round: like auto_create_calendar, this lock is
        # held across source verification reads (_verify_table_exists /
        # _verify_calendar_columns — network I/O). `acquire_model_definition_lock`'s
        # `lock_timeout` bounds how long any OTHER writer waits on this model's
        # lock if those reads are slow.
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
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
        await _verify_calendar_columns(
            qualified_name, connection,
            model_id=model_id, source_id=source.id, bearer=bearer,
            column_mappings={
                field: getattr(body, field)
                for field in _CALENDAR_COLUMN_FIELDS
            },
        )

        # Idempotent: if a CalendarTable for this (source, table_name) already
        # exists, update its column mappings rather than creating a duplicate.
        # BigQuery discovery can store dataset.table while a manual bind sends
        # project.dataset.table; treat those as the same source table.
        name_variants = _calendar_name_variants(qualified_name)
        existing = (
            await db.execute(
                select(CalendarTable).where(
                    CalendarTable.data_source_id == source.id,
                    CalendarTable.table_name.in_(name_variants),
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
        await db.flush()

        auto_created_aliases: list[str] = []
        try:
            _, _, auto_created_aliases = await _auto_create_date_hierarchies_for_model(
                db,
                model_id=model_id,
                calendar_model_table_id=alias_mt.id,
            )
        except Exception as exc:
            # Bug-6242: fail loud + roll back instead of swallowing, so a bound
            # calendar is never persisted without its date hierarchy.
            await db.rollback()
            logger.error(
                "Calendar bind aborted: date-hierarchy generation failed for "
                "model %s calendar %s: %s",
                model_id, qualified_name, exc, exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Automatic date-hierarchy generation failed; the calendar "
                    f"changes were rolled back. Reason: {exc}. Fix the underlying "
                    "issue and retry, or add the date hierarchies manually."
                ),
            ) from exc

        await db.commit()
        await db.refresh(cal)

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


def _build_gap_count_sql(
    connector: str,
    *,
    table_name: str,
    date_col: str,
    range_lo: str,
    range_hi: str,
) -> str:
    """Bug-7197: count distinct dates in the calendar between two date
    boundaries.  By comparing this count to the expected number of days in
    the range we can detect interior gaps (missing dates within the range
    that MIN/MAX alone cannot see).

    All identifiers are connector-quoted; date literals are passed as CAST
    expressions to avoid dialect-specific date syntax (sqlglot transpiles
    the canonical PG ``CAST('...' AS DATE)`` to the target dialect)."""
    from shared.connector_qualify import (
        quote_identifier,
        quote_table_ref,
        transpile_preview_sql,
    )

    pg_table = quote_table_ref("postgresql", table_name)
    pg_col = quote_identifier("postgresql", date_col)
    canonical = (
        f"SELECT COUNT(DISTINCT {pg_col}) AS cnt "
        f"FROM {pg_table} "
        f"WHERE {pg_col} >= CAST('{range_lo}' AS DATE) "
        f"AND {pg_col} <= CAST('{range_hi}' AS DATE)"
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

        # Bug-5681: parse date boundary strings into date objects before
        # comparison. String comparison can give wrong results for dates in
        # different formats (e.g. "2020-1-5" vs "2020-01-05", or timestamps
        # with trailing timezone info). The source DB may return dates as
        # ISO strings, timestamps, or other formats. Parse robustly.
        from datetime import date as _date_type, datetime as _dt_type

        def _parse_date_boundary(val: str) -> _date_type:
            """Parse a date string into a date object for comparison."""
            s = val.strip()
            # Try ISO date first (YYYY-MM-DD)
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return _dt_type.strptime(s[:len(fmt.replace('%', '0'))], fmt).date()
                except (ValueError, IndexError):
                    continue
            # Fallback: parse just the date portion (first 10 chars)
            try:
                return _dt_type.strptime(s[:10], "%Y-%m-%d").date()
            except ValueError:
                # Last resort: return as-is for string comparison (legacy path)
                raise

        try:
            fact_lo_d = _parse_date_boundary(fact_lo)
            fact_hi_d = _parse_date_boundary(fact_hi)
            cal_lo_d = _parse_date_boundary(cal_lo)
            cal_hi_d = _parse_date_boundary(cal_hi)
            below = fact_lo_d < cal_lo_d
            above = fact_hi_d > cal_hi_d
        except (ValueError, TypeError):
            # Parsing failed — fall back to string comparison (original behaviour)
            below = fact_lo < cal_lo
            above = fact_hi > cal_hi
        # Bug-7197: detect interior gaps (missing dates within the range)
        # by counting distinct calendar dates within the overlapping range.
        # The overlap range is the intersection of the calendar and fact
        # ranges — the window where we expect the calendar to have
        # continuous daily coverage.
        interior_gap = None
        cal_date_count = None
        expected_date_count = None
        try:
            overlap_lo = max(cal_lo_d, fact_lo_d)
            overlap_hi = min(cal_hi_d, fact_hi_d)
            if overlap_lo <= overlap_hi:
                expected_days = (overlap_hi - overlap_lo).days + 1
                gap_sql = _build_gap_count_sql(
                    connector,
                    table_name=cal_qualified,
                    date_col=cal.date_column,
                    range_lo=overlap_lo.isoformat(),
                    range_hi=overlap_hi.isoformat(),
                )
                gap_results = await _introspect_batch_via_router(
                    str(model_id), [("gap", gap_sql)], bearer,
                )
                gap_rows, _gap_cols, gap_err = gap_results.get(
                    "gap", ([], [], "no result"),
                )
                if not gap_err and gap_rows:
                    raw_cnt = gap_rows[0].get("cnt")
                    if raw_cnt is not None:
                        actual_count = int(raw_cnt)
                        cal_date_count = actual_count
                        expected_date_count = expected_days
                        interior_gap = actual_count < expected_days
        except (ValueError, TypeError):
            # Bug-7197: log so operators can see when interior gap detection
            # fails (e.g. non-integer count, unparseable date boundary).
            # Falls back to boundary-only detection — not a coverage failure.
            logger.warning(
                "Calendar coverage interior gap detection skipped for "
                "calendar %s: date parsing or count conversion failed",
                calendar_id,
                exc_info=True,
            )

        boundary_gap = below or above
        if not boundary_gap and not interior_gap:
            return CalendarCoverageResponse(
                covered=True,
                calendar_min=cal_lo, calendar_max=cal_hi,
                fact_min=fact_lo, fact_max=fact_hi,
                interior_gap=interior_gap,
                calendar_date_count=cal_date_count,
                expected_date_count=expected_date_count,
            )

        parts = []
        if below:
            parts.append(f"fact rows from {fact_lo} predate the calendar start {cal_lo}")
        if above:
            parts.append(f"fact rows to {fact_hi} run past the calendar end {cal_hi}")
        if interior_gap:
            parts.append(
                f"the calendar has {cal_date_count} dates in the fact range "
                f"but {expected_date_count} are expected (interior gaps detected)"
            )

        if boundary_gap and interior_gap:
            gap_label = "both" if (below and above) else ("below" if below else "above")
            gap_label = f"{gap_label}+interior"
        elif interior_gap:
            gap_label = "interior"
        else:
            gap_label = "both" if (below and above) else ("below" if below else "above")

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
            gap=gap_label, warning=warning,
            interior_gap=interior_gap,
            calendar_date_count=cal_date_count,
            expected_date_count=expected_date_count,
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
    request: Request,
    project_id: UUID,
    model_id: UUID,
    source_id: UUID,
    calendar_id: UUID,
    body: CalendarUpdateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> CalendarTableResponse:
    bearer = _extract_bearer(request)
    async for db in get_tenant_db(current_user.tenant_id):
        # F-016-12: verify the source belongs to this model/project before
        # mutating, matching the chain every sibling endpoint applies.
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        cal = await db.get(CalendarTable, calendar_id)
        if cal is None or cal.data_source_id != source_id:
            raise HTTPException(status_code=404, detail="CalendarTable not found")
        _source, connection = await _load_source_with_connection(
            db, source_id, model_id, project_id=project_id
        )

        updates = body.model_dump(exclude_unset=True)

        # F-016-11: validate calendar_type and refuse a type change on an
        # auto-created calendar — the physical table's columns encode the old
        # type's semantics, so the type must be changed by regenerating the
        # table (auto-create), not by re-labelling the registration.
        if "calendar_type" in updates:
            # Bug-7210: reject explicit null — calendar_type is NOT NULL
            # in the DB; an explicit null would IntegrityError (500).
            if updates["calendar_type"] is None:
                raise HTTPException(
                    status_code=422,
                    detail="calendar_type cannot be cleared (must be a valid calendar type).",
                )
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

        # Bug-7215: refuse fiscal_year_start_month changes on auto-created
        # calendars. Fiscal DDL physically materialises year, half, and
        # quarter using the start month. Changing the metadata without
        # regenerating the physical table creates a silent contradiction.
        if "fiscal_year_start_month" in updates:
            new_fys = updates["fiscal_year_start_month"]
            # Codex F5: reject explicit null -- the DB column is NOT NULL
            # and an explicit null would produce an IntegrityError (500).
            if new_fys is None:
                raise HTTPException(
                    status_code=422,
                    detail="fiscal_year_start_month cannot be cleared (must be 1-12).",
                )
            if (
                cal.autocreated
                and new_fys != getattr(cal, "fiscal_year_start_month", 1)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Changing fiscal_year_start_month on an auto-created "
                        "calendar would leave the physical table's period columns "
                        "inconsistent with the new fiscal start. Regenerate the "
                        "calendar (Generate) with the new fiscal start month "
                        "instead of editing it here."
                    ),
                )

        if any(field in updates for field in _CALENDAR_COLUMN_FIELDS):
            final_mappings = {
                field: updates.get(field, getattr(cal, field))
                for field in _CALENDAR_COLUMN_FIELDS
            }
            await _verify_calendar_columns(
                cal.table_name, connection,
                model_id=model_id, source_id=source_id, bearer=bearer,
                column_mappings=final_mappings,
            )

        # F-016-03: a BOUND calendar's period math (calendar_type, fiscal start,
        # or period-column map) can change here — auto-created type/fiscal changes
        # are already 409'd above. When it does, every time-grained aggregate
        # built on the OLD grain now holds stale rows, so invalidate them with the
        # same helper + pending/refresh_prior_status contract auto-create uses,
        # atomically in this transaction. Without this an active aggregate keeps
        # being served against the new calendar and diverges from source.
        _calendar_math_changed = _calendar_period_math_changed(cal, updates)

        for k, v in updates.items():
            setattr(cal, k, v)
        if _calendar_math_changed:
            await _invalidate_time_grained_aggregates(db, model_id=model_id)
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
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        await _load_source_with_connection(db, source_id, model_id, project_id=project_id)
        cal = await db.get(CalendarTable, calendar_id)
        if cal is None or cal.data_source_id != source_id:
            return

        # Bug-7209: check for dependent references before deletion.
        dependents: list[str] = []
        measure_result = await db.execute(
            select(Measure.name).where(
                Measure.resolved_calendar_id == calendar_id
            )
        )
        dep_measures = [name for (name,) in measure_result.all()]
        if dep_measures:
            dependents.append(
                f"measures: {', '.join(dep_measures[:10])}"
                + (f" (+{len(dep_measures) - 10} more)"
                   if len(dep_measures) > 10 else "")
            )
        alias_result = await db.execute(
            select(ModelTable.alias).where(
                ModelTable.calendar_table_id == calendar_id
            )
        )
        dep_aliases = [a for (a,) in alias_result.all() if a]
        if dep_aliases:
            dependents.append(
                f"calendar aliases: {', '.join(dep_aliases[:10])}"
                + (f" (+{len(dep_aliases) - 10} more)"
                   if len(dep_aliases) > 10 else "")
            )
        if dependents:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot delete calendar: it is referenced by "
                    + "; ".join(dependents)
                    + ". Remove the references first."
                ),
            )

        await db.delete(cal)
        await db.commit()
