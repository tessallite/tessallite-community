"""Routing identity of a materialised artifact's databases (Bug-8473/Bug-8602).

A materialised artifact has TWO routing identities, and both are mutable
control-plane state that can move without the artifact row changing at all:

* the TARGET — WHERE the rows were written (Bug-8473); and
* the SOURCE — WHICH database the CTAS read them FROM (Bug-8602).

They are two sides of ONE concept, so they live in one module and share one
fingerprint function, one endpoint resolver, one lock-for-finalization
convention and one invalidation policy. Forking them is how the source side
went uncovered for as long as it did: the target mechanism was assumed to cover
"the connection", when it only ever enumerated ``DataTarget``.

A pocket or aggregate names its cached table by ``(target_schema,
physical_table_name)``. Those names do NOT identify a table on their own: which
DATABASE they resolve to is decided at execution time by
``DataTarget.project_connection_id`` -> ``ProjectConnection`` (host / port /
database, or BigQuery project / dataset), plus ``DataTarget.config``. Both are
mutable control-plane state.

So a modeller who re-points a target at another connection, or an admin who
edits a connection's endpoint, silently changes which physical table every
already-built artifact on that target reads from — with no change to the
artifact row at all. If the new database happens to contain a table of the same
name (a clone, a restore, a same-named table in another environment), the cached
artifact serves ITS rows. Under active row-level security the injected predicate
is evaluated against that foreign table's column, so a row that was never part
of the admitted model population can satisfy it and be returned: a row-security
EXPOSURE, not merely a stale number.

Two mechanisms close that, and both live here so they cannot drift apart:

1. :func:`routing_fingerprint` — a stable hash of everything that decides WHICH
   database a connection addresses. The pocket producer records it with the
   build (``row_manifest["target_binding"]``); the query-router's generation
   guard compares the recorded binding with live state before it scans. A
   mismatch falls back to source.
2. :func:`invalidate_artifacts_for_target` /
   :func:`invalidate_artifacts_for_connection` — the ROOT-CAUSE half. The
   control-plane writers call these in the SAME transaction as the edit, so any
   routing-affecting change immediately takes every artifact built against the
   old location out of the serving pool until it is rebuilt.

Fingerprint policy — fail closed on the unknown
-----------------------------------------------
Every scalar in the connection's credentials and config contributes, EXCEPT an
explicit deny-list of pure secrets (passwords, keys, tokens). An unrecognised
key therefore CHANGES the fingerprint, which costs a rebuild but can never miss
a re-point. The inverse policy — an allow-list of known routing keys — would
silently miss the first connector option nobody thought of, which is exactly the
enumeration blind spot this codebase keeps rediscovering.

Note the consequence: rotating a credential that is NOT on the deny-list
invalidates artifacts on that connection. That is deliberate; over-invalidation
costs a refresh, under-invalidation leaks rows.

The SOURCE side (Bug-8602)
--------------------------
Cross-database aggregates are a first-class mode: source connection A, target
connection B. ``resolve_source_connection`` picks A from
``DataSource.project_connection_id`` across the model's tables, and the CTAS
reads its rows FROM A. Editing A's host/database therefore changes what the
next build would produce, while the already-built artifact keeps serving rows
materialised from the OLD database — and the source-route fallback for the same
question reads the NEW one. Two routes, two answers, no staleness, no error.

The same three mechanisms close it, in the same shapes:

1. :func:`invalidate_artifacts_for_source_connection` — the root-cause half,
   folded into :func:`invalidate_artifacts_for_connection` so the ONE
   control-plane call site cannot cover one side and forget the other;
2. :func:`capture_source_build_binding` /
   :func:`source_build_binding_matches_live` — build-start capture and
   stamp-time re-proof under row locks, for the window in which the artifact
   row does not exist yet or is about to be restored to a serving status;
3. the recorded ``AggregateDefinition.built_for_source_binding``, re-proved by
   the query-router's aggregate generation guard, for the paths that move the
   EFFECTIVE source endpoint without touching the connection row at all (the
   persisted ``source_db.fallback_*`` chain, Bug-8482).

Residual closed by Bug-8772/Bug-8794: the three mechanisms above prove the
recorded SOURCE binding still matches live state at finalisation, but between
``resolve_source_connection`` and that finalisation check a build's CTAS setup
kept dialling the live ORM ``ProjectConnection`` object for every physical
source operation (numeric-type introspection, cross-database batch reads, the
finalisation row-lock id). A same-ID connection repoint that lands mid-build
and refreshes that ORM instance could silently redirect those physical
operations to the replacement endpoint before the fail-closed binding
comparison ever runs — wasting a build, or worse, reading rows for
diagnostics from a database the build was never proven against.
:func:`freeze_source_execution_connection` mirrors the SHAPE of
:func:`freeze_target_execution_connection` (Bug-8481 R1), which closed the
identical hazard for the optimizer creator's TARGET connection specifically.
That does not mean the target side is closed everywhere it could apply:
``freeze_target_execution_connection`` has exactly one caller
(``optimizer/src/lifecycle/creator.py``) today — the scheduler's
``full_refresh.py``/``incremental_refresh.py`` still address ``target_conn``
live throughout their build, an existing, separate residual this fix does not
touch.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Bump when the fingerprint COMPOSITION changes. A recorded binding carrying a
# different version can never compare equal, so artifacts fall back to source
# until their next refresh re-records one — fail closed, no silent acceptance.
FINGERPRINT_VERSION = "v2"

# Pure secrets: material that authenticates to a database but does not decide
# WHICH database. Everything not listed here contributes to the fingerprint.
_SECRET_KEYS = frozenset({
    "password", "passwd", "pass", "secret", "client_secret", "token",
    "access_token", "refresh_token", "api_key", "apikey", "private_key",
    "private_key_id", "auth_provider_x509_cert_url", "client_x509_cert_url",
    "auth_uri", "token_uri", "certificate", "cert", "ssl_key", "sslkey",
})


@dataclass(frozen=True)
class ArtifactTargetBuildBinding:
    """Immutable storage routing identity captured before materialisation.

    Plain scalars deliberately outlive ORM expiry/refresh and are safe to carry
    across the commits an aggregate build performs.  The dictionary shape is
    shared with pocket ``row_manifest.target_binding`` so the routing identity
    has one producer contract across artifact kinds.
    """

    target_id: str
    project_connection_id: str
    routing_fingerprint: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactTargetBuildBinding":
        return cls(
            target_id=str(value.get("target_id") or ""),
            project_connection_id=str(value.get("project_connection_id") or ""),
            routing_fingerprint=str(value.get("routing_fingerprint") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "project_connection_id": self.project_connection_id,
            "routing_fingerprint": self.routing_fingerprint,
        }

    def matches(self, other: "ArtifactTargetBuildBinding") -> bool:
        """Exact equality; missing values can never prove storage identity."""
        if not all(
            (
                self.target_id,
                self.project_connection_id,
                self.routing_fingerprint,
                other.target_id,
                other.project_connection_id,
                other.routing_fingerprint,
            )
        ):
            return False
        return self == other


@dataclass(frozen=True)
class ArtifactSourceBuildBinding:
    """Immutable SOURCE routing identity captured before the CTAS runs.

    The sibling of :class:`ArtifactTargetBuildBinding` for the other side of the
    build: which database the rows were READ FROM, rather than written to.

    ``model_id`` is part of the binding, not merely a lookup argument, so
    :func:`source_build_binding_matches_live` is self-contained in exactly the
    way ``target_id`` makes the target binding self-contained — a serving guard
    holding only the recorded dict can re-prove it with no other input.

    ``source_connection_id`` catches a re-point of
    ``DataSource.project_connection_id`` to a DIFFERENT connection;
    ``routing_fingerprint`` catches an in-place edit of the SAME connection's
    endpoint.

    ``source_connection_project_id`` carries the Bug-5325 tenant-isolation
    guard through to serve time. The BUILD path refuses a source connection
    whose project differs from the owning model's
    (``resolve_source_connection`` -> ``assert_connection_in_project``), but the
    source connection is never dialled while serving an aggregate, so nothing
    would otherwise re-check it. Recording the project the connection was in at
    build time enforces it with no extra query: it was equal to the model's
    project then (the build proved it), so the connection later moving to
    another project breaks the binding here. Stated residual: this treats
    ``Model.project_id`` as immutable, which it is — no route writes it.
    """

    model_id: str
    source_connection_id: str
    source_connection_project_id: str
    routing_fingerprint: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactSourceBuildBinding":
        return cls(
            model_id=str(value.get("model_id") or ""),
            source_connection_id=str(value.get("source_connection_id") or ""),
            source_connection_project_id=str(
                value.get("source_connection_project_id") or ""
            ),
            routing_fingerprint=str(value.get("routing_fingerprint") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "model_id": self.model_id,
            "source_connection_id": self.source_connection_id,
            "source_connection_project_id": self.source_connection_project_id,
            "routing_fingerprint": self.routing_fingerprint,
        }

    def matches(self, other: "ArtifactSourceBuildBinding") -> bool:
        """Exact equality; a missing value can never prove source identity."""
        if not all(
            (
                self.model_id,
                self.source_connection_id,
                self.source_connection_project_id,
                self.routing_fingerprint,
                other.model_id,
                other.source_connection_id,
                other.source_connection_project_id,
                other.routing_fingerprint,
            )
        ):
            return False
        return self == other


@dataclass(frozen=True)
class ArtifactTargetExecutionConnection:
    """Detached build-start connection values used for physical operations.

    A metadata finalisation query can legitimately refresh ORM rows in the
    build session. Physical verification and orphan cleanup must nevertheless
    continue to address the endpoint used by the build, including when a
    connection is re-pointed in place under the same primary key.
    """

    id: Any
    project_id: Any
    connection_type: Any
    encrypted_credentials: Any
    config: dict[str, Any]
    display_name: Any = None
    tenant_slug: Any = None
    tenant_id: Any = None


@dataclass(frozen=True)
class _TargetRoutingState:
    id: Any
    project_connection_id: Any
    config: Any


@dataclass(frozen=True)
class _ConnectionRoutingState:
    id: Any
    project_id: Any
    connection_type: Any
    encrypted_credentials: Any
    config: Any


def freeze_target_execution_connection(
    conn: Any,
) -> ArtifactTargetExecutionConnection:
    """Copy the execution-relevant connection state out of the ORM identity map."""
    from copy import deepcopy

    config = getattr(conn, "config", None) or {}
    if not isinstance(config, dict):
        raise ValueError("Connection config is not an object")
    return ArtifactTargetExecutionConnection(
        id=getattr(conn, "id", None),
        project_id=getattr(conn, "project_id", None),
        connection_type=getattr(conn, "connection_type", None),
        encrypted_credentials=getattr(conn, "encrypted_credentials", None),
        config=deepcopy(config),
        display_name=getattr(conn, "display_name", None),
        tenant_slug=getattr(conn, "tenant_slug", None),
        tenant_id=getattr(conn, "tenant_id", None),
    )


@dataclass(frozen=True)
class ArtifactSourceExecutionConnection:
    """Detached build-start SOURCE connection values used for physical reads.

    The sibling of :class:`ArtifactTargetExecutionConnection` for the read
    side of a build (Bug-8772/Bug-8794). Numeric-type introspection,
    cross-database batch reads (``open_source_connection``), and the
    finalisation row-lock id must all keep addressing the endpoint the build
    was frozen against — never whatever the ORM identity map holds if a
    same-ID connection repoint lands mid-build. Same field shape as the
    target side; a plain scalar copy outlives ORM expiry/refresh.
    """

    id: Any
    project_id: Any
    connection_type: Any
    encrypted_credentials: Any
    config: dict[str, Any]
    display_name: Any = None
    tenant_slug: Any = None
    tenant_id: Any = None


def freeze_source_execution_connection(
    conn: Any,
) -> ArtifactSourceExecutionConnection:
    """Copy the execution-relevant SOURCE connection state out of the ORM
    identity map (Bug-8772/Bug-8794 — mirrors
    :func:`freeze_target_execution_connection`)."""
    from copy import deepcopy

    config = getattr(conn, "config", None) or {}
    if not isinstance(config, dict):
        raise ValueError("Connection config is not an object")
    return ArtifactSourceExecutionConnection(
        id=getattr(conn, "id", None),
        project_id=getattr(conn, "project_id", None),
        connection_type=getattr(conn, "connection_type", None),
        encrypted_credentials=getattr(conn, "encrypted_credentials", None),
        config=deepcopy(config),
        display_name=getattr(conn, "display_name", None),
        tenant_slug=getattr(conn, "tenant_slug", None),
        tenant_id=getattr(conn, "tenant_id", None),
    )


def _routing_view(value: Any) -> Any:
    """Strip pure secrets, keep everything else (fail closed on unknown keys)."""
    if isinstance(value, dict):
        return {
            k: _routing_view(v)
            for k, v in sorted(value.items())
            if str(k).strip().lower() not in _SECRET_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_routing_view(v) for v in value]
    return value


def _explicit_source_db_endpoint(
    connection_type: Any,
    credentials: Any,
    config: Any,
) -> dict[str, Any] | None:
    """Return a complete stored PostgreSQL-family endpoint, if present.

    Partial endpoints deliberately return ``None``: completing them requires
    the async setting resolver, which is owned by
    :func:`resolve_target_binding_dict`.
    """
    from shared.schemas.connection_type import normalize_connection_type

    connector = normalize_connection_type(str(connection_type or ""))
    if connector not in ("postgresql", "redshift"):
        return None
    if not isinstance(credentials, dict) or not isinstance(config, dict):
        return None
    host = credentials.get("host") or config.get("host")
    port = credentials.get("port") or config.get("port")
    database = credentials.get("database") or config.get("database")
    if not host or not port or not database:
        return None
    return {
        "host": str(host),
        "port": int(port),
        "database": str(database),
    }


def _connection_inputs(conn: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decrypt and validate the two connection routing-input dictionaries."""
    from shared.security.credential_crypto import decrypt_json

    blob = getattr(conn, "encrypted_credentials", None)
    credentials: Any = decrypt_json(blob) if blob else {}
    if not isinstance(credentials, dict):
        raise ValueError("Connection credentials did not decrypt to an object")

    config: Any = getattr(conn, "config", None) or {}
    if not isinstance(config, dict):
        raise ValueError("Connection config is not an object")
    return credentials, config


