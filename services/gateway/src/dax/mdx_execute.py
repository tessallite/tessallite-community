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
from src.dax.mdx_calc_members import (
    BLANK_MEMBER,
    parse_calc_members,
    evaluate_calc_members,
)

logger = logging.getLogger(__name__)

# Bug-6659: the companion result column that carries a dimension member's CAPTION
# (from the dimension's display_column_name) alongside its key. The translated
# SQL (xmla_server._mdx_to_sql) projects the display column under this alias; the
# Execute axis builder reads it to emit UName=key, Caption=caption. Producer and
# consumer MUST use this one helper so the alias matches end to end.
_MEMBER_CAPTION_SUFFIX = "__caption"


def _member_caption_col(dim_name: str) -> str:
    """Return the companion caption-column alias for a dimension (Bug-6659)."""
    return f"{dim_name}{_MEMBER_CAPTION_SUFFIX}"


def _normalize_member_captions(
    members: list[dict[str, Any]],
    measure_caption_map: dict[str, str],
    member_caption_lookup: dict[str, dict[str, str]],
) -> None:
    """Rewrite Execute axis captions to the field-list friendly labels (F-002-05).

    - Measure members (``hierarchy == "[Measures]"``): caption <- display_name
      (Bug-6657). The UName (and parallel ``name``) keep the internal measure
      name; cell resolution MUST read that, not caption (F-002-01 / F-103-01).
    - Dimension members: caption <- the display-column value for the member's key
      (Bug-6659). The dim_col is read from the member's ``lname`` (``[hier].[dc]``)
      so only members of a dimension that declared a display column are remapped;
      the key stays the UName. Missing captions leave the existing key caption.
    """
    for m in members:
        hier = m.get("hierarchy", "")
        if hier == "[Measures]":
            # Map key is the internal name (UName / parallel ``name``). Caption
            # is rewritten for the axis label only (Bug-6657); cell lookup
            # must not use the rewritten caption (F-002-01 / F-103-01).
            internal = m.get("name") or m.get("caption")
            if internal and internal in measure_caption_map:
                m["caption"] = measure_caption_map[internal]
            continue
        if not member_caption_lookup:
            continue
        # Resolve the dim_col from lname = "[hier].[dc]".
        lname = m.get("lname", "")
        lm = re.search(r'\.\[((?:[^\]]|\]\])+)\]\s*$', lname)
        dc = lm.group(1).replace("]]", "]") if lm else ""
        lut = member_caption_lookup.get(dc)
        if not lut:
            continue
        key = m.get("key", m.get("caption"))
        if key is not None and str(key) in lut:
            m["caption"] = lut[str(key)]


def _internal_measure_name(member: dict[str, Any]) -> str:
    """SQL-column / format_map key for a Measures axis member (F-002-01).

    After Bug-6657, ``caption`` is the field-list display_name and is an axis
    label only. Cell lookup stays on the internal name stored in UName
    (``[Measures].[base_amount]``) or the parallel ``name`` field. Never use
    caption as a result-column key — that is how live pivots emitted
    ``xsi:nil`` while JDBC/SPA returned numbers (F-103-01 / Bug-9232).
    """
    uname = str(member.get("uname") or "")
    names = _extract_measure_names(uname)
    if names:
        return names[0]
    explicit = member.get("name")
    if explicit:
        return str(explicit)
    return ""


def _escape_mdx_bracket(name: str) -> str:
    """Escape ``]`` inside an MDX bracketed identifier by doubling it.

    Bug-6717: aligned with ``mdschema._escape_mdx_bracket`` and the
    excel-plugin's ``escapeMdxBracketContent`` helper.
    """
    return name.replace("]", "]]")


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
    if s.endswith("%"):
        # decimals/thousands read from the numeric core (drop the trailing '%').
        core = s[:-1]
        int_part, _, frac_part = core.partition(".")
        decimals = sum(1 for c in frac_part if c in "0#")
        thousands = "," in int_part
        return (f"{v * 100:,.{decimals}f}%" if thousands
                else f"{v * 100:.{decimals}f}%")
    # Bug-6070: currency is not always a leading '$'. Locate the numeric core
    # (the run of #/0 with optional grouping/decimal marks) and preserve ANY
    # currency literal that sits BEFORE it (e.g. ``$#,##0.00``, ``€#,##0``) OR
    # AFTER it (suffix currencies: ``#,##0.00 €``, ``#,##0.00 kr``,
    # ``#,##0 zł``). The previous code only recognised a leading $/€/£ and
    # dropped every suffix symbol; it also let suffix text pollute the decimal
    # count. Currency symbol for the named "Currency" format stays "$" (the
    # en-US .NET default) because no connection locale is available here.
    # The numeric core is a run of #/0 with optional grouping and an optional
    # fractional part, OR a leading-dot fraction (``.00``) with no integer digit.
    core_match = re.search(r"[#0][#0,]*(?:\.[#0]+)?|\.[#0]+", s)
    if not core_match:
        return None
    core = core_match.group(0)
    int_part, _, frac_part = core.partition(".")
    decimals = sum(1 for c in frac_part if c in "0#")
    thousands = "," in int_part

    def _literal_text(fragment: str) -> str:
        # .NET custom format strings wrap literal text in single/double quotes
        # or escape one character with a backslash; strip those so only the
        # visible currency/symbol text remains.
        return re.sub(r"[\\\"']", "", fragment)

    prefix = _literal_text(s[:core_match.start()])
    suffix = _literal_text(s[core_match.end():])
    body = f"{v:,.{decimals}f}" if thousands else f"{v:.{decimals}f}"
    return f"{prefix}{body}{suffix}"


# ---------------------------------------------------------------------------
# KPI member-function resolution (Bug-3657)
# ---------------------------------------------------------------------------

def _kpi_single_measure_from_expression(expression: str | None) -> str:
    """Return the single measure name a KPI value expression reduces to, else "".

    Bug-6702: v2 expression KPIs carry no ``value_measure_id`` — their value is
    defined by ``expression``. When the WHOLE expression is exactly one bare
    measure reference (``measure("X")`` / ``measure('X')``, optionally wrapped in
    surrounding whitespace or parentheses) the KPI value IS that measure, and the
    executable XMLA member is ``[Measures].[X]``. Any additional operator,
    literal, function wrapper (e.g. ``safe_div(...)``), or second measure makes
    the expression COMPOSITE — it has no single executable measure member on the
    XMLA surface, so this returns "" and the caller advertises no value member
    (rather than a member Execute cannot resolve). The live acme-demo ``Net
    Revenue`` KPI is ``measure("net_amount")`` — the single-measure case.
    """
    text = (expression or "").strip()
    if not text:
        return ""
    # Peel a single layer of wrapping parentheses at a time, e.g. `(measure("X"))`.
    while text.startswith("(") and text.endswith(")"):
        inner = text[1:-1].strip()
        if not inner:
            break
        text = inner
    m = re.fullmatch(r'measure\(\s*"([^"]+)"\s*\)', text, re.IGNORECASE)
    if m is None:
        m = re.fullmatch(r"measure\(\s*'([^']+)'\s*\)", text, re.IGNORECASE)
    return m.group(1) if m else ""


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
    measure_names = {m.get("name", "") for m in measures_meta if m.get("name")}

    value_m = measure_map.get(str(kpi.get("value_measure_id", "")), {})
    value_name = value_m.get("name", "") if value_m else ""
    # Bug-6702: fall back to the KPI value EXPRESSION when there is no explicit
    # value measure. A single-measure expression resolves to that measure's
    # executable member; a composite expression stays "" (KPI value has no single
    # XMLA-executable member). The resolved measure MUST exist in the executable
    # `measures_meta` set so the advertised member and the Execute path agree.
    if not value_name:
        single = _kpi_single_measure_from_expression(kpi.get("expression"))
        if single and single in measure_names:
            value_name = single
    kpi_value = f"[Measures].[{_escape_mdx_bracket(value_name)}]" if value_name else ""

    # Goal: static literal, measure target (by id), or legacy goal measure.
    # Bug-6259/Bug-5695: shared resolver — a ``measure`` target renders as
    # [Measures].[<name>]; an ``expression``/``prior_period`` DSL target is not
    # executable MDX and resolves to "" (KPIGoal then reports undefined rather
    # than advertising non-executable content).
    from src.dax.mdschema import resolve_kpi_goal_mdx
    kpi_goal = resolve_kpi_goal_mdx(kpi, measure_map)

    if prop == "KPIValue":
        if not kpi_value:
            raise ValueError(
                f"KPI '{kpi.get('name', '')}' has no resolvable value measure."
            )
        return kpi_value

    if prop == "KPIGoal":
        return kpi_goal or None

    if prop == "KPIStatus":
        # Bug-6608 (un-gated 2026-07-21): the LIVE KPIStatus value is the governed
        # −1/0/1 RAG verdict served by the model-service authority through the
        # KPIStatus member-function interception in xmla_server (identical to the
        # SPA scorecard and the Excel custom function). This resolver only supplies
        # the ADDRESSABLE metadata member for MDSCHEMA_KPIS: an authored status
        # expression verbatim, else the value member so native pivot clients have a
        # member to bind. The verdict itself does not come from this string.
        legacy_status = kpi.get("status_expression") or ""
        if legacy_status:
            return legacy_status
        return kpi_value or None

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
# R2 finding 2: BLANK_MEMBER is the single source of truth in mdx_calc_members
# (imported above) so the row rewrite here and the denominator re-query planner
# there can never diverge.

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


