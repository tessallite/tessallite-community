"""Phase 2: model-service create-cap enforcement glue.

Verifies the default-OFF no-op (full product unchanged) and the ON path
(Community) blocking over-cap creates. Uses a real signed license + key registry.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src import licensing_guard
from shared.licensing.issuer.sign import generate_ed25519_keypair, sign_license


@pytest.fixture(autouse=True)
def _issuer_context(monkeypatch):
    """This module builds real signed licences via ``sign_license``, which is
    fail-closed to a sanctioned issuer context (Bug-6547). Scope the marker to THIS
    file only — the rest of the model-service suite runs guard-live, so an accidental
    in-product mint would fail loudly there rather than being masked."""
    monkeypatch.setenv("TESSALLITE_LICENSE_ISSUER", "1")


async def _load_license_from_file(monkeypatch, lf):
    """Populate the license manager from a signed license FILE.

    After the License-Manager refactor, ``get_license_manager()`` no longer auto-loads;
    the manager is (re)built by ``reload_license_manager()``, which reads
    ``load_license_doc_from_db()`` (system DB first, file fallback). Unit tests have no
    system DB, so mock that loader to return the file's doc and trigger the reload.
    Pass ``lf=None`` for the no-license case.
    """
    doc = json.loads(open(lf, encoding="utf-8").read()) if lf else None
    monkeypatch.setattr(
        licensing_guard, "load_license_doc_from_db", AsyncMock(return_value=doc)
    )
    await licensing_guard.reload_license_manager()


def _settings(**kw):
    base = dict(
        LICENSE_ENFORCEMENT_ENABLED=False, LICENSE_FILE="", LICENSE_PUBLIC_KEYS=""
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _community_license_file(tmp_path, models=2, demo_tenant_id=None):
    priv, pub = generate_ed25519_keypair()
    doc = {
        "schema_version": 1,
        "license_id": "lic_community_1",
        "key_id": "k1",
        "issuer": "tessallite.io",
        "edition": "community",
        "issued_at": "2026-06-22T00:00:00Z",
        "expires_at": None,
        "product": "tessallite-community",
        "entitlements": {
            "own_tenants": 1,
            "models": models,
            "users": 2,
            "features": "all",
        },
    }
    if demo_tenant_id:
        doc["entitlements"]["demo_tenant"] = {"enabled": True, "tenant_id": demo_tenant_id}
    signed = sign_license(doc, priv)
    path = tmp_path / "license.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    pub_spec = "k1:" + base64.b64encode(pub).decode("ascii")
    return str(path), pub_spec


@pytest.fixture(autouse=True)
def _clear_cache():
    # licensing_guard caches the manager in a module global (_MANAGER) with an async
    # reload_license_manager(); reset that singleton between tests. (Was .cache_clear()
    # from the old @lru_cache impl, which broke after the License-Manager refactor.)
    licensing_guard._MANAGER = None
    licensing_guard._MANAGER_LOADED_AT = 0.0
    yield
    licensing_guard._MANAGER = None
    licensing_guard._MANAGER_LOADED_AT = 0.0


async def _count(n: int) -> int:
    return n


async def test_enforcement_off_is_noop(monkeypatch):
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    queried = {"called": False}

    async def count_fn() -> int:
        queried["called"] = True
        return 999

    # no raise even though count would be way over any cap
    await licensing_guard.enforce_create_cap("model", count_fn)
    # count not even queried when enforcement is off
    assert queried["called"] is False
    assert licensing_guard.get_license_manager().status()["edition"] == "internal-unlimited"


async def test_enforcement_off_is_not_enterprise(monkeypatch):
    """F-031-02: the hatch must never report edition=enterprise."""
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    status = licensing_guard.get_license_manager().status()
    assert status["edition"] != "enterprise"
    assert status["edition"] == "internal-unlimited"
    assert status["activated"] is False


async def test_enforcement_on_allows_under_cap(tmp_path, monkeypatch):
    lf, pk = _community_license_file(tmp_path, models=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )
    await _load_license_from_file(monkeypatch, lf)
    # 0 and 1 existing models -> allowed (cap 2)
    await licensing_guard.enforce_create_cap("model", lambda: _count(0))
    await licensing_guard.enforce_create_cap("model", lambda: _count(1))


async def test_enforcement_on_blocks_at_cap(tmp_path, monkeypatch):
    lf, pk = _community_license_file(tmp_path, models=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )
    await _load_license_from_file(monkeypatch, lf)
    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_create_cap("model", lambda: _count(2))
    assert exc.value.status_code == 403
    assert "limit reached" in str(exc.value.detail).lower()


async def test_unactivated_when_enabled_without_license(monkeypatch):
    # enforcement ON but no license -> fail-CLOSED: the unactivated manager DENIES creates
    # (Bug-5496 fix). Not installing a licence must never grant the full product.
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True),
    )
    await _load_license_from_file(monkeypatch, None)
    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_create_cap("project", lambda: _count(0))
    assert exc.value.status_code == 403


async def test_no_license_enforcement_on_is_unactivated_not_unlimited(monkeypatch):
    # Regression for Bug-5496 (the breach): enforcement ON + no licence must NOT fall back
    # to the full product. The manager is the fail-closed unactivated stub that denies every
    # create — so caps can't be bypassed by simply never installing a licence.
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True),
    )
    await _load_license_from_file(monkeypatch, None)
    mgr = licensing_guard.get_license_manager()
    assert mgr.status()["activated"] is False
    assert mgr.can_create("model", 0).allowed is False
    assert mgr.can_create("user", 0).allowed is False
    assert mgr.can_create("project", 0).allowed is False


def test_no_license_enforcement_off_is_full_product(monkeypatch):
    # Counterpart: enforcement OFF + no licence stays the full product (dev/internal).
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    mgr = licensing_guard.get_license_manager()
    assert mgr.can_create("model", 999).allowed is True


async def test_db_lookup_error_falls_back_to_file(tmp_path, monkeypatch):
    # Regression for Bug-5485: if the system-DB licence lookup raises (e.g. the schema
    # is not migrated yet — on K8s the migration runs as a post-install Job AFTER the
    # pods start), load_license_doc_from_db must SWALLOW it and fall through to the
    # file. Otherwise a file-mounted Community licence never loads and the manager is
    # stuck unactivated, denying every create even with a valid licence installed.
    lf, pk = _community_license_file(tmp_path, models=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("relation \"system_settings\" does not exist")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(licensing_guard, "get_system_db", _boom)
    doc = await licensing_guard.load_license_doc_from_db()
    assert doc is not None and doc.get("license_id"), "file licence must load when the DB errors"

    # end to end: reload from that file -> activated manager that enforces (not unactivated)
    await licensing_guard.reload_license_manager()
    assert licensing_guard.get_license_manager().status().get("activated") is True


async def test_installed_but_untrusted_license_reports_invalid_not_unactivated(
    tmp_path, monkeypatch
):
    """Bug-6437: a licence IS installed but its signature cannot be verified
    (tampered / untrusted / expired key). The manager must report a DISTINCT
    ``invalid`` state — not the generic ``unactivated`` one that reads as "no
    licence was ever installed" — while still failing closed."""
    lf, _pk = _community_license_file(tmp_path, models=2)
    # Verify with the WRONG public key so the stored licence's signature is
    # untrusted — the closed verifier rejects it.
    _, wrong_pub = generate_ed25519_keypair()
    wrong_pk = "k1:" + base64.b64encode(wrong_pub).decode("ascii")
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=wrong_pk
        ),
    )
    await _load_license_from_file(monkeypatch, lf)
    mgr = licensing_guard.get_license_manager()
    st = mgr.status()
    assert st["activated"] is False
    # The distinguishing signal: an installed-but-invalid licence, not "no licence".
    assert st.get("license_state") == "invalid"
    assert st.get("edition") == "community"
    # Bug-8164 (F02): the SPECIFIC machine-readable taxonomy code is carried end to
    # end (an untrusted signature -> "invalid_signature"), not flattened away.
    assert st.get("error_code") == "invalid_signature"
    # Still fail-closed on every capped create.
    decision = mgr.can_create("model", 0)
    assert decision.allowed is False
    assert "expired or untrusted" in decision.reason