async def _resolve_postgres_family_endpoint(
    connection_type: Any,
    credentials: dict[str, Any],
    config: dict[str, Any],
    *,
    tenant_session: Any = None,
    system_session: Any = None,
    project_id: Any = None,
) -> dict[str, Any] | None:
    """Resolve a PostgreSQL-family endpoint with persisted system fallbacks."""
    from shared.schemas.connection_type import normalize_connection_type

    connector = normalize_connection_type(str(connection_type or ""))
    if connector not in ("postgresql", "redshift"):
        return None

    explicit = _explicit_source_db_endpoint(
        connection_type,
        credentials,
        config,
    )
    if explicit is not None:
        return explicit

    from shared.config.source_db import resolve_source_db_endpoint

    async def _resolve(sys_db: Any) -> dict[str, Any]:
        host, port, database = await resolve_source_db_endpoint(
            credentials,
            config,
            tenant_session=tenant_session,
            system_session=sys_db,
            project_id=project_id,
        )
        return {
            "host": host,
            "port": port,
            "database": database,
        }

    if system_session is not None:
        return await _resolve(system_session)

    from shared.db.session import SystemSessionLocal

    async with SystemSessionLocal() as sys_db:
        return await _resolve(sys_db)


async def resolve_connection_source_db_endpoint(
    conn: Any,
    *,
    tenant_session: Any = None,
    system_session: Any = None,
) -> dict[str, Any] | None:
    """Effective PostgreSQL-family endpoint used by a connection, if relevant.

    A real system session is opened when the stored connection does not provide
    all three endpoint fields. This is the production path that makes persisted
    ``source_db.fallback_*`` values authoritative instead of silently replacing
    them with registry defaults.
    """
    credentials, config = _connection_inputs(conn)
    return await _resolve_postgres_family_endpoint(
        getattr(conn, "connection_type", None),
        credentials,
        config,
        tenant_session=tenant_session,
        system_session=system_session,
        project_id=getattr(conn, "project_id", None),
    )


