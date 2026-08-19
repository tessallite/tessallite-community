"""
Unit tests for rehydrator FK-ordering and snapshot immutability (Bug-171).

Bug: ``rehydrate_into_live`` inserted ``ModelTable`` rows (which carry
``calendar_table_id`` FK -> ``calendar_tables``) BEFORE inserting
``CalendarTable`` rows, causing an ``IntegrityError`` on any model with a
calendar binding.  The transaction rolled back silently and the model
stayed empty.

Also: multiple inserter functions mutated the snapshot dict in-place via
``.pop()``, corrupting the in-memory representation.
"""
from __future__ import annotations

import contextlib
import copy
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.model_snapshot.rehydrator import (
    SnapshotSchemaError,
    _insert_aggregates,
    _insert_glossary,
    _insert_hierarchies,
    _insert_pockets,
    _insert_row_security,
    _insert_source_statistics,
    _insert_tables_and_columns,
    _insert_calendar_tables,
    rehydrate_into_live,
)

pytestmark = pytest.mark.unit


def _u() -> str:
    return str(uuid.uuid4())


_MODEL_ID = uuid.UUID(_u())


def _make_full_snapshot() -> dict[str, Any]:
    """Build a v2 snapshot with all nested entity families populated."""
    hier_id = _u()
    level_id = _u()
    measure_id = _u()
    agg_id = _u()
    pocket_id = _u()
    glossary_id = _u()
    stat_id = _u()
    source_id = _u()
    cal_table_id = _u()
    fact_table_id = _u()
    dim_table_id = _u()

    return {
        "schema_version": 2,
        "exported_at": "2026-01-01T00:00:00",
        "model": {
            "id": str(_MODEL_ID),
            "slug": "test-model",
            "display_name": "Test Model",
            "status": "active",
        },
        "data_sources": [
            {
                "id": source_id,
                "model_id": str(_MODEL_ID),
                "source_type": "postgresql",
                "display_name": "pg-source",
                "project_connection_id": _u(),
                "default_schema": "public",
                "config": {},
            }
        ],
        "data_targets": [],
        "calendar_tables": [
            {
                "id": cal_table_id,
                "data_source_id": source_id,
                "table_name": "dim_date",
                "dialect": "postgresql",
                "calendar_type": "standard",
                "date_column": "date_key",
                "fiscal_year_start_month": 1,
            }
        ],
        "tables": [
            {
                "id": fact_table_id,
                "model_id": str(_MODEL_ID),
                "source_id": source_id,
                "table_type": "fact",
                "physical_name": "fact_sales",
                "alias": "fact_sales",
                "display_name": "Fact Sales",
            },
            {
                "id": dim_table_id,
                "model_id": str(_MODEL_ID),
                "source_id": source_id,
                "table_type": "calendar",
                "physical_name": "dim_date",
                "alias": "dim_date",
                "display_name": "Date Dim",
                "calendar_table_id": cal_table_id,
            },
        ],
        "columns": [],
        "user_defined_attributes": [],
        "uda_column_refs": [],
        "joins": [],
        "hierarchies": [
            {
                "id": hier_id,
                "model_id": str(_MODEL_ID),
                "name": "Date Hierarchy",
                "display_name": "Date",
                "levels": [
                    {
                        "id": level_id,
                        "hierarchy_id": hier_id,
                        "name": "Year",
                        "ordinal": 0,
                        "attributes": [
                            {"id": _u(), "level_id": level_id, "column_id": _u()}
                        ],
                    }
                ],
            }
        ],
        "dimensions": [
            {
                "id": _u(),
                "model_id": str(_MODEL_ID),
                "name": "date_key",
                "display_name": "Date Key",
            }
        ],
        "measures": [
            {
                "id": measure_id,
                "model_id": str(_MODEL_ID),
                "name": "revenue",
                "display_name": "Revenue",
                "aggregation_function": "sum",
                "measure_type": "base",
            }
        ],
        "drill_through_sets": [],
        "aggregates": [
            {
                "id": agg_id,
                "model_id": str(_MODEL_ID),
                "physical_table_name": "agg_test",
                "target_schema": "public",
                "status": "active",
                "grain": ["date_key"],
                "columns": [
                    {"id": _u(), "aggregate_definition_id": agg_id, "column_name": "revenue"}
                ],
                "refresh_policy": {
                    "id": _u(), "aggregate_definition_id": agg_id, "strategy": "full"
                },
            }
        ],
        "personas": [],
        "pockets": [
            {
                "id": pocket_id,
                "model_id": str(_MODEL_ID),
                "name": "test-pocket",
                "status": "active",
                "predicates": [
                    {
                        "id": _u(),
                        "pocket_definition_id": pocket_id,
                        "column_name": "region",
                        "operator": "eq",
                        "value": "US",
                    }
                ],
                "refresh_policy": {
                    "id": _u(), "pocket_definition_id": pocket_id, "strategy": "full"
                },
            }
        ],
        "row_security_rules": [],
        "glossary_entries": [
            {
                "id": glossary_id,
                "model_id": str(_MODEL_ID),
                "term": "Revenue",
                "definition": "Total revenue",
                "synonyms": [
                    {"id": _u(), "entry_id": glossary_id, "synonym": "sales"}
                ],
                "attachments": [
                    {"id": _u(), "entry_id": glossary_id, "url": "https://example.com"}
                ],
            }
        ],
        "aggregate_lifecycle_events": [],
        "source_statistics": [
            {
                "id": stat_id,
                "data_source_id": source_id,
                "columns": [
                    {"id": _u(), "source_statistics_id": stat_id, "column_name": "revenue"}
                ],
            }
        ],
        "source_join_statistics": [],
        "lineage_mappings": [],
        "model_settings": {},
    }


