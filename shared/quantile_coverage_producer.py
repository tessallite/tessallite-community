"""QuantileCoverage PRODUCER -- writes coverage rows at materialisation time.

Bug-7852 / Bug-6969. Governing invariant: pNN coverage is served ONLY when
it provably matches the current physical data and the deployed definition.
On ANY uncertainty, leave NO usable coverage so the query falls to source.
"""
from __future__ import annotations

import logging
import types as _types
import uuid
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.aggregate_quantiles import (
    is_quantile_stat_type,
    quantile_suffix_to_percentile,
)
from shared.db.models import AggregateColumn, QuantileCoverage
from shared.quantile_contracts import (
    EXACT,
    METHOD_CONTINUOUS,
    NULL_IGNORE,
    ORDER_ASC,
    UNKNOWN,
    build_input_fingerprint,
    measure_value_definition,
    measure_value_type,
)

logger = logging.getLogger(__name__)


async def _load_deployed_measure_definitions(
    model_id: object,
    db: AsyncSession,
) -> Optional[dict[str, dict]]:
    """Load the deployed snapshot's measure value-definition identities.

    Returns:
    - ``{}`` : deployed_version_id is None (no deployed version, no drift risk).
    - ``None`` : ANY error or ambiguity -> caller MUST fail CLOSED.
    - ``{name: {...}}`` : successfully loaded.
    """
    from shared.db.models import Model, ModelVersion

    model = await db.get(Model, model_id)
    if model is None:
        return None
    deployed_vid = getattr(model, "deployed_version_id", None)
    if deployed_vid is None:
        return {}

    version = await db.get(ModelVersion, deployed_vid)
    if version is None:
        return None
    snap = getattr(version, "snapshot_json", None)
    if not snap or not isinstance(snap, dict):
        return None

    measures = snap.get("measures")
    if not measures or not isinstance(measures, list):
        return None

    # FIX C: the serialiser writes snap["columns"] (not "model_columns")
    # and snap["tables"]. Use the SAME keys so the loader finds data.
    snap_columns = snap.get("columns", [])
    col_by_id: dict[str, dict] = {}
    for c in (snap_columns if isinstance(snap_columns, list) else []):
        cid = c.get("id")
        if cid:
            col_by_id[str(cid)] = c

    snap_tables = snap.get("tables", [])
    table_by_id: dict[str, dict] = {}
    for t in (snap_tables if isinstance(snap_tables, list) else []):
        tid = t.get("id")
        if tid:
            table_by_id[str(tid)] = t

    out: dict[str, dict] = {}
    for m in measures:
        name = m.get("name")
        if not name:
            return None
        scid = m.get("source_column_id")
        col_name = None
        table_phys = None
        table_source_id = None
        if scid:
            col = col_by_id.get(str(scid))
            if col:
                col_name = col.get("column_name")
                mtid = col.get("model_table_id")
                if mtid:
                    tbl = table_by_id.get(str(mtid))
                    if tbl:
                        table_phys = tbl.get("physical_name")
                        table_source_id = tbl.get("source_id")
        out[name] = {
            "source_column_id": scid,
            "expression": m.get("expression"),
            "user_defined_attribute_id": m.get("user_defined_attribute_id"),
            "column_name": col_name,
            "table_physical_name": table_phys,
            "table_source_id": table_source_id,
        }
    return out


