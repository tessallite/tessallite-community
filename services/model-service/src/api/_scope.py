"""
Shared project/model scope validation helpers for route handlers.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, select

from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection,
)
from shared.db.models import (
    DataSource,
    EntityTranslation,
    GlossaryAttachment,
    GlossaryEntry,
    Model,
    ProjectConnection,
    UserEntityPreference,
)


def model_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found")


def cross_project_connection() -> HTTPException:
    """Bug-5325: a DataSource/DataTarget row points its connection at a
    different project than the one that owns the source. Refuse to use it."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            "Source connection belongs to a different project. This source is "
            "misconfigured; re-point it at a connection in the correct project."
        ),
    )


async def resolve_source_connection(
    db,
    source: DataSource,
    *,
    expected_project_id: UUID,
) -> ProjectConnection:
    """Read-time, fail-closed resolution of a DataSource's ProjectConnection.

    Bug-5325: create/update guards stop NEW cross-project writes, but legacy or
    imported ``DataSource.project_connection_id`` rows could already point at a
    ProjectConnection that belongs to a DIFFERENT project (and therefore a
    different tenant's source credentials). Every read site that turns a source
    into a live connection must funnel through here so such a row is REJECTED
    rather than silently used.

    ``expected_project_id`` is the project that owns the source — i.e. the
    project_id of the source's model, already validated by
    ``ensure_model_in_project``. The connection is accepted only when its
    ``project_id`` matches; this mirrors the create/update validators' notion of
    "belongs to this project".

    Delegates the actual project-scope check to the shared fail-closed resolver
    (``shared.connection_scope``) so there is a single source of truth shared
    with the query-router execution sites; this handler only maps the shared
    errors onto the model-service's HTTP error shapes.
    """
    try:
        return await resolve_endpoint_connection(
            db, source, expected_project_id=expected_project_id
        )
    except CrossProjectConnectionError:
        raise cross_project_connection()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project connection not found",
        )


async def purge_entity_soft_references(db, *, model_id: UUID, entity_id: UUID) -> None:
    """Delete the soft-referencing rows that survive an entity's deletion.

    ``EntityTranslation.entity_id`` and ``UserEntityPreference.entity_id`` are
    generic UUIDs with no database foreign key — they point polymorphically at
    a measure / dimension / KPI / named set / glossary entry, so the cascade
    that fires on the parent row cannot reach them. When such an entity is
    deleted these rows would otherwise linger forever and inflate translation
    coverage (F-029-15). Callers invoke this in the entity's delete handler,
    before commit, on the same session.
    """
    await db.execute(
        delete(EntityTranslation).where(
            EntityTranslation.model_id == model_id,
            EntityTranslation.entity_id == entity_id,
        )
    )
    await db.execute(
        delete(UserEntityPreference).where(
            UserEntityPreference.model_id == model_id,
            UserEntityPreference.entity_id == entity_id,
        )
    )


async def ensure_model_in_project(
    db,
    *,
    project_id: UUID,
    model_id: UUID,
) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise model_not_found()
    return model


async def glossary_text_for_target(
    db, model_id, target_type: str, target_id
) -> str | None:
    """Return the latest approved glossary definition attached to the given
    semantic object, or None when no entry exists.

    F-018-20: this was copy-pasted verbatim in dimensions.py and measures.py;
    hoisted here so both call one implementation.
    """
    stmt = (
        select(GlossaryEntry.definition)
        .join(GlossaryAttachment, GlossaryAttachment.entry_id == GlossaryEntry.id)
        .where(GlossaryEntry.model_id == model_id)
        .where(GlossaryEntry.status == "approved")
        .where(GlossaryEntry.superseded_by.is_(None))
        .where(GlossaryAttachment.target_type == target_type)
        .where(GlossaryAttachment.target_id == target_id)
        .order_by(GlossaryEntry.version.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def glossary_texts_for_targets(
    db,
    model_id,
    target_type: str,
    target_ids: list[UUID],
) -> dict[UUID, str]:
    """Batch sibling of ``glossary_text_for_target``.

    Returns the latest approved glossary definition for every target id in a
    single query, keyed by target id. The list endpoints call this once per
    page instead of issuing one ``glossary_text_for_target`` query per row,
    eliminating the per-row N+1 (F-018-22).
    """
    if not target_ids:
        return {}
    stmt = (
        select(GlossaryAttachment.target_id, GlossaryEntry.definition)
        .join(GlossaryEntry, GlossaryAttachment.entry_id == GlossaryEntry.id)
        .where(GlossaryEntry.model_id == model_id)
        .where(GlossaryEntry.status == "approved")
        .where(GlossaryEntry.superseded_by.is_(None))
        .where(GlossaryAttachment.target_type == target_type)
        .where(GlossaryAttachment.target_id.in_(target_ids))
        .order_by(GlossaryAttachment.target_id, GlossaryEntry.version.desc())
    )
    result = await db.execute(stmt)
    by_target: dict[UUID, str] = {}
    # Rows are ordered version-desc per target; first seen wins (latest).
    for target_id, definition in result.all():
        by_target.setdefault(target_id, definition)
    return by_target
