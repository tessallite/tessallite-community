"""XMLA DRILLTHROUGH handler.

Handles MDX ``DRILLTHROUGH`` statements arriving via the XMLA gateway.
Auto-hierarchy aware: calls ``/drill-options`` then ``/drill-through``
on the query-router, formats the result as an XMLA Rowset response.

Result columns: original SELECT columns + current drill dimension +
upper hierarchy level dimensions (toward the root).

Response format: flat ``<row>`` XML (XMLA Rowset), not MDDataSet.
"""
from __future__ import annotations

import logging
import re
from typing import Any, NamedTuple

from src.dax.member_uname import (
    KEY_PATH,
    is_all_member_token,
    parse_member_keys,
    unescape_member_key,
)

# Bracket body that tolerates the SSAS ``]]`` escape inside a bracketed name,
# mirroring member_uname._BRACKET_BODY. Used to extract member references whose
# name/key segments contain a literal ``]`` (Bug-1056).
_BRACKET_BODY_RE = r"(?:[^\]]|\]\])"
from src.dax.ts_mdx_parser import ParsedMDX
from src.router_client import (
    QueryRouterError,
    execute_drill_options,
    execute_drill_through,
)

logger = logging.getLogger(__name__)

_ROWSET_NS = "urn:schemas-microsoft-com:xml-analysis:rowset"
_SQL_NS = "urn:schemas-microsoft-com:xml-sql"


class DrillthroughResult(NamedTuple):
    """Outcome of an XMLA DRILLTHROUGH.

    ``next_cursor`` is the opaque keyset continuation token minted by the
    query-router for the NEXT page, or ``None`` when the result is complete
    (Bug-8048). It is never interpreted here — the gateway only carries it.
    """

    xml_body: str
    warnings: list[str]
    next_cursor: str | None


class DrillThroughResolutionError(ValueError):
    """A non-``[Measures]`` DRILLTHROUGH member ref could not be resolved.

    Bug-3622: dropping an unresolvable WHERE-tuple member silently widened
    the result (the drill ran without that filter). The handler fails loud
    with this error instead, which the Execute path turns into a SOAP fault.
    """