async def test_expired_stored_licence_surfaces_license_expired_code_end_to_end(
    tmp_path, monkeypatch
):
    """Bug-8164 (F02) end-to-end: a persisted licence that is EXPIRED at load time is
    rejected by the real closed verifier (LicenseExpired) and its stable
    ``error_code`` ("license_expired") survives through load_manager ->
    UnactivatedManager.status() -> _InvalidLicenseManager to the operator status."""
    priv, pub = generate_ed25519_keypair()
    doc = {
        "schema_version": 1,
        "license_id": "lic_community_exp",
        "key_id": "k1",
        "issuer": "tessallite.io",
        "edition": "community",
        "issued_at": "2020-01-01T00:00:00Z",
        "expires_at": "2020-06-01T00:00:00Z",  # long past
        "product": "tessallite-community",
        "entitlements": {"own_tenants": 1, "models": 2, "users": 2, "features": "all"},
    }
    signed = sign_license(doc, priv)
    lf = tmp_path / "expired.json"
    lf.write_text(json.dumps(signed), encoding="utf-8")
    pk = "k1:" + base64.b64encode(pub).decode("ascii")
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=str(lf), LICENSE_PUBLIC_KEYS=pk
        ),
    )
    await _load_license_from_file(monkeypatch, str(lf))
    st = licensing_guard.get_license_manager().status()
    assert st["activated"] is False
    assert st.get("error_code") == "license_expired"