# ---------------------------------------------------------------------------
# Test 1: FK insertion ordering via patched inserter functions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_calendar_tables_inserted_before_model_tables():
    """CalendarTable INSERT must precede ModelTable INSERT (Bug-171 FK fix).

    We patch each inserter function and record the call order to verify
    the correct dependency chain:
      DataSource -> CalendarTable -> ModelTable
    """
    snap = _make_full_snapshot()
    call_order: list[str] = []

    async def _track(name):
        async def _fn(*_a, **_k):
            call_order.append(name)
        return _fn

    model_ns = MagicMock()
    model_ns.id = _MODEL_ID

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=model_ns)
    result_mock = MagicMock()
    result_mock.all.return_value = []
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.add = MagicMock()

    base = "shared.model_snapshot.rehydrator"
    inserter_names = [
        "_insert_data_sources_and_targets", "_insert_calendar_tables",
        "_insert_tables_and_columns", "_insert_udas", "_insert_joins",
        "_insert_hierarchies", "_insert_dimensions", "_insert_measures",
        "_insert_drill_through_sets",
        "_insert_aggregates", "_insert_personas", "_insert_pockets",
        "_insert_row_security", "_insert_glossary",
        "_insert_aggregate_lifecycle", "_insert_source_statistics",
        "_insert_source_join_statistics", "_insert_ai_scheduler",
        "_insert_lineage",
    ]
    short = {n: n.replace("_insert_", "").replace("_and_targets", "") for n in inserter_names}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(f"{base}._truncate_model_children", new=AsyncMock()))
        for fn_name in inserter_names:
            tag = short[fn_name]
            stack.enter_context(
                patch(f"{base}.{fn_name}", side_effect=lambda *a, _t=tag, **k: call_order.append(_t))
            )
        await rehydrate_into_live(_MODEL_ID, snap, mock_db)

    assert "data_sources" in call_order
    assert "calendar_tables" in call_order
    assert "tables_and_columns" in call_order

    ds_idx = call_order.index("data_sources")
    cal_idx = call_order.index("calendar_tables")
    tbl_idx = call_order.index("tables_and_columns")

    assert ds_idx < cal_idx, (
        f"data_sources (idx {ds_idx}) must precede calendar_tables (idx {cal_idx})"
    )
    assert cal_idx < tbl_idx, (
        f"calendar_tables (idx {cal_idx}) must precede tables_and_columns (idx {tbl_idx}); "
        f"ModelTable.calendar_table_id FK requires calendar_tables to exist first; "
        f"full order: {call_order}"
    )


