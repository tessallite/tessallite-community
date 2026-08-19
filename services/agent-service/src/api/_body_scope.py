"""Body-parameter foreign-key scope for agent-service routes.

DEFECT CLASS: "unscoped body foreign key". A route authorises the CALLER for
the project named in the URL PATH (``src/auth/project_access.py``) and then
writes an id taken from the REQUEST BODY into a persisted row without proving
that the referenced entity belongs to that project. Caller authorisation is
necessary and never sufficient: a modeller legitimately bound to project A can
submit project B's UUID and have it persisted, after which project A's agent
runs on project B's LLM provider credentials, judges with project B's rubric,
or reports project B's persona name back to the caller.

Every entity this service references from a request body is PROJECT-owned —
``LLMProviderConfig``, ``AgentJudgeRubric``, ``ProjectPersona``, ``Model``.
There is therefore one predicate, ``row.project_id == <path project_id>``, and
this module is the one place it lives.

Why this module and not model-service's ``src/api/_scope``
----------------------------------------------------------
``_scope`` is inside the model-service package and is not importable here, and
its family is MODEL-owned: every helper proves ``project -> model -> row``
through an ``_OWNERSHIP_PARENT`` chain. None of the entities above has a
``model_id`` at all, so that family does not apply even if it were reachable.
What this service needs is the one-hop project predicate it was ALREADY
writing by hand, in four places, with two different status codes — see the
"existing idiom" note below. This module is that idiom factored into one
named function, not a second copy of ``_scope``. Whether the project-level
predicate should eventually live in ``tessallite/shared/`` so both services
share one implementation is recorded for the user's decision in
``docs/questions/questions_body-fk-scope-primitive-placement.md``.

The existing idiom, and what changed
------------------------------------
Before this module the service had four hand-written copies of
``row = await db.get(Entity, body_id); if row is None or row.project_id !=
project_id: raise HTTPException(...)``:

* ``conversations._validate_pinned_model`` — 422
* ``conversations.create_conversation``'s inline persona check — 422
* ``agent_config.replace_allow_list`` — 400
* ``agent_config.upsert_model_context`` — 400

Four copies of one rule had already produced the failure mode this codebase
knows well: the copies drifted on status code, and the surfaces that were
never given a copy at all (``PATCH /config``, ``PUT /config``,
``PATCH /conversations/{id}``'s ``persona_id``) were simply open. The two 400
call sites are left answering 400 deliberately — an existing test and a live
client branch on that text — and the divergence is logged rather than changed
under this lane.

STATUS CODE: 422. The addressed PATH resource exists and the caller is
authorised for it; what is wrong is the submitted PAYLOAD. Answering 404 would
misreport the path resource. This matches ``conversations.py``'s existing
choice for exactly this shape.

ERROR SHAPE: a plain STRING ``detail``, deliberately, and NOT the structured
``{error_code, field, ids, message}`` dict that model-service's ``_scope``
family raises. Three SPA screens (``ProjectLLMScreen``, ``ProjectAgentTabs``,
``ModelLLMFunctions``) read ``err.response.data.detail`` and render it
straight into an MUI ``<Alert>`` typed as ``string``; handing them an object
renders nothing useful at best and throws in React at worst. The message TEXT
is aligned with ``_scope``'s wording so the two services read the same to a
user even though the envelope differs.

ANTI-ORACLE: "no such row anywhere" and "a row in another project" produce the
SAME status and the SAME message. The ownership predicate is inside the
SELECT, so there is one outcome for both and the code cannot accidentally
branch on existence. Echoing the offending id back is safe: it is the client's
own input.

CALL IT BEFORE THE WRITE — before ``db.add``, before any ``setattr`` loop, and
before any side effect the request would otherwise perform on its way to being
refused.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select


def _entity_noun(entity: type) -> str:
    """"LLMProviderConfig" -> "an LLM provider config". Wording only.

    Runs of capitals are kept intact so an acronym class does not reach the end
    user as "a l l m provider config", and the article follows the first letter.
    """
    name = getattr(entity, "__name__", "row")
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    words = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", words)
    words = " ".join(w if w.isupper() else w.lower() for w in words.split())
    article = "an" if words[:1].lower() in "aeio" else "a"
    return f"{article} {words}"


def _as_uuid(value: Any) -> Optional[UUID]:
    """Normalise a client-supplied id to ``UUID`` (``None`` passes through).

    Route schemas type these fields as ``UUID`` already; this exists so the
    helper can never compare ``str`` to ``UUID`` and silently match nothing.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _project_scope_column(entity: type):
    """The ``project_id`` column that OWNS this entity, or raise.

    Fails CLOSED. A ``project_id`` attribute alone is not proof of ownership;
    two further conditions are required and both reject real classes in this
    schema:

    * NOT NULL — a nullable ``project_id`` means some rows are owned at the
      tenant level (``UserAccessBinding`` has such rows), so
      ``project_id == :id`` would silently EXCLUDE legitimate rows: the
      deny-everything failure direction, which is just as much a defect as
      accept-everything.
    * a real foreign key to ``projects.id`` — an unconstrained ``project_id``
      is a telemetry correlation field, not an ownership axis.

    Raising here rather than emitting an unrestricted SELECT is the point: an
    unrestricted SELECT is precisely the defect this module exists to prevent,
    and it would look applied while proving nothing.
    """
    name = getattr(entity, "__name__", str(entity))
    column = getattr(entity, "project_id", None)
    if column is None:
        raise TypeError(
            f"{name} has no project_id column, so it cannot be scoped to the "
            "path project. Scope it at the level that actually owns it."
        )
    if getattr(entity, "id", None) is None:
        raise TypeError(
            f"{name} has no id column and cannot be referenced by id from a "
            "request body."
        )
    table_column = entity.__table__.c["project_id"]
    if not any(
        fk.target_fullname == "projects.id" for fk in table_column.foreign_keys
    ):
        raise TypeError(
            f"{name}.project_id is not a foreign key to projects.id, so it is "
            "a loose reference, not an ownership axis."
        )
    if table_column.nullable:
        raise TypeError(
            f"{name}.project_id is nullable, so it is not an ownership axis: "
            "rows with a NULL project_id are owned at another level and would "
            "be silently excluded."
        )
    return column


