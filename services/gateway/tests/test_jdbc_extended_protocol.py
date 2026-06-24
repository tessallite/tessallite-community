"""Unit tests for JDBC extended query protocol — parameter binding, substitution, statement caching."""
from __future__ import annotations

import struct
import pytest

from src.jdbc import protocol as proto
from src.jdbc.server import _substitute_params


# ---------------------------------------------------------------------------
# Helpers — build Bind payloads
# ---------------------------------------------------------------------------


def _build_bind_payload(
    portal: str = "",
    stmt: str = "",
    format_codes: list[int] | None = None,
    params: list[bytes | None] | None = None,
    result_formats: list[int] | None = None,
) -> bytes:
    """Construct a Bind message payload for testing."""
    buf = portal.encode() + b"\x00"
    buf += stmt.encode() + b"\x00"

    fcs = format_codes or []
    buf += struct.pack("!H", len(fcs))
    for fc in fcs:
        buf += struct.pack("!H", fc)

    ps = params or []
    buf += struct.pack("!H", len(ps))
    for p in ps:
        if p is None:
            buf += struct.pack("!i", -1)
        else:
            buf += struct.pack("!I", len(p)) + p

    rfs = result_formats or []
    buf += struct.pack("!H", len(rfs))
    for rf in rfs:
        buf += struct.pack("!H", rf)

    return buf


# ===================================================================
# parse_bind_parameters
# ===================================================================


class TestParseBindParameters:
    def test_no_params(self):
        payload = _build_bind_payload()
        portal, stmt, params, rfmt = proto.parse_bind_parameters(payload)
        assert portal == ""
        assert stmt == ""
        assert params == []
        assert rfmt == []

    def test_single_text_param(self):
        payload = _build_bind_payload(params=[b"hello"])
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == ["hello"]

    def test_multiple_text_params(self):
        payload = _build_bind_payload(params=[b"42", b"world", b"3.14"])
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == ["42", "world", "3.14"]

    def test_null_param(self):
        payload = _build_bind_payload(params=[b"a", None, b"c"])
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == ["a", None, "c"]

    def test_portal_and_statement_names(self):
        payload = _build_bind_payload(portal="p1", stmt="s1", params=[b"x"])
        portal, stmt, params, _ = proto.parse_bind_parameters(payload)
        assert portal == "p1"
        assert stmt == "s1"
        assert params == ["x"]

    def test_binary_int4(self):
        raw = struct.pack("!i", 99)
        payload = _build_bind_payload(format_codes=[1], params=[raw])
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == ["99"]

    def test_binary_bool(self):
        payload = _build_bind_payload(format_codes=[1], params=[b"\x01"])
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == ["true"]

        payload = _build_bind_payload(format_codes=[1], params=[b"\x00"])
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == ["false"]

    def test_mixed_format_codes(self):
        int_val = struct.pack("!i", 7)
        payload = _build_bind_payload(
            format_codes=[0, 1],
            params=[b"text_val", int_val],
        )
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == ["text_val", "7"]

    def test_single_format_code_applies_to_all(self):
        payload = _build_bind_payload(
            format_codes=[0],
            params=[b"a", b"b"],
        )
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == ["a", "b"]

    def test_empty_string_param(self):
        payload = _build_bind_payload(params=[b""])
        _, _, params, _ = proto.parse_bind_parameters(payload)
        assert params == [""]

    def test_result_format_codes_binary(self):
        payload = _build_bind_payload(result_formats=[1])
        _, _, _, rfmt = proto.parse_bind_parameters(payload)
        assert rfmt == [1]

    def test_result_format_codes_mixed(self):
        payload = _build_bind_payload(result_formats=[0, 1, 1])
        _, _, _, rfmt = proto.parse_bind_parameters(payload)
        assert rfmt == [0, 1, 1]


# ===================================================================
# parameter_description
# ===================================================================


