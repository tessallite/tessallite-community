"""Bug-9208 — persona assignment cannot be shed by choosing the base catalogue.

DECISION (2026-09-01, Option A): ``is_hidden`` stays CURATION, not access
control. That decision is only defensible because ASSIGNMENT, not catalogue
choice, decides which persona applies. If a user assigned a restrictive persona
could drop it by connecting to the base ``<model.slug>`` catalogue, hiding would
be the only thing standing between them and the data, and Option A would be
wrong.

These tests pin the half of the matrix that carries that weight. They are the
reason the reported "the base catalogue runs no persona gate" behaviour is NOT
an authorization bypass: an unassigned caller is unrestricted BY DESIGN, and an
assigned caller keeps their persona whether or not they ask for one.

Matrix under test (from the module docstring of shared/security/persona_resolver):
- assigned to exactly 1 persona -> auto-resolves to it, even with no request;
  a request for any OTHER persona is refused.
- assigned to N>1 -> omitting the choice is refused, not silently unrestricted.
- no assignment -> None (everything in the model). This is the documented
  contract, not a gap; restriction comes from assigning a persona, or from
  column-level and row-level security.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from shared.security.persona_resolver import resolve_effective_persona

MODEL_ID = uuid.uuid4()


def _user(role: str = "viewer"):
    return SimpleNamespace(
        user_id="u1", tenant_id="acme", email="analyst@example.com",
        role=role, roles=[role], persona_id=None,
    )


def _persona(name: str, *, hidden: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(), name=name, model_id=MODEL_ID,
        includes_hidden_columns=hidden, audience_roles=["viewer"],
        bypass_row_security=False,
    )


@pytest.mark.asyncio
async def test_bug9208_single_assignment_applies_without_being_requested():
    """THE contract. Connecting to the base catalogue sends no persona id;
    the assigned persona must still apply, or hiding becomes the boundary."""
    restrictive = _persona("finance")
    with patch(
        "shared.security.persona_resolver.get_assigned_personas",
        AsyncMock(return_value=[restrictive]),
    ):
        resolved = await resolve_effective_persona(
            AsyncMock(), current_user=_user(),
            model_id=MODEL_ID, requested_persona_id=None,
        )
    assert resolved is restrictive, (
        "an assigned persona was dropped when the caller requested none — "
        "the base catalogue would then be an allow-list bypass"
    )


@pytest.mark.asyncio
async def test_bug9208_single_assignment_refuses_a_different_persona():
    restrictive = _persona("finance")
    other = _persona("ops")
    with patch(
        "shared.security.persona_resolver.get_assigned_personas",
        AsyncMock(return_value=[restrictive]),
    ):
        with pytest.raises(HTTPException) as exc:
            await resolve_effective_persona(
                AsyncMock(), current_user=_user(),
                model_id=MODEL_ID, requested_persona_id=str(other.id),
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bug9208_multiple_assignments_refuse_an_omitted_choice():
    """Omitting the pick must NOT fall through to unrestricted access."""
    with patch(
        "shared.security.persona_resolver.get_assigned_personas",
        AsyncMock(return_value=[_persona("finance"), _persona("ops")]),
    ):
        with pytest.raises(HTTPException) as exc:
            await resolve_effective_persona(
                AsyncMock(), current_user=_user(),
                model_id=MODEL_ID, requested_persona_id=None,
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bug9208_no_assignment_is_unrestricted_by_design():
    """The documented matrix: no assignment means everything in the model.

    This is what makes ``apply_persona_gate`` return early on the base
    catalogue. It is the contract, not a defect — and it is exactly why hiding
    a sensitive field is not a substitute for assigning a persona.
    """
    with patch(
        "shared.security.persona_resolver.get_assigned_personas",
        AsyncMock(return_value=[]),
    ):
        resolved = await resolve_effective_persona(
            AsyncMock(), current_user=_user(),
            model_id=MODEL_ID, requested_persona_id=None,
        )
    assert resolved is None
