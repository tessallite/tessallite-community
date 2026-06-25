"""Regression tests for glossary semantic-layer fixes.

Covers:
  - Phase 3 reject endpoint is a soft delete (status flips to 'rejected',
    row is preserved for audit).
  - Phase 3 created_by is coerced from the CurrentUser string id into a
    UUID when possible, otherwise None.
  - Phase 4 share token is stamped with a jti claim tied to the registry
    and the revocation flow sets revoked_at on the row.
  - Hard-delete endpoint removes entry and version chain.
  - Bootstrap skips rejected entries.
  - Bootstrap does not overwrite existing LLM entries when LLM fails.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from src.api.glossary import (
    _JOB_RETENTION_PER_MODEL,
    _PUBLIC_TOKEN_PURPOSE,
    _cascade_hidden_to_columns,
    _coerce_user_uuid,
    _decode_public_token,
    _deserialize_job_result,
    _heuristic_definition_for_dimension,
    _heuristic_definition_for_measure,
    _issue_public_token,
    _refresh_source_statistics_background,
    _sample_values_from_item_stats,
    _serialize_job_result,
    _sweep_bootstrap_jobs,
)


def test_coerce_user_uuid_none_for_system_caller():
    assert _coerce_user_uuid(None) is None
    assert _coerce_user_uuid("__system__") is None
    assert _coerce_user_uuid("") is None


def test_coerce_user_uuid_for_string_uuid():
    uid = uuid.uuid4()
    assert _coerce_user_uuid(str(uid)) == uid


def test_coerce_user_uuid_none_for_non_uuid_string():
    # Emails and other arbitrary strings are not UUIDs; we don't raise,
    # we simply return None so the row still persists.
    assert _coerce_user_uuid("jane@example.com") is None


def test_public_token_round_trip_carries_jti():
    tenant = "demo"
    model_id = uuid.uuid4()
    jti = uuid.uuid4()
    token = _issue_public_token(tenant, model_id, jti)

    decoded_tenant, decoded_model, decoded_jti = _decode_public_token(token)
    assert decoded_tenant == tenant
    assert decoded_model == model_id
    assert decoded_jti == jti


def test_public_token_rejects_wrong_purpose():
    from jose import jwt

    from shared.config.settings import get_settings

    settings = get_settings()
    payload = {
        "purpose": "something_else",
        "tenant_id": "demo",
        "model_id": str(uuid.uuid4()),
        "jti": str(uuid.uuid4()),
    }
    bad_token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    with pytest.raises(Exception):
        _decode_public_token(bad_token)


def test_public_token_rejects_missing_jti():
    from jose import jwt

    from shared.config.settings import get_settings

    settings = get_settings()
    payload = {
        "purpose": _PUBLIC_TOKEN_PURPOSE,
        "tenant_id": "demo",
        "model_id": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    with pytest.raises(Exception):
        _decode_public_token(token)


# ---------------------------------------------------------------------------
# Heuristic definition regression tests (Round 2)
# ---------------------------------------------------------------------------

class _FakeDimension:
    def __init__(self, name: str, display_name: str | None = None, is_time_dim: bool = False):
        self.name = name
        self.display_name = display_name
        self.is_time_dim = is_time_dim


class _FakeMeasure:
    def __init__(self, name: str, display_name: str | None = None, default_agg: str = "sum"):
        self.name = name
        self.display_name = display_name
        self.default_agg = default_agg


def test_sample_values_from_item_stats_bounds_and_sorts_values():
    item = {"sample_values": ["EMEA", "APAC", "EMEA", None]}

    assert _sample_values_from_item_stats(item, 5) == ["APAC", "EMEA"]
    assert _sample_values_from_item_stats(item, 1) is None
    assert _sample_values_from_item_stats(None, 5) is None


@pytest.mark.asyncio
async def test_refresh_source_statistics_background_calls_optimizer():
    source_id = uuid.uuid4()

    with patch("src.api.glossary.httpx.AsyncClient") as mock_httpx_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = ""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls.return_value = mock_client

        await _refresh_source_statistics_background(
            source_ids=[source_id],
            bearer="test-token",
            low_cardinality_threshold=25,
        )

    mock_client.post.assert_awaited_once()
    kwargs = mock_client.post.await_args.kwargs
    assert kwargs["params"] == {"low_cardinality_threshold": 25}
    assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert f"/api/v1/sources/{source_id}/statistics/refresh" in mock_client.post.await_args.args[0]


class TestHeuristicDefinitionQuality:

    def test_dimension_definition_is_neutral(self):
        dim = _FakeDimension("country")
        defn = _heuristic_definition_for_dimension(dim)
        assert "physical" not in defn.lower()
        assert "underlying column" not in defn.lower()

    def test_measure_definition_no_underlying_column(self):
        meas = _FakeMeasure("total_revenue", default_agg="sum")
        defn = _heuristic_definition_for_measure(meas)
        assert "underlying column" not in defn.lower()


# ---------------------------------------------------------------------------
# Hard-delete endpoint tests
# ---------------------------------------------------------------------------

class TestDeleteEntry:
    """DELETE /projects/{p}/models/{m}/glossary/{eid} removes the entry."""

    @pytest.fixture
    def _ids(self):
        return {
            "project_id": uuid.uuid4(),
            "model_id": uuid.uuid4(),
            "entry_id": uuid.uuid4(),
        }

    def _make_entry(self, entry_id, model_id, *, status="pending_review",
                    source="heuristic", superseded_by=None):
        return types.SimpleNamespace(
            id=entry_id,
            model_id=model_id,
            status=status,
            source=source,
            superseded_by=superseded_by,
            synonyms=[],
            attachments=[],
        )

    @pytest.mark.asyncio
    async def test_delete_returns_204(self, client, _ids):
        entry = self._make_entry(_ids["entry_id"], _ids["model_id"])
        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=entry)
        mock_db.delete = AsyncMock()
        mock_db.commit = AsyncMock()
        exec_result = MagicMock()
        exec_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=exec_result)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/{_ids['entry_id']}"
            )
            resp = await client.request("DELETE", url)

        assert resp.status_code == 204
        mock_db.delete.assert_called()

    @pytest.mark.asyncio
    async def test_delete_404_for_missing_entry(self, client, _ids):
        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=None)

        exec_result = MagicMock()
        exec_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=exec_result)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/{_ids['entry_id']}"
            )
            resp = await client.request("DELETE", url)

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_removes_version_chain(self, client, _ids):
        """When an entry has older versions linked via superseded_by, all are deleted."""
        old_id = uuid.uuid4()
        entry = self._make_entry(_ids["entry_id"], _ids["model_id"])
        old_entry = self._make_entry(old_id, _ids["model_id"])

        mock_db = AsyncMock()
        mock_db.delete = AsyncMock()
        mock_db.commit = AsyncMock()

        def _get_side_effect(model_cls, eid):
            if eid == _ids["entry_id"]:
                return entry
            if eid == old_id:
                return old_entry
            return None

        mock_db.get = AsyncMock(side_effect=_get_side_effect)

        r1 = MagicMock()
        r1.all.return_value = [(old_id,)]
        r2 = MagicMock()
        r2.all.return_value = []
        mock_db.execute = AsyncMock(side_effect=[r1, r2])

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/{_ids['entry_id']}"
            )
            resp = await client.request("DELETE", url)

        assert resp.status_code == 204
        assert mock_db.delete.call_count == 2

    @pytest.mark.asyncio
    async def test_delete_removes_deep_version_chain(self, client, _ids):
        """v1 -> v2 -> v3: deleting v3 must delete all three entries."""
        v1_id = uuid.uuid4()
        v2_id = uuid.uuid4()
        v3_id = _ids["entry_id"]
        entries = {
            v3_id: self._make_entry(v3_id, _ids["model_id"]),
            v2_id: self._make_entry(v2_id, _ids["model_id"]),
            v1_id: self._make_entry(v1_id, _ids["model_id"]),
        }

        mock_db = AsyncMock()
        mock_db.delete = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.get = AsyncMock(side_effect=lambda cls, eid: entries.get(eid))

        r1 = MagicMock()
        r1.all.return_value = [(v2_id,)]
        r2 = MagicMock()
        r2.all.return_value = [(v1_id,)]
        r3 = MagicMock()
        r3.all.return_value = []
        mock_db.execute = AsyncMock(side_effect=[r1, r2, r3])

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/{_ids['entry_id']}"
            )
            resp = await client.request("DELETE", url)

        assert resp.status_code == 204
        assert mock_db.delete.call_count == 3

    @pytest.mark.asyncio
    async def test_delete_superseded_entry_returns_409(self, client, _ids):
        """Deleting a non-latest (superseded) entry must return 409."""
        successor_id = uuid.uuid4()
        entry = self._make_entry(
            _ids["entry_id"], _ids["model_id"], superseded_by=successor_id,
        )
        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=entry)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/{_ids['entry_id']}"
            )
            resp = await client.request("DELETE", url)

        assert resp.status_code == 409
        mock_db.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_cycle_guard_terminates(self, client, _ids):
        """Cyclic predecessor chain must not loop; visited set breaks the cycle."""
        cycle_id = uuid.uuid4()
        entry = self._make_entry(_ids["entry_id"], _ids["model_id"])
        cycle_entry = self._make_entry(cycle_id, _ids["model_id"])

        mock_db = AsyncMock()
        mock_db.delete = AsyncMock()
        mock_db.commit = AsyncMock()

        def _get_side_effect(cls, eid):
            if eid == _ids["entry_id"]:
                return entry
            if eid == cycle_id:
                return cycle_entry
            return None

        mock_db.get = AsyncMock(side_effect=_get_side_effect)

        # cycle_id points back to entry_id as predecessor, creating a loop
        r1 = MagicMock()
        r1.all.return_value = [(cycle_id,)]
        r2 = MagicMock()
        r2.all.return_value = [(_ids["entry_id"],)]  # would loop without visited guard
        r3 = MagicMock()
        r3.all.return_value = []
        mock_db.execute = AsyncMock(side_effect=[r1, r2, r3])

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/{_ids['entry_id']}"
            )
            resp = await client.request("DELETE", url)

        assert resp.status_code == 204
        # Only 2 deletes: entry + cycle_id. The cycle back to entry_id is skipped.
        assert mock_db.delete.call_count == 2


# ---------------------------------------------------------------------------
# Bootstrap regression tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# create_entry / import defaulting tests
# ---------------------------------------------------------------------------

class TestCreateEntryDefaults:
    """create_entry() must set visibility=show, confidence=high when omitted."""

    @pytest.mark.asyncio
    async def test_create_entry_defaults_visibility_and_confidence(self, client):
        project_id = uuid.uuid4()
        model_id = uuid.uuid4()
        entry_id = uuid.uuid4()

        captured_entries = []

        mock_db = MagicMock()
        mock_db.add = MagicMock(side_effect=lambda obj: captured_entries.append(obj))
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        reload_entry = types.SimpleNamespace(
            id=entry_id,
            model_id=model_id,
            term="Revenue",
            definition="Total revenue",
            context_notes=None,
            source="user",
            status="approved",
            version=1,
            superseded_by=None,
            created_by=None,
            proposed_is_hidden=None,
            visibility="show",
            confidence="high",
            sample_values=None,
            created_at="2026-05-10T00:00:00Z",
            updated_at="2026-05-10T00:00:00Z",
            synonyms=[],
            attachments=[],
        )
        reload_result = MagicMock()
        reload_result.scalar_one.return_value = reload_entry
        mock_db.execute = AsyncMock(return_value=reload_result)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = f"/api/v1/projects/{project_id}/models/{model_id}/glossary"
            resp = await client.post(url, json={
                "term": "Revenue",
                "definition": "Total revenue",
                "target_type": "measure",
                "target_id": str(uuid.uuid4()),
            })

        assert resp.status_code == 201
        glossary_entries = [
            e for e in captured_entries
            if hasattr(e, "visibility") and hasattr(e, "confidence") and hasattr(e, "term")
        ]
        assert len(glossary_entries) >= 1
        entry = glossary_entries[0]
        assert entry.visibility == "show"
        assert entry.confidence == "high"

    @pytest.mark.asyncio
    async def test_create_entry_respects_explicit_visibility(self, client):
        project_id = uuid.uuid4()
        model_id = uuid.uuid4()
        entry_id = uuid.uuid4()

        captured_entries = []

        mock_db = MagicMock()
        mock_db.add = MagicMock(side_effect=lambda obj: captured_entries.append(obj))
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        reload_entry = types.SimpleNamespace(
            id=entry_id,
            model_id=model_id,
            term="Secret",
            definition="Secret metric",
            context_notes=None,
            source="user",
            status="approved",
            version=1,
            superseded_by=None,
            created_by=None,
            proposed_is_hidden=None,
            visibility="hide",
            confidence="medium",
            sample_values=None,
            created_at="2026-05-10T00:00:00Z",
            updated_at="2026-05-10T00:00:00Z",
            synonyms=[],
            attachments=[],
        )
        reload_result = MagicMock()
        reload_result.scalar_one.return_value = reload_entry
        mock_db.execute = AsyncMock(return_value=reload_result)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = f"/api/v1/projects/{project_id}/models/{model_id}/glossary"
            resp = await client.post(url, json={
                "term": "Secret",
                "definition": "Secret metric",
                "target_type": "measure",
                "target_id": str(uuid.uuid4()),
                "visibility": "hide",
                "confidence": "medium",
            })

        assert resp.status_code == 201
        glossary_entries = [
            e for e in captured_entries
            if hasattr(e, "visibility") and hasattr(e, "term") and getattr(e, "term", None) == "Secret"
        ]
        assert len(glossary_entries) >= 1
        entry = glossary_entries[0]
        assert entry.visibility == "hide"
        assert entry.confidence == "medium"


class TestUpdateVisibilityConfidence:
    """PATCH /{entry_id} with visibility/confidence fields."""

    @pytest.fixture
    def _ids(self):
        return {
            "project_id": uuid.uuid4(),
            "model_id": uuid.uuid4(),
            "entry_id": uuid.uuid4(),
        }

    def _make_existing(self, entry_id, model_id, *,
                       visibility="review", confidence="low"):
        entry = types.SimpleNamespace(
            id=entry_id,
            model_id=model_id,
            term="Revenue",
            definition="Total revenue",
            context_notes=None,
            source="user",
            status="approved",
            version=1,
            superseded_by=None,
            created_by=None,
            proposed_is_hidden=None,
            visibility=visibility,
            confidence=confidence,
            sample_values=None,
            synonyms=[],
            attachments=[],
        )
        return entry

    @pytest.mark.asyncio
    async def test_update_glossary_visibility_confidence(self, client, _ids):
        existing = self._make_existing(_ids["entry_id"], _ids["model_id"])
        captured = []

        mock_db = AsyncMock()
        mock_db.add = MagicMock(side_effect=lambda obj: captured.append(obj))
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()

        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = existing

        reload_entry = types.SimpleNamespace(
            id=uuid.uuid4(),
            model_id=_ids["model_id"],
            term="Revenue",
            definition="Total revenue",
            context_notes=None,
            source="user",
            status="approved",
            version=2,
            superseded_by=None,
            created_by=None,
            proposed_is_hidden=None,
            visibility="hide",
            confidence="low",
            sample_values=None,
            created_at="2026-05-11T00:00:00Z",
            updated_at="2026-05-11T00:00:00Z",
            synonyms=[],
            attachments=[],
        )
        reload_result = MagicMock()
        reload_result.scalar_one.return_value = reload_entry
        mock_db.execute = AsyncMock(side_effect=[exec_result, reload_result])

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/{_ids['entry_id']}"
            )
            resp = await client.patch(url, json={
                "visibility": "hide",
                "confidence": "low",
            })

        assert resp.status_code == 200
        glossary_entries = [
            e for e in captured
            if hasattr(e, "visibility") and hasattr(e, "confidence")
        ]
        assert len(glossary_entries) >= 1
        entry = glossary_entries[0]
        assert entry.visibility == "hide"
        assert entry.confidence == "low"

    @pytest.mark.asyncio
    async def test_update_glossary_partial_visibility_preserves_confidence(self, client, _ids):
        existing = self._make_existing(
            _ids["entry_id"], _ids["model_id"],
            visibility="review", confidence="medium",
        )
        captured = []

        mock_db = AsyncMock()
        mock_db.add = MagicMock(side_effect=lambda obj: captured.append(obj))
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()

        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = existing

        reload_entry = types.SimpleNamespace(
            id=uuid.uuid4(),
            model_id=_ids["model_id"],
            term="Revenue",
            definition="Total revenue",
            context_notes=None,
            source="user",
            status="approved",
            version=2,
            superseded_by=None,
            created_by=None,
            proposed_is_hidden=None,
            visibility="show",
            confidence="medium",
            sample_values=None,
            created_at="2026-05-11T00:00:00Z",
            updated_at="2026-05-11T00:00:00Z",
            synonyms=[],
            attachments=[],
        )
        reload_result = MagicMock()
        reload_result.scalar_one.return_value = reload_entry
        mock_db.execute = AsyncMock(side_effect=[exec_result, reload_result])

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/{_ids['entry_id']}"
            )
            resp = await client.patch(url, json={
                "visibility": "show",
            })

        assert resp.status_code == 200
        glossary_entries = [
            e for e in captured
            if hasattr(e, "visibility") and hasattr(e, "confidence")
        ]
        assert len(glossary_entries) >= 1
        entry = glossary_entries[0]
        assert entry.visibility == "show"
        assert entry.confidence == "medium"


class TestImportGlossaryCsvDefaults:
    """import_glossary_csv() must set visibility=show, confidence=high."""

    @pytest.mark.asyncio
    async def test_csv_import_defaults_visibility_and_confidence(self, client):
        project_id = uuid.uuid4()
        model_id = uuid.uuid4()

        captured_entries = []

        mock_db = MagicMock()
        mock_db.add = MagicMock(side_effect=lambda obj: captured_entries.append(obj))
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = f"/api/v1/projects/{project_id}/models/{model_id}/glossary/import"
            resp = await client.post(url, json={
                "csv": "term,description\nRevenue,Total revenue\nCost,Total cost",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 2

        glossary_entries = [
            e for e in captured_entries
            if hasattr(e, "visibility") and hasattr(e, "confidence") and hasattr(e, "term")
        ]
        assert len(glossary_entries) == 2
        for entry in glossary_entries:
            assert entry.visibility == "show"
            assert entry.confidence == "high"


class TestBootstrapSkipBehavior:
    """Verify that bootstrap correctly skips rejected and LLM entries."""

    @pytest.fixture
    def _ids(self):
        return {
            "project_id": uuid.uuid4(),
            "model_id": uuid.uuid4(),
        }

    def _make_model(self, model_id):
        return types.SimpleNamespace(
            id=model_id,
            slug="test-model",
            display_name="Test Model",
            description=None,
            glossary_max_distinct=50,
        )

    def _make_dim(self, dim_id, model_id, name="country", source_column_id=None):
        return types.SimpleNamespace(
            id=dim_id,
            model_id=model_id,
            name=name,
            display_name=None,
            is_time_dim=False,
            source_column_id=source_column_id,
        )

    def _make_entry(self, entry_id, model_id, *, status="pending_review",
                    source="heuristic", term="Country", sample_values=None):
        return types.SimpleNamespace(
            id=entry_id,
            model_id=model_id,
            term=term,
            definition="Old definition",
            status=status,
            source=source,
            visibility="review",
            confidence="low",
            sample_values=sample_values,
            synonyms=[],
            attachments=[],
        )

    def _mock_db_for_bootstrap(self, model, dims, existing_map, entries_by_id):
        """Build a mock DB that satisfies the bootstrap query pattern.

        Bootstrap runs these queries in order for the synchronous path:
          1. select ModelTable.source_id -> source ids
          2. select GlossaryAttachment -> existing_entry_map
          3. select Dimension -> dims
          4. select Measure -> empty
          5. select Join -> empty (table relationships)
        """
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.delete = AsyncMock()

        def _get(cls, oid):
            if oid == model.id:
                return model
            return entries_by_id.get(oid)

        mock_db.get = AsyncMock(side_effect=_get)

        r_existing = MagicMock()
        r_existing.all.return_value = existing_map

        r_dims = MagicMock()
        r_dims.scalars.return_value.all.return_value = dims

        r_measures = MagicMock()
        r_measures.scalars.return_value.all.return_value = []

        r_joins = MagicMock()
        r_joins.scalars.return_value.all.return_value = []

        r_sources = MagicMock()
        r_sources.scalars.return_value.all.return_value = []

        mock_db.execute = AsyncMock(
            side_effect=[
                r_sources, r_existing, r_dims, r_measures, r_joins,
                *[MagicMock() for _ in range(10)],
            ],
        )
        return mock_db

    @pytest.mark.asyncio
    async def test_bootstrap_with_sources_queues_statistics_first_job(self, client, _ids, override_auth):
        """The public bootstrap call returns quickly and schedules stats-first generation."""
        override_auth.raw_token = "test-token"
        model = self._make_model(_ids["model_id"])
        source_id = uuid.uuid4()
        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=model)
        r_sources = MagicMock()
        r_sources.scalars.return_value.all.return_value = [source_id]
        mock_db.execute = AsyncMock(return_value=r_sources)

        async def _gen(*a, **kw):
            yield mock_db

        def _close_background_coro(coro):
            coro.close()
            return MagicMock()

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("src.api.glossary.asyncio.create_task", side_effect=_close_background_coro) as create_task,
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/bootstrap"
            )
            resp = await client.post(url)

        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"]
        assert data["job_status"] == "queued"
        assert data["proposed_count"] == 0
        create_task.assert_called_once()


    @pytest.mark.asyncio
    async def test_bootstrap_sends_source_statistics_sample_values_to_llm(self, client, _ids, override_auth):
        """The generation pass uses SourceColumnStatistics, not old glossary samples."""
        override_auth.raw_token = "test-token"
        dim_id = uuid.uuid4()
        column_id = uuid.uuid4()
        table_id = uuid.uuid4()
        model = self._make_model(_ids["model_id"])
        dim = self._make_dim(dim_id, _ids["model_id"], source_column_id=column_id)
        column = types.SimpleNamespace(id=column_id, model_table_id=table_id)
        table = types.SimpleNamespace(id=table_id, alias="customer", physical_name="customer")
        stat = types.SimpleNamespace(
            model_column_id=column_id,
            distinct_count=2,
            null_ratio=0.0,
            min_value=None,
            max_value=None,
            top_values=[{"value": "EMEA"}, {"value": "APAC"}, {"value": "EMEA"}],
        )
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.delete = AsyncMock()

        def _get(cls, oid):
            if oid == model.id:
                return model
            if oid == column_id:
                return column
            if oid == table_id:
                return table
            return None

        mock_db.get = AsyncMock(side_effect=_get)

        r_sources = MagicMock()
        r_sources.scalars.return_value.all.return_value = []
        r_existing = MagicMock()
        r_existing.all.return_value = []
        r_dims = MagicMock()
        r_dims.scalars.return_value.all.return_value = [dim]
        r_measures = MagicMock()
        r_measures.scalars.return_value.all.return_value = []
        r_joins = MagicMock()
        r_joins.scalars.return_value.all.return_value = []
        r_col_rows = MagicMock()
        r_col_rows.all.return_value = [types.SimpleNamespace(id=column_id, model_table_id=table_id)]
        r_stats = MagicMock()
        r_stats.scalars.return_value.all.return_value = [stat]
        mock_db.execute = AsyncMock(
            side_effect=[r_sources, r_existing, r_dims, r_measures, r_joins, r_col_rows, r_stats],
        )

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("src.api.glossary.asyncio.create_task") as create_task,
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {
                "definitions": [{
                    "id": str(dim_id),
                    "definition": "Country where the customer is located.",
                    "synonyms": ["nation"],
                    "confidence": "high",
                }],
                "llm_provider": "openai",
                "llm_model": "gpt-4o",
            }
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx_cls.return_value = mock_client

            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/bootstrap"
            )
            resp = await client.post(url)

        assert resp.status_code == 200
        payload = mock_client.post.await_args.kwargs["json"]
        assert payload["items"][0]["sample_values"] == ["APAC", "EMEA"]
        create_task.assert_not_called()


    @pytest.mark.asyncio
    async def test_bootstrap_refreshes_sample_values_on_skipped_existing_entry(self, client, _ids, override_auth):
        """Approved/user definitions are preserved, but stats-derived samples are refreshed."""
        override_auth.raw_token = "test-token"
        dim_id = uuid.uuid4()
        entry_id = uuid.uuid4()
        column_id = uuid.uuid4()
        table_id = uuid.uuid4()
        model = self._make_model(_ids["model_id"])
        dim = self._make_dim(dim_id, _ids["model_id"], source_column_id=column_id)
        entry = self._make_entry(
            entry_id,
            _ids["model_id"],
            status="approved",
            source="user",
            sample_values=["OLD"],
        )
        column = types.SimpleNamespace(id=column_id, model_table_id=table_id)
        table = types.SimpleNamespace(id=table_id, alias="customer", physical_name="customer")
        stat = types.SimpleNamespace(
            model_column_id=column_id,
            distinct_count=2,
            null_ratio=0.0,
            min_value=None,
            max_value=None,
            top_values=[{"value": "EMEA"}, {"value": "APAC"}],
        )
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.delete = AsyncMock()

        def _get(cls, oid):
            if oid == model.id:
                return model
            if oid == column_id:
                return column
            if oid == table_id:
                return table
            if oid == entry_id:
                return entry
            return None

        mock_db.get = AsyncMock(side_effect=_get)

        r_sources = MagicMock()
        r_sources.scalars.return_value.all.return_value = []
        r_existing = MagicMock()
        r_existing.all.return_value = [("dimension", dim_id, entry_id)]
        r_dims = MagicMock()
        r_dims.scalars.return_value.all.return_value = [dim]
        r_measures = MagicMock()
        r_measures.scalars.return_value.all.return_value = []
        r_joins = MagicMock()
        r_joins.scalars.return_value.all.return_value = []
        r_col_rows = MagicMock()
        r_col_rows.all.return_value = [types.SimpleNamespace(id=column_id, model_table_id=table_id)]
        r_stats = MagicMock()
        r_stats.scalars.return_value.all.return_value = [stat]
        mock_db.execute = AsyncMock(
            side_effect=[r_sources, r_existing, r_dims, r_measures, r_joins, r_col_rows, r_stats],
        )

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {"definitions": [], "llm_provider": "openai", "llm_model": "gpt-4o"}
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx_cls.return_value = mock_client

            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/bootstrap"
            )
            resp = await client.post(url)

        assert resp.status_code == 200
        assert resp.json()["skipped_count"] == 1
        assert entry.definition == "Old definition"
        assert entry.sample_values == ["APAC", "EMEA"]


    @pytest.mark.asyncio
    async def test_bootstrap_skips_rejected_entry(self, client, _ids):
        """A rejected entry must remain rejected after bootstrap re-run."""
        dim_id = uuid.uuid4()
        entry_id = uuid.uuid4()
        model = self._make_model(_ids["model_id"])
        dim = self._make_dim(dim_id, _ids["model_id"])
        entry = self._make_entry(
            entry_id, _ids["model_id"], status="rejected", source="llm",
        )

        existing_map = [("dimension", dim_id, entry_id)]
        mock_db = self._mock_db_for_bootstrap(
            model, [dim], existing_map, {entry_id: entry},
        )

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_resp = MagicMock()
            mock_resp.is_success = True
            mock_resp.json.return_value = {
                "definitions": [{
                    "id": str(dim_id),
                    "definition": "New LLM def",
                    "synonyms": [],
                    "visibility": "show",
                    "confidence": "high",
                }],
                "llm_provider": "openai",
                "llm_model": "gpt-4o",
            }
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx_cls.return_value = mock_client

            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/bootstrap"
            )
            resp = await client.post(url)

        assert resp.status_code == 200
        data = resp.json()
        assert data["proposed_count"] == 0
        assert data["skipped_count"] >= 1
        assert entry.status == "rejected"
        assert entry.definition == "Old definition"

    @pytest.mark.asyncio
    async def test_bootstrap_preserves_llm_entry_on_total_failure(self, client, _ids):
        """When LLM call fails entirely, existing LLM entries must not be overwritten."""
        dim_id = uuid.uuid4()
        entry_id = uuid.uuid4()
        model = self._make_model(_ids["model_id"])
        dim = self._make_dim(dim_id, _ids["model_id"])
        entry = self._make_entry(
            entry_id, _ids["model_id"],
            status="pending_review", source="llm",
        )

        existing_map = [("dimension", dim_id, entry_id)]
        mock_db = self._mock_db_for_bootstrap(
            model, [dim], existing_map, {entry_id: entry},
        )

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("LLM down"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx_cls.return_value = mock_client

            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/bootstrap"
            )
            resp = await client.post(url)

        assert resp.status_code == 200
        data = resp.json()
        assert data["proposed_count"] == 0
        assert data["skipped_count"] >= 1
        assert entry.source == "llm"
        assert entry.definition == "Old definition"

    @pytest.mark.asyncio
    async def test_bootstrap_fallback_count_only_counts_actual_writes(self, client, _ids):
        """fallback_count must not include skipped entries."""
        dim_id = uuid.uuid4()
        entry_id = uuid.uuid4()
        model = self._make_model(_ids["model_id"])
        dim = self._make_dim(dim_id, _ids["model_id"])
        entry = self._make_entry(
            entry_id, _ids["model_id"],
            status="approved", source="user",
        )

        existing_map = [("dimension", dim_id, entry_id)]
        mock_db = self._mock_db_for_bootstrap(
            model, [dim], existing_map, {entry_id: entry},
        )

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("LLM down"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_httpx_cls.return_value = mock_client

            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/bootstrap"
            )
            resp = await client.post(url)

        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback_count"] == 0


class TestBulkDelete:
    """POST /glossary/delete-bulk removes entries by scope."""

    @pytest.fixture
    def _ids(self):
        return {"project_id": uuid.uuid4(), "model_id": uuid.uuid4()}

    def _entry(self, eid, model_id, *, source="heuristic", superseded_by=None):
        return types.SimpleNamespace(
            id=eid, model_id=model_id, source=source,
            superseded_by=superseded_by, synonyms=[], attachments=[],
        )

    @pytest.mark.asyncio
    async def test_invalid_scope_returns_422(self, client, _ids):
        url = (
            f"/api/v1/projects/{_ids['project_id']}"
            f"/models/{_ids['model_id']}/glossary/delete-bulk"
        )
        resp = await client.post(url, json={"scope": "bogus"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_bulk_delete_walks_version_chains(self, client, _ids):
        latest_id = uuid.uuid4()
        old_id = uuid.uuid4()
        latest = self._entry(latest_id, _ids["model_id"])
        old = self._entry(old_id, _ids["model_id"], superseded_by=latest_id)

        mock_db = AsyncMock()
        mock_db.delete = AsyncMock()
        mock_db.commit = AsyncMock()

        def _get(_cls, eid):
            return {latest_id: latest, old_id: old}.get(eid)

        mock_db.get = AsyncMock(side_effect=_get)

        latest_result = MagicMock()
        latest_result.scalars.return_value.all.return_value = [latest]
        chain1 = MagicMock()
        chain1.all.return_value = [(old_id,)]
        chain2 = MagicMock()
        chain2.all.return_value = []
        mock_db.execute = AsyncMock(side_effect=[latest_result, chain1, chain2])

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}/glossary/delete-bulk"
            )
            resp = await client.post(url, json={"scope": "all"})

        assert resp.status_code == 200
        assert resp.json()["deleted_count"] == 2
        assert mock_db.delete.call_count == 2


class TestVisibilityCascade:
    """F-018-01: `proposed_is_hidden` must reach the underlying ModelColumn
    through dimension/measure attachments, not only literal `column` ones.

    The dimension/measure response builders and the gateway catalog derive
    visibility from ModelColumn.is_hidden, so flipping that column is what
    actually hides the field everywhere it surfaces."""

    def _fake_db(self, *, dim=None, measure=None, columns=None):
        from shared.db.models import Dimension, Measure, ModelColumn
        columns = columns or {}

        async def _get(cls, oid):
            if cls is Dimension and dim is not None and oid == dim.id:
                return dim
            if cls is Measure and measure is not None and oid == measure.id:
                return measure
            if cls is ModelColumn:
                return columns.get(oid)
            return None

        db = AsyncMock()
        db.get = AsyncMock(side_effect=_get)
        return db

    @pytest.mark.asyncio
    async def test_dimension_attachment_hides_source_column(self):
        col_id = uuid.uuid4()
        col = types.SimpleNamespace(id=col_id, is_hidden=False)
        dim = types.SimpleNamespace(id=uuid.uuid4(), source_column_id=col_id)
        att = types.SimpleNamespace(target_type="dimension", target_id=dim.id)
        db = self._fake_db(dim=dim, columns={col_id: col})

        changed = await _cascade_hidden_to_columns(db, [att], True)

        assert changed == 1
        assert col.is_hidden is True

    @pytest.mark.asyncio
    async def test_measure_attachment_hides_source_column(self):
        col_id = uuid.uuid4()
        col = types.SimpleNamespace(id=col_id, is_hidden=False)
        meas = types.SimpleNamespace(id=uuid.uuid4(), source_column_id=col_id)
        att = types.SimpleNamespace(target_type="measure", target_id=meas.id)
        db = self._fake_db(measure=meas, columns={col_id: col})

        changed = await _cascade_hidden_to_columns(db, [att], True)

        assert changed == 1
        assert col.is_hidden is True

    @pytest.mark.asyncio
    async def test_cascade_unhides_when_false(self):
        col_id = uuid.uuid4()
        col = types.SimpleNamespace(id=col_id, is_hidden=True)
        dim = types.SimpleNamespace(id=uuid.uuid4(), source_column_id=col_id)
        att = types.SimpleNamespace(target_type="dimension", target_id=dim.id)
        db = self._fake_db(dim=dim, columns={col_id: col})

        changed = await _cascade_hidden_to_columns(db, [att], False)

        assert changed == 1
        assert col.is_hidden is False

    @pytest.mark.asyncio
    async def test_literal_column_attachment_still_handled(self):
        col_id = uuid.uuid4()
        col = types.SimpleNamespace(id=col_id, is_hidden=False)
        att = types.SimpleNamespace(target_type="column", target_id=col_id)
        db = self._fake_db(columns={col_id: col})

        changed = await _cascade_hidden_to_columns(db, [att], True)

        assert changed == 1
        assert col.is_hidden is True

    @pytest.mark.asyncio
    async def test_dimension_without_source_column_is_noop(self):
        dim = types.SimpleNamespace(id=uuid.uuid4(), source_column_id=None)
        att = types.SimpleNamespace(target_type="dimension", target_id=dim.id)
        db = self._fake_db(dim=dim)

        changed = await _cascade_hidden_to_columns(db, [att], True)

        assert changed == 0


class TestDurableBootstrapJobRegistry:
    """F-018-03: bootstrap job status is persisted to the per-tenant DB so any
    replica can answer a poll and the table stays bounded (TTL + per-model cap)."""

    @pytest.fixture
    def _ids(self):
        return {"project_id": uuid.uuid4(), "model_id": uuid.uuid4(),
                "job_id": uuid.uuid4()}

    def test_job_result_round_trips_through_jsonb(self):
        from shared.schemas.pydantic_models import GlossaryBootstrapResponse
        job_id = uuid.uuid4()
        resp = GlossaryBootstrapResponse(
            proposed_count=3, job_id=job_id, job_status="completed",
            message="done", used_llm=True, fallback_count=1,
        )
        stored = _serialize_job_result(resp)
        assert isinstance(stored, dict)
        back = _deserialize_job_result(stored)
        assert back is not None
        assert back.proposed_count == 3
        assert back.job_status == "completed"
        assert back.job_id == job_id

    def test_deserialize_handles_garbage(self):
        assert _deserialize_job_result(None) is None
        assert _deserialize_job_result("not-a-dict") is None

    @pytest.mark.asyncio
    async def test_sweep_bounds_table_by_ttl_and_per_model_cap(self, _ids):
        """The sweep deletes expired rows and trims per-model history."""
        from shared.db.models import GlossaryBootstrapJob

        keep_ids = [uuid.uuid4() for _ in range(_JOB_RETENTION_PER_MODEL)]
        deletes = []

        keep_result = MagicMock()
        keep_result.scalars.return_value.all.return_value = keep_ids

        async def _execute(stmt):
            # The select for keep-ids returns the recent rows; deletes return a
            # rowcount-like result we don't inspect.
            cls = getattr(getattr(stmt, "column_descriptions", [{}]), "__len__", lambda: 0)
            text = str(stmt).lower()
            if text.startswith("select"):
                return keep_result
            deletes.append(text)
            return MagicMock()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)

        await _sweep_bootstrap_jobs(db, _ids["model_id"])

        # Two DELETEs: one TTL-based, one per-model trim.
        assert len(deletes) == 2
        assert all("glossary_bootstrap_jobs" in d for d in deletes)

    @pytest.mark.asyncio
    async def test_poll_reads_job_from_db_so_second_replica_answers(self, client, _ids):
        """The poll endpoint reads the durable row via db.get, so a request that
        lands on a replica that never ran the job still gets the status (not a
        404 that wedges the UI). Simulated by a fresh session returning the row."""
        from shared.schemas.pydantic_models import GlossaryBootstrapResponse

        completed = GlossaryBootstrapResponse(
            proposed_count=5, job_id=_ids["job_id"], job_status="completed",
            message="Glossary bootstrap completed.",
        )
        job_row = types.SimpleNamespace(
            id=_ids["job_id"],
            project_id=_ids["project_id"],
            model_id=_ids["model_id"],
            status="completed",
            message="Glossary bootstrap completed.",
            result=_serialize_job_result(completed),
        )

        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=job_row)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/bootstrap/jobs/{_ids['job_id']}"
            )
            resp = await client.get(url)

        assert resp.status_code == 200
        data = resp.json()
        assert data["job_status"] == "completed"
        assert data["proposed_count"] == 5

    @pytest.mark.asyncio
    async def test_poll_404_for_wrong_project(self, client, _ids):
        """A job that belongs to a different project must 404 (scope check)."""
        job_row = types.SimpleNamespace(
            id=_ids["job_id"],
            project_id=uuid.uuid4(),  # different project
            model_id=_ids["model_id"],
            status="queued",
            message=None,
            result=None,
        )
        mock_db = AsyncMock()
        mock_db.get = AsyncMock(return_value=job_row)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}"
                f"/glossary/bootstrap/jobs/{_ids['job_id']}"
            )
            resp = await client.get(url)

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# ML17 fixes
# ---------------------------------------------------------------------------

from src.api.glossary import _formula_guard, _pdf_escape  # noqa: E402


def test_formula_guard_neutralises_leading_formula_chars():
    """F-018-10: cells starting with = + - @ (or tab/CR) get a leading quote so
    the spreadsheet treats them as text, not an executing formula."""
    for trigger in ("=HYPERLINK(1)", "+1", "-1", "@SUM", "\t=x", "\rx"):
        out = _formula_guard(trigger)
        assert out.startswith("'"), trigger
    # Ordinary text is untouched.
    assert _formula_guard("Total revenue") == "Total revenue"
    assert _formula_guard(None) == ""


def test_pdf_escape_neutralises_markup():
    """F-018-09: < & > in glossary text must be XML-escaped before they reach a
    ReportLab Paragraph, otherwise the PDF build raises and 500s."""
    out = _pdf_escape("margin < 5% & rising <b>bold</b>")
    assert "&lt;" in out
    assert "&amp;" in out
    assert "<b>" not in out


def test_heuristic_measure_additivity_varies_by_agg():
    """F-018-14: only sum/count are described as additive; avg and
    count_distinct are non-additive; min/max are semi-additive."""
    def _meas(agg):
        return types.SimpleNamespace(display_name=None, name="m", default_agg=agg)

    assert "additive across the model's grain." in _heuristic_definition_for_measure(_meas("sum"))
    assert "non-additive" in _heuristic_definition_for_measure(_meas("avg"))
    assert "non-additive" in _heuristic_definition_for_measure(_meas("count_distinct"))
    assert "semi-additive" in _heuristic_definition_for_measure(_meas("max"))
    # The wrong blanket assertion is gone for non-sum aggs.
    assert "additive across the model's grain." not in _heuristic_definition_for_measure(_meas("avg"))


class TestBulkApprove:
    """POST /glossary/approve-bulk approves visible pending proposals in one call (F-018-22)."""

    @pytest.fixture
    def _ids(self):
        return {"project_id": uuid.uuid4(), "model_id": uuid.uuid4()}

    @pytest.mark.asyncio
    async def test_bulk_approve_updates_sources_and_visibility(self, client, _ids):
        column_id = uuid.uuid4()
        col = types.SimpleNamespace(id=column_id, is_hidden=False)
        entry_llm = types.SimpleNamespace(
            id=uuid.uuid4(),
            model_id=_ids["model_id"],
            status="pending_review",
            source="llm",
            created_by=None,
            proposed_is_hidden=True,
            attachments=[
                types.SimpleNamespace(
                    target_type="column",
                    target_id=column_id,
                )
            ],
        )
        entry_heuristic = types.SimpleNamespace(
            id=uuid.uuid4(),
            model_id=_ids["model_id"],
            status="pending_review",
            source="heuristic",
            created_by=None,
            proposed_is_hidden=None,
            attachments=[],
        )

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.get = AsyncMock(return_value=col)
        result = MagicMock()
        result.scalars.return_value.all.return_value = [entry_llm, entry_heuristic]
        mock_db.execute = AsyncMock(return_value=result)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}/glossary/approve-bulk"
            )
            resp = await client.post(url)

        assert resp.status_code == 200
        assert resp.json() == {"approved_count": 2}
        assert entry_llm.status == "approved"
        assert entry_llm.source == "llm_approved"
        assert entry_heuristic.status == "approved"
        assert entry_heuristic.source == "heuristic"
        assert col.is_hidden is True
        mock_db.commit.assert_awaited_once()