@pytest.mark.parametrize(
    "code",
    ["malformed_license", "invalid_signature", "unknown_key_id",
     "unsupported_algorithm", "license_expired"],
)
async def test_rejected_licence_code_propagates_through_invalid_manager(
    monkeypatch, code
):
    """Bug-8164 (F02) propagation: for EVERY taxonomy member, a rejected stored
    licence's error_code must survive load_manager's result/status through
    _InvalidLicenseManager to the operator-facing status — not be flattened to a
    bare "invalid"."""
    from shared.licensing.manager import UnactivatedManager

    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True),
    )
    monkeypatch.setattr(
        licensing_guard, "load_license_doc_from_db",
        AsyncMock(return_value={"license_id": "L", "edition": "community"}),
    )
    # Real stub carrying the rejected-licence code (what load_manager now returns).
    monkeypatch.setattr(
        licensing_guard, "load_manager",
        lambda **kw: UnactivatedManager(license_error_code=code),
    )
    monkeypatch.setattr(licensing_guard, "build_registry", lambda keys: {"r": keys})
    await licensing_guard.reload_license_manager()
    mgr = licensing_guard.get_license_manager()
    st = mgr.status()
    assert st["activated"] is False
    assert st.get("license_state") == "invalid"
    assert st.get("error_code") == code
    # entitlements carry it too (producer/consumer alignment), still fail-closed
    assert mgr.entitlements().get("error_code") == code
    assert mgr.can_create("model", 0).allowed is False


async def test_present_but_broken_closed_build_preserves_manager_load_failed(
    monkeypatch
):
    """Bug-7466 (F03): when the closed manager is present-but-broken, load_manager
    returns license_state "manager_load_failed" with a load_error. model-service
    must PRESERVE that distinct build-fault state, not flatten it to generic
    "invalid" — an operator must be able to tell a broken build from a bad licence."""
    from shared.licensing.manager import UnactivatedManager

    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True),
    )
    monkeypatch.setattr(
        licensing_guard, "load_license_doc_from_db",
        AsyncMock(return_value={"license_id": "L", "edition": "community"}),
    )
    monkeypatch.setattr(
        licensing_guard, "load_manager",
        lambda **kw: UnactivatedManager(load_error="import failed: RuntimeError"),
    )
    monkeypatch.setattr(licensing_guard, "build_registry", lambda keys: {"r": keys})
    await licensing_guard.reload_license_manager()
    mgr = licensing_guard.get_license_manager()
    st = mgr.status()
    assert st["activated"] is False
    assert st.get("license_state") == "manager_load_failed"
    assert st.get("load_error") == "import failed: RuntimeError"
    # fail-closed, and the reason names the build fault (not "expired or untrusted")
    decision = mgr.can_create("model", 0)
    assert decision.allowed is False
    assert "build/packaging fault" in decision.reason


