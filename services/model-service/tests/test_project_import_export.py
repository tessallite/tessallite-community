"""
Tests for project import/export endpoints and critical import invariants.

Spec Section 12 — required test coverage:
  12.1  Replace-mode rollback: failed import must leave existing project intact
        (verified by asserting rollback() is called on the DB session).
  12.2  Imported model with a prior deployed_version_id has it cleared to NULL;
        the model slug appears in `models_requiring_deploy`.
  12.3  All AggregateDefinition and PocketDefinition rows imported with
        force_aggregate_pending=True and force_pocket_stale=True respectively.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import httpx
from src.main import app
from src.auth.middleware import CurrentUser, get_current_user
from shared.model_snapshot.project_rehydrator import (
    PROJECT_EXPORT_FORMAT,
    ProjectImportError,
    import_project,
    plan_project_import,
)
from tests.conftest import TEST_TENANT, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit

PREFIX = "/api/v1/projects"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client_admin(mock_rbac_get_tenant_db):  # noqa: F811
    """AsyncClient whose current_user has role='tenant_admin'."""
    admin = CurrentUser(
        user_id="admin@test.com",
        tenant_id=TEST_TENANT,
        email="admin@test.com",
        role="tenant_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Bundle factory
# ---------------------------------------------------------------------------

def _bundle(models: list | None = None, **overrides) -> dict:
    base = {
        "schema_version": 1,
        "export_format": PROJECT_EXPORT_FORMAT,
        "exported_at": "2026-05-01T00:00:00Z",
        "exported_from": {"tenant_slug": "src", "project_id": str(uuid.uuid4())},
        "credentials_included": False,
        "credentials_envelope": None,
        "included_sections": [],
        "project": {"slug": "imported", "display_name": "Imported", "is_active": True},
        "models": models if models is not None else [],
        "test_metadata": None,
    }
    base.update(overrides)
    return base


def _model_snap(
    slug: str = "mymodel",
    exported_dvid: str | None = None,
    has_aggregates: bool = False,
    has_pockets: bool = False,
) -> dict:
    return {
        "schema_version": 2,
        "model": {
            "id": str(uuid.uuid4()),
            "slug": slug,
            "display_name": slug,
            "seed": "abc123",
        },
        "exported_deployed_version_id": exported_dvid,
        "model_versions": [],
        "aggregates": [{"id": str(uuid.uuid4()), "status": "active"}] if has_aggregates else [],
        "pockets": [{"id": str(uuid.uuid4()), "status": "fresh"}] if has_pockets else [],
    }


def _import_db_mock() -> AsyncMock:
    """DB mock suitable for import_project() create mode with no sections."""
    db = make_mock_db()
    no_row = MagicMock()
    no_row.scalar_one_or_none.return_value = None
    no_row.scalars.return_value.all.return_value = []
    no_row.all.return_value = []
    db.execute = AsyncMock(return_value=no_row)
    return db


# ---------------------------------------------------------------------------
# 12.1  Replace-mode rollback on failure
# ---------------------------------------------------------------------------

class TestReplaceRollbackOnFailure:
    @pytest.mark.asyncio
    async def test_failed_import_calls_rollback(self, client_admin):
        """When import_project raises ProjectImportError the endpoint must
        call db.rollback() and return HTTP 422."""
        mock_db = _import_db_mock()

        with (
            patch(
                "src.api.project_import_export.get_tenant_db",
                async_gen_from(mock_db),
            ),
            patch(
                "src.api.project_import_export.import_project",
                side_effect=ProjectImportError("simulated failure"),
            ),
        ):
            resp = await client_admin.post(
                f"{PREFIX}/import",
                json={"bundle": _bundle(), "mode": "create"},
            )

        assert resp.status_code == 422
        assert "simulated failure" in resp.json()["detail"]
        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_import_does_not_commit(self, client_admin):
        """Rollback path must NOT call commit."""
        mock_db = _import_db_mock()

        with (
            patch(
                "src.api.project_import_export.get_tenant_db",
                async_gen_from(mock_db),
            ),
            patch(
                "src.api.project_import_export.import_project",
                side_effect=ProjectImportError("simulated failure"),
            ),
        ):
            await client_admin.post(
                f"{PREFIX}/import",
                json={"bundle": _bundle(), "mode": "create"},
            )

        mock_db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 12.2  Imported model deployed_version_id must be NULL / listed for redeploy
# ---------------------------------------------------------------------------

class TestDeployedVersionIdClearedOnImport:
    @pytest.mark.asyncio
    async def test_model_with_deployed_version_appears_in_requiring_deploy(self):
        """A model that had deployed_version_id set in the export bundle must
        appear in models_requiring_deploy in the import response."""
        old_dvid = str(uuid.uuid4())
        bundle = _bundle(models=[_model_snap(slug="modelx", exported_dvid=old_dvid)])
        db = _import_db_mock()

        with (
            patch(
                "shared.model_snapshot.project_rehydrator.prepare_snapshot_for_import",
                return_value=(bundle["models"][0], set()),
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.rehydrate_into_live",
                new_callable=AsyncMock,
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.insert_model_versions",
                new_callable=AsyncMock,
            ),
        ):
            result = await import_project(bundle, db, mode="create")

        assert "modelx" in result["models_requiring_deploy"]
        assert "redeploy_required" in result["post_import_actions"]

    @pytest.mark.asyncio
    async def test_model_without_deployed_version_not_in_requiring_deploy(self):
        """A model that had no deployed_version_id is not listed for redeploy."""
        bundle = _bundle(models=[_model_snap(slug="modely", exported_dvid=None)])
        db = _import_db_mock()

        with (
            patch(
                "shared.model_snapshot.project_rehydrator.prepare_snapshot_for_import",
                return_value=(bundle["models"][0], set()),
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.rehydrate_into_live",
                new_callable=AsyncMock,
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.insert_model_versions",
                new_callable=AsyncMock,
            ),
        ):
            result = await import_project(bundle, db, mode="create")

        assert "modely" not in result["models_requiring_deploy"]
        assert "redeploy_required" not in result["post_import_actions"]


# ---------------------------------------------------------------------------
# 12.3  Aggregate / pocket status set to pending / stale on import
# ---------------------------------------------------------------------------

class TestAggregateAndPocketStatusAfterImport:
    @pytest.mark.asyncio
    async def test_rehydrate_called_with_force_aggregate_pending(self):
        """rehydrate_into_live must receive force_aggregate_pending=True."""
        bundle = _bundle(models=[_model_snap(slug="m1", has_aggregates=True)])
        db = _import_db_mock()

        mock_rehydrate = AsyncMock()
        with (
            patch(
                "shared.model_snapshot.project_rehydrator.prepare_snapshot_for_import",
                return_value=(bundle["models"][0], set()),
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.rehydrate_into_live",
                mock_rehydrate,
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.insert_model_versions",
                new_callable=AsyncMock,
            ),
        ):
            await import_project(bundle, db, mode="create")

        mock_rehydrate.assert_awaited_once()
        _, kwargs = mock_rehydrate.call_args
        assert kwargs.get("force_aggregate_pending") is True

    @pytest.mark.asyncio
    async def test_rehydrate_called_with_force_pocket_stale(self):
        """rehydrate_into_live must receive force_pocket_stale=True."""
        bundle = _bundle(models=[_model_snap(slug="m2", has_pockets=True)])
        db = _import_db_mock()

        mock_rehydrate = AsyncMock()
        with (
            patch(
                "shared.model_snapshot.project_rehydrator.prepare_snapshot_for_import",
                return_value=(bundle["models"][0], set()),
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.rehydrate_into_live",
                mock_rehydrate,
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.insert_model_versions",
                new_callable=AsyncMock,
            ),
        ):
            await import_project(bundle, db, mode="create")

        mock_rehydrate.assert_awaited_once()
        _, kwargs = mock_rehydrate.call_args
        assert kwargs.get("force_pocket_stale") is True

    @pytest.mark.asyncio
    async def test_post_import_actions_includes_refresh_aggregates(self):
        """When the bundle has aggregates or pockets, refresh_aggregates must
        appear in post_import_actions."""
        bundle = _bundle(
            models=[_model_snap(slug="m3", has_aggregates=True, has_pockets=True)]
        )
        db = _import_db_mock()

        with (
            patch(
                "shared.model_snapshot.project_rehydrator.prepare_snapshot_for_import",
                return_value=(bundle["models"][0], set()),
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.rehydrate_into_live",
                new_callable=AsyncMock,
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.insert_model_versions",
                new_callable=AsyncMock,
            ),
        ):
            result = await import_project(bundle, db, mode="create")

        assert "refresh_aggregates" in result["post_import_actions"]

    @pytest.mark.asyncio
    async def test_no_refresh_action_when_no_aggregates_or_pockets(self):
        """refresh_aggregates must NOT appear when the bundle has no agg/pocket rows."""
        bundle = _bundle(models=[_model_snap(slug="m4")])
        db = _import_db_mock()

        with (
            patch(
                "shared.model_snapshot.project_rehydrator.prepare_snapshot_for_import",
                return_value=(bundle["models"][0], set()),
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.rehydrate_into_live",
                new_callable=AsyncMock,
            ),
            patch(
                "shared.model_snapshot.project_rehydrator.insert_model_versions",
                new_callable=AsyncMock,
            ),
        ):
            result = await import_project(bundle, db, mode="create")

        assert "refresh_aggregates" not in result["post_import_actions"]


# ---------------------------------------------------------------------------
# F-020-01  Replace-mode connection remap order (delete-before-remap bug)
# ---------------------------------------------------------------------------

class _ConnRow:
    """Minimal stand-in for a ProjectConnection ORM row."""

    def __init__(self, cid: str, display_name: str, connection_type: str):
        self.id = cid
        self.display_name = display_name
        self.connection_type = connection_type


def _table_name_of(stmt) -> str | None:
    """Best-effort table name for a select/delete statement."""
    try:
        # delete() statements expose .table
        if getattr(stmt, "is_delete", False):
            return stmt.table.name
        # select() — first FROM entity
        froms = getattr(stmt, "get_final_froms", None)
        if froms:
            fl = froms()
            if fl:
                return getattr(fl[0], "name", None)
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def _delete_in_ids(stmt) -> set[str] | None:
    """Extract the id values of a ``WHERE id IN (...)`` delete.

    Returns None when the delete has no id-IN clause (the clean-slate
    delete-by-project_id path), in which case the caller deletes all.
    """
    try:
        compiled = stmt.compile(compile_kwargs={"literal_binds": False})
        params = compiled.params
        # The orphan prune binds the id list; the clean-slate delete binds
        # project_id only. Distinguish by parameter name prefix.
        in_vals: set[str] = set()
        for k, v in params.items():
            if not k.startswith("id_"):
                continue
            if isinstance(v, (list, tuple, set)):
                in_vals.update(str(x) for x in v)
            else:
                in_vals.add(str(v))
        if in_vals:
            return in_vals
    except Exception:  # pragma: no cover - defensive
        pass
    return None


class _ReplaceModeDB:
    """Stateful AsyncSession mock that simulates the connection FK lifecycle.

    Tracks which ProjectConnection ids still exist and records the ordered
    log of (op, table) so the test can assert that connection resolution
    happens against live pre-existing rows and that the connection delete
    is an orphan-only prune that runs AFTER resolution.
    """

    def __init__(self, project, conns: list[_ConnRow]):
        self._project = project
        self._live_conn_ids = {c.id for c in conns}
        self._conns = conns
        self.ops: list[tuple[str, str | None]] = []
        self.added = []
        self.info = {"tenant_id": TEST_TENANT}
        self.committed = False
        self.rolled_back = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def execute(self, stmt, *args, **kwargs):
        table = _table_name_of(stmt)
        is_delete = bool(getattr(stmt, "is_delete", False))
        self.ops.append(("delete" if is_delete else "select", table))

        if is_delete:
            if table == "project_connections":
                # Remove only the ids named by the IN clause (orphan prune).
                # If the WHERE has no id IN list (the no-connections clean
                # slate path), delete all.
                target_ids = _delete_in_ids(stmt)
                if target_ids is None:
                    self._live_conn_ids.clear()
                else:
                    for cid in target_ids:
                        self._live_conn_ids.discard(cid)
            return MagicMock()

        # select():
        result = MagicMock()
        if table == "projects":
            result.scalar_one_or_none.return_value = self._project
        elif table == "project_connections":
            live = [c for c in self._conns if c.id in self._live_conn_ids]
            result.scalars.return_value.all.return_value = live
            result.scalar_one_or_none.return_value = None
        else:
            result.scalar_one_or_none.return_value = None
            result.scalars.return_value.all.return_value = []
        return result


class TestReplaceModeConnectionRemapOrder:
    """F-020-01: replace mode must resolve/retain connections BEFORE the
    orphan delete, so mapped/matched connection ids survive for the new
    DataSource FK."""

    def _replace_bundle(self, conns: list[dict]) -> dict:
        return _bundle(
            included_sections=["connections"],
            connections=conns,
            models=[],
        )

    @pytest.mark.asyncio
    async def test_mapped_connection_survives_replace(self):
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        keep_id = str(uuid.uuid4())
        orphan_id = str(uuid.uuid4())
        keep = _ConnRow(keep_id, "Prod WH", "postgresql")
        orphan = _ConnRow(orphan_id, "Old WH", "postgresql")
        db = _ReplaceModeDB(target, [keep, orphan])

        bundle = self._replace_bundle(
            [{"id": "exp-1", "display_name": "Prod WH", "connection_type": "postgresql"}]
        )

        result = await import_project(
            bundle,
            db,
            mode="replace",
            connection_mapping={"exp-1": keep_id},
        )

        # The mapped target connection must still be live (not deleted).
        assert keep_id in db._live_conn_ids
        # The unreferenced connection must have been pruned.
        assert orphan_id not in db._live_conn_ids
        # The export id remaps to the retained target connection.
        assert result["id_map"]["connections"]["exp-1"] == keep_id

    @pytest.mark.asyncio
    async def test_resolution_reads_live_connections_before_delete(self):
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        match_id = str(uuid.uuid4())
        match = _ConnRow(match_id, "Prod WH", "postgresql")
        db = _ReplaceModeDB(target, [match])

        bundle = self._replace_bundle(
            [{"id": "exp-1", "display_name": "Prod WH", "connection_type": "postgresql"}]
        )

        result = await import_project(bundle, db, mode="replace")

        # Auto-match by (display_name, connection_type) only works if the
        # SELECT ran while the connection was still live.
        assert result["id_map"]["connections"]["exp-1"] == match_id
        assert match_id in db._live_conn_ids

        # Ordering invariant: the connection SELECT precedes any
        # project_connections DELETE.
        select_idx = next(
            i for i, (op, t) in enumerate(db.ops)
            if op == "select" and t == "project_connections"
        )
        conn_deletes = [
            i for i, (op, t) in enumerate(db.ops)
            if op == "delete" and t == "project_connections"
        ]
        # A matched connection is retained -> no orphan delete fires here.
        assert conn_deletes == [] or min(conn_deletes) > select_idx

    @pytest.mark.asyncio
    async def test_unmapped_connection_without_match_raises(self):
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        db = _ReplaceModeDB(target, [])

        bundle = self._replace_bundle(
            [{"id": "exp-1", "display_name": "Ghost", "connection_type": "postgresql"}]
        )

        with pytest.raises(ProjectImportError):
            await import_project(bundle, db, mode="replace")


class _DeleteRecordingDB:
    """Records the tables touched by DELETE statements in replace mode."""

    def __init__(self, project):
        self._project = project
        self.deleted_tables: list[str] = []
        self.info = {"tenant_id": TEST_TENANT}

    def add(self, obj):
        pass

    async def flush(self):
        return None

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def execute(self, stmt, *args, **kwargs):
        table = _table_name_of(stmt)
        if bool(getattr(stmt, "is_delete", False)):
            self.deleted_tables.append(table)
            return MagicMock()
        result = MagicMock()
        if table == "projects":
            result.scalar_one_or_none.return_value = self._project
        else:
            result.scalar_one_or_none.return_value = None
            result.scalars.return_value.all.return_value = []
            result.all.return_value = []
        return result


class TestReplaceModeSectionScopedDeletion:
    """F-020-05/06: replace deletes only what the bundle carries, and uses the
    programmatic cascade for models."""

    @pytest.mark.asyncio
    async def test_omitted_sections_are_not_deleted(self):
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        db = _DeleteRecordingDB(target)
        # Bundle with NO included_sections (the standard export omits
        # access_bindings; here we omit everything).
        bundle = _bundle(included_sections=[], models=[])

        await import_project(bundle, db, mode="replace")

        # F-020-05: access bindings, agent conversations, settings, etc. must
        # NOT be wiped when their section is absent.
        assert "user_access_bindings" not in db.deleted_tables
        assert "agent_conversations" not in db.deleted_tables
        assert "project_settings" not in db.deleted_tables
        assert "llm_provider_configs" not in db.deleted_tables

    @pytest.mark.asyncio
    async def test_present_sections_are_deleted(self):
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        db = _DeleteRecordingDB(target)
        bundle = _bundle(
            included_sections=["access_bindings", "project_settings"],
            access_bindings=[],
            project_settings=[],
            models=[],
        )

        await import_project(bundle, db, mode="replace")

        assert "user_access_bindings" in db.deleted_tables
        assert "project_settings" in db.deleted_tables

    @pytest.mark.asyncio
    async def test_models_deleted_via_programmatic_cascade(self):
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        mid = uuid.uuid4()

        class _DB(_DeleteRecordingDB):
            async def execute(self, stmt, *a, **k):
                table = _table_name_of(stmt)
                if not getattr(stmt, "is_delete", False) and table == "models":
                    result = MagicMock()
                    result.all.return_value = [(mid,)]
                    result.scalars.return_value.all.return_value = []
                    result.scalar_one_or_none.return_value = None
                    return result
                return await super().execute(stmt, *a, **k)

        db = _DB(target)
        bundle = _bundle(included_sections=[], models=[])

        with patch(
            "shared.model_snapshot.project_rehydrator.delete_model_cascade",
            new=AsyncMock(return_value=[]),
        ) as cascade:
            await import_project(bundle, db, mode="replace")

        # F-020-06: the programmatic cascade must be used (not a raw
        # delete(Model)). The "models" table is therefore never named in a raw
        # DELETE issued by the rehydrator itself.
        cascade.assert_awaited_once()
        assert cascade.await_args.args[1] == mid
        assert "models" not in db.deleted_tables

    @pytest.mark.asyncio
    async def test_cascade_error_aborts_replace(self):
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        mid = uuid.uuid4()

        class _DB(_DeleteRecordingDB):
            async def execute(self, stmt, *a, **k):
                table = _table_name_of(stmt)
                if not getattr(stmt, "is_delete", False) and table == "models":
                    result = MagicMock()
                    result.all.return_value = [(mid,)]
                    result.scalars.return_value.all.return_value = []
                    result.scalar_one_or_none.return_value = None
                    return result
                return await super().execute(stmt, *a, **k)

        db = _DB(target)
        bundle = _bundle(included_sections=[], models=[])

        with patch(
            "shared.model_snapshot.project_rehydrator.delete_model_cascade",
            new=AsyncMock(return_value=["measures: boom"]),
        ):
            with pytest.raises(ProjectImportError):
                await import_project(bundle, db, mode="replace")


# ---------------------------------------------------------------------------
# F-020-E3  Replace-mode dry-run plan
# ---------------------------------------------------------------------------

class TestProjectImportDryRun:
    """The dry-run plan must describe what a real import would delete/create
    without touching the tenant DB, and must honour the section-scoped delete
    safety (F-020-05) so the plan matches actual behaviour."""

    @pytest.mark.asyncio
    async def test_dry_run_endpoint_returns_plan_without_import_or_commit(
        self, client_admin
    ):
        mock_db = _import_db_mock()
        plan = {
            "mode": "replace",
            "target_project_id": str(uuid.uuid4()),
            "target_project_slug": "imported",
            "target_project_display_name": "Imported",
            "target_project_exists": True,
            "will_create_project": False,
            "will_replace_project": True,
            "delete_counts": {"models": 2, "orphan_connections": 1},
            "incoming_counts": {"models": 1},
            "connection_actions": [],
            "model_slugs": ["mymodel"],
            "post_import_actions": ["redeploy_required"],
            "warnings": ["dry-run note"],
        }
        mock_plan = AsyncMock(return_value=plan)
        mock_import = AsyncMock()

        with (
            patch(
                "src.api.project_import_export.get_tenant_db",
                async_gen_from(mock_db),
            ),
            patch(
                "src.api.project_import_export.plan_project_import",
                mock_plan,
            ),
            patch("src.api.project_import_export.import_project", mock_import),
            patch(
                "src.api.project_import_export.get_credential_fernet",
                side_effect=AssertionError("dry_run must not load Fernet"),
            ),
        ):
            resp = await client_admin.post(
                f"{PREFIX}/import",
                json={
                    "bundle": _bundle(credentials_included=True),
                    "mode": "replace",
                    "dry_run": True,
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["models_imported"] == 0
        assert body["plan"]["delete_counts"] == {"models": 2, "orphan_connections": 1}
        assert body["warnings"] == ["dry-run note"]
        mock_plan.assert_awaited_once()
        mock_import.assert_not_awaited()
        mock_db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_mode_plan_has_no_deletions(self):
        bundle = _bundle(
            models=[_model_snap(slug="orders")],
        )
        db = _import_db_mock()  # project lookup returns None → create path

        plan = await plan_project_import(bundle, db, mode="create")

        assert plan["will_create_project"] is True
        assert plan["will_replace_project"] is False
        assert plan["target_project_exists"] is False
        assert plan["delete_counts"] == {}
        assert plan["incoming_counts"]["models"] == 1
        assert plan["model_slugs"] == ["orders"]

    @pytest.mark.asyncio
    async def test_replace_plan_delete_counts_are_section_scoped(self):
        """delete_counts must contain only models (always) plus the sections
        actually present in the bundle — mirroring import_project's
        section-scoped deletion (F-020-05). Sections absent from the bundle
        must NOT appear, because the real import would not delete them."""
        project_id = uuid.uuid4()
        bundle = _bundle(
            included_sections=["llm_configs"],
            llm_configs=[{"id": str(uuid.uuid4())}],
            models=[
                _model_snap(
                    slug="orders",
                    exported_dvid=str(uuid.uuid4()),
                    has_aggregates=True,
                )
            ],
        )
        db = _import_db_mock()
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = SimpleNamespace(
            id=project_id, slug="imported"
        )
        db.execute = AsyncMock(return_value=project_result)

        # _count_rows is called once for models, once per included deletable
        # section (llm_configs), and once for the clean-slate connection count
        # (no connections section). That is 3 calls: models=2, llm_configs=1,
        # orphan_connections=0.
        with patch(
            "shared.model_snapshot.project_rehydrator._count_rows",
            AsyncMock(side_effect=[2, 1, 0]),
        ):
            plan = await plan_project_import(bundle, db, mode="replace")

        assert plan["will_replace_project"] is True
        # Models always deleted; llm_configs deleted because its section is in
        # the bundle. No connections section → no orphan_connections key beyond
        # the replace clean-slate count (0 here, no connections present).
        assert plan["delete_counts"]["models"] == 2
        assert plan["delete_counts"]["llm_configs"] == 1
        assert "agent_configs" not in plan["delete_counts"]
        assert "access_bindings" not in plan["delete_counts"]
        assert "project_settings" not in plan["delete_counts"]
        # No connections section: clean-slate connection delete count is 0.
        assert plan["delete_counts"]["orphan_connections"] == 0
        # post_import_actions reflect the bundle (deployed version + aggregates).
        assert plan["post_import_actions"] == [
            "redeploy_required",
            "refresh_aggregates",
        ]

    @pytest.mark.asyncio
    async def test_replace_plan_missing_project_raises(self):
        bundle = _bundle(models=[_model_snap()])
        db = _import_db_mock()  # project lookup returns None

        with pytest.raises(ProjectImportError):
            await plan_project_import(bundle, db, mode="replace")
