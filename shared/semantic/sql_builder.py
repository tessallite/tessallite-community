"""Shared SQL builders that compose physical SQL from a semantic model.

Canonical home for FROM/JOIN expansion and per-feature SELECT builders.
Both the scheduler's aggregate refresh jobs and the pocket refresh path
import from here so model→physical SQL has one source of truth.

Public surface
--------------
- ``build_from_clause(db, model_id) -> (from_sql, alias_by_table_id)``:
  expands ``ModelTable`` + ``Join`` into a JOINed FROM clause anchored on
  the fact table. Mirrors the BFS strategy that
  ``services/scheduler/src/jobs/full_refresh.py`` shipped with.
- ``build_pocket_select_sql(pocket, db) -> str``: emits the SELECT body
  for ``CREATE TABLE … AS <select>`` from a pocket whose ``defining_sql``
  is ``SELECT * FROM <model_slug> [WHERE …] [ORDER BY …] [LIMIT …]``.
  Translates dimension references in WHERE/ORDER BY to fully qualified
  physical column references and projects every ``ModelColumn`` exposed
  by the model.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.connector_qualify import coerce_join_types, quote_identifier, quote_table_ref
from shared.db.models import (
    Dimension,
    Measure,
    ModelColumn,
    PocketDefinition,
)
from shared.semantic.graph_order import (
    CANONICAL_ORDER_DESCRIPTION,
    anchor_is_by_convention,
    canonical_column_order,
    canonical_join_order,
    canonical_table_order,
    pick_anchor_table,
    select_model_columns,
    select_model_joins,
    select_model_tables,
)
from shared.semantic.join_keyword import join_keyword

logger = logging.getLogger(__name__)


async def build_from_clause(
    db: AsyncSession,
    model_id: object,
    needed_table_ids: set | None = None,
    physical_name_overrides: dict | None = None,
    connector: str = "postgresql",
) -> tuple[str, dict]:
    """Build a FROM clause for the model's tables and joins.

    Returns ``(from_sql, alias_by_table_id)``. Aliases let DDL builders
    emit qualified column references (``t3."active_flag"``) instead of
    bare names.

    Strategy: anchor on the model's fact table, or — when it has none,
    which is legal — on the first table in canonical ``id`` order;
    BFS-expand joins so every joined table appears exactly once with a
    deterministic short alias.

    Bug-8605: both reads go through ``shared.semantic.graph_order`` so the
    anchor and the BFS expansion are a pure function of the model's rows
    rather than of the order the database happened to return them in. The
    anchor decides the LEFT JOIN base, so an unordered read let the same
    unedited model materialise different totals run to run; the join order
    decides alias numbering and breaks ties between equally short
    anchor-to-table paths, which changes which intermediate tables reach
    the FROM clause at all. Canonical order is ``id`` — the only key the
    deployed snapshot carries verbatim and ``rehydrate_into_live``
    preserves — so a CTAS built from live rows, source SQL built from the
    deployed snapshot, and a graph rehydrated by a revert all anchor on
    the same table. See ``shared/semantic/graph_order.py``.

    When ``needed_table_ids`` is supplied, the BFS computes the minimal
    join closure — the set of tables on any path from anchor to each
    needed table, including intermediates — and only joins those.

    When ``physical_name_overrides`` is supplied (``{table_id: name}``),
    the override name is used instead of ``ModelTable.physical_name``
    for the specified tables.  Used by aggregate/pocket CTAS to
    substitute target-side calendar table names.
    """
    tables_result = await db.execute(select_model_tables(model_id))
    ordered_tables = canonical_table_order(tables_result.scalars().all())
    tables: dict = {t.id: t for t in ordered_tables}
    if not tables:
        raise ValueError(f"No ModelTable records for model {model_id}")

    joins_result = await db.execute(select_model_joins(model_id))
    joins = canonical_join_order(joins_result.scalars().all())

    col_ids = {j.left_column_id for j in joins} | {j.right_column_id for j in joins}
    cols: dict = {}
    if col_ids:
        cols_result = await db.execute(
            select(ModelColumn).where(ModelColumn.id.in_(list(col_ids)))
        )
        cols = {c.id: c for c in cols_result.scalars().all()}

    anchor = pick_anchor_table(ordered_tables)
    if anchor_is_by_convention(ordered_tables):
        logger.info(
            "Model %s has no fact table; the FROM base is chosen by canonical "
            "%s order and resolved to %r. The model carries no metadata "
            "declaring a base table, so this is a platform convention rather "
            "than a modelling decision (Bug-8605).",
            model_id, CANONICAL_ORDER_DESCRIPTION, anchor.physical_name,
        )

    # Build adjacency map for join closure computation.
    adjacency: dict[object, list[object]] = {}
    for j in joins:
        lid, rid = j.left_table_id, j.right_table_id
        if tables.get(lid) and tables.get(rid):
            adjacency.setdefault(lid, []).append(rid)
            adjacency.setdefault(rid, []).append(lid)

    # Compute the minimal join closure when pruning.
    allowed_table_ids: set | None = None
    if needed_table_ids is not None:
        allowed_table_ids = _join_closure(anchor.id, needed_table_ids, adjacency)

    _overrides = physical_name_overrides or {}

    def _phys(table_id: object, table: object) -> str:
        return _overrides.get(table_id, table.physical_name)

    alias: dict = {anchor.id: "base"}
    from_parts = [f"{quote_table_ref(connector, _phys(anchor.id, anchor))} AS base"]
    visited = {anchor.id}

    changed = True
    while changed:
        changed = False
        for j in joins:
            lid, rid = j.left_table_id, j.right_table_id
            lc = cols.get(j.left_column_id)
            rc = cols.get(j.right_column_id)
            lt = tables.get(lid)
            rt = tables.get(rid)
            if not (lc and rc and lt and rt):
                continue
            q = lambda col: quote_identifier(connector, col)  # noqa: E731

            if lid in visited and rid not in visited:
                if allowed_table_ids is not None and rid not in allowed_table_ids:
                    continue
                # Bug-8628: FORWARD traversal — the already-visited table is
                # the modeller's LEFT table, so the emitted keyword matches
                # the declaration as written.
                sql_join = join_keyword(j.join_type, flipped=False)
                a = f"t{len(alias)}"
                alias[rid] = a
                lhs_expr = f"{alias[lid]}.{q(lc.column_name)}"
                rhs_expr = f"{a}.{q(rc.column_name)}"
                lhs_expr, rhs_expr = coerce_join_types(
                    lhs_expr, getattr(lc, "data_type", None),
                    rhs_expr, getattr(rc, "data_type", None),
                )
                from_parts.append(
                    f"{sql_join} {quote_table_ref(connector, _phys(rid, rt))} AS {a} "
                    f"ON {lhs_expr} = {rhs_expr}"
                )
                visited.add(rid)
                changed = True
            elif rid in visited and lid not in visited:
                if allowed_table_ids is not None and lid not in allowed_table_ids:
                    continue
                # Bug-8628: REVERSED traversal — the already-visited table is
                # the modeller's RIGHT table, so the newly added table (the
                # modeller's LEFT one) lands on the physical right of the JOIN
                # and the keyword must FLIP to keep the same relation
                # preserved. This branch previously appended the SAME keyword
                # string as the forward branch above, so a declared
                # ``dim LEFT JOIN fact`` materialised as ``fact LEFT JOIN dim``
                # — the opposite row population to what the source route
                # (``rewrite/joins.py``, which has flipped since Bug-7775)
                # serves for the same model.
                sql_join = join_keyword(j.join_type, flipped=True)
                a = f"t{len(alias)}"
                alias[lid] = a
                lhs_expr = f"{a}.{q(lc.column_name)}"
                rhs_expr = f"{alias[rid]}.{q(rc.column_name)}"
                lhs_expr, rhs_expr = coerce_join_types(
                    lhs_expr, getattr(lc, "data_type", None),
                    rhs_expr, getattr(rc, "data_type", None),
                )
                from_parts.append(
                    f"{sql_join} {quote_table_ref(connector, _phys(lid, lt))} AS {a} "
                    f"ON {lhs_expr} = {rhs_expr}"
                )
                visited.add(lid)
                changed = True

    if needed_table_ids is not None:
        missing = needed_table_ids - visited
        if missing:
            raise ValueError(
                f"Cannot reach tables {sorted(str(t) for t in missing)} "
                f"from anchor via joins for model {model_id}"
            )

    return "\n  ".join(from_parts), alias


def _join_closure(
    anchor_id: object,
    needed: set,
    adjacency: dict[object, list[object]],
) -> set:
    """Compute the minimal set of tables on paths from anchor to each needed table.

    Uses BFS from the anchor to find a shortest path to each needed table,
    then collects every table along those paths. The anchor is always included.
    """
    from collections import deque

    parent: dict[object, object | None] = {anchor_id: None}
    queue: deque = deque([anchor_id])
    while queue:
        node = queue.popleft()
        for nbr in adjacency.get(node, []):
            if nbr not in parent:
                parent[nbr] = node
                queue.append(nbr)

    closure = {anchor_id}
    for tid in needed:
        cur = tid
        while cur is not None and cur in parent:
            closure.add(cur)
            cur = parent[cur]
    return closure


async def _load_field_index(
    db: AsyncSession, model_id: object
) -> dict[str, tuple[Any, str]]:
    """Map dimension/measure name -> (model_table_id, physical_column_name).

    Used to translate semantic column references in pocket WHERE/ORDER BY
    clauses to physical references qualified with their table alias.
    """
    index: dict[str, tuple[Any, str]] = {}

    cols_result = await db.execute(select_model_columns(model_id))
    columns_by_id = {c.id: c for c in cols_result.scalars().all()}

    dims_result = await db.execute(
        select(Dimension).where(Dimension.model_id == model_id)
    )
    for dim in dims_result.scalars().all():
        if dim.source_column_id and dim.source_column_id in columns_by_id:
            col = columns_by_id[dim.source_column_id]
            index[dim.name.lower()] = (col.model_table_id, col.column_name)

    measures_result = await db.execute(
        select(Measure).where(Measure.model_id == model_id)
    )
    for meas in measures_result.scalars().all():
        if meas.source_column_id and meas.source_column_id in columns_by_id:
            col = columns_by_id[meas.source_column_id]
            index[meas.name.lower()] = (col.model_table_id, col.column_name)

    return index


def _qualify_column_refs(
    sql_fragment: str,
    field_index: dict[str, tuple[Any, str]],
    alias_by_table_id: dict,
) -> str:
    """Rewrite bare column refs in ``sql_fragment`` to ``alias.\"col\"``.

    Uses sqlglot to walk the AST so quoted identifiers and case folding
    behave like they do in the downstream Postgres engine.
    """
    if not sql_fragment.strip():
        return sql_fragment
    import sqlglot
    from sqlglot import exp

    parsed = sqlglot.parse_one(sql_fragment, read="postgres")
    unknown: list[str] = []
    for col_node in parsed.find_all(exp.Column):
        if col_node.table:
            continue
        name = (col_node.name or "").lower()
        entry = field_index.get(name)
        if not entry:
            unknown.append(col_node.name or name)
            continue
        table_id, physical = entry
        alias = alias_by_table_id.get(table_id)
        if not alias:
            continue
        col_node.set("table", exp.to_identifier(alias))
        col_node.set("this", exp.to_identifier(physical, quoted=True))
    if unknown:
        raise ValueError(
            f"Unknown columns in query: {unknown}. "
            "These columns are not defined in the semantic model."
        )
    return parsed.sql(dialect="postgres")


async def build_pocket_select_sql(
    pocket: PocketDefinition,
    db: AsyncSession,
    connector: str = "postgresql",
) -> str:
    """Emit the SELECT used for ``CREATE TABLE <pocket> AS <select>``.

    Expects ``pocket.defining_sql`` to be a v1-shaped pocket query
    (``SELECT * FROM <model_slug> [WHERE …] [ORDER BY …] [LIMIT …]``).
    The pocket validator enforces this shape; this builder assumes it
    and raises if the WHERE/ORDER BY references a column the model does
    not expose.
    """
    import sqlglot
    from sqlglot import exp

    parsed = sqlglot.parse_one(pocket.defining_sql, read="postgres")
    if not isinstance(parsed, exp.Select):
        raise ValueError("pocket defining_sql must be a SELECT statement")

    # sqlglot uses ``from_`` for the FROM clause arg key.
    if not (parsed.args.get("from") or parsed.args.get("from_")):
        raise ValueError("pocket defining_sql is missing a FROM clause")

    # Bug-8605 (round-1 review, finding 3): canonically ordered. The alias
    # loop below resolves a duplicate column name by ARRIVAL POSITION — first
    # arrival keeps the plain name, later ones get an ``{alias}_`` prefix — and
    # those names are materialised INTO the pocket table. The pocket route
    # rewrites only the table reference, so the column names are a contract
    # with every query written against the pocket. An unordered read made that
    # contract depend on the storage engine's row order: two tables carrying
    # ``region_id`` (entirely ordinary) could swap which one owns the plain
    # name across a rebuild, and the same unchanged query would then group by
    # a different table's column.
    cols_result = await db.execute(select_model_columns(pocket.model_id))
    all_columns = canonical_column_order(cols_result.scalars().all())
    if not all_columns:
        raise ValueError(
            f"Model {pocket.model_id} has no ModelColumn records to project"
        )

    needed_table_ids = {col.model_table_id for col in all_columns}
    from_clause, alias_by_table_id = await build_from_clause(
        db, pocket.model_id, needed_table_ids=needed_table_ids or None,
        connector=connector,
    )
    field_index = await _load_field_index(db, pocket.model_id)

    seen_aliases: set[str] = set()
    select_parts: list[str] = []
    for col in all_columns:
        alias = alias_by_table_id.get(col.model_table_id)
        if not alias:
            continue
        out_alias = col.column_name
        if out_alias in seen_aliases:
            out_alias = f"{alias}_{col.column_name}"
        seen_aliases.add(out_alias)
        q = lambda name: quote_identifier(connector, name)  # noqa: E731
        select_parts.append(f"{alias}.{q(col.column_name)} AS {q(out_alias)}")

    if not select_parts:
        raise ValueError(
            f"Model {pocket.model_id} produced no projectable columns"
        )

    where_node = parsed.args.get("where")
    where_sql = ""
    if where_node is not None:
        where_inner = where_node.this.sql(dialect="postgres")
        where_sql = "\nWHERE " + _qualify_column_refs(
            where_inner, field_index, alias_by_table_id
        )

    order_nodes = parsed.args.get("order")
    order_sql = ""
    if order_nodes is not None:
        order_inner = order_nodes.sql(dialect="postgres")
        if order_inner.upper().startswith("ORDER BY"):
            order_inner = order_inner[len("ORDER BY"):].strip()
        order_sql = "\nORDER BY " + _qualify_column_refs(
            order_inner, field_index, alias_by_table_id
        )

    limit_node = parsed.args.get("limit")
    limit_sql = ""
    if limit_node is not None:
        limit_sql = "\n" + limit_node.sql(dialect="postgres")

    select_body = ",\n  ".join(select_parts)
    return (
        f"SELECT\n  {select_body}\nFROM {from_clause}"
        f"{where_sql}{order_sql}{limit_sql}"
    )