async def test_stale_manager_reloads_new_license_across_replicas(tmp_path, monkeypatch):
    """Bug-6436: the per-process manager is cached; a licence installed on
    another Cloud Run replica must be picked up once the local cache passes its
    TTL. The async enforcement path reloads a stale cache before deciding."""
    # Replica starts with NO licence (fail-closed unactivated).
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True),
    )
    monkeypatch.setattr(
        licensing_guard, "load_license_doc_from_db", AsyncMock(return_value=None)
    )
    await licensing_guard.reload_license_manager()
    assert licensing_guard.get_license_manager().status()["activated"] is False

    # A valid licence is now installed (by another replica); the local cache is
    # still the old unactivated manager but is now older than the TTL.
    lf, pk = _community_license_file(tmp_path, models=2)
    doc = json.loads(open(lf, encoding="utf-8").read())
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    monkeypatch.setattr(
        licensing_guard, "load_license_doc_from_db", AsyncMock(return_value=doc)
    )
    # Force staleness so the next enforcement call reloads.
    licensing_guard._MANAGER_LOADED_AT = 0.0

    # Under-cap create is now allowed because the stale cache reloaded the
    # freshly-installed licence instead of serving the old unactivated manager.
    await licensing_guard.enforce_create_cap("model", lambda: _count(0))
    assert licensing_guard.get_license_manager().status()["activated"] is True


async def test_fresh_manager_not_reloaded_within_ttl(tmp_path, monkeypatch):
    """Bug-6436: a manager loaded within the TTL is NOT reloaded on every call —
    the DB read only happens once the cache goes stale."""
    lf, pk = _community_license_file(tmp_path, models=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    loader = AsyncMock(return_value=json.loads(open(lf, encoding="utf-8").read()))
    monkeypatch.setattr(licensing_guard, "load_license_doc_from_db", loader)
    await licensing_guard.reload_license_manager()
    calls_after_reload = loader.call_count
    # Two enforcement calls immediately after a fresh reload must not re-read.
    await licensing_guard.enforce_create_cap("model", lambda: _count(0))
    await licensing_guard.enforce_create_cap("model", lambda: _count(1))
    assert loader.call_count == calls_after_reload


# ---------------------------------------------------------------------------
# Bug-6476: operational revocation (model-service side) — clear_license_doc
# ---------------------------------------------------------------------------


async def test_clear_license_doc_removes_and_reloads(monkeypatch):
    """Bug-6476: an operator has an operable path to remove an installed licence.
    clear_license_doc deletes the persisted document, reloads the manager, and
    reports that a row was removed."""
    class _Result:
        rowcount = 1

    class _DB:
        def __init__(self):
            self.committed = False

        async def execute(self, *_a, **_k):
            return _Result()

        async def commit(self):
            self.committed = True

    db = _DB()

    async def _fake_system_db():
        yield db

    monkeypatch.setattr(licensing_guard, "get_system_db", _fake_system_db)
    reload_mock = AsyncMock()
    monkeypatch.setattr(licensing_guard, "reload_license_manager", reload_mock)

    removed = await licensing_guard.clear_license_doc()

    assert removed is True
    assert db.committed is True
    reload_mock.assert_awaited_once()


async def test_clear_license_doc_noop_when_absent(monkeypatch):
    """Returns False (and still reloads) when there was no licence to remove."""
    class _Result:
        rowcount = 0

    class _DB:
        async def execute(self, *_a, **_k):
            return _Result()

        async def commit(self):
            return None

    async def _fake_system_db():
        yield _DB()

    monkeypatch.setattr(licensing_guard, "get_system_db", _fake_system_db)
    reload_mock = AsyncMock()
    monkeypatch.setattr(licensing_guard, "reload_license_manager", reload_mock)

    removed = await licensing_guard.clear_license_doc()

    assert removed is False
    reload_mock.assert_awaited_once()


async def test_demo_source_locked_blocks_demo_tenant(tmp_path, monkeypatch):
    lf, pk = _community_license_file(tmp_path, demo_tenant_id="demo-123")
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )
    await _load_license_from_file(monkeypatch, lf)
    with pytest.raises(HTTPException) as exc:
        licensing_guard.enforce_demo_source_locked("demo-123")
    assert exc.value.status_code == 403
    # the user's own tenant is unaffected
    licensing_guard.enforce_demo_source_locked("own-456")


