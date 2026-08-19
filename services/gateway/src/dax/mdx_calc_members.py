"""
MDX calculated member evaluator.

Evaluates WITH MEMBER expressions using base measure values from query results.
Supports Excel's Show Values As patterns:
  - % of Grand Total, % of Parent
  - % of Row Total, % of Column Total (Bug-8206)
  - Difference From, % Difference From (absolute or relative
    PrevMember/NextMember/Lag/Lead reference member)
  - Running Total
  - Rank (Smallest/Largest)
  - Index (value vs the average of the displayed members)

% of Row / Column Total (Bug-8206) hold the FIXED axis dims constant and total
the base measure over the OTHER axis's members — % of Parent generalised to an
arbitrary fixed-dim subset. The row/col axis split is threaded in from the caller
so the evaluator knows which dims are pinned versus summed over; when the split
is unknown the cells are blanked (fail closed), never mis-computed.

For a NON-ADDITIVE base measure (avg / count_distinct) the % of Grand Total /
Parent / Row Total / Column Total denominator is the measure re-aggregated at the
wider grain (a router re-query), never the sum of the already-aggregated leaf
cells, which would be a sum-of-averages and mathematically wrong (F-002-04).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from shared.connector_qualify import quote_identifier, quote_literal
from src.dax.member_uname import KEY_PATH, parse_member_keys

logger = logging.getLogger(__name__)

# F-002-01 (Bug-6065): marker key stamped on synthetic rows produced by an
# ``Aggregate({...})`` custom-group calc member. These rows are summaries, not
# detail peers, so every grain-sensitive Show-Values-As evaluator must exclude
# them from its denominator / peer / ranking set (otherwise the group's total
# is double-counted, e.g. a 100/1000 leaf shows 100/1300). The group row still
# receives its OWN percentage/index, computed against the clean peer set.
CALC_GROUP_ROW_KEY = "_calc_group_row"

# F-002-10 / R1 finding 2: SSAS renders a fact whose dimension member is
# NULL/empty under a stable "(blank)" member. ``build_real_execute_response``
# (mdx_execute) rewrites NULL/"" dim values to this same string in place BEFORE
# the Show-Values-As evaluators run, so a denominator re-query planned from the
# RAW router rows must normalise its partition keys the same way — otherwise the
# producer key ("None"/"") never matches the consumer key ("(blank)") and the
# non-additive % of Parent silently falls back to the wrong sum-of-cells for any
# NULL/empty parent. Defined here (not imported from mdx_execute) because
# mdx_execute imports THIS module — importing back would be circular.
BLANK_MEMBER = "(blank)"


def _normalize_member_value(val: Any) -> str:
    """Normalise a raw router dimension value to its axis member string.

    The router delivers SQL NULL as Python ``None`` (JSON null); an empty string
    is likewise a blank member. Both map to :data:`BLANK_MEMBER` so re-query
    partition keys align with the normalised rows the evaluator sees. Only the
    real NULL representations are mapped — a genuine ``"None"`` string value is a
    distinct member and is left as-is (R2 finding 3: mapping it would desync the
    planner key from the evaluator key, which does not map it).
    """
    if val is None:
        return BLANK_MEMBER
    s = str(val)
    if s == "":
        return BLANK_MEMBER
    return s

# Bracket body that tolerates the SSAS ``]]`` escape (mirrors member_uname).
_CM_BRACKET_BODY = r"(?:[^\]]|\]\])"


def _escape_cm(name: str) -> str:
    """Escape ``]`` -> ``]]`` for a bracket body (Bug-6746).

    Used when a raw name is put back inside ``[...]`` for a substring test
    against the (still-escaped) MDX expression.
    """
    return name.replace("]", "]]")

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


def _axis_order_key_fn(rows: list[dict], dim: str):
    """Return a stable sort-key function that orders dimension members by their
    REAL axis order, never by caption alphabetical.

    MDX relative navigation (PrevMember / NextMember / Lag / Lead) and running
    totals are position-sensitive: "previous member" means the member that sits
    immediately before the current one in the hierarchy's own order — the order
    the query-router already returned the rows in. ``_member_sort_key`` gives a
    correct SEMANTIC order for recognised dates / months / quarters / numbers,
    but for any unrecognised label it falls back to alphabetical caption order,
    which silently reorders arbitrary text members (e.g. product names, custom
    sort orders) and computes the difference / cumulative sum against the WRONG
    peer — a silent-wrong-numbers defect.

    This composite key keeps the semantic ordering for recognised values and,
    for everything else, breaks ties by the member's first-appearance ordinal in
    ``rows`` (the real axis order) instead of the caption string.
    """
    ordinal: dict[str, int] = {}
    for r in rows:
        mv = str(r.get(dim, "")) if dim else ""
        if mv not in ordinal:
            ordinal[mv] = len(ordinal)

    def _key(r: dict) -> tuple:
        mv = str(r.get(dim, "")) if dim else ""
        cat, val, _ = _member_sort_key(mv)
        return (cat, val, ordinal.get(mv, len(ordinal)))

    return _key


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
    # Bug-6067: a RELATIVE reference member (PrevMember / NextMember / Lag(n) /
    # Lead(n)) carries no fixed member key, so the absolute-reference resolver
    # (``_extract_difference_ref``) returns nothing and the column renders all
    # blank. A non-empty ``ref_offset_dim`` marks that a relative navigation was
    # detected; the difference is evaluated against the member ``ref_offset``
    # positions away (negative = backward) along the ordered members of
    # ``ref_offset_dim``, partitioned by the other dims. ``ref_offset`` may
    # legitimately be 0 (Lag(0)/Lead(0) = self -> zero difference), so routing
    # keys off ``ref_offset_dim``, never off ``ref_offset`` truthiness.
    ref_offset: int = 0
    ref_offset_dim: str = ""
    solve_order: int = 0
    dim_name: str = ""
    aggregate_members: list[str] = field(default_factory=list)
    # F-002-07 (adversarial R3 F1): for a pct_grand_total, the set of hierarchy /
    # dimension names the denominator pins to its (All) member. Excel emits a TRUE
    # grand total by pinning EVERY queried hierarchy to (All); a "% of Row Total" /
    # "% of Column Total" pins only a PROPER SUBSET (one axis), which is a
    # different quantity. The classifier cannot see the axis layout, so it records
    # the pinned names here and the evaluator (which knows dim_cols) rejects the
    # subset case as the unsupported axis-total shape instead of silently
    # computing a grand-total ratio. Empty means "no explicit hierarchy pin"
    # (e.g. the ``.AllMembers`` form), which stays a grand total.
    grand_total_all_dims: list[str] = field(default_factory=list)
    # Bug-8206: for a ``pct_row_total`` / ``pct_col_total`` member the set of
    # hierarchy / dimension names the denominator's axis set names explicitly
    # (the ``.CurrentMember ... .Members`` form). Empty for the ``Axis(0)`` /
    # ``Axis(1)`` form, where the axis is identified positionally instead
    # (``axis_total_axis``). The evaluator maps these names to the pivot's
    # row-axis / column-axis dim_cols to decide which dims are held fixed (the
    # "pinned" partition) versus summed over (the axis being totalled). This is a
    # generalised % of Parent: the denominator is the base measure summed over the
    # peers that share the partition-dim values, re-queried at grain when the base
    # measure is non-additive.
    axis_total_denom_dims: list[str] = field(default_factory=list)
    # Bug-8206: 0 = Axis(0)/columns referenced in the denominator (a ROW total,
    # summed across columns), 1 = Axis(1)/rows (a COLUMN total). -1 when the shape
    # named its axis by hierarchy (``axis_total_denom_dims``) rather than Axis(n).
    axis_total_axis: int = -1


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
        for m in re.finditer(rf'\[Measures\]\.\[({_CM_BRACKET_BODY}+)\]', c.expression):
            # Bug-6746: unescape ]] before comparing to raw calc-member names.
            ref_lower = m.group(1).replace("]]", "]").lower()
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
        # F-002-01 (Bug-6065): iterate dependencies in a stable (sorted) order
        # so the topological result is deterministic across processes — a Python
        # ``set`` iterates in hash order, which varies between runs.
        for dep in sorted(deps.get(name, set())):
            visit(dep, path)
        path.pop()
        state[name] = DONE
        order.append(name)

    # F-002-01 (Bug-6065): visit roots in a stable (sorted) order. Iterating the
    # unordered ``name_set`` made the evaluation order — and therefore
    # grain-sensitive numbers for members with equal solve_order — depend on set
    # iteration order and differ between gateway restarts.
    for name in sorted(name_set):
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

    # F-002-01 (Bug-6065): tie-break equal (depth, solve_order) groups by name
    # so members with the same solve order evaluate in a fixed, reproducible
    # order rather than an arbitrary set-derived one.
    topo_ordered.sort(
        key=lambda c: (depth.get(c.name.lower(), 0), c.solve_order, c.name.lower())
    )
    return topo_ordered


def evaluate_calc_members(
    calc_members: list[CalcMember],
    rows: list[dict[str, Any]],
    measure_cols: list[str],
    dim_cols: list[str],
    measures_meta: list[dict[str, Any]] | None = None,
    requery_results: dict[tuple, Any] | None = None,
    denom_requery_results: dict[tuple, Any] | None = None,
    hierarchy_level_dims: dict[str, list[str]] | None = None,
    row_axis_dims: list[str] | None = None,
    col_axis_dims: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Evaluate calculated members and inject their values into result rows.

    Returns the augmented rows with calculated member columns added.
    Dimension-level aggregate members add synthetic rows to the result.

    ``row_axis_dims`` / ``col_axis_dims`` (Bug-8206) are the dim_col subsets on
    the pivot's ROW axis and COLUMN axis. A ``% of Row Total`` holds the row-axis
    dims fixed and totals the base measure over the column-axis members; a
    ``% of Column Total`` holds the column-axis dims fixed. When the split is
    unknown (both None) an axis-total member cannot be evaluated correctly and its
    cells are blanked (fail closed) rather than mis-computed.

    ``denom_requery_results`` (F-002-04) carries re-aggregated denominators for
    Show-Values-As ratio members (% of Grand Total, % of Parent) whose base
    measure is NON-ADDITIVE (avg / count_distinct). Summing the already-averaged
    leaf cells is mathematically wrong for those measures, so the true
    grand/parent total is re-queried at the wider grain and injected here. Keyed
    by ``(calc.name, "__grand__")`` for a grand total and
    ``(calc.name, parent_key_tuple)`` for each parent total.
    """
    if not calc_members:
        return rows

    # Bug-8381 sibling (deep-review R1 finding 3): every ``_eval_*`` below writes
    # ``r[calc.name] = value`` on each row, and ``calc.name`` is chosen by the
    # CLIENT. If it matches a dimension column the router returned, that column
    # is destroyed on every row — and because
    # ``mdx_execute.build_real_execute_response`` derives its axis member list
    # from the rows AFTER this call, the pivot renders the calculated numbers in
    # place of the dimension's members. Same class as the constant-writer
    # collision Bug-8381 guards in ``xmla_server._handle_execute``; there is no
    # correct value to put in one column for both, so refuse rather than
    # overwrite. The caller turns this ValueError into a SOAP Client fault.
    # ``aggregate_set`` is exempt: it writes its name as a MEMBER VALUE into the
    # target dimension column of a synthetic row, never as a column key.
    #
    # The comparison is deliberately case-INSENSITIVE while the write itself is
    # case-sensitive, so a member named ``product`` beside a ``Product`` column is
    # refused even though it would only add a new key. That is intentional: SQL
    # identifiers fold case, the router may return either spelling for the same
    # source column, and a client that has to guess which casing is safe has no
    # good answer. Refusing the ambiguous pair matches the case-insensitive
    # constant-writer guard in ``xmla_server._handle_execute`` (Bug-8381).
    _existing_lower = {
        str(c).strip().lower(): str(c)
        for c in list(dim_cols or []) + list(measure_cols or [])
        if str(c).strip()
    }
    for _cm in calc_members:
        if _cm.calc_type == "aggregate_set":
            continue
        _clash = _existing_lower.get(str(_cm.name).strip().lower())
        if _clash is not None:
            raise ValueError(
                f"Calculated member '{_cm.name}' collides with the column "
                f"'{_clash}' this query already returns (a dimension or a real "
                "measure of the same name). The member's values would overwrite "
                "that column on every row. Rename the calculated member, or "
                "query them separately."
            )

    calc_members = _check_circular_references(calc_members)

    # An ``aggregate_set`` custom group PRODUCES a row; every other calc type
    # CONSUMES rows. ``_check_circular_references`` orders by (depth,
    # solve_order, NAME) — a name tie-break — so whether the custom-group row
    # exists before another member is evaluated came down to alphabetical
    # order: a member named "Doubled" ran before the group "Grp" and its column
    # was never written on the group row, rendering a BLANK cell in Excel where a
    # real number belongs, while renaming the member to "ZDoubled" produced the
    # correct value. Row producers must run first; this is a stable partition, so
    # the topological and solve_order relationships inside each group are
    # untouched.
    calc_members = (
        [c for c in calc_members if c.calc_type == "aggregate_set"]
        + [c for c in calc_members if c.calc_type != "aggregate_set"]
    )

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
    from src.dax.subtotal_engine import (
        SUBTOTAL_LEVEL_KEY,
        select_detail_rows,
        select_non_detail_rows,
    )

    has_subtotals = any(SUBTOTAL_LEVEL_KEY in r for r in rows)
    detail_rows = select_detail_rows(rows) if has_subtotals else rows
    non_detail_rows = select_non_detail_rows(rows) if has_subtotals else []

    # Show-Values-As types are grain-sensitive: their peer/denominator set is
    # the detail grain. Row-local types (arithmetic) and set-building
    # (aggregate_set) are unaffected by grain and run over every row.
    _GRAIN_SENSITIVE = {
        "pct_grand_total", "pct_parent", "index", "difference",
        "pct_difference", "running_total", "rank_asc", "rank_desc",
        "pct_row_total", "pct_col_total", "pct_axis_total",
    }

    # F-002-04: map base-measure name -> default_agg so a ratio denominator over a
    # non-additive measure (avg / count_distinct) uses the re-aggregated value
    # from ``denom_requery_results`` instead of the wrong sum-of-cells.
    _agg_by_measure: dict[str, str] = {}
    if measures_meta:
        for _m in measures_meta:
            _mn = _m.get("name", "")
            if _mn:
                _agg_by_measure[_mn.lower()] = (_m.get("default_agg") or "sum").lower()

    # Bug-7857: only SUM and COUNT are truly additive (total = sum of cells).
    # Every other aggregation (avg, count_distinct, min, max, percentile/pNN,
    # median, stddev, etc.) requires re-aggregation at the wider grain for
    # a correct % of total denominator. The semi-additive semantic tags
    # (LAST_NON_EMPTY, FIRST_NON_EMPTY, BY_ACCOUNT, AVG_OF_CHILDREN) resolve
    # to SUM at the SQL level, so they pass the additive test.
    _ADDITIVE_AGGS = frozenset({"sum", "count"})

    def _is_nonadditive(base: str) -> bool:
        agg = _agg_by_measure.get((base or "").lower(), "sum")
        return agg not in _ADDITIVE_AGGS

    _dim_cols_lower = {d.lower() for d in dim_cols}
    # F-002-07 (adversarial R3 F1/F2): map every pinned name (a DIMENSION or a
    # HIERARCHY name) to the set of result dim_cols it covers. A defined
    # hierarchy pins ALL of its level columns at once, so comparing a pinned
    # HIERARCHY name directly against LEVEL dim_cols would both false-negative
    # (miss a real row total) and false-positive (fault a true grand total on a
    # hierarchy pivot). ``hierarchy_level_dims`` (hierarchy/dim name -> its level
    # dim_col names) expands the pin into the correct namespace before comparison.
    _hld_lower: dict[str, set[str]] = {}
    for _hname, _lvls in (hierarchy_level_dims or {}).items():
        _cov = {lv.lower() for lv in _lvls if lv.lower() in _dim_cols_lower}
        if _cov:
            _hld_lower[_hname.lower()] = _cov

    def _pinned_dim_cols(pinned_names: list[str]) -> tuple[set[str], bool]:
        """Expand pinned dim/hierarchy names to the dim_cols they cover.

        Returns ``(covered, all_resolved)``. ``all_resolved`` is False when any
        pinned name maps to NO dim_col (neither a direct dim_col nor a known
        hierarchy) — in that case we cannot safely judge the subset, so the guard
        stays silent (never false-fault a query we don't fully understand).
        """
        covered: set[str] = set()
        all_resolved = True
        for nm in pinned_names:
            low = nm.lower()
            # R5 finding: UNION both resolutions rather than short-circuiting on a
            # direct dim_col match. A hierarchy named identically to one of its own
            # level columns (a common BI pattern, e.g. a "Product" hierarchy whose
            # leaf level is also "Product") would otherwise resolve only to that
            # one level and false-fault a legitimate multi-level grand total. A
            # pin resolves if it matches EITHER a dim_col OR a known hierarchy;
            # both contribute their coverage.
            matched = False
            if low in _dim_cols_lower:
                covered.add(low)
                matched = True
            if low in _hld_lower:
                covered |= _hld_lower[low]
                matched = True
            if not matched:
                all_resolved = False
        return covered, all_resolved

    def _raise_axis_total_unsupported(name: str) -> None:
        raise ValueError(
            f"Show Values As '% of Column/Row Total' (member '{name}') "
            "is not supported by this server. Use % of Grand Total or "
            "% of Parent Total, or compute the column/row total in the client."
        )

    for calc in calc_members:
        # F-002-07 (adversarial R3 F1): a pct_grand_total whose denominator pins
        # only a PROPER SUBSET of the pivot's dimensions to the All member is
        # actually a "% of Row Total" / "% of Column Total" (Excel pins EVERY
        # hierarchy for a true grand total). Computing cell/sum(all-cells) for it
        # is a silent wrong number, so reject it with the same clear fault.
        # Fires ONLY when: the pin set is known, >1 dim is on the pivot, EVERY
        # pinned name resolved to a real dim_col/hierarchy (so we are sure this is
        # not just a name we failed to map), the pins cover some but NOT all
        # dim_cols. This is conservative by construction — an unresolved pin or a
        # full-coverage pin never faults a legitimate grand total.
        if calc.calc_type == "pct_grand_total" and calc.grand_total_all_dims and len(dim_cols) > 1:
            covered, all_resolved = _pinned_dim_cols(calc.grand_total_all_dims)
            if all_resolved and covered and (_dim_cols_lower - covered):
                _raise_axis_total_unsupported(calc.name)
        if calc.calc_type == "aggregate_set":
            rows = _eval_aggregate_set(calc, rows, measure_cols, dim_cols, measures_meta, requery_results)
            # Re-derive the detail partition: aggregate_set may add rows.
            if has_subtotals:
                detail_rows = select_detail_rows(rows)
                non_detail_rows = select_non_detail_rows(rows)
            continue

        target_rows = detail_rows if calc.calc_type in _GRAIN_SENSITIVE else rows

        # F-002-01 (Bug-6065): a custom-group (Aggregate set) synthetic row is a
        # summary, not a detail peer. For grain-sensitive measures the reference
        # aggregate (grand total, parent total, average, rank/running sequence,
        # difference reference) must be computed over the DETAIL peers only,
        # excluding group rows — otherwise the group total is double-counted.
        peer_rows = (
            [r for r in target_rows if not r.get(CALC_GROUP_ROW_KEY)]
            if calc.calc_type in _GRAIN_SENSITIVE else target_rows
        )
        group_rows = (
            [r for r in target_rows if r.get(CALC_GROUP_ROW_KEY)]
            if calc.calc_type in _GRAIN_SENSITIVE else []
        )

        if calc.calc_type == "pct_grand_total":
            # Ratio types assign per row against a global reference, so the
            # group row keeps its OWN percentage (group_total / grand_total)
            # while the reference excludes it.
            # F-002-04: for a non-additive base measure, use the re-aggregated
            # grand total (denom_requery_results) rather than sum-of-cells.
            _reagg_grand = None
            _nonadd = _is_nonadditive(calc.base_measure)
            if _nonadd:
                _reagg_grand = (denom_requery_results or {}).get(
                    (calc.name, "__grand__")
                )
            _eval_pct_grand_total(
                calc, target_rows, peer_rows=peer_rows,
                reaggregated_total=_reagg_grand,
                require_reaggregated=_nonadd,
            )
        elif calc.calc_type == "pct_parent":
            # F-002-04: non-additive parent totals come from per-parent re-query.
            # The planner keys each parent under its parent-dim value tuple (and
            # the no-dim / no-parent degenerate cases under ("__all__",)), which
            # is exactly what _eval_pct_parent derives — no key translation.
            _reagg_parents = None
            _nonadd = _is_nonadditive(calc.base_measure)
            if _nonadd:
                _reagg_parents = {
                    key[1]: val
                    for key, val in (denom_requery_results or {}).items()
                    if key[0] == calc.name
                }
            _eval_pct_parent(
                calc, target_rows, dim_cols, peer_rows=peer_rows,
                reaggregated_parents=_reagg_parents,
                require_reaggregated=_nonadd,
            )
        elif calc.calc_type in ("pct_row_total", "pct_col_total", "pct_axis_total"):
            # Bug-8206: % of Row / Column Total. The denominator holds the FIXED
            # axis dims constant and totals the base measure over the other axis.
            # partition_dims = the fixed (pinned) axis dims; the denominator is the
            # sum of the base over the peers sharing those values — re-aggregated
            # at grain (denom_requery_results) when the base measure is
            # non-additive, exactly the % of Parent contract.
            _pinned = _resolve_axis_total_pinned_dims(
                calc, dim_cols, row_axis_dims, col_axis_dims,
            )
            _reagg_axis = None
            _nonadd = _is_nonadditive(calc.base_measure)
            if _nonadd:
                _reagg_axis = {
                    key[1]: val
                    for key, val in (denom_requery_results or {}).items()
                    if key[0] == calc.name
                }
            _eval_pct_axis_total(
                calc, target_rows, _pinned, peer_rows=peer_rows,
                reaggregated_totals=_reagg_axis,
                require_reaggregated=_nonadd,
            )
        elif calc.calc_type == "index":
            _eval_index(calc, target_rows, peer_rows=peer_rows)
        elif calc.calc_type == "difference":
            if calc.ref_offset_dim:
                _eval_difference_relative(
                    calc, target_rows, dim_cols, peer_rows=peer_rows,
                )
            else:
                _eval_difference(calc, target_rows, dim_cols, peer_rows=peer_rows)
        elif calc.calc_type == "pct_difference":
            if calc.ref_offset_dim:
                _eval_difference_relative(
                    calc, target_rows, dim_cols, peer_rows=peer_rows, pct=True,
                )
            else:
                _eval_pct_difference(calc, target_rows, dim_cols, peer_rows=peer_rows)
        elif calc.calc_type == "running_total":
            # Sequence types have no natural position for a summary row: run the
            # cumulative sum over the peers only and blank the group rows.
            _eval_running_total(calc, peer_rows, dim_cols)
            for r in group_rows:
                r[calc.name] = None
        elif calc.calc_type == "rank_asc":
            _eval_rank(calc, peer_rows, dim_cols, ascending=True)
            for r in group_rows:
                r[calc.name] = None
        elif calc.calc_type == "rank_desc":
            _eval_rank(calc, peer_rows, dim_cols, ascending=False)
            for r in group_rows:
                r[calc.name] = None
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
    """Extract the measure name from [Measures].[Name].

    Bug-6717: accepts ``]]`` inside bracket bodies and unescapes to the
    raw technical name.
    """
    m = re.search(r'\[Measures\]\.\[((?:[^\]]|\]\])+)\]', full_name)
    return m.group(1).replace("]]", "]") if m else full_name


