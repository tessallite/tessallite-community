"""Named list builder-definition to MDX compiler.

Converts structured builder_definition JSON into MDX set expressions.
Supports: fixedMembers, topN, filter builder types.
"""
from __future__ import annotations

import re


class CompilationError(ValueError):
    """Raised when a builder definition cannot be compiled to MDX."""


# Control characters are never valid inside an MDX identifier or literal and
# would let a member key/value break the generated expression. Reject them.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_chars(text: str, what: str) -> None:
    if _CONTROL_CHARS.search(text):
        raise CompilationError(f"{what} contains control characters")


def _escape_mdx_name(name: str) -> str:
    """Escape an MDX bracketed identifier — a literal ``]`` is doubled."""
    _reject_control_chars(name, "Identifier")
    return name.replace("]", "]]")


def _escape_member_key(key: str) -> str:
    """Escape an MDX member key used inside ``&[...]`` — ``]`` is doubled."""
    _reject_control_chars(key, "Member key")
    return key.replace("]", "]]")


def _quote_member(dim: str, hier: str, key: str) -> str:
    return (
        f"[{_escape_mdx_name(dim)}].[{_escape_mdx_name(hier)}]."
        f"&[{_escape_member_key(key)}]"
    )


def _coerce_member_key(member: object, ordinal: int) -> str:
    if isinstance(member, dict):
        if "key" not in member or member.get("key") in (None, ""):
            raise CompilationError(
                f"fixedMembers member {ordinal} requires a non-empty 'key'"
            )
        key = member["key"]
    else:
        key = member

    if isinstance(key, bool) or not isinstance(key, (str, int, float)):
        raise CompilationError(
            f"fixedMembers member {ordinal} key must be a string or number"
        )
    text = str(key)
    if not text:
        raise CompilationError(
            f"fixedMembers member {ordinal} requires a non-empty key"
        )
    return text


def _compile_fixed_members(defn: dict, _meta: dict) -> str:
    members = defn.get("members", [])
    if not members:
        raise CompilationError("fixedMembers requires at least one member")

    dim = defn.get("dimension", "")
    hier = defn.get("hierarchy", dim)
    if not dim:
        raise CompilationError("fixedMembers requires a 'dimension' field")

    member_refs = [
        _quote_member(dim, hier, _coerce_member_key(m, i + 1))
        for i, m in enumerate(members)
    ]
    return "{ " + ", ".join(member_refs) + " }"


def _compile_top_n(defn: dict, _meta: dict) -> str:
    entity = defn.get("entity", "")
    count = defn.get("count")
    measure = defn.get("measure", "")
    direction = defn.get("direction", "top")

    if not entity:
        raise CompilationError("topN requires an 'entity' (dimension.hierarchy.level)")
    if not count or not isinstance(count, (int, float)) or count <= 0:
        raise CompilationError("topN requires a positive 'count'")
    if not measure:
        raise CompilationError("topN requires a 'measure'")

    count = int(count)
    direction = direction.lower()  # normalize so "Top"/"TOP" map to "top"
    _DIRECTION_MAP = {"top": "TopCount", "bottom": "BottomCount"}
    func_name = _DIRECTION_MAP.get(direction)
    if func_name is None:
        raise CompilationError(
            f"topN direction must be 'top' or 'bottom'; got {direction!r}"
        )
    return (
        f"{func_name}([{_escape_mdx_name(entity)}].Members, {count}, "
        f"[Measures].[{_escape_mdx_name(measure)}])"
    )


