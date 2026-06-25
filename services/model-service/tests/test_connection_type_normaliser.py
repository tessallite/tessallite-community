"""Phase 2 (CR-002 Finding 4) — shared connection_type normaliser.

Verifies the single source of truth at ``shared/schemas/connection_type.py``
collapses the legacy ``jdbc`` alias to the canonical ``hadoop_spark`` and
passes everything else through unchanged.

These tests intentionally stay at the pure-Python level (no FastAPI, no
DB) so they run fast in every service test suite.
"""
from __future__ import annotations

import pytest

from shared.schemas.connection_type import (
    ALLOWED_CONNECTION_TYPES,
    is_allowed_canonical,
    normalize_connection_type,
)


def test_canonical_values_pass_through_unchanged():
    for v in ("bigquery", "postgresql", "hadoop_spark"):
        assert normalize_connection_type(v) == v


def test_legacy_jdbc_collapses_to_hadoop_spark():
    assert normalize_connection_type("jdbc") == "hadoop_spark"


def test_none_returns_none():
    assert normalize_connection_type(None) is None


def test_canonical_redshift_passes_through():
    assert normalize_connection_type("redshift") == "redshift"


def test_unknown_value_passes_through_for_caller_error_handling():
    assert normalize_connection_type("mysql") == "mysql"


def test_is_allowed_canonical_recognises_all_three_canonical():
    for v in ALLOWED_CONNECTION_TYPES:
        assert is_allowed_canonical(v) is True


def test_is_allowed_canonical_recognises_legacy_jdbc_as_allowed():
    # Because the normaliser collapses jdbc → hadoop_spark, the helper
    # treats jdbc as allowed at the read path. Write path still rejects
    # it via ConnectionCreate's Pydantic field validator.
    assert is_allowed_canonical("jdbc") is True


def test_is_allowed_canonical_recognises_redshift():
    assert is_allowed_canonical("redshift") is True


def test_is_allowed_canonical_rejects_unknown():
    assert is_allowed_canonical("mysql") is False
    assert is_allowed_canonical("") is False
    assert is_allowed_canonical(None) is False
