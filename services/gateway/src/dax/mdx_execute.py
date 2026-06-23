"""
MDDataSet response builder for XMLA Execute requests.

Builds properly formatted MDDataSet XML that MSOLAP/Excel can parse.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from itertools import product

from shared.config.bootstrap import system_snapshot_get
from shared.schemas.measure_formats import format_token_to_mdx
from src.dax.mdx_calc_members import parse_calc_members, evaluate_calc_members

logger = logging.getLogger(__name__)


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_cell_value(value, fmt_str: str | None) -> str | None:
    """Bug-5432: render a cell's FORMATTED_VALUE (``FmtValue``) from its numeric
    value and the measure's SSAS/.NET ``FORMAT_STRING``.

    SSAS returns both the raw ``<Value>`` and the formatted ``<FmtValue>`` per cell;
    we previously emitted only the raw value (and FORMAT_STRING), forcing every
    client to format itself. This covers the common formats — the named
    Standard/Percent/Currency plus decimal / thousands / percent / currency
    patterns — and returns ``None`` for anything it can't format, so the caller
    omits FmtValue and the client falls back to FORMAT_STRING (no regression).
    """
    if not fmt_str:
        return None
    try:
        v = float(value)
    except (ValueError, TypeError):
        return None
    s = fmt_str.strip()
    low = s.lower()
    if low in ("", "general number", "general"):
        return None
    if low == "standard":
        return f"{v:,.2f}"
    if low == "percent":
        return f"{v * 100:.2f}%"
    if low == "currency":
        return f"${v:,.2f}"
    # .NET/SSAS format strings can carry up to four ';'-separated sections
    # (positive;negative;zero;text). Use only the POSITIVE section to read the
    # decimal/percent/currency pattern — Python's own formatting handles the sign
    # — otherwise the negative section pollutes the decimal count and a trailing
    # ')' hides the '%'. (Section-specific negative styling / scaling-comma forms
    # are not reproduced; the raw FORMAT_STRING is still emitted for the client.)
    s = s.split(";", 1)[0].strip()
    if not s:
        return None
    # decimals = count of 0/# after the first '.'; thousands = ',' in integer part.
    int_part, _, frac_part = s.partition(".")
    decimals = sum(1 for c in frac_part if c in "0#")
    thousands = "," in int_part
    if s.endswith("%"):
        return (f"{v * 100:,.{decimals}f}%" if thousands
                else f"{v * 100:.{decimals}f}%")
    prefix = s[0] if s[:1] in ("$", "€", "£") else ""
    body = f"{v:,.{decimals}f}" if thousands else f"{v:.{decimals}f}"
    return f"{prefix}{body}"


# ---------------------------------------------------------------------------
# KPI member-function resolution (Bug-3657)
# ---------------------------------------------------------------------------

def resolve_kpi_property_expr(
    kpi: dict[str, Any],
    prop: str,
    measures_meta: list[dict[str, Any]],
) -> str | None:
    """Resolve a KPI member function to an executable MDX scalar expression.

    *prop* is one of ``KPIValue`` / ``KPIGoal`` / ``KPIStatus`` / ``KPITrend``.
    The value reference points at the KPI's value MEASURE (queryable through the
    normal MDX→SQL path), not the synthetic ``[KPI] <name>`` catalogue column.

    Returns the MDX expression string, or ``None`` when the property has no
    published definition (e.g. a KPI with no trend → ``KPITrend`` is undefined).
    Raises ``ValueError`` when the KPI cannot produce the requested property
    because its value measure is unresolved.
    """
    measure_map = {str(m.get("id", "")): m for m in measures_meta}

    value_m = measure_map.get(str(kpi.get("value_measure_id", "")), {})
    value_name = value_m.get("name", "") if value_m else ""
    kpi_value = f"[Measures].[{value_name}]" if value_name else ""

    # Goal: static literal, measure/expression target, or legacy goal measure.
    target_type = kpi.get("target_type") or ""
    target_value = kpi.get("target_value")
    if target_type == "static" and target_value is not None:
        kpi_goal = str(target_value)
    elif target_type in ("measure", "expression"):
        kpi_goal = kpi.get("target_expression") or ""
    else:
        goal_m = measure_map.get(str(kpi.get("goal_measure_id", "")), {})
        kpi_goal = f"[Measures].[{goal_m.get('name', '')}]" if goal_m else ""

    if prop == "KPIValue":
        if not kpi_value:
            raise ValueError(
                f"KPI '{kpi.get('name', '')}' has no resolvable value measure."
            )
        return kpi_value

    if prop == "KPIGoal":
        return kpi_goal or None

    if prop == "KPIStatus":
        legacy_status = kpi.get("status_expression") or ""
        if legacy_status:
            return legacy_status
        if kpi_value and kpi_goal:
            direction = kpi.get("direction") or "higher_is_better"
            if direction == "lower_is_better":
                return (
                    f"CASE WHEN {kpi_value} <= {kpi_goal} THEN 1 "
                    f"WHEN {kpi_value} <= {kpi_goal} * 1.1 THEN 0 ELSE -1 END"
                )
            return (
                f"CASE WHEN {kpi_value} >= {kpi_goal} THEN 1 "
                f"WHEN {kpi_value} >= {kpi_goal} * 0.9 THEN 0 ELSE -1 END"
            )
        return None

    if prop == "KPITrend":
        return kpi.get("trend_expression") or None

    raise ValueError(f"Unknown KPI member property: {prop}")


def _meta_modified() -> str:
    return str(system_snapshot_get("xmla.metadata_modified_at"))

# F-002-10: SSAS renders a fact whose dimension member is NULL/empty under a
# stable "(blank)" member rather than dropping the row. The axis-tuple builders
# and the single-hierarchy member list previously treated NULL/"" as "no
# member" and discarded the tuple, so those facts silently vanished and the
# pivot's column/grand totals no longer reconciled with the source. Normalising
# the value once — before any axis or cell building — keeps the axis member key,
# the cell-matching key, and the subtotal grain key aligned on the same string.
BLANK_MEMBER = "(blank)"

_MDDATASET_NS = "urn:schemas-microsoft-com:xml-analysis:mddataset"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_XSD_NS = "http://www.w3.org/2001/XMLSchema"

# MDDataSet XSD schema — required by MSOLAP to parse Execute responses.
# Matches OlaPy's execute_xsd exactly (confirmed working with Excel).
_EXECUTE_XSD = (
    '<xs:schema elementFormDefault="qualified"'
    ' targetNamespace="urn:schemas-microsoft-com:xml-analysis:mddataset"'
    ' xmlns="urn:schemas-microsoft-com:xml-analysis:mddataset"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:complexType name="MemberType"><xs:sequence>'
    '<xs:any maxOccurs="unbounded" minOccurs="0" namespace="##targetNamespace" processContents="skip"/>'
    '</xs:sequence><xs:attribute name="Hierarchy" type="xs:string"/></xs:complexType>'
    '<xs:complexType name="PropType"><xs:sequence>'
    '<xs:element minOccurs="0" name="Default"/>'
    '</xs:sequence><xs:attribute name="name" type="xs:string" use="required"/>'
    '<xs:attribute name="type" type="xs:QName"/></xs:complexType>'
    '<xs:complexType name="TupleType"><xs:sequence>'
    '<xs:element maxOccurs="unbounded" name="Member" type="MemberType"/>'
    '</xs:sequence></xs:complexType>'
    '<xs:complexType name="MembersType"><xs:sequence>'
    '<xs:element maxOccurs="unbounded" minOccurs="0" name="Member" type="MemberType"/>'
    '</xs:sequence><xs:attribute name="Hierarchy" type="xs:string" use="required"/></xs:complexType>'
    '<xs:complexType name="TuplesType"><xs:sequence>'
    '<xs:element maxOccurs="unbounded" minOccurs="0" name="Tuple" type="TupleType"/>'
    '</xs:sequence></xs:complexType>'
    '<xs:group name="SetType"><xs:choice>'
    '<xs:element name="Members" type="MembersType"/>'
    '<xs:element name="Tuples" type="TuplesType"/>'
    '<xs:element name="CrossProduct" type="SetListType"/>'
    '<xs:element name="Union"><xs:complexType>'
    '<xs:group maxOccurs="unbounded" minOccurs="0" ref="SetType"/>'
    '</xs:complexType></xs:element>'
    '</xs:choice></xs:group>'
    '<xs:complexType name="SetListType">'
    '<xs:group maxOccurs="unbounded" minOccurs="0" ref="SetType"/>'
    '<xs:attribute name="Size" type="xs:unsignedInt"/></xs:complexType>'
    '<xs:complexType name="OlapInfo"><xs:sequence>'
    '<xs:element name="CubeInfo"><xs:complexType><xs:sequence>'
    '<xs:element maxOccurs="unbounded" name="Cube"><xs:complexType><xs:sequence>'
    '<xs:element name="CubeName" type="xs:string"/>'
    '<xs:element minOccurs="0" name="LastDataUpdate" type="xs:dateTime"/>'
    '<xs:element minOccurs="0" name="LastSchemaUpdate" type="xs:dateTime"/>'
    '</xs:sequence></xs:complexType></xs:element>'
    '</xs:sequence></xs:complexType></xs:element>'
    '<xs:element name="AxesInfo"><xs:complexType><xs:sequence>'
    '<xs:element maxOccurs="unbounded" name="AxisInfo"><xs:complexType><xs:sequence>'
    '<xs:element maxOccurs="unbounded" minOccurs="0" name="HierarchyInfo"><xs:complexType><xs:sequence>'
    '<xs:any maxOccurs="unbounded" minOccurs="0" namespace="##targetNamespace" processContents="skip"/>'
    '</xs:sequence><xs:attribute name="name" type="xs:string" use="required"/>'
    '</xs:complexType></xs:element>'
    '</xs:sequence><xs:attribute name="name" type="xs:string"/>'
    '</xs:complexType></xs:element>'
    '</xs:sequence></xs:complexType></xs:element>'
    '<xs:element name="CellInfo"><xs:complexType>'
    '<xs:choice maxOccurs="unbounded" minOccurs="0">'
    '<xs:any maxOccurs="unbounded" minOccurs="0" namespace="##targetNamespace" processContents="skip"/>'
    '</xs:choice></xs:complexType></xs:element>'
    '</xs:sequence></xs:complexType>'
    '<xs:complexType name="Axes"><xs:sequence>'
    '<xs:element maxOccurs="unbounded" name="Axis"><xs:complexType>'
    '<xs:group maxOccurs="unbounded" minOccurs="0" ref="SetType"/>'
    '<xs:attribute name="name" type="xs:string"/>'
    '</xs:complexType></xs:element>'
    '</xs:sequence></xs:complexType>'
    '<xs:complexType name="CellData"><xs:sequence>'
    '<xs:element maxOccurs="unbounded" minOccurs="0" name="Cell"><xs:complexType><xs:sequence>'
    '<xs:any maxOccurs="unbounded" minOccurs="0" namespace="##targetNamespace" processContents="skip"/>'
    '</xs:sequence><xs:attribute name="CellOrdinal" type="xs:unsignedInt" use="required"/>'
    '</xs:complexType></xs:element>'
    '</xs:sequence></xs:complexType>'
    '<xs:element name="root"><xs:complexType><xs:sequence>'
    '<xs:any minOccurs="0" namespace="http://www.w3.org/2001/XMLSchema" processContents="strict"/>'
    '<xs:element minOccurs="0" name="OlapInfo" type="OlapInfo"/>'
    '<xs:element minOccurs="0" name="Axes" type="Axes"/>'
    '<xs:element minOccurs="0" name="CellData" type="CellData"/>'
    '</xs:sequence></xs:complexType></xs:element>'
    '</xs:schema>'
)


def _get_axis_expr(mdx: str, axis_name: str) -> str:
    """Extract the raw expression for a specific axis from a multi-axis SELECT.
    Handles: SELECT <expr0> ON COLUMNS, <expr1> ON ROWS FROM ...
    Strips NON EMPTY prefix and Hierarchize/AddCalculatedMembers wrappers.
    """
    # Extract the full SELECT body (between SELECT and FROM)
    select_match = re.search(r'SELECT\s+(.+?)\s+FROM\s+\[', mdx, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return ""
    select_body = select_match.group(1).strip()

    axis_token = axis_name.upper()
    axis_num = "0" if axis_token == "COLUMNS" else "1"

    # Excel sometimes sends ROWS first; parse each "<expr> ON <axis>" independently
    matches = []
    for m in re.finditer(
        r'(?P<expr>.+?)\s+ON\s+(?P<axis>COLUMNS|ROWS|0|1)\b',
        select_body,
        re.IGNORECASE | re.DOTALL,
    ):
        matches.append((m.group("axis").upper(), m.group("expr").strip()))

    for axis, expr in matches:
        if axis in (axis_token, axis_num):
            expr = expr.rstrip().rstrip(",")
            expr = re.sub(r'^NON\s+EMPTY\s+', '', expr, flags=re.IGNORECASE)
            expr = re.sub(r'^Hierarchize\s*\((.+)\)\s*$', r'\1', expr,
                          flags=re.IGNORECASE | re.DOTALL)
            expr = re.sub(r'^Union\s*\((.+)\)\s*$', r'\1', expr,
                          flags=re.IGNORECASE | re.DOTALL)
            expr = re.sub(r'^AddCalculatedMembers\s*\((.+)\)\s*$', r'\1', expr,
                          flags=re.IGNORECASE | re.DOTALL)
            expr = re.sub(r'\s+DIMENSION\s+PROPERTIES\s+.*$', '', expr,
                          flags=re.IGNORECASE | re.DOTALL)
            return expr.strip()
    return ""


def _extract_hierarchies(axis_expr: str) -> list[str]:
    """
    Extract unique hierarchy names from an axis expression.

    Key distinction:
    - [Measures].[Amount] is a MEMBER reference → hierarchy = [Measures]
    - [Geography].[Geography] is a HIERARCHY name → hierarchy = [Geography].[Geography]
    - [Geography].[Geography].[Continent].Members is a LEVEL reference → hierarchy = [Geography].[Geography]

    This prevents treating each measure member or level name as a separate hierarchy,
    which would produce wrong cross-product tuples.
    """
    if not axis_expr:
        return []

    # Inner capture pattern for bracket contents like [(All)], [Continent], etc.
    _BC = r'([^\]]+)'

    hierarchies: list[str] = []
    seen: set[str] = set()

    # Level references like [Dim].[Hier].[Level].Members still belong to
    # hierarchy [Dim].[Hier]. Capture those first so pure level-member queries
    # from Excel do not fall back to the default layout.
    for m in re.finditer(
        rf'\[{_BC}\]\.\[{_BC}\]\.\[{_BC}\]',
        axis_expr,
    ):
        dim = m.group(1).strip()
        hier_name = m.group(2).strip()
        if dim == "Measures":
            hier = "[Measures]"
        else:
            hier = f"[{dim}].[{hier_name}]"
        if hier not in seen:
            seen.add(hier)
            hierarchies.append(hier)

    for m in re.finditer(r'\[([^\]]+)\]\.\[([^\]]+)\]', axis_expr):
        dim = m.group(1).strip()
        name = m.group(2).strip()

        # [Measures].[X] is a member reference — hierarchy is [Measures]
        if dim == "Measures":
            hier = "[Measures]"
        else:
            hier = f"[{dim}].[{name}]"

        if hier not in seen:
            seen.add(hier)
            hierarchies.append(hier)

    # Check for bare [Measures] not already captured
    if "[Measures]" in axis_expr and "[Measures]" not in seen:
        hierarchies.append("[Measures]")

    return hierarchies


def _extract_all_member_filters(axis_expr: str) -> dict[str, tuple[str, str]]:
    """
    Extract ALL level/member filters from an axis expression.
    Returns dict: {hierarchy_unique_name: (name, operation)}
    Handles patterns like:
      [Dim].[Hier].[Level].Members    → level query
      [Dim].[Hier].[Member].Children  → member-children query
    """
    result: dict[str, tuple[str, str]] = {}
    bracket_content = r'([^]]+)'
    for m in re.finditer(
        rf'\[{bracket_content}\]\.\[{bracket_content}\]\.\[{bracket_content}\]'
        rf'\s*\.(\w+)',
        axis_expr,
        re.IGNORECASE,
    ):
        dim = m.group(1).strip()
        hier_name = m.group(2).strip()
        third = m.group(3).strip()
        func = m.group(4).lower()
        hier = f"[{dim}].[{hier_name}]"
        if func == "children":
            result[hier] = (third, "children")
        elif func == "members":
            result[hier] = (third, "members")
        else:
            result[hier] = (third, "members")
    return result


def _extract_currentmember_ascendants(axis_expr: str) -> dict[str, str]:
    """Extract Ascendants([Dim].[Hier].CurrentMember) references from an axis expression."""
    result: dict[str, str] = {}
    for m in re.finditer(
        r'Ascendants\s*\(\s*\[([^\]]+)\]\.\[([^\]]+)\]\.currentmember\s*\)',
        axis_expr,
        re.IGNORECASE,
    ):
        dim = m.group(1).strip()
        hier_name = m.group(2).strip()
        result[f"[{dim}].[{hier_name}]"] = dim
    return result


def _extract_all_drilldown_members(axis_expr: str) -> dict[str, str]:
    """
    Extract ALL DrilldownLevel members from an axis expression.
    Returns dict: {hierarchy_unique_name: member_name}
    Handles: DrilldownLevel({[Dim].[Hier].[Member]})
    """
    result: dict[str, str] = {}
    for m in re.finditer(
        r'DrilldownLevel\s*\(\s*\{\s*\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]\s*\}\s*\)',
        axis_expr,
        re.IGNORECASE,
    ):
        dim = m.group(1).strip()
        hier_name = m.group(2).strip()
        member_name = m.group(3).strip()
        hier = f"[{dim}].[{hier_name}]"
        result[hier] = member_name
    return result


def _extract_drilldown_member_expansions(axis_expr: str) -> dict[str, list[str]]:
    """
    Parse DrilldownMember({{base_set}}, {member_list}) from axis expression.
    Returns dict: {hierarchy_unique_name: [member_names_to_expand]}

    DrilldownMember means: take the base set, and for each member in the
    drill list, replace it with itself + its children.

    Example: DrilldownMember({{DrilldownLevel({[Time].[Time].[All]})}}, {[Time].[Time].[2024]})
    → hierarchy=[Time].[Time], expand members=["2024"]

    B8 round-2 fix (deep-review Finding 4): member references may be
    path-qualified key form — ``[Time].[Time].[Month].&[2025]&[4]`` —
    the grammar the server itself emits on subtotal axes. The member to
    expand is the deepest key of the path; previously the level name was
    misread as the member.
    """
    from src.dax.member_uname import parse_member_keys

    result: dict[str, list[str]] = {}
    for m in re.finditer(
        r'DrilldownMember\s*\(\s*\{\{.+?\}\}\s*,\s*\{([^}]+)\}\s*\)',
        axis_expr,
        re.IGNORECASE,
    ):
        member_list_str = m.group(1).strip()
        # Parse member references: [Dim].[Hier].[Member] /
        # [Dim].[Hier].[Level].&[k0]&[k1]... / [Dim].[Hier].&[k0]&[k1]...
        members: list[str] = []
        hier = None
        for member_match in re.finditer(
            r'\[([^\]]+)\]\.\[([^\]]+)\](?:\.\[([^\]]+)\])?((?:\.?\&\[[^\]]+\])+)?',
            member_list_str,
        ):
            dim = member_match.group(1).strip()
            hier_name = member_match.group(2).strip()
            caption_part = member_match.group(3)
            keys_part = member_match.group(4)
            if keys_part:
                keys = parse_member_keys(keys_part)
                if not keys:
                    continue
                member_name = _normalize_member_name(keys[-1].strip())
            elif caption_part:
                member_name = _normalize_member_name(caption_part.strip())
            else:
                continue
            hier = f"[{dim}].[{hier_name}]"
            members.append(member_name)
        if hier and members:
            result[hier] = members
    return result


def _parse_where_measure(mdx: str) -> str | None:
    """Extract measure name from WHERE ([Measures].[MeasureName]) clause."""
    match = re.search(r'WHERE\s*\(\s*\[Measures\]\.\[([^\]]+)\]', mdx, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _extract_measure_names(expr: str) -> list[str]:
    """Extract measure references from an axis expression.
    Supports both [Measures].[name] and [Measures].name forms.
    """
    names: list[str] = []
    for pattern in (
        r'\[Measures\]\.\[([^\]]+)\]',
        r'\[Measures\]\.([A-Za-z_][A-Za-z0-9_]*)',
    ):
        for m in re.finditer(pattern, expr):
            name = m.group(1).strip()
            if name not in names:
                names.append(name)
    return names


def _parse_where_dimension_members(mdx: str) -> dict[str, str]:
    """Extract dimension member selections from the WHERE tuple.
    Returns dim name -> member caption, preserving [All] selections.
    """
    filters: dict[str, str] = {}
    match = re.search(r'WHERE\s*\(([^)]+)\)', mdx, re.IGNORECASE)
    if not match:
        return filters
    where_expr = match.group(1)
    for m in re.finditer(r'\[([^\]]+)\]\.\[([^\]]+)\]\.\[([^\]]+)\]', where_expr):
        dim = m.group(1).strip()
        member = m.group(3).strip().strip("()")
        if dim != "Measures" and dim not in filters:
            filters[dim] = member
    return filters


def _normalize_member_name(name: str) -> str:
    """Normalize a member name by stripping surrounding parentheses.
    Excel sends both [All] and [(All)] for the All member.
    The demo data uses "All" (without parens) as the member name.
    """
    if name.startswith("(") and name.endswith(")"):
        return name[1:-1]
    return name


def _build_olap_info(
    cube: str,
    col_hierarchies: list[str],
    row_hierarchies: list[str],
    slicer_dims: list[str],
    slicer_measure: str | None,
    dims: dict[str, Any],
    dim_props: list[str],
    minimal_excel_props: bool = False,
) -> str:
    """Build the OlapInfo section of the MDDataSet response.
    Order matches the MDDataSet schema: CubeInfo → AxesInfo → CellInfo."""
    xml = '<OlapInfo>'

    # CubeInfo
    _ts = _meta_modified()
    xml += (
        f'<CubeInfo><Cube>'
        f'<CubeName>{_xe(cube)}</CubeName>'
        f'<LastDataUpdate xmlns="http://schemas.microsoft.com/analysisservices/2003/engine">'
        f'{_ts}</LastDataUpdate>'
        f'<LastSchemaUpdate xmlns="http://schemas.microsoft.com/analysisservices/2003/engine">'
        f'{_ts}</LastSchemaUpdate>'
        f'</Cube></CubeInfo>'
    )

    # AxesInfo
    xml += '<AxesInfo>'

    # Column axis info
    if col_hierarchies:
        xml += '<AxisInfo name="Axis0">'
        for hier in col_hierarchies:
            xml += _hierarchy_info(
                hier,
                [] if hier == "[Measures]" else dim_props,
                minimal_excel_props=minimal_excel_props,
            )
        xml += '</AxisInfo>'

    # Row axis info
    if row_hierarchies:
        xml += '<AxisInfo name="Axis1">'
        for hier in row_hierarchies:
            xml += _hierarchy_info(
                hier,
                [] if hier == "[Measures]" else dim_props,
                minimal_excel_props=minimal_excel_props,
            )
        xml += '</AxisInfo>'

    # Slicer axis info (non-queried dims + measures if not on an axis)
    xml += '<AxisInfo name="SlicerAxis">'
    if slicer_measure:
        xml += _hierarchy_info(
            "[Measures]",
            [],
            minimal_excel_props=minimal_excel_props,
        )
    for dname in slicer_dims:
        dim = dims[dname]
        hier = dim["hierarchy"]
        xml += _hierarchy_info(
            hier,
            [],
            minimal_excel_props=minimal_excel_props,
        )
    xml += '</AxisInfo>'

    xml += '</AxesInfo>'

    # CellInfo
    xml += (
        '<CellInfo>'
        '<Value name="VALUE"/>'
        '<FmtValue name="FORMATTED_VALUE" type="xs:string"/>'  # Bug-5432
        '<FormatString name="FORMAT_STRING" type="xs:string"/>'
        '<Language name="LANGUAGE" type="xs:unsignedInt"/>'
        '<BackColor name="BACK_COLOR" type="xs:unsignedInt"/>'
        '<ForeColor name="FORE_COLOR" type="xs:unsignedInt"/>'
        '<FontFlags name="FONT_FLAGS" type="xs:int"/>'
        '</CellInfo>'
    )

    xml += '</OlapInfo>'
    return xml


def _parse_dimension_properties(mdx: str) -> list[dict[str, str | None]]:
    """Extract all property names from DIMENSION PROPERTIES clauses.
    Returns a list of dicts with:
    - tag: bare property tag, e.g. MEMBER_KEY
    - name_attr: exact requested property token
    - hierarchy: optional hierarchy unique name when the property was scoped
      like [Dim].[Hier].[Level].[MEMBER_KEY]

    The character class for the property list must include `()` because
    Excel scopes properties to the (All) level — e.g.
    `[country_code].[country_code].[(All)].[MEMBER_KEY]`. Pre-Bug-XMLA-001b
    the class was `[\\w\\[\\].\\s,]` which choked on the `(` and the
    entire DIMENSION PROPERTIES clause silently parsed as empty.
    """
    props: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str]] = set()
    for m in re.finditer(r'DIMENSION\s+PROPERTIES\s+([\w\[\]().\s,]+?)(?:\s+ON\b)', mdx, re.IGNORECASE):
        for token in re.split(r'\s*,\s*', m.group(1).strip()):
            token = token.strip()
            if not token:
                continue
            hierarchy: str | None = None
            scoped = re.match(r'^\[([^\]]+)\]\.\[([^\]]+)\]\.\[[^\]]+\]\.\[([^\]]+)\]$', token)
            if scoped:
                hierarchy = f'[{scoped.group(1).strip()}].[{scoped.group(2).strip()}]'
                bare = scoped.group(3).strip()
            else:
                bare = re.sub(r'^(?:\[[^\]]+\]\.)+', '', token)
                bare = bare.strip('[]')
            key = (hierarchy, bare)
            if bare and key not in seen:
                seen.add(key)
                props.append({"tag": bare, "name_attr": token, "hierarchy": hierarchy})
    return props


# Type and default value for each known dimension property
_DIM_PROP_TYPES: dict[str, tuple[str, str]] = {
    "PARENT_UNIQUE_NAME": ("xs:string", ""),
    "HIERARCHY_UNIQUE_NAME": ("xs:string", ""),
    "MEMBER_TYPE": ("xs:int", "1"),
    "MEMBER_ORDINAL": ("xs:unsignedInt", "0"),
    "CHILDREN_CARDINALITY": ("xs:unsignedInt", "0"),
    "MEMBER_KEY": ("xs:string", ""),
    "MEMBER_VALUE": ("xs:string", ""),
    # UNIQUE_NAME is the member's unique name — same as UName element
    "UNIQUE_NAME": ("xs:string", ""),
    "MEMBER_UNIQUE_NAME": ("xs:string", ""),
    "MEMBER_NAME": ("xs:string", ""),
    "MEMBER_CAPTION": ("xs:string", ""),
    "LEVEL_UNIQUE_NAME": ("xs:string", ""),
    "LEVEL_NUMBER": ("xs:int", "0"),
    "DISPLAY_INFO": ("xs:unsignedInt", "0"),
}


def _hierarchy_info(
    hier: str,
    dim_props: list[dict[str, str | None]],
    minimal_excel_props: bool = False,
) -> str:
    """Build a HierarchyInfo element for AxesInfo.
    Declares all standard properties plus any DIMENSION PROPERTIES
    requested in the MDX. Type attributes match OlaPy."""
    h = _xe(hier)
    xml = f'<HierarchyInfo name="{h}">'
    xml += f'<UName name="{h}.[MEMBER_UNIQUE_NAME]" type="xs:string"/>'
    xml += f'<Caption name="{h}.[MEMBER_CAPTION]" type="xs:string"/>'
    xml += f'<LName name="{h}.[LEVEL_UNIQUE_NAME]" type="xs:string"/>'
    xml += f'<LNum name="{h}.[LEVEL_NUMBER]" type="xs:int"/>'
    xml += f'<DisplayInfo name="{h}.[DISPLAY_INFO]" type="xs:unsignedInt"/>'
    emitted: set[str] = {
        "MEMBER_UNIQUE_NAME",
        "MEMBER_CAPTION",
        "LEVEL_UNIQUE_NAME",
        "LEVEL_NUMBER",
        "DISPLAY_INFO",
    }

    if hier != "[Measures]":
        if minimal_excel_props:
            # Excel compatibility mode: keep the base member metadata minimal
            # and rely on DIMENSION PROPERTIES for additional attributes.
            xml += f'<MEMBER_TYPE name="{h}.[MEMBER_TYPE]" type="xs:int"/>'
            emitted.update({"MEMBER_TYPE"})
        else:
            xml += f'<PARENT_UNIQUE_NAME name="{h}.[PARENT_UNIQUE_NAME]" type="xs:string"/>'
            xml += f'<HIERARCHY_UNIQUE_NAME name="{h}.[HIERARCHY_UNIQUE_NAME]" type="xs:string"/>'
            xml += f'<MEMBER_TYPE name="{h}.[MEMBER_TYPE]" type="xs:int"/>'
            emitted.update(
                {
                    "PARENT_UNIQUE_NAME",
                    "HIERARCHY_UNIQUE_NAME",
                    "MEMBER_TYPE",
                }
            )
            xml += f'<MEMBER_ORDINAL name="{h}.[MEMBER_ORDINAL]" type="xs:unsignedInt"/>'
            xml += f'<CHILDREN_CARDINALITY name="{h}.[CHILDREN_CARDINALITY]" type="xs:unsignedInt"/>'
            xml += f'<MEMBER_KEY name="{h}.[MEMBER_KEY]" type="xs:string"/>'
            xml += f'<MEMBER_VALUE name="{h}.[MEMBER_VALUE]" type="xs:string"/>'
            xml += f'<MEMBER_NAME name="{h}.[MEMBER_NAME]" type="xs:string"/>'
            emitted.update(
                {
                    "MEMBER_ORDINAL",
                    "CHILDREN_CARDINALITY",
                    "MEMBER_KEY",
                    "MEMBER_VALUE",
                    "MEMBER_NAME",
                }
            )

    for prop_info in dim_props:
        prop_hier = prop_info.get("hierarchy")
        if prop_hier and prop_hier != hier:
            continue
        prop = prop_info["tag"]
        if prop in emitted:
            continue
        # The `name` attribute identifies the property in the AxisInfo
        # declaration. Pre-Bug-XMLA-001 we passed through the exact MDX
        # token (e.g. `[X].[X].[(All)].[MEMBER_KEY]`), which scoped the
        # property to the (All) level. When the response actually contains
        # leaf-level members rather than the (All) member, MSOLAP rejects
        # the schema mismatch. Always declare the property at the
        # hierarchy level — it then applies to whatever members the
        # response returns.
        name_attr = f"{hier}.[{prop}]"

        xsd_type = _DIM_PROP_TYPES.get(prop, ("xs:string", ""))[0]
        xml += f'<{prop} name="{_xe(name_attr)}" type="{xsd_type}"/>'
        emitted.add(prop)

    xml += '</HierarchyInfo>'
    return xml


def _build_cross_product_tuples(
    hierarchies: list[str],
    members: list[dict],
) -> list[list[dict]]:
    """
    For a CrossJoin axis, build the cartesian product of members across hierarchies.
    Returns a list of tuples, each tuple being a list of one member per hierarchy.
    """
    # Group members by hierarchy preserving order
    by_hier: dict[str, list[dict]] = {h: [] for h in hierarchies}
    for m in members:
        h = m["hierarchy"]
        if h in by_hier:
            by_hier[h].append(m)

    groups = [by_hier[h] for h in hierarchies if by_hier.get(h)]
    if not groups:
        return []

    return [list(combo) for combo in product(*groups)]


def _build_existing_axis_tuples(
    hierarchies: list[str],
    rows: list[dict[str, Any]],
) -> list[list[dict[str, str]]]:
    """Build only the tuples that actually exist in the result rows for a multi-hierarchy axis.

    Bug-XMLA-003 fix: every member carries a unique ``member_ordinal``
    within its hierarchy (0, 1, 2, ... in first-appearance order), a
    full ``name``/``key``/``parent`` triple, and a ``children_cardinality``
    of zero. Without these, a CrossJoin axis with repeated members
    (which is normal — e.g. ``AE`` appearing in three tuples because
    of three customer segments) causes Excel's MSOLAP client to crash
    with ``RPC failed`` because the response violates an internal
    uniqueness invariant it assumes for tuple member dictionaries.

    The cartesian tuple list itself is still built from the result
    rows so only the combinations Excel will actually render are
    included (``NonEmpty`` semantics).
    """
    # Assign each distinct value within each hierarchy a stable
    # first-appearance ordinal. Two tuples that share the same
    # country_code member emit the same ordinal for that slot.
    ordinal_by_member: dict[tuple[str, str], int] = {}
    ordinal_counter: dict[str, int] = {}

    tuples: list[list[dict[str, str]]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        members: list[dict[str, str]] = []
        key_parts: list[str] = []
        valid = True
        for hier in hierarchies:
            dim_match = re.match(r'\[([^\]]+)\]', hier)
            if not dim_match:
                valid = False
                break
            dname = dim_match.group(1).strip()
            value = row.get(dname)
            if value in (None, ""):
                valid = False
                break
            sval = str(value)
            key_parts.append(sval)

            ord_key = (hier, sval)
            if ord_key not in ordinal_by_member:
                ordinal_by_member[ord_key] = ordinal_counter.get(hier, 0)
                ordinal_counter[hier] = ordinal_counter.get(hier, 0) + 1
            member_ordinal = ordinal_by_member[ord_key]

            members.append({
                "hierarchy": hier,
                "uname": f"{hier}.[{sval}]",
                "name": sval,
                "key": sval,
                "caption": sval,
                "lname": f"{hier}.[{dname}]",
                "lnum": "1",
                "parent": f"{hier}.[All]",
                "has_children": False,
                "member_type": 1,
                "member_ordinal": member_ordinal,
                "children_cardinality": 0,
            })
        key = tuple(key_parts)
        if valid and key not in seen:
            seen.add(key)
            tuples.append(members)
    return tuples


def _build_axes(
    col_hierarchies: list[str],
    col_members: list[dict],
    row_hierarchies: list[str],
    row_members: list[dict],
    slicer_dims: list[str],
    slicer_measure: str | None,
    dims: dict[str, Any],
    measures: dict[str, Any],
    dim_props: list[str] | None = None,
    axis_format: str | None = None,
    col_axis_tuples: list[list[dict[str, str]]] | None = None,
    row_axis_tuples: list[list[dict[str, str]]] | None = None,
    minimal_excel_props: bool = False,
) -> str:
    """Build the Axes section of the MDDataSet response."""
    dp = dim_props or []
    axis_fmt = (axis_format or "").strip().lower()
    # Keep single-hierarchy axes as Members by default. Only explicit
    # TupleFormat/CustomFormat requests should force tuple payloads.
    use_tuple_format = axis_fmt in ("tupleformat", "customformat") or minimal_excel_props
    xml = '<Axes>'

    # Axis0 (columns)
    if col_hierarchies:
        if len(col_hierarchies) > 1 or use_tuple_format:
            xml += '<Axis name="Axis0"><Tuples>'
            tuples = col_axis_tuples if col_axis_tuples is not None else (
                _build_cross_product_tuples(col_hierarchies, col_members)
                if len(col_hierarchies) > 1
                else [[m] for m in col_members]
            )
            for members in tuples:
                xml += _member_tuple(members, dp, minimal_excel_props=minimal_excel_props)
            xml += '</Tuples></Axis>'
        else:
            xml += f'<Axis name="Axis0"><Members Hierarchy="{_xe(col_hierarchies[0])}">'
            for m in col_members:
                xml += _member_xml(m, dp, minimal_excel_props=minimal_excel_props)
            xml += '</Members></Axis>'

    # Axis1 (rows)
    if row_hierarchies:
        if len(row_hierarchies) > 1 or use_tuple_format:
            xml += '<Axis name="Axis1"><Tuples>'
            tuples = row_axis_tuples if row_axis_tuples is not None else (
                _build_cross_product_tuples(row_hierarchies, row_members)
                if len(row_hierarchies) > 1
                else [[m] for m in row_members]
            )
            for members in tuples:
                xml += _member_tuple(members, dp, minimal_excel_props=minimal_excel_props)
            xml += '</Tuples></Axis>'
        else:
            xml += f'<Axis name="Axis1"><Members Hierarchy="{_xe(row_hierarchies[0])}">'
            for m in row_members:
                xml += _member_xml(m, dp, minimal_excel_props=minimal_excel_props)
            xml += '</Members></Axis>'

    # SlicerAxis
    slicer_members = []
    if slicer_measure:
        slicer_members.append({
            "hierarchy": "[Measures]",
            "uname": f"[Measures].[{slicer_measure}]",
            "caption": slicer_measure,
            "lname": "[Measures]",
            "lnum": 0,
            "has_children": False,
            "parent": "",
            "member_type": 3,
        })
    for dname in slicer_dims:
        dim = dims[dname]
        hier = dim["hierarchy"]
        first_member = dim["members"][0] if dim["members"] else {"name": "All", "level": "(All)", "member_type": 2}
        mlevel = first_member.get("level", "(All)")
        uname = f'{hier}.[{first_member["name"]}]'
        slicer_members.append({
            "hierarchy": hier,
            "uname": uname,
            "caption": first_member.get("caption", first_member["name"]),
            "lname": f'{hier}.[{mlevel}]',
            "lnum": 0,
            "has_children": False,
            "parent": "",
            "member_type": first_member.get("member_type", 2 if first_member["name"] == "All" else 1),
            "children_cardinality": first_member.get("children_cardinality", 0),
        })
        
    if slicer_members:
        xml += '<Axis name="SlicerAxis"><Tuples>'
        xml += _member_tuple(slicer_members, [], minimal_excel_props=minimal_excel_props)
        xml += '</Tuples></Axis>'
    else:
        xml += '<Axis name="SlicerAxis"></Axis>'

    xml += '</Axes>'
    return xml


def _member_tuple(
    members: list[dict],
    dp: list[dict[str, str | None]],
    minimal_excel_props: bool = False,
) -> str:
    """
    Build a Tuple element containing one or more Member elements.
    Emits all DIMENSION PROPERTIES declared in dim_props so MSOLAP's
    MoveToHierProperty can find every property it expects.

    CRITICAL: Every property declared in HierarchyInfo MUST appear on every
    Member element in the same axis — even if the value is empty. Omitting
    a declared property causes MSOLAP's MoveToHierProperty to crash.
    """
    xml = '<Tuple>'
    for member in members:
        xml += _member_xml(member, dp, minimal_excel_props=minimal_excel_props)
    xml += '</Tuple>'
    return xml


def _member_xml(
    member: dict,
    dp: list[dict[str, str | None]],
    minimal_excel_props: bool = False,
) -> str:
    """Build a single Member element."""
    hier = member["hierarchy"]
    is_measure = "[Measures]" in hier
    # DisplayInfo: MSOLAP uses bit flags. 131072 = has children flag.
    # OlaPy uses 131076 for members with children, 0 for leaf members.
    has_children = member.get("has_children", False)
    display_info = "131076" if has_children else "0"

    xml = f'<Member Hierarchy="{_xe(hier)}">'
    xml += f'<UName>{_xe(member["uname"])}</UName>'
    xml += f'<Caption>{_xe(member["caption"])}</Caption>'
    xml += f'<LName>{_xe(member["lname"])}</LName>'
    xml += f'<LNum>{member["lnum"]}</LNum>'
    xml += f'<DisplayInfo>{display_info}</DisplayInfo>'

    # Emit EVERY requested dimension property on EVERY member.
    # Omitting a property that HierarchyInfo declares crashes MSOLAP, and
    # emitting one that HierarchyInfo does NOT declare also crashes MSOLAP.
    # The base UName/Caption/LName/LNum/DisplayInfo elements above already
    # carry the MEMBER_UNIQUE_NAME / MEMBER_CAPTION / LEVEL_UNIQUE_NAME /
    # LEVEL_NUMBER / DISPLAY_INFO values, so those property names must be
    # in `emitted_props` regardless of which mode we're in — otherwise the
    # dim_props loop will re-emit them as ALL-CAPS elements that do not
    # appear in the AxisInfo declaration.
    emitted_props: set[str] = {
        "MEMBER_UNIQUE_NAME",
        "MEMBER_CAPTION",
        "LEVEL_UNIQUE_NAME",
        "LEVEL_NUMBER",
        "DISPLAY_INFO",
    }
    if is_measure:
        xml += '</Member>'
        return xml
    member_ordinal = member.get("member_ordinal", 0)
    member_value = member.get("value", member.get("key", member["caption"]))

    if minimal_excel_props:
        xml += f'<MEMBER_TYPE>{member.get("member_type", 1)}</MEMBER_TYPE>'
        emitted_props.add("MEMBER_TYPE")
    else:
        xml += f'<PARENT_UNIQUE_NAME>{_xe(member.get("parent", ""))}</PARENT_UNIQUE_NAME>'
        xml += f'<HIERARCHY_UNIQUE_NAME>{_xe(hier)}</HIERARCHY_UNIQUE_NAME>'
        xml += f'<MEMBER_TYPE>{member.get("member_type", 1)}</MEMBER_TYPE>'
        emitted_props.update(
            {
                "PARENT_UNIQUE_NAME",
                "HIERARCHY_UNIQUE_NAME",
                "MEMBER_TYPE",
            }
        )
        xml += f'<MEMBER_ORDINAL>{member_ordinal}</MEMBER_ORDINAL>'
        xml += f'<CHILDREN_CARDINALITY>{member.get("children_cardinality", 0)}</CHILDREN_CARDINALITY>'
        xml += f'<MEMBER_KEY>{_xe(member.get("key", member["caption"]))}</MEMBER_KEY>'
        xml += f'<MEMBER_VALUE>{_xe(member_value)}</MEMBER_VALUE>'
        xml += f'<MEMBER_NAME>{_xe(member.get("name", member["caption"]))}</MEMBER_NAME>'
        emitted_props.update(
            {
                "MEMBER_ORDINAL",
                "CHILDREN_CARDINALITY",
                "MEMBER_KEY",
                "MEMBER_VALUE",
                "MEMBER_NAME",
            }
        )

    for prop_info in dp:
        prop_hier = prop_info.get("hierarchy")
        if prop_hier and prop_hier != hier:
            continue
        prop = prop_info["tag"]
        if prop in emitted_props:
            continue
        emitted_props.add(prop)
        if prop == "PARENT_UNIQUE_NAME":
            # Must be present on ALL members — empty string for root members
            val = member.get("parent", "")
            xml += f'<PARENT_UNIQUE_NAME>{_xe(val)}</PARENT_UNIQUE_NAME>'
        elif prop == "HIERARCHY_UNIQUE_NAME":
            xml += f'<HIERARCHY_UNIQUE_NAME>{_xe(hier)}</HIERARCHY_UNIQUE_NAME>'
        elif prop == "MEMBER_TYPE":
            xml += f'<MEMBER_TYPE>{member.get("member_type", 1)}</MEMBER_TYPE>'
        elif prop == "MEMBER_ORDINAL":
            xml += f'<MEMBER_ORDINAL>{member_ordinal}</MEMBER_ORDINAL>'
        elif prop == "CHILDREN_CARDINALITY":
            cc = member.get("children_cardinality", 0)
            xml += f'<CHILDREN_CARDINALITY>{cc}</CHILDREN_CARDINALITY>'
        elif prop == "MEMBER_KEY":
            xml += f'<MEMBER_KEY>{_xe(member.get("key", member["caption"]))}</MEMBER_KEY>'
        elif prop == "MEMBER_VALUE":
            xml += f'<MEMBER_VALUE>{_xe(member_value)}</MEMBER_VALUE>'
        elif prop == "UNIQUE_NAME" or prop == "MEMBER_UNIQUE_NAME":
            xml += f'<{prop}>{_xe(member["uname"])}</{prop}>'
        elif prop == "MEMBER_NAME":
            xml += f'<MEMBER_NAME>{_xe(member.get("name", member["caption"]))}</MEMBER_NAME>'
        elif prop == "MEMBER_CAPTION":
            xml += f'<MEMBER_CAPTION>{_xe(member["caption"])}</MEMBER_CAPTION>'
        elif prop == "LEVEL_UNIQUE_NAME":
            xml += f'<LEVEL_UNIQUE_NAME>{_xe(member["lname"])}</LEVEL_UNIQUE_NAME>'
        elif prop == "LEVEL_NUMBER":
            xml += f'<LEVEL_NUMBER>{member["lnum"]}</LEVEL_NUMBER>'
        elif prop == "DISPLAY_INFO":
            xml += f'<DISPLAY_INFO>{display_info}</DISPLAY_INFO>'
        else:
            default = _DIM_PROP_TYPES.get(prop, ("xs:string", ""))[1]
            xml += f'<{prop}>{_xe(default)}</{prop}>'

    xml += '</Member>'
    return xml


def _build_subtotal_row_members(
    rows: list[dict[str, Any]],
    subtotal_hierarchy: Any,
    mdx_dim_name: str,
    mdx_hier_name: str,
) -> list[dict[str, str]]:
    """Build multi-level row axis members from subtotal-tagged rows.

    Each row maps to exactly one axis tuple. Grand total rows get
    (All) member, subtotal rows get their level member, detail rows
    get the leaf level member.

    B8 round-2 fix (deep-review Finding 2): this builder now uses the
    same path-qualified uname grammar and stable per-member ordinals as
    ``_build_multi_hierarchy_row_tuples``. Previously it emitted
    ``[Cal].[Cal].[Month].&[4]`` for month 4 of both 2025 and 2026 with
    raw-row-index ordinals — one member identity with two contradictory
    parents and ordinals, violating the MSOLAP uniqueness invariant
    (Bug-XMLA-003). Cell values were unaffected (no dedup in this path);
    only member identity changes.
    """
    from src.dax.subtotal_engine import SUBTOTAL_GRAIN_KEY

    hier_bracket = f"[{mdx_dim_name}].[{mdx_hier_name}]"
    members: list[dict[str, str]] = []

    levels = subtotal_hierarchy.levels
    leaf_ordinal = levels[-1].ordinal if levels else 0

    ordinal_by_member: dict[str, int] = {}

    def _stable_ordinal(uname: str) -> int:
        if uname not in ordinal_by_member:
            ordinal_by_member[uname] = len(ordinal_by_member)
        return ordinal_by_member[uname]

    for row in rows:
        grain = row.get(SUBTOTAL_GRAIN_KEY, leaf_ordinal)

        if grain == -1:
            # Bug-5433: the (All) MEMBER unique name is [Hier].[All] (member name
            # "All"); [(All)] is the LEVEL name, kept on lname only. Aligns the
            # Execute axis with DISCOVER (MDSCHEMA_MEMBERS), which emits [All].
            all_uname = f"{hier_bracket}.[All]"
            members.append({
                "hierarchy": hier_bracket,
                "uname": all_uname,
                "name": "All",
                "key": "All",
                "caption": "All",
                "lname": f"{hier_bracket}.[(All)]",
                "lnum": "0",
                "parent": "",
                "has_children": True,
                "member_type": 2,
                "member_ordinal": _stable_ordinal(all_uname),
            })
            continue

        level_idx = next(
            (i for i, l in enumerate(levels) if l.ordinal == grain),
            0,
        )
        level = levels[level_idx] if levels else None
        is_leaf = grain == leaf_ordinal
        lname = level.name if level else ("Detail" if is_leaf else "Unknown")
        dim_name = level.dim_name if level else ""
        val = str(row.get(dim_name, ""))
        # Ancestor key path read straight off the row — every higher-level
        # dim value for this grain is present in the merged result.
        key_path = [
            str(row.get(l.dim_name, "")) for l in levels[: level_idx + 1]
        ]
        if level_idx > 0:
            parent_level = levels[level_idx - 1]
            parent_uname = (
                _path_qualified_uname(
                    hier_bracket, parent_level.name, key_path[:-1],
                )
                if str(row.get(parent_level.dim_name, ""))
                else f"{hier_bracket}.[All]"  # Bug-5433: (All) member form
            )
        else:
            parent_uname = f"{hier_bracket}.[All]"  # Bug-5433: (All) member form
        uname = _path_qualified_uname(hier_bracket, lname, key_path)
        members.append({
            "hierarchy": hier_bracket,
            "uname": uname,
            "name": val,
            "key": val,
            "caption": val,
            "lname": f"{hier_bracket}.[{lname}]",
            "lnum": str(len(levels)) if is_leaf else str(level_idx + 1),
            "parent": parent_uname,
            "has_children": not is_leaf,
            "member_type": 1,
            "member_ordinal": _stable_ordinal(uname),
        })

    return members


def _path_qualified_uname(
    hier_bracket: str,
    level_name: str,
    key_path: list[str],
) -> str:
    """SSAS-style member unique name carrying the full ancestor key path.

    F-002-01 fix: unames like ``[Cal].[Cal].[Month].&[4]`` are ambiguous —
    month 4 of 2025 and month 4 of 2026 collide, which both confuses Excel
    member identity and causes false tuple deduplication (shifting every
    subsequent cell against the axis). The SSAS convention is
    ``[Cal].[Cal].[Month].&[2025]&[4]``.

    B8 round-2: delegates to ``member_uname.qualify_member_uname`` — the
    single grammar shared with every parser that consumes unames.
    """
    from src.dax.member_uname import qualify_member_uname
    return qualify_member_uname(hier_bracket, level_name, key_path)


def _build_multi_hierarchy_row_tuples(
    rows: list[dict[str, Any]],
    subtotal_hierarchies: list,
) -> list[list[dict[str, str]]]:
    """Build row axis tuples for multi-hierarchy subtotal responses.

    Each row maps to exactly one tuple. For each hierarchy, the
    per-hierarchy grain tag determines which level the member belongs to.

    Member unames are path-qualified (see ``_path_qualified_uname``) so
    that members with equal captions at the same level but different
    ancestors stay distinct, and ``member_ordinal`` is stable per distinct
    member within its hierarchy (Bug-XMLA-003 invariant: repeated members
    across tuples must carry identical ordinals or MSOLAP rejects the
    response).
    """
    from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX

    ordinal_by_member: dict[tuple[str, str], int] = {}
    ordinal_counter: dict[str, int] = {}

    def _stable_ordinal(hier_bracket: str, uname: str) -> int:
        key = (hier_bracket, uname)
        if key not in ordinal_by_member:
            ordinal_by_member[key] = ordinal_counter.get(hier_bracket, 0)
            ordinal_counter[hier_bracket] = ordinal_counter.get(hier_bracket, 0) + 1
        return ordinal_by_member[key]

    tuples: list[list[dict[str, str]]] = []
    for row in rows:
        members: list[dict[str, str]] = []
        for h in subtotal_hierarchies:
            hier_bracket = f"[{h.mdx_dim_name}].[{h.mdx_hier_name}]"
            grain = row.get(SUBTOTAL_GRAIN_PREFIX + h.hierarchy_name, -2)
            leaf_ordinal = h.levels[-1].ordinal if h.levels else 0

            if grain == -1:
                # Bug-5433: (All) MEMBER uname is [Hier].[All]; [(All)] is the level.
                all_uname = f"{hier_bracket}.[All]"
                members.append({
                    "hierarchy": hier_bracket,
                    "uname": all_uname,
                    "name": "All",
                    "key": "All",
                    "caption": "All",
                    "lname": f"{hier_bracket}.[(All)]",
                    "lnum": "0",
                    "parent": "",
                    "has_children": True,
                    "member_type": 2,
                    "member_ordinal": _stable_ordinal(hier_bracket, all_uname),
                    "children_cardinality": 0,
                })
                continue

            level_idx = next(
                (i for i, l in enumerate(h.levels) if l.ordinal == grain),
                0,
            )
            level = h.levels[level_idx] if h.levels else None
            is_leaf = grain == leaf_ordinal
            lname = level.name if level else ("Detail" if is_leaf else "Unknown")
            dim_name = level.dim_name if level else ""
            val = str(row.get(dim_name, ""))
            # Ancestor key path: the row carries every higher-level dim
            # value for its grain, so the path is read straight off the row.
            key_path = [
                str(row.get(l.dim_name, "")) for l in h.levels[: level_idx + 1]
            ]
            if level_idx > 0:
                parent_level = h.levels[level_idx - 1]
                parent_uname = (
                    _path_qualified_uname(
                        hier_bracket, parent_level.name, key_path[:-1],
                    )
                    if str(row.get(parent_level.dim_name, ""))
                    else f"{hier_bracket}.[All]"  # Bug-5433
                )
            else:
                parent_uname = f"{hier_bracket}.[All]"  # Bug-5433
            uname = _path_qualified_uname(hier_bracket, lname, key_path)
            members.append({
                "hierarchy": hier_bracket,
                "uname": uname,
                "name": val,
                "key": val,
                "caption": val,
                "lname": f"{hier_bracket}.[{lname}]",
                "lnum": str(len(h.levels)) if is_leaf else str(level_idx + 1),
                "parent": parent_uname,
                "has_children": not is_leaf,
                "member_type": 1,
                "member_ordinal": _stable_ordinal(hier_bracket, uname),
                "children_cardinality": 0,
            })
        tuples.append(members)
    return tuples


def _deduplicate_axis_tuples(
    tuples: list[list[dict[str, str]]],
) -> list[list[dict[str, str]]]:
    """Return unique tuples preserving first-seen order."""
    seen: dict[tuple[str, ...], int] = {}
    unique: list[list[dict[str, str]]] = []
    for t in tuples:
        key = tuple(m.get("uname", "") for m in t)
        if key not in seen:
            seen[key] = len(unique)
            unique.append(t)
    return unique


def _build_cross_axis_subtotal_cell_data(
    rows: list[dict[str, Any]],
    per_row_row_tuples: list[list[dict[str, str]]],
    per_row_col_tuples: list[list[dict[str, str]]],
    measure_cols: list[str],
    slicer_measure: str | None,
    measures_meta: list[dict[str, Any]] | None = None,
    calc_format_map: dict[str, str] | None = None,
) -> str:
    """Build CellData for cross-axis subtotal responses.

    When hierarchies span both row and column axes, each flat row
    corresponds to one (row_position, col_position) cell.  Ordinal is
    ``row_idx * num_unique_cols * num_measures + col_idx * num_measures + m_idx``.
    """

    def _tkey(members: list[dict]) -> tuple[str, ...]:
        return tuple(m.get("uname", "") for m in members)

    row_index: dict[tuple[str, ...], int] = {}
    for t in per_row_row_tuples:
        k = _tkey(t)
        if k not in row_index:
            row_index[k] = len(row_index)

    col_index: dict[tuple[str, ...], int] = {}
    for t in per_row_col_tuples:
        k = _tkey(t)
        if k not in col_index:
            col_index[k] = len(col_index)

    active_measures = [slicer_measure] if slicer_measure else measure_cols
    num_cols = len(col_index)
    num_measures = len(active_measures)

    format_map: dict[str, str] = {}
    if measures_meta:
        for m in measures_meta:
            # Bug-5432: translate the Tessallite format TOKEN (percent_2dp,
            # currency, …) to an SSAS/.NET FORMAT_STRING for FmtValue/FORMAT_STRING.
            fmt = format_token_to_mdx(m.get("format"))
            if fmt:
                format_map[m.get("name", "")] = fmt
    if calc_format_map:
        format_map.update(calc_format_map)

    xml_parts: list[str] = ["<CellData>"]
    # B8 round-2 (deep-review Finding 5): duplicate (row, col) tuple keys
    # would emit two cells at one ordinal — skip and log loudly instead.
    emitted_ordinals: set[int] = set()
    for row, r_tuple, c_tuple in zip(rows, per_row_row_tuples, per_row_col_tuples):
        r_pos = row_index[_tkey(r_tuple)]
        c_pos = col_index[_tkey(c_tuple)]

        for m_idx, mname in enumerate(active_measures):
            ordinal = r_pos * num_cols * num_measures + c_pos * num_measures + m_idx
            val = row.get(mname)
            if val is not None and ordinal in emitted_ordinals:
                logger.warning(
                    "Duplicate CellOrdinal %s for measure %s — duplicate "
                    "axis tuple keys upstream; cell skipped (B8 round 2).",
                    ordinal, mname,
                )
                continue
            if val is not None:
                emitted_ordinals.add(ordinal)
                try:
                    fval = float(val)
                    fmt_str = format_map.get(mname, "")
                    xml_parts.append(
                        f'<Cell CellOrdinal="{ordinal}">'
                        f'<Value xsi:type="xsd:double">{fval}</Value>'
                    )
                    _fmt_val = _format_cell_value(fval, fmt_str)  # Bug-5432
                    if _fmt_val is not None:
                        xml_parts.append(f'<FmtValue>{_escape_xml(_fmt_val)}</FmtValue>')
                    if fmt_str:
                        xml_parts.append(
                            f'<FormatString>{_escape_xml(fmt_str)}</FormatString>'
                        )
                    xml_parts.append("</Cell>")
                except (ValueError, TypeError):
                    xml_parts.append(
                        f'<Cell CellOrdinal="{ordinal}">'
                        f'<Value xsi:type="xsd:string">{_escape_xml(str(val))}</Value>'
                        f'</Cell>'
                    )

    xml_parts.append("</CellData>")
    return "".join(xml_parts)


def _build_flat_column_positions(
    col_hierarchies: list[str],
    col_members: list[dict],
    measure_cols: list[str],
    slicer_measure: str | None,
) -> tuple[
    list[list[dict[str, str]]] | None,
    list[str],
    list[tuple[tuple[str, ...], str]],
] | None:
    """Column-axis position map for mixed subtotal shapes.

    B8 round-2 fix (deep-review Finding 3): when subtotal hierarchies sit
    on the rows axis while the columns axis carries one or more flat
    (non-subtotal) dimensions — Excel's "two hierarchies in Rows + a flat
    attribute in Columns" layout — each cell's column position depends on
    which column-dim member its data row belongs to. The previous ordinal
    formula ignored the row's column-dim value, writing every measure cell
    of a row at the same column position (duplicate CellOrdinals), and
    ``_build_existing_axis_tuples`` rendered Axis0 empty because it treats
    ``[Measures]`` as a row column lookup.

    Returns ``(axis_tuples, flat_dim_names, position_entries)`` where
    ``position_entries[i] = (flat_dim_value_combo, measure_name)`` for
    column position ``i``. ``axis_tuples`` is None when the axis renders
    ``col_members`` directly (single flat hierarchy, Members format).
    Returns None when the columns axis has no flat dims or the shape is
    not representable — the caller keeps the previously validated
    behavior.
    """
    flat_hiers = [h for h in col_hierarchies if "[Measures]" not in h]
    if not flat_hiers:
        return None

    has_measures_axis = any("[Measures]" in h for h in col_hierarchies)
    active_measures = (
        [slicer_measure] if slicer_measure else list(measure_cols)
    )
    if not has_measures_axis and len(active_measures) != 1:
        # No measure coordinate on the axis and more than one measure in
        # play — not representable; keep existing behavior.
        return None

    def _dim_of(hier: str) -> str:
        m = re.match(r'\[([^\]]+)\]', hier)
        return m.group(1).strip() if m else ""

    if len(col_hierarchies) > 1:
        axis_tuples = _build_cross_product_tuples(col_hierarchies, col_members)
        if not axis_tuples:
            return None
        # Flat dim order as it actually appears in the emitted tuples.
        flat_dims = [
            _dim_of(mem.get("hierarchy", ""))
            for mem in axis_tuples[0]
            if "[Measures]" not in mem.get("hierarchy", "")
        ]
        default_measure = active_measures[0] if active_measures else ""
        entries: list[tuple[tuple[str, ...], str]] = []
        for t in axis_tuples:
            combo: list[str] = []
            mname = default_measure
            for mem in t:
                if "[Measures]" in mem.get("hierarchy", ""):
                    mname = mem.get("caption", "")
                else:
                    combo.append(str(mem.get("caption", "")))
            entries.append((tuple(combo), mname))
        return axis_tuples, flat_dims, entries

    # Single flat hierarchy: the axis renders col_members directly
    # (Members format), one position per member; the measure comes from
    # the slicer.
    if not col_members:
        return None
    flat_dims = [_dim_of(flat_hiers[0])]
    default_measure = active_measures[0] if active_measures else ""
    entries = [
        ((str(mem.get("caption", "")),), default_measure)
        for mem in col_members
    ]
    return None, flat_dims, entries


def _build_mirror_subtotal_cell_data(
    rows: list[dict[str, Any]],
    per_row_col_members: list[dict[str, str]],
    row_flat_dims: list[str],
    measure_cols: list[str],
    slicer_measure: str | None,
    measures_on_cols: bool,
    measures_meta: list[dict[str, Any]] | None = None,
    calc_format_map: dict[str, str] | None = None,
) -> tuple[str, list[list[dict[str, str]]]]:
    """Build CellData for the MIRROR mixed shape (Bug-3618).

    The subtotal hierarchy sits on the COLUMNS axis while a flat
    (non-subtotal) dimension sits on the ROWS axis — the structural mirror
    of the Bug-1039 (subtotal-on-rows + flat-on-cols) layout. Each result
    ``row`` therefore maps to:

    - a COLUMN position from its deduplicated subtotal member
      (``per_row_col_members[i]``), optionally crossed with each measure
      when the measures hierarchy is also on the columns axis; and
    - a ROW position from its flat-row-dim member combo.

    The cell ordinal is ``row_pos * num_cols + col_pos`` — the standard
    MDDataSet convention with the columns axis fastest-varying. Returns the
    CellData XML and the deduplicated ``(col_member [, measure])`` Axis0
    tuples so the caller can render Axis0 consistently with the cells.
    """
    active_measures = (
        [slicer_measure] if slicer_measure else list(measure_cols)
    )

    format_map: dict[str, str] = {}
    if measures_meta:
        for m in measures_meta:
            # Bug-5432: translate the Tessallite format TOKEN (percent_2dp,
            # currency, …) to an SSAS/.NET FORMAT_STRING for FmtValue/FORMAT_STRING.
            fmt = format_token_to_mdx(m.get("format"))
            if fmt:
                format_map[m.get("name", "")] = fmt
    if calc_format_map:
        format_map.update(calc_format_map)

    # Row positions: distinct flat-row-dim member combos, first-seen order.
    row_index: dict[tuple[str, ...], int] = {}
    row_pos_per_row: list[int] = []
    for row in rows:
        rk = tuple(str(row.get(d, "")) for d in row_flat_dims)
        if rk not in row_index:
            row_index[rk] = len(row_index)
        row_pos_per_row.append(row_index[rk])

    # Column positions: deduplicated subtotal members (by uname), each
    # crossed with the measures when measures live on the columns axis.
    col_uname_index: dict[str, int] = {}
    col_axis_tuples: list[list[dict[str, str]]] = []
    measure_members: list[dict[str, str]] = [
        {
            "hierarchy": "[Measures]",
            "uname": f"[Measures].[{m}]",
            "caption": m,
            "lname": "[Measures]",
            "lnum": "0",
            "parent": "",
            "has_children": False,
        }
        for m in active_measures
    ]
    for cm in per_row_col_members:
        u = cm.get("uname", "")
        if u in col_uname_index:
            continue
        # Store the base COLUMN POSITION (already accounting for the measure
        # cross-product), so the per-cell math only adds the measure offset.
        col_uname_index[u] = len(col_axis_tuples)
        if measures_on_cols and measure_members:
            for mm in measure_members:
                col_axis_tuples.append([cm, mm])
        else:
            col_axis_tuples.append([cm])

    if measures_on_cols and measure_members:
        num_cols = len(col_uname_index) * len(measure_members)
    else:
        num_cols = max(len(col_uname_index), 1)
    num_cols = num_cols or 1

    xml_parts: list[str] = ["<CellData>"]
    emitted_ordinals: set[int] = set()

    def _emit_cell(ordinal: int, val: Any, mname: str) -> None:
        if val is None:
            return
        if ordinal in emitted_ordinals:
            logger.warning(
                "Duplicate CellOrdinal %s for measure %s — duplicate mirror "
                "subtotal coordinates upstream; cell skipped (Bug-3618).",
                ordinal, mname,
            )
            return
        emitted_ordinals.add(ordinal)
        try:
            fval = float(val)
            fmt_str = format_map.get(mname, "")
            xml_parts.append(
                f'<Cell CellOrdinal="{ordinal}">'
                f'<Value xsi:type="xsd:double">{fval}</Value>'
            )
            _fmt_val = _format_cell_value(fval, fmt_str)  # Bug-5432
            if _fmt_val is not None:
                xml_parts.append(f'<FmtValue>{_escape_xml(_fmt_val)}</FmtValue>')
            if fmt_str:
                xml_parts.append(
                    f'<FormatString>{_escape_xml(fmt_str)}</FormatString>'
                )
            xml_parts.append("</Cell>")
        except (ValueError, TypeError):
            xml_parts.append(
                f'<Cell CellOrdinal="{ordinal}">'
                f'<Value xsi:type="xsd:string">{_escape_xml(str(val))}</Value>'
                f'</Cell>'
            )

    for row_idx, row in enumerate(rows):
        r_pos = row_pos_per_row[row_idx]
        base_col = col_uname_index.get(per_row_col_members[row_idx].get("uname", ""))
        if base_col is None:
            continue
        for m_idx, mname in enumerate(active_measures):
            if measures_on_cols and measure_members:
                # base_col is already the position of this member's first
                # measure column; the measure offset is added directly.
                c_pos = base_col + m_idx
            else:
                # Single slicer measure: one cell per (row, col) coordinate.
                if m_idx > 0:
                    break
                c_pos = base_col
            _emit_cell(r_pos * num_cols + c_pos, row.get(mname), mname)

    xml_parts.append("</CellData>")
    return "".join(xml_parts), col_axis_tuples


def _build_subtotal_cell_data(
    rows: list[dict[str, Any]],
    col_members: list[dict],
    measure_cols: list[str],
    slicer_measure: str | None,
    measures_meta: list[dict[str, Any]] | None = None,
    calc_format_map: dict[str, str] | None = None,
    per_row_tuples: list[list[dict[str, str]]] | None = None,
    col_positions: list[tuple[tuple[str, ...], str]] | None = None,
    col_flat_dims: list[str] | None = None,
) -> str:
    """Build CellData for subtotal-enhanced responses.

    Each row maps to exactly one row axis position. Cell data is
    row-major: ordinal = row_idx * num_col_tuples + col_idx.

    F-002-01 fix: when the subtotal axis is rendered from deduplicated
    tuples (multi-hierarchy layouts), ``per_row_tuples`` carries the
    pre-deduplication tuple per row. The axis position of each row is then
    the first-seen index of its tuple key — identical to the ordering
    `_deduplicate_axis_tuples` emits — instead of the raw row index, so
    cells can never shift against the axis.

    B8 round-2 fix (deep-review Finding 3): ``col_positions`` /
    ``col_flat_dims`` carry the column-axis position map for mixed shapes
    (flat dims on the columns axis). Each row's cell lands at the column
    position whose flat-dim member combo matches the row's own values —
    ``ordinal = row_pos * num_cols + col_pos`` — instead of a formula
    that ignored the column-dim coordinate and emitted duplicate
    CellOrdinals.

    B8 round-2 fix (deep-review Finding 5): every branch guards against
    emitting two cells at the same ordinal. Genuine duplicates indicate
    an upstream bug (grain queries are GROUP BYs over distinct combos);
    they are skipped and logged loudly instead of producing structurally
    malformed XML.
    """
    row_position: list[int] | None = None
    if per_row_tuples is not None:
        pos_by_key: dict[tuple[str, ...], int] = {}
        row_position = []
        for t in per_row_tuples:
            key = tuple(m.get("uname", "") for m in t)
            if key not in pos_by_key:
                pos_by_key[key] = len(pos_by_key)
            row_position.append(pos_by_key[key])

    col_measure_names: list[str] = []
    for m in col_members:
        if "[Measures]" in m.get("hierarchy", ""):
            col_measure_names.append(m.get("caption", ""))

    active_measures = col_measure_names or ([slicer_measure] if slicer_measure else measure_cols)

    format_map: dict[str, str] = {}
    if measures_meta:
        for m in measures_meta:
            # Bug-5432: translate the Tessallite format TOKEN (percent_2dp,
            # currency, …) to an SSAS/.NET FORMAT_STRING for FmtValue/FORMAT_STRING.
            fmt = format_token_to_mdx(m.get("format"))
            if fmt:
                format_map[m.get("name", "")] = fmt
    if calc_format_map:
        format_map.update(calc_format_map)

    has_measure_cols = any("[Measures]" in m.get("hierarchy", "") for m in col_members)

    xml_parts: list[str] = ["<CellData>"]
    emitted_ordinals: set[int] = set()

    def _emit_cell(ordinal: int, val: Any, mname: str) -> None:
        if val is None:
            return
        if ordinal in emitted_ordinals:
            logger.warning(
                "Duplicate CellOrdinal %s for measure %s — duplicate axis "
                "tuple keys upstream; cell skipped (Bug guard, B8 round 2).",
                ordinal, mname,
            )
            return
        emitted_ordinals.add(ordinal)
        try:
            fval = float(val)
            fmt_str = format_map.get(mname, "")
            xml_parts.append(
                f'<Cell CellOrdinal="{ordinal}">'
                f'<Value xsi:type="xsd:double">{fval}</Value>'
            )
            _fmt_val = _format_cell_value(fval, fmt_str)  # Bug-5432
            if _fmt_val is not None:
                xml_parts.append(f'<FmtValue>{_escape_xml(_fmt_val)}</FmtValue>')
            if fmt_str:
                xml_parts.append(
                    f'<FormatString>{_escape_xml(fmt_str)}</FormatString>'
                )
            xml_parts.append("</Cell>")
        except (ValueError, TypeError):
            xml_parts.append(
                f'<Cell CellOrdinal="{ordinal}">'
                f'<Value xsi:type="xsd:string">{_escape_xml(str(val))}</Value>'
                f'</Cell>'
            )

    if col_positions is not None:
        num_cols = max(len(col_positions), 1)
        flat_dims = col_flat_dims or []
        for row_idx, row in enumerate(rows):
            pos = row_position[row_idx] if row_position is not None else row_idx
            row_combo = tuple(str(row.get(d, "")) for d in flat_dims)
            for col_idx, (combo_key, mname) in enumerate(col_positions):
                if combo_key != row_combo:
                    continue
                _emit_cell(pos * num_cols + col_idx, row.get(mname), mname)
    elif has_measure_cols:
        for row_idx, row in enumerate(rows):
            pos = row_position[row_idx] if row_position is not None else row_idx
            for cm_idx, cm in enumerate(col_members):
                mname = cm.get("caption", "")
                if "[Measures]" not in cm.get("hierarchy", ""):
                    continue
                _emit_cell(pos * len(col_members) + cm_idx, row.get(mname), mname)
    else:
        # Bug-3616: the subtotal hierarchies are on the COLUMNS axis (or a
        # single slicer measure applies), so each result ``row`` maps to a
        # COLUMN position (``pos``) and the measures occupy the ROWS axis.
        # The ROWS axis is the slow (most-significant) axis, so the MDDataSet
        # convention is ``measure_pos * |Axis0| + col_pos`` — measures are not
        # the fastest-varying coordinate. The old ``col_pos * num_measures +
        # m_idx`` interleaved the measures within each column, transposing
        # every cell whenever 2+ measures sat on the rows axis. For the common
        # single-measure case (Bug-586) both formulas collapse to ``pos``.
        num_axis0 = (
            (max(row_position) + 1) if row_position else len(rows)
        ) or 1
        for row_idx, row in enumerate(rows):
            pos = row_position[row_idx] if row_position is not None else row_idx
            for m_idx, mname in enumerate(active_measures):
                _emit_cell(m_idx * num_axis0 + pos, row.get(mname), mname)

    xml_parts.append("</CellData>")
    return "".join(xml_parts)


def build_real_execute_response(
    mdx: str,
    catalog: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    measures_meta: list[dict[str, Any]] | None = None,
    dimensions_meta: list[dict[str, Any]] | None = None,
    axis_format: str | None = None,
    client_app_name: str | None = None,
    subtotal_hierarchy: Any | None = None,
    requery_results: dict[tuple, Any] | None = None,
    subtotal_hierarchies: list | None = None,
) -> str:
    """
    Build an MDDataSet Execute response from real query-router results.

    Takes the tabular result (columns + rows) from the query-router and wraps
    them in the MDDataSet XML format that MSOLAP/Excel expects.

    The query-router returns flat tabular data like:
      columns: ["Geography", "Product", "Amount"]
      rows: [{"Geography": "France", "Product": "Laptop", "Amount": 1234}, ...]

    This function classifies each column as a dimension or measure based on
    the model metadata, then builds the axis/cell structure Excel expects.

    When subtotal_hierarchy is provided, rows include tagged subtotal/grand-total
    entries that are rendered as multi-level axis members with proper parent-child
    relationships.
    """
    cube = catalog

    # Classify columns into dimensions and measures
    measure_names_set: set[str] = set()
    if measures_meta:
        for m in measures_meta:
            measure_names_set.add(m.get("name", ""))

    dim_names_set: set[str] = set()
    if dimensions_meta:
        for d in dimensions_meta:
            dim_names_set.add(d.get("name", ""))

    # Classify result columns (skip subtotal marker columns)
    _marker_cols = {"_subtotal_level", "_subtotal_grain"}
    dim_cols: list[str] = []
    measure_cols: list[str] = []
    for col in columns:
        if col in _marker_cols:
            continue
        if col in measure_names_set:
            measure_cols.append(col)
        elif col in dim_names_set:
            dim_cols.append(col)
        else:
            # Heuristic: if it looks numeric across all rows, treat as measure
            is_numeric = True
            for r in rows[:10]:
                val = r.get(col)
                if val is not None:
                    try:
                        float(val)
                    except (ValueError, TypeError):
                        is_numeric = False
                        break
            if is_numeric and rows:
                measure_cols.append(col)
            else:
                dim_cols.append(col)

    # If no measures detected, use all numeric columns
    if not measure_cols:
        measure_cols = [c for c in columns if c not in dim_cols]

    # F-002-10: map NULL/empty dimension members to the stable "(blank)" member
    # in place, so the fact stays on the pivot (SSAS semantics) and every
    # downstream consumer agrees on the member string. Measures are untouched.
    if dim_cols:
        for r in rows:
            for dc in dim_cols:
                v = r.get(dc)
                if v is None or (isinstance(v, str) and v == ""):
                    r[dc] = BLANK_MEMBER

    # Parse WITH MEMBER definitions and evaluate calculated members
    calc_members = []
    try:
        from src.dax.ts_mdx_parser import parse_mdx
        parsed = parse_mdx(mdx)
        if parsed.with_members:
            calc_members = parse_calc_members(parsed.with_members)
            rows = evaluate_calc_members(calc_members, rows, measure_cols, dim_cols, measures_meta, requery_results)
            for cm in calc_members:
                if cm.name not in measure_cols:
                    measure_cols.append(cm.name)
    except ValueError:
        raise
    except Exception:
        pass

    # Parse MDX to determine axis layout
    col_expr = _get_axis_expr(mdx, "COLUMNS")
    row_expr = _get_axis_expr(mdx, "ROWS")
    col_hierarchies = _extract_hierarchies(col_expr)
    row_hierarchies = _extract_hierarchies(row_expr)
    has_explicit_axes = bool(re.search(r"\bON\s+(?:COLUMNS|ROWS|0|1)\b", mdx, re.IGNORECASE))

    # Preserve genuine no-axis MDX probes from Excel. Only synthesize a default
    # layout when the query did specify axes but the parser failed to detect them.
    if not col_hierarchies and not row_hierarchies and has_explicit_axes:
        col_hierarchies = ["[Measures]"]
        row_hierarchies = [f"[{d}].[{d}]" for d in dim_cols]

    dim_props = _parse_dimension_properties(mdx)
    col_member_filters = _extract_all_member_filters(col_expr)
    row_member_filters = _extract_all_member_filters(row_expr)
    col_ascendants = _extract_currentmember_ascendants(col_expr)
    row_ascendants = _extract_currentmember_ascendants(row_expr)
    col_drilldowns = _extract_all_drilldown_members(col_expr)
    row_drilldowns = _extract_all_drilldown_members(row_expr)
    # Bug-XMLA-001 fix: the original `minimal_excel_props` flag bundled two
    # behaviours together — (1) emit `<Tuples>` axis format for Excel, and
    # (2) strip MEMBER_KEY/MEMBER_VALUE/MEMBER_NAME/PARENT_UNIQUE_NAME etc.
    # from each member. Behaviour (1) is correct (Excel wants Tuples), but
    # (2) was the bug — Excel's MSOLAP client actually requires those
    # properties and crashes with an RPC failure when they are missing.
    # We keep the Tuples format for Excel by passing axis_format="tupleformat"
    # but always emit the full property set.
    minimal_excel_props = False
    if "excel" in (client_app_name or "").strip().lower() and not (axis_format or "").strip():
        axis_format = "tupleformat"

    # Build unique dimension member lists from result data
    dim_members_map: dict[str, list[str]] = {}
    for dc in dim_cols:
        seen: set[str] = set()
        ordered: list[str] = []
        for r in rows:
            val = str(r.get(dc, ""))
            if val and val not in seen:
                seen.add(val)
                ordered.append(val)
        dim_members_map[dc] = ordered

    # Determine slicer measure (WHERE clause)
    all_axis_hiers = col_hierarchies + row_hierarchies
    has_measures_axis = any("[Measures]" in h for h in all_axis_hiers)
    if not has_measures_axis:
        slicer_measure = _parse_where_measure(mdx) or (measure_cols[0] if measure_cols else None)
    else:
        slicer_measure = None

    # Determine which dimensions are on axes vs slicer.
    #
    # Bug-1026: a result dimension column belongs on the SlicerAxis only when
    # it is genuinely pinned to a single member by the WHERE clause and is not
    # iterated on an axis. The previous substring test (`[dc] in axis_text`)
    # mis-classified every dim column whose name does not literally appear in
    # an axis hierarchy expression — hierarchy helper dims (business_date_year
    # / business_date_month for the business_date_h hierarchy) and plain level
    # dims referenced by caption — as slicer dims pinned to their first member,
    # falsely advertising a filter context Excel renders as a report filter
    # while the cells actually aggregate every member. Such columns are axis
    # dimensions (they carry the row/column members and frequently span many
    # values), never slicers. The slicer set is therefore exactly the WHERE-
    # pinned dims that are not themselves an axis dimension column.
    where_dim_members = _parse_where_dimension_members(mdx)
    queried_dims: set[str] = set()
    for h in all_axis_hiers:
        for dc in dim_cols:
            if f"[{dc}]" in h:
                queried_dims.add(dc)
    # Axis dim columns are every dimension column carrying result members.
    axis_dim_cols = set(dim_cols)
    slicer_dims = [
        dname for dname in where_dim_members
        if dname not in axis_dim_cols and dname not in queried_dims
    ]

    # Build column axis members
    col_members: list[dict[str, str]] = []
    for hier in col_hierarchies:
        if "[Measures]" in hier:
            # Extract specific measures from MDX
            specific = _extract_measure_names(col_expr)
            active_measures = specific if specific else measure_cols
            for mname in active_measures:
                col_members.append({
                    "hierarchy": "[Measures]",
                    "uname": f"[Measures].[{mname}]",
                    "caption": mname,
                    "lname": "[Measures]",
                    "lnum": "0",
                    "parent": "",
                    "has_children": False,
                })
        else:
            dim_match = re.match(r'\[([^\]]+)\]', hier)
            if not dim_match:
                continue
            dname = dim_match.group(1).strip()
            drill_member = _normalize_member_name(col_drilldowns.get(hier, ""))
            if hier in col_ascendants:
                col_members.append({
                    "hierarchy": hier,
                    "uname": f"{hier}.[All]",
                    "name": "All",
                    "key": "All",
                    "caption": f"All {dname}",
                    "lname": f"{hier}.[(All)]",
                    "lnum": "0",
                    "parent": "",
                    "has_children": bool(dim_members_map.get(dname)),
                    "member_type": 2,
                    "children_cardinality": len(dim_members_map.get(dname, [])),
                })
                continue
            if drill_member == "All":
                for mval in dim_members_map.get(dname, []):
                    col_members.append({
                        "hierarchy": hier,
                        "uname": f"{hier}.[{mval}]",
                        "caption": mval,
                        "lname": f"{hier}.[{dname}]",
                        "lnum": "1",
                        "parent": f"{hier}.[All]",
                        "has_children": False,
                        "member_type": 1,
                    })
                continue
            filter_spec = col_member_filters.get(hier)
            if filter_spec and filter_spec[1] == "members" and _normalize_member_name(filter_spec[0]) == "All":
                # Bug-XMLA-001 fix: `[Hier].[(All)].Members` in MDX means
                # "the children of the (All) level", not the (All) member
                # itself. When the SQL result has real member values, emit
                # them as children. Only fall back to the (All) placeholder
                # when there is no data to show (schema probe / empty cube).
                member_values = dim_members_map.get(dname, [])
                only_all = (
                    len(member_values) == 1
                    and _normalize_member_name(member_values[0]) == "All"
                )
                if not member_values or only_all:
                    col_members.append({
                        "hierarchy": hier,
                        "uname": f"{hier}.[All]",
                        "name": "All",
                        "key": "All",
                        "caption": f"All {dname}",
                        "lname": f"{hier}.[(All)]",
                        "lnum": "0",
                        "parent": "",
                        "has_children": bool(dim_members_map.get(dname)),
                        "member_type": 2,
                        "member_ordinal": 0,
                        "children_cardinality": len(dim_members_map.get(dname, [])),
                    })
                else:
                    for idx, mval in enumerate(member_values):
                        col_members.append({
                            "hierarchy": hier,
                            "uname": f"{hier}.[{mval}]",
                            "name": mval,
                            "key": mval,
                            "caption": mval,
                            "lname": f"{hier}.[{dname}]",
                            "lnum": "1",
                            "parent": f"{hier}.[All]",
                            "has_children": False,
                            "member_type": 1,
                            "member_ordinal": idx,
                            "children_cardinality": 0,
                        })
                continue
            for idx, mval in enumerate(dim_members_map.get(dname, [])):
                all_member = f"{hier}.[All]"
                col_members.append({
                    "hierarchy": hier,
                    "uname": f"{hier}.[{mval}]",
                    "caption": mval,
                    "lname": f"{hier}.[{dname}]",
                    "lnum": "1",
                    "parent": all_member,
                    "has_children": False,
                    "member_type": 1,
                    "member_ordinal": idx,
                })

    # Build row axis members
    row_members: list[dict[str, str]] = []
    for hier in row_hierarchies:
        if "[Measures]" in hier:
            specific = _extract_measure_names(row_expr)
            active_measures = specific if specific else measure_cols
            for mname in active_measures:
                row_members.append({
                    "hierarchy": "[Measures]",
                    "uname": f"[Measures].[{mname}]",
                    "caption": mname,
                    "lname": "[Measures]",
                    "lnum": "0",
                    "parent": "",
                    "has_children": False,
                })
        else:
            dim_match = re.match(r'\[([^\]]+)\]', hier)
            if not dim_match:
                continue
            dname = dim_match.group(1).strip()
            drill_member = _normalize_member_name(row_drilldowns.get(hier, ""))
            if hier in row_ascendants:
                row_members.append({
                    "hierarchy": hier,
                    "uname": f"{hier}.[All]",
                    "name": "All",
                    "key": "All",
                    "caption": f"All {dname}",
                    "lname": f"{hier}.[(All)]",
                    "lnum": "0",
                    "parent": "",
                    "has_children": bool(dim_members_map.get(dname)),
                    "member_type": 2,
                    "children_cardinality": len(dim_members_map.get(dname, [])),
                })
                continue
            if drill_member == "All":
                for mval in dim_members_map.get(dname, []):
                    row_members.append({
                        "hierarchy": hier,
                        "uname": f"{hier}.[{mval}]",
                        "caption": mval,
                        "lname": f"{hier}.[{dname}]",
                        "lnum": "1",
                        "parent": f"{hier}.[All]",
                        "has_children": False,
                        "member_type": 1,
                    })
                continue
            filter_spec = row_member_filters.get(hier)
            if filter_spec and filter_spec[1] == "members" and _normalize_member_name(filter_spec[0]) == "All":
                # Bug-XMLA-001 fix: see column-axis branch above. `(All).Members`
                # means children-of-All, not the All node itself.
                member_values = dim_members_map.get(dname, [])
                only_all = (
                    len(member_values) == 1
                    and _normalize_member_name(member_values[0]) == "All"
                )
                if not member_values or only_all:
                    row_members.append({
                        "hierarchy": hier,
                        "uname": f"{hier}.[All]",
                        "name": "All",
                        "key": "All",
                        "caption": f"All {dname}",
                        "lname": f"{hier}.[(All)]",
                        "lnum": "0",
                        "parent": "",
                        "has_children": bool(dim_members_map.get(dname)),
                        "member_type": 2,
                        "member_ordinal": 0,
                        "children_cardinality": len(dim_members_map.get(dname, [])),
                    })
                else:
                    for idx, mval in enumerate(member_values):
                        row_members.append({
                            "hierarchy": hier,
                            "uname": f"{hier}.[{mval}]",
                            "name": mval,
                            "key": mval,
                            "caption": mval,
                            "lname": f"{hier}.[{dname}]",
                            "lnum": "1",
                            "parent": f"{hier}.[All]",
                            "has_children": False,
                            "member_type": 1,
                            "member_ordinal": idx,
                            "children_cardinality": 0,
                        })
                continue
            for idx, mval in enumerate(dim_members_map.get(dname, [])):
                all_member = f"{hier}.[All]"
                row_members.append({
                    "hierarchy": hier,
                    "uname": f"{hier}.[{mval}]",
                    "caption": mval,
                    "lname": f"{hier}.[{dname}]",
                    "lnum": "1",
                    "parent": all_member,
                    "has_children": False,
                    "member_type": 1,
                    "member_ordinal": idx,
                })

    _use_subtotal_cell_data = False
    _cross_axis_subtotals = False
    _per_row_row_tuples: list[list[dict[str, str]]] | None = None
    _per_row_col_tuples: list[list[dict[str, str]]] | None = None
    # F-002-01 fix: track which axes the subtotal branches actually populate
    # so the default-tuple fallback below covers every other shape. The old
    # guards encoded only the single-hierarchy and cross-axis shapes; with
    # two subtotal hierarchies both on ROWS (the standard Excel layout of
    # two hierarchies stacked in the Rows area) neither the subtotal branch
    # nor the fallback assigned col_axis_tuples -> UnboundLocalError.
    _subtotal_col_tuples_set = False
    _subtotal_row_tuples_set = False
    _subtotal_members_on_rows = False
    # Bug-3618 (MIRROR) state: subtotal hierarchy on COLUMNS + flat dim on ROWS.
    _mirror_subtotal = False
    _mirror_col_members: list[dict[str, str]] = []
    _mirror_row_flat_dims: list[str] = []
    _mirror_measures_on_cols = False
    _orig_col_has_measures = any("[Measures]" in h for h in col_hierarchies)
    if subtotal_hierarchies and len(subtotal_hierarchies) > 1 and rows:
        row_sub_hiers = [h for h in subtotal_hierarchies if h.axis == 1]
        col_sub_hiers = [h for h in subtotal_hierarchies if h.axis == 0]

        all_hier_dim_cols: set[str] = set()
        for h in subtotal_hierarchies:
            for lvl in h.levels:
                all_hier_dim_cols.add(lvl.dim_name)
        dim_cols = [d for d in dim_cols if d not in all_hier_dim_cols]

        if row_sub_hiers:
            _per_row_row_tuples = _build_multi_hierarchy_row_tuples(rows, row_sub_hiers)
            row_axis_tuples = _deduplicate_axis_tuples(_per_row_row_tuples)
            row_hierarchies = [
                f"[{h.mdx_dim_name}].[{h.mdx_hier_name}]"
                for h in row_sub_hiers
            ]
            row_members = []
            _subtotal_row_tuples_set = True
            _subtotal_members_on_rows = True
        if col_sub_hiers:
            _per_row_col_tuples = _build_multi_hierarchy_row_tuples(rows, col_sub_hiers)
            col_axis_tuples = _deduplicate_axis_tuples(_per_row_col_tuples)
            col_hierarchies = [
                f"[{h.mdx_dim_name}].[{h.mdx_hier_name}]"
                for h in col_sub_hiers
            ]
            col_members = []
            _subtotal_col_tuples_set = True
        _cross_axis_subtotals = bool(row_sub_hiers and col_sub_hiers)
        _use_subtotal_cell_data = True
    elif subtotal_hierarchy is not None and rows:
        hier_dim_cols = {lvl.dim_name for lvl in subtotal_hierarchy.levels}
        dim_cols = [d for d in dim_cols if d not in hier_dim_cols]

        _st_bracket = f"[{subtotal_hierarchy.mdx_dim_name}].[{subtotal_hierarchy.mdx_hier_name}]"
        _st_members = _build_subtotal_row_members(
            rows, subtotal_hierarchy,
            subtotal_hierarchy.mdx_dim_name,
            subtotal_hierarchy.mdx_hier_name,
        )
        if getattr(subtotal_hierarchy, "axis", 1) == 0:
            col_members = _st_members
            col_hierarchies = [_st_bracket]
            # Bug-3618 (MIRROR): a flat (non-subtotal) dimension on the ROWS
            # axis turns this into a cross-axis shape (subtotal members on
            # COLUMNS x flat dim on ROWS). The default single-hierarchy path
            # renders one column member per result row (un-deduplicated) and
            # ignores the row dim when placing cells. Capture the per-row
            # column members + the flat row dims so the mirror cell-data
            # builder can deduplicate Axis0 and align cells to
            # row_pos * |Axis0| + col_pos.
            if dim_cols:
                _mirror_subtotal = True
                _mirror_col_members = _st_members
                _mirror_row_flat_dims = list(dim_cols)
                _mirror_measures_on_cols = _orig_col_has_measures
        else:
            row_members = _st_members
            row_hierarchies = [_st_bracket]
            _subtotal_members_on_rows = True
        _use_subtotal_cell_data = True

    # F-002-01 fix: every axis the subtotal branches did not populate gets the
    # default tuple treatment, regardless of which subtotal shape was taken.
    if not _subtotal_col_tuples_set:
        col_axis_tuples = _build_existing_axis_tuples(col_hierarchies, rows) if len(col_hierarchies) > 1 else None
    if not _subtotal_row_tuples_set:
        row_axis_tuples = _build_existing_axis_tuples(row_hierarchies, rows) if len(row_hierarchies) > 1 else None

    # B8 round-2 fix (deep-review Finding 3): mixed shape — subtotal members
    # on the rows axis while the columns axis carries flat (non-subtotal)
    # dims. The column position of each cell depends on the row's own
    # column-dim values, and the column axis itself must be built from the
    # flat members x measures (``_build_existing_axis_tuples`` above renders
    # it empty because it treats [Measures] as a result-row column).
    _col_positions: list[tuple[tuple[str, ...], str]] | None = None
    _col_flat_dims: list[str] | None = None
    if (
        _use_subtotal_cell_data
        and not _cross_axis_subtotals
        and _subtotal_members_on_rows
        and not _subtotal_col_tuples_set
    ):
        _flat = _build_flat_column_positions(
            col_hierarchies, col_members, measure_cols, slicer_measure,
        )
        if _flat is not None:
            _axis_tuples_override, _col_flat_dims, _col_positions = _flat
            if _axis_tuples_override is not None:
                col_axis_tuples = _axis_tuples_override

    # Bug-3618 (MIRROR): subtotal members on COLUMNS + a flat dim on ROWS.
    # Build the deduplicated Axis0 (subtotal member [x measure]) tuples and a
    # cell-data builder that places each row at row_pos * |Axis0| + col_pos.
    _mirror_cell_data: str | None = None
    if _mirror_subtotal and _mirror_col_members and _mirror_row_flat_dims:
        _mirror_cell_data, _mirror_col_tuples = _build_mirror_subtotal_cell_data(
            rows,
            _mirror_col_members,
            _mirror_row_flat_dims,
            measure_cols,
            slicer_measure,
            _mirror_measures_on_cols,
            measures_meta=measures_meta,
            calc_format_map={
                _cm.name: _cm.format_string
                for _cm in calc_members if _cm.format_string
            } or None,
        )
        col_axis_tuples = _mirror_col_tuples
        col_members = []
        if _mirror_measures_on_cols and "[Measures]" not in col_hierarchies:
            col_hierarchies = col_hierarchies + ["[Measures]"]

    # Build OlapInfo — need dims dict for slicer
    # Create a minimal dims dict for non-queried dimensions
    dims_for_slicer: dict[str, Any] = {}
    for dname in slicer_dims:
        members_list = dim_members_map.get(dname, [])
        where_member = where_dim_members.get(dname)
        selected_member = where_member or (members_list[0] if members_list else "All")
        is_all = selected_member == "All"
        level_name = "(All)" if is_all else dname
        caption = f"All {dname}" if is_all else selected_member
        dims_for_slicer[dname] = {
            "hierarchy": f"[{dname}].[{dname}]",
            "members": [{
                "name": selected_member,
                "level": level_name,
                "member_type": 2 if is_all else 1,
                "children_cardinality": len(members_list) if is_all else 0,
                "caption": caption,
            }],
        }

    olap_info = _build_olap_info(
        cube, col_hierarchies, row_hierarchies,
        slicer_dims, slicer_measure, dims_for_slicer, dim_props,
        minimal_excel_props=minimal_excel_props,
    )

    # Build Axes
    axes_xml = _build_axes(
        col_hierarchies, col_members,
        row_hierarchies, row_members,
        slicer_dims, slicer_measure,
        dims_for_slicer, {},  # empty measures dict (not needed for slicer rendering)
        dim_props,
        axis_format,
        col_axis_tuples,
        row_axis_tuples,
        minimal_excel_props=minimal_excel_props,
    )

    _calc_fmt: dict[str, str] = {}
    for _cm in calc_members:
        if _cm.format_string:
            _calc_fmt[_cm.name] = _cm.format_string

    if _mirror_cell_data is not None:
        cell_data = _mirror_cell_data
    elif _cross_axis_subtotals and _per_row_row_tuples and _per_row_col_tuples:
        cell_data = _build_cross_axis_subtotal_cell_data(
            rows, _per_row_row_tuples, _per_row_col_tuples,
            measure_cols, slicer_measure,
            measures_meta=measures_meta,
            calc_format_map=_calc_fmt or None,
        )
    elif _use_subtotal_cell_data:
        cell_data = _build_subtotal_cell_data(
            rows, col_members, measure_cols, slicer_measure,
            measures_meta=measures_meta,
            calc_format_map=_calc_fmt or None,
            # F-002-01 fix: in multi-hierarchy single-axis layouts the axis
            # is rendered from deduplicated tuples — pass the per-row tuples
            # so cell ordinals follow the deduplicated axis positions.
            per_row_tuples=_per_row_row_tuples or _per_row_col_tuples,
            # B8 round-2 fix (Finding 3): mixed-shape column position map.
            col_positions=_col_positions,
            col_flat_dims=_col_flat_dims,
        )
    else:
        # Build CellData from actual query results
        # Map each cell to the correct row in the result data
        cell_data = _build_real_cell_data(
            col_hierarchies, col_members,
            row_hierarchies, row_members,
            measure_cols, slicer_measure,
            rows, dim_cols,
            col_axis_tuples,
            row_axis_tuples,
            measures_meta=measures_meta,
            calc_format_map=_calc_fmt or None,
        )

    return (
        f'<return>'
        f'<root xmlns="{_MDDATASET_NS}"'
        f' xmlns:xsi="{_XSI_NS}"'
        f' xmlns:xsd="{_XSD_NS}">'
        f'{_EXECUTE_XSD}'
        f'{olap_info}'
        f'{axes_xml}'
        f'{cell_data}'
        f'</root>'
        f'</return>'
    )


def _build_real_cell_data(
    col_hierarchies: list[str],
    col_members: list[dict],
    row_hierarchies: list[str],
    row_members: list[dict],
    measure_cols: list[str],
    slicer_measure: str | None,
    rows: list[dict[str, Any]],
    dim_cols: list[str],
    col_axis_tuples: list[list[dict[str, str]]] | None = None,
    row_axis_tuples: list[list[dict[str, str]]] | None = None,
    measures_meta: list[dict[str, Any]] | None = None,
    calc_format_map: dict[str, str] | None = None,
) -> str:
    """
    Build CellData from real query-router results.

    Maps the MDDataSet cell ordinals to actual values from the tabular result.
    Cell ordinal = row_index * num_col_tuples + col_index.
    """
    # Build lookup index: (dim_val_tuple) -> row dict for fast lookups
    row_index: dict[tuple, dict] = {}
    for r in rows:
        key = tuple(str(r.get(dc, "")) for dc in dim_cols)
        row_index[key] = r

    format_map: dict[str, str] = {}
    if measures_meta:
        for _m in measures_meta:
            # Bug-5432: translate the format TOKEN to an SSAS/.NET FORMAT_STRING.
            _fmt = format_token_to_mdx(_m.get("format"))
            if _fmt:
                format_map[_m.get("name", "")] = _fmt
    if calc_format_map:
        format_map.update(calc_format_map)

    # Determine which measures are on columns vs slicer
    col_measure_names: list[str] = []
    col_dim_members_by_hier: dict[str, list[str]] = {}
    for m in col_members:
        if "[Measures]" in m["hierarchy"]:
            col_measure_names.append(m["caption"])
        else:
            col_dim_members_by_hier.setdefault(m["hierarchy"], []).append(m["caption"])

    row_dim_members_by_hier: dict[str, list[str]] = {}
    row_measure_names: list[str] = []
    for m in row_members:
        if "[Measures]" in m["hierarchy"]:
            row_measure_names.append(m["caption"])
        else:
            row_dim_members_by_hier.setdefault(m["hierarchy"], []).append(m["caption"])

    # Build column tuples (cartesian product if multiple hierarchies on columns)
    col_tuples: list[dict[str, str]] = []
    if len(col_hierarchies) > 1:
        source_tuples = col_axis_tuples if col_axis_tuples is not None else _build_cross_product_tuples(col_hierarchies, col_members)
        for combo in source_tuples:
            entry: dict[str, str] = {}
            for cm in combo:
                if "[Measures]" in cm["hierarchy"]:
                    entry["__measure__"] = cm["caption"]
                else:
                    dim_match = re.match(r'\[([^\]]+)\]', cm["hierarchy"])
                    if dim_match:
                        entry[dim_match.group(1)] = cm["caption"]
            col_tuples.append(entry)
    else:
        for cm in col_members:
            entry = {}
            if "[Measures]" in cm["hierarchy"]:
                entry["__measure__"] = cm["caption"]
            else:
                dim_match = re.match(r'\[([^\]]+)\]', cm["hierarchy"])
                if dim_match:
                    entry[dim_match.group(1)] = cm["caption"]
            col_tuples.append(entry)

    # Build row tuples
    row_tuples: list[dict[str, str]] = []
    if len(row_hierarchies) > 1:
        source_tuples = row_axis_tuples if row_axis_tuples is not None else _build_cross_product_tuples(row_hierarchies, row_members)
        for combo in source_tuples:
            entry = {}
            for rm in combo:
                if "[Measures]" in rm["hierarchy"]:
                    entry["__measure__"] = rm["caption"]
                else:
                    dim_match = re.match(r'\[([^\]]+)\]', rm["hierarchy"])
                    if dim_match:
                        entry[dim_match.group(1)] = rm["caption"]
            row_tuples.append(entry)
    else:
        for rm in row_members:
            entry = {}
            if "[Measures]" in rm["hierarchy"]:
                entry["__measure__"] = rm["caption"]
            else:
                dim_match = re.match(r'\[([^\]]+)\]', rm["hierarchy"])
                if dim_match:
                    entry[dim_match.group(1)] = rm["caption"]
            row_tuples.append(entry)

    if not row_tuples:
        row_tuples = [{}]
    if not col_tuples:
        col_tuples = [{}]

    # If the MDX has no measure on any axis and no slicer measure, this is a
    # member-discovery query — Excel asks for the dimension members only and
    # expects an empty CellData section. Emitting placeholder cells in this
    # case used to crash MSOLAP with "a null value was specified".
    has_any_measure = (
        bool(slicer_measure)
        or any(("[Measures]" in m["hierarchy"]) for m in col_members)
        or any(("[Measures]" in m["hierarchy"]) for m in row_members)
    )
    if not has_any_measure:
        return '<CellData/>'

    xml = '<CellData>'
    ordinal = 0
    for rt in row_tuples:
        for ct in col_tuples:
            # Determine which measure this cell is for
            measure_name = (
                ct.get("__measure__")
                or rt.get("__measure__")
                or slicer_measure
                or (measure_cols[0] if measure_cols else None)
            )

            # Build dimension filter key to look up the row
            key_parts: list[str] = []
            combined = {**rt, **ct}
            for dc in dim_cols:
                key_parts.append(combined.get(dc, ""))
            lookup_key = tuple(key_parts)

            val = None
            if lookup_key in row_index and measure_name:
                val = row_index[lookup_key].get(measure_name)
            if val is None and measure_name == "cChildren":
                val = len(rows)

            if val is not None:
                try:
                    if measure_name == "cChildren":
                        int_val = int(val)
                        xml += (
                            f'<Cell CellOrdinal="{ordinal}">'
                            f'<Value xsi:type="xsd:int">{int_val}</Value>'
                            f'</Cell>'
                        )
                        ordinal += 1
                        continue
                    float_val = float(val)
                    _fmt_str = format_map.get(measure_name, "") if measure_name else ""
                    xml += (
                        f'<Cell CellOrdinal="{ordinal}">'
                        f'<Value xsi:type="xsd:double">{float_val}</Value>'
                    )
                    _fmt_val = _format_cell_value(float_val, _fmt_str)  # Bug-5432
                    if _fmt_val is not None:
                        xml += f'<FmtValue>{_escape_xml(_fmt_val)}</FmtValue>'
                    if _fmt_str:
                        xml += f'<FormatString>{_escape_xml(_fmt_str)}</FormatString>'
                    xml += '</Cell>'
                except (ValueError, TypeError):
                    xml += (
                        f'<Cell CellOrdinal="{ordinal}">'
                        f'<Value xsi:type="xsd:string">{_xe(str(val))}</Value>'
                        f'</Cell>'
                    )
            else:
                # Preserve cell ordinals even when there is no measure/value.
                # Excel's member-picker Execute flow relies on the ordinals being
                # present, but the CellInfo schema declares Value as required, so
                # the empty cell needs an explicit xsi:nil Value element. Without
                # it MSOLAP rejects the response with "a null value was specified".
                xml += (
                    f'<Cell CellOrdinal="{ordinal}">'
                    f'<Value xsi:nil="true"/>'
                    f'</Cell>'
                )
            ordinal += 1

    xml += '</CellData>'
    return xml


def _xe(t: str) -> str:
    return (
        str(t)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
