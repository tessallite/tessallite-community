"""Unit tests for RowSecurityRule pydantic shape validators (Phase 5.1).

Covers the dual-shape CHECK-constraint contract expressed in
:class:`RowSecurityRuleBase`: role_predicate requires predicate + roles and
forbids mapping_* fields; user_mapping requires mapping_* fields and
forbids predicate / roles. The ORM-level CHECK constraint in migration
0035 mirrors these rules at the DB layer; these tests lock the API-layer
contract.
"""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from shared.schemas.pydantic_models import (
    RowSecurityRuleCreate,
    RowSecurityRuleUpdate,
)

pytestmark = pytest.mark.unit


class TestRolePredicateShape:
    def test_accepts_well_formed_role_predicate(self):
        r = RowSecurityRuleCreate(
            name="north_only",
            dimension_path="region.region_code",
            rule_type="role_predicate",
            predicate_expression="dimension_equals('region.region_code', 'NORTH')",
            applies_to_roles=["region_manager_north"],
        )
        assert r.rule_type == "role_predicate"
        assert r.mapping_table_id is None
        assert r.mapping_user_column is None
        assert r.mapping_value_column is None

    def test_rejects_missing_predicate_expression(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="role_predicate",
                applies_to_roles=["r"],
            )

    def test_rejects_missing_applies_to_roles(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="role_predicate",
                predicate_expression="dimension_equals('y','Z')",
            )

    def test_rejects_empty_applies_to_roles(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="role_predicate",
                predicate_expression="dimension_equals('y','Z')",
                applies_to_roles=[],
            )

    def test_rejects_mapping_fields_set(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="role_predicate",
                predicate_expression="dimension_equals('y','Z')",
                applies_to_roles=["r"],
                mapping_table_id=uuid.uuid4(),
                mapping_user_column="u",
                mapping_value_column="v",
            )

    @pytest.mark.parametrize("attribute_source", ["saml_claim", "oidc_scope"])
    def test_rejects_claim_or_scope_source_without_claim_name(self, attribute_source):
        # Bug-5904: claim/scope-sourced role predicates must require
        # attribute_claim_name so rules cannot save inertly.
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="claim_backed",
                dimension_path="region.region_code",
                rule_type="role_predicate",
                predicate_expression="dimension_equals('region.region_code', 'NORTH')",
                applies_to_roles=["finance"],
                attribute_source=attribute_source,
            )

    @pytest.mark.parametrize("attribute_source", ["saml_claim", "oidc_scope"])
    def test_rejects_claim_or_scope_source_with_blank_claim_name(self, attribute_source):
        # Bug-5904: a whitespace-only claim name is just as inert as a
        # missing one at runtime (predicate_compiler treats it as falsy).
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="claim_backed",
                dimension_path="region.region_code",
                rule_type="role_predicate",
                predicate_expression="dimension_equals('region.region_code', 'NORTH')",
                applies_to_roles=["finance"],
                attribute_source=attribute_source,
                attribute_claim_name="   ",
            )

    @pytest.mark.parametrize("attribute_source", ["saml_claim", "oidc_scope"])
    def test_accepts_claim_or_scope_source_with_claim_name(self, attribute_source):
        # Bug-5904 companion: the legitimate case must still save.
        r = RowSecurityRuleCreate(
            name="claim_backed",
            dimension_path="region.region_code",
            rule_type="role_predicate",
            predicate_expression="dimension_equals('region.region_code', 'NORTH')",
            applies_to_roles=["finance"],
            attribute_source=attribute_source,
            attribute_claim_name="department",
        )
        assert r.attribute_claim_name == "department"

    @pytest.mark.parametrize("attribute_source", ["saml_claim", "oidc_scope"])
    def test_trims_padded_claim_name(self, attribute_source):
        # Bug-5904 hardening: predicate_compiler does an exact-key lookup
        # (principal.claims.get(claim_name)) at runtime, so a claim name
        # saved with stray whitespace would never match the real JWT claim
        # — the same silently-inert-rule class this bug fixes, just via a
        # near-miss instead of a blank. Normalize at save time.
        r = RowSecurityRuleCreate(
            name="claim_backed",
            dimension_path="region.region_code",
            rule_type="role_predicate",
            predicate_expression="dimension_equals('region.region_code', 'NORTH')",
            applies_to_roles=["finance"],
            attribute_source=attribute_source,
            attribute_claim_name="  department  ",
        )
        assert r.attribute_claim_name == "department"


