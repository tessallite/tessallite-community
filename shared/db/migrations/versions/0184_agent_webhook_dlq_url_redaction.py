"""Bug-8350 — stop persisting the plaintext webhook DLQ destination URL.

``agent_webhook_dlq.target_url`` stored the complete destination URL,
including any bearer token / API key embedded in userinfo, a query
parameter, or a path segment (e.g.
``https://receiver.example/hooks/<token>?key=<secret>``). That column is
copied into database backups and returned verbatim by ``GET .../dlq``.

This migration:
  1. Adds ``target_host`` (sanitised ``scheme://host[:port]`` hint — no
     credentials, path, query, or fragment; the reported repro embedded the
     secret in the *path*, so the path is dropped too, not just the query
     string) and ``target_url_hash`` (bcrypt-backed fingerprint, Bug-8350 R2
     MED-1 — a bare SHA-256 digest is offline-brute-forceable at tens of
     thousands of candidates/sec against a stolen DB copy; bcrypt's slow
     cost factor makes that infeasible, mirroring
     ``services/model-service/src/auth/pat.py``'s bcrypt token hashing).
     ``target_url_hash`` is populated by the LIVE dispatcher
     (``shared.webhooks.redact.hash_url_secure``) going forward; this
     migration deliberately does NOT backfill it for legacy rows (see the
     backfill note below) — only ``target_host`` and ``last_error`` are
     backfilled.
  2. Backfills ``target_host`` from any existing ``target_url`` values so
     rows created before this migration keep an operator-visible hint
     instead of losing all context. Bug-8350 R2 MED-2: also scrubs any
     embedded URL out of existing ``last_error`` values in the same pass —
     the write path scrubs ``last_error`` going forward, but a row written
     before that fix shipped can still carry a raw, secret-bearing URL an
     HTTP client echoed back in its exception message.
     ``target_url_hash`` is intentionally left NULL for backfilled rows
     (fresh-reviewer finding on the R2 gate lane): nothing in the codebase
     reads ``target_url_hash`` today, and bcrypt's deliberately slow cost
     factor (~0.3s/row measured) would turn backfilling any real DLQ table
     into a multi-minute single-transaction migration step for a column
     with no current consumer.
  3. Drops ``target_url`` — a manual DLQ retry already reloads the live URL
     from ``ProjectAgentConfig`` at retry time, so the plaintext is not
     needed again once a row exists.

Tenant-schema guarded (skip when the schema has no ``agent_webhook_dlq``
table), idempotent, reversible (downgrade recreates ``target_url`` as an
empty string — the original plaintext is gone by design and cannot be
restored).

Bug-8349 R2 HIGH (migration-robustness half of the same finding): a legacy
row with a malformed-port ``target_url`` (e.g. ``https://host:notaport/``)
used to crash this migration outright — ``urlparse(url).port`` raises
``ValueError`` for a syntactically invalid port, and that access was
unguarded. The transactional DDL rolled back cleanly (no corruption) but the
whole tenant was blocked from upgrading past 0183 until the dirty row was
manually fixed. ``_redact`` and the hash/scrub helpers below now tolerate
that case instead of raising.

Revision ID: 0184
Revises: 0183
Create Date: 2026-07-28
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

import sqlalchemy as sa
from alembic import op

revision = "0184"
down_revision = "0183"
branch_labels = None
depends_on = None

_TABLE = "agent_webhook_dlq"
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_index(table: str, index_name: str) -> bool:
    bind = op.get_bind()
    return any(ix["name"] == index_name for ix in sa.inspect(bind).get_indexes(table))


def _redact(url: str) -> str:
    """``scheme://host[:port]`` only -- mirrors
    shared.webhooks.redact.redact_url_for_display. Path is dropped
    deliberately: the reported repro embedded the secret in the path.

    Bug-8349 R2 HIGH: ``parsed.port`` raises ``ValueError`` for a
    syntactically malformed port (e.g. ``https://host:notaport/hook``) —
    that access must be guarded separately from the ``urlparse`` guard
    above it, or exactly one dirty legacy row aborts the whole migration
    for that tenant."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable-url>"
    netloc = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        return "<unparseable-url>"
    if port:
        netloc = f"{netloc}:{port}"
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def _scrub_url_from_text(text: str | None, url: str | None) -> str | None:
    """Strip URL(s) out of free text -- duplicated from
    ``shared.webhooks.redact.scrub_url_from_text`` for the same
    self-containment reason as ``_redact`` (Bug-8350 R2 MED-2)."""
    if not text:
        return text
    scrubbed = text
    if url:
        scrubbed = scrubbed.replace(url, "<redacted-url>")
    return _URL_PATTERN.sub("<redacted-url>", scrubbed)


def upgrade() -> None:
    if not _table_exists(_TABLE):
        return

    if not _has_column(_TABLE, "target_host"):
        op.add_column(_TABLE, sa.Column("target_host", sa.Text(), nullable=True))
    if not _has_column(_TABLE, "target_url_hash"):
        op.add_column(
            _TABLE, sa.Column("target_url_hash", sa.String(length=64), nullable=True)
        )
    # Reviewer follow-up: index creation is guarded independently of the
    # column-add above (not nested inside the same `if not _has_column`
    # block) so a re-run after a partial failure between "column added" and
    # "index created" still creates the missing index, instead of the
    # column-exists check permanently short-circuiting it.
    if not _has_index(_TABLE, "ix_agent_webhook_dlq_target_url_hash"):
        op.create_index(
            "ix_agent_webhook_dlq_target_url_hash", _TABLE, ["target_url_hash"],
        )

    if _has_column(_TABLE, "target_url"):
        bind = op.get_bind()
        rows = bind.execute(
            sa.text(f'SELECT id, target_url, last_error FROM "{_TABLE}"')
        ).fetchall()
        for row_id, target_url, last_error in rows:
            # Bug-8350 R2 MED-2 — scrub any embedded URL out of a legacy
            # last_error even when target_url itself is empty/NULL: the
            # write path's own scrub sweeps for ANY bare http(s):// substring
            # (not just the specific request URL), so a legacy row can carry
            # a leaked URL in last_error independent of whether target_url
            # was populated.
            scrubbed_error = _scrub_url_from_text(last_error, target_url)
            if not target_url:
                if scrubbed_error != last_error:
                    bind.execute(
                        sa.text(
                            f'UPDATE "{_TABLE}" SET last_error = :err '
                            f"WHERE id = :id"
                        ),
                        {"err": scrubbed_error, "id": row_id},
                    )
                continue
            # Bug-8349 R2 gate follow-up (fresh-reviewer finding on this
            # lane) — legacy rows are backfilled with ``target_host`` and a
            # scrubbed ``last_error`` only; ``target_url_hash`` is left NULL
            # here rather than computed with bcrypt. Nothing in the
            # codebase reads ``target_url_hash`` today (it is write-only,
            # produced by the live dispatcher and never queried or
            # returned), and bcrypt's deliberately slow cost factor
            # (~0.3s/row measured) turns a backfill of any real DLQ table
            # into a multi-minute single-transaction migration step,
            # entirely for a value with no current consumer. New rows
            # written after this migration by the live dispatcher still get
            # a bcrypt-backed hash (``shared.webhooks.redact.hash_url_secure``)
            # — this only affects historical rows that already existed
            # before the upgrade.
            bind.execute(
                sa.text(
                    f'UPDATE "{_TABLE}" SET target_host = :host, '
                    f"last_error = :err WHERE id = :id"
                ),
                {
                    "host": _redact(target_url),
                    "err": scrubbed_error,
                    "id": row_id,
                },
            )
        op.drop_column(_TABLE, "target_url")


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if not _has_column(_TABLE, "target_url"):
        # Original plaintext is gone by design; downgrade restores the
        # column shape only, not the data.
        op.add_column(
            _TABLE,
            sa.Column(
                "target_url", sa.Text(), nullable=False, server_default=""
            ),
        )
        op.alter_column(_TABLE, "target_url", server_default=None)
    if _has_index(_TABLE, "ix_agent_webhook_dlq_target_url_hash"):
        op.drop_index("ix_agent_webhook_dlq_target_url_hash", table_name=_TABLE)
    if _has_column(_TABLE, "target_url_hash"):
        op.drop_column(_TABLE, "target_url_hash")
    if _has_column(_TABLE, "target_host"):
        op.drop_column(_TABLE, "target_host")
