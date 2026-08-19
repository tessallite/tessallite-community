"""Add a DB-level CHECK on project_agent_configs.judge_mode (v5A#51).

The judge_mode domain is two values — ``sync`` (validated-first, the default:
the verdict resolves before the answer is exposed) or ``async`` (answer shown,
then validated). It is enforced application-side by the agent config API
(``agent_config.py``, pattern ``^(async|sync)$``), but the column is a
producer/consumer boundary for the snapshot serialiser, the rehydrator and
``guardrails/block._should_block``. This migration adds the matching fail-closed
CHECK so no out-of-band writer (import/rehydrate, a script, a future endpoint)
can persist a value those consumers cannot interpret.

Tenant-schema guarded (skip when the schema has no ``project_agent_configs``
table), idempotent (skip when the constraint already exists), and reversible.
Any pre-existing out-of-domain value is coerced to ``sync`` — the safest,
validated-first default — BEFORE the constraint is added so an ADD CONSTRAINT
can never fail on legacy data. The API layer never wrote such a value, but the
coercion keeps the migration safe.

This is a TENANT-chain migration: it revises 0204 (the tenant head), NOT the
system head (0207). ``alembic heads`` stays at exactly two heads
(system 0207 + tenant 0208).

Revision ID: 0208
Revises: 0204
Create Date: 2026-08-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0208"
down_revision = "0204"
branch_labels = None
depends_on = None

_TABLE = "project_agent_configs"
_CONSTRAINT = "ck_project_agent_configs_judge_mode"


def _existing_check_constraints(
    inspector: sa.engine.reflection.Inspector, table: str
) -> set[str]:
    return {c["name"] for c in inspector.get_check_constraints(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _CONSTRAINT in _existing_check_constraints(inspector, _TABLE):
        return
    # Coerce any legacy out-of-domain value so ADD CONSTRAINT cannot fail.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET judge_mode = 'sync' "
            "WHERE judge_mode NOT IN ('sync', 'async')"
        )
    )
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        "judge_mode IN ('sync', 'async')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _CONSTRAINT not in _existing_check_constraints(inspector, _TABLE):
        return
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