class TestUserMappingShape:
    def test_accepts_well_formed_user_mapping(self):
        r = RowSecurityRuleCreate(
            name="per_user_region",
            dimension_path="region.region_code",
            rule_type="user_mapping",
            mapping_table_id=uuid.uuid4(),
            mapping_user_column="user_id",
            mapping_value_column="region_code",
        )
        assert r.rule_type == "user_mapping"
        assert r.predicate_expression is None
        assert r.applies_to_roles is None

    def test_rejects_missing_mapping_table(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="user_mapping",
                mapping_user_column="u",
                mapping_value_column="v",
            )

    def test_rejects_missing_user_column(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="user_mapping",
                mapping_table_id=uuid.uuid4(),
                mapping_value_column="v",
            )

    def test_rejects_missing_value_column(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="user_mapping",
                mapping_table_id=uuid.uuid4(),
                mapping_user_column="u",
            )

    def test_rejects_predicate_expression_set(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="user_mapping",
                mapping_table_id=uuid.uuid4(),
                mapping_user_column="u",
                mapping_value_column="v",
                predicate_expression="not allowed",
            )

    def test_rejects_applies_to_roles_set(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="user_mapping",
                mapping_table_id=uuid.uuid4(),
                mapping_user_column="u",
                mapping_value_column="v",
                applies_to_roles=["nope"],
            )

    def test_rejects_non_default_attribute_source(self):
        # Bug-5905: user_mapping always keys by user_identity at runtime
        # (predicate_compiler.py); a non-default attribute_source would be
        # silently ignored by the compiler, so it must be rejected at save.
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="user_mapping",
                mapping_table_id=uuid.uuid4(),
                mapping_user_column="u",
                mapping_value_column="v",
                attribute_source="saml_claim",
                attribute_claim_name="claim",
            )

    def test_rejects_attribute_claim_name_set(self):
        # Bug-5905: attribute_claim_name is meaningless for user_mapping
        # even when attribute_source is left at its default.
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x",
                dimension_path="y",
                rule_type="user_mapping",
                mapping_table_id=uuid.uuid4(),
                mapping_user_column="u",
                mapping_value_column="v",
                attribute_claim_name="claim",
            )


class TestRuleTypeAllowList:
    def test_rejects_unknown_rule_type(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleCreate(
                name="x", dimension_path="y", rule_type="bogus"
            )


class TestUpdatePatchShape:
    """Updates are a partial patch (all fields optional). The rule_type
    cannot be changed after creation — it would require rebuilding the
    shape contract — so it is omitted from Update.

    F-007-08: the update path used to have NO field validation at all, so a
    PATCH could silently neutralise a rule by clearing its roles. The update
    schema now rejects an explicitly-empty applies_to_roles and an
    out-of-allow-list attribute_source, while leaving omitted fields alone.
    """

    def test_update_all_optional(self):
        upd = RowSecurityRuleUpdate()
        dumped = upd.model_dump(exclude_unset=True)
        assert dumped == {}

    def test_update_sets_is_enabled_only(self):
        upd = RowSecurityRuleUpdate(is_enabled=False)
        dumped = upd.model_dump(exclude_unset=True)
        assert dumped == {"is_enabled": False}

    def test_update_rejects_empty_applies_to_roles(self):
        # F-007-08 fail-closed: an empty roles list is NOT NULL (passes the
        # DB CHECK) but matches nothing and carries no '*' wildcard, so the
        # rule would silently stop filtering. The update path must reject it.
        with pytest.raises(ValidationError):
            RowSecurityRuleUpdate(applies_to_roles=[])

    def test_update_accepts_non_empty_applies_to_roles(self):
        upd = RowSecurityRuleUpdate(applies_to_roles=["region_manager_north"])
        assert upd.model_dump(exclude_unset=True) == {
            "applies_to_roles": ["region_manager_north"]
        }

    def test_update_accepts_wildcard_role(self):
        upd = RowSecurityRuleUpdate(applies_to_roles=["*"])
        assert upd.applies_to_roles == ["*"]

    def test_update_omitting_roles_is_allowed(self):
        # None means "don't change the roles" — must NOT be rejected.
        upd = RowSecurityRuleUpdate(is_enabled=True)
        assert "applies_to_roles" not in upd.model_dump(exclude_unset=True)

    def test_update_rejects_unknown_attribute_source(self):
        with pytest.raises(ValidationError):
            RowSecurityRuleUpdate(attribute_source="bogus_source")

    def test_update_accepts_known_attribute_source(self):
        upd = RowSecurityRuleUpdate(attribute_source="idp_group")
        assert upd.attribute_source == "idp_group"

    def test_update_trims_padded_claim_name(self):
        # Bug-5904 hardening, update-path companion to the create-schema
        # trim test above.
        upd = RowSecurityRuleUpdate(attribute_claim_name="  department  ")
        assert upd.attribute_claim_name == "department"

    def test_update_collapses_whitespace_only_claim_name_to_blank(self):
        # The schema-level trim alone does not reject — the row_security.py
        # handler rejects a blank effective claim name for a claim-sourced
        # rule (see test_row_security_api.py Bug-5904 cases); this test
        # only locks that the schema normalizes whitespace-only input to ""
        # rather than passing it through unchanged.
        upd = RowSecurityRuleUpdate(attribute_claim_name="   ")
        assert upd.attribute_claim_name == ""
