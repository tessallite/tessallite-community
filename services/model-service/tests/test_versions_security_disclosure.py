"""Bug-7844 [SECURITY]: viewer-facing version get/diff must not disclose the
tenant's access-control policy embedded in the model snapshot.

The model snapshot stored on every ``ModelVersion`` embeds the full governance
configuration — row-security predicates and their claim/dimension/mapping-table
wiring (``row_security_rules``), the column-level-security classification
(``data_tags`` + ``persona_tag_restrictions``), and personas whose
``default_filters`` are data-scoping values. ``GET /versions/{v}`` and
``GET /versions/{a}/diff/{b}`` are viewer-readable (a viewer legitimately views
model version history/shape in the read-only Model Builder), so the security
tables must be REDACTED from the viewer response — never disclosed — while a
modeler+ still receives the full snapshot.

This is the same disclosure class already closed for the export bundle
(Bug-7300) and the direct row-security routes (Bug-7807); here it is fixed at
the RESPONSE boundary, on a copy, so the STORED snapshot and the
deploy/rehydrate path keep every table intact.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import Model, ModelVersion

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"

# The exact predicate string a viewer must never be able to read back.
_SECRET_PREDICATE = "region = claim('region')"


def _governance_snapshot(version_id: uuid.UUID, number: int) -> dict:
    """A snapshot carrying every security-sensitive table plus benign content."""
    return {
        "schema_version": 3,
        "model": {"id": str(TEST_MODEL_ID)},
        # Benign, non-security content a viewer legitimately sees.
        "measures": [{"id": str(uuid.uuid4()), "slug": "revenue"}],
        "dimensions": [{"id": str(uuid.uuid4()), "slug": "geography"}],
        # ---- security-sensitive tables (must be redacted for a viewer) ----
        "row_security_rules": [
            {
                "id": str(uuid.uuid4()),
                "predicate_expression": _SECRET_PREDICATE,
                "applies_to_roles": ["viewer"],
                "claim_name": "region",
            }
        ],
        "data_tags": [
            {"id": str(uuid.uuid4()), "slug": "pii", "column_ids": [str(uuid.uuid4())]}
        ],
        "persona_tag_restrictions": [
            {"id": str(uuid.uuid4()), "persona_id": str(uuid.uuid4())}
        ],
        "personas": [
            {
                "id": str(uuid.uuid4()),
                "slug": "sales-eu",
                "default_filters": {"region": "EU"},
            }
        ],
    }


def _version(version_id: uuid.UUID, number: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=version_id,
        model_id=TEST_MODEL_ID,
        version_number=number,
        summary=None,
        snapshot_json=_governance_snapshot(version_id, number),
        created_at=NOW,
        created_by="user@example.com",
    )


def _wire_db(model, *versions):
    """Return a mock db whose .get dispatches Model / ModelVersion by id."""
    db = make_mock_db()
    by_id = {v.id: v for v in versions}

    async def _get(cls, obj_id):
        if cls is Model:
            return model
        if cls is ModelVersion:
            return by_id.get(obj_id)
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


_SENSITIVE_KEYS = (
    "row_security_rules",
    "data_tags",
    "persona_tag_restrictions",
    "personas",
)


# ---------------------------------------------------------------------------
# get_version
# ---------------------------------------------------------------------------

class TestGetVersionRedaction:
    @pytest.mark.asyncio
    async def test_viewer_response_omits_security_tables(self, client):
        """A viewer (non-modeler) gets the version but NOT the security tables,
        and the predicate string never appears anywhere in the response body."""
        model = make_model()
        v = _version(uuid.uuid4(), 1)
        db = _wire_db(model, v)

        with patch("src.api.versions.get_tenant_db", async_gen_from(db)), patch(
            "src.api.versions._ensure_model_access",
            AsyncMock(return_value=model),
        ), patch(
            "src.api.versions.caller_has_role",
            AsyncMock(return_value=False),  # viewer
        ):
            resp = await client.get(f"{PREFIX}/versions/{v.id}")

        assert resp.status_code == 200, resp.text
        snap = resp.json()["snapshot"]
        # Benign content is still visible — the viewer keeps version visibility.
        assert snap["measures"] and snap["dimensions"]
        # Every security-sensitive table is absent.
        for key in _SENSITIVE_KEYS:
            assert key not in snap, f"{key} leaked to viewer"
        # Defence in depth: the predicate string must not appear via ANY nested
        # key either.
        assert _SECRET_PREDICATE not in resp.text

    @pytest.mark.asyncio
    async def test_modeler_response_keeps_full_snapshot(self, client):
        """A modeler+ receives the complete snapshot including the predicates."""
        model = make_model()
        v = _version(uuid.uuid4(), 1)
        db = _wire_db(model, v)

        with patch("src.api.versions.get_tenant_db", async_gen_from(db)), patch(
            "src.api.versions._ensure_model_access",
            AsyncMock(return_value=model),
        ), patch(
            "src.api.versions.caller_has_role",
            AsyncMock(return_value=True),  # modeler+
        ):
            resp = await client.get(f"{PREFIX}/versions/{v.id}")

        assert resp.status_code == 200, resp.text
        snap = resp.json()["snapshot"]
        for key in _SENSITIVE_KEYS:
            assert key in snap, f"{key} missing for modeler"
        assert snap["row_security_rules"][0]["predicate_expression"] == _SECRET_PREDICATE

    @pytest.mark.asyncio
    async def test_stored_snapshot_is_not_mutated(self, client):
        """Redaction operates on a copy: the stored snapshot_json still carries
        every security table after a viewer read (deploy/rehydrate integrity)."""
        model = make_model()
        v = _version(uuid.uuid4(), 1)
        db = _wire_db(model, v)

        with patch("src.api.versions.get_tenant_db", async_gen_from(db)), patch(
            "src.api.versions._ensure_model_access",
            AsyncMock(return_value=model),
        ), patch(
            "src.api.versions.caller_has_role",
            AsyncMock(return_value=False),  # viewer
        ):
            await client.get(f"{PREFIX}/versions/{v.id}")

        for key in _SENSITIVE_KEYS:
            assert key in v.snapshot_json, f"stored snapshot lost {key}"
        assert (
            v.snapshot_json["row_security_rules"][0]["predicate_expression"]
            == _SECRET_PREDICATE
        )


# ---------------------------------------------------------------------------
# diff_versions
# ---------------------------------------------------------------------------

class TestDiffVersionsRedaction:
    @pytest.mark.asyncio
    async def test_viewer_diff_never_surfaces_predicates(self, client):
        """A viewer diff must not surface any row-security predicate, CLS tag, or
        persona filter as an added/removed/changed entry."""
        model = make_model()
        va = _version(uuid.uuid4(), 1)
        vb = _version(uuid.uuid4(), 2)
        # Make B's predicate DIFFERENT so an unredacted diff WOULD emit a change.
        vb.snapshot_json["row_security_rules"][0]["predicate_expression"] = (
            "region = claim('other')"
        )
        db = _wire_db(model, va, vb)

        with patch("src.api.versions.get_tenant_db", async_gen_from(db)), patch(
            "src.api.versions._ensure_model_access",
            AsyncMock(return_value=model),
        ), patch(
            "src.api.versions.caller_has_role",
            AsyncMock(return_value=False),  # viewer
        ):
            resp = await client.get(f"{PREFIX}/versions/{va.id}/diff/{vb.id}")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Neither predicate string may appear anywhere in the diff payload.
        assert _SECRET_PREDICATE not in resp.text
        assert "region = claim('other')" not in resp.text
        # The security categories must be empty (both sides redacted → no diff).
        diff = body["diff"]
        for key in _SENSITIVE_KEYS:
            cat = diff.get(key)
            if cat is None:
                continue
            assert not cat.get("added")
            assert not cat.get("removed")
            assert not cat.get("changed")

    @pytest.mark.asyncio
    async def test_modeler_diff_reports_predicate_change(self, client):
        """A modeler+ diff reports the row-security predicate change (full
        authoring visibility)."""
        model = make_model()
        va = _version(uuid.uuid4(), 1)
        vb = _version(uuid.uuid4(), 2)
        vb.snapshot_json["row_security_rules"] = [
            dict(
                va.snapshot_json["row_security_rules"][0],
                predicate_expression="region = claim('other')",
            )
        ]
        db = _wire_db(model, va, vb)

        with patch("src.api.versions.get_tenant_db", async_gen_from(db)), patch(
            "src.api.versions._ensure_model_access",
            AsyncMock(return_value=model),
        ), patch(
            "src.api.versions.caller_has_role",
            AsyncMock(return_value=True),  # modeler+
        ):
            resp = await client.get(f"{PREFIX}/versions/{va.id}/diff/{vb.id}")

        assert resp.status_code == 200, resp.text
        changed = resp.json()["diff"]["row_security_rules"]["changed"]
        assert changed, "modeler diff must report the predicate change"
        assert "region = claim('other')" in resp.text


# ---------------------------------------------------------------------------
# redaction helper unit behaviour
# ---------------------------------------------------------------------------

class TestRedactHelper:
    def test_returns_copy_without_sensitive_keys(self):
        from src.api.versions import _redact_security_tables

        snap = _governance_snapshot(uuid.uuid4(), 1)
        out = _redact_security_tables(snap)
        assert out is not snap  # copy, not the same object
        for key in _SENSITIVE_KEYS:
            assert key not in out
            assert key in snap  # original untouched
        assert out["measures"] == snap["measures"]

    def test_non_dict_passthrough(self):
        from src.api.versions import _redact_security_tables

        assert _redact_security_tables(None) is None
        assert _redact_security_tables("legacy") == "legacy"
