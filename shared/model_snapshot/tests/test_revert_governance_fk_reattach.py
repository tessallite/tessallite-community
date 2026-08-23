"""Bug-6205 / Bug-6591 (reopened) — governance-revert FK re-attach edge paths.

On revert (``restore_governance=False``) the rehydrator preserves live
governance while rebuilding the definition tables/columns, then re-attaches two
FK couplings by id:

  * ``row_security_rules.mapping_table_id`` -> ``model_tables``
  * ``data_tag_columns.model_column_id``    -> ``model_columns``

The risky path is a revert to an OLDER definition that DROPPED the referenced
table/column. Fable deep-review flagged this path as untested and signalless.
These tests lock two guarantees:

  1. A dropped referenced table/column is handled fail-closed — the link is left
     detached (never re-pointed at a wrong table/column) and the rehydrate does
     NOT crash.
  2. A detached link raises an operator-visible signal: a persisted ModelAlert
     (category ``governance_revert``). The detachment is fail-closed — a detached
     row-security mapping makes the rule DENY affected users' queries at runtime
     (_load_mapping_table(None) -> RowSecurityCompileError -> 422), never widening
     row visibility; a detached tag link only arises when the column was dropped
     by the revert. The alert exists so the operator restores intended access.

When every referenced table/column still exists, the links re-attach and NO
alert is raised (no false alarms).
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.db.models import ModelAlert
from shared.model_snapshot import rehydrator
from shared.model_snapshot.rehydrator import rehydrate_into_live

# Every child-insert / truncate / validate helper the driver fans out to.
# Neutralised so the test isolates ONLY the inline FK re-attach block.
_NEUTRALISED = [
    "_truncate_model_children",
    "_insert_data_sources_and_targets",
    "_insert_calendar_tables",
    "_insert_tables_and_columns",
    "_insert_udas",
    "_insert_joins",
    "_insert_hierarchies",
    "_insert_dimensions",
    "_synthesize_missing_hierarchy_dimensions",
    "_insert_measures",
    "_insert_named_sets",
    "_insert_kpis",
    "_insert_drill_through_sets",
    "_insert_aggregate_lifecycle",
    "_insert_personas",
    "_insert_pockets",
    "_insert_data_tags",
    "_insert_row_security",
    "_insert_glossary",
    "_insert_source_statistics",
    "_insert_source_join_statistics",
    "_insert_ai_scheduler",
    "_insert_lineage",
    "_insert_model_parameters",
    "_insert_model_alias_map",
    "_insert_refresh_sla_config",
    "_insert_data_quality_rules",
    "_insert_entity_translations",
    "_validate_preserved_aggregates",
    "_validate_preserved_pockets",
]


def _result(rows):
    res = MagicMock()
    res.all.return_value = rows
    res.first.return_value = rows[0] if rows else None
    scalars = MagicMock()
    scalars.all.return_value = [r[0] if isinstance(r, tuple) else r for r in rows]
    scalars.first.return_value = None
    res.scalars.return_value = scalars
    return res


def _build_db(*, rs_rows, dtc_rows, live_table_ids, live_col_ids, live_tag_ids):
    """AsyncSession mock that routes each SELECT to the right result set by the
    selected entity + column names, and records inserts + db.add() objects."""
    db = AsyncMock()
    inserts: list[tuple[str, dict]] = []
    added: list = []

    model = MagicMock()
    model.seed = "seed123"
    db.get = AsyncMock(return_value=model)

    def _sig(stmt):
        try:
            descs = stmt.column_descriptions
        except Exception:
            return None
        names = tuple(d.get("name") for d in descs)
        ent = descs[0].get("entity") if descs else None
        return (getattr(ent, "__name__", None), names)

    async def _execute(stmt):
        is_select = getattr(stmt, "is_select", False)
        if is_select:
            sig = _sig(stmt)
            ent, names = sig if sig else (None, ())
            if ent == "RowSecurityRule" and "mapping_table_id" in names:
                return _result(rs_rows)
            if names == ("tag_id", "model_column_id") or "model_column_id" in names and ent is None:
                return _result(dtc_rows)
            if ent == "ModelTable":
                return _result([(t,) for t in live_table_ids])
            if ent == "ModelColumn":
                return _result([(c,) for c in live_col_ids])
            if ent == "DataTag":
                return _result([(t,) for t in live_tag_ids])
            return _result([])
        # non-select: update / insert / delete
        table = getattr(getattr(stmt, "table", None), "name", None)
        if table is not None:
            try:
                params = dict(stmt.compile().params)
            except Exception:
                params = {}
            inserts.append((table, params))
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db.add = MagicMock(side_effect=lambda obj: added.append(obj))
    db._inserts = inserts
    db._added = added
    return db


async def _run(db):
    snapshot = {
        "schema_version": 1,
        "model": {},
        "hierarchies": [],
        "aggregates": [],
    }
    model_id = uuid.uuid4()
    with_patches = {name: AsyncMock() for name in _NEUTRALISED}
    with_patches["_insert_aggregates"] = AsyncMock(return_value=[])
    from unittest.mock import patch
    with patch.multiple(rehydrator, **with_patches):
        await rehydrate_into_live(
            model_id, snapshot, db,
            preserve_aggregates=False,
            preserve_pockets=False,
            restore_governance=False,
        )
    return model_id


@pytest.mark.asyncio
async def test_dropped_table_leaves_rs_rule_detached_with_alert():
    """The reverted-to definition dropped the table a preserved row-security rule
    pointed at. The rule must NOT be re-pointed (fail-closed) and an operator
    alert must be raised."""
    rule_id = uuid.uuid4()
    dropped_table = uuid.uuid4()
    live_table = uuid.uuid4()

    db = _build_db(
        rs_rows=[(rule_id, dropped_table)],
        dtc_rows=[],
        live_table_ids={live_table},        # dropped_table is gone
        live_col_ids=set(),
        live_tag_ids=set(),
    )
    await _run(db)

    # The rule was detached to NULL up front; it was NOT re-pointed to the
    # dropped table (no update carries the dropped table id as mapping_table_id).
    reattach = [
        p for t, p in db._inserts
        if t == "row_security_rules" and p.get("mapping_table_id") == dropped_table
    ]
    assert reattach == []
    # Operator-visible signal: a governance_revert ModelAlert was persisted.
    alerts = [a for a in db._added if isinstance(a, ModelAlert)]
    assert len(alerts) == 1
    assert alerts[0].category == "governance_revert"
    assert alerts[0].severity == "warning"


@pytest.mark.asyncio
async def test_dropped_column_leaves_tag_link_detached_with_alert():
    """The reverted-to definition dropped a tagged column. The surviving tag link
    re-attaches; the dropped one does not; an alert is raised."""
    tag_id = uuid.uuid4()
    dropped_col = uuid.uuid4()
    live_col = uuid.uuid4()

    db = _build_db(
        rs_rows=[],
        dtc_rows=[(tag_id, dropped_col), (tag_id, live_col)],
        live_table_ids=set(),
        live_col_ids={live_col},            # dropped_col is gone
        live_tag_ids={tag_id},
    )
    await _run(db)

    link_rows = [p for t, p in db._inserts if t == "data_tag_columns"]
    linked_cols = {p["model_column_id"] for p in link_rows}
    assert linked_cols == {live_col}
    assert dropped_col not in linked_cols
    alerts = [a for a in db._added if isinstance(a, ModelAlert)]
    assert len(alerts) == 1
    assert alerts[0].category == "governance_revert"


@pytest.mark.asyncio
async def test_all_references_intact_reattaches_without_alert():
    """When every referenced table/column still exists, both links re-attach and
    NO alert is raised (no false alarms)."""
    rule_id = uuid.uuid4()
    table_id = uuid.uuid4()
    tag_id = uuid.uuid4()
    col_id = uuid.uuid4()

    db = _build_db(
        rs_rows=[(rule_id, table_id)],
        dtc_rows=[(tag_id, col_id)],
        live_table_ids={table_id},
        live_col_ids={col_id},
        live_tag_ids={tag_id},
    )
    await _run(db)

    # Row-security rule re-pointed to its (surviving) table.
    reattach = [
        p for t, p in db._inserts
        if t == "row_security_rules" and p.get("mapping_table_id") == table_id
    ]
    assert len(reattach) == 1
    # Tag link re-attached.
    link_rows = [p for t, p in db._inserts if t == "data_tag_columns"]
    assert {p["model_column_id"] for p in link_rows} == {col_id}
    # No detachment -> no alert.
    alerts = [a for a in db._added if isinstance(a, ModelAlert)]
    assert alerts == []
