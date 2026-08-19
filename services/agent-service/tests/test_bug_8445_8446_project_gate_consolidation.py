"""Bug-8445 / Bug-8446 — one project authorization primitive, used everywhere.

Bug-8445 [structural]: this service hand-rolled five near-identical
``UserAccessBinding`` gates across three modules, and they had already drifted
on five independent axes (project predicate, role predicate, bootstrap-open
posture, admin-bypass predicate, identity comparison). Each individual drift
produced its own HIGH/MEDIUM security bug — Bug-8356 (cross-project IDOR),
Bug-8444 (secret-bearing ``webhook_url`` disclosure), Bug-8460/8459 (ungated
routes). Patching the five copies one at a time is a band-aid by construction:
the duplication IS the defect, and the sixth copy would drift the same way.

Bug-8446 [inconsistent authorization]: every copy compared
``UserAccessBinding.user_identity`` with a raw, case-sensitive ``==``, while
model-service both WRITES and reads bindings through
``shared.auth.identity.user_identity_matches`` (which lower-cases an email
identity first). A user whose IdP returns ``User@Example.com`` against a stored
``user@example.com`` binding was authorised by model-service — they could see
the project and its models — and 403'd by every agent-service surface. Silent
cross-service lockout with no diagnostic.

What this file guards
---------------------
1. The identity contract (Bug-8446), driven end-to-end through the real ASGI
   app over a fake session that evaluates the REAL ``WHERE`` clause, so a gate
   that regresses to ``==`` genuinely returns no row and the test goes red.
2. The structural property (Bug-8445): exactly one binding lookup exists in
   the service, every named gate delegates to it, and a sixth hand-rolled copy
   fails this file rather than shipping.
3. The two axes that had drifted silently and had no test at all: the
   admin-bypass predicate, and what happens to a non-human principal that
   reaches a gate.

Property 2's DOMAIN is derived, not written down
------------------------------------------------
This file used to prove property 2 by iterating a hardcoded five-entry tuple of
``(module, gate)`` pairs. An external reviewer then defeated all three of this
service's authorization guards simultaneously, by execution rather than by
reading, using one new module — and the hardcoded tuple was the load-bearing
hole: a gate in a module the tuple had never heard of was simply never checked
for delegation, so the route guard's certification-by-name had nothing standing
behind it. Three gates already shipped in the certification allowlist
(``_authorize_refresh_derived``, ``_require_project_access_and_agent``,
``_require_webhook_project_access``) were unchecked for exactly that reason.

The domain is now ``_PROJECT_SCOPED_GATES`` — the route guard's own
certification allowlist, imported rather than restated — walked over the real
``src/`` tree. See ``TestEveryCertifiedGateDelegates`` for the full argument.

Test escape: the pre-existing RBAC tests all used a caller whose JWT subject
was byte-identical to the stored binding, so no test could observe the
comparison being case-sensitive; no test enumerated the gates as a SET, so each
new copy was invisible until a human grepped for it; and once one did, the SET
it enumerated was frozen at the five gates that existed the day it was written.
Guard: this file. Tier: T1 (authorization contract).
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.util
import inspect
import pathlib
import re
import sys
import textwrap
import uuid
from unittest.mock import patch

import httpx
import pytest

from shared.auth.middleware import CurrentServiceUser
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from tests.test_bug_8356_webhook_project_idor import (
    PROJECT_A,
    PROJECT_B,
    TEST_TENANT,
    _BindingStore,
    _binding,
    _gen,
)
# The gate contract lives in ONE module, shared with
# ``test_project_route_authorization_coverage.py``. Importing it rather than
# restating any part of it is the fix this file exists to carry: every round
# this saga lost, it lost because two things that had to agree were written
# down twice. See tests/gate_contract.py.
from tests.gate_contract import (
    PROJECT_SCOPED_GATES as _PROJECT_SCOPED_GATES,
    TERMINAL_PRIMITIVES as _TERMINAL_PRIMITIVES,
    discover_gate_bindings as _discover_gate_bindings,
    gate_does_not_delegate as _gate_does_not_delegate,
    gate_names_bound_at_module_scope as _gate_names_bound_at_module_scope,
    identity_of as _identity_of,
    load_synthetic_module as _load_synthetic_module,
)

pytestmark = pytest.mark.unit

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
_PRIMITIVE = _SRC / "auth" / "project_access.py"

# Anti-vacuity floor, NOT the iteration domain. Discovery below decides which
# gates get checked; this only asserts the discovery walk still finds the ones
# we know exist, so a regression that empties it fails here instead of turning
# the delegation assertion into a no-op over an empty set. The previous version
# of this file made the opposite choice — it ITERATED a frozen 5-tuple — and an
# external gate defeated the whole guard family by adding a sixth gate the
# tuple had never heard of.
_KNOWN_GATE_DEFINITIONS = {
    ("src.api.agent_config", "_require_project_modeller"),
    ("src.api.agent_config", "_require_project_viewer"),
    ("src.api.agent_config", "_require_blocked_original_access"),
    ("src.api.agent_config", "_authorize_refresh_derived"),
    ("src.api.personas", "_require_project_modeller"),
    ("src.api.personas", "_require_project_viewer"),
    ("src.api.webhooks", "_require_webhook_project_access"),
    ("src.api.conversations", "_require_project_access_and_agent"),
}


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """Node ids of every DOCSTRING constant in *tree*.

    Prose is not code. Six of this service's modules describe
    ``UserAccessBinding`` in a docstring while correctly delegating, so a
    string match that cannot tell a docstring from a lookup either has to stay
    narrow (missing dynamic resolution) or floods with false positives. A guard
    that flags everything gets disabled, which is worse than one that misses a
    shape — so exclude docstrings and then match strings aggressively.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def _references_the_binding_table(source: str) -> bool:
    """True if *source* reaches ``user_access_bindings``.

    Five shapes, each added only after a review round proved the guard failed
    OPEN without it — a guard is exactly as good as the spellings it knows,
    and this file's history is three rounds of the DISCOVERY mechanism having
    the hole rather than the code:

    * the bare ORM name (original);
    * an attribute-qualified one, ``models.UserAccessBinding`` (R1 F4);
    * an alias import, ``from ... import UserAccessBinding as UAB`` — the
      alias is an ``ast.alias``, so neither of the above sees it and a sixth
      hand-rolled gate written that way was invisible (R2 finding 3, proven by
      executing this helper against the shape);
    * raw SQL naming the table;
    * the name as a STRING LITERAL anywhere outside a docstring, which covers
      ``getattr(models, "UserAccessBinding")``, a registry lookup
      (``Base.registry._class_registry["UserAccessBinding"]``), and any other
      dynamic attribute resolution. This was the last documented residual;
      docstring exclusion is what makes matching it practical, because six
      modules legitimately NAME the table in prose while delegating correctly.

    This helper is now defence in depth rather than the load-bearing check.
    The property "no module hand-rolls its own gate" is enforced positively by
    ``TestEveryCertifiedGateDelegates``: a hand-rolled gate must reach a
    terminal primitive no matter HOW it spells its own lookup, so a spelling
    this walk cannot see no longer buys an attacker a certified route.
    """
    tree = ast.parse(source)
    docstrings = _docstring_constant_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "UserAccessBinding":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "UserAccessBinding":
            return True
        if isinstance(node, ast.alias) and node.name == "UserAccessBinding":
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if (
                "user_access_bindings" in node.value
                or "UserAccessBinding" in node.value
            ):
                return True
    return False



