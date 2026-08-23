"""Endpoint-level guards for the ecosystem import lane (Lane D).

Covers:
  - Bug-7622: digit-leading / symbol-only entity names must not 500. The mapper
    now emits a BI-safe slug; a residual invalid slug is surfaced as 422, never
    an uncaught 500.
  - Bug-7309: dry_run=true returns the parse/loss report WITHOUT persisting —
    no tenant session is opened and nothing is committed (models_created=0).
  - Bug-7291-family (no partial state): a mid-import failure must not leave
    committed models. Because commit is the last step and the session
    auto-rolls-back on any exception, commit() is never reached and no partial
    state is persisted.
"""
from __future__ import annotations

import io
import textwrap
import uuid
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.main import app
from shared.auth.middleware import CurrentUser, require_tenant_admin
from shared.importers.dbt_mapper import MapResult

from .conftest import TEST_PROJECT_ID, TEST_TENANT, async_gen_from, make_mock_db, make_project

pytestmark = pytest.mark.unit

DBT_PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/import"
YAML_PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/import/yaml"


# ---------------------------------------------------------------------------
# Admin auth + demo-lock / cap bypass
# ---------------------------------------------------------------------------

def _admin_user() -> CurrentUser:
    return CurrentUser(
        user_id="admin@t.test",
        tenant_id=TEST_TENANT,
        email="admin@t.test",
        role="tenant_admin",
    )


@pytest.fixture
def admin_client():
    app.dependency_overrides[require_tenant_admin] = _admin_user
    try:
        with patch(
            "src.licensing_guard.enforce_demo_source_locked", lambda *a, **k: None
        ), patch(
            "src.api.dbt_import.enforce_demo_source_locked", lambda *a, **k: None
        ), patch(
            "src.api.cube_import.enforce_demo_source_locked", lambda *a, **k: None
        ), patch(
            "src.api.atscale_import.enforce_demo_source_locked", lambda *a, **k: None
        ), patch(
            "src.api.yaml_export.enforce_demo_source_locked", lambda *a, **k: None
        ), patch(
            "src.api.dbt_import.enforce_import_model_cap", new_callable=AsyncMock
        ), patch(
            "src.api.cube_import.enforce_import_model_cap", new_callable=AsyncMock
        ), patch(
            "src.api.atscale_import.enforce_import_model_cap", new_callable=AsyncMock
        ):
            yield
    finally:
        app.dependency_overrides.pop(require_tenant_admin, None)


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _upload(content: str, filename: str = "models.yml") -> dict:
    return {"file": (filename, content.encode("utf-8"), "application/x-yaml")}


def _zip_upload(files: dict[str, str], filename: str = "bundle.zip") -> dict:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return {"file": (filename, buf.getvalue(), "application/zip")}


# A dbt semantic model whose NAME is digit-leading — the Bug-7622 trigger.
_DIGIT_LEADING_DBT = textwrap.dedent("""\
    semantic_models:
      - name: "123orders"
        model: ref('orders')
        measures:
          - name: revenue
            agg: sum
            expr: amount
""")

_VALID_DBT = textwrap.dedent("""\
    semantic_models:
      - name: orders
        model: ref('orders')
        measures:
          - name: revenue
            agg: sum
            expr: amount
""")


