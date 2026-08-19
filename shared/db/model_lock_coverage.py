"""API-handler LINT for the per-model advisory lock (NOT the proof of coverage).

WHAT THIS IS, AND WHAT IT IS NOT (Bug-7982 R7, findings 3+4)
-----------------------------------------------------------
Read ``shared/db/model_write_lock_guard.py`` first. Three consecutive external
cross-family gates disproved three successive versions of this module's claim to
PROVE that every write is lock-covered:

  R3   substring match          -> a comment containing the call defeated it
  R4/5 regex, then AST presence -> a docstring defeated it; then dead code
  R6   AST + receiver-name set  -> ``stmt = update(...); db.execute(stmt)`` and
                                   ``db.execute(delete(T).where(...))`` and an
                                   unrecognised session variable name defeated it
  R6   route-derived discovery  -> structurally blind to a NON-route writer

That is one root cause with two faces: proving coverage by recognising code
SHAPES silently passes every unrecognised shape, and enumerating writers by
guessing where they live is blind to writers that live elsewhere. Both biases
produce a false PASS. The property is dynamic and is now asserted at RUNTIME, at
the cursor, by ``model_write_lock_guard`` — where no code shape can evade it.

This module's remaining, honest job is a LINT with a narrower contract:

  * scope — model-scoped (``{model_id}``) mutating FastAPI routes only. It makes
    NO claim about writers that are not registered routes;
  * it checks something the runtime guard structurally cannot: that the lock is
    bound to THIS endpoint's own ``model_id`` parameter, is reachable, dominates
    the handler body, and is taken on the same session the writes use. SQL text
    at the cursor carries no model identity, so this remains static work;
  * its write detection is now CONSERVATIVE — anything it cannot prove is a read
    counts as a write. A shape it does not understand therefore produces a
    FALSE FAILURE (a human looks, and either fixes the handler or allow-lists it
    with a reason), never a false pass. That inversion is the point: the failure
    direction, not the pattern coverage, is what three gates kept punishing.

Each service's ``tests/test_model_lock_coverage.py`` imports this engine, points
it at its own ``src.api`` package, and supplies its own reasoned allow-list.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
import textwrap

from fastapi import APIRouter

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# ORM operations that WRITE, on a DB session receiver. ``.execute`` is excluded
# from the bare set because a SELECT read also uses it; a write via ``.execute``
# is detected by inspecting the statement passed to it.
_WRITE_METHODS = {"add", "add_all", "delete", "merge", "commit", "flush"}
_WRITE_STMT_FUNCS = {"insert", "update", "delete", "pg_insert"}
# Statement constructors that are PROVABLY reads. Everything not on this list is
# treated as a write (see ``_execute_arg_is_write``) — the conservative
# inversion of R6's "recognise known write shapes" bias.
_READ_STMT_FUNCS = {"select", "exists", "union", "union_all"}
_LOCK_FN = "acquire_model_definition_lock"


# ---------------------------------------------------------------------------
# Structural route discovery
# ---------------------------------------------------------------------------

def discover_api_modules(api_pkg) -> tuple[list, list[str]]:
    """Import every submodule of ``api_pkg``. Returns (modules, import_failures).

    An import failure is surfaced, not swallowed — a writer module that fails to
    import would otherwise silently vanish from coverage.
    """
    mods = []
    failures: list[str] = []
    pkg_name = api_pkg.__name__
    for mi in pkgutil.iter_modules(api_pkg.__path__):
        try:
            mods.append(importlib.import_module(f"{pkg_name}.{mi.name}"))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{pkg_name}.{mi.name}: {exc!r}")
    return mods, failures


def model_scoped_mutating_endpoints(api_pkg, modules=None) -> list[tuple[str, str, object]]:
    """Every model-scoped (``{model_id}``) mutating endpoint across ALL routers of
    the given (or all discovered) modules, as a LIST of ``(module, fn_name, fn)``.

    A LIST (not a name-keyed dict) so two modules sharing a function name are BOTH
    checked — a name key would silently drop one.
    """
    if modules is None:
        modules, _ = discover_api_modules(api_pkg)
    out: list[tuple[str, str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    for mod in modules:
        for _n, obj in inspect.getmembers(mod):
            if not isinstance(obj, APIRouter):
                continue
            for route in obj.routes:
                methods = getattr(route, "methods", set()) or set()
                path = getattr(route, "path", "")
                if "{model_id}" in path and (methods & MUTATING_METHODS):
                    fn = route.endpoint
                    key = (getattr(fn, "__module__", "?"), fn.__name__, id(fn))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append((getattr(fn, "__module__", "?"), fn.__name__, fn))
    return out


# ---------------------------------------------------------------------------
# Effectiveness (AST)
# ---------------------------------------------------------------------------

def _funcdef(fn):
    return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]


def _reachable_statements(funcdef):
    """Yield AST nodes inside ``funcdef``'s BODY in source order, descending into
    control flow but NOT into nested function defs, NOT into a literal
    ``if False:`` dead branch, and NOT into the function's own decorators."""
    root = funcdef

    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not root:
            return
        if isinstance(node, ast.If):
            is_dead = isinstance(node.test, ast.Constant) and node.test.value is False
            yield from walk(node.test)
            if not is_dead:
                for c in node.body:
                    yield c
                    yield from walk(c)
            for c in node.orelse:
                yield c
                yield from walk(c)
            return
        for _field, value in ast.iter_fields(node):
            items = value if isinstance(value, list) else [value]
            for child in items:
                if not isinstance(child, ast.AST):
                    continue
                yield child
                yield from walk(child)

    for stmt in root.body:
        yield stmt
        yield from walk(stmt)


