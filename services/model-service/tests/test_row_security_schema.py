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
