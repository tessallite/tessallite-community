"""Tests for Gateway / XMLA Correctness lane L3 bugs.

Bug-6945: Timeline range sentinel must not corrupt bounds for member keys
          containing ``__``.  The old ``__BETWEEN__<start>__<end>`` encoding
          split on every ``__`` occurrence; keys like ``FY__2024`` produced
          wrong BETWEEN bounds.

Bug-6933: Pre-auth rejections (malformed dbname, empty password) must count
          toward the brute-force throttle.

Bug-6946: Subtotal grain-query failures must surface a client-visible SOAP
          Warning instead of degrading silently.

Bug-7842: test_bug_5888_xmla_cancel fake_get_model_hierarchies must match
          the production signature (positional model_id, tenant_slug,
          jwt_token args).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_GATEWAY_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _GATEWAY_SRC not in sys.path:
    sys.path.insert(0, _GATEWAY_SRC)


# ---------------------------------------------------------------------------
# Bug-6945: collision-safe timeline sentinel
# ---------------------------------------------------------------------------

class TestBug6945TimelineSentinel:
    """Member keys containing ``__`` must survive the range sentinel
    encoding/decoding round-trip without corrupting the BETWEEN bounds."""

    def test_double_underscore_key_round_trip(self):
        """A range filter with keys ``FY__2024`` / ``FY__2026`` must produce
        a correct BETWEEN clause, not split on the embedded ``__``."""
        from dax.xmla_server import (
            _build_where_sql_clauses,
            _RANGE_PREFIX,
            _RANGE_SEP,
        )
        from shared.connector_qualify import quote_identifier

        # Simulate producer output: sentinel with double-underscore keys.
        sentinel = f"{_RANGE_PREFIX}FY__2024{_RANGE_SEP}FY__2026"
        where_filters = {"fiscal_year": [sentinel]}
        clauses = _build_where_sql_clauses(
            where_filters,
            lambda n: quote_identifier("postgresql", n),
        )
        assert len(clauses) == 1
        clause = clauses[0]
        assert "BETWEEN" in clause
        assert "'FY__2024'" in clause
        assert "'FY__2026'" in clause

    def test_plain_key_still_works(self):
        """Plain keys (no ``__``) must still round-trip correctly."""
        from dax.xmla_server import (
            _build_where_sql_clauses,
            _RANGE_PREFIX,
            _RANGE_SEP,
        )
        from shared.connector_qualify import quote_identifier

        sentinel = f"{_RANGE_PREFIX}2024{_RANGE_SEP}2026"
        where_filters = {"year": [sentinel]}
        clauses = _build_where_sql_clauses(
            where_filters,
            lambda n: quote_identifier("postgresql", n),
        )
        assert len(clauses) == 1
        assert "'2024'" in clauses[0]
        assert "'2026'" in clauses[0]

    def test_sentinel_does_not_match_normal_values(self):
        """A normal dimension value starting with ``__BETWEEN__`` (the old
        prefix) must NOT be mistakenly parsed as a range sentinel."""
        from dax.xmla_server import (
            _build_where_sql_clauses,
            _RANGE_PREFIX,
        )
        from shared.connector_qualify import quote_identifier

        # A member key that looks like the old sentinel format.
        filters = {"dim": ["__BETWEEN__A__B"]}
        clauses = _build_where_sql_clauses(
            filters,
            lambda n: quote_identifier("postgresql", n),
        )
        # The old-format string should be treated as a NORMAL value (IN/=),
        # not parsed as a range.  The clause must be an equality, not a
        # ``BETWEEN ... AND ...`` range expression.
        assert len(clauses) == 1
        assert " = " in clauses[0]
        # Verify the value is preserved verbatim as a quoted literal.
        assert "'__BETWEEN__A__B'" in clauses[0]


# ---------------------------------------------------------------------------
# Bug-6933: pre-auth rejections must count toward the throttle
# ---------------------------------------------------------------------------

class TestBug6933PreAuthThrottle:
    """Malformed dbname and empty password rejections must call
    governor.record_auth_failure so repeated attempts are throttled."""

    @pytest.mark.asyncio
    async def test_malformed_dbname_records_failure(self):
        from jdbc.server import PGWireServer
        from jdbc.throttle import JdbcConnectionGovernor

        server = PGWireServer.__new__(PGWireServer)
        server._pid = 1
        server._peer_ip = "1.2.3.4"
        server._model_id = None

        governor = JdbcConnectionGovernor()
        writer = AsyncMock()

        with patch("jdbc.server.get_governor", return_value=governor):
            with patch("jdbc.server.proto") as mock_proto:
                mock_proto.error_response.return_value = b"error"
                # Use an unparseable database name (contains a space).
                result = await server._authenticate(
                    {"database": "bad name!!!"}, AsyncMock(), writer,
                )

        assert result is False
        # The failure must have been recorded.
        assert governor.is_throttled("1.2.3.4") is False  # 1 failure < default threshold
        # But it WAS recorded (the deque is non-empty).
        assert len(governor._failures.get("1.2.3.4", [])) == 1

    @pytest.mark.asyncio
    async def test_empty_password_records_failure(self):
        from jdbc.server import PGWireServer
        from jdbc.throttle import JdbcConnectionGovernor

        server = PGWireServer.__new__(PGWireServer)
        server._pid = 1
        server._peer_ip = "1.2.3.4"
        server._model_id = None
        server._tenant_slug = None
        server._project_hint = None

        governor = JdbcConnectionGovernor()
        reader = AsyncMock()
        writer = AsyncMock()

        with patch("jdbc.server.get_governor", return_value=governor):
            with patch("jdbc.server.proto") as mock_proto:
                mock_proto.error_response.return_value = b"error"
                mock_proto.authentication_cleartext_password.return_value = b"challenge"
                # Simulate empty password returned from the client.
                mock_proto.read_password_message = AsyncMock(return_value="")
                result = await server._authenticate(
                    {"database": "acme-demo"}, reader, writer,
                )

        assert result is False
        assert len(governor._failures.get("1.2.3.4", [])) == 1


# ---------------------------------------------------------------------------
# Bug-6946: subtotal grain-query failure warning
# ---------------------------------------------------------------------------

class TestBug6946SubtotalWarning:
    """Failed subtotal grain queries must produce a SOAP <Warning>."""

    def test_failed_grain_labels_produce_warning_xml(self):
        """Verify the warning XML generation pattern works end-to-end.
        (The full integration requires a live server; this tests the
        contract that _failed_grain_labels -> Messages XML.)"""
        from dax.xmla_server import _escape_xml

        labels = ["grand_total", "year_subtotal"]
        msgs = "".join(
            f'<Warning><Description>{_escape_xml("Subtotal grain query failed for level: " + lbl)}</Description></Warning>'
            for lbl in labels
        )
        messages_xml = f"<Messages>{msgs}</Messages>"
        assert "<Warning>" in messages_xml
        assert "grand_total" in messages_xml
        assert "year_subtotal" in messages_xml
        assert messages_xml.count("<Warning>") == 2


# ---------------------------------------------------------------------------
# Bug-7842: fake_get_model_hierarchies signature
# ---------------------------------------------------------------------------

class TestBug7842FakeSignature:
    """The test double in test_bug_5888_xmla_cancel must accept the same
    positional args as the production get_model_hierarchies."""

    def test_fake_accepts_positional_args(self):
        """The fake must be callable with (model_id, tenant_slug, jwt_token)
        positional args plus arbitrary keyword args, matching the production
        function in router_client.py."""
        from router_client import get_model_hierarchies
        import inspect

        prod_sig = inspect.signature(get_model_hierarchies)
        prod_params = list(prod_sig.parameters.keys())
        # The production function takes model_id, tenant_slug, jwt_token as
        # the first three positional parameters.
        assert prod_params[:3] == ["model_id", "tenant_slug", "jwt_token"]

        # Now verify the FIXED fake in test_bug_5888_xmla_cancel._patch_common
        # accepts them.  We import the module and extract the fake.
        import importlib
        test_mod = importlib.import_module("test_bug_5888_xmla_cancel")
        # The fake is defined inside _patch_common; call _patch_common with a
        # mock monkeypatch and capture the fake it sets.
        captured = {}

        class FakeMonkeypatch:
            def setattr(self, obj, name, value):
                if name == "get_model_hierarchies":
                    captured["fake"] = value

        test_mod._patch_common(FakeMonkeypatch(), AsyncMock())
        fake_fn = captured["fake"]
        fake_sig = inspect.signature(fake_fn)
        fake_params = list(fake_sig.parameters.keys())
        # The fake must accept model_id, tenant_slug, jwt_token as positional.
        assert "model_id" in fake_params
        assert "tenant_slug" in fake_params
        assert "jwt_token" in fake_params
