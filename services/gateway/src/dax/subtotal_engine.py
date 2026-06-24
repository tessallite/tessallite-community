"""
Subtotal engine for multi-grain hierarchy expansion in XMLA Execute.

Detects when Excel requests hierarchy member expansion (.MEMBERS),
generates SQL queries at each hierarchy grain level, and merges
results with correct aggregation per measure type.

Semi-additive LAST_NON_EMPTY subtotals are computed from detail rows
in Python since the window-function SQL pattern required cannot go
through the query-router's semantic binding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


SUBTOTAL_LEVEL_KEY = "_subtotal_level"
SUBTOTAL_GRAIN_KEY = "_subtotal_grain"


@dataclass
class SubtotalLevel:
    """One level of a subtotal hierarchy."""
    name: str
    ordinal: int
    dim_name: str
    time_unit: str | None = None


@dataclass
class SubtotalHierarchy:
    """A hierarchy on an MDX axis that requires subtotal generation."""
    hierarchy_name: str
    mdx_dim_name: str
    mdx_hier_name: str
    levels: list[SubtotalLevel]
    axis: int = 1


@dataclass
class GrainQuery:
    """A SQL query at a specific grain level."""
    sql: str
    protocol: str
    grain_ordinal: int
    level_name: str
    dim_cols: list[str]
    grain_per_hierarchy: dict[str, int] | None = None


@dataclass
class GrainResult:
    """Result from executing a grain-level query."""
    query: GrainQuery
    columns: list[str]
    rows: list[dict[str, Any]]


def detect_subtotal_hierarchies(
    col_expr: str,
    row_expr: str,
    hierarchy_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
) -> list[SubtotalHierarchy]:
    """Detect MDX axes requesting full hierarchy member expansion.

    Scans axis expressions for [Dim].[Hier].MEMBERS patterns.
    Level-specific patterns like [Dim].[Hier].[Level].MEMBERS
    do NOT trigger subtotals.

    Each returned SubtotalHierarchy carries ``axis`` (0=columns, 1=rows)
    so callers can build subtotal tuples on the correct XMLA axis.
    """
    _MEMBERS_RE = re.compile(
        r'(?<!\]\.)'
        r'\[([^\]]+)\]\.\[([^\]]+)\]'
        r'(?!\s*\.\s*(?:\[|&\[))'
        r'\.(?:Members|MEMBERS|AllMembers)\b',
    )
    results: list[SubtotalHierarchy] = []
    seen: set[str] = set()

    for axis_idx, axis_expr in ((0, col_expr), (1, row_expr)):
        for m in _MEMBERS_RE.finditer(axis_expr):
            dim_part = m.group(1).strip()
            hier_part = m.group(2).strip()

            for hkey in [hier_part.lower(), dim_part.lower()]:
                if hkey in seen:
                    continue
                level_map = hierarchy_level_dim_map.get(hkey)
                if not level_map:
                    continue

                hier_def = None
                for h in hierarchy_meta:
                    if (h.get("name") or "").strip().lower() == hkey:
                        hier_def = h
                        break
                if not hier_def:
                    continue

                raw_levels = sorted(
                    hier_def.get("levels") or [],
                    key=lambda lv: int(lv.get("ordinal", 0)),
                )
                levels: list[SubtotalLevel] = []
                for lvl in raw_levels:
                    lname = (lvl.get("name") or "").strip()
                    dim_name = level_map.get(lname.lower())
                    if dim_name:
                        levels.append(SubtotalLevel(
                            name=lname,
                            ordinal=int(lvl.get("ordinal", 0)),
                            dim_name=dim_name,
                            time_unit=lvl.get("time_unit"),
                        ))

                if levels:
                    seen.add(hkey)
                    results.append(SubtotalHierarchy(
                        hierarchy_name=hier_def.get("name", ""),
                        mdx_dim_name=dim_part,
                        mdx_hier_name=hier_part,
                        levels=levels,
                        axis=axis_idx,
                    ))
                    break

    return results


def build_subtotal_queries(
    *,
    mdx_dims: list[str],
    mdx_measures: list[str],
    where_sql_clauses: list[str],
    model_slug: str,
    measures_meta: list[dict[str, Any]],
    hierarchy: SubtotalHierarchy,
    measure_canonical: dict[str, str],
) -> list[GrainQuery]:
    """Generate SQL queries at each intermediate and grand-total grain.

    Does NOT generate the detail query (the caller uses the existing one).
    LAST_NON_EMPTY measures are aggregated with SUM in these queries —
    the gateway replaces those values with Python-computed LAST_NON_EMPTY
    from the detail results afterward.
    """
    def _q(name: str) -> str:
        return f'"{name}"'

    measure_agg: dict[str, str] = {}
    for m_meta in measures_meta:
        mname = m_meta.get("name", "")
        if mname:
            measure_agg[mname] = (m_meta.get("default_agg") or "sum").upper()

    hier_dim_names = {lvl.dim_name for lvl in hierarchy.levels}
    non_hier_dims = [d for d in mdx_dims if d not in hier_dim_names]

    queries: list[GrainQuery] = []

    for level_idx in range(len(hierarchy.levels) - 2, -1, -1):
        level = hierarchy.levels[level_idx]
        grain_dims = non_hier_dims + [
            l.dim_name for l in hierarchy.levels[: level_idx + 1]
        ]
        sql = _build_grain_sql(
            grain_dims, mdx_measures, measure_agg, measure_canonical,
            where_sql_clauses, model_slug,
        )
        queries.append(GrainQuery(
            sql=sql, protocol="jdbc",
            grain_ordinal=level.ordinal,
            level_name=level.name, dim_cols=list(grain_dims),
        ))

    grand_sql = _build_grain_sql(
        non_hier_dims, mdx_measures, measure_agg, measure_canonical,
        where_sql_clauses, model_slug,
    )
    queries.append(GrainQuery(
        sql=grand_sql, protocol="jdbc",
        grain_ordinal=-1, level_name="Grand Total",
        dim_cols=list(non_hier_dims),
    ))

    return queries


def _build_grain_sql(
    grain_dims: list[str],
    mdx_measures: list[str],
    measure_agg: dict[str, str],
    measure_canonical: dict[str, str],
    where_sql_clauses: list[str],
    model_slug: str,
) -> str:
    def _q(name: str) -> str:
        return f'"{name}"'

    select_parts: list[str] = [_q(d) for d in grain_dims]
    for meas in mdx_measures:
        canonical = measure_canonical.get(meas.lower())
        if not canonical:
            continue
        agg = measure_agg.get(canonical, "SUM")
        qc = _q(canonical)
        if agg == "COUNT_DISTINCT":
            select_parts.append(f"COUNT(DISTINCT {qc}) AS {qc}")
        elif agg == "COUNT":
            select_parts.append(f"COUNT({qc}) AS {qc}")
        elif agg == "LAST_NON_EMPTY":
            select_parts.append(f"SUM({qc}) AS {qc}")
        else:
            select_parts.append(f"{agg}({qc}) AS {qc}")

    if not select_parts:
        return ""
    from_table = _q(model_slug or "model_table")
    sql = f'SELECT {", ".join(select_parts)} FROM {from_table}'
    if where_sql_clauses:
        sql += f' WHERE {" AND ".join(where_sql_clauses)}'
    if grain_dims:
        group_cols = [_q(d) for d in grain_dims]
        sql += f' GROUP BY {", ".join(group_cols)}'
    return sql


_DATE_FMTS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m", "%Y/%m",
)

_MONTH_NAMES: dict[str, int] = {}
for _i, _m in enumerate(
    ["january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"], 1
):
    _MONTH_NAMES[_m] = _i
    _MONTH_NAMES[_m[:3]] = _i

_QUARTER_MAP: dict[str, int] = {}
for _q in range(1, 21):
    _QUARTER_MAP[f"q{_q}"] = _q


def _temporal_sort_key(val: Any) -> tuple[int, float, str]:
    """Return a sort key that orders temporal values correctly.

    Tries: date/datetime objects first, then ISO-like string parsing,
    month/quarter names, then pure numeric, falling back to string.
    The tuple ensures numeric/date keys never compare against string keys.
    """
    if isinstance(val, (date, datetime)):
        ts = val.toordinal() if isinstance(val, date) else val.timestamp()
        return (0, ts, "")
    s = str(val).strip()
    if not s:
        return (2, 0.0, "")
    for fmt in _DATE_FMTS:
        try:
            dt = datetime.strptime(s, fmt)
            return (0, dt.timestamp(), "")
        except ValueError:
            continue
    low = s.lower()
    if low in _MONTH_NAMES:
        return (0, float(_MONTH_NAMES[low]), "")
    if low in _QUARTER_MAP:
        return (0, float(_QUARTER_MAP[low]), "")
    try:
        return (1, float(s), "")
    except (ValueError, TypeError):
        pass
    return (2, 0.0, s)


def _last_non_empty_value(
    group_rows: list[dict[str, Any]],
    measure: str,
    finest_dim: str,
) -> Any:
    """Return the value of *measure* at the latest period where it is non-empty.

    F-002-04 (semantics fix): "last non-empty" means the value from the most
    recent time period in which the measure actually has data — not the value
    of the temporally-last row regardless of emptiness. Taking the latest row
    blindly returns NULL whenever the newest period has no fact, when the
    correct answer is the previous period's value. We therefore sort the group
    by the temporal dimension descending and return the first non-null value.
    """
    ordered = sorted(
        group_rows,
        key=lambda r: _temporal_sort_key(r.get(finest_dim, "")),
        reverse=True,
    )
    for r in ordered:
        v = r.get(measure)
        if v is not None and str(v).strip() != "":
            return v
    return None


def _group_lne(
    group_rows: list[dict[str, Any]],
    measures: list[str],
    finest_dim: str,
) -> dict[str, Any]:
    """Per-measure last-non-empty values for one grain group."""
    return {m: _last_non_empty_value(group_rows, m, finest_dim) for m in measures}


def compute_last_non_empty_subtotals(
    detail_rows: list[dict[str, Any]],
    hierarchy: SubtotalHierarchy,
    last_non_empty_measures: list[str],
    non_hier_dims: list[str] | None = None,
) -> dict[int, dict[tuple, dict[str, Any]]]:
    """Compute LAST_NON_EMPTY values from detail rows for each grain level.

    For each subtotal grain, groups detail rows by the full grain dimensions
    (non-hierarchy dims + hierarchy dims at this level), then for each group
    takes the value from the row with the maximum value of the finest
    hierarchy dimension (i.e., the last time period).

    Returns: {grain_ordinal: {dim_val_tuple: {measure: value}}}
    """
    if not last_non_empty_measures or not detail_rows:
        return {}

    non_hier = non_hier_dims or []
    finest_dim = hierarchy.levels[-1].dim_name
    result: dict[int, dict[tuple, dict[str, Any]]] = {}

    for level_idx in range(len(hierarchy.levels) - 2, -1, -1):
        level = hierarchy.levels[level_idx]
        grain_dims = non_hier + [l.dim_name for l in hierarchy.levels[: level_idx + 1]]

        groups: dict[tuple, list[dict[str, Any]]] = {}
        for row in detail_rows:
            key = tuple(str(row.get(d, "")) for d in grain_dims)
            groups.setdefault(key, []).append(row)

        level_vals: dict[tuple, dict[str, Any]] = {}
        for key, group_rows in groups.items():
            level_vals[key] = _group_lne(group_rows, last_non_empty_measures, finest_dim)

        result[level.ordinal] = level_vals

    if last_non_empty_measures:
        if detail_rows:
            groups_gt: dict[tuple, list[dict[str, Any]]] = {}
            for row in detail_rows:
                key = tuple(str(row.get(d, "")) for d in non_hier)
                groups_gt.setdefault(key, []).append(row)
            gt_vals: dict[tuple, dict[str, Any]] = {}
            for key, group_rows in groups_gt.items():
                gt_vals[key] = _group_lne(group_rows, last_non_empty_measures, finest_dim)
            result[-1] = gt_vals

    return result


def merge_grain_results(
    detail_result: GrainResult,
    subtotal_results: list[GrainResult],
    hierarchy: SubtotalHierarchy,
    lne_overrides: dict[int, dict[tuple, dict[str, Any]]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Merge detail and subtotal results into hierarchically ordered rows.

    Each row is tagged with SUBTOTAL_LEVEL_KEY and SUBTOTAL_GRAIN_KEY.
    If lne_overrides is provided, LAST_NON_EMPTY measure values in
    subtotal rows are replaced with the pre-computed values.
    """
    lne_overrides = lne_overrides or {}

    def _tag_rows(gr: GrainResult) -> list[dict[str, Any]]:
        tagged = []
        override_dims = gr.query.dim_cols

        overrides = lne_overrides.get(gr.query.grain_ordinal, {})
        for row in gr.rows:
            r = dict(row)
            r[SUBTOTAL_LEVEL_KEY] = gr.query.level_name
            r[SUBTOTAL_GRAIN_KEY] = gr.query.grain_ordinal
            if overrides:
                key = tuple(str(r.get(d, "")) for d in override_dims)
                vals = overrides.get(key, {})
                r.update(vals)
            tagged.append(r)
        return tagged

    all_rows: list[dict[str, Any]] = []

    for sr in subtotal_results:
        if sr.query.grain_ordinal == -1:
            all_rows.extend(_tag_rows(sr))

    for sr in sorted(subtotal_results, key=lambda s: s.query.grain_ordinal):
        if sr.query.grain_ordinal >= 0:
            all_rows.extend(_tag_rows(sr))

    detail_tagged = []
    for row in detail_result.rows:
        r = dict(row)
        r[SUBTOTAL_LEVEL_KEY] = "detail"
        leaf = hierarchy.levels[-1] if hierarchy.levels else None
        r[SUBTOTAL_GRAIN_KEY] = leaf.ordinal if leaf else 999
        detail_tagged.append(r)
    all_rows.extend(detail_tagged)

    all_rows.sort(key=lambda r: _hierarchical_sort_key(r, hierarchy))

    hier_dims = [lvl.dim_name for lvl in hierarchy.levels]
    measure_cols: list[str] = []
    for c in detail_result.columns:
        if c not in set(hier_dims):
            measure_cols.append(c)

    non_hier_dims: list[str] = []
    for c in detail_result.query.dim_cols:
        if c not in set(hier_dims):
            non_hier_dims.append(c)

    columns = non_hier_dims + hier_dims + [
        c for c in measure_cols if c not in non_hier_dims
    ]

    return columns, all_rows