async def write_quantile_coverage(
    *,
    aggregate_definition_id: object,
    db: AsyncSession,
    source_dialect: Optional[str] = None,
    target_dialect: Optional[str] = None,
    build_model_version: Optional[str] = None,
    refresh_run_id: Optional[object] = None,
) -> int:
    """Write one ``QuantileCoverage`` row per pNN ``AggregateColumn``.

    Atomicity: (1) delete old coverage and flush (outside savepoint),
    (2) insert new coverage inside a SAVEPOINT (begin_nested). If the
    insert fails, the savepoint rolls back inserts only -- the flushed
    delete survives, leaving ZERO coverage rows so pNN fails closed to
    source. The exception re-raises so the caller marks the refresh FAILED.
    """
    from sqlalchemy.orm import selectinload

    col_result = await db.execute(
        select(AggregateColumn)
        .options(selectinload(AggregateColumn.measure))
        .where(AggregateColumn.aggregate_definition_id == aggregate_definition_id)
    )
    columns = list(col_result.scalars().all())

    quantile_cols = [
        c for c in columns
        if is_quantile_stat_type(c.stat_type) and c.measure is not None
    ]
    if not quantile_cols:
        return 0

    # ITEM 2 (approach b): always write exactness='unknown' so pNN never
    # serves from an aggregate whose coverage could be stale relative to its
    # physical data. The physical DDL commits independently from coverage
    # metadata; an outer-transaction rollback can revert coverage while new
    # physical data stays, creating old-coverage-over-new-data (wrong pNN).
    # Until Bug-7901 guarantees physical/coverage atomicity + refresh from
    # deployed definitions, exactness is capped at 'unknown' -- the consumer's
    # proof gate rejects it and routes to source (correct-but-slow). The
    # coverage infrastructure (fingerprints, drift guard, snapshot flow) is
    # fully built and tested; upgrading to 'exact' is a Bug-7901 deliverable.
    base_exactness = UNKNOWN

    model_id = quantile_cols[0].measure.model_id if quantile_cols else None
    try:
        deployed_defs = await _load_deployed_measure_definitions(model_id, db) if model_id else {}
    except Exception as exc:
        logger.warning(
            "Bug-7852 drift guard: failed to load deployed snapshot for "
            "model %s; writing exactness='unknown' (fail closed): %s",
            model_id, exc,
        )
        deployed_defs = None

    # Resolve source column names AND table physical names from the live DB
    # for the same-ID drift check (FIX A: include table identity).
    from shared.db.models import ModelColumn, ModelTable
    source_col_ids = set()
    for c in quantile_cols:
        scid = getattr(c.measure, "source_column_id", None)
        if scid:
            source_col_ids.add(scid)

    col_info_by_id: dict[str, dict] = {}
    if source_col_ids:
        col_result2 = await db.execute(
            select(ModelColumn.id, ModelColumn.column_name, ModelColumn.model_table_id)
            .where(ModelColumn.id.in_(list(source_col_ids)))
        )
        table_ids_needed = set()
        col_rows = []
        for cid, cname, mtid in col_result2.all():
            col_rows.append((str(cid), cname, str(mtid) if mtid else None))
            if mtid:
                table_ids_needed.add(mtid)

        table_info_by_id: dict[str, dict] = {}
        if table_ids_needed:
            tbl_result = await db.execute(
                select(ModelTable.id, ModelTable.physical_name, ModelTable.source_id)
                .where(ModelTable.id.in_(list(table_ids_needed)))
            )
            for tid, tphys, tsid in tbl_result.all():
                table_info_by_id[str(tid)] = {
                    "physical_name": tphys,
                    "source_id": str(tsid) if tsid else None,
                }

        for cid_str, cname, mtid_str in col_rows:
            ti = table_info_by_id.get(mtid_str) if mtid_str else None
            col_info_by_id[cid_str] = {
                "column_name": cname,
                "table_physical_name": ti["physical_name"] if ti else None,
                "table_source_id": ti["source_id"] if ti else None,
            }

    # Step 1: delete old coverage and flush (ensures delete is durable
    # within this transaction even if the insert below fails).
    await db.execute(
        delete(QuantileCoverage)
        .where(QuantileCoverage.aggregate_definition_id == aggregate_definition_id)
    )
    await db.flush()

    # Step 2: attempt insert inside a SAVEPOINT. If insert fails, the
    # savepoint rolls back ONLY the inserts (the delete is already flushed
    # outside the savepoint), leaving ZERO coverage rows.
    try:
        async with db.begin_nested():
            written = 0
            for col in quantile_cols:
                pct = quantile_suffix_to_percentile(col.stat_type)
                if pct is None:
                    continue

                measure = col.measure
                fraction_str = str(pct / 100)

                vt = measure_value_type(measure)
                scid = getattr(measure, "source_column_id", None)
                ci = col_info_by_id.get(str(scid)) if scid else None
                live_col_name = ci["column_name"] if ci else None
                live_table_phys = ci["table_physical_name"] if ci else None
                live_table_sid = ci["table_source_id"] if ci else None
                vd = measure_value_definition(
                    measure,
                    source_column_name=live_col_name,
                    source_table_physical_name=live_table_phys,
                    source_table_source_id=live_table_sid,
                )

                fingerprint = build_input_fingerprint(
                    measure_name=measure.name,
                    value_type=vt,
                    value_definition=vd,
                )

                exactness = base_exactness
                if deployed_defs is None:
                    exactness = UNKNOWN
                elif deployed_defs:
                    dep_m = deployed_defs.get(measure.name)
                    if dep_m is not None:
                        dep_ns = _types.SimpleNamespace(**dep_m)
                        dep_vd = measure_value_definition(
                            dep_ns,
                            source_column_name=dep_m.get("column_name"),
                            source_table_physical_name=dep_m.get("table_physical_name"),
                            source_table_source_id=dep_m.get("table_source_id"),
                        )
                        if dep_vd != vd:
                            exactness = UNKNOWN
                            logger.warning(
                                "Bug-7852 drift guard: measure %r value definition "
                                "changed without deploy (live=%s, deployed=%s); "
                                "writing exactness='unknown'",
                                measure.name, vd, dep_vd,
                            )

                coverage = QuantileCoverage(
                    id=uuid.uuid4(),
                    aggregate_column_id=col.id,
                    aggregate_definition_id=aggregate_definition_id,
                    measure_id=measure.id,
                    semantic_measure_name=measure.name,
                    input_expression_fingerprint=fingerprint,
                    fraction=fraction_str,
                    method=METHOD_CONTINUOUS,
                    order_direction=ORDER_ASC,
                    null_policy=NULL_IGNORE,
                    value_type=vt,
                    collation=None,
                    timezone=None,
                    exactness=exactness,
                    algorithm="exact_sort" if exactness == EXACT else None,
                    algorithm_version=None,
                    build_source_dialect=source_dialect,
                    build_model_version=build_model_version,
                    refresh_run_id=refresh_run_id,
                    coverage_schema_version=1,
                )
                db.add(coverage)
                written += 1

            if written:
                await db.flush()

    except Exception:
        # FIX B: insert failed. The savepoint rolled back the inserts but
        # the delete (flushed before the savepoint) survives. Coverage is
        # already ZERO rows. Re-raise so the caller marks the refresh FAILED.
        logger.error(
            "Bug-7852: coverage INSERT failed for aggregate %s; "
            "coverage is ZERO rows (delete survived, inserts rolled back) "
            "so pNN fails closed to source",
            aggregate_definition_id,
        )
        raise

    if written:
        logger.info(
            "Bug-7852: wrote %d QuantileCoverage row(s) for aggregate %s "
            "(base_exactness=%s, source_dialect=%s)",
            written, aggregate_definition_id, base_exactness, source_dialect,
        )

    return written
