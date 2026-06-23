"""External catalog import — one-time metadata retrieve from DataHub,
OpenMetadata, or Alation into a Tessallite model.

POST /projects/{project_id}/import/catalog
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import uuid as _uuid
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpcore
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from shared.auth.middleware import CurrentUser, require_tenant_admin
from shared.db.models import Model, Project, ProjectConnection
from shared.db.session import get_tenant_db
from shared.model_snapshot.importer import prepare_snapshot_for_import
from shared.model_snapshot.rehydrator import rehydrate_into_live

logger = logging.getLogger(__name__)
router = APIRouter()

_BLOCKED_HOSTS = {
    "localhost", "metadata.google.internal", "metadata.google", "metadata",
}


def _validate_catalog_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Catalog URL must use http(s), got {parsed.scheme!r}")
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("Catalog URL has no hostname")
    if hostname.lower().rstrip(".") in _BLOCKED_HOSTS:
        raise ValueError(f"Catalog URL host {hostname!r} is not allowed")
    return url


class _SSRFSafeBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        from httpcore._backends.anyio import AnyIOBackend
        self._inner = AnyIOBackend()

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None,
    ):
        try:
            infos = socket.getaddrinfo(str(host), port, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise httpcore.ConnectError(f"SSRF: host {host!r} does not resolve")
        validated_ip: str | None = None
        for _fam, _type, _proto, _canon, sockaddr in infos:
            addr = ipaddress.ip_address(sockaddr[0])
            if not addr.is_global or addr.is_multicast:
                raise httpcore.ConnectError(
                    f"SSRF blocked: {host!r} resolves to non-global address {addr}"
                )
            if validated_ip is None:
                validated_ip = sockaddr[0]
        if validated_ip is None:
            raise httpcore.ConnectError(f"SSRF: host {host!r} has no usable address")
        return await self._inner.connect_tcp(
            validated_ip, port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix socket connections not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _ssrf_safe_client(timeout: float = 60.0) -> httpx.AsyncClient:
    pool = httpcore.AsyncConnectionPool(network_backend=_SSRFSafeBackend())
    transport = httpx.AsyncHTTPTransport()
    transport._pool = pool  # type: ignore[attr-defined]
    return httpx.AsyncClient(timeout=timeout, transport=transport)


class CatalogImportRequest(BaseModel):
    catalog_type: str  # "datahub" | "openmetadata" | "alation"
    api_url: str
    api_token: str
    dataset_filter: str | None = None
    model_name: str | None = None


class CatalogImportResponse(BaseModel):
    models_created: int
    model_names: list[str]
    tables_imported: int
    dimensions_imported: int
    measures_imported: int
    warnings: list[str]


async def _fetch_datahub(
    api_url: str, api_token: str, dataset_filter: str | None,
) -> tuple[list[dict], list[str]]:
    """Fetch dataset metadata from DataHub's GraphQL API."""
    warnings: list[str] = []
    headers = {"Authorization": f"Bearer {api_token}"}
    # F-020-20: pass the dataset filter as a GraphQL variable, not via string
    # interpolation. Quotes/braces in the filter previously corrupted the query.
    query = """
    query Search($q: String!) {
      search(input: {type: DATASET, query: $q, start: 0, count: 200}) {
        searchResults {
          entity {
            ... on Dataset {
              urn
              name
              platform { name }
              editableSchemaMetadata { editableSchemaFieldInfo { fieldPath description } }
              schemaMetadata {
                fields { fieldPath nativeDataType description }
              }
              editableProperties { description }
              tags { tags { tag { name } } }
            }
          }
        }
      }
    }
    """

    tables: list[dict] = []
    _validate_catalog_url(api_url)
    async with _ssrf_safe_client() as client:
        resp = await client.post(
            f"{api_url.rstrip('/')}/api/graphql",
            json={"query": query, "variables": {"q": dataset_filter or "*"}},
            headers=headers,
        )
    if not resp.is_success:
        raise HTTPException(status_code=502, detail=f"DataHub returned {resp.status_code}")

    data = resp.json()
    results = (
        data.get("data", {}).get("search", {}).get("searchResults", [])
    )
    for r in results:
        entity = r.get("entity", {})
        fields = []
        schema = entity.get("schemaMetadata") or {}
        for f in schema.get("fields", []):
            fields.append({
                "name": f.get("fieldPath", ""),
                "data_type": f.get("nativeDataType", "string"),
                "description": f.get("description", ""),
            })
        tables.append({
            "name": entity.get("name", ""),
            "description": (entity.get("editableProperties") or {}).get(
                "description", ""
            ),
            "platform": (entity.get("platform") or {}).get("name", ""),
            "fields": fields,
        })
    return tables, warnings


