"""Bug-7231: the ad-hoc KPI evaluation endpoint had NO rate limit.

Each ``POST .../kpis/evaluate-adhoc`` runs a live query against the source, and
the wizard preview fires one per keystroke, so an unbounded caller could storm
the source database with unsaved preview queries. A per-USER, config-driven,
bounded (and shared-capable) ceiling now caps it, enforced through the shared
action-quota engine (``shared.middleware.action_throttle``) — NOT the unbounded
per-process function attribute v5 used.

Test escape: no test exercised the ad-hoc endpoint's request volume; every
prior ad-hoc test asserted a single call's value/disposition.
Guard: this module. Tier: T1 (abuse control; execution scope: isolated).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shared.middleware.action_throttle import (
    consume_action_quota,
    reset_action_quotas,
)

from .conftest import (  # noqa: F401
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    TEST_USER_ID,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    f"/kpis/evaluate-adhoc"
)


@pytest.fixture(autouse=True)
def _reset_quota():
    reset_action_quotas()
    yield
    reset_action_quotas()


@pytest.mark.asyncio
async def test_bug7231_adhoc_evaluation_is_rate_limited_per_user(client):  # noqa: F811
    """With the per-user ceiling at 1/minute, an ad-hoc call made after the
    user's minute quota is already spent is rejected with HTTP 429.

    The quota is pre-exhausted with the SAME identity the endpoint keys on
    (action ``kpi.evaluate_adhoc`` scoped by tenant + user), so the endpoint's
    own ``consume_action_quota`` call is the one that trips. Pre-fix (no rate
    limit at all) the request would proceed and never return 429.
    """
    # Spend the single unit this user is allowed this minute, keyed exactly as
    # the endpoint does (see evaluate_adhoc: action, tenant, user, "N/minute").
    assert consume_action_quota(
        "kpi.evaluate_adhoc", TEST_TENANT, TEST_USER_ID, limit="1/minute"
    ) is True

    with patch("src.api.kpis.system_snapshot_get", return_value=1), \
         patch("src.api.kpis.get_tenant_db", async_gen_from(make_mock_db())):
        resp = await client.post(URL, json={"expression": "SUM(x)"})

    assert resp.status_code == 429
    assert "per minute" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_bug7231_adhoc_rate_limit_disabled_when_zero(client):  # noqa: F811
    """A ceiling of 0 disables the cap: the quota engine is never consulted and
    the request is never 429."""
    with patch("src.api.kpis.system_snapshot_get", return_value=0), \
         patch("src.api.kpis.get_tenant_db", async_gen_from(make_mock_db())), \
         patch("src.api.kpis.consume_action_quota", MagicMock()) as spy:
        resp = await client.post(URL, json={"expression": "SUM(x)"})

    assert resp.status_code != 429
    spy.assert_not_called()
