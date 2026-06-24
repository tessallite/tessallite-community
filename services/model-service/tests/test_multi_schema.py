"""Tests for multi-schema source support (Block A)."""
import pytest
from shared.schemas.pydantic_models import DataSourceCreate, DataSourceResponse


def test_datasource_create_accepts_default_schema():
    ds = DataSourceCreate(
        project_connection_id="00000000-0000-0000-0000-000000000001",
        source_type="postgresql",
        display_name="DWH",
        default_schema="analytics",
    )
    assert ds.default_schema == "analytics"


def test_datasource_create_default_schema_optional():
    ds = DataSourceCreate(
        project_connection_id="00000000-0000-0000-0000-000000000001",
        source_type="postgresql",
        display_name="DWH",
    )
    assert ds.default_schema is None


def test_datasource_response_includes_default_schema():
    resp = DataSourceResponse(
        id="00000000-0000-0000-0000-000000000001",
        model_id="00000000-0000-0000-0000-000000000002",
        project_connection_id="00000000-0000-0000-0000-000000000003",
        source_type="postgresql",
        display_name="DWH",
        default_schema="analytics",
        config={},
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    assert resp.default_schema == "analytics"


# F-014-13 (ML13): the `_qualify_physical_name` helper in `src.api.sources` was
# dead code (zero production callers, flagged by the Fable unit-014 review) and
# has been deleted. The four isolated tests that exercised it were stale — they
# asserted a removed helper rather than any reachable behaviour — so they were
# removed with it. Schema qualification for source discovery is handled by the
# query-router `/introspect` path, not by this helper.
