"""Deploy-time tenant-global verification of dimension attribute relationships.

Spec: architecture_derived-grain-aggregate-routing.md §7.6.2. On model deploy the
shared verifier checks each ENABLED declared relationship over the COMPLETE tenant
population with the internal service principal (never an end-user RLS subset), and
records typed evidence bound to the selected deployed version + deploy epoch.

This is model-HEALTH evidence, not a serving authority and not a publish blocker
(spec §7.6.2): deployment may complete with a relationship unavailable so ordinary
source queries stay usable. It therefore NEVER raises and NEVER blocks the deploy —
any failure is swallowed and surfaces only as evidence/health. Phase 2 authorises
NO serving route from this evidence.

Functional dependencies are hereditary under row filtering (§9.2): a relationship
proven over the full tenant population holds in every RLS subset, which is why the
tenant-global check is the mandatory basis.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.connector_qualify import normalize_type_token
from shared.db.models import (
    DimensionAttributeRelationship,
    DimensionAttributeVerification,
    ModelColumn,
    ModelTable,
)
from shared.semantic.attribute_relationship_verifier import (
    BIJECTION,
    ERR_UNCERTIFIED_COLLATION,
    ERROR,
    PENDING,
    VERIFIED as VERIFIED_STATUS,
    RelationColumns,
    is_collation_stable_key_type,
    is_text_detail_type,
    verify_relationship,
)

logger = logging.getLogger(__name__)


async def _resolve_relation_columns(
    db: AsyncSession, rel: DimensionAttributeRelationship,
) -> Optional[RelationColumns]:
    """Resolve a relationship's key/detail physical columns + owning table.

    v1: key and detail are physical columns in the same governed relation (the
    dimension's source table). Returns None when either endpoint cannot be bound
    to a physical column (e.g. a column was SET NULL by a delete) — the caller
    then records an ERROR evidence row rather than a check.
    """
    if rel.key_column_id is None or rel.detail_column_id is None:
        return None
    key_col = await db.get(ModelColumn, rel.key_column_id)
    detail_col = await db.get(ModelColumn, rel.detail_column_id)
    if key_col is None or detail_col is None:
        return None
    table = await db.get(ModelTable, key_col.model_table_id)
    if table is None:
        return None
    return RelationColumns(
        table_ref=table.physical_name,
        key_physical=key_col.column_name,
        detail_physical=detail_col.column_name,
        detail_type=normalize_type_token(detail_col.data_type),
        key_type=normalize_type_token(key_col.data_type),
    )


async def verify_model_relationships_on_deploy(
    *,
    db: AsyncSession,
    model_id: UUID,
    deployed_version_id: Optional[UUID],
    deploy_epoch: int,
    verifier_version: str,
    conn_obj: Any,
    connector: str,
    tenant_session: Any = None,
) -> list[DimensionAttributeVerification]:
    """Verify every enabled relationship for a model and stage evidence rows.

    Adds evidence rows to ``db`` (the caller commits them in the SAME deploy
    transaction so evidence carries the committed deploy epoch). Returns the
    staged rows. NEVER raises: a resolution/connection failure records an ERROR
    row for that relationship and continues. Deploy proceeds regardless.
    """
    staged: list[DimensionAttributeVerification] = []
    try:
        rels = (
            await db.execute(
                select(DimensionAttributeRelationship).where(
                    DimensionAttributeRelationship.model_id == model_id,
                    DimensionAttributeRelationship.enabled.is_(True),
                )
            )
        ).scalars().all()
    except Exception:
        logger.warning("deploy verify: could not load relationships for model %s", model_id)
        return staged

    for rel in rels:
        evidence_kwargs = dict(
            relationship_id=rel.id,
            verifier_version=verifier_version,
            declaration_hash=rel.declaration_hash,
            deployed_version_id=deployed_version_id,
            deploy_epoch=deploy_epoch,
            scope_kind="TENANT_GLOBAL",
            artifact_kind="DEPLOY_CHECK",
        )
        try:
            cols = await _resolve_relation_columns(db, rel)
            if cols is None:
                row = DimensionAttributeVerification(
                    status=ERROR, violation_count=0,
                    error_code="UNRESOLVED_COLUMN", **evidence_kwargs,
                )
            elif (
                rel.cardinality == BIJECTION
                and (
                    is_text_detail_type(cols.detail_type)
                    or is_text_detail_type(cols.key_type)
                )
            ):
                # Bug-7892/7900: a BIJECTION relabel involving ANY text column
                # (key or detail) is certifiable ONLY when EVERY text column
                # has a DETERMINISTIC collation on the SOURCE. A non-
                # deterministic (ci/ai) collation on the detail folds distinct
                # values (Bug-7900: merged codes). A non-deterministic
                # collation on the key self-masks the reverse check (Bug-7899:
                # COUNT(DISTINCT key) folds). Non-text columns are
                # intrinsically collation-independent.
                #
                # Columns checked on the SOURCE:
                #   - detail (if text): must be deterministic
                #   - key (if text): must be deterministic
                try:
                    from shared.semantic.collation_profiler import (
                        check_columns_collation_deterministic,
                    )
                    _src_text_cols = []
                    if is_text_detail_type(cols.detail_type):
                        _src_text_cols.append(cols.detail_physical)
                    if is_text_detail_type(cols.key_type):
                        _src_text_cols.append(cols.key_physical)
                    _src_collation_ok = await check_columns_collation_deterministic(
                        conn_obj=conn_obj, connector=connector,
                        table_ref=cols.table_ref,
                        columns=_src_text_cols,
                        tenant_session=tenant_session,
                    ) if _src_text_cols else True
                except Exception:
                    _src_collation_ok = False  # fail closed
                # Key stability: non-text key is intrinsically stable; text key
                # is stable only if its declared collation is deterministic.
                key_is_stable = (
                    is_collation_stable_key_type(cols.key_type)
                    or _src_collation_ok
                )
                if not _src_collation_ok or not key_is_stable:
                    row = DimensionAttributeVerification(
                        status=ERROR, violation_count=0,
                        error_code=ERR_UNCERTIFIED_COLLATION, **evidence_kwargs,
                    )
                else:
                    # Text detail beside a collation-stable key. Bug-7898: still
                    # run the source-side NULL/forward/reverse checks now (with
                    # text certification enabled at SOURCE scope — the source data
                    # is authoritative for 1:1-ness) so a forward-violating or
                    # null-endpoint declaration is caught as BROKEN health
                    # evidence rather than silently deferred. A GENUINELY 1:1
                    # source result is NOT VERIFIED here — it is downgraded to a
                    # NON-SERVING PENDING, because serve-collation fold-safety is
                    # still unproven until the artifact is built and re-verified.
                    # PENDING never serves (trust predicate rule 2 admits only
                    # VERIFIED) and CLEARS to VERIFIED after artifact re-verify.
                    ev = await verify_relationship(
                        cols=cols,
                        cardinality=rel.cardinality,
                        connector=connector,
                        conn_obj=conn_obj,
                        tenant_session=tenant_session,
                        text_collation_certified=True,
                    )
                    if ev.status == VERIFIED_STATUS:
                        row = DimensionAttributeVerification(
                            status=PENDING, violation_count=0,
                            error_code=ERR_UNCERTIFIED_COLLATION,
                            **evidence_kwargs,
                        )
                    else:
                        row = DimensionAttributeVerification(
                            status=ev.status,
                            violation_count=ev.violation_count,
                            error_code=ev.error_code,
                            **evidence_kwargs,
                        )
            else:
                # Non-text (numeric/date/bool) details are collation-independent
                # and verify now over the complete tenant population. Text
                # certification is deferred to artifact build (branch above);
                # float/unsupported details still ERROR via the verifier's
                # certified-type gate. text_collation_certified stays False here.
                ev = await verify_relationship(
                    cols=cols,
                    cardinality=rel.cardinality,
                    connector=connector,
                    conn_obj=conn_obj,
                    tenant_session=tenant_session,
                    text_collation_certified=False,
                )
                row = DimensionAttributeVerification(
                    status=ev.status,
                    violation_count=ev.violation_count,
                    error_code=ev.error_code,
                    **evidence_kwargs,
                )
        except Exception:
            logger.warning(
                "deploy verify: relationship %s check failed (recording ERROR)",
                rel.id,
            )
            row = DimensionAttributeVerification(
                status=ERROR, violation_count=0,
                error_code="EXECUTION_ERROR", **evidence_kwargs,
            )
        db.add(row)
        staged.append(row)

    return staged


async def advance_artifact_manifest(
    *,
    db: AsyncSession,
    model_id: UUID,
    artifact: Any,
    artifact_kind: str,
    artifact_refresh_run_id: UUID,
    target_conn: Any,
    target_schema: Optional[str],
    physical_table_name: str,
    grain_keys: Optional[list] = None,
    passenger_draft: Any = None,
    source_conn: Any = None,
) -> None:
    """Phase-3 artifact-LOCAL re-verification + atomic manifest advance (§7.6.3).

    Runs the strict/forward/NULL checks over the COMPLETE BUILT artifact rows
    (not the source relation — the piece Phase 2 deferred) and, ONLY when every
    carried edge passes, advances the artifact's immutable manifest atomically:
    writes ``grain_keys``/``attribute_edges``/``passenger_columns`` (or
    ``row_manifest`` for a pocket), sets ``active_refresh_run_id`` to this exact
    run, and stages VERIFIED artifact evidence bound to the run + manifest hash.

    Fail-CLOSED for trust, fail-OPEN for the refresh: any doubt (edge not
    activatable, passenger column absent because materialisation is gated off,
    check BROKEN/ERROR, no target connection) leaves ``active_refresh_run_id``
    NULL so no route (Phase 4+) can ever trust this run. It NEVER raises and
    NEVER blocks the refresh. Authorises NO serving route (serving is shadow-only
    through Phase 4); it only records what was built.

    Must be called inside the caller's refresh transaction, AFTER the run is
    marked COMPLETED and the physical switch is done, so the manifest + pointer
    commit atomically with the run (spec §7.6.3: one lifecycle transaction).

    Spec §3.5 (built-name validation): when the passenger was materialised
    (``optimizer.derived_expression_auto_build`` on), ``passenger_draft`` supplies
    the immutable build draft. This function then STOPS constructing
    ``ArtifactRelation``/``PassengerSpec`` from SOURCE names and instead resolves
    the BUILT names — grain by ``key_grain_key_id`` -> ``grain_keys[].physical_column``,
    passenger by attribute key -> ``passenger_columns[].passenger_column``,
    diagnostics from the passenger draft — validates their lineage against the
    relationship, and verifies the complete built artifact using those PHYSICAL
    names before earning trust. Any unresolved/mismatched built name => no trust
    (source-routes). When materialisation is off, ``passenger_draft`` is ``None``
    and the source-name path is dead descriptive metadata (earns no trust).
    """
    try:
        from shared.config.resolver import get_setting
        from shared.db.models import Model
        from shared.semantic.artifact_local_verifier import (
            ArtifactRelation,
            verify_artifact_edge,
        )
        from shared.semantic.artifact_manifest import (
            MaterializedAttributeEdge,
            PassengerColumn,
            compute_manifest_hash,
        )
        from shared.semantic.attribute_relationship_verifier import (
            VERIFIED as V_OK,
            check_detail_type_certified,
            is_collation_stable_key_type,
            is_text_detail_type,
        )

        # Bug-7877: only BIJECTION relationships can earn serving trust in
        # stage 4 (N:1 is Phase 6, deferred). Filter at query level so a model
        # with only N:1 relationships hits the ``if not rels:`` early return
        # identically to a model with no relationships, avoiding a behavioral
        # asymmetry where the N:1-only path would deny the grained artifact's
        # derived-expression serving trust that a relationship-free model earns.
        rels = (
            await db.execute(
                select(DimensionAttributeRelationship).where(
                    DimensionAttributeRelationship.model_id == model_id,
                    DimensionAttributeRelationship.enabled.is_(True),
                    DimensionAttributeRelationship.cardinality == "BIJECTION",
                )
                # Same deterministic order as the planner (Fable R1 #6) so the
                # persisted ``passenger_columns`` order matches the built/draft order
                # and the incremental shape-guard's list comparison is stable.
                .order_by(DimensionAttributeRelationship.id)
            )
        ).scalars().all()

        # Spec §3.6: grain_keys are REQUIRED build truth for physical and stage-5
        # expression artifacts too — not just relationship-carrying ones. A grained
        # artifact must persist real grain_keys and NEVER hash grain_keys=[]. Take
        # the supplied draft (the shared build planner's output); when none was
        # supplied (legacy caller), fall back to the existing manifest so we never
        # regress a previously-written grain list to []. ``grain_keys=[]`` is only
        # legitimate for an artifact that genuinely has no grain.
        draft_grain_keys = grain_keys
        if draft_grain_keys is None:
            draft_grain_keys = list(getattr(artifact, "grain_keys", None) or [])
        # Normalise to plain dicts for hashing/persistence.
        grain_key_dicts = [
            gk.to_dict() if hasattr(gk, "to_dict") else dict(gk)
            for gk in draft_grain_keys
        ]

        if not rels:
            # No relationship edges, but a grained artifact still must persist its
            # real grain manifest (spec §3.6). There is no per-artifact manifest-hash
            # column for a no-edge artifact (the hash back-reference lives on edges,
            # and the consumer's ``_candidate_manifest_hash`` returns None here — an
            # expression/physical identity is proven by fingerprint / key-id + ordered
            # lineage, not by the manifest hash), so we persist the grain list only.
            if grain_key_dicts and artifact_kind != "POCKET":
                artifact.grain_keys = grain_key_dicts
                artifact.attribute_edges = []
                artifact.passenger_columns = []
                # No edge to verify -> the physical/expression grain artifact's
                # active pointer reflects this completed run (the build is the proof).
                artifact.active_refresh_run_id = artifact_refresh_run_id
            return

        # Materialisation gate (Fable R1 #5): the passenger physical column only
        # exists in the built table when the producer materialised it, and the
        # producer is the SINGLE authority for that decision — it built the CTAS with
        # the passenger fragments IFF it also built ``passenger_draft`` (its
        # ``load_passenger_draft_if_enabled`` reads BOTH the auto-build flag and
        # ``model.attribute_relationship_verification``). So ``passenger_draft is not
        # None`` IS "was materialised"; re-reading the flags here would be a SECOND
        # resolution pass (§3.6 forbids "SQL and metadata from separate resolution
        # passes") that a mid-run settings flip could desync from the actual build.
        # A non-empty draft with plans means passengers physically exist.
        passenger_materialised = passenger_draft is not None

        model = await db.get(Model, model_id)
        deployed_version_id = getattr(model, "deployed_version_id", None)
        deploy_epoch = int(getattr(model, "deploy_epoch", 0) or 0)
        try:
            verifier_version = str(await get_setting(
                "model.attribute_relationship_verifier_version", tenant_session=db,
            ))
        except Exception:
            verifier_version = "v0"

        table_ref = (
            f"{target_schema}.{physical_table_name}"
            if target_schema else physical_table_name
        )

        edges: list[MaterializedAttributeEdge] = []
        passengers: list[PassengerColumn] = []
        # Per-edge artifact-local check results, staged as evidence AFTER the
        # manifest hash is known (so each evidence row can carry it — §7.6.4 rule
        # 4 gates on the manifest hash). Tuple: (rel, evidence).
        pending_evidence: list[tuple[DimensionAttributeRelationship, Any]] = []
        all_ok = passenger_materialised  # no materialised passenger => cannot earn trust

        # Index the PHYSICAL grain keys by the source column id they materialise,
        # so an edge can reference the canonical ``key_grain_key_id`` of the grain
        # key that holds its relationship KEY column (spec §2.4 / §3.2). Restricted
        # to ``kind=PHYSICAL_COLUMN`` + ``dim:`` prefix + a SINGLE-column lineage
        # equal to exactly the key column — an expression grain key whose first leaf
        # happens to be the key column must NEVER be selected as the edge's key
        # grain (that would point the edge at an ``expr:`` id, not the physical dim).
        from shared.semantic.artifact_manifest import KIND_PHYSICAL_COLUMN
        key_id_by_source_col: dict[str, str] = {}
        grain_by_key_id: dict[str, dict] = {}
        for gk in grain_key_dicts:
            key_id = gk.get("key_id")
            if key_id:
                grain_by_key_id[str(key_id)] = gk
            inputs = gk.get("input_column_ids") or []
            if (
                key_id
                and str(key_id).startswith("dim:")
                and gk.get("kind") == KIND_PHYSICAL_COLUMN
                and len(inputs) == 1
            ):
                # Full single-column lineage tuple keyed by the sole source column.
                key_id_by_source_col[str(inputs[0])] = str(key_id)

        # Spec §3.5: the passenger + diagnostic BUILT names come from the immutable
        # build draft (the shared passenger planner's output), NOT from source names.
        # Index the draft's plans by relationship id so each edge resolves the
        # physical passenger/diagnostic columns that were actually materialised.
        passenger_plan_by_rel: dict[str, Any] = {}
        if passenger_draft is not None:
            for _plan in getattr(passenger_draft, "plans", None) or []:
                passenger_plan_by_rel[str(getattr(_plan, "relationship_id", ""))] = _plan

        for rel in rels:
            cols = await _resolve_relation_columns(db, rel)
            if cols is None:
                all_ok = False
                continue
            # Certified-type gate (spec §7.6.2). Bug-7894: a TEXT detail's
            # certification is collation-dependent and provable ONLY here, at
            # artifact-build, by the collation-aware reverse-uniqueness check over
            # the BUILT rows under the real serve collation (run inside
            # ``verify_artifact_edge`` below). So a text detail must be allowed to
            # PROCEED to that check — passing ``text_collation_certified=True``
            # asks the gate "is the type otherwise certifiable?" and lets the
            # BUILT-row reverse check be the actual collation certification. This
            # does NOT weaken the guard: if the serve collation folds two
            # source-distinct labels, the reverse check over the built rows
            # returns a counterexample -> BROKEN -> no trust (see the negative
            # test). Non-text details keep the strict gate: float/unsupported
            # types are refused here and never reach the artifact check.
            #
            # Bug-7892/7899/7900: a text relabel is certifiable ONLY when
            # ALL relevant text columns on BOTH source AND target have a
            # DETERMINISTIC collation. A folding collation on the SOURCE
            # merges distinct codes during materialization (Bug-7900: the
            # target keeps them distinct but the data is already merged ->
            # wrong numbers). A folding collation on the TARGET self-masks
            # the reverse check (Bug-7899) or produces merged served values.
            #
            # ALL-COLUMNS RULE (authoritative for serving trust):
            #   SOURCE: detail (if text), key (if text)
            #   TARGET: passenger column (if text), key grain (if text)
            # Non-text columns are intrinsically collation-independent.
            # When source_conn is None (legacy caller), fail closed for
            # text relabels (source collation unverifiable).
            if (
                is_text_detail_type(cols.detail_type)
                or is_text_detail_type(cols.key_type)
            ):
                _collation_ok = False
                try:
                    from shared.semantic.collation_profiler import (
                        check_columns_collation_deterministic,
                    )
                    from shared.source_executor import resolve_connector_type

                    # --- SOURCE columns (check each text column) ---
                    if source_conn is None:
                        raise ValueError("source_conn required for text relabel")
                    _src_connector = await resolve_connector_type(source_conn)
                    _src_text_cols = []
                    if is_text_detail_type(cols.detail_type):
                        _src_text_cols.append(cols.detail_physical)
                    if is_text_detail_type(cols.key_type):
                        _src_text_cols.append(cols.key_physical)
                    if _src_text_cols:
                        _src_ok = await check_columns_collation_deterministic(
                            conn_obj=source_conn, connector=_src_connector,
                            table_ref=cols.table_ref,
                            columns=_src_text_cols,
                            tenant_session=db,
                        )
                        if not _src_ok:
                            raise ValueError("source collation non-deterministic")

                    # --- TARGET columns (check each text column) ---
                    _tgt_connector = await _resolve_target_connector(
                        target_conn,
                    )
                    _tgt_text_cols = []
                    if is_text_detail_type(cols.detail_type):
                        _plan = passenger_plan_by_rel.get(str(rel.id))
                        if _plan and getattr(_plan, "passenger_column", None):
                            _tgt_text_cols.append(str(_plan.passenger_column))
                    if is_text_detail_type(cols.key_type) and grain_key_dicts:
                        _tgt_text_cols.append(
                            str(grain_key_dicts[0].get("physical_column") or "")
                        )
                    if _tgt_text_cols:
                        _tgt_ok = await check_columns_collation_deterministic(
                            conn_obj=target_conn, connector=_tgt_connector,
                            table_ref=table_ref,
                            columns=_tgt_text_cols,
                            tenant_session=db,
                        )
                        if not _tgt_ok:
                            raise ValueError("target collation non-deterministic")

                    _collation_ok = True
                except Exception:
                    _collation_ok = False  # fail closed
                # Key stability: non-text key is intrinsically stable; text key
                # is stable only if its collation is deterministic (already
                # included in the all-columns check above).
                key_is_stable = (
                    is_collation_stable_key_type(cols.key_type)
                    or _collation_ok
                )
                if not _collation_ok or not key_is_stable:
                    all_ok = False
                    continue
                # Bug-7894 R2 finding 1 (wrong numbers): the forward-dependency
                # diagnostic (COUNT(DISTINCT detail) per BUILD GROUP, source
                # collation) is authoritative for the full relationship KEY only
                # when the artifact grain is EXACTLY that key — one built group per
                # key. On a COMPOSITE grain (e.g. (country_id, year)) a single key
                # is split across groups; each group can have ndistinct=1 while the
                # key carries two label variants across groups ('Usa'/'USA'), which
                # then fold together in the serve-collation forward/reverse checks
                # (COUNT(DISTINCT passenger) / GROUP BY passenger) and self-mask ->
                # a false VERIFIED. Fail closed: a text relabel is certifiable ONLY
                # when the artifact grain is the single edge key. This is exactly
                # the grain that the v1 exact read serves anyway; a coarser grain
                # cannot carry a proven relabel in v1.
                key_col_str = str(rel.key_column_id) if rel.key_column_id else ""
                edge_key_grain_id = key_id_by_source_col.get(key_col_str)
                if (
                    edge_key_grain_id is None
                    or len(grain_key_dicts) != 1
                    or str(grain_key_dicts[0].get("key_id") or "") != edge_key_grain_id
                ):
                    all_ok = False
                    continue
                type_gate_err = check_detail_type_certified(
                    cols.detail_type, text_collation_certified=True,
                )
            else:
                type_gate_err = check_detail_type_certified(
                    cols.detail_type, text_collation_certified=False,
                )
            if type_gate_err:
                all_ok = False
                continue
            attribute_key = f"attr:{rel.id}"
            # The canonical id of the grain key this edge sits beside — the grain
            # key that materialises the relationship's KEY column (spec §2.4).
            key_grain_key_id = key_id_by_source_col.get(
                str(rel.key_column_id) if rel.key_column_id else ""
            )

            # Spec §3.5 built-name resolution. When materialisation is OFF, NO
            # passenger column physically exists in the built table, so we persist NO
            # passenger/edge record at all (Fable R1 #5): a descriptive source-name
            # record would LIE about the physical shape, and the incremental
            # shape-guard consumes ``passenger_columns`` as physical truth — a
            # source-name record there would let an INSERT emit passenger fragments
            # into a table lacking those columns. ``all_ok`` already started False, so
            # no trust is earned; the grained manifest still persists below.
            if not passenger_materialised:
                all_ok = False
                continue

            # Materialised: resolve/verify BUILT names from the immutable draft; NEVER
            # guess from source names. Fail-closed: any unresolved/mismatched built
            # name earns no trust (all_ok=False) and the edge/passenger are omitted.
            plan = passenger_plan_by_rel.get(str(rel.id))
            if plan is None:
                # Materialisation on but this relationship was NOT in the built draft
                # (its owning key is not a grain of this layout, or its detail did not
                # resolve): no built passenger exists to read.
                all_ok = False
                continue
            # 1. Resolve the ONE grain entry by the edge's key_grain_key_id, and
            #    2. validate it is a PHYSICAL dim grain (Fable R1 #8 — an expression
            #    grain whose single leaf happens to equal the key column must NEVER be
            #    accepted as the built KEY column) with single-column lineage == key.
            grain_entry = grain_by_key_id.get(str(plan.key_grain_key_id))
            if grain_entry is None:
                all_ok = False
                continue
            if (
                grain_entry.get("kind") != KIND_PHYSICAL_COLUMN
                or not str(grain_entry.get("key_id") or "").startswith("dim:")
            ):
                all_ok = False
                continue
            g_inputs = [str(x) for x in (grain_entry.get("input_column_ids") or [])]
            if g_inputs != [str(rel.key_column_id) if rel.key_column_id else ""]:
                all_ok = False
                continue
            key_grain_key_id = str(plan.key_grain_key_id)
            # 5. Read the KEY name only from grain_keys[].physical_column.
            key_grain_column = str(grain_entry.get("physical_column") or "")
            # 6. Read the passenger only from passenger_columns[].passenger_column.
            passenger_column = str(plan.passenger_column)
            # 4. Validate the passenger's source column == relationship detail.
            if str(plan.detail_column_id) != (
                str(rel.detail_column_id) if rel.detail_column_id else ""
            ):
                all_ok = False
                continue
            # 7. Diagnostic names come only from the passenger draft.
            ndistinct_column = str(plan.detail_ndistinct_column)
            nullcount_column = str(plan.detail_nullcount_column)
            if not key_grain_column or not passenger_column:
                all_ok = False
                continue

            passengers.append(PassengerColumn(
                passenger_column=passenger_column,
                source_column_id=str(rel.detail_column_id) if rel.detail_column_id else None,
                output_type=cols.detail_type,
                nullable=False,
                relationship_id=str(rel.id),
                attribute_key=attribute_key,
                detail_ndistinct_column=ndistinct_column,
                detail_nullcount_column=nullcount_column,
            ))
            edge = MaterializedAttributeEdge(
                relationship_id=str(rel.id),
                key_grain_column=key_grain_column,
                detail_passenger_column=passenger_column,
                cardinality=rel.cardinality,
                declaration_hash=rel.declaration_hash,
                artifact_refresh_run_id=str(artifact_refresh_run_id),
                attribute_key=attribute_key,
                key_grain_key_id=key_grain_key_id,
            )
            edges.append(edge)

            if passenger_materialised:
                # Bug-7806 / §3.5: verify the COMPLETE BUILT artifact using the
                # PHYSICAL names resolved from the draft — the same built grain key
                # column and passenger column serving reads (grain_keys[].
                # physical_column + passenger_columns[].passenger_column), never a
                # source-name guess. A missing/renamed built column fails the check
                # closed to ERROR -> no trust.
                artifact_rel = ArtifactRelation(
                    table_ref=table_ref,
                    key_grain_column=key_grain_column,
                    detail_passenger_column=passenger_column,
                    # Bug-7898: pass the BUILT forward-dependency diagnostics so
                    # verify_artifact_edge can catch a MIN()-masked forward
                    # violation / partial NULL. Resolved from the immutable draft
                    # (never source names) exactly like the passenger column.
                    detail_ndistinct_column=ndistinct_column,
                    detail_nullcount_column=nullcount_column,
                )
                connector = await _resolve_target_connector(target_conn)
                ev = await verify_artifact_edge(
                    rel=artifact_rel, cardinality=rel.cardinality,
                    connector=connector, conn_obj=target_conn, tenant_session=db,
                )
                if ev.status != V_OK:
                    all_ok = False
                edge.verification_id = None  # assigned by DB default; not read in v1
                pending_evidence.append((rel, ev))

        # Spec §3.5/§3.6: hash the REAL ordered grain list. ``grain_keys=[]`` is
        # forbidden for an artifact that has grain. The planner produced
        # ``MaterializedGrainKey`` objects; reconstruct them for the hash from the
        # dicts so a caller that supplied dicts and one that supplied objects hash
        # identically.
        from shared.semantic.artifact_manifest import MaterializedGrainKey as _MGK
        _hash_grain_keys = [
            gk if isinstance(gk, _MGK) else _MGK(**{
                k: v for k, v in gk.items()
                if k in _MGK.__dataclass_fields__
            })
            for gk in draft_grain_keys
        ]
        manifest_hash = compute_manifest_hash(
            grain_keys=_hash_grain_keys, attribute_edges=edges,
            passenger_columns=passengers,
        )
        for e in edges:
            e.manifest_hash = manifest_hash

        # Stage the artifact-local evidence now that the manifest hash is known,
        # binding each VERIFIED/BROKEN/ERROR row to the manifest it was checked
        # against (§7.6.4 rule 4). Only runs when the passenger was materialised.
        for rel, ev in pending_evidence:
            _stage_artifact_evidence(
                db, rel=rel, status=ev.status, error_code=ev.error_code,
                violation_count=ev.violation_count,
                verifier_version=verifier_version,
                deployed_version_id=deployed_version_id,
                deploy_epoch=deploy_epoch, artifact_kind=artifact_kind,
                artifact_id=artifact.id,
                artifact_refresh_run_id=artifact_refresh_run_id,
                artifact_manifest_hash=manifest_hash,
            )

        # Write the immutable manifest as descriptive build metadata regardless
        # (spec §5.3: manifests may travel), but ONLY advance the live trust
        # pointer when every edge verified over the built rows.
        if artifact_kind == "POCKET":
            # Bug-8393: this writes only the EDGE half of a pocket's row manifest
            # and is reachable only for a model with enabled BIJECTION
            # relationships. ``shared/pocket/row_manifest`` runs immediately after
            # every completed pocket refresh, carries these edges forward when
            # they are stamped with the same run, and adds the materialised
            # ``columns`` the query-router's RLS gate requires. Do not add
            # ``columns=`` here: this branch has no view of the built table.
            from shared.semantic.artifact_manifest import (
                RowManifest, compute_row_manifest_hash,
            )
            rm = RowManifest(
                deployed_version_id=str(deployed_version_id) if deployed_version_id else None,
                row_definition_fingerprint=getattr(artifact, "query_fingerprint", None),
                attribute_edges=[e.to_dict() for e in edges],
                build_refresh_run_id=str(artifact_refresh_run_id),
            )
            rm.manifest_hash = compute_row_manifest_hash(rm)
            artifact.row_manifest = rm.to_dict()
        else:
            # Persist the REAL grain manifest (spec §3.6) plus the edges/passengers.
            artifact.grain_keys = grain_key_dicts
            artifact.attribute_edges = [e.to_dict() for e in edges]
            artifact.passenger_columns = [p.to_dict() for p in passengers]

        # By this point ``rels`` is always non-empty (the no-relationship grained
        # artifact returned early above at ``if not rels:`` with its pointer set from
        # the completed build). So trust is earned ONLY when every eligible edge
        # verified over the built rows (Bug-7876); an edge-eligible artifact that
        # produced no verified edge — every edge failed the §3.5 built-name/lineage
        # check, or materialisation is off — fails closed and must NOT carry a stale
        # trust pointer from a previous run (spec §7.6.3 / pitfall 19/20).
        #
        # Bug-8393: a POCKET's ``active_refresh_run_id`` is NOT an
        # attribute-edge trust pointer. It is the liveness binding for the
        # pocket's row manifest (which build's rows the manifest describes), and
        # the query-router's RLS-safe pocket gate reads it that way. Gating it on
        # "every declared BIJECTION edge verified" made it unreachable for the
        # overwhelming majority of pockets (a model with no enabled relationship
        # never even reaches this branch — see the ``if not rels:`` early return).
        # ``shared/pocket/row_manifest.write_pocket_row_manifest`` owns that
        # pointer for pockets and writes it on every completed refresh, in the
        # same transaction as the manifest it binds to. Leave it alone here so the
        # two writers cannot disagree; aggregates keep the edge-trust semantics.
        if artifact_kind != "POCKET":
            if all_ok and edges:
                artifact.active_refresh_run_id = artifact_refresh_run_id
            else:
                artifact.active_refresh_run_id = None
    except Exception:
        logger.warning(
            "artifact-local manifest advance skipped for %s %s",
            artifact_kind, getattr(artifact, "id", None), exc_info=True,
        )
        # Fail-closed on a mid-advance error AFTER the physical swap (spec §3.6
        # lifecycle law). Bug-7873a: clear the trust pointer for BOTH shapes that
        # may have set it optimistically before the exception:
        #   (a) an edge-carrying artifact — its pointer + evidence would still match
        #       each other (rule 4) and serve stale trust over fresh data; and
        #   (b) a grained NO-edge (physical / stage-5 expression) artifact — the
        #       no-relationship early-return and the grained-no-edge branch both set
        #       ``active_refresh_run_id`` optimistically, so an exception raised AFTER
        #       that assignment but BEFORE the transaction commits (e.g. while staging
        #       evidence or hashing) would otherwise leave a pointer to a run whose
        #       manifest write did not complete. The manifest + pointer must commit
        #       atomically (spec §3.6 lifecycle law); on any doubt, no trust.
        # Only a POCKET (no ``active_refresh_run_id`` trust semantics here) is left
        # untouched.
        try:
            if artifact_kind != "POCKET":
                artifact.active_refresh_run_id = None
        except Exception:
            pass


async def _resolve_target_connector(target_conn: Any) -> str:
    from shared.source_executor import resolve_connector_type
    return await resolve_connector_type(target_conn)


def _stage_artifact_evidence(
    db: AsyncSession, *, rel: DimensionAttributeRelationship, status: str,
    error_code: Optional[str], violation_count: int, verifier_version: str,
    deployed_version_id: Optional[UUID], deploy_epoch: int, artifact_kind: str,
    artifact_id: UUID, artifact_refresh_run_id: UUID,
    artifact_manifest_hash: Optional[str] = None,
) -> None:
    """Stage one artifact-local VERIFIED/BROKEN/ERROR evidence row (§7.6.3).

    Bound to the exact refresh run + manifest hash so the Phase-4 router trust
    predicate (§7.6.4 rules 4) can match evidence to the candidate's active run
    and physical manifest.
    """
    db.add(DimensionAttributeVerification(
        relationship_id=rel.id, status=status, violation_count=violation_count,
        error_code=error_code, verifier_version=verifier_version,
        declaration_hash=rel.declaration_hash,
        deployed_version_id=deployed_version_id, deploy_epoch=deploy_epoch,
        scope_kind="PERSONA_ARTIFACT", artifact_kind=artifact_kind,
        artifact_id=artifact_id, artifact_refresh_run_id=artifact_refresh_run_id,
        artifact_manifest_hash=artifact_manifest_hash,
    ))
