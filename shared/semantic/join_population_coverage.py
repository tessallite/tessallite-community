"""Mutation-sensitive inventory for the G3 join-population consumers.

This is intentionally an explicit inventory, not a claim that AST inspection
can prove arbitrary future Python.  The guard catches the practical failure
mode for this change: a known builder, wrapper, alias, or re-export is changed
back to projection-only logic without being wired to the shared contract.  A
new builder-shaped function or a new import alias must be added to the
inventory before the guard passes.

The check is deliberately about AST calls and value flow, rather than text.
That distinction matters: a dead reference to ``augment_required_table_ids``
or a call whose result is discarded is not a serving contract.  Unknown import
forms and parse failures are violations (fail closed), while arbitrary future
syntax remains outside this small, enumerated guard's claim.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ConsumerSpec:
    path: str
    function: str
    required_symbols: tuple[str, ...]
    note: str


_CALL_SYMBOLS = frozenset({
    "augment_required_table_ids",
    "population_defining_join_rows",
    "population_defining_table_ids",
    "aggregate_plan_upper_bound",
    "build_from_clause",
    "_build_from_clause",
    "_build_joined_from_clause",
    "_resolve_required_and_base_tables",
    "_population_star_from_clause",
    "bind_query_to_model",
    "_full_refresh_aggregate_locked",
    "_incremental_refresh_aggregate_locked",
    "JoinEdge",
    "normalized_population_participation",
    "named_query_population_manifest_matches",
    "bind_query_to_model",
})

# These calls return a value that must be captured and used by the consumer.
# The two JoinEdge-field calls are intentionally different: they are
# constructor/keyword arguments whose value is propagated directly into the
# graph object rather than through a local assignment.
_ASSIGNMENT_CALL_SYMBOLS = _CALL_SYMBOLS - {
    "JoinEdge",
    "normalized_population_participation",
    "named_query_population_manifest_matches",
    "_full_refresh_aggregate_locked",
    "_incremental_refresh_aggregate_locked",
}


# Keep this list exhaustive for the production routes that can choose a FROM
# graph.  Functions which delegate to a wired helper name the delegate in
# ``required_symbols``; the guard does not pretend to follow arbitrary calls.
CONSUMER_INVENTORY: tuple[ConsumerSpec, ...] = (
    ConsumerSpec(
        "services/query-router/src/rewrite/table_resolution.py",
        "_resolve_required_and_base_tables",
        ("augment_required_table_ids",),
        "source semantic table closure",
    ),
    ConsumerSpec(
        "services/query-router/src/rewrite/source_sql.py",
        "_build_no_columns_sql",
        ("augment_required_table_ids",),
        "source COUNT/constant population",
    ),
    ConsumerSpec(
        "services/query-router/src/rewrite/source_sql.py",
        "_build_source_sql",
        ("_resolve_required_and_base_tables",),
        "source projected route delegates to table resolution",
    ),
    ConsumerSpec(
        "services/query-router/src/rewrite/source_sql.py",
        "_substitute_table_names",
        ("_population_star_from_clause",),
        "unrestricted SELECT * population closure",
    ),
    ConsumerSpec(
        "services/query-router/src/rewrite/source_sql.py",
        "_build_persona_star_sql",
        ("augment_required_table_ids",),
        "persona/CLS star population closure",
    ),
    ConsumerSpec(
        "services/query-router/src/rewrite/raw_sql.py",
        "rewrite_for_raw",
        (
            "augment_required_table_ids",
            "population_defining_join_rows",
            "population_defining_table_ids",
        ),
        "ungrouped/raw source route",
    ),
    ConsumerSpec(
        "shared/semantic/sql_builder.py",
        "build_from_clause",
        ("augment_required_table_ids",),
        "scheduler/materialization shared FROM builder",
    ),
    ConsumerSpec(
        "services/optimizer/src/lifecycle/creator.py",
        "_build_source_from_clause",
        ("build_from_clause",),
        "optimizer aggregate creation delegates to shared builder",
    ),
    ConsumerSpec(
        "services/optimizer/src/lifecycle/creator.py",
        "_narrow_effective_measures_to_query_population",
        ("aggregate_plan_upper_bound", "population_defining_table_ids"),
        "optimizer candidate population bound",
    ),
    ConsumerSpec(
        "services/scheduler/src/jobs/full_refresh.py",
        "full_refresh_aggregate",
        ("_full_refresh_aggregate_locked",),
        "scheduler full materialization wrapper",
    ),
    ConsumerSpec(
        "services/scheduler/src/jobs/full_refresh.py",
        "_full_refresh_aggregate_locked",
        ("_build_from_clause",),
        "scheduler full materialization delegates to shared builder",
    ),
    ConsumerSpec(
        "services/scheduler/src/jobs/incremental_refresh.py",
        "incremental_refresh_aggregate",
        ("_incremental_refresh_aggregate_locked",),
        "scheduler incremental materialization wrapper",
    ),
    ConsumerSpec(
        "services/scheduler/src/jobs/incremental_refresh.py",
        "_incremental_refresh_aggregate_locked",
        ("_build_from_clause",),
        "scheduler incremental materialization delegates to shared builder",
    ),
    ConsumerSpec(
        "services/query-router/src/routing/aggregate_population.py",
        "aggregate_population_proven",
        ("population_defining_table_ids",),
        "aggregate candidate/admission proof",
    ),
    ConsumerSpec(
        "services/query-router/src/routing/pocket_population.py",
        "population_proven",
        ("population_defining_table_ids",),
        "pocket build/proof contract",
    ),
    ConsumerSpec(
        "services/query-router/src/routing/pocket_matcher.py",
        "_edges_from_rows",
        ("JoinEdge", "normalized_population_participation"),
        "snapshot/live graph loader carries the normalized participation field",
    ),
    ConsumerSpec(
        "services/query-router/src/rewrite/joins.py",
        "_build_joined_from_clause",
        ("required_table_ids",),
        "source renderer consumes the table-resolution closure",
    ),
    ConsumerSpec(
        "services/query-router/src/routing/aggregate_matcher.py",
        "_population_proven_for",
        ("_population",),
        "aggregate admission wrapper",
    ),
    ConsumerSpec(
        "shared/named_query/population_contract.py",
        "named_query_population_fingerprint",
        ("NQ_POPULATION_CONTRACT_VERSION",),
        "NQ artifact contract fingerprint",
    ),
    ConsumerSpec(
        "shared/named_query/refresh.py",
        "_get_rewritten_sql",
        ("NQ_CANONICAL_FORCE_ROUTE",),
        "NQ refresh wrapper routes through /explain source compile",
    ),
    ConsumerSpec(
        "services/query-router/src/routing/named_query_generation_guard.py",
        "assert_named_query_route_admissible",
        ("named_query_population_manifest_matches",),
        "NQ serve admission wrapper",
    ),
    ConsumerSpec(
        "services/query-router/src/api/routes.py",
        "_handle_explain",
        ("bind_query_to_model",),
        "explain consumer of the ordinary source/aggregate/pocket router",
    ),
    ConsumerSpec(
        "shared/pocket/refresh.py",
        "_get_rewritten_sql",
        ("_ROW_SECURITY_WRAP_MARKER",),
        "pocket refresh wrapper delegates to /explain",
    ),
)

DISCOVERY_NAME_FRAGMENTS = (
    "build_from_clause",
    "_build_source_from_clause",
    "_build_joined_from_clause",
    "rewrite_for_raw",
    "_build_no_columns_sql",
    "population_proven",
    "full_refresh_aggregate",
    "incremental_refresh_aggregate",
    "_get_rewritten_sql",
    "_handle_explain",
)

_EXCLUDED_PARTS = frozenset({
    ".git", ".venv", "venv", "site-packages", "__pycache__",
})
_NON_CONSUMER_CONTRACT_IMPORTS = frozenset({
    # Migration metadata imports the contract version to compose its rebuild
    # reason; it does not build or serve a FROM graph.  Its executable state
    # transition is covered by the migration integration proof.
    "shared/db/migrations/versions/0218_join_population_serving_contract_v1_reset.py",
})


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _function_nodes(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _qualified_name(node: ast.AST) -> str | None:
    """Return a static dotted name, or ``None`` for dynamic syntax."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _import_aliases(tree: ast.AST) -> tuple[dict[str, str], list[str]]:
    """Resolve only static imports used by the explicit consumer inventory.

    The second return value records unknown contract import shapes.  A star
    import or a dynamic module alias cannot be proved by this guard and must
    therefore fail closed rather than being silently treated as covered.
    """
    aliases: dict[str, str] = {}
    unknown: list[str] = []
    contract_modules = {
        "shared.semantic.sql_builder",
        "shared.semantic.join_population_serving",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in contract_modules:
            for imported in node.names:
                if imported.name == "*":
                    unknown.append(f"{node.module}: star import")
                    continue
                aliases[imported.asname or imported.name] = imported.name
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in contract_modules:
                    aliases[imported.asname or imported.name.rsplit(".", 1)[-1]] = imported.name
    return aliases, unknown


def _imports_contract(tree: ast.AST) -> bool:
    aliases, unknown = _import_aliases(tree)
    return bool(aliases or unknown)


def _resolved_call_name(call: ast.Call, aliases: Mapping[str, str]) -> str | None:
    raw = _qualified_name(call.func)
    if raw is None:
        return None
    head, *tail = raw.split(".")
    resolved_head = aliases.get(head, head)
    if tail:
        # ``import shared.semantic.sql_builder as builder`` followed by
        # ``builder.build_from_clause`` is supported; all other dynamic or
        # nested forms are intentionally not inferred.
        if resolved_head.startswith("shared.semantic."):
            return tail[-1]
        return tail[-1]
    return resolved_head.rsplit(".", 1)[-1]


def _parent_map(node: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _target_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }


def _has_load(node: ast.AST, names: set[str], *, skip: ast.AST) -> bool:
    for child in ast.walk(node):
        if child is skip:
            continue
        if isinstance(child, ast.Name) and child.id in names and isinstance(child.ctx, ast.Load):
            return True
    return False


_VALIDATION_CALLS = frozenset({
    "all", "any", "isdisjoint", "issubset", "issuperset", "matches", "validate",
})

# Only these explicit serving/render calls prove that the contract value has
# reached a consumer.  Generic observations (``tuple(value)``, ``set(value)``,
# logging, metrics, and arbitrary helper calls) are intentionally excluded.
# The transformation entries are the small, governed graph operations whose
# result is subsequently consumed by one of the serving calls.
_SEMANTIC_SINK_CALLS = frozenset({
    "build_from_clause",
    "_build_from_clause",
    "_build_joined_from_clause",
    "_resolve_required_and_base_tables",
    "_try_join",
    "postgres_ddl.build_pg_ctas",
    "build_pg_ctas",
    "_transpile_ddl",
    "population_proven",
    "aggregate_population_proven",
    "route_query",
})
_SEMANTIC_TRANSFORM_CALLS = frozenset({
    "_join_closure",
    "aggregate_plan_upper_bound",
    "population_defining_join_rows",
    "population_defining_table_ids",
    "_population_star_from_clause",
})


def _node_position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", -1), getattr(node, "col_offset", -1))


def _is_validation_only_use(
    load: ast.Name,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    """Exclude ordered uses which only validate a contract result."""
    parent = parents.get(load)
    if isinstance(parent, ast.Call):
        raw_name = _qualified_name(parent.func)
        terminal = raw_name.rsplit(".", 1)[-1] if raw_name else None
        if terminal in (_SEMANTIC_SINK_CALLS | _SEMANTIC_TRANSFORM_CALLS):
            return False
    if isinstance(parent, ast.Compare):
        return True
    if isinstance(parent, ast.Attribute):
        call = parents.get(parent)
        if isinstance(call, ast.Call) and call.func is parent:
            return getattr(parent, "attr", "") in _VALIDATION_CALLS
    current: ast.AST | None = load
    while current is not None:
        parent = parents.get(current)
        if isinstance(parent, (ast.If, ast.While)) and parent.test is current:
            return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            break
        current = parent
    return False


def _is_semantic_sink(
    load: ast.Name,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    """Return whether an ordered use feeds a serving/render plan sink.

    A generic call or container construction is not evidence of serving: it
    may merely observe the value before a later overwrite.  Only the explicit
    serving calls and governed graph transformations below qualify.
    """
    if _is_validation_only_use(load, parents):
        return False
    parent = parents.get(load)
    if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        return False  # alias propagation is followed by the caller
    if isinstance(parent, ast.Return):
        return True
    current: ast.AST | None = parent
    while isinstance(current, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        current = parents.get(current)
    if isinstance(current, ast.Return):
        return True
    call: ast.Call | None = None
    if isinstance(current, ast.Call):
        call = current
    elif isinstance(current, ast.keyword):
        candidate = parents.get(current)
        if isinstance(candidate, ast.Call):
            call = candidate
    if call is None:
        return False
    raw_name = _qualified_name(call.func)
    terminal = raw_name.rsplit(".", 1)[-1] if raw_name else None
    return terminal in (_SEMANTIC_SINK_CALLS | _SEMANTIC_TRANSFORM_CALLS)


def _ordered_value_reaches_sink(
    function_node: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
    name: str,
    after: tuple[int, int],
    *,
    seen: set[tuple[str, tuple[int, int]]],
) -> bool:
    """Follow one enumerated value until a sink or an ordered reassignment."""
    if not name or (name, after) in seen:
        return False
    seen.add((name, after))
    events = sorted(
        (
            child for child in ast.walk(function_node)
            if isinstance(child, ast.Name)
            and child.id == name
            and _node_position(child) > after
        ),
        key=_node_position,
    )
    for event in events:
        parent = parents.get(event)
        if isinstance(event.ctx, ast.Store):
            # The target store belonging to the alias assignment is the
            # propagation edge itself.  Skip it; a later store is a genuine
            # overwrite and must still kill the proof.
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                if isinstance(parent, ast.AugAssign):
                    continue
                # The original assignment target and a later simple alias
                # target are propagation edges. A value-producing call or a
                # literal has no source Name and therefore remains a kill.
                value = getattr(parent, "value", None)
                has_source_name = isinstance(value, ast.Name) and isinstance(
                    value.ctx, ast.Load
                )
                if _node_position(parent) == after or has_source_name:
                    continue
            return False  # the contract value was killed before consumption
        if _is_validation_only_use(event, parents):
            continue
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = _target_names(parent)
            if targets:
                return any(
                    _ordered_value_reaches_sink(
                        function_node, parents, target, _node_position(parent), seen=seen,
                    )
                    for target in targets
                )
            return False
        # A value can pass through a bounded expression used to derive the
        # next serving-plan variable (set algebra in the raw route, for
        # example).  Follow only expression nodes into an assignment; a Call
        # is deliberately excluded so ``tuple(value)`` remains a vacuous
        # observation rather than a propagation edge.
        current: ast.AST | None = parent
        while isinstance(current, (
            ast.BinOp, ast.BoolOp, ast.UnaryOp, ast.IfExp, ast.Compare,
            ast.Set, ast.List, ast.Tuple, ast.Dict, ast.SetComp, ast.ListComp,
            ast.DictComp, ast.GeneratorExp, ast.Starred, ast.FormattedValue,
            ast.JoinedStr,
        )):
            current = parents.get(current)
        if isinstance(current, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = _target_names(current)
            if targets:
                return any(
                    _ordered_value_reaches_sink(
                        function_node, parents, target, _node_position(current), seen=seen,
                    )
                    for target in targets
                )
            return False
        # A non-sink observation (including tuple/list constructors) is not
        # proof by itself, but it also does not terminate the value's ordered
        # def-use walk.  Continue looking for the genuine serving sink; any
        # intervening Store above has already killed the proof.
        if _is_semantic_sink(event, parents):
            return True
    return False


def _call_has_result_flow(
    call: ast.Call,
    function_node: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
    symbol: str,
) -> bool:
    """Whether a contract call reaches a consumer-visible value.

    An assignment is accepted only when the assigned value is subsequently
    loaded.  This rejects both a bare/dead call and a discarded-result
    mutation.  Predicate calls (manifest matching) and constructor-field calls
    are accepted when their enclosing expression consumes them.
    """
    parent = parents.get(call)
    while isinstance(parent, (ast.Await, ast.Yield, ast.YieldFrom)):
        parent = parents.get(parent)
    if parent is None:
        return False
    if isinstance(parent, ast.Expr):
        return False
    if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        target_names = _target_names(parent)
        if symbol in _SEMANTIC_TRANSFORM_CALLS:
            return any(
                _ordered_value_has_governed_use(
                    function_node, parents, target, _node_position(parent), seen=set(),
                )
                for target in target_names
            )
        return any(
            _ordered_value_reaches_sink(
                function_node,
                parents,
                target,
                _node_position(parent),
                seen=set(),
            )
            for target in target_names
        )
    if symbol in _ASSIGNMENT_CALL_SYMBOLS:
        # Climb through await/parentheses represented by AST expression nodes.
        # A direct call argument is not a sufficient proof for a builder result:
        # known builders must first expose their result to the caller's plan.
        return False
    # Constructor values, boolean predicates and return values are propagated
    # by their enclosing expression (e.g. ``edges.append(JoinEdge(...))`` or
    # ``if not named_query_population_manifest_matches(...):``).
    return isinstance(parent, (ast.Call, ast.keyword, ast.Return, ast.Compare, ast.UnaryOp, ast.BoolOp))


def _ordered_value_has_governed_use(
    function_node: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
    name: str,
    after: tuple[int, int],
    *,
    seen: set[tuple[str, tuple[int, int]]],
) -> bool:
    """Check that a known graph transform is used after its assignment.

    These few transforms expose graph facts that are consumed through local
    set algebra or a loop before rendering.  They are governed contract
    operations, so a live non-validation use is sufficient; arbitrary calls
    never enter this path.  Ordered stores still kill the result unless the
    assignment is a direct alias.
    """
    if not name or (name, after) in seen:
        return False
    seen.add((name, after))
    events = sorted(
        (
            child for child in ast.walk(function_node)
            if isinstance(child, ast.Name)
            and child.id == name
            and _node_position(child) > after
        ),
        key=_node_position,
    )
    for event in events:
        parent = parents.get(event)
        if isinstance(event.ctx, ast.Store):
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = getattr(parent, "value", None)
                if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load):
                    continue
            return False
        if _is_validation_only_use(event, parents):
            continue
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = _target_names(parent)
            if targets:
                return any(
                    _ordered_value_has_governed_use(
                        function_node, parents, target, _node_position(parent), seen=seen,
                    )
                    for target in targets
                )
            return False
        return True
    return False


def _load_references(node: ast.AST, symbol: str) -> list[ast.Name]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
        and child.id == symbol
        and isinstance(child.ctx, ast.Load)
    ]


def _audit_function(
    spec: ConsumerSpec,
    node: ast.AST,
    tree: ast.AST,
) -> list[str]:
    aliases, unknown_imports = _import_aliases(tree)
    problems = [
        f"{spec.path}:{spec.function}: unknown contract import shape {item}"
        for item in unknown_imports
    ]
    parents = _parent_map(node)
    calls_by_symbol: dict[str, list[ast.Call]] = {symbol: [] for symbol in spec.required_symbols}
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        resolved = _resolved_call_name(child, aliases)
        raw = _qualified_name(child.func)
        raw_terminal = raw.rsplit(".", 1)[-1] if raw else None
        for expected in calls_by_symbol:
            # Match both the imported canonical name (``build_from_clause``)
            # and the local alias used at the call site
            # (``_build_from_clause``).  This is still AST identity, not a
            # substring search, and unknown/dynamic callables remain absent.
            if expected in {resolved, raw_terminal}:
                calls_by_symbol[expected].append(child)

    for symbol in spec.required_symbols:
        calls = calls_by_symbol[symbol]
        if symbol in _CALL_SYMBOLS:
            if not calls:
                problems.append(
                    f"{spec.path}:{spec.function}: required AST call {symbol} "
                    f"is missing ({spec.note})"
                )
                continue
            for call in calls:
                if not _call_has_result_flow(call, node, parents, symbol):
                    problems.append(
                        f"{spec.path}:{spec.function}: {symbol} call result is "
                        f"not propagated ({spec.note})"
                    )
        elif not _load_references(node, symbol):
            problems.append(
                f"{spec.path}:{spec.function}: required value {symbol} is not "
                f"loaded ({spec.note})"
            )
    return problems


def audit_consumer_inventory(
    repository_root: Path | None = None,
    *,
    source_overrides: Mapping[str, str] | None = None,
) -> list[str]:
    """Return fail-closed inventory violations (empty means covered).

    ``source_overrides`` is a test seam: mutation tests can remove a contract
    call without editing the checkout and must then observe a violation.
    """
    root = repository_root or _root()
    overrides = source_overrides or {}
    problems: list[str] = []
    indexed_paths = {spec.path for spec in CONSUMER_INVENTORY}

    for spec in CONSUMER_INVENTORY:
        path = root / spec.path
        try:
            source = overrides.get(spec.path, path.read_text(encoding="utf-8"))
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:
            problems.append(f"{spec.path}: cannot parse inventory consumer: {exc}")
            continue
        node = _function_nodes(tree).get(spec.function)
        if node is None:
            problems.append(f"{spec.path}: missing inventoried function {spec.function}")
            continue
        problems.extend(_audit_function(spec, node, tree))

    # Alias/import discovery: a new production import of one of the shared
    # contract primitives must be inventoried instead of hiding behind an
    # unclassified wrapper or re-export.  Tests are excluded because they are
    # evidence, not production consumers.
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if _EXCLUDED_PARTS.intersection(path.parts):
            continue
        if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
            continue
        try:
            source = overrides.get(rel, path.read_text(encoding="utf-8"))
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:
            problems.append(f"{rel}: cannot parse production consumer scan: {exc}")
            continue
        if rel not in indexed_paths and rel not in _NON_CONSUMER_CONTRACT_IMPORTS and (
            _imports_contract(tree)
        ):
            problems.append(
                f"{rel}: shared G3 builder/contract import is not in "
                "CONSUMER_INVENTORY"
            )

    # Name-based discovery is deliberately limited to a known production
    # surface.  If a new builder-shaped function appears there without a
    # corresponding inventory row, fail closed; arbitrary future syntax is
    # outside this guard's claim and remains a review obligation.
    for directory in (root / "services", root / "shared"):
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if _EXCLUDED_PARTS.intersection(path.parts):
                continue
            if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError) as exc:
                problems.append(f"{rel}: cannot parse builder discovery: {exc}")
                continue
            for name in _function_nodes(tree):
                if not any(fragment in name for fragment in DISCOVERY_NAME_FRAGMENTS):
                    continue
                if not any(spec.path == rel and spec.function == name for spec in CONSUMER_INVENTORY):
                    problems.append(f"{rel}: unclassified builder-shaped function {name}")
    return problems