# ---------------------------------------------------------------------------
# Bug-7622 — digit-leading name must not 500
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dbt_digit_leading_name_does_not_500(admin_client):
    """The digit-leading model name maps to a BI-safe slug (``_123orders``);
    the import succeeds (2xx), never a 500 from validate_bi_safe_slug."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_project())
    inserted_model = MagicMock(id=uuid.uuid4(), slug="_123orders")

    with patch("src.api.dbt_import.get_tenant_db", async_gen_from(db)), patch(
        "src.api.dbt_import.insert_model_with_slug_retry",
        AsyncMock(return_value=(inserted_model, "_123orders")),
    ), patch(
        "src.api.dbt_import.rehydrate_into_live", new_callable=AsyncMock
    ), patch(
        "src.api.dbt_import.seed_technical_persona", new_callable=AsyncMock
    ), patch(
        "src.api.dbt_import.prepare_snapshot_for_import",
        lambda snap, new_model_id: (dict(snap), []),
    ):
        async with await _client() as ac:
            resp = await ac.post(
                f"{DBT_PREFIX}/dbt", files=_upload(_DIGIT_LEADING_DBT)
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["model_names"] == ["_123orders"]
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_dbt_invalid_slug_is_422_not_500(admin_client):
    """Safety net: if insert_model_with_slug_retry still raises ValueError for a
    residual non-BI-safe slug, the endpoint returns a clean 422 and never
    commits (no partial state)."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_project())

    with patch("src.api.dbt_import.get_tenant_db", async_gen_from(db)), patch(
        "src.api.dbt_import.insert_model_with_slug_retry",
        AsyncMock(side_effect=ValueError("Model slug '1x' is not BI-safe.")),
    ), patch(
        "src.api.dbt_import.rehydrate_into_live", new_callable=AsyncMock
    ), patch(
        "src.api.dbt_import.prepare_snapshot_for_import",
        lambda snap, new_model_id: (dict(snap), []),
    ):
        async with await _client() as ac:
            resp = await ac.post(f"{DBT_PREFIX}/dbt", files=_upload(_VALID_DBT))

    assert resp.status_code == 422, resp.text
    assert "not BI-safe" in resp.text
    # No partial state: commit was never reached.
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bug-8139 — malformed YAML in a project-bundle .zip upload must be a clean
# 422 naming the bad line, never an uncaught yaml.YAMLError -> 500.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_yaml_bundle_malformed_project_yaml_is_422_not_500(admin_client):
    """Bug-8139: POST /projects/{p}/import/yaml with a project.yaml containing
    a literal-tab syntax error must return HTTP 422 naming the offending
    line, not an uncaught 500.

    Pre-fix, ``yaml.safe_load`` in ``parse_project_yaml``
    (shared/model_snapshot/yaml_deserialiser.py) had no try/except around
    it: the raw ``yaml.scanner.ScannerError`` propagated past this
    endpoint's ``except YamlImportError`` handler (it never matched, since
    a bare ``yaml.YAMLError`` is not a ``YamlImportError``) straight to
    FastAPI's default handler -- an uncaught-exception 500, even though a
    malformed bundle is client input, not a server fault. No tenant DB
    session is needed for this assertion: the parse (and its rejection)
    happens before ``get_tenant_db`` is ever opened.
    """
    async with await _client() as ac:
        resp = await ac.post(
            YAML_PREFIX,
            files=_zip_upload({"project.yaml": "project:\n\tname: x\n"}),
        )

    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]["errors"]
    assert any("line 2" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Bug-7309 — dry_run persists nothing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dbt_dry_run_persists_nothing(admin_client):
    """dry_run=true returns the parse/loss report and opens NO tenant session."""
    open_db = MagicMock(side_effect=AssertionError("get_tenant_db must not be opened on dry_run"))

    with patch("src.api.dbt_import.get_tenant_db", open_db):
        async with await _client() as ac:
            resp = await ac.post(
                f"{DBT_PREFIX}/dbt?dry_run=true", files=_upload(_VALID_DBT)
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models_created"] == 0
    assert body["models_parsed"] == 1
    assert body["model_names"] == ["orders"]
    # The DB dependency was never entered — nothing could have been persisted.
    open_db.assert_not_called()


@pytest.mark.asyncio
async def test_bug8141_dbt_warning_response_is_structured(admin_client):
    """Bug-8141: the API boundary converts legacy mapper detail to a stable record."""
    mapped = MapResult(
        bundle={
            "models": [],
            "project": {"slug": "dbt-import", "display_name": "dbt Import"},
        },
        warnings=["A dbt measure was imported as disabled; review it."],
    )

    with patch(
        "src.api.dbt_import.map_dbt_to_tessallite",
        return_value=mapped,
    ):
        async with await _client() as ac:
            resp = await ac.post(
                f"{DBT_PREFIX}/dbt?dry_run=true", files=_upload(_VALID_DBT)
            )

    assert resp.status_code == 200, resp.text
    warning = resp.json()["warnings"][0]
    assert warning["code"] == "legacy.warning"
    assert warning["source"] == "dbt"
    assert warning["element"] is None
    assert warning["action"] == "review"
    assert warning["params"] == {}
    assert warning["detail"] == "A dbt measure was imported as disabled; review it."


@pytest.mark.asyncio
async def test_cube_dry_run_persists_nothing(admin_client):
    cube_yaml = textwrap.dedent("""\
        cubes:
          - name: orders
            sql_table: public.orders
            measures:
              - name: total
                type: sum
                sql: amount
            dimensions:
              - name: region
                type: string
                sql: region
    """)
    open_db = MagicMock(side_effect=AssertionError("get_tenant_db must not be opened on dry_run"))

    with patch("src.api.cube_import.get_tenant_db", open_db):
        async with await _client() as ac:
            resp = await ac.post(
                f"{DBT_PREFIX}/cube?dry_run=true", files=_upload(cube_yaml)
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["models_created"] == 0
    open_db.assert_not_called()
