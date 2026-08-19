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
from src.auth.middleware import CurrentServiceUser, CurrentUser, get_current_user
from shared.auth.service_principal import SCOPE_AGGREGATE_REBUILD
from shared.model_snapshot.project_rehydrator import (
    PROJECT_EXPORT_FORMAT,
    ProjectImportError,
    _prepare_cross_model_recipes_for_import,
    _referenced_export_connection_ids,
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
# Bug-8480 / F-021-04 (decision #9) — project export secret-egress is closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_export_denies_signed_in_non_admin_zero_binding_tenant():
    """Bug-8480: a signed-in NON-admin in a ZERO-binding tenant must be DENIED
    project export — the live secret-egress path. F-021-04 removed the
    zero-binding bootstrap-admin grant, so require_role('admin') denies with no
    binding, WITH and WITHOUT include_credentials.
    """
    member = CurrentUser(
        user_id="member@test.com",
        tenant_id=TEST_TENANT,
        email="member@test.com",
        # Coarse local role only; NOT a human tenant/system admin, and holds no
        # binding on the zero-binding project.
        role="member",
    )

    # require_role's binding lookup finds nothing on a zero-binding project.
    rbac_db = AsyncMock()
    no_binding = MagicMock()
    no_binding.scalar_one_or_none.return_value = None
    no_binding.first.return_value = None
    rbac_db.execute = AsyncMock(return_value=no_binding)

    project_id = uuid.uuid4()

    app.dependency_overrides[get_current_user] = lambda: member
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.auth.rbac.get_tenant_db", async_gen_from(rbac_db)):
                without_creds = await ac.post(
                    f"{PREFIX}/{project_id}/export", json={}
                )
                with_creds = await ac.post(
                    f"{PREFIX}/{project_id}/export",
                    json={
                        "include_credentials": True,
                        "passphrase": "correct-horse-battery",
                    },
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert without_creds.status_code == 403, without_creds.text
    assert with_creds.status_code == 403, with_creds.text


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


def _recipe_import_bundle(*, second_model_id: str | None = None) -> dict:
    first = _model_snap("sales")
    second = _model_snap("units")
    if second_model_id is not None:
        second["model"]["id"] = second_model_id
    first["measures"] = [{"id": str(uuid.uuid4()), "name": "Revenue"}]
    second["measures"] = [{"id": str(uuid.uuid4()), "name": "Units"}]
    recipe_id = str(uuid.uuid4())
    return _bundle(
        models=[first, second],
        included_sections=["cross_model_recipes"],
        cross_model_recipes=[
            {
                "id": recipe_id,
                "name": "Revenue per unit",
                "parameters": [],
                "steps": [
                    {
                        "name": "sales",
                        "model_id": first["model"]["id"],
                        "measures": ["Revenue"],
                    },
                    {
                        "name": "units",
                        "model_id": second["model"]["id"],
                        "measures": ["Units"],
                    },
                ],
                "combine": {
                    "op": "div",
                    "args": [
                        {"ref": {"step": "sales", "measure": "Revenue"}},
                        {"ref": {"step": "units", "measure": "Units"}},
                    ],
                },
            }
        ],
    )


class TestRecipeImportPreflight8096:
    def test_deep_copy_remaps_both_step_model_ids_without_mutating_bundle(self):
        bundle = _recipe_import_bundle()
        original_steps = [dict(step) for step in bundle["cross_model_recipes"][0]["steps"]]
        old_ids = [step["model_id"] for step in original_steps]
        remap = {old_id: str(uuid.uuid4()) for old_id in old_ids}

        rewritten = _prepare_cross_model_recipes_for_import(bundle, remap)

        assert [step["model_id"] for step in rewritten[0]["steps"]] == [
            remap[old_ids[0]],
            remap[old_ids[1]],
        ]
        assert bundle["cross_model_recipes"][0]["steps"] == original_steps

    @pytest.mark.parametrize("invalid_name", [None, "", 7])
    def test_invalid_step_name_fails_even_without_combine(self, invalid_name):
        bundle = _recipe_import_bundle()
        recipe = bundle["cross_model_recipes"][0]
        recipe["steps"][0]["name"] = invalid_name
        recipe["combine"] = None
        remap = {
            step["model_id"]: str(uuid.uuid4()) for step in recipe["steps"]
        }

        with pytest.raises(
            ProjectImportError,
            match=rf"cross_model_recipe:{recipe['id']}\$\.steps\[0\]\.name",
        ):
            _prepare_cross_model_recipes_for_import(bundle, remap)

    def test_case_insensitive_duplicate_step_names_fail_without_combine(self):
        bundle = _recipe_import_bundle()
        recipe = bundle["cross_model_recipes"][0]
        recipe["steps"][1]["name"] = "SALES"
        recipe["combine"] = None
        remap = {
            step["model_id"]: str(uuid.uuid4()) for step in recipe["steps"]
        }

        with pytest.raises(
            ProjectImportError,
            match=rf"cross_model_recipe:{recipe['id']}\$\.steps\[1\]\.name duplicates",
        ):
            _prepare_cross_model_recipes_for_import(bundle, remap)

    @pytest.mark.asyncio
    async def test_unmapped_recipe_model_fails_before_any_project_mutation(self):
        bundle = _recipe_import_bundle(second_model_id=str(uuid.uuid4()))
        # Remove the second model snapshot while retaining its recipe reference.
        bundle["models"] = bundle["models"][:1]
        recipe_id = bundle["cross_model_recipes"][0]["id"]
        db = _import_db_mock()

        with pytest.raises(
            ProjectImportError,
            match=rf"cross_model_recipe:{recipe_id}.*steps\[1\]\.model_id is unmapped",
        ):
            await import_project(bundle, db, mode="create")

        db.add.assert_not_called()
        db.flush.assert_not_awaited()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_combine_fails_at_exact_path_before_mutation(self):
        bundle = _recipe_import_bundle()
        recipe = bundle["cross_model_recipes"][0]
        recipe["combine"]["args"][1] = {
            "ref": {"step": "units", "measure": ["Units"]}
        }
        db = _import_db_mock()

        with pytest.raises(
            ProjectImportError,
            match=(
                rf"cross_model_recipe:{recipe['id']}"
                r"\$\.combine\.args\[1\]\.ref\.measure"
            ),
        ):
            await import_project(bundle, db, mode="create")

        db.add.assert_not_called()
        db.flush.assert_not_awaited()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["create", "replace"])
    @pytest.mark.parametrize("op_value", [[], {}], ids=["array-op", "object-op"])
    async def test_malformed_operator_fails_before_create_or_replace_mutation(
        self, mode, op_value
    ):
        bundle = _recipe_import_bundle()
        recipe = bundle["cross_model_recipes"][0]
        recipe["combine"]["op"] = op_value
        db = _import_db_mock()

        with pytest.raises(
            ProjectImportError,
            match=rf"cross_model_recipe:{recipe['id']}\$\.combine\.op",
        ):
            await import_project(bundle, db, mode=mode)

        db.add.assert_not_called()
        db.flush.assert_not_awaited()
        db.execute.assert_not_awaited()


class TestBug8134OneFactTableFailsBeforeStaging:
    """Bug-8134: import_project must reject a two-fact-table model snapshot
    via ``_validate_bundle`` -- its very first call -- before a single row
    is staged.

    Pre-fix there was no one-fact-per-model check anywhere on the import
    path: the bundle sailed past every guard, a Project row and a Model row
    were created (``db.add`` + flush), and ``rehydrate_into_live`` was
    reached and handed the two-fact snapshot -- which would go on to reach
    ``_insert_tables_and_columns`` (shared/model_snapshot/rehydrator.py),
    where the second fact-typed row's per-row Core INSERT trips the DB's
    partial unique index (``uq_model_tables_one_fact_per_model``, migration
    0136), a raw IntegrityError surfacing as an opaque 500 -- not the clean
    422 a malformed bundle deserves. ``rehydrate_into_live`` is patched out
    here (as the sibling tests in ``TestAggregateAndPocketStatusAfterImport``
    already do) so the proof isolates cleanly on "did staging happen before
    the fact-table shape was ever checked", independent of what a live
    partial unique index or ``rehydrate_into_live``'s own internals would
    do with a bare mock DB.
    """

    @staticmethod
    def _two_fact_model_snap() -> dict:
        snap = _model_snap("twofact")
        snap["tables"] = [
            {"id": str(uuid.uuid4()), "physical_name": "orders", "table_type": "fact"},
            {"id": str(uuid.uuid4()), "physical_name": "shipments", "table_type": "fact"},
        ]
        snap["data_sources"] = []
        return snap

    @pytest.mark.asyncio
    async def test_two_fact_tables_raises_before_any_db_mutation(self):
        bundle = _bundle(models=[self._two_fact_model_snap()])
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
            with pytest.raises(ProjectImportError, match="fact table"):
                await import_project(bundle, db, mode="create")

        # The fix rejects the bundle in _validate_bundle, before mode
        # dispatch even runs -- so neither the Project row nor the Model
        # row (let alone any ModelTable row) is ever staged, and
        # rehydrate_into_live -- the function whose internal ModelTable
        # insert loop is what the DB's partial unique index would have
        # caught -- is never even reached.
        db.add.assert_not_called()
        db.flush.assert_not_awaited()
        db.execute.assert_not_awaited()
        mock_rehydrate.assert_not_awaited()


# ---------------------------------------------------------------------------
# 12.1  Replace-mode rollback on failure
# ---------------------------------------------------------------------------

class TestReplaceRollbackOnFailure:
    @pytest.mark.asyncio
    async def test_bug_8140_late_replace_failure_before_commit_preserves_metadata_and_physical_objects(
        self, client_admin
    ):
        """A failure after deletion scheduling but before commit rolls the
        metadata/outbox transaction back and never reaches target DDL."""
        mock_db = _import_db_mock()
        result = {
            "project_id": str(uuid.uuid4()),
            "project_slug": "imported",
            "id_map": {
                "models": {}, "connections": {}, "personas": {},
                "llm_configs": {}, "judge_rubrics": {},
            },
            "models_imported": 0,
            "models_requiring_deploy": [],
            "post_import_actions": [],
            "warnings": [],
        }
        post_commit_cleanup = AsyncMock()
        cleanup_task_id = uuid.uuid4()
        staged_cleanup_task = SimpleNamespace(id=cleanup_task_id, status="pending")
        mock_db.info = {}

        async def import_after_scheduling_cleanup(*_args, **_kwargs):
            mock_db.add(staged_cleanup_task)
            mock_db.info.setdefault("physical_cleanup_task_ids", []).append(
                cleanup_task_id
            )
            return result

        with (
            patch(
                "src.api.project_import_export.get_tenant_db",
                async_gen_from(mock_db),
            ),
            patch(
                "src.api.project_import_export.import_project",
                new=AsyncMock(side_effect=import_after_scheduling_cleanup),
            ),
            patch(
                "src.api.project_import_export.audit_required",
                new=AsyncMock(side_effect=RuntimeError("late audit failure")),
            ),
            patch(
                "src.api.project_import_export.attempt_scheduled_physical_cleanup",
                post_commit_cleanup,
            ),
        ):
            with pytest.raises(RuntimeError, match="late audit failure"):
                await client_admin.post(
                    f"{PREFIX}/import",
                    json={
                        "bundle": _bundle(),
                        "mode": "replace",
                        "project_slug": "imported",
                    },
                )

        mock_db.rollback.assert_awaited_once()
        mock_db.commit.assert_not_awaited()
        mock_db.add.assert_called_once_with(staged_cleanup_task)
        assert mock_db.info["physical_cleanup_task_ids"] == [cleanup_task_id]
        post_commit_cleanup.assert_not_awaited()

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

    @pytest.mark.asyncio
    async def test_slug_valueerror_midimport_leaves_no_partial_state(
        self, client_admin
    ):
        """Bug-7291 (NOT-A-BUG lock): a slug ValueError raised mid-import (the
        DeepSeek CF-020-DS-F02008 scenario — a connection-naming/slug mismatch
        failing after inserts begin) must NOT leave partial state. The endpoint
        catches ValueError, rolls back, returns 422, and never commits, so no
        models from an earlier loop iteration are persisted.
        """
        mock_db = _import_db_mock()

        with (
            patch(
                "src.api.project_import_export.get_tenant_db",
                async_gen_from(mock_db),
            ),
            patch(
                "src.api.project_import_export.import_project",
                side_effect=ValueError(
                    "Model slug '1bad' is not BI-safe."
                ),
            ),
        ):
            resp = await client_admin.post(
                f"{PREFIX}/import",
                json={
                    "bundle": _bundle(models=[_model_snap("good"), _model_snap("1bad")]),
                    "mode": "create",
                },
            )

        assert resp.status_code == 422
        assert "not BI-safe" in resp.json()["detail"]
        # No partial state: rollback fired, commit never did.
        mock_db.rollback.assert_awaited()
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

        authentic_vid = uuid.uuid4()
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
            # Bug-6295: a deployed-at-export model gets an authentic, servable
            # version appended so deploy-latest resolves a real snapshot.
            patch(
                "shared.model_snapshot.project_rehydrator.append_authentic_import_version",
                new_callable=AsyncMock,
                return_value=authentic_vid,
            ) as mock_append,
        ):
            result = await import_project(bundle, db, mode="create")

        assert "modelx" in result["models_requiring_deploy"]
        assert "redeploy_required" in result["post_import_actions"]
        # The importer must append the authentic servable version for a
        # previously-deployed model (Bug-6295 deploy happy-path).
        mock_append.assert_awaited_once()

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
            patch(
                "shared.model_snapshot.project_rehydrator.append_authentic_import_version",
                new_callable=AsyncMock,
            ) as mock_append,
        ):
            result = await import_project(bundle, db, mode="create")

        assert "modely" not in result["models_requiring_deploy"]
        assert "redeploy_required" not in result["post_import_actions"]
        # An undeployed-at-export model needs no authentic version appended.
        mock_append.assert_not_awaited()


