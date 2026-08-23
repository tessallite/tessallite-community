"""Typed JWT contract for short-lived internal service principals."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import jwt

from shared.config.settings import get_settings

SERVICE_AUDIENCE = "service"
SERVICE_TOKEN_TYPE = "service"
SCOPE_AGENT_REFRESH = "agent.refresh-derived"
SCOPE_AGGREGATE_REBUILD = "scheduler.refresh-model"
SCOPE_CACHE_EVICT = "query-router.cache-evict"
SCOPE_DATA_QUALITY = "query-router.data-quality"
SCOPE_GLOSSARY_STATS_REFRESH = "optimizer.stats-refresh"
SCOPE_KPI_EVALUATE = "model-service.kpi-evaluate"
# The KPI evaluator crosses the model-service -> query-router boundary to
# execute the already-bound KPI expression. Keep that hop as its own typed
# capability; granting the model-service scope to query-router would make a
# cross-service name imply permission in the wrong service.
SCOPE_KPI_QUERY_EXECUTE = "query-router.kpi-execute"
SCOPE_POCKET_REFRESH = "query-router.pocket-refresh"
# Bug-8029: deploy/import-time predictive cold-start kickoff. Must match the
# scope constant the optimizer's cold-start route enforces
# (optimizer/src/api/cold_start_routes.SCOPE_PREDICTIVE_COLD_START).
SCOPE_PREDICTIVE_COLD_START = "optimizer.predictive-cold-start"
# Bug-8034 durable advisor dispatch (spec:
# docs/architecture/architecture_ai-advisor-durable-dispatch.md). Two hops, each
# signed-services-only:
#   optimizer -> scheduler  "start draining my tenant's queued advisor runs now"
#   scheduler -> optimizer  "execute this already-claimed run"
SCOPE_AI_RUN_DISPATCH = "scheduler.ai-run-dispatch"
SCOPE_AI_RUN_EXECUTE = "optimizer.ai-run-execute"

# Narrow service-only role for the KPI snapshot evaluator (Bug-7122).
# Sits below ``member`` in the rank hierarchy -- it grants no human-user
# capabilities and exists solely so the service token carries the
# least-privileged role label possible.  Authorization is scope-based
# (SCOPE_KPI_EVALUATE), not role-based, but a low role limits blast
# radius if the JWT leaks outside the service-principal flow.
KPI_EVALUATOR_ROLE = "kpi_evaluator"

_PRINCIPAL_POLICY = {
    "aggregate-rebuild-service": {
        "max_role": "tenant_admin",
        "scopes": frozenset({SCOPE_AGGREGATE_REBUILD}),
    },
    # Bug-8034 durable advisor dispatch. Both principals are pinned to the
    # lowest role in _ROLE_RANK: neither route makes a role-based decision
    # (authorisation is scope-based), and the work they start runs as an
    # internal job, so a leaked token must grant no human capability.
    "ai-run-dispatch": {
        "max_role": KPI_EVALUATOR_ROLE,
        "scopes": frozenset({SCOPE_AI_RUN_DISPATCH}),
    },
    "ai-run-executor": {
        "max_role": KPI_EVALUATOR_ROLE,
        "scopes": frozenset({SCOPE_AI_RUN_EXECUTE}),
    },
    "data-quality-validator": {
        "max_role": "system_admin",
        "scopes": frozenset({SCOPE_DATA_QUALITY}),
    },
    "glossary-bootstrap-service": {
        "max_role": "tenant_admin",
        "scopes": frozenset({SCOPE_GLOSSARY_STATS_REFRESH}),
    },
    "kpi-snapshot-sweep": {
        "max_role": KPI_EVALUATOR_ROLE,
        "scopes": frozenset({SCOPE_KPI_EVALUATE, SCOPE_KPI_QUERY_EXECUTE}),
    },
    "model-service-deploy": {
        "max_role": "tenant_admin",
        "scopes": frozenset(
            {SCOPE_AGENT_REFRESH, SCOPE_CACHE_EVICT, SCOPE_PREDICTIVE_COLD_START}
        ),
    },
    "pocket-refresh": {
        "max_role": "system_admin",
        "scopes": frozenset({SCOPE_POCKET_REFRESH}),
    },
    # Bug-9188: the Named Query refresh (shared/named_query/refresh.py:_mint_service_token)
    # mints this principal with role=system_admin + SCOPE_POCKET_REFRESH to compile the
    # materialisation SELECT via the query-router /explain endpoint (which requires
    # SCOPE_POCKET_REFRESH). Without registering it here, token verification rejected it
    # with "unsupported service principal" and EVERY NQ materialisation failed. It shares
    # the pocket-refresh scope because it performs the same operation (compile-for-
    # materialisation via /explain); the distinct principal name preserves audit + blast
    # radius. Mirrors "pocket-refresh".
    "named-query-refresh": {
        "max_role": "system_admin",
        "scopes": frozenset({SCOPE_POCKET_REFRESH}),
    },
}

_ROLE_RANK = {
    KPI_EVALUATOR_ROLE: 0,
    "member": 1,
    "modeler": 2,
    "tenant_admin": 3,
    "system_admin": 4,
}


def is_allowed_service_principal(name: str | None) -> bool:
    return isinstance(name, str) and name in _PRINCIPAL_POLICY


def _policy_for(principal: str) -> dict:
    if not is_allowed_service_principal(principal):
        raise ValueError("unsupported service principal")
    return _PRINCIPAL_POLICY[principal]


def validate_service_payload(payload: dict) -> tuple[str, str, str, list[str]]:
    """Return (principal, tenant_id, role, scopes) or raise ValueError."""
    principal = payload.get("service_principal")
    tenant_id = payload.get("tenant_id")
    role = payload.get("role")
    if payload.get("aud") != SERVICE_AUDIENCE:
        raise ValueError("service audience required")
    if payload.get("token_type") != SERVICE_TOKEN_TYPE:
        raise ValueError("service token_type required")
    if not is_allowed_service_principal(principal):
        raise ValueError("unsupported service principal")
    if payload.get("sub") != f"service:{principal}":
        raise ValueError("service subject mismatch")
    if not isinstance(tenant_id, str) or not tenant_id or tenant_id == "__system__":
        raise ValueError("tenant-scoped service token required")
    policy = _policy_for(principal)
    max_role = policy["max_role"]
    if role not in _ROLE_RANK or _ROLE_RANK[role] > _ROLE_RANK[max_role]:
        raise ValueError("unsupported service role")
    raw_scopes = payload.get("service_scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise ValueError("service scopes required")
    scopes = [str(scope) for scope in raw_scopes if isinstance(scope, str) and scope]
    if len(scopes) != len(raw_scopes):
        raise ValueError("service scopes malformed")
    if not set(scopes).issubset(policy["scopes"]):
        raise ValueError("unsupported service scope")
    return principal, tenant_id, role, scopes


def create_service_access_token(
    *,
    principal: str,
    tenant_id: str,
    role: str = "tenant_admin",
    ttl_minutes: int = 1,
    scopes: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Mint a short-lived signed service JWT using the shared contract."""
    if not isinstance(tenant_id, str) or not tenant_id or tenant_id == "__system__":
        raise ValueError("tenant-scoped service token required")
    policy = _policy_for(principal)
    max_role = policy["max_role"]
    if role not in _ROLE_RANK or _ROLE_RANK[role] > _ROLE_RANK[max_role]:
        raise ValueError("unsupported service role")
    requested_scopes = list(scopes) if scopes is not None else sorted(policy["scopes"])
    if not requested_scopes:
        raise ValueError("service scopes required")
    if not set(requested_scopes).issubset(policy["scopes"]):
        raise ValueError("unsupported service scope")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": f"service:{principal}",
        "tenant_id": tenant_id,
        "role": role,
        "aud": SERVICE_AUDIENCE,
        "token_type": SERVICE_TOKEN_TYPE,
        "service_principal": principal,
        "service_scopes": requested_scopes,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
    settings = get_settings()
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
