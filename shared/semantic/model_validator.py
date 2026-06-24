"""Structural validation for dimensions, measures, and aggregates.

When a join, table, or column referenced by a semantic object is
deleted, that object becomes structurally invalid: the rewriter can
no longer qualify its source column, and queries (or aggregate CTAS
generation) against it will fail at the database with a cryptic
error. Rather than waiting for that runtime failure, this module
marks affected rows with a boolean ``is_invalid`` flag + a
human-readable ``invalid_reason`` so the gateway, scheduler, and
Model Health tab can behave correctly and surface the problem to
the modeler.

Three object types are validated:

- **Dimension**: invalid when its source column was deleted, its
  source table is no longer reachable from the fact anchor, or its
  UDA reference is dangling.
- **Measure**: same rules.
- **AggregateDefinition**: invalid when any grain dimension's source
  table is unreachable, any referenced measure is gone, or the
  model has no tables at all.

All three run against a single pre-loaded ``_ModelStructure`` so a
full revalidation pass over a model issues only a handful of
queries regardless of how many objects need checking.

Recovery is automatic: the scheduler's full-refresh path re-runs
the aggregate validator before attempting a CTAS and flips
``status`` back to ``active`` if the model has since been fixed.
Dimensions and measures revalidate on every structural edit
(delete/patch of joins, tables, columns, dims, measures).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    Dimension,
    Join,
    Measure,
    ModelColumn,
    ModelTable,
)


@dataclass
class _ModelStructure:
    dim_by_name: dict
    tables: dict
    columns: dict
    joins: list
    measure_ids: set
    measures_by_name: dict
    anchor_id: Optional[UUID]
    reachable_from_anchor: frozenset


async def _load_model_structure(
    model_id: UUID, db: AsyncSession
) -> _ModelStructure:
    """Load every structural row the validator needs in one pass.

    Called once per revalidation call so that a model with 50 aggregates
    doesn't trigger 300 small queries.
    """
    dims_result = await db.execute(
        select(Dimension).where(Dimension.model_id == model_id)
    )
    dim_by_name = {d.name: d for d in dims_result.scalars().all()}

    tables_result = await db.execute(
        select(ModelTable).where(ModelTable.model_id == model_id)
    )
    tables = {t.id: t for t in tables_result.scalars().all()}

    if tables:
        cols_result = await db.execute(
            select(ModelColumn).where(
                ModelColumn.model_table_id.in_(list(tables.keys()))
            )
        )
        columns = {c.id: c for c in cols_result.scalars().all()}
    else:
        columns = {}

    joins_result = await db.execute(
        select(Join).where(Join.model_id == model_id)
    )
    joins = list(joins_result.scalars().all())

    measures_result = await db.execute(
        select(Measure).where(Measure.model_id == model_id)
    )
    measures_list = list(measures_result.scalars().all())
    measure_ids = {m.id for m in measures_list}
    measures_by_name = {m.name: m for m in measures_list}

    anchor_id: Optional[UUID] = None
    reachable: set[UUID] = set()
    if tables:
        facts = [t for t in tables.values() if (t.table_type or "").lower() == "fact"]
        anchor = facts[0] if facts else next(iter(tables.values()))
        anchor_id = anchor.id
        reachable = {anchor.id}
        changed = True
        while changed:
            changed = False
            for j in joins:
                if j.left_table_id in reachable and j.right_table_id not in reachable:
                    reachable.add(j.right_table_id)
                    changed = True
                elif j.right_table_id in reachable and j.left_table_id not in reachable:
                    reachable.add(j.left_table_id)
                    changed = True

    return _ModelStructure(
        dim_by_name=dim_by_name,
        tables=tables,
        columns=columns,
        joins=joins,
        measure_ids=measure_ids,
        measures_by_name=measures_by_name,
        anchor_id=anchor_id,
        reachable_from_anchor=frozenset(reachable),
    )


def detect_chasm_trap(structure: _ModelStructure) -> Optional[str]:
    """Detect a chasm trap topology: two or more fact tables joined only
    through shared dimension tables.

    Returns a warning message if detected, or None.
    """
    fact_ids = [
        tid for tid, t in structure.tables.items()
        if (t.table_type or "").lower() == "fact"
    ]
    if len(fact_ids) < 2:
        return None

    join_pairs: set[tuple[UUID, UUID]] = set()
    for j in structure.joins:
        join_pairs.add((j.left_table_id, j.right_table_id))
        join_pairs.add((j.right_table_id, j.left_table_id))

    for i, f1 in enumerate(fact_ids):
        for f2 in fact_ids[i + 1:]:
            if (f1, f2) in join_pairs:
                continue
            fact_names = [
                structure.tables[fid].physical_name
                for fid in fact_ids
                if fid in structure.tables
            ]
            logger.warning(
                "[MODEL_VALIDATOR] chasm trap detected: fact tables %s "
                "joined only through shared dimensions",
                fact_names,
            )
            return (
                f"This model contains multiple fact tables ({', '.join(fact_names)}) "
                "joined through shared dimensions. Aggregation queries that span "
                "both fact tables may produce incorrect results (fan-out / chasm "
                "trap). Consider splitting into separate models."
            )
    return None


async def validate_aggregate(
    agg: AggregateDefinition,
    db: AsyncSession,
    structure: Optional[_ModelStructure] = None,
) -> Optional[str]:
    """Return None if valid, or a short invalid_reason string.

    ``structure`` is an optional pre-loaded model snapshot. Pass it
    explicitly when validating several aggregates in a row to avoid
    reloading the same rows every iteration.
    """
    if structure is None:
        structure = await _load_model_structure(agg.model_id, db)

    if not structure.tables:
        return "Model has no tables"

    grain_table_ids: set[UUID] = set()
    for grain_name in agg.grain or []:
        dim = structure.dim_by_name.get(grain_name)
        if dim is None:
            return f"Grain dimension {grain_name!r} no longer exists"
        if dim.source_column_id is None:
            # UDA / expression-based; cannot determine required table from
            # the structural view — skip reachability check for this grain.
            continue
        col = structure.columns.get(dim.source_column_id)
        if col is None or col.model_table_id not in structure.tables:
            return (
                f"Grain dimension {grain_name!r} references a missing column"
                f" or table"
            )
        grain_table_ids.add(col.model_table_id)

    for tid in grain_table_ids:
        if tid not in structure.reachable_from_anchor:
            src = structure.tables.get(tid)
            # Bug-3816: use the human-readable table name, never a raw UUID.
            src_name = (
                (src.display_name or src.alias or src.physical_name)
                if src else "unknown table (removed)"
            )
            return f"Table {src_name} is no longer reachable from the fact table"

    agg_cols_result = await db.execute(
        select(AggregateColumn)
        .options(selectinload(AggregateColumn.measure))
        .where(AggregateColumn.aggregate_definition_id == agg.id)
    )
    for ac in agg_cols_result.scalars().all():
        if ac.measure_id is None:
            if ac.physical_col_name == "__row_count__count":
                continue
            return "A referenced measure was deleted"
        if ac.measure_id not in structure.measure_ids:
            return "Referenced measure no longer exists"

    return None


def validate_dimension(
    dim: Dimension,
    structure: _ModelStructure,
) -> Optional[str]:
    """Return None if the dimension is structurally valid, or a reason
    string naming the specific broken reference.

    Pure: takes an already-loaded structure snapshot so a full pass
    over a model doesn't hit the DB per-dimension.
    """
    if not structure.tables:
        return "Model has no tables"
    if dim.source_column_id is None and dim.user_defined_attribute_id is None:
        # Nothing to check; the dimension is a pure semantic label with
        # no physical binding and will never be queryable anyway. Leave
        # it valid so the modeler can finish wiring it.
        return None
    if dim.source_column_id is not None:
        col = structure.columns.get(dim.source_column_id)
        if col is None:
            return "Source column no longer exists"
        if col.model_table_id not in structure.tables:
            return "Source column's table has been removed from the model"
        if col.model_table_id not in structure.reachable_from_anchor:
            src = structure.tables.get(col.model_table_id)
            # Bug-3816: use the human-readable table name, never a raw UUID.
            src_name = (
                (src.display_name or src.alias or src.physical_name)
                if src else "unknown table (removed)"
            )
            return f"Source table {src_name} is no longer reachable from the fact table"
    return None


def validate_measure(
    measure: Measure,
    structure: _ModelStructure,
) -> Optional[str]:
    """Same contract as ``validate_dimension`` but for measures.

    Calculated measures are checked for reference integrity: every
    ``measure("name")`` token must resolve to an existing standard or
    variant measure in the same model. Referencing a missing or another
    calculated measure returns a reason string so the row is marked
    soft-invalid (matches Q15=C: strict-on-create, soft-on-dependency-
    breakage).
    """
    if not structure.tables:
        return "Model has no tables"
    if getattr(measure, "measure_type", "standard") == "calculated":
        from shared.semantic.calculated_expression import (
            ExpressionValidationError,
            parse_expression,
        )

        if not measure.expression:
            return "Calculated measure has no expression"
        try:
            parsed = parse_expression(measure.expression)
        except ExpressionValidationError as exc:
            return f"Invalid expression: {exc}"
        missing: list[str] = []
        non_simple: list[str] = []
        for name in parsed.referenced_names:
            ref = structure.measures_by_name.get(name)
            if ref is None:
                missing.append(name)
                continue
            if ref.measure_type == "calculated":
                non_simple.append(name)
        if missing:
            return f"Referenced measure(s) no longer exist: {', '.join(sorted(set(missing)))}"
        if non_simple:
            return (
                "Calculated measures cannot reference other calculated "
                f"measures: {', '.join(sorted(set(non_simple)))}"
            )
        return None
    if measure.source_column_id is None and measure.user_defined_attribute_id is None:
        if not measure.expression:
            return None
        return None
    if measure.source_column_id is not None:
        col = structure.columns.get(measure.source_column_id)
        if col is None:
            return "Source column no longer exists"
        if col.model_table_id not in structure.tables:
            return "Source column's table has been removed from the model"
        if col.model_table_id not in structure.reachable_from_anchor:
            src = structure.tables.get(col.model_table_id)
            # Bug-3816: use the human-readable table name, never a raw UUID.
            src_name = (
                (src.display_name or src.alias or src.physical_name)
                if src else "unknown table (removed)"
            )
            return f"Source table {src_name} is no longer reachable from the fact table"
    return None


async def revalidate_aggregates(
    model_id: UUID,
    db: AsyncSession,
) -> tuple[list[tuple[UUID, str]], list[UUID]]:
    """Re-run aggregate validation for every aggregate in a model.

    Prefer ``revalidate_model`` for new call-sites — it runs the
    dim/measure passes as well against the same pre-loaded structure.
    Kept as a public helper because older code paths still import
    it; the contract is the new transition-aware ``(currently_invalid,
    newly_valid)`` tuple rather than the old flat list.
    """
    structure = await _load_model_structure(model_id, db)
    return await _revalidate_aggregates_with_structure(model_id, db, structure)


async def _revalidate_aggregates_with_structure(
    model_id: UUID,
    db: AsyncSession,
    structure: _ModelStructure,
) -> tuple[list[tuple[UUID, str]], list[UUID]]:
    """Walk every aggregate, mutate in place, and return two lists:

    - ``currently_invalid``: ``(id, reason)`` for every aggregate whose
      current state is invalid (regardless of whether it was already
      invalid before this pass). Consumers call ``record_alert`` for
      each entry; the partial-unique dedup on the alerts table means
      repeat calls just bump ``occurrence_count``.
    - ``newly_valid``: ids of aggregates that **transitioned** from
      invalid to valid in this pass. Consumers call ``resolve_alert``
      for each entry, closing any open alert.
    """
    aggs_result = await db.execute(
        select(AggregateDefinition).where(
            AggregateDefinition.model_id == model_id
        )
    )
    currently_invalid: list[tuple[UUID, str]] = []
    newly_valid: list[UUID] = []
    for agg in aggs_result.scalars().all():
        if agg.status == "retired":
            continue
        reason = await validate_aggregate(agg, db, structure=structure)
        if reason is None:
            if agg.status == "invalid":
                agg.status = "active"
                agg.invalid_reason = None
                newly_valid.append(agg.id)
        else:
            agg.status = "invalid"
            agg.invalid_reason = reason
            currently_invalid.append((agg.id, reason))
    return currently_invalid, newly_valid


@dataclass(frozen=True)
class ModelRevalidationReport:
    """Outcome of a full model revalidation pass.

    All ``*_invalid_*`` lists hold the set of objects whose current
    state after the pass is invalid (with reason). Repeat-invalid
    objects are re-listed on every pass; the alert recorder dedups
    via a partial-unique index.

    All ``*_newly_valid`` lists hold the subset that **transitioned**
    from invalid to valid in this pass. The alert recorder calls
    ``resolve_alert`` only for these, so closing the open alert is
    an O(transitions) cost instead of O(objects).
    """

    invalid_dimensions: list[tuple[UUID, str]]
    invalid_measures: list[tuple[UUID, str]]
    invalid_aggregates: list[tuple[UUID, str]]
    newly_valid_dimensions: list[UUID]
    newly_valid_measures: list[UUID]
    newly_valid_aggregates: list[UUID]
    warnings: list[str] = field(default_factory=list)

    @property
    def has_any_invalid(self) -> bool:
        return bool(
            self.invalid_dimensions
            or self.invalid_measures
            or self.invalid_aggregates
        )


async def revalidate_model(
    model_id: UUID,
    db: AsyncSession,
) -> ModelRevalidationReport:
    """Re-run validation for every dimension, measure, and aggregate.

    Loads the model structure once, then sweeps all three object
    families against it. Flips invalid→valid rows automatically when
    the structural problem has been fixed since the last pass, and
    populates ``is_invalid`` / ``invalid_reason`` in-place on the ORM
    rows. Caller must ``db.commit()``.

    Returns a :class:`ModelRevalidationReport` so callers (including
    the Phase-2 alert recorder) can emit events for each state
    transition without re-querying.
    """
    structure = await _load_model_structure(model_id, db)

    invalid_dims: list[tuple[UUID, str]] = []
    newly_valid_dims: list[UUID] = []
    dims_result = await db.execute(
        select(Dimension).where(Dimension.model_id == model_id)
    )
    for dim in dims_result.scalars().all():
        reason = validate_dimension(dim, structure)
        if reason is None:
            if dim.is_invalid:
                dim.is_invalid = False
                dim.invalid_reason = None
                newly_valid_dims.append(dim.id)
        else:
            dim.is_invalid = True
            dim.invalid_reason = reason
            invalid_dims.append((dim.id, reason))

    invalid_measures: list[tuple[UUID, str]] = []
    newly_valid_measures: list[UUID] = []
    measures_result = await db.execute(
        select(Measure).where(Measure.model_id == model_id)
    )
    for measure in measures_result.scalars().all():
        reason = validate_measure(measure, structure)
        if reason is None:
            if measure.is_invalid:
                measure.is_invalid = False
                measure.invalid_reason = None
                newly_valid_measures.append(measure.id)
        else:
            measure.is_invalid = True
            measure.invalid_reason = reason
            invalid_measures.append((measure.id, reason))

    invalid_aggs, newly_valid_aggs = await _revalidate_aggregates_with_structure(
        model_id, db, structure
    )

    warnings: list[str] = []
    chasm = detect_chasm_trap(structure)
    if chasm:
        warnings.append(chasm)

    report = ModelRevalidationReport(
        invalid_dimensions=invalid_dims,
        invalid_measures=invalid_measures,
        invalid_aggregates=invalid_aggs,
        newly_valid_dimensions=newly_valid_dims,
        newly_valid_measures=newly_valid_measures,
        newly_valid_aggregates=newly_valid_aggs,
        warnings=warnings,
    )
    await _sync_alerts_with_report(db, model_id, report)
    return report


async def _sync_alerts_with_report(
    db: AsyncSession,
    model_id: UUID,
    report: "ModelRevalidationReport",
) -> None:
    """Bridge a revalidation report into the model_alerts stream.

    Records an open alert for each currently-invalid object (dedup
    handled by the partial-unique index) and resolves any alert whose
    condition transitioned back to valid in this pass. Pulled into a
    helper so the model_validator stays free of alert-side imports
    until this function actually runs.
    """
    from shared.semantic.model_alerts import (
        CATEGORY_INVALID_AGGREGATE,
        CATEGORY_INVALID_DIMENSION,
        CATEGORY_INVALID_MEASURE,
        OBJECT_AGGREGATE,
        OBJECT_DIMENSION,
        OBJECT_MEASURE,
        SEVERITY_WARNING,
        record_alert,
        resolve_alert,
    )

    for dim_id, reason in report.invalid_dimensions:
        await record_alert(
            db,
            model_id=model_id,
            severity=SEVERITY_WARNING,
            category=CATEGORY_INVALID_DIMENSION,
            title="Dimension is structurally invalid",
            detail=reason,
            related_object_type=OBJECT_DIMENSION,
            related_object_id=dim_id,
        )
    for dim_id in report.newly_valid_dimensions:
        await resolve_alert(
            db,
            model_id=model_id,
            category=CATEGORY_INVALID_DIMENSION,
            related_object_type=OBJECT_DIMENSION,
            related_object_id=dim_id,
        )

    for m_id, reason in report.invalid_measures:
        await record_alert(
            db,
            model_id=model_id,
            severity=SEVERITY_WARNING,
            category=CATEGORY_INVALID_MEASURE,
            title="Measure is structurally invalid",
            detail=reason,
            related_object_type=OBJECT_MEASURE,
            related_object_id=m_id,
        )
    for m_id in report.newly_valid_measures:
        await resolve_alert(
            db,
            model_id=model_id,
            category=CATEGORY_INVALID_MEASURE,
            related_object_type=OBJECT_MEASURE,
            related_object_id=m_id,
        )

    for agg_id, reason in report.invalid_aggregates:
        await record_alert(
            db,
            model_id=model_id,
            severity=SEVERITY_WARNING,
            category=CATEGORY_INVALID_AGGREGATE,
            title="Aggregate is structurally invalid",
            detail=reason,
            related_object_type=OBJECT_AGGREGATE,
            related_object_id=agg_id,
        )
    for agg_id in report.newly_valid_aggregates:
        await resolve_alert(
            db,
            model_id=model_id,
            category=CATEGORY_INVALID_AGGREGATE,
            related_object_type=OBJECT_AGGREGATE,
            related_object_id=agg_id,
        )
