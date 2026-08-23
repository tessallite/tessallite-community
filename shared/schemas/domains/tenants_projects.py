"""Auto-split from pydantic_models.py — Tenant (system DB), Project, Project Connection (credential pool)"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..measure_formats import (
    HIERARCHY_TIME_CALCS as _HIERARCHY_TIME_CALCS,
    HIERARCHY_TIME_UNITS as _HIERARCHY_TIME_UNITS,
    MEASURE_FORMAT_TOKENS as _MEASURE_FORMAT_TOKENS,
    TIME_VARIANT_NAMES as _TIME_VARIANT_NAMES,
)

from ._base import OrmBase

# ---------------------------------------------------------------------------
# Tenant (system DB)
# ---------------------------------------------------------------------------

class TenantCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: str = Field(max_length=255)
    database_url: Optional[str] = Field(None, description="Optional PG URL; auto-derived from system DB if omitted.")


class TenantUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None


class TenantResponse(OrmBase):
    id: uuid.UUID
    slug: str
    display_name: str
    db_schema_prefix: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: Optional[str] = Field(default=None, max_length=255)
    pocket_size_budget_bytes: Optional[int] = Field(
        default=None, ge=0,
        description="Project-level byte ceiling for pocket tables. NULL = unlimited.",
    )


class ProjectUpdate(BaseModel):
    slug: Optional[str] = Field(None, pattern=r"^[a-z0-9_-]+$", max_length=64)
    display_name: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None
    pocket_size_budget_bytes: Optional[int] = Field(
        default=None, ge=0,
        description="Project-level byte ceiling for pocket tables. NULL = unlimited.",
    )


class ProjectResponse(OrmBase):
    id: uuid.UUID
    slug: str
    display_name: str
    is_active: bool
    pocket_size_budget_bytes: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Project Connection (credential pool)
# ---------------------------------------------------------------------------

_ALLOWED_CONNECTION_TYPES = ("bigquery", "postgresql", "hadoop_spark", "redshift", "snowflake", "sqlserver")

# F-014-01 (Bug-7983): ``config`` is a non-secret bag (host/schema/dataset/flags)
# that is stored as plaintext JSONB and returned verbatim to modelers. A secret
# accidentally or programmatically placed here bypasses Fernet encryption and is
# exfiltrated to lower-privileged users. Reject secret-like keys server-side on
# every write path (create/update/test) so credentials can only ever live in the
# encrypted ``credentials`` blob. Kept in sync with ``connections._SENSITIVE_KEYS``.
# Exact key names that are always secret.
_SENSITIVE_CONFIG_KEYS_EXACT = frozenset({
    "password",
    "service_account_json",
    "secret",
    "token",
    "api_key",
    "private_key",
    "client_secret",
    "refresh_token",
    "access_token",
    "credentials",
})

# Fable FINDING-3: substring stems that catch compound names like
# ``db_password``, ``aws_secret_access_key``, ``bearer_token``, ``pwd``,
# ``passphrase``, ``keyfile_json``, ``connection_string``, ``dsn``.
_SENSITIVE_CONFIG_KEY_STEMS = (
    "password", "passwd", "pwd", "secret", "token", "api_key",
    "private_key", "credential", "passphrase", "keyfile",
    # Fable R2 FINDING-3: connection strings embed credentials inline
    # (``postgres://user:PASS@host/db``, ``...;PWD=...``).
    "connection_string", "conn_str", "dsn", "jdbc_url",
)

# Public alias kept for backwards compatibility (Opus/Fable sync checks).
_SENSITIVE_CONFIG_KEYS = _SENSITIVE_CONFIG_KEYS_EXACT


def _is_sensitive_key(key: str) -> bool:
    """Return True if *key* looks like it holds a secret.

    Checks exact membership first, then substring/stem matching so compound
    names (``db_password``, ``bearer_token``, ``aws_secret_access_key``) are
    also caught.
    """
    lower = str(key).lower().strip()
    if lower in _SENSITIVE_CONFIG_KEYS_EXACT:
        return True
    return any(stem in lower for stem in _SENSITIVE_CONFIG_KEY_STEMS)


# ---------------------------------------------------------------------------
# Credentials preview (Bug-6215)
# ---------------------------------------------------------------------------
# The connection edit dialog pre-fills its NON-secret fields from a preview of
# the decrypted credential blob. That preview used to be produced by removing
# secret-like KEY NAMES from whatever the blob happened to contain. A key-name
# denylist over an open key space is structurally unable to guarantee that no
# secret escapes: the secret material may sit in a VALUE under a perfectly
# innocuous key -- a PEM block or a whole BigQuery service-account JSON stored
# as a string under, say, ``gcp_sa`` or ``json``. No addition to the denylist
# can close that, because the attacker (or a careless integration) picks the
# key name.
#
# The preview is therefore an ALLOWLIST. Only keys the connectors actually read
# as non-secret connection coordinates are ever echoed back, and even those are
# constrained to short scalars that carry no secret markers. Everything else is
# dropped. Keep this list in sync with the ``credentials``-group entries of
# ``frontend/src/components/connectionFields.ts`` (guarded by
# ``tests/unit/test_credential_preview_allowlist.py``).
_CREDENTIAL_PREVIEW_ALLOWED_KEYS = frozenset({
    "account",        # snowflake
    "auth_method",    # hadoop_spark
    "database",
    "dbname",
    "driver",         # sqlserver ODBC driver name
    "host",
    "location",       # bigquery
    "port",
    "project_id",     # bigquery
    "region",
    "role",           # snowflake
    "schema",
    "user",
    "username",
    "warehouse",      # snowflake
})

# No legitimate allowlisted coordinate is long. A PEM key, an SA JSON, or a
# base64 blob is. This is a second, key-name-independent barrier.
_CREDENTIAL_PREVIEW_MAX_VALUE_LEN = 256

# Markers that identify secret material inside a VALUE regardless of its key.
_SECRET_VALUE_MARKERS = (
    "-----begin",                 # any PEM block (PRIVATE KEY, RSA, ENCRYPTED)
    "private_key",                # serialised service-account / key JSON
    '"type": "service_account"',
    '"type":"service_account"',
)


def _looks_like_secret_material(value: str) -> bool:
    """Return True if *value* carries key/credential material of any shape."""
    lowered = value.lower()
    return any(marker in lowered for marker in _SECRET_VALUE_MARKERS)


def credential_preview(raw: Any) -> dict[str, Any]:
    """Return the non-secret subset of a decrypted credential blob (Bug-6215).

    Allowlist first (unknown keys are dropped, not inspected), then scalars
    only (a nested dict or list is never previewed), then a length cap, then a
    value-content scan. A private key cannot satisfy all four.
    """
    if not isinstance(raw, dict):
        return {}

    preview: dict[str, Any] = {}
    for key, value in raw.items():
        normalised = str(key).lower().strip()
        if normalised not in _CREDENTIAL_PREVIEW_ALLOWED_KEYS:
            continue
        # Belt: the allowlist must never overlap the secret-key policy. If a
        # future edit adds a secret-like name to the allowlist, this drops it.
        if _is_sensitive_key(normalised):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            preview[key] = value
            continue
        if not isinstance(value, str):
            # dict / list / bytes: no allowlisted coordinate is structured.
            continue
        if len(value) > _CREDENTIAL_PREVIEW_MAX_VALUE_LEN:
            continue
        if _looks_like_secret_material(value):
            continue
        preview[key] = value
    return preview


def _find_sensitive_config_keys(value: Any) -> list[str]:
    """Return any secret-like key names found anywhere in a config mapping.

    Recurses nested dicts/lists so a secret buried under a nested object is
    still caught. Uses both exact and substring matching.
    """
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if _is_sensitive_key(k):
                    found.append(str(k))
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(value)
    return found


def _strip_sensitive_keys(value: Any) -> Any:
    """Recursively strip secret-like keys from a config mapping.

    Used by the import path (Fable FINDING-2) to sanitise imported bundles
    rather than rejecting the entire import.
    """
    if isinstance(value, dict):
        return {
            k: _strip_sensitive_keys(v)
            for k, v in value.items()
            if not _is_sensitive_key(k)
        }
    if isinstance(value, list):
        return [_strip_sensitive_keys(item) for item in value]
    return value


# Placeholder substituted for a config value that carries key material. The key
# name is still shown so the operator can see WHICH field needs cleaning.
REDACTED_CONFIG_VALUE = "__REDACTED__"


def redact_config_bag(value: Any) -> Any:
    """Recursively strip secret-like KEYS and redact secret-looking VALUES.

    The response-side belt for any plaintext ``config`` JSONB bag (connections,
    LLM provider configs). Two independent barriers, because each covers the
    other's structural gap:

    * ``_is_sensitive_key`` (denylist, substring stems) catches ``password``,
      ``db_password``, ``bearer_token``, ``dsn``, ...  A denylist is the only
      option here: a config bag is an OPEN key space (the connection dialog
      re-emits unknown keys as passthrough on save), so an allowlist would
      silently drop legitimate config.
    * ``_looks_like_secret_material`` (value scan) catches a PEM block or a
      service-account JSON parked under an innocuous key such as ``notes`` --
      the case no key-name denylist can ever close (Bug-6215 R2 finding 12).

    Used by every read path that echoes a stored config bag, so pre-gate rows
    written before the write-side validator existed still cannot leak.
    """
    if isinstance(value, dict):
        return {
            k: redact_config_bag(v)
            for k, v in value.items()
            if not _is_sensitive_key(k)
        }
    if isinstance(value, list):
        return [redact_config_bag(v) for v in value]
    if isinstance(value, str) and _looks_like_secret_material(value):
        return REDACTED_CONFIG_VALUE
    return value


def _validate_non_sensitive_config(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return value
    offending = _find_sensitive_config_keys(value)
    if offending:
        raise ValueError(
            "config must not contain secret-like keys "
            f"({', '.join(sorted(set(offending)))}). Put all credentials in the "
            "encrypted 'credentials' field; 'config' is stored in plaintext and "
            "returned to modelers."
        )
    return value


def _validate_connection_type(value: str) -> str:
    """Phase C of the code-review remediation: writes must use the canonical
    ``hadoop_spark`` value; the legacy ``jdbc`` label was historically used
    for Spark/Hive but collided with PostgreSQL-over-JDBC, so it is now
    rejected on create/update paths. Existing rows with ``jdbc`` keep working
    because the read path normalises them — see ``_run_connection_test`` and
    ``query-router/_execute_sql``.
    """
    if value == "jdbc":
        raise ValueError(
            "connection_type 'jdbc' is no longer supported. Use 'hadoop_spark' "
            "for Spark/Hive Thrift connections, or 'postgresql' for PostgreSQL."
        )
    if value not in _ALLOWED_CONNECTION_TYPES:
        raise ValueError(
            f"connection_type must be one of {_ALLOWED_CONNECTION_TYPES}, got {value!r}."
        )
    return value


class ConnectionCreate(BaseModel):
    display_name: str = Field(max_length=255)
    connection_type: str = Field(description="bigquery | postgresql | hadoop_spark | redshift | snowflake | sqlserver")
    credentials: dict[str, Any] = Field(description="Raw credentials -- encrypted at rest, never returned")
    config: dict[str, Any] = Field(default_factory=dict, description="Non-sensitive config (host, dataset, etc.)")

    @field_validator("connection_type")
    @classmethod
    def _check_connection_type(cls, v: str) -> str:
        return _validate_connection_type(v)

    @field_validator("config")
    @classmethod
    def _check_config(cls, v: dict[str, Any]) -> dict[str, Any]:
        # F-014-01: secrets must never enter the plaintext config bag.
        return _validate_non_sensitive_config(v)


class ConnectionUpdate(BaseModel):
    display_name: Optional[str] = None
    connection_type: Optional[str] = None
    credentials: Optional[dict[str, Any]] = None
    config: Optional[dict[str, Any]] = None

    @field_validator("config")
    @classmethod
    def _check_config(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        # F-014-01: secrets must never enter the plaintext config bag.
        return _validate_non_sensitive_config(v)

    @field_validator("connection_type")
    @classmethod
    def _check_connection_type(cls, v: Optional[str]) -> Optional[str]:
        # F-014-10: PATCH previously skipped validation, so a client could
        # set connection_type to ``jdbc``/``oracle``/any string and the
        # connection then failed at every dispatch site with "Unsupported
        # connector type". Apply the same gate as ConnectionCreate; ``None``
        # means "field not being changed" and is left untouched.
        if v is None:
            return v
        return _validate_connection_type(v)


class ConnectionResponse(OrmBase):
    id: uuid.UUID
    project_id: uuid.UUID
    display_name: str
    connection_type: str
    config: dict[str, Any]
    # Non-sensitive preview of the stored credentials so the edit dialog can
    # pre-fill host/port/database/username. Sensitive keys (password,
    # service_account_json, secret, token, api_key) are stripped server-side.
    # Populated by the API layer; the ORM object does not carry it.
    credentials_preview: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


# Legacy aliases
ProjectConnectionCreate = ConnectionCreate
ProjectConnectionUpdate = ConnectionUpdate
ProjectConnectionResponse = ConnectionResponse


class ProfileTableRef(BaseModel):
    """A single table to profile. ``schema`` defaults to ``public`` (PG)."""
    schema_: str = Field("public", alias="schema")
    table: str = Field(min_length=1)

    model_config = {"populate_by_name": True}


class ProfileTablesRequest(BaseModel):
    """F-014-14: typed body for the profile endpoint. Previously the route
    took a raw ``dict`` and ``tbl["table"]`` raised a bare ``KeyError`` that
    surfaced as a misleading 502 ``"'table'"`` instead of a 422 validation
    error."""
    tables: list[ProfileTableRef] = Field(default_factory=list)


class ConnectionTestResponse(BaseModel):
    ok: bool
    detail: Optional[str] = None


class ConnectionTestRequest(BaseModel):
    connection_type: str = Field(description="bigquery | postgresql | hadoop_spark | redshift | snowflake | sqlserver")
    credentials: dict[str, Any] = Field(description="Raw credentials to validate")

    @field_validator("connection_type")
    @classmethod
    def _check_connection_type(cls, v: str) -> str:
        return _validate_connection_type(v)
    config: dict[str, Any] = Field(default_factory=dict, description="Non-sensitive config")

    @field_validator("config")
    @classmethod
    def _check_config(cls, v: dict[str, Any]) -> dict[str, Any]:
        # F-014-01: secrets must never enter the plaintext config bag.
        return _validate_non_sensitive_config(v)


