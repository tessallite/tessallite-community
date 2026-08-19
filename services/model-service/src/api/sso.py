"""SSO endpoints: SAML 2.0 SP and OIDC authorization-code flow.

These are redirect-based authentication flows that operate outside the
credential-based auth chain.  The browser is redirected to the IdP, then
back to the ACS/callback endpoint, which validates the response, runs
JIT adoption, and redirects to the frontend with a JWT.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from shared.audit.logger import audit, audit_required
from shared.auth.cookie import _cookie_secure, set_auth_cookies
from shared.config.bootstrap import system_snapshot_get
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, require_tenant_admin
from src.auth.sso_overlay import current_overlay, tenant_sso_overlay
from src.auth.claim_bounds import (
    ClaimsTooLargeError,
    bound_token_claims,
    referenced_claim_names,
)
from src.auth.jit import jit_adopt_user, require_external_identity_admitted
from src.auth.local_backend import create_access_token
from src.auth.saml_backend import (
    build_authn_request,
    get_sp_metadata,
    process_saml_response,
)
from src.auth.oidc_backend import build_authorization_url, exchange_code
from src.auth.saml_replay import record_assertion_or_reject
from src.auth.sso_state import (
    SSO_NONCE_COOKIE,
    consume_state,
    create_state,
    discard_state,
    set_state_request_id,
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


def _configured_origins() -> list[str]:
    """Operator-configured origins this deployment is allowed to answer as.

    ``CORS_ORIGINS`` is the app origin the browser actually loads and the
    origin the reverse proxy forwards SSO requests under, so it is the correct
    allowlist. ``ALLOWED_EMBED_ORIGINS`` is deliberately NOT included: those
    are third-party ISV sites permitted to frame the app, and honouring one as
    our own callback origin would reopen exactly the hole this closes.
    """
    from shared.config.settings import get_settings
    raw = get_settings().CORS_ORIGINS or ""
    origins = []
    for candidate in raw.split(","):
        candidate = candidate.strip().rstrip("/")
        if candidate and candidate != "*":
            origins.append(candidate)
    return origins


def _base_url(request: Request) -> str:
    """Return the absolute, externally-reachable origin of this deployment.

    Bug-6307: this value becomes the SAML AssertionConsumerService URL, the SP
    metadata entity/ACS URLs, the ``Destination`` the SAML response is checked
    against, and the OIDC ``redirect_uri``. It was previously reconstructed
    from ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` (or, failing those, the
    request's own ``Host`` header) — all client-controlled. An attacker could
    therefore start a login carrying ``X-Forwarded-Host: evil.example`` and
    have the IdP post the signed assertion to their own endpoint, or replay a
    captured assertion past the Destination check by naming the host it was
    minted for.

    Resolution order, fail-closed:

    1. ``PUBLIC_BASE_URL`` when configured — headers are ignored entirely.
    2. Otherwise the origin derived from the request, accepted only if it is
       one of the operator-configured ``CORS_ORIGINS``.
    3. Otherwise refuse. SSO cannot publish a callback URL it cannot vouch
       for, and guessing one is what created the vulnerability.
    """
    from shared.config.settings import get_settings

    configured = (get_settings().PUBLIC_BASE_URL or "").strip().rstrip("/")
    if configured:
        return configured

    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    # A proxy may append to X-Forwarded-*; the first entry is the client-facing
    # value. Take it, then hold it to the allowlist.
    scheme = scheme.split(",")[0].strip().lower()
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    host = host.split(",")[0].strip()
    derived = f"{scheme}://{host}".rstrip("/")

    allowed = _configured_origins()
    if derived.lower() in {origin.lower() for origin in allowed}:
        return derived

    logger.error(
        "SSO refused: request origin %r is not a configured origin. Set "
        "PUBLIC_BASE_URL to this deployment's external URL, or add it to "
        "CORS_ORIGINS. Configured origins: %s",
        derived, allowed or "<none>",
    )
    raise HTTPException(
        status_code=503,
        detail=(
            "SSO is not configured for this host. Set PUBLIC_BASE_URL to the "
            "deployment's external URL."
        ),
    )


# -----------------------------------------------------------------------
# Available backends discovery
# -----------------------------------------------------------------------

@router.get("/backends")
async def list_backends(tenant_id: str | None = Query(None)) -> dict:
    """Return which auth backends are actually in the login chain (F-021-03)."""
    if tenant_id:
        async with tenant_sso_overlay(tenant_id):
            return _backends_payload()
    return _backends_payload()


def _backends_payload() -> dict:
    from shared.config.settings import get_settings
    from src.auth.chain import get_auth_chain

    settings = get_settings()
    chain_names = list(get_auth_chain().backend_names)
    auth_backends = [n.strip().lower() for n in settings.AUTH_BACKENDS.split(",") if n.strip()]
    names = list(dict.fromkeys(
        chain_names + [n for n in auth_backends if n in ("saml", "oidc")]
    ))
    return {
        "backends": names,
        "saml_enabled": _saml_configured(),
        "oidc_enabled": _oidc_configured(),
        "ldap_enabled": bool(settings.LDAP_ENABLED),
        "gcp_iam_enabled": bool(settings.GCP_IAM_AUDIENCE),
    }


def _saml_configured() -> bool:
    ov = current_overlay().get("saml") or {}
    if ov.get("idp_metadata_url") or ov.get("idp_metadata_xml"):
        return True
    from shared.config.settings import get_settings
    s = get_settings()
    return bool(s.SAML_IDP_METADATA_URL or s.SAML_IDP_METADATA_XML)


def _oidc_configured() -> bool:
    ov = current_overlay().get("oidc") or {}
    if ov.get("issuer") and ov.get("client_id"):
        return True
    from shared.config.settings import get_settings
    s = get_settings()
    return bool(s.OIDC_ISSUER and s.OIDC_CLIENT_ID)


def _require_configured(configured: bool, name: str) -> None:
    if not configured:
        raise HTTPException(status_code=404, detail=f"{name} not configured")


# -----------------------------------------------------------------------
# SAML 2.0 SP
# -----------------------------------------------------------------------

@router.get("/saml/metadata")
async def saml_metadata(request: Request) -> Response:
    """Serve SP metadata XML for the IdP to consume."""
    _require_configured(_saml_configured(), "SAML")
    xml = get_sp_metadata(_base_url(request))
    if xml is None:
        raise HTTPException(status_code=404, detail="SAML not configured")
    return Response(content=xml, media_type="application/xml")


@router.get("/saml/login")
async def saml_login(
    request: Request,
    tenant_id: str = Query(...),
) -> RedirectResponse:
    """Initiate SAML AuthnRequest: redirect browser to IdP.

    F-021-03: the flow state is created first (to obtain the RelayState), the
    AuthnRequest is then built with that RelayState, and the request's own ID is
    stored back onto the state row. The ACS callback supplies that ID as the
    expected ``request_id``, binding the returned assertion to this exact login
    attempt (``InResponseTo`` validation).
    """
    # Bug-6307: resolve (and validate) the callback origin BEFORE creating the
    # flow state, so a rejected origin cannot leave an orphan state row behind
    # on every attempt — the same reason ``discard_state`` exists below.
    async with tenant_sso_overlay(tenant_id):
        _require_configured(_saml_configured(), "SAML")
        base_url = _base_url(request)
        state, nonce, _oidc_nonce, _code_verifier = await create_state(tenant_id, "saml")
        built = build_authn_request(base_url, relay_state=state)
        if built is None:
            await discard_state(state)
            raise HTTPException(status_code=500, detail="SAML AuthnRequest generation failed")
        redirect_url, request_id = built
        await set_state_request_id(state, request_id)
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
    tenant_id, _oidc_nonce, expected_request_id, _code_verifier = consumed

    client_ip = request.client.host if request.client else None

    async with tenant_sso_overlay(tenant_id):
        result = process_saml_response(
            _base_url(request), SAMLResponse, relay_state=RelayState,
            expected_request_id=expected_request_id,
        )
    if result is None:
        async for db in get_tenant_db(tenant_id):
            await audit(
                db, action="auth.sso_failure", severity="critical",
                ip_address=client_ip, detail={"method": "saml"},
            )
            await db.commit()
        raise HTTPException(status_code=401, detail="SAML authentication failed")

    identity = result.identity

    # F-021-03: replay ledger — record the assertion ID atomically and reject a
    # second use of the same (still-valid, signed) assertion. This closes the
    # capture-and-replay-with-a-fresh-state hole that single-use RelayState alone
    # does not cover.
    first_use = await record_assertion_or_reject(
        assertion_id=result.assertion_id,
        tenant_id=tenant_id,
        not_on_or_after=result.not_on_or_after,
    )
    if not first_use:
        async for db in get_tenant_db(tenant_id):
            await audit(
                db, action="auth.sso_failure", severity="critical",
                ip_address=client_ip,
                detail={"method": "saml", "reason": "assertion_replay"},
            )
            await db.commit()
        raise HTTPException(status_code=401, detail="SAML assertion replay detected")

    canonical_email = identity.email
    token_claims: dict = {}
    async for db in get_tenant_db(tenant_id):
        await require_external_identity_admitted(db, identity)
        rls_claim_names = await referenced_claim_names(db)
        try:
            token_claims = bound_token_claims(
                identity.raw_claims or {}, rls_claim_names,
                backend="saml", subject=identity.email.strip().lower(),
            )
        except ClaimsTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        local_user, role = await jit_adopt_user(db, identity, tenant_id)
        # F-021-09: the JWT subject must be the canonical (lowercased) email so
        # it matches the UserAccessBinding.user_identity that JIT writes.
        canonical_email = local_user.email
        await audit(
            db, action="auth.login_success", severity="info",
            actor_id=local_user.id, actor_email=canonical_email,
            ip_address=client_ip,
            detail={"backend": "saml", "role": role},
        )
        await db.commit()

    # F-007-02/Bug-1072: the IdP claim set was bounded before any JIT mutation
    # above, so an oversized SAML token fails without creating/changing a user.
    token = create_access_token(
        sub=canonical_email, tenant_id=tenant_id, role=role,
        groups=identity.groups or [], claims=token_claims,
        token_version=getattr(local_user, "token_version", 0),
        # Bug-8017: RLS named-role subject. The named RLS roles of an SSO user
        # come from their IdP groups/claims (already carried above), which the
        # idp_group/saml_claim/oidc_scope rules match. The single RBAC-tier role
        # is emitted as a single-element jwt_role subject; there is no
        # multi-named-role local grant.
        roles=[role] if role else [],
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
    # Bug-6307: validate the callback origin before creating flow state (see
    # ``saml_login``) so a rejected origin does not accumulate orphan rows.
    async with tenant_sso_overlay(tenant_id):
        _require_configured(_oidc_configured(), "OIDC")
        base_url = _base_url(request)
        state, nonce, oidc_nonce, code_verifier = await create_state(tenant_id, "oidc")
        redirect_url = await build_authorization_url(
            base_url, state=state, tenant_id=tenant_id, nonce=oidc_nonce,
            code_verifier=code_verifier,
        )
        if redirect_url is None:
            await discard_state(state)
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
    tenant_id, expected_nonce, _request_id, code_verifier = consumed

    client_ip = request.client.host if request.client else None

    # Bug-8142: replay the PKCE verifier on the back-channel token exchange.
    async with tenant_sso_overlay(tenant_id):
        identity = await exchange_code(
            _base_url(request), code, expected_nonce=expected_nonce,
            code_verifier=code_verifier,
        )
    if identity is None:
        async for db in get_tenant_db(tenant_id):
            await audit(
                db, action="auth.sso_failure", severity="critical",
                ip_address=client_ip, detail={"method": "oidc"},
            )
            await db.commit()
        raise HTTPException(status_code=401, detail="OIDC authentication failed")

    canonical_email = identity.email
    token_claims: dict = {}
    async for db in get_tenant_db(tenant_id):
        await require_external_identity_admitted(db, identity)
        rls_claim_names = await referenced_claim_names(db)
        try:
            token_claims = bound_token_claims(
                identity.raw_claims or {}, rls_claim_names,
                backend="oidc", subject=identity.email.strip().lower(),
            )
        except ClaimsTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc))
        local_user, role = await jit_adopt_user(db, identity, tenant_id)
        # F-021-09: canonical (lowercased) email is the JWT subject so it
        # matches the binding user_identity written by JIT.
        canonical_email = local_user.email
        await audit(
            db, action="auth.login_success", severity="info",
            actor_id=local_user.id, actor_email=canonical_email,
            ip_address=client_ip,
            detail={"backend": "oidc", "role": role},
        )
        await db.commit()

    # F-007-02/Bug-1072: the IdP claim set was bounded before any JIT mutation
    # above, so an oversized OIDC token fails without creating/changing a user.
    token = create_access_token(
        sub=canonical_email, tenant_id=tenant_id, role=role,
        groups=identity.groups or [], claims=token_claims,
        token_version=getattr(local_user, "token_version", 0),
        # Bug-8017: RLS named-role subject (see SAML callback). Named RLS roles
        # for an SSO user come from IdP groups/claims; the RBAC-tier role is a
        # single-element jwt_role subject with no multi-named-role local grant.
        roles=[role] if role else [],
    )
    max_age = int(system_snapshot_get("auth.jwt_expire_minutes")) * 60
    cb_params = urlencode({"tenant_id": tenant_id})
    redirect = RedirectResponse(
        url=f"/sso/callback?{cb_params}",
        status_code=302,
    )
    set_auth_cookies(redirect, token, max_age_seconds=max_age)
    _clear_nonce_cookie(redirect)
    return redirect


SSO_CONFIG_KEY = "sso.config"


def _public_sso_config(raw: dict) -> dict:
    """Strip encrypted secrets from the admin GET payload."""
    oidc = dict(raw.get("oidc") or {})
    oidc.pop("client_secret", None)
    oidc.pop("client_secret_enc", None)
    oidc["client_secret_set"] = bool((raw.get("oidc") or {}).get("client_secret_enc"))
    return {
        "saml": dict(raw.get("saml") or {}),
        "oidc": oidc,
    }


@router.get("/sso-config")
async def get_sso_config(
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> dict:
    """Tenant-admin IdP settings (G-021-02). Secrets are never returned."""
    from sqlalchemy import select

    from shared.db.models import TenantSetting

    async for db in get_tenant_db(current_user.tenant_id):
        row = (
            await db.execute(
                select(TenantSetting).where(TenantSetting.key == SSO_CONFIG_KEY)
            )
        ).scalar_one_or_none()
        raw = dict(row.value_json) if row is not None else {}
        return _public_sso_config(raw)
    return _public_sso_config({})


@router.put("/sso-config")
async def put_sso_config(
    body: dict,
    current_user: CurrentUser = Depends(require_tenant_admin),
) -> dict:
    """Persist tenant SSO overlay. OIDC client_secret is Fernet-encrypted at rest.

    Bug-9325: the form is a legitimate SUBSET editor — it submits only
    ``oidc.issuer``/``oidc.client_id``/optional ``oidc.client_secret`` and
    ``saml.idp_metadata_url``. Other overlay keys (``oidc.scopes``,
    ``oidc.groups_claim``, ``saml.idp_metadata_xml``) may have been set through a
    direct API PUT and are consumed by the OIDC/SAML backends. So we PRESERVE by
    default: the existing overlay is read and the incoming body is shallow-merged
    per section, keeping any key the request omits. Read and write happen in ONE
    transaction with the existing row locked ``FOR UPDATE`` so two concurrent
    Saves serialise and cannot lose an update.
    """
    import base64

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from shared.db.models import TenantSetting
    from shared.security.credential_crypto import encrypt_str

    incoming_saml = dict(body.get("saml") or {})
    incoming_oidc = dict(body.get("oidc") or {})
    secret = incoming_oidc.pop("client_secret", None)
    # client_secret_enc is server-managed and never accepted from the client;
    # it is only ever produced here from a plaintext client_secret.
    incoming_oidc = {k: v for k, v in incoming_oidc.items() if k != "client_secret_enc"}

    async for db in get_tenant_db(current_user.tenant_id):
        existing_row = (
            await db.execute(
                select(TenantSetting)
                .where(TenantSetting.key == SSO_CONFIG_KEY)
                .with_for_update()
            )
        ).scalar_one_or_none()
        existing = dict(existing_row.value_json) if existing_row is not None else {}
        existing_saml = dict(existing.get("saml") or {})
        existing_oidc = dict(existing.get("oidc") or {})

        # Preserve-by-default: keys the body omits survive unchanged; keys the
        # body carries override. Existing client_secret_enc survives here because
        # the incoming oidc never carries it.
        merged_saml = {**existing_saml, **incoming_saml}
        merged_oidc = {**existing_oidc, **incoming_oidc}

        secret_set = isinstance(secret, str) and bool(secret.strip())
        if secret_set:
            merged_oidc["client_secret_enc"] = base64.b64encode(
                encrypt_str(secret.strip())
            ).decode("ascii")

        payload = {"saml": merged_saml, "oidc": merged_oidc}
        stmt = (
            pg_insert(TenantSetting)
            .values(
                key=SSO_CONFIG_KEY,
                value_json=payload,
                updated_by=current_user.email,
            )
            .on_conflict_do_update(
                index_elements=[TenantSetting.key],
                set_={"value_json": payload, "updated_by": current_user.email},
            )
        )
        await db.execute(stmt)
        # Audit detail reflects the keys the REQUEST actually changed, not the
        # full preserved overlay, so it stays truthful about the mutation.
        changed_oidc_keys = sorted(
            set(incoming_oidc.keys()) | ({"client_secret_enc"} if secret_set else set())
        )
        await audit_required(
            db, action="sso.config_update", severity="warn",
            actor_email=current_user.email, target_type="sso",
            detail={
                "saml_keys": sorted(incoming_saml.keys()),
                "oidc_keys": changed_oidc_keys,
            },
        )
        await db.commit()
        return _public_sso_config(payload)
    return _public_sso_config({"saml": incoming_saml, "oidc": incoming_oidc})

