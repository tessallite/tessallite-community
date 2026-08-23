"""The project-scoped authorization primitives for agent-service.

Bug-8445 / Bug-8446 — structural fix, not another per-copy patch.

Two entry points, one binding lookup
------------------------------------
``require_project_role`` is the ONE ``UserAccessBinding`` lookup in this service
and gates every management, configuration and admin-content surface.
``require_project_chat_access`` (Bug-8589) gates the conversational surfaces,
which must stay reachable by an embed token and therefore delegate the human /
embed decision to the platform-canonical, embed-aware
``shared.auth.project_access.ensure_project_model_access``. It adds the one
thing that terminal deliberately does not do — refusing a service principal by
type — because the chat routes carry no route-level scope dependency that could
have authorized one. Neither entry point hand-rolls a lookup, so the
"exactly one binding lookup exists" property below is unchanged.

Why this module exists
----------------------
This service used to hand-roll the same ``UserAccessBinding`` lookup five
times (``agent_config`` x3, ``personas`` x2). Five copies of one rule drift,
and these had already drifted on FIVE independent axes by the time they were
enumerated:

1. **project predicate** — present in some copies, absent in others. Absent is
   a cross-project IDOR (Bug-8356 on the webhook router, Bug-8444 on both
   ``_require_project_viewer`` copies).
2. **role predicate** — the read tier accepts any role, the write tier
   requires ``admin``/``modeler``. Legitimate difference, but expressed by
   editing a copy rather than by a parameter, so it could not be reviewed as
   a policy.
3. **bootstrap-open branch** — configuration surfaces used to open when a
   tenant had zero bindings (first-run setup, decision D2) while content
   surfaces failed closed (F-023-01 round 2); once a per-copy edit, then a
   single parameter after Bug-8445. F-021-04 HARD CUTOVER (Wave C decision #9,
   Bug-9442) has since REMOVED the branch outright: a binding-less project
   denies every ordinary caller on every surface, and only a human
   tenant/system admin repairs it. The axis no longer exists to drift on.
4. **admin bypass predicate** — ``is_human_tenant_admin`` in ``agent_config``'s
   modeller and viewer gates, ``is_human_tenant_admin_or_system_admin`` in
   ``personas``' two gates and in ``_require_blocked_original_access``. So a
   canonical human system admin was admitted by the STRICTEST gate in the
   service and by personas, and refused by the two weakest ones. Unified here
   on the platform-canonical ``is_human_tenant_admin_or_system_admin`` — the
   same predicate ``shared/auth/middleware.require_tenant_admin`` and
   ``shared/auth/project_access._has_admin_bypass`` use.
5. **identity comparison** (Bug-8446) — every copy compared
   ``UserAccessBinding.user_identity == current_user.user_id`` with a raw,
   case-sensitive ``==``, while model-service WRITES and looks bindings up
   through ``shared.auth.identity.user_identity_matches``, which lower-cases
   an email identity first. A user whose IdP returns ``User@Example.com``
   against a stored ``user@example.com`` binding was authorised by
   model-service and 403'd by every agent-service surface.

Because every route also re-applied its gate by hand, a newly added route
could ship completely ungated with nothing failing. Two guards now close that
half: the webhook router is closed by construction with a router-level
``dependencies=[...]`` (Bug-8356), and
``tests/test_project_route_authorization_coverage.py`` enumerates ROUTES (not
gates) and fails closed on any project-addressed route without one.

Why the per-module wrappers stay
--------------------------------
Callers do NOT import ``require_project_role`` directly; each module keeps a
named wrapper (``_require_project_viewer`` and friends). That is deliberate,
for two independent reasons:

* ``tests/test_project_route_authorization_coverage.py`` certifies a route by
  AST-matching an awaited bare ``Name`` against a known set of gate names.
  Keeping the names keeps that guard meaningful.
* A large number of existing tests patch ``src.api.<module>.get_tenant_db``.
  This module therefore never imports ``get_tenant_db``: the caller passes its
  own module-level symbol in as ``db_factory``, and because a wrapper body
  references the bare name, it resolves from the wrapper's OWN module globals
  at call time — so every existing patch target keeps working unchanged. That
  session-factory indirection is the ONLY thing the wrappers own; the whole
  authorization decision lives here.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select

from shared.auth.identity import user_identity_matches
from shared.auth.project_access import ensure_project_model_access
from shared.db.models import UserAccessBinding
from src.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    CurrentUser,
    is_human_tenant_admin_or_system_admin,
)

# Bug-1082 — the platform stores exactly one spelling, "modeler" (model-service
# rbac.py ROLE_HIERARCHY, the SPA AccessRole type, migration 0014
# user_access_bindings). "admin" is a valid PROJECT-binding role, distinct from
# the JWT-level tenant_admin.
MODELER_BINDING_ROLES: tuple[str, ...] = ("admin", "modeler")

# Every role is accepted at the read tier; expressed as None rather than as an
# enumeration so a role added to the platform later is admitted at the read
# tier without another edit here.
ANY_ROLE: Optional[tuple[str, ...]] = None


async def require_project_role(
    project_id: UUID,
    current_user: CurrentUser,
    *,
    db_factory: Callable[..., Any],
    roles: Optional[Iterable[str]] = ANY_ROLE,
    detail: str,
) -> None:
    """Authorize ``current_user`` against ``project_id``, or raise 403.

    ``db_factory`` is the CALLER's ``get_tenant_db`` symbol (see the module
    docstring for why it is injected rather than imported).

    ``roles`` restricts the accepted binding roles; ``None`` accepts any role
    (the read tier).

    Zero-binding projects deny every ordinary caller — F-021-04 HARD CUTOVER
    (Wave C decision #9, Bug-9442). This primitive used to carry a
    ``bootstrap_open`` parameter: when a tenant had ZERO bindings anywhere it
    admitted any authenticated caller on "first-run setup" configuration
    surfaces (the accepted-risk decision D2). Decision #9 removed that
    first-arriver grant everywhere — a binding-less project is a LEGACY state
    that only a human tenant/system admin may repair (via model-service),
    never a self-service claim. There is therefore no longer any posture in
    which a binding-less project admits an ordinary user: the branch is gone,
    not merely gated. Content surfaces (``_require_blocked_original_access``)
    already denied under the old ``bootstrap_open=False``; the configuration
    surfaces now share that same fail-closed behaviour.

    Principal types are decided BEFORE any binding lookup:

    * a human tenant admin or canonical human system admin passes by role;
    * an embed token and a service principal are refused outright. Neither has
      a ``UserAccessBinding`` — model-service only ever writes bindings for
      human identities — so previously they fell through to the lookup, missed,
      and then hit the ``bootstrap_open`` branch, which ADMITTED them in any
      tenant that happened to have no bindings yet. Refusing by type is
      strictly fail-closed and is independent of the cutover above. Embed
      tokens are additionally rejected upstream by ``forbid_embed_user`` on
      every route that calls this, and a service principal that is legitimately
      authorized reaches its route through a scope dependency and must return
      before calling this (see ``agent_config._authorize_refresh_derived``) —
      AUTH-RR-01: a service principal is scope-authorized at the route level
      and must never enter human binding lookup.
    """
    if is_human_tenant_admin_or_system_admin(current_user):
        return

    if isinstance(current_user, (CurrentEmbedUser, CurrentServiceUser)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail,
        )

    predicates = [
        # Bug-8446 — the platform's canonical identity comparison, which
        # lower-cases an email identity on both sides. A raw `==` forks
        # authorization between this service and model-service.
        user_identity_matches(
            UserAccessBinding.user_identity, current_user.user_id
        ),
        # Bug-8356 / Bug-8444 — the requested project, or a tenant-wide
        # binding (NULL project_id). Bindings written by the platform always
        # carry a project_id (model-service access.py / auth/jit.py), so the
        # predicate cannot lock out a legitimately-bound caller.
        (UserAccessBinding.project_id == project_id)
        | (UserAccessBinding.project_id.is_(None)),
    ]
    if roles is not None:
        predicates.insert(1, UserAccessBinding.role.in_(tuple(roles)))

    async for db in db_factory(current_user.tenant_id):
        result = await db.execute(
            select(UserAccessBinding).where(*predicates).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return
        # F-021-04 (Bug-9442): a binding-less project no longer bootstrap-opens.
        # No matching binding => deny; only a human admin (handled above) passes.
        break

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
    )


async def require_project_chat_access(
    db,
    current_user: CurrentUser,
    *,
    project_id: UUID,
    min_role: str = "viewer",
) -> None:
    """The CHAT tier: authorize a conversational surface, or raise 403.

    Bug-8589 HALF A. This service has two authorization tiers, not one, because
    its surfaces do not all have the same legitimate caller set:

    * ``require_project_role`` above — the management/config/content tiers.
      Refuses an embed token AND a service principal by TYPE, then looks the
      caller's ``UserAccessBinding`` up itself.
    * this gate — the conversational tier (the nine conversation routes and
      ``GET /projects/{project_id}/agent/selectable-models``). An EMBED token is
      a first-class caller here (the conversational-client widget and the Excel
      plugin both authenticate with one), so ``require_project_role`` cannot be
      reused: it refuses embed tokens by type. It therefore delegates the human
      and embed decision to the platform-canonical, embed-aware
      ``shared.auth.project_access.ensure_project_model_access``.

    What this gate ADDS to that terminal, and why it has to exist at all
    --------------------------------------------------------------------
    ``ensure_project_model_access`` ADMITS a ``CurrentServiceUser`` rather than
    refusing one. That posture is correct for its own contract (AUTH-RR-01: a
    service principal is scope-authorized at the ROUTE level, so the project
    check must skip the human binding lookup rather than block the request) —
    but it is only SOUND where the route really does carry a scope dependency.
    Both chat route families gate on ``require_capability("chat")``, which has
    no ``CurrentServiceUser`` branch at all, so AUTH-RR-01's premise was false
    on exactly these surfaces and a service token minted for an unrelated scope
    reached conversation create/read/send and the model picker on any project in
    its tenant (Bug-8589 HALF A, verified by execution: one statement ran, zero
    binding lookups).

    The refusal is unconditional rather than scope-checked because no service
    principal has a legitimate reason to be here: none of the platform's defined
    service scopes relates to chat, and the one genuinely legitimate service
    caller into agent-service (model-service's deploy fan-out to
    ``POST /agent/refresh-derived``) has its own narrow typed-scope bypass in
    ``agent_config._authorize_refresh_derived`` and never reaches this gate.

    It lives HERE, as one named tier next to the others, rather than as an
    ``isinstance`` check at each of the two call sites — two copies of one
    authorization rule is precisely the drift shape Bug-8445 consolidated away,
    and the second call site
    (``agent_config.list_selectable_models``) only existed as a defect because
    the first one's fix was never propagated to it (Bug-8460, then Bug-8589
    again on the same pair).

    Residual, stated rather than implied: the delegated SHARED terminal
    (``shared.auth.project_access.ensure_project_model_access``) still
    bootstrap-opens PER PROJECT (a project with zero bindings admits any
    authenticated tenant user). ``require_project_role`` no longer does — its
    zero-binding grant was removed outright by the F-021-04 HARD CUTOVER (Wave
    C decision #9, Bug-9442). The chat tier is now STRICTLY weaker than the
    management tier on this axis, not merely differently scoped. Closing the
    residual is a change to the shared terminal (Bug-8589 HALF B — still open),
    owned by the shared/model-service RBAC cutover, deliberately NOT changed
    here; decision #9's "project/model access helpers" clause targets it too.
    It is reachable through exactly one agent-service gate, so closing it is a
    change in one place.
    """
    if isinstance(current_user, CurrentServiceUser):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service tokens cannot access conversational surfaces",
        )
    await ensure_project_model_access(
        db, current_user, project_id=project_id, min_role=min_role,
    )
