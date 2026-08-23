"""Bug-6389 [SECURITY] — the plugin route trace must not disclose physical
rewritten SQL to a caller who is not entitled to it.

``PluginRouteTrace.rewritten_query`` carries the physical schema/table/column
names AND the compiled row-security predicate. The product already treats that
content as privileged: ``GET /diagnostics/query-rewrites``, which returns the
same ``(raw, rewritten)`` pairs, is gated behind ``require_tenant_admin``. The
route trace embedded in an ordinary ``/plugin/execute`` response was returned to
any ``query``-capability caller, i.e. the same content through a different door.

R2 review finding B1 — these tests are written against the vocabulary the
platform's own token mint ACTUALLY issues. The first version of this suite only
exercised ``"viewer"`` / ``"modeler"`` / ``"admin"`` on ``current_user.role``,
which is the PROJECT-binding vocabulary and never appears on a JWT. It therefore
passed while the shipped gate denied every real tenant admin and granted an
anonymous embed session whose RLS role happened to be named ``"modeler"``.

**Second half of this file, 2026-08-11:** the separate EMBED withhold that once
lived alongside this gate was REMOVED by user decision (option C — see
``docs/questions/questions_disclosure-by-entitlement-not-auth-method.md``). The
tests that asserted it were stale by design change and now pin the replacement
contract instead, together with the ACCESS controls that share the ``is_embed``
flag and were deliberately NOT removed.
"""
from __future__ import annotations

import json
import types

import pytest
from fastapi import HTTPException

from src.api._sql_disclosure import (
    may_disclose_physical_sql,
    redact_physical_sql,
)

_PHYSICAL_SQL = (
    'SELECT "region_code", SUM("amount") FROM "acme_aggregates"."agg_sales_v3" '
    "WHERE (NOT \"region_code\" = 'EMEA') GROUP BY \"region_code\""
)
_PROJECT = "11111111-1111-1111-1111-111111111111"
_MODEL = "22222222-2222-2222-2222-222222222222"


def _user(role: str, roles: list[str] | None = None, tenant_id: str = "t1"):
    """A human CurrentUser as the real token mint produces one: ``role`` is the
    JWT TENANT tier, and ``roles`` is the additive row-security role subject."""
    from shared.auth.middleware import CurrentUser

    return CurrentUser(
        user_id="u@x.com", tenant_id=tenant_id, email="u@x.com",
        role=role, roles=roles if roles is not None else [role],
    )


def _embed(rls_role: str | None):
    from shared.auth.middleware import CurrentEmbedUser

    return CurrentEmbedUser(
        user_id="anon@viewer", tenant_id="t1", email="anon@viewer",
        capabilities=["query"], rls_role=rls_role,
    )


class _Db:
    """Stands in for the tenant session. ``binding_role`` is what the project
    binding lookup would resolve to; None means 'no binding, denied'."""

    def __init__(self, binding_role: str | None):
        self.binding_role = binding_role
        self.calls = 0


async def _fake_ensure(db, current_user, *, project_id, model_id, min_role):
    """Mirror ensure_project_model_access's contract: return on entitled,
    raise HTTPException(403) otherwise."""
    from shared.auth.roles import project_role_level

    db.calls += 1
    if db.binding_role is None:
        raise HTTPException(status_code=403, detail="no binding")
    if project_role_level(db.binding_role) > project_role_level(min_role):
        raise HTTPException(status_code=403, detail="insufficient")
    return None


@pytest.fixture(autouse=True)
def _patch_access(monkeypatch):
    import src.api._sql_disclosure as mod

    monkeypatch.setattr(mod, "ensure_project_model_access", _fake_ensure)


async def _may(user, binding_role="viewer"):
    return await may_disclose_physical_sql(
        _Db(binding_role), user, project_id=_PROJECT, model_id=_MODEL,
    )


