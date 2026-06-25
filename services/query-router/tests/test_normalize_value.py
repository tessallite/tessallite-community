"""Bug-5383: _normalize_value must handle scientific-notation integers.

The test helper ``_normalize_value`` in ``validate_rewrite_semantic.py``
had a guard (``"e" not in s.lower()``) that blocked normalisation of
scientific-notation integer strings like ``"1.0E+5"`` to ``"100000"``.
This caused direct-vs-gateway mismatches for COUNT(*) results where
asyncpg returns ``Decimal('1E+5')`` and pydantic serialises it as
``"1.0E+5"``.

These tests exercise the FIXED version of the normaliser logic.  The
function body is copied here to avoid importing the validation script
(which has heavy side-effects and optional dependencies).
"""
from __future__ import annotations

import re


def _normalize_value(v):
    """Mirror of the fixed _normalize_value from validate_rewrite_semantic.py."""
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in ("true", "false"):
        return s.lower()
    if re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', s):
        from datetime import datetime, timezone
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
    # Bug-5383 FIX: removed the ``"e" not in s.lower()`` guard so that
    # scientific-notation integers like ``1.0E+5`` normalise to ``100000``.
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
        return s
    except (ValueError, OverflowError):
        pass
    return s


# ---- Tests ----

def test_plain_integer():
    assert _normalize_value(100000) == "100000"


def test_integer_string():
    assert _normalize_value("100000") == "100000"


def test_scientific_notation_integer_upper():
    """Bug-5383: ``1.0E+5`` must normalise to ``100000``."""
    assert _normalize_value("1.0E+5") == "100000"


def test_scientific_notation_integer_lower():
    assert _normalize_value("1e5") == "100000"


def test_scientific_notation_integer_no_decimal():
    assert _normalize_value("1E+5") == "100000"


def test_scientific_notation_large():
    assert _normalize_value("1.5E+6") == "1500000"


def test_decimal_non_integer_passthrough():
    """Non-integer decimals should stay as-is."""
    assert _normalize_value("3.14") == "3.14"


def test_scientific_notation_whole_after_conversion():
    """``1.5E+1`` = 15.0 which is a whole number."""
    assert _normalize_value("1.5E+1") == "15"


def test_none_passthrough():
    assert _normalize_value(None) is None


def test_boolean_normalisation():
    assert _normalize_value("True") == "true"
    assert _normalize_value("False") == "false"


def test_non_numeric_string():
    assert _normalize_value("hello") == "hello"


def test_float_zero():
    assert _normalize_value("0.0") == "0"


def test_negative_integer():
    assert _normalize_value("-100") == "-100"


def test_negative_scientific():
    assert _normalize_value("-1E+3") == "-1000"


def test_float_string_whole_number():
    """A string like ``100000.0`` (from JSON float serialization)."""
    assert _normalize_value("100000.0") == "100000"