def _compile_filter(defn: dict, _meta: dict) -> str:
    entity = defn.get("entity", "")
    conditions = defn.get("conditions", [])
    logic = defn.get("logic", "AND").upper()

    if not entity:
        raise CompilationError("filter requires an 'entity' (dimension.hierarchy.level)")
    if not conditions:
        raise CompilationError("filter requires at least one condition")

    _OP_MAP = {
        "=": "=", "equals": "=",
        "!=": "<>", "not_equals": "<>",
        ">": ">", "greater_than": ">",
        "<": "<", "less_than": "<",
        ">=": ">=", "greater_or_equal": ">=",
        "<=": "<=", "less_or_equal": "<=",
    }

    mdx_conditions = []
    for cond in conditions:
        field = cond.get("field", "")
        op = cond.get("operator", "=")
        value = cond.get("value")
        if not field:
            raise CompilationError("Each condition requires a 'field'")
        mdx_op = _OP_MAP.get(op)
        if mdx_op is None:
            raise CompilationError(f"Unsupported operator: {op}")
        if isinstance(value, str):
            # F-018-17: escape embedded double-quotes so a value cannot close the
            # MDX string literal and inject trailing expression text.
            _reject_control_chars(value, "Filter value")
            value_expr = '"' + value.replace('"', '""') + '"'
        elif value is None:
            raise CompilationError("Each condition requires a 'value'")
        else:
            value_expr = str(value)
        mdx_conditions.append(
            f"[Measures].[{_escape_mdx_name(field)}] {mdx_op} {value_expr}"
        )

    _LOGIC_MAP = {"AND": " AND ", "OR": " OR "}
    joiner = _LOGIC_MAP.get(logic)
    if joiner is None:
        raise CompilationError(
            f"filter logic must be 'AND' or 'OR'; got {logic!r}"
        )
    combined = joiner.join(mdx_conditions)
    return f"Filter([{_escape_mdx_name(entity)}].Members, {combined})"


_COMPILERS = {
    "fixedMembers": _compile_fixed_members,
    "fixed": _compile_fixed_members,
    "topN": _compile_top_n,
    "dynamic_top_n": _compile_top_n,
    "filter": _compile_filter,
    "filtered": _compile_filter,
}


def compile_definition(builder_def: dict, model_metadata: dict | None = None) -> str:
    """Compile a builder_definition JSON into an MDX set expression.

    Args:
        builder_def: The builder definition dict with a ``type`` key.
        model_metadata: Optional model metadata for dimension/measure validation.

    Returns:
        MDX set expression string.

    Raises:
        CompilationError: If the definition is invalid or unsupported.
    """
    if not isinstance(builder_def, dict):
        raise CompilationError("builder_definition must be a dict")

    build_type = builder_def.get("type", "")
    compiler = _COMPILERS.get(build_type)
    if compiler is None:
        raise CompilationError(
            f"Unsupported builder type: {build_type!r}. "
            f"Supported: {', '.join(sorted(_COMPILERS.keys()))}"
        )
    return compiler(builder_def, model_metadata or {})


def explain_definition(builder_def: dict) -> str:
    """Return a plain-English explanation of a builder definition."""
    if not isinstance(builder_def, dict):
        return "Invalid definition"

    build_type = builder_def.get("type", "")

    if build_type in ("fixedMembers", "fixed"):
        members = builder_def.get("members", [])
        dim = builder_def.get("dimension", "unknown")
        count = len(members)
        return f"A fixed list of {count} member(s) from the {dim} dimension."

    if build_type in ("topN", "dynamic_top_n"):
        count = builder_def.get("count", "?")
        entity = builder_def.get("entity", "unknown")
        measure = builder_def.get("measure", "unknown")
        direction = builder_def.get("direction", "top")
        word = "highest" if direction == "top" else "lowest"
        return f"The {word} {count} items from {entity} ranked by {measure}."

    if build_type in ("filter", "filtered"):
        entity = builder_def.get("entity", "unknown")
        conditions = builder_def.get("conditions", [])
        logic = builder_def.get("logic", "AND")
        cond_count = len(conditions)
        return (
            f"Members from {entity} matching {cond_count} condition(s) "
            f"combined with {logic}."
        )

    return f"Advanced MDX expression (type: {build_type})."