async def handle_drillthrough(
    parsed: ParsedMDX,
    tenant_slug: str,
    jwt_token: str,
    measures_meta: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    hierarchy_defs: list[dict[str, Any]],
    persona_id: str | None = None,
    cursor: str | None = None,
    session_vars: dict[str, str] | None = None,
) -> DrillthroughResult:
    """Execute an XMLA DRILLTHROUGH and return a :class:`DrillthroughResult`.

    Carries the inner XML body (to be wrapped in ``<tns:ExecuteResponse>``),
    a list of warning messages (e.g. "Rows trimmed to N"), and the keyset
    continuation token for the next page.

    Bug-8048: ``cursor`` is an opaque token handed out by a prior DRILLTHROUGH
    page. When supplied, the query-router continues by keyset (seek) rather
    than by offset, so pages stay stable while the source is being written to.
    The gateway never mints, parses or validates the token — the query-router
    signs it and rejects a tampered or out-of-scope one.

    Wave C #11 / B1: ``session_vars`` carries the Execute's declared XMLA
    ``<Parameters>`` as ``app.<name>`` → value. The drill query is a
    result-bearing query of the SAME Execute, so it must be scoped by the same
    parameters as the MDX path (the query-router resolves them into
    parameterised row-security / default filters). Absent params it is ``None``
    and the drill behaves exactly as before.
    """
    measure_name = _extract_measure_name(parsed)
    if not measure_name:
        raise ValueError("DRILLTHROUGH: no measure found on COLUMNS axis")

    measure_meta = _find_measure(measure_name, measures_meta)
    if not measure_meta:
        raise ValueError(f"DRILLTHROUGH: measure '{measure_name}' not found in model")
    measure_id = str(measure_meta["id"])

    grouping_levels = _extract_grouping_levels(
        parsed, dimensions_meta, hierarchy_defs,
    )

    drillable, drill_failure_warning = await _fetch_drill_options(
        measure_id, grouping_levels, jwt_token, session_vars=session_vars,
    )

    warnings: list[str] = []
    hierarchy_id: str | None = None
    # Bug-6665: surface the drill-options failure as a SOAP warning so the
    # client knows the drill grain was not resolved (instead of silently
    # degrading to leaf-detail drill).
    if drill_failure_warning:
        warnings.append(drill_failure_warning)
    if len(drillable) == 1:
        hierarchy_id = drillable[0].get("hierarchy_id")
    elif len(drillable) > 1:
        # Bug-4153: more than one hierarchy is drillable. Previously the handler
        # fell back silently to leaf detail with no signal to the client about
        # which grain it got. Apply a deterministic pick — the hierarchy named in
        # the DRILLTHROUGH RETURN clause if present, else the first hierarchy by
        # name (alphabetical; grain depth is unavailable) — and surface a
        # warning naming the chosen hierarchy.
        chosen, reason = _pick_drill_hierarchy(drillable, parsed)
        if chosen is not None:
            hierarchy_id = chosen.get("hierarchy_id")
            warnings.append(
                "Multiple drillable hierarchies were available "
                f"({_hierarchy_name_list(drillable)}). Drilled by "
                f"'{chosen.get('hierarchy_name') or hierarchy_id}' ({reason})."
            )

    limit = parsed.maxrows

    result = await _fetch_drill_through(
        measure_id=measure_id,
        grouping_levels=grouping_levels,
        jwt_token=jwt_token,
        hierarchy_id=hierarchy_id,
        limit=limit,
        persona_id=persona_id,
        cursor=cursor,
        session_vars=session_vars,
    )

    columns = result.get("columns", [])
    rows = result.get("rows", [])
    page = result.get("page") or {}
    has_more = page.get("has_more", False)
    next_cursor = page.get("next_cursor")
    hierarchy_path = result.get("hierarchy_path", [])

    columns, rows = _augment_hierarchy_columns(
        columns, rows, hierarchy_path,
    )

    # Wave C #4: honour DRILLTHROUGH RETURN as an ordered subset of the already
    # persona-authorised, server-curated result — POST-PROJECTION. The secured
    # drill has already run (persona / CLS / RLS applied by the query-router), so
    # RETURN can only ever NARROW and re-order the curated columns; it can never
    # widen access. Any requested column that is not present in the returned
    # curated set (unknown, or CLS-omitted for this persona) faults the WHOLE
    # request with one uniform error — never a partial/blank projection (#5).
    if getattr(parsed, "return_columns", None):
        columns, rows = _project_return_columns(
            parsed.return_columns, columns, rows,
            dimensions_meta, hierarchy_defs,
        )
    if has_more:
        # F-019-14: report the server-clamped page size, not the raw MAXROWS.
        # The drill builder clamps to a 10000 ceiling, so a MAXROWS of 50000
        # actually returns 10000 — the warning must say so, not "50000".
        requested = limit or 1000
        effective_limit = min(requested, 10000)
        warnings.append(
            f"Result truncated to {effective_limit} rows. "
            "Use MAXROWS to increase the limit (max 10000)."
        )

    xml_body = build_drillthrough_rowset(columns, rows)
    return DrillthroughResult(xml_body, warnings, next_cursor)


