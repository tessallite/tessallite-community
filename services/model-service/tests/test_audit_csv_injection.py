"""Bug-6314 [SECURITY] regression — audit CSV export must neutralise
CSV/formula injection.

Audit fields (actor_email, action, target_name, detail) carry
attacker-influenced text. A spreadsheet treats a cell that begins with a
formula trigger (``=``, ``+``, ``-``, ``@``) or a control byte as a live
formula, so a crafted value executes when a compliance admin opens the export.
The export prefixes such cells with a single quote (OWASP mitigation). These
tests lock that behaviour, which previously shipped with no coverage.
"""
from __future__ import annotations

import csv
import io
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.api.audit import _csv_safe
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import NOW, TEST_TENANT, TEST_USER_ID, async_gen_from, make_mock_db

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _csv_safe — pure-function unit coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger", ["=", "+", "-", "@", "\t", "\r", "\n"]
)
def test_csv_safe_prefixes_formula_triggers(trigger):
    payload = f"{trigger}cmd|'/c calc'!A1"
    out = _csv_safe(payload)
    assert out == "'" + payload
    assert out[0] == "'"


def test_csv_safe_leaves_plain_text_untouched():
    assert _csv_safe("model.deploy") == "model.deploy"
    assert _csv_safe("user@test.com") == "user@test.com"  # '@' not leading


def test_csv_safe_handles_none_and_nonstring():
    assert _csv_safe(None) == ""
    assert _csv_safe({"k": "v"}).startswith("{")


# ---------------------------------------------------------------------------
# Export endpoint — end-to-end neutralisation
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    user = CurrentUser(
        user_id=TEST_USER_ID, tenant_id=TEST_TENANT,
        email=TEST_USER_ID, role="tenant_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_export_neutralises_injection_in_target_name(admin_user):
    """A crafted target_name is written as inert text (leading quote), not a
    live formula, in the CSV export."""
    malicious = "=cmd|'/c calc'!A1"
    mock_event = types.SimpleNamespace(
        id=uuid.uuid4(), timestamp=NOW, actor_id=None,
        actor_email="attacker@test.com", action="model.deploy",
        target_type="model", target_id=uuid.uuid4(),
        target_name=malicious, severity="warn", detail=None, ip_address=None,
    )
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = [mock_event]
    db = make_mock_db()
    db.execute = AsyncMock(return_value=rows_result)

    with patch("src.api.audit.get_tenant_db", async_gen_from(db)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/api/v1/admin/audit-events/export")

    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.text)))
    # Header + one data row.
    data_row = rows[1]
    target_name_cell = data_row[4]  # timestamp, actor, action, target_type, target_name
    assert target_name_cell == "'" + malicious
    # The raw formula must never appear unescaped as a cell value.
    assert malicious not in data_row
