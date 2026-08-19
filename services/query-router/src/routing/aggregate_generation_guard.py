"""Aggregate generation guard — the aggregate sibling of the pocket TOCTOU
guard (Bug-8457).

Why this exists
---------------
Bug-8392 closed the admission-to-scan race for POCKETS. The aggregate route has
the identical exposure on a path that serves far more production traffic, and
the fix was never propagated to it (CLAUDE.md's shared-primitive discipline: a
new invariant added for one caller of a shared exposure is not complete until
every sibling is enumerated). Before this module, the aggregate branch of
``execute_routed_query`` loaded the definition and scanned the materialised
table with NO execution-time re-proof and NO before/after generation stamp,
while the pocket branch immediately above it had both.

What the race does on the aggregate path
----------------------------------------
An aggregate's physical table is rebuilt in place. The router proved at
admission that the aggregate is ``active``, that it was built for the currently
deployed model version, that its grain and stored statistics cover the query,
and — under active row-level security — that every security dimension column is
in its grain (``_aggregate_is_rls_safe``). A definition edit plus a rebuild
landing inside the routing tail substitutes a generation the query was never
matched against: a different grain or a different stored statistic under the
same physical column names yields silently WRONG NUMBERS (not merely a missed
acceleration), and a rebuild that dropped a security column from the grain
leaves the injected predicate referencing a column that no longer exists.

It is not an RLS leak: the compiled predicate is still injected per scan, so no
forbidden row is returned — the failure mode is missing/wrong values, the same
class and the same bounded window as Bug-8455 on the pocket side.

Construction — identical to the pocket guard, by design
--------------------------------------------------------
All three parts come from ``routing/artifact_generation_guard``:

  1. ``RouteDecision.admitted_generation`` carries the stamp of the aggregate
     the MATCHER admitted (set at all three aggregate route-decision sites in
     ``routing/router``: the ordinary match, the derived-exact serve, and the
     RLS-safe serve);
  2. ``assert_aggregate_route_admissible`` re-runs the admission gates against a
     FRESH read and requires the live generation to equal the admitted one;
  3. ``assert_generation_unchanged`` re-reads the stamp after the scan.

Producer discipline that makes detection sound here: the Bug-7903 uniform
refresh pending-guard commits ``active -> pending`` (with the prior status
durably snapshotted in ``pre_refresh_status``) BEFORE any physical change, and
a completed refresh writes a NEW ``active_refresh_run_id`` together with the
restored status. So the same ``(status, active_refresh_run_id,
physical_table_name, target_schema, target_id)`` stamp construction applies.

``PocketGenerationChangedError``'s recovery leg in ``execute_with_observation``
already covers ``route_type in ("aggregate", "pocket")``; both errors now derive
from ``ArtifactGenerationChangedError``, which is what that leg catches.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from shared.artifact_version_gate import artifact_built_for_current
from shared.db.models import AggregateDefinition

from src.routing.artifact_generation_guard import (
    ArtifactGeneration,
    ArtifactGenerationChangedError,
    as_str,
    assert_admitted_generation,
    fetch_columns,
    generation_from,
    logger,
    targets_live_location,
)
from src.routing.artifact_generation_guard import (
    assert_generation_unchanged as _assert_generation_unchanged,
)

# Only an aggregate in this status may be scanned. Mirrors the matcher's own
# filter (``aggregate_matcher`` admits ``status == "active"`` only); re-checked
# here because the matcher's read is the time-of-CHECK and this is the
# time-of-USE.
_SERVABLE_STATUS = "active"

_KIND = "aggregate"


class AggregateGenerationChangedError(ArtifactGenerationChangedError):
    """The aggregate's physical generation is not (or may no longer be) the one
    the route decision was proved against.

    Callers must DISCARD any rows already read and re-route the query to source.
    This is never returned to the user as a failure when a fallback exists.
    """


_GENERATION_COLUMNS = (
    AggregateDefinition.status,
    AggregateDefinition.active_refresh_run_id,
    AggregateDefinition.physical_table_name,
    AggregateDefinition.target_schema,
    AggregateDefinition.target_id,
)

_ADMISSION_COLUMNS = _GENERATION_COLUMNS + (
    # Bug-8250 R2: ``is_stale`` is the matcher's own hard refusal
    # (aggregate_matcher.py rejects a stale aggregate on every candidate-return
    # path), and it is the mechanism the definition binding uses to keep a
    # mid-build draft edit and a mid-build target repoint non-serving. Without
    # it here, the execution-time re-proof accepted an aggregate that had been
    # staled between admission and execution: a source-schema drift sweep sets
    # ONLY this flag (artifact_invalidation.py), leaving status "active" and
    # active_refresh_run_id untouched, so nothing else in this tuple moves.
    AggregateDefinition.is_stale,
    AggregateDefinition.built_for_version_id,
    AggregateDefinition.built_for_epoch,
    AggregateDefinition.built_for_storage_binding,
    # Bug-8602: the SOURCE half of the routing proof. A cross-database aggregate
    # (source connection A, target connection B) has no DataTarget on A, so
    # ``built_for_storage_binding`` is structurally silent about which database
    # its rows were READ FROM.
    AggregateDefinition.built_for_source_binding,
    # Round-2 review finding 4: the source binding names its own model so the
    # serving guard is self-contained, but self-containment without a
    # consistency check is how a guard ends up proving the WRONG model's
    # source. Read the live model id and refuse a binding that disagrees.
    AggregateDefinition.model_id,
    AggregateDefinition.grain,
    AggregateDefinition.grain_physical_cols,
)


def aggregate_generation_of(aggregate: Any) -> ArtifactGeneration:
    """Admission-time stamp of the aggregate the matcher just admitted.

    Called from ``routing/router`` at each aggregate RouteDecision site, against
    the ORM object the matcher read, so the stamp records the generation the
    admission proof was actually made against (Bug-8457).
    """
    return generation_from(aggregate)


async def read_aggregate_generation(
    db: AsyncSession, aggregate_id: Any
) -> Optional[ArtifactGeneration]:
    """Current generation stamp of an aggregate, or ``None`` when it is gone."""
    row = await fetch_columns(
        db, AggregateDefinition, aggregate_id, _GENERATION_COLUMNS
    )
    return None if row is None else generation_from(row)


async def _binding_matches(
    row: Any,
    target: Any,
    conn: Any,
    *,
    tenant_session: Any = None,
) -> bool:
    """True when the aggregate's build still points at the storage it was built
    on (Bug-8473/Bug-8481 parity with the pocket guard).

    ``(target_schema, physical_table_name)`` names a table only inside a
    database. A modeller re-pointing ``DataTarget.project_connection_id``, or an
    admin editing that connection's endpoint, changes WHICH database those names
    resolve to without touching the aggregate row at all; if the new database
    holds a same-named table (a clone, a restore, another environment) the
    aggregate would serve ITS rows.

    The aggregate producer records the build's routing identity in the dedicated
    ``built_for_storage_binding`` column (Bug-8481), the same dict shape the
    pocket stores under ``row_manifest["target_binding"]``. An aggregate with NO
    recorded binding is not refused — refusing would disable aggregate
    acceleration for every aggregate built before that column existed until its
    next rebuild, and those rely on the root-cause half: the control-plane
    writers (``shared/artifact_target_binding.invalidate_artifacts_for_*``) take
    every artifact on a re-pointed target out of the serving pool in the same
    transaction as the edit, which the ``status`` leg of the stamp observes. A
    binding that EXISTS but is unusable IS refused.

    Producer/consumer alignment (deep-review finding 12). The recorded value is
    written in the OPTIMIZER/SCHEDULER process
    (``creator``/``full_refresh``/``incremental_refresh`` ->
    ``apply_target_build_binding`` <- ``current_target_build_binding`` <-
    ``capture_target_build_binding``) and re-derived HERE in the query-router
    process. Those are not two independent implementations:
    ``capture_target_build_binding`` is a thin wrapper around the very same
    ``resolve_target_binding_dict`` this function calls, so the fingerprint is
    computed by ONE shared helper on both sides. The session arguments also
    match the shipped pocket guard exactly (``tenant_session=db``, no
    ``system_session``), so this check carries no divergence risk the pocket
    path has not already been running in production since Bug-8473. A parity
    test pins both properties; a cross-process divergence would otherwise
    refuse EVERY aggregate carrying a recorded binding and silently drop all
    aggregate acceleration to source, so it is worth pinning rather than
    assuming.
    """
    from shared.artifact_target_binding import resolve_target_binding_dict

    recorded = row.built_for_storage_binding
    if not isinstance(recorded, dict):
        return True
    if not recorded.get("routing_fingerprint"):
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
            "Could not resolve the live storage binding for aggregate %s; "
            "refusing the aggregate route",
            getattr(row, "id", None),
            exc_info=True,
        )
        return False
    return all(
        str(recorded.get(key) or "") == str(live.get(key) or "")
        for key in ("target_id", "project_connection_id", "routing_fingerprint")
    )


async def _source_binding_matches(
    db: AsyncSession,
    row: Any,
) -> bool:
    """True when the aggregate's rows still come from the database it read at
    build time (Bug-8602 — the source sibling of :func:`_binding_matches`).

    An aggregate is a cached ANSWER, and the answer depends on which database
    the question was asked of. Three control-plane acts can move that without
    touching the aggregate row, and each has its own root-cause invalidator
    running in the same transaction as the edit:

    * ``DataSource.project_connection_id`` re-pointed at another connection ->
      ``invalidate_artifacts_for_model``, called by the sources handler. Keyed
      on the MODEL, because neither ``ProjectConnection`` row changes.
    * the source ``ProjectConnection``'s endpoint edited ->
      ``invalidate_artifacts_for_source_connection``, folded into
      ``invalidate_artifacts_for_connection`` and called by the connections
      handler. Keyed on the CONNECTION.
    * a persisted ``source_db.fallback_*`` value changed -> NOTHING, because
      the effective endpoint moves with every stored row byte-identical. Only
      this re-derivation sees that one.

    So this check is not merely defence in depth for the first two: it is the
    sole cover for the third.

    Backward compatibility matches the target guard exactly: an aggregate with
    NO recorded source binding is NOT refused, because refusing would drop
    aggregate acceleration for every aggregate built before the column existed
    until it happens to rebuild. A binding that EXISTS but is unusable IS
    refused.

    Be precise about what that costs, because "the control-plane half covers
    them" is only two thirds true. For a binding-less aggregate the first two
    levers above ARE covered — both run an invalidator in the edit's own
    transaction. The THIRD is not: a ``source_db.fallback_*`` change has no
    invalidator anywhere, so a binding-less aggregate stays servable across it
    until its next rebuild records a binding. That residual is bounded (it
    closes itself on the first refresh of each aggregate after this ships) and
    is the same trade Bug-8482 made on the target side, but it is a residual,
    not a covered case — do not restate it as one.

    Producer/consumer alignment: the recorded value is written in the
    optimizer/scheduler by ``apply_source_build_binding`` <-
    ``capture_source_build_binding``, a thin wrapper around the same
    ``resolve_source_binding_dict`` re-derived here, so the fingerprint has ONE
    implementation across the two processes.
    """
    from shared.artifact_target_binding import (
        ArtifactSourceBuildBinding,
        source_build_binding_matches_live,
    )

    recorded = row.built_for_source_binding
    if not isinstance(recorded, dict):
        return True
    binding = ArtifactSourceBuildBinding.from_dict(recorded)
    live_model_id = str(getattr(row, "model_id", "") or "")
    if live_model_id and binding.model_id != live_model_id:
        # The recorded binding describes a different model's source, so it
        # cannot prove anything about this aggregate. Defence in depth: the one
        # writer that moves an aggregate between models clears the binding on
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
        # Present but unusable: it cannot identify a database, and it is also
        # the shape an OLDER binding version would have. Refuse rather than
        # silently compare a subset (``matches`` would refuse anyway; failing
        # here avoids paying for the live read to reach the same answer).
        return False
    # ``source_build_binding_matches_live`` already fails closed (returns False)
    # on any resolution error, so no route can be admitted on an unprovable
    # source identity.
    return await source_build_binding_matches_live(db, binding)


async def assert_aggregate_route_admissible(
    db: AsyncSession,
    *,
    bound: Any,
    decision: Any,
    target: Any = None,
    conn: Any = None,
) -> ArtifactGeneration:
    """Re-prove the aggregate route against LIVE state, then return its stamp.

    Raises :class:`AggregateGenerationChangedError` when the live aggregate no
    longer satisfies what the route decision was built on. Each check is a re-run
    of the corresponding admission-time gate against live state, using the same
    helper the matcher/router uses — never a second, weaker opinion.

    * the row still exists and is still ``active`` (a refresh in flight has
      already moved it to ``pending`` via the Bug-7903 guard, and a
      deploy/revert or a target re-point moves it too);
    * the already-rewritten SQL still names the aggregate's LIVE physical
      location, so a rebuild that re-bound it before this check cannot leave us
      proving one table and scanning another;
    * its build binding still matches the model's deployed pointer, using the
      same ``artifact_built_for_current`` helper and the same "skip when the
      model is undeployed" condition the aggregate matcher applies;
    * its recorded build storage identity still addresses the target/connection
      about to be scanned;
    * its recorded build SOURCE identity still names the database the model
      reads FROM (Bug-8602), so a cached answer computed against another
      database cannot be served as this model's;
    * when the route carries a compiled row-security predicate, the LIVE row
      still passes ``_aggregate_is_rls_safe`` — every security dimension column
      still present in the live grain and still resolvable to a physical column;
    * (Bug-8457) the live generation is the ADMITTED generation. The matcher's
      grain/measure coverage proof is not re-run here, so this binding is what
      makes the checks above statements about the build the matcher proved
      rather than about whatever happens to be live.
    """
    aggregate_id = getattr(decision, "aggregate_id", None)
    row = await fetch_columns(
        db, AggregateDefinition, aggregate_id, _ADMISSION_COLUMNS
    )
    if row is None:
        raise AggregateGenerationChangedError(
            f"Aggregate {aggregate_id} no longer exists; re-routing to source"
        )

    if as_str(row.status) != _SERVABLE_STATUS:
        raise AggregateGenerationChangedError(
            f"Aggregate {aggregate_id} is no longer {_SERVABLE_STATUS} "
            f"(status={row.status!r}) at execution time; re-routing to source"
        )

    # Bug-8250 R2: the matcher refuses a stale aggregate outright, so admitting
    # one here would serve rows the matcher would have declined had it read the
    # row a moment later. A drift sweep or a superseded build sets this flag
    # WITHOUT touching status or active_refresh_run_id, so no other check in
    # this function can stand in for it.
    if bool(getattr(row, "is_stale", False)):
        raise AggregateGenerationChangedError(
            f"Aggregate {aggregate_id} was marked stale between admission and "
            f"execution; re-routing to source"
        )

    if not targets_live_location(
        getattr(decision, "rewritten_query", ""),
        row,
        getattr(decision, "target_dialect", None),
    ):
        raise AggregateGenerationChangedError(
            f"Aggregate {aggregate_id} no longer lives where the rewritten "
            f"query points (schema={row.target_schema!r} "
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
        raise AggregateGenerationChangedError(
            f"Aggregate {aggregate_id} was built for a different deployed model "
            f"version than the one now serving; re-routing to source"
        )

    # Bug-8761: targets written before the connection-authority contract may
    # carry a forged/mismatched target_type or a dotted/unprovable BigQuery
    # location. The router always executes through ``conn``; refuse rather than
    # using legacy target metadata to certify a different physical project.
    if target is not None and conn is not None:
        from shared.config.source_db import target_connection_authority_is_provable

        if not target_connection_authority_is_provable(target, conn):
            raise AggregateGenerationChangedError(
                f"Aggregate {aggregate_id} has an unsafe legacy target/connection "
                "authority binding; re-routing to source"
            )

    # Prove the storage identity BEFORE the row-security proof — a grain that
    # describes a table on a different database must not be used to prove
    # anything at all. Only enforced when the caller supplied the live
    # target/connection it is about to scan; the execution chokepoint always
    # does, and it has both in hand already, so this costs no extra query.
    if target is not None and conn is not None and not await _binding_matches(
        row,
        target,
        conn,
        tenant_session=db,
    ):
        raise AggregateGenerationChangedError(
            f"Aggregate {aggregate_id} was built against a different target "
            f"connection than the one it would now be scanned on "
            f"(target={getattr(target, 'id', None)}); re-routing to source"
        )

    # Bug-8602: and prove the SOURCE identity too, for the same reason and in
    # the same place — a grain that describes rows read from a different
    # database must not be used to prove anything either. Unlike the target
    # check this needs no caller-supplied objects: the recorded binding names
    # the model, and the live source is resolved from it.
    if not await _source_binding_matches(db, row):
        raise AggregateGenerationChangedError(
            f"Aggregate {aggregate_id} was built from a different source "
            f"database than the model now reads; re-routing to source"
        )

    compiled = getattr(decision, "security_compiled", None)
    if compiled is not None:
        # Local import: the proof lives with the admission-time gate so the two
        # can never drift apart. Imported lazily to keep this module free of an
        # import-time dependency on the router.
        from src.routing.router import _aggregate_is_rls_safe

        # ``_aggregate_is_rls_safe`` reads only ``grain`` and
        # ``grain_physical_cols`` (through ``_build_dim_phys_lookup``), both of
        # which are in the live column read above, so the re-proof runs against
        # LIVE values rather than the session-cached ORM row.
        live_view = _LiveAggregateView(
            id=aggregate_id,
            grain=row.grain,
            grain_physical_cols=row.grain_physical_cols,
        )
        if not _aggregate_is_rls_safe(live_view, compiled):
            raise AggregateGenerationChangedError(
                f"Aggregate {aggregate_id} is no longer provably "
                f"row-security-safe at execution time; re-routing to source"
            )

    live = generation_from(row)
    # Bug-8457: bind the whole proof above to the generation the MATCHER
    # admitted. Last, so the more specific diagnostics above win when both
    # apply.
    assert_admitted_generation(
        live,
        getattr(decision, "admitted_generation", None),
        kind=_KIND,
        artifact_id=aggregate_id,
        error_cls=AggregateGenerationChangedError,
    )
    return live


class _LiveAggregateView:
    """Minimal stand-in carrying the LIVE fields ``_aggregate_is_rls_safe``
    reads.

    A real class rather than ``SimpleNamespace`` so an attribute the gate starts
    reading in future raises ``AttributeError`` here (a loud test failure)
    instead of silently reading ``None`` from a namespace and weakening the
    proof.
    """

    __slots__ = ("id", "grain", "grain_physical_cols")

    def __init__(self, *, id, grain, grain_physical_cols) -> None:  # noqa: A002
        self.id = id
        self.grain = grain
        self.grain_physical_cols = grain_physical_cols


def assert_generation_unchanged(
    before: ArtifactGeneration,
    after: Optional[ArtifactGeneration],
    *,
    aggregate_id: Any,
) -> None:
    """Fail closed when the aggregate's generation moved across the scan."""
    _assert_generation_unchanged(
        before,
        after,
        kind=_KIND,
        artifact_id=aggregate_id,
        error_cls=AggregateGenerationChangedError,
    )