class TestParameterDescription:
    def test_empty(self):
        msg = proto.parameter_description()
        assert msg[0:1] == b"t"
        payload = msg[5:]
        count = struct.unpack("!H", payload[:2])[0]
        assert count == 0

    def test_with_oids(self):
        msg = proto.parameter_description([23, 25])
        payload = msg[5:]
        count = struct.unpack("!H", payload[:2])[0]
        assert count == 2
        oid1 = struct.unpack("!I", payload[2:6])[0]
        oid2 = struct.unpack("!I", payload[6:10])[0]
        assert oid1 == 23
        assert oid2 == 25


# ===================================================================
# Binary data_row encoding
# ===================================================================


class TestBinaryDataRow:
    def test_text_format_default(self):
        msg = proto.data_row(["hello"])
        assert msg[0:1] == b"D"
        payload = msg[5:]
        count = struct.unpack("!H", payload[:2])[0]
        assert count == 1
        vlen = struct.unpack("!I", payload[2:6])[0]
        assert payload[6:6 + vlen] == b"hello"

    def test_binary_int4(self):
        msg = proto.data_row(["42"], result_formats=[1], col_oids=[23])
        payload = msg[5:]
        count = struct.unpack("!H", payload[:2])[0]
        assert count == 1
        vlen = struct.unpack("!I", payload[2:6])[0]
        assert vlen == 4
        val = struct.unpack("!i", payload[6:10])[0]
        assert val == 42

    def test_binary_int8(self):
        msg = proto.data_row(["999"], result_formats=[1], col_oids=[proto.OID_INT8])
        payload = msg[5:]
        vlen = struct.unpack("!I", payload[2:6])[0]
        assert vlen == 8
        val = struct.unpack("!q", payload[6:14])[0]
        assert val == 999

    def test_binary_float8(self):
        msg = proto.data_row(["3.14"], result_formats=[1], col_oids=[proto.OID_FLOAT8])
        payload = msg[5:]
        vlen = struct.unpack("!I", payload[2:6])[0]
        assert vlen == 8
        val = struct.unpack("!d", payload[6:14])[0]
        assert abs(val - 3.14) < 1e-10

    def test_binary_text_fallback(self):
        msg = proto.data_row(["hello"], result_formats=[1], col_oids=[proto.OID_TEXT])
        payload = msg[5:]
        vlen = struct.unpack("!I", payload[2:6])[0]
        assert payload[6:6 + vlen] == b"hello"

    def test_null_value(self):
        msg = proto.data_row([None], result_formats=[1], col_oids=[23])
        payload = msg[5:]
        vlen = struct.unpack("!i", payload[2:6])[0]
        assert vlen == -1


def _row_description_format_codes(msg: bytes) -> list[int]:
    """Extract the per-column format code from a RowDescription ('T') message."""
    assert msg[0:1] == b"T"
    payload = msg[5:]
    n = struct.unpack("!H", payload[:2])[0]
    off = 2
    fmts: list[int] = []
    for _ in range(n):
        end = payload.index(b"\x00", off)
        off = end + 1            # name
        off += 4                 # table OID
        off += 2                 # column attr number
        off += 4                 # type OID
        off += 2                 # type size
        off += 4                 # type modifier
        fmts.append(struct.unpack("!H", payload[off:off + 2])[0])
        off += 2
    return fmts


