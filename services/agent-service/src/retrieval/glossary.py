"""Glossary retrieval for prompt grounding.

Token-overlap baseline (no vector store). For each allow-listed model we
pull GlossaryEntry rows, score against the user question + conversation
history, and return top K=20.

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


async def retrieve_glossary_cards(
    db: AsyncSession,
    model_ids: Iterable[UUID],
    user_message: str,
    conversation_context: str = "",
    top_k: int = _TOP_K,
) -> list[GlossaryCard]:
    """Pull all glossary entries for the given models, rank by token overlap,
    return top-K. Synonyms count toward the term's token bag."""

    model_ids = list(model_ids)
    if not model_ids:
        return []

    qtokens = _tokens(user_message) | _tokens(conversation_context)

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
        return []

    syn_q = await db.execute(
        select(GlossarySynonym).where(
            GlossarySynonym.entry_id.in_([e.id for e in entries])
        )
    )
    syn_by_entry: dict[UUID, list[str]] = {}
    for syn in syn_q.scalars().all():
        syn_by_entry.setdefault(syn.entry_id, []).append(syn.synonym)

    scored: list[tuple[int, GlossaryEntry, list[str]]] = []
    for e in entries:
        synonyms = syn_by_entry.get(e.id, [])
        bag = _tokens(e.term) | _tokens(e.definition) | _tokens(" ".join(synonyms))
        if e.sample_values:
            bag |= _tokens(" ".join(str(v) for v in e.sample_values))
        scored.append((_score(qtokens, bag), e, synonyms))

    # Always include items with score>0; if too few, pad with highest-scoring zero-score
    # entries up to top_k so the LLM still sees the shape of the model.
    scored.sort(key=lambda t: (-t[0], t[1].term.lower()))
    selected = scored[:top_k]

    return [
        GlossaryCard(
            model_id=e.model_id,
            term=e.term,
            definition=e.definition,
            synonyms=syns,
            sample_values=e.sample_values,
        )
        for _score, e, syns in selected
    ]


async def retrieve_alias_maps(
    db: AsyncSession,
    model_ids: Iterable[UUID],
) -> list[AliasMapBlock]:
    model_ids = list(model_ids)
    if not model_ids:
        return []
    q = await db.execute(
        select(ModelAliasMap).where(ModelAliasMap.model_id.in_(model_ids))
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
