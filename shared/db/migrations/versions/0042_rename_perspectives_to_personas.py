"""Rename perspectives to personas, add slug + includes_hidden_columns,
seed a default ``technical`` persona per existing model.

Revision ID: 0042
Revises: 0041
Create Date: 2026-04-24

Driven by `Docs/persona-rename-and-virtual-catalog-open-questions.md`.
The entity is renamed globally (Q1=A, Q6=replace-all). Persona becomes
a virtual-catalog variant at the gateway (`<model.slug>_<persona.slug>`)
so a slug column is added. The old hardcoded `_technical` variant the
gateway emits today is replaced by a seeded persona row per model with
``slug='technical'`` and ``includes_hidden_columns=true``.

Schema changes (idempotent — each step checks current state so repeat
runs, partial downgrades, and dev DBs built without the prior 0038
revision are all handled).

- Rename table  `perspectives` -> `personas`.
- Rename indexes / constraints that name the old table.
- Rename column   `aggregate_definitions.perspective_id` -> `persona_id`
  and the matching FK / index. Same for `pocket_definitions`,
  `query_logs`, `query_miss_logs`.
- Rename the composite unique added in 0041 on `query_miss_logs`.
- Add `personas.slug VARCHAR(64) NOT NULL` (populated from the name) and
  a `(model_id, slug)` unique constraint.
- Add `personas.includes_hidden_columns BOOLEAN NOT NULL DEFAULT false`.
- Seed one persona per existing model with
  ``(slug='technical', name='Technical', includes_hidden_columns=true)``
  when no row for that (model_id, slug) pair yet exists.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op


revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


_PERSPECTIVE_ID_TABLES = (
    "aggregate_definitions",
    "pocket_definitions",
    "query_logs",
    "query_miss_logs",
)


def _exists(sql: str, **params) -> bool:
    result = op.get_bind().execute(sa.text(sql), params)
    return result.scalar() is not None


def _table_exists(table: str) -> bool:
    return _exists(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = :t",
        t=table,
    )


def _column_exists(table: str, column: str) -> bool:
    return _exists(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = :t AND column_name = :c",
        t=table, c=column,
    )


def _index_exists(name: str) -> bool:
    return _exists(
        "SELECT 1 FROM pg_indexes "
        "WHERE schemaname = current_schema() AND indexname = :n",
        n=name,
    )


def _constraint_exists(name: str) -> bool:
    return _exists(
        "SELECT 1 FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = current_schema() AND c.conname = :n",
        n=name,
    )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    return slug or "persona"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Rename the table
    # ------------------------------------------------------------------
    if _table_exists("perspectives") and not _table_exists("personas"):
        op.rename_table("perspectives", "personas")

    # Rename the primary-table index + uniqueness constraint
    if _index_exists("ix_perspectives_model_id") and not _index_exists("ix_personas_model_id"):
        op.execute("ALTER INDEX ix_perspectives_model_id RENAME TO ix_personas_model_id")
    if _constraint_exists("uq_perspective_model_name") and not _constraint_exists("uq_persona_model_name"):
        op.execute(
            "ALTER TABLE personas "
            "RENAME CONSTRAINT uq_perspective_model_name TO uq_persona_model_name"
        )

    # ------------------------------------------------------------------
    # 2. Rename perspective_id -> persona_id on every dependent table
    # ------------------------------------------------------------------
    for table in _PERSPECTIVE_ID_TABLES:
        if not _table_exists(table):
            continue
        if _column_exists(table, "perspective_id") and not _column_exists(table, "persona_id"):
            op.alter_column(table, "perspective_id", new_column_name="persona_id")

        old_fk = f"fk_{table}_perspective_id"
        new_fk = f"fk_{table}_persona_id"
        if _constraint_exists(old_fk) and not _constraint_exists(new_fk):
            op.execute(
                f"ALTER TABLE {table} RENAME CONSTRAINT {old_fk} TO {new_fk}"
            )

        old_ix = f"ix_{table}_perspective_id"
        new_ix = f"ix_{table}_persona_id"
        if _index_exists(old_ix) and not _index_exists(new_ix):
            op.execute(f"ALTER INDEX {old_ix} RENAME TO {new_ix}")

    # Rename the composite unique from 0041
    old_uq = "uq_query_miss_logs_model_fingerprint_perspective"
    new_uq = "uq_query_miss_logs_model_fingerprint_persona"
    if _constraint_exists(old_uq) and not _constraint_exists(new_uq):
        op.execute(
            f"ALTER TABLE query_miss_logs RENAME CONSTRAINT {old_uq} TO {new_uq}"
        )

    # ------------------------------------------------------------------
    # 3. Add slug + includes_hidden_columns on personas
    # ------------------------------------------------------------------
    if _table_exists("personas"):
        if not _column_exists("personas", "slug"):
            op.add_column(
                "personas",
                sa.Column("slug", sa.String(64), nullable=True),
            )
            # Backfill slug from name, guarding against collisions within a model.
            rows = op.get_bind().execute(
                sa.text("SELECT id, model_id, name FROM personas ORDER BY created_at")
            ).fetchall()
            seen: dict[tuple, set[str]] = {}
            for row in rows:
                base = _slugify(row.name)
                bucket = seen.setdefault(row.model_id, set())
                slug = base
                suffix = 2
                while slug in bucket:
                    slug = f"{base}_{suffix}"
                    suffix += 1
                bucket.add(slug)
                op.get_bind().execute(
                    sa.text("UPDATE personas SET slug = :s WHERE id = :i"),
                    {"s": slug, "i": row.id},
                )
            op.alter_column("personas", "slug", nullable=False)

        if not _constraint_exists("uq_persona_model_slug"):
            op.create_unique_constraint(
                "uq_persona_model_slug", "personas", ["model_id", "slug"]
            )

        if not _column_exists("personas", "includes_hidden_columns"):
            op.add_column(
                "personas",
                sa.Column(
                    "includes_hidden_columns",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                ),
            )

    # ------------------------------------------------------------------
    # 4. Seed a technical persona per existing model
    # ------------------------------------------------------------------
    if _table_exists("personas") and _table_exists("models"):
        op.get_bind().execute(
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
                    '[]'::jsonb, '{}'::jsonb,
                    false, true, now(), now()
                FROM models m
                WHERE NOT EXISTS (
                    SELECT 1 FROM personas p
                    WHERE p.model_id = m.id AND p.slug = 'technical'
                )
                """
            )
        )