def test_demo_source_lock_noop_when_enforcement_off(monkeypatch):
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    monkeypatch.delenv("DEMO_SOURCE_LOCKED_TENANTS", raising=False)
    licensing_guard.enforce_demo_source_locked("demo-123")  # no raise when off


# ---------------------------------------------------------------------------
# Bug-6529: DEMO_SOURCE_LOCKED_TENANTS env var (enforcement-independent)
# ---------------------------------------------------------------------------


def test_demo_lock_env_blocks_listed_tenant_enforcement_off(monkeypatch):
    """Bug-6529: DEMO_SOURCE_LOCKED_TENANTS blocks the listed tenant even
    when LICENSE_ENFORCEMENT_ENABLED is False (the hosted-demo config)."""
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", "acme-demo")
    with pytest.raises(HTTPException) as exc:
        licensing_guard.enforce_demo_source_locked("acme-demo")
    assert exc.value.status_code == 403
    assert "fixed and read-only" in str(exc.value.detail)


def test_demo_lock_env_allows_unlisted_tenant(monkeypatch):
    """Bug-6529: DEMO_SOURCE_LOCKED_TENANTS does NOT block a tenant that
    is not in the list -- own tenants are unaffected."""
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", "acme-demo")
    # Should not raise for a different tenant.
    licensing_guard.enforce_demo_source_locked("customer-tenant")


def test_demo_lock_env_multiple_tenants(monkeypatch):
    """Bug-6529: comma-separated list with multiple tenant IDs."""
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", "demo-a, demo-b, demo-c")
    with pytest.raises(HTTPException):
        licensing_guard.enforce_demo_source_locked("demo-b")
    # Unlisted tenant is fine.
    licensing_guard.enforce_demo_source_locked("real-tenant")


def test_demo_lock_env_empty_string_is_noop(monkeypatch):
    """Bug-6529: empty DEMO_SOURCE_LOCKED_TENANTS is a no-op (same as unset)."""
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", "")
    licensing_guard.enforce_demo_source_locked("acme-demo")  # no raise


def test_demo_lock_env_overrides_enforcement_off_for_demo(monkeypatch):
    """Bug-6529: even when enforcement is off AND no license is installed,
    the env var still blocks the demo tenant."""
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", "acme-demo")
    # Verify enforcement is off.
    assert not _settings().LICENSE_ENFORCEMENT_ENABLED
    with pytest.raises(HTTPException) as exc:
        licensing_guard.enforce_demo_source_locked("acme-demo")
    assert exc.value.status_code == 403


async def test_demo_lock_env_and_license_both_fire(tmp_path, monkeypatch):
    """Bug-6529: when both the env var AND the license classify a tenant as
    demo, the env var fires first (short-circuit) -- the end result is the
    same 403."""
    lf, pk = _community_license_file(tmp_path, demo_tenant_id="acme-demo")
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk),
    )
    monkeypatch.setenv("DEMO_SOURCE_LOCKED_TENANTS", "acme-demo")
    await _load_license_from_file(monkeypatch, lf)
    with pytest.raises(HTTPException) as exc:
        licensing_guard.enforce_demo_source_locked("acme-demo")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Bug-7468: enforce_import_model_cap — batch import cap enforcement
# ---------------------------------------------------------------------------


async def test_import_cap_enforcement_off_is_noop(monkeypatch):
    """Bug-7468: enforcement OFF skips the cap check entirely (full product)."""
    monkeypatch.setattr(licensing_guard, "get_settings", lambda: _settings())
    queried = {"called": False}

    async def count_fn() -> int:
        queried["called"] = True
        return 999

    # No raise even though importing 100 models would be way over any cap.
    await licensing_guard.enforce_import_model_cap(100, count_fn)
    assert queried["called"] is False


