"""Unit tests for the Personal Access Token service, backend, and terminal
auth chain (Bug-7314).

These test the security-critical invariants directly against ``src.auth.pat``,
``src.auth.pat_backend`` and ``src.auth.chain`` with a fake in-memory tenant DB,
so no live Postgres is required.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from .result_fakes import FakeScalarResult

from src.auth.pat import (
    PAT_SCHEME_PREFIX,
    generate_token,
    is_pat_form,
    is_pat_scheme,
    touch_last_used,
    validate_pat,
)
from src.auth.local_backend import verify_password

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake DB plumbing
# ---------------------------------------------------------------------------

class _Row:
    """Row-like: attribute access + tuple-ish, matching SQLAlchemy Row."""
    def __init__(self, **cols):
        self.__dict__.update(cols)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Answers the queries validate_pat / touch_last_used make.

    validate_pat now selects raw COLUMNS (id+hash on the prefix lookup, then
    user_id/revoked_at/expires_at on the by-id re-read) — deliberately bypassing
    the ORM identity map. touch_last_used selects the ORM entity. This fake
    mirrors that by returning column Rows for the column-selects and the object
    itself for the entity-select.
    """

    def __init__(self, pats, users, *, id_read_pat=None):
        self._pats = pats
        self._users = users
        # Optional override: the row the by-id re-read should return (to simulate
        # a revoke landing during bcrypt). Defaults to the matched pat itself.
        self._id_read_pat = id_read_pat
        self.committed = 0
        self.rolled_back = 0

    async def execute(self, stmt):
        sql = str(stmt).lower()
        where = str(getattr(stmt, "whereclause", "")).lower()
        params = stmt.compile().params
        uuid_binds = {v for v in params.values() if isinstance(v, uuid.UUID)}
        str_binds = {v for v in params.values() if isinstance(v, str)}

        if "personal_access_tokens" in sql:
            is_entity = "personal_access_tokens.token_hash" in sql and (
                "personal_access_tokens.label" in sql
                or "personal_access_tokens.created_at" in sql
            )
            # by-id re-read: raw columns user_id/revoked_at/expires_at
            if "personal_access_tokens.id =" in where and uuid_binds:
                src = self._id_read_pat
                if src is None:
                    src = next((p for p in self._pats if p.id in uuid_binds), None)
                if src is None:
                    return _Result([])
                return _Result([_Row(
                    user_id=src.user_id,
                    revoked_at=src.revoked_at,
                    expires_at=src.expires_at,
                )])
            # prefix lookup
            prefix = next(
                (b for b in str_binds if b.startswith(PAT_SCHEME_PREFIX)), None
            )
            rows = [p for p in self._pats if p.token_prefix == prefix]
            if uuid_binds:
                rows = [p for p in rows if p.user_id in uuid_binds]
            if "revoked_at is null" in where:
                rows = [p for p in rows if p.revoked_at is None]
            if is_entity:
                # touch_last_used: return the ORM entity (SimpleNamespace).
                return _Result(rows)
            # validate_pat prefix lookup: raw (id, token_hash), limit 1.
            return _Result([
                _Row(id=p.id, token_hash=p.token_hash) for p in rows[:1]
            ])
        if "local_users" in sql:
            rows = [u for u in self._users.values() if u.id in uuid_binds]
            return _Result(rows)
        raise AssertionError(f"unexpected query: {sql}")

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1


def _make_pat(*, token_hash, token_prefix, user_id, expires_at=None,
              revoked_at=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=token_hash,
        token_prefix=token_prefix,
        expires_at=expires_at,
        revoked_at=revoked_at,
        last_used_at=None,
    )


def _make_user(*, email="user@acme.test", is_active=True, role="member"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        username=email.split("@")[0],
        is_active=is_active,
        role=role,
    )


# ---------------------------------------------------------------------------
# generate_token / is_pat_form
# ---------------------------------------------------------------------------

def test_generated_token_shape_and_prefix():
    plaintext, prefix, token_hash = generate_token()
    assert plaintext.startswith(PAT_SCHEME_PREFIX)
    assert not plaintext.startswith("ey")
    assert is_pat_form(plaintext)
    assert prefix.startswith(PAT_SCHEME_PREFIX)
    assert plaintext.startswith(prefix)


