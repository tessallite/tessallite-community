"""ML17 / F-018-13: deprecated named sets must not reach BI clients, and
certified sets carry a governance marker in MDSCHEMA_SETS."""
import pytest

from src.dax.mdschema import _rows_sets


def test_rows_sets_marks_certified_in_description():
    rows = _rows_sets(
        "Cat",
        [
            {"name": "A", "certification_status": "certified", "description": "Top sellers"},
            {"name": "B", "certification_status": "draft", "description": "Working set"},
            {"name": "C", "certification_status": "shared", "description": ""},
        ],
    )
    by_name = {r["SET_NAME"]: r for r in rows}
    assert by_name["A"]["SET_DESCRIPTION"].startswith("[Certified]")
    assert "Top sellers" in by_name["A"]["SET_DESCRIPTION"]
    # Drafts carry no marker.
    assert "[Certified]" not in by_name["B"]["SET_DESCRIPTION"]
    # Bug-6264 (authority named_sets.py:238-241): "shared" is certified-
    # equivalent and MUST carry the marker, even with an empty base description.
    assert by_name["C"]["SET_DESCRIPTION"].startswith("[Certified]")


@pytest.mark.asyncio
async def test_get_model_named_sets_excludes_deprecated(monkeypatch):
    """The gateway client drops deprecated sets so MDSCHEMA_SETS never lists a
    set an admin retired (F-018-13)."""
    from src import router_client

    payload = [
        {"name": "Live", "certification_status": "certified"},
        {"name": "Old", "certification_status": "deprecated"},
        {"name": "Wip", "certification_status": "draft"},
    ]

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(router_client.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        router_client, "_resolve_project_id", lambda *a, **k: _coro("p")
    )

    out = await router_client.get_model_named_sets("m", "tenant", "jwt", project_id="p")
    names = {ns["name"] for ns in out}
    assert "Live" in names
    assert "Wip" in names
    assert "Old" not in names  # deprecated filtered out


async def _coro(v):
    return v
