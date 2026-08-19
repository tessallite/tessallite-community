"""Every project-scoped agent-service route must be authorization-gated.

Why this file exists
--------------------
This lane fixed the same defect class four times, and each time the fix was
found by someone else because the enumeration method used to look for it was
structurally incapable of finding the next one:

* Bug-8356 — the four webhook management routes called a gate that had no
  ``project_id`` predicate at all.
* Bug-8444 — ``agent_config._require_project_viewer`` had the same missing
  predicate. Found by grepping ``select(UserAccessBinding)``.
* Bug-8444 (2nd) — ``personas._require_project_viewer`` had it too. The grep
  above *would* have found it; the person running the grep stopped at one
  file. Found by a fresh reviewer.
* Bug-8460 / Bug-8459 — ``rubrics.py`` had SIX routes with no gate whatsoever
  (including PUT/DELETE/bulk-replace of the judge criteria), and ``kpis.py``
  gated ``/calibration`` but not ``/kpis`` or ``/cost``. **Grepping for
  binding lookups cannot possibly find these**: a discovery mechanism keyed on
  gates that EXIST is blind, by construction, to a route carrying none. Found
  by a fresh reviewer.

That last one is the CLAUDE.md "coverage-tool blind-spot" category: the
verification method's own enumeration logic had the hole, not just the code.
The fix is to enumerate the thing the property is actually about — ROUTES —
rather than the thing the property is implemented with.

What this guard asserts
-----------------------
For every registered route that addresses a project: some callable it runs
satisfies ONE predicate — ``gate_contract.callable_is_a_project_gate``, i.e. it
reaches a terminal authorization primitive AND is told which project. Two
discovery mechanisms feed that predicate (a route/router dependency, or a gate
name the handler awaits and resolves), because a dependency is injected with
the project by name while a handler passes it as an argument. Nothing else
certifies a route.

There is exactly one predicate on purpose. Every defect this guard family has
shipped was TWO ACCEPTING PATHS VERIFIED TO DIFFERENT STANDARDS, and each one
was found only after the previous had been declared closed:

* "a gate is called" alone is not the property — Bug-8356 was a gate that ran
  but was never told which project, hence the ``project_id`` obligation;
* "a gate NAME is called" alone is not the property either. Certification was
  bare set-membership on the awaited name, backed by a separate guard that
  enumerated only MODULE-SCOPE bindings of those names. The names a handler can
  resolve at runtime are a strict superset of that, so a certified name bound
  by a local alias, a function-scope import (the ordinary circular-import
  workaround), ``globals()[...] = `` or ``setattr(sys.modules[__name__], ...)``
  was certified here and verified nowhere. Four such wide-open routes passed
  the whole guard family, proven by execution.
* the handler's SOURCE is not the handler either: ``inspect.getsourcelines``
  calls ``inspect.unwrap``, so a ``functools.wraps`` wrapper that never called
  through presented the undecorated body's gate call and certified. Decorated
  endpoints are refused rather than unwrapped.
* the DEPENDENCY branch tested membership in ``_DEPENDENCY_GATES`` and stopped.
  That set is unrelated to the delegation domain, so a router gate named
  anything else was certified and verified by nothing — round 1's defect, one
  allowlist over. Also proven by execution. That branch now runs the same
  predicate as the handler branch, which is what removed the second standard
  rather than patching it a fifth time.

Collapsing to one predicate ended "two standards", and two further gaps then
turned up one level more general — not two standards, but the STATIC-NAME model
diverging from Python's and FastAPI's DYNAMIC binding. Both are closed by
REFUSING rather than by recognising one more shape, which is the move that ends
the category: an unrecognised shape now produces a red build instead of a green
certification.

* a name the function REBINDS in its own body is refused. Resolution goes
  through module globals, and all nine modules hosting a project route bind a
  certified gate name at module scope — so ``_require_project_viewer =
  _wide_open`` inside a handler resolved to the REAL gate while calling the
  fake one. Proven by execution on three shapes plus a function-scope import.
* a route whose addressed project is not NAMED ``project_id`` is refused
  outright, before either branch. On ``/projects/{pid}/...`` FastAPI has no
  ``{project_id}`` path parameter to bind, so a gate declaring ``project_id``
  receives a CLIENT-SUPPLIED QUERY PARAMETER —
  ``GET /projects/<victim>/agent/secrets?project_id=<one I own>`` passed the
  gate and served ``<victim>``. Enumeration was keyed on the path segment while
  certification was keyed on the parameter name.

It fails CLOSED. An unrecognised route, a route whose gate this file does not
know about, and a route whose addressing it cannot line up with what the gates
see are all FAILURES requiring a human decision — never a silent skip.

Addressing shapes this guard enumerates, each added only after a review round
proved the guard failed OPEN without it — the pattern is worth naming, because
every single one was "a scope the property applies to that the discovery
mechanism was never pointed at":

* ``project_id`` as a path segment under the canonical
  ``/projects/{project_id}`` prefix (the original shape);
* ``project_id`` as a path segment ANYWHERE else, matched on the parameter
  NAME rather than on the path string (R5 — matching the literal marker meant
  an ungated ``/admin/agent/x/{project_id}`` route passed the whole suite);
* ``project_id`` as a query parameter anywhere in the dependency tree,
  including behind a shared ``Depends(...)`` filter (R4) and including one
  bound via ``Query(alias="project_id")`` (R5);
* ``project_id`` as a HEADER or COOKIE parameter, name or alias, matched
  case-insensitively and with ``-``/``_`` folded (external Codex gate — the
  docstring previously claimed headers were out of scope while presenting the
  gap list as complete, and the gate duly shipped an unchecked header-addressed
  route past all 11 tests);
* a ``/projects/{anything}`` SEGMENT whose parameter is NOT named
  ``project_id`` (Opus5 R3 gate — every previous branch keyed on the literal
  name, so ``/projects/{pid}/agent/secrets`` was invisible to all three
  enumerations and failed OPEN).

Shapes it still does NOT see, listed because an incomplete gap list is worse
than no gap list — a reader trusts it, and this list has been wrong three
times:

* ``project_id`` carried in a REQUEST BODY field;
* WebSocket routes (the walk keeps only ``APIRoute``, and the OpenAPI schema
  omits websockets);
* a dependency attached at INCLUDE time (``include_router(r,
  dependencies=[...])``) under FastAPI 0.139, which is the ambient developer
  interpreter: the walk descends ``original_router.routes``, which are the
  sub-router's routes as built BEFORE the include, so include-time
  dependencies are absent from ``route.dependant``. Under the locked 0.136.1
  that the Docker image installs, routes are flat and merged and the
  dependency IS visible. The divergence fails CLOSED (a spurious red under
  0.139), so it is not a hole — but the pressure a spurious red creates is
  toward adding a ``_DELIBERATELY_UNGATED`` entry, which would be. Prefer the
  ``APIRouter(dependencies=[...])`` constructor shape, which survives both:
  that is what ``webhooks.py`` uses.

None of the three exists in agent-service today — verified — and each would
need a new enumeration branch here before one is introduced. Tracked as Bug-8595
so the gap list is an artefact somebody owns, not only a docstring.
Adding a genuinely-unauthenticated project route means adding it to
``_DELIBERATELY_UNGATED`` with a written reason, which is a reviewable diff.

Tier: T1 (authorization contract). Test escape for Bug-8460/8459: no test
enumerated routes; every RBAC test asserted one hand-picked endpoint.
"""
from __future__ import annotations

import inspect
import re
import sys
import textwrap

from fastapi.routing import APIRoute

from src.main import app

