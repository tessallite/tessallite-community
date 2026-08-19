"""Tests for hierarchy-level dimension naming (H3 + Bug-423 fixes).

Verifies that ``resolve_hierarchy_dimension_map`` produces qualified
names (``{hierarchy}.{level}``) when ambiguous, and promotes bare
names into both ``dim_names`` and lookup maps when unique.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from shared.semantic.aggregate_select_builder import PG_DIALECT, build_select_parts
from shared.semantic.hierarchy_resolver import (
    resolve_hierarchy_dimension_map,
    resolve_aggregate_layout_with_hierarchy_levels,
)


@pytest.mark.asyncio
async def test_bug_8684_qualified_hierarchy_grain_groups_known_values():
    """A qualified level must resolve to its column, not a flat-name collision."""
    table_id = uuid4()
    calendar_year_id = uuid4()
    fiscal_year_id = uuid4()
    amount_id = uuid4()
    explicit_year = SimpleNamespace(
        id=uuid4(),
        name="Year",
        source_column_id=fiscal_year_id,
        user_defined_attribute_id=None,
        is_time_dim=True,
    )
    hierarchy_dimensions = [
        SimpleNamespace(
            id=f"hlevel-{uuid4()}",
            name="Calendar.Year",
            source_column_id=calendar_year_id,
            user_defined_attribute_id=None,
            is_time_dim=True,
        ),
        SimpleNamespace(
            id=f"hlevel-{uuid4()}",
            name="Year",
            source_column_id=calendar_year_id,
            user_defined_attribute_id=None,
            is_time_dim=True,
        ),
    ]
    table = SimpleNamespace(id=table_id, physical_name="sales")
    columns = [
        SimpleNamespace(
            id=calendar_year_id,
            model_table_id=table_id,
            column_name="calendar_year",
        ),
        SimpleNamespace(
            id=fiscal_year_id,
            model_table_id=table_id,
            column_name="fiscal_year",
        ),
        SimpleNamespace(
            id=amount_id,
            model_table_id=table_id,
            column_name="amount",
        ),
    ]
    measure = SimpleNamespace(
        id=uuid4(), name="Revenue", source_column_id=amount_id
    )
    with patch(
        "shared.semantic.hierarchy_resolver.load_hierarchy_level_dimensions",
        new=AsyncMock(return_value=hierarchy_dimensions),
    ):
        layout, dimensions = await resolve_aggregate_layout_with_hierarchy_levels(
            uuid4(),
            AsyncMock(),
            [explicit_year],
            grain_names=["Calendar.Year"],
            measure_specs=[("Revenue", "sum", "sum")],
            measures=[measure],
            tables=[table],
            columns=columns,
        )

    assert layout.grain_cols[0].source_column_name == "calendar_year"
    assert (
        next(d for d in dimensions if d.name == "Year").source_column_id
        == fiscal_year_id
    )
    parts = build_select_parts(
        layout=layout,
        alias_by_table_id={table_id: "base"},
        dialect=PG_DIALECT,
        emit=lambda expression, output: f'{expression} AS "{output}"',
    )
    query = (
        f"SELECT {', '.join(parts)} FROM sales AS base "
        'GROUP BY base."calendar_year" ORDER BY base."calendar_year"'
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE sales "
            "(calendar_year INTEGER, fiscal_year INTEGER, amount INTEGER)"
        )
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?, ?)",
            [(2024, 2025, 10), (2024, 2026, 20), (2025, 2026, 5)],
        )
        rows = connection.execute(query).fetchall()
    finally:
        connection.close()

    assert rows == [(2024, 30, 2), (2025, 5, 1)]


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
