"""
Tree-sitter MDX walker — produces a ParsedMDX IR from MDX statements.

Walks the CST produced by the Tree-sitter MDX grammar and extracts
axis expressions, WITH MEMBER definitions, WHERE clause members,
DIMENSION PROPERTIES, and subselect filters.

The IR replaces the collection of regex helpers in mdx_execute.py and
xmla_server.py with a single structured parse result.
"""
from __future__ import annotations

import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_GRAMMAR_DIR = os.path.join(os.path.dirname(__file__), "grammars", "tree-sitter-mdx")
_SO_PATH = os.path.join(os.path.dirname(__file__), "grammars", "mdx.so")

_parser_cache: dict[str, Any] = {}


def _load_parser():
    """Build and cache the Tree-sitter MDX parser."""
    if "parser" in _parser_cache:
        return _parser_cache["parser"]
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning)
        from tree_sitter import Language, Parser
        if not os.path.exists(_SO_PATH):
            Language.build_library(_SO_PATH, [_GRAMMAR_DIR])
        lang = Language(_SO_PATH, "mdx")
        parser = Parser()
        parser.set_language(lang)
        _parser_cache["parser"] = parser
        return parser


@dataclass
class WithMemberDef:
    """A WITH MEMBER definition extracted from MDX."""
    name: str
    expression: str
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class WithSetDef:
    """A WITH SET definition extracted from MDX."""
    name: str
    expression: str


@dataclass
class AxisDef:
    """A single axis definition (COLUMNS or ROWS)."""
    axis_name: str
    non_empty: bool
    raw_expr: str
    dim_properties: list[str] = field(default_factory=list)


@dataclass
class WhereMember:
    """A member reference from the WHERE clause."""
    parts: list[str]


@dataclass
class ParsedMDX:
    """Structured representation of a parsed MDX statement."""
    with_members: list[WithMemberDef] = field(default_factory=list)
    with_sets: list[WithSetDef] = field(default_factory=list)
    axes: list[AxisDef] = field(default_factory=list)
    cube_name: str = ""
    where_members: list[WhereMember] = field(default_factory=list)
    subselect: ParsedMDX | None = None
    raw_mdx: str = ""
    warnings: list[str] = field(default_factory=list)
    is_drillthrough: bool = False
    maxrows: int | None = None
    return_columns: list[list[str]] = field(default_factory=list)

    def axis_expr(self, axis_name: str) -> str:
        """Get the raw expression for a named axis (COLUMNS/ROWS or 0/1)."""
        normalized = axis_name.upper()
        alias = {"COLUMNS": "0", "0": "0", "ROWS": "1", "1": "1"}
        target = alias.get(normalized, normalized)
        for ax in self.axes:
            ax_norm = alias.get(ax.axis_name.upper(), ax.axis_name)
            if ax_norm == target:
                return ax.raw_expr
        return ""


def parse_mdx(mdx: str) -> ParsedMDX:
    """Parse an MDX statement into a ParsedMDX IR using Tree-sitter."""
    parser = _load_parser()
    tree = parser.parse(mdx.encode("utf-8"))
    root = tree.root_node
    result = ParsedMDX(raw_mdx=mdx)

    if root.has_error:
        result.warnings.append("Tree-sitter parse error in MDX")

    _walk_source_file(root, result, mdx.encode("utf-8"))
    return result


def _node_text(node, source: bytes) -> str:
    """Extract the text content of a node."""
    return source[node.start_byte:node.end_byte].decode("utf-8")


def _walk_source_file(node, result: ParsedMDX, source: bytes) -> None:
    """Walk the root source_file → mdx_statement | drillthrough_statement."""
    for child in node.children:
        if child.type == "drillthrough_statement":
            _walk_drillthrough_statement(child, result, source)
        elif child.type == "mdx_statement":
            _walk_mdx_statement(child, result, source)
        elif child.type == "with_clause":
            _walk_with_clause(child, result, source)
        elif child.type == "select_statement":
            _walk_select_statement(child, result, source)


