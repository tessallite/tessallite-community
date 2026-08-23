"""The agent-service project-authorization gate contract, in one place.

Not a test module (pytest collects ``test_*.py`` only). This holds the
machinery two guard files share, because keeping it in one of them meant the
other had to import a test module, and because the whole family of defects
this code exists to prevent is "two things that had to agree drifted apart".

The contract has exactly two halves, and the ONLY reason it works is that both
halves are keyed on the same thing:

* ``test_project_route_authorization_coverage.py`` enumerates every route that
  addresses a project and requires each one to run a gate — CERTIFYING the
  route by RESOLVING the gate name the handler awaits and walking it to a
  primitive.
* ``test_bug_8445_8446_project_gate_consolidation.py`` enumerates every
  module-level binding of a gate name under ``src/`` and requires each one to
  reach a primitive too, so the names in ``PROJECT_SCOPED_GATES`` mean
  something before any route uses them.

Why "keyed on the same thing" is the load-bearing sentence
----------------------------------------------------------
Every round this saga has lost, it lost the same way: the two halves were
keyed differently, and the gap between the keys was the exploit.

* Round N: certification was keyed on a NAME, verification iterated a
  HARDCODED five-tuple of ``(module, gate)`` pairs. A gate in a module the
  tuple had never heard of was certified by a string and verified by nothing.
  An external reviewer shipped a wide-open route through all three guards.
* Round N+1: verification's domain was derived from the certification
  allowlist — the sets finally agreed — but certification was still keyed on
  the NAME while verification was keyed on the AST BINDING SITE. The names a
  handler can resolve at runtime are a strict superset of the module-scope AST
  bindings, so a gate name bound by a local alias, a function-scope import
  (the ordinary circular-import workaround), ``globals()[...] = ``, or
  ``setattr(sys.modules[__name__], ...)`` was certified and verified by
  nothing. Executed, not reasoned: four such routes passed both guard files.

Both halves are now keyed on the RESOLVED CALLABLE: whatever object the name
actually denotes, walked to a terminal primitive by ``module:qualname``
identity. There is no third key left to disagree with.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import pathlib
import sys
import textwrap

# Gate helpers that take the requested project and decide access. A handler
# satisfies the contract by awaiting one of these WITH ``project_id``.
#
# This is the certification allowlist. Adding a name here places a real
# obligation on the code: whatever the name resolves to, at a route's handler
# or at module scope under ``src/``, must reach a terminal primitive below.
#
# ``_enforce_project_scope`` is deliberately NOT here even though several
# handlers call it: it only narrows an EMBED token to its permitted projects
# and is a no-op for a regular tenant user (conversations.py:499-506). A route
# gated by that alone would be wide open to any tenant user, so accepting it
# would have made this guard endorse the very defect class it exists to catch.
# The conversation routes that call it also call
# ``_require_project_access_and_agent``, which is the real gate.
PROJECT_SCOPED_GATES = frozenset({
    "_require_project_modeller",
    "_require_project_viewer",
    "_require_blocked_original_access",
    "_authorize_refresh_derived",
    "_require_project_access_and_agent",
    "_require_webhook_project_access",
    # The service's CHAT tier (src/auth/project_access.py). Embed-aware — so it
    # is the correct gate for chat-capable surfaces that must stay reachable by
    # an embed token — and it refuses a service principal by type before
    # delegating.
    #
    # ``ensure_project_model_access`` USED TO BE LISTED HERE, and its removal is
    # Bug-8589 HALF A's structural half. The platform terminal admits a service
    # principal (correctly, under AUTH-RR-01: it assumes the ROUTE already
    # scope-authorized one). Both agent-service route families that named it
    # directly are gated by ``require_capability("chat")``, which has no service
    # branch, so the assumption was false and both were open. Listing the
    # terminal here is what made "this route is gated" mean two different things
    # inside one service. A route that awaits it directly is now simply NOT
    # certified — the route guard's fail-closed branch reports it ungated and a
    # human looks, which is the safe direction.
    "require_project_chat_access",
})

# The terminal authorization primitives, by ``module:qualname`` identity — NOT
# by bare name, for the same reason ``_DEPENDENCY_GATES`` is identity-keyed: a
# local no-op called ``require_project_role`` must not be able to terminate a
# delegation chain.
#
# There are two, and the second is not a loophole:
#   * ``src.auth.project_access.require_project_role`` — this service's ONE
#     binding lookup (Bug-8445), which every management/config/admin-content
#     wrapper delegates to.
#   * ``src.auth.project_access.require_project_chat_access`` — the CHAT tier.
#     The conversational surfaces must stay reachable by an embed token, which
#     ``require_project_role`` refuses by principal type, so this tier delegates
#     the human/embed decision to the platform-canonical, embed-aware
#     ``shared.auth.project_access.ensure_project_model_access``.
#
# WHY THE SECOND ENTRY IS A SERVICE-LOCAL TIER AND NOT THE PLATFORM TERMINAL
# ITSELF. It used to be the platform terminal. An Opus5 R2 gate read the two
# side by side and found three divergences, two of them literally Bug-8445's
# own drift axes 3 and 4 — so "this route is gated" meant two different things
# inside one service, and the difference was live (Bug-8589):
#
#   | axis              | require_project_role     | ensure_project_model_access |
#   |-------------------|--------------------------|-----------------------------|
#   | service principal | 403 by type (AUTH-RR-01) | admitted (``_is_service_user`` returns) |
#   | bootstrap-open    | REMOVED — a binding-less  | unconditional, and scoped   |
#   |                   | project denies every      | PER PROJECT (admits when the|
#   |                   | ordinary caller (F-021-04 | addressed project has zero  |
#   |                   | cutover, Bug-9442)        | bindings)                   |
#   | embed, project_ids| refused by type          | admitted for any project    |
#   | is None           |                          |                             |
#
# Row 1 is closed by construction now: BOTH accepted terminals refuse a
# ``CurrentServiceUser`` by type, so certification means one thing on the
# principal-type axis across the whole certified set (Bug-8589 HALF A). The
# platform terminal is deliberately NOT a terminal here any more — reaching it
# without going through a service-local tier no longer certifies anything, so a
# future gate cannot re-acquire the admit-a-service-principal posture by
# accident.
#
# Row 2 is now ASYMMETRIC, and the asymmetry is deliberate: F-021-04's HARD
# CUTOVER (Wave C decision #9, Bug-9442) removed the zero-binding bootstrap
# grant from ``require_project_role`` outright — a binding-less project denies
# every ordinary caller on every management/config/content surface, and only a
# human tenant/system admin repairs it. The SHARED platform terminal still
# bootstrap-opens per project (Bug-8589 HALF B, still open — owned by the
# shared/model-service RBAC cutover, which decision #9's "project/model access
# helpers" clause also targets), so the chat tier that delegates to it is now
# STRICTLY WEAKER than the management tier on this axis, not merely
# differently scoped. Row 3 (embed ``project_ids is None``) is likewise still
# open on the shared terminal. Both live behind exactly ONE agent-service gate
# instead of two route families that reached the terminal independently, so
# each is a one-place change when the shared lane decides it.
# See ``TestNonHumanPrincipalsFailClosed`` in
# ``test_bug_8445_8446_project_gate_consolidation.py``, which enumerates the
# principal axis across every discovered gate instead of one hand-picked caller.
TERMINAL_PRIMITIVES = frozenset({
    "src.auth.project_access:require_project_role",
    "src.auth.project_access:require_project_chat_access",
})

# Delegation chains in this service are 3 hops at most
# (``_require_webhook_project_access`` -> ``_require_blocked_original_access``
# -> ``require_project_role``). The bound exists so a cycle or a pathological
# chain fails CLOSED with a readable trace instead of recursing forever.
MAX_DELEGATION_DEPTH = 8

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
_SERVICE_ROOT = _SRC.parent


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def identity_of(call) -> str:
    """``module:qualname`` for a callable, the identity a gate is recognised
    by. Falls back to a shape that can never match an allowlist entry, so an
    unrecognisable callable fails closed."""
    module = getattr(call, "__module__", None)
    qualname = getattr(call, "__qualname__", None) or getattr(call, "__name__", None)
    if not module or not qualname:
        return f"<unidentifiable:{call!r}>"
    return f"{module}:{qualname}"


# ---------------------------------------------------------------------------
# "This call really runs, and it really receives the project"
# ---------------------------------------------------------------------------


def locally_bound_names(source: str) -> set[str]:
    """Names *source*'s own body binds, so a caller can REFUSE to resolve them.

    Name resolution in ``delegation_trace`` and ``resolved_gate_delegates``
    goes through the defining function's ``__globals__``. That is only the name
    Python would use if the function does not bind the name itself — and every
    one of the nine modules hosting a project route binds at least one
    certified gate name at module scope, so in a REAL module the global is the
    real gate while the function may be calling something else entirely.

    Executed proof (Opus5 R4 gate): a handler in a module carrying a genuine
    module-scope ``_require_project_viewer`` was CERTIFIED for
    ``_require_project_viewer = _wide_open`` in its body, for a function-scope
    ``from x import y as _require_project_viewer``, and for a ``for``-loop
    target of that name. The same shadowing applied to ``require_project_role``
    inside a gate made the gate's delegation trace read clean. Both halves were
    keyed identically and both were wrong identically.

    The counter-examples that were supposed to cover this used synthetic
    modules that did NOT bind the gate name, so resolution returned ``None``
    and they passed for the wrong reason.

    Deliberately over-broad — it collects every binding form (``Store``-context
    names, which covers assignment, augmented assignment, ``for`` targets,
    walrus, comprehension targets and ``with ... as``; function-scope imports;
    ``except ... as``; structural pattern captures; nested ``def``/``class``;
    parameters) and does not model scopes. Over-collecting causes a REFUSAL,
    which reds the build and brings a human; under-collecting causes a
    certification, which does not. No live handler or gate binds a certified
    name (verified: two greps over ``src/`` return zero), so the strictness
    costs nothing today.
    """
    tree = ast.parse(source)
    bound: set[str] = set()
    top_level_defs = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    # The scanned function's own name is a binding of the enclosing module, not
    # of its body; keeping it would refuse nothing useful and could confuse a
    # self-recursive helper.
    return bound - top_level_defs


def awaited_calls_receiving_project_id(source: str) -> set[str]:
    """Every bare-``Name`` call AWAITED in *source* that is passed ``project_id``
    AND is not rebound by *source* itself.

    Six properties, each closing a way an earlier draft could be fooled into
    reporting a gate that does not actually run:

    * the call must be wrapped in ``await`` — every gate is a coroutine, so an
      un-awaited call enforces nothing at runtime. This is the realistic
      accident, not a contrived one.
    * ``node.func`` must be a bare ``Name`` — ``some_obj._require_project_viewer(...)``
      is a different callable that merely shares a suffix.
    * the body of a nested ``def``/``async def``/``lambda`` is not searched —
      defining a checker and never calling it enforces nothing.
    * an ``if <falsy constant>:`` body is not searched.
    * ``project_id`` must be passed, positionally or by keyword. A gate that
      runs but is never told WHICH project is precisely the Bug-8356 shape.
    * the name must NOT be rebound by this function's own body — see
      :func:`locally_bound_names`. Resolution goes through module globals, so
      a function-scope rebinding makes the resolved callable a different object
      from the one that actually runs.

    Deliberately UNFILTERED by ``PROJECT_SCOPED_GATES``. The delegation walk
    depends on that: ``require_project_role`` is the terminal primitive and is
    correctly absent from the allowlist (no route is certified by it, because
    handlers never call it directly), so narrowing this walk would blind the
    walk to the only hop that matters. ``gate_calls_in_source`` applies the
    narrowing where certification needs it.

    Known limits, stated rather than papered over. Each needs real
    control-flow or data-flow analysis to decide, so they are documented
    instead of half-implemented; all fail in the PERMISSIVE direction, which
    is why they are written down here where the next reader will see them:

    * a gate placed after an unconditional early ``return`` on some path is
      still counted;
    * a gate awaited inside ``try: ... except HTTPException: pass`` is still
      counted, even though the swallow makes it non-enforcing;
    * a gate under ``if TYPE_CHECKING:`` or any other non-literal runtime
      condition (``if not skip_auth:``) is still counted — only a
      literal-falsy ``if`` test is pruned;
    * a handler that REBINDS ``project_id`` to a body-derived value before
      calling the gate is still counted as project-aware, while actually
      checking a different project than the path addresses. This one needs
      data-flow analysis, and it is the most dangerous of the four: it would
      look correct to a reader too.

    Shapes that fail in the SAFE direction (the route is reported ungated, so
    a human looks) need no special handling: a gate awaited via
    ``asyncio.gather`` or assigned to a variable first is simply not seen.
    """
    return awaited_calls_ignoring_rebinding(source) - locally_bound_names(source)


def awaited_calls_ignoring_rebinding(source: str) -> set[str]:
    """The same walk WITHOUT the rebinding refusal.

    Exposed for DIAGNOSTICS only — never for certification. It lets the
    route guard tell a developer "you await a gate but rebind its name"
    instead of the bare "this route has no gate", which is what stops the
    refusal being mistaken for a false positive and answered with an
    exemption entry.
    """
    return _scan_body(ast.parse(source))


def gate_calls_in_source(source: str) -> set[str]:
    """RECOGNISED gate names awaited in *source* and passed ``project_id``."""
    return awaited_calls_receiving_project_id(source) & PROJECT_SCOPED_GATES


def _scan_body(node) -> set[str]:
    found: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ) and not isinstance(node, ast.Module):
            continue  # a nested definition is not code that runs
        if isinstance(child, ast.If):
            test = child.test
            if isinstance(test, ast.Constant) and not test.value:
                for orelse in child.orelse:
                    found |= _scan_body(orelse)
                continue
        if isinstance(child, ast.Await) and isinstance(child.value, ast.Call):
            call = child.value
            if isinstance(call.func, ast.Name):
                args = list(call.args) + [kw.value for kw in call.keywords]
                if any(
                    isinstance(a, ast.Name) and a.id == "project_id" for a in args
                ):
                    found.add(call.func.id)
        found |= _scan_body(child)
    return found


# ---------------------------------------------------------------------------
# Delegation: does this callable actually reach an authorization primitive?
# ---------------------------------------------------------------------------


def delegation_trace(func, *, depth: int = 0, seen=None) -> tuple[bool, list[str]]:
    """Walk *func*'s awaited calls looking for a terminal primitive.

    Resolution is by RUNTIME identity, not by name: an awaited bare ``Name`` is
    looked up in the defining function's own ``__globals__`` and the resulting
    object's ``module:qualname`` is what gets compared. That is what makes a
    locally-defined impostor called ``require_project_role`` a dead end rather
    than a certification.

    Fails CLOSED on everything it cannot resolve — an unreadable source, a
    non-callable binding, an unresolvable name, a chain that runs past
    ``MAX_DELEGATION_DEPTH``. Two consequences worth naming so a future reader
    does not mistake them for bugs:

    * A DECORATED gate is refused outright rather than unwrapped. Following
      ``__wrapped__`` reads the undecorated body and never sees the wrapper's
      control flow, so a ``@functools.wraps`` decorator that returns early
      under some condition would present a clean delegation while skipping the
      gate at runtime (Opus5 R1 finding F2, proven by execution). No gate in
      this service is decorated today; refusing costs nothing now and forces a
      written decision if one appears.
    * A ``functools.partial`` of the real primitive is refused too, because it
      has no readable source and no stable identity. That is the safe
      direction (the build goes red, a human looks) and is deliberate, not an
      oversight.

    The trace is returned so the failure message names the exact hop that did
    not reach a primitive.
    """
    seen = set() if seen is None else seen
    if func is None:
        return False, ["<unbound>"]
    if not callable(func):
        return False, [f"<not callable: {func!r}>"]

    identity = identity_of(func)
    if getattr(func, "__wrapped__", None) is not None:
        return False, [
            f"{identity} <decorated: unwrapping would hide the wrapper's own "
            "control flow, so this fails closed by design>"
        ]
    if identity in TERMINAL_PRIMITIVES:
        return True, [identity]
    if identity in seen:
        return False, [f"{identity} <cycle>"]
    if depth >= MAX_DELEGATION_DEPTH:
        return False, [f"{identity} <max depth>"]
    seen = seen | {identity}

    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return False, [f"{identity} <source unavailable>"]

    namespace = getattr(func, "__globals__", {})
    dead_ends: list[str] = []
    for name in sorted(awaited_calls_receiving_project_id(source)):
        reached, trace = delegation_trace(
            namespace.get(name), depth=depth + 1, seen=seen
        )
        if reached:
            return True, [identity, *trace]
        dead_ends.append(f"{name}->[{' -> '.join(trace)}]")

    return False, [f"{identity} awaits {dead_ends or '<nothing project-aware>'}"]


def gate_does_not_delegate(module_name: str, gate_name: str, func) -> str | None:
    """``None`` if the gate reaches a terminal primitive, else the reason."""
    reached, trace = delegation_trace(func)
    if reached:
        return None
    return (
        f"{module_name}.{gate_name} never reaches an authorization primitive "
        f"({sorted(TERMINAL_PRIMITIVES)}); chain was: {' -> '.join(trace)}"
    )


def callable_is_a_project_gate(call) -> tuple[bool, list[str]]:
    """THE single accepting predicate: does *call* authorize the requested
    project?

    Two obligations, and they are the same two the handler-side check applies:

    * it reaches a terminal authorization primitive (``delegation_trace``);
    * it declares ``project_id``, so FastAPI injects the requested project into
      it. A gate that runs but is never told WHICH project is the original
      Bug-8356 shape, and the handler side has always required the argument to
      be passed — this is the dependency-side equivalent.

    Why this function exists at all
    -------------------------------
    ``_classify_ungated`` used to have TWO accepting branches held to DIFFERENT
    standards: the handler branch resolved the callable and walked it to a
    primitive, while the dependency branch tested bare membership in a
    hand-maintained ``_DEPENDENCY_GATES`` allowlist and stopped there. Those
    two sets are unrelated, so a router-level gate whose name was not also in
    ``PROJECT_SCOPED_GATES`` was certified by one branch and verified by
    nothing — round 1's defect, one allowlist over. Proven by execution: a
    synthetic router gate that authorized nothing certified its whole prefix
    the moment its identity was added to the allowlist, with no test going red.

    Every defect this saga produced, without exception, was TWO ACCEPTING PATHS
    VERIFIED TO DIFFERENT STANDARDS. Collapsing them to one predicate is the
    root-cause fix rather than a fifth patch, and it removes the allowlist as a
    trust anchor: nothing is certified because it appears in a list.
    """
    reached, trace = delegation_trace(call)
    if not reached:
        return False, trace
    try:
        params = inspect.signature(call).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False, [*trace, "<signature unreadable>"]
    if "project_id" not in params:
        return False, [*trace, "<declares no project_id>"]
    return True, trace


def dependency_gate_delegates(dependant) -> bool:
    """Does any callable in *dependant*'s tree satisfy
    :func:`callable_is_a_project_gate`?

    This is the Bug-8356 pattern — gate the router, not each handler — and it
    is now held to exactly the same standard as a handler-awaited gate.
    """

    def _walk(dep) -> bool:
        for sub_dep in dep.dependencies:
            if sub_dep.call is not None and callable_is_a_project_gate(
                sub_dep.call
            )[0]:
                return True
            if _walk(sub_dep):
                return True
        return False

    return _walk(dependant)


def resolved_gate_delegates(endpoint) -> bool:
    """Does *endpoint* await a certified gate name that RESOLVES to a real gate?

    This is route certification, and it is deliberately not "the handler
    awaits a name in the allowlist". That weaker form is what let four
    wide-open routes through the whole guard family: the name a handler awaits
    can be bound by a local alias, a function-scope import, ``globals()[...]``
    or ``setattr(sys.modules[__name__], ...)``, none of which is a module-scope
    AST binding, so nothing verified them.

    Resolving through the handler's own ``__globals__`` closes the two that
    bind the name at MODULE scope (``globals()[...] = `` and
    ``setattr(sys.modules[__name__], ...)``): the resolved object is the
    impostor, and it fails the delegation walk.

    It does NOT close the two that bind at FUNCTION scope — an earlier round
    claimed it closed all four, and that claim was false in every module that
    also binds the gate name at module scope, which is all nine modules hosting
    a project route. There the global IS the real gate, so a local alias or a
    function-scope import certified while the handler called something else.
    Those two are closed instead by :func:`locally_bound_names`, which makes
    ``awaited_calls_receiving_project_id`` REFUSE a rebound name outright.

    Fails closed on an unreadable handler and on a decorated one.
    """
    # Same posture, and the same argument, as ``delegation_trace``'s refusal to
    # unwrap a decorated gate — carried across here because it was NOT, and an
    # Opus5 R2 gate proved the omission by execution.
    # ``inspect.getsourcelines`` calls ``inspect.unwrap`` internally, so
    # ``getsource`` on a ``functools.wraps`` wrapper returns the UNDECORATED
    # handler's body. A wrapper that returns early — or never calls through at
    # all — therefore presents the handler's gate call, which it never
    # executes, and the route is certified while performing no authorization.
    # Measured: ``_classify_ungated`` returned ``[]`` for exactly that route.
    # No route handler in this service is decorated today (verified: none of
    # the 49 project-addressed routes carries ``__wrapped__``), so refusing
    # costs nothing now and forces a written decision if one appears.
    if getattr(endpoint, "__wrapped__", None) is not None:
        return False
    try:
        source = textwrap.dedent(inspect.getsource(endpoint))
    except (OSError, TypeError):  # pragma: no cover - defensive
        return False
    namespace = getattr(endpoint, "__globals__", {})
    return any(
        delegation_trace(namespace.get(name))[0]
        for name in sorted(gate_calls_in_source(source))
    )


# ---------------------------------------------------------------------------
# Shared test scaffolding
# ---------------------------------------------------------------------------

_SYNTHETIC_MODULE_COUNTER = iter(range(1, 100_000))


def load_synthetic_module(tmp_path, source: str):
    """Import *source* as a real module and return ``(name, module)``.

    A synthetic MODULE, not a synthetic string: both halves of the contract
    resolve an awaited name through the defining function's ``__globals__`` and
    compare runtime identity, and none of that can be exercised by parsing a
    string or by a function defined inside a test method (whose enclosing names
    are locals, not module globals). Counter-examples that skip this step
    silently test something weaker than production.

    Lives here rather than in either guard file because both need it, and
    "written down twice" is the failure mode this whole module exists to end.
    """
    name = f"_gate_contract_probe_{next(_SYNTHETIC_MODULE_COUNTER)}"
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # pragma: no cover - defensive
        sys.modules.pop(name, None)
        raise
    return name, module


def module_scope_statements(tree: ast.Module):
    """Statements that execute at MODULE scope, including ones nested inside
    module-level ``if`` / ``try`` / ``with`` / loops.

    Bodies of functions, lambdas and classes are skipped: a name bound there is
    not a module attribute. Note this is why the module-scope walk cannot be
    the load-bearing check for route certification — see
    ``resolved_gate_delegates``.
    """
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        # "cases" covers ``match`` — an ``ast.Match`` holds its statements
        # under ``cases[].body``, and a ``match_case`` yielded here then has
        # its own ``body`` pushed on the next iteration (Opus5 R2 gate, F5).
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            stack.extend(getattr(node, field, None) or [])


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for elt in node.elts:
            names |= _target_names(elt)
        return names
    return set()


def gate_names_bound_at_module_scope(source: str) -> set[str]:
    """Every certified-gate NAME this module binds at module scope.

    Four binding shapes:

    * ``def`` / ``async def`` — the obvious one, and the only one the old
      hardcoded tuple could ever have represented;
    * ``import`` / ``from ... import [as]`` — a re-export. Usually benign (it
      resolves to the real gate, which the delegation walk then confirms), but
      ``from .evil import x as _require_project_viewer`` is a certified name
      bound to arbitrary code, so it must be checked, not assumed;
    * assignment, ``_require_project_viewer = _my_hand_rolled_check`` — the
      cheapest way to defeat a def-only scan;
    * annotated assignment, the same thing with a type on it.

    Not covered, by construction: anything not statically visible at module
    scope (``globals()[...] = ``, ``setattr(sys.modules[__name__], ...)``, a
    subscript target, a name bound inside a function). Those are covered on the
    route side instead, by ``resolved_gate_delegates``, which resolves what the
    handler actually awaits rather than what the AST can see being bound. This
    walk is the module-inventory half; it is not the security boundary.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in module_scope_statements(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in PROJECT_SCOPED_GATES:
                found.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in PROJECT_SCOPED_GATES:
                    found.add(bound)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                found |= _target_names(target) & PROJECT_SCOPED_GATES
        elif isinstance(node, ast.AnnAssign):
            found |= _target_names(node.target) & PROJECT_SCOPED_GATES
    return found


def module_name_for(path: pathlib.Path) -> str:
    parts = list(path.relative_to(_SERVICE_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def discover_gate_bindings() -> tuple[dict[tuple[str, str], object], int]:
    """Every ``(module, gate_name)`` bound at module scope anywhere under
    ``src/``, plus the number of files scanned so a caller can assert the walk
    has not silently regressed to nothing."""
    bindings: dict[tuple[str, str], object] = {}
    scanned = 0
    for path in sorted(_SRC.rglob("*.py")):
        # utf-8-sig: seven files elsewhere in this repo carry a UTF-8 BOM
        # (Bug-8539), which makes ast.parse on a plain utf-8 read raise — and a
        # coverage guard that CRASHES stops enumerating the rest of the tree.
        source = path.read_text(encoding="utf-8-sig")
        scanned += 1
        names = gate_names_bound_at_module_scope(source)
        if not names:
            continue
        module_name = module_name_for(path)
        module = importlib.import_module(module_name)
        for name in sorted(names):
            bindings[(module_name, name)] = getattr(module, name, None)
    return bindings, scanned
