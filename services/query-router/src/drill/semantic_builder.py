"""Semantic drill-through builder.

Builds model-level SQL that routes through the query-router's
parse → bind → route → rewrite → execute pipeline.  Every drill
query is a GROUP BY against the model slug, so UDAs, row security,
persona gating, and aggregate routing work automatically.

Hierarchy-aware: when a grouping dimension belongs to a hierarchy
and is not at the leaf level, the drill query aggregates at the
next hierarchy level.  At leaf (or for non-hierarchy dims), the
query aggregates at the leaf grain.

5262+3971 — Snapshot alignment: when a model has a deployed version,
drill metadata (measures, dimensions, hierarchies, drill-through sets,
columns, tables) is resolved from the deployed snapshot, the same
source the execution binder uses. This prevents draft edits from
producing SQL the deployed binder cannot bind. When no deployed
snapshot exists, falls back to the live tables (undeployed models).
"""
from __future__ import annotations

import base64
import json
import types
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Dimension,
    DrillThroughSet,
    HierarchyDefinition,
    HierarchyLevel,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    ModelVersion,
    UserDefinedAttribute,
)

from src.drill.predicate import (
    DrillPredicateError,
    compile_where,
    quote_ident,
)


class DrillSemanticError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = code


@dataclass
class DrillableHierarchy:
    hierarchy_id: UUID
    hierarchy_name: str
    current_level_name: str
    current_level_ordinal: int
    next_level_name: str
    next_level_ordinal: int
    next_level_dimension_id: UUID
    next_level_dimension_name: str
    next_level_dimension_display_name: str


@dataclass
class DrillDimension:
    id: UUID
    name: str
    display_name: str


@dataclass
class HierarchyPathEntry:
    level_name: str
    dimension_name: str
    value: Any