def _level_idx_for_ordinal(hierarchy: SubtotalHierarchy, ordinal: int) -> int:
    for i, lvl in enumerate(hierarchy.levels):
        if lvl.ordinal == ordinal:
            return i
    return -1


def _sortable_val(val: str) -> str:
    """Pad pure-integer strings so they sort numerically."""
    try:
        return f"{int(val):020d}"
    except (ValueError, TypeError):
        return val


def _hierarchical_sort_key(row: dict[str, Any], hierarchy: SubtotalHierarchy) -> tuple:
    grain = row.get(SUBTOTAL_GRAIN_KEY, -2)
    parts: list = []
    for level in hierarchy.levels:
        val = str(row.get(level.dim_name) or "")
        parts.append(_sortable_val(val))
        parts.append(1 if grain > level.ordinal else 0)
    return tuple(parts)


# ---------------------------------------------------------------------------
# Multi-hierarchy subtotal support (Bug-573)
# ---------------------------------------------------------------------------

SUBTOTAL_GRAIN_PREFIX = "_subtotal_grain_"


def build_multi_subtotal_queries(
    *,
    mdx_dims: list[str],
    mdx_measures: list[str],
    where_sql_clauses: list[str],
    model_slug: str,
    measures_meta: list[dict[str, Any]],
    hierarchies: list[SubtotalHierarchy],
    measure_canonical: dict[str, str],
) -> list[GrainQuery]:
    """Generate subtotal queries for all grain combinations across hierarchies.

    Produces the cross-product of grain levels from each hierarchy.
    Skips the all-detail combination (the caller's original query covers it).
    """
    from itertools import product as _product

    measure_agg: dict[str, str] = {}
    for m_meta in measures_meta:
        mname = m_meta.get("name", "")
        if mname:
            measure_agg[mname] = (m_meta.get("default_agg") or "sum").upper()

    all_hier_dims: set[str] = set()
    for h in hierarchies:
        for lvl in h.levels:
            all_hier_dims.add(lvl.dim_name)
    non_hier_dims = [d for d in mdx_dims if d not in all_hier_dims]

    grain_options: list[list[tuple[int, str, list[str]]]] = []
    for h in hierarchies:
        opts: list[tuple[int, str, list[str]]] = []
        finest = h.levels[-1]
        opts.append((finest.ordinal, "detail", [l.dim_name for l in h.levels]))
        for level_idx in range(len(h.levels) - 2, -1, -1):
            level = h.levels[level_idx]
            opts.append((
                level.ordinal, level.name,
                [l.dim_name for l in h.levels[: level_idx + 1]],
            ))
        opts.append((-1, "All", []))
        grain_options.append(opts)

    detail_combo = tuple(opts[0] for opts in grain_options)

    queries: list[GrainQuery] = []
    for combo in _product(*grain_options):
        if combo == detail_combo:
            continue

        grain_spec: dict[str, int] = {}
        grain_dims = list(non_hier_dims)
        level_parts: list[str] = []

        for i, (ordinal, level_name, hier_dims) in enumerate(combo):
            grain_spec[hierarchies[i].hierarchy_name] = ordinal
            grain_dims.extend(hier_dims)
            if level_name != "detail":
                level_parts.append(
                    f"{hierarchies[i].hierarchy_name}:{level_name}"
                )

        if all(c[1] == "All" for c in combo):
            label = "Grand Total"
        elif level_parts:
            label = " x ".join(level_parts)
        else:
            label = "subtotal"

        sql = _build_grain_sql(
            grain_dims, mdx_measures, measure_agg, measure_canonical,
            where_sql_clauses, model_slug,
        )
        queries.append(GrainQuery(
            sql=sql, protocol="jdbc",
            grain_ordinal=sum(c[0] for c in combo),
            level_name=label,
            dim_cols=grain_dims,
            grain_per_hierarchy=grain_spec,
        ))

    return queries


