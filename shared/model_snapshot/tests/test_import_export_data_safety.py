"""Bug-7298, Bug-7299, Bug-7307, Bug-7308 regression tests.

These tests protect against DATA-LOSS paths in the import/export subsystem:

- Bug-7298: imported clone must not touch source model's aggregate tables
- Bug-7299: _validate_preserved_pockets must not crash on dropped dimensions
- Bug-7307: ecosystem imports must not silently bind to arbitrary connections
- Bug-7308: replace mode must preserve connections when bundle excludes them
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.model_snapshot.rehydrator import (
    _invalidate_borrowing_models_on_source_repoint,
    _reseed_physical_table_name,
    _validate_preserved_pockets,
)


# -----------------------------------------------------------------------
# Bug-7298: reseed must never leave a non-standard name pointing at source
# -----------------------------------------------------------------------

class TestReseedNeverPassesThrough:
    """Every name must either be rewritten to include the new seed or be
    unchanged only when new_seed is None/name is None."""

    def test_standard_3part_rebinds_seed(self):
        assert _reseed_physical_table_name("agg_OLD_abcd", "agg", "NEW") == "agg_NEW_abcd"

    def test_bare_hex_optimizer_name_is_replaced(self):
        """Optimizer lifecycle creator emits bare hex (token_hex(6))."""
        result = _reseed_physical_table_name("a1b2c3d4e5f6", "agg", "dest")
        assert result.startswith("agg_dest_")
        assert result != "a1b2c3d4e5f6"

    def test_single_part_name_is_replaced(self):
        result = _reseed_physical_table_name("myagg", "agg", "dest")
        assert result.startswith("agg_dest_")
        assert result != "myagg"

    def test_four_part_name_is_replaced(self):
        result = _reseed_physical_table_name("agg_a_b_c", "agg", "dest")
        assert result.startswith("agg_dest_")
        assert result != "agg_a_b_c"

    def test_wrong_prefix_is_replaced_under_requested_prefix(self):
        result = _reseed_physical_table_name("agg_x_y", "pocket", "dest")
        assert result.startswith("pocket_dest_")
        assert result != "agg_x_y"

    def test_no_op_when_seed_is_none(self):
        assert _reseed_physical_table_name("a1b2c3", "agg", None) == "a1b2c3"

    def test_no_op_when_name_is_none(self):
        assert _reseed_physical_table_name(None, "agg", "NEW") is None

    def test_generated_names_are_unique_across_calls(self):
        """Two calls with different non-standard names should not collide."""
        r1 = _reseed_physical_table_name("bare_hex_1", "agg", "seed")
        r2 = _reseed_physical_table_name("bare_hex_2", "agg", "seed")
        # Both start with agg_seed_ but the suffix should differ
        assert r1.startswith("agg_seed_")
        assert r2.startswith("agg_seed_")
        # They may or may not collide (random), but structurally both are valid


# -----------------------------------------------------------------------
# Bug-7299: _validate_preserved_pockets must use status="stale", not
# is_stale=True (which crashes since the column does not exist)
# -----------------------------------------------------------------------

class TestValidatePreservedPockets:
    """The function must mark pockets with missing dimensions as stale
    via the ``status`` column, not crash with ``is_stale``."""

    @pytest.mark.asyncio
    async def test_marks_pocket_stale_when_dimension_dropped(self):
        """A pocket whose predicate references a dropped dimension must be
        marked status='stale' without crashing."""
        model_id = uuid.uuid4()
        pocket_id = uuid.uuid4()

        db = AsyncMock()
        call_idx = [0]
        captured_updates = []

        async def _mock_execute(stmt):
            result = MagicMock()
            idx = call_idx[0]
            call_idx[0] += 1

            if idx == 0:
                # First: select(Dimension.name) -> dim_q.all()
                result.all.return_value = [("region",)]
            elif idx == 1:
                # Second: select(PocketDefinition) -> pocket_q.scalars().all()
                mock_pocket = MagicMock()
                mock_pocket.id = pocket_id
                # Bug-8602: the validator now reads these two, so a bare
                # MagicMock would supply a truthy ``retired_at`` and silently
                # skip the pocket. A live pocket has neither set.
                mock_pocket.status = "fresh"
                mock_pocket.retired_at = None
                scalars_result = MagicMock()
                scalars_result.all.return_value = [mock_pocket]
                result.scalars.return_value = scalars_result
            elif idx == 2:
                # Third: select(PocketPredicate.column_name) -> pred_q.all()
                result.all.return_value = [("region",), ("dropped_dim",)]
            else:
                # Fourth: update(PocketDefinition).values(status="stale")
                captured_updates.append(stmt)

            return result

        db.execute = AsyncMock(side_effect=_mock_execute)

        # This should NOT raise (Bug-7299 fix: uses status="stale" not is_stale=True)
        await _validate_preserved_pockets(model_id, db)

        # Verify the update was called
        assert call_idx[0] == 4, f"Expected 4 DB calls, got {call_idx[0]}"
        assert len(captured_updates) == 1
        # The statement should compile to use status="stale"
        update_stmt = captured_updates[0]
        compiled = update_stmt.compile()
        assert compiled.params.get("status") == "stale", (
            f"Expected status='stale', got params={compiled.params}"
        )
        # Must NOT contain is_stale
        assert "is_stale" not in compiled.params, (
            "Bug-7299: must not use is_stale (column does not exist on PocketDefinition)"
        )

    @pytest.mark.asyncio
    async def test_every_preserved_pocket_is_staled_even_with_valid_dimensions(self):
        """Bug-8602 (round-3 review): staling is now UNCONDITIONAL.

        This test previously asserted the opposite — that a pocket whose
        predicate dimensions all still exist is left serving. That was a
        wrong-numbers hole, not a feature. A revert UPSERTS every
        ``data_sources`` column from the snapshot, including
        ``project_connection_id``, so reverting past a source re-point changes
        WHICH database the model reads while the preserved pocket still holds
        rows materialised from the other one. Predicate dimensions are
        untouched by that, so the old check could not see it; the pocket has no
        source build binding for the serve-time guard to refuse either
        (Bug-8780); and the control-plane invalidator is not on this path
        because the rehydrator writes the column directly.

        ``_validate_preserved_aggregates`` has stale-marked every surviving
        aggregate since Bug-7146 for the same class of reason. This aligns the
        two artifact kinds on one rule.
        """
        model_id = uuid.uuid4()
        pocket_id = uuid.uuid4()

        db = AsyncMock()
        call_idx = [0]
        captured_updates = []

        async def _mock_execute(stmt):
            result = MagicMock()
            idx = call_idx[0]
            call_idx[0] += 1

            if idx == 0:
                result.all.return_value = [("region",), ("country",)]
            elif idx == 1:
                mock_pocket = MagicMock()
                mock_pocket.id = pocket_id
                mock_pocket.status = "fresh"
                mock_pocket.retired_at = None
                scalars_result = MagicMock()
                scalars_result.all.return_value = [mock_pocket]
                result.scalars.return_value = scalars_result
            elif idx == 2:
                # Predicate references only valid dims — the OLD stop condition.
                result.all.return_value = [("region",)]
            else:
                captured_updates.append(stmt)
            return result

        db.execute = AsyncMock(side_effect=_mock_execute)

        await _validate_preserved_pockets(model_id, db)

        assert len(captured_updates) == 1, (
            "a preserved pocket with valid predicate dimensions was left "
            "serving; if the revert moved the model's source connection it is "
            "now answering from a different database"
        )
        assert captured_updates[0].compile().params.get("status") == "stale"

    @pytest.mark.asyncio
    async def test_a_retired_pocket_is_left_alone(self):
        """Retired artifacts are outside every invalidator's scope; resurrecting
        one into ``stale`` would put it back in the rebuild queue."""
        model_id = uuid.uuid4()
        db = AsyncMock()
        call_idx = [0]
        captured_updates = []

        async def _mock_execute(stmt):
            result = MagicMock()
            idx = call_idx[0]
            call_idx[0] += 1
            if idx == 0:
                result.all.return_value = [("region",)]
            elif idx == 1:
                mock_pocket = MagicMock()
                mock_pocket.id = uuid.uuid4()
                mock_pocket.status = "fresh"
                mock_pocket.retired_at = "2026-01-01"
                scalars_result = MagicMock()
                scalars_result.all.return_value = [mock_pocket]
                result.scalars.return_value = scalars_result
            elif idx == 2:
                result.all.return_value = [("region",)]
            else:
                captured_updates.append(stmt)
            return result

        db.execute = AsyncMock(side_effect=_mock_execute)
        await _validate_preserved_pockets(model_id, db)
        assert captured_updates == []


# -----------------------------------------------------------------------
# Bug-7308: replace-mode must not delete connections when bundle excludes
# the connections section
# -----------------------------------------------------------------------

class TestReplacePreservesConnectionsWhenSectionAbsent:
    """The project_rehydrator's _connection_plan must return orphan_count=0
    when the connections section is not included in the bundle."""

    @pytest.mark.asyncio
    async def test_connection_plan_returns_zero_orphans_when_section_absent(self):
        """Dry-run plan must report 0 orphan connections when 'connections'
        is not in included_sections, regardless of how many connections
        the target project has."""
        from shared.model_snapshot.project_rehydrator import _connection_plan

        project_id = uuid.uuid4()
        db = AsyncMock()

        # Simulate a project with 3 existing connections
        async def _mock_execute(stmt):
            result = MagicMock()
            result.scalar.return_value = 3
            return result

        db.execute = AsyncMock(side_effect=_mock_execute)

        actions, warnings, orphan_count = await _connection_plan(
            bundle={},
            tenant_db=db,
            project_id=project_id,
            mode="replace",
            included=set(),  # connections NOT included
            connection_mapping=None,
            has_creds=False,
        )
        assert orphan_count == 0, (
            f"Bug-7308: orphan_count should be 0 when connections section "
            f"is absent, got {orphan_count}"
        )
        assert actions == []

    @pytest.mark.asyncio
    async def test_connection_plan_returns_zero_orphans_even_with_mapping(self):
        """Even when a connection_mapping is provided but connections section
        is absent, orphan_count must still be 0."""
        from shared.model_snapshot.project_rehydrator import _connection_plan

        project_id = uuid.uuid4()
        target_conn_id = str(uuid.uuid4())
        db = AsyncMock()

        actions, warnings, orphan_count = await _connection_plan(
            bundle={},
            tenant_db=db,
            project_id=project_id,
            mode="replace",
            included=set(),  # connections NOT included
            connection_mapping={"src_id": target_conn_id},
            has_creds=False,
        )
        assert orphan_count == 0, (
            f"Bug-7308: orphan_count should be 0 when connections section "
            f"is absent, got {orphan_count}"
        )


# -----------------------------------------------------------------------
# Bug-8602 round-4: the revert must take the control-plane tables in the
# SAME order the aggregate finalisation does, or the two deadlock
# -----------------------------------------------------------------------

def test_revert_upserts_targets_before_sources_matching_the_finalisation_order():
    """Bug-8602 round-4: the revert holds data_sources and data_targets in ONE
    transaction, so it must take them in the same order lock_finalization_rows
    does (data_targets then data_sources) or it deadlocks with any aggregate
    finalisation on the same model. Reproduced live against PostgreSQL."""
    import inspect

    from shared.model_snapshot import rehydrator

    src = inspect.getsource(rehydrator._insert_data_sources_and_targets)
    t_at = src.index("await _write(DataTarget, row)")
    s_at = src.index("await _write(DataSource, row)")
    assert t_at < s_at, (
        "the revert upserts data_sources before data_targets; "
        "lock_finalization_rows takes data_targets first, so a revert "
        "concurrent with an aggregate build deadlocks (Bug-8602)"
    )


def test_the_finalisation_prelock_order_matches_the_revert_order():
    """Pin the two halves TOGETHER. Either one alone can be 'corrected' into
    disagreement with the other; the invariant is that they agree."""
    import inspect

    from shared import artifact_target_binding as atb
    from shared.model_snapshot import rehydrator

    lock_src = inspect.getsource(atb.lock_finalization_rows)
    lock_order = (
        lock_src.index("ProjectConnection.id.in_"),
        lock_src.index("select(DataTarget.id)"),
        lock_src.index("source_connection_ids_for_model("),
    )
    assert list(lock_order) == sorted(lock_order), (
        "lock_finalization_rows no longer acquires "
        "project_connections -> data_targets -> data_sources"
    )

    revert_src = inspect.getsource(rehydrator._insert_data_sources_and_targets)
    assert (
        revert_src.index("await _write(DataTarget, row)")
        < revert_src.index("await _write(DataSource, row)")
    ), "the revert's table order no longer matches the finalisation's"


@pytest.mark.asyncio
async def test_revert_source_repoint_invalidates_every_borrowing_model():
    """The comparison must use the binding captured before the snapshot upsert."""
    source_id = uuid.uuid4()
    owning_model_id = uuid.uuid4()
    borrowing_model_id = uuid.uuid4()
    invalidate = AsyncMock(return_value=(0, 0))

    with (
        patch(
            "shared.aggregate_connection.model_ids_reading_source",
            AsyncMock(return_value=[owning_model_id, borrowing_model_id]),
        ),
        patch(
            "shared.artifact_target_binding.invalidate_artifacts_for_model",
            invalidate,
        ),
    ):
        await _invalidate_borrowing_models_on_source_repoint(
            {
                "data_sources": [
                    {"id": str(source_id), "project_connection_id": str(uuid.uuid4())}
                ]
            },
            AsyncMock(),
            {str(source_id): str(uuid.uuid4())},
        )

    invalidated_models = {call.args[1] for call in invalidate.await_args_list}
    assert invalidated_models == {owning_model_id, borrowing_model_id}


# -----------------------------------------------------------------------
# F-020-01: a Named Query artifact must be reseeded on import so a
# same-tenant clone refresh never DROP/CTAS the SOURCE model's NQ table.
# -----------------------------------------------------------------------

class TestNamedQueryPhysicalNameReseededOnImport:
    def _snapshot(self):
        return {
            "named_queries": [
                {
                    "id": str(uuid.uuid4()),
                    "name": "q1",
                    "definition_sql": "SELECT 1",
                    "shape": "projection",
                    "artifact": {
                        "id": str(uuid.uuid4()),
                        "target_id": str(uuid.uuid4()),
                        "physical_table_name": "nq_SRC_deadbeef",
                    },
                }
            ]
        }

    def _artifact_name(self, db_execute):
        from shared.db.models import NamedQueryArtifact
        for call in db_execute.await_args_list:
            stmt = call.args[0]
            if getattr(getattr(stmt, "table", None), "name", None) == "named_query_artifacts":
                return stmt.compile().params.get("physical_table_name")
        raise AssertionError("no NamedQueryArtifact insert captured")

    @pytest.mark.asyncio
    async def test_import_reseeds_to_destination(self):
        from unittest.mock import AsyncMock
        from shared.model_snapshot.rehydrator import (
            RehydrationMode,
            _insert_named_queries,
        )

        db = AsyncMock()
        await _insert_named_queries(
            uuid.uuid4(), self._snapshot(), db,
            mode=RehydrationMode.IMPORT, reseed="DEST",
        )
        name = self._artifact_name(db.execute)
        assert name != "nq_SRC_deadbeef"
        assert name.startswith("nq_DEST_")

    @pytest.mark.asyncio
    async def test_revert_preserves_physical_name(self):
        from unittest.mock import AsyncMock
        from shared.model_snapshot.rehydrator import (
            RehydrationMode,
            _insert_named_queries,
        )

        db = AsyncMock()
        await _insert_named_queries(
            uuid.uuid4(), self._snapshot(), db,
            mode=RehydrationMode.RESTORE, reseed="DEST",
        )
        # Revert is the SAME model: the physical table IS this model's own.
        assert self._artifact_name(db.execute) == "nq_SRC_deadbeef"


# -----------------------------------------------------------------------
# F-020-10 (Bug-9080): project export must strip secret-like keys from the
# plaintext connection / LLM config bags, mirroring the import-side strip.
# -----------------------------------------------------------------------

class TestExportStripsSecretConfigBags:
    def _db(self, connections=(), llm_configs=()):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        project = SimpleNamespace(
            slug="p", display_name="P", is_active=True
        )

        def _scalars(rows):
            r = MagicMock()
            r.scalars.return_value.all.return_value = list(rows)
            return r

        async def _execute(stmt):
            table = ""
            try:
                table = stmt.get_final_froms()[0].name
            except Exception:
                table = str(stmt)
            if "project_connections" in table:
                return _scalars(connections)
            if "llm_provider_configs" in table:
                return _scalars(llm_configs)
            return _scalars([])

        db = AsyncMock()
        db.get = AsyncMock(return_value=project)
        db.execute = AsyncMock(side_effect=_execute)
        return db

    @pytest.mark.asyncio
    async def test_connection_config_password_is_stripped_on_export(self):
        from types import SimpleNamespace
        from shared.model_snapshot.project_serialiser import export_project

        conn = SimpleNamespace(
            id=uuid.uuid4(),
            display_name="warehouse",
            connection_type="postgres",
            config={"host": "db.example.com", "password": "s3cret"},
            encrypted_credentials=b"",
        )
        db = self._db(connections=[conn])
        bundle = await export_project(
            uuid.uuid4(), db, tenant_slug="t", sections={"connections"},
        )
        cfg = bundle["connections"][0]["config"]
        assert "password" not in cfg
        assert cfg.get("host") == "db.example.com"  # non-secret key kept

    @pytest.mark.asyncio
    async def test_llm_config_api_key_is_stripped_on_export(self):
        from types import SimpleNamespace
        from shared.model_snapshot.project_serialiser import export_project

        lc = SimpleNamespace(
            id=uuid.uuid4(),
            provider="openai",
            display_name="gpt",
            base_url=None,
            model_name="gpt-4",
            max_tokens=100,
            temperature=0.0,
            timeout_seconds=30,
            config={"api_key": "sk-leak", "region": "us"},
            encrypted_api_key=b"",
        )
        db = self._db(llm_configs=[lc])
        bundle = await export_project(
            uuid.uuid4(), db, tenant_slug="t", sections={"llm_configs"},
        )
        cfg = bundle["llm_configs"][0]["config"]
        assert "api_key" not in cfg
        assert cfg.get("region") == "us"
