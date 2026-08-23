"""Bug-7257: fixedMembers preview count/truncation at the 100-member boundary.

The preview endpoint returns at most a 100-row slice of the declared members,
but ``total_count`` and ``truncated`` describe the FULL declared set, not the
slice. The pre-fix code derived both from the slice (``total_count = len(items)``,
``truncated = len(items) >= 100``), which:

  * lost any real total above 100 (a 250-member set reported 100), and
  * mis-flagged an exactly-100-member set as truncated.

``fixedMembers`` previews are computed in-process by ``_preview_from_builder``,
so these tests drive the real route code end to end with no router stub — the
count/truncation logic under test is exercised for real, not mocked.

Boundary: 100 -> not truncated, total 100; 101 -> truncated, total 101, preview
capped at 100; 250 -> total 250 (never 100), preview capped at 100.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,  # noqa: F401 -- pytest fixture
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/named-sets"

NS_ID = uuid.uuid4()

PREVIEW_CAP = 100  # server-side preview slice size the endpoint returns


def _members(n: int) -> list[str]:
    """A declared fixedMembers list of *n* distinct string members."""
    return [f"M{i:04d}" for i in range(n)]


def _live_set(members: list[str]):
    return types.SimpleNamespace(
        id=NS_ID,
        model_id=TEST_MODEL_ID,
        name="Members",
        display_name="Members",
        description=None,
        display_folder=None,
        scope=1,
        expression="{ [Dim].[M0000] }",
        dimensions="Dim",
        builder_definition={"type": "fixedMembers", "dimension": "Dim", "members": members},
        list_type="fixed",
        certification_status="draft",
        replacement_id=None,
        owner_user_id="owner@acme.com",
        created_at=NOW,
        updated_at=NOW,
    )


def _db(members: list[str]):
    """``ensure_model_in_project`` -> Model, then the live named-set row."""
    db = make_mock_db()
    db.get = AsyncMock(side_effect=[make_model(), _live_set(members)])
    return db


async def _preview(client, members: list[str]):  # noqa: F811
    """Drive the live (non-deployed) preview path for a fixedMembers set."""
    db = _db(members)
    with (
        patch("src.api.named_sets.get_tenant_db", async_gen_from(db)),
        patch(
            "src.api.named_sets.resolve_effective_persona",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.post(f"{PREFIX}/{NS_ID}/preview")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_exactly_100_members_is_not_truncated(client):  # noqa: F811
    """Bug-7257: exactly 100 is the full set, so truncated MUST be False.

    Fail-before: the slice-based code returned ``len(items) >= 100`` -> True.
    """
    payload = await _preview(client, _members(100))

    assert payload["total_count"] == 100
    assert payload["truncated"] is False
    assert len(payload["items"]) == 100


@pytest.mark.asyncio
async def test_101_members_is_truncated_and_reports_the_full_count(client):  # noqa: F811
    """Bug-7257: 101 overflows the preview, so total_count MUST be 101, not 100.

    Fail-before: the slice-based code reported ``total_count = len(items) = 100``.
    """
    payload = await _preview(client, _members(101))

    assert payload["total_count"] == 101
    assert payload["truncated"] is True
    assert len(payload["items"]) == PREVIEW_CAP


@pytest.mark.asyncio
async def test_large_set_reports_full_count_not_the_preview_cap(client):  # noqa: F811
    """Bug-7257: a 250-member set MUST report 250, never the 100-row cap.

    Fail-before: the slice-based code reported ``total_count = 100``.
    """
    payload = await _preview(client, _members(250))

    assert payload["total_count"] == 250
    assert payload["truncated"] is True
    assert len(payload["items"]) == PREVIEW_CAP