class TestBug3655NumericDateTimestampLockstep:
    """Bug-3655 (option b): NUMERIC/DATE/TIMESTAMP columns advertise text
    format (0) even when binary is requested, and data_row emits text, so the
    advertised format and the emitted payload stay in lockstep."""

    def test_row_description_downgrades_numeric_to_text(self):
        cols = [("revenue", proto.OID_NUMERIC), ("qty", proto.OID_INT4)]
        msg = proto.row_description(cols, result_formats=[1])
        fmts = _row_description_format_codes(msg)
        assert fmts[0] == 0       # NUMERIC forced to text
        assert fmts[1] == 1       # INT4 stays binary

    def test_row_description_downgrades_date_and_timestamp(self):
        cols = [
            ("d", proto.OID_DATE),
            ("ts", proto.OID_TIMESTAMP),
            ("tstz", proto.OID_TIMESTAMPTZ),
        ]
        msg = proto.row_description(cols, result_formats=[1])
        assert _row_description_format_codes(msg) == [0, 0, 0]

    def test_data_row_emits_text_for_numeric_when_binary_requested(self):
        msg = proto.data_row(
            ["123.45"], result_formats=[1], col_oids=[proto.OID_NUMERIC],
        )
        payload = msg[5:]
        vlen = struct.unpack("!I", payload[2:6])[0]
        assert payload[6:6 + vlen] == b"123.45"   # text, not binary

    def test_data_row_emits_text_for_date_when_binary_requested(self):
        msg = proto.data_row(
            ["2025-01-15"], result_formats=[1], col_oids=[proto.OID_DATE],
        )
        payload = msg[5:]
        vlen = struct.unpack("!I", payload[2:6])[0]
        assert payload[6:6 + vlen] == b"2025-01-15"

    def test_row_description_and_data_row_agree_for_mixed_columns(self):
        cols = [
            ("revenue", proto.OID_NUMERIC),
            ("qty", proto.OID_INT4),
            ("ts", proto.OID_TIMESTAMP),
        ]
        rd = proto.row_description(cols, result_formats=[1])
        fmts = _row_description_format_codes(rd)
        # INT4 binary (4 bytes), NUMERIC + TIMESTAMP text.
        dr = proto.data_row(
            ["100.5", "7", "2025-01-15 00:00:00"],
            result_formats=[1],
            col_oids=[proto.OID_NUMERIC, proto.OID_INT4, proto.OID_TIMESTAMP],
        )
        payload = dr[5:]
        off = 2
        for i, expected_fmt in enumerate(fmts):
            vlen = struct.unpack("!I", payload[off:off + 4])[0]
            off += 4
            val = payload[off:off + vlen]
            off += vlen
            if i == 1:
                assert expected_fmt == 1 and vlen == 4   # INT4 binary
            else:
                assert expected_fmt == 0                 # text columns
                assert b"." in val or b"-" in val


# ===================================================================
# _substitute_params
# ===================================================================


