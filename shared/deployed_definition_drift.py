"""Deployed-snapshot drift check for artifact builds (Bug-7901 / Bug-8250 / Bug-8443).

The single place any artifact writer asks: *are the LIVE definitions I am about
to materialise from identical to the DEPLOYED definitions the query-router binds
queries to?*

Before this module the answer was computed by three module-private copies of a
measure-only comparison — ``scheduler/jobs/full_refresh.py``,
``optimizer/lifecycle/creator.py``, and nothing at all on the incremental refresh
path, which is how the incremental writer shipped with no guard while its own
comment asserted one had run. The triplication is the root cause recorded in
Bug-8443; this module replaces all three.

What it proves
--------------
:func:`check_deployed_definition_drift` returns a :class:`DriftCheck` whose
``reasons`` are empty only when every build input — measures and their
calculated closure, the grain dimensions, the whole join graph, every model
table, the calendar tables the FROM clause substitutes, the pinned attribute
relationships the passenger planner emits, the data sources the CTAS reads FROM,
and the columns/UDAs/hierarchies the snapshot names — matches the deployed
snapshot. See :mod:`shared.definition_closure` for the comparison tiers
and the excluded-field rationale.

Fail-closed rules
-----------------
* ``deployed_version_id is None`` — genuinely undeployed model. Nothing is
  served from it, so there is nothing to drift against: ALLOW.
* deployed version row missing, ``snapshot_json`` missing, not a dict, or
  carrying no measures — REFUSE. An unreadable snapshot means the drift state
  is unknown, and unknown must never be treated as clean.
* Any loader exception — the caller wraps and REFUSES.

Freshness
---------
Every read is ``populate_existing=True``. The scheduler sweep reuses one
``expire_on_commit=False`` session across many aggregates, so a plain ``select``
returns instances loaded before a deploy landed and the guard would compare
current definitions against a stale idea of "live" — the fail-OPEN direction
(Bug-8479). The refresh never mutates these rows, so overwriting the cached
instances with committed state is safe.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.definition_closure import (
    ClosureSpec,
    DefinitionClosure,
    closure_digest,
    closure_from_snapshot,
    compare_closures,
    strip_non_defining,
)
from shared.model_snapshot.serialiser import row_to_snapshot_dict

#: Force every definition read to committed database state (see module docstring).
_FRESH = {"populate_existing": True}


@dataclass(frozen=True)
class DriftCheck:
    """Outcome of one live-vs-deployed comparison.

    ``live_digest`` identifies the exact live closure that was compared. A
    caller that captures it at build start and re-computes it at stamp time
    detects a definition edit that landed mid-build — the window a single
    up-front check structurally cannot see.
    """

    reasons: tuple[str, ...]
    live_digest: str | None
    deployed_version_id: Any | None
    #: False when the model is undeployed, so there was nothing to compare.
    checked: bool

    @property
    def has_drift(self) -> bool:
        return bool(self.reasons)

    def message(self, limit: int = 5) -> str:
        return "; ".join(self.reasons[:limit])


def _expand_measure_closure(
    measures_by_name: Mapping[str, Any],
    seed_names: Iterable[str],
) -> set[str]:
    """Seed names plus every transitively referenced calculated measure.

    Depth is followed to a fixed point (calc A -> calc B -> base C). An
    unparseable expression is skipped: the build itself fails on it, and
    swallowing the parse error here must not silently shrink the closure to
    something the comparison then declares clean — the names already collected
    are still compared.
    """
    from shared.semantic.calculated_expression import parse_expression

    closure: set[str] = {n for n in seed_names if n}
    pending = list(closure)
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        measure = measures_by_name.get(name)
        expression = getattr(measure, "expression", None) if measure else None
        if not expression:
            continue
        try:
            parsed = parse_expression(expression)
        except Exception:
            continue
        for ref in parsed.references:
            if ref.name not in closure:
                closure.add(ref.name)
                pending.append(ref.name)
    return closure


async def _load_live_rows(db: AsyncSession, model_id: Any) -> dict[str, list[Any]]:
    """Load every live ORM row the comparison needs, in one pass."""
    from shared.db.models import (
        CalendarTable,
        DataSource,
        Dimension,
        DimensionAttributeRelationship,
        HierarchyDefinition,
        HierarchyLevel,
        HierarchyLevelAttribute,
        Join,
        Measure,
        ModelColumn,
        ModelTable,
        UserDefinedAttribute,
        UserDefinedAttributeColumnRef,
    )

    async def all_of(stmt) -> list[Any]:
        return list((await db.execute(stmt.execution_options(**_FRESH))).scalars().all())

    measures = await all_of(select(Measure).where(Measure.model_id == model_id))
    dimensions = await all_of(select(Dimension).where(Dimension.model_id == model_id))
    tables = await all_of(select(ModelTable).where(ModelTable.model_id == model_id))
    joins = await all_of(select(Join).where(Join.model_id == model_id))

    table_ids = [t.id for t in tables]
    columns = (
        await all_of(select(ModelColumn).where(ModelColumn.model_table_id.in_(table_ids)))
        if table_ids
        else []
    )

    udas = await all_of(
        select(UserDefinedAttribute).where(UserDefinedAttribute.model_id == model_id)
    )
    uda_ids = [u.id for u in udas]
    uda_refs = (
        await all_of(
            select(UserDefinedAttributeColumnRef).where(
                UserDefinedAttributeColumnRef.attribute_id.in_(uda_ids)
            )
        )
        if uda_ids
        else []
    )

    hierarchies = await all_of(
        select(HierarchyDefinition).where(HierarchyDefinition.model_id == model_id)
    )
    hier_ids = [h.id for h in hierarchies]
    levels = (
        await all_of(select(HierarchyLevel).where(HierarchyLevel.hierarchy_id.in_(hier_ids)))
        if hier_ids
        else []
    )
    level_ids = [lv.id for lv in levels]
    level_attrs = (
        await all_of(
            select(HierarchyLevelAttribute).where(
                HierarchyLevelAttribute.level_id.in_(level_ids)
            )
        )
        if level_ids
        else []
    )

    # Calendar tables the model's tables point at. NOT decorative: both refresh
    # writers call ``ensure_target_calendar_table(cal.calendar_type,
    # cal.fiscal_year_start_month)`` and substitute the returned name into the
    # CTAS FROM clause, and the fiscal variants hold DIFFERENT values under the
    # same column names (``tess_cal_fiscal_1`` vs ``tess_cal_fiscal_4`` disagree
    # on ``year_no`` for the same date). A registered, non-autocreated calendar
    # can be re-pointed live with no deploy, which silently rebases every fiscal
    # total the aggregate serves. Found by execution in the round-1 review.
    calendar_ids = [
        t.calendar_table_id
        for t in tables
        if getattr(t, "calendar_table_id", None) is not None
    ]
    calendars = (
        await all_of(select(CalendarTable).where(CalendarTable.id.in_(calendar_ids)))
        if calendar_ids
        else []
    )

    # Pinned attribute-relationship declarations: the passenger planner turns
    # these into SELECT fragments the CTAS materialises, so an edit changes the
    # artifact's columns while the router still binds the pinned declaration.
    attribute_relationships = await all_of(
        select(DimensionAttributeRelationship).where(
            DimensionAttributeRelationship.model_id == model_id
        )
    )

    # The SOURCE side of the build. ``resolve_source_connection`` reads these to
    # decide which database the CTAS runs against; a live re-point sends the same
    # SQL at different data. (The TARGET side is artifact_target_binding's job.)
    data_sources = await all_of(
        select(DataSource).where(DataSource.model_id == model_id)
    )

    return {
        "measures": measures,
        "dimensions": dimensions,
        "tables": tables,
        "joins": joins,
        "columns": columns,
        "user_defined_attributes": udas,
        "uda_column_refs": uda_refs,
        "hierarchies": hierarchies,
        "hierarchy_levels": levels,
        "hierarchy_level_attributes": level_attrs,
        "calendar_tables": calendars,
        "attribute_relationships": attribute_relationships,
        "data_sources": data_sources,
    }


def _live_hierarchies_nested(rows: Mapping[str, list[Any]]) -> list[dict[str, Any]]:
    """Rebuild the snapshot's nested hierarchy shape from flat live rows."""
    attrs_by_level: dict[str, list[dict[str, Any]]] = {}
    for a in rows["hierarchy_level_attributes"]:
        attrs_by_level.setdefault(str(a.level_id), []).append(
            strip_non_defining(row_to_snapshot_dict(a))
        )
    levels_by_hier: dict[str, list[dict[str, Any]]] = {}
    for lv in rows["hierarchy_levels"]:
        lv_dict = strip_non_defining(row_to_snapshot_dict(lv))
        lv_dict["attributes"] = sorted(
            attrs_by_level.get(str(lv.id), []), key=lambda a: str(a.get("id") or "")
        )
        levels_by_hier.setdefault(str(lv.hierarchy_id), []).append(lv_dict)
    out: list[dict[str, Any]] = []
    for h in rows["hierarchies"]:
        h_dict = strip_non_defining(row_to_snapshot_dict(h))
        h_dict["levels"] = sorted(
            levels_by_hier.get(str(h.id), []), key=lambda l: str(l.get("id") or "")
        )
        out.append(h_dict)
    return out


