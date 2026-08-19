"""Bug-6597: SSO-elevated tenant_admin must be reconciled DOWN on de-provision,
without clobbering a manually-promoted admin, and the tenant can never be left
with zero admins (last-admin guard).

Covers three surfaces:
  * ``jit._reconcile_sso_tenant_role`` (the demote/elevate decision),
  * ``jit.jit_adopt_user`` end-to-end for a returning de-provisioned admin, and
  * the user-management API last-admin guard on PATCH/DELETE /auth/users.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.auth.backend import UserIdentity
from src.auth.middleware import CurrentUser, require_tenant_admin
from src.main import app

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, scalar=None, scalar_one=None, all_=None, rows=None):
        self._scalar = scalar
        self._scalar_one = scalar_one
        self._all = all_ or []
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar_one

    def all(self):
        # Row tuples for the combined-lock guard (id, role, is_active).
        return self._rows

    def scalars(self):
        s = MagicMock()
        s.all.return_value = self._all
        return s


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []
        self.added = []
        self.deleted = []
        self.committed = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        if str(stmt).startswith("UPDATE local_users"):
            return _FakeResult(scalar_one=1)
        if self._results:
            return self._results.pop(0)
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def flush(self):
        pass

    async def refresh(self, obj):
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        obj.created_at = NOW


def _admin_user(email="admin@corp.com", role="tenant_admin", role_source="sso"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        username=email.split("@")[0],
        email=email,
        is_active=True,
        role=role,
        role_source=role_source,
        auth_source="oidc",
        token_version=0,
        created_at=NOW,
    )


# ---------------------------------------------------------------------------
# _reconcile_sso_tenant_role — the demote/elevate decision
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sso_admin_demoted_when_group_removed_and_other_admin_exists():
    """ROOT CAUSE: an SSO-elevated tenant_admin whose IdP admin group is gone is
    reconciled DOWN to the JIT default (viewer) — the privilege no longer
    lingers forever."""
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role_source="sso")
    db = _FakeDB([
        # locked active-admin ids: one OTHER admin present -> exists
        _FakeResult(all_=[uuid.uuid4()]),
        _FakeResult(scalar_one=None),   # jit default setting -> viewer
    ])

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role=None, tenant_group_role=None,
        groups=["employees"],  # authoritative, non-empty, no admin group
    )

    assert user.role == "viewer", "SSO admin must be demoted when group removed"
    assert user.role_source == "sso"
    assert user.token_version == 1


@pytest.mark.asyncio
async def test_manual_admin_never_demoted():
    """A MANUALLY-promoted admin (role_source='manual') is never auto-demoted,
    even when the IdP grants no admin group — operator intent outranks SSO."""
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role_source="manual")
    db = _FakeDB([])  # no query should run at all

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role=None, tenant_group_role=None,
        groups=["employees"],
    )

    assert user.role == "tenant_admin", "manual admin must be preserved"
    assert user.role_source == "manual"
    assert db.executed == [], "manual admin path must not query the DB"


@pytest.mark.asyncio
async def test_last_sso_admin_not_demoted_last_admin_guard():
    """Last-admin guard: if no other active tenant_admin exists, the SSO admin is
    RETAINED rather than demoted, so the tenant is never locked out."""
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role_source="sso")
    db = _FakeDB([
        # locked active-admin ids: only the user themselves -> no OTHER admin
        _FakeResult(all_=[user.id]),
    ])

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role=None, tenant_group_role=None,
        groups=["employees"],
    )

    assert user.role == "tenant_admin", "last admin must be retained"


@pytest.mark.asyncio
async def test_empty_groups_never_demote():
    """Fail-closed: an empty/undetermined group set (IdP omits group claims) must
    NOT demote — it is not authoritative."""
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role_source="sso")
    db = _FakeDB([])

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role=None, tenant_group_role=None, groups=[],
    )

    assert user.role == "tenant_admin"
    assert db.executed == [], "empty group set must not trigger any query"


@pytest.mark.asyncio
async def test_returning_member_elevated_and_stamped_sso():
    """Elevation still works: a returning non-admin whose group now maps to admin
    is elevated to tenant_admin and stamped role_source='sso'."""
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role="viewer", role_source="sso")
    db = _FakeDB([])

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role="tenant_admin", tenant_group_role=None,
        groups=["admins"],
    )

    assert user.role == "tenant_admin"
    assert user.role_source == "sso"
    assert user.token_version == 1


@pytest.mark.asyncio
async def test_sso_admin_demoted_to_remaining_group_role():
    """When the admin group is gone but another tenant-wide mapping remains, the
    admin is demoted to THAT role (not the bare JIT default)."""
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role_source="sso")
    db = _FakeDB([
        _FakeResult(all_=[uuid.uuid4()]),  # another admin exists
    ])

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role=None, tenant_group_role="model_technical",
        groups=["staff"],
    )

    # base_role came from the remaining tenant-wide group role, not the JIT
    # default (which would have been viewer) — proving the group role wins.
    assert user.role == "model_technical"
    assert user.token_version == 1


# ---------------------------------------------------------------------------
# tenant_admin_guard — exclude-self / last-admin arithmetic + FOR UPDATE lock
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_guard_excludes_self_and_takes_row_lock():
    from src.auth.tenant_admin_guard import (
        count_active_tenant_admins,
        other_active_tenant_admin_exists,
    )

    self_id = uuid.uuid4()
    other_id = uuid.uuid4()

    # Two active admins (self + one other): excluding self leaves 1 -> other exists.
    db = _FakeDB([_FakeResult(all_=[self_id, other_id])])
    assert await other_active_tenant_admin_exists(db, self_id) is True
    # The locking SELECT must carry FOR UPDATE so concurrent removals serialise.
    locked_sql = str(db.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in locked_sql.upper()

    # Only self is an active admin: excluding self leaves 0 -> no other admin.
    db2 = _FakeDB([_FakeResult(all_=[self_id])])
    assert await other_active_tenant_admin_exists(db2, self_id) is False

    # Total count is not reduced when no exclusion is requested.
    db3 = _FakeDB([_FakeResult(all_=[self_id, other_id])])
    assert await count_active_tenant_admins(db3) == 2


@pytest.mark.asyncio
async def test_applying_change_orphans_tenant_before_after():
    """Bug-6640 combined-lock guard: block only when the change drops the LAST
    active admin (before>0 and after==0); never on an already-adminless tenant."""
    from src.auth.tenant_admin_guard import applying_change_orphans_tenant

    tid = uuid.uuid4()
    other = uuid.uuid4()

    # Demoting the only admin -> orphans.
    db = _FakeDB([_FakeResult(rows=[(tid, "tenant_admin", True)])])
    assert await applying_change_orphans_tenant(db, tid, new_role="member") is True
    # The guard SELECT must be FOR UPDATE and cover the target row explicitly
    # (the `OR id = <target>` disjunct is what makes the target lock, not just
    # the active-admin set).
    sql = str(db.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in sql.upper()
    assert tid.hex in sql.replace("-", ""), "guard must lock the target row by id"
    assert "OR local_users.id" in sql, "target row must be in the lock via OR id = target"

    # Demoting one of two admins -> safe.
    db2 = _FakeDB([_FakeResult(
        rows=[(tid, "tenant_admin", True), (other, "tenant_admin", True)])])
    assert await applying_change_orphans_tenant(db2, tid, new_role="member") is False

    # Deactivating the last admin -> orphans.
    db3 = _FakeDB([_FakeResult(rows=[(tid, "tenant_admin", True)])])
    assert await applying_change_orphans_tenant(db3, tid, new_is_active=False) is True

    # Deleting a NON-admin while the tenant already has zero admins -> NOT blocked
    # (the change is not what orphaned it; before==0).
    db4 = _FakeDB([_FakeResult(rows=[(tid, "member", True)])])
    assert await applying_change_orphans_tenant(db4, tid, new_is_active=False) is False

    # Reactivating a currently-INACTIVE admin -> adds an admin, never blocked
    # (before=0 because the target is inactive; after=1).
    db5 = _FakeDB([_FakeResult(rows=[(tid, "tenant_admin", False)])])
    assert await applying_change_orphans_tenant(db5, tid, new_is_active=True) is False


@pytest.mark.asyncio
async def test_sso_demotion_writes_audit_event():
    """Bug-6641: an automatic SSO revocation leaves a queryable audit trail."""
    from shared.db.models import AuditEvent
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role_source="sso")
    db = _FakeDB([
        _FakeResult(all_=[uuid.uuid4()]),  # another admin exists
        _FakeResult(scalar_one=None),      # jit default -> viewer
        _FakeResult(scalar_one=None),      # audit-level lookup -> info
    ])

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role=None, tenant_group_role=None,
        groups=["employees"],
    )

    events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(events) == 1, "SSO demotion must write exactly one audit event"
    ev = events[0]
    assert ev.action == "user.sso_role_reconcile"
    assert ev.severity == "critical"
    assert ev.detail["outcome"] == "revoke"
    assert ev.detail["from_role"] == "tenant_admin"
    assert ev.detail["to_role"] == "viewer"


@pytest.mark.asyncio
async def test_sso_elevation_writes_audit_event():
    """Bug-6641: a returning user auto-elevated to tenant_admin is audited (warn)."""
    from shared.db.models import AuditEvent
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role="viewer", role_source="sso")
    db = _FakeDB([_FakeResult(scalar_one=None)])  # audit-level lookup only

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role="tenant_admin", tenant_group_role=None,
        groups=["admins"],
    )

    assert user.role == "tenant_admin"
    events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(events) == 1
    assert events[0].action == "user.sso_role_reconcile"
    assert events[0].severity == "warn"
    assert events[0].detail["outcome"] == "elevate"
    assert events[0].detail["from_role"] == "viewer"
    assert events[0].detail["to_role"] == "tenant_admin"


@pytest.mark.asyncio
async def test_last_admin_refusal_writes_audit_event():
    """Bug-6641: a refused last-admin revocation is audited as a critical event so
    operators can see the tenant is running on its last admin."""
    from shared.db.models import AuditEvent
    from src.auth.jit import _reconcile_sso_tenant_role

    user = _admin_user(role_source="sso")
    db = _FakeDB([
        _FakeResult(all_=[user.id]),   # only self -> no other admin -> refuse
        _FakeResult(scalar_one=None),  # audit-level lookup
    ])

    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role=None, tenant_group_role=None,
        groups=["employees"],
    )

    assert user.role == "tenant_admin", "last admin retained"
    events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(events) == 1
    assert events[0].severity == "critical"
    assert events[0].detail["outcome"] == "revoke_refused_last_admin"


# ---------------------------------------------------------------------------
# jit_adopt_user — end-to-end returning de-provisioned admin
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_jit_adopt_returning_admin_loses_role_on_deprovision():
    """End-to-end: a returning SSO admin whose groups no longer map to admin (but
    remain non-empty) is demoted by jit_adopt_user."""
    from src.auth.jit import jit_adopt_user

    existing = _admin_user(email="boss@corp.com", role="tenant_admin", role_source="sso")
    identity = UserIdentity(
        email="boss@corp.com", display_name="Boss",
        groups=["employees"], source_backend="oidc", raw_claims={},
    )
    # Query order: (1) user lookup -> existing; (2) resolve_group_role mapping
    # lookup -> no mappings; (3) reconcile: other-admin count -> 1; (4) jit
    # default setting -> None; (5) audit-level lookup (Bug-6641 reconcile audit);
    # (6) _sync_group_bindings project mappings -> none; (7) revocation scan.
    db = _FakeDB([
        _FakeResult(scalar_one=existing),  # 1 user lookup
        _FakeResult(all_=[]),              # 2 tenant-wide group mappings (none)
        _FakeResult(all_=[uuid.uuid4()]),  # 3 locked admin ids: another admin
        _FakeResult(scalar_one=None),      # 4 jit default -> viewer
        _FakeResult(scalar_one=None),      # 5 audit-level lookup -> info
        _FakeResult(all_=[]),              # 6 project-scoped mappings
        _FakeResult(all_=[]),              # 7 sso_group revocation scan
    ])

    user, role = await jit_adopt_user(db, identity, "acme")

    assert role == "viewer", "de-provisioned SSO admin must lose tenant_admin"
    assert user.role == "viewer"
    assert db.committed is True


# ---------------------------------------------------------------------------
# API last-admin guard: PATCH / DELETE /auth/users
# ---------------------------------------------------------------------------
async def _yield(v):
    yield v


def _override_admin():
    app.dependency_overrides[require_tenant_admin] = lambda: CurrentUser(
        user_id="admin@corp.com", tenant_id="acme",
        email="admin@corp.com", role="tenant_admin",
    )


@pytest.mark.asyncio
async def test_delete_last_tenant_admin_blocked():
    target = _admin_user(email="only-admin@corp.com", role="tenant_admin")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=target)
    # combined-lock guard rows: the target is the ONLY active admin
    mock_db.execute = AsyncMock(
        return_value=_FakeResult(rows=[(target.id, "tenant_admin", True)])
    )

    _override_admin()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.delete(f"/api/v1/auth/users/{target.id}")
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 409
    assert "last tenant administrator" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_admin_allowed_when_other_admin_exists():
    target = _admin_user(email="admin-a@corp.com", role="tenant_admin")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=target)
    # combined-lock guard rows: another active admin exists besides the target
    mock_db.execute = AsyncMock(return_value=_FakeResult(
        rows=[(target.id, "tenant_admin", True), (uuid.uuid4(), "tenant_admin", True)],
    ))
    mock_db.delete = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    _override_admin()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
                patch("src.api.auth.emit_webhook", AsyncMock()),
            ):
                resp = await ac.delete(f"/api/v1/auth/users/{target.id}")
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_ordinary_member_not_blocked():
    """Deleting an ordinary (non-admin) user is never blocked by the last-admin
    guard, even though the guard now runs on every delete (Bug-6640)."""
    target = _admin_user(email="member@corp.com", role="member")
    admin_id = uuid.uuid4()
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=target)
    # An admin exists elsewhere; the target is a member -> before=1, after=1.
    mock_db.execute = AsyncMock(return_value=_FakeResult(
        rows=[(admin_id, "tenant_admin", True), (target.id, "member", True)],
    ))
    mock_db.delete = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    _override_admin()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)),
                patch("src.api.auth.emit_webhook", AsyncMock()),
            ):
                resp = await ac.delete(f"/api/v1/auth/users/{target.id}")
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 204
    mock_db.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_demote_last_tenant_admin_blocked():
    target = _admin_user(email="only-admin@corp.com", role="tenant_admin")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=target)
    # combined-lock guard rows: target is the ONLY active admin
    mock_db.execute = AsyncMock(
        return_value=_FakeResult(rows=[(target.id, "tenant_admin", True)])
    )

    _override_admin()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.patch(
                    f"/api/v1/auth/users/{target.id}", json={"role": "member"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 409
    assert "last tenant administrator" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_deactivate_last_tenant_admin_blocked():
    target = _admin_user(email="only-admin@corp.com", role="tenant_admin")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=target)
    # combined-lock guard rows: target is the ONLY active admin
    mock_db.execute = AsyncMock(
        return_value=_FakeResult(rows=[(target.id, "tenant_admin", True)])
    )

    _override_admin()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.patch(
                    f"/api/v1/auth/users/{target.id}", json={"is_active": False},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_operator_role_change_stamps_role_source_manual():
    """An operator setting a role (here promoting member -> tenant_admin) marks
    role_source='manual' so the SSO reconcile path can never auto-demote it
    afterward — operator intent outranks SSO provenance."""
    target = _admin_user(email="admin-a@corp.com", role="member", role_source="sso")
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=target)
    # Promotion never orphans the tenant; then the token-version bump returns
    # the durable version and the audit-level lookup returns no override.
    mock_db.execute = AsyncMock(side_effect=[
        _FakeResult(rows=[(target.id, "member", True)]),
        _FakeResult(scalar_one=1),
        _FakeResult(scalar_one=None),
    ])
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    async def _refresh(obj):
        pass
    mock_db.refresh = _refresh

    _override_admin()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.auth.get_tenant_db", lambda tid: _yield(mock_db)):
                resp = await ac.patch(
                    f"/api/v1/auth/users/{target.id}", json={"role": "tenant_admin"},
                )
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)

    assert resp.status_code == 200
    assert target.role == "tenant_admin"
    assert target.role_source == "manual", "operator-set role must be manual"


# ---------------------------------------------------------------------------
# Bug-6639: grandfathered SSO admin reconciliation after migration 0170
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_grandfathered_sso_admin_demoted_after_role_source_backfill():
    """Bug-6639: a pre-0156 SSO tenant_admin who received role_source='manual' as
    the server_default is reconciled DOWN after migration 0170 re-stamps their
    role_source to 'sso'.

    Before 0170, the reconcile path skipped this user (manual admins are never
    auto-demoted). After 0170, role_source='sso' makes the user eligible for
    Case 2 demotion when their IdP admin group disappears.

    Test escape: no test previously covered the transition from grandfathered
    'manual' (external auth_source) to 'sso' (post-backfill).
    Guard: this test. Tier: T1 (producer/consumer contract).
    """
    from src.auth.jit import _reconcile_sso_tenant_role

    # Simulate a post-0170 state: the user's role_source has been corrected
    # from 'manual' to 'sso' by the migration.
    user = _admin_user(role_source="sso")
    user.auth_source = "saml"  # external IdP -- was pre-0156

    db = _FakeDB([
        _FakeResult(all_=[uuid.uuid4()]),  # another admin exists
        _FakeResult(scalar_one=None),      # jit default -> viewer
        _FakeResult(scalar_one=None),      # audit-level lookup
    ])

    # Groups are non-empty but do NOT include an admin-mapped group.
    await _reconcile_sso_tenant_role(
        db, user, mapped_tenant_role=None, tenant_group_role=None,
        groups=["employees"],
    )

    assert user.role == "viewer", (
        "Bug-6639: grandfathered SSO admin must be demotable after backfill"
    )
    assert user.role_source == "sso"
