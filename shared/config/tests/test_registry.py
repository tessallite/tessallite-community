"""Tests for the setting registry — coverage, validation, coercion."""
from __future__ import annotations

import pytest

from shared.config.registry import (
    REGISTRY,
    SettingDef,
    all_for_level,
    coerce,
    get_def,
    has_key,
    validate,
)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def test_registry_levels_match_2026_04_restructure():
    """Pin the sanctioned scope of every setting level.

    The tenant scope is deliberately small but is not limited to branding:
    calendar caption vocabulary, population-reason vocabulary, and
    observability/version-retention policies are tenant-wide contracts too.
    The generated configuration reference is produced from the registry and
    documents these same homes. A new tenant setting must be justified by its
    producer/consumer and added here in the same change; otherwise this guard
    must fail.
    """
    assert all_for_level("system"), "no settings registered at level 'system'"
    assert all_for_level("project"), "no settings registered at level 'project'"
    assert all_for_level("model"), "no settings registered at level 'model'"
    tenant_keys = sorted(d.key for d in all_for_level("tenant"))
    assert tenant_keys == [
        "audit.log_level",
        "audit.retention_days",
        "branding.app_title",
        "branding.font_family",
        "branding.logo_url",
        "branding.primary_color",
        "branding.secondary_color",
        "calendar.fiscal_year_label_format",
        "query.population_mismatch_reason_mode",
        "query_log.retention_days",
        "versions.retention_count",
    ], (
        "tenant level must contain only the sanctioned tenant-wide policy, "
        "branding keys — justify any new tenant-only setting and update this "
        "test plus the generated configuration reference"
    )


def test_every_definition_has_a_description():
    """Documentation discipline — empty descriptions become poor UI tooltips."""
    for (level, key), definition in REGISTRY.items():
        assert definition.description.strip(), (
            f"{level}/{key} has empty description"
        )


def test_every_non_override_default_is_set():
    """System settings must seed something concrete; project/model keys
    that act as overrides may default to None. After the restructure,
    tenant is empty so this loop only exercises system."""
    for definition in all_for_level("system"):
        if definition.key == "meta.bootstrap_env_view":
            continue
        assert definition.default is not None, (
            f"system/{definition.key} has no default"
        )


#: Exact keys whose NAME trips the credential heuristic below but which are not
#: credentials. Exact keys, never patterns, so the list cannot silently absorb a
#: real secret; each entry carries its reason.
#:
#: ``ai_scheduler.daily_token_budget`` (F-011-05 / Bug-9407) — an integer ceiling
#: on LLM tokens the AI optimiser may spend per UTC day. "Token" here is the
#: billing unit of a language model, not an authentication credential. Renaming
#: it to dodge the substring would hide it from the guard rather than answer the
#: guard, and would cost the operator the unit in the setting's own name.
_NON_CREDENTIAL_KEYS_MATCHING_THE_HEURISTIC = frozenset({
    "ai_scheduler.daily_token_budget",
})


def test_no_secret_keys_are_in_the_registry():
    """C-4 decision: credentials live in .env, never in DB-backed settings.
    Three exemptions:
      * env_var=True — env-only display rows.
      * sensitive_display=True — per-project secrets stored encrypted at
        rest in ProjectSetting.value_json (e.g. agent.webhook_secret).
        This is an explicit design choice for project-scoped agent
        credentials that cannot live in .env (per-tenant/per-project).
      * an exact key in ``_NON_CREDENTIAL_KEYS_MATCHING_THE_HEURISTIC`` — a
        reviewed false positive of the substring match."""
    forbidden_substrings = (
        "password", "passwd", "secret", "api_key", "private_key", "token",
    )
    for (_lvl, key), definition in REGISTRY.items():
        if definition.env_var:  # env-only display rows are exempt
            continue
        if definition.sensitive_display:  # encrypted per-project values
            continue
        if key in _NON_CREDENTIAL_KEYS_MATCHING_THE_HEURISTIC:
            continue
        for needle in forbidden_substrings:
            assert needle not in key.lower(), (
                f"setting {key!r} looks like a credential — "
                "credentials must stay in .env per C-4"
            )


