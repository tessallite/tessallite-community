"""Bootstrap-only settings (per C-1 of the configuration revamp).

Everything that *can* live in the database lives there now and is read via
``shared.config.resolver.get_setting`` or
``shared.config.bootstrap.system_snapshot_get``. The fields below are the
strict bootstrap minimum that must exist before the system DB is reachable
or before service processes can listen for traffic:

  - the system DB DSN (chicken/egg)
  - secrets (Fernet key, JWT signing key, system admin credentials)
  - listening ports for the JDBC and XMLA gateway
  - peer service URLs used for inter-service calls
  - CORS allow-list (used by middleware before the snapshot is loaded)

Operational tuning knobs (rate limits, optimizer thresholds, result-row
caps, scheduler cadences, XMLA metadata timestamps, etc.) have moved to
``system_settings`` and are surfaced in the System Configuration UI. See
``docs/guides/guides_configuration-reference.md`` for the full per-key catalog.
"""
import base64
import logging
import os
import sys

from pydantic import model_validator
from pydantic_settings import BaseSettings
from functools import lru_cache

log = logging.getLogger(__name__)

_PLACEHOLDER_PREFIX = "CHANGE_ME"
# Only bypass secret validation when both the env var is set AND pytest is
# the running process.  This two-factor check makes accidental production
# activation virtually impossible.
_TESTING = (
    os.environ.get("TESSALLITE_TESTING", "").lower() in ("1", "true")
    and "pytest" in sys.modules
)


