"""Bug-9392 / L9-F1 — the live routes and the deployed snapshot must agree.

``effective_description`` has TWO producers for ONE contract:

  * the LIVE routes (``src/api/_scope.py::glossary_text_for_target`` and its
    batch sibling), read by the Explorer and the Excel task pane;
  * the DEPLOYED snapshot (``shared/model_snapshot/serialiser.py::
    _merge_effective_descriptions``), read by the JDBC and XMLA catalogues.

A user sees both surfaces for the same object, so a rule added to one and not
the other is a visible product defect, not an internal inconsistency. Bug-9392
added the column-attachment fallback to the serialiser only: a dimension whose
glossary term is attached to its PHYSICAL COLUMN (about a third of live terms —
that is where bootstrap-proposed terms land) showed the curated definition in
Power BI and the raw column description in the task pane.

This test seeds ONE set of real rows in a scratch PostgreSQL schema and drives
BOTH producers off it, so parity is proven rather than restated:

  1. direct dimension attachment, WITH a competing column attachment — the
     direct term wins on both sides;
  2. column attachment only (the orphaned case Bug-9392 is about) — the column
     term reaches both sides;
  3. no attachment at all — both sides fall back to the raw description.

Both the single-row route path (``get_dimension``/``get_measure`` ->
``_build_response`` with no prefetch) and the batched list path
(``list_dimensions``/``list_measures`` -> ``_glossary_texts_for_targets``) are
exercised, because they are separate implementations of the same precedence and
the N+1-elimination batch is where a fallback is easiest to drop.

Pre-fix, cases 2 fail: the live routes returned the raw description while the
serialiser returned the glossary definition.

Skips cleanly when PostgreSQL is not reachable (coding tiers must not require a
live stack). Guard tier: T1 producer/consumer contract.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from shared.db.models import (
    DataSource,
    Dimension,
    GlossaryAttachment,
    GlossaryEntry,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    Project,
    ProjectConnection,
    TenantBase,
)
from shared.model_snapshot.serialiser import (
    _merge_effective_descriptions,
    _row_to_dict,
)
from src.api.dimensions import _build_response as build_dimension_response
from src.api.dimensions import _glossary_texts_for_targets as dimension_glossary_texts
from src.api.measures import _build_response as build_measure_response
from src.api.measures import _glossary_texts_for_targets as measure_glossary_texts


def _db_url() -> str:
    url = os.environ.get("SYSTEM_DATABASE_URL")
    if url:
        return url
    pw = os.environ.get("POSTGRES_PASSWORD", "tessallite")
    return f"postgresql+asyncpg://tessallite:{pw}@localhost:5432/tessallite_system"


async def _snapshot_effective_descriptions(db, model_id) -> tuple[dict, dict]:
    """Run the REAL serialiser merge over the same rows the routes read.

    Builds the ``glossary_entries`` / ``dimensions`` / ``measures`` snapshot
    fragments exactly as ``snapshot_model`` does (``_row_to_dict`` plus the
    per-entry attachment list) and applies ``_merge_effective_descriptions``,
    so this side of the parity is the shipped code path and not a restatement
    of its rules.
    """
    from sqlalchemy import select

    entries = (
        await db.execute(
            select(GlossaryEntry).where(GlossaryEntry.model_id == model_id)
        )
    ).scalars().all()
    entry_dicts = []
    for entry in entries:
        as_dict = _row_to_dict(entry, exclude=("created_at", "updated_at"))
        attachments = (
            await db.execute(
                select(GlossaryAttachment).where(
                    GlossaryAttachment.entry_id == entry.id
                )
            )
        ).scalars().all()
        as_dict["attachments"] = [_row_to_dict(a) for a in attachments]
        entry_dicts.append(as_dict)

    dims = (
        await db.execute(select(Dimension).where(Dimension.model_id == model_id))
    ).scalars().all()
    measures = (
        await db.execute(select(Measure).where(Measure.model_id == model_id))
    ).scalars().all()
    snap = {
        "glossary_entries": entry_dicts,
        "dimensions": [_row_to_dict(d) for d in dims],
        "measures": [_row_to_dict(m) for m in measures],
    }
    _merge_effective_descriptions(snap)
    return (
        {row["name"]: row["effective_description"] for row in snap["dimensions"]},
        {row["name"]: row["effective_description"] for row in snap["measures"]},
    )


@pytest.mark.asyncio
async def test_live_routes_and_snapshot_agree_on_effective_description():
    schema = f"bug9392_parity_{uuid.uuid4().hex[:12]}"
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
            project = Project(slug="p9392", display_name="P9392")
            db.add(project)
            await db.flush()
            model = Model(
                project_id=project.id, slug="m9392", display_name="M9392",
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
            source = DataSource(
                model_id=model.id, project_connection_id=pconn.id,
                source_type="jdbc", display_name="src", config={},
            )
            db.add(source)
            await db.flush()
            table = ModelTable(
                model_id=model.id, source_id=source.id, table_type="fact",
                physical_name="fact_sales", alias="fact_sales",
                display_name="Sales",
            )
            db.add(table)
            await db.flush()

            columns = {}
            for col_name in ("region", "channel", "orphan_free", "amount", "cost"):
                col = ModelColumn(
                    model_table_id=table.id, column_name=col_name,
                    data_type="text" if col_name not in ("amount", "cost") else "numeric",
                )
                db.add(col)
                columns[col_name] = col
            await db.flush()

            dims = {
                # (1) direct attachment AND a competing column attachment.
                "dim_direct": Dimension(
                    model_id=model.id, name="dim_direct",
                    description="raw direct description",
                    source_column_id=columns["region"].id,
                ),
                # (2) column attachment only — the Bug-9392 orphan case.
                "dim_orphan": Dimension(
                    model_id=model.id, name="dim_orphan",
                    description="raw orphan description",
                    source_column_id=columns["channel"].id,
                ),
                # (3) no attachment anywhere.
                "dim_none": Dimension(
                    model_id=model.id, name="dim_none",
                    description="raw none description",
                    source_column_id=columns["orphan_free"].id,
                ),
            }
            measures = {
                "measure_direct": Measure(
                    model_id=model.id, name="measure_direct",
                    description="raw measure direct description",
                    source_column_id=columns["amount"].id,
                ),
                "measure_orphan": Measure(
                    model_id=model.id, name="measure_orphan",
                    description="raw measure orphan description",
                    source_column_id=columns["cost"].id,
                ),
            }
            db.add_all([*dims.values(), *measures.values()])
            await db.flush()

            def _entry(term: str, definition: str, target_type: str, target_id):
                entry = GlossaryEntry(
                    model_id=model.id, term=term, definition=definition,
                    source="user", status="approved", version=1,
                    visibility="show",
                )
                db.add(entry)
                return entry, target_type, target_id

            pending = [
                _entry(
                    "Direct term", "DIRECT dimension definition",
                    "dimension", dims["dim_direct"].id,
                ),
                # Competing column term on the SAME dimension: direct must win.
                _entry(
                    "Region column term", "COLUMN definition for region",
                    "column", columns["region"].id,
                ),
                _entry(
                    "Channel column term", "COLUMN definition for channel",
                    "column", columns["channel"].id,
                ),
                _entry(
                    "Measure direct term", "DIRECT measure definition",
                    "measure", measures["measure_direct"].id,
                ),
                _entry(
                    "Cost column term", "COLUMN definition for cost",
                    "column", columns["cost"].id,
                ),
            ]
            await db.flush()
            for entry, target_type, target_id in pending:
                db.add(
                    GlossaryAttachment(
                        entry_id=entry.id, target_type=target_type,
                        target_id=target_id,
                    )
                )
            await db.flush()

            # Known answers, so a bug that breaks BOTH producers identically
            # still fails rather than passing a self-consistent parity check.
            expected_dims = {
                "dim_direct": "DIRECT dimension definition",
                "dim_orphan": "COLUMN definition for channel",
                "dim_none": "raw none description",
            }
            expected_measures = {
                "measure_direct": "DIRECT measure definition",
                "measure_orphan": "COLUMN definition for cost",
            }

            snap_dims, snap_measures = await _snapshot_effective_descriptions(
                db, model.id
            )
            assert snap_dims == expected_dims
            assert snap_measures == expected_measures

            # --- single-row route path (get_dimension / get_measure) ---------
            # Asserted here, before the batch path is even built, so the
            # pre-fix failure is the BEHAVIOUR (raw description served where the
            # catalogue serves the column's term) and not a missing keyword.
            single_dims = {}
            for name, dim in dims.items():
                resp = await build_dimension_response(db, dim)
                single_dims[name] = resp.effective_description
            single_measures = {}
            for name, measure in measures.items():
                resp = await build_measure_response(db, measure)
                single_measures[name] = resp.effective_description
            assert single_dims == expected_dims
            assert single_measures == expected_measures

            # --- batched list route path (list_dimensions / list_measures) ---
            dim_list = list(dims.values())
            dim_texts = await dimension_glossary_texts(
                db, model.id, "dimension", [d.id for d in dim_list],
                fallback_column_ids={d.id: d.source_column_id for d in dim_list},
            )
            batched_dims = {}
            for dim in dim_list:
                resp = await build_dimension_response(
                    db, dim, glossary_texts=dim_texts
                )
                batched_dims[dim.name] = resp.effective_description

            measure_list = list(measures.values())
            measure_texts = await measure_glossary_texts(
                db, model.id, "measure", [m.id for m in measure_list],
                fallback_column_ids={m.id: m.source_column_id for m in measure_list},
            )
            batched_measures = {}
            for measure in measure_list:
                resp = await build_measure_response(
                    db, measure, glossary_texts=measure_texts
                )
                batched_measures[measure.name] = resp.effective_description

        # Parity on the batched path too — the N+1-elimination prefetch is a
        # second implementation of the same precedence.
        assert batched_dims == expected_dims
        assert batched_measures == expected_measures
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
        await admin_engine.dispose()
