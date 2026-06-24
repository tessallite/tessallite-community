"""
ProjectConnection CRUD + test-connection endpoint.

Credentials are encrypted with Fernet before storage and never returned in responses.

Role requirements: all mutations and connection testing require admin role.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.audit.logger import audit
from shared.db.models import DataSource, DataTarget, Model, ProjectConnection
from shared.security.credential_crypto import decrypt_json, encrypt_json
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    ConnectionCreate,
    ConnectionResponse,
    ConnectionTestRequest,
    ConnectionUpdate,
    ProfileTablesRequest,
)
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.licensing_guard import enforce_demo_source_locked

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_id}/connections", tags=["connections"])


def _encrypt(data: dict) -> bytes:
    # Rotation-aware: encrypts under the current key, decrypts under current
    # or any previous key during a rotation window (F-014-03).
    return encrypt_json(data)


def _decrypt(data: bytes) -> dict:
    return decrypt_json(data)


_SENSITIVE_KEYS = frozenset({
    "password", "service_account_json", "secret", "token", "api_key",
})

# F-014-17: sentinel that explicitly clears a stored credential field. The edit
# dialog treats a blank field as "keep stored value" (so users need not retype
# the password), which meant there was no way to *remove* a secret — e.g.
# switching a Spark connection from LDAP to NOSASL left the old password in the
# encrypted blob forever. Sending this sentinel as a field value removes the key.
CREDENTIAL_CLEAR_SENTINEL = "__CLEAR__"


def _merge_credentials(existing: dict, new_creds: dict) -> dict:
    """Merge ``new_creds`` over ``existing`` for an edit/test.

    Rules (F-014-17):
    - A blank/``None`` value means "keep the stored value" (lets the edit
      dialog omit unchanged secrets).
    - The :data:`CREDENTIAL_CLEAR_SENTINEL` value explicitly removes the key.
    - Any other value overwrites the stored one.
    """
    merged = dict(existing)
    for key, val in (new_creds or {}).items():
        if val == CREDENTIAL_CLEAR_SENTINEL:
            merged.pop(key, None)
        elif val not in (None, ""):
            merged[key] = val
    return merged


def _credentials_preview(enc: bytes | None) -> dict:
    """Decrypt and strip sensitive keys so the edit dialog can pre-fill
    non-secret fields (host, port, database, username, ...)."""
    if not enc:
        return {}
    try:
        raw = _decrypt(enc)
    except Exception:
        return {}
    return {k: v for k, v in raw.items() if k not in _SENSITIVE_KEYS}


def _to_response(c: ProjectConnection) -> ConnectionResponse:
    resp = ConnectionResponse.model_validate(c)
    resp.credentials_preview = _credentials_preview(c.encrypted_credentials)
    return resp


async def _get_connection_dependents(
    db: AsyncSession, connection_id: UUID
) -> list[str]:
    """Return display names of models that use this connection via DataSource or DataTarget."""
    model_ids: set[UUID] = set()

    src_rows = (
        await db.execute(
            select(DataSource.model_id)
            .where(DataSource.project_connection_id == connection_id)
        )
    ).scalars().all()
    model_ids.update(src_rows)

    tgt_rows = (
        await db.execute(
            select(DataTarget.model_id)
            .where(DataTarget.project_connection_id == connection_id)
        )
    ).scalars().all()
    model_ids.update(tgt_rows)

    if not model_ids:
        return []

    names = (
        await db.execute(
            select(Model.display_name).where(Model.id.in_(model_ids))
        )
    ).scalars().all()
    return list(names)


async def _run_connection_test(
    connector: str,
    creds: dict,
    config: dict,
    *,
    tenant_session: AsyncSession | None = None,
    project_id: UUID | None = None,
) -> dict:
    from shared.source_introspection import test_connection_raw

    ok, detail = await test_connection_raw(
        connector, creds, config,
        tenant_session=tenant_session,
        project_id=project_id,
    )
    if ok:
        return {"ok": True}
    return {"ok": False, "detail": detail}


@router.post(
    "",
    response_model=ConnectionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("admin")],
)
async def create_connection(
    project_id: UUID,
    body: ConnectionCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ConnectionResponse:
    enforce_demo_source_locked(current_user.tenant_id)
    async for db in get_tenant_db(current_user.tenant_id):
        encrypted = _encrypt(body.credentials)
        conn = ProjectConnection(
            project_id=project_id,
            display_name=body.display_name,
            connection_type=body.connection_type,
            encrypted_credentials=encrypted,
            config=body.config,
        )
        db.add(conn)
        await db.flush()
        await audit(
            db, action="connection.create", severity="info",
            actor_email=current_user.email,
            target_type="connection", target_id=conn.id,
            target_name=conn.display_name,
            detail={"connection_type": conn.connection_type},
        )
        await db.commit()
        await db.refresh(conn)
        return _to_response(conn)


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    project_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> list[ConnectionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        result = await db.execute(
            select(ProjectConnection)
            .where(ProjectConnection.project_id == project_id)
            .order_by(ProjectConnection.display_name)
        )
        return [_to_response(c) for c in result.scalars().all()]


@router.get(
    "/{connection_id}",
    response_model=ConnectionResponse,
    dependencies=[require_role("modeler")],
)
async def get_connection(
    project_id: UUID,
    connection_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ConnectionResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        return _to_response(c)


@router.patch(
    "/{connection_id}",
    response_model=ConnectionResponse,
    dependencies=[require_role("admin")],
)
async def update_connection(
    project_id: UUID,
    connection_id: UUID,
    body: ConnectionUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ConnectionResponse:
    enforce_demo_source_locked(current_user.tenant_id)
    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        data = body.model_dump(exclude_unset=True)
        # F-014-10: switching connector type (e.g. postgresql → snowflake)
        # must replace credentials, not merge — otherwise stale keys from the
        # old connector (host, port, ...) are permanently retained in the
        # encrypted blob and the new connection carries dead fields.
        type_changing = (
            "connection_type" in data
            and data["connection_type"] is not None
            and data["connection_type"] != c.connection_type
        )
        if "credentials" in data:
            new_creds = data.pop("credentials") or {}
            existing = (
                {}
                if type_changing
                else (_decrypt(c.encrypted_credentials) if c.encrypted_credentials else {})
            )
            c.encrypted_credentials = _encrypt(_merge_credentials(existing, new_creds))
        elif type_changing:
            # Type changed without new credentials supplied — drop the stale
            # blob so the old connector's secrets are not retained.
            c.encrypted_credentials = _encrypt({})
        if "config" in data:
            c.config = data.pop("config") or {}
        for key, val in data.items():
            setattr(c, key, val)
        await audit(
            db, action="connection.update", severity="info",
            actor_email=current_user.email,
            target_type="connection", target_id=c.id,
            target_name=c.display_name,
        )
        await db.commit()
        await db.refresh(c)
        return _to_response(c)


@router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("admin")],
)
async def delete_connection(
    project_id: UUID,
    connection_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    enforce_demo_source_locked(current_user.tenant_id)
    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")

        dependents = await _get_connection_dependents(db, connection_id)
        if dependents:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": (
                        "Cannot delete connection: it is used by one or more models. "
                        "Remove all sources and targets referencing this connection first."
                    ),
                    "dependent_models": dependents,
                },
            )

        conn_name = c.display_name
        await audit(
            db, action="connection.delete", severity="critical",
            actor_email=current_user.email,
            target_type="connection", target_id=connection_id,
            target_name=conn_name,
        )
        await db.delete(c)
        await db.commit()


@router.post(
    "/{connection_id}/test",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("admin")],
)
async def test_connection(
    project_id: UUID,
    connection_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    """
    Test that the stored credentials can open a real connection.
    Supports: bigquery, postgresql, hadoop_spark. Legacy rows whose
    connection_type is still 'jdbc' are transparently routed to the
    hadoop_spark branch via normalize_connection_type.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        creds = _decrypt(c.encrypted_credentials)
        connector = c.connection_type.lower()
        config = c.config or {}
        return await _run_connection_test(
            connector, creds, config,
            tenant_session=db, project_id=project_id,
        )


