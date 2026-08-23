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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx


def _route_bootstrap_execute(*, sources, existing, dims, measures, joins,
                             col_rows=None, stats=None):
    """Order-independent db.execute router for glossary bootstrap tests.

    Bug-7982 R6 (round 2, BLOCKER): the create-vs-update decision map
    (GlossaryAttachment query) moved from BEFORE the LLM call to AFTER the lock,
    which shifted every ordered ``side_effect=[...]`` mock. Routing by the
    selected entity makes these tests immune to query reordering (and to the lock,
    which is no-op'd by the autouse fixture)."""
    from shared.db.models import (
        DataSource, Dimension, GlossaryAttachment, Join, Measure,
        ModelColumn, ModelTable, SourceColumnStatistics,
    )
    _default = MagicMock()
    _default.scalars.return_value.all.return_value = []
    _default.all.return_value = []
    table = {
        DataSource.__name__: sources,
        ModelTable.__name__: sources,
        GlossaryAttachment.__name__: existing,
        Dimension.__name__: dims,
        Measure.__name__: measures,
        Join.__name__: joins,
        ModelColumn.__name__: col_rows if col_rows is not None else _default,
        SourceColumnStatistics.__name__: stats if stats is not None else _default,
    }

    async def _exec(stmt, *a, **kw):
        descs = getattr(stmt, "column_descriptions", None) or []
        ent = descs[0].get("entity") if descs else None
        return table.get(getattr(ent, "__name__", ""), _default)

    return _exec


@pytest.fixture(autouse=True)
def _noop_model_lock():
    """Bug-7982 R6: glossary mutating endpoints now acquire the per-model
    advisory lock (one extra ``db.execute``). These unit tests mock the DB with
    ORDERED ``execute()`` side-effects that assert on the HANDLER's own queries;
    the lock's execute would shift that sequence. The lock's real behaviour is
    covered by ``tests/test_model_lock_coverage.py`` and the live-DB suites, so
    no-op it here to keep these tests focused on the glossary logic under test."""
    with patch("src.api.glossary.acquire_model_definition_lock", AsyncMock()):
        yield


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
    _mint_glossary_service_token,
    _refresh_source_statistics_background,
    _run_glossary_bootstrap_job,
    _sample_values_from_item_stats,
    _serialize_job_result,
    _sweep_bootstrap_jobs,
    _validate_attachment_target,
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


def test_mint_glossary_service_token_uses_typed_service_contract():
    from shared.auth.jwt import decode_access_token

    token = _mint_glossary_service_token("acme")
    claims = decode_access_token(token)

    assert claims["sub"] == "service:glossary-bootstrap-service"
    assert claims["tenant_id"] == "acme"
    assert claims["role"] == "modeler"  # Bug-7758: minimum-privilege role
    assert claims["aud"] == "service"
    assert claims["token_type"] == "service"
    assert claims["service_principal"] == "glossary-bootstrap-service"
    assert claims["service_scopes"] == ["optimizer.stats-refresh"]


