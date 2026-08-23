"""Bug-8356 / Bug-8444 — agent-service project-scoped authorization.

Bug-8356 [HIGH, live]: the four webhook management endpoints
(``rotate-secret``, ``GET /dlq``, ``dlq/{id}/retry``, ``DELETE /dlq/{id}``)
are all declared under ``/projects/{project_id}/agent/webhook`` but
authorized on ANY tenant-wide modeler/admin binding, with no predicate on
the REQUESTED project. A user holding a binding on project B could rotate
project A's signing secret (breaking A's receiver), read A's dead-letter
queue -- which carries ``user_message``/``answer_text`` conversation
content -- replay it, or delete it.

Bug-8444 [HIGH]: ``agent_config._require_project_viewer`` had the identical
missing predicate, and ``GET /agent/config`` returns ``webhook_url`` -- the
field Bug-8350 established commonly embeds a bearer token.

Why the fake DB in this file evaluates the real WHERE clause
------------------------------------------------------------
The obvious mock (``result.scalar_one_or_none.return_value = binding``)
returns the same binding no matter what the gate asks for, so it passes
just as happily against the BROKEN gate as against the fixed one -- it
cannot detect this bug class at all. ``_BindingStore`` instead walks the
statement's ``whereclause`` and evaluates it against in-memory rows, so a
gate that omits the ``project_id`` predicate genuinely returns a row it
should not have matched, and these tests genuinely go red.

Test escape: every pre-existing agent-service RBAC test asserted against a
mock that ignored the WHERE clause, so no test could observe a missing
predicate. Guard: ``_BindingStore`` + the router-coverage test below.
Tier: T1 (authorization contract).
"""
from __future__ import annotations

import types
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.routing import APIRoute
from shared.db.models import UserAccessBinding
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Grouping,
    Null,
)
from sqlalchemy.sql.functions import Function

from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

TEST_TENANT = "idor-tenant"
PROJECT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
PROJECT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
DLQ_ID = uuid.UUID("cccccccc-0000-4000-8000-000000000003")

WEBHOOK_PREFIX = "/api/v1/projects/{project_id}/agent/webhook"


# ---------------------------------------------------------------------------
# A fake session that actually applies the statement's WHERE clause
# ---------------------------------------------------------------------------


def _literal(node):
    if isinstance(node, Null):
        return None
    if isinstance(node, BindParameter):
        return node.value
    if isinstance(node, Grouping):
        return _literal(node.element)
    raise AssertionError(f"unsupported literal node {node!r}")


def _lhs_value(node, row):
    """Resolve the LEFT side of a comparison against one in-memory row.

    Bug-8446 — the gates now compare identities through
    ``shared.auth.identity.user_identity_matches``, which emits
    ``func.lower(UserAccessBinding.user_identity) == '<canonical>'`` for an
    email identity. A left side that is a SQL function, not a bare column, is
    exactly the shape this evaluator must model rather than silently resolve
    to ``None`` (which would degrade every gate to "matches nothing" and turn
    a real authorization contract into an all-403 tautology).
    """
    if isinstance(node, Grouping):
        return _lhs_value(node.element, row)
    if isinstance(node, Function):
        if node.name != "lower":
            raise AssertionError(f"unsupported SQL function {node.name!r}")
        inner = list(node.clauses)
        if len(inner) != 1:
            raise AssertionError(f"lower() with {len(inner)} arguments")
        value = _lhs_value(inner[0], row)
        return value.lower() if isinstance(value, str) else value
    name = getattr(node, "name", None)
    if name is None:
        raise AssertionError(f"unsupported left-hand node {node!r}")
    return getattr(row, name, None)


def _matches(clause, row) -> bool:
    """Evaluate a SQLAlchemy WHERE clause against one in-memory row.

    Deliberately supports only the operators the authorization gates use,
    and raises on anything else -- a gate rewritten to use an operator this
    evaluator does not model must fail loudly here rather than silently
    degrade to "matches everything".
    """
    if clause is None:
        return True
    if isinstance(clause, Grouping):
        return _matches(clause.element, row)
    if isinstance(clause, BooleanClauseList):
        parts = [_matches(c, row) for c in clause.clauses]
        if clause.operator is operators.and_:
            return all(parts)
        if clause.operator is operators.or_:
            return any(parts)
        raise AssertionError(f"unsupported boolean operator {clause.operator!r}")
    if isinstance(clause, BinaryExpression):
        actual = _lhs_value(clause.left, row)
        op = clause.operator
        if op is operators.eq:
            return actual == _literal(clause.right)
        if op is operators.ne:
            return actual != _literal(clause.right)
        if op is operators.in_op:
            return actual in _literal(clause.right)
        if op is operators.is_:
            return actual is _literal(clause.right)
        if op is operators.is_not:
            return actual is not _literal(clause.right)
        raise AssertionError(f"unsupported binary operator {op!r}")
    raise AssertionError(f"unsupported clause {clause!r}")


