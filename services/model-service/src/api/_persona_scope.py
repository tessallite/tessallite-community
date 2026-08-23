"""Persona-scoped metadata helpers for model-service endpoints.

Delegates effective-persona resolution to the shared resolver in
``shared.security.persona_resolver``.  This module re-exports the
resolver plus metadata-specific helpers (allow-list parsing,
hierarchy-level attribute exclusion).
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Dimension,
    Persona,
    PersonaTagRestriction,
    data_tag_columns,
)
from shared.security.persona_resolver import resolve_effective_persona  # noqa: F401

logger = logging.getLogger(__name__)


async def get_restricted_column_ids(
    db: AsyncSession, persona_id: UUID
) -> set[UUID]:
    """Return the model-column ids a persona is CLS-restricted from seeing.

    A column is restricted when it carries a data tag that the persona's
    tag-restriction set denies. Metadata surfaces must fail closed on these
    columns: the query-router blocks the *values* on every path, but the
    column *names* leak through catalogue/list endpoints unless callers
    exclude them here. Returns an empty set when the persona has no
    restrictions.
    """
    rows = await db.execute(
        select(data_tag_columns.c.model_column_id)
        .join(
            PersonaTagRestriction,
            PersonaTagRestriction.data_tag_id == data_tag_columns.c.tag_id,
        )
        .where(PersonaTagRestriction.persona_id == persona_id)
    )
    return {cid for cid in rows.scalars().all() if cid is not None}


def parse_allowed_ids(raw_ids: list) -> list[UUID] | None:
    """Convert a JSONB allow-list to typed UUIDs.

    Returns ``None`` when the list is empty (unrestricted).
    Raises 500 on malformed UUID strings — fail closed.
    """
    if not raw_ids:
        return None
    out: list[UUID] = []
    for v in raw_ids:
        try:
            out.append(UUID(str(v)))
        except ValueError:
            logger.error("Malformed UUID in persona allow-list: %s", v)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Persona configuration error",
            )
    return out


async def get_excluded_level_attribute_ids(
    db: AsyncSession,
    *,
    model_id: UUID,
    persona: Persona,
) -> set[UUID] | None:
    """Return attribute IDs that hierarchy levels must NOT expose.

    When the persona's ``included_dimension_ids`` is populated, any
    hierarchy level whose key attribute backs an excluded dimension is
    hidden.  Returns ``None`` when there is no dimension restriction
    (empty allow-list = unrestricted).
    """
    allowed_dim_ids = parse_allowed_ids(persona.included_dimension_ids)
    if allowed_dim_ids is None:
        return None

    allowed_set = set(allowed_dim_ids)
    result = await db.execute(
        select(Dimension.id, Dimension.source_column_id, Dimension.user_defined_attribute_id)
        .where(Dimension.model_id == model_id)
    )
    excluded_attr_ids: set[UUID] = set()
    for dim_id, src_col_id, uda_id in result.all():
        if dim_id in allowed_set:
            continue
        if src_col_id is not None:
            excluded_attr_ids.add(src_col_id)
        if uda_id is not None:
            excluded_attr_ids.add(uda_id)
    return excluded_attr_ids
