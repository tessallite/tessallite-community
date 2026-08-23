"""
PostgreSQL wire protocol v3 framing helpers.

Implements the subset of the PostgreSQL frontend/backend protocol required
for JDBC BI tool connectivity:

  Client → Server (frontend messages):
    SSLRequest (80877103)
    StartupMessage (protocol 196608)
    Query ('Q')
    Terminate ('X')

  Server → Client (backend messages):
    AuthenticationOK ('R')
    ParameterStatus ('S')
    BackendKeyData ('K')
    ReadyForQuery ('Z')
    RowDescription ('T')
    DataRow ('D')
    CommandComplete ('C')
    ErrorResponse ('E')
    EmptyQueryResponse ('I')
    NoticeResponse ('N')

Wire encoding: big-endian, length prefix includes itself (4 bytes).
Startup/SSLRequest are headerless (no type byte).
All other messages: 1-byte type tag + 4-byte length (including length field).
"""
from __future__ import annotations

import datetime as _dt
import decimal as _decimal
import os
import struct
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SSL_REQUEST_CODE = 80877103   # 1234 5679 in big-endian 32-bit int
CANCEL_REQUEST_CODE = 80877102  # 1234 5678 — CancelRequest (F-001-12)
PROTOCOL_VERSION = 196608     # 3.0  (major 3 << 16 | minor 0)

# F-001-07: pre-auth frame-length caps so a client cannot force ~4 GiB
# allocations per frame on the public listener. PostgreSQL itself rejects
# oversize startup packets; regular messages are bounded a few MB.
MAX_STARTUP_LEN = 10 * 1024          # 10 KB — startup / SSL / cancel
MAX_MESSAGE_LEN = 16 * 1024 * 1024   # 16 MB — regular protocol messages


class FrameTooLargeError(ValueError):
    """Raised when a client-declared frame length exceeds the allowed bound."""


class ParamDecodeError(ValueError):
    """A bound parameter cannot be terminated into a safe typed SQL literal.

    Wave C #6: the gateway TERMINATES pgwire Bind parameters into typed safe SQL
    literals; it must never GUESS a value. This is raised for an unsupported
    parameter type OID, or a binary encoding it cannot decode exactly (unknown
    OID, wrong byte width, or invalid UTF-8). The Bind handler maps it to a stable
    protocol ErrorResponse (``sqlstate``) instead of inlining a guessed value that
    could silently change a number. Carries a stable SQLSTATE (default
    ``0A000`` feature_not_supported; ``22P03`` invalid_binary_representation for a
    malformed binary payload).
    """

    def __init__(self, message: str, sqlstate: str = "0A000"):
        super().__init__(message)
        self.sqlstate = sqlstate


def _checked_frame_length(length: int, limit: int) -> int:
    """Validate the declared frame length against *limit* (F-001-07)."""
    if length < 4 or (length - 4) > limit:
        raise FrameTooLargeError(
            f"Declared frame length {length} exceeds limit {limit + 4}"
        )
    return length

# Authentication response codes
AUTH_OK = 0
AUTH_CLEARTEXT = 3

# Transaction status bytes (ReadyForQuery)
TXN_IDLE = b"I"
TXN_IN_TXN = b"T"
TXN_FAILED = b"E"

# PostgreSQL type OIDs used for column descriptions
OID_TEXT = 25
OID_INT2 = 21
OID_INT4 = 23
OID_INT8 = 20
OID_FLOAT4 = 700
OID_FLOAT8 = 701
OID_BOOL = 16
OID_NUMERIC = 1700
OID_DATE = 1082
OID_TIME = 1083
OID_TIMESTAMP = 1114
OID_TIMESTAMPTZ = 1184
OID_TIMETZ = 1266

