"""
Hierarchy CRUD, level CRUD, and reorder routes.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from uuid import UUID

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError

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
from shared.semantic.calendar_types import (
    CALENDAR_TYPES,
    normalize_calendar_type,
)
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
from src.auth.middleware import CurrentUser, enforce_model_scope, get_current_user
from src.auth.rbac import require_role
from src.api._persona_scope import get_excluded_level_attribute_ids, parse_allowed_ids, resolve_effective_persona
from shared.security import Principal, compile_row_security

from shared.connector_qualify import CONNECTOR_TO_SQLGLOT as _CONNECTOR_TO_SQLGLOT, transpile_preview_sql


# ---------------------------------------------------------------------------
# Register missing sqlglot dialect generators (sqlglot 30.x gaps)
# ---------------------------------------------------------------------------
from shared.sqlglot_compat import register_bigquery_patches
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


async def _next_calendar_alias(db, model_id: UUID, col_name: str, used: set[str]) -> str:
    """Return '{col_slug}_calendar', auto-sequenced if already taken in model or used set."""
    existing = set(
        (
            await db.execute(select(ModelTable.alias).where(ModelTable.model_id == model_id))
        ).scalars().all()
    ) | used
    base = f"{_slugify_name(col_name)}_calendar"
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def _date_component_expression(source_expr: str, component: str) -> str:
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

    if require_dimension_table and table.table_type == "fact":
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


async def _collect_companion_alias_table_ids(
    db, *, model_id: UUID, levels: list[HierarchyLevel]
) -> set[UUID]:
    """Return the ids of dedicated calendar-alias ModelTables that hold this
    hierarchy's level-key UDAs.

    Generated date hierarchies (batch-date / auto-create) materialise a
    companion ``dim_detail`` alias ModelTable — one per fact date column — that
    carries a calendar date-key column + the level UDAs, joined many-to-one to
    the fact column (hierarchies.py ``_auto_create_date_hierarchies_for_model``
    / ``batch-date``). A candidate alias has ``calendar_table_id`` set; the
    fact/dimension tables a *user* hierarchy keys on never do, so they are
    never collected here.
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
            ModelTable.calendar_table_id.is_not(None),
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
    enforce_model_scope(current_user, str(model_id))
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


@router.get("/hierarchies/{hierarchy_id}", response_model=HierarchyDetailResponse)
async def get_hierarchy(
    project_id: UUID,
    model_id: UUID,
    hierarchy_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> HierarchyDetailResponse:
    enforce_model_scope(current_user, str(model_id))
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
    enforce_model_scope(current_user, str(model_id))
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
        generated_names = [f"{base_name}_{component}" for component, _ in components]
        gen_expressions = [
            _date_component_expression(canonical_col_expr, component)
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
                            display_name=f"{level_name} ({body.name})",
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
        generated_names = [f"{base_name}_seg_{idx + 1}" for idx in range(len(level_names))]
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
    enforce_model_scope(current_user, str(model_id))
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

        # Compile RLS predicate if persona doesn't bypass row security.
        rls_where: str | None = None
        if persona and not persona.bypass_row_security:
            principal = Principal.from_current_user(current_user)
            compiled = await compile_row_security(model_id, principal, db)
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
    cal_join_subq = (
        select(Join.left_column_id)
        .join(ModelTable, ModelTable.id == Join.right_table_id)
        .where(
            Join.model_id == model_id,
            ModelTable.calendar_table_id.is_not(None),
        )
        .scalar_subquery()
    )

    # Physical columns with date/time types
    phys_result = await db.execute(
        select(ModelColumn, ModelTable)
        .join(ModelTable, ModelTable.id == ModelColumn.model_table_id)
        .where(
            ModelTable.model_id == model_id,
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

    # UDAs with date/time output types
    uda_result = await db.execute(
        select(UserDefinedAttribute, ModelTable)
        .join(ModelTable, ModelTable.id == UserDefinedAttribute.table_id)
        .where(
            UserDefinedAttribute.model_id == model_id,
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
    grain: str = "y_m_d",
) -> tuple[int, list[str], list[str]]:
    """Auto-create date hierarchies for all unassigned fact-table date columns.

    Called after a calendar is created or bound. Returns (created_count, skipped_reasons).
    Raises ValueError if calendar_model_table_id does not reference a valid calendar alias.
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
        return 0, [f"Calendar date key column '{cal_info.date_column}' not found in calendar alias"]

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

        hier_name = f"{attr.display_name or attr.column_name} Calendar"
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

        alias_alias = await _next_calendar_alias(db, model_id, attr.column_name, used_aliases)
        used_aliases.add(alias_alias)
        alias = ModelTable(
            model_id=model_id,
            source_id=cal_mt.source_id,
            table_type="dim_detail",
            physical_name=cal_mt.physical_name,
            alias=alias_alias,
            display_name=f"{attr.display_name or attr.column_name} Calendar",
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

        await _create_date_hierarchy_for_alias(
            db,
            model_id=model_id,
            table_id=alias.id,
            date_key_col_id=alias_date_key.id,
            date_key_col_name=alias_date_key.column_name,
            grain=grain,
            name=hier_name,
        )

        db.add(Join(
            model_id=model_id,
            left_table_id=attr.table_id,
            right_table_id=alias.id,
            join_type="many_to_one",
            left_column_id=join_col_id,
            right_column_id=alias_date_key.id,
        ))
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
) -> HierarchyDefinition:
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
        },
    )
    db.add(hierarchy)
    await db.flush()

    generated_names = [f"{_slugify_name(name)}_{component}" for component, _ in components]
    gen_expressions = [_date_component_expression(expr, component) for component, _ in components]
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
            db.add(
                Dimension(
                    model_id=model_id,
                    name=gen_name,
                    display_name=f"{level_name} ({name})",
                    user_defined_attribute_id=uda.id,
                    is_time_dim=True,
                    time_grain=time_grain,
                    description=f"Auto-generated for hierarchy '{name}' ({component})",
                )
            )

    await db.flush()
    return hierarchy


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
    enforce_model_scope(current_user, str(model_id))
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

        cal_date_key_col: ModelColumn | None = None

        if cal_mt.calendar_table_id is not None:
            cal_info = await db.get(CalendarTable, cal_mt.calendar_table_id)
            if cal_info is not None and cal_info.date_column:
                date_key_result = await db.execute(
                    select(ModelColumn)
                    .where(
                        ModelColumn.model_table_id == cal_mt.id,
                        ModelColumn.column_name == cal_info.date_column,
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

            hier_name = f"{attr.display_name or attr.column_name} Calendar"
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

            alias_alias = await _next_calendar_alias(db, model_id, attr.column_name, used_aliases)
            used_aliases.add(alias_alias)
            alias = ModelTable(
                model_id=model_id,
                source_id=cal_mt.source_id,
                table_type="dim_detail",
                physical_name=cal_mt.physical_name,
                alias=alias_alias,
                display_name=f"{attr.display_name or attr.column_name} Calendar",
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

            hierarchy = await _create_date_hierarchy_for_alias(
                db,
                model_id=model_id,
                table_id=alias.id,
                date_key_col_id=alias_date_key.id,
                date_key_col_name=alias_date_key.column_name,
                grain=grain,
                name=hier_name,
            )

            db.add(Join(
                model_id=model_id,
                left_table_id=attr.table_id,
                right_table_id=alias.id,
                join_type="many_to_one",
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