def test_generated_hash_verifies_only_its_own_plaintext():
    plaintext, _prefix, token_hash = generate_token()
    assert verify_password(plaintext, token_hash) is True
    other, _p2, _h2 = generate_token()
    assert verify_password(other, token_hash) is False


def test_token_entropy_and_length_bound():
    a, _, _ = generate_token()
    b, _, _ = generate_token()
    assert a != b
    secret = a.split("_", 2)[2]
    assert len(secret) >= 43  # base64url of 32 bytes
    # Under bcrypt's 72-byte input limit — no silent truncation.
    assert len(a.encode("utf-8")) <= 72


def test_is_pat_form_rejects_non_pat():
    assert is_pat_form("hunter2") is False
    assert is_pat_form("ey.somejwt") is False
    assert is_pat_form("") is False


def test_is_pat_form_rejects_malformed_and_oversized():
    # Missing secret segment / no underscores => not a valid PAT.
    assert is_pat_form("tesspat_") is False
    assert is_pat_form("tesspat_abc") is False
    assert is_pat_form("tesspat_abc_") is False
    # Oversized (would trip bcrypt's 72-byte ValueError) => rejected cheaply.
    assert is_pat_form("tesspat_abc_" + "x" * 200) is False
    # A lone surrogate must be rejected cleanly, never raise (R2 finding 1).
    assert is_pat_form("tesspat_a_\ud800") is False
    # Delimiter-complete garbage: wrong public-id shape and non-base64url secret
    # must be rejected BEFORE any DB/bcrypt (R3 finding 1). The public id must be
    # exactly 12 lowercase hex; the secret must be base64url.
    assert is_pat_form("tesspat_z_not-base64!") is False          # bad public id + bad secret
    assert is_pat_form("tesspat_zzzzzzzzzzzz_abc") is False       # public id not hex
    assert is_pat_form("tesspat_ABCDEF012345_abc") is False       # public id not lowercase
    assert is_pat_form("tesspat_abcdef012345_ab!") is False       # secret not base64url
    # A trailing newline must NOT slip through (regex uses fullmatch, not $ +
    # match, which would admit a final \n) — R4 finding 1.
    assert is_pat_form("tesspat_abcdef012345\n_validSecret1") is False  # newline in public id
    assert is_pat_form("tesspat_abcdef012345_validSecret\n") is False   # newline in secret
    assert is_pat_form("tesspat_abcdef012345_abcDEF-_0") is True  # well-formed


def test_is_pat_scheme_vs_is_pat_form_routing():
    # SCHEME routing: anything tesspat_-prefixed is routed terminally to PAT,
    # even if it is malformed/oversized (so a bearer secret is never forwarded).
    assert is_pat_scheme("tesspat_") is True
    assert is_pat_scheme("tesspat_abc") is True
    assert is_pat_scheme("tesspat_abc_" + "x" * 200) is True
    # But those malformed values are NOT valid PATs.
    assert is_pat_form("tesspat_") is False
    assert is_pat_form("tesspat_abc") is False
    # Non-scheme is neither.
    assert is_pat_scheme("hunter2") is False
    assert is_pat_form("hunter2") is False


def test_hash_is_not_the_plaintext():
    plaintext, _prefix, token_hash = generate_token()
    assert plaintext not in token_hash


# ---------------------------------------------------------------------------
# validate_pat — happy path + every rejection branch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_pat_success_resolves_owner_without_mutation():
    plaintext, prefix, token_hash = generate_token()
    user = _make_user()
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=user.id)
    db = _FakeDB([pat], {user.id: user})
    resolved = await validate_pat(db, token=plaintext, email=user.email)
    assert resolved is user
    # validate_pat must NOT touch last_used_at or commit anything.
    assert pat.last_used_at is None
    assert db.committed == 0


@pytest.mark.asyncio
async def test_validate_pat_rejects_non_pat_string():
    assert await validate_pat(_FakeDB([], {}), token="not-a-pat") is None


@pytest.mark.asyncio
async def test_validate_pat_rejects_oversized_without_raising():
    # A very long tesspat_-string must be rejected cleanly (no bcrypt ValueError
    # that would become a 503 upstream).
    assert await validate_pat(_FakeDB([], {}), token="tesspat_a_" + "z" * 300) is None


