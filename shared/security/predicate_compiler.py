"""Compile row-security rules into a SQL predicate for a given principal.

Two rule shapes, discriminated by ``rule_type``:

* ``role_predicate`` — a restricted DSL expression evaluated if any of the
  rule's ``applies_to_roles`` matches the principal's role set. The special
  role ``"*"`` is a wildcard that matches every principal. DSL:
  ``dimension_equals(path, value)``, ``in(path, [v1, v2, ...])``,
  boolean composition via ``and(...)``, ``or(...)``, ``not(...)``.

* ``user_mapping`` — compiled to
  ``<dim_col> IN (SELECT <value_col> FROM <mapping_table> WHERE <user_col> = :user)``.

Composition (F-007-03 + wildcard baseline):

* WILDCARD (``"*"``) ``role_predicate`` rules are UNIVERSAL restrictions every
  principal must satisfy; they AND as mandatory baseline conjuncts and are never
  OR-composed with named grants (OR'ing would let a named-role caller bypass the
  universal floor — a fail-open widening).
* NAMED-role ``role_predicate`` fragments that apply are ALTERNATIVE entitlements
  — a multi-role caller is entitled to the UNION of their roles' row access, so
  those fragments are OR-composed (AND within one role's co-restrictions).
* ``user_mapping`` fragments are independent identity restrictions and AND.

The effective predicate is ``AND(each wildcard, OR(named grants), each mapping)``.

Coverage / fail-closed (F-007-01): if the model defines any ``role_predicate``
rule but the principal matches NO ``role_predicate`` rule (named OR wildcard),
``compile_row_security`` returns a deny-all predicate (``0 = 1``) rather than
``None`` — an unmatched member of a role-governed audience sees no rows, never
the unrestricted set. This fires EVEN IF a ``user_mapping`` rule matched (a
user_mapping applies to every identity and must not satisfy role-audience
coverage). ``None`` (skip injection) is returned only when the model has no
role_predicate rules and no user_mapping rule applies.

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

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from sqlglot import exp as sqlglot_exp

from shared.connector_qualify import CONNECTOR_TO_SQLGLOT, quote_identifier, quote_table_ref
from shared.db.models import ModelTable, RowSecurityRule


class RowSecurityCompileError(Exception):
    """Raised when a row-security rule cannot be compiled."""


class RowSecurityDialectError(RowSecurityCompileError):
    """Raised when a compiled predicate cannot be PROVEN safe to execute in a
    dialect other than the one it was compiled in (Bug-8396)."""


# Bug-8396: accept both canonical connector tokens (``postgresql``,
# ``hadoop_spark``) and sqlglot dialect tokens (``postgres``, ``spark``) so
# every call site can pass whichever it already holds. An unrecognised token is
# NOT defaulted to postgres — see ``_resolve_sqlglot_dialect``.
_DIALECT_TOKEN_ALIASES: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "pg": "postgres",
    "redshift": "redshift",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "spark": "spark",
    "spark_sql": "spark",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
    "mssql": "tsql",
    "tsql": "tsql",
}


def _resolve_sqlglot_dialect(token: str | None) -> str | None:
    """Map a connector or dialect token to a sqlglot dialect, or ``None``.

    Returning ``None`` for an unrecognised token is deliberate: a security
    predicate must never be transpiled on a guess. Callers treat ``None`` as
    "only a byte-identical token is acceptable" (see
    :func:`render_predicate_for_dialect`).
    """
    return _DIALECT_TOKEN_ALIASES.get((token or "").strip().lower())


def _parse_predicate(sql_text: str, dialect: str) -> tuple[Any, frozenset[str]]:
    """Parse a bare predicate under *dialect*; return its WHERE node and the
    set of column names it references."""
    import sqlglot

    ast = sqlglot.parse_one(f"SELECT 1 WHERE {sql_text}", read=dialect)
    where = ast.find(sqlglot_exp.Where)
    if where is None or where.this is None:
        raise RowSecurityDialectError(
            "the compiled row-security predicate did not parse to a WHERE clause "
            f"under dialect {dialect!r}"
        )
    return where, frozenset(c.name for c in where.find_all(sqlglot_exp.Column))


def render_predicate_for_dialect(compiled: Any, exec_dialect: str | None) -> str:
    """Return ``compiled.sql_expression`` rendered in *exec_dialect*, fail-closed.

    Bug-8396 (RLS bypass, cross-connector aggregates). ``sql_expression`` is a
    dialect-specific SQL string: identifiers are quoted with the SOURCE
    connector's quoting rules by :func:`compile_row_security`. An aggregate may
    legitimately be materialised into a target on a DIFFERENT connector (unlike
    a pocket, whose cross-connector materialisation is rejected outright by
    ``unsupported_pocket_combo_reason``), so the serving dialect is not always
    the compile dialect. Re-parsing a PostgreSQL-quoted predicate with
    ``read="bigquery"`` degrades ``"region"`` from an identifier to a string
    literal, so ``NOT "region" = 'EMEA'`` becomes the constant-true
    ``NOT 'region' = 'EMEA'`` and EVERY row is served.

    This is the single place allowed to move a compiled predicate between
    dialects. It proves the move preserved meaning instead of assuming it:

    * the predicate is parsed under the dialect it was actually COMPILED in;
    * a predicate that declares security dimension columns but parses to ZERO
      column references is rejected — that is the degradation signature, and it
      also catches a mis-recorded ``compile_connector``;
    * the re-rendered string is re-parsed under the execution dialect and its
      column set must be IDENTICAL to the compile-dialect column set.

    Raises :class:`RowSecurityDialectError` when the move cannot be proven
    safe. Callers must fail closed (reject / fall back to the source route),
    never execute the unconverted string.
    """
    sql_text = getattr(compiled, "sql_expression", None)
    if not sql_text:
        raise RowSecurityDialectError("compiled predicate has no SQL expression")
    # R5 finding F7: no guess. ``CompiledPredicate`` always carries this (field
    # default at the dataclass), and the router's rename block restamps it on
    # its SimpleNamespace copy, so an absent value means the caller built
    # something this function has no business transpiling. An enforcement gate
    # must raise there, not assume a dialect.
    src_token = getattr(compiled, "compile_connector", None)
    if not src_token:
        raise RowSecurityDialectError(
            "compiled predicate carries no compile_connector, so the dialect "
            "it was rendered in cannot be established"
        )
    src = _resolve_sqlglot_dialect(src_token)
    dst = _resolve_sqlglot_dialect(exec_dialect)

    if src is None or dst is None:
        # At least one token is not in the supported set. The only provably
        # safe action is to require the two tokens be the same string, in which
        # case no conversion is happening at all.
        if (src_token or "").strip().lower() == (exec_dialect or "").strip().lower():
            return sql_text
        raise RowSecurityDialectError(
            "cannot prove a row-security predicate compiled for "
            f"{src_token!r} is safe to execute as {exec_dialect!r}"
        )

    declared_cols = tuple(getattr(compiled, "security_dimension_columns", ()) or ())
    _where_src, cols_src = _parse_predicate(sql_text, src)
    if declared_cols and not cols_src:
        # The predicate restricts named dimension columns, yet parsing it under
        # its recorded compile dialect finds no identifier at all. Either the
        # recorded dialect is wrong or the expression already degraded. Both are
        # fail-closed conditions: executing it would filter nothing.
        raise RowSecurityDialectError(
            f"row-security predicate declares columns {declared_cols!r} but parses "
            f"to no column reference under its compile dialect {src!r} — refusing "
            "to execute a predicate that would not filter"
        )
    if dst == src:
        return sql_text

    rendered = _where_src.this.sql(dialect=dst)
    _where_dst, cols_dst = _parse_predicate(rendered, dst)
    if cols_dst != cols_src:
        raise RowSecurityDialectError(
            f"row-security predicate did not survive {src!r} -> {dst!r} rendering: "
            f"columns {sorted(cols_src)} became {sorted(cols_dst)}"
        )
    return rendered


# Bug-8447, user decision Option B: these named roles are exempt from the
# unmatched-audience coverage denial.  This is deliberately narrower than an
# RLS bypass: a wildcard rule or a rule that explicitly names one of these
# roles still matches and is compiled normally.
PRIVILEGED_COVERAGE_EXEMPT_ROLES = frozenset(
    {"tenant_admin", "modeler", "system_admin"}
)


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
    # An explicitly authenticated internal full-model operation may opt out
    # of the unmatched-audience *coverage* denial. This is not an RLS bypass:
    # wildcard rules and rules that explicitly match the principal still
    # compile and apply. The query-router sets this only for the signed KPI
    # snapshot hop after validating its service scope and client marker.
    unmatched_role_coverage_exempt: bool = False

    @classmethod
    def from_current_user(cls, current_user: Any) -> "Principal":
        """Adapt the shared ``CurrentUser`` to a Principal.

        Bug-7995 / F-024-01: an embed session with an admin-authored RLS subject
        surfaces its role/groups/claims under these same attribute names, so an
        embedded principal is built identically to an interactive one. A bare
        embed token (no RLS role) carries the ``"embed"`` sentinel role — that is
        NOT a real IdP role, so it is mapped to an empty role set. This keeps a
        role-governed model failing closed (the coverage gate denies all rows)
        for a subject-less embed token, and prevents a rule that literally targets
        the string ``embed`` from being matched by every embed session.

        Bug-8017 / F-007-03: the principal's named-role set is built from the
        multi-valued ``CurrentUser.roles`` subject when present (minted on the
        JWT ``roles`` claim), so a caller entitled to several named RLS roles is
        matched against — and ORs the grants of — ALL of them, not only the
        single RBAC-tier ``role``. It falls back to ``{role}`` for legacy tokens
        and hand-built CurrentUser instances, keeping their behaviour identical.
        """
        role = getattr(current_user, "role", None)
        email = getattr(current_user, "email", None) or getattr(
            current_user, "user_id", ""
        )
        if role == "embed" and getattr(current_user, "is_embed", False):
            role = None
        # Bug-8017 / F-007-03: a caller entitled to several named RLS roles is
        # entitled to the UNION of their roles' row access, so the principal
        # must carry the FULL named-role set — not just the single RBAC-tier
        # ``role``. Prefer the multi-valued ``roles`` subject minted on the JWT
        # (populated by every human mint site and derived from the single
        # rls_role/role for embed/service tokens). Fall back to ``{role}`` when
        # ``roles`` is absent or empty (legacy tokens, hand-built CurrentUser,
        # or the embed sentinel that just cleared ``role``) so pre-fix behaviour
        # is byte-identical and no principal is ever widened or crashes.
        raw_roles = getattr(current_user, "roles", None)
        multi_roles = (
            frozenset(r for r in raw_roles if r)
            if isinstance(raw_roles, (list, tuple, set, frozenset))
            else frozenset()
        )
        # The embed sentinel path cleared ``role`` above; honour it by dropping
        # the sentinel from any minted roles list too (a bare embed token must
        # carry no named RLS role so a role-governed model fails closed).
        if getattr(current_user, "is_embed", False):
            multi_roles = multi_roles - {"embed"}
        if multi_roles:
            roles = multi_roles
        else:
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
    # Bug-7039: source_ids of mapping tables used by user_mapping rules.
    # Empty when no user_mapping rules are active. The router validates
    # that these match the fact query's execution source_id to prevent
    # cross-database subqueries that cannot execute.
    mapping_source_ids: tuple[str, ...] = field(default_factory=tuple)
    # Bug-8396: the connector whose SQL dialect ``sql_expression`` was RENDERED
    # in. Identifier quoting is dialect-specific (PostgreSQL/Redshift double
    # quotes vs BigQuery/Spark backticks), so a consumer that re-parses this
    # string under a DIFFERENT dialect silently mis-reads it: a PostgreSQL
    # ``"region"`` parsed with ``read="bigquery"`` is a STRING LITERAL, turning
    # ``NOT "region" = 'EMEA'`` into the constant-true ``NOT 'region' = 'EMEA'``
    # — a total row-security bypass. Any consumer executing this predicate
    # against a connection in another dialect MUST go through
    # :func:`render_predicate_for_dialect` rather than re-parsing blind.
    compile_connector: str = "postgresql"
    # F-007-05 / Bug-8896: (column_name, physical_table_name) pairs so
    # ``_inject_security_where`` can qualify the security column with the
    # owning relation instead of fanning the predicate onto every scan.
    # Empty means "owner unknown" — inject stays bare (a fact-only column
    # still works). Populated at compile time from Dimension → ModelColumn
    # → ModelTable.physical_name.
    security_column_owners: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def policy_hash(self) -> str:
        """Deterministic hash of the compiled RLS policy for cache keying.

        Bug-7038: the result-cache key must incorporate the effective
        row-security rule identity so that tightening (editing, creating, or
        deleting) a rule invalidates stale pre-tightening cached rows. The
        hash is derived from the sorted rule IDs and the compiled SQL
        expression, both of which are deterministic across replicas for the
        same database state (same rules produce the same compiled output).
        """
        import hashlib
        canonical = "|".join(sorted(self.active_rule_ids)) + "||" + self.sql_expression
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Rule loading + principal matching
# ---------------------------------------------------------------------------


async def _load_rules_for_model(
    db: AsyncSession, model_id: Any
) -> list[RowSecurityRule]:
    # Bug-7038 (codex review R1): ORDER BY id so the compiled sql_expression
    # is deterministic across replicas. Without this, two replicas loading the
    # same rules could produce different fragment ordering in the AND-joined
    # expression, causing cache-key divergence for identical policy state.
    result = await db.execute(
        sa_select(RowSecurityRule).where(
            RowSecurityRule.model_id == model_id,
            RowSecurityRule.is_enabled.is_(True),
        ).order_by(RowSecurityRule.id)
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


def _matched_role_key(rule: RowSecurityRule, principal: Principal) -> frozenset[str]:
    """The principal-role subset that caused *rule* to apply (F-007-03 grouping).

    Two role_predicate rules that target the SAME role are co-restrictions on
    that role's grant (AND them). Rules reached via DIFFERENT roles are
    alternative grants a multi-role principal is entitled to (OR them). This
    returns the key that groups rules into per-grant families:

    * wildcard (``*``) rule -> the fixed key ``{"*"}`` (a grant available to
      every principal, independent of which named role they hold);
    * named/attribute rule -> the intersection of the rule's roles with the
      principal's matched attribute values, so a rule listing ``[R1, R2]`` for a
      principal holding both keys under ``{R1, R2}`` (one grant reachable via
      either), while a rule listing only ``[R1]`` keys under ``{R1}``.
    """
    rule_roles = set(rule.applies_to_roles or [])
    if "*" in rule_roles:
        return frozenset({"*"})
    subject = _resolve_principal_attribute(rule, principal)
    return frozenset(rule_roles & set(subject))


# Fail-closed sentinel (F-007-01): a predicate that admits no row. When a model
# defines role_predicate row-security rules but NONE matches the principal, the
# principal is inside the governed audience with no applicable grant, so RLS
# must deny every row rather than run unfiltered. ``0 = 1`` is dialect-portable
# (every supported connector renders it identically) and needs no column to be
# visible in the scanned scope, so ``_inject_security_where`` can always inject
# it (unlike a column-scoped predicate).
_DENY_ALL_PREDICATE = "0 = 1"


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


def _quote_value(v: str, connector: str = "postgresql") -> str:
    m = _STRING_LITERAL_RE.match(v)
    if not m:
        raise RowSecurityCompileError(
            f"row-security predicate values must be single-quoted strings, got {v!r}"
        )
    # The regex accepts doubled quotes (`''`) as escaped quotes inside the
    # literal, so the capture group already contains encoded data. Decode to
    # the raw value, then re-encode through sqlglot for the TARGET dialect.
    #
    # Bug-6133 [SECURITY]: the previous ANSI re-encode (`'`->`''`) is only
    # correct for engines that use doubled single quotes. BigQuery and Spark
    # escape embedded quotes with a BACKSLASH (`\'`), so a value like
    # ``O'Brien`` rendered as ``'O''Brien'`` is re-parsed there as the literal
    # ``'O'`` followed by a stray ``'Brien'`` token — silently corrupting the
    # RLS predicate (wrong rows) or opening a predicate-injection surface.
    # Rendering the literal via sqlglot for the connector's dialect (SQL rule 1:
    # dialect via sqlglot transpilation, never per-connector branches) emits the
    # correct per-dialect escaping and round-trips cleanly.
    decoded = m.group(1).replace("''", "'")
    dialect = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    return sqlglot_exp.Literal.string(decoded).sql(dialect=dialect)


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
        return f'{quote_identifier(connector, col)} = {_quote_value(args[1].strip(), connector)}'

    if fn == "in":
        if len(args) < 2:
            raise RowSecurityCompileError("in() expects path and at least one value")
        path_arg = args[0].strip()
        path_lit = _STRING_LITERAL_RE.match(path_arg)
        if not path_lit:
            raise RowSecurityCompileError("in() first argument must be a quoted path")
        col = _path_to_column(path_lit.group(1))
        values = ", ".join(_quote_value(a.strip(), connector) for a in args[1:])
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
) -> tuple[str, str | None]:
    """Return (sql_fragment, source_id) for a user_mapping rule.

    Bug-7039: the source_id of the mapping table is returned so the
    router can validate it matches the fact query's execution source.
    """
    table = await _load_mapping_table(db, rule.mapping_table_id)
    dim_col = _path_to_column(rule.dimension_path)
    value_col = quote_identifier(connector, rule.mapping_value_column or "")
    user_col = quote_identifier(connector, rule.mapping_user_column or "")
    phys = quote_table_ref(connector, table.physical_name)
    dialect = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    user_literal = sqlglot_exp.Literal.string(principal.user_identity).sql(dialect=dialect)
    frag = (
        f'{quote_identifier(connector, dim_col)} IN (SELECT {value_col} FROM {phys} '
        f"WHERE {user_col} = {user_literal})"
    )
    # Bug-7039: capture the mapping table's source_id for cross-source validation.
    source_id = str(getattr(table, "source_id", "") or "") or None
    return frag, source_id


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

    # F-007-01 (fail-closed coverage): a model that defines role_predicate
    # row-security rules governs an audience. If the principal matched NO
    # role_predicate rule, they are an unmatched member of that audience — a
    # typo'd/renamed IdP role, a role the admin forgot to grant, or a genuine
    # non-member. RLS must DENY every row for them rather than treat the
    # absence of a matching grant as "unrestricted".
    #
    # Fable B2-2: the coverage gate fires even when a DIFFERENT rule type
    # (user_mapping) matched. A user_mapping rule applies to every authenticated
    # identity, so in a model with BOTH a role_predicate rule and a user_mapping
    # rule, ``matching`` is never empty for an authenticated caller. Gating only
    # on ``not matching`` left the role-governed audience unenforced for a caller
    # who matched the user_mapping but no role rule — they read every row of the
    # role dimension. Deny-all whenever the model is role-governed but the
    # principal matched no role_predicate rule, regardless of user_mapping.
    has_role_predicate_rule = any(
        r.rule_type == "role_predicate" for r in rules
    )
    matched_role_predicate = any(
        r.rule_type == "role_predicate" for r in matching
    )
    privileged_coverage_exempt = bool(
        principal.roles & PRIVILEGED_COVERAGE_EXEMPT_ROLES
    ) or principal.unmatched_role_coverage_exempt
    if (
        has_role_predicate_rule
        and not matched_role_predicate
        and not privileged_coverage_exempt
    ):
        return _deny_all_predicate()
    if not matching:
        # No role_predicate rules on the model and nothing else matched -> no-op.
        return None

    # F-007-03 (OR-of-role entitlements) + Fable B2-1 (wildcard baseline).
    # role_predicate rules split into two kinds:
    #   * WILDCARD ("*") rules — a UNIVERSAL restriction every principal must
    #     satisfy (Bug-883 "everyone is limited to X"). These AND as mandatory
    #     baseline conjuncts; they are NOT alternatives and must NEVER be OR'd
    #     with named grants (OR'ing let a named-role caller bypass the universal
    #     floor — a fail-open widening).
    #   * NAMED-role grants — grouped by the principal-role subset that made them
    #     apply: AND within a group (same-role co-restrictions), OR across groups
    #     (a multi-role principal is entitled to the UNION of their roles).
    # user_mapping fragments are independent identity restrictions and AND too.
    # Effective predicate:
    #   AND( each wildcard, OR(named grant groups), each mapping )
    wildcard_fragments: list[str] = []
    role_groups: dict[frozenset[str], list[str]] = {}
    role_group_order: list[frozenset[str]] = []
    mapping_fragments: list[str] = []
    active_ids: list[str] = []
    dim_cols: list[str] = []
    applied: list[dict] = []
    mapping_source_ids: list[str] = []

    for rule in matching:
        if rule.rule_type == "role_predicate":
            frag = _compile_dsl_expression(rule.predicate_expression or "", connector)
            key = _matched_role_key(rule, principal)
            if key == frozenset({"*"}):
                # Universal baseline — mandatory AND, never an OR alternative.
                wildcard_fragments.append(frag)
            else:
                if key not in role_groups:
                    role_groups[key] = []
                    role_group_order.append(key)
                role_groups[key].append(frag)
        elif rule.rule_type == "user_mapping":
            frag, mapping_src = await _compile_user_mapping(rule, principal, db, connector)
            if mapping_src:
                mapping_source_ids.append(mapping_src)
            mapping_fragments.append(frag)
        else:
            raise RowSecurityCompileError(
                f"unknown rule_type on rule {rule.id}: {rule.rule_type!r}"
            )
        active_ids.append(str(rule.id))
        dim_cols.append(_path_to_column(rule.dimension_path))
        applied.append({"rule_id": str(rule.id), "rule_name": rule.name, "predicate_sql": frag})

    # Build each NAMED role-grant group (AND within), then OR the grants.
    grant_clauses: list[str] = []
    for key in role_group_order:
        frags = role_groups[key]
        if len(frags) > 1:
            grant_clauses.append("(" + " AND ".join(frags) + ")")
        else:
            grant_clauses.append(frags[0])

    conjuncts: list[str] = []
    # Wildcard baselines are mandatory conjuncts (Fable B2-1).
    conjuncts.extend(wildcard_fragments)
    if grant_clauses:
        if len(grant_clauses) > 1:
            conjuncts.append("(" + " OR ".join(grant_clauses) + ")")
        else:
            conjuncts.append(grant_clauses[0])
    conjuncts.extend(mapping_fragments)

    if conjuncts:
        sql_expression = " AND ".join(conjuncts) if len(conjuncts) > 1 else conjuncts[0]
    else:  # pragma: no cover - matching is non-empty, so at least one conjunct
        sql_expression = _DENY_ALL_PREDICATE

    owners = await _load_security_column_owners(
        db, model_id, dim_cols, dim_paths=[r.dimension_path for r in matching],
    )
    return CompiledPredicate(
        sql_expression=sql_expression,
        active_rule_ids=tuple(active_ids),
        security_dimension_columns=tuple(dict.fromkeys(dim_cols)),
        applied_rules=tuple(applied),
        mapping_source_ids=tuple(dict.fromkeys(mapping_source_ids)),
        # Bug-8396: record the dialect the fragments were actually quoted in so
        # a consumer serving from a target on another connector can convert it
        # provably instead of re-parsing it blind.
        compile_connector=connector,
        security_column_owners=owners,
    )


def _physical_table_token(phys: str) -> str:
    """Last identifier of a physical table name (``demo.sales`` → ``sales``).

    ``_inject_security_where`` matches owners against sqlglot ``table.name``,
    which is the unqualified relation token.
    """
    token = str(phys).strip().strip('"').strip("`")
    if "." in token:
        token = token.rsplit(".", 1)[-1]
    return token


def _preferred_path_names(dim_paths: list[str] | None, column: str) -> set[str]:
    """Dimension-path segments that identify the owner of ``column``."""
    col = (column or "").lower()
    out: set[str] = set()
    for path in dim_paths or []:
        if not path:
            continue
        parts = [p for p in str(path).split(".") if p]
        if not parts or parts[-1].lower() != col:
            continue
        out.add(parts[-1].lower())
        if len(parts) >= 2:
            out.add(parts[0].lower())
    return out


async def _load_security_column_owners(
    db: AsyncSession, model_id: Any, dim_cols: list[str],
    dim_paths: list[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Resolve (column_name, physical_table_name) for security columns (F-007-05).

    Used by ``_inject_security_where`` to qualify the predicate with the
    owning relation instead of leaving a bare column that two joined
    scans of the same name make ambiguous. Fail open to empty owners
    (bare inject, existing tests) when the lookup cannot run — never
    invent a table name.

    Bug-9264: ORDER BY Dimension.id and prefer the rule's dimension_path
    owner, then fact, so two tables carrying the same column do not
    qualify fact vs dim nondeterministically. Empty owners stay
    bare-column. Multiple owners for one column are ordered fallbacks
    (first present in the SELECT wins) — never fanned out (Bug-7034).
    """
    if not dim_cols:
        return ()
    try:
        from shared.db.models import Dimension, ModelColumn, ModelTable

        lowered = [c.lower() for c in dim_cols]
        result = await db.execute(
            sa_select(
                ModelColumn.column_name,
                ModelTable.physical_name,
                ModelTable.table_type,
                Dimension.name,
                Dimension.id,
            )
            .select_from(Dimension)
            .join(ModelColumn, ModelColumn.id == Dimension.source_column_id)
            .join(ModelTable, ModelTable.id == ModelColumn.model_table_id)
            .where(
                Dimension.model_id == model_id,
                sa_func.lower(ModelColumn.column_name).in_(lowered),
            )
            .order_by(Dimension.id)
        )
        rows = result.all() if hasattr(result, "all") else []
        by_col: dict[str, list[tuple[str, str, str, str, str]]] = {}
        seen: set[tuple[str, str]] = set()
        for col_name, phys, table_type, dim_name, dim_id in rows:
            if not col_name or not phys:
                continue
            token = _physical_table_token(str(phys))
            if not token:
                continue
            key = (str(col_name), token)
            if key in seen:
                continue
            seen.add(key)
            by_col.setdefault(str(col_name), []).append(
                (
                    str(col_name),
                    token,
                    str(table_type or ""),
                    str(dim_name or ""),
                    str(dim_id),
                )
            )
        pairs: list[tuple[str, str]] = []
        for col_name, cands in by_col.items():
            preferred = _preferred_path_names(dim_paths, col_name)

            def _rank(c: tuple[str, str, str, str, str]) -> tuple[int, str]:
                _cname, _token, ttype, dname, did = c
                dlow = dname.lower()
                if preferred and dlow in preferred:
                    return (0, did)
                if ttype.lower() == "fact":
                    return (1, did)
                return (2, did)

            ordered = sorted(cands, key=_rank)
            for _cname, token, _ttype, _dname, _did in ordered:
                pairs.append((_cname, token))
        return tuple(pairs)
    except Exception:
        logger.debug(
            "F-007-05: could not resolve security column owners",
            exc_info=True,
        )
        return ()