def _live_closure(rows: Mapping[str, list[Any]], spec: ClosureSpec) -> DefinitionClosure:
    def proj(items: Iterable[Any]) -> list[dict[str, Any]]:
        return [strip_non_defining(row_to_snapshot_dict(i)) for i in items]

    return DefinitionClosure(
        measures=proj(
            m for m in rows["measures"] if m.name in spec.closure_measure_names
        ),
        dimensions=proj(d for d in rows["dimensions"] if d.name in spec.grain_names),
        tables=proj(rows["tables"]),
        joins=proj(rows["joins"]),
        columns=proj(rows["columns"]),
        user_defined_attributes=proj(rows["user_defined_attributes"]),
        uda_column_refs=proj(rows["uda_column_refs"]),
        hierarchies=_live_hierarchies_nested(rows),
        calendar_tables=proj(rows["calendar_tables"]),
        attribute_relationships=proj(rows["attribute_relationships"]),
        data_sources=proj(rows["data_sources"]),
    )


def _grain_resolvable_names_live(rows: Mapping[str, list[Any]]) -> set[str]:
    names = {d.name for d in rows["dimensions"] if d.name}
    hier_name_by_id = {str(h.id): h.name for h in rows["hierarchies"]}
    for lv in rows["hierarchy_levels"]:
        if not lv.name:
            continue
        names.add(lv.name)
        hier_name = hier_name_by_id.get(str(lv.hierarchy_id))
        if hier_name:
            names.add(f"{hier_name}.{lv.name}")
    return names