# ---------------------------------------------------------------------------
# The audience that MUST keep the trace (B1a — the gate previously denied them)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_admin_may_see_physical_sql():
    """``current_user.role`` is the JWT tenant tier. A tenant admin can already
    read this exact content at /diagnostics/query-rewrites, so the trace gate
    must not lock them out — and the demo/dev admin is minted ``tenant_admin``
    (scripts/seed_acme_demo_project.py), so denying it killed the feature in
    the shipped default stack."""
    assert await _may(_user("tenant_admin"), binding_role=None) is True


@pytest.mark.asyncio
async def test_canonical_system_admin_may_see_physical_sql():
    """A canonical system admin is ``role="system_admin"`` AND
    ``tenant_id="__system__"`` (is_canonical_human_system_admin)."""
    assert await _may(
        _user("system_admin", tenant_id="__system__"), binding_role=None,
    ) is True


@pytest.mark.asyncio
async def test_system_admin_role_inside_a_tenant_is_not_a_platform_admin():
    """Guard the exact predicate: a token claiming ``system_admin`` while
    scoped to an ordinary tenant is NOT canonical, so it must fall through to
    the project binding rather than being waved past."""
    assert await _may(
        _user("system_admin", tenant_id="t1"), binding_role="viewer",
    ) is False


@pytest.mark.asyncio
async def test_project_modeler_may_see_physical_sql():
    """A modeller authors the physical bindings, so the physical names are not
    a disclosure to them. Their JWT role is the ordinary ``member``; the
    entitlement comes from the PROJECT BINDING."""
    assert await _may(_user("member"), binding_role="modeler") is True


@pytest.mark.asyncio
async def test_project_admin_may_see_physical_sql():
    assert await _may(_user("member"), binding_role="admin") is True


# ---------------------------------------------------------------------------
# The audience that must NOT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_viewer_is_redacted():
    sql, redacted = await redact_physical_sql(
        _PHYSICAL_SQL, _Db("viewer"), _user("member"),
        project_id=_PROJECT, model_id=_MODEL,
    )
    assert sql is None
    assert redacted is True


@pytest.mark.asyncio
async def test_caller_with_no_binding_is_redacted():
    assert await _may(_user("member"), binding_role=None) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("rls_role", [None, "manager", "modeler", "admin", "viewer"])
async def test_embed_session_never_sees_physical_sql(rls_role):
    """B1b (fail-open, verified): ``EmbedRlsSubject.role`` is arbitrary
    admin-authored text that ``CurrentEmbedUser`` surfaces as ``role``/``roles``.
    An anonymous embedded-dashboard viewer whose RLS role happens to be named
    ``modeler``/``admin`` must NOT thereby receive the physical schema and the
    compiled row-security predicate. Entitlement is decided by the caller's
    KIND, before any role token is consulted."""
    user = _embed(rls_role)
    assert await _may(user, binding_role="admin") is False
    sql, redacted = await redact_physical_sql(
        _PHYSICAL_SQL, _Db("admin"), user,
        project_id=_PROJECT, model_id=_MODEL,
    )
    assert sql is None
    assert redacted is True


@pytest.mark.asyncio
async def test_service_principal_never_sees_physical_sql():
    """A service token is scope-authorized for a job and has no human
    accountable for the disclosure. Note it is constructed here with
    role="tenant_admin" on purpose: the admin fast-path must not admit it,
    which is why the kind check runs FIRST."""
    from shared.auth.middleware import CurrentServiceUser

    svc = CurrentServiceUser(
        principal="p1", tenant_id="t1", role="tenant_admin", scopes=["query"],
    )
    assert await _may(svc, binding_role="admin") is False


@pytest.mark.asyncio
async def test_row_security_role_subject_is_never_consulted():
    """``CurrentUser.roles`` is documented as additive and used ONLY by the
    row-security principal adapter. A member whose RLS role set contains the
    string ``modeler`` must not be entitled by it."""
    user = _user("member", roles=["member", "modeler", "admin"])
    assert await _may(user, binding_role="viewer") is False


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_user_fails_closed():
    assert await _may(None) is False


@pytest.mark.asyncio
async def test_unresolvable_project_or_model_fails_closed():
    assert await may_disclose_physical_sql(
        _Db("admin"), _user("member"), project_id=None, model_id=_MODEL,
    ) is False
    assert await may_disclose_physical_sql(
        _Db("admin"), _user("member"), project_id=_PROJECT, model_id=None,
    ) is False


