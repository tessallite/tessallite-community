"""Calendar / time-dimension support for the query rewriter.

Time-dimension detection, calendar binding resolution, calendar column mapping,
hierarchy calendar-rule lookup, the semi-additive grain rank, and semi-additive
aggregation rendering.

Extracted from query_rewriter.py (Phase 3 decomposition); behaviour-identical.
"""
from __future__ import annotations

import inspect
import uuid
from typing import Any

from sqlglot import exp

from shared.semantic.calendar_types import normalize_calendar_type
from src.ir.logical_query import SemanticBindingError
from src.rewrite.conditions import _CanonicalFragmentPostgres
from src.rewrite.dialects import _render_for_dialect
from src.semantic.snapshot_resolver import (
    SnapshotAuthority,
    resolve_calendar_serving_shape,
)


# Map CalendarTable physical column attributes to the keys the time-variant
# emitter expects in VariantBinding.calendar_columns.
_CALENDAR_COLUMN_ATTRS: tuple[tuple[str, str], ...] = (
    ("date", "date_column"),
    ("year", "year_column"),
    ("half", "half_column"),
    ("quarter", "quarter_column"),
    ("month", "month_column"),
    ("week", "week_column"),
    ("day", "day_column"),
)


def _is_time_dimension(d: object) -> bool:
    """Check whether a resolved dimension represents a time dimension.

    ORM Dimension objects carry ``is_time_dim`` (bool).  Virtual
    dimensions produced by ``_load_hierarchy_level_dimensions`` may
    carry ``dimension_kind``.  This helper checks both so the
    rewriter works with either representation.
    """
    if getattr(d, "is_time_dim", False):
        return True
    return getattr(d, "dimension_kind", None) == "time"


_SA_GRAIN_RANK = {"day": 0, "week": 1, "month": 2, "quarter": 3, "half": 4, "year": 5}


# F-016-04 / F-016-09: the expression-vs-table decision lives in
# shared.semantic.calendar_dialects (single source of truth). Imported lazily
# inside the helper to keep this module's import graph light and to mirror the
# rest of calendar_support's lazy ORM imports.
def _calendar_type_is_expression_capable(calendar_type: str | None) -> bool:
    """True when *calendar_type*'s period boundaries can be computed with
    date arithmetic alone (no physical calendar table required).

    A None / unknown type is treated as expression-capable: None means
    'standard' (Gregorian), and an unknown token would already have been
    rejected at hierarchy-save time. The canonical set covers standard,
    fiscal, iso_week and thai_buddhist; retail_445 and hijri require a bound
    calendar table.
    """
    from shared.semantic.calendar_dialects import (
        EXPRESSION_CAPABLE_CALENDAR_TYPES,
        TABLE_BOUND_CALENDAR_TYPES,
    )

    norm = normalize_calendar_type(calendar_type) or "standard"
    if norm in TABLE_BOUND_CALENDAR_TYPES:
        return False
    # Expression-capable when explicitly listed; anything else (unknown,
    # already-validated) defaults to expression-capable.
    return norm in EXPRESSION_CAPABLE_CALENDAR_TYPES or norm not in TABLE_BOUND_CALENDAR_TYPES