class Settings(BaseSettings):
    # System DB — global PostgreSQL that stores the tenant registry + per-tenant DB URLs
    SYSTEM_DATABASE_URL: str = "postgresql+asyncpg://tessallite:tessallite@localhost:5432/tessallite_system"

    # Credential encryption — Fernet symmetric key (base64-encoded, 32 bytes)
    # Generate with: from cryptography.fernet import Fernet; Fernet.generate_key().decode()
    CREDENTIAL_ENCRYPTION_KEY: str = "CHANGE_ME_generate_with_fernet"
    # Previous key for zero-downtime rotation: set the old key here, update
    # CREDENTIAL_ENCRYPTION_KEY to the new key, then call the rotation endpoint.
    CREDENTIAL_ENCRYPTION_KEY_PREVIOUS: str = ""

    # System admin — infrastructure-level credentials (no DB model)
    SYSTEM_ADMIN_EMAIL: str = "admin@tessallite.local"
    SYSTEM_ADMIN_PASSWORD: str = "CHANGE_ME_set_in_env"

    # Auth — JWT signing material (rotation requires service restart)
    JWT_SECRET_KEY: str = "CHANGE_ME_use_strong_secret_in_production"
    JWT_ALGORITHM: str = "HS256"

    # Open-core Community Edition licensing. Default OFF == full product: no
    # control-plane caps, current behaviour unchanged. ON == Community: the
    # license manager enforces create caps (users/projects/models/tenants).
    LICENSE_ENFORCEMENT_ENABLED: bool = False
    LICENSE_FILE: str = ""  # path to the signed license JSON (Community installs)
    LICENSE_PUBLIC_KEYS: str = ""  # "key_id:base64rawpubkey[,key_id2:base64...]"

    # Auth backends — ordered list tried during login.
    # Supported values: "local", "ldap", "gcp_iam", "saml", "oidc"
    AUTH_BACKENDS: str = "local"

    # LDAP configuration (used when "ldap" is in AUTH_BACKENDS)
    LDAP_URL: str = ""
    LDAP_BIND_DN: str = ""
    LDAP_BIND_PASSWORD: str = ""
    LDAP_USER_SEARCH_BASE: str = ""
    LDAP_USER_SEARCH_FILTER: str = "(mail={email})"
    LDAP_GROUP_SEARCH_BASE: str = ""
    LDAP_GROUP_ATTRIBUTE: str = "memberOf"
    LDAP_EMAIL_ATTRIBUTE: str = "mail"
    LDAP_DISPLAY_NAME_ATTRIBUTE: str = "displayName"
    LDAP_USE_SSL: bool = True

    # GCP IAM configuration (used when "gcp_iam" is in AUTH_BACKENDS)
    GCP_IAM_ALLOWED_DOMAINS: str = ""
    GCP_IAM_AUDIENCE: str = ""

    # SAML 2.0 SP configuration (used when "saml" is in AUTH_BACKENDS)
    SAML_IDP_METADATA_URL: str = ""
    SAML_IDP_METADATA_XML: str = ""
    SAML_SP_ENTITY_ID: str = ""
    SAML_ATTR_EMAIL: str = "email"
    SAML_ATTR_DISPLAY_NAME: str = "displayName"
    SAML_ATTR_GROUPS: str = "groups"

    # JWT claims bounding (Bug-1072): SSO callbacks embed only the IdP
    # attributes referenced by row-security rules plus this allow-list,
    # and refuse to issue a token whose serialized claims exceed the cap.
    AUTH_JWT_CLAIMS_ALLOWLIST: str = ""
    AUTH_JWT_CLAIMS_MAX_BYTES: int = 4096

    # OIDC authorization-code flow configuration (used when "oidc" is in AUTH_BACKENDS)
    OIDC_ISSUER: str = ""
    OIDC_CLIENT_ID: str = ""
    OIDC_CLIENT_SECRET: str = ""
    OIDC_SCOPES: str = "openid email profile"
    OIDC_GROUPS_CLAIM: str = "groups"

    # Gateway listen ports (read at process start)
    JDBC_PORT: int = 5433   # PostgreSQL wire protocol (V1)
    XMLA_PORT: int = 8080   # DAX/XMLA HTTP hybrid

    # Gateway SSL/TLS (JDBC listener)
    GATEWAY_SSL_ENABLED: bool = False
    GATEWAY_SSL_CERT_FILE: str = ""
    GATEWAY_SSL_KEY_FILE: str = ""
    GATEWAY_SSL_CA_FILE: str = ""
    # F-001-08: require TLS for JDBC. When True, the gateway refuses any
    # plaintext startup (a client that did not first negotiate SSLRequest, or
    # one downgraded by an active attacker) with SQLSTATE 28000 so the tenant
    # password is never transmitted in the clear. Off by default for local dev
    # (plain TCP); enable on any public listener. Requires GATEWAY_SSL_ENABLED.
    GATEWAY_SSL_REQUIRED: bool = False
    # F-001-07: pre-auth access-governance on the JDBC TCP listener. A per-IP
    # cap on concurrent connections and a sliding-window failed-auth throttle
    # bound brute-force and connection-flood attempts before they reach
    # model-service. 0 disables the respective control (default: enabled with
    # generous limits so legitimate BI tools — which open many short-lived
    # connections — are never blocked).
    GATEWAY_JDBC_MAX_CONN_PER_IP: int = 50
    GATEWAY_JDBC_MAX_AUTH_FAILURES: int = 10
    GATEWAY_JDBC_AUTH_FAILURE_WINDOW_SECONDS: int = 60
    # Gateway XMLA/HTTP listener TLS (uvicorn). Reuses the cert/key files above.
    # Off by default (plain HTTP, e.g. local compose where TLS is terminated by
    # nginx or not needed). Enable on edge deployments that expose 8080 directly
    # to BI clients (Excel/Power BI XMLA) over the public internet.
    GATEWAY_XMLA_TLS_ENABLED: bool = False
    LOOKER_GATEWAY_ENABLED: bool = False

    # Internal service URLs (used by inter-service HTTP clients)
    QUERY_ROUTER_URL: str = "http://query-router:8000"
    # model-service listens on 8001 (services/model-service/Dockerfile EXPOSE 8001);
    # every compose/Cloud-Run consumer overrides this to :8001. The default was
    # left at the wrong :8000, which silently broke the scheduler's KPI snapshot
    # sweep — added later as a model-service client without an explicit override
    # (F-012-03). Default corrected to the real port so a missing override no
    # longer points at a dead port.
    MODEL_SERVICE_URL: str = "http://model-service:8001"
    OPTIMIZER_URL: str = "http://optimizer:8000"
    SCHEDULER_URL: str = "http://scheduler:8000"
    AGENT_SERVICE_URL: str = "http://agent-service:8000"

    # Cookie security — set False for local dev over plain HTTP
    COOKIE_SECURE: bool = True

    # CORS origins (comma-separated). Read by middleware before the system
    # snapshot is loaded, so it stays in env.
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # Additional CORS origins for embed token consumers (comma-separated).
    # Merged with CORS_ORIGINS at startup.
    ALLOWED_EMBED_ORIGINS: str = ""

    # Query result cache TTL in seconds. 0 = disabled.
    QUERY_CACHE_TTL_SECONDS: int = 60

    # Bug-5436a: max members returned by XMLA member discovery (hierarchy preview
    # + dimension members). The old hard-coded 1000 silently truncated large
    # hierarchies/dimensions in Excel/Power BI filter dropdowns. Raised to a high
    # bound; discovery logs a warning when a result hits the cap so truncation is
    # never silent. Tune down only if a source has pathologically large dims.
    MEMBER_DISCOVERY_LIMIT: int = 100000

    # Headless API rate limit — max requests per minute per tenant.
    HEADLESS_RATE_LIMIT: int = 100

    # Rate-limiter storage backend URI (F-H27R1-02). Empty -> per-replica
    # in-memory store (``memory://``); buckets are NOT shared across uvicorn
    # workers or service replicas, so the effective ceiling is N× the
    # configured limit under scale-out. Point this at a shared store that the
    # underlying ``limits`` library already supports (e.g.
    # ``redis://host:6379/0`` or ``memcached://host:11211``) to enforce a
    # single ceiling across all replicas. No new dependency — URI passthrough.
    RATE_LIMIT_STORAGE_URI: str = ""

    # SMTP — outbound email for alert notifications
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_TLS: bool = True
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@tessallite.local"

    @model_validator(mode="after")
    def _reject_placeholder_secrets(self) -> "Settings":
        if _TESTING:
            return self
        for field_name in (
            "CREDENTIAL_ENCRYPTION_KEY",
            "SYSTEM_ADMIN_PASSWORD",
            "JWT_SECRET_KEY",
        ):
            value = getattr(self, field_name)
            if not value or not value.strip():
                raise ValueError(
                    f"{field_name} must not be empty. "
                    f"Set a real value in .env or environment variables before starting."
                )
            if value.startswith(_PLACEHOLDER_PREFIX):
                raise ValueError(
                    f"{field_name} is still set to a placeholder value. "
                    f"Set a real value in .env or environment variables before starting."
                )

        if len(self.JWT_SECRET_KEY) < 32:
            raise ValueError(
                "JWT_SECRET_KEY must be at least 32 characters. "
                "Generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )

        try:
            raw = base64.urlsafe_b64decode(self.CREDENTIAL_ENCRYPTION_KEY)
            if len(raw) != 32:
                raise ValueError("decoded key is not 32 bytes")
        except Exception:
            raise ValueError(
                "CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key (base64-encoded 32 bytes). "
                "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

        return self

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        # Legacy .env keys (rate_limit_*, agg_*, miss_threshold_*, etc.) were
        # migrated to `system_settings` during the config revamp. Tolerate them
        # in the host .env until operators trim their local copies to match
        # `.env.example` (bootstrap-only).
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