class _BindingStore:
    """Async-session stand-in backed by a real list of binding rows.

    Doubles as the handler-side session: a SELECT against any entity other
    than ``UserAccessBinding`` (the agent config row, a DLQ row) resolves to
    empty, so a gate that PASSES surfaces the handler's own 404/200 rather
    than a 403. That matters because ``agent_config``'s gate and its handlers
    share one ``get_tenant_db`` symbol -- two separate patches of the same
    attribute would silently clobber each other.
    """

    def __init__(self, rows):
        self._rows = list(rows)
        self.get = AsyncMock(return_value=None)
        self.commit = AsyncMock()
        self.delete = AsyncMock()
        self.refresh = AsyncMock()
        self.add = MagicMock()
        self.flush = AsyncMock()

    async def execute(self, stmt):
        entities = [d.get("entity") for d in stmt.column_descriptions]
        if UserAccessBinding in entities:
            matched = [r for r in self._rows if _matches(stmt.whereclause, r)]
        else:
            matched = []
        result = MagicMock()
        result.scalar_one_or_none.return_value = matched[0] if matched else None
        result.scalars.return_value.all.return_value = matched
        result.all.return_value = []
        # ``ensure_project_model_access``'s bootstrap-open probe is
        # ``select(UserAccessBinding.id)...first()``. Leaving ``.first()``
        # unstubbed returns a truthy MagicMock, so that branch NEVER fires
        # and the double silently makes the code look more CLOSED than it
        # is -- the same class of infidelity this file's own docstring
        # rejects for ``scalar_one_or_none``.
        result.first.return_value = matched[0] if matched else None
        # R2 reviewer tests_to_promote item 1 -- kpis handlers call
        # .scalar() for count aggregates; a MagicMock default would be
        # returned into an int field and 500 the positive-path assertion.
        result.scalar.return_value = 0
        return result


def _binding(*, project_id, role="modeler", user="probe@example.com"):
    return types.SimpleNamespace(
        user_identity=user, role=role, project_id=project_id, model_id=None,
    )


def _gen(db):
    async def _g(*_a, **_kw):
        yield db
    return _g


def _user(role: str = "member") -> CurrentUser:
    return CurrentUser(
        user_id="probe@example.com",
        tenant_id="__system__" if role == "system_admin" else TEST_TENANT,
        email="probe@example.com",
        role=role,
    )


async def _request(
    method: str,
    path: str,
    *,
    bindings,
    role="member",
    endpoint_module="src.api.webhooks",
    json=None,
):
    """Drive a real request through the ASGI app.

    The gate always lives in ``src.api.agent_config``; ``endpoint_module`` is
    the module owning the handler. One ``_BindingStore`` instance backs both
    so the same-module case (agent_config's own routes) works without two
    patches fighting over one attribute.
    """
    store = _BindingStore(bindings)
    targets = {"src.api.agent_config", endpoint_module}
    app.dependency_overrides[get_current_user] = lambda: _user(role)
    try:
        with ExitStack() as stack:
            for module in targets:
                stack.enter_context(
                    patch(f"{module}.get_tenant_db", _gen(store))
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, json=json)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# The four routes under test, with the status each returns once the gate
# PASSES against the empty handler-side DB above. Asserting the exact
# post-gate status (not merely "!= 403") proves the request really reached
# the handler rather than being rejected somewhere else.
_WEBHOOK_ROUTES = [
    ("POST", f"/api/v1/projects/{PROJECT_A}/agent/webhook/rotate-secret", 404),
    ("GET", f"/api/v1/projects/{PROJECT_A}/agent/webhook/dlq", 200),
    ("POST", f"/api/v1/projects/{PROJECT_A}/agent/webhook/dlq/{DLQ_ID}/retry", 404),
    ("DELETE", f"/api/v1/projects/{PROJECT_A}/agent/webhook/dlq/{DLQ_ID}", 404),
]


