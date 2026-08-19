"""Flip project_agent_configs.judge_mode server default to validated-first (F-023-29 / Bug-8148).

The conversational agent's judge validates every answer against a rubric. In
"async" mode the answer is shown to the user and validated afterwards, so a
user can read and act on an answer the judge later marks wrong. In
"sync" (validated-first) mode the verdict is resolved BEFORE the answer is
exposed, so an unvetted answer is never shown by default.

Per the F-023-29 decision (docs/questions/questions_f023-29-default-judge-mode.md)
the DEFAULT for new project agent configs becomes validated-first ("sync").
"async" remains an explicit, lower-assurance per-project override.

This migration only changes the column server default (new rows). It does NOT
rewrite existing rows: a project that explicitly chose "async" keeps it, and a
project already on the previous "async" default keeps its stored value. There is
no way to distinguish an existing row that explicitly chose "async" from one
that merely took the old default, so existing behaviour is left untouched and
operators flip individual projects through the settings surface.

Revision ID: 0178
Revises: 0177
Create Date: 2026-07-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0178"
down_revision = "0177"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "project_agent_configs" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("project_agent_configs")}
    if "judge_mode" not in columns:
        return
    op.alter_column(
        "project_agent_configs",
        "judge_mode",
        existing_type=sa.String(length=16),
        existing_nullable=False,
        server_default=sa.text("'sync'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "project_agent_configs" not in table_names:
        return
    columns = {col["name"] for col in inspector.get_columns("project_agent_configs")}
    if "judge_mode" not in columns:
        return
    op.alter_column(
        "project_agent_configs",
        "judge_mode",
        existing_type=sa.String(length=16),
        existing_nullable=False,
        server_default=sa.text("'async'"),
    )