async def test_import_cap_allows_batch_within_limit(tmp_path, monkeypatch):
    """Bug-7468: importing N models when current + N <= cap is allowed."""
    lf, pk = _community_license_file(tmp_path, models=5)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    await _load_license_from_file(monkeypatch, lf)
    # 2 existing + 3 to import = 5 total, at the cap of 5 -> allowed
    await licensing_guard.enforce_import_model_cap(3, lambda: _count(2))
    # 0 existing + 5 to import = 5 total -> allowed
    await licensing_guard.enforce_import_model_cap(5, lambda: _count(0))
    # 4 existing + 1 to import = 5 total -> allowed
    await licensing_guard.enforce_import_model_cap(1, lambda: _count(4))


async def test_import_cap_blocks_batch_exceeding_limit(tmp_path, monkeypatch):
    """Bug-7468: importing N models when current + N > cap is rejected."""
    lf, pk = _community_license_file(tmp_path, models=3)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    await _load_license_from_file(monkeypatch, lf)
    # 2 existing + 2 to import = 4 total, cap is 3 -> rejected
    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_import_model_cap(2, lambda: _count(2))
    assert exc.value.status_code == 403
    assert "exceed" in str(exc.value.detail).lower() or "limit" in str(exc.value.detail).lower()


async def test_import_cap_blocks_single_over_cap(tmp_path, monkeypatch):
    """Bug-7468: even importing 1 model is blocked when already at the cap."""
    lf, pk = _community_license_file(tmp_path, models=2)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    await _load_license_from_file(monkeypatch, lf)
    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_import_model_cap(1, lambda: _count(2))
    assert exc.value.status_code == 403


async def test_import_cap_zero_models_is_noop(tmp_path, monkeypatch):
    """Bug-7468: importing 0 models skips the cap check (no-op)."""
    lf, pk = _community_license_file(tmp_path, models=1)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    await _load_license_from_file(monkeypatch, lf)
    queried = {"called": False}

    async def count_fn() -> int:
        queried["called"] = True
        return 999

    # 0 models to import -> early return, count not queried
    await licensing_guard.enforce_import_model_cap(0, count_fn)
    assert queried["called"] is False


# ---------------------------------------------------------------------------
# Bug-6567: enforce_import_model_cap uses the same advisory lock as
# enforce_create_cap for "model" — imports and direct creates serialise.
# ---------------------------------------------------------------------------


async def test_import_cap_acquires_advisory_lock(tmp_path, monkeypatch):
    """Bug-6567: enforce_import_model_cap must acquire the same advisory lock
    as enforce_create_cap('model') when db is provided, so concurrent imports
    (or an import racing a direct create) cannot exceed the model cap."""
    import zlib
    from unittest.mock import AsyncMock as AM, MagicMock, call
    from sqlalchemy import text

    lf, pk = _community_license_file(tmp_path, models=5)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    await _load_license_from_file(monkeypatch, lf)

    # Track which SQL statements the mock db receives.
    executed_stmts: list[str] = []
    count_result = MagicMock()
    count_result.scalar.return_value = 2  # under cap

    async def _fake_execute(stmt):
        executed_stmts.append(str(stmt))
        return count_result

    db = MagicMock()
    db.execute = _fake_execute

    await licensing_guard.enforce_import_model_cap(1, lambda: _count(2), db=db)

    # The advisory lock SQL must have been executed BEFORE the count query.
    expected_key = zlib.crc32("tessallite_cap_model".encode()) & 0x7FFFFFFF
    assert any(
        f"pg_advisory_xact_lock({expected_key})" in s for s in executed_stmts
    ), f"Advisory lock not acquired; executed: {executed_stmts}"


