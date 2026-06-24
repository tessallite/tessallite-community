"""Tests for date intelligence helpers (Block C)."""
import pytest
from shared.schemas.pydantic_models import HierarchySummaryResponse


def test_hierarchy_summary_includes_level_names():
    resp = HierarchySummaryResponse(
        id="00000000-0000-0000-0000-000000000001",
        model_id="00000000-0000-0000-0000-000000000002",
        name="Date Hierarchy",
        type="date_embedded",
        dimension_kind=None,
        description=None,
        level_count=3,
        level_names=["Year", "Quarter", "Month"],
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    assert resp.level_names == ["Year", "Quarter", "Month"]


def test_hierarchy_summary_default_level_names_empty():
    resp = HierarchySummaryResponse(
        id="00000000-0000-0000-0000-000000000001",
        model_id="00000000-0000-0000-0000-000000000002",
        name="Region Hierarchy",
        type="explicit",
        dimension_kind=None,
        description=None,
        level_count=0,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    assert resp.level_names == []
