"""
Shared fixtures for gateway tests.

sys.path configured via [tool.pytest.ini_options] pythonpath in pyproject.toml:
  - "."  → tessallite/services/gateway/   (enables "from src.xxx import")
  - "../../" → tessallite/               (NOT used by gateway tests — no shared DB models needed)
"""
import pytest
from shared.config.fastapi_drift import check_fastapi_version_drift

check_fastapi_version_drift()


@pytest.fixture(autouse=True)
def _local_dev_transport_posture(monkeypatch):
    """The gateway test suite runs as a LOCAL-DEV posture (Wave C #2).

    TLS is required by default and ``start_jdbc_server`` refuses to bind a listener
    with password auth + TLS disabled (``validate_transport_security``). The test
    process has no TLS certs, so it sets the explicit local-dev opt-out — exactly
    as a developer's ``.env`` does — so tests that start the real JDBC listener
    run. The transport-security gate itself is exercised with its own settings in
    test_wave_c_tls_required.py.
    """
    from shared.config.settings import get_settings
    monkeypatch.setattr(
        get_settings(), "GATEWAY_ALLOW_INSECURE_TRANSPORT", True, raising=False,
    )
    yield


@pytest.fixture(autouse=True)
def _reset_session_check_cache():
    """Isolate the process-global upstream session-validation cache (G2).

    ``src.auth.base._session_check_cache`` records, per JWT, the last time the
    gateway confirmed the session was live with model-service (TTL-cached, 30s).
    It is a module global, so without a reset a token validated by one test is
    served from cache to the next — producing order-dependent results once JDBC
    query paths began calling ``validate_session_upstream`` per query (grok
    F-001-01). Clearing before AND after each test keeps every test hermetic.
    """
    resetters = []
    for name in ("src.auth.base", "auth.base"):
        try:
            mod = __import__(name, fromlist=["_session_check_reset_for_tests"])
            resetters.append(mod._session_check_reset_for_tests)
        except Exception:
            continue
    for reset in resetters:
        reset()
    yield
    for reset in resetters:
        reset()


@pytest.fixture(autouse=True)
def _default_session_revalidation_passthrough(request):
    """Default the JDBC per-query session revalidation to a live-session
    pass-through (G2 / grok F-001-01).

    The JDBC query paths now re-validate the session upstream before dispatch.
    Gateway tests that drive those paths with a synthetic ``_jwt_token`` and no
    live model-service are testing query behavior (routing, shaping, protocol),
    not revocation, so upstream revalidation would otherwise fail-closed and
    mask what they assert. Tests that specifically exercise revocation patch
    ``src.jdbc.server.validate_session_upstream`` themselves, which overrides
    this default; tests calling ``src.auth.base.validate_session_upstream``
    directly are unaffected (this only rebinds the name imported into
    ``src.jdbc.server``). Opt out with ``@pytest.mark.no_session_passthrough``.
    """
    if request.node.get_closest_marker("no_session_passthrough"):
        yield
        return

    async def _passthrough(token):
        return None

    # The suite imports the JDBC server module under two names — ``src.jdbc.server``
    # and ``jdbc.server`` (pythonpath exposes both ``.`` and ``../../``). Those are
    # distinct module objects with independent ``validate_session_upstream``
    # bindings, so patch every one that is importable to keep the pass-through
    # effective regardless of which alias a test used.
    modules = []
    for name in ("src.jdbc.server", "jdbc.server"):
        try:
            mod = __import__(name, fromlist=["validate_session_upstream"])
        except Exception:
            continue
        modules.append(mod)

    saved = [(m, m.validate_session_upstream) for m in modules]
    for m in modules:
        m.validate_session_upstream = _passthrough  # type: ignore[assignment]
    try:
        yield
    finally:
        for m, original in saved:
            m.validate_session_upstream = original  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _default_execute_named_set_fetch(request):
    """Default the Execute-time named-set fetch to "this model has no named sets".

    ``_handle_execute`` fetches the model's saved named sets so it can inline
    them into the MDX before axis extraction, and Bug-7254 made that fetch fail
    CLOSED. Gateway tests that drive Execute with a synthetic ``jwt_token`` and
    no live model-service are testing MDX translation, protocol shaping or
    re-query behaviour — not named-set inlining — so before this fixture the
    unstubbed fetch issued a REAL outbound HTTP request. That passes on a
    developer box that can resolve the model-service host and fails with
    ``httpx.ConnectError: [Errno -3] Temporary failure in name resolution`` on
    every CI runner, which is exactly how a coding-tier suite ended up
    requiring live services (forbidden by the test strategy).

    Tests that DO exercise inlining monkeypatch
    ``xmla_server.get_model_named_sets`` themselves; their patch is applied
    after this fixture and therefore wins. Opt out entirely with
    ``@pytest.mark.no_named_set_stub``.
    """
    if request.node.get_closest_marker("no_named_set_stub"):
        yield
        return

    try:
        from src.dax import xmla_server
    except Exception:
        yield
        return

    async def _no_named_sets(*_args, **_kwargs):
        return []

    original = xmla_server.get_model_named_sets
    xmla_server.get_model_named_sets = _no_named_sets  # type: ignore[assignment]
    try:
        yield
    finally:
        xmla_server.get_model_named_sets = original  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _reset_xmla_member_cache():
    """Isolate the process-global XMLA member/metadata caches (Bug-6602).

    ``src.dax.member_cache`` holds short-TTL member + metadata results keyed by
    (tenant, model, persona, principal, ...). Many gateway tests reuse the same
    tenant/model ids with DIFFERENT monkeypatched metadata, so without a reset an
    entry cached by one test would be served to the next (stale, order-dependent
    failures). Clearing before AND after every test keeps each hermetic.
    """
    try:
        from src.dax import member_cache
    except Exception:
        yield
        return
    member_cache._reset_for_tests()
    yield
    member_cache._reset_for_tests()
