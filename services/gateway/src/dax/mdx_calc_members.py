"""
MDX calculated member evaluator.

Evaluates WITH MEMBER expressions using base measure values from query results.
Supports Excel's Show Values As patterns:
  - % of Grand Total, % of Column/Row Total, % of Parent
  - Difference From, % Difference From
  - Running Total
  - Rank (Smallest/Largest)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.dax.member_uname import KEY_PATH, parse_member_keys

logger = logging.getLogger(__name__)

# Bracket body that tolerates the SSAS ``]]`` escape (mirrors member_uname).
_CM_BRACKET_BODY = r"(?:[^\]]|\]\])"

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


def _member_sort_key(val: Any) -> tuple[int, float, str]:
    """Sort key for dimension member values: dates, months, quarters, numbers, strings."""
    s = str(val).strip() if val is not None else ""
    if not s:
        return (2, 0.0, "")
    for fmt in _DATE_FMTS:
        try:
            return (0, datetime.strptime(s, fmt).timestamp(), "")
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


@dataclass
class CalcMember:
    """A parsed calculated member ready for evaluation."""
    name: str
    expression: str
    format_string: str = ""
    base_measure: str = ""
    calc_type: str = "custom"
    ref_member_parts: list[str] = field(default_factory=list)
    # Bug-3619 (round-1 review F-P4b1-01 correction): ancestor-key constraints
    # for a composite-key reference member (``&[k0]&[k1]...&[kn]``). The key
    # path is ancestor-first, so ``kn`` is the named level's key and ``k0..kn-1``
    # belong to progressively shallower ANCESTOR levels.
    #
    # The member uname alone carries no ancestor LEVEL names, so the parser
    # cannot tag each ancestor key with its level here. Instead each entry is
    # ``(depth_above_named_level, key)`` where depth 1 is the immediate parent,
    # 2 its grandparent, and so on. ``_difference_ref_scope`` resolves each
    # ancestor key to the result column that sits ``depth`` positions above the
    # named-level column in ``dim_cols`` (the levels in their natural order),
    # so the ancestor filter scopes to the correct column rather than the
    # self-referential named level.
    ref_ancestor_filters: list[tuple[int, str]] = field(default_factory=list)
    solve_order: int = 0
    dim_name: str = ""
    aggregate_members: list[str] = field(default_factory=list)


def parse_calc_members(with_members: list) -> list[CalcMember]:
    """Parse WithMemberDef objects into CalcMember evaluation descriptors."""
    result: list[CalcMember] = []
    for wm in with_members:
        expr = wm.expression
        fmt = wm.properties.get("FORMAT_STRING", "")
        so_raw = wm.properties.get("SOLVE_ORDER", "0")
        try:
            solve_order = int(so_raw)
        except (ValueError, TypeError):
            solve_order = 0

        is_dim_member = not wm.name.startswith("[Measures]")
        if is_dim_member:
            dim_name, member_name = _extract_dim_member_name(wm.name)
            calc = CalcMember(
                name=member_name,
                expression=expr,
                format_string=fmt,
                solve_order=solve_order,
                dim_name=dim_name,
            )
            _classify_dim_expression(calc)
        else:
            name = _extract_member_name(wm.name)
            calc = CalcMember(
                name=name,
                expression=expr,
                format_string=fmt,
                solve_order=solve_order,
            )
            _classify_expression(calc)

        result.append(calc)

    return result


def _check_circular_references(calc_members: list[CalcMember]) -> list[CalcMember]:
    """Detect circular references and return members in dependency order.

    Raises ValueError if a cycle is found.
    """
    canon = {c.name.lower(): c.name for c in calc_members}
    name_set = set(canon.keys())
    deps: dict[str, set[str]] = {}
    for c in calc_members:
        refs: set[str] = set()
        for m in re.finditer(r'\[Measures\]\.\[([^\]]+)\]', c.expression):
            ref_lower = m.group(1).lower()
            if ref_lower in name_set:
                refs.add(ref_lower)
        deps[c.name.lower()] = refs

    # Topological sort with cycle detection
    UNVISITED, IN_STACK, DONE = 0, 1, 2
    state: dict[str, int] = {name: UNVISITED for name in name_set}
    order: list[str] = []

    def visit(name: str, path: list[str]) -> None:
        if state[name] == DONE:
            return
        if state[name] == IN_STACK:
            cycle_start = path.index(name)
            cycle = " -> ".join(canon[n] for n in path[cycle_start:] + [name])
            raise ValueError(
                f"Circular WITH MEMBER reference detected: {cycle}"
            )
        state[name] = IN_STACK
        path.append(name)
        for dep in deps.get(name, set()):
            visit(dep, path)
        path.pop()
        state[name] = DONE
        order.append(name)

    for name in name_set:
        if state[name] == UNVISITED:
            visit(name, [])

    by_name = {c.name.lower(): c for c in calc_members}
    topo_ordered = [by_name[n] for n in order if n in by_name]

    # Stable-sort by solve_order within topological equivalence groups.
    # Two members are in the same equivalence group if neither depends on
    # the other (directly or transitively). We use the topological position
    # as a depth proxy — members with no dependencies come first.
    depth: dict[str, int] = {}
    for n in order:
        d = 0
        for dep in deps.get(n, set()):
            d = max(d, depth.get(dep, 0) + 1)
        depth[n] = d

    topo_ordered.sort(key=lambda c: (depth.get(c.name.lower(), 0), c.solve_order))
    return topo_ordered


def evaluate_calc_members(
    calc_members: list[CalcMember],
    rows: list[dict[str, Any]],
    measure_cols: list[str],
    dim_cols: list[str],
    measures_meta: list[dict[str, Any]] | None = None,
    requery_results: dict[tuple, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Evaluate calculated members and inject their values into result rows.

    Returns the augmented rows with calculated member columns added.
    Dimension-level aggregate members add synthetic rows to the result.
    """
    if not calc_members:
        return rows

    calc_members = _check_circular_references(calc_members)

    # F-002-03 — grain partitioning.
    #
    # When subtotal/grand-total rows are present (tagged by the subtotal
    # engine), Show-Values-As measures must be evaluated over the DETAIL
    # (leaf) grain only. Evaluating "% of Grand Total" or Rank over the merged
    # set counts each amount up to three times (leaf + region subtotal + grand
    # total), so a 300/1000 leaf shows 10% instead of 30%. The detail rows
    # carry the leaf grain; subtotal and grand-total rows carry coarser grains
    # and must not pollute the denominator / peer set, nor receive a derived
    # value (Excel renders Show-Values-As on leaf cells, not on subtotal rows).
    from src.dax.subtotal_engine import SUBTOTAL_LEVEL_KEY

    has_subtotals = any(SUBTOTAL_LEVEL_KEY in r for r in rows)
    detail_rows = (
        [r for r in rows if r.get(SUBTOTAL_LEVEL_KEY, "detail") == "detail"]
        if has_subtotals else rows
    )
    non_detail_rows = (
        [r for r in rows if r.get(SUBTOTAL_LEVEL_KEY, "detail") != "detail"]
        if has_subtotals else []
    )

    # Show-Values-As types are grain-sensitive: their peer/denominator set is
    # the detail grain. Row-local types (arithmetic) and set-building
    # (aggregate_set) are unaffected by grain and run over every row.
    _GRAIN_SENSITIVE = {
        "pct_grand_total", "pct_parent", "index", "difference",
        "pct_difference", "running_total", "rank_asc", "rank_desc",
    }

    for calc in calc_members:
        if calc.calc_type == "aggregate_set":
            rows = _eval_aggregate_set(calc, rows, measure_cols, dim_cols, measures_meta, requery_results)
            # Re-derive the detail partition: aggregate_set may add rows.
            if has_subtotals:
                detail_rows = [
                    r for r in rows
                    if r.get(SUBTOTAL_LEVEL_KEY, "detail") == "detail"
                ]
                non_detail_rows = [
                    r for r in rows
                    if r.get(SUBTOTAL_LEVEL_KEY, "detail") != "detail"
                ]
            continue

        target_rows = detail_rows if calc.calc_type in _GRAIN_SENSITIVE else rows

        if calc.calc_type == "pct_grand_total":
            _eval_pct_grand_total(calc, target_rows)
        elif calc.calc_type == "pct_parent":
            _eval_pct_parent(calc, target_rows, dim_cols)
        elif calc.calc_type == "index":
            _eval_index(calc, target_rows)
        elif calc.calc_type == "difference":
            _eval_difference(calc, target_rows, dim_cols)
        elif calc.calc_type == "pct_difference":
            _eval_pct_difference(calc, target_rows, dim_cols)
        elif calc.calc_type == "running_total":
            _eval_running_total(calc, target_rows, dim_cols)
        elif calc.calc_type == "rank_asc":
            _eval_rank(calc, target_rows, dim_cols, ascending=True)
        elif calc.calc_type == "rank_desc":
            _eval_rank(calc, target_rows, dim_cols, ascending=False)
        elif calc.calc_type == "arithmetic":
            _eval_arithmetic(calc, rows)
        else:
            _eval_arithmetic(calc, rows)

        # Grain-sensitive calc columns are not defined on subtotal /
        # grand-total rows — leave them blank rather than carrying a value
        # computed over the wrong grain.
        if calc.calc_type in _GRAIN_SENSITIVE and non_detail_rows:
            for r in non_detail_rows:
                r.setdefault(calc.name, None)
                r[calc.name] = None

    return rows


