"""Pocket generation guard — close the admission-to-scan TOCTOU (Bug-8392) and
bind the proof to the ADMITTED generation (Bug-8455).

The generic machinery (the generation stamp, the live-location proof, the
before/after comparison, the admitted-generation binding) lives in
``routing/artifact_generation_guard`` and is shared with the aggregate sibling
(Bug-8457) so the two cannot drift. Read that module's docstring for why
detection is sufficient and what producer invariants make it sound. This module
holds only the POCKET-specific parts.

The problem, restated for pockets
---------------------------------
A pocket's physical table is REUSED in place across refreshes. A
``RouteDecision`` therefore names a TABLE, not a GENERATION of that table.
Everything the router proved at admission — that the pocket is a row-preserving
``SELECT *`` slice, that its ``row_manifest`` shows every row-security column
materialised, that it was built for the currently deployed model version, and
that the query is CONTAINED in the pocket's predicate slice — was proved against
the row it read at that instant. A refresh that completes in between substitutes
a different generation:

* built from an edited ``defining_sql`` -> a narrower slice than the matcher's
  ``query ⊆ pocket`` containment assumed -> silently missing rows;
* built without the security column -> the injected predicate references a column
  that no longer exists -> fail-closed database error (safe, but a 502);
* built with a same-named column carrying different content -> the injected
  row-security predicate filters on the wrong thing -> rows the principal must not
  see. This is the leak Bug-8392 was raised for, and it became reachable the
  moment Bug-8393 activated RLS-safe pocket serving.

Pocket-specific producer notes
------------------------------
The invariants live in ``shared/pocket/refresh.refresh_pocket_definition`` (the
ONLY writer of ``status="fresh"`` and the only code path that replaces a pocket
table): ``status="invalidating"`` is committed before any target DDL (I1);
returning to ``fresh`` always carries a NEW ``active_refresh_run_id`` — Bug-8393
advances the pointer on every completed refresh, INCLUDING one whose row-manifest
write failed, which is why ``shared/pocket/row_manifest.clear_pocket_row_manifest``
drops the manifest but keeps the pointer on that path (I2); a refresh that fails
AFTER starting materialisation leaves ``status="failed"`` and clears both the
manifest and the pointer (I3). The two pre-materialisation failure legs (router
validation and an unsupported connector combo) leave the previous stamp in place,
which is sound: nothing physical was touched, and ``failed`` is unservable to both
the matcher and this guard.

I4 (Bug-8807): a COMPLETED refresh whose finalisation guard REFUSES it
(``shared/pocket/refresh_guard.resolve_pocket_serving_refusal`` — the pocket was
invalidated mid-build, or the target/source routing moved under it) lands
``status="stale"`` with a reason and clears BOTH the manifest and the pointer.
It is not returning to ``fresh``, so I2's advance-the-pointer requirement does
not apply to it; the stamp ``(stale, None)`` can never compare equal to an
admitted ``(fresh, run_id)``, so this guard refuses it, and the matcher already
refuses any non-``fresh`` status.

One carve-out to I1's "only writer" claim: the demo seed script
(``tessallite/scripts/bootstrap_acme_demo_models.py``) creates pockets
``status="fresh"`` with no run, pointer or manifest. That is dev/demo only —
every production creation path materialises through the refresh chokepoint — and
such a pocket carries the constant stamp ``(fresh, None, ...)`` until its first
real refresh, at which point the stamp starts moving normally. It can never be
served under row security (no manifest => the gate fails closed), and its
constant stamp compares equal to itself, so the admitted-generation binding is a
no-op for it rather than a false refusal.

Admission binding (Bug-8455 — the residual half of Bug-8392, now closed)
------------------------------------------------------------------------
``RouteDecision.admitted_generation`` carries the stamp of the pocket the
MATCHER admitted, set at both pocket route-decision sites in ``routing/router``.
``assert_pocket_route_admissible`` requires the live generation to EQUAL it, so
the containment proof the matcher made (``query ⊆ pocket``, which is not re-run
here because re-running ``find_best_pocket`` would double the matcher cost on
the pocket hot path) is bound to the exact build it was made against. A
definition edit plus a COMPLETE refresh landing between the matcher's read and
this pre-check is now REFUSED rather than accepted.
"""
from __future__ import annotations

import types
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from shared.artifact_version_gate import artifact_built_for_current
from shared.db.models import PocketDefinition

from src.routing.artifact_generation_guard import (
    ArtifactGeneration,
    ArtifactGenerationChangedError,
    as_str,
    assert_admitted_generation,
    fetch_columns,
    generation_from,
    targets_live_location,
)
from src.routing.artifact_generation_guard import (
    assert_generation_unchanged as _assert_generation_unchanged,
)

