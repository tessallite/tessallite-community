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
    """After the 2026-04 restructure: System/Project/Model are non-empty.
    Tenant carries ONLY the deliberate post-restructure additions: audit
    logging (level + retention) and tenant branding. If you add a
    tenant-level key, justify it, update this list AND the architecture
    docs."""
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
    ], (
        "tenant level must contain only the sanctioned audit + branding keys — "
        "if you add a tenant-only setting, justify and update this test"
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


def test_no_secret_keys_are_in_the_registry():
    """C-4 decision: credentials live in .env, never in DB-backed settings.
    Two exemptions:
      * env_var=True — env-only display rows.
      * sensitive_display=True — per-project secrets stored encrypted at
        rest in ProjectSetting.value_json (e.g. agent.webhook_secret).
        This is an explicit design choice for project-scoped agent
        credentials that cannot live in .env (per-tenant/per-project)."""
    forbidden_substrings = (
        "password", "passwd", "secret", "api_key", "private_key", "token",
    )
    for (_lvl, key), definition in REGISTRY.items():
        if definition.env_var:  # env-only display rows are exempt
            continue
        if definition.sensitive_display:  # encrypted per-project values
            continue
        for needle in forbidden_substrings:
            assert needle not in key.lower(), (
                f"setting {key!r} looks like a credential — "
                "credentials must stay in .env per C-4"
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
