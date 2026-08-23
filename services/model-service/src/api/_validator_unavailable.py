"""Bug-8162: the one way this service says "the validator could not answer".

Both authoring paths that check user-written SQL against the query-router's
``/validate`` — ``scratchpad_measures._validate_expression_against_model`` and
``pockets._validate_via_router`` — must fail CLOSED when the router cannot
answer at all, and must say so in a way the caller can tell apart from a real
rejection.

Why the split matters. Failing open costs correctness silently and
permanently: an unvalidated expression is persisted, the outage passes, and it
renders as an all-NULL column — a wrong answer presented as a real one.
Failing closed costs availability briefly and visibly: the author retries.
Authoring is not a serving path. Decided 2026-08-11,
``work/porting-remediation-lane-plan.md`` D-7.

Why 503 and not 400. A 4xx asserts the CALLER's input was faulty. Using one
for an outage tells an author their CORRECT SQL is wrong — its own defect, and
the likeliest careless implementation of a fail-closed decision. 503 is the
standard "temporarily unable to handle the request" signal, retryable by
definition, which is exactly the instruction the caller needs.

The prose here is for API and log consumers. The text a USER sees is chosen by
the frontend from the 503 status and lives in
``frontend/src/i18n/en/`` — never hard-coded server-side.
"""
from __future__ import annotations

from fastapi import HTTPException

# Stable machine-readable marker, so a client can tell "unreachable" from
# "rejected" without parsing prose. The 503 status carries the same
# distinction and is what the frontend keys on.
VALIDATOR_UNAVAILABLE_CODE = "validator_unavailable"


def validator_unavailable(subject: str, reason: object) -> HTTPException:
    """Build the 503 that means "unknown — retry", never "you are wrong".

    ``subject`` names what was not saved/validated, in the caller's own words
    (e.g. ``"scratchpad measure"``). ``reason`` is the diagnostic cause.
    """
    return HTTPException(
        status_code=503,
        detail=(
            f"{VALIDATOR_UNAVAILABLE_CODE}: the query validator could not be "
            f"reached, so this {subject} was not validated and was NOT saved. "
            f"The SQL has not been rejected — retry shortly. ({reason})"
        ),
    )
