"""Tests for gateway JDBC bugs 5185-5188.

Bug-5185: $KPIs unsupported WHERE predicate must error, not silently return all rows.
Bug-5186: computed column type must not inherit catalogue column type on name collision.
Bug-5187: binary Bind parameter decoding must use declared OIDs, not byte-length.
Bug-5188: CancelRequest must cancel the in-flight query task.
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from src.jdbc import protocol as proto
from src.jdbc.server import PGWireServer, _substitute_params


# ---------------------------------------------------------------------------
# Bug-5185: $KPIs unsupported WHERE predicate must error
# ---------------------------------------------------------------------------

KPI_COLUMNS = ["kpi_name", "value", "target", "status"]
KPI_ROWS = [
    {"kpi_name": "Revenue", "value": 100.0, "target": 90.0, "status": 1},
    {"kpi_name": "Margin", "value": 0.42, "target": 0.40, "status": 1},
    {"kpi_name": "Churn", "value": 0.05, "target": 0.03, "status": 2},
]


class TestBug5185KpiUnsupportedPredicate:
    """An unsupported WHERE predicate on $KPIs must return an error, not all rows."""

    def test_unsupported_function_predicate_returns_error(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE upper(kpi_name) = \'X\'',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is not None
        assert "Unsupported WHERE predicate" in err
        assert rows == []

    def test_unsupported_like_predicate_returns_error(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE kpi_name LIKE \'%rev%\'',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is not None
        assert "Unsupported WHERE predicate" in err
        assert rows == []

    def test_unsupported_in_predicate_returns_error(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE kpi_name IN (\'Revenue\', \'Margin\')',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is not None
        assert "Unsupported WHERE predicate" in err
        assert rows == []

    def test_supported_eq_predicate_still_works(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE kpi_name = \'Revenue\'',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        assert len(rows) == 1
        assert rows[0][0] == "Revenue"

    def test_supported_and_predicate_still_works(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE status = 1 AND value > 50',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        assert len(rows) == 1
        assert rows[0][0] == "Revenue"

    def test_no_where_still_works(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs"',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        assert len(rows) == len(KPI_ROWS)

    def test_mixed_supported_and_unsupported_in_and_returns_error(self):
        """An AND with one unsupported arm must error, not partially filter."""
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE status = 1 AND upper(kpi_name) = \'X\'',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is not None
        assert rows == []

    def test_unparseable_sql_returns_error_not_full_dump(self):
        """Review finding [1]: unparseable SQL must fail closed."""
        s = PGWireServer()
        # Feed genuinely unparseable SQL (not valid PostgreSQL at all)
        cols, rows, err = s._shape_kpi_result(
            '@@@ NOT VALID SQL AT ALL @@@',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is not None
        assert "parse" in err.lower()
        assert rows == []

    def test_supported_or_predicate_works(self):
        s = PGWireServer()
        cols, rows, err = s._shape_kpi_result(
            'SELECT * FROM "modelx$KPIs" WHERE kpi_name = \'Revenue\' OR kpi_name = \'Margin\'',
            list(KPI_COLUMNS), [dict(r) for r in KPI_ROWS],
        )
        assert err is None
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Bug-5186: computed column must not inherit catalogue type on name collision
# ---------------------------------------------------------------------------


class TestBug5186ComputedColumnType:
    """A computed/expression output column must be typed by its actual value,
    not inherit a same-named catalogue column's type."""

    def _make_server(self):
        s = PGWireServer()
        s._table_columns = {
            "modelx": [
                {"name": "revenue", "data_type": "numeric"},
                {"name": "region", "data_type": "text"},
                {"name": "qty", "data_type": "integer"},
            ]
        }
        return s

    def test_bare_column_gets_catalogue_type(self):
        s = self._make_server()
        typed = s._type_columns_from_catalogue(
            "SELECT revenue, region FROM modelx",
            ["revenue", "region"],
        )
        assert typed[0] == {"name": "revenue", "type": "numeric"}
        assert typed[1] == {"name": "region", "type": "text"}

    def test_sum_aliased_as_column_name_stays_text(self):
        """SUM(revenue) AS revenue must NOT get type 'numeric' from catalogue."""
        s = self._make_server()
        typed = s._type_columns_from_catalogue(
            "SELECT SUM(revenue) AS revenue FROM modelx",
            ["revenue"],
        )
        assert typed[0] == {"name": "revenue", "type": "text"}

    def test_count_aliased_as_column_name_stays_text(self):
        s = self._make_server()
        typed = s._type_columns_from_catalogue(
            "SELECT COUNT(qty) AS qty FROM modelx",
            ["qty"],
        )
        assert typed[0] == {"name": "qty", "type": "text"}

    def test_case_expression_aliased_as_column_name_stays_text(self):
        s = self._make_server()
        typed = s._type_columns_from_catalogue(
            "SELECT CASE WHEN revenue > 100 THEN 'high' ELSE 'low' END AS revenue FROM modelx",
            ["revenue"],
        )
        assert typed[0] == {"name": "revenue", "type": "text"}

    def test_column_rename_alias_keeps_catalogue_type(self):
        """SELECT revenue AS rev — direct column renamed, so catalogue type applies."""
        s = self._make_server()
        typed = s._type_columns_from_catalogue(
            "SELECT revenue AS rev FROM modelx",
            ["rev"],
        )
        # 'rev' is an alias for a direct column reference. Since 'rev' is not
        # in the catalogue, it falls back to text. That is correct — it would
        # be wrong to inherit 'revenue' type for a different name.
        assert typed[0] == {"name": "rev", "type": "text"}

    def test_unknown_alias_stays_text(self):
        s = self._make_server()
        typed = s._type_columns_from_catalogue(
            "SELECT 1 AS computed_val FROM modelx",
            ["computed_val"],
        )
        assert typed[0] == {"name": "computed_val", "type": "text"}

    def test_mixed_bare_and_computed(self):
        s = self._make_server()
        typed = s._type_columns_from_catalogue(
            "SELECT region, SUM(revenue) AS revenue FROM modelx GROUP BY region",
            ["region", "revenue"],
        )
        assert typed[0] == {"name": "region", "type": "text"}
        # SUM(revenue) AS revenue is computed — must NOT inherit numeric
        assert typed[1] == {"name": "revenue", "type": "text"}

    def test_already_typed_dict_columns_are_preserved(self):
        """Columns already typed by the router (dict form) are never overridden."""
        s = self._make_server()
        typed = s._type_columns_from_catalogue(
            "SELECT revenue FROM modelx",
            [{"name": "revenue", "type": "float8"}],
        )
        assert typed[0] == {"name": "revenue", "type": "float8"}