def _extract_dim_member_name(full_name: str) -> tuple[str, str]:
    """Extract (dimension_name, member_name) from [Dim].[Member] or [Dim].[Hier].[Member].

    Bug-6746: the bracket-body pattern is escape-aware — it accepts ``]]`` inside
    a body (an MDX-escaped literal ``]``) and unescapes to the raw name, matching
    the measure path (:func:`_extract_member_name`). Without this a dimension or
    hierarchy name containing ``]`` was split at the escaped bracket and
    mis-extracted.
    """
    parts = re.findall(r'\[((?:[^\]]|\]\])+)\]', full_name)
    if len(parts) >= 2:
        return parts[0].replace("]]", "]"), parts[-1].replace("]]", "]")
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
        # Bug-6746: escape-aware bracket bodies; unescape ]] on the captured
        # member name before it becomes an aggregate-set match key.
        members = re.findall(
            rf'\[({_CM_BRACKET_BODY}+)\](?:\.\[({_CM_BRACKET_BODY}+)\])*',
            member_list,
        )
        parsed: list[str] = []
        for m in members:
            last_non_empty = [p for p in m if p]
            if last_non_empty:
                parsed.append(last_non_empty[-1].replace("]]", "]"))
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
        calc.grand_total_all_dims = _extract_all_pinned_dims(expr)
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
        offset, off_dim = _extract_relative_ref(expr)
        if off_dim:  # relative navigation detected (offset may legitimately be 0)
            calc.ref_offset, calc.ref_offset_dim = offset, off_dim
        else:
            calc.ref_member_parts, calc.ref_ancestor_filters = _extract_difference_ref(expr)
        return

    if _is_difference(expr, base):
        calc.calc_type = "difference"
        offset, off_dim = _extract_relative_ref(expr)
        if off_dim:  # relative navigation detected (offset may legitimately be 0)
            calc.ref_offset, calc.ref_offset_dim = offset, off_dim
        else:
            calc.ref_member_parts, calc.ref_ancestor_filters = _extract_difference_ref(expr)
        return

    # Bug-8206: % of Column Total / % of Row Total. Excel emits these as a ratio
    # of the base measure over an AXIS-scoped total (an ``Axis(0)`` / ``Axis(1)``
    # reference, or the axis dimension's ``.CurrentMember`` folded to its
    # ``.Members`` total). These are now supported: the evaluator partitions the
    # detail rows by the FIXED (non-totalled) axis dims and totals the base
    # measure over the totalled axis, re-querying the denominator at grain for a
    # non-additive base — the same fail-closed contract as % of Parent.
    axis_kind, denom_axis, denom_dims = _classify_axis_total(expr, base)
    if axis_kind:
        calc.calc_type = axis_kind  # "pct_row_total" or "pct_col_total"
        calc.axis_total_axis = denom_axis
        calc.axis_total_denom_dims = denom_dims
        return

    measures = re.findall(rf'\[Measures\]\.\[{_CM_BRACKET_BODY}+\]', expr)
    if measures:
        calc.calc_type = "arithmetic"
        return

    calc.calc_type = "custom"


