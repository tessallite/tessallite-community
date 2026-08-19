"""Bug-6293: a credential-including project export must not 500 on a
deferred-credential connection (empty ciphertext).

A connection created via the credentialless import path persists
``encrypted_credentials = b""``. ``Fernet.decrypt(b"")`` raises ``InvalidToken``,
so a later ``include_credentials=True`` export crashed with a raw 500 and no
bundle. The serialiser now guards the empty ciphertext (mirroring the
``llm_configs`` / ``agent_config`` branches) and simply omits the credentials
field for that connection so it round-trips as a placeholder.

Test escape: no export test covered the empty-ciphertext connection.
Guard: ``and c.encrypted_credentials`` on the connection credentials branch.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from shared.model_snapshot.project_serialiser import export_project


def _scalars_result(rows):
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = rows
    result.scalars.return_value = scalars
    return result


@pytest.mark.asyncio
async def test_export_skips_empty_ciphertext_and_keeps_real_credentials():
    project_id = uuid.uuid4()
    system_fernet = Fernet(Fernet.generate_key())
    passphrase_fernet = Fernet(Fernet.generate_key())

    deferred = SimpleNamespace(
        id=uuid.uuid4(),
        display_name="deferred-conn",
        connection_type="postgres",
        config={"host": "db"},
        encrypted_credentials=b"",  # deferred-credential connection
    )
    real = SimpleNamespace(
        id=uuid.uuid4(),
        display_name="real-conn",
        connection_type="postgres",
        config={"host": "db2"},
        encrypted_credentials=system_fernet.encrypt(b'{"password":"s3cret"}'),
    )

    db = AsyncMock()
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            slug="demo", display_name="Demo", is_active=True
        )
    )
    # execute() is called for the connections query, then the models query.
    db.execute = AsyncMock(
        side_effect=[
            _scalars_result([deferred, real]),  # connections
            _scalars_result([]),                # models (none)
        ]
    )

    bundle = await export_project(
        project_id,
        db,
        tenant_slug="t1",
        sections={"connections"},
        include_credentials=True,
        system_fernet=system_fernet,
        passphrase_fernet=passphrase_fernet,
    )

    conns = {c["display_name"]: c for c in bundle["connections"]}
    # Deferred connection exports without crashing and omits credentials.
    assert "credentials" not in conns["deferred-conn"]
    # The real connection still carries a re-encrypted credentials envelope
    # that decrypts back to the original plaintext.
    assert "credentials" in conns["real-conn"]
    import base64
    portable = base64.b64decode(conns["real-conn"]["credentials"])
    assert passphrase_fernet.decrypt(portable) == b'{"password":"s3cret"}'
