"""System-admin License Manager — upload/verify/persist to the system DB (Bug-5466).

The license is fed from the UI, verified with the built-in public key, and stored
in the system DB (no file/env), so it applies immediately and works on read-only
hosts. These tests cover the endpoint behaviour and the manager-load fallback.

Run from tessallite/services/model-service/:
    pytest tests/test_admin_license.py
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from shared.licensing.errors import InvalidSignature
from src.api import admin as mod
from src import licensing_guard as guard

pytestmark = pytest.mark.unit


def _admin():
    from src.auth.middleware import CurrentUser
    return CurrentUser(user_id="root", tenant_id="__system__", email="root@x", role="system_admin")


@pytest.fixture
def patched(monkeypatch):
    mgr = MagicMock()
    mgr.status.return_value = {"edition": "enterprise", "activated": True}
    mgr.entitlements.return_value = {"users": 50}
    monkeypatch.setattr(mod, "get_license_manager", MagicMock(return_value=mgr))
    monkeypatch.setattr(mod, "build_registry", lambda keys: {"registry": keys})
    monkeypatch.setattr(mod, "has_installed_license", AsyncMock(return_value=True))
    monkeypatch.setattr(mod, "store_license_doc", AsyncMock())
    monkeypatch.setattr(mod, "license_public_keys", lambda: "k:v")
    monkeypatch.setattr(mod, "reload_license_manager", AsyncMock())
    mock_sys = AsyncMock()
    mock_sys.commit = AsyncMock()
    mock_sys.add = MagicMock()
    mock_sys.flush = AsyncMock()

    async def _sys_db():
        yield mock_sys

    monkeypatch.setattr(mod, "get_system_db", lambda: _sys_db())
    monkeypatch.setattr(mod, "system_audit", AsyncMock())
    return mgr


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_install_rejects_invalid_signature(monkeypatch, patched):
    def _raise(*a, **k):
        raise InvalidSignature("does not verify")
    monkeypatch.setattr(mod, "verify_license", _raise)
    with pytest.raises(HTTPException) as exc:
        await mod.install_license(body={"bad": 1}, current_user=_admin())
    assert exc.value.status_code == 400
    # Bug-8164: the reject detail is now STRUCTURED { error_code, message }, not a
    # bare string — a consumer branches on the stable token, not on prose.
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["error_code"] == "invalid_signature"
    assert "rejected" in exc.value.detail["message"].lower()
    mod.store_license_doc.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_cls, expected_code",
    [
        ("MalformedLicense", "malformed_license"),
        ("InvalidSignature", "invalid_signature"),
        ("UnknownKeyId", "unknown_key_id"),
        ("UnsupportedAlgorithm", "unsupported_algorithm"),
        ("LicenseExpired", "license_expired"),
    ],
)
async def test_install_reject_returns_structured_code_per_taxonomy_member(
    monkeypatch, patched, exc_cls, expected_code
):
    """Bug-8164 (F02) API contract: EACH taxonomy member surfaces its stable
    error_code in the 400 detail, so the admin UI / automation can branch on the
    machine-readable token rather than parse the human message."""
    import shared.licensing.errors as errs

    def _raise(*a, **k):
        raise getattr(errs, exc_cls)("boom")

    monkeypatch.setattr(mod, "verify_license", _raise)
    with pytest.raises(HTTPException) as exc:
        await mod.install_license(body={"bad": 1}, current_user=_admin())
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == expected_code
    assert exc.value.detail["message"]  # non-empty human message alongside the code
    mod.store_license_doc.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifespan_fails_fast_on_malformed_beacon_key(monkeypatch):
    """Bug-8374 (F01) boundary: a malformed BEACON_ENC_KEY must ABORT model-service
    startup. The lifespan must NOT swallow BeaconConfigError — previously the check
    returned a string that startup logged-and-ignored, then booted and silently lost
    every beacon."""
    import src.main as app_main
    from shared.licensing.beacon import BeaconConfigError
    from src import licensing_guard as guard

    async def _noop(*a, **k):
        return None

    # Neutralize the DB-touching startup steps that run before the beacon check.
    monkeypatch.setattr(app_main, "refresh_system_snapshot", _noop, raising=True)
    monkeypatch.setattr(guard, "reload_license_manager", AsyncMock(), raising=True)
    monkeypatch.setenv("BEACON_ENC_KEY", "not-a-valid-fernet-key")

    with pytest.raises(BeaconConfigError):
        async with app_main.lifespan(app_main.app):
            pass  # pragma: no cover — startup must raise before yielding


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, expected_code",
    [
        ("expired", "license_expired"),
        ("bad_signature", "invalid_signature"),
        ("unknown_key", "unknown_key_id"),
        ("malformed", "malformed_license"),
    ],
)
async def test_l21_r1_f02_persisted_rejection_reaches_startup_admin_status(
    monkeypatch, kind, expected_code
):
    """Bug-8164 (F02) END-TO-END through STARTUP: a persisted licence that the real
    closed verifier rejects must surface its stable machine-readable error_code in
    GET /admin/license status AFTER the model-service lifespan builds the manager —
    not a generic "invalid". Drives a REAL malformed/expired/unknown-key/bad-signature
    document through the lifespan, then reads the operator status endpoint."""
    import base64
    from types import SimpleNamespace

    import src.main as app_main
    from src import licensing_guard as guard
    from src.api import admin as admin_mod
    from shared.licensing.issuer.sign import generate_ed25519_keypair, sign_license

    monkeypatch.setenv("TESSALLITE_LICENSE_ISSUER", "1")  # sign_license fail-closed guard
    priv, pub = generate_ed25519_keypair()
    pk = "k1:" + base64.b64encode(pub).decode("ascii")
    doc = {
        "schema_version": 1,
        "license_id": "lic_persisted",
        "key_id": "k1",
        "issuer": "tessallite.io",
        "edition": "community",
        "issued_at": "2026-06-22T00:00:00Z",
        "expires_at": None,
        "product": "tessallite-community",
        "entitlements": {"own_tenants": 1, "models": 2, "users": 2, "features": "all"},
    }
    if kind == "expired":
        persisted = sign_license({**doc, "expires_at": "2020-01-01T00:00:00Z"}, priv)
    elif kind == "bad_signature":
        _, wrong_pub = generate_ed25519_keypair()  # registry holds a DIFFERENT key
        pk = "k1:" + base64.b64encode(wrong_pub).decode("ascii")
        persisted = sign_license(doc, priv)
    elif kind == "unknown_key":
        persisted = sign_license({**doc, "key_id": "k-not-registered"}, priv)
    else:  # malformed: a required field is missing -> MalformedLicense at verify
        bad = {**doc}
        del bad["entitlements"]
        persisted = bad

    async def _noop(*a, **k):
        return None

    settings = SimpleNamespace(
        LICENSE_ENFORCEMENT_ENABLED=True, LICENSE_FILE="", LICENSE_PUBLIC_KEYS=pk
    )
    monkeypatch.setattr(app_main, "refresh_system_snapshot", _noop, raising=True)
    monkeypatch.setattr(guard, "get_settings", lambda: settings, raising=True)
    monkeypatch.setattr(
        guard, "load_license_doc_from_db", AsyncMock(return_value=persisted), raising=True
    )
    # get_license_status() also reports has_license; keep it off the DB in-test.
    monkeypatch.setattr(admin_mod, "has_installed_license", AsyncMock(return_value=True))
    # Neutralize the shutdown pool-close so exiting the lifespan is a clean no-op.
    import shared.source_pool as _sp
    monkeypatch.setattr(_sp, "close_all_pools", _noop, raising=True)
    guard._MANAGER = None
    guard._MANAGER_LOADED_AT = 0.0
    monkeypatch.delenv("BEACON_ENC_KEY", raising=False)

    async with app_main.lifespan(app_main.app):
        out = await admin_mod.get_license_status()

    assert out["status"]["activated"] is False
    assert out["status"]["error_code"] == expected_code, (
        f"persisted {kind} licence must surface {expected_code!r}, got "
        f"{out['status'].get('error_code')!r}"
    )


@pytest.mark.asyncio
async def test_install_happy_path_persists_to_db(monkeypatch, patched):
    monkeypatch.setattr(mod, "verify_license", lambda *a, **k: types.SimpleNamespace(license_id="L-OK"))
    body = {"license_id": "L-OK", "edition": "enterprise", "signature": "ed25519:abc"}

    out = await mod.install_license(body=body, current_user=_admin())

    assert out["status"] == "installed"
    # Persisted to the system DB (not a file), with the installer recorded.
    # Bug-9301: store and audit share the caller session (`db=`).
    mod.store_license_doc.assert_awaited_once()
    _args, _kwargs = mod.store_license_doc.await_args
    assert _args[0] == body
    assert _kwargs["installed_by"] == "root@x"
    assert _kwargs["db"] is not None
    assert out["license"]["edition"] == "enterprise"
    assert out["license"]["has_license"] is True


@pytest.mark.asyncio
async def test_status_reports_edition_and_install_state(monkeypatch, patched):
    out = await mod.get_license_status()
    assert out["edition"] == "enterprise"
    assert out["has_license"] is True
    assert "enforcement_enabled" in out


# ---------------------------------------------------------------------------
# Uninstall endpoint (Bug-6476: operational revocation, model-service side)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uninstall_removes_db_license(monkeypatch, patched):
    """A DB-installed licence is removed and reported as removed."""
    monkeypatch.setattr(mod, "clear_license_doc", AsyncMock(return_value=True))
    monkeypatch.setattr(mod, "load_license_doc_from_db", AsyncMock(return_value=None))

    out = await mod.uninstall_license(current_user=_admin())

    assert out["status"] == "removed"
    assert "message" not in out
    mod.clear_license_doc.assert_awaited_once()


@pytest.mark.asyncio
async def test_uninstall_reports_file_license_still_present(monkeypatch, patched):
    """Bug-6476 / Fable finding: a LICENSE_FILE-mounted licence is reloaded on
    the manager reload even after the DB document is deleted. The response must
    NOT misleadingly report the licence as gone — it says file_license_present
    and carries a remediation message directing the admin to license.status."""
    monkeypatch.setattr(mod, "clear_license_doc", AsyncMock(return_value=False))
    monkeypatch.setattr(
        mod,
        "load_license_doc_from_db",
        AsyncMock(return_value={"license_id": "file-lic", "edition": "community"}),
    )

    out = await mod.uninstall_license(current_user=_admin())

    assert out["status"] == "file_license_present"
    assert "message" in out and "LICENSE_FILE" in out["message"]


@pytest.mark.asyncio
async def test_uninstall_no_license_present(monkeypatch, patched):
    """No DB doc and no file licence -> nothing to remove."""
    monkeypatch.setattr(mod, "clear_license_doc", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "load_license_doc_from_db", AsyncMock(return_value=None))

    out = await mod.uninstall_license(current_user=_admin())

    assert out["status"] == "no_license"
    assert "message" not in out


# ---------------------------------------------------------------------------
# Bug-6547: the admin licence surface is UPLOAD-only — no generate/issue route
# ---------------------------------------------------------------------------

def test_admin_license_surface_is_upload_only():
    """The system-admin licence API exposes status (GET), upload (POST) and
    uninstall (DELETE) only. It must NOT expose any route that MINTS a licence
    (issue/generate/sign/keygen) — generation lives in the Tessallite service
    (community) and the product-control-center (corporate)."""
    from src.api.admin import router

    license_routes = {
        (tuple(sorted(m for m in r.methods if m not in {"HEAD", "OPTIONS"})), r.path)
        for r in router.routes
        if r.path.endswith("/license") or "/license/" in r.path
    }
    methods = {m for methods, _ in license_routes for m in methods}
    # Exactly the upload-only surface: read + apply-uploaded + remove.
    assert methods == {"GET", "POST", "DELETE"}
    # No route path advertises a generation/minting verb.
    forbidden = ("issue", "generate", "sign", "keygen", "mint", "create")
    for _methods, path in license_routes:
        low = path.lower()
        assert not any(tok in low for tok in forbidden), f"minting route exposed: {path}"


# ---------------------------------------------------------------------------
# licensing_guard: built-in key + no-license fallback
# ---------------------------------------------------------------------------

def test_built_in_public_key_used_when_env_unset(monkeypatch):
    from shared.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEYS", "")
    assert guard.license_public_keys() == guard.BUILTIN_PUBLIC_KEYS


def test_env_public_key_appends_to_vendor_key(monkeypatch):
    """F-031-24: LICENSE_PUBLIC_KEYS cannot replace the built-in vendor key."""
    from shared.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "LICENSE_PUBLIC_KEYS", "custom:key")
    assert guard.license_public_keys().startswith(guard.BUILTIN_PUBLIC_KEYS)
    assert "custom:key" in guard.license_public_keys()


@pytest.mark.asyncio
async def test_reload_with_no_license_is_internal_unlimited(monkeypatch):
    monkeypatch.setattr(guard, "load_license_doc_from_db", AsyncMock(return_value=None))
    mgr = await guard.reload_license_manager()
    assert mgr.status().get("edition") == "internal-unlimited"
    assert mgr.can_create("model", 999).allowed is True


@pytest.mark.asyncio
async def test_reload_with_license_builds_from_doc(monkeypatch):
    doc = {"license_id": "L1", "edition": "community"}
    monkeypatch.setattr(guard, "load_license_doc_from_db", AsyncMock(return_value=doc))
    built = MagicMock()
    built.status.return_value = {"edition": "community"}
    monkeypatch.setattr(guard, "load_manager", lambda **kw: built)
    monkeypatch.setattr(guard, "build_registry", lambda keys: {"r": keys})
    mgr = await guard.reload_license_manager()
    assert mgr.status().get("edition") == "community"


@pytest.mark.asyncio
async def test_bug_9301_install_license_audit_failure_leaves_document_absent(
    monkeypatch, patched
):
    """Bug-9301: system_audit failure must roll back license.document.

    store_license_doc and system_audit share the caller session; commit happens
    only after audit succeeds. If audit raises, the upsert is never committed
    so license.document is absent.
    """
    monkeypatch.setattr(
        mod, "verify_license",
        lambda *a, **k: types.SimpleNamespace(license_id="L-OK", edition="enterprise"),
    )
    monkeypatch.setattr(mod, "store_license_doc", guard.store_license_doc)
    monkeypatch.setattr(mod, "reload_license_manager", AsyncMock())
    monkeypatch.setattr(mod, "emit_webhook", AsyncMock())
    monkeypatch.setattr(
        mod, "system_audit", AsyncMock(side_effect=RuntimeError("audit boom"))
    )

    session = AsyncMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()

    async def _sys_db():
        yield session

    monkeypatch.setattr(mod, "get_system_db", lambda: _sys_db())

    with pytest.raises(RuntimeError, match="audit boom"):
        await mod.install_license(
            body={"license_id": "L-OK", "edition": "enterprise"},
            current_user=_admin(),
        )

    session.execute.assert_awaited()
    session.commit.assert_not_awaited()
    mod.reload_license_manager.assert_not_awaited()
