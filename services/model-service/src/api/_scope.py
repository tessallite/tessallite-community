"""
Shared project/model scope validation helpers for route handlers.

Three families live here:

* **Path-parameter scope** — ``ensure_model_in_project`` proves the model named
  in the URL belongs to the project named in the URL.
* **Body-parameter foreign-key scope** — ``ensure_ref_in_model``,
  ``ensure_refs_in_model``, ``ensure_target_in_model`` and
  ``ensure_calendar_table_in_model`` prove that a foreign key arriving in the
  REQUEST BODY points at a row the path project and path model own, before it
  is written to a persisted entity. ``ensure_ref_in_project`` is the same
  guard one containment level up, for the body foreign keys that name a
  PROJECT-owned entity with no ``model_id`` of its own (``LLMProviderConfig``
  and friends). Use these instead of hand-rolling another
  ``row = await db.get(X, body.x); if row is None or row.model_id != model_id``
  block — four such re-inventions already exist (``measures.py``,
  ``data_tags.py``, ``glossary.py``, ``joins.py``) and the recurrence of that
  copy-paste is why the gap kept reappearing at new call sites.
* **Read-path scope** — ``scoped_select`` is the ownership predicate itself,
  exposed for handlers that LOAD an already-persisted row. The body-FK family
  guards what a request is about to WRITE; it says nothing about what is
  already STORED, and a row bound to a foreign resource before those guards
  existed keeps resolving. A handler that does
  ``row = await db.get(X, path_id)`` and compares ``row.model_id``
  afterwards has already read across the project boundary by the time it
  refuses, and a stored foreign key dereferenced with a bare ``db.get`` is
  returned or acted on with nothing having checked it at all. Build the
  predicate into the SELECT with ``scoped_select`` instead, so a foreign row
  is indistinguishable from an unknown one on the READ path exactly as it
  already is on the WRITE path.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, select

from shared.connection_scope import (
    CrossProjectConnectionError,
    resolve_endpoint_connection,
)
from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    AggregateRefreshPolicy,
    AggregateRefreshRun,
    CalendarTable,
    DataSource,
    DrillThroughSet,
    EntityTranslation,
    GlossaryAttachment,
    GlossaryEntry,
    GlossarySynonym,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    KPI,
    KPISnapshot,
    KPIVersion,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    NamedSet,
    NamedSetVersion,
    PocketDefinition,
    PocketPredicate,
    PocketRefreshPolicy,
    PocketRefreshRun,
    ProjectConnection,
    QuantileCoverage,
    SourceColumnStatistics,
    SourceStatistics,
    UserDefinedAttribute,
    UserDefinedAttributeColumnRef,
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


# ---------------------------------------------------------------------------
# Body-parameter foreign-key scope guards
# ---------------------------------------------------------------------------
#
# DEFECT CLASS: "unscoped body foreign key". A route validates the PATH
# (project_id / model_id) with ``ensure_model_in_project`` and then writes an id
# taken from the REQUEST BODY into a persisted row without proving that the
# referenced entity belongs to that model. A modeler authorized for project A
# can then bind their entity to a measure / dimension / column / table /
# calendar from project B — leaking the foreign object's identity and, worse,
# pulling it into deploy, snapshot, SQL rewrite and RBAC decisions where it is
# treated as in-scope. The path check is necessary but never sufficient.
#
# STATUS-CODE POLICY (deliberate, and different from the path helper):
#   * ``ensure_model_in_project`` answers 404. The addressed RESOURCE is the
#     thing in doubt, and 404 is chosen so the response cannot confirm that a
#     model with that id exists somewhere else in the tenant.
#   * The body-FK helpers answer 422. Here the addressed resource (the path
#     model) has already been proven to exist and to belong to the caller; what
#     is wrong is the submitted PAYLOAD. Answering 404 would misreport the path
#     resource and clients that branch on 404 would mis-handle it. This also
#     matches the ``data_tags.py`` precedent for exactly this shape.
#
# ORACLE POLICY: for the SAME submitted id, "no such row anywhere" and "a row
# that belongs to another project" MUST be indistinguishable — same status, same
# error_code, same message template, same number of queries, same code path. The
# only part of the response that may vary between two such requests is the echo
# of the id the client itself sent. Every helper below reaches that by
# construction — the ownership predicate is inside the SELECT, so "no row" is
# the single outcome for both cases and the code cannot accidentally branch on
# existence (this is the property glossary.py:537-542 had to add by hand).
# Echoing the offending id back is safe and deliberate: the id is client-
# supplied, so it discloses nothing the client did not already send. What would
# be an oracle is a DIFFERENT status/detail for the two cases, never the echo.
#
# WHY EVERY HELPER TAKES BOTH project_id AND model_id: each one proves the WHOLE
# chain — path project owns path model owns referenced row — in a single query.
# It would be cheaper to prove only "row.model_id == model_id" and document
# "caller must have called ensure_model_in_project first", but a precondition a
# reader has to remember is exactly the kind of contract this codebase has
# already lost four times. Because the project hop is inside the query, a caller
# that FORGETS ensure_model_in_project still cannot reach another project's row.
#
# What that does NOT buy you: passing a BODY-supplied model_id is still unsafe.
# The predicate is "row.model_id == model_id AND model.project_id ==
# project_id", so a body-chosen sibling model inside the caller's own project
# resolves and the helper accepts. No structural defence against that exists
# here. BOTH ids must come from the URL path — that is a requirement, not a
# convenience.
#
# WHAT THESE DO NOT DO: they prove ownership at READ time. Under READ
# COMMITTED the referenced row could in principle be re-parented between the
# check and the caller's write. These helpers deliberately do NOT take row
# locks: a validation helper that issued SELECT ... FOR UPDATE would lock the
# joined ``models`` row on every body-FK check at all ~20 sites, serialising
# unrelated writes and inviting deadlocks. Serialisation is the caller's job
# and the codebase already has the tool — ``acquire_model_definition_lock``
# (Bug-7982), which every read-modify-write route takes on the path model
# before validating. Exposure without it is small (no supported operation
# moves a row between models or projects), but it is a real boundary and it is
# named here rather than left to be rediscovered.
#
# CALL THESE BEFORE db.add(). The session runs with autoflush on, so the
# helper's own SELECT flushes anything already pending. A route that builds and
# adds the row first and guards afterwards has sent the unvalidated foreign key
# to the database before the guard could refuse it.
#
# ERROR SHAPE: ``detail`` is a dict — {error_code, field, ids, message} — the
# ``data_tags.py`` precedent, chosen so a client can branch on error_code and an
# operator is told WHICH id was wrong. ``glossary.py`` and ``joins.py`` return a
# plain string, and several frontend screens render ``detail`` directly as text.
# A route adopting these helpers MUST therefore also point its error rendering
# at ``detail.message``, or the modeller sees a blank/crashed panel instead of
# the id they mistyped.


def _as_uuid(value: Any) -> UUID | None:
    """Normalise a client-supplied id to ``UUID`` (``None`` passes through).

    Route schemas type these fields as ``UUID`` already; this exists so a
    helper called from a non-schema path (importer, agent tool, raw dict) can
    never end up comparing ``str`` to ``UUID`` and silently matching nothing —
    or, worse, matching by a differently-cased string form.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _entity_label(entity: type) -> str:
    """"ModelColumn" -> "a model column", "KPI" -> "a KPI". Wording only.

    Runs of capitals are kept intact so an acronym class does not reach the
    end user as "a k p i", and the article follows the first letter.
    """
    name = getattr(entity, "__name__", "row")
    # Split lower->Upper ("ModelColumn") and ACRONYM->Word ("KPISnapshot"),
    # but never inside a run of capitals ("KPI" stays whole).
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    words = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", words)
    # Lower-case only the words that are not acronyms, so KPI stays KPI.
    words = " ".join(w if w.isupper() else w.lower() for w in words.split())
    # "u" is excluded on purpose: "a user defined attribute", not "an".
    article = "an" if words[:1].lower() in "aeio" else "a"
    return f"{article} {words}"


