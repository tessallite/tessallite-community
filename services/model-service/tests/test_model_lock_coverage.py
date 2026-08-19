"""Producer-derived guard: every model-scoped WRITE endpoint must EFFECTIVELY
acquire the per-model advisory lock (Bug-7982).

The lock is the primitive that stops a concurrent write from being lost / a
version snapshot from being mixed / a governance change from serving fail-open
during a revert. A hand-maintained enumeration of "which modules to check" (the
R3-round design) can silently miss a WHOLE MODULE the same way a hand-maintained
router attribute missed a second router — the Codex R6 gate proved this (it found
snapshot-owned writer modules absent from the list entirely).

Bug-7982 R6 finding 3 (STRUCTURAL): the module/endpoint list is now DERIVED from
the registered FastAPI routes. ``_model_scoped_mutating_endpoints`` discovers
EVERY ``src.api`` submodule (via ``pkgutil``), enumerates EVERY ``APIRouter``
instance in each (not just the one named ``router``), and yields every route
whose path is model-scoped (contains ``{model_id}``) and uses a mutating HTTP
method. A newly-added module with a model-scoped write route is therefore caught
automatically — it fails this guard until it either acquires the lock or is
consciously added to ``_ALLOW_NO_LOCK`` with a reason. Nobody has to remember to
add the module to a list.

Bug-7982 R6 finding 4 (EFFECTIVENESS, not mere presence): ``_lock_is_effective``
does not just prove a lock call exists SOMEWHERE in the function tree (the R3
check, satisfied by a call inside ``if False:``, an unreachable ``except``, or a
nested helper that is never awaited). It requires the lock call to be:
  * reachable — not nested inside a literal ``if False:`` dead branch and not
    inside a nested ``def``/``async def`` other than the endpoint itself;
  * dominating the mutation — textually BEFORE the first ORM write
    (``.add``/``.add_all``/``.delete``/``.merge``/``.commit``/``.flush`` or an
    ``.execute`` of an ``insert``/``update``/``delete``/``pg_insert`` statement);
  * bound to THIS endpoint's own ``model_id`` — its second argument is the
    function's ``model_id`` parameter, not a hardcoded value or unrelated name.
"""
from __future__ import annotations

import pytest

import src.api as _api_pkg
from shared.db.model_lock_coverage import (
    MUTATING_METHODS as _MUTATING_METHODS,
    discover_api_modules as _shared_discover,
    effective_from_source as _effective_from_source,
    lock_is_effective as _lock_is_effective,
    model_scoped_mutating_endpoints as _shared_endpoints,
)

pytestmark = pytest.mark.unit


def _discover_api_modules():
    return _shared_discover(_api_pkg)


def _model_scoped_mutating_endpoints(modules=None):
    return _shared_endpoints(_api_pkg, modules=modules)


# ---------------------------------------------------------------------------
# Endpoints that legitimately do NOT take the model definition/governance lock.
#
# Every entry is ``(CATEGORY, reason)``. The CATEGORY is a value from the closed
# set below, NOT prose — Bug-8708: the first version of the reconciliation check
# keyed on the substring "preserved on revert" in a free-text reason, and three
# of four real wordings escaped it, including the exact pre-lane wording this
# lane deleted ("DataSource is preserved/upserted-in-place on revert"). Restoring
# that sentence would have left the Bug-8441 guard green. A category cannot be
# reworded past a test, and a new entry cannot be added without choosing one.
# ---------------------------------------------------------------------------
READ_ONLY = "READ_ONLY"
TELEMETRY = "TELEMETRY"
OPERATIONAL_ARTIFACT = "OPERATIONAL_ARTIFACT"
EXTERNAL_IO = "EXTERNAL_IO"
PRESERVED_ON_REVERT = "PRESERVED_ON_REVERT"
MODEL_LIFECYCLE = "MODEL_LIFECYCLE"

_ALLOW_CATEGORIES = frozenset({
    READ_ONLY, TELEMETRY, OPERATIONAL_ARTIFACT, EXTERNAL_IO,
    PRESERVED_ON_REVERT, MODEL_LIFECYCLE,
})