# Re-exported under its historical private name: the Bug-8392 suite exercises
# the location re-proof directly, and the check is now shared machinery.
_targets_live_location = targets_live_location

# Only a pocket in this status may be scanned. Mirrors the matcher's own filter
# (``pocket_matcher`` selects ``status == "fresh"``); re-checked here because the
# matcher's read is the time-of-CHECK and this is the time-of-USE.
_SERVABLE_STATUS = "fresh"

_KIND = "pocket"


class PocketGenerationChangedError(ArtifactGenerationChangedError):
    """The pocket's physical generation is not (or may no longer be) the one the
    route decision was proved against.

    Callers must DISCARD any rows already read and re-route the query to source.
    This is never returned to the user as a failure when a fallback exists.
    """


# Back-compat alias: the stamp type is now the shared ``ArtifactGeneration``.
PocketGeneration = ArtifactGeneration


_GENERATION_COLUMNS = (
    PocketDefinition.status,
    PocketDefinition.population_eligibility,
    PocketDefinition.active_refresh_run_id,
    PocketDefinition.physical_table_name,
    PocketDefinition.target_schema,
    PocketDefinition.target_id,
)

_ADMISSION_COLUMNS = _GENERATION_COLUMNS + (
    PocketDefinition.defining_sql,
    PocketDefinition.row_manifest,
    PocketDefinition.built_for_version_id,
    PocketDefinition.built_for_epoch,
    # Bug-8780: needed so the source binding guard can verify consistency
    # (the recorded binding names a model_id — the serving guard must enforce
    # it describes THIS pocket's model, not another's).
    PocketDefinition.model_id,
)


def pocket_generation_of(pocket: Any) -> ArtifactGeneration:
    """Admission-time stamp of the pocket the matcher just admitted.

    Called from ``routing/router`` at each pocket RouteDecision site, against the
    ORM object the matcher read, so the stamp records the generation the
    admission proof was actually made against (Bug-8455).
    """
    return generation_from(pocket)


async def read_pocket_generation(
    db: AsyncSession, pocket_id: Any
) -> Optional[ArtifactGeneration]:
    """Current generation stamp of a pocket, or ``None`` when the row is gone."""
    row = await fetch_columns(db, PocketDefinition, pocket_id, _GENERATION_COLUMNS)
    return None if row is None else generation_from(row)


async def _binding_matches(
    row: Any,
    target: Any,
    conn: Any,
    *,
    tenant_session: Any = None,
) -> bool:
    """True when the pocket's build still points at the storage it was built on.

    Bug-8473: ``(target_schema, physical_table_name)`` names a table only inside
    a database. A modeller re-pointing ``DataTarget.project_connection_id``, or
    an admin editing that connection's endpoint, changes WHICH database those
    names resolve to without touching the pocket row at all. If the new database
    holds a same-named table (a clone, a restore, another environment) the cached
    pocket would serve ITS rows — and under row-level security the injected
    predicate would be evaluated against that foreign table's column, so a row
    that was never part of the admitted population can satisfy it. That is an
    exposure, not a stale number.

    The producer records the build's routing identity in
    ``row_manifest["target_binding"]``. Here it is recomputed from the LIVE
    target + connection about to be scanned and compared.

    A pocket with NO manifest at all is not refused here: it can never be served
    under row-level security (the RLS gate fails closed on a missing manifest),
    and refusing it would silently disable pocket acceleration for every pocket
    built before this binding existed until its next refresh. Those rely on the
    root-cause half — the control-plane writers
    (``shared/artifact_target_binding.invalidate_artifacts_for_*``) take every
    artifact on a re-pointed target out of the serving pool in the same
    transaction as the edit, which the ``status`` leg of the generation stamp
    then observes. A manifest that EXISTS but carries no usable binding IS
    refused: an unpinned manifest cannot say which database its columns
    describe, and it is exactly the artifact the RLS gate would trust.
    """
    from shared.artifact_target_binding import resolve_target_binding_dict

    from src.routing.artifact_generation_guard import logger

    manifest = row.row_manifest
    if not isinstance(manifest, dict):
        return True
    recorded = manifest.get("target_binding")
    if not isinstance(recorded, dict) or not recorded.get("routing_fingerprint"):
        return False
    try:
        live = await resolve_target_binding_dict(
            target,
            conn,
            # The binding helper opens a system-database session when this
            # tenant-scoped request needs persisted source_db.fallback_* values.
            tenant_session=tenant_session,
        )
    except Exception:
        logger.warning(
            "Could not resolve the live storage binding for pocket %s; "
            "refusing the pocket route",
            getattr(row, "id", None),
            exc_info=True,
        )
        return False
    return all(
        str(recorded.get(key) or "") == str(live.get(key) or "")
        for key in ("target_id", "project_connection_id", "routing_fingerprint")
    )


