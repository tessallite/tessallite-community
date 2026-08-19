"""Bug-8285: member-caption end-to-end wiring on the gateway side.

Three seams are pinned here:
1. PRODUCER SIGNAL — ``_caption_dimension_names`` selects exactly the dimensions
   that declare a distinct display column, so the gateway asks the router to
   project captions only for those (ExecuteRequest.caption_dimensions).
2. CONTRACT ALIGNMENT — the companion column name the consumer expects
   (``_member_caption_col``) uses the ``__caption`` suffix the router projects.
3. CONSUMER RENDER — ``_normalize_member_captions`` rewrites a member's caption to
   the display value looked up from the companion column, keeping the key as UName.
"""
from src.dax.xmla_server import _caption_dimension_names
from src.dax.mdx_execute import _member_caption_col, _normalize_member_captions


def test_caption_dimension_names_selects_distinct_display_dims():
    dims = [
        {"name": "product_code", "display_column_name": "product_name"},
        {"name": "region_code", "display_column_name": None},          # none
        {"name": "account_type", "display_column_name": ""},            # empty
        {"name": "city", "display_column_name": "city"},               # equal -> skip
        {"name": "channel", "display_column_name": "channel_label"},
        {"name": "", "display_column_name": "whatever"},               # no name
    ]
    assert _caption_dimension_names(dims) == ["product_code", "channel"]


def test_caption_dimension_names_empty_inputs():
    assert _caption_dimension_names(None) == []
    assert _caption_dimension_names([]) == []


def test_companion_column_name_matches_router_suffix():
    # The router projects "<dim>__caption"; the consumer must build the same name.
    assert _member_caption_col("product_code") == "product_code__caption"


def test_normalize_member_captions_renders_display_value():
    members = [
        {"hierarchy": "[product_code]", "lname": "[product_code].[product_code]",
         "key": "P001", "caption": "P001"},
        {"hierarchy": "[product_code]", "lname": "[product_code].[product_code]",
         "key": "P002", "caption": "P002"},
    ]
    lookup = {"product_code": {"P001": "Premium Card", "P002": "Debit Card"}}
    _normalize_member_captions(members, {}, lookup)
    # Caption became the friendly display value; the key (UName) is unchanged.
    assert members[0]["caption"] == "Premium Card"
    assert members[0]["key"] == "P001"
    assert members[1]["caption"] == "Debit Card"


def test_normalize_member_captions_missing_lookup_keeps_key():
    members = [
        {"hierarchy": "[product_code]", "lname": "[product_code].[product_code]",
         "key": "P999", "caption": "P999"},
    ]
    lookup = {"product_code": {"P001": "Premium Card"}}
    _normalize_member_captions(members, {}, lookup)
    # An unmapped member keeps its key caption (no crash, no blank).
    assert members[0]["caption"] == "P999"