class TestSubstituteParams:
    def test_no_params(self):
        assert _substitute_params("SELECT 1", []) == "SELECT 1"

    def test_no_placeholders(self):
        assert _substitute_params("SELECT * FROM t", ["unused"]) == "SELECT * FROM t"

    def test_single_string_param(self):
        result = _substitute_params("SELECT * FROM t WHERE name = $1", ["Alice"])
        assert result == "SELECT * FROM t WHERE name = 'Alice'"

    def test_single_int_param(self):
        result = _substitute_params("SELECT * FROM t WHERE id = $1", ["42"])
        assert result == "SELECT * FROM t WHERE id = 42"

    def test_single_float_param(self):
        result = _substitute_params("SELECT * FROM t WHERE price > $1", ["9.99"])
        assert result == "SELECT * FROM t WHERE price > 9.99"

    def test_null_param(self):
        result = _substitute_params("SELECT * FROM t WHERE x = $1", [None])
        assert result == "SELECT * FROM t WHERE x = NULL"

    def test_multiple_params(self):
        result = _substitute_params(
            "SELECT * FROM t WHERE a = $1 AND b = $2 AND c = $3",
            ["hello", "42", None],
        )
        assert result == "SELECT * FROM t WHERE a = 'hello' AND b = 42 AND c = NULL"

    def test_string_with_quotes(self):
        result = _substitute_params("SELECT $1", ["it's a test"])
        assert result == "SELECT 'it''s a test'"

    def test_sql_injection_attempt(self):
        result = _substitute_params("SELECT $1", ["'); DROP TABLE users; --"])
        assert result == "SELECT '''); DROP TABLE users; --'"

    def test_param_index_out_of_range(self):
        result = _substitute_params("SELECT $1, $5", ["only_one"])
        assert result == "SELECT 'only_one', $5"

    def test_negative_number(self):
        result = _substitute_params("SELECT $1", ["-7"])
        assert result == "SELECT -7"

    def test_scientific_notation_quoted_without_numeric_oid(self):
        # F-001-06: scientific notation is not a strict SQL numeric literal and
        # no numeric OID was declared, so it is quoted as text (the report's
        # recommended `^-?\d+(\.\d+)?$` validation rejects it).
        result = _substitute_params("SELECT $1", ["1.5e3"])
        assert result == "SELECT '1.5e3'"

    def test_nan_treated_as_string(self):
        result = _substitute_params("SELECT $1", ["NaN"])
        assert result == "SELECT 'NaN'"

    def test_infinity_treated_as_string(self):
        result = _substitute_params("SELECT $1", ["Infinity"])
        assert result == "SELECT 'Infinity'"

    # --- F-001-06: declared parameter OID governs quoting ---

    def test_leading_zero_text_param_quoted_without_oid(self):
        # Zip code "00123": without a numeric OID it must stay text so the
        # leading zeros survive (strict regex rejects "00123").
        result = _substitute_params("SELECT $1", ["00123"])
        assert result == "SELECT '00123'"

    def test_underscore_number_quoted(self):
        # "1_000" is a PG15 syntax error if inlined raw; quote it instead.
        result = _substitute_params("SELECT $1", ["1_000"])
        assert result == "SELECT '1_000'"

    def test_text_oid_forces_quoting_of_numeric_looking_value(self):
        from src.jdbc import protocol as proto
        result = _substitute_params("SELECT $1", ["00123"], [proto.OID_TEXT])
        assert result == "SELECT '00123'"

    def test_numeric_oid_inlines_valid_number(self):
        from src.jdbc import protocol as proto
        result = _substitute_params("SELECT $1", ["42"], [proto.OID_INT4])
        assert result == "SELECT 42"

    def test_numeric_oid_quotes_invalid_number(self):
        # Numeric OID declared but the value is not a strict number → quote so
        # malformed raw SQL never reaches the source.
        from src.jdbc import protocol as proto
        result = _substitute_params("SELECT $1", ["1_000"], [proto.OID_INT8])
        assert result == "SELECT '1_000'"

    def test_param_10_does_not_conflict_with_param_1(self):
        params = [f"p{i}" for i in range(10)]
        result = _substitute_params("SELECT $1, $10", params)
        assert result == "SELECT 'p0', 'p9'"

    def test_repeated_param(self):
        result = _substitute_params("SELECT $1, $1", ["val"])
        assert result == "SELECT 'val', 'val'"

    def test_date_string(self):
        result = _substitute_params("SELECT $1", ["2024-01-15"])
        assert result == "SELECT '2024-01-15'"

    def test_uuid_string(self):
        result = _substitute_params("SELECT $1", ["a1b2c3d4-e5f6-7890-abcd-ef1234567890"])
        assert result == "SELECT 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'"


# ===================================================================
# Statement caching (via PGWireServer._statements)
# ===================================================================


class TestStatementCache:
    def _make_server(self):
        from src.jdbc.server import PGWireServer
        return PGWireServer()

    def test_named_statement_persists(self):
        srv = self._make_server()
        srv._statements["my_stmt"] = "SELECT $1 FROM t"
        assert "my_stmt" in srv._statements
        assert srv._statements["my_stmt"] == "SELECT $1 FROM t"

    def test_unnamed_replaces_previous(self):
        srv = self._make_server()
        srv._statements[""] = "SELECT 1"
        srv._statements[""] = "SELECT 2"
        assert srv._statements[""] == "SELECT 2"

    def test_close_removes_named_statement(self):
        srv = self._make_server()
        srv._statements["s1"] = "SELECT 1"
        srv._statements["s2"] = "SELECT 2"
        payload = b"S" + b"s1\x00"
        close_type = chr(payload[0])
        close_name = payload[1:].rstrip(b"\x00").decode("utf-8", errors="replace")
        if close_type == "S" and close_name in srv._statements:
            del srv._statements[close_name]
        assert "s1" not in srv._statements
        assert "s2" in srv._statements

    def test_close_portal_does_not_remove_statement(self):
        srv = self._make_server()
        srv._statements["s1"] = "SELECT 1"
        payload = b"P" + b"s1\x00"
        close_type = chr(payload[0])
        close_name = payload[1:].rstrip(b"\x00").decode("utf-8", errors="replace")
        if close_type == "S" and close_name in srv._statements:
            del srv._statements[close_name]
        assert "s1" in srv._statements