@pytest.mark.asyncio
async def test_full_insert_ordering_respects_fk_dependencies():
    """Verify the full insert dependency chain: sources -> calendars ->
    tables -> UDAs -> joins -> hierarchies -> dims -> measures -> ..."""
    snap = _make_full_snapshot()
    call_order: list[str] = []

    model_ns = MagicMock()
    model_ns.id = _MODEL_ID

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=model_ns)
    result_mock = MagicMock()
    result_mock.all.return_value = []
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.add = MagicMock()

    base = "shared.model_snapshot.rehydrator"
    tag_map = {
        "_insert_data_sources_and_targets": "data_sources",
        "_insert_calendar_tables": "calendar_tables",
        "_insert_tables_and_columns": "tables",
        "_insert_udas": "udas",
        "_insert_joins": "joins",
        "_insert_hierarchies": "hierarchies",
        "_insert_dimensions": "dimensions",
        "_insert_measures": "measures",
        "_insert_drill_through_sets": "drill_through",
        "_insert_aggregates": "aggregates",
        "_insert_personas": "personas",
        "_insert_pockets": "pockets",
        "_insert_row_security": "row_security",
        "_insert_glossary": "glossary",
        "_insert_aggregate_lifecycle": "agg_lifecycle",
        "_insert_source_statistics": "source_stats",
        "_insert_source_join_statistics": "join_stats",
        "_insert_ai_scheduler": "ai_scheduler",
        "_insert_lineage": "lineage",
        # v3 model-scoped config families (F-013-06 / Bug-3578). They run last
        # in rehydrate_into_live, after _insert_lineage, in this source order.
        "_insert_model_parameters": "model_parameters",
        "_insert_model_alias_map": "model_alias_map",
        "_insert_refresh_sla_config": "refresh_sla_config",
        "_insert_data_quality_rules": "data_quality_rules",
        "_insert_entity_translations": "entity_translations",
    }

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(f"{base}._truncate_model_children", new=AsyncMock()))
        for fn_name, tag in tag_map.items():
            stack.enter_context(
                patch(f"{base}.{fn_name}", side_effect=lambda *a, _t=tag, **kw: call_order.append(_t))
            )
        await rehydrate_into_live(_MODEL_ID, snap, mock_db)

    required_orderings = [
        ("data_sources", "calendar_tables"),
        ("calendar_tables", "tables"),
        ("tables", "udas"),
        ("tables", "joins"),
        ("measures", "drill_through"),
        # Bug-3578: the v3 model-scoped config families run last, after the
        # core graph and after _insert_lineage, in this source order. Asserting
        # them here pins their full-cycle invocation by rehydrate_into_live to
        # the ordering test (not only the isolated mock-capture tests). Their
        # FK shape does not force this order among themselves — e.g.
        # EntityTranslation.entity_id is a soft reference with no DB FK
        # (rehydrator.py:416-419) — so this asserts their ACTUAL invocation
        # order, which is the contract the rehydrator must keep stable.
        ("lineage", "model_parameters"),
        ("model_parameters", "model_alias_map"),
        ("model_alias_map", "refresh_sla_config"),
        ("refresh_sla_config", "data_quality_rules"),
        ("data_quality_rules", "entity_translations"),
    ]
    for before, after in required_orderings:
        if before in call_order and after in call_order:
            assert call_order.index(before) < call_order.index(after), (
                f"{before} must precede {after} in insert order; "
                f"got: {call_order}"
            )

    # All five v3 inserters must actually be invoked in the full cycle — the
    # `if before in call_order` guard above silently skips a pair when an
    # inserter is missing, so assert their presence explicitly here.
    for v3_tag in (
        "model_parameters",
        "model_alias_map",
        "refresh_sla_config",
        "data_quality_rules",
        "entity_translations",
    ):
        assert v3_tag in call_order, (
            f"v3 inserter '{v3_tag}' was not invoked by rehydrate_into_live; "
            f"got: {call_order}"
        )


# ---------------------------------------------------------------------------
# Test 2: Snapshot immutability — inserters must not mutate the dict
# ---------------------------------------------------------------------------

def _noop_db() -> AsyncMock:
    """DB session mock that accepts any execute() and returns empty results."""
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_insert_hierarchies_does_not_mutate_snapshot():
    """_insert_hierarchies must not pop() levels."""
    snap = _make_full_snapshot()
    original = copy.deepcopy(snap["hierarchies"])
    db = _noop_db()

    await _insert_hierarchies(_MODEL_ID, snap, db)

    for i, h in enumerate(snap["hierarchies"]):
        assert "levels" in h, f"hierarchy[{i}] lost 'levels' key"
        for j, lvl in enumerate(h["levels"]):
            assert "attributes" in lvl, f"hierarchy[{i}].levels[{j}] lost 'attributes' key"

    assert snap["hierarchies"] == original


