"""Raw-route SQL construction for the query rewriter.

Builds a flat-row (ungrouped) SQL query against the model's source database.
Used when the gateway detects a BI-client SELECT with no GROUP BY: the caller
wants per-row data, not aggregated summaries.

Key differences from ``rewrite_for_source`` (source_sql.py):
  * No GROUP BY clause.
  * No aggregation wrappers (SUM/AVG/COUNT) around measure columns.
  * All JOINs forced to LEFT JOIN (ignore model's inner setting).
  * Disconnected tables (unreachable from fact via join graph) are not joined;
    their columns appear as schema-preserving ``CAST(NULL AS <type>)`` so the
    result column count and order stay stable for BI clients.
  * Time-variant measures and COUNT(*) emit typed NULLs (meaningless at row
    level).
  * Dialect translation via ``_transpile_to_dialect`` (Bug-5900): this module
    builds SQL internally in PostgreSQL-canonical form (``_qid``/``_qcol``
    always emit double-quoted identifiers), so the translation step must
    always READ as postgres regardless of the query's ``input_dialect`` — the
    latter only describes how the *user's original SQL* was authored, not how
    this internally-generated SQL is quoted.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from shared.semantic.calculated_expression import (
    ExpressionValidationError,
    parse_expression,
)
from shared.semantic.graph_order import is_fact_table

from shared.connector_qualify import safe_ident
from src.ir.logical_query import BoundQuery, SemanticBindingError


class RawRouteUnsupported(Exception):
    """The raw route cannot faithfully represent this query.

    Raised instead of producing SQL that drops predicates or columns; the
    router catches it and falls back to the source (passthrough) rewrite,
    which preserves the original WHERE verbatim.
    """
from src.rewrite.conditions import _render_where
from src.rewrite.dialect_resolution import _resolve_target_dialect
from src.rewrite.dialects import _dialect_to_connector, _transpile_to_dialect
from src.rewrite.joins import _coerce_join_pair
from src.rewrite.table_resolution import _load_model_graph
from src.rewrite.uda import _render_uda_expression


_PG_TYPE_MAP = {
    "int2": "SMALLINT",
    "int4": "INTEGER",
    "int8": "BIGINT",
    "float4": "REAL",
    "float8": "DOUBLE PRECISION",
    "numeric": "NUMERIC",
    "decimal": "NUMERIC",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
    "text": "TEXT",
    "varchar": "TEXT",
    "char": "TEXT",
    "date": "DATE",
    "time": "TIME",
    "timetz": "TIME",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMP",
    "timestamp_tz": "TIMESTAMP",
    "datetime": "TIMESTAMP",
    "uuid": "TEXT",
    "json": "TEXT",
    "jsonb": "TEXT",
    "bigint": "BIGINT",
    "integer": "INTEGER",
    "smallint": "SMALLINT",
    "real": "REAL",
    "double precision": "DOUBLE PRECISION",
}


def _pg_null(data_type: str | None, alias: str) -> str:
    mapped = _PG_TYPE_MAP.get((data_type or "").lower().split("(")[0].strip(), "TEXT")
    # Bug-7023: use safe_ident to escape embedded double-quotes in alias.
    return f'CAST(NULL AS {mapped}) AS {safe_ident(alias)}'


async def rewrite_for_raw(
    bound_query: BoundQuery, db: Any, *, target_dialect: str | None = None,
) -> str:
    """Build an ungrouped SQL query for the raw route.

    Returns a flat-row query with LEFT JOINs, no aggregation, and typed-NULL
    placeholders for columns backed by unreachable tables or unsupported
    measure types.
    """
    if target_dialect is not None:
        _target_dialect = target_dialect
    elif db is not None:
        try:
            _target_dialect = await _resolve_target_dialect(db, bound_query.model.id)
        except Exception:
            _target_dialect = "postgres"
    else:
        _target_dialect = "postgres"

    # Bug-7023: use safe_ident (escapes embedded double-quotes) instead of
    # bare f'"{name}"' to prevent SQL injection via malicious identifiers.
    def _qid(name: str) -> str:
        return safe_ident(name)

    def _qcol(alias: str, col: str) -> str:
        return f'{safe_ident(alias)}.{safe_ident(col)}'

    col_ids: set = set()
    uda_ids: set = set()
    dimensions_by_name = dict(
        getattr(bound_query, "resolved_dimensions_by_name", {}) or {}
    )
    if not dimensions_by_name:
        dimensions_by_name = {
            dim.name: dim for dim in bound_query.resolved_dimensions
        }

    for dim in bound_query.resolved_dimensions:
        sc = getattr(dim, "source_column_id", None)
        if sc:
            col_ids.add(sc)
        ua = getattr(dim, "user_defined_attribute_id", None)
        if ua:
            uda_ids.add(ua)

    for meas in bound_query.resolved_measures:
        sc = getattr(meas, "source_column_id", None)
        if sc:
            col_ids.add(sc)
        ua = getattr(meas, "user_defined_attribute_id", None)
        if ua:
            uda_ids.add(ua)

    # Bug-7766 (R2 Codex Finding 1): a calculated measure references base
    # measures BY NAME in its expression, but the binder only puts EXPLICITLY
    # selected fields in ``resolved_measures`` — it does NOT add the base
    # measures referenced inside a selected calc measure. So a calc measure
    # selected ALONE (e.g. ``SELECT total_value`` where ``total_value =
    # measure("price") * measure("qty")`` and neither base is co-selected) has
    # references that are absent from ``resolved_measures``. The non-raw
    # ``_build_source_sql`` path loads those referenced base measures from the
    # DB (source_sql.py Phase 4A) so they resolve; the raw route did not, so
    # the reference lookup missed and the whole calc rendered as a silent NULL
    # (pre-7766) or — after the 7766 fail-loud — raised on a VALID query.
    #
    # This dependency load MUST run BEFORE ``_load_model_graph`` (R3 Codex
    # Finding 3): the dependency measures may be UDA-backed, and on a cache
    # HIT ``_load_model_graph`` only refills UDAs whose ids are passed in via
    # ``uda_ids``. Discovering the dependency UDA ids AFTER the graph load
    # would leave a newly-deployed dependency UDA absent from ``uda_by_id`` on
    # a cache hit -> spurious "UDA not found" on a VALID query. So: load the
    # dependency Measure rows first, fold their UDA ids into ``uda_ids``, THEN
    # load the graph. ``__row_count`` is a SYNTHETIC sentinel with no persisted
    # Measure row (R3 Codex Finding 2) — exclude it from the DB dependency
    # query; the renderer handles it (typed NULL) via its own branch, which
    # must be reachable without a map entry. (A ``count_star``-typed measure IS
    # persisted and loads normally; only ``__row_count`` lacks a row.)
    calc_ref_measures_by_name: dict[str, Any] = {}
    if db is not None:
        _calc_ref_names: set[str] = set()
        _resolved_names = {m.name for m in bound_query.resolved_measures}
        for _meas in bound_query.resolved_measures:
            if getattr(_meas, "measure_type", None) != "calculated":
                continue
            try:
                _parsed = parse_expression(getattr(_meas, "expression", None) or "")
            except ExpressionValidationError:
                # An unparseable calc expression is handled (fail-loud) inside
                # _render_calc_measure_raw; skip dependency loading for it.
                continue
            for _ref in _parsed.references:
                if _ref.name in _resolved_names:
                    continue
                if _ref.name == "__row_count":
                    # Synthetic COUNT(*) sentinel — no persisted Measure row;
                    # the renderer emits a typed NULL for it. Do not attempt a
                    # DB load (would leave it map-absent and mis-classify it as
                    # an unknown-measure invalid object).
                    continue
                _calc_ref_names.add(_ref.name)
        if _calc_ref_names:
            # Bug-7803: resolve calc-DEPENDENCY base measures from the DEPLOYED
            # snapshot (fail-closed), not live draft Measure rows, so the calc
            # measure and its referenced base measures share one semantic
            # authority. Mirrors the Bug-7784 aggregate_matcher precedent and the
            # non-raw source path (source_sql.py Phase 4A). A deployed model whose
            # snapshot cannot be resolved leaves the dependency absent -> the
            # renderer fails closed under deployed semantics (never draft state);
            # live ORM is used only for an UNDEPLOYED model.
            from src.semantic.snapshot_resolver import (
                resolve_calc_dependency_measures,
            )
            _dep_measures = await resolve_calc_dependency_measures(
                bound_query.model, db, _calc_ref_names,
            )
            for _name, _m in _dep_measures.items():
                calc_ref_measures_by_name[_name] = _m
                # R3 Codex Finding 3: fold a dependency measure's UDA id into
                # uda_ids BEFORE the graph load so a cache-hit refill includes
                # it (see comment above).
                _dep_uda = getattr(_m, "user_defined_attribute_id", None)
                if _dep_uda:
                    uda_ids.add(_dep_uda)

    tables_by_id, joins, columns_by_id, uda_by_id = await _load_model_graph(
        bound_query, db, uda_ids,
    )

    required_table_ids: set = set()
    table_id_for_field: dict[str, Any] = {}

    for dim in bound_query.resolved_dimensions:
        sc_id = getattr(dim, "source_column_id", None)
        mc = columns_by_id.get(sc_id) if sc_id else None
        if mc:
            required_table_ids.add(mc.model_table_id)
            table_id_for_field[dim.name] = mc.model_table_id
        elif getattr(dim, "user_defined_attribute_id", None):
            uda = uda_by_id.get(dim.user_defined_attribute_id)
            # Bug-6121 (F-006-01): the ORM attribute on UserDefinedAttribute
            # is ``table_id``, not ``model_table_id``.  The wrong name made
            # getattr always return None, so every UDA-backed dimension fell
            # through to the typed-NULL path — silent NULL for every row.
            if uda and getattr(uda, "table_id", None):
                required_table_ids.add(uda.table_id)
                table_id_for_field[dim.name] = uda.table_id

    for meas in bound_query.resolved_measures:
        sc_id = getattr(meas, "source_column_id", None)
        mc = columns_by_id.get(sc_id) if sc_id else None
        if mc:
            required_table_ids.add(mc.model_table_id)
            table_id_for_field[meas.name] = mc.model_table_id
        elif getattr(meas, "user_defined_attribute_id", None):
            # Bug-6121-B: symmetric with the dimension UDA branch above —
            # a UDA-backed measure whose UDA lives on a non-base table must
            # register that table so it gets joined; without this, the
            # measure renders with a table alias absent from FROM (SQL error).
            uda = uda_by_id.get(meas.user_defined_attribute_id)
            if uda and getattr(uda, "table_id", None):
                required_table_ids.add(uda.table_id)
                table_id_for_field[meas.name] = uda.table_id

    # Bug-7766 (R2 Codex Finding 1): register the tables that back calc-
    # referenced base measures (loaded above but absent from resolved_measures)
    # so the join planner reaches them. Without this a calc measure selected
    # alone would find its referenced columns on tables that were never added
    # to required_table_ids -> unreachable -> the calc renders as a typed NULL
    # even though the columns exist. Mirrors the non-raw path, where the loaded
    # referenced measures' source columns / UDAs drive join planning in the
    # same pass as regular measures.
    for _ref_meas in calc_ref_measures_by_name.values():
        _sc_id = getattr(_ref_meas, "source_column_id", None)
        _mc = columns_by_id.get(_sc_id) if _sc_id else None
        if _mc:
            required_table_ids.add(_mc.model_table_id)
        else:
            _uda_id = getattr(_ref_meas, "user_defined_attribute_id", None)
            _uda = uda_by_id.get(_uda_id) if _uda_id else None
            if _uda and getattr(_uda, "table_id", None):
                required_table_ids.add(_uda.table_id)

    # Bug-5880: filters may reference model fields that are not in the SELECT
    # list (WHERE-only predicates). Resolve them against the full model
    # dimension map so their tables are joined and the predicate is rendered —
    # a dropped filter returns unfiltered rows and only the downstream
    # security audit catches it (fail-closed, but with an opaque error).
    filter_only_columns: dict[str, Any] = {}
    for f in bound_query.resolved_filters:
        fname = getattr(f, "dimension_name", None)
        if not fname or fname in table_id_for_field:
            continue
        fdim = dimensions_by_name.get(fname)
        sc_id = getattr(fdim, "source_column_id", None) if fdim else None
        mc = columns_by_id.get(sc_id) if sc_id else None
        if mc:
            required_table_ids.add(mc.model_table_id)
            table_id_for_field[fname] = mc.model_table_id
            filter_only_columns[fname] = mc

    # Bug-7016: prefer a fact table that the query actually references
    # (in ``required_table_ids``). On multi-fact models the old code picked
    # the first ``table_type="fact"`` unconditionally, which could select a
    # fact table whose columns are not queried — producing CAST(NULL) for
    # every requested column and the wrong fact's cardinality (silent wrong
    # data). Resolution order:
    #   1. Fact table in required_table_ids (best: queried fact).
    #   2. Any fact table (single-fact model fallback).
    #   3. Any table in required_table_ids (no fact table in model).
    #   4. Any table at all (last resort).
    # Deterministic tie-breaking (sorted by table id) prevents order-
    # dependent behaviour from dict iteration.
    base_table = None
    _sorted_tables = sorted(tables_by_id.items(), key=lambda kv: str(kv[0]))
    # Pass 1: fact table in required_table_ids.
    for tid, t in _sorted_tables:
        if is_fact_table(t) and tid in required_table_ids:
            base_table = t
            break
    # Pass 2: any fact table.
    if base_table is None:
        for tid, t in _sorted_tables:
            if is_fact_table(t):
                base_table = t
                break
    # Pass 3: any required table.
    if base_table is None:
        for tid, t in _sorted_tables:
            if tid in required_table_ids:
                base_table = t
                break
    # Pass 4: any table.
    if base_table is None and tables_by_id:
        base_table = _sorted_tables[0][1]
    if base_table is None:
        raise SemanticBindingError("No tables found in model for raw route")

    base_table_id = base_table.id

    adjacency: dict[Any, list[Any]] = defaultdict(list)
    for j in joins:
        adjacency[j.left_table_id].append(j)
        adjacency[j.right_table_id].append(j)

    reachable: set = {base_table_id}
    queue: list = [base_table_id]
    while queue:
        current = queue.pop(0)
        for j in adjacency.get(current, []):
            nxt = j.right_table_id if j.left_table_id == current else j.left_table_id
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)

    joinable_table_ids = required_table_ids & reachable
    unreachable_table_ids = required_table_ids - reachable

    alias_by_table_id: dict[Any, str] = {}
    for tid, t in tables_by_id.items():
        alias_by_table_id[tid] = getattr(t, "alias", None) or t.name

    base_ref = ".".join(safe_ident(p) for p in base_table.physical_name.split("."))
    from_clause = f'{base_ref} AS {_qid(alias_by_table_id[base_table_id])}'
    joined: set = {base_table_id}
    pending = joinable_table_ids - {base_table_id}

    all_joinable = set()
    for j in joins:
        all_joinable.add(j.left_table_id)
        all_joinable.add(j.right_table_id)

    def _try_join(targets: set) -> bool:
        for tid in list(joined):
            for j in adjacency.get(tid, []):
                nxt = None
                if j.left_table_id == tid and j.right_table_id in targets:
                    nxt = j.right_table_id
                elif j.right_table_id == tid and j.left_table_id in targets:
                    nxt = j.left_table_id
                if nxt is None:
                    continue
                nxt_table = tables_by_id.get(nxt)
                if not nxt_table:
                    continue
                if nxt not in alias_by_table_id:
                    alias_by_table_id[nxt] = getattr(nxt_table, "alias", None) or f"t_{len(alias_by_table_id)}"

                cur_col_id = j.left_column_id if j.left_table_id == tid else j.right_column_id
                nxt_col_id = j.right_column_id if j.left_table_id == tid else j.left_column_id
                cur_col = columns_by_id.get(cur_col_id)
                nxt_col = columns_by_id.get(nxt_col_id)
                if not cur_col or not nxt_col:
                    continue

                lhs = _qcol(alias_by_table_id[tid], cur_col.column_name)
                rhs = _qcol(alias_by_table_id[nxt], nxt_col.column_name)
                lhs, rhs = _coerce_join_pair(
                    lhs, getattr(cur_col, "data_type", None),
                    rhs, getattr(nxt_col, "data_type", None),
                )
                nxt_ref = ".".join(safe_ident(p) for p in nxt_table.physical_name.split("."))
                nonlocal from_clause
                from_clause += (
                    f' LEFT JOIN {nxt_ref} AS {_qid(alias_by_table_id[nxt])}'
                    f' ON {lhs} = {rhs}'
                )
                joined.add(nxt)
                pending.discard(nxt)
                return True
        return False

    while pending:
        if _try_join(pending):
            continue
        intermediates = all_joinable - joined
        if intermediates and _try_join(intermediates):
            continue
        break

    select_parts: list[str] = []

    for dim in bound_query.resolved_dimensions:
        tid = table_id_for_field.get(dim.name)
        if tid and tid in unreachable_table_ids:
            sc_id = getattr(dim, "source_column_id", None)
            mc = columns_by_id.get(sc_id) if sc_id else None
            dt = getattr(mc, "data_type", None) if mc else None
            select_parts.append(_pg_null(dt, dim.name))
            continue

        calc_expr = getattr(dim, "calc_expression", None)
        if calc_expr:
            qualified = _qualify_calc_expression_raw(
                calc_expr, columns_by_id, alias_by_table_id, tables_by_id,
                reachable,
            )
            if qualified is not None:
                select_parts.append(f'({qualified}) AS {_qid(dim.name)}')
            else:
                select_parts.append(_pg_null(None, dim.name))
            continue

        uda_id = getattr(dim, "user_defined_attribute_id", None)
        if uda_id:
            uda = uda_by_id.get(uda_id)
            if uda is None:
                # Bug-7024: the UDA definition is missing/orphaned — fail loud.
                raise SemanticBindingError(
                    f"Cannot resolve UDA for dimension {dim.name!r}: "
                    f"user_defined_attribute_id {uda_id!r} not found in model."
                )
            if getattr(uda, "table_id", None) in reachable:
                alias = alias_by_table_id.get(uda.table_id, "")
                try:
                    rendered = _render_uda_expression(
                        expression=uda.expression,
                        table_alias=alias,
                        target_dialect="postgres",
                    )
                    select_parts.append(f'({rendered}) AS {_qid(dim.name)}')
                    continue
                except Exception as exc:
                    # Bug-7024: a reachable UDA expression that fails to
                    # render is an invalid object — fail loud rather than
                    # silently emitting NULL (which hides data corruption).
                    raise SemanticBindingError(
                        f"Cannot render UDA expression for dimension "
                        f"{dim.name!r}: {exc}"
                    ) from exc
            # UDA table is unreachable — typed NULL is the correct stable-
            # column-count behaviour for disconnected tables.
            select_parts.append(_pg_null(
                getattr(uda, "output_data_type", None),
                dim.name,
            ))
            continue

        sc_id = getattr(dim, "source_column_id", None)
        mc = columns_by_id.get(sc_id) if sc_id else None
        if mc:
            alias = alias_by_table_id.get(mc.model_table_id, "")
            select_parts.append(f'{_qcol(alias, mc.column_name)} AS {_qid(dim.name)}')
        else:
            # Bug-7024: a dimension with no source column, no calc
            # expression, and no UDA is an invalid model object — fail loud.
            raise SemanticBindingError(
                f"Cannot resolve dimension {dim.name!r} to a physical "
                f"column in raw route: no source column, calculated "
                f"expression, or user-defined attribute mapping was found."
            )

    for meas in bound_query.resolved_measures:
        mt = getattr(meas, "measure_type", "standard")
        mname = meas.name

        if getattr(meas, "variant_of_measure_id", None):
            sc_id = getattr(meas, "source_column_id", None)
            mc = columns_by_id.get(sc_id) if sc_id else None
            dt = getattr(mc, "data_type", None) if mc else None
            select_parts.append(_pg_null(dt or "NUMERIC", mname))
            continue

        if mname == "__row_count" or mt == "count_star":
            select_parts.append(_pg_null("BIGINT", mname))
            continue

        if mt == "calculated":
            rendered = _render_calc_measure_raw(
                meas, bound_query, columns_by_id, alias_by_table_id,
                tables_by_id, uda_by_id, reachable,
                calc_ref_measures_by_name=calc_ref_measures_by_name,
            )
            select_parts.append(f'{rendered} AS {_qid(mname)}')
            continue

        tid = table_id_for_field.get(mname)
        if tid and tid in unreachable_table_ids:
            sc_id = getattr(meas, "source_column_id", None)
            mc = columns_by_id.get(sc_id) if sc_id else None
            dt = getattr(mc, "data_type", None) if mc else None
            select_parts.append(_pg_null(dt or "NUMERIC", mname))
            continue

        uda_id = getattr(meas, "user_defined_attribute_id", None)
        if uda_id:
            uda = uda_by_id.get(uda_id)
            if uda is None:
                # Bug-7024: the UDA definition is missing/orphaned — fail loud.
                raise SemanticBindingError(
                    f"Cannot resolve UDA for measure {mname!r}: "
                    f"user_defined_attribute_id {uda_id!r} not found in model."
                )
            if getattr(uda, "table_id", None) in reachable:
                alias = alias_by_table_id.get(uda.table_id, "")
                try:
                    rendered = _render_uda_expression(
                        expression=uda.expression,
                        table_alias=alias,
                        target_dialect="postgres",
                    )
                    select_parts.append(f'({rendered}) AS {_qid(mname)}')
                    continue
                except Exception as exc:
                    # Bug-7024: a reachable UDA expression that fails to
                    # render is an invalid object — fail loud rather than
                    # silently emitting NULL (which hides data corruption).
                    raise SemanticBindingError(
                        f"Cannot render UDA expression for measure "
                        f"{mname!r}: {exc}"
                    ) from exc
            # UDA table is unreachable — typed NULL for stable column count.
            select_parts.append(_pg_null(
                getattr(uda, "output_data_type", None),
                mname,
            ))
            continue

        sc_id = getattr(meas, "source_column_id", None)
        mc = columns_by_id.get(sc_id) if sc_id else None
        if mc:
            alias = alias_by_table_id.get(mc.model_table_id, "")
            select_parts.append(f'{_qcol(alias, mc.column_name)} AS {_qid(mname)}')
        else:
            # Bug-7024: a standard measure with no source column and no UDA
            # is an invalid model object — fail loud.
            raise SemanticBindingError(
                f"Cannot resolve measure {mname!r} to a physical column "
                f"in raw route: no source column or user-defined attribute "
                f"mapping was found."
            )

    if not select_parts:
        raise SemanticBindingError(
            "Cannot build raw query: no columns could be rendered"
        )

    sql = f'SELECT {", ".join(select_parts)} FROM {from_clause}'

    if bound_query.resolved_filters:
        field_expr_by_name: dict[str, str] = {}
        # Bug-6123: carry the source column type per filter field so the raw
        # route's WHERE applies the same numeric-literal typing as the general
        # path (a numeric value against an INT/NUMERIC column must render as a
        # bare token, not a quoted string, or it mis-compares on a strictly
        # typed connector). Built in lockstep with field_expr_by_name.
        col_type_by_name: dict[str, str] = {}
        for dim in bound_query.resolved_dimensions:
            sc_id = getattr(dim, "source_column_id", None)
            mc = columns_by_id.get(sc_id) if sc_id else None
            if mc and mc.model_table_id in reachable:
                alias = alias_by_table_id.get(mc.model_table_id, "")
                field_expr_by_name[dim.name] = _qcol(alias, mc.column_name)
                if getattr(mc, "data_type", None) is not None:
                    col_type_by_name[dim.name] = mc.data_type
        for meas in bound_query.resolved_measures:
            sc_id = getattr(meas, "source_column_id", None)
            mc = columns_by_id.get(sc_id) if sc_id else None
            if mc and mc.model_table_id in reachable:
                alias = alias_by_table_id.get(mc.model_table_id, "")
                field_expr_by_name.setdefault(
                    meas.name, _qcol(alias, mc.column_name)
                )
                if getattr(mc, "data_type", None) is not None:
                    col_type_by_name.setdefault(meas.name, mc.data_type)
        for fname, mc in filter_only_columns.items():
            if mc.model_table_id in reachable:
                alias = alias_by_table_id.get(mc.model_table_id, "")
                field_expr_by_name.setdefault(fname, _qcol(alias, mc.column_name))
                if getattr(mc, "data_type", None) is not None:
                    col_type_by_name.setdefault(fname, mc.data_type)
        unrenderable = [
            f.dimension_name for f in bound_query.resolved_filters
            if f.dimension_name not in field_expr_by_name
        ]
        if unrenderable:
            # Bug-5880: never drop a filter silently — unfiltered detail rows
            # are a data-exposure bug the security audit would only catch with
            # an opaque fail-closed error. Hand the query back to the source
            # rewrite, which resolves hidden/persona-scoped filter fields.
            raise RawRouteUnsupported(
                "filter(s) "
                f"{', '.join(repr(n) for n in sorted(set(unrenderable)))} "
                "could not be resolved to a reachable source column"
            )
        where_str = _render_where(
            list(bound_query.resolved_filters),
            field_expr_by_name,
            col_type_by_name=col_type_by_name,
            like_target_connector=_dialect_to_connector(_target_dialect),
            # Bug-7918: ``col_type_by_name`` holds ``ModelColumn.data_type``,
            # the source's own spelling — tz-awareness needs the source dialect.
            source_connector=_dialect_to_connector(_target_dialect),
        )
        sql += f" WHERE {where_str}"

    if bound_query.logical_query.order_by:
        # Bug-7025: ORDER BY items may reference semantic fields that are
        # NOT in the SELECT projection (ORDER BY-only fields recognised by
        # dialect_resolution).  Resolve each key through the same
        # dimension/measure physical map used for WHERE; fall back to the
        # projected output alias when no physical resolution is available
        # (the field IS already projected with an AS alias).
        _ob_phys: dict[str, str] = {}
        for dim in bound_query.resolved_dimensions:
            sc_id = getattr(dim, "source_column_id", None)
            mc = columns_by_id.get(sc_id) if sc_id else None
            if mc and mc.model_table_id in reachable:
                alias = alias_by_table_id.get(mc.model_table_id, "")
                _ob_phys.setdefault(dim.name, _qcol(alias, mc.column_name))
        for meas in bound_query.resolved_measures:
            sc_id = getattr(meas, "source_column_id", None)
            mc = columns_by_id.get(sc_id) if sc_id else None
            if mc and mc.model_table_id in reachable:
                alias = alias_by_table_id.get(mc.model_table_id, "")
                _ob_phys.setdefault(meas.name, _qcol(alias, mc.column_name))
        for fname, mc in filter_only_columns.items():
            if mc.model_table_id in reachable:
                alias = alias_by_table_id.get(mc.model_table_id, "")
                _ob_phys.setdefault(fname, _qcol(alias, mc.column_name))

        _projected_aliases = set()
        for dim in bound_query.resolved_dimensions:
            _projected_aliases.add(dim.name)
        for meas in bound_query.resolved_measures:
            _projected_aliases.add(meas.name)

        ob_parts = []
        for col_name, direction in bound_query.logical_query.order_by:
            if col_name in _ob_phys:
                ob_parts.append(f'{_ob_phys[col_name]} {direction}')
            elif col_name in _projected_aliases:
                ob_parts.append(f'{_qid(col_name)} {direction}')
            else:
                raise RawRouteUnsupported(
                    f"ORDER BY field {col_name!r} could not be resolved "
                    "to a physical column in the raw route"
                )
        sql += f' ORDER BY {", ".join(ob_parts)}'

    if bound_query.logical_query.limit is not None:
        sql += f" LIMIT {int(bound_query.logical_query.limit)}"
    if bound_query.logical_query.offset is not None:
        sql += f" OFFSET {int(bound_query.logical_query.offset)}"

    # Bug-5900: this SQL was constructed above entirely in PostgreSQL-canonical
    # form (``_qid``/``_qcol`` always emit double-quoted identifiers). It is
    # NOT the user's originally-authored SQL, so the translation read-side
    # must always be "postgres" — reading it as the query's ``input_dialect``
    # (e.g. bigquery/spark) made sqlglot treat "table"."column" as a string
    # literal instead of an identifier, corrupting non-Postgres raw-route
    # output. ``_transpile_to_dialect`` always reads postgres and writes the
    # resolved target dialect, matching every other rewrite path's contract.
    return _transpile_to_dialect(sql, _target_dialect)


def _qualify_calc_expression_raw(
    expression: str,
    columns_by_id: dict,
    alias_by_table_id: dict,
    tables_by_id: dict,
    reachable: set,
) -> str | None:
    """Qualify a calculated-dimension expression with table aliases.

    Returns None if the expression references unreachable tables.
    """
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(expression, read="postgres")
    except Exception:
        return None

    col_by_name: dict[str, tuple[str, Any]] = {}
    for cid, mc in columns_by_id.items():
        tid = mc.model_table_id
        if tid in reachable and tid in alias_by_table_id:
            col_by_name[mc.column_name.lower()] = (alias_by_table_id[tid], mc)

    has_unreachable = False

    def _qualify(node: exp.Expression) -> exp.Expression:
        nonlocal has_unreachable
        if isinstance(node, exp.Column):
            entry = col_by_name.get(node.name.lower())
            if entry:
                alias, _ = entry
                return exp.column(node.name, table=alias, quoted=True)
            else:
                has_unreachable = True
        return node

    qualified = tree.transform(_qualify)
    if has_unreachable:
        return None
    return qualified.sql(dialect="postgres")


def _render_calc_measure_raw(
    meas: Any,
    bound_query: BoundQuery,
    columns_by_id: dict,
    alias_by_table_id: dict,
    tables_by_id: dict,
    uda_by_id: dict,
    reachable: set,
    *,
    calc_ref_measures_by_name: dict | None = None,
) -> str:
    """Render a calculated measure for raw mode.

    Walks the expression tree and substitutes measure() references with
    their physical column expressions. If any referenced measure is
    unreachable or itself a time-variant/count-star, emits a typed NULL.

    Bug-7766: an INVALID calc-measure object — an expression that will not
    parse, or a reference to a measure that is not in the model, or a base
    measure with no source-column mapping at all — is model corruption, not a
    disconnected/meaningless-at-row-level value.  Mirror the Bug-7024 fail-loud
    contract the standard dimension/measure branches in this module (and the
    non-raw ``_build_source_sql`` calc path) already enforce: raise
    ``SemanticBindingError`` rather than emitting a silent typed NULL that hides
    the corruption.  The legitimately-NULL cases are preserved untouched:
    variant references and ``__row_count`` (meaningless per row), and a
    reference whose physical column exists but lives on an UNREACHABLE table
    (stable-column-count policy for disconnected tables).

    Bug-7766 (R2 Codex Finding 1): ``calc_ref_measures_by_name`` carries base
    measures referenced by the calc expression that are NOT in
    ``resolved_measures`` (the binder only resolves EXPLICITLY selected fields).
    It is merged with ``resolved_measures`` so a calc measure selected alone
    resolves its references — mirroring the non-raw path's Phase-4A load. Only
    a reference absent from BOTH is a genuine unknown-measure object (fail loud).
    """
    expression = getattr(meas, "expression", None) or ""
    try:
        parsed = parse_expression(expression)
    except ExpressionValidationError as exc:
        # Bug-7766: an unparseable calc expression is an invalid object —
        # fail loud instead of a silent typed NULL (was Bug-7024-F7).
        raise SemanticBindingError(
            f"Cannot render calculated measure {meas.name!r} in raw route: "
            f"its expression failed to parse: {exc}"
        ) from exc

    if not parsed.references:
        return _pg_null(None, meas.name).rsplit(" AS ", 1)[0]

    # Merge DB-loaded referenced base measures UNDER resolved_measures (selected
    # measures win on a name collision, matching the non-raw path).
    resolved_by_name = dict(calc_ref_measures_by_name or {})
    resolved_by_name.update({m.name: m for m in bound_query.resolved_measures})
    substitutions: dict[str, str] = {}

    for ref in parsed.references:
        # R3 Codex Finding 2: ``__row_count`` (COUNT(*)) is a SYNTHETIC
        # sentinel with no persisted Measure row, so it is never present in
        # resolved_by_name (neither the binder nor the DB dependency load
        # produce it). It is meaningless per row -> typed NULL. This check MUST
        # precede the unknown-measure raise below, or a calc selected alone that
        # references ``__row_count`` would be mis-classified as an invalid
        # object and raise on a VALID query.
        if ref.name == "__row_count":
            return _pg_null("BIGINT", meas.name).rsplit(" AS ", 1)[0]

        ref_meas = resolved_by_name.get(ref.name)
        if ref_meas is None:
            # Bug-7766: the calc references a measure that is not in the
            # resolved model — an invalid object. Fail loud (mirrors the
            # non-raw path's "references unknown measure" SemanticBindingError).
            raise SemanticBindingError(
                f"Cannot render calculated measure {meas.name!r} in raw route: "
                f"it references unknown measure {ref.name!r}."
            )

        if getattr(ref_meas, "variant_of_measure_id", None):
            return _pg_null("NUMERIC", meas.name).rsplit(" AS ", 1)[0]
        if ref_meas.name == "__row_count":
            return _pg_null("BIGINT", meas.name).rsplit(" AS ", 1)[0]

        sc_id = getattr(ref_meas, "source_column_id", None)
        mc = columns_by_id.get(sc_id) if sc_id else None
        if mc is not None:
            if mc.model_table_id in reachable:
                alias = alias_by_table_id.get(mc.model_table_id, "")
                substitutions[ref.placeholder] = (
                    f'{safe_ident(alias)}.{safe_ident(mc.column_name)}'
                )
                continue
            # Physical column exists but its table is unreachable from the fact
            # via the join graph — a disconnected table. Typed NULL is the
            # correct stable-column-count behaviour (NOT an invalid object).
            return _pg_null("NUMERIC", meas.name).rsplit(" AS ", 1)[0]

        # Bug-7766 (R1 Finding 1): a referenced base measure with no
        # source_column_id may still be UDA-backed — mirror the non-raw
        # ``_phys_for_measure`` contract (source_sql.py), which resolves the
        # source column FIRST and falls back to the user_defined_attribute
        # expression, only failing when BOTH are absent. Without this arm a
        # legitimate ``measure("uda_backed_base") * 2`` (valid, save-time
        # permitted) raised instead of rendering. The UDA arm mirrors the
        # standard-measure UDA branch above (reachable -> render expression;
        # unreachable table -> typed NULL; missing/unrenderable UDA -> raise).
        uda_id = getattr(ref_meas, "user_defined_attribute_id", None)
        if uda_id:
            uda = uda_by_id.get(uda_id)
            if uda is None:
                raise SemanticBindingError(
                    f"Cannot render calculated measure {meas.name!r} in raw "
                    f"route: referenced measure {ref.name!r} points at "
                    f"user_defined_attribute_id {uda_id!r} not found in model."
                )
            uda_tid = getattr(uda, "table_id", None)
            if uda_tid in reachable:
                alias = alias_by_table_id.get(uda_tid, "")
                try:
                    rendered_uda = _render_uda_expression(
                        expression=uda.expression,
                        table_alias=alias,
                        target_dialect="postgres",
                    )
                except Exception as exc:
                    # A reachable UDA that fails to render is an invalid object
                    # — fail loud (mirrors the standard-measure UDA branch).
                    raise SemanticBindingError(
                        f"Cannot render calculated measure {meas.name!r} in raw "
                        f"route: UDA expression for referenced measure "
                        f"{ref.name!r} failed to render: {exc}"
                    ) from exc
                substitutions[ref.placeholder] = f'({rendered_uda})'
                continue
            # UDA table is unreachable — typed NULL for stable column count.
            return _pg_null("NUMERIC", meas.name).rsplit(" AS ", 1)[0]

        # Bug-7766: neither a source column NOR a UDA — the referenced base
        # measure has no physical mapping at all (orphaned/invalid object).
        # Fail loud (mirrors the non-raw path's "cannot resolve physical
        # expression" SemanticBindingError).
        raise SemanticBindingError(
            f"Cannot render calculated measure {meas.name!r} in raw route: "
            f"referenced measure {ref.name!r} has no source-column or "
            f"user-defined-attribute mapping."
        )

    result = parsed.ast.sql(dialect="postgres")
    # Bug-7801: single-pass regex substitution keyed on full placeholder
    # tokens so (a) __tessallite_measure_ref__1 does not match inside
    # __tessallite_measure_ref__10 (prefix collision), and (b) a physical
    # expression emitted by an earlier substitution cannot be re-mutated
    # by a later one (sequential str.replace hazard). The regex matches
    # the placeholder optionally wrapped in double-quotes (sqlglot renders
    # identifiers quoted) and replaces all occurrences in one pass.
    import re as _re_mod
    if substitutions:
        # Build a combined pattern: match any placeholder (optionally quoted).
        # Sort by length descending in the alternation so the regex engine
        # tries longer tokens first (standard regex alternation semantics).
        sorted_phs = sorted(substitutions.keys(), key=len, reverse=True)
        escaped = [_re_mod.escape(ph) for ph in sorted_phs]
        # Match "placeholder" (quoted) or bare placeholder.
        pattern = "|".join(f'"{e}"|{e}' for e in escaped)

        def _sub(m: _re_mod.Match) -> str:
            token = m.group(0)
            # Strip surrounding quotes if present to get the dict key.
            key = token.strip('"')
            return substitutions[key]

        result = _re_mod.sub(pattern, _sub, result)

    return f"({result})"
