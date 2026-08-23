"""Bug-6665: XMLA drill-options FAILURE must emit a SOAP warning.

Previously, _fetch_drill_options swallowed QueryRouterError into [] and
hierarchy_id=None, silently degrading to leaf-detail drill. The AMBIGUITY
case (Bug-4153) already emits a warning, but the FAILURE case stayed silent.

Now _fetch_drill_options returns (hierarchies, warning_text_or_None), and
handle_drillthrough attaches the warning to the SOAP response.
"""
from __future__ import annotations

import pytest

from src.router_client import QueryRouterError
from src.dax.drillthrough_handler import _fetch_drill_options


# Minimal mock for execute_drill_options
class _MockModule:
    """Provides a fake execute_drill_options for testing."""
    pass


@pytest.mark.asyncio
async def test_drill_options_failure_returns_warning(monkeypatch):
    """A QueryRouterError produces an empty list AND a warning string."""

    async def _fail(*a, **k):
        raise QueryRouterError("drill-options route not found", status_code=404)

    import src.dax.drillthrough_handler as dth
    monkeypatch.setattr(dth, "execute_drill_options", _fail)

    hierarchies, warning = await _fetch_drill_options("m1", [], "jwt")
    assert hierarchies == []
    assert warning is not None
    assert "Drill-through hierarchy resolution failed" in warning
    assert "Falling back to leaf-detail drill" in warning


@pytest.mark.asyncio
async def test_drill_options_success_no_warning(monkeypatch):
    """A successful fetch returns hierarchies and no warning."""

    async def _ok(*a, **k):
        return {"hierarchies": [{"hierarchy_id": "h1", "hierarchy_name": "Date"}]}

    import src.dax.drillthrough_handler as dth
    monkeypatch.setattr(dth, "execute_drill_options", _ok)

    hierarchies, warning = await _fetch_drill_options("m1", [], "jwt")
    assert len(hierarchies) == 1
    assert hierarchies[0]["hierarchy_id"] == "h1"
    assert warning is None