async def assert_pocket_route_admissible(
    db: AsyncSession,
    *,
    bound: Any,
    decision: Any,
    target: Any = None,
    conn: Any = None,
) -> ArtifactGeneration:
    """Re-prove the pocket route against LIVE state, then return its generation.

    Raises :class:`PocketGenerationChangedError` when the live pocket no longer
    satisfies what the route decision was built on. Each check below is a re-run
    of the corresponding admission-time gate against live state, using the same
    helper the matcher/router uses — never a second, weaker opinion.

    * the row still exists and is still ``fresh`` (a refresh in flight has
      already moved it to ``invalidating``, and a deploy/revert to ``stale``);
    * the already-rewritten SQL still names the pocket's LIVE physical location,
      so a refresh that re-bound it before this check cannot leave us proving
      one table and scanning another;
    * its build binding still matches the model's deployed pointer, using the
      same helper and the same "skip when the model is undeployed" condition the
      pocket matcher applies;
    * when the route carries a compiled row-security predicate, the LIVE row
      still passes the full RLS-safety proof — row-preserving shape plus every
      security column present in the pocket's own live ``row_manifest``, bound to
      its live refresh run;
    * (Bug-8455) the live generation is the ADMITTED generation. The matcher's
      containment gate (``query ⊆ pocket``) is not re-run here, so this binding
      is what makes the checks above statements about the build the matcher
      proved rather than about whatever happens to be live.
    """
    pocket_id = getattr(decision, "pocket_id", None)
    row = await fetch_columns(db, PocketDefinition, pocket_id, _ADMISSION_COLUMNS)
    if row is None:
        raise PocketGenerationChangedError(
            f"Pocket {pocket_id} no longer exists; re-routing to source"
        )

    if as_str(row.status) != _SERVABLE_STATUS:
        raise PocketGenerationChangedError(
            f"Pocket {pocket_id} is no longer {_SERVABLE_STATUS} "
            f"(status={row.status!r}) at execution time; re-routing to source"
        )

    if as_str(getattr(row, "population_eligibility", None)) == "ineligible":
        raise PocketGenerationChangedError(
            f"Pocket {pocket_id} has an unproven join population at execution "
            "time; re-routing to source"
        )

    if not targets_live_location(
        getattr(decision, "rewritten_query", ""),
        row,
        getattr(decision, "target_dialect", None),
    ):
        raise PocketGenerationChangedError(
            f"Pocket {pocket_id} no longer lives where the rewritten query "
            f"points (schema={row.target_schema!r} "
            f"table={row.physical_table_name!r}); re-routing to source"
        )

    model = getattr(bound, "model", None)
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is not None and not artifact_built_for_current(
        row.built_for_version_id,
        row.built_for_epoch,
        deployed_version_id,
        getattr(model, "deploy_epoch", 0),
    ):
        raise PocketGenerationChangedError(
            f"Pocket {pocket_id} was built for a different deployed model "
            f"version than the one now serving; re-routing to source"
        )

    # Bug-8761: target metadata cannot override the connection the serving
    # path actually opens. Refuse legacy mismatches and dotted/unprovable
    # BigQuery targets before any manifest or storage binding can certify them.
    if target is not None and conn is not None:
        from shared.config.source_db import target_connection_authority_is_provable

        if not target_connection_authority_is_provable(target, conn):
            raise PocketGenerationChangedError(
                f"Pocket {pocket_id} has an unsafe legacy target/connection "
                "authority binding; re-routing to source"
            )

    # Bug-8473: prove the storage identity BEFORE the row-security proof — a
    # manifest that describes a table on a different database must not be used
    # to prove anything at all. Only enforced when the caller supplied the live
    # target/connection it is about to scan; the execution chokepoint always
    # does, and it has both in hand already, so this costs no extra query.
    if target is not None and conn is not None and not await _binding_matches(
        row,
        target,
        conn,
        tenant_session=db,
    ):
        raise PocketGenerationChangedError(
            f"Pocket {pocket_id} was built against a different target "
            f"connection than the one it would now be scanned on "
            f"(target={getattr(target, 'id', None)}); re-routing to source"
        )

    # Bug-8780: prove the SOURCE identity too, for the same reason and in the
    # same place — a row_manifest that describes rows read from a different
    # database must not be trusted. Unlike the target check this requires no
    # caller-supplied objects: the recorded binding names the model, and the
    # live source is resolved from it.
    if not await _source_binding_matches(db, row):
        raise PocketGenerationChangedError(
            f"Pocket {pocket_id} was built from a different source "
            f"database than the model now reads; re-routing to source"
        )

    compiled = getattr(decision, "security_compiled", None)
    if compiled is not None:
        # Local import: the proof lives with the admission-time gate so the two
        # can never drift apart. Imported lazily to keep this module free of an
        # import-time dependency on the router.
        from src.routing.router import _pocket_is_rls_safe

        live_view = types.SimpleNamespace(
            id=pocket_id,
            defining_sql=row.defining_sql,
            row_manifest=row.row_manifest,
            active_refresh_run_id=row.active_refresh_run_id,
        )
        if not _pocket_is_rls_safe(live_view, compiled):
            raise PocketGenerationChangedError(
                f"Pocket {pocket_id} is no longer provably row-security-safe at "
                f"execution time; re-routing to source"
            )

    live = generation_from(row)
    # Bug-8455: bind the whole proof above to the generation the MATCHER
    # admitted. Last, so the more specific diagnostics above win when both
    # apply.
    assert_admitted_generation(
        live,
        getattr(decision, "admitted_generation", None),
        kind=_KIND,
        artifact_id=pocket_id,
        error_cls=PocketGenerationChangedError,
    )
    return live


