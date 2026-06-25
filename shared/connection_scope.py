"""Fail-closed project-scope validation for source/target connections.

Bug-5325 / Bug-5326: a ``DataSource`` or ``DataTarget`` row carries a
``project_connection_id``. A legacy or imported row can point that FK at a
``ProjectConnection`` that belongs to a DIFFERENT project (and therefore a
different tenant's source credentials). Create/update guards in the
model-service block NEW cross-project writes, but every READ site that turns a
source/target into a live connection must ALSO refuse such a row rather than
silently execute SQL against another project's database.

This module is the single source of truth for that check. Every execution
site — model-service read endpoints, the query-router introspect endpoint, the
query-router normal-query / aggregate-target / pocket-target execution paths,
and the shared aggregate connection resolver — funnels its connection resolution
through here so the rule (``connection.project_id == owning_model.project_id``)
is enforced in exactly one place and cannot be skipped or diverge per site.

The check is intentionally a plain ``ValueError`` subclass so callers in any
service (with or without FastAPI) can catch it and translate to the right
transport error (HTTP 422, a gateway error frame, a ValueError to the optimizer,
etc.) without this module depending on web frameworks.
"""
from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Model, ProjectConnection


class CrossProjectConnectionError(ValueError):
    """Raised when a source/target connection belongs to a different project
    than the model that owns the source/target. Fail-closed: the row is
    misconfigured and must not be used to execute against another project's
    database."""


class _HasConnectionRef(Protocol):
    """A DataSource or DataTarget — anything carrying ``project_connection_id``
    and ``model_id``."""

    project_connection_id: UUID
    model_id: UUID


def assert_connection_in_project(
    conn: ProjectConnection,
    expected_project_id: UUID,
) -> None:
    """Fail-closed: raise ``CrossProjectConnectionError`` unless ``conn``
    belongs to ``expected_project_id``.

    ``expected_project_id`` is the project that owns the source/target — i.e.
    the ``project_id`` of the owning ``Model``. This mirrors the create/update
    validators' notion of "the connection belongs to this project".
    """
    if conn.project_id != expected_project_id:
        raise CrossProjectConnectionError(
            "Source/target connection belongs to a different project. The "
            "row is misconfigured; re-point it at a connection in the correct "
            "project."
        )


async def resolve_endpoint_connection(
    db: AsyncSession,
    endpoint: _HasConnectionRef,
    *,
    expected_project_id: UUID,
) -> ProjectConnection:
    """Load and validate the ``ProjectConnection`` for a DataSource/DataTarget.

    Loads ``endpoint.project_connection_id`` and asserts it belongs to
    ``expected_project_id`` before returning it. Raises:

    * ``CrossProjectConnectionError`` when the connection's project differs
      from ``expected_project_id`` (fail-closed cross-project guard).
    * ``ValueError`` when the connection row does not exist.
    """
    conn = await db.get(ProjectConnection, endpoint.project_connection_id)
    if conn is None:
        raise ValueError(
            f"ProjectConnection {endpoint.project_connection_id} not found"
        )
    assert_connection_in_project(conn, expected_project_id)
    return conn


async def resolve_endpoint_connection_for_model(
    db: AsyncSession,
    endpoint: _HasConnectionRef,
    *,
    model_id: UUID,
) -> ProjectConnection:
    """Load and validate a DataSource/DataTarget connection against its model.

    Background/job paths (stats collector, schema drift, pocket/aggregate
    materialisation and refresh) frequently have the owning ``model_id`` in
    hand but not the ``Model`` row, and must apply the SAME fail-closed
    cross-project guard the foreground (gateway) paths apply via
    :func:`resolve_endpoint_connection`. This convenience loads the owning
    ``Model`` to read its ``project_id`` and then delegates to
    :func:`resolve_endpoint_connection`, so the rule
    (``connection.project_id == owning_model.project_id``) stays defined in
    exactly one place.

    Raises:

    * ``ValueError`` when the ``Model`` row does not exist.
    * ``CrossProjectConnectionError`` when the connection's project differs
      from the model's project (fail-closed cross-project guard).
    * ``ValueError`` when the connection row does not exist.
    """
    model = await db.get(Model, model_id)
    if model is None:
        raise ValueError(
            f"Model {model_id} not found for connection-scope resolution"
        )
    return await resolve_endpoint_connection(
        db, endpoint, expected_project_id=model.project_id
    )
