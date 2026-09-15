"""System-log credential redaction stays independent of host environment values."""

from __future__ import annotations

import pytest

from shared.system_logs.redaction import redact


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("private_key", "private-key-material-123"),
        ("AWS_SECRET_ACCESS_KEY", "aws-secret-material-123"),
        ("client_secret", "client-secret-material-123"),
        ("session_token", "session-token-material-123"),
        ("cloud/private-key", "cloud-key-material-123"),
        ("cloud_private-key", "cloud-key-material-456"),
        ("AZURE_CLIENT_SECRET", "azure-secret-material-123"),
        ("GCP_SERVICE_ACCOUNT_KEY", "gcp-key-material-123"),
    ],
)
def test_named_cloud_credentials_are_redacted_without_environment_setup(
    field, value
):
    rendered = redact(f"{field}={value}")

    assert value not in rendered
    assert rendered == f"{field}=[REDACTED]"


def test_pem_private_key_block_is_redacted_across_lines():
    begin = "-----BEGIN " + "PRIVATE KEY-----"
    end = "-----END " + "PRIVATE KEY-----"
    body = "MII-fake-private-key-material"

    rendered = redact(f"startup key={begin}\n{body}\n{end}")

    assert rendered == "startup key=[REDACTED_PRIVATE_KEY]"
    assert body not in rendered
    assert begin not in rendered
    assert end not in rendered
