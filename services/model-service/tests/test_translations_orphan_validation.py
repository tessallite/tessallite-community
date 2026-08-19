"""Bug-5981 (F-029-02): translation write paths must reject an entity_id
that does not resolve to a live entity on the model, instead of silently
persisting an orphan EntityTranslation row that inflates coverage but
never renders anywhere (useModelTranslations.ts matches by exact id).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/translations"


def _model(model_id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID):
    return types.SimpleNamespace(id=model_id, project_id=project_id)


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


# ---------------------------------------------------------------------------
# _validate_translation_target — unit tests
# ---------------------------------------------------------------------------


class TestValidateTranslationTarget:
    @pytest.mark.asyncio
    async def test_model_entity_type_requires_entity_id_equals_model_id(self):
        from src.api.translations import _validate_translation_target

        db = make_mock_db()
        assert await _validate_translation_target(db, TEST_MODEL_ID, "model", TEST_MODEL_ID) is True
        assert await _validate_translation_target(db, TEST_MODEL_ID, "model", uuid.uuid4()) is False

    @pytest.mark.asyncio
    async def test_dimension_must_exist_and_belong_to_model(self):
        from src.api.translations import _validate_translation_target

        dim_id = uuid.uuid4()
        db = make_mock_db()
        db.get = AsyncMock(return_value=types.SimpleNamespace(id=dim_id, model_id=TEST_MODEL_ID))
        assert await _validate_translation_target(db, TEST_MODEL_ID, "dimension", dim_id) is True

    @pytest.mark.asyncio
    async def test_dimension_from_a_different_model_is_rejected(self):
        from src.api.translations import _validate_translation_target

        dim_id = uuid.uuid4()
        other_model = uuid.uuid4()
        db = make_mock_db()
        db.get = AsyncMock(return_value=types.SimpleNamespace(id=dim_id, model_id=other_model))
        assert await _validate_translation_target(db, TEST_MODEL_ID, "dimension", dim_id) is False

    @pytest.mark.asyncio
    async def test_nonexistent_measure_is_rejected(self):
        from src.api.translations import _validate_translation_target

        db = make_mock_db()
        db.get = AsyncMock(return_value=None)
        assert await _validate_translation_target(db, TEST_MODEL_ID, "measure", uuid.uuid4()) is False

    @pytest.mark.asyncio
    async def test_glossary_entry_resolved(self):
        from src.api.translations import _validate_translation_target

        gid = uuid.uuid4()
        db = make_mock_db()
        db.get = AsyncMock(return_value=types.SimpleNamespace(
            id=gid, model_id=TEST_MODEL_ID, superseded_by=None,
        ))
        assert await _validate_translation_target(db, TEST_MODEL_ID, "glossary_entry", gid) is True

    @pytest.mark.asyncio
    async def test_superseded_glossary_entry_is_rejected(self):
        # Bug-5981 review round 1 (M3): translation_coverage excludes
        # superseded glossary entries from both numerator and denominator
        # -- a translation accepted for one would be counted nowhere and
        # rendered nowhere, the exact write/read asymmetry this bug fixes.
        from src.api.translations import _validate_translation_target

        gid = uuid.uuid4()
        db = make_mock_db()
        db.get = AsyncMock(return_value=types.SimpleNamespace(
            id=gid, model_id=TEST_MODEL_ID, superseded_by=uuid.uuid4(),
        ))
        assert await _validate_translation_target(db, TEST_MODEL_ID, "glossary_entry", gid) is False

    @pytest.mark.asyncio
    async def test_unknown_entity_type_is_rejected(self):
        from src.api.translations import _validate_translation_target

        db = make_mock_db()
        assert await _validate_translation_target(db, TEST_MODEL_ID, "kpi", uuid.uuid4()) is False


class TestValidateTranslationField:
    def test_known_translatable_fields_are_accepted(self):
        from src.api.translations import _validate_translation_field

        assert _validate_translation_field("dimension", "display_name") is True
        assert _validate_translation_field("measure", "description") is True
        assert _validate_translation_field("glossary_entry", "term") is True
        assert _validate_translation_field("model", "display_name") is True

    def test_unknown_field_names_are_rejected(self):
        from src.api.translations import _validate_translation_field

        assert _validate_translation_field("dimension", "sql_expression") is False
        assert _validate_translation_field("measure", "owner_email") is False
        assert _validate_translation_field("unknown", "display_name") is False


class TestCoverageNumerator:
    def test_model_translations_do_not_count_in_coverage_numerator(self):
        from sqlalchemy.dialects import postgresql

        from src.api.translations import _coverage_live_entity_filter

        expr = _coverage_live_entity_filter([uuid.uuid4()], [uuid.uuid4()], [uuid.uuid4()])
        sql = str(
            expr.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        assert "'dimension'" in sql
        assert "'measure'" in sql
        assert "'glossary_entry'" in sql
        assert "'model'" not in sql


# ---------------------------------------------------------------------------
# create_translation — endpoint tests
# ---------------------------------------------------------------------------


class TestCreateTranslationRejectsOrphan:
    @pytest.mark.asyncio
    async def test_orphan_dimension_id_returns_400(self, client):
        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda model_cls, _id: (
            _model() if getattr(model_cls, "__name__", "") == "Model" else None
        ))
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                PREFIX,
                json={
                    "entity_type": "dimension",
                    "entity_id": str(uuid.uuid4()),
                    "field_name": "display_name",
                    "locale": "fr",
                    "translated_text": "Région",
                },
            )
        assert resp.status_code == 400, resp.text
        assert "no dimension" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_unsupported_field_name_returns_400(self, client):
        dim_id = uuid.uuid4()

        def _get(model_cls, obj_id):
            name = getattr(model_cls, "__name__", "")
            if name == "Model":
                return _model()
            if name == "Dimension":
                return types.SimpleNamespace(id=dim_id, model_id=TEST_MODEL_ID)
            return None

        db = make_mock_db()
        db.get = AsyncMock(side_effect=_get)
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                PREFIX,
                json={
                    "entity_type": "dimension",
                    "entity_id": str(dim_id),
                    "field_name": "sql_expression",
                    "locale": "fr",
                    "translated_text": "bad",
                },
            )
        assert resp.status_code == 400, resp.text
        assert "unsupported field" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_live_dimension_id_is_accepted(self, client):
        dim_id = uuid.uuid4()

        def _get(model_cls, obj_id):
            name = getattr(model_cls, "__name__", "")
            if name == "Model":
                return _model()
            if name == "Dimension":
                return types.SimpleNamespace(id=dim_id, model_id=TEST_MODEL_ID)
            return None

        db = make_mock_db()
        db.get = AsyncMock(side_effect=_get)
        db.execute = AsyncMock(return_value=_ScalarResult([]))  # no existing row

        async def _mock_refresh(obj):
            obj.id = uuid.uuid4()

        db.refresh = _mock_refresh
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                PREFIX,
                json={
                    "entity_type": "dimension",
                    "entity_id": str(dim_id),
                    "field_name": "display_name",
                    "locale": "fr",
                    "translated_text": "Région",
                },
            )
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# import_translations — endpoint tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bug-7664: JSON import malformed-shape validation
# The JSON branch must validate that the parsed result is a list of dicts
# with the required keys, and report errors via TranslationImportResponse
# instead of raising unhandled 500s.
# ---------------------------------------------------------------------------


class TestImportTranslationsJsonValidation:
    @pytest.mark.asyncio
    async def test_json_top_level_object_returns_400(self, client):
        """A JSON object (not array) should return 400, not 500."""
        import io

        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda model_cls, _id: (
            _model() if getattr(model_cls, "__name__", "") == "Model" else None
        ))
        json_content = '{"translations": [{"locale": "fr"}]}'
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{PREFIX}/import",
                files={"file": ("translations.json", io.BytesIO(json_content.encode()), "application/json")},
            )
        assert resp.status_code == 400, resp.text
        assert "array" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_json_non_dict_item_reported_as_error(self, client):
        """A JSON array containing a non-dict item should be reported in errors."""
        import io

        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda model_cls, _id: (
            _model() if getattr(model_cls, "__name__", "") == "Model" else None
        ))
        json_content = '["just a string", 42]'
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{PREFIX}/import",
                files={"file": ("translations.json", io.BytesIO(json_content.encode()), "application/json")},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["imported"] == 0
        assert len(data["errors"]) == 2
        assert "not an object" in data["errors"][0].lower()

    @pytest.mark.asyncio
    async def test_json_item_missing_keys_reported_as_error(self, client):
        """A JSON item missing required keys should be reported in errors."""
        import io

        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda model_cls, _id: (
            _model() if getattr(model_cls, "__name__", "") == "Model" else None
        ))
        json_content = '[{"locale": "fr", "entity_type": "dimension"}]'
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{PREFIX}/import",
                files={"file": ("translations.json", io.BytesIO(json_content.encode()), "application/json")},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["imported"] == 0
        assert any("missing keys" in e.lower() for e in data["errors"])


class TestImportTranslationsRejectsOrphan:
    @pytest.mark.asyncio
    async def test_orphan_entity_id_reported_per_row_and_skipped(self, client):
        import io

        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda model_cls, _id: (
            _model() if getattr(model_cls, "__name__", "") == "Model" else None
        ))
        csv_content = (
            "entity_type,entity_id,field_name,locale,translated_text\n"
            f"dimension,{uuid.uuid4()},display_name,fr,Région\n"
        )
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{PREFIX}/import",
                files={"file": ("translations.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["imported"] == 0
        assert data["skipped"] == 1
        assert any("no dimension" in e.lower() for e in data["errors"])

    @pytest.mark.asyncio
    async def test_unsupported_field_name_reported_per_row_and_skipped(self, client):
        import io

        dim_id = uuid.uuid4()

        def _get(model_cls, obj_id):
            name = getattr(model_cls, "__name__", "")
            if name == "Model":
                return _model()
            if name == "Dimension":
                return types.SimpleNamespace(id=dim_id, model_id=TEST_MODEL_ID)
            return None

        db = make_mock_db()
        db.get = AsyncMock(side_effect=_get)
        csv_content = (
            "entity_type,entity_id,field_name,locale,translated_text\n"
            f"dimension,{dim_id},sql_expression,fr,bad\n"
        )
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                f"{PREFIX}/import",
                files={"file": ("translations.csv", io.BytesIO(csv_content.encode()), "text/csv")},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["imported"] == 0
        assert data["skipped"] == 1
        assert any("unsupported field" in e.lower() for e in data["errors"])


# ---------------------------------------------------------------------------
# translation_coverage — excludes orphan rows from the numerator
# ---------------------------------------------------------------------------


class TestCoverageExcludesOrphans:
    @pytest.mark.asyncio
    async def test_coverage_query_filters_by_live_entity_ids(self, client):
        # This exercises the query construction path (no DB errors raised)
        # rather than asserting exact counts against a mocked engine — the
        # meaningful behavior (entity_id IN <live ids> per entity_type) is
        # expressed directly in the SQLAlchemy WHERE clause in
        # translations.py and is covered by the _validate_translation_target
        # unit tests above for the write-side guarantee that keeps future
        # rows in sync with what coverage now counts.
        db = make_mock_db()
        db.get = AsyncMock(side_effect=lambda model_cls, _id: (
            _model() if getattr(model_cls, "__name__", "") == "Model" else None
        ))
        empty = _ScalarResult([])
        db.execute = AsyncMock(return_value=empty)
        with patch("src.api.translations.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{PREFIX}/coverage")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total_translatable"] == 0
        assert data["locales"] == []
