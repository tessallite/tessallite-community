"""
ProjectConnection CRUD + test-connection endpoint.

Credentials are encrypted with Fernet before storage and never returned in responses.

Role requirements: all mutations and connection testing require admin role.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.artifact_target_binding import (
    connection_routing_fingerprint,
    invalidate_artifacts_for_connection,
)
from shared.audit.logger import audit_required
from shared.config.settings import get_settings
from shared.db.models import DataSource, DataTarget, Model, ProjectConnection
from shared.schemas.domains.tenants_projects import (
    credential_preview,
    redact_config_bag,
)

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

# Historical private name for the shared config-bag redactor. The logic moved to
# ``shared.schemas.domains.tenants_projects`` (Bug-8259) so the LLM-provider
# config bag echoes through the exact same barrier as the connection one; the
# alias keeps this module's call site and its test unchanged.
_redact_credential_preview_value = redact_config_bag


def _encrypt(data: dict) -> bytes:
    # Rotation-aware: encrypts under the current key, decrypts under current
    # or any previous key during a rotation window (F-014-03).
    return encrypt_json(data)


def _decrypt(data: bytes) -> dict:
    return decrypt_json(data)



# F-014-17: sentinel that explicitly clears a stored credential field. The edit
# dialog treats a blank field as "keep stored value" (so users need not retype
# the password), which meant there was no way to *remove* a secret — e.g.
# switching a Spark connection from LDAP to NOSASL left the old password in the
# encrypted blob forever. Sending this sentinel as a field value removes the key.
CREDENTIAL_CLEAR_SENTINEL = "__CLEAR__"

# Bug-7162: the same "blank means keep" rule also makes a legitimately EMPTY
# value unreachable. The credentials blob carries non-secret coordinates too
# (``database``, ``schema``, ``user``, ``role``, ...), so an admin who needs to
# blank one — e.g. drop an explicit Snowflake role so the account default
# applies — has no way to say so: ``""`` is read as "unchanged" and
# :data:`CREDENTIAL_CLEAR_SENTINEL` removes the key entirely, which is a
# different state to a connector that distinguishes "present but empty" from
# "absent". This sentinel sets the key to the empty string explicitly.
CREDENTIAL_EMPTY_SENTINEL = "__EMPTY__"


def _merge_credentials(existing: dict, new_creds: dict) -> dict:
    """Merge ``new_creds`` over ``existing`` for an edit/test.

    Rules (F-014-17 + Bug-7162):
    - A blank/``None`` value means "keep the stored value" (lets the edit
      dialog omit unchanged secrets).
    - The :data:`CREDENTIAL_CLEAR_SENTINEL` value explicitly removes the key.
    - The :data:`CREDENTIAL_EMPTY_SENTINEL` value explicitly sets the key to
      the empty string.
    - Any other value overwrites the stored one.
    """
    merged = dict(existing)
    for key, val in (new_creds or {}).items():
        if val == CREDENTIAL_CLEAR_SENTINEL:
            merged.pop(key, None)
        elif val == CREDENTIAL_EMPTY_SENTINEL:
            merged[key] = ""
        elif val not in (None, ""):
            merged[key] = val
    return merged


def _credentials_preview(enc: bytes | None) -> dict:
    """Decrypt and return ONLY the allowlisted non-secret connection
    coordinates so the edit dialog can pre-fill host, port, database,
    username, ... (Bug-6215).

    This is an allowlist, not a denylist: see
    ``shared.schemas.domains.tenants_projects.credential_preview``. A
    key-name denylist cannot cover a private key carried as the VALUE of an
    innocuous key (a PEM blob or a service-account JSON string), which is
    exactly the BigQuery shape that made this a leak.

    Bug-7158: when decryption fails (corrupted blob, key rotation without
    re-encryption), log a warning so operators can find the affected
    connection in logs. The ``return {}`` fallback is kept so one
    misconfigured connection does not break the connection list endpoint.
    """
    if not enc:
        return {}
    try:
        raw = _decrypt(enc)
    except Exception:
        logger.warning(
            "Bug-7158: credential blob decryption failed -- returning "
            "empty preview. The encrypted_credentials blob may be "
            "corrupted or encrypted under a rotated-out key.",
            exc_info=True,
        )
        return {}
    return credential_preview(raw)


async def _reject_blocked_source_host(
    connector: str | None, creds: dict | None, config: dict | None,
) -> None:
    """Refuse to STORE a connection aimed at a host the platform must not dial.

    Bug-6216. The execution path enforces the same policy at the moment a host
    becomes a socket, but that check is literal-only (no DNS on the query hot
    path), so a NAME is judged here or nowhere.

    R5: this was literal-only too, on the reasoning that a save must not fail
    because the customer's database is unresolvable right now. That left a
    real, non-racy hole: ``127.0.0.1.nip.io`` and ``localtest.me`` resolve to
    loopback every single time, pass a literal check, and are never seen by the
    resolving check because that one lives on the OPTIONAL Test-Connection
    path. Create a connection, skip Test, run one query, and the socket to
    127.0.0.1 on a port of your choosing opens.

    So the write path resolves, with the original objection answered rather
    than traded away: a host that cannot be resolved right now is ACCEPTED (and
    logged), because that is the transient-DNS case the literal-only choice was
    protecting. A host that resolves to a blocked address is refused.
    """
    from shared.security.source_host_policy import (
        SourceHostBlockedError,
        assert_source_host_allowed,
        check_source_host_literal,
    )
    from shared.schemas.connection_type import normalize_connection_type

    normalised = normalize_connection_type((connector or "").lower()) or ""

    # Bug-6216 R2 finding 4: a BigQuery connection has no host field, but the
    # uploaded service-account JSON names its own OAuth endpoints. Refuse to
    # STORE a blob that points them anywhere but Google, so it cannot fire on
    # the first query instead.
    if normalised == "bigquery":
        from shared.security.source_host_policy import (
            assert_service_account_endpoints_allowed,
        )
        sa_info = (creds or {}).get("service_account_json", creds)
        if isinstance(sa_info, str):
            try:
                import json as _json
                sa_info = _json.loads(sa_info)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"The service-account key is not valid JSON: {exc}",
                ) from exc
        try:
            assert_service_account_endpoints_allowed(sa_info)
        except SourceHostBlockedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return

    host = (creds or {}).get("host") or (config or {}).get("host")
    if not host:
        return
    try:
        check_source_host_literal(str(host), connector=normalised)
    except SourceHostBlockedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Then resolve. A name that resolves to loopback / link-local / metadata is
    # refused; a name that does not resolve at all is accepted, because "the
    # customer's DB is not reachable from here yet" is a legitimate save.
    try:
        await assert_source_host_allowed(str(host), connector=normalised)
    except SourceHostBlockedError as exc:
        if "could not be resolved" in str(exc) or "did not resolve" in str(exc):
            logger.info(
                "Connection host %r does not resolve from this deployment; "
                "saving anyway (Bug-6216: transient DNS must not block a save).",
                host,
            )
            return
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _to_response(c: ProjectConnection) -> ConnectionResponse:
    resp = ConnectionResponse.model_validate(c)
    # F-014-01 (Bug-7983): the write path now rejects secret-like keys in
    # ``config`` (schema validator ``_validate_non_sensitive_config``), but a
    # pre-existing row created before that gate could still carry a plaintext
    # secret in JSONB. Strip sensitive keys from ``config`` on every read so no
    # such value is ever echoed back to a caller (including modelers who can
    # list connections). This is the response-side belt to the write-side gate.
    resp.config = _redact_credential_preview_value(c.config or {})
    resp.credentials_preview = _credentials_preview(c.encrypted_credentials)
    return resp


async def _get_connection_dependents(
    db: AsyncSession, connection_id: UUID
) -> list[str]:
    """Return display names of models that use this connection via DataSource or DataTarget.

    Bug-7161: the previous implementation ran two separate queries for
    DataSource and DataTarget, creating a TOCTOU race where a concurrent
    source/target creation between the two checks could allow a delete to
    proceed despite a live dependent. Now both are fetched in a single
    UNION ALL query.
    """
    from sqlalchemy import union_all

    combined = union_all(
        select(DataSource.model_id).where(
            DataSource.project_connection_id == connection_id
        ),
        select(DataTarget.model_id).where(
            DataTarget.project_connection_id == connection_id
        ),
    ).subquery()

    model_ids = (
        await db.execute(select(combined.c.model_id))
    ).scalars().all()
    model_ids_set = set(model_ids)

    if not model_ids_set:
        return []

    names = (
        await db.execute(
            select(Model.display_name).where(Model.id.in_(model_ids_set))
        )
    ).scalars().all()
    return list(names)


async def _run_connection_test(
    connector: str,
    creds: dict,
    config: dict,
    *,
    bearer: str,
    project_id: UUID,
) -> dict:
    """Draft / merged Test Connection via the query-router (F-014-04)."""
    result = await _connection_introspect_via_router(
        "test-draft",
        {
            "project_id": str(project_id),
            "connection_type": connector,
            "credentials": creds,
            "config": config or {},
        },
        bearer,
        timeout_s=130.0,
    )
    if isinstance(result, dict):
        return result
    return {"ok": False, "detail": "Unexpected test response"}


def tables_from_discover_payload(raw: list | dict | None) -> list[dict]:
    """Unwrap ``{tables, truncated}`` or a legacy list from discover-tables."""
    if isinstance(raw, dict):
        tables = raw.get("tables") or []
        return [t for t in tables if isinstance(t, dict)]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, dict)]
    return []


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
    # Bug-6216: a blocked egress target must never reach the database.
    await _reject_blocked_source_host(
        body.connection_type, body.credentials, body.config
    )
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
        # F-022-01/F-022-02: creating a source/target connection is a protected
        # mutation; fail closed so it cannot commit without a durable record.
        await audit_required(
            db, action="connection.create", severity="warn",
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
        # Bug-8473 / Bug-8602: capture the connection's ROUTING identity before
        # the edit. Every aggregate and pocket built through a target on this
        # connection holds rows from the database it addressed at build time;
        # changing where it points (host / port / database / project / dataset —
        # anything but a pure secret) re-points every one of those caches at a
        # different database, where a same-named table would be served instead.
        # Under row-level security that returns rows the principal's policy was
        # never evaluated against. The SAME edit also moves the database every
        # artifact whose model reads FROM this connection was materialised from,
        # which for a cross-database aggregate involves no DataTarget at all.
        _routing_before = connection_routing_fingerprint(c)
        # F-022-01: capture reconstructive before/after for the audit record.
        # Credential values are secret and never recorded; we only note WHETHER
        # they changed. Non-secret scalar fields carry their prior and new value
        # so a compliance officer can reconstruct exactly what an admin altered.
        _audit_before: dict[str, Any] = {
            "connection_type": c.connection_type,
            "display_name": c.display_name,
        }
        _submitted_fields = sorted(data.keys())
        _credentials_changed = "credentials" in data
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
        # Bug-6216: re-check AFTER the merge — an edit that supplies only a new
        # host still has to clear the egress policy, and the merged credentials
        # are what would actually be dialled.
        #
        # R5 review finding 4: this resolves DNS while the tenant session and an
        # open transaction with pending dirty state are held. It stays here
        # deliberately. Moving it out would mean reading the row, closing the
        # session, resolving, then reopening — which introduces a TOCTOU on the
        # row the merge was computed from, a worse defect than the one it
        # avoids. The exposure is instead BOUNDED: the lookup is wrapped in
        # SOURCE_HOST_RESOLVE_TIMEOUT_SEC (default 5s), so a tarpitting
        # nameserver can no longer pin the connection indefinitely.
        await _reject_blocked_source_host(
            c.connection_type,
            _decrypt(c.encrypted_credentials) if c.encrypted_credentials else {},
            c.config or {},
        )
        # Bug-8473 / Bug-8602: if the connection now addresses a different
        # database, take every artifact built through it — written TO it OR read
        # FROM it — out of the serving pool, in THIS transaction so the two can
        # never be observed apart. ``invalidate_artifacts_for_connection`` owns
        # both directions deliberately: this is the only invalidation call a
        # connection edit makes, so a second entry point for the source side
        # would be a gap waiting to be forgotten.
        if connection_routing_fingerprint(c) != _routing_before:
            await invalidate_artifacts_for_connection(
                db, c.id,
                reason=(
                    "The connection's database location changed, so this cache "
                    "was built against a different database and must be "
                    "rebuilt before it can serve again."
                ),
            )
        # F-022-01/F-022-02: connection changes are a sensitive control-plane
        # mutation. Emit a reconstructive, fail-closed audit record before the
        # commit so the mutation cannot succeed while its evidence is lost.
        await audit_required(
            db, action="connection.update", severity="warn",
            actor_email=current_user.email,
            target_type="connection", target_id=c.id,
            target_name=c.display_name,
            detail={
                "fields": _submitted_fields,
                "credentials_changed": _credentials_changed,
                "connector_type_changed": type_changing,
                "before": _audit_before,
                "after": {
                    "connection_type": c.connection_type,
                    "display_name": c.display_name,
                },
            },
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
        # F-022-01/F-022-02: destructive connection removal is protected —
        # fail closed so the delete cannot commit without durable evidence.
        await audit_required(
            db, action="connection.delete", severity="critical",
            actor_email=current_user.email,
            target_type="connection", target_id=connection_id,
            target_name=conn_name,
            detail={"connection_type": c.connection_type},
        )
        await db.delete(c)
        await db.commit()


@router.post(
    "/{connection_id}/test",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("admin")],
)
async def test_connection(
    request: Request,
    project_id: UUID,
    connection_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    """
    Test that the stored credentials can open a real connection.
    Supports: bigquery, postgresql, hadoop_spark. Legacy rows whose
    connection_type is still 'jdbc' are transparently routed to the
    hadoop_spark branch via normalize_connection_type.

    F-014-04: the actual source dial happens in the query-router, not here.
    """
    enforce_demo_source_locked(current_user.tenant_id)
    bearer = _extract_bearer(request)
    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        result = await _connection_introspect_via_router(
            "test",
            {
                "connection_id": str(connection_id),
                "project_id": str(project_id),
            },
            bearer,
            timeout_s=130.0,
        )
        if isinstance(result, dict):
            return result
        return {"ok": False, "detail": "Unexpected test response"}
    return {"ok": False, "detail": "No tenant session"}


_settings = get_settings()


def _profile_max_tables() -> int:
    """Maximum tables accepted in a single profile request (Bug-6214).

    Each table triggers a cardinality probe against the customer's source
    database (exact COUNT on PostgreSQL; APPROX + ``__TABLES__`` on BigQuery),
    so an unbounded batch multiplies into a runaway workload. Bound the batch
    size; override with the ``PROFILE_MAX_TABLES`` env var. A value <= 0
    disables the cap. Read per-call so the env var can be tuned without a restart.
    """
    try:
        return int(os.getenv("PROFILE_MAX_TABLES", "25"))
    except (TypeError, ValueError):
        return 25


def _extract_bearer(request: Request) -> str:
    """Extract the bearer token from the incoming request for forwarding."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1]
    cookie = request.cookies.get("access_token")
    if cookie:
        return cookie
    raise HTTPException(status_code=401, detail="Bearer token required")