async def test_import_and_create_share_lock_key(tmp_path, monkeypatch):
    """Bug-6567: the advisory lock key for enforce_import_model_cap and
    enforce_create_cap('model') must be identical so they serialise."""
    import zlib
    from unittest.mock import MagicMock

    lf, pk = _community_license_file(tmp_path, models=5)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    await _load_license_from_file(monkeypatch, lf)

    # Capture the SQL from each path.
    import_stmts: list[str] = []
    create_stmts: list[str] = []

    count_result = MagicMock()
    count_result.scalar.return_value = 0

    async def _exec_import(stmt):
        import_stmts.append(str(stmt))
        return count_result

    async def _exec_create(stmt):
        create_stmts.append(str(stmt))
        return count_result

    db_import = MagicMock()
    db_import.execute = _exec_import

    db_create = MagicMock()
    db_create.execute = _exec_create

    await licensing_guard.enforce_import_model_cap(1, lambda: _count(0), db=db_import)
    await licensing_guard.enforce_create_cap("model", lambda: _count(0), db=db_create)

    # Extract lock key from each.
    def _extract_lock_key(stmts):
        for s in stmts:
            if "pg_advisory_xact_lock" in s:
                return s
        return None

    import_lock = _extract_lock_key(import_stmts)
    create_lock = _extract_lock_key(create_stmts)

    assert import_lock is not None, "import path did not acquire lock"
    assert create_lock is not None, "create path did not acquire lock"
    assert import_lock == create_lock, (
        f"Lock keys differ: import={import_lock!r} vs create={create_lock!r}"
    )


async def test_import_cap_blocks_at_cap_with_db(tmp_path, monkeypatch):
    """Bug-6567: enforce_import_model_cap with db still correctly blocks."""
    from unittest.mock import MagicMock

    lf, pk = _community_license_file(tmp_path, models=3)
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE=lf, LICENSE_PUBLIC_KEYS=pk
        ),
    )
    await _load_license_from_file(monkeypatch, lf)

    count_result = MagicMock()
    count_result.scalar.return_value = 3

    async def _fake_execute(stmt):
        return count_result

    db = MagicMock()
    db.execute = _fake_execute

    with pytest.raises(HTTPException) as exc:
        await licensing_guard.enforce_import_model_cap(1, lambda: _count(3), db=db)
    assert exc.value.status_code == 403


def test_vendor_key_cannot_be_replaced(monkeypatch):
    """F-031-24: LICENSE_PUBLIC_KEYS cannot replace the built-in vendor key_id."""
    attacker_pub = base64.b64encode(b"\x01" * 32).decode("ascii")
    extra = f"tessallite-prod-2026:{attacker_pub}"
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(LICENSE_PUBLIC_KEYS=extra),
    )
    spec = licensing_guard.license_public_keys()
    assert spec.startswith(licensing_guard.BUILTIN_PUBLIC_KEYS)
    from shared.licensing.loader import build_registry
    reg = build_registry(spec)
    vendor_id = licensing_guard.BUILTIN_PUBLIC_KEYS.split(":", 1)[0]
    assert vendor_id in reg


async def test_can_create_denies_when_expired(tmp_path, monkeypatch):
    """F-031-13: an expired licence cannot create even under remaining numeric cap."""
    priv, pub = generate_ed25519_keypair()
    doc = {
        "schema_version": 1,
        "license_id": "lic_expired",
        "key_id": "k1",
        "issuer": "tessallite.io",
        "edition": "community",
        "issued_at": "2020-01-01T00:00:00Z",
        "expires_at": "2020-01-02T00:00:00Z",
        "product": "tessallite-community",
        "entitlements": {
            "own_tenants": 1,
            "models": 10,
            "users": 10,
            "features": "all",
        },
    }
    signed = sign_license(doc, priv)
    path = tmp_path / "expired.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    pk = "k1:" + base64.b64encode(pub).decode("ascii")
    monkeypatch.setattr(
        licensing_guard,
        "get_settings",
        lambda: _settings(
            LICENSE_ENFORCEMENT_ENABLED=True,
            LICENSE_FILE=str(path),
            LICENSE_PUBLIC_KEYS=pk,
        ),
    )
    await _load_license_from_file(monkeypatch, str(path))
    mgr = licensing_guard.get_license_manager()
    decision = mgr.can_create("model", 0)
    assert decision.allowed is False
    assert "expired" in decision.reason.lower()
