"""Named Query generation guard — the pocket guard, applied to NQ artifacts.

The Named Query materialised fast-path reuses the pocket generation guard
machinery (Bug-8392/Bug-8455/Bug-8473/Bug-8780) verbatim: the generic stamp
comparison and location proofs live in ``routing/artifact_generation_guard``;
the target/source storage-binding proofs are imported from the pocket guard
(they are artifact-kind-agnostic duck-typed helpers over the row manifest).

This module holds only the Named-Query-specific parts: the column set read
from ``named_query_artifacts`` (plus the owning ``named_queries`` row for
``model_id``/``definition_sql``, which the artifact row does not carry), the
``fresh`` servable status, and the execution-time re-proof.

Why the guard is needed here, restated
--------------------------------------
A Named Query result table is REUSED in place across refreshes. The serving
proof (fresh status, version binding, storage binding, row-manifest column
coverage for the projection RLS proof) was made against the row the resolver
read at admission. A refresh completing in between substitutes a different
generation. The three-part construction — admission stamp carried on the
decision, execution-time re-proof against live state bound to the ADMITTED
stamp, and a before/after stamp across the scan — is what closes it. Detection
is sufficient: on any mismatch the caller discards rows and falls back to LIVE
execution of the definition under the consumer's security context.
"""
from __future__ import annotations

import types
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.routing.artifact_generation_guard import (
    ArtifactGenerationChangedError,
    as_str,
    fetch_columns,
    generation_from,
    targets_live_location,
)
from src.routing.pocket_generation_guard import (
    _binding_matches,
    _source_binding_matches,
)
from shared.db.models import NamedQueryArtifact

_KIND = "named_query"
_SERVABLE_STATUS = "fresh"

# Bug-9189: these MUST be ORM Column objects, not name strings — fetch_columns
# does select(*columns), and SQLAlchemy 2.0 rejects a bare string
# ("Textual column expression 'status' should be explicitly declared with
# text()/column()"). As plain strings every materialised @nq serve 500'd at the
# admission guard and fell over instead of serving. Mirrors the pocket guard's
# _GENERATION_COLUMNS (PocketDefinition.<col>).
_GENERATION_COLUMNS = (
    NamedQueryArtifact.status,
    NamedQueryArtifact.active_refresh_run_id,
    NamedQueryArtifact.physical_table_name,
    NamedQueryArtifact.target_schema,
    NamedQueryArtifact.target_id,
)
_ADMISSION_COLUMNS = _GENERATION_COLUMNS + (
    # Bug-9189: _row_with_model reads row.id -> id MUST be selected, or the
    # column-SELECT row (which carries ONLY the selected columns) raises
    # AttributeError('id') on every materialised @nq serve.
    NamedQueryArtifact.id,
    NamedQueryArtifact.built_for_version_id,
    NamedQueryArtifact.built_for_epoch,
    NamedQueryArtifact.row_manifest,
)


class NamedQueryGenerationChangedError(ArtifactGenerationChangedError):
    """The artifact's generation is not the one the serving proof was made
    against — discard rows and fall back to live execution."""


async def _read_named_query_row(
    db: AsyncSession, named_query_id: Any
) -> Optional[Any]:
    """Direct column SELECT of the owning definition row (model_id + SQL)."""
    from sqlalchemy import select

    from shared.db.models import NamedQuery

    result = await db.execute(
        select(
            NamedQuery.model_id, NamedQuery.definition_sql
        ).where(NamedQuery.id == named_query_id)
    )
    return result.first()


def _row_with_model(row: Any, nq_row: Any) -> Any:
    """Attach ``model_id``/``definition_sql`` to an artifact column row.

    A simple namespace so the duck-typed binding helpers read the same
    attribute names they read on a pocket row.
    """
    return types.SimpleNamespace(
        id=row.id,
        status=row.status,
        active_refresh_run_id=row.active_refresh_run_id,
        physical_table_name=row.physical_table_name,
        target_schema=row.target_schema,
        target_id=row.target_id,
        built_for_version_id=row.built_for_version_id,
        built_for_epoch=row.built_for_epoch,
        row_manifest=row.row_manifest,
        model_id=(nq_row.model_id if nq_row is not None else None),
        definition_sql=(nq_row.definition_sql if nq_row is not None else None),
    )


async def read_named_query_generation(
    db: AsyncSession, artifact_id: Any
) -> Optional[Any]:
    """Current generation stamp of an NQ artifact, or None when the row is
    gone."""
    from shared.db.models import NamedQueryArtifact

    row = await fetch_columns(
        db, NamedQueryArtifact, artifact_id, _GENERATION_COLUMNS
    )
    return None if row is None else generation_from(row)


