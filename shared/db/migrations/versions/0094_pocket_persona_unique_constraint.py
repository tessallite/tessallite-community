"""Replace pocket uniqueness constraint with persona-aware partial indexes.

The old constraint uq_pocket_model_fp_predhash over (model_id,
query_fingerprint, predicate_set_hash) prevented two personas from
materializing the same query shape. Split into two partial unique indexes:
one for global pockets (persona_id IS NULL) and one for persona-scoped
pockets (persona_id IS NOT NULL) that includes persona_id in the key.
"""
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_pocket_model_fp_predhash",
        "pocket_definitions",
        type_="unique",
    )
    op.execute(
        'CREATE UNIQUE INDEX uq_pocket_global '
        'ON pocket_definitions (model_id, query_fingerprint, predicate_set_hash) '
        'WHERE persona_id IS NULL AND retired_at IS NULL'
    )
    op.execute(
        'CREATE UNIQUE INDEX uq_pocket_persona '
        'ON pocket_definitions (model_id, query_fingerprint, predicate_set_hash, persona_id) '
        'WHERE persona_id IS NOT NULL AND retired_at IS NULL'
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS uq_pocket_persona')
    op.execute('DROP INDEX IF EXISTS uq_pocket_global')
    op.create_unique_constraint(
        "uq_pocket_model_fp_predhash",
        "pocket_definitions",
        ["model_id", "query_fingerprint", "predicate_set_hash"],
    )