async def _fetch_openmetadata(
    api_url: str, api_token: str, dataset_filter: str | None,
) -> tuple[list[dict], list[str]]:
    """Fetch table metadata from OpenMetadata's REST API."""
    warnings: list[str] = []
    headers = {"Authorization": f"Bearer {api_token}"}
    tables: list[dict] = []

    _validate_catalog_url(api_url)
    async with _ssrf_safe_client() as client:
        params: dict[str, Any] = {"limit": 200}
        if dataset_filter:
            params["database"] = dataset_filter
        resp = await client.get(
            f"{api_url.rstrip('/')}/api/v1/tables",
            params=params,
            headers=headers,
        )
    if not resp.is_success:
        raise HTTPException(
            status_code=502, detail=f"OpenMetadata returned {resp.status_code}"
        )

    for tbl in resp.json().get("data", []):
        fields = []
        for col in tbl.get("columns", []):
            fields.append({
                "name": col.get("name", ""),
                "data_type": col.get("dataType", "string"),
                "description": col.get("description", ""),
            })
        tables.append({
            "name": tbl.get("name", ""),
            "description": tbl.get("description", ""),
            "platform": tbl.get("serviceType", ""),
            "fields": fields,
        })
    return tables, warnings


async def _fetch_alation(
    api_url: str, api_token: str, dataset_filter: str | None,
) -> tuple[list[dict], list[str]]:
    """Fetch table metadata from Alation's REST API."""
    warnings: list[str] = []
    headers = {"Token": api_token}
    tables: list[dict] = []

    params: dict[str, Any] = {"limit": 200}
    if dataset_filter:
        params["name__icontains"] = dataset_filter

    _validate_catalog_url(api_url)
    async with _ssrf_safe_client() as client:
        resp = await client.get(
            f"{api_url.rstrip('/')}/integration/v2/table/",
            params=params,
            headers=headers,
        )
    if not resp.is_success:
        raise HTTPException(
            status_code=502, detail=f"Alation returned {resp.status_code}"
        )

    for tbl in resp.json():
        fields = []
        for col in tbl.get("columns", []):
            fields.append({
                "name": col.get("name", ""),
                "data_type": col.get("column_type", "string"),
                "description": col.get("description", ""),
            })
        tables.append({
            "name": tbl.get("name", ""),
            "description": tbl.get("description", ""),
            "platform": tbl.get("ds_type", ""),
            "fields": fields,
        })
    return tables, warnings


_FETCHERS = {
    "datahub": _fetch_datahub,
    "openmetadata": _fetch_openmetadata,
    "alation": _fetch_alation,
}


