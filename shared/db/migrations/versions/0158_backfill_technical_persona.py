"""Backfill missing ``slug='technical'`` personas for models created
between migration 0042 (which seeded them for existing models) and the
2026-07-07 fix. Models created in that window had no technical persona
row, so their gateway ``<slug>_technical`` catalog stayed inert.

This is a DATA-ONLY migration — no schema changes. It mirrors the 0042
seed logic (insert a technical persona per model that lacks one) and
the 0124 audience gate (set ``audience_roles`` to ``["model_technical"]``
on hidden-columns personas). Both steps are idempotent: the INSERT uses
``WHERE NOT EXISTS`` and the UPDATE's WHERE clause already excludes rows
that already carry the audience gate.

Bug-6630 / Bug-6138 backfill.

Revision ID: 0158
Revises: 0157
Create Date: 2026-07-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0158"
down_revision = "0157"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Tenant-only tables; no-op on the tess_system DB.
    if "personas" not in table_names or "models" not in table_names:
        return

    # Step 1 — seed a technical persona for every model that lacks one.
    # Mirrors the 0042 INSERT exactly (same columns, same defaults).
    bind.execute(
        sa.text(
            """
            INSERT INTO personas (
                id, model_id, name, slug, description,
                included_measure_ids, included_dimension_ids,
                included_hierarchy_ids, audience_roles, default_filters,
                bypass_row_security, includes_hidden_columns,
                created_at, updated_at
            )
            SELECT
                gen_random_uuid(), m.id, 'Technical', 'technical',
                'Auto-seeded technical view — shows every column '
                'including those marked hidden on the business view.',
                '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                '["model_technical"]'::jsonb, '{}'::jsonb,
                false, true, now(), now()
            FROM models m
            WHERE NOT EXISTS (
                SELECT 1 FROM personas p
                WHERE p.model_id = m.id AND p.slug = 'technical'
            )
            """
        )
    )

    # Step 2 — apply the 0124 audience gate to any technical persona rows
    # that still carry an empty audience_roles (e.g. rows seeded by 0042
    # before 0124 ran, on tenants that somehow skipped 0124).
    bind.execute(
        sa.text(
            """
            UPDATE personas
            SET audience_roles = '["model_technical"]'::jsonb
            WHERE includes_hidden_columns = true
              AND slug = 'technical'
              AND audience_roles = '[]'::jsonb
            """
        )
    )


def downgrade() -> None:
    # Best-effort: remove only the auto-seeded rows that match the exact
    # seed signature (name + slug + hidden-columns + audience gate). Rows
    # modified by users are preserved.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "personas" not in table_names:
        return

    bind.execute(
        sa.text(
            """
            DELETE FROM personas
            WHERE slug = 'technical'
              AND name = 'Technical'
              AND includes_hidden_columns = true
              AND audience_roles = '["model_technical"]'::jsonb
              AND description LIKE 'Auto-seeded technical view%%'
            """
        )
    )