async def _source_binding_matches(
    db: AsyncSession,
    row: Any,
) -> bool:
    """True when the pocket's rows still come from the database it read at
    build time (Bug-8780 — the source sibling of :func:`_binding_matches`).

    A pocket's rows were materialised from whichever database the model's
    ``DataSource`` rows addressed at build time. Editing that connection's
    endpoint (or re-pointing ``DataSource.project_connection_id``) changes
    WHICH database the source-route fallback reads from, while the already-built
    pocket keeps serving rows from the OLD database — two routes, two answers.

    Three control-plane acts can move the source without touching the pocket:

    * ``DataSource.project_connection_id`` re-pointed ->
      ``invalidate_artifacts_for_model``, scoped on the MODEL;
    * the source ``ProjectConnection``'s endpoint edited ->
      ``invalidate_artifacts_for_source_connection``;
    * persisted ``source_db.fallback_*`` values changed -> NOTHING, because the
      effective endpoint moves with every stored row byte-identical. Only this
      re-derivation sees that one.

    Fail-closed — a manifest with NO ``source_binding`` key (built before
    Bug-8780) IS refused. The recorded binding must also name the same model
    as the live pocket row, so a pocket moved between models cannot prove
    against the wrong connection.

    Producer/consumer alignment follows the aggregate pattern exactly: the
    producer writes ``source_binding`` into ``row_manifest`` via the same
    ``resolve_source_binding_dict`` re-derived here (through
    ``ArtifactSourceBuildBinding.from_dict`` ->
    ``source_build_binding_matches_live``), so the fingerprint has ONE
    implementation across the refresh and serve-time processes.
    """
    from shared.artifact_target_binding import (
        ArtifactSourceBuildBinding,
        source_build_binding_matches_live,
    )

    manifest = row.row_manifest
    if not isinstance(manifest, dict):
        return False
    recorded = manifest.get("source_binding")
    if not isinstance(recorded, dict):
        return False
    binding = ArtifactSourceBuildBinding.from_dict(recorded)
    live_model_id = str(getattr(row, "model_id", "") or "")
    if live_model_id and binding.model_id != live_model_id:
        # The recorded binding describes a different model's source, so it
        # cannot prove anything about this pocket. Defence in depth: the one
        # writer that moves a pocket between models clears the manifest on
        # the same row, so this should be unreachable — refuse rather than
        # resolve the wrong model's connection.
        return False
    if not all(
        (
            binding.model_id,
            binding.source_connection_id,
            binding.source_connection_project_id,
            binding.routing_fingerprint,
        )
    ):
        # Present but unusable: it cannot identify a database, and it is
        # also the shape an OLDER binding version would have. Refuse rather
        # than silently compare a subset.
        return False
    return await source_build_binding_matches_live(db, binding)


def assert_generation_unchanged(
    before: ArtifactGeneration,
    after: Optional[ArtifactGeneration],
    *,
    pocket_id: Any,
) -> None:
    """Fail closed when the pocket's generation moved across the scan."""
    _assert_generation_unchanged(
        before,
        after,
        kind=_KIND,
        artifact_id=pocket_id,
        error_cls=PocketGenerationChangedError,
    )