def _clip(value: str, limit: int = 64) -> str:
    """Echoed ids are client input; never reflect an unbounded string."""
    return value if len(value) <= limit else value[:limit] + "..."


# An id list long enough to matter is a mistake, not a request; echo enough to
# diagnose it and no more.
_MAX_ECHOED_IDS = 20

# Bind-parameter budget per IN clause. PostgreSQL's extended protocol caps
# parameters at 65535; 1000 leaves ample headroom for the scope predicates.
_ID_BATCH = 1000

# Total distinct ids one collection may carry. Batching removed the driver-level
# ceiling, so without this a 100k-id array would simply become 100 sequential
# queries inside one request. A collection this large is a mistake, not a
# request, and the primitive owns the bound rather than 20 request schemas.
_MAX_IDS = 10_000


def _not_in_model(
    *, error_code: str, field_name: str, ids: list[str], noun: str,
    scope: str = "model",
) -> HTTPException:
    """The one error shape every body-FK helper raises.

    Identical for "no such row anywhere" and "row belongs to another project" —
    see ORACLE POLICY above.

    ``scope`` names the containment level the reference was checked against —
    "model" for the model-owned family, "project" for
    ``ensure_ref_in_project``. It only changes the noun in the message; the
    status, the error-code vocabulary and the anti-oracle property are the same
    in both, so a client can branch on ``error_code`` regardless.
    """
    shown = [_clip(i) for i in ids[:_MAX_ECHOED_IDS]]
    overflow = len(ids) - len(shown)
    listed = ", ".join(shown) + (f" and {overflow} more" if overflow > 0 else "")
    if len(ids) == 1:
        message = f"{field_name} does not reference {noun} in this {scope}: {listed}."
    else:
        message = (
            f"{field_name} contains ids that do not reference {noun} in this "
            f"{scope}: {listed}."
        )
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "error_code": error_code,
            "field": field_name,
            "ids": shown,
            "message": message,
        },
    )