# The gate contract lives in ONE module, shared with
# ``test_bug_8445_8446_project_gate_consolidation.py``. Both halves of the
# contract — "every project route runs a gate" (here) and "every binding of a
# gate name is a real gate" (there) — are keyed on the RESOLVED CALLABLE, and
# tests/gate_contract.py explains at length why that single shared key is the
# whole property. Do not restate any of it locally.
from tests.gate_contract import (
    PROJECT_SCOPED_GATES as _PROJECT_SCOPED_GATES,
    TERMINAL_PRIMITIVES as _TERMINAL_PRIMITIVES,
    awaited_calls_ignoring_rebinding as _awaited_calls_ignoring_rebinding,
    awaited_calls_receiving_project_id as _awaited_calls_receiving_project_id,
    callable_is_a_project_gate as _callable_is_a_project_gate,
    dependency_gate_delegates as _dependency_gate_delegates,
    gate_calls_in_source as _gate_calls_in_source,
    identity_of as _identity_of,
    load_synthetic_module as _load_synthetic_module,
    locally_bound_names as _locally_bound_names,
    resolved_gate_delegates as _resolved_gate_delegates,
)

PROJECT_PATH_MARKER = "/projects/{project_id}"

# Callables EXPECTED to gate a router or route. This is an INVENTORY, not a
# trust anchor: nothing is certified because it appears here. Certification of
# a dependency goes through ``_dependency_gate_delegates``, which applies the
# same predicate as the handler side — reach a terminal primitive AND declare
# ``project_id``. The list survives only so that a gate silently disappearing
# from the dependency tree is a test failure rather than an unnoticed opening;
# ``test_every_dependency_gate_is_a_real_project_aware_gate`` pins it both ways.
#
# It used to BE the certification, by ``module:qualname`` membership. That was
# strictly weaker than the handler branch — the delegation domain is
# ``PROJECT_SCOPED_GATES``, an unrelated set, so a router gate named anything
# else was certified here and verified nowhere. Proven by execution: a
# synthetic router gate authorizing nothing certified its whole prefix the
# moment its identity was added, with no test going red (Opus5 R3 gate, F3).
#
# Identity is still ``module:qualname``, never bare ``__name__``. The Codex
# cross-family gate demonstrated why: it added a route depending on a LOCAL
# no-op function it simply named ``_require_webhook_project_access``, and every
# coverage test passed while the route performed no authorization at all.
_DEPENDENCY_GATES = frozenset({
    "src.api.webhooks:_require_webhook_project_access",
})

# Routes deliberately not project-scope-gated, each with the reason it is
# safe. Anything added here is a security decision and must be justified in
# the diff.
_DELIBERATELY_UNGATED: dict[str, str] = {}

# Query-parameter project routes that are gated by a TENANT-WIDE role instead
# of a per-project binding. `require_tenant_admin` is a strictly stronger
# check than any project binding, and `get_tenant_db(current_user.tenant_id)`
# makes cross-tenant reach impossible, so per-project scoping adds nothing
# here. Listed explicitly so the shape stays enumerated rather than invisible.
# The identity that justification resolves to. Pinned as a constant so the
# assertion below compares ``module:qualname``, never a bare name.
_TENANT_ADMIN_IDENTITY = "shared.auth.middleware:require_tenant_admin"

_TENANT_ADMIN_QUERY_ROUTES: dict[str, str] = {
    "GET /admin/agent/retention": "require_tenant_admin; tenant-wide by design",
    "POST /admin/agent/retention/cleanup": "require_tenant_admin; tenant-wide by design",
    "GET /admin/agent/conversations/stats": "require_tenant_admin; tenant-wide by design",
    "DELETE /admin/agent/conversations/purge": "require_tenant_admin; tenant-wide by design",
}