@pytest.mark.asyncio
async def test_binding_lookup_error_fails_closed(monkeypatch):
    """A disclosure gate must never open because the entitlement lookup blew
    up — an unexpected exception is not evidence of entitlement."""
    import src.api._sql_disclosure as mod

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "ensure_project_model_access", _boom)
    assert await _may(_user("member"), binding_role="admin") is False


@pytest.mark.asyncio
async def test_absent_sql_is_not_reported_as_redacted():
    """A route that produced no SQL must not be reported as a policy withhold —
    the two states are different and the client renders them differently."""
    assert await redact_physical_sql(
        None, _Db("viewer"), _user("member"),
        project_id=_PROJECT, model_id=_MODEL,
    ) == (None, False)
    assert await redact_physical_sql(
        None, _Db("admin"), _user("tenant_admin"),
        project_id=_PROJECT, model_id=_MODEL,
    ) == (None, False)


def test_plugin_route_trace_defaults_to_not_redacted():
    """Back-compat: the new field must default False so existing producers and
    stored payloads do not read as 'withheld'."""
    from src.api.plugin import PluginRouteTrace

    trace = PluginRouteTrace(route_type="source", reason="")
    assert trace.rewritten_query is None
    assert trace.rewritten_query_redacted is False


# ---------------------------------------------------------------------------
# The EMBED withhold was REMOVED — decision 2026-08-11, option C
# (docs/questions/questions_disclosure-by-entitlement-not-auth-method.md).
#
# ``redact_physical_details_for_embed`` gated on the token TYPE, so /execute
# and /explain answered the same question differently depending on which door
# the caller used. The tests that lived here asserted that withhold; they are
# stale by explicit user decision (triage route 2) and are replaced by guards
# on the contract that took its place.
#
# The MODELLER-TIER gate above (``may_disclose_physical_sql``) is a DIFFERENT
# control on the ENTITLEMENT axis and is untouched — including its rule that an
# embed or service principal is never entitled to the physical SQL.
# ---------------------------------------------------------------------------

_PHYS_TABLE = "agg_sales_v3"


def _trace_with_physical_detail():
    from src.api.routes import (
        AggregateUsedInfo,
        PipelineTrace,
        PocketUsedInfo,
        TargetSystemInfo,
        TraceStep,
    )

    return PipelineTrace(
        steps=[
            TraceStep(
                stage="binder", title="Bind", detail="d", status="ok",
                data={"model": "Sales", "measures": ["revenue"]},
            ),
            TraceStep(
                stage="rewriter", title="Rewrite", detail="d", status="ok",
                data={"rewritten_sql": _PHYSICAL_SQL},
            ),
        ],
        target_system=TargetSystemInfo(
            name="acme_source", type="postgresql", location="acme_aggregates",
        ),
        aggregate_used=AggregateUsedInfo(
            id="a1", physical_table_name=_PHYS_TABLE, grain=["region_code"],
        ),
        pocket_used=PocketUsedInfo(id="p1", physical_table_name="pkt_sales_v2"),
    )


def _execute_response():
    from src.api.routes import ExecuteResponse

    return ExecuteResponse(
        rows=[], columns=[], route_type="aggregate", reason="r",
        aggregate_id="agg-uuid", pocket_id="pkt-uuid", execution_ms=1,
        bytes_processed=0, rows_returned=0, routed_sql=_PHYSICAL_SQL,
        trace=_trace_with_physical_detail(),
    )


def _explain_response():
    from src.api.routes import ExplainResponse

    return ExplainResponse(
        route_type="aggregate", aggregate_id="agg-uuid", pocket_id="pkt-uuid",
        reason="r", rewritten_query=_PHYSICAL_SQL, requested_measures=[],
        requested_dimensions=[], grain=[], query_fingerprint="f",
        trace=_trace_with_physical_detail(),
    )