# ===================================================================
# _try_constant_select
# ===================================================================


class TestTryConstantSelect:
    def _make_server(self):
        from src.jdbc.server import PGWireServer
        return PGWireServer()

    def test_select_1(self):
        result = self._make_server()._try_constant_select("SELECT 1")
        assert result is not None
        cols, rows = result
        assert cols == [("?column?", 23)]
        assert rows == [["1"]]

    def test_select_1_semicolon(self):
        result = self._make_server()._try_constant_select("SELECT 1;")
        assert result is not None
        _, rows = result
        assert rows == [["1"]]

    def test_select_negative_int(self):
        result = self._make_server()._try_constant_select("SELECT -42")
        assert result is not None
        cols, rows = result
        assert cols == [("?column?", 23)]
        assert rows == [["-42"]]

    def test_select_string(self):
        result = self._make_server()._try_constant_select("SELECT 'hello'")
        assert result is not None
        cols, rows = result
        assert cols == [("?column?", 25)]
        assert rows == [["hello"]]

    def test_select_with_from_returns_none(self):
        result = self._make_server()._try_constant_select("SELECT id FROM users")
        assert result is None

    def test_non_select_returns_none(self):
        result = self._make_server()._try_constant_select("INSERT INTO t VALUES (1)")
        assert result is None

    def test_empty_string_returns_none(self):
        result = self._make_server()._try_constant_select("")
        assert result is None

    def test_select_version_style_is_not_echoed(self):
        # F-001-05: a function call is NOT a literal projection. The local
        # constant handler returns None (the expression is delegated to the
        # catalogue SQLite engine) instead of echoing "version()" as data.
        result = self._make_server()._try_constant_select("SELECT version()")
        assert result is None

    def test_arithmetic_is_not_echoed(self):
        # F-001-05: SELECT 1+1 must not echo "1+1" as text.
        assert self._make_server()._try_constant_select("SELECT 1+1") is None

    def test_select_alias_is_honoured(self):
        # F-001-05: SELECT 1 AS one → column "one", value 1.
        result = self._make_server()._try_constant_select("SELECT 1 AS one")
        assert result is not None
        cols, rows = result
        assert cols == [("one", 23)]
        assert rows == [["1"]]

    def test_select_multiple_literals(self):
        # F-001-05: SELECT 1, 2 → two columns, not one "1, 2" text column.
        result = self._make_server()._try_constant_select("SELECT 1, 2")
        assert result is not None
        cols, rows = result
        assert len(cols) == 2
        assert rows == [["1", "2"]]


# ===================================================================
# _count_param_placeholders
# ===================================================================


class TestCountParamPlaceholders:
    def test_no_params(self):
        from src.jdbc.server import _count_param_placeholders
        assert _count_param_placeholders("SELECT 1") == 0

    def test_single_param(self):
        from src.jdbc.server import _count_param_placeholders
        assert _count_param_placeholders("SELECT * FROM t WHERE x = $1") == 1

    def test_two_params(self):
        from src.jdbc.server import _count_param_placeholders
        assert _count_param_placeholders(
            "SELECT * FROM t WHERE a = $1 AND b = $2"
        ) == 2

    def test_gap_in_numbering_uses_max(self):
        from src.jdbc.server import _count_param_placeholders
        assert _count_param_placeholders("SELECT $1, $5") == 5

    def test_repeated_param(self):
        from src.jdbc.server import _count_param_placeholders
        assert _count_param_placeholders("SELECT $1, $1") == 1

    def test_ten_params(self):
        from src.jdbc.server import _count_param_placeholders
        assert _count_param_placeholders("SELECT $1, $10") == 10

    def test_information_schema_discover(self):
        from src.jdbc.server import _count_param_placeholders
        sql = (
            "SELECT table_schema, table_name, table_type "
            "FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND table_schema = $1 "
            "ORDER BY table_schema, table_name LIMIT 500"
        )
        assert _count_param_placeholders(sql) == 1