@pytest.mark.asyncio
async def test_validate_pat_rejects_unknown_prefix():
    plaintext, _prefix, _hash = generate_token()
    assert await validate_pat(_FakeDB([], {}), token=plaintext) is None


@pytest.mark.asyncio
async def test_validate_pat_rejects_wrong_secret_same_prefix():
    plaintext, prefix, token_hash = generate_token()
    user = _make_user()
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=user.id)
    db = _FakeDB([pat], {user.id: user})
    forged = f"{prefix}_forgedsecretforgedsecretforgedsecretforged"
    assert await validate_pat(db, token=forged, email=user.email) is None


@pytest.mark.asyncio
async def test_validate_pat_rejects_revoked_token():
    plaintext, prefix, token_hash = generate_token()
    user = _make_user()
    pat = _make_pat(
        token_hash=token_hash, token_prefix=prefix, user_id=user.id,
        revoked_at=datetime.now(timezone.utc),
    )
    db = _FakeDB([pat], {user.id: user})
    assert await validate_pat(db, token=plaintext, email=user.email) is None


@pytest.mark.asyncio
async def test_validate_pat_rejects_expired_token():
    plaintext, prefix, token_hash = generate_token()
    user = _make_user()
    pat = _make_pat(
        token_hash=token_hash, token_prefix=prefix, user_id=user.id,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db = _FakeDB([pat], {user.id: user})
    assert await validate_pat(db, token=plaintext, email=user.email) is None


@pytest.mark.asyncio
async def test_validate_pat_accepts_future_expiry():
    plaintext, prefix, token_hash = generate_token()
    user = _make_user()
    pat = _make_pat(
        token_hash=token_hash, token_prefix=prefix, user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db = _FakeDB([pat], {user.id: user})
    assert await validate_pat(db, token=plaintext, email=user.email) is user


@pytest.mark.asyncio
async def test_validate_pat_toctou_recheck_after_bcrypt():
    # Simulate a revocation that lands DURING bcrypt: the initial prefix-lookup
    # candidate is active, but the post-bcrypt by-id re-read returns the LIVE
    # (revoked) state. validate_pat re-reads raw columns, so the identity map
    # cannot serve the stale object.
    plaintext, prefix, token_hash = generate_token()
    user = _make_user()
    active = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=user.id)
    revoked_view = types.SimpleNamespace(
        id=active.id, user_id=user.id, token_hash=token_hash,
        token_prefix=prefix, expires_at=None,
        revoked_at=datetime.now(timezone.utc), last_used_at=None,
    )
    # The by-id re-read returns the revoked view; the prefix lookup still sees
    # the active candidate (bcrypt happens against it first).
    db = _FakeDB([active], {user.id: user}, id_read_pat=revoked_view)
    assert await validate_pat(db, token=plaintext, email=user.email) is None


@pytest.mark.asyncio
async def test_validate_pat_single_bcrypt_for_miss_and_wrong_secret(monkeypatch):
    # External review R2 finding 3: exactly ONE bcrypt per validation for both a
    # prefix-miss and a wrong-secret-with-matching-prefix, so response timing
    # does not reveal whether the prefix exists.
    import src.auth.pat as pat_mod

    calls = {"n": 0}
    real_verify = pat_mod.verify_password

    def _counting_verify(plain, hashed):
        calls["n"] += 1
        return real_verify(plain, hashed)

    monkeypatch.setattr(pat_mod, "verify_password", _counting_verify)

    plaintext, prefix, token_hash = generate_token()
    user = _make_user()
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=user.id)

    # prefix-miss (no candidate)
    calls["n"] = 0
    await validate_pat(_FakeDB([], {}), token=plaintext)
    assert calls["n"] == 1

    # wrong secret with an EXISTING prefix
    calls["n"] = 0
    forged = f"{prefix}_forgedsecretforgedsecretforgedsecretforgedxx"
    await validate_pat(_FakeDB([pat], {user.id: user}), token=forged, email=user.email)
    assert calls["n"] == 1

    # correct secret
    calls["n"] = 0
    await validate_pat(_FakeDB([pat], {user.id: user}), token=plaintext, email=user.email)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_validate_pat_garbage_does_no_db_or_bcrypt(monkeypatch):
    # R3 finding 1: a delimiter-complete but malformed PAT must be rejected by
    # is_pat_form BEFORE any DB query or bcrypt verify.
    import src.auth.pat as pat_mod

    verify_calls = {"n": 0}
    monkeypatch.setattr(
        pat_mod, "verify_password",
        lambda p, h: verify_calls.__setitem__("n", verify_calls["n"] + 1),
    )

    class _NoQueryDB:
        async def execute(self, stmt):
            raise AssertionError("no DB query expected for malformed PAT")

    assert await validate_pat(_NoQueryDB(), token="tesspat_z_not-base64!") is None
    # A newline-tainted public id must also do zero DB/verify (R4 finding 1).
    assert await validate_pat(
        _NoQueryDB(), token="tesspat_abcdef012345\n_validSecret1"
    ) is None
    assert verify_calls["n"] == 0


