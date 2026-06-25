"""Tests for project-level import/export logic (bundle validation, constants)."""
from __future__ import annotations

import copy

import pytest

from shared.model_snapshot.project_rehydrator import (
    PROJECT_EXPORT_FORMAT,
    ProjectImportError,
    _validate_bundle,
)
from shared.model_snapshot.project_serialiser import (
    PROJECT_BUNDLE_VERSION,
    PROJECT_EXPORT_FORMAT as SERIALISER_FORMAT,
)


def _minimal_bundle(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "export_format": PROJECT_EXPORT_FORMAT,
        "exported_at": "2026-04-27T00:00:00Z",
        "exported_from": {"tenant_slug": "t1", "project_id": "p1"},
        "credentials_included": False,
        "credentials_envelope": None,
        "included_sections": [],
        "project": {"slug": "demo", "display_name": "Demo", "is_active": True},
        "models": [],
    }
    base.update(overrides)
    return base


class TestFormatConstants:
    def test_export_format_matches_between_serialiser_and_rehydrator(self):
        assert SERIALISER_FORMAT == PROJECT_EXPORT_FORMAT

    def test_bundle_version_is_one(self):
        assert PROJECT_BUNDLE_VERSION == 1


class TestValidateBundle:
    def test_valid_minimal_bundle_passes(self):
        _validate_bundle(_minimal_bundle())

    def test_wrong_export_format_raises(self):
        bundle = _minimal_bundle(export_format="wrong/v1")
        with pytest.raises(ProjectImportError, match="Unsupported export_format"):
            _validate_bundle(bundle)

    def test_missing_export_format_raises(self):
        bundle = _minimal_bundle()
        del bundle["export_format"]
        with pytest.raises(ProjectImportError, match="Unsupported export_format"):
            _validate_bundle(bundle)

    def test_wrong_schema_version_raises(self):
        bundle = _minimal_bundle(schema_version=99)
        with pytest.raises(ProjectImportError, match="Unsupported schema_version"):
            _validate_bundle(bundle)

    def test_missing_schema_version_raises(self):
        bundle = _minimal_bundle()
        del bundle["schema_version"]
        with pytest.raises(ProjectImportError, match="Unsupported schema_version"):
            _validate_bundle(bundle)

    def test_included_section_with_null_value_raises(self):
        bundle = _minimal_bundle(
            included_sections=["connections"],
            connections=None,
        )
        with pytest.raises(ProjectImportError, match="connections.*null/missing"):
            _validate_bundle(bundle)

    def test_included_section_with_missing_key_raises(self):
        bundle = _minimal_bundle(included_sections=["llm_configs"])
        with pytest.raises(ProjectImportError, match="llm_configs.*null/missing"):
            _validate_bundle(bundle)

    def test_included_section_with_valid_data_passes(self):
        bundle = _minimal_bundle(
            included_sections=["connections", "project_settings"],
            connections=[{"id": "c1", "display_name": "pg", "connection_type": "postgres", "config": {}}],
            project_settings=[{"key": "k", "value": "v"}],
        )
        _validate_bundle(bundle)

    def test_empty_list_section_passes(self):
        bundle = _minimal_bundle(
            included_sections=["connections"],
            connections=[],
        )
        _validate_bundle(bundle)

    def test_multiple_sections_validated(self):
        bundle = _minimal_bundle(
            included_sections=["connections", "llm_configs"],
            connections=[],
        )
        with pytest.raises(ProjectImportError, match="llm_configs.*null/missing"):
            _validate_bundle(bundle)


class TestProjectImportError:
    def test_is_value_error(self):
        assert issubclass(ProjectImportError, ValueError)

    def test_message_preserved(self):
        err = ProjectImportError("test message")
        assert str(err) == "test message"


class TestBundleImmutability:
    def test_validate_does_not_mutate_bundle(self):
        bundle = _minimal_bundle(
            included_sections=["connections"],
            connections=[{"id": "c1"}],
        )
        original = copy.deepcopy(bundle)
        _validate_bundle(bundle)
        assert bundle == original
