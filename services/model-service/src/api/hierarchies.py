"""
Hierarchy CRUD, level CRUD, and reorder routes.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
from uuid import UUID

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

import sqlglot
from sqlglot import exp

from shared.config.settings import get_settings
from shared.connector_qualify import quote_identifier, quote_table_ref
from shared.schemas.connection_type import normalize_connection_type
from shared.schemas.measure_formats import (
    are_valid_time_calcs,
    is_valid_dimension_kind,
    is_valid_time_unit,
)
from shared.semantic.calendar_dialects import (
    CALENDAR_COLUMN_SETS,
    EXPRESSION_CAPABLE_CALENDAR_TYPES,
    TABLE_BOUND_CALENDAR_TYPES,
)
from shared.semantic.calendar_types import (
    CALENDAR_TYPES,
    normalize_calendar_type,
)
from shared.semantic.graph_order import is_fact_table
from shared.db.models import (
    CalendarTable,
    DataSource,
    Dimension,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    Join,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    ProjectConnection,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
)
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    HierarchyAttributeRef,
    HierarchyBatchDateRequest,
    HierarchyBatchDateResponse,
    HierarchyBatchDateSkipped,
    HierarchyCreate,
    HierarchyDetailResponse,
    HierarchyGenerateDateRequest,
    HierarchyGenerateSegmentRequest,
    HierarchyGeneratedResponse,
    HierarchyLevelAttributeResponse,
    HierarchyLevelCreate,
    HierarchyLevelResponse,
    HierarchyLevelUpdate,
    HierarchyPreviewLevelSummary,
    HierarchyPreviewMember,
    HierarchyPreviewResponse,
    HierarchyPreviewWarning,
    HierarchyReorderRequest,
    HierarchySummaryResponse,
    HierarchyUpdate,
    UnassignedDateColumn,
)
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, enforce_model_scope, get_current_user
from src.auth.rbac import require_role
from src.api._persona_scope import get_excluded_level_attribute_ids, parse_allowed_ids, resolve_effective_persona
from shared.security import Principal, RowSecurityCompileError, compile_row_security

from shared.connector_qualify import CONNECTOR_TO_SQLGLOT as _CONNECTOR_TO_SQLGLOT, transpile_preview_sql


# ---------------------------------------------------------------------------
# Register missing sqlglot dialect generators (sqlglot 30.x gaps)
# ---------------------------------------------------------------------------
from shared.sqlglot_compat import register_bigquery_patches
from shared.semantic.join_keyword import split_join_token

# A calendar dimension-alias join runs owning-table -> calendar alias: many
# rows to one calendar day. Derived ONCE through the shared classifier so the
# orientation and the cardinality can never disagree, and so this write path
# cannot re-introduce a cardinality token into ``Join.join_type``
# (join-orientation contract, invariant 3).
_CALENDAR_ALIAS_JOIN_TYPE, _CALENDAR_ALIAS_CARDINALITY = split_join_token("many_to_one")
register_bigquery_patches()


def _transpile_uda_expression(
    expression: str,
    connector: str,
    *,
    table_alias: str | None = None,
) -> str:
    """Parse a canonical PostgreSQL UDA expression and transpile to *connector*'s dialect.

    When *table_alias* is provided, all bare column references in the
    expression are qualified with the alias so the SQL is unambiguous
    regardless of how the FROM clause is structured.
    """
    target = _CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    tree = sqlglot.parse_one(expression, read="postgres")
    if table_alias:
        def _qualify(node):
            if isinstance(node, exp.Column) and not node.table:
                return exp.column(node.name, table=table_alias, quoted=True)
            return node
        tree = tree.transform(_qualify)
    return tree.sql(dialect=target)


router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}",
    tags=["hierarchies"],
)

ALLOWED_HIERARCHY_TYPES = {"explicit", "date_embedded", "segment"}
ALLOWED_ATTRIBUTE_SOURCES = {"physical_column", "user_defined_attribute"}
ALLOWED_ATTRIBUTE_ROLES = {"display", "filter"}
_DATE_COMPONENT_TO_TIME_UNIT: dict[str, str] = {
    "year": "year",
    "half_year": "half",
    "quarter": "quarter",
    "month": "month",
    "week": "week",
    "day": "day",
}

_DEFAULT_TIME_CALCS: list[str] = [
    "lag", "parallel_period", "period_to_date", "moving_window",
]
DATE_HIERARCHY_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "y_m_d": [("year", "Year"), ("month", "Month"), ("day", "Day")],
    "y_q_m_d": [("year", "Year"), ("quarter", "Quarter"), ("month", "Month"), ("day", "Day")],
    "y_h_q_m_d": [
        ("year", "Year"),
        ("half_year", "Half-Year"),
        ("quarter", "Quarter"),
        ("month", "Month"),
        ("day", "Day"),
    ],
    "y_w_d": [("year", "Year"), ("week", "Week"), ("day", "Day")],
    "y_m_w_d": [("year", "Year"), ("month", "Month"), ("week", "Week"), ("day", "Day")],
}

# Bug-7203/7204: calendar-type-specific hierarchy level templates.
# Each entry maps a calendar type to its default hierarchy levels as a list of
# (component, level_display_name) tuples.  For expression-capable types the
# component drives the _date_component_expression UDA generator.  For
# table-bound types the component names match the physical column names on the
# calendar table (from CALENDAR_COLUMN_SETS in calendar_dialects.py), and levels
# are built from those columns directly.
#
# These templates implement the spec in architecture_multi-calendar.md
# "Hierarchy Pre-Population by Calendar Type".
CALENDAR_HIERARCHY_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "standard": [
        ("year", "Year"), ("half_year", "Half-Year"), ("quarter", "Quarter"),
        ("month", "Month"), ("week", "Week"), ("day", "Day"),
    ],
    "fiscal": [
        ("year", "Fiscal Year"), ("half_year", "Fiscal Half"),
        ("quarter", "Fiscal Quarter"), ("month", "Fiscal Period"), ("day", "Day"),
    ],
    "iso_week": [
        ("year", "ISO Year"), ("week", "ISO Week"), ("day", "Day"),
    ],
    "thai_buddhist": [
        ("year", "Thai Year"), ("quarter", "Quarter"),
        ("month", "Month"), ("day", "Day"),
    ],
    "retail_445": [
        ("retail_year", "Retail Year"), ("retail_quarter", "Retail Quarter"),
        ("retail_period", "Retail Period"), ("retail_week", "Retail Week"),
    ],
    "hijri": [
        ("hijri_year", "Hijri Year"), ("hijri_month", "Hijri Month"),
        ("hijri_day", "Hijri Day"),
    ],
}

# Bug-7204: for table-bound calendar types, map each hierarchy component to
# the physical column name on the calendar table and a time_unit value.
# These are used to build hierarchy levels from actual calendar table columns
# rather than from EXTRACT-based UDA expressions.
_TABLE_BOUND_COMPONENT_TO_COLUMN: dict[str, dict[str, str]] = {
    # Fiscal calendars are table-bound for the generated hierarchy path when
    # a materialised calendar is available.  Their period keys use the same
    # physical columns as the standard calendar; the separate ``year_label``
    # column is attached to the year Dimension as its caption below.
    "year": {"column": "year_no", "time_unit": "year"},
    "half_year": {"column": "half_no", "time_unit": "half"},
    "quarter": {"column": "quarter_no", "time_unit": "quarter"},
    "month": {"column": "month_no", "time_unit": "month"},
    "day": {"column": "day_no", "time_unit": "day"},
    "retail_year": {"column": "retail_year", "time_unit": "year"},
    "retail_quarter": {"column": "retail_quarter", "time_unit": "quarter"},
    "retail_period": {"column": "retail_period", "time_unit": "month"},
    "retail_week": {"column": "retail_week", "time_unit": "week"},
    "hijri_year": {"column": "hijri_year", "time_unit": "year"},
    "hijri_month": {"column": "hijri_month", "time_unit": "month"},
    "hijri_day": {"column": "hijri_day", "time_unit": "day"},
}
settings = get_settings()
_logger = logging.getLogger(__name__)


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1]
    cookie = request.cookies.get("access_token")
    if cookie:
        return cookie
    raise HTTPException(status_code=401, detail="Bearer token required")


async def _introspect_batch_via_router(
    model_id: str,
    queries: list[tuple[str, str]],
    bearer: str,
    timeout_s: float = 60.0,
) -> dict[str, tuple[list[dict], list[str], str | None]]:
    """Execute a batch of raw SQL queries via the query-router.

    *queries* is a list of ``(key, sql)`` tuples. Returns a dict
    keyed by *key*, with ``(rows, columns, error_or_none)`` values.
    """
    if not queries:
        return {}
    url = f"{settings.QUERY_ROUTER_URL}/api/v1/introspect/batch"
    headers = {"Authorization": f"Bearer {bearer}"}
    body = {
        "model_id": model_id,
        "queries": [{"key": k, "raw_sql": s} for k, s in queries],
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else resp.text
            except Exception:
                detail = resp.text or f"Introspect batch returned HTTP {resp.status_code}"
            raise HTTPException(status_code=resp.status_code, detail=detail)
        data = resp.json()
        out: dict[str, tuple[list[dict], list[str], str | None]] = {}
        for item in data.get("results", []):
            out[item["key"]] = (item["rows"], item["columns"], item.get("error"))
        return out


def _validation_error(message: str, *, field: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"error": "validation_error", "message": message, "field": field, "code": code},
    )


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


# F-016-04: hierarchies now accept the full 6-type calendar vocabulary
# (was the 4-token {standard, fiscal, hijri, iso}). ``iso`` is normalised to
# ``iso_week`` before validation so the hierarchy side and the calendar-table
# side share one enum.
_VALID_CALENDAR_TYPES = CALENDAR_TYPES


def _validate_calendar_fields(
    calendar_type: str | None, fiscal_year_start_month: int | None
) -> None:
    # F-016-18: calendar-field validations carry calendar-specific codes (C1 /
    # C2). They previously reused H3 / H4, which the spec's code contract
    # (architecture_hierarchy-requirements.md §12.1) assigns to "level must
    # have exactly one key attribute" (H3) and "key reuse" (H4) — so a client
    # keying behaviour off ``code`` got mismatched meanings.
    calendar_type = normalize_calendar_type(calendar_type)
    if calendar_type is not None and calendar_type not in _VALID_CALENDAR_TYPES:
        raise _validation_error(
            f"calendar_type must be one of {sorted(_VALID_CALENDAR_TYPES)}.",
            field="calendar_type",
            code="C1",
        )
    if calendar_type == "fiscal":
        if fiscal_year_start_month is None:
            raise _validation_error(
                "fiscal_year_start_month is required when calendar_type is 'fiscal'.",
                field="fiscal_year_start_month",
                code="C2",
            )
        if not 1 <= fiscal_year_start_month <= 12:
            raise _validation_error(
                "fiscal_year_start_month must be between 1 and 12.",
                field="fiscal_year_start_month",
                code="C2",
            )
    elif fiscal_year_start_month is not None:
        raise _validation_error(
            "fiscal_year_start_month is only valid when calendar_type is 'fiscal'.",
            field="fiscal_year_start_month",
            code="C2",
        )


async def _ensure_model_in_project(
    db,
    *,
    project_id: UUID,
    model_id: UUID,
) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise _not_found("Model not found")
    return model


def _normalize_source(source: str, *, field: str) -> str:
    out = (source or "").strip()
    if out not in ALLOWED_ATTRIBUTE_SOURCES:
        raise _validation_error(
            f"Unsupported attribute source '{source}'",
            field=field,
            code="H3",
        )
    return out


def _normalize_hierarchy_type(raw_type: str) -> str:
    out = (raw_type or "").strip()
    if out not in ALLOWED_HIERARCHY_TYPES:
        raise _validation_error(
            f"Unsupported hierarchy type '{raw_type}'",
            field="type",
            code="D2",
        )
    return out


def _type_family(data_type: str) -> str:
    t = (data_type or "").lower()
    if any(x in t for x in ("char", "text", "string", "varchar")):
        return "string"
    if any(x in t for x in ("int", "numeric", "decimal", "float", "double", "real", "number")):
        return "numeric"
    if any(x in t for x in ("date", "time", "timestamp")):
        return "datetime"
    return t


def _types_compatible(a: str, b: str) -> bool:
    fa, fb = _type_family(a), _type_family(b)
    if fa == fb:
        return True
    return {fa, fb} <= {"numeric"}


@dataclass
class _ResolvedAttribute:
    ref: HierarchyAttributeRef
    table: ModelTable


@dataclass
class _ResolvedSqlAttribute:
    resolved: _ResolvedAttribute
    expression: str
    referenced_column_ids: list[UUID]


def _slugify_name(raw: str) -> str:
    lowered = (raw or "").strip().lower()
    out = "".join(ch if ch.isalnum() else "_" for ch in lowered)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "attribute"


# Persisted name columns for auto-generated calendar aliases, hierarchies,
# UDAs and dimensions are all varchar(255). Auto-generation derives these
# from user-facing labels, so a pathological label (or a regression that lets
# a name grow) must never raise asyncpg StringDataRightTruncationError on the
# INSERT. This is a backstop only — the primary defence is deriving names from
# stable base columns and never re-consuming generated calendar-internal
# attributes (see _get_unassigned_date_cols).
_NAME_COLUMN_LIMIT = 255


def _clamp_to_limit(value: str | None, limit: int = _NAME_COLUMN_LIMIT) -> str | None:
    """Clamp *value* to *limit* characters.

    When truncation is required, a deterministic hash of the full value is
    appended so distinct over-long inputs keep distinct clamped outputs
    (uniqueness preserved for the model-scoped unique constraints on alias,
    hierarchy name, UDA name and dimension name).

    Bug-6706: the original 8-hex SHA-1 suffix gave only 32 bits of collision
    resistance — brute-forceable and likely to collide on large models with
    many auto-generated names. Upgraded to 16 hex chars of SHA-256 (64-bit
    collision resistance), which is practical for model-scoped uniqueness
    constraints and infeasible to brute-force.
    """
    if value is None or len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    keep = limit - len(digest) - 1
    if keep < 1:
        return digest[:limit]
    return f"{value[:keep]}_{digest}"


def _calendar_hier_label(display_name: str | None, column_name: str) -> str:
    """The clamped '<label> Calendar' name used for an auto-generated date
    hierarchy and its calendar-alias display_name.

    Single source of truth shared by both generators
    (_auto_create_date_hierarchies_for_model and the batch-date endpoint) so
    they persist byte-identical, varchar(255)-safe names and therefore
    recognise each other's existing hierarchies instead of creating duplicates.
    """
    return _clamp_to_limit(f"{display_name or column_name} Calendar")


async def _next_calendar_alias(db, model_id: UUID, col_name: str, used: set[str]) -> str:
    """Return '{col_slug}_calendar', auto-sequenced if already taken in model or used set."""
    existing = set(
        (
            await db.execute(select(ModelTable.alias).where(ModelTable.model_id == model_id))
        ).scalars().all()
    ) | used
    # Clamp the base leaving headroom for the "_<n>" uniqueness suffix so the
    # persisted alias (varchar(255)) can never overflow even for a very long
    # column name.
    base = _clamp_to_limit(f"{_slugify_name(col_name)}_calendar", _NAME_COLUMN_LIMIT - 8)
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def _date_component_expression(source_expr: str, component: str) -> str:
    """Gregorian (standard-calendar) level-key expression for a date component.

    This is the calendar-AGNOSTIC baseline. For fiscal / ISO / Thai calendars
    the period boundaries differ; ``_calendar_component_expression`` overlays
    the calendar-specific math and delegates here for the standard cases.
    """
    base = f"({source_expr})"
    if component == "year":
        return f"EXTRACT(YEAR FROM {base})"
    if component == "quarter":
        return f"EXTRACT(QUARTER FROM {base})"
    if component == "month":
        return f"EXTRACT(MONTH FROM {base})"
    if component == "week":
        return f"EXTRACT(WEEK FROM {base})"
    if component == "half_year":
        return f"CASE WHEN EXTRACT(MONTH FROM {base}) <= 6 THEN 1 ELSE 2 END"
    if component == "day":
        return f"CAST({base} AS DATE)"
    raise _validation_error(
        f"Unsupported date hierarchy component '{component}'",
        field="template",
        code="D2",
    )


def _calendar_component_expression(
    source_expr: str,
    component: str,
    calendar_type: str | None = None,
    fiscal_year_start_month: int | None = None,
) -> str:
    """Calendar-aware level-key expression for a generated date hierarchy.

    F-016-01 (CRITICAL, wrong numbers): a generated ``Fiscal Year`` / ``ISO
    Year`` / ``Thai Year`` level must bucket the SAME way as the calendar-table
    column and the time-variant SQL, not a bare Gregorian ``EXTRACT(YEAR ...)``.
    Previously every expression-capable calendar type shared the Gregorian
    helper, so under an April fiscal calendar 2025-03-31 grouped as year 2025
    while the calendar table's ``year_no`` (and every YTD/QTD variant) said
    2024; ISO week-1 boundary days filed under the wrong year; Thai Year showed
    2025 instead of 2568.

    The expressions here are IDENTICAL in semantics to the canonical forms in
    ``shared.semantic.calendar_dialects._emit_standard`` (calendar table) and
    ``shared.semantic.time_variants_sql._extract_period`` (variant SQL) so all
    three agree end to end:

    * fiscal year (fys != 1):  CASE WHEN month >= fys THEN year ELSE year-1 END
    * fiscal quarter (fys!=1):  FLOOR(MOD(month - fys + 12, 12) / 3) + 1
    * fiscal half (fys != 1):   H1 for fiscal quarters 1-2, else H2
    * fiscal period (F-016-18): MOD(month - fys + 12, 12) + 1  (1 = first
                                fiscal month; for fys == 1 this is the calendar
                                month, so standard behaviour is preserved)
    * iso_week year:            EXTRACT(ISOYEAR FROM d)
    * thai year:                EXTRACT(YEAR FROM d) + 543

    The result is stored canonical PostgreSQL and transpiled to the source
    dialect at execution time (SQL rule 1). Only expression-capable calendar
    types reach this helper; table-bound types (retail_445, hijri) key their
    hierarchy levels on physical calendar columns (see the ``column_id_map``
    branch of ``_create_date_hierarchy_for_alias``).
    """
    cal = normalize_calendar_type(calendar_type) or "standard"
    # F-016-01 (safety belt, mirrors time_variants_sql._extract_period): a
    # table-bound calendar (retail_445, hijri) cannot have its hierarchy level
    # keys computed from a Gregorian expression on the fact date — its retail /
    # Hijri period boundaries live in the materialised calendar table's own
    # columns. Reaching this helper with a table-bound type means the caller
    # tried to build an expression-based date hierarchy for a type that requires
    # a bound calendar table (e.g. the explicit generate-date endpoint with
    # calendar_type=retail_445). Fail loud instead of silently emitting Gregorian
    # (WRONG) keys; the correct path is calendar bind + auto-create, which keys
    # levels on the physical calendar columns.
    if cal in TABLE_BOUND_CALENDAR_TYPES:
        raise _validation_error(
            f"Calendar type '{cal}' is table-bound: its date-hierarchy level "
            f"keys must come from the bound calendar table's period columns, not "
            f"a Gregorian expression on the fact date. Bind the calendar and use "
            f"calendar auto-create instead of an expression-based date hierarchy.",
            field="calendar_type",
            code="C1",
        )
    # Standard/Gregorian and the raw day level are calendar-agnostic — keep the
    # baseline expression byte-identical so standard hierarchies never change.
    if cal == "standard" or component == "day":
        return _date_component_expression(source_expr, component)

    base = f"({source_expr})"
    fys = fiscal_year_start_month or 1

    if component == "year":
        if cal == "iso_week":
            return f"EXTRACT(ISOYEAR FROM {base})"
        if cal == "thai_buddhist":
            return f"EXTRACT(YEAR FROM {base}) + 543"
        if cal == "fiscal" and fys != 1:
            return (
                f"CASE WHEN EXTRACT(MONTH FROM {base}) >= {fys} "
                f"THEN EXTRACT(YEAR FROM {base}) "
                f"ELSE EXTRACT(YEAR FROM {base}) - 1 END"
            )
        return _date_component_expression(source_expr, component)

    if component == "quarter":
        if cal == "fiscal" and fys != 1:
            return (
                f"FLOOR(MOD(EXTRACT(MONTH FROM {base}) - {fys} + 12, 12) / 3) + 1"
            )
        return _date_component_expression(source_expr, component)

    if component == "half_year":
        if cal == "fiscal" and fys != 1:
            fiscal_qtr = (
                f"FLOOR(MOD(EXTRACT(MONTH FROM {base}) - {fys} + 12, 12) / 3) + 1"
            )
            return f"CASE WHEN {fiscal_qtr} <= 2 THEN 1 ELSE 2 END"
        return _date_component_expression(source_expr, component)

    if component == "month":
        # F-016-18: under a fiscal calendar the hierarchy month level is the
        # FISCAL PERIOD (1 = the first month of the fiscal year), so a drill on
        # a fiscal hierarchy counts months the way the business does. For a
        # non-fiscal calendar the month is the plain calendar month.
        if cal == "fiscal":
            return f"MOD(EXTRACT(MONTH FROM {base}) - {fys} + 12, 12) + 1"
        return _date_component_expression(source_expr, component)

    # week (ISO on Postgres for both standard and iso_week) and any other
    # component fall back to the Gregorian baseline.
    return _date_component_expression(source_expr, component)


async def _find_reusable_uda_names(
    db,
    *,
    model_id: UUID,
    table_id: UUID,
    name_expr_pairs: list[tuple[str, str]],
) -> set[str]:
    """Return lowercase names from *name_expr_pairs* whose UDA already exists
    with an identical expression — safe to reuse rather than recreate."""
    if not name_expr_pairs:
        return set()
    names_lower = [n.lower() for n, _ in name_expr_pairs]
    rows = (
        await db.execute(
            select(UserDefinedAttribute.name, UserDefinedAttribute.expression).where(
                UserDefinedAttribute.model_id == model_id,
                UserDefinedAttribute.table_id == table_id,
            )
        )
    ).all()
    existing_by_name = {r[0].lower(): r[1] for r in rows if r[0]}
    reusable: set[str] = set()
    for gen_name, gen_expr in name_expr_pairs:
        key = gen_name.lower()
        if key in existing_by_name and existing_by_name[key] == gen_expr:
            reusable.add(key)
    return reusable


async def _existing_table_attribute_names(
    db,
    *,
    model_id: UUID,
    table_id: UUID,
) -> set[str]:
    cols_result = await db.execute(
        select(ModelColumn.column_name).where(ModelColumn.model_table_id == table_id)
    )
    uda_result = await db.execute(
        select(UserDefinedAttribute.name).where(
            UserDefinedAttribute.model_id == model_id,
            UserDefinedAttribute.table_id == table_id,
        )
    )
    names = {str(r[0]).lower() for r in cols_result.fetchall() if r[0]}
    names.update(str(r[0]).lower() for r in uda_result.fetchall() if r[0])
    return names


async def _assert_names_available(
    db,
    *,
    model_id: UUID,
    table_id: UUID,
    names: list[str],
    reusable_uda_names: set[str] | None = None,
) -> None:
    if not names:
        return
    normalized: list[str] = []
    seen: set[str] = set()
    for n in names:
        item = (n or "").strip()
        if not item:
            raise _validation_error(
                "Generated attribute name cannot be empty.",
                field="name",
                code="H1",
            )
        key = item.lower()
        if key in seen:
            raise _validation_error(
                f"Generated attribute name '{item}' is duplicated.",
                field="name",
                code="H1",
            )
        seen.add(key)
        normalized.append(item)

    allow = reusable_uda_names or set()
    existing = await _existing_table_attribute_names(db, model_id=model_id, table_id=table_id)
    for item in normalized:
        if item.lower() in existing and item.lower() not in allow:
            raise _validation_error(
                f"Generated attribute name '{item}' conflicts with an existing table attribute.",
                field="name",
                code="H1",
            )


async def _resolve_sql_attribute(
    db,
    *,
    model_id: UUID,
    attribute_id: UUID,
    source: str,
    require_dimension_table: bool,
    connector: str = "postgresql",
    table_alias: str | None = None,
) -> _ResolvedSqlAttribute:
    resolved = await _resolve_attribute(
        db,
        model_id=model_id,
        attribute_id=attribute_id,
        source=source,
        require_dimension_table=require_dimension_table,
    )

    if source == "physical_column":
        col = await db.get(ModelColumn, attribute_id)
        if col is None:
            raise _not_found("Attribute not found")
        col_ref = quote_identifier(connector, col.column_name)
        if table_alias:
            col_ref = f"{quote_identifier(connector, table_alias)}.{col_ref}"
        return _ResolvedSqlAttribute(
            resolved=resolved,
            expression=col_ref,
            referenced_column_ids=[col.id],
        )

    uda = await db.get(UserDefinedAttribute, attribute_id)
    if uda is None:
        raise _not_found("Attribute not found")
    refs_result = await db.execute(
        select(UserDefinedAttributeColumnRef.column_id).where(
            UserDefinedAttributeColumnRef.attribute_id == uda.id
        )
    )
    ref_col_ids = [row[0] for row in refs_result.fetchall() if row[0] is not None]
    transpiled = _transpile_uda_expression(
        uda.expression, connector, table_alias=table_alias,
    )
    return _ResolvedSqlAttribute(
        resolved=resolved,
        expression=f"({transpiled})",
        referenced_column_ids=ref_col_ids,
    )


async def _create_generated_uda(
    db,
    *,
    model_id: UUID,
    table_id: UUID,
    name: str,
    expression: str,
    output_data_type: str,
    referenced_column_ids: list[UUID],
    description: str | None = None,
    reuse_existing: bool = False,
    history_capture: dict | None = None,
) -> UserDefinedAttribute:
    if reuse_existing:
        existing = (
            await db.execute(
                select(UserDefinedAttribute).where(
                    UserDefinedAttribute.model_id == model_id,
                    UserDefinedAttribute.table_id == table_id,
                    UserDefinedAttribute.name == name,
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.expression == expression:
            # Reused generator output: stamp the flag in case it predates the
            # is_generated column (migration 0132 backfill covers level-keyed
            # UDAs, but a freshly reused one before commit may not be covered).
            if not existing.is_generated:
                existing.is_generated = True
            return existing

    uda = UserDefinedAttribute(
        model_id=model_id,
        table_id=table_id,
        name=name,
        expression=expression,
        output_data_type=output_data_type,
        description=description,
        validated=True,
        validation_error=None,
        is_generated=True,
    )
    db.add(uda)
    await db.flush()
    if history_capture is not None:
        history_capture.setdefault("generated_uda_ids", []).append(uda.id)
    for col_id in set(referenced_column_ids):
        db.add(
            UserDefinedAttributeColumnRef(
                attribute_id=uda.id,
                column_id=col_id,
            )
        )
    return uda


async def _resolve_model_source_connection(db, model_id: UUID) -> tuple[ProjectConnection | None, str | None]:
    source = (
        await db.execute(
            select(DataSource).where(DataSource.model_id == model_id).limit(1)
        )
    ).scalar_one_or_none()
    if source is None:
        return None, "No data source configured for this model."
    # Bug-5325: fail closed if a legacy/imported source points its connection at
    # a different project. Derive the source's owning project from its model and
    # reject a cross-project connection (via the shared fail-closed resolver)
    # rather than previewing the wrong project's source data.
    from src.api._scope import resolve_source_connection
    model = await db.get(Model, model_id)
    if model is None:
        return None, "Model not found for source connection resolution."
    conn = await resolve_source_connection(
        db, source, expected_project_id=model.project_id
    )
    return conn, None


async def _resolve_preview_connection(
    db,
    *,
    model_id: UUID,
):
    conn, err = await _resolve_model_source_connection(db, model_id)
    if conn is None:
        return None, None, err
    connector = normalize_connection_type(
        (conn.connection_type or "").lower()
    )
    return conn, connector, None

async def _resolve_attribute(
    db,
    *,
    model_id: UUID,
    attribute_id: UUID,
    source: str,
    require_dimension_table: bool,
) -> _ResolvedAttribute:
    source = _normalize_source(source, field="attribute_source")
    if source == "physical_column":
        col = await db.get(ModelColumn, attribute_id)
        if col is None:
            raise _not_found("Attribute not found")
        table = await db.get(ModelTable, col.model_table_id)
        if table is None or table.model_id != model_id:
            raise _not_found("Attribute not found")
        data_type = col.data_type
        name = col.column_name
        table_name = table.alias or table.display_name or table.physical_name
    else:
        uda = await db.get(UserDefinedAttribute, attribute_id)
        if uda is None or uda.model_id != model_id:
            raise _not_found("Attribute not found")
        table = await db.get(ModelTable, uda.table_id)
        if table is None or table.model_id != model_id:
            raise _not_found("Attribute not found")
        data_type = uda.output_data_type
        name = uda.name
        table_name = table.alias or table.display_name or table.physical_name

    if require_dimension_table and is_fact_table(table):
        raise _validation_error(
            f"Attribute '{name}' belongs to a fact table. Hierarchy levels can only use dimension table attributes.",
            field="key_attribute_id",
            code="H7",
        )

    return _ResolvedAttribute(
        ref=HierarchyAttributeRef(
            id=attribute_id,
            name=name,
            table_id=table.id,
            table_name=table_name,
            data_type=data_type,
            source=source,
        ),
        table=table,
    )


async def _load_hierarchy_or_404(db, model_id: UUID, hierarchy_id: UUID) -> HierarchyDefinition:
    h = await db.get(HierarchyDefinition, hierarchy_id)
    if h is None or h.model_id != model_id:
        raise _not_found("Hierarchy not found")
    return h


async def _load_level_or_404(
    db,
    *,
    hierarchy_id: UUID,
    level_id: UUID,
) -> HierarchyLevel:
    level = await db.get(HierarchyLevel, level_id)
    if level is None or level.hierarchy_id != hierarchy_id:
        raise _not_found("Hierarchy level not found")
    return level


async def _levels_for_hierarchy(db, hierarchy_id: UUID) -> list[HierarchyLevel]:
    result = await db.execute(
        select(HierarchyLevel)
        .where(HierarchyLevel.hierarchy_id == hierarchy_id)
        .order_by(HierarchyLevel.ordinal)
    )
    return list(result.scalars().all())


async def _delete_unreferenced_generated_udas(
    db,
    *,
    model_id: UUID,
    candidate_uda_ids: list[UUID],
) -> list[UUID]:
    """Delete each candidate UDA only when nothing references it any more.

    Generated date hierarchies share level-key UDAs between hierarchies built on
    the same column (``reuse_existing`` in ``_create_generated_uda``). The old
    delete path removed every level-key UDA unconditionally, so deleting one
    hierarchy hard-deleted UDAs that a sibling hierarchy's level still keyed on,
    leaving dangling ``key_attribute_id`` references (F-016-07).

    MUST be called AFTER the target hierarchy/levels are deleted and flushed so
    the surviving-reference query reflects post-deletion state. A UDA is kept if
    any surviving ``HierarchyLevel``, ``HierarchyLevelAttribute`` or ``Measure``
    still references it. The UDA's companion generated ``Dimension`` (one per UDA,
    shared across sibling hierarchies built on the same column) is NOT a pin — it
    is deleted alongside the UDA so a kept UDA keeps its dimension and a removed
    UDA removes it. Returns the UDA ids actually deleted.
    """
    if not candidate_uda_ids:
        return []

    candidates = list(dict.fromkeys(candidate_uda_ids))

    # Collect every candidate UDA id still referenced by something other than its
    # own companion dimension — those are pinned and must survive.
    referenced: set[UUID] = set()

    level_keys = await db.execute(
        select(HierarchyLevel.key_attribute_id).where(
            HierarchyLevel.key_attribute_source == "user_defined_attribute",
            HierarchyLevel.key_attribute_id.in_(candidates),
        )
    )
    referenced.update(r[0] for r in level_keys.fetchall() if r[0] is not None)

    level_attrs = await db.execute(
        select(HierarchyLevelAttribute.attribute_id).where(
            HierarchyLevelAttribute.attribute_source == "user_defined_attribute",
            HierarchyLevelAttribute.attribute_id.in_(candidates),
        )
    )
    referenced.update(r[0] for r in level_attrs.fetchall() if r[0] is not None)

    meas_refs = await db.execute(
        select(Measure.user_defined_attribute_id).where(
            Measure.model_id == model_id,
            Measure.user_defined_attribute_id.in_(candidates),
        )
    )
    referenced.update(r[0] for r in meas_refs.fetchall() if r[0] is not None)

    deletable = [uid for uid in candidates if uid not in referenced]
    if not deletable:
        return []

    # Remove the companion virtual dimensions, the column refs, then the UDAs.
    await db.execute(
        delete(Dimension).where(
            Dimension.model_id == model_id,
            Dimension.user_defined_attribute_id.in_(deletable),
        )
    )
    await db.execute(
        delete(UserDefinedAttributeColumnRef).where(
            UserDefinedAttributeColumnRef.attribute_id.in_(deletable)
        )
    )
    await db.execute(
        delete(UserDefinedAttribute).where(UserDefinedAttribute.id.in_(deletable))
    )
    return deletable


def _calendar_instance_clause(model_id: UUID):
    """SQL boolean: the enclosing query's ``ModelTable`` row is a calendar
    instance in *model_id* — the single source of truth for "calendar-internal"
    used by the unassigned-date scan, the assigned-join test and the
    hierarchy-delete companion-alias collector (Bug-6683).

    Three shapes qualify:
      * marked alias (``calendar_table_id`` set) — normal auto-create output;
      * calendar spine (``table_type == 'calendar'``) — the registered
        calendar source table, whose backlink can be NULL when unbound;
      * unmarked alias — batch-date against an UNBOUND spine creates
        ``dim_detail`` aliases with ``calendar_table_id = NULL``; they are
        identified by sharing a spine's SOURCE-SCOPED physical_name
        (correlated EXISTS, so a same-named table in a different source does
        NOT qualify).
    """
    spine = aliased(ModelTable)
    return or_(
        ModelTable.calendar_table_id.is_not(None),
        ModelTable.table_type == "calendar",
        select(spine.id)
        .where(
            spine.model_id == model_id,
            spine.table_type == "calendar",
            spine.source_id == ModelTable.source_id,
            spine.physical_name == ModelTable.physical_name,
        )
        .exists(),
    )


async def _collect_companion_alias_table_ids(
    db, *, model_id: UUID, levels: list[HierarchyLevel]
) -> set[UUID]:
    """Return the ids of dedicated calendar-alias ModelTables that hold this
    hierarchy's level-key UDAs.

    Generated date hierarchies (batch-date / auto-create) materialise a
    companion ``dim_detail`` alias ModelTable — one per fact date column — that
    carries a calendar date-key column + the level UDAs, joined many-to-one to
    the fact column (hierarchies.py ``_auto_create_date_hierarchies_for_model``
    / ``batch-date``). Candidates are DEDICATED companion aliases only:
    ``table_type == 'dim_detail'`` rows that are calendar instances per
    ``_calendar_instance_clause`` — i.e. marked aliases (calendar_table_id
    set) and UNMARKED aliases created by batch-date against an unbound
    calendar (calendar_table_id NULL, spine physical_name match), which the
    previous marker-only predicate leaked on delete, leaving the alias + join
    behind so recreate cycles accumulated ``_2``-suffixed aliases (Bug-6683
    external review).

    Calendar SPINE registrations (``table_type == 'calendar'``) are explicitly
    NOT candidates: a spine is the model's calendar registration, never a
    per-fact-column companion, and deleting a user hierarchy that happens to
    key on a spine-hosted UDA must not tear the registration down (Bug-6683
    round-4 external review; this also tightens the pre-existing
    collectability of bound spines under the old marker-only predicate). The
    fact/dimension tables a *user* hierarchy keys on never match the clause,
    so they are never collected here; the reference scan in
    ``_delete_orphaned_companion_aliases`` remains the deletion safety net.
    """
    uda_ids = [
        lvl.key_attribute_id
        for lvl in levels
        if lvl.key_attribute_source == "user_defined_attribute" and lvl.key_attribute_id
    ]
    if not uda_ids:
        return set()
    rows = await db.execute(
        select(ModelTable.id)
        .join(UserDefinedAttribute, UserDefinedAttribute.table_id == ModelTable.id)
        .where(
            ModelTable.model_id == model_id,
            ModelTable.table_type == "dim_detail",
            _calendar_instance_clause(model_id),
            UserDefinedAttribute.id.in_(uda_ids),
        )
    )
    return {r[0] for r in rows.fetchall()}


async def _delete_orphaned_companion_aliases(
    db, *, model_id: UUID, candidate_table_ids: set[UUID]
) -> list[UUID]:
    """Delete each candidate calendar-alias ModelTable that nothing references
    any more (F-016-13).

    MUST run AFTER the hierarchy, its levels and its unreferenced generated
    UDAs are deleted and flushed. An alias is kept if any surviving
    ``HierarchyLevel`` / ``HierarchyLevelAttribute`` keys on a UDA that still
    lives on it, any ``Dimension`` references such a UDA, or a ``Measure``'s
    calendar/date binding still points at the alias. Deleting the ModelTable
    cascades its ModelColumns/UDAs; the companion ``Join`` rows are removed
    explicitly first (no ON DELETE cascade from model_tables to joins).
    """
    if not candidate_table_ids:
        return []

    deleted: list[UUID] = []
    for table_id in candidate_table_ids:
        # UDAs that still live on this alias table.
        surviving_uda_ids = {
            r[0]
            for r in (
                await db.execute(
                    select(UserDefinedAttribute.id).where(
                        UserDefinedAttribute.table_id == table_id
                    )
                )
            ).fetchall()
        }
        if surviving_uda_ids:
            # Any surviving hierarchy level/level-attribute still keying on one
            # of them pins the whole alias.
            lvl_ref = (
                await db.execute(
                    select(HierarchyLevel.id)
                    .where(
                        HierarchyLevel.key_attribute_source == "user_defined_attribute",
                        HierarchyLevel.key_attribute_id.in_(surviving_uda_ids),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if lvl_ref is not None:
                continue
            lvl_attr_ref = (
                await db.execute(
                    select(HierarchyLevelAttribute.id)
                    .where(
                        HierarchyLevelAttribute.attribute_source == "user_defined_attribute",
                        HierarchyLevelAttribute.attribute_id.in_(surviving_uda_ids),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if lvl_attr_ref is not None:
                continue
            dim_ref = (
                await db.execute(
                    select(Dimension.id)
                    .where(
                        Dimension.model_id == model_id,
                        Dimension.user_defined_attribute_id.in_(surviving_uda_ids),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if dim_ref is not None:
                continue
            meas_ref = (
                await db.execute(
                    select(Measure.id)
                    .where(
                        Measure.model_id == model_id,
                        Measure.user_defined_attribute_id.in_(surviving_uda_ids),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if meas_ref is not None:
                continue

        # A measure may bind a calendar/date directly to this alias ModelTable.
        meas_cal_ref = (
            await db.execute(
                select(Measure.id)
                .where(
                    Measure.model_id == model_id,
                    or_(
                        Measure.calendar_model_table_id == table_id,
                        Measure.date_dimension_column_id.in_(
                            select(ModelColumn.id).where(
                                ModelColumn.model_table_id == table_id
                            )
                        ),
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if meas_cal_ref is not None:
            continue

        # Orphan: remove its joins, then the table (cascades columns + UDAs).
        await db.execute(
            delete(Join).where(
                Join.model_id == model_id,
                or_(
                    Join.left_table_id == table_id,
                    Join.right_table_id == table_id,
                ),
            )
        )
        alias_tbl = await db.get(ModelTable, table_id)
        if alias_tbl is not None:
            await db.delete(alias_tbl)
            deleted.append(table_id)
    if deleted:
        await db.flush()
    return deleted


async def _collect_level_uda_ids(db, levels: list[HierarchyLevel]) -> list[UUID]:
    """UDA ids keyed by, or attached to, the given levels (deduplicated)."""
    ids: list[UUID] = [
        lvl.key_attribute_id
        for lvl in levels
        if lvl.key_attribute_source == "user_defined_attribute"
    ]
    level_ids = [lvl.id for lvl in levels]
    if level_ids:
        attr_rows = await db.execute(
            select(HierarchyLevelAttribute.attribute_id).where(
                HierarchyLevelAttribute.level_id.in_(level_ids),
                HierarchyLevelAttribute.attribute_source == "user_defined_attribute",
            )
        )
        ids.extend(r[0] for r in attr_rows.fetchall() if r[0] is not None)
    return list(dict.fromkeys(ids))


async def _build_join_graph(db, model_id: UUID) -> dict[UUID, set[UUID]]:
    result = await db.execute(select(Join).where(Join.model_id == model_id))
    graph: dict[UUID, set[UUID]] = defaultdict(set)
    for j in result.scalars().all():
        graph[j.left_table_id].add(j.right_table_id)
        graph[j.right_table_id].add(j.left_table_id)
    return graph


def _has_table_path(graph: dict[UUID, set[UUID]], start: UUID, end: UUID) -> bool:
    if start == end:
        return True
    queue: deque[UUID] = deque([start])
    seen: set[UUID] = {start}
    while queue:
        cur = queue.popleft()
        for nxt in graph.get(cur, set()):
            if nxt in seen:
                continue
            if nxt == end:
                return True
            seen.add(nxt)
            queue.append(nxt)
    return False


async def _validate_level_connectivity(db, model_id: UUID, hierarchy_id: UUID) -> None:
    levels = await _levels_for_hierarchy(db, hierarchy_id)
    if not levels:
        return

    expected = list(range(len(levels)))
    found = [lvl.ordinal for lvl in levels]
    if found != expected:
        raise _validation_error(
            "Level ordinals must be contiguous integers starting from 0.",
            field="ordinal",
            code="H5",
        )

    resolved: list[_ResolvedAttribute] = []
    for lvl in levels:
        resolved_attr = await _resolve_attribute(
            db,
            model_id=model_id,
            attribute_id=lvl.key_attribute_id,
            source=lvl.key_attribute_source,
            require_dimension_table=False,
        )
        resolved.append(resolved_attr)

    graph = await _build_join_graph(db, model_id)
    for idx in range(len(resolved) - 1):
        left = resolved[idx]
        right = resolved[idx + 1]
        if left.table.id == right.table.id:
            continue
        if not _has_table_path(graph, left.table.id, right.table.id):
            raise _validation_error(
                (
                    "No join path between table "
                    f"'{left.table.alias or left.table.physical_name}' (level '{levels[idx].name}') and "
                    f"table '{right.table.alias or right.table.physical_name}' (level '{levels[idx + 1].name}'). "
                    "Define a join first."
                ),
                field="levels",
                code="J1",
            )


async def _level_attributes_response(db, level: HierarchyLevel, model_id: UUID) -> list[HierarchyLevelAttributeResponse]:
    result = await db.execute(
        select(HierarchyLevelAttribute)
        .where(HierarchyLevelAttribute.level_id == level.id)
        .order_by(HierarchyLevelAttribute.id)
    )
    attrs = []
    for item in result.scalars().all():
        resolved = await _resolve_attribute(
            db,
            model_id=model_id,
            attribute_id=item.attribute_id,
            source=item.attribute_source,
            require_dimension_table=False,
        )
        attrs.append(
            HierarchyLevelAttributeResponse(
                id=item.id,
                attribute=resolved.ref,
                role=item.role,
            )
        )
    return attrs


async def _level_response(db, level: HierarchyLevel, model_id: UUID) -> HierarchyLevelResponse:
    key_attr = await _resolve_attribute(
        db,
        model_id=model_id,
        attribute_id=level.key_attribute_id,
        source=level.key_attribute_source,
        require_dimension_table=False,
    )
    attrs = await _level_attributes_response(db, level, model_id)
    return HierarchyLevelResponse(
        id=level.id,
        name=level.name,
        ordinal=level.ordinal,
        key_attribute=key_attr.ref,
        attributes=attrs,
        description=level.description,
        time_unit=level.time_unit,
        allowed_time_calcs=list(level.allowed_time_calcs or []),
    )


async def _hierarchy_details_batch(
    db,
    hierarchies: list[HierarchyDefinition],
    model_id: UUID,
    *,
    excluded_attr_ids: set[UUID] | None = None,
) -> list[HierarchyDetailResponse]:
    """Build hierarchy details from one model-scoped metadata batch.

    The model editor opens a whole catalogue, so resolving each hierarchy with
    ``_hierarchy_detail`` turns one request into a hierarchy/level/attribute
    fan-out.  Keep the endpoint's response contract, but load each metadata
    relation once and resolve references from in-memory maps.  The fixed set of
    statements is intentional: it remains bounded when a model has hundreds
    of hierarchies and also avoids ``Session.get`` calls hidden inside a loop.
    """
    hierarchy_ids = [hierarchy.id for hierarchy in hierarchies]
    levels_result = await db.execute(
        select(HierarchyLevel)
        .where(HierarchyLevel.hierarchy_id.in_(hierarchy_ids))
        .order_by(HierarchyLevel.hierarchy_id, HierarchyLevel.ordinal)
    )
    all_levels = list(levels_result.scalars().all())
    levels_by_hierarchy: dict[UUID, list[HierarchyLevel]] = defaultdict(list)
    visible_levels: list[HierarchyLevel] = []
    for level in all_levels:
        if excluded_attr_ids is not None and level.key_attribute_id in excluded_attr_ids:
            continue
        levels_by_hierarchy[level.hierarchy_id].append(level)
        visible_levels.append(level)

    level_ids = [level.id for level in visible_levels]
    attributes_result = await db.execute(
        select(HierarchyLevelAttribute)
        .where(HierarchyLevelAttribute.level_id.in_(level_ids))
        .order_by(HierarchyLevelAttribute.level_id, HierarchyLevelAttribute.id)
    )
    attrs_by_level: dict[UUID, list[HierarchyLevelAttribute]] = defaultdict(list)
    all_attributes = list(attributes_result.scalars().all())
    for item in all_attributes:
        attrs_by_level[item.level_id].append(item)

    # Keep all three catalogue reads unconditional.  Besides making the query
    # budget explicit, this means a model containing only physical columns or
    # only UDAs has the same bounded request shape as a mixed model.
    physical_ids = {
        level.key_attribute_id
        for level in visible_levels
        if _normalize_source(level.key_attribute_source, field="key_attribute_source")
        == "physical_column"
    }
    uda_ids = {
        level.key_attribute_id
        for level in visible_levels
        if _normalize_source(level.key_attribute_source, field="key_attribute_source")
        == "user_defined_attribute"
    }
    for item in all_attributes:
        source = _normalize_source(item.attribute_source, field="attribute_source")
        if source == "physical_column":
            physical_ids.add(item.attribute_id)
        else:
            uda_ids.add(item.attribute_id)

    columns_result = await db.execute(
        select(ModelColumn).where(ModelColumn.id.in_(physical_ids))
    )
    columns_by_id = {column.id: column for column in columns_result.scalars().all()}
    udas_result = await db.execute(
        select(UserDefinedAttribute).where(
            UserDefinedAttribute.model_id == model_id,
            UserDefinedAttribute.id.in_(uda_ids),
        )
    )
    udas_by_id = {uda.id: uda for uda in udas_result.scalars().all()}
    table_ids = {
        column.model_table_id for column in columns_by_id.values()
    } | {
        uda.table_id for uda in udas_by_id.values()
    }
    tables_result = await db.execute(
        select(ModelTable).where(
            ModelTable.model_id == model_id,
            ModelTable.id.in_(table_ids),
        )
    )
    tables_by_id = {table.id: table for table in tables_result.scalars().all()}

    def resolve(attribute_id: UUID, source: str) -> _ResolvedAttribute:
        source = _normalize_source(source, field="attribute_source")
        if source == "physical_column":
            column = columns_by_id.get(attribute_id)
            if column is None:
                raise _not_found("Attribute not found")
            table = tables_by_id.get(column.model_table_id)
            if table is None:
                raise _not_found("Attribute not found")
            name = column.column_name
            data_type = column.data_type
        else:
            uda = udas_by_id.get(attribute_id)
            if uda is None:
                raise _not_found("Attribute not found")
            table = tables_by_id.get(uda.table_id)
            if table is None:
                raise _not_found("Attribute not found")
            name = uda.name
            data_type = uda.output_data_type
        return _ResolvedAttribute(
            ref=HierarchyAttributeRef(
                id=attribute_id,
                name=name,
                table_id=table.id,
                table_name=table.alias or table.display_name or table.physical_name,
                data_type=data_type,
                source=source,
            ),
            table=table,
        )

    details: list[HierarchyDetailResponse] = []
    for hierarchy in hierarchies:
        level_items: list[HierarchyLevelResponse] = []
        for level in levels_by_hierarchy.get(hierarchy.id, []):
            key_attr = resolve(level.key_attribute_id, level.key_attribute_source)
            attr_items = [
                HierarchyLevelAttributeResponse(
                    id=item.id,
                    attribute=resolve(item.attribute_id, item.attribute_source).ref,
                    role=item.role,
                )
                for item in attrs_by_level.get(level.id, [])
            ]
            level_items.append(
                HierarchyLevelResponse(
                    id=level.id,
                    name=level.name,
                    ordinal=level.ordinal,
                    key_attribute=key_attr.ref,
                    attributes=attr_items,
                    description=level.description,
                    time_unit=level.time_unit,
                    allowed_time_calcs=list(level.allowed_time_calcs or []),
                )
            )
        if excluded_attr_ids is not None and not level_items:
            continue
        details.append(
            HierarchyDetailResponse(
                id=hierarchy.id,
                model_id=hierarchy.model_id,
                name=hierarchy.name,
                type=hierarchy.type,
                dimension_kind=hierarchy.dimension_kind,
                description=hierarchy.description,
                segment_config=hierarchy.segment_config,
                date_config=hierarchy.date_config,
                calendar_type=hierarchy.calendar_type,
                fiscal_year_start_month=hierarchy.fiscal_year_start_month,
                levels=level_items,
                created_at=hierarchy.created_at,
                updated_at=hierarchy.updated_at,
            )
        )
    return details


async def _hierarchy_detail(
    db,
    hierarchy: HierarchyDefinition,
    *,
    excluded_attr_ids: set[UUID] | None = None,
) -> HierarchyDetailResponse:
    levels = await _levels_for_hierarchy(db, hierarchy.id)
    if excluded_attr_ids is not None:
        levels = [lv for lv in levels if lv.key_attribute_id not in excluded_attr_ids]
    level_items = [await _level_response(db, lvl, hierarchy.model_id) for lvl in levels]
    return HierarchyDetailResponse(
        id=hierarchy.id,
        model_id=hierarchy.model_id,
        name=hierarchy.name,
        type=hierarchy.type,
        dimension_kind=hierarchy.dimension_kind,
        description=hierarchy.description,
        segment_config=hierarchy.segment_config,
        date_config=hierarchy.date_config,
        calendar_type=hierarchy.calendar_type,
        fiscal_year_start_month=hierarchy.fiscal_year_start_month,
        levels=level_items,
        created_at=hierarchy.created_at,
        updated_at=hierarchy.updated_at,
    )


async def _reindex_levels(db, hierarchy_id: UUID) -> None:
    levels = await _levels_for_hierarchy(db, hierarchy_id)
    for idx, lvl in enumerate(levels):
        lvl.ordinal = 10000 + idx
    await db.flush()
    for idx, lvl in enumerate(levels):
        lvl.ordinal = idx
    await db.flush()


async def _generated_ref(
    db,
    *,
    model_id: UUID,
    uda: UserDefinedAttribute,
) -> HierarchyAttributeRef:
    resolved = await _resolve_attribute(
        db,
        model_id=model_id,
        attribute_id=uda.id,
        source="user_defined_attribute",
        require_dimension_table=False,
    )
    return resolved.ref


def _build_estimate_sql(
    connector: str,
    *,
    table_name: str,
    key_expr: str,
    sample_size: int,
    rls_where: str | None = None,
) -> str:
    pg_quoted = quote_table_ref("postgresql", table_name)
    where = f"({key_expr}) IS NOT NULL"
    if rls_where:
        where += f" AND ({rls_where})"
    canonical = (
        "SELECT COUNT(*) AS c FROM ("
        f"SELECT DISTINCT {key_expr} AS k "
        f"FROM {pg_quoted} AS t "
        f"WHERE {where} "
        f"LIMIT {sample_size * 20}"
        ") s"
    )
    return transpile_preview_sql(connector, canonical)


def _build_sample_sql(
    connector: str,
    *,
    table_name: str,
    key_expr: str,
    sample_size: int,
    parent_expr: str | None = None,
    parent_key: str | None = None,
    rls_where: str | None = None,
    caption_expr: str | None = None,
) -> str:
    pg_quoted = quote_table_ref("postgresql", table_name)
    where = f"({key_expr}) IS NOT NULL"
    if parent_expr is not None and parent_key is not None:
        # F-016-19: build the string literal through sqlglot's literal builder
        # so escaping is library-handled rather than a hand-rolled
        # ``replace("'", "''")``. The whole statement is parsed (read="postgres")
        # and re-emitted per dialect by transpile_preview_sql, so the literal is
        # produced by sqlglot, never spliced raw.
        safe_key = exp.Literal.string(parent_key).sql(dialect="postgres")
        # Canonical CAST; transpile_preview_sql renders the dialect-specific type.
        where += f" AND CAST(({parent_expr}) AS TEXT) = {safe_key}"
    if rls_where:
        where += f" AND ({rls_where})"
    # Bug-3617 (Phase 0.5a): select the display caption alongside the key when
    # the level has a display-role attribute. Omitted (single-column, identical
    # to the legacy query) when no caption_expr is supplied — back-compatible.
    select_cols = f"{key_expr} AS key_value"
    if caption_expr is not None:
        select_cols += f", {caption_expr} AS caption_value"
    canonical = (
        f"SELECT DISTINCT {select_cols} "
        f"FROM {pg_quoted} AS t "
        f"WHERE {where} "
        "ORDER BY 1 "
        f"LIMIT {sample_size}"
    )
    return transpile_preview_sql(connector, canonical)


def _build_ancestor_path_sample_sql(
    connector: str,
    *,
    table_name: str,
    key_exprs: list[str],
    sample_size: int,
    rls_where: str | None = None,
    caption_expr: str | None = None,
) -> str:
    """Bug-3617 (Phase 0.5b): sample distinct ancestor key TUPLES for a level.

    Returns one row per member at the target level carrying its full
    ancestor-first key path (``key_0 .. key_n``, where ``key_n`` is the level's
    own key). Used for parent-less whole-level enumeration of a single-table
    hierarchy, where the flat per-level sample (``_build_sample_sql``) would lose
    the ancestor context the canonical composite member unique name needs
    (e.g. distinguishing month 4 of 2025 from month 4 of 2026). Canonical-PG,
    transpiled once; key/caption expressions are already resolved + quoted
    against alias ``t`` by the caller (``connector_qualify`` convention).
    """
    pg_quoted = quote_table_ref("postgresql", table_name)
    select_parts = [f"{expr} AS key_{i}" for i, expr in enumerate(key_exprs)]
    if caption_expr is not None:
        select_parts.append(f"{caption_expr} AS caption_value")
    where_parts = [f"({expr}) IS NOT NULL" for expr in key_exprs]
    if rls_where:
        where_parts.append(f"({rls_where})")
    order_by = ", ".join(str(i + 1) for i in range(len(key_exprs)))
    canonical = (
        f"SELECT DISTINCT {', '.join(select_parts)} "
        f"FROM {pg_quoted} AS t "
        f"WHERE {' AND '.join(where_parts)} "
        f"ORDER BY {order_by} "
        f"LIMIT {sample_size}"
    )
    return transpile_preview_sql(connector, canonical)


async def _resolve_join_between(
    db, *, model_id: UUID, table_a_id: UUID, table_b_id: UUID,
) -> tuple[str, str] | None:
    """Find a single model ``Join`` connecting two tables and return the
    ``(column_on_a, column_on_b)`` physical column names.

    F-016-24: cross-table hierarchy preview filters a child level by its parent
    via the model's defined join. Only a direct (one-hop) join is resolved —
    multi-hop paths return None and the caller falls back to the unsupported
    warning. The join is symmetric, so we check both orientations.
    """
    from shared.db.models import Join

    rows = (
        await db.execute(
            select(Join).where(
                Join.model_id == model_id,
                or_(
                    and_(Join.left_table_id == table_a_id, Join.right_table_id == table_b_id),
                    and_(Join.left_table_id == table_b_id, Join.right_table_id == table_a_id),
                ),
            )
        )
    ).scalars().all()
    if not rows:
        return None
    join = rows[0]
    left_col = await db.get(ModelColumn, join.left_column_id)
    right_col = await db.get(ModelColumn, join.right_column_id)
    if left_col is None or right_col is None:
        return None
    # Orient to (column_on_a, column_on_b).
    if join.left_table_id == table_a_id:
        return left_col.column_name, right_col.column_name
    return right_col.column_name, left_col.column_name


def _build_cross_table_sample_sql(
    connector: str,
    *,
    child_table: str,
    child_key_expr: str,
    child_join_col: str,
    parent_table: str,
    parent_key_expr: str,
    parent_join_col: str,
    parent_key: str,
    sample_size: int,
    rls_where: str | None = None,
    caption_expr: str | None = None,
) -> str:
    """Sample distinct child-level keys filtered to one parent member, joining
    the child table to the parent table on the model's defined join columns
    (F-016-24). Canonical-PG, then transpiled to the source dialect; the parent
    filter literal is produced by sqlglot, never spliced raw (F-016-19 pattern).
    The key expressions are resolved against alias ``t`` (child) but the join
    columns are quoted bare and prefixed here so they bind to the right side."""
    pg_child = quote_table_ref("postgresql", child_table)
    pg_parent = quote_table_ref("postgresql", parent_table)
    child_jc = quote_identifier("postgresql", child_join_col)
    parent_jc = quote_identifier("postgresql", parent_join_col)
    safe_key = exp.Literal.string(parent_key).sql(dialect="postgres")
    rls_clause = f" AND ({rls_where})" if rls_where else ""
    # Bug-3617 (Phase 0.5a): caption selected alongside key when available.
    select_cols = f"{child_key_expr} AS key_value"
    if caption_expr is not None:
        select_cols += f", {caption_expr} AS caption_value"
    canonical = (
        f"SELECT DISTINCT {select_cols} "
        f"FROM {pg_child} AS t "
        f"JOIN {pg_parent} AS p ON t.{child_jc} = p.{parent_jc} "
        f"WHERE ({child_key_expr}) IS NOT NULL "
        f"AND CAST(({parent_key_expr}) AS TEXT) = {safe_key}"
        f"{rls_clause} "
        "ORDER BY 1 "
        f"LIMIT {sample_size}"
    )
    return transpile_preview_sql(connector, canonical)


@router.post(
    "/hierarchies",
    response_model=HierarchyDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_hierarchy(
    project_id: UUID,
    model_id: UUID,
    body: HierarchyCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> HierarchyDetailResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        if not is_valid_dimension_kind(body.dimension_kind):
            raise _validation_error(
                f"Unsupported dimension_kind '{body.dimension_kind}'.",
                field="dimension_kind",
                code="D2",
            )
        _validate_calendar_fields(body.calendar_type, body.fiscal_year_start_month)
        hierarchy = HierarchyDefinition(
            model_id=model_id,
            name=body.name,
            type=_normalize_hierarchy_type(body.type),
            dimension_kind=body.dimension_kind,
            description=body.description,
            segment_config=body.segment_config,
            date_config=body.date_config,
            calendar_type=normalize_calendar_type(body.calendar_type),
            fiscal_year_start_month=body.fiscal_year_start_month,
        )
        db.add(hierarchy)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise _validation_error(
                f"A hierarchy named '{body.name}' already exists in this model.",
                field="name",
                code="H1",
            )
        await db.refresh(hierarchy)
        return await _hierarchy_detail(db, hierarchy)


@router.get("/hierarchies", response_model=list[HierarchySummaryResponse])
async def list_hierarchies(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[HierarchySummaryResponse]:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        stmt = (
            select(HierarchyDefinition)
            .where(HierarchyDefinition.model_id == model_id)
        )
        if persona:
            allowed = parse_allowed_ids(persona.included_hierarchy_ids)
            if allowed is not None:
                stmt = stmt.where(HierarchyDefinition.id.in_(allowed))
        stmt = stmt.order_by(HierarchyDefinition.name)
        result = await db.execute(stmt)

        excluded_attrs = (
            await get_excluded_level_attribute_ids(db, model_id=model_id, persona=persona)
            if persona else None
        )

        hierarchies = result.scalars().all()
        hierarchy_ids = [h.id for h in hierarchies]

        levels_by_hierarchy: dict[UUID, list] = {hid: [] for hid in hierarchy_ids}
        if hierarchy_ids:
            all_levels = (
                await db.execute(
                    select(HierarchyLevel.id, HierarchyLevel.name, HierarchyLevel.ordinal,
                           HierarchyLevel.key_attribute_id, HierarchyLevel.hierarchy_id)
                    .where(HierarchyLevel.hierarchy_id.in_(hierarchy_ids))
                    .order_by(HierarchyLevel.hierarchy_id, HierarchyLevel.ordinal)
                )
            ).all()
            for lv in all_levels:
                if excluded_attrs is not None and lv.key_attribute_id in excluded_attrs:
                    continue
                levels_by_hierarchy[lv.hierarchy_id].append(lv)

        items = []
        for h in hierarchies:
            levels_result = levels_by_hierarchy.get(h.id, [])
            if excluded_attrs is not None and not levels_result:
                continue
            items.append(
                HierarchySummaryResponse(
                    id=h.id,
                    model_id=h.model_id,
                    name=h.name,
                    type=h.type,
                    dimension_kind=h.dimension_kind,
                    description=h.description,
                    calendar_type=h.calendar_type,
                    fiscal_year_start_month=h.fiscal_year_start_month,
                    level_count=len(levels_result),
                    level_names=[lv.name for lv in levels_result],
                    created_at=h.created_at,
                    updated_at=h.updated_at,
                )
            )
        return items


@router.get("/hierarchies/with-levels", response_model=list[HierarchyDetailResponse])
async def list_hierarchies_with_levels(
    project_id: UUID,
    model_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[HierarchyDetailResponse]:
    """Return one model-scoped hierarchy/level catalogue for editor opens.

    The canvas needs level attributes to build persona bindings. Keeping this
    contract model-scoped avoids turning one model open into one request per
    hierarchy while retaining the same persona filtering as the summary and
    detail routes.
    """
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        stmt = select(HierarchyDefinition).where(
            HierarchyDefinition.model_id == model_id,
        )
        if persona:
            allowed = parse_allowed_ids(persona.included_hierarchy_ids)
            if allowed is not None:
                stmt = stmt.where(HierarchyDefinition.id.in_(allowed))
        hierarchies = (await db.execute(stmt.order_by(HierarchyDefinition.name))).scalars().all()
        excluded_attrs = (
            await get_excluded_level_attribute_ids(db, model_id=model_id, persona=persona)
            if persona else None
        )
        return await _hierarchy_details_batch(
            db,
            list(hierarchies),
            model_id,
            excluded_attr_ids=excluded_attrs,
        )


@router.get("/hierarchies/{hierarchy_id}", response_model=HierarchyDetailResponse)
async def get_hierarchy(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> HierarchyDetailResponse:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        hierarchy = await _load_hierarchy_or_404(db, model_id, hierarchy_id)
        if persona:
            allowed = parse_allowed_ids(persona.included_hierarchy_ids)
            if allowed is not None and hierarchy.id not in allowed:
                raise HTTPException(status_code=404, detail="Hierarchy not found")
        excluded_attrs = (
            await get_excluded_level_attribute_ids(db, model_id=model_id, persona=persona)
            if persona else None
        )
        return await _hierarchy_detail(db, hierarchy, excluded_attr_ids=excluded_attrs)


@router.put(
    "/hierarchies/{hierarchy_id}",
    response_model=HierarchyDetailResponse,
    dependencies=[require_role("modeler")],
)
async def update_hierarchy(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    body: HierarchyUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> HierarchyDetailResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        hierarchy = await _load_hierarchy_or_404(db, model_id, hierarchy_id)
        updates = body.model_dump(exclude_unset=True)
        if "type" in updates:
            hierarchy.type = _normalize_hierarchy_type(updates["type"])
        if "dimension_kind" in updates:
            if not is_valid_dimension_kind(updates["dimension_kind"]):
                raise _validation_error(
                    f"Unsupported dimension_kind '{updates['dimension_kind']}'.",
                    field="dimension_kind",
                    code="D2",
                )
            hierarchy.dimension_kind = updates["dimension_kind"]
        if "name" in updates:
            hierarchy.name = updates["name"]
        if "description" in updates:
            hierarchy.description = updates["description"]
        if "segment_config" in updates:
            hierarchy.segment_config = updates["segment_config"]
        if "date_config" in updates:
            hierarchy.date_config = updates["date_config"]
        if "calendar_type" in updates or "fiscal_year_start_month" in updates:
            cal_type = normalize_calendar_type(
                updates.get("calendar_type", hierarchy.calendar_type)
            )
            fiscal_month = updates.get("fiscal_year_start_month", hierarchy.fiscal_year_start_month)
            _validate_calendar_fields(cal_type, fiscal_month)
            hierarchy.calendar_type = cal_type
            hierarchy.fiscal_year_start_month = fiscal_month
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise _validation_error(
                f"A hierarchy named '{hierarchy.name}' already exists in this model.",
                field="name",
                code="H1",
            )
        await db.refresh(hierarchy)
        return await _hierarchy_detail(db, hierarchy)


@router.delete(
    "/hierarchies/{hierarchy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_hierarchy(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    from src.api.personas import strip_id_from_personas

    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        hierarchy = await _load_hierarchy_or_404(db, model_id, hierarchy_id)

        levels = await _levels_for_hierarchy(db, hierarchy_id)
        candidate_uda_ids = await _collect_level_uda_ids(db, levels)
        # F-016-13: capture the companion calendar-alias table(s) this hierarchy
        # was generated onto BEFORE deletion (the level UDAs still resolve).
        companion_alias_ids = await _collect_companion_alias_table_ids(
            db, model_id=model_id, levels=levels
        )

        await strip_id_from_personas(
            db, model_id=model_id, object_id=hierarchy_id, object_class="hierarchy"
        )
        # Delete the hierarchy (cascades its levels + level attributes) and flush
        # so the reference scan in _delete_unreferenced_generated_udas sees the
        # post-deletion state — a UDA shared with a sibling hierarchy survives.
        await db.delete(hierarchy)
        await db.flush()

        deleted_uda_ids = await _delete_unreferenced_generated_udas(
            db, model_id=model_id, candidate_uda_ids=candidate_uda_ids
        )
        if deleted_uda_ids:
            # Drop the virtual dimensions only for UDAs actually removed; a UDA
            # kept for a sibling hierarchy keeps its dimension too (F-016-07).
            await db.execute(
                delete(Dimension).where(
                    Dimension.model_id == model_id,
                    Dimension.user_defined_attribute_id.in_(deleted_uda_ids),
                )
            )

        # F-016-13: drop the companion alias table + its join when the hierarchy
        # was generated onto a dedicated calendar alias and nothing else
        # references it any more (re-running batch-date otherwise accumulated
        # ``order_date_calendar_2`` clutter each cycle).
        await _delete_orphaned_companion_aliases(
            db, model_id=model_id, candidate_table_ids=companion_alias_ids
        )

        await db.commit()


@router.post(
    "/hierarchies/{hierarchy_id}/levels",
    response_model=HierarchyLevelResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_hierarchy_level(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    body: HierarchyLevelCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> HierarchyLevelResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        await _load_hierarchy_or_404(db, model_id, hierarchy_id)
        existing_levels = await _levels_for_hierarchy(db, hierarchy_id)
        if body.ordinal > len(existing_levels):
            raise _validation_error(
                f"Invalid ordinal {body.ordinal}. Must be between 0 and {len(existing_levels)}.",
                field="ordinal",
                code="H5",
            )

        key_source = _normalize_source(body.key_attribute_source, field="key_attribute_source")
        await _resolve_attribute(
            db,
            model_id=model_id,
            attribute_id=body.key_attribute_id,
            source=key_source,
            require_dimension_table=False,
        )
        for attr in body.attributes:
            attr_source = _normalize_source(attr.attribute_source, field="attribute_source")
            if attr.role not in ALLOWED_ATTRIBUTE_ROLES:
                raise _validation_error(
                    f"Unsupported level attribute role '{attr.role}'",
                    field="role",
                    code="H3",
                )
            await _resolve_attribute(
                db,
                model_id=model_id,
                attribute_id=attr.attribute_id,
                source=attr_source,
                require_dimension_table=False,
            )

        await db.execute(
            update(HierarchyLevel)
            .where(
                HierarchyLevel.hierarchy_id == hierarchy_id,
                HierarchyLevel.ordinal >= body.ordinal,
            )
            .values(ordinal=HierarchyLevel.ordinal + 1)
        )

        if not is_valid_time_unit(body.time_unit):
            raise _validation_error(
                f"Unsupported time_unit '{body.time_unit}'.",
                field="time_unit",
                code="D2",
            )
        if not are_valid_time_calcs(body.allowed_time_calcs):
            raise _validation_error(
                f"Unknown token in allowed_time_calcs: {body.allowed_time_calcs}.",
                field="allowed_time_calcs",
                code="D2",
            )
        level = HierarchyLevel(
            hierarchy_id=hierarchy_id,
            name=body.name,
            ordinal=body.ordinal,
            key_attribute_id=body.key_attribute_id,
            key_attribute_source=key_source,
            description=body.description,
            time_unit=body.time_unit,
            allowed_time_calcs=list(body.allowed_time_calcs or []),
        )
        db.add(level)
        await db.flush()

        for item in body.attributes:
            db.add(
                HierarchyLevelAttribute(
                    level_id=level.id,
                    attribute_id=item.attribute_id,
                    attribute_source=_normalize_source(item.attribute_source, field="attribute_source"),
                    role=item.role,
                )
            )

        try:
            await _reindex_levels(db, hierarchy_id)
            await _validate_level_connectivity(db, model_id, hierarchy_id)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise _validation_error(
                f"Level '{body.name}' or key attribute already exists in this hierarchy.",
                field="name",
                code="H6",
            )
        await db.refresh(level)
        return await _level_response(db, level, model_id)


@router.get("/hierarchies/{hierarchy_id}/levels", response_model=list[HierarchyLevelResponse])
async def list_hierarchy_levels(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[HierarchyLevelResponse]:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        hierarchy = await _load_hierarchy_or_404(db, model_id, hierarchy_id)
        if persona:
            allowed = parse_allowed_ids(persona.included_hierarchy_ids)
            if allowed is not None and hierarchy.id not in allowed:
                raise HTTPException(status_code=404, detail="Hierarchy not found")
        levels = await _levels_for_hierarchy(db, hierarchy_id)
        excluded_attrs = (
            await get_excluded_level_attribute_ids(db, model_id=model_id, persona=persona)
            if persona else None
        )
        if excluded_attrs is not None:
            levels = [lv for lv in levels if lv.key_attribute_id not in excluded_attrs]
        return [await _level_response(db, level, model_id) for level in levels]


@router.put(
    "/hierarchies/{hierarchy_id}/levels/reorder",
    response_model=list[HierarchyLevelResponse],
    dependencies=[require_role("modeler")],
)
async def reorder_hierarchy_levels(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    body: HierarchyReorderRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[HierarchyLevelResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        await _load_hierarchy_or_404(db, model_id, hierarchy_id)
        levels = await _levels_for_hierarchy(db, hierarchy_id)
        existing_ids = {lvl.id for lvl in levels}
        requested_ids = list(body.level_ids_in_order)
        if existing_ids != set(requested_ids) or len(requested_ids) != len(existing_ids):
            raise _validation_error(
                "level_ids_in_order must include each hierarchy level exactly once.",
                field="level_ids_in_order",
                code="H5",
            )
        by_id = {lvl.id: lvl for lvl in levels}
        for idx, lvl_id in enumerate(requested_ids):
            by_id[lvl_id].ordinal = 10000 + idx
        await db.flush()
        for idx, lvl_id in enumerate(requested_ids):
            by_id[lvl_id].ordinal = idx
        await db.flush()
        await _validate_level_connectivity(db, model_id, hierarchy_id)
        await db.commit()
        updated = await _levels_for_hierarchy(db, hierarchy_id)
        return [await _level_response(db, level, model_id) for level in updated]


@router.put(
    "/hierarchies/{hierarchy_id}/levels/{level_id}",
    response_model=HierarchyLevelResponse,
    dependencies=[require_role("modeler")],
)
async def update_hierarchy_level(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    level_id: UUID,
    body: HierarchyLevelUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> HierarchyLevelResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        await _load_hierarchy_or_404(db, model_id, hierarchy_id)
        level = await _load_level_or_404(db, hierarchy_id=hierarchy_id, level_id=level_id)
        updates = body.model_dump(exclude_unset=True)

        if "key_attribute_id" in updates or "key_attribute_source" in updates:
            key_attr_id = updates.get("key_attribute_id", level.key_attribute_id)
            key_source = updates.get("key_attribute_source", level.key_attribute_source)
            key_source = _normalize_source(key_source, field="key_attribute_source")
            await _resolve_attribute(
                db,
                model_id=model_id,
                attribute_id=key_attr_id,
                source=key_source,
                require_dimension_table=False,
            )
            level.key_attribute_id = key_attr_id
            level.key_attribute_source = key_source

        if "name" in updates:
            level.name = updates["name"]
        if "description" in updates:
            level.description = updates["description"]
        if "time_unit" in updates:
            if not is_valid_time_unit(updates["time_unit"]):
                raise _validation_error(
                    f"Unsupported time_unit '{updates['time_unit']}'.",
                    field="time_unit",
                    code="D2",
                )
            level.time_unit = updates["time_unit"]
        if "allowed_time_calcs" in updates:
            calcs = updates["allowed_time_calcs"] or []
            if not are_valid_time_calcs(calcs):
                raise _validation_error(
                    f"Unknown token in allowed_time_calcs: {calcs}.",
                    field="allowed_time_calcs",
                    code="D2",
                )
            level.allowed_time_calcs = list(calcs)

        if "ordinal" in updates and updates["ordinal"] != level.ordinal:
            target = updates["ordinal"]
            old = level.ordinal
            level_count = len(await _levels_for_hierarchy(db, hierarchy_id))
            if target < 0 or target >= level_count:
                raise _validation_error(
                    f"Invalid ordinal {target}. Must be between 0 and {level_count - 1}.",
                    field="ordinal",
                    code="H5",
                )
            if target > old:
                await db.execute(
                    update(HierarchyLevel)
                    .where(
                        HierarchyLevel.hierarchy_id == hierarchy_id,
                        HierarchyLevel.ordinal > old,
                        HierarchyLevel.ordinal <= target,
                    )
                    .values(ordinal=HierarchyLevel.ordinal - 1)
                )
            else:
                await db.execute(
                    update(HierarchyLevel)
                    .where(
                        HierarchyLevel.hierarchy_id == hierarchy_id,
                        HierarchyLevel.ordinal >= target,
                        HierarchyLevel.ordinal < old,
                    )
                    .values(ordinal=HierarchyLevel.ordinal + 1)
                )
            level.ordinal = target

        if "attributes" in updates:
            await db.execute(
                delete(HierarchyLevelAttribute).where(HierarchyLevelAttribute.level_id == level.id)
            )
            attrs = body.attributes or []
            for attr in attrs:
                attr_source = _normalize_source(attr.attribute_source, field="attribute_source")
                if attr.role not in ALLOWED_ATTRIBUTE_ROLES:
                    raise _validation_error(
                        f"Unsupported level attribute role '{attr.role}'",
                        field="role",
                        code="H3",
                    )
                await _resolve_attribute(
                    db,
                    model_id=model_id,
                    attribute_id=attr.attribute_id,
                    source=attr_source,
                    require_dimension_table=False,
                )
                db.add(
                    HierarchyLevelAttribute(
                        level_id=level.id,
                        attribute_id=attr.attribute_id,
                        attribute_source=attr_source,
                        role=attr.role,
                    )
                )

        try:
            await _reindex_levels(db, hierarchy_id)
            await _validate_level_connectivity(db, model_id, hierarchy_id)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise _validation_error(
                f"Level '{level.name}' or key attribute already exists in this hierarchy.",
                field="name",
                code="H6",
            )
        await db.refresh(level)
        return await _level_response(db, level, model_id)


@router.delete(
    "/hierarchies/{hierarchy_id}/levels/{level_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_hierarchy_level(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    level_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        await _load_hierarchy_or_404(db, model_id, hierarchy_id)
        levels = await _levels_for_hierarchy(db, hierarchy_id)
        if len(levels) <= 2:
            raise _validation_error(
                "A hierarchy must have at least two levels (root and leaf).",
                field="levels",
                code="H2",
            )
        level = await _load_level_or_404(db, hierarchy_id=hierarchy_id, level_id=level_id)

        # Candidate UDAs of this level (its key + any UDA-keyed level attributes).
        candidate_uda_ids = await _collect_level_uda_ids(db, [level])

        # Delete the level (cascades its level attributes) and flush before the
        # reference scan so a UDA shared with another level/hierarchy survives
        # (F-016-07).
        await db.delete(level)
        await db.flush()

        await _delete_unreferenced_generated_udas(
            db, model_id=model_id, candidate_uda_ids=candidate_uda_ids
        )

        await _reindex_levels(db, hierarchy_id)
        await _validate_level_connectivity(db, model_id, hierarchy_id)
        await db.commit()


@router.post(
    "/hierarchies/generate-date",
    response_model=HierarchyGeneratedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def generate_date_hierarchy(
    project_id: UUID,
    model_id: UUID,
    body: HierarchyGenerateDateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> HierarchyGeneratedResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        source_kind = _normalize_source(body.source_attribute_source, field="source_attribute_source")
        # Generated UDA expressions are stored canonical (PostgreSQL); the source
        # dialect is applied only at execution time. Resolve in postgres so the
        # stored expression never carries source-dialect identifier quoting.
        source_sql = await _resolve_sql_attribute(
            db,
            model_id=model_id,
            attribute_id=body.source_attribute_id,
            source=source_kind,
            require_dimension_table=False,
            connector="postgresql",
        )

        if _type_family(source_sql.resolved.ref.data_type) != "datetime":
            raise _validation_error(
                (
                    "Date hierarchy source attribute must be a date/time type, "
                    f"got '{source_sql.resolved.ref.data_type}'."
                ),
                field="source_attribute_id",
                code="D1",
            )

        template = (body.template or "").strip().lower()
        components = DATE_HIERARCHY_TEMPLATES.get(template)
        if not components:
            raise _validation_error(
                (
                    "Unsupported date hierarchy template. Expected one of: "
                    + ", ".join(sorted(DATE_HIERARCHY_TEMPLATES))
                ),
                field="template",
                code="D2",
            )

        # F-016-04: validate the calendar_type a generated date hierarchy
        # carries (was stored unchecked), and normalise the legacy ``iso``
        # token to ``iso_week`` so the generated hierarchy aligns with any
        # ISO-week calendar table.
        _validate_calendar_fields(body.calendar_type, body.fiscal_year_start_month)

        base_name = _slugify_name(source_sql.resolved.ref.name)
        canonical_col_expr = source_sql.expression
        # Clamp generated UDA/dimension names to varchar(255): base_name derives
        # from a user attribute name that may itself be up to 255 chars, so
        # "<base>_<component>" can overflow UserDefinedAttribute.name /
        # Dimension.name and raise an unhandled StringDataRightTruncationError
        # (the IntegrityError guard below does not catch DataError). Same #3
        # backstop as _create_date_hierarchy_for_alias.
        generated_names = [
            _clamp_to_limit(f"{base_name}_{component}") for component, _ in components
        ]
        # F-016-01: apply the hierarchy's calendar type/fiscal start to the
        # level-key expressions so a fiscal/ISO/Thai date hierarchy buckets the
        # way its calendar counts, not Gregorian.
        gen_expressions = [
            _calendar_component_expression(
                canonical_col_expr,
                component,
                calendar_type=normalize_calendar_type(body.calendar_type),
                fiscal_year_start_month=body.fiscal_year_start_month,
            )
            for component, _ in components
        ]
        reusable = await _find_reusable_uda_names(
            db,
            model_id=model_id,
            table_id=source_sql.resolved.table.id,
            name_expr_pairs=list(zip(generated_names, gen_expressions)),
        )
        await _assert_names_available(
            db,
            model_id=model_id,
            table_id=source_sql.resolved.table.id,
            names=generated_names,
            reusable_uda_names=reusable,
        )

        hierarchy = HierarchyDefinition(
            model_id=model_id,
            name=body.name,
            type="date_embedded",
            dimension_kind="time",
            description=body.description,
            calendar_type=normalize_calendar_type(body.calendar_type),
            fiscal_year_start_month=body.fiscal_year_start_month,
            date_config={
                "template": template,
                "source_attribute_id": str(body.source_attribute_id),
                "source_attribute_source": source_kind,
            },
        )
        db.add(hierarchy)
        generated_attrs: list[HierarchyAttributeRef] = []
        try:
            await db.flush()
            for ordinal, (component, level_name) in enumerate(components):
                gen_name = generated_names[ordinal]
                output_type = "date" if component == "day" else "integer"
                uda = await _create_generated_uda(
                    db,
                    model_id=model_id,
                    table_id=source_sql.resolved.table.id,
                    name=gen_name,
                    expression=gen_expressions[ordinal],
                    output_data_type=output_type,
                    referenced_column_ids=source_sql.referenced_column_ids,
                    description=f"Auto-generated for hierarchy '{body.name}' ({component})",
                    reuse_existing=bool(reusable),
                )
                key_attribute_id = uda.id
                key_attribute_source = "user_defined_attribute"
                generated_attrs.append(await _generated_ref(db, model_id=model_id, uda=uda))

                db.add(
                    HierarchyLevel(
                        hierarchy_id=hierarchy.id,
                        name=level_name,
                        ordinal=ordinal,
                        key_attribute_id=key_attribute_id,
                        key_attribute_source=key_attribute_source,
                        description=None,
                        time_unit=_DATE_COMPONENT_TO_TIME_UNIT.get(component),
                        allowed_time_calcs=list(_DEFAULT_TIME_CALCS),
                    )
                )

                time_grain = _DATE_COMPONENT_TO_TIME_UNIT.get(component)
                existing_dim = (
                    await db.execute(
                        select(Dimension).where(
                            Dimension.model_id == model_id,
                            Dimension.name == gen_name,
                        )
                    )
                ).scalar_one_or_none()
                if existing_dim is None:
                    db.add(
                        Dimension(
                            model_id=model_id,
                            name=gen_name,
                            display_name=_clamp_to_limit(f"{level_name} ({body.name})"),
                            user_defined_attribute_id=uda.id,
                            is_time_dim=True,
                            time_grain=time_grain,
                            description=f"Auto-generated for hierarchy '{body.name}' ({component})",
                        )
                    )
            await db.flush()
            await _validate_level_connectivity(db, model_id, hierarchy.id)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise _validation_error(
                (
                    f"Failed to generate date hierarchy '{body.name}'. "
                    "Name collisions or duplicate generated attributes were detected."
                ),
                field="name",
                code="H1",
            )

        await db.refresh(hierarchy)
        detail = await _hierarchy_detail(db, hierarchy)
        return HierarchyGeneratedResponse(
            hierarchy=detail,
            generated_attributes=generated_attrs,
        )


@router.post(
    "/hierarchies/generate-segment",
    response_model=HierarchyGeneratedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def generate_segment_hierarchy(
    project_id: UUID,
    model_id: UUID,
    body: HierarchyGenerateSegmentRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> HierarchyGeneratedResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        source_kind = _normalize_source(body.source_attribute_source, field="source_attribute_source")
        # Stored canonical (PostgreSQL); source dialect applied at execution time.
        source_sql = await _resolve_sql_attribute(
            db,
            model_id=model_id,
            attribute_id=body.source_attribute_id,
            source=source_kind,
            require_dimension_table=False,
            connector="postgresql",
        )

        mode = (body.mode or "").strip().lower()
        if mode not in {"delimiter", "positional"}:
            raise _validation_error(
                "mode must be one of: delimiter, positional",
                field="mode",
                code="S1",
            )

        level_names: list[str] = []
        expressions: list[str] = []
        segment_config: dict[str, object]
        canonical_col_expr = source_sql.expression
        source_text_expr = f"CAST({canonical_col_expr} AS TEXT)"

        if mode == "delimiter":
            delimiter = (body.delimiter or "")
            if len(delimiter) != 1:
                raise _validation_error(
                    "Delimiter must be a single character.",
                    field="delimiter",
                    code="S3",
                )
            if len(body.levels) < 2:
                raise _validation_error(
                    "Segment hierarchy must include at least two levels.",
                    field="levels",
                    code="H2",
                )
            level_names = [item.name for item in body.levels]
            escaped_delim = delimiter.replace("'", "''")
            expressions = [
                f"SPLIT_PART({source_text_expr}, '{escaped_delim}', {idx + 1})"
                for idx in range(len(level_names))
            ]
            segment_config = {
                "mode": "delimiter",
                "delimiter": delimiter,
                "levels": level_names,
                "source_attribute_id": str(body.source_attribute_id),
                "source_attribute_source": source_kind,
            }
        else:
            if len(body.segments) < 2:
                raise _validation_error(
                    "Positional mode requires at least two segments.",
                    field="segments",
                    code="H2",
                )
            sorted_segments = sorted(body.segments, key=lambda item: item.start)
            previous_end = 0
            for seg in sorted_segments:
                seg_start = seg.start
                seg_end = seg.start + seg.length - 1
                if seg_start <= previous_end:
                    raise _validation_error(
                        "Positional segments must not overlap.",
                        field="segments",
                        code="S2",
                    )
                previous_end = seg_end
            level_names = [seg.name for seg in body.segments]
            expressions = [
                f"SUBSTRING({source_text_expr}, {seg.start}, {seg.length})"
                for seg in body.segments
            ]
            segment_config = {
                "mode": "positional",
                "segments": [seg.model_dump() for seg in body.segments],
                "source_attribute_id": str(body.source_attribute_id),
                "source_attribute_source": source_kind,
            }

        base_name = _slugify_name(source_sql.resolved.ref.name)
        # Clamp generated UDA/dimension names to varchar(255) (same #3 backstop
        # as the date-generate path): base_name derives from a user attribute
        # name of up to 255 chars, so "<base>_seg_<n>" can overflow
        # UserDefinedAttribute.name / Dimension.name.
        generated_names = [
            _clamp_to_limit(f"{base_name}_seg_{idx + 1}") for idx in range(len(level_names))
        ]
        reusable = await _find_reusable_uda_names(
            db,
            model_id=model_id,
            table_id=source_sql.resolved.table.id,
            name_expr_pairs=list(zip(generated_names, expressions)),
        )
        await _assert_names_available(
            db,
            model_id=model_id,
            table_id=source_sql.resolved.table.id,
            names=generated_names,
            reusable_uda_names=reusable,
        )

        hierarchy = HierarchyDefinition(
            model_id=model_id,
            name=body.name,
            type="segment",
            description=body.description,
            segment_config=segment_config,
        )
        db.add(hierarchy)

        generated_attrs: list[HierarchyAttributeRef] = []
        try:
            await db.flush()
            for ordinal, level_name in enumerate(level_names):
                uda = await _create_generated_uda(
                    db,
                    model_id=model_id,
                    table_id=source_sql.resolved.table.id,
                    name=generated_names[ordinal],
                    expression=expressions[ordinal],
                    output_data_type="varchar",
                    referenced_column_ids=source_sql.referenced_column_ids,
                    description=f"Auto-generated for hierarchy '{body.name}' segment level {ordinal + 1}",
                    reuse_existing=bool(reusable),
                )
                generated_attrs.append(await _generated_ref(db, model_id=model_id, uda=uda))
                db.add(
                    HierarchyLevel(
                        hierarchy_id=hierarchy.id,
                        name=level_name,
                        ordinal=ordinal,
                        key_attribute_id=uda.id,
                        key_attribute_source="user_defined_attribute",
                        description=None,
                    )
                )
            await db.flush()
            await _validate_level_connectivity(db, model_id, hierarchy.id)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise _validation_error(
                (
                    f"Failed to generate segment hierarchy '{body.name}'. "
                    "Name collisions or duplicate generated attributes were detected."
                ),
                field="name",
                code="H1",
            )

        await db.refresh(hierarchy)
        detail = await _hierarchy_detail(db, hierarchy)
        return HierarchyGeneratedResponse(
            hierarchy=detail,
            generated_attributes=generated_attrs,
        )


async def _resolve_level_caption_expr(
    db, *, model_id: UUID, level: HierarchyLevel, table_alias: str = "t",
) -> str | None:
    """Resolve the SQL expression for a hierarchy level's display caption.

    Bug-3617 (Phase 0.5a): a level may carry a display-role
    ``HierarchyLevelAttribute`` distinct from its key attribute. When present
    it is selected as the member caption so ``MEMBER_CAPTION`` can differ from
    ``MEMBER_KEY`` on the XMLA wire. Returns ``None`` when the level has no
    display attribute (caption falls back to the key) or it cannot be resolved
    — resolved canonical (PostgreSQL); the caller transpiles once.
    """
    row = (
        await db.execute(
            select(HierarchyLevelAttribute)
            .where(
                HierarchyLevelAttribute.level_id == level.id,
                HierarchyLevelAttribute.role == "display",
            )
            .order_by(HierarchyLevelAttribute.id)
        )
    ).scalars().first()
    if row is None:
        return None
    try:
        resolved = await _resolve_sql_attribute(
            db,
            model_id=model_id,
            attribute_id=row.attribute_id,
            source=row.attribute_source,
            require_dimension_table=False,
            connector="postgresql",
            table_alias=table_alias,
        )
    except HTTPException:
        return None
    return resolved.expression


@router.get("/hierarchies/{hierarchy_id}/preview", response_model=HierarchyPreviewResponse)
async def preview_hierarchy(
    request: Request,
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    sample_size: int = Query(default=100, ge=10, le=settings.MEMBER_DISCOVERY_LIMIT),
    expand_level: int | None = Query(default=None, ge=0),
    parent_key: str | None = Query(default=None),
    persona_id: UUID | None = Query(default=None),
    include_key_path: bool = Query(default=False),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> HierarchyPreviewResponse:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    bearer = _extract_bearer(request)

    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)

        # ---- Bug-5424: persona-scoped hierarchy preview ----
        # Resolve the effective persona (mirrors list/get endpoints).
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        hierarchy = await _load_hierarchy_or_404(db, model_id, hierarchy_id)

        # Check hierarchy visibility under this persona.
        if persona:
            allowed_hier = parse_allowed_ids(persona.included_hierarchy_ids)
            if allowed_hier is not None and hierarchy.id not in allowed_hier:
                raise HTTPException(status_code=404, detail="Hierarchy not found")

        # Bug-7205: compile RLS predicate unconditionally for the requesting
        # principal. RLS rules (role_predicate, user_mapping) fire for
        # principals with or without personas. Skip ONLY when an explicit
        # bypass_row_security persona is present.
        rls_where: str | None = None
        if persona is None or not persona.bypass_row_security:
            principal = Principal.from_current_user(current_user)
            try:
                compiled = await compile_row_security(model_id, principal, db)
            except RowSecurityCompileError as exc:
                # F-007-16 / Bug-9021: same typed 422 as /execute. Do not
                # import query-router _sql_disclosure from model-service.
                _logger.warning(
                    "row-security rule failed to compile on hierarchy preview: "
                    "%s: %s",
                    type(exc).__name__,
                    exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "message": (
                            "A row-level security rule on this model is "
                            "misconfigured and could not be compiled. The "
                            "query was blocked (fail closed); ask a modeler "
                            "to fix the rule's predicate."
                        ),
                        "error_type": "row_security_misconfigured",
                    },
                )
            if compiled is not None:
                rls_where = compiled.sql_expression

        # Build excluded level attribute set for this persona.
        excluded_attrs = (
            await get_excluded_level_attribute_ids(db, model_id=model_id, persona=persona)
            if persona else None
        )

        levels = await _levels_for_hierarchy(db, hierarchy_id)
        # Filter out levels whose key attribute is excluded by the persona.
        if excluded_attrs is not None:
            levels = [lv for lv in levels if lv.key_attribute_id not in excluded_attrs]
        if len(levels) < 2:
            raise _validation_error(
                "A hierarchy must have at least two levels (root and leaf).",
                field="hierarchy_id",
                code="H2",
            )

        target_level = expand_level if expand_level is not None else 0
        if target_level < 0 or target_level >= len(levels):
            raise _validation_error(
                f"expand_level must be between 0 and {len(levels) - 1}.",
                field="expand_level",
                code="H5",
            )
        if target_level > 0 and parent_key is not None and parent_key.strip() == "":
            parent_key = None

        warnings: list[HierarchyPreviewWarning] = []
        conn_obj, connector, preview_err = await _resolve_preview_connection(
            db, model_id=model_id,
        )
        level_sql: list[_ResolvedSqlAttribute | None] = []
        level_summaries: list[HierarchyPreviewLevelSummary] = []

        for lvl in levels:
            resolved_sql: _ResolvedSqlAttribute | None = None
            try:
                # Resolve key expressions canonical (PostgreSQL); the preview SQL
                # builders translate the full statement to the source dialect once
                # via transpile_preview_sql. Mixing dialects here would embed
                # source-dialect quoting into a postgres-canonical string.
                resolved_sql = await _resolve_sql_attribute(
                    db,
                    model_id=model_id,
                    attribute_id=lvl.key_attribute_id,
                    source=lvl.key_attribute_source,
                    require_dimension_table=False,
                    connector="postgresql",
                    table_alias="t",
                )
            except HTTPException as exc:
                if exc.status_code == status.HTTP_404_NOT_FOUND:
                    warnings.append(
                        HierarchyPreviewWarning(
                            level_name=lvl.name,
                            type="invalid_level_attribute",
                            message=(
                                "This hierarchy references a missing attribute. "
                                "Update the level key attribute and retry preview."
                            ),
                        )
                    )
                else:
                    raise
            level_sql.append(resolved_sql)
            estimate = (
                resolved_sql.resolved.table.row_count_estimate
                if resolved_sql is not None
                else None
            )
            if estimate is not None and estimate > 50000:
                warnings.append(
                    HierarchyPreviewWarning(
                        level_name=lvl.name,
                        type="high_cardinality",
                        message=f"Level '{lvl.name}' may be high-cardinality (estimated rows: {estimate}).",
                    )
                )
            level_summaries.append(
                HierarchyPreviewLevelSummary(
                    ordinal=lvl.ordinal,
                    name=lvl.name,
                    estimated_members=int(estimate) if estimate is not None else None,
                )
            )

        if level_sql[target_level] is None:
            return HierarchyPreviewResponse(
                hierarchy_id=hierarchy.id,
                hierarchy_name=hierarchy.name,
                sample_size=sample_size,
                warnings=warnings,
                levels_summary=level_summaries,
                members=[],
            )

        # Bug-3617 (Phase 0.5a/b): each entry is (key, caption, key_path);
        # caption defaults to the key when the level has no display attribute,
        # key_path is the ancestor-first tuple when include_key_path resolved one
        # (parent-less single-table enumeration), else None.
        sampled_members: list[tuple[str, str, list[str] | None]] = []
        if preview_err:
            warnings.append(
                HierarchyPreviewWarning(
                    level_name=levels[target_level].name,
                    type="preview_unavailable",
                    message=preview_err,
                )
            )
        elif conn_obj is not None:
            try:
                batch_queries: list[tuple[str, str]] = []
                # Bug-3617 (Phase 0.5b): set when the "sample" query returns
                # ancestor key tuples (key_0..key_n) rather than a flat key.
                ancestor_path_mode = False

                for idx, lvl in enumerate(levels):
                    if level_sql[idx] is None:
                        continue
                    sql = _build_estimate_sql(
                        connector,
                        table_name=level_sql[idx].resolved.table.physical_name,
                        key_expr=level_sql[idx].expression,
                        sample_size=sample_size,
                        rls_where=rls_where,
                    )
                    batch_queries.append((f"est_{idx}", sql))

                target_sql_attr = level_sql[target_level]
                parent_expr = None
                # F-016-24: when parent and child live in different tables we
                # join the two on the model's defined relationship instead of
                # bailing. cross_table holds the resolved join + parent-side
                # expression (aliased to ``p``) when that path is available.
                cross_table: dict | None = None
                can_sample = target_sql_attr is not None
                if target_level > 0 and parent_key is not None:
                    parent_sql = level_sql[target_level - 1]
                    if parent_sql is None:
                        can_sample = False
                        warnings.append(
                            HierarchyPreviewWarning(
                                level_name=levels[target_level].name,
                                type="invalid_level_attribute",
                                message=(
                                    "Parent level attribute is missing. "
                                    "Update hierarchy level keys and retry preview."
                                ),
                            )
                        )
                    elif target_sql_attr is not None and parent_sql.resolved.table.id != target_sql_attr.resolved.table.id:
                        # Cross-table: resolve a one-hop join between the child
                        # and parent tables. If found, build a joined sample;
                        # otherwise fall back to the honest unsupported warning.
                        join_cols = await _resolve_join_between(
                            db,
                            model_id=model_id,
                            table_a_id=target_sql_attr.resolved.table.id,
                            table_b_id=parent_sql.resolved.table.id,
                        )
                        parent_sql_p = None
                        if join_cols is not None:
                            try:
                                parent_sql_p = await _resolve_sql_attribute(
                                    db,
                                    model_id=model_id,
                                    attribute_id=levels[target_level - 1].key_attribute_id,
                                    source=levels[target_level - 1].key_attribute_source,
                                    require_dimension_table=False,
                                    connector="postgresql",
                                    table_alias="p",
                                )
                            except HTTPException:
                                parent_sql_p = None
                        if join_cols is not None and parent_sql_p is not None:
                            child_jc, parent_jc = join_cols
                            cross_table = {
                                "child_join_col": child_jc,
                                "parent_join_col": parent_jc,
                                "parent_table": parent_sql.resolved.table.physical_name,
                                "parent_expr": parent_sql_p.expression,
                            }
                        else:
                            can_sample = False
                            warnings.append(
                                HierarchyPreviewWarning(
                                    level_name=levels[target_level].name,
                                    type="cross_table_preview_not_supported",
                                    message=(
                                        "Preview expansion across this level needs a "
                                        "direct join between the parent and child "
                                        "tables; none is defined in the model."
                                    ),
                                )
                            )
                    else:
                        parent_expr = parent_sql.expression

                if can_sample and target_sql_attr is not None:
                    # Bug-3617 (Phase 0.5a): resolve the target level's display
                    # caption (alias ``t`` — same table as the key) so the
                    # sample carries caption alongside key. None → caption=key.
                    caption_expr = await _resolve_level_caption_expr(
                        db, model_id=model_id, level=levels[target_level],
                    )
                    # Bug-3617 (Phase 0.5b): for a parent-less whole-level
                    # enumeration of a SINGLE-table hierarchy, sample the full
                    # ancestor key tuple per member so the caller can build the
                    # canonical composite member unique name. Cross-table or
                    # parent-filtered drills keep the flat per-level sample (the
                    # drill path reconstructs the path from the parent key).
                    ancestor_path_mode = (
                        include_key_path
                        and target_level > 0
                        and parent_key is None
                        and cross_table is None
                        and all(level_sql[i] is not None for i in range(target_level + 1))
                        and len({level_sql[i].resolved.table.id for i in range(target_level + 1)}) == 1
                    )
                    if ancestor_path_mode:
                        sample_sql = _build_ancestor_path_sample_sql(
                            connector,
                            table_name=target_sql_attr.resolved.table.physical_name,
                            key_exprs=[level_sql[i].expression for i in range(target_level + 1)],
                            sample_size=sample_size,
                            rls_where=rls_where,
                            caption_expr=caption_expr,
                        )
                    elif cross_table is not None:
                        sample_sql = _build_cross_table_sample_sql(
                            connector,
                            child_table=target_sql_attr.resolved.table.physical_name,
                            child_key_expr=target_sql_attr.expression,
                            child_join_col=cross_table["child_join_col"],
                            parent_table=cross_table["parent_table"],
                            parent_key_expr=cross_table["parent_expr"],
                            parent_join_col=cross_table["parent_join_col"],
                            parent_key=parent_key,
                            sample_size=sample_size,
                            rls_where=rls_where,
                            caption_expr=caption_expr,
                        )
                    else:
                        sample_sql = _build_sample_sql(
                            connector,
                            table_name=target_sql_attr.resolved.table.physical_name,
                            key_expr=target_sql_attr.expression,
                            sample_size=sample_size,
                            parent_expr=parent_expr,
                            parent_key=parent_key,
                            rls_where=rls_where,
                            caption_expr=caption_expr,
                        )
                    batch_queries.append(("sample", sample_sql))

                results = await _introspect_batch_via_router(
                    str(model_id), batch_queries, bearer,
                )

                for idx in range(len(levels)):
                    key = f"est_{idx}"
                    if key not in results:
                        continue
                    rows, _cols, err = results[key]
                    if err or not rows:
                        continue
                    val = rows[0].get("c")
                    if val is not None:
                        level_summaries[idx].estimated_members = int(val)

                if "sample" in results:
                    rows, _cols, err = results["sample"]
                    if not err and ancestor_path_mode:
                        # Each row is (key_0..key_target, caption_value).
                        for row in rows:
                            path: list[str] = []
                            for i in range(target_level + 1):
                                kv = row.get(f"key_{i}")
                                if kv is None:
                                    break
                                path.append(str(kv))
                            if len(path) != target_level + 1:
                                continue
                            key = path[-1]
                            cap = row.get("caption_value")
                            caption = str(cap) if cap is not None else key
                            sampled_members.append((key, caption, path))
                    elif not err:
                        for row in rows:
                            val = row.get("key_value")
                            if val is not None:
                                cap = row.get("caption_value")
                                caption = str(cap) if cap is not None else str(val)
                                sampled_members.append((str(val), caption, None))

            except Exception as exc:
                warnings.append(
                    HierarchyPreviewWarning(
                        level_name=levels[target_level].name,
                        type="preview_query_failed",
                        message=f"Failed to sample hierarchy members: {exc}",
                    )
                )

        members = [
            HierarchyPreviewMember(
                level_ordinal=levels[target_level].ordinal,
                level_name=levels[target_level].name,
                key_value=value,
                caption=caption,
                key_path=key_path,
                attributes={},
                parent_key=parent_key,
                children_loaded=False,
                child_count_estimate=None,
            )
            for value, caption, key_path in sampled_members
        ]

        return HierarchyPreviewResponse(
            hierarchy_id=hierarchy.id,
            hierarchy_name=hierarchy.name,
            sample_size=sample_size,
            warnings=warnings,
            levels_summary=level_summaries,
            members=members,
        )


# ---------------------------------------------------------------------------
# Batch date hierarchy helpers
# ---------------------------------------------------------------------------

class _UnassignedDateAttr:
    """Lightweight carrier for both physical columns and UDAs."""

    __slots__ = ("id", "column_name", "display_name", "data_type",
                 "table_id", "table_alias", "is_uda", "physical_column_id")

    def __init__(
        self, *, id, column_name, display_name, data_type,
        table_id, table_alias, is_uda=False, physical_column_id=None,
    ):
        self.id = id
        self.column_name = column_name
        self.display_name = display_name
        self.data_type = data_type
        self.table_id = table_id
        self.table_alias = table_alias
        self.is_uda = is_uda
        self.physical_column_id = physical_column_id


async def _get_unassigned_date_cols(
    db,
    model_id: UUID,
) -> list[_UnassignedDateAttr]:
    # A fact column already joined to ANY calendar instance (marked alias,
    # spine, or unmarked alias — see _calendar_instance_clause) is assigned.
    # The previous marker-only test (calendar_table_id IS NOT NULL) let a fact
    # date column joined to an UNMARKED alias (batch-date against an unbound
    # calendar) resurface as unassigned; the name-existence check hid this
    # until the generated hierarchy was renamed (Bug-6683 external review).
    cal_join_subq = (
        select(Join.left_column_id)
        .join(ModelTable, ModelTable.id == Join.right_table_id)
        .where(
            Join.model_id == model_id,
            _calendar_instance_clause(model_id),
        )
        .scalar_subquery()
    )

    # Physical columns with date/time types.
    # Exclude columns that live on any calendar instance (the three shapes in
    # _calendar_instance_clause). A calendar's own date columns are the date
    # spine itself, never a fact date column that needs its own auto-calendar;
    # re-consuming them is what compounded names past varchar(255) (Bug-6683).
    # Excluding them keeps repeated auto-creates idempotent and bounded.
    phys_result = await db.execute(
        select(ModelColumn, ModelTable)
        .join(ModelTable, ModelTable.id == ModelColumn.model_table_id)
        .where(
            ModelTable.model_id == model_id,
            ~_calendar_instance_clause(model_id),
            or_(
                ModelColumn.data_type.ilike("%date%"),
                ModelColumn.data_type.ilike("%time%"),
                ModelColumn.data_type.ilike("%timestamp%"),
            ),
            ModelColumn.id.not_in(cal_join_subq),
        )
    )
    rows: list[_UnassignedDateAttr] = []
    for col, tbl in phys_result.fetchall():
        rows.append(_UnassignedDateAttr(
            id=col.id, column_name=col.column_name,
            display_name=col.display_name, data_type=col.data_type,
            table_id=tbl.id, table_alias=tbl.alias,
        ))

    # UDAs with date/time output types.
    #
    # Bug-6683 root fix: NEVER treat a generated hierarchy-component UDA
    # (``is_generated`` = True) as an unassigned date column. The date-hierarchy
    # generator (``generate-date`` / ``_create_date_hierarchy_for_alias``) emits
    # a date-typed ``*_day`` component UDA whose description is
    # "Auto-generated for hierarchy '<name>' (day)". ``generate-date`` places it
    # on the *fact* table (calendar_table_id IS NULL), so the calendar-alias
    # exclusion below does not catch it. Consuming it made calendar auto-create
    # wrap that description into a new "<...> Calendar" hierarchy, whose own
    # generated day UDA was wrapped again on the next auto-create, compounding
    # names ("Auto generated for hierarchy Auto generated for hierarchy ...")
    # until the varchar(255) alias/display_name/name columns overflowed. A
    # generated component UDA is an internal artifact of an EXISTING date
    # hierarchy and must never seed its own auto-calendar, so excluding
    # ``is_generated`` makes repeated auto-creates fully idempotent and
    # bounded.
    #
    # The calendar-instance exclusion is retained as defence in depth: it also
    # excludes any manually-authored (non-generated) date UDA that a user
    # attaches to a calendar instance table.
    uda_result = await db.execute(
        select(UserDefinedAttribute, ModelTable)
        .join(ModelTable, ModelTable.id == UserDefinedAttribute.table_id)
        .where(
            UserDefinedAttribute.model_id == model_id,
            UserDefinedAttribute.is_generated.is_(False),
            ~_calendar_instance_clause(model_id),
            or_(
                UserDefinedAttribute.output_data_type.ilike("%date%"),
                UserDefinedAttribute.output_data_type.ilike("%time%"),
                UserDefinedAttribute.output_data_type.ilike("%timestamp%"),
            ),
        )
    )
    existing_ids = {r.id for r in rows}
    for uda, tbl in uda_result.fetchall():
        if uda.id in existing_ids:
            continue
        ref_result = await db.execute(
            select(UserDefinedAttributeColumnRef.column_id)
            .where(UserDefinedAttributeColumnRef.attribute_id == uda.id)
        )
        ref_col_ids = [r[0] for r in ref_result.fetchall()]
        rows.append(_UnassignedDateAttr(
            id=uda.id, column_name=uda.name,
            display_name=uda.description or uda.name, data_type=uda.output_data_type,
            table_id=tbl.id, table_alias=tbl.alias,
            is_uda=True,
            physical_column_id=ref_col_ids[0] if len(ref_col_ids) == 1 else None,
        ))

    return rows


async def _auto_create_date_hierarchies_for_model(
    db,
    *,
    model_id: UUID,
    calendar_model_table_id: UUID,
    calendar_table_id: UUID | None = None,
    grain: str = "y_m_d",
    history_capture: dict | None = None,
) -> tuple[int, list[str], list[str]]:
    """Auto-create date hierarchies for all unassigned fact-table date columns.

    Called after a calendar is created or bound. Returns
    (created_count, skipped_reasons, created_hierarchy_names).
    Raises ValueError if calendar_model_table_id does not reference a valid calendar alias.

    Bug-6722: for expression-capable calendar types (standard, fiscal,
    iso_week, thai_buddhist), the hierarchy UDAs are placed directly on the
    fact table, referencing the fact's own date column.  No calendar-alias
    join is created because the period boundaries (year, quarter, month, day)
    are computed via EXTRACT / CASE date arithmetic -- the physical calendar
    table is not needed at query time.  This eliminates the class of
    "calendar table column name mismatch" errors that occur when the
    CalendarTable metadata (date_column) does not match the physical source
    table's actual column names (e.g. BigQuery dim_date with ``full_date``
    vs the Tessallite-standard ``date_key``).

    Table-bound calendar types (retail_445, hijri) still create a
    calendar-alias join because their period boundaries require data from
    the materialised calendar table columns.
    """
    if grain not in DATE_HIERARCHY_TEMPLATES:
        raise ValueError(
            f"Unknown date hierarchy grain '{grain}'. "
            f"Supported: {', '.join(sorted(DATE_HIERARCHY_TEMPLATES))}"
        )
    cal_mt = await db.get(ModelTable, calendar_model_table_id)
    if cal_mt is None or cal_mt.calendar_table_id is None:
        raise ValueError("calendar_model_table_id must reference a calendar ModelTable")

    cal_info = await db.get(CalendarTable, cal_mt.calendar_table_id)
    if cal_info is None or not cal_info.date_column:
        raise ValueError("Calendar table has no date_column configured")

    # Bug-6722: determine whether this calendar type can compute its period
    # boundaries from date arithmetic alone (no physical calendar table join).
    # Fiscal calendars additionally need the materialised ``year_label`` for
    # BI captions, so their generated hierarchy uses the table-bound path when
    # a calendar alias is available.  The query-time expression path remains
    # available to the variant emitter and explicit expression callers.
    cal_type = normalize_calendar_type(cal_info.calendar_type) or "standard"
    expr_capable = (
        cal_type in EXPRESSION_CAPABLE_CALENDAR_TYPES
        and cal_type != "fiscal"
    )

    # For table-bound types we still need the calendar date-key column.
    cal_date_key_col = None
    if not expr_capable:
        date_key_result = await db.execute(
            select(ModelColumn)
            .where(
                ModelColumn.model_table_id == cal_mt.id,
                ModelColumn.column_name == cal_info.date_column,
            )
            .limit(1)
        )
        cal_date_key_col = date_key_result.scalar_one_or_none()
        if cal_date_key_col is None:
            return 0, [
                f"Calendar date key column '{cal_info.date_column}' not found in calendar alias"
            ], []

    unassigned = await _get_unassigned_date_cols(db, model_id)
    created = 0
    skipped: list[str] = []
    created_names: list[str] = []
    used_aliases: set[str] = set()

    for attr in unassigned:
        if attr.table_id == cal_mt.id:
            skipped.append(f"{attr.column_name}: belongs to calendar table")
            continue

        join_col_id = attr.id
        if attr.is_uda:
            if attr.physical_column_id is None:
                skipped.append(f"{attr.column_name}: computed attribute references multiple or no physical columns")
                continue
            join_col_id = attr.physical_column_id

        # Derive the hierarchy/alias label from the base date column's stable
        # user label (display_name, else column_name), clamped to varchar(255).
        # Combined with the calendar-alias exclusion in
        # _get_unassigned_date_cols, this keeps repeated auto-creates
        # idempotent: a re-run resolves the same label, finds the existing
        # hierarchy below, and skips instead of wrapping the name.
        hier_name = _calendar_hier_label(attr.display_name, attr.column_name)
        existing = (
            await db.execute(
                select(HierarchyDefinition.id)
                .where(
                    HierarchyDefinition.model_id == model_id,
                    HierarchyDefinition.name == hier_name,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            skipped.append(f"{attr.column_name}: hierarchy already exists")
            continue

        if expr_capable:
            # Bug-6722: expression-capable path -- UDAs on the fact table
            # directly, referencing the fact date column.  No alias, no join.
            fact_col_name = attr.column_name
            fact_col_id = attr.id
            if attr.is_uda and attr.physical_column_id is not None:
                phys = await db.get(ModelColumn, attr.physical_column_id)
                if phys is not None:
                    fact_col_name = phys.column_name
                    fact_col_id = phys.id

            # Bug-7203: propagate calendar_type and fiscal_year_start_month
            await _create_date_hierarchy_for_alias(
                db,
                model_id=model_id,
                table_id=attr.table_id,
                date_key_col_id=fact_col_id,
                date_key_col_name=fact_col_name,
                grain=grain,
                name=hier_name,
                calendar_type=cal_type,
                fiscal_year_start_month=cal_info.fiscal_year_start_month,
                calendar_table_id=calendar_table_id,
                history_capture=history_capture,
            )
        else:
            # Bug-7204: Table-bound path (retail_445, hijri) -- calendar alias
            # + join. Copy ALL period columns (not just date_key) so the
            # hierarchy can reference them.
            alias_alias = await _next_calendar_alias(db, model_id, attr.column_name, used_aliases)
            used_aliases.add(alias_alias)
            alias = ModelTable(
                model_id=model_id,
                source_id=cal_mt.source_id,
                table_type="dim_detail",
                physical_name=cal_mt.physical_name,
                alias=alias_alias,
                display_name=hier_name,
                calendar_table_id=cal_mt.calendar_table_id,
            )
            db.add(alias)
            await db.flush()
            if history_capture is not None:
                history_capture.setdefault("generated_model_table_ids", []).append(alias.id)

            # Create date_key column on the alias
            alias_date_key = ModelColumn(
                model_table_id=alias.id,
                column_name=cal_date_key_col.column_name,
                display_name=cal_date_key_col.display_name or cal_date_key_col.column_name,
                data_type=cal_date_key_col.data_type,
                is_nullable=cal_date_key_col.is_nullable,
                is_hidden=False,
            )
            db.add(alias_date_key)
            await db.flush()

            # Bug-7204: copy all period columns from the calendar to the alias
            # and build a column_id_map for the hierarchy level builder.
            # Use the CalendarTable's configured column names (year_column,
            # quarter_column, etc.) which reflect the actual physical names
            # rather than the hardcoded defaults -- this handles bound
            # calendars whose columns may have custom names.
            column_id_map: dict[str, UUID] = {}
            cal_columns = CALENDAR_COLUMN_SETS.get(cal_type, {})
            for slot, default_col_name in cal_columns.items():
                if slot == "date_column":
                    continue  # already created above
                # Use the configured column name from CalendarTable if
                # available, falling back to the default for the type.
                phys_col_name = getattr(cal_info, slot, None) or default_col_name
                # Find the source column on the spine calendar alias
                src_col_result = await db.execute(
                    select(ModelColumn)
                    .where(
                        ModelColumn.model_table_id == cal_mt.id,
                        ModelColumn.column_name == phys_col_name,
                    )
                    .limit(1)
                )
                src_col = src_col_result.scalar_one_or_none()
                if src_col is None:
                    continue
                # Create a copy on the companion alias
                alias_col = ModelColumn(
                    model_table_id=alias.id,
                    column_name=src_col.column_name,
                    display_name=src_col.display_name or src_col.column_name,
                    data_type=src_col.data_type,
                    is_nullable=src_col.is_nullable,
                    is_hidden=False,
                )
                db.add(alias_col)
                await db.flush()
                # Map by DEFAULT column name (used as component in template)
                # so the hierarchy template can find it
                column_id_map[default_col_name] = alias_col.id

            # Bug-7203/7204: pass calendar metadata and column map
            await _create_date_hierarchy_for_alias(
                db,
                model_id=model_id,
                table_id=alias.id,
                date_key_col_id=alias_date_key.id,
                date_key_col_name=alias_date_key.column_name,
                grain=grain,
                name=hier_name,
                calendar_type=cal_type,
                fiscal_year_start_month=cal_info.fiscal_year_start_month,
                column_id_map=column_id_map if column_id_map else None,
                caption_source_table_id=cal_mt.id,
                calendar_table_id=calendar_table_id,
                history_capture=history_capture,
            )

            join = Join(
                model_id=model_id,
                left_table_id=attr.table_id,
                right_table_id=alias.id,
                # Orientation and cardinality are SEPARATE fields
                # (join-orientation contract, invariant 3). This edge is
                # owning-table -> calendar alias, i.e. many rows to one
                # calendar day, and the orientation that preserves the many
                # side (the owning table, which is also the anchor-ward one)
                # is a LEFT join — the same rows the legacy ``many_to_one``
                # token rendered. Parking the cardinality in ``join_type``, as
                # this did, made every calendar-alias model permanently
                # unprovable to the pocket row-population proof.
                join_type=_CALENDAR_ALIAS_JOIN_TYPE,
                cardinality=_CALENDAR_ALIAS_CARDINALITY,
                left_column_id=join_col_id,
                right_column_id=alias_date_key.id,
            )
            db.add(join)
            await db.flush()
            if history_capture is not None:
                history_capture.setdefault("generated_join_ids", []).append(join.id)

        created_names.append(attr.display_name or attr.column_name)
        created += 1

    return created, skipped, created_names


async def _create_date_hierarchy_for_alias(
    db,
    *,
    model_id: UUID,
    table_id: UUID,
    date_key_col_id: UUID,
    date_key_col_name: str,
    grain: str,
    name: str,
    calendar_type: str | None = None,
    fiscal_year_start_month: int | None = None,
    column_id_map: dict[str, UUID] | None = None,
    caption_source_table_id: UUID | None = None,
    calendar_table_id: UUID | None = None,
    history_capture: dict | None = None,
) -> HierarchyDefinition:
    """Create a date hierarchy with UDA levels on a table.

    Bug-7203: ``calendar_type`` and ``fiscal_year_start_month`` are propagated
    onto the generated HierarchyDefinition so the query-time path
    (_resolve_hierarchy_calendar_rules) and time-variant SQL use the correct
    calendar boundaries rather than defaulting to Gregorian.

    Bug-7204: for table-bound calendar types (retail_445, hijri), when
    ``column_id_map`` is provided, hierarchy levels are keyed on the physical
    calendar table columns (e.g. retail_year, retail_quarter) instead of
    EXTRACT-based UDA expressions. ``column_id_map`` maps component names
    (e.g. 'retail_year') to the ModelColumn.id on the alias.

    Parameters
    ----------
    calendar_type:
        The calendar type (standard, fiscal, iso_week, retail_445, hijri,
        thai_buddhist). When set, selects the calendar-specific hierarchy
        template from CALENDAR_HIERARCHY_TEMPLATES instead of the grain-based
        DATE_HIERARCHY_TEMPLATES.
    fiscal_year_start_month:
        Fiscal year start month (1-12). Propagated to the hierarchy for
        query-time fiscal offset computation.
    column_id_map:
        For table-bound types: maps component name to a ModelColumn.id on
        the calendar alias. When provided, levels reference these physical
        columns directly instead of creating UDA expressions.
    """
    # Bug-7203: use calendar-type-specific template when available
    if calendar_type and calendar_type in CALENDAR_HIERARCHY_TEMPLATES:
        components = CALENDAR_HIERARCHY_TEMPLATES[calendar_type]
    else:
        components = DATE_HIERARCHY_TEMPLATES[grain]

    # Stored canonical (PostgreSQL); source dialect applied at execution time.
    expr = quote_identifier("postgresql", date_key_col_name)

    hierarchy = HierarchyDefinition(
        model_id=model_id,
        name=name,
        type="date_embedded",
        dimension_kind="time",
        date_config={
            "template": grain,
            "source_attribute_id": str(date_key_col_id),
            "source_attribute_source": "physical_column",
            **({"calendar_table_id": str(calendar_table_id)} if calendar_table_id else {}),
        },
        calendar_type=calendar_type,
        fiscal_year_start_month=fiscal_year_start_month,
    )
    db.add(hierarchy)
    await db.flush()
    if history_capture is not None:
        history_capture.setdefault("generated_hierarchy_ids", []).append(hierarchy.id)

    # Bug-7204: for table-bound types with column_id_map, build levels directly
    # from physical calendar columns instead of UDA expressions.
    if column_id_map:
        caption_dimensions: list[Dimension] = []

        for ordinal, (component, level_name) in enumerate(components):
            meta = _TABLE_BOUND_COMPONENT_TO_COLUMN.get(component, {})
            physical_name = meta.get("column", component)
            col_id = column_id_map.get(component) or column_id_map.get(physical_name)
            if col_id is None:
                continue  # column not available on alias; skip this level

            time_unit = meta.get("time_unit")

            db.add(
                HierarchyLevel(
                    hierarchy_id=hierarchy.id,
                    name=level_name,
                    ordinal=ordinal,
                    key_attribute_id=col_id,
                    key_attribute_source="physical_column",
                    description=None,
                    time_unit=time_unit,
                    allowed_time_calcs=list(_DEFAULT_TIME_CALCS),
                )
            )

            # Create a dimension for this level if one does not exist.
            gen_name = _clamp_to_limit(f"{_slugify_name(name)}_{component}")
            existing_dim = (
                await db.execute(
                    select(Dimension).where(
                        Dimension.model_id == model_id,
                        Dimension.name == gen_name,
                    )
                )
            ).scalar_one_or_none()
            if existing_dim is None:
                generated_dimension = Dimension(
                    model_id=model_id,
                    name=gen_name,
                    display_name=_clamp_to_limit(f"{level_name} ({name})"),
                    source_column_id=col_id,
                    is_time_dim=True,
                    time_grain=time_unit,
                    description=f"Auto-generated for hierarchy '{name}' ({component})",
                )
                db.add(generated_dimension)
                dimension_for_caption = generated_dimension
                if history_capture is not None:
                    history_capture.setdefault("generated_dimension_objects", []).append(
                        generated_dimension
                    )
            else:
                dimension_for_caption = existing_dim
            if component in {"year", "retail_year"}:
                caption_dimensions.append(dimension_for_caption)

        # A caption is optional during the normal pre-rebuild window.  Do not
        # advertise a display column until the source calendar alias really
        # exposes it; XMLA/Power BI then naturally falls back to the key.  The
        # lookup is deliberately after level creation so an older caller's
        # key-only path remains usable even when no optional metadata row exists.
        if (
            caption_source_table_id is not None
            and calendar_type in {"fiscal", "retail_445"}
        ):
            try:
                label_result = await db.execute(
                    select(ModelColumn)
                    .where(
                        ModelColumn.model_table_id == caption_source_table_id,
                        ModelColumn.column_name == "year_label",
                    )
                    .limit(1)
                )
            except StopAsyncIteration:
                # Test doubles and legacy metadata adapters can have no answer
                # for this optional probe.  Treat that exactly like an old
                # calendar: year_no remains the key and caption.
                label_result = None
            source_year_label = (
                label_result.scalar_one_or_none() if label_result is not None else None
            )
            if source_year_label is not None:
                alias_year_label = ModelColumn(
                    model_table_id=table_id,
                    column_name=source_year_label.column_name,
                    display_name=source_year_label.display_name or "Year Label",
                    description=source_year_label.description,
                    data_type=source_year_label.data_type,
                    is_nullable=source_year_label.is_nullable,
                    is_hidden=False,
                )
                db.add(alias_year_label)
                await db.flush()
                for dimension in caption_dimensions:
                    if dimension.display_column_id is None:
                        dimension.display_column_id = alias_year_label.id

        await db.flush()
        if history_capture is not None:
            history_capture.setdefault("generated_dimension_ids", []).extend(
                dimension.id
                for dimension in history_capture.pop("generated_dimension_objects", [])
                if dimension.id is not None
            )
        return hierarchy

    # Expression-capable path: create UDA expressions for each level.
    generated_names = [
        _clamp_to_limit(f"{_slugify_name(name)}_{component}") for component, _ in components
    ]
    # F-016-01: fiscal/ISO/Thai level keys use calendar-correct period math,
    # matching the calendar table columns and the time-variant SQL.
    gen_expressions = [
        _calendar_component_expression(
            expr, component, calendar_type, fiscal_year_start_month
        )
        for component, _ in components
    ]
    reusable = await _find_reusable_uda_names(
        db, model_id=model_id, table_id=table_id,
        name_expr_pairs=list(zip(generated_names, gen_expressions)),
    )

    for ordinal, (component, level_name) in enumerate(components):
        gen_name = generated_names[ordinal]
        output_type = "date" if component == "day" else "integer"
        uda = await _create_generated_uda(
            db,
            model_id=model_id,
            table_id=table_id,
            name=gen_name,
            expression=gen_expressions[ordinal],
            output_data_type=output_type,
            referenced_column_ids=[date_key_col_id],
            description=f"Auto-generated for hierarchy '{name}' ({component})",
            reuse_existing=bool(reusable),
            history_capture=history_capture,
        )
        key_attribute_id = uda.id
        key_attribute_source = "user_defined_attribute"

        db.add(
            HierarchyLevel(
                hierarchy_id=hierarchy.id,
                name=level_name,
                ordinal=ordinal,
                key_attribute_id=key_attribute_id,
                key_attribute_source=key_attribute_source,
                description=None,
                time_unit=_DATE_COMPONENT_TO_TIME_UNIT.get(component),
                allowed_time_calcs=list(_DEFAULT_TIME_CALCS),
            )
        )

        time_grain = _DATE_COMPONENT_TO_TIME_UNIT.get(component)
        existing_dim = (
            await db.execute(
                select(Dimension).where(
                    Dimension.model_id == model_id,
                    Dimension.name == gen_name,
                )
            )
        ).scalar_one_or_none()
        if existing_dim is None:
            generated_dimension = Dimension(
                    model_id=model_id,
                    name=gen_name,
                    display_name=_clamp_to_limit(f"{level_name} ({name})"),
                    user_defined_attribute_id=uda.id,
                    is_time_dim=True,
                    time_grain=time_grain,
                    description=f"Auto-generated for hierarchy '{name}' ({component})",
                )
            db.add(generated_dimension)
            if history_capture is not None:
                history_capture.setdefault("generated_dimension_objects", []).append(
                    generated_dimension
                )

    await db.flush()
    if history_capture is not None:
        history_capture.setdefault("generated_dimension_ids", []).extend(
            dimension.id
            for dimension in history_capture.pop("generated_dimension_objects", [])
            if dimension.id is not None
        )
    return hierarchy


async def reconcile_generated_calendar_captions(
    db,
    *,
    model_id: UUID,
    calendar_table_id: UUID,
    calendar_type: str,
    history_capture: dict | None = None,
) -> int:
    """Reconcile captions on already-generated fiscal/retail hierarchies.

    Auto-generation is intentionally idempotent and therefore skips a hierarchy
    whose name already exists.  A calendar rebuild must still revisit the
    generated year dimension after the physical ``year_label`` column appears.
    This narrow pass follows the persisted ``date_config.calendar_table_id``
    marker and only changes server-generated time dimensions whose key column
    and caption column belong to the same generated calendar alias.  It never
    changes a numeric level key or a modeller-authored dimension.
    """
    if calendar_type not in {"fiscal", "retail_445"}:
        return 0
    rows = (
        await db.execute(select(HierarchyDefinition).where(HierarchyDefinition.model_id == model_id))
    ).scalars().all()
    reconciled = 0
    calendar_marker = str(calendar_table_id)
    calendar = await db.get(CalendarTable, calendar_table_id)
    if calendar is None:
        return 0
    spine = (
        await db.execute(
            select(ModelTable).where(
                ModelTable.model_id == model_id,
                ModelTable.calendar_table_id == calendar_table_id,
                ModelTable.table_type == "calendar",
            ).order_by(ModelTable.id).limit(1)
        )
    ).scalar_one_or_none()
    if spine is None:
        return 0
    for hierarchy in rows:
        config = hierarchy.date_config
        if not isinstance(config, dict) or config.get("calendar_table_id") != calendar_marker:
            continue
        if hierarchy.calendar_type != calendar_type or hierarchy.type != "date_embedded":
            continue
        levels = (
            await db.execute(
                select(HierarchyLevel).where(HierarchyLevel.hierarchy_id == hierarchy.id)
            )
        ).scalars().all()
        year_level = next((level for level in levels if level.time_unit == "year"), None)
        if year_level is None:
            continue
        key_column = await db.get(ModelColumn, year_level.key_attribute_id)
        if key_column is None and year_level.key_attribute_source == "user_defined_attribute":
            # Legacy fiscal hierarchies were expression-backed.  They are
            # server-generated (date_config marker + time hierarchy), so
            # upgrade their levels to the existing physical calendar alias
            # rather than leaving a caption pointer to a non-existent source.
            source_id = config.get("source_attribute_id")
            try:
                source_attribute_id = UUID(str(source_id))
            except (TypeError, ValueError):
                source_attribute_id = None
            source_column = (
                await db.get(ModelColumn, source_attribute_id)
                if source_attribute_id is not None else None
            )
            if source_column is None and source_attribute_id is not None:
                # Older expression-backed hierarchies may persist the source
                # UDA id rather than its single physical date column. Resolve
                # that supported representation before looking up the
                # companion join; multi-column UDAs cannot identify one date
                # role and remain on their existing expression path.
                source_uda = await db.get(UserDefinedAttribute, source_attribute_id)
                if source_uda is not None:
                    ref_ids = (
                        await db.execute(
                            select(UserDefinedAttributeColumnRef.column_id).where(
                                UserDefinedAttributeColumnRef.attribute_id == source_uda.id
                            )
                        )
                    ).scalars().all()
                    if len(ref_ids) == 1:
                        source_column = await db.get(ModelColumn, ref_ids[0])
            if source_column is None:
                continue
            aliases = (
                await db.execute(
                    select(ModelTable).where(
                        ModelTable.model_id == model_id,
                        ModelTable.calendar_table_id == calendar_table_id,
                        ModelTable.id != spine.id,
                    )
                )
            ).scalars().all()
            alias_by_id = {item.id: item for item in aliases}
            alias = None
            date_alias_column = None
            if alias_by_id:
                # The semantic identity of a companion alias is the fact-date
                # column it joins, not merely its membership in this calendar.
                # A model can have Order Date and Ship Date hierarchies over
                # the same calendar, each requiring its own alias and join.
                joins = (
                    await db.execute(
                        select(Join).where(
                            Join.model_id == model_id,
                            Join.left_column_id == source_column.id,
                            Join.right_table_id.in_(tuple(alias_by_id)),
                        )
                    )
                ).scalars().all()
                for candidate_join in joins:
                    candidate_alias = alias_by_id.get(candidate_join.right_table_id)
                    if candidate_alias is None:
                        continue
                    candidate_column = await db.get(
                        ModelColumn, candidate_join.right_column_id
                    )
                    if (
                        candidate_column is not None
                        and candidate_column.model_table_id == candidate_alias.id
                        and candidate_column.column_name == calendar.date_column
                    ):
                        alias = candidate_alias
                        date_alias_column = candidate_column
                        break

            if alias is not None and history_capture is not None:
                recorded_ids = {
                    str(item.get("id"))
                    for item in history_capture.setdefault("reused_model_tables", [])
                    if isinstance(item, dict) and item.get("id")
                }
                if str(alias.id) not in recorded_ids:
                    history_capture["reused_model_tables"].append({
                        "id": str(alias.id),
                        "calendar_table_id": str(alias.calendar_table_id)
                        if alias.calendar_table_id is not None else None,
                        "table_type": alias.table_type,
                    })
            if alias is None:
                alias_name = await _next_calendar_alias(
                    db, model_id, source_column.column_name, set()
                )
                alias = ModelTable(
                    model_id=model_id,
                    source_id=spine.source_id,
                    table_type="dim_detail",
                    physical_name=spine.physical_name,
                    alias=alias_name,
                    display_name=f"{hierarchy.name} Calendar",
                    calendar_table_id=calendar_table_id,
                )
                db.add(alias)
                await db.flush()
                if history_capture is not None:
                    history_capture.setdefault("generated_model_table_ids", []).append(alias.id)
                spine_columns = (
                    await db.execute(
                        select(ModelColumn).where(ModelColumn.model_table_id == spine.id)
                    )
                ).scalars().all()
                for source in spine_columns:
                    db.add(ModelColumn(
                        model_table_id=alias.id,
                        column_name=source.column_name,
                        display_name=source.display_name,
                        description=source.description,
                        data_type=source.data_type,
                        is_nullable=source.is_nullable,
                        is_hidden=False,
                    ))
                await db.flush()
                date_alias_column = next(
                    (item for item in (await db.execute(
                        select(ModelColumn).where(ModelColumn.model_table_id == alias.id)
                    )).scalars().all() if item.column_name == calendar.date_column),
                    None,
                )
                if date_alias_column is not None:
                    join = Join(
                        model_id=model_id,
                        left_table_id=source_column.model_table_id,
                        right_table_id=alias.id,
                        join_type=_CALENDAR_ALIAS_JOIN_TYPE,
                        cardinality=_CALENDAR_ALIAS_CARDINALITY,
                        left_column_id=source_column.id,
                        right_column_id=date_alias_column.id,
                    )
                    db.add(join)
                    await db.flush()
                    if history_capture is not None:
                        history_capture.setdefault("generated_join_ids", []).append(join.id)
            alias_columns = (
                await db.execute(
                    select(ModelColumn).where(ModelColumn.model_table_id == alias.id)
                )
            ).scalars().all()
            by_name = {item.column_name: item for item in alias_columns}
            component_columns = {
                "year": getattr(calendar, "year_column", None),
                "half": getattr(calendar, "half_column", None),
                "quarter": getattr(calendar, "quarter_column", None),
                "month": getattr(calendar, "month_column", None),
                "week": getattr(calendar, "week_column", None),
                "day": getattr(calendar, "day_column", None),
            }
            for level in levels:
                component = level.time_unit
                physical = by_name.get(component_columns.get(component))
                if physical is None:
                    continue
                if history_capture is not None:
                    history_capture.setdefault("hierarchy_level_keys", []).append({
                        "id": str(level.id), "key_attribute_id": str(level.key_attribute_id),
                        "key_attribute_source": level.key_attribute_source,
                    })
                level.key_attribute_id = physical.id
                level.key_attribute_source = "physical_column"
            key_column = by_name.get(component_columns.get("year"))
            if key_column is None:
                continue
        if key_column is None or year_level.key_attribute_source != "physical_column":
            continue
        label_result = await db.execute(
            select(ModelColumn).where(
                ModelColumn.model_table_id == key_column.model_table_id,
                ModelColumn.column_name == "year_label",
            ).limit(1)
        )
        label_column = label_result.scalar_one_or_none()
        if label_column is None:
            continue
        dimension_name = _clamp_to_limit(
            f"{_slugify_name(hierarchy.name)}_"
            f"{'retail_year' if calendar_type == 'retail_445' else 'year'}"
        )
        dimension = (
            await db.execute(
                select(Dimension).where(
                    Dimension.model_id == model_id,
                    Dimension.name == dimension_name,
                    Dimension.is_time_dim.is_(True),
                )
            )
        ).scalar_one_or_none()
        if dimension is None:
            continue
        if history_capture is not None and dimension.display_column_id != label_column.id:
            history_capture.setdefault("dimension_display_columns", []).append(
                {"id": str(dimension.id), "display_column_id": (
                    str(dimension.display_column_id)
                    if dimension.display_column_id is not None else None
                )}
            )
        if dimension.source_column_id != key_column.id:
            if history_capture is not None:
                history_capture.setdefault("dimension_source_columns", []).append({
                    "id": str(dimension.id),
                    "source_column_id": str(dimension.source_column_id)
                    if dimension.source_column_id is not None else None,
                    "user_defined_attribute_id": str(dimension.user_defined_attribute_id)
                    if dimension.user_defined_attribute_id is not None else None,
                })
            dimension.source_column_id = key_column.id
            dimension.user_defined_attribute_id = None
        if dimension.display_column_id != label_column.id:
            dimension.display_column_id = label_column.id
            reconciled += 1
    await db.flush()
    return reconciled


# ---------------------------------------------------------------------------
# Batch date hierarchy endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/columns/unassigned-dates",
    response_model=list[UnassignedDateColumn],
    dependencies=[require_role("viewer")],
)
async def list_unassigned_date_columns(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[UnassignedDateColumn]:
    enforce_model_scope(current_user, str(model_id), project_id=str(project_id))
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        rows = await _get_unassigned_date_cols(db, model_id)
        return [
            UnassignedDateColumn(
                column_id=r.id,
                column_name=r.column_name,
                display_name=r.display_name,
                data_type=r.data_type,
                table_id=r.table_id,
                table_alias=r.table_alias,
                is_uda=r.is_uda,
            )
            for r in rows
        ]


@router.post(
    "/hierarchies/batch-date",
    response_model=HierarchyBatchDateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def batch_create_date_hierarchies(
    project_id: UUID,
    model_id: UUID,
    body: HierarchyBatchDateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> HierarchyBatchDateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock

        grain = (body.grain or "").strip().lower()
        if grain not in DATE_HIERARCHY_TEMPLATES:
            raise _validation_error(
                "Unsupported grain template. Expected: " + ", ".join(sorted(DATE_HIERARCHY_TEMPLATES)),
                field="grain",
                code="D2",
            )

        cal_mt = await db.get(ModelTable, body.calendar_table_id)
        if cal_mt is None or cal_mt.model_id != model_id:
            raise _not_found("Calendar model table not found")

        # Bug-6722: determine expression-capable vs table-bound.
        batch_expr_capable = False
        batch_cal_type = "standard"
        cal_info_batch: CalendarTable | None = None
        if cal_mt.calendar_table_id is not None:
            cal_info_batch = await db.get(CalendarTable, cal_mt.calendar_table_id)
            if cal_info_batch is not None:
                batch_cal_type = normalize_calendar_type(
                    getattr(cal_info_batch, "calendar_type", None)
                ) or "standard"
                batch_expr_capable = (
                    batch_cal_type in EXPRESSION_CAPABLE_CALENDAR_TYPES
                    and batch_cal_type != "fiscal"
                )

        cal_date_key_col: ModelColumn | None = None

        # Table-bound types still need the calendar date-key column for the
        # alias join.
        if not batch_expr_capable:
            if cal_info_batch is not None and cal_info_batch.date_column:
                date_key_result = await db.execute(
                    select(ModelColumn)
                    .where(
                        ModelColumn.model_table_id == cal_mt.id,
                        ModelColumn.column_name == cal_info_batch.date_column,
                    )
                    .limit(1)
                )
                cal_date_key_col = date_key_result.scalar_one_or_none()

            if cal_date_key_col is None and cal_mt.table_type == "calendar":
                by_name = await db.execute(
                    select(ModelColumn)
                    .where(
                        ModelColumn.model_table_id == cal_mt.id,
                        ModelColumn.column_name == "date_key",
                    )
                    .limit(1)
                )
                cal_date_key_col = by_name.scalar_one_or_none()

            if cal_date_key_col is None and cal_mt.table_type == "calendar":
                by_type = await db.execute(
                    select(ModelColumn)
                    .where(
                        ModelColumn.model_table_id == cal_mt.id,
                        ModelColumn.data_type.ilike("%date%"),
                    )
                    .order_by(ModelColumn.column_name)
                    .limit(1)
                )
                cal_date_key_col = by_type.scalar_one_or_none()

            if cal_date_key_col is None:
                raise _validation_error(
                    "Could not determine the date key column. "
                    "Ensure the calendar table has a column named 'date_key' or a DATE-typed column.",
                    field="calendar_table_id",
                    code="D3",
                )

        unassigned = await _get_unassigned_date_cols(db, model_id)

        # When the caller specifies column_ids, process only those columns.
        col_id_filter = set(body.column_ids) if body.column_ids else None

        created_hierarchies = 0
        created_aliases = 0
        skipped: list[HierarchyBatchDateSkipped] = []
        used_aliases: set[str] = set()

        for attr in unassigned:
            if col_id_filter is not None and attr.id not in col_id_filter:
                continue
            if attr.table_id == cal_mt.id:
                skipped.append(HierarchyBatchDateSkipped(
                    column_name=attr.column_name,
                    reason="column belongs to calendar table",
                ))
                continue

            # For UDAs, resolve the underlying physical column for join/measure FKs.
            join_col_id = attr.id
            if attr.is_uda:
                if attr.physical_column_id is None:
                    skipped.append(HierarchyBatchDateSkipped(
                        column_name=attr.column_name,
                        reason="computed attribute references multiple or no physical columns",
                    ))
                    continue
                join_col_id = attr.physical_column_id

            # Same clamped label as _auto_create_date_hierarchies_for_model.
            # Without it a column display name >= ~247 chars overflows
            # HierarchyDefinition.name / ModelTable.display_name on the FIRST
            # run, and the existence check below would not recognise the
            # clamped name the auto-create path persists. Both consumers share
            # _calendar_hier_label so their names stay byte-identical.
            hier_name = _calendar_hier_label(attr.display_name, attr.column_name)
            existing = await db.execute(
                select(HierarchyDefinition.id)
                .where(
                    HierarchyDefinition.model_id == model_id,
                    HierarchyDefinition.name == hier_name,
                )
                .limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                skipped.append(HierarchyBatchDateSkipped(
                    column_name=attr.column_name,
                    reason=f"hierarchy '{hier_name}' already exists",
                ))
                continue

            if batch_expr_capable:
                # Bug-6722: expression-capable path -- UDAs on the fact table
                # directly, referencing the fact date column.  No alias, no join.
                fact_col_name = attr.column_name
                fact_col_id = attr.id
                if attr.is_uda and attr.physical_column_id is not None:
                    phys = await db.get(ModelColumn, attr.physical_column_id)
                    if phys is not None:
                        fact_col_name = phys.column_name
                        fact_col_id = phys.id

                # Bug-7203: propagate calendar_type and fiscal_year_start_month
                await _create_date_hierarchy_for_alias(
                    db,
                    model_id=model_id,
                    table_id=attr.table_id,
                    date_key_col_id=fact_col_id,
                    date_key_col_name=fact_col_name,
                    grain=grain,
                    name=hier_name,
                    calendar_type=batch_cal_type,
                    fiscal_year_start_month=(
                        cal_info_batch.fiscal_year_start_month
                        if cal_info_batch else None
                    ),
                )
                created_hierarchies += 1
            else:
                # Bug-7204: Table-bound path (retail_445, hijri) -- calendar
                # alias + join. Copy ALL period columns.
                alias_alias = await _next_calendar_alias(db, model_id, attr.column_name, used_aliases)
                used_aliases.add(alias_alias)
                alias = ModelTable(
                    model_id=model_id,
                    source_id=cal_mt.source_id,
                    table_type="dim_detail",
                    physical_name=cal_mt.physical_name,
                    alias=alias_alias,
                    display_name=hier_name,
                    calendar_table_id=cal_mt.calendar_table_id,
                )
                db.add(alias)
                await db.flush()

                alias_date_key = ModelColumn(
                    model_table_id=alias.id,
                    column_name=cal_date_key_col.column_name,
                    display_name=cal_date_key_col.display_name or cal_date_key_col.column_name,
                    data_type=cal_date_key_col.data_type,
                    is_nullable=cal_date_key_col.is_nullable,
                    is_hidden=False,
                )
                db.add(alias_date_key)
                await db.flush()

                # Bug-7204: copy all period columns and build column_id_map.
                # Use CalendarTable's configured column names when available.
                batch_column_id_map: dict[str, UUID] = {}
                batch_cal_columns = CALENDAR_COLUMN_SETS.get(batch_cal_type, {})
                for slot, default_col_name in batch_cal_columns.items():
                    if slot == "date_column":
                        continue
                    phys_col_name = (
                        getattr(cal_info_batch, slot, None) or default_col_name
                    )
                    src_col_result = await db.execute(
                        select(ModelColumn)
                        .where(
                            ModelColumn.model_table_id == cal_mt.id,
                            ModelColumn.column_name == phys_col_name,
                        )
                        .limit(1)
                    )
                    src_col = src_col_result.scalar_one_or_none()
                    if src_col is None:
                        continue
                    alias_col = ModelColumn(
                        model_table_id=alias.id,
                        column_name=src_col.column_name,
                        display_name=src_col.display_name or src_col.column_name,
                        data_type=src_col.data_type,
                        is_nullable=src_col.is_nullable,
                        is_hidden=False,
                    )
                    db.add(alias_col)
                    await db.flush()
                    batch_column_id_map[default_col_name] = alias_col.id

                # Bug-7203/7204: pass calendar metadata and column map
                hierarchy = await _create_date_hierarchy_for_alias(
                    db,
                    model_id=model_id,
                    table_id=alias.id,
                    date_key_col_id=alias_date_key.id,
                    date_key_col_name=alias_date_key.column_name,
                    grain=grain,
                    name=hier_name,
                    calendar_type=batch_cal_type,
                    fiscal_year_start_month=(
                        cal_info_batch.fiscal_year_start_month
                        if cal_info_batch else None
                    ),
                    column_id_map=batch_column_id_map if batch_column_id_map else None,
                    caption_source_table_id=cal_mt.id,
                )

                db.add(Join(
                    model_id=model_id,
                    left_table_id=attr.table_id,
                    right_table_id=alias.id,
                    # See the single-alias path above: orientation and
                    # cardinality are separate fields (invariant 3).
                    join_type=_CALENDAR_ALIAS_JOIN_TYPE,
                    cardinality=_CALENDAR_ALIAS_CARDINALITY,
                    left_column_id=join_col_id,
                    right_column_id=alias_date_key.id,
                ))

                created_aliases += 1
                created_hierarchies += 1

        await db.commit()

        return HierarchyBatchDateResponse(
            created_hierarchies=created_hierarchies,
            created_aliases=created_aliases,
            skipped=skipped,
        )
