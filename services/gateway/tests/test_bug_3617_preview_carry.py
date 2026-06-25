"""Bug-3617 (Phase 0.5c): the gateway preview-member mapping carries the member
KEY, CAPTION, and ancestor key PATH separately from the display name.

These are the fields the canonical member-identity layer (Phase 1/2) needs:
- ``key``     -> MEMBER_KEY / the key segment of MEMBER_UNIQUE_NAME
- ``caption`` -> MEMBER_CAPTION (may differ from the key)
- ``key_path``-> ancestor-first key path for the canonical composite uname,
                 present only when the model-service supplied one.
"""
from __future__ import annotations

import pytest

from src.dax.xmla_server import _to_preview_member_row

pytestmark = pytest.mark.unit


def test_carry_key_caption_and_path_when_present():
    row = _to_preview_member_row(
        {
            "key_value": "4",
            "caption": "April",
            "key_path": ["2025", "4"],
            "parent_key": "2025",
            "level_name": "Month",
        },
        ordinal=3,
    )
    assert row["key"] == "4"
    assert row["caption"] == "April"
    assert row["key_path"] == ["2025", "4"]
    assert row["parent"] == "2025"
    assert row["level"] == "Month"
    # name stays = key_value for back-compat with the caption-form emit (Phase 2).
    assert row["name"] == "4"


def test_caption_defaults_to_key_when_absent():
    row = _to_preview_member_row({"key_value": "DE"}, ordinal=0)
    assert row["key"] == "DE"
    assert row["caption"] == "DE"
    assert row["key_path"] is None


def test_key_path_none_when_not_a_list():
    row = _to_preview_member_row(
        {"key_value": "X", "key_path": "not-a-list"}, ordinal=0
    )
    assert row["key_path"] is None


def test_key_path_coerced_to_strings():
    row = _to_preview_member_row(
        {"key_value": "4", "key_path": [2025, 4]}, ordinal=0
    )
    assert row["key_path"] == ["2025", "4"]
