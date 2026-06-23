"""ML26 auth/RBAC + audit/webhook schema changes.

Three independent, idempotent, mode-aware changes (env.py sets search_path to
either ``tess_system`` or ``{slug}_meta`` and each block self-selects via a
table-existence check, so one revision serves both branches):

- F-022-06: ``webhook_deliveries.next_attempt_at`` (tenant) — a pending
  delivery records its next backoff time so the scheduler drain job can retry
  it asynchronously instead of sleeping inside a request.
- F-021-11/F-021-04: ``user_access_bindings`` unique key drops ``role`` so a
  user has at most one binding per (project, model) scope. Pre-existing
  contradictory rows (same scope, two roles) are de-duplicated first, keeping
  the strongest role (admin < modeler < viewer by privilege).
- F-021-05: ``tess_system.sso_states`` (system) — durable, TTL-bounded SSO
  flow state so the login and callback legs survive landing on different
  replicas.

Idempotent: guarded on the live schema.

Revision ID: 0142
Revises: 0141
Create Date: 2026-06-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None

_WEBHOOK_DELIVERIES = "webhook_deliveries"
_NEXT_ATTEMPT_COL = "next_attempt_at"
_NEXT_ATTEMPT_IX = "ix_webhook_deliveries_next_attempt_at"

_BINDINGS = "user_access_bindings"
_OLD_UQ = "user_access_bindings_user_identity_role_project_id_model_id_key"
_NEW_UQ = "uq_access_binding_scope"

_SSO_STATES = "sso_states"


def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_column(table: str, column: str) -> bool:
    try:
        return any(c["name"] == column for c in _insp().get_columns(table))
    except sa.exc.NoSuchTableError:
        return False


def _constraint_names(table: str) -> set[str]:
    insp = _insp()
    names: set[str] = set()
    try:
        for uc in insp.get_unique_constraints(table):
            if uc.get("name"):
                names.add(uc["name"])
    except sa.exc.NoSuchTableError:
        pass
    return names


def upgrade() -> None:
    bind = op.get_bind()

    # --- F-022-06: webhook_deliveries.next_attempt_at (tenant schema) ---
    if _has_table(_WEBHOOK_DELIVERIES) and not _has_column(
        _WEBHOOK_DELIVERIES, _NEXT_ATTEMPT_COL
    ):
        op.add_column(
            _WEBHOOK_DELIVERIES,
            sa.Column(_NEXT_ATTEMPT_COL, sa.TIMESTAMP(timezone=True), nullable=True),
        )
        op.create_index(
            _NEXT_ATTEMPT_IX, _WEBHOOK_DELIVERIES, [_NEXT_ATTEMPT_COL]
        )

    # --- F-021-11/F-021-04: user_access_bindings unique key drops role ---
    if _has_table(_BINDINGS):
        # De-duplicate any contradictory bindings (same user/project/model,
        # multiple roles) before the new unique key is added. Keep the
        # strongest role per scope (admin=0 < modeler=1 < viewer=2).
        bind.execute(sa.text(
            """
            DELETE FROM user_access_bindings b
            USING (
                SELECT user_identity, project_id, model_id,
                       (ARRAY_AGG(id ORDER BY
                           CASE role WHEN 'admin' THEN 0
                                     WHEN 'modeler' THEN 1
                                     WHEN 'viewer' THEN 2
                                     ELSE 3 END,
                           created_at))[1] AS keep_id
                FROM user_access_bindings
                GROUP BY user_identity, project_id, model_id
                HAVING COUNT(*) > 1
            ) dup
            WHERE b.user_identity = dup.user_identity
              AND b.project_id IS NOT DISTINCT FROM dup.project_id
              AND b.model_id IS NOT DISTINCT FROM dup.model_id
              AND b.id <> dup.keep_id
            """
        ))
        existing = _constraint_names(_BINDINGS)
        # Drop the old role-inclusive unique key (name may vary by how the
        # table was created; drop whichever role-inclusive one is present).
        for name in existing:
            if name == _NEW_UQ:
                continue
            # The auto-named constraint includes "role" in its column list.
            try:
                cols = {
                    c
                    for uc in _insp().get_unique_constraints(_BINDINGS)
                    if uc.get("name") == name
                    for c in uc["column_names"]
                }
            except sa.exc.NoSuchTableError:
                cols = set()
            if "role" in cols:
                op.drop_constraint(name, _BINDINGS, type_="unique")
        if _NEW_UQ not in _constraint_names(_BINDINGS):
            op.create_unique_constraint(
                _NEW_UQ, _BINDINGS, ["user_identity", "project_id", "model_id"]
            )

    # --- F-021-05: tess_system.sso_states (system schema) ---
    if not _has_table(_SSO_STATES) and not _has_table(_BINDINGS):
        # Only the system branch lacks both a bindings table and an sso_states
        # table; create the SSO state store there.
        op.create_table(
            _SSO_STATES,
            sa.Column("state", sa.String(128), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("flow_type", sa.String(16), nullable=False),
            sa.Column("browser_nonce", sa.String(128), nullable=True),
            sa.Column("oidc_nonce", sa.String(128), nullable=True),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_sso_states_expires_at", _SSO_STATES, ["expires_at"]
        )


def downgrade() -> None:
    if _has_table(_WEBHOOK_DELIVERIES) and _has_column(
        _WEBHOOK_DELIVERIES, _NEXT_ATTEMPT_COL
    ):
        op.drop_index(_NEXT_ATTEMPT_IX, table_name=_WEBHOOK_DELIVERIES)
        op.drop_column(_WEBHOOK_DELIVERIES, _NEXT_ATTEMPT_COL)

    if _has_table(_BINDINGS) and _NEW_UQ in _constraint_names(_BINDINGS):
        op.drop_constraint(_NEW_UQ, _BINDINGS, type_="unique")
        op.create_unique_constraint(
            _OLD_UQ, _BINDINGS,
            ["user_identity", "role", "project_id", "model_id"],
        )

    if _has_table(_SSO_STATES):
        op.drop_index("ix_sso_states_expires_at", table_name=_SSO_STATES)
        op.drop_table(_SSO_STATES)
