"""Bug-5424: hierarchy preview must forward persona_id from gateway.

Tests that:
1. get_hierarchy_preview passes persona_id to the model-service endpoint.
2. _load_hierarchy_member_data threads persona_id to get_hierarchy_preview.
3. _load_discover_member_data threads persona_id to
   _load_hierarchy_member_data for hierarchy-sourced dimensions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.router_client import get_hierarchy_preview
from src.dax.xmla_server import (
    _load_hierarchy_member_data,
    _load_discover_member_data,
)


class TestBug5424RouterClient:
    """Verify that get_hierarchy_preview forwards persona_id."""

    @pytest.mark.asyncio
    async def test_persona_id_included_in_params(self, monkeypatch):
        """When persona_id is provided, it must appear in the query params
        sent to the model-service hierarchy preview endpoint."""
        import src.router_client as rc

        captured_params: list[dict] = []

        async def fake_resolve_project_id(model_id, tenant_slug, jwt_token):
            return "project-1"

        class FakeResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {"hierarchy_id": "h1", "members": [], "levels_summary": []}

        class FakeClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get(self, url, *, headers=None, params=None):
                captured_params.append(params or {})
                return FakeResponse()

        monkeypatch.setattr(rc, "_resolve_project_id", fake_resolve_project_id)

        import httpx
        original_async_client = httpx.AsyncClient

        def mock_client(**kwargs):
            return FakeClient()

        monkeypatch.setattr(httpx, "AsyncClient", mock_client)

        result = await get_hierarchy_preview(
            model_id="model-1",
            hierarchy_id="hier-1",
            tenant_slug="test",
            jwt_token="jwt-token",
            persona_id="persona-restricted-1",
        )

        assert len(captured_params) == 1
        assert captured_params[0].get("persona_id") == "persona-restricted-1"

    @pytest.mark.asyncio
    async def test_persona_id_omitted_when_none(self, monkeypatch):
        """When persona_id is None, it must NOT appear in query params."""
        import src.router_client as rc

        captured_params: list[dict] = []

        async def fake_resolve_project_id(model_id, tenant_slug, jwt_token):
            return "project-1"

        class FakeResponse:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {"hierarchy_id": "h1", "members": [], "levels_summary": []}

        class FakeClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def get(self, url, *, headers=None, params=None):
                captured_params.append(params or {})
                return FakeResponse()

        monkeypatch.setattr(rc, "_resolve_project_id", fake_resolve_project_id)

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())

        await get_hierarchy_preview(
            model_id="model-1",
            hierarchy_id="hier-1",
            tenant_slug="test",
            jwt_token="jwt-token",
        )

        assert len(captured_params) == 1
        assert "persona_id" not in captured_params[0]


class TestBug5424HierarchyMemberData:
    """Verify that _load_hierarchy_member_data threads persona_id to
    get_hierarchy_preview."""

    @pytest.mark.asyncio
    async def test_persona_id_forwarded_to_get_hierarchy_preview(self, monkeypatch):
        from src.dax import xmla_server

        captured_calls: list[dict] = []

        async def capturing_get_hierarchy_preview(
            model_id, hierarchy_id, tenant_slug, jwt_token,
            project_id="", *, sample_size=1000, expand_level=None,
            parent_key=None, persona_id=None, include_key_path=False,
        ):
            captured_calls.append({"persona_id": persona_id})
            return {
                "members": [{"key_value": "US", "level_name": "Region"}],
                "levels_summary": [],
            }

        monkeypatch.setattr(
            xmla_server, "get_hierarchy_preview",
            capturing_get_hierarchy_preview,
        )

        dimension = {
            "name": "Geography",
            "source": "hierarchy",
            "hierarchy_id": "h-1",
            "levels": [
                {"name": "Region", "ordinal": 0},
                {"name": "Country", "ordinal": 1},
            ],
        }

        result = await _load_hierarchy_member_data(
            model_id="model-1",
            project_id="project-1",
            dimension=dimension,
            tenant_slug="test",
            jwt_token="jwt-token",
            restrictions={},
            persona_id="persona-restricted-1",
        )

        assert len(captured_calls) == 1
        assert captured_calls[0]["persona_id"] == "persona-restricted-1"

    @pytest.mark.asyncio
    async def test_persona_id_none_when_not_provided(self, monkeypatch):
        from src.dax import xmla_server

        captured_calls: list[dict] = []

        async def capturing_get_hierarchy_preview(
            model_id, hierarchy_id, tenant_slug, jwt_token,
            project_id="", *, sample_size=1000, expand_level=None,
            parent_key=None, persona_id=None, include_key_path=False,
        ):
            captured_calls.append({"persona_id": persona_id})
            return {"members": [], "levels_summary": []}

        monkeypatch.setattr(
            xmla_server, "get_hierarchy_preview",
            capturing_get_hierarchy_preview,
        )

        dimension = {
            "name": "Geography",
            "source": "hierarchy",
            "hierarchy_id": "h-1",
            "levels": [
                {"name": "Region", "ordinal": 0},
                {"name": "Country", "ordinal": 1},
            ],
        }

        await _load_hierarchy_member_data(
            model_id="model-1",
            project_id="project-1",
            dimension=dimension,
            tenant_slug="test",
            jwt_token="jwt-token",
            restrictions={},
        )

        assert len(captured_calls) == 1
        assert captured_calls[0]["persona_id"] is None


class TestBug5424DiscoverMemberData:
    """Verify that _load_discover_member_data threads persona_id to
    _load_hierarchy_member_data for hierarchy-sourced dimensions."""

    @pytest.mark.asyncio
    async def test_persona_id_threaded_to_hierarchy_member_data(self, monkeypatch):
        from src.dax import xmla_server

        captured_calls: list[dict] = []

        async def capturing_load_hierarchy(
            *, model_id, project_id, dimension, tenant_slug,
            jwt_token, restrictions, persona_id=None,
        ):
            captured_calls.append({"persona_id": persona_id})
            return {"members": [], "levels": ["L0", "L1"], "members_by_level": {}}

        monkeypatch.setattr(
            xmla_server, "_load_hierarchy_member_data",
            capturing_load_hierarchy,
        )

        dimensions = [
            {"name": "Geography", "source": "hierarchy", "hierarchy_id": "h-1",
             "levels": [{"name": "Region"}, {"name": "Country"}]},
        ]

        result = await _load_discover_member_data(
            model_id="model-1",
            project_id="project-1",
            dimensions=dimensions,
            tenant_slug="test",
            jwt_token="jwt-token",
            restrictions={},
            persona_id="persona-restricted-1",
        )

        assert len(captured_calls) == 1
        assert captured_calls[0]["persona_id"] == "persona-restricted-1"