def _semi_additive_agg(
    behavior: str, col_expr: str, time_expr: str, target_dialect: str = "postgres",
) -> str:
    """Build a semi-additive aggregation expression, transpiled to *target_dialect*.

    Architectural note (Bug-906): the expression is first built in PostgreSQL
    syntax (``pg``), then transpiled via SQLGlot for non-postgres targets.
    The early return for ``target_dialect == "postgres"`` skips a redundant
    parse-and-emit round-trip; it is not dialect branching without
    transpilation.

    Bug-7017: Spark does not support ``ARRAY_AGG(col ORDER BY time)``;
    sqlglot transpiles it to ``COLLECT_LIST(col)`` DROPPING the ORDER BY,
    which makes semi-additive LAST/FIRST_NON_EMPTY return a non-deterministic
    value. The fix is in ``_rewrite_semi_additive_for_spark`` (dialects.py),
    a pre-generation AST transform applied at the single ``_transpile_to_dialect``
    boundary that rewrites to ``MAX_BY``/``MIN_BY`` (Spark 3.0+) with a CASE
    WHEN NULL guard. This is SQL Rule 1 compliant (no per-connector branch
    in the renderer; the transform fires once at the transpile boundary).
    """
    b = behavior.lower()
    if b == "by_account":
        # F-015-06: by_account is rejected upstream (source_sql) because it
        # needs per-account dispatch the flat GROUP BY cannot express. Reaching
        # here means the upstream guard was bypassed; fail loud rather than
        # emit a silently-wrong plain last-non-empty (the account column is
        # not consulted here).
        raise SemanticBindingError(
            "by_account semi-additive behaviour is not supported at query "
            "time (per-account aggregation dispatch is unimplemented)."
        )
    column = exp.Var(this=col_expr)
    time_column = exp.Var(this=time_expr)

    if b in ("last_non_empty", "first_non_empty"):
        descending = b == "last_non_empty"
        ordered = exp.Order(
            this=column.copy(),
            expressions=[
                exp.Ordered(
                    this=time_column,
                    desc=descending,
                    nulls_first=descending,
                )
            ],
        )
        aggregate = exp.ArrayAgg(this=ordered)
        non_null = exp.Not(
            this=exp.Is(this=column.copy(), expression=exp.Null()),
        )
        tree: exp.Expression = exp.Bracket(
            this=exp.Paren(
                this=exp.Filter(
                    this=aggregate,
                    expression=exp.Where(this=non_null),
                )
            ),
            # SQLGlot stores bracket indexes zero-based and PostgreSQL emits
            # this as [1]; target generators use the explicit offset below.
            expressions=[exp.Literal.number(0)],
        )
    elif b == "avg_of_children":
        tree = exp.Avg(this=column)
    elif b == "min":
        tree = exp.Min(this=column)
    elif b == "max":
        tree = exp.Max(this=column)
    else:
        tree = exp.Sum(this=column)

    # Bug-5173: emit from the expression tree; no dialect SQL fragment is
    # assembled by string formatting.
    if target_dialect == "postgres":
        return _CanonicalFragmentPostgres().generate(tree)

    tree = tree.copy()
    for node in tree.walk():
        if isinstance(node, exp.Bracket) and node.args.get("offset") is None:
            node.set("offset", 0)
    # Codex gate R3: route through _render_for_dialect so Spark semi-additive
    # reject and other pre-generation transforms fire.
    return _render_for_dialect(tree, target_dialect)



def _model_id_of(objs: list[Any]) -> Any | None:
    """First non-null ``model_id`` across *objs* (measures share one model)."""
    for o in objs:
        mid = getattr(o, "model_id", None)
        if mid is not None:
            return mid
    return None


def _hydrate_calendar_from_snapshot(row: dict[str, Any]) -> Any:
    """Build a transient (un-persisted) ``CalendarTable`` from a snapshot row.

    Only keys that map to real ORM columns are passed; 36-char dashed strings are
    coerced back to ``UUID`` (mirrors ``snapshot_resolver._coerce`` for ids). The
    result exposes the same attributes the rewriter reads from a live row
    (``calendar_type``, ``fiscal_year_start_month``, ``table_name``,
    ``date_column`` and the period column map), so no downstream consumer can tell
    a pinned calendar from a live one.
    """
    from shared.db.models import CalendarTable

    valid = {c.name for c in CalendarTable.__table__.columns}
    kwargs: dict[str, Any] = {}
    for k, v in row.items():
        if k not in valid:
            continue
        if isinstance(v, str) and len(v) == 36 and v.count("-") == 4:
            try:
                v = uuid.UUID(v)
            except ValueError:
                pass
        kwargs[k] = v
    return CalendarTable(**kwargs)


