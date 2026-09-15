"""Bug-9913 regression coverage for gateway burst coalescing and mappings."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jose import jwt
from starlette.requests import Request

from shared.config.settings import get_settings
from src import router_client
from src.async_singleflight import run_singleflight
from src.auth import base as auth_base
from src.auth.base import (
    SessionValidationUnavailable,
    TokenPayload,
    validate_session_upstream,
    _session_check_reset_for_tests,
)
from src.dax import credential_cache, session_store
from src.dax import xmla_server
from src.jdbc.server import (
    PGWireServer,
    SessionAuthorityUnavailableError,
)

pytestmark = pytest.mark.unit

_SETTINGS = get_settings()
_MODEL_A = "11111111-1111-1111-1111-111111111111"
_MODEL_B = "22222222-2222-2222-2222-222222222222"
_MODEL_C = "33333333-3333-3333-3333-333333333333"


def _mint(
    *,
    tenant_id: str = "acme",
    role: str = "member",
    groups: list[str] | None = None,
    persona_id: str = "persona-a",
    capabilities: list[str] | None = None,
    custom_claim: str = "same",
    iat: int | None = None,
    jti: str = "jti-a",
) -> str:
    now = int(time.time()) if iat is None else iat
    claims = {
        "sub": "user@example.com",
        "tenant_id": tenant_id,
        "role": role,
        "groups": groups or ["group-a"],
        "persona_id": persona_id,
        "capabilities": capabilities or ["read"],
        "custom_claim": custom_claim,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "jti": jti,
    }
    return jwt.encode(
        claims,
        _SETTINGS.JWT_SECRET_KEY,
        algorithm=_SETTINGS.JWT_ALGORITHM,
    )


@pytest.fixture(autouse=True)
def _reset_bug9913_state():
    _session_check_reset_for_tests()
    router_client._reset_metadata_caches_for_tests()
    credential_cache._reset_for_tests()
    yield
    _session_check_reset_for_tests()
    router_client._reset_metadata_caches_for_tests()
    credential_cache._reset_for_tests()


class _HeldLoginClient:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    failure: BaseException | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, _url, **_kwargs):
        type(self).calls += 1
        type(self).started.set()
        await type(self).release.wait()
        if type(self).failure is not None:
            raise type(self).failure
        return _LoginResponse()


class _LoginResponse:
    status_code = 200
    cookies = {"access_token": "issued-jwt"}

    def raise_for_status(self):
        return None


def _reset_held_login(*, failure: BaseException | None = None) -> None:
    _HeldLoginClient.calls = 0
    _HeldLoginClient.started = asyncio.Event()
    _HeldLoginClient.release = asyncio.Event()
    _HeldLoginClient.failure = failure


@pytest.mark.asyncio
async def test_singleflight_is_bounded_and_bypasses_at_capacity():
    registry: dict[object, asyncio.Task[str]] = {}
    started = asyncio.Event()
    release = asyncio.Event()

    async def held_loader():
        started.set()
        await release.wait()
        return "shared"

    first = asyncio.create_task(
        run_singleflight(
            registry, "first", held_loader, max_entries=1,
        )
    )
    await started.wait()
    direct_calls = 0

    async def bypass_loader():
        nonlocal direct_calls
        direct_calls += 1
        return "bypassed"

    assert await run_singleflight(
        registry, "second", bypass_loader, max_entries=1,
    ) == "bypassed"
    assert direct_calls == 1
    release.set()
    assert await first == "shared"
    await asyncio.sleep(0)
    assert registry == {}


@pytest.mark.asyncio
async def test_singleflight_sole_waiter_cancel_observes_failed_loader():
    registry: dict[object, asyncio.Task[str]] = {}
    started = asyncio.Event()
    release = asyncio.Event()
    loader_finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_contexts: list[dict] = []

    def capture_loop_exception(_loop, context):
        loop_contexts.append(context)

    async def failing_loader():
        started.set()
        await release.wait()
        try:
            raise RuntimeError("shared loader failed after cancellation")
        finally:
            loader_finished.set()

    loop.set_exception_handler(capture_loop_exception)
    try:
        waiter = asyncio.create_task(
            run_singleflight(
                registry, "sole-waiter", failing_loader, max_entries=1,
            )
        )
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        release.set()
        await loader_finished.wait()
        # The task's done callback runs on the next loop turn after the
        # failing loader completes.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert registry == {}
        assert not any(
            context.get("message") == "Task exception was never retrieved"
            for context in loop_contexts
        )
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_login_miss_is_shared_and_follower_cancellation_is_safe(monkeypatch):
    _reset_held_login()
    monkeypatch.setattr(router_client.httpx, "AsyncClient", _HeldLoginClient)

    leader = asyncio.create_task(
        router_client.login_for_token("acme", "u@example.com", "pw")
    )
    await _HeldLoginClient.started.wait()
    follower = asyncio.create_task(
        router_client.login_for_token("acme", "u@example.com", "pw")
    )
    await asyncio.sleep(0)
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    _HeldLoginClient.release.set()
    assert await leader == "issued-jwt"
    assert _HeldLoginClient.calls == 1


@pytest.mark.asyncio
async def test_login_failure_is_removed_and_retried(monkeypatch):
    _reset_held_login(failure=RuntimeError("temporary login failure"))
    monkeypatch.setattr(router_client.httpx, "AsyncClient", _HeldLoginClient)
    with pytest.raises(RuntimeError, match="temporary login failure"):
        first = asyncio.create_task(
            router_client.login_for_token("acme", "u@example.com", "pw")
        )
        await _HeldLoginClient.started.wait()
        _HeldLoginClient.release.set()
        await first

    _reset_held_login()
    with patch.object(router_client.httpx, "AsyncClient", _HeldLoginClient):
        second = asyncio.create_task(
            router_client.login_for_token("acme", "u@example.com", "pw")
        )
        await _HeldLoginClient.started.wait()
        _HeldLoginClient.release.set()
        assert await second == "issued-jwt"
    assert _HeldLoginClient.calls == 1


class _HeldSessionClient:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    response: httpx.Response | None = None
    failure: BaseException | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, *_args, **_kwargs):
        type(self).calls += 1
        type(self).started.set()
        await type(self).release.wait()
        if type(self).failure is not None:
            raise type(self).failure
        assert type(self).response is not None
        return type(self).response


def _reset_held_session(
    response: httpx.Response | None = None,
    failure: BaseException | None = None,
) -> None:
    _HeldSessionClient.calls = 0
    _HeldSessionClient.started = asyncio.Event()
    _HeldSessionClient.release = asyncio.Event()
    _HeldSessionClient.response = response
    _HeldSessionClient.failure = failure


def _response(status: int, *, retry_after: str | None = None) -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(
        status,
        headers=headers,
        request=httpx.Request("GET", "http://model-service/users/me"),
    )


@pytest.mark.asyncio
async def test_session_validation_shares_exact_token_and_caches_only_200(monkeypatch):
    _reset_held_session(_response(200))
    monkeypatch.setattr(auth_base.httpx, "AsyncClient", _HeldSessionClient)
    token = "exact-token"
    calls = asyncio.gather(
        validate_session_upstream(token),
        validate_session_upstream(token),
        validate_session_upstream(token),
    )
    await _HeldSessionClient.started.wait()
    _HeldSessionClient.release.set()
    await calls
    assert _HeldSessionClient.calls == 1
    await validate_session_upstream(token)
    assert _HeldSessionClient.calls == 1

    other = "different-exact-token"
    await validate_session_upstream(other)
    assert _HeldSessionClient.calls == 2


@pytest.mark.asyncio
async def test_session_unavailability_is_typed_non_cached_and_follower_cancel_safe(
    monkeypatch,
):
    _reset_held_session(_response(503, retry_after="999"))
    monkeypatch.setattr(auth_base.httpx, "AsyncClient", _HeldSessionClient)
    token = "unavailable-token"
    leader = asyncio.create_task(validate_session_upstream(token))
    await asyncio.wait_for(_HeldSessionClient.started.wait(), timeout=2)
    follower = asyncio.create_task(validate_session_upstream(token))
    await asyncio.sleep(0)
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(follower, timeout=2)
    _HeldSessionClient.release.set()
    with pytest.raises(SessionValidationUnavailable) as exc_info:
        await asyncio.wait_for(leader, timeout=2)
    assert exc_info.value.retry_after_seconds <= 60

    _reset_held_session(_response(200))
    _HeldSessionClient.release.set()
    await validate_session_upstream(token)
    assert _HeldSessionClient.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 302, 404, 500])
async def test_session_authority_statuses_are_typed_unavailability(
    monkeypatch, status,
):
    _reset_held_session(_response(status))
    monkeypatch.setattr(auth_base.httpx, "AsyncClient", _HeldSessionClient)
    _HeldSessionClient.release.set()
    with pytest.raises(SessionValidationUnavailable):
        await validate_session_upstream(f"status-{status}")


@pytest.mark.asyncio
async def test_metadata_shares_equivalent_tokens_and_separates_security_scopes(
    monkeypatch,
):
    calls = {"dimensions": 0, "list": 0}
    models = [
        {
            "id": _MODEL_A,
            "slug": "sales",
            "project_id": "p1",
            "project_slug": "project-one",
            "deployed_version_id": None,
        },
        {
            "id": _MODEL_B,
            "slug": "finance",
            "project_id": "p2",
            "project_slug": "project-two",
            "deployed_version_id": None,
        },
        {
            "id": _MODEL_C,
            "slug": "sales-copy",
            "project_id": "p2",
            "project_slug": "project-two",
            "deployed_version_id": None,
        },
    ]

    async def list_models(_tenant, _token):
        calls["list"] += 1
        return [dict(model) for model in models], 0

    async def dimensions(*_args, **_kwargs):
        calls["dimensions"] += 1
        return []

    async def empty_list(*_args, **_kwargs):
        return []

    async def empty_dict(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(router_client, "_list_all_models_for_tenant_uncached", list_models)
    monkeypatch.setattr(router_client, "get_model_dimensions", dimensions)
    monkeypatch.setattr(router_client, "get_model_measures", empty_list)
    monkeypatch.setattr(router_client, "get_model_personas", empty_list)
    monkeypatch.setattr(router_client, "get_model_snapshot", empty_dict)
    monkeypatch.setattr(router_client, "get_model_kpis", empty_list)

    base_iat = int(time.time()) - 1
    base = _mint(iat=base_iat, jti="one")
    rotated = _mint(iat=base_iat, jti="two")
    await asyncio.gather(
        router_client.fetch_model_metadata(
            _MODEL_A, "acme", base, project_slug="project-one", use_cache=True,
        ),
        router_client.fetch_model_metadata(
            _MODEL_A, "acme", rotated, project_slug="project-one", use_cache=True,
        ),
    )
    assert calls["dimensions"] == 1

    variants = [
        dict(role="admin"),
        dict(groups=["group-b"]),
        dict(persona_id="persona-b"),
        dict(capabilities=["write"]),
        dict(custom_claim="different"),
    ]
    for changed in variants:
        await router_client.fetch_model_metadata(
            _MODEL_A,
            "acme",
            _mint(**changed),
            project_slug="project-one",
            use_cache=True,
        )
    await router_client.fetch_model_metadata(
        _MODEL_A, "other-tenant", base, project_slug="project-one", use_cache=True,
    )
    await router_client.fetch_model_metadata(
        _MODEL_B, "acme", base, project_slug="project-two", use_cache=True,
    )
    await router_client.fetch_model_metadata(
        _MODEL_C, "acme", base, project_slug="project-two", use_cache=True,
    )
    assert calls["dimensions"] == 1 + len(variants) + 3

    # CLS refreshes deliberately bypass both completed and in-flight startup
    # work, even when the principal is otherwise equivalent.
    await router_client.fetch_model_metadata(
        _MODEL_A, "acme", base, project_slug="project-one", use_cache=False,
    )
    assert calls["dimensions"] == 1 + len(variants) + 4


@pytest.mark.asyncio
async def test_metadata_failure_is_not_cached_or_shared_after_terminal_failure(
    monkeypatch,
):
    calls = 0

    async def list_models(_tenant, _token):
        return [{
            "id": _MODEL_A,
            "slug": "sales",
            "project_id": "p1",
            "project_slug": "project-one",
            "deployed_version_id": None,
        }], 0

    async def dimensions(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("metadata authority unavailable")

    monkeypatch.setattr(router_client, "_list_all_models_for_tenant_uncached", list_models)
    monkeypatch.setattr(router_client, "get_model_dimensions", dimensions)
    for name, value in {
        "get_model_measures": lambda *_a, **_k: [],
        "get_model_personas": lambda *_a, **_k: [],
        "get_model_snapshot": lambda *_a, **_k: {},
        "get_model_kpis": lambda *_a, **_k: [],
    }.items():
        async def _value(*_args, _result=value, **_kwargs):
            return _result
        monkeypatch.setattr(router_client, name, _value)

    for _ in range(2):
        with pytest.raises(router_client.ModelMetadataUnavailable):
            await router_client.fetch_model_metadata(
                _MODEL_A, "acme", _mint(), project_slug="project-one", use_cache=True,
            )
    assert calls == 2


def _soap_request(body: bytes, *, state: dict | None = None, path: str = "/xmla") -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
        "state": state or {},
    }
    return Request(scope, receive)


_SESSION_SOAP = (
    b'<Envelope xmlns:tns="urn:schemas-microsoft-com:xml-analysis">'
    b'<Header><tns:Session SessionId="sid-9913"/></Header>'
    b"<Body><Discover/></Body></Envelope>"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("server_level", [True, False])
async def test_xmla_unavailable_is_503_with_retry_and_retains_session(
    monkeypatch, server_level,
):
    delete = AsyncMock()
    monkeypatch.setattr(xmla_server, "verify_jwt_token", lambda _token: None)
    monkeypatch.setattr(
        xmla_server,
        "validate_session_upstream",
        AsyncMock(side_effect=SessionValidationUnavailable(23)),
    )
    monkeypatch.setattr(session_store, "get", AsyncMock(return_value="jwt"))
    monkeypatch.setattr(session_store, "delete", delete)
    request = _soap_request(
        _SESSION_SOAP,
        state={"jwt_token": "jwt", "username": "user@example.com"},
    )
    if server_level:
        response = await xmla_server.xmla_server_endpoint(request)
    else:
        response = await xmla_server._handle_xmla_request(
            "acme", "user@example.com", "jwt", _SESSION_SOAP, request,
        )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "23"
    assert b"Session authority is temporarily unavailable" in response.body
    delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("server_level", [True, False])
async def test_xmla_rejected_session_is_401_and_deletes_resumed_session(
    monkeypatch, server_level,
):
    delete = AsyncMock()
    monkeypatch.setattr(xmla_server, "verify_jwt_token", lambda _token: None)
    monkeypatch.setattr(
        xmla_server,
        "validate_session_upstream",
        AsyncMock(side_effect=ValueError("Session has been revoked")),
    )
    monkeypatch.setattr(session_store, "get", AsyncMock(return_value="jwt"))
    monkeypatch.setattr(session_store, "delete", delete)
    request = _soap_request(
        _SESSION_SOAP,
        state={"jwt_token": "jwt", "username": "user@example.com"},
    )
    if server_level:
        response = await xmla_server.xmla_server_endpoint(request)
    else:
        response = await xmla_server._handle_xmla_request(
            "acme", "user@example.com", "jwt", _SESSION_SOAP, request,
        )
    assert response.status_code == 401
    delete.assert_awaited_once_with("sid-9913")


class _JDBCWriter:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        return None


def _governor():
    governor = MagicMock()
    governor.record_auth_success = MagicMock()
    governor.record_auth_failure = MagicMock()
    return governor


def _jdbc_metadata_result():
    return (
        ["modely"],
        {"modely": []},
        {"modely": _MODEL_A},
        {},
        {},
        {"modely": None},
        {"modely": False},
        {"modely": "modely"},
        {"modely": []},
        {"modely": None},
        set(),
        {"modely": "project1"},
    )


@pytest.mark.asyncio
async def test_jdbc_ready_precedes_full_metadata_fanout(monkeypatch):
    """The 10-second connect budget excludes the full catalogue build."""
    server = PGWireServer()
    server._tenant_slug = "acme"
    server._project_hint = "project1"
    server._model_id = "modely"
    server._jwt_token = "jwt"
    server._tls_active = True
    writer = _JDBCWriter()
    metadata_started = asyncio.Event()
    release_metadata = asyncio.Event()

    async def held_metadata(*_args, **_kwargs):
        metadata_started.set()
        await release_metadata.wait()
        return _jdbc_metadata_result()

    model = {
        "id": _MODEL_A,
        "slug": "modely",
        "project_slug": "project1",
    }
    with patch(
        "src.jdbc.server.proto.read_startup",
        new=AsyncMock(return_value={
            "type": "startup",
            "params": {"database": "acme/project1/modely", "user": "u@example.com"},
        }),
    ), patch.object(
        server, "_authenticate", new=AsyncMock(return_value=True),
    ), patch(
        "src.jdbc.server.list_all_models_for_tenant",
        new=AsyncMock(return_value=[model]),
    ), patch(
        "src.jdbc.server.tenant_listing_degraded", return_value=0,
    ), patch(
        "src.jdbc.server.fetch_model_metadata", new=held_metadata,
    ), patch(
        "src.jdbc.server.CatalogueDB", return_value=MagicMock(),
    ), patch.object(
        server, "_query_loop", new=AsyncMock(),
    ) as query_loop:
        task = asyncio.create_task(
            server._run(asyncio.StreamReader(), writer)
        )
        await asyncio.wait_for(metadata_started.wait(), timeout=2)
        assert b"Z" in writer.buf
        assert not task.done()
        release_metadata.set()
        await asyncio.wait_for(task, timeout=2)

    assert server._model_id == _MODEL_A
    query_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_jdbc_invalid_model_still_fails_before_ready():
    server = PGWireServer()
    server._tenant_slug = "acme"
    server._project_hint = "project1"
    server._model_id = "missing"
    server._jwt_token = "jwt"
    server._tls_active = True
    writer = _JDBCWriter()

    with patch(
        "src.jdbc.server.proto.read_startup",
        new=AsyncMock(return_value={
            "type": "startup",
            "params": {"database": "acme/project1/missing", "user": "u@example.com"},
        }),
    ), patch.object(
        server, "_authenticate", new=AsyncMock(return_value=True),
    ), patch(
        "src.jdbc.server.list_all_models_for_tenant",
        new=AsyncMock(return_value=[{
            "id": _MODEL_A,
            "slug": "modely",
            "project_slug": "project1",
        }]),
    ), patch(
        "src.jdbc.server.tenant_listing_degraded", return_value=0,
    ), patch(
        "src.jdbc.server.fetch_model_metadata", new=AsyncMock(),
    ) as fetch_metadata:
        await server._run(asyncio.StreamReader(), writer)

    assert b"3D000" in writer.buf
    assert b"Z" not in writer.buf
    fetch_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_jdbc_connect_unavailable_is_08006_without_bad_auth_count(monkeypatch):
    server = PGWireServer()
    governor = _governor()
    with patch("src.jdbc.server.get_governor", return_value=governor), \
            patch("src.jdbc.server.proto.read_password_message", new=AsyncMock(return_value="pw")), \
            patch("src.jdbc.server.login_for_token", new=AsyncMock(return_value="jwt")), \
            patch("src.jdbc.server.verify_jwt_token", return_value=TokenPayload("u", "acme", 0)), \
            patch(
                "src.jdbc.server.validate_session_upstream",
                new=AsyncMock(side_effect=SessionValidationUnavailable(19)),
            ):
        writer = _JDBCWriter()
        assert await server._authenticate(
            {"database": "acme", "user": "u@example.com"},
            asyncio.StreamReader(),
            writer,
        ) is False
    assert b"08006" in writer.buf
    assert b"28000" not in writer.buf
    governor.record_auth_failure.assert_not_called()


def _login_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://model-service/api/v1/auth/login")
    return httpx.HTTPStatusError(
        f"login returned HTTP {status}",
        request=request,
        response=httpx.Response(status, request=request),
    )


def _login_failure(kind: str) -> BaseException:
    request = httpx.Request("POST", "http://model-service/api/v1/auth/login")
    if kind == "transport":
        return httpx.ConnectError("authority unavailable", request=request)
    if kind == "timeout":
        return httpx.ReadTimeout("authority timed out", request=request)
    if kind == "server-error":
        return _login_status_error(503)
    if kind == "rate-limit":
        return _login_status_error(429)
    if kind == "protocol":
        return router_client.LoginProtocolError("missing access token")
    if kind == "internal":
        return RuntimeError("unexpected login implementation failure")
    raise AssertionError(f"unknown login failure kind: {kind}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_sqlstate"),
    [
        ("transport", "08006"),
        ("timeout", "08006"),
        ("server-error", "08006"),
        ("protocol", "08006"),
        ("internal", "08006"),
        ("rate-limit", "53300"),
    ],
)
async def test_jdbc_login_noncredential_failures_preserve_state(
    failure_kind, expected_sqlstate,
):
    server = PGWireServer()
    governor = _governor()
    credential_cache.put(
        "u@example.com", "pw", "other-tenant-jwt", scope="other-tenant",
    )
    failure = _login_failure(failure_kind)
    with patch("src.jdbc.server.get_governor", return_value=governor), \
            patch("src.jdbc.server.proto.read_password_message", new=AsyncMock(return_value="pw")), \
            patch("src.jdbc.server.login_for_token", new=AsyncMock(side_effect=failure)):
        writer = _JDBCWriter()
        assert await server._authenticate(
            {"database": "acme", "user": "u@example.com"},
            asyncio.StreamReader(),
            writer,
        ) is False

    assert expected_sqlstate.encode() in writer.buf
    if expected_sqlstate != "28000":
        assert b"28000" not in writer.buf
    assert b"model-service" not in writer.buf
    governor.record_auth_failure.assert_not_called()
    assert credential_cache.get(
        "u@example.com", "pw", scope="other-tenant",
    ) == "other-tenant-jwt"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_jdbc_login_rejection_invalidates_and_counts_once(status):
    server = PGWireServer()
    governor = _governor()
    credential_cache.put(
        "u@example.com", "old-pw", "stale-acme-jwt", scope="acme",
    )
    credential_cache.put(
        "u@example.com", "pw", "other-tenant-jwt", scope="other-tenant",
    )
    with patch("src.jdbc.server.get_governor", return_value=governor), \
            patch("src.jdbc.server.proto.read_password_message", new=AsyncMock(return_value="pw")), \
            patch(
                "src.jdbc.server.login_for_token",
                new=AsyncMock(side_effect=_login_status_error(status)),
            ):
        writer = _JDBCWriter()
        assert await server._authenticate(
            {"database": "acme", "user": "u@example.com"},
            asyncio.StreamReader(),
            writer,
        ) is False

    assert b"28000" in writer.buf
    assert b"08006" not in writer.buf
    assert b"model-service" not in writer.buf
    governor.record_auth_failure.assert_called_once_with(server._peer_ip)
    assert credential_cache.get(
        "u@example.com", "old-pw", scope="acme",
    ) is None
    assert credential_cache.get(
        "u@example.com", "pw", scope="other-tenant",
    ) == "other-tenant-jwt"


@pytest.mark.asyncio
async def test_jdbc_revoked_session_remains_28000_and_closes(monkeypatch):
    server = PGWireServer()
    governor = _governor()
    with patch("src.jdbc.server.get_governor", return_value=governor), \
            patch("src.jdbc.server.proto.read_password_message", new=AsyncMock(return_value="pw")), \
            patch("src.jdbc.server.login_for_token", new=AsyncMock(return_value="jwt")), \
            patch("src.jdbc.server.verify_jwt_token", return_value=TokenPayload("u", "acme", 0)), \
            patch(
                "src.jdbc.server.validate_session_upstream",
                new=AsyncMock(side_effect=ValueError("Session has been revoked")),
            ):
        writer = _JDBCWriter()
        assert await server._authenticate(
            {"database": "acme", "user": "u@example.com"},
            asyncio.StreamReader(),
            writer,
        ) is False
    assert b"28000" in writer.buf
    governor.record_auth_failure.assert_called_once()


@pytest.mark.asyncio
async def test_jdbc_long_lived_revalidation_maps_unavailable_to_08006():
    server = PGWireServer()
    server._jwt_token = "jwt"
    server._resolve_model_id_and_variant = lambda _sql: (_MODEL_A, False, None)
    writer = _JDBCWriter()
    with patch(
        "src.jdbc.server.validate_session_upstream",
        new=AsyncMock(side_effect=SessionValidationUnavailable(11)),
    ), patch("src.jdbc.server.execute_query", new=AsyncMock()) as execute:
        with pytest.raises(SessionAuthorityUnavailableError):
            await server._handle_user_query("SELECT amount FROM sales", writer)
    assert b"08006" in writer.buf
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_jdbc_credential_cache_is_scoped_by_tenant(monkeypatch):
    calls = []

    async def login(tenant, _email, _password):
        calls.append(tenant)
        return f"jwt-{tenant}"

    async def session_ok(_token):
        return None

    with patch("src.jdbc.server.get_governor", side_effect=[_governor(), _governor()]), \
            patch("src.jdbc.server.proto.read_password_message", new=AsyncMock(return_value="pw")), \
            patch("src.jdbc.server.login_for_token", new=login), \
            patch("src.jdbc.server.verify_jwt_token", side_effect=lambda token: TokenPayload("u", token.removeprefix("jwt-"), 0)), \
            patch("src.jdbc.server.validate_session_upstream", new=session_ok):
        for tenant in ("acme", "other"):
            server = PGWireServer()
            writer = _JDBCWriter()
            assert await server._authenticate(
                {"database": tenant, "user": "u@example.com"},
                asyncio.StreamReader(),
                writer,
            ) is True
    assert calls == ["acme", "other"]


def test_credential_cache_scope_keeps_entries_and_invalidation_separate():
    credential_cache.put("u", "pw", "jwt-a", scope="tenant-a")
    credential_cache.put("u", "pw", "jwt-b", scope="tenant-b")
    assert credential_cache.get("u", "pw", scope="tenant-a") == "jwt-a"
    assert credential_cache.get("u", "pw", scope="tenant-b") == "jwt-b"
    credential_cache.invalidate("u", scope="tenant-a")
    assert credential_cache.get("u", "pw", scope="tenant-a") is None
    assert credential_cache.get("u", "pw", scope="tenant-b") == "jwt-b"