def test_credential_heuristic_exemptions_are_live_and_still_needed():
    """A stale exemption is a hole. Every exempted key must still exist AND
    still trip the heuristic — otherwise it is silently covering nothing (or,
    worse, a key that was renamed into something the guard would now catch)."""
    forbidden_substrings = (
        "password", "passwd", "secret", "api_key", "private_key", "token",
    )
    registry_keys = {key for (_lvl, key) in REGISTRY}
    for key in sorted(_NON_CREDENTIAL_KEYS_MATCHING_THE_HEURISTIC):
        assert key in registry_keys, (
            f"exempted key {key!r} is no longer in the registry — remove the "
            "exemption rather than leaving a hole"
        )
        assert any(n in key.lower() for n in forbidden_substrings), (
            f"exempted key {key!r} no longer trips the heuristic — the "
            "exemption is dead and should be removed"
        )


# ---------------------------------------------------------------------------
# get_def / has_key
# ---------------------------------------------------------------------------

def test_get_def_returns_definition_for_known_key():
    d = get_def("auth.jwt_expire_minutes", "system")
    assert isinstance(d, SettingDef)
    assert d.type == "int"
    assert d.default == 60


def test_get_def_raises_for_unknown_key():
    with pytest.raises(KeyError):
        get_def("does.not.exist", "system")


def test_get_def_raises_for_wrong_level():
    # auth.jwt_expire_minutes is system-level, not tenant
    with pytest.raises(KeyError):
        get_def("auth.jwt_expire_minutes", "tenant")


def test_bug9172_sol_r1_f4_named_query_fallback_threshold_is_governed():
    """B9172-SOL-R1-F4: health recommendations use a registered setting."""
    setting = get_def(
        "named_query.analytics_sustained_fallback_count", "system"
    )
    assert setting.default == 3
    assert setting.type == "int"
    validate(1, setting)
    with pytest.raises(ValueError):
        validate(0, setting)


def test_has_key_distinguishes_levels():
    assert has_key("auth.jwt_expire_minutes", "system")
    assert not has_key("auth.jwt_expire_minutes", "model")


# ---------------------------------------------------------------------------
# coerce
# ---------------------------------------------------------------------------

def test_coerce_int_accepts_string_form():
    d = get_def("auth.jwt_expire_minutes", "system")
    assert coerce("90", d) == 90


def test_coerce_bool_accepts_yes_no_strings():
    d = get_def("rate_limit.enabled", "system")
    assert coerce("true", d) is True
    assert coerce("no", d) is False
    assert coerce(1, d) is True


def test_coerce_int_rejects_bool():
    """A bool would silently pass int(True) == 1 — guard against it."""
    d = get_def("auth.jwt_expire_minutes", "system")
    with pytest.raises(ValueError):
        coerce(True, d)


def test_coerce_list_str_splits_csv():
    d = SettingDef(
        key="x", level="system", type="list[str]", default=[],
        description="test",
    )
    assert coerce("a, b, c", d) == ["a", "b", "c"]


def test_coerce_passes_through_none():
    d = get_def("auth.jwt_expire_minutes", "system")
    assert coerce(None, d) is None


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def test_validate_cron_accepts_valid_expression():
    d = get_def("aggregate.default_cron", "model")
    validate("0 5 * * *", d)  # no raise


def test_validate_cron_rejects_wrong_field_count():
    d = get_def("aggregate.default_cron", "model")
    with pytest.raises(ValueError):
        validate("0 5 * *", d)


def test_validate_positive_int_rejects_zero():
    d = get_def("auth.jwt_expire_minutes", "system")
    with pytest.raises(ValueError):
        validate(0, d)


def test_validate_ratio_rejects_above_one():
    d = get_def("pocket.max_ndv_ratio", "system")
    with pytest.raises(ValueError):
        validate(1.5, d)


def test_validate_url_requires_scheme():
    d = get_def("xmla.datasource_url_fallback", "system")
    with pytest.raises(ValueError):
        validate("localhost:8080", d)


def test_validate_iso_datetime_rejects_garbage():
    d = get_def("xmla.metadata_created_at", "system")
    with pytest.raises(ValueError):
        validate("not a date", d)


def test_validate_skips_when_value_is_none():
    """None means "unset, fall through"; validators must not fire."""
    d = get_def("aggregate.default_cron", "model")
    validate(None, d)  # no raise even though "" would fail