@router.post(
    "/test",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("admin")],
)
async def test_connection_payload(
    project_id: UUID,
    body: ConnectionTestRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    async for db in get_tenant_db(current_user.tenant_id):
        connector = body.connection_type.lower()
        return await _run_connection_test(
            connector, body.credentials, body.config or {},
            tenant_session=db, project_id=project_id,
        )


@router.post(
    "/{connection_id}/test_edit",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("admin")],
)
async def test_connection_merged(
    project_id: UUID,
    connection_id: UUID,
    body: ConnectionUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    """Test the stored connection with the edit dialog's partial overrides
    merged in. Empty/missing fields fall back to stored values — lets the
    user test edits without re-typing the password."""
    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        data = body.model_dump(exclude_unset=True)
        existing_creds = _decrypt(c.encrypted_credentials) if c.encrypted_credentials else {}
        new_creds = data.get("credentials") or {}
        creds = _merge_credentials(existing_creds, new_creds)
        config = data.get("config") if "config" in data else (c.config or {})
        return await _run_connection_test(
            c.connection_type.lower(), creds, config or {},
            tenant_session=db, project_id=project_id,
        )


@router.get(
    "/{connection_id}/tables",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("modeler")],
)
async def discover_tables(
    project_id: UUID,
    connection_id: UUID,
    schema_filter: str | None = None,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[dict]:
    """
    Introspect the remote database and return available tables.

    Returns a list of {"schema": "...", "table": "...", "type": "BASE TABLE"|"VIEW"}.
    Optional query param ``schema_filter`` limits results to one schema/dataset.
    """
    from shared.source_introspection import discover_tables as _discover

    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        try:
            return await _discover(
                c, schema=schema_filter, tenant_session=db,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error("Failed to discover tables for connection %s: %s", connection_id, exc, exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to discover tables: {exc}")
    return []


@router.get(
    "/{connection_id}/columns",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("modeler")],
)
async def discover_columns(
    project_id: UUID,
    connection_id: UUID,
    schema: str,
    table: str,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[dict]:
    """
    Return column names and data types for a single table.

    Query params: ``schema`` and ``table`` (both required).
    Returns: [{"column_name": "...", "data_type": "...", "is_nullable": true/false}]
    """
    from shared.source_introspection import discover_columns as _discover

    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        try:
            return await _discover(
                c, schema=schema, table=table, tenant_session=db,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error("Failed to discover columns for connection %s: %s", connection_id, exc, exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to discover columns: {exc}")
    return []


# ---------------------------------------------------------------------------
# Profile tables — classify as fact/dimension and suggest measures/dims
# Uses: naming conventions, data types, and actual cardinality from the DB
# ---------------------------------------------------------------------------

# F-014-02: classify on canonical type *family*, not connector-native spelling.
# PostgreSQL says ``integer``/``timestamp without time zone``, BigQuery says
# ``int64``/``datetime``, Snowflake ``number``/``timestamp_ntz``, SQL Server
# ``nvarchar``/``bit``, Spark ``double``/``string``. A literal-spelling set only
# recognised the PostgreSQL names, so measures and time dimensions were silently
# lost on the other four connectors. ``shared.type_family.type_family`` maps every
# spelling to ``numeric``/``datetime``/``boolean``/``text``/``other``.
from shared.type_family import (
    BOOLEAN as _FAM_BOOLEAN,
    DATETIME as _FAM_DATETIME,
    NUMERIC as _FAM_NUMERIC,
    TEXT as _FAM_TEXT,
    type_family,
)

# Name-based hints (checked as substrings of the lowercased name)
_FACT_TABLE_HINTS = (
    "fact", "fct", "transaction", "event", "order", "sale", "payment",
    "log", "activity", "metric", "billing", "invoice", "shipment",
    "click", "impression", "booking",
)
_DIM_TABLE_HINTS = (
    "dim", "dimension", "lookup", "ref", "reference", "type", "status",
    "category", "mapping", "bridge", "hierarchy", "calendar",
)

# Column name patterns that strongly indicate a foreign key / identifier
_FK_SUFFIXES = ("_id", "_key", "_fk", "_pk", "_code", "_sk")
_FK_EXACT = ("id", "key", "pk", "sk")

# Column name patterns for measures
_MEASURE_NAME_HINTS = (
    "amount", "total", "sum", "price", "cost", "revenue", "profit",
    "quantity", "qty", "count", "fee", "charge", "tax", "discount",
    "balance", "weight", "volume", "duration", "score", "rate",
    "commission", "salary", "wage", "budget", "spend", "value",
    "net", "gross", "margin",
)

# Column name patterns for dimensions (beyond FK patterns)
_DIM_COL_HINTS = (
    "name", "type", "status", "category", "flag", "desc", "label",
    "region", "country", "city", "state", "segment", "group", "class",
    "tier", "level", "mode", "method", "channel", "scheme", "brand",
    "currency", "language", "gender", "plan",
)


def _is_fk_or_pk(col_name: str) -> bool:
    """Check if a column name looks like a primary or foreign key."""
    lower = col_name.lower()
    if lower in _FK_EXACT:
        return True
    return any(lower.endswith(s) for s in _FK_SUFFIXES)


def _classify_table(
    table_name: str,
    columns: list[dict],
    row_count: int,
) -> str:
    """
    Classify a table as 'fact', 'dim_aggregate', or 'dim_detail'.

    - **fact**: transactional/event data with measures.
    - **dim_aggregate**: low/moderate cardinality dimension safe for GROUP BY,
      charts, dashboards, and KPI slicing.
    - **dim_detail**: very high row count *and* very high cardinality dimension,
      mainly for lookup, filtering, drill-through, and detailed analysis — not
      suitable for standard dashboard aggregation.

    Signals used:
    1. Table name patterns (strongest signal)
    2. Row count / cardinality (high row count -> likely fact)
    3. Column composition: numeric ratio, FK density, date presence
    4. Column cardinality ratios (high-cardinality numerics -> measures)
    """
    lower = table_name.lower()
    # Score components are tracked separately so that a fact verdict driven
    # *only* by raw row count (with no compositional fact evidence) can be
    # re-examined for the dim_detail case — see F-014-09.
    name_score = 0.0
    row_count_score = 0.0
    composition_score = 0.0

    # --- Signal 1: Table name ---
    if any(h in lower for h in _FACT_TABLE_HINTS):
        name_score += 3.0
    if any(h in lower for h in _DIM_TABLE_HINTS):
        name_score -= 3.0

    # --- Signal 2: Row count ---
    # Tables with > 1000 rows are expected to be fact tables
    # Dimension tables are typically small (< 1000 rows)
    if row_count > 100_000:
        row_count_score += 3.0
    elif row_count > 10_000:
        row_count_score += 2.5
    elif row_count > 1_000:
        row_count_score += 2.0
    elif row_count < 100:
        row_count_score -= 2.0
    elif row_count <= 1_000:
        row_count_score -= 1.0

    # --- Signal 3: Column composition ---
    total = max(len(columns), 1)
    numeric_cols = [c for c in columns if type_family(c["data_type"]) == _FAM_NUMERIC]
    date_cols = [c for c in columns if type_family(c["data_type"]) == _FAM_DATETIME]
    fk_cols = [c for c in columns if _is_fk_or_pk(c["column_name"])]

    # High ratio of numeric columns -> fact
    numeric_ratio = len(numeric_cols) / total
    if numeric_ratio >= 0.3:
        composition_score += 1.5
    elif numeric_ratio <= 0.1:
        composition_score -= 0.5

    # Presence of timestamp/date columns -> fact (events have timestamps)
    if len(date_cols) >= 2:
        composition_score += 1.0
    elif len(date_cols) == 0:
        composition_score -= 0.5

    # Many FK columns -> fact (joins to dimensions)
    fk_ratio = len(fk_cols) / total
    if fk_ratio >= 0.2:
        composition_score += 1.0

    # Few columns overall -> dimension (lookup tables are narrow)
    if total <= 5:
        composition_score -= 1.0
    elif total >= 20:
        composition_score += 0.5

    # --- Signal 4: Column cardinality ---
    # Non-FK numeric columns with high cardinality -> continuous measures -> fact
    for c in columns:
        card = c.get("approx_distinct")
        if card is None:
            continue
        fam = type_family(c["data_type"])
        name = c["column_name"].lower()
        if fam == _FAM_NUMERIC and not _is_fk_or_pk(name):
            card_ratio = card / max(row_count, 1)
            if card_ratio > 0.5:
                composition_score += 0.3
        elif fam == _FAM_TEXT:
            card_ratio = card / max(row_count, 1)
            if card_ratio < 0.01 and card < 100:
                composition_score -= 0.2

    score = name_score + row_count_score + composition_score

    if score > 0:
        # F-014-09: the dim_detail concept ("large lookup/reference tables
        # e.g. customer, product catalog") describes exactly the tables that
        # earn the large row-count bonus. A 50k-row all-text high-cardinality
        # ``customers`` table would score positive purely on row count and be
        # mislabelled fact. When the positive verdict is *not* backed by
        # name or composition evidence (both net <= 0), re-check the
        # dim_detail cardinality condition before committing to fact.
        if name_score <= 0 and composition_score <= 0:
            detail = _maybe_dim_detail(columns, row_count)
            if detail is not None:
                return detail
        return "fact"

    # --- Dimension sub-classification ---
    detail = _maybe_dim_detail(columns, row_count)
    return detail if detail is not None else "dim_aggregate"


def _maybe_dim_detail(columns: list[dict], row_count: int) -> str | None:
    """Return ``"dim_detail"`` for a large, high-cardinality dimension, else None.

    Detail dimensions: high row count AND most columns have high cardinality.
    These are large lookup/reference tables (e.g. customer, product catalog)
    not suitable for GROUP BY in dashboards.
    """
    if row_count <= 10_000:
        return None
    # Compute average cardinality ratio across non-FK, non-date columns.
    card_ratios = []
    for c in columns:
        ad = c.get("approx_distinct")
        if ad is None:
            continue
        name = c["column_name"].lower()
        if _is_fk_or_pk(name) or type_family(c["data_type"]) == _FAM_DATETIME:
            continue
        card_ratios.append(ad / max(row_count, 1))
    avg_card = sum(card_ratios) / max(len(card_ratios), 1) if card_ratios else 0
    # If the average cardinality ratio is high (most values are unique),
    # this is a detail dimension, not an aggregate dimension.
    if avg_card > 0.3:
        return "dim_detail"
    return None


def _suggest_role(
    col_name: str,
    data_type: str,
    table_class: str,
    approx_distinct: int | None,
    row_count: int,
) -> str:
    """
    Suggest 'measure', 'dimension', or 'time_dimension' for a column,
    using data type, naming, and cardinality.
    """
    fam = type_family(data_type)
    name = col_name.lower()

    # Date/timestamp → always time dimension
    if fam == _FAM_DATETIME:
        return "time_dimension"

    # Boolean → always dimension (flag)
    if fam == _FAM_BOOLEAN:
        return "dimension"

    # FK / PK columns → always dimension regardless of type
    if _is_fk_or_pk(name):
        return "dimension"

    # Numeric columns: use naming + cardinality to decide
    if fam == _FAM_NUMERIC:
        # Name strongly suggests a measure
        if any(h in name for h in _MEASURE_NAME_HINTS):
            return "measure"
        # Name strongly suggests a dimension
        if any(h in name for h in _DIM_COL_HINTS):
            return "dimension"
        # In a fact table, use cardinality to decide
        if table_class == "fact":
            if approx_distinct is not None and row_count > 0:
                card_ratio = approx_distinct / row_count
                # Low cardinality numeric (few distinct values) → likely a
                # categorical code or flag, not a measure
                if card_ratio < 0.01 and approx_distinct <= 50:
                    return "dimension"
            # Default: numeric in a fact table → measure
            return "measure"
        # In a dimension table, numerics are usually attributes, not measures
        # Exception: if the cardinality is very high, it might be a degenerate
        # measure (e.g. population, area)
        if approx_distinct is not None and row_count > 0:
            card_ratio = approx_distinct / row_count
            if card_ratio > 0.8:
                return "measure"
        return "dimension"

    # Text columns: use naming + cardinality
    if fam == _FAM_TEXT:
        return "dimension"

    # Fallback (other / unknown families → dimension is the safe default)
    return "dimension"


def _suggest_agg(col_name: str, data_type: str) -> str:
    """Suggest a default aggregation function based on column name and type."""
    name = col_name.lower()

    # Count-like columns → sum (they're pre-aggregated counts)
    if "count" in name or "qty" in name or "quantity" in name:
        return "sum"
    # Averages, rates, ratios, scores → avg
    if any(h in name for h in ("avg", "average", "rate", "ratio", "pct",
                                "percent", "score", "index")):
        return "avg"
    # FX rates, unit prices → avg (not sum)
    if "fx" in name or "exchange" in name or "unit_price" in name:
        return "avg"
    # Default for amounts, totals, counts, etc. (numeric measures sum by default,
    # across every connector's spelling — family check, not literal-type check)
    return "sum"


def _apply_role_suggestions(
    columns: list[dict], classification: str, row_count: int,
) -> None:
    """Mutate *columns* in-place: add suggested_role, suggested_agg, cardinality_ratio."""
    for col in columns:
        role = _suggest_role(
            col["column_name"],
            col["data_type"],
            classification,
            col.get("approx_distinct"),
            row_count,
        )
        col["suggested_role"] = role
        col["suggested_agg"] = (
            _suggest_agg(col["column_name"], col["data_type"])
            if role == "measure"
            else None
        )
        ad = col.get("approx_distinct")
        col["cardinality_ratio"] = (
            round(ad / max(row_count, 1), 4) if ad is not None else None
        )


@router.post(
    "/{connection_id}/profile",
    status_code=status.HTTP_200_OK,
    # F-014-01: profiling runs COUNT(*)/COUNT(DISTINCT) scans against the
    # customer's source database — gate at modeler, matching discover_tables
    # and discover_columns.
    dependencies=[require_role("modeler")],
)
async def profile_tables(
    project_id: UUID,
    connection_id: UUID,
    body: ProfileTablesRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[dict]:
    """
    Profile a list of tables: classify each as fact/dimension and suggest
    column roles (measure, dimension, time_dimension).

    Uses data types, naming conventions, and actual cardinality from the DB.

    Body: {"tables": [{"schema": "...", "table": "..."}]}
    Returns: [{schema, table, classification, row_count, columns: [{column_name,
               data_type, is_nullable, approx_distinct, cardinality_ratio,
               suggested_role, suggested_agg}]}]
    """
    from shared.source_introspection import profile_table

    # F-014-14: ``body`` is now a typed ``ProfileTablesRequest``. A malformed
    # entry (missing ``table``) is rejected by FastAPI with a 422 before this
    # handler runs, rather than surfacing a bare ``KeyError`` as a misleading
    # 502.
    requested = body.tables
    if not requested:
        return []

    results: list[dict] = []
    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")

        try:
            for tbl in requested:
                schema = tbl.schema_ or "public"
                table_name = tbl.table
                columns, row_count = await profile_table(
                    c, schema=schema, table=table_name, tenant_session=db,
                )
                try:
                    row_count = int(float(row_count))
                except (ValueError, TypeError):
                    row_count = 0
                classification = _classify_table(table_name, columns, row_count)
                _apply_role_suggestions(columns, classification, row_count)
                # F-014-02: cardinality drives several auto-classify signals. If a
                # connector/table could not return per-column distinct counts the
                # suggestions are degraded — surface that so the UI can warn the
                # modeler instead of presenting weaker suggestions as confident.
                cardinality_available = bool(columns) and all(
                    col.get("approx_distinct") is not None for col in columns
                )
                results.append({
                    "schema": schema,
                    "table": table_name,
                    "classification": classification,
                    "row_count": row_count,
                    "cardinality_available": cardinality_available,
                    "columns": columns,
                })
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error("Failed to profile tables for connection %s: %s", connection_id, exc, exc_info=True)
            raise HTTPException(
                status_code=502, detail=f"Failed to profile tables: {exc}"
            )

    return results