def _axis_dim_split(
    mdx: str, dim_cols: list[str],
) -> tuple[list[str], list[str]]:
    """Split ``dim_cols`` into (row_axis_dims, col_axis_dims) by the axis each
    dimension appears on in the MDX (Bug-8206).

    A dim_col is on the COLUMN axis if its bracketed name ``[dc]`` occurs in the
    ON COLUMNS axis expression, on the ROW axis if it occurs in the ON ROWS
    expression. A dim absent from both (or ambiguous, appearing in neither) is
    left out of both lists — the axis-total evaluator then fails closed for it.
    Used only to scope % of Row/Column Total; other calc types ignore the split.
    """
    col_expr = _get_axis_expr(mdx, "COLUMNS")
    row_expr = _get_axis_expr(mdx, "ROWS")
    row_dims: list[str] = []
    col_dims: list[str] = []
    for dc in dim_cols:
        token = f"[{_escape_mdx_bracket(dc)}]"
        in_col = token in col_expr
        in_row = token in row_expr
        # Assign to exactly one axis; a name on both (unusual) is left unassigned
        # so it is neither summed nor pinned ambiguously.
        if in_col and not in_row:
            col_dims.append(dc)
        elif in_row and not in_col:
            row_dims.append(dc)
    return row_dims, col_dims


def _get_axis_expr(mdx: str, axis_name: str) -> str:
    """Extract the raw expression for a specific axis from a multi-axis SELECT.
    Handles: SELECT <expr0> ON COLUMNS, <expr1> ON ROWS FROM ...
    Strips NON EMPTY prefix and Hierarchize/AddCalculatedMembers wrappers.

    Bug-8770: the WITH prelude is stripped first (see
    ``xmla_server._mdx_statement_body``) so a comma, SELECT token, or FROM
    token inside a calc-member expression or bracketed caption cannot anchor
    the axis extraction regex. Mirrors the identical fix Bug-8750 applied to
    ``xmla_server._mdx_axis_expr``.
    """
    # Bug-8770: normalise to the statement body (past the WITH prelude).
    # Lazy import to break the circular xmla_server <-> mdx_execute dependency.
    from src.dax.xmla_server import _mdx_statement_body
    mdx = _mdx_statement_body(mdx)

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


# A single measure reference: `[Measures].[m]` or `[Measures].m`.
# Bug-6717: accept `]]` inside bracket bodies (MDX escaping of `]`).
_MEASURE_REF = r'\[Measures\]\.(?:\[(?:[^\]]|\]\])+\]|[A-Za-z_][A-Za-z0-9_]*)'

# Bug-6746: escape-aware bracket-body fragment for dimension/hierarchy/level
# names. MDX escapes a literal ``]`` inside a bracket body by doubling it
# (``]]``), so ``[^\]]+`` truncates any name that contains ``]``. ``_BB`` is a
# CAPTURING group that accepts ``]]`` runs; ``_BBN`` is its non-capturing form
# for use inside larger patterns where the body is not extracted. Re-emission
# sites keep the captured (still-escaped) body inside ``[...]`` so the rebuilt
# key stays consistent with emit-side keys; sites that compare a body against a
# RAW model name unescape with :func:`_unbracket` first.
_BB = r'((?:[^\]]|\]\])+)'
_BBN = r'(?:[^\]]|\]\])+'


def _unbracket(body: str) -> str:
    """Unescape an MDX bracket body: ``]]`` -> ``]`` (Bug-6746)."""
    return body.replace("]]", "]")


_CMP_OP = r'(?:>=|<=|<>|>|<|=)'

# (1) Comparison-operand measures — a condition inside a Filter() predicate.
# A measure is a predicate operand when a comparison operator sits on EITHER
# side of it: `[Measures].[m] > 5`, `5 < [Measures].[m]`, or both sides in a
# measure-vs-measure compare `[Measures].[a] > [Measures].[b]`. The operator is
# captured (group `op`) and re-emitted by the replacement so the *other* operand
# can also strip on the same pass; the leading-measure form uses a lookahead so
# the operator is never consumed there either.
#
# Bug-5495: a paren-wrapped or function-wrapped operand — `([Measures].[m]) > 5`
# or `CoalesceEmpty([Measures].[m], 0) > 5` — also places the measure in a
# predicate context, not on the axis. The regex now also matches:
#   - `( [Measures].[m] )` adjacent to a comparison operator (paren-wrapped),
#   - a measure reference that appears inside a function call (word + `(`)
#     followed by `)` and then a comparison operator.
# A genuine wrapped axis measure is always set-wrapped (`{[Measures].[m]}`)
# and never adjacent to a comparison operator, so it is never stripped.

# Paren-wrapped measure: `( [Measures].[m] )` — the outer parens plus the
# measure reference.  Must match when a comparison sits on either side.
# Also handles double-paren wrapping `(( [Measures].[m] ))` (Bug-5495).
_PAREN_MEASURE = rf'\(+\s*{_MEASURE_REF}\s*\)+'

_PREDICATE_MEASURE_RE = re.compile(
    # bare leading measure before a comparison operator
    rf'(?P<lead>{_MEASURE_REF})(?=\s*{_CMP_OP})'
    rf'|'
    # bare trailing measure after a comparison operator
    rf'(?P<op>{_CMP_OP})\s*{_MEASURE_REF}'
    rf'|'
    # paren-wrapped leading measure before a comparison operator (Bug-5495)
    rf'(?P<pwlead>{_PAREN_MEASURE})(?=\s*{_CMP_OP})'
    rf'|'
    # paren-wrapped trailing measure after a comparison operator (Bug-5495)
    rf'(?P<pwop>{_CMP_OP})\s*{_PAREN_MEASURE}',
    re.IGNORECASE,
)


def _blank_predicate_measure(match: "re.Match") -> str:
    """Replace a matched comparison-operand measure with a space, preserving any
    comparison operator captured on the trailing-measure branch so the other
    operand of a measure-vs-measure compare still strips on the same pass.

    Bug-5495: also handles paren-wrapped measure operands via the ``pwlead``
    and ``pwop`` groups added to ``_PREDICATE_MEASURE_RE``."""
    # Bare trailing: `op [Measures].[m]`
    op = match.group("op")
    if op:
        return f"{op} "
    # Paren-wrapped trailing: `op ([Measures].[m])`
    pwop = match.group("pwop")
    if pwop:
        return f"{pwop} "
    # Paren-wrapped leading: `([Measures].[m]) <op>`
    if match.group("pwlead"):
        return " "
    # Bare leading: `[Measures].[m] <op>`
    return " "