def _reaches_a_session_factory(source: str) -> bool:
    """True if *source* can reach ``get_tenant_db`` without being handed it.

    Three shapes, because the factory is reachable by more than an
    ``ImportFrom``: a direct import, an aliased import, and
    ``import shared.db.session`` followed by an attribute access.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if "get_tenant_db" in (alias.name, alias.asname):
                    return True
        if isinstance(node, ast.Attribute) and node.attr == "get_tenant_db":
            return True
    return False


# ---------------------------------------------------------------------------
# Derived gate discovery (replaces the hardcoded ``_EXPECTED_GATES`` tuple)
# ---------------------------------------------------------------------------

_DISCOVERY_CACHE: tuple[dict[tuple[str, str], object], int] | None = None


def _gate_bindings() -> dict[tuple[str, str], object]:
    global _DISCOVERY_CACHE
    if _DISCOVERY_CACHE is None:
        _DISCOVERY_CACHE = _discover_gate_bindings()
    return _DISCOVERY_CACHE[0]


def _files_scanned() -> int:
    _gate_bindings()
    return _DISCOVERY_CACHE[1]


def _defined_gates() -> dict[tuple[str, str], object]:
    """Discovered bindings whose callable is DEFINED in the module that binds
    it — i.e. the real gates, not another module's gate re-exported into this
    one's namespace. A re-export still has to pass the delegation walk (it
    resolves to the real function), but it is not a second gate to drive.
    """
    return {
        key: fn
        for key, fn in _gate_bindings().items()
        if fn is not None and getattr(fn, "__module__", None) == key[0]
    }


# Gates the admin-bypass axis cannot drive by a direct two-argument call, each
# with the reason it is exempt. ``test_every_defined_gate_is_driven_or_declared
# _undrivable`` asserts this partition is exhaustive AND not stale, so a new
# gate with an unusual signature fails closed until somebody writes a reason.
_ADMIN_BYPASS_UNDRIVABLE: dict[tuple[str, str], str] = {
    ("src.api.conversations", "_require_project_access_and_agent"): (
        "signature is (db, project_id, current_user) — it takes its session as "
        "a PARAMETER, so there is no module-level get_tenant_db for "
        "_no_tenant_session_anywhere to explode and the parametrization above "
        "structurally cannot reach it. NOT skipped: the axis is asserted "
        "directly, on this gate, by "
        "TestAdminBypassAxisIsUniform."
        "test_the_conversations_gate_bypasses_the_binding_lookup_by_role, "
        "which drives it with a session double that fails on any statement "
        "touching user_access_bindings while allowing the legitimate post-gate "
        "agent-config read. (The earlier version of this reason cited "
        "tests/test_bug_5951_5952_5956_5957.py, which contains no admin-bypass "
        "assertion at all — an auditable skip backed by a citation that does "
        "not hold is worse than no citation, because the next reviewer trusts "
        "it. Opus5 R1 gate, finding F3.)"
    ),
    ("src.auth.project_access", "require_project_chat_access"): (
        "signature is (db, current_user, *, project_id, min_role) — it takes "
        "its session as a PARAMETER (it is a drop-in for the platform terminal "
        "it delegates to), so the (project_id, current_user) parametrization "
        "structurally cannot call it. NOT skipped: BOTH principal axes are "
        "asserted directly on this gate — the service-principal axis by "
        "TestNonHumanPrincipalsFailClosed."
        "test_the_chat_tier_refuses_a_service_principal_before_any_lookup, and "
        "the admin-bypass axis by TestAdminBypassAxisIsUniform."
        "test_the_chat_tier_bypasses_the_binding_lookup_by_role. It is also the "
        "gate BOTH chat route families reach, so the two HTTP-boundary tests in "
        "TestNonHumanPrincipalsFailClosed drive it through the real dependency "
        "chain as well."
    ),
}


# Discovered gate bindings whose RESOLVED callable neither principal axis
# drives, each with the reason. ``_defined_gates`` is keyed on "the callable is
# defined in the module that binds it", so a RE-EXPORT escapes that partition
# entirely. Most re-exports are harmless (they resolve to a callable another
# binding already drives), but a re-export of a callable defined OUTSIDE
# ``src/`` resolves to something no axis touches — and the only one in the
# service is the terminal that FAILS the axis. Partitioning by binding site
# rather than by resolved callable is what hid it (Opus5 R3 gate, F2): the
# blind spot was shaped precisely around the defect.
# Bug-8589 HALF A emptied this dict, and the emptying is itself the evidence.
# Its one entry was ``shared.auth.project_access:ensure_project_model_access``,
# re-exported into ``src.api.agent_config`` and ``src.api.conversations`` and
# FAILING the service-principal axis. Both route families now reach the
# service-local CHAT tier instead, which refuses a service principal by type,
# and the platform terminal is no longer a certified gate name — so no discovered
# binding resolves to a callable outside ``src/`` any more and there is nothing
# left to excuse. The mechanism stays (an empty exemption set is still asserted
# non-stale and still fails closed on the next un-driven binding); it is the
# EXEMPTION that is gone.
_PRINCIPAL_AXIS_UNCOVERED: dict[str, str] = {}


class _ExplodingSession:
    """A session that fails on ANY statement.

    A terminal must refuse a service principal BY TYPE, before it looks
    anything up — reaching the lookup at all is what let the bootstrap-open
    branch admit a principal that can never hold a binding.
    """

    @staticmethod
    async def execute(stmt):
        raise AssertionError(f"a statement ran before the refusal: {stmt}")


async def _exploding_session_factory(*_a, **_kw):
    raise AssertionError("a tenant session was opened before the refusal")
    yield  # pragma: no cover - generator shape only


# How to drive each TERMINAL primitive with a bare service principal. The two
# terminals do not share a signature (one takes an injected session FACTORY,
# the other an already-open session), so the property test below cannot call
# them uniformly — but the table is keyed on the terminal identity and asserted
# both exhaustive (no terminal without a driver) and non-stale (no driver
# without a terminal), so it cannot silently drift out of the contract.
_TERMINAL_SERVICE_DRIVERS = {
    "src.auth.project_access:require_project_role": lambda gate, caller: gate(
        PROJECT_A,
        caller,
        db_factory=_exploding_session_factory,
        roles=None,
        detail="probe",
    ),
    "src.auth.project_access:require_project_chat_access": (
        lambda gate, caller: gate(
            _ExplodingSession(),
            caller,
            project_id=PROJECT_A,
            min_role="viewer",
        )
    ),
}


def _directly_drivable_gates() -> list[tuple[str, str]]:
    """Defined gates callable as ``gate(project_id, current_user)``."""
    out = []
    for key, fn in sorted(_defined_gates().items()):
        params = list(inspect.signature(fn).parameters)
        if params[:2] == ["project_id", "current_user"]:
            out.append(key)
    return out


@contextlib.contextmanager
def _no_tenant_session_anywhere(reason: str):
    """Explode on ANY tenant-session acquisition, in EVERY imported ``src``
    module that holds a ``get_tenant_db`` symbol.

    Patching only the gate's own module is not enough once gates DELEGATE:
    ``webhooks._require_webhook_project_access`` runs ``agent_config``'s
    factory, so a webhooks-only patch could never fire and the assertion would
    pass vacuously no matter what the gate did.
    """

    async def _explode(*_a, **_kw):
        raise AssertionError(reason)
        yield  # pragma: no cover - generator shape only

    targets = [
        name
        for name, module in list(sys.modules.items())
        if name.startswith("src.")
        and module is not None
        and hasattr(module, "get_tenant_db")
    ]
    assert targets, "no src module exposes get_tenant_db — patch set is empty"
    with contextlib.ExitStack() as stack:
        for name in targets:
            stack.enter_context(patch(f"{name}.get_tenant_db", _explode))
        yield


def _user(user_id: str, role: str = "member") -> CurrentUser:
    return CurrentUser(
        user_id=user_id,
        tenant_id="__system__" if role == "system_admin" else TEST_TENANT,
        email=user_id,
        role=role,
    )


async def _request(method, path, *, bindings, caller, endpoint_module):
    store = _BindingStore(bindings)
    app.dependency_overrides[get_current_user] = lambda: caller
    try:
        with patch(f"{endpoint_module}.get_tenant_db", _gen(store)):
            with patch("src.api.agent_config.get_tenant_db", _gen(store)):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    return await client.request(method, path)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# One route per gate tier, so the identity contract is proven at every tier
# rather than at one convenient endpoint.
#   (label, method, path, endpoint_module, expected status once the gate passes)
_GATE_ROUTES = [
    (
        "agent_config viewer",
        "GET", f"/api/v1/projects/{PROJECT_A}/agent/config",
        "src.api.agent_config", 404,
    ),
    (
        # A bodyless route, deliberately: FastAPI validates a request body
        # BEFORE the handler runs, so a body-carrying route can return 422
        # without the gate ever executing — it would look green under a
        # mutation that breaks the gate. (Mutation-verified: the first draft
        # of this table used PUT /agent/models and stayed green when the
        # identity comparison was reverted to a raw ``==``.)
        "agent_config modeller (via rubrics)",
        "DELETE", f"/api/v1/projects/{PROJECT_A}/agent/rubrics/{uuid.uuid4()}",
        "src.api.rubrics", 404,
    ),
    (
        "blocked-original (strict)",
        "GET", f"/api/v1/projects/{PROJECT_A}/agent/calibration",
        "src.api.kpis", 200,
    ),
    (
        "personas viewer",
        "GET", f"/api/v1/projects/{PROJECT_A}/agent/personas",
        "src.api.personas", 200,
    ),
    (
        "personas modeller",
        "DELETE", f"/api/v1/projects/{PROJECT_A}/agent/personas/{uuid.uuid4()}",
        "src.api.personas", 404,
    ),
]


class TestIdentityComparisonIsCanonical:
    """Bug-8446 — the JWT subject and the stored binding may differ only in
    case. model-service authorises that caller; agent-service must too."""

    @pytest.mark.parametrize(
        "label,method,path,module,ok_status", _GATE_ROUTES,
        ids=[r[0] for r in _GATE_ROUTES],
    )
    @pytest.mark.asyncio
    async def test_case_differing_jwt_subject_is_authorized(
        self, label, method, path, module, ok_status
    ):
        resp = await _request(
            method, path,
            bindings=[
                _binding(
                    project_id=PROJECT_A, role="modeler",
                    user="probe@example.com",
                )
            ],
            caller=_user("Probe@Example.COM"),
            endpoint_module=module,
        )
        assert resp.status_code == ok_status, (
            f"{label}: a caller whose IdP returns 'Probe@Example.COM' against "
            f"a stored 'probe@example.com' binding was refused "
            f"({resp.status_code}) — model-service authorises exactly this "
            f"caller, so this is a silent cross-service lockout (Bug-8446)"
        )

    @pytest.mark.asyncio
    async def test_a_different_user_is_still_refused(self):
        """The canonicalisation must not degrade into 'any identity matches'."""
        resp = await _request(
            "GET", f"/api/v1/projects/{PROJECT_A}/agent/config",
            bindings=[
                _binding(
                    project_id=PROJECT_A, role="modeler",
                    user="someone-else@example.com",
                )
            ],
            caller=_user("probe@example.com"),
            endpoint_module="src.api.agent_config",
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_non_email_identity_is_compared_exactly(self):
        """``canonical_user_identity`` deliberately lower-cases ONLY email-
        shaped identities. A ``service:``-prefixed or opaque subject must keep
        exact-match semantics, or a case-insensitive collision becomes an
        authorization bypass."""
        resp = await _request(
            "GET", f"/api/v1/projects/{PROJECT_A}/agent/config",
            bindings=[
                _binding(project_id=PROJECT_A, role="modeler", user="AbC123")
            ],
            caller=_user("abc123"),
            endpoint_module="src.api.agent_config",
        )
        assert resp.status_code == 403


class TestExactlyOneBindingLookupExists:
    """Bug-8445 — the structural property. Five copies of one rule drift; the
    guard is that there is nowhere for a sixth copy to hide."""

    def test_no_module_outside_the_primitive_queries_user_access_binding(self):
        """R1 reviewer F4 — match every spelling of the lookup, not just one.

        The first version of this guard matched ``UserAccessBinding`` only as a
        bare ``ast.Name``. A sixth hand-rolled gate written
        ``from shared.db import models`` / ``select(models.UserAccessBinding)``
        is an ``ast.Attribute`` and was invisible: the guard passed and the
        exact drift this lane exists to prevent would have shipped. Raw SQL
        against the table name was invisible too. Both are matched now, and
        both directions are self-tested below — a coverage tool that fails OPEN
        on a shape it does not recognise is the CLAUDE.md blind-spot category.
        """
        offenders = []
        scanned = 0
        for path in sorted(_SRC.rglob("*.py")):
            if path == _PRIMITIVE:
                continue
            # utf-8-sig: seven files elsewhere in this repo carry a UTF-8 BOM
            # (Bug-8539), which makes ast.parse on a plain utf-8 read raise —
            # and a coverage guard that CRASHES stops enumerating the rest of
            # the tree. None are in this service today; read tolerantly so a
            # future one cannot silently hollow this guard out.
            source = path.read_text(encoding="utf-8-sig")
            scanned += 1
            if _references_the_binding_table(source):
                offenders.append(str(path.relative_to(_SRC)))
        assert scanned >= 30, (
            f"only {scanned} source files enumerated — the walk has regressed "
            "and this guard is asserting almost nothing"
        )
        assert not offenders, (
            "these modules reference UserAccessBinding directly instead of "
            "going through src/auth/project_access.py — that is how the five "
            "drifted copies (Bug-8445) came to exist:\n  "
            + "\n  ".join(offenders)
        )

    # ``test_the_guard_sees_an_attribute_access_not_only_a_bare_name`` used to
    # sit here. Deleted on the Opus5 R1 gate's finding F4: it re-implemented
    # the attribute check inline instead of calling
    # ``_references_the_binding_table``, so deleting the ``ast.Attribute``
    # branch from the production helper left it green. It asserted that one
    # hand-written expression matches another, and it is fully subsumed by
    # ``test_the_production_walk_flags_every_spelling[attribute-qualified ORM
    # name]``, which drives the real helper.

    @pytest.mark.parametrize(
        "source,label",
        [
            ("q = select(UserAccessBinding).where(x)", "bare ORM name"),
            (
                "from shared.db import models\n"
                "q = select(models.UserAccessBinding).where(x)",
                "attribute-qualified ORM name",
            ),
            (
                'q = text("SELECT 1 FROM user_access_bindings WHERE x")',
                "raw SQL against the table",
            ),
            (
                "from shared.db.models import UserAccessBinding as UAB\n"
                "q = select(UAB).where(UAB.user_identity == uid)\n",
                "alias-imported ORM name",
            ),
            (
                "from shared.db import models\n"
                'q = select(getattr(models, "UserAccessBinding")).where(x)\n',
                "dynamic getattr resolution (the last documented residual)",
            ),
            (
                'q = select(Base.registry._class_registry["UserAccessBinding"])',
                "registry lookup by string",
            ),
        ],
    )
    def test_the_production_walk_flags_every_spelling(self, source, label):
        """Drives the SAME helper the live assertion drives, over each shape a
        sixth copy could take. Self-testing the helper rather than only the
        assertion is what stops the two drifting apart."""
        assert _references_the_binding_table(source), label

    def test_the_production_walk_does_not_flag_unrelated_code(self):
        """Fail-closed must not become fail-always: a guard that flags
        everything gets disabled, which is worse than one that misses a shape."""
        assert not _references_the_binding_table(
            "from shared.db.models import Project\n"
            "q = select(Project).where(Project.id == pid)\n"
        )

    def test_a_docstring_mentioning_the_table_is_not_an_offender(self):
        """Prose is not a lookup. Six modules in this service describe
        ``UserAccessBinding`` in a docstring while delegating correctly; if the
        string match could not tell those apart it would flag them all, and the
        first person to hit that flood would delete the guard."""
        assert not _references_the_binding_table(
            '"""Bindings are checked via UserAccessBinding elsewhere."""\n'
            "async def gate(project_id, user):\n"
            '    """Reads user_access_bindings? No -- it delegates."""\n'
            "    await require_project_role(project_id, user)\n"
        )

    def test_the_primitive_does_not_import_a_session_factory(self):
        """The session factory is INJECTED by each wrapper, never imported
        here. That is what preserves the ``src.api.<module>.get_tenant_db``
        patch target a large number of existing tests bind to — and it is the
        exact constraint that blocked this consolidation in the previous
        lane."""
        assert not _reaches_a_session_factory(
            _PRIMITIVE.read_text(encoding="utf-8")
        )

    @pytest.mark.parametrize(
        "source,label",
        [
            ("from shared.db.session import get_tenant_db", "direct import"),
            (
                "from shared.db.session import get_tenant_db as _f",
                "aliased import",
            ),
            (
                "import shared.db.session\n"
                "async def g(p, u):\n"
                "    async for db in shared.db.session.get_tenant_db(u.tenant_id):\n"
                "        pass\n",
                "module import + attribute access",
            ),
        ],
    )
    def test_the_session_factory_walk_flags_every_reach(self, source, label):
        """Opus5 R3 gate + self-mutation — the walk was ``ImportFrom``-only, so
        ``import shared.db.session`` followed by
        ``shared.db.session.get_tenant_db(...)`` reached the factory just as
        directly and was invisible. Adding the attribute branch was not enough
        on its own: reverting it turned NOTHING red, because the only test
        drove the real primitive, which does not use that shape. Drive the
        helper over each shape so the branch is actually pinned."""
        assert _reaches_a_session_factory(source), label

    def test_the_session_factory_walk_does_not_flag_the_injection_pattern(self):
        """Fail-closed must not become fail-always: the primitive legitimately
        NAMES its injected parameter, and that must not count as reaching a
        factory."""
        assert not _reaches_a_session_factory(
            "async def require_project_role(project_id, user, *, db_factory):\n"
            "    async for db in db_factory(user.tenant_id):\n"
            "        pass\n"
        )


def _classify_synthetic(tmp_path, source: str) -> list[str]:
    """Run the PRODUCTION discovery + delegation walk over a synthetic module
    and return the failure reasons, so the counter-examples drive the same code
    the live assertion drives rather than a paraphrase of it."""
    source = textwrap.dedent(source)
    name, module = _load_synthetic_module(tmp_path, source)
    try:
        reasons = []
        for gate_name in sorted(_gate_names_bound_at_module_scope(source)):
            reason = _gate_does_not_delegate(
                name, gate_name, getattr(module, gate_name, None)
            )
            if reason is not None:
                reasons.append(reason)
        return reasons
    finally:
        sys.modules.pop(name, None)


# The chat tier's sanctioned callers. It is a certified gate that is WEAKER
# than the management tier on two axes it does not close, so where it is used
# is part of the contract, not a coincidence.
_CHAT_TIER_CALLERS = {
    ("src.api.conversations", "_require_project_access_and_agent"),
    ("src.api.agent_config", "list_selectable_models"),
}


class TestEveryCertifiedGateDelegates:
    """The delegation domain is DERIVED from the certification allowlist.

    Why this class replaced a five-entry tuple
    ------------------------------------------
    Three guards were supposed to make a hand-rolled authorization gate
    impossible to ship, and an external reviewer defeated all three at once by
    executing them — not by reading them — with a single new module:

    1. ``_references_the_binding_table`` had a documented ``getattr`` blind
       spot, so the new module's binding lookup was invisible to it.
    2. THIS assertion iterated a hardcoded five-tuple, so a gate in a module
       the tuple had never heard of was never checked for delegation at all.
       Two gates already in the certification allowlist
       (``_require_project_access_and_agent``, ``_require_webhook_project_access``)
       plus ``_authorize_refresh_derived`` were unchecked for the same reason,
       in shipped code, not just in the attack.
    3. ``test_project_route_authorization_coverage`` certifies a route when its
       handler awaits a bare ``Name`` that is a MEMBER of
       ``_PROJECT_SCOPED_GATES``. Certification by name is only as strong as the
       guarantee that every binding of that name is a real gate — and (2) meant
       there was no such guarantee.

    The structural correction is that (2) and (3) now share one source of truth.
    The domain of this check is not a list somebody remembers to update; it is
    ``_PROJECT_SCOPED_GATES`` itself, walked over the actual ``src/`` tree. A
    name cannot be added to the allowlist without every module-level binding of
    it having to prove delegation, and a hand-rolled gate under a name that is
    NOT in the allowlist does not certify a route in the first place — the
    route guard's fail-closed branch catches it.

    That is also why (1) stopped being load-bearing: how a hand-rolled gate
    spells its own binding lookup no longer decides whether it is caught. It is
    caught by not reaching a primitive. (The ``getattr`` spelling is closed
    anyway, above, as defence in depth.)

    Tier: T1 (authorization contract).
    """

    def test_discovery_is_not_vacuous(self):
        """An empty domain makes every assertion below trivially true. Assert
        the walk still finds the gates we know exist, and still reads the tree.

        ``_KNOWN_GATE_DEFINITIONS`` is a FLOOR, never the iteration domain —
        that distinction is the entire fix. Adding a gate must not require
        editing it; deleting the walk must fail here.
        """
        assert _files_scanned() >= 30, (
            f"only {_files_scanned()} source files enumerated — the walk has "
            "regressed and this guard is asserting almost nothing"
        )
        missing = sorted(_KNOWN_GATE_DEFINITIONS - set(_gate_bindings()))
        assert not missing, (
            "gate discovery no longer finds these known gates, so they are no "
            f"longer being checked for delegation: {missing}"
        )

    def test_every_module_level_binding_of_a_certified_name_delegates(self):
        """THE assertion. Every module-level binding, under any module in
        ``src/``, of any name the route guard certifies routes by, must reach
        a terminal authorization primitive."""
        bindings = _gate_bindings()
        assert bindings, "no certified gate names are bound anywhere under src/"
        failures = [
            reason
            for (module_name, gate_name), func in sorted(bindings.items())
            if (reason := _gate_does_not_delegate(module_name, gate_name, func))
        ]
        assert not failures, (
            "these bindings carry a name that CERTIFIES a route in "
            "test_project_route_authorization_coverage.py, but do not "
            "delegate to the service's authorization primitive — a route "
            "gated by one of them is gated by a string (Bug-8445):\n  "
            + "\n  ".join(failures)
        )

    def test_every_certified_name_is_accounted_for(self):
        """A name in the allowlist with no binding anywhere under ``src/`` is a
        name that certifies routes and is checked by nothing. Fail closed on
        it rather than letting the allowlist quietly outgrow the code."""
        bound = {name for _, name in _gate_bindings()}
        orphans = sorted(_PROJECT_SCOPED_GATES - bound)
        assert not orphans, (
            "these names certify routes in "
            "test_project_route_authorization_coverage.py but are bound "
            f"nowhere under src/, so nothing verifies them: {orphans}"
        )

    def test_the_terminal_primitives_resolve_to_the_real_functions(self):
        """An identity allowlist that has drifted from the code certifies
        nothing. Pin both terminals to the functions they name."""
        from src.auth.project_access import (
            require_project_chat_access,
            require_project_role,
        )

        assert {
            _identity_of(require_project_role),
            _identity_of(require_project_chat_access),
        } == set(_TERMINAL_PRIMITIVES)

    @pytest.mark.parametrize("identity", sorted(_TERMINAL_PRIMITIVES))
    @pytest.mark.asyncio
    async def test_every_terminal_primitive_refuses_a_service_principal(
        self, identity
    ):
        """Bug-8589 HALF A — the invariant the whole contract now rests on,
        asserted as a PROPERTY over the terminal set rather than as prose.

        ``gate_contract``'s comment and the conversational-agent architecture
        doc both now claim that certification means one thing on the
        principal-type axis, because BOTH accepted terminals refuse a service
        principal by type. Before this test that claim was enforced only by the
        hardcoded two-element identity pin above — which reds when a third
        terminal is added, but says nothing about what the third terminal DOES.
        Adding a permissive third terminal was therefore a one-line edit to the
        identity pin away from silently re-opening HALF A.

        Fails CLOSED on a terminal with no driver: a new terminal cannot be
        added without someone writing down how to drive it and watching it
        refuse.
        """
        from fastapi import HTTPException

        module_name, _, qualname = identity.partition(":")
        gate = getattr(importlib.import_module(module_name), qualname, None)
        assert gate is not None, (
            f"{identity} names no callable — the terminal allowlist has "
            "drifted from the code and certifies nothing"
        )

        driver = _TERMINAL_SERVICE_DRIVERS.get(identity)
        assert driver is not None, (
            f"{identity} was added to TERMINAL_PRIMITIVES with no driver in "
            "_TERMINAL_SERVICE_DRIVERS, so nothing proves it refuses a service "
            "principal. Every terminal must be shown to refuse one by type — "
            "that property is what makes 'this route is gated' mean one thing "
            "(Bug-8589 HALF A)."
        )

        caller = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )
        # A driver is hand-written, so it can raise a 403 of its OWN and never
        # touch the gate at all -- the table would then "prove" a refusal the
        # terminal never performed, and the exhaustiveness and staleness checks
        # around it are both blind to that. Executed by the Opus5 R2
        # confirmation gate: a driver that ignores ``gate`` and raises its own
        # HTTPException passed this test unchanged, for BOTH terminals. Hand
        # the driver a recording proxy instead of the bare gate; the record is
        # written only when the coroutine is actually awaited, so creating and
        # discarding it does not count either.
        # Recording ENTRY is not enough on its own: the Opus5 R3 confirmation
        # gate executed a driver that awaits the proxy, SWALLOWS the terminal's
        # refusal, and raises a 403 of its own -- ``entered`` was non-empty and
        # the test passed, for BOTH terminals, while proving nothing. Record the
        # refusal the terminal itself raised and require that the exception the
        # driver propagated IS that object.
        entered: list[str] = []
        raised: list[BaseException] = []

        async def _recording_gate(*args, **kwargs):
            entered.append(identity)
            try:
                return await gate(*args, **kwargs)
            except BaseException as terminal_refusal:
                raised.append(terminal_refusal)
                raise

        with pytest.raises(HTTPException) as exc:
            await driver(_recording_gate, caller)
        assert entered, (
            f"the driver for {identity} in _TERMINAL_SERVICE_DRIVERS raised "
            "without ever calling the terminal, so it proves nothing about "
            "what the terminal does"
        )
        assert raised and exc.value is raised[-1], (
            f"the driver for {identity} did not propagate the terminal's own "
            "refusal -- it either swallowed it and raised a 403 of its own, or "
            "the terminal returned and the driver refused on its behalf. "
            "Either way the refusal asserted below is the DRIVER's, not the "
            "terminal's"
        )
        assert exc.value.status_code == 403, (
            f"{identity} admitted a service principal holding only an "
            "unrelated scope"
        )

    def test_the_chat_tier_gates_the_chat_surfaces_and_nothing_else(self):
        """Bug-8589 HALF A left the certified set with two terminals of
        UNEQUAL strength (gate_contract rows 2 and 3: the chat tier admits an
        embed token, and a project with zero bindings bootstrap-opens through
        it -- HALF B, open). It certifies a route exactly as strongly as the
        management tier does, so a NON-chat surface gated by it silently
        acquires both, and every guard in this family stays green.

        The architecture doc states as a fact that this gate covers the nine
        conversation routes and selectable-models and nothing else. That was
        prose. This makes it a check.

        Tier: T1 (authorization contract).
        """
        from tests.gate_contract import module_name_for

        def _calls_under(node, stack, module_name, out):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _calls_under(child, stack + [child.name], module_name, out)
                    continue
                if isinstance(child, ast.Call):
                    fn = child.func
                    name = (
                        fn.id if isinstance(fn, ast.Name)
                        else fn.attr if isinstance(fn, ast.Attribute)
                        else None
                    )
                    # ``getattr(project_access, "require_project_chat_access")``
                    # resolves the weaker tier while naming it neither as a call
                    # target nor as an import alias, so BOTH checks below were
                    # blind to it. Executed by the Opus5 R3 confirmation gate:
                    # a route reaching the chat tier this way left this test
                    # green (the route guard did fail closed on it, which is why
                    # this is defence in depth rather than the only line).
                    if name == "getattr":
                        for arg in child.args:
                            if (
                                isinstance(arg, ast.Constant)
                                and arg.value == "require_project_chat_access"
                            ):
                                name = arg.value
                    if name == "require_project_chat_access":
                        # A call at MODULE scope or in a CLASS BODY has an empty
                        # stack. The first version skipped those (``and stack``)
                        # rather than reporting them, so a reference from a
                        # scope this contract never sanctioned passed silently.
                        # Fail closed on it: an unattributable call site is
                        # still a call site.
                        out.add((module_name, stack[-1] if stack else "<module scope>"))
                _calls_under(child, stack, module_name, out)

        importers: set[str] = set()
        callers: set[tuple[str, str]] = set()
        for path in sorted(_SRC.rglob("*.py")):
            module_name = module_name_for(path)
            if module_name == "src.auth.project_access":
                continue  # the definition site
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if "require_project_chat_access" in (
                            alias.name, alias.asname
                        ):
                            importers.add(module_name)
            _calls_under(tree, [], module_name, callers)

        assert callers == _CHAT_TIER_CALLERS, (
            "the chat tier is weaker than the management tier on the embed and "
            "bootstrap-open axes and certifies a route all the same, so where "
            "it is called is part of the authorization contract. Expected "
            f"{sorted(_CHAT_TIER_CALLERS)}, found {sorted(callers)}"
        )
        assert importers == {m for m, _ in _CHAT_TIER_CALLERS}, (
            "a module imports the chat tier without calling it, or calls it "
            "under an alias this walk cannot see: " + str(sorted(importers))
        )

    def test_the_terminal_driver_table_is_not_stale(self):
        """A driver for a terminal that no longer exists is dead weight that
        makes the table look more complete than it is."""
        stale = sorted(set(_TERMINAL_SERVICE_DRIVERS) - set(_TERMINAL_PRIMITIVES))
        assert not stale, (
            f"_TERMINAL_SERVICE_DRIVERS drives identities that are no longer "
            f"terminals: {stale}"
        )

    @pytest.mark.asyncio
    async def test_the_chat_terminal_actually_authorizes(self):
        """Bug-8589 HALF A — promoting a gate to TERMINAL means the delegation
        walk STOPS at it and never inspects what it does. That is a trust
        transfer, so the property the walk can no longer see has to be asserted
        BEHAVIOURALLY, not by reading its source: a caller bound to a DIFFERENT
        project is refused, and a caller bound to THIS one is admitted.

        Without this, ``require_project_chat_access`` could be emptied to a bare
        service-principal refusal and every route certified against it would
        stay green while authorizing no human at all.
        """
        from fastapi import HTTPException

        from src.auth.project_access import require_project_chat_access

        bound_elsewhere = _BindingStore([
            _binding(project_id=PROJECT_B, role="modeler"),
            # PROJECT_A must carry at least one binding of its own, or the
            # delegated terminal PER-PROJECT bootstrap-opens and ADMITS this
            # caller (Bug-8589 HALF B, still open). Without this row the
            # refusal below is an artifact of the session double, not a
            # property of the code -- proven by execution: stubbing
            # ``.first()`` faithfully turned this assertion red on its own.
            _binding(project_id=PROJECT_A, role="modeler",
                     user="someone-else@example.com"),
        ])
        with pytest.raises(HTTPException) as exc:
            await require_project_chat_access(
                bound_elsewhere, _user("probe@example.com"),
                project_id=PROJECT_A, min_role="viewer",
            )
        assert exc.value.status_code == 403, (
            "the chat terminal admitted a caller with no binding on the "
            "addressed project — it is trusted by the delegation walk while "
            "authorizing nothing"
        )

        bound_here = _BindingStore([_binding(project_id=PROJECT_A, role="modeler")])
        await require_project_chat_access(
            bound_here, _user("probe@example.com"),
            project_id=PROJECT_A, min_role="viewer",
        )

    # -- counter-examples: every way this check could fail OPEN --------------

    def test_a_hand_rolled_gate_under_a_certified_name_is_flagged(self, tmp_path):
        """The exact attack the external reviewer executed: a NEW module with
        its own binding lookup, under a name the route guard certifies. Note it
        spells the lookup with ``getattr`` — the spelling that used to be the
        documented blind spot — to prove the delegation check does not depend
        on recognising the lookup at all."""
        reasons = _classify_synthetic(tmp_path, '''
            from shared.db import models
            from sqlalchemy import select

            async def _require_project_viewer(project_id, current_user):
                async for db in _tenant_db(current_user.tenant_id):
                    q = select(getattr(models, "UserAccessBinding"))
                    if (await db.execute(q)).scalar_one_or_none():
                        return
                raise RuntimeError("denied")
        ''')
        assert len(reasons) == 1 and "_require_project_viewer" in reasons[0], reasons

    def test_a_locally_defined_impostor_primitive_is_flagged(self, tmp_path):
        """Delegation is resolved by ``module:qualname``, not by name. A local
        no-op called ``require_project_role`` must be a dead end — otherwise
        the delegation check is certifying a string too, which would just move
        the original defect one level down."""
        reasons = _classify_synthetic(tmp_path, '''
            async def require_project_role(project_id, current_user, **kw):
                return None

            async def _require_project_modeller(project_id, current_user):
                await require_project_role(project_id, current_user)
        ''')
        assert len(reasons) == 1, reasons
        assert "_require_project_modeller" in reasons[0]

    def test_module_scope_discovery_descends_every_compound_statement(self):
        """A gate bound inside a module-level ``if`` / ``try`` / ``with`` /
        loop / ``match`` is still a module attribute.

        ``match`` was the one this walk missed (Opus5 R2 gate, F5): an
        ``ast.Match`` holds its statements under ``cases[].body``, and the walk
        descended ``body``/``orelse``/``finalbody``/``handlers`` only. Not
        exploitable — the route side resolves through ``__globals__`` and would
        report the route ungated — but the module-inventory half must not have
        a shape it silently skips, which is the whole reason this file exists.
        """
        shapes = {
            "if": (
                "import os\n"
                "if os.environ:\n"
                "    async def _require_project_viewer(project_id, u): ...\n"
            ),
            "try/except": (
                "try:\n"
                "    from .nope import _require_project_viewer\n"
                "except ImportError:\n"
                "    async def _require_project_viewer(project_id, u): ...\n"
            ),
            "try/finally": (
                "try:\n"
                "    pass\n"
                "finally:\n"
                "    async def _require_project_viewer(project_id, u): ...\n"
            ),
            "for": (
                "for _ in (1,):\n"
                "    async def _require_project_viewer(project_id, u): ...\n"
            ),
            "with": (
                "import contextlib\n"
                "with contextlib.suppress(Exception):\n"
                "    async def _require_project_viewer(project_id, u): ...\n"
            ),
            "match": (
                "import os\n"
                "match os.name:\n"
                "    case _:\n"
                "        async def _require_project_viewer(project_id, u): ...\n"
            ),
        }
        missed = [
            label
            for label, source in shapes.items()
            if "_require_project_viewer"
            not in _gate_names_bound_at_module_scope(source)
        ]
        assert not missed, (
            "module-scope discovery does not descend into these compound "
            f"statements, so a gate bound there is not inventoried: {missed}"
        )

    def test_a_module_level_alias_to_a_hand_rolled_check_is_flagged(self, tmp_path):
        """``_require_project_viewer = _my_check`` binds a certified name
        without a ``def``, which is the cheapest way to defeat a scan that only
        looks for function definitions."""
        reasons = _classify_synthetic(tmp_path, '''
            async def _my_check(project_id, current_user):
                return None

            _require_project_viewer = _my_check
        ''')
        assert len(reasons) == 1, reasons
        assert "_require_project_viewer" in reasons[0]

    def test_an_aliased_import_of_a_hand_rolled_check_is_flagged(self, tmp_path):
        """``from x import y as _require_project_viewer`` — same defeat, via
        the import statement."""
        helper_name, _ = _load_synthetic_module(tmp_path, textwrap.dedent('''
            async def wide_open(project_id, current_user):
                return None
        '''))
        try:
            sys.path.insert(0, str(tmp_path))
            reasons = _classify_synthetic(tmp_path, f'''
                from {helper_name} import wide_open as _require_project_viewer
            ''')
        finally:
            sys.path.remove(str(tmp_path))
            sys.modules.pop(helper_name, None)
        assert len(reasons) == 1, reasons
        assert "_require_project_viewer" in reasons[0]

    def test_a_gate_that_never_passes_project_id_onward_is_flagged(self, tmp_path):
        """The Bug-8356 shape, one level deeper: the gate DOES reach the real
        primitive, but never tells it which project. Certifying that would
        endorse a cross-project IDOR that reads as correct."""
        reasons = _classify_synthetic(tmp_path, '''
            from src.auth.project_access import require_project_role

            async def _require_project_viewer(project_id, current_user):
                await require_project_role(None, current_user, db_factory=None)
        ''')
        assert len(reasons) == 1, reasons
        assert "_require_project_viewer" in reasons[0]

    def test_a_genuine_multi_hop_delegation_is_accepted(self, tmp_path):
        """Fail-closed must not become fail-always. The real
        ``_require_webhook_project_access`` is two hops from the primitive, so
        a walk that only looked one level down would break production code.
        """
        reasons = _classify_synthetic(tmp_path, '''
            from src.api.agent_config import _require_blocked_original_access

            async def _require_project_viewer(project_id, current_user):
                await _require_blocked_original_access(project_id, current_user)
        ''')
        assert reasons == []

    def test_a_delegation_cycle_fails_closed(self, tmp_path):
        """Two gates awaiting each other reach no primitive. The walk must
        report that, not recurse until the interpreter gives up."""
        reasons = _classify_synthetic(tmp_path, '''
            async def _require_project_viewer(project_id, current_user):
                await _require_project_modeller(project_id, current_user)

            async def _require_project_modeller(project_id, current_user):
                await _require_project_viewer(project_id, current_user)
        ''')
        assert len(reasons) == 2, reasons

    def test_a_gate_that_rebinds_the_terminal_at_function_scope_is_flagged(
        self, tmp_path
    ):
        """The route half's rebinding hole, mirrored on the gate half.

        ``delegation_trace`` resolves an awaited name through the defining
        function's ``__globals__``. A gate whose module legitimately imports
        ``require_project_role`` — every real gate module does — can shadow it
        inside its own body and still present a clean delegation trace. Proven
        by execution (Opus5 R4 gate): this gate reported ``delegates? True``
        while authorizing nothing.
        """
        reasons = _classify_synthetic(tmp_path, '''
            from src.auth.project_access import require_project_role


            async def _wide_open(project_id, current_user, **kw):
                return None


            async def _require_project_modeller(project_id, current_user):
                require_project_role = _wide_open
                await require_project_role(project_id, current_user)
        ''')
        assert len(reasons) == 1, reasons
        assert "_require_project_modeller" in reasons[0]

    def test_a_decorated_gate_fails_closed(self, tmp_path):
        """Opus5 R1 gate finding F2 — the walk used ``inspect.unwrap``, which
        follows ``__wrapped__`` straight past the wrapper to the undecorated
        body. A ``@functools.wraps`` decorator that returns early under some
        condition therefore presented a CLEAN delegation while skipping the
        gate at runtime: the trace read
        ``_require_project_viewer -> require_project_role`` and the route was
        certified, with the gate bypassable by an env var.

        No gate in this service is decorated today, so refusing outright costs
        nothing now and forces a written decision if one ever appears. The
        walk cannot reason about a wrapper's control flow, so the only honest
        answers are "refuse" or "prove the wrapper always calls through", and
        refusing is the fail-closed one.
        """
        reasons = _classify_synthetic(tmp_path, '''
            import functools
            import os

            from src.auth.project_access import require_project_role

            def maybe(fn):
                @functools.wraps(fn)
                async def inner(*a, **kw):
                    if os.getenv("SKIP_AUTH"):
                        return None
                    return await fn(*a, **kw)
                return inner

            @maybe
            async def _require_project_viewer(project_id, current_user):
                await require_project_role(
                    project_id, current_user, db_factory=None,
                )
        ''')
        assert len(reasons) == 1, reasons
        assert "decorated" in reasons[0], reasons[0]

    def test_a_one_hop_delegation_to_the_chat_tier_is_accepted(self, tmp_path):
        """A one-hop delegation to the service's embed-aware CHAT tier is the
        shape ``conversations`` uses, and must be accepted."""
        reasons = _classify_synthetic(tmp_path, '''
            from src.auth.project_access import require_project_chat_access

            async def _require_project_viewer(db, project_id, current_user):
                await require_project_chat_access(
                    db, current_user, project_id=project_id, min_role="viewer",
                )
        ''')
        assert reasons == []

    def test_the_platform_terminal_alone_no_longer_certifies(self, tmp_path):
        """Bug-8589 HALF A, the structural half — and the exact inverse of what
        this test asserted before.

        It used to assert that a one-hop delegation to
        ``shared.auth.project_access.ensure_project_model_access`` was accepted,
        because that was the shape ``conversations`` and
        ``agent_config.list_selectable_models`` used. That acceptance was the
        hole: the platform terminal ADMITS a ``CurrentServiceUser`` on the
        assumption the ROUTE already scope-authorized one, both of those route
        families gate on ``require_capability('chat')``, which has no service
        branch, and so both were open to any service token in the tenant.

        Accepting that shape is therefore no longer safe, and the guard now says
        so: a gate that reaches only the platform terminal is a DEAD END. The
        chat tier is the one sanctioned way to reach it from agent-service, and
        it supplies the missing principal-type refusal. A new gate written the
        old way now fails this contract instead of shipping a fourth instance of
        the same defect.
        """
        reasons = _classify_synthetic(tmp_path, '''
            from shared.auth.project_access import ensure_project_model_access

            async def _require_project_viewer(db, project_id, current_user):
                await ensure_project_model_access(
                    db, current_user, project_id=project_id, min_role="viewer",
                )
        ''')
        assert len(reasons) == 1, reasons
        assert "ensure_project_model_access" in reasons[0], reasons[0]


class TestAdminBypassAxisIsUniform:
    """The drift axis nobody had recorded: ``is_human_tenant_admin`` in
    agent_config's two gates vs ``is_human_tenant_admin_or_system_admin`` in
    personas' two and in the STRICTEST gate. A canonical human system admin was
    admitted by the strongest gate and refused by the weakest ones.

    The domain is DERIVED, for the same reason the delegation domain is: this
    parametrization used to iterate the frozen five-tuple, so the two gates
    added since (``_authorize_refresh_derived``,
    ``_require_webhook_project_access``) were never checked on this axis at all.
    """

    @pytest.mark.parametrize(
        "module_name,gate_name",
        _directly_drivable_gates(),
        ids=[f"{m.rsplit('.', 1)[-1]}.{g}" for m, g in _directly_drivable_gates()],
    )
    @pytest.mark.asyncio
    async def test_system_admin_passes_every_gate_by_role(
        self, module_name, gate_name
    ):
        """Called directly rather than through a route: the gate and its
        handlers share one ``get_tenant_db`` symbol, so an exploding factory
        driven through the ASGI app would also blow up the handler's own
        legitimate session and prove nothing about the gate."""
        gate = getattr(importlib.import_module(module_name), gate_name)

        with _no_tenant_session_anywhere(
            f"{module_name}.{gate_name}: a privileged-by-role caller must "
            "not reach the bindings table"
        ):
            await gate(
                PROJECT_A, _user("root@tessallite.local", role="system_admin")
            )
            await gate(
                PROJECT_A, _user("boss@example.com", role="tenant_admin")
            )

    @pytest.mark.asyncio
    async def test_the_conversations_gate_bypasses_the_binding_lookup_by_role(self):
        """The one gate the parametrization above structurally cannot reach.

        Opus5 R1 gate finding F3: it was carved out of the axis with a reason
        claiming another file covered it, and that file contains no
        admin-bypass assertion at all — so the axis had NO coverage for this
        gate anywhere in agent-service. Asserted here directly instead.

        The carve-out's stated obstacle (an exploding session cannot tell the
        binding lookup apart from the legitimate post-gate agent-config read)
        is real for a blunt explode-on-everything double, and is solved by
        looking at the STATEMENT rather than at the call count: a privileged
        caller must produce the config read and must NOT produce anything
        touching ``user_access_bindings``.

        Mutation-proof: remove ``_has_admin_bypass`` from
        ``shared/auth/project_access.py`` and this goes red, because
        ``ensure_project_model_access`` then issues its binding select.
        """
        from types import SimpleNamespace

        from src.api.conversations import _require_project_access_and_agent

        executed: list[str] = []

        class _Result:
            @staticmethod
            def scalar_one_or_none():
                return SimpleNamespace(enabled=True, project_id=PROJECT_A)

        class _Db:
            @staticmethod
            async def execute(stmt):
                sql = str(stmt)
                executed.append(sql)
                assert "user_access_bindings" not in sql, (
                    "a privileged-by-role caller reached the bindings table: "
                    f"{sql}"
                )
                return _Result()

        for role in ("system_admin", "tenant_admin"):
            executed.clear()
            config = await _require_project_access_and_agent(
                _Db(), PROJECT_A, _user(f"root-{role}@tessallite.local", role=role)
            )
            assert config is not None, role
            assert executed, (
                f"{role}: no statement ran at all — the double never got the "
                "post-gate agent-config read, so this assertion proved nothing"
            )

    @pytest.mark.asyncio
    async def test_the_chat_tier_bypasses_the_binding_lookup_by_role(self):
        """The second gate the parametrization structurally cannot reach
        (Bug-8589 HALF A added it; ``_ADMIN_BYPASS_UNDRIVABLE`` names this test
        as its coverage).

        The refusal the chat tier adds is by principal TYPE, and a human tenant
        admin / canonical system admin is not that type — so the admin bypass
        must still short-circuit ahead of any binding lookup, exactly as it does
        on every other tier. A refusal written as "anything that is not a
        plainly-bound member" would pass every service-principal test in this
        file and lock every admin out of chat.
        """
        from src.auth.project_access import require_project_chat_access

        class _Db:
            @staticmethod
            async def execute(stmt):
                raise AssertionError(
                    "a privileged-by-role caller reached a lookup instead of "
                    f"being bypassed: {stmt}"
                )

        for role in ("system_admin", "tenant_admin"):
            await require_project_chat_access(
                _Db(), _user(f"root-{role}@tessallite.local", role=role),
                project_id=PROJECT_A, min_role="viewer",
            )

    def test_every_defined_gate_is_driven_or_declared_undrivable(self):
        """Fail CLOSED on a gate this axis cannot reach.

        A gate whose signature is not ``(project_id, current_user)`` cannot be
        driven by the parametrization above. Silently skipping it is how a
        frozen enumeration becomes a blind spot, so every defined gate must be
        either driven or listed in ``_ADMIN_BYPASS_UNDRIVABLE`` with a written
        reason.
        """
        defined = set(_defined_gates())
        driven = set(_directly_drivable_gates())
        unaccounted = sorted(
            defined - driven - set(_ADMIN_BYPASS_UNDRIVABLE)
        )
        assert not unaccounted, (
            "these gates are neither driven by the admin-bypass "
            "parametrization nor declared undrivable with a reason:\n  "
            + "\n  ".join(f"{m}.{g}" for m, g in unaccounted)
        )
        stale = sorted(set(_ADMIN_BYPASS_UNDRIVABLE) - defined)
        assert not stale, (
            f"_ADMIN_BYPASS_UNDRIVABLE names gates that no longer exist: {stale}"
        )

    def test_every_undrivable_reason_cites_tests_that_actually_exist(self):
        """An auditable skip is only as good as its citation.

        This file has already been burned by exactly this: the conversations
        entry's ORIGINAL reason cited a file containing no admin-bypass
        assertion at all, so the axis had NO coverage for that gate anywhere in
        the service while looking fully accounted for (Opus5 R1 gate, F3). The
        staleness check above verifies the exempted GATE still exists; nothing
        verified the tests the reason promises instead.

        The dict has since grown a second entry (Bug-8589 HALF A's chat tier),
        which doubles the chance of recurrence, so the citation is now checked
        mechanically: every ``test_*`` name a reason names must be a test
        defined in THIS module.

        File paths are excluded — a reason may legitimately name another test
        FILE (the conversations entry does, precisely to record the citation
        that did not hold).
        """
        tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }

        # PER ENTRY, not in aggregate. The previous version unioned every
        # reason's citations and asserted the UNION was non-empty, so a third
        # entry citing nothing at all -- or citing in a spelling the regex
        # cannot see -- was carried by the two entries that did cite. Executed
        # by the Opus5 R2 confirmation gate: both shapes passed. An exemption
        # that cannot name its own coverage is an unbacked claim, which is the
        # exact defect this test was added for (Opus5 R1 gate, F3).
        problems: list[str] = []
        for (module_name, gate_name), reason in sorted(
            _ADMIN_BYPASS_UNDRIVABLE.items()
        ):
            cited: set[str] = set()
            # A citation is written ``ClassName.test_name``, so the preceding
            # dot must NOT be excluded; a file path ``tests/test_x.py`` must be.
            for match in re.finditer(r"(?<![\w/])test_[a-z0-9_]+", reason):
                if reason[match.end():match.end() + 3] == ".py":
                    continue  # a file path, not a test function
                cited.add(match.group(0))
            if not cited:
                problems.append(
                    f"{module_name}.{gate_name}: its reason names no test at "
                    "all, so the claim that the axis is covered elsewhere is "
                    "backed by nothing"
                )
                continue
            missing = sorted(cited - defined)
            if missing:
                problems.append(
                    f"{module_name}.{gate_name}: cites {missing}, which are "
                    "not defined in this module"
                )
        assert not problems, (
            "_ADMIN_BYPASS_UNDRIVABLE excuses these gates from the driven "
            "parametrization by promising other tests cover them instead:\n  "
            + "\n  ".join(problems)
        )

    def test_every_discovered_binding_is_covered_by_the_principal_axes(self):
        """Fail CLOSED on a gate binding neither principal axis reaches.

        The partition above is over ``_defined_gates()``, which is keyed on the
        BINDING SITE — "the callable is defined in the module that binds it".
        Every other discovered binding is a re-export; the in-``src/`` ones
        resolve to a callable another binding already drives, but a re-export
        of a callable defined OUTSIDE ``src/`` resolves to something no axis
        touches and was dropped with no written reason.

        Exactly one such binding used to exist, and it was the one that FAILED
        the axis (Bug-8589 HALF A: the platform terminal, re-exported into two
        api modules) — which is why this partition must be over RESOLVED
        CALLABLES, not over binding sites. Had it been, the residual would have
        gone red on day one and forced the written reason that surfaces the
        second affected route family. Opus5 R3 gate, F2.

        HALF A's fix removed it: both chat route families now reach the
        service-local chat tier, and the platform terminal is no longer a
        certified gate name, so ``_PRINCIPAL_AXIS_UNCOVERED`` is empty. The
        partition must STAY over resolved callables — an empty exemption set is
        the property being protected here, not a reason to relax the keying.
        """
        driven = set(_directly_drivable_gates()) | set(_ADMIN_BYPASS_UNDRIVABLE)
        covered = {
            _identity_of(getattr(importlib.import_module(module_name), gate_name))
            for module_name, gate_name in driven
        }
        discovered = {_identity_of(fn) for fn in _gate_bindings().values()}
        assert discovered, "gate discovery is empty — this asserts nothing"
        unaccounted = sorted(
            discovered - covered - set(_PRINCIPAL_AXIS_UNCOVERED)
        )
        assert not unaccounted, (
            "these gate bindings resolve to a callable that neither the "
            "admin-bypass axis nor the service-principal axis ever drives, and "
            "no reason is recorded for them:\n  " + "\n  ".join(unaccounted)
        )
        stale = sorted(set(_PRINCIPAL_AXIS_UNCOVERED) - discovered)
        assert not stale, (
            "_PRINCIPAL_AXIS_UNCOVERED names callables that no binding under "
            f"src/ resolves to any more: {stale}"
        )

    @pytest.mark.parametrize(
        "label,method,path,module,ok_status", _GATE_ROUTES,
        ids=[r[0] for r in _GATE_ROUTES],
    )
    @pytest.mark.asyncio
    async def test_system_admin_is_not_403d_on_any_gated_route(
        self, label, method, path, module, ok_status
    ):
        resp = await _request(
            method, path, bindings=[],
            caller=_user("root@tessallite.local", role="system_admin"),
            endpoint_module=module,
        )
        assert resp.status_code != 403, (
            f"{label}: canonical human system admin refused ({resp.status_code})"
        )


class TestNonHumanPrincipalsFailClosed:
    """A service principal has no ``UserAccessBinding`` — model-service only
    ever writes bindings for human identities — so before this fix it fell
    through the lookup, missed, and then hit the BOOTSTRAP-OPEN branch, which
    ADMITS the caller in any tenant that has no bindings yet. Refusing by
    principal type is strictly fail-closed (AUTH-RR-01).

    Opus5 R2 gate: this invariant used to be asserted against ONE hand-picked
    caller (``agent_config._require_project_modeller``) while every other gate
    shared the same certification contract and the same exposure. That is
    exactly CLAUDE.md's shared-primitive hardening rule — "the bug was reported
    against caller A, so I fixed caller A" is an incomplete fix by definition.
    The axis is now ENUMERATED over every discovered gate binding — by RESOLVED
    CALLABLE, not by binding site, because the binding-site partition dropped
    re-exports and the only re-export that resolved outside ``src/`` was exactly
    the terminal that failed the axis (Opus5 R3 gate, F2). The two gates that
    failed it carried ``xfail(strict=True)`` markers against Bug-8589 rather
    than being quietly excluded, one per certified route family.

    Bug-8589 HALF A closed both, and the markers are gone (``strict=True`` did
    its job: the fix XPASSed them and forced their removal). The chat surfaces
    are no longer gated by the platform terminal directly but by
    ``src.auth.project_access.require_project_chat_access``, which refuses a
    ``CurrentServiceUser`` by type and then delegates the embed-aware human
    decision unchanged. Every accepted terminal in the contract now refuses a
    service principal by type, so this axis holds by construction rather than
    per-caller. HALF B (bootstrap-open scoped per project) is a different axis
    and is still open.
    """

    @pytest.mark.parametrize(
        "module_name,gate_name",
        _directly_drivable_gates(),
        ids=[f"{m.rsplit('.', 1)[-1]}.{g}" for m, g in _directly_drivable_gates()],
    )
    @pytest.mark.asyncio
    async def test_every_drivable_gate_refuses_an_unrelated_service_principal(
        self, module_name, gate_name
    ):
        """A service token minted for some OTHER purpose must not authorize a
        project surface. ``_authorize_refresh_derived`` is included on purpose:
        it is the one gate that deliberately admits a service principal, but
        only one carrying ``SCOPE_AGENT_REFRESH``, so an unrelated scope must
        still be refused — that narrowness is the whole point of the bypass and
        nothing was asserting it."""
        from fastapi import HTTPException

        gate = getattr(importlib.import_module(module_name), gate_name)
        caller = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )
        with _no_tenant_session_anywhere(
            f"{module_name}.{gate_name}: a service principal must be refused "
            "by TYPE, before any binding lookup — reaching the lookup is what "
            "let the bootstrap-open branch admit it"
        ):
            with pytest.raises(HTTPException) as exc:
                await gate(PROJECT_A, caller)
        assert exc.value.status_code == 403, (
            f"{module_name}.{gate_name} admitted a service principal holding "
            "only an unrelated scope"
        )

    @pytest.mark.asyncio
    async def test_the_conversations_gate_refuses_a_service_principal(self):
        """Opus5 R2 gate finding 2, executed: the gate issued only the
        agent-config read and never touched ``user_access_bindings``, i.e. it
        admitted a service token holding an unrelated scope to another
        project's conversation content."""
        from types import SimpleNamespace

        from fastapi import HTTPException

        from src.api.conversations import _require_project_access_and_agent

        executed: list[str] = []

        class _Result:
            @staticmethod
            def scalar_one_or_none():
                return SimpleNamespace(enabled=True, project_id=PROJECT_A)

            @staticmethod
            def first():
                return None

        class _Db:
            @staticmethod
            async def execute(stmt):
                executed.append(str(stmt))
                return _Result()

        caller = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )
        with pytest.raises(HTTPException) as exc:
            await _require_project_access_and_agent(_Db(), PROJECT_A, caller)
        assert exc.value.status_code == 403, (
            "a service principal holding an unrelated scope was admitted to "
            f"this project's conversations; statements run: {executed}"
        )

    @pytest.mark.asyncio
    async def test_the_selectable_models_route_refuses_a_service_principal(self):
        from types import SimpleNamespace

        from fastapi import HTTPException

        import src.api.agent_config as agent_config

        executed: list[str] = []

        class _Result:
            @staticmethod
            def scalar_one_or_none():
                return None

            @staticmethod
            def first():
                return None

            @staticmethod
            def scalars():
                return SimpleNamespace(all=lambda: [])

        class _Db:
            @staticmethod
            async def execute(stmt):
                executed.append(" ".join(str(stmt).split()))
                return _Result()

        async def _session(*_a, **_kw):
            yield _Db()

        caller = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )
        with patch("src.api.agent_config.get_tenant_db", _session):
            with pytest.raises(HTTPException) as exc:
                await agent_config.list_selectable_models(PROJECT_A, caller)
        assert exc.value.status_code == 403, (
            "a service principal holding only an unrelated scope read this "
            f"project's agent allow-list; statements run: {executed}"
        )

    @pytest.mark.asyncio
    async def test_the_chat_tier_refuses_a_service_principal_before_any_lookup(
        self,
    ):
        """Bug-8589 HALF A — the axis asserted directly on the gate BOTH chat
        route families share, because its ``(db, current_user, *, project_id)``
        signature keeps the parametrized sweep above from reaching it (declared
        in ``_ADMIN_BYPASS_UNDRIVABLE`` with that reason).

        "Before any lookup" is the load-bearing half: reaching the delegated
        terminal at all is what let the bootstrap-open branch admit a principal
        that can never hold a binding. The session double therefore fails on
        ANY statement, not just on a binding query.
        """
        from fastapi import HTTPException

        from src.auth.project_access import require_project_chat_access

        class _Db:
            @staticmethod
            async def execute(stmt):
                raise AssertionError(
                    "a service principal must be refused by TYPE, before any "
                    f"statement runs; this one ran: {stmt}"
                )

        caller = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )
        with pytest.raises(HTTPException) as exc:
            await require_project_chat_access(
                _Db(), caller, project_id=PROJECT_A, min_role="viewer",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_the_embed_caller_is_still_admitted_by_the_chat_tier(self):
        """The refusal must not overshoot. An embed token is a FIRST-CLASS chat
        caller (the conversational-client widget and the Excel plugin both
        present one), and it is the only reason the chat surfaces could not
        simply reuse ``require_project_role``, which refuses embed tokens by
        type. Refusing service principals by copying that posture wholesale
        would have silently killed every embedded chat deployment — a
        product-outage-shaped overshoot that no service-principal test would
        have caught.
        """
        from src.auth.middleware import CurrentEmbedUser
        from src.auth.project_access import require_project_chat_access

        class _Db:
            @staticmethod
            async def execute(stmt):  # pragma: no cover - must not be reached
                raise AssertionError(
                    "an embed token is decided by its own token scope, not by "
                    f"a binding lookup; this ran: {stmt}"
                )

        embed = CurrentEmbedUser(
            user_id="embed-user",
            tenant_id=TEST_TENANT,
            email="embed-user@example.com",
            capabilities=["chat"],
            project_ids=[str(PROJECT_A)],
        )
        await require_project_chat_access(
            _Db(), embed, project_id=PROJECT_A, min_role="viewer",
        )

    @pytest.mark.asyncio
    async def test_a_conversation_route_403s_a_service_principal_over_http(self):
        """END TO END through the real app: the real ``require_capability('chat')``
        dependency, the real handler, the real gate.

        The direct-call tests above prove the gate; only this proves the ROUTE
        is actually wired to it. Pre-fix this request returned 404 (the gate
        admitted the service token and the handler's agent-config lookup missed
        on the double), not 403 — so the two statuses discriminate the fix.
        """
        caller = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )
        resp = await _request(
            "GET", f"/api/v1/projects/{PROJECT_A}/agent/conversations",
            bindings=[], caller=caller,
            endpoint_module="src.api.conversations",
        )
        assert resp.status_code == 403, (
            "a service token holding only an unrelated scope reached this "
            f"project's conversation list over HTTP ({resp.status_code})"
        )

    @pytest.mark.asyncio
    async def test_selectable_models_403s_a_service_principal_over_http(self):
        """The SECOND route family, end to end. Pre-fix this returned 200 with
        the project's model picker contents."""
        caller = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )
        resp = await _request(
            "GET", f"/api/v1/projects/{PROJECT_A}/agent/selectable-models",
            bindings=[], caller=caller,
            endpoint_module="src.api.agent_config",
        )
        assert resp.status_code == 403, (
            "a service token holding only an unrelated scope read this "
            f"project's agent model picker over HTTP ({resp.status_code})"
        )

    @pytest.mark.asyncio
    async def test_service_principal_is_refused_in_a_zero_binding_tenant(self):
        from src.api.agent_config import _require_project_modeller
        from fastapi import HTTPException

        caller = CurrentServiceUser(
            principal="glossary-bootstrap-service",
            tenant_id=TEST_TENANT,
            role="tenant_admin",
            scopes=["optimizer.stats-refresh"],
        )

        async def _explode(*_a, **_kw):
            raise AssertionError(
                "a service principal must be refused by TYPE, before any "
                "binding lookup — reaching the lookup is what let the "
                "bootstrap-open branch admit it"
            )
            yield

        with patch("src.api.agent_config.get_tenant_db", _explode):
            with pytest.raises(HTTPException) as exc:
                await _require_project_modeller(PROJECT_A, caller)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_human_with_no_binding_is_denied_in_a_zero_binding_tenant(self):
        """F-021-04 HARD CUTOVER (Wave C decision #9, Bug-9442).

        The old ``bootstrap_open`` first-run posture (decision D2) admitted any
        authenticated human on a binding-less project's CONFIGURATION surfaces.
        Decision #9 removed that first-arriver grant everywhere: a binding-less
        project is a legacy state only a human tenant/system admin may repair,
        never a self-service claim. A plain member with no binding is now
        denied on the same configuration tier that used to admit them — the
        exact zero-binding tenant-isolation hole this lane closes.
        """
        from fastapi import HTTPException

        from src.api.agent_config import _require_project_modeller

        store = _BindingStore([])
        with patch("src.api.agent_config.get_tenant_db", _gen(store)):
            with pytest.raises(HTTPException) as exc:
                await _require_project_modeller(
                    PROJECT_A, _user("new@example.com")
                )
        assert exc.value.status_code == 403, (
            "a human member with no binding was admitted to a binding-less "
            "project's agent configuration — the zero-binding bootstrap grant "
            "F-021-04 removed (Bug-9442) is still live"
        )


