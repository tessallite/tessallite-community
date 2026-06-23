"""Tests for the cross-tenant snapshot rewriter (Phase 6)."""
from __future__ import annotations

import uuid

import pytest

from shared.model_snapshot.importer import (
    _collect_pks,
    _is_uuid_string,
    prepare_snapshot_for_import,
)


def _u() -> str:
    """Return a fresh stringified UUID."""
    return str(uuid.uuid4())


def test_is_uuid_string_recognises_valid_uuid():
    assert _is_uuid_string(_u()) is True


def test_is_uuid_string_rejects_garbage():
    assert _is_uuid_string("not-a-uuid") is False
    assert _is_uuid_string("12345678-1234-1234-1234-12345678") is False
    assert _is_uuid_string(None) is False  # type: ignore[arg-type]
    assert _is_uuid_string(123) is False  # type: ignore[arg-type]


def test_collect_pks_picks_up_every_id():
    snap = {
        "model": {"id": _u()},
        "tables": [{"id": _u()}, {"id": _u()}],
        "columns": [{"id": _u()}],
        "user_defined_attributes": [{"id": _u()}],
        "joins": [{"id": _u()}],
        "dimensions": [{"id": _u()}],
        "measures": [{"id": _u()}],
        "data_sources": [{"id": _u()}],
        "data_targets": [{"id": _u()}],
        "lineage_mappings": [{"id": _u()}],
        "hierarchies": [
            {
                "id": _u(),
                "levels": [
                    {"id": _u(), "attributes": [{"id": _u()}]},
                ],
            }
        ],
        "aggregates": [
            {
                "id": _u(),
                "columns": [{"id": _u()}],
                "refresh_policy": {"id": _u()},
            }
        ],
        "ai_scheduler_config": {"id": _u()},
        "uda_column_refs": [{"id": _u()}],
    }
    pk_map = _collect_pks(snap)
    # 1 model + 2 tables + 1 col + 1 uda + 1 join + 1 dim + 1 measure
    # + 1 source + 1 target + 1 lineage + 1 hier + 1 lvl + 1 lvl_attr
    # + 1 agg + 1 agg_col + 1 agg_policy + 1 sched + 1 uda_ref = 19
    assert len(pk_map) == 19
    # Every value should be a fresh, valid UUID string.
    for new_v in pk_map.values():
        assert _is_uuid_string(new_v)


def test_prepare_for_import_overrides_model_id():
    old_model_id = _u()
    new_model_id = uuid.uuid4()
    snap = {"model": {"id": old_model_id, "slug": "x"}}
    rewritten, missing = prepare_snapshot_for_import(
        snap, new_model_id=new_model_id
    )
    assert rewritten["model"]["id"] == str(new_model_id)
    assert missing == []