def _unconditional_lock_linenos(funcdef) -> set[int]:
    """Linenos of lock calls that DOMINATE the body: at the function top level or
    inside only the unconditional wrappers (session ``async for`` loop, with-
    blocks), never inside an if/try/while/generic-for."""
    root = funcdef
    found: set[int] = set()

    def _is_session_loop(node) -> bool:
        it = getattr(node, "iter", None)
        f = it.func if isinstance(it, ast.Call) else None
        return (getattr(f, "id", None) or getattr(f, "attr", None)) == "get_tenant_db"

    def walk_body(body):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk_body(stmt.body)
                continue
            if isinstance(stmt, ast.AsyncFor) and _is_session_loop(stmt):
                walk_body(stmt.body)
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await):
                call = stmt.value.value
                if isinstance(call, ast.Call):
                    nm = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                    if nm == _LOCK_FN:
                        found.add(call.lineno)
            # Do NOT descend into If/Try/While/for-non-session bodies.

    walk_body(root.body)
    return found


def _lock_calls(funcdef, model_param: str | None):
    """Reachable lock calls: (lineno, arg_ok, dominating, session_arg) where
    session_arg is the Name of the lock's FIRST positional arg (the session)."""
    dominating = _unconditional_lock_linenos(funcdef)
    out = []
    for node in _reachable_statements(funcdef):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != _LOCK_FN:
            continue
        arg_ok = False
        if model_param is not None and len(node.args) >= 2:
            second = node.args[1]
            arg_ok = isinstance(second, ast.Name) and second.id == model_param
        session_arg = (
            node.args[0].id if node.args and isinstance(node.args[0], ast.Name) else None
        )
        out.append((node.lineno, arg_ok, node.lineno in dominating, session_arg))
    return out


def _chain_root(node):
    """Unwrap a builder chain to the call that STARTED it.

    ``delete(T).where(x).returning(y)`` -> the ``delete(T)`` Call. R6's checker
    only inspected the OUTERMOST call, so every chained builder — the ordinary
    way SQLAlchemy statements are written — read as "not a write".
    """
    seen = 0
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        node = node.func.value
        seen += 1
        if seen > 64:  # pathological/degenerate source; stop rather than spin
            return None
    return node if isinstance(node, ast.Call) else None


def _statement_assignments(funcdef) -> dict[str, ast.AST]:
    """Map local name -> last value assigned to it inside this function.

    Resolves the ``stmt = update(T).values(...)`` / ``await db.execute(stmt)``
    shape that R6's checker read as "not a write" because it only looked at the
    expression handed to ``.execute`` directly.
    """
    out: dict[str, ast.AST] = {}
    for node in _reachable_statements(funcdef):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name) and node.value is not None:
                out[node.target.id] = node.value
    return out


#: Types a receiver can be RESOLVED to that prove it is not a DB session.
_PLAIN_COLLECTIONS = (set, frozenset, list, dict, tuple)


def _non_session_names(funcdef, globals_map: dict | None = None) -> set[str]:
    """Names PROVABLY bound to a plain Python collection.

    Only these are excused from the write check. R6 did the inverse — it
    recognised a hardcoded set of session names (``db``/``tenant_db``/...) and
    ignored everything else, so a session held in a variable with any other name
    was invisible. Inverting it means an unfamiliar receiver is treated as a
    session (conservative), and only a DEMONSTRABLE collection is excluded.

    Two evidence sources, both actual resolution rather than name matching:
    a local ``seen = set()`` in this function, and — when the caller supplies the
    function's own module globals — a module-level binding whose live value is a
    plain collection (e.g. ``_GLOSSARY_BOOTSTRAP_TASKS: set[asyncio.Task]``).
    """
    out: set[str] = set()
    for name, value in _statement_assignments(funcdef).items():
        if isinstance(value, (ast.Set, ast.List, ast.Dict, ast.Tuple,
                              ast.SetComp, ast.ListComp, ast.DictComp)):
            out.add(name)
        elif isinstance(value, ast.Call):
            ctor = getattr(value.func, "id", None)
            if ctor in {"set", "list", "dict", "tuple", "frozenset",
                        "defaultdict", "Counter", "deque"}:
                out.add(name)
    if globals_map:
        for name, value in globals_map.items():
            if isinstance(value, _PLAIN_COLLECTIONS):
                out.add(name)
    return out


