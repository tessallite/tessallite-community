"""SAML assertion replay ledger (F-021-03 / Bug-7993).

A signed, still-valid SAML assertion can be captured and replayed against the
ACS to forge a second session for the victim (RelayState single-use does not
help: an attacker just starts a fresh login flow to mint a new state and pairs
it with the captured assertion). This module records each processed assertion's
unique ID in a durable, multi-replica-safe ledger and rejects any second use.

The record-or-reject is atomic: an ``INSERT ... ON CONFLICT DO NOTHING`` that
reports whether the row was newly inserted. Only the first ACS POST carrying a
given assertion ID inserts; every replay hits the conflict and is refused.
Rows are reaped past ``not_on_or_after`` (the assertion's own validity horizon).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.db.models import SamlAssertionReplay
from shared.db.session import get_system_db

logger = logging.getLogger(__name__)


async def record_assertion_or_reject(
    *,
    assertion_id: str,
    tenant_id: str,
    not_on_or_after: datetime | None,
) -> bool:
    """Atomically record a SAML assertion ID. Return True if this is the FIRST
    use (accept), False if the assertion has already been seen (replay — reject).

    ``not_on_or_after`` bounds ledger reaping. When the IdP did not supply one
    (or the library could not extract it) we fall back to a conservative window
    so the row still expires — a missing horizon must not make the ledger grow
    forever, and the assertion's own signature/time validity is enforced
    separately by the SAML library.

    Fail-closed: any DB error is treated as "cannot prove first use" and the
    caller must reject the login. This function returns False on such errors.
    """
    if not assertion_id:
        # An assertion with no ID cannot be tracked for replay; fail closed.
        logger.warning(
            "SAML assertion has no ID; cannot enforce replay ledger — rejecting"
        )
        return False

    now = datetime.now(timezone.utc)
    if not_on_or_after is None:
        # Conservative bound: SAML assertions are short-lived; 15 minutes is
        # generous and keeps the ledger from growing without an IdP horizon.
        from datetime import timedelta
        expiry = now + timedelta(minutes=15)
    else:
        expiry = not_on_or_after

    try:
        async for db in get_system_db():
            # Opportunistically reap expired rows so the ledger stays bounded
            # even without a dedicated sweep.
            await db.execute(
                delete(SamlAssertionReplay).where(
                    SamlAssertionReplay.not_on_or_after < now
                )
            )
            stmt = (
                pg_insert(SamlAssertionReplay)
                .values(
                    assertion_id=assertion_id,
                    tenant_id=tenant_id,
                    not_on_or_after=expiry,
                )
                .on_conflict_do_nothing(index_elements=["assertion_id"])
                .returning(SamlAssertionReplay.assertion_id)
            )
            result = await db.execute(stmt)
            inserted = result.first() is not None
            await db.commit()
            return inserted
    except Exception:
        logger.exception(
            "SAML assertion replay-ledger write failed — rejecting login "
            "(fail closed)"
        )
        return False