async def _resolve_calendar_binding(db: Any, period_aware_measures: list[Any]) -> Any | None:
    """
    Resolve the CalendarTable used by a query's period-aware measures.

    New path (denormalized snapshot): Measure.resolved_calendar_id → CalendarTable
    in one hop. The snapshot is set at variant creation time.

    Legacy fallback: Measure.calendar_model_table_id → ModelTable.calendar_table_id
    → CalendarTable (for variants created before the multi-calendar migration).

    Deploy pin (F-013-01 / F-016-02 / F-101-03): when the owning model is
    DEPLOYED, the calendar identity — the legacy alias→calendar map AND the
    final calendar ROW (type, fiscal_year_start_month, physical column map,
    physical table name) — is resolved from the request-pinned deployed snapshot,
    never from live ORM rows. A draft fiscal-start / column-remap edit therefore
    cannot move deployed period-variant SQL until the next Deploy. Live reads
    remain the authority only for an UNDEPLOYED model (authoring / editor
    preview). A DEPLOYED model whose snapshot is missing the pinned calendar id
    fails closed rather than silently reading the mutable live row.

    Returns None if no measure has a calendar pinned. Raises
    SemanticBindingError when measures in the same query disagree on
    which calendar to use, or when a deployed snapshot cannot supply the pin.
    """
    from shared.db.models import CalendarTable, ModelTable

    resolved_cal_ids: set[Any] = set()
    legacy_alias_ids: set[Any] = set()

    for m in period_aware_measures:
        rcal = getattr(m, "resolved_calendar_id", None)
        legacy_cal = getattr(m, "calendar_model_table_id", None)
        if rcal is not None:
            resolved_cal_ids.add(rcal)
        # Bug-6711: always collect the legacy pin even when a new-style pin
        # exists. A measure may carry BOTH a new-style resolved_calendar_id
        # AND a legacy calendar_model_table_id (from before the multi-calendar
        # migration). If they point at different calendars, the old code silently
        # used the new-style pin and ignored the legacy disagreement --- the
        # query executed against the wrong calendar with no diagnostic. Always
        # recording the legacy pin ensures the len(resolved_cal_ids) > 1 check
        # below catches the mismatch.
        if legacy_cal is not None:
            legacy_alias_ids.add(legacy_cal)

    # --- fast-exit: nothing pinned -----------------------------------------
    if not resolved_cal_ids and not legacy_alias_ids:
        return None

    # --- deploy authority: pin from the snapshot when the model is deployed --
    authority, shape = await resolve_calendar_serving_shape(
        _model_id_of(period_aware_measures), db
    )
    if authority is SnapshotAuthority.DEPLOYED_SNAPSHOT_INVALID:
        # Unreachable in practice (the binder 503s before rewrite), but never
        # fall back to live for a deployed model whose snapshot is unusable.
        raise SemanticBindingError(
            "The deployed model snapshot is unavailable; cannot resolve the "
            "calendar for period-aware measures."
        )
    deployed = authority is SnapshotAuthority.DEPLOYED and shape is not None

    # --- resolve ALL legacy aliases to their calendar_table_id ------------
    # Two distinct ModelTable alias IDs may resolve to the SAME CalendarTable
    # (e.g. two dimension aliases of one calendar — a first-class concept).
    # Resolve before raising so we compare calendars, not alias row IDs.
    if legacy_alias_ids:
        for alias_id in legacy_alias_ids:
            legacy_cal_id: Any = None
            if deployed:
                # Pin the alias→calendar map from the snapshot's pinned tables
                # so a draft remap of which calendar an alias points at cannot
                # change the deployed calendar identity.
                trow = shape.tables_by_id.get(str(alias_id))
                if trow is not None:
                    legacy_cal_id = trow.get("calendar_table_id")
            else:
                alias_table = await db.get(ModelTable, alias_id)
                if inspect.isawaitable(alias_table):
                    alias_table = await alias_table
                legacy_cal_id = (
                    getattr(alias_table, "calendar_table_id", None)
                    if alias_table is not None
                    else None
                )
            if legacy_cal_id is not None:
                # Bug-6711: merge the resolved legacy calendar_table_id into
                # the resolved set so the len > 1 check below catches
                # disagreements between new-style and legacy pins.
                resolved_cal_ids.add(legacy_cal_id)
        if not resolved_cal_ids:
            # All legacy aliases were unresolvable and no new-style pin exists.
            return None
        # else: some legacy aliases were unresolvable but at least one resolved
        # or a new-style pin exists — the resolved set is authoritative.

    # --- final disagreement check -----------------------------------------
    # At this point resolved_cal_ids is guaranteed non-empty: the empty/empty
    # case fast-exits above, and the legacy-only-unresolvable case returns None.
    # Compare on stringified ids so a UUID pin and a snapshot str id for the SAME
    # calendar are not counted as two calendars.
    if len({str(c) for c in resolved_cal_ids}) > 1:
        raise SemanticBindingError(
            "Period-aware measures in this query reference more than one "
            "calendar; split the query so each pivot uses a single calendar."
        )
    cal_id = next(iter(resolved_cal_ids))

    if deployed:
        row = shape.calendar_tables_by_id.get(str(cal_id))
        if row is None:
            # Deployed model whose pinned snapshot does not carry this calendar.
            # Fail closed: never read the mutable live row for a deployed model.
            raise SemanticBindingError(
                "The deployed model snapshot does not contain the calendar bound "
                "to this query's period-aware measures; redeploy the model after "
                "binding the calendar."
            )
        return _hydrate_calendar_from_snapshot(row)

    calendar = await db.get(CalendarTable, cal_id)
    if inspect.isawaitable(calendar):
        calendar = await calendar
    return calendar


def _build_calendar_columns(calendar: Any) -> dict[str, str]:
    """Convert a CalendarTable row to the calendar_columns dict expected
    by the time-variant emitter. Skips attributes that are NULL."""
    return {
        key: value
        for key, attr in _CALENDAR_COLUMN_ATTRS
        if (value := getattr(calendar, attr, None))
    }