_ALLOW_NO_LOCK: dict[str, tuple[str, str]] = {
    # --- read / compute / validate / preview / simulate (no model mutation) ---
    "validate_named_set": (READ_ONLY, "read-only validation"),
    "preview_named_set_by_definition": (READ_ONLY, "read-only preview"),
    "preview_named_set": (READ_ONLY, "read-only preview"),
    "validate_kpi_expression": (READ_ONLY, "read-only validation"),
    "evaluate_kpi": (READ_ONLY, "read-only evaluation"),
    "evaluate_adhoc": (READ_ONLY, "read-only evaluation"),
    "evaluate_batch": (READ_ONLY, "read-only evaluation (writes kpi_latest, an operational "
                       "materialisation guarded by its own epoch/eval_started_at ordering, "
                       "not a snapshot-owned definition table)"),
    "validate_calculated_expression": (READ_ONLY, "read-only expression validation"),
    "validate_detail_columns": (READ_ONLY, "read-only validation"),
    "validate_user_defined_attribute_expression": (READ_ONLY, "read-only expression validation"),
    "analyze_table_endpoint": (READ_ONLY, "read-only heuristic analysis (returns suggestions)"),
    "emit_calendar_script": (READ_ONLY, "read-only script generation"),
    "simulate_as_user": (READ_ONLY, "read-only row-security simulation"),
    "validate_model": (READ_ONLY, "read-only model validation"),
    "query_impact": (READ_ONLY, "read-only impact analysis"),
    "run_impact_scan": (TELEMETRY, "upserts GatewayQueryReference (operational impact-scan "
                        "telemetry), not a snapshot-owned definition/governance table"),
    "dry_run_pocket_sql": (READ_ONLY, "read-only pocket SQL dry run"),
    "validate_pocket_sql": (READ_ONLY, "read-only pocket SQL validation"),
    "validate_named_query_sql": (READ_ONLY, "read-only Named Query SQL validation (router /validate round trip + metadata derivation)"),
    "export_model_lookml": (READ_ONLY, "read-only LookML export generation"),
    # --- fire-and-forget usage telemetry (swallows FK race, no definition write) ---
    "report_named_set_usage": (TELEMETRY, "fire-and-forget usage telemetry"),
    "report_kpi_usage": (TELEMETRY, "fire-and-forget usage telemetry"),
    # --- operational / per-user artifacts (NOT part of the model snapshot) ---
    "dismiss_alert_endpoint": (PRESERVED_ON_REVERT,
                               "ModelAlert operational state; the rehydrator only ever "
                               "APPENDS alerts, never deletes or updates one"),
    "revalidate_model_endpoint": (OPERATIONAL_ARTIFACT,
                                  "writes only DERIVED validity flags (Dimension/"
                                  "Measure.is_invalid/invalid_reason) that a revert "
                                  "re-derives from the snapshot; a lost revalidate is "
                                  "self-healing, never a wrong number or a lost edit"),
    "record_recently_used": (OPERATIONAL_ARTIFACT, "per-user recently-used telemetry, not snapshot-owned"),
    "toggle_favourite": (OPERATIONAL_ARTIFACT, "per-user favourite flag, not snapshot-owned"),
    "create_saved_query": (OPERATIONAL_ARTIFACT, "per-user saved query, not snapshot-owned"),
    "update_saved_query": (OPERATIONAL_ARTIFACT, "per-user saved query, not snapshot-owned"),
    "delete_saved_query": (OPERATIONAL_ARTIFACT, "per-user saved query, not snapshot-owned"),
    "create_pivot_view": (OPERATIONAL_ARTIFACT, "per-user pivot view, not snapshot-owned"),
    "update_pivot_view": (OPERATIONAL_ARTIFACT, "per-user pivot view, not snapshot-owned"),
    "delete_pivot_view": (OPERATIONAL_ARTIFACT, "per-user pivot view, not snapshot-owned"),
    "create_scratchpad_measure": (OPERATIONAL_ARTIFACT, "per-user scratchpad measure, not snapshot-owned"),
    "update_scratchpad_measure": (OPERATIONAL_ARTIFACT, "per-user scratchpad measure, not snapshot-owned"),
    "delete_scratchpad_measure": (OPERATIONAL_ARTIFACT, "per-user scratchpad measure, not snapshot-owned"),
    "acknowledge_schema_change": (OPERATIONAL_ARTIFACT, "operational schema-drift acknowledgement, not snapshot-owned"),
    "create_downstream_asset": (OPERATIONAL_ARTIFACT, "DownstreamAsset lineage annotation, not in the model snapshot"),
    "update_downstream_asset": (OPERATIONAL_ARTIFACT, "DownstreamAsset lineage annotation, not in the model snapshot"),
    "delete_downstream_asset": (OPERATIONAL_ARTIFACT, "DownstreamAsset lineage annotation, not in the model snapshot"),
    # --- data-quality VIOLATIONS (operational), not the RULES (which are locked) ---
    "run_validation": (OPERATIONAL_ARTIFACT, "writes DataQualityViolation (operational); the "
                       "snapshot-owned DataQualityRule CRUD is locked"),
    # Bug-8740 REMOVED: clear_violations also resets
    # DataQualityRule.last_violation_count, and data_quality_rules IS
    # snapshot-owned. It is a lock holder now, not an exemption.
    # --- glossary: operational share tokens (entries CRUD + bootstrap are locked) ---
    "issue_share_token": (OPERATIONAL_ARTIFACT, "GlossaryShareToken operational credential, not snapshot-owned"),
    "regenerate_share_token": (OPERATIONAL_ARTIFACT, "GlossaryShareToken operational credential, not snapshot-owned"),
    "revoke_share_tokens": (OPERATIONAL_ARTIFACT, "GlossaryShareToken operational credential, not snapshot-owned"),
    # --- external-integration config + sync (external I/O; MUST NOT hold the lock; ---
    # --- integration config tables are not part of the model definition snapshot) ---
    "create_collibra_config": (EXTERNAL_IO, "integration config (external metadata catalogue), not snapshot-owned"),
    "update_collibra_config": (EXTERNAL_IO, "integration config, not snapshot-owned"),
    "delete_collibra_config": (EXTERNAL_IO, "integration config, not snapshot-owned"),
    "validate_collibra_connection": (EXTERNAL_IO, "external connection test (I/O); must not hold the lock"),
    "collibra_sync": (EXTERNAL_IO, "external metadata sync (long I/O); must not hold the lock"),
    "collibra_export_preview": (READ_ONLY, "read-only export preview"),
    "create_solidatus_config": (EXTERNAL_IO, "integration config, not snapshot-owned"),
    "update_solidatus_config": (EXTERNAL_IO, "integration config, not snapshot-owned"),
    "delete_solidatus_config": (EXTERNAL_IO, "integration config, not snapshot-owned"),
    "validate_solidatus_connection": (EXTERNAL_IO, "external connection test (I/O); must not hold the lock"),
    "solidatus_sync": (EXTERNAL_IO, "external metadata sync (long I/O); must not hold the lock"),
    "solidatus_export_preview": (READ_ONLY, "read-only export preview"),
    # --- PRESERVE-GATED on revert: the rehydrator does not delete-reinsert these ---
    # Bug-8441: every PRESERVED_ON_REVERT entry names the snapshot-owned table
    # that justifies it in _PRESERVE_GATED_JUSTIFICATION below, and that table
    # must appear in the runtime guard's ``gated_families()``. The two layers
    # previously carried independent hand-written claims about the same property
    # and contradicted each other on five endpoints.
    "create_aggregate": (PRESERVED_ON_REVERT, "aggregate lifecycle (Bug-8431 audit)"),
    "update_aggregate": (PRESERVED_ON_REVERT, "aggregate lifecycle (Bug-8431 audit)"),
    "delete_aggregate": (PRESERVED_ON_REVERT, "aggregate lifecycle (Bug-8431 audit)"),
    "create_pocket": (PRESERVED_ON_REVERT, "pocket lifecycle (Bug-8431 audit)"),
    "update_pocket": (PRESERVED_ON_REVERT, "pocket lifecycle (Bug-8431 audit)"),
    "delete_pocket": (PRESERVED_ON_REVERT, "pocket lifecycle (Bug-8431 audit)"),
    "refresh_pocket": (PRESERVED_ON_REVERT, "pocket refresh (operational run)"),
    "upsert_pocket_refresh_policy": (PRESERVED_ON_REVERT, "pocket refresh policy"),
    "upsert_refresh_policy": (PRESERVED_ON_REVERT, "aggregate refresh policy"),
    # Bug-8441 REMOVED from this list: create_source, update_source,
    # create_target, update_target, delete_target. They are NOT preserved in
    # place — the revert upserts every column of the surviving rows and
    # HARD-DELETES rows absent from the snapshot
    # (rehydrator._reconcile_sources_and_targets, Bug-7147), and create_target /
    # delete_target additionally write ``models.target_id``. All five now acquire
    # the per-model definition lock.
    # Bug-8437 REMOVED from this list: update_model. A revert UPDATEs the model
    # row's scalars from the snapshot, so an unlocked concurrent edit is a silent
    # lost update. ``models`` stays out of the RUNTIME guard's table set (it is
    # not delete-reinserted, and guarding it re-introduces the bump_data_epoch
    # flood), so exclusion — not detection — is what closes that race.
    # The lock IS taken — this entry exists only because the static checker
    # cannot SEE it. ``delete_model`` acquires it indirectly, in the shared
    # primitive ``shared/model_snapshot/cascade_delete.py::delete_model_cascade``,
    # which is where it belongs: the same primitive is reached by the project
    # DELETE endpoint and by project import in ``replace`` mode, and locking at
    # one call site would have left the other two unlocked. ``_lock_is_effective``
    # requires the call textually inside the endpoint, bound to the endpoint's own
    # ``model_id``, so a lock one frame down is invisible to it by construction.
    #
    # The reason this entry USED to give — "the per-model lock is moot once the
    # row is gone" — was wrong, and it is what let the gap survive a
    # producer-derived guard. The lock is not about the state after the delete;
    # it is about serialising the delete against a holder that is mid-write on
    # the SAME model (Save, revert, or migration 0194), and about agreeing with
    # 0194's lock-then-write order instead of inverting it into an ABBA deadlock.
    #
    # What actually guards it now, since this file cannot:
    #   optimizer/tests/test_cascade_delete_physical_tables.py
    #     ::test_model_cascade_acquires_the_model_lock_before_any_other_statement
    #   model-service/tests/integration/test_cascade_delete_model_lock_db.py
    #     (real Postgres: serialisation, no 40P01, strict runtime write guard)
    "delete_model": (MODEL_LIFECYCLE,
                     "whole-model delete; the per-model lock is acquired one frame "
                     "down, in the shared delete_model_cascade primitive, where all "
                     "three of its callers inherit it — see the note above"),
}