async def _connection_introspect_via_router(
    path: str,
    body: dict,
    bearer: str,
    *,
    timeout_s: float = 60.0,
) -> list | dict:
    """Call a query-router ``/introspect/connection/*`` endpoint.

    Bug-6213: centralises discover/profile source access through the
    query-router so the model-service never opens a direct connection to
    the customer's source database.
    """
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/introspect/connection/{path}"
    headers = {"Authorization": f"Bearer {bearer}"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = (
                    payload.get("detail")
                    if isinstance(payload, dict)
                    else resp.text
                )
            except Exception:
                detail = resp.text or f"Introspect returned HTTP {resp.status_code}"
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()


@router.post(
    "/test",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("admin")],
)
async def test_connection_payload(
    request: Request,
    project_id: UUID,
    body: ConnectionTestRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    enforce_demo_source_locked(current_user.tenant_id)
    bearer = _extract_bearer(request)
    connector = body.connection_type.lower()
    return await _run_connection_test(
        connector, body.credentials, body.config or {},
        bearer=bearer, project_id=project_id,
    )


@router.post(
    "/{connection_id}/test_edit",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("admin")],
)
async def test_connection_merged(
    request: Request,
    project_id: UUID,
    connection_id: UUID,
    body: ConnectionUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    """Test the stored connection with the edit dialog's partial overrides
    merged in. Empty/missing fields fall back to stored values -- lets the
    user test edits without re-typing the password.

    Bug-7176: when ``body.connection_type`` is provided and differs from
    the stored type, the test must use the *new* connector (matching
    ``update_connection``'s type-change semantics) and replace credentials
    rather than merging, so the tester exercises the same state that save
    would persist.
    """
    enforce_demo_source_locked(current_user.tenant_id)
    bearer = _extract_bearer(request)
    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        data = body.model_dump(exclude_unset=True)
        # Bug-7176: determine effective connector type.
        type_changing = (
            "connection_type" in data
            and data["connection_type"] is not None
            and data["connection_type"] != c.connection_type
        )
        effective_connector = (
            data["connection_type"].lower()
            if type_changing
            else c.connection_type.lower()
        )
        # Bug-7176: on type change, do not merge old credentials.
        existing_creds = (
            {}
            if type_changing
            else (_decrypt(c.encrypted_credentials) if c.encrypted_credentials else {})
        )
        new_creds = data.get("credentials") or {}
        creds = _merge_credentials(existing_creds, new_creds)
        config = data.get("config") if "config" in data else (c.config or {})
        return await _run_connection_test(
            effective_connector, creds, config or {},
            bearer=bearer, project_id=project_id,
        )


@router.get(
    "/{connection_id}/tables",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("modeler")],
)
async def discover_tables(
    request: Request,
    project_id: UUID,
    connection_id: UUID,
    schema_filter: str | None = None,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> dict:
    """
    Introspect the remote database and return available tables.

    Returns ``{"tables": [{"schema", "table", "type"}], "truncated": bool}``.
    Truncation is a flag, never a fake selectable table (F-014-05).
    Optional query param ``schema_filter`` limits results to one schema/dataset.

    Bug-6213: routed through the query-router's connection-introspect endpoint
    so all source-database access is centralised (gateway-only-data-access
    invariant).
    """
    bearer = _extract_bearer(request)

    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        raw = await _connection_introspect_via_router(
            "discover-tables",
            {
                "connection_id": str(connection_id),
                "project_id": str(project_id),
                "schema_filter": schema_filter,
            },
            bearer,
        )
        from shared.source_introspection import normalize_discover_payload
        return normalize_discover_payload(raw)
    return {"tables": [], "truncated": False}


@router.get(
    "/{connection_id}/columns",
    status_code=status.HTTP_200_OK,
    dependencies=[require_role("modeler")],
)
async def discover_columns(
    request: Request,
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

    Bug-6213: routed through the query-router's connection-introspect endpoint
    so all source-database access is centralised (gateway-only-data-access
    invariant).
    """
    bearer = _extract_bearer(request)

    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")
        return await _connection_introspect_via_router(
            "discover-columns",
            {
                "connection_id": str(connection_id),
                "project_id": str(project_id),
                "schema": schema,
                "table": table,
            },
            bearer,
        )
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
    # F-014-01: profiling runs a cardinality probe against the
    # customer's source database — gate at modeler, matching discover_tables
    # and discover_columns.
    dependencies=[require_role("modeler")],
)
async def profile_tables(
    request: Request,
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

    Bug-6213: raw column/row-count data is fetched via the query-router's
    connection-introspect endpoint (gateway-only-data-access invariant).
    Classification and role suggestions are applied locally — they are
    business logic, not source-database access.
    """
    bearer = _extract_bearer(request)

    # F-014-14: ``body`` is now a typed ``ProfileTablesRequest``. A malformed
    # entry (missing ``table``) is rejected by FastAPI with a 422 before this
    # handler runs, rather than surfacing a bare ``KeyError`` as a misleading
    # 502.
    requested = body.tables
    if not requested:
        return []

    # Bug-6214: bound the batch so profiling cannot fan out into an unbounded
    # set of full-table cardinality scans against the source database.
    max_tables = _profile_max_tables()
    if max_tables > 0 and len(requested) > max_tables:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Profile request exceeds the maximum of {max_tables} tables "
                f"per call ({len(requested)} requested). Profiling runs "
                f"COUNT(*)/COUNT(DISTINCT) scans against the source database; "
                f"split the request into smaller batches."
            ),
        )

    async for db in get_tenant_db(current_user.tenant_id):
        c = await db.get(ProjectConnection, connection_id)
        if c is None or c.project_id != project_id:
            raise HTTPException(status_code=404, detail="Connection not found")

        # Fetch raw profile data from the query-router.
        raw_profiles = await _connection_introspect_via_router(
            "profile",
            {
                "connection_id": str(connection_id),
                "project_id": str(project_id),
                "tables": [
                    {"schema": tbl.schema_ or "public", "table": tbl.table}
                    for tbl in requested
                ],
            },
            bearer,
            timeout_s=120.0,
        )

        # Apply classification / role suggestion (business logic) locally.
        results: list[dict] = []
        for entry in raw_profiles:
            schema = entry["schema"]
            table_name = entry["table"]
            columns = entry["columns"]
            row_count = entry["row_count"]
            try:
                row_count = int(float(row_count))
            except (ValueError, TypeError):
                row_count = 0
            classification = _classify_table(table_name, columns, row_count)
            _apply_role_suggestions(columns, classification, row_count)
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
        return results

    return []
