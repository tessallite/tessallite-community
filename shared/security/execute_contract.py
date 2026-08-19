"""The ``/execute`` row-security contract, in ONE place (Bug-8453).

``ExecuteResponse.security_rules_applied`` (added for Bug-8449) is what lets a
caller tell three states apart that were previously one:

* the query ran and matched nothing            -> genuinely no data
* a row-security rule NARROWED the result      -> the rows shown are correct
                                                  but scoped to the caller
* row security DENIED every row (``__deny_all__``) -> the caller is not
                                                  permitted to see anything

There is no single shared ``/execute`` client — six modules each hand-roll their
own ``httpx.post`` — and that is precisely why the Bug-8449 fix reached only one
of them. Extracting the HTTP client is a larger refactor; extracting the
CLASSIFICATION is what stops this defect class recurring, because a new consumer
can no longer invent its own (wrong) notion of "denied".

Read the field through :func:`security_rules_from_execute_response` and decide
with :func:`row_security_denied_all` / :func:`execute_response_denied_all`.
Never string-match the router's prose ``reason``.
"""
from __future__ import annotations

from typing import Any, Iterable

# The sentinel rule id the router reports when the F-007-01 fail-closed coverage
# gate denied every row (the model is role-governed and the principal matched no
# rule). Mirrors ``shared.security.predicate_compiler._deny_all_predicate`` and
# the query-router's execute-contract ``DENY_ALL_RULE_ID``.
ROW_SECURITY_DENY_ALL_RULE_ID = "__deny_all__"


class RowSecurityDeniedError(RuntimeError):
    """Raised by an ``/execute`` client when the router denied EVERY row.

    Lives here rather than in each caller so a service that adds a new
    ``/execute`` client inherits one definition instead of inventing its own —
    the "one place" doctrine this module exists for.

    Deliberately a ``RuntimeError`` and NOT a ``ValueError``: the existing
    clients already raise ``ValueError`` for an HTTP-error response and their
    callers catch broad ``Exception`` to report "your SQL/expression is
    broken". A governance denial must be catchable BEFORE those handlers so it
    is never misattributed to the user's query.
    """


def security_rules_from_execute_response(payload: Any) -> set[str]:
    """Return the applied row-security rule ids from an ``/execute`` payload.

    Accepts a parsed JSON dict or any object exposing the attribute, and is
    defensive by design: this datum is DIAGNOSTIC on the happy path but drives a
    fail-closed branch on the denial path, so a malformed value must degrade to
    "nothing reported" rather than raise inside a caller's result handling.
    """
    if payload is None:
        return set()
    if isinstance(payload, dict):
        rules = payload.get("security_rules_applied")
    else:
        rules = getattr(payload, "security_rules_applied", None)
    if not isinstance(rules, (list, tuple, set, frozenset)):
        return set()
    return {str(r) for r in rules if r}


def row_security_denied_all(rules: Iterable[str] | None) -> bool:
    """True when the applied rule ids carry the deny-all sentinel.

    Bug-8449 (Codex gate finding 3): callers must branch on THIS, never on
    "the value came back None / the row set is empty". A deny-all rewrites the
    query to ``... WHERE 0 = 1``, and over an empty scan ``COUNT(*)`` and any
    ``COALESCE``-wrapped expression return a bare ``0`` — an authoritative-looking
    number that is not a measurement at all.
    """
    if not rules:
        return False
    return ROW_SECURITY_DENY_ALL_RULE_ID in {str(r) for r in rules}


def execute_response_denied_all(payload: Any) -> bool:
    """One-call form of the two functions above, for the common case."""
    return row_security_denied_all(security_rules_from_execute_response(payload))


def row_security_narrowed(rules: Iterable[str] | None) -> bool:
    """True when row security applied but did NOT deny everything.

    The rows the caller received are correct, just scoped to them. Surfacing
    this is a transparency affordance, not a fail-closed branch.
    """
    if not rules:
        return False
    ids = {str(r) for r in rules}
    return bool(ids) and ROW_SECURITY_DENY_ALL_RULE_ID not in ids