def _extract_member_name(full_name: str) -> str:
    """Extract the measure name from [Measures].[Name]."""
    m = re.search(r'\[Measures\]\.\[([^\]]+)\]', full_name)
    return m.group(1) if m else full_name


def _extract_dim_member_name(full_name: str) -> tuple[str, str]:
    """Extract (dimension_name, member_name) from [Dim].[Member] or [Dim].[Hier].[Member]."""
    parts = re.findall(r'\[([^\]]+)\]', full_name)
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return "", full_name


def _classify_dim_expression(calc: CalcMember) -> None:
    """Classify a dimension-level WITH MEMBER expression."""
    expr = calc.expression.strip()
    agg_match = re.search(
        r'\bAggregate\s*\(\s*\{(.+?)\}\s*\)',
        expr, re.IGNORECASE | re.DOTALL,
    )
    if agg_match:
        member_list = agg_match.group(1)
        members = re.findall(r'\[([^\]]+)\](?:\.\[([^\]]+)\])*', member_list)
        parsed: list[str] = []
        for m in members:
            last_non_empty = [p for p in m if p]
            if last_non_empty:
                parsed.append(last_non_empty[-1])
        calc.calc_type = "aggregate_set"
        calc.aggregate_members = parsed
        return
    calc.calc_type = "custom"


def _classify_expression(calc: CalcMember) -> None:
    """Classify a WITH MEMBER expression into a known pattern."""
    expr = calc.expression.strip()

    base = _find_base_measure(expr)
    if base:
        calc.base_measure = base

    if _is_index(expr, base):
        calc.calc_type = "index"
        return

    if _is_pct_grand_total(expr, base):
        calc.calc_type = "pct_grand_total"
        return

    if _is_pct_parent(expr, base):
        calc.calc_type = "pct_parent"
        calc.ref_member_parts, calc.ref_ancestor_filters = _extract_parent_ref(expr)
        return

    rank_dir = _is_rank(expr, base)
    if rank_dir:
        calc.calc_type = rank_dir
        return

    if _is_running_total(expr, base):
        calc.calc_type = "running_total"
        return

    if _is_pct_difference(expr, base):
        calc.calc_type = "pct_difference"
        calc.ref_member_parts, calc.ref_ancestor_filters = _extract_difference_ref(expr)
        return

    if _is_difference(expr, base):
        calc.calc_type = "difference"
        calc.ref_member_parts, calc.ref_ancestor_filters = _extract_difference_ref(expr)
        return

    measures = re.findall(r'\[Measures\]\.\[([^\]]+)\]', expr)
    if measures:
        calc.calc_type = "arithmetic"
        return

    calc.calc_type = "custom"


