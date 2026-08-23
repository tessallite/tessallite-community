"""L-NQ-ARTIFACT — Named Query delete / revert lifecycle guards.

F-013-07: model/project delete must delete the Named Query family BEFORE
data_targets (the artifact->data_targets FK is NO ACTION) and schedule a DROP of
the NQ physical table.
F-013-02: revert preserves NQ identity (own historical ids) and schedules a DROP
for NQs removed by the reverted-to version.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.model_snapshot import cascade_delete
from shared.model_snapshot.rehydrator import (
    RehydrationMode,
    _insert_named_queries,
    _schedule_removed_named_query_cleanup,
)


# --- F-013-07: cascade delete step ordering --------------------------------

def _step_names():
    return [name for name, _sql in cascade_delete._MODEL_DELETE_STEPS]


def test_named_query_family_is_deleted_before_data_targets():
    names = _step_names()
    for t in (
        "named_query_artifacts",
        "named_query_refresh_runs",
        "named_query_refresh_policies",
        "named_queries",
    ):
        assert t in names, f"{t} missing from cascade delete steps (Bug-9162)"
    # The NO ACTION artifact->data_targets FK requires artifacts to go first.
    assert names.index("named_query_artifacts") < names.index("data_targets")
    assert names.index("named_queries") < names.index("data_targets")
    # Artifacts before their parent named_queries (child-before-parent).
    assert names.index("named_query_artifacts") < names.index("named_queries")


def test_bug9222_restore_rebuilds_nq_family_when_pockets_are_preserved():
    """RESTORE must not leave NQ rows behind when the pocket family is kept.

    ``preserve_pockets`` is used by the definition-only RESTORE path, but it is
    not a Named Query preservation flag.  Keep this as a source-shape guard so
    a future edit cannot accidentally put the NQ delete block back under the
    pocket guard and make RESTORE hit duplicate historical primary keys.
    """
    import ast
    import inspect
    import textwrap

    from shared.model_snapshot import rehydrator

    source = textwrap.dedent(inspect.getsource(rehydrator._truncate_model_children))
    tree = ast.parse(source)
    named_query_types = {
        "NamedQuery",
        "NamedQueryArtifact",
        "NamedQueryRefreshPolicy",
        "NamedQueryRefreshRun",
    }
    found_delete = False

    def visit(node, *, under_pocket_guard=False):
        nonlocal found_delete
        guarded = under_pocket_guard
        if isinstance(node, ast.If):
            guarded = guarded or any(
                isinstance(name, ast.Name) and name.id == "preserve_pockets"
                for name in ast.walk(node.test)
            )
        if isinstance(node, ast.Call):
            names = {
                name.id
                for name in ast.walk(node)
                if isinstance(name, ast.Name)
            }
            is_delete = any(
                isinstance(name, ast.Name) and name.id == "delete"
                for name in ast.walk(node.func)
            )
            if is_delete and names & named_query_types:
                found_delete = True
                assert not guarded, (
                    "Named Query cleanup is incorrectly gated by preserve_pockets"
                )
        for child in ast.iter_child_nodes(node):
            visit(child, under_pocket_guard=guarded)

    visit(tree)
    assert found_delete, "truncate funnel no longer contains Named Query cleanup"


# --- F-013-02: revert preserves NQ identity --------------------------------

def _snapshot_one_nq(nq_id, artifact_id):
    return {
        "named_queries": [
            {
                "id": str(nq_id),
                "name": "q1",
                "definition_sql": "SELECT 1",
                "shape": "projection",
                "artifact": {
                    "id": str(artifact_id),
                    "target_id": str(uuid.uuid4()),
                    "physical_table_name": "nq_src_abcd",
                },
            }
        ]
    }


def _captured_insert_params(db_execute_mock):
    """Return {table_name: params} for each insert() call recorded on the mock."""
    out = {}
    for call in db_execute_mock.await_args_list:
        stmt = call.args[0]
        table = getattr(getattr(stmt, "table", None), "name", None)
        try:
            params = stmt.compile().params
        except Exception:
            params = {}
        out.setdefault(table, []).append(params)
    return out


@pytest.mark.asyncio
async def test_restore_preserves_named_query_and_artifact_ids():
    nq_id, art_id = uuid.uuid4(), uuid.uuid4()
    db = AsyncMock()
    await _insert_named_queries(
        uuid.uuid4(), _snapshot_one_nq(nq_id, art_id), db,
        mode=RehydrationMode.RESTORE,
    )
    params = _captured_insert_params(db.execute)
    assert params["named_queries"][0]["id"] == nq_id  # own historical id kept
    assert params["named_query_artifacts"][0]["id"] == art_id
    # Liveness cleared so the artifact re-earns trust via a refresh.
    assert params["named_query_artifacts"][0]["status"] == "stale"
    assert params["named_query_artifacts"][0]["built_for_version_id"] is None


@pytest.mark.asyncio
async def test_import_mints_fresh_named_query_id():
    nq_id, art_id = uuid.uuid4(), uuid.uuid4()
    db = AsyncMock()
    await _insert_named_queries(
        uuid.uuid4(), _snapshot_one_nq(nq_id, art_id), db,
        mode=RehydrationMode.IMPORT,
    )
    params = _captured_insert_params(db.execute)
    # Import into a (possibly same-tenant) clone must not reuse the source id.
    assert params["named_queries"][0]["id"] != nq_id
    assert params["named_query_artifacts"][0]["id"] != art_id


@pytest.mark.asyncio
async def test_bug9222_import_mints_policy_and_destination_physical_identity():
    """An import must not carry source NQ policy/table identity into a clone."""
    nq_id, art_id, policy_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source_table = "nq_src_abcd"
    snapshot = _snapshot_one_nq(nq_id, art_id)
    snapshot["named_queries"][0]["artifact"]["physical_table_name"] = source_table
    snapshot["named_queries"][0]["refresh_policy"] = {
        "id": str(policy_id),
        "cron_expression": "0 * * * *",
        "is_enabled": True,
    }
    db = AsyncMock()
    await _insert_named_queries(
        uuid.uuid4(), snapshot, db, mode=RehydrationMode.IMPORT,
    )
    params = _captured_insert_params(db.execute)
    assert params["named_queries"][0]["id"] != nq_id
    assert params["named_query_artifacts"][0]["id"] != art_id
    assert params["named_query_artifacts"][0]["physical_table_name"] != source_table
    assert params["named_query_artifacts"][0]["physical_table_name"].startswith("nq_")
    assert params["named_query_refresh_policies"][0]["id"] != policy_id
    assert params["named_query_refresh_policies"][0]["named_query_id"] == params["named_queries"][0]["id"]


def test_bug9222_import_rewrites_nested_named_query_ids():
    """Every import funnel's shared PK rewriter includes NQ nested identities."""
    from shared.model_snapshot.importer import prepare_snapshot_for_import

    old_nq, old_art, old_policy = (str(uuid.uuid4()) for _ in range(3))
    source = {
        "model": {"id": str(uuid.uuid4())},
        "named_queries": [{
            "id": old_nq,
            "artifact": {"id": old_art},
            "refresh_policy": {"id": old_policy},
        }],
    }
    rewritten, missing = prepare_snapshot_for_import(
        source, new_model_id=uuid.uuid4(),
    )
    assert missing == []
    row = rewritten["named_queries"][0]
    assert row["id"] != old_nq
    assert row["artifact"]["id"] != old_art
    assert row["refresh_policy"]["id"] != old_policy