def _classify_axis_total(expr: str, base: str) -> tuple[str, int, list[str]]:
    """Classify a % of Column Total / % of Row Total Show-Values-As shape.

    Returns ``(calc_type, denom_axis, denom_dims)``:
    - ``calc_type`` is ``"pct_row_total"``, ``"pct_col_total"`` or ``""`` (not an
      axis total).
    - ``denom_axis`` is the MDX axis number the denominator totals over (0 =
      columns, 1 = rows) for the ``Axis(n)`` form, else -1.
    - ``denom_dims`` is the list of hierarchy / dimension names named in the
      denominator's ``.CurrentMember ... .Members`` axis set (empty for the
      ``Axis(n)`` form).

    Excel's canonical column/row-total forms:
    - ``Axis(0)`` totals over the COLUMN axis -> a ROW total (each row's cells
      summed across columns) -> ``pct_row_total``.
    - ``Axis(1)`` totals over the ROW axis -> a COLUMN total -> ``pct_col_total``.
    - The ``[Dim].[Hier].CurrentMember ... .Members`` form names the hierarchy
      being totalled; the evaluator maps it to row- or column-axis dim_cols.

    Conservative: only fires on the axis-total constructs Excel actually emits
    (requires a division and the base measure), so legitimate arithmetic members
    are never mislabelled. Grand total (``[(All)]``) and % of Parent (``.Parent``)
    are classified before this is reached.
    """
    if not base or "/" not in expr:
        return "", -1, []
    denom = expr.split("/", 1)[1]
    # Explicit axis reference (Excel's canonical column/row-total form).
    m = re.search(r'\bAxis\s*\(\s*([01])\s*\)', denom, re.IGNORECASE)
    if m:
        axis = int(m.group(1))
        # Axis(0)=columns totalled -> ROW total; Axis(1)=rows totalled -> COLUMN
        # total.
        return ("pct_row_total" if axis == 0 else "pct_col_total"), axis, []
    # Guard the Axis(n) form appearing anywhere (not just after the first '/'):
    m = re.search(r'\bAxis\s*\(\s*([01])\s*\)', expr, re.IGNORECASE)
    if m:
        axis = int(m.group(1))
        return ("pct_row_total" if axis == 0 else "pct_col_total"), axis, []
    # ``.CurrentMember ... .Members`` axis-membership total in the DENOMINATOR
    # (the right side of the first division): a level-members set the base
    # measure is aggregated over. Scoped to the denominator so an arithmetic
    # member that merely mentions .CurrentMember/.Members on the LEFT is not
    # falsely flagged (R2 finding 4). Not a grand total ([(All)]) nor a parent
    # (.Parent) — both classified before this is reached.
    if (
        re.search(r'\.CurrentMember', denom, re.IGNORECASE)
        and re.search(r'\.Members\b', denom, re.IGNORECASE)
    ):
        denom_dims = _extract_axis_total_denom_dims(denom)
        # The axis totalled is the one whose hierarchy is named; the evaluator
        # resolves that against the row/col dim split at eval time. We classify
        # as ``pct_axis_total`` (generic) with the named dims; the resolver
        # determines row-total vs col-total from which axis the named dims land
        # on. axis=-1 signals "resolve by named dims, not by Axis(n)".
        return "pct_axis_total", -1, denom_dims
    return "", -1, []