def _walk_drillthrough_statement(node, result: ParsedMDX, source: bytes) -> None:
    """Walk drillthrough_statement → DRILLTHROUGH [maxrows] [with] select [return]."""
    result.is_drillthrough = True
    for child in node.children:
        if child.type == "maxrows_clause":
            count_field = child.child_by_field_name("count")
            if count_field:
                try:
                    result.maxrows = int(_node_text(count_field, source))
                except ValueError:
                    result.warnings.append("Invalid MAXROWS value")
        elif child.type == "with_clause":
            _walk_with_clause(child, result, source)
        elif child.type == "select_statement":
            _walk_select_statement(child, result, source)
        elif child.type == "return_clause":
            _walk_return_clause(child, result, source)


def _walk_return_clause(node, result: ParsedMDX, source: bytes) -> None:
    """Walk return_clause → RETURN return_column, ..."""
    for child in node.children:
        if child.type == "return_column":
            for gc in child.children:
                if gc.type == "dotted_ref":
                    parts = _dotted_ref_parts(gc, source)
                    if parts:
                        result.return_columns.append(parts)


def _walk_mdx_statement(node, result: ParsedMDX, source: bytes) -> None:
    """Walk mdx_statement → with_clause? select_statement."""
    for child in node.children:
        if child.type == "with_clause":
            _walk_with_clause(child, result, source)
        elif child.type == "select_statement":
            _walk_select_statement(child, result, source)


def _walk_with_clause(node, result: ParsedMDX, source: bytes) -> None:
    """Walk with_clause → WITH (with_member_def | with_set_def)+."""
    for child in node.children:
        if child.type == "with_member_def":
            _walk_with_member_def(child, result, source)
        elif child.type == "with_set_def":
            _walk_with_set_def(child, result, source)


def _walk_with_member_def(node, result: ParsedMDX, source: bytes) -> None:
    """Walk with_member_def → MEMBER name AS expression [, property]*."""
    name = ""
    expression = ""
    properties: dict[str, str] = {}

    for child in node.children:
        if child.type == "dotted_ref" and hasattr(child, "is_named") and child.is_named:
            if not name:
                name = _node_text(child, source)
        elif child.type == "calc_expression":
            expression = _node_text(child, source).strip()
        elif child.type == "member_property":
            _walk_member_property(child, properties, source)

    name_field = node.child_by_field_name("name")
    expr_field = node.child_by_field_name("expression")
    if name_field:
        name = _node_text(name_field, source)
    if expr_field:
        expression = _node_text(expr_field, source).strip()

    result.with_members.append(WithMemberDef(
        name=name,
        expression=expression,
        properties=properties,
    ))


def _walk_member_property(node, properties: dict[str, str], source: bytes) -> None:
    """Walk member_property → identifier = value."""
    key = ""
    value = ""
    for child in node.children:
        if child.type == "identifier":
            key = _node_text(child, source)
        elif child.type in ("string_literal", "number", "dotted_ref"):
            value = _node_text(child, source)
            if child.type == "string_literal" and value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
    if key:
        properties[key] = value


def _walk_with_set_def(node, result: ParsedMDX, source: bytes) -> None:
    """Walk with_set_def → SET name AS expression."""
    name = ""
    expression = ""

    name_field = node.child_by_field_name("name")
    expr_field = node.child_by_field_name("expression")
    if name_field:
        name = _node_text(name_field, source)
    if expr_field:
        expression = _node_text(expr_field, source).strip()

    result.with_sets.append(WithSetDef(name=name, expression=expression))


def _walk_select_statement(node, result: ParsedMDX, source: bytes) -> None:
    """Walk select_statement → SELECT axis_list? FROM from_clause WHERE?."""
    for child in node.children:
        if child.type == "axis_list":
            _walk_axis_list(child, result, source)
        elif child.type == "from_clause":
            _walk_from_clause(child, result, source)
        elif child.type == "where_clause":
            _walk_where_clause(child, result, source)


def _walk_axis_list(node, result: ParsedMDX, source: bytes) -> None:
    """Walk axis_list → axis_def (, axis_def)*."""
    for child in node.children:
        if child.type == "axis_def":
            _walk_axis_def(child, result, source)