def compute_multi_lne_subtotals(
    detail_rows: list[dict[str, Any]],
    hierarchies: list[SubtotalHierarchy],
    last_non_empty_measures: list[str],
    subtotal_queries: list[GrainQuery],
) -> dict[tuple, dict[tuple, dict[str, Any]]]:
    """Compute LAST_NON_EMPTY overrides for multi-hierarchy grain combinations.

    Returns {dim_cols_key: {row_val_tuple: {measure: value}}}.
    Keyed by tuple(dim_cols) so merge can look up by query dim_cols.
    """
    if not last_non_empty_measures or not detail_rows or not subtotal_queries:
        return {}

    finest_dim = None
    for h in hierarchies:
        if any(lvl.time_unit for lvl in h.levels):
            finest_dim = h.levels[-1].dim_name
            break
    if not finest_dim:
        finest_dim = hierarchies[0].levels[-1].dim_name

    result: dict[tuple, dict[tuple, dict[str, Any]]] = {}

    for sq in subtotal_queries:
        grain_dims = sq.dim_cols
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for row in detail_rows:
            key = tuple(str(row.get(d, "")) for d in grain_dims)
            groups.setdefault(key, []).append(row)

        level_vals: dict[tuple, dict[str, Any]] = {}
        for key, group_rows in groups.items():
            level_vals[key] = _group_lne(group_rows, last_non_empty_measures, finest_dim)
        result[tuple(grain_dims)] = level_vals

    return result