def _grain_resolvable_names_snapshot(snapshot: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for d in snapshot.get("dimensions") or []:
        if isinstance(d, Mapping) and d.get("name"):
            names.add(d["name"])
    for h in snapshot.get("hierarchies") or []:
        if not isinstance(h, Mapping):
            continue
        hier_name = h.get("name")
        for lv in h.get("levels") or []:
            if not isinstance(lv, Mapping) or not lv.get("name"):
                continue
            names.add(lv["name"])
            if hier_name:
                names.add(f"{hier_name}.{lv['name']}")
    return names


async def check_deployed_definition_drift(
    db: AsyncSession,
    *,
    model_id: Any,
    deployed_version_id: Any,
    measure_names: Iterable[str],
    grain_names: Iterable[str],
) -> DriftCheck:
    """Compare the live build inputs against the deployed snapshot.

    ``deployed_version_id`` must be the pointer the artifact will be STAMPED
    with — normally the value frozen by ``capture_build_binding``. Validating
    against the same pointer that gets stamped is what makes the guard and the
    stamp agree by construction; re-reading ``Model`` here could compare against
    a different version than the one recorded (Bug-8479).
    """
    from shared.db.models import ModelVersion

    if deployed_version_id is None:
        return DriftCheck(
            reasons=(), live_digest=None, deployed_version_id=None, checked=False
        )

    version = await db.get(ModelVersion, deployed_version_id)
    if version is None:
        return DriftCheck(
            reasons=("deployed version row missing (fail-closed)",),
            live_digest=None,
            deployed_version_id=deployed_version_id,
            checked=True,
        )
    snapshot = getattr(version, "snapshot_json", None)
    if not snapshot or not isinstance(snapshot, Mapping):
        return DriftCheck(
            reasons=("deployed snapshot missing or not an object (fail-closed)",),
            live_digest=None,
            deployed_version_id=deployed_version_id,
            checked=True,
        )
    if not isinstance(snapshot.get("measures"), list) or not snapshot.get("measures"):
        return DriftCheck(
            reasons=("deployed snapshot has no measures (fail-closed)",),
            live_digest=None,
            deployed_version_id=deployed_version_id,
            checked=True,
        )

    rows = await _load_live_rows(db, model_id)
    measures_by_name = {m.name: m for m in rows["measures"] if m.name}
    spec = ClosureSpec.of(
        _expand_measure_closure(measures_by_name, measure_names),
        grain_names,
    )

    live = _live_closure(rows, spec)
    deployed = closure_from_snapshot(snapshot, spec)
    reasons = compare_closures(live, deployed)

    # Grain resolution is a property of the WHOLE name space, not of the rows in
    # the closure: a grain name stops resolving when its dimension is deleted, or
    # starts resolving to a hierarchy level that only exists on one side. Compare
    # membership per grain name rather than asserting absolute resolvability, so
    # the check states exactly what changed without re-implementing the resolver.
    live_names = _grain_resolvable_names_live(rows)
    snap_names = _grain_resolvable_names_snapshot(snapshot)
    for name in sorted(spec.grain_names):
        in_live = name in live_names
        in_snap = name in snap_names
        if in_live != in_snap:
            reasons.append(
                f"grain {name!r}: resolvable in the deployed snapshot={in_snap} "
                f"but live={in_live}"
            )

    return DriftCheck(
        reasons=tuple(reasons),
        live_digest=closure_digest(live),
        deployed_version_id=deployed_version_id,
        checked=True,
    )
