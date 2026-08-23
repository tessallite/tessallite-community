"""Backfill every undeclared ``joins.join_type`` AND repair the deployed
snapshot to match, in one transaction.

Decision: ``docs/questions/questions_pocket-join-population.md``, "RESOLVED
2026-08-04 — user picks (iii), broadly". Contract:
``docs/architecture/architecture_join-orientation-and-cardinality.md``.

What ``0191`` left open
-----------------------
``0191`` split ``Join.join_type`` (orientation) from ``Join.cardinality``
(fan-out) and backfilled the new column, but deliberately did NOT rewrite
``join_type`` on existing rows. Its reasoning was correct and is quoted here so
this migration is judged against it rather than around it: ``join_type`` is
mirrored verbatim into every deployed model-version snapshot, and
``shared/definition_closure.py`` diffs the live graph against that snapshot
before permitting any aggregate/pocket refresh. Rewriting the live value ALONE
makes the two disagree on ``joins.join_type``, and the closure then refuses
every refresh on that model until a human notices and redeploys — an
unattended availability and cost regression.

The cost of leaving it is that ``routing/pocket_population.py`` refuses to
prove row population for ANY plan containing an undeclared edge, so pockets
cannot accelerate a legacy model at all, forever, with nothing prompting the
modeller.

What this migration does instead
--------------------------------
It performs BOTH halves atomically, which is what removes ``0191``'s
objection:

1. **Live backfill.** Every ``joins`` row whose token does not declare an
   orientation (``is_orientation_declared`` is False — the same predicate
   ``pocket_population`` refuses on, not a restated token list) is rewritten
   using ``shared/semantic/join_orientation_backfill.py``: the ``0191``
   cardinality inference for a cardinality token, ``left`` otherwise (the
   value ``join_keyword`` already coerces an unrecognised token to, so the
   un-flipped rendering is unchanged).

2. **Deployed-snapshot repair.** For each affected model that HAS a deployed
   version, the SAME tokens are patched into that version's stored
   ``snapshot_json`` IN PLACE and ``deploy_epoch`` is bumped, under the model's
   own advisory lock. Live and deployed therefore agree the instant this
   migration commits and the closure never refuses a refresh over it.

Why the deployed snapshot is corrected in place
-----------------------------------------------
This is what decision (iii) actually specified ("rewrites live ``join_type``
AND every affected deployed snapshot in one transaction"), with the cost
("mutates version history") explicitly accepted. An earlier draft of this
migration instead APPENDED a new version row and moved the deploy pointer to
it, on the theory that a stored version is immutable history. Deep review
proved that mechanism worse, not merely different:

``version_number`` is not a neutral surrogate key — it is the MODELLER'S save
sequence, and two consumers read "latest" as "highest number":
``versions.deploy_model`` when ``version_id`` is omitted (which is what both
frontend Deploy buttons send) and ``models._resolve_version_numbers``'
``last_saved``. Injecting a system-authored row therefore (a) makes
deploy-latest publish the migration's row instead of the modeller's saved
draft, and (b) makes ``last_saved == deployed``, which silences the
``UnsavedDeployWarning`` banner that exists precisely to show a modeller their
saved work is not live. A modeller who had saved and not yet deployed would
lose the warning, and every subsequent Deploy would republish the old content
and report success. Fixing that needs a schema discriminator plus changes in
two model-service consumers; correcting the snapshot in place needs neither,
because it never enters the numbering space at all.

The immutability cost is real and is bounded to one field on one row: the
version-diff between this version and its predecessor now attributes the
``join_type: many_to_one -> left`` change to that version. That is disclosed
rather than hidden — the version's ``summary`` gets an explicit
``[migration 0194]`` note and an audit event is written — and the field being
corrected is one whose value was ambiguous by construction. Nothing else is
affected: the export bundle deliberately omits version snapshots (Bug-7623),
nothing hashes or fingerprints a STORED snapshot (``closure_digest`` and
``join_definition_fingerprint`` both hash LIVE rows at build time), and every
cache that reads ``joins`` out of a snapshot — ``snapshot_resolver``,
``join_graph_cache``, ``rewrite/table_resolution``, ``pocket_matcher``, the
result cache — is keyed on ``deploy_epoch`` as well as the version id, so the
bump invalidates them exactly as a redeploy-of-the-same-version does
(Bug-8250). (``params/named_list_resolver`` is keyed on the version id alone,
but reads no join, so it is unaffected; a future in-place snapshot mutation
touching other keys must re-check it.)

Why the patched snapshot is the OLD DEPLOYED one, not a fresh live capture
---------------------------------------------------------------------------
Re-serialising the live graph (what ``POST /deploy`` publishes after a Save)
would also publish every UN-DEPLOYED modeller edit sitting in the live tables —
an unattended, silent publish of work in progress. Only the join tokens this
migration rewrote are changed. Pre-existing live/deployed drift is neither
repaired nor worsened; it stays exactly as visible as it was.

Artifact invalidation
---------------------
Deliberately NOT a second hand-written predicate. Bumping ``deploy_epoch``
makes every artifact of the model incompatible under
``shared/artifact_version_gate.py`` — the SAME module the runtime matchers and
the real deploy path use, and the module whose docstring states that an
epoch-only difference is still an incompatibility (the revert-to-same-version
case). This migration calls ``artifact_incompatible_sql`` directly, so the
staling rule cannot drift from the serving rule. Per the decision, every
aggregate/pocket this touches is EXPECTED to go stale and rebuild; that is the
intended outcome, not a regression to mitigate.

Models with NO deployed version get the live backfill and nothing else: their
artifacts are already refused by the same gate (a NULL deployed pointer makes
everything incompatible), and the scheduler's derived binding check already
treats them as due, so an extra staleness write would add nothing.

Accepted, disclosed risk — with its exact blast radius
------------------------------------------------------
A join whose true orientation was ambiguous before this migration may serve
different numbers once its ``join_type`` is explicit. The decision above
accepts it as a one-time correction: the edge's orientation was already
anchor-dependent (two plans over the same model rendered it preserving
opposite sides), which is the defect being closed. Stated precisely rather
than as a general warning:

* ``many_to_one`` / ``one_to_one`` / ``many_to_many`` / any unrecognised token
  -> ``left``. The UN-FLIPPED rendering is byte-identical, so a conventionally
  drawn star schema traversed from its fact does not move at all. This is why
  the acme-demo seed measured zero served-number change across all 20 of its
  legacy edges. Only a plan whose traversal reaches the edge from the far side
  moves — and that is the case that was previously anchor-dependent.
* ``one_to_many`` -> ``right`` is the ONE token whose un-flipped rendering also
  changes (``LEFT JOIN`` -> ``RIGHT JOIN``), because its many side is the
  modeller's RIGHT table and the legacy rendering was preserving the ONE side.
  ``shared/semantic/tests/test_join_orientation_backfill.py`` pins this as the
  only member of that class, so a later edit to the inference cannot silently
  widen it.
* ``shared/semantic/redundant_partner.py`` folds every legacy token onto
  ``inner`` and emits a hint only for ``inner``, or for ``left``/``right``
  matching the fact side. A backfilled ``many_to_one`` edge whose FACT is the
  modeller's RIGHT table therefore stops producing a redundant-grain hint, so
  the grain picker no longer disables that dimension column and the aggregate/
  dimension/measure routes stop demanding ``confirm_redundant_grain``. The
  guardrail fails OPEN, not wrong; tracked separately rather than silently
  absorbed. **Superseded by Bug-8647:** that "tracked separately" item was
  resolved in the other direction. ``redundant_partner`` no longer keeps a
  private token table and no longer emits a hint for ANY outer join, legacy
  token included, because an outer join leaves the dimension-side key NULL
  where the fact-side key is populated. Every edge this migration backfills to
  ``left``/``right`` therefore stops producing a redundant-grain hint, not
  only the RIGHT-table-fact ones. Still fail-OPEN, and this migration's own
  behaviour is unchanged.

Known, bounded caveats (documented, not silently skipped)
---------------------------------------------------------
* **In-process KPI cache.** ``services/model-service/src/kpi_cache.py`` is an
  in-process 300s TTL cache that a real deploy evicts explicitly. A migration
  cannot reach it. When ``alembic upgrade`` is run against a LIVE stack (the
  ``/admin`` migrate endpoint), a KPI on a corrected model can serve a
  pre-backfill value for up to the TTL. Tracked as Bug-8692; the durable
  ``pending_kpi_reeval`` row written below is what stops the epoch bump
  withholding ``$KPIs`` until the next hourly sweep.
* **Undeployed-model runtime caches.** For a model with NO deployed version
  the query-router's join-graph / model-table / result caches degrade to the
  key ``(model_id, "", 0)``, so the live backfill does not self-invalidate
  them for their TTL. Benign in direction: the cached state is the
  PRE-migration one, and an undeployed model can serve no artifact at all
  (``artifact_built_for_current`` refuses a NULL deployed pointer), so this
  only delays the correction.
* **Tenant git repo.** Deploy tags the version in the per-tenant git repo, and
  Save writes its YAML. This migration does neither, so the git YAML for the
  corrected version still shows the legacy token. No runtime path reads it.
* **Ingest path.** ``model_snapshot/rehydrator._insert_joins`` writes a
  bundle's ``join_type`` verbatim (unlike ``yaml_deserialiser`` and the
  AtScale mapper, which normalise through ``split_join_token``), so importing
  a PRE-0194 export bundle re-creates undeclared tokens that no migration will
  run again to repair. Fail-closed (pockets simply refuse those plans), and
  tracked as Bug-8698. The shipped ``seeds/acme-demo/project.json`` is
  normalised in the same commit as this migration, in lockstep on both its
  live joins and its version snapshots, so a freshly seeded tenant is born in
  the post-0194 state.
* **Deploy-time health evidence.** ``dimension_attribute_verifications`` and
  ``join_population_checks`` rows stay bound to the OLD epoch, so they read as
  absent for the new one. Both are fail-closed health evidence —
  a missing row makes derived-grain relabel routing fall back to the ordinary
  route and makes join-population health "unmeasured". Neither can produce a
  wrong number, and the scheduler's relationship sweep re-proves the first on
  its cadence. This is the same state a deploy whose best-effort verification
  failed already produces.

Idempotency: after this runs, no row satisfies the target predicate, so a
second ``upgrade`` plans nothing, appends no version, and moves no pointer.

Revision ID: 0194
Revises: 0193
Create Date: 2026-08-05
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from shared.artifact_version_gate import artifact_incompatible_sql
from shared.db.model_lock import model_advisory_lock_key
from shared.semantic.join_orientation_backfill import (
    JoinBackfill,
    patch_snapshot_join_types,
    plan_backfills,
    resolves_no_fan_out,
)

logger = logging.getLogger(__name__)

revision = "0194"
down_revision = "0193"
branch_labels = None
depends_on = None

_JOINS = "joins"
_MODELS = "models"
_MODEL_VERSIONS = "model_versions"
_AGGREGATE_DEFINITIONS = "aggregate_definitions"
_POCKET_DEFINITIONS = "pocket_definitions"
_PENDING_KPI_REEVAL = "pending_kpi_reeval"
_KPIS = "kpis"
_AUDIT_EVENTS = "audit_events"

#: Recorded as the audit actor so an operator can tell this correction from a
#: human deploy at a glance.
_ACTOR = "system:migration-0194"

_UUID = postgresql.UUID(as_uuid=True)

_AGG_TABLE = sa.table(
    _AGGREGATE_DEFINITIONS,
    sa.column("model_id", _UUID),
    sa.column("status", sa.String),
    sa.column("is_stale", sa.Boolean),
    sa.column("built_for_version_id", _UUID),
    sa.column("built_for_epoch", sa.Integer),
)

_POCKET_TABLE = sa.table(
    _POCKET_DEFINITIONS,
    sa.column("model_id", _UUID),
    sa.column("status", sa.String),
    sa.column("retired_at", sa.DateTime(timezone=True)),
    sa.column("built_for_version_id", _UUID),
    sa.column("built_for_epoch", sa.Integer),
)


def _present_tables(*names: str) -> frozenset:
    """Which of ``names`` this schema has, resolved ONCE.

    Reflecting inside the per-model loop would issue an inspector round trip
    per affected model, and a module-level memo would be wrong when one
    process migrates several tenant schemas in turn.

    Deliberately a plain function returning a frozenset rather than a
    dataclass: Alembic executes a revision module WITHOUT registering it in
    ``sys.modules`` (``alembic.util.pyfiles.load_module_py``), so under
    ``from __future__ import annotations`` the ``@dataclass`` machinery
    cannot resolve its own string annotations and raises
    ``AttributeError: 'NoneType' object has no attribute '__dict__'`` at
    import time — i.e. the migration would fail before running a single
    statement. Guarded repo-wide by
    ``tests/unit/test_migration_modules_load_like_alembic.py``.
    """
    inspector = sa.inspect(op.get_bind())
    return frozenset(n for n in names if inspector.has_table(n))


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(
        c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table)
    )


def _lock_models(bind, model_ids: list[uuid.UUID]) -> None:
    """Take the per-model advisory lock for EVERY affected model, up front.

    Two reasons this happens before the live UPDATE rather than per model
    inside the repair loop:

    * **Contract.** ``shared/db/model_lock.py`` says the lock serialises every
      model-scoped writer of snapshot-owned state; Save, deploy, undeploy and
      revert all take it. This migration is a fourth mover of ``deploy_epoch``
      and of the deployed snapshot, and is reachable against a LIVE stack via
      the ``/admin`` migrate endpoint. Each service's
      ``test_model_lock_coverage.py`` discovers ``src.api`` ROUTES, so a
      migration is structurally invisible to it — nothing but this call
      enforces the invariant here.
    * **Lock ORDER.** ``versions.revert_to_version`` and the Save path take
      the advisory lock FIRST and then write ``joins`` rows. Backfilling the
      live rows before taking the advisory lock would invert that order, and
      an ABBA deadlock aborts the whole ``alembic upgrade`` transaction with
      an opaque ``40P01``. Locking every affected model first makes the
      migration agree with the API on order.

    Sorted so two concurrent runs (a retry racing a first attempt) also agree
    with each other. Transaction-scoped: released when the migration commits
    or rolls back. The wait is deliberately UNBOUNDED — unlike
    ``acquire_model_definition_lock``, which sets a ``lock_timeout`` because a
    user request must not hang. A schema upgrade blocking behind an in-flight
    Save is the correct outcome; failing fast would leave the tenant on a
    half-migrated chain.
    """
    for model_id in sorted(model_ids, key=str):
        bind.execute(
            sa.text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": model_advisory_lock_key(model_id)},
        )


def _apply_live_backfill(
    bind, plans: list[JoinBackfill], stored_cardinality: dict[str, object]
) -> list[JoinBackfill]:
    """Rewrite ``joins.join_type`` and return only the rows actually changed.

    The row-level guard is not decoration. This runs at READ COMMITTED, so a
    session that does not take the model lock can commit an edit between the
    read and this UPDATE. Dropping a plan whose UPDATE matched nothing keeps
    the snapshot patch below in lockstep with what the live table actually
    holds — patching a snapshot for a live value we did not write is precisely
    the live/deployed disagreement this migration exists to prevent. The guard
    pins BOTH fields the rewrite depends on, because both are read before they
    are written.
    """
    applied: list[JoinBackfill] = []
    # ``cardinality`` moves in the SAME statement, wherever the stored value
    # RESOLVES no fan-out. ``0191`` backfilled it for every row that existed
    # when it ran, but a row inserted BETWEEN the two migrations — a pre-0191
    # export bundle imported after it, Bug-8698 — never got that treatment, and
    # ``rehydrator._insert_joins`` writes an imported ``cardinality`` verbatim,
    # so the stored value may be NULL *or* a non-null string the vocabulary
    # rejects. Either way the legacy token is still the row's only working
    # carrier of fan-out, and the snapshot repair below writes that fan-out
    # into the deployed side; moving one side only would relocate the drift
    # from ``join_type`` to ``cardinality`` rather than removing it, and
    # leaving BOTH sides on an unusable value silently disarms the
    # many-to-many compatibility guard.
    #
    # The decision is bound from ``resolves_no_fan_out`` — the SAME function
    # the snapshot patch selects on — rather than restated as ``IS NULL`` in
    # SQL, so the two halves cannot diverge. A value the vocabulary DOES
    # recognise is never overwritten, which is what protects a modeller
    # declaration (the API's Literal makes an unrecognised value impossible to
    # declare through it).
    # Both fields the decision depends on are pinned in the WHERE clause, not
    # just ``join_type``. ``:overwrite`` is computed in Python from a value this
    # transaction READ, so the row must still hold that value for the decision
    # to apply to it; ``IS NOT DISTINCT FROM`` makes NULL compare as a value
    # rather than as unknown. Callers hold the model lock, so this can only
    # fire against a writer that bypasses it — in which case matching zero rows
    # drops the plan, the snapshot is left unpatched too, and the join keeps its
    # legacy token: the pre-migration state, repaired by a re-run.
    stmt = sa.text(
        f"UPDATE {_JOINS} SET join_type = :new, "
        "cardinality = CASE WHEN :overwrite THEN :card ELSE cardinality END "
        "WHERE id = :id AND join_type = :old "
        "AND cardinality IS NOT DISTINCT FROM :stored"
    ).bindparams(
        sa.bindparam("id", type_=_UUID),
        sa.bindparam("overwrite", type_=sa.Boolean),
        sa.bindparam("stored", type_=sa.String),
    )
    # Sorted by join id so the row locks this loop takes are acquired in a
    # deterministic order. It cannot prevent a deadlock against an arbitrary
    # multi-row writer that skips the model advisory lock (``project_rehydrator``
    # is a documented non-holder), but it removes migration-vs-migration and
    # migration-vs-ordered-writer cycles; anything left aborts one side with
    # 40P01 and rolls this whole transaction back cleanly, which is re-runnable.
    for plan in sorted(plans, key=lambda p: p.join_id):
        stored = stored_cardinality.get(plan.join_id)
        result = bind.execute(
            stmt,
            {
                "new": plan.new_token,
                "card": plan.new_cardinality,
                "overwrite": resolves_no_fan_out(stored),
                "stored": stored,
                "id": uuid.UUID(plan.join_id),
                "old": plan.old_token,
            },
        )
        if result.rowcount:
            applied.append(plan)
    return applied


def _repair_deployed_snapshot(
    bind,
    model_id: uuid.UUID,
    plans: list[JoinBackfill],
    present: frozenset,
    has_predictive_column: bool,
) -> None:
    """Bring the DEPLOYED snapshot back into agreement with the live rows.

    The model's advisory lock is already held (``_lock_models``, taken for
    every affected model before the live backfill so the migration and the API
    agree on lock order). This patches the deployed snapshot's join tokens,
    bumps ``deploy_epoch`` (which is what invalidates artifacts and every
    runtime cache), queues the KPI re-evaluation and writes an audit event. It does NOT append a ``model_versions`` row — see the
    module docstring's "Why the deployed snapshot is corrected in place".
    """
    model = bind.execute(
        sa.text(
            f"SELECT project_id, display_name, deployed_version_id, deploy_epoch "
            f"FROM {_MODELS} WHERE id = :m"
        ).bindparams(sa.bindparam("m", type_=_UUID)),
        {"m": model_id},
    ).first()
    if model is None or model.deployed_version_id is None:
        # Nothing binds a snapshot for this model, so there is no live/deployed
        # disagreement to create and nothing to repair. Its artifacts are
        # already incompatible under the shared gate (NULL deployed pointer).
        return
    version_id = uuid.UUID(str(model.deployed_version_id))

    deployed = bind.execute(
        sa.text(
            f"SELECT snapshot_json, snapshot_unavailable, summary "
            f"FROM {_MODEL_VERSIONS} WHERE id = :v"
        ).bindparams(sa.bindparam("v", type_=_UUID)),
        {"v": version_id},
    ).first()
    if deployed is None or deployed.snapshot_unavailable:
        # Bug-6295 placeholder history carries no authentic shape; deploy
        # refuses to publish one and this migration must not manufacture one.
        return
    snapshot = deployed.snapshot_json
    if isinstance(snapshot, str):
        # Defensive: a schema where the column was written as text rather than
        # jsonb. Parse rather than silently skipping the model.
        try:
            snapshot = json.loads(snapshot)
        except ValueError:
            return
    if not isinstance(snapshot, dict):
        return

    patched, changed = patch_snapshot_join_types(
        snapshot, {p.join_id: p for p in plans}
    )
    if changed == 0:
        # The deployed snapshot either never carried these edges, or already
        # disagreed with live on them. Either way this migration did not create
        # the divergence and must not paper over it by rewriting the snapshot
        # to a token the router would then bind differently.
        return

    note = (
        f"[migration 0194] Declared an explicit join orientation for {changed} "
        f"join(s) in this deployed snapshot that carried a legacy undeclared "
        f"join_type, in lockstep with the live model. No other definition "
        f"changed."
    )
    bind.execute(
        sa.text(
            f"UPDATE {_MODEL_VERSIONS} SET snapshot_json = :snap, "
            "summary = CASE WHEN summary IS NULL OR summary = '' THEN :note "
            "               ELSE summary || ' ' || :note END "
            "WHERE id = :v"
        ).bindparams(
            sa.bindparam("v", type_=_UUID),
            sa.bindparam("snap", type_=postgresql.JSONB),
        ),
        {"snap": patched, "note": note, "v": version_id},
    )

    new_epoch = int(model.deploy_epoch or 0) + 1
    # ``predictive_built_for_version_id`` is cleared for the same reason a real
    # deploy does not need to: the optimizer's predictive sweep SKIPS a model
    # whose deployed pointer still equals it, and the pointer does not move
    # here. Leaving it set would freeze predictive candidate generation on the
    # pre-correction definition.
    predictive_clear = (
        ", predictive_built_for_version_id = NULL" if has_predictive_column else ""
    )
    bind.execute(
        sa.text(
            f"UPDATE {_MODELS} SET deploy_epoch = :e, last_deployed_at = now()"
            f"{predictive_clear} WHERE id = :m"
        ).bindparams(sa.bindparam("m", type_=_UUID)),
        {"e": new_epoch, "m": model_id},
    )

    _stale_incompatible_artifacts(bind, model_id, version_id, new_epoch, present)
    _enqueue_pending_kpi_reeval(
        bind, model_id, model.project_id, new_epoch, present
    )
    _audit(
        bind, model_id, model.display_name, version_id, new_epoch, changed,
        present,
    )


def _stale_incompatible_artifacts(
    bind,
    model_id: uuid.UUID,
    version_id: uuid.UUID,
    new_epoch: int,
    present: frozenset,
) -> None:
    """Mirror ``versions._stale_incompatible_artifacts`` through the SAME rule.

    ``artifact_incompatible_sql`` is imported rather than restated: it is the
    single source of truth the runtime matchers evaluate in Python and the
    deploy path evaluates in SQL, and a second copy here would be a third rule
    that merely happens to agree today (exactly the defect Bug-8250's re-gate
    removed).
    """
    if _AGGREGATE_DEFINITIONS in present:
        bind.execute(
            sa.update(_AGG_TABLE)
            .where(
                _AGG_TABLE.c.model_id == model_id,
                _AGG_TABLE.c.status == "active",
                _AGG_TABLE.c.is_stale.is_(False),
                artifact_incompatible_sql(
                    _AGG_TABLE.c.built_for_version_id,
                    _AGG_TABLE.c.built_for_epoch,
                    version_id,
                    new_epoch,
                ),
            )
            .values(is_stale=True)
        )
    if _POCKET_DEFINITIONS in present:
        bind.execute(
            sa.update(_POCKET_TABLE)
            .where(
                _POCKET_TABLE.c.model_id == model_id,
                _POCKET_TABLE.c.status == "fresh",
                _POCKET_TABLE.c.retired_at.is_(None),
                artifact_incompatible_sql(
                    _POCKET_TABLE.c.built_for_version_id,
                    _POCKET_TABLE.c.built_for_epoch,
                    version_id,
                    new_epoch,
                ),
            )
            .values(status="stale")
        )


def _enqueue_pending_kpi_reeval(
    bind, model_id: uuid.UUID, project_id, new_epoch: int, present: frozenset
) -> None:
    """Mirror ``versions._enqueue_pending_kpi_reeval`` (Bug-7982 finding 6).

    The epoch bump makes the ``$KPIs`` serve predicate withhold every
    ``kpi_latest`` row stamped with the OLD epoch. Without this durable outbox
    row the withholding lasts until the next hourly sweep with no signal —
    silently empty ``$KPIs`` on every republished model.
    """
    if _PENDING_KPI_REEVAL not in present or _KPIS not in present:
        return
    bind.execute(
        sa.text(
            f"INSERT INTO {_PENDING_KPI_REEVAL} "
            "(id, model_id, project_id, requested_for_epoch, requested_at) "
            "SELECT gen_random_uuid(), :m, :p, :e, now() "
            f"WHERE EXISTS (SELECT 1 FROM {_KPIS} "
            "               WHERE model_id = :m AND is_deployed IS TRUE) "
            "ON CONFLICT ON CONSTRAINT uq_pending_kpi_reeval_model DO UPDATE "
            "SET project_id = EXCLUDED.project_id, "
            "    requested_for_epoch = EXCLUDED.requested_for_epoch, "
            "    requested_at = EXCLUDED.requested_at"
        ).bindparams(
            sa.bindparam("m", type_=_UUID), sa.bindparam("p", type_=_UUID)
        ),
        {"m": model_id, "p": project_id, "e": new_epoch},
    )


def _audit(
    bind,
    model_id: uuid.UUID,
    display_name,
    version_id: uuid.UUID,
    new_epoch: int,
    changed: int,
    present: frozenset,
) -> None:
    """Record the republish as a ``model.deploy`` audit event.

    Same action name a human deploy writes, so an operator asking "why did this
    model republish / why did these numbers move" finds it on the timeline they
    already use; ``detail.reason`` and the non-email actor distinguish it.
    """
    if _AUDIT_EVENTS not in present:
        return
    bind.execute(
        sa.text(
            f"INSERT INTO {_AUDIT_EVENTS} "
            "(id, timestamp, actor_email, action, target_type, target_id, "
            " target_name, severity, detail) "
            "VALUES (gen_random_uuid(), now(), :actor, 'model.deploy', 'model', "
            "        :m, :name, 'warn', :detail)"
        ).bindparams(
            sa.bindparam("m", type_=_UUID),
            sa.bindparam("detail", type_=postgresql.JSONB),
        ),
        {
            "actor": _ACTOR,
            "m": model_id,
            "name": display_name,
            "detail": {
                "version_id": str(version_id),
                "deploy_epoch": new_epoch,
                "joins_redeclared": changed,
                "reason": "join_orientation_backfill",
                "migration": revision,
            },
        },
    )


def upgrade() -> None:
    if not _table_exists(_JOINS):
        return
    bind = op.get_bind()

    # Pass 1 — UNLOCKED, and used ONLY to learn which models to lock. Nothing
    # is decided from it: the ``cardinality`` overwrite decision below reads a
    # value, and a value read before the lock is not a value this transaction
    # controls. That is how the round-4 widening (moving the predicate from
    # ``CASE WHEN cardinality IS NULL``, which Postgres evaluated against the
    # row, into Python) briefly reopened a clobber window.
    scout = bind.execute(
        sa.text(f"SELECT id, model_id, join_type FROM {_JOINS}")
    ).fetchall()
    scouted = plan_backfills((r.id, r.model_id, r.join_type) for r in scout)
    if not scouted:
        return
    locked = sorted({uuid.UUID(p.model_id) for p in scouted}, key=str)
    _lock_models(bind, locked)

    # Pass 2 — the AUTHORITATIVE read, taken under the locks, scoped to the
    # models we hold. Every lock-respecting writer of these rows (Save, deploy,
    # revert, and the Joins panel routes, all covered by
    # ``test_model_lock_coverage``) is now excluded, so what this reads is what
    # the UPDATE will find.
    #
    # A join created on a model we did NOT lock between the two passes is
    # deliberately skipped rather than raced for: it keeps its legacy token,
    # which is the pre-migration state, and this migration is idempotent, so a
    # re-run repairs it. Fail-closed beats a second unlocked round.
    rows = bind.execute(
        sa.text(
            f"SELECT id, model_id, join_type, cardinality FROM {_JOINS} "
            "WHERE model_id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": [str(m) for m in locked]},
    ).fetchall()
    plans = plan_backfills((r.id, r.model_id, r.join_type) for r in rows)
    if not plans:
        return
    stored_cardinality = {str(r.id): r.cardinality for r in rows}

    applied = _apply_live_backfill(bind, plans, stored_cardinality)
    if not applied:
        return

    if not (
        _table_exists(_MODELS)
        and _table_exists(_MODEL_VERSIONS)
        and _has_column(_MODELS, "deployed_version_id")
        and _has_column(_MODELS, "deploy_epoch")
    ):
        # A schema too old to carry a deploy pointer cannot have a deployed
        # snapshot to disagree with, so the live backfill alone is complete.
        return

    by_model: dict[str, list[JoinBackfill]] = defaultdict(list)
    for plan in applied:
        by_model[plan.model_id].append(plan)
    has_predictive = _has_column(_MODELS, "predictive_built_for_version_id")
    present = _present_tables(
        _AGGREGATE_DEFINITIONS,
        _POCKET_DEFINITIONS,
        _PENDING_KPI_REEVAL,
        _KPIS,
        _AUDIT_EVENTS,
    )
    for model_id, model_plans in sorted(by_model.items()):
        _repair_deployed_snapshot(
            bind, uuid.UUID(model_id), model_plans, present, has_predictive
        )

    _warn_about_anything_left(bind)


def _warn_about_anything_left(bind) -> None:
    """Log any join this run could not reach, so the gap is not silent.

    Two ways a row survives: it belongs to a model that was NOT in the locked
    set because it appeared between the scout and the re-read, or its UPDATE
    was refused by the row guard because a writer that bypasses the model lock
    moved it. Both leave the join on its legacy token — the pre-migration
    state, so nothing is wrong, but pockets keep refusing every plan that
    touches it and NOTHING schedules a retry. Re-running ``alembic upgrade``
    after stamping back to 0193 repairs it; this line is what tells an operator
    that is worth doing.
    """
    remaining = bind.execute(
        sa.text(f"SELECT id, model_id, join_type FROM {_JOINS}")
    ).fetchall()
    left = plan_backfills((r.id, r.model_id, r.join_type) for r in remaining)
    if not left:
        return
    logger.warning(
        "0194: %d join(s) across %d model(s) still carry an undeclared "
        "join_type after this run and were skipped (a concurrent write moved "
        "them, or their model appeared after the lock set was chosen). They "
        "keep their pre-migration behaviour and pockets stay refused on those "
        "models; re-run this migration to repair them. Joins: %s",
        len(left),
        len({p.model_id for p in left}),
        sorted(p.join_id for p in left),
    )


def downgrade() -> None:
    """Irreversible by design.

    Restoring the legacy tokens would resurrect an anchor-dependent join
    rendering (the defect), un-stale artifacts that have since been rebuilt
    under the corrected one, and require knowing which of several legacy
    spellings each row originally held — information this migration
    deliberately does not preserve, because keeping it would be keeping the
    ambiguity. An artifact built before this migration is wrong under the
    corrected renderer and wrong again if the code is rolled back to build it a
    third way; leaving the corrected state in place forces exactly one rebuild
    under whichever renderer is actually running. A modeller who wants a
    different orientation declares it in the Joins panel, which is the
    supported path and produces an auditable version of its own.
    """