def test_prepare_for_import_rewrites_self_referential_uuids():
    """Hierarchy levels point at columns by uuid via key_attribute_id —
    that field is NOT a SQL FK in the schema, but the rewriter must
    still replace it because it references a column id from the same
    snapshot."""
    column_id = _u()
    snap = {
        "model": {"id": _u()},
        "columns": [{"id": column_id, "column_name": "country"}],
        "hierarchies": [
            {
                "id": _u(),
                "levels": [
                    {
                        "id": _u(),
                        "key_attribute_id": column_id,
                        "key_attribute_source": "physical_column",
                        "attributes": [
                            {
                                "id": _u(),
                                "attribute_id": column_id,
                                "attribute_source": "physical_column",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    new_column_id = rewritten["columns"][0]["id"]
    new_key_attr = rewritten["hierarchies"][0]["levels"][0]["key_attribute_id"]
    new_attr_id = (
        rewritten["hierarchies"][0]["levels"][0]["attributes"][0]["attribute_id"]
    )
    # Both references should now point at the rewritten column id.
    assert new_key_attr == new_column_id
    assert new_attr_id == new_column_id
    # And the new id must differ from the original.
    assert new_column_id != column_id


def test_prepare_for_import_remaps_connections():
    src_conn = _u()
    tgt_conn = _u()
    snap = {
        "model": {"id": _u()},
        "data_sources": [{"id": _u(), "project_connection_id": src_conn}],
        "data_targets": [],
    }
    rewritten, missing = prepare_snapshot_for_import(
        snap,
        new_model_id=uuid.uuid4(),
        connection_mapping={src_conn: tgt_conn},
    )
    assert rewritten["data_sources"][0]["project_connection_id"] == tgt_conn
    assert missing == []


def test_prepare_for_import_reports_missing_connections():
    src_conn = _u()
    snap = {
        "model": {"id": _u()},
        "data_sources": [
            {"id": _u(), "project_connection_id": src_conn, "display_name": "analytics"}
        ],
        "data_targets": [],
    }
    rewritten, missing = prepare_snapshot_for_import(
        snap,
        new_model_id=uuid.uuid4(),
        connection_mapping={},
    )
    assert missing == ["source:analytics"]
    # The connection id should NOT have been rewritten (no mapping entry).
    assert rewritten["data_sources"][0]["project_connection_id"] == src_conn


def test_prepare_for_import_strips_llm_config_id():
    """llm_provider_configs is a tenant-level table; the source
    tenant's llm_config_id has no meaning in the target tenant."""
    snap = {
        "model": {"id": _u(), "llm_config_id": _u()},
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    assert rewritten["model"]["llm_config_id"] is None


def test_prepare_for_import_does_not_mutate_input():
    """The importer must deep-copy so the caller's bundle stays clean."""
    src_conn = _u()
    snap = {
        "model": {"id": _u(), "llm_config_id": _u()},
        "data_sources": [{"id": _u(), "project_connection_id": src_conn}],
    }
    original_llm = snap["model"]["llm_config_id"]
    original_conn = snap["data_sources"][0]["project_connection_id"]
    prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4(),
        connection_mapping={src_conn: _u()},
    )
    assert snap["model"]["llm_config_id"] == original_llm
    assert snap["data_sources"][0]["project_connection_id"] == original_conn


def test_prepare_for_import_rewrites_join_fks():
    """Joins reference table and column ids — both must be remapped."""
    table_id = _u()
    column_id = _u()
    snap = {
        "model": {"id": _u()},
        "tables": [{"id": table_id}],
        "columns": [{"id": column_id, "model_table_id": table_id}],
        "joins": [
            {
                "id": _u(),
                "left_table_id": table_id,
                "right_table_id": table_id,
                "left_column_id": column_id,
                "right_column_id": column_id,
            }
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    new_table = rewritten["tables"][0]["id"]
    new_col = rewritten["columns"][0]["id"]
    j = rewritten["joins"][0]
    assert j["left_table_id"] == new_table
    assert j["right_table_id"] == new_table
    assert j["left_column_id"] == new_col
    assert j["right_column_id"] == new_col
    # And the column's model_table_id was remapped too.
    assert rewritten["columns"][0]["model_table_id"] == new_table


def test_prepare_for_import_rewrites_aggregate_children():
    """Aggregate columns reference measure id; refresh_policy references
    aggregate_definition_id."""
    measure_id = _u()
    agg_id = _u()
    snap = {
        "model": {"id": _u()},
        "measures": [{"id": measure_id}],
        "aggregates": [
            {
                "id": agg_id,
                "columns": [
                    {"id": _u(), "measure_id": measure_id, "aggregate_definition_id": agg_id}
                ],
                "refresh_policy": {
                    "id": _u(),
                    "aggregate_definition_id": agg_id,
                },
            }
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    new_measure = rewritten["measures"][0]["id"]
    new_agg = rewritten["aggregates"][0]["id"]
    agg_col = rewritten["aggregates"][0]["columns"][0]
    policy = rewritten["aggregates"][0]["refresh_policy"]
    assert agg_col["measure_id"] == new_measure
    assert agg_col["aggregate_definition_id"] == new_agg
    assert policy["aggregate_definition_id"] == new_agg
