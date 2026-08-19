"""Personal Access Token (PAT) service (Bug-7314).

A PAT is a long-lived bearer secret that lets SSO (SAML/OIDC) users — who have
no password — authenticate to JDBC/XMLA BI clients (Excel XMLA Basic, Power BI
PostgreSQL :5433). The user mints a PAT in the web UI and pastes it as the
PASSWORD in the client.

Security model (all enforced here — this is the sanctioned validation path):

* At rest we store ONLY a bcrypt hash of the plaintext token, plus a short
  non-secret ``token_prefix`` used to narrow the candidate set. No part of the
  random secret is persisted. The plaintext is returned exactly once, at
  creation.
* The plaintext carries >= 256 bits of entropy (32 random bytes, base64url) and
  a fixed, bounded shape (``tesspat_<public_id>_<secret>``), always < 72 bytes so
  bcrypt never truncates it.
* Validation is constant-work: exactly ONE bcrypt verify runs for every
  PAT-shaped input — a real candidate when the prefix matches, or a fixed dummy
  hash when it does not — so "no such token" is not a faster path than "wrong
  secret". A malformed / oversized input is rejected by a cheap format check and
  never reaches bcrypt (so it can never raise and become a 503 upstream).
* Expiry and revocation are enforced on EVERY validation, and re-checked against
  fresh DB state AFTER the bcrypt so a revoke/expiry landing during the (slow)
  bcrypt window cannot be missed (TOCTOU).
* Tenant + role are resolved LIVE from the owning ``local_users`` row at
  validation time; they are never stamped onto the token.
* The plaintext token and the full hash are never logged.
"""
from __future__ import annotations

import logging
import re
import secrets
import string
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import LocalUser, PersonalAccessToken

from src.auth.local_backend import hash_password, verify_password

logger = logging.getLogger(__name__)

# Fixed, recognizable scheme marker. Chosen so a PAT NEVER starts with "ey"
# (the gateway's JWT-direct sentinel) and is detectable by secret scanners.
PAT_SCHEME_PREFIX = "tesspat_"
# Public id: fixed-length hex, non-secret, embedded in the lookup prefix. 48
# bits makes a cross-token prefix collision negligible; the single-bcrypt
# guarantee in validate_pat (LIMIT 1) closes the timing oracle regardless, so
# this is defence-in-depth only. Kept short enough that the whole token stays
# well under bcrypt's 72-byte input limit.
_PUBLIC_ID_HEX_LEN = 12
# Random secret entropy: 32 bytes = 256 bits.
_SECRET_BYTES = 32
# Upper bound on an acceptable PAT length. The generated token is a fixed 60
# bytes; we allow a little slack but reject anything that could exceed bcrypt's
# 72-byte input limit (bcrypt >= 4.1 RAISES beyond it — which would otherwise
# surface as a 503 on the discovery-login path). Anything longer is not one of
# our tokens, so reject it cheaply before hashing.
_MAX_PAT_LEN = 72

# Exact public-id shape: ``_PUBLIC_ID_HEX_LEN`` lowercase hex chars (what
# ``generate_token`` emits). A prefix that does not conform is not one of our
# tokens; rejecting it in ``is_pat_form`` avoids a needless prefix DB lookup +
# dummy bcrypt for delimiter-complete garbage (external review R3 finding 1).
# Use fullmatch below: an anchored pattern with ``$`` would still admit a
# trailing newline (Python ``$`` matches before a final ``\n``), so a
# newline-tainted public id could slip past. ``fullmatch`` requires the WHOLE
# segment to be exactly 12 lowercase hex.
_PUBLIC_ID_RE = re.compile(r"[0-9a-f]{%d}" % _PUBLIC_ID_HEX_LEN)
# Secret alphabet: base64url (``secrets.token_urlsafe`` output). Non-empty and
# drawn only from [A-Za-z0-9_-]. A non-conforming secret is not one of our
# tokens either.
_SECRET_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")

# A precomputed valid bcrypt hash of a random string. Used to pay the bcrypt
# cost on a prefix-miss so "no such token" is not a timing oracle vs
# "wrong secret".
_DUMMY_HASH = hash_password("tessallite-pat-dummy-no-match")


def is_pat_scheme(secret: str) -> bool:
    """Return True if *secret* merely CARRIES the PAT scheme prefix.

    This is the ROUTING predicate: anything that starts with ``tesspat_`` is a
    PAT-scheme credential and must be handled terminally by the PAT path — even
    if it is malformed or oversized — so a bearer secret (or a value shaped like
    one) is NEVER forwarded to LDAP/local. Format validity is a SEPARATE concern
    (``is_pat_form``); routing must not depend on it, or a malformed PAT would
    leak to the next backend (external review R2 finding 1).
    """
    return bool(secret) and secret.startswith(PAT_SCHEME_PREFIX)


def is_pat_form(secret: str) -> bool:
    """Return True if *secret* is a well-formed, bounded PAT.

    This is the VALIDATION predicate used inside ``validate_pat`` to reject a
    malformed/oversized ``tesspat_``-string before it reaches bcrypt. It is total
    and never raises (a lone-surrogate string cannot raise here). A ``True``
    result guarantees ``_public_id`` yields a non-empty prefix and the value is
    within bcrypt's input bound.
    """
    if not is_pat_scheme(secret):
        return False
    try:
        encoded_len = len(secret.encode("utf-8"))
    except (UnicodeEncodeError, UnicodeError):
        # e.g. a lone surrogate — not one of our tokens; reject cleanly.
        return False
    if encoded_len > _MAX_PAT_LEN:
        return False
    # The random secret is base64url and MAY itself contain "_", so split with a
    # bounded maxsplit=2: ["tesspat", "<public_id>", "<secret-which-may-have-_>"].
    parts = secret.split("_", 2)
    if len(parts) != 3:
        return False
    public_id, secret_part = parts[1], parts[2]
    # Public id must be exactly our fixed lowercase-hex shape, and the secret
    # must be a non-empty base64url string. Rejecting delimiter-complete garbage
    # here avoids a pointless prefix lookup + dummy bcrypt (R3 finding 1).
    if not _PUBLIC_ID_RE.fullmatch(public_id):
        return False
    if not secret_part or any(ch not in _SECRET_ALPHABET for ch in secret_part):
        return False
    return True