# ---------------------------------------------------------------------------
# Bug-8356 — the cross-project IDOR itself
# ---------------------------------------------------------------------------


class TestWebhookCrossProjectIdor:
    @pytest.mark.parametrize("method,path,_ok", _WEBHOOK_ROUTES)
    @pytest.mark.asyncio
    async def test_binding_on_another_project_is_refused(self, method, path, _ok):
        """The live exploit: an admin binding on project B must not authorize
        anything on project A."""
        resp = await _request(
            method, path, bindings=[_binding(project_id=PROJECT_B, role="admin")]
        )
        assert resp.status_code == 403, (
            f"{method} {path} authorized a caller whose only binding is on "
            f"another project (cross-project IDOR)"
        )

    @pytest.mark.parametrize("method,path,ok_status", _WEBHOOK_ROUTES)
    @pytest.mark.asyncio
    async def test_binding_on_requested_project_is_allowed(self, method, path, ok_status):
        resp = await _request(
            method, path, bindings=[_binding(project_id=PROJECT_A, role="modeler")]
        )
        assert resp.status_code == ok_status

    @pytest.mark.parametrize("method,path,ok_status", _WEBHOOK_ROUTES)
    @pytest.mark.asyncio
    async def test_tenant_wide_binding_is_allowed(self, method, path, ok_status):
        """A binding with a NULL project_id is tenant-wide and still grants
        access -- the fix must scope, not break, legitimate access."""
        resp = await _request(
            method, path, bindings=[_binding(project_id=None, role="admin")]
        )
        assert resp.status_code == ok_status

    @pytest.mark.parametrize("method,path,ok_status", _WEBHOOK_ROUTES)
    @pytest.mark.asyncio
    async def test_tenant_admin_passes_by_role(self, method, path, ok_status):
        resp = await _request(method, path, bindings=[], role="tenant_admin")
        assert resp.status_code == ok_status

    @pytest.mark.parametrize("method,path,_ok", _WEBHOOK_ROUTES)
    @pytest.mark.asyncio
    async def test_viewer_role_binding_on_this_project_is_refused(self, method, path, _ok):
        """Read-tier roles must not reach the webhook surface: the DLQ carries
        conversation content and rotate-secret is a credential operation."""
        resp = await _request(
            method, path, bindings=[_binding(project_id=PROJECT_A, role="viewer")]
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,_ok", _WEBHOOK_ROUTES)
    @pytest.mark.asyncio
    async def test_zero_bindings_does_not_bootstrap_open(self, method, path, _ok):
        """The webhook router uses the STRICT tier: a tenant with no bindings
        at all must not expose DLQ conversation content, or let any
        authenticated user rotate a signing secret, to a plain member.
        (F-023-01 round 2 settled this rule for content surfaces.)"""
        resp = await _request(method, path, bindings=[])
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Bug-8356 — the router is closed BY CONSTRUCTION, not per handler
# ---------------------------------------------------------------------------


def _flatten_dependant_calls(dependant, seen=None):
    seen = seen if seen is not None else []
    for sub in dependant.dependencies:
        if sub.call is not None:
            seen.append(sub.call)
        _flatten_dependant_calls(sub, seen)
    return seen


def _walk_api_routes(routes):
    """Recursively collect every ``APIRoute`` reachable from ``app.routes``.

    FastAPI 0.139 no longer leaves included routers flattened in
    ``app.routes``: it inserts an ``_IncludedRouter`` wrapper that is neither
    an ``APIRoute`` nor exposes ``.routes``, so the naive
    ``for r in app.routes: if isinstance(r, APIRoute)`` idiom silently sees
    ONLY the handful of routes registered directly on the app. That is not
    hypothetical -- it is why this service's own
    ``test_endpoint_accessibility.py`` currently collects 2 tests instead of
    the entire API surface (filed separately). Descend through
    ``original_router`` so this guard cannot rot the same way.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _walk_api_routes(original.routes)
        elif hasattr(route, "routes"):
            yield from _walk_api_routes(route.routes)


class TestWebhookRouterGate:
    """The router must be closed BY CONSTRUCTION, and this guard must be
    provably enumerating something.

    Two INDEPENDENT enumerations are cross-checked: a walk of the app's route
    tree (which yields the ``dependant`` needed to see the gate) and the
    public OpenAPI schema (which is the product's own published surface). If
    the walk ever silently stops seeing routes -- a framework upgrade, a new
    wrapper type -- the counts disagree and this fails CLOSED instead of
    quietly asserting nothing.
    """

    ROUTER_PREFIX = "/projects/{project_id}/agent/webhook"
    PUBLIC_PREFIX = "/api/v1/projects/{project_id}/agent/webhook"

    def _walked(self):
        # `in`, not `startswith`: under the FastAPI version pinned in
        # uv.lock (0.136.1 — what the Docker image installs, and therefore
        # what production runs) `app.routes` is a flat list carrying the FULL
        # mounted path, while the newer 0.139 in the ambient developer
        # interpreter exposes router-local paths. This guard was written
        # against 0.139 only and enumerated ZERO routes under the locked
        # version -- it failed closed, but "green" had only ever been shown on
        # a framework no build artifact installs.
        return [
            r for r in _walk_api_routes(app.routes)
            if self.ROUTER_PREFIX in r.path
        ]

    def _published(self):
        return [
            p for p in app.openapi()["paths"]
            if self.ROUTER_PREFIX in p
        ]

    def test_enumeration_is_not_vacuous(self):
        walked = {
            r.path[r.path.index(self.ROUTER_PREFIX):] for r in self._walked()
        }
        published = {
            p[p.index(self.ROUTER_PREFIX):] for p in self._published()
        }
        assert published, (
            "no webhook routes in the OpenAPI schema -- the prefix is stale "
            "or the router is no longer mounted"
        )
        assert walked == published, (
            "route-tree walk and OpenAPI schema disagree on the webhook "
            f"surface; walk={sorted(walked)} openapi={sorted(published)}"
        )

    def test_every_webhook_route_carries_the_project_gate(self):
        from src.api.webhooks import _require_webhook_project_access

        routes = self._walked()
        assert len(routes) >= 4, (
            f"expected at least the 4 known webhook routes under "
            f"{self.ROUTER_PREFIX}, found {len(routes)}"
        )
        for route in routes:
            calls = _flatten_dependant_calls(route.dependant)
            assert _require_webhook_project_access in calls, (
                f"{sorted(route.methods)} {route.path} is not covered by the "
                f"project-scoped webhook authorization gate"
            )

    def test_module_no_longer_defines_an_unscoped_gate(self):
        """The drifted copy that caused Bug-8356 must stay deleted: a
        project-unaware gate in this module is the defect itself."""
        import src.api.webhooks as webhooks_module

        assert not hasattr(webhooks_module, "_require_modeller"), (
            "the project-unaware `_require_modeller` gate was reintroduced"
        )


# ---------------------------------------------------------------------------
# Bug-8444 — the same missing predicate on the agent-config read gate
# ---------------------------------------------------------------------------


class TestAgentConfigViewerGateProjectScope:
    @pytest.mark.asyncio
    async def test_binding_on_another_project_cannot_read_agent_config(self):
        """GET /agent/config returns webhook_url, which commonly embeds a
        bearer token (Bug-8350) -- a project-B binding must not read it."""
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/config",
            bindings=[_binding(project_id=PROJECT_B, role="viewer")],
            endpoint_module="src.api.agent_config",
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_binding_on_requested_project_still_reads_agent_config(self):
        # No config row in the handler DB -> the gate passed and the handler
        # returned its own 404, not a 403.
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/config",
            bindings=[_binding(project_id=PROJECT_A, role="viewer")],
            endpoint_module="src.api.agent_config",
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_tenant_wide_binding_still_reads_agent_config(self):
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/config",
            bindings=[_binding(project_id=None, role="viewer")],
            endpoint_module="src.api.agent_config",
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_zero_bindings_denies_config_read(self):
        """F-021-04 HARD CUTOVER (Wave C decision #9, Bug-9442).

        Decision D2's bootstrap-open posture used to admit a plain member to a
        binding-less project's agent CONFIGURATION read (returning 404 for the
        absent config). Decision #9 removed that first-arriver grant: a
        binding-less project denies every ordinary caller, so this member with
        no binding is now refused with 403 before the handler runs. Only a
        human tenant/system admin may reach a binding-less project."""
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/config",
            bindings=[],
            endpoint_module="src.api.agent_config",
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Bug-8444 (second occurrence) — the SAME missing predicate on personas'
# read gate. Promoted verbatim from the R1 fresh-reviewer's tests_to_promote:
# the first enumeration pass covered agent_config.py's three gates and
# stopped short of this file's second gate, and the reviewer live-reproduced
# a 200 where a 403 belonged.
# ---------------------------------------------------------------------------


class TestPersonasViewerGateProjectScope:
    @pytest.mark.asyncio
    async def test_binding_on_another_project_cannot_list_personas(self):
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/personas",
            bindings=[_binding(project_id=PROJECT_B, role="viewer")],
            endpoint_module="src.api.personas",
        )
        assert resp.status_code == 403, (
            f"personas list authorized a caller bound only to another "
            f"project (got {resp.status_code}) — cross-project IDOR"
        )

    @pytest.mark.asyncio
    async def test_binding_on_requested_project_still_lists(self):
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/personas",
            bindings=[_binding(project_id=PROJECT_A, role="viewer")],
            endpoint_module="src.api.personas",
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# R2 fresh-reviewer tests_to_promote items 2 and 3, applied verbatim.
#
# The lane's own enumeration grepped `select(UserAccessBinding)` -- a
# discovery mechanism that can only find gates which EXIST, and is
# structurally blind to a route carrying no gate at all. Both classes below
# were exactly that: rubrics.py had six completely ungated routes (including
# PUT/DELETE/bulk-replace), and kpis.py gated /calibration but not /kpis or
# /cost. See TestEveryProjectRouteIsGated below for the guard that closes the
# enumeration blind spot itself.
# ---------------------------------------------------------------------------


class TestRubricsProjectScope:
    """F1 (R2 reviewer) — rubrics.py shipped with NO project gate at all:
    every route was reachable by any non-embed tenant user. These pin the
    gate the same way TestPersonasViewerGateProjectScope pins personas'."""

    @pytest.mark.asyncio
    async def test_binding_on_another_project_cannot_list_rubrics(self):
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/rubrics",
            bindings=[_binding(project_id=PROJECT_B, role="viewer")],
            endpoint_module="src.api.rubrics",
        )
        assert resp.status_code == 403, (
            f"rubrics list authorized a caller bound only to another project "
            f"(got {resp.status_code}) — cross-project IDOR"
        )

    @pytest.mark.asyncio
    async def test_binding_on_another_project_cannot_delete_rubric(self):
        resp = await _request(
            "DELETE",
            f"/api/v1/projects/{PROJECT_A}/agent/rubrics/{uuid.uuid4()}",
            bindings=[_binding(project_id=PROJECT_B, role="modeler")],
            endpoint_module="src.api.rubrics",
        )
        assert resp.status_code == 403, (
            "rubric delete must require a modeller binding on THIS project"
        )

    @pytest.mark.asyncio
    async def test_viewer_binding_on_requested_project_still_lists(self):
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/rubrics",
            bindings=[_binding(project_id=PROJECT_A, role="viewer")],
            endpoint_module="src.api.rubrics",
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_viewer_binding_cannot_write_rubrics(self):
        resp = await _request(
            "POST",
            f"/api/v1/projects/{PROJECT_A}/agent/rubrics",
            bindings=[_binding(project_id=PROJECT_A, role="viewer")],
            endpoint_module="src.api.rubrics",
            json={"name": "x", "sections": []},
        )
        assert resp.status_code == 403, (
            "rubric create is a modeller/admin surface (matches recipes.py)"
        )


class TestKpisProjectScope:
    """F2 (R2 reviewer) — /kpis and /cost were ungated while /calibration in
    the same module was strict-gated."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("suffix", ["kpis", "cost"])
    async def test_binding_on_another_project_cannot_read(self, suffix):
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/{suffix}",
            bindings=[_binding(project_id=PROJECT_B, role="viewer")],
            endpoint_module="src.api.kpis",
        )
        assert resp.status_code == 403, (
            f"/{suffix} served cross-project agent metrics (got {resp.status_code})"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("suffix", ["kpis", "cost"])
    async def test_binding_on_requested_project_still_reads(self, suffix):
        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/{suffix}",
            bindings=[_binding(project_id=PROJECT_A, role="modeler")],
            endpoint_module="src.api.kpis",
        )
        assert resp.status_code == 200  # needs the scalar -> 0 harness item
