"""Coverage tests for snapshot schema v2 additions (Bug-106).

Validates that:

1. ``SNAPSHOT_SCHEMA_VERSION`` == 2.
2. The serialiser emits a key for every v2 entity family (personas,
   pockets, drill-through, calendars, row-security, glossary, lifecycle,
   source statistics).
3. The cross-tenant importer remaps PKs for every v2 family — including
   nested children (pocket predicates, glossary synonyms/attachments,
   source column statistics).
4. Cross-tenant strip rules clear ``glossary_entries[].created_by`` since
   the source-tenant user UUID has no meaning post-import.
"""
from __future__ import annotations

import uuid

from shared.model_snapshot.importer import (
    _collect_pks,
    prepare_snapshot_for_import,
)
from shared.model_snapshot.serialiser import SNAPSHOT_SCHEMA_VERSION


V2_TOP_LEVEL_KEYS = (
    "drill_through_sets",
    "calendar_tables",
    "personas",
    "pockets",
    "row_security_rules",
    "glossary_entries",
    "aggregate_lifecycle_events",
    "source_statistics",
    "source_join_statistics",
)


def _u() -> str:
    return str(uuid.uuid4())


def test_schema_version_is_current():
    # Bumped to v3 by F-013-06 (added the six previously-missing model-scoped
    # config families). The v2 families this module exercises still travel; the
    # constant just moved forward.
    assert SNAPSHOT_SCHEMA_VERSION == 3


def test_collect_pks_remaps_v2_flat_families():
    """Every flat v2 family contributes its row id to the pk_map."""
    snap = {
        "model": {"id": _u()},
        "drill_through_sets": [{"id": _u()}],
        "calendar_tables": [{"id": _u()}],
        "personas": [{"id": _u()}, {"id": _u()}],
        "row_security_rules": [{"id": _u()}],
        "aggregate_lifecycle_events": [{"id": _u()}],
        "source_join_statistics": [{"id": _u()}],
    }
    pk_map = _collect_pks(snap)
    # 1 model + 1 drill + 1 cal + 2 personas + 1 rls + 1 lifecycle + 1 join_stats = 8
    assert len(pk_map) == 8


def test_collect_pks_remaps_pocket_nested_children():
    pocket_id = _u()
    pred_id = _u()
    policy_id = _u()
    snap = {
        "model": {"id": _u()},
        "pockets": [
            {
                "id": pocket_id,
                "predicates": [{"id": pred_id}, {"id": _u()}],
                "refresh_policy": {"id": policy_id},
            }
        ],
    }
    pk_map = _collect_pks(snap)
    assert pocket_id in pk_map
    assert pred_id in pk_map
    assert policy_id in pk_map


def test_collect_pks_remaps_glossary_nested_children():
    entry_id = _u()
    syn_id = _u()
    att_id = _u()
    snap = {
        "model": {"id": _u()},
        "glossary_entries": [
            {
                "id": entry_id,
                "synonyms": [{"id": syn_id}],
                "attachments": [{"id": att_id}],
            }
        ],
    }
    pk_map = _collect_pks(snap)
    assert entry_id in pk_map
    assert syn_id in pk_map
    assert att_id in pk_map


def test_collect_pks_remaps_source_statistics_nested_columns():
    stats_id = _u()
    col_stats_id = _u()
    snap = {
        "model": {"id": _u()},
        "source_statistics": [
            {"id": stats_id, "columns": [{"id": col_stats_id}]}
        ],
    }
    pk_map = _collect_pks(snap)
    assert stats_id in pk_map
    assert col_stats_id in pk_map


