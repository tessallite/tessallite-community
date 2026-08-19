"""Canonical schema walker for cross-model recipe combine expressions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


COMBINE_OP_ARITY: dict[str, tuple[int, int | None]] = {
    **{
        name: (2, 2)
        for name in (
            "add", "sub", "mul", "div", "floordiv", "mod", "pow",
            "eq", "ne", "lt", "le", "gt", "ge",
        )
    },
    "not": (1, 1),
    "neg": (1, 1),
    "abs": (1, 1),
    "len": (1, 1),
    "round": (1, 2),
    "min": (1, None),
    "max": (1, None),
    "sum": (1, None),
    "and": (2, None),
    "or": (2, None),
    "if": (3, 3),
}


@dataclass(frozen=True)
class CombineReference:
    step: str
    measure: str
    path: str
    container: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class CombineSchemaIssue:
    path: str
    detail: str
    scope: Any = field(compare=False, repr=False)


class CombineSchemaError(ValueError):
    def __init__(self, path: str, detail: str):
        self.path = path
        self.detail = detail
        super().__init__(f"{path}: {detail}")


def collect_combine_references(
    node: Any,
    *,
    path: str = "$.combine",
) -> list[CombineReference]:
    """Validate the complete expression tree and return its typed references."""
    references, issues = inspect_combine_tree(node, path=path)
    if issues:
        first = issues[0]
        raise CombineSchemaError(first.path, first.detail)
    return references


def inspect_combine_tree(
    node: Any,
    *,
    path: str = "$.combine",
) -> tuple[list[CombineReference], list[CombineSchemaIssue]]:
    """Walk every reachable branch, retaining exact paths for schema defects."""
    if not isinstance(node, dict):
        return [], [CombineSchemaIssue(path, "must be an object", node)]
    kinds = {"const", "ref", "op"} & set(node)
    if len(kinds) != 1:
        return [], [CombineSchemaIssue(
            path, "must have exactly one of 'const', 'ref', or 'op'", node
        )]
    if "const" in node:
        if not isinstance(node["const"], (int, float, str, bool)):
            return [], [CombineSchemaIssue(
                f"{path}.const",
                "must be a number, string, or boolean",
                node["const"],
            )]
        return [], []
    if "ref" in node:
        ref = node["ref"]
        if not isinstance(ref, dict):
            return [], [CombineSchemaIssue(
                f"{path}.ref", "must be an object with step and measure", ref
            )]
        step = ref.get("step")
        measure = ref.get("measure")
        if not isinstance(step, str) or not step:
            return [], [CombineSchemaIssue(
                f"{path}.ref.step", "must be a non-empty string", ref
            )]
        if not isinstance(measure, str) or not measure:
            return [], [CombineSchemaIssue(
                f"{path}.ref.measure", "must be a non-empty string", ref
            )]
        return [CombineReference(step, measure, f"{path}.ref", ref)], []

    op = node["op"]
    if not isinstance(op, str) or not op:
        return [], [CombineSchemaIssue(
            f"{path}.op", "must be a non-empty string", node
        )]
    if op not in COMBINE_OP_ARITY:
        return [], [CombineSchemaIssue(
            f"{path}.op", f"unsupported operator {op!r}", node
        )]
    args = node.get("args")
    if not isinstance(args, list):
        return [], [CombineSchemaIssue(f"{path}.args", "must be a list", args)]
    minimum, maximum = COMBINE_OP_ARITY[op]
    if len(args) < minimum or (maximum is not None and len(args) > maximum):
        return [], [CombineSchemaIssue(
            f"{path}.args", f"invalid arity {len(args)} for operator {op!r}", args
        )]
    references: list[CombineReference] = []
    issues: list[CombineSchemaIssue] = []
    for index, child in enumerate(args):
        child_references, child_issues = inspect_combine_tree(
            child, path=f"{path}.args[{index}]"
        )
        references.extend(child_references)
        issues.extend(child_issues)
    return references, issues