# ---------------------------------------------------------------------------
# Bug-8441: the ONE claim both layers make, stated once.
#
# An allow-list entry categorised PRESERVED_ON_REVERT is only sound if the
# RUNTIME write-lock guard agrees the entity is excluded from the snapshot-owned
# set. Before this, the lint and the guard each carried their own hand-written
# enumeration of that property and disagreed on five source/target endpoints —
# the lint said preserved-so-unlocked-is-fine while the guard's derived set
# reported every one of their writes as a violation.
#
# Each entry maps the endpoint to the snapshot table whose exclusion justifies
# it. The tests below assert every such table is in ``gated_families()``, and
# that the map covers exactly the PRESERVED_ON_REVERT category — both directions.
# ---------------------------------------------------------------------------
_PRESERVE_GATED_JUSTIFICATION: dict[str, str] = {
    "create_aggregate": "aggregate_definitions",
    "update_aggregate": "aggregate_definitions",
    "delete_aggregate": "aggregate_definitions",
    "create_pocket": "pocket_definitions",
    "update_pocket": "pocket_definitions",
    "delete_pocket": "pocket_definitions",
    "refresh_pocket": "pocket_refresh_runs",
    "upsert_pocket_refresh_policy": "pocket_refresh_policies",
    "upsert_refresh_policy": "aggregate_refresh_policies",
    "dismiss_alert_endpoint": "model_alerts",
}
# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_all_api_modules_import():
    """Reviewer #4: a src.api module that fails to import would silently drop its
    endpoints from coverage — fail loud instead of swallowing the ImportError."""
    _mods, failures = _discover_api_modules()
    assert not failures, f"src.api modules failed to import: {failures}"


def test_every_model_scoped_mutating_endpoint_effectively_locks():
    modules, import_failures = _discover_api_modules()
    endpoints = _model_scoped_mutating_endpoints(modules=modules)
    assert endpoints, "route discovery found no model-scoped mutating endpoints — discovery is broken"
    failures: list[str] = []
    for module, name, fn in sorted(endpoints, key=lambda t: (t[0], t[1])):
        if name in _ALLOW_NO_LOCK:
            continue
        ok, reason = _lock_is_effective(fn)
        if not ok:
            # R7 review round 2: report the SOURCE-EXTRACTION state, not just the
            # verdict. The most plausible cause of this check's one unreproduced
            # failure is extraction skew — `inspect.getsource` reads src/api/*.py
            # from disk (via linecache) at test time, and on this shared tree
            # another agent may be rewriting those same files. If the extracted
            # first line is not this endpoint's own `def`, the failure is skew,
            # not a real coverage gap, and the assertion text says so outright.
            code = getattr(fn, "__code__", None)
            try:
                import inspect as _inspect
                first_line = _inspect.getsource(fn).lstrip().splitlines()[0]
            except Exception as exc:  # noqa: BLE001
                first_line = f"<unreadable: {exc!r}>"
            failures.append(
                f"{module}:{name}: {reason} "
                f"[src={getattr(code, 'co_filename', '?')}:"
                f"{getattr(code, 'co_firstlineno', '?')} first_line={first_line!r}]"
            )
    # Bug-7982 R7: this check reads the live module registry, so its result can in
    # principle depend on what an earlier test in the same process imported or
    # left patched. One unreproduced failure was observed in a full-suite run and
    # did not recur in four subsequent runs (three of them randomly ordered), so
    # the DISCOVERY STATE is reported alongside the failure list — a recurrence
    # then says immediately whether discovery itself differed, instead of needing
    # another 3-minute run to find out. See the filed intake.
    assert not failures, (
        "these model-scoped mutating endpoints do not EFFECTIVELY acquire the "
        "per-model definition/governance lock and are not allow-listed "
        "(Bug-7982):\n  " + "\n  ".join(failures) + "\n\nAdd "
        "`await acquire_model_definition_lock(db, model_id)` AFTER the ownership "
        "check and BEFORE the first write, or add the endpoint to _ALLOW_NO_LOCK "
        "with a reason if it is a read/compute/telemetry/operational write."
        f"\n\nDISCOVERY STATE at failure: {len(modules)} src.api modules, "
        f"{len(endpoints)} model-scoped mutating endpoints, "
        f"import failures={import_failures or 'none'}."
    )