# OIDs the gateway can encode in the PostgreSQL BINARY wire format. This is an
# ALLOW-list, not a deny-list, so a type OID added to ``server._map_type_oid``
# without an encoder here downgrades to text automatically instead of emitting a
# payload no client can parse.
#
# Bug-3655 originally listed the INVERSE — the six types with no encoder — and
# forced their format code to 0 so ``row_description`` and ``data_row`` stayed in
# lockstep. That reasoning holds only for a client that reads the RowDescription
# the gateway sends alongside the rows. A prepared-statement client does not: it
# fixes the result formats from the STATEMENT description at Bind time and never
# re-reads them, so it requested binary NUMERIC, received text, and decoded
# garbage (asyncpg: ``insufficient data in buffer``). A server may not downgrade
# a format the client asked for, so the encoders below implement the six types
# rather than declining them.
_BINARY_ENCODABLE_OIDS = frozenset({
    OID_TEXT, OID_BOOL, OID_INT2, OID_INT4, OID_INT8, 26,  # 26 = oid
    OID_FLOAT4, OID_FLOAT8,
    OID_NUMERIC, OID_DATE, OID_TIME, OID_TIMETZ, OID_TIMESTAMP, OID_TIMESTAMPTZ,
})


class BinaryEncodeError(Exception):
    """A value cannot be encoded in the binary format its column advertises.

    Raised instead of silently falling back to text bytes: under a binary format
    code the client parses the payload as binary, so a text fallback is not a
    degradation, it is a corrupted value. The extended-protocol Execute handler
    converts this into an ErrorResponse.
    """

    def __init__(self, message: str, sqlstate: str = "22P03"):
        super().__init__(message)
        self.sqlstate = sqlstate


def _effective_result_format(requested_fmt: int, oid: int) -> int:
    """Return the format code actually used for a column.

    Downgrades a requested binary (1) format to text (0) only for OIDs with no
    binary encoder, so the advertised format and the emitted payload agree.
    """
    if requested_fmt == 1 and oid not in _BINARY_ENCODABLE_OIDS:
        return 0
    return requested_fmt


# ---------------------------------------------------------------------------
# Low-level read helpers
# ---------------------------------------------------------------------------

async def read_bytes(reader, n: int) -> bytes:
    """Read exactly *n* bytes from an asyncio StreamReader."""
    data = await reader.readexactly(n)
    return data


async def read_startup(reader) -> dict[str, Any]:
    """
    Read either SSLRequest or StartupMessage (both lack a type byte).

    Returns a dict with key "type":
      {"type": "ssl"}
      {"type": "startup", "params": {key: value, ...}}
    """
    length_bytes = await read_bytes(reader, 4)
    length = struct.unpack("!I", length_bytes)[0]
    _checked_frame_length(length, MAX_STARTUP_LEN)
    payload = await read_bytes(reader, length - 4)

    code = struct.unpack("!I", payload[:4])[0]

    if code == SSL_REQUEST_CODE:
        return {"type": "ssl"}

    if code == CANCEL_REQUEST_CODE:
        # CancelRequest: backend PID + secret key follow the code (F-001-12).
        pid = secret = 0
        if len(payload) >= 12:
            pid, secret = struct.unpack("!II", payload[4:12])
        return {"type": "cancel", "pid": pid, "secret": secret}

    if code == PROTOCOL_VERSION:
        # Parse NUL-terminated key/value pairs
        params: dict[str, str] = {}
        rest = payload[4:]
        parts = rest.split(b"\x00")
        # parts = [key, val, key, val, ..., ""]
        it = iter(parts)
        for key_b in it:
            if not key_b:
                break
            val_b = next(it, b"")
            params[key_b.decode()] = val_b.decode()
        return {"type": "startup", "params": params}

    raise ValueError(f"Unexpected startup code: {code}")


async def read_password_message(reader) -> str:
    """
    Read a PasswordMessage ('p') sent by the client after an auth challenge.

    Returns the password string (strips trailing NUL).
    Raises ValueError if the message type is not 'p'.
    """
    type_byte = await read_bytes(reader, 1)
    length_bytes = await read_bytes(reader, 4)
    length = struct.unpack("!I", length_bytes)[0]
    _checked_frame_length(length, MAX_STARTUP_LEN)
    payload = await read_bytes(reader, length - 4)
    if type_byte != b"p":
        raise ValueError(f"Expected PasswordMessage ('p'), got {type_byte!r}")
    return payload.rstrip(b"\x00").decode("utf-8", errors="replace")