def test_prepare_for_import_strips_glossary_created_by():
    snap = {
        "model": {"id": _u()},
        "glossary_entries": [
            {"id": _u(), "term": "MAU", "created_by": _u()},
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    assert rewritten["glossary_entries"][0]["created_by"] is None


def test_prepare_for_import_remaps_persona_included_ids():
    """Personas store included_measure_ids / included_dimension_ids /
    included_hierarchy_ids as JSONB lists of UUIDs. The recursive walker
    must rewrite every entry so the persona's includes survive cross-tenant
    import."""
    measure_id = _u()
    dim_id = _u()
    snap = {
        "model": {"id": _u()},
        "measures": [{"id": measure_id}],
        "dimensions": [{"id": dim_id}],
        "personas": [
            {
                "id": _u(),
                "name": "execs",
                "slug": "execs",
                "included_measure_ids": [measure_id],
                "included_dimension_ids": [dim_id],
            }
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    new_measure = rewritten["measures"][0]["id"]
    new_dim = rewritten["dimensions"][0]["id"]
    persona = rewritten["personas"][0]
    assert persona["included_measure_ids"] == [new_measure]
    assert persona["included_dimension_ids"] == [new_dim]


def test_prepare_for_import_remaps_pocket_persona_ref():
    """PocketDefinition.persona_id (FK SET NULL) must follow the persona's
    new id once remapped."""
    persona_id = _u()
    snap = {
        "model": {"id": _u()},
        "personas": [{"id": persona_id, "name": "execs", "slug": "execs"}],
        "pockets": [
            {
                "id": _u(),
                "persona_id": persona_id,
                "target_id": _u(),
                "predicates": [],
            }
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    new_persona = rewritten["personas"][0]["id"]
    assert rewritten["pockets"][0]["persona_id"] == new_persona


def test_prepare_for_import_remaps_drill_through_refs():
    """DrillThroughSet references measure_id (CASCADE) and source_table_id
    (SET NULL). Both must be remapped."""
    measure_id = _u()
    table_id = _u()
    snap = {
        "model": {"id": _u()},
        "tables": [{"id": table_id}],
        "measures": [{"id": measure_id}],
        "drill_through_sets": [
            {
                "id": _u(),
                "measure_id": measure_id,
                "source_table_id": table_id,
            }
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    new_measure = rewritten["measures"][0]["id"]
    new_table = rewritten["tables"][0]["id"]
    drill = rewritten["drill_through_sets"][0]
    assert drill["measure_id"] == new_measure
    assert drill["source_table_id"] == new_table


def test_prepare_for_import_remaps_row_security_mapping_table():
    """RowSecurityRule.mapping_table_id references model_tables (RESTRICT)."""
    table_id = _u()
    snap = {
        "model": {"id": _u()},
        "tables": [{"id": table_id}],
        "row_security_rules": [
            {
                "id": _u(),
                "name": "region",
                "rule_type": "user_mapping",
                "mapping_table_id": table_id,
            }
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    new_table = rewritten["tables"][0]["id"]
    assert rewritten["row_security_rules"][0]["mapping_table_id"] == new_table


def test_collect_pks_remaps_data_tags():
    """F-008-09: data tags are part of the snapshot and their PKs remap."""
    tag_id = _u()
    snap = {
        "model": {"id": _u()},
        "data_tags": [{"id": tag_id, "tag_name": "PII", "column_ids": []}],
    }
    pk_map = _collect_pks(snap)
    assert tag_id in pk_map


def test_prepare_for_import_remaps_data_tag_columns_and_restrictions():
    """Tag column assignments and persona tag restrictions must follow the
    remapped column / persona / tag ids so column-level security survives
    a cross-tenant import intact (F-008-09)."""
    col_id = _u()
    persona_id = _u()
    tag_id = _u()
    snap = {
        "model": {"id": _u()},
        "columns": [{"id": col_id}],
        "personas": [{"id": persona_id, "name": "partner", "slug": "partner"}],
        "data_tags": [
            {"id": tag_id, "tag_name": "PII", "column_ids": [col_id]}
        ],
        "persona_tag_restrictions": [
            {"persona_id": persona_id, "data_tag_id": tag_id}
        ],
    }
    rewritten, _ = prepare_snapshot_for_import(
        snap, new_model_id=uuid.uuid4()
    )
    new_col = rewritten["columns"][0]["id"]
    new_persona = rewritten["personas"][0]["id"]
    new_tag = rewritten["data_tags"][0]["id"]
    assert new_tag != tag_id
    assert rewritten["data_tags"][0]["column_ids"] == [new_col]
    restriction = rewritten["persona_tag_restrictions"][0]
    assert restriction["persona_id"] == new_persona
    assert restriction["data_tag_id"] == new_tag


def test_collect_pks_remaps_kpis_and_named_sets():
    """Bug-1022: KPI and named-set PKs must be remapped or any snapshot
    import into the same tenant collides on the original primary keys."""
    kpi_id = _u()
    set_id = _u()
    measure_id = _u()
    snap = {
        "model": {"id": _u()},
        "measures": [{"id": measure_id}],
        "kpis": [{"id": kpi_id, "measure_id": measure_id}],
        "named_sets": [{"id": set_id}],
    }
    pk_map = _collect_pks(snap)
    assert kpi_id in pk_map
    assert set_id in pk_map

    rewritten, _ = prepare_snapshot_for_import(snap, new_model_id=uuid.uuid4())
    assert rewritten["kpis"][0]["id"] != kpi_id
    assert rewritten["named_sets"][0]["id"] != set_id
    # FK follows the remapped measure id.
    assert rewritten["kpis"][0]["measure_id"] == rewritten["measures"][0]["id"]
