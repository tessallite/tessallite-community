"""
Tree-sitter DAX walker — produces the same ParsedDAX IR as dax_parser.py.

Walks the CST produced by the Tree-sitter DAX grammar and extracts
dimensions, measures, filters, time-variant hints, ordering, and limits.
"""
from __future__ import annotations

import logging
import os
import warnings
from typing import Optional

from .dax_parser import ParsedDAX

logger = logging.getLogger(__name__)

_GRAMMAR_DIR = os.path.join(os.path.dirname(__file__), "grammars", "tree-sitter-dax")
_SO_PATH = os.path.join(os.path.dirname(__file__), "grammars", "dax.so")

_TIME_INTEL_MAP = {
    "totalytd": "ytd",
    "totalqtd": "qtd",
    "totalmtd": "mtd",
    "sameperiodlastyear": "prior_year",
    "previousmonth": "prior_month",
    "previousquarter": "prior_quarter",
    "previousyear": "prior_year",
    "dateadd": "period_offset",
}

_AGG_FUNCTIONS = {
    "sum", "average", "count", "countrows",
    "min", "max", "distinctcount",
}

_OP_MAP = {
    "=": "eq", "<>": "neq",
    "<": "lt", ">": "gt",
    "<=": "lte", ">=": "gte",
}


def _load_parser():
    """Build and cache the Tree-sitter DAX parser."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning)
        from tree_sitter import Language, Parser
        if not os.path.exists(_SO_PATH):
            Language.build_library(_SO_PATH, [_GRAMMAR_DIR])
        lang = Language(_SO_PATH, "dax")
    parser = Parser()
    parser.set_language(lang)
    return parser


_parser = None


def _get_parser():
    global _parser
    if _parser is None:
        _parser = _load_parser()
    return _parser


def translate_dax_ts(dax_statement: str, model_id: str) -> ParsedDAX:
    """Parse a DAX EVALUATE statement using Tree-sitter and return a ParsedDAX."""
    parser = _get_parser()
    tree = parser.parse(bytes(dax_statement, "utf-8"))
    root = tree.root_node

    if root.type != "source_file" or root.child_count == 0:
        raise ValueError("DAX statement must start with EVALUATE")

    stmt = root.children[0]
    if stmt.type != "dax_statement":
        raise ValueError("DAX statement must start with EVALUATE")

    result = ParsedDAX(model_id=model_id, raw_dax=dax_statement)

    expr_node = _find_child(stmt, "expression")
    if expr_node is None:
        raise ValueError("No expression found after EVALUATE")

    if root.has_error:
        inner = _unwrap_expression(expr_node)
        _KNOWN_TOP_TYPES = {
            "var_block", "summarize_columns", "summarize", "calculate",
            "calculate_table", "filter_func", "row_func", "topn",
            "select_columns", "add_columns", "distinct_func", "values_func",
            "aggregate_func", "time_intel_func", "divide_func", "if_func",
            "treatas_func", "all_func", "allselected_func",
            "removefilters_func", "binary_expr",
        }
        if inner.type == "ERROR" or inner.type not in _KNOWN_TOP_TYPES:
            raw_text = dax_statement.strip()
            after_eval = raw_text[len("EVALUATE"):].strip() if raw_text.upper().startswith("EVALUATE") else raw_text
            raise ValueError(
                f"Unsupported DAX expression: {after_eval[:80]}"
            )

    _walk_expression(expr_node, result, context="top")

    if root.has_error:
        result.warnings.append(
            "Partial parse — some DAX fragments were not recognised"
        )

    if result.warnings:
        logger.info("DAX parse warnings: %s", result.warnings)

    logger.debug(
        "Parsed DAX (TS) → dims=%s measures=%s filters=%d limit=%s variants=%s",
        result.dimensions,
        result.measures,
        len(result.filters),
        result.limit,
        result.time_variant_hints or "none",
    )
    return result


def _walk_expression(node, result: ParsedDAX, context: str = "") -> None:
    """Dispatch to the appropriate handler based on node type."""
    if node is None:
        return

    inner = _unwrap_expression(node)
    ntype = inner.type

    if ntype == "var_block":
        _walk_var_block(inner, result)
    elif ntype == "summarize_columns":
        _walk_summarize_columns(inner, result)
    elif ntype == "summarize":
        _walk_summarize(inner, result)
    elif ntype == "calculate":
        _walk_calculate(inner, result)
    elif ntype == "calculate_table":
        _walk_calculate_table(inner, result)
    elif ntype == "filter_func":
        _walk_filter_func(inner, result)
    elif ntype == "row_func":
        _walk_row(inner, result)
    elif ntype == "topn":
        _walk_topn(inner, result)
    elif ntype == "select_columns":
        _walk_select_add_columns(inner, result)
    elif ntype == "add_columns":
        _walk_select_add_columns(inner, result)
    elif ntype in ("distinct_func", "values_func"):
        _walk_distinct_values(inner, result)
    elif ntype == "aggregate_func":
        _walk_aggregate(inner, result)
    elif ntype == "time_intel_func":
        _walk_time_intel(inner, result)
    elif ntype == "divide_func":
        _walk_divide(inner, result)
    elif ntype == "if_func":
        _walk_if(inner, result)
    elif ntype == "treatas_func":
        text = _node_text(inner)
        result.warnings.append(f"TREATAS ignored — unsupported: {text[:60]}")
    elif ntype in ("all_func", "allselected_func", "removefilters_func"):
        pass
    elif ntype == "binary_expr":
        _walk_binary(inner, result, context)
    elif ntype == "column_ref":
        _extract_column_or_measure(inner, result, context)
    elif ntype == "paren_expr":
        for child in inner.children:
            if child.type == "expression":
                _walk_expression(child, result, context)
    elif ntype == "string_literal":
        pass
    elif ntype == "number":
        pass
    elif ntype == "bare_identifier":
        pass
    else:
        raise ValueError(f"Unsupported DAX expression: {_node_text(inner)[:80]}")


def _unwrap_expression(node):
    """Unwrap nested expression wrappers to get to the actual node type."""
    while node.type == "expression" and node.child_count == 1:
        node = node.children[0]
    if node.type == "argument" and node.child_count == 1:
        node = node.children[0]
        return _unwrap_expression(node)
    return node


def _walk_var_block(node, result: ParsedDAX) -> None:
    """VAR x = expr [VAR y = ..] RETURN body — walk all VAR value expressions and the body to extract measures/dimensions."""
    for child in node.children:
        if child.type == "var_decl":
            val = child.child_by_field_name("value")
            if val:
                _walk_expression(val, result, context="measure_expr")
    body = node.child_by_field_name("body")
    if body:
        _walk_expression(body, result, context="top")


def _walk_summarize_columns(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    i = 0
    while i < len(args):
        arg = args[i]
        inner = _unwrap_expression(arg)

        if inner.type in ("filter_func", "all_func", "allselected_func", "removefilters_func"):
            _walk_expression(arg, result)
            i += 1
            continue

        if inner.type == "string_literal":
            name = _unquote(inner)
            if name and name not in result.measures:
                result.measures.append(name)
            if i + 1 < len(args):
                _walk_expression(args[i + 1], result, context="measure_expr")
            i += 2
            continue

        if inner.type == "column_ref":
            _add_dimension_from_column_ref(inner, result)
            i += 1
            continue

        _walk_expression(arg, result, context="measure_expr")
        i += 1


def _walk_summarize(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    i = 0
    for i_arg, arg in enumerate(args):
        if i_arg == 0:
            continue
        inner = _unwrap_expression(arg)
        if inner.type == "column_ref":
            _add_dimension_from_column_ref(inner, result)
        elif inner.type == "string_literal":
            name = _unquote(inner)
            if name and name not in result.measures:
                result.measures.append(name)


def _walk_calculate(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    if not args:
        return

    _walk_expression(args[0], result, context="measure_expr")
    measure_name = result.measures[-1] if result.measures else None

    for arg in args[1:]:
        inner = _unwrap_expression(arg)
        if inner.type in ("all_func", "allselected_func", "removefilters_func"):
            continue
        if inner.type == "filter_func":
            _walk_filter_func(inner, result)
            continue
        if inner.type == "treatas_func":
            text = _node_text(inner)
            result.warnings.append(f"TREATAS ignored — unsupported: {text[:60]}")
            continue
        if inner.type == "time_intel_func":
            if measure_name:
                func_name_node = inner.child_by_field_name("func_name")
                if func_name_node:
                    fname = _node_text(func_name_node).lower()
                    variant = _TIME_INTEL_MAP.get(fname)
                    if variant:
                        result.time_variant_hints[measure_name] = variant
            continue
        if inner.type == "binary_expr":
            _walk_filter_condition(inner, result)
            continue
        if inner.type == "calculate":
            _walk_calculate(inner, result)
            continue
        _walk_filter_condition(inner, result)


def _walk_calculate_table(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    for arg in args[1:]:
        inner = _unwrap_expression(arg)
        if inner.type == "binary_expr":
            _walk_filter_condition(inner, result)
        else:
            _walk_expression(arg, result)


def _walk_filter_func(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    if len(args) >= 2:
        inner = _unwrap_expression(args[1])
        if inner.type == "binary_expr":
            _walk_filter_condition(inner, result)


def _walk_row(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    if len(args) >= 2:
        _walk_expression(args[1], result, context="measure_expr")


def _walk_topn(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    if not args:
        return

    num_node = _unwrap_expression(args[0])
    if num_node.type == "number":
        try:
            result.limit = int(_node_text(num_node))
        except ValueError:
            pass

    if len(args) >= 2:
        _walk_expression(args[1], result)

    if len(args) >= 3:
        col_inner = _unwrap_expression(args[2])
        col_name = _extract_column_name(col_inner)
        table_hint = _extract_table_hint(col_inner)
        direction = "DESC"
        if len(args) >= 4:
            dir_inner = _unwrap_expression(args[3])
            if _node_text(dir_inner).upper() == "ASC":
                direction = "ASC"
        if col_name:
            result.order_by.append({
                "column": col_name,
                "direction": direction,
                "table": table_hint or "",
            })


def _walk_select_add_columns(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    if not args:
        return

    inner = _unwrap_expression(args[0])
    if inner.type in ("summarize_columns", "summarize", "topn"):
        _walk_expression(args[0], result)

    i = 1
    while i < len(args):
        inner = _unwrap_expression(args[i])
        if inner.type == "string_literal":
            name = _unquote(inner)
            if i + 1 < len(args):
                _walk_expression(args[i + 1], result, context="measure_expr")
                if name and name not in result.measures:
                    result.measures.append(name)
                i += 2
                continue
        i += 1


def _walk_distinct_values(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    if args:
        inner = _unwrap_expression(args[0])
        if inner.type == "column_ref":
            _add_dimension_from_column_ref(inner, result)


def _walk_aggregate(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    if args:
        inner = _unwrap_expression(args[0])
        col_name = _extract_column_name(inner)
        if col_name and col_name not in result.measures:
            result.measures.append(col_name)


def _walk_time_intel(node, result: ParsedDAX) -> None:
    func_name_node = node.child_by_field_name("func_name")
    fname = _node_text(func_name_node).lower() if func_name_node else ""
    variant = _TIME_INTEL_MAP.get(fname)

    args = _get_arguments(node)
    if args:
        _walk_expression(args[0], result, context="measure_expr")
        measure_name = result.measures[-1] if result.measures else None
        if measure_name and variant:
            result.time_variant_hints[measure_name] = variant


def _walk_divide(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    for arg in args[:2]:
        _walk_expression(arg, result, context="measure_expr")


def _walk_if(node, result: ParsedDAX) -> None:
    args = _get_arguments(node)
    for arg in args[1:]:
        _walk_expression(arg, result, context="measure_expr")


def _walk_binary(node, result: ParsedDAX, context: str) -> None:
    if context == "filter":
        _walk_filter_condition(node, result)
    else:
        for child in node.children:
            if child.type == "expression":
                _walk_expression(child, result, context="measure_expr")


def _walk_filter_condition(node, result: ParsedDAX) -> None:
    inner = _unwrap_expression(node)
    if inner.type != "binary_expr":
        return

    children = inner.children
    if len(children) < 3:
        return

    lhs = children[0]
    op_text = None
    rhs = None
    for i, child in enumerate(children):
        if child.type not in ("expression",) and _node_text(child) in _OP_MAP:
            op_text = _node_text(child)
            if i + 1 < len(children):
                rhs = children[i + 1]
            break

    if op_text is None or rhs is None:
        return

    lhs_inner = _unwrap_expression(lhs)
    col_name = _extract_column_name(lhs_inner)
    table_hint = _extract_table_hint(lhs_inner)
    rhs_inner = _unwrap_expression(rhs)
    raw_value = _node_text(rhs_inner)
    value_str = raw_value.strip('"').strip("'")
    # F-002-12: record whether the DAX literal was a STRING (quoted) so the SQL
    # emitter quotes it even when it looks numeric ("00123", "true"). The
    # tree-sitter node type carries the original literal kind; a quoted prefix
    # is the fallback when the grammar collapsed the literal node.
    value_is_string = (
        getattr(rhs_inner, "type", "") == "string_literal"
        or raw_value.strip().startswith(('"', "'"))
    )

    if col_name:
        result.filters.append({
            "column": col_name,
            "table": table_hint,
            "operator": _OP_MAP.get(op_text, op_text),
            "value": value_str,
            "value_is_string": value_is_string,
        })


def _extract_column_or_measure(node, result: ParsedDAX, context: str) -> None:
    if context in ("measure_expr", "top"):
        name = _extract_column_name(node)
        if name and name not in result.measures:
            result.measures.append(name)
    else:
        _add_dimension_from_column_ref(node, result)


def _add_dimension_from_column_ref(node, result: ParsedDAX) -> None:
    col_name = _extract_column_name(node)
    table_hint = _extract_table_hint(node)
    if col_name and col_name not in result.dimensions:
        result.dimensions.append(col_name)
        result.dimension_hierarchy_hints.append(table_hint)


def _extract_column_name(node) -> Optional[str]:
    if node.type == "column_ref":
        for child in node.children:
            if child.type == "bracket_name":
                return _node_text(child).strip("[]")
    elif node.type == "bracket_name":
        return _node_text(node).strip("[]")
    return None


def _extract_table_hint(node) -> Optional[str]:
    if node.type == "column_ref":
        for child in node.children:
            if child.type == "table_ref":
                return _extract_table_name(child)
    return None


def _extract_table_name(node) -> str:
    if node.type == "table_ref":
        for child in node.children:
            if child.type == "identifier":
                return _node_text(child)
            if child.type == "quoted_table_name":
                return _node_text(child).strip("'")
    return _node_text(node).strip("'")


def _get_arguments(node) -> list:
    args = []
    for child in node.children:
        if child.type == "argument":
            args.append(child)
    return args


def _find_child(node, child_type: str):
    for child in node.children:
        if child.type == child_type:
            return child
    return None


def _node_text(node) -> str:
    return node.text.decode("utf-8") if isinstance(node.text, bytes) else str(node.text)


def _unquote(node) -> str:
    return _node_text(node).strip('"').strip("'").strip()


def _has_valid_children(node) -> bool:
    """Check if a node has at least one non-ERROR child with semantic content."""
    for child in node.children:
        if child.type not in ("ERROR", "(", ")", ",") and not child.has_error:
            return True
    return False
