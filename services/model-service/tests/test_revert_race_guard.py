"""Bug-8394 revert-race guard: a data-tag/RLS/persona write that commits
between a revert's capture and its commit must be SERIALISED by the per-model
advisory lock so the concurrent write is never silently lost.

The race window is:
  1. Revert acquires the per-model advisory lock.
  2. Revert captures governance state (data-tag column bindings, RLS rules,
     persona tag restrictions).
  3. Revert truncates snapshot-owned tables.
  4. Revert re-inserts from the captured snapshot.
  5. Revert commits and releases the lock.

An admin editing a data-tag column binding (step X) between steps 2 and 5 must
BLOCK on the lock at step X until the revert commits (step 5), then the admin's
write commits on top of the reverted state — the newly-tagged (restricted)
column is never silently served untagged.

This test verifies the contract rather than simulating true concurrency: it
asserts that every governance writer endpoint (data_tags, row_security,
personas) acquires ``acquire_model_definition_lock`` BEFORE its first DB write,
on the SAME session the writes use, bound to the endpoint's own ``model_id``
parameter. That combination is what makes the lock effective against the race.
"""
from __future__ import annotations

import pytest
from shared.db.model_lock_coverage import (
    effective_from_source as _effective_from_source,
    lock_is_effective as _lock_is_effective,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Contract tests: data_tags governance writers
# ---------------------------------------------------------------------------

def test_create_tag_lock_is_effective():
    """Bug-8394: create_tag acquires the lock before any write."""
    from src.api.data_tags import create_tag

    ok, reason = _lock_is_effective(create_tag)
    assert ok, f"create_tag lock not effective: {reason}"


def test_update_tag_lock_is_effective():
    """Bug-8394: update_tag acquires the lock before any write."""
    from src.api.data_tags import update_tag

    ok, reason = _lock_is_effective(update_tag)
    assert ok, f"update_tag lock not effective: {reason}"


def test_delete_tag_lock_is_effective():
    """Bug-8394: delete_tag acquires the lock before any write."""
    from src.api.data_tags import delete_tag

    ok, reason = _lock_is_effective(delete_tag)
    assert ok, f"delete_tag lock not effective: {reason}"


def test_set_persona_tag_restrictions_lock_is_effective():
    """Bug-8394: set_persona_tag_restrictions acquires the lock before any
    write — this is the CLS-surface mutation that would cause a fail-open
    exposure if it committed between a revert's capture and commit."""
    from src.api.data_tags import set_persona_tag_restrictions

    ok, reason = _lock_is_effective(set_persona_tag_restrictions)
    assert ok, f"set_persona_tag_restrictions lock not effective: {reason}"


# ---------------------------------------------------------------------------
# Contract tests: row_security governance writers
# ---------------------------------------------------------------------------

def test_create_rule_lock_is_effective():
    """Bug-8394: create_rule acquires the lock before any write."""
    from src.api.row_security import create_rule

    ok, reason = _lock_is_effective(create_rule)
    assert ok, f"create_rule lock not effective: {reason}"


def test_update_rule_lock_is_effective():
    """Bug-8394: update_rule acquires the lock before any write."""
    from src.api.row_security import update_rule

    ok, reason = _lock_is_effective(update_rule)
    assert ok, f"update_rule lock not effective: {reason}"


def test_delete_rule_lock_is_effective():
    """Bug-8394: delete_rule acquires the lock before any write."""
    from src.api.row_security import delete_rule

    ok, reason = _lock_is_effective(delete_rule)
    assert ok, f"delete_rule lock not effective: {reason}"


# ---------------------------------------------------------------------------
# Contract tests: personas governance writers
# ---------------------------------------------------------------------------

def test_create_persona_lock_is_effective():
    """Bug-8394: create_persona acquires the lock before any write."""
    from src.api.personas import create_persona

    ok, reason = _lock_is_effective(create_persona)
    assert ok, f"create_persona lock not effective: {reason}"


def test_update_persona_lock_is_effective():
    """Bug-8394: update_persona acquires the lock before any write."""
    from src.api.personas import update_persona

    ok, reason = _lock_is_effective(update_persona)
    assert ok, f"update_persona lock not effective: {reason}"


def test_delete_persona_lock_is_effective():
    """Bug-8394: delete_persona acquires the lock before any write."""
    from src.api.personas import delete_persona

    ok, reason = _lock_is_effective(delete_persona)
    assert ok, f"delete_persona lock not effective: {reason}"


# ---------------------------------------------------------------------------
# Regression guard: a NEW governance writer that forgets the lock
# ---------------------------------------------------------------------------

def test_unlocked_governance_writer_fails_effectiveness_check():
    """Bug-8394 regression guard: an endpoint shaped like a governance writer
    that does NOT acquire the lock must FAIL the effectiveness check. This
    verifies the guard itself catches the omission — not just that the current
    code happens to pass."""
    ok, reason = _effective_from_source(
        """
        async def new_governance_writer(project_id, model_id, db):
            async for db in get_tenant_db(tenant):
                await _get_model(db, project_id, model_id)
                # Forgot to acquire the lock!
                db.add(object())
                await db.commit()
        """
    )
    assert not ok, (
        "an unlocked governance writer must fail the effectiveness check, "
        "but it passed — the guard has a blind spot"
    )