async def read_message(reader) -> tuple[str, bytes]:
    """
    Read a standard frontend message: 1-byte type + 4-byte length + payload.

    Returns (type_char, payload_bytes).
    """
    type_byte = await read_bytes(reader, 1)
    length_bytes = await read_bytes(reader, 4)
    length = struct.unpack("!I", length_bytes)[0]
    _checked_frame_length(length, MAX_MESSAGE_LEN)
    payload = await read_bytes(reader, length - 4)
    return type_byte.decode("ascii"), payload


# ---------------------------------------------------------------------------
# Backend message builders
# ---------------------------------------------------------------------------

def _msg(type_byte: str, payload: bytes) -> bytes:
    """Build a standard backend message."""
    length = len(payload) + 4  # includes the 4-byte length field
    return type_byte.encode("ascii") + struct.pack("!I", length) + payload


def ssl_deny() -> bytes:
    return b"N"


def ssl_accept() -> bytes:
    return b"S"


def authentication_ok() -> bytes:
    """AuthenticationOK (R)."""
    return _msg("R", struct.pack("!I", AUTH_OK))


def authentication_cleartext_password() -> bytes:
    """AuthenticationCleartextPassword (R, code 3) — request password from client."""
    return _msg("R", struct.pack("!I", AUTH_CLEARTEXT))


def parameter_status(name: str, value: str) -> bytes:
    """ParameterStatus (S)."""
    payload = name.encode() + b"\x00" + value.encode() + b"\x00"
    return _msg("S", payload)


def backend_key_data(pid: int = 1, secret: int = 0) -> bytes:
    """BackendKeyData (K)."""
    return _msg("K", struct.pack("!II", pid, secret))


def ready_for_query(status: bytes = TXN_IDLE) -> bytes:
    """ReadyForQuery (Z)."""
    return _msg("Z", status)


def row_description(
    columns: list[tuple[str, int]],
    result_formats: list[int] | None = None,
) -> bytes:
    """RowDescription (T).

    columns: list of (name, type_oid).
    When *result_formats* specifies binary (1), the per-column format code
    is set accordingly so the client knows how to decode DataRow values.
    """
    n = len(columns)
    payload = struct.pack("!H", n)
    for i, (name, oid) in enumerate(columns):
        fmt = _effective_result_format(
            _result_format_for_col(result_formats, i), oid,
        )
        payload += (
            name.encode() + b"\x00"      # column name, NUL-terminated
            + struct.pack("!I", 0)        # table OID (0 = not a table column)
            + struct.pack("!H", 0)        # column attribute number
            + struct.pack("!I", oid)      # type OID
            + struct.pack("!H", -1 & 0xFFFF)  # type size (-1 = variable)
            + struct.pack("!I", -1 & 0xFFFFFFFF)  # type modifier
            + struct.pack("!H", fmt)      # format code
        )
    return _msg("T", payload)


def data_row(
    values: list[str | None],
    result_formats: list[int] | None = None,
    col_oids: list[int] | None = None,
) -> bytes:
    """DataRow (D).

    When *result_formats* requests binary (1), values are encoded using
    the PG binary wire format for the column's type OID.  Otherwise
    values are text-encoded (format code 0).  None → NULL (-1 length).
    """
    n = len(values)
    payload = struct.pack("!H", n)
    for i, v in enumerate(values):
        if v is None:
            payload += struct.pack("!i", -1)
            continue
        oid = col_oids[i] if col_oids and i < len(col_oids) else OID_TEXT
        # Bug-3655: downgrade to text for OIDs the gateway cannot binary-encode,
        # matching the format code row_description advertises for the column.
        fmt = _effective_result_format(
            _result_format_for_col(result_formats, i), oid,
        )
        if fmt == 1:
            encoded = _encode_binary_value(str(v), oid)
            payload += struct.pack("!I", len(encoded)) + encoded
        else:
            encoded = str(v).encode("utf-8")
            payload += struct.pack("!I", len(encoded)) + encoded
    return _msg("D", payload)


def _result_format_for_col(result_formats: list[int] | None, idx: int) -> int:
    if not result_formats:
        return 0
    if len(result_formats) == 1:
        return result_formats[0]
    if idx < len(result_formats):
        return result_formats[idx]
    return 0


# PostgreSQL's binary date/time epoch: 2000-01-01T00:00:00.
_PG_EPOCH_DATE = _dt.date(2000, 1, 1)
_PG_EPOCH_NAIVE = _dt.datetime(2000, 1, 1)
_PG_EPOCH_AWARE = _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc)