def routing_fingerprint(
    *,
    connection_type: Any,
    credentials: Any = None,
    config: Any = None,
    target_config: Any = None,
    resolved_endpoint: Any = None,
) -> str:
    """Stable hash of the storage location a connection (+ target) addresses.

    Deterministic across processes and replicas: canonical JSON, sorted keys.
    ``target_config`` is included because a ``DataTarget``'s own config decides
    the schema / BigQuery dataset (and project) the artifact table is qualified
    with, so a config edit re-points the table just as a connection swap does.

    ``resolved_endpoint`` is the effective address selected after all fallback
    layers. It is required for PostgreSQL/Redshift artifact bindings because
    ``source_db.fallback_*`` can move execution without changing the stored
    connection row (Bug-8482).
    """
    payload = {
        "v": FINGERPRINT_VERSION,
        "connection_type": str(connection_type or ""),
        "credentials": _routing_view(credentials or {}),
        "config": _routing_view(config or {}),
        "target_config": _routing_view(target_config or {}),
        "resolved_endpoint": _routing_view(resolved_endpoint or {}),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


def connection_routing_fingerprint(conn: Any, target: Any = None) -> str:
    """Stored-state fingerprint for control-plane comparisons.

    This synchronous helper cannot read async setting fallbacks. It is used to
    detect edits to the connection row itself. Artifact build/serve bindings
    must use :func:`resolve_target_binding_dict`, which also hashes the endpoint
    selected by execution.
    """
    creds: Any = {}
    try:
        # Local import: avoids a package cycle at module import time.
        from shared.security.credential_crypto import decrypt_json

        blob = getattr(conn, "encrypted_credentials", None)
        if blob:
            creds = decrypt_json(blob)
    except Exception:
        logger.warning(
            "Could not read connection %s credentials for its routing "
            "fingerprint; the artifact binding will not match and routes fall "
            "back to source",
            getattr(conn, "id", None), exc_info=True,
        )
        return f"{FINGERPRINT_VERSION}:unreadable"
    connection_type = getattr(conn, "connection_type", None)
    config = getattr(conn, "config", None) or {}
    return routing_fingerprint(
        connection_type=connection_type,
        credentials=creds,
        config=config,
        target_config=getattr(target, "config", None) if target is not None else None,
        resolved_endpoint=_explicit_source_db_endpoint(
            connection_type,
            creds,
            config,
        ),
    )


def target_binding_dict(target: Any, conn: Any) -> dict[str, str]:
    """Stored-state binding for synchronous control-plane/test callers.

    Artifact producers and serving guards must use
    :func:`resolve_target_binding_dict` so setting-backed endpoint fallbacks are
    included.
    """
    return {
        "target_id": str(getattr(target, "id", "") or ""),
        "project_connection_id": str(
            getattr(target, "project_connection_id", "") or ""
        ),
        "routing_fingerprint": connection_routing_fingerprint(conn, target),
    }


async def resolve_target_binding_dict(
    target: Any,
    conn: Any,
    *,
    tenant_session: Any = None,
    system_session: Any = None,
) -> dict[str, str]:
    """Resolve the exact storage binding used by materialisation/execution.

    The executor's only ``get_setting``-driven addressing path is the
    PostgreSQL/Redshift ``source_db.fallback_*`` chain. Other connector
    endpoints are fully represented by credentials/config. Any decryption or
    endpoint-resolution error is raised so producers clear the manifest and
    serving guards refuse the pocket.
    """
    credentials, config = _connection_inputs(conn)
    resolved_endpoint = await _resolve_postgres_family_endpoint(
        getattr(conn, "connection_type", None),
        credentials,
        config,
        tenant_session=tenant_session,
        system_session=system_session,
        project_id=getattr(conn, "project_id", None),
    )

    return {
        "target_id": str(getattr(target, "id", "") or ""),
        "project_connection_id": str(
            getattr(target, "project_connection_id", "") or ""
        ),
        "routing_fingerprint": routing_fingerprint(
            connection_type=getattr(conn, "connection_type", None),
            credentials=credentials,
            config=config,
            target_config=(
                getattr(target, "config", None) if target is not None else None
            ),
            resolved_endpoint=resolved_endpoint,
        ),
    }


async def capture_target_build_binding(
    target: Any,
    conn: Any,
    *,
    tenant_session: Any = None,
    system_session: Any = None,
) -> ArtifactTargetBuildBinding:
    """Freeze the resolved storage identity before the first physical write."""
    return ArtifactTargetBuildBinding.from_dict(
        await resolve_target_binding_dict(
            target,
            conn,
            tenant_session=tenant_session,
            system_session=system_session,
        )
    )


async def current_target_build_binding(
    db: AsyncSession,
    target_id: Any,
    *,
    lock_for_finalization: bool = False,
    system_session: Any = None,
) -> ArtifactTargetBuildBinding | None:
    """Read the current target/connection routing identity from committed state.

    ``lock_for_finalization`` closes the comparison-to-activation race.  The
    target and connection rows stay locked until the caller commits aggregate
    metadata: a repoint that committed first is observed here, while a repoint
    that starts later waits and then invalidates the completed aggregate.

    Autoflush is suppressed deliberately.  Completion paths have dirty
    aggregate metadata; flushing it before acquiring these control-plane locks
    can deadlock with a connection edit that already owns the connection row
    and is waiting to invalidate that aggregate.
    """
    from shared.db.models import DataTarget, ProjectConnection

    with db.no_autoflush:
        target_stmt = (
            select(
                DataTarget.id,
                DataTarget.project_connection_id,
                DataTarget.config,
            )
            .where(DataTarget.id == target_id)
        )
        if lock_for_finalization:
            target_stmt = target_stmt.with_for_update()
        target_row = (await db.execute(target_stmt)).one_or_none()
        if target_row is None:
            return None

        target = _TargetRoutingState(*target_row)
        connection_id = target.project_connection_id
        if connection_id is None:
            return None
        conn_stmt = (
            select(
                ProjectConnection.id,
                ProjectConnection.project_id,
                ProjectConnection.connection_type,
                ProjectConnection.encrypted_credentials,
                ProjectConnection.config,
            )
            .where(ProjectConnection.id == connection_id)
        )
        if lock_for_finalization:
            conn_stmt = conn_stmt.with_for_update()
        conn_row = (await db.execute(conn_stmt)).one_or_none()
        if conn_row is None:
            return None
        conn = _ConnectionRoutingState(*conn_row)

        return await capture_target_build_binding(
            target,
            conn,
            tenant_session=db,
            system_session=system_session,
        )


async def target_build_binding_matches_live(
    db: AsyncSession,
    binding: ArtifactTargetBuildBinding,
    *,
    lock_for_finalization: bool = False,
    system_session: Any = None,
) -> bool:
    """Fail-closed proof that a captured build still addresses live storage."""
    try:
        live = await current_target_build_binding(
            db,
            binding.target_id,
            lock_for_finalization=lock_for_finalization,
            system_session=system_session,
        )
    except Exception:
        logger.warning(
            "Could not re-prove target storage binding for target %s; "
            "aggregate completion will remain non-serving",
            binding.target_id,
            exc_info=True,
        )
        return False
    return live is not None and binding.matches(live)


def apply_target_build_binding(
    artifact: Any,
    binding: ArtifactTargetBuildBinding,
) -> None:
    """Persist build-start storage truth; never substitute a completion read."""
    artifact.built_for_storage_binding = binding.to_dict()


# ---------------------------------------------------------------------------
# SOURCE side (Bug-8602) — same mechanism, other direction
# ---------------------------------------------------------------------------


async def resolve_source_binding_dict(
    model_id: Any,
    conn: Any,
    *,
    tenant_session: Any = None,
    system_session: Any = None,
) -> dict[str, str]:
    """Resolve the exact SOURCE binding a build reads through.

    Deliberately the same body as :func:`resolve_target_binding_dict` minus
    ``target_config`` (a source read is not qualified by a ``DataTarget``): the
    same ``_connection_inputs`` decryption, the same
    ``_resolve_postgres_family_endpoint`` fallback chain, and the same
    :func:`routing_fingerprint`. One producer, so the build-time capture and the
    query-router's serve-time re-derivation cannot disagree by construction.

    Any decryption or endpoint-resolution error is raised so producers refuse to
    stamp and serving guards refuse the route.
    """
    credentials, config = _connection_inputs(conn)
    resolved_endpoint = await _resolve_postgres_family_endpoint(
        getattr(conn, "connection_type", None),
        credentials,
        config,
        tenant_session=tenant_session,
        system_session=system_session,
        project_id=getattr(conn, "project_id", None),
    )
    return {
        "model_id": str(model_id or ""),
        "source_connection_id": str(getattr(conn, "id", "") or ""),
        "source_connection_project_id": str(
            getattr(conn, "project_id", "") or ""
        ),
        "routing_fingerprint": routing_fingerprint(
            connection_type=getattr(conn, "connection_type", None),
            credentials=credentials,
            config=config,
            target_config=None,
            resolved_endpoint=resolved_endpoint,
        ),
    }


async def capture_source_build_binding(
    model_id: Any,
    conn: Any,
    *,
    tenant_session: Any = None,
    system_session: Any = None,
) -> ArtifactSourceBuildBinding:
    """Freeze the resolved source identity before the first row is read."""
    return ArtifactSourceBuildBinding.from_dict(
        await resolve_source_binding_dict(
            model_id,
            conn,
            tenant_session=tenant_session,
            system_session=system_session,
        )
    )


async def current_source_build_binding(
    db: AsyncSession,
    model_id: Any,
    *,
    lock_for_finalization: bool = False,
    system_session: Any = None,
) -> ArtifactSourceBuildBinding | None:
    """Read the model's current source routing identity from committed state.

    Returns ``None`` — never a partially-resolved binding — when the model has
    no source connection or its tables span more than one. Those are precisely
    the states ``resolve_source_connection`` refuses to build from, so a
    recorded binding must not compare equal to them.

    ``lock_for_finalization`` locks the ``DataSource`` pointer rows and the
    ``ProjectConnection`` row until the caller commits, closing the
    comparison-to-activation race exactly as the target side does: a re-point
    that committed first is observed here; one that starts later waits and then
    invalidates the completed aggregate.

    Autoflush is suppressed for the same reason as
    :func:`current_target_build_binding` — a completion path holds dirty
    aggregate metadata, and flushing it before taking these control-plane locks
    can deadlock against a connection edit that already owns the connection row.
    """
    from shared.aggregate_connection import source_connection_ids_for_model
    from shared.db.models import ProjectConnection

    with db.no_autoflush:
        conn_ids = await source_connection_ids_for_model(
            model_id, db, lock_for_update=lock_for_finalization
        )
        if len(conn_ids) != 1:
            return None

        conn_stmt = select(
            ProjectConnection.id,
            ProjectConnection.project_id,
            ProjectConnection.connection_type,
            ProjectConnection.encrypted_credentials,
            ProjectConnection.config,
        ).where(ProjectConnection.id == conn_ids[0])
        if lock_for_finalization:
            conn_stmt = conn_stmt.with_for_update()
        conn_row = (await db.execute(conn_stmt)).one_or_none()
        if conn_row is None:
            return None

        return await capture_source_build_binding(
            model_id,
            _ConnectionRoutingState(*conn_row),
            tenant_session=db,
            system_session=system_session,
        )


def _as_model_uuid(model_id: Any) -> UUID | None:
    """Coerce an artifact's ``model_id`` to ``UUID`` for the advisory-lock key.

    ``model_advisory_lock_key`` needs ``.int``, and these call sites source the
    id from an ORM attribute that a driver may hand back as a string, so the
    string form is coerced rather than skipped.

    A value that is not a UUID at all cannot name a row in ``models``: the column
    is ``UUID(as_uuid=True)``, so no real model — and therefore no real
    ``delete_model_cascade`` — can be on the other side of the lock. Returning
    ``None`` (logged at ERROR, never silently) is preferred to raising, because
    raising would convert a placeholder identifier into an aborted refresh. The
    invariant that PRODUCTION call sites always supply a model id is held by
    ``test_every_finalisation_call_site_passes_a_model_id``, not by this coercion.
    """
    if isinstance(model_id, UUID):
        return model_id
    try:
        return UUID(str(model_id))
    except (AttributeError, TypeError, ValueError):
        logger.error(
            "CR-1: finalisation model_id %r is not a UUID, so the per-model "
            "advisory lock cannot be keyed and this finalisation will NOT "
            "serialise against a concurrent model delete. No real model row can "
            "carry this id, but the call site is wrong.",
            model_id,
        )
        return None


async def lock_finalization_rows(
    db: AsyncSession,
    *,
    connection_ids: Any = (),
    target_id: Any = None,
    model_id: Any = None,
) -> None:
    """Take the model's advisory lock, then EVERY control-plane row the
    finalisation will lock.

    The invariant, learned the hard way over two review rounds and proven by a
    live deadlock each time:

        every control-plane row lock a build's finalisation takes must be
        acquired BEFORE the aggregate row is dirtied, and in one fixed order.

    CR-1 (sol T3 review of ``a9711143``) added the missing outer half:

        and the model's per-model advisory lock must be acquired BEFORE any of
        those control-plane rows.

    ``delete_model_cascade`` takes that advisory lock as its first statement and
    then walks its delete steps in the opposite row order — ``aggregate_definitions``
    (``cascade_delete.py``) BEFORE ``data_sources``/``data_targets``. A finalisation
    that locks ``data_targets``/``data_sources`` first and only then dirties the
    aggregate row closes an ABBA cycle with it:

      1. the finalisation locks the model's ``DataTarget`` and ``DataSource``;
      2. a model delete takes the advisory lock — the finalisation never
         participated in it, so nothing serialises the two;
      3. the delete drops the physical tables, then deletes ``aggregate_definitions``;
      4. the delete reaches ``data_sources`` and waits on the finalisation;
      5. the finalisation flushes its dirty ``AggregateDefinition`` and waits on
         the delete;
      6. PostgreSQL aborts one side with ``40P01``.

    If the DELETE loses, its metadata transaction rolls back but the source-side
    ``DROP TABLE`` it already issued does not: the model stays visible with active
    definitions pointing at physical tables that no longer exist. That is why the
    lock lives HERE, in the shared primitive, and not at the call sites — the four
    artifact finalisers (optimizer aggregate creation incl. predictive builds,
    scheduler full refresh, scheduler incremental refresh, and pocket finalisation
    via ``shared/pocket/refresh_guard.read_pocket_finalization_state``) all funnel
    through this one function, exactly as the three cascade callers funnel through
    ``delete_model_cascade``. Bolting the lock onto three of them while a fourth
    exists is the enumeration defect that produced CR-1 in the first place.

    CALLER CONTRACT — no model-owned row lock may be held on entry.
    The advisory lock is acquired here, so any model-owned row this transaction
    already locked was locked BEFORE it: the same inversion, one level up. A
    refresh job that inserted its ``AggregateRefreshRun`` row (an FK ``KEY SHARE``
    on ``aggregate_definitions``) and never committed it would deadlock against a
    cascade that holds the advisory lock and is waiting to delete that very row.
    Both refresh jobs therefore COMMIT the run row unconditionally before the
    physical build, so the finalisation transaction reaches this call clean; the
    optimizer creator has not inserted its aggregate row yet, and the pocket
    refresh commits its ``invalidating`` flip before the build.

    Acquisition is inside ``no_autoflush`` for the same reason the row locks are:
    autoflushing dirty artifact metadata would take the aggregate row lock BEFORE
    the advisory lock and re-create the inversion the call exists to remove.

    Why both halves matter. Each control-plane writer locks ONE control-plane
    table and then UPDATEs the artifacts hanging off it:

    * ``connections.py::update_connection`` -> ``project_connections``, then
      every artifact on it (both directions, Bug-8602);
    * ``targets.py::update_target``         -> ``data_targets``, then its
      artifacts;
    * ``sources.py::update_source``         -> ``data_sources``, then the
      model's artifacts.

    And one writer takes TWO of them in a single transaction — the snapshot
    rehydrator's revert (``_insert_data_sources_and_targets`` with
    ``upsert=True``). That is why the order below is FIXED rather than merely
    "all before the aggregate row": the revert has to agree with it, and it
    does (it upserts ``data_targets`` before ``data_sources``, with a comment
    pointing back here). Round-4 review found the two disagreeing and
    reproduced the deadlock against real PostgreSQL. If a fourth two-family
    writer ever appears, it joins this order too.

    A finalisation that dirties ``agg_def`` first (``write_quantile_coverage``
    flushes unconditionally) and only then asks for one of those rows closes a
    cycle with the matching writer. Hoisting all three above the first
    ``agg_def`` write turns every one of those cycles into a plain wait.

    The three families are locked in a FIXED order — connections (primary-key
    order), then the target, then the model's sources — so two concurrent
    finalisations cannot cycle against each other either. The
    ``lock_for_finalization=True`` re-reads that follow simply re-acquire locks
    this transaction already holds, which is a no-op.

    Called with BUILD-START identifiers. If a re-point moved an endpoint to a
    row outside this set mid-build, that row is locked outside the order — but
    the binding comparison then fails and the build is discarded anyway, so the
    residual is a possible abort on a path that was already non-serving.
    ``None`` and duplicate ids are dropped, so a same-database build takes
    exactly one connection lock.
    """
    from shared.aggregate_connection import source_connection_ids_for_model
    from shared.db.model_lock import acquire_model_definition_lock
    from shared.db.models import DataTarget, ProjectConnection

    with db.no_autoflush:
        # CR-1: the model's advisory lock FIRST — before any control-plane row,
        # before any artifact row. See the docstring: this is the same lock
        # ``delete_model_cascade`` takes as its first statement, and taking it
        # here is what makes a finalisation and a model delete SERIALISE instead
        # of racing each other's row-lock order into a 40P01.
        _lock_key_model = (
            _as_model_uuid(model_id) if model_id is not None else None
        )
        if _lock_key_model is not None:
            await acquire_model_definition_lock(db, _lock_key_model)
        unique = sorted({str(c) for c in (connection_ids or ()) if c is not None})
        if unique:
            await db.execute(
                select(ProjectConnection.id)
                .where(ProjectConnection.id.in_(unique))
                .order_by(ProjectConnection.id)
                .with_for_update()
            )
        if target_id is not None:
            await db.execute(
                select(DataTarget.id)
                .where(DataTarget.id == target_id)
                .with_for_update()
            )
        if model_id is not None:
            # Same statement ``current_source_build_binding`` issues, so this
            # takes exactly the rows that re-proof would take later.
            await source_connection_ids_for_model(
                model_id, db, lock_for_update=True
            )


async def source_build_binding_matches_live(
    db: AsyncSession,
    binding: ArtifactSourceBuildBinding,
    *,
    lock_for_finalization: bool = False,
    system_session: Any = None,
) -> bool:
    """Fail-closed proof that a build still reads the database it was built on."""
    try:
        live = await current_source_build_binding(
            db,
            binding.model_id,
            lock_for_finalization=lock_for_finalization,
            system_session=system_session,
        )
    except Exception:
        logger.warning(
            "Could not re-prove the source binding for model %s; the build "
            "will remain non-serving",
            binding.model_id,
            exc_info=True,
        )
        return False
    return live is not None and binding.matches(live)


def apply_source_build_binding(
    artifact: Any,
    binding: ArtifactSourceBuildBinding,
) -> None:
    """Persist build-start source truth; never substitute a completion read."""
    artifact.built_for_source_binding = binding.to_dict()


async def _invalidate_artifacts(
    db: AsyncSession,
    *,
    pocket_scope: Any,
    aggregate_scope: Any,
    reason: str,
    named_query_scope: Any = None,
) -> tuple[int, int, int]:
    """Apply the invalidation POLICY to a caller-supplied scope predicate.

    One body, so the target-scoped and source-scoped invalidators (Bug-8473 /
    Bug-8602) can never drift on WHAT invalidation means — only on WHICH rows it
    applies to. Every rule below was established by the target-side rounds and
    now binds both sides.

    MUST be called in the SAME transaction as the routing-affecting edit, so a
    reader either sees the old location with servable artifacts or the new one
    with none — never the new location with artifacts built against the old.

    Pockets go ``stale`` and lose their row manifest AND liveness pointer: the
    manifest describes columns of a table on the previous database, so leaving it
    would let the row-security gate prove coverage against the wrong table.
    Named Query artifacts go ``stale`` with the same manifest/pointer clearing
    (they share the same serve-time proof contract). Serving aggregates go
    ``pending``, the established "rebuild me" state used by the
    definition-change invalidators. User-disabled aggregates stay ``disabled``
    while becoming stale; retired aggregates are untouched.

    Returns ``(pockets_invalidated, aggregates_invalidated,
    named_queries_invalidated)``.

    Bug-8994 lock order: aggregate definitions MUST be updated before pocket
    definitions. ``delete_model_cascade`` uses that order, and a connection
    edit can touch both families concurrently with a delete. Reversing the two
    statements creates an ABBA row-lock cycle. Named Query artifacts are
    updated LAST — they are a distinct row family and a new lock participant;
    taking them after aggregates/pockets keeps them out of the pre-existing
    cycles. All public invalidators funnel through this function so the
    invariant is enforced once.
    """
    from shared.db.models import AggregateDefinition, NamedQueryArtifact, PocketDefinition

    # EVERY non-retired aggregate, not only the currently-active ones. Confining
    # this to ``status == "active"`` left three ways back into the serving pool
    # without a rebuild. The aggregate route DOES now carry an execution-time
    # storage re-proof (``query-router/src/routing/aggregate_generation_guard
    # ._binding_matches``, the Bug-8457/Bug-8473 sibling of the pocket guard),
    # but it can only compare against a binding the build RECORDED, so an
    # aggregate built before ``built_for_storage_binding`` existed still relies
    # on this predicate alone:
    #   * an ``invalid`` aggregate (e.g. a deleted join) is skipped, then the
    #     structural revalidation self-heal flips it ``invalid -> active`` with
    #     no rebuild — the ``disabled -> active`` enable path is safe only
    #     because it sets ``is_stale``;
    #   * a ``pending`` mid-refresh aggregate is skipped, and the refresh
    #     pending-guard restores ``active`` when its CTAS — which ran through the
    #     OLD endpoint into the OLD database — completes;
    #   * a brand-new aggregate inserted ``active`` by the optimizer after its
    #     own CTAS.
    # ``is_stale`` is what the matcher actually honours, so set it too: a status
    # transition alone can be undone by any of those paths, ``is_stale`` cannot
    # be cleared except by a real rebuild.
    agg_result = await db.execute(
        update(AggregateDefinition)
        .where(
            aggregate_scope,
            AggregateDefinition.retired_at.is_(None),
            AggregateDefinition.status != "retired",
        )
        .values(
            status=case(
                (AggregateDefinition.status == "disabled", "disabled"),
                else_="pending",
            ),
            is_stale=True,
            active_refresh_run_id=None,
        )
    )
    pocket_result = await db.execute(
        update(PocketDefinition)
        .where(
            pocket_scope,
            PocketDefinition.status != "stale",
            PocketDefinition.retired_at.is_(None),
        )
        .values(
            status="stale",
            failure_reason=reason[:1000],
            row_manifest=None,
            active_refresh_run_id=None,
        )
    )
    nq_result = None
    if named_query_scope is not None:
        nq_result = await db.execute(
            update(NamedQueryArtifact)
            .where(
                named_query_scope,
                NamedQueryArtifact.status != "stale",
                NamedQueryArtifact.retired_at.is_(None),
            )
            .values(
                status="stale",
                failure_reason=reason[:1000],
                row_manifest=None,
                active_refresh_run_id=None,
            )
        )
    return (
        int(pocket_result.rowcount or 0),
        int(agg_result.rowcount or 0),
        int(nq_result.rowcount or 0) if nq_result is not None else 0,
    )


async def invalidate_artifacts_for_target(
    db: AsyncSession, target_id: Any, *, reason: str
) -> tuple[int, int]:
    """Take every artifact WRITTEN TO ``target_id`` out of the serving pool."""
    from shared.db.models import AggregateDefinition, NamedQueryArtifact, PocketDefinition

    pockets, aggregates, nqs = await _invalidate_artifacts(
        db,
        pocket_scope=(PocketDefinition.target_id == target_id),
        aggregate_scope=(AggregateDefinition.target_id == target_id),
        named_query_scope=(NamedQueryArtifact.target_id == target_id),
        reason=reason,
    )
    if pockets or aggregates or nqs:
        logger.warning(
            "Bug-8473: invalidated %d pocket(s), %d aggregate(s) and %d "
            "named query artifact(s) on target %s — %s",
            pockets, aggregates, nqs, target_id, reason,
        )
    return pockets, aggregates


async def invalidate_artifacts_for_source_connection(
    db: AsyncSession, connection_id: Any, *, reason: str
) -> tuple[int, int]:
    """Take every artifact BUILT FROM ``connection_id`` out of the serving pool.

    The Bug-8602 root-cause half. An artifact's rows were materialised from
    whichever database its model's ``DataSource`` rows addressed at build time;
    editing that connection's endpoint means those rows came from a database the
    model no longer reads, so they must not keep serving. This is independent of
    the target: a cross-database aggregate (source A, target B) has NO
    ``DataTarget`` on A, which is precisely why the target-scoped invalidator
    could never see it.

    Both artifact kinds are in scope — ``shared/pocket/refresh.py`` resolves its
    source through the same ``resolve_source_connection``, so a pocket has the
    identical exposure.
    """
    from shared.aggregate_connection import model_ids_for_source_connection
    from shared.db.models import (
        AggregateDefinition,
        NamedQuery,
        NamedQueryArtifact,
        PocketDefinition,
    )

    model_ids = await model_ids_for_source_connection(connection_id, db)
    if not model_ids:
        return 0, 0

    pockets, aggregates, nqs = await _invalidate_artifacts(
        db,
        pocket_scope=PocketDefinition.model_id.in_(model_ids),
        aggregate_scope=AggregateDefinition.model_id.in_(model_ids),
        named_query_scope=NamedQueryArtifact.named_query_id.in_(
            select(NamedQuery.id).where(NamedQuery.model_id.in_(model_ids))
        ),
        reason=reason,
    )
    if pockets or aggregates or nqs:
        logger.warning(
            "Bug-8602: invalidated %d pocket(s), %d aggregate(s) and %d "
            "named query artifact(s) built FROM connection %s (models %s) — %s",
            pockets, aggregates, nqs, connection_id,
            ", ".join(str(m) for m in model_ids), reason,
        )
    return pockets, aggregates


async def invalidate_artifacts_for_model(
    db: AsyncSession, model_id: Any, *, reason: str, also_models: Any = None
) -> tuple[int, int]:
    """Take every artifact BUILT FROM ``model_id``'s sources out of the pool.

    Bug-8602's OTHER lever. A model's source database moves in two independent
    ways, and they have two different write paths:

    * the ``ProjectConnection`` endpoint is edited — keyed on the connection,
      handled by :func:`invalidate_artifacts_for_source_connection`;
    * ``DataSource.project_connection_id`` is re-pointed at a DIFFERENT
      connection — the ``ProjectConnection`` rows are untouched, so a
      connection-keyed invalidator structurally cannot see it. That is this
      function, and it is scoped on the MODEL because the pointer belongs to
      the model.

    Nothing else covers the second lever. The definition closure DOES compare
    ``data_sources``, but it is a BUILD-WINDOW mechanism: its only consumers are
    the three aggregate writers, so it refuses the next rebuild while saying
    nothing about the artifact already serving. The serve-time source binding
    catches it, but only for an artifact that HAS a recorded binding — which
    excludes every aggregate built before that column existed, and every pocket
    (Bug-8780). So this control-plane half is what protects them.

    Writer enumeration (round-3 review): ``DataSource.project_connection_id``
    has TWO writers, not one. ``sources.py::update_source`` calls this
    function. ``shared/model_snapshot/rehydrator.py`` also writes the column,
    by upserting every ``data_sources`` field from the snapshot on a REVERT,
    and it does not call this function — a revert past a source re-point is
    instead covered inside the rehydrator itself, which unconditionally stales
    every preserved aggregate (``_validate_preserved_aggregates``, Bug-7146)
    and every preserved pocket (``_validate_preserved_pockets``, Bug-8602).
    Do not assume this function is the only cover for the re-point lever.

    ``also_models`` exists so a caller that knows a WIDER blast radius can pass
    it. ``model_ids_for_source_connection`` deliberately enumerates through
    ``ModelTable.source_id`` rather than ``DataSource.model_id``, because a
    table can reference another model's DataSource (legacy/imported rows) and
    that IS a real read. There is no composite FK preventing that state, so a
    re-point handler scoping only on the DataSource's OWNING model would leave
    a borrowing model's artifacts serving from the previous database. Round-2
    review finding 3: align the two enumerations rather than let one assert a
    case the other treats as impossible.
    """
    from shared.db.models import AggregateDefinition, NamedQuery, NamedQueryArtifact, PocketDefinition

    model_ids = {model_id, *(also_models or ())}
    pockets, aggregates, nqs = await _invalidate_artifacts(
        db,
        pocket_scope=PocketDefinition.model_id.in_(model_ids),
        aggregate_scope=AggregateDefinition.model_id.in_(model_ids),
        named_query_scope=NamedQueryArtifact.named_query_id.in_(
            select(NamedQuery.id).where(NamedQuery.model_id.in_(model_ids))
        ),
        reason=reason,
    )
    if pockets or aggregates or nqs:
        logger.warning(
            "Bug-8602: invalidated %d pocket(s), %d aggregate(s) and %d "
            "named query artifact(s) on model(s) %s after a source re-point — %s",
            pockets, aggregates, nqs,
            ", ".join(sorted(str(m) for m in model_ids)), reason,
        )
    return pockets, aggregates


async def invalidate_artifacts_for_connection(
    db: AsyncSession, connection_id: Any, *, reason: str
) -> tuple[int, int]:
    """Every artifact a ``connection_id`` edit re-points — BOTH sides.

    A connection can be an artifact's target, its source, or (the same-database
    case) both. The control plane has exactly ONE invalidation call site for a
    connection edit, so this function must cover both directions itself: adding
    a second function for callers to remember is how the source side stayed
    uncovered through the whole of Bug-8473 (Bug-8602).

    The two scopes overlap for a same-database artifact (source and target on
    one connection). Re-invalidating an already-invalidated row is idempotent —
    both passes write the same terminal values — so correctness is unaffected,
    but the returned counts are a SUM OF PER-PASS ROWCOUNTS and therefore
    over-report distinct rows in that case. That is acceptable because the
    return is telemetry: the only caller (``connections.py``) logs it and never
    branches on it. Do not start using it as a row count.
    """
    from shared.db.models import DataTarget

    target_ids = (
        await db.execute(
            select(DataTarget.id).where(
                DataTarget.project_connection_id == connection_id
            )
        )
    ).scalars().all()
    pockets = aggregates = 0
    for target_id in target_ids:
        p, a = await invalidate_artifacts_for_target(db, target_id, reason=reason)
        pockets += p
        aggregates += a

    p, a = await invalidate_artifacts_for_source_connection(
        db, connection_id, reason=reason
    )
    return pockets + p, aggregates + a
