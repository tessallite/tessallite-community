"""F-021-03 / Bug-7993: SAML AuthnRequest binding + assertion replay ledger.

Two system-schema changes so SAML SP-initiated SSO becomes request-bound and
replay-resistant:

- ``tess_system.sso_states.request_id`` — the AuthnRequest ID minted when the
  login flow starts, supplied to python3-saml at ACS time as the expected
  ``request_id`` so the IdP's ``InResponseTo`` is validated (an assertion that
  was not produced for this login attempt is rejected).
- ``tess_system.saml_assertion_replay`` — a durable ledger keyed by the unique
  assertion ID. Each processed assertion is recorded atomically; a second POST
  carrying the same assertion ID is refused. ``not_on_or_after`` bounds reaping.

Both live only in the system branch (``sso_states`` already does). env.py sets
the search_path to either ``tess_system`` or ``{slug}_meta``; each block
self-selects on the presence of ``sso_states`` (system-only), so the tenant
branch is a no-op. Idempotent: guarded on the live schema.

Revision ID: 0174
Revises: 0173
Create Date: 2026-07-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0174"
down_revision = "0173"
branch_labels = None
depends_on = None

_SSO_STATES = "sso_states"
_REQUEST_ID_COL = "request_id"
_REPLAY = "saml_assertion_replay"
_REPLAY_EXPIRY_IX = "ix_saml_assertion_replay_not_on_or_after"


def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_column(table: str, column: str) -> bool:
    try:
        return any(c["name"] == column for c in _insp().get_columns(table))
    except sa.exc.NoSuchTableError:
        return False


def upgrade() -> None:
    # System branch only: sso_states exists there and nowhere else.
    if not _has_table(_SSO_STATES):
        return

    if not _has_column(_SSO_STATES, _REQUEST_ID_COL):
        op.add_column(
            _SSO_STATES,
            sa.Column(_REQUEST_ID_COL, sa.String(128), nullable=True),
        )

    if not _has_table(_REPLAY):
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
        )
        op.create_index(
            _REPLAY_EXPIRY_IX, _REPLAY, ["not_on_or_after"]
        )


def downgrade() -> None:
    if _has_table(_REPLAY):
        op.drop_index(_REPLAY_EXPIRY_IX, table_name=_REPLAY)
        op.drop_table(_REPLAY)

    if _has_table(_SSO_STATES) and _has_column(_SSO_STATES, _REQUEST_ID_COL):
        op.drop_column(_SSO_STATES, _REQUEST_ID_COL)
