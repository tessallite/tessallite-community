"""Bug-5383: JDBC wire integer formatting.

asyncpg may return ``Decimal('1E+5')`` for aggregate results.  Pydantic
serialises that as ``"1.0E+5"`` in the JSON response.  The JDBC gateway
converts JSON values to wire strings via ``str(v)``; without normalisation
a whole-number COUNT(*) renders as ``1.0E+5`` instead of ``100000``.

The ``_normalize_jdbc_value`` helper normalises numeric values so that
whole-number floats and scientific-notation strings render as plain
integer strings on the JDBC wire.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The function is module-level in server.py.  Import it directly.
_GATEWAY_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)

from jdbc.server import _normalize_jdbc_value


def test_plain_integer():
    assert _normalize_jdbc_value(100000) == "100000"


def test_float_whole_number():
    assert _normalize_jdbc_value(100000.0) == "100000"


def test_scientific_notation_string():
    """Bug-5383: ``1.0E+5`` must render as ``100000``."""
    assert _normalize_jdbc_value("1.0E+5") == "100000"


def test_scientific_notation_lower():
    assert _normalize_jdbc_value("1e5") == "100000"


def test_non_integer_float():
    assert _normalize_jdbc_value(3.14) == "3.14"


def test_non_integer_string():
    assert _normalize_jdbc_value("3.14") == "3.14"


def test_string_passthrough():
    assert _normalize_jdbc_value("hello") == "hello"


def test_zero():
    assert _normalize_jdbc_value(0) == "0"


def test_negative_integer():
    assert _normalize_jdbc_value(-500) == "-500"


def test_negative_scientific():
    assert _normalize_jdbc_value("-1E+3") == "-1000"


def test_large_integer():
    assert _normalize_jdbc_value("1E+10") == "10000000000"


def test_boolean_stays_string():
    """Booleans should pass through as their string representation."""
    assert _normalize_jdbc_value(True) == "True"
    assert _normalize_jdbc_value(False) == "False"


def test_none_like_values():
    """None is handled by the caller; this tests str(None) behaviour."""
    # The caller guards None before calling; but str(None) = "None"
    assert _normalize_jdbc_value("None") == "None"