# (2) Sort-key / rank measures — the scalar measure argument of an ordering or
# ranking function: `Order(set, [Measures].[m], BDESC)`,
# `TopCount(set, 10, [Measures].[m])`, `BottomCount(...)`, `TopPercent(...)`.
# Unlike CrossJoin (whose bare measure argument is a genuine axis member), the
# measure here is a numeric sort key. The argument span of these calls is found
# by balanced-paren scan (depth-independent), then any bare scalar measure
# argument that is NOT wrapped in a `{}` set is blanked.
_RANK_FUNCS = frozenset({
    "order", "topcount", "bottomcount", "toppercent",
    "bottompercent", "topsum", "bottomsum",
})
_RANK_CALL_RE = re.compile(
    rf'\b({"|".join(_RANK_FUNCS)})\s*\(', re.IGNORECASE,
)
# A measure reference anchored for replacement inside an argument scan.
_ANCHORED_MEASURE_RE = re.compile(rf'^\s*{_MEASURE_REF}\s*$')

# Set-valued functions whose result is an axis SET, never a scalar sort key. A
# rank-call argument headed by one of these is the ranked SET (or a nested set),
# so its measures are axis members and must be preserved. Used to distinguish a
# scalar function-wrapped sort key (Bug-5495 — strip) from a set argument (keep).
_SET_FUNCS = frozenset({
    "crossjoin", "filter", "union", "intersect", "except", "descendants",
    "children", "members", "addcalculatedmembers", "topcount", "bottomcount",
    "toppercent", "bottompercent", "topsum", "bottomsum", "order", "hierarchize",
    "distinct", "generate", "extract", "subset", "head", "tail", "drilldownlevel",
    "drilldownmember", "drilluplevel", "drillupmember", "namedset", "set",
})
# A scalar function-wrapped sort key: `Name( ... [Measures]... )` whose head NAME
# is NOT a set function and whose body has no set-brace `{` and no `.Members` set
# expansion — i.e. a numeric expression over measures (`CoalesceEmpty([m],0)`,
# `Abs([m])`, `IIF(cond,[m],0)`), used as the rank sort key (Bug-5495).
_SCALAR_FUNC_HEAD_RE = re.compile(r'^\s*([A-Za-z_]\w*)\s*\(', re.IGNORECASE)


def _is_scalar_func_sort_key(seg: str) -> bool:
    """True when *seg* is a scalar FUNCTION call wrapping measure(s) that is a
    rank sort key (Bug-5495), not a set argument. Conservative: requires a
    non-set head function, at least one measure, and no set-brace / `.Members`."""
    s = seg.strip()
    head = _SCALAR_FUNC_HEAD_RE.match(s)
    if not head:
        return False
    if head.group(1).lower() in _SET_FUNCS:
        return False
    if "[Measures]" not in s or "{" in s:
        return False
    if re.search(r'\.\s*Members\b', s, re.IGNORECASE):
        return False
    return True


def _blank_top_level_measure_args(arg_list: str) -> str:
    """Within a rank call's argument list (the text BETWEEN the outer parens),
    blank any argument that is a sort-key measure expression. An argument is a
    sort key when it is *exactly* a bare measure reference, OR (Bug-5495) a scalar
    FUNCTION call wrapping measures that is not a set argument
    (`CoalesceEmpty([Measures].[m], 0)`). Arguments are split on top-level commas —
    commas at paren-depth 0, brace-depth 0 AND bracket-depth 0 — so a comma inside
    a `[Member, Name]` identifier, a `{}` set, or a nested call is never treated as
    an argument separator. A `{}`-set argument or a set-function argument (the
    ranked SET) is preserved so genuine axis measures survive."""
    out: list[str] = []
    depth_paren = 0
    depth_brace = 0
    depth_bracket = 0
    start = 0
    i = 0
    n = len(arg_list)

    def emit(seg: str) -> str:
        if _ANCHORED_MEASURE_RE.match(seg) or _is_scalar_func_sort_key(seg):
            return " "
        return seg

    while i < n:
        c = arg_list[i]
        if c == '[':
            depth_bracket += 1
        elif c == ']':
            if depth_bracket > 0:
                depth_bracket -= 1
        elif depth_bracket == 0:
            # Paren/brace/comma structure is only significant outside a
            # bracketed identifier (member names may contain (){}, commas).
            if c == '(':
                depth_paren += 1
            elif c == ')':
                depth_paren -= 1
            elif c == '{':
                depth_brace += 1
            elif c == '}':
                depth_brace -= 1
            elif c == ',' and depth_paren == 0 and depth_brace == 0:
                out.append(emit(arg_list[start:i]))
                out.append(',')
                start = i + 1
        i += 1
    out.append(emit(arg_list[start:n]))
    return "".join(out)


def _strip_rank_key_measures(expr: str) -> str:
    """Blank bare scalar measure arguments of ranking/order calls, at any paren
    nesting depth. Walks each rank call's balanced-paren argument span and
    removes only measures that stand alone as a top-level argument (a sort key),
    leaving set-wrapped axis measures (`{[Measures].[m]}`) and measures inside
    nested set expressions untouched."""
    out = expr
    pos = 0
    while True:
        m = _RANK_CALL_RE.search(out, pos)
        if not m:
            break
        open_idx = m.end() - 1  # index of the '('
        depth = 0
        bracket = 0
        close_idx = -1
        for i in range(open_idx, len(out)):
            c = out[i]
            if c == '[':
                bracket += 1
            elif c == ']':
                if bracket > 0:
                    bracket -= 1
            elif bracket == 0:
                # Parens inside a bracketed member name (e.g. `[Sales (Net)]`)
                # are literal text, not call structure.
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        close_idx = i
                        break
        if close_idx == -1:
            break  # unbalanced — leave the rest untouched
        inner = out[open_idx + 1:close_idx]
        new_inner = _blank_top_level_measure_args(inner)
        out = out[:open_idx + 1] + new_inner + out[close_idx:]
        # Advance past this call's name; nested rank calls inside the (rewritten)
        # span are still reachable on the next iteration.
        pos = m.end()
    return out


# (3) Function-wrapped comparison operand (Bug-5495): a function call like
# `CoalesceEmpty([Measures].[m], 0)` or `IIF(cond, [Measures].[m], 0)` that is
# itself a comparison operand — `CoalesceEmpty([Measures].[m], 0) > 5`. The
# function name precedes an opening paren; we scan to the balanced close, then
# check whether a comparison operator follows. If so, any [Measures] ref inside
# the function call is a predicate operand and is blanked.
_FUNC_CALL_CMP_RE = re.compile(
    r'\b[A-Za-z_]\w*\s*\(',
    re.IGNORECASE,
)
_BARE_MEASURE_RE = re.compile(
    rf'{_MEASURE_REF}',
    re.IGNORECASE,
)


_TRAILING_CMP_RE = re.compile(rf'{_CMP_OP}\s*$')


