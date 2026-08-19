"""Bug-6216 — a connection aimed at a blocked egress target cannot be stored.

The runtime guard (``shared.security.source_host_policy``, exercised in
``tests/unit/test_source_host_egress_policy.py``) refuses the socket. This is
the write-side belt: an operator who types the metadata endpoint into the
connection form is told at the point of the mistake, and the row never exists
to be re-tested, discovered against, or executed through later.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.api import connections as conn_api
from tests.conftest import TEST_PROJECT_ID, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit


@pytest.fixture
def url():
    return f"/api/v1/projects/{TEST_PROJECT_ID}/connections"


@pytest.fixture(autouse=True)
def _default_policy(monkeypatch):
    fake = SimpleNamespace(SOURCE_HOST_BLOCK_PRIVATE=False, SOURCE_HOST_ALLOWLIST="")
    monkeypatch.setattr("shared.config.settings.get_settings", lambda: fake)
    return fake



def _resolves_to(monkeypatch, *addresses):
    import socket as _socket

    async def _getaddrinfo(host, port, **kwargs):
        return [
            (_socket.AF_INET, _socket.SOCK_STREAM, _socket.IPPROTO_TCP, "", (a, 0))
            for a in addresses
        ]

    class _Loop:
        getaddrinfo = staticmethod(_getaddrinfo)

    monkeypatch.setattr("asyncio.get_running_loop", lambda: _Loop())


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [
    "169.254.169.254",
    "127.0.0.1",
    "localhost",
    "metadata.google.internal",
])
async def test_blocked_hosts_are_refused_on_write(host):
    with pytest.raises(HTTPException) as exc:
        await conn_api._reject_blocked_source_host(
            "postgresql",
            {"host": host, "port": 5432, "database": "d", "username": "u"},
            {},
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_a_real_customer_host_is_accepted_on_write(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    await conn_api._reject_blocked_source_host(
        "postgresql",
        {"host": "db.customer.example.com", "port": 5432,
         "database": "d", "username": "u"},
        {},
    )


@pytest.mark.asyncio
async def test_private_host_still_saves_for_self_hosted_installs():
    """The shipped docker-compose stack points at a private address."""
    await conn_api._reject_blocked_source_host(
        "postgresql", {"host": "172.18.0.4"}, {},
    )


@pytest.mark.asyncio
async def test_bigquery_connection_is_not_policed_on_its_host_key():
    """BigQuery has no host FIELD, so a stray ``host`` key is not an egress
    target. Its service-account OAuth endpoints ARE policed (Bug-6216 R2
    finding 4) -- covered in tests/unit/test_source_host_egress_policy.py."""
    await conn_api._reject_blocked_source_host(
        "bigquery", {"host": "169.254.169.254"}, {},
    )


@pytest.mark.asyncio
async def test_host_taken_from_config_is_policed_too():
    with pytest.raises(HTTPException):
        await conn_api._reject_blocked_source_host(
            "postgresql", {}, {"host": "169.254.169.254"},
        )


@pytest.mark.asyncio
async def test_create_endpoint_refuses_a_metadata_endpoint_host(client, url):
    db = make_mock_db()
    with (
        patch("src.api.connections.get_tenant_db", async_gen_from(db)),
        patch("src.api.connections.audit_required", new_callable=AsyncMock),
    ):
        resp = await client.post(url, json={
            "display_name": "metadata probe",
            "connection_type": "postgresql",
            "credentials": {
                "host": "169.254.169.254", "port": 80,
                "database": "d", "username": "u", "password": "p",
            },
        })
    assert resp.status_code == 422
    assert "169.254.169.254" in resp.json()["detail"]
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_endpoint_refuses_repointing_at_loopback(client, url):
    """The merge happens first, so an edit supplying only a new host is still
    judged on what would actually be dialled."""
    conn_id = uuid.uuid4()
    stored = SimpleNamespace(
        id=conn_id,
        project_id=TEST_PROJECT_ID,
        display_name="prod",
        connection_type="postgresql",
        encrypted_credentials=conn_api._encrypt(
            {"host": "db.customer.example.com", "port": 5432,
             "database": "d", "username": "u", "password": "p"}
        ),
        config={},
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=stored)
    with (
        patch("src.api.connections.get_tenant_db", async_gen_from(db)),
        patch("src.api.connections.audit_required", new_callable=AsyncMock),
        patch("src.api.connections.invalidate_artifacts_for_connection",
              new_callable=AsyncMock),
    ):
        resp = await client.patch(f"{url}/{conn_id}", json={
            "credentials": {"host": "127.0.0.1"},
        })
    assert resp.status_code == 422
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# R4 review finding 2 — a NAME that statically resolves to loopback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1.nip.io", "localtest.me"])
async def test_a_name_that_resolves_to_loopback_cannot_be_persisted(host, monkeypatch):
    """The write gate was literal-only, on the reasoning that a save must not
    fail because the customer's database is unreachable right now. That left a
    real, NON-RACY hole: these names resolve to loopback every time, pass a
    literal check, and are never seen by the resolving check, which lives only
    on the OPTIONAL Test-Connection path. Create, skip Test, run one query, and
    the socket to 127.0.0.1 on a chosen port opens."""
    _resolves_to(monkeypatch, "127.0.0.1")
    with pytest.raises(HTTPException) as exc:
        await conn_api._reject_blocked_source_host(
            "postgresql", {"host": host, "username": "u"}, {},
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_a_host_that_does_not_resolve_yet_still_saves(monkeypatch):
    """The objection the literal-only choice was protecting is answered rather
    than traded away: transient DNS must not block a legitimate save."""
    import socket as _socket

    class _Loop:
        @staticmethod
        async def getaddrinfo(host, port, **kwargs):
            raise _socket.gaierror("not yet in DNS")

    monkeypatch.setattr("asyncio.get_running_loop", lambda: _Loop())
    await conn_api._reject_blocked_source_host(
        "postgresql", {"host": "db.not-yet-provisioned.example", "username": "u"}, {},
    )
