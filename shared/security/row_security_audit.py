"""Row-security rule audit + normalisation (Bug-6034).

Status: active. Last meaningful update: 2026-07-03.

Canonical, database-free normalisation of a single row-security rule's
attribute-mapping fields. It mirrors the create/update CRUD guards added by
Bug-5904 / Bug-5905
(``services/model-service/src/api/row_security.py``) and the snapshot-import
guard ``_validate_row_security_rule_on_import``
(``shared/model_snapshot/rehydrator.py``) — but as a pure function so it can
run inside an Alembic data migration against rows already persisted in a
tenant database.

Why this exists
---------------
Bug-5904 fixed a defect where claim/scope-sourced row-security rules
(``attribute_source`` of ``saml_claim`` / ``oidc_scope``) could be silently
inert: a blank or whitespace-padded ``attribute_claim_name`` never resolves a
subject in
``predicate_compiler._resolve_principal_attribute`` at query time, so the
intended restriction is not applied (fail-open — the query returns unfiltered
rows). The CRUD path now validates on create/update and the rehydrator
validates on import, but rules written BEFORE that fix may still sit inert in
tenant ``<slug>_meta`` schemas. The Bug-6034 backfill migration (0153) uses
this function to detect and correct those rows.

Normalisation rules (fail-closed for security)
-----------------------------------------------
1. A non-string claim name is cleared (defensive; the DB column is text so
   this only guards corrupt input).
2. A whitespace-padded claim name is trimmed so the exact-key claim lookup at
   query time succeeds.
3. An unknown / padded ``attribute_source`` is normalised to ``jwt_role`` and
   the rule is disabled — an unrecognised source would resolve to the
   principal's roles at runtime (``_resolve_principal_attribute`` falls through
   to ``principal.roles``), silently changing the rule's meaning.
4. A ``role_predicate`` rule whose source is claim/scope-based but whose claim
   name is blank after trimming is disabled — it can never match a principal,
   so leaving it enabled is a fail-open no-op.
5. A ``user_mapping`` rule that carries a non-default ``attribute_source`` or a
   stray ``attribute_claim_name`` has them reset to their defaults — that rule
   type always keys by ``user_identity`` and never consumes those fields.

A NULL / empty ``attribute_source`` is treated as ``jwt_role`` to match the
runtime resolver's ``source or "jwt_role"`` coalescing, so a legitimately
role-based rule with an unset source is never mistaken for an unknown source
and disabled.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Recognised attribute sources — the SINGLE canonical definition. The two
# sibling guards that must agree with this set import it directly rather than
# re-declaring it (Bug-6034 follow-up: valid-source consolidation):
#   * the CRUD schema's ``_ROW_SECURITY_ATTRIBUTE_SOURCES``
#     (``shared/schemas/domains/aggregates_security.py``), and
#   * the rehydrator's ``_ROW_SECURITY_VALID_SOURCES``
#     (``shared/model_snapshot/rehydrator.py``).
# Both are now aliases for this exact frozenset object, so the three guards can
# no longer drift — a new source (e.g. ``azure_group``) is added here once and
# every guard picks it up. The drift-guard test
# (``tests/unit/test_row_security_claim_backfill.py::
# test_valid_source_sets_identical_across_guards``) asserts object identity to
# lock the consolidation in place. This module is the lowest-level home (it
# imports only the stdlib), so consumers can import it without a cycle.
ROW_SECURITY_VALID_SOURCES: frozenset[str] = frozenset(
    {"jwt_role", "idp_group", "saml_claim", "oidc_scope"}
)

# Sources that require an explicit claim name to resolve a subject at runtime.
ROW_SECURITY_CLAIM_SOURCES: frozenset[str] = frozenset({"saml_claim", "oidc_scope"})

# The safe default source (also the DB column server_default).
DEFAULT_ATTRIBUTE_SOURCE = "jwt_role"


@dataclass(frozen=True)
class RowSecurityAuditResult:
    """Outcome of auditing one rule.

    ``changed`` is True iff any of ``attribute_source`` /
    ``attribute_claim_name`` / ``is_enabled`` differs from the input, i.e. the
    caller must persist an UPDATE. ``actions`` is a stable, machine-readable
    list of the corrections applied (useful for logging / assertions);
    ``warnings`` is the human-readable companion.
    """

    attribute_source: str
    attribute_claim_name: str | None
    is_enabled: bool
    changed: bool
    actions: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def audit_row_security_rule(
    *,
    attribute_source: object,
    attribute_claim_name: object,
    rule_type: object,
    is_enabled: object,
    rule_label: str = "?",
) -> RowSecurityAuditResult:
    """Normalise one row-security rule's attribute-mapping fields.

    Pure and idempotent: feeding the result back through this function yields
    ``changed == False`` and no further actions. Never re-enables a rule
    (fail-closed only). See module docstring for the full rule set.
    """
    orig_source = attribute_source
    orig_claim = attribute_claim_name
    orig_enabled = bool(is_enabled)

    source: object = attribute_source
    claim: object = attribute_claim_name
    enabled = orig_enabled

    actions: list[str] = []
    warnings: list[str] = []

    # 1. Non-string claim name — clear the junk (defensive against corrupt or
    #    hand-edited rows). Does not disable on its own; the blank-claim check
    #    below decides that for claim-sourced rules.
    if claim is not None and not isinstance(claim, str):
        warnings.append(
            f"rule {rule_label!r}: attribute_claim_name has non-string type "
            f"{type(claim).__name__}; cleared"
        )
        actions.append("cleared_non_string_claim")
        claim = None

    # 2. Trim a whitespace-padded claim name so the exact-key claim lookup in
    #    predicate_compiler._resolve_principal_attribute succeeds at runtime.
    if isinstance(claim, str):
        trimmed = claim.strip()
        if trimmed != claim:
            warnings.append(
                f"rule {rule_label!r}: attribute_claim_name was whitespace-"
                f"padded ({claim!r}); trimmed to {trimmed!r}"
            )
            actions.append("trimmed_claim_name")
            claim = trimmed

    # Coalesce a NULL / empty source to the default, matching the runtime
    # resolver's ``source or "jwt_role"`` semantics, before validating.
    effective_source = source if source else DEFAULT_ATTRIBUTE_SOURCE

    # 3. Unknown / padded source — normalise to the safe default AND disable.
    if effective_source not in ROW_SECURITY_VALID_SOURCES:
        warnings.append(
            f"rule {rule_label!r}: invalid attribute_source={effective_source!r}; "
            f"normalised to {DEFAULT_ATTRIBUTE_SOURCE!r} and disabled"
        )
        actions.append("normalized_invalid_source")
        if enabled:
            actions.append("disabled_invalid_source")
        source = DEFAULT_ATTRIBUTE_SOURCE
        enabled = False
        return _build_result(
            source, claim, enabled, orig_source, orig_claim, orig_enabled,
            actions, warnings,
        )

    # Source is recognised — persist the coalesced value (a NULL becomes the
    # explicit default).
    source = effective_source

    rt = rule_type
    if rt == "role_predicate":
        # 4. Claim/scope-sourced predicate with a blank claim name never
        #    matches — disable it (fail-closed).
        if effective_source in ROW_SECURITY_CLAIM_SOURCES and not (
            (claim or "") if isinstance(claim, str) else ""
        ).strip():
            warnings.append(
                f"rule {rule_label!r}: attribute_source={effective_source!r} but "
                f"attribute_claim_name is blank; disabled "
                f"(claim-sourced rule without a claim name never matches)"
            )
            if enabled:
                actions.append("disabled_blank_claim")
            enabled = False
    elif rt == "user_mapping":
        # 5. user_mapping keys by user_identity — attribute_source /
        #    attribute_claim_name are never consumed. Reset any stray values.
        if effective_source != DEFAULT_ATTRIBUTE_SOURCE:
            warnings.append(
                f"rule {rule_label!r}: user_mapping has "
                f"attribute_source={effective_source!r}; normalised to "
                f"{DEFAULT_ATTRIBUTE_SOURCE!r}"
            )
            actions.append("normalized_user_mapping_source")
            source = DEFAULT_ATTRIBUTE_SOURCE
        if claim is not None:
            warnings.append(
                f"rule {rule_label!r}: user_mapping has attribute_claim_name "
                f"set; cleared"
            )
            actions.append("cleared_user_mapping_claim")
            claim = None

    return _build_result(
        source, claim, enabled, orig_source, orig_claim, orig_enabled,
        actions, warnings,
    )


def _build_result(
    source: object,
    claim: object,
    enabled: bool,
    orig_source: object,
    orig_claim: object,
    orig_enabled: bool,
    actions: list[str],
    warnings: list[str],
) -> RowSecurityAuditResult:
    changed = (
        source != orig_source
        or claim != orig_claim
        or enabled != orig_enabled
    )
    return RowSecurityAuditResult(
        attribute_source=str(source),
        attribute_claim_name=claim if claim is None else str(claim),
        is_enabled=enabled,
        changed=changed,
        actions=tuple(actions),
        warnings=tuple(warnings),
    )


__all__ = [
    "ROW_SECURITY_VALID_SOURCES",
    "ROW_SECURITY_CLAIM_SOURCES",
    "DEFAULT_ATTRIBUTE_SOURCE",
    "RowSecurityAuditResult",
    "audit_row_security_rule",
]