# ===================================================================
# sqlglot-based helpers
# ===================================================================


class TestExtractTablesViaSqlglot:
    """F-001-15: tests retargeted from the deleted ``_extract_first_table_*``
    helpers onto the production list extractor. F-001-04: unquoted names are
    case-folded to lowercase; quoted names keep their case.
    """

    @staticmethod
    def _extract(sql: str):
        from src.jdbc.server import PGWireServer
        return PGWireServer._extract_tables_via_sqlglot(sql)

    def test_simple_select(self):
        assert self._extract("SELECT * FROM users") == ["users"]

    def test_unquoted_uppercase_is_folded(self):
        # F-001-04: PostgreSQL folds unquoted identifiers to lowercase.
        assert self._extract("SELECT * FROM MODELX") == ["modelx"]

    def test_cte_query(self):
        sql = "WITH cte AS (SELECT id FROM orders) SELECT * FROM cte"
        assert "orders" in self._extract(sql)

    def test_subquery(self):
        sql = "SELECT * FROM (SELECT id FROM products) sub"
        assert "products" in self._extract(sql)

    def test_lateral_join(self):
        sql = "SELECT * FROM users u, LATERAL (SELECT * FROM orders WHERE orders.uid = u.id) o"
        assert self._extract(sql)

    def test_select_from_string_literal(self):
        """SELECT 'FROM' AS col has no real tables."""
        assert self._extract("SELECT 'FROM' AS col") == []

    def test_malformed_sql_returns_empty(self):
        assert self._extract("THIS IS NOT SQL AT ALL !!!") == []

    def test_quoted_schema_qualified_preserves_case(self):
        # Quoted identifiers stay case-sensitive (PG semantics).
        assert self._extract('SELECT * FROM public."MyTable"') == ["MyTable"]

    def test_no_tables(self):
        assert self._extract("SELECT 1") == []


class TestExtractTablesViaRegex:
    @staticmethod
    def _extract(sql: str):
        from src.jdbc.server import PGWireServer
        return PGWireServer._extract_tables_via_regex(sql)

    def test_simple_select(self):
        assert "users" in self._extract("SELECT * FROM users")

    def test_quoted_table(self):
        assert "MyModel" in self._extract('SELECT * FROM "MyModel"')

    def test_schema_qualified(self):
        assert "MyModel" in self._extract('SELECT * FROM public."MyModel"')

    def test_no_from_returns_empty(self):
        assert self._extract("SELECT 1") == []


class TestIsConstantSelectSqlglot:
    def test_select_1(self):
        from src.jdbc.server import _is_constant_select_sqlglot
        assert _is_constant_select_sqlglot("SELECT 1") is True

    def test_select_with_table(self):
        from src.jdbc.server import _is_constant_select_sqlglot
        assert _is_constant_select_sqlglot("SELECT * FROM users") is False

    def test_select_from_string_literal(self):
        """SELECT 'FROM' should be detected as constant (no real tables)."""
        from src.jdbc.server import _is_constant_select_sqlglot
        assert _is_constant_select_sqlglot("SELECT 'FROM' AS col") is True

    def test_malformed_sql(self):
        from src.jdbc.server import _is_constant_select_sqlglot
        assert _is_constant_select_sqlglot("NOT VALID SQL") is False


class TestTryConstantSelectFromLiteral:
    """Regression: SELECT 'FROM' AS col should be treated as constant."""
    def _make_server(self):
        from src.jdbc.server import PGWireServer
        return PGWireServer()

    def test_select_from_string_literal_is_constant(self):
        result = self._make_server()._try_constant_select("SELECT 'FROM' AS col")
        assert result is not None