def _deny_all_predicate() -> CompiledPredicate:
    """A CompiledPredicate that admits no row (F-007-01 fail-closed coverage).

    ``active_rule_ids`` carries the sentinel ``"__deny_all__"`` so
    :func:`has_active_rules` reports the policy active (the router must take the
    RLS path and inject the predicate), while ``security_dimension_columns`` is
    empty because ``0 = 1`` references no column and can be injected into any
    scanned SELECT. An aggregate/pocket is never RLS-safe for this predicate
    (``_aggregate_is_rls_safe`` fails closed on empty security columns), so a
    denied principal always routes to source with the deny-all filter.
    """
    return CompiledPredicate(
        sql_expression=_DENY_ALL_PREDICATE,
        active_rule_ids=("__deny_all__",),
        security_dimension_columns=(),
        applied_rules=(
            {
                "rule_id": "__deny_all__",
                "rule_name": "row-security coverage deny-all",
                "predicate_sql": _DENY_ALL_PREDICATE,
            },
        ),
        mapping_source_ids=(),
    )


def has_active_rules(
    compiled: CompiledPredicate | None,
) -> bool:
    """True iff the principal has at least one matching rule.

    The router uses this to choose the ROUTE-SELECTION path
    (``_route_with_row_security``), NOT to bypass acceleration. Bug-7033 /
    Bug-8018: an active rule does not disable the aggregate and pocket
    matchers — each candidate must instead prove it can carry the same
    compiled predicate (``router._aggregate_is_rls_safe`` needs every security
    column in the aggregate's grain; ``router._pocket_is_rls_safe`` needs a
    row-preserving ``SELECT *`` whose ``row_manifest`` records every security
    column, matched case-sensitively), and anything unproven routes to source
    with the predicate injected there. Enforcement itself is per-scan WHERE
    injection (F-007-01), never an outer subquery wrap.
    """
    return compiled is not None and bool(compiled.active_rule_ids)
