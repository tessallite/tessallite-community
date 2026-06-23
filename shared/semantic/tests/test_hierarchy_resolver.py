"""Tests for hierarchy-level dimension naming (H3 + Bug-423 fixes).

Verifies that ``resolve_hierarchy_dimension_map`` produces qualified
names (``{hierarchy}.{level}``) when ambiguous, and promotes bare
names into both ``dim_names`` and lookup maps when unique.
"""
from __future__ import annotations

from shared.semantic.hierarchy_resolver import resolve_hierarchy_dimension_map


def test_two_hierarchies_same_level_produces_qualified_names():
    dims_meta: list[dict] = []
    hierarchy_meta = [
        {
            "name": "Date",
            "levels": [
                {"name": "Year", "ordinal": 0, "key_attribute_source": "physical_column", "key_attribute_id": "col-1"},
                {"name": "Month", "ordinal": 1, "key_attribute_source": "physical_column", "key_attribute_id": "col-2"},
            ],
        },
        {
            "name": "Fiscal",
            "levels": [
                {"name": "Year", "ordinal": 0, "key_attribute_source": "physical_column", "key_attribute_id": "col-3"},
                {"name": "Quarter", "ordinal": 1, "key_attribute_source": "physical_column", "key_attribute_id": "col-4"},
            ],
        },
    ]

    dim_names, level_map, default_map = resolve_hierarchy_dimension_map(dims_meta, hierarchy_meta)

    assert "Date.Year" in dim_names
    assert "Fiscal.Year" in dim_names
    assert "Year" not in dim_names

    assert level_map["date"]["year"] == "Date.Year"
    assert level_map["fiscal"]["year"] == "Fiscal.Year"

    assert "Date.Month" in dim_names
    assert "Month" in dim_names
    assert level_map["date"]["month"] == "Month"

    assert "Fiscal.Quarter" in dim_names
    assert "Quarter" in dim_names
    assert level_map["fiscal"]["quarter"] == "Quarter"


def test_unique_level_gets_bare_alias():
    dims_meta: list[dict] = []
    hierarchy_meta = [
        {
            "name": "Geography",
            "levels": [
                {"name": "Country", "ordinal": 0, "key_attribute_source": "physical_column", "key_attribute_id": "col-1"},
                {"name": "City", "ordinal": 1, "key_attribute_source": "physical_column", "key_attribute_id": "col-2"},
            ],
        },
    ]

    dim_names, level_map, default_map = resolve_hierarchy_dimension_map(dims_meta, hierarchy_meta)

    assert "Geography.Country" in dim_names
    assert "Country" in dim_names
    assert "Geography.City" in dim_names
    assert "City" in dim_names

    assert level_map["geography"]["country"] == "Country"
    assert level_map["geography"]["city"] == "City"


def test_explicit_dimension_match_preserves_original_name():
    dims_meta = [
        {"name": "Region", "source_column_id": "col-10"},
    ]
    hierarchy_meta = [
        {
            "name": "Geography",
            "levels": [
                {"name": "Region", "ordinal": 0, "key_attribute_source": "physical_column", "key_attribute_id": "col-10"},
            ],
        },
    ]

    dim_names, level_map, default_map = resolve_hierarchy_dimension_map(dims_meta, hierarchy_meta)

    assert level_map["geography"]["region"] == "Region"
    assert "Geography.Region" not in dim_names


def test_bare_name_matching_explicit_dimension_uses_explicit_name():
    """When a level bare name matches an explicit dimension, use it."""
    dims_meta = [
        {"name": "Year", "source_column_id": "col-99"},
    ]
    hierarchy_meta = [
        {
            "name": "Date",
            "levels": [
                {"name": "Year", "ordinal": 0, "key_attribute_source": "physical_column", "key_attribute_id": "col-5"},
                {"name": "Month", "ordinal": 1, "key_attribute_source": "physical_column", "key_attribute_id": "col-6"},
            ],
        },
    ]

    dim_names, level_map, default_map = resolve_hierarchy_dimension_map(dims_meta, hierarchy_meta)

    assert level_map["date"]["year"] == "Year"
    assert "Year" in dim_names
    assert "Month" in dim_names


def test_default_dim_uses_deepest_level():
    dims_meta: list[dict] = []
    hierarchy_meta = [
        {
            "name": "Geography",
            "levels": [
                {"name": "Country", "ordinal": 0, "key_attribute_source": "physical_column", "key_attribute_id": "col-1"},
                {"name": "City", "ordinal": 1, "key_attribute_source": "physical_column", "key_attribute_id": "col-2"},
            ],
        },
    ]

    dim_names, level_map, default_map = resolve_hierarchy_dimension_map(dims_meta, hierarchy_meta)

    assert default_map["geography"] == "City"