def test_preserve_gated_allow_list_agrees_with_the_runtime_guard():
    """Bug-8441: the lint and the runtime write guard must not contradict.

    Every allow-list entry justified by "preserved in place on revert" names the
    snapshot table that justifies it, and that table must be excluded by
    ``snapshot_owned_tables.gated_families()``. If the guard considers the table
    revert-owned, the endpoint is racing a revert that deletes or overwrites its
    rows and the allow-list entry is a hole, not an exemption.

    Mutation proof: drop ``"model_alerts"`` from ``APPEND_ONLY_TABLES`` (or
    re-add ``"data_sources"`` to this map) and this fails.
    """
    from shared.model_snapshot.snapshot_owned_tables import (
        gated_families,
        snapshot_owned_tables,
    )

    gated = gated_families()
    guarded = snapshot_owned_tables()
    contradictions = []
    for endpoint, table in sorted(_PRESERVE_GATED_JUSTIFICATION.items()):
        if table not in gated:
            contradictions.append(
                f"{endpoint}: allow-listed as preserved-on-revert, but "
                f"{table!r} is NOT in gated_families()"
            )
        if table in guarded:
            contradictions.append(
                f"{endpoint}: allow-listed as preserved-on-revert, but the "
                f"runtime guard treats {table!r} as snapshot-owned"
            )
    assert not contradictions, (
        "the static lock lint and the runtime write-lock guard disagree about "
        "which entities a revert preserves (Bug-8441):\n  "
        + "\n  ".join(contradictions)
    )


def test_preserve_gated_justifications_name_real_allow_list_entries():
    """Every justification must belong to an actual allow-list entry.

    Otherwise the map above could keep asserting a property for an endpoint that
    is no longer excused — a claim with nothing behind it.
    """
    orphans = sorted(set(_PRESERVE_GATED_JUSTIFICATION) - set(_ALLOW_NO_LOCK))
    assert not orphans, (
        f"_PRESERVE_GATED_JUSTIFICATION names endpoints that are not in "
        f"_ALLOW_NO_LOCK: {orphans}"
    )


def test_every_preserved_on_revert_allow_list_entry_is_reconciled():
    """Bug-8704: the reconciliation map must be complete in BOTH directions.

    The sibling test above checks map-keys are a subset of the allow-list. The
    reverse was unchecked, so a NEW allow-list entry justified by "preserved on
    revert" that nobody added to the map is never reconciled against the runtime
    guard — reproducing Bug-8441's exact condition with a green suite. The
    requirement is DERIVED from the entry's own CATEGORY, not from its prose
    and not from a fourth hand-written list.
    """
    unreconciled = sorted(
        name for name, (category, _reason) in _ALLOW_NO_LOCK.items()
        if category == PRESERVED_ON_REVERT
        and name not in _PRESERVE_GATED_JUSTIFICATION
    )
    assert not unreconciled, (
        "these endpoints are allow-listed as PRESERVED_ON_REVERT but name no "
        "snapshot table in _PRESERVE_GATED_JUSTIFICATION, so nothing checks the "
        f"runtime guard agrees (Bug-8441 / Bug-8704): {unreconciled}"
    )
    miscategorised = sorted(
        name for name in _PRESERVE_GATED_JUSTIFICATION
        if _ALLOW_NO_LOCK.get(name, (None, ""))[0] != PRESERVED_ON_REVERT
    )
    assert not miscategorised, (
        "these endpoints claim a preserve-gated justification but are not "
        f"categorised PRESERVED_ON_REVERT: {miscategorised}"
    )


# ---------------------------------------------------------------------------
# Bug-8728: the endpoints whose write target the AST extraction can SEE but not
# RESOLVE — ``db.delete(<instance>)`` and the ORM dirty-UPDATE
# (``obj = await db.get(X, id); obj.field = v; await db.commit()``), neither of
# which names a mapped class at the write site.
#
# The reconciliation cannot tell those apart from "writes nothing", so each one
# is confirmed BY HAND here with the entity it actually writes, and a NEW one
# fails ``test_the_allow_list_reconciliation_is_not_vacuous`` until someone does
# the same. That converts a silent blind spot into a reviewed list. The runtime
# write-lock guard remains the backstop for all of them: it sees the write at
# the cursor whatever the source looks like.
# ---------------------------------------------------------------------------
_UNEXTRACTABLE_WRITES: dict[str, str] = {
    # Each verified by reading the handler: the ``db.get(<class>, id)`` two lines
    # above the ``db.delete(<instance>)`` names the entity, and none of them is
    # in ``snapshot_owned_tables()``.
    "delete_aggregate": "AggregateDefinition — preserve-gated on revert, so "
                        "excluded from the guarded set by design",
    "delete_pocket": "PocketDefinition — preserve-gated on revert",
    "delete_collibra_config": "CollibraConnection — integration config, not in "
                              "the model snapshot",
    "delete_solidatus_config": "SolidatusConnection — integration config, not in "
                               "the model snapshot",
    # ``delete_downstream_asset`` and ``update_downstream_asset`` were BOTH here
    # until the read-path project-scope lane (F-01/F-02). They are gone now, and
    # their removal is the point rather than an accident:
    #   * the delete handler wrote through ``db.delete(asset)``, whose target the
    #     extraction could only see as an unresolved instance;
    #   * the update handler wrote its associations through
    #     ``asset.columns = [...]``, a relationship assignment with no statement
    #     to read at all.
    # Both now issue explicit ``delete(downstream_asset_columns)`` /
    # ``insert(downstream_asset_columns)`` / ``delete(DownstreamAsset)``
    # statements — because assigning to the relationship makes SQLAlchemy load
    # the existing collection, which reads every associated ``ModelColumn``
    # including a legacy one belonging to another project. The scan can name
    # those tables, so the entries became STALE and this file's reconciliation
    # test said so. Neither table is in ``snapshot_owned_tables()``, which is why
    # both endpoints stay in ``_ALLOW_NO_LOCK``.
    "delete_pivot_view": "SavedPivotView — per-user artifact",
    "delete_saved_query": "SavedQuery — per-user artifact",
    "delete_scratchpad_measure": "ScratchpadMeasure — per-user artifact",
    # Not a write at all. ``ensure_refs_in_model(db, ModelTable, ...)`` is the
    # canonical body-FK ownership guard from ``src/api/_scope.py``: it issues a
    # scoped SELECT and either returns the rows or raises 422. The extraction
    # sees a mapped class handed to a callee it cannot classify, so it reports
    # the call as unresolved; verified by reading ``_scope.ensure_refs_in_model``
    # that it performs no INSERT/UPDATE/DELETE and adds nothing to the session.
    "validate_detail_columns": "ModelTable — read-only scope guard "
                               "(_scope.ensure_refs_in_model issues a SELECT "
                               "only); the endpoint itself is advisory and "
                               "persists nothing",
}


