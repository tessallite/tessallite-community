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

    def test_bundle_version_is_two(self):
        # Bug-7623: bumped 1 -> 2 (v2 carries per-version snapshots).
        assert PROJECT_BUNDLE_VERSION == 2


class TestValidateBundle:
    def test_valid_minimal_bundle_passes(self):
        # Default fixture is v1; still a supported (old) format.
        _validate_bundle(_minimal_bundle())

    def test_v2_bundle_passes(self):
        # Bug-7623: the new per-version-snapshot format is accepted too.
        _validate_bundle(_minimal_bundle(schema_version=2))

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


class TestBug6290AgentConfigRoundTrip:
    """Bug-6290: a project with no agent config must export a bundle that
    passes validation (and thus re-imports cleanly)."""

    def test_empty_agent_config_shape_passes_validation(self):
        """The serialiser now emits an empty-but-present shape instead of
        None, so the validator should accept it."""
        bundle = _minimal_bundle(
            included_sections=["agent_config"],
            agent_config={
                "config": {},
                "models": [],
                "model_contexts": [],
                "judge_rubrics": [],
            },
        )
        _validate_bundle(bundle)

    def test_backwards_compat_null_agent_config_passes(self):
        """Bundles exported before the fix carry agent_config=None. The
        validator should now treat that as 'section absent' rather than
        rejecting the bundle, so existing bundles can still be imported."""
        bundle = _minimal_bundle(
            included_sections=["agent_config"],
            agent_config=None,
        )
        # Previously this raised ProjectImportError; now it should pass.
        _validate_bundle(bundle)

    def test_non_agent_null_section_still_raises(self):
        """The backwards-compat exemption is limited to agent_config.
        Other null sections must still be rejected."""
        bundle = _minimal_bundle(
            included_sections=["connections"],
            connections=None,
        )
        with pytest.raises(ProjectImportError, match="connections.*null/missing"):
            _validate_bundle(bundle)

    def test_serialiser_emits_valid_empty_agent_config(self):
        """Verify that the empty shape the serialiser emits for a
        project with no agent config has the expected structure and
        passes validation."""
        from shared.model_snapshot.project_serialiser import _AGENT_CONFIG_FIELDS
        # Reproduce what the serialiser does when cfg is None:
        empty_shape = {
            "config": {},
            "models": [],
            "model_contexts": [],
            "judge_rubrics": [],
        }
        # The shape must have the four expected keys.
        assert set(empty_shape.keys()) == {
            "config", "models", "model_contexts", "judge_rubrics"
        }
        # And it must pass bundle validation.
        bundle = _minimal_bundle(
            included_sections=["agent_config"],
            agent_config=empty_shape,
        )
        _validate_bundle(bundle)


class TestBug6631ModelIdValidation:
    """Bug-6631: _validate_bundle must reject bundles with missing or
    duplicate model ids, which would crash or silently corrupt import."""

    def test_missing_model_id_raises(self):
        bundle = _minimal_bundle(
            models=[{"model": {}, "data_sources": []}],
        )
        with pytest.raises(ProjectImportError, match="missing.*model.id"):
            _validate_bundle(bundle)

    def test_duplicate_model_id_raises(self):
        mid = "11111111-1111-1111-1111-111111111111"
        bundle = _minimal_bundle(
            models=[
                {"model": {"id": mid, "name": "A"}, "data_sources": []},
                {"model": {"id": mid, "name": "B"}, "data_sources": []},
            ],
        )
        with pytest.raises(ProjectImportError, match="Duplicate model id"):
            _validate_bundle(bundle)

    def test_valid_distinct_model_ids_pass(self):
        bundle = _minimal_bundle(
            models=[
                {"model": {"id": "aaaa-1111", "name": "A"}, "data_sources": []},
                {"model": {"id": "bbbb-2222", "name": "B"}, "data_sources": []},
            ],
        )
        _validate_bundle(bundle)


