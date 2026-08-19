"""Unit tests for the shared refresh pending-guard helper (Bug-7903).

These lock the pure decision logic (decide_refresh_pending) and the committed-status
re-read that honours an out-of-band user disable/retire during a refresh window
(resolve_success_restore_status) — the single home the full and incremental refresh
jobs share so they cannot drift (Fable R1 #4).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.aggregate_refresh_guard import (
    PendingGuardDecision,
    decide_refresh_pending,
    resolve_success_restore_status,
)

pytestmark = pytest.mark.unit

# shared/tests has no asyncio auto-mode, so async cases are marked explicitly.
_aio = pytest.mark.asyncio


def _agg(status: str, prior=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(), status=status, refresh_prior_status=prior,
    )


def _db_committed_status(value):
    """AsyncMock session whose status-column SELECT returns ``value``."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    db.execute = AsyncMock(return_value=result)
    return db


# --- decide_refresh_pending -------------------------------------------------

def test_decide_fresh_active_flips_and_commits():
    d = decide_refresh_pending(_agg("active"))
    assert d == PendingGuardDecision(prior_status="active", flipped=True, needs_commit=True)


def test_decide_recovery_pending_with_disabled_prior_no_commit():
    d = decide_refresh_pending(_agg("pending", prior="disabled"))
    assert d.prior_status == "disabled"
    assert d.flipped is True
    assert d.needs_commit is False  # already non-servable, no re-flip


def test_decide_recovery_invalid_with_active_prior_no_commit():
    d = decide_refresh_pending(_agg("invalid", prior="active"))
    assert d.prior_status == "active"
    assert d.flipped is True
    assert d.needs_commit is False


def test_decide_recovery_active_with_durable_prior_reflips_and_commits():
    # Early invalid->active self-heal left status active but a durable prior exists.
    d = decide_refresh_pending(_agg("active", prior="disabled"))
    assert d.prior_status == "disabled"
    assert d.flipped is True
    assert d.needs_commit is True  # must re-pend the now-active aggregate


def test_decide_non_servable_no_prior_left_untouched():
    d = decide_refresh_pending(_agg("invalid"))
    assert d == PendingGuardDecision(prior_status="invalid", flipped=False, needs_commit=False)


# --- resolve_success_restore_status ----------------------------------------

@_aio
async def test_restore_flipped_active_returns_active():
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("pending")  # no out-of-band change
    assert await resolve_success_restore_status(db, agg, d) == "active"


@_aio
async def test_restore_flipped_disabled_returns_disabled():
    agg = _agg("pending", prior="disabled")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("pending")
    assert await resolve_success_restore_status(db, agg, d) == "disabled"


@_aio
async def test_restore_honours_out_of_band_user_disable():
    """A user committed status="disabled" DURING the refresh: the committed read
    wins over the durable "active" prior — never clobber a user's disable back to
    active (Fable R2 #2)."""
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("disabled")  # user disabled mid-refresh
    assert await resolve_success_restore_status(db, agg, d) == "disabled"


@_aio
async def test_restore_honours_out_of_band_user_retire():
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("retired")
    assert await resolve_success_restore_status(db, agg, d) == "retired"


@_aio
async def test_restore_legacy_self_heal_returns_active():
    """No durable prior, entry invalid: legacy Bug-7131 self-heal to active."""
    agg = _agg("invalid")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("invalid")
    assert await resolve_success_restore_status(db, agg, d) == "active"


@_aio
async def test_restore_row_deleted_midrefresh_leaves_unchanged():
    """The aggregate row vanished (deleted mid-refresh) — return None so the caller
    leaves the status unchanged and does not resurrect a deleted aggregate."""
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status(None)
    assert await resolve_success_restore_status(db, agg, d) is None


@_aio
async def test_storage_repoint_refuses_active_restore_and_queues_rebuild():
    """Successful CTAS on old storage is not a successful serving restore."""
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("pending")
    assert await resolve_success_restore_status(
        db, agg, d, storage_binding_matches=False
    ) == "pending"


@_aio
async def test_storage_repoint_preserves_durable_prior_disabled_from_pending():
    """A repoint invalidation must not erase a refresh-owned user disable."""
    agg = _agg("pending", prior="disabled")
    decision = decide_refresh_pending(agg)
    db = _db_committed_status("pending")

    assert await resolve_success_restore_status(
        db, agg, decision, storage_binding_matches=False
    ) == "disabled"


@_aio
@pytest.mark.parametrize("terminal", ["disabled", "retired"])
async def test_storage_repoint_preserves_user_terminal_status(terminal):
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status(terminal)
    assert await resolve_success_restore_status(
        db, agg, d, storage_binding_matches=False
    ) == terminal


# ---------------------------------------------------------------------------
# Bug-8602 — the SOURCE binding gates a restore exactly as the target one does
# ---------------------------------------------------------------------------


@_aio
async def test_source_repoint_refuses_active_restore_and_queues_rebuild():
    """A successful CTAS whose SOURCE moved mid-build wrote rows read from a
    database the model no longer reads. The DDL succeeded, but the numbers are
    not the deployed definition's, so the aggregate must stay non-serving.

    Both refresh writers delegate this verdict here so they cannot drift; that
    is only worth anything if the source leg is actually consulted.
    """
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("pending")
    assert await resolve_success_restore_status(
        db, agg, d, storage_binding_matches=True, source_binding_matches=False
    ) == "pending"


@_aio
async def test_source_repoint_preserves_durable_prior_disabled():
    """A source repoint must not turn a user disable into sweep-owned pending
    either — the same precedence the target side established (Bug-8481 R1)."""
    agg = _agg("pending", prior="disabled")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("pending")
    assert await resolve_success_restore_status(
        db, agg, d, storage_binding_matches=True, source_binding_matches=False
    ) == "disabled"


@_aio
@pytest.mark.parametrize("terminal", ["disabled", "retired"])
async def test_source_repoint_preserves_user_terminal_status(terminal):
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status(terminal)
    assert await resolve_success_restore_status(
        db, agg, d, storage_binding_matches=True, source_binding_matches=False
    ) == terminal


@_aio
async def test_both_bindings_matching_still_restores_active():
    """Control: adding the source leg must not refuse a clean rebuild."""
    agg = _agg("pending", prior="active")
    d = decide_refresh_pending(agg)
    db = _db_committed_status("pending")
    assert await resolve_success_restore_status(
        db, agg, d, storage_binding_matches=True, source_binding_matches=True
    ) == "active"
