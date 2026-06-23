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

from sqlalchemy import delete

from shared.db.models import SsoState
from shared.db.session import get_system_db

# SSO flows are short-lived; 10 minutes is generous for an IdP round trip.
_STATE_TTL_SECONDS = 600
SSO_NONCE_COOKIE = "tsl_sso_nonce"


async def create_state(tenant_id: str, flow_type: str) -> tuple[str, str, str]:
    """Persist a new SSO flow state. Returns (state, browser_nonce, oidc_nonce)."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    oidc_nonce = secrets.token_urlsafe(32)
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
            expires_at=expires_at,
        ))
        await db.commit()
    return state, nonce, oidc_nonce


async def consume_state(
    state: str, flow_type: str, browser_nonce: str | None,
) -> tuple[str, str | None] | None:
    """Validate and consume an SSO state. Returns (tenant_id, oidc_nonce) or None.

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
            )
        )
        row = result.first()
        await db.commit()

        if row is None:
            return None

        tenant_id, stored_nonce, stored_oidc_nonce, stored_flow, expires_at = row

        if expires_at < datetime.now(timezone.utc):
            return None
        if stored_flow != flow_type:
            return None
        # Bug-5270: skip nonce check for SAML — the cross-site POST from
        # the IdP will never carry a SameSite=Lax cookie. SAML's signed
        # assertion + single-use state provide equivalent protection.
        # Nonce mismatch is intentionally non-fatal for SAML: the Lax cookie
        # is absent on cross-site POST; signed assertion + single-use state
        # provide the CSRF/replay protection instead.
        if flow_type != "saml" and stored_nonce and browser_nonce != stored_nonce:
            return None
        return tenant_id, stored_oidc_nonce
