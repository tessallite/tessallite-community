"""Create tess_system.sso_states (+ PKCE code_verifier) and saml_assertion_replay
on the SYSTEM branch.

Fixes two defects — one delivery gap this lane exposed, one pre-existing CRITICAL:

- F-1 (Bug-8142 delivery): the OIDC PKCE ``code_verifier`` column — and, indeed,
  the entire ``sso_states`` table — must exist in ``tess_system`` for
  ``create_state`` (which writes through ``get_system_db`` -> search_path
  tess_system) to succeed. A tenant-branch column add never reaches it.

- F-2 (pre-existing CRITICAL): ``sso_states`` and ``saml_assertion_replay`` were
  originally created by 0142 / 0174, whose docstrings claim "system branch" but
  which are in fact chained on the TENANT branch (root 0002). This chain has two
  intentional heads and ``upgrade head`` is unsupported (see env.py): deployments
  run ``alembic upgrade system@head`` and ``tenant@head`` separately. ``system@head``
  (=0189) therefore creates NEITHER table, and a live ``tess_system`` schema
  (alembic_version=0189) has neither — every SSO login that reads or writes these
  tables 500s.

This revision re-parents the creation onto the SYSTEM branch (``down_revision =
"0189"``; ``branch_labels = None`` so it inherits the ``system`` label from root
0001 through the 0016->0128->0189 chain — exactly as 0128 re-parented
``revoked_embed_tokens`` when the same class of misplacement was found there). It
idempotently creates both tables in ``tess_system`` in their CURRENT ORM shape
(``sso_states`` INCLUDING ``code_verifier``), guarded on the live schema so
environments that already have them upgrade cleanly. Where ``sso_states`` already
exists it additionally backfills the ``request_id`` / ``code_verifier`` columns if
absent, so an out-of-band or partially-migrated table converges on the ORM shape.

The tenant-branch 0142/0174 creations are deliberately left untouched: on a tenant
schema those tables are unused (all SSO state is read/written in ``tess_system``),
and re-parenting them is out of this lane's scope. No merge revision is authored —
env.py forbids it, as it would collapse the two heads and break MIGRATE_MODE schema
isolation.

Revision ID: 0206
Revises: 0189
Create Date: 2026-08-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0206"
down_revision = "0189"
branch_labels = None
depends_on = None

_SCHEMA = "tess_system"
_SSO_STATES = "sso_states"
_REPLAY = "saml_assertion_replay"
_SSO_EXPIRY_IX = "ix_sso_states_expires_at"
_REPLAY_EXPIRY_IX = "ix_saml_assertion_replay_not_on_or_after"


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t"
            ),
            {"s": _SCHEMA, "t": table},
        ).scalar()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t AND column_name = :c"
            ),
            {"s": _SCHEMA, "t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, _SSO_STATES):
        op.create_table(
            _SSO_STATES,
            sa.Column("state", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("flow_type", sa.String(16), nullable=False),
            sa.Column("browser_nonce", sa.String(128), nullable=True),
            sa.Column("oidc_nonce", sa.String(128), nullable=True),
            sa.Column("request_id", sa.String(128), nullable=True),
            sa.Column("code_verifier", sa.String(128), nullable=True),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            schema=_SCHEMA,
        )
        op.create_index(
            _SSO_EXPIRY_IX, _SSO_STATES, ["expires_at"], schema=_SCHEMA,
        )
    else:
        # Table already present (out-of-band / partial migration): converge it on
        # the current ORM shape by backfilling the newer nullable columns.
        if not _has_column(conn, _SSO_STATES, "request_id"):
            op.add_column(
                _SSO_STATES,
                sa.Column("request_id", sa.String(128), nullable=True),
                schema=_SCHEMA,
            )
        if not _has_column(conn, _SSO_STATES, "code_verifier"):
            op.add_column(
                _SSO_STATES,
                sa.Column("code_verifier", sa.String(128), nullable=True),
                schema=_SCHEMA,
            )

    if not _has_table(conn, _REPLAY):
        op.create_table(
            _REPLAY,
            sa.Column("assertion_id", sa.String(255), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "not_on_or_after", sa.TIMESTAMP(timezone=True), nullable=False
            ),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            schema=_SCHEMA,
        )
        op.create_index(
            _REPLAY_EXPIRY_IX, _REPLAY, ["not_on_or_after"], schema=_SCHEMA,
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _REPLAY):
        op.drop_index(_REPLAY_EXPIRY_IX, table_name=_REPLAY, schema=_SCHEMA)
        op.drop_table(_REPLAY, schema=_SCHEMA)
    if _has_table(conn, _SSO_STATES):
        op.drop_index(_SSO_EXPIRY_IX, table_name=_SSO_STATES, schema=_SCHEMA)
        op.drop_table(_SSO_STATES, schema=_SCHEMA)
