"""Bug-6138: every new model must get the canonical Technical persona seeded.

Migrations 0042 (seed) + 0124 (audience gate) only covered models that existed
at migration time; models created afterwards had an inert technical flow because
``create_model`` never seeded the persona. These tests pin the seed helper.
"""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

from shared.auth.roles import MODEL_TECHNICAL_ROLE
from src.api.personas import (
    TECHNICAL_PERSONA_NAME,
    TECHNICAL_PERSONA_SLUG,
    seed_technical_persona,
)


@pytest.mark.asyncio
async def test_seed_creates_technical_persona_when_absent():
    """A model with no technical persona gets one with the canonical shape:
    hidden-columns exposed, gated to the model_technical audience role, no
    row-security bypass."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # none exists yet
    db.execute = AsyncMock(return_value=result)
    added = []
    db.add = MagicMock(side_effect=added.append)

    model_id = uuid4()
    persona = await seed_technical_persona(db, model_id)

    assert added == [persona]
    assert persona.model_id == model_id
    assert persona.slug == TECHNICAL_PERSONA_SLUG
    assert persona.name == TECHNICAL_PERSONA_NAME
    assert persona.includes_hidden_columns is True
    assert persona.bypass_row_security is False
    assert persona.audience_roles == [MODEL_TECHNICAL_ROLE]
    assert persona.included_measure_ids == []
    assert persona.included_dimension_ids == []
    assert persona.included_hierarchy_ids == []
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_is_idempotent_when_already_present():
    """If a technical persona already exists (imported bundle / migration /
    re-run) the helper returns it and creates nothing."""
    db = AsyncMock()
    existing = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()

    persona = await seed_technical_persona(db, uuid4())

    assert persona is existing
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