def test_the_allow_list_reconciliation_is_not_vacuous():
    """Bug-8728: the reconciliation passes trivially for any endpoint whose write
    the extractor cannot represent, and nothing measured how often that is.

    Three floors, so the blind spot is an enumerable reviewed list rather than
    silence: every allow-list entry must actually be EXAMINED; a useful number of
    them must yield an extractable write at all; and any endpoint whose write
    target the extraction saw but could not RESOLVE must be named in
    ``_UNEXTRACTABLE_WRITES`` with a reason. A new one fails this test.
    """
    import ast
    import inspect
    import sys
    import textwrap

    from shared.model_snapshot.snapshot_owned_tables import analyse_source

    modules, _failures = _discover_api_modules()
    endpoints = _model_scoped_mutating_endpoints(modules=modules)
    examined = 0
    with_writes = 0
    discarded: dict[str, str] = {}
    for module, name, fn in sorted(endpoints, key=lambda t: (t[0], t[1])):
        if name not in _ALLOW_NO_LOCK:
            continue
        mod = sys.modules.get(getattr(fn, "__module__", ""))
        assert mod is not None, f"{module}:{name} is not in sys.modules"
        mod_src = inspect.getsource(mod)
        mod_tree = ast.parse(mod_src)
        imports = "\n".join(
            ast.get_source_segment(mod_src, n) or ""
            for n in mod_tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
        )
        writes, unresolved = analyse_source(
            imports + "\n" + textwrap.dedent(inspect.getsource(fn))
        )
        examined += 1
        if writes:
            with_writes += 1
        # the endpoint's own ``@router.delete(...)`` decorator is not a DB write
        real = [u for u in unresolved if not u.startswith("router.")]
        if not writes and real:
            discarded[name] = real[0]

    assert examined == len(_ALLOW_NO_LOCK), (
        f"the reconciliation examined {examined} of {len(_ALLOW_NO_LOCK)} "
        "allow-list entries; the rest were skipped with no signal"
    )
    assert with_writes >= 15, (
        f"only {with_writes} allow-listed endpoints yielded ANY extractable "
        "write — the reconciliation has lost its ability to see writes at all"
    )
    unreviewed = sorted(set(discarded) - set(_UNEXTRACTABLE_WRITES))
    assert not unreviewed, (
        "these allow-listed endpoints write through a target the extraction "
        "could not resolve, so the reconciliation cannot tell a guarded write "
        "from no write at all. Confirm by hand that each writes nothing "
        "snapshot-owned, then add it to _UNEXTRACTABLE_WRITES with the "
        "reason:\n  " + "\n  ".join(f"{n}: {discarded[n]}" for n in unreviewed)
    )
    stale = sorted(set(_UNEXTRACTABLE_WRITES) - set(discarded))
    assert not stale, (
        f"_UNEXTRACTABLE_WRITES names endpoints the extraction now resolves "
        f"(or that no longer exist): {stale}"
    )


