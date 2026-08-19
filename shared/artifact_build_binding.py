"""Build-start capture of the artifact-to-version binding (Bug-8412).

Companion to :mod:`shared.artifact_version_gate`. The gate decides whether an
artifact's recorded ``(built_for_version_id, built_for_epoch)`` still matches the
model's live deployed pointer. This module owns the other half of that contract:
**what those two columns must be set to, and when they must be read.**

Core invariant
--------------
``built_for_*`` records the deployed pointer the artifact's rows were BUILT FOR.
The rows are produced from the model definitions as they stood when the build
STARTED, so the pointer must be read at BUILD START and frozen as plain scalars.

Why an explicit capture and not "just read the Model object again"
------------------------------------------------------------------
A materialisation (CTAS / staging swap / DELETE+INSERT) can run for many minutes
and performs mid-build commits. Reading ``Model.deploy_epoch`` at WRITE time
returns whatever a deploy or revert has since committed, so a build that
materialised the epoch-5 definitions gets stamped with epoch 6 and then passes
``artifact_built_for_current`` — the matcher routes queries to rows built from a
superseded definition and serves a SILENT WRONG NUMBER over JDBC/XMLA.

Reading a Model ORM object that happens to be cached in the session is NOT a
substitute. It is correct only while every one of the following holds: the
session factory uses ``expire_on_commit=False``; nothing on the shared session
calls ``rollback()`` or ``expire_all()`` (both expire the identity map
regardless of that flag); and some earlier code path happened to load that Model
before the build. Those are invisible, action-at-a-distance preconditions for a
wrong-number-critical invariant. Capturing scalars removes all three.

Usage
-----
Capture once, BEFORE any physical change::

    binding = await capture_build_binding(db, agg_def.model_id)

Stamp at the end of a successful build::

    superseded = await apply_build_binding(db, agg_def, binding)

``apply_build_binding`` always writes the CAPTURED binding (never a fresh read)
and returns True when the model's pointer moved during the build. A superseded
build is fail-closed by construction — the stamped binding no longer matches the
live pointer, so the gate refuses it — and the caller should additionally mark
the artifact stale so the next sweep rebuilds it instead of leaving a
permanently unusable artifact until the next cadence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ArtifactBuildBinding:
    """The deployed pointer an in-flight build is materialising rows FOR.

    Immutable plain scalars, deliberately not ORM attributes: once captured the
    values cannot be changed by a later commit, expiry, rollback, or refresh of
    the session that produced them.

    ``version_id`` is None for an undeployed model; ``epoch`` is then None too.
    An undeployed model is never served through the gateway, and the gate treats
    a NULL ``built_for_version_id`` as incompatible.
    """

    version_id: Any | None
    epoch: int | None

    @property
    def is_deployed(self) -> bool:
        return self.version_id is not None

    def matches(self, other: "ArtifactBuildBinding") -> bool:
        """True when both bindings name the same deployed pointer.

        Two UNDEPLOYED bindings match — that is a legitimate steady state, and
        differs from ``artifact_built_for_current``, which asks the stricter
        "may this artifact SERVE" question and refuses a NULL pointer outright.

        Epoch coercion is delegated to the version gate's ``coerce_epoch`` so the
        two modules cannot drift on NULL handling: a deployed binding carrying a
        NULL epoch is a half-written state, and reporting it as NOT matching
        makes the caller treat the build as superseded — fail closed.
        """
        from shared.artifact_version_gate import coerce_epoch

        if self.version_id is None or other.version_id is None:
            return self.version_id is None and other.version_id is None
        if str(self.version_id) != str(other.version_id):
            return False
        self_epoch = coerce_epoch(self.epoch)
        other_epoch = coerce_epoch(other.epoch)
        if self_epoch is None or other_epoch is None:
            return False
        return self_epoch == other_epoch


def binding_from_model(model: Any) -> ArtifactBuildBinding:
    """Freeze a Model row's deployed pointer into plain scalars.

    Reads both attributes once, in the caller's transaction, and copies them out
    of the ORM object. ``deploy_epoch`` is only meaningful alongside a non-NULL
    ``deployed_version_id``, so an undeployed model yields ``(None, None)`` —
    matching what the deploy/revert staling pass and the gate expect.
    """
    if model is None:
        return ArtifactBuildBinding(version_id=None, epoch=None)
    version_id = getattr(model, "deployed_version_id", None)
    if version_id is None:
        return ArtifactBuildBinding(version_id=None, epoch=None)
    try:
        epoch = int(getattr(model, "deploy_epoch", 0) or 0)
    except (TypeError, ValueError):
        epoch = 0
    return ArtifactBuildBinding(version_id=version_id, epoch=epoch)


async def capture_build_binding(db: AsyncSession, model_id: Any) -> ArtifactBuildBinding:
    """Read the model's deployed pointer NOW and freeze it for the build.

    Call this BEFORE the definitions to be materialised are read and before the
    first physical change, so the captured pointer is at-or-before the state
    those definitions were read from. Ordering matters in one direction only: if
    a deploy lands between the capture and the definition read, the stamp names
    the OLDER pointer and the version gate refuses the artifact — fail closed. If
    the capture came AFTER the read, the stamp would name a newer pointer than
    the rows were built from, which is the Bug-8412 wrong-number itself.

    Uses the same direct read as :func:`current_binding` rather than ``db.get``:
    on a long-lived shared session (the scheduler sweep reuses one across many
    aggregates) the identity map can hold a Model loaded well before this build
    started, and stamping that older pointer causes needless rebuild churn.
    """
    return await current_binding(db, model_id)


async def current_binding(db: AsyncSession, model_id: Any) -> ArtifactBuildBinding:
    """Read the model's deployed pointer as it stands right now.

    A two-column SELECT, deliberately NOT ``db.get``: the build may have run for
    many minutes on a session whose identity map still holds the pre-build Model
    row, and ``db.get`` would return that cached copy and see no deploy at all.
    A direct query always reads committed state (READ COMMITTED), issues one
    round trip, and does not mutate the session's Model instance the way
    ``db.refresh`` would.

    Used only to DETECT supersession; it is never stamped onto an artifact.
    """
    from sqlalchemy import select

    from shared.db.models import Model  # local import: avoids a package cycle

    row = (
        await db.execute(
            select(Model.deployed_version_id, Model.deploy_epoch).where(
                Model.id == model_id
            )
        )
    ).first()
    if row is None:
        # Model deleted mid-build. Report a pointer that can never match a
        # captured deployed binding, so the caller treats the build as
        # superseded (fail closed) rather than assuming it is still current.
        return ArtifactBuildBinding(version_id=None, epoch=None)
    return binding_from_model(
        _PointerRow(deployed_version_id=row[0], deploy_epoch=row[1])
    )


@dataclass(frozen=True)
class _PointerRow:
    """Adapter so a two-column result row reuses ``binding_from_model``'s rules."""

    deployed_version_id: Any | None
    deploy_epoch: Any | None


async def apply_build_binding(
    db: AsyncSession,
    artifact: Any,
    binding: ArtifactBuildBinding,
) -> bool:
    """Stamp the CAPTURED build-start binding onto a freshly built artifact.

    Returns True when the model's deployed pointer moved while the build ran
    (the build is superseded). The stamp is written unconditionally either way:
    recording the pointer the rows were actually built for is what makes the
    gate refuse a superseded artifact instead of serving it.

    The caller owns the recovery decision for a superseded build (mark stale,
    log, re-queue); this function never silently upgrades the stamp to the newer
    pointer, which is precisely the Bug-8412 defect.

    The supersession probe costs one two-column SELECT and is skipped when the
    build started against an UNDEPLOYED model. That case stamps NULL, which the
    version gate rejects unconditionally whatever the model does next, so there
    is no compatible-looking outcome to detect and nothing the probe could
    change. (The artifact is then rebuilt on its ordinary refresh cadence.)
    """
    artifact.built_for_version_id = binding.version_id
    artifact.built_for_epoch = binding.epoch
    if not binding.is_deployed:
        return False
    live = await current_binding(db, artifact.model_id)
    return not binding.matches(live)