# ---------------------------------------------------------------------------
# F2b — importer always holds an admin binding after import (F-021-04)
# ---------------------------------------------------------------------------

class TestF2bImporterAdminBinding:
    """F-021-04 / F2b: import must mirror create_project's ATOMIC creator-admin
    binding. A bootstrap-era zero-binding bundle imports binding-less; without
    this guarantee, after the hard cutover every ordinary user is locked out AND
    the importer holds no admin binding to run the repair op from."""

    @staticmethod
    def _added_bindings(db):
        from shared.db.models import UserAccessBinding
        return [
            c.args[0]
            for c in db.add.call_args_list
            if isinstance(c.args[0], UserAccessBinding)
        ]

    @pytest.mark.asyncio
    async def test_import_binding_less_bundle_grants_importer_admin(self):
        """Bundle carries NO access_bindings section (bootstrap-era zero-binding
        export). After import the importer holds a project-scoped admin binding;
        no other binding is created, so ordinary users stay denied until an
        admin runs the repair op."""
        from shared.auth.identity import canonical_user_identity

        bundle = _bundle()  # no models, no sections, no access_bindings
        db = _import_db_mock()

        result = await import_project(
            bundle, db, mode="create", actor="Importer@Test.com",
        )

        added = self._added_bindings(db)
        # Exactly one binding created — the importer's admin binding. No other
        # user is granted access (ordinary users stay denied until repair).
        assert len(added) == 1, [(b.user_identity, b.role) for b in added]
        importer_binding = added[0]
        assert importer_binding.role == "admin"
        assert importer_binding.model_id is None  # project-scoped
        assert str(importer_binding.project_id) == str(result["project_id"])
        # Identity is canonicalised, matching require_role's binding lookup.
        assert importer_binding.user_identity == canonical_user_identity(
            "Importer@Test.com"
        )
        assert importer_binding.user_identity == "importer@test.com"

    @pytest.mark.asyncio
    async def test_import_with_default_actor_grants_no_binding(self):
        """The guarantee is scoped to a real human importer. An internal/seed
        import that does not thread a human actor (the default sentinel) must NOT
        mint a junk admin binding for the non-user identity."""
        bundle = _bundle()
        db = _import_db_mock()

        await import_project(bundle, db, mode="create")  # default actor

        assert self._added_bindings(db) == []

    @pytest.mark.asyncio
    async def test_import_skips_when_bundle_already_granted_importer_admin(self):
        """Idempotent: if the bundle already restored a project-scoped admin
        binding for the importer, Step 10b must not add a second one. The
        importer-binding probe is the only UserAccessBinding SELECT in create
        mode, so route that query to an existing admin row."""
        bundle = _bundle()
        db = _import_db_mock()

        existing_admin = SimpleNamespace(
            role="admin", model_id=None, project_id=uuid.uuid4(),
            user_identity="importer@test.com",
        )
        found = MagicMock()
        found.scalar_one_or_none.return_value = existing_admin
        no_row = MagicMock()
        no_row.scalar_one_or_none.return_value = None
        no_row.scalars.return_value.all.return_value = []
        no_row.all.return_value = []

        async def _execute(stmt, *a, **k):
            if "user_access_bindings" in str(stmt):
                return found
            return no_row

        db.execute = _execute

        await import_project(
            bundle, db, mode="create", actor="importer@test.com",
        )

        # No new binding added — the existing admin binding satisfies the guarantee.
        assert self._added_bindings(db) == []