@pytest.mark.asyncio
async def test_glossary_bootstrap_job_uses_typed_service_raw_token():
    from shared.auth.jwt import decode_access_token

    captured: dict[str, object] = {}
    job_id = uuid.uuid4()
    project_id = uuid.uuid4()
    model_id = uuid.uuid4()

    async def _capture_update(*args, **kwargs):
        captured.setdefault("updates", []).append((args, kwargs))
        return True  # Bug-6812: _update_bootstrap_job now returns bool

    async def _capture_refresh(*, source_ids, bearer, low_cardinality_threshold):
        captured["refresh_token"] = bearer
        captured["source_ids"] = source_ids
        captured["threshold"] = low_cardinality_threshold

    async def _capture_bootstrap(project_id_arg, model_id_arg, current_user):
        captured["bootstrap_project_id"] = project_id_arg
        captured["bootstrap_model_id"] = model_id_arg
        captured["bootstrap_user"] = current_user
        from shared.schemas.pydantic_models import GlossaryBootstrapResponse

        return GlossaryBootstrapResponse(proposed_count=0)

    with (
        patch("src.api.glossary._update_bootstrap_job", new=_capture_update),
        patch("src.api.glossary._refresh_source_statistics_background", new=_capture_refresh),
        patch("src.api.glossary.bootstrap", new=_capture_bootstrap),
    ):
        await _run_glossary_bootstrap_job(
            job_id=job_id,
            tenant_id="acme",
            user_id="user@example.com",
            email="user@example.com",
            project_id=project_id,
            model_id=model_id,
            source_ids=[uuid.uuid4()],
            max_distinct=25,
        )

    refresh_claims = decode_access_token(str(captured["refresh_token"]))
    job_user = captured["bootstrap_user"]
    bootstrap_claims = decode_access_token(job_user.raw_token)
    assert refresh_claims["service_principal"] == "glossary-bootstrap-service"
    assert refresh_claims["role"] == "modeler"  # Bug-7758: minimum-privilege role
    assert refresh_claims["service_scopes"] == ["optimizer.stats-refresh"]
    assert bootstrap_claims == refresh_claims
    assert job_user.role == "modeler"
    assert getattr(job_user, "_glossary_run_now") is True


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
            patch("src.api.glossary._validate_attachment_target", AsyncMock()),
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
            patch("src.api.glossary._validate_attachment_target", AsyncMock()),
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

    @pytest.mark.asyncio
    async def test_create_entry_normalizes_review_visibility_to_show(self, client):
        """Bug-6261 (Codex R2): manual create produces status='approved', so
        visibility='review' must be normalised to 'show' -- otherwise the
        approved entry is excluded from public pages and gateway descriptions."""
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
            term="Net Revenue",
            definition="Total net revenue",
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
            patch("src.api.glossary._validate_attachment_target", AsyncMock()),
        ):
            url = f"/api/v1/projects/{project_id}/models/{model_id}/glossary"
            resp = await client.post(url, json={
                "term": "Net Revenue",
                "definition": "Total net revenue",
                "target_type": "measure",
                "target_id": str(uuid.uuid4()),
                "visibility": "review",
            })

        assert resp.status_code == 201
        glossary_entries = [
            e for e in captured_entries
            if hasattr(e, "visibility") and hasattr(e, "term")
            and getattr(e, "term", None) == "Net Revenue"
        ]
        assert len(glossary_entries) >= 1
        entry = glossary_entries[0]
        # "review" must be normalised to "show" on an approved entry.
        assert entry.visibility == "show"


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

        Bug-7982 R6 (round 2): the GlossaryAttachment ``existing_entry_map`` query
        moved to AFTER the LLM call, UNDER the lock, so the query ORDER is no longer
        fixed. ``_route_bootstrap_execute`` routes db.execute by the selected entity
        (ModelTable/Dimension/Measure/Join/GlossaryAttachment/...), making these
        tests immune to query reordering — do NOT re-introduce an ordered list.
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
            side_effect=_route_bootstrap_execute(
                sources=r_sources, existing=r_existing, dims=r_dims,
                measures=r_measures, joins=r_joins,
            ),
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
            side_effect=_route_bootstrap_execute(
                sources=r_sources, existing=r_existing, dims=r_dims,
                measures=r_measures, joins=r_joins, col_rows=r_col_rows, stats=r_stats,
            ),
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
            side_effect=_route_bootstrap_execute(
                sources=r_sources, existing=r_existing, dims=r_dims,
                measures=r_measures, joins=r_joins, col_rows=r_col_rows, stats=r_stats,
            ),
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


