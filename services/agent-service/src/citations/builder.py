"""Citation chip data emission per spec §5 / B2.3.

After a query returns rows, we emit one citation per resolved measure /
dimension involved in the answer. Stable ids (per H5) come from the
Measure/Dimension UUIDs in tess; display name is the canonical name the
binder accepted. Each citation may carry a `value` (the measure value
from the first row) so the chip can render the headline figure inline.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Dimension, Measure


async def build_citations(
    db: AsyncSession,
    model_id: UUID,
    measure_names: list[str],
    dimension_names: list[str],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []

    if measure_names:
        m_q = await db.execute(
            select(Measure).where(
                Measure.model_id == model_id,
                Measure.name.in_(measure_names),
            )
        )
        first_row = rows[0] if rows else {}
        for m in m_q.scalars().all():
            citations.append(
                {
                    "kind": "measure",
                    "id": str(m.id),
                    "name": m.name,
                    "display_name": m.display_name or m.name,
                    "value": first_row.get(m.name),
                }
            )

    if dimension_names:
        d_q = await db.execute(
            select(Dimension).where(
                Dimension.model_id == model_id,
                Dimension.name.in_(dimension_names),
            )
        )
        for d in d_q.scalars().all():
            citations.append(
                {
                    "kind": "dimension",
                    "id": str(d.id),
                    "name": d.name,
                    "display_name": d.display_name or d.name,
                    "value": None,
                }
            )

    return citations
