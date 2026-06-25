"""Compile row-security rules into a SQL predicate for a given principal.

Two rule shapes, discriminated by ``rule_type``:

* ``role_predicate`` — a restricted DSL expression evaluated if any of the
  rule's ``applies_to_roles`` matches the principal's role set. The special
  role ``"*"`` is a wildcard that matches every principal. DSL:
  ``dimension_equals(path, value)``, ``in(path, [v1, v2, ...])``,
  boolean composition via ``and(...)``, ``or(...)``, ``not(...)``.

* ``user_mapping`` — compiled to
  ``<dim_col> IN (SELECT <value_col> FROM <mapping_table> WHERE <user_col> = :user)``.

Multiple active rules are joined with ``AND``. If no rule applies to the
principal, ``compile_row_security`` returns ``None`` and the router skips
predicate injection (no-op).

This module only compiles the predicate string. The router
(:func:`query-router.src.routing.router._inject_security_where`) is the
enforcement pass: it ANDs the compiled predicate into the WHERE of every
SELECT that scans a physical table (per-scan injection, fail-closed). The
old subquery-wrap pass was retired in the F-007-01 fix because it
re-broke ``LIMIT`` semantics (Bug-915) and could not correctly scope a
scalar-subquery leak.

References to dimension paths resolve to the **last segment** of the path
(e.g. ``region.region_code`` → ``"region_code"``). The predicate is
column-name scoped: a scope where that column is not visible fails in the
database ("column does not exist") rather than running unfiltered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.connector_qualify import quote_identifier, quote_table_ref
from shared.db.models import ModelTable, RowSecurityRule


class RowSecurityCompileError(Exception):
    """Raised when a row-security rule cannot be compiled."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller identity used for row-security matching.

    ``roles`` is a frozenset so rules that list multiple matching roles
    collapse to set-membership checks. ``groups`` carries IdP group names
    embedded in the JWT (populated at SSO login). ``claims`` carries
    arbitrary custom JWT claims for SAML attribute / OIDC scope matching.
    """

    user_identity: str
    roles: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    claims: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_current_user(cls, current_user: Any) -> "Principal":
        """Adapt the shared ``CurrentUser`` to a Principal."""
        role = getattr(current_user, "role", None)
        email = getattr(current_user, "email", None) or getattr(
            current_user, "user_id", ""
        )
        roles = frozenset([role]) if role else frozenset()
        raw_groups = getattr(current_user, "groups", None) or []
        groups = frozenset(raw_groups) if raw_groups else frozenset()
        # F-007-02: carry IdP claims (SAML attributes / OIDC claims+scopes)
        # so saml_claim / oidc_scope attribute sources resolve at runtime,
        # not only in the /simulate preview.
        raw_claims = getattr(current_user, "claims", None)
        claims = dict(raw_claims) if isinstance(raw_claims, dict) else {}
        return cls(
            user_identity=email, roles=roles, groups=groups, claims=claims,
        )


@dataclass(frozen=True)
class CompiledPredicate:
    """Result of compiling all matching rules for a principal."""

    sql_expression: str
    active_rule_ids: tuple[str, ...]
    security_dimension_columns: tuple[str, ...] = field(default_factory=tuple)
    applied_rules: tuple[dict, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Rule loading + principal matching
# ---------------------------------------------------------------------------


async def _load_rules_for_model(
    db: AsyncSession, model_id: Any
) -> list[RowSecurityRule]:
    result = await db.execute(
        sa_select(RowSecurityRule).where(
            RowSecurityRule.model_id == model_id,
            RowSecurityRule.is_enabled.is_(True),
        )
    )
    return list(result.scalars().all())


def _resolve_principal_attribute(rule: RowSecurityRule, principal: Principal) -> frozenset[str]:
    """Return the set of values from the principal to match against applies_to_roles."""
    source = getattr(rule, "attribute_source", "jwt_role") or "jwt_role"
    if source == "jwt_role":
        return principal.roles
    if source == "idp_group":
        return principal.groups
    if source in ("saml_claim", "oidc_scope"):
        claim_name = getattr(rule, "attribute_claim_name", None)
        if not claim_name:
            return frozenset()
        raw = principal.claims.get(claim_name)
        if raw is None:
            return frozenset()
        if isinstance(raw, list):
            return frozenset(str(v) for v in raw)
        if source == "oidc_scope" and isinstance(raw, str):
            # OAuth2 grants are conventionally a space-delimited string
            # ("openid profile reports:read") — match each scope value.
            return frozenset(raw.split())
        return frozenset([str(raw)])
    return principal.roles


def _rule_applies_to_principal(rule: RowSecurityRule, principal: Principal) -> bool:
    if rule.rule_type == "role_predicate":
        rule_roles = set(rule.applies_to_roles or [])
        if "*" in rule_roles:
            return True  # wildcard — applies to every principal
        subject = _resolve_principal_attribute(rule, principal)
        return bool(rule_roles.intersection(subject))
    if rule.rule_type == "user_mapping":
        return bool(principal.user_identity)
    return False


# ---------------------------------------------------------------------------
# DSL parsing for role_predicate
# ---------------------------------------------------------------------------

# Restricted DSL: the grammar is intentionally tiny. Anything beyond the
# four recognised calls + boolean composition raises. This keeps the wrap
# predicates auditable at review time and closes the door on arbitrary SQL
# injection via stored expressions.
_DSL_FN_CALL_RE = re.compile(
    r"(?P<fn>[a-z_]+)\s*\((?P<args>.*)\)\s*$", re.IGNORECASE | re.DOTALL
)
_STRING_LITERAL_RE = re.compile(r"^'((?:[^']|'')*)'$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _path_to_column(path: str) -> str:
    """Translate a dimension path to the column name the wrap references.

    Uses the last dotted segment. The inner query must project that
    column for the wrap to apply — v1 limitation logged to
    ``docs/execution/execution_issue-registry.md``.
    """
    if not path or not _IDENT_RE.match(path):
        raise RowSecurityCompileError(
            f"invalid dimension path in row-security rule: {path!r}"
        )
    return path.rsplit(".", 1)[-1]


def _split_top_level(args: str) -> list[str]:
    """Split an argument list by top-level commas, respecting quotes + parens."""
    out: list[str] = []
    depth = 0
    in_str = False
    cur: list[str] = []
    i = 0
    while i < len(args):
        ch = args[i]
        if in_str:
            cur.append(ch)
            if ch == "'":
                if i + 1 < len(args) and args[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        out.append("".join(cur).strip())
    return out


def _quote_value(v: str) -> str:
    m = _STRING_LITERAL_RE.match(v)
    if not m:
        raise RowSecurityCompileError(
            f"row-security predicate values must be single-quoted strings, got {v!r}"
        )
    # The regex accepts doubled quotes (`''`) as escaped quotes inside the
    # literal, so the capture group already contains encoded data. Decode
    # then re-encode so the output round-trips cleanly and never
    # double-escapes an already-escaped input.
    decoded = m.group(1).replace("''", "'")
    inner = decoded.replace("'", "''")
    return f"'{inner}'"


def _compile_dsl_expression(expr: str, connector: str = "postgresql") -> str:
    """Recursively compile a restricted DSL expression to SQL."""
    expr = expr.strip()
    m = _DSL_FN_CALL_RE.match(expr)
    if not m:
        raise RowSecurityCompileError(
            f"row-security expression must be a function call, got {expr!r}"
        )
    fn = m.group("fn").lower()
    args = _split_top_level(m.group("args"))

    if fn == "dimension_equals":
        if len(args) != 2:
            raise RowSecurityCompileError(
                "dimension_equals expects exactly 2 arguments"
            )
        path_arg = args[0].strip()
        path_lit = _STRING_LITERAL_RE.match(path_arg)
        if not path_lit:
            raise RowSecurityCompileError(
                "dimension_equals first argument must be a quoted path"
            )
        col = _path_to_column(path_lit.group(1))
        return f'{quote_identifier(connector, col)} = {_quote_value(args[1].strip())}'

    if fn == "in":
        if len(args) < 2:
            raise RowSecurityCompileError("in() expects path and at least one value")
        path_arg = args[0].strip()
        path_lit = _STRING_LITERAL_RE.match(path_arg)
        if not path_lit:
            raise RowSecurityCompileError("in() first argument must be a quoted path")
        col = _path_to_column(path_lit.group(1))
        values = ", ".join(_quote_value(a.strip()) for a in args[1:])
        return f'{quote_identifier(connector, col)} IN ({values})'

    if fn == "and":
        if not args:
            raise RowSecurityCompileError("and() requires at least one argument")
        compiled = [_compile_dsl_expression(a, connector) for a in args]
        return "(" + " AND ".join(compiled) + ")"

    if fn == "or":
        if not args:
            raise RowSecurityCompileError("or() requires at least one argument")
        compiled = [_compile_dsl_expression(a, connector) for a in args]
        return "(" + " OR ".join(compiled) + ")"

    if fn == "not":
        if len(args) != 1:
            raise RowSecurityCompileError("not() requires exactly one argument")
        return "(NOT " + _compile_dsl_expression(args[0], connector) + ")"

    raise RowSecurityCompileError(f"unknown row-security function: {fn!r}")


# ---------------------------------------------------------------------------
# user_mapping compilation
# ---------------------------------------------------------------------------


async def _load_mapping_table(
    db: AsyncSession, mapping_table_id: Any
) -> ModelTable:
    result = await db.execute(
        sa_select(ModelTable).where(ModelTable.id == mapping_table_id)
    )
    table = result.scalar_one_or_none()
    if table is None:
        raise RowSecurityCompileError(
            f"row-security mapping table {mapping_table_id!r} not found"
        )
    return table


async def _compile_user_mapping(
    rule: RowSecurityRule, principal: Principal, db: AsyncSession,
    connector: str = "postgresql",
) -> str:
    table = await _load_mapping_table(db, rule.mapping_table_id)
    dim_col = _path_to_column(rule.dimension_path)
    value_col = quote_identifier(connector, rule.mapping_value_column or "")
    user_col = quote_identifier(connector, rule.mapping_user_column or "")
    phys = quote_table_ref(connector, table.physical_name)
    user_literal = "'" + principal.user_identity.replace("'", "''") + "'"
    return (
        f'{quote_identifier(connector, dim_col)} IN (SELECT {value_col} FROM {phys} '
        f"WHERE {user_col} = {user_literal})"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def compile_row_security(
    model_id: Any,
    principal: Principal,
    db: AsyncSession,
    connector: str = "postgresql",
) -> CompiledPredicate | None:
    """Return a compiled predicate for every active rule matching the principal.

    Returns ``None`` when no rule applies — callers treat this as a no-op
    and skip the wrap.
    """
    rules = await _load_rules_for_model(db, model_id)
    matching = [r for r in rules if _rule_applies_to_principal(r, principal)]
    if not matching:
        return None

    fragments: list[str] = []
    active_ids: list[str] = []
    dim_cols: list[str] = []
    applied: list[dict] = []

    for rule in matching:
        if rule.rule_type == "role_predicate":
            frag = _compile_dsl_expression(rule.predicate_expression or "", connector)
        elif rule.rule_type == "user_mapping":
            frag = await _compile_user_mapping(rule, principal, db, connector)
        else:
            raise RowSecurityCompileError(
                f"unknown rule_type on rule {rule.id}: {rule.rule_type!r}"
            )
        fragments.append(frag)
        active_ids.append(str(rule.id))
        dim_cols.append(_path_to_column(rule.dimension_path))
        applied.append({"rule_id": str(rule.id), "rule_name": rule.name, "predicate_sql": frag})

    sql_expression = " AND ".join(fragments) if len(fragments) > 1 else fragments[0]
    return CompiledPredicate(
        sql_expression=sql_expression,
        active_rule_ids=tuple(active_ids),
        security_dimension_columns=tuple(dict.fromkeys(dim_cols)),
        applied_rules=tuple(applied),
    )


def has_active_rules(
    compiled: CompiledPredicate | None,
) -> bool:
    """True iff the principal has at least one matching rule.

    Router uses this to bypass aggregate + pocket matching.
    """
    return compiled is not None and bool(compiled.active_rule_ids)
