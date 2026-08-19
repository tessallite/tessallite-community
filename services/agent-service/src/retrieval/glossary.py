"""Glossary retrieval for prompt grounding.

Token-overlap baseline (no vector store). For each allow-listed model we
pull GlossaryEntry rows, score against the user question + conversation
history, and return matches with score > 0 (Bug-7931 — no zero-score
padding; irrelevant cards are distractors, not context).

The prompt assembler chooses between three glossary modes based on an
attention budget (spec 3.4): the FULL glossary in the stable cacheable
prefix when it fits, a compact TERM INDEX (always-on) plus score>0
retrieved cards otherwise, or retrieval-only above the index budget. This
module supplies the primitives — ``list_glossary_cards`` (full,
deterministic order, the single DB load), plus the pure derivations
``term_index_from_cards`` and ``score_cards`` (and the
``retrieve_glossary_cards`` convenience wrapper) — and leaves the
policy to the assembler.

list_model_attributes() filters hidden columns at both the direct
source_column_id level and transitively through UDA column_refs, so
UDA-backed dimensions/measures whose expressions reference hidden columns
are excluded from the LLM prompt.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    Dimension,
    GlossaryEntry,
    GlossarySynonym,
    Measure,
    Model,
    ModelAliasMap,
    ModelColumn,
    ModelTable,
    UserDefinedAttributeColumnRef,
)

logger = logging.getLogger(__name__)

_TOP_K = 20
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class GlossaryCard:
    model_id: UUID
    term: str
    definition: str
    synonyms: list[str]
    sample_values: list[str] | None = None
    # Bug-7931 rendering detail — carry the model slug so the assembler can
    # prefix cards with the stable slug instead of the volatile UUID (which
    # also makes the full-glossary prefix human-legible and cache-stable).
    model_slug: str | None = None


@dataclass
class GlossaryTermIndexEntry:
    """One line of the always-on compact term index (two-tier mode): the term,
    its synonyms, and the model slug it belongs to. Definitions are NOT
    included — the full card is retrieved on demand when relevant."""
    model_id: UUID
    model_slug: str | None
    term: str
    synonyms: list[str]


@dataclass
class AliasMapBlock:
    model_id: UUID
    pairs: dict[str, str]


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1}


def _score(query_tokens: set[str], entry_tokens: set[str]) -> int:
    if not query_tokens or not entry_tokens:
        return 0
    return len(query_tokens & entry_tokens)


async def _load_admissible_entries(
    db: AsyncSession,
    model_ids: list[UUID],
) -> tuple[list[GlossaryEntry], dict[UUID, list[str]], dict[UUID, str | None]]:
    """Fetch approved, visible, medium/high-confidence glossary entries for the
    given models, their synonyms, and a model_id -> slug map. Shared by the
    full-glossary, term-index, and retrieval primitives so they all draw from
    exactly the same admissible set."""
    entries_q = await db.execute(
        select(GlossaryEntry).where(
            GlossaryEntry.model_id.in_(model_ids),
            GlossaryEntry.status == "approved",
            func.coalesce(GlossaryEntry.visibility, "show") == "show",
            func.coalesce(GlossaryEntry.confidence, "medium").in_(
                ["high", "medium"]
            ),
        )
    )
    entries: list[GlossaryEntry] = list(entries_q.scalars().all())
    if not entries:
        return [], {}, {}

    syn_q = await db.execute(
        select(GlossarySynonym).where(
            GlossarySynonym.entry_id.in_([e.id for e in entries])
        )
    )
    syn_by_entry: dict[UUID, list[str]] = {}
    for syn in syn_q.scalars().all():
        syn_by_entry.setdefault(syn.entry_id, []).append(syn.synonym)
    for syns in syn_by_entry.values():
        syns.sort()

    slug_q = await db.execute(
        select(Model.id, Model.slug).where(Model.id.in_(model_ids))
    )
    slug_by_model: dict[UUID, str | None] = {
        mid: slug for mid, slug in slug_q.all()
    }
    return entries, syn_by_entry, slug_by_model


def _card_token_bag(c: GlossaryCard) -> set[str]:
    bag = (
        _tokens(c.term)
        | _tokens(c.definition)
        | _tokens(" ".join(c.synonyms))
    )
    if c.sample_values:
        bag |= _tokens(" ".join(str(v) for v in c.sample_values))
    return bag


def score_cards(
    cards: list[GlossaryCard],
    user_message: str,
    conversation_context: str = "",
    top_k: int = _TOP_K,
) -> list[GlossaryCard]:
    """Rank already-loaded cards by token overlap with the question (+ recent
    history) and return ONLY cards with a positive score (Bug-7931 — no
    zero-score padding), capped at ``top_k``. Pure function so the assembler's
    budget policy can derive retrieval from a single glossary load."""
    qtokens = _tokens(user_message) | _tokens(conversation_context)
    scored = [
        (_score(qtokens, _card_token_bag(c)), c)
        for c in cards
    ]
    # Bug-7931 — drop zero-score cards entirely. A zero-overlap card is a
    # candidate wrong term-resolution (distractor), not useful shape context;
    # the always-on full glossary / term index (assembler policy) is the
    # correct way to expose model shape.
    positive = [(s, c) for s, c in scored if s > 0]
    positive.sort(key=lambda t: (-t[0], t[1].term.lower()))
    return [c for _s, c in positive[:top_k]]


def term_index_from_cards(
    cards: list[GlossaryCard],
) -> list[GlossaryTermIndexEntry]:
    """Derive the compact always-on term index (term + synonyms, no
    definitions) from already-loaded cards (two-tier mode, spec 3.4.2): the
    planner always knows THAT a term exists and what it maps to; full cards are
    retrieved by relevance into the per-turn suffix. Order follows the cards'
    (already deterministic) order."""
    return [
        GlossaryTermIndexEntry(
            model_id=c.model_id,
            model_slug=c.model_slug,
            term=c.term,
            synonyms=c.synonyms,
        )
        for c in cards
    ]


async def list_glossary_cards(
    db: AsyncSession,
    model_ids: Iterable[UUID],
) -> list[GlossaryCard]:
    """Return EVERY admissible glossary card for the given models in a
    deterministic order (by model slug, then term, then entry id). Used for the
    full-glossary-in-the-cacheable-prefix mode (spec 3.4.1): byte-stable across
    turns so the stable prefix stays cache-eligible until the glossary is
    edited. The entry-id tiebreaker matters — terms are not unique per model
    (case variants / duplicates), and without it the order would fall back to
    nondeterministic DB scan order, silently breaking the stable prefix."""
    model_ids = list(model_ids)
    if not model_ids:
        return []
    entries, syn_by_entry, slug_by_model = await _load_admissible_entries(
        db, model_ids
    )
    entries.sort(
        key=lambda e: (
            slug_by_model.get(e.model_id) or str(e.model_id),
            e.term.lower(),
            str(e.id),
        )
    )
    return [
        GlossaryCard(
            model_id=e.model_id,
            term=e.term,
            definition=e.definition,
            synonyms=syn_by_entry.get(e.id, []),
            sample_values=e.sample_values,
            model_slug=slug_by_model.get(e.model_id),
        )
        for e in entries
    ]


async def retrieve_glossary_cards(
    db: AsyncSession,
    model_ids: Iterable[UUID],
    user_message: str,
    conversation_context: str = "",
    top_k: int = _TOP_K,
) -> list[GlossaryCard]:
    """Load admissible glossary cards and rank them by relevance — thin wrapper
    over ``list_glossary_cards`` + ``score_cards`` for callers that want
    retrieval in one call."""
    model_ids = list(model_ids)
    if not model_ids:
        return []
    cards = await list_glossary_cards(db, model_ids)
    return score_cards(cards, user_message, conversation_context, top_k)


async def retrieve_alias_maps(
    db: AsyncSession,
    model_ids: Iterable[UUID],
) -> list[AliasMapBlock]:
    model_ids = list(model_ids)
    if not model_ids:
        return []
    # Deterministic order (cache-prefix byte-stability): the alias-map blocks
    # render into the stable GROUNDING system section, so an unordered read
    # would reshuffle them between calls and break the cacheable prefix.
    q = await db.execute(
        select(ModelAliasMap)
        .where(ModelAliasMap.model_id.in_(model_ids))
        .order_by(ModelAliasMap.model_id)
    )
    out: list[AliasMapBlock] = []
    for row in q.scalars().all():
        if not row.alias_map:
            continue
        out.append(
            AliasMapBlock(
                model_id=row.model_id,
                pairs=dict(row.alias_map),
            )
        )
    return out


async def _load_hidden_column_ids(
    db: AsyncSession,
    model_id: UUID,
) -> set:
    """Return the set of ModelColumn ids whose is_hidden flag is true."""
    result = await db.execute(
        select(ModelColumn.id)
        .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
        .where(ModelTable.model_id == model_id)
        .where(ModelColumn.is_hidden.is_(True))
    )
    return {row[0] for row in result.all()}


async def _load_hidden_uda_ids(
    db: AsyncSession,
    hidden_column_ids: set,
) -> set:
    """Return UDA IDs where any referenced column is hidden."""
    if not hidden_column_ids:
        return set()
    result = await db.execute(
        select(UserDefinedAttributeColumnRef.attribute_id).where(
            UserDefinedAttributeColumnRef.column_id.in_(hidden_column_ids)
        )
    )
    return {row[0] for row in result.all()}


def _is_visible(
    col_id, uda_id, hidden_col_ids: set, hidden_uda_ids: set,
) -> bool:
    if col_id is not None:
        return col_id not in hidden_col_ids
    if uda_id is not None:
        return uda_id not in hidden_uda_ids
    return True


async def list_model_attributes(
    db: AsyncSession,
    model_id: UUID,
) -> tuple[list[str], list[str]]:
    """Return (measure_names, dimension_names) for a model — used by the
    prompt assembler so the LLM has a concrete name list to choose from.

    Excludes invalid objects, objects whose source column is hidden,
    and UDA-backed objects whose expression references any hidden column."""

    hidden_col_ids = await _load_hidden_column_ids(db, model_id)
    hidden_uda_ids = await _load_hidden_uda_ids(db, hidden_col_ids)

    m_q = await db.execute(
        select(
            Measure.name,
            Measure.source_column_id,
            Measure.user_defined_attribute_id,
        ).where(
            Measure.model_id == model_id,
            Measure.is_invalid.is_(False),
        )
    )
    d_q = await db.execute(
        select(
            Dimension.name,
            Dimension.source_column_id,
            Dimension.user_defined_attribute_id,
        ).where(
            Dimension.model_id == model_id,
            Dimension.is_invalid.is_(False),
        )
    )
    measures = sorted([
        str(name) for name, col_id, uda_id in m_q.all()
        if _is_visible(col_id, uda_id, hidden_col_ids, hidden_uda_ids)
    ])
    dimensions = sorted([
        str(name) for name, col_id, uda_id in d_q.all()
        if _is_visible(col_id, uda_id, hidden_col_ids, hidden_uda_ids)
    ])
    return measures, dimensions
