"""Bug-6264 (import sibling) — a non-admin cannot mint a certified/shared KPI
or named-set by hand-authoring a snapshot-import bundle.

The snapshot-import route is modeler-gated and the bundle is caller-supplied,
unsigned JSON. The shared rehydrator inserts governance verbatim, so the
model-service import route clamps imported KPI/named-set certification_status to
draft for non-admin importers before rehydration. Admin importers may restore an
admin-conferred status.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.import_export import EXPORT_FORMAT, _clamp_imported_certification
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import TEST_PROJECT_ID, TEST_TENANT, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit


def _bundle():
    return {
        "kpis": [
            {"name": "A", "certification_status": "certified"},
            {"name": "B", "certification_status": "shared"},
            {"name": "C", "certification_status": "draft"},
            {"name": "D", "certification_status": "pending"},  # junk (Bug-6615)
        ],
        "named_sets": [
            {"name": "NS", "certification_status": "certified"},
        ],
    }


def test_non_admin_import_clamps_all_non_draft_to_draft():
    snap = _bundle()
    _clamp_imported_certification(snap, is_admin=False)
    # Privileged AND junk statuses both neutralised; draft untouched.
    assert [k["certification_status"] for k in snap["kpis"]] == [
        "draft", "draft", "draft", "draft",
    ]
    assert snap["named_sets"][0]["certification_status"] == "draft"


def test_admin_import_preserves_status():
    snap = _bundle()
    _clamp_imported_certification(snap, is_admin=True)
    assert [k["certification_status"] for k in snap["kpis"]] == [
        "certified", "shared", "draft", "pending",
    ]
    assert snap["named_sets"][0]["certification_status"] == "certified"


@pytest.mark.asyncio
async def test_import_route_wires_clamp_for_non_admin():
    """Wiring guard: the modeler snapshot-import route must actually invoke the
    clamp so the snapshot handed to rehydrate_into_live has draft KPIs. Deleting
    the clamp call must fail this test (not just the helper unit tests)."""
    modeler = CurrentUser(
        user_id="m@example.com", tenant_id=TEST_TENANT,
        email="m@example.com", role="modeler",
    )
    app.dependency_overrides[get_current_user] = lambda: modeler
    try:
        rewritten = {
            "model": {},
            "kpis": [{"name": "K", "certification_status": "certified"}],
            "named_sets": [{"name": "NS", "certification_status": "shared"}],
        }
        captured = {}

        async def _capture_rehydrate(new_model_id, snapshot, tenant_db, **kw):
            captured["snapshot"] = snapshot

        db = make_mock_db()
        slug_result = MagicMock()
        slug_result.all.return_value = []
        db.execute = AsyncMock(return_value=slug_result)

        body = {
            "bundle": {
                "schema_version": 1,
                "export_format": EXPORT_FORMAT,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "exported_from": {"model_id": str(uuid.uuid4())},
                "model_display_name": "Imported Model",
                "model_slug": "imported-model",
                "snapshot": {"model": {}},
            },
            "target_project_id": str(TEST_PROJECT_ID),
        }

        with (
            patch("src.api.import_export.get_tenant_db", async_gen_from(db)),
            patch("src.api.import_export._ensure_project_access", new=AsyncMock()),
            patch(
                "src.api.import_export.prepare_snapshot_for_import",
                new=MagicMock(return_value=(rewritten, [])),
            ),
            patch(
                "src.api.import_export.insert_model_with_slug_retry",
                new=AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), "imported-model")),
            ),
            patch("src.api.import_export.rehydrate_into_live", new=_capture_rehydrate),
            patch("src.api.import_export.trigger_model_refresh", new=AsyncMock()),
        ):
            import httpx

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/models/snapshot-import",
                    json=body,
                )

        assert resp.status_code == 200, resp.text
        # The snapshot handed to the rehydrator was clamped before rehydration.
        assert captured["snapshot"]["kpis"][0]["certification_status"] == "draft"
        assert captured["snapshot"]["named_sets"][0]["certification_status"] == "draft"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_bug_8134_two_fact_table_snapshot_import_rejects_clean_4xx_no_staging():
    """Bug-8134 (AKA F-013-10): a single-model snapshot-import bundle with two
    fact-typed tables must be rejected with a clean 4xx BEFORE any row is
    staged, not left to reach the DB's partial unique index
    (uq_model_tables_one_fact_per_model, migration 0136) and 500 as a raw
    IntegrityError.

    Bug-8134's registered remediation names the snapshot-import route
    (api/import_export.py:418-439, this endpoint) as its PRIMARY evidence
    path; the project-bundle import fix (project_rehydrator.py::
    _validate_bundle) covers only /projects/{p}/import (whole-project
    bundles), a DIFFERENT endpoint. This mirrors that fix's shape
    (is_fact_table over snapshot["tables"], BEFORE the destination Model row
    is staged) for THIS endpoint. ``insert_model_with_slug_retry`` and
    ``rehydrate_into_live`` are both patched with AsyncMocks so the proof is
    direct: with the fix, neither is ever called; pre-fix, both would be
    (the destination Model row staged, then rehydrate_into_live handed the
    two-fact snapshot) and the endpoint would 200.
    """
    modeler = CurrentUser(
        user_id="m@example.com", tenant_id=TEST_TENANT,
        email="m@example.com", role="modeler",
    )
    app.dependency_overrides[get_current_user] = lambda: modeler
    try:
        rewritten = {
            "model": {},
            "tables": [
                {"id": "t1", "physical_name": "orders", "table_type": "fact"},
                {"id": "t2", "physical_name": "shipments", "table_type": "fact"},
            ],
        }

        db = make_mock_db()
        slug_result = MagicMock()
        slug_result.all.return_value = []
        db.execute = AsyncMock(return_value=slug_result)

        body = {
            "bundle": {
                "schema_version": 1,
                "export_format": EXPORT_FORMAT,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "exported_from": {"model_id": str(uuid.uuid4())},
                "model_display_name": "Two Fact",
                "model_slug": "two-fact",
                "snapshot": {"model": {}},
            },
            "target_project_id": str(TEST_PROJECT_ID),
        }

        mock_insert_model = AsyncMock(
            return_value=(SimpleNamespace(id=uuid.uuid4()), "two-fact")
        )
        mock_rehydrate = AsyncMock()

        with (
            patch("src.api.import_export.get_tenant_db", async_gen_from(db)),
            patch("src.api.import_export._ensure_project_access", new=AsyncMock()),
            patch(
                "src.api.import_export.prepare_snapshot_for_import",
                new=MagicMock(return_value=(rewritten, [])),
            ),
            patch(
                "src.api.import_export.insert_model_with_slug_retry",
                new=mock_insert_model,
            ),
            patch("src.api.import_export.rehydrate_into_live", new=mock_rehydrate),
            patch("src.api.import_export.trigger_model_refresh", new=AsyncMock()),
        ):
            import httpx

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.post(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/models/snapshot-import",
                    json=body,
                )

        assert 400 <= resp.status_code < 500, resp.text
        assert "fact table" in resp.json()["detail"]
        mock_insert_model.assert_not_awaited()
        mock_rehydrate.assert_not_awaited()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_clamp_tolerates_missing_keys_and_non_dicts():
    # No kpis/named_sets keys — must not raise.
    _clamp_imported_certification({}, is_admin=False)
    # Non-dict rows are skipped defensively; absent status key is left alone
    # (DB default is draft), non-draft is clamped.
    snap = {"kpis": [None, "junk", {"name": "no_status"},
                     {"certification_status": "certified"}]}
    _clamp_imported_certification(snap, is_admin=False)
    assert "certification_status" not in snap["kpis"][2]
    assert snap["kpis"][3]["certification_status"] == "draft"