@pytest.mark.asyncio
async def test_insert_aggregates_does_not_mutate_snapshot():
    """_insert_aggregates must not pop() columns or refresh_policy."""
    snap = _make_full_snapshot()
    original_aggs = copy.deepcopy(snap["aggregates"])
    db = _noop_db()

    await _insert_aggregates(_MODEL_ID, snap, db)

    for i, a in enumerate(snap["aggregates"]):
        assert "columns" in a, f"aggregate[{i}] lost 'columns' key"
        assert "refresh_policy" in a, f"aggregate[{i}] lost 'refresh_policy' key"

    assert snap["aggregates"] == original_aggs


@pytest.mark.asyncio
async def test_insert_pockets_does_not_mutate_snapshot():
    """_insert_pockets must not pop() predicates or refresh_policy."""
    snap = _make_full_snapshot()
    original_pockets = copy.deepcopy(snap["pockets"])
    db = _noop_db()

    await _insert_pockets(_MODEL_ID, snap, db)

    for i, p in enumerate(snap["pockets"]):
        assert "predicates" in p, f"pocket[{i}] lost 'predicates' key"
        assert "refresh_policy" in p, f"pocket[{i}] lost 'refresh_policy' key"

    assert snap["pockets"] == original_pockets


@pytest.mark.asyncio
async def test_insert_glossary_does_not_mutate_snapshot():
    """_insert_glossary must not pop() synonyms or attachments."""
    snap = _make_full_snapshot()
    original_glossary = copy.deepcopy(snap["glossary_entries"])
    db = _noop_db()

    await _insert_glossary(_MODEL_ID, snap, db)

    for i, g in enumerate(snap["glossary_entries"]):
        assert "synonyms" in g, f"glossary[{i}] lost 'synonyms' key"
        assert "attachments" in g, f"glossary[{i}] lost 'attachments' key"

    assert snap["glossary_entries"] == original_glossary


@pytest.mark.asyncio
async def test_insert_source_statistics_does_not_mutate_snapshot():
    """_insert_source_statistics must not pop() columns."""
    snap = _make_full_snapshot()
    original_stats = copy.deepcopy(snap["source_statistics"])
    db = _noop_db()

    await _insert_source_statistics(_MODEL_ID, snap, db)

    for i, st in enumerate(snap["source_statistics"]):
        assert "columns" in st, f"source_statistics[{i}] lost 'columns' key"

    assert snap["source_statistics"] == original_stats


# ---------------------------------------------------------------------------
# Test 3: Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revert_empty_snapshot_succeeds():
    """Reverting to a snapshot with no tables should not error."""
    snap = {
        "schema_version": 2,
        "model": {"id": str(_MODEL_ID), "slug": "empty"},
        "data_sources": [],
        "data_targets": [],
        "calendar_tables": [],
        "tables": [],
        "columns": [],
        "user_defined_attributes": [],
        "uda_column_refs": [],
        "joins": [],
        "hierarchies": [],
        "dimensions": [],
        "measures": [],
        "drill_through_sets": [],
        "aggregates": [],
        "personas": [],
        "pockets": [],
        "row_security_rules": [],
        "glossary_entries": [],
        "aggregate_lifecycle_events": [],
        "source_statistics": [],
        "source_join_statistics": [],
        "lineage_mappings": [],
        "model_settings": {},
    }

    model_ns = MagicMock()
    model_ns.id = _MODEL_ID

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=model_ns)
    result_mock = MagicMock()
    result_mock.all.return_value = []
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.add = MagicMock()

    with patch("shared.model_snapshot.rehydrator._truncate_model_children", new=AsyncMock()):
        await rehydrate_into_live(_MODEL_ID, snap, mock_db)


# ---------------------------------------------------------------------------
# Test 4: Bug-6132 — rehydrator rejects uncompilable RLS DSL at import
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_row_security_rejects_unsupported_dsl():
    """Bug-6132: dimension_in is not a supported DSL function.

    The rehydrator must fail loud (SnapshotSchemaError) when a
    role_predicate rule uses an uncompilable expression, instead of
    silently inserting an inert rule that hard-blocks every matched caller.
    """
    snap = {
        "row_security_rules": [
            {
                "id": _u(),
                "model_id": str(_MODEL_ID),
                "name": "Bad DSL rule",
                "dimension_path": "channel_code",
                "rule_type": "role_predicate",
                "predicate_expression": "dimension_in('channel_code', ['POS', 'WEB'])",
                "applies_to_roles": ["account_manager"],
                "attribute_source": "jwt_role",
                "attribute_claim_name": None,
                "is_enabled": True,
                "mapping_table_id": None,
                "mapping_user_column": None,
                "mapping_value_column": None,
            }
        ]
    }
    db = _noop_db()

    with pytest.raises(SnapshotSchemaError, match="uncompilable predicate"):
        await _insert_row_security(_MODEL_ID, snap, db)


