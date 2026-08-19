"""Bug-7108 — shared SSRF allowlist for LLM base_url values.

Tests that the shared validator (used by build_adapter) correctly
accepts known LLM provider URLs, rejects SSRF-risk URLs, and handles
edge cases.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from shared.llm.base_url_validator import validate_llm_base_url

pytestmark = pytest.mark.unit


class TestAllowedProviders:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.openai.com/v1",
            "https://api.anthropic.com",
            "https://api.deepseek.com/v1",
            "https://api.z.ai/v1",
            "https://open.bigmodel.cn/api/v4",
            "https://generativelanguage.googleapis.com/v1beta",
            "https://aiplatform.googleapis.com/v1",
        ],
    )
    def test_known_providers_pass(self, url: str) -> None:
        assert validate_llm_base_url(url) == url

    def test_none_passes_through(self) -> None:
        assert validate_llm_base_url(None) is None


class TestOllamaLoopback:
    def test_ollama_default_port_accepted(self) -> None:
        url = "http://localhost:11434/v1"
        assert validate_llm_base_url(url) == url

    def test_other_loopback_port_rejected(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            validate_llm_base_url("http://localhost:5432")

    def test_model_service_port_rejected(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            validate_llm_base_url("http://localhost:8001/api")

    def test_loopback_default_http_port_rejected(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            validate_llm_base_url("http://localhost/v1")

    def test_ipv4_loopback_rejected(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            validate_llm_base_url("http://127.0.0.1:8080")


class TestRemoteHTTPRejected:
    def test_http_to_known_provider_rejected(self) -> None:
        """API keys and prompts must not travel over plaintext."""
        with pytest.raises(ValueError, match="requires HTTPS"):
            validate_llm_base_url("http://api.openai.com/v1")

    def test_http_to_anthropic_rejected(self) -> None:
        with pytest.raises(ValueError, match="requires HTTPS"):
            validate_llm_base_url("http://api.anthropic.com")


class TestSSRFBlocked:
    def test_internal_host_rejected(self) -> None:
        with pytest.raises(ValueError, match="not in the allowed"):
            validate_llm_base_url("https://evil.internal.corp/api")

    def test_metadata_endpoint_rejected(self) -> None:
        with pytest.raises(ValueError, match="not in the allowed"):
            validate_llm_base_url("http://169.254.169.254/metadata")

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid base_url scheme"):
            validate_llm_base_url("ftp://files.example.com")

    def test_custom_host_rejected(self) -> None:
        with pytest.raises(ValueError, match="not in the allowed"):
            validate_llm_base_url("https://custom-llm.example.com:8443/v1")


class TestConfiguredEndpoints:
    def test_configured_endpoint_accepted(self) -> None:
        mock_endpoints = {"custom": "https://custom-llm.corp.com:8443/v1"}
        with patch(
            "shared.config.bootstrap.system_snapshot_get",
            return_value=mock_endpoints,
        ):
            url = "https://custom-llm.corp.com:8443/v1"
            assert validate_llm_base_url(url) == url

    def test_configured_endpoint_wrong_port_rejected(self) -> None:
        mock_endpoints = {"custom": "https://custom-llm.corp.com:8443/v1"}
        with patch(
            "shared.config.bootstrap.system_snapshot_get",
            return_value=mock_endpoints,
        ):
            with pytest.raises(ValueError, match="not in the allowed"):
                validate_llm_base_url("https://custom-llm.corp.com:9999/v1")

    def test_configured_endpoint_wrong_scheme_rejected(self) -> None:
        mock_endpoints = {"custom": "https://custom-llm.corp.com:8443/v1"}
        with patch(
            "shared.config.bootstrap.system_snapshot_get",
            return_value=mock_endpoints,
        ):
            with pytest.raises(ValueError, match="not in the allowed"):
                validate_llm_base_url("http://custom-llm.corp.com:8443/v1")


class TestBuildAdapterEnforcesSSRF:
    """Bug-7108 — build_adapter must reject non-allowlisted base_url."""

    def test_build_adapter_rejects_ssrf_url(self) -> None:
        from shared.llm.adapter import LLMConfig, build_adapter

        config = LLMConfig(
            provider="openai",
            display_name="SSRF test",
            base_url="http://169.254.169.254/metadata",
            api_key="test-key",
            model_name="gpt-4",
            max_tokens=100,
            temperature=0,
            timeout_seconds=30,
        )
        with pytest.raises(ValueError, match="not in the allowed"):
            build_adapter(config)

    def test_build_adapter_accepts_known_provider(self) -> None:
        from shared.llm.adapter import LLMConfig, build_adapter

        config = LLMConfig(
            provider="openai",
            display_name="OK test",
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model_name="gpt-4",
            max_tokens=100,
            temperature=0,
            timeout_seconds=30,
        )
        # Should not raise — adapter construction succeeds.
        adapter = build_adapter(config)
        assert adapter is not None