def encode_cursor(offset: int) -> str:
    raw = json.dumps({"o": int(offset)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        return int(json.loads(raw)["o"])
    except (ValueError, TypeError, json.JSONDecodeError, KeyError) as exc:
        raise DrillSemanticError("INVALID_CURSOR", f"Invalid cursor: {exc}") from exc


_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 10_000


def _clamp_limit(n: int | None) -> int:
    if n is None or n <= 0:
        return _DEFAULT_LIMIT
    return min(n, _MAX_LIMIT)


def _quote(name: str) -> str:
    # Escaped identifier quoting via sqlglot (F-019-12): doubles embedded
    # quotes so a crafted column/measure name cannot break out of the
    # quoting. Mirrors the frontend ``quoteIdent``.
    return quote_ident(name)


@dataclass
class DrillCuration:
    """Resolved drill-through curation for a measure.

    detail_dim_names / joined_dim_names are model dimension *names* (the only
    thing the semantic binder can resolve in ``FROM "slug"`` SQL); fact_table
    is the physical name of the effective source table for the transparency
    response field.
    """
    detail_dim_names: list[str]
    joined_dim_names: list[str]
    row_limit_override: int | None
    fact_table: str | None
    # Name of a dimension over a primary-key column on the effective source
    # table, projectable through the semantic binder. When present it is a
    # guaranteed-unique tie-breaker that makes the leaf ORDER BY a TOTAL order
    # (Bug-1108); None when the source table has no PK-backed dimension.
    #
    # 3546 — RESIDUAL: when tiebreaker_dim_name is None, the leaf ORDER BY
    # orders by the full projection (all detail/joined dims + the measure
    # value). This is NOT a strict total order when two contributing fact rows
    # have identical values across the entire projection — LIMIT/OFFSET pages
    # may non-deterministically skip or duplicate such rows. The residual
    # cannot be closed within the semantic SQL path because connector-specific
    # pseudo-columns (PostgreSQL ctid, BigQuery _TABLE_SUFFIX, Snowflake
    # METADATA$ROW_ID) are physical identifiers not projectable through the
    # semantic binder's FROM-slug resolution. Mitigations:
    #   (1) Model a PK dimension on the source table — the builder detects
    #       it automatically and the sort becomes total.
    #   (2) Accept the residual — identical-projection rows are rare in
    #       practice; the sort is deterministic for all rows with any
    #       distinguishing value across the projection.
    tiebreaker_dim_name: str | None
    source_join_path: list[str]


# ---------------------------------------------------------------------------
# 5262+3971 — Deployed-snapshot metadata resolution
# ---------------------------------------------------------------------------

def _coerce_uuid(value: Any) -> UUID | None:
    """Coerce a snapshot value to UUID, or None."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and len(value) == 36 and value.count("-") == 4:
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


@dataclass
class _SnapshotMeta:
    """Hydrated drill metadata from a deployed snapshot.

    Provides the same data the builder used to read from live tables, but
    resolved from the immutable snapshot_json so drill SQL aligns with
    what the deployed binder will bind.
    """
    measure_by_id: dict[UUID, Any]
    dimensions_by_name: dict[str, Any]  # name -> dimension-like namespace
    dimensions_by_id: dict[UUID, Any]
    dimensions_by_source_col: dict[UUID, Any]  # source_column_id -> dim
    dimensions_by_uda: dict[UUID, Any]  # user_defined_attribute_id -> dim
    drill_sets_by_measure: dict[UUID, Any]  # measure_id -> drill-set namespace
    columns_by_id: dict[UUID, Any]  # column id -> column namespace
    tables_by_id: dict[UUID, Any]  # table id -> table namespace
    hierarchy_defs: list[Any]  # hierarchy definition namespaces
    hierarchy_levels: list[Any]  # hierarchy level namespaces
    levels_by_hier: dict[UUID, list[Any]]  # hierarchy_id -> ordered levels
    model_id: UUID


def _hydrate_snapshot(model_id: UUID, snapshot: dict[str, Any]) -> _SnapshotMeta:
    """Hydrate drill-relevant metadata from a deployed snapshot dict."""

    # Measures
    measure_by_id: dict[UUID, Any] = {}
    for m in snapshot.get("measures", []) or []:
        mid = _coerce_uuid(m.get("id"))
        if mid is None:
            continue
        measure_by_id[mid] = types.SimpleNamespace(
            id=mid,
            model_id=model_id,
            name=m.get("name", ""),
            default_agg=m.get("default_agg", "SUM"),
            source_column_id=_coerce_uuid(m.get("source_column_id")),
            user_defined_attribute_id=_coerce_uuid(m.get("user_defined_attribute_id")),
        )

    # Dimensions
    dims_by_name: dict[str, Any] = {}
    dims_by_id: dict[UUID, Any] = {}
    dims_by_src_col: dict[UUID, Any] = {}
    dims_by_uda: dict[UUID, Any] = {}
    for d in snapshot.get("dimensions", []) or []:
        did = _coerce_uuid(d.get("id"))
        name = d.get("name", "")
        if not name:
            continue
        ns = types.SimpleNamespace(
            id=did or UUID(int=0),
            model_id=model_id,
            name=name,
            display_name=d.get("display_name") or name,
            source_column_id=_coerce_uuid(d.get("source_column_id")),
            user_defined_attribute_id=_coerce_uuid(d.get("user_defined_attribute_id")),
        )
        dims_by_name[name] = ns
        if did is not None:
            dims_by_id[did] = ns
        if ns.source_column_id is not None:
            dims_by_src_col[ns.source_column_id] = ns
        if ns.user_defined_attribute_id is not None:
            dims_by_uda[ns.user_defined_attribute_id] = ns

    # Drill-through sets
    drill_sets: dict[UUID, Any] = {}
    for ds in snapshot.get("drill_through_sets", []) or []:
        ds_mid = _coerce_uuid(ds.get("measure_id"))
        if ds_mid is None:
            continue
        drill_sets[ds_mid] = types.SimpleNamespace(
            id=_coerce_uuid(ds.get("id")) or UUID(int=0),
            measure_id=ds_mid,
            source_table_id=_coerce_uuid(ds.get("source_table_id")),
            detail_columns=ds.get("detail_columns"),
            joined_dimension_ids=ds.get("joined_dimension_ids"),
            row_limit_override=ds.get("row_limit_override"),
            source_join_path=ds.get("source_join_path"),
        )

    # Model columns
    cols_by_id: dict[UUID, Any] = {}
    for c in snapshot.get("columns", []) or []:
        cid = _coerce_uuid(c.get("id"))
        if cid is None:
            continue
        cols_by_id[cid] = types.SimpleNamespace(
            id=cid,
            model_table_id=_coerce_uuid(c.get("model_table_id")),
            column_name=c.get("column_name", ""),
            is_primary_key=bool(c.get("is_primary_key")),
        )

    # Model tables
    tables_by_id: dict[UUID, Any] = {}
    for t in snapshot.get("tables", []) or []:
        tid = _coerce_uuid(t.get("id"))
        if tid is None:
            continue
        tables_by_id[tid] = types.SimpleNamespace(
            id=tid,
            physical_name=t.get("physical_name", ""),
        )

    # Hierarchies
    hier_defs: list[Any] = []
    hier_levels: list[Any] = []
    levels_by_hier: dict[UUID, list[Any]] = {}
    for h in snapshot.get("hierarchies", []) or []:
        hid = _coerce_uuid(h.get("id"))
        if hid is None:
            continue
        hier_defs.append(types.SimpleNamespace(
            id=hid,
            model_id=model_id,
            name=h.get("name", ""),
        ))
        for lvl in sorted(
            h.get("levels", []) or [],
            key=lambda l: int(l.get("ordinal", 0)),
        ):
            lvl_ns = types.SimpleNamespace(
                hierarchy_id=hid,
                ordinal=int(lvl.get("ordinal", 0)),
                name=lvl.get("name", ""),
                key_attribute_id=_coerce_uuid(lvl.get("key_attribute_id")),
                key_attribute_source=lvl.get("key_attribute_source", ""),
            )
            hier_levels.append(lvl_ns)
            levels_by_hier.setdefault(hid, []).append(lvl_ns)

    return _SnapshotMeta(
        measure_by_id=measure_by_id,
        dimensions_by_name=dims_by_name,
        dimensions_by_id=dims_by_id,
        dimensions_by_source_col=dims_by_src_col,
        dimensions_by_uda=dims_by_uda,
        drill_sets_by_measure=drill_sets,
        columns_by_id=cols_by_id,
        tables_by_id=tables_by_id,
        hierarchy_defs=hier_defs,
        hierarchy_levels=hier_levels,
        levels_by_hier=levels_by_hier,
        model_id=model_id,
    )


async def _load_deployed_snapshot(
    db: AsyncSession, model: Any,
) -> _SnapshotMeta | None:
    """Load and hydrate the deployed snapshot for drill metadata resolution.

    Returns None when the model has no deployed version, the version row
    is missing, or the snapshot is empty/unusable — the caller falls back
    to the live-table path.
    """
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return None
    version = await db.get(ModelVersion, deployed_version_id)
    if version is None or not isinstance(version.snapshot_json, dict):
        return None
    snap = version.snapshot_json
    if not snap.get("measures") and not snap.get("dimensions"):
        return None
    return _hydrate_snapshot(model.id, snap)


# ---------------------------------------------------------------------------
# Snapshot-aware metadata accessors (5262+3971)
# ---------------------------------------------------------------------------

async def _resolve_measure(
    db: AsyncSession, measure_id: UUID, snap: _SnapshotMeta | None,
) -> Any | None:
    """Resolve a Measure from the snapshot, falling back to the live DB."""
    if snap is not None:
        m = snap.measure_by_id.get(measure_id)
        if m is not None:
            return m
        # Measure present in live DB but not in deployed snapshot: the
        # modeller added a measure after the last deploy. Drill against
        # an undeployed measure must fail loud.
        live = await db.get(Measure, measure_id)
        if live is not None:
            raise DrillSemanticError(
                "DRILL_MEASURE_NOT_DEPLOYED",
                f"Measure '{live.name}' exists but is not in the deployed "
                f"model version. Deploy the model to drill this measure.",
            )
        return None
    return await db.get(Measure, measure_id)


async def _resolve_model(
    db: AsyncSession, model_id: UUID,
) -> Any | None:
    """Resolve a Model. Always from live DB (we need deployed_version_id)."""
    return await db.get(Model, model_id)


def _snap_dim_names_for_col_ids(
    snap: _SnapshotMeta, col_ids: list[UUID],
) -> list[str]:
    """Map detail_column (ModelColumn) IDs to dimension names via snapshot."""
    names = []
    for cid in col_ids:
        dim = snap.dimensions_by_source_col.get(cid)
        if dim is not None and dim.name:
            names.append(dim.name)
    return names


def _snap_dim_names_for_dim_ids(
    snap: _SnapshotMeta, dim_ids: list[UUID],
) -> list[str]:
    """Map joined_dimension_ids to dimension names via snapshot."""
    names = []
    for did in dim_ids:
        dim = snap.dimensions_by_id.get(did)
        if dim is not None and dim.name:
            names.append(dim.name)
    return names


def _snap_effective_source_table(
    snap: _SnapshotMeta, measure: Any, drill_set: Any | None,
) -> Any | None:
    """Resolve effective source table from snapshot."""
    if drill_set is not None and drill_set.source_table_id is not None:
        return snap.tables_by_id.get(drill_set.source_table_id)
    # Intrinsic table from measure's source column
    if measure.source_column_id is not None:
        col = snap.columns_by_id.get(measure.source_column_id)
        if col is not None and col.model_table_id is not None:
            return snap.tables_by_id.get(col.model_table_id)
    # UDA-backed measures: the snapshot stores user_defined_attributes
    # separately; the UDA's table_id maps to a model table. However the
    # snapshot does not currently embed UDA objects with table_id, so this
    # path returns None and the tiebreaker / source table will be absent.
    # This mirrors the live-path fallback when UDA.table_id is not set.
    return None


def _snap_intrinsic_source_table(
    snap: _SnapshotMeta, measure: Any,
) -> Any | None:
    """Resolve the measure's intrinsic source table from snapshot."""
    if measure.source_column_id is not None:
        col = snap.columns_by_id.get(measure.source_column_id)
        if col is not None and col.model_table_id is not None:
            return snap.tables_by_id.get(col.model_table_id)
    return None


def _snap_pk_tiebreaker_dim(
    snap: _SnapshotMeta, fact_table: Any | None,
) -> str | None:
    """Resolve PK-backed tiebreaker dimension from snapshot."""
    if fact_table is None:
        return None
    # Find PK columns on this table
    pk_col_ids = [
        c.id for c in snap.columns_by_id.values()
        if c.model_table_id == fact_table.id and c.is_primary_key
    ]
    if not pk_col_ids:
        return None
    # Find a dimension over a PK column, ordered by column_name for stability
    pk_dims = []
    for cid in pk_col_ids:
        dim = snap.dimensions_by_source_col.get(cid)
        if dim is not None and dim.name:
            col = snap.columns_by_id.get(cid)
            col_name = col.column_name if col else ""
            pk_dims.append((col_name, dim.name))
    if pk_dims:
        pk_dims.sort()
        return pk_dims[0][1]
    return None


async def _load_curation(
    db: AsyncSession, measure: Any,
    snap: _SnapshotMeta | None = None,
) -> DrillCuration:
    """Resolve the measure's DrillThroughSet into model-name curation.

    5262+3971: when ``snap`` is provided (deployed model), metadata is
    resolved from the snapshot rather than live tables, so the drill SQL
    aligns with what the deployed binder will bind.

    detail_columns (ModelColumn ids) and joined_dimension_ids (Dimension ids)
    are mapped to the model dimension *names* the semantic binder can resolve.
    A detail column with no dimension over it is not projectable through the
    semantic chokepoint and raises ``DRILL_DETAIL_COLUMN_NOT_PROJECTABLE``
    (5261: fail loud rather than silently dropping). The effective source
    table -- the measure's intrinsic table, or the curated override -- supplies
    the ``fact_table`` transparency field and bounds projectable detail dims.
    """
    # --- resolve drill set ---
    if snap is not None:
        drill_set = snap.drill_sets_by_measure.get(measure.id)
    else:
        result = await db.execute(
            select(DrillThroughSet).where(DrillThroughSet.measure_id == measure.id)
        )
        drill_set = result.scalar_one_or_none()

    # --- resolve effective source table ---
    if snap is not None:
        fact_table = _snap_effective_source_table(snap, measure, drill_set)
    else:
        fact_table = await _resolve_effective_source_table(db, measure, drill_set)
    fact_table_name = fact_table.physical_name if fact_table is not None else None

    if drill_set is None:
        if snap is not None:
            tiebreaker = _snap_pk_tiebreaker_dim(snap, fact_table)
        else:
            tiebreaker = await _resolve_pk_tiebreaker_dim(db, measure.model_id, fact_table)
        return DrillCuration([], [], None, fact_table_name, tiebreaker, [])

    # --- resolve detail columns -> dimension names ---
    detail_dim_names: list[str] = []
    if drill_set.detail_columns:
        col_ids = [UUID(str(c)) for c in drill_set.detail_columns]
        if snap is not None:
            detail_dim_names = _snap_dim_names_for_col_ids(snap, col_ids)
        else:
            dim_rows = await db.execute(
                select(Dimension.name).where(
                    Dimension.model_id == measure.model_id,
                    Dimension.source_column_id.in_(col_ids),
                )
            )
            detail_dim_names = [n for n in dim_rows.scalars().all() if n]
        # 5261: surface unprojectable detail columns loudly rather than
        # silently dropping them.
        if len(detail_dim_names) < len(col_ids):
            n_dropped = len(col_ids) - len(detail_dim_names)
            raise DrillSemanticError(
                "DRILL_DETAIL_COLUMN_NOT_PROJECTABLE",
                f"{n_dropped} drill-through detail column(s) have no "
                f"dimension and cannot be projected through the semantic "
                f"layer. Create a dimension over each column or remove "
                f"it from the drill-through set.",
            )

    # --- resolve joined dimension names ---
    joined_dim_names: list[str] = []
    if drill_set.joined_dimension_ids:
        dim_ids = [UUID(str(d)) for d in drill_set.joined_dimension_ids]
        if snap is not None:
            joined_dim_names = _snap_dim_names_for_dim_ids(snap, dim_ids)
        else:
            jrows = await db.execute(
                select(Dimension.name).where(
                    Dimension.model_id == measure.model_id,
                    Dimension.id.in_(dim_ids),
                )
            )
            joined_dim_names = [n for n in jrows.scalars().all() if n]

    # --- tiebreaker ---
    if snap is not None:
        tiebreaker = _snap_pk_tiebreaker_dim(snap, fact_table)
    else:
        tiebreaker = await _resolve_pk_tiebreaker_dim(db, measure.model_id, fact_table)

    source_join_path = [str(jid) for jid in (drill_set.source_join_path or [])]

    # --- join-path validation ---
    if snap is not None:
        intrinsic_table = _snap_intrinsic_source_table(snap, measure)
    else:
        intrinsic_table = await _resolve_intrinsic_source_table(db, measure)
    if (
        drill_set.source_table_id is not None
        and intrinsic_table is not None
        and drill_set.source_table_id != intrinsic_table.id
        and not source_join_path
    ):
        raise DrillSemanticError(
            "DRILL_JOIN_PATH_REQUIRED",
            "This drill-through source-table override has no executable join path. "
            "Choose and save a source join path before drilling.",
        )
    return DrillCuration(
        detail_dim_names=detail_dim_names,
        joined_dim_names=joined_dim_names,
        row_limit_override=drill_set.row_limit_override,
        fact_table=fact_table_name,
        tiebreaker_dim_name=tiebreaker,
        source_join_path=source_join_path,
    )


async def _resolve_effective_source_table(
    db: AsyncSession, measure: Measure, drill_set: DrillThroughSet | None,
) -> ModelTable | None:
    """Effective source table = curated override, else the measure's
    intrinsic source table. Mirrors model-service's resolver so the runtime
    reads the same table the curator validated detail columns against.
    """
    if drill_set is not None and drill_set.source_table_id is not None:
        return await db.get(ModelTable, drill_set.source_table_id)
    return await _resolve_intrinsic_source_table(db, measure)


async def _resolve_intrinsic_source_table(
    db: AsyncSession, measure: Measure,
) -> ModelTable | None:
    """Resolve the measure's own physical source table, without curation."""
    if measure.source_column_id is not None:
        col = await db.get(ModelColumn, measure.source_column_id)
        if col is not None and col.model_table_id is not None:
            return await db.get(ModelTable, col.model_table_id)
    if getattr(measure, "user_defined_attribute_id", None) is not None:
        uda = await db.get(UserDefinedAttribute, measure.user_defined_attribute_id)
        if uda is not None and getattr(uda, "table_id", None) is not None:
            return await db.get(ModelTable, uda.table_id)
    return None


async def _resolve_pk_tiebreaker_dim(
    db: AsyncSession, model_id: UUID, fact_table: ModelTable | None,
) -> str | None:
    """Resolve a unique tie-breaker dimension for the leaf ORDER BY (Bug-1108).

    Returns the *name* of a model dimension that sits over a primary-key
    column of the effective source table, so it is projectable through the
    semantic binder (``FROM "slug"``) and guaranteed distinct per fact row.
    Appending it to the leaf ORDER BY turns the sort into a TOTAL order, so
    LIMIT/OFFSET pagination cannot skip or duplicate rows on any engine
    (PostgreSQL/BigQuery/Spark/Snowflake/Redshift) regardless of scan order.

    Returns ``None`` when the source table is unknown or has no PK-backed
    dimension; the caller then falls back to ordering by the full projection.
    """
    if fact_table is None:
        return None
    result = await db.execute(
        select(Dimension.name)
        .join(ModelColumn, Dimension.source_column_id == ModelColumn.id)
        .where(
            Dimension.model_id == model_id,
            ModelColumn.model_table_id == fact_table.id,
            ModelColumn.is_primary_key.is_(True),
        )
        .order_by(ModelColumn.column_name)
    )
    name = result.scalars().first()
    return name or None


async def resolve_drill_options(
    *,
    measure_id: UUID,
    grouping_levels: Sequence[dict[str, Any]],
    db: AsyncSession,
) -> list[DrillableHierarchy]:
    """Return drillable hierarchies for a set of grouping dimensions."""
    measure = await db.get(Measure, measure_id)
    if measure is None:
        raise DrillSemanticError("MEASURE_NOT_FOUND", f"Measure {measure_id} not found")

    # 5262+3971: load deployed snapshot for consistent metadata resolution
    model = await _resolve_model(db, measure.model_id)
    snap = await _load_deployed_snapshot(db, model) if model is not None else None
    if snap is not None:
        measure = await _resolve_measure(db, measure_id, snap)
        if measure is None:
            raise DrillSemanticError("MEASURE_NOT_FOUND", f"Measure {measure_id} not found")

    dim_names = [g["column"] for g in grouping_levels if g.get("column")]
    if not dim_names:
        return []

    dims = await _load_dimensions_by_name(db, measure.model_id, dim_names, snap=snap)
    return await _find_drillable_hierarchies(db, measure.model_id, dims, snap=snap)


async def build_drill_sql(
    *,
    measure_id: UUID,
    hierarchy_id: UUID | None,
    grouping_levels: Sequence[dict[str, Any]],
    filters: Sequence[dict[str, Any]] | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    db: AsyncSession,
) -> tuple[
    str, str, int, int, DrillDimension | None, str,
    list[HierarchyPathEntry], list[DrillableHierarchy], str | None, list[str],
]:
    """Build semantic SQL for a drill-through.

    Two products, decided by ``drill_mode``:

    * ``hierarchy`` -- the cell sits on a hierarchy dimension above its leaf,
      so the drill steps down one level and aggregates the measure at the
      next level (``SELECT <next_level>, AGG(measure) GROUP BY <next_level>``).
    * ``leaf`` -- the cell is at the bottom of its hierarchy (or on a
      non-hierarchy dimension), so the drill returns the *contributing detail
      rows* behind the cell: the curated detail dimensions (plus any joined
      dimensions), projected without rolling the measure up. This is the
      "which rows made up this number?" product (F-019-02).

    Curation (F-019-01): the measure's DrillThroughSet narrows the leaf
    projection to its detail columns (PII / noise hiding), adds joined
    dimensions, and overrides the page limit. Curation does not change the
    hierarchy step-down product.

    ``filters`` (F-019-03) are additional predicates (active slicers) ANDed
    with the cell coordinates so the drill reconciles with the clicked cell.

    5262+3971 -- SNAPSHOT-ALIGNED: when the model has a deployed version,
    all metadata (measures, dimensions, hierarchies, drill-through sets)
    is resolved from the deployed snapshot, the same source the execution
    binder uses. This ensures the drill SQL references only objects the
    deployed binder knows, preventing silent divergence between the
    builder and the binder. When no deployed snapshot exists (undeployed
    model), falls back to the live tables.

    Returns (sql, model_id_str, offset, effective_limit, drill_dimension,
             drill_mode, hierarchy_path, drillable_hierarchies, fact_table,
             source_join_path).
    """
    # Always load model from live DB (need deployed_version_id)
    live_measure = await db.get(Measure, measure_id)
    if live_measure is None:
        raise DrillSemanticError("MEASURE_NOT_FOUND", f"Measure {measure_id} not found")

    model = await db.get(Model, live_measure.model_id)
    if model is None:
        raise DrillSemanticError("MODEL_NOT_FOUND", "Model not found")

    # 5262+3971: resolve from deployed snapshot when available
    snap = await _load_deployed_snapshot(db, model)
    measure = await _resolve_measure(db, measure_id, snap)
    if measure is None:
        raise DrillSemanticError("MEASURE_NOT_FOUND", f"Measure {measure_id} not found")

    curation = await _load_curation(db, measure, snap=snap)

    dim_names = [g["column"] for g in grouping_levels if g.get("column")]
    dims_by_name = await _load_dimensions_by_name(
        db, measure.model_id, dim_names, snap=snap,
    )

    drillable = await _find_drillable_hierarchies(
        db, measure.model_id, dims_by_name, snap=snap,
    )

    drill_target: DrillableHierarchy | None = None
    if hierarchy_id is not None:
        # Pick the deepest level for this hierarchy — when grouping_levels
        # contain Year+Month from the same hierarchy, we need Month→Day,
        # not Year→Month.
        candidates = [h for h in drillable if h.hierarchy_id == hierarchy_id]
        if candidates:
            drill_target = max(candidates, key=lambda h: h.current_level_ordinal)
    elif len(drillable) == 1:
        drill_target = drillable[0]

    offset = decode_cursor(cursor)
    # Row-limit precedence: explicit request limit > curated override >
    # global default. All clamped to the 10k ceiling.
    effective_limit = _clamp_limit(limit if limit is not None else curation.row_limit_override)

    hierarchy_path = _build_path(grouping_levels, dims_by_name)

    if drill_target is not None:
        drill_dim = DrillDimension(
            id=drill_target.next_level_dimension_id,
            name=drill_target.next_level_dimension_name,
            display_name=drill_target.next_level_dimension_display_name,
        )
        drill_mode = "hierarchy"
    else:
        drill_dim = None
        drill_mode = "leaf"

    if drill_target is not None:
        # Hierarchy step-down: aggregate the measure at the next level.
        agg = (measure.default_agg or "SUM").upper()
        measure_expr = (
            f"COUNT(DISTINCT {_quote(measure.name)})"
            if agg == "COUNT_DISTINCT"
            else f"{agg}({_quote(measure.name)})"
        )
        target_dim_name = drill_target.next_level_dimension_name
        select_cols = f"{_quote(target_dim_name)}, {measure_expr} AS {_quote(measure.name)}"
        group_by = _quote(target_dim_name)
        order_cols = [target_dim_name]
    else:
        # Leaf detail mode (F-019-02): project the contributing rows, NOT a
        # restated aggregate. Projection = curated detail dimensions (else the
        # cell's own grouping dimensions) plus any joined dimensions, and the
        # measure's own value column UN-aggregated so the rows reconcile with
        # the clicked cell (SUM of the projected measure column == cell value).
        # No GROUP BY and no aggregation — these are the contributing fact rows.
        leaf_dims = _resolve_leaf_detail_dimensions(grouping_levels, curation)
        # The bare measure name binds to its raw value column (the semantic
        # binder resolves an un-aggregated measure reference to its source
        # column), so it appears once per contributing fact row.
        projected = [*leaf_dims, measure.name]
        # De-duplicate in case the measure column is also a curated detail dim.
        seen: set[str] = set()
        cols: list[str] = []
        for c in projected:
            if c and c not in seen:
                seen.add(c)
                cols.append(c)
        # Bug-1108 + 3546 — TOTAL-ORDER leaf ORDER BY. ``leaf_dims`` are the
        # cell's grouping coordinates, which are CONSTANT for a given cell
        # (e.g. account_type='WALLET'), so ordering by them alone sorts by a
        # constant and LIMIT/OFFSET pages are only stable by accident of the
        # engine's scan order. To make the sort a total order we order by:
        #   1. the full projection (leaf detail dims + joined dims + the
        #      un-aggregated measure value) — every projected attribute breaks
        #      ties, the widest deterministic key reachable through the binder;
        #   2. a PK-backed dimension on the source table when one is
        #      projectable — a guaranteed-unique tail so two fact rows with
        #      identical projected values still get a distinct sort position.
        # Both are dialect-neutral (sqlglot quoting; downstream transpile).
        # 3546 RESIDUAL: when no PK-backed dimension exists, two fact rows
        # with identical values across the full projection share the same
        # sort position and LIMIT/OFFSET may non-deterministically skip or
        # duplicate them. Connector pseudo-columns (ctid, ROW_ID) are not
        # projectable through the semantic binder. The modeller can close
        # this gap by adding a PK dimension to the source table.
        order_cols = list(cols)
        tb = curation.tiebreaker_dim_name
        if tb:
            if tb not in cols:
                cols.append(tb)
            if tb not in order_cols:
                order_cols.append(tb)
        select_cols = ", ".join(_quote(c) for c in cols)
        group_by = ""

    # Predicate compilation via sqlglot (F-019-05): type-safe literals,
    # escaped identifiers, supported operators. Cell coordinates default to
    # equality; slicer ``filters`` carry their own operators.
    try:
        coord_where = compile_where(list(grouping_levels))
        filter_where = compile_where(list(filters or []))
    except DrillPredicateError as exc:
        raise DrillSemanticError(exc.error_code, str(exc)) from exc

    where_clauses = [w for w in (coord_where, filter_where) if w]
    where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    group_sql = f" GROUP BY {group_by}" if group_by else ""
    # Deterministic ORDER BY (F-019-04): without it, LIMIT/OFFSET pagination
    # can repeat or skip rows between pages on PostgreSQL / BigQuery / Spark.
    # Bug-5344: in LEAF-detail mode the un-aggregated measure VALUE is sorted
    # DESCENDING so the first page surfaces the biggest contributing rows
    # ("which rows made up this number?"), not the smallest — e.g. a whole first
    # page of 0.00 when many fact rows have a zero measure and the leaf
    # projection is just the bare measure column (no dimensions / no curated
    # detail columns). The detail/grouping dimensions and the PK tiebreaker stay
    # ASC, so the ordering KEY SET is unchanged and the sort is still a total
    # order (Bug-1108) — only the measure's direction flips. (Ordering by a
    # measure value is valid SQL; the antipattern is a measure in GROUP BY,
    # which this builder never emits — GROUP BY is the next-level dimension in
    # hierarchy mode and absent in leaf mode.)
    def _order_term(col: str) -> str:
        if drill_mode == "leaf" and col == measure.name:
            return f"{_quote(col)} DESC"
        return _quote(col)

    order_sql = (
        f" ORDER BY {', '.join(_order_term(c) for c in order_cols)}"
        if order_cols
        else ""
    )
    fetch_limit = effective_limit + 1
    sql = (
        f"SELECT {select_cols} FROM {_quote(model.slug)}"
        f"{where_sql}{group_sql}{order_sql}"
        f" LIMIT {fetch_limit} OFFSET {offset}"
    )

    return (
        sql,
        str(measure.model_id),
        offset,
        effective_limit,
        drill_dim,
        drill_mode,
        hierarchy_path,
        drillable,
        curation.fact_table,
        curation.source_join_path,
    )


def _build_path(
    grouping_levels: Sequence[dict[str, Any]],
    dims_by_name: dict[str, Dimension],
) -> list[HierarchyPathEntry]:
    path = []
    for g in grouping_levels:
        col = g.get("column", "")
        dim = dims_by_name.get(col)
        display = dim.display_name if dim else col
        path.append(HierarchyPathEntry(
            level_name=display or col,
            dimension_name=col,
            value=g.get("value"),
        ))
    return path


def _resolve_leaf_detail_dimensions(
    grouping_levels: Sequence[dict[str, Any]],
    curation: DrillCuration,
) -> list[str]:
    """Resolve the leaf-detail projection.

    Curated detail columns (F-019-01) take priority — these are the columns
    the modeller chose to expose, so a column they removed (PII, internal id)
    is genuinely absent from the projection. Joined dimensions are appended.
    When no detail columns are curated, fall back to the cell's grouping
    dimensions so a default measure still returns its contributing breakdown.
    De-duplicates while preserving order.
    """
    if curation.detail_dim_names:
        base = list(curation.detail_dim_names)
    else:
        base = [g["column"] for g in grouping_levels if g.get("column")]
    ordered: list[str] = []
    seen: set[str] = set()
    for name in [*base, *curation.joined_dim_names]:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


async def _load_dimensions_by_name(
    db: AsyncSession, model_id: UUID, names: list[str],
    snap: _SnapshotMeta | None = None,
) -> dict[str, Any]:
    if not names:
        return {}
    if snap is not None:
        return {n: snap.dimensions_by_name[n]
                for n in names if n in snap.dimensions_by_name}
    result = await db.execute(
        select(Dimension).where(
            Dimension.model_id == model_id,
            Dimension.name.in_(names),
        )
    )
    return {d.name: d for d in result.scalars().all()}


async def _find_drillable_hierarchies(
    db: AsyncSession,
    model_id: UUID,
    dims_by_name: dict[str, Any],
    snap: _SnapshotMeta | None = None,
) -> list[DrillableHierarchy]:
    """For each grouping dimension, check if it belongs to a hierarchy
    and has a next level with a corresponding Dimension row."""
    if not dims_by_name:
        return []

    attr_ids: dict[str, UUID] = {}
    for name, dim in dims_by_name.items():
        if dim.user_defined_attribute_id:
            attr_ids[name] = dim.user_defined_attribute_id
        elif dim.source_column_id:
            attr_ids[name] = dim.source_column_id

    if not attr_ids:
        return []

    all_attr_id_set = set(attr_ids.values())

    if snap is not None:
        # Resolve from snapshot hierarchy data
        levels = [l for l in snap.hierarchy_levels
                  if l.key_attribute_id in all_attr_id_set]
        if not levels:
            return []
        hierarchy_ids = {lvl.hierarchy_id for lvl in levels}
        hierarchies = {h.id: h for h in snap.hierarchy_defs
                       if h.id in hierarchy_ids and h.model_id == model_id}
        levels_by_hier = {hid: snap.levels_by_hier.get(hid, [])
                          for hid in hierarchy_ids}
    else:
        levels_result = await db.execute(
            select(HierarchyLevel).where(
                HierarchyLevel.key_attribute_id.in_(all_attr_id_set)
            )
        )
        levels = list(levels_result.scalars().all())
        if not levels:
            return []

        hierarchy_ids = {lvl.hierarchy_id for lvl in levels}
        hier_result = await db.execute(
            select(HierarchyDefinition).where(
                HierarchyDefinition.id.in_(hierarchy_ids),
                HierarchyDefinition.model_id == model_id,
            )
        )
        hierarchies = {h.id: h for h in hier_result.scalars().all()}

        all_levels_result = await db.execute(
            select(HierarchyLevel).where(
                HierarchyLevel.hierarchy_id.in_(hierarchy_ids)
            ).order_by(HierarchyLevel.ordinal)
        )
        all_levels = list(all_levels_result.scalars().all())
        levels_by_hier: dict[UUID, list] = {}
        for lvl in all_levels:
            levels_by_hier.setdefault(lvl.hierarchy_id, []).append(lvl)

    drillable: list[DrillableHierarchy] = []
    for dim_name, attr_id in attr_ids.items():
        for lvl in levels:
            if lvl.key_attribute_id != attr_id:
                continue
            hier = hierarchies.get(lvl.hierarchy_id)
            if hier is None:
                continue
            siblings = levels_by_hier.get(lvl.hierarchy_id, [])
            next_lvl = next(
                (s for s in siblings if s.ordinal == lvl.ordinal + 1), None
            )
            if next_lvl is None:
                continue
            next_dim = await _resolve_level_dimension(
                db, model_id, next_lvl, snap=snap,
            )
            if next_dim is None:
                continue
            drillable.append(DrillableHierarchy(
                hierarchy_id=hier.id,
                hierarchy_name=hier.name,
                current_level_name=lvl.name,
                current_level_ordinal=lvl.ordinal,
                next_level_name=next_lvl.name,
                next_level_ordinal=next_lvl.ordinal,
                next_level_dimension_id=next_dim.id,
                next_level_dimension_name=next_dim.name,
                next_level_dimension_display_name=next_dim.display_name or next_dim.name,
            ))
    return drillable


async def _resolve_level_dimension(
    db: AsyncSession, model_id: UUID, level: Any,
    snap: _SnapshotMeta | None = None,
) -> Any | None:
    """Find the Dimension that corresponds to a hierarchy level."""
    if snap is not None:
        if level.key_attribute_source == "user_defined_attribute":
            return snap.dimensions_by_uda.get(level.key_attribute_id)
        return snap.dimensions_by_source_col.get(level.key_attribute_id)

    if level.key_attribute_source == "user_defined_attribute":
        result = await db.execute(
            select(Dimension).where(
                Dimension.model_id == model_id,
                Dimension.user_defined_attribute_id == level.key_attribute_id,
            )
        )
        return result.scalar_one_or_none()
    result = await db.execute(
        select(Dimension).where(
            Dimension.model_id == model_id,
            Dimension.source_column_id == level.key_attribute_id,
        )
    )
    return result.scalar_one_or_none()