def test_no_token_type_disclosure_transform_survives_on_execute_or_explain():
    """The removed control's entry points must be gone, not merely unwired.

    A dead-but-importable redactor is how a later author quietly re-wires the
    token-type axis this decision removed. Fails against the pre-decision code,
    where all three symbols existed.
    """
    import src.api._sql_disclosure as mod

    for gone in (
        "redact_physical_details_for_embed",
        "is_embed_principal",
        "EMBED_REDACTED_REASON_TOKEN",
    ):
        assert not hasattr(mod, gone), (
            f"{gone} is back; physical-detail disclosure must not be decided "
            f"by how the caller authenticated (decision 2026-08-11, option C)"
        )


def test_execute_and_explain_publish_no_permanently_false_redaction_flag():
    """``routed_sql_redacted`` / ``rewritten_query_redacted`` /
    ``reason_redacted`` were removed WITH the control that was their only
    writer. A "was this withheld" flag that can only report ``false`` is false
    assurance — the same shape the R4 review already caught once on this very
    module. Fails against the pre-decision code, where all four fields existed.
    """
    from src.api.routes import ExecuteResponse, ExplainResponse

    assert not (
        {"routed_sql_redacted", "reason_redacted"} & set(ExecuteResponse.model_fields)
    )
    assert not (
        {"rewritten_query_redacted", "reason_redacted"}
        & set(ExplainResponse.model_fields)
    )
    body = json.dumps(_execute_response().model_dump(mode="json"))
    assert "_redacted" not in body


# --- the end-to-end identity guard, driven through the real ASGI routes ------

_TEST_TENANT = "test-tenant"


def _settings():
    from shared.config.settings import get_settings

    return get_settings()


def _mint_tenant_token() -> str:
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    st = _settings()
    return jwt.encode(
        {
            "sub": "user@example.com",
            "tenant_id": _TEST_TENANT,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            "role": "member",
        },
        st.JWT_SECRET_KEY,
        algorithm=st.JWT_ALGORITHM,
    )


def _mint_embed_token(rls_role: str | None) -> str:
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    st = _settings()
    payload = {
        "sub": "embed@example.com",
        "tenant_id": _TEST_TENANT,
        "aud": "embed",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "capabilities": ["query"],
    }
    if rls_role is not None:
        payload["rls"] = {"role": rls_role}
    return jwt.encode(payload, st.JWT_SECRET_KEY, algorithm=st.JWT_ALGORITHM)


@pytest.fixture
async def route_client():
    import httpx

    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as ac:
        yield ac


def _stub_route_pipeline(monkeypatch, *, handler_name, response):
    """Stub everything between the auth boundary and the response builder, so
    the only thing that can differ between two principals is the disclosure
    policy under test."""
    import uuid as _uuid
    from unittest.mock import AsyncMock

    model_id = _uuid.uuid4()
    model = types.SimpleNamespace(
        id=model_id, project_id=_uuid.uuid4(),
        deployed_version_id=_uuid.uuid4(), slug="test",
    )

    async def _db_gen(*a, **k):
        yield AsyncMock()

    monkeypatch.setattr("src.api.routes.get_tenant_db", _db_gen)
    monkeypatch.setattr(
        "src.api.routes.load_authorized_model", AsyncMock(return_value=model),
    )
    monkeypatch.setattr(
        "src.api.routes.resolve_execution_persona", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.api.routes._log_preexec_failure", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        f"src.api.routes.{handler_name}", AsyncMock(return_value=response),
    )
    return model_id


