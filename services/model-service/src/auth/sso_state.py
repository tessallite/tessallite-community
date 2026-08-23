"""Durable SSO authorization-flow state store (F-021-05).

The SSO login leg and the callback leg may land on different replicas, so the
flow state cannot live in process memory. This module persists it in the system
DB (``tess_system.sso_states``) keyed by the random ``state`` value, with a
short TTL so abandoned flows are reaped rather than leaking forever.

A ``browser_nonce`` is stored alongside and mirrored to the browser via a
short-lived httpOnly cookie; the callback requires the cookie to match the
stored nonce (login-CSRF defence, F-021-09). When the cookie is absent the
flow still completes on the stored state alone (browsers that strip the cookie
on a cross-site redirect) — the binding hardens, it does not gate.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update

from shared.db.models import SsoState
from shared.db.session import get_system_db

# SSO flows are short-lived; 10 minutes is generous for an IdP round trip.
_STATE_TTL_SECONDS = 600
SSO_NONCE_COOKIE = "tsl_sso_nonce"


async def create_state(
    tenant_id: str, flow_type: str, request_id: str | None = None,
) -> tuple[str, str, str, str]:
    """Persist a new SSO flow state.

    Returns ``(state, browser_nonce, oidc_nonce, code_verifier)``.

    ``request_id`` (F-021-03) is the SAML AuthnRequest ID bound to this flow so
    the ACS callback can require the IdP's ``InResponseTo`` to match. It is
    ignored for non-SAML flows.

    Bug-8142: ``code_verifier`` is the PKCE (RFC 7636) high-entropy secret. It is
    generated here (43-128 unreserved chars; ``token_urlsafe(64)`` yields ~86),
    persisted with the flow, and returned so the OIDC login leg can place its
    S256 ``code_challenge`` on the authorization request. Only the challenge
    travels on the front channel; the verifier is replayed on the back-channel
    token exchange. SAML flows generate one too (uniform tuple) but ignore it.
    """
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    oidc_nonce = secrets.token_urlsafe(32)
    # RFC 7636 §4.1: verifier is 43-128 chars of the unreserved set.
    code_verifier = secrets.token_urlsafe(64)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_STATE_TTL_SECONDS)
    async for db in get_system_db():
        # Opportunistically reap expired rows so the table stays bounded even
        # if no dedicated sweep runs.
        await db.execute(
            delete(SsoState).where(SsoState.expires_at < datetime.now(timezone.utc))
        )
        db.add(SsoState(
            state=state,
            tenant_id=tenant_id,
            flow_type=flow_type,
            browser_nonce=nonce,
            oidc_nonce=oidc_nonce,
            request_id=request_id,
            code_verifier=code_verifier,
            expires_at=expires_at,
        ))
        await db.commit()
    return state, nonce, oidc_nonce, code_verifier


async def set_state_request_id(state: str, request_id: str | None) -> None:
    """F-021-03: stamp the SAML AuthnRequest ID onto an existing flow-state row.

    Called after ``create_state`` once the AuthnRequest has been built (its ID
    is only known then). No-op when ``request_id`` is None.
    """
    if request_id is None:
        return
    async for db in get_system_db():
        await db.execute(
            update(SsoState)
            .where(SsoState.state == state)
            .values(request_id=request_id)
        )
        await db.commit()


async def discard_state(state: str) -> None:
    """Delete a flow-state row (e.g. when AuthnRequest generation fails after
    the state was created), so a misconfigured setup leaves no orphan rows."""
    async for db in get_system_db():
        await db.execute(delete(SsoState).where(SsoState.state == state))
        await db.commit()


async def consume_state(
    state: str, flow_type: str, browser_nonce: str | None,
) -> tuple[str, str | None, str | None, str | None] | None:
    """Validate and consume an SSO state.

    Returns ``(tenant_id, oidc_nonce, request_id, code_verifier)`` or None.
    ``request_id`` (F-021-03) is the SAML AuthnRequest ID the ACS supplies to
    the SAML library to enforce ``InResponseTo``; it is None for OIDC / legacy
    rows. ``code_verifier`` (Bug-8142) is the PKCE verifier the OIDC callback
    replays on the token exchange; it is None for SAML / legacy rows. It is
    returned by the same atomic DELETE ... RETURNING that consumes the row, so
    the verifier is available exactly once and never outlives the flow.

    Single-use: the row is deleted on consumption. Returns None when the state
    is unknown, expired, of the wrong flow type, or when the browser nonce
    check fails (OIDC only). The returned ``oidc_nonce`` is the value the
    caller must verify against the id_token's ``nonce`` claim.

    Bug-5270 / Bug-5271: consumption is now atomic — a single DELETE ...
    RETURNING replaces the previous SELECT-then-DELETE, preventing two
    concurrent callbacks from both reading before either deletes.

    Bug-5270 — SAML browser-nonce exemption:
    The SAML POST binding is a cross-site POST from the IdP. Browsers
    conforming to RFC 6265bis do not send ``SameSite=Lax`` cookies on
    cross-site POST requests, so the browser_nonce cookie is never
    present on the SAML ACS callback. SAML already provides replay and
    CSRF protection via the signed assertion + the single-use RelayState
    (consumed atomically here), so the OIDC-style double-submit cookie
    is the wrong control for this flow. The nonce check is therefore
    skipped for ``flow_type=="saml"`` while remaining enforced for OIDC.
    """
    async for db in get_system_db():
        # Bug-5271: atomic consumption — DELETE ... RETURNING ensures that
        # two concurrent callbacks cannot both read the same row. Only the
        # first DELETE succeeds; the second gets no row back.
        result = await db.execute(
            delete(SsoState)
            .where(SsoState.state == state)
            .returning(
                SsoState.tenant_id,
                SsoState.browser_nonce,
                SsoState.oidc_nonce,
                SsoState.flow_type,
                SsoState.expires_at,
                SsoState.request_id,
                SsoState.code_verifier,
            )
        )
        row = result.first()
        await db.commit()

        if row is None:
            return None

        (
            tenant_id,
            stored_nonce,
            stored_oidc_nonce,
            stored_flow,
            expires_at,
            stored_request_id,
            stored_code_verifier,
        ) = row

        if expires_at < datetime.now(timezone.utc):
            return None
        if stored_flow != flow_type:
            return None
        if flow_type != "saml":
            # OIDC: nonce is required. A missing cookie or stored nonce is a
            # CSRF/replay failure, not a skip (F-021-11). SAML keeps the
            # SameSite=Lax cookie exemption below.
            if not stored_nonce or not browser_nonce or stored_nonce != browser_nonce:
                return None
            return tenant_id, stored_oidc_nonce, stored_request_id, stored_code_verifier
        # Bug-5270: skip nonce check for SAML — the cross-site POST from
        # the IdP will never carry a SameSite=Lax cookie. SAML's signed
        # assertion + single-use state provide equivalent protection.
        return tenant_id, stored_oidc_nonce, stored_request_id, stored_code_verifier