def _find_base_measure(expr: str) -> str:
    """Find the primary measure referenced in an expression."""
    m = re.search(r'\[Measures\]\.\[([^\]]+)\]', expr)
    return m.group(1) if m else ""


def _is_pct_grand_total(expr: str, base: str) -> bool:
    """Check if expression is a % of Grand Total pattern."""
    if not base or "/" not in expr:
        return False
    if re.search(r'\[\(All\)\]', expr, re.IGNORECASE):
        return True
    if re.search(r'\.AllMembers', expr, re.IGNORECASE):
        return True
    parts = expr.split("/")
    if len(parts) == 2:
        left = parts[0].strip()
        right = parts[1].strip()
        if f"[{base}]" in left and f"[{base}]" in right:
            if re.search(r'\[\(All\)\]|\[All\]', right, re.IGNORECASE):
                return True
    return False


def _is_pct_parent(expr: str, base: str) -> bool:
    """Check if expression is a % of Parent pattern."""
    if not base or "/" not in expr:
        return False
    if re.search(r'\.Parent', expr, re.IGNORECASE):
        return True
    return False


def _extract_parent_ref(expr: str) -> tuple[list[str], list[tuple[int, str]]]:
    """Extract parent member reference from a % of Parent expression.

    Returns ``(parts, ancestor_filters)``. ``.Parent`` navigation carries no
    member key, so ``ancestor_filters`` is always empty here; the tuple shape
    matches :func:`_extract_difference_ref` (``(depth, key)`` entries) so the
    caller treats both uniformly.
    """
    m = re.search(r'\[([^\]]+)\]\.\[([^\]]+)\]\.CurrentMember\.Parent', expr, re.IGNORECASE)
    if m:
        return [m.group(1), m.group(2)], []
    m = re.search(r'\[([^\]]+)\]\.\[([^\]]+)\]\.Parent', expr, re.IGNORECASE)
    if m:
        return [m.group(1), m.group(2)], []
    return [], []


def _resolve_child_dim(ref_parts: list[str], dim_cols: list[str]) -> str:
    """Match ref_member_parts [dim_name, hierarchy_name] to a dim_col."""
    if not ref_parts or not dim_cols:
        return dim_cols[-1] if dim_cols else ""
    dim_lower = {d.lower(): d for d in dim_cols}
    for part in reversed(ref_parts):
        if part.lower() in dim_lower:
            return dim_lower[part.lower()]
    return dim_cols[-1]


def _is_difference(expr: str, base: str) -> bool:
    """Check if expression is a Difference From pattern."""
    if not base or "-" not in expr:
        return False
    parts = re.split(r'\s*-\s*', expr, maxsplit=1)
    if len(parts) == 2:
        if f"[{base}]" in parts[0] and f"[{base}]" in parts[1]:
            return True
    return False


def _is_pct_difference(expr: str, base: str) -> bool:
    """Check if expression is a % Difference From pattern."""
    if not base or "/" not in expr:
        return False
    if re.search(r'\(\s*\[Measures\].*-.*\[Measures\].*\)\s*/\s*\(?\s*\[Measures\]', expr):
        return True
    return False


def _extract_difference_ref(expr: str) -> tuple[list[str], list[tuple[int, str]]]:
    """Extract the reference member from a Difference expression.

    Returns ``(parts, ancestor_filters)``:

    - ``parts`` is ``[dim_resolution_name, hierarchy, member_value]`` where
      ``member_value`` is the value matched against ``dim_resolution_name``'s
      result column. For a composite-key reference
      (``[geo].[geo].[City].&[Germany]&[Berlin]``) the named level is ``City``
      and the value is the deepest key ``Berlin`` — NOT the level caption.
    - ``ancestor_filters`` carries each ancestor key as
      ``(depth_above_named_level, key)``. The composite key path is
      ancestor-first, so for ``&[Germany]&[Berlin]`` the deepest key
      ``Berlin`` is the named level and ``Germany`` is its immediate parent,
      i.e. ``(1, "Germany")``. The uname carries no ancestor level NAMES, so
      the level is resolved positionally at scope time against the result
      columns (Bug-3619, round-1 review F-P4b1-01 — previously every ancestor
      was mis-tagged with the NAMED level, making the constraint
      self-referential and silently dropped).

    Falls back to the legacy caption form ``[Dim].[Hier].[Member]`` when no
    composite key path is present.
    """
    b = _CM_BRACKET_BODY

    def _depth_tag(keys: list[str]) -> list[tuple[int, str]]:
        # keys are ancestor-first; the deepest key (keys[-1]) is the named
        # level. An ancestor at index i is (len(keys)-1-i) levels above the
        # named level.
        ancestor_keys = keys[:-1]
        n = len(keys)
        return [(n - 1 - i, k) for i, k in enumerate(ancestor_keys)]

    # (1) Explicit level + key path: [Dim].[Hier].[Level].&[k0]&[k1]...
    m = re.search(
        rf'\[({b}+)\]\.\[({b}+)\]\.\[({b}+)\]\.({KEY_PATH})', expr, re.IGNORECASE,
    )
    if m and m.group(1).lower() != "measures":
        keys = parse_member_keys(m.group(4))
        if keys:
            parts = [m.group(3), m.group(2), keys[-1]]
            return parts, _depth_tag(keys)

    # (2) Hierarchy + key path, no explicit level: [Dim].[Hier].&[k0]&[k1]...
    m = re.search(
        rf'\[({b}+)\]\.\[({b}+)\]\.({KEY_PATH})', expr, re.IGNORECASE,
    )
    if m and m.group(1).lower() != "measures":
        keys = parse_member_keys(m.group(3))
        if keys:
            # No explicit level name — match the deepest key against the
            # hierarchy's own result column.
            parts = [m.group(2), m.group(2), keys[-1]]
            return parts, _depth_tag(keys)

    # (3) Legacy caption form: [Dim].[Hier].[Member]
    for m in re.finditer(rf'\[({b}+)\]\.\[({b}+)\]\.\[({b}+)\]', expr):
        if m.group(1).lower() != "measures":
            return [m.group(1), m.group(2), m.group(3)], []
    return [], []