def downgrade() -> None:
    # Best-effort reversal. The seeded technical persona is removed only
    # if its name+slug match the seed exactly, so user-edited rows are
    # preserved.
    if _table_exists("personas"):
        op.get_bind().execute(
            sa.text(
                "DELETE FROM personas "
                "WHERE slug = 'technical' AND name = 'Technical' "
                "AND includes_hidden_columns = true"
            )
        )
        if _column_exists("personas", "includes_hidden_columns"):
            op.drop_column("personas", "includes_hidden_columns")
        if _constraint_exists("uq_persona_model_slug"):
            op.drop_constraint("uq_persona_model_slug", "personas", type_="unique")
        if _column_exists("personas", "slug"):
            op.drop_column("personas", "slug")

    # Flip the composite unique back
    old_uq = "uq_query_miss_logs_model_fingerprint_perspective"
    new_uq = "uq_query_miss_logs_model_fingerprint_persona"
    if _constraint_exists(new_uq) and not _constraint_exists(old_uq):
        op.execute(
            f"ALTER TABLE query_miss_logs RENAME CONSTRAINT {new_uq} TO {old_uq}"
        )

    for table in _PERSPECTIVE_ID_TABLES:
        if not _table_exists(table):
            continue
        old_ix = f"ix_{table}_perspective_id"
        new_ix = f"ix_{table}_persona_id"
        if _index_exists(new_ix) and not _index_exists(old_ix):
            op.execute(f"ALTER INDEX {new_ix} RENAME TO {old_ix}")

        old_fk = f"fk_{table}_perspective_id"
        new_fk = f"fk_{table}_persona_id"
        if _constraint_exists(new_fk) and not _constraint_exists(old_fk):
            op.execute(
                f"ALTER TABLE {table} RENAME CONSTRAINT {new_fk} TO {old_fk}"
            )

        if _column_exists(table, "persona_id") and not _column_exists(table, "perspective_id"):
            op.alter_column(table, "persona_id", new_column_name="perspective_id")

    if _constraint_exists("uq_persona_model_name") and not _constraint_exists("uq_perspective_model_name"):
        op.execute(
            "ALTER TABLE personas "
            "RENAME CONSTRAINT uq_persona_model_name TO uq_perspective_model_name"
        )
    if _index_exists("ix_personas_model_id") and not _index_exists("ix_perspectives_model_id"):
        op.execute("ALTER INDEX ix_personas_model_id RENAME TO ix_perspectives_model_id")
    if _table_exists("personas") and not _table_exists("perspectives"):
        op.rename_table("personas", "perspectives")
