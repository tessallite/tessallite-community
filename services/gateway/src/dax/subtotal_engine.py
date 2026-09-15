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
from decimal import Decimal, InvalidOperation
from typing import Any

from shared.connector_qualify import quote_identifier
from src.dax.member_uname import (
    KEY_PATH,
    first_bracket_body,
    parse_member_uname,
    unescape_member_key,
)


SUBTOTAL_LEVEL_KEY = "_subtotal_level"
SUBTOTAL_GRAIN_KEY = "_subtotal_grain"

# Shared by detect_subtotal_hierarchies and detect_flat_attribute_rollups:
# matches [Dim].[Hier], optionally followed by an explicit [(All)] level
# qualifier, followed by .Members / .MEMBERS / .AllMembers -- but NOT when
# followed by any OTHER bracketed segment (a level-scoped or member-scoped
# request, which must not be read as a full-hierarchy/full-attribute
# expansion). See detect_subtotal_hierarchies's docstring (Bug-9766,
# Bug-6892) for why the (All) qualifier must be consumed here rather than
# excluded by the trailing negative lookahead.
# Bug-9777: ``DrilldownLevel({[Dim].[Hier].[All]})`` -- the shape Excel sends
# once it knows the hierarchy HAS an All member (i.e. once MDSCHEMA_HIERARCHIES
# advertises ALL_MEMBER, which it did not before Bug-9772). Per SSAS,
# DrilldownLevel returns the argument set PLUS the children of its lowest level,
# so an All-member argument means "the All row AND its children" -- an explicit,
# unambiguous request for the rollup row. Unlike the bare ``.Members`` shape this
# CANNOT be confused with ordinary field enumeration, because the client had to
# name the All member to write it.
# Bug-9772: ``AddCalculatedMembers({[Dim].[Hier].[(All)].Members})`` -- the
# shape native Excel sends for ONE flat field. The set is the (All)-level
# member plus its calculated siblings, so it names the aggregate explicitly
# and, like DrilldownLevel, removes the ambiguity that keeps a bare
# ``.Members`` from producing a rollup. Bare ``.Members`` is unchanged.
_CALC_MEMBERS_ALL_RE = re.compile(
    r'AddCalculatedMembers\s*\(\s*\{\s*'
    r'\[([^\]]+)\]\.\[([^\]]+)\]\s*\.\s*\[\(\s*[Aa][Ll][Ll]\s*\)\]'
    r'\s*\.\s*(?:Members|MEMBERS)\s*\}\s*\)',
)

# Bug-9857: Excel's "Expand Entire Field" sends the two-argument form
# ``DrilldownLevel({[H].[H].[All]}, [H].[H].[Month])`` -- the All set plus a
# target LEVEL. SSAS returns every member down to and including that level.
# Group 4 captures the optional level name; absent, the target is the first
# data level (Bug-9764). Without this branch the level reference fell through
# to the member translator and became ``WHERE <leaf> = 'Month'``.
# Excel wraps the base set in a doubled brace pair (``{{...}}``); the named
# group keeps the closing pair balanced so the match never swallows the
# enclosing set function's brace.
_DRILLDOWN_ALL_RE = re.compile(
    r'DrilldownLevel\s*\(\s*\{\s*(?P<inner_all>\{\s*)?'
    r'\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]'
    r'\s*\.\s*\[\s*\(?\s*[Aa][Ll][Ll]\s*\)?\s*\]'
    r'\s*(?(inner_all)\}\s*\}|\})\s*'
    r'(?:,\s*\[(?:[^\]]|\]\])+\]\.\[(?:[^\]]|\]\])+\]'
    r'\s*\.\s*\[((?:[^\]]|\]\])+)\]\s*)?'
    r'\)',
    re.IGNORECASE,
)

# Bug-9857: Excel's per-level expand. The base set is a LEVEL's members, not
# the All member, so the result carries that level plus the level below the
# named (or, absent an argument, the base) level -- and no All row. Group 3 is
# the base level, group 4 the optional target level.
_DRILLDOWN_LEVEL_SET_RE = re.compile(
    r'DrilldownLevel\s*\(\s*\{\s*(?P<inner_lvl>\{\s*)?'
    r'\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]'
    r'\s*\.\s*\[((?:[^\]]|\]\])+)\]\s*\.\s*(?:Members|MEMBERS)'
    r'\s*(?(inner_lvl)\}\s*\}|\})\s*'
    r'(?:,\s*\[(?:[^\]]|\]\])+\]\.\[(?:[^\]]|\]\])+\]'
    r'\s*\.\s*\[((?:[^\]]|\]\])+)\]\s*)?'
    r'\)',
    re.IGNORECASE,
)


# Bug-9777: set-RESTRICTING functions. A DrilldownLevel nested inside one of
# these no longer describes the whole member set, so a grand total computed over
# the unrestricted dimension would not be the total of the rows actually shown.
# Live proof: DrilldownMember(DrilldownLevel({[a].[a].[All]}), {[a].[a].[CREDIT]})
# rendered an All row carrying 36,179,774.10 -- CREDIT's value -- while the true
# grand total is 180,720,566.17. Detection is therefore SKIPPED for these shapes
# and falls back to the `.Members` rule, which yields no rollup: a MISSING All
# row is a display gap, a WRONG All row is a wrong number reported to the user.
# CrossJoin is deliberately absent -- it combines dimensions, it does not
# restrict members, and it is the shape a nested PivotTable actually sends.
_SET_RESTRICTING_FN_RE = re.compile(
    r'\b(?:DrilldownMember|DrillupMember|DrillupLevel|Filter|TopCount'
    r'|BottomCount|TopPercent|BottomPercent|TopSum|BottomSum|Head|Tail'
    r'|Subset|Except|Intersect)\s*\(',
    re.IGNORECASE,
)

_WIRE_HIERARCHY_OWNER_DIMS = frozenset({"hierarchies", "dimensions"})


def _hierarchy_drilldown_all_lookup_key(
    dim_part: str,
    hier_part: str,
    standalone_attribute_names: set[str],
) -> str | None:
    """Hierarchy key for ``DrilldownLevel({[…].[…].[All]})`` detection/rewrite.

    Accepts internal self-named hierarchies ``[H].[H]`` and wire names
    ``[Hierarchies].[H]`` / ``[Dimensions].[H]`` (Bug-9764 Excel Year drag).
    Flat attributes ``[a].[a]`` stay on ``detect_flat_attribute_rollups``.
    """
    dim_l = dim_part.strip().lower()
    hier_l = hier_part.strip().lower()
    names_lower = {n.lower() for n in standalone_attribute_names}
    if dim_l in names_lower:
        return hier_l if dim_l == hier_l else None
    if dim_l in _WIRE_HIERARCHY_OWNER_DIMS:
        return hier_l
    if dim_l == hier_l:
        return hier_l
    return None


_SUBTOTAL_MEMBERS_RE = re.compile(
    r'(?<!\]\.)'
    r'\[([^\]]+)\]\.\[([^\]]+)\]'
    r'(?:\s*\.\s*\[\(\s*[Aa][Ll][Ll]\s*\)\])?'
    r'(?!\s*\.\s*(?:\[|&\[))'
    r'\.(?:Members|MEMBERS|AllMembers)\b',
)


def select_detail_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the DETAIL (leaf-grain) rows of a merged result set.

    Bug-8323 / Bug-8379 — one definition of the detail-grain invariant.

    When a pivot requests hierarchy subtotals, ``merge_grain_results`` returns a
    MERGED row list: leaf rows tagged ``SUBTOTAL_LEVEL_KEY == "detail"`` plus
    subtotal / grand-total rows tagged with their grain level. A subtotal row
    carries ``None`` in every dimension column finer than its own grain, so any
    consumer that derives a *grain-sensitive* artefact from it — a
    denominator/aggregate re-query partition key, a Top-N survivor predicate, a
    custom-group member match, a Show-Values-As peer set — reads that ``None``
    as a real "(blank)" member and produces a spurious or contaminated result.

    Every such consumer previously re-implemented the same one-line filter, and
    the copies drifted (``plan_denominator_requeried`` was left out of the
    Bug-8323 fix and stayed exposed as Bug-8379). This helper is the single
    home for the rule: a row with no ``SUBTOTAL_LEVEL_KEY`` at all is a detail
    row, so this is a no-op on a flat (non-subtotal) pivot.
    """
    return [r for r in rows if r.get(SUBTOTAL_LEVEL_KEY, "detail") == "detail"]


def select_non_detail_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Complement of :func:`select_detail_rows` — subtotal / grand-total rows."""
    return [r for r in rows if r.get(SUBTOTAL_LEVEL_KEY, "detail") != "detail"]


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
    is_flat_attribute_rollup: bool = False
    # Bug-9857: False when the client's set never contained the All member
    # (Excel's ``DrilldownLevel({[H].[H].[Year].Members}, [H].[H].[Year])``):
    # the rollup levels are served, the grand grain is not queried or emitted.
    include_all: bool = True
    # Bug-9891: True when the client placed one LEVEL's ``.Members`` of a
    # defined hierarchy (``[Geography].[Geography].[City].Members``) next to
    # rollups on the other axis. The level chain is registered so the
    # coverage invariant sees the hierarchy and the detail query carries the
    # ancestor columns; no grain above the named level is queried or
    # emitted -- the client asked for that level only.
    leaf_only: bool = False