class TestBug7253_CrossProjectAttachmentRejection:
    """Bug-7253 (CF-018-Fable-F01802): glossary attachment targets must be
    validated against the path model. A dimension/measure/column UUID from
    another model must be rejected at create time to prevent cross-project
    hide-cascade and name disclosure."""

    def _fake_db(self, objects: dict):
        """Return an AsyncMock db whose .get(cls, id) looks up from *objects*
        keyed as (cls, id) tuples."""

        async def _get(cls, oid):
            return objects.get((cls, oid))

        db = AsyncMock()
        db.get = AsyncMock(side_effect=_get)
        return db

    @pytest.mark.asyncio
    async def test_same_model_dimension_accepted(self):
        from shared.db.models import Dimension
        model_id = uuid.uuid4()
        dim_id = uuid.uuid4()
        dim = types.SimpleNamespace(id=dim_id, model_id=model_id)
        db = self._fake_db({(Dimension, dim_id): dim})
        # Should not raise
        await _validate_attachment_target(db, model_id, "dimension", dim_id)

    @pytest.mark.asyncio
    async def test_foreign_model_dimension_rejected(self):
        from fastapi import HTTPException
        from shared.db.models import Dimension
        model_id = uuid.uuid4()
        foreign_model_id = uuid.uuid4()
        dim_id = uuid.uuid4()
        dim = types.SimpleNamespace(id=dim_id, model_id=foreign_model_id)
        db = self._fake_db({(Dimension, dim_id): dim})
        with pytest.raises(HTTPException) as exc_info:
            await _validate_attachment_target(db, model_id, "dimension", dim_id)
        assert exc_info.value.status_code == 422
        assert "dimension" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_nonexistent_dimension_rejected(self):
        from fastapi import HTTPException
        model_id = uuid.uuid4()
        db = self._fake_db({})
        with pytest.raises(HTTPException) as exc_info:
            await _validate_attachment_target(db, model_id, "dimension", uuid.uuid4())
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_foreign_model_measure_rejected(self):
        from fastapi import HTTPException
        from shared.db.models import Measure
        model_id = uuid.uuid4()
        foreign_model_id = uuid.uuid4()
        meas_id = uuid.uuid4()
        meas = types.SimpleNamespace(id=meas_id, model_id=foreign_model_id)
        db = self._fake_db({(Measure, meas_id): meas})
        with pytest.raises(HTTPException) as exc_info:
            await _validate_attachment_target(db, model_id, "measure", meas_id)
        assert exc_info.value.status_code == 422
        assert "measure" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_same_model_measure_accepted(self):
        from shared.db.models import Measure
        model_id = uuid.uuid4()
        meas_id = uuid.uuid4()
        meas = types.SimpleNamespace(id=meas_id, model_id=model_id)
        db = self._fake_db({(Measure, meas_id): meas})
        # Should not raise
        await _validate_attachment_target(db, model_id, "measure", meas_id)

    @pytest.mark.asyncio
    async def test_concept_type_always_accepted(self):
        db = self._fake_db({})
        # concept attachments have no target_id; should not raise
        await _validate_attachment_target(db, uuid.uuid4(), "concept", None)

    @pytest.mark.asyncio
    async def test_none_target_id_accepted(self):
        db = self._fake_db({})
        # None target_id short-circuits validation for any target_type
        await _validate_attachment_target(db, uuid.uuid4(), "dimension", None)

    @pytest.mark.asyncio
    async def test_foreign_model_column_rejected(self):
        """R1-F2: _validate_attachment_target must reject a column whose
        ModelTable belongs to a different model."""
        from fastapi import HTTPException
        from shared.db.models import ModelColumn, ModelTable
        model_id = uuid.uuid4()
        foreign_model_id = uuid.uuid4()
        tbl_id = uuid.uuid4()
        col_id = uuid.uuid4()
        col = types.SimpleNamespace(id=col_id, model_table_id=tbl_id)
        tbl = types.SimpleNamespace(id=tbl_id, model_id=foreign_model_id)
        db = self._fake_db({(ModelColumn, col_id): col, (ModelTable, tbl_id): tbl})
        with pytest.raises(HTTPException) as exc_info:
            await _validate_attachment_target(db, model_id, "column", col_id)
        assert exc_info.value.status_code == 422
        assert "column" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_same_model_column_accepted(self):
        """R1-F2: a column whose ModelTable belongs to the path model
        must be accepted."""
        from shared.db.models import ModelColumn, ModelTable
        model_id = uuid.uuid4()
        tbl_id = uuid.uuid4()
        col_id = uuid.uuid4()
        col = types.SimpleNamespace(id=col_id, model_table_id=tbl_id)
        tbl = types.SimpleNamespace(id=tbl_id, model_id=model_id)
        db = self._fake_db({(ModelColumn, col_id): col, (ModelTable, tbl_id): tbl})
        # Should not raise
        await _validate_attachment_target(db, model_id, "column", col_id)

    @pytest.mark.asyncio
    async def test_cascade_skips_foreign_column(self):
        """R1-F1: _cascade_hidden_to_columns must skip column-type targets
        from other models when model_id is supplied."""
        model_a = uuid.uuid4()
        model_b = uuid.uuid4()
        col_a_id = uuid.uuid4()
        col_b_id = uuid.uuid4()
        tbl_a_id = uuid.uuid4()
        tbl_b_id = uuid.uuid4()
        from shared.db.models import ModelColumn, ModelTable
        col_a = types.SimpleNamespace(id=col_a_id, model_table_id=tbl_a_id, is_hidden=False)
        col_b = types.SimpleNamespace(id=col_b_id, model_table_id=tbl_b_id, is_hidden=False)
        tbl_a = types.SimpleNamespace(id=tbl_a_id, model_id=model_a)
        tbl_b = types.SimpleNamespace(id=tbl_b_id, model_id=model_b)

        async def _get(cls, oid):
            if cls is ModelColumn:
                if oid == col_a_id:
                    return col_a
                if oid == col_b_id:
                    return col_b
            if cls is ModelTable:
                if oid == tbl_a_id:
                    return tbl_a
                if oid == tbl_b_id:
                    return tbl_b
            return None

        db = AsyncMock()
        db.get = AsyncMock(side_effect=_get)
        att_a = types.SimpleNamespace(target_type="column", target_id=col_a_id)
        att_b = types.SimpleNamespace(target_type="column", target_id=col_b_id)

        changed = await _cascade_hidden_to_columns(
            db, [att_a, att_b], True, model_id=model_a,
        )
        # Only col_a should be hidden; col_b is from model_b
        assert changed == 1
        assert col_a.is_hidden is True
        assert col_b.is_hidden is False

    @pytest.mark.asyncio
    async def test_cascade_skips_foreign_dimension(self):
        """_cascade_hidden_to_columns must skip targets from other models
        when model_id is supplied (Bug-7253 defence in depth)."""
        model_a = uuid.uuid4()
        model_b = uuid.uuid4()
        col_a_id = uuid.uuid4()
        col_b_id = uuid.uuid4()
        from shared.db.models import Dimension, ModelColumn
        dim_a = types.SimpleNamespace(
            id=uuid.uuid4(), model_id=model_a, source_column_id=col_a_id,
        )
        dim_b = types.SimpleNamespace(
            id=uuid.uuid4(), model_id=model_b, source_column_id=col_b_id,
        )
        col_a = types.SimpleNamespace(id=col_a_id, is_hidden=False)
        col_b = types.SimpleNamespace(id=col_b_id, is_hidden=False)

        async def _get(cls, oid):
            if cls is Dimension:
                if oid == dim_a.id:
                    return dim_a
                if oid == dim_b.id:
                    return dim_b
            if cls is ModelColumn:
                if oid == col_a_id:
                    return col_a
                if oid == col_b_id:
                    return col_b
            return None

        db = AsyncMock()
        db.get = AsyncMock(side_effect=_get)
        att_a = types.SimpleNamespace(target_type="dimension", target_id=dim_a.id)
        att_b = types.SimpleNamespace(target_type="dimension", target_id=dim_b.id)

        changed = await _cascade_hidden_to_columns(
            db, [att_a, att_b], True, model_id=model_a,
        )
        # Only dim_a's column should be hidden; dim_b is from model_b
        assert changed == 1
        assert col_a.is_hidden is True
        assert col_b.is_hidden is False


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

    @pytest.mark.asyncio
    async def test_poll_fails_wedged_stale_job(self, client, _ids):
        """Bug-6265: a non-terminal job whose updated_at is older than the stale
        window (the worker died on a restart / CPU throttle) is failed at read
        time so the panel stops polling forever."""
        stale_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        job_row = types.SimpleNamespace(
            id=_ids["job_id"],
            project_id=_ids["project_id"],
            model_id=_ids["model_id"],
            status="generating_glossary",  # non-terminal
            message="Generating glossary...",
            result=None,
            created_at=stale_time,
            updated_at=stale_time,
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
        assert resp.json()["job_status"] == "failed"
        # The watchdog wrote the terminal state back.
        assert job_row.status == "failed"
        mock_db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_poll_keeps_recent_running_job(self, client, _ids):
        """Bug-6265: a non-terminal job that IS still advancing (recent
        updated_at) must NOT be failed by the watchdog."""
        fresh = datetime.now(timezone.utc)
        job_row = types.SimpleNamespace(
            id=_ids["job_id"],
            project_id=_ids["project_id"],
            model_id=_ids["model_id"],
            status="generating_glossary",
            message="Generating glossary...",
            result=None,
            created_at=fresh,
            updated_at=fresh,
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
        assert resp.json()["job_status"] == "generating_glossary"
        assert job_row.status == "generating_glossary"
        mock_db.commit.assert_not_awaited()


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
    # F-018-16: a trigger hidden behind leading whitespace is still guarded.
    for hidden in (" =HYPERLINK(1)", "  +1", "\n=x", " \t@SUM"):
        out = _formula_guard(hidden)
        assert out.startswith("'"), repr(hidden)
        # The original text is preserved verbatim after the quote.
        assert out == "'" + hidden, repr(hidden)
    # Ordinary text (including a leading space with no formula) is untouched.
    assert _formula_guard("Total revenue") == "Total revenue"
    assert _formula_guard("  just spaced text") == "  just spaced text"
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
        table_id = uuid.uuid4()
        col = types.SimpleNamespace(
            id=column_id, is_hidden=False, model_table_id=table_id,
        )
        tbl = types.SimpleNamespace(id=table_id, model_id=_ids["model_id"])
        entry_llm = types.SimpleNamespace(
            id=uuid.uuid4(),
            model_id=_ids["model_id"],
            status="pending_review",
            source="llm",
            created_by=None,
            proposed_is_hidden=True,
            visibility="show",
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
            visibility="review",
            attachments=[],
        )

        from shared.db.models import ModelColumn, ModelTable

        async def _get(cls, oid):
            if cls is ModelColumn and oid == column_id:
                return col
            if cls is ModelTable and oid == table_id:
                return tbl
            return col  # fallback for the final col flip

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.get = AsyncMock(side_effect=_get)
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
        # Bug-6261: approval must promote visibility to "show" so the
        # heuristic entry reaches the public page, downloads, and gateway
        # descriptions.  An LLM entry already at "show" stays at "show".
        assert entry_llm.visibility == "show"
        assert entry_heuristic.visibility == "show"
        assert col.is_hidden is True
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_approve_heuristic_entry_promotes_visibility_review_to_show(
        self, client, _ids
    ):
        """Bug-6261: heuristic entries start with visibility='review'. Approval
        must promote to 'show' so the entry reaches the public page, downloads,
        and gateway descriptions (effective_description)."""
        entry = types.SimpleNamespace(
            id=uuid.uuid4(),
            model_id=_ids["model_id"],
            status="pending_review",
            source="heuristic",
            created_by=None,
            proposed_is_hidden=None,
            visibility="review",
            attachments=[],
        )

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [entry]
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
        assert entry.status == "approved"
        # The key assertion: visibility promoted from "review" to "show".
        assert entry.visibility == "show"

    @pytest.mark.asyncio
    async def test_approve_preserves_explicit_hide_visibility(
        self, client, _ids
    ):
        """Bug-6261 edge case: an entry whose modeller explicitly set
        visibility='hide' must NOT be promoted to 'show' on approval --
        'hide' is a deliberate decision to suppress the column."""
        entry = types.SimpleNamespace(
            id=uuid.uuid4(),
            model_id=_ids["model_id"],
            status="pending_review",
            source="heuristic",
            created_by=None,
            proposed_is_hidden=None,
            visibility="hide",
            attachments=[],
        )

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [entry]
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
        assert entry.status == "approved"
        # Explicit hide must be preserved.
        assert entry.visibility == "hide"


class TestGlossaryEditVisibility:
    """Bug-6261 (Codex R1): editing a glossary entry auto-approves it,
    so the resulting visibility must be normalised the same way as approve."""

    @pytest.fixture
    def _ids(self):
        return {"project_id": uuid.uuid4(), "model_id": uuid.uuid4()}

    @pytest.mark.asyncio
    async def test_edit_heuristic_entry_promotes_review_visibility(
        self, client, _ids
    ):
        """Editing a heuristic entry with visibility='review' produces a
        new approved version with visibility='show', so it reaches the
        public page, downloads, and gateway descriptions."""
        entry_id = uuid.uuid4()
        existing = types.SimpleNamespace(
            id=entry_id,
            model_id=_ids["model_id"],
            term="Net Revenue",
            definition="old def",
            context_notes=None,
            source="heuristic",
            status="pending_review",
            version=1,
            superseded_by=None,
            proposed_is_hidden=None,
            visibility="review",
            confidence="low",
            sample_values=None,
            created_by=None,
            created_at=None,
            updated_at=None,
            synonyms=[],
            attachments=[],
        )

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.flush = AsyncMock()

        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=exec_result)

        captured_entries = []

        def _capture_add(obj):
            captured_entries.append(obj)

        mock_db.add = MagicMock(side_effect=_capture_add)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("src.api.glossary._reload", AsyncMock(return_value=types.SimpleNamespace(
                id=uuid.uuid4(),
                model_id=_ids["model_id"],
                term="Net Revenue",
                definition="updated def",
                context_notes=None,
                source="user",
                status="approved",
                version=2,
                superseded_by=None,
                created_by=None,
                proposed_is_hidden=None,
                visibility="show",
                confidence="low",
                sample_values=None,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                synonyms=[],
                attachments=[],
            ))),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}/glossary/{entry_id}"
            )
            resp = await client.patch(url, json={
                "definition": "updated def",
            })

        assert resp.status_code == 200
        # The first add() call should be the new GlossaryEntry.
        # Verify its visibility was normalised to "show".
        new_entry = captured_entries[0]
        assert new_entry.visibility == "show"
        assert new_entry.status == "approved"

    @pytest.mark.asyncio
    async def test_edit_entry_preserves_explicit_hide(self, client, _ids):
        """Editing an entry with visibility='hide' keeps it hidden even
        though the edit auto-approves."""
        entry_id = uuid.uuid4()
        existing = types.SimpleNamespace(
            id=entry_id,
            model_id=_ids["model_id"],
            term="Internal Code",
            definition="old def",
            context_notes=None,
            source="user",
            status="approved",
            version=1,
            superseded_by=None,
            proposed_is_hidden=None,
            visibility="hide",
            confidence="high",
            sample_values=None,
            created_by=None,
            created_at=None,
            updated_at=None,
            synonyms=[],
            attachments=[],
        )

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.flush = AsyncMock()

        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = existing
        mock_db.execute = AsyncMock(return_value=exec_result)

        captured_entries = []

        def _capture_add(obj):
            captured_entries.append(obj)

        mock_db.add = MagicMock(side_effect=_capture_add)

        async def _gen(*a, **kw):
            yield mock_db

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("src.api.glossary._reload", AsyncMock(return_value=types.SimpleNamespace(
                id=uuid.uuid4(),
                model_id=_ids["model_id"],
                term="Internal Code",
                definition="updated def",
                context_notes=None,
                source="user",
                status="approved",
                version=2,
                superseded_by=None,
                created_by=None,
                proposed_is_hidden=None,
                visibility="hide",
                confidence="high",
                sample_values=None,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                synonyms=[],
                attachments=[],
            ))),
        ):
            url = (
                f"/api/v1/projects/{_ids['project_id']}"
                f"/models/{_ids['model_id']}/glossary/{entry_id}"
            )
            resp = await client.patch(url, json={
                "definition": "updated def",
            })

        assert resp.status_code == 200
        new_entry = captured_entries[0]
        assert new_entry.visibility == "hide"