def _project_return_columns(
    return_columns: list[list[str]],
    columns: list[str],
    rows: list[dict[str, Any]],
    dimensions_meta: list[dict[str, Any]],
    hierarchy_defs: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Project the curated drill result down to the requested RETURN columns.

    Wave C #4: ``return_columns`` is the parsed DRILLTHROUGH RETURN list (each an
    ordered list of bracket segments, e.g. ``["Measures", "amount"]`` or
    ``["geo", "City"]``). Each is canonicalised and resolved against the CURATED
    ``columns`` the secured drill actually returned. The projection:

      * preserves the client's requested ORDER;
      * de-duplicates repeated requests (first position wins);
      * projects each row to exactly the resolved columns.

    Any requested column that does not resolve to a returned curated column —
    because it does not exist, or because CLS omitted it for this persona — faults
    the WHOLE request with one uniform error (:class:`DrillThroughResolutionError`,
    which the Execute path turns into a SOAP client fault). There is never a
    partial or blank projection.
    """
    dim_names = {d.get("name", ""): d for d in dimensions_meta}
    hier_level_to_dim = _build_hier_level_to_dim(hierarchy_defs, dim_names)
    curated_lower = {str(c).lower(): str(c) for c in columns}

    projected: list[str] = []
    seen: set[str] = set()
    for parts in return_columns:
        resolved = _canonicalise_return_column(
            parts, curated_lower, dim_names, hier_level_to_dim,
        )
        if resolved is None:
            ref_repr = ".".join(f"[{p}]" for p in parts) if parts else "[?]"
            raise DrillThroughResolutionError(
                f"DRILLTHROUGH RETURN column {ref_repr} is not available in the "
                "authorised drill-through result (unknown column, or restricted "
                "for your access). The whole request was refused rather than "
                "returning a partial result."
            )
        if resolved not in seen:
            seen.add(resolved)
            projected.append(resolved)

    new_rows = [{c: row.get(c) for c in projected} for row in rows]
    return projected, new_rows


def _canonicalise_return_column(
    parts: list[str],
    curated_lower: dict[str, str],
    dim_names: dict[str, Any],
    hier_level_to_dim: dict[str, dict[str, str]],
) -> str | None:
    """Resolve one RETURN member reference to a returned curated column name.

    Returns the curated column name (original casing) or ``None`` when the
    reference cannot be resolved against the curated set. Measure references
    (``[Measures].[X]``) resolve ONLY to a measure column named ``X`` — they never
    fall through to dimension resolution, so a measure name colliding with a
    dimension column cannot be mis-matched.
    """
    if not parts:
        return None
    if (parts[0] or "").lower() == "measures":
        return curated_lower.get((parts[-1] or "").lower())

    # Dimension / hierarchy-level reference → resolve to its dimension column.
    hier_name = parts[-2] if len(parts) >= 2 else parts[0]
    level_name = parts[-1]
    col = _resolve_dim_column(hier_name, level_name, dim_names, hier_level_to_dim)
    if col and col.lower() in curated_lower:
        return curated_lower[col.lower()]
    # The leaf segment may itself already be the curated (physical) column name.
    if (level_name or "").lower() in curated_lower:
        return curated_lower[(level_name or "").lower()]
    return None


def build_drillthrough_rowset(
    columns: list[str],
    rows: list[dict[str, Any]],
) -> str:
    """Build an XMLA Rowset XML body for DRILLTHROUGH results.

    Produces ``<return><root>`` with XSD schema and ``<row>`` elements.
    All columns typed as ``string`` — DRILLTHROUGH results are untyped.
    """
    col_elements = ""
    for col in columns:
        safe = _xml_safe_name(col)
        col_elements += (
            f'<xs:element minOccurs="0" name="{_xe(safe)}" '
            f'sql:field="{_xe(safe)}" type="xs:string"/>'
        )

    schema = (
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        f'xmlns:sql="{_SQL_NS}" elementFormDefault="qualified" '
        f'targetNamespace="{_ROWSET_NS}">'
        f'<xs:element name="root"><xs:complexType>'
        f'<xs:sequence maxOccurs="unbounded" minOccurs="0">'
        f'<xs:element name="row" type="row"/>'
        f'</xs:sequence></xs:complexType></xs:element>'
        f'<xs:complexType name="row"><xs:sequence>'
        f'{col_elements}'
        f'</xs:sequence></xs:complexType>'
        f'</xs:schema>'
    )

    rows_xml = ""
    for row in rows:
        cells = ""
        for col in columns:
            safe = _xml_safe_name(col)
            val = row.get(col)
            if val is None:
                continue
            cells += f"<{safe}>{_xe(str(val))}</{safe}>"
        rows_xml += f"<row>{cells}</row>"

    return (
        f'<return><root xmlns="{_ROWSET_NS}"'
        f' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        f' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"{schema}{rows_xml}</root></return>"
    )


# ---------------------------------------------------------------------------
# MDX extraction helpers
# ---------------------------------------------------------------------------

def _extract_measure_name(parsed: ParsedMDX) -> str | None:
    """Extract the first measure name from the COLUMNS axis.

    Bug-6717: accepts ``]]`` inside bracket bodies and unescapes to the
    raw technical name.
    """
    col_expr = parsed.axis_expr("COLUMNS")
    if not col_expr:
        return None
    m = re.search(r"\[Measures\]\.\[((?:[^\]]|\]\])+)\]", col_expr, re.IGNORECASE)
    return m.group(1).replace("]]", "]") if m else None


def _find_measure(
    name: str, measures_meta: list[dict[str, Any]],
) -> dict[str, Any] | None:
    lower = name.lower()
    for m in measures_meta:
        if (m.get("name", "")).lower() == lower:
            return m
    return None


def _extract_grouping_levels(
    parsed: ParsedMDX,
    dimensions_meta: list[dict[str, Any]],
    hierarchy_defs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build grouping_levels from WHERE clause and ROWS axis members.

    Each entry: ``{"column": dim_name, "value": member_value}``.

    B8 round-3 fix (Bug-1049): member references may carry path-qualified
    composite keys (``[geo].[geo].[City].&[Germany]&[Berlin]`` — the
    grammar the server itself emits on subtotal axes, which Excel echoes
    back on drill-through). The deepest key filters the named level and
    each ancestor key filters its own level dimension, exactly as the
    WHERE-clause parsers do. Previously the deepest key was lost (the
    ancestor key was applied to the named level) and the rows-axis regex
    took only the FIRST key.
    """
    levels: list[dict[str, Any]] = []
    dim_names = {d.get("name", ""): d for d in dimensions_meta}
    hier_level_to_dim = _build_hier_level_to_dim(hierarchy_defs, dim_names)

    # Bug-1056: the tree-sitter ``bracket_name`` token (/\[[^\]]*\]/) has no
    # ``]]`` escape, so a WHERE-tuple member key containing ``]`` (SSAS escapes
    # it as ``]]``) is truncated at the first ``]`` before parsing — a drill
    # member ``&[A]]B]`` becomes key ``A``, returning the wrong (or empty)
    # drill rows. Regenerating the committed ``.so`` is not possible without
    # the tree-sitter CLI, so we re-extract the WHERE members from the raw MDX
    # with the same ``]]``-aware regex grammar the ROWS-axis path already uses
    # (``parse_member_keys`` unescapes ``]]`` → ``]``). This is the single
    # non-regex seam called out post-Bug-1052.
    where_expr = _extract_where_clause(parsed.raw_mdx)
    keyed_refs_found = False
    if where_expr:
        for m in re.finditer(
            r"((?:\[" + _BRACKET_BODY_RE + r"+\]\.)+)(" + KEY_PATH + r")", where_expr
        ):
            names = _bracket_names(m.group(1))
            keys = parse_member_keys(m.group(2))
            new_levels = _keyed_ref_to_groupings(
                names, keys, dim_names, hier_level_to_dim
            )
            if new_levels:
                keyed_refs_found = True
                levels.extend(new_levels)

    # sol review F-CR-02: this used to be all-or-nothing — one key-form match
    # suppressed the tree-sitter members entirely, so a MIXED slicer such as
    # ``([geo].[geo].[City].[Berlin], [Date].[business_date].[Year].&[2025])``
    # kept only the keyed filter and silently dropped Berlin, returning every
    # city in 2025. Caption-form members are never matched by the regex above
    # (it requires a ``&[...]`` key path), so they can always be processed
    # without double-counting. Only KEYED members are still deferred to the
    # regex when it produced results, because the regex is the ``]]``-aware
    # grammar and the tree-sitter key token is truncated at the first ``]``
    # (Bug-1056) — taking both would add a second, wrong filter for the same
    # level. Identical ``(column, value)`` pairs are deduped below regardless.
    for wm in parsed.where_members:
        if keyed_refs_found and any(p.startswith("&") for p in wm.parts):
            continue
        levels.extend(
            _member_parts_to_groupings(wm.parts, dim_names, hier_level_to_dim)
        )

    rows_expr = parsed.axis_expr("ROWS")
    if rows_expr:
        for m in re.finditer(
            r"((?:\[" + _BRACKET_BODY_RE + r"+\]\.)+)(" + KEY_PATH + r")", rows_expr
        ):
            names = _bracket_names(m.group(1))
            keys = parse_member_keys(m.group(2))
            levels.extend(
                _keyed_ref_to_groupings(names, keys, dim_names, hier_level_to_dim)
            )

    # Dedupe identical (column, value) pairs — the same member may appear
    # in both the WHERE tuple and the ROWS axis of the echoed statement.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for entry in levels:
        key = (entry["column"], entry["value"])
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    return deduped