class RollupGrainBudgetExceeded(Exception):
    """The requested rollup lattice exceeds the configured query budget (F4)."""

    def __init__(self, planned: int, budget: int, hierarchies: int):
        super().__init__(
            f"{planned} rollup grain queries planned for {hierarchies} "
            f"hierarchies; budget is {budget}"
        )
        self.planned = planned
        self.budget = budget
        self.hierarchies = hierarchies


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

    Bug-9766: Excel's real PivotTable subtotal request is
    ``[Dim].[Hier].[(All)].Members`` -- an EXPLICIT ``(All)`` level
    qualifier, not the bare two-part form. Semantically this means the
    same thing as ``[Dim].[Hier].Members`` (a full-hierarchy expansion
    from the All level down, per this gateway's own established handling
    of ``.[(All)].Members`` elsewhere) and must trigger subtotal
    detection the same way. Before this fix, the level-scoped exclusion
    below (Bug-6892: ``[Dim].[Hier].[Year].Members`` must NOT trigger
    subtotals) treated the literal ``(All)`` bracket exactly like a real
    level name and excluded it too, so ``subtotal_hierarchies`` came back
    empty for every real Excel PivotTable subtotal request, the flat
    detail-only SQL was used with no rollup query, and every subtotal /
    grand-total row rendered as a label with a blank value. The regex now
    consumes an optional ``.[(All)]`` segment before the level-scoped
    negative lookahead, so ``(All)`` no longer masquerades as a level name
    while a genuine level (``[Year]``, ``[Month]``, ...) still does.

    Scoped deliberately to genuine multi-level HIERARCHIES only (entries
    resolvable via ``hierarchy_level_dim_map``) -- a plain flat attribute
    dimension (e.g. ``account_type``) does NOT trigger subtotal detection
    here, even in its self-qualified ``[name].[name].[(All)].Members``
    form. See ``detect_flat_attribute_rollups`` below for that case, kept
    as a DELIBERATELY SEPARATE detector and result list. An earlier
    version of this fix folded flat attributes into THIS function's
    results and was reverted before shipping: doing so made
    ``bool(subtotal_hierarchies)`` -- which also gates the
    semi-additive/LAST_NON_EMPTY hidden-time-grain repair, the "no time
    dimension available" fail-loud guard, and empty-axis-member
    restoration in ``xmla_server.py`` -- true for queries that have
    nothing to do with time at all. A flat, non-time attribute has no
    temporal ordering, so it must never implicitly signal "this axis
    provides the LNE time key": the reverted version silently computed a
    naive SUM instead of the required LNE value, and silently skipped a
    fault that should have fired. Keeping detection separate lets callers
    combine the two result lists for query generation/merging (both
    produce the same ``SubtotalHierarchy`` shape and share the same,
    already-tested grain machinery) while keeping ``subtotal_hierarchies``
    itself -- the thing every LNE-related gate reads -- strictly
    hierarchy-only.

    Each returned SubtotalHierarchy carries ``axis`` (0=columns, 1=rows)
    so callers can build subtotal tuples on the correct XMLA axis.
    """
    results: list[SubtotalHierarchy] = []
    seen: set[str] = set()

    for axis_idx, axis_expr in ((0, col_expr), (1, row_expr)):
        for m in _SUBTOTAL_MEMBERS_RE.finditer(axis_expr):
            dim_part = m.group(1).strip()
            hier_part = m.group(2).strip()

            for hkey in [hier_part.lower(), dim_part.lower()]:
                if hkey in seen:
                    continue
                level_map = hierarchy_level_dim_map.get(hkey)
                if not level_map:
                    continue

                # Bug-6892: the two-part form is ambiguous. [Dim].[Hier].Members
                # is a whole-hierarchy expansion (subtotals), but Excel emits a
                # LEVEL-scoped request as [Hier].[Level].Members — same shape.
                # When the FIRST part resolved to the hierarchy and the second
                # part names one of its levels, the client asked for that level
                # only; treating it as a full expansion grouped the SQL by every
                # grain (a Year pivot came back at day grain — wrong numbers).
                if (
                    hkey == dim_part.lower()
                    and hier_part.lower() != dim_part.lower()
                    and hier_part.lower() in level_map
                ):
                    continue

                # Bug-7596: symmetric guard for when hkey == hier_part and
                # dim_part is a level of that hierarchy.
                if (
                    hkey == hier_part.lower()
                    and dim_part.lower() != hier_part.lower()
                    and dim_part.lower() in level_map
                ):
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


def detect_flat_attribute_rollups(
    col_expr: str,
    row_expr: str,
    standalone_attribute_names: set[str],
) -> list[SubtotalHierarchy]:
    """Detect MDX axes requesting a rollup over a FLAT (non-hierarchy)
    attribute dimension -- Bug-9766's remaining scope after
    ``detect_subtotal_hierarchies`` was fixed for genuine hierarchies.

    Excel's real request shape for a flat attribute (e.g. ``account_type``)
    dragged into Rows with subtotals on is the same self-qualified
    ``[name].[name].[(All)].Members`` / ``[name].[name].Members`` form a
    genuine hierarchy uses -- but ``account_type`` has no entry in
    ``hierarchy_level_dim_map`` (that map is built only from real
    multi-level ``HierarchyDefinition`` objects), so
    ``detect_subtotal_hierarchies`` correctly never matches it. This is a
    DELIBERATELY SEPARATE function, not a fallback branch bolted onto that
    one -- see its docstring for why: folding both cases into one result
    list made that list's truthiness silently gate LAST_NON_EMPTY safety
    logic that has nothing to do with a flat, non-time attribute.

    ``standalone_attribute_names`` MUST be derived the same way the
    Discover/catalogue surface classifies a dimension as a standalone
    attribute (``cube_model.build_cube_dimensions`` +
    ``cube_model.is_standalone_attribute``), not a raw dimension-name set.
    A raw set would also match a multi-level hierarchy's own level-backing
    column (e.g. ``business_date_calendar_year``) -- already excluded from
    the catalogue by Bug-6890's dedup because it duplicates a level the
    parent hierarchy already exposes -- and incorrectly offer it as an
    independently rollup-able attribute.

    Returns the SAME ``SubtotalHierarchy`` shape ``detect_subtotal_hierarchies``
    does (here always a single synthetic level named after the attribute
    itself: detail = the attribute's own column, All = the grand total) so
    the caller can feed both result lists through the existing, tested
    single-/multi-level grain-query and merge machinery
    (``build_subtotal_queries`` / ``build_multi_subtotal_queries`` /
    ``merge_grain_results`` / ``merge_multi_hierarchy_results``) for QUERY
    GENERATION and RESULT MERGING -- while keeping this list OUT of
    whatever variable a caller's LAST_NON_EMPTY gating reads. The caller is
    responsible for explicitly refusing (not silently executing) any query
    that combines a non-empty result here with a LAST_NON_EMPTY/semi-additive
    measure request, since this function only knows about requested axis
    grains, not the model's temporal metadata or which measures are
    involved.

    A LONE flat attribute on an axis (nothing else CrossJoin'd alongside
    it) does NOT trigger detection here, even though it uses the identical
    ``.Members`` / ``.[(All)].Members`` shape a genuine subtotal request
    does. Unlike a real hierarchy -- whose ``.Members`` unambiguously means
    "expand every level" because the modeller defined more than one level
    -- a flat attribute has only ONE level, and Excel sends this exact
    same shape whether or not the user wants a rollup: it is also just how
    Excel populates an ordinary single-field PivotTable axis. Verified
    against this gateway's own existing flat-pivot LAST_NON_EMPTY tests: a
    lone ``{[Product].[Product].Members}`` axis is the ALREADY-WORKING
    flat-pivot case (``_flat_lne_hidden_time_dim`` /
    ``_collapse_flat_lne_rows``), which folding into attribute_rollups
    would have wrongly refused as a fabricated LNE-plus-subtotal conflict.
    The unambiguous signal -- confirmed against the real, DEBUG-log-
    captured Excel MDX for the reported bug -- is a CrossJoin of two or
    more dimensions on the SAME axis: Excel only asks for BOTH dimensions'
    ``(All)``-qualified member sets together when it needs the server to
    compute the cross-nested subtotal it cannot derive client-side.

    SECOND TRIGGER (Bug-9777): ``DrilldownLevel({[d].[d].[All]})``. This
    shape became reachable only after Bug-9772 started advertising
    ``ALL_MEMBER``; before that Excel did not believe an All member existed
    and never asked to drill one. It is exempt from the two-dimension rule
    because it is not ambiguous in the first place -- the client had to NAME
    the All member to write it, which a plain field enumeration never does.
    Per the SSAS definition of ``DrilldownLevel`` (the argument set PLUS the
    children of its lowest level) the All member is a requested part of the
    result, not an artefact. Detecting it is what puts the grand-total row
    back on a single drilled-down field.

    Both triggers feed the SAME coverage rule enforced in the body: whatever
    causes an axis to roll up, EVERY dimension on that axis must end up in
    the returned list. See the comment on ``min_dims_for_rollup``.
    """
    results: list[SubtotalHierarchy] = []
    seen: set[str] = set()
    names_lower = {n.lower() for n in standalone_attribute_names}

    for axis_idx, axis_expr in ((0, col_expr), (1, row_expr)):
        # Bug-9777: an explicit DrilldownLevel on the All member is a rollup
        # request on its OWN, with no 2+-dimension requirement. The count rule
        # below exists only because a bare `.Members` is ambiguous with ordinary
        # field enumeration; naming the All member removes that ambiguity, so a
        # single drilled-down field must still get its All row and grand total.
        drilled_on_axis: set[str] = set()
        # See _SET_RESTRICTING_FN_RE: a restricted axis gets no All row rather
        # than a wrong one. Checked per axis, since a restriction on ROWS says
        # nothing about COLUMNS.
        axis_is_restricted = bool(_SET_RESTRICTING_FN_RE.search(axis_expr))
        if not axis_is_restricted:
            for m in _CALC_MEMBERS_ALL_RE.finditer(axis_expr):
                dim_part = m.group(1).strip()
                hier_part = m.group(2).strip()
                if dim_part.lower() == hier_part.lower() and dim_part.lower() in names_lower:
                    drilled_on_axis.add(dim_part.lower())
        for m in ([] if axis_is_restricted else _DRILLDOWN_ALL_RE.finditer(axis_expr)):
            dim_part = m.group(2).strip()
            hier_part = m.group(3).strip()
            key = dim_part.lower()
            if key != hier_part.lower() or key not in names_lower:
                continue
            drilled_on_axis.add(key)
            if key in seen:
                continue
            seen.add(key)
            results.append(SubtotalHierarchy(
                hierarchy_name=dim_part,
                mdx_dim_name=dim_part,
                mdx_hier_name=hier_part,
                levels=[SubtotalLevel(name=dim_part, ordinal=0, dim_name=dim_part)],
                axis=axis_idx,
                is_flat_attribute_rollup=True,
            ))

        axis_matches = list(_SUBTOTAL_MEMBERS_RE.finditer(axis_expr))
        distinct_dims_on_axis = {m.group(1).strip().lower() for m in axis_matches}
        # COVERAGE INVARIANT (Bug-9777): the rollup set must name EVERY
        # dimension on the axis. The grain queries built from it GROUP BY only
        # the rollup dimensions, and the merge keys the result rows on them, so
        # a rollup set covering a SUBSET of the axis silently drops the
        # uncovered dimension's column from the merged result -- the axis then
        # renders with that hierarchy missing entirely. This is why the count
        # rule below is a rule about COVERAGE, not about "is 2 enough to be
        # sure": with `.Members` alone every axis dimension matches or none
        # does, so requiring 2+ was both the ambiguity guard AND, incidentally,
        # full coverage. A DrilldownLevel names ONE dimension explicitly, so it
        # resolves the ambiguity for the axis without providing coverage --
        # hence it lowers the threshold to 1 rather than bypassing the loop,
        # pulling the CrossJoin'd siblings in alongside it.
        min_dims_for_rollup = 1 if drilled_on_axis else 2
        if len(distinct_dims_on_axis) < min_dims_for_rollup:
            continue
        for m in axis_matches:
            dim_part = m.group(1).strip()
            hier_part = m.group(2).strip()
            key = dim_part.lower()
            # A standalone attribute's canonical hierarchy/level/member
            # unique name is always [name].[name] (cube_model.py's
            # dimension_unique_name_for docstring) -- a mismatched pair
            # cannot be this dimension in its self-qualified form.
            if key != hier_part.lower() or key in seen or key not in names_lower:
                continue
            seen.add(key)
            results.append(SubtotalHierarchy(
                hierarchy_name=dim_part,
                mdx_dim_name=dim_part,
                mdx_hier_name=hier_part,
                levels=[SubtotalLevel(name=dim_part, ordinal=0, dim_name=dim_part)],
                axis=axis_idx,
                is_flat_attribute_rollup=True,
            ))

    return results


def _hierarchy_def_for_key(
    hkey: str,
    hierarchy_meta: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for h in hierarchy_meta:
        if (h.get("name") or "").strip().lower() == hkey:
            return h
    return None


def _drilldown_target_level(
    hier_def: dict[str, Any],
    level_map: dict[str, str],
) -> SubtotalLevel | None:
    """First data level below All for ``[H].[H].[(All)].Members`` (Bug-9764)."""
    raw_levels = sorted(
        hier_def.get("levels") or [],
        key=lambda lv: int(lv.get("ordinal", 0)),
    )
    for lvl in raw_levels:
        lname = (lvl.get("name") or "").strip()
        if not lname or lname.lower() in {"all", "(all)"}:
            continue
        dim_name = level_map.get(lname.lower())
        if dim_name:
            return SubtotalLevel(
                name=lname,
                ordinal=int(lvl.get("ordinal", 0)),
                dim_name=dim_name,
                time_unit=lvl.get("time_unit"),
            )
    return None


def _resolve_hierarchy_drilldown_key(
    dim_part: str,
    hier_part: str,
    hierarchy_level_dim_map: dict[str, dict[str, str]],
) -> tuple[str, dict[str, str]] | None:
    for hkey in (hier_part.lower(), dim_part.lower()):
        level_map = hierarchy_level_dim_map.get(hkey)
        if level_map:
            return hkey, level_map
    return None


@dataclass
class _DrilldownResolution:
    """One hierarchy's resolved ``DrilldownLevel`` chain on an axis."""
    dim_part: str
    hier_part: str
    hier_def: dict[str, Any]
    level_map: dict[str, str]
    depth: int  # number of data levels to serve (1 = first data level)
    include_all: bool


def _data_level_names(hier_def: dict[str, Any], level_map: dict[str, str]) -> list[str]:
    """Ancestor-first data level names the level map can serve."""
    names: list[str] = []
    for lvl in sorted(hier_def.get("levels") or [], key=lambda lv: int(lv.get("ordinal", 0))):
        lname = (lvl.get("name") or "").strip()
        if not lname or lname.lower() in {"all", "(all)"}:
            continue
        if level_map.get(lname.lower()):
            names.append(lname)
    return names


def _resolve_drilldown_chain(
    text: str,
    hierarchy_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    standalone_attribute_names: set[str],
) -> tuple[str, dict[str, _DrilldownResolution]]:
    """Collapse every hierarchy ``DrilldownLevel`` in ``text`` (Bug-9764/9857).

    Two base forms, both optionally carrying a target level:

    - ``DrilldownLevel({[H].[H].[All]} [, [H].[H].[L]])`` -- All plus the
      first data level, or every level down to ``L`` (SSAS semantics).
    - ``DrilldownLevel({[H].[H].[L1].Members} [, [H].[H].[L]])`` -- Excel's
      per-level expand: the members of ``L1`` and the level below the target
      (``L1`` when no target is given). The set never held All, so no All
      row is served.

    Each match is rewritten to the deepest level's ``.Members`` and the pass
    repeats, so a nested expand (``DrilldownLevel(DrilldownLevel(...))``,
    Month then Day) resolves from the inside out. Returns the rewritten text
    and, per hierarchy key, the depth and All-membership to register. A
    target level the hierarchy does not have leaves that match untouched.
    """
    resolved: dict[str, _DrilldownResolution] = {}
    if not text or "DrilldownLevel" not in text:
        return text, resolved

    def _lookup(dim_part: str, hier_part: str):
        hkey = _hierarchy_drilldown_all_lookup_key(
            dim_part, hier_part, standalone_attribute_names,
        )
        if not hkey:
            return None
        level_map = hierarchy_level_dim_map.get(hkey)
        hier_def = _hierarchy_def_for_key(hkey, hierarchy_meta) if level_map else None
        if not level_map or not hier_def:
            return None
        return hkey, hier_def, level_map

    for _ in range(8):  # bounded: each pass strips one nesting level
        replacements: list[tuple[int, int, str]] = []
        for pattern, base_is_all in ((_DRILLDOWN_ALL_RE, True), (_DRILLDOWN_LEVEL_SET_RE, False)):
            for m in pattern.finditer(text):
                dim_part = m.group(2).strip()
                hier_part = m.group(3).strip()
                found = _lookup(dim_part, hier_part)
                if not found:
                    continue
                hkey, hier_def, level_map = found
                names = _data_level_names(hier_def, level_map)
                lower = [n.lower() for n in names]
                if not names:
                    continue
                if base_is_all:
                    base_depth = 0
                    target = m.group(4)
                else:
                    base_name = unescape_member_key(m.group(4)).strip().lower()
                    if base_name not in lower:
                        continue
                    base_depth = lower.index(base_name) + 1
                    target = m.group(5)
                if target:
                    target_name = unescape_member_key(target).strip().lower()
                    if target_name not in lower:
                        continue
                    target_depth = lower.index(target_name) + 1
                    if base_is_all:
                        depth = target_depth
                    elif target_depth == base_depth:
                        depth = base_depth + 1
                    else:
                        # No member of the set sits at the target level:
                        # SSAS returns the set unchanged.
                        depth = base_depth
                else:
                    depth = base_depth + 1
                depth = min(depth, len(names))
                prior = resolved.get(hkey)
                resolved[hkey] = _DrilldownResolution(
                    dim_part=dim_part, hier_part=hier_part,
                    hier_def=hier_def, level_map=level_map,
                    depth=max(depth, prior.depth if prior else 0),
                    include_all=base_is_all or (prior.include_all if prior else False),
                )
                replacements.append((
                    m.start(), m.end(),
                    f"{{[{dim_part}].[{hier_part}].[{names[depth - 1]}].Members}}",
                ))
        if not replacements:
            break
        replacements.sort()
        out: list[str] = []
        last = 0
        for start, end, repl in replacements:
            if start < last:
                continue  # overlapping match from the other pattern
            out.append(text[last:start])
            out.append(repl)
            last = end
        out.append(text[last:])
        text = "".join(out)
    return text, resolved


def _levels_for_resolution(r: _DrilldownResolution) -> list[SubtotalLevel]:
    names = _data_level_names(r.hier_def, r.level_map)[: r.depth]
    by_name = {
        (lvl.get("name") or "").strip().lower(): lvl
        for lvl in (r.hier_def.get("levels") or [])
    }
    return [
        SubtotalLevel(
            name=n,
            ordinal=int(by_name[n.lower()].get("ordinal", 0)),
            dim_name=r.level_map[n.lower()],
            time_unit=by_name[n.lower()].get("time_unit"),
        )
        for n in names
    ]


def detect_drilldown_hierarchy_rollups(
    col_expr: str,
    row_expr: str,
    hierarchy_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    standalone_attribute_names: set[str],
) -> list[SubtotalHierarchy]:
    """Bug-9764 / Bug-9857: ``DrilldownLevel`` chains on a genuine hierarchy.

    Registers one ``SubtotalHierarchy`` per hierarchy carrying the data
    levels the chain resolves to (see ``_resolve_drilldown_chain``), so the
    grain machinery emits the ancestor rollups — and the All row only when the
    client's set contained it. Flat attributes stay on
    ``detect_flat_attribute_rollups``.
    """
    results: list[SubtotalHierarchy] = []
    seen: set[str] = set()
    for axis_idx, axis_expr in ((0, col_expr), (1, row_expr)):
        if _SET_RESTRICTING_FN_RE.search(axis_expr):
            continue
        _, resolved = _resolve_drilldown_chain(
            axis_expr, hierarchy_meta, hierarchy_level_dim_map,
            standalone_attribute_names,
        )
        for hkey, r in resolved.items():
            if hkey in seen:
                continue
            levels = _levels_for_resolution(r)
            if not levels:
                continue
            seen.add(hkey)
            results.append(SubtotalHierarchy(
                hierarchy_name=r.hier_def.get("name", ""),
                mdx_dim_name=r.dim_part,
                mdx_hier_name=r.hier_part,
                levels=levels,
                axis=axis_idx,
                include_all=r.include_all,
            ))
    return results


def rewrite_drilldown_level_hierarchy_all(
    statement: str,
    hierarchy_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    standalone_attribute_names: set[str],
) -> str:
    """Rewrite hierarchy ``DrilldownLevel`` chains to the deepest level's
    ``.Members`` (Bug-9764 / Bug-9857). Flat-attribute DrilldownLevel shapes
    are unchanged (Bug-9777 owns those). Idempotent."""
    text, _ = _resolve_drilldown_chain(
        statement, hierarchy_meta, hierarchy_level_dim_map,
        standalone_attribute_names,
    )
    return text


# Bug-9764: Excel's Year drag on a single hierarchy axis sends
# ``[H].[H].[(All)].Members`` (not ``DrilldownLevel``). SSAS semantics treat
# that as children of the All level (first data level only) — no All member
# row. Rewrite to level-1 ``.Members``; do NOT register a subtotal hierarchy
# (DrilldownLevel owns the All+children rollup path). CrossJoined 2+ ``.Members``
# axes stay untouched (Bug-9766 subtotal shape).
_EXPLICIT_ALL_MEMBERS_RE = re.compile(
    r'(?<!\]\.)'
    r'\[([^\]]+)\]\.\[([^\]]+)\]'
    r'\s*\.\s*\[\(\s*[Aa][Ll][Ll]\s*\)\]'
    r'(?!\s*\.\s*(?:\[|&\[))'
    r'\.(?:Members|MEMBERS|AllMembers)\b',
)


def _axis_qualifies_for_all_members_rewrite(
    axis_expr: str,
    standalone_attribute_names: set[str],
) -> bool:
    """True when a lone ``[(All)].Members`` on this axis is a level-1 drill."""
    if not axis_expr or _SET_RESTRICTING_FN_RE.search(axis_expr):
        return False
    explicit_keys: set[str] = set()
    for m in _EXPLICIT_ALL_MEMBERS_RE.finditer(axis_expr):
        hkey = _hierarchy_drilldown_all_lookup_key(
            m.group(1), m.group(2), standalone_attribute_names,
        )
        if hkey:
            explicit_keys.add(hkey)
    if len(explicit_keys) != 1:
        return False
    distinct_dims = {
        m.group(1).strip().lower() for m in _SUBTOTAL_MEMBERS_RE.finditer(axis_expr)
    }
    return len(distinct_dims) < 2


def detect_all_members_hierarchy_rollups(
    col_expr: str,
    row_expr: str,
    hierarchy_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    standalone_attribute_names: set[str],
) -> list[SubtotalHierarchy]:
    """Bug-9764 helper: identifies lone ``[(All)].Members`` drill axes.

    Not merged into ``subtotal_hierarchies`` — ``[(All)].Members`` requests
    first-level children only (no All row). Kept for unit tests and parity
    with ``rewrite_hierarchy_all_members_to_first_level`` eligibility.
    """
    results: list[SubtotalHierarchy] = []
    seen: set[str] = set()

    for axis_idx, axis_expr in ((0, col_expr), (1, row_expr)):
        if not _axis_qualifies_for_all_members_rewrite(
            axis_expr, standalone_attribute_names,
        ):
            continue
        for m in _EXPLICIT_ALL_MEMBERS_RE.finditer(axis_expr):
            dim_part = m.group(1).strip()
            hier_part = m.group(2).strip()
            hkey = _hierarchy_drilldown_all_lookup_key(
                dim_part, hier_part, standalone_attribute_names,
            )
            if not hkey or hkey in seen:
                continue
            level_map = hierarchy_level_dim_map.get(hkey)
            if not level_map:
                continue
            hier_def = _hierarchy_def_for_key(hkey, hierarchy_meta)
            if not hier_def:
                continue
            target = _drilldown_target_level(hier_def, level_map)
            if not target:
                continue
            seen.add(hkey)
            results.append(SubtotalHierarchy(
                hierarchy_name=hier_def.get("name", ""),
                mdx_dim_name=dim_part,
                mdx_hier_name=hier_part,
                levels=[target],
                axis=axis_idx,
            ))
    return results


def rewrite_hierarchy_all_members_to_first_level(
    statement: str,
    col_expr: str,
    row_expr: str,
    hierarchy_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    standalone_attribute_names: set[str],
) -> str:
    """Rewrite lone ``[(All)].Members`` on a hierarchy to level-1 ``.Members``."""
    if not statement or "Members" not in statement:
        return statement
    col_ok = _axis_qualifies_for_all_members_rewrite(
        col_expr, standalone_attribute_names,
    )
    row_ok = _axis_qualifies_for_all_members_rewrite(
        row_expr, standalone_attribute_names,
    )
    if not col_ok and not row_ok:
        return statement

    replacements: list[tuple[int, int, str]] = []
    for m in _EXPLICIT_ALL_MEMBERS_RE.finditer(statement):
        fragment = m.group(0)
        if not ((col_ok and fragment in col_expr) or (row_ok and fragment in row_expr)):
            continue
        dim_part = m.group(1).strip()
        hier_part = m.group(2).strip()
        hkey = _hierarchy_drilldown_all_lookup_key(
            dim_part, hier_part, standalone_attribute_names,
        )
        if not hkey:
            continue
        level_map = hierarchy_level_dim_map.get(hkey)
        if not level_map:
            continue
        hier_def = _hierarchy_def_for_key(hkey, hierarchy_meta)
        if not hier_def:
            continue
        target = _drilldown_target_level(hier_def, level_map)
        if not target:
            continue
        replacement = (
            f"[{dim_part}].[{hier_part}].[{target.name}].Members"
        )
        replacements.append((m.start(), m.end(), replacement))

    if not replacements:
        return statement
    out: list[str] = []
    last = 0
    for start, end, repl in replacements:
        out.append(statement[last:start])
        out.append(repl)
        last = end
    out.append(statement[last:])
    return "".join(out)


@dataclass
class DrilldownMemberPlan:
    """A PivotTable expand/collapse request (Bug-9783).

    ``DrilldownMember(base, targets, [hier])`` returns the base set PLUS, for
    each base member that is also in ``targets``, that member's children in
    ``[hier]``. Excel sends it when one field of a nested PivotTable is expanded
    or collapsed.

    The result is a MIXED-GRAIN set -- a rollup row for every outer member, and
    detail rows only for the DRILLED ones -- which is exactly what the existing
    grain-query/merge machinery produces. ``inner`` is therefore an ordinary
    ``SubtotalHierarchy`` that the caller feeds into the normal rollup path; the
    only extra information is WHICH outer members keep their detail rows.
    """
    inner: SubtotalHierarchy
    outer: SubtotalHierarchy
    outer_dim: str
    members: list[str]
    complement: bool

    @property
    def rollups(self) -> list[SubtotalHierarchy]:
        """Both dimensions, in axis order.

        COVERAGE INVARIANT (see detect_flat_attribute_rollups): a rollup set
        covering a SUBSET of the axis silently drops the uncovered dimension's
        column from the merged result, and the axis then renders with that
        hierarchy missing. Rolling up only the inner dimension produced exactly
        that -- an axis of 8 channel tuples against 33 cells. The OUTER
        dimension is genuinely a rollup here too: Excel's base set is
        ``{[outer].[All], [outer].[outer].Members}``, which names its All
        member, so the grand-total row is part of what was requested.
        """
        return [self.outer, self.inner]

    def is_drilled(self, outer_value: Any) -> bool:
        """Whether *outer_value* keeps its detail (expanded) rows.

        The outer ALL member is never drilled. Its row is the PivotTable's
        Grand Total, which shows a single figure; expanding it into one row per
        inner member would add rows the client never displays. An All-grain row
        carries no value for the outer column, which is what an empty value
        means here.
        """
        text = "" if outer_value is None else str(outer_value)
        if not text:
            return False
        hit = text in {str(m) for m in self.members}
        return (not hit) if self.complement else hit


def _split_call_arguments(expr: str, start: int) -> list[tuple[int, int]]:
    """Argument spans of the call whose opening paren is at *start*.

    Bracket-balanced rather than comma-split: every argument here is a nested
    set expression, and splitting on the first comma would cut inside one.
    Returns [] for unbalanced input so a statement this cannot parse is simply
    not recognised, rather than half-parsed into a wrong plan.
    """
    if start >= len(expr) or expr[start] != "(":
        return []
    spans: list[tuple[int, int]] = []
    depth = 0
    in_bracket = False
    arg_start = start + 1
    i = start
    while i < len(expr):
        ch = expr[i]
        if in_bracket:
            if ch == "]":
                if i + 1 < len(expr) and expr[i + 1] == "]":
                    i += 2
                    continue
                in_bracket = False
            i += 1
            continue
        if ch == "[":
            in_bracket = True
        elif ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
            if depth == 0:
                spans.append((arg_start, i))
                return spans
        elif ch == "," and depth == 1:
            spans.append((arg_start, i))
            arg_start = i + 1
        i += 1
    return []


_DDM_CALL_RE = re.compile(r'\bDrilldownMember\s*\(', re.IGNORECASE)
# The hierarchy argument: a self-qualified `[X].[X]`, optionally trailing.
_DDM_HIER_RE = re.compile(r'^\s*\[([^\]]+)\]\.\[([^\]]+)\]\s*$')
# Candidate target member wire forms. The canonical parser owns interpretation;
# this expression only finds complete references inside the target set.
_DDM_TARGET_RE = re.compile(
    r'\[(?:[^\]]|\]\])+\]\.\[(?:[^\]]|\]\])+\](?:'
    r'\.\[(?:[^\]]|\]\])+\]\.?' + KEY_PATH
    + r'|\.?' + KEY_PATH
    + r'|\.\[(?:[^\]]|\]\])+\](?!\s*\.))'
)


# Bug-9873: Excel's expand (+) on a member of a PLACED hierarchy sends the
# two-argument form on the same hierarchy:
#   DrilldownMember({{{[H].[H].[Country].Members}}}, {[H].[H].[Country].&[GB], ...})
# and, one level deeper, nests the previous result as the base. The children
# are the next level of the same hierarchy, so the shape is unambiguous.
_DDM_LEVEL_BASE_RE = re.compile(
    r'^\{+\s*\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]'
    r'\s*\.\s*\[((?:[^\]]|\]\])+)\]\s*\.\s*(?:Members|MEMBERS)\s*\}+$',
    re.IGNORECASE,
)


@dataclass
class HierarchyDrilldownMembers:
    """Which members of a placed hierarchy are expanded, per level (Bug-9873).

    ``keep[i]`` governs the rows at data level ``i`` (0 = first data level):
    ``(complement, ancestor_paths)`` -- a row is kept when the tuple of its
    ancestor keys (levels ``0..i-1``) is in ``ancestor_paths``, inverted when
    ``complement`` (Excel's ``{-{X}}`` collapse form). Levels without a rule
    keep every row.
    """
    dim_part: str
    hier_part: str
    hier_def: dict[str, Any]
    level_map: dict[str, str]
    depth: int  # number of data levels served
    keep: dict[int, tuple[bool, set[tuple[str, ...]]]]


def _resolve_drilldown_member_hierarchy(
    text: str,
    hierarchy_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    standalone_attribute_names: set[str],
) -> tuple[str, dict[str, HierarchyDrilldownMembers]]:
    """Collapse same-hierarchy ``DrilldownMember`` calls to the deepest level's
    ``.Members`` (inside out), recording which members are expanded.

    Flat-attribute forms (the three-argument cross-dimension plan, Bug-9783)
    are untouched: the base must be a LEVEL set of a defined hierarchy.
    """
    resolved: dict[str, HierarchyDrilldownMembers] = {}
    if not text or "DrilldownMember" not in text:
        return text, resolved
    for _ in range(8):
        changed = False
        for m in list(_DDM_CALL_RE.finditer(text)):
            args = _split_call_arguments(text, m.end() - 1)
            if len(args) != 2:
                continue
            base_text = text[args[0][0]:args[0][1]].strip()
            target_text = text[args[1][0]:args[1][1]].strip()
            bm = _DDM_LEVEL_BASE_RE.match(base_text)
            if not bm:
                continue  # nested or flat base: resolved on a later pass, or not ours
            dim_part, hier_part = bm.group(1).strip(), bm.group(2).strip()
            hkey = _hierarchy_drilldown_all_lookup_key(dim_part, hier_part, standalone_attribute_names)
            level_map = hierarchy_level_dim_map.get(hkey) if hkey else None
            hier_def = _hierarchy_def_for_key(hkey, hierarchy_meta) if level_map else None
            if not level_map or not hier_def:
                continue
            names = _data_level_names(hier_def, level_map)
            lower = [n.lower() for n in names]
            base_level = unescape_member_key(bm.group(3)).strip().lower()
            if base_level not in lower or lower.index(base_level) >= len(names) - 1:
                continue
            base_idx = lower.index(base_level)
            complement = bool(re.match(r'^\s*\{\s*-', target_text))
            paths: set[tuple[str, ...]] = set()
            for tm in _DDM_TARGET_RE.finditer(target_text):
                _, level_name, grammar, key_path = parse_member_uname(tm.group(0))
                if grammar not in {"key", "caption"} or not key_path:
                    continue
                if level_name and level_name.lower() != base_level:
                    continue
                paths.add(tuple(key_path))
            if not paths:
                continue
            # Find the enclosing call span: from "DrilldownMember" to the
            # closing paren of its argument list.
            close = args[-1][1]
            while close < len(text) and text[close] != ")":
                close += 1
            depth = base_idx + 2
            prior = resolved.get(hkey)
            keep = dict(prior.keep) if prior else {}
            keep[base_idx + 1] = (complement, paths)
            resolved[hkey] = HierarchyDrilldownMembers(
                dim_part=dim_part, hier_part=hier_part, hier_def=hier_def,
                level_map=level_map, depth=max(depth, prior.depth if prior else 0),
                keep=keep,
            )
            text = text[:m.start()] + f"{{[{dim_part}].[{hier_part}].[{names[base_idx + 1]}].Members}}" + text[close + 1:]
            changed = True
            break
        if not changed:
            break
    return text, resolved


def hierarchy_drilldown_rollup(r: HierarchyDrilldownMembers, axis: int) -> SubtotalHierarchy:
    """The rollup registration for an expanded hierarchy: its data levels down
    to the drilled depth, no All grain (the base set never held All)."""
    return SubtotalHierarchy(
        hierarchy_name=r.hier_def.get("name", ""),
        mdx_dim_name=r.dim_part,
        mdx_hier_name=r.hier_part,
        levels=_levels_for_resolution(_DrilldownResolution(
            dim_part=r.dim_part, hier_part=r.hier_part, hier_def=r.hier_def,
            level_map=r.level_map, depth=r.depth, include_all=False,
        )),
        axis=axis,
        include_all=False,
    )


def apply_hierarchy_drilldown_members(
    rows: list[dict[str, Any]],
    rollup: SubtotalHierarchy,
    drill: HierarchyDrilldownMembers,
) -> list[dict[str, Any]]:
    """Keep detail rows only under the expanded members (Bug-9873).

    Post-merge, like ``apply_drilldown_member_plan``: the member list never
    reaches the SQL. Rows at a level with no rule are kept as they are.
    """
    grain_key = SUBTOTAL_GRAIN_PREFIX + rollup.hierarchy_name
    ordinal_to_idx = {lvl.ordinal: i for i, lvl in enumerate(rollup.levels)}
    kept: list[dict[str, Any]] = []
    for row in rows:
        grain = row.get(grain_key, row.get(SUBTOTAL_GRAIN_KEY))
        idx = ordinal_to_idx.get(grain)
        rule = drill.keep.get(idx) if idx is not None else None
        if rule is None:
            kept.append(row)
            continue
        complement, paths = rule
        path = tuple(str(row.get(lvl.dim_name, "")) for lvl in rollup.levels[:idx])
        if (path in paths) != complement:
            kept.append(row)
    return kept


def detect_drilldown_member_plan(
    col_expr: str,
    row_expr: str,
    standalone_attribute_names: set[str],
) -> DrilldownMemberPlan | None:
    """Recognise Excel's PivotTable expand/collapse request (Bug-9783).

    Returns ``None`` for anything not confidently recognised -- an unparseable
    call, a hierarchy argument that is not a known standalone attribute, targets
    naming more than one outer dimension, or a target set with no members. The
    caller then behaves exactly as before, so a shape this does not understand
    degrades to the current no-op rather than to a wrong row set.

    SCOPE: flat attribute outer x flat attribute inner, which is what Excel
    sends for the reported case. Multi-level hierarchies are deliberately not
    matched here -- they are owned by ``detect_subtotal_hierarchies`` and must
    keep their existing behaviour.

    That boundary is also what makes the simple target parsing safe. A
    multi-level hierarchy echoes back a PATH-QUALIFIED key
    (``[Date].[Cal].[Month].&[2025]&[4]``) whose member is the DEEPEST key, and
    reading the first key there would name the year instead of the month. A
    single-level attribute has no such path, so the case cannot arise on this
    route. If the scope is ever widened to hierarchies, the target parsing must
    take the deepest key -- this note is the surviving record of that
    requirement, which previously lived only in tests around an unwired
    function (removed with it; see Bug-9783).
    """
    names_lower = {n.lower() for n in standalone_attribute_names}

    for axis_idx, axis_expr in ((0, col_expr), (1, row_expr)):
        if not axis_expr:
            continue
        m = _DDM_CALL_RE.search(axis_expr)
        if not m:
            continue
        args = _split_call_arguments(axis_expr, m.end() - 1)
        if len(args) < 3:
            # A 2-argument DrilldownMember does not name the hierarchy to drill,
            # so the inner dimension is ambiguous. Not guessed.
            continue

        base_text = axis_expr[args[0][0]:args[0][1]]
        target_text = axis_expr[args[1][0]:args[1][1]]
        hier_text = axis_expr[args[2][0]:args[2][1]]

        hm = _DDM_HIER_RE.match(hier_text)
        if not hm or hm.group(1).strip().lower() != hm.group(2).strip().lower():
            continue
        inner_name = hm.group(1).strip()
        if inner_name.lower() not in names_lower:
            continue

        # `{-{X}}` is set difference with an empty left side: "every base member
        # EXCEPT X". A plain `{X}` means "only X". Excel uses the first form to
        # collapse and the second to expand.
        complement = bool(re.match(r'^\s*\{\s*-', target_text))

        outer_dims: set[str] = set()
        members: list[str] = []
        for tm in _DDM_TARGET_RE.finditer(target_text):
            hier_bracket, level_name, grammar, key_path = parse_member_uname(
                tm.group(0)
            )
            if grammar not in {"key", "caption"} or not key_path:
                continue
            outer_dim = first_bracket_body(hier_bracket)
            if outer_dim is None:
                continue
            # A flat hierarchy can repeat its attribute name as an explicit
            # level. A different level name denotes a multi-level hierarchy
            # and stays on the subtotal hierarchy path.
            if level_name is not None and level_name.lower() != outer_dim.lower():
                continue
            outer_dims.add(outer_dim.lower())
            members.append(key_path[-1])

        if len(outer_dims) != 1 or not members:
            continue
        outer_dim = outer_dims.pop()
        if outer_dim == inner_name.lower() or outer_dim not in names_lower:
            continue
        # The base set must actually carry both dimensions, or this is not the
        # nested-PivotTable shape and the grain plan below would not describe it.
        bl = base_text.lower()
        if f"[{outer_dim}]" not in bl or f"[{inner_name.lower()}]" not in bl:
            continue

        canonical_inner = next(
            (n for n in standalone_attribute_names if n.lower() == inner_name.lower()),
            inner_name,
        )
        canonical_outer = next(
            (n for n in standalone_attribute_names if n.lower() == outer_dim),
            outer_dim,
        )
        return DrilldownMemberPlan(
            outer=SubtotalHierarchy(
                hierarchy_name=canonical_outer,
                mdx_dim_name=canonical_outer,
                mdx_hier_name=canonical_outer,
                levels=[SubtotalLevel(
                    name=canonical_outer, ordinal=0, dim_name=canonical_outer)],
                axis=axis_idx,
            ),
            inner=SubtotalHierarchy(
                hierarchy_name=canonical_inner,
                mdx_dim_name=canonical_inner,
                mdx_hier_name=canonical_inner,
                levels=[SubtotalLevel(
                    name=canonical_inner, ordinal=0, dim_name=canonical_inner)],
                axis=axis_idx,
            ),
            outer_dim=canonical_outer,
            members=members,
            complement=complement,
        )
    return None


def apply_drilldown_member_plan(
    rows: list[dict[str, Any]],
    plan: DrilldownMemberPlan,
) -> list[dict[str, Any]]:
    """Drop detail rows for outer members that are NOT drilled.

    Applied AFTER the merge, deliberately, rather than as a WHERE on the detail
    grain query: the defect this feature grew out of was a member list wrongly
    reaching the SQL and inverting the result, so the member list stays out of
    the SQL entirely.

    Rollup rows (the inner hierarchy at its All grain, ordinal ``-1``) are kept
    for EVERY outer member -- that is what makes a collapsed member still show
    its own total, and what keeps every sibling's group header present.
    """
    grain_key = SUBTOTAL_GRAIN_PREFIX + plan.inner.hierarchy_name
    kept: list[dict[str, Any]] = []
    for row in rows:
        grain = row.get(grain_key, row.get(SUBTOTAL_GRAIN_KEY))
        if grain == -1:
            kept.append(row)
            continue
        if plan.is_drilled(row.get(plan.outer_dim, "")):
            kept.append(row)
    return kept


_LEVEL_SET_RE = re.compile(
    r'\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]'
    r'\s*\.\s*\[((?:[^\]]|\]\])+)\]\s*\.\s*(?:Members|MEMBERS)\b',
)


def detect_level_set_hierarchies(
    col_expr: str,
    row_expr: str,
    hierarchy_meta: list[dict[str, Any]],
    hierarchy_level_dim_map: dict[str, dict[str, str]],
    registered: set[str],
) -> list[SubtotalHierarchy]:
    """Leaf-only registrations for ``[H].[H].[Level].Members`` sets (Bug-9891).

    Excel places a hierarchy level that is expanded to one grain as that
    level's ``.Members`` (a Geography column axis expanded to City). No rollup
    detector recognises that shape, so when the OTHER axis carries rollups
    (three flat fields on rows, each ``DrilldownLevel`` on All) the coverage
    guard saw an uncovered ``city_name`` and dropped every rollup: all of the
    row subtotals and the grand total vanished, and Excel rendered the
    group header rows blank.

    Each hit becomes a ``SubtotalHierarchy`` whose levels run from the first
    data level down to the named level, ``include_all=False`` and
    ``leaf_only=True``: the grain builders serve exactly the named level, the
    detail query gains the ancestor columns for parent assignment, and the
    coverage invariant holds. Hierarchies in ``registered`` (already served by
    another detector) and undefined hierarchies are left alone.
    """
    results: list[SubtotalHierarchy] = []
    seen = {k.lower() for k in registered}
    for axis_idx, axis_expr in ((0, col_expr), (1, row_expr)):
        for m in _LEVEL_SET_RE.finditer(axis_expr or ""):
            dim_part, hier_part = m.group(1).strip(), m.group(2).strip()
            level_name = unescape_member_key(m.group(3)).strip()
            if level_name.lower() in {"all", "(all)"}:
                continue
            resolved = _resolve_hierarchy_drilldown_key(dim_part, hier_part, hierarchy_level_dim_map)
            if not resolved:
                continue
            hkey, level_map = resolved
            if hkey in seen or level_name.lower() not in level_map:
                continue
            hier_def = _hierarchy_def_for_key(hkey, hierarchy_meta)
            if not hier_def:
                continue
            names = [n.lower() for n in _data_level_names(hier_def, level_map)]
            if level_name.lower() not in names:
                continue
            levels = _levels_for_resolution(_DrilldownResolution(
                dim_part=dim_part, hier_part=hier_part, hier_def=hier_def,
                level_map=level_map, depth=names.index(level_name.lower()) + 1,
                include_all=False,
            ))
            if not levels:
                continue
            seen.add(hkey)
            results.append(SubtotalHierarchy(
                hierarchy_name=hier_def.get("name", ""),
                mdx_dim_name=dim_part,
                mdx_hier_name=hier_part,
                levels=levels,
                axis=axis_idx,
                include_all=False,
                leaf_only=True,
            ))
    return results


# Bug-9902: a standalone attribute's own member set, in every spelling Excel
# sends for it. Two-part ``[a].[a].Members`` is the ungrouped form; the
# three-part group is the attribute's SINGLE self-named level
# (``build_cube_dimensions`` gives a flat dimension one level named after
# itself) or the ``[(All)]`` level, which is what the wire name
# ``[Dimensions].[a].[a].Members`` becomes once ``_normalize_wire_mdx`` has
# swapped the group node for the internal ``[a].[a]`` prefix. A member
# reference (``[a].[a].[CREDIT].Members``) is excluded because CREDIT is not a
# level of the attribute; a key reference (``&[x]``) never matches at all.
_FLAT_MEMBER_SET_RE = re.compile(
    r'\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]'
    r'(?:\s*\.\s*\[((?:[^\]]|\]\])+)\])?'
    r'\s*\.\s*(?:Members|MEMBERS|AllMembers|ALLMEMBERS)\b',
)


def detect_flat_member_set_rollups(
    col_expr: str,
    row_expr: str,
    standalone_attribute_names: set[str],
    registered: set[str],
) -> list[SubtotalHierarchy]:
    """Leaf-only registrations for a bare flat-attribute member set (Bug-9902).

    The Bug-9891 shape in its second spelling. Excel sends a standalone
    attribute placed with subtotals OFF (or simply fully listed) as its own
    ``.Members`` set, and ``detect_flat_attribute_rollups`` deliberately
    refuses to claim it: a lone flat ``.Members`` axis is also how Excel
    populates an ordinary single-field pivot, so treating it as a rollup
    request would fabricate an All row nobody asked for (see that function's
    docstring). It therefore claims such a set only when the SAME axis carries
    a ``DrilldownLevel`` or a second attribute -- which leaves the set
    unclaimed when the rollup lives on the OTHER axis, and leaves the
    ``[a].[a].[a].Members`` (grouped wire) spelling unclaimed on any axis.

    The Bug-9785 coverage guard then saw an uncovered dimension and dropped
    EVERY rollup in the query. Since Bug-9862 F6 that omission is no longer
    silent: the response validator raises a SOAP fault naming the grain the
    client asked for and did not get.

    Each unclaimed hit becomes a ``SubtotalHierarchy`` with the attribute's
    single level, ``include_all=False`` and ``leaf_only=True``: it COVERS the
    axis, contributes NO grain of its own (``grain_options_for_hierarchies``
    stops at the detail option), and the other field keeps its full lattice.

    Called ONLY when another detector already produced a rollup -- a flat
    member set alone must keep the plain path exactly as it is. ``registered``
    holds the dimension names already claimed, so a set that
    ``detect_flat_attribute_rollups`` legitimately rolls up (its own two-or-
    more-dimension / DrilldownLevel rule) keeps its full All grain and is not
    downgraded here.

    A set-RESTRICTING function on the axis (``_SET_RESTRICTING_FN_RE``) is left
    alone for the same reason that function refuses it: the other axis's grand
    total would then be computed over rows the restricted axis does not show,
    and a missing subtotal is a display gap where a wrong one is a wrong number.
    """
    results: list[SubtotalHierarchy] = []
    names_lower = {n.lower() for n in standalone_attribute_names}
    seen = {k.lower() for k in registered}
    for axis_idx, axis_expr in ((0, col_expr), (1, row_expr)):
        if not axis_expr or _SET_RESTRICTING_FN_RE.search(axis_expr):
            continue
        for m in _FLAT_MEMBER_SET_RE.finditer(axis_expr):
            dim_part, hier_part = m.group(1).strip(), m.group(2).strip()
            key = dim_part.lower()
            if key != hier_part.lower() or key in seen or key not in names_lower:
                continue
            level_part = (m.group(3) or "").strip().lower()
            if level_part and level_part not in {key, "all", "(all)"}:
                continue
            seen.add(key)
            results.append(SubtotalHierarchy(
                hierarchy_name=dim_part,
                mdx_dim_name=dim_part,
                mdx_hier_name=hier_part,
                levels=[SubtotalLevel(name=dim_part, ordinal=0, dim_name=dim_part)],
                axis=axis_idx,
                is_flat_attribute_rollup=True,
                include_all=False,
                leaf_only=True,
            ))
    return results


def uncovered_axis_dimensions(
    axis_dims: set[str] | list[str],
    rollups: list[SubtotalHierarchy],
) -> set[str]:
    """Axis dimensions that no rollup in *rollups* names.

    THE COVERAGE INVARIANT, in one place. Grain queries GROUP BY only the
    rollup dimensions and the merge keys its rows on them, so a rollup set
    covering a SUBSET of the axis silently drops the uncovered dimension's
    column. The axis is then emitted with that hierarchy missing from AxisInfo
    while its tuples still reference it -- and Excel does not degrade on that,
    it CRASHES.

    A non-empty result means the caller must NOT roll up. Failing safe drops
    the rollups, never the query: the plain path emits every hierarchy
    correctly and merely lacks the All/subtotal rows. A missing subtotal row is
    a display gap; a malformed axis takes the client down.

    This exists because the invariant was violated three times in three
    different ways -- a CrossJoin sibling (Bug-9777), the outer dimension of a
    drill plan (Bug-9783), and any time dimension or user hierarchy sharing an
    axis with a flat attribute (Bug-9785). The detectors recognise standalone
    ATTRIBUTES by design, so the third class can never be covered by them; that
    is why the check belongs here, after detection, rather than inside each
    detector.
    """
    rollup_dims = {lvl.dim_name for r in rollups for lvl in r.levels}
    return set(axis_dims) - rollup_dims


def build_subtotal_queries(
    *,
    mdx_dims: list[str],
    mdx_measures: list[str],
    where_sql_clauses: list[str],
    model_slug: str,
    measures_meta: list[dict[str, Any]],
    hierarchy: SubtotalHierarchy,
    measure_canonical: dict[str, str],
    connector_type: str = "postgresql",
) -> list[GrainQuery]:
    """Generate SQL queries at each intermediate and grand-total grain.

    Does NOT generate the detail query (the caller uses the existing one).
    LAST_NON_EMPTY measures are aggregated with SUM in these queries ---
    the gateway replaces those values with Python-computed LAST_NON_EMPTY
    from the detail results afterward.
    """
    measure_agg: dict[str, str] = {}
    for m_meta in measures_meta:
        mname = m_meta.get("name", "")
        if mname:
            measure_agg[mname] = (m_meta.get("default_agg") or "sum").upper()

    hier_dim_names = {lvl.dim_name for lvl in hierarchy.levels}
    non_hier_dims = [d for d in mdx_dims if d not in hier_dim_names]

    queries: list[GrainQuery] = []
    if hierarchy.leaf_only:
        return queries  # Bug-9891: the named level is the only grain served

    for level_idx in range(len(hierarchy.levels) - 2, -1, -1):
        level = hierarchy.levels[level_idx]
        grain_dims = non_hier_dims + [
            l.dim_name for l in hierarchy.levels[: level_idx + 1]
        ]
        sql = _build_grain_sql(
            grain_dims, mdx_measures, measure_agg, measure_canonical,
            where_sql_clauses, model_slug,
            connector_type=connector_type,
        )
        queries.append(GrainQuery(
            sql=sql, protocol="jdbc",
            grain_ordinal=level.ordinal,
            level_name=level.name, dim_cols=list(grain_dims),
        ))

    if not hierarchy.include_all:
        # Bug-9857: the client's set never held the All member; serving a
        # grand row it did not ask for would put an unrequested tuple on
        # the axis.
        return queries

    grand_sql = _build_grain_sql(
        non_hier_dims, mdx_measures, measure_agg, measure_canonical,
        where_sql_clauses, model_slug,
        connector_type=connector_type,
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
    *,
    connector_type: str = "postgresql",
) -> str:
    def _q(name: str) -> str:
        return quote_identifier(connector_type, name)

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


def _sum_last_non_empty_values(values: list[Any]) -> Any:
    """Add peer values without changing the last-over-time decision.

    Values are normally numeric database values (often ``Decimal``). Integer
    values stay integers; all other numeric representations are summed through
    ``Decimal(str(value))`` so numeric strings and mixed integer/decimal values
    cannot accidentally concatenate or lose precision through ``float``.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return sum(values)
    try:
        total = sum(
            (Decimal(str(value).strip()) for value in values),
            Decimal("0"),
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            "LAST_NON_EMPTY peer values must be numeric to apply additive rollup"
        ) from exc
    return total


def _group_lne(
    group_rows: list[dict[str, Any]],
    measures: list[str],
    finest_dim: str,
    peer_dims: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate LNE per non-time peer, then add the peer values.

    Semi-additive measures are last over time but additive across every
    non-time peer that was rolled out of the requested grain. A single global
    ``_last_non_empty_value`` would select one peer's value when peers share the
    latest period, or discard a peer whose own last value is from an earlier
    period. Keeping peer partitioning here makes the rule apply identically to
    intermediate subtotals and grand totals.
    """
    if not peer_dims:
        return {m: _last_non_empty_value(group_rows, m, finest_dim) for m in measures}

    peer_groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in group_rows:
        peer_key = tuple(str(row.get(dim, "")) for dim in peer_dims)
        peer_groups.setdefault(peer_key, []).append(row)

    values: dict[str, Any] = {}
    for measure in measures:
        peer_values = [
            value
            for peer_rows in peer_groups.values()
            if (value := _last_non_empty_value(peer_rows, measure, finest_dim)) is not None
            and str(value).strip() != ""
        ]
        values[measure] = _sum_last_non_empty_values(peer_values)
    return values


def compute_last_non_empty_subtotals(
    detail_rows: list[dict[str, Any]],
    hierarchy: SubtotalHierarchy,
    last_non_empty_measures: list[str],
    non_hier_dims: list[str] | None = None,
) -> dict[int, dict[tuple, dict[str, Any]]]:
    """Compute LAST_NON_EMPTY values from detail rows for each grain level.

    For each subtotal grain, groups detail rows by the full grain dimensions
    (non-hierarchy dims + hierarchy dims at this level). Any omitted
    non-temporal hierarchy dimensions are then treated as independent peers:
    each peer contributes its value from its own latest non-empty time period,
    and those peer values are added together.

    Returns: {grain_ordinal: {dim_val_tuple: {measure: value}}}
    """
    if not last_non_empty_measures or not detail_rows:
        return {}

    non_hier = non_hier_dims or []
    finest_dim = hierarchy.levels[-1].dim_name
    hierarchy_dims = [level.dim_name for level in hierarchy.levels]
    time_dims = {
        level.dim_name for level in hierarchy.levels if level.time_unit
    }
    result: dict[int, dict[tuple, dict[str, Any]]] = {}

    for level_idx in range(len(hierarchy.levels) - 2, -1, -1):
        level = hierarchy.levels[level_idx]
        grain_dims = non_hier + [l.dim_name for l in hierarchy.levels[: level_idx + 1]]

        groups: dict[tuple, list[dict[str, Any]]] = {}
        for row in detail_rows:
            key = tuple(str(row.get(d, "")) for d in grain_dims)
            groups.setdefault(key, []).append(row)

        level_vals: dict[tuple, dict[str, Any]] = {}
        peer_dims = [
            dim for dim in hierarchy_dims
            if dim not in grain_dims and dim not in time_dims
        ]
        for key, group_rows in groups.items():
            level_vals[key] = _group_lne(
                group_rows, last_non_empty_measures, finest_dim, peer_dims,
            )

        result[level.ordinal] = level_vals

    if last_non_empty_measures:
        if detail_rows:
            groups_gt: dict[tuple, list[dict[str, Any]]] = {}
            for row in detail_rows:
                key = tuple(str(row.get(d, "")) for d in non_hier)
                groups_gt.setdefault(key, []).append(row)
            gt_vals: dict[tuple, dict[str, Any]] = {}
            peer_dims = [
                dim for dim in hierarchy_dims
                if dim not in non_hier and dim not in time_dims
            ]
            for key, group_rows in groups_gt.items():
                gt_vals[key] = _group_lne(
                    group_rows, last_non_empty_measures, finest_dim, peer_dims,
                )
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


GRAIN_ALL = -1


def grain_options_for_hierarchies(
    hierarchies: list[SubtotalHierarchy],
) -> list[list[tuple[int, str, list[str]]]]:
    """The grain options each hierarchy contributes to the requested lattice.

    One list per hierarchy of ``(grain ordinal, level name, GROUP BY dims)``,
    finest first: the detail level, then each intermediate level the client
    asked for, then ``All`` when the requested set held the All member. The
    lattice the client asked for is the Cartesian product of these lists.

    Bug-9862 F6: this is the SINGLE producer of that lattice.
    ``build_multi_subtotal_queries`` plans from it and
    ``rollup_validator.expected_grain_lattice`` proves the response covered it,
    so the planner and its safety net can never disagree about what was asked
    for. A second hand-written copy is exactly how a validator ends up
    certifying the very omission it exists to catch.
    """
    options: list[list[tuple[int, str, list[str]]]] = []
    for h in hierarchies:
        opts: list[tuple[int, str, list[str]]] = []
        finest = h.levels[-1]
        opts.append((finest.ordinal, "detail", [l.dim_name for l in h.levels]))
        if h.leaf_only:  # Bug-9891: one level requested, one grain served
            options.append(opts)
            continue
        for level_idx in range(len(h.levels) - 2, -1, -1):
            level = h.levels[level_idx]
            opts.append((
                level.ordinal, level.name,
                [l.dim_name for l in h.levels[: level_idx + 1]],
            ))
        if h.include_all:  # Bug-9857: no All grain for a level-set expand
            opts.append((GRAIN_ALL, "All", []))
        options.append(opts)
    return options


def build_multi_subtotal_queries(
    *,
    mdx_dims: list[str],
    mdx_measures: list[str],
    where_sql_clauses: list[str],
    model_slug: str,
    measures_meta: list[dict[str, Any]],
    hierarchies: list[SubtotalHierarchy],
    measure_canonical: dict[str, str],
    connector_type: str = "postgresql",
    max_grain_queries: int | None = None,
) -> list[GrainQuery]:
    """Generate the subtotal grains for the requested axis.

    ``CrossJoin`` is a Cartesian product: for ``N`` flat fields on one axis the
    client asked for every combination of ``All``/detail per field, and a real
    engine returns them all - ``NON EMPTY`` is the only thing that removes a
    tuple, and only after the grain has actually been evaluated. Emitting fewer
    grains than requested silently deletes whole subtotal families
    (Bug-9845: 41 of 48 tuples for two flat fields, 76 of 126 for three).

    The all-detail combination is always skipped because the caller's
    original query covers it. (The nested-prefix pruning that the retired
    ``calculated-total`` profile needed, Bug-9244, was removed by Bug-9874.)
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

    grain_options = grain_options_for_hierarchies(hierarchies)

    detail_combo = tuple(opts[0] for opts in grain_options)

    # The grains that will actually execute: the full lattice the client asked
    # for, minus the detail combination the original query already covers.
    planned_combos = [
        combo for combo in _product(*grain_options)
        if combo != detail_combo
    ]

    # Deep-review F4 / B3: refuse past the budget with a typed error the
    # caller turns into a clear fault; never prune silently.
    if max_grain_queries is not None and len(planned_combos) > max_grain_queries:
        raise RollupGrainBudgetExceeded(
            len(planned_combos), max_grain_queries, len(hierarchies),
        )

    queries: list[GrainQuery] = []
    for combo in planned_combos:

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
            connector_type=connector_type,
        )
        queries.append(GrainQuery(
            sql=sql, protocol="jdbc",
            grain_ordinal=sum(c[0] for c in combo),
            level_name=label,
            dim_cols=grain_dims,
            grain_per_hierarchy=grain_spec,
        ))

    return queries


# ---------------------------------------------------------------------------
# Bug-9864: serve the whole lattice in ONE source operation
# ---------------------------------------------------------------------------

# Marker column the query-router projects per grain column. Producer
# (query-router ``api.routes.GROUPING_MARKER_PREFIX`` /
# ``rewrite.source_sql.GROUPING_MARKER_PREFIX``) and consumer (here) MUST derive
# the name identically -- a divergence would make every All row look like a
# member row whose value is NULL.
GROUPING_MARKER_PREFIX = "_grouping__"


def grouping_marker_name(dimension_name: str) -> str:
    """Marker column name the router projects for grain column *dimension_name*."""
    return f"{GROUPING_MARKER_PREFIX}{dimension_name}"


class LatticeUnavailable(Exception):
    """This rollup lattice cannot be served exactly as one grouping-sets query.

    Never a failure: the caller falls back to the bounded one-query-per-grain
    path, which returns exactly the same rows more slowly.
    """


@dataclass
class LatticeQuery:
    """One source operation covering every requested rollup grain.

    ``sql`` is an ordinary GROUP BY query at the UNION of every planned grain --
    the same shape, and the same layering over the persona model query, as the
    per-grain queries it replaces. ``grouping_sets`` rides alongside it on the
    execute request; the router renders the lattice and the markers on top of
    the bound query.

    ``grain_queries`` is the untouched output of ``build_multi_subtotal_queries``.
    Keeping it means ``merge_multi_hierarchy_results``, the LNE post-computation
    and ``rollup_validator`` receive exactly the objects the multi-query path
    produced, so nothing downstream can tell the two paths apart.
    """

    sql: str
    protocol: str
    dim_cols: list[str]
    grouping_sets: list[list[str]]
    grain_queries: list[GrainQuery]


def build_lattice_query(
    grain_queries: list[GrainQuery],
    *,
    mdx_measures: list[str],
    measures_meta: list[dict[str, Any]],
    measure_canonical: dict[str, str],
    where_sql_clauses: list[str],
    model_slug: str,
    connector_type: str = "postgresql",
) -> LatticeQuery:
    """Fold planned per-grain queries into one grouping-sets request.

    Raises :class:`LatticeUnavailable` when the fold would not be exact, so the
    caller keeps the multi-query path rather than returning different rows.
    """
    if not grain_queries:
        raise LatticeUnavailable("no rollup grains planned")

    # Two grains that GROUP BY the same columns are indistinguishable in a
    # single result set: their rows carry identical grouping markers, so the
    # split below could not tell them apart and would drop one grain's rows.
    seen: set[frozenset[str]] = set()
    for q in grain_queries:
        key = frozenset(q.dim_cols)
        if key in seen:
            raise LatticeUnavailable(
                "two planned grains group by the same columns"
            )
        seen.add(key)
        if not q.sql:
            # A measureless grand grain is synthesised in Python by the caller;
            # it has no SQL to fold into the lattice.
            raise LatticeUnavailable("a planned grain has no SQL")

    union_dims: list[str] = []
    for q in grain_queries:
        for d in q.dim_cols:
            if d not in union_dims:
                union_dims.append(d)
    if not union_dims:
        # One flat field asks for exactly one rollup grain: the grand total,
        # grouped by nothing. That grain is planned and served as normal -- it
        # is simply nothing to FOLD. One grain is already one query, and with no
        # grain column there is no GROUPING() marker to split the result by, so
        # folding would turn a working single query into an unsplittable one.
        raise LatticeUnavailable(
            "only one rollup grain, grouped by no columns; nothing to fold"
        )

    measure_agg: dict[str, str] = {}
    for m_meta in measures_meta:
        mname = m_meta.get("name", "")
        if mname:
            measure_agg[mname] = (m_meta.get("default_agg") or "sum").upper()

    sql = _build_grain_sql(
        union_dims, mdx_measures, measure_agg, measure_canonical,
        where_sql_clauses, model_slug,
        connector_type=connector_type,
    )
    if not sql:
        raise LatticeUnavailable("the lattice query produced no SQL")

    return LatticeQuery(
        sql=sql,
        protocol=grain_queries[0].protocol,
        dim_cols=union_dims,
        grouping_sets=[list(q.dim_cols) for q in grain_queries],
        grain_queries=list(grain_queries),
    )


def _marker_is_aggregated(value: Any) -> bool:
    """True when a GROUPING() marker says the column was rolled up (All).

    The marker, never the column's own NULL-ness, decides the grain. A member
    whose value IS NULL is a real member and must stay one; reading NULL as All
    is how a "(blank)" row silently becomes the grand total.
    """
    if value is None:
        raise LatticeUnavailable("a grouping marker came back NULL")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    text = str(value).strip().lower()
    if text in ("1", "true", "t"):
        return True
    if text in ("0", "false", "f"):
        return False
    raise LatticeUnavailable(f"unrecognised grouping marker value {value!r}")


def split_lattice_results(
    lattice: LatticeQuery,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> list[GrainResult]:
    """Split one lattice result set back into per-grain results.

    Each row's grouping markers name the columns it was actually grouped by;
    that set identifies exactly one planned grain. The returned results are
    indistinguishable from the ones the per-grain queries produced, which is
    what lets the merge, the LNE overrides and the validator stay untouched.
    """
    markers = {d: grouping_marker_name(d) for d in lattice.dim_cols}
    missing = [m for m in markers.values() if m not in columns]
    if missing:
        raise LatticeUnavailable(
            f"grouping markers missing from the result: {missing}"
        )

    marker_names = set(markers.values())
    measure_cols = [
        c for c in columns
        if c not in marker_names and c not in set(lattice.dim_cols)
    ]

    by_key: dict[frozenset[str], GrainQuery] = {
        frozenset(q.dim_cols): q for q in lattice.grain_queries
    }
    buckets: dict[frozenset[str], list[dict[str, Any]]] = {
        k: [] for k in by_key
    }

    for row in rows:
        grouped = frozenset(
            d for d in lattice.dim_cols
            if not _marker_is_aggregated(row.get(markers[d]))
        )
        if grouped not in by_key:
            raise LatticeUnavailable(
                f"result row grouped by {sorted(grouped)}, which is not a "
                "planned grain"
            )
        q = by_key[grouped]
        out = {d: row.get(d) for d in q.dim_cols}
        for m in measure_cols:
            out[m] = row.get(m)
        buckets[grouped].append(out)

    return [
        GrainResult(
            query=q,
            columns=list(q.dim_cols) + measure_cols,
            rows=buckets[frozenset(q.dim_cols)],
        )
        for q in lattice.grain_queries
    ]


def compute_multi_lne_subtotals(
    detail_rows: list[dict[str, Any]],
    hierarchies: list[SubtotalHierarchy],
    last_non_empty_measures: list[str],
    subtotal_queries: list[GrainQuery],
) -> dict[tuple, dict[tuple, dict[str, Any]]]:
    """Compute LAST_NON_EMPTY overrides for multi-hierarchy grain combinations.

    LAST_NON_EMPTY is last over the temporal hierarchy and additive across every
    non-temporal hierarchy peer omitted by the requested grain. This preserves
    all peer values when several peers share the latest time and when a peer's
    own latest non-empty value is older than the group's latest time.

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

    all_hier_dims: list[str] = []
    time_dims: set[str] = set()
    for hierarchy in hierarchies:
        for level in hierarchy.levels:
            if level.dim_name not in all_hier_dims:
                all_hier_dims.append(level.dim_name)
            if level.time_unit:
                time_dims.add(level.dim_name)

    all_dim_names = list(all_hier_dims)
    for query in subtotal_queries:
        for dim in query.dim_cols:
            if dim not in all_dim_names:
                all_dim_names.append(dim)

    result: dict[tuple, dict[tuple, dict[str, Any]]] = {}

    for sq in subtotal_queries:
        grain_dims = sq.dim_cols
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for row in detail_rows:
            key = tuple(str(row.get(d, "")) for d in grain_dims)
            groups.setdefault(key, []).append(row)

        level_vals: dict[tuple, dict[str, Any]] = {}
        peer_dims = [
            dim for dim in all_dim_names
            if dim not in grain_dims and dim not in time_dims
        ]
        for key, group_rows in groups.items():
            level_vals[key] = _group_lne(
                group_rows, last_non_empty_measures, finest_dim, peer_dims,
            )
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
