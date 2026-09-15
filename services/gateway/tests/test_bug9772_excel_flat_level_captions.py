"""Bug-9772: Excel flat All level keeps [(All)] identity, display-name caption.

Duplicate All/data captions repair the workbook on Open (a0e029179). Distinct
captions with the All unique name unchanged saved and reopened on ALEX
2026-09-03 without repair.
"""
from src.dax.mdschema import (
    _excel_flat_all_member_caption,
    _excel_flat_level_captions,
    _rows_levels,
    _rows_members,
)
from src.dax.member_uname import synthetic_all_member_metadata

_EXCEL = {"SspropInitAppName": "Microsoft Office Excel"}
_FLAT = {"name": "account_type", "display_name": "account type"}


def test_bug9772_excel_flat_all_caption_is_display_name_data_stays_distinct():
    rows = _rows_levels(
        catalog="m",
        dimensions=[_FLAT],
        member_data={"account_type": {"members": [{"name": "CURRENT"}]}},
        properties=_EXCEL,
    )
    levels = [
        row
        for row in rows
        if row.get("HIERARCHY_UNIQUE_NAME") == "[Dimensions].[account_type]"
    ]
    all_level = next(row for row in levels if row["LEVEL_NAME"] == "(All)")
    data_level = next(row for row in levels if row["LEVEL_NAME"] == "account_type")
    assert all_level["LEVEL_UNIQUE_NAME"].endswith(".[(All)]")
    assert all_level["LEVEL_CAPTION"] == "account type"
    assert data_level["LEVEL_CAPTION"] == "account_type"
    assert all_level["LEVEL_CAPTION"] != data_level["LEVEL_CAPTION"]


def test_bug9772_non_excel_flat_all_caption_stays_all():
    all_caption, data_caption = _excel_flat_level_captions(
        _FLAT, {"SspropInitAppName": "OnlyOffice"}
    )
    assert all_caption is None
    assert data_caption == "account type"


def test_bug9772_excel_hierarchy_all_caption_stays_all():
    geography = {
        "name": "Geography",
        "display_name": "Geography",
        "source": "hierarchy",
        "levels": [
            {"ordinal": 0, "name": "Region"},
            {"ordinal": 1, "name": "Country"},
        ],
    }
    rows = _rows_levels(
        catalog="m",
        dimensions=[geography],
        member_data={"Geography": {"members": [{"name": "EMEA"}]}},
        properties=_EXCEL,
    )
    all_level = next(row for row in rows if row["LEVEL_NAME"] == "(All)")
    assert all_level["LEVEL_CAPTION"] == "(All)"


def test_bug9772_excel_flat_same_name_and_display_keeps_distinct_data_caption():
    all_caption, data_caption = _excel_flat_level_captions(
        {"name": "channel", "display_name": "channel"},
        _EXCEL,
    )
    assert all_caption == "channel"
    assert data_caption == "channel values"
    assert data_caption != all_caption


def test_bug9772_excel_flat_all_member_caption_is_all_not_technical_name():
    """Nested blank subtotal rows cache MEMBER_CAPTION, not the level caption.

    Book5 showed ``All channel_name`` under each account type because the All
    member caption was ``All {technical name}``. Unique name stays ``[All]``.
    """
    assert _excel_flat_all_member_caption(_FLAT, _EXCEL) == "All"
    rows = _rows_members(
        "m",
        [{"name": "average_base_amount"}],
        [_FLAT, {"name": "channel_name", "display_name": "channel name"}],
        {},
        {
            "account_type": {"members": [{"name": "CREDIT"}]},
            "channel_name": {"members": [{"name": "Web"}]},
        },
        properties=_EXCEL,
    )
    all_rows = [row for row in rows if row.get("MEMBER_TYPE") == "2"]
    by_hier = {row["HIERARCHY_UNIQUE_NAME"]: row for row in all_rows}
    account = by_hier["[Dimensions].[account_type]"]
    channel = by_hier["[Dimensions].[channel_name]"]
    assert account["MEMBER_NAME"] == "All"
    assert account["MEMBER_UNIQUE_NAME"] == "[Dimensions].[account_type].[All]"
    assert account["MEMBER_CAPTION"] == "All"
    assert channel["MEMBER_UNIQUE_NAME"] == "[Dimensions].[channel_name].[All]"
    assert channel["MEMBER_CAPTION"] == "All"


def test_bug9772_non_excel_flat_all_member_caption_keeps_dimension_name():
    rows = _rows_members(
        "m",
        [],
        [_FLAT],
        {},
        {"account_type": {"members": [{"name": "CREDIT"}]}},
        properties={"SspropInitAppName": "OnlyOffice"},
    )
    all_row = next(row for row in rows if row.get("MEMBER_TYPE") == "2")
    assert all_row["MEMBER_CAPTION"] == "All account_type"
    assert all_row["MEMBER_UNIQUE_NAME"] == "[Dimensions].[account_type].[All]"


def test_bug9772_excel_hierarchy_all_member_caption_keeps_dimension_name():
    geography = {
        "name": "Geography",
        "display_name": "Geography",
        "source": "hierarchy",
        "levels": [
            {"ordinal": 0, "name": "Region"},
            {"ordinal": 1, "name": "Country"},
        ],
    }
    assert _excel_flat_all_member_caption(geography, _EXCEL) is None
    rows = _rows_members(
        "m",
        [],
        [geography],
        {},
        {"Geography": {"members": [{"name": "EMEA"}]}},
        properties=_EXCEL,
    )
    all_row = next(row for row in rows if row.get("MEMBER_TYPE") == "2")
    assert all_row["MEMBER_CAPTION"] == "All Geography"


def test_bug9772_all_member_metadata_caption_override_keeps_identity():
    default = synthetic_all_member_metadata(
        "[Dimensions].[channel_name]", "channel_name",
    )
    excel = synthetic_all_member_metadata(
        "[Dimensions].[channel_name]", "channel_name", caption="All",
    )
    assert default["caption"] == "All channel_name"
    assert excel["caption"] == "All"
    assert excel["uname"] == default["uname"] == "[Dimensions].[channel_name].[All]"
    assert excel["name"] == default["name"] == "All"