def _not_in_project(*, field_name: str, ref_id: str, noun: str) -> HTTPException:
    """The one rejection every guard in this module raises.

    Identical for "no such row anywhere" and "a row in another project" — see
    ANTI-ORACLE above.
    """
    shown = ref_id if len(ref_id) <= 64 else ref_id[:64] + "..."
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            f"{field_name} does not reference {noun} in this project: {shown}."
        ),
    )


async def ensure_ref_in_project(
    db,
    entity: type,
    *,
    ref_id: Any | None,
    project_id: UUID,
    field_name: str,
    required: bool = False,
    noun: str | None = None,
):
    """Prove a body-supplied foreign key points at a row THIS PROJECT owns.

    WHAT IT PROVES, in one query: a row with id ``ref_id`` exists in
    ``entity`` and its ``project_id`` is the project named in the URL PATH.
    Nothing else — it makes no statement about the row's state; assert extra
    state at the call site.

    ``project_id`` MUST come from the URL path. A body-supplied project_id
    makes the predicate self-referential and proves nothing.

    ``None`` NEVER means "not validated". The helper returns ``None`` in
    exactly one case — the caller supplied no id — and RAISES in every failure
    case, so a caller cannot mistake a rejected reference for an absent one.
    An absent OPTIONAL foreign key is not a scope violation, which is why the
    default is permissive; that is also what lets a caller apply the guard
    unconditionally on a PATCH field that is PRESENT-but-null (an explicit
    unbind) instead of wrapping it in ``if body.x:`` and losing the guard on
    the other branch.

    Pass ``required=True`` when the business rule needs the reference present.

    Every id argument is keyword-only so a transposed ``(ref_id, project_id)``
    pair — which would be a silently-passing authorisation check — is
    unexpressible.

    ``Model`` is an ACCEPTED entity here, unlike in model-service's
    ``_scope.ensure_ref_in_project`` which refuses it. That is a deliberate
    difference, not drift: model-service's project-addressed routes always
    carry a path ``model_id``, so a body-supplied model id there is a hazard
    the helper should not legitimise. This service's ``/projects/{id}/agent``
    routes carry NO path model, and ``primary_model_id`` / ``pinned_model_id``
    are genuine body references to a model inside the path project — refusing
    ``Model`` here would only push those two back into a hand-rolled check.
    """
    label = noun or _entity_noun(entity)
    if ref_id is None:
        if required:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{field_name} is required and must name {label}.",
            )
        return None
    scope_column = _project_scope_column(entity)
    parsed = _as_uuid(ref_id)
    if parsed is None:
        # Malformed id: fail closed with the SAME error as unknown/foreign, so
        # the shape of the id cannot be probed either.
        raise _not_in_project(
            field_name=field_name, ref_id=str(ref_id), noun=label
        )
    result = await db.execute(
        select(entity)
        .where(scope_column == project_id)
        .where(entity.id == parsed)
    )
    # one_or_none, not first: two rows for one primary key would mean a broken
    # schema invariant, and picking the first would resolve ownership
    # arbitrarily rather than surfacing the break.
    row = result.scalars().one_or_none()
    if row is None:
        raise _not_in_project(
            field_name=field_name, ref_id=str(parsed), noun=label
        )
    return row