# --- F-013-02: revert schedules cleanup for removed NQ ---------------------

@pytest.mark.asyncio
async def test_removed_named_query_schedules_physical_cleanup():
    model_id = uuid.uuid4()
    kept_nq, removed_nq = uuid.uuid4(), uuid.uuid4()
    kept_art = _art(uuid.uuid4())
    removed_art = _art(uuid.uuid4())

    db = AsyncMock()
    result = AsyncMock()
    result.all = lambda: [(kept_art, kept_nq), (removed_art, removed_nq)]
    db.execute.return_value = result

    # Snapshot only keeps kept_nq -> removed_nq's artifact must be scheduled.
    snapshot = {"named_queries": [{"id": str(kept_nq)}]}

    with patch(
        "shared.physical_cleanup.schedule_model_physical_cleanup",
        new_callable=AsyncMock,
    ) as sched:
        await _schedule_removed_named_query_cleanup(
            model_id, snapshot, db, requested_by="revert",
        )

    sched.assert_awaited_once()
    passed = list(sched.await_args.kwargs["named_query_artifacts"])
    assert passed == [removed_art]


@pytest.mark.asyncio
async def test_no_removed_named_query_schedules_nothing():
    model_id = uuid.uuid4()
    kept_nq = uuid.uuid4()
    kept_art = _art(uuid.uuid4())
    db = AsyncMock()
    result = AsyncMock()
    result.all = lambda: [(kept_art, kept_nq)]
    db.execute.return_value = result
    snapshot = {"named_queries": [{"id": str(kept_nq)}]}
    with patch(
        "shared.physical_cleanup.schedule_model_physical_cleanup",
        new_callable=AsyncMock,
    ) as sched:
        await _schedule_removed_named_query_cleanup(
            model_id, snapshot, db, requested_by="revert",
        )
    sched.assert_not_called()


def _art(art_id):
    import types
    return types.SimpleNamespace(id=art_id, physical_table_name="nq_x_1")


# --- F-020-01: clone-isolation reseed guard (CP-04 T3 challenger CP04-F1) ---

@pytest.mark.asyncio
async def test_import_reseeds_named_query_artifact_physical_table_name():
    """F-020-01 (data-loss guard): on IMPORT the NQ artifact physical_table_name
    is rebound to the destination seed so a clone's first refresh DROP/CTAS never
    lands on the SOURCE model's table."""
    nq_id, art_id = uuid.uuid4(), uuid.uuid4()
    db = AsyncMock()
    await _insert_named_queries(
        uuid.uuid4(), _snapshot_one_nq(nq_id, art_id), db,
        mode=RehydrationMode.IMPORT, reseed="dstseed",
    )
    params = _captured_insert_params(db.execute)
    name = params["named_query_artifacts"][0]["physical_table_name"]
    assert name != "nq_src_abcd", "source NQ table name leaked into the clone (data-loss path)"
    assert name.startswith("nq_dstseed_"), name


@pytest.mark.asyncio
async def test_restore_preserves_named_query_artifact_physical_table_name():
    """RESTORE (revert of the same model) preserves the artifact's own
    physical_table_name even when a reseed value is supplied."""
    nq_id, art_id = uuid.uuid4(), uuid.uuid4()
    db = AsyncMock()
    await _insert_named_queries(
        uuid.uuid4(), _snapshot_one_nq(nq_id, art_id), db,
        mode=RehydrationMode.RESTORE, reseed="dstseed",
    )
    params = _captured_insert_params(db.execute)
    assert params["named_query_artifacts"][0]["physical_table_name"] == "nq_src_abcd"