# ---------------------------------------------------------------------------
# F1 — import must reject non-human (service) principals (F-021-04)
# ---------------------------------------------------------------------------

class TestF1ImportRejectsServicePrincipal:
    """F1 (F-021-04): project import is a project-CREATING path and must mirror
    create_project's HUMAN-only gate.

    A service principal minted in production with role='tenant_admin'
    (aggregate-rebuild-service, model-service-deploy, glossary-bootstrap-service,
    ...) previously passed the import route's coarse token-role check
    (``current_user.role in {"tenant_admin", "system_admin"}``) and reached
    import_project with actor='service:<principal>'. Step 10b (F2b) then persisted
    a junk ADMIN UserAccessBinding for that non-human identity — a contract
    violation ("only a HUMAN tenant_admin/system_admin bypasses") that also
    permanently defeats the binding-less repair op (POST .../access/repair 409s
    when ANY binding exists). Two guards close this:
      1. Gate: the route now uses the human-only require_tenant_admin (identical
         to create_project), which rejects service AND embed principals.
      2. Defence in depth: Step 10b skips a 'service:'-prefixed actor even if one
         somehow reaches import_project.
    """

    @staticmethod
    def _service_admin() -> CurrentServiceUser:
        # role='tenant_admin' is the exact F1 vector: it slips the coarse
        # token-role check that only the human-only helper closes.
        return CurrentServiceUser(
            principal="aggregate-rebuild-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=[SCOPE_AGGREGATE_REBUILD],
        )

    @staticmethod
    def _valid_import_result() -> dict:
        return {
            "project_id": str(uuid.uuid4()),
            "project_slug": "imported",
            "id_map": {
                "models": {},
                "connections": {},
                "personas": {},
                "llm_configs": {},
                "judge_rubrics": {},
            },
            "models_imported": 0,
            "models_requiring_deploy": [],
            "post_import_actions": [],
            "warnings": [],
        }

    @staticmethod
    def _added_bindings(db):
        from shared.db.models import UserAccessBinding
        return [
            c.args[0]
            for c in db.add.call_args_list
            if isinstance(c.args[0], UserAccessBinding)
        ]

    @pytest.mark.asyncio
    async def test_f1_import_denies_service_principal_at_gate(self):
        """A role='tenant_admin' SERVICE token is rejected with 403 at the import
        gate and never reaches import_project — exactly as project CREATE denies
        it. import_project is patched to a mock so that, pre-fix, a passing gate
        would let the request through and the assertion fails on both the 403 and
        the not-awaited check."""
        svc = self._service_admin()
        mock_import = AsyncMock(return_value=self._valid_import_result())

        app.dependency_overrides[get_current_user] = lambda: svc
        try:
            with (
                patch(
                    "src.api.project_import_export.get_tenant_db",
                    async_gen_from(make_mock_db()),
                ),
                patch(
                    "src.api.project_import_export.get_credential_fernet",
                    new=MagicMock(return_value=object()),
                ),
                patch(
                    "src.api.project_import_export.import_project",
                    new=mock_import,
                ),
                patch(
                    "src.api.project_import_export.seed_technical_persona",
                    new=AsyncMock(),
                ),
                patch(
                    "src.api.project_import_export.audit_required",
                    new=AsyncMock(),
                ),
                patch(
                    "src.api.project_import_export."
                    "attempt_scheduled_physical_cleanup",
                    new=AsyncMock(),
                ),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as ac:
                    resp = await ac.post(
                        "/api/v1/projects/import",
                        json={"bundle": {}, "mode": "create"},
                    )
            assert resp.status_code == 403, resp.text
            mock_import.assert_not_awaited()
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_f1_import_service_actor_persists_no_binding(self):
        """Defence in depth: even if a non-human 'service:<principal>' actor
        reaches import_project, Step 10b must NOT mint an admin binding for it.
        Pre-fix Step 10b wrote a 'service:aggregate-rebuild-service' admin
        binding; post-fix it is skipped and no binding is added."""
        bundle = _bundle()  # no models, no sections, no access_bindings
        db = _import_db_mock()

        await import_project(
            bundle, db, mode="create", actor="service:aggregate-rebuild-service",
        )

        added = self._added_bindings(db)
        assert added == [], [(b.user_identity, b.role) for b in added]


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
        assert cascade.await_args.kwargs["cleanup_reason"] == "project_replace"
        assert "models" not in db.deleted_tables

    @pytest.mark.asyncio
    async def test_model_cascade_precedes_connection_prune(self):
        """Bug-7293 (sibling of fixed Bug-3684): the model cascade removes each
        model's DataSource/DataTarget, which RESTRICT-FK to project_connections.
        Those must be gone BEFORE any connection is pruned, or the prune 500s on
        the RESTRICT FK. Lock the FK-safe order: the model cascade runs before
        any project_connections DELETE."""
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        mid = uuid.uuid4()
        keep_id = str(uuid.uuid4())
        orphan_id = str(uuid.uuid4())
        order: list[str] = []

        class _DB(_DeleteRecordingDB):
            async def execute(self, stmt, *a, **k):
                table = _table_name_of(stmt)
                if getattr(stmt, "is_delete", False):
                    order.append(f"delete:{table}")
                    return MagicMock()
                if table == "models":
                    result = MagicMock()
                    result.all.return_value = [(mid,)]
                    result.scalars.return_value.all.return_value = []
                    result.scalar_one_or_none.return_value = None
                    return result
                if table == "project_connections":
                    result = MagicMock()
                    # One live orphan connection (not referenced by the bundle),
                    # so a prune DELETE is actually issued.
                    result.scalars.return_value.all.return_value = [
                        SimpleNamespace(
                            id=uuid.UUID(orphan_id),
                            display_name="Orphan",
                            connection_type="postgresql",
                        )
                    ]
                    result.scalar_one_or_none.return_value = None
                    return result
                return await super().execute(stmt, *a, **k)

        db = _DB(target)
        # Bundle carries a `connections` section so the replace prunes orphans.
        bundle = _bundle(
            included_sections=["connections"],
            connections=[],
            models=[],
        )

        async def _cascade(tenant_db, model_id, **kw):
            order.append("cascade")
            return []

        with patch(
            "shared.model_snapshot.project_rehydrator.delete_model_cascade",
            new=AsyncMock(side_effect=_cascade),
        ):
            await import_project(bundle, db, mode="replace")

        cascade_idx = order.index("cascade")
        conn_deletes = [
            i for i, ev in enumerate(order) if ev == "delete:project_connections"
        ]
        # FK-safe order: the model cascade precedes every connection prune.
        assert conn_deletes, "expected an orphan connection prune to fire"
        assert cascade_idx < min(conn_deletes)

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
        assert body["warnings"][0]["code"] == "legacy.warning"
        assert body["warnings"][0]["detail"] == "dry-run note"
        assert body["plan"]["warnings"] == body["warnings"]
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


# ---------------------------------------------------------------------------
# Bug-4263 — replace-plan cascade volume (aggregates/pockets/logs)
# ---------------------------------------------------------------------------

class TestReplacePlanCascadeCounts:
    """The dry-run plan must report the true cascade row volume a replace
    deletes (Bug-4263), not just the model count. ``delete_model_cascade``
    removes each model's aggregates, pockets and query/route logs — so the
    plan exposes a ``model_cascade_counts`` breakdown computed on the preview
    path only."""

    @pytest.mark.asyncio
    async def test_replace_plan_reports_per_model_cascade_volume(self):
        project_id = uuid.uuid4()
        model_id = uuid.uuid4()
        bundle = _bundle(models=[_model_snap(slug="orders")])

        db = _import_db_mock()

        project_row = MagicMock()
        project_row.scalar_one_or_none.return_value = SimpleNamespace(
            id=project_id, slug="imported"
        )
        model_list_row = MagicMock()
        model_list_row.all.return_value = [(model_id, "orders", "Orders")]
        route_count_row = MagicMock()
        route_count_row.scalar_one.return_value = 7

        # execute() sequence: project lookup → model list (cascade) →
        # route_logs count. _count_rows / _count_rows_by_model are patched, so
        # they do not consume an execute() call.
        db.execute = AsyncMock(
            side_effect=[project_row, model_list_row, route_count_row]
        )

        # delete_counts path: models=2, orphan_connections=0 (no conn section).
        with patch(
            "shared.model_snapshot.project_rehydrator._count_rows",
            AsyncMock(side_effect=[2, 0]),
        ), patch(
            "shared.model_snapshot.project_rehydrator._count_rows_by_model",
            # aggregates, pockets, query_logs, query_miss_logs for the one model
            AsyncMock(side_effect=[4, 3, 120, 9]),
        ):
            plan = await plan_project_import(bundle, db, mode="replace")

        assert plan["will_replace_project"] is True
        cascade = plan["model_cascade_counts"]
        assert cascade["totals"] == {
            "aggregates": 4,
            "pockets": 3,
            "query_logs": 120,
            "query_miss_logs": 9,
            "route_logs": 7,
        }
        assert len(cascade["per_model"]) == 1
        entry = cascade["per_model"][0]
        assert entry["slug"] == "orders"
        assert entry["display_name"] == "Orders"
        assert entry["model_id"] == str(model_id)
        assert entry["counts"] == {
            "aggregates": 4,
            "pockets": 3,
            "query_logs": 120,
            "query_miss_logs": 9,
            "route_logs": 7,
        }

    @pytest.mark.asyncio
    async def test_create_plan_has_empty_cascade_counts(self):
        """Create mode deletes nothing, so the cascade breakdown is empty and
        no counting queries run on the create path."""
        bundle = _bundle(models=[_model_snap(slug="orders")])
        db = _import_db_mock()  # project lookup returns None → create path

        plan = await plan_project_import(bundle, db, mode="create")

        assert plan["model_cascade_counts"] == {"per_model": [], "totals": {}}


# ---------------------------------------------------------------------------
# Bug-7296 — connection_mapping supplied without a `connections` section
# ---------------------------------------------------------------------------

class TestConnectionMappingConsumption:
    """Bug-7296: a connection_mapping is keyed by the EXPORT-side connection id.
    When a mapping key names a connection that no model's data_sources/targets
    reference (e.g. a stale or typo'd key, common when the bundle omits the
    `connections` section), the key is a no-op. The import must NOT crash — it
    must report the unused key as a warning so the operator can fix the mapping,
    while genuinely-unmapped model connections still fail with a clear error.
    """

    def test_referenced_ids_collects_source_and_target_connections(self):
        src_conn = str(uuid.uuid4())
        tgt_conn = str(uuid.uuid4())
        bundle = _bundle(models=[{
            "model": {"id": str(uuid.uuid4()), "slug": "m", "seed": "s"},
            "data_sources": [{"id": str(uuid.uuid4()), "project_connection_id": src_conn}],
            "data_targets": [{"id": str(uuid.uuid4()), "project_connection_id": tgt_conn}],
        }])

        referenced = _referenced_export_connection_ids(bundle)

        assert referenced == {src_conn, tgt_conn}

    def test_referenced_ids_empty_when_no_connection_refs(self):
        bundle = _bundle(models=[{
            "model": {"id": str(uuid.uuid4()), "slug": "m", "seed": "s"},
            "data_sources": [{"id": str(uuid.uuid4())}],
            "data_targets": [],
        }])

        assert _referenced_export_connection_ids(bundle) == set()

    @pytest.mark.asyncio
    async def test_unused_mapping_key_reported_as_warning_no_crash(self):
        """A connection_mapping key referenced by no model surfaces as a warning
        (not a crash) after import."""
        used_conn = str(uuid.uuid4())
        stale_conn = str(uuid.uuid4())
        target_conn_id = str(uuid.uuid4())

        model_snap = {
            "schema_version": 2,
            "model": {"id": str(uuid.uuid4()), "slug": "orders", "seed": "abc123"},
            "exported_deployed_version_id": None,
            "model_versions": [],
            "aggregates": [],
            "pockets": [],
            "data_sources": [
                {"id": str(uuid.uuid4()), "project_connection_id": used_conn}
            ],
            "data_targets": [],
        }
        # No `connections` section; caller maps the used connection AND a stale
        # one that no model references.
        bundle = _bundle(
            models=[model_snap],
            connection_mapping=None,
        )

        # The mapping targets must be pre-existing connections of the project so
        # the target-ownership validation passes and the import reaches the
        # unused-key detection. Return target_conn_id from the project_connections
        # SELECT (the `elif connection_mapping:` branch selects only the id).
        db = _import_db_mock()

        def _table_name(stmt) -> str | None:
            try:
                return stmt.get_final_froms()[0].name
            except Exception:
                return None

        async def _execute(stmt, *a, **kw):
            result = MagicMock()
            if _table_name(stmt) == "project_connections":
                result.scalars.return_value.all.return_value = [uuid.UUID(target_conn_id)]
            else:
                result.scalar_one_or_none.return_value = None
                result.scalars.return_value.all.return_value = []
                result.all.return_value = []
            return result

        db.execute = AsyncMock(side_effect=_execute)

        async def _fake_rehydrate(*args, **kwargs):
            return None

        async def _fake_insert_model(tenant_db, **kwargs):
            new_id = uuid.uuid4()
            return SimpleNamespace(id=new_id, slug=kwargs.get("base_slug")), kwargs.get("base_slug")

        with patch(
            "shared.model_snapshot.project_rehydrator.rehydrate_into_live",
            _fake_rehydrate,
        ), patch(
            "shared.model_snapshot.project_rehydrator.insert_model_with_slug_retry",
            _fake_insert_model,
        ), patch(
            "shared.model_snapshot.project_rehydrator.prepare_snapshot_for_import",
            lambda ms, **kw: (ms, []),
        ):
            result = await import_project(
                bundle,
                db,
                mode="create",
                connection_mapping={used_conn: target_conn_id, stale_conn: target_conn_id},
            )

        warnings = " ".join(warning.detail for warning in result["warnings"])
        assert stale_conn in warnings
        assert "were not used by any imported model" in warnings
        # The used connection id must NOT be reported as unused.
        assert used_conn not in warnings

    @pytest.mark.asyncio
    async def test_connection_retained_via_connections_section_not_flagged_unused(self):
        """Fable R1 finding 3: a mapping key consumed on the connections-present
        path (it matched/retained a target connection, protecting it from the
        replace orphan-prune / driving a credential override) must NOT be flagged
        'unused' even when no model references it. The import records such keys in
        ``consumed_mapping_keys``, so deriving the consumed set from there (not
        model references alone) prevents an operator from removing a key that is
        protecting a live connection."""
        retained_export_id = "exp-retained"
        target_conn_id = str(uuid.uuid4())

        # A model that references NOTHING (no data_sources/targets), so the only
        # way the mapping key is consumed is the connections-section retention.
        model_snap = {
            "schema_version": 2,
            "model": {"id": str(uuid.uuid4()), "slug": "orders", "seed": "abc123"},
            "exported_deployed_version_id": None,
            "model_versions": [],
            "aggregates": [],
            "pockets": [],
            "data_sources": [],
            "data_targets": [],
        }
        bundle = _bundle(
            models=[model_snap],
            included_sections=["connections"],
            connections=[{
                "id": retained_export_id,
                "display_name": "Prod WH",
                "connection_type": "postgresql",
            }],
        )

        # Pre-existing target connection that the mapping retains.
        existing_conn = SimpleNamespace(
            id=uuid.UUID(target_conn_id),
            display_name="Prod WH",
            connection_type="postgresql",
        )

        db = _import_db_mock()

        def _table_name(stmt) -> str | None:
            try:
                return stmt.get_final_froms()[0].name
            except Exception:
                return None

        async def _execute(stmt, *a, **kw):
            result = MagicMock()
            if _table_name(stmt) == "project_connections":
                result.scalars.return_value.all.return_value = [existing_conn]
                result.scalar_one_or_none.return_value = None
            else:
                result.scalar_one_or_none.return_value = None
                result.scalars.return_value.all.return_value = []
                result.all.return_value = []
            return result

        db.execute = AsyncMock(side_effect=_execute)

        async def _fake_rehydrate(*args, **kwargs):
            return None

        async def _fake_insert_model(tenant_db, **kwargs):
            new_id = uuid.uuid4()
            return SimpleNamespace(id=new_id, slug=kwargs.get("base_slug")), kwargs.get("base_slug")

        with patch(
            "shared.model_snapshot.project_rehydrator.rehydrate_into_live",
            _fake_rehydrate,
        ), patch(
            "shared.model_snapshot.project_rehydrator.insert_model_with_slug_retry",
            _fake_insert_model,
        ), patch(
            "shared.model_snapshot.project_rehydrator.prepare_snapshot_for_import",
            lambda ms, **kw: (ms, []),
        ):
            result = await import_project(
                bundle,
                db,
                mode="create",
                connection_mapping={retained_export_id: target_conn_id},
            )

        warnings = " ".join(warning.detail for warning in result["warnings"])
        # The retained key was consumed (entered conn_id_remap) — no false alarm.
        assert retained_export_id not in warnings


# ---------------------------------------------------------------------------
# Bug-8770 (AKA source Bug-8781) -- connection credential overrides change
# source routing and must invalidate artifacts in the same import transaction.
# ---------------------------------------------------------------------------


class TestBug8837ConnectionOverrideEgressPolicy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_explicit_mapping", [True, False])
    @pytest.mark.parametrize("connection_type", ["postgresql", "bigquery"])
    async def test_bug_8837_import_override_rejects_disallowed_egress_host(
        self, use_explicit_mapping: bool, connection_type: str,
    ):
        """Both existing-connection override branches are source write paths.

        A bundle must not use either explicit mapping or display-name/type
        auto-match to persist a host that the ordinary connection API refuses.
        The policy check must happen before the SQL UPDATE is issued.
        """
        import base64
        import json

        from cryptography.fernet import Fernet

        export_connection_id = "export-connection"
        target_connection_id = uuid.uuid4()
        passphrase_fernet = Fernet(Fernet.generate_key())
        system_fernet = Fernet(Fernet.generate_key())
        credentials = (
            {
                "service_account_json": json.dumps({
                    "token_uri": "https://169.254.169.254/token",
                }),
            }
            if connection_type == "bigquery"
            else {
                "host": "169.254.169.254",
                "username": "tenant-user",
            }
        )
        plaintext = json.dumps(credentials).encode("utf-8")
        encrypted = passphrase_fernet.encrypt(plaintext)
        bundle = _bundle(
            credentials_included=True,
            included_sections=["connections"],
            connections=[{
                "id": export_connection_id,
                "display_name": "Production source",
                "connection_type": connection_type,
                "config": {"database": "analytics"},
                "credentials": base64.b64encode(encrypted).decode("ascii"),
            }],
        )
        existing_connection = SimpleNamespace(
            id=target_connection_id,
            display_name="Production source",
            connection_type=connection_type,
        )
        updates: list[str] = []
        db = _import_db_mock()

        async def _execute(stmt, *args, **kwargs):
            result = MagicMock()
            if getattr(stmt, "is_update", False):
                updates.append(stmt.table.name)
            else:
                try:
                    table_name = stmt.get_final_froms()[0].name
                except Exception:
                    table_name = None
                if table_name == "project_connections":
                    result.scalars.return_value.all.return_value = [
                        existing_connection
                    ]
                else:
                    result.scalar_one_or_none.return_value = None
                    result.scalars.return_value.all.return_value = []
                    result.all.return_value = []
            return result

        db.execute = AsyncMock(side_effect=_execute)
        connection_mapping = (
            {export_connection_id: str(target_connection_id)}
            if use_explicit_mapping
            else None
        )

        with pytest.raises(ProjectImportError, match="source egress policy"):
            await import_project(
                bundle,
                db,
                mode="create",
                connection_mapping=connection_mapping,
                override_connections=True,
                passphrase_fernet=passphrase_fernet,
                system_fernet=system_fernet,
            )

        assert updates == [], "the unsafe override reached SQL before rejection"


class TestConnectionOverrideArtifactInvalidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("use_explicit_mapping", [True, False])
    async def test_override_updates_then_invalidates_target_connection(
        self, use_explicit_mapping: bool,
    ):
        import base64

        from cryptography.fernet import Fernet

        export_connection_id = "export-connection"
        target_connection_id = uuid.uuid4()
        passphrase_fernet = Fernet(Fernet.generate_key())
        system_fernet = Fernet(Fernet.generate_key())
        encrypted = passphrase_fernet.encrypt(b"source-credentials")
        bundle = _bundle(
            credentials_included=True,
            included_sections=["connections"],
            connections=[{
                "id": export_connection_id,
                "display_name": "Production source",
                "connection_type": "postgresql",
                "config": {"database": "new_source"},
                "credentials": base64.b64encode(encrypted).decode("ascii"),
            }],
        )
        existing_connection = SimpleNamespace(
            id=target_connection_id,
            display_name="Production source",
            connection_type="postgresql",
        )
        events: list[str] = []
        db = _import_db_mock()

        async def _execute(stmt, *args, **kwargs):
            result = MagicMock()
            if getattr(stmt, "is_update", False):
                assert stmt.table.name == "project_connections"
                events.append("connection_update")
            else:
                try:
                    table_name = stmt.get_final_froms()[0].name
                except Exception:
                    table_name = None
                if table_name == "project_connections":
                    result.scalars.return_value.all.return_value = [
                        existing_connection
                    ]
                else:
                    result.scalar_one_or_none.return_value = None
                    result.scalars.return_value.all.return_value = []
                    result.all.return_value = []
            return result

        async def _invalidate(session, connection_id, *, reason):
            assert session is db
            assert connection_id == target_connection_id
            assert reason == "import_override_connections"
            events.append("artifact_invalidation")
            return (1, 1)

        db.execute = AsyncMock(side_effect=_execute)
        connection_mapping = (
            {export_connection_id: str(target_connection_id)}
            if use_explicit_mapping
            else None
        )

        with patch(
            "shared.model_snapshot.project_rehydrator."
            "invalidate_artifacts_for_connection",
            new=_invalidate,
        ):
            await import_project(
                bundle,
                db,
                mode="create",
                connection_mapping=connection_mapping,
                override_connections=True,
                passphrase_fernet=passphrase_fernet,
                system_fernet=system_fernet,
            )

        assert events == ["connection_update", "artifact_invalidation"]


# ---------------------------------------------------------------------------
# Bug-8350 R2 [MED-3] — project import must not restore a weak/predictable
# webhook_signing_secret with no strength validation. Every in-app path
# (auto-generation, rotate-secret) only ever produces a
# secrets.token_urlsafe(32) secret; an import bundle is untrusted input that
# could carry anything.
# ---------------------------------------------------------------------------


class TestAgentConfigWebhookSecretImportStrengthCheck:
    def _agent_config_bundle(self, *, plaintext_secret: str, passphrase_fernet):
        """Minimal bundle carrying only an agent_config section with a
        webhook_signing_secret encrypted under the caller's passphrase, the
        same shape import_project expects (Step 8b)."""
        encrypted = passphrase_fernet.encrypt(plaintext_secret.encode())
        import base64 as _b64

        return _bundle(
            credentials_included=True,
            included_sections=["agent_config"],
            agent_config={
                "config": {
                    "enabled": True,
                    "webhook_url": "https://receiver.example/hooks/x",
                    "webhook_signing_secret": _b64.b64encode(encrypted).decode(),
                },
                "models": [],
                "model_contexts": [],
                "judge_rubrics": [],
            },
        )

    @pytest.mark.asyncio
    async def test_weak_imported_secret_is_discarded_not_restored(self):
        from cryptography.fernet import Fernet
        from shared.db.models import ProjectAgentConfig

        passphrase_fernet = Fernet(Fernet.generate_key())
        system_fernet = Fernet(Fernet.generate_key())
        bundle = self._agent_config_bundle(
            plaintext_secret="abc123",  # far under the 32-char floor
            passphrase_fernet=passphrase_fernet,
        )
        db = _import_db_mock()

        await import_project(
            bundle, db, mode="create",
            passphrase_fernet=passphrase_fernet,
            system_fernet=system_fernet,
        )

        added_pac = [
            call.args[0] for call in db.add.call_args_list
            if isinstance(call.args[0], ProjectAgentConfig)
        ]
        assert len(added_pac) == 1
        assert added_pac[0].webhook_signing_secret is None, (
            "a weak imported secret must never be persisted — the agent "
            "webhook should stay refused-unsigned until a modeller rotates "
            "a real secret from the UI"
        )

    @pytest.mark.asyncio
    async def test_dropped_weak_secret_is_surfaced_to_the_importing_admin(self):
        """Fresh-reviewer follow-up on this lane: dropping the secret
        silently disables agent webhooks on a project whose webhook_url WAS
        restored (it is not gated on this check) -- a server log alone is
        not an admin-visible signal. import_project already returns a
        `warnings` list used for far less consequential cases (a skipped
        access binding, a deferred connection credential); the dropped
        secret must use the same channel."""
        from cryptography.fernet import Fernet

        passphrase_fernet = Fernet(Fernet.generate_key())
        system_fernet = Fernet(Fernet.generate_key())
        bundle = self._agent_config_bundle(
            plaintext_secret="abc123", passphrase_fernet=passphrase_fernet,
        )
        result = await import_project(
            bundle, _import_db_mock(), mode="create",
            passphrase_fernet=passphrase_fernet,
            system_fernet=system_fernet,
        )
        joined = " ".join(
            warning.detail for warning in result["warnings"]
        ).lower()
        assert "webhook" in joined and "secret" in joined, (
            "the import silently disabled agent webhook delivery with no "
            f"admin-visible warning; warnings={result['warnings']!r}"
        )

    @pytest.mark.asyncio
    async def test_placeholder_imported_secret_is_discarded(self):
        """Bug-5951's placeholder list applies at import time too, not only
        at dispatch time. Uses an exact placeholder value (the frozenset in
        ``is_valid_signing_secret`` only matches exactly, case-insensitive
        after stripping) -- it is also short, so this exercises the
        placeholder branch of the ``and`` (the length-floor test above
        already covers the other branch in isolation)."""
        from cryptography.fernet import Fernet
        from shared.db.models import ProjectAgentConfig

        passphrase_fernet = Fernet(Fernet.generate_key())
        system_fernet = Fernet(Fernet.generate_key())
        bundle = self._agent_config_bundle(
            plaintext_secret="changeme",
            passphrase_fernet=passphrase_fernet,
        )
        db = _import_db_mock()

        await import_project(
            bundle, db, mode="create",
            passphrase_fernet=passphrase_fernet,
            system_fernet=system_fernet,
        )

        added_pac = [
            call.args[0] for call in db.add.call_args_list
            if isinstance(call.args[0], ProjectAgentConfig)
        ]
        assert len(added_pac) == 1
        assert added_pac[0].webhook_signing_secret is None

    @pytest.mark.asyncio
    async def test_strong_imported_secret_is_restored(self):
        """Regression guard against over-rejecting: a real
        generate_signing_secret()-shaped secret must still round-trip.
        ``system_fernet`` is a real ``Fernet`` instance passed directly to
        ``import_project`` (mirroring production, where the caller supplies
        the live system Fernet) — no crypto needs mocking here."""
        import secrets as _secrets

        from cryptography.fernet import Fernet
        from shared.db.models import ProjectAgentConfig

        passphrase_fernet = Fernet(Fernet.generate_key())
        system_fernet = Fernet(Fernet.generate_key())
        strong_secret = _secrets.token_urlsafe(32)
        bundle = self._agent_config_bundle(
            plaintext_secret=strong_secret,
            passphrase_fernet=passphrase_fernet,
        )
        db = _import_db_mock()

        await import_project(
            bundle, db, mode="create",
            passphrase_fernet=passphrase_fernet,
            system_fernet=system_fernet,
        )

        added_pac = [
            call.args[0] for call in db.add.call_args_list
            if isinstance(call.args[0], ProjectAgentConfig)
        ]
        assert len(added_pac) == 1
        assert added_pac[0].webhook_signing_secret is not None
        # Round-trip: decrypting what was stored under system_fernet must
        # yield back the original strong secret.
        assert (
            system_fernet.decrypt(added_pac[0].webhook_signing_secret).decode()
            == strong_secret
        )


class _ModelsForReplaceDB(_ReplaceModeDB):
    """``_ReplaceModeDB`` that also answers the replace-mode model enumeration.

    ``project_rehydrator`` reads the existing model ids with
    ``select(Model.id).where(Model.project_id == ...)`` and consumes the result
    with ``.all()``. Everything else is inherited.
    """

    def __init__(self, project, conns, model_ids):
        super().__init__(project, conns)
        self._model_ids = list(model_ids)

    async def execute(self, stmt, *args, **kwargs):
        result = await super().execute(stmt, *args, **kwargs)
        if not getattr(stmt, "is_delete", False) and _table_name_of(stmt) == "models":
            result.all.return_value = [(mid,) for mid in self._model_ids]
        return result


class TestReplaceModeDeletesModelsInLockOrder:
    """CR-4: import-replace must hand its model ids to ``delete_model_cascade``
    in ``sorted(key=str)`` order.

    ``delete_model_cascade`` acquires each model's advisory lock and holds it
    until the import transaction ends, so a replace of an N-model project holds
    N locks at once. Two concurrent multi-model lockers that disagree on
    acquisition order deadlock with each other (40P01), and ``str(uuid)`` is the
    order ``0194._lock_models`` and ``delete_project_cascade`` both use.

    Test escape: existing import coverage proves the rehydrator CALLS the
    cascade; nothing proved the ORDER it calls it in, so deleting the ``sorted``
    stayed green. Guard: this test. Tier: T1.
    """

    @pytest.mark.asyncio
    async def test_replace_deletes_models_in_str_uuid_order(self):
        target = SimpleNamespace(
            id="proj-1", slug="imported", display_name="Imported", is_active=True
        )
        # Four ids whose natural/creation order deliberately differs from their
        # string order, so a missing sort cannot pass by accident.
        model_ids = [
            uuid.UUID("ffffffff-0000-0000-0000-000000000001"),
            uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
            uuid.UUID("7f000000-0000-0000-0000-000000000002"),
            uuid.UUID("0f000000-0000-0000-0000-000000000003"),
        ]
        expected = sorted(model_ids, key=str)
        assert model_ids != expected, "the fixture no longer tests ordering"

        db = _ModelsForReplaceDB(target, [], model_ids)
        seen: list[uuid.UUID] = []

        async def _record(
            session,
            mid,
            *,
            fail_fast=True,
            cleanup_reason="model_delete",
        ):
            assert cleanup_reason == "project_replace"
            seen.append(mid)
            return []

        bundle = _bundle(included_sections=[], models=[])
        with patch(
            "shared.model_snapshot.project_rehydrator.delete_model_cascade",
            new=_record,
        ):
            await import_project(bundle, db, mode="replace")

        assert seen == expected, (
            "import-replace deleted the existing models in "
            f"{[str(m) for m in seen]} order, not str(uuid) order "
            f"{[str(m) for m in expected]}. delete_model_cascade takes each "
            "model's advisory lock and holds it for the whole import, so a "
            "replace that disagrees with delete_project_cascade / "
            "0194._lock_models on acquisition order deadlocks with them."
        )