def _is_rank(expr: str, base: str) -> str:
    """Check if expression is a Rank pattern. Returns 'rank_asc', 'rank_desc', or ''."""
    if re.search(r'\bRank\b', expr, re.IGNORECASE):
        if re.search(r'\bASC\b|Smallest', expr, re.IGNORECASE):
            return "rank_asc"
        return "rank_desc"
    return ""


def _is_index(expr: str, base: str) -> bool:
    """Check if expression is an Index pattern (value / average * 100)."""
    if not base:
        return False
    if re.search(r'\bAverage\b.*\bDescendants\b', expr, re.IGNORECASE):
        return True
    if re.search(r'\bAvg\b.*\bDescendants\b', expr, re.IGNORECASE):
        return True
    return False


def _is_running_total(expr: str, base: str) -> bool:
    """Check if expression is a Running Total pattern."""
    if re.search(r'\bSum\b.*\bHead\b|\bRunning\b', expr, re.IGNORECASE):
        return True
    return False


def _extract_running_total_dim(expr: str, dim_cols: list[str]) -> str | None:
    """Extract the target dimension for a running total from the MDX expression.

    The MDX pattern is Sum(Head([Dim].[Hier].CurrentMember.Level.Members, ...)).
    Returns the matching dim_col name, or None if not found.
    """
    m = re.search(r'\[([^\]]+)\](?:\.\[([^\]]+)\])?\.CurrentMember', expr, re.IGNORECASE)
    if not m:
        return None
    candidates = [m.group(2), m.group(1)] if m.group(2) else [m.group(1)]
    dim_lower = {d.lower(): d for d in dim_cols}
    for c in candidates:
        if c and c.lower() in dim_lower:
            return dim_lower[c.lower()]
    return None


def _eval_pct_grand_total(calc: CalcMember, rows: list[dict]) -> None:
    """Evaluate % of Grand Total: cell / sum(all cells for same measure)."""
    base = calc.base_measure
    total = 0.0
    for r in rows:
        val = r.get(base)
        if val is not None:
            try:
                total += float(val)
            except (ValueError, TypeError):
                pass

    for r in rows:
        val = r.get(base)
        if val is not None and total != 0:
            try:
                r[calc.name] = float(val) / total
            except (ValueError, TypeError):
                r[calc.name] = None
        else:
            r[calc.name] = None


def _eval_pct_parent(
    calc: CalcMember,
    rows: list[dict],
    dim_cols: list[str],
) -> None:
    """Evaluate % of Parent: cell / parent member's value."""
    base = calc.base_measure
    if not dim_cols:
        _eval_pct_grand_total(calc, rows)
        return

    child_dim = _resolve_child_dim(calc.ref_member_parts, dim_cols)
    child_idx = dim_cols.index(child_dim) if child_dim in dim_cols else len(dim_cols) - 1
    parent_dims = [dc for i, dc in enumerate(dim_cols) if i != child_idx]

    parent_totals: dict[tuple, float] = {}
    for r in rows:
        parent_key = tuple(str(r.get(dc, "")) for dc in parent_dims) if parent_dims else ("__all__",)
        val = r.get(base)
        if val is not None:
            try:
                parent_totals[parent_key] = parent_totals.get(parent_key, 0.0) + float(val)
            except (ValueError, TypeError):
                pass

    for r in rows:
        parent_key = tuple(str(r.get(dc, "")) for dc in parent_dims) if parent_dims else ("__all__",)
        total = parent_totals.get(parent_key, 0.0)
        val = r.get(base)
        if val is not None and total != 0:
            try:
                r[calc.name] = float(val) / total
            except (ValueError, TypeError):
                r[calc.name] = None
        else:
            r[calc.name] = None


def _eval_index(calc: CalcMember, rows: list[dict]) -> None:
    """Evaluate Index: (cell / average_of_all_cells) * 100."""
    base = calc.base_measure
    total = 0.0
    count = 0
    for r in rows:
        val = r.get(base)
        if val is not None:
            try:
                total += float(val)
                count += 1
            except (ValueError, TypeError):
                pass

    avg = total / count if count > 0 else 0.0

    for r in rows:
        val = r.get(base)
        if val is not None and avg != 0:
            try:
                r[calc.name] = (float(val) / avg) * 100.0
            except (ValueError, TypeError):
                r[calc.name] = None
        else:
            r[calc.name] = None


