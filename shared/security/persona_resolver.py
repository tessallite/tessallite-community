"""Shared effective-persona resolution for all services.

Resolves the authoritative persona for a request, considering:
1. Locked persona from the authenticated context (embed tokens).
2. Privileged users (admin/modeler) — any persona or none.
3. Technical persona holder — auto-resolves to the technical persona.
4. Audience-based assignment for regular users.
5. No persona (everything in the model) when the caller has no assignment.

Resolution matrix (Q2):
- Embed with locked persona -> use locked; reject conflicts.
- Privileged (admin / modeler) -> optional.
- Technical persona holder -> auto-resolve to technical persona.
- Assigned to 1 persona -> auto-resolve; reject other picks.
- Assigned to N>1 personas -> must pick from assigned list; reject omission.
- No assignment -> everything in the model; any persona is a voluntary filter.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.auth.middleware import CurrentUser
from shared.db.models import Persona

logger = logging.getLogger(__name__)

PRIVILEGED_ROLES = frozenset({"system_admin", "tenant_admin", "modeler"})


def caller_roles(user: CurrentUser) -> set[str]:
    bag: set[str] = set()
    role = getattr(user, "role", None)
    if role:
        bag.add(role)
    extra = getattr(user, "roles", None)
    if extra:
        bag.update(extra)
    return bag


def is_privileged_by_role(user: CurrentUser) -> bool:
    return bool(caller_roles(user) & PRIVILEGED_ROLES)


def _explicit_audience_match(persona: Persona, roles: set[str]) -> bool:
    """True when the persona names at least one of the caller's roles."""
    return bool(persona.audience_roles and set(persona.audience_roles) & roles)


def is_in_audience(persona: Persona, roles: set[str]) -> bool:
    """Single audience predicate shared by resolution and listing (I-1).

    Regular personas: an empty ``audience_roles`` list means "available
    to everyone"; a non-empty list requires an intersection with the
    caller's roles.

    Hidden-columns personas (the technical view, F-008-04): privileged
    surfaces — they require an explicit, non-empty audience-role match
    and are never "for everyone" via an empty audience list. The grant
    role is :data:`shared.auth.roles.MODEL_TECHNICAL_ROLE` for seeded
    Technical personas.
    """
    if getattr(persona, "includes_hidden_columns", False):
        return _explicit_audience_match(persona, roles)
    return not persona.audience_roles or bool(
        set(persona.audience_roles) & roles
    )


async def get_assigned_personas(
    db: AsyncSession, user: CurrentUser, model_id: UUID,
) -> list[Persona]:
    """Return personas in the model that the user is assigned to.

    A persona is assigned when its ``audience_roles`` list intersects
    the user's roles, OR when ``audience_roles`` is empty (available to
    everyone).

    Exception (F-008-04): personas that expose hidden columns
    (``includes_hidden_columns=True``, e.g. the seeded Technical
    persona) are privileged surfaces. They are assigned ONLY on an
    explicit, non-empty audience-role match — an empty audience list on
    a hidden-columns persona must never make it "available to everyone",
    which would force-lock every regular user to the technical view and
    expose hidden columns to all viewers.
    """
    result = await db.execute(
        select(Persona).where(Persona.model_id == model_id)
    )
    all_personas = list(result.scalars().all())
    roles = caller_roles(user)
    return [p for p in all_personas if is_in_audience(p, roles)]


async def load_persona_or_fail(
    db: AsyncSession, persona_id: UUID | str, model_id: UUID,
) -> Persona:
    try:
        uid = UUID(str(persona_id))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Persona not found for model",
        )
    result = await db.execute(
        select(Persona).where(Persona.id == uid, Persona.model_id == model_id)
    )
    persona = result.scalar_one_or_none()
    if persona is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Persona not found for model",
        )
    return persona


async def resolve_effective_persona(
    db: AsyncSession,
    *,
    current_user: CurrentUser,
    model_id: UUID,
    requested_persona_id: UUID | str | None,
) -> Persona | None:
    """Return the authoritative Persona for a metadata or execution request.

    Returns ``None`` only when no persona applies and the caller is
    allowed unrestricted access (everything in the model).
    """
    locked_id: str | None = getattr(current_user, "persona_id", None)

    if locked_id is not None:
        if (
            requested_persona_id is not None
            and str(requested_persona_id) != str(locked_id)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Persona does not match authenticated context",
            )
        return await load_persona_or_fail(db, locked_id, model_id)

    if is_privileged_by_role(current_user):
        if requested_persona_id is not None:
            return await load_persona_or_fail(db, requested_persona_id, model_id)
        return None

    assigned = await get_assigned_personas(db, current_user, model_id)

    # Technical-persona holder: get_assigned_personas only includes a
    # hidden-columns persona on an explicit audience-role match
    # (F-008-04), so this branch fires for genuine holders only — never
    # for every viewer because the seeded persona had an empty audience.
    tech = next(
        (p for p in assigned if getattr(p, "includes_hidden_columns", False)),
        None,
    )
    if tech is not None:
        if requested_persona_id is not None:
            if str(requested_persona_id) != str(tech.id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Technical persona holders must use the technical persona",
                )
        return tech

    if len(assigned) == 0:
        if requested_persona_id is not None:
            persona = await load_persona_or_fail(db, requested_persona_id, model_id)
            # F-008-04: a voluntary pick narrows visibility; a
            # hidden-columns persona WIDENS it. Non-privileged callers
            # may only use a technical persona via an explicit
            # audience-role grant.
            if getattr(persona, "includes_hidden_columns", False):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "The technical persona requires an explicit "
                        "audience-role assignment"
                    ),
                )
            return persona
        return None

    if len(assigned) == 1:
        if requested_persona_id is not None:
            if str(requested_persona_id) != str(assigned[0].id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not assigned to this persona",
                )
        return assigned[0]

    assigned_ids = {str(p.id) for p in assigned}
    if requested_persona_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You have multiple personas assigned — please select one",
        )
    req_str = str(requested_persona_id)
    if req_str not in assigned_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not assigned to this persona",
        )
    return next(p for p in assigned if str(p.id) == req_str)
