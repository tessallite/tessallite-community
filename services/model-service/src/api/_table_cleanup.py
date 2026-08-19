"""Shared Bug-6225 table-delete cleanup.

Deleting a ModelTable (or a DataSource, which DB-cascades all its tables) must
NOT rely on the FK cascade alone. The bulk row removal takes measures,
dimensions and hierarchy levels with it but never touches the persona
allow-lists / ``default_filters`` / soft-referencing rows that point at them —
leaving dangling UUIDs that silently shrink filtered persona allow-lists (a
governance / data-exposure hazard, cf. Bug-5607 / F-015-17) and stale
translation / preference rows that linger forever.

``cleanup_table_dependents`` performs exactly the strip + purge + hierarchy
orphan-sweep the single-entity delete endpoints perform, for one table, on the
caller's session, before the rows vanish. Both ``tables.py::delete_table`` and
``sources.py::delete_source`` (Bug-7794) route through it so the cleanup can
never be bypassed from either surface. The caller owns the final
``db.delete``/``revalidate_model``/``commit``.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, func, or_, select

from shared.db.models import (
    Dimension,
    DrillThroughSet,
    HierarchyDefinition,
    HierarchyLevel,
    HierarchyLevelAttribute,
    Measure,
    ModelColumn,
    ModelTable,
    Persona,
    RowSecurityRule,
    UserDefinedAttribute,
)


def is_rls_mapping_integrity_error(exc: Exception) -> bool:
    """True when an IntegrityError is the RowSecurityRule.mapping_table_id
    RESTRICT FK tripping (a table/source delete raced a concurrent RLS-rule
    insert past the app-level guard). Matched on the constraint/table name so an
    unrelated FK violation still surfaces normally."""
    text = str(getattr(exc, "orig", exc))
    return "mapping_table_id" in text or "row_security_rules" in text


async def assert_table_not_rls_mapping(db, *, model_id: UUID, table_id: UUID) -> None:
    """Raise 409 if the table is referenced as a row-security mapping table.

    ``RowSecurityRule.mapping_table_id`` is ondelete=RESTRICT, so deleting a
    table (or a source that contains it) which an RLS rule maps against would
    otherwise raise a raw IntegrityError -> HTTP 500 at flush time. Fail with a
    clear 409 naming the blocking rules instead, so the modeller retires or
    re-points the rule first. Checked for every table before its cleanup runs,
    covering both the single-table and whole-source delete paths.

    Locks the target ModelTable row FOR UPDATE first so a concurrent
    RowSecurityRule insert (which takes a FOR KEY SHARE on the referenced table
    row via its FK) blocks until this delete resolves — closing the TOCTOU
    window where a new mapping rule could commit between this check and the
    flush and then trip the RESTRICT FK as a raw 500. The commit-time
    ``is_rls_mapping_integrity_error`` safety net in the callers catches any
    residual race.
    """
    await db.execute(
        select(ModelTable.id)
        .where(ModelTable.id == table_id, ModelTable.model_id == model_id)
        .with_for_update()
    )
    rule_names = (
        await db.execute(
            select(RowSecurityRule.name).where(
                RowSecurityRule.model_id == model_id,
                RowSecurityRule.mapping_table_id == table_id,
            )
        )
    ).scalars().all()
    if rule_names:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot delete this table; it is the mapping table for row "
                f"security rules: {', '.join(rule_names)}. Retire or re-point "
                "those rules first."
            ),
        )


async def cleanup_table_dependents(db, *, model_id: UUID, table_id: UUID) -> None:
    """Strip + purge every measure/dimension/hierarchy a table delete removes.

    Runs on the caller's session and does NOT commit. Safe to call once per
    table when deleting a whole source (Bug-7794): each table's own
    columns/UDAs are enumerated independently, so a shared dimension/hierarchy
    is re-checked for remaining levels after every table's levels are dropped.

    Raises 409 up front if the table is an RLS mapping table (RESTRICT FK) so
    the caller returns a clean dependency conflict instead of a flush-time 500.
    """
    from src.api.personas import strip_id_from_personas
    from src.api._scope import purge_entity_soft_references

    # Bug-7795-family: block before any destructive work so the transaction is
    # untouched when we reject.
    await assert_table_not_rls_mapping(db, model_id=model_id, table_id=table_id)

    col_ids = (
        await db.execute(
            select(ModelColumn.id).where(ModelColumn.model_table_id == table_id)
        )
    ).scalars().all()

    uda_ids = (
        await db.execute(
            select(UserDefinedAttribute.id).where(
                UserDefinedAttribute.table_id == table_id
            )
        )
    ).scalars().all()

    # Enumerate every measure/dimension this deletion removes and strip + purge
    # each BEFORE the rows vanish (Bug-6225 [SECURITY]).
    measure_filters = []
    dim_filters = []
    if col_ids:
        measure_filters.append(Measure.source_column_id.in_(col_ids))
        dim_filters.append(Dimension.source_column_id.in_(col_ids))
    if uda_ids:
        measure_filters.append(Measure.user_defined_attribute_id.in_(uda_ids))
        dim_filters.append(Dimension.user_defined_attribute_id.in_(uda_ids))

    doomed_measure_ids: list[UUID] = []
    if measure_filters:
        base_measure_ids = (
            await db.execute(select(Measure.id).where(or_(*measure_filters)))
        ).scalars().all()
        variant_ids: list[UUID] = []
        if base_measure_ids:
            # Variant rows are removed by the variant_of_measure_id CASCADE but
            # carry their own persona references (F-015-17 parity).
            variant_ids = (
                await db.execute(
                    select(Measure.id).where(
                        Measure.variant_of_measure_id.in_(base_measure_ids)
                    )
                )
            ).scalars().all()
        doomed_measure_ids = list({*base_measure_ids, *variant_ids})

    doomed_dimension_ids: list[UUID] = []
    doomed_dimension_names: list[str] = []
    if dim_filters:
        dim_rows = (
            await db.execute(
                select(Dimension.id, Dimension.name).where(or_(*dim_filters))
            )
        ).all()
        doomed_dimension_ids = [r[0] for r in dim_rows]
        doomed_dimension_names = [r[1] for r in dim_rows if r[1]]

    for mid in doomed_measure_ids:
        await strip_id_from_personas(
            db, model_id=model_id, object_id=mid, object_class="measure"
        )
        await purge_entity_soft_references(db, model_id=model_id, entity_id=mid)
    for did in doomed_dimension_ids:
        await strip_id_from_personas(
            db, model_id=model_id, object_id=did, object_class="dimension"
        )
        await purge_entity_soft_references(db, model_id=model_id, entity_id=did)

    # default_filters is keyed by dimension NAME (Bug-5607), so drop the deleted
    # dimensions' names from every persona on the model.
    if doomed_dimension_names:
        names_to_drop = set(doomed_dimension_names)
        persona_rows = (
            await db.execute(select(Persona).where(Persona.model_id == model_id))
        ).scalars().all()
        for persona in persona_rows:
            df = persona.default_filters
            if isinstance(df, dict) and any(n in df for n in names_to_drop):
                persona.default_filters = {
                    k: v for k, v in df.items() if k not in names_to_drop
                }

    # Hierarchy levels are keyed on this table's attributes from BOTH sources —
    # physical columns (col_ids) and user-defined attributes (uda_ids).
    # key_attribute_id is a polymorphic UUID with no FK, so a bare table/source
    # delete leaves the levels dangling once the columns/UDAs are cascade-removed
    # (Bug-6225). Delete the levels from both sources and collect their
    # hierarchies for a single orphan sweep.
    orphan_candidate_hierarchy_ids: set[UUID] = set()

    if col_ids:
        await db.execute(
            delete(Dimension).where(Dimension.source_column_id.in_(col_ids))
        )
        await db.execute(
            delete(Measure).where(Measure.source_column_id.in_(col_ids))
        )
        phys_level_hids = (
            await db.execute(
                select(HierarchyLevel.hierarchy_id).where(
                    HierarchyLevel.key_attribute_id.in_(col_ids),
                    HierarchyLevel.key_attribute_source == "physical_column",
                )
            )
        ).scalars().all()
        orphan_candidate_hierarchy_ids.update(phys_level_hids)
        await db.execute(
            delete(HierarchyLevel).where(
                HierarchyLevel.key_attribute_id.in_(col_ids),
                HierarchyLevel.key_attribute_source == "physical_column",
            )
        )

    if uda_ids:
        await db.execute(
            delete(Dimension).where(
                Dimension.user_defined_attribute_id.in_(uda_ids)
            )
        )
        await db.execute(
            delete(Measure).where(
                Measure.user_defined_attribute_id.in_(uda_ids)
            )
        )
        uda_level_hids = (
            await db.execute(
                select(HierarchyLevel.hierarchy_id).where(
                    HierarchyLevel.key_attribute_id.in_(uda_ids),
                    HierarchyLevel.key_attribute_source == "user_defined_attribute",
                )
            )
        ).scalars().all()
        orphan_candidate_hierarchy_ids.update(uda_level_hids)
        await db.execute(
            delete(HierarchyLevel).where(
                HierarchyLevel.key_attribute_id.in_(uda_ids),
                HierarchyLevel.key_attribute_source == "user_defined_attribute",
            )
        )

    for hid in orphan_candidate_hierarchy_ids:
        remaining = (
            await db.execute(
                select(func.count()).where(HierarchyLevel.hierarchy_id == hid)
            )
        ).scalar()
        if remaining == 0:
            # A now-empty hierarchy must be stripped from persona
            # included_hierarchy_ids allow-lists and its soft references purged,
            # mirroring the single-entity delete cleanup.
            await strip_id_from_personas(
                db, model_id=model_id, object_id=hid, object_class="hierarchy",
            )
            await purge_entity_soft_references(db, model_id=model_id, entity_id=hid)
            await db.execute(
                delete(HierarchyDefinition).where(HierarchyDefinition.id == hid)
            )

    # Bug-7800: sweep HierarchyLevelAttribute rows whose attribute_id
    # references a deleted physical column or UDA on a SURVIVING hierarchy
    # level. HierarchyLevelAttribute.level_id CASCADE covers attributes on
    # DELETED levels, but a level keyed on a surviving column that carries a
    # display/filter attribute pointing at a column of THIS table is left
    # dangling.
    if col_ids:
        await db.execute(
            delete(HierarchyLevelAttribute).where(
                HierarchyLevelAttribute.attribute_source == "physical_column",
                HierarchyLevelAttribute.attribute_id.in_(col_ids),
            )
        )
    if uda_ids:
        await db.execute(
            delete(HierarchyLevelAttribute).where(
                HierarchyLevelAttribute.attribute_source == "user_defined_attribute",
                HierarchyLevelAttribute.attribute_id.in_(uda_ids),
            )
        )

    # Bug-7800: scrub DrillThroughSet JSON curation for deleted columns and
    # dimensions. detail_columns and source_join_path are UUID lists;
    # joined_dimension_ids references dimension IDs deleted above.
    all_deleted_attr_ids = list({*col_ids, *uda_ids})
    deleted_id_strs = {str(uid) for uid in all_deleted_attr_ids}
    doomed_dim_strs = {str(did) for did in doomed_dimension_ids}
    scrub_ids = deleted_id_strs | doomed_dim_strs
    if scrub_ids:
        drill_rows = (
            await db.execute(
                select(DrillThroughSet).where(
                    DrillThroughSet.measure_id.in_(
                        select(Measure.id).where(Measure.model_id == model_id)
                    )
                )
            )
        ).scalars().all()
        for dts in drill_rows:
            if isinstance(dts.detail_columns, list):
                cleaned = [c for c in dts.detail_columns if str(c) not in scrub_ids]
                if len(cleaned) != len(dts.detail_columns):
                    dts.detail_columns = cleaned
            if isinstance(dts.joined_dimension_ids, list):
                cleaned = [d for d in dts.joined_dimension_ids if str(d) not in scrub_ids]
                if len(cleaned) != len(dts.joined_dimension_ids):
                    dts.joined_dimension_ids = cleaned
            if isinstance(dts.source_join_path, list):
                cleaned = [s for s in dts.source_join_path if str(s) not in scrub_ids]
                if len(cleaned) != len(dts.source_join_path):
                    dts.source_join_path = cleaned
