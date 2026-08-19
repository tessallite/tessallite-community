"""Bug-6306 / Bug-6352 R2 — key embed-token revocations by (jti, tenant_id).

Revision ID: 0189
Revises: 0128
Create Date: 2026-08-03

``revoked_embed_tokens`` stored one row per ``jti``. Once the revocation CHECK
became tenant-scoped, that single row became a shared, last-writer-wins slot
that any tenant could claim:

* the owning tenant revokes its own leaked embed token -> row (jti, owner);
* a stranger calls ``DELETE /auth/embed-token/{jti}`` with the same jti -- which
  is plaintext in any embed JWT that has ever leaked into a page, a referrer or
  a log -- and the upsert rewrites the row to (jti, stranger);
* the owner's check now reads tenant_id="stranger" and returns NOT revoked.

The killed token is live again. That is a cross-tenant access-control bypass,
strictly worse than the cross-tenant denial of service the tenant-scoped check
was added to close. Reverting to a plain INSERT is not the answer either: it
restores the mirror attack, where a stranger pre-claims the jti and the owner's
own revoke then fails on the primary key forever.

Both attacks exist only because two tenants share one row. Give each tenant its
own row: the primary key becomes ``(jti, tenant_id)``, the upsert conflicts only
with the same tenant's own prior record, and no tenant can observe or overwrite
another's revocation.
"""
from alembic import op
import sqlalchemy as sa


revision = "0189"
down_revision = "0128"
branch_labels = None
depends_on = None

_SCHEMA = "tess_system"
_TABLE = "revoked_embed_tokens"


def _table_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name = :t"
            ),
            {"s": _SCHEMA, "t": _TABLE},
        ).scalar()
    )


def _pk_name(conn) -> str | None:
    return conn.execute(
        sa.text(
            "SELECT c.conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE c.contype = 'p' AND n.nspname = :s AND t.relname = :t"
        ),
        {"s": _SCHEMA, "t": _TABLE},
    ).scalar()


def _pk_columns(conn) -> list[str]:
    rows = conn.execute(
        sa.text(
            "SELECT a.attname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
            "WHERE c.contype = 'p' AND n.nspname = :s AND t.relname = :t "
            "ORDER BY k.ord"
        ),
        {"s": _SCHEMA, "t": _TABLE},
    ).scalars().all()
    return list(rows)


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        # 0128 is guarded the same way; nothing to migrate on a fresh system
        # schema that has not created the table yet.
        return
    if _pk_columns(conn) == ["jti", "tenant_id"]:
        return  # already migrated / created out of band

    name = _pk_name(conn)
    if name:
        op.drop_constraint(name, _TABLE, schema=_SCHEMA, type_="primary")
    op.create_primary_key(
        f"{_TABLE}_pkey", _TABLE, ["jti", "tenant_id"], schema=_SCHEMA,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    # Going back to a jti-only key cannot keep two tenants' rows for the same
    # jti. Drop the duplicates (keeping the most recent) rather than failing the
    # downgrade; a revocation record is short-lived by construction.
    # ``revoked_at`` is NULLABLE in 0128 (it has a server default, but the
    # column permits NULL). A bare ``a.revoked_at < b.revoked_at`` is NULL in
    # BOTH directions for a NULL/NULL pair, so neither row would be deleted and
    # the jti-only primary key would then fail to create. COALESCE plus the
    # ctid tiebreak makes the ordering total, so exactly one row survives per
    # jti whatever the data looks like.
    conn.execute(
        sa.text(
            f"DELETE FROM {_SCHEMA}.{_TABLE} a "
            f"USING {_SCHEMA}.{_TABLE} b "
            "WHERE a.jti = b.jti AND a.ctid <> b.ctid "
            "AND (COALESCE(a.revoked_at, 'epoch'::timestamptz), a.tenant_id, a.ctid) "
            "  < (COALESCE(b.revoked_at, 'epoch'::timestamptz), b.tenant_id, b.ctid)"
        )
    )
    name = _pk_name(conn)
    if name:
        op.drop_constraint(name, _TABLE, schema=_SCHEMA, type_="primary")
    op.create_primary_key(f"{_TABLE}_pkey", _TABLE, ["jti"], schema=_SCHEMA)