def _extract_axis_total_denom_dims(denom: str) -> list[str]:
    """Return the hierarchy / dimension names referenced in the denominator's
    ``.CurrentMember ... .Members`` axis set (Bug-8206).

    Excel writes the axis-membership total as
    ``([Dim].[Hier].CurrentMember, [Dim].[Hier].[Level].Members)`` or the level
    form ``[Dim].[Hier].[Level].Members``. We collect each ``[Dim]`` (and the
    optional ``[Hier]``) so the evaluator can map them onto the pivot's row/col
    dim_cols. ``[Measures]`` is skipped.
    """
    b = _CM_BRACKET_BODY
    names: list[str] = []
    for m in re.finditer(
        rf'\[({b}+)\](?:\.\[({b}+)\])?(?:\.\[({b}+)\])?\.(?:CurrentMember|Members)\b',
        denom, re.IGNORECASE,
    ):
        dim = m.group(1).replace("]]", "]")
        if dim.lower() == "measures":
            continue
        if dim not in names:
            names.append(dim)
        hier = m.group(2).replace("]]", "]") if m.group(2) else ""
        if hier and hier.lower() != "measures" and hier not in names:
            names.append(hier)
    return names


def _find_base_measure(expr: str) -> str:
    """Find the primary measure referenced in an expression.

    Bug-6746: escape-aware bracket body; returns the RAW (unescaped) measure
    name so it round-trips as a result-row key (``r.get(calc.base_measure)``)
    and as the measure's semantic identity. Substring checks against the raw
    expression must re-escape (see ``_escape_cm``).
    """
    m = re.search(rf'\[Measures\]\.\[({_CM_BRACKET_BODY}+)\]', expr)
    return m.group(1).replace("]]", "]") if m else ""


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
        # Bug-6746: base is the raw name; re-escape for the substring test
        # against the (still MDX-escaped) expression.
        base_b = f"[{_escape_cm(base)}]"
        if base_b in left and base_b in right:
            if re.search(r'\[\(All\)\]|\[All\]', right, re.IGNORECASE):
                return True
    return False


def _extract_all_pinned_dims(expr: str) -> list[str]:
    """Return the (dimension, hierarchy) names pinned to the All member in a
    grand-total denominator (F-002-07 adversarial R3 F1/F2).

    Excel pins each hierarchy as ``[Dim].[Hier].[(All)]`` or the member-form
    ``[Dim].[Hier].[All]`` (``[(All)]`` is the LEVEL name, ``[All]`` the ALL
    MEMBER unique name — clients echo BOTH, see Bug-5433 / mdschema ALL_MEMBER),
    and also the two-part ``[Dim].[(All)]`` / ``[Dim].[All]``. A true grand total
    pins EVERY queried hierarchy; a row/column total pins a proper subset. We
    return both the leading dimension name AND the (optional) hierarchy name of
    each pin so the evaluator can resolve either against the pivot's dim_cols or
    a hierarchy→level map. The ``.AllMembers`` form carries no explicit pin -> [].
    """
    b = _CM_BRACKET_BODY
    names: list[str] = []
    # Match [(All)] (level form) OR [All] (member form) as the final segment.
    for m in re.finditer(
        rf'\[({b}+)\](?:\.\[({b}+)\])?\.\[(?:\(All\)|All)\]', expr, re.IGNORECASE,
    ):
        dim = m.group(1).replace("]]", "]")
        if dim.lower() == "measures":
            continue
        names.append(dim)
        hier = m.group(2).replace("]]", "]") if m.group(2) else ""
        if hier and hier.lower() != "measures" and hier != dim:
            names.append(hier)
    return names


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
    # Bug-6746: escape-aware bodies; parts are matched against raw dim names
    # (_resolve_child_dim), so unescape ]] -> ].
    m = re.search(
        rf'\[({_CM_BRACKET_BODY}+)\]\.\[({_CM_BRACKET_BODY}+)\]\.CurrentMember\.Parent',
        expr, re.IGNORECASE,
    )
    if m:
        return [m.group(1).replace("]]", "]"), m.group(2).replace("]]", "]")], []
    m = re.search(
        rf'\[({_CM_BRACKET_BODY}+)\]\.\[({_CM_BRACKET_BODY}+)\]\.Parent',
        expr, re.IGNORECASE,
    )
    if m:
        return [m.group(1).replace("]]", "]"), m.group(2).replace("]]", "]")], []
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
        # Bug-6746: base is raw; re-escape for the substring test.
        base_b = f"[{_escape_cm(base)}]"
        if base_b in parts[0] and base_b in parts[1]:
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
            # Bug-6746: unescape the dim/hier/level bodies (keys already
            # unescaped by parse_member_keys) so parts match raw model names.
            parts = [m.group(3).replace("]]", "]"), m.group(2).replace("]]", "]"), keys[-1]]
            return parts, _depth_tag(keys)

    # (2) Hierarchy + key path, no explicit level: [Dim].[Hier].&[k0]&[k1]...
    m = re.search(
        rf'\[({b}+)\]\.\[({b}+)\]\.({KEY_PATH})', expr, re.IGNORECASE,
    )
    if m and m.group(1).lower() != "measures":
        keys = parse_member_keys(m.group(3))
        if keys:
            # No explicit level name — match the deepest key against the
            # hierarchy's own result column. Bug-6746: unescape ]].
            hier = m.group(2).replace("]]", "]")
            parts = [hier, hier, keys[-1]]
            return parts, _depth_tag(keys)

    # (3) Legacy caption form: [Dim].[Hier].[Member]
    for m in re.finditer(rf'\[({b}+)\]\.\[({b}+)\]\.\[({b}+)\]', expr):
        if m.group(1).lower() != "measures":
            # Bug-6746: unescape ]] on each part (matched against raw names).
            return [
                m.group(1).replace("]]", "]"),
                m.group(2).replace("]]", "]"),
                m.group(3).replace("]]", "]"),
            ], []
    return [], []