def _eval_difference(
    calc: CalcMember,
    rows: list[dict],
    dim_cols: list[str],
) -> None:
    """Evaluate Difference From: cell - reference cell."""
    base = calc.base_measure
    ref = calc.ref_member_parts

    if not ref or len(ref) < 3:
        for r in rows:
            r[calc.name] = None
        return

    ref_dim, ref_member, scope_dims, ancestor_filters = _difference_ref_scope(
        calc, dim_cols,
    )

    def _peer_key(r: dict) -> tuple:
        return tuple(str(r.get(dc, "")) for dc in dim_cols if dc not in scope_dims)

    def _matches_ref(r: dict) -> bool:
        if str(r.get(ref_dim, "")) != ref_member:
            return False
        for a_dim, a_key in ancestor_filters:
            if str(r.get(a_dim, "")) != a_key:
                return False
        return True

    ref_val_map: dict[tuple, float] = {}
    for r in rows:
        if _matches_ref(r):
            val = r.get(base)
            if val is not None:
                try:
                    ref_val_map[_peer_key(r)] = float(val)
                except (ValueError, TypeError):
                    pass

    for r in rows:
        key = _peer_key(r)
        ref_val = ref_val_map.get(key)
        val = r.get(base)
        if val is not None and ref_val is not None:
            try:
                r[calc.name] = float(val) - ref_val
            except (ValueError, TypeError):
                r[calc.name] = None
        else:
            r[calc.name] = None


def _difference_ref_scope(
    calc: CalcMember,
    dim_cols: list[str],
) -> tuple[str, str, set[str], list[tuple[str, str]]]:
    """Resolve the reference scope for a Difference / % Difference member.

    Returns ``(ref_dim, ref_member, scope_dims, ancestor_filters)`` where:
    - ``ref_dim`` is the result column the named-level value is matched against,
    - ``ref_member`` is that value (the deepest key for composite refs),
    - ``scope_dims`` are the columns to EXCLUDE from the peer key (named dim +
      any ancestor dims present in ``dim_cols``),
    - ``ancestor_filters`` are the (column, value) ancestor constraints that
      actually map to a result column (Bug-3619 ancestor scoping).

    F-P4b1-01: ancestor keys arrive as ``(depth_above_named_level, key)``.
    The result columns ``dim_cols`` are the hierarchy levels in their natural
    (coarse→fine) order, so the column ``depth`` positions before the
    named-level column is the ancestor's level. Resolving positionally avoids
    the previous self-referential mis-tag where every ancestor was assigned the
    named level and then dropped by the ``a_col != ref_dim`` guard.
    """
    ref = calc.ref_member_parts
    ref_dim = _resolve_ref_dim_col(ref[0], dim_cols)
    ref_member = ref[2]
    scope_dims = {ref_dim}
    ancestor_filters: list[tuple[str, str]] = []
    named_idx = dim_cols.index(ref_dim) if ref_dim in dim_cols else -1
    for depth, a_key in calc.ref_ancestor_filters:
        a_col = ""
        if named_idx >= 0 and depth >= 1:
            anc_idx = named_idx - depth
            if 0 <= anc_idx < len(dim_cols):
                a_col = dim_cols[anc_idx]
        if a_col and a_col != ref_dim and a_col not in scope_dims:
            ancestor_filters.append((a_col, a_key))
            scope_dims.add(a_col)
    return ref_dim, ref_member, scope_dims, ancestor_filters


def _resolve_ref_dim_col(name: str, dim_cols: list[str]) -> str:
    """Map a reference dimension/level name to its result column (CI match)."""
    if name in dim_cols:
        return name
    lower_map = {d.lower(): d for d in dim_cols}
    return lower_map.get((name or "").lower(), name)


def _eval_pct_difference(
    calc: CalcMember,
    rows: list[dict],
    dim_cols: list[str],
) -> None:
    """Evaluate % Difference From: (cell - ref) / ref."""
    _eval_difference(calc, rows, dim_cols)
    base_name = calc.name
    ref_parts = calc.ref_member_parts
    if not ref_parts or len(ref_parts) < 3:
        return

    ref_dim, ref_member, scope_dims, ancestor_filters = _difference_ref_scope(
        calc, dim_cols,
    )

    def _peer_key(r: dict) -> tuple:
        return tuple(str(r.get(dc, "")) for dc in dim_cols if dc not in scope_dims)

    def _matches_ref(r: dict) -> bool:
        if str(r.get(ref_dim, "")) != ref_member:
            return False
        for a_dim, a_key in ancestor_filters:
            if str(r.get(a_dim, "")) != a_key:
                return False
        return True

    ref_val_map: dict[tuple, float] = {}
    for r in rows:
        if _matches_ref(r):
            v = r.get(calc.base_measure)
            if v is not None:
                try:
                    ref_val_map[_peer_key(r)] = float(v)
                except (ValueError, TypeError):
                    pass

    for r in rows:
        diff = r.get(base_name)
        if diff is not None:
            ref_val = ref_val_map.get(_peer_key(r))
            if ref_val and ref_val != 0:
                r[base_name] = diff / ref_val
            else:
                r[base_name] = None


def _eval_running_total(
    calc: CalcMember,
    rows: list[dict],
    dim_cols: list[str],
) -> None:
    """Evaluate Running Total: cumulative sum partitioned by non-target dims.

    The target dimension is extracted from the MDX expression's hierarchy
    reference. Rows are sorted by the target dimension within each partition
    before accumulation so that order is deterministic regardless of DB
    result ordering.
    """
    base = calc.base_measure
    target_dim = _extract_running_total_dim(calc.expression, dim_cols)
    partition_dims = [d for d in dim_cols if d != target_dim] if target_dim else []

    def _sort_key(r: dict) -> tuple:
        v = r.get(target_dim, "") if target_dim else ""
        return _member_sort_key(v)

    if not partition_dims:
        sorted_rows = sorted(rows, key=_sort_key)
        running = 0.0
        for r in sorted_rows:
            val = r.get(base)
            if val is not None:
                try:
                    running += float(val)
                except (ValueError, TypeError):
                    pass
            r[calc.name] = running
        return

    partitions: dict[tuple, list[dict]] = {}
    for r in rows:
        key = tuple(str(r.get(d, "")) for d in partition_dims)
        partitions.setdefault(key, []).append(r)

    for group in partitions.values():
        group.sort(key=_sort_key)
        running = 0.0
        for r in group:
            val = r.get(base)
            if val is not None:
                try:
                    running += float(val)
                except (ValueError, TypeError):
                    pass
            r[calc.name] = running


