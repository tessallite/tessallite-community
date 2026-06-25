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
OID_TIMESTAMP = 1114
OID_TIMESTAMPTZ = 1184

# Bug-3655 (option b): OIDs the gateway emits as TEXT even when a client
# requests binary result format. The gateway has no true PG binary wire
# encoder for these types (NUMERIC is a base-10000 digit array; DATE /
# TIMESTAMP are epoch-offset integers), so it text-encodes the value in
# data_row. The advertised per-column format code in row_description MUST
# match that, otherwise the client mis-parses a text payload as binary.
# Forcing format code 0 for these columns keeps row_description and data_row
# in lockstep regardless of the requested format.
_TEXT_ONLY_BINARY_OIDS = frozenset({
    OID_NUMERIC, OID_DATE, OID_TIMESTAMP, OID_TIMESTAMPTZ,
})


def _effective_result_format(requested_fmt: int, oid: int) -> int:
    """Return the format code actually used for a column (Bug-3655).

    Downgrades a requested binary (1) format to text (0) for OIDs the gateway
    cannot binary-encode, so the advertised format and the emitted payload
    always agree.
    """
    if requested_fmt == 1 and oid in _TEXT_ONLY_BINARY_OIDS:
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


def _encode_binary_value(text: str, oid: int) -> bytes:
    """Encode a text string as PG binary wire format for the given OID."""
    if oid == OID_BOOL:
        return b"\x01" if text.lower() in ("true", "t", "1") else b"\x00"
    if oid == OID_INT8:
        try:
            return struct.pack("!q", int(text))
        except (ValueError, struct.error):
            return text.encode("utf-8")
    if oid in (23, 26):  # INT4, OID
        try:
            return struct.pack("!i", int(text))
        except (ValueError, struct.error):
            return text.encode("utf-8")
    if oid in (21,):  # INT2
        try:
            return struct.pack("!h", int(text))
        except (ValueError, struct.error):
            return text.encode("utf-8")
    if oid == OID_FLOAT8:
        try:
            return struct.pack("!d", float(text))
        except (ValueError, struct.error):
            return text.encode("utf-8")
    if oid in (700,):  # FLOAT4
        try:
            return struct.pack("!f", float(text))
        except (ValueError, struct.error):
            return text.encode("utf-8")
    # NUMERIC / DATE / TIMESTAMP / TIMESTAMPTZ and any other OID fall through to
    # text. data_row never reaches here for those OIDs (Bug-3655 downgrades them
    # to text before calling this), so the advertised format and payload agree;
    # this is the defensive fallback for any unexpected OID requesting binary.
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


def _decode_binary_param(data: bytes, oid: int = 0) -> str:
    """Decode a binary-format parameter value to its text representation.

    Bug-5187: uses the declared parameter OID from the Parse message to
    choose the correct decoder. Falls back to a byte-length heuristic only
    when no OID is declared (oid == 0), which preserves backwards
    compatibility for clients that omit type declarations.

    Without OID-driven decoding, a 4-byte UTF-8 text string (e.g. a date
    like "2024") would be mis-decoded as an int32, and an 8-byte text
    string would be mis-decoded as an int64, silently corrupting the value.
    """
    n = len(data)

    # --- OID-driven decoding (preferred path) ---
    if oid == OID_BOOL:
        return "true" if (n >= 1 and data[0]) else "false"
    if oid == OID_INT2 and n >= 2:
        return str(struct.unpack_from("!h", data, 0)[0])
    if oid in (OID_INT4, 26) and n >= 4:  # 26 = OID type
        return str(struct.unpack_from("!i", data, 0)[0])
    if oid == OID_INT8 and n >= 8:
        return str(struct.unpack_from("!q", data, 0)[0])
    if oid == OID_FLOAT4 and n >= 4:
        return str(struct.unpack_from("!f", data, 0)[0])
    if oid == OID_FLOAT8 and n >= 8:
        return str(struct.unpack_from("!d", data, 0)[0])
    if oid in (OID_TEXT, 1043, 1042, 18):
        # TEXT (25), VARCHAR (1043), CHAR (1042), "char" (18) — decode as UTF-8.
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.hex()
    if oid == OID_DATE:
        # PG binary date: int32 = days since 2000-01-01.
        if n == 4:
            from datetime import date, timedelta
            days = struct.unpack_from("!i", data, 0)[0]
            return str(date(2000, 1, 1) + timedelta(days=days))
        # Non-standard length — try UTF-8 text representation.
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.hex()
    if oid in (OID_TIMESTAMP, OID_TIMESTAMPTZ):
        # PG binary timestamp: int64 = microseconds since 2000-01-01 00:00:00.
        if n == 8:
            from datetime import datetime, timedelta, timezone
            microseconds = struct.unpack_from("!q", data, 0)[0]
            base = datetime(2000, 1, 1)
            dt = base + timedelta(microseconds=microseconds)
            if oid == OID_TIMESTAMPTZ:
                dt = dt.replace(tzinfo=timezone.utc)
            return str(dt)
        # Non-standard length — try UTF-8 text representation.
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.hex()
    if oid != 0:
        # Known OID but no specific decoder — treat as text.
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.hex()

    # --- Legacy byte-length heuristic (OID unknown / 0) ---
    if n == 1:
        return "true" if data[0] else "false"
    if n == 2:
        return str(struct.unpack_from("!h", data, 0)[0])
    if n == 4:
        return str(struct.unpack_from("!i", data, 0)[0])
    if n == 8:
        return str(struct.unpack_from("!q", data, 0)[0])
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.hex()


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
