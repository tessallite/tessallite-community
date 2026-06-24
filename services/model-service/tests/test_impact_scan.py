"""Unit tests for impact-scan table matching (F-030-10)."""
from __future__ import annotations

import pytest

from src.api.impact_scan import _table_matches

pytestmark = pytest.mark.unit


def test_table_match_is_identifier_aware():
    """F-030-10: a table name must match as a whole SQL identifier, not as a
    substring — `order` must not match `orders` or `order_items`."""
    assert _table_matches("order", "select * from order where id = 1")
    assert _table_matches("order", "select * from public.order o")
    # Substring false positives are rejected.
    assert not _table_matches("order", "select * from orders")
    assert not _table_matches("order", "select * from order_items")
    assert not _table_matches("order", "select reorder_flag from products")


def test_table_match_handles_qualified_and_quoted_names():
    assert _table_matches("payment", "select * from analytics.payment p")
    assert _table_matches("payment", 'select * from "payment"')
    assert not _table_matches("pay", "select payment_method from payment")
