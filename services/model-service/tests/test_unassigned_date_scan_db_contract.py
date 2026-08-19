"""Bug-6683 DB-backed contract test for _get_unassigned_date_cols.

Unlike the mock-based guards in test_calendar_autocreate_name_bounds.py, this
suite creates REAL rows in a scratch PostgreSQL schema and asserts the scan's
actual result set, so the exclusion semantics are proven against the database,
not against rendered SQL strings:

  * a fact table's physical date column IS surfaced;
  * a generated day-component UDA on the fact table (is_generated=True) is NOT
    re-consumed — the Bug-6683 compounding vector;
  * a non-generated user date UDA IS surfaced;
  * date columns on calendar spine tables (bound AND unbound / NULL
    calendar_table_id backlink) are NOT surfaced;
  * date columns on calendar-alias dim_detail tables (marked via
    calendar_table_id AND unmarked-but-same-source-physical-name) are NOT
    surfaced;
  * a table in a DIFFERENT source that merely shares a calendar spine's
    physical_name IS surfaced (the exclusion is source-scoped);
  * once a fact date column is joined to a calendar instance it is NOT
    surfaced (the second idempotency mechanism) — proven for BOTH a marked
    alias and an unmarked alias;
  * hierarchy-delete companion collection recognises an unmarked alias holding
    a generated level UDA (so DELETE hierarchy tears it down, no leak).

Skips cleanly when the local PostgreSQL is not reachable (CI without Docker).

Test escape: prior coverage asserted rendered WHERE-clause substrings and
mocked scan results, which could not catch a wrong-in-practice predicate.
Guard tier: T1 DB contract.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from shared.db.models import (
    CalendarTable,
    DataSource,
    HierarchyDefinition,
    HierarchyLevel,
    Join,
    Model,
    ModelColumn,
    ModelTable,
    Project,
    ProjectConnection,
    TenantBase,
    UserDefinedAttribute,
)
from src.api.hierarchies import (
    _collect_companion_alias_table_ids,
    _get_unassigned_date_cols,
)


def _db_url() -> str:
    url = os.environ.get("SYSTEM_DATABASE_URL")
    if url:
        return url
    pw = os.environ.get("POSTGRES_PASSWORD", "tessallite")
    return f"postgresql+asyncpg://tessallite:{pw}@localhost:5432/tessallite_system"


@pytest.mark.asyncio
async def test_unassigned_date_scan_db_contract():
    schema = f"bug6683_scan_{uuid.uuid4().hex[:12]}"
    admin_engine = create_async_engine(_db_url())
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        await admin_engine.dispose()
        pytest.skip(f"local PostgreSQL not reachable: {exc}")

    async with admin_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        _db_url(),
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(TenantBase.metadata.create_all)

        async with AsyncSession(engine, expire_on_commit=False) as db:
            project = Project(slug="p6683", display_name="P6683")
            db.add(project)
            await db.flush()
            model = Model(
                project_id=project.id, slug="m6683", display_name="M6683",
                seed="test-seed",
            )
            db.add(model)
            await db.flush()
            pconn = ProjectConnection(
                project_id=project.id, display_name="conn",
                connection_type="postgresql",
                encrypted_credentials=b"test", config={},
            )
            db.add(pconn)
            await db.flush()
            src_a = DataSource(
                model_id=model.id, project_connection_id=pconn.id,
                source_type="jdbc", display_name="src A", config={},
            )
            src_b = DataSource(
                model_id=model.id, project_connection_id=pconn.id,
                source_type="jdbc", display_name="src B", config={},
            )
            db.add_all([src_a, src_b])
            await db.flush()

            def _table(source, ttype, physical, alias, cal_id=None):
                return ModelTable(
                    model_id=model.id, source_id=source.id, table_type=ttype,
                    physical_name=physical, alias=alias, display_name=alias,
                    calendar_table_id=cal_id,
                )

            def _col(tbl, name, dtype="date"):
                return ModelColumn(
                    model_table_id=tbl.id, column_name=name,
                    display_name=name, data_type=dtype,
                )

            fact = _table(src_a, "fact", "demo.fact_sales", "fact_sales")
            db.add(fact)
            await db.flush()
            business_date = _col(fact, "business_date")
            settlement_date = _col(fact, "settlement_date")
            amount = _col(fact, "amount", dtype="numeric")
            db.add_all([business_date, settlement_date, amount])

            cal = CalendarTable(
                data_source_id=src_a.id, table_name="demo.calendar",
                dialect="postgresql", date_column="date_key",
            )
            db.add(cal)
            await db.flush()

            bound_spine = _table(src_a, "calendar", "demo.calendar", "cal_bound", cal.id)
            unbound_spine = _table(src_a, "calendar", "demo.calendar_unbound", "cal_unbound")
            db.add_all([bound_spine, unbound_spine])
            await db.flush()
            db.add_all([_col(bound_spine, "date_key"), _col(unbound_spine, "date_key")])

            # Marked alias (normal auto-create output).
            marked_alias = _table(src_a, "dim_detail", "demo.calendar", "bd_calendar", cal.id)
            # Unmarked alias (batch-date against the UNBOUND spine): dim_detail,
            # calendar_table_id NULL, shares the spine's physical_name+source.
            unmarked_alias = _table(src_a, "dim_detail", "demo.calendar_unbound", "bd_cal_unmarked")
            # Different-source table that merely shares the spine's physical
            # name: NOT a calendar instance, must stay eligible.
            other_source_dim = _table(src_b, "dim_detail", "demo.calendar_unbound", "other_src_dim")
            db.add_all([marked_alias, unmarked_alias, other_source_dim])
            await db.flush()
            marked_date_key = _col(marked_alias, "date_key")
            unmarked_date_key = _col(unmarked_alias, "date_key")
            db.add_all(
                [
                    marked_date_key,
                    unmarked_date_key,
                    _col(other_source_dim, "other_src_date"),
                ]
            )

            # The Bug-6683 compounding vector: generate-date's day-component
            # UDA — date-typed, generated, on the FACT table.
            db.add(
                UserDefinedAttribute(
                    model_id=model.id, table_id=fact.id,
                    name="business_date_day",
                    expression='CAST(("business_date") AS DATE)',
                    output_data_type="date",
                    description="Auto-generated for hierarchy 'business date quarter hierarchy' (day)",
                    validated=True, is_generated=True,
                )
            )
            # A user-authored date UDA must still be surfaced.
            db.add(
                UserDefinedAttribute(
                    model_id=model.id, table_id=fact.id,
                    name="ship_date_calc",
                    expression='CAST(("business_date") AS DATE)',
                    output_data_type="date",
                    validated=True, is_generated=False,
                )
            )
            await db.flush()

            rows = await _get_unassigned_date_cols(db, model.id)
            surfaced = {(r.column_name, r.is_uda) for r in rows}
            assert surfaced == {
                ("business_date", False),    # legit fact date column
                ("settlement_date", False),  # legit fact date column
                ("other_src_date", False),   # same physical name, other source
                ("ship_date_calc", True),    # user UDA
            }, f"unexpected scan result: {surfaced}"

            # Second idempotency mechanism: once a fact column is joined to a
            # calendar instance (what auto-create/batch-date themselves
            # persist), it no longer counts as unassigned on the next run.
            # This must hold for MARKED aliases (business_date) and for
            # UNMARKED aliases created against an unbound calendar
            # (settlement_date) — the marker-only join test regressed the
            # latter (round-3 external review).
            db.add_all(
                [
                    Join(
                        model_id=model.id,
                        left_table_id=fact.id, right_table_id=marked_alias.id,
                        join_type="many_to_one",
                        left_column_id=business_date.id,
                        right_column_id=marked_date_key.id,
                    ),
                    Join(
                        model_id=model.id,
                        left_table_id=fact.id, right_table_id=unmarked_alias.id,
                        join_type="many_to_one",
                        left_column_id=settlement_date.id,
                        right_column_id=unmarked_date_key.id,
                    ),
                ]
            )
            await db.flush()

            rows2 = await _get_unassigned_date_cols(db, model.id)
            surfaced2 = {(r.column_name, r.is_uda) for r in rows2}
            assert surfaced2 == {
                ("other_src_date", False),
                ("ship_date_calc", True),
            }, f"joined fact date column still surfaced: {surfaced2}"

            # Hierarchy-delete companion collection must also recognise the
            # UNMARKED alias so DELETE hierarchy tears it down instead of
            # leaking it (round-3 external review): a generated level UDA
            # living on the unmarked alias pins it as a companion candidate.
            gen_level_uda = UserDefinedAttribute(
                model_id=model.id, table_id=unmarked_alias.id,
                name="settlement_date_calendar_day",
                expression='CAST(("date_key") AS DATE)',
                output_data_type="date",
                validated=True, is_generated=True,
            )
            db.add(gen_level_uda)
            await db.flush()
            hierarchy = HierarchyDefinition(
                model_id=model.id, name="settlement_date Calendar",
                type="date_embedded", dimension_kind="time",
            )
            db.add(hierarchy)
            await db.flush()
            level = HierarchyLevel(
                hierarchy_id=hierarchy.id, name="Day", ordinal=0,
                key_attribute_id=gen_level_uda.id,
                key_attribute_source="user_defined_attribute",
            )
            db.add(level)
            await db.flush()

            # A calendar SPINE registration must NEVER be collected as a
            # companion, even when a hierarchy level keys on a UDA hosted on
            # it: deleting that hierarchy must not tear down the model's
            # calendar registration (round-4 external review).
            spine_uda = UserDefinedAttribute(
                model_id=model.id, table_id=bound_spine.id,
                name="spine_hosted_day",
                expression='CAST(("date_key") AS DATE)',
                output_data_type="date",
                validated=True, is_generated=True,
            )
            db.add(spine_uda)
            await db.flush()
            spine_level = HierarchyLevel(
                hierarchy_id=hierarchy.id, name="SpineDay", ordinal=1,
                key_attribute_id=spine_uda.id,
                key_attribute_source="user_defined_attribute",
            )
            db.add(spine_level)
            await db.flush()

            companions = await _collect_companion_alias_table_ids(
                db, model_id=model.id, levels=[level, spine_level]
            )
            assert unmarked_alias.id in companions, (
                "unmarked calendar alias not collected as a companion — "
                "DELETE hierarchy would leak it"
            )
            # The fact table must never be collected as a companion.
            assert fact.id not in companions
            # Spine registrations (bound or unbound) must never be collected.
            assert bound_spine.id not in companions
            assert unbound_spine.id not in companions
            await db.rollback()
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