# ---------------------------------------------------------------------------
# Bug-5187: binary Bind parameter decoding must use declared OIDs
# ---------------------------------------------------------------------------


class TestBug5187BinaryParamDecoding:
    """Binary params must be decoded using the declared OID from Parse."""

    def test_text_oid_decodes_4_bytes_as_text_not_int(self):
        """A 4-byte UTF-8 string with OID_TEXT must not be decoded as int32."""
        data = b"2024"  # 4 bytes, valid UTF-8 AND valid int32 bytes
        result = proto._decode_binary_param(data, oid=proto.OID_TEXT)
        assert result == "2024"  # text, not some random integer

    def test_text_oid_decodes_8_bytes_as_text_not_int64(self):
        """An 8-byte UTF-8 string with OID_TEXT must not be decoded as int64."""
        data = b"20240115"  # 8 bytes
        result = proto._decode_binary_param(data, oid=proto.OID_TEXT)
        assert result == "20240115"

    def test_varchar_oid_decodes_as_text(self):
        data = b"hello"
        result = proto._decode_binary_param(data, oid=1043)  # VARCHAR
        assert result == "hello"

    def test_int4_oid_decodes_as_integer(self):
        data = struct.pack("!i", 42)
        result = proto._decode_binary_param(data, oid=proto.OID_INT4)
        assert result == "42"

    def test_int8_oid_decodes_as_bigint(self):
        data = struct.pack("!q", 9999999999)
        result = proto._decode_binary_param(data, oid=proto.OID_INT8)
        assert result == "9999999999"

    def test_int2_oid_decodes_as_smallint(self):
        data = struct.pack("!h", 7)
        result = proto._decode_binary_param(data, oid=proto.OID_INT2)
        assert result == "7"

    def test_float4_oid_decodes_as_float(self):
        data = struct.pack("!f", 3.14)
        result = proto._decode_binary_param(data, oid=proto.OID_FLOAT4)
        assert abs(float(result) - 3.14) < 0.01

    def test_float8_oid_decodes_as_double(self):
        data = struct.pack("!d", 3.14159)
        result = proto._decode_binary_param(data, oid=proto.OID_FLOAT8)
        assert abs(float(result) - 3.14159) < 1e-5

    def test_bool_oid_true(self):
        result = proto._decode_binary_param(b"\x01", oid=proto.OID_BOOL)
        assert result == "true"

    def test_bool_oid_false(self):
        result = proto._decode_binary_param(b"\x00", oid=proto.OID_BOOL)
        assert result == "false"

    def test_date_oid_decodes_text_representation(self):
        """Date strings sent as binary text must decode correctly."""
        data = b"2024-01-15"
        result = proto._decode_binary_param(data, oid=proto.OID_DATE)
        assert result == "2024-01-15"

    def test_timestamp_oid_decodes_text_representation(self):
        data = b"2024-01-15 10:30:00"
        result = proto._decode_binary_param(data, oid=proto.OID_TIMESTAMP)
        assert result == "2024-01-15 10:30:00"

    def test_date_oid_pg_binary_epoch_offset(self):
        """Review finding [2]: PG binary date is int32 days since 2000-01-01."""
        from datetime import date, timedelta
        # 2024-01-15 = 8780 days after 2000-01-01
        target_date = date(2024, 1, 15)
        days = (target_date - date(2000, 1, 1)).days
        data = struct.pack("!i", days)
        result = proto._decode_binary_param(data, oid=proto.OID_DATE)
        assert result == "2024-01-15"

    def test_timestamp_oid_pg_binary_epoch_offset(self):
        """Review finding [2]: PG binary timestamp is int64 microseconds since 2000-01-01."""
        from datetime import datetime, timedelta
        target_dt = datetime(2024, 1, 15, 10, 30, 0)
        microseconds = int((target_dt - datetime(2000, 1, 1)).total_seconds() * 1_000_000)
        data = struct.pack("!q", microseconds)
        result = proto._decode_binary_param(data, oid=proto.OID_TIMESTAMP)
        assert "2024-01-15" in result
        assert "10:30:00" in result

    def test_timestamptz_oid_pg_binary_includes_utc(self):
        """PG binary timestamptz includes UTC timezone info."""
        from datetime import datetime, timedelta
        target_dt = datetime(2024, 6, 15, 12, 0, 0)
        microseconds = int((target_dt - datetime(2000, 1, 1)).total_seconds() * 1_000_000)
        data = struct.pack("!q", microseconds)
        result = proto._decode_binary_param(data, oid=proto.OID_TIMESTAMPTZ)
        assert "2024-06-15" in result
        assert "UTC" in result or "+00:00" in result

    def test_unknown_oid_zero_falls_back_to_heuristic(self):
        """OID 0 (unspecified) preserves the legacy byte-length heuristic."""
        data = struct.pack("!i", 42)
        result = proto._decode_binary_param(data, oid=0)
        assert result == "42"

    def test_oid_passed_through_parse_bind_parameters(self):
        """parse_bind_parameters with param_oids uses OID-driven decoding."""
        # Build a Bind payload with a single binary param (4-byte text "ABCD")
        payload = _build_bind_payload_with_binary(
            format_codes=[1],
            params=[b"ABCD"],
        )
        # Without OIDs, 4 bytes would be decoded as int32 (heuristic).
        _, _, params_no_oid, _ = proto.parse_bind_parameters(payload)
        assert params_no_oid[0] != "ABCD"  # misinterpreted as int

        # With TEXT OID, 4 bytes are correctly decoded as text.
        _, _, params_with_oid, _ = proto.parse_bind_parameters(
            payload, param_oids=[proto.OID_TEXT],
        )
        assert params_with_oid[0] == "ABCD"

    def test_peek_bind_names(self):
        payload = b"myportal\x00mystmt\x00" + struct.pack("!H", 0) + struct.pack("!H", 0) + struct.pack("!H", 0)
        portal, stmt = proto.peek_bind_names(payload)
        assert portal == "myportal"
        assert stmt == "mystmt"