# Entities that reach ``models.id`` through a PARENT row instead of carrying a
# model_id column of their own. Each entry names the foreign-key column and the
# parent entity EXPLICITLY, because several of these rows have more than one
# foreign key and picking the wrong one is a silent authorization bug — e.g.
# ``DrillThroughSet`` points at both a Measure and a ModelTable, and only the
# Measure is its owner. Chains may be several hops long (SourceColumnStatistics
# -> SourceStatistics -> DataSource -> Model); ``scoped_select`` walks them.
#
# This table is a coverage mechanism, so it is itself a place an enumeration
# blind spot can hide. ``tests/test_scope_body_fk_helpers.py`` asserts that
# EVERY entry's chain terminates at a non-nullable model_id with a real foreign
# key to models.id, so a wrong or stale declaration fails the suite rather than
# scoping a query to nothing.
_OWNERSHIP_PARENT: dict[type, tuple[Any, type]] = {
    ModelColumn: (ModelColumn.model_table_id, ModelTable),
    CalendarTable: (CalendarTable.data_source_id, DataSource),
    AggregateColumn: (
        AggregateColumn.aggregate_definition_id, AggregateDefinition,
    ),
    AggregateRefreshPolicy: (
        AggregateRefreshPolicy.aggregate_definition_id, AggregateDefinition,
    ),
    AggregateRefreshRun: (
        AggregateRefreshRun.aggregate_definition_id, AggregateDefinition,
    ),
    QuantileCoverage: (
        QuantileCoverage.aggregate_definition_id, AggregateDefinition,
    ),
    PocketPredicate: (PocketPredicate.pocket_definition_id, PocketDefinition),
    PocketRefreshPolicy: (
        PocketRefreshPolicy.pocket_definition_id, PocketDefinition,
    ),
    PocketRefreshRun: (
        PocketRefreshRun.pocket_definition_id, PocketDefinition,
    ),
    HierarchyLevel: (HierarchyLevel.hierarchy_id, HierarchyDefinition),
    HierarchyLevelAttribute: (
        HierarchyLevelAttribute.level_id, HierarchyLevel,
    ),
    GlossaryAttachment: (GlossaryAttachment.entry_id, GlossaryEntry),
    GlossarySynonym: (GlossarySynonym.entry_id, GlossaryEntry),
    KPISnapshot: (KPISnapshot.kpi_id, KPI),
    KPIVersion: (KPIVersion.kpi_id, KPI),
    NamedSetVersion: (NamedSetVersion.named_set_id, NamedSet),
    # A drill-through set belongs to the measure it drills from; its
    # source_table_id is a reference, not its owner.
    DrillThroughSet: (DrillThroughSet.measure_id, Measure),
    UserDefinedAttributeColumnRef: (
        UserDefinedAttributeColumnRef.attribute_id, UserDefinedAttribute,
    ),
    SourceStatistics: (SourceStatistics.data_source_id, DataSource),
    SourceColumnStatistics: (
        SourceColumnStatistics.source_statistics_id, SourceStatistics,
    ),
}

_MAX_CHAIN_HOPS = 8


def _fk_target_column(fk_source, parent: type):
    """The column ``fk_source`` actually references on ``parent``.

    Read from the foreign-key metadata rather than assumed to be ``parent.id``:
    a join built on the wrong column is not an error, it is a valid query with
    the wrong scope.
    """
    for fk in fk_source.foreign_keys:
        if fk.column.table is parent.__table__:
            return fk.column
    raise TypeError(
        f"{fk_source.table.name}.{fk_source.name} is not a foreign key to "
        f"{parent.__tablename__}; the _OWNERSHIP_PARENT entry declaring it "
        "does not describe a real relationship and cannot prove ownership."
    )


def _direct_model_column(entity: type):
    """The ``model_id`` column that OWNS this entity, or raise.

    A ``model_id`` attribute alone is not proof of ownership. Two extra
    conditions are required, and both reject real classes in this schema:

    * it must be NOT NULL. A nullable model_id means some rows are not
      model-owned at all (``UserAccessBinding`` has project-level rows,
      ``CollibraSyncRun``/``SolidatusSyncRun`` have tenant-level ones), so
      ``model_id == :id`` would silently EXCLUDE legitimate rows — the
      deny-everything failure direction that shipped as Bug-8864.
    * it must be a real foreign key to ``models.id``. ``QueryLog`` and
      ``QueryMissLog`` carry a loose model_id with no constraint; they are
      telemetry, not model-owned entities, and must not be reachable this way.

    Failing closed here is deliberate: the alternative is a guard that looks
    applied and proves nothing.
    """
    column = getattr(entity, "model_id", None)
    name = getattr(entity, "__name__", str(entity))
    if column is None:
        raise TypeError(
            f"{name} has no model_id column and no entry in _OWNERSHIP_PARENT, "
            "so _scope cannot prove who owns it. Add an entry naming the "
            "foreign key and parent that prove ownership — do NOT scope it ad "
            "hoc at the call site, which is how four duplicate guards got "
            "written."
        )
    table_column = entity.__table__.c["model_id"]
    # FK first, then nullability, so each rejection reports the FIRST reason it
    # is not an ownership axis and both branches stay independently testable.
    if not any(
        fk.target_fullname == "models.id" for fk in table_column.foreign_keys
    ):
        raise TypeError(
            f"{name}.model_id is not a foreign key to models.id, so it is a "
            "loose reference (telemetry/log), not an ownership axis."
        )
    if table_column.nullable:
        raise TypeError(
            f"{name}.model_id is nullable, so it is not an ownership axis: "
            "rows with a NULL model_id are owned at another level and would be "
            "silently excluded. Scope this entity at the level that actually "
            "owns it."
        )
    return column


