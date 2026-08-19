"""Bug-8407 + Bug-8408/Bug-8357 — drop the write-only DLQ URL hash, and
re-scrub legacy ``last_error`` with the escaping-aware matcher.

Two independent things, in one migration because they touch one table and
must both run exactly once per tenant schema.

1. **Drop ``agent_webhook_dlq.target_url_hash`` (Bug-8407).** Migration 0184
   added it as "a one-way fingerprint of the full URL for dedup/correlation".
   It started as an unsalted SHA-256 — offline-brute-forceable, measured at
   42,001 candidate URLs in 0.07s — and Bug-8350 R2 MED-1 hardened it to
   bcrypt. That closed the brute-force half and left the other half standing:
   NOTHING in the codebase ever read the column, and salted bcrypt cannot
   correlate two rows anyway, so it could not serve the purpose it was
   documented for. A persisted per-row derivative of a secret-bearing URL with
   no reader is pure liability in a stolen backup, and it cost a ~0.3s bcrypt
   call on every DLQ write. The right answer to "a hash of a secret with no
   consumer" is not a slower hash — it is not storing it.

2. **Re-scrub ``last_error`` (Bug-8408 residual, via Bug-8357).** 0184 already
   scrubbed legacy ``last_error`` values, but with the OLD matcher, which only
   recognised a literal unescaped ``://``. A row whose ``last_error`` carried
   a JSON-escaped (``https:\\/\\/``), percent-encoded (``https%3A%2F%2F``) or
   entity-encoded rendering of the destination URL survived that pass
   completely. Deployments that already ran 0184 cannot re-run it, so the
   corrected sweep has to be its own migration. The plaintext ``target_url``
   is gone by then, so this pass is the generic scheme-anchored sweep only —
   the same one ``shared.webhooks.redact.scrub_url_from_text`` applies with no
   ``url`` argument. A receiver echo of ONLY a bare credential-bearing path,
   with no scheme, is not recoverable here and is covered at read time
   instead, where the endpoint's current URL is known
   (``agent-service/src/api/webhooks.list_dlq``).

The pattern is duplicated from ``shared.webhooks.redact`` rather than
imported, for the same self-containment reason 0184 gave: a migration must
keep doing what it did on the day it was written, even if the shared helper is
later changed again.

Tenant-schema guarded (skip when the schema has no ``agent_webhook_dlq``
table), idempotent, and reversible in SHAPE only — the dropped hash cannot be
recomputed (the plaintext URL it was derived from was already destroyed by
0184) and redacted text cannot be un-redacted.

Revision ID: 0188
Revises: 0187
Create Date: 2026-08-03
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0188"
down_revision = "0187"
branch_labels = None
depends_on = None

_TABLE = "agent_webhook_dlq"
_INDEX = "ix_agent_webhook_dlq_target_url_hash"
_REDACTED = "<redacted-url>"

# Kept in lockstep with shared.webhooks.redact at the time of writing
# (Bug-8357). See the module docstring for why it is copied, not imported.
_COLON = r"(?::|%3A|&#58;|&#x3A;|\\u003A)"
_SLASH = r"(?:/|\\/|%2F|&#47;|&#x2F;|\\u002F)"
_URL_PATTERN = re.compile(rf"https?{_COLON}{_SLASH}{_SLASH}\S+", re.IGNORECASE)


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(
        c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table)
    )


def _has_index(table: str, index_name: str) -> bool:
    return any(
        ix["name"] == index_name
        for ix in sa.inspect(op.get_bind()).get_indexes(table)
    )


def _rescrub_last_error() -> None:
    """Rewrite only the rows the corrected matcher actually changes."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f'SELECT id, last_error FROM "{_TABLE}" WHERE last_error IS NOT NULL'
        )
    ).fetchall()
    for row_id, last_error in rows:
        scrubbed = _URL_PATTERN.sub(_REDACTED, last_error)
        if scrubbed != last_error:
            bind.execute(
                sa.text(f'UPDATE "{_TABLE}" SET last_error = :err WHERE id = :id'),
                {"err": scrubbed, "id": row_id},
            )


def upgrade() -> None:
    if not _table_exists(_TABLE):
        return

    _rescrub_last_error()

    # Index dropped independently of the column so a re-run after a partial
    # failure between the two still finishes the job (the same reasoning 0184
    # applied to its create order, in reverse).
    if _has_index(_TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_column(_TABLE, "target_url_hash"):
        op.drop_column(_TABLE, "target_url_hash")


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    # Shape only. The hash cannot be recomputed: 0184 destroyed the plaintext
    # target_url it was derived from, and nothing read the column anyway.
    if not _has_column(_TABLE, "target_url_hash"):
        op.add_column(
            _TABLE, sa.Column("target_url_hash", sa.String(length=64), nullable=True)
        )
    if not _has_index(_TABLE, _INDEX):
        op.create_index(_INDEX, _TABLE, ["target_url_hash"])
