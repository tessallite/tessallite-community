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
from typing import Awaitable, Callable, Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

SLUG_MAX_LEN = 64
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_SLUG_RE_UNDERSCORE = re.compile(r"[^a-z0-9]+")

# Bug-6291 / Bug-5513: BI-safe slug contract.  Model slugs containing
# hyphens, spaces, or special characters break Excel, Power BI, and
# DBeaver parsing.  This pattern mirrors the ModelCreate/ModelUpdate
# validator in shared/schemas/domains/models_sources.py.
_BI_SAFE_SLUG_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BI_SAFE_SLUG_MSG = (
    "Slug must contain only letters, digits, and underscores, "
    "and must start with a letter or underscore. "
    "Hyphens, spaces, and special characters are not allowed because "
    "BI clients (Excel, Power BI, DBeaver) parse them as operators."
)


def validate_bi_safe_slug(slug: str, *, label: str = "Slug") -> None:
    """Raise ``ValueError`` if *slug* violates the BI-safe contract.

    This is the chokepoint validator that every import path (YAML, JSON,
    project bundle, ecosystem mappers) inherits via
    :func:`insert_model_with_slug_retry`.  It mirrors the Pydantic
    ``ModelCreate``/``ModelUpdate`` guard (Bug-5513) so that no import
    path can produce slugs the CRUD API would reject.
    """
    if not slug or not _BI_SAFE_SLUG_RE.match(slug):
        raise ValueError(
            f"{label} '{slug}' is not BI-safe. {_BI_SAFE_SLUG_MSG}"
        )


def slugify(name: str, *, fallback: str = "imported_model", separator: str = "_") -> str:
    """Lowercase, collapse disallowed runs, strip edges, bound to 64 chars.

    Bug-6291: default separator changed from ``-`` to ``_`` so that the
    output is BI-safe (``^[A-Za-z_][A-Za-z0-9_]*$``).  Callers that need
    a different separator (none currently do) can pass it explicitly.

    Bug-7622: guarantee a BI-safe *start* character. A source name whose
    slugified form begins with a digit (e.g. ``123sales`` -> ``123sales``) or
    an empty result from a symbol-only name (e.g. ``$$$`` -> ````) previously
    passed straight into ``validate_bi_safe_slug`` and raised ``ValueError``
    (an uncaught 500 in the ecosystem/catalog import endpoints). Empty results
    fall back; a digit-leading result is prefixed with ``_`` so the output
    always satisfies ``^[A-Za-z_]...`` while staying inside the 64-char bound.
    This is the single BI-safe slug generator every import path should use for
    name derivation.
    """
    pattern = _SLUG_RE_UNDERSCORE if separator == "_" else _SLUG_RE
    out = pattern.sub(separator, (name or "").lower().strip()).strip(separator)
    out = out or fallback
    # A leading digit is a valid slug *character* but not a valid slug *start*
    # under the BI-safe contract. Prefix an underscore (itself a legal start
    # character) rather than dropping the leading digit, so distinct names like
    # ``1_sales`` and ``2_sales`` stay distinct.
    if out and out[0].isdigit():
        out = f"_{out}"
    return out[:SLUG_MAX_LEN]


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


_SUFFIX_RE = re.compile(r"_(\d+)$")


def _parse_suffix_number(candidate: str) -> int:
    """Return the next suffix number to try after ``candidate``.

    If ``candidate`` already carries a ``_N`` suffix, return ``N + 1`` so the
    retry loop continues from there rather than resetting to 2 (Bug-5870).
    If there is no numeric suffix (i.e. the candidate equals the bare base),
    return 2 — the first suffix that ``slug_with_suffix`` will append.
    """
    m = _SUFFIX_RE.search(candidate)
    if m:
        return int(m.group(1)) + 1
    return 2


async def insert_model_with_slug_retry(
    db: AsyncSession,
    *,
    project_id,
    base_slug: str,
    existing_slugs: set[str],
    display_name: str,
    new_model_id=None,
    max_attempts: int = 50,
    cap_guard: Callable[[], Awaitable[None]] | None = None,
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

    # Bug-6291: enforce the BI-safe slug contract at the shared
    # chokepoint so every import path (YAML, JSON bundle, project
    # bundle, ecosystem mappers) inherits the guard.
    validate_bi_safe_slug(base_slug, label="Model slug")

    if new_model_id is None:
        new_model_id = _uuid.uuid4()

    candidate = resolve_slug_collision(base_slug, existing_slugs)
    attempt = 0
    # Track the numeric suffix so a race collision advances from where we
    # are, not from 2.  If resolve_slug_collision already picked e.g.
    # ``sales_51``, the next retry after a race should try ``sales_52``,
    # not ``sales_2`` (Bug-5870).
    n = _parse_suffix_number(candidate)

    while True:
        attempt += 1
        if cap_guard is not None:
            await cap_guard()
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
            # Re-parse in case the candidate itself carried a suffix (race on
            # a suffixed slug); start from the *next* number after the
            # colliding candidate's suffix.
            n = _parse_suffix_number(candidate)
            candidate = slug_with_suffix(base_slug, n)
