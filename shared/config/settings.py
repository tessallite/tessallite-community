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
import re
import sys

from pydantic import Field, model_validator
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

    # Bug-9192 / RFGPT-002: max retained per-tenant request engines in THIS
    # process. Each engine holds pool_size=2 connections; an unbounded cache
    # of engines can still exhaust PostgreSQL max_connections under a large
    # active-tenant sweep. LRU eviction disposes the oldest engines.
    TENANT_ENGINE_CACHE_MAX: int = Field(default=16, ge=1)

    # Credential encryption — Fernet symmetric key (base64-encoded, 32 bytes)
    # Generate with: from cryptography.fernet import Fernet; Fernet.generate_key().decode()
    CREDENTIAL_ENCRYPTION_KEY: str = "CHANGE_ME_generate_with_fernet"
    # Previous key for zero-downtime rotation: set the old key here, update
    # CREDENTIAL_ENCRYPTION_KEY to the new key, then call the rotation endpoint.
    CREDENTIAL_ENCRYPTION_KEY_PREVIOUS: str = ""

    # System admin — infrastructure-level credentials (no DB model)
    SYSTEM_ADMIN_EMAIL: str = "admin@tessallite.local"
    SYSTEM_ADMIN_PASSWORD: str = "CHANGE_ME_set_in_env"

    # Bug-9316-followup: whether a weak SYSTEM_ADMIN_PASSWORD is a HARD FAILURE at
    # Settings() construction (True) or a logged WARNING that proceeds (False).
    # The authoritative production guard is the installer's prompt-/secret-time
    # validation (deploy/local, deploy/gcp, deploy/community all enforce the same
    # floor). The runtime re-check is defence-in-depth. Defaulting it OFF stops
    # the dev/test/CI/demo `.env` (which carries a short bootstrap password) from
    # crash-looping services and failing test collection. Production installers
    # write ENFORCE_SECRET_STRENGTH=true into the deployed `.env`, so real
    # deployments keep the fatal runtime re-check. Never weaken the installer
    # validation — this flag only governs the runtime re-check, not the installer.
    ENFORCE_SECRET_STRENGTH: bool = False

    # Auth — JWT signing material (rotation requires service restart)
    JWT_SECRET_KEY: str = "CHANGE_ME_use_strong_secret_in_production"
    JWT_ALGORITHM: str = "HS256"

    # Open-core Community Edition licensing. Default ON: unactivated instances
    # deny capped creates until a signed licence is installed. Set
    # TESSALLITE_DEV_UNLIMITED=true (or the legacy LICENSE_ENFORCEMENT_ENABLED=false
    # hatch) only for internal/dev stacks. Status never reports "enterprise"
    # without a verified Enterprise licence (F-031-01 / F-031-02).
    LICENSE_ENFORCEMENT_ENABLED: bool = True
    TESSALLITE_DEV_UNLIMITED: bool = False
    LICENSE_FILE: str = ""  # path to the signed license JSON (Community installs)
    LICENSE_PUBLIC_KEYS: str = ""  # extras only; the vendor key is always trusted

    # Product-side licence beacon emitter (Bug-5459). Default OFF: with no URL the
    # emitter never starts. The beacon carries license_id ONLY (no PII) and is the
    # client counterpart to the issuer ``/beacon`` sink. The URL is the product's
    # OWN endpoint var (distinct from the server-side ``ISSUER_BEACON`` sink selector).
    LICENSE_BEACON_URL: str = ""  # e.g. https://issuer.tessallite.io/beacon
    LICENSE_BEACON_INTERVAL_HOURS: float = 24.0  # cadence; floored to 60s in the emitter
    LICENSE_BEACON_VERSION: str = ""  # product version reported on the beacon (non-PII)

    # Anti-exploitation: gate LLM configs that authenticate via cloud
    # service-account / OAuth (Application Default Credentials) instead of a
    # bring-your-own API key — e.g. Google Vertex AI mode (google_mode=vertex_ai).
    # That path bills the DEPLOYMENT's cloud project, not the customer's key, so
    # it is OFF by default: the LLM-config CRUD only accepts BYO API keys unless
    # an operator deliberately enables this. (Cost-leak hardening, 2026-06-23.)
    LLM_ALLOW_SERVICE_ACCOUNT_AUTH: bool = False

    # Bug-5937: catalog metadata import (DataHub/OpenMetadata/Alation) sends
    # the operator-supplied API token as an Authorization header to the
    # operator-supplied catalog URL. `http://` transport would send that
    # token in plaintext, observable/tamperable by any network
    # intermediary. OFF by default: only `https://` catalog URLs are
    # accepted. Self-hosted/on-prem deployments that genuinely run an
    # unencrypted internal catalog service can opt in explicitly; SSRF
    # host/IP blocking (private/loopback/reserved address checks) still
    # applies regardless of this flag.
    CATALOG_IMPORT_ALLOW_HTTP: bool = False

    # Bug-7339: Webhook transport security. When False (default), webhook
    # endpoint URLs must use HTTPS — the HMAC signature and payload are
    # never sent over plaintext. Labs or on-prem environments behind a
    # trusted network may opt in to HTTP explicitly; SSRF host/IP blocking
    # still applies regardless. The existing ``allow_http`` parameter on
    # ``validate_webhook_url`` is now driven by this setting.
    WEBHOOK_ALLOW_HTTP: bool = False

    # Bug-8355: outbound agent-webhook multi-tenancy isolation. The dispatcher
    # shares ONE httpx connection pool across every tenant in the process. A
    # receiver that trickles bytes slowly (staying under the per-read timeout
    # and under the response-byte cap) holds its connection for as long as it
    # keeps trickling, so without a per-tenant budget one tenant's misbehaving
    # receiver can occupy the whole pool and starve every other tenant's
    # deliveries. Three knobs, all with safe defaults:
    #   * pool size (unchanged from the value the pool has always used);
    #   * how much of that pool ANY single tenant may hold at once — this is
    #     the isolation guarantee, and it must stay well below the pool size
    #     or it guarantees nothing;
    #   * an absolute wall-clock ceiling on one delivery attempt, because the
    #     httpx timeout is per-read and every arriving chunk resets it, so
    #     bytes-based and per-read guards alone cannot bound hold time.
    AGENT_WEBHOOK_MAX_CONNECTIONS: int = 20
    AGENT_WEBHOOK_MAX_CONNECTIONS_PER_TENANT: int = 5
    AGENT_WEBHOOK_ATTEMPT_DEADLINE_SEC: int = 30
    AGENT_WEBHOOK_POOL_ACQUIRE_TIMEOUT_SEC: int = 5

    # Auth backends — ordered list tried during login.
    # Supported values: "local", "ldap", "gcp_iam", "saml", "oidc"
    AUTH_BACKENDS: str = "local"

    # LDAP configuration (used when "ldap" is in AUTH_BACKENDS)
    LDAP_ENABLED: bool = False
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
    # Bug-7316: comma-separated list of trusted IdP entityIDs.  When set,
    # the SAML backend rejects metadata whose entityID is not in the list,
    # preventing metadata-swap attacks.
    SAML_ALLOWED_ENTITY_IDS: str = ""
    SAML_ATTR_EMAIL: str = "email"
    SAML_ATTR_DISPLAY_NAME: str = "displayName"
    SAML_ATTR_GROUPS: str = "groups"
    # Optional SP signing material. When both are set, AuthnRequests are signed
    # (F-021-03 / G-021-02 deployable SAML).
    SAML_SP_X509CERT: str = ""
    SAML_SP_PRIVATE_KEY: str = ""

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

    # Bug-8974: the gateway's /health proves its own JDBC accept loop is alive
    # with a credential-free loopback PostgreSQL SSLRequest exchange (see
    # shared/gateway_liveness.py). Without it, /health reported 200 while the
    # JDBC listener was bound-but-not-accepting and every BI client hung.
    # Switch it off ONLY on a deployment that does not serve JDBC at all;
    # leaving it on where the listener never binds turns /health into a
    # permanent 503.
    GATEWAY_HEALTH_JDBC_PROBE_ENABLED: bool = True
    # Bounds the loopback exchange. Kept well under the 5s healthcheck timeout
    # in docker-compose so a wedged listener fails the probe rather than the
    # whole HTTP request.
    GATEWAY_HEALTH_JDBC_PROBE_TIMEOUT_SECONDS: float = 2.0

    # Bug-8533 auto-recovery half: /health telling the truth does not RESTART
    # anything on Docker Compose or `docker run` — `restart: unless-stopped`
    # acts on process EXIT, not on health. So the gateway watches its own accept
    # loop with the same probe and exits non-zero once it is proven dead, which
    # the restart policy then recycles. Three CONSECUTIVE failures, not one: a
    # single transient miss (a scheduling stall, an accept-path retry, a
    # governor refusal under load) must never restart a working gateway — that
    # false negative is worse than the silent wedge being fixed.
    GATEWAY_JDBC_WATCHDOG_ENABLED: bool = True
    # Dormancy between probes. 60s x 3 strikes bounds the worst-case detection
    # delay at ~3 minutes, against the ~17 minutes a backlog-exhaustion probe
    # would have taken.
    GATEWAY_JDBC_WATCHDOG_INTERVAL_SECONDS: float = 60.0
    # Consecutive failures required before the process exits. A successful probe
    # resets the count to zero.
    GATEWAY_JDBC_WATCHDOG_FAILURE_LIMIT: int = 3

    # Gateway SSL/TLS (JDBC listener)
    GATEWAY_SSL_ENABLED: bool = False
    GATEWAY_SSL_CERT_FILE: str = ""
    GATEWAY_SSL_KEY_FILE: str = ""
    GATEWAY_SSL_CA_FILE: str = ""
    # F-001-08: require TLS for JDBC. When True, the gateway refuses any
    # plaintext startup (a client that did not first negotiate SSLRequest, or
    # one downgraded by an active attacker) with SQLSTATE 28000 so the tenant
    # password is never transmitted in the clear. Requires GATEWAY_SSL_ENABLED.
    #
    # Wave C #2: TLS is required BY DEFAULT (True). The startup validation
    # (gateway ``validate_transport_security``) refuses to start a gateway that
    # serves password/JWT auth with TLS disabled UNLESS the operator sets the
    # explicit local-dev opt-out ``GATEWAY_ALLOW_INSECURE_TRANSPORT=True``. This
    # closes the "production silently runs plaintext" gap while keeping a single,
    # explicit escape hatch for local dev.
    GATEWAY_SSL_REQUIRED: bool = True
    # Wave C #2: the EXPLICIT local-dev opt-out. When True the gateway may serve
    # plaintext (no TLS) — for local Docker Compose / developer stacks only. When
    # False (the default, i.e. the production posture) the gateway will not START
    # unless TLS is enabled and required; there is no silent plaintext production.
    GATEWAY_ALLOW_INSECURE_TRANSPORT: bool = False
    # F-001-07: pre-auth access-governance on the JDBC TCP listener. A per-IP
    # cap on concurrent connections and a sliding-window failed-auth throttle
    # bound brute-force and connection-flood attempts before they reach
    # model-service. 0 disables the respective control (default: enabled with
    # generous limits so legitimate BI tools — which open many short-lived
    # connections — are never blocked).
    GATEWAY_JDBC_MAX_CONN_PER_IP: int = 50
    GATEWAY_JDBC_MAX_AUTH_FAILURES: int = 10
    GATEWAY_JDBC_AUTH_FAILURE_WINDOW_SECONDS: int = 60
    # Bug-8143: hard cap on the number of distinct failure-window keys the
    # governor tracks. JDBC keys on the raw peer IP (naturally bounded), but the
    # XMLA throttle key includes a client-supplied identity, so an attacker
    # could otherwise grow the failure map without bound. When the map exceeds
    # this cap the governor reclaims expired windows and, if still over, evicts
    # down to a low-water mark — sub-threshold buckets first (oldest-first
    # within a tier) so an actively-throttling bucket is not flushed. 0 disables
    # the cap.
    GATEWAY_JDBC_MAX_TRACKED_FAILURE_KEYS: int = 20000
    # Bug-8108: hard cap on the number of rows a single JDBC extended-protocol
    # portal (``_execute_for_extended``) will buffer in gateway memory before
    # Describe/Execute can page it out via PortalSuspended. PortalSuspended
    # only bounds how many rows are put on the wire per Execute call, not how
    # much the gateway holds in memory before that — an unbounded result set
    # is fully materialised regardless of the client's requested fetch size.
    # Exceeding the cap fails closed with SQLSTATE 54000
    # (program_limit_exceeded) instead of accepting unbounded memory growth.
    # Same three-tier resolution as GATEWAY_QUERY_BYTE_CEILING: hot-reloadable
    # system setting (``gateway.jdbc_portal_row_buffer_cap``) first, this env
    # var second, hardcoded constant last. 0 disables the cap.
    GATEWAY_JDBC_PORTAL_ROW_BUFFER_CAP: int = 50_000
    # Gateway XMLA/HTTP listener TLS (uvicorn). Reuses the cert/key files above.
    # Off by default (plain HTTP, e.g. local compose where TLS is terminated by
    # nginx or not needed). Enable on edge deployments that expose 8080 directly
    # to BI clients (Excel/Power BI XMLA) over the public internet.
    GATEWAY_XMLA_TLS_ENABLED: bool = False
    LOOKER_GATEWAY_ENABLED: bool = False

    # Public-facing XMLA gateway URL used to generate .odc connection files
    # for BI clients (Excel, Power BI). Defaults to the local Docker Compose
    # address. Override in production with the externally-reachable URL
    # (e.g. https://gateway.example.com:8080).
    GATEWAY_XMLA_PUBLIC_URL: str = "http://localhost:8080"

    # Bug-6216: egress policy for tenant-supplied source-database hosts.
    # Connection Test / Discover open a real TCP connection to whatever host a
    # tenant types, from inside the platform's network. Loopback, link-local
    # (169.254.169.254 cloud metadata), unspecified, multicast and reserved
    # addresses are always refused. Private RFC1918 space is ALLOWED by default
    # because a self-hosted / docker-compose install reaches its source database
    # over exactly such an address; set this true on a managed multi-tenant
    # deployment where every tenant source is on the public internet.
    SOURCE_HOST_BLOCK_PRIVATE: bool = False
    # Comma-separated hosts exempt from the policy above, for the deployment
    # whose legitimate source really does sit on loopback or a blocked name.
    SOURCE_HOST_ALLOWLIST: str = ""
    # Seconds to wait for a source-host DNS lookup. The name is tenant-supplied
    # and resolved on a write endpoint, so an unbounded lookup lets a tarpitting
    # nameserver pin a request worker (and, on the update path, an open tenant
    # transaction).
    SOURCE_HOST_RESOLVE_TIMEOUT_SEC: float = 5.0

    # Bug-6307: the externally-reachable origin of this deployment
    # (e.g. https://cloud.example.com), WITHOUT a trailing slash. Redirect-based
    # flows that must publish an absolute callback URL to a third party — the
    # SAML AssertionConsumerService URL, the SP metadata document, the SAML
    # Destination check, the OIDC redirect_uri — read it from here. Those URLs
    # used to be reconstructed from the request's own Host / X-Forwarded-Host
    # headers, which the client controls, so an attacker could aim a signed
    # SAML assertion at a host of their choosing. When left empty the request
    # origin is accepted only if it matches a configured CORS origin.
    PUBLIC_BASE_URL: str = ""

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
    # Per-replica hard bound on resident query results.
    QUERY_CACHE_MAX_ENTRIES: int = Field(default=10_000, ge=1)

    # Bug-5436a: max members returned by XMLA member discovery (hierarchy preview
    # + dimension members). The old hard-coded 1000 silently truncated large
    # hierarchies/dimensions in Excel/Power BI filter dropdowns. Raised to a high
    # bound; discovery logs a warning when a result hits the cap so truncation is
    # never silent. Tune down only if a source has pathologically large dims.
    MEMBER_DISCOVERY_LIMIT: int = 100000

    # Headless API rate limit — max requests per minute per tenant.
    HEADLESS_RATE_LIMIT: int = 100

    # Bug-7787 (Impact Analysis, spec §7.5): non-secret display/safety caps for
    # the model-dependency what-if engine. Truncation NEVER changes a guard
    # decision — the engine always computes the complete reachable set for
    # enforcement, then caps only display paths and marks summary.truncated.
    #   IMPACT_MAX_PATHS_PER_OBJECT  additional witness paths shown per impact.
    #   IMPACT_MAX_DISPLAY_IMPACTS   impacts returned in the response body.
    #   IMPACT_CATALOGUE_PAGE_LIMIT  default GET /objects page size.
    #   IMPACT_CATALOGUE_MAX_LIMIT   hard ceiling on a client-requested limit.
    IMPACT_MAX_PATHS_PER_OBJECT: int = 3
    IMPACT_MAX_DISPLAY_IMPACTS: int = 2000
    IMPACT_CATALOGUE_PAGE_LIMIT: int = 200
    IMPACT_CATALOGUE_MAX_LIMIT: int = 1000

    # Bug-7745: cumulative byte ceiling per query response on the public
    # gateway path. The gateway rejects (fail-closed) any query whose
    # response payload exceeds this many bytes. 0 disables the ceiling.
    # Default 50 MiB — generous for BI analytics, prevents unbounded
    # exfiltration / memory exhaustion via SELECT * on large tables.
    GATEWAY_QUERY_BYTE_CEILING: int = 50 * 1024 * 1024

    # Bug-7745: per-tenant query rate limit on the public gateway path
    # (JDBC + XMLA). Maximum queries per tenant per minute. The gateway
    # rejects excess queries with a typed error. 0 disables.
    # Per-replica: effective limit is N x configured on multi-replica
    # deployments (same as HEADLESS_RATE_LIMIT).
    GATEWAY_QUERY_RATE_LIMIT_PER_MINUTE: int = 120

    # Rate-limiter storage backend URI (F-H27R1-02). Empty -> per-replica
    # in-memory store (``memory://``); buckets are NOT shared across uvicorn
    # workers or service replicas, so the effective ceiling is N× the
    # configured limit under scale-out. Point this at a shared store that the
    # underlying ``limits`` library already supports (e.g.
    # ``redis://host:6379/0`` or ``memcached://host:11211``) to enforce a
    # single ceiling across all replicas. No new dependency — URI passthrough.
    RATE_LIMIT_STORAGE_URI: str = ""

    # Named Lists — SQL-path fixed member lists (section 5.5 of the spec).
    # Default member-count cap per list and hard ceiling an operator may raise it to.
    NAMED_LIST_MEMBER_CAP: int = Field(default=1000, ge=1)
    NAMED_LIST_MEMBER_CAP_CEILING: int = Field(default=5000, ge=1)

    # Bug-7982 completion round: bounds how long a writer will BLOCK waiting to
    # acquire the per-model definition/governance advisory lock
    # (model-service ``src/api/_model_lock.py``). Most holders release in
    # milliseconds, but a holder that runs a slow/hung external call while the
    # lock is held (e.g. calendar auto-create's source DDL) could otherwise
    # starve every other writer on the model indefinitely. This is a
    # PostgreSQL ``lock_timeout`` (SET LOCAL, applied to the WAITER's own
    # transaction before it attempts to acquire the lock) — it never affects
    # how long a holder may hold the lock, only how long another writer will
    # wait before failing loud with a clear, retryable error.
    MODEL_DEFINITION_LOCK_TIMEOUT_SECONDS: int = Field(default=30, ge=1)

    # Bug-7982 R7 (findings 3+4): enforcement level of the RUNTIME guard that
    # asserts no snapshot-owned table is written without the per-model
    # definition lock held on the writing connection
    # (``shared/db/model_write_lock_guard.py``).
    #   ``warn``   — log an ERROR naming the table and statement (default: turns
    #                a silent race into an operator-visible signal, never breaks
    #                a running product).
    #   ``strict`` — raise ``ModelWriteWithoutLockError``. Used by the guard test
    #                suites so a regression fails the build.
    #   ``off``    — disable, for tooling that legitimately rebuilds this state
    #                wholesale (migrations, seeding, restore).
    MODEL_WRITE_LOCK_GUARD_MODE: str = Field(
        default="warn", pattern="^(off|warn|strict)$"
    )

    # Agent glossary grounding — ATTENTION budget (not a cost budget) governing
    # the glossary inclusion policy in the planner prompt (spec 3.4). When the
    # full glossary fits this budget it is rendered in the stable cacheable
    # prefix every turn (retrieval disabled — no per-turn miss risk); above it,
    # a compact term index stays always-on and full cards are retrieved by
    # relevance (score>0) into the per-turn suffix. Estimated as chars/4.
    AGENT_GLOSSARY_ATTENTION_BUDGET_TOKENS: int = Field(default=8000, ge=0)

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

        # Bug-9304: SYSTEM_ADMIN_PASSWORD must meet the same floor as tenant
        # user passwords (min 12, upper + lower + digit). _TESTING still bypasses
        # this (returned early above) so pytest can load placeholder .env values.
        #
        # Bug-9316-followup: this is the runtime RE-check. In a PRODUCTION
        # deployment (ENFORCE_SECRET_STRENGTH=true, written by the installers) a
        # weak value is fatal, exactly as before. In dev/test/CI/demo the flag is
        # off, so a short bootstrap password logs a clear WARNING and proceeds
        # instead of crash-looping services or failing test collection. The
        # authoritative production guard remains the installer's prompt-/secret-
        # time validation, which is unchanged and independent of this flag.
        pw = self.SYSTEM_ADMIN_PASSWORD
        problems = []
        if len(pw) < 12:
            problems.append("must be at least 12 characters")
        if not re.search(r"[A-Z]", pw):
            problems.append("must contain at least one uppercase letter")
        if not re.search(r"[a-z]", pw):
            problems.append("must contain at least one lowercase letter")
        if not re.search(r"[0-9]", pw):
            problems.append("must contain at least one digit")
        if problems:
            detail = "SYSTEM_ADMIN_PASSWORD " + "; ".join(problems) + "."
            if self.ENFORCE_SECRET_STRENGTH:
                raise ValueError(detail)
            log.warning(
                "%s Proceeding because ENFORCE_SECRET_STRENGTH is off "
                "(dev/test/CI/demo). Production installers set it to true so this "
                "check is fatal in real deployments.",
                detail,
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