# Bug-6067: relative reference navigation used by "Difference From (previous)"
# and similar. PrevMember/Lag walk backward; NextMember/Lead walk forward.
# Bug-6746: escape-aware bracket bodies ([Dim]/[Hier] names may contain ]]).
_RELATIVE_NAV_RE = re.compile(
    r'\[((?:[^\]]|\]\])+)\](?:\.\[((?:[^\]]|\]\])+)\])?'  # [Dim] or [Dim].[Hier]
    r'(?:\.CurrentMember)?'                       # optional .CurrentMember
    r'\.(PrevMember|NextMember|Lag|Lead)'         # navigation function
    r'(?:\s*\(\s*(\d+)\s*\))?',                   # optional (n) for Lag/Lead
    re.IGNORECASE,
)


def _extract_relative_ref(expr: str) -> tuple[int, str]:
    """Detect a relative reference member (PrevMember/NextMember/Lag/Lead).

    Returns ``(offset, dim_name)`` where ``offset`` is signed — negative for
    backward navigation (PrevMember, Lag(n)) and positive for forward
    (NextMember, Lead(n)) — and ``dim_name`` is the dimension/hierarchy the
    navigation walks. A non-empty ``dim_name`` signals a relative reference was
    detected (the caller routes on that, since ``offset`` can legitimately be 0
    for Lag(0)/Lead(0)). Returns ``(0, "")`` when the reference is absolute so
    the caller falls back to the fixed-member resolver.
    """
    for m in _RELATIVE_NAV_RE.finditer(expr):
        # Bug-6746: unescape ]] — dim/hier matched against raw dim columns.
        dim = m.group(1).replace("]]", "]")
        hier = m.group(2).replace("]]", "]") if m.group(2) else m.group(2)
        # The navigation is never on [Measures]; skip a spurious measure match.
        if (hier or dim).lower() == "measures" or dim.lower() == "measures":
            continue
        fn = m.group(3).lower()
        n_raw = m.group(4)
        n = int(n_raw) if n_raw else 1
        offset = -n if fn in ("prevmember", "lag") else n
        # Prefer the hierarchy (deepest bracket) for dim-col resolution, matching
        # _extract_running_total_dim's candidate order.
        return offset, (hier or dim)
    return 0, ""


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
    m = re.search(
        rf'\[({_CM_BRACKET_BODY}+)\](?:\.\[({_CM_BRACKET_BODY}+)\])?\.CurrentMember',
        expr, re.IGNORECASE,
    )
    if not m:
        return None
    # Bug-6746: unescape ]] — candidates matched against raw dim columns.
    g1 = m.group(1).replace("]]", "]")
    g2 = m.group(2).replace("]]", "]") if m.group(2) else None
    candidates = [g2, g1] if g2 else [g1]
    dim_lower = {d.lower(): d for d in dim_cols}
    for c in candidates:
        if c and c.lower() in dim_lower:
            return dim_lower[c.lower()]
    return None


def _eval_pct_grand_total(
    calc: CalcMember,
    rows: list[dict],
    peer_rows: list[dict] | None = None,
    reaggregated_total: float | None = None,
    require_reaggregated: bool = False,
) -> None:
    """Evaluate % of Grand Total: cell / grand total for same measure.

    ``peer_rows`` (default ``rows``) is the DETAIL peer set the grand total is
    summed over; a custom-group synthetic row is excluded from it but still
    assigned its own ratio (F-002-01 / Bug-6065).

    ``reaggregated_total`` (F-002-04): when the base measure is NON-ADDITIVE
    (avg / count_distinct) the grand total cannot be the sum of the already
    aggregated leaf cells — SSAS re-aggregates the measure over the full grain.
    When a re-aggregated total is supplied it is used as the denominator instead
    of the sum-of-cells.

    ``require_reaggregated`` (adversarial R3 F2): True for a non-additive base.
    In that case the sum-of-cells is a WRONG denominator, so when no re-aggregated
    total is available (the re-query returned empty / NULL for this grain) the
    ratio is left undefined (None) — NEVER silently computed as sum-of-averages.
    """
    base = calc.base_measure
    denom_rows = peer_rows if peer_rows is not None else rows
    if reaggregated_total is not None:
        try:
            total = float(reaggregated_total)
        except (ValueError, TypeError):
            total = 0.0
    elif require_reaggregated:
        # Non-additive measure with no usable re-aggregated denominator: the only
        # mathematically valid denominator is unavailable, so emit blanks rather
        # than the wrong sum-of-averages (fail closed on the number, not open).
        for r in rows:
            r[calc.name] = None
        return
    else:
        total = 0.0
        for r in denom_rows:
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
    peer_rows: list[dict] | None = None,
    reaggregated_parents: dict[tuple, Any] | None = None,
    require_reaggregated: bool = False,
) -> None:
    """Evaluate % of Parent: cell / parent member's value.

    ``peer_rows`` (default ``rows``) is the DETAIL peer set the parent totals are
    summed over, excluding custom-group synthetic rows (F-002-01 / Bug-6065).

    ``reaggregated_parents`` (F-002-04): for a NON-ADDITIVE base measure the
    parent total is the measure re-aggregated over that parent's grain, NOT the
    sum of the child leaf cells (which are themselves averages / distinct counts).
    It is keyed by the same ``parent_key`` tuple this function derives. When a
    key is present its value is used as the parent denominator.

    ``require_reaggregated`` (adversarial R3 F2): True for a non-additive base.
    For any parent whose re-aggregated total is missing (the re-query returned
    empty / NULL for that parent grain) the ratio is left undefined (None) — the
    sum-of-cells fallback is a WRONG denominator and must never be used.
    """
    base = calc.base_measure
    denom_rows = peer_rows if peer_rows is not None else rows
    if not dim_cols:
        # Grand-total degenerate case: use a re-aggregated grand total when the
        # planner supplied one under the grand-total key.
        _grand = None
        if reaggregated_parents:
            _grand = reaggregated_parents.get(("__all__",))
        _eval_pct_grand_total(
            calc, rows, peer_rows=peer_rows, reaggregated_total=_grand,
            require_reaggregated=require_reaggregated,
        )
        return

    child_dim = _resolve_child_dim(calc.ref_member_parts, dim_cols)
    child_idx = dim_cols.index(child_dim) if child_dim in dim_cols else len(dim_cols) - 1
    parent_dims = [dc for i, dc in enumerate(dim_cols) if i != child_idx]

    parent_totals: dict[tuple, float] = {}
    for r in denom_rows:
        parent_key = tuple(str(r.get(dc, "")) for dc in parent_dims) if parent_dims else ("__all__",)
        val = r.get(base)
        if val is not None:
            try:
                parent_totals[parent_key] = parent_totals.get(parent_key, 0.0) + float(val)
            except (ValueError, TypeError):
                pass

    for r in rows:
        parent_key = tuple(str(r.get(dc, "")) for dc in parent_dims) if parent_dims else ("__all__",)
        # F-002-04: a re-aggregated non-additive parent total overrides the
        # sum-of-cells for this parent key when the re-query supplied one.
        reagg = reaggregated_parents.get(parent_key) if reaggregated_parents else None
        if reagg is not None:
            try:
                total = float(reagg)
            except (ValueError, TypeError):
                total = None
        elif require_reaggregated:
            # Non-additive and no re-aggregated parent total for this grain:
            # blank the cell rather than divide by the wrong sum-of-averages.
            total = None
        else:
            total = parent_totals.get(parent_key, 0.0)
        val = r.get(base)
        if val is not None and total is not None and total != 0:
            try:
                r[calc.name] = float(val) / total
            except (ValueError, TypeError):
                r[calc.name] = None
        else:
            r[calc.name] = None