def _catalog_to_bundle(
    tables: list[dict], model_id: str, model_slug: str, model_display_name: str,
) -> tuple[dict[str, Any], int, int, int]:
    """Convert fetched catalog tables into a per-model snapshot (v2).

    F-020-04: the bundle must match the contract ``rehydrate_into_live``
    consumes — ``schema_version`` on the snapshot, a ``data_sources`` entry
    (placeholder source, ``project_connection_id`` injected by the endpoint),
    and ``data_type``-normalised columns. The previous shape used a
    ``"sources"`` key (never read) and omitted ``schema_version``, so every
    call raised ``SnapshotSchemaError`` → HTTP 500.
    """
    source_id = str(_uuid.uuid4())
    bundle_tables = []
    bundle_columns = []
    bundle_dimensions = []
    bundle_measures = []

    numeric_types = {
        "int", "integer", "bigint", "smallint", "tinyint", "float",
        "double", "decimal", "numeric", "number", "real", "int64",
        "float64", "int32", "float32",
    }

    for tbl in tables:
        table_id = str(_uuid.uuid4())
        alias = _slugify(tbl["name"])
        bundle_tables.append({
            "id": table_id,
            "model_id": model_id,
            "source_id": source_id,
            "physical_name": tbl["name"],
            "alias": alias,
            "display_name": tbl["name"],
            "description": tbl.get("description", "") or None,
            # F-020-23: default to a documented table_type (fact | dim_aggregate
            # | dim_detail) rather than the undocumented "unclassified" value.
            "table_type": "dim_detail",
        })

        for field in tbl.get("fields", []):
            col_id = str(_uuid.uuid4())
            raw_type = (field.get("data_type") or "string").lower()
            base_type = raw_type.split("(")[0].strip()
            is_numeric = base_type in numeric_types
            bundle_columns.append({
                "id": col_id,
                "model_table_id": table_id,
                "column_name": field["name"],
                "data_type": "numeric" if is_numeric else "string",
            })

            if is_numeric:
                bundle_measures.append({
                    "id": str(_uuid.uuid4()),
                    "model_id": model_id,
                    "name": f"{alias}_{field['name']}",
                    "display_name": field["name"].replace("_", " ").title(),
                    "description": field.get("description", "") or None,
                    "source_column_id": col_id,
                    "default_agg": "sum",
                    "measure_type": "standard",
                    "semi_additive_behavior": None,
                })
            else:
                bundle_dimensions.append({
                    "id": str(_uuid.uuid4()),
                    "model_id": model_id,
                    "name": f"{alias}_{field['name']}",
                    "display_name": field["name"].replace("_", " ").title(),
                    "description": field.get("description", "") or None,
                    "source_column_id": col_id,
                    "is_time_dim": False,
                })

    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "model_id": model_id,
        "model": {
            "id": model_id,
            "slug": model_slug,
            "display_name": model_display_name,
            "description": None,
            "refresh_strategy": "manual",
            "max_aggregates": 20,
            "aggregations_enabled": True,
            "include_all_measures": True,
        },
        "tables": bundle_tables,
        "columns": bundle_columns,
        "joins": [],
        "dimensions": bundle_dimensions,
        "measures": bundle_measures,
        "hierarchies": [],
        "personas": [{
            "id": str(_uuid.uuid4()),
            "model_id": model_id,
            "slug": "everyone",
            "name": "Everyone",
            "description": "Default persona — full access",
        }],
        "user_defined_attributes": [],
        "uda_column_refs": [],
        "aggregates": [],
        "data_sources": [{
            "id": source_id,
            "model_id": model_id,
            "source_type": "import_placeholder",
            "display_name": "Catalog Import Source",
            "config": {},
        }],
        "data_targets": [],
    }

    bundle: dict[str, Any] = {
        "schema_version": 1,
        "export_format": "tessallite-project/v1",
        "models": [snapshot],
    }
    return bundle, len(bundle_tables), len(bundle_dimensions), len(bundle_measures)


