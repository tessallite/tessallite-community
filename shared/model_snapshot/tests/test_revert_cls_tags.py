"""Bug-6205 (definition-only revert contract) + F-013-03 governance restore.

Two contracts are locked here:

1. DEPLOY / IMPORT restore mechanism (``restore_governance=True``).
   ``_insert_data_tags`` re-creates data tags, re-attaches tag-to-column links
   to the rebuilt columns, and re-attaches persona tag restrictions. This is
   what runs on a fresh deploy/import, where the whole model — governance
   included — is materialised from the snapshot. The first two tests below lock
   that restoration so the original fail-open CLS regression (F-013-03) cannot
   return on the deploy/import path.

2. REVERT is DEFINITION ONLY (``restore_governance=False`` — Bug-6205).
   Revert rewrites a model's SHAPE but must PRESERVE live governance: personas,
   data tags (+ column assignments and persona tag restrictions), and
   row-security rules. Rolling a model's definition back must never silently
   change who can see which rows/columns. The gate test at the bottom proves
   ``rehydrate_into_live`` skips the governance inserts when
   ``restore_governance=False`` and runs them when True.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.model_snapshot import rehydrator
from shared.model_snapshot.rehydrator import _insert_data_tags, rehydrate_into_live


def _capture_db(live_column_ids, live_persona_ids):
    """Mock AsyncSession: column/persona existence queries return the supplied
    live ids; insert() statements are recorded by table name."""
    db = AsyncMock()
    inserts: list[tuple[str, dict]] = []
    calls = {"n": 0}

    col_result = MagicMock()
    col_result.all.return_value = [(c,) for c in live_column_ids]
    persona_result = MagicMock()
    persona_result.all.return_value = [(p,) for p in live_persona_ids]

    async def _execute(stmt):
        # SELECTs come first (columns, then personas); inserts in between.
        table = getattr(getattr(stmt, "table", None), "name", None)
        if table is None:
            # SELECT — alternate columns then personas by call order.
            calls["n"] += 1
            return col_result if calls["n"] == 1 else persona_result
        try:
            params = dict(stmt.compile().params)
        except Exception:
            params = {}
        inserts.append((table, params))
        return MagicMock()

    db.execute = AsyncMock(side_effect=_execute)
    db._inserts = inserts
    return db


@pytest.mark.asyncio
async def test_deploy_import_restores_tag_column_links_and_persona_restrictions():
    """Deploy/import (restore_governance=True) re-materialises CLS from the
    snapshot: tags re-created, surviving column links re-attached, persona
    restrictions re-attached."""
    model_id = uuid.uuid4()
    tag_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    col_a = uuid.uuid4()
    col_b = uuid.uuid4()
    col_gone = uuid.uuid4()  # a column dropped in the reverted-to version

    snapshot = {
        "data_tags": [
            {
                "id": str(tag_id),
                "tag_name": "pii",
                "column_ids": [str(col_a), str(col_b), str(col_gone)],
            }
        ],
        "persona_tag_restrictions": [
            {"persona_id": str(persona_id), "data_tag_id": str(tag_id)}
        ],
    }

    # col_gone no longer exists post-rehydrate; persona survives.
    db = _capture_db(live_column_ids={col_a, col_b}, live_persona_ids={persona_id})
    await _insert_data_tags(model_id, snapshot, db)

    tables = [t for t, _ in db._inserts]
    # The tag is re-created.
    assert "data_tags" in tables
    # Both surviving column links are re-attached; the dropped column is skipped.
    link_rows = [p for t, p in db._inserts if t == "data_tag_columns"]
    linked_cols = {r["model_column_id"] for r in link_rows}
    assert linked_cols == {col_a, col_b}
    assert col_gone not in linked_cols
    # The persona restriction is re-attached (CLS is restored, not fail-open).
    restr_rows = [p for t, p in db._inserts if t == "persona_tag_restrictions"]
    assert len(restr_rows) == 1
    assert restr_rows[0]["persona_id"] == persona_id
    assert restr_rows[0]["data_tag_id"] == tag_id


@pytest.mark.asyncio
async def test_restriction_dropped_when_persona_absent():
    """On deploy/import restore, if the persona was removed in the snapshot its
    restriction is not re-created (no dangling FK), but the tag + column links
    still are."""
    model_id = uuid.uuid4()
    tag_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    col_a = uuid.uuid4()

    snapshot = {
        "data_tags": [
            {"id": str(tag_id), "tag_name": "pii", "column_ids": [str(col_a)]}
        ],
        "persona_tag_restrictions": [
            {"persona_id": str(persona_id), "data_tag_id": str(tag_id)}
        ],
    }
    db = _capture_db(live_column_ids={col_a}, live_persona_ids=set())  # persona gone
    await _insert_data_tags(model_id, snapshot, db)

    assert any(t == "data_tag_columns" for t, _ in db._inserts)
    assert not any(t == "persona_tag_restrictions" for t, _ in db._inserts)


# ---------------------------------------------------------------------------
# Bug-6205 — the revert-vs-deploy/import governance gate
# ---------------------------------------------------------------------------

# Every child-insert / truncate / validate / guard helper rehydrate_into_live
# fans out to. Neutralised so the test isolates ONLY the governance gate.
_NEUTRALISED_HELPERS = [
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
    "_insert_pockets",
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

# The three governance inserts whose invocation the gate controls.
_GOVERNANCE_HELPERS = ["_insert_personas", "_insert_data_tags", "_insert_row_security"]


def _empty_result() -> MagicMock:
    """A query result that answers empty to every access shape used above."""
    res = MagicMock()
    res.all.return_value = []
    res.first.return_value = None
    scalars = MagicMock()
    scalars.all.return_value = []
    scalars.first.return_value = None
    res.scalars.return_value = scalars
    return res


def _model_db() -> AsyncMock:
    db = AsyncMock()
    model = MagicMock()
    model.seed = "seed123"
    db.get = AsyncMock(return_value=model)
    db.execute = AsyncMock(return_value=_empty_result())
    db.add = MagicMock()
    return db


async def _run_rehydrate(*, restore_governance: bool):
    """Drive rehydrate_into_live with every fan-out helper stubbed, returning
    the governance-helper mocks so the caller can assert invocation."""
    # A snapshot that carries 'hierarchies' (skips the wipe guard) and an empty
    # 'model' (no scalar update). schema_version is mandatory.
    snapshot = {
        "schema_version": 1,
        "model": {},
        "hierarchies": [],
        "aggregates": [],
    }
    model_id = uuid.uuid4()
    db = _model_db()

    patches = {name: AsyncMock() for name in _NEUTRALISED_HELPERS}
    patches["_insert_aggregates"] = AsyncMock(return_value=[])
    gov = {name: AsyncMock() for name in _GOVERNANCE_HELPERS}
    patches.update(gov)

    with patch.multiple(rehydrator, **patches):
        # revert combo preserves the materialised artifacts (aggregates/pockets);
        # deploy/import default rebuilds them. Named sets are always rebuilt.
        # Only restore_governance differs for the gate under test.
        preserve = not restore_governance
        await rehydrate_into_live(
            model_id, snapshot, db,
            preserve_aggregates=preserve,
            preserve_pockets=preserve,
            restore_governance=restore_governance,
        )
    return gov


@pytest.mark.asyncio
async def test_revert_skips_governance_restore():
    """Bug-6205: on revert (restore_governance=False) the rehydrator must NOT
    call the governance inserts — live personas / data tags / row-security are
    preserved, not rolled back to the snapshot."""
    gov = await _run_rehydrate(restore_governance=False)
    gov["_insert_personas"].assert_not_awaited()
    gov["_insert_data_tags"].assert_not_awaited()
    gov["_insert_row_security"].assert_not_awaited()


@pytest.mark.asyncio
async def test_deploy_import_runs_full_governance_restore():
    """Deploy/import (the default restore_governance=True) DOES materialise
    governance from the snapshot — the gate must not suppress it there."""
    gov = await _run_rehydrate(restore_governance=True)
    gov["_insert_personas"].assert_awaited_once()
    gov["_insert_data_tags"].assert_awaited_once()
    gov["_insert_row_security"].assert_awaited_once()
