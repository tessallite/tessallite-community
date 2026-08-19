"""Backfill: audit + normalise pre-Bug-5904 claim/scope row-security rules.

Revision ID: 0153
Revises: 0152
Create Date: 2026-07-03

Bug-6034. Bug-5904 fixed a defect where a claim/scope-sourced row-security
rule (``attribute_source`` = ``saml_claim`` / ``oidc_scope``) could be
silently inert: a blank or whitespace-padded ``attribute_claim_name`` never
resolves a subject at query time, so the intended restriction is not applied
(fail-open). The CRUD guards (create/update) and the snapshot rehydrator now
reject / correct those shapes, but rules written BEFORE that fix may still sit
inert in tenant ``<slug>_meta`` schemas.

This data migration scans ``row_security_rules`` in the current tenant schema
(``search_path`` is set to ``<slug>_meta`` by env.py, so it runs once per
tenant schema) and applies the canonical normalisation from
``shared.security.row_security_audit.audit_row_security_rule``:

  * whitespace-padded claim names are trimmed;
  * genuinely inert claim/scope rules (blank claim name) are DISABLED;
  * unknown ``attribute_source`` values are normalised to ``jwt_role`` and
    disabled;
  * ``user_mapping`` rules have stray attribute-source / claim-name fields
    reset to their defaults.

Idempotent: on a fresh or already-corrected database every rule audits clean,
so no rows are updated. Every change is logged with the rule id and the
actions applied.

Downgrade is a no-op: disabling an inert rule is a fail-closed correction and
the prior (broken) state is not restored.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

from shared.security.row_security_audit import audit_row_security_rule

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.row_security_backfill")

_TABLE = "row_security_rules"


def _table_exists() -> bool:
    """True if row_security_rules is present in the search_path schema.

    The system (``tess_system``) database has no row_security_rules table, and
    a tenant schema paused before migration 0035 does not either, so guard
    before touching it — this migration is then a clean no-op there.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    try:
        insp.get_columns(_TABLE)
        return True
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    if not _table_exists():
        return
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, name, rule_type, attribute_source, "
            "attribute_claim_name, is_enabled FROM row_security_rules"
        )
    ).fetchall()

    corrected = 0
    for r in rows:
        m = r._mapping
        result = audit_row_security_rule(
            attribute_source=m["attribute_source"],
            attribute_claim_name=m["attribute_claim_name"],
            rule_type=m["rule_type"],
            is_enabled=m["is_enabled"],
            rule_label=str(m["name"] or m["id"]),
        )
        if not result.changed:
            continue
        conn.execute(
            sa.text(
                "UPDATE row_security_rules SET attribute_source = :src, "
                "attribute_claim_name = :claim, is_enabled = :enabled "
                "WHERE id = :id"
            ),
            {
                "src": result.attribute_source,
                "claim": result.attribute_claim_name,
                "enabled": result.is_enabled,
                "id": m["id"],
            },
        )
        corrected += 1
        for w in result.warnings:
            logger.warning("Bug-6034 backfill: %s", w)
        logger.info(
            "Bug-6034 backfill: rule %s normalised (actions=%s)",
            m["id"],
            ",".join(result.actions) or "none",
        )

    if corrected:
        logger.info(
            "Bug-6034 backfill: corrected %d row-security rule(s) in this schema",
            corrected,
        )


def downgrade() -> None:
    # One-way security normalisation. Disabling an inert rule is a fail-closed
    # correction; the prior (broken) state is intentionally not restored.
    pass
