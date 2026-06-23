"""Unify connection_type: rewrite legacy 'jdbc' rows to 'hadoop_spark'.

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-14

Phase C of the external code-review remediation (docs/archive/archive_code-review-001__superseded.md
Finding 3). The frontend historically created connections with
``connection_type = 'jdbc'`` for Spark/Hive Thrift connections, but the
model-service test path treated ``jdbc`` as PostgreSQL via asyncpg and the
query-router routed ``jdbc`` to ``PostgresExecutor`` — a silent contract
mismatch that made Spark connections fail at runtime.

The UI path has always collected Spark/Hive Thrift fields for ``jdbc``, so
every existing row is known to represent a Hadoop/Spark connection. This
migration rewrites them in place to the canonical ``hadoop_spark`` value.

The write path (``shared.schemas.pydantic_models.ConnectionCreate`` field
validator) now rejects new ``jdbc`` writes. A read-path fallback remains in
the test-connection helper, the query-router source resolver, and the
frontend source/target forms so any request still carrying the legacy value
executes against the Spark branch instead of failing.

No downgrade: rolling back would require knowing which rows were originally
Spark vs PostgreSQL-over-JDBC, and no such metadata exists. Mark the
downgrade as a no-op and document the one-way nature here.
"""
from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE project_connections "
            "SET connection_type = 'hadoop_spark' "
            "WHERE connection_type = 'jdbc'"
        )
    )


def downgrade() -> None:
    # Intentionally empty — the original distinction between 'jdbc' (Spark)
    # and any hypothetical PostgreSQL-over-JDBC row cannot be reconstructed,
    # and no such row ever existed in production per the UI's create path.
    pass