@pytest.mark.asyncio
async def test_insert_row_security_accepts_valid_in_dsl():
    """Bug-6132: the supported in() form must pass validation and insert.

    Verifies that the rehydrator accepts the correct DSL and calls
    db.execute to insert the rule row.
    """
    snap = {
        "row_security_rules": [
            {
                "id": _u(),
                "model_id": str(_MODEL_ID),
                "name": "Valid DSL rule",
                "dimension_path": "channel_code",
                "rule_type": "role_predicate",
                "predicate_expression": "in('channel_code', 'POS', 'WEB', 'MOBILE')",
                "applies_to_roles": ["account_manager"],
                "attribute_source": "jwt_role",
                "attribute_claim_name": None,
                "is_enabled": True,
                "mapping_table_id": None,
                "mapping_user_column": None,
                "mapping_value_column": None,
            }
        ]
    }
    db = _noop_db()

    # Should not raise
    await _insert_row_security(_MODEL_ID, snap, db)

    # Verify the rule was actually inserted via db.execute
    assert db.execute.call_count >= 1, (
        "db.execute should have been called to insert the row-security rule"
    )


@pytest.mark.asyncio
async def test_insert_row_security_skips_validation_for_user_mapping():
    """Bug-6132: user_mapping rules have no predicate_expression to compile.

    The DSL compile-validation must only run for role_predicate rules; a
    user_mapping rule with no predicate_expression must pass through
    without error.
    """
    snap = {
        "row_security_rules": [
            {
                "id": _u(),
                "model_id": str(_MODEL_ID),
                "name": "User mapping rule",
                "dimension_path": "region_code",
                "rule_type": "user_mapping",
                "predicate_expression": None,
                "applies_to_roles": ["*"],
                "attribute_source": "jwt_role",
                "attribute_claim_name": None,
                "is_enabled": True,
                "mapping_table_id": _u(),
                "mapping_user_column": "user_email",
                "mapping_value_column": "region_code",
            }
        ]
    }
    db = _noop_db()

    # Should not raise
    await _insert_row_security(_MODEL_ID, snap, db)
    assert db.execute.call_count >= 1


@pytest.mark.asyncio
async def test_insert_row_security_rejects_empty_predicate():
    """Bug-6132: a role_predicate with an empty predicate_expression would
    crash at runtime (_compile_dsl_expression raises on empty strings).

    The rehydrator must reject it at import time with a clear error rather
    than silently inserting a rule that will 500 every matched caller.
    """
    snap = {
        "row_security_rules": [
            {
                "id": _u(),
                "model_id": str(_MODEL_ID),
                "name": "Empty predicate rule",
                "dimension_path": "channel_code",
                "rule_type": "role_predicate",
                "predicate_expression": "",
                "applies_to_roles": ["account_manager"],
                "attribute_source": "jwt_role",
                "attribute_claim_name": None,
                "is_enabled": True,
                "mapping_table_id": None,
                "mapping_user_column": None,
                "mapping_value_column": None,
            }
        ]
    }
    db = _noop_db()

    with pytest.raises(SnapshotSchemaError, match="empty predicate_expression"):
        await _insert_row_security(_MODEL_ID, snap, db)


@pytest.mark.asyncio
async def test_insert_row_security_rejects_none_predicate():
    """Bug-6132: a role_predicate with predicate_expression=None must also
    be rejected at import time (same crash path as empty string).
    """
    snap = {
        "row_security_rules": [
            {
                "id": _u(),
                "model_id": str(_MODEL_ID),
                "name": "None predicate rule",
                "dimension_path": "channel_code",
                "rule_type": "role_predicate",
                "predicate_expression": None,
                "applies_to_roles": ["account_manager"],
                "attribute_source": "jwt_role",
                "attribute_claim_name": None,
                "is_enabled": True,
                "mapping_table_id": None,
                "mapping_user_column": None,
                "mapping_value_column": None,
            }
        ]
    }
    db = _noop_db()

    with pytest.raises(SnapshotSchemaError, match="empty predicate_expression"):
        await _insert_row_security(_MODEL_ID, snap, db)