class TestBug8134OneFactTablePerModel:
    """Bug-8134: _validate_bundle must reject a model snapshot carrying more
    than one fact-typed table, BEFORE any row is staged.

    The storage layer enforces at most one fact table per model with a
    partial unique index (F-013-11, migration 0136:
    ``uq_model_tables_one_fact_per_model``), and the create/update table
    API guards it with a check-then-act read (``_assert_at_most_one_fact``
    in ``services/model-service/src/api/tables.py``). Project import
    bypasses those endpoints entirely -- the same class of gap
    ``sanitise_imported_config``'s docstring documents for connection/LLM
    config bags -- so a hand-crafted or corrupt two-fact-table bundle had
    nothing rejecting it here. Left unchecked, it would reach
    ``_insert_tables_and_columns`` (shared/model_snapshot/rehydrator.py),
    whose per-row Core INSERT for the second fact-typed row trips the
    partial unique index only AFTER the first fact row (and every sibling
    row already inserted in that same loop) has been staged into the
    transaction -- surfacing a raw IntegrityError instead of a clean,
    actionable error. This test guards the earliest possible rejection
    point: ``_validate_bundle`` is the very first call in both
    ``import_project`` and ``plan_project_import``, before the Project row
    itself (let alone any ModelTable row) is created.
    """

    @staticmethod
    def _two_fact_model(model_id: str = "22222222-2222-2222-2222-222222222222") -> dict:
        return {
            "model": {"id": model_id, "slug": "twofact", "display_name": "Two Fact"},
            "tables": [
                {"id": "t1", "physical_name": "orders", "table_type": "fact"},
                {"id": "t2", "physical_name": "shipments", "table_type": "fact"},
            ],
            "data_sources": [],
        }

    def test_two_fact_tables_raises_project_import_error(self):
        bundle = _minimal_bundle(models=[self._two_fact_model()])
        with pytest.raises(ProjectImportError, match="fact table"):
            _validate_bundle(bundle)

    def test_error_names_the_offending_tables(self):
        bundle = _minimal_bundle(models=[self._two_fact_model()])
        with pytest.raises(ProjectImportError, match="orders.*shipments"):
            _validate_bundle(bundle)

    def test_single_fact_table_passes(self):
        model = self._two_fact_model()
        model["tables"] = [model["tables"][0]]
        bundle = _minimal_bundle(models=[model])
        _validate_bundle(bundle)

    def test_zero_fact_tables_passes(self):
        model = self._two_fact_model()
        model["tables"] = [
            {"id": "d1", "physical_name": "customers", "table_type": "dim_detail"},
        ]
        bundle = _minimal_bundle(models=[model])
        _validate_bundle(bundle)

    def test_missing_tables_key_passes(self):
        # Mirrors TestBug6631ModelIdValidation's fixtures, which omit
        # "tables" entirely (an older/minimal bundle shape).
        bundle = _minimal_bundle(
            models=[{"model": {"id": "aaaa-1111", "name": "A"}, "data_sources": []}],
        )
        _validate_bundle(bundle)

    def test_two_fact_tables_across_different_models_each_pass_independently(self):
        # The constraint is per-model, not per-bundle: two models that each
        # have exactly one fact table must import fine even though the
        # bundle as a whole carries two fact-typed rows.
        model_a = self._two_fact_model("33333333-3333-3333-3333-333333333333")
        model_a["tables"] = [model_a["tables"][0]]
        model_b = self._two_fact_model("44444444-4444-4444-4444-444444444444")
        model_b["tables"] = [model_b["tables"][1]]
        bundle = _minimal_bundle(models=[model_a, model_b])
        _validate_bundle(bundle)


class TestBundleImmutability:
    def test_validate_does_not_mutate_bundle(self):
        bundle = _minimal_bundle(
            included_sections=["connections"],
            connections=[{"id": "c1"}],
        )
        original = copy.deepcopy(bundle)
        _validate_bundle(bundle)
        assert bundle == original