def _resolve_axis_total_pinned_dims(
    calc: CalcMember,
    dim_cols: list[str],
    row_axis_dims: list[str] | None,
    col_axis_dims: list[str] | None,
) -> list[str] | None:
    """Resolve which dim_cols are HELD FIXED for a % of Row/Column Total (Bug-8206).

    A ``% of Row Total`` totals over the column axis, so the ROW-axis dims are
    fixed (the partition); a ``% of Column Total`` fixes the COLUMN-axis dims.

    Returns the ordered list of pinned dim_cols, or ``None`` when the axis split
    is unknown / the totalled axis is empty — the caller then blanks the cells
    (fail closed) rather than computing over the wrong grain.
    """
    row_dims = [d for d in (row_axis_dims or []) if d in dim_cols]
    col_dims = [d for d in (col_axis_dims or []) if d in dim_cols]
    if calc.calc_type == "pct_row_total":
        fixed, totalled = row_dims, col_dims
    elif calc.calc_type == "pct_col_total":
        fixed, totalled = col_dims, row_dims
    elif calc.calc_type == "pct_axis_total" and calc.axis_total_denom_dims:
        # Hierarchy-named form: the named dims identify the axis being totalled.
        # Resolve each named dim against the row/col split to find which axis it
        # is on; the OTHER axis is fixed. Fall back to fail-closed (None) if the
        # named dims can't be mapped.
        named_lower = {n.lower() for n in calc.axis_total_denom_dims}
        named_on_row = any(d.lower() in named_lower for d in row_dims)
        named_on_col = any(d.lower() in named_lower for d in col_dims)
        if named_on_row and not named_on_col:
            # Named dim is on the row axis -> totalling over rows -> COLUMN total.
            fixed, totalled = col_dims, row_dims
        elif named_on_col and not named_on_row:
            # Named dim is on the column axis -> totalling over columns -> ROW total.
            fixed, totalled = row_dims, col_dims
        else:
            # Ambiguous or unresolvable: fail closed.
            return None
    else:
        # Generic pct_axis_total with no denom_dims: fail closed.
        return None

    # The split must be known: at least one dim on the axis being totalled.
    # Without a totalled axis the "total" is a single cell (ratio == 1) which is
    # never what the user asked for — treat as unknown and fail closed.
    if not row_axis_dims and not col_axis_dims:
        return None
    if not totalled:
        return None
    return fixed


def _eval_pct_axis_total(
    calc: CalcMember,
    rows: list[dict],
    pinned_dims: list[str] | None,
    peer_rows: list[dict] | None = None,
    reaggregated_totals: dict[tuple, Any] | None = None,
    require_reaggregated: bool = False,
) -> None:
    """Evaluate % of Row Total / % of Column Total (Bug-8206).

    ``pinned_dims`` are the dim_cols held FIXED (the partition); the denominator
    for a cell is the base measure totalled over the peers that share the pinned
    values — i.e. summed across the OTHER (totalled) axis. This is the % of Parent
    contract generalised to an arbitrary fixed-dim subset.

    ``reaggregated_totals`` (F-002-04 parity): for a NON-ADDITIVE base measure the
    partition total is the measure re-aggregated at the pinned grain, keyed by the
    pinned value tuple (BLANK_MEMBER-normalised), NOT the sum of the leaf cells.

    ``require_reaggregated`` True for a non-additive base: a partition whose
    re-aggregated total is missing is left undefined (None) — the sum-of-cells
    fallback is a WRONG denominator and is never used.

    When ``pinned_dims`` is None the axis split is unknown; every cell is blanked
    (fail closed) rather than computing a wrong ratio.
    """
    base = calc.base_measure
    if pinned_dims is None:
        for r in rows:
            r[calc.name] = None
        return

    denom_rows = peer_rows if peer_rows is not None else rows

    def _part_key(r: dict) -> tuple:
        if not pinned_dims:
            return ("__all__",)
        return tuple(_normalize_member_value(r.get(dc)) for dc in pinned_dims)

    part_totals: dict[tuple, float] = {}
    for r in denom_rows:
        key = _part_key(r)
        val = r.get(base)
        if val is not None:
            try:
                part_totals[key] = part_totals.get(key, 0.0) + float(val)
            except (ValueError, TypeError):
                pass

    for r in rows:
        key = _part_key(r)
        reagg = reaggregated_totals.get(key) if reaggregated_totals else None
        if reagg is not None:
            try:
                total: float | None = float(reagg)
            except (ValueError, TypeError):
                total = None
        elif require_reaggregated:
            total = None
        else:
            total = part_totals.get(key, 0.0)
        val = r.get(base)
        if val is not None and total is not None and total != 0:
            try:
                r[calc.name] = float(val) / total
            except (ValueError, TypeError):
                r[calc.name] = None
        else:
            r[calc.name] = None


def _eval_index(
    calc: CalcMember, rows: list[dict], peer_rows: list[dict] | None = None,
) -> None:
    """Evaluate Index: (cell / average_of_all_cells) * 100.

    ``peer_rows`` (default ``rows``) is the DETAIL peer set the average is taken
    over, excluding custom-group synthetic rows (F-002-01 / Bug-6065).
    """
    base = calc.base_measure
    denom_rows = peer_rows if peer_rows is not None else rows
    total = 0.0
    count = 0
    for r in denom_rows:
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
    peer_rows: list[dict] | None = None,
) -> None:
    """Evaluate Difference From: cell - reference cell.

    ``peer_rows`` (default ``rows``) is the DETAIL peer set the reference member
    value is resolved from, excluding custom-group synthetic rows (F-002-01 /
    Bug-6065).
    """
    base = calc.base_measure
    ref = calc.ref_member_parts
    ref_source_rows = peer_rows if peer_rows is not None else rows

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
    for r in ref_source_rows:
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
    peer_rows: list[dict] | None = None,
) -> None:
    """Evaluate % Difference From: (cell - ref) / ref.

    ``peer_rows`` (default ``rows``) is the DETAIL peer set the reference member
    is resolved from, excluding custom-group synthetic rows (F-002-01 /
    Bug-6065).
    """
    _eval_difference(calc, rows, dim_cols, peer_rows=peer_rows)
    base_name = calc.name
    ref_parts = calc.ref_member_parts
    ref_source_rows = peer_rows if peer_rows is not None else rows
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
    for r in ref_source_rows:
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