def _build_hier_level_to_dim(
    hierarchy_defs: list[dict[str, Any]],
    dim_names: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Map (hierarchy_name_lower, level_name_lower) → dimension_name.

    Levels are inserted in ordinal order, so each inner dict's value
    order is ancestor-first — the composite key-path alignment in
    ``_keyed_ref_to_groupings`` relies on this.
    """
    result: dict[str, dict[str, str]] = {}
    for h in hierarchy_defs:
        hname = (h.get("name") or "").lower()
        levels = h.get("levels") or []
        levels = sorted(
            (lvl for lvl in levels if isinstance(lvl, dict)),
            key=lambda lvl: lvl.get("ordinal", 0),
        )
        level_map: dict[str, str] = {}
        for lvl in levels:
            if isinstance(lvl, dict):
                lname = (lvl.get("name") or "").lower()
                ka = lvl.get("key_attribute") or {}
                ka_source = ka.get("source", "")
                ka_id = ka.get("id", "")
                for dname, dmeta in dim_names.items():
                    if ka_source == "user_defined_attribute" and str(
                        dmeta.get("user_defined_attribute_id", "")
                    ) == str(ka_id):
                        level_map[lname] = dname
                        break
                    if ka_source == "physical_column" and str(
                        dmeta.get("source_column_id", "")
                    ) == str(ka_id):
                        level_map[lname] = dname
                        break
        if level_map:
            result[hname] = level_map
    return result


def _resolve_dim_column(
    dim_or_hier: str,
    level_name: str,
    dim_names: dict[str, Any],
    hier_level_to_dim: dict[str, dict[str, str]],
) -> str | None:
    """Resolve an MDX [Dimension].[Level] reference to a dimension column name."""
    lname = level_name.lower()
    hname = dim_or_hier.lower()
    by_level = hier_level_to_dim.get(hname, {})
    if lname in by_level:
        return by_level[lname]
    if level_name in dim_names:
        return level_name
    lower_map = {k.lower(): k for k in dim_names}
    if lname in lower_map:
        return lower_map[lname]
    if hname in lower_map:
        return lower_map[hname]
    return None


def _extract_where_clause(mdx: str) -> str:
    """Return the WHERE-clause expression of an MDX statement (Bug-1056).

    Returns the text after the top-level ``WHERE`` keyword. Outer parentheses
    of a tuple slicer are tolerated by the downstream member regex.
    """
    if not mdx:
        return ""
    m = re.search(r"\bWHERE\b\s*(.+?)\s*$", mdx, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _bracket_names(names_segment: str) -> list[str]:
    """Extract ``]]``-aware bracketed name segments from a dotted name path."""
    return [
        unescape_member_key(b)
        for b in re.findall(r"\[(" + _BRACKET_BODY_RE + r"+)\]", names_segment)
    ]


def _member_parts_to_groupings(
    parts: list[str],
    dim_names: dict[str, Any],
    hier_level_to_dim: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Convert a WhereMember.parts list to grouping_level dicts.

    ``parts`` carries name segments first and ``&``-prefixed key segments
    last (``['geo', 'geo', 'City', '&Germany', '&Berlin']``). Caption-form
    members have no key segments and the last name segment is the value.
    Returns one entry for the named level plus one per ancestor key.
    """
    if len(parts) < 2:
        return []
    # Bug-3622: ``[Measures].[X]`` is a measure context, not a grouping filter.
    # It is intentionally not turned into a filter and must never trip the
    # fail-loud guard below.
    if parts and (parts[0] or "").lower() == "measures":
        return []
    keys = [p[1:] for p in parts if p.startswith("&")]
    names = [p for p in parts if not p.startswith("&")]
    if not keys:
        # Bug-8047 (XMLA/Excel surface of the grand-total drill): a caption-form
        # reference to the (All) member — ``[Dim].[Hier].[(All)]``,
        # ``[Dim].[All]`` and their case variants — is the grand-total
        # coordinate. It names EVERY member of the hierarchy, so the correct
        # drill filter on that dimension is NO filter at all. Treated as an
        # ordinary caption member it produced ``WHERE dim = '(All)'`` (zero
        # rows) or, when no dimension answered to the segment name, a
        # DrillThroughResolutionError SOAP fault — either way the grand-total
        # cell was undrillable, which is the whole point of drilling a total.
        #
        # Scoped to the CAPTION branch on purpose. A key-form reference
        # ``&[All]`` addresses a real data member whose key happens to be the
        # string "All"; dropping ITS filter would silently widen the drill and
        # return rows the user never asked for — precisely the failure mode
        # Bug-3622's fail-loud rule exists to prevent.
        #
        # sol review F-CR-01: the token alone is NOT enough to classify. The
        # synthetic (All) member sits ABOVE every level, so its unique name
        # never carries a LEVEL segment (``[Dim].[Hier].[All]``,
        # ``[Dim].[All]``). A reference that DOES name a level uses its
        # trailing token as a data caption — ``[geo].[geo].[City].[All]`` is
        # the city literally called "All" and must keep ``city = 'All'``.
        # Skipping it handed the query-router an unfiltered city coordinate
        # and returned every city.
        #
        # sol recheck (Bug-8047 REOPENED): classifying on "does the leading
        # path resolve to a dimension column" instead of "does it name a
        # LEVEL" reopened the original defect for every PLAIN dimension — the
        # dominant model shape. A plain Tessallite dimension is one column
        # exposed as its own attribute hierarchy (``cube_model`` ORIGIN_
        # ATTRIBUTE), so its dimension and hierarchy names always coincide and
        # the gateway advertises ``[dname].[dname]``. ``hierarchy_defs`` holds
        # USER-DEFINED hierarchies only, so ``[dname].[dname].[All]`` resolved
        # through the plain-dimension name fallback, the skip did not fire,
        # and the grand-total drill was back to ``WHERE dname = 'All'`` —
        # zero rows.
        #
        # The signal is therefore the presence of a genuine LEVEL segment
        # (:func:`_has_level_segment`), which is structural and independent of
        # whether the leading path happens to answer to a dimension name.
        if is_all_member_token(names[-1]) and not _has_level_segment(
            names[:-1], dim_names, hier_level_to_dim,
        ):
            return []
        # Caption form: the last name segment is the member value.
        keys = [names[-1]]
        names = names[:-1]
    if not names:
        return []
    return _keyed_ref_to_groupings(names, keys, dim_names, hier_level_to_dim)