def _walk_api_routes(routes):
    """Recursively collect every ``APIRoute``.

    FastAPI 0.139 wraps included routers in ``_IncludedRouter``, which is
    neither an ``APIRoute`` nor exposes ``.routes``, so the naive
    ``for r in app.routes: if isinstance(r, APIRoute)`` idiom silently sees
    almost nothing — that is Bug-8448, which quietly emptied three services'
    endpoint-accessibility suites. Descend through ``original_router``.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _walk_api_routes(original.routes)
        elif hasattr(route, "routes"):
            yield from _walk_api_routes(route.routes)


def _normalise(path: str) -> str:
    """Reduce a route path to the marker-anchored suffix.

    The two supported FastAPI versions expose different path shapes, and this
    guard must mean the same thing under both. Under the version pinned in
    ``uv.lock`` (0.136.1 — what the Docker image installs and therefore what
    production runs) ``app.routes`` is a FLAT list of ``APIRoute`` carrying
    the full mounted path (``/api/v1/projects/...``). Under the newer 0.139
    present in the ambient developer interpreter, included routers are wrapped
    in ``_IncludedRouter`` and the inner routes carry router-local paths
    (``/projects/...``).

    This guard was originally written against 0.139 only and was RED under the
    locked version — i.e. green had been demonstrated only on a framework no
    build artifact installs, which is exactly what CLAUDE.md means by "green
    must mean the deployed product works". Anchoring on the marker makes both
    shapes comparable and is a no-op on either one alone.
    """
    idx = path.index(PROJECT_PATH_MARKER)
    return path[idx:]


def _project_routes(routes=None):
    return [
        r for r in _walk_api_routes(app.routes if routes is None else routes)
        if PROJECT_PATH_MARKER in r.path
    ]


def _non_path_project_routes(routes=None):
    """Routes that address a project through something other than the
    canonical path segment: a query parameter, a header, or a cookie.

    A guard keyed on the ``/projects/{project_id}`` path shape is structurally
    blind to these — ``maintenance.py``'s admin routes reach exactly the same
    per-project data (including a hard delete of a project's whole
    conversation history) through ``project_id: UUID = Query(...)``. Missing a
    whole addressing shape is the enumeration blind spot CLAUDE.md tells this
    kind of tool to audit itself for, so they are enumerated here rather than
    left invisible.
    """
    out = []
    for route in _walk_api_routes(app.routes if routes is None else routes):
        if PROJECT_PATH_MARKER in route.path:
            continue
        addressed = (
            _all_query_param_names(route.dependant)
            | _all_header_and_cookie_param_names(route.dependant)
        )
        if "project_id" in addressed:
            out.append(route)
    return out


def _all_query_param_names(dependant) -> set[str]:
    """Query-parameter names across the WHOLE dependency tree, not just the
    endpoint signature. ``dependant.query_params`` holds only the params
    declared directly on the handler; a ``project_id`` delivered through
    ``Depends(some_shared_filter)`` lives on the sub-dependant and would
    otherwise be invisible -- the guard would fail OPEN on that shape."""
    names: set[str] = set()
    for q in dependant.query_params:
        names.add(q.name)
        # R5 — `pid: str = Query(alias="project_id")` binds the wire name to
        # `alias`, not `name`. A route consuming `?project_id=` that way was
        # invisible to a name-only flatten.
        alias = getattr(q, "alias", None)
        if alias:
            names.add(alias)
    for sub_dep in dependant.dependencies:
        names |= _all_query_param_names(sub_dep)
    return names


def _all_header_and_cookie_param_names(dependant) -> set[str]:
    """Header and cookie parameter names/aliases across the whole tree.

    Codex cross-family gate finding: the guard examined path and query
    parameters only, while FastAPI equally permits
    ``project_id: UUID = Header(..., alias="project_id")`` or a ``Cookie``.
    The gate added exactly such a route, reading one project's conversation
    count with no project-scoped check, and all 11 coverage tests passed.

    Header parameters are matched case-insensitively because HTTP header names
    are, and FastAPI's default alias conversion turns ``project_id`` into
    ``project-id`` on the wire — both spellings must count.
    """
    names: set[str] = set()
    for field in list(dependant.header_params) + list(dependant.cookie_params):
        for candidate in (field.name, getattr(field, "alias", None)):
            if candidate:
                names.add(candidate.replace("-", "_").lower())
    for sub_dep in dependant.dependencies:
        names |= _all_header_and_cookie_param_names(sub_dep)
    return names


def _all_path_param_names(dependant) -> set[str]:
    """Path-parameter names across the whole dependency tree.

    R5 — the path-segment shape was enumerated by matching the literal
    ``/projects/{project_id}`` marker, i.e. by the path STRING rather than by
    the parameter NAME. An ungated ``/admin/agent/x/{project_id}`` route
    therefore passed the entire suite green (mutation-proven). Every router in
    ``src/api/`` uses the canonical prefix today, so nothing was live -- but a
    guard whose contract is "fail closed on a shape I do not recognise" must
    not have a recognised shape it silently skips.
    """
    names = {p.name for p in dependant.path_params}
    for sub_dep in dependant.dependencies:
        names |= _all_path_param_names(sub_dep)
    return names


def _strip_mount(path: str) -> str:
    """Drop the ``/api/v1`` mount prefix when the running FastAPI version
    includes it, so route keys are stable across both supported shapes."""
    return path[len("/api/v1"):] if path.startswith("/api/v1") else path


def _dependency_names(dependant) -> set[str]:
    """Bare ``__name__`` of every callable in the dependency tree.

    Used only for the human-readable tenant-admin allowlist assertion, where a
    same-named impostor is not a meaningful risk (that assertion is checking
    our own four routes still carry the dependency we think they do). Route
    CERTIFICATION uses ``_dependency_identities`` instead — see
    ``_DEPENDENCY_GATES``.
    """
    names: set[str] = set()

    def _flatten(dep):
        for sub_dep in dep.dependencies:
            if sub_dep.call is not None:
                names.add(getattr(sub_dep.call, "__name__", ""))
            _flatten(sub_dep)

    _flatten(dependant)
    return names


def _dependency_identities(dependant) -> set[str]:
    """``module:qualname`` of every callable in the dependency tree."""
    identities: set[str] = set()

    def _flatten(dep):
        for sub_dep in dep.dependencies:
            if sub_dep.call is not None:
                identities.add(_identity_of(sub_dep.call))
            _flatten(sub_dep)

    _flatten(dependant)
    return identities


_PROJECT_SEGMENT_RE = re.compile(r"/projects/\{[^}]+\}")


def _off_marker_path_project_routes(routes=None):
    """Routes addressing a project through a PATH parameter outside the
    canonical ``/projects/{project_id}`` prefix. Split out so the live
    assertion and the counter-example test share one enumeration instead of
    two copies.

    Three ways to be one, because the first two are both keyed on the literal
    parameter NAME and a route is not obliged to spell it that way:

    * the literal ``{project_id}`` marker anywhere in the path;
    * a path parameter actually NAMED ``project_id`` anywhere in the tree (R5 —
      matching only the path string meant an ungated
      ``/admin/agent/x/{project_id}`` route passed the whole suite);
    * a ``/projects/{anything}`` SEGMENT, whatever the parameter is called
      (Opus5 R3 gate, F4). ``/projects/{pid}/agent/secrets`` reaches exactly
      the same per-project data and was invisible to all three enumerations —
      proven by execution, and failing OPEN rather than closed. No live route
      spells it that way today; a guard whose contract is "fail closed on a
      shape I do not recognise" must not have a recognised shape it skips.
    """
    out = []
    for route in _walk_api_routes(app.routes if routes is None else routes):
        if PROJECT_PATH_MARKER in route.path:
            continue
        if (
            "{project_id}" in route.path
            or "project_id" in _all_path_param_names(route.dependant)
            or _PROJECT_SEGMENT_RE.search(route.path)
        ):
            out.append(route)
    return out


def _addressed_project_is_named_project_id(route) -> bool:
    """Can a gate's ``project_id`` possibly BE the project this route serves?

    Opus5 R4 gate. Both accepting branches decide "is told which project" by
    the NAME ``project_id`` — a dependency declares it, a handler passes it. On
    a route whose project segment is spelled differently, that name is not the
    addressed project at all: FastAPI has no ``{project_id}`` path parameter to
    bind, so it binds ``project_id`` as a CLIENT-SUPPLIED QUERY PARAMETER.

    Measured on ``GET /projects/{pid}/agent/secrets`` with a real delegating
    router gate: path params ``{'pid'}``, query params ``{'project_id'}``,
    ``_classify_ungated`` -> ``[]``. So
    ``GET /projects/<victim>/agent/secrets?project_id=<one I own>`` passes the
    gate and the handler serves ``<victim>`` — a textbook IDOR, certified green.

    R3 added the ``/projects/{anything}`` enumeration branch precisely so this
    shape is SEEN. Enumeration keyed on the path segment, certification keyed
    on the parameter name: two things keyed differently, one layer below where
    the single-predicate collapse was looking.

    This is a route-level PRECONDITION, checked before either branch, because
    the hole is identical on both — a handler's ``await gate(project_id, ...)``
    resolves ``project_id`` from its own signature, which FastAPI binds the
    same way. Refusing is the whole mechanism: an unrecognised addressing shape
    now produces a red build instead of a green certification.
    """
    if "project_id" in _all_path_param_names(route.dependant):
        return True
    if _PROJECT_SEGMENT_RE.search(route.path) or "{project_id}" in route.path:
        # A /projects/{something-else} segment: the addressed project has a
        # different name, so nothing bound to ``project_id`` is it.
        return False
    # Addressed by a query parameter, header or cookie actually called
    # ``project_id`` (maintenance.py's admin routes) — then a gate's
    # ``project_id`` binds to the same value the handler reads.
    return "project_id" in (
        _all_query_param_names(route.dependant)
        | _all_header_and_cookie_param_names(route.dependant)
    )


def _why_not_certified(route) -> str:
    """A diagnostic suffix naming the REFUSAL, when there is a specific one.

    Opus5 confirmation round, LOW-1: the addressing refusal explains itself but
    the rebinding refusal did not, so a developer saw only "this route has no
    gate" while looking at a handler that visibly awaits one with
    ``project_id`` — and the generic remediation text offers
    ``_DELIBERATELY_UNGATED`` as one of three options. That is precisely the
    pressure toward an exemption entry that would turn a fail-closed guard into
    a fail-open one. Name the cause instead.
    """
    try:
        source = textwrap.dedent(inspect.getsource(route.endpoint))
    except (OSError, TypeError):  # pragma: no cover - defensive
        return ""
    if getattr(route.endpoint, "__wrapped__", None) is not None:
        return (
            "  [handler is DECORATED: inspect.getsource unwraps, so the gate "
            "call it shows is the one the wrapper may never execute]"
        )
    rebound = sorted(
        _awaited_calls_ignoring_rebinding(source)
        & _locally_bound_names(source)
        & _PROJECT_SCOPED_GATES
    )
    if rebound:
        return (
            f"  [awaits {rebound} but REBINDS the name in its own body, so the "
            "gate that runs is not the module-level one this guard can verify "
            "— hoist the binding to module scope rather than exempting the "
            "route]"
        )
    return ""


def _classify_ungated(routes) -> list[str]:
    """THE decision: which of ``routes`` reach project data with no
    project-aware gate. Returns human-readable keys.

    Extracted so the permanent counter-example tests below drive the SAME
    logic the real assertions drive. The re-gate's finding was that the
    self-tests exercised the helpers (parameter extraction, identity
    comparison) but never this classifier, so the wiring between them could
    regress while every self-test stayed green.

    There is exactly ONE accepting predicate:
    ``gate_contract.callable_is_a_project_gate`` — the callable reaches a
    terminal authorization primitive AND declares/receives ``project_id``. Two
    DISCOVERY mechanisms feed it, because a dependency is injected with the
    project by name while a handler passes it as an argument, but both
    terminate in the same check.

    That is the structural correction, and it is worth stating why rather than
    just doing it. Every defect this saga produced was TWO ACCEPTING PATHS
    VERIFIED TO DIFFERENT STANDARDS:

    * R1 — certification keyed on a gate NAME, verification iterated a
      hardcoded five-tuple. A gate in a module the tuple never heard of was
      certified by a string.
    * R2 — the sets agreed, but certification keyed on the NAME while
      verification keyed on the module-scope AST BINDING SITE. Four wide-open
      routes passed (local alias, function-scope import, ``globals()[...] = ``,
      ``setattr(sys.modules[__name__], ...)``).
    * R3 — ``inspect.getsourcelines`` unwraps, so certification read a
      DECORATED handler's undecorated body and certified a wrapper that never
      called through.
    * R4 — the handler branch resolved and walked; the DEPENDENCY branch tested
      bare membership in ``_DEPENDENCY_GATES`` and stopped. Round 1's defect,
      one allowlist over.

    Each was patched in turn and the next one appeared one level down. Rather
    than a fifth patch, the branches now share one predicate, so there is no
    second standard left for a future path to be held to.
    """
    ungated: list[str] = []
    for route in routes:
        # Per METHOD, not per route: a route registered with
        # ``methods=["GET", "POST"]`` collapsed to one key, so a single
        # ``_DELIBERATELY_UNGATED`` entry would have exempted both verbs
        # (Opus5 R4 gate; prospective only — no multi-method route exists).
        for method in sorted(route.methods):
            key = f"{method} {_strip_mount(route.path)}"
            if key in _DELIBERATELY_UNGATED or key in _TENANT_ADMIN_QUERY_ROUTES:
                continue
            if not _addressed_project_is_named_project_id(route):
                ungated.append(
                    f"{key}  ({route.endpoint.__module__})  "
                    "[addressed project is not named project_id, so no gate "
                    "can be told which project this route serves]"
                )
                continue
            if _dependency_gate_delegates(route.dependant):
                continue
            if _resolved_gate_delegates(route.endpoint):
                continue
            ungated.append(
                f"{key}  ({route.endpoint.__module__}){_why_not_certified(route)}"
            )
    return sorted(ungated)


def _counterexample_app():
    """A synthetic app carrying one route of every shape that has previously
    slipped past this guard. Registered permanently, not injected into the
    real app during a review, so a regression in route classification fails
    here rather than being rediscovered by the next external gate."""
    from fastapi import Cookie, Depends, FastAPI, Header, Query

    probe = FastAPI()

    # An impostor sharing the real gate's bare NAME but not its identity.
    async def _require_webhook_project_access() -> None:
        return None

    async def _shared_query_filter(project_id: str = Query(...)) -> str:
        return project_id

    @probe.get("/x/{project_id}")                      # off-marker path segment
    async def _p(project_id: str):
        return {}

    @probe.get("/hdr")                                  # header, mixed case
    async def _h(project_id: str = Header(..., alias="Project-ID")):
        return {}

    @probe.get("/cookie")                               # cookie
    async def _c(project_id: str = Cookie(...)):
        return {}

    @probe.get("/subdep")                               # query via a sub-dependency
    async def _s(pid: str = Depends(_shared_query_filter)):
        return {}

    @probe.get(                                         # same-named impostor gate
        "/impostor/{project_id}",
        dependencies=[Depends(_require_webhook_project_access)],
    )
    async def _i(project_id: str):
        return {}

    return probe


class TestEveryProjectRouteIsGated:
    def test_enumeration_is_not_vacuous(self):
        """Cross-check the route-tree walk against the published OpenAPI
        schema. If the walk ever stops seeing routes -- a framework upgrade,
        a new wrapper type -- the counts disagree and this fails CLOSED
        instead of quietly asserting nothing about zero routes."""
        # Both sides are reduced to the marker-anchored suffix so the
        # comparison holds under both supported FastAPI route shapes (see
        # _normalise). The two enumerations remain genuinely independent --
        # OpenAPI generation has its own traversal -- so a walker regression
        # still shows up as a disagreement.
        walked = {_normalise(r.path) for r in _project_routes()}
        published = {
            _normalise(p)
            for p in app.openapi()["paths"]
            if PROJECT_PATH_MARKER in p
        }
        assert published, "no project-scoped paths in the OpenAPI schema"
        assert walked == published, (
            "route-tree walk and OpenAPI schema disagree; "
            f"walk-only={sorted(walked - published)} "
            f"openapi-only={sorted(published - walked)}"
        )
        # Count ROUTES (method + path), not distinct paths: several paths
        # carry 2-3 methods and the property is per-route.
        assert len(_project_routes()) >= 40, (
            f"only {len(_project_routes())} project-scoped routes enumerated "
            "-- the walker has regressed"
        )

    def test_every_project_scoped_route_has_a_project_aware_gate(self):
        routes = _project_routes()
        assert len(routes) >= 40, (
            f"expected at least the 40 known project-scoped routes, found "
            f"{len(routes)}"
        )
        ungated = _classify_ungated(routes)
        assert not ungated, (
            "these project-scoped routes reach project data with no "
            "project-aware authorization gate:\n  " + "\n  ".join(ungated)
            + "\n\nEither call one of "
            + f"{sorted(_PROJECT_SCOPED_GATES)} with project_id, attach a "
            "router-level dependency, or add the route to "
            "_DELIBERATELY_UNGATED with a written reason."
        )

    def test_non_path_project_routes_are_enumerated_and_gated(self):
        """A route addressing a project through a query parameter, header or
        cookie reaches the same per-project data as a path-segment one --
        ``maintenance.py``'s purge route hard-deletes a project's entire
        conversation history -- but is invisible to a path-shape-keyed guard.
        Enumerate those shapes too, and require each route to be either
        project-gated or explicitly listed as tenant-admin-gated."""
        unaccounted = _classify_ungated(_non_path_project_routes())
        assert not unaccounted, (
            "these routes address a project through a query parameter, header "
            "or cookie and are neither project-gated nor listed as "
            "tenant-admin-gated:\n  " + "\n  ".join(unaccounted)
        )

    def test_the_tenant_admin_allowlist_is_not_stale(self):
        """An allowlist that names routes which no longer exist is an
        allowlist nobody is checking. Every entry must still resolve."""
        # Keyed per METHOD, exactly as _classify_ungated keys — Opus5
        # confirmation round, LOW-2. Leaving the two keyed differently is the
        # "two things keyed differently" pattern this whole lane exists to
        # eliminate, in miniature.
        live = {
            f"{method} {_strip_mount(r.path)}"
            for r in _non_path_project_routes()
            for method in r.methods
        }
        stale = sorted(set(_TENANT_ADMIN_QUERY_ROUTES) - live)
        assert not stale, f"allowlist names routes that no longer exist: {stale}"

    def test_allowlisted_query_routes_really_require_tenant_admin(self):
        """The allowlist's justification is 'require_tenant_admin covers it'.
        Assert that is actually true rather than trusting the comment.

        By ``module:qualname``, not bare ``__name__`` (Opus5 R3 gate, F6).
        These four routes are exempted from project-scope gating ENTIRELY on
        the strength of this assertion, and one of them hard-deletes a
        project's whole conversation history — so a same-named local impostor
        is exactly as meaningful a risk here as it was for the dependency
        allowlist the Codex gate defeated, and there is no reason to hold this
        one to the weaker standard.
        """
        checked = 0
        for route in _non_path_project_routes():
            keys = {
                f"{method} {_strip_mount(route.path)}" for method in route.methods
            }
            if not keys & set(_TENANT_ADMIN_QUERY_ROUTES):
                continue
            checked += 1
            identities = _dependency_identities(route.dependant)
            assert _TENANT_ADMIN_IDENTITY in identities, (
                f"{sorted(keys)} is allowlisted as tenant-admin-gated but "
                f"{_TENANT_ADMIN_IDENTITY} is not in its dependency tree; it "
                f"depends on {sorted(identities)}"
            )
        assert checked == len(_TENANT_ADMIN_QUERY_ROUTES), (
            f"only {checked} of {len(_TENANT_ADMIN_QUERY_ROUTES)} allowlisted "
            "routes were reached — this assertion is partly vacuous"
        )

    def test_path_param_project_routes_outside_the_marker_are_enumerated(self):
        """R5 reviewer — the path-segment shape was enumerated by the literal
        '/projects/{project_id}' marker, not the param name; an ungated
        '/admin/agent/x/{project_id}' route passed the whole suite green
        (mutation-proven). Fail CLOSED on any off-marker project_id path
        param."""
        strays = _classify_ungated(_off_marker_path_project_routes())
        assert not strays, (
            "these routes consume a project_id path segment outside the "
            "canonical /projects/{project_id} prefix and carry no "
            "project-aware gate:\n  " + "\n  ".join(sorted(strays))
        )

    def test_query_param_enumeration_sees_aliased_params(self):
        """R5 reviewer — `Query(alias="project_id")` binds the wire name to
        `alias`, not `name`, so a name-only flatten could not see it."""
        from fastapi import FastAPI, Query

        probe = FastAPI()

        @probe.get("/probe-alias")
        async def _handler(pid: str = Query(alias="project_id")):
            return {}

        route = next(
            r for r in probe.routes
            if isinstance(r, APIRoute) and r.path == "/probe-alias"
        )
        assert {q.name for q in route.dependant.query_params} == {"pid"}, (
            "FastAPI now reports the alias as the param name; the alias "
            "branch in _all_query_param_names is redundant (but harmless) "
            "and this pin should be revisited"
        )
        assert "project_id" in _all_query_param_names(route.dependant)

    def test_query_param_enumeration_sees_sub_dependency_params(self):
        """R4 reviewer — ``dependant.query_params`` holds only the params
        declared directly on the handler signature. A ``project_id``
        delivered through ``Depends(some_shared_filter)`` lives on the
        SUB-dependant, and before ``_all_query_param_names`` flattened the
        tree, an ungated route using that shape sailed through this guard
        green (proven by mutation on maintenance.py). Exercised against a
        synthetic app so the flatten itself is pinned, independent of
        whether any live route currently uses the shape."""
        from fastapi import Depends, FastAPI, Query

        probe = FastAPI()

        async def _shared_filter(project_id: str = Query(...)):
            return project_id

        @probe.get("/probe")
        async def _handler(pid: str = Depends(_shared_filter)):
            return {}

        route = next(
            r for r in probe.routes
            if isinstance(r, APIRoute) and r.path == "/probe"
        )
        top_level = {q.name for q in route.dependant.query_params}
        assert "project_id" not in top_level, (
            "FastAPI now flattens sub-dependency query params onto the "
            "top-level dependant; _all_query_param_names is redundant (but "
            "harmless) and this pin should be revisited"
        )
        assert "project_id" in _all_query_param_names(route.dependant)

    def test_header_addressed_project_routes_are_enumerated(self):
        """External Codex gate — the guard read path and query params only,
        so a route taking ``project_id`` through a Header (or Cookie) was
        invisible and shipped unchecked past all 11 tests. Synthetic so the
        enumeration is pinned whether or not a live route uses the shape."""
        from fastapi import Cookie, FastAPI, Header

        probe = FastAPI()

        @probe.get("/probe-header")
        async def _h(project_id: str = Header(...)):
            return {}

        @probe.get("/probe-cookie")
        async def _c(project_id: str = Cookie(...)):
            return {}

        by_path = {
            r.path: r for r in probe.routes if isinstance(r, APIRoute)
        }
        for path in ("/probe-header", "/probe-cookie"):
            found = _all_header_and_cookie_param_names(by_path[path].dependant)
            assert "project_id" in found, (
                f"{path}: header/cookie enumeration missed project_id "
                f"(saw {sorted(found)})"
            )

    # ``test_guard_rejects_a_same_named_impostor_dependency`` used to sit here.
    # Deleted on the Opus5 R4 gate's finding F3, for the same reason the R1
    # gate deleted ``test_the_guard_sees_an_attribute_access_not_only_a_bare_name``
    # (tombstone in the consolidation file): it never touched
    # ``_classify_ungated``, ``_dependency_gate_delegates`` or
    # ``callable_is_a_project_gate``. It asserted string membership in
    # ``_DEPENDENCY_GATES`` — the set this lane demoted from trust anchor to
    # inventory — and its docstring still described a certification path that
    # no longer exists. Two of its four assertions were tautologies about a
    # name the test itself had just written.
    #
    # The property it meant to hold is covered twice over, by code that runs:
    # ``_counterexample_app``'s ``/impostor/{project_id}`` route (driven
    # through the real classifier by
    # ``test_classifier_flags_every_shape_that_has_slipped_past_before``) and
    # ``test_every_dependency_gate_is_a_real_project_aware_gate``.

    def test_classifier_flags_every_shape_that_has_slipped_past_before(self):
        """Re-gate finding: the self-tests exercised the HELPERS but never the
        classifier, so the wiring between them could regress unnoticed. This
        drives the real classifier over a synthetic app carrying one route of
        every shape that has historically slipped past -- off-marker path
        segment, header (mixed case), cookie, query via a sub-dependency, and
        a dependency that merely shares the real gate's name -- and requires
        every one to come back flagged."""
        probe = _counterexample_app()
        found = set()
        for collection in (
            _project_routes(probe.routes),
            _non_path_project_routes(probe.routes),
            _off_marker_path_project_routes(probe.routes),
        ):
            found.update(_classify_ungated(collection))

        flagged_paths = {entry.split()[1] for entry in found}
        for expected in ("/x/{project_id}", "/hdr", "/cookie", "/subdep",
                         "/impostor/{project_id}"):
            assert expected in flagged_paths, (
                f"the classifier did NOT flag the ungated route {expected}; "
                f"it flagged {sorted(flagged_paths)}"
            )

    def test_guard_rejects_a_gate_that_ignores_project_id(self):
        """The guard must catch the ORIGINAL Bug-8356 shape -- a gate that
        runs but is never told which project -- not merely a missing gate.
        Exercised against a synthetic handler so the guard's own logic is
        tested, not just the code it inspects."""
        bad = """