def _public_id(token: str) -> str:
    """Return the non-secret lookup prefix ``tesspat_<public_id>``.

    Assumes ``is_pat_form(token)`` already held; returns "" defensively if not.
    """
    parts = token.split("_", 2)
    if len(parts) < 3 or not parts[1]:
        return ""
    return f"{PAT_SCHEME_PREFIX}{parts[1]}"


def generate_token() -> tuple[str, str, str]:
    """Mint a new PAT.

    Returns ``(plaintext, token_prefix, token_hash)``.

    * ``plaintext`` — ``tesspat_<public_id>_<secret>``; returned to the caller
      exactly once and never persisted.
    * ``token_prefix`` — ``tesspat_<public_id>``; non-secret, stored + indexed.
    * ``token_hash`` — bcrypt hash of the full plaintext; stored.

    No part of the random secret is persisted or returned as metadata.
    """
    public_id = secrets.token_hex(_PUBLIC_ID_HEX_LEN // 2)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    plaintext = f"{PAT_SCHEME_PREFIX}{public_id}_{secret}"
    token_prefix = f"{PAT_SCHEME_PREFIX}{public_id}"
    token_hash = hash_password(plaintext)
    return plaintext, token_prefix, token_hash


async def validate_pat(
    db: AsyncSession, *, token: str, email: str | None = None
) -> LocalUser | None:
    """Validate a PAT against the tenant DB and return its owning active user.

    Returns the ``LocalUser`` on success, or ``None`` on any failure (not
    PAT-shaped, unknown prefix, hash mismatch, expired, revoked, missing/inactive
    owner, or email mismatch). Never raises for a bad credential — callers treat
    ``None`` as an auth failure — and never mutates the session (it does NOT
    touch ``last_used_at``; the caller records usage only after the login
    succeeds).

    ``email``, when provided, must match the token owner's email
    (case-insensitive): BI clients send the user's email as the username, so
    binding the two prevents a valid PAT for user A from authenticating a session
    that claims to be user B.
    """
    if not is_pat_form(token):
        return None

    prefix = _public_id(token)
    # Select the id + hash of AT MOST ONE candidate for this prefix. The prefix
    # embeds a server-generated random public id, so at most one active token
    # normally matches; picking exactly one keeps the bcrypt work constant.
    result = await db.execute(
        select(PersonalAccessToken.id, PersonalAccessToken.token_hash)
        .where(PersonalAccessToken.token_prefix == prefix)
        .order_by(PersonalAccessToken.created_at)
        .limit(1)
    )
    row = result.first()

    # Exactly ONE bcrypt verify per call, whether or not a candidate exists:
    # verify against the candidate's hash, or a fixed dummy hash on a prefix
    # miss. bcrypt.checkpw is constant-time for a given hash, so prefix-miss and
    # wrong-secret are indistinguishable by timing (external review R2 finding 3).
    hash_to_check = row.token_hash if row is not None else _DUMMY_HASH
    verified = verify_password(token, hash_to_check)
    if row is None or not verified:
        return None
    matched_id = row.id

    # TOCTOU close: re-read the matched row's LIVE columns AFTER the (slow) bcrypt
    # so a revocation/expiry that committed during the bcrypt window is honoured.
    # Select the raw columns (not the ORM entity) so SQLAlchemy's identity map
    # cannot return a stale already-loaded object (external review R2 finding 2).
    # ``now`` is sampled here, after the read, for the same reason.
    fresh = (
        await db.execute(
            select(
                PersonalAccessToken.user_id,
                PersonalAccessToken.revoked_at,
                PersonalAccessToken.expires_at,
            ).where(PersonalAccessToken.id == matched_id)
        )
    ).first()
    if fresh is None:
        return None
    now = datetime.now(timezone.utc)
    if fresh.revoked_at is not None:
        return None
    if fresh.expires_at is not None and fresh.expires_at <= now:
        return None

    user = (
        await db.execute(
            select(LocalUser).where(LocalUser.id == fresh.user_id)
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None

    if email is not None:
        # The email is the client-supplied username, NOT a secret, so a plain
        # equality check is correct here — and constant-time comparison via
        # hmac.compare_digest would RAISE on a non-ASCII (internationalized)
        # email, breaking auth / turning it into a 503 (external review R3
        # finding 2). Compare canonicalised emails directly.
        if (user.email or "").strip().lower() != (email or "").strip().lower():
            return None

    return user


async def touch_last_used(db: AsyncSession, *, token_prefix: str, user_id) -> None:
    """Best-effort ``last_used_at`` update for the active PAT with *token_prefix*.

    Called by the auth backend AFTER validation succeeds, on the backend's own
    session, so a failed login never advances "last used" and a telemetry write
    failure can never reject a valid PAT. Swallows its own errors.
    """
    try:
        result = await db.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.token_prefix == token_prefix,
                PersonalAccessToken.user_id == user_id,
                PersonalAccessToken.revoked_at.is_(None),
            )
        )
        pat = result.scalar_one_or_none()
        if pat is not None:
            pat.last_used_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception:  # noqa: BLE001 — telemetry only, never fail/relect auth
        logger.warning("Failed to update PAT last_used_at", exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
