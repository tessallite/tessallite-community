"""Bug-8259: the LLM provider ``config`` bag must not carry plaintext secrets.

``LLMProviderConfig.config`` is an untyped dict persisted as plaintext JSONB and
returned by ``GET /projects/{id}/llm-configs``, which any project VIEWER may
call while writing it needs only project admin. That is the same
plaintext-config-bag class F-014-01 / Bug-7983 closed on ``ProjectConnection``:
the provider API key itself is Fernet-encrypted and never echoed
(``has_api_key`` bool), but nothing stopped a secret being parked in ``config``.

Two barriers, asserted here as security properties rather than as "the function
ran":

* write side — a create/update/ad-hoc-test body carrying a secret-like key is
  REFUSED, and a legitimate provider config is ACCEPTED;
* read side — a row that already holds one (written before the gate, or by any
  non-API writer) is never echoed back.

Run from tessallite/services/model-service/:
    pytest tests/test_llm_config_secret_gate_8259.py
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shared.schemas.pydantic_models import (
    LLMConnectionTestRequest,
    LLMProviderConfigCreate,
    LLMProviderConfigUpdate,
)
from src.api import llm_config as mod

pytestmark = pytest.mark.unit


def _create_kwargs(**overrides):
    base = dict(
        provider="anthropic",
        display_name="primary",
        api_key="sk-live-do-not-log",
        model_name="claude-x",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Write side: a secret-like key in ``config`` is REFUSED
# ---------------------------------------------------------------------------

# Names that must be refused. Exact hits plus the compound/stem shapes the
# shared policy exists to catch, so a future narrowing of the stems is caught.
_REJECTED_CONFIG_KEYS = [
    "api_key",
    "password",
    "secret",
    "token",
    "private_key",
    "client_secret",
    "credentials",
    "db_password",
    "aws_secret_access_key",
    "bearer_token",
    "keyfile_json",
    "connection_string",
]


@pytest.mark.parametrize("key", _REJECTED_CONFIG_KEYS)
def test_create_refuses_secret_like_config_key(key):
    with pytest.raises(ValidationError) as exc:
        LLMProviderConfigCreate(**_create_kwargs(config={key: "s3cr3t"}))
    assert "secret-like keys" in str(exc.value)


@pytest.mark.parametrize("key", _REJECTED_CONFIG_KEYS)
def test_update_refuses_secret_like_config_key(key):
    with pytest.raises(ValidationError):
        LLMProviderConfigUpdate(config={key: "s3cr3t"})


def test_adhoc_test_request_refuses_secret_like_config_key():
    """The ad-hoc bag is not persisted but IS forwarded to the optimizer
    service, so it carries the same policy as ``ConnectionTestRequest``."""
    with pytest.raises(ValidationError):
        LLMConnectionTestRequest(
            provider="google",
            api_key="k",
            model_name="gemini-x",
            config={"service_account_json": "{...}"},
        )


def test_create_refuses_secret_nested_under_an_innocuous_key():
    """The gate recurses, so burying the secret one level down does not help."""
    with pytest.raises(ValidationError):
        LLMProviderConfigCreate(
            **_create_kwargs(config={"vendor": {"extra": {"api_key": "s3cr3t"}}})
        )


# ---------------------------------------------------------------------------
# Write side: the legitimate provider config is ACCEPTED
# ---------------------------------------------------------------------------

def test_create_accepts_the_real_provider_config_keys():
    """The gate must not break the product. These are every key the LLM
    settings dialog emits plus the documented per-row overrides
    (``LLMConfigurationsPanel.tsx``, ``shared/llm/sa_auth.py``,
    ``llm.anthropic_thinking_budget``)."""
    cfg = {
        "anthropic_api_version": "2023-06-01",
        "google_mode": "vertex_ai",
        "google_project": "acme-prod",
        "google_location": "europe-west1",
        "thinking_budget": 4000,
    }
    assert LLMProviderConfigCreate(**_create_kwargs(config=cfg)).config == cfg
    assert LLMProviderConfigUpdate(config=cfg).config == cfg


def test_update_with_no_config_field_is_untouched():
    """``None`` means "field not being changed" — the PATCH gate must not turn
    an omitted field into a rejection."""
    assert LLMProviderConfigUpdate(display_name="renamed").config is None


def test_empty_config_is_accepted():
    assert LLMProviderConfigCreate(**_create_kwargs()).config == {}


# ---------------------------------------------------------------------------
# Read side: a pre-gate row is never echoed back
# ---------------------------------------------------------------------------

def _record(config: dict, *, encrypted_api_key: bytes | None = b"x") -> types.SimpleNamespace:
    now = datetime.now(timezone.utc)
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        provider="anthropic",
        display_name="primary",
        base_url=None,
        model_name="claude-x",
        max_tokens=4096,
        temperature=0.2,
        timeout_seconds=60,
        config=config,
        encrypted_api_key=encrypted_api_key,
        created_at=now,
        updated_at=now,
    )


def test_response_drops_a_secret_key_written_before_the_gate():
    """A row persisted before the write gate existed must not leak through the
    viewer-readable list endpoint."""
    resp = mod._to_response(
        _record({"google_project": "acme-prod", "api_key": "sk-leaked"})
    )
    assert "api_key" not in resp.config
    assert "sk-leaked" not in str(resp.config)
    assert resp.config["google_project"] == "acme-prod"


# Fixtures for the value-content barrier. Deliberately NOT a private-key PEM:
# this directory is published into the community repository, and
# ``scripts/community_release/leak_check.py`` hard-fails the export on a literal
# ``BEGIN ... PRIVATE KEY`` anywhere in the tree — a scanner cannot tell a test
# fixture from the real thing, and it is right not to try. These two strings hit
# the same two ``_SECRET_VALUE_MARKERS`` a leaked key would (``-----begin`` and
# ``"type": "service_account"``) without putting key material in the repository.
# Do not "improve" this back to a private-key block: it breaks the release gate.
_PEM_BLOCK = "-----BEGIN CERTIFICATE-----\nFAKEMATERIALNOTAREALKEY\n-----END CERTIFICATE-----"
_SA_JSON = '{"type": "service_account", "project_id": "acme-prod"}'


@pytest.mark.parametrize("blob", [_PEM_BLOCK, _SA_JSON], ids=["pem-block", "sa-json"])
def test_response_redacts_key_material_parked_under_an_innocuous_key(blob):
    """A key-name denylist alone cannot close this: the secret sits in the
    VALUE under a name the denylist has no reason to know."""
    resp = mod._to_response(_record({"notes": blob}))
    assert blob not in str(resp.config)
    assert resp.config["notes"] == "__REDACTED__"
    # The key name survives so an operator can see WHICH field needs cleaning.
    assert "notes" in resp.config


def test_response_leaves_a_clean_config_untouched():
    """Redaction must be inert on real data — the edit dialog reads this back."""
    cfg = {"google_mode": "vertex_ai", "google_project": "acme-prod"}
    assert mod._to_response(_record(cfg)).config == cfg


def test_response_still_reports_api_key_presence_without_the_key():
    resp = mod._to_response(_record({}, encrypted_api_key=b"ciphertext"))
    assert resp.has_api_key is True
    assert not hasattr(resp, "api_key")