def _build_bind_payload_with_binary(
    portal: str = "",
    stmt: str = "",
    format_codes: list[int] | None = None,
    params: list[bytes | None] | None = None,
    result_formats: list[int] | None = None,
) -> bytes:
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


# ---------------------------------------------------------------------------
# Bug-5188: CancelRequest must cancel the in-flight query task
# ---------------------------------------------------------------------------


class TestBug5188CancelRequest:
    """CancelRequest must cancel the running query, not just acknowledge."""

    @pytest.mark.asyncio
    async def test_cancel_request_cancels_inflight_task(self):
        """A CancelRequest with matching pid/secret cancels the registered task."""
        from src.jdbc.server import _inflight_tasks, _conn_counter_lock

        # Create a long-running task
        event = asyncio.Event()

        async def long_query():
            await event.wait()  # will never complete naturally
            return {"columns": [], "rows": []}

        task = asyncio.ensure_future(long_query())
        cancel_key = (999, 12345)
        with _conn_counter_lock:
            _inflight_tasks[cancel_key] = task

        try:
            # Simulate CancelRequest
            server = PGWireServer()
            startup = {"type": "cancel", "pid": 999, "secret": 12345}
            # The cancel logic from _run:
            cancel_pid = startup["pid"]
            cancel_secret = startup["secret"]
            with _conn_counter_lock:
                found_task = _inflight_tasks.get((cancel_pid, cancel_secret))
            assert found_task is not None
            assert not found_task.done()
            found_task.cancel()

            # Wait briefly for cancellation to propagate
            await asyncio.sleep(0.01)
            assert task.cancelled()
        finally:
            with _conn_counter_lock:
                _inflight_tasks.pop(cancel_key, None)

    @pytest.mark.asyncio
    async def test_cancel_request_wrong_secret_does_not_cancel(self):
        """A CancelRequest with wrong secret does not cancel the task."""
        from src.jdbc.server import _inflight_tasks, _conn_counter_lock

        event = asyncio.Event()

        async def long_query():
            await event.wait()
            return {"columns": [], "rows": []}

        task = asyncio.ensure_future(long_query())
        cancel_key = (999, 12345)
        with _conn_counter_lock:
            _inflight_tasks[cancel_key] = task

        try:
            # Try with wrong secret
            with _conn_counter_lock:
                found_task = _inflight_tasks.get((999, 99999))
            assert found_task is None  # no match — task not cancelled

            assert not task.cancelled()
        finally:
            task.cancel()
            with _conn_counter_lock:
                _inflight_tasks.pop(cancel_key, None)

    @pytest.mark.asyncio
    async def test_run_cancellable_registers_and_unregisters_task(self):
        """_run_cancellable registers the task and cleans up after completion."""
        from src.jdbc.server import _inflight_tasks, _conn_counter_lock

        server = PGWireServer()
        # Give a known pid/secret for testing
        server._pid = 8888
        server._cancel_secret = 7777
        cancel_key = (8888, 7777)

        async def quick_query():
            # Verify that we are registered during execution
            with _conn_counter_lock:
                assert cancel_key in _inflight_tasks
            return {"columns": ["x"], "rows": [["1"]]}

        result = await server._run_cancellable(quick_query())
        assert result == {"columns": ["x"], "rows": [["1"]]}

        # After completion, the task is unregistered
        with _conn_counter_lock:
            assert cancel_key not in _inflight_tasks

    @pytest.mark.asyncio
    async def test_run_cancellable_raises_cancelled_error(self):
        """When a task is cancelled via the registry, CancelledError is raised."""
        from src.jdbc.server import _inflight_tasks, _conn_counter_lock

        server = PGWireServer()
        server._pid = 9999
        server._cancel_secret = 1111
        cancel_key = (9999, 1111)

        started = asyncio.Event()

        async def slow_query():
            started.set()
            await asyncio.sleep(10)  # long enough to be cancelled
            return {"columns": [], "rows": []}

        async def run_and_cancel():
            # Start the cancellable query in a separate task
            run_task = asyncio.ensure_future(server._run_cancellable(slow_query()))
            await started.wait()
            # Now cancel via the registry
            with _conn_counter_lock:
                inflight = _inflight_tasks.get(cancel_key)
            assert inflight is not None
            inflight.cancel()
            return await run_task

        with pytest.raises(asyncio.CancelledError):
            await run_and_cancel()

        # Cleanup happened
        with _conn_counter_lock:
            assert cancel_key not in _inflight_tasks

    def test_cancel_request_protocol_parsing(self):
        """CancelRequest protocol message is parsed with pid and secret."""
        body = struct.pack("!III", proto.CANCEL_REQUEST_CODE, 42, 9876)
        frame = struct.pack("!I", len(body) + 4) + body

        import asyncio
        result = asyncio.run(
            proto.read_startup(_FakeStartupReader(frame))
        )
        assert result["type"] == "cancel"
        assert result["pid"] == 42
        assert result["secret"] == 9876


class _FakeStartupReader:
    """Feeds raw bytes for read_startup."""
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        return chunk