@pytest.mark.asyncio
@pytest.mark.parametrize("rls_role", [None, "manager", "modeler", "admin", "viewer"])
@pytest.mark.parametrize(
    ("path", "handler_name", "builder"),
    [
        ("/api/v1/execute", "_handle_execute", _execute_response),
        ("/api/v1/explain", "_handle_explain", _explain_response),
    ],
)
async def test_embed_and_tenant_principals_receive_identical_physical_detail(
    route_client, monkeypatch, rls_role, path, handler_name, builder,
):
    """THE new contract: disclosure is decided by ENTITLEMENT, not by the door.

    Drives the REAL route with a real embed token and a real tenant token over
    identical inputs and requires the served bodies to be byte-identical, for
    every RLS role an embed token can carry.

    Fails against the pre-decision code, where the embed body had its
    ``routed_sql`` / ``rewritten_query`` nulled, its aggregate/pocket ids
    dropped, its trace stripped of the rewritten SQL, the artifact descriptors
    and the target system, and its ``reason`` replaced by a sentinel token —
    while the tenant body kept all of it.
    """
    bodies = {}
    for label, token in (
        ("embed", _mint_embed_token(rls_role)),
        ("tenant", _mint_tenant_token()),
    ):
        model_id = _stub_route_pipeline(
            monkeypatch, handler_name=handler_name, response=builder(),
        )
        resp = await route_client.post(
            path,
            json={
                "model_id": str(model_id),
                "raw_query": "SELECT 1",
                "protocol": "jdbc",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, f"{label} {path}: {resp.text}"
        bodies[label] = resp.json()

    assert bodies["embed"] == bodies["tenant"], (
        f"{path} still answers differently depending on how the caller "
        f"authenticated; disclosure must key off entitlement"
    )
    # And it is the REAL detail both receive, not a jointly-stripped one.
    served = json.dumps(bodies["embed"])
    for kept in (_PHYS_TABLE, "acme_aggregates", "acme_source", "agg-uuid"):
        assert kept in served, f"{path}: {kept!r} was withheld from both callers"


def test_the_fallback_reason_no_longer_interpolates_the_guard_exception():
    """Guard the PRODUCER, which now matters MORE than it did.

    While the embed withhold existed, a reason string carrying
    ``(schema=... table=...)`` was scrubbed for the lowest-trust caller. With
    the withhold gone this producer is the only thing keeping the generation
    guard's exception out of a caller-visible field, so the guard is kept and
    strengthened in intent rather than retired with the redactor.
    """
    import inspect

    import src.api.routes as routes

    src_text = inspect.getsource(routes)
    marker = "servable at execution time; fell back to source"
    assert marker in src_text, "the fallback reason branch moved; re-point this guard"
    assert "fell back to source ({e})" not in src_text, (
        "the fallback reason interpolates the generation-guard exception, "
        "which carries (schema=... table=...) into ExecuteResponse.reason and "
        "trace.steps[router].detail"
    )


# ---------------------------------------------------------------------------
# ACCESS controls that share the ``is_embed`` flag and must NOT have moved.
#
# Removing either of these would GRANT embed tokens a capability they have
# never had — the exact opposite of the consistency fix. They are asserted here
# because this file is where a future reader looks to understand what the
# option-C removal did and did not touch.
# ---------------------------------------------------------------------------


def test_embed_principal_still_cannot_reach_simulate_as():
    """simulate-as is a human-admin debugging surface. An embed token must not
    reach it regardless of the admin-shaped RLS role it was minted with
    (Bug-7995 / F-024-01). This is ACCESS, not disclosure."""
    from src.api._simulate import resolve_principal

    for rls_role in (None, "tenant_admin", "system_admin", "admin"):
        with pytest.raises(HTTPException) as exc:
            resolve_principal(
                _embed(rls_role), "victim@x.com", "tenant_admin", None, None,
            )
        assert exc.value.status_code == 403


def test_bare_embed_token_still_carries_no_named_rls_role():
    """``predicate_compiler`` clears the ``embed`` sentinel so a bare embed
    token carries NO named RLS role and a role-governed model FAILS CLOSED.
    Removing that would widen which ROWS an embed token can read — a larger
    escalation than the simulate one. ACCESS, not disclosure."""
    from shared.security import Principal

    principal = Principal.from_current_user(_embed(None))
    assert principal.roles == frozenset(), (
        "a bare embed token gained a named RLS role; a role-governed model "
        "would stop failing closed for it"
    )


def test_f007_14_disclosure_gate_does_not_catch_base_exception():
    """F-007-14 / Bug-9140: entitlement lookup catches Exception, not BaseException."""
    import inspect
    from src.api import _sql_disclosure

    src = inspect.getsource(_sql_disclosure.may_disclose_physical_sql)
    assert "except BaseException" not in src
    assert "except Exception:" in src
