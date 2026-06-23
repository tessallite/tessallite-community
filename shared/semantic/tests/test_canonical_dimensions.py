"""Tests for canonical_dimensions module."""
from __future__ import annotations

import types
import uuid

import pytest

from shared.semantic.canonical_dimensions import (
    CanonicalDim,
    _backing_key,
    build_name_to_canonical_map,
)


def test_backing_key_uda():
    uid = uuid.uuid4()
    assert _backing_key(None, uid) == f"uda:{uid}"


def test_backing_key_col():
    uid = uuid.uuid4()
    assert _backing_key(uid, None) == f"col:{uid}"


def test_backing_key_uda_takes_precedence():
    col_id = uuid.uuid4()
    uda_id = uuid.uuid4()
    assert _backing_key(col_id, uda_id) == f"uda:{uda_id}"


def test_backing_key_none():
    assert _backing_key(None, None) is None


def test_build_name_to_canonical_map_basic():
    dims = [
        CanonicalDim(
            canonical_name="city",
            backing_key="col:abc",
            all_names={"city", "geo_hierarchy.city"},
        ),
        CanonicalDim(
            canonical_name="year",
            backing_key="uda:def",
            all_names={"year", "date_hier.Year", "fiscal_hier.Year"},
        ),
    ]
    mapping = build_name_to_canonical_map(dims)
    assert mapping["city"] == "city"
    assert mapping["geo_hierarchy.city"] == "city"
    assert mapping["year"] == "year"
    assert mapping["date_hier.Year"] == "year"
    assert mapping["fiscal_hier.Year"] == "year"


def test_build_name_to_canonical_map_no_dims():
    assert build_name_to_canonical_map([]) == {}


def test_canonical_dim_all_names_includes_canonical():
    dim = CanonicalDim(
        canonical_name="month",
        backing_key="uda:xyz",
        all_names={"month", "date.Month"},
    )
    mapping = build_name_to_canonical_map([dim])
    assert mapping["month"] == "month"
    assert mapping["date.Month"] == "month"


@pytest.mark.asyncio
async def test_bare_name_not_added_when_flat_dim_exists():
    """When a flat dimension has the same bare name as a hierarchy level,
    the hierarchy level must NOT get a bare alias — it would shadow the
    flat dimension in the canonical map (BUG-1 fix)."""
    from unittest.mock import AsyncMock, MagicMock
    from shared.semantic.canonical_dimensions import build_canonical_dimension_list

    col_flat = uuid.uuid4()
    uda_hier = uuid.uuid4()

    flat_year = MagicMock()
    flat_year.id = uuid.uuid4()
    flat_year.name = "Year"
    flat_year.source_column_id = col_flat
    flat_year.user_defined_attribute_id = None
    flat_year.is_time_dim = True

    hierarchy = MagicMock()
    hierarchy.name = "date_hier"
    hierarchy.model_id = uuid.uuid4()
    hierarchy.dimension_kind = "time"

    level = MagicMock()
    level.name = "Year"
    level.key_attribute_source = "user_defined_attribute"
    level.key_attribute_id = uda_hier
    level.ordinal = 1

    flat_result = MagicMock()
    flat_result.scalars.return_value.all.return_value = [flat_year]

    hier_result = MagicMock()
    hier_result.all.return_value = [(hierarchy, level)]

    call_idx = [0]

    async def mock_execute(stmt):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx == 0:
            return flat_result
        return hier_result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=mock_execute)

    dims = await build_canonical_dimension_list(hierarchy.model_id, db)

    names_map = build_name_to_canonical_map(dims)
    assert names_map["Year"] == "Year", (
        "Flat dim 'Year' must own the bare name, not the hierarchy level"
    )
    hier_entry = next(d for d in dims if d.backing_key == f"uda:{uda_hier}")
    assert "Year" not in hier_entry.all_names, (
        "Hierarchy level must not get bare alias when flat dim has the same name"
    )
    assert hier_entry.canonical_name == "date_hier.Year"
