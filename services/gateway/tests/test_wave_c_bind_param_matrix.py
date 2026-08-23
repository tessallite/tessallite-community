"""Wave C #6 — pgwire Bind parameters TERMINATED into typed safe SQL literals.

The gateway decodes typed Bind values and inlines them as safe literals; it never
guesses. This is the param type matrix: text / numeric / bool / date / timestamp /
NULL / quotes+semicolons all produce a stable typed literal; an unsupported OID or
an unsupported/invalid binary encoding produces a STABLE protocol error, never a
guessed value.
"""
from __future__ import annotations

import struct

import pytest

from src.jdbc import protocol as proto
from src.jdbc.protocol import ParamDecodeError, _decode_binary_param
from src.jdbc.server import _substitute_params

_SQL = "SELECT * FROM t WHERE c = $1"


# ---------------------------------------------------------------------------
# Text-format termination — typed literals, injection-safe
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("oid", "value", "expected"),
    [
        (proto.OID_TEXT, "hello", "SELECT * FROM t WHERE c = 'hello'"),
        (proto.OID_INT4, "42", "SELECT * FROM t WHERE c = 42"),          # numeric inline
        (proto.OID_INT4, "4x2", "SELECT * FROM t WHERE c = '4x2'"),      # malformed → quoted, not raw
        (proto.OID_NUMERIC, "3.14", "SELECT * FROM t WHERE c = 3.14"),
        (proto.OID_BOOL, "true", "SELECT * FROM t WHERE c = 'true'"),
        (proto.OID_DATE, "2024-01-01", "SELECT * FROM t WHERE c = '2024-01-01'"),
        (proto.OID_TIMESTAMP, "2024-01-01 12:00:00",
         "SELECT * FROM t WHERE c = '2024-01-01 12:00:00'"),
        (proto.OID_TEXT, "007", "SELECT * FROM t WHERE c = '007'"),      # numeric-looking text stays text
    ],
)
def test_text_param_typed_literal(oid, value, expected):
    assert _substitute_params(_SQL, [value], [oid]) == expected


def test_null_param_becomes_null_literal():
    assert _substitute_params(_SQL, [None], [proto.OID_TEXT]) == "SELECT * FROM t WHERE c = NULL"


def test_quotes_and_semicolons_are_escaped_not_executed():
    # An embedded quote + semicolon must stay INSIDE the string literal.
    out = _substitute_params(_SQL, ["a'; DROP TABLE users;--"], [proto.OID_TEXT])
    assert out == "SELECT * FROM t WHERE c = 'a''; DROP TABLE users;--'"
    # No unescaped single-quote breaks out of the literal.
    assert out.count("'") % 2 == 0


def test_unsupported_text_oid_is_stable_error():
    # OID 114 = json; the gateway cannot terminate it as a scalar literal.
    with pytest.raises(ParamDecodeError):
        _substitute_params(_SQL, ["{}"], [114])


def test_undeclared_oid_zero_infers_numeric_or_text():
    # OID 0 (unspecified): strict-numeric inlines, everything else quotes as text.
    assert _substitute_params(_SQL, ["42"], [0]) == "SELECT * FROM t WHERE c = 42"
    assert _substitute_params(_SQL, ["ab"], [0]) == "SELECT * FROM t WHERE c = 'ab'"


# ---------------------------------------------------------------------------
# Binary-format decode — strict, never guessed
# ---------------------------------------------------------------------------

def test_binary_supported_types_decode_exactly():
    assert _decode_binary_param(struct.pack("!i", -7), proto.OID_INT4) == "-7"
    assert _decode_binary_param(struct.pack("!h", 5), proto.OID_INT2) == "5"
    assert _decode_binary_param(struct.pack("!q", 99), proto.OID_INT8) == "99"
    assert _decode_binary_param(struct.pack("!d", 3.5), proto.OID_FLOAT8) == "3.5"
    assert _decode_binary_param(b"\x01", proto.OID_BOOL) == "true"
    assert _decode_binary_param(b"\x00", proto.OID_BOOL) == "false"
    assert _decode_binary_param(b"caf\xc3\xa9", proto.OID_TEXT) == "café"
    # date: days since 2000-01-01 → 2000-01-02
    assert _decode_binary_param(struct.pack("!i", 1), proto.OID_DATE) == "2000-01-02"


def test_binary_undeclared_oid_zero_is_error():
    # A binary param with no declared type is genuinely ambiguous → refuse.
    with pytest.raises(ParamDecodeError):
        _decode_binary_param(struct.pack("!i", 42), 0)


def test_binary_unsupported_type_is_error():
    # NUMERIC binary is a digit array with no lossless scalar decoder here.
    with pytest.raises(ParamDecodeError):
        _decode_binary_param(b"\x00\x00", proto.OID_NUMERIC)


def test_binary_wrong_width_is_error():
    # INT4 must be exactly 4 bytes; 3 bytes is a malformed binary representation.
    with pytest.raises(ParamDecodeError) as exc:
        _decode_binary_param(b"\x00\x00\x00", proto.OID_INT4)
    assert exc.value.sqlstate == "22P03"


def test_binary_invalid_utf8_text_is_error():
    with pytest.raises(ParamDecodeError):
        _decode_binary_param(b"\xff\xfe", proto.OID_TEXT)


# ---------------------------------------------------------------------------
# End-to-end through the wire helper: a binary-decode failure propagates so the
# Bind handler can emit a stable ErrorResponse (never a guessed value).
# ---------------------------------------------------------------------------

def _bind_payload(oid_bytes: bytes) -> bytes:
    # portal="", stmt="", 1 format code (binary), 1 param, then result formats=0.
    body = b"\x00" + b"\x00"
    body += struct.pack("!H", 1) + struct.pack("!H", 1)   # one format code: binary
    body += struct.pack("!H", 1)                           # one param
    body += struct.pack("!i", len(oid_bytes)) + oid_bytes
    body += struct.pack("!H", 0)                           # no result format codes
    return body


def test_parse_bind_propagates_binary_error_for_unsupported_oid():
    payload = _bind_payload(b"\x00\x00")
    with pytest.raises(ParamDecodeError):
        proto.parse_bind_parameters(payload, param_oids=[proto.OID_NUMERIC])
