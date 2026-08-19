"""Route tests for the Personal Access Token management API (Bug-7314).

Drives POST/GET/DELETE /api/v1/auth/tokens through the ASGI app with the
current-user dependency overridden and the tenant DB faked, so no live
Postgres is required. Asserts ownership scoping, show-once semantics, and that
list/response never carry the plaintext or the hash.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from .result_fakes import FakeScalarResult

from src.main import app
from src.auth.middleware import CurrentUser, get_current_user

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _current_user() -> CurrentUser:
    return CurrentUser(
        user_id="user@acme.test",
        tenant_id="acme",
        email="user@acme.test",
        role="member",
    )


def _make_user():
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        username="user",
        email="user@acme.test",
        is_active=True,
        role="member",
    )


class _ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, user, pats=None):
        self._user = user
        self._pats = list(pats or [])
        self.added = []
        self.committed = 0

    async def execute(self, stmt):
        sql = str(stmt).lower()
        if "local_users" in sql:
            return _ScalarResult([self._user])
        if "personal_access_tokens" in sql:
            params = stmt.compile().params
            uuid_binds = {v for v in params.values() if isinstance(v, uuid.UUID)}
            # WHERE binds: LIST -> {user_id}; REVOKE -> {token_id, user_id}.
            # Match a row only if BOTH its id and user_id are represented among
            # the bind values, EXCEPT the list path (single bind = user_id only).
            rows = []
            for p in self._pats:
                if p.user_id not in uuid_binds:
                    continue
                if len(uuid_binds) >= 2 and p.id not in uuid_binds:
                    continue
                rows.append(p)
            return _ScalarResult(rows)
        raise AssertionError(f"unexpected query: {sql}")

    def add(self, obj):
        # Simulate DB defaults the response schema reads back.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.last_used_at = getattr(obj, "last_used_at", None)
        obj.revoked_at = getattr(obj, "revoked_at", None)
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        return None


def _yield(db):
    async def _gen(_tid):
        yield db
    return _gen


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.fixture(autouse=True)
def _override_user():
    app.dependency_overrides[get_current_user] = _current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_create_returns_plaintext_once(monkeypatch):
    from src.api import pat_tokens

    user = _make_user()
    db = _FakeDB(user)
    monkeypatch.setattr(pat_tokens, "get_tenant_db", _yield(db))

    async with await _client() as ac:
        resp = await ac.post(
            "/api/v1/auth/tokens", json={"label": "Excel laptop"}
        )
    assert resp.status_code == 201
    body = resp.json()
    # The plaintext is present exactly here and is PAT-shaped.
    assert body["token"].startswith("tesspat_")
    # The metadata object must NOT leak the hash or the plaintext.
    assert "token_hash" not in body["pat"]
    assert body["token"] not in body["pat"].values()
    assert body["pat"]["label"] == "Excel laptop"
    assert body["pat"]["token_prefix"].startswith("tesspat_")
    # The stored row holds a hash, never the plaintext.
    stored = db.added[0]
    assert stored.token_hash != body["token"]
    assert body["token"] not in stored.token_hash


@pytest.mark.asyncio
async def test_create_with_expiry_sets_expires_at(monkeypatch):
    from src.api import pat_tokens

    db = _FakeDB(_make_user())
    monkeypatch.setattr(pat_tokens, "get_tenant_db", _yield(db))
    async with await _client() as ac:
        resp = await ac.post(
            "/api/v1/auth/tokens", json={"label": "x", "expires_in_days": 30}
        )
    assert resp.status_code == 201
    assert db.added[0].expires_at is not None


@pytest.mark.asyncio
async def test_create_rejects_out_of_range_expiry(monkeypatch):
    from src.api import pat_tokens

    db = _FakeDB(_make_user())
    monkeypatch.setattr(pat_tokens, "get_tenant_db", _yield(db))
    async with await _client() as ac:
        resp = await ac.post(
            "/api/v1/auth/tokens", json={"expires_in_days": 9999}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_returns_metadata_only(monkeypatch):
    from src.api import pat_tokens

    user = _make_user()
    pat = types.SimpleNamespace(
        id=uuid.uuid4(), user_id=user.id, token_prefix="tesspat_abcd1234",
        label="mine", created_at=NOW,
        expires_at=None, last_used_at=None, revoked_at=None,
    )
    db = _FakeDB(user, pats=[pat])
    monkeypatch.setattr(pat_tokens, "get_tenant_db", _yield(db))
    async with await _client() as ac:
        resp = await ac.get("/api/v1/auth/tokens")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert "token" not in rows[0]
    assert "token_hash" not in rows[0]
    assert rows[0]["token_prefix"] == "tesspat_abcd1234"


@pytest.mark.asyncio
async def test_revoke_sets_revoked_at(monkeypatch):
    from src.api import pat_tokens

    user = _make_user()
    pat = types.SimpleNamespace(
        id=uuid.uuid4(), user_id=user.id, token_prefix="tesspat_abcd1234",
        label="mine", created_at=NOW,
        expires_at=None, last_used_at=None, revoked_at=None,
    )
    db = _FakeDB(user, pats=[pat])
    monkeypatch.setattr(pat_tokens, "get_tenant_db", _yield(db))
    async with await _client() as ac:
        resp = await ac.delete(f"/api/v1/auth/tokens/{pat.id}")
    assert resp.status_code == 204
    assert pat.revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_unknown_id_is_404(monkeypatch):
    from src.api import pat_tokens

    db = _FakeDB(_make_user(), pats=[])
    monkeypatch.setattr(pat_tokens, "get_tenant_db", _yield(db))
    async with await _client() as ac:
        resp = await ac.delete(f"/api/v1/auth/tokens/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revoke_malformed_id_is_404(monkeypatch):
    from src.api import pat_tokens

    db = _FakeDB(_make_user(), pats=[])
    monkeypatch.setattr(pat_tokens, "get_tenant_db", _yield(db))
    async with await _client() as ac:
        resp = await ac.delete("/api/v1/auth/tokens/not-a-uuid")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revoke_other_users_token_is_404(monkeypatch):
    from src.api import pat_tokens

    user = _make_user()
    # A token owned by a DIFFERENT user id: the ownership filter must exclude it.
    other_pat = types.SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), token_prefix="tesspat_ffff0000",
        label="theirs", created_at=NOW,
        expires_at=None, last_used_at=None, revoked_at=None,
    )
    db = _FakeDB(user, pats=[other_pat])
    monkeypatch.setattr(pat_tokens, "get_tenant_db", _yield(db))
    async with await _client() as ac:
        resp = await ac.delete(f"/api/v1/auth/tokens/{other_pat.id}")
    assert resp.status_code == 404
    assert other_pat.revoked_at is None