def test_no_allow_listed_endpoint_dirty_updates_a_guarded_entity():
    """Bug-8740 / Bug-8743: the ORM dirty-UPDATE is invisible to the write scan.

    ``rule = await db.get(DataQualityRule, id); rule.x = None; await db.commit()``
    emits an UPDATE, but there is no write CALL node anywhere — so
    ``test_no_allow_listed_endpoint_writes_a_guarded_table`` sees nothing and
    ``_UNEXTRACTABLE_WRITES`` only catches the endpoint if it happens to ALSO do
    something unresolvable. ``clear_violations`` was exactly that: allow-listed
    as "violations only", signed off on an unrelated ``db.delete(v)``, and
    quietly issuing an unlocked UPDATE on the guarded ``data_quality_rules``.

    This binds ``<name> = await db.get(<Class>, ...)`` inside each allow-listed
    handler and treats a later ``<name>.<attr> = ...`` as a write of that class.
    """
    import ast
    import inspect
    import sys
    import textwrap

    from shared.db import models as orm
    from shared.model_snapshot.snapshot_owned_tables import snapshot_owned_tables

    guarded = snapshot_owned_tables()
    offenders: list[str] = []
    for module, name, fn in sorted(_model_scoped_mutating_endpoints(),
                                   key=lambda t: (t[0], t[1])):
        if name not in _ALLOW_NO_LOCK:
            continue
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        except (OSError, TypeError, SyntaxError):  # pragma: no cover
            continue
        bound: dict[str, str] = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                continue
            value = node.value
            if isinstance(value, ast.Await):
                value = value.value
            if not (isinstance(value, ast.Call)
                    and getattr(value.func, "attr", None) == "get"
                    and value.args):
                continue
            cls = getattr(value.args[0], "id", None)
            if not isinstance(cls, str):
                continue
            table = getattr(getattr(orm, cls, None), "__tablename__", None)
            if isinstance(table, str):
                bound[node.targets[0].id] = table
        if not bound:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for tgt in targets:
                if not (isinstance(tgt, ast.Attribute)
                        and isinstance(tgt.value, ast.Name)):
                    continue
                table = bound.get(tgt.value.id)
                if table in guarded:
                    offenders.append(
                        f"{module}:{name} dirty-UPDATEs snapshot-owned "
                        f"{table!r} via {tgt.value.id}.{tgt.attr}"
                    )
    assert not offenders, (
        "these endpoints are allow-listed as NOT needing the per-model "
        "definition lock, but mutate a loaded ORM instance of a snapshot-owned "
        "entity — an UPDATE with no write call for any scan to see "
        "(Bug-8740):" + "\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_no_allow_listed_endpoint_writes_a_guarded_table():
    """Bug-8725: the reconciliation must cover EVERY category, not just one.

    ``test_preserve_gated_allow_list_agrees_with_the_runtime_guard`` binds the
    PRESERVED_ON_REVERT entries to ``gated_families()``. Nothing bound the other
    five categories to anything — so filing a genuinely revert-owned writer under
    OPERATIONAL_ARTIFACT or EXTERNAL_IO reproduced Bug-8441 with a green suite,
    just one level up from the prose escape Bug-8708 closed.

    This asks the question directly instead of trusting a category: for each
    allow-listed endpoint, what tables does its body actually write, and is any
    of them in the runtime guard's snapshot-owned set? The write extraction is
    the SAME derivation the guard itself uses (``analyse_source``), fed the
    endpoint's own source with its module's imports prepended so the SQLAlchemy
    constructor bindings resolve.

    Honest limitation, stated rather than implied: this sees writes issued in the
    handler's own body, not writes reached through a helper in another module.
    That residual is the runtime guard's job — and it sees it, at the cursor.
    """
    import ast
    import inspect
    import linecache
    import sys
    import textwrap

    from shared.model_snapshot.snapshot_owned_tables import (
        analyse_source, gated_families, snapshot_owned_tables,
    )

    guarded = snapshot_owned_tables()
    gated = gated_families()
    offenders: list[str] = []
    # Bug-8428 — FAIL CLOSED on a source-extraction failure, and remove the
    # skew's CAUSE rather than retrying past its symptom.
    #
    # Every ``continue`` below used to swallow one: an allow-listed endpoint
    # whose source could not be read, parsed or analysed was silently treated as
    # having no writes, i.e. as compliant. The exemption then landed on files
    # chosen by an I/O accident — precisely the population nobody has checked.
    #
    # The reported flakiness is source-extraction SKEW: ``inspect.getsource``
    # reads through ``linecache``, which caches file contents, so in a shared
    # working tree a module imported before a concurrent edit is matched against
    # post-edit lines and yields torn source. The upstream proposal was a retry
    # decorator around the whole test; that masks the symptom and leaves a stale
    # cache to be re-read. Dropping the cache entry for the module's own file
    # before reading removes the staleness itself, deterministically and once.
    unreadable: list[str] = []
    for module, name, fn in sorted(_model_scoped_mutating_endpoints(),
                                   key=lambda t: (t[0], t[1])):
        entry = _ALLOW_NO_LOCK.get(name)
        if entry is None:
            continue
        category, _reason = entry
        mod = sys.modules.get(getattr(fn, "__module__", ""))
        if mod is None:
            unreadable.append(f"{module}:{name} — module not in sys.modules")
            continue
        linecache.checkcache(getattr(mod, "__file__", None) or "")
        try:
            mod_src = inspect.getsource(mod)
            mod_tree = ast.parse(mod_src)
            body = textwrap.dedent(inspect.getsource(fn))
        except (OSError, TypeError, SyntaxError) as exc:
            unreadable.append(f"{module}:{name} — source unavailable ({exc})")
            continue
        imports = "\n".join(
            ast.get_source_segment(mod_src, n) or ""
            for n in mod_tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
        )
        try:
            writes, _unresolved = analyse_source(imports + "\n" + body)
        except SyntaxError as exc:
            unreadable.append(f"{module}:{name} — not analysable ({exc})")
            continue
        for table, kind, _gated in writes:
            if table not in guarded:
                continue
            if category == PRESERVED_ON_REVERT and table in gated:
                continue
            offenders.append(
                f"{module}:{name} [{category}] writes snapshot-owned "
                f"{table!r} ({kind})"
            )
    assert not unreadable, (
        "Bug-8428: these allow-listed endpoints could not be read back from "
        "source, so this test proves NOTHING about them. An unreadable endpoint "
        "is unchecked, not clean:\n  " + "\n  ".join(sorted(set(unreadable)))
    )
    assert not offenders, (
        "these endpoints are allow-listed as NOT needing the per-model "
        "definition lock, but their own bodies write a table the runtime guard "
        "treats as snapshot-owned — a revert can discard those writes "
        "(Bug-8441 / Bug-8725):\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_every_allow_list_entry_carries_a_known_category():
    """Bug-8708: the exemption CATEGORY is a closed set, not free text.

    The previous reconciliation check keyed on the substring "preserved on
    revert" appearing in a free-text reason. Three of four real wordings escaped
    it — including the exact pre-lane wording this lane deleted, so restoring
    that one sentence would have left the Bug-8441 guard green. A category cannot
    be reworded past a test, and this makes an UNCATEGORISED new entry impossible
    rather than merely discouraged.
    """
    bad = sorted(
        f"{name}: {value!r}" for name, value in _ALLOW_NO_LOCK.items()
        if not (isinstance(value, tuple) and len(value) == 2
                and value[0] in _ALLOW_CATEGORIES and isinstance(value[1], str)
                and value[1].strip())
    )
    assert not bad, (
        "every _ALLOW_NO_LOCK entry must be (CATEGORY, reason) with CATEGORY in "
        f"{sorted(_ALLOW_CATEGORIES)}: {bad}"
    )


def test_source_target_and_model_scalar_writers_are_locked_not_allow_listed():
    """Bug-8437 / Bug-8441 regression guard, stated as a named list.

    These six endpoints were allow-listed on the belief that their entities are
    "preserved/upserted in place on revert". They are not:
      * the revert HARD-DELETES data_sources / data_targets rows absent from the
        snapshot and upserts every column of the survivors;
      * create_target / delete_target write ``models.target_id``;
      * update_model writes the model scalars the revert restores.
    Each must acquire the per-model definition lock, effectively.

    Mutation proof: remove the ``acquire_model_definition_lock`` call from any
    one of them (or re-add its name to ``_ALLOW_NO_LOCK``) and this fails.
    """
    must_lock = {
        "create_source", "update_source",
        "create_target", "update_target", "delete_target",
        "update_model",
    }
    still_allow_listed = sorted(must_lock & set(_ALLOW_NO_LOCK))
    assert not still_allow_listed, (
        "these endpoints write revert-owned state and must not be allow-listed "
        f"(Bug-8437 / Bug-8441): {still_allow_listed}"
    )
    endpoints = _model_scoped_mutating_endpoints()
    found = {name for _mod, name, _fn in endpoints}
    missing = sorted(must_lock - found)
    assert not missing, f"route discovery did not surface: {missing}"
    failures = []
    for _mod, name, fn in endpoints:
        if name not in must_lock:
            continue
        ok, reason = _lock_is_effective(fn)
        if not ok:
            failures.append(f"{_mod}:{name}: {reason}")
    assert not failures, (
        "these revert-owned-state writers do not EFFECTIVELY acquire the "
        "per-model definition lock:\n  " + "\n  ".join(failures)
    )


def test_allow_list_has_no_stale_entries():
    """Every allow-list entry must name a REAL model-scoped mutating endpoint, so
    an exclusion cannot silently rot into covering a renamed/added writer."""
    real = {name for _mod, name, _fn in _model_scoped_mutating_endpoints()}
    stale = sorted(set(_ALLOW_NO_LOCK) - real)
    assert not stale, f"stale _ALLOW_NO_LOCK entries (no such endpoint): {stale}"


def test_colliding_endpoint_names_are_all_checked():
    """Reviewer #3: endpoints are enumerated as a LIST, so two modules sharing a
    function name (e.g. data_quality.create_rule vs row_security.create_rule) are
    BOTH checked — a name-keyed dict would silently drop one."""
    from collections import Counter

    endpoints = _model_scoped_mutating_endpoints()
    counts = Counter(name for _mod, name, _fn in endpoints)
    collisions = {n for n, c in counts.items() if c > 1}
    for _mod, name, fn in endpoints:
        if name in collisions and name not in _ALLOW_NO_LOCK:
            ok, reason = _lock_is_effective(fn)
            assert ok, f"{_mod}:{name} (colliding name) not effectively locked: {reason}"


# --- finding 4: the effectiveness check must REJECT ineffective locks ---

def test_effectiveness_rejects_lock_in_dead_branch():
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            if False:
                await acquire_model_definition_lock(db, model_id)
            db.add(object())
            await db.commit()
        """
    )
    assert not ok and "no reachable" in reason


def test_effectiveness_rejects_lock_after_write():
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            db.add(object())
            await acquire_model_definition_lock(db, model_id)
            await db.commit()
        """
    )
    assert not ok and "AFTER the first ORM write" in reason


def test_effectiveness_rejects_lock_on_wrong_argument():
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, other_id, db):
            await acquire_model_definition_lock(db, other_id)
            db.add(object())
            await db.commit()
        """
    )
    assert not ok and "own model_id parameter" in reason


def test_effectiveness_rejects_lock_in_nested_uncalled_helper():
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            async def _helper():
                await acquire_model_definition_lock(db, model_id)
            db.add(object())
            await db.commit()
        """
    )
    assert not ok and "no reachable" in reason


