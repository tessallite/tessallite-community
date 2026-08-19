"""Bug-5383 / Bug-6054: JDBC wire value normalisation.

Bug-5383: asyncpg may return ``Decimal('1E+5')`` for aggregate results.
Pydantic serialises that as ``"1.0E+5"`` in the JSON response.  The JDBC
gateway converts JSON values to wire strings via ``str(v)``; without
normalisation a whole-number COUNT(*) renders as ``1.0E+5`` instead of
``100000``.

Bug-6054 (F-001-17): the original normaliser round-tripped EVERY value
through ``float()``, which silently corrupted (a) integers above 2^53
(bigint keys lose precision) and (b) TEXT column values that happen to parse
as numbers (leading-zero codes, ``"1e5"``-shaped strings got rewritten). The
corrected helper is lossless and type-aware: string values are reshaped ONLY
when the result column's catalogue type is numeric (``is_numeric=True``);
native ``int`` renders exactly at any magnitude.

Per the failing-test triage policy, the scientific-notation-string cases
below now pass ``is_numeric=True`` to model an aggregate on a NUMERIC column
(the old assertions encoded the type-blind bug the review flagged).
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# The function is module-level in server.py.  Import it directly.
_GATEWAY_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)

from jdbc.server import _normalize_jdbc_row, _normalize_jdbc_value


def test_plain_integer():
    assert _normalize_jdbc_value(100000) == "100000"


def test_float_whole_number():
    assert _normalize_jdbc_value(100000.0) == "100000"


def test_decimal_whole_number():
    """A native Decimal aggregate renders as a plain integer (Bug-5383)."""
    assert _normalize_jdbc_value(Decimal("1E+5")) == "100000"


def test_scientific_notation_string_numeric_column():
    """Bug-5383: ``1.0E+5`` on a numeric column must render as ``100000``."""
    assert _normalize_jdbc_value("1.0E+5", is_numeric=True) == "100000"


def test_scientific_notation_lower_numeric_column():
    assert _normalize_jdbc_value("1e5", is_numeric=True) == "100000"


def test_non_integer_float():
    assert _normalize_jdbc_value(3.14) == "3.14"


def test_non_integer_string():
    assert _normalize_jdbc_value("3.14") == "3.14"
    assert _normalize_jdbc_value("3.14", is_numeric=True) == "3.14"


def test_string_passthrough():
    assert _normalize_jdbc_value("hello") == "hello"


def test_zero():
    assert _normalize_jdbc_value(0) == "0"


def test_negative_integer():
    assert _normalize_jdbc_value(-500) == "-500"


def test_negative_scientific_numeric_column():
    assert _normalize_jdbc_value("-1E+3", is_numeric=True) == "-1000"


def test_large_scientific_numeric_column():
    assert _normalize_jdbc_value("1E+10", is_numeric=True) == "10000000000"


def test_boolean_stays_string():
    """Booleans should pass through as their string representation."""
    assert _normalize_jdbc_value(True) == "True"
    assert _normalize_jdbc_value(False) == "False"


def test_none_like_values():
    """None is handled by the caller; this tests str(None) behaviour."""
    # The caller guards None before calling; but str(None) = "None"
    assert _normalize_jdbc_value("None") == "None"


# ---------------------------------------------------------------------------
# Bug-6054 (F-001-17): no precision loss on big integers, no rewriting of
# numeric-looking TEXT codes.
# ---------------------------------------------------------------------------


def test_bigint_python_int_exact_above_2_53():
    """A bigint key above 2^53 must render exactly (no float round-trip)."""
    v = 9007199254740993  # 2^53 + 1
    assert _normalize_jdbc_value(v) == "9007199254740993"


def test_bigint_string_numeric_column_exact():
    """A big-integer STRING on a numeric column stays exact, not float-rounded."""
    assert _normalize_jdbc_value("9007199254740993", is_numeric=True) == "9007199254740993"


def test_oversized_numeric_string_id_numeric_column_unchanged():
    big = "123456789012345678901"
    assert _normalize_jdbc_value(big, is_numeric=True) == big


def test_leading_zero_text_code_unchanged():
    """A TEXT product/zip code with leading zeros must survive verbatim."""
    assert _normalize_jdbc_value("0042") == "0042"
    assert _normalize_jdbc_value("0042", is_numeric=False) == "0042"


def test_scientific_looking_text_value_unchanged():
    """``1e5`` in a TEXT column is a code, not a number — keep it verbatim."""
    assert _normalize_jdbc_value("1e5") == "1e5"
    assert _normalize_jdbc_value("1e5", is_numeric=False) == "1e5"


def test_decimal_string_trailing_zero_numeric_column():
    assert _normalize_jdbc_value("100000.0", is_numeric=True) == "100000"


def test_non_integer_decimal_string_numeric_column_kept():
    """A genuine fractional value keeps its exact text (e.g. money)."""
    assert _normalize_jdbc_value("12.10", is_numeric=True) == "12.10"


def test_row_normalisation_uses_column_type_context():
    """Bug-6054: row marshalling must protect text codes and bigint precision."""
    row = {
        "code": "0042",
        "big_id": "9007199254740993",
        "count_value": "1.0E+5",
    }
    assert _normalize_jdbc_row(
        row,
        ["code", "big_id", "count_value"],
        {"big_id", "count_value"},
    ) == ["0042", "9007199254740993", "100000"]