async def _resolve_hierarchy_calendar_rules(
    db: Any, time_dim: Any, model_id: Any,
) -> tuple[str, int | None]:
    """Load calendar_type and fiscal_year_start_month from the time
    hierarchy that owns the query's time dimension.

    Resolution order (F-016-03):

    1. ``time_dim.hierarchy_id`` — virtual level dimensions produced by
       ``load_hierarchy_level_dimensions`` carry the parent hierarchy id
       directly. Generated date hierarchies key their levels on UDAs, so
       ``source_column_id`` is None and the legacy column-match below can
       never resolve them — this is the production-default configuration and
       must be honoured, otherwise fiscal / ISO / Hijri configuration is
       silently ignored and Gregorian numbers are returned.
    2. ``time_dim.source_column_id`` — plain (non-hierarchy) date dimensions
       resolve by matching a physical-column hierarchy level.

    Returns ('standard', None) when no explicit calendar_type is set. The
    returned type is normalised to the canonical vocabulary so a legacy
    ``iso`` token aligns with an ``iso_week`` calendar table (F-016-04).

    Deploy pin (F-016-02 / F-101-03): when *model_id* is DEPLOYED, calendar_type
    and fiscal_year_start_month are resolved from the request-pinned deployed
    snapshot's ``hierarchies`` rows, not from live ``HierarchyDefinition``. A
    draft edit to a hierarchy's calendar_type / fiscal start therefore cannot
    change deployed period math until the next Deploy. Live rows remain the
    authority only for an UNDEPLOYED model.
    """
    from sqlalchemy import select as sa_select
    from shared.db.models import HierarchyDefinition, HierarchyLevel

    authority, shape = await resolve_calendar_serving_shape(model_id, db)
    if authority is SnapshotAuthority.DEPLOYED and shape is not None:
        return _hierarchy_calendar_rules_from_snapshot(shape, time_dim)
    # UNDEPLOYED / snapshot-invalid: the invalid case cannot reach here (the
    # binder 503s first), so live rows are the authoring authority.

    # Path 1: resolve straight off the hierarchy the dimension belongs to.
    hierarchy_id = getattr(time_dim, "hierarchy_id", None)
    if hierarchy_id is not None:
        result = await db.execute(
            sa_select(
                HierarchyDefinition.calendar_type,
                HierarchyDefinition.fiscal_year_start_month,
            ).where(HierarchyDefinition.id == hierarchy_id)
        )
        row = result.first()
        if row is not None:
            return (normalize_calendar_type(row[0]) or "standard", row[1])
        # The dimension names a hierarchy that no longer exists; fall through
        # to the column match rather than guessing.

    # Path 2: plain date dimension — match a physical-column hierarchy level.
    src_col_id = getattr(time_dim, "source_column_id", None)
    if src_col_id is None:
        return ("standard", None)

    result = await db.execute(
        sa_select(
            HierarchyDefinition.calendar_type,
            HierarchyDefinition.fiscal_year_start_month,
        )
        .join(HierarchyLevel, HierarchyLevel.hierarchy_id == HierarchyDefinition.id)
        .where(
            HierarchyDefinition.model_id == model_id,
            HierarchyDefinition.dimension_kind == "time",
            HierarchyLevel.key_attribute_source == "physical_column",
            HierarchyLevel.key_attribute_id == src_col_id,
        )
        .limit(1)
    )
    row = result.first()
    if row is None:
        return ("standard", None)
    return (normalize_calendar_type(row[0]) or "standard", row[1])


def _hierarchy_calendar_rules_from_snapshot(
    shape: Any, time_dim: Any,
) -> tuple[str, int | None]:
    """Pinned-snapshot mirror of ``_resolve_hierarchy_calendar_rules`` paths 1-2.

    Reads calendar_type / fiscal_year_start_month from the deployed snapshot's
    nested ``hierarchies`` rows so a draft hierarchy edit does not leak to
    deployed serving. Same two-path resolution order as the live query.
    """
    rows = shape.hierarchy_rows or []

    # Path 1: match the hierarchy the dimension belongs to by id.
    hierarchy_id = getattr(time_dim, "hierarchy_id", None)
    if hierarchy_id is not None:
        hid = str(hierarchy_id)
        for h in rows:
            if str(h.get("id")) == hid:
                return (
                    normalize_calendar_type(h.get("calendar_type")) or "standard",
                    h.get("fiscal_year_start_month"),
                )
        # Names a hierarchy absent from the snapshot; fall through to column match.

    # Path 2: plain date dimension — match a time hierarchy's physical-column level.
    src_col_id = getattr(time_dim, "source_column_id", None)
    if src_col_id is None:
        return ("standard", None)
    scid = str(src_col_id)
    for h in rows:
        if h.get("dimension_kind") != "time":
            continue
        for lvl in h.get("levels", []) or []:
            if (
                lvl.get("key_attribute_source") == "physical_column"
                and str(lvl.get("key_attribute_id")) == scid
            ):
                return (
                    normalize_calendar_type(h.get("calendar_type")) or "standard",
                    h.get("fiscal_year_start_month"),
                )
    return ("standard", None)
