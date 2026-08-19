"""Seed-bundle row-security contract tests.

Bug-6132: demo/bootstrap row-security rules must compile under the restricted
RLS DSL before they can be imported or deployed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.security.predicate_compiler import (
    RowSecurityCompileError,
    _compile_dsl_expression,
)


def _walk_row_security_rules(node: Any, path: str = "$") -> list[tuple[str, dict]]:
    rules: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        maybe_rules = node.get("row_security_rules")
        if isinstance(maybe_rules, list):
            for index, rule in enumerate(maybe_rules):
                if isinstance(rule, dict):
                    rules.append((f"{path}.row_security_rules[{index}]", rule))
        for key, value in node.items():
            rules.extend(_walk_row_security_rules(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            rules.extend(_walk_row_security_rules(value, f"{path}[{index}]"))
    return rules


def test_acme_seed_row_security_role_predicates_compile() -> None:
    seed_path = (
        Path(__file__).resolve().parents[3]
        / "seeds"
        / "acme-demo"
        / "project.json"
    )
    payload = json.loads(seed_path.read_text(encoding="utf-8-sig"))
    all_rules = _walk_row_security_rules(payload)
    role_predicates = [
        (path, rule)
        for path, rule in all_rules
        if rule.get("rule_type") == "role_predicate"
    ]

    assert role_predicates, "seed bundle must contain row-security role predicates"

    failures: list[str] = []
    for path, rule in role_predicates:
        expr = (rule.get("predicate_expression") or "").strip()
        if not expr:
            failures.append(f"{path}: empty predicate_expression")
            continue
        try:
            _compile_dsl_expression(expr)
        except RowSecurityCompileError as exc:
            failures.append(f"{path} ({rule.get('name', 'unnamed')}): {exc}")

    assert failures == []