@pytest.mark.asyncio
async def test_validate_pat_non_ascii_email_binding():
    # R3 finding 2: a non-ASCII (internationalized) owner email must compare
    # correctly (match AND mismatch), never raise.
    plaintext, prefix, token_hash = generate_token()
    owner = _make_user(email="josé@acme.test")
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=owner.id)
    db = _FakeDB([pat], {owner.id: owner})
    assert await validate_pat(db, token=plaintext, email="JOSÉ@acme.test") is owner
    db2 = _FakeDB([pat], {owner.id: owner})
    assert await validate_pat(db2, token=plaintext, email="bob@acme.test") is None


@pytest.mark.asyncio
async def test_validate_pat_rejects_inactive_owner():
    plaintext, prefix, token_hash = generate_token()
    user = _make_user(is_active=False)
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=user.id)
    db = _FakeDB([pat], {user.id: user})
    assert await validate_pat(db, token=plaintext, email=user.email) is None


@pytest.mark.asyncio
async def test_validate_pat_rejects_missing_owner():
    plaintext, prefix, token_hash = generate_token()
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=uuid.uuid4())
    db = _FakeDB([pat], {})
    assert await validate_pat(db, token=plaintext) is None


@pytest.mark.asyncio
async def test_validate_pat_rejects_email_mismatch():
    plaintext, prefix, token_hash = generate_token()
    owner = _make_user(email="alice@acme.test")
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=owner.id)
    db = _FakeDB([pat], {owner.id: owner})
    assert await validate_pat(db, token=plaintext, email="bob@acme.test") is None
    db2 = _FakeDB([pat], {owner.id: owner})
    assert await validate_pat(db2, token=plaintext, email="ALICE@ACME.TEST") is owner


# ---------------------------------------------------------------------------
# touch_last_used (post-success telemetry)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_touch_last_used_updates_active_token():
    _pt, prefix, token_hash = generate_token()
    user = _make_user()
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=user.id)
    db = _FakeDB([pat], {user.id: user})
    await touch_last_used(db, token_prefix=prefix, user_id=user.id)
    assert pat.last_used_at is not None
    assert db.committed == 1