def _text_literal_is_write(arg: ast.Call) -> bool:
    targs = getattr(arg, "args", [])
    lit = targs[0].value if targs and isinstance(targs[0], ast.Constant) else None
    if not isinstance(lit, str):
        # A non-literal ``text(...)`` cannot be proven to be a read.
        return True
    return bool(
        re.search(r"\b(insert|update|delete|truncate|merge)\b", lit, re.IGNORECASE)
    )


def _execute_arg_is_write(arg, assignments: dict[str, ast.AST], depth: int = 0) -> bool:
    """CONSERVATIVE: True unless the argument is PROVABLY a read.

    This is the inversion that matters. R6 asked "does this look like one of the
    write shapes I know?" and answered False for everything else, so every
    unrecognised shape silently passed. This asks "can I prove this is a read?"
    and answers True (write) for everything else, so an unrecognised shape fails
    loudly and a human decides.
    """
    if depth > 8 or arg is None:
        return True
    if isinstance(arg, ast.Name):
        target = assignments.get(arg.id)
        # A name we cannot resolve inside this function is not provably a read.
        return True if target is None else _execute_arg_is_write(
            target, assignments, depth + 1
        )
    if isinstance(arg, ast.Await):
        return _execute_arg_is_write(arg.value, assignments, depth + 1)
    if isinstance(arg, ast.Call):
        root = _chain_root(arg) or arg
        rname = getattr(root.func, "id", None) or getattr(root.func, "attr", None)
        if rname in _READ_STMT_FUNCS:
            return False
        if rname in _WRITE_STMT_FUNCS:
            return True
        if rname == "text":
            return _text_literal_is_write(root)
        return True
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return bool(
            re.search(
                r"\b(insert|update|delete|truncate|merge)\b", arg.value, re.IGNORECASE
            )
        )
    return True


def _first_write(funcdef, globals_map: dict | None = None):
    """Return (lineno, session_name) of the first DB write, or (None, None).

    A write is ``<recv>.<add|add_all|delete|merge|commit|flush>()`` or
    ``<recv>.execute(<not provably a read>)``, where ``<recv>`` is any name not
    provably bound to a plain Python collection.

    Remaining limitation, stated honestly: a write performed indirectly through a
    module-local helper is still not followed here (doing so needs
    branch-sensitive analysis to avoid false-positiving return-early operational
    writes such as glossary bootstrap's job creation). That gap is NOT closed by
    making this smarter — it is closed by the runtime guard in
    ``model_write_lock_guard.py``, which sees the write wherever it is issued.
    """
    assignments = _statement_assignments(funcdef)
    excluded = _non_session_names(funcdef, globals_map)
    best: tuple[int, str | None] | None = None
    for node in _reachable_statements(funcdef):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
            continue
        recv = f.value.id
        if recv in excluded:
            continue
        is_write = False
        if f.attr in _WRITE_METHODS:
            is_write = True
        elif f.attr == "execute":
            is_write = _execute_arg_is_write(
                node.args[0] if node.args else None, assignments
            )
        if is_write and (best is None or node.lineno < best[0]):
            best = (node.lineno, recv)
    return best if best is not None else (None, None)


def effective_from_funcdef(funcdef, globals_map: dict | None = None) -> tuple[bool, str]:
    params = {a.arg for a in funcdef.args.args + funcdef.args.kwonlyargs}
    model_param = "model_id" if "model_id" in params else None
    locks = _lock_calls(funcdef, model_param)
    if not locks:
        return False, "no reachable acquire_model_definition_lock call"
    good = [(ln, sess) for ln, ok, dom, sess in locks if ok and dom]
    if not good:
        if not any(ok for _ln, ok, _dom, _s in locks):
            return False, "lock call's 2nd arg is not the endpoint's own model_id parameter"
        return False, (
            "the lock call is nested inside a conditional/try/loop and does not "
            "dominate the handler's writes (reviewer #5)"
        )
    first_write_ln, write_sess = _first_write(funcdef, globals_map)
    # Reviewer #2: the lock must be on the SAME session the writes use, and it
    # must precede the first write.
    if first_write_ln is not None and write_sess is not None:
        good = [(ln, sess) for ln, sess in good if sess is None or sess == write_sess]
        if not good:
            return False, (
                f"the lock is acquired on a different session than the writes "
                f"(writes use '{write_sess}') — it serialises nothing"
            )
    first_lock = min(ln for ln, _sess in good)
    if first_write_ln is not None and first_lock > first_write_ln:
        return False, (
            f"lock acquired at line {first_lock} AFTER the first ORM write at "
            f"line {first_write_ln} — it does not dominate the mutation"
        )
    return True, "ok"


def lock_is_effective(fn) -> tuple[bool, str]:
    # Pass the function's own module globals so a receiver that is a module-level
    # plain collection (not a DB session) can be RESOLVED rather than guessed at.
    return effective_from_funcdef(_funcdef(fn), getattr(fn, "__globals__", None))


def effective_from_source(src: str) -> tuple[bool, str]:
    return effective_from_funcdef(ast.parse(textwrap.dedent(src)).body[0])