def test_effectiveness_accepts_lock_before_write_with_own_model_id():
    """A genuine READ before the lock does not count as a write.

    R7: the read must be a PROVABLE read (``select(...)``). Under the conservative
    write detection introduced in R7, an unresolvable call handed to ``.execute``
    counts as a write — see
    ``test_effectiveness_treats_an_unprovable_execute_argument_as_a_write``.
    """
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            result = await db.execute(select(Measure).where(Measure.model_id == model_id))
            await acquire_model_definition_lock(db, model_id)
            db.add(object())
            await db.commit()
        """
    )
    assert ok, reason


# --- R7 finding 3: the shapes the R6 checker false-PASSed ---
#
# Each of these was reported (and independently reproduced) by the third
# consecutive external cross-family gate as returning ``(True, "ok")`` from the
# R6 checker. They are the mutation guards for the conservative write detection.


def test_effectiveness_rejects_lock_after_name_bound_write_statement():
    """``stmt = update(...).values(...)`` then ``await db.execute(stmt)``.

    R6 inspected only the expression handed directly to ``.execute``; a Name was
    not a recognised write shape, so a write BEFORE the lock read as no write at
    all and the handler passed."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            stmt = update(Measure).where(Measure.model_id == model_id).values(name="x")
            await db.execute(stmt)
            await acquire_model_definition_lock(db, model_id)
            await db.commit()
        """
    )
    assert not ok and "AFTER the first ORM write" in reason, reason


def test_effectiveness_rejects_lock_after_chained_builder_write():
    """``await db.execute(delete(Thing).where(...))``.

    R6 only looked at the OUTERMOST call, which is ``.where`` — never ``delete``
    — so every chained builder (the ordinary way statements are written) read as
    not-a-write."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            await db.execute(delete(Measure).where(Measure.model_id == model_id))
            await acquire_model_definition_lock(db, model_id)
            await db.commit()
        """
    )
    assert not ok and "AFTER the first ORM write" in reason, reason


def test_effectiveness_rejects_write_on_an_unrecognised_session_name():
    """A session held in a variable R6 did not have on its hardcoded name list
    (``db``/``tenant_db``/``snap_db``/``session``/``sess``) was invisible."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, conn_for_writes):
            conn_for_writes.add(object())
            await acquire_model_definition_lock(conn_for_writes, model_id)
            await conn_for_writes.commit()
        """
    )
    assert not ok and "AFTER the first ORM write" in reason, reason


def test_effectiveness_treats_an_unprovable_execute_argument_as_a_write():
    """The conservative inversion itself: an argument that cannot be PROVEN to be
    a read counts as a write, so an unfamiliar shape fails loudly instead of
    passing silently."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            await db.execute(build_some_statement(model_id))
            await acquire_model_definition_lock(db, model_id)
            await db.commit()
        """
    )
    assert not ok and "AFTER the first ORM write" in reason, reason


def test_module_level_plain_collection_receiver_is_not_a_write():
    """A resolved module-level ``set``/``dict`` receiver (e.g. a background-task
    registry) is excused — by RESOLVING its live value, not by name matching."""
    from shared.db.model_lock_coverage import effective_from_funcdef as _eff
    import ast as _ast
    import textwrap as _tw

    src = """
    async def endpoint(project_id, model_id, db):
        await acquire_model_definition_lock(db, model_id)
        _TASKS.add(task)
        db.add(object())
        await db.commit()
    """
    funcdef = _ast.parse(_tw.dedent(src)).body[0]
    ok, reason = _eff(funcdef, {"_TASKS": set()})
    assert ok, reason
    # Without the resolution evidence the receiver is CONSERVATIVELY treated as a
    # session, so the checker reports a (false, but loud) same-session failure —
    # the intended failure direction. It never silently passes.
    ok_no_globals, reason_no_globals = _eff(funcdef, None)
    assert not ok_no_globals and "different session" in reason_no_globals, (
        reason_no_globals
    )


def test_new_unlocked_model_scoped_route_is_flagged_by_discovery():
    """Finding 3b + reviewer #11: a NEW model-scoped mutating endpoint with no
    lock must be surfaced by the DISCOVERY mechanism (not just by calling the
    effectiveness check directly). Inject a synthetic module with such a route and
    assert ``_model_scoped_mutating_endpoints`` returns it AND it fails
    effectiveness — proving derive-from-routes catches an unlisted writer."""
    import types as _types

    from fastapi import APIRouter as _R

    r = _R(prefix="/projects/{project_id}/models/{model_id}/synthetic")

    @r.post("")
    async def brand_new_unlocked_writer(project_id, model_id, db):  # pragma: no cover
        db.add(object())
        await db.commit()

    fake_mod = _types.ModuleType("src.api._synthetic_test_mod")
    fake_mod.router = r

    discovered = _model_scoped_mutating_endpoints(modules=[fake_mod])
    names = {name for _m, name, _f in discovered}
    assert "brand_new_unlocked_writer" in names, "discovery did not surface the new route"
    fn = next(f for _m, name, f in discovered if name == "brand_new_unlocked_writer")
    assert "brand_new_unlocked_writer" not in _ALLOW_NO_LOCK
    ok, reason = _lock_is_effective(fn)
    assert not ok and "no reachable" in reason


def test_effectiveness_rejects_lock_inside_runtime_conditional():
    """Reviewer #5: a lock reachable only inside an ``if <cond>:`` does not
    dominate the unconditional write below it."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            if model_id:
                await acquire_model_definition_lock(db, model_id)
            db.add(object())
            await db.commit()
        """
    )
    assert not ok and "does not dominate" in reason


def test_effectiveness_rejects_lock_inside_try_handler():
    """Reviewer #5: a lock inside a try/except body does not dominate."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            try:
                await acquire_model_definition_lock(db, model_id)
            except Exception:
                pass
            db.add(object())
            await db.commit()
        """
    )
    assert not ok and "does not dominate" in reason


def test_effectiveness_rejects_lock_after_raw_sql_write():
    """Reviewer #5: db.execute(text('DELETE ...')) is a write; a lock after it
    does not dominate."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            await db.execute(text("DELETE FROM measures WHERE model_id = :m"))
            await acquire_model_definition_lock(db, model_id)
            await db.commit()
        """
    )
    assert not ok and "AFTER the first ORM write" in reason


def test_effectiveness_rejects_lock_on_a_different_session():
    """Reviewer round 2 #2: a lock acquired on a DIFFERENT session than the writes
    serialises nothing."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, snap_db, db):
            await acquire_model_definition_lock(snap_db, model_id)
            db.add(object())
            await db.commit()
        """
    )
    assert not ok and "different session" in reason


def test_effectiveness_accepts_lock_inside_session_loop_and_with():
    """The unconditional wrappers every handler uses (async-for session loop,
    with-blocks) do not defeat dominance."""
    ok, reason = _effective_from_source(
        """
        async def endpoint(project_id, model_id, db):
            async for db in get_tenant_db(tenant):
                await ensure_model_in_project(db, project_id, model_id)
                await acquire_model_definition_lock(db, model_id)
                db.add(object())
                await db.commit()
        """
    )
    assert ok, reason


# ---------------------------------------------------------------------------
# Bug-8394: coverage guard extension for data_tags / row_security / personas
#
# The dynamic route-based discovery SHOULD already enumerate these modules, but
# an enumeration blind spot in the discovery mechanism itself (see CLAUDE.md
# "Coverage-tool blind-spot audit") would silently drop an entire module's
# writers from coverage. These tests explicitly assert that each of the three
# governance-writer modules IS discovered and that EVERY one of its model-scoped
# mutating endpoints IS effectively locked — the coverage guard guards itself.
# ---------------------------------------------------------------------------

_GOVERNANCE_WRITER_MODULES = {
    "src.api.data_tags": {
        "create_tag",
        "update_tag",
        "delete_tag",
        "set_persona_tag_restrictions",
    },
    "src.api.personas": {
        "create_persona",
        "update_persona",
        "delete_persona",
    },
    "src.api.row_security": {
        "create_rule",
        "update_rule",
        "delete_rule",
    },
}


def test_governance_writer_modules_are_discovered():
    """Bug-8394: data_tags, row_security, and personas modules must be found by
    the dynamic route discovery. If a module fails to import or its routers are
    not detected, the entire module's writers are silently uncovered."""
    modules, failures = _discover_api_modules()
    assert not failures, f"import failures: {failures}"
    discovered_names = {getattr(m, "__name__", "") for m in modules}
    for mod_name in _GOVERNANCE_WRITER_MODULES:
        assert mod_name in discovered_names, (
            f"coverage guard discovery missed module {mod_name!r} — its "
            f"model-scoped mutating endpoints are silently uncovered"
        )