def merge_multi_hierarchy_results(
    detail_result: GrainResult,
    subtotal_results: list[GrainResult],
    hierarchies: list[SubtotalHierarchy],
    lne_overrides: dict[tuple, dict[tuple, dict[str, Any]]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Merge detail and subtotal results for multiple hierarchies."""
    lne_overrides = lne_overrides or {}

    def _tag_rows(gr: GrainResult) -> list[dict[str, Any]]:
        tagged = []
        override_key = tuple(gr.query.dim_cols)
        overrides = lne_overrides.get(override_key, {})
        for row in gr.rows:
            r = dict(row)
            r[SUBTOTAL_LEVEL_KEY] = gr.query.level_name
            r[SUBTOTAL_GRAIN_KEY] = gr.query.grain_ordinal
            if gr.query.grain_per_hierarchy:
                for hname, ordinal in gr.query.grain_per_hierarchy.items():
                    r[SUBTOTAL_GRAIN_PREFIX + hname] = ordinal
            if overrides:
                key = tuple(str(r.get(d, "")) for d in gr.query.dim_cols)
                vals = overrides.get(key, {})
                r.update(vals)
            tagged.append(r)
        return tagged

    all_rows: list[dict[str, Any]] = []
    for sr in subtotal_results:
        all_rows.extend(_tag_rows(sr))

    detail_grain_spec: dict[str, int] = {}
    for h in hierarchies:
        detail_grain_spec[h.hierarchy_name] = h.levels[-1].ordinal

    for row in detail_result.rows:
        r = dict(row)
        r[SUBTOTAL_LEVEL_KEY] = "detail"
        r[SUBTOTAL_GRAIN_KEY] = sum(
            h.levels[-1].ordinal for h in hierarchies
        )
        for hname, ordinal in detail_grain_spec.items():
            r[SUBTOTAL_GRAIN_PREFIX + hname] = ordinal
        all_rows.append(r)

    all_rows.sort(
        key=lambda r: _multi_hierarchy_sort_key(r, hierarchies),
    )

    all_hier_dims: list[str] = []
    hier_dim_set: set[str] = set()
    for h in hierarchies:
        for lvl in h.levels:
            if lvl.dim_name not in hier_dim_set:
                all_hier_dims.append(lvl.dim_name)
                hier_dim_set.add(lvl.dim_name)

    non_hier_dims: list[str] = []
    for c in detail_result.query.dim_cols:
        if c not in hier_dim_set:
            non_hier_dims.append(c)

    measure_cols: list[str] = []
    seen = set(non_hier_dims) | hier_dim_set
    for c in detail_result.columns:
        if c not in seen:
            measure_cols.append(c)

    columns = non_hier_dims + all_hier_dims + measure_cols
    return columns, all_rows


def _multi_hierarchy_sort_key(
    row: dict[str, Any], hierarchies: list[SubtotalHierarchy],
) -> tuple:
    parts: list = []
    for h in hierarchies:
        grain = row.get(SUBTOTAL_GRAIN_PREFIX + h.hierarchy_name, -2)
        for level in h.levels:
            val = str(row.get(level.dim_name) or "")
            parts.append(_sortable_val(val))
            parts.append(1 if grain > level.ordinal else 0)
    return tuple(parts)
