"""Row-security predicate compilation, shared across query-router and
model-service (the latter needs it for the simulate-as-user preview).

See ``work/phase-5-row-security-and-live-polish-action-plan.md``.
"""
from shared.security.execute_contract import (
    ROW_SECURITY_DENY_ALL_RULE_ID,
    RowSecurityDeniedError,
    execute_response_denied_all,
    row_security_denied_all,
    row_security_narrowed,
    security_rules_from_execute_response,
)
from shared.security.predicate_compiler import (
    CompiledPredicate,
    Principal,
    RowSecurityCompileError,
    RowSecurityDialectError,
    compile_row_security,
    has_active_rules,
    render_predicate_for_dialect,
)

# NOTE: the Bug-6034 audit/backfill normaliser lives in
# ``shared.security.row_security_audit`` and is imported from that submodule
# directly by its consumers (migration 0153 + its unit tests). It is
# deliberately NOT re-exported here: package-level re-exports with no
# consumers are dead surface, and this package's public API is the
# predicate-compilation set below.

__all__ = [
    "CompiledPredicate",
    "Principal",
    "RowSecurityCompileError",
    "ROW_SECURITY_DENY_ALL_RULE_ID",
    "RowSecurityDeniedError",
    "RowSecurityDialectError",
    "execute_response_denied_all",
    "row_security_denied_all",
    "row_security_narrowed",
    "security_rules_from_execute_response",
    "compile_row_security",
    "has_active_rules",
    "render_predicate_for_dialect",
]
