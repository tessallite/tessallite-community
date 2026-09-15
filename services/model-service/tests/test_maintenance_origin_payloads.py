from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _response():
    response = MagicMock(status_code=200)
    response.json.return_value = {"rows": [], "columns": []}
    return response


def _client_patch(module_name: str):
    client = MagicMock()
    client.post = AsyncMock(return_value=_response())
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    target = "httpx.AsyncClient" if module_name == "httpx" else f"{module_name}.httpx.AsyncClient"
    return patch(target, MagicMock(return_value=context)), client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "function_name", "args"),
    [
        ("src.api.pockets", "_route_query", (uuid4(), "SELECT 1", "token")),
        ("src.api.named_sets", "_execute_via_router", (uuid4(), "SELECT 1", "token")),
        (
            "src.api.row_security",
            "_run_probe_as_principal",
            (uuid4(), "SELECT 1", None, MagicMock(user_identity="u", roles=set(), groups=set(), claims={}), "token"),
        ),
    ],
)
async def test_model_service_maintenance_execute_producers_stamp_origin(
    module_name, function_name, args,
):
    module = __import__(module_name, fromlist=[function_name])
    patcher, client = _client_patch(module_name) if module_name != "src.api.row_security" else _client_patch("httpx")
    with patcher:
        if module_name == "src.api.row_security":
            await getattr(module, function_name)(
                model_id=args[0], probe_query=args[1], persona_id=args[2],
                principal=args[3], bearer=args[4],
            )
        else:
            await getattr(module, function_name)(*args)
    payload = client.post.await_args.kwargs["json"]
    assert payload["client_kind"] == "maintenance"


@pytest.mark.asyncio
async def test_pocket_refresh_execute_producer_stamps_origin():
    from shared.pocket import refresh

    patcher, client = _client_patch("shared.pocket.refresh")
    with patcher:
        await refresh._execute_via_router(uuid4(), "SELECT 1", "token")
    assert client.post.await_args.kwargs["json"]["client_kind"] == "maintenance"