def _eval_rank(
    calc: CalcMember,
    rows: list[dict],
    dim_cols: list[str],
    ascending: bool = True,
) -> None:
    """Evaluate Rank: assign rank based on measure value, partitioned by non-target dims."""
    base = calc.base_measure
    target_dim = _extract_running_total_dim(calc.expression, dim_cols)
    partition_dims = [d for d in dim_cols if d != target_dim] if target_dim else []

    if not partition_dims:
        _rank_rows(calc.name, base, rows, ascending)
        return

    partitions: dict[tuple, list[dict]] = {}
    for r in rows:
        key = tuple(str(r.get(d, "")) for d in partition_dims)
        partitions.setdefault(key, []).append(r)

    for group in partitions.values():
        _rank_rows(calc.name, base, group, ascending)


def _rank_rows(name: str, base: str, rows: list[dict], ascending: bool) -> None:
    vals: list[tuple[int, float | None]] = []
    for i, r in enumerate(rows):
        v = r.get(base)
        try:
            vals.append((i, float(v) if v is not None else None))
        except (ValueError, TypeError):
            vals.append((i, None))

    sortable = [(i, v) for i, v in vals if v is not None]
    sortable.sort(key=lambda x: x[1], reverse=not ascending)

    # F-002-14: Excel's RANK assigns equal values the SAME rank — competition
    # ranking (1, 2, 2, 4), where the next distinct value skips the tied
    # positions. The previous sequential numbering gave tied rows arbitrary
    # distinct ranks that depended on input order.
    rank_map: dict[int, int] = {}
    prev_val: float | None = None
    rank = 0
    for position, (idx, v) in enumerate(sortable, start=1):
        if prev_val is None or v != prev_val:
            rank = position
            prev_val = v
        rank_map[idx] = rank

    for i, r in enumerate(rows):
        r[name] = rank_map.get(i)


def _eval_aggregate_set(
    calc: CalcMember,
    rows: list[dict],
    measure_cols: list[str],
    dim_cols: list[str],
    measures_meta: list[dict[str, Any]] | None = None,
    requery_results: dict[tuple, Any] | None = None,
) -> list[dict]:
    """Evaluate dimension-level Aggregate({member set}).

    Finds rows whose dimension value matches any member in the set,
    aggregates their measure values respecting each measure's default_agg,
    and appends a synthetic group row.
    """
    if not calc.aggregate_members or not calc.dim_name:
        return rows

    members_lower = {m.lower() for m in calc.aggregate_members}

    # Match target column by dim_name metadata first; fall back to value scan.
    target_dim = None
    dim_name_lower = calc.dim_name.lower()
    for dc in dim_cols:
        if dc.lower() == dim_name_lower:
            target_dim = dc
            break
    if target_dim is None:
        for dc in dim_cols:
            all_vals = {str(r.get(dc, "")).lower() for r in rows}
            if all_vals & members_lower:
                target_dim = dc
                break
    if target_dim is None:
        logger.warning(
            "Aggregate member set %r: no matching dimension column found in %r",
            calc.aggregate_members, dim_cols,
        )
        return rows

    matching = [
        r for r in rows
        if str(r.get(target_dim, "")).lower() in members_lower
    ]
    if not matching:
        return rows

    measure_agg: dict[str, str] = {}
    if measures_meta:
        for m in measures_meta:
            mname = m.get("name", "")
            if mname:
                measure_agg[mname.lower()] = (m.get("default_agg") or "sum").lower()

    # F-002-09: a custom group spans only the target dimension. When OTHER
    # dimensions are present on the pivot, the group must be aggregated SEPARATELY
    # within each other-dimension tuple, emitting one synthetic group row per
    # partition — not one row that folds every partition's facts together and
    # mislabels them with matching[0]'s other-dimension values. Mirror the
    # partitioning already used by _eval_rank / _eval_running_total.
    other_dims = [dc for dc in dim_cols if dc != target_dim]

    partitions: dict[tuple, list[dict]] = {}
    for r in matching:
        key = tuple(str(r.get(d, "")) for d in other_dims)
        partitions.setdefault(key, []).append(r)

    new_rows: list[dict] = []
    for part_key, part_rows in partitions.items():
        synth: dict[str, Any] = {}
        for dc in dim_cols:
            if dc == target_dim:
                synth[dc] = calc.name
            else:
                synth[dc] = part_rows[0].get(dc)

        for mc in measure_cols:
            agg = measure_agg.get(mc.lower(), "sum")
            vals: list[float] = []
            for r in part_rows:
                v = r.get(mc)
                if v is not None:
                    try:
                        vals.append(float(v))
                    except (ValueError, TypeError):
                        pass
            if not vals:
                synth[mc] = None
            elif agg in ("count_distinct", "avg"):
                # Non-composable: resolved via a per-partition re-query keyed by
                # the same other-dimension tuple the planner used.
                rq_val = (requery_results or {}).get((calc.name, mc, part_key))
                if rq_val is None:
                    rq_val = (requery_results or {}).get((calc.name, mc, ()))
                synth[mc] = rq_val
            elif agg == "max":
                synth[mc] = max(vals)
            elif agg == "min":
                synth[mc] = min(vals)
            else:
                synth[mc] = sum(vals)
        new_rows.append(synth)

    rows.extend(new_rows)
    return rows