def _strip_func_wrapped_comparison_measures(expr: str) -> str:
    """Blank measure references inside function calls that are comparison operands.

    Scans for `FuncName(...)` patterns and blanks any `[Measures].[x]` reference
    inside the call when a comparison operator sits immediately AFTER the closing
    paren (`FuncName(...) > value`) OR immediately BEFORE the function name
    (`value < FuncName(...)`) -- in either orientation the call is a predicate
    operand, so the measures inside it are conditions, not axis members.

    Does NOT touch function calls that are not adjacent to a comparison operator --
    those may be genuine axis expressions like `AddCalculatedMembers({[Measures].[m]})`
    or a set-function whose result is never compared (`CrossJoin(...)`, `Filter(...)`).
    A set-wrapped axis measure `{[Measures].[m]}` is never inside a comparison-operand
    call, so it survives.

    The scan advances past the function NAME (not the entire call) on non-match
    so that nested function calls (e.g. `CoalesceEmpty(...)` inside `Filter(...)`)
    are still reached.
    """
    out = expr
    pos = 0
    while pos < len(out):
        m = _FUNC_CALL_CMP_RE.search(out, pos)
        if not m:
            break
        name_start = m.start()
        open_idx = m.end() - 1  # the '('
        # balanced-paren scan (bracket-aware)
        depth = 0
        bracket = 0
        close_idx = -1
        for i in range(open_idx, len(out)):
            c = out[i]
            if c == '[':
                bracket += 1
            elif c == ']':
                if bracket > 0:
                    bracket -= 1
            elif bracket == 0:
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        close_idx = i
                        break
        if close_idx == -1:
            break  # unbalanced
        # The call is a comparison operand iff a comparison operator follows its
        # closing paren (skipping wrapper parens) OR precedes its function NAME
        # (skipping wrapper parens).  Bug-5495: `(CoalesceEmpty(...)) > 5`
        # wraps the function call in extra parens before the comparison operator.
        after = out[close_idx + 1:].lstrip()
        # Skip trailing wrapper parens: `) > 5` -> skip `)` to find `> 5`
        after = after.lstrip(")")
        after = after.lstrip()
        followed_by_cmp = bool(re.match(_CMP_OP, after))
        # Skip leading wrapper parens before the function name
        before_text = out[:name_start].rstrip()
        preceded_by_cmp = bool(_TRAILING_CMP_RE.search(before_text.rstrip("(").rstrip()))
        if not (followed_by_cmp or preceded_by_cmp):
            # Advance past the function name only (not the entire call) so
            # nested function calls inside the argument list are still found.
            pos = m.end()
            continue
        # The function call is a comparison operand -- blank any measure ref
        # inside it. Only blank inside the function call span, not outside.
        inner = out[open_idx + 1:close_idx]
        if "[Measures]" in inner:
            new_inner = _BARE_MEASURE_RE.sub(" ", inner)
            delta = len(new_inner) - len(inner)
            out = out[:open_idx + 1] + new_inner + out[close_idx:]
            pos = close_idx + 1 + delta
        else:
            pos = close_idx + 1
    return out


def _strip_predicate_measures(axis_expr: str) -> str:
    """Blank out measure references that are *operands* of a set-function
    predicate or ranking key rather than axis members (Bug-5492).

    `Filter([Dim].Members, [Measures].[m] > N)`, `Order(set, [Measures].[m], …)`
    and `TopCount(set, n, [Measures].[m])` use the measure as a *condition* or
    *sort key*, not as an axis member. Counting it as a row/column hierarchy
    pollutes the axis layout (the measure leaks onto ROWS, flips
    has_measures_axis, and the member tuples get dropped).

    A measure placed directly on an axis is wrapped in a set (`{[Measures].[m]}`)
    or stands alone — it is never adjacent to a comparison operator, and never a
    bare comma-delimited scalar argument of a ranking call — so removing only
    comparison operands and bare scalar rank-key arguments leaves genuine axis
    measures untouched.

    Bug-5495: also handles paren-wrapped and function-wrapped comparison operands
    like `([Measures].[m]) > 5` and `CoalesceEmpty([Measures].[m], 0) > 5`.
    """
    if not axis_expr or "[Measures]" not in axis_expr:
        return axis_expr
    stripped = _PREDICATE_MEASURE_RE.sub(_blank_predicate_measure, axis_expr)
    stripped = _strip_rank_key_measures(stripped)
    stripped = _strip_func_wrapped_comparison_measures(stripped)
    return stripped