# ---------------------------------------------------------------------------
# Bug-7957: Public glossary payload response-contract test
# ---------------------------------------------------------------------------

# The allowed key sets are locked down here. If a new field is added to the
# public payload, this test must be updated — the failing assertion is the
# safety gate that prevents internal identifiers from silently creeping back
# into the unauthenticated public surface.
_ALLOWED_MODEL_KEYS = {"display_name", "slug", "description"}
_ALLOWED_ENTRY_KEYS = {"term", "definition", "context_notes", "synonyms", "version", "updated_at"}


@pytest.mark.asyncio
async def test_public_glossary_payload_contains_only_allowed_keys():
    """Bug-7957: the public glossary JSON must contain ONLY the allowed
    fields. No internal object IDs (model.id, attachment target_id, entry
    id) may appear on the unauthenticated share-link surface."""
    from src.api.glossary import _build_public_payload

    model_id = uuid.uuid4()

    fake_model = types.SimpleNamespace(
        id=model_id,
        slug="test-model",
        display_name="Test Model",
        description="Test description",
    )

    syn = types.SimpleNamespace(synonym="alias1")
    att = types.SimpleNamespace(
        target_type="dimension",
        target_id=uuid.uuid4(),
    )
    fake_entry = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=model_id,
        term="Revenue",
        definition="Total revenue",
        context_notes="Finance context",
        source="user",
        status="approved",
        version=1,
        superseded_by=None,
        visibility="show",
        proposed_is_hidden=None,
        confidence="high",
        sample_values=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        synonyms=[syn],
        attachments=[att],
    )

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_model)

    scalars_result = MagicMock()
    scalars_result.all.return_value = [fake_entry]
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars_result
    mock_db.execute = AsyncMock(return_value=exec_result)

    async def _gen(*a, **kw):
        yield mock_db

    with patch("src.api.glossary.get_tenant_db", _gen):
        payload = await _build_public_payload("test-tenant", model_id)

    # Assert model keys
    assert set(payload["model"].keys()) == _ALLOWED_MODEL_KEYS, (
        f"Public model payload has extra keys: {set(payload['model'].keys()) - _ALLOWED_MODEL_KEYS}"
    )
    # Assert no model.id leaked
    assert "id" not in payload["model"]

    # Assert entry keys
    assert len(payload["entries"]) == 1
    entry_keys = set(payload["entries"][0].keys())
    assert entry_keys == _ALLOWED_ENTRY_KEYS, (
        f"Public entry payload has extra keys: {entry_keys - _ALLOWED_ENTRY_KEYS}"
    )
    # Assert no attachments or internal IDs leaked
    assert "attachments" not in payload["entries"][0]
    assert "id" not in payload["entries"][0]
    assert "target_id" not in payload["entries"][0]

    # Positive check: verify expected data is present
    assert payload["model"]["display_name"] == "Test Model"
    assert payload["entries"][0]["term"] == "Revenue"
    assert payload["entries"][0]["synonyms"] == ["alias1"]