@dataclass
class ReQuerySpec:
    """Specification for a re-query to resolve non-composable aggregations."""
    calc_name: str
    measure_name: str
    agg: str
    dim_col: str
    members: list[str]
    model_slug: str
    extra_where: list[str] = field(default_factory=list)
    # F-002-09: when other dimensions are on the pivot, a custom group is
    # aggregated per other-dimension partition; the re-query for a non-composable
    # aggregation (avg/count_distinct) must be scoped to that partition. The key
    # is the tuple of other-dimension VALUES (matching _eval_aggregate_set's
    # partition key, in `partition_dims` order); the filters pin them in SQL.
    partition_key: tuple = field(default_factory=tuple)
    partition_dims: list[str] = field(default_factory=list)
    partition_values: list[str] = field(default_factory=list)


def plan_aggregate_requeried(
    calc_members: list[CalcMember],
    measures_meta: list[dict[str, Any]] | None,
    model_slug: str,
    dim_cols: list[str],
    rows: list[dict[str, Any]],
    queried_measures: set[str] | None = None,
) -> list[ReQuerySpec]:
    """Identify aggregate_set calc members that need re-queries for non-composable aggregations."""
    if not measures_meta:
        return []

    measure_agg: dict[str, str] = {}
    measure_names: set[str] = set()
    for m in measures_meta:
        mname = m.get("name", "")
        if mname:
            measure_agg[mname.lower()] = (m.get("default_agg") or "sum").lower()
            measure_names.add(mname)

    specs: list[ReQuerySpec] = []
    for calc in calc_members:
        if calc.calc_type != "aggregate_set" or not calc.aggregate_members or not calc.dim_name:
            continue

        target_dim = None
        dim_name_lower = calc.dim_name.lower()
        for dc in dim_cols:
            if dc.lower() == dim_name_lower:
                target_dim = dc
                break
        if target_dim is None:
            members_lower = {m.lower() for m in calc.aggregate_members}
            for dc in dim_cols:
                all_vals = {str(r.get(dc, "")).lower() for r in rows}
                if all_vals & members_lower:
                    target_dim = dc
                    break
        if target_dim is None:
            continue

        target_measures = measure_names
        if queried_measures is not None:
            target_measures = measure_names & queried_measures

        # F-002-09: one re-query per other-dimension partition so a
        # non-composable group value is scoped to its partition, not computed
        # once across all of them. Partition over the rows that actually match
        # the group's members (same key as _eval_aggregate_set).
        members_lower = {m.lower() for m in calc.aggregate_members}
        other_dims = [dc for dc in dim_cols if dc != target_dim]
        matching = [
            r for r in rows
            if str(r.get(target_dim, "")).lower() in members_lower
        ]
        seen_partitions: dict[tuple, list[str]] = {}
        for r in matching:
            key = tuple(str(r.get(d, "")) for d in other_dims)
            if key not in seen_partitions:
                seen_partitions[key] = [str(r.get(d, "")) for d in other_dims]
        if not seen_partitions:
            seen_partitions[tuple()] = []

        for mname in target_measures:
            agg = measure_agg.get(mname.lower(), "sum")
            if agg in ("count_distinct", "avg"):
                for part_key, part_values in seen_partitions.items():
                    specs.append(ReQuerySpec(
                        calc_name=calc.name,
                        measure_name=mname,
                        agg=agg,
                        dim_col=target_dim,
                        members=list(calc.aggregate_members),
                        model_slug=model_slug,
                        partition_key=part_key,
                        partition_dims=list(other_dims),
                        partition_values=part_values,
                    ))

    return specs


def build_requery_sql(spec: ReQuerySpec) -> str:
    """Build the SQL for a single re-query spec."""
    def _q(name: str) -> str:
        return f'"{name}"'

    if spec.agg == "count_distinct":
        agg_expr = f"COUNT(DISTINCT {_q(spec.measure_name)})"
    else:
        agg_expr = f"AVG({_q(spec.measure_name)})"

    escaped_members = [m.replace("'", "''") for m in spec.members]
    in_list = ", ".join(f"'{m}'" for m in escaped_members)
    where_parts = [f"{_q(spec.dim_col)} IN ({in_list})"]
    # F-002-09: pin the other-dimension partition so the re-query value belongs
    # to exactly the synthetic group row that consumes it.
    for dim, val in zip(spec.partition_dims, spec.partition_values):
        where_parts.append(f"{_q(dim)} = '{val.replace(chr(39), chr(39) * 2)}'")
    if spec.extra_where:
        where_parts.extend(spec.extra_where)
    return (
        f"SELECT {agg_expr} AS {_q(spec.measure_name)}"
        f" FROM {_q(spec.model_slug)}"
        f" WHERE {' AND '.join(where_parts)}"
    )


def _eval_arithmetic(calc: CalcMember, rows: list[dict]) -> None:
    """Evaluate an arithmetic WITH MEMBER expression.

    Supports: measure references, numeric literals, +, -, *, /,
    IIF(condition, true_val, false_val), and parenthesised sub-expressions.
    """
    expr = calc.expression.strip()
    compiled = _compile_expression(expr)
    for r in rows:
        try:
            r[calc.name] = compiled(r)
        except Exception:
            r[calc.name] = None


def _is_balanced_outer_parens(expr: str) -> bool:
    """True only if the first '(' is matched by the final ')'."""
    depth = 0
    for i, ch in enumerate(expr):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if depth == 0 and i < len(expr) - 1:
            return False
    return depth == 0