def _eval_difference_relative(
    calc: CalcMember,
    rows: list[dict],
    dim_cols: list[str],
    peer_rows: list[dict] | None = None,
    pct: bool = False,
) -> None:
    """Evaluate a Difference From a RELATIVE member (PrevMember/NextMember/
    Lag/Lead).

    Bug-6067: the absolute-reference path resolves a FIXED member value and
    returned an all-blank column for relative navigations, which carry no
    member key. This instead compares each cell to the cell of the member
    ``calc.ref_offset`` positions away along the ordered members of the target
    dimension, partitioned by the other dims — standard MDX PrevMember/Lag
    semantics. ``pct`` yields ``(cell - ref) / ref`` instead of ``cell - ref``.
    The reference set is the DETAIL ``peer_rows`` (excludes synthetic group
    rows), matching the absolute path.
    """
    base = calc.base_measure
    offset = calc.ref_offset
    target_dim = _resolve_ref_dim_col(calc.ref_offset_dim, dim_cols)
    ref_source_rows = peer_rows if peer_rows is not None else rows

    # offset 0 (Lag(0)/Lead(0)) is a valid self-reference -> zero difference,
    # so it is NOT a blanking condition; only an unresolvable target dim or
    # missing base measure blanks the column (R3 Codex finding).
    if not base or target_dim not in dim_cols:
        for r in rows:
            r[calc.name] = None
        return

    partition_dims = [d for d in dim_cols if d != target_dim]

    # Order members by real axis order (semantic value, then first-appearance
    # ordinal) — NOT caption alphabetical, which would resolve PrevMember/Lag
    # against the wrong peer for arbitrary text members (silent wrong numbers).
    _sort_key = _axis_order_key_fn(ref_source_rows, target_dim)

    # Order the target members and map member -> value within each partition,
    # from the detail peer rows.
    groups: dict[tuple, list[dict]] = {}
    for r in ref_source_rows:
        key = tuple(str(r.get(d, "")) for d in partition_dims)
        groups.setdefault(key, []).append(r)

    ordered_members: dict[tuple, list[str]] = {}
    member_val: dict[tuple, dict[str, float | None]] = {}
    for key, group in groups.items():
        group_sorted = sorted(group, key=_sort_key)
        members: list[str] = []
        vmap: dict[str, float | None] = {}
        for g in group_sorted:
            mv = str(g.get(target_dim, ""))
            if mv in vmap:
                continue  # keep first occurrence's position
            members.append(mv)
            val = g.get(base)
            try:
                vmap[mv] = float(val) if val is not None else None
            except (ValueError, TypeError):
                vmap[mv] = None
        ordered_members[key] = members
        member_val[key] = vmap

    for r in rows:
        key = tuple(str(r.get(d, "")) for d in partition_dims)
        members = ordered_members.get(key, [])
        vmap = member_val.get(key, {})
        cur_member = str(r.get(target_dim, ""))
        cur_val = r.get(base)
        try:
            idx = members.index(cur_member)
        except ValueError:
            r[calc.name] = None
            continue
        ref_idx = idx + offset
        if not (0 <= ref_idx < len(members)) or cur_val is None:
            r[calc.name] = None
            continue
        ref_val = vmap.get(members[ref_idx])
        if ref_val is None:
            r[calc.name] = None
            continue
        try:
            diff = float(cur_val) - ref_val
            if pct:
                r[calc.name] = diff / ref_val if ref_val != 0 else None
            else:
                r[calc.name] = diff
        except (ValueError, TypeError):
            r[calc.name] = None


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

    # Order the cumulative sequence by real axis order (semantic value, then
    # first-appearance ordinal), never caption alphabetical — an alphabetical
    # fallback accumulates arbitrary text members in the wrong order and yields
    # wrong running totals.
    _sort_key = _axis_order_key_fn(rows, target_dim)

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

    # Bug-8323: detail-vs-subtotal row discriminator (see the ``matching`` filter
    # below). Imported locally to match this module's existing pattern and avoid a
    # module-load import cycle with subtotal_engine.
    from src.dax.subtotal_engine import select_detail_rows

    # The custom group is a DETAIL-grain construct. ``rows`` is still returned in
    # full (the synthetic group row is appended to it), so keep the merged list
    # under its own name and derive every grain-sensitive decision from
    # ``detail_only``.
    detail_only = select_detail_rows(rows)

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
            all_vals = {str(r.get(dc, "")).lower() for r in detail_only}
            if all_vals & members_lower:
                target_dim = dc
                break
    if target_dim is None:
        logger.warning(
            "Aggregate member set %r: no matching dimension column found in %r",
            calc.aggregate_members, dim_cols,
        )
        return rows

    # Bug-8323: on a pivot that also carries a subtotal hierarchy, ``rows`` is the
    # MERGED result (detail + subtotal/grand-total rows). A subtotal row taken at
    # (or above) the group's target level can match a member while its OTHER
    # dimension columns are None, which would spawn a synthetic group row for a
    # spurious ``other_dim=None`` partition (a wasted/again-faulting re-query and a
    # blank/mis-aggregated cell). The custom group is a detail-grain construct, so
    # aggregate ONLY over detail rows — exactly the rows the planner
    # (``plan_aggregate_requeried``) now scopes its partitions to, keeping the
    # evaluator's partition keys and the re-query specs in lock-step. A flat pivot
    # has no subtotal rows (the key defaults to "detail"), so this is a no-op there.
    matching = [
        r for r in detail_only
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
        # F-002-01 (Bug-6065): stamp the synthetic group row so grain-sensitive
        # evaluators exclude it from denominators / peer sets.
        synth: dict[str, Any] = {CALC_GROUP_ROW_KEY: True}
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
    connector_type: str = "postgresql"
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
    connector_type: str = "postgresql",
) -> list[ReQuerySpec]:
    """Identify aggregate_set calc members that need re-queries for non-composable aggregations."""
    if not measures_meta:
        return []

    # Bug-8323 / Bug-8379: scope every row-derived decision in this planner to the
    # DETAIL grain (see ``select_detail_rows`` for why, and the ``matching``
    # filter below). Rebinding the parameter — rather than filtering at each use
    # site — is deliberate: it makes it structurally impossible for a later loop
    # added to this function to read a subtotal row's ``None`` dimension value as
    # a real member. Local import matches this module's pattern and avoids a
    # module-load cycle with subtotal_engine.
    from src.dax.subtotal_engine import select_detail_rows

    rows = select_detail_rows(rows)

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
        #
        # Bug-8323: when the pivot also has a subtotal hierarchy, ``rows`` is the
        # MERGED result set. A subtotal/grand-total row has None in its finer
        # dimension columns, so partitioning over it emits spurious
        # ``other_dim=None`` specs (``city='None'`` re-queries that waste a source
        # round-trip and again fault). Scope partitions to DETAIL rows only — the
        # exact set ``_eval_aggregate_set`` now aggregates — so the planned
        # partition keys and the consumed re-query keys stay in lock-step. A flat
        # pivot has no subtotal rows, so this is a no-op there.
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
            # For aggregate_set, only NON-COMPOSABLE aggregations need a
            # re-query. SUM, COUNT, MIN, MAX are composable:
            #   SUM(SUM(a), SUM(b)) = SUM(a, b)
            #   MAX(MAX(a), MAX(b)) = MAX(a, b)
            # AVG and COUNT_DISTINCT are NOT composable:
            #   AVG(AVG(a), AVG(b)) != AVG(a, b)
            # Percentile, stddev, etc. are also non-composable.
            _COMPOSABLE_AGGS = frozenset({"sum", "count", "min", "max"})
            if agg not in _COMPOSABLE_AGGS:
                for part_key, part_values in seen_partitions.items():
                    specs.append(ReQuerySpec(
                        calc_name=calc.name,
                        measure_name=mname,
                        agg=agg,
                        dim_col=target_dim,
                        members=list(calc.aggregate_members),
                        model_slug=model_slug,
                        connector_type=connector_type,
                        partition_key=part_key,
                        partition_dims=list(other_dims),
                        partition_values=part_values,
                    ))

    return specs


def build_requery_sql(spec: ReQuerySpec) -> str:
    """Build the SQL for a single re-query spec.

    INVARIANT (live channel): the re-query is executed through the query-router
    with ``dialect="postgres"`` (it re-parses this SQL as canonical PostgreSQL
    and transpiles to the source), so on that channel ``spec.connector_type``
    MUST stay ``"postgresql"`` and this function emits canonical-postgres SQL.
    The ``connector_type`` parameter renders identifiers AND literals in a single
    consistent dialect (so unit tests can pin connector-native quoting), but
    wiring a non-``postgresql`` connector into the live ``execute_query`` path
    would emit SQL the router mislabels as postgres — do not do so without also
    changing the executed ``dialect``.
    """
    def _q(name: str) -> str:
        return quote_identifier(spec.connector_type, name)

    def _lit(value: str) -> str:
        # Bug-6074: client-derived member/partition values are rendered as
        # dialect-correct SQL string literals via sqlglot, never string-concat
        # with naive ``''`` doubling (which is bypassable on backslash-aware
        # dialects such as BigQuery/Spark/Snowflake -> SQL injection).
        return quote_literal(spec.connector_type, value)

    if spec.agg == "count_distinct":
        agg_expr = f"COUNT(DISTINCT {_q(spec.measure_name)})"
    elif spec.agg == "min":
        agg_expr = f"MIN({_q(spec.measure_name)})"
    elif spec.agg == "max":
        agg_expr = f"MAX({_q(spec.measure_name)})"
    else:
        agg_expr = f"AVG({_q(spec.measure_name)})"

    in_list = ", ".join(_lit(m) for m in spec.members)
    where_parts = [f"{_q(spec.dim_col)} IN ({in_list})"]
    # F-002-09: pin the other-dimension partition so the re-query value belongs
    # to exactly the synthetic group row that consumes it.
    for dim, val in zip(spec.partition_dims, spec.partition_values):
        where_parts.append(f"{_q(dim)} = {_lit(val)}")
    if spec.extra_where:
        where_parts.extend(spec.extra_where)
    return (
        f"SELECT {agg_expr} AS {_q(spec.measure_name)}"
        f" FROM {_q(spec.model_slug)}"
        f" WHERE {' AND '.join(where_parts)}"
    )


@dataclass
class DenomReQuerySpec:
    """Re-query for a Show-Values-As ratio denominator over a non-additive
    measure (F-002-04).

    A % of Grand Total / % of Parent denominator for an avg / count_distinct
    measure must be the measure re-aggregated at the wider grain, never the sum
    of the already-aggregated leaf cells. Each spec re-queries exactly one
    denominator; the result is injected back keyed by
    ``(calc_name, partition_key)`` where ``partition_key`` is ``"__grand__"``
    for a grand total or the parent-dimension value tuple for a parent total.
    """
    calc_name: str
    measure_name: str
    agg: str
    model_slug: str
    partition_key: Any  # "__grand__" or tuple(parent dim values)
    partition_dims: list[str] = field(default_factory=list)
    partition_values: list[str] = field(default_factory=list)
    connector_type: str = "postgresql"
    extra_where: list[str] = field(default_factory=list)