@pytest.mark.asyncio
async def test_touch_last_used_swallows_errors():
    class _BoomDB:
        async def execute(self, stmt):
            raise RuntimeError("db down")

        async def rollback(self):
            return None

    # Must not raise — telemetry failure can never reject a valid login.
    await touch_last_used(_BoomDB(), token_prefix="tesspat_x", user_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# PAT auth backend
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pat_backend_returns_none_for_non_pat_without_db(monkeypatch):
    from src.auth.pat_backend import PatAuthBackend

    called = {"db": False}

    async def _boom(*a, **k):
        called["db"] = True
        yield None

    monkeypatch.setattr("src.auth.pat_backend.get_tenant_db", _boom)
    backend = PatAuthBackend()
    identity = await backend.authenticate(
        tenant_id="acme", email="a@b.com", password="plainpassword"
    )
    assert identity is None
    assert called["db"] is False


@pytest.mark.asyncio
async def test_pat_backend_success_touches_and_maps_identity(monkeypatch):
    from src.auth import pat_backend as pb

    plaintext, prefix, token_hash = generate_token()
    user = _make_user(role="tenant_admin")
    pat = _make_pat(token_hash=token_hash, token_prefix=prefix, user_id=user.id)
    db = _FakeDB([pat], {user.id: user})

    async def _fake_tenant_db(tenant_id):
        yield db

    monkeypatch.setattr(pb, "get_tenant_db", _fake_tenant_db)
    backend = pb.PatAuthBackend()
    identity = await backend.authenticate(
        tenant_id="acme", email=user.email, password=plaintext
    )
    assert identity is not None
    assert identity.source_backend == "pat"
    assert identity.email == user.email
    assert identity.raw_claims["role"] == "tenant_admin"
    # last_used_at recorded via touch_last_used AFTER success.
    assert pat.last_used_at is not None
    assert db.committed == 1


def test_pat_is_not_external_identity():
    from shared.auth.backend import UserIdentity
    from src.auth.jit import is_external_identity

    assert is_external_identity(UserIdentity(email="u@acme.test", source_backend="pat")) is False


# ---------------------------------------------------------------------------
# Terminal auth chain — a PAT-shaped credential must NOT fall through
# ---------------------------------------------------------------------------

class _RecordingBackend:
    name = "recording"

    def __init__(self):
        self.seen: list[str] = []

    async def authenticate(self, *, tenant_id, email, password, **kwargs):
        self.seen.append(password)
        return None


class _PatStub:
    name = "pat"

    def __init__(self, identity=None):
        self._identity = identity
        self.calls = 0

    async def authenticate(self, *, tenant_id, email, password, **kwargs):
        self.calls += 1
        return self._identity


@pytest.mark.asyncio
async def test_terminal_chain_pat_shape_does_not_reach_other_backends():
    from shared.auth.backend import UserIdentity
    from src.auth.chain import PatTerminalAuthChain

    pat = _PatStub(identity=None)  # PAT rejects
    ldap_like = _RecordingBackend()
    chain = PatTerminalAuthChain([pat, ldap_like])

    pat_pw = "tesspat_ab12cd34_secretsecretsecretsecretsecretsecret1"
    # authenticate: a PAT-shaped password must be terminal on rejection.
    result = await chain.authenticate(tenant_id="t", email="u@x", password=pat_pw)
    assert result is None
    assert ldap_like.seen == []  # the PAT never reached the other backend

    # authenticate_outcome: same terminal semantics.
    outcome = await chain.authenticate_outcome(tenant_id="t", email="u@x", password=pat_pw)
    assert outcome.status == "rejected"
    assert ldap_like.seen == []


@pytest.mark.asyncio
async def test_terminal_chain_malformed_pat_scheme_still_terminal():
    # External review R2 finding 1: a MALFORMED / OVERSIZED tesspat_-string must
    # still be terminal (routed to PAT only) so the bearer secret is never
    # forwarded to LDAP/local. Routing is by scheme, not by format validity.
    from src.auth.chain import PatTerminalAuthChain

    ldap_like = _RecordingBackend()
    chain = PatTerminalAuthChain([_PatStub(identity=None), ldap_like])

    for bad in (
        "tesspat_",                       # no secret
        "tesspat_abc",                    # no secret segment
        "tesspat_ab12cd34_" + "x" * 300,  # oversized (would break bcrypt/LDAP)
    ):
        ldap_like.seen.clear()
        result = await chain.authenticate(tenant_id="t", email="u@x", password=bad)
        assert result is None
        outcome = await chain.authenticate_outcome(tenant_id="t", email="u@x", password=bad)
        assert outcome.status == "rejected"
        assert ldap_like.seen == []  # never forwarded to the directory bind


@pytest.mark.asyncio
async def test_terminal_chain_non_pat_still_reaches_other_backends():
    from src.auth.chain import PatTerminalAuthChain

    pat = _PatStub(identity=None)
    ldap_like = _RecordingBackend()
    chain = PatTerminalAuthChain([pat, ldap_like])

    await chain.authenticate(tenant_id="t", email="u@x", password="normalpw")
    # A normal password is offered to the downstream backend as usual.
    assert ldap_like.seen == ["normalpw"]