def _compile_expression(expr: str):
    """Compile an MDX calc expression into a callable that takes a row dict."""
    expr = expr.strip()

    iif_match = re.match(
        r'^IIF\s*\((.+)\)\s*$', expr, re.IGNORECASE | re.DOTALL,
    )
    if iif_match:
        inner = iif_match.group(1)
        parts = _split_iif_args(inner)
        if len(parts) == 3:
            cond_fn = _compile_condition(parts[0].strip())
            true_fn = _compile_expression(parts[1].strip())
            false_fn = _compile_expression(parts[2].strip())
            return lambda r, c=cond_fn, t=true_fn, f=false_fn: t(r) if c(r) else f(r)

    if expr.startswith('(') and expr.endswith(')') and _is_balanced_outer_parens(expr):
        return _compile_expression(expr[1:-1].strip())

    tokens = _tokenize_arithmetic(expr)
    if len(tokens) == 1:
        return _compile_atom(tokens[0])

    return _compile_infix(tokens)


def _compile_atom(token: str):
    """Compile a single token into a callable."""
    token = token.strip()
    m = re.match(r'\[Measures\]\.\[([^\]]+)\]', token)
    if m:
        name = m.group(1)
        return lambda r, n=name: _to_float(r.get(n))

    try:
        val = float(token)
        return lambda r, v=val: v
    except ValueError:
        pass

    if re.match(r'^IIF\s*\(', token, re.IGNORECASE):
        return _compile_expression(token)

    if token.startswith('('):
        return _compile_expression(token)

    return lambda r: None


def _to_float(val):
    """Convert a value to float, returning None for unconvertible values."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _tokenize_arithmetic(expr: str) -> list[str]:
    """Split an expression into tokens preserving measure refs and parens."""
    tokens: list[str] = []
    current = ""
    i = 0
    depth = 0

    while i < len(expr):
        ch = expr[i]
        if ch == '(' and not (current.rstrip().upper().endswith("IIF")):
            depth += 1
            current += ch
        elif ch == '(':
            depth += 1
            current += ch
        elif ch == ')':
            depth -= 1
            current += ch
            if depth == 0 and current.strip():
                tokens.append(current.strip())
                current = ""
        elif depth > 0:
            current += ch
        elif ch in "+-" and not current.strip().endswith(("(", ",", "<", ">", "=")):
            _OPS = ("+", "-", "*", "/", "<", ">", "<=", ">=", "<>", "=", ",")
            is_binary = bool(current.strip())
            if not is_binary and tokens and tokens[-1] not in _OPS:
                is_binary = True
            if is_binary:
                if current.strip():
                    tokens.append(current.strip())
                tokens.append(ch)
                current = ""
            else:
                current += ch
        elif ch in "*/":
            if current.strip():
                tokens.append(current.strip())
            tokens.append(ch)
            current = ""
        elif ch == '<' and i + 1 < len(expr) and expr[i+1] in ('>', '='):
            if current.strip():
                tokens.append(current.strip())
            tokens.append(expr[i:i+2])
            current = ""
            i += 1
        elif ch in ('<', '>'):
            if current.strip():
                tokens.append(current.strip())
            tokens.append(ch)
            current = ""
        else:
            current += ch
        i += 1

    if current.strip():
        tokens.append(current.strip())

    return tokens


def _compile_infix(tokens: list[str]):
    """Compile a list of tokens with infix operators into a callable."""
    if len(tokens) == 1:
        return _compile_atom(tokens[0])

    for op in ("+", "-"):
        for i in range(len(tokens) - 1, 0, -1):
            if tokens[i] == op:
                left = _compile_infix(tokens[:i])
                right = _compile_infix(tokens[i+1:])
                if op == "+":
                    return lambda r, l=left, ri=right: _safe_op(l(r), ri(r), "+")
                else:
                    return lambda r, l=left, ri=right: _safe_op(l(r), ri(r), "-")

    for op in ("*", "/"):
        for i in range(len(tokens) - 1, 0, -1):
            if tokens[i] == op:
                left = _compile_infix(tokens[:i])
                right = _compile_infix(tokens[i+1:])
                if op == "*":
                    return lambda r, l=left, ri=right: _safe_op(l(r), ri(r), "*")
                else:
                    return lambda r, l=left, ri=right: _safe_op(l(r), ri(r), "/")

    return _compile_atom(" ".join(tokens))


def _safe_op(a, b, op: str):
    """Perform an arithmetic operation with NULL propagation."""
    if a is None or b is None:
        return None
    if op == "+":
        return a + b
    elif op == "-":
        return a - b
    elif op == "*":
        return a * b
    elif op == "/":
        return a / b if b != 0 else None
    return None


def _compile_condition(cond: str):
    """Compile a simple comparison condition."""
    for op in (">=", "<=", "<>", ">", "<", "="):
        if op in cond:
            parts = cond.split(op, 1)
            left_fn = _compile_expression(parts[0].strip())
            right_fn = _compile_expression(parts[1].strip())
            if op == ">":
                return lambda r, l=left_fn, ri=right_fn: (l(r) or 0) > (ri(r) or 0)
            elif op == "<":
                return lambda r, l=left_fn, ri=right_fn: (l(r) or 0) < (ri(r) or 0)
            elif op == ">=":
                return lambda r, l=left_fn, ri=right_fn: (l(r) or 0) >= (ri(r) or 0)
            elif op == "<=":
                return lambda r, l=left_fn, ri=right_fn: (l(r) or 0) <= (ri(r) or 0)
            elif op == "=":
                return lambda r, l=left_fn, ri=right_fn: (l(r) or 0) == (ri(r) or 0)
            elif op == "<>":
                return lambda r, l=left_fn, ri=right_fn: (l(r) or 0) != (ri(r) or 0)
    return lambda r: True


def _split_iif_args(inner: str) -> list[str]:
    """Split IIF arguments respecting parenthesised sub-expressions."""
    args: list[str] = []
    current = ""
    depth = 0
    for ch in inner:
        if ch == '(':
            depth += 1
            current += ch
        elif ch == ')':
            depth -= 1
            current += ch
        elif ch == ',' and depth == 0:
            args.append(current)
            current = ""
        else:
            current += ch
    if current:
        args.append(current)
    return args