def _slugify(name: str) -> str:
    """Slugify and bound to the 64-char column (F-020-20)."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower().strip()).strip("_")
    return (slug or "catalog_model")[:64]


def _slug_with_headroom(slug: str, n: int) -> str:
    """Reserve headroom for the ``_{n}`` collision suffix (F-020-19).

    The slug column is ``String(64)``; a 64-char slug plus ``_{n}`` would
    overflow. Truncate the base so the final ``slug_n`` fits in 64 chars.
    """
    suffix_len = len(f"_{n}")
    return slug[: 64 - suffix_len]


@router.post(
    "/projects/{project_id}/import/catalog",
    response_model=CatalogImportResponse,
    tags=["catalog-import"],
)
async def import_from_catalog(
    project_id: UUID,
    body: CatalogImportRequest,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> CatalogImportResponse:
    fetcher = _FETCHERS.get(body.catalog_type)
    if not fetcher:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported catalog type: {body.catalog_type}. "
            f"Supported: {', '.join(_FETCHERS)}",
        )

    # Bug-5268: translate URL validation errors and network failures into
    # user-facing 4xx/502 responses instead of letting them propagate as 500s.
    try:
        tables, warnings = await fetcher(body.api_url, body.api_token, body.dataset_filter)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid catalog URL: {exc}",
        ) from exc
    except httpcore.ConnectError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not connect to catalog: {exc}",
        ) from exc
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not connect to catalog: {exc}",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Catalog request timed out: {exc}",
        ) from exc
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Catalog request failed: {exc}",
        ) from exc
    if not tables:
        return CatalogImportResponse(
            models_created=0,
            model_names=[],
            tables_imported=0,
            dimensions_imported=0,
            measures_imported=0,
            warnings=warnings + ["No tables found in catalog"],
        )

    model_slug = _slugify(body.model_name or f"catalog-{body.catalog_type}")
    model_display = body.model_name or f"Catalog Import ({body.catalog_type.title()})"

    model_id = str(_uuid.uuid4())
    bundle, tbl_count, dim_count, meas_count = _catalog_to_bundle(
        tables, model_id, model_slug, model_display,
    )

    async for db in get_tenant_db(current_user.tenant_id):
        project = await db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        # Bind the placeholder data source to a project connection. Reuse the
        # first existing connection; create a placeholder if the project has
        # none (mirrors the dbt/cube importer pattern). Without this the
        # NOT NULL data_sources.project_connection_id FK would violate.
        conn_q = await db.execute(
            select(ProjectConnection.id)
            .where(ProjectConnection.project_id == project_id)
            .limit(1)
        )
        conn_row = conn_q.first()
        if conn_row is not None:
            default_conn_id = str(conn_row[0])
        else:
            from shared.security.credential_crypto import encrypt_json
            placeholder_conn = ProjectConnection(
                project_id=project_id,
                display_name="(imported — configure me)",
                connection_type="postgresql",
                encrypted_credentials=encrypt_json({}),
                config={},
            )
            db.add(placeholder_conn)
            await db.flush()
            default_conn_id = str(placeholder_conn.id)
            warnings = warnings + [
                "No project connection found — created a placeholder. "
                "Configure it with real credentials before querying."
            ]

        model_snap = bundle["models"][0]
        slug = model_snap["model"]["slug"]

        for ds in model_snap.get("data_sources", []):
            if not ds.get("project_connection_id"):
                ds["project_connection_id"] = default_conn_id

        # Bug-5266: slug allocation is check-then-insert which is racy under
        # concurrent imports. Use an optimistic approach: pick the best
        # candidate, try to flush, and retry with a bumped suffix on
        # unique-constraint violation (up to a reasonable bound).
        _MAX_SLUG_RETRIES = 5
        existing_q = await db.execute(
            select(Model.slug).where(Model.project_id == project_id)
        )
        existing_slugs = {r[0] for r in existing_q.all()}
        candidate = slug
        n = 2
        while candidate in existing_slugs:
            candidate = f"{_slug_with_headroom(slug, n)}_{n}"
            n += 1

        for _retry in range(_MAX_SLUG_RETRIES):
            new_model_id = _uuid.uuid4()
            rewritten, _missing = prepare_snapshot_for_import(
                model_snap, new_model_id=new_model_id,
            )
            rewritten.setdefault("model", {})
            rewritten["model"]["slug"] = candidate
            rewritten["model"]["display_name"] = model_display

            new_model = Model(
                id=new_model_id,
                project_id=project_id,
                slug=candidate,
                display_name=model_display,
                seed=str(_uuid.uuid4()),
            )
            db.add(new_model)
            # Bug-5266: wrap in a SAVEPOINT so an IntegrityError on the
            # model flush rolls back only the nested block, preserving the
            # placeholder ProjectConnection flushed earlier.  A bare
            # db.rollback() would discard the entire transaction including
            # default_conn_id, leaving dangling FK references on retry.
            try:
                async with db.begin_nested():
                    await db.flush()
            except IntegrityError:
                candidate = f"{_slug_with_headroom(slug, n)}_{n}"
                n += 1
                continue

            await rehydrate_into_live(
                new_model_id,
                rewritten,
                db,
                drop_orphan_aggregates=False,
                actor=current_user.email or current_user.user_id,
                force_aggregate_pending=True,
                force_pocket_stale=True,
                preserve_destination_seed=True,
            )
            await db.commit()

            return CatalogImportResponse(
                models_created=1,
                model_names=[candidate],
                tables_imported=tbl_count,
                dimensions_imported=dim_count,
                measures_imported=meas_count,
                warnings=warnings,
            )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not allocate a unique model slug after retries",
        )