@pytest.mark.asyncio
async def test_public_glossary_payload_csv_download_has_no_internal_ids():
    """Bug-7957: CSV download columns must not include attachments or IDs."""
    from src.api.glossary import _build_public_payload

    model_id = uuid.uuid4()

    fake_model = types.SimpleNamespace(
        id=model_id,
        slug="test-model",
        display_name="Test Model",
        description=None,
    )

    fake_entry = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=model_id,
        term="Revenue",
        definition="Total revenue",
        context_notes=None,
        source="user",
        status="approved",
        version=1,
        superseded_by=None,
        visibility="show",
        proposed_is_hidden=None,
        confidence="high",
        sample_values=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        synonyms=[],
        attachments=[],
    )

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=fake_model)

    scalars_result = MagicMock()
    scalars_result.all.return_value = [fake_entry]
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars_result
    mock_db.execute = AsyncMock(return_value=exec_result)

    async def _gen(*a, **kw):
        yield mock_db

    with patch("src.api.glossary.get_tenant_db", _gen):
        payload = await _build_public_payload("test-tenant", model_id)

    # The CSV writer uses exactly these keys; confirm no attachment field
    # is available for accidental inclusion
    for entry in payload["entries"]:
        assert "attachments" not in entry
        assert "id" not in entry