def _direct_project_column(entity: type):
    """The ``project_id`` column that OWNS this entity, or raise.

    The project-level twin of ``_direct_model_column``, and it applies the same
    two conditions for the same two reasons:

    * it must be NOT NULL — a nullable ``project_id`` means some rows are owned
      at the tenant level, so ``project_id == :id`` would silently EXCLUDE
      legitimate rows (the deny-everything direction of Bug-8864);
    * it must be a real foreign key to ``projects.id`` — a loose,
      unconstrained ``project_id`` is a telemetry/log correlation field, not an
      ownership axis, and must not be reachable this way.

    Nothing here walks a parent chain. A project-owned entity carries its
    ``project_id`` directly by definition; an entity that reaches a project
    only through a parent is owned by that parent, and the caller should scope
    it at the level that actually owns it (usually the model-owned family
    above) rather than reaching past it to the project.
    """
    column = getattr(entity, "project_id", None)
    name = getattr(entity, "__name__", str(entity))
    if column is None:
        raise TypeError(
            f"{name} has no project_id column, so _scope cannot prove which "
            "project owns it. If it is model-owned, use ensure_ref_in_model; "
            "if it is owned some other way, scope it at the level that owns "
            "it — do NOT scope it ad hoc at the call site."
        )
    table_column = entity.__table__.c["project_id"]
    if not any(
        fk.target_fullname == "projects.id" for fk in table_column.foreign_keys
    ):
        raise TypeError(
            f"{name}.project_id is not a foreign key to projects.id, so it is "
            "a loose reference (telemetry/log), not an ownership axis."
        )
    if table_column.nullable:
        raise TypeError(
            f"{name}.project_id is nullable, so it is not an ownership axis: "
            "rows with a NULL project_id are owned at another level and would "
            "be silently excluded."
        )
    return column


