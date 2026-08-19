"""Last-tenant-admin guard (Bug-6597).

A tenant must never be left with zero usable ``tenant_admin`` accounts. Losing
the last admin locks the tenant out of user/project/model administration — only
the env-hardcoded system admin could then recover it. This module centralises
the "is there another active admin?" check so every path that could remove or
demote a tenant_admin enforces the same invariant:

* the SSO reconcile path in ``jit.jit_adopt_user`` (an IdP de-provisioning that
  would strip the last admin is refused — the admin is kept), and
* the admin-facing user-management API (``PATCH``/``DELETE`` /auth/users) which
  rejects the operation with a 4xx.

"Active" matters: a deactivated admin cannot log in, so it does not count toward
the tenant's usable-admin quota. ``system_admin`` is env-provisioned and lives
outside ``local_users``, so it is intentionally not counted here.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.roles import TENANT_ADMIN_ROLE
from shared.db.models import LocalUser


async def _locked_active_tenant_admin_ids(db: AsyncSession) -> list[uuid.UUID]:
    """Ids of all active ``tenant_admin`` rows, taking a ``FOR UPDATE`` row lock
    in a CONSISTENT (id-ordered) order.

    The consistent lock order is what makes the last-admin guard atomic: two
    concurrent removals of two different admins would otherwise both read
    "another admin still exists" (check-then-act TOCTOU) and both proceed,
    leaving the tenant with zero admins. Locking the whole active-admin set in a
    fixed order forces the transactions to serialise — the second one blocks,
    then re-reads the now-smaller set and is correctly refused. Ordering by id
    (rather than lock-on-demand) also avoids a lock-ordering deadlock.

    On the mocked unit-test sessions the ``FOR UPDATE`` clause is inert; on the
    real tenant session it holds the row locks for the enclosing transaction.
    """
    stmt = (
        select(LocalUser.id)
        .where(
            LocalUser.role == TENANT_ADMIN_ROLE,
            LocalUser.is_active.is_(True),
        )
        .order_by(LocalUser.id)
        .with_for_update()
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_active_tenant_admins(
    db: AsyncSession, exclude_user_id=None
) -> int:
    """Number of active ``tenant_admin`` rows in the tenant, optionally
    excluding one user id (the target of the operation under evaluation).

    Takes a row lock on the active-admin set so the count is stable for the rest
    of the transaction (closes the last-admin TOCTOU race)."""
    ids = await _locked_active_tenant_admin_ids(db)
    if exclude_user_id is not None:
        ids = [i for i in ids if i != exclude_user_id]
    return len(ids)


async def other_active_tenant_admin_exists(
    db: AsyncSession, exclude_user_id
) -> bool:
    """True if at least one *other* active tenant_admin exists (i.e. removing or
    demoting ``exclude_user_id`` would NOT leave the tenant with zero admins).

    The underlying query row-locks the active-admin set, so a concurrent removal
    cannot slip between this check and the caller's mutation.

    Used by the SSO reconcile path, where the target is a *confirmed current*
    ``tenant_admin`` and is therefore already inside the locked set. The admin-
    facing API paths use :func:`applying_change_orphans_tenant` instead, because
    there the target's role/active state is read from an unlocked ``db.get`` and
    must be re-evaluated under the lock (Bug-6640)."""
    return await count_active_tenant_admins(db, exclude_user_id=exclude_user_id) > 0


# Sentinel: "this field is not being changed by the pending operation".
_UNSET = object()


async def applying_change_orphans_tenant(
    db: AsyncSession,
    target_id: uuid.UUID,
    *,
    new_role=_UNSET,
    new_is_active=_UNSET,
) -> bool:
    """Would applying the pending change to ``target_id`` leave the tenant with
    zero active ``tenant_admin`` accounts?

    Closes the residual target-row TOCTOU (Bug-6640): the admin-facing API reads
    the target via an unlocked ``db.get``, so a concurrent promotion/demotion of
    the target could make the stale ``removes_admin`` decision wrong. This locks
    the UNION of (all active tenant_admins) and (the target row) in a SINGLE
    ``ORDER BY id ... FOR UPDATE`` statement — one statement acquires its row
    locks in id order, so two concurrent guards serialise with no lock-ordering
    deadlock — then computes the post-change active-admin count from the freshly
    locked rows (never the caller's stale snapshot).

    ``new_role`` / ``new_is_active`` express the pending mutation:
      * a role change   -> ``new_role=<role>``
      * a deactivation  -> ``new_is_active=False``
      * a deletion      -> ``new_is_active=False`` (the row contributes zero)
    Fields left ``_UNSET`` keep their locked current value.

    Returns True iff the tenant currently HAS at least one active admin and the
    change would drop that to zero. If the tenant already has zero active admins
    (a degenerate state recoverable only via the system admin), the change is not
    what orphaned it, so this returns False — e.g. deleting an unrelated
    non-admin user is never blocked."""
    stmt = (
        select(LocalUser.id, LocalUser.role, LocalUser.is_active)
        .where(
            (
                (LocalUser.role == TENANT_ADMIN_ROLE)
                & (LocalUser.is_active.is_(True))
            )
            | (LocalUser.id == target_id)
        )
        .order_by(LocalUser.id)
        .with_for_update()
    )
    rows = (await db.execute(stmt)).all()

    before = 0
    after = 0
    for row_id, role, is_active in rows:
        if role == TENANT_ADMIN_ROLE and is_active:
            before += 1
        if row_id == target_id:
            if new_role is not _UNSET:
                role = new_role
            if new_is_active is not _UNSET:
                is_active = new_is_active
        if role == TENANT_ADMIN_ROLE and is_active:
            after += 1
    return before > 0 and after == 0