def _keyed_ref_to_groupings(
    names: list[str],
    keys: list[str],
    dim_names: dict[str, Any],
    hier_level_to_dim: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Map a member reference (name path + ancestor-first key path) to filters.

    The deepest key filters the named level's dimension; each ancestor key
    filters its own level dimension (same contract as the WHERE-clause
    parsers in ``xmla_server``, B8 round-3 Bug-1049).
    """
    if not names or not keys:
        return []

    # Bug-3622: a ``[Measures]`` reference is a measure context, not a grouping
    # filter — skip it without tripping the fail-loud guard below.
    if (names[0] or "").lower() == "measures":
        return []

    hier_name = names[-2] if len(names) >= 2 else names[0]
    level_name = names[-1]
    col = _resolve_ref_column(names, keys, dim_names, hier_level_to_dim)
    if col is None:
        # Bug-3622: fail loud instead of silently dropping the filter. A
        # member ref that names a non-existent level/dimension previously
        # widened the drill (it ran WITHOUT this filter, returning over-broad
        # rows). Surface a fault naming the unresolved reference.
        ref_repr = ".".join(f"[{n}]" for n in names)
        if keys:
            ref_repr += "." + "".join(f"&[{k}]" for k in keys)
        raise DrillThroughResolutionError(
            f"DRILLTHROUGH member reference {ref_repr} could not be resolved "
            "to a model dimension or hierarchy level. The drill was not run "
            "to avoid returning unfiltered rows."
        )

    entries = [{"column": col, "value": _parse_value(keys[-1])}]
    if len(keys) > 1:
        ordered = _hier_ordered_dims([hier_name, names[0], level_name], hier_level_to_dim)
        if not ordered or col not in ordered:
            logger.warning(
                "Composite member key path %s on '%s' could not be aligned "
                "to hierarchy levels; ancestor keys ignored.", keys, col,
            )
            return entries
        level_idx = ordered.index(col)
        n_ancestors = len(keys) - 1
        if n_ancestors > level_idx:
            logger.warning(
                "Composite member key path %s is deeper than the levels "
                "above '%s'; ancestor keys ignored.", keys, col,
            )
            return entries
        for a_dim, a_key in zip(ordered[level_idx - n_ancestors:level_idx], keys[:-1]):
            if a_dim in dim_names:
                entries.append({"column": a_dim, "value": _parse_value(a_key)})
    return entries


def _has_level_segment(
    names: list[str],
    dim_names: dict[str, Any],
    hier_level_to_dim: dict[str, dict[str, str]],
) -> bool:
    """Does a caption member's LEADING name path carry an explicit level segment?

    *names* is the member reference minus its trailing caption token, so the
    question is whether a real hierarchy LEVEL was named before that token.
    This is the one signal that separates the synthetic grand-total member —
    which sits above every level and therefore never names one — from a data
    member whose caption happens to be ``All`` (sol review F-CR-01 / the
    Bug-8047 reopen).

    Segment COUNT is the primary rule, because it holds regardless of whether
    the model's level map could be built:

    - ``[Hier]`` (one segment, from ``[Hier].[Member]``) — no level.
    - ``[Dim].[Hier].[Level]`` (three or more) — the third segment is a level.
    - Two segments are the ambiguous middle: ``[Dim].[Hier]`` when the member
      is ``[Dim].[Hier].[Member]``, or ``[Hier].[Level]`` when it is
      ``[Hier].[Level].[Member]``. Decided in this order, each test only ever
      turning the answer toward "level":

      1. The hierarchy's level map claims the second segment — a declared
         level, so a level was named.
      2. The two segments are IDENTICAL (``[country_code].[country_code]``).
         Every hierarchy unique name the gateway emits is ``[X].[X]`` (see
         ``mdschema._rows_hierarchies`` / ``_rows_members``), for plain
         dimensions and multi-level user hierarchies alike, so identical
         segments are the hierarchy bracket and no level was named.
      3. Otherwise the second segment counts as a level when it answers to a
         dimension name (``[geo].[City]``). That form can only reach here from
         a client using the ``[Hier].[Level].[Member]`` shorthand, and the
         check keeps the answer stable even when ``_build_hier_level_to_dim``
         dropped the hierarchy for an unmatched key attribute.

    Erring toward "there IS a level" is the non-widening direction: it keeps a
    filter rather than dropping one.
    """
    if len(names) < 2:
        return False
    if len(names) >= 3:
        return True
    if _resolve_level_dim(names[0], names[1], hier_level_to_dim) is not None:
        return True
    first = (names[0] or "").strip().lower()
    second = (names[1] or "").strip().lower()
    if not second or second == first:
        return False
    return second in {k.lower() for k in dim_names}


def _resolve_ref_column(
    names: list[str],
    keys: list[str],
    dim_names: dict[str, Any],
    hier_level_to_dim: dict[str, dict[str, str]],
) -> str | None:
    """Resolve a member reference's name path to a dimension column, or None.

    The single, non-raising resolution rule used by
    :func:`_keyed_ref_to_groupings`, which faults (Bug-3622) when it returns
    ``None``. Deliberately NOT the (All)-member classifier: "resolves to a
    column" and "names a level" are different questions, and conflating them
    reopened Bug-8047 for plain dimensions. See :func:`_has_level_segment`.
    """
    if not names:
        return None
    hier_name = names[-2] if len(names) >= 2 else names[0]
    level_name = names[-1]

    # Explicit-level resolution first ([Dim].[Hier].[Level] / [Dim].[Level]).
    col = _resolve_level_dim(hier_name, level_name, hier_level_to_dim)
    if col is None and len(names) >= 3:
        col = _resolve_level_dim(names[0], level_name, hier_level_to_dim)

    # No-level composite path ([Dim].[Hier].&[k0]&[k1]...): the last name
    # segment is the hierarchy, and the path runs from the root level, so
    # the named member sits at index len(keys) - 1.
    if col is None and len(keys) > 1:
        ordered = _hier_ordered_dims(
            [level_name, hier_name, names[0]], hier_level_to_dim,
        )
        if ordered and len(keys) <= len(ordered):
            col = ordered[len(keys) - 1]

    # Legacy fallbacks: the level or hierarchy segment names a dimension.
    if col is None:
        col = _resolve_dim_column(
            hier_name, level_name, dim_names, hier_level_to_dim,
        )
    return col if col in dim_names else None


def _resolve_level_dim(
    hier_name: str,
    level_name: str,
    hier_level_to_dim: dict[str, dict[str, str]],
) -> str | None:
    """Resolve a (hierarchy, level) pair strictly via the level map."""
    by_level = hier_level_to_dim.get((hier_name or "").lower(), {})
    return by_level.get((level_name or "").lower())


def _hier_ordered_dims(
    hier_candidates: list[str],
    hier_level_to_dim: dict[str, dict[str, str]],
) -> list[str] | None:
    """Ancestor-first level dims for the first matching hierarchy name."""
    for h in hier_candidates:
        level_map = hier_level_to_dim.get((h or "").lower())
        if level_map:
            return list(level_map.values())
    return None


def _parse_value(raw: str) -> Any:
    """Convert a member key to int/float only when the round-trip is lossless.

    Bug-6662: the previous implementation blindly coerced every numeric-looking
    string, so zero-padded keys ('007' -> 7) and scientific-notation strings
    ('1e5' -> 100000.0) became different values, producing wrong/empty
    drill-through results. Now we coerce only when ``str(int(raw)) == raw``
    (or float equivalent), which guarantees no leading zeros are dropped and
    no scientific notation is reinterpreted.

    Numeric coercion is preserved for legitimately numeric keys (year=2025,
    month=4) because BigQuery does NOT implicitly coerce STRING to INT64
    in comparisons -- ``WHERE year = '2025'`` fails against an INT64 column.
    The lossless round-trip check preserves type fidelity for clean integers
    while protecting text-typed keys from corruption.
    """
    try:
        i = int(raw)
        if str(i) == raw:
            return i
    except ValueError:
        pass
    try:
        f = float(raw)
        if str(f) == raw:
            return f
    except ValueError:
        pass
    return raw


# ---------------------------------------------------------------------------
# Column augmentation
# ---------------------------------------------------------------------------

def _augment_hierarchy_columns(
    columns: list[str],
    rows: list[dict[str, Any]],
    hierarchy_path: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Add upper hierarchy level columns to the result.

    The hierarchy_path from drill-through response contains entries like:
    ``{"level_name": "Year", "dimension_name": "year", "value": 2025}``

    For each path entry whose dimension_name is NOT already in the result
    columns, prepend it and fill the value in every row.
    """
    if not hierarchy_path:
        return columns, rows

    extra_cols: list[tuple[str, Any]] = []
    col_set = set(columns)
    for entry in hierarchy_path:
        dim_name = entry.get("dimension_name", "")
        if dim_name and dim_name not in col_set:
            extra_cols.append((dim_name, entry.get("value")))

    if not extra_cols:
        return columns, rows

    new_columns = [c for c, _ in extra_cols] + columns
    new_rows = []
    for row in rows:
        new_row = dict(row)
        for col, val in extra_cols:
            new_row[col] = val
        new_rows.append(new_row)

    return new_columns, new_rows


# ---------------------------------------------------------------------------
# Multi-hierarchy deterministic pick (Bug-4153)
# ---------------------------------------------------------------------------

def _hierarchy_name_list(drillable: list[dict[str, Any]]) -> str:
    """Comma-separated drillable hierarchy names for a warning message."""
    names = [
        str(h.get("hierarchy_name") or h.get("hierarchy_id") or "")
        for h in drillable
    ]
    return ", ".join(n for n in names if n)


def _pick_drill_hierarchy(
    drillable: list[dict[str, Any]],
    parsed: ParsedMDX,
) -> tuple[dict[str, Any] | None, str]:
    """Deterministically choose one hierarchy when several are drillable.

    Rule (Bug-4153):
      1. If the DRILLTHROUGH RETURN clause names a level whose hierarchy is
         drillable, pick that hierarchy.
      2. Otherwise pick the FIRST hierarchy by name (alphabetical). Grain depth
         is not carried on the drill-options rows, so the tiebreak is a
         deterministic alphabetical-by-name sort — NOT a true finest-grain
         pick (F-P4b1-03: the label previously claimed "finest grain", which
         was misleading).

    Returns ``(hierarchy_dict, reason)``; ``hierarchy_dict`` is None only when
    *drillable* is empty.
    """
    if not drillable:
        return None, ""

    return_hier_names: set[str] = set()
    for col_parts in getattr(parsed, "return_columns", None) or []:
        for part in col_parts:
            if part:
                return_hier_names.add(part.lower())

    if return_hier_names:
        for h in sorted(
            drillable, key=lambda d: str(d.get("hierarchy_name") or "").lower()
        ):
            hname = str(h.get("hierarchy_name") or "").lower()
            if hname and hname in return_hier_names:
                return h, "named in the RETURN clause"

    chosen = sorted(
        drillable, key=lambda d: str(d.get("hierarchy_name") or "").lower()
    )[0]
    return chosen, "first by hierarchy name (deterministic; grain depth unavailable)"


# ---------------------------------------------------------------------------
# Router client wrappers
# ---------------------------------------------------------------------------

async def _fetch_drill_options(
    measure_id: str,
    grouping_levels: list[dict[str, Any]],
    jwt_token: str,
    *,
    session_vars: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(drillable_hierarchies, failure_warning_or_None)``.

    Bug-6665: a QueryRouterError was previously swallowed silently,
    degrading to leaf-detail drill (hierarchy_id=None) with no client
    warning. The AMBIGUITY case (Bug-4153) emits a warning, but the
    FAILURE case stayed silent. Now returns the warning text so the
    caller can attach a SOAP warning on the failure path.

    Wave C #11 / B1: ``session_vars`` is threaded to the router so the whole
    Execute (options + detail) is scoped by the same declared parameters.
    """
    try:
        result = await execute_drill_options(
            measure_id, grouping_levels, jwt_token, session_vars=session_vars,
        )
        return result.get("hierarchies", []), None
    except QueryRouterError as exc:
        logger.warning("drill-options failed: %s", exc)
        return [], (
            "Drill-through hierarchy resolution failed "
            f"({exc}). Falling back to leaf-detail drill. "
            "The returned columns may not match the expected drill grain."
        )


async def _fetch_drill_through(
    *,
    measure_id: str,
    grouping_levels: list[dict[str, Any]],
    jwt_token: str,
    hierarchy_id: str | None,
    limit: int | None,
    persona_id: str | None,
    cursor: str | None = None,
    session_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    return await execute_drill_through(
        measure_id=measure_id,
        grouping_levels=grouping_levels,
        jwt_token=jwt_token,
        hierarchy_id=hierarchy_id,
        limit=limit,
        persona_id=persona_id,
        cursor=cursor,
        session_vars=session_vars,
    )


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _xe(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _xml_safe_name(name: str) -> str:
    """Ensure a column name is safe for use as an XML element name."""
    safe = re.sub(r"[^A-Za-z0-9_.]", "_", name)
    if safe and safe[0].isdigit():
        safe = "_" + safe
    return safe or "_col"
