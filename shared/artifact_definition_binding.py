"""Build-window binding of an artifact to the deployed DEFINITIONS (Bug-8250).

Third member of the artifact-binding family, and deliberately shaped like the
other two so all three read the same way:

===========================  =====================================  ==========================
module                       binds the build to                     detects
===========================  =====================================  ==========================
``artifact_build_binding``   the deployed pointer (version, epoch)   a deploy/revert mid-build
``artifact_target_binding``  the resolved storage endpoint           a target/connection repoint
``artifact_definition_binding`` (here) the deployed DEFINITIONS      a draft edit mid-build
===========================  =====================================  ==========================

Why the third one is needed
---------------------------
``artifact_build_binding`` proves the artifact is stamped for the pointer its
rows were built under. It says nothing about whether the DEFINITIONS behind that
pointer were the ones actually materialised. A draft edit — change a measure's
aggregation, re-point a grain dimension, add a join — does NOT move
``deploy_epoch``, so the pointer binding stays perfectly valid while the rows
were computed from definitions the router will never bind. The artifact then
passes the version gate and serves a silent wrong number.

A single up-front drift check cannot close this either: it proves the inputs
matched at one instant, and the build then runs for minutes, re-reading metadata
(``build_from_clause`` loads the join graph AFTER the classic guard ran) and
finishing long after. So the check is a PAIR, exactly like the pointer binding:

    binding = await capture_definition_binding(db, model_id=..., ...)
    if binding.has_drift:
        refuse                     # Bug-7901 semantics, unchanged
    ... materialise ...
    if not await definition_binding_still_current(db, binding):
        mark stale / do not serve  # a draft edit landed mid-build

Residual, stated rather than hidden: an edit that is applied and then reverted
back to the deployed state entirely within the build window is not detected.
Both this and the pointer binding share that hole; closing it requires a
metadata-mutation epoch, which does not exist today (``dependency_revision``
bumps only on dependency-EDGE changes, not on a value-changing edit such as
``default_agg`` sum -> avg).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.deployed_definition_drift import check_deployed_definition_drift

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtifactDefinitionBinding:
    """The deployed definitions an in-flight build is materialising rows FOR.

    Immutable plain scalars, deliberately not ORM state: nothing a later commit,
    expiry, or refresh does to the session can move these values.
    """

    model_id: Any
    deployed_version_id: Any | None
    #: Digest of the LIVE build inputs at capture time.
    live_digest: str | None
    #: Measures and grain names the build depends on, replayed by the re-check.
    measure_names: tuple[str, ...]
    grain_names: tuple[str, ...]
    #: Drift found AT CAPTURE (live already disagreed with the snapshot).
    reasons: tuple[str, ...]
    #: False when the model is undeployed, so there was nothing to bind to.
    checked: bool

    @property
    def has_drift(self) -> bool:
        return bool(self.reasons)

    def message(self, limit: int = 5) -> str:
        return "; ".join(self.reasons[:limit])


async def capture_definition_binding(
    db: AsyncSession,
    *,
    model_id: Any,
    deployed_version_id: Any,
    measure_names,
    grain_names,
) -> ArtifactDefinitionBinding:
    """Prove live == deployed and freeze the live closure digest for the build.

    Call BEFORE the first physical change, passing the SAME
    ``deployed_version_id`` the artifact will be stamped with (the value frozen
    by ``capture_build_binding``), so the guard validates against exactly the
    pointer that gets recorded.

    Never raises: a loader failure becomes a drift reason, so an unknown state
    is refused rather than mistaken for a clean one.
    """
    measure_names = tuple(measure_names)
    grain_names = tuple(grain_names)
    try:
        check = await check_deployed_definition_drift(
            db,
            model_id=model_id,
            deployed_version_id=deployed_version_id,
            measure_names=measure_names,
            grain_names=grain_names,
        )
    except Exception as exc:  # loader failure -> unknown state -> refuse
        logger.warning(
            "Deployed-definition drift check failed for model %s: %s",
            model_id, exc, exc_info=True,
        )
        return ArtifactDefinitionBinding(
            model_id=model_id,
            deployed_version_id=deployed_version_id,
            live_digest=None,
            measure_names=measure_names,
            grain_names=grain_names,
            reasons=(f"drift check failed: {exc}",),
            checked=True,
        )
    return ArtifactDefinitionBinding(
        model_id=model_id,
        deployed_version_id=check.deployed_version_id,
        live_digest=check.live_digest,
        measure_names=measure_names,
        grain_names=grain_names,
        reasons=check.reasons,
        checked=check.checked,
    )


async def definition_binding_still_current(
    db: AsyncSession,
    binding: ArtifactDefinitionBinding,
) -> bool:
    """True when the build inputs still match what was captured AND the snapshot.

    Call at stamp time, inside the transaction that activates the artifact. A
    False result means a definition edit landed while the build ran, so the rows
    on disk are not provably the deployed definition's output: the caller must
    keep the artifact non-serving (stale) rather than stamp it compatible.

    An undeployed model returns True: there is no deployed definition to
    contradict, and ``apply_build_binding`` stamps a NULL pointer that the
    version gate refuses unconditionally, so nothing can serve either way.
    """
    if not binding.checked:
        return True
    if binding.has_drift or binding.live_digest is None:
        # Capture already refused; the caller should not have built at all. Report
        # not-current so a caller that ignored the capture result still fails closed.
        return False
    try:
        check = await check_deployed_definition_drift(
            db,
            model_id=binding.model_id,
            deployed_version_id=binding.deployed_version_id,
            measure_names=binding.measure_names,
            grain_names=binding.grain_names,
        )
    except Exception as exc:
        logger.warning(
            "Could not re-prove deployed-definition binding for model %s; "
            "treating the build as superseded: %s",
            binding.model_id, exc, exc_info=True,
        )
        return False
    if check.has_drift:
        logger.warning(
            "Definitions for model %s drifted from the deployed snapshot during "
            "the build: %s",
            binding.model_id, check.message(),
        )
        return False
    if check.live_digest != binding.live_digest:
        logger.warning(
            "Build inputs for model %s changed during the build "
            "(digest %s -> %s); the materialised rows are not provably the "
            "deployed definition's output",
            binding.model_id, binding.live_digest, check.live_digest,
        )
        return False
    return True