def _walk_axis_def(node, result: ParsedMDX, source: bytes) -> None:
    """Walk axis_def → [NON EMPTY] axis_body [DIMENSION PROPERTIES] ON axis_id."""
    non_empty = False
    raw_expr = ""
    axis_name = ""
    dim_props: list[str] = []

    full_text = _node_text(node, source)
    if re.match(r'\s*NON\s+EMPTY\b', full_text, re.IGNORECASE):
        non_empty = True

    for child in node.children:
        if child.type == "axis_body":
            raw_expr = _node_text(child, source).strip()
        elif child.type == "axis_id":
            axis_name = _node_text(child, source).upper()
        elif child.type == "dim_properties":
            dim_props = _walk_dim_properties(child, source)

    axis_field = node.child_by_field_name("axis_name")
    body_field = node.child_by_field_name("body")
    if axis_field:
        axis_name = _node_text(axis_field, source).upper()
    if body_field:
        raw_expr = _node_text(body_field, source).strip()

    result.axes.append(AxisDef(
        axis_name=axis_name,
        non_empty=non_empty,
        raw_expr=raw_expr,
        dim_properties=dim_props,
    ))


def _walk_dim_properties(node, source: bytes) -> list[str]:
    """Walk dim_properties → DIMENSION PROPERTIES ref, ref, ..."""
    props: list[str] = []
    for child in node.children:
        if child.type in ("dotted_ref", "identifier"):
            props.append(_node_text(child, source))
    return props


def _walk_from_clause(node, result: ParsedMDX, source: bytes) -> None:
    """Walk from_clause → bracket_name | subselect."""
    for child in node.children:
        if child.type == "bracket_name":
            text = _node_text(child, source)
            if text.startswith("[") and text.endswith("]"):
                result.cube_name = text[1:-1]
            else:
                result.cube_name = text
        elif child.type == "subselect":
            sub = ParsedMDX(raw_mdx=_node_text(child, source))
            for sc in child.children:
                if sc.type == "select_statement":
                    _walk_select_statement(sc, sub, source)
            result.subselect = sub


def _walk_where_clause(node, result: ParsedMDX, source: bytes) -> None:
    """Walk where_clause → WHERE where_tuple."""
    for child in node.children:
        if child.type == "where_tuple":
            _walk_where_tuple(child, result, source)


def _walk_where_tuple(node, result: ParsedMDX, source: bytes) -> None:
    """Walk where_tuple → ( dotted_ref | set_literal, ... )."""
    for child in node.children:
        if child.type == "dotted_ref":
            parts = _dotted_ref_parts(child, source)
            result.where_members.append(WhereMember(parts=parts))
        elif child.type == "set_literal":
            for sc in child.children:
                if sc.type == "dotted_ref" or (sc.type == "axis_token" and sc.children):
                    _extract_where_set_members(sc, result, source)


def _extract_where_set_members(node, result: ParsedMDX, source: bytes) -> None:
    """Extract dotted_ref members from set literals in WHERE clause."""
    if node.type == "dotted_ref":
        parts = _dotted_ref_parts(node, source)
        result.where_members.append(WhereMember(parts=parts))
    elif node.type == "axis_token":
        for child in node.children:
            if child.type == "dotted_ref":
                parts = _dotted_ref_parts(child, source)
                result.where_members.append(WhereMember(parts=parts))
    for child in node.children:
        if child.type in ("dotted_ref", "axis_token", "set_literal"):
            _extract_where_set_members(child, result, source)


def _dotted_ref_parts(node, source: bytes) -> list[str]:
    """Extract the bracket names from a dotted_ref node."""
    parts: list[str] = []
    for child in node.children:
        if child.type == "bracket_name":
            text = _node_text(child, source)
            if text.startswith("[") and text.endswith("]"):
                parts.append(text[1:-1])
            else:
                parts.append(text)
        elif child.type == "ampersand_key":
            for gc in child.children:
                if gc.type == "bracket_name":
                    text = _node_text(gc, source)
                    key = text[1:-1] if text.startswith("[") else text
                    parts.append(f"&{key}")
        elif child.type == "identifier":
            parts.append(_node_text(child, source))
    return parts
