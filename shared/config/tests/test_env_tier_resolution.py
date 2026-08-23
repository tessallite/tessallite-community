"""Bug-8108 F3 / R2 — the explicit-system -> env -> default resolution tier.

The JDBC portal row-buffer cap (and its siblings ``gateway.query_byte_ceiling``
/ ``gateway.query_rate_limit_per_minute``) advertise a three-tier resolution:
a stored, hot-reloadable ``system_settings`` value first, then an environment
variable, then a hardcoded default.

Before this fix the callers read ``system_snapshot_get``, which substitutes the
REGISTRY DEFAULT for an absent stored value — so the function always returned a
non-None value at the first tier and the advertised environment variable was
unreachable whenever no system row was stored (the common case). These tests
exercise the REAL resolver (``resolve_system_env_default_int`` /
``system_snapshot_get_explicit``) rather than monkeypatching the caller's cap
function, which is exactly what concealed the defect.
"""
from __future__ import annotations

import pytest

from shared.config import bootstrap
from shared.config.bootstrap import (
    resolve_system_env_default_int,
    system_snapshot_get,
    system_snapshot_get_explicit,
)

# A real, registered system-level key whose registry default is 50_000.
_KEY = "gateway.jdbc_portal_row_buffer_cap"
_REG_DEFAULT = 50_000


@pytest.fixture(autouse=True)
def _isolate_snapshot():
    """Each test starts with a clean, empty snapshot (no stored/explicit
    values) and restores it afterward."""
    bootstrap.clear_snapshot()
    yield
    bootstrap.clear_snapshot()


# ---------------------------------------------------------------------------
# system_snapshot_get_explicit — preserves "no stored value"
# ---------------------------------------------------------------------------


def test_explicit_absent_returns_none_not_default():
    # The whole bug: the OLD accessor folds the registry default in...
    assert system_snapshot_get(_KEY) == _REG_DEFAULT
    # ...while the explicit accessor preserves "operator stored nothing".
    assert system_snapshot_get_explicit(_KEY) is None


def test_explicit_present_after_write():
    bootstrap.update_snapshot(_KEY, 7)
    assert system_snapshot_get_explicit(_KEY) == 7


def test_explicit_cleared_on_delete_to_none():
    bootstrap.update_snapshot(_KEY, 7)
    assert system_snapshot_get_explicit(_KEY) == 7
    bootstrap.update_snapshot(_KEY, None)  # delete / fall-through
    assert system_snapshot_get_explicit(_KEY) is None


# ---------------------------------------------------------------------------
# resolve_system_env_default_int — the three-tier resolver
# ---------------------------------------------------------------------------


def test_stored_explicit_value_wins_over_env():
    bootstrap.update_snapshot(_KEY, 7)
    # Even with an env override present, the explicitly-stored system value
    # (hot-reloadable) takes precedence.
    assert resolve_system_env_default_int(_KEY, 3, _REG_DEFAULT) == 7


def test_env_override_reachable_when_nothing_stored():
    # THE F3 fix: with no stored system row, the env value must be honored
    # rather than masked by the registry default.
    assert system_snapshot_get_explicit(_KEY) is None
    assert resolve_system_env_default_int(_KEY, 3, _REG_DEFAULT) == 3


def test_default_fallback_when_env_none():
    # No stored value and no env value -> registry default.
    assert resolve_system_env_default_int(_KEY, None, _REG_DEFAULT) == _REG_DEFAULT


def test_zero_env_disables_not_rejected():
    # 0 is the documented "disabled" sentinel and must pass through intact.
    assert resolve_system_env_default_int(_KEY, 0, _REG_DEFAULT) == 0


def test_negative_env_rejected_falls_to_default_not_disabled():
    # A negative env value is a typo, NOT a request to disable the control:
    # it must fall through to the default, never silently switch the cap off
    # (which -1 would do at the cap<=0 check).
    assert resolve_system_env_default_int(_KEY, -5, _REG_DEFAULT) == _REG_DEFAULT


def test_stored_zero_disables():
    bootstrap.update_snapshot(_KEY, 0)
    assert resolve_system_env_default_int(_KEY, 3, _REG_DEFAULT) == 0


def test_unknown_key_falls_to_passed_default():
    # No registry default for a made-up key: the ultimate net is the passed
    # constant (both explicit and env absent).
    assert resolve_system_env_default_int("nonexistent.key.xyz", None, 999) == 999


def test_non_numeric_env_ignored():
    # A malformed env value must not crash; it falls through to the default.
    assert resolve_system_env_default_int(_KEY, "not-a-number", _REG_DEFAULT) == _REG_DEFAULT
