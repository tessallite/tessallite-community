"""Bug-9261 — a persona default filter with an operator the query gate cannot
apply must be refused at IMPORT, on both writers that bypass the REST API.

The gate (query-router ``persona_gate.merge_default_filters``) now refuses
the query outright when it meets such a filter. These tests pin the other half
of the remediation: a YAML bundle or a snapshot rehydrate carrying that filter
is refused before it is persisted, using the same shared validator the API
uses (``persona_filter_value_is_valid``), so a bad row cannot land through one
writer and surface as a refused query later.

Test escape: the import writers already called the shared validator, but no
test asserted the refusal, so a regression that dropped the call would have
passed silently. Guard: this file. Tier: T2 (shared contract across writers).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import yaml

from shared.model_snapshot.rehydrator import (
    SnapshotSchemaError,
    _validate_imported_persona,
)
from shared.model_snapshot.yaml_deserialiser import YamlImportError, parse_model_yaml


_BAD_VALUES = [
    {"bogus_op": "EMEA"},     # operator outside the canonical set
    {},                       # no operator
    {"eq": 1, "neq": 2},      # ambiguous multi-key dict
    {"between": [1]},         # BETWEEN with one bound
]


def _yaml_doc(filters):
    return {
        "model": {"name": "sales", "display_name": "Sales"},
        "tables": [{"name": "orders", "source_table": "public.orders"}],
        "personas": [
            {"name": "regional", "filters": filters, "audience_roles": ["analyst"]},
        ],
    }


@pytest.mark.parametrize("bad", _BAD_VALUES)
def test_bug_9261_yaml_import_refuses_uncoercible_default_filter(bad):
    with pytest.raises(YamlImportError) as exc:
        parse_model_yaml(yaml.safe_dump(_yaml_doc({"region": bad})))
    msg = str(exc.value)
    assert "regional" in msg
    assert "region" in msg


def test_bug_9261_yaml_import_accepts_valid_operator_form():
    snap = parse_model_yaml(
        yaml.safe_dump(_yaml_doc({"region": {"in": ["EMEA", "APAC"]}}))
    )
    assert snap["personas"][0]["default_filters"] == {
        "region": {"in": ["EMEA", "APAC"]}
    }


def _row(default_filters):
    return {
        "slug": "regional",
        "name": "regional",
        "audience_roles": ["analyst"],
        "included_measure_ids": [],
        "included_dimension_ids": [],
        "included_hierarchy_ids": [],
        "default_filters": default_filters,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _BAD_VALUES)
async def test_bug_9261_rehydrate_refuses_uncoercible_default_filter(bad):
    db = AsyncMock()
    with pytest.raises(SnapshotSchemaError) as exc:
        await _validate_imported_persona(db, uuid.uuid4(), _row({"region": bad}))
    msg = str(exc.value)
    assert "regional" in msg
    assert "region" in msg
    # Refused BEFORE any lookup: the operator check needs no database.
    db.execute.assert_not_called()
