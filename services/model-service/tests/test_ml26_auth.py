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

    def test_uses_constant_time_compare(self, monkeypatch):
        """Both fields must go through hmac.compare_digest, not ``==``."""
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
        assert calls["n"] == 2, "both email and password must use compare_digest"


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