# ---------------------------------------------------------------------------
# Bug-7962: Manual create cascades proposed_is_hidden to ModelColumn
# ---------------------------------------------------------------------------

class TestManualCreateHiddenCascade:
    """Bug-7962: manual glossary create with proposed_is_hidden=True must
    invoke _cascade_hidden_to_columns before commit, so the target column's
    is_hidden flag is actually flipped. Previously, manual create set
    status='approved' without invoking the cascade, leaving the column
    visible despite the modeller selecting 'Hide column'.
    """

    @pytest.fixture
    def _ids(self):
        return {
            "project_id": uuid.uuid4(),
            "model_id": uuid.uuid4(),
            "entry_id": uuid.uuid4(),
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target_type", ["dimension", "measure", "column"])
    async def test_create_with_hide_invokes_cascade(self, client, _ids, target_type):
        """Creating a glossary entry with proposed_is_hidden=True must call
        _cascade_hidden_to_columns for dimension, measure, and column
        attachment types."""
        target_id = uuid.uuid4()
        entry_id = _ids["entry_id"]
        model_id = _ids["model_id"]

        captured_entries = []

        mock_db = MagicMock()
        mock_db.add = MagicMock(side_effect=lambda obj: captured_entries.append(obj))
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        reload_entry = types.SimpleNamespace(
            id=entry_id,
            model_id=model_id,
            term="Hidden Dim",
            definition="Should be hidden",
            context_notes=None,
            source="user",
            status="approved",
            version=1,
            superseded_by=None,
            created_by=None,
            proposed_is_hidden=True,
            visibility="show",
            confidence="high",
            sample_values=None,
            created_at="2026-07-21T00:00:00Z",
            updated_at="2026-07-21T00:00:00Z",
            synonyms=[],
            attachments=[],
        )
        reload_result = MagicMock()
        reload_result.scalar_one.return_value = reload_entry
        mock_db.execute = AsyncMock(return_value=reload_result)

        async def _gen(*a, **kw):
            yield mock_db

        cascade_calls = []

        async def _mock_cascade(db, attachments, hidden, model_id=None):
            cascade_calls.append({
                "attachments": attachments,
                "hidden": hidden,
                "model_id": model_id,
            })
            return 1

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("src.api.glossary._validate_attachment_target", AsyncMock()),
            patch("src.api.glossary._cascade_hidden_to_columns", _mock_cascade),
        ):
            url = f"/api/v1/projects/{_ids['project_id']}/models/{model_id}/glossary"
            resp = await client.post(url, json={
                "term": "Hidden Dim",
                "definition": "Should be hidden",
                "target_type": target_type,
                "target_id": str(target_id),
                "proposed_is_hidden": True,
            })

        assert resp.status_code == 201
        assert len(cascade_calls) == 1, "cascade must be called exactly once"
        assert cascade_calls[0]["hidden"] is True
        assert cascade_calls[0]["model_id"] == model_id
        # The attachment passed to cascade must have the correct target_type
        att = cascade_calls[0]["attachments"][0]
        assert att.target_type == target_type
        assert att.target_id == target_id

    @pytest.mark.asyncio
    async def test_create_without_hide_skips_cascade(self, client, _ids):
        """When proposed_is_hidden is None (not set), cascade must NOT be invoked."""
        target_id = uuid.uuid4()
        model_id = _ids["model_id"]

        mock_db = MagicMock()
        mock_db.add = MagicMock()
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        reload_entry = types.SimpleNamespace(
            id=_ids["entry_id"],
            model_id=model_id,
            term="Visible Dim",
            definition="Should remain visible",
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
            created_at="2026-07-21T00:00:00Z",
            updated_at="2026-07-21T00:00:00Z",
            synonyms=[],
            attachments=[],
        )
        reload_result = MagicMock()
        reload_result.scalar_one.return_value = reload_entry
        mock_db.execute = AsyncMock(return_value=reload_result)

        async def _gen(*a, **kw):
            yield mock_db

        cascade_calls = []

        async def _mock_cascade(db, attachments, hidden, model_id=None):
            cascade_calls.append(True)
            return 0

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("src.api.glossary._validate_attachment_target", AsyncMock()),
            patch("src.api.glossary._cascade_hidden_to_columns", _mock_cascade),
        ):
            url = f"/api/v1/projects/{_ids['project_id']}/models/{model_id}/glossary"
            resp = await client.post(url, json={
                "term": "Visible Dim",
                "definition": "Should remain visible",
                "target_type": "dimension",
                "target_id": str(target_id),
            })

        assert resp.status_code == 201
        assert len(cascade_calls) == 0, "cascade must NOT be called when proposed_is_hidden is None"

    @pytest.mark.asyncio
    async def test_create_with_unhide_invokes_cascade_false(self, client, _ids):
        """proposed_is_hidden=False (explicit un-hide) must invoke the cascade
        with hidden=False so the column is made visible."""
        target_id = uuid.uuid4()
        model_id = _ids["model_id"]

        mock_db = MagicMock()
        mock_db.add = MagicMock()
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        reload_entry = types.SimpleNamespace(
            id=_ids["entry_id"],
            model_id=model_id,
            term="Unhidden Dim",
            definition="Should be visible",
            context_notes=None,
            source="user",
            status="approved",
            version=1,
            superseded_by=None,
            created_by=None,
            proposed_is_hidden=False,
            visibility="show",
            confidence="high",
            sample_values=None,
            created_at="2026-07-21T00:00:00Z",
            updated_at="2026-07-21T00:00:00Z",
            synonyms=[],
            attachments=[],
        )
        reload_result = MagicMock()
        reload_result.scalar_one.return_value = reload_entry
        mock_db.execute = AsyncMock(return_value=reload_result)

        async def _gen(*a, **kw):
            yield mock_db

        cascade_calls = []

        async def _mock_cascade(db, attachments, hidden, model_id=None):
            cascade_calls.append({"hidden": hidden})
            return 1

        with (
            patch("src.api.glossary.get_tenant_db", _gen),
            patch("src.api.glossary.ensure_model_in_project", AsyncMock()),
            patch("src.api.glossary._validate_attachment_target", AsyncMock()),
            patch("src.api.glossary._cascade_hidden_to_columns", _mock_cascade),
        ):
            url = f"/api/v1/projects/{_ids['project_id']}/models/{model_id}/glossary"
            resp = await client.post(url, json={
                "term": "Unhidden Dim",
                "definition": "Should be visible",
                "target_type": "dimension",
                "target_id": str(target_id),
                "proposed_is_hidden": False,
            })

        assert resp.status_code == 201
        assert len(cascade_calls) == 1, "cascade must be called for explicit False"
        assert cascade_calls[0]["hidden"] is False
