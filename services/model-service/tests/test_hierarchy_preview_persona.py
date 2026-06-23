"""Bug-5424: hierarchy preview must honour persona_id for RLS scoping.

Tests that:
1. persona_id is accepted by the preview endpoint.
2. When persona excludes the hierarchy, a 404 is returned.
3. When persona has RLS rules (and bypass_row_security=False),
   the compiled predicate is injected into the preview SQL.
4. When persona has bypass_row_security=True, no RLS is injected.
5. Level-attribute exclusion filters out persona-excluded levels.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"


def _make_persona(
    *,
    persona_id: uuid.UUID | None = None,
    bypass_rls: bool = False,
    included_hierarchy_ids: list | None = None,
    included_dimension_ids: list | None = None,
):
    return types.SimpleNamespace(
        id=persona_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name="restricted",
        slug="restricted",
        bypass_row_security=bypass_rls,
        included_hierarchy_ids=included_hierarchy_ids or [],
        included_dimension_ids=included_dimension_ids or [],
        includes_hidden_columns=False,
        audience_roles=[],
        default_filters={},
    )


def _make_levels(count: int = 2):
    return [
        types.SimpleNamespace(
            ordinal=i,
            name=f"Level_{i}",
            key_attribute_id=uuid.uuid4(),
            key_attribute_source="physical_column",
        )
        for i in range(count)
    ]


def _make_resolved_sql(table_name: str = "dim_region"):
    table_id = uuid.uuid4()
    return types.SimpleNamespace(
        resolved=types.SimpleNamespace(
            table=types.SimpleNamespace(
                id=table_id,
                physical_name=table_name,
                row_count_estimate=100,
            ),
            ref=types.SimpleNamespace(name="region_code", data_type="varchar"),
        ),
        expression='"t"."region_code"',
        referenced_column_ids=[],
    )


@pytest.mark.asyncio
async def test_preview_accepts_persona_id_parameter(client):
    """GET .../preview?persona_id=... is accepted and returns 200."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())
    hierarchy_id = uuid.uuid4()
    persona = _make_persona()
    levels = _make_levels()

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(
                return_value=types.SimpleNamespace(
                    id=hierarchy_id, name="Geo", model_id=TEST_MODEL_ID,
                ),
            ),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch(
            "src.api.hierarchies._resolve_sql_attribute",
            new=AsyncMock(side_effect=[_make_resolved_sql(), _make_resolved_sql()]),
        ),
        patch(
            "src.api.hierarchies._resolve_preview_connection",
            new=AsyncMock(return_value=(None, "postgresql", "No connection")),
        ),
        patch("src.api.hierarchies.compile_row_security", new=AsyncMock(return_value=None)),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview"
            f"?sample_size=10&persona_id={persona.id}",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["hierarchy_id"] == str(hierarchy_id)


@pytest.mark.asyncio
async def test_preview_returns_404_when_persona_excludes_hierarchy(client):
    """If the persona's included_hierarchy_ids does not include the
    requested hierarchy, the preview endpoint must return 404."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())
    hierarchy_id = uuid.uuid4()
    other_hierarchy_id = uuid.uuid4()
    persona = _make_persona(included_hierarchy_ids=[str(other_hierarchy_id)])

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(
                return_value=types.SimpleNamespace(
                    id=hierarchy_id, name="Geo", model_id=TEST_MODEL_ID,
                ),
            ),
        ),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview"
            f"?sample_size=10&persona_id={persona.id}",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_injects_rls_predicate_into_sql(client):
    """When persona has active RLS rules (bypass_row_security=False),
    the compiled predicate must be injected into the preview SQL."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())
    hierarchy_id = uuid.uuid4()
    persona = _make_persona(bypass_rls=False)
    levels = _make_levels()
    resolved = _make_resolved_sql()

    compiled_pred = types.SimpleNamespace(
        sql_expression='"customer_region" = \'US\'',
        active_rule_ids=("rule-1",),
        security_dimension_columns=("customer_region",),
        applied_rules=({"rule_id": "rule-1", "rule_name": "US Only",
                        "predicate_sql": '"customer_region" = \'US\''},),
    )

    captured_estimate_calls: list[dict] = []
    captured_sample_calls: list[dict] = []

    original_build_estimate = None
    original_build_sample = None

    # Capture the rls_where parameter passed to SQL builders
    from src.api import hierarchies as hier_mod

    original_build_estimate = hier_mod._build_estimate_sql
    original_build_sample = hier_mod._build_sample_sql

    def capturing_build_estimate(connector, *, table_name, key_expr,
                                 sample_size, rls_where=None):
        captured_estimate_calls.append({"rls_where": rls_where})
        return "SELECT 1"

    def capturing_build_sample(connector, *, table_name, key_expr,
                               sample_size, parent_expr=None,
                               parent_key=None, rls_where=None):
        captured_sample_calls.append({"rls_where": rls_where})
        return "SELECT 1"

    introspect_mock = AsyncMock(return_value={
        "est_0": ([{"c": 5}], ["c"], None),
        "est_1": ([{"c": 10}], ["c"], None),
        "sample": ([{"key_value": "US"}], ["key_value"], None),
    })

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(
                return_value=types.SimpleNamespace(
                    id=hierarchy_id, name="Geo", model_id=TEST_MODEL_ID,
                ),
            ),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch(
            "src.api.hierarchies._resolve_sql_attribute",
            new=AsyncMock(side_effect=[resolved, _make_resolved_sql()]),
        ),
        patch(
            "src.api.hierarchies._resolve_preview_connection",
            new=AsyncMock(return_value=(object(), "postgresql", None)),
        ),
        patch("src.api.hierarchies.compile_row_security", new=AsyncMock(return_value=compiled_pred)),
        patch("src.api.hierarchies._build_estimate_sql", capturing_build_estimate),
        patch("src.api.hierarchies._build_sample_sql", capturing_build_sample),
        patch("src.api.hierarchies._introspect_batch_via_router", introspect_mock),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview"
            f"?sample_size=10&persona_id={persona.id}",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200

    # Verify RLS predicate was passed to all SQL builder calls
    for call in captured_estimate_calls:
        assert call["rls_where"] == '"customer_region" = \'US\''

    for call in captured_sample_calls:
        assert call["rls_where"] == '"customer_region" = \'US\''