# The two chat-capable route families, with the status each returns once the
# gate ADMITS, against the empty handler-side store. Asserting the exact
# post-gate status (not merely "!= 403") proves the request really reached the
# handler rather than being let through somewhere else.
_CHAT_ROUTES = [
    (
        "conversations list",
        "GET", f"/api/v1/projects/{PROJECT_A}/agent/conversations",
        "src.api.conversations", 404,
    ),
    (
        "selectable-models",
        "GET", f"/api/v1/projects/{PROJECT_A}/agent/selectable-models",
        "src.api.agent_config", 200,
    ),
]


class TestChatSurfacesStayReachableByTheirLegitimateCallers:
    """Bug-8589 HALF A, the ADMISSION direction, at the ROUTE boundary.

    Every route-level guard this lane added asserts a REFUSAL. Nothing asserted
    that the two chat route families are still REACHABLE by the callers whose
    existence is the whole reason the chat tier had to be invented at all -- an
    embed token cannot use ``require_project_role``, which refuses it by type.

    Mutation-proven gap (Opus5 R1 confirmation gate). Both of these shipped
    GREEN across 201 tests in eight suites:

    * ``agent_config.list_selectable_models`` reverted to the management tier
      ``_require_project_viewer`` -- a plausible "consolidate the gates"
      refactor that 403s every embedded chat widget's and every Excel plugin
      user's model picker. The route-coverage guard still CERTIFIES it, because
      ``_require_project_viewer`` is itself a certified gate.
    * ``conversations._require_project_access_and_agent`` refusing embed tokens
      at the CALL SITE, ahead of the tier.

    ``test_the_embed_caller_is_still_admitted_by_the_chat_tier`` covers the TIER
    and cannot see either: both are WIRING, not tier logic. That is exactly the
    unit-passes / production-path-fails shape CLAUDE.md names, on the outage
    side rather than the exposure side.

    Tier: T1 (authorization contract).
    """

    @pytest.mark.parametrize(
        "label,method,path,module,ok_status", _CHAT_ROUTES,
        ids=[r[0] for r in _CHAT_ROUTES],
    )
    @pytest.mark.asyncio
    async def test_an_embed_token_scoped_to_this_project_is_admitted(
        self, label, method, path, module, ok_status
    ):
        from src.auth.middleware import CurrentEmbedUser

        caller = CurrentEmbedUser(
            user_id="embed-user", tenant_id=TEST_TENANT,
            email="embed-user@example.com",
            capabilities=["chat"], project_ids=[str(PROJECT_A)],
        )
        resp = await _request(
            method, path, bindings=[], caller=caller, endpoint_module=module,
        )
        assert resp.status_code == ok_status, (
            f"{label}: an embed token scoped to this project was blocked "
            f"({resp.status_code}) -- every embedded chat deployment and the "
            "Excel plugin's in-chat model picker are down"
        )

    @pytest.mark.parametrize(
        "label,method,path,module,ok_status", _CHAT_ROUTES,
        ids=[r[0] for r in _CHAT_ROUTES],
    )
    @pytest.mark.parametrize("role", ["tenant_admin", "system_admin"])
    @pytest.mark.asyncio
    async def test_a_privileged_human_is_admitted(
        self, role, label, method, path, module, ok_status
    ):
        resp = await _request(
            method, path, bindings=[],
            caller=_user(f"root-{role}@tessallite.local", role=role),
            endpoint_module=module,
        )
        assert resp.status_code == ok_status, (
            f"{label}: {role} was blocked ({resp.status_code})"
        )

    @pytest.mark.parametrize(
        "label,method,path,module,ok_status", _CHAT_ROUTES,
        ids=[r[0] for r in _CHAT_ROUTES],
    )
    @pytest.mark.asyncio
    async def test_a_member_bound_to_this_project_is_admitted(
        self, label, method, path, module, ok_status
    ):
        resp = await _request(
            method, path,
            bindings=[_binding(project_id=PROJECT_A, role="viewer")],
            caller=_user("probe@example.com"), endpoint_module=module,
        )
        assert resp.status_code == ok_status, (
            f"{label}: a member bound to this project as viewer was blocked "
            f"({resp.status_code})"
        )

    @pytest.mark.parametrize(
        "label,method,path,module,ok_status", _CHAT_ROUTES,
        ids=[r[0] for r in _CHAT_ROUTES],
    )
    @pytest.mark.asyncio
    async def test_a_member_bound_only_elsewhere_is_refused(
        self, label, method, path, module, ok_status
    ):
        """The paired refusal, so the three admissions above cannot pass by the
        gate simply admitting everybody."""
        resp = await _request(
            method, path,
            bindings=[
                _binding(project_id=PROJECT_B, role="admin"),
                # See test_the_chat_terminal_actually_authorizes: PROJECT_A
                # needs a binding of its own, or HALF B's per-project
                # bootstrap-open admits this caller.
                _binding(project_id=PROJECT_A, role="admin",
                         user="someone-else@example.com"),
            ],
            caller=_user("probe@example.com"), endpoint_module=module,
        )
        assert resp.status_code == 403, (
            f"{label}: a caller whose only binding is on another project "
            f"reached this project's chat surface ({resp.status_code})"
        )

    @pytest.mark.asyncio
    async def test_half_b_closed_chat_surface_denies_binding_less_caller(
        self,
    ):
        """Bug-8589 HALF B -- CLOSED by F-021-04 (Wave C hard cutover).

        This was an ``xfail(strict=True)`` tripwire while HALF B was open: the
        delegated platform terminal bootstrap-opened PER PROJECT, so an
        authenticated tenant user with no binding anywhere was ADMITTED to a
        binding-less project's conversations. The F-021-04 cutover removed the
        agent-service ``bootstrap_open`` grant (Bug-9442) and the shared
        ``ensure_project_model_access`` bootstrap (LANE-RBAC), so the chat
        surface now DENIES a binding-less caller. The tripwire XPASSed and the
        marker was dropped; this is now the live denial contract, paired with
        ``TestNonHumanPrincipalsFailClosed`` and
        ``test_a_human_with_no_binding_is_denied_in_a_zero_binding_tenant``.
        Still depends on ``_BindingStore`` modelling ``.first()`` so the removed
        bootstrap branch is genuinely exercised, not skipped.
        """
        resp = await _request(
            "GET", f"/api/v1/projects/{PROJECT_A}/agent/conversations",
            bindings=[], caller=_user("nobody@example.com"),
            endpoint_module="src.api.conversations",
        )
        assert resp.status_code == 403, (
            "F-021-04 regression -- a tenant user with no binding was admitted "
            f"to a binding-less project's chat surface ({resp.status_code}); the "
            "zero-binding bootstrap grant has reappeared"
        )
