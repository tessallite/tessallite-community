"""Bug-5920: optional-dependency calendar types must be advertised as
available only when their runtime dependency is actually installed.

`available_calendar_types()` is the single source of truth both the
model-service validator and the frontend picker consult, replacing the old
pattern where the API accepted 'hijri' unconditionally and the frontend
compensated with a separate hardcoded disable flag.

Hijri update (2026-07): ``hijri-converter==2.3.1`` is now a shipped dependency
of every service (declared in ``shared/pyproject.toml`` and installed by each
service Dockerfile), so ``hijri`` IS available in the deployed image/venv.
These tests moved forward with the codebase (triage policy case 2): they now
assert hijri is available AND guard that the dependency stays shipped — if the
package is ever dropped, ``available_calendar_types()`` would silently exclude
hijri again and these tests would fail.
"""
from __future__ import annotations

from fastapi import HTTPException
import pytest

from shared.semantic.calendar_types import (
    CALENDAR_TYPES,
    available_calendar_types,
    is_available_calendar_type,
)
from src.api.calendar import _validate_calendar_type


def test_hijri_converter_installed_as_shipped_dependency():
    # hijri-converter is now a declared dependency (shared/pyproject.toml) and
    # installed in each service image/venv, so its import must resolve here.
    import importlib.util
    assert importlib.util.find_spec("hijri_converter") is not None


def test_hijri_included_in_available_types_when_dependency_installed():
    available = available_calendar_types()
    assert "hijri" in available
    # hijri is the only type with an optional dependency, and it is now
    # installed, so every canonical type is available.
    assert available == CALENDAR_TYPES


def test_is_available_calendar_type_accepts_hijri():
    assert is_available_calendar_type("hijri") is True
    assert is_available_calendar_type("standard") is True


def test_validate_calendar_type_accepts_hijri():
    # With the dependency shipped, hijri validates and normalises like any
    # other canonical type instead of raising the "not available" 400.
    assert _validate_calendar_type("hijri") == "hijri"


def test_validate_calendar_type_still_accepts_standard():
    assert _validate_calendar_type("standard") == "standard"


def test_validate_calendar_type_still_rejects_unknown_token():
    with pytest.raises(HTTPException) as exc_info:
        _validate_calendar_type("banana")
    assert exc_info.value.status_code == 400
    assert "is invalid" in exc_info.value.detail


def test_validate_calendar_type_passes_through_none():
    assert _validate_calendar_type(None) is None