@pytest.mark.asyncio
async def test_preview_skips_rls_when_persona_bypasses(client):
    """When persona has bypass_row_security=True, no RLS predicate is
    injected even if rules exist."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())
    hierarchy_id = uuid.uuid4()
    persona = _make_persona(bypass_rls=True)
    levels = _make_levels()
    resolved = _make_resolved_sql()

    captured_rls_args: list[str | None] = []

    def capturing_build_estimate(connector, *, table_name, key_expr,
                                 sample_size, rls_where=None):
        captured_rls_args.append(rls_where)
        return "SELECT 1"

    def capturing_build_sample(connector, *, table_name, key_expr,
                               sample_size, parent_expr=None,
                               parent_key=None, rls_where=None):
        captured_rls_args.append(rls_where)
        return "SELECT 1"

    introspect_mock = AsyncMock(return_value={
        "est_0": ([{"c": 5}], ["c"], None),
        "est_1": ([{"c": 10}], ["c"], None),
        "sample": ([{"key_value": "US"}], ["key_value"], None),
    })

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(
                return_value=types.SimpleNamespace(
                    id=hierarchy_id, name="Geo", model_id=TEST_MODEL_ID,
                ),
            ),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch(
            "src.api.hierarchies._resolve_sql_attribute",
            new=AsyncMock(side_effect=[resolved, _make_resolved_sql()]),
        ),
        patch(
            "src.api.hierarchies._resolve_preview_connection",
            new=AsyncMock(return_value=(object(), "postgresql", None)),
        ),
        patch("src.api.hierarchies._build_estimate_sql", capturing_build_estimate),
        patch("src.api.hierarchies._build_sample_sql", capturing_build_sample),
        patch("src.api.hierarchies._introspect_batch_via_router", introspect_mock),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview"
            f"?sample_size=10&persona_id={persona.id}",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200
    # compile_row_security was never called (bypass=True), so rls_where=None
    for arg in captured_rls_args:
        assert arg is None


@pytest.mark.asyncio
async def test_preview_filters_levels_by_persona_dimension_exclusion(client):
    """When persona's included_dimension_ids excludes attributes used by
    hierarchy levels, those levels must be filtered from the preview."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())
    hierarchy_id = uuid.uuid4()
    excluded_attr_id = uuid.uuid4()
    included_attr_id = uuid.uuid4()

    persona = _make_persona()

    levels = [
        types.SimpleNamespace(
            ordinal=0, name="Region",
            key_attribute_id=included_attr_id,
            key_attribute_source="physical_column",
        ),
        types.SimpleNamespace(
            ordinal=1, name="Excluded",
            key_attribute_id=excluded_attr_id,
            key_attribute_source="physical_column",
        ),
    ]

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(
                return_value=types.SimpleNamespace(
                    id=hierarchy_id, name="Geo", model_id=TEST_MODEL_ID,
                ),
            ),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch(
            "src.api.hierarchies.get_excluded_level_attribute_ids",
            new=AsyncMock(return_value={excluded_attr_id}),
        ),
        patch(
            "src.api.hierarchies._resolve_preview_connection",
            new=AsyncMock(return_value=(None, "postgresql", "No connection")),
        ),
        patch("src.api.hierarchies.compile_row_security", new=AsyncMock(return_value=None)),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview"
            f"?sample_size=10&persona_id={persona.id}",
            headers={"Authorization": "Bearer test-token"},
        )

    # With only 1 level remaining after filtering, the endpoint should
    # raise the "at least two levels" validation error.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_preview_without_persona_id_works_as_before(client):
    """Calling preview without persona_id must still work (no persona
    resolution, no RLS injection, no level filtering)."""
    mock_db = make_mock_db()
    mock_db.get = AsyncMock(return_value=make_model())
    hierarchy_id = uuid.uuid4()
    levels = _make_levels()

    with (
        patch("src.api.hierarchies.get_tenant_db", async_gen_from(mock_db)),
        patch(
            "src.api.hierarchies.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.api.hierarchies._load_hierarchy_or_404",
            new=AsyncMock(
                return_value=types.SimpleNamespace(
                    id=hierarchy_id, name="Geo", model_id=TEST_MODEL_ID,
                ),
            ),
        ),
        patch("src.api.hierarchies._levels_for_hierarchy", new=AsyncMock(return_value=levels)),
        patch(
            "src.api.hierarchies._resolve_sql_attribute",
            new=AsyncMock(side_effect=[_make_resolved_sql(), _make_resolved_sql()]),
        ),
        patch(
            "src.api.hierarchies._resolve_preview_connection",
            new=AsyncMock(return_value=(None, "postgresql", "No connection")),
        ),
    ):
        resp = await client.get(
            f"{PREFIX}/hierarchies/{hierarchy_id}/preview?sample_size=10",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["hierarchy_id"] == str(hierarchy_id)
