"""Tests for Bug-5942 (OIDC time-claim validation) and Bug-6033
(rehydrator claim-rule validation on import).

Bug-5942: verifies that the exchange_code path rejects expired, not-yet-valid,
and stale ID tokens.

Bug-6033: verifies that _validate_row_security_rule_on_import enforces the
same shape invariants the CRUD API enforces (Bug-5904 / Bug-5905) so
claim-sourced rules imported via snapshot are never silently inert.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Bug-5942: OIDC time-claim validation
# ---------------------------------------------------------------------------

class TestOidcTimeClaims:
    """Test the time-claim validation in oidc_backend.exchange_code."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        """Provide a frozen 'now' for deterministic tests."""
        self.now = 1_700_000_000.0
        monkeypatch.setattr(time, "time", lambda: self.now)

    def _make_base_claims(self):
        return {
            "iss": "https://idp.example.com",
            "aud": "my-client-id",
            "email": "user@example.com",
            "name": "Test User",
            "exp": self.now + 3600,
            "iat": self.now - 10,
        }

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, monkeypatch):
        """A token whose exp is in the past (beyond skew) is rejected."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        claims["exp"] = self.now - 300  # expired 5 minutes ago

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is None

    @pytest.mark.asyncio
    async def test_expired_within_skew_accepted(self, monkeypatch):
        """A token whose exp is within the clock-skew window is accepted."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        claims["exp"] = self.now - 60  # 60s ago, within 120s skew

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is not None
        assert identity.email == "user@example.com"

    @pytest.mark.asyncio
    async def test_nbf_future_rejected(self, monkeypatch):
        """A token whose nbf is in the future (beyond skew) is rejected."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        claims["nbf"] = self.now + 300  # not valid for 5 more minutes

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is None

    @pytest.mark.asyncio
    async def test_nbf_within_skew_accepted(self, monkeypatch):
        """A token whose nbf is within the clock-skew window is accepted."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        claims["nbf"] = self.now + 60  # 60s from now, within 120s skew

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is not None

    @pytest.mark.asyncio
    async def test_stale_iat_rejected(self, monkeypatch):
        """A token issued more than 24h ago is rejected."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        claims["iat"] = self.now - 90_000  # ~25 hours ago

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is None

    @pytest.mark.asyncio
    async def test_valid_time_claims_accepted(self, monkeypatch):
        """A token with all time claims valid is accepted."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        claims["nbf"] = self.now - 10  # valid since 10s ago
        claims["iat"] = self.now - 10  # issued 10s ago
        claims["exp"] = self.now + 3600  # expires in 1h

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is not None
        assert identity.email == "user@example.com"

    @pytest.mark.asyncio
    async def test_missing_exp_rejected(self, monkeypatch):
        """OIDC Core S2 mandates exp; a token without it must be rejected."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        del claims["exp"]

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is None

    @pytest.mark.asyncio
    async def test_exp_exactly_at_skew_boundary_accepted(self, monkeypatch):
        """A token whose exp equals now - skew is still within tolerance."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        # exp + skew == now  =>  now > exp + skew is False  =>  accepted
        claims["exp"] = self.now - 120  # exactly at 120s skew boundary

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is not None

    @pytest.mark.asyncio
    async def test_nbf_exactly_at_skew_boundary_accepted(self, monkeypatch):
        """A token whose nbf equals now + skew is still within tolerance."""
        from src.auth import oidc_backend

        claims = self._make_base_claims()
        # nbf - skew == now  =>  now < nbf - skew is False  =>  accepted
        claims["nbf"] = self.now + 120  # exactly at 120s skew boundary

        identity = await self._exchange_with_claims(
            monkeypatch, oidc_backend, claims
        )
        assert identity is not None

    async def _exchange_with_claims(self, monkeypatch, oidc_backend, claims):
        """Run exchange_code with a mocked HTTP layer that returns
        the given claims as a decoded JWT."""
        import types

        # Mock get_oidc_config
        monkeypatch.setattr(
            oidc_backend, "get_oidc_config",
            lambda: {
                "issuer": "https://idp.example.com",
                "client_id": "my-client-id",
                "client_secret": "secret",
                "scopes": "openid email profile",
                "groups_claim": "groups",
            },
        )

        # Mock _discover
        async def mock_discover(issuer):
            return {
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token",
                "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
            }
        monkeypatch.setattr(oidc_backend, "_discover", mock_discover)

        # Mock _fetch_jwks
        async def mock_fetch_jwks(uri):
            return {"keys": [{"kty": "RSA", "kid": "mock"}]}
        monkeypatch.setattr(oidc_backend, "_fetch_jwks", mock_fetch_jwks)

        # Mock httpx.AsyncClient for the token exchange
        class MockResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "id_token": "mock_jwt_token",
                    "scope": "openid email profile",
                }

            @property
            def is_success(self):
                return True

        class MockClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, **kwargs):
                return MockResponse()

        monkeypatch.setattr(oidc_backend.httpx, "AsyncClient", MockClient)

        # Mock KeySet.import_key_set to return a dummy key set
        mock_key_set = types.SimpleNamespace()
        monkeypatch.setattr(
            oidc_backend.KeySet, "import_key_set",
            staticmethod(lambda jwks: mock_key_set),
        )

        # Mock joserfc_jwt.decode to return the claims directly
        mock_token = types.SimpleNamespace(claims=claims)
        monkeypatch.setattr(
            oidc_backend.joserfc_jwt, "decode",
            lambda raw, key_set: mock_token,
        )

        return await oidc_backend.exchange_code(
            "http://localhost:8001", "test-code"
        )


# ---------------------------------------------------------------------------
# Bug-6033: rehydrator claim-rule validation on import
# ---------------------------------------------------------------------------

class TestRehydratorClaimRuleValidation:
    """_validate_row_security_rule_on_import must enforce Bug-5904/5905
    invariants so the import path does not persist silently inert rules."""

    def _validate(self, row, label="test"):
        from shared.model_snapshot.rehydrator import (
            _validate_row_security_rule_on_import,
        )
        return _validate_row_security_rule_on_import(row, rule_label=label)

    def test_valid_jwt_role_predicate_passes(self):
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "jwt_role",
            "attribute_claim_name": None,
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert warnings == []
        assert row["is_enabled"] is True

    def test_valid_oidc_scope_predicate_passes(self):
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "oidc_scope",
            "attribute_claim_name": "scope",
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert warnings == []
        assert row["is_enabled"] is True

    def test_oidc_scope_blank_claim_name_disables(self):
        """Bug-5904 import-path: oidc_scope with no claim name is disabled."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "oidc_scope",
            "attribute_claim_name": "",
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 1
        assert "disabled on import" in warnings[0]
        assert row["is_enabled"] is False

    def test_saml_claim_none_claim_name_disables(self):
        """Bug-5904 import-path: saml_claim with None claim name is disabled."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "saml_claim",
            "attribute_claim_name": None,
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 1
        assert row["is_enabled"] is False

    def test_saml_claim_whitespace_claim_name_disables(self):
        """Bug-5904 import-path: whitespace-only claim name is treated as blank."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "saml_claim",
            "attribute_claim_name": "   ",
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 1
        assert row["is_enabled"] is False

    def test_invalid_attribute_source_normalises_and_disables(self):
        """Unknown attribute_source is normalised to jwt_role and disabled."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "bogus_source",
            "attribute_claim_name": None,
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 1
        assert row["attribute_source"] == "jwt_role"
        assert row["is_enabled"] is False

    def test_user_mapping_non_default_source_normalises(self):
        """Bug-5905 import-path: user_mapping with non-default attribute_source
        is normalised to jwt_role."""
        row = {
            "rule_type": "user_mapping",
            "attribute_source": "oidc_scope",
            "attribute_claim_name": "scope",
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 2  # source normalised + claim_name cleared
        assert row["attribute_source"] == "jwt_role"
        assert row["attribute_claim_name"] is None

    def test_user_mapping_default_source_passes(self):
        """user_mapping with jwt_role and no claim name passes cleanly."""
        row = {
            "rule_type": "user_mapping",
            "attribute_source": "jwt_role",
            "attribute_claim_name": None,
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert warnings == []

    def test_user_mapping_claim_name_set_is_cleared(self):
        """user_mapping with attribute_claim_name set is cleared."""
        row = {
            "rule_type": "user_mapping",
            "attribute_source": "jwt_role",
            "attribute_claim_name": "department",
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 1
        assert row["attribute_claim_name"] is None

    def test_idp_group_predicate_passes(self):
        """idp_group attribute_source is valid and does not require claim_name."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "idp_group",
            "attribute_claim_name": None,
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert warnings == []
        assert row["is_enabled"] is True

    def test_padded_claim_name_is_trimmed(self):
        """Bug-5904 hardening: a padded claim name is trimmed on import
        so it matches the exact-key lookup in predicate_compiler."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "oidc_scope",
            "attribute_claim_name": "department ",
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert warnings == []
        assert row["attribute_claim_name"] == "department"
        assert row["is_enabled"] is True

    def test_oidc_scope_mixed_whitespace_claim_name_disables(self):
        """oidc_scope with mixed-whitespace-only claim name is disabled
        (differentiates from the saml_claim whitespace test above)."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "oidc_scope",
            "attribute_claim_name": "\t\n ",
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 1
        assert row["is_enabled"] is False

    def test_non_string_claim_name_cleared_and_disabled(self):
        """A non-string attribute_claim_name (e.g. from a corrupt snapshot)
        on a claim-sourced rule is cleared; the cleared (now-None) claim
        name then triggers the blank-claim check, which disables the rule."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "oidc_scope",
            "attribute_claim_name": 123,
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) >= 1
        assert any("non-string" in w for w in warnings)
        assert row["attribute_claim_name"] is None
        assert row["is_enabled"] is False

    def test_non_string_claim_name_on_jwt_role_stays_enabled(self):
        """A non-string attribute_claim_name on a jwt_role predicate is
        cleared and warned, but the rule STAYS ENABLED -- the claim name is
        not consumed for jwt_role rules, so the rule is fully functional
        and disabling it would remove a working security restriction."""
        row = {
            "rule_type": "role_predicate",
            "attribute_source": "jwt_role",
            "attribute_claim_name": 123,
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 1
        assert "non-string" in warnings[0]
        assert row["attribute_claim_name"] is None
        assert row["is_enabled"] is True

    def test_non_string_claim_name_on_user_mapping_stays_enabled(self):
        """A non-string attribute_claim_name on a user_mapping rule is
        cleared and warned, but the rule STAYS ENABLED -- user_mapping
        keys by user_identity and never consumes the claim name."""
        row = {
            "rule_type": "user_mapping",
            "attribute_source": "jwt_role",
            "attribute_claim_name": ["scope"],
            "is_enabled": True,
        }
        warnings = self._validate(row)
        assert len(warnings) == 1
        assert "non-string" in warnings[0]
        assert row["attribute_claim_name"] is None
        assert row["is_enabled"] is True
