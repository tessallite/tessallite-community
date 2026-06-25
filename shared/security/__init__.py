"""Row-security predicate compilation, shared across query-router and
model-service (the latter needs it for the simulate-as-user preview).

See ``work/phase-5-row-security-and-live-polish-action-plan.md``.
"""
from shared.security.predicate_compiler import (
    CompiledPredicate,
    Principal,
    RowSecurityCompileError,
    compile_row_security,
    has_active_rules,
)

__all__ = [
    "CompiledPredicate",
    "Principal",
    "RowSecurityCompileError",
    "compile_row_security",
    "has_active_rules",
]