def plan_denominator_requeried(
    calc_members: list[CalcMember],
    measures_meta: list[dict[str, Any]] | None,
    model_slug: str,
    dim_cols: list[str],
    rows: list[dict[str, Any]],
    connector_type: str = "postgresql",
    row_axis_dims: list[str] | None = None,
    col_axis_dims: list[str] | None = None,
) -> list[DenomReQuerySpec]:
    """Plan denominator re-queries for non-additive % of Grand Total / Parent /
    Row Total / Column Total.

    Index is intentionally excluded: its reference is the AVERAGE of the members
    actually displayed (``Avg({members}, measure)``), which is correctly the
    mean of the shown cells — re-aggregating from fact grain would compute a
    different quantity. Grand-total, parent-total and axis-total denominators, by
    contrast, ARE the measure's own total at a coarser grain and must be
    re-aggregated for a non-additive measure.

    ``row_axis_dims`` / ``col_axis_dims`` (Bug-8206) drive the axis-total
    partition: a % of Row Total pins the row-axis dims, a % of Column Total pins
    the column-axis dims (one re-query per distinct pinned tuple).
    """
    if not measures_meta:
        return []

    # Bug-8379 (root cause; the leg Bug-8323 left unfixed): ``rows`` is the
    # MERGED result set when the pivot also carries a subtotal hierarchy. A
    # subtotal / grand-total row has ``None`` in its finer dimension columns, so
    # partitioning over it emits partition keys of ``BLANK_MEMBER`` that
    # ``build_denominator_requery_sql`` pins as ``(dim IS NULL OR dim = '')``.
    # Those specs are never consumed — ``evaluate_calc_members`` evaluates every
    # grain-sensitive calc over DETAIL rows only and blanks the calc column on
    # subtotal rows — yet each one is a REQUIRED re-query (F-002-03): it burns a
    # source round-trip, and if it trips the byte ceiling / rate limit or errors,
    # ``_rq_failures`` faults the WHOLE pivot that would otherwise have rendered
    # correctly. Scope the partitioning to detail rows only, so the planner's
    # keys are exactly the keys the evaluator derives (lock-step, the same
    # contract ``plan_aggregate_requeried`` holds). No-op on a flat pivot.
    from src.dax.subtotal_engine import select_detail_rows

    rows = select_detail_rows(rows)

    agg_by_measure: dict[str, str] = {}
    for m in measures_meta:
        mname = m.get("name", "")
        if mname:
            agg_by_measure[mname.lower()] = (m.get("default_agg") or "sum").lower()

    specs: list[DenomReQuerySpec] = []
    for calc in calc_members:
        if calc.calc_type not in (
            "pct_grand_total", "pct_parent", "pct_row_total", "pct_col_total",
            "pct_axis_total",
        ):
            continue
        base = calc.base_measure
        agg = agg_by_measure.get((base or "").lower(), "sum")
        # Bug-7857: only SUM and COUNT are additive (sum-of-cells is correct).
        # Everything else (avg, count_distinct, min, max, percentile/pNN,
        # median, stddev) requires re-aggregation at the wider grain.
        if agg in ("sum", "count"):
            continue  # additive measure: sum-of-cells is correct, no re-query
        # Only emit a re-query spec for agg types we can correctly build SQL
        # for. For unsupported types (percentile/pNN, stddev, etc.), skip the
        # spec -- the evaluator blanks the cell (require_reaggregated=True but
        # no result) which is fail-closed (blank, not wrong).
        _REQUERY_SUPPORTED = frozenset({"avg", "count_distinct", "min", "max"})
        if agg not in _REQUERY_SUPPORTED:
            continue

        # Bug-8206: % of Row / Column Total — one re-query per pinned (fixed-axis)
        # tuple. The pinned dims are resolved the same way the evaluator does.
        if calc.calc_type in ("pct_row_total", "pct_col_total", "pct_axis_total"):
            pinned = _resolve_axis_total_pinned_dims(
                calc, dim_cols, row_axis_dims, col_axis_dims,
            )
            if pinned is None:
                # Unknown split -> evaluator fails closed; no re-query to plan.
                continue
            if not pinned:
                specs.append(DenomReQuerySpec(
                    calc_name=calc.name,
                    measure_name=base,
                    agg=agg,
                    model_slug=model_slug,
                    partition_key=("__all__",),
                    connector_type=connector_type,
                ))
                continue
            seen_axis: dict[tuple, list[str]] = {}
            for r in rows:
                key = tuple(_normalize_member_value(r.get(dc)) for dc in pinned)
                if key not in seen_axis:
                    seen_axis[key] = list(key)
            for part_key, part_values in seen_axis.items():
                specs.append(DenomReQuerySpec(
                    calc_name=calc.name,
                    measure_name=base,
                    agg=agg,
                    model_slug=model_slug,
                    partition_key=part_key,
                    partition_dims=list(pinned),
                    partition_values=part_values,
                    connector_type=connector_type,
                ))
            continue

        if calc.calc_type == "pct_grand_total":
            specs.append(DenomReQuerySpec(
                calc_name=calc.name,
                measure_name=base,
                agg=agg,
                model_slug=model_slug,
                partition_key="__grand__",
                connector_type=connector_type,
            ))
            continue

        # pct_parent with no dim columns degenerates to a grand total, but the
        # evaluator's no-dim branch reads it under ("__all__",) — R1 finding 3:
        # emit that exact key so the result is not discarded.
        if not dim_cols:
            specs.append(DenomReQuerySpec(
                calc_name=calc.name,
                measure_name=base,
                agg=agg,
                model_slug=model_slug,
                partition_key=("__all__",),
                connector_type=connector_type,
            ))
            continue

        # pct_parent: one re-query per distinct parent tuple (partition = all
        # dims except the resolved child/deepest dim — mirrors _eval_pct_parent).
        child_dim = _resolve_child_dim(calc.ref_member_parts, dim_cols)
        child_idx = (
            dim_cols.index(child_dim) if child_dim in dim_cols else len(dim_cols) - 1
        )
        parent_dims = [dc for i, dc in enumerate(dim_cols) if i != child_idx]
        if not parent_dims:
            specs.append(DenomReQuerySpec(
                calc_name=calc.name,
                measure_name=base,
                agg=agg,
                model_slug=model_slug,
                partition_key=("__all__",),
                connector_type=connector_type,
            ))
            continue

        # R1 finding 2: normalise NULL/empty parent members to BLANK_MEMBER so the
        # planned partition key matches the evaluator's normalised row key.
        seen: dict[tuple, list[str]] = {}
        for r in rows:
            key = tuple(_normalize_member_value(r.get(dc)) for dc in parent_dims)
            if key not in seen:
                seen[key] = list(key)
        for part_key, part_values in seen.items():
            specs.append(DenomReQuerySpec(
                calc_name=calc.name,
                measure_name=base,
                agg=agg,
                model_slug=model_slug,
                partition_key=part_key,
                partition_dims=list(parent_dims),
                partition_values=part_values,
                connector_type=connector_type,
            ))

    return specs


def build_denominator_requery_sql(spec: DenomReQuerySpec) -> str:
    """Build the SQL re-aggregating a denominator over the wider grain.

    Same live-channel invariant as :func:`build_requery_sql`: the re-query runs
    through the query-router with ``dialect="postgres"``, so on that channel
    ``connector_type`` MUST stay ``"postgresql"`` and this emits canonical
    postgres SQL. Client-derived partition values are rendered as dialect-correct
    literals via :func:`quote_literal` (never string-concat) — the same
    injection-safe path as the aggregate-set re-query (Bug-6074).
    """
    def _q(name: str) -> str:
        return quote_identifier(spec.connector_type, name)

    def _lit(value: str) -> str:
        return quote_literal(spec.connector_type, value)

    if spec.agg == "count_distinct":
        agg_expr = f"COUNT(DISTINCT {_q(spec.measure_name)})"
    elif spec.agg == "min":
        agg_expr = f"MIN({_q(spec.measure_name)})"
    elif spec.agg == "max":
        agg_expr = f"MAX({_q(spec.measure_name)})"
    else:
        agg_expr = f"AVG({_q(spec.measure_name)})"

    where_parts: list[str] = []
    for dim, val in zip(spec.partition_dims, spec.partition_values):
        # R1 finding 2: a normalised BLANK_MEMBER partition value stands for a
        # NULL or empty source cell, so pin it with the SQL semantics that
        # actually matched those rows — never ``col = '(blank)'`` (a value that
        # does not exist in the source).
        if val == BLANK_MEMBER:
            where_parts.append(
                f"({_q(dim)} IS NULL OR {_q(dim)} = {_lit('')})"
            )
        else:
            where_parts.append(f"{_q(dim)} = {_lit(val)}")
    if spec.extra_where:
        where_parts.extend(spec.extra_where)

    sql = f"SELECT {agg_expr} AS {_q(spec.measure_name)} FROM {_q(spec.model_slug)}"
    if where_parts:
        sql += f" WHERE {' AND '.join(where_parts)}"
    return sql


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
        except Exception as exc:
            # Bug-6949: log on failure rather than silently blanking.
            logger.debug(
                "Calc-member %r evaluation error: %s", calc.name, exc,
            )
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
    m = re.match(rf'\[Measures\]\.\[({_CM_BRACKET_BODY}+)\]', token)
    if m:
        # Bug-6746: unescape ]] — measure name keys into the raw result row.
        name = m.group(1).replace("]]", "]")
        return lambda r, n=name: _to_float(r.get(n))

    try:
        val = float(token)
        return lambda r, v=val: v
    except ValueError:
        pass

    # Bug-6071: a unary sign on a NON-numeric operand (a measure reference or a
    # parenthesised sub-expression, e.g. ``-[Measures].[Revenue]`` or
    # ``-([Measures].[A]+[Measures].[B])``). Pure numeric signs like ``-5`` are
    # already resolved by ``float()`` above; without this branch a leading '-'
    # fell through to the ``None`` fallback and the member rendered blank.
    if token[:1] in ('-', '+'):
        inner = _compile_expression(token[1:].strip())
        if token[0] == '-':
            return lambda r, f=inner: (None if f(r) is None else -f(r))
        return inner

    if re.match(r'^IIF\s*\(', token, re.IGNORECASE):
        return _compile_expression(token)

    if token.startswith('('):
        return _compile_expression(token)

    # Bug-6949: log for unrecognized tokens so blank cells are diagnosable.
    logger.warning(
        "Bug-6949: unsupported calc-member token %r -- "
        "this expression will evaluate to blank.",
        token[:120],
    )
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
