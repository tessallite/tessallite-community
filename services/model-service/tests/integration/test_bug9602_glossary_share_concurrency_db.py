"""Real-PostgreSQL Bug-9602 proof for concurrent share regeneration.

Two regenerations for one model must serialize on the canonical model advisory
lock. Each request must revoke the rows visible in its transaction and issue a
replacement in the same transaction, leaving exactly one active token after
both requests commit. Mocked sessions cannot prove the interleaving or the
transaction-scoped lock, so this suite skips unless an approved disposable
PostgreSQL URL is supplied.

Run:
    cd tessallite/services/model-service
    TESSALLITE_TESTING=1 \
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_bug9602_glossary_share_concurrency_db.py -v
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from shared.auth.middleware import CurrentUser
from shared.db.models import AuditEvent, GlossaryShareToken, Model, Project
from src.api import glossary

from tests.integration.test_versioning_consistency_db import (  # noqa: E402
    _DB_URL,
    _isolated_schema,
)

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="no disposable versioning DB URL configured")
async def test_concurrent_regeneration_leaves_one_active_replacement():
    async with _isolated_schema() as (factory, _schema):
        project_id, model_id = uuid.uuid4(), uuid.uuid4()
        old_token_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        async with factory() as setup:
            setup.add(
                Project(
                    id=project_id,
                    slug=f"share-p-{project_id.hex[:8]}",
                    display_name="Share concurrency project",
                )
            )
            setup.add(
                Model(
                    id=model_id,
                    project_id=project_id,
                    slug=f"share-m-{model_id.hex[:8]}",
                    display_name="Share concurrency model",
                    seed=uuid.uuid4().hex,
                )
            )
            await setup.flush()
            setup.add(
                GlossaryShareToken(
                    id=old_token_id,
                    model_id=model_id,
                    created_by=actor_id,
                )
            )
            await setup.commit()

        user = CurrentUser(
            user_id=str(actor_id),
            tenant_id="disposable-share-tenant",
            email="share-concurrency@example.com",
        )

        async def _tenant_db(_tenant_id):
            async with factory() as session:
                yield session

        async def _run_regeneration():
            return await glossary.regenerate_share_token(project_id, model_id, user)

        with (
            patch("src.api.glossary.get_tenant_db", _tenant_db),
            patch("src.api.glossary.emit_webhook", new=AsyncMock()),
        ):
            first, second = await asyncio.gather(
                _run_regeneration(),
                _run_regeneration(),
            )

        first_jti = glossary._decode_public_token(first["token"])[2]
        second_jti = glossary._decode_public_token(second["token"])[2]
        assert first_jti != second_jti

        async with factory() as verify:
            rows = (
                await verify.execute(
                    select(GlossaryShareToken).where(
                        GlossaryShareToken.model_id == model_id
                    )
                )
            ).scalars().all()
            active = [row for row in rows if row.revoked_at is None]
            assert len(rows) == 3, "two regenerations must each issue one replacement"
            assert len(active) == 1, (
                "Bug-9602: concurrent regeneration left more than one active "
                "replacement token"
            )
            assert active[0].id in {first_jti, second_jti}
            assert all(
                row.id != old_token_id or row.revoked_at is not None
                for row in rows
            )

            audits = (
                await verify.execute(
                    select(AuditEvent).where(
                        AuditEvent.target_id == model_id,
                    )
                )
            ).scalars().all()
            actions = [event.action for event in audits]
            assert actions.count("glossary.share_token.revoke") == 2
            assert actions.count("glossary.share_token.issue") == 2
