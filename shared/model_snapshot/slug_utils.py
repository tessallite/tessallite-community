"""Slug helpers shared by every import path (F-020-19, F-020-22).

Model slugs live in a ``String(64)`` column (``shared/db/models.py``). Every
importer (snapshot import, YAML, dbt, Cube, AtScale, catalog) needs to:

  1. Slugify a human name into the allowed character set, bounded to 64 chars.
  2. Resolve collisions against the project's existing slugs by appending a
     ``_{n}`` suffix — *without* overflowing the 64-char column (F-020-19): a
     64-char base plus ``_12`` would be 67 chars and raise a DataError/500.
  3. Survive the concurrent-import race (F-020-22): two replicas importing the
     same bundle pick the same in-memory candidate and one insert hits the
     ``models_project_id_slug`` unique constraint. The candidate generator is
     deterministic and pure; ``insert_model_with_slug_retry`` wraps the insert
     in a SAVEPOINT and, on a unique violation, advances to the next suffix and
     retries — bounded — so the race degrades to a higher suffix, never a 500.

These were duplicated (and only partially correct) across the importers; this
module is the single implementation they all call.
"""
from __future__ import annotations

import re
import uuid as _uuid
from typing import Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

SLUG_MAX_LEN = 64
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_SLUG_RE_UNDERSCORE = re.compile(r"[^a-z0-9]+")


def slugify(name: str, *, fallback: str = "imported-model", separator: str = "-") -> str:
    """Lowercase, collapse disallowed runs, strip edges, bound to 64 chars.

    ``separator`` is ``-`` for the model-import path (matches the historic
    ``import_export._slugify``) and ``_`` for the ecosystem importers (dbt/Cube/
    AtScale/catalog), which slug with underscores.
    """
    pattern = _SLUG_RE_UNDERSCORE if separator == "_" else _SLUG_RE
    out = pattern.sub(separator, (name or "").lower().strip()).strip(separator)
    return (out or fallback)[:SLUG_MAX_LEN]


def slug_with_suffix(base: str, n: int) -> str:
    """Return ``base`` with a ``_{n}`` suffix, reserving headroom (F-020-19).

    The base is truncated so ``f"{base}_{n}"`` always fits in 64 chars. ``n``
    of 1 (or below) means "no suffix" and returns the bounded base unchanged.
    """
    bounded = (base or "")[:SLUG_MAX_LEN]
    if n <= 1:
        return bounded
    suffix = f"_{n}"
    return f"{bounded[: SLUG_MAX_LEN - len(suffix)]}{suffix}"


def resolve_slug_collision(base: str, existing: Iterable[str]) -> str:
    """Pick the first non-colliding ``base``/``base_n`` slug, headroom-safe.

    Pure and deterministic given ``existing`` — used for in-process collision
    resolution before insert. The cross-replica race is handled by
    :func:`insert_model_with_slug_retry`.
    """
    existing_set = set(existing)
    candidate = slug_with_suffix(base, 1)
    n = 2
    while candidate in existing_set:
        candidate = slug_with_suffix(base, n)
        n += 1
    return candidate


async def insert_model_with_slug_retry(
    db: AsyncSession,
    *,
    project_id,
    base_slug: str,
    existing_slugs: set[str],
    display_name: str,
    new_model_id=None,
    max_attempts: int = 50,
):
    """Insert a ``Model`` row, surviving concurrent slug races (F-020-22).

    Resolves an in-process collision-free candidate, then inserts inside a
    SAVEPOINT. If a concurrent import already took that slug (unique-constraint
    violation), advances the suffix and retries up to ``max_attempts``. The
    caller still owns the outer transaction/commit; the SAVEPOINT only protects
    the single insert so a failed attempt does not poison the session.

    Returns the created ``Model`` and adds its slug to ``existing_slugs`` so a
    subsequent model in the same bundle does not re-pick it.
    """
    from shared.db.models import Model

    if new_model_id is None:
        new_model_id = _uuid.uuid4()

    candidate = resolve_slug_collision(base_slug, existing_slugs)
    attempt = 0
    # Track the numeric suffix we are at so a race bumps to the next one.
    n = 2 if candidate != slug_with_suffix(base_slug, 1) else 2

    while True:
        attempt += 1
        model = Model(
            id=new_model_id,
            project_id=project_id,
            slug=candidate,
            display_name=display_name,
            seed=str(_uuid.uuid4()),
        )
        try:
            async with db.begin_nested():
                db.add(model)
                await db.flush()
            existing_slugs.add(candidate)
            return model, candidate
        except IntegrityError:
            # A concurrent import grabbed this slug between our read and insert.
            # The SAVEPOINT rolled the failed insert back; advance and retry.
            if attempt >= max_attempts:
                raise
            existing_slugs.add(candidate)
            candidate = slug_with_suffix(base_slug, n)
            n += 1