def test_governance_writer_endpoints_are_enumerated_and_locked():
    """Bug-8394: every CLS/RLS/persona governance writer must be individually
    enumerated by the coverage guard AND pass the effectiveness check.

    This catches two blind-spot classes:
    1. A module is discovered but a ROUTER within it is missed (e.g.
       data_tags.restriction_router vs data_tags.router).
    2. An endpoint is enumerated but its lock call fails the effectiveness
       check (wrong argument, wrong session, after the first write, etc.).
    """
    endpoints = _model_scoped_mutating_endpoints()
    # Build a {module: {endpoint_name}} map from discovered endpoints
    discovered: dict[str, set[str]] = {}
    for mod_name, fn_name, fn in endpoints:
        discovered.setdefault(mod_name, set()).add(fn_name)

    missing_endpoints: list[str] = []
    lock_failures: list[str] = []

    for mod_name, expected_fns in _GOVERNANCE_WRITER_MODULES.items():
        found_fns = discovered.get(mod_name, set())
        for fn_name in expected_fns:
            if fn_name not in found_fns:
                missing_endpoints.append(f"{mod_name}:{fn_name}")
            else:
                # Find the actual function object and check effectiveness
                fn = next(
                    f for m, n, f in endpoints
                    if m == mod_name and n == fn_name
                )
                if fn_name not in _ALLOW_NO_LOCK:
                    ok, reason = _lock_is_effective(fn)
                    if not ok:
                        lock_failures.append(f"{mod_name}:{fn_name}: {reason}")

    assert not missing_endpoints, (
        "these governance-writer endpoints were not discovered by the "
        "coverage guard (Bug-8394 enumeration blind spot):\n  "
        + "\n  ".join(missing_endpoints)
    )
    assert not lock_failures, (
        "these governance-writer endpoints are not EFFECTIVELY locked "
        "(Bug-8394):\n  " + "\n  ".join(lock_failures)
    )
