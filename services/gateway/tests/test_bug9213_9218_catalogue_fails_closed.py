"""Bug-9213 / Bug-9218 — a metadata FAILURE must never be served as an ABSENCE.

The gateway's catalogue builder answered every upstream failure by returning
less: an empty model list, a dropped project, a dropped model. Consumers then
reported the absence as fact, which produced two different user-visible lies
from one root cause:

  * Bug-9213 — a JDBC client connecting to a tenant got a catalogue with NO
    TABLES when the model-service errored. "This tenant has no models" is a
    silent lie; the client has no way to tell it from the truth.
  * Bug-9218 — a client naming a model got ``FATAL Unknown model`` for a model
    that exists and is deployed, because the project owning it failed to list.
    The reported asymmetry ("JDBC cannot open modell while the query-router
    BigQuery execute path works") is exactly this: the query-router resolves the
    model directly and never goes through these calls.

Both now raise ``ModelMetadataUnavailable`` and the connection reports a service
fault (SQLSTATE 08006), which a BI user can act on.

Execution scope: isolated. Gate tier: T1 (fail-closed contract).
"""
from __future__ import annotations



import pytest

from src import router_client
from src.jdbc.server import PGWireServer
from src.router_client import ModelMetadataUnavailable

MODEL_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _clear_caches():
    router_client._reset_metadata_caches_for_tests()
    yield
    router_client._reset_metadata_caches_for_tests()


# ---------------------------------------------------------------------------
# Bug-9213 — the tenant-wide failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_listing_failure_raises_instead_of_an_empty_catalogue(
    monkeypatch,
):
    """Pre-fix this returned twelve empty containers, so a browsing client was
    served a catalogue with no tables and no error."""

    async def _boom(tenant_slug, jwt_token):
        raise RuntimeError("model-service 500")

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _boom)

    with pytest.raises(ModelMetadataUnavailable):
        await router_client.fetch_model_metadata(None, "acme", "jwt")


@pytest.mark.asyncio
async def test_connection_setup_reports_a_service_fault_not_an_empty_catalogue(
    monkeypatch,
):
    """The user-visible half: startup must FAIL, not hand over a blank catalogue.

    Asserted on the emitted protocol bytes — a FATAL ErrorResponse carrying
    SQLSTATE 08006 — not on an internal call.
    """
    server = PGWireServer()
    server._tenant_slug = "acme"
    server._jwt_token = "jwt"
    server._model_id = None
    # Past the F-001-08 require-TLS gate, which is not what this test examines.
    server._tls_active = True

    async def _unavailable(*_a, **_kw):
        raise ModelMetadataUnavailable("model-service unreachable")

    monkeypatch.setattr("src.jdbc.server.fetch_model_metadata", _unavailable)

    written = bytearray()

    class _W:
        def write(self, data):
            written.extend(data)

        async def drain(self):
            return None

        def close(self):
            return None

        def get_extra_info(self, _name, default=None):
            return ("127.0.0.1", 55000)

    async def _authed(*_a, **_kw):
        return True

    monkeypatch.setattr(server, "_authenticate", _authed)
    monkeypatch.setattr(
        "src.jdbc.protocol.read_startup",
        _make_startup_reader({"database": "acme", "user": "u@acme.test"}),
    )

    # ``_run`` is the startup/auth/metadata sequence inside ``handle_client``;
    # driving it directly keeps the test on the seam under examination rather
    # than the per-IP admission governor.
    await server._run(_NullReader(), _W())

    assert written[:1] == b"E", "expected an ErrorResponse, got %r" % bytes(written[:1])
    payload = bytes(written)
    assert b"08006" in payload, "expected SQLSTATE 08006 (connection failure)"
    assert b"FATAL" in payload
    assert b"Unknown model" not in payload, (
        "a metadata fault must not be reported as a missing model"
    )
    # And no ReadyForQuery: the connection never came up with a blank catalogue.
    assert b"Z" not in payload[-6:], "startup completed despite the fault"


# ---------------------------------------------------------------------------
# Bug-9218 — the per-project / per-model failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_degraded_listing_refuses_to_resolve_a_named_model(monkeypatch):
    """A dropped project makes a real model look absent. When ONE model was
    named, that ambiguity must be reported, not resolved as "unknown"."""

    async def _partial(tenant_slug, jwt_token):
        # The listing succeeded for one project and silently lost another.
        return [], 1

    monkeypatch.setattr(
        router_client, "_list_all_models_for_tenant_uncached", _partial
    )

    with pytest.raises(ModelMetadataUnavailable) as exc:
        await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    assert "incomplete" in str(exc.value)


@pytest.mark.asyncio
async def test_named_model_metadata_failure_survives_per_model_cleanup(monkeypatch):
    """Bug-9218: a dimensions/measures failure must not be swallowed by the
    per-model cleanup catch and reported as ``Unknown model``."""

    async def _complete(_tenant_slug, _jwt_token):
        return [
            {
                "id": MODEL_ID,
                "slug": "modely",
                "project_id": "p1",
                "project_slug": "project1",
                "deployed_version_id": None,
            }
        ], 0

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("dimensions service unavailable")

    monkeypatch.setattr(
        router_client, "_list_all_models_for_tenant_uncached", _complete
    )
    monkeypatch.setattr(router_client, "get_model_dimensions", _boom)

    with pytest.raises(ModelMetadataUnavailable) as exc:
        await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    assert "could not be fetched" in str(exc.value)


@pytest.mark.asyncio
async def test_a_degraded_listing_still_degrades_gracefully_for_discovery(
    monkeypatch,
):
    """F-013-12 is preserved: one flaky project must not blank every BI catalog.

    Broad discovery (no model named) still returns what resolved. This is the
    boundary that makes the fix a narrowing, not a blanket hard-fail.
    """

    async def _partial(tenant_slug, jwt_token):
        return [], 1

    monkeypatch.setattr(
        router_client, "_list_all_models_for_tenant_uncached", _partial
    )

    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    assert result[0] == []  # no models resolved, but no exception


@pytest.mark.asyncio
async def test_a_complete_listing_that_finds_no_model_is_not_a_fault(monkeypatch):
    """The genuine "no such model" case must still be a plain empty result, so
    the connection can report ``Unknown model`` honestly."""

    async def _complete(tenant_slug, jwt_token):
        return [], 0

    monkeypatch.setattr(
        router_client, "_list_all_models_for_tenant_uncached", _complete
    )

    result = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    assert result[0] == []


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _NullReader:
    async def readexactly(self, n):  # pragma: no cover - never reached
        raise EOFError

    async def read(self, n=-1):  # pragma: no cover
        return b""


def _make_startup_reader(params):
    async def _read_startup(_reader):
        return {"type": "startup", "params": params}

    return _read_startup