async def assert_named_query_route_admissible(
    db: AsyncSession,
    *,
    named_query_id: Any,
    artifact_id: Any,
    decision: Any,
    model: Any,
    target: Any = None,
    conn: Any = None,
    admitted_definition_sql: Optional[str] = None,
    security_compiled: Any = None,
    expected_population_fingerprint: Optional[str] = None,
) -> Any:
    """Re-prove the materialised serving decision against LIVE state.

    Each check re-runs the corresponding admission-time gate against live
    state (never a second, weaker opinion):

    * the artifact row still exists and is still ``fresh``;
    * the rewritten SQL still names the artifact's LIVE physical location;
    * the build binding still matches the model's deployed pointer
      (``artifact_built_for_current``);
    * the population CONTRACT still matches (Bug-9161 corrected Phase 1): the
      live manifest must carry the current ``manifest_version``, be bound to
      the artifact's live build (``build_refresh_run_id``), and carry the
      expected population fingerprint — re-proved from the manifest already
      read in the admission column SELECT, so a definition edit + refresh
      landing between admission and the scan is refused before any row is
      scanned;
    * the storage binding (target) and the source binding still match live —
      imported verbatim from the pocket guard;
    * when the route carries a compiled row-security predicate, the LIVE
      manifest still proves every security column materialised and the
      admitted definition is still the row-preserving shape (the pocket
      §5.1 proof consumed, not re-authored).

    Returns the live generation stamp on success; raises
    :class:`NamedQueryGenerationChangedError` otherwise. The caller discards
    any rows and falls back to live execution.
    """
    from shared.artifact_version_gate import artifact_built_for_current
    from shared.db.models import NamedQueryArtifact
    from shared.named_query.population_contract import (
        named_query_population_manifest_matches,
    )

    from src.routing.named_query_resolver import projection_security_proof_holds

    row = await fetch_columns(
        db, NamedQueryArtifact, artifact_id, _ADMISSION_COLUMNS
    )
    if row is None:
        raise NamedQueryGenerationChangedError(
            f"Named Query artifact {artifact_id} no longer exists; "
            f"re-routing to live execution"
        )
    if as_str(row.status) != _SERVABLE_STATUS:
        raise NamedQueryGenerationChangedError(
            f"Named Query artifact {artifact_id} is no longer "
            f"{_SERVABLE_STATUS} (status={row.status!r}) at execution time; "
            f"re-routing to live execution"
        )
    if not targets_live_location(
        getattr(decision, "rewritten_query", ""),
        row,
        getattr(decision, "target_dialect", None),
    ):
        raise NamedQueryGenerationChangedError(
            f"Named Query artifact {artifact_id} no longer lives where the "
            f"rewritten query points (schema={row.target_schema!r} "
            f"table={row.physical_table_name!r}); re-routing to live execution"
        )

    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is not None and not artifact_built_for_current(
        row.built_for_version_id,
        row.built_for_epoch,
        deployed_version_id,
        getattr(model, "deploy_epoch", 0),
    ):
        raise NamedQueryGenerationChangedError(
            f"Named Query artifact {artifact_id} was built for a different "
            f"deployed model version than the one now serving; re-routing to "
            f"live execution"
        )

    # NQ-2/Bug-9161 population-contract re-proof, immediately before the scan
    # (reuses the manifest already read in the admission columns — no extra
    # query). A None/empty expected fingerprint proves nothing and fails
    # closed, exactly like a missing/mismatched manifest.
    if not named_query_population_manifest_matches(
        manifest=row.row_manifest,
        active_refresh_run_id=row.active_refresh_run_id,
        expected_fingerprint=expected_population_fingerprint,
    ):
        raise NamedQueryGenerationChangedError(
            f"Named Query artifact {artifact_id} no longer matches the "
            f"canonical population contract at execution time (manifest "
            f"fingerprint mismatch or missing); re-routing to live execution"
        )

    if target is not None and conn is not None:
        from shared.config.source_db import target_connection_authority_is_provable

        if not target_connection_authority_is_provable(target, conn):
            raise NamedQueryGenerationChangedError(
                f"Named Query artifact {artifact_id} has an unsafe legacy "
                f"target/connection authority binding; re-routing to live "
                f"execution"
            )

    if target is not None and conn is not None and not await _binding_matches(
        _row_with_model(row, None),
        target,
        conn,
        tenant_session=db,
    ):
        raise NamedQueryGenerationChangedError(
            f"Named Query artifact {artifact_id} was built against a "
            f"different target connection than the one it would now be "
            f"scanned on; re-routing to live execution"
        )

    nq_row = await _read_named_query_row(db, named_query_id)
    if not await _source_binding_matches(db, _row_with_model(row, nq_row)):
        raise NamedQueryGenerationChangedError(
            f"Named Query artifact {artifact_id} was built from a different "
            f"source database than the model now reads; re-routing to live "
            f"execution"
        )

    if security_compiled is not None:
        # Re-prove the projection RLS proof against the LIVE manifest and the
        # ADMITTED definition (the definition is deployed-snapshot-pinned; a
        # redeploy is refused by the version gate above).
        definition_sql = admitted_definition_sql
        if definition_sql is None:
            definition_sql = getattr(nq_row, "definition_sql", "") or ""
        user_mapping_active = bool(
            getattr(security_compiled, "mapping_source_ids", ())
        )
        security_columns = list(
            getattr(security_compiled, "security_dimension_columns", ()) or ()
        )
        if not projection_security_proof_holds(
            definition_sql=definition_sql,
            manifest=row.row_manifest,
            active_refresh_run_id=row.active_refresh_run_id,
            security_columns=security_columns,
            user_mapping_active=user_mapping_active,
        ):
            raise NamedQueryGenerationChangedError(
                f"Named Query artifact {artifact_id} is no longer provably "
                f"row-security-safe at execution time; re-routing to live "
                f"execution"
            )

    live = generation_from(row)
    from src.routing.artifact_generation_guard import assert_admitted_generation

    assert_admitted_generation(
        live,
        getattr(decision, "admitted_generation", None),
        kind=_KIND,
        artifact_id=artifact_id,
        error_cls=NamedQueryGenerationChangedError,
    )
    return live


async def assert_named_query_generation_unchanged(
    before: Any,
    after: Optional[Any],
    *,
    artifact_id: Any,
) -> None:
    """Fail closed when the artifact's generation moved across the scan."""
    from src.routing.artifact_generation_guard import (
        assert_generation_unchanged as _assert_generation_unchanged,
    )

    _assert_generation_unchanged(
        before,
        after,
        kind=_KIND,
        artifact_id=artifact_id,
        error_cls=NamedQueryGenerationChangedError,
    )
