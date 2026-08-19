"""F-014-06 / F-014-07: source create-path secrets and nullability.

Test escape: classify-add hardcoded is_nullable=true; DataSource.config had no
secret gate. Guard: classifiedColumnsToSyncPayload + DataSourceCreate validator.
Tier: T2.
"""
from __future__ import annotations

import pytest

from shared.schemas.domains.models_sources import DataSourceCreate, DataSourceResponse


def test_datasource_create_rejects_password_in_config():
    with pytest.raises(Exception):
        DataSourceCreate(
            project_connection_id="00000000-0000-0000-0000-000000000001",
            source_type="postgresql",
            display_name="leaky",
            config={"password": "LEAK-IN-CONFIG", "schema": "public"},
        )


def test_datasource_response_redacts_pre_gate_password():
    resp = DataSourceResponse(
        id="00000000-0000-0000-0000-000000000001",
        model_id="00000000-0000-0000-0000-000000000002",
        project_connection_id="00000000-0000-0000-0000-000000000003",
        source_type="postgresql",
        display_name="legacy",
        default_schema="public",
        config={"schema": "public", "password": "PLAINTEXT-LEAK"},
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    assert "password" not in resp.config
    assert "PLAINTEXT-LEAK" not in str(resp.config)
    assert resp.config.get("schema") == "public"


def test_redact_secrets_in_error_strips_password_value():
    """F-014-10 belt: a synthetic driver message that embeds the password."""
    from shared.source_introspection import _redact_secrets_in_error

    detail = _redact_secrets_in_error(
        "login failed for PWD=super-secret-pass; user=u",
        {"password": "super-secret-pass", "username": "u"},
    )
    assert "super-secret-pass" not in detail
    assert "PWD=***" in detail