async def handler(project_id, current_user):
    await _require_project_viewer(current_user)
"""
        good = """
async def handler(project_id, current_user):
    await _require_project_viewer(project_id, current_user)
"""
        assert _gate_calls_in_source(bad) == set(), (
            "the guard accepted a gate that never receives project_id -- it "
            "would have passed the original Bug-8356 code unchanged"
        )
        assert _gate_calls_in_source(good) == {"_require_project_viewer"}
        unknown = """
async def handler(project_id, current_user):
    await _some_new_helper(project_id, current_user)
"""
        assert _gate_calls_in_source(unknown) == set(), (
            "an unrecognised helper must not be mistaken for a gate"
        )

    def test_guard_rejects_unreachable_or_unawaited_gates(self):
        """R3 reviewer tests_to_promote item 1 — each of these LOOKS like a
        gate to a naive AST scan but enforces nothing at runtime."""
        unawaited = """
async def handler(project_id, current_user):
    _require_project_viewer(project_id, current_user)
"""
        dead_branch = """
async def handler(project_id, current_user):
    if False:
        await _require_project_viewer(project_id, current_user)
"""
        nested = """
async def handler(project_id, current_user):
    async def _check():
        await _require_project_viewer(project_id, current_user)
    return 1
"""
        foreign_attr = """
