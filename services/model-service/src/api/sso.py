"""SSO endpoints: SAML 2.0 SP and OIDC authorization-code flow.

These are redirect-based authentication flows that operate outside the
credential-based auth chain.  The browser is redirected to the IdP, then
back to the ACS/callback endpoint, which validates the response, runs
JIT adoption, and redirects to the frontend with a JWT.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from shared.audit.logger import audit
from shared.auth.cookie import _cookie_secure, set_auth_cookies
from shared.config.bootstrap import system_snapshot_get
from shared.db.session import get_tenant_db
from src.auth.claim_bounds import (
    ClaimsTooLargeError,
    bound_token_claims,
    referenced_claim_names,
)
from src.auth.jit import jit_adopt_user
from src.auth.local_backend import create_access_token
from src.auth.saml_backend import (
    build_authn_request,
    get_sp_metadata,
    process_saml_response,
)
from src.auth.oidc_backend import build_authorization_url, exchange_code
from src.auth.sso_state import (
    SSO_NONCE_COOKIE,
    consume_state,
    create_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["sso"])

# F-021-05: SSO flow state is now persisted in tess_system.sso_states (durable,
# multi-replica-safe, TTL-bounded) via src.auth.sso_state — no per-process dict.

_NONCE_COOKIE_MAX_AGE = 600


def _set_nonce_cookie(response: RedirectResponse, nonce: str) -> None:
    """Bind the SSO flow to the initiating browser (login-CSRF defence)."""
    response.set_cookie(
        key=SSO_NONCE_COOKIE,
        value=nonce,
        max_age=_NONCE_COOKIE_MAX_AGE,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _clear_nonce_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(key=SSO_NONCE_COOKIE, path="/")


def _base_url(request: Request) -> str:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{scheme}://{host}"


# -----------------------------------------------------------------------
# Available backends discovery
# -----------------------------------------------------------------------

@router.get("/backends")
async def list_backends() -> dict:
    """Return which auth backends are configured for frontend login UI."""
    from shared.config.settings import get_settings
    settings = get_settings()
    names = [n.strip().lower() for n in settings.AUTH_BACKENDS.split(",") if n.strip()]
    return {
        "backends": names,
        "saml_enabled": "saml" in names,
        "oidc_enabled": "oidc" in names,
    }


# -----------------------------------------------------------------------
# SAML 2.0 SP
# -----------------------------------------------------------------------

@router.get("/saml/metadata")
async def saml_metadata(request: Request) -> Response:
    """Serve SP metadata XML for the IdP to consume."""
    xml = get_sp_metadata(_base_url(request))
    if xml is None:
        raise HTTPException(status_code=404, detail="SAML not configured")
    return Response(content=xml, media_type="application/xml")


@router.get("/saml/login")
async def saml_login(
    request: Request,
    tenant_id: str = Query(...),
) -> RedirectResponse:
    """Initiate SAML AuthnRequest: redirect browser to IdP."""
    # Persist flow state only once we know the request can be built — otherwise
    # a misconfigured SAML setup would leave orphan state rows on every attempt.
    state, nonce, _oidc_nonce = await create_state(tenant_id, "saml")
    redirect_url = build_authn_request(_base_url(request), relay_state=state)
    if redirect_url is None:
        raise HTTPException(status_code=500, detail="SAML AuthnRequest generation failed")
    redirect = RedirectResponse(url=redirect_url, status_code=302)
    _set_nonce_cookie(redirect, nonce)
    return redirect


@router.post("/saml/acs")
async def saml_acs(
    request: Request,
    SAMLResponse: str = Form(...),
    RelayState: str = Form(None),
) -> RedirectResponse:
    """Assertion Consumer Service: process SAML response from IdP."""
    browser_nonce = request.cookies.get(SSO_NONCE_COOKIE)
    consumed = (
        await consume_state(RelayState, "saml", browser_nonce) if RelayState else None
    )
    if consumed is None:
        raise HTTPException(status_code=400, detail="Invalid or expired SSO state")
    tenant_id, _oidc_nonce = consumed

    client_ip = request.client.host if request.client else None

    identity = process_saml_response(
        _base_url(request), SAMLResponse, relay_state=RelayState,
    )
    if identity is None:
        async for db in get_tenant_db(tenant_id):
            await audit(
                db, action="auth.sso_failure", severity="critical",
                ip_address=client_ip, detail={"method": "saml"},
            )
            await db.commit()
        raise HTTPException(status_code=401, detail="SAML authentication failed")

    rls_claim_names: set[str] = set()
    canonical_email = identity.email
    async for db in get_tenant_db(tenant_id):
        local_user, role = await jit_adopt_user(db, identity, tenant_id)
        # F-021-09: the JWT subject must be the canonical (lowercased) email so
        # it matches the UserAccessBinding.user_identity that JIT writes.
        canonical_email = local_user.email
        rls_claim_names = await referenced_claim_names(db)
        await audit(
            db, action="auth.login_success", severity="info",
            actor_id=local_user.id, actor_email=canonical_email,
            ip_address=client_ip,
            detail={"backend": "saml", "role": role},
        )
        await db.commit()

    # F-007-02: carry IdP groups + SAML attributes on the JWT so
    # idp_group / saml_claim row-security rules are enforced at query time.
    # Bug-1072: bound the attribute set to rule-referenced + allow-listed
    # names and refuse to issue an oversized token.
    try:
        token_claims = bound_token_claims(
            identity.raw_claims or {}, rls_claim_names,
            backend="saml", subject=canonical_email,
        )
    except ClaimsTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    token = create_access_token(
        sub=canonical_email, tenant_id=tenant_id, role=role,
        groups=identity.groups or [], claims=token_claims,
    )
    max_age = int(system_snapshot_get("auth.jwt_expire_minutes")) * 60
    # Bug-836 fix: do not pass role in URL — frontend resolves it via /users/me
    cb_params = urlencode({"tenant_id": tenant_id})
    redirect = RedirectResponse(
        url=f"/sso/callback?{cb_params}",
        status_code=302,
    )
    set_auth_cookies(redirect, token, max_age_seconds=max_age)
    _clear_nonce_cookie(redirect)
    return redirect


# -----------------------------------------------------------------------
# OIDC Authorization-Code Flow
# -----------------------------------------------------------------------

@router.get("/oidc/login")
async def oidc_login(
    request: Request,
    tenant_id: str = Query(...),
) -> RedirectResponse:
    """Initiate OIDC authorization-code flow: redirect browser to IdP."""
    state, nonce, oidc_nonce = await create_state(tenant_id, "oidc")

    redirect_url = await build_authorization_url(
        _base_url(request), state=state, tenant_id=tenant_id, nonce=oidc_nonce,
    )
    if redirect_url is None:
        raise HTTPException(status_code=500, detail="OIDC authorization URL generation failed")
    redirect = RedirectResponse(url=redirect_url, status_code=302)
    _set_nonce_cookie(redirect, nonce)
    return redirect


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    """OIDC callback: exchange code for tokens and complete login."""
    browser_nonce = request.cookies.get(SSO_NONCE_COOKIE)
    consumed = await consume_state(state, "oidc", browser_nonce)
    if consumed is None:
        raise HTTPException(status_code=400, detail="Invalid or expired SSO state")
    tenant_id, expected_nonce = consumed

    client_ip = request.client.host if request.client else None

    identity = await exchange_code(_base_url(request), code, expected_nonce=expected_nonce)
    if identity is None:
        async for db in get_tenant_db(tenant_id):
            await audit(
                db, action="auth.sso_failure", severity="critical",
                ip_address=client_ip, detail={"method": "oidc"},
            )
            await db.commit()
        raise HTTPException(status_code=401, detail="OIDC authentication failed")

    rls_claim_names: set[str] = set()
    canonical_email = identity.email
    async for db in get_tenant_db(tenant_id):
        local_user, role = await jit_adopt_user(db, identity, tenant_id)
        # F-021-09: canonical (lowercased) email is the JWT subject so it
        # matches the binding user_identity written by JIT.
        canonical_email = local_user.email
        rls_claim_names = await referenced_claim_names(db)
        await audit(
            db, action="auth.login_success", severity="info",
            actor_id=local_user.id, actor_email=canonical_email,
            ip_address=client_ip,
            detail={"backend": "oidc", "role": role},
        )
        await db.commit()

    # F-007-02: carry IdP groups + OIDC claims/scopes on the JWT so
    # idp_group / oidc_scope row-security rules are enforced at query time.
    # Bug-1072: bound the claim set to rule-referenced + allow-listed
    # names and refuse to issue an oversized token.
    try:
        token_claims = bound_token_claims(
            identity.raw_claims or {}, rls_claim_names,
            backend="oidc", subject=canonical_email,
        )
    except ClaimsTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    token = create_access_token(
        sub=canonical_email, tenant_id=tenant_id, role=role,
        groups=identity.groups or [], claims=token_claims,
    )
    max_age = int(system_snapshot_get("auth.jwt_expire_minutes")) * 60
    # Bug-836 fix: do not pass role in URL — frontend resolves it via /users/me
    cb_params = urlencode({"tenant_id": tenant_id})
    redirect = RedirectResponse(
        url=f"/sso/callback?{cb_params}",
        status_code=302,
    )
    set_auth_cookies(redirect, token, max_age_seconds=max_age)
    _clear_nonce_cookie(redirect)
    return redirect
