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
from shared.db.models import Persona, PersonaTagRestriction

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


def widens_visibility(persona: Persona) -> bool:
    """True when the persona WIDENS access rather than narrowing it.

    Two persona flags widen visibility past the model's default surface,
    so both are privilege-granting rather than voluntary filters:

    * ``includes_hidden_columns`` — the technical view exposes columns the
      modeller curated away (F-008-04).
    * ``bypass_row_security`` — skips the row-level-security predicate
      entirely, so the holder reads every row regardless of RLS rules
      (F-008-30 / Bug-6136).

    A widening persona is a privileged surface: it is granted only on an
    explicit, non-empty audience-role match, is never "available to
    everyone" via an empty audience list, and is never a legitimate
    voluntary pick for a non-privileged caller.
    """
    return bool(
        getattr(persona, "includes_hidden_columns", False)
        or getattr(persona, "bypass_row_security", False)
    )


def narrows_visibility(
    persona: Persona, *, has_tag_restrictions: bool = False,
) -> bool:
    """True when the persona NARROWING access rather than being a filter-only
    or unrestricted surface.

    F-008-03: an empty ``audience_roles`` list historically meant "available
    to everyone". Combined with a measure/dimension/hierarchy allow-list,
    default filters, or CLS tag restrictions, that assigned a locking
    persona to every caller and could deny the whole tenant (complex SQL
    403, hidden measures). Narrowing personas require an explicit role
    match, the same way widening personas already do.
    """
    if persona.included_measure_ids:
        return True
    if persona.included_dimension_ids:
        return True
    if persona.included_hierarchy_ids:
        return True
    if persona.default_filters:
        return True
    return bool(has_tag_restrictions)


class PersonaAudienceNarrowingError(ValueError):
    """A narrowing persona was persisted (or imported) with an empty audience.

    F-008-03 / Bug-9266: after explicit-audience assignment, an allow-listed
    persona with no ``audience_roles`` is assigned to nobody, so the
    restriction never applies. Reject at every writer instead.
    """


def payload_narrows_visibility(
    *,
    included_measure_ids=None,
    included_dimension_ids=None,
    included_hierarchy_ids=None,
    default_filters=None,
    restricted_tag_ids=None,
) -> bool:
    """True when a persona payload would narrow visibility (F-008-03).

    Filter-only empty everything (no allow-lists, no default filters, no
    tag restrictions) is unrestricted and may keep an empty audience.
    """
    if included_measure_ids:
        return True
    if included_dimension_ids:
        return True
    if included_hierarchy_ids:
        return True
    if default_filters:
        return True
    if restricted_tag_ids:
        return True
    return False


def reject_empty_audience_narrowing(
    audience_roles,
    *,
    included_measure_ids=None,
    included_dimension_ids=None,
    included_hierarchy_ids=None,
    default_filters=None,
    restricted_tag_ids=None,
) -> None:
    """Refuse a narrowing persona that names no audience role (F-008-03).

    Shared by REST create/update, YAML import, and snapshot ``_insert_personas``.
    """
    if audience_roles:
        return
    if not payload_narrows_visibility(
        included_measure_ids=included_measure_ids,
        included_dimension_ids=included_dimension_ids,
        included_hierarchy_ids=included_hierarchy_ids,
        default_filters=default_filters,
        restricted_tag_ids=restricted_tag_ids,
    ):
        return
    raise PersonaAudienceNarrowingError(
        "A persona that narrows visibility (allow-lists, default "
        "filters, or column-tag restrictions) must name at least "
        "one audience role. An empty audience would leave the "
        "restriction unassigned after F-008-03, so the imported "
        "allow-list would never apply."
    )


def is_in_audience(
    persona: Persona,
    roles: set[str],
    *,
    has_tag_restrictions: bool = False,
) -> bool:
    """Single audience predicate shared by resolution and listing (I-1).

    Regular personas: an empty ``audience_roles`` list means "available
    to everyone"; a non-empty list requires an intersection with the
    caller's roles.

    Visibility-widening personas (hidden-columns technical view, or
    ``bypass_row_security``; F-008-04 / F-008-30): privileged surfaces —
    they require an explicit, non-empty audience-role match and are never
    "for everyone" via an empty audience list. Otherwise an empty-audience
    bypass/technical persona would be silently assigned to every regular
    user, skipping RLS or exposing hidden columns tenant-wide. The grant
    role is :data:`shared.auth.roles.MODEL_TECHNICAL_ROLE` for seeded
    Technical personas.

    Visibility-narrowing personas (F-008-03): allow-lists, default
    filters, or CLS tag restrictions also require an explicit role match.
    Filter-only empty everything + empty audience stays "everyone".
    """
    if widens_visibility(persona) or narrows_visibility(
        persona, has_tag_restrictions=has_tag_restrictions,
    ):
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

    Exception (F-008-04 / F-008-30): visibility-widening personas —
    those that expose hidden columns (``includes_hidden_columns=True``,
    e.g. the seeded Technical persona) OR bypass row-level security
    (``bypass_row_security=True``) — are privileged surfaces. They are
    assigned ONLY on an explicit, non-empty audience-role match. An empty
    audience list on a widening persona must never make it "available to
    everyone", which would force-lock every regular user into it and
    either expose hidden columns or skip RLS for all viewers.
    """
    result = await db.execute(
        select(Persona).where(Persona.model_id == model_id)
    )
    all_personas = list(result.scalars().all())
    roles = caller_roles(user)
    tagged: set = set()
    if all_personas:
        # Bug-9263: a failed tag-restriction lookup used to treat every
        # persona as untagged. A CLS-only empty-audience persona then
        # looked unrestricted and was assigned to everyone (tenant lock)
        # while the failure was hidden. Fail the request instead.
        try:
            raw = (
                await db.execute(
                    select(PersonaTagRestriction.persona_id).where(
                        PersonaTagRestriction.persona_id.in_(
                            [p.id for p in all_personas]
                        )
                    )
                )
            ).scalars().all()
            tagged = set()
            for item in raw:
                pid = getattr(item, "id", item)
                tagged.add(UUID(str(pid)))
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                "Bug-9263: PersonaTagRestriction lookup failed; "
                "refusing persona assignment rather than treating every "
                "persona as untagged"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "PERSONA_ASSIGNMENT_UNAVAILABLE",
                    "message": (
                        "Persona assignment could not be determined "
                        "because a tag-restriction lookup failed. Retry "
                        "the request."
                    ),
                },
            )
    return [
        p for p in all_personas
        if is_in_audience(p, roles, has_tag_restrictions=p.id in tagged)
    ]


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
            # F-008-04 / F-008-30: a voluntary pick may only NARROW
            # visibility. A hidden-columns or bypass_row_security persona
            # WIDENS it (exposes hidden columns / skips RLS), so a
            # non-privileged caller with no assignment may never select
            # one — that is a privilege escalation. Such personas require
            # an explicit audience-role grant.
            if widens_visibility(persona):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "This persona widens data access and requires an "
                        "explicit audience-role assignment"
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