async def handler(project_id, current_user):
    await some_obj._require_project_viewer(project_id)
"""
        for src in (unawaited, dead_branch, nested, foreign_attr):
            assert _gate_calls_in_source(src) == set(), src

    def test_certification_is_not_satisfied_by_a_name_bound_outside_module_scope(
        self, tmp_path
    ):
        """Opus5 R1 gate — certification keys on the NAME a handler awaits,
        while the delegation guard in
        ``test_bug_8445_8446_project_gate_consolidation.py`` enumerates only
        MODULE-SCOPE bindings (``def`` / ``Assign`` / ``AnnAssign`` /
        ``Import``). Any other binding of a certified name is therefore
        certified by a string and verified by nothing — the same defect the
        derived-domain fix closed, one scope level down.

        Three self-contained shapes are driven here; a function-scope
        ``from x import y as _require_project_viewer`` — the ordinary
        circular-import workaround, and the one that is an accident rather
        than an attack — behaves identically and is the reason this is not
        theatre.

        Proven by execution before the fix: each shape registered a real
        ``/projects/{project_id}/agent/probe`` route whose gate returned
        ``None`` unconditionally, and ``_classify_ungated`` reported all three
        as gated.
        """
        import importlib.util
        import sys
        import textwrap as _tw

        from fastapi import FastAPI

        shapes = {
            "local alias in the handler body": """
                async def _wide_open(project_id, current_user=None):
                    return None

                async def handler(project_id: str, current_user=None):
                    _require_project_viewer = _wide_open
                    await _require_project_viewer(project_id, current_user)
                    return {}
            """,
            'globals()["<gate>"] = fn': """
                async def _wide_open(project_id, current_user=None):
                    return None

                globals()["_require_project_viewer"] = _wide_open

                async def handler(project_id: str, current_user=None):
                    await _require_project_viewer(project_id, current_user)
                    return {}
            """,
            "setattr(sys.modules[__name__], ...)": """
                import sys as _sys

                async def _wide_open(project_id, current_user=None):
                    return None

                setattr(_sys.modules[__name__], "_require_project_viewer", _wide_open)

                async def handler(project_id: str, current_user=None):
                    await _require_project_viewer(project_id, current_user)
                    return {}
            """,
        }

        unflagged = []
        for i, (label, src) in enumerate(shapes.items()):
            name = f"_gate_scope_probe_{i}"
            path = tmp_path / f"{name}.py"
            path.write_text(_tw.dedent(src), encoding="utf-8")
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
                probe = FastAPI()
                probe.get("/projects/{project_id}/agent/probe")(module.handler)
                if not _classify_ungated(_project_routes(probe.routes)):
                    unflagged.append(label)
            finally:
                sys.modules.pop(name, None)

        assert not unflagged, (
            "these wide-open project routes were CERTIFIED by the route guard "
            "because they await a name in _PROJECT_SCOPED_GATES that is bound "
            "somewhere the delegation guard does not enumerate:\n  "
            + "\n  ".join(unflagged)
        )

    def test_every_dependency_gate_is_a_real_project_aware_gate(self):
        """Opus5 R3 gate — ``_classify_ungated`` had TWO accepting branches and
        only one of them resolved. The awaited-name branch walked the callable
        to a terminal primitive; the dependency branch tested membership in
        ``_DEPENDENCY_GATES`` and stopped. Those sets are unrelated: the
        delegation domain is ``PROJECT_SCOPED_GATES``, so a name in
        ``_DEPENDENCY_GATES`` that is not also in ``PROJECT_SCOPED_GATES`` was
        certified there and verified nowhere — round 1's defect, one allowlist
        over.

        Proven by execution: a synthetic router-level gate that authorized
        nothing certified its whole prefix the moment its identity was added to
        ``_DEPENDENCY_GATES``, and nothing in either guard file went red. The
        single live entry was safe only by coincidence (it also happens to be
        in ``PROJECT_SCOPED_GATES``); nothing enforced that coincidence.

        The branch now runs ``callable_is_a_project_gate``, so the allowlist is
        no longer a trust anchor. This test keeps it honest in BOTH directions:
        every entry must still resolve and must still satisfy the predicate, so
        the inventory cannot drift from the code and cannot quietly list
        something that would not certify on its own merits.
        """
        import importlib

        assert _DEPENDENCY_GATES, "the dependency-gate inventory is empty"
        for identity in sorted(_DEPENDENCY_GATES):
            module_name, _, qualname = identity.partition(":")
            obj = importlib.import_module(module_name)
            for part in qualname.split("."):
                obj = getattr(obj, part, None)
                assert obj is not None, (
                    f"_DEPENDENCY_GATES names {identity!r}, which resolves to "
                    "nothing — the inventory has drifted from the code"
                )
            ok, trace = _callable_is_a_project_gate(obj)
            assert ok, (
                f"{identity} is listed as a router gate but does not satisfy "
                f"the one accepting predicate (reach {sorted(_TERMINAL_PRIMITIVES)} "
                f"AND declare project_id); chain was: " + " -> ".join(trace)
            )

    def test_the_dependency_branch_runs_the_predicate_not_a_membership_test(
        self, tmp_path
    ):
        """Pin the WIRING, not just the helper.

        Self-mutation found this: reverting ``_classify_ungated``'s dependency
        branch to bare ``_DEPENDENCY_GATES`` membership turned NOTHING red.
        ``test_every_dependency_gate_is_a_real_project_aware_gate`` asserts the
        inventory's entries satisfy the predicate, but nothing asserted that
        the classifier CONSULTS the predicate — the same "the self-tests
        exercised the helpers but never the classifier" gap a previous round
        recorded, reappearing on the branch that had just been rewritten.

        Two synthetic routers discriminate the two implementations:

        * one gated by a REAL delegating gate whose identity is NOT in the
          inventory. Under the predicate it certifies (correct — a genuinely
          delegating router gate should not need allowlisting). Under
          membership it is reported ungated.
        * one gated by a callable that authorizes nothing. Must be reported
          ungated under either, so the first assertion cannot pass by the
          classifier simply accepting everything.
        """
        from fastapi import Depends, FastAPI

        name, module = _load_synthetic_module(tmp_path, '''
            from uuid import UUID

            from fastapi import Depends

            from src.api.agent_config import _require_blocked_original_access
            from src.auth.middleware import CurrentUser, forbid_embed_user


            async def real_router_gate(
                project_id: UUID,
                current_user: CurrentUser = Depends(forbid_embed_user),
            ) -> None:
                await _require_blocked_original_access(project_id, current_user)


            async def wide_open_router_gate(project_id: UUID) -> None:
                return None
        ''')
        try:
            assert (
                _identity_of(module.real_router_gate) not in _DEPENDENCY_GATES
            ), "pick a gate NOT in the inventory, or this proves nothing"

            good = FastAPI()

            @good.get(
                "/projects/{project_id}/agent/real",
                dependencies=[Depends(module.real_router_gate)],
            )
            async def _g(project_id: str):
                return {}

            bad = FastAPI()

            @bad.get(
                "/projects/{project_id}/agent/fake",
                dependencies=[Depends(module.wide_open_router_gate)],
            )
            async def _b(project_id: str):
                return {}

            assert _classify_ungated(_project_routes(good.routes)) == [], (
                "a router-level dependency that genuinely reaches an "
                "authorization primitive was reported ungated — the "
                "dependency branch is testing inventory membership, not the "
                "predicate"
            )
            assert _classify_ungated(_project_routes(bad.routes)), (
                "a router-level dependency that authorizes nothing was "
                "CERTIFIED"
            )
        finally:
            sys.modules.pop(name, None)

    def test_a_dependency_gate_must_be_told_which_project(self, tmp_path):
        """The Bug-8356 shape, dependency-side.

        A dependency receives ``project_id`` by NAME — FastAPI injects the path
        parameter into a parameter of that name. A gate that reaches the
        primitive while gating some OTHER project (a constant, a config value)
        runs, passes, and protects nothing. Self-mutation found this too:
        dropping the ``project_id`` obligation from the single predicate turned
        nothing red, because the only test touching it drove the live
        inventory, whose one entry happens to declare the parameter.
        """
        from fastapi import Depends, FastAPI

        name, module = _load_synthetic_module(tmp_path, '''
            from fastapi import Depends

            from src.auth.middleware import CurrentUser, forbid_embed_user
            from src.auth.project_access import require_project_role


            async def gates_the_wrong_project(
                current_user: CurrentUser = Depends(forbid_embed_user),
            ) -> None:
                # Reaches the terminal, but the project it checks comes from
                # nowhere the request can influence, so the REQUESTED project
                # is never authorized.
                project_id = "00000000-0000-0000-0000-000000000000"
                await require_project_role(
                    project_id, current_user, db_factory=None, detail="x",
                )
        ''')
        try:
            ok, trace = _callable_is_a_project_gate(module.gates_the_wrong_project)
            assert not ok, (
                "a gate that reaches the primitive but declares no project_id "
                f"parameter was accepted; trace: {trace}"
            )
            assert "project_id" in trace[-1], trace

            probe = FastAPI()

            @probe.get(
                "/projects/{project_id}/agent/thing",
                dependencies=[Depends(module.gates_the_wrong_project)],
            )
            async def _h(project_id: str):
                return {}

            assert _classify_ungated(_project_routes(probe.routes)), (
                "a route whose only gate authorizes a hardcoded project was "
                "CERTIFIED"
            )
        finally:
            sys.modules.pop(name, None)

    def test_certification_is_not_satisfied_by_a_gate_name_rebound_in_the_handler(
        self, tmp_path
    ):
        """Opus5 R4 gate — the earlier fix resolves the awaited name through the
        handler's own ``__globals__``, and every one of its counter-examples
        used a module that does NOT bind the gate name at module scope. So
        resolution returned ``None`` and the shape was flagged for the wrong
        reason.

        All NINE modules that host a project route bind at least one certified
        gate name at module scope (measured 2026-08-04, after Bug-8589 HALF A
        added the chat tier: ``discover_gate_bindings`` returns 20 bindings over
        10 modules — the tenth is ``src.auth.project_access`` itself, which
        defines a gate but hosts no route). There, the module global IS the real
        gate,
        so a handler that rebinds the name at FUNCTION scope is certified by
        the real gate and calls the rebound one. Proven by execution:
        ``_classify_ungated`` returned ``[]`` for assignment, loop target,
        tuple-unpack and function-scope import shapes. A later chronological
        deep review found the same false pass for all three structural-pattern
        capture forms, whose bound names live in AST string fields rather than
        ``Name(Store)`` nodes.
        """
        from fastapi import FastAPI

        real_gate = '''
            from src.auth.project_access import require_project_role


            async def _require_project_viewer(project_id, current_user, **kw):
                await require_project_role(
                    project_id, current_user, db_factory=None, detail="x",
                )


            async def _wide_open(project_id, current_user=None):
                return None
        '''
        shapes = {
            "assignment in the handler body": real_gate + '''

            async def handler(project_id: str, current_user=None):
                _require_project_viewer = _wide_open
                await _require_project_viewer(project_id, current_user)
                return {}
        ''',
            "for-loop target": real_gate + '''

            async def handler(project_id: str, current_user=None):
                for _require_project_viewer in (_wide_open,):
                    pass
                await _require_project_viewer(project_id, current_user)
                return {}
        ''',
            "tuple unpack": real_gate + '''

            async def handler(project_id: str, current_user=None):
                _require_project_viewer, _n = _wide_open, 1
                await _require_project_viewer(project_id, current_user)
                return {}
        ''',
            "function-scope import alias": real_gate + '''

            async def handler(project_id: str, current_user=None):
                from src.api.agent_config import (
                    _require_project_viewer as _require_project_viewer,
                )
                await _require_project_viewer(project_id, current_user)
                return {}
        ''',
            "match capture": real_gate + '''

            async def handler(project_id: str, current_user=None):
                match _wide_open:
                    case _require_project_viewer:
                        pass
                await _require_project_viewer(project_id, current_user)
                return {}
        ''',
            "match sequence-star capture": real_gate + '''

            async def handler(project_id: str, current_user=None):
                match [_wide_open]:
                    case [*_require_project_viewer]:
                        pass
                await _require_project_viewer(project_id, current_user)
                return {}
        ''',
            "match mapping-rest capture": real_gate + '''

            async def handler(project_id: str, current_user=None):
                match {"gate": _wide_open}:
                    case {**_require_project_viewer}:
                        pass
                await _require_project_viewer(project_id, current_user)
                return {}
        ''',
        }

        certified = []
        undiagnosed = []
        for label, src in shapes.items():
            name, module = _load_synthetic_module(tmp_path, src)
            try:
                probe = FastAPI()
                probe.get("/projects/{project_id}/agent/probe")(module.handler)
                reported = _classify_ungated(_project_routes(probe.routes))
                if not reported:
                    certified.append(label)
                elif not any("REBINDS" in entry for entry in reported):
                    undiagnosed.append(f"{label}: {reported}")
            finally:
                sys.modules.pop(name, None)

        assert not certified, (
            "these handlers were CERTIFIED because the awaited gate name "
            "resolves to the module-scope REAL gate, while the name they "
            "actually call is rebound at function scope: " + ", ".join(certified)
        )
        # The refusal must SAY why. A developer looking at a handler that
        # visibly awaits a gate with project_id, told only "this route has no
        # gate", reasonably concludes the guard is wrong and reaches for
        # _DELIBERATELY_UNGATED — which converts a fail-closed guard into a
        # fail-open one. Opus5 confirmation round, LOW-1.
        assert not undiagnosed, (
            "the refusal fired but did not name rebinding as the cause:\n  "
            + "\n  ".join(undiagnosed)
        )

    def test_a_dependency_gate_must_receive_the_ADDRESSED_project(self, tmp_path):
        """Opus5 R4 gate — ``callable_is_a_project_gate`` treats "declares a
        parameter named ``project_id``" as "is told which project". On a
        ``/projects/{pid}`` route that is false: ``project_id`` is not a path
        parameter there, so FastAPI binds it as a QUERY parameter and the gate
        authorizes whatever project the CLIENT supplies, while the handler
        serves ``{pid}``.

        Measured on this exact route: path params ``{'pid'}``, query params
        ``{'project_id'}``, ``_classify_ungated`` -> ``[]``. R3 added the
        ``/projects/{anything}`` enumeration branch precisely so this shape is
        SEEN; certification then accepted it anyway — enumeration keyed on the
        path segment, certification keyed on the parameter name.
        """
        from fastapi import Depends, FastAPI

        name, module = _load_synthetic_module(tmp_path, '''
            from uuid import UUID

            from fastapi import Depends

            from src.api.agent_config import _require_blocked_original_access
            from src.auth.middleware import CurrentUser, forbid_embed_user


            async def gate(
                project_id: UUID,
                current_user: CurrentUser = Depends(forbid_embed_user),
            ) -> None:
                await _require_blocked_original_access(project_id, current_user)
        ''')
        try:
            probe = FastAPI()

            @probe.get(
                "/projects/{pid}/agent/secrets",
                dependencies=[Depends(module.gate)],
            )
            async def _h(pid: str):
                return {}

            route = _off_marker_path_project_routes(probe.routes)[0]
            assert "project_id" not in _all_path_param_names(route.dependant), (
                "sanity: the addressed project is {pid}, not {project_id}"
            )
            assert "project_id" in _all_query_param_names(route.dependant), (
                "sanity: FastAPI binds the gate's project_id as a CLIENT-"
                "supplied query parameter because the segment is called pid"
            )
            assert _classify_ungated([route]), (
                "a /projects/{pid} route was CERTIFIED by a dependency whose "
                "project_id is a client-supplied query parameter rather than "
                "the addressed project — GET /projects/<victim>/agent/secrets"
                "?project_id=<mine> passes the gate and serves <victim>"
            )
        finally:
            sys.modules.pop(name, None)

    def test_a_HANDLER_awaited_gate_must_receive_the_ADDRESSED_project(
        self, tmp_path
    ):
        """The addressing precondition's HANDLER-branch half.

        Its sibling above covers the dependency branch only. The hole is
        identical on the handler branch — ``await gate(project_id, ...)``
        resolves ``project_id`` from the handler's own signature, which
        FastAPI binds from the query string when the path segment is called
        something else — and the precondition is route-level precisely so it
        covers both. Nothing pinned that. Measured on this route: the handler
        branch reports ``resolved_gate_delegates -> True`` (it awaits the REAL
        gate and reaches a terminal primitive), so relocating the precondition
        into ``_dependency_gate_delegates`` would silently re-certify
        ``GET /projects/<victim>/agent/probe?project_id=<mine>``.
        """
        from fastapi import FastAPI

        name, module = _load_synthetic_module(tmp_path, '''
            from src.api.agent_config import _require_blocked_original_access


            async def handler(project_id: str, current_user=None):
                await _require_blocked_original_access(project_id, current_user)
                return {}
        ''')
        try:
            probe = FastAPI()
            probe.get("/projects/{pid}/agent/probe")(module.handler)
            route = _off_marker_path_project_routes(probe.routes)[0]

            assert "project_id" not in _all_path_param_names(route.dependant), (
                "sanity: the addressed project is {pid}, not {project_id}"
            )
            assert "project_id" in _all_query_param_names(route.dependant), (
                "sanity: FastAPI binds the handler's project_id as a CLIENT-"
                "supplied query parameter because the segment is called pid"
            )
            assert _resolved_gate_delegates(route.endpoint), (
                "sanity: the handler branch DOES certify this route on its own "
                "-- if it did not, this test would pass for the wrong reason"
            )
            assert _classify_ungated([route]), (
                "a /projects/{pid} route was CERTIFIED by a gate the HANDLER "
                "awaits whose project_id is a client-supplied query parameter "
                "rather than the addressed project -- the precondition must be "
                "route-level, not inside the dependency branch"
            )
        finally:
            sys.modules.pop(name, None)

    def test_a_multi_method_route_is_classified_and_allowlisted_per_method(self):
        """Opus5 confirmation round, LOW-2 — the classifier and the two
        tenant-admin allowlist assertions used to key differently: the
        classifier per method, the assertions on ``sorted(route.methods)[0]``.

        No live route registers more than one method, so the two agree today
        and neither the live assertions nor a mutation of them can tell the
        difference. Pin the property on a synthetic route instead, because
        "two things keyed differently" is the exact pattern this whole file
        exists to eliminate and leaving an unverifiable instance of it in the
        file that eliminates it is not acceptable.

        The consequence if they diverge: one ``_DELIBERATELY_UNGATED`` entry
        would exempt every verb of a multi-method route — a ``GET`` exemption
        silently covering a ``DELETE``.
        """
        from fastapi import FastAPI

        probe = FastAPI()

        @probe.api_route(
            "/projects/{project_id}/agent/multi", methods=["GET", "DELETE"]
        )
        async def _handler(project_id: str):
            return {}

        keys = _classify_ungated(_project_routes(probe.routes))
        methods = sorted(k.split()[0] for k in keys)
        assert methods == ["DELETE", "GET"], (
            "a two-method route must yield one classification key PER METHOD, "
            f"or a single exemption entry covers both verbs; got {keys}"
        )

    def test_a_project_path_segment_is_seen_whatever_the_param_is_called(self):
        """Opus5 R3 gate — every path branch keyed on the literal name
        ``project_id``, so ``/projects/{pid}/agent/secrets`` reached exactly
        the same per-project data and was invisible to all three enumerations.
        It failed OPEN, which is the direction that matters."""
        from fastapi import FastAPI

        probe = FastAPI()

        @probe.get("/projects/{pid}/agent/secrets")
        async def _handler(pid: str):
            return {}

        found = _off_marker_path_project_routes(probe.routes)
        assert [r.path for r in found] == ["/projects/{pid}/agent/secrets"], (
            "a /projects/{...} segment whose parameter is not literally named "
            "project_id is invisible to the route enumeration"
        )
        assert _classify_ungated(found), (
            "...and therefore an ungated one is not reported"
        )

    def test_a_decorated_endpoint_is_not_certified(self, tmp_path):
        """Opus5 R2 gate — ``delegation_trace`` refuses a decorated GATE
        because ``inspect.getsourcelines`` calls ``inspect.unwrap``, so
        ``getsource`` on a ``functools.wraps`` wrapper returns the UNDECORATED
        body. Certification read the ENDPOINT's source exactly the same way
        with no such refusal, so a wrapper that never calls the handler
        presented the handler's gate call and certified a wide-open route.
        Proven by execution: ``_classify_ungated`` returned ``[]``.

        No route handler in this service is decorated today, so refusing costs
        nothing now and forces a written decision if one ever appears — the
        same posture, and the same argument, as the gate half.
        """
        import importlib.util
        import sys
        import textwrap as _tw

        from fastapi import FastAPI

        name = "_gate_decorated_endpoint_probe"
        path = tmp_path / f"{name}.py"
        path.write_text(_tw.dedent('''
            import functools

            from src.api.agent_config import _require_project_viewer

            def bypass(fn):
                @functools.wraps(fn)
                async def inner(project_id: str, current_user=None):
                    return {"every_project": "leaked"}
                return inner

            @bypass
            async def handler(project_id: str, current_user=None):
                await _require_project_viewer(project_id, current_user)
                return {}
        '''), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            probe = FastAPI()
            probe.get("/projects/{project_id}/agent/probe")(module.handler)
            assert _classify_ungated(_project_routes(probe.routes)), (
                "a decorated endpoint whose wrapper never calls the handler "
                "was CERTIFIED: inspect.getsource unwrapped past the wrapper "
                "and read the gate call the wrapper never executes"
            )
        finally:
            sys.modules.pop(name, None)

    def test_every_project_route_endpoint_is_defined_under_src(self):
        """The delegation guard walks ``src/`` only. If a project-addressed
        route's handler is ever defined outside that tree — imported from
        ``shared/``, or from a plugin package — the gate names its module
        binds are verified by nothing, and certification-by-name silently
        becomes certification-by-string again. Pin the assumption instead of
        leaving it implicit in a docstring."""
        routes = (
            list(_project_routes())
            + list(_non_path_project_routes())
            + list(_off_marker_path_project_routes())
        )
        assert routes, "no project-addressed routes enumerated"
        outside = sorted({
            r.endpoint.__module__
            for r in routes
            if not r.endpoint.__module__.startswith("src.")
        })
        assert not outside, (
            "these modules define project-addressed route handlers but sit "
            "outside src/, so test_bug_8445_8446_project_gate_consolidation "
            f"never checks the gate names they bind: {outside}"
        )

    def test_the_shared_walk_is_not_narrowed_to_the_allowlist(self):
        """``_awaited_calls_receiving_project_id`` must stay UNFILTERED.

        The delegation guard in
        ``test_bug_8445_8446_project_gate_consolidation.py`` walks a chain of
        hops, and the hops that matter most are the ones that are NOT gate
        names: ``require_project_role`` is the terminal primitive and is
        deliberately absent from ``_PROJECT_SCOPED_GATES`` (nothing certifies a
        route by it, because handlers never call it directly). If someone
        re-narrows this walk to the allowlist -- the shape it had before -- the
        delegation guard silently stops being able to see the terminal hop and
        starts passing everything. Pin it.
        """
        source = """
async def gate(project_id, current_user):
    await require_project_role(project_id, current_user, db_factory=x)
"""
        assert _awaited_calls_receiving_project_id(source) == {
            "require_project_role"
        }
        assert _gate_calls_in_source(source) == set(), (
            "_gate_calls_in_source must stay narrowed to the allowlist even "
            "though the walk beneath it is not"
        )

    def test_guard_still_sees_a_gate_inside_a_real_block(self):
        """The hardening must not overshoot: the conversation handlers call
        their gate inside an ``async for db in get_tenant_db(...)`` block, and
        that is a perfectly real call site."""
        nested_block = """
async def handler(project_id, current_user):
    async for db in get_tenant_db(current_user.tenant_id):
        await _require_project_access_and_agent(db, project_id, current_user)
"""
        assert _gate_calls_in_source(nested_block) == {
            "_require_project_access_and_agent"
        }
