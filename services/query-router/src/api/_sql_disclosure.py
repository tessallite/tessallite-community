"""Who may be shown the PHYSICAL rewritten SQL (Bug-6389).

The rewritten query text is not a neutral diagnostic. It contains:

* the physical schema / table / column names the semantic layer exists to keep
  behind logical names, and
* the COMPILED row-security predicate — i.e. the row-level policy logic itself,
  including the grants that apply to roles the caller does not hold.

The product already treats this as privileged on its diagnostic surface:
``GET /diagnostics/query-rewrites`` (which returns exactly these
``(raw, rewritten)`` pairs) is gated behind ``require_tenant_admin``. The route
trace embedded in an ordinary query response was not gated at all, so any
``query``-capability caller received the same content through a different door.

**R2 review, finding B1 — read the RIGHT vocabulary.** The first version of this
module gated on ``current_user.role`` / ``.roles`` via ``project_role_level``.
That is a category error with two opposite failures, both verified by execution:

* ``current_user.role`` is the JWT TENANT tier (``member`` / ``tenant_admin`` /
  ``model_technical`` / ``system_admin``). The project tier
  (``admin`` / ``modeler`` / ``viewer``) lives in ``UserAccessBinding.role`` and
  never appears on ``current_user``. So a real tenant admin scored BELOW viewer
  and was denied — while being able to read the identical content at
  ``/diagnostics/query-rewrites``. The feature was dead for its whole audience.
* ``CurrentEmbedUser`` surfaces the admin-authored ``EmbedRlsSubject.role`` — an
  arbitrary string — as ``role``/``roles``. An anonymous embedded-dashboard
  viewer whose RLS role happened to be named ``"modeler"`` or ``"admin"`` was
  therefore GRANTED the physical schema and the compiled row-security
  predicate: fail-open to the lowest-trust principal in the system.

``middleware`` documents ``roles`` as "ADDITIVE and used ONLY by the row-security
principal adapter". This module must not read it at all. Entitlement is decided
from the caller's KIND and their real PROJECT BINDING instead.

The tier is ``modeler`` rather than ``admin`` deliberately: a modeller authors
the physical bindings in the Model Builder, so the physical names are not a
disclosure to them, and gating at admin would remove a tool modellers need.

Redaction is structural, never a placeholder string: the field becomes ``None``
and a separate boolean says the omission was a policy decision rather than "no
SQL was produced". Consumers render their own localized wording.

**The EMBED redaction was REMOVED, 2026-08-11 (user decision, option C).** See
``docs/questions/questions_disclosure-by-entitlement-not-auth-method.md``. This
module used to carry a second control — ``redact_physical_details_for_embed``,
plus the ``is_embed_principal`` helper and an ``EMBED_REDACTED_REASON_TOKEN``
sentinel — that stripped every physical detail from ``/execute``, ``/explain``,
``/headless/query``, ``/plugin/execute`` and the leaf-mode drill response
whenever the caller arrived on an embed token. It gated on the token TYPE, so
the service answered the same question differently depending on which door the
caller used: two principals with identical rights got different responses, and
an embed token minted for a trusted internal dashboard was denied detail a
low-privilege tenant viewer received freely.

That is a deliberate SECURITY-POSTURE CHANGE, not cleanup: it reverses the R4
review finding that introduced the control. The accepted risk is recorded in the
decision document — an embed token is the one credential in this system that can
be handed to someone with no account in the tenant, and with the redaction gone
such a holder sees what every other authenticated caller sees. That is accepted
on the basis that embed is another BI channel whose holders are trusted
equivalently to JDBC/XMLA callers. **Revisit if embed tokens are ever issued to
genuinely untrusted or public audiences.**

What did NOT change, and must not: every ACCESS control keyed on the same flag.
``_simulate.py`` still rejects embed principals outright,
``shared/security/predicate_compiler.py`` still clears the ``embed`` sentinel so
a bare embed token carries no named RLS role and a role-governed model fails
closed, and ``may_disclose_physical_sql`` below still denies embed and service
principals the modeller-tier physical SQL. Removing any of those would GRANT
embed tokens a capability they have never had.

**Bug-8809 — the ERROR path is part of this boundary too.** Everything above
acts on a SUCCESS response, i.e. on an object that passes through a
``response_model``. An ``HTTPException`` passes through none of it, so a 4xx
body was a second, uncovered door out of the same room: the row-security
compiler interpolates real security-dimension column names, dimension paths,
rule ids and mapping-table ids into its exception messages, and the API layer
folded those messages verbatim into 403/422 details reachable by an embed
session.

The fix on that path is deliberately NOT a redaction pass. There is no
principal at the raise site inside ``routing/router.py`` (a
sensitive-component-guard engine), so no policy tier can be evaluated there;
and a regex that tries to strip "identifiers" out of free English prose is a
static pattern matcher asked to prove a dynamic property — the exact shape
CLAUDE.md tells us not to iterate on. Instead the error payloads are
CONSTRUCTED safe for the lowest-trust caller: a stable machine ``error_code``
(plus a closed-vocabulary ``reason_code`` where one adds signal) and a generic,
identifier-free sentence. The diagnostic that used to travel in the body is
logged server-side, where the operator who needs it can read it and the caller
cannot. The helpers below are the single place those bodies are built, so a new
call site cannot reinvent an interpolating one.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from shared.auth.middleware import (
    CurrentEmbedUser,
    CurrentServiceUser,
    is_human_tenant_admin_or_system_admin,
)
from shared.auth.project_access import ensure_project_model_access
from shared.auth.roles import PROJECT_MODELER_ROLE

# Minimum PROJECT-BINDING role allowed to see physical rewritten SQL.
PHYSICAL_SQL_MIN_ROLE = PROJECT_MODELER_ROLE

logger = logging.getLogger(__name__)

# Bug-8809. Stable machine token for "a row-security rule on this model cannot
# be compiled". Clients branch on this, never on the sentence.
ROW_SECURITY_MISCONFIGURED_ERROR_TYPE = "row_security_misconfigured"


async def may_disclose_physical_sql(
    db: Any,
    current_user: Any,
    *,
    project_id: UUID | str | None,
    model_id: UUID | str | None,
) -> bool:
    """True iff *current_user* is entitled to see this model's physical SQL.

    Order matters, and every branch fails closed:

    1. No user at all -> no.
    2. An EMBED session -> never, regardless of its RLS role. An embed token is
       an anonymous, admin-issued, link-shareable credential; its ``role`` is
       free text authored for row-security matching and carries no entitlement.
    3. A SERVICE principal -> never. Service tokens are scope-authorized for a
       specific job; none of those jobs needs the rendered SQL echoed back, and
       a service token has no human accountable for the disclosure.
    4. A human tenant admin / canonical system admin -> yes. They can already
       read the same content at ``/diagnostics/query-rewrites``.
    5. Anyone else -> only with a project/model binding of ``modeler`` or above,
       resolved from the database by the same helper the write endpoints use, so
       this gate cannot drift from the product's own notion of "is a modeller".

    Note on step 5: ``ensure_project_model_access`` permits a project that has
    NO bindings at all (the bootstrap case), which this gate inherits
    deliberately — in that state every tenant member can already perform
    modeller actions on the project, including opening the Model Builder and
    reading the physical bindings directly, so withholding the rendered SQL
    would protect nothing while breaking the trace for real users.
    """
    if current_user is None:
        return False
    if isinstance(current_user, (CurrentEmbedUser, CurrentServiceUser)):
        return False
    if is_human_tenant_admin_or_system_admin(current_user):
        return True
    if project_id is None or model_id is None:
        # Cannot resolve a binding -> cannot prove entitlement.
        return False
    try:
        await ensure_project_model_access(
            db,
            current_user,
            project_id=project_id,
            model_id=model_id,
            min_role=PHYSICAL_SQL_MIN_ROLE,
        )
    except HTTPException:
        return False
    except Exception:
        # Any failure to PROVE entitlement withholds. A disclosure gate must
        # never open because a lookup errored.
        return False
    return True


async def redact_physical_sql(
    sql: Any,
    db: Any,
    current_user: Any,
    *,
    project_id: UUID | str | None,
    model_id: UUID | str | None,
) -> tuple[str | None, bool]:
    """Return ``(sql_or_None, was_redacted)`` for *current_user*.

    ``was_redacted`` is True ONLY when SQL existed and policy withheld it, so a
    consumer can distinguish "you are not allowed to see this" from "the route
    produced no SQL".
    """
    if sql is None:
        return None, False
    allowed = await may_disclose_physical_sql(
        db, current_user, project_id=project_id, model_id=model_id,
    )
    if allowed:
        return str(sql), False
    return None, True


def row_security_misconfigured_detail(exc: Exception, *, surface: str) -> dict[str, str]:
    """Build the 422 body for an uncompilable row-security rule (Bug-8809).

    ``RowSecurityCompileError`` messages name the artefacts that failed to
    compile — ``invalid dimension path in row-security rule: 'customer.region'``,
    ``row-security mapping table UUID(...) not found``, ``unknown rule_type on
    rule <id>``. Every row-returning surface used to interpolate that message
    into the caller-visible detail, so an embed session (an anonymous,
    link-shareable credential that carries an RLS subject and therefore reaches
    the compiler on every query) learned dimension paths, rule ids and mapping
    table ids from a malformed rule.

    The caller gets the FACT and the remedy; the operator gets the specifics in
    the service log. ``error_type`` is the stable token consumers already branch
    on, so this is not a contract change for them.
    """
    logger.warning(
        "row-security rule failed to compile on %s: %s: %s",
        surface, type(exc).__name__, exc,
    )
    return {
        "message": (
            "A row-level security rule on this model is misconfigured and "
            "could not be compiled. The query was blocked (fail closed); ask "
            "a modeler to fix the rule's predicate."
        ),
        "error_type": ROW_SECURITY_MISCONFIGURED_ERROR_TYPE,
    }
