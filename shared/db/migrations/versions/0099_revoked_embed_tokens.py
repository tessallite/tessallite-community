"""revoked_embed_tokens — superseded no-op (see system migration 0128).

This revision originally tried to create ``tess_system.revoked_embed_tokens``
but sat on the TENANT branch with a body gated on ``MIGRATE_MODE=system`` —
a combination no documented migration path ever runs, so the table was never
created anywhere (Bug-1033). The table is a system-schema table and is now
created by revision 0128 on the SYSTEM branch (0016 -> 0128).

Kept as a pure no-op so the linear tenant revision chain
(0098 -> 0099 -> 0100) stays intact for databases that already recorded it.
"""
revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op: superseded by system-branch revision 0128 (Bug-1033).
    return


def downgrade() -> None:
    return
