"""Audit event writer with severity-level gating.

Usage:
    from shared.audit.logger import audit

    await audit(
        db,
        action="model.deploy",
        severity="warn",
        actor_id=user.id,
        actor_email=user.email,
        target_type="model",
        target_id=model.id,
        target_name=model.display_name,
        detail={"version": version_number},
        ip_address=request_ip,
    )

Severity levels (ascending):
    critical — auth failures, security changes, destructive deletes
    warn     — deploy/undeploy, user changes, settings changes
    info     — all CRUD, query execution, connection changes

The tenant-level setting ``audit.log_level`` gates which events are written.
An event is written only if its severity >= the configured level. ``off``
suppresses all writes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import AuditEvent, TenantSetting

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {
    "off": -1,
    "critical": 3,
    "warn": 2,
    "info": 1,
}


class AuditWriteError(RuntimeError):
    """Raised by :func:`audit_required` when a required audit event cannot be
    persisted.

    F-022-02: protected mutations (persona/CLS/RLS/role/license/connection/
    webhook-secret changes and other security-relevant state) must not commit
    while their audit evidence is lost. Callers emit the event through
    :func:`audit_required` **before** ``db.commit()`` on the same session, so a
    write failure raised here rolls the whole transaction back — the mutation
    and its evidence commit atomically or neither does.

    A deliberately suppressed event (tenant ``audit.log_level`` gating) is NOT a
    failure and never raises: gating returns ``None`` just as it does for the
    fail-open :func:`audit` variant.
    """



async def _get_audit_level(db: AsyncSession) -> str:
    """Resolve the tenant ``audit.log_level`` setting.

    Bug-5946: the settings resolver (``shared.config.resolver``) stores scalar
    strings directly in ``TenantSetting.value_json`` (e.g. ``"off"``,
    ``"warn"``). The previous implementation only handled the legacy dict
    shape ``{"value": "..."}``, so a scalar setting was ignored and the writer
    always fell through to ``"info"``. Now handles both shapes.
    """
    from sqlalchemy import select
    result = await db.execute(
        select(TenantSetting.value_json).where(TenantSetting.key == "audit.log_level")
    )
    row = result.scalar_one_or_none()
    if row is None:
        return "info"
    # The resolver writes scalar strings; legacy code may have written dicts.
    if isinstance(row, str):
        return row if row in _SEVERITY_RANK else "info"
    if isinstance(row, dict):
        val = row.get("value", "info")
        return val if val in _SEVERITY_RANK else "info"
    return "info"


def _should_log(event_severity: str, configured_level: str) -> bool:
    if configured_level == "off":
        return False
    # F-022-10: an unknown/misspelled severity (e.g. "warning" instead of
    # "warn") must NOT be silently dropped. Treat it as the most severe level
    # so a call-site typo always produces a recorded event rather than a
    # vanished one — audit completeness fails closed (never drop).
    if event_severity not in _SEVERITY_RANK or event_severity == "off":
        logger.warning(
            "audit() called with unrecognised severity %r — recording it as "
            "critical so the event is never silently dropped. Fix the call site "
            "to use one of: critical, warn, info.",
            event_severity,
        )
        event_rank = _SEVERITY_RANK["critical"]
    else:
        event_rank = _SEVERITY_RANK[event_severity]
    threshold_rank = _SEVERITY_RANK.get(configured_level, 1)
    return event_rank >= threshold_rank


async def _write_audit_event(
    db: AsyncSession,
    *,
    action: str,
    severity: str,
    actor_id: Optional[uuid.UUID],
    actor_email: Optional[str],
    target_type: Optional[str],
    target_id: Optional[uuid.UUID],
    target_name: Optional[str],
    detail: Optional[dict[str, Any]],
    ip_address: Optional[str],
    force: bool = False,
) -> Optional[AuditEvent]:
    """Resolve gating and persist the event. Raises on any write failure.

    Returns ``None`` when the tenant ``audit.log_level`` gate suppresses the
    event (not a failure). The two public entry points wrap this: :func:`audit`
    swallows exceptions (fail-open, informational), :func:`audit_required`
    lets them propagate (fail-closed, protected mutations).

    ``force=True`` (Bug-8131) records the event UNCONDITIONALLY, bypassing the
    tenant ``audit.log_level`` gate — for evidence that must survive even when a
    tenant sets ``audit.log_level=off`` (e.g. a fail-closed webhook-callback
    refusal, whose durability is a security contract, not an operator
    preference). The level is still read so the deliberate override is explicit,
    never accidental.
    """
    level = await _get_audit_level(db)
    if not force and not _should_log(severity, level):
        return None

    event = AuditEvent(
        id=uuid.uuid4(),
        timestamp=datetime.now(timezone.utc),
        actor_id=actor_id,
        actor_email=actor_email,
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        severity=severity,
        detail=detail,
        ip_address=ip_address,
    )
    db.add(event)
    await db.flush()
    return event


async def audit(
    db: AsyncSession,
    *,
    action: str,
    severity: str,
    actor_id: Optional[uuid.UUID] = None,
    actor_email: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
    target_name: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    force: bool = False,
) -> Optional[AuditEvent]:
    """Fail-open audit write for informational events.

    A persistence failure is logged and swallowed (returns ``None``) so a
    non-critical event never breaks its caller. Do NOT use this for a protected
    security mutation whose evidence must survive — use :func:`audit_required`.

    ``force=True`` bypasses the tenant ``audit.log_level`` gate so the event is
    recorded even when the tenant set the level to ``off`` (Bug-8131 durable
    refusal evidence). Persistence failure is still fail-open here — use
    :func:`audit_required` when the write must be atomic with a mutation.
    """
    try:
        return await _write_audit_event(
            db,
            action=action,
            severity=severity,
            actor_id=actor_id,
            actor_email=actor_email,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            detail=detail,
            ip_address=ip_address,
            force=force,
        )
    except Exception:
        logger.exception("Failed to write audit event: action=%s", action)
        return None


async def audit_required(
    db: AsyncSession,
    *,
    action: str,
    severity: str,
    actor_id: Optional[uuid.UUID] = None,
    actor_email: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[uuid.UUID] = None,
    target_name: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> Optional[AuditEvent]:
    """Fail-closed audit write for protected mutations (F-022-02).

    Emit this on the SAME session as the mutation and BEFORE ``db.commit()``. A
    persistence failure raises :class:`AuditWriteError`, which propagates out of
    the request handler (rolling the mutation back) instead of silently losing
    the only durable evidence for a successful state change.

    Returns ``None`` only when tenant gating deliberately suppresses the event
    AND this is not a required write. Required events always pass ``force=True``
    so ``audit.log_level=off`` cannot drop security evidence (F-022-07).
    """
    try:
        return await _write_audit_event(
            db,
            action=action,
            severity=severity,
            actor_id=actor_id,
            actor_email=actor_email,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            detail=detail,
            ip_address=ip_address,
            force=True,
        )
    except Exception as exc:
        logger.exception(
            "Required audit event could not be persisted; failing the mutation "
            "closed: action=%s",
            action,
        )
        raise AuditWriteError(
            f"Required audit event {action!r} could not be persisted"
        ) from exc
