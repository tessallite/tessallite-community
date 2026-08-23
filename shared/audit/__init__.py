"""Audit subsystem: severity-gated audit event writers.

``audit`` is the fail-open informational writer; ``audit_required`` is the
fail-closed writer for protected mutations (F-022-02) — it raises
``AuditWriteError`` when required evidence cannot be persisted so the mutation
rolls back instead of committing without a durable record.
"""
from shared.audit.logger import AuditWriteError, audit, audit_required

__all__ = ["audit", "audit_required", "AuditWriteError"]