def _missing_required(
    *, error_code: str, field_name: str, noun: str
) -> HTTPException:
    """A required reference was not supplied at all.

    Distinct from the not-in-model error on purpose: nothing was looked up, so
    claiming the id "does not reference ... in this model" would report a check
    that never ran. No id is echoed because none was sent.
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "error_code": error_code,
            "field": field_name,
            "ids": [],
            "message": f"{field_name} is required and must name {noun}.",
        },
    )


def scoped_select(entity: type, *, model_id: UUID, project_id: UUID):
    """SELECT over ``entity`` restricted to rows the path project+model own.

    This is the single place that knows HOW a given entity proves membership,
    so every body-FK helper shares one ownership predicate rather than each
    call site re-deciding. The emitted query always closes the full chain
    ``project -> model -> row``; see "WHY EVERY HELPER TAKES BOTH" above.

    It fails CLOSED. An entity whose ownership path is not declared raises
    ``TypeError`` at call time rather than returning an unrestricted SELECT,
    because an unrestricted SELECT is precisely the defect these helpers exist
    to prevent. Teaching it a new entity means adding an ``_OWNERSHIP_PARENT``
    entry (or confirming its model_id really is the ownership axis), which is a
    reviewable act rather than an accident of attribute naming.

    PUBLIC on purpose. Route handlers that load an ALREADY-PERSISTED row —
    where the id is a PATH parameter, or a stored foreign key being
    dereferenced — compose their own ``.where(entity.id == ...)`` onto this
    and keep their own status code (404 for a path resource; the body-FK
    helpers' 422 is for payloads). What they must not do is load by id and
    compare an attribute afterwards: the foreign row has been read by then.
    """
    if entity is Model:
        raise TypeError(
            "Model is a path resource, not a body reference; use "
            "ensure_model_in_project(db, project_id=..., model_id=...)."
        )
    if getattr(entity, "id", None) is None:
        raise TypeError(
            f"{getattr(entity, '__name__', entity)!s} has no id column and "
            "cannot be referenced by id from a request body."
        )
    stmt = select(entity)
    current = entity
    # +1: the break happens on the iteration AFTER the last hop, so a
    # bare range(_MAX_CHAIN_HOPS) would admit one hop fewer than declared.
    for _ in range(_MAX_CHAIN_HOPS + 1):
        if current not in _OWNERSHIP_PARENT:
            break
        fk_column, parent = _OWNERSHIP_PARENT[current]
        fk_source = current.__table__.c[fk_column.key]
        if fk_source.nullable:
            # Same rule _direct_model_column applies to the terminal model_id:
            # a nullable link is not an ownership axis. Joining through it
            # INNER-joins away every row whose parent is NULL, which is the
            # deny-everything failure direction that shipped as Bug-8864.
            raise TypeError(
                f"{current.__name__}.{fk_column.key} is nullable, so joining "
                "through it would silently EXCLUDE rows whose parent is NULL. "
                "Declare the level that actually owns those rows."
            )
        # Join on the column the foreign key ACTUALLY targets rather than
        # assuming the parent's primary key is called ``id``. Every parent here
        # happens to use ``id`` today, but a natural-key or renamed-PK parent
        # would otherwise produce a syntactically valid join on the wrong
        # column — silently wrong scope, not an error.
        target = _fk_target_column(fk_source, parent)
        stmt = stmt.join(parent, fk_column == target)
        current = parent
    else:
        raise TypeError(
            f"Ownership chain for {getattr(entity, '__name__', entity)!s} "
            "exceeds the hop limit; _OWNERSHIP_PARENT is cyclic or too deep."
        )
    scope_column = _direct_model_column(current)
    return (
        stmt
        .join(Model, scope_column == Model.id)
        .where(scope_column == model_id)
        .where(Model.project_id == project_id)
    )


async def _lookup_scoped(
    db, entity: type, *, ref_id: UUID, model_id: UUID, project_id: UUID
):
    """Load one ``entity`` row by id, if the path project+model own it."""
    stmt = scoped_select(
        entity, model_id=model_id, project_id=project_id
    ).where(entity.id == ref_id)
    result = await db.execute(stmt)
    # one_or_none, not first: the ownership chain joins on primary keys, so two
    # rows for one id would mean a broken schema invariant. Picking the first
    # would resolve ownership ARBITRARILY; raising surfaces the break.
    return result.scalars().one_or_none()


async def ensure_ref_in_model(
    db,
    entity: type,
    *,
    ref_id: Any | None,
    model_id: UUID,
    project_id: UUID,
    field_name: str,
    required: bool = False,
    noun: str | None = None,
    error_code: str = "REF_NOT_IN_MODEL",
):
    """Prove a single body-supplied foreign key points inside the path model.

    Closes the "unscoped body foreign key" defect class (see the section header)
    for the one-id shape — the shape ``joins.py:171-176`` and
    ``measures.py:485-490`` each re-implemented by hand.

    WHAT IT PROVES, in one query: a row with id ``ref_id`` exists, is owned by
    ``model_id`` (directly via ``entity.model_id``, or through the ownership
    join ``scoped_select`` declares for that entity), and that model belongs to
    ``project_id``. Nothing else — it makes no statement about the row's STATE;
    assert extra state in the caller, the way ``measures.py`` additionally
    requires the referenced ModelTable's ``calendar_table_id`` to be set.

    ``project_id`` and ``model_id`` MUST both come from the URL path — see
    "WHY EVERY HELPER TAKES BOTH" above for exactly how far that protects you,
    and where it does not.

    ``None`` NEVER means "not validated". The helper returns ``None`` in
    exactly one case — the caller supplied no id — and RAISES in every failure
    case, so a caller cannot mistake a rejected reference for an absent one.
    An absent OPTIONAL foreign key is not a scope violation, which is why the
    default is permissive: it stops a caller wrapping the guard in
    ``if body.x is not None:`` and forgetting to re-add it on the other branch.

    Pass ``required=True`` when the business rule needs the reference present.
    The helper then rejects ``ref_id=None`` with the same 422 instead of
    returning ``None``, so "this field must be there" is stated at the call
    site rather than left to a reader to infer from the return value.

    Every id argument is keyword-only. Two pre-existing helpers
    (``translations.py`` and the scheduler's ``sla.py``) take
    ``(db, model_id, project_id)`` positionally — in the opposite order to each
    other — which is a live transposition hazard. A transposed pair here would
    be a silently-passing authorization check, so the signature makes it
    unexpressible.

    Answers 422; see STATUS-CODE POLICY. The detail is identical whether the id
    is unknown or foreign; see ORACLE POLICY.
    """
    label = noun or _entity_label(entity)
    if ref_id is None:
        if required:
            raise _missing_required(
                error_code=error_code, field_name=field_name, noun=label
            )
        return None
    parsed = _as_uuid(ref_id)
    if parsed is None:
        # Malformed id: fail closed with the SAME error as unknown/foreign, so
        # the shape of the id cannot be probed either.
        raise _not_in_model(
            error_code=error_code,
            field_name=field_name,
            ids=[str(ref_id)],
            noun=label,
        )
    row = await _lookup_scoped(
        db, entity, ref_id=parsed, model_id=model_id, project_id=project_id
    )
    if row is None:
        raise _not_in_model(
            error_code=error_code,
            field_name=field_name,
            ids=[str(parsed)],
            noun=label,
        )
    return row


async def ensure_ref_in_project(
    db,
    entity: type,
    *,
    ref_id: Any | None,
    project_id: UUID,
    field_name: str,
    required: bool = False,
    noun: str | None = None,
    error_code: str = "REF_NOT_IN_PROJECT",
):
    """Prove a body-supplied foreign key points at a row THIS PROJECT owns.

    The same defect class as ``ensure_ref_in_model``, one containment level up.
    It exists because several body foreign keys name entities that have no
    ``model_id`` at all and are owned by the PROJECT — ``LLMProviderConfig``
    (which carries a Fernet-encrypted provider API key and a base_url),
    ``AgentJudgeRubric``, ``ProjectPersona``. Passing one of those to
    ``ensure_ref_in_model`` raises ``TypeError`` by design, so without this
    helper the only options at the call site were the hand-rolled
    ``row = await db.get(X, body.x); if row.project_id != project_id`` block
    that produced the defect class in the first place, or no check at all.

    WHAT IT PROVES, in one query: a row with id ``ref_id`` exists and its
    ``project_id`` is the project named in the URL PATH. Nothing else — assert
    any extra state in the caller.

    ``project_id`` MUST come from the URL path. A body-supplied project_id
    makes the predicate self-referential and proves nothing; that is the same
    requirement, and for the same reason, as "WHY EVERY HELPER TAKES BOTH"
    above.

    WHAT IT DOES NOT PROVE — and this is a real, named limit, not a caveat.
    Project containment is strictly weaker than model containment: it accepts
    ANY row in the caller's own project. Where the natural containment unit is
    the model, use ``ensure_ref_in_model`` instead. This helper is correct only
    where the referenced entity is genuinely project-owned, which
    ``_direct_project_column`` enforces rather than assumes: an entity whose
    ``project_id`` is nullable or is not a real foreign key to ``projects.id``
    raises instead of producing a query that looks scoped and is not.

    ``None`` NEVER means "not validated": the helper returns ``None`` only when
    the caller supplied no id, and RAISES in every failure case. Pass
    ``required=True`` to reject an absent id with the same 422.

    Answers 422 with a detail identical for "no such row anywhere" and "a row
    in another project" — see STATUS-CODE POLICY and ORACLE POLICY.
    """
    label = noun or _entity_label(entity)
    if ref_id is None:
        if required:
            raise _missing_required(
                error_code=error_code, field_name=field_name, noun=label
            )
        return None
    # The entity's fitness is settled BEFORE the id is examined, so a caller
    # cannot dodge the fail-closed declaration checks by sending a malformed
    # id. Deciding "is this entity project-owned at all" is a property of the
    # CALL SITE, not of the value the client happened to send; ordering it
    # after the id parse made a mis-declared entity answer 422 (a normal
    # rejection) instead of raising, which is precisely how a broken
    # declaration hides in a guard that still appears to work.
    if entity is Model:
        # Model carries project_id directly, so the generic path would work —
        # but a body-supplied model id is a different (and open) question from
        # a body-supplied reference INSIDE a model, and conflating them here
        # would let a caller believe this helper authorised a model. Route it
        # explicitly.
        raise TypeError(
            "Model is a path resource, not a body reference; use "
            "ensure_model_in_project(db, project_id=..., model_id=...)."
        )
    if getattr(entity, "id", None) is None:
        raise TypeError(
            f"{getattr(entity, '__name__', entity)!s} has no id column and "
            "cannot be referenced by id from a request body."
        )
    scope_column = _direct_project_column(entity)
    parsed = _as_uuid(ref_id)
    if parsed is not None:
        stmt = (
            select(entity)
            .where(scope_column == project_id)
            .where(entity.id == parsed)
        )
        result = await db.execute(stmt)
        # one_or_none, not first: two rows for one primary key would mean a
        # broken schema invariant, and picking the first would resolve
        # ownership arbitrarily.
        row = result.scalars().one_or_none()
    else:
        # Malformed id: fail closed with the SAME error as unknown/foreign, so
        # the shape of the id cannot be probed either.
        row = None
    if row is None:
        raise _not_in_model(
            error_code=error_code,
            field_name=field_name,
            ids=[str(parsed if parsed is not None else ref_id)],
            noun=label,
            scope="project",
        )
    return row


async def ensure_refs_in_model(
    db,
    entity: type,
    *,
    ref_ids: Iterable[Any] | None,
    model_id: UUID,
    project_id: UUID,
    field_name: str,
    required: bool = False,
    noun: str | None = None,
) -> list[Any]:
    """Prove every id in a body-supplied collection points inside the path model.

    The list shape of the same defect class, generalised from
    ``data_tags.py:78-107`` (F-008-14): a single foreign column smuggled into a
    ``column_ids`` array is attached to the entity and can carry a restriction
    the modeler never intended, so the whole request fails closed rather than
    the guard silently dropping the offenders.

    WHAT IT PROVES: EVERY requested id resolves to a row owned by the path
    ``project_id``/``model_id`` pair. Partial success is not a result — the
    returned list is either complete or the request is rejected. Silently
    returning the in-scope subset would let a caller persist a shorter
    collection than the user asked for.

    Duplicates are collapsed, so the returned list has one row per DISTINCT
    requested id; ``None``/empty returns ``[]`` — pass ``required=True`` to
    reject an empty collection instead. As with the single form, ``[]`` never
    means "not validated": every failure raises. Row order follows the database,
    not the request — callers that need request order should key by id.

    The rejection names the offending ids (the ``data_tags`` behaviour) because
    an operator otherwise cannot tell which of fifty ids was wrong. That is not
    an existence oracle: the ids are the caller's own input, and unknown and
    foreign ids are reported identically — see ORACLE POLICY.

    Long collections are handled here rather than delegated to 20 request
    schemas. Ids are de-duplicated, capped at ``_MAX_IDS``, and queried in
    batches of ``_ID_BATCH``: PostgreSQL's extended protocol allows 65535 bind
    parameters and SQLAlchemy expands ``IN`` to one bind per element, so an
    uncapped array from a single un-bounded schema would otherwise surface as a
    driver-level 500 instead of this 422. One primitive absorbing that is the
    whole point of the family.
    """
    raw = list(ref_ids or [])
    label = noun or _entity_label(entity)
    if not raw:
        if required:
            raise _missing_required(
                error_code="REFS_NOT_IN_MODEL",
                field_name=field_name,
                noun=label,
            )
        return []
    # Order-preserving de-duplication on the normalised form.
    wanted: dict[UUID, None] = {}
    malformed: list[str] = []
    for value in raw:
        parsed = _as_uuid(value)
        if parsed is None:
            malformed.append(str(value))
        else:
            wanted.setdefault(parsed, None)
    if malformed:
        raise _not_in_model(
            error_code="REFS_NOT_IN_MODEL",
            field_name=field_name,
            ids=sorted(malformed),
            noun=label,
        )
    if not wanted:
        return []
    distinct = list(wanted)
    if len(distinct) > _MAX_IDS:
        # A separate code and an honest message: these ids were never looked
        # up, so reporting them as "not in this model" would tell the caller
        # something the server did not check.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error_code": "TOO_MANY_IDS",
                "field": field_name,
                "message": (
                    f"{field_name} carries {len(distinct)} distinct ids; the "
                    f"limit is {_MAX_IDS}. None were validated."
                ),
            },
        )
    rows: list[Any] = []
    for start in range(0, len(distinct), _ID_BATCH):
        stmt = scoped_select(
            entity, model_id=model_id, project_id=project_id
        ).where(entity.id.in_(distinct[start:start + _ID_BATCH]))
        rows.extend((await db.execute(stmt)).scalars().all())
    found = {row.id for row in rows}
    missing = [str(i) for i in wanted if i not in found]
    if missing:
        raise _not_in_model(
            error_code="REFS_NOT_IN_MODEL",
            field_name=field_name,
            ids=missing,
            noun=label,
        )
    return rows


async def ensure_target_in_model(
    db,
    *,
    target_type: str | None,
    target_id: Any | None,
    model_id: UUID,
    project_id: UUID,
    allowed_targets: Mapping[str, type | None],
    field_name: str = "target_id",
):
    """Prove a polymorphic ``(target_type, target_id)`` pair points inside the
    path model.

    Generalises ``glossary.py:502-550`` (Bug-7253 / CF-018-Fable-F01802):
    without this guard a modeler authorized for project A could attach an entry
    to a dimension / measure / column UUID from project B and use
    ``proposed_is_hidden`` to hide or unhide a column in a model they have no
    rights to. Any polymorphic body reference has the same exposure, which is
    why this is a primitive rather than one route's private function.

    WHAT IT PROVES: ``target_type`` is one the CALLER declared legal, and the
    row it names is owned by the path project+model. ``allowed_targets`` maps
    each legal ``target_type`` to its ORM class, or to ``None`` for a type that
    carries no id at all (glossary's ``"concept"``). The map is required and is
    the ONLY accepted vocabulary: an unrecognised ``target_type`` is rejected,
    never ignored. ``glossary.py``'s hand-written ``if/elif`` chain simply falls
    through on an unknown type — safe there only because a Pydantic validator
    happens to constrain the field first. Here the fail-closed behaviour is in
    the primitive, so it does not depend on a second file staying correct.

    A type that expects an id but arrives with ``target_id=None`` is rejected
    too, rather than quietly persisting an attachment that points at nothing.

    Returns the resolved row, or ``None`` for an id-less target type.

    Answers 422 with a detail identical for unknown and foreign ids, preserving
    the anti-oracle property glossary.py:537-542 established for its column
    branch; here it holds for every branch by construction, because the
    ownership predicate lives in the query rather than in a second lookup.
    """
    if target_type is None or target_type not in allowed_targets:
        # Distinct from the id errors on purpose: target_type is caller-chosen
        # vocabulary, not a resource id, so naming it leaks nothing about which
        # rows exist.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error_code": "TARGET_TYPE_NOT_ALLOWED",
                "field": "target_type",
                "message": (
                    "target_type must be one of "
                    f"{sorted(allowed_targets)}; got {target_type!r}."
                ),
            },
        )
    entity = allowed_targets[target_type]
    noun = f"a {target_type}"
    if entity is None:
        if target_id is not None:
            # Payload-shape error, not a scope error: this target type has no
            # referent at all, so no lookup happens and nothing is disclosed.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error_code": "TARGET_ID_NOT_ALLOWED",
                    "field": field_name,
                    "message": (
                        f"target_type {target_type!r} takes no {field_name}."
                    ),
                },
            )
        return None
    if target_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error_code": "TARGET_ID_REQUIRED",
                "field": field_name,
                "message": (
                    f"target_type {target_type!r} requires a {field_name}."
                ),
            },
        )
    parsed = _as_uuid(target_id)
    row = (
        None
        if parsed is None
        else await _lookup_scoped(
            db, entity, ref_id=parsed, model_id=model_id, project_id=project_id
        )
    )
    if row is None:
        raise _not_in_model(
            error_code="TARGET_NOT_IN_MODEL",
            field_name=field_name,
            # Canonical when it parsed, raw when it did not — the same rule
            # ensure_ref_in_model applies, so the echo is uniform across the
            # family.
            ids=[str(parsed if parsed is not None else target_id)],
            noun=noun,
        )
    return row


async def ensure_calendar_table_in_model(
    db,
    *,
    calendar_table_id: Any | None,
    model_id: UUID,
    project_id: UUID,
    required: bool = False,
    field_name: str = "calendar_table_id",
) -> CalendarTable | None:
    """Prove a body-supplied ``calendar_table_id`` belongs to the caller's model.

    Same defect class, but ``CalendarTable`` is the case that needs naming,
    because it has NO model_id column. Ownership runs
    ``CalendarTable.data_source_id -> DataSource.model_id -> Model.project_id``:
    the obvious-looking ``cal.model_id != model_id`` check does not even
    compile, and the obvious workaround — load the calendar, then load its
    DataSource, then compare — is a multi-statement walk that is easy to write
    half-way (confirming the calendar exists and never checking the source's
    owner). Getting it wrong is worse here than elsewhere: the reference reaches
    a ``DataSource``, and a DataSource carries ``project_connection_id``, i.e.
    live source credentials (Bug-5325), so a slip is a credential-scope slip and
    not merely a metadata one.

    WHAT IT PROVES, in ONE query, so no hop can be skipped or go stale between
    hops:
      1. the calendar row exists;
      2. its DataSource belongs to ``model_id``;
      3. that model belongs to ``project_id``.

    It does NOT hand-build that walk. ``CalendarTable`` is a declared entry in
    ``_OWNERSHIP_PARENT``, so this delegates to the same chain machinery every
    other helper uses and inherits its tests and its mutation coverage. What
    this helper adds is a NAME a caller can find, a typed return, and this
    docstring — the three things that stop the walk being re-derived by hand.

    Both ``project_id`` and ``model_id`` MUST come from the URL path.

    ``calendar_table_id=None`` returns ``None`` (absent optional FK).

    Answers 422 with one detail for "no such calendar", "calendar of another
    model" and "calendar of another project" alike — see ORACLE POLICY.
    Bug-8878's intake note proposed 404 for its own calendar_table_id fix; this
    family answers 422 uniformly for the reason in STATUS-CODE POLICY, and that
    note needs reconciling rather than a second convention.
    """
    return await ensure_ref_in_model(
        db,
        CalendarTable,
        ref_id=calendar_table_id,
        model_id=model_id,
        project_id=project_id,
        field_name=field_name,
        required=required,
        noun="a calendar table",
        error_code="CALENDAR_TABLE_NOT_IN_MODEL",
    )


def _glossary_eligibility_criteria(model_id, target_type: str) -> tuple:
    """The ONE eligibility predicate every glossary-text surface reads through.

    Bug-5926 (approved / not superseded / visibility show-or-null) and the
    target-type filter, as WHERE criteria so the single-row and batch helpers
    can each keep their own SELECT shape without restating the rules. The
    deployment-pinned serialiser
    (``shared/model_snapshot/serialiser.py::_merge_effective_descriptions``)
    applies the identical rules over snapshot dicts; the two must not drift.
    """
    from sqlalchemy import or_
    return (
        GlossaryEntry.model_id == model_id,
        GlossaryEntry.status == "approved",
        GlossaryEntry.superseded_by.is_(None),
        or_(
            GlossaryEntry.visibility == "show",
            GlossaryEntry.visibility.is_(None),
        ),
        GlossaryAttachment.target_type == target_type,
    )


def _one_glossary_definition_select(model_id, target_type: str, target_id):
    """Latest qualifying definition for ONE target, as a scalar select."""
    return (
        select(GlossaryEntry.definition)
        .join(GlossaryAttachment, GlossaryAttachment.entry_id == GlossaryEntry.id)
        .where(*_glossary_eligibility_criteria(model_id, target_type))
        .where(GlossaryAttachment.target_id == target_id)
        .order_by(GlossaryEntry.version.desc())
        .limit(1)
    )


def _many_glossary_definitions_select(model_id, target_type: str, target_ids):
    """Latest qualifying definition per target, ordered version-desc per target."""
    return (
        select(GlossaryAttachment.target_id, GlossaryEntry.definition)
        .join(GlossaryEntry, GlossaryAttachment.entry_id == GlossaryEntry.id)
        .where(*_glossary_eligibility_criteria(model_id, target_type))
        .where(GlossaryAttachment.target_id.in_(target_ids))
        .order_by(GlossaryAttachment.target_id, GlossaryEntry.version.desc())
    )


async def glossary_text_for_target(
    db, model_id, target_type: str, target_id, fallback_column_id=None
) -> str | None:
    """Return the latest approved glossary definition attached to the given
    semantic object, or None when no entry exists.

    F-018-20: this was copy-pasted verbatim in dimensions.py and measures.py;
    hoisted here so both call one implementation.

    Bug-5926: only entries with visibility == "show" (or legacy NULL) are
    eligible for gateway metadata and public-facing surfaces.

    Bug-9392: an attachment may instead target the PHYSICAL column a dimension
    or measure is built on (``target_type == "column"``), which is where a large
    share of bootstrap-proposed terms land. When ``fallback_column_id`` is
    supplied and the object has no direct attachment of its own, the term
    attached to that column is returned. A direct dimension/measure attachment
    always wins. This mirrors ``_merge_effective_descriptions`` in
    ``shared/model_snapshot/serialiser.py`` exactly: without it the Excel task
    pane (live route) and the JDBC/XMLA catalogue (deployed snapshot) show
    DIFFERENT description text for the same object.
    """
    result = await db.execute(
        _one_glossary_definition_select(model_id, target_type, target_id)
    )
    direct = result.scalar_one_or_none()
    if direct is not None or fallback_column_id is None:
        return direct
    fallback_result = await db.execute(
        _one_glossary_definition_select(model_id, "column", fallback_column_id)
    )
    return fallback_result.scalar_one_or_none()


async def glossary_texts_for_targets(
    db,
    model_id,
    target_type: str,
    target_ids: list[UUID],
    fallback_column_ids: Mapping[UUID, UUID | None] | None = None,
) -> dict[UUID, str]:
    """Batch sibling of ``glossary_text_for_target``.

    Returns the latest approved glossary definition for every target id in a
    single query, keyed by target id. The list endpoints call this once per
    page instead of issuing one ``glossary_text_for_target`` query per row,
    eliminating the per-row N+1 (F-018-22).

    Bug-5926: only entries with visibility == "show" (or legacy NULL) are
    eligible for gateway metadata and public-facing surfaces.

    Bug-9392: ``fallback_column_ids`` maps a target id to the physical column it
    is built on (``Dimension.source_column_id`` / ``Measure.source_column_id``).
    Targets left without a direct attachment fall back to the term attached to
    their column, in ONE extra query for the whole page — same precedence as the
    single-row helper and as the serialiser.
    """
    if not target_ids:
        return {}
    result = await db.execute(
        _many_glossary_definitions_select(model_id, target_type, target_ids)
    )
    by_target: dict[UUID, str] = {}
    # Rows are ordered version-desc per target; first seen wins (latest).
    for target_id, definition in result.all():
        by_target.setdefault(target_id, definition)
    if not fallback_column_ids:
        return by_target

    # Only targets with no direct attachment consult their column.
    wanted: dict[UUID, list[UUID]] = {}
    for target_id in target_ids:
        if target_id in by_target:
            continue
        column_id = fallback_column_ids.get(target_id)
        if column_id is None:
            continue
        wanted.setdefault(column_id, []).append(target_id)
    if not wanted:
        return by_target

    column_result = await db.execute(
        _many_glossary_definitions_select(model_id, "column", list(wanted))
    )
    by_column: dict[UUID, str] = {}
    for column_id, definition in column_result.all():
        by_column.setdefault(column_id, definition)
    for column_id, dependent_targets in wanted.items():
        definition = by_column.get(column_id)
        if definition is None:
            continue
        for target_id in dependent_targets:
            by_target[target_id] = definition
    return by_target