# NUMERIC sign words (see PostgreSQL ``src/backend/utils/adt/numeric.c``).
_NUMERIC_POS = 0x0000
_NUMERIC_NEG = 0x4000
_NUMERIC_NAN = 0xC000


def _encode_numeric_binary(text: str) -> bytes:
    """PG binary NUMERIC: ndigits, weight, sign, dscale + base-10000 digits.

    ``Decimal`` arithmetic is context-bound: ``abs()``, unary ``-`` and other
    operators round at the ambient decimal precision. Parse and split the
    exact coefficient as text so wide NUMERIC values cannot be silently
    changed before their wire representation is built.
    """
    raw = text.strip()
    try:
        value = _decimal.Decimal(raw)
    except (_decimal.InvalidOperation, ValueError) as exc:
        raise BinaryEncodeError(
            f"value {text!r} is not a valid NUMERIC for binary encoding"
        ) from exc
    if value.is_nan():
        return struct.pack("!hhHh", 0, 0, _NUMERIC_NAN, 0)
    if value.is_infinite():
        # PostgreSQL 14+ has ±Infinity numerics, but the gateway never produces
        # one; refuse rather than emit a representation the client may not know.
        raise BinaryEncodeError(
            f"value {text!r} is an infinite NUMERIC, which the gateway does "
            "not serve in binary format"
        )
    with _decimal.localcontext() as ctx:
        ctx.prec = _decimal.MAX_PREC
        sign = _NUMERIC_NEG if value < 0 else _NUMERIC_POS
        _sign, digits, exponent = value.copy_abs().as_tuple()
    exponent = int(exponent)
    digit_text = "".join(str(d) for d in digits) or "0"
    if exponent > 0:
        digit_text += "0" * exponent
        exponent = 0
    # dscale is the number of digits kept AFTER the decimal point, never negative.
    dscale = -exponent
    if len(digit_text) <= dscale:
        int_text = ""
        frac_text = digit_text.rjust(dscale, "0")
    else:
        split = len(digit_text) - dscale
        int_text = digit_text[:split]
        frac_text = digit_text[split:]

    # Integer side: base-10000 groups, most significant first.
    int_text = int_text.lstrip("0")
    lead = len(int_text) % 4
    int_groups: list[int] = []
    if lead:
        int_groups.append(int(int_text[:lead]))
    int_groups.extend(
        int(int_text[i:i + 4]) for i in range(lead, len(int_text), 4)
    )

    # Fraction side: pad the scale to a multiple of 4 so it splits into whole
    # base-10000 groups, most significant first.
    frac_groups: list[int] = []
    if dscale:
        padded_scale = ((dscale + 3) // 4) * 4
        frac_text = frac_text.ljust(padded_scale, "0")
        frac_groups = [
            int(frac_text[i:i + 4]) for i in range(0, padded_scale, 4)
        ]

    weight = len(int_groups) - 1
    all_groups = int_groups + frac_groups
    # Trim leading zero groups (adjusting weight) and trailing zero groups; PG
    # stores no leading/trailing all-zero digit words.
    while all_groups and all_groups[0] == 0:
        all_groups.pop(0)
        weight -= 1
    while all_groups and all_groups[-1] == 0:
        all_groups.pop()
    if not all_groups:
        # Exact zero: no digit words, weight 0, dscale preserved.
        return _pack_numeric_header(text, 0, 0, sign, dscale)
    payload = _pack_numeric_header(text, len(all_groups), weight, sign, dscale)
    for group in all_groups:
        payload += struct.pack("!h", group)
    return payload


def _pack_numeric_header(
    text: str, ndigits: int, weight: int, sign: int, dscale: int
) -> bytes:
    """Pack NUMERIC int16 header fields as a typed binary-encoding failure."""
    try:
        return struct.pack("!hhHh", ndigits, weight, sign, dscale)
    except struct.error as exc:
        raise BinaryEncodeError(
            f"value {text!r} does not fit the PostgreSQL NUMERIC wire format "
            f"(ndigits={ndigits}, weight={weight}, dscale={dscale})"
        ) from exc


def _parse_iso_datetime(text: str) -> _dt.datetime:
    raw = text.strip().replace(" ", "T", 1) if " " in text.strip() else text.strip()
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BinaryEncodeError(
            f"value {text!r} is not an ISO-8601 timestamp for binary encoding"
        ) from exc


def _encode_date_binary(text: str) -> bytes:
    """PG binary DATE: int32 days since 2000-01-01."""
    raw = text.strip()
    try:
        value = _dt.date.fromisoformat(raw)
    except ValueError:
        value = _parse_iso_datetime(raw).date()
    return struct.pack("!i", (value - _PG_EPOCH_DATE).days)


def _encode_timestamp_binary(text: str, *, aware: bool) -> bytes:
    """PG binary TIMESTAMP/TIMESTAMPTZ: int64 microseconds since 2000-01-01."""
    value = _parse_iso_datetime(text)
    if aware:
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        delta = value - _PG_EPOCH_AWARE
    else:
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        delta = value - _PG_EPOCH_NAIVE
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return struct.pack("!q", micros)


def _parse_iso_time(text: str) -> _dt.time:
    raw = text.strip()
    try:
        return _dt.time.fromisoformat(raw)
    except ValueError as exc:
        raise BinaryEncodeError(
            f"value {text!r} is not an ISO-8601 time for binary encoding"
        ) from exc


def _encode_time_binary(text: str) -> bytes:
    """PG binary TIME: int64 microseconds since midnight."""
    value = _parse_iso_time(text)
    micros = (
        (value.hour * 3600 + value.minute * 60 + value.second) * 1_000_000
        + value.microsecond
    )
    return struct.pack("!q", micros)


def _encode_timetz_binary(text: str) -> bytes:
    """PG binary TIMETZ: int64 microseconds since midnight + int32 zone offset.

    The zone field is seconds WEST of UTC — the negation of Python's
    ``utcoffset()``, which counts seconds east.
    """
    value = _parse_iso_time(text)
    micros = (
        (value.hour * 3600 + value.minute * 60 + value.second) * 1_000_000
        + value.microsecond
    )
    offset = value.utcoffset()
    zone = -int(offset.total_seconds()) if offset is not None else 0
    return struct.pack("!qi", micros, zone)


def _encode_binary_value(text: str, oid: int) -> bytes:
    """Encode a text string as PG binary wire format for the given OID.

    Raises :class:`BinaryEncodeError` when the value does not fit the type the
    column advertises. Falling back to text bytes (the pre-fix behaviour) is not
    safe here: the client parses the payload according to the BINARY format code
    it requested, so a text fallback silently corrupts the value rather than
    degrading it.
    """
    if oid == OID_BOOL:
        lowered = text.strip().lower()
        if lowered in ("true", "t", "1", "yes", "y", "on"):
            return b"\x01"
        if lowered in ("false", "f", "0", "no", "n", "off"):
            return b"\x00"
        raise BinaryEncodeError(
            f"value {text!r} is not a valid BOOLEAN for binary encoding"
        )
    if oid in (OID_INT2, OID_INT4, OID_INT8, 26):
        fmt = {OID_INT2: "!h", OID_INT4: "!i", 26: "!I", OID_INT8: "!q"}[oid]
        try:
            return struct.pack(fmt, int(text.strip()))
        except (ValueError, struct.error) as exc:
            raise BinaryEncodeError(
                f"value {text!r} does not fit the integer type (OID {oid}) "
                "its column advertises"
            ) from exc
    if oid in (OID_FLOAT4, OID_FLOAT8):
        fmt = "!f" if oid == OID_FLOAT4 else "!d"
        try:
            return struct.pack(fmt, float(text.strip()))
        except (ValueError, struct.error) as exc:
            raise BinaryEncodeError(
                f"value {text!r} is not a valid floating-point number for "
                f"binary encoding (OID {oid})"
            ) from exc
    if oid == OID_NUMERIC:
        return _encode_numeric_binary(text)
    if oid == OID_DATE:
        return _encode_date_binary(text)
    if oid == OID_TIME:
        return _encode_time_binary(text)
    if oid == OID_TIMETZ:
        return _encode_timetz_binary(text)
    if oid in (OID_TIMESTAMP, OID_TIMESTAMPTZ):
        return _encode_timestamp_binary(text, aware=(oid == OID_TIMESTAMPTZ))
    # TEXT and anything else with no distinct binary representation: the binary
    # form IS the UTF-8 bytes. ``_effective_result_format`` has already forced
    # format 0 for any OID absent from ``_BINARY_ENCODABLE_OIDS``, so an
    # unrecognised OID never reaches here under a binary format code.
    return text.encode("utf-8")


def command_complete(tag: str) -> bytes:
    """CommandComplete (C). tag e.g. 'SELECT 5'."""
    return _msg("C", tag.encode() + b"\x00")


def empty_query_response() -> bytes:
    """EmptyQueryResponse (I)."""
    return _msg("I", b"")


def error_response(
    message: str,
    severity: str = "ERROR",
    code: str = "42601",
) -> bytes:
    """
    ErrorResponse (E).

    Sends minimal required fields: severity (S), code (C), message (M).
    """
    payload = (
        b"S" + severity.encode() + b"\x00"
        + b"C" + code.encode() + b"\x00"
        + b"M" + message.encode() + b"\x00"
        + b"\x00"  # terminator
    )
    return _msg("E", payload)


def parse_complete() -> bytes:
    """ParseComplete (1)."""
    return _msg("1", b"")


def bind_complete() -> bytes:
    """BindComplete (2)."""
    return _msg("2", b"")


def close_complete() -> bytes:
    """CloseComplete (3)."""
    return _msg("3", b"")


def no_data() -> bytes:
    """NoData (n) — sent in response to Describe when there are no columns."""
    return _msg("n", b"")


def portal_suspended() -> bytes:
    """PortalSuspended (s) — sent when Execute stops before exhausting all rows.

    Bug-6935: clients that request bounded fetches (max_rows > 0 in Execute)
    expect PortalSuspended followed by further Execute calls.
    """
    return _msg("s", b"")


def parameter_description(type_oids: list[int] | None = None) -> bytes:
    """ParameterDescription (t) — describes statement parameters.

    asyncpg expects this after Describe Statement (before RowDescription).
    The caller passes one OID per ``$N`` placeholder in the statement (the
    gateway advertises TEXT for each); ``[]`` yields a count of 0.
    """
    oids = type_oids or []
    payload = struct.pack("!H", len(oids))
    for oid in oids:
        payload += struct.pack("!I", oid)
    return _msg("t", payload)


def notice_response(message: str) -> bytes:
    """NoticeResponse (N) — non-fatal informational message."""
    payload = (
        b"SM" + message.encode() + b"\x00"
        + b"\x00"
    )
    return _msg("N", payload)


# ---------------------------------------------------------------------------
# Bind message parser
# ---------------------------------------------------------------------------


def peek_bind_names(payload: bytes) -> tuple[str, str]:
    """Extract only portal and statement names from a Bind payload (Bug-5187).

    This is a lightweight peek used to resolve the statement's declared
    parameter OIDs *before* the full Bind parse, so binary parameter
    decoding can use OID-driven logic instead of byte-length heuristics.
    """
    nul1 = payload.index(b"\x00", 0)
    portal = payload[:nul1].decode("utf-8", errors="replace")
    nul2 = payload.index(b"\x00", nul1 + 1)
    stmt = payload[nul1 + 1:nul2].decode("utf-8", errors="replace")
    return portal, stmt


def parse_bind_parameters(
    payload: bytes,
    param_oids: list[int] | None = None,
) -> tuple[str, str, list[str | None], list[int]]:
    """Parse a Bind message payload.

    Returns ``(portal, statement, params, result_format_codes)``.

    Parameter values are returned as text strings (or None for NULL).
    Binary-format values are decoded using the declared parameter OIDs
    from the Parse message (Bug-5187). When *param_oids* is provided,
    each binary parameter is decoded according to its declared type
    instead of guessing from byte length.
    ``result_format_codes`` contains the format codes the client requested
    for result columns (0 = text, 1 = binary).
    """
    offset = 0
    declared_oids = param_oids or []

    nul = payload.index(b"\x00", offset)
    portal_name = payload[offset:nul].decode("utf-8", errors="replace")
    offset = nul + 1

    nul = payload.index(b"\x00", offset)
    stmt_name = payload[offset:nul].decode("utf-8", errors="replace")
    offset = nul + 1

    (num_fmt,) = struct.unpack_from("!H", payload, offset)
    offset += 2
    fmt_codes: list[int] = []
    for _ in range(num_fmt):
        (fc,) = struct.unpack_from("!H", payload, offset)
        fmt_codes.append(fc)
        offset += 2

    (num_params,) = struct.unpack_from("!H", payload, offset)
    offset += 2

    params: list[str | None] = []
    for i in range(num_params):
        (plen,) = struct.unpack_from("!i", payload, offset)
        offset += 4
        if plen == -1:
            params.append(None)
        else:
            raw = payload[offset:offset + plen]
            offset += plen
            if not fmt_codes:
                fc = 0
            elif len(fmt_codes) == 1:
                fc = fmt_codes[0]
            elif i < len(fmt_codes):
                fc = fmt_codes[i]
            else:
                fc = 0

            if fc == 0:
                params.append(raw.decode("utf-8", errors="replace"))
            else:
                # Bug-5187: use the declared OID from the Parse message
                # to decode binary params, not a byte-length heuristic.
                oid = declared_oids[i] if i < len(declared_oids) else 0
                params.append(_decode_binary_param(raw, oid))

    result_format_codes: list[int] = []
    if offset + 2 <= len(payload):
        (num_result_fmt,) = struct.unpack_from("!H", payload, offset)
        offset += 2
        for _ in range(num_result_fmt):
            if offset + 2 <= len(payload):
                (rfc,) = struct.unpack_from("!H", payload, offset)
                result_format_codes.append(rfc)
                offset += 2

    return portal_name, stmt_name, params, result_format_codes


def parse_parse_message(payload: bytes) -> tuple[str, str, list[int]]:
    """Parse a Parse ('P') message payload.

    Returns ``(statement_name, query, param_type_oids)``.

    Wire format: statement-name (C-string), query (C-string), int16
    parameter-type count, then that many int32 type OIDs. Clients such as
    pgJDBC declare the parameter types here (e.g. VARCHAR vs INT4); a count of
    0 means "let the server infer" (F-001-06).
    """
    first = payload.index(b"\x00")
    stmt_name = payload[:first].decode("utf-8", errors="replace")
    second = payload.index(b"\x00", first + 1)
    query = payload[first + 1:second].decode("utf-8", errors="replace")

    offset = second + 1
    oids: list[int] = []
    if offset + 2 <= len(payload):
        (num_types,) = struct.unpack_from("!H", payload, offset)
        offset += 2
        for _ in range(num_types):
            if offset + 4 <= len(payload):
                (oid,) = struct.unpack_from("!I", payload, offset)
                oids.append(oid)
                offset += 4
    return stmt_name, query, oids


# OID groups used by the gateway to decide how to emit a bound parameter
# (F-001-06). A parameter declared with a numeric OID may be inlined raw
# after strict validation; everything else (text, unknown OID 0, dates) is
# quoted as a string literal so type semantics survive.
NUMERIC_PARAM_OIDS = frozenset({
    OID_INT2, OID_INT4, OID_INT8,
    OID_FLOAT4, OID_FLOAT8, OID_NUMERIC,
    26,  # OID
})

# Wave C #6: text-like OIDs whose binary wire form is just UTF-8 bytes.
_TEXTLIKE_OIDS = frozenset({
    OID_TEXT, 1043, 1042, 18, 19, 2950,  # text/varchar/char/"char"/name/uuid
})

# Wave C #6: OIDs the gateway can decode from PG BINARY wire format into an
# EXACT text value, with the exact byte width PostgreSQL uses. A binary payload
# of any other width (or an OID absent from this table AND not text-like) is
# refused as ``ParamDecodeError`` rather than guessed. NUMERIC/TIME/TIMETZ have
# no lossless scalar binary decoder here, so they are intentionally NOT listed —
# a binary value of those types is refused (the client may send them as text).
_BINARY_FIXED_WIDTH = {
    OID_BOOL: 1,
    OID_INT2: 2,
    OID_INT4: 4, 26: 4,
    OID_INT8: 8,
    OID_FLOAT4: 4,
    OID_FLOAT8: 8,
    OID_DATE: 4,
    OID_TIMESTAMP: 8,
    OID_TIMESTAMPTZ: 8,
}

# Wave C #6: the full set of parameter type OIDs the gateway will terminate into
# a safe SQL literal (in either text or binary format). OID 0 (unspecified) is
# treated as a text literal — PostgreSQL infers its type in context. A non-zero
# OID outside this set is refused rather than mis-typed.
SUPPORTED_PARAM_OIDS = frozenset(
    {0, OID_NUMERIC, OID_TIME, OID_TIMETZ}
    | set(_TEXTLIKE_OIDS)
    | set(NUMERIC_PARAM_OIDS)
    | {OID_BOOL}
    | {OID_DATE, OID_TIMESTAMP, OID_TIMESTAMPTZ}
)


def _decode_binary_param(data: bytes, oid: int = 0) -> str:
    """Decode a BINARY-format Bind parameter into its exact text representation.

    Wave C #6: strictly OID-driven and length-checked — NEVER a byte-length
    guess. A binary payload is decoded only when its declared OID has a known,
    lossless decoder AND the byte width matches PostgreSQL's wire width. An
    unknown/undeclared OID (including ``0``), an unsupported binary type
    (NUMERIC/TIME/TIMETZ), a wrong width, or invalid UTF-8 raises
    :class:`ParamDecodeError` so the gateway emits a stable protocol error rather
    than a guessed value (a guess can silently corrupt a number).
    """
    n = len(data)

    if oid in _TEXTLIKE_OIDS:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParamDecodeError(
                f"binary parameter for type OID {oid} is not valid UTF-8",
                sqlstate="22P03",
            ) from exc

    width = _BINARY_FIXED_WIDTH.get(oid)
    if width is None:
        raise ParamDecodeError(
            f"binary-format parameter with type OID {oid} is not supported; "
            "declare a supported scalar type or send the value in text format."
        )
    if n != width:
        raise ParamDecodeError(
            f"binary parameter for type OID {oid} has {n} byte(s), "
            f"expected {width}.",
            sqlstate="22P03",
        )

    if oid == OID_BOOL:
        return "true" if data[0] else "false"
    if oid == OID_INT2:
        return str(struct.unpack("!h", data)[0])
    if oid in (OID_INT4, 26):
        return str(struct.unpack("!i", data)[0])
    if oid == OID_INT8:
        return str(struct.unpack("!q", data)[0])
    if oid == OID_FLOAT4:
        return str(struct.unpack("!f", data)[0])
    if oid == OID_FLOAT8:
        return str(struct.unpack("!d", data)[0])
    if oid == OID_DATE:
        from datetime import date, timedelta
        return str(date(2000, 1, 1) + timedelta(days=struct.unpack("!i", data)[0]))
    # OID_TIMESTAMP / OID_TIMESTAMPTZ (8-byte microseconds since 2000-01-01).
    from datetime import datetime, timedelta, timezone
    dt = datetime(2000, 1, 1) + timedelta(microseconds=struct.unpack("!q", data)[0])
    if oid == OID_TIMESTAMPTZ:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(dt)


# ---------------------------------------------------------------------------
# Startup parameter block helpers
# ---------------------------------------------------------------------------

_PG_VERSION = os.environ.get("GATEWAY_PG_VERSION", "15.0")

STARTUP_PARAMETERS = [
    ("server_version", _PG_VERSION),
    ("server_version_num", "150000"),
    ("server_encoding", "UTF8"),
    ("client_encoding", "UTF8"),
    ("DateStyle", "ISO, MDY"),
    ("integer_datetimes", "on"),
    ("standard_conforming_strings", "on"),
    ("TimeZone", "UTC"),
]


def startup_sequence(pid: int = 1, secret: int = 0) -> bytes:
    """
    Returns the full byte sequence sent immediately after receiving a valid
    StartupMessage: AuthenticationOK, ParameterStatus blocks, BackendKeyData,
    ReadyForQuery. *secret* is the per-connection cancel key (F-001-12).
    """
    buf = authentication_ok()
    for name, value in STARTUP_PARAMETERS:
        buf += parameter_status(name, value)
    buf += backend_key_data(pid, secret)
    buf += ready_for_query()
    return buf