def _extract_hierarchies(axis_expr: str) -> list[str]:
    """
    Extract unique hierarchy names from an axis expression.

    Key distinction:
    - [Measures].[Amount] is a MEMBER reference → hierarchy = [Measures]
    - [Geography].[Geography] is a HIERARCHY name → hierarchy = [Geography].[Geography]
    - [Geography].[Geography].[Continent].Members is a LEVEL reference → hierarchy = [Geography].[Geography]

    This prevents treating each measure member or level name as a separate hierarchy,
    which would produce wrong cross-product tuples.

    Bug-5492: a measure used as a comparison operand inside a set-function
    predicate (e.g. `Filter([Dim].Members, [Measures].[m] > N)`) is a condition,
    not an axis member, so it is excluded from the hierarchy collection.
    """
    if not axis_expr:
        return []

    # Drop predicate-operand measures (Filter/Order/TopCount conditions) so they
    # do not leak onto the axis hierarchy list.
    axis_expr = _strip_predicate_measures(axis_expr)

    # Inner capture pattern for bracket contents like [(All)], [Continent], etc.
    # Bug-6746: escape-aware so a name containing ``]`` (written ``]]``) is
    # captured whole rather than truncated.
    _BC = _BB

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

    for m in re.finditer(rf'\[{_BB}\]\.\[{_BB}\]', axis_expr):
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
    Returns dict: {hierarchy_unique_name: (member_name, operation)}
    Handles patterns like:
      [Dim].[Hier].[Level].Members          → level query
      [Dim].[Hier].[Member].Children        → member-children query (caption form)
      [Dim].[Hier].&[key].Children          → member-children query (key form)
      [Dim].[Hier].[Level].&[k0]&[k1].Children → composite key, path-qualified

    Bug-5519 round-2 (Codex Finding 2): the key form `[Dim].[Hier].&[1999]`
    (and the path-qualified `[Dim].[Hier].[Level].&[k0]&[k1]`) is the member
    grammar Excel/Power BI commonly emit. Both caption and key forms must feed
    the leaf resolver; the returned ``member_name`` is the deepest key for key
    forms (the named member), the caption for caption forms. ``(All)`` parens
    are normalised away so case/paren spellings reach the resolver intact.
    """
    from src.dax.member_uname import (
        KEYS_OR_CAPTION,
        deepest_member_key,
        parse_member_keys,
    )

    result: dict[str, tuple[str, str]] = {}
    bracket = _BB  # Bug-6746: escape-aware bracket body

    # Form A — caption member or level: `[Dim].[Hier].[Member].func`.
    for m in re.finditer(
        rf'\[{bracket}\]\.\[{bracket}\]\.\[{bracket}\]\s*\.(\w+)',
        axis_expr,
        re.IGNORECASE,
    ):
        dim = m.group(1).strip()
        hier_name = m.group(2).strip()
        third = m.group(3).strip()
        func = m.group(4).lower()
        hier = f"[{dim}].[{hier_name}]"
        op = "children" if func == "children" else "members"
        result[hier] = (third, op)

    # Form B — key member: `[Dim].[Hier].&[k0]&[k1].func` or path-qualified
    # `[Dim].[Hier].[Level].&[k0]&[k1].func`. The member is the deepest key.
    for m in re.finditer(
        rf'\[{bracket}\]\.\[{bracket}\](?:\.\[{bracket}\])?'
        rf'\.{KEYS_OR_CAPTION}\s*\.(\w+)',
        axis_expr,
        re.IGNORECASE,
    ):
        dim = m.group(1).strip()
        hier_name = m.group(2).strip()
        keys_part = m.group(4)
        func = m.group(5).lower()
        # Only react to genuine key forms here; pure caption forms are Form A.
        if "&" not in (keys_part or ""):
            continue
        member = deepest_member_key(keys_part)
        if member is None:
            keys = parse_member_keys(keys_part)
            if not keys:
                continue
            member = keys[-1]
        hier = f"[{dim}].[{hier_name}]"
        op = "children" if func == "children" else "members"
        # A key form is the canonical member identity; let it win over any
        # caption form captured for the same hierarchy.
        result[hier] = (member.strip(), op)

    return result


def _extract_currentmember_ascendants(axis_expr: str) -> dict[str, str]:
    """Extract Ascendants([Dim].[Hier].CurrentMember) references from an axis expression."""
    result: dict[str, str] = {}
    for m in re.finditer(
        rf'Ascendants\s*\(\s*\[{_BB}\]\.\[{_BB}\]\.currentmember\s*\)',
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
        rf'DrilldownLevel\s*\(\s*\{{\s*\[{_BB}\]\.\[{_BB}\]\.\[{_BB}\]\s*\}}\s*\)',
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
            rf'\[{_BB}\]\.\[{_BB}\](?:\.\[{_BB}\])?((?:\.?\&\[[^\]]+\])+)?',
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
    """Extract measure name from WHERE ([Measures].[MeasureName]) clause.

    Bug-6717: accepts ``]]`` inside bracket bodies and unescapes to the
    raw technical name.
    """
    match = re.search(
        r'WHERE\s*\(\s*\[Measures\]\.\[((?:[^\]]|\]\])+)\]', mdx, re.IGNORECASE,
    )
    if match:
        return match.group(1).strip().replace("]]", "]")
    return None


def _extract_measure_names(expr: str) -> list[str]:
    """Extract measure references from an axis expression.
    Supports both [Measures].[name] and [Measures].name forms.

    Bug-6717: the bracket pattern accepts ``]]`` (escaped ``]``) inside
    bracketed names and unescapes the captured content so the returned names
    are the raw technical names suitable for model-metadata lookup.
    """
    names: list[str] = []
    for pattern in (
        r'\[Measures\]\.\[((?:[^\]]|\]\])+)\]',
        r'\[Measures\]\.([A-Za-z_][A-Za-z0-9_]*)',
    ):
        for m in re.finditer(pattern, expr):
            # Bug-6717: unescape ]] -> ] for model-metadata lookup
            name = m.group(1).strip().replace("]]", "]")
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
    for m in re.finditer(rf'\[{_BB}\]\.\[{_BB}\]\.\[{_BB}\]', where_expr):
        # Bug-6746: dim is compared against raw model dimension names downstream
        # (slicer resolution), so unescape ``]]`` -> ``]``.
        dim = _unbracket(m.group(1).strip())
        member = _unbracket(m.group(3).strip().strip("()"))
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


def _hierarchy_data_levels(
    hier: str,
    dimensions_meta: list[dict[str, Any]] | None,
    hierarchy_defs: list[dict[str, Any]] | None,
) -> list[str]:
    """Ordered data-level names (below the (All) level) of a `[Dim].[Hier]`.

    Mirrors the level metadata the MDSCHEMA_LEVELS / TREE_OP path uses so leaf
    determination on the MDX `.Children` axis path agrees with the discovery
    path (Bug-5431). Resolution order:

      1. A defined multi-level hierarchy whose name matches `Hier` — its
         ordered level names.
      2. A flat dimension's auto-hierarchy (`[Dim].[Dim]`) — the single data
         level named after the dimension.

    Returns an empty list when the hierarchy is unknown (the caller then makes
    no leaf claim and preserves existing behaviour).
    """
    m = re.match(rf'\[{_BB}\]\.\[{_BB}\]', hier)
    if not m:
        return []
    # Bug-6746: compared against RAW model dim/hierarchy names below, so unescape
    # ``]]`` -> ``]`` here.
    dim_name = _unbracket(m.group(1).strip())
    hier_name = _unbracket(m.group(2).strip())

    # 1. Defined hierarchy (multi-level) — match by hierarchy name.
    for h in hierarchy_defs or []:
        if str(h.get("name", "")).strip() != hier_name:
            continue
        levels = h.get("levels") or []
        ordered = sorted(levels, key=lambda item: int(item.get("ordinal", 0)))
        names = [str(item.get("name", "")).strip() for item in ordered]
        names = [n for n in names if n]
        if names:
            return names

    # 2. Flat dimension auto-hierarchy ([Dim].[Dim]) — one data level.
    #
    # Bug-5519 round-2 (Codex Finding 1): only the auto-hierarchy whose name
    # equals the dimension name is the flat dim's single level. An UNKNOWN
    # hierarchy over a known flat dim (`[year].[not_year]`) must NOT be treated
    # as that single level — it is unresolved, so we return [] ("unknown") and
    # the caller preserves the prior whole-level behaviour.
    if hier_name != dim_name:
        return []
    for d in dimensions_meta or []:
        if str(d.get("name", "")).strip() != dim_name:
            continue
        levels = d.get("levels") or []
        if levels and isinstance(levels[0], dict):
            ordered = sorted(levels, key=lambda item: int(item.get("ordinal", 0)))
            names = [str(item.get("name", "")).strip() for item in ordered]
            names = [n for n in names if n]
            if names:
                return names
        if levels:
            names = [str(x).strip() for x in levels if str(x).strip()]
            if names:
                return names
        # Flat dimension with no explicit level metadata: its single data
        # level is the dimension itself.
        return [dim_name]

    return []


def _member_children_resolution(
    hier: str,
    member_name: str,
    dimensions_meta: list[dict[str, Any]] | None,
    hierarchy_defs: list[dict[str, Any]] | None,
) -> str:
    """Classify a `[Dim].[Hier].[Member].Children` request.

    Returns one of:
      "all"     — the member is the (All) member; its children are the members
                  of the first (top) data level.
      "leaf"    — the member sits at the deepest data level of its hierarchy;
                  it has no level below, so `.Children` is the EMPTY set.
      "unknown" — the hierarchy/level structure could not be resolved, or the
                  member sits at an intermediate level. Preserve the existing
                  default rendering rather than guess.

    Root cause of Bug-5519: a leaf member's `.Children` previously fell through
    to the whole-level enumeration. Leaf-ness is decided from the hierarchy's
    level structure (the same metadata MDSCHEMA_LEVELS / TREE_OP uses), not by
    guessing.
    """
    # Bug-5519 round-2 (Codex Finding 3): the (All) member spelling is
    # case-insensitive — Excel/Power BI emit `[All]`, `[(All)]`, and clients
    # may lower-case to `[all]` / `[(all)]`. Normalise parens then compare
    # case-insensitively so an All member is never misclassified as a leaf.
    norm = _normalize_member_name(member_name).strip().lower()
    if norm == "all":
        return "all"

    levels = _hierarchy_data_levels(hier, dimensions_meta, hierarchy_defs)
    if not levels:
        return "unknown"

    # A flat dimension has exactly one data level; any concrete member sits at
    # that single (deepest) level, so its children are empty.
    if len(levels) == 1:
        return "leaf"

    # Multi-level hierarchy: identify which level the named member belongs to.
    # The named member's caption alone does not pin its level here (the axis
    # path carries only the caption), so without per-member level data we make
    # no leaf claim for intermediate names and preserve existing behaviour.
    return "unknown"


def _normalize_cube_timestamp(value: Any) -> str:
    """Normalise a model refresh/schema timestamp to the CubeInfo string form.

    Accepts ISO-8601 (``2026-07-15T09:30:00`` / with fractional seconds / ``Z``)
    and returns ``YYYY-MM-DDTHH:MM:SS`` (xs:dateTime with the ``T`` separator).
    The XSD declares LastDataUpdate/LastSchemaUpdate as ``xs:dateTime``, so the
    ``T`` is required — MSOLAP rejects a space separator with a parse error.
    Returns "" for an empty/unparseable value so the caller can fall back.
    """
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    cleaned = s.replace(" ", "T")
    # Drop fractional seconds.
    cleaned = cleaned.split(".")[0]
    # Drop any trailing timezone marker: a ``Z`` or a numeric ``+HH:MM`` /
    # ``-HH:MM`` offset (a TIMESTAMPTZ .isoformat() with zero microseconds yields
    # e.g. ``2026-07-01T08:30:00+00:00``). Only strip the offset AFTER the time,
    # never the date's own hyphens.
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    else:
        _m = re.search(r'[+-]\d{2}:?\d{2}$', cleaned)
        if _m:
            cleaned = cleaned[:_m.start()]
    return cleaned.strip()


def _build_olap_info(
    cube: str,
    col_hierarchies: list[str],
    row_hierarchies: list[str],
    slicer_dims: list[str],
    slicer_measure: str | None,
    dims: dict[str, Any],
    dim_props: list[str],
    minimal_excel_props: bool = False,
    last_data_update: str | None = None,
) -> str:
    """Build the OlapInfo section of the MDDataSet response.
    Order matches the MDDataSet schema: CubeInfo → AxesInfo → CellInfo.

    ``last_data_update`` (F-002-10): the model's real data-refresh time
    (``trust_meta.last_refreshed_at``). Excel / Power BI read CubeInfo
    ``LastDataUpdate`` as the honest data-freshness signal, so it must reflect
    when the underlying data was actually refreshed — NOT the static system
    metadata-config clock, which made stale (aggregate-served) pivots advertise
    themselves as fresh. When no model refresh time is available it falls back to
    the metadata stamp. ``LastSchemaUpdate`` continues to use the metadata stamp:
    a true deploy/schema timestamp is not currently propagated to the gateway.
    """
    xml = '<OlapInfo>'

    # CubeInfo
    _schema_ts = _meta_modified()
    _data_ts = _normalize_cube_timestamp(last_data_update) or _schema_ts
    xml += (
        f'<CubeInfo><Cube>'
        f'<CubeName>{_xe(cube)}</CubeName>'
        f'<LastDataUpdate xmlns="http://schemas.microsoft.com/analysisservices/2003/engine">'
        f'{_xe(_data_ts)}</LastDataUpdate>'
        f'<LastSchemaUpdate xmlns="http://schemas.microsoft.com/analysisservices/2003/engine">'
        f'{_xe(_schema_ts)}</LastSchemaUpdate>'
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
            scoped = re.match(
                rf'^\[{_BB}\]\.\[{_BB}\]\.\[{_BBN}\]\.\[{_BB}\]$', token,
            )
            if scoped:
                # Bug-6746: keep the hierarchy body escaped in the rebuilt
                # unique-name (consistent with emit-side keys).
                hierarchy = f'[{scoped.group(1).strip()}].[{scoped.group(2).strip()}]'
                bare = scoped.group(3).strip()
            else:
                bare = re.sub(rf'^(?:\[{_BBN}\]\.)+', '', token)
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
) -> list[list[dict[str, str]]] | None:
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

    Bug-9246 / F-002-01 / XLC-01: an axis that includes ``[Measures]``
    cannot be reconstructed from result-row columns (``row.get("Measures")``
    is always None, so every row was marked invalid and this function
    returned ``[]``). Callers treat ``[]`` as a real empty tuple list and
    emit empty Axis0 plus ``xsi:nil`` cells. Return ``None`` instead so
    both callers fall back to the member cross-product. Dim-only axes
    keep NonEmpty row-derived tuples.
    """
    if any("[Measures]" in h for h in hierarchies):
        return None

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
            dim_match = re.match(rf'\[{_BB}\]', hier)
            if not dim_match:
                valid = False
                break
            # Bug-6746: dname keys into the raw result row, so unescape.
            dname = _unbracket(dim_match.group(1).strip())
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
            "uname": f"[Measures].[{_escape_mdx_bracket(slicer_measure)}]",
            "name": slicer_measure,
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
        # Bug-6746: dim name keys into raw result rows downstream; unescape ]].
        m = re.match(rf'\[{_BB}\]', hier)
        return _unbracket(m.group(1).strip()) if m else ""

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
                    mname = _internal_measure_name(mem)
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
            "uname": f"[Measures].[{_escape_mdx_bracket(m)}]",
            "name": m,
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
            col_measure_names.append(_internal_measure_name(m))

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
                mname = _internal_measure_name(cm)
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


