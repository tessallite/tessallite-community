"""Tests for the auth backend protocol and chain."""
import pytest

from shared.auth.backend import AuthBackend, AuthChain, UserIdentity


class FakeSuccessBackend:
    name = "fake_success"

    async def authenticate(self, *, tenant_id, email, password, **kwargs):
        return UserIdentity(
            email=email,
            display_name="Fake User",
            source_backend=self.name,
        )


class FakeFailBackend:
    name = "fake_fail"

    async def authenticate(self, *, tenant_id, email, password, **kwargs):
        return None


class FakeErrorBackend:
    name = "fake_error"

    async def authenticate(self, *, tenant_id, email, password, **kwargs):
        raise RuntimeError("LDAP unreachable")


@pytest.mark.asyncio
async def test_chain_first_success_wins():
    chain = AuthChain([FakeFailBackend(), FakeSuccessBackend()])
    identity = await chain.authenticate(
        tenant_id="t1", email="a@b.com", password="pw"
    )
    assert identity is not None
    assert identity.source_backend == "fake_success"


@pytest.mark.asyncio
async def test_chain_all_fail_returns_none():
    chain = AuthChain([FakeFailBackend(), FakeFailBackend()])
    identity = await chain.authenticate(
        tenant_id="t1", email="a@b.com", password="pw"
    )
    assert identity is None


@pytest.mark.asyncio
async def test_chain_skips_erroring_backend():
    chain = AuthChain([FakeErrorBackend(), FakeSuccessBackend()])
    identity = await chain.authenticate(
        tenant_id="t1", email="a@b.com", password="pw"
    )
    assert identity is not None
    assert identity.source_backend == "fake_success"


@pytest.mark.asyncio
async def test_chain_error_only_returns_none():
    chain = AuthChain([FakeErrorBackend()])
    identity = await chain.authenticate(
        tenant_id="t1", email="a@b.com", password="pw"
    )
    assert identity is None


def test_backend_names():
    chain = AuthChain([FakeFailBackend(), FakeSuccessBackend()])
    assert chain.backend_names == ["fake_fail", "fake_success"]


def test_user_identity_fields():
    ui = UserIdentity(
        email="test@example.com",
        display_name="Test User",
        groups=["admin"],
        source_backend="local",
        raw_claims={"role": "admin"},
    )
    assert ui.email == "test@example.com"
    assert ui.display_name == "Test User"
    assert ui.groups == ["admin"]
    assert ui.source_backend == "local"
    assert ui.raw_claims["role"] == "admin"


def test_fake_backend_satisfies_protocol():
    assert isinstance(FakeSuccessBackend(), AuthBackend)
