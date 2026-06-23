"""Setting registry — single source of truth for every configurable knob.

Every value previously hardcoded in source lives here as a ``SettingDef``.
The registry drives:

  - validation on write (type coercion, validator callable)
  - seed-migration on bootstrap and tenant-create
  - UI form rendering at every level
  - the per-key reference doc in ``docs/guides/guides_configuration-reference.md``

Adding a new setting is registry-only: pick a key, level, type, default;
no migration required because storage is JSONB key/value.

Levels
------
A setting declares the lowest level at which it can be set. The resolver
falls back upward (model -> project -> tenant -> system -> default) and
treats a stored ``None`` as "unset, fall through". Settings flagged with
``restart_required=True`` append to ``system_restart_pending`` on write.

Per-key home is exclusive: each setting lives at exactly one level. The
former ``(override)`` pattern (same key duplicated at lower levels) was
retired in favour of resolver-driven fallback when both system and model
hold the same key string. The model-level row, if set, wins; if unset
(stored as ``None``), the system row wins.

Secrets
-------
The registry contains NO credentials. Per the C-4 decision, every secret
(DB passwords, API keys, JWT signing key, Fernet key, system admin
password, SMTP creds) stays in ``.env`` and is referenced via the
``env_var`` field for read-only display only.

UI metadata
-----------
Each SettingDef can carry ``label``, ``ui_help``, ``ui_group``,
``ui_control``, ``ui_choices``, and ``unit`` so the Configuration UI
can render a friendly form (no raw keys, no cron expressions in
free-text boxes, no cryptic acronyms). These are optional — if omitted
the renderer falls back to a prettified key and generic control.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional


Level = Literal["system", "tenant", "project", "model"]
SettingType = Literal[
    "int", "float", "str", "bool", "cron", "iso_datetime",
    "list[str]", "dict",
]


@dataclass(frozen=True)
class SettingDef:
    """Declarative definition of a single configurable value."""

    key: str
    level: Level
    type: SettingType
    default: Any
    description: str = ""
    restart_required: bool = False
    env_var: Optional[str] = None
    section: str = "General"
    validator: Optional[Callable[[Any], None]] = None
    sensitive_display: bool = False
    # UI metadata — optional, but strongly recommended so the form renderer
    # does not need to expose the raw registry key to end users.
    label: Optional[str] = None
    ui_help: Optional[str] = None
    ui_group: Optional[str] = None
    # Controls: "switch", "select", "select-multi", "cron", "text", "textarea",
    #          "number", "slider", "url", "dict", "secret", "llm-config-picker"
    ui_control: Optional[str] = None
    ui_choices: Optional[list] = None  # enum options for "select" controls
    unit: Optional[str] = None  # e.g. "seconds", "rows", "hours"
    # When false, the API/UI hides this setting from the primary configuration
    # surface even though it lives in the registry. Used for operational
    # levers we keep typed and validated but don't want admins editing.
    surfaced: bool = True


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_CRON_FIELD_RANGES: list[tuple[str, int, int]] = [
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
]


def _validate_cron_field(field_name: str, token: str, lo: int, hi: int) -> None:
    """Validate a single cron field token against its allowed range."""
    if token == "*":
        return
    if token.startswith("*/"):
        step = token[2:]
        if not step.isdigit() or int(step) < 1:
            raise ValueError(
                f"Invalid cron field '{field_name}': step '*/{ step}' must be a positive integer."
            )
        return
    for part in token.split(","):
        if "-" in part:
            bounds = part.split("-", 1)
            if len(bounds) != 2 or not bounds[0].isdigit() or not bounds[1].isdigit():
                raise ValueError(
                    f"Invalid cron field '{field_name}': range '{part}' is malformed."
                )
            a, b = int(bounds[0]), int(bounds[1])
            if a < lo or a > hi or b < lo or b > hi:
                raise ValueError(
                    f"Invalid cron field '{field_name}': range '{part}' is outside the allowed range {lo}-{hi}."
                )
        elif part.isdigit():
            n = int(part)
            if n < lo or n > hi:
                raise ValueError(
                    f"Invalid cron field '{field_name}': {n} is outside the allowed range {lo}-{hi}."
                )
        else:
            raise ValueError(
                f"Invalid cron field '{field_name}': '{part}' is not a valid value."
            )


def _validate_cron(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cron expression must be a non-empty string")
    parts = value.split()
    if len(parts) != 5:
        raise ValueError(
            f"cron expression must have 5 fields (got {len(parts)}): {value!r}"
        )
    for (field_name, lo, hi), token in zip(_CRON_FIELD_RANGES, parts):
        _validate_cron_field(field_name, token, lo, hi)


def _validate_positive_int(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"value must be a positive integer (got {value!r})")


def _validate_non_negative_int(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"value must be a non-negative integer (got {value!r})")


def _validate_ratio(value: Any) -> None:
    if not isinstance(value, (int, float)) or value < 0 or value > 1:
        raise ValueError(f"value must be between 0 and 1 (got {value!r})")


def _validate_url(value: Any) -> None:
    if not isinstance(value, str) or not (
        value.startswith("http://") or value.startswith("https://")
    ):
        raise ValueError(f"value must be a http(s) URL (got {value!r})")


def _validate_optional_url(value: Any) -> None:
    if value is None or value == "":
        return
    _validate_url(value)


def _validate_refresh_policy_list(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("value must be a list")
    # F-005-21: ``event`` is a valid pocket refresh policy — a pocket on this
    # policy is re-materialised when the source schema for its model drifts
    # (detected by the scheduler's drift sweep), rather than on a cron. It is
    # accepted as a known value here so a tenant can enable it; aggregates keep
    # their ["schedule","manual"] default list, so this does not change aggregate
    # behaviour.
    allowed = {"schedule", "manual", "event"}
    invalid = [v for v in value if str(v) not in allowed]
    if invalid:
        raise ValueError(
            f"refresh policies must be one of {sorted(allowed)} (invalid={invalid!r})"
        )


def _validate_iso_datetime(value: Any) -> None:
    from datetime import datetime
    if not isinstance(value, str):
        raise ValueError(f"ISO datetime must be a string (got {value!r})")
    try:
        datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid ISO datetime: {value!r} ({exc})") from exc


def _validate_locale(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("locale must be a non-empty string (e.g. 'en-US')")


# ---------------------------------------------------------------------------
# UI groups — the human-friendly top-level panels on the Configuration page.
# ---------------------------------------------------------------------------

_GRP_SECURITY = "Security"
_GRP_SCHEDULER = "Scheduler"
_GRP_NETWORK = "Network timeouts"
_GRP_RATE = "Rate limiting"
_GRP_RESULT = "Query results"
_GRP_FRONTEND = "Frontend"
_GRP_BOOTSTRAP = "Bootstrap (read-only)"
_GRP_AGG = "Aggregates"
_GRP_AI_SCHED = "AI optimizer"
_GRP_POCKET = "Pocket tables"
_GRP_PREDICTIVE = "Predictive aggregates"
_GRP_LIMITS = "Per-model limits"
_GRP_VERSIONS = "Model versions"
# Agent settings ui_groups removed: agent fields persist on
# ProjectAgentConfig and are edited via the project-drawer agent tabs
# directly (not through the registry).


# ---------------------------------------------------------------------------
# SYSTEM — operator-only · 24 surfaced editable + 1 read-only env mirror
# Plus 2 internal AI-timeout knobs kept for operational tuning (surfaced=False).
# ---------------------------------------------------------------------------

_SYSTEM_SETTINGS: list[SettingDef] = [
    # Authentication
    SettingDef(
        key="auth.jwt_expire_minutes",
        level="system", type="int", default=60,
        section="Authentication",
        description="JWT session lifetime in minutes.",
        validator=_validate_positive_int,
        label="Session lifetime",
        ui_group=_GRP_SECURITY, ui_control="number", unit="minutes",
        ui_help="How long a user's login session lasts before they must sign in again.",
    ),

    # Scheduler cadences — operational timing; hidden from the primary UI.
    # Require restart because APScheduler binds job schedules at startup.
    # Defaults are pre-staggered (hours 2-6 UTC, minutes 5/20) and safe.
    SettingDef(
        key="scheduler.refresh_check_minute",
        level="system", type="int", default=5,
        section="Scheduling", restart_required=True,
        description="Minute of every hour at which the refresh sweep runs.",
        validator=_validate_non_negative_int,
        label="Refresh sweep minute",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="minute (0-59)",
        surfaced=False,
    ),
    SettingDef(
        key="scheduler.retirement_hour",
        level="system", type="int", default=3,
        section="Scheduling", restart_required=True,
        description="Hour of day (0-23) when unused-aggregate cleanup runs.",
        validator=_validate_non_negative_int,
        label="Aggregate cleanup",
        ui_group=_GRP_SCHEDULER, ui_control="hour-of-day",
        ui_help="Daily time (UTC) when unused aggregates are cleaned up.",
    ),
    SettingDef(
        key="scheduler.schema_drift_hour",
        level="system", type="int", default=4,
        section="Scheduling", restart_required=True,
        description="Hour of day (0-23) when source-schema check runs.",
        validator=_validate_non_negative_int,
        label="Source schema check",
        ui_group=_GRP_SCHEDULER, ui_control="hour-of-day",
        ui_help="Daily time (UTC) when source database schema changes are detected.",
    ),
    SettingDef(
        key="scheduler.optimizer_sweep_hour",
        level="system", type="int", default=5,
        section="Scheduling", restart_required=True,
        description="Hour of day (0-23) when AI advisor runs.",
        validator=_validate_non_negative_int,
        label="AI advisor",
        ui_group=_GRP_SCHEDULER, ui_control="hour-of-day",
        ui_help="Daily time (UTC) when the AI advisor analyses query patterns and recommends aggregates.",
    ),
    SettingDef(
        key="scheduler.predictive_feedback_hour",
        level="system", type="int", default=6,
        section="Scheduling", restart_required=True,
        description="Hour of day (0-23) when predictive-aggregate validation runs.",
        validator=_validate_non_negative_int,
        label="Predictive aggregate check",
        ui_group=_GRP_SCHEDULER, ui_control="hour-of-day",
        ui_help="Daily time (UTC) when predicted aggregates are validated against real queries.",
    ),
    SettingDef(
        key="scheduler.pocket_eviction_hour",
        level="system", type="int", default=2,
        section="Scheduling", restart_required=True,
        description="Hour of day (UTC) for pocket table cleanup.",
        validator=_validate_non_negative_int,
        label="Pocket table cleanup",
        ui_group=_GRP_SCHEDULER, ui_control="hour-of-day",
        ui_help="Daily time (UTC) when stale pocket tables are removed.",
    ),
    SettingDef(
        key="scheduler.pocket_refresh_minute",
        level="system", type="int", default=20,
        section="Scheduling", restart_required=True,
        description="Minute of each hour for pocket refresh sweep.",
        validator=_validate_non_negative_int,
        label="Pocket refresh sweep minute",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="minute (0-59)",
        surfaced=False,
    ),
    SettingDef(
        key="scheduler.kpi_snapshot_minute",
        level="system", type="int", default=45,
        section="Scheduling", restart_required=True,
        description="Minute of each hour for KPI snapshot sweep.",
        validator=_validate_non_negative_int,
        label="KPI snapshot sweep minute",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="minute (0-59)",
        surfaced=False,
    ),
    SettingDef(
        key="scheduler.kpi_snapshot_purge_hour",
        level="system", type="int", default=6,
        section="Scheduling", restart_required=True,
        description="UTC hour for daily KPI snapshot purge sweep.",
        validator=_validate_non_negative_int,
        label="KPI snapshot purge hour (UTC)",
        ui_group=_GRP_SCHEDULER, ui_control="hour-of-day",
        ui_help="Daily time (UTC) when excess KPI snapshots are purged.",
    ),
    SettingDef(
        key="scheduler.agent_retention_hour",
        level="system", type="int", default=6,
        section="Scheduling", restart_required=True,
        description="UTC hour for the daily agent conversation retention sweep.",
        validator=_validate_non_negative_int,
        label="Agent retention sweep hour (UTC)",
        ui_group=_GRP_SCHEDULER, ui_control="hour-of-day",
        ui_help=(
            "Daily time (UTC) when inactive agent conversations are soft-deleted "
            "per project retention, and conversations soft-deleted beyond the "
            "purge grace period are permanently removed."
        ),
    ),
    SettingDef(
        key="scheduler.agent_purge_grace_days",
        level="system", type="int", default=30,
        section="Scheduling", restart_required=False,
        description=(
            "Days a soft-deleted agent conversation is retained before the "
            "retention sweep hard-purges it. 0 = never auto-purge (default 30)."
        ),
        validator=_validate_non_negative_int,
        label="Agent conversation purge grace period",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="days",
        ui_help=(
            "After an agent conversation is soft-deleted (inactive beyond its "
            "project retention window), it is kept this many extra days so it "
            "can still be recovered, then permanently deleted to reclaim "
            "storage. Set to 0 to keep soft-deleted conversations indefinitely."
        ),
    ),
    SettingDef(
        key="scheduler.misfire_grace_seconds_short",
        level="system", type="int", default=3600,
        section="Scheduling", restart_required=True,
        description="Misfire tolerance window for short-cadence jobs.",
        validator=_validate_positive_int,
        label="Short-job misfire tolerance",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="seconds",
        ui_help="If a short-cadence job is late by more than this, skip it instead of running late.",
        surfaced=False,
    ),
    SettingDef(
        key="scheduler.misfire_grace_seconds_long",
        level="system", type="int", default=3600,
        section="Scheduling", restart_required=True,
        description="Misfire tolerance window for long-cadence jobs.",
        validator=_validate_positive_int,
        label="Long-job misfire tolerance",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="seconds",
        ui_help="If a long-cadence job is late by more than this, skip it instead of running late.",
        surfaced=False,
    ),

    # Gateway HTTP client timeouts — operational; hidden from primary UI.
    SettingDef(
        key="gateway.router_client_timeout_default",
        level="system", type="int", default=15,
        section="Gateway timeouts",
        description="Default downstream HTTP client timeout in seconds.",
        validator=_validate_positive_int,
        label="Default downstream timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        ui_help="Default timeout for HTTP calls between Tessallite services.",
        surfaced=False,
    ),
    SettingDef(
        key="gateway.router_client_timeout_medium",
        level="system", type="int", default=30,
        section="Gateway timeouts",
        description="Medium-weight downstream HTTP client timeout (hierarchy fetch, auth discover).",
        validator=_validate_positive_int,
        label="Medium-weight timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        ui_help="Timeout for medium-weight calls (hierarchy fetch, auth discovery).",
        surfaced=False,
    ),
    SettingDef(
        key="gateway.router_client_timeout_long",
        level="system", type="int", default=60,
        section="Gateway timeouts",
        description="Long-running downstream HTTP client timeout.",
        validator=_validate_positive_int,
        label="Long-running call timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        ui_help="Timeout for longer downstream calls such as list endpoints.",
        surfaced=False,
    ),
    SettingDef(
        key="gateway.router_client_timeout_xlong",
        level="system", type="int", default=120,
        section="Gateway timeouts",
        description="Extra-long downstream HTTP client timeout (large queries).",
        validator=_validate_positive_int,
        label="Extra-long call timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        ui_help="Timeout for the heaviest downstream calls, such as large query results.",
        surfaced=False,
    ),
    SettingDef(
        key="gateway.subtotal_grain_max_concurrency",
        level="system", type="int", default=4,
        section="Gateway timeouts",
        description=(
            "Maximum number of subtotal grain / calc-member re-query SQL calls "
            "the XMLA Execute path fires concurrently. Caps the fan-out a single "
            "deep multi-hierarchy pivot can put on the query-router and source DB."
        ),
        validator=_validate_positive_int,
        label="XMLA subtotal query concurrency",
        ui_group=_GRP_NETWORK, ui_control="number", unit="queries",
        ui_help=(
            "Upper bound on concurrent subtotal/re-query SQL calls per XMLA "
            "Execute. Lower values protect the source DB; higher values speed "
            "deep pivots."
        ),
        surfaced=False,
    ),

    # Control-plane timeouts — operational; hidden from primary UI.
    SettingDef(
        key="control.scheduler_config_timeout",
        level="system", type="int", default=5,
        section="Control plane",
        description="Scheduler config API call timeout.",
        validator=_validate_positive_int,
        label="Scheduler config call timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        surfaced=False,
    ),
    SettingDef(
        key="control.llm_config_timeout",
        level="system", type="int", default=90,
        section="Control plane",
        description="LLM provider configuration API call timeout.",
        validator=_validate_positive_int,
        label="LLM config call timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        surfaced=False,
    ),
    SettingDef(
        key="control.admin_timeout",
        level="system", type="int", default=120,
        section="Control plane",
        description="Admin API call timeout.",
        validator=_validate_positive_int,
        label="Admin call timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        surfaced=False,
    ),

    # AI-run derived timeouts — kept for operational tuning, not surfaced
    # in the primary System UI (see ``surfaced=False``).
    SettingDef(
        key="ai.run_min_timeout",
        level="system", type="int", default=300,
        section="AI",
        description="Minimum timeout for AI optimizer runs.",
        validator=_validate_positive_int,
        label="AI run minimum timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        ui_help="Never use an AI-run timeout below this, even if the derived value is smaller.",
        surfaced=False,
    ),
    SettingDef(
        key="ai.run_timeout_multiplier",
        level="system", type="float", default=5.0,
        section="AI",
        description="Multiplier applied to LLM timeout to derive AI run timeout.",
        label="AI run timeout multiplier",
        ui_group=_GRP_NETWORK, ui_control="number",
        ui_help="AI-run timeout = this multiplier x LLM call timeout.",
        surfaced=False,
    ),

    # Frontend defaults — operational; hidden from primary UI.
    SettingDef(
        key="frontend.endpoint_defaults",
        level="system", type="dict",
        default={
            "model_service_port": 8001,
            "query_router_port": 8002,
            "optimizer_port": 8003,
            "scheduler_port": 8004,
            "gateway_http_port": 8080,
            "gateway_jdbc_port": 5433,
        },
        section="Frontend", restart_required=True,
        description="Default ports surfaced in the EndpointsPanel UI and used by the SPA when no per-service override is set.",
        label="Service port defaults",
        ui_group=_GRP_FRONTEND, ui_control="dict",
        ui_help="Default ports the SPA uses for each backend service when a full-URL override is not set.",
        surfaced=False,
    ),
    SettingDef(
        key="frontend.api_base_overrides",
        level="system", type="dict",
        default={
            "model_service": None,
            "query_router": None,
            "optimizer": None,
            "scheduler": None,
        },
        section="Frontend", restart_required=True,
        description="Per-service base URL overrides for the SPA. null = derive from window.location.",
        label="SPA base URL overrides",
        ui_group=_GRP_FRONTEND, ui_control="dict",
        ui_help="Set a per-service full URL to override what the SPA calls. Leave entries empty to use the current origin.",
        surfaced=False,
    ),

    # Source query timeouts
    SettingDef(
        key="query.connect_timeout_seconds",
        level="system", type="int", default=10,
        section="Query timeouts",
        description="Timeout in seconds for establishing a connection to the source database.",
        validator=_validate_positive_int,
        label="Source connect timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        ui_help="Maximum time to wait when opening a connection to a source database.",
    ),
    SettingDef(
        key="query.statement_timeout_seconds",
        level="system", type="int", default=120,
        section="Query timeouts",
        description="Timeout in seconds for source database query execution.",
        validator=_validate_positive_int,
        label="Query timeout",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        ui_help="Maximum time a query is allowed to run on the source database before being cancelled.",
    ),

    # System-wide query ceilings (model may override; see model section)
    SettingDef(
        key="result.max_rows",
        level="system", type="int", default=100_000,
        section="Result limits",
        description="Hard cap on rows returned by a single query.",
        validator=_validate_positive_int,
        label="Maximum rows per query",
        ui_group=_GRP_RESULT, ui_control="number", unit="rows",
        ui_help="System-wide hard cap on rows returned for one query — excess rows are truncated. Models may override.",
    ),
    SettingDef(
        key="result.chunk_size",
        level="system", type="int", default=5_000,
        section="Result limits",
        description="Streaming chunk size for result delivery.",
        validator=_validate_positive_int,
        label="Streaming chunk size",
        ui_group=_GRP_RESULT, ui_control="number", unit="rows",
        ui_help="Number of rows per streaming batch sent to clients.",
    ),

    # Rate limiting
    SettingDef(
        key="rate_limit.enabled",
        level="system", type="bool", default=True,
        section="Rate limiting",
        description="Whether per-tenant rate limiting is active.",
        label="Enforce tenant query rate limits",
        ui_group=_GRP_RATE, ui_control="switch",
        ui_help="When on, each tenant is limited to the requests-per-minute ceiling below. Excess requests receive HTTP 429. Takes effect without a restart.",
    ),
    SettingDef(
        key="rate_limit.per_minute",
        level="system", type="int", default=60,
        section="Rate limiting",
        description="Maximum requests per tenant per minute.",
        validator=_validate_positive_int,
        label="Requests per tenant per minute",
        ui_group=_GRP_RATE, ui_control="number", unit="requests / minute",
    ),
    SettingDef(
        key="rate_limit.login_per_minute",
        level="system", type="int", default=10,
        section="Rate limiting",
        description="Maximum login attempts per client address per minute.",
        validator=_validate_positive_int,
        label="Login attempts per client per minute",
        ui_group=_GRP_RATE, ui_control="number", unit="attempts / minute",
        ui_help="Stricter ceiling for the login endpoints to slow password guessing. Counted per client address, since login requests carry no tenant identity yet.",
    ),
    SettingDef(
        key="rate_limit.retry_after_seconds",
        level="system", type="int", default=60,
        section="Rate limiting",
        description="Retry-After value sent with 429 responses.",
        validator=_validate_positive_int,
        label="Retry-After delay",
        ui_group=_GRP_RATE, ui_control="number", unit="seconds",
        ui_help="Value returned to clients telling them how long to wait after a rate-limit rejection.",
    ),

    # Bootstrap (read-only display, sourced from .env)
    SettingDef(
        key="meta.bootstrap_env_view",
        level="system", type="dict", default={},
        section="Bootstrap (read-only)",
        env_var="(see .env)",
        description="Read-only mirror of bootstrap env vars, populated at request time.",
        sensitive_display=True,
        label="Bootstrap .env mirror",
        ui_group=_GRP_BOOTSTRAP, ui_control="dict",
    ),

    # -----------------------------------------------------------------
    # LEGACY / OPERATIONAL — kept in the registry so existing call sites
    # continue to resolve, but ``surfaced=False`` keeps them out of the
    # primary System Configuration UI per the 2026-04 restructure.
    #
    #   - xmla.*              → editing UI lives in the model toolbox
    #                           (Endpoints tool); the global default is
    #                           still consumed by the gateway.
    #   - llm.provider_endpoints / llm.anthropic_api_version /
    #     llm.model_name_suggestions → folded into per-row
    #     LLMProviderConfig (project level); kept here as fallback for
    #     legacy call sites that haven't migrated yet.
    #   - pocket.{tenant_scope_from_context,require_tenant_filter,
    #     refresh_every_hours,allowed_refresh_policies}
    #     → no longer user-facing; consumed by pocket matcher.
    #   - source_db.* / agg_target.* / spark.thrift_*
    #     → connection definition lives on per-project ProjectConnection
    #     rows; these system defaults still fill blank fields when a
    #     connection record omits them.
    #   - pocket.* and predictive.* operational defaults retained here
    #     so call sites that read them at system level still work; the
    #     model-level rows with the same key strings take precedence
    #     when set, via resolver fallback.
    # -----------------------------------------------------------------
    SettingDef(
        key="xmla.session_ttl_seconds",
        level="system", type="int", default=10800,
        section="XMLA (legacy)", restart_required=True,
        description="XMLA session TTL (default 3 hours).",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="xmla.session_store_path",
        level="system", type="str", default="/tmp/xmla_sessions.json",
        section="XMLA (legacy)", restart_required=True,
        description="Filesystem path for the XMLA session cache file.",
        surfaced=False,
    ),
    SettingDef(
        key="xmla.metadata_created_at",
        level="system", type="iso_datetime", default="2026-01-01T00:00:00",
        section="XMLA (legacy)",
        description="Static creation timestamp emitted in XMLA metadata responses.",
        validator=_validate_iso_datetime,
        surfaced=False,
    ),
    SettingDef(
        key="xmla.metadata_modified_at",
        level="system", type="iso_datetime", default="2026-03-30T00:00:00",
        section="XMLA (legacy)",
        description="Static modification timestamp emitted in XMLA metadata responses.",
        validator=_validate_iso_datetime,
        surfaced=False,
    ),
    SettingDef(
        key="xmla.datasource_url_fallback",
        level="system", type="str",
        default="http://localhost:8080/api/v1/xmla/",
        section="XMLA (legacy)", restart_required=True,
        description="Fallback datasource URL used when no host is detected from the request.",
        validator=_validate_url,
        surfaced=False,
    ),
    SettingDef(
        key="llm.provider_endpoints",
        level="system", type="dict",
        default={
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com",
            "deepseek": "https://api.deepseek.com/v1",
            "glm": "https://api.z.ai/api/paas/v4",
            "ollama": "http://localhost:11434/v1",
        },
        section="LLM (legacy)",
        description="Per-provider base-URL fallback. Per-project LLMProviderConfig rows override.",
        surfaced=False,
    ),
    SettingDef(
        key="llm.anthropic_api_version",
        level="system", type="str", default="2023-06-01",
        section="LLM (legacy)",
        description="Default 'anthropic-version' header. Per-row LLMProviderConfig.config may override.",
        surfaced=False,
    ),
    SettingDef(
        key="llm.model_name_suggestions",
        level="system", type="dict",
        default={
            "openai": ["gpt-4o"],
            "google": ["gemini-1.5-pro"],
            "anthropic": ["claude-sonnet-4-5-20250514"],
            "deepseek": ["deepseek-chat"],
            "glm": ["glm-4.5-flash", "glm-4.5", "glm-5-turbo", "glm-5.1"],
            "ollama": ["llama3"],
        },
        section="LLM (legacy)",
        description="Default model-name suggestions used when a user adds a provider.",
        surfaced=False,
    ),
    SettingDef(
        key="pocket.enabled",
        level="system", type="bool", default=True,
        section="Pocket tables (legacy)",
        description="Master pocket-table flag. Model rows override via 'pocket.model_enabled'.",
        surfaced=False,
    ),
    SettingDef(
        key="pocket.auto_create_enabled",
        level="system", type="bool", default=False,
        section="Pocket tables (legacy)",
        description="Allow optimizer auto-create at the system fallback. Per-model row overrides.",
        surfaced=False,
    ),
    SettingDef(
        key="pocket.max_rows",
        level="system", type="int", default=1_000_000,
        section="Pocket tables (legacy)",
        description="System fallback for the max-row pocket threshold. Per-model row overrides.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="pocket.min_uci",
        level="system", type="float", default=100.0,
        section="Pocket tables (legacy)",
        description="System fallback for the unified-cost benefit score. Per-model row overrides.",
        surfaced=False,
    ),
    SettingDef(
        key="pocket.min_hits_24h",
        level="system", type="int", default=3,
        section="Pocket tables (legacy)",
        description="System fallback for the 24h-hit threshold. Per-model row overrides.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="pocket.max_ndv_ratio",
        level="system", type="float", default=0.20,
        section="Pocket tables (legacy)",
        description="System fallback for the max distinct-value ratio. Per-model row overrides.",
        validator=_validate_ratio,
        surfaced=False,
    ),
    SettingDef(
        key="pocket.ttl_days",
        level="system", type="int", default=14,
        section="Pocket tables (legacy)",
        description="System fallback for the unused-pocket TTL. Per-model row overrides.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="pocket.refresh_every_hours",
        level="system", type="int", default=6,
        section="Pocket tables (legacy)",
        description="Default schedule-based pocket refresh interval. Aggregates use 'aggregate.default_refresh_interval_hours' instead.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="pocket.invalidating_recovery_hours",
        level="system", type="int", default=6,
        section="Pocket tables (legacy)",
        description="F-005-10: a crash/timeout mid-refresh strands a pocket in 'invalidating' (the matcher excludes it and the sweep skipped it). The refresh sweep re-picks an 'invalidating' pocket once it has been stuck longer than this many hours, so it recovers on the next scheduled run instead of needing a manual 'Refresh now'.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="pocket.allowed_refresh_policies",
        level="system", type="list[str]",
        default=["schedule", "manual", "event"],
        section="Pocket tables (legacy)",
        description="Allowed pocket refresh policies. 'schedule' = cron sweep; 'manual' = Refresh now; 'event' = re-materialise when the source schema for the model drifts (F-005-21, detected by the scheduler drift sweep). Aggregates use the model-level 'aggregate.allowed_refresh_policies' key.",
        validator=_validate_refresh_policy_list,
        surfaced=False,
    ),
    SettingDef(
        key="pocket.require_tenant_filter",
        level="system", type="bool", default=True,
        section="Pocket tables",
        description="Require a tenant-compatible scope before a pocket can match. With 'pocket.tenant_scope_from_context' on (the default), the authenticated tenant-bound session satisfies this, so pockets match out-of-box; turn this off only if you want pockets to match with no tenant scoping at all.",
        surfaced=True,
    ),
    SettingDef(
        key="pocket.tenant_scope_from_context",
        level="system", type="bool", default=True,
        section="Pocket tables",
        description="F-005-05: accept the authenticated tenant context (JWT claim + tenant-scoped DB session) as satisfying 'pocket.require_tenant_filter'. Per-tenant schema isolation already binds the session to one tenant and pockets live in that tenant's schema, so an explicit tenant-named filter on the query body is redundant. Defaults on so pocket matching is not silently inert on typical (non-tenant-column) models; set it off to force an explicit tenant filter on every query.",
        surfaced=True,
    ),
    SettingDef(
        key="predictive.evaluation_window_days",
        level="system", type="int", default=7,
        section="Predictive (legacy)",
        description="System fallback for the predictive feedback evaluation window. Per-model row overrides.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="predictive.validation_min_hits",
        level="system", type="int", default=3,
        section="Predictive (legacy)",
        description="System fallback for the predictive validation hit threshold. Per-model row overrides.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="predictive.unused_retire_days",
        level="system", type="int", default=14,
        section="Predictive (legacy)",
        description="System fallback for the predictive unused retire age. Per-model row overrides.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="source_db.fallback_host",
        level="system", type="str", default="localhost",
        section="Source database (legacy)",
        description="Default source DB host when a connection record omits one.",
        surfaced=False,
    ),
    SettingDef(
        key="source_db.fallback_port",
        level="system", type="int", default=5432,
        section="Source database (legacy)",
        description="Default source DB port when a connection record omits one.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="source_db.fallback_database",
        level="system", type="str", default="postgres",
        section="Source database (legacy)",
        description="Default source DB database name when a connection record omits one.",
        surfaced=False,
    ),
    SettingDef(
        key="agg_target.default_schema",
        level="system", type="str", default="aggregates",
        section="Aggregate target (legacy)",
        description="Default schema where aggregate tables are created when a target does not specify one.",
        surfaced=False,
    ),
    SettingDef(
        key="agg_target.default_dataset",
        level="system", type="str", default="default",
        section="Aggregate target (legacy)",
        description="Default BigQuery dataset for aggregate output.",
        surfaced=False,
    ),
    SettingDef(
        key="agg_target.default_database",
        level="system", type="str", default="public",
        section="Aggregate target (legacy)",
        description="Default database for aggregate output.",
        surfaced=False,
    ),
    SettingDef(
        key="spark.thrift_port",
        level="system", type="int", default=10000,
        section="Spark Thrift (legacy)",
        description="Default Spark Thrift server port when a connection record omits one.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="spark.thrift_database",
        level="system", type="str", default="default",
        section="Spark Thrift (legacy)",
        description="Default Spark Thrift database when a connection record omits one.",
        surfaced=False,
    ),
    SettingDef(
        key="spark.thrift_auth_mode",
        level="system", type="str", default="NOSASL",
        section="Spark Thrift (legacy)",
        description="Default Spark Thrift auth mode (NOSASL, NONE, KERBEROS).",
        surfaced=False,
    ),
]


# ---------------------------------------------------------------------------
# TENANT — empty after the 2026-04 restructure.
# Connections and LLM configurations live at PROJECT level. Conversation
# retention is now project-only (no tenant fallback).
# ---------------------------------------------------------------------------

_GRP_AUDIT = "Audit logging"
_GRP_BRANDING = "Branding"


def _validate_audit_level(value: Any) -> None:
    allowed = {"off", "critical", "warn", "info"}
    if value not in allowed:
        raise ValueError(f"audit log level must be one of {sorted(allowed)}, got {value!r}")


def _validate_retention_days(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"retention days must be an integer, got {value!r}")
    if value < 0:
        raise ValueError(f"retention days must be >= 0 (0 = indefinite), got {value!r}")
    if value != 0 and value < 30:
        raise ValueError(f"minimum retention is 30 days (got {value!r}), or 0 for indefinite")


def _validate_version_retention_count(value: Any) -> None:
    # F-013-17: 0 means keep all (default — behaviour unchanged until an admin
    # opts in). A non-zero value must keep at least 2 so the deployed version
    # and the newest save can never both be pruned away.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"version retention count must be an integer, got {value!r}")
    if value < 0:
        raise ValueError(f"version retention count must be >= 0 (0 = keep all), got {value!r}")
    if value != 0 and value < 2:
        raise ValueError(
            f"minimum version retention is 2 (got {value!r}), or 0 to keep all versions"
        )


_TENANT_SETTINGS: list[SettingDef] = [
    SettingDef(
        key="audit.log_level",
        level="tenant", type="str", default="info",
        section="Audit logging",
        description="Controls which actions are recorded in the audit log.",
        validator=_validate_audit_level,
        label="Audit log level",
        ui_group=_GRP_AUDIT, ui_control="select",
        ui_choices=["off", "critical", "warn", "info"],
        ui_help=(
            "off = disabled; critical = auth failures, security changes, destructive "
            "actions only; warn = adds deploy/undeploy, user changes, settings changes; "
            "info = all CRUD, query execution, connection changes."
        ),
    ),
    SettingDef(
        key="audit.retention_days",
        level="tenant", type="int", default=365,
        section="Audit logging",
        description="How many days to keep audit events before automatic purge. 0 disables purging (keep indefinitely).",
        validator=_validate_retention_days,
        label="Audit retention",
        ui_group=_GRP_AUDIT, ui_control="number", unit="days",
        ui_help="Minimum 30 days, or 0 for indefinite retention. Default is 365 days.",
    ),
    SettingDef(
        key="query_log.retention_days",
        level="tenant", type="int", default=90,
        section="Audit logging",
        description="How many days to keep query logs before automatic purge. 0 disables purging (keep indefinitely).",
        validator=_validate_retention_days,
        label="Query log retention",
        ui_group=_GRP_AUDIT, ui_control="number", unit="days",
        ui_help="Minimum 30 days, or 0 for indefinite retention. Default is 90 days.",
    ),
    SettingDef(
        # F-013-17: bound unbounded version growth. Every Save copies the
        # whole v2 snapshot (glossary, source stats, lifecycle events) into a
        # new JSONB row, so a model edited heavily for months accumulates
        # hundreds of heavyweight rows. This caps how many versions to keep
        # per model. Default 0 = keep all, so existing tenants are unaffected
        # until an admin opts in. When set, the newest N versions plus the
        # currently-deployed version are always retained; older versions are
        # pruned on the next Save.
        key="versions.retention_count",
        level="tenant", type="int", default=0,
        section="Model versions",
        description="How many saved versions to keep per model (0 = keep all).",
        validator=_validate_version_retention_count,
        label="Version retention count",
        ui_group=_GRP_VERSIONS, ui_control="number", unit="versions",
        ui_help=(
            "0 = keep every saved version (default). A value of N keeps the "
            "newest N versions per model plus the currently-deployed version; "
            "older ones are pruned automatically when you Save. Minimum 2."
        ),
    ),

    # Branding
    SettingDef(
        key="branding.logo_url",
        level="tenant", type="str", default="",
        section="Branding",
        description="URL for the tenant logo displayed in the navigation bar.",
        validator=_validate_optional_url,
        label="Logo URL",
        ui_group=_GRP_BRANDING, ui_control="url",
        ui_help="Full URL to the tenant logo image (PNG/SVG recommended).",
    ),
    SettingDef(
        key="branding.primary_color",
        level="tenant", type="str", default="#006C35",
        section="Branding",
        description="Primary theme colour (hex).",
        label="Primary colour",
        ui_group=_GRP_BRANDING, ui_control="text",
        ui_help="Hex colour code for buttons, links, and active states.",
    ),
    SettingDef(
        key="branding.secondary_color",
        level="tenant", type="str", default="#D4AF37",
        section="Branding",
        description="Secondary theme colour (hex).",
        label="Secondary colour",
        ui_group=_GRP_BRANDING, ui_control="text",
        ui_help="Hex colour code for accents and highlights.",
    ),
    SettingDef(
        key="branding.font_family",
        level="tenant", type="str", default="Inter, sans-serif",
        section="Branding",
        description="CSS font-family for UI text.",
        label="Font family",
        ui_group=_GRP_BRANDING, ui_control="text",
        ui_help="CSS font-family value (e.g. 'Inter, sans-serif').",
    ),
    SettingDef(
        key="branding.app_title",
        level="tenant", type="str", default="Tessallite",
        section="Branding",
        description="Application title displayed in the browser tab and header.",
        label="Application title",
        ui_group=_GRP_BRANDING, ui_control="text",
        ui_help="Name shown in the navigation bar and browser tab title.",
    ),
]


# ---------------------------------------------------------------------------
# PROJECT — empty registry. Per-project agent fields live on
# ProjectAgentConfig (edited via the drawer's bespoke agent tabs); per-project
# connections and LLM bundles live on ProjectConnection and LLMProviderConfig
# (edited via the Connections and LLM Configurations tabs). The registry is
# only used here for system-level operator settings.
# ---------------------------------------------------------------------------

_PROJECT_SETTINGS: list[SettingDef] = [
    # Placeholder — kept as a list for symmetry with the other levels.
    # Will be populated again only if a new project-level *primitive* setting
    # appears that doesn't have a dedicated table behind it.
    SettingDef(
        key="agent.__placeholder__",
        level="project", type="str", default="",
        section="(internal)",
        description="Internal placeholder; not surfaced.",
        surfaced=False,
        label="(placeholder)",
    ),
]


# ---------------------------------------------------------------------------
# MODEL — 20 keys, 5 Settings tabs (Aggregates · AI Optimizer · Pocket ·
# Predictive · Limits). Endpoints (XMLA, Spark Thrift) and Source/Target
# pickers live in the model toolbox, NOT in Settings.
# ---------------------------------------------------------------------------

_MODEL_SETTINGS: list[SettingDef] = [
    # Aggregates (3)
    SettingDef(
        key="aggregate.default_cron",
        level="model", type="cron", default="0 2 * * *",
        section="Aggregates",
        description="Default cron used when a new aggregate on this model is created without a schedule.",
        validator=_validate_cron,
        label="Default aggregate schedule",
        ui_group=_GRP_AGG, ui_control="cron",
        ui_help="Schedule picked for new aggregates on this model when you don't pick one.",
    ),
    SettingDef(
        key="aggregate.default_refresh_interval_hours",
        level="model", type="int", default=6,
        section="Aggregates",
        description="Default refresh interval (in hours) for scheduled aggregates on this model.",
        validator=_validate_positive_int,
        label="Default refresh interval",
        ui_group=_GRP_AGG, ui_control="number", unit="hours",
        ui_help="How often a scheduled aggregate refreshes by default, unless an aggregate overrides it.",
    ),
    SettingDef(
        key="aggregate.allowed_refresh_policies",
        level="model", type="list[str]",
        default=["schedule", "manual"],
        section="Aggregates",
        description="Refresh policy modes modelers may pick on aggregates of this model.",
        validator=_validate_refresh_policy_list,
        label="Allowed refresh policies",
        ui_group=_GRP_AGG, ui_control="select-multi",
        ui_choices=["schedule", "manual"],
        ui_help="Which refresh modes are permitted on aggregates of this model. Default = all enabled.",
    ),

    SettingDef(
        key="aggregate.batch_insert_size",
        level="model", type="int", default=20_000,
        section="Aggregates",
        description="Number of rows per INSERT batch during cross-database aggregate materialization.",
        validator=_validate_positive_int,
        label="Cross-database batch insert size",
        ui_group=_GRP_AGG, ui_control="number", unit="rows",
        ui_help=(
            "When source and target databases differ, rows are transferred in batches "
            "of this size. Larger values are faster but use more memory."
        ),
    ),

    # Idle retirement (2)
    SettingDef(
        key="scheduler.idle_retire_days",
        level="model", type="int", default=0,
        section="Aggregates",
        description=(
            "Number of days with zero query hits before an aggregate is retired. "
            "0 = disabled (default)."
        ),
        validator=_validate_non_negative_int,
        label="Idle retirement window",
        ui_group=_GRP_AGG, ui_control="number", unit="days",
        ui_help=(
            "Aggregates with no matching query hits in this many days are automatically "
            "retired. Set to 0 to disable idle retirement."
        ),
    ),
    SettingDef(
        key="scheduler.idle_retire_manual",
        level="model", type="bool", default=False,
        section="Aggregates",
        description="When true, manually-created aggregates are also eligible for idle retirement.",
        label="Retire manual aggregates when idle",
        ui_group=_GRP_AGG, ui_control="switch",
        ui_help=(
            "By default, aggregates you created manually are never idle-retired. "
            "Enable this to allow automatic retirement of manual aggregates too."
        ),
    ),
    SettingDef(
        key="scheduler.retired_purge_days",
        level="model", type="int", default=0,
        section="Aggregates",
        description=(
            "Days an aggregate must stay retired before its physical target "
            "table is dropped to reclaim storage. 0 = never purge (default)."
        ),
        validator=_validate_non_negative_int,
        label="Retired-table purge grace period",
        ui_group=_GRP_AGG, ui_control="number", unit="days",
        ui_help=(
            "Retired aggregates leave their materialised target table behind so "
            "it can be recovered if the aggregate is reinstated. After this many "
            "days the daily sweep drops the table to reclaim storage (real money "
            "on BigQuery/Snowflake). Set to 0 to keep retired tables indefinitely."
        ),
    ),

    # AI optimizer (5)
    SettingDef(
        key="ai_scheduler.cron",
        level="model", type="cron", default="0 5 * * *",
        section="AI optimizer",
        description="Cron at which the AI optimizer is invoked for this model.",
        validator=_validate_cron,
        label="AI optimizer schedule",
        ui_group=_GRP_AI_SCHED, ui_control="cron",
        ui_help="When the AI optimizer should evaluate this model.",
    ),
    SettingDef(
        key="ai_scheduler.lookback_hours",
        level="model", type="int", default=168,
        section="AI optimizer",
        description="Hours of telemetry history considered by the optimizer.",
        validator=_validate_positive_int,
        label="Telemetry lookback window",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="hours",
        ui_help="How far back the optimizer looks when analysing query telemetry.",
    ),
    SettingDef(
        key="ai_scheduler.max_creates_per_run",
        level="model", type="int", default=3,
        section="AI optimizer",
        description="Maximum aggregates the optimizer may create per run.",
        validator=_validate_positive_int,
        label="Max creations per run",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="aggregates",
        ui_help="Ceiling on aggregates the optimizer may create in a single run.",
    ),
    SettingDef(
        key="ai_scheduler.confidence_threshold",
        level="model", type="float", default=0.5,
        section="AI optimizer",
        description="Minimum confidence below which a recommendation is dropped.",
        validator=_validate_ratio,
        label="Minimum confidence threshold",
        ui_group=_GRP_AI_SCHED, ui_control="slider", unit="ratio (0-1)",
        ui_help="Optimizer recommendations below this confidence are ignored.",
    ),
    SettingDef(
        key="ai_scheduler.auto_create_cap_per_sweep",
        level="model", type="int", default=1,
        section="AI optimizer",
        description="Hard cap on aggregates auto-created per optimizer run.",
        validator=_validate_positive_int,
        label="Auto-create cap per run",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="aggregates",
    ),
    SettingDef(
        key="ai_scheduler.prompt_excluded_measures",
        level="model", type="list[str]", default=[],
        section="AI optimizer",
        description=(
            "Measure names hidden from the AI optimiser prompt (e.g. ordinal "
            "helper measures like sort_order or week_no that are not worth "
            "materialising). Empty by default."
        ),
        label="Measures excluded from AI prompt",
        ui_group=_GRP_AI_SCHED, ui_control="text", unit="names",
        ui_help=(
            "Comma- or JSON-list of measure names the AI optimiser should "
            "ignore when recommending aggregates."
        ),
    ),

    # Pocket tables (7)
    SettingDef(
        key="pocket.model_enabled",
        level="model", type="bool", default=True,
        section="Pocket tables",
        description="Enable pocket matching/creation for this model.",
        label="Pocket tables on this model",
        ui_group=_GRP_POCKET, ui_control="switch",
        ui_help="Turn pocket-table matching and creation on or off for this single model.",
    ),
    SettingDef(
        key="pocket.auto_create_enabled",
        level="model", type="bool", default=False,
        section="Pocket tables",
        description="Allow the optimizer to auto-create pocket tables on this model.",
        label="Auto-create pocket tables",
        ui_group=_GRP_POCKET, ui_control="switch",
        ui_help="When on, the optimizer may create new pocket tables on this model on its own.",
    ),
    SettingDef(
        key="pocket.max_rows",
        level="model", type="int", default=1_000_000,
        section="Pocket tables",
        description="Maximum row count allowed for a pocket table candidate on this model.",
        validator=_validate_positive_int,
        label="Maximum pocket row count",
        ui_group=_GRP_POCKET, ui_control="number", unit="rows",
        ui_help="Candidates larger than this never become pocket tables on this model.",
    ),
    SettingDef(
        key="pocket.min_hits_24h",
        level="model", type="int", default=3,
        section="Pocket tables",
        description="Minimum number of matching hits in the last 24 hours on this model.",
        validator=_validate_positive_int,
        label="Minimum 24-hour hit count",
        ui_group=_GRP_POCKET, ui_control="number", unit="hits / 24h",
        ui_help="A candidate must be seen this many times in 24 hours to qualify on this model.",
    ),
    SettingDef(
        key="pocket.min_uci",
        level="model", type="float", default=100.0,
        section="Pocket tables",
        description="Minimum unified-cost score required for pocket candidacy on this model.",
        label="Minimum benefit score",
        ui_group=_GRP_POCKET, ui_control="number",
        ui_help="Candidates below this unified-cost benefit score are rejected on this model.",
    ),
    SettingDef(
        key="pocket.max_ndv_ratio",
        level="model", type="float", default=0.20,
        section="Pocket tables",
        description="Reject candidates whose projected NDV ratio exceeds this threshold on this model.",
        validator=_validate_ratio,
        label="Max distinct-value ratio",
        ui_group=_GRP_POCKET, ui_control="slider", unit="ratio (0-1)",
        ui_help="Reject candidates whose projected distinct-to-total value ratio is above this. Lower means stricter.",
    ),
    SettingDef(
        key="pocket.ttl_days",
        level="model", type="int", default=14,
        section="Pocket tables",
        description="Time-to-live in days before eviction when unused on this model.",
        validator=_validate_positive_int,
        label="Unused-pocket time-to-live",
        ui_group=_GRP_POCKET, ui_control="number", unit="days",
        ui_help="Unused pocket tables are evicted after this many days on this model.",
    ),

    # Predictive aggregates (3)
    SettingDef(
        key="predictive.evaluation_window_days",
        level="model", type="int", default=7,
        section="Predictive aggregates",
        description="Window over which predictive aggregate hits are counted for validation.",
        validator=_validate_positive_int,
        label="Predictive evaluation window",
        ui_group=_GRP_PREDICTIVE, ui_control="number", unit="days",
        ui_help="Lookback window used when counting real-query hits for predictive aggregate validation.",
    ),
    SettingDef(
        key="predictive.validation_min_hits",
        level="model", type="int", default=3,
        section="Predictive aggregates",
        description="Minimum hits within the evaluation window for a predictive aggregate to be marked validated.",
        validator=_validate_positive_int,
        label="Predictive validation hit threshold",
        ui_group=_GRP_PREDICTIVE, ui_control="number", unit="hits",
        ui_help="A predicted aggregate is marked validated once it gets this many real-query hits inside the window.",
    ),
    SettingDef(
        key="predictive.unused_retire_days",
        level="model", type="int", default=14,
        section="Predictive aggregates",
        description="Predictive aggregates with zero hits older than this are auto-retired.",
        validator=_validate_positive_int,
        label="Predictive unused retire age",
        ui_group=_GRP_PREDICTIVE, ui_control="number", unit="days",
        ui_help="Predictive aggregates that age past this with zero real-query hits are automatically retired.",
    ),

    # Per-model limits (3) — same key strings as their system counterparts;
    # resolver picks model value first, then falls back to system.
    SettingDef(
        key="query.statement_timeout_seconds",
        level="model", type="int", default=None,
        section="Limits",
        description=(
            "Per-model override for the statement timeout. Leave unset "
            "(None) for no model-level cap — the system-wide value applies."
        ),
        validator=lambda v: None if v is None else _validate_positive_int(v),
        label="Query timeout (model override)",
        ui_group=_GRP_LIMITS, ui_control="number", unit="seconds",
        ui_help="Override the system-wide statement timeout for queries on this model. Default = no model-level limit.",
    ),
    SettingDef(
        key="result.max_rows",
        level="model", type="int", default=None,
        section="Limits",
        description=(
            "Per-model override for the maximum rows returned by a single query "
            "on this model. Leave unset (None) for no model-level cap — the "
            "system-wide value applies."
        ),
        validator=lambda v: None if v is None else _validate_positive_int(v),
        label="Max rows per query (model override)",
        ui_group=_GRP_LIMITS, ui_control="number", unit="rows",
        ui_help="Override the system-wide row cap for queries on this model. Default = no model-level limit.",
    ),
    SettingDef(
        key="rate_limit.per_minute",
        level="model", type="int", default=None,
        section="Limits",
        description=(
            "Per-model override for the rate-limit ceiling. Leave unset for no "
            "model-level cap — the tenant/system value applies."
        ),
        validator=lambda v: None if v is None else _validate_positive_int(v),
        label="Requests per minute (model override)",
        ui_group=_GRP_LIMITS, ui_control="number", unit="requests / minute",
        ui_help="Override the system-wide rate-limit ceiling for this model. Default = no model-level limit.",
    ),
]


# ---------------------------------------------------------------------------
# Registry — flat dict keyed by (level, key)
# ---------------------------------------------------------------------------

REGISTRY: dict[tuple[Level, str], SettingDef] = {}

for _defs, _level in (
    (_SYSTEM_SETTINGS, "system"),
    (_TENANT_SETTINGS, "tenant"),
    (_PROJECT_SETTINGS, "project"),
    (_MODEL_SETTINGS, "model"),
):
    for _d in _defs:
        if _d.level != _level:
            raise RuntimeError(
                f"Setting {_d.key!r} declared at level {_d.level!r} but registered in {_level!r} list"
            )
        REGISTRY[(_level, _d.key)] = _d


def get_def(key: str, level: Level) -> SettingDef:
    try:
        return REGISTRY[(level, key)]
    except KeyError as exc:
        raise KeyError(f"unknown setting at level {level!r}: {key!r}") from exc


def all_for_level(level: Level) -> list[SettingDef]:
    return [d for (lvl, _k), d in REGISTRY.items() if lvl == level]


def all_keys_for_level(level: Level) -> list[str]:
    return [d.key for d in all_for_level(level)]


def has_key(key: str, level: Level) -> bool:
    return (level, key) in REGISTRY


def surfaced_for_level(level: Level) -> list[SettingDef]:
    """Return only the settings that should be visible on the level's primary
    configuration UI (``surfaced=True``). Operational levers
    (``surfaced=False``) are excluded.
    """
    return [d for d in all_for_level(level) if d.surfaced]


def coerce(value: Any, definition: SettingDef) -> Any:
    """Coerce a stored or incoming value to the registry-declared type.

    JSONB returns native Python types, but UI/API inputs may arrive as
    strings. Coercion runs before the validator on writes and before
    return on reads.
    """
    if value is None:
        return None
    t = definition.type
    if t == "int":
        if isinstance(value, bool):
            raise ValueError(f"expected int, got bool for {definition.key}")
        return int(value)
    if t == "float":
        if isinstance(value, bool):
            raise ValueError(f"expected float, got bool for {definition.key}")
        return float(value)
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    if t in ("str", "cron", "iso_datetime"):
        return str(value)
    if t == "list[str]":
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        raise ValueError(f"cannot coerce {value!r} to list[str]")
    if t == "dict":
        if isinstance(value, dict):
            return value
        raise ValueError(f"cannot coerce {value!r} to dict")
    raise ValueError(f"unknown type {t!r}")


def validate(value: Any, definition: SettingDef) -> None:
    """Run the registered validator. Coerce first."""
    if value is None:
        return  # null = unset, falls through in the resolver
    coerced = coerce(value, definition)
    if definition.validator is not None:
        definition.validator(coerced)