def _mdx_strip_literals_and_comments(mdx: str) -> str:
    """Blank out bracketed identifiers, quoted strings, and comments.

    Used only to count real ``MEMBER`` *declarations* (a declaration keyword
    always sits outside brackets/strings/comments). Every stripped character is
    replaced by a space so word boundaries around surviving tokens are
    preserved and no false ``MEMBER`` token is created by splicing.

    Bug-6612: SSAS MDX block comments NEST — ``/* a /* b */ c */`` is one
    comment. A non-greedy ``/\\*[\\s\\S]*?\\*/`` regex closes at the FIRST
    ``*/`` and leaves the outer comment's tail (``c */``) as live text, which
    can re-introduce a false ``MEMBER`` and fault a valid query. This scanner
    tracks block-comment nesting depth so the whole nested comment is stripped.
    Brackets and string literals take precedence over comment markers, so a
    ``/*`` / ``//`` / ``--`` inside a caption or string is treated as literal
    text, not a comment (mirrors the previous regex's alternative ordering and
    the SSAS ``]]`` bracket escape).
    """
    out: list[str] = []
    i = 0
    n = len(mdx)
    while i < n:
        ch = mdx[i]
        # Bracketed identifier: [ ... ] tolerating the ]] escape.
        if ch == '[':
            out.append(' ')
            i += 1
            while i < n:
                if mdx[i] == ']':
                    if i + 1 < n and mdx[i + 1] == ']':
                        out.append('  ')
                        i += 2
                        continue
                    out.append(' ')
                    i += 1
                    break
                out.append(' ')
                i += 1
            continue
        # String literal (single or double quoted).
        if ch in ('"', "'"):
            quote = ch
            out.append(' ')
            i += 1
            while i < n:
                out.append(' ')
                closing = mdx[i] == quote
                i += 1
                if closing:
                    break
            continue
        # Line comment: // or -- to end of line.
        if (ch == '/' and i + 1 < n and mdx[i + 1] == '/') or \
           (ch == '-' and i + 1 < n and mdx[i + 1] == '-'):
            while i < n and mdx[i] != '\n':
                out.append(' ')
                i += 1
            continue
        # Block comment: /* ... */ with nesting.
        if ch == '/' and i + 1 < n and mdx[i + 1] == '*':
            depth = 1
            out.append('  ')
            i += 2
            while i < n and depth > 0:
                if mdx[i] == '/' and i + 1 < n and mdx[i + 1] == '*':
                    depth += 1
                    out.append('  ')
                    i += 2
                elif mdx[i] == '*' and i + 1 < n and mdx[i + 1] == '/':
                    depth -= 1
                    out.append('  ')
                    i += 2
                else:
                    out.append(' ')
                    i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


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
    hierarchy_defs: list[dict[str, Any]] | None = None,
    denom_requery_results: dict[tuple, Any] | None = None,
    last_data_update: str | None = None,
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
    # Bug-6657: the field list (MDSCHEMA_MEASURES) advertises the friendly
    # MEASURE_CAPTION (display_name), but the Execute measure axis previously
    # captioned members with the INTERNAL measure name (the result column). Map
    # the internal name to display_name so the pivot header shows the SAME caption
    # the user picked from the field list.
    measure_caption_map: dict[str, str] = {}
    if measures_meta:
        for m in measures_meta:
            _mn = m.get("name", "")
            measure_names_set.add(_mn)
            if _mn:
                measure_caption_map[_mn] = (m.get("display_name") or _mn)

    dim_names_set: set[str] = set()
    # Bug-6659: a flat dimension may carry a DISPLAY column (display_column_name,
    # Bug-5434) whose value is the member CAPTION, distinct from the key. The
    # translated SQL projects that caption alongside the key into a companion
    # column (``<dim>__caption``); map dim_col -> caption column so the Execute
    # axis can emit UName=key, Caption=caption instead of the raw key twice.
    dim_caption_col_map: dict[str, str] = {}
    if dimensions_meta:
        for d in dimensions_meta:
            _dn = d.get("name", "")
            dim_names_set.add(_dn)
            _disp = (d.get("display_column_name") or "").strip()
            if _dn and _disp and _disp != _dn:
                dim_caption_col_map[_dn] = _member_caption_col(_dn)

    # Bug-6659: the companion caption columns are lookups, not axis members —
    # exclude them from dim/measure classification. Build a per-dim key->caption
    # map from the rows so the axis builder can look up each member's caption.
    _caption_cols = set(dim_caption_col_map.values())
    member_caption_lookup: dict[str, dict[str, str]] = {}
    for _dn, _cc in dim_caption_col_map.items():
        _m: dict[str, str] = {}
        for r in rows:
            _k = r.get(_dn)
            _c = r.get(_cc)
            if _k is not None and _c is not None:
                _m[str(_k)] = str(_c)
        if _m:
            member_caption_lookup[_dn] = _m

    # Classify result columns (skip subtotal marker + caption companion columns).
    # Import the marker names rather than re-spelling them: a literal copy is the
    # same drift mechanism that left Bug-8379 open for a release.
    from src.dax.subtotal_engine import SUBTOTAL_GRAIN_KEY, SUBTOTAL_LEVEL_KEY
    _marker_cols = {SUBTOTAL_LEVEL_KEY, SUBTOTAL_GRAIN_KEY}
    dim_cols: list[str] = []
    measure_cols: list[str] = []
    for col in columns:
        if col in _marker_cols or col in _caption_cols:
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

    # If no measures detected, use all numeric columns. Bug-8285: the caption
    # companion columns (and subtotal markers) are lookups, not axis members or
    # measures — they were skipped in the classification loop above, so they land
    # in neither dim_cols nor measure_cols. Exclude them here too, otherwise a
    # measure-less (dimension-only) pivot over a captioned dimension would render
    # ``<dim>__caption`` as a phantom measure column.
    if not measure_cols:
        measure_cols = [
            c for c in columns
            if c not in dim_cols and c not in _caption_cols and c not in _marker_cols
        ]

    # F-002-10: map NULL/empty dimension members to the stable "(blank)" member
    # in place, so the fact stays on the pivot (SSAS semantics) and every
    # downstream consumer agrees on the member string. Measures are untouched.
    if dim_cols:
        for r in rows:
            for dc in dim_cols:
                v = r.get(dc)
                if v is None or (isinstance(v, str) and v == ""):
                    r[dc] = BLANK_MEMBER

    # Parse WITH MEMBER definitions and evaluate calculated members.
    # Bug-6066: the parse step is best-effort — if tree-sitter cannot parse the
    # statement the axis layout is still recovered by the regex helpers below,
    # so a parse failure must stay non-fatal. But a calc-member EVALUATION
    # failure must SURFACE: previously it was swallowed with a bare ``pass``,
    # which dropped every WITH MEMBER column and returned a silently-incomplete
    # result. The caller converts a ValueError into a proper SOAP fault, so a
    # failed calc member now tells the BI client the query failed instead of
    # rendering blank columns.
    calc_members = []
    parsed = None

    # Count MEMBER *declarations* up front, on a copy of the MDX with every
    # bracketed identifier, quoted string literal, and COMMENT stripped. A
    # declaration keyword always sits OUTSIDE brackets / quotes / comments
    # (``MEMBER [x] AS ...``), so stripping only removes FALSE occurrences of the
    # word "Member" — inside a caption (``[Total Member Revenue]``), a WHERE
    # member (``[Segment].[Member]``), a string literal, or a comment
    # (``// member calc``) — that would otherwise be mistaken for a declaration.
    # The bracket pattern tolerates the SSAS ``]]`` escape so an escaped caption
    # is fully stripped (mirrors ``_CM_BRACKET_BODY`` in mdx_calc_members).
    # Comments must be stripped because the Tree-sitter MDX grammar has no comment
    # rule, so ANY comment (``//`` ``--`` ``/* */``, all valid SSAS MDX) sets
    # ``has_error`` on an otherwise perfectly-parsed statement — and a comment
    # containing the word "member" would then inflate the count and FALSELY fault
    # a valid commented query (Bug-6611). String/bracket alternatives precede the
    # comment handling so a comment marker inside a literal is consumed as
    # part of the literal, not treated as a comment. This count gates BOTH the
    # parser-unavailable branch and the parse-error guard, so a WITH SET-only
    # query (zero MEMBER declarations) with a "Member"-word caption or comment is
    # never mistaken for a calc-member statement (Bug-6607 / Bug-6611).
    # Bug-6612: block comments NEST in SSAS MDX, so a depth-aware scanner strips
    # them (a non-greedy regex closed at the first ``*/`` and leaked the tail).
    _mdx_no_literals = _mdx_strip_literals_and_comments(mdx)
    _declared_members = len(re.findall(r'\bMEMBER\b', _mdx_no_literals, re.IGNORECASE))
    _declares_with_member = _declared_members > 0

    try:
        from src.dax.ts_mdx_parser import parse_mdx
        parsed = parse_mdx(mdx)
    except Exception as exc:
        # Parse failure is non-fatal ONLY when there are no calculated members
        # to lose — the axis layout is still recovered by the regex helpers
        # below. But if the statement DECLARES a WITH ... MEMBER clause, a parse
        # failure would silently drop those columns (the Bug-6066 defect), so
        # surface it as a client fault instead.
        if _declares_with_member:
            raise ValueError(
                f"Calculated member parse failed: {exc}"
            ) from exc
        parsed = None

    # Bug-6066 (root fix): parse_mdx does NOT raise on a grammar-level parse
    # error — it returns a ParsedMDX carrying a "Tree-sitter parse error"
    # warning. The ``except`` branch above only fires when the parser itself is
    # unavailable (missing grammar DLL), so a REAL unparseable ``WITH ... MEMBER``
    # statement slipped straight through: the grammar could not extract the
    # member, ``with_members`` came back EMPTY, and the response was returned with
    # every calculated column silently dropped (the exact Fable finding —
    # ``warnings=[parse error]`` + ``with_members=[]``, never a raised exception).
    #
    # When the statement DECLARES at least one WITH ... MEMBER (``_declared_members
    # > 0``) and the parse errored, fault if the recovered members are
    # UNTRUSTWORTHY, rather than returning a silently-incomplete result. Three
    # signals mark untrustworthy recovery:
    #   (a) no members recovered at all (total drop);
    #   (b) a recovered member has an empty/whitespace expression — the grammar
    #       appended a WithMemberDef but lost its ``calc_expression`` sub-node
    #       (``ts_mdx_parser._walk_with_member_def`` always appends), which
    #       classifies as ``custom`` → ``_eval_arithmetic`` → a silently blank
    #       column; and
    #   (c) fewer usable members recovered than the client DECLARED (a MEMBER was
    #       dropped entirely, e.g. one valid + one garbled member).
    # Gating on ``_declared_members > 0`` means a query with ZERO member
    # declarations (e.g. WITH SET only) can never fault here, even when the
    # grammar over-flags ``has_error`` on an unrelated axis construct and a
    # caption happens to contain the word "Member" (Bug-6607).
    #
    # This is deliberately NOT "fault on any parse-error warning": the grammar's
    # error recovery frequently reports ``has_error`` for an UNRELATED axis
    # construct (e.g. Generate/Ascendants) while still correctly extracting every
    # calc member, and those results are valid. A plain query with a parse-error
    # warning also stays non-fatal (axis recovered by the regex helpers below).
    # Residual limitation: a member recovered with a NON-empty but semantically
    # truncated/garbage expression that happens to compile cannot be detected
    # here without re-validating each expression; the total-drop, empty-expr, and
    # dropped-member modes — the observed Bug-6066 failure modes — are covered.
    if parsed is not None and _declares_with_member:
        _parse_errored = any(
            "parse error" in (w or "").lower() for w in parsed.warnings
        )
        if _parse_errored:
            _usable = [m for m in parsed.with_members if (m.expression or "").strip()]
            if (
                not parsed.with_members
                or len(_usable) < len(parsed.with_members)
                or len(_usable) < _declared_members
            ):
                raise ValueError(
                    "Calculated member parse failed: the MDX WITH ... MEMBER "
                    "clause could not be fully parsed. Refusing to return a "
                    "result with calculated columns silently dropped."
                )

    if parsed is not None and parsed.with_members:
        try:
            calc_members = parse_calc_members(parsed.with_members)
            # F-002-07 (adversarial R3 F1/F2): a map from each hierarchy / flat
            # dimension name to the result dim_col names it covers, so the
            # % of Grand Total vs % of Row/Column Total guard compares the pinned
            # (All) hierarchies against dim_cols in the correct namespace (a
            # defined hierarchy pins all its level columns at once).
            _hier_level_dims: dict[str, list[str]] = {}
            for _h in (hierarchy_defs or []):
                _hn = str(_h.get("name", "")).strip()
                if not _hn:
                    continue
                _lvls = [
                    str(_l.get("name", "")).strip()
                    for _l in (_h.get("levels") or [])
                    if str(_l.get("name", "")).strip()
                ]
                if _lvls:
                    _hier_level_dims[_hn] = _lvls
            # Bug-8206: compute the row/col axis dim split so % of Row/Column
            # Total holds the correct axis fixed. Cheap regex over the axis
            # exprs; other calc types ignore the split.
            _row_axis_dims, _col_axis_dims = _axis_dim_split(mdx, dim_cols)
            rows = evaluate_calc_members(
                calc_members, rows, measure_cols, dim_cols,
                measures_meta, requery_results,
                denom_requery_results=denom_requery_results,
                hierarchy_level_dims=_hier_level_dims or None,
                row_axis_dims=_row_axis_dims,
                col_axis_dims=_col_axis_dims,
            )
        except ValueError:
            # Already a client-facing message (e.g. circular reference).
            raise
        except Exception as exc:
            # Any other parse/eval failure is surfaced as a client fault rather
            # than 500-ing or silently dropping the WITH MEMBER columns.
            raise ValueError(
                f"Calculated member evaluation failed: {exc}"
            ) from exc
        for cm in calc_members:
            if cm.name not in measure_cols:
                measure_cols.append(cm.name)

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
                    "uname": f"[Measures].[{_escape_mdx_bracket(mname)}]",
                    "name": mname,
                    "caption": mname,
                    "lname": "[Measures]",
                    "lnum": "0",
                    "parent": "",
                    "has_children": False,
                })
        else:
            dim_match = re.match(rf'\[{_BB}\]', hier)
            if not dim_match:
                continue
            # Bug-6746: dname keys into raw member maps/rows; unescape ]].
            dname = _unbracket(dim_match.group(1).strip())
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
            if filter_spec and filter_spec[1] == "children":
                # Bug-5519: `[Dim].[Hier].[Member].Children` on a LEAF member
                # must resolve to the EMPTY set, not the whole level. Leaf-ness
                # is read from the hierarchy's level structure (same metadata as
                # MDSCHEMA_LEVELS / TREE_OP). `[All].Children` still yields the
                # top data level's members; an unknown/intermediate member keeps
                # the existing whole-level rendering.
                _children_kind = _member_children_resolution(
                    hier, filter_spec[0], dimensions_meta, hierarchy_defs,
                )
                if _children_kind == "leaf":
                    # Leaf member has no level below it -> no axis members.
                    continue
                # "all"/"unknown" -> fall through to the whole-level rendering
                # below (children of (All) = the top data level members).
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
                    "uname": f"[Measures].[{_escape_mdx_bracket(mname)}]",
                    "name": mname,
                    "caption": mname,
                    "lname": "[Measures]",
                    "lnum": "0",
                    "parent": "",
                    "has_children": False,
                })
        else:
            dim_match = re.match(rf'\[{_BB}\]', hier)
            if not dim_match:
                continue
            # Bug-6746: dname keys into raw member maps/rows; unescape ]].
            dname = _unbracket(dim_match.group(1).strip())
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
            if filter_spec and filter_spec[1] == "children":
                # Bug-5519: see column-axis branch above. A LEAF member's
                # `.Children` is the EMPTY set; `[All].Children` is the top
                # data level. Leaf-ness comes from the hierarchy level metadata.
                _children_kind = _member_children_resolution(
                    hier, filter_spec[0], dimensions_meta, hierarchy_defs,
                )
                if _children_kind == "leaf":
                    continue
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

    # F-002-05: normalise captions so the Execute axis shows the SAME friendly
    # labels the field list advertised. Measure members: caption <- display_name
    # (Bug-6657). Dimension members: caption <- the projected display-column value
    # keyed by the member's key (Bug-6659); UName keeps the key so cell coordinate
    # matching is unaffected. Applied to the plain (non-subtotal) axis members;
    # the subtotal tuple builders below carry their own captions.
    _normalize_member_captions(
        col_members, measure_caption_map, member_caption_lookup,
    )
    _normalize_member_captions(
        row_members, measure_caption_map, member_caption_lookup,
    )

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
        last_data_update=last_data_update,
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
            col_measure_names.append(_internal_measure_name(m))
        else:
            col_dim_members_by_hier.setdefault(m["hierarchy"], []).append(m["caption"])

    row_dim_members_by_hier: dict[str, list[str]] = {}
    row_measure_names: list[str] = []
    for m in row_members:
        if "[Measures]" in m["hierarchy"]:
            row_measure_names.append(_internal_measure_name(m))
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
                    entry["__measure__"] = _internal_measure_name(cm)
                else:
                    dim_match = re.match(rf'\[{_BB}\]', cm["hierarchy"])
                    if dim_match:
                        # Bug-6746: dim key matched against raw result columns.
                        entry[_unbracket(dim_match.group(1))] = cm["caption"]
            col_tuples.append(entry)
    else:
        for cm in col_members:
            entry = {}
            if "[Measures]" in cm["hierarchy"]:
                entry["__measure__"] = _internal_measure_name(cm)
            else:
                dim_match = re.match(rf'\[{_BB}\]', cm["hierarchy"])
                if dim_match:
                    # Bug-6746: dim key matched against raw result columns.
                    entry[_unbracket(dim_match.group(1))] = cm["caption"]
            col_tuples.append(entry)

    # Build row tuples
    row_tuples: list[dict[str, str]] = []
    if len(row_hierarchies) > 1:
        source_tuples = row_axis_tuples if row_axis_tuples is not None else _build_cross_product_tuples(row_hierarchies, row_members)
        for combo in source_tuples:
            entry = {}
            for rm in combo:
                if "[Measures]" in rm["hierarchy"]:
                    entry["__measure__"] = _internal_measure_name(rm)
                else:
                    dim_match = re.match(rf'\[{_BB}\]', rm["hierarchy"])
                    if dim_match:
                        # Bug-6746: dim key matched against raw result columns.
                        entry[_unbracket(dim_match.group(1))] = rm["caption"]
            row_tuples.append(entry)
    else:
        for rm in row_members:
            entry = {}
            if "[Measures]" in rm["hierarchy"]:
                entry["__measure__"] = _internal_measure_name(rm)
            else:
                dim_match = re.match(rf'\[{_BB}\]', rm["hierarchy"])
                if dim_match:
                    # Bug-6746: dim key matched against raw result columns.
                    entry[_unbracket(dim_match.group(1))] = rm["caption"]
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
