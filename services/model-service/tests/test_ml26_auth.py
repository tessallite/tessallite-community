"""ML26 — auth/RBAC defensive-correctness tests.

Covers the F-021 fixes that are pure-logic (no live DB): constant-time
system-admin compare (F-021-10), case-insensitive user lookup (F-021-09), and
the SSRF URL guard shared by the webhook surfaces (F-022-05). The endpoint-level
RBAC and SSO/JIT behaviour is covered in test_access.py / test_sso.py.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# F-021-10: system-admin compare is constant-time and correct
# ---------------------------------------------------------------------------

class TestSystemAdminConstantTime:
    def test_correct_credentials_accepted(self, monkeypatch):
        from src.auth import local_backend
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_EMAIL", "root@x")
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_PASSWORD", "s3cret")
        assert local_backend.authenticate_system_admin("root@x", "s3cret") is True

    def test_wrong_password_rejected(self, monkeypatch):
        from src.auth import local_backend
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_EMAIL", "root@x")
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_PASSWORD", "s3cret")
        assert local_backend.authenticate_system_admin("root@x", "wrong") is False

    def test_wrong_email_rejected(self, monkeypatch):
        from src.auth import local_backend
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_EMAIL", "root@x")
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_PASSWORD", "s3cret")
        assert local_backend.authenticate_system_admin("evil@x", "s3cret") is False

    def test_password_uses_constant_time_compare(self, monkeypatch):
        """Bug-7802: the PASSWORD must go through hmac.compare_digest (the
        secret). The email is not a secret and uses a plain case-insensitive
        comparison, avoiding the Unicode fragility of forcing bytes through
        hmac.compare_digest for a non-secret field."""
        import hmac
        from src.auth import local_backend
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_EMAIL", "root@x")
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_PASSWORD", "s3cret")
        calls = {"n": 0}
        real = hmac.compare_digest

        def counting(a, b):
            calls["n"] += 1
            return real(a, b)

        monkeypatch.setattr(hmac, "compare_digest", counting)
        local_backend.authenticate_system_admin("root@x", "s3cret")
        assert calls["n"] == 1, "password must use compare_digest (email uses plain ==)"

    def test_email_case_insensitive(self, monkeypatch):
        """Bug-7802: system-admin email match is case-insensitive."""
        from src.auth import local_backend
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_EMAIL", "Root@X.com")
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_PASSWORD", "s3cret")
        assert local_backend.authenticate_system_admin("root@x.com", "s3cret") is True
        assert local_backend.authenticate_system_admin("ROOT@X.COM", "s3cret") is True

    def test_non_ascii_email_does_not_crash(self, monkeypatch):
        """Bug-7802: a non-ASCII email must not raise TypeError; it should
        simply fail the match (the configured system-admin email is normally
        ASCII)."""
        from src.auth import local_backend
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_EMAIL", "root@x")
        monkeypatch.setattr(local_backend.settings, "SYSTEM_ADMIN_PASSWORD", "s3cret")
        result = local_backend.authenticate_system_admin("rooñ@x", "s3cret")
        assert result is False


# ---------------------------------------------------------------------------
# F-021-09: case-insensitive local user authentication
# ---------------------------------------------------------------------------

class TestCaseInsensitiveAuth:
    @pytest.mark.asyncio
    async def test_authenticate_user_lowercases_email(self):
        from unittest.mock import AsyncMock, MagicMock
        from src.auth.local_backend import authenticate_user

        captured = {}

        class _DB:
            async def execute(self, stmt):
                captured["stmt"] = str(stmt)
                result = MagicMock()
                user = MagicMock()
                user.is_active = True
                user.hashed_password = "h"
                result.scalar_one_or_none.return_value = user
                return result

        from unittest.mock import patch
        with patch("src.auth.local_backend.verify_password", return_value=True):
            user = await authenticate_user(_DB(), "MixedCase@Corp.COM", "pw")
        assert user is not None
        # The query lowercases both sides (func.lower on the column, lowered arg).
        assert "lower" in captured["stmt"].lower()

    @pytest.mark.asyncio
    async def test_authenticate_user_pays_dummy_hash_for_missing_email(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.auth import local_backend

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        with patch("src.auth.local_backend.verify_password", return_value=False) as vp:
            user = await local_backend.authenticate_user(
                db, "missing@corp.com", "wrong"
            )

        assert user is None
        vp.assert_called_once_with("wrong", local_backend._LOGIN_DUMMY_HASH)


# ---------------------------------------------------------------------------
# F-022-05: SSRF URL guard (shared, used by webhook create/update + dispatch)
# ---------------------------------------------------------------------------

class TestSsrfValidation:
    def test_https_public_host_allowed(self):
        from shared.webhooks.ssrf import validate_webhook_url
        assert validate_webhook_url("https://example.com/hook") == "https://example.com/hook"

    @pytest.mark.parametrize("url", [
        "ftp://example.com/hook",            # bad scheme
        "https:///nohost",                   # no host
        "http://localhost/hook",             # blocked host
        "http://metadata.google.internal/x", # cloud metadata
        "http://169.254.169.254/latest",     # link-local metadata IP
        "http://127.0.0.1/x",                # loopback IP
        "http://10.0.0.5/x",                 # private IP
    ])
    def test_unsafe_urls_rejected(self, url):
        from shared.webhooks.ssrf import validate_webhook_url
        with pytest.raises(ValueError):
            validate_webhook_url(url)

    def test_https_only_mode(self):
        from shared.webhooks.ssrf import validate_webhook_url
        with pytest.raises(ValueError):
            validate_webhook_url("http://example.com/hook", allow_http=False)


# ---------------------------------------------------------------------------
# Bug-7326: bcrypt 72-byte password limit
# ---------------------------------------------------------------------------

class TestPasswordLengthLimit:
    def test_hash_password_rejects_over_72_bytes(self):
        """Bug-7326: hash_password must reject passwords over 72 bytes."""
        from src.auth.local_backend import hash_password
        long_pw = "a" * 73  # 73 ASCII bytes
        with pytest.raises(ValueError, match="too long"):
            hash_password(long_pw)

    def test_hash_password_accepts_72_bytes(self):
        """Bug-7326: exactly 72 bytes must be accepted."""
        from src.auth.local_backend import hash_password
        pw = "a" * 72
        h = hash_password(pw)
        assert h.startswith("$2")

    def test_verify_password_returns_false_for_over_72_bytes(self):
        """Bug-7326: verify_password returns False (no crash) for overlong
        passwords, since no valid hash could exist for such a password."""
        from src.auth.local_backend import hash_password, verify_password
        short_pw = "short"
        h = hash_password(short_pw)
        long_pw = "b" * 73
        assert verify_password(long_pw, h) is False

    def test_hash_password_rejects_multibyte_over_72(self):
        """Bug-7326: a password under 72 characters but over 72 UTF-8 bytes
        must be rejected (e.g. 25 3-byte characters = 75 bytes)."""
        from src.auth.local_backend import hash_password
        # Each CJK character is 3 bytes in UTF-8
        long_pw = "一" * 25  # 25 chars * 3 bytes = 75 bytes
        with pytest.raises(ValueError, match="too long"):
            hash_password(long_pw)


# ---------------------------------------------------------------------------
# Bug-6816: invalid/unactivated license entitlements fail-closed
# ---------------------------------------------------------------------------

class TestFailClosedLicenseEntitlements:
    def test_unactivated_manager_denies_features(self):
        """Bug-6816: _UnactivatedManager must report features: 'none',
        not 'all', so UI feature-gate consumers see a consistent deny."""
        from src.licensing_guard import _UnactivatedManager
        mgr = _UnactivatedManager()
        ent = mgr.entitlements()
        assert ent["features"] == "none"
        assert ent.get("activated") is False

    def test_invalid_manager_denies_features(self):
        """Bug-6816: _InvalidLicenseManager must report features: 'none',
        not 'all', so UI feature-gate consumers see a consistent deny."""
        from src.licensing_guard import _InvalidLicenseManager
        mgr = _InvalidLicenseManager()
        ent = mgr.entitlements()
        assert ent["features"] == "none"
        assert ent.get("activated") is False

    def test_unlimited_manager_allows_features(self):
        """Sanity: the full-product manager reports features: 'all'."""
        from src.licensing_guard import _UnlimitedManager
        mgr = _UnlimitedManager()
        assert mgr.entitlements()["features"] == "all"
