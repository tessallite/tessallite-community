"""Row-security enforcement for the query router.

Thin re-export of :mod:`shared.security` — the compiler now lives there
because the model-service ``simulate-as-user`` preview needs the same
logic. See ``work/phase-5-row-security-and-live-polish-action-plan.md``.
"""
from shared.security import (
    CompiledPredicate,
    Principal,
    RowSecurityCompileError,
    RowSecurityDialectError,
    compile_row_security,
    has_active_rules,
    render_predicate_for_dialect,
)

__all__ = [
    "CompiledPredicate",
    "Principal",
    "RowSecurityCompileError",
    "RowSecurityDialectError",
    "compile_row_security",
    "has_active_rules",
    "render_predicate_for_dialect",
]
