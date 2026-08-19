"""F-014-01 (Bug-7983): config is a plaintext credential exfiltration channel.

Two layers of defence are asserted here:

1. The API rejects a secret placed in the ``config`` bag on create (422), so a
   secret can never enter the plaintext JSONB column.
2. ``_to_response`` strips secret-like keys from ``config`` on every read, so a
   pre-gate row that already carries a plaintext secret never echoes it to a
   caller (including modelers who can list connections).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone

import pytest

from src.api import connections as conn_api


# ---------------------------------------------------------------------------
# Layer 2: response-side redaction of pre-existing plaintext secrets
# ---------------------------------------------------------------------------


def _fake_row(config: dict):
    # SimpleNamespace (not MagicMock) so ConnectionResponse.model_validate does
    # not pick up an auto-created ``credentials_preview`` attribute.
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        display_name="legacy",
        connection_type="postgresql",
        config=config,
        encrypted_credentials=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def test_to_response_strips_secret_from_config():
    row = _fake_row({"schema": "public", "password": "PLAINTEXT-LEAK", "host": "h"})
    resp = conn_api._to_response(row)
    assert "password" not in resp.config
    assert "PLAINTEXT-LEAK" not in str(resp.config)
    # Non-secret fields survive.
    assert resp.config.get("schema") == "public"
    assert resp.config.get("host") == "h"


def test_to_response_strips_nested_secret_from_config():
    row = _fake_row({"advanced": {"token": "LEAK"}, "schema": "public"})
    resp = conn_api._to_response(row)
    assert "LEAK" not in str(resp.config)
    assert resp.config.get("schema") == "public"


def test_to_response_strips_secret_in_list_of_dicts():
    """Opus reviewer FINDING-2: a secret inside a list of dicts must also be
    stripped by the recursive redaction."""
    row = _fake_row({"items": [{"password": "LIST-LEAK"}], "schema": "public"})
    resp = conn_api._to_response(row)
    assert "LIST-LEAK" not in str(resp.config)
    assert resp.config.get("schema") == "public"


# ---------------------------------------------------------------------------
# Layer 1: the create route's request model is the gated schema, so a secret in
# config is rejected at request validation (422) before the handler persists it.
# The gate logic itself is asserted in
# tests/unit/test_connections_credential_security_b2c.py; here we prove the API
# uses the gated model rather than a raw dict.
# ---------------------------------------------------------------------------


def test_create_route_uses_gated_connection_create_schema():
    from shared.schemas.domains.tenants_projects import ConnectionCreate

    # The route imports ConnectionCreate; confirm the wired model enforces the
    # config secret gate so FastAPI rejects the request body before the handler.
    assert conn_api.ConnectionCreate is ConnectionCreate
    with pytest.raises(Exception):
        conn_api.ConnectionCreate(
            display_name="leaky",
            connection_type="postgresql",
            credentials={"host": "h", "username": "u", "password": "p"},
            config={"password": "LEAK-IN-CONFIG"},
        )
