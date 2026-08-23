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

from shared.semantic.fiscal_year_labels import (
    DEFAULT_FISCAL_YEAR_LABEL_FORMAT,
    FISCAL_YEAR_LABEL_FORMATS,
    validate_fiscal_year_label_format,
)


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


def _validate_positive_float(value: Any) -> None:
    """Strictly positive rate. Zero is rejected, not merely discouraged.

    Bug-9076: these are ROI cost rates. A zero storage or compute rate makes
    every candidate look free, so the ranker would spend the scarce create slot
    on whichever aggregate is largest. Refuse it at the settings boundary rather
    than compensating for it in the estimator.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"value must be a positive number (got {value!r})")


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


def _validate_derived_routing_mode(value: Any) -> None:
    # Spec §15.1: off | shadow | serve. Default off. No "trust declared" mode.
    allowed = {"off", "shadow", "serve"}
    if value not in allowed:
        raise ValueError(
            f"derived expression routing mode must be one of {sorted(allowed)}, got {value!r}"
        )


def _validate_quantile_proof_mode(value: Any) -> None:
    # Spec §15: off | shadow | enforce. Default off — the pNN aggregate serving
    # feature is inert until a tenant/model explicitly enables it after coverage
    # backfill and live known-answer gates. ``off`` = quantile queries behave
    # exactly as before the feature landed (MEDIAN via the existing path,
    # non-median pNN to source); ``enforce`` = coverage-gated pNN serving.
    # ``shadow`` is RESERVED: the shadow evaluator (compute+log proofs while
    # source still executes, spec §15 step 3) is NOT yet implemented, so shadow
    # currently behaves identically to ``off``. It is accepted so the setting
    # vocabulary matches the spec, but an operator MUST NOT read a quiet shadow
    # run as enforce-readiness until the evaluator lands (Fable R2 MEDIUM-2).
    allowed = {"off", "shadow", "enforce"}
    if value not in allowed:
        raise ValueError(
            f"quantile proof mode must be one of {sorted(allowed)}, got {value!r}"
        )


def _validate_population_reason_mode(value: Any) -> None:
    # Bug-8789: legacy | split. Default legacy — the single
    # ``join_population_mismatch`` code every existing log pipeline and
    # optimizer dashboard already consumes. ``split`` opts into the two
    # sub-codes (``population_plan_mismatch`` / ``population_unprovable_model``).
    allowed = {"legacy", "split"}
    if value not in allowed:
        raise ValueError(
            f"population mismatch reason mode must be one of {sorted(allowed)}, got {value!r}"
        )


def _validate_derived_auto_build_mode(value: Any) -> None:
    # Spec §15.1: off | approval | automatic. Default off.
    allowed = {"off", "approval", "automatic"}
    if value not in allowed:
        raise ValueError(
            f"derived expression auto-build mode must be one of {sorted(allowed)}, got {value!r}"
        )


def _validate_judge_context_mode(value: Any) -> None:
    # R2 (F2): distilled | full. Default distilled (spec section 7 escape
    # hatch). 'distilled' strips non-evidential planner boilerplate from the
    # judge evidence pack; 'full' reverts to the verbatim planner system prompt.
    allowed = {"distilled", "full"}
    if value not in allowed:
        raise ValueError(
            f"judge context mode must be one of {sorted(allowed)}, got {value!r}"
        )


#: Bug-8615 (join population governance, G5). The row-effect fraction at or
#: below which a non-neutral, undeclared join is WARNING; above it the model
#: rolls up to BLOCKED and the measured policy row refuses deployment. 1% is
#: the governance plan's stated threshold.
#: Declared here — the registry is the single source of truth for every knob —
#: and re-exported by ``shared.semantic.join_population_validator`` as
#: ``DEFAULT_ROW_EFFECT_WARNING_THRESHOLD`` so the classifier never carries a
#: magic number. Not imported FROM that module: it imports this one.
JOIN_POPULATION_ROW_EFFECT_THRESHOLD_DEFAULT: float = 0.01

#: Wall-clock budget for classifying ONE model's joins at deploy. Deploy is a
#: synchronous request and a wide model can declare dozens of joins at 2-3
#: aggregate statements each, so an unbounded pass would let a merely-slow
#: source hold a deploy open for joins x 3 x the source statement timeout.
#: Once spent, the remaining joins record an unmeasured verdict (the rollup then
#: reports evaluated=false) instead of the deploy stalling. 0 disables the bound.
JOIN_POPULATION_PROBE_BUDGET_SECONDS_DEFAULT: float = 60.0


def _validate_join_population_mode(value: Any) -> None:
    # on | off. Default on. Off does not touch the source: it still clears the
    # previous verdicts and records a conservative unmeasured one per join, so
    # the model's join population status reads UNEVALUATED rather than stale.
    # Off never measures, so its rows remain unmeasured and cannot block.
    allowed = {"on", "off"}
    if value not in allowed:
        raise ValueError(
            f"join population validation mode must be one of {sorted(allowed)}, got {value!r}"
        )


def _validate_join_population_threshold(value: Any) -> None:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"join population row-effect threshold must be a number, got {value!r}"
        ) from None
    if not (0.0 <= ratio <= 1.0):
        raise ValueError(
            "join population row-effect threshold is a fraction of rows and must "
            f"be between 0 and 1, got {ratio!r}"
        )


def _validate_join_population_budget(value: Any) -> None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"join population probe budget must be a number of seconds, got {value!r}"
        ) from None
    if seconds < 0:
        raise ValueError(
            f"join population probe budget cannot be negative, got {seconds!r}"
        )


def _validate_attribute_verification_mode(value: Any) -> None:
    # Spec §15.1: on | off. Default on. Disabling makes all data-verified edges
    # UNAVAILABLE (fail-closed source route); it never preserves old trust.
    allowed = {"on", "off"}
    if value not in allowed:
        raise ValueError(
            f"attribute relationship verification mode must be one of {sorted(allowed)}, got {value!r}"
        )


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
_GRP_DERIVED = "Derived-grain routing"
# Agent settings ui_groups removed: agent fields persist on
# ProjectAgentConfig and are edited via the project-drawer agent tabs
# directly (not through the registry).


# ---------------------------------------------------------------------------
# SYSTEM — operator-only · 25 surfaced editable + 1 read-only env mirror
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
        key="scheduler.kpi_snapshot_eval_timeout_seconds",
        level="system", type="int", default=30,
        section="Scheduling", restart_required=True,
        description=(
            "Per-model HTTP timeout (seconds) for the KPI snapshot sweep's "
            "evaluate-batch call to the model-service. Must stay well below the "
            "service token lifetime so a slow model can never outlive its token."
        ),
        validator=_validate_positive_int,
        label="KPI snapshot evaluate timeout (s)",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="seconds",
        surfaced=False,
    ),
    SettingDef(
        key="scheduler.ai_run_dispatch_minute",
        level="system", type="str", default="*",
        section="Scheduling", restart_required=True,
        description=(
            "Bug-8034: cron minute specification for the durable AI advisor "
            "dispatch sweep, which starts advisor runs accepted as 'queued'. "
            "'*' means every minute."
        ),
        label="AI advisor dispatch sweep minute",
        ui_group=_GRP_SCHEDULER, ui_control="text",
        surfaced=False,
    ),
    SettingDef(
        key="scheduler.ai_run_dispatch_timeout_seconds",
        level="system", type="int", default=15,
        section="Scheduling", restart_required=False,
        description=(
            "Bug-8034: HTTP timeout (seconds) for the dispatch sweep's call to "
            "the optimizer's internal execute route. The call only starts the "
            "run; it does not wait for the advisor to finish."
        ),
        validator=_validate_positive_int,
        label="AI advisor dispatch timeout (s)",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="seconds",
        surfaced=False,
    ),
    SettingDef(
        key="scheduler.ai_run_dispatch_max_age_seconds",
        level="system", type="int", default=3600,
        section="Scheduling", restart_required=False,
        description=(
            "Bug-8034: a queued AI advisor run that has still not been "
            "dispatched this many seconds after acceptance is marked failed "
            "instead of being retried forever."
        ),
        validator=_validate_positive_int,
        label="AI advisor dispatch max queue age (s)",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="seconds",
        surfaced=False,
    ),
    SettingDef(
        key="optimizer.ai_run_claim_grace_seconds",
        level="system", type="int", default=120,
        section="Scheduling", restart_required=False,
        description=(
            "Bug-8034 / review F-559-02: how long a freshly dispatcher-claimed "
            "AI advisor run is protected from the optimizer's orphan janitor. "
            "The janitor treats a free per-model lock as proof of a dead "
            "executor, but a run claimed seconds ago has not reached its lock "
            "yet. During a rolling deploy the starting optimizer would "
            "otherwise terminal-fail a run the scheduler just legitimately "
            "claimed. Raise it only if hand-offs are slower than this."
        ),
        validator=_validate_positive_int,
        label="AI advisor claim grace (s)",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="seconds",
        surfaced=False,
    ),
    SettingDef(
        key="optimizer.ai_run_janitor_interval_seconds",
        level="system", type="int", default=300,
        section="Scheduling", restart_required=True,
        description=(
            "Bug-8034 / review F-559-01-R2: how often the optimizer re-runs its "
            "orphan-run janitor. The janitor also ran at startup only, so a run "
            "claimed just before its dispatcher died was skipped by the claim "
            "grace and then never revisited — it stayed 'running' forever and "
            "blocked every later run of that model. Must be shorter than the "
            "delay operators will accept before a dead claim is reconciled."
        ),
        validator=_validate_positive_int,
        label="AI advisor orphan janitor interval (s)",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="seconds",
        surfaced=False,
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
        section="Scheduling", restart_required=True,
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
        key="scheduler.webhook_delivery_retention_days",
        level="system", type="int", default=30,
        section="Scheduling", restart_required=True,
        description=(
            "Bug-6316: days a terminal webhook delivery row (delivered or "
            "dead-lettered) is retained before the retention sweep purges it. "
            "0 = never auto-purge. In-flight (pending) deliveries are never "
            "purged regardless of age."
        ),
        validator=_validate_non_negative_int,
        label="Webhook delivery retention period",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="days",
        ui_help=(
            "Delivered and dead-lettered webhook delivery records are kept this "
            "many days so their history can be inspected, then permanently "
            "deleted to reclaim storage. Set to 0 to keep them indefinitely. "
            "Pending (not-yet-delivered) records are never purged."
        ),
    ),
    SettingDef(
        key="scheduler.notification_delivery_retention_days",
        level="system", type="int", default=30,
        section="Scheduling", restart_required=True,
        description=(
            "Bug-8385: days an email/Slack notification delivery record is "
            "retained before the retention sweep purges it. 0 = never "
            "auto-purge. Every record is a terminal outcome (sent, or failed "
            "including a misconfiguration skip), so none is in flight."
        ),
        validator=_validate_non_negative_int,
        label="Notification delivery retention period",
        ui_group=_GRP_SCHEDULER, ui_control="number", unit="days",
        ui_help=(
            "Records of email and Slack notification attempts are kept this "
            "many days so a channel that has been failing can be inspected, "
            "then permanently deleted to reclaim storage. Set to 0 to keep "
            "them indefinitely."
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
        key="gateway.login_retry_attempts",
        level="system", type="int", default=1,
        section="Gateway timeouts",
        description="Extra retry attempts for gateway login relays after a transport timeout (Cloud Run cold start).",
        validator=_validate_non_negative_int,
        label="Login retry attempts",
        ui_group=_GRP_NETWORK, ui_control="number", unit="attempts",
        ui_help="How many times the gateway retries a credential login after a downstream timeout before failing the request.",
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
    # Bug-7043: CLS catalogue staleness TTL. After CLS configuration changes
    # a JDBC connection's cached catalogue may list columns the persona no
    # longer has access to (or omit newly-allowed columns). This TTL controls
    # how often the gateway re-fetches model metadata to refresh the catalogue.
    # Default 0 = re-validate on every catalogue query (fail-closed for CLS
    # tightening). Higher values reduce model-service load at the cost of a
    # longer stale-catalogue window. Value is in seconds.
    SettingDef(
        key="gateway.catalogue_cls_ttl",
        level="system", type="int", default=0,
        section="Gateway security",
        description=(
            "Maximum age (seconds) of the JDBC catalogue before re-fetching "
            "model metadata to pick up CLS changes. 0 = re-validate on every "
            "catalogue query (most secure)."
        ),
        validator=_validate_non_negative_int,
        label="Catalogue CLS refresh TTL",
        ui_group=_GRP_NETWORK, ui_control="number", unit="seconds",
        ui_help=(
            "How long a JDBC connection may serve cached catalogue metadata "
            "before re-checking persona column restrictions. Set to 0 for "
            "immediate CLS enforcement in the field list; increase to reduce "
            "model-service load for tenants that rarely change CLS."
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
        section="AI", restart_required=True,
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
        section="AI", restart_required=True,
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

    # Derived-grain aggregate routing (spec §15.1). Defaults are the safe
    # fully-off state: no derived-expression proof reaches matcher/rewrite, and
    # ordinary source/aggregate routing is byte-identical to pre-feature. These
    # are operational gates staged on per the spec's rollout order, so they are
    # not surfaced on the primary System UI until serving stages are certified.
    # DEPRECATED as a serve gate (spec architecture_derived-grain-operational-
    # serving.md §A). Serving is now governed by the operational kill-switch
    # ``query.derived_expression_serving_enabled`` (default ON) AND per-relationship
    # health — NOT by a stored "serve" value. This key is retained ONLY for the
    # cheap shadow/observe capability (``shadow`` builds proofs + compares without
    # serving); ``serve`` is no longer read as a precondition anywhere.
    SettingDef(
        key="query.derived_expression_routing_mode",
        level="system", type="str", default="off",
        section="Derived-grain routing",
        description=(
            "DEPRECATED / currently inert — serving is governed by "
            "'derived_expression_serving_enabled' (kill-switch) + relationship "
            "health, and no runtime path reads this key anymore. Retained only as "
            "a placeholder for a future shadow/observe capability (the shadow "
            "evaluator is not wired). 'serve' is no longer a precondition. "
            "Default off."
        ),
        validator=_validate_derived_routing_mode,
        label="Derived expression shadow mode (deprecated serve gate)",
        ui_group=_GRP_DERIVED, ui_control="select",
        ui_choices=["off", "shadow", "serve"],
        ui_help=(
            "Legacy observe-only control. Serving is now controlled by the "
            "'Derived relabel serving enabled' kill-switch and continuous "
            "relationship health, not by this setting."
        ),
        surfaced=True,
    ),
    # Operational serving kill-switch (spec: architecture_derived-grain-operational-
    # serving.md §A). This is the master enable for derived-grain relabel serving.
    # It DEFAULTS TO ENABLED and is an operator safety valve, NOT a turn-on gate:
    # a served relabel additionally requires the relationship to be CURRENTLY healthy
    # (VERIFIED artifact-local evidence bound to the active run — the router trust
    # predicate). Setting this OFF disables ALL relabel serving instantly (queries
    # fall back to ordinary aggregate / source routing, byte-identical). The manual
    # ``derived_expression_routing_mode == serve`` precondition is REMOVED; this
    # replaces it as the single system-level control.
    SettingDef(
        key="query.derived_expression_serving_enabled",
        level="system", type="bool", default=True,
        section="Derived-grain routing",
        description=(
            "Master enable (kill-switch) for derived-grain relabel serving. "
            "Default ON. A relabel serves only when this is ON AND the "
            "relationship is currently healthy (continuously re-verified by the "
            "sweep). Turn OFF to disable all relabel serving immediately."
        ),
        label="Derived relabel serving enabled",
        ui_group=_GRP_DERIVED, ui_control="switch",
        ui_help=(
            "When on, verified dimension attribute relationships are served from "
            "aggregates whenever the periodic health sweep currently confirms the "
            "1:1 mapping holds on the served data. Turn off as an instant safety "
            "valve to route every such query the ordinary way."
        ),
    ),
    SettingDef(
        key="query.derived_expression_shadow_sample_rate",
        level="system", type="float", default=0.0,
        section="Derived-grain routing",
        description=(
            "Fraction (0..1) of eligible queries for which shadow mode runs the "
            "bounded source-vs-artifact comparison. 0 disables comparison even "
            "in shadow mode. Default 0."
        ),
        validator=_validate_ratio,
        label="Derived shadow sample rate",
        ui_group=_GRP_DERIVED, ui_control="slider", unit="ratio (0-1)",
        ui_help=(
            "How often, in shadow mode, the derived proof result is compared "
            "against the authoritative source result. Higher values give more "
            "coverage at more cost."
        ),
        surfaced=False,
    ),
    SettingDef(
        key="query.derived_expression_registry_version",
        level="system", type="str", default="v0",
        section="Derived-grain routing",
        description=(
            "Version tag of the function-semantics registry "
            "(derived_expression_semantics.json) in force. Participates in every "
            "derived proof's compatibility hash; a change quarantines / rebuilds "
            "affected artifact-owned derived keys before serving (spec §12, §17)."
        ),
        label="Derived registry version",
        ui_group=_GRP_DERIVED, ui_control="text",
        surfaced=False,
    ),
    SettingDef(
        key="model.attribute_relationship_verifier_version",
        level="system", type="str", default="v0",
        section="Derived-grain routing",
        description=(
            "Version tag of the shared attribute-relationship verifier. Bound "
            "into every verification evidence row; the router trust predicate "
            "rejects evidence written by a non-accepted verifier version "
            "(spec §7.6.4)."
        ),
        label="Attribute verifier version",
        ui_group=_GRP_DERIVED, ui_control="text",
        surfaced=False,
    ),
    # Bug-8615 phase G1. SYSTEM level on purpose: the threshold is the platform's
    # governance policy, not a per-model dial a modeller could raise to 1.0 to
    # silence their own BLOCKED finding. Same shape as the verifier version above
    # — a ``model.``-prefixed key that lives at system scope.
    SettingDef(
        key="model.join_population_row_effect_threshold",
        level="system", type="float",
        default=JOIN_POPULATION_ROW_EFFECT_THRESHOLD_DEFAULT,
        section="Aggregates",
        description=(
            "System row-effect threshold for deploy-time join governance. A "
            "measured, unresolved UNDECLARED effect above this value is BLOCKED; "
            "a filtering ENRICHMENT_ONLY effect is treated the same way. "
            "0.01 = 1% of rows. preserve_base_rows and unmeasured checks never "
            "block."
        ),
        validator=_validate_join_population_threshold,
        label="Join population deploy threshold",
        ui_group=_GRP_AGG, ui_control="number", unit="fraction of rows",
        ui_help=(
            "How much row loss or duplication a join may cause before an "
            "undeclared join is escalated from a warning to a blocking finding. "
            "0.01 means one percent of rows."
        ),
        surfaced=False,
    ),
    SettingDef(
        key="model.join_population_probe_budget_seconds",
        level="system", type="float",
        default=JOIN_POPULATION_PROBE_BUDGET_SECONDS_DEFAULT,
        section="Aggregates",
        description=(
            "Wall-clock budget for classifying ONE model's joins at deploy. "
            "Deploy is a synchronous request; once this is spent the remaining "
            "joins are recorded as unmeasured (the model's status reports "
            "evaluated=false) rather than holding the deploy open. 0 disables "
            "the bound."
        ),
        validator=_validate_join_population_budget,
        label="Join population probe budget",
        ui_group=_GRP_AGG, ui_control="number", unit="seconds",
        ui_help=(
            "How long the deploy-time join check may spend querying the source "
            "for one model before it stops and reports the rest as unchecked."
        ),
        surfaced=False,
    ),

    # Rate limiting
    SettingDef(
        key="rate_limit.enabled",
        level="system", type="bool", default=False,
        section="Rate limiting",
        description="Whether per-tenant rate limiting is active.",
        label="Enforce tenant query rate limits",
        ui_group=_GRP_RATE, ui_control="switch",
        ui_help="When on, each tenant is limited to the requests-per-minute ceiling below. Excess requests receive HTTP 429. Takes effect without a restart.",
    ),
    # Per-tenant HTTP-ingress bucket for the blanket TenantRateLimitMiddleware.
    # Placement decision (docs/architecture/architecture_rate-limit-placement.md,
    # user 2026-08-14): this middleware is attached on the GATEWAY ONLY — where
    # BI-client user queries enter — and no longer on the model-service /
    # agent-service operational API. Default reconciled 600 -> 120: the 600 was
    # a prior compromise to stop the model builder's operational-DB metadata
    # fan-out (one /tables/{id}/attributes call per table) from 429-ing on
    # model-open — the wrong axis, now moot because the operational API is no
    # longer throttled. 120 aligns with the gateway's dedicated per-query ceiling
    # (gateway.query_rate_limit_per_minute, also 120): well above a single BI
    # client's legitimate burst, but a bounded per-tenant abuse backstop.
    SettingDef(
        key="rate_limit.per_minute",
        level="system", type="int", default=120,
        section="Rate limiting",
        description=(
            "Maximum HTTP requests per tenant per minute on the gateway user-query "
            "bucket. The blanket per-tenant limiter is attached on the gateway only "
            "(where BI-client queries enter), not on the operational/metadata API."
        ),
        validator=_validate_positive_int,
        label="Requests per tenant per minute",
        ui_group=_GRP_RATE, ui_control="number", unit="requests / minute",
        ui_help="Per-tenant ceiling for user-facing requests entering the gateway. Excess requests receive HTTP 429. Governs the gateway user-query surface only; operational/metadata reads (model CRUD, builder loads) are not throttled by this limit. Actual JDBC/XMLA queries are additionally bounded by the dedicated gateway query rate limit.",
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
    # Bug-7231: ad-hoc KPI evaluation runs a live source query on every wizard
    # preview keystroke — expensive and unsaved. This per-USER ceiling caps that
    # storm independently of the coarse per-tenant ingress limit above. 0
    # disables the cap. Enforced via the bounded/shared action-quota engine.
    SettingDef(
        key="rate_limit.adhoc_kpi_per_minute",
        level="system", type="int", default=30,
        section="Rate limiting",
        description="Maximum ad-hoc KPI preview evaluations per user per minute (0 disables).",
        validator=_validate_non_negative_int,
        label="Ad-hoc KPI previews per user per minute",
        ui_group=_GRP_RATE, ui_control="number", unit="previews / minute",
        ui_help="Caps the live KPI wizard preview per user. Each preview runs a real query against the source, so an unbounded preview loop can storm the source database. 0 turns the cap off.",
    ),

    # Bug-6324: notification test-send abuse controls. "Send a test alert" is
    # a modeler-level action that puts arbitrary recipient addresses behind the
    # PLATFORM's verified SMTP identity. Unbounded, that is an open relay: a
    # single compromised modeler account can burn the sending reputation every
    # tenant depends on. These ceilings are per project and per hour.
    SettingDef(
        key="notifications.test_send_per_hour",
        level="system", type="int", default=10,
        section="Rate limiting",
        description=(
            "Maximum notification test-sends per project per hour. 0 disables "
            "test-sends entirely."
        ),
        validator=_validate_non_negative_int,
        label="Notification test-sends per project per hour",
        ui_group=_GRP_RATE, ui_control="number", unit="sends / hour",
        ui_help=(
            "Test alerts are delivered through the platform's own SMTP sender. "
            "This ceiling stops one project from using it as an open relay and "
            "damaging deliverability for every tenant. Set to 0 to switch test "
            "sends off."
        ),
    ),
    SettingDef(
        key="notifications.test_send_per_hour_tenant",
        level="system", type="int", default=30,
        section="Rate limiting",
        description=(
            "Maximum notification test-sends per TENANT per hour. 0 disables "
            "the tenant ceiling (the per-project one still applies)."
        ),
        validator=_validate_non_negative_int,
        label="Notification test-sends per tenant per hour",
        ui_group=_GRP_RATE, ui_control="number", unit="sends / hour",
        ui_help=(
            "A per-project ceiling alone does not bound the harm: an admin who "
            "can create projects can multiply it. This bounds the tenant."
        ),
    ),
    SettingDef(
        key="notifications.test_send_per_hour_platform",
        level="system", type="int", default=200,
        section="Rate limiting",
        description=(
            "Maximum notification test-sends across the WHOLE platform per "
            "hour. 0 disables the platform ceiling."
        ),
        validator=_validate_non_negative_int,
        label="Notification test-sends per platform per hour",
        ui_group=_GRP_RATE, ui_control="number", unit="sends / hour",
        ui_help=(
            "The damage being bounded -- SMTP sender reputation -- is shared by "
            "every tenant, so the last ceiling has to be platform-wide. Note "
            "that with the default in-memory limiter store these counts are "
            "per replica; point RATE_LIMIT_STORAGE_URI at a shared store to "
            "enforce one ceiling across all of them."
        ),
    ),
    SettingDef(
        key="notifications.test_max_recipients",
        level="system", type="int", default=5,
        section="Rate limiting",
        description="Maximum recipient addresses accepted in one test-send.",
        validator=_validate_positive_int,
        label="Recipients per notification test-send",
        ui_group=_GRP_RATE, ui_control="number", unit="recipients",
    ),
    SettingDef(
        key="notifications.recipient_domain_allowlist",
        level="system", type="str", default="",
        section="Rate limiting",
        description=(
            "Comma-separated email domains that notification test-sends may "
            "target. Empty means any domain is accepted."
        ),
        label="Allowed notification recipient domains",
        ui_group=_GRP_RATE, ui_control="text",
        ui_help=(
            "Leave empty unless you want to confine test alerts to your own "
            "domains (e.g. example.com,corp.example.com). Matching is on the "
            "part after the @, case-insensitive, and includes subdomains."
        ),
    ),

    # Bug-7745: gateway query byte ceiling + rate limit (DoS / exfiltration defence)
    SettingDef(
        key="gateway.query_byte_ceiling",
        level="system", type="int", default=50 * 1024 * 1024,
        section="Gateway security",
        description="Maximum response payload bytes per query on the public gateway path. 0 disables.",
        validator=_validate_non_negative_int,
        label="Query byte ceiling",
        ui_group=_GRP_SECURITY, ui_control="number", unit="bytes",
        ui_help="Hard cap on the response payload size (bytes) for a single query through the gateway. Queries whose response exceeds this ceiling are rejected fail-closed. 0 disables the ceiling.",
    ),
    SettingDef(
        key="gateway.query_rate_limit_per_minute",
        level="system", type="int", default=120,
        section="Gateway security",
        description="Maximum queries per tenant per minute through the JDBC/XMLA gateway. 0 disables.",
        validator=_validate_non_negative_int,
        label="Gateway query rate limit",
        ui_group=_GRP_RATE, ui_control="number", unit="queries / minute",
        ui_help="Per-tenant sliding-window rate limit for queries arriving through the JDBC and XMLA gateway endpoints. Excess queries are rejected. 0 disables. Per-replica: effective limit is N x configured under multi-replica scale-out.",
    ),
    # Bug-8108: JDBC extended-protocol portal row-buffer cap
    SettingDef(
        key="gateway.jdbc_portal_row_buffer_cap",
        level="system", type="int", default=50_000,
        section="Gateway security",
        description="Maximum rows a single JDBC extended-protocol portal buffers in gateway memory before Describe/Execute pages it out. 0 disables.",
        validator=_validate_non_negative_int,
        label="JDBC portal row buffer cap",
        ui_group=_GRP_SECURITY, ui_control="number", unit="rows",
        ui_help="Hard cap on the number of rows a single JDBC Bind/Describe/Execute portal will hold in gateway memory. PortalSuspended only bounds how many rows are sent per Execute call, not how much is buffered before that. A result set larger than this cap is rejected fail-closed with SQLSTATE 54000. 0 disables the cap.",
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
        key="llm.anthropic_thinking_budget",
        level="system", type="int", default=4000,
        section="LLM (legacy)",
        description=(
            "Default Anthropic extended-thinking budget in tokens (R6/F15). "
            "Independent of max_tokens so lowering the output cap never "
            "degrades planner reasoning. Per-row LLMProviderConfig.config "
            "may override via 'thinking_budget'. Clamped below max_tokens only "
            "to satisfy the API's budget_tokens < max_tokens constraint."
        ),
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="llm.prompt_cache_enabled",
        level="system", type="bool", default=True,
        section="LLM", restart_required=True,
        description=(
            "R1 (F1) — master switch for Anthropic prompt-cache breakpoints on "
            "the stable planner/judge system prefix. The cache marker never "
            "changes the rendered prompt text (byte-identical with or without "
            "it), so this is a cost/latency lever, not a correctness one. "
            "Default ON. Turn OFF as an operator safety valve if a misplaced "
            "breakpoint is billing at full price; queries behave identically "
            "either way. Surfaced so the valve is flippable through the system "
            "settings API without a deploy (the write API rejects non-surfaced "
            "keys); consuming services read it from their startup snapshot, so "
            "a flip takes effect on the next agent-service restart "
            "(restart_required). No System Configuration UI tab renders the "
            "'LLM' group yet — flip via the settings API (see the "
            "configuration reference)."
        ),
        label="LLM prompt caching",
        ui_group="LLM", ui_control="switch",
        ui_help=(
            "When on, the stable part of the AI agent's prompt is served from "
            "the provider's cache on repeat calls, cutting cost and latency. "
            "Answers are identical either way. Turn off only as a safety "
            "valve if provider billing looks wrong. Takes effect after the "
            "agent service restarts."
        ),
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
        key="pocket.serve_overdue_grace_hours",
        level="system", type="int", default=6,
        section="Pocket tables",
        description=(
            "Bug-5148/Bug-8338: serve-time staleness safety for pocket tables. "
            "A pocket is labelled fresh only by the refresh sweep; if the "
            "scheduler is scaled-to-zero or behind, a fresh-labelled pocket can "
            "serve outdated numbers. At serve time the router refuses a "
            "schedule-policy pocket that has gone more than this many hours past "
            "its first missed scheduled refresh, falling back to the source "
            "(correct numbers). Sized above the hourly sweep cadence so a "
            "just-fired-but-not-yet-swept pocket still serves. Set 0 to refuse "
            "the instant a scheduled refresh is missed. Manual and event pockets "
            "have no cron cadence and are unaffected."
        ),
        # 0 is a valid strictest setting (refuse the instant a scheduled fire is
        # missed); the serve-time gate honors it, so allow it here.
        validator=_validate_non_negative_int,
        surfaced=True,
    ),
    SettingDef(
        key="aggregate.serve_overdue_grace_hours",
        level="system", type="int", default=6,
        section="Aggregates",
        description=(
            "Bug-5148/Bug-8338: serve-time staleness safety for aggregates. An "
            "aggregate is labelled active/fresh only by the refresh sweep; if the "
            "scheduler is scaled-to-zero or behind, a fresh-labelled aggregate can "
            "serve outdated numbers. At serve time the router refuses a schedule-"
            "policy aggregate that has gone more than this many hours past its "
            "first missed scheduled refresh, falling back to the source (correct "
            "numbers). Sized above the hourly sweep cadence so a just-fired-but-"
            "not-yet-swept aggregate still serves; lower it (0 = refuse the "
            "instant a scheduled refresh is missed) to fail over to source sooner "
            "when refreshes stall. Manual-policy aggregates have no cadence and "
            "are unaffected."
        ),
        # 0 is a valid strictest setting (refuse the instant a scheduled fire is
        # missed); the serve-time gate honors it, so allow it here.
        validator=_validate_non_negative_int,
        surfaced=True,
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
        key="named_query.analytics_sustained_fallback_count",
        level="system", type="int", default=3,
        section="Named queries",
        description=(
            "Minimum number of attributed live-fallback observations in the "
            "analytics window before a costly fallback pattern is considered "
            "sustained. The recommendation remains to repair, refresh, or "
            "adjust Named Query materialisation; it never builds an aggregate "
            "copy."
        ),
        validator=_validate_positive_int,
        label="Named Query sustained fallback count",
        ui_group=_GRP_LIMITS, ui_control="number", unit="fallbacks",
        ui_help=(
            "How many attributed live fallbacks are required before health "
            "analytics recommends repairing or refreshing the Named Query."
        ),
        surfaced=True,
    ),
    SettingDef(
        key="named_query.max_rows",
        level="system", type="int", default=100_000,
        section="Limits",
        description=(
            "Maximum rows a materialised Named Query result table may hold. "
            "Enforced at refresh against the ACTUAL materialised cardinality: "
            "an oversized result is dropped and the artifact marked failed "
            "(ROW_CAP_EXCEEDED) — reject, never truncate. A per-Named-Query "
            "``row_cap`` override applies when present."
        ),
        validator=_validate_positive_int,
        label="Named Query max rows",
        ui_group=_GRP_LIMITS, ui_control="number", unit="rows",
        ui_help="A materialised Named Query that exceeds this row count is dropped and marked failed instead of being served.",
        surfaced=True,
    ),
    SettingDef(
        key="named_query.max_columns",
        level="system", type="int", default=200,
        section="Limits",
        description=(
            "Maximum output columns a Named Query definition may project. "
            "Enforced at create/validate time against the bound select list — "
            "reject, never truncate. A per-Named-Query ``column_cap`` override "
            "applies when present."
        ),
        validator=_validate_positive_int,
        label="Named Query max columns",
        ui_group=_GRP_LIMITS, ui_control="number", unit="columns",
        ui_help="A Named Query definition whose projection exceeds this width is rejected at authoring time.",
        surfaced=True,
    ),
    SettingDef(
        key="named_query.serve_overdue_grace_hours",
        level="system", type="int", default=6,
        section="Named queries",
        description=(
            "Serve-time staleness safety for materialised Named Queries, same "
            "contract as 'pocket.serve_overdue_grace_hours': the router "
            "refuses a schedule-policy Named Query that has gone more than "
            "this many hours past its first missed scheduled refresh, falling "
            "back to live source execution (correct numbers). Set 0 to refuse "
            "the instant a scheduled refresh is missed. Manual-policy Named "
            "Queries have no cadence and are unaffected."
        ),
        validator=_validate_non_negative_int,
        label="Named Query overdue grace",
        ui_group=_GRP_LIMITS, ui_control="number", unit="hours",
        ui_help="How long a materialised Named Query keeps serving after a missed scheduled refresh before the router falls back to live source execution.",
        surfaced=True,
    ),
    SettingDef(
        key="named_query.invalidating_recovery_hours",
        level="system", type="int", default=6,
        section="Named queries",
        description=(
            "Stuck-refresh recovery: a crash/timeout mid-materialisation "
            "strands a Named Query artifact in 'invalidating' (never served, "
            "and skipped by refreshes). A manual refresh re-picks an "
            "'invalidating' artifact once it has been stuck longer than this "
            "many hours, so it recovers without operator intervention."
        ),
        validator=_validate_positive_int,
        label="Named Query invalidating recovery",
        ui_group=_GRP_LIMITS, ui_control="number", unit="hours",
        ui_help="How long an 'invalidating' Named Query is left alone before a new manual refresh is allowed to re-pick it.",
        surfaced=True,
    ),
    SettingDef(
        key="quantile_routing.proof_mode",
        level="system", type="str", default="off",
        section="Quantile routing",
        description="Bug-6969/5891, spec §15: master mode for proof-carrying pNN "
                    "(percentile) aggregate serving. 'off' (default) = quantile "
                    "queries route exactly as before the feature (MEDIAN via the "
                    "existing path, non-median percentiles to source); 'shadow' is "
                    "RESERVED and currently behaves as 'off' (the shadow evaluator "
                    "is not yet implemented — do not treat a quiet shadow run as "
                    "enforce-readiness); 'enforce' = a proven pNN aggregate column "
                    "is served. Model rows override via "
                    "'quantile_routing.model_proof_mode'.",
        validator=_validate_quantile_proof_mode,
        surfaced=False,
    ),


    SettingDef(
        key="predictive.evaluation_window_days",
        level="system", type="int", default=7,
        section="Predictive (legacy)", restart_required=True,
        description="System fallback for the predictive feedback evaluation window. Per-model row overrides.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="predictive.validation_min_hits",
        level="system", type="int", default=3,
        section="Predictive (legacy)", restart_required=True,
        description="System fallback for the predictive validation hit threshold. Per-model row overrides.",
        validator=_validate_positive_int,
        surfaced=False,
    ),
    SettingDef(
        key="predictive.unused_retire_days",
        level="system", type="int", default=14,
        section="Predictive (legacy)", restart_required=True,
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
# TENANT — tenant-wide policies and presentation defaults.
# Connections and LLM configurations live at PROJECT level. Conversation
# retention is project-only (no tenant fallback); the policies below apply to
# all projects/models in a tenant and therefore have no narrower home.
# ---------------------------------------------------------------------------

_GRP_AUDIT = "Audit logging"
_GRP_BRANDING = "Branding"
_GRP_CALENDAR = "Calendars"


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
        key="calendar.fiscal_year_label_format",
        level="tenant", type="dict", default={"format": DEFAULT_FISCAL_YEAR_LABEL_FORMAT},
        section="Calendars",
        description=(
            "Caption format for fiscal and NRF retail year members. The numeric "
            "year key remains unchanged; January-start and ISO calendars always "
            "use the plain integer caption."
        ),
        validator=validate_fiscal_year_label_format,
        label="Fiscal year label format",
        ui_group=_GRP_CALENDAR, ui_control="dict",
        ui_choices=[{"format": token} for token in FISCAL_YEAR_LABEL_FORMATS],
        ui_help=(
            "Choose start_year (default), span_short (2025-26), span_long "
            "(2025-2026), span_fy (FY25-26), or end_year (FY2026). Rebuild "
            "existing calendars before the caption column is available."
        ),
    ),
    # Bug-8789: controls the miss-reason VOCABULARY the aggregate row-population
    # proof reports. "legacy" (default) emits the single opaque
    # join_population_mismatch, exactly as every existing deployment, log
    # pipeline and dashboard expects. "split" emits the two sub-codes:
    # population_plan_mismatch (BUILD — a narrower aggregate could serve) and
    # population_unprovable_model (INELIGIBLE — a model-level fault no
    # aggregate can fix).
    #
    # A STRING TOKEN, not a bool, and tenant-level, not system-level. Both are
    # deliberate and both were defects in the original implementation:
    #   * system-level could not be resolved at all from the query-router,
    #     which holds only a tenant session (``get_setting`` skips the system
    #     branch unless a system session is supplied, so the flag was
    #     permanently stuck at its default in production);
    #   * a bool goes through ``coerce`` -> ``bool(value)``, so ANY non-null
    #     stored value flips it on. A token compared against an exact literal
    #     fails safe on anything it does not recognise — the same pattern
    #     ``quantile_routing.proof_mode`` uses for the same reason.
    SettingDef(
        key="query.population_mismatch_reason_mode",
        level="tenant", type="str", default="legacy",
        section="Aggregates",
        description=(
            "Miss-reason vocabulary for aggregate row-population refusals: "
            "'legacy' (one join_population_mismatch code) or 'split' "
            "(buildable vs terminal sub-codes)."
        ),
        label="Population mismatch reason mode",
        ui_group=_GRP_AGG, ui_control="select",
        ui_choices=["legacy", "split"],
        validator=_validate_population_reason_mode,
        ui_help=(
            "When 'legacy' (default) the router logs one join_population_mismatch for "
            "every population refusal. When 'split', the router emits "
            "population_plan_mismatch (a narrower aggregate could fix it) or "
            "population_unprovable_model (no aggregate can serve — a cyclic join "
            "component or unresolvable anchor). Any unrecognised value is treated "
            "as 'legacy'."
        ),
        surfaced=False,
    ),
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
# PROJECT — mostly empty. Per-project agent fields live on
# ProjectAgentConfig (edited via the drawer's bespoke agent tabs); per-project
# connections and LLM bundles live on ProjectConnection and LLMProviderConfig
# (edited via the Connections and LLM Configurations tabs). Only project-level
# *primitive* settings without a dedicated table behind them live here.
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
    SettingDef(
        key="agent.judge_context_mode",
        level="project", type="str", default="distilled",
        section="Agent judge",
        description=(
            "R2 (F2) — PER-PROJECT escape hatch for the judge evidence pack "
            "(spec section 7). 'distilled' (default) sends the judge a "
            "distilled evidence pack that keeps ALL evidential context (model "
            "layer, glossary/alias, retrieved grounding cards, plan, rows, "
            "rubric, history) but strips non-evidential planner boilerplate "
            "(TASK examples, OUTPUT FORMAT schemas, expression rules). 'full' "
            "reverts THIS project's judge to receiving the entire planner "
            "system prompt verbatim — a deploy-free revert if a judge-quality "
            "regression is observed in the project's judge-block-rate KPI. "
            "Distillation never judges on LESS: on any assembly failure it "
            "falls back to full."
        ),
        validator=_validate_judge_context_mode,
        surfaced=False,
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
    SettingDef(
        key="ai_scheduler.daily_token_budget",
        level="model", type="int", default=0,
        section="AI optimizer",
        description=(
            "Ceiling on LLM tokens the AI optimiser may spend on this model per "
            "UTC day. Admission reserves the prompt plus the provider "
            "configuration's maximum output; completed runs record actual "
            "input+output tokens for audit. A run whose reservation would "
            "exceed it stops BEFORE the provider call. 0 means no limit. "
            "Separate from the conversational agent's budget: an advisor loop "
            "must not be able to consume the chat allowance, or vice versa."
        ),
        validator=_validate_non_negative_int,
        label="AI optimiser daily token budget",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="tokens",
        ui_help=(
            "Stops the AI optimiser before a call whose prompt plus configured "
            "maximum output would exceed this model's daily reservation. "
            "Actual provider usage is recorded on completed runs. Leave at 0 "
            "for no limit."
        ),
    ),

    # ROI cost model (Bug-9076). These are COST ASSUMPTIONS about the customer's
    # own infrastructure, not code constants, and since Bug-8123 they ORDER the
    # candidates rather than merely gating them — so the rate decides which
    # single aggregate gets built. The shipped defaults are the generic figures
    # the estimator used to hard-code. Per-CONNECTOR economics (BigQuery bills
    # per byte scanned, Snowflake per warehouse-second) is a separate governed
    # cost-catalogue decision and is NOT expressible with these three knobs.
    SettingDef(
        key="optimizer.roi_storage_rate_gb_month",
        level="model", type="float", default=0.023,
        section="AI optimizer",
        description=(
            "Storage price in $ per GB per month used by the ROI cost model "
            "when it decides whether an aggregate is worth its disk. Default "
            "0.023 (generic object-storage list price)."
        ),
        validator=_validate_positive_float,
        label="ROI storage rate",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="$/GB/month",
        ui_help=(
            "What a gigabyte of aggregate storage costs you per month. Used to "
            "work out whether an aggregate pays for itself."
        ),
    ),
    SettingDef(
        key="optimizer.roi_compute_rate_per_million_rows",
        level="model", type="float", default=0.005,
        section="AI optimizer",
        description=(
            "Compute price in $ per million source rows scanned, used to value "
            "the scans an aggregate avoids and to price its refresh. Default "
            "0.005."
        ),
        validator=_validate_positive_float,
        label="ROI compute rate",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="$/M rows",
        ui_help=(
            "What scanning a million source rows costs you. Used to value the "
            "scans an aggregate saves and the cost of refreshing it."
        ),
    ),
    SettingDef(
        key="optimizer.roi_daily_refresh_rate",
        level="model", type="float", default=1.0,
        section="AI optimizer",
        description=(
            "Refreshes per day assumed by the ROI cost model for an aggregate "
            "with no explicit schedule. Default 1.0."
        ),
        validator=_validate_positive_float,
        label="ROI assumed refresh frequency",
        ui_group=_GRP_AI_SCHED, ui_control="number", unit="refreshes/day",
        ui_help=(
            "How often the cost model assumes an aggregate is rebuilt when no "
            "schedule says otherwise."
        ),
    ),

    # Derived-grain aggregate routing (spec §15.1) — per-model gates.
    SettingDef(
        key="optimizer.derived_expression_auto_build",
        level="model", type="str", default="off",
        section="AI optimizer",
        description=(
            "Whether the optimizer may create artifact-owned derived-expression "
            "keys for hot unmodelled expressions on this model. 'off' = never; "
            "'approval' = suggest, require modeller/operator approval before "
            "build; 'automatic' = build unattended (later opt-in). Default off."
        ),
        validator=_validate_derived_auto_build_mode,
        label="Derived expression auto-build",
        ui_group=_GRP_AI_SCHED, ui_control="select",
        ui_choices=["off", "approval", "automatic"],
        ui_help=(
            "Controls whether the optimizer can build accelerator aggregates for "
            "repeated inline expressions (e.g. DATE_TRUNC) it observes on this "
            "model. Approval mode requires you to confirm each build."
        ),
        surfaced=False,
    ),
    SettingDef(
        key="model.attribute_relationship_verification",
        level="model", type="str", default="on",
        section="Aggregates",
        description=(
            "Whether declared dimension key-to-detail relationships are "
            "data-verified at deploy/build/refresh for this model. 'on' = verify "
            "and gate serving on verified evidence (default); 'off' = do not "
            "verify, which makes ALL data-verified attribute edges unavailable "
            "(source route) — it never preserves prior trust (spec §15.1)."
        ),
        validator=_validate_attribute_verification_mode,
        label="Attribute relationship verification",
        ui_group=_GRP_AGG, ui_control="select",
        ui_choices=["on", "off"],
        ui_help=(
            "Verifies that a declared 1:1 or many-to-1 relationship between a "
            "dimension key and a detail column actually holds in the data before "
            "any aggregate is allowed to substitute the detail. Turning this off "
            "disables that acceleration; it does not weaken results."
        ),
        surfaced=False,
    ),

    # Join population governance (Bug-8615, phase G1) — deploy-time only.
    SettingDef(
        key="model.join_population_validation",
        level="model", type="str", default="on",
        section="Aggregates",
        description=(
            "Whether each of this model's joins is classified at DEPLOY time as "
            "neutral / filtering / multiplying against the real source data. "
            "'on' = measure and record the per-join verdict and the model's "
            "OK/WARNING/BLOCKED rollup (default); 'off' = do not touch the "
            "source, which clears the previous verdicts and leaves the model's "
            "join population status UNEVALUATED rather than stale. Unmeasured "
            "rows never block; measured G5 policy blockers do."
        ),
        validator=_validate_join_population_mode,
        label="Join population validation",
        ui_group=_GRP_AGG, ui_control="select",
        ui_choices=["on", "off"],
        ui_help=(
            "Checks, when you deploy, whether any join in this model drops or "
            "duplicates rows. It reports what it finds; it never changes results. "
            "Measured policy blockers stop a deployment before publish state "
            "changes."
        ),
        surfaced=False,
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
        key="quantile_routing.model_proof_mode",
        level="model", type="str", default="off",
        section="Quantile routing",
        description="Per-model override for proof-carrying pNN aggregate serving "
                    "(off | shadow | enforce). Defaults off so the feature is inert "
                    "until this model's quantile coverage is backfilled and gated. "
                    "See the system 'quantile_routing.proof_mode' key.",
        validator=_validate_quantile_proof_mode,
        surfaced=False,
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

    # Derived-grain relationship health sweep cadence (spec: architecture_derived-
    # grain-operational-serving.md §C). How often the scheduler re-proves each
    # declared attribute relationship's 1:1 mapping over the served (aggregate)
    # data for this model. A lower value re-checks health more often (fresher
    # serving decisions, more query load); a higher value re-checks less often.
    # Falls back to this registry default when unset for the model.
    SettingDef(
        key="optimizer.derived_relationship_sweep_interval_hours",
        level="model", type="int", default=24,
        section="Derived-grain routing",
        description=(
            "How often (in hours) the relationship health sweep re-proves each "
            "declared attribute relationship's 1:1 mapping over the served data "
            "for this model. Default 24."
        ),
        validator=_validate_positive_int,
        label="Relationship health sweep interval",
        ui_group=_GRP_DERIVED, ui_control="number", unit="hours",
        ui_help=(
            "The periodic sweep re-checks whether each key-to-detail relationship "
            "still maps 1:1 on the built aggregate. Set how often that runs for "
            "this model. Lower is fresher but costs more query load."
        ),
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
